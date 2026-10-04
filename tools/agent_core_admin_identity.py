"""Read-only target identity and cutover preflight for the Admin engine."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from collections.abc import Callable
from typing import Any


class AdminIdentityError(RuntimeError):
    """A fail-closed Admin target-identity error."""


@dataclass(frozen=True, slots=True)
class TargetFacts:
    root: Path
    git_dir: Path
    common_dir: Path
    branch: str
    head: str
    repository: str


_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z", re.ASCII)
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9-]+/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+\Z", re.ASCII)
_ORIGIN_PATTERNS = (
    re.compile(
        r"git@github\.com:([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?\Z",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"ssh://git@github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?\Z",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z",
        re.IGNORECASE | re.ASCII,
    ),
)
def _subprocess_environment() -> dict[str, str]:
    """Discard caller-supplied Git overrides and disable ambient Git config."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    # These are fixed safeguards, not values inherited from the caller.
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=_subprocess_environment(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise AdminIdentityError(f"cannot run {argv[0]} for Admin identity inspection") from exc


def _git_result(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=",
            "-c",
            "safe.directory=*",
            "--no-pager",
            *args,
        ],
        cwd,
    )


def _git(args: list[str], cwd: Path, *, check: bool = True) -> str:
    result = _git_result(args, cwd)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AdminIdentityError(f"Git identity query failed: {detail}")
    return result.stdout


def _canonical_directory(path: Path, *, label: str, compare_parent_device: bool) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or resolved != path
            or os.path.ismount(path)
        ):
            raise AdminIdentityError(f"{label} is linked, mounted, or not a canonical directory")
        if compare_parent_device:
            parent = path.parent
            parent_metadata = parent.stat()
            if metadata.st_dev != parent_metadata.st_dev:
                raise AdminIdentityError(f"{label} is mounted on a different filesystem")
    except AdminIdentityError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdminIdentityError(f"{label} is unavailable or not canonical") from exc
    return path


def _blocked_worktree_registration(reason: str) -> AdminIdentityError:
    return AdminIdentityError(
        f"linked worktree Gitfile/registration is ambiguous; BLOCKED: {reason}"
    )


def _read_regular_file(path: Path, *, label: str) -> str:
    try:
        metadata = path.lstat()
        parent_metadata = path.parent.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or path.resolve(strict=True) != path
            or os.path.ismount(path)
            or metadata.st_dev != parent_metadata.st_dev
        ):
            raise _blocked_worktree_registration(f"{label} is linked, mounted, or not regular")
        return path.read_text(encoding="utf-8")
    except AdminIdentityError:
        raise
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        raise _blocked_worktree_registration(f"{label} is unavailable or malformed") from exc


def _parse_gitfile(contents: str) -> Path:
    if (
        not contents.endswith("\n")
        or contents.count("\n") != 1
        or contents.endswith("\r\n")
        or not contents.startswith("gitdir: ")
    ):
        raise _blocked_worktree_registration("the .git Gitfile must contain one exact gitdir path")
    git_dir_text = contents[len("gitdir: ") : -1]
    try:
        git_dir = Path(git_dir_text)
    except (TypeError, ValueError) as exc:
        raise _blocked_worktree_registration("the .git Gitfile path is invalid") from exc
    if (
        not git_dir_text
        or not git_dir.is_absolute()
        or str(git_dir) != git_dir_text
        or any(character in git_dir_text for character in ("\n", "\r", "\0"))
    ):
        raise _blocked_worktree_registration("the .git Gitfile path is not canonical and absolute")
    return git_dir


def _linked_worktree_registration(gitfile: Path) -> tuple[Path, Path]:
    """Validate the on-disk reciprocal registration before asking Git to follow it."""
    git_dir = _parse_gitfile(_read_regular_file(gitfile, label=".git Gitfile"))
    if git_dir.name in {"", ".", ".."} or git_dir.parent.name != "worktrees":
        raise _blocked_worktree_registration("the Gitfile does not name a common-dir worktrees entry")

    common_dir = git_dir.parent.parent
    worktrees_dir = common_dir / "worktrees"
    try:
        _canonical_directory(common_dir, label="common Git directory", compare_parent_device=True)
        _canonical_directory(
            worktrees_dir,
            label="worktrees registration directory",
            compare_parent_device=True,
        )
        _canonical_directory(
            git_dir, label="linked Git directory", compare_parent_device=True
        )
    except AdminIdentityError as exc:
        raise _blocked_worktree_registration(str(exc)) from exc

    reciprocal = _read_regular_file(git_dir / "gitdir", label="reciprocal gitdir file")
    expected_gitfile = f"{gitfile}\n"
    if reciprocal != expected_gitfile:
        raise _blocked_worktree_registration(
            "the reciprocal gitdir file does not point to this worktree"
        )

    commondir = _read_regular_file(git_dir / "commondir", label="commondir file")
    expected_common_relative = os.path.relpath(common_dir, git_dir)
    if commondir != f"{expected_common_relative}\n":
        raise _blocked_worktree_registration(
            "the commondir file does not name the exact common directory"
        )
    return git_dir, common_dir


