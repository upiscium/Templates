"""Fail-closed recovery of lost ignored Task State for an exact Task/PR."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
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
    return lifecycle.git(*args, cwd=root, check=check)


def _gh_json(root: Path, *args: str) -> object:
    result = lifecycle.gh(*args, cwd=root, check=False)
    if result.returncode:
        raise TaskStateRecoveryError(
            result.stderr.strip() or result.stdout.strip() or f"GitHub CLI exit {result.returncode}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TaskStateRecoveryError("GitHub response is not valid JSON") from exc


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
            ["git", "show", f"{revision}:{relative}"], cwd=root, check=True
        )
        return result.stdout.encode("utf-8")
    except UnicodeError as exc:
        raise TaskStateRecoveryError(f"Git blob is not valid UTF-8: {relative}") from exc


def _require_source_and_main(source_root: Path, target: Path, implementation_revision: str) -> dict:
    source_root = source_root.resolve()
    if lifecycle.repo_root(source_root) != source_root:
        raise TaskStateRecoveryError("source root is not an exact Git worktree root")
    default = lifecycle.default_branch(target)
    if default != "main":
        raise TaskStateRecoveryError(f"default branch is not main: {default}")
    main = lifecycle.main_worktree(target)
    source_record = lifecycle.current_worktree(source_root)
    if source_record.path != source_root or source_root == target:
        raise TaskStateRecoveryError("source root is not an exact registered implementation worktree")
    if main.path == target or main.branch != default:
        raise TaskStateRecoveryError("registered default-branch worktree is ambiguous")
    head = _oid(_git(source_root, "rev-parse", "--verify", "HEAD^{commit}"), "source HEAD")
    if head != implementation_revision:
        raise TaskStateRecoveryError("source HEAD does not match the implementation revision")
    if _git(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskStateRecoveryError("source worktree must be clean")
    main_head = _oid(_git(main.path, "rev-parse", "--verify", "HEAD^{commit}"), "current main HEAD")
    main_remote = _git(main.path, "rev-parse", "--verify", "refs/remotes/origin/main", check=False)
    if main_remote != main_head:
        raise TaskStateRecoveryError("registered main is not synchronized with origin/main")
    if _git(main.path, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskStateRecoveryError("registered main worktree must be clean")
    return {
        "branch": default,
        "revision": main_head,
        "implementation_revision": implementation_revision,
        "worktree": source_root,
        "main_worktree": main.path,
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
    fields = (
        "number,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,"
        "isCrossRepository,headRepository"
    )
    value = _gh_json(
        target,
        "pr",
        "list",
        "--repo",
        repository,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        "100",
        "--json",
        fields,
    )
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise TaskStateRecoveryError("exactly one pull request for the Task branch is required")
    pr = value[0]
    if pr.get("number") != requested:
        raise TaskStateRecoveryError("requested pull request is not the unique Task-branch pull request")
    return pr


def _prove_base(target: Path, target_head: str, current_main: str, pr: dict) -> str:
    parents = _git(target, "rev-list", "--parents", "-n", "1", target_head).split()
    if len(parents) != 2 or parents[0] != target_head:
        raise TaskStateRecoveryError("Task HEAD must have exactly one mechanically provable parent")
    parent = _oid(parents[1], "Task original base")
    bases = [item for item in _git(target, "merge-base", "--all", target_head, current_main).splitlines() if item]
    if bases != [parent]:
        raise TaskStateRecoveryError("Task original base is ambiguous or does not match its parent")
    if pr.get("baseRefOid") != parent:
        raise TaskStateRecoveryError("pull request base revision does not match the proven original base")
    if pr.get("baseRefName") != "main":
        raise TaskStateRecoveryError("pull request base branch is not main")
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
    if lifecycle.repo_root(target) != target:
        raise TaskStateRecoveryError("target is not an exact Git worktree root")
    current = lifecycle.current_worktree(target)
    main = lifecycle.main_worktree(target)
    record = lifecycle.worktree_for_task(target, task)
    if current.path != target or record.path != target or current.path == main.path:
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
    source = _require_source_and_main(source_root, target, implementation_revision)
    target_repository = contract.repository_identity(target)
    source_repository = contract.repository_identity(source_root)
    if target_repository.casefold() != source_repository.casefold():
        raise TaskStateRecoveryError("target and source repository identities differ")
    if private_state.common_git_dir(target).resolve() != private_state.common_git_dir(source_root).resolve():
        raise TaskStateRecoveryError("target and source do not share one Git common directory")
    pr = _pull_request(target, target_repository, branch, requested_pr)
    if (
        pr.get("state") != "OPEN"
        or pr.get("isDraft") is not True
        or pr.get("isCrossRepository") is not False
        or pr.get("headRefName") != branch
        or pr.get("headRefOid") != target_head
        or pr.get("baseRefName") != source["branch"]
        or not isinstance(pr.get("headRepository"), dict)
        or pr["headRepository"].get("nameWithOwner") != target_repository
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
        "pr_base_oid": base,
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


def _same_plan(before: dict, after: dict) -> None:
    for name in (
        "task", "repository", "branch", "head", "tree", "base", "default_revision",
        "remote_head", "issue_digest", "issue_bytes", "contract_bytes", "state_bytes",
        "receipt_bytes",
    ):
        if before[name] != after[name]:
            raise TaskStateRecoveryError(f"recovery authority changed before mutation: {name}")
    if before["pr"] != after["pr"]:
        raise TaskStateRecoveryError("pull request identity changed before mutation")


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


def _publish_state(plan: dict, target: Path, receipt: dict) -> None:
    directory = _ensure_state_directory(target)
    try:
        with lifecycle.state_directory_lock(target) as directory_fd:
            pinned = os.fstat(directory_fd)
            current = directory.lstat()
            if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
                raise TaskStateRecoveryError("Task State directory changed during recovery")
            entries = _state_entries_from_fd(directory_fd)
            _validate_existing_state(plan, directory_fd, entries, receipt)
            # state_directory_lock creates and pins work-units.lock. Publish
            # metadata before task.md so consumers never observe a nominal
            # Task without its Issue snapshot and contract metadata.
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
    if isinstance(requested_pr, bool):
        raise TaskStateRecoveryError("pull request number must be positive")
    requested_pr = int(requested_pr)
    if requested_pr < 1:
        raise TaskStateRecoveryError("pull request number must be positive")
    target = target.resolve()
    receipt_path = private_state.lost_ignored_task_state_receipt(target)
    existing_receipt = _read_receipt(receipt_path)
    _state_topology(target, existing_receipt is not None)
    plan = _plan(source_root, target, task, requested_pr, implementation_revision)
    if existing_receipt is not None:
        receipt_bytes, receipt_value = existing_receipt
        if receipt_bytes != plan["receipt_bytes"] or receipt_value != plan["receipt"]:
            raise TaskStateRecoveryError("conflicting lost Task State recovery receipt exists")

    try:
        private_state._validate_canonical(private_state.topology(target))
        with private_state.mutation_lock(target, admin=True):
            private_state._validate_canonical(private_state.topology(target))
            latest_receipt = _read_receipt(receipt_path)
            latest_plan = _plan(source_root, target, task, requested_pr, implementation_revision)
            _same_plan(plan, latest_plan)
            if latest_receipt is not None:
                if latest_receipt[0] != plan["receipt_bytes"] or latest_receipt[1] != plan["receipt"]:
                    raise TaskStateRecoveryError("recovery receipt changed before mutation")
            private_state.exclusive_write_bytes(
                receipt_path, plan["receipt_bytes"], _lock_held=True
            )
            _publish_state(plan, target, plan["receipt"])
    except (private_state.GitPrivateStateError, OSError) as exc:
        raise TaskStateRecoveryError(str(exc)) from exc

    after = _plan(source_root, target, task, requested_pr, implementation_revision)
    _same_plan(plan, after)
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TaskStateRecoveryError("recovery changed tracked or unignored target content")
    try:
        resume = contract.check_resume_contract(target, task, runner=_issue_runner)
    except Exception as exc:
        raise TaskStateRecoveryError(f"recovered Task State failed resume validation: {exc}") from exc
    if resume.get("mode") != "resume" or resume.get("taskStatus") != "implementing":
        raise TaskStateRecoveryError("recovered Task State is not implementing/resumable")
    return {
        "status": "TASK_STATE_ALREADY_RECOVERED" if existing_receipt is not None else "TASK_STATE_RECOVERED",
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
