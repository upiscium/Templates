"""Fail-closed recovery of lost ignored Task State for an exact Task/PR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path

import git_private_state as private_state
import task_contract as contract
import task_lifecycle as lifecycle


TASK_STATE_DIRECTORY = ".task-state"
RECOVERY_RECEIPT = "lost-ignored-task-state.json"
TEMPLATE_PATH = "components/agent-core/.automation/templates/task-state.md"
STATE_FILES = ("task.md", "issue.json", "contract.json")
ALLOWED_STATE_FILES = frozenset((*STATE_FILES, "work-units.lock"))
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
POSITIVE_RE = re.compile(r"^[1-9][0-9]*$")


class TaskStateRecoveryError(lifecycle.LifecycleError):
    """A recovery precondition or postcondition failed."""


def _oid(value: object, label: str) -> str:
    if not isinstance(value, str) or OID_RE.fullmatch(value) is None:
        raise TaskStateRecoveryError(f"{label} must be a full lowercase immutable revision")
    return value


def _number(value: str, label: str) -> int:
    if not POSITIVE_RE.fullmatch(value):
        raise TaskStateRecoveryError(f"{label} must be an exact positive decimal integer")
    return int(value)


def _git(root: Path, *args: str, check: bool = True) -> str:
    return lifecycle.git("--no-replace-objects", *args, cwd=root, check=check)


def _issue_runner(command: list[str], *, cwd: Path, **_: object):
    if not command or command[0] != "gh":
        raise TaskStateRecoveryError("Issue authority runner accepts only GitHub CLI commands")
    return lifecycle.gh(*command[1:], cwd=cwd, check=False)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TaskStateRecoveryError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TaskStateRecoveryError(f"unsafe {label}: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TaskStateRecoveryError(f"cannot read {label}: {path}") from exc


def _git_blob(root: Path, revision: str, relative: str) -> bytes:
    _oid(revision, "Git blob revision")
    try:
        result = lifecycle.run(
            ["git", "--no-replace-objects", "show", f"{revision}:{relative}"],
            cwd=root,
            check=True,
        )
        return result.stdout.encode("utf-8")
    except UnicodeError as exc:
        raise TaskStateRecoveryError(f"Git blob is not valid UTF-8: {relative}") from exc


def _remote_default_branch_and_revision(source_root: Path) -> tuple[str, str]:
    try:
        result = lifecycle.network_git(
            "ls-remote", "--symref", "origin", "HEAD", cwd=source_root
        )
    except lifecycle.LifecycleError as exc:
        raise TaskStateRecoveryError("cannot resolve the remote default branch") from exc
    symbolic = []
    revisions = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[1] != "HEAD":
            continue
        if fields[0].startswith("ref: "):
            symbolic.append(fields[0][5:])
        else:
            revisions.append(fields[0])
    if (
        len(symbolic) != 1
        or not symbolic[0].startswith("refs/heads/")
        or len(revisions) != 1
    ):
        raise TaskStateRecoveryError("remote default branch advertisement is invalid")
    branch = symbolic[0].removeprefix("refs/heads/")
    revision = _oid(revisions[0], "advertised remote default revision")
    return branch, revision


def _fetched_default_revision(
    source_root: Path, branch: str, advertised_revision: str
) -> str:
    """Fetch and validate the advertised remote default without consulting a main worktree."""
    advertised_revision = _oid(advertised_revision, "advertised remote default revision")
    remote_ref = f"refs/remotes/origin/{branch}"
    symbolic_ref = lifecycle.run(
        ["git", "symbolic-ref", "--quiet", remote_ref],
        cwd=source_root,
        check=False,
    )
    if symbolic_ref.returncode == 0:
        raise TaskStateRecoveryError("origin default ref must not be symbolic")
    if symbolic_ref.returncode != 1:
        raise TaskStateRecoveryError("cannot inspect origin default ref type")
    previous_ref = lifecycle.run(
        ["git", "show-ref", "--verify", "--hash", remote_ref],
        cwd=source_root,
        check=False,
    )
    if previous_ref.returncode == 0:
        previous = _oid(previous_ref.stdout.strip(), "previous origin default revision")
        resolved_previous = _oid(
            _git(source_root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"),
            "previous origin default commit",
        )
        if previous != resolved_previous:
            raise TaskStateRecoveryError("origin default ref is not a direct commit ref")
    elif previous_ref.returncode == 1:
        previous = None
    else:
        raise TaskStateRecoveryError("cannot inspect previous origin default ref")

    temporary_ref = f"refs/agent-core/recovery-default/{secrets.token_hex(16)}"
    collision = lifecycle.run(
        ["git", "show-ref", "--verify", "--quiet", temporary_ref],
        cwd=source_root,
        check=False,
    )
    if collision.returncode == 0:
        raise TaskStateRecoveryError("temporary recovery default ref already exists")
    if collision.returncode != 1:
        raise TaskStateRecoveryError("cannot validate temporary recovery default ref")
    temporary_symbolic = lifecycle.run(
        ["git", "symbolic-ref", "--quiet", temporary_ref],
        cwd=source_root,
        check=False,
    )
    if temporary_symbolic.returncode == 0:
        raise TaskStateRecoveryError("temporary recovery default ref must not be symbolic")
    if temporary_symbolic.returncode != 1:
        raise TaskStateRecoveryError("cannot inspect temporary recovery default ref type")

    temporary_revision: str | None = None
    try:
        lifecycle.network_git(
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:{temporary_ref}",
            cwd=source_root,
        )
        temporary_revision = _oid(
            _git(source_root, "rev-parse", "--verify", f"{temporary_ref}^{{commit}}"),
            "fetched default revision",
        )
        if temporary_revision != advertised_revision:
            raise TaskStateRecoveryError(
                "remote default moved between advertisement and fetch"
            )
        if previous is not None and lifecycle.run(
            [
                "git",
                "--no-replace-objects",
                "merge-base",
                "--is-ancestor",
                previous,
                temporary_revision,
            ],
            cwd=source_root,
            check=False,
        ).returncode != 0:
            raise TaskStateRecoveryError("origin default branch moved non-fast-forward")

        expected_previous = previous or "0" * len(temporary_revision)
        try:
            lifecycle.run(
                [
                    "git",
                    "update-ref",
                    "--no-deref",
                    remote_ref,
                    temporary_revision,
                    expected_previous,
                ],
                cwd=source_root,
            )
        except lifecycle.LifecycleError as exc:
            raise TaskStateRecoveryError("origin default ref moved during recovery fetch") from exc
        remote = _oid(
            _git(source_root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"),
            "origin default revision",
        )
        symbolic_after = lifecycle.run(
            ["git", "symbolic-ref", "--quiet", remote_ref],
            cwd=source_root,
            check=False,
        )
        if symbolic_after.returncode == 0:
            raise TaskStateRecoveryError("origin default ref became symbolic during fetch")
        if symbolic_after.returncode != 1:
            raise TaskStateRecoveryError("cannot verify origin default ref type after fetch")
        if remote != temporary_revision:
            raise TaskStateRecoveryError("origin default ref moved during recovery planning")
        return remote
    except TaskStateRecoveryError:
        raise
    except lifecycle.LifecycleError as exc:
        raise TaskStateRecoveryError(
            f"cannot obtain trusted origin/{branch} revision"
        ) from exc
    finally:
        cleanup_ref = temporary_revision
        if cleanup_ref is None:
            temporary_result = lifecycle.run(
                ["git", "show-ref", "--verify", "--hash", temporary_ref],
                cwd=source_root,
                check=False,
            )
            if temporary_result.returncode == 0:
                cleanup_ref = _oid(
                    temporary_result.stdout.strip(),
                    "temporary fetched default revision",
                )
            elif temporary_result.returncode != 1:
                raise TaskStateRecoveryError(
                    "cannot inspect temporary fetched default ref after failure"
                )
        if cleanup_ref is not None:
            try:
                lifecycle.run(
                    ["git", "update-ref", "--no-deref", "-d", temporary_ref, cleanup_ref],
                    cwd=source_root,
                )
            except lifecycle.LifecycleError as exc:
                raise TaskStateRecoveryError(
                    "cannot remove temporary fetched default ref"
                ) from exc


def _require_source_and_default(source_root: Path, implementation_revision: str) -> dict:
    """Bind recovery to the exact clean implementation and fetched remote default."""
    source_root = source_root.resolve()
    if lifecycle.repo_root(source_root) != source_root:
        raise TaskStateRecoveryError("source root is not an exact Git worktree root")
    source_record = lifecycle.current_worktree(source_root)
    if source_record.path != source_root:
        raise TaskStateRecoveryError("source root is not an exact registered implementation worktree")
    head = _oid(_git(source_root, "rev-parse", "--verify", "HEAD^{commit}"), "source HEAD")
    if head != implementation_revision:
        raise TaskStateRecoveryError("source HEAD does not match the implementation revision")
    if _git(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskStateRecoveryError("source worktree must be clean")
    default, advertised_revision = _remote_default_branch_and_revision(source_root)
    if default != "main":
        raise TaskStateRecoveryError(f"default branch is not main: {default}")
    default_revision = _fetched_default_revision(
        source_root, default, advertised_revision
    )
    if (
        _git(source_root, "rev-parse", "--verify", "HEAD^{commit}") != head
        or _git(source_root, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise TaskStateRecoveryError("source worktree changed while fetching current default")
    return {
        "branch": default,
        "revision": default_revision,
        "implementation_revision": implementation_revision,
        "worktree": source_root,
    }


def _require_ignored_state(target: Path) -> None:
    for name in (*STATE_FILES, "work-units.lock"):
        result = lifecycle.run(
            ["git", "check-ignore", "-q", "--", f"{TASK_STATE_DIRECTORY}/{name}"],
            cwd=target,
            check=False,
        )
        if result.returncode != 0:
            raise TaskStateRecoveryError(f".task-state/{name} is not ignored")
    tracked = _git(target, "ls-files", "--", TASK_STATE_DIRECTORY, check=False)
    if tracked:
        raise TaskStateRecoveryError(".task-state contains tracked paths")


def _state_entries(target: Path) -> tuple[Path, set[str]]:
    directory = target / TASK_STATE_DIRECTORY
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return directory, set()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskStateRecoveryError(".task-state is not a real directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise TaskStateRecoveryError(".task-state directory has unsafe ownership or mode")
    entries: set[str] = set()
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise TaskStateRecoveryError("cannot inspect .task-state") from exc
    for child in children:
        child_metadata = child.lstat()
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISREG(child_metadata.st_mode):
            raise TaskStateRecoveryError(f"unsafe .task-state entry: {child.name}")
        if child_metadata.st_uid != os.geteuid() or stat.S_IMODE(child_metadata.st_mode) & 0o022:
            raise TaskStateRecoveryError(f"unsafe .task-state entry ownership or mode: {child.name}")
        entries.add(child.name)
    unknown = entries - ALLOWED_STATE_FILES
    if unknown:
        raise TaskStateRecoveryError(
            "unsupported partial .task-state evidence: " + ", ".join(sorted(unknown))
        )
    return directory, entries


def _read_receipt(path: Path) -> tuple[bytes, dict] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TaskStateRecoveryError("recovery receipt is not a regular file")
    try:
        content = private_state.read_bytes(path, "lost Task State recovery receipt")
        value = json.loads(content.decode("utf-8"))
        private_state._validate_legacy_content(path, content)
    except (UnicodeError, json.JSONDecodeError, private_state.GitPrivateStateError) as exc:
        raise TaskStateRecoveryError("recovery receipt is invalid") from exc
    if not isinstance(value, dict):
        raise TaskStateRecoveryError("recovery receipt is invalid")
    return content, value


def _pull_request(target: Path, repository: str, branch: str, requested: int) -> dict:
    try:
        value = lifecycle.pull_requests_for_branch(target, branch, repository)
    except lifecycle.LifecycleError as exc:
        raise TaskStateRecoveryError(str(exc)) from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise TaskStateRecoveryError("exactly one pull request for the Task branch is required")
    pr = value[0]
    if pr.get("number") != requested:
        raise TaskStateRecoveryError("requested pull request is not the unique Task-branch pull request")
    return pr


def _prove_base(target: Path, target_head: str, current_main: str, pr: dict) -> str:
    current_main = _oid(current_main, "synchronized default branch")
    parents = _git(target, "rev-list", "--parents", "-n", "1", target_head).split()
    if len(parents) != 2 or parents[0] != target_head:
        raise TaskStateRecoveryError("Task HEAD must have exactly one mechanically provable parent")
    parent = _oid(parents[1], "Task original base")
    bases = [item for item in _git(target, "merge-base", "--all", target_head, current_main).splitlines() if item]
    if bases != [parent]:
        raise TaskStateRecoveryError("Task original base is ambiguous or does not match its parent")
    if pr.get("baseRefName") != "main":
        raise TaskStateRecoveryError("pull request base branch is not main")
    _oid(pr.get("baseRefOid"), "pull request base revision")
    return parent


def _build_state(template: bytes, task: int, digest: str, branch: str, target: Path, base: str) -> bytes:
    try:
        text = template.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskStateRecoveryError("Task State template is not valid UTF-8") from exc
    replacements = {
        "@@TASK_ID@@": str(task),
        "@@BRANCH@@": branch,
        "@@WORKTREE@@": str(target),
        "@@BASE_BRANCH@@": "main",
        "@@BASE_REVISION@@": base,
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    if "@@" in text:
        raise TaskStateRecoveryError("Task State template contains unresolved placeholders")
    try:
        text = contract._canonical_state(text, task, digest)
    except Exception as exc:
        raise TaskStateRecoveryError(f"Task State schema is incompatible: {exc}") from exc
    text, count = re.subn(r"(?m)^- Status: initialized$", "- Status: implementing", text, count=1)
    if count != 1:
        raise TaskStateRecoveryError("Task State template does not contain initialized status")
    text, count = re.subn(
        r"(?m)^- Unverified: .*$",
        "- Unverified: ignored Task State was recovered; verification and review evidence require fresh validation",
        text,
        count=1,
    )
    if count != 1:
        raise TaskStateRecoveryError("Task State template does not contain an Unverified field")
    return text.encode("utf-8")


def _template_compatibility(source_root: Path, target: Path, target_head: str, base: str, revision: str) -> bytes:
    source_path = source_root / TEMPLATE_PATH
    current = _regular_bytes(source_path, "source Task State template")
    base_blob = _git_blob(target, base, TEMPLATE_PATH)
    target_blob = _git_blob(target, target_head, TEMPLATE_PATH)
    implementation_blob = _git_blob(source_root, revision, TEMPLATE_PATH)
    if not (current == base_blob == target_blob == implementation_blob):
        raise TaskStateRecoveryError("Task State template/schema is incompatible with the proven baseline")
    return current


def _plan(
    source_root: Path,
    target: Path,
    task: str,
    requested_pr: int,
    implementation_revision: str,
) -> dict:
    task_number = _number(task, "Task/Issue")
    implementation_revision = _oid(implementation_revision, "implementation revision")
    target = target.resolve()
    source_root = source_root.resolve()
    if source_root == target:
        raise TaskStateRecoveryError("source root and target Task worktree must be distinct")
    if lifecycle.repo_root(target) != target:
        raise TaskStateRecoveryError("target is not an exact Git worktree root")
    current = lifecycle.current_worktree(target)
    record = lifecycle.worktree_for_task(target, task)
    if current.path != target or record.path != target:
        raise TaskStateRecoveryError("target is not the exact registered non-default Task worktree")
    branch = record.branch
    if not lifecycle.branch_matches_task(branch, task) or branch is None:
        raise TaskStateRecoveryError("registered Task branch does not match the Issue")
    _require_ignored_state(target)
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskStateRecoveryError("target worktree must be clean")
    target_head = _oid(_git(target, "rev-parse", "--verify", "HEAD^{commit}"), "target HEAD")
    local_head = _oid(_git(target, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"), "local branch HEAD")
    if target_head != local_head or record.head != target_head:
        raise TaskStateRecoveryError("target HEAD, local branch, and registered worktree HEAD differ")
    tree = _oid(_git(target, "rev-parse", "--verify", "HEAD^{tree}"), "target tree")
    target_repository = contract.repository_identity(target)
    source_repository = contract.repository_identity(source_root)
    if target_repository.casefold() != source_repository.casefold():
        raise TaskStateRecoveryError("target and source repository identities differ")
    if private_state.common_git_dir(target).resolve() != private_state.common_git_dir(source_root).resolve():
        raise TaskStateRecoveryError("target and source do not share one Git common directory")
    source = _require_source_and_default(source_root, implementation_revision)
    pr = _pull_request(target, target_repository, branch, requested_pr)
    if (
        pr.get("state") != "OPEN"
        or pr.get("draft") is not True
        or pr.get("isCrossRepository") is not False
        or pr.get("headRefName") != branch
        or pr.get("headRefOid") != target_head
        or pr.get("baseRefName") != source["branch"]
        or not isinstance(pr.get("headRepository"), str)
        or pr["headRepository"].casefold() != target_repository.casefold()
        or not isinstance(pr.get("baseRepository"), str)
        or pr["baseRepository"].casefold() != target_repository.casefold()
    ):
        raise TaskStateRecoveryError("pull request is not the exact same-repository open Draft target")
    remote_head = lifecycle.remote_branch_head(record)
    if remote_head != target_head:
        raise TaskStateRecoveryError("remote Task branch does not match the exact target HEAD")
    base = _prove_base(target, target_head, source["revision"], pr)
    template = _template_compatibility(source_root, target, target_head, base, implementation_revision)
    identity, issue_payload = contract.fetch_issue(target, task, runner=_issue_runner)
    if identity.casefold() != target_repository.casefold():
        raise TaskStateRecoveryError("Issue repository identity differs from the target repository")
    payload = contract.authoritative_payload(issue_payload, task_number, target_repository)
    issue_digest = contract._digest(payload)
    snapshot = {
        "schema_version": 1,
        "issue": task_number,
        "repository": target_repository,
        "sha256": issue_digest,
        "payload": payload,
    }
    issue_bytes = (json.dumps(snapshot, sort_keys=True) + "\n").encode("utf-8")
    contract_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "issue": task_number,
                "repository": target_repository,
                "snapshot": contract.SNAPSHOT,
                "sha256": issue_digest,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    state_bytes = _build_state(template, task_number, issue_digest, branch, target, base)
    reconstructed = {
        "task.md": _sha256(state_bytes),
        "issue.json": _sha256(issue_bytes),
        "contract.json": _sha256(contract_bytes),
    }
    receipt = {
        "schema_version": 1,
        "kind": "lost-ignored-task-state",
        "repository": target_repository,
        "task_id": task,
        "worktree": str(target),
        "branch": branch,
        "head": target_head,
        "tree": tree,
        "base_branch": "main",
        "base_revision": base,
        "default_revision": source["revision"],
        "remote_branch_head": remote_head,
        "pr_number": requested_pr,
        "pr_state": "OPEN",
        "pr_draft": True,
        "pr_head_ref": branch,
        "pr_head_oid": target_head,
        "pr_base_ref": "main",
        "pr_base_oid": pr["baseRefOid"],
        "issue_sha256": issue_digest,
        "implementation_source": str(source_root.resolve()),
        "implementation_revision": implementation_revision,
        "reconstructed_file_sha256": reconstructed,
    }
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return {
        "task": task,
        "repository": target_repository,
        "branch": branch,
        "head": target_head,
        "tree": tree,
        "base": base,
        "default_revision": source["revision"],
        "remote_head": remote_head,
        "pr": pr,
        "issue_digest": issue_digest,
        "issue_bytes": issue_bytes,
        "contract_bytes": contract_bytes,
        "state_bytes": state_bytes,
        "receipt": receipt,
        "receipt_bytes": receipt_bytes,
        "reconstructed": reconstructed,
    }


def _require_fast_forward_observation(
    source_root: Path, previous: str, current: str, label: str
) -> None:
    previous = _oid(previous, f"previous {label}")
    current = _oid(current, f"current {label}")
    if previous == current:
        return
    result = lifecycle.run(
        ["git", "--no-replace-objects", "merge-base", "--is-ancestor", previous, current],
        cwd=source_root,
        check=False,
    )
    if result.returncode != 0:
        raise TaskStateRecoveryError(
            f"{label} is not a fast-forward of its prior observation"
        )


def _same_recovery_receipt(receipt: dict, plan: dict) -> None:
    """Allow live default/PR-base observations to advance, not receipt identity."""
    expected = plan["receipt"]
    if not isinstance(receipt, dict) or set(receipt) != set(expected):
        raise TaskStateRecoveryError("conflicting lost Task State recovery receipt exists")
    volatile_observations = {"default_revision", "pr_base_oid"}
    for name, value in expected.items():
        if name not in volatile_observations and receipt[name] != value:
            raise TaskStateRecoveryError(
                f"conflicting lost Task State recovery receipt field: {name}"
            )
    _oid(receipt["default_revision"], "recovery receipt default revision")
    _oid(receipt["pr_base_oid"], "recovery receipt PR base revision")
    _oid(plan["default_revision"], "current recovery default revision")
    _oid(plan["pr"]["baseRefOid"], "current PR base revision")
    source_root = Path(expected["implementation_source"])
    _require_fast_forward_observation(
        source_root,
        receipt["default_revision"],
        plan["default_revision"],
        "recovery default revision",
    )


def _same_pr_identity(before: dict, after: dict) -> None:
    stable_fields = (
        "number",
        "state",
        "draft",
        "headRefName",
        "headRefOid",
        "headRepository",
        "baseRefName",
        "baseRepository",
        "isCrossRepository",
    )
    if any(before.get(name) != after.get(name) for name in stable_fields):
        raise TaskStateRecoveryError("pull request identity changed before mutation")
    _oid(before.get("baseRefOid"), "previous PR base revision")
    _oid(after.get("baseRefOid"), "current PR base revision")


def _same_plan(before: dict, after: dict) -> None:
    for name in (
        "task", "repository", "branch", "head", "tree", "base", "remote_head",
        "issue_digest", "issue_bytes", "contract_bytes", "state_bytes", "reconstructed",
    ):
        if before[name] != after[name]:
            raise TaskStateRecoveryError(f"recovery authority changed before mutation: {name}")
    _same_pr_identity(before["pr"], after["pr"])
    _same_recovery_receipt(before["receipt"], after)


def _state_topology(target: Path, receipt_exists: bool) -> tuple[Path, set[str]]:
    directory, entries = _state_entries(target)
    if not receipt_exists and entries - {"work-units.lock"}:
        raise TaskStateRecoveryError("partial .task-state exists without a matching recovery receipt")
    return directory, entries


def _state_file_from_fd(directory_fd: int, name: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise TaskStateRecoveryError(f"unsafe .task-state entry: {name}")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise TaskStateRecoveryError(f"unsafe .task-state entry ownership or mode: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise TaskStateRecoveryError(f".task-state entry changed while reading: {name}")
        return b"".join(chunks)
    except TaskStateRecoveryError:
        raise
    except OSError as exc:
        raise TaskStateRecoveryError(f"cannot read .task-state entry: {name}") from exc
    finally:
        os.close(fd)


def _state_entries_from_fd(directory_fd: int) -> set[str]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise TaskStateRecoveryError("cannot inspect pinned .task-state directory") from exc
    entries: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            raise TaskStateRecoveryError(".task-state contains an invalid entry name")
        if name not in ALLOWED_STATE_FILES:
            raise TaskStateRecoveryError(f"unsupported partial .task-state evidence: {name}")
        if _state_file_from_fd(directory_fd, name) is None:
            raise TaskStateRecoveryError(f".task-state entry disappeared during inspection: {name}")
        entries.add(name)
    return entries


def _validate_existing_state(plan: dict, directory_fd: int, entries: set[str], receipt: dict) -> None:
    if entries - set(STATE_FILES):
        if entries - set(STATE_FILES) != {"work-units.lock"}:
            raise TaskStateRecoveryError("recovery encountered unsupported historical Task State evidence")
    for name in STATE_FILES:
        content = _state_file_from_fd(directory_fd, name)
        if content is None:
            continue
        expected_hash = receipt["reconstructed_file_sha256"][name]
        if _sha256(content) != expected_hash:
            raise TaskStateRecoveryError(f"conflicting reconstructed Task State file: {name}")
        expected = plan[{"task.md": "state_bytes", "issue.json": "issue_bytes", "contract.json": "contract_bytes"}[name]]
        if content != expected:
            raise TaskStateRecoveryError(f"reconstructed Task State changed: {name}")


def _publish_state_file(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short Task State write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    except FileExistsError:
        existing = _state_file_from_fd(directory_fd, name)
        if existing != content:
            raise TaskStateRecoveryError(f"conflicting reconstructed Task State file: {name}")
    except OSError as exc:
        raise TaskStateRecoveryError(f"cannot publish reconstructed Task State file: {name}") from exc


def _ensure_state_directory(target: Path) -> Path:
    directory = target / TASK_STATE_DIRECTORY
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskStateRecoveryError(".task-state changed into an unsafe object")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise TaskStateRecoveryError(".task-state directory has unsafe ownership or mode")
    return directory


def _publish_state(
    plan: dict, target: Path, receipt: dict, directory_fd: int
) -> None:
    directory = target / TASK_STATE_DIRECTORY
    try:
        pinned = os.fstat(directory_fd)
        current = directory.lstat()
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise TaskStateRecoveryError("Task State directory changed during recovery")
        entries = _state_entries_from_fd(directory_fd)
        _validate_existing_state(plan, directory_fd, entries, receipt)
        # The caller holds work-units.lock through publication and final resume
        # validation. Publish metadata before task.md so consumers never observe
        # a nominal Task without its Issue snapshot and contract metadata.
        _publish_state_file(directory_fd, "issue.json", plan["issue_bytes"])
        _publish_state_file(directory_fd, "contract.json", plan["contract_bytes"])
        _publish_state_file(directory_fd, "task.md", plan["state_bytes"])
        final_entries = _state_entries_from_fd(directory_fd)
        _validate_existing_state(plan, directory_fd, final_entries, receipt)
    except lifecycle.LifecycleError as exc:
        raise TaskStateRecoveryError(str(exc)) from exc


def recover_missing_task_state(
    source_root: Path,
    target: Path,
    task: str,
    requested_pr: int,
    implementation_revision: str,
) -> dict:
    """Recover only missing canonical Task authority; never mutate the Task/PR."""
    if (
        not isinstance(requested_pr, int)
        or isinstance(requested_pr, bool)
        or requested_pr < 1
    ):
        raise TaskStateRecoveryError("pull request number must be a positive integer")
    target = target.resolve()
    receipt_path = private_state.lost_ignored_task_state_receipt(target)
    existing_receipt = _read_receipt(receipt_path)
    receipt_was_present = existing_receipt is not None
    _state_topology(target, existing_receipt is not None)
    plan = _plan(source_root, target, task, requested_pr, implementation_revision)
    if existing_receipt is not None:
        _same_recovery_receipt(existing_receipt[1], plan)

    try:
        private_state._validate_canonical(private_state.topology(target))
        with private_state.mutation_lock(target, admin=True):
            private_state._validate_canonical(private_state.topology(target))
            _ensure_state_directory(target)
            # Normal Task commits use this same lock. Keep it held from the last
            # plan through receipt/State publication and resume validation so a
            # concurrent commit cannot strand or invalidate recovery evidence.
            with lifecycle.state_directory_lock(target) as directory_fd:
                latest_receipt = _read_receipt(receipt_path)
                latest_plan = _plan(
                    source_root, target, task, requested_pr, implementation_revision
                )
                _same_plan(plan, latest_plan)
                if existing_receipt is not None:
                    if (
                        latest_receipt is None
                        or latest_receipt[0] != existing_receipt[0]
                        or latest_receipt[1] != existing_receipt[1]
                    ):
                        raise TaskStateRecoveryError("recovery receipt changed before mutation")
                    receipt_to_publish = latest_receipt[1]
                elif latest_receipt is not None:
                    _same_recovery_receipt(latest_receipt[1], latest_plan)
                    receipt_was_present = True
                    receipt_to_publish = latest_receipt[1]
                else:
                    receipt_to_publish = latest_plan["receipt"]
                    private_state.exclusive_write_bytes(
                        receipt_path, latest_plan["receipt_bytes"], _lock_held=True
                    )

                # A retry may observe a newer fast-forward default/PR-base snapshot.
                # Refresh only those observation fields after stable authority identity
                # has been re-proven under both locks.
                if receipt_to_publish != latest_plan["receipt"]:
                    private_state.write_bytes(
                        receipt_path,
                        latest_plan["receipt_bytes"],
                        _lock_held=True,
                    )
                    receipt_to_publish = latest_plan["receipt"]
                _publish_state(
                    latest_plan, target, receipt_to_publish, directory_fd
                )
                after = _plan(
                    source_root, target, task, requested_pr, implementation_revision
                )
                _same_plan(latest_plan, after)
                if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
                    raise TaskStateRecoveryError(
                        "recovery changed tracked or unignored target content"
                    )
                try:
                    resume = contract.check_resume_contract(
                        target,
                        task,
                        runner=_issue_runner,
                        directory_fd=directory_fd,
                        expected_base_branch=latest_plan["receipt"]["base_branch"],
                        expected_base_revision=latest_plan["receipt"]["base_revision"],
                    )
                except Exception as exc:
                    raise TaskStateRecoveryError(
                        f"recovered Task State failed resume validation: {exc}"
                    ) from exc
                if resume.get("mode") != "resume" or resume.get("taskStatus") != "implementing":
                    raise TaskStateRecoveryError(
                        "recovered Task State is not implementing/resumable"
                    )
                plan = latest_plan
                return {
                    "status": "TASK_STATE_ALREADY_RECOVERED" if receipt_was_present else "TASK_STATE_RECOVERED",
                    "task": task,
                    "repository": plan["repository"],
                    "branch": plan["branch"],
                    "worktree": str(target),
                    "head": plan["head"],
                    "baseRevision": plan["base"],
                    "pullRequest": requested_pr,
                    "taskStatus": "implementing",
                    "receipt": str(receipt_path),
                    "resume": resume,
                    "githubMutations": 0,
                }
    except TaskStateRecoveryError:
        raise
    except (private_state.GitPrivateStateError, lifecycle.LifecycleError, OSError) as exc:
        raise TaskStateRecoveryError(str(exc)) from exc