def _validate_expected_values(
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
) -> None:
    if not isinstance(expected_repository, str) or not _REPOSITORY_RE.fullmatch(
        expected_repository
    ):
        raise AdminIdentityError("expected GitHub repository must be exact owner/repository")
    if not isinstance(expected_branch, str) or not expected_branch:
        raise AdminIdentityError("expected branch is invalid")
    if "\0" in expected_branch or len(expected_branch) > 1024:
        raise AdminIdentityError("expected branch is invalid")
    if not isinstance(expected_head, str) or not _OID_RE.fullmatch(expected_head):
        raise AdminIdentityError("expected HEAD must be a full 40- or 64-character object ID")


def _repository_from_origin(origin: str) -> str:
    for pattern in _ORIGIN_PATTERNS:
        match = pattern.fullmatch(origin)
        if match:
            repository = f"{match.group(1)}/{match.group(2)}"
            if _REPOSITORY_RE.fullmatch(repository):
                return repository
    raise AdminIdentityError("origin must be a canonical GitHub HTTPS or SSH repository URL")


def _origin_repository(root: Path) -> str:
    result = _git_result(
        ["config", "--local", "--no-includes", "--null", "--get-all", "remote.origin.url"],
        root,
    )
    if result.returncode:
        if result.returncode == 1 and not result.stdout and not result.stderr.strip():
            raise AdminIdentityError("canonical GitHub origin is missing")
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AdminIdentityError(f"cannot read canonical GitHub origin: {detail}")
    origins = result.stdout.split("\0")
    if origins and origins[-1] == "":
        origins.pop()
    if len(origins) != 1:
        raise AdminIdentityError("origin must contain exactly one canonical repository URL")
    return _repository_from_origin(origins[0])


def observe_target(
    root: Path,
    *,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
) -> TargetFacts:
    """Observe one canonical, named-branch Git checkout without modifying it."""
    _validate_expected_values(expected_repository, expected_branch, expected_head)
    if not isinstance(root, Path):
        raise AdminIdentityError("target root must be a pathlib Path")
    try:
        absolute_root = Path(os.path.abspath(root))
        canonical_root = absolute_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdminIdentityError("target root is unavailable") from exc
    if absolute_root != canonical_root:
        raise AdminIdentityError("target root must not traverse a symlink")
    _canonical_directory(canonical_root, label="target root", compare_parent_device=True)
    if any(character in str(canonical_root) for character in ("\n", "\r", "\0")):
        raise AdminIdentityError("target root contains unsupported path characters")

    git_entry = canonical_root / ".git"
    try:
        git_entry_metadata = git_entry.lstat()
    except OSError as exc:
        raise AdminIdentityError("target root does not contain a canonical .git entry") from exc
    linked_gitfile = stat.S_ISREG(git_entry_metadata.st_mode)
    if stat.S_ISLNK(git_entry_metadata.st_mode):
        raise _blocked_worktree_registration("the target .git entry must not be a symlink")
    if linked_gitfile:
        registered_git_dir, registered_common_dir = _linked_worktree_registration(git_entry)
    elif stat.S_ISDIR(git_entry_metadata.st_mode):
        registered_git_dir = git_entry
        registered_common_dir = git_entry
    else:
        raise _blocked_worktree_registration(
            "the target .git entry is not a directory or regular Gitfile"
        )

    check_branch = _git_result(
        ["check-ref-format", f"refs/heads/{expected_branch}"], canonical_root
    )
    if check_branch.returncode:
        raise AdminIdentityError("expected branch is not a valid Git branch name")

    top_text = _git(["rev-parse", "--show-toplevel"], canonical_root).strip()
    if Path(top_text) != canonical_root:
        if linked_gitfile:
            raise _blocked_worktree_registration("Git did not identify the exact Gitfile worktree root")
        raise AdminIdentityError("target must be the exact Git worktree root")
    if _git(["rev-parse", "--is-inside-work-tree"], canonical_root).strip() != "true":
        raise AdminIdentityError("target is not a Git worktree")
    if _git(["rev-parse", "--is-bare-repository"], canonical_root).strip() != "false":
        raise AdminIdentityError("target must not be a bare Git repository")

    git_dir_text = _git(["rev-parse", "--absolute-git-dir"], canonical_root).strip()
    common_dir_text = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], canonical_root
    ).strip()
    git_dir = Path(git_dir_text)
    common_dir = Path(common_dir_text)
    if (
        git_dir != registered_git_dir
        or git_dir_text != str(registered_git_dir)
        or common_dir != registered_common_dir
        or common_dir_text != str(registered_common_dir)
    ):
        if linked_gitfile:
            raise _blocked_worktree_registration(
                "Git's exact git-dir/common-dir queries disagree with the reciprocal registration"
            )
        raise AdminIdentityError("target must use its canonical in-root, non-linked Git directory")
    if not linked_gitfile:
        _canonical_directory(git_dir, label="Git directory", compare_parent_device=True)

    branch_result = _git_result(["symbolic-ref", "--quiet", "HEAD"], canonical_root)
    if branch_result.returncode:
        if (
            branch_result.returncode == 1
            and not branch_result.stdout
            and not branch_result.stderr.strip()
        ):
            raise AdminIdentityError("target HEAD is detached or does not name a local branch")
        detail = (
            branch_result.stderr.strip()
            or branch_result.stdout.strip()
            or f"exit {branch_result.returncode}"
        )
        raise AdminIdentityError(f"Git identity query failed: {detail}")
    branch_ref = branch_result.stdout.strip()
    if not branch_ref.startswith("refs/heads/"):
        raise AdminIdentityError("target HEAD is detached or does not name a local branch")
    branch = branch_ref.removeprefix("refs/heads/")
    if branch != expected_branch:
        raise AdminIdentityError("target branch does not match the expected branch")

    head = _git(["rev-parse", "--verify", "HEAD^{commit}"], canonical_root).strip()
    if not _OID_RE.fullmatch(head):
        raise AdminIdentityError("target HEAD is not a full 40- or 64-character commit ID")
    head = head.lower()
    if len(head) != len(expected_head) or head != expected_head.lower():
        raise AdminIdentityError("target HEAD does not match the expected commit ID")

    repository = _origin_repository(canonical_root)
    if repository != expected_repository:
        raise AdminIdentityError("target origin does not match the exact expected repository")

    return TargetFacts(
        root=canonical_root,
        git_dir=git_dir,
        common_dir=common_dir,
        branch=branch,
        head=head,
        repository=repository,
    )


def _gh_api(root: Path, args: list[str]) -> Any:
    if not args or args[0] != "api" or len(args) < 2:
        raise AdminIdentityError("GitHub identity runner only accepts gh api reads")
    endpoint = args[1]
    match = re.fullmatch(
        r"repos/([^/]+)/([^/]+)/(?:issues|pulls)/[1-9][0-9]*",
        endpoint,
        re.ASCII,
    )
    if (
        len(args) != 2
        or match is None
        or not _REPOSITORY_RE.fullmatch(f"{match.group(1)}/{match.group(2)}")
    ):
        raise AdminIdentityError("GitHub identity runner only accepts exact repository API reads")
    result = _run(["gh", "api", "--hostname", "github.com", *args[1:]], root)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AdminIdentityError(f"GitHub identity query failed: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdminIdentityError("GitHub API returned invalid JSON") from exc


def _positive_number(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdminIdentityError(f"{label} must be a positive integer")
    return value


def _issue_identity(root: Path, repository: str, issue_number: int) -> dict[str, Any]:
    value = _gh_api(root, ["api", f"repos/{repository}/issues/{issue_number}"])
    if not isinstance(value, dict):
        raise AdminIdentityError("GitHub returned an invalid Issue response")
    if (
        isinstance(value.get("number"), bool)
        or value.get("number") != issue_number
        or value.get("state") != "open"
        or value.get("repository_url") != f"https://api.github.com/repos/{repository}"
        or "pull_request" in value
    ):
        raise AdminIdentityError("the exact Issue is missing, closed, a PR, or in another repository")
    return {"number": issue_number, "repository": repository, "state": "open"}


def _pull_request_identity(
    root: Path,
    repository: str,
    pr_number: int,
    branch: str,
    head: str,
    expected_base: str,
) -> dict[str, Any]:
    value = _gh_api(root, ["api", f"repos/{repository}/pulls/{pr_number}"])
    if not isinstance(value, dict):
        raise AdminIdentityError("GitHub returned an invalid pull request response")
    if isinstance(value.get("number"), bool) or value.get("number") != pr_number:
        raise AdminIdentityError("GitHub returned a different pull request number")
    base = value.get("base")
    pr_head = value.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = pr_head.get("repo") if isinstance(pr_head, dict) else None
    base_repository = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    head_repository = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    if (
        value.get("state") != "open"
        or value.get("draft") is not True
        or base_repository != repository
        or not isinstance(base, dict)
        or base.get("ref") != expected_base
        or head_repository != repository
        or not isinstance(pr_head, dict)
        or pr_head.get("ref") != branch
        or pr_head.get("sha") != head
    ):
        raise AdminIdentityError(
            "the exact pull request must be open, draft, same-repository, and bound to the expected base and target branch and HEAD"
        )
    return {
        "number": pr_number,
        "repository": repository,
        "state": "open",
        "draft": True,
        "baseRepository": repository,
        "baseBranch": expected_base,
        "headRepository": repository,
        "headBranch": branch,
        "head": head,
    }


def _remote_snapshot(
    facts: TargetFacts, issue: int, pr: int, expected_base: str
) -> dict[str, Any]:
    issue_identity = _issue_identity(facts.root, facts.repository, issue)
    pr_identity = _pull_request_identity(
        facts.root, facts.repository, pr, facts.branch, facts.head, expected_base
    )
    return {
        "issue": issue_identity,
        "pullRequest": pr_identity,
        "association": {
            "kind": "external-task-binding",
            "issue": issue,
            "pullRequest": pr,
            "repository": facts.repository,
        },
    }


def _target_snapshot(facts: TargetFacts) -> dict[str, str]:
    return {
        "root": str(facts.root),
        "gitDir": str(facts.git_dir),
        "commonDir": str(facts.common_dir),
        "branch": facts.branch,
        "head": facts.head,
        "repository": facts.repository,
    }


def _reobserve_facts(facts: TargetFacts) -> TargetFacts:
    if type(facts) is not TargetFacts:
        raise AdminIdentityError("target facts must be an observed TargetFacts value")
    current = observe_target(
        facts.root,
        expected_repository=facts.repository,
        expected_branch=facts.branch,
        expected_head=facts.head,
    )
    if current != facts:
        raise AdminIdentityError("target identity changed since it was observed")
    return current


def verify_cutover_binding(
    facts: TargetFacts,
    *,
    issue: int,
    pr: int,
    expected_base: str,
    task_binding: Callable[[TargetFacts, int, int], bool] | None,
) -> dict[str, Any]:
    """Require stable local/GitHub identities and an authoritative caller task binding."""
    issue = _positive_number(issue, "Issue number")
    pr = _positive_number(pr, "pull request number")
    if not isinstance(expected_base, str) or not expected_base or "\0" in expected_base:
        raise AdminIdentityError("expected base branch is invalid")
    if task_binding is None:
        raise AdminIdentityError(
            "active cutover requires the #194 task_binding callback; Issue/PR linkage is not inferred"
        )
    if not callable(task_binding):
        raise AdminIdentityError("task_binding must be callable")

    current = _reobserve_facts(facts)
    first = _remote_snapshot(current, issue, pr, expected_base)
    try:
        binding_matches = task_binding(current, issue, pr)
    except Exception as exc:
        raise AdminIdentityError("task_binding failed closed") from exc
    if binding_matches is not True:
        raise AdminIdentityError("active Task is not bound to this Issue and PR")

    final = _remote_snapshot(current, issue, pr, expected_base)
    _reobserve_facts(current)
    if final != first:
        raise AdminIdentityError("Issue/PR identity changed during cutover preflight")
    return {
        "target": _target_snapshot(current),
        **final,
    }
