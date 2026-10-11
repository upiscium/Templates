#!/usr/bin/env python3
"""Guarded collaboration API for Templates source-development worktrees."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any

REPO = "upiscium/Templates"
DEFAULT = "main"
REMOTE = "origin"
# Only the Nix-installed copy receives a reviewed revision during its build.
# An unsubstituted source-tree payload must never authorize publication.
TRUSTED_SOURCE_BASE = "@TRUSTED_SOURCE_BASE@"
TRUSTED_GIT = "@TRUSTED_GIT@"
TRUSTED_GH = "@TRUSTED_GH@"
TRUSTED_PATH = "@TRUSTED_PATH@"
# Review-sensitive next-version source paths; not an allow/deny list.
# This code is installed separately, so these changed paths are only data.
SOURCE_AUTHORITY = frozenset({
    "Justfile",
    "just/source.just",
    "tools/source_collaboration.py",
    "tools/source_publication_launcher.sh",
    "just/template.just",
    "tools/render_templates.py",
    "flake.nix",
})


class GuardError(RuntimeError):
    pass


def trusted_environment() -> dict[str, str]:
    """Subprocesses must not inherit caller Git/SSH/CLI execution overrides."""
    if (not Path(TRUSTED_GIT).is_absolute() or not Path(TRUSTED_GIT).is_file()
            or not Path(TRUSTED_GH).is_absolute() or not Path(TRUSTED_GH).is_file()
            or not TRUSTED_PATH or not all(Path(part).is_absolute() for part in TRUSTED_PATH.split(os.pathsep))):
        raise GuardError("installed source executables are unavailable")
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError as exc:
        raise GuardError("canonical account home is unavailable") from exc
    environment = {
        "HOME": home,
        "PATH": TRUSTED_PATH,
        "LC_ALL": "C",
        "LANG": "C",
        "GH_HOST": "github.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "NO_COLOR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    agent = os.environ.get("SSH_AUTH_SOCK")
    if agent:
        socket_path = Path(agent)
        try:
            info = socket_path.lstat()
        except OSError:
            pass  # A stale agent must not block offline checks or HTTPS origins.
        else:
            if socket_path.is_absolute() and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                environment["SSH_AUTH_SOCK"] = agent
    return environment


def run(root: Path, *args: str, binary: bool = False, check: bool = True,
        env: dict[str, str] | None = None, input_data: bytes | None = None):
    if args and args[0] in {"git", "gh"}:
        args = (TRUSTED_GIT if args[0] == "git" else TRUSTED_GH, *args[1:])
    return subprocess.run(
        list(args), cwd=root, text=not binary, capture_output=True, check=check,
        env=trusted_environment() if env is None else env,
        input=input_data,
    )


GIT_OPTIONS = (
    "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
    "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
)


def git(root: Path, *args: str, binary: bool = False, check: bool = True,
        input_data: bytes | None = None):
    # Candidate hooks, fsmonitor and signing commands must not execute in policy.
    return run(root, "git", *GIT_OPTIONS, *args, binary=binary, check=check,
               input_data=input_data)


def gh_json(root: Path, *args: str) -> Any:
    # Explicit --repo / API endpoints must not load candidate-local Git config.
    value = run(Path(__file__).resolve().parent, "gh", *args).stdout
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise GuardError("GitHub CLI returned invalid JSON") from exc


def root(cwd: Path | None = None) -> Path:
    base = Path.cwd() if cwd is None else cwd
    path = Path(git(base, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if not path.is_dir():
        raise GuardError("repository root is unavailable")
    if cwd is not None and base.resolve() != path:
        raise GuardError("supplied Task worktree is not its own Git root")
    return path


def safe_local_git_config(root_: Path) -> None:
    """Reject candidate-controlled Git configuration that can execute worktree code."""
    raw = git(root_, "config", "--local", "--null", "--list", binary=True).stdout
    allowed = {
        "core.repositoryformatversion", "core.filemode", "core.bare",
        "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
        "remote.origin.url", "remote.origin.fetch", "remote.origin.pushurl",
        "user.name", "user.email",
    }
    for record in raw.split(b"\0"):
        if not record:
            continue
        key, sep, _ = record.partition(b"\n")
        if not sep:
            raise GuardError("invalid local Git configuration")
        try:
            name = key.decode("utf-8").casefold()
        except UnicodeDecodeError as exc:
            raise GuardError("invalid local Git configuration") from exc
        if name not in allowed and not re.fullmatch(r"branch\..+\.(?:remote|merge)", name):
            raise GuardError(f"unsafe local Git configuration is forbidden: {name}")


def trusted_base_revision() -> str:
    # The approved base is part of the installed Python artifact itself. It must
    # remain unchanged when the wrapper is bypassed or the caller alters its env.
    value = TRUSTED_SOURCE_BASE
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) or set(value) == {"0"}:
        raise GuardError("approved source authority revision is unavailable")
    return value


def head(root_: Path) -> str:
    return git(root_, "rev-parse", "--verify", "HEAD").stdout.strip()


def branch(root_: Path) -> str:
    out = git(root_, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if out.returncode or not out.stdout.strip():
        raise GuardError("a named source branch is required")
    return out.stdout.strip()


def registered_worktree(root_: Path, ref: str, old: str) -> tuple[Path, Path]:
    """Bind a mutable .git pointer to this root's registered Git administration."""
    gitdir = Path(git(root_, "rev-parse", "--absolute-git-dir").stdout.strip())
    common = Path(git(root_, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    if (not gitdir.is_absolute() or not common.is_absolute()
            or gitdir.is_symlink() or common.is_symlink()
            or not gitdir.is_dir() or not common.is_dir()
            or gitdir != gitdir.resolve() or common != common.resolve()):
        raise GuardError("Task Git administration is unsafe")
    pointer = root_ / ".git"
    if gitdir == common:
        if pointer.is_symlink() or not pointer.is_dir() or pointer.resolve() != gitdir:
            raise GuardError("Task Git administration is not registered to this root")
    else:
        record = gitdir / "gitdir"
        if (gitdir.parent != common / "worktrees" or pointer.is_symlink()
                or not pointer.is_file() or record.is_symlink() or not record.is_file()
                or Path(record.read_text(encoding="utf-8").strip()) != pointer):
            raise GuardError("Task Git administration is not registered to this root")
    records = git(root_, "worktree", "list", "--porcelain").stdout.split("\n\n")
    matched = [lines for lines in (block.splitlines() for block in records)
               if lines and lines[0] == f"worktree {root_}"]
    if len(matched) != 1 or f"branch {ref}" not in matched[0] or f"HEAD {old}" not in matched[0]:
        raise GuardError("Task root/branch is not an exact registered Git worktree")
    return gitdir, common


def full_branch_ref(root_: Path) -> str:
    out = git(root_, "symbolic-ref", "--quiet", "HEAD", check=False)
    if out.returncode or not out.stdout.strip().startswith("refs/heads/"):
        raise GuardError("Task HEAD is no longer a named branch")
    return out.stdout.strip()


def remote_repo(url: str) -> str:
    url = url.strip()
    for pattern in (
        r"git@github\.com:([a-z0-9-]+)/([a-z0-9_.-]+?)(?:\.git)?",
        r"ssh://git@github\.com/([a-z0-9-]+)/([a-z0-9_.-]+?)(?:\.git)?",
        r"https://github\.com/([a-z0-9-]+)/([a-z0-9_.-]+?)(?:\.git)?/?",
    ):
        match = re.fullmatch(pattern, url, flags=re.ASCII | re.IGNORECASE)
        if match:
            return "/".join(match.groups())
    raise GuardError(f"unsupported origin URL: {url}")


def issue_meta(root_: Path, issue: int) -> dict[str, Any]:
    value = gh_json(
        root_, "issue", "view", str(issue), "--repo", REPO,
        "--json", "number,state,title,url"
    )
    if value.get("number") != issue or str(value.get("state", "")).upper() != "OPEN":
        raise GuardError(f"Issue #{issue} is not an open canonical Issue")
    if not str(value.get("title", "")).strip():
        raise GuardError("Issue title is unavailable")
    return value


def context(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    if issue <= 0:
        raise GuardError("Issue must be positive")
    trusted_base_revision()
    root_ = root(cwd)
    if Path(__file__).resolve().is_relative_to(root_):
        raise GuardError("source publication requires authority installed outside the task worktree")
    safe_local_git_config(root_)
    origin = git(root_, "remote", "get-url", REMOTE).stdout.strip()
    if remote_repo(origin).casefold() != REPO.casefold():
        raise GuardError(f"origin must be exactly {REPO}")
    branch_ = branch(root_)
    if branch_ == DEFAULT:
        raise GuardError("default-branch source mutation is forbidden")
    if not re.search(rf"(?<!\d){issue}(?!\d)", branch_):
        raise GuardError(f"branch {branch_!r} does not bind Issue #{issue}")
    old = head(root_)
    gitdir, common = registered_worktree(root_, f"refs/heads/{branch_}", old)
    meta = issue_meta(root_, issue)
    return {
        "root": root_, "issue": issue, "branch": branch_, "head": old,
        "title": meta["title"], "origin": origin, "gitdir": gitdir, "common": common,
    }


def norm(path: str) -> str:
    if not path or "\\" in path or "\0" in path:
        raise GuardError(f"unsafe path: {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise GuardError(f"unsafe path: {path!r}")
    value = p.as_posix()
    if value == ".git" or value.startswith(".git/"):
        raise GuardError("Git administrative path is forbidden")
    return value


def secret_like(path: str) -> bool:
    parts = [p.casefold() for p in PurePosixPath(norm(path)).parts]
    for index, part in enumerate(parts):
        if part == ".env" or (
            part.startswith(".env.")
            and not (index == len(parts) - 1 and part == ".env.example")
        ):
            return True
        if part.endswith((".pem", ".key")):
            return True
        if part in {"id_rsa.pub", "id_dsa.pub", "id_ecdsa.pub", "id_ed25519.pub"}:
            continue
        for token in ("credentials", "secret", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"):
            if re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", part):
                return True
    return False


def nul_paths(raw: bytes) -> list[str]:
    result = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            result.append(norm(item.decode("utf-8")))
        except UnicodeDecodeError as exc:
            raise GuardError("source paths must be UTF-8") from exc
    return result


def clean_index(root_: Path) -> None:
    out = git(root_, "diff", "--no-ext-diff", "--cached", "--quiet", "--exit-code", check=False)
    if out.returncode not in (0, 1):
        raise GuardError("cannot inspect staged index")
    if out.returncode:
        raise GuardError("index must be clean before source scope capture")


def paths(root_: Path, *, cached: bool = False, unstaged: bool = False) -> list[str]:
    safe_local_git_config(root_)
    args = ["diff", "--no-ext-diff"]
    if cached:
        args.append("--cached")
    args += ["--name-only", "-z", "--no-renames", "--diff-filter=ACDMRTUXB"]
    if not cached and not unstaged:
        args.append("HEAD")
    args.append("--")
    changed = nul_paths(git(root_, *args, binary=True).stdout)
    untracked = []
    if not cached:
        untracked = nul_paths(
            git(root_, "ls-files", "--others", "--exclude-standard", "-z", binary=True).stdout
        )
    return sorted(set(changed + untracked))


def require_approved_lineage(root_: Path, current_head: str) -> None:
    """Prove that installed source authority predates Task HEAD and tracked main.

    Publishing proposed next-version source does not activate that source:
    this policy and its parity checker execute from the pinned installed image.
    """
    base = trusted_base_revision()
    common_dir = Path(git(root_, "rev-parse", "--git-common-dir").stdout.strip())
    if not common_dir.is_absolute():
        common_dir = root_ / common_dir
    common_dir = common_dir.resolve()
    if any((common_dir / path).exists() or (common_dir / path).is_symlink()
           for path in ("info/grafts", "shallow")):
        raise GuardError("grafted or shallow Git history cannot establish source authority")
    tracked = git(root_, "rev-parse", "--verify", "--quiet",
                  f"refs/remotes/{REMOTE}/{DEFAULT}^{{commit}}", check=False)
    if tracked.returncode:
        raise GuardError("tracked origin/main is required for source authority check")
    # A graft installed during preflight cannot silently change the proof.
    if any((common_dir / path).exists() or (common_dir / path).is_symlink()
           for path in ("info/grafts", "shallow")):
        raise GuardError("grafted or shallow Git history cannot establish source authority")
    # Find a raw parent path from each observed tip to the installed base.
    # A merge can legitimately import a side branch rooted before that base.
    # Git ancestry is existential: every alternate parent need not reach base.
    for tip in (tracked.stdout.strip(), current_head):
        visited: set[str] = set()
        pending = [tip]
        found_base = False
        while pending:
            commit_sha = pending.pop()
            if commit_sha == base:
                found_base = True
                break
            if commit_sha in visited:
                continue
            visited.add(commit_sha)
            raw = git(root_, "cat-file", "commit", commit_sha, binary=True).stdout
            parent_lines = [line.split(b" ", 1)[1] for line in raw.split(b"\n\n", 1)[0].split(b"\n")
                            if line.startswith(b"parent ")]
            for parent in parent_lines:
                if not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", parent):
                    raise GuardError("invalid raw source commit ancestry")
                pending.append(parent.decode("ascii"))
        if not found_base:
            raise GuardError("approved source authority revision must precede main and HEAD")


def manifest(ctx: dict[str, Any]) -> dict[str, Any]:
    safe_local_git_config(ctx["root"])
    clean_index(ctx["root"])
    require_approved_lineage(ctx["root"], ctx["head"])
    changed = paths(ctx["root"])
    if not changed:
        raise GuardError("no source changes")
    entries = []
    for path in changed:
        if secret_like(path):
            raise GuardError(f"secret-like path is forbidden: {path}")
        target = ctx["root"] / path
        try:
            info = target.lstat()
        except FileNotFoundError:
            entries.append({"path": path, "kind": "deleted"})
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GuardError(f"source path must be a regular file: {path}")
        data = target.read_bytes()
        entries.append({
            "path": path, "kind": "file", "mode": stat.S_IMODE(info.st_mode),
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        })
    return {
        "version": 1, "repository": REPO, "issue": ctx["issue"],
        "branch": ctx["branch"], "head": ctx["head"], "entries": entries,
    }


def digest(value: dict[str, Any]) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def verify_worktree_scope(root_: Path, value: dict[str, Any]) -> None:
    """Recheck all reviewed live bytes against the still-old real index."""
    wanted = sorted(entry["path"] for entry in value["entries"])
    if paths(root_, unstaged=True) != wanted:
        raise GuardError("source worktree scope changed during commit")
    for entry in value["entries"]:
        path = entry["path"]
        target = root_ / path
        if entry["kind"] == "deleted":
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            raise GuardError(f"source changed while preparing commit: {path}")
        try:
            info = target.lstat()
            live = target.read_bytes()
        except OSError as exc:
            raise GuardError(f"source changed while preparing commit: {path}") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"]
                or len(live) != entry["size"]
                or hashlib.sha256(live).hexdigest() != entry["sha256"]):
            raise GuardError(f"source changed while preparing commit: {path}")


def private_index_git(root_: Path, index: Path, *args: str, check: bool = True):
    env = trusted_environment()
    env["GIT_INDEX_FILE"] = str(index)
    return run(root_, "git", *GIT_OPTIONS, *args, env=env, check=check)


def index_tree_from_bytes(root_: Path, data: bytes) -> str:
    """Inspect an immutable snapshot, never ask Git to write to a held index.lock."""
    with tempfile.TemporaryDirectory(prefix="templates-source-snapshot-", dir="/tmp") as directory:
        index = Path(directory) / "index"
        index.write_bytes(data)
        out = private_index_git(root_, index, "write-tree", check=False)
        if out.returncode:
            raise GuardError("shared index is unmerged or cannot be inspected")
        return out.stdout.strip()


def stage_source_scope(root_: Path, value: dict[str, Any]) -> dict[str, str]:
    """Hash reviewed bytes without touching the real worktree index."""
    blobs = {}
    for entry in value["entries"]:
        path = entry["path"]
        if entry["kind"] == "deleted":
            continue
        target = root_ / path
        try:
            info = target.lstat()
            data = target.read_bytes()
        except OSError as exc:
            raise GuardError(f"source changed while preparing commit: {path}") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"]
                or len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]):
            raise GuardError(f"source changed while preparing commit: {path}")
        oid = git(root_, "hash-object", "--no-filters", "-w", "--stdin",
                  binary=True, input_data=data).stdout.decode("ascii").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid):
            raise GuardError(f"invalid staged blob identity: {path}")
        blobs[path] = oid
    return blobs


def reviewed_tree(root_: Path, value: dict[str, Any], blobs: dict[str, str]) -> tuple[str, bytes]:
    """Construct the reviewed tree independently of the mutable worktree index."""
    with tempfile.TemporaryDirectory(prefix="templates-source-index-", dir="/tmp") as directory:
        index = Path(directory) / "index"

        private_index_git(root_, index, "read-tree", value["head"])
        for entry in value["entries"]:
            path = entry["path"]
            if entry["kind"] == "deleted":
                private_index_git(root_, index, "update-index", "--force-remove", "--", path)
            else:
                mode = "100755" if entry["mode"] & 0o111 else "100644"
                private_index_git(root_, index, "update-index", "--add", "--cacheinfo",
                                  mode, blobs[path], path)
        if private_index_git(root_, index, "diff", "--no-ext-diff", "--cached", "--check",
                             value["head"], check=False).returncode:
            raise GuardError("git diff --cached --check failed for reviewed tree")
        tree = private_index_git(root_, index, "write-tree").stdout.strip()
        return tree, index.read_bytes()


def update_task_ref(root_: Path, common: Path, ref: str, old: str, new: str):
    """Use Git's ref CAS without borrowing this worktree's locked HEAD/reflog."""
    if not common.is_absolute() or common.is_symlink() or not common.is_dir():
        raise GuardError("common Git reference directory is unavailable")
    with tempfile.TemporaryDirectory(prefix="templates-source-ref-", dir="/tmp") as directory:
        scratch = Path(directory)
        # A detached scratch HEAD shares only the common object/reference store.
        # Git update-ref would otherwise try to take this worktree's HEAD.lock
        # for its HEAD reflog while we hold that lock against branch switches.
        (scratch / "HEAD").write_text(old + "\n", encoding="ascii")
        (scratch / "commondir").write_text(str(common) + "\n", encoding="utf-8")
        env = trusted_environment()
        env["GIT_COMMON_DIR"] = str(common)
        return run(root_, "git", *GIT_OPTIONS, "--git-dir", str(scratch),
                   "update-ref", "-m", "templates-source: guarded commit",
                   ref, new, old, check=False, env=env)


def shared_index(ctx: dict[str, Any]) -> Path:
    """Resolve the exact per-worktree index, not a guessed common-dir index."""
    index = Path(git(ctx["root"], "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
    if index != ctx["gitdir"] / "index" or index.is_symlink() or not index.is_file():
        raise GuardError("Task worktree index is unsafe or unavailable")
    return index


def unchanged_index(index: Path, data: bytes, identity: tuple[int, int]) -> None:
    try:
        info = index.lstat()
        if ((info.st_dev, info.st_ino) != identity or not stat.S_ISREG(info.st_mode)
                or index.read_bytes() != data):
            raise GuardError("shared index changed; concurrent staged work was retained")
    except OSError as exc:
        raise GuardError("shared index changed; concurrent staged work was retained") from exc


def pinned_ref(ctx: dict[str, Any], ref: str) -> str | None:
    out = git(ctx["root"], "--git-dir", str(ctx["gitdir"]),
              "rev-parse", "--verify", "--quiet", ref, check=False)
    return out.stdout.strip() if not out.returncode else None


def release_owned_lock(path: Path, fd: int | None) -> None:
    if fd is None:
        return
    owned = os.fstat(fd)
    os.close(fd)
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
        path.unlink()


def commit_ref(ctx: dict[str, Any], ref: str, old: str, new: str,
               value: dict[str, Any], tree: str, prepared_index: bytes) -> None:
    """Fence real Git checkout and staged writers before the exact Task-ref CAS.

    Git checkout can change the index/worktree BEFORE it tries HEAD.lock. Hold
    the per-worktree index.lock first, then HEAD.lock, and never stage through
    the shared index. A published ref with an unconverged index is a precise
    reconciliation failure, not a successful ordinary commit.
    """
    root_, gitdir, common = ctx["root"], ctx["gitdir"], ctx["common"]
    if registered_worktree(root_, ref, old) != (gitdir, common):
        raise GuardError("Task Git administration changed before commit CAS")
    index = shared_index(ctx)
    index_lock = Path(str(index) + ".lock")
    head_file = gitdir / "HEAD"
    head_lock = Path(str(head_file) + ".lock")
    if not head_file.is_file() or head_file.is_symlink():
        raise GuardError("worktree HEAD administrative file is unsafe")
    try:
        index_fd = os.open(index_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise GuardError("cannot lock Task index; concurrent staged work was retained") from exc
    head_fd: int | None = None
    published = False
    attempted = False
    try:
        try:
            head_fd = os.open(head_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise GuardError("cannot lock Task HEAD; concurrent staged work was retained") from exc
        if registered_worktree(root_, ref, old) != (gitdir, common):
            raise GuardError("Task branch/ref moved before commit CAS")
        if head_file.read_bytes() != f"ref: {ref}\n".encode("ascii") or pinned_ref(ctx, ref) != old:
            raise GuardError("Task branch/ref moved before commit CAS")
        info = index.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise GuardError("shared index is not a regular file")
        identity = (info.st_dev, info.st_ino)
        snapshot = index.read_bytes()
        unchanged_index(index, snapshot, identity)
        old_tree = git(root_, "rev-parse", f"{old}^{{tree}}").stdout.strip()
        if index_tree_from_bytes(root_, snapshot) != old_tree:
            raise GuardError("shared index changed after clean scope capture; concurrent staged work was retained")
        verify_worktree_scope(root_, value)
        unchanged_index(index, snapshot, identity)
        if head_file.read_bytes() != f"ref: {ref}\n".encode("ascii") or pinned_ref(ctx, ref) != old:
            raise GuardError("Task branch/ref moved before commit CAS")
        attempted = True
        out = update_task_ref(root_, common, ref, old, new)
        current_ref = pinned_ref(ctx, ref)
        published = current_ref == new
        if not published or head_file.read_bytes() != f"ref: {ref}\n".encode("ascii"):
            raise GuardError(
                f"commit ref reconciliation required: {ref} expected {old} -> {new}, "
                f"observed {current_ref or 'missing'}; concurrent index retained; "
                f"Git reported: {out.stderr.strip()}"
            )
        if registered_worktree(root_, ref, new) != (gitdir, common):
            raise GuardError("Task Git administration changed after commit CAS")
        # A checkout or non-cooperating writer may have changed files or the
        # real index despite the locks. Do not install over any such state.
        unchanged_index(index, snapshot, identity)
        verify_worktree_scope(root_, value)
        os.fchmod(index_fd, stat.S_IMODE(info.st_mode))
        view = memoryview(prepared_index)
        while view:
            view = view[os.write(index_fd, view):]
        os.fsync(index_fd)
        unchanged_index(index, snapshot, identity)
        os.replace(index_lock, index)
        os.close(index_fd)
        index_fd = None
        # Reacquire before checking the final index/worktree. Another Git
        # writer winning this short interval requires reconciliation, not a
        # silent clean-success report.
        index_fd = os.open(index_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if (registered_worktree(root_, ref, new) != (gitdir, common)
                or index.read_bytes() != prepared_index or index_tree_from_bytes(root_, prepared_index) != tree
                or pinned_ref(ctx, ref) != new or head_file.read_bytes() != f"ref: {ref}\n".encode("ascii")
                or paths(root_, cached=True) or paths(root_, unstaged=True)):
            raise GuardError("committed Task index/worktree postcondition changed")
    except (GuardError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        if attempted and not published:
            # Even an exception while querying the ref can follow a successful
            # CAS. Re-read the pinned ref instead of claiming no mutation.
            try:
                observed = pinned_ref(ctx, ref)
            except (GuardError, OSError, subprocess.CalledProcessError) as probe_error:
                raise GuardError(
                    f"commit ref effect uncertain for {ref} expected {old} -> {new}; "
                    f"index retained, reconcile without reset or force-update: {probe_error}"
                ) from exc
            if observed == new:
                published = True
            elif observed != old:
                raise GuardError(
                    f"commit ref conflict for {ref} expected {old} -> {new}, "
                    f"observed {observed or 'missing'}; index retained, reconcile without force-update"
                ) from exc
        if published:
            raise GuardError(
                f"exact commit {new} applied to {ref}, but index/worktree convergence needs "
                f"reconciliation; concurrent work retained; do not reset or force-update: {exc}"
            ) from exc
        if isinstance(exc, GuardError):
            raise
        if isinstance(exc, KeyboardInterrupt):
            raise GuardError(
                f"commit interrupted with {ref} still at {old}; index retained, "
                "recheck exact subject before retry"
            ) from exc
        raise GuardError(f"commit preparation failed without ref update: {exc}") from exc
    finally:
        cleanup_errors = []
        for lock_path, owned_fd in ((head_lock, head_fd), (index_lock, index_fd)):
            try:
                release_owned_lock(lock_path, owned_fd)
            except OSError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise GuardError(
                f"commit lock cleanup needs reconciliation for {ref} expected {old} -> {new}; "
                f"inspect ref/index without reset or force-update: {'; '.join(cleanup_errors)}"
            )


def publication_check(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    value = manifest(ctx)
    safe_local_git_config(ctx["root"])
    check = git(ctx["root"], "diff", "--no-ext-diff", "--check", check=False)
    if check.returncode:
        raise GuardError("git diff --check failed")
    return {
        "status": "READY", "scope_digest": digest(value), "manifest": value,
        "review_sensitive_paths": sorted(
            entry["path"] for entry in value["entries"]
            if entry["path"] in SOURCE_AUTHORITY
        ),
    }


def parity(root_: Path) -> None:
    # The parity checker is installed beside this policy, never loaded from the
    # mutable task worktree's Justfile or tools/render_templates.py.
    renderer = Path(__file__).resolve().with_name("render_templates.py")
    if renderer.is_symlink() or not renderer.is_file():
        raise GuardError("trusted template parity checker is unavailable")
    out = run(root_, sys.executable, "-I", str(renderer), "check", "--root", str(root_),
              check=False)
    if out.returncode:
        raise GuardError("template parity check failed:\n" + (out.stdout + out.stderr).strip())


def commit(issue: int, expected: str, message: str, cwd: Path | None = None) -> dict[str, Any]:
    if not message.strip() or "\n" in message:
        raise GuardError("commit message must be one non-empty line")
    ctx = context(issue, cwd)
    value = manifest(ctx)
    actual = digest(value)
    if actual != expected:
        raise GuardError(f"source scope changed: expected {expected}, got {actual}")
    safe_local_git_config(ctx["root"])
    if git(ctx["root"], "diff", "--no-ext-diff", "--check", check=False).returncode:
        raise GuardError("git diff --check failed")
    parity(ctx["root"])
    wanted = sorted(entry["path"] for entry in value["entries"])
    ref = f"refs/heads/{ctx['branch']}"
    if registered_worktree(ctx["root"], ref, ctx["head"]) != (ctx["gitdir"], ctx["common"]):
        raise GuardError("Task Git administration changed before staging")
    if full_branch_ref(ctx["root"]) != ref or head(ctx["root"]) != ctx["head"]:
        raise GuardError("Task branch/ref moved before staging")
    # Never use the shared index as staging scratch: a concurrent same-path
    # blob must not be overwritten, even briefly, by reviewed bytes.
    blobs = stage_source_scope(ctx["root"], value)
    tree, prepared_index = reviewed_tree(ctx["root"], value, blobs)
    if full_branch_ref(ctx["root"]) != ref or head(ctx["root"]) != ctx["head"]:
        raise GuardError("Task branch/ref moved before commit; staged work was retained")
    new_head = git(ctx["root"], "commit-tree", tree, "-p", ctx["head"], "-m", message).stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", new_head):
        raise GuardError("invalid reviewed commit identity")
    commit_ref(ctx, ref, ctx["head"], new_head, value, tree, prepared_index)
    return {
        "status": "COMMITTED", "issue": issue, "branch": ctx["branch"],
        "old_head": ctx["head"], "head": new_head, "paths": wanted,
    }


def require_clean(root_: Path) -> None:
    safe_local_git_config(root_)
    if git(root_, "status", "--porcelain=v1", "-z", binary=True).stdout:
        raise GuardError("source worktree must be clean")


@contextmanager
def isolated_transport(root_: Path):
    """Use candidate objects, never candidate Git config, for network operations."""
    object_dir = Path(git(root_, "rev-parse", "--git-path", "objects").stdout.strip())
    if not object_dir.is_absolute():
        object_dir = root_ / object_dir
    if object_dir.is_symlink() or not object_dir.is_dir():
        raise GuardError("source object directory is unsafe")
    object_dir = object_dir.resolve()
    object_format = git(root_, "rev-parse", "--show-object-format").stdout.strip()
    if object_format not in {"sha1", "sha256"}:
        raise GuardError("source object format is unsupported")
    with tempfile.TemporaryDirectory(prefix="templates-source-transport-", dir="/tmp") as directory:
        scratch = Path(directory)
        template = scratch / "empty-template"
        template.mkdir()
        bare = scratch / "transport.git"
        run(scratch, "git", *GIT_OPTIONS, "init", "--bare",
            f"--object-format={object_format}", f"--template={template}", str(bare))
        env = trusted_environment()
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(object_dir)
        yield scratch, bare, env


def transport_git(transport, *args: str, check: bool = True):
    scratch, bare, env = transport
    return run(scratch, "git", *GIT_OPTIONS, "--git-dir", str(bare), *args,
               check=check, env=env)


def remote_head(root_: Path, branch_: str, origin_url: str) -> str | None:
    ref = f"refs/heads/{branch_}"
    with isolated_transport(root_) as transport:
        out = transport_git(transport, "ls-remote", "--heads", origin_url, ref, check=False)
    if out.returncode:
        raise GuardError("cannot inspect remote branch")
    lines = [line for line in out.stdout.splitlines() if line]
    if not lines:
        return None
    if len(lines) != 1:
        raise GuardError("remote branch identity is ambiguous")
    sha, observed = lines[0].split("\t", 1)
    if observed != ref or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise GuardError("invalid remote branch response")
    return sha


def transport_push(
    root_: Path, origin_url: str, destination: str, branch_: str, expected_head: str,
    remote: str | None,
):
    with isolated_transport(root_) as transport:
        if remote:
            if transport_git(transport, "cat-file", "-e", f"{remote}^{{commit}}", check=False).returncode:
                fetched = transport_git(
                    transport, "fetch", "--no-tags", origin_url,
                    f"refs/heads/{branch_}:refs/heads/source-upstream", check=False,
                )
                if fetched.returncode or transport_git(
                    transport, "rev-parse", "refs/heads/source-upstream"
                ).stdout.strip() != remote:
                    raise GuardError("remote branch changed during source ancestry check")
            if transport_git(
                transport, "merge-base", "--is-ancestor", remote, expected_head, check=False
            ).returncode:
                raise GuardError("remote branch diverged; push would not be fast-forward")
        return transport_git(
            transport, "push", "--no-verify", destination,
            f"{expected_head}:refs/heads/{branch_}", check=False,
        )


def push_destination(root_: Path) -> str:
    # Git can send `git push origin` to pushurl instead of the validated fetch URL.
    # Bind the actual push to one independently validated destination.
    urls = git(root_, "remote", "get-url", "--push", "--all", REMOTE).stdout.splitlines()
    if len(urls) != 1 or urls[0] != urls[0].strip():
        raise GuardError("origin must have exactly one canonical push destination")
    if remote_repo(urls[0]).casefold() != REPO.casefold():
        raise GuardError(f"origin push destination must be exactly {REPO}")
    # A URL already expanded from origin can be rewritten AGAIN when passed as
    # an explicit git push argument. Reject matching rules rather than trusting
    # the first expansion as proof of the final transport destination.
    rules = git(root_, "config", "--null", "--get-regexp", r"^url\.",
                binary=True, check=False)
    if rules.returncode not in (0, 1):
        raise GuardError("cannot inspect Git URL rewrite configuration")
    for entry in rules.stdout.split(b"\0"):
        if not entry:
            continue
        if b"\n" not in entry:
            raise GuardError("invalid Git URL rewrite configuration")
        key, prefix = entry.split(b"\n", 1)
        try:
            rewrite_key = key.decode("utf-8").casefold()
        except UnicodeDecodeError as exc:
            raise GuardError("invalid Git URL rewrite configuration") from exc
        if rewrite_key.endswith((".insteadof", ".pushinsteadof")) and urls[0].encode().startswith(prefix):
            raise GuardError("validated push destination is subject to Git URL rewriting")
    return urls[0]


def push(issue: int, expected_head: str, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    require_approved_lineage(ctx["root"], ctx["head"])
    if ctx["head"] != expected_head:
        raise GuardError("local HEAD moved")
    destination = push_destination(ctx["root"])
    remote = remote_head(ctx["root"], ctx["branch"], ctx["origin"])
    if remote == expected_head:
        return {"status": "ALREADY_PUSHED", "head": expected_head, "branch": ctx["branch"]}
    out = transport_push(ctx["root"], ctx["origin"], destination, ctx["branch"],
                         expected_head, remote)
    if out.returncode:
        if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) == expected_head:
            return {"status": "PUSHED", "head": expected_head, "branch": ctx["branch"]}
        raise GuardError("source push failed:\n" + (out.stdout + out.stderr).strip())
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
        raise GuardError("remote push postcondition mismatch")
    return {"status": "PUSHED", "head": expected_head, "branch": ctx["branch"]}


def api_pages(root_: Path, endpoint: str) -> list[dict[str, Any]]:
    """Consume every REST page; duplicate IDs signal a shifting/incomplete scan."""
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        batch = gh_json(root_, "api", f"{endpoint}{'&' if '?' in endpoint else '?'}{query}")
        if not isinstance(batch, list) or len(batch) > 100:
            raise GuardError(f"invalid GitHub page for {endpoint}")
        for item in batch:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                raise GuardError(f"invalid GitHub record for {endpoint}")
            if item["id"] in seen:
                raise GuardError(f"duplicate GitHub record during paginated scan: {item['id']}")
            seen.add(item["id"])
            items.append(item)
        if len(batch) < 100:
            return items
        page += 1


def same_repo(name: Any) -> bool:
    return isinstance(name, str) and name.casefold() == REPO.casefold()


def require_task_head(ctx: dict[str, Any]) -> None:
    """A saved subject is not proof that the local Task still names that subject."""
    ref = f"refs/heads/{ctx['branch']}"
    try:
        if (registered_worktree(ctx["root"], ref, ctx["head"])
                != (ctx["gitdir"], ctx["common"]) or full_branch_ref(ctx["root"]) != ref
                or head(ctx["root"]) != ctx["head"]):
            raise GuardError("registered Task administration/ref no longer matches")
    except GuardError as exc:
        raise GuardError(f"local Task branch/HEAD changed; reconcile {ref} at {ctx['head']}: {exc}") from exc


def pulls(root_: Path, branch_: str) -> list[dict[str, Any]]:
    # Scan all states, not just a filtered first page: a prior Human-closed
    # identity, an altered base or a deleted/changed head must not be bypassed.
    found = []
    for pr in api_pages(root_, f"repos/{REPO}/pulls?state=all"):
        h = pr.get("head") or {}
        if not isinstance(h, dict):
            raise GuardError("invalid PR head response")
        head_repo = h.get("repo") or {}
        if not isinstance(head_repo, dict):
            raise GuardError("invalid PR head repository response")
        repo = head_repo.get("full_name")
        label = h.get("label")
        owner_label = isinstance(label, str) and label.casefold() == f"upiscium:{branch_}".casefold()
        if owner_label or (h.get("ref") == branch_ and same_repo(repo)):
            found.append(pr)
    return found


def validate_pr(
    pr: dict[str, Any], branch_: str, expected_head: str, issue: int
) -> None:
    h, b = pr.get("head") or {}, pr.get("base") or {}
    if not isinstance(h, dict) or not isinstance(b, dict):
        raise GuardError("invalid PR head/base response")
    head_repo, base_repo = h.get("repo") or {}, b.get("repo") or {}
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        raise GuardError("invalid PR repository response")
    if str(pr.get("state", "")).lower() != "open" or pr.get("merged_at"):
        reason = "merged" if pr.get("merged_at") else "Human-closed"
        raise GuardError(f"PR is not open ({reason}); do not create a replacement")
    if h.get("ref") != branch_ or h.get("sha") != expected_head:
        raise GuardError("PR head identity mismatch")
    if not same_repo(head_repo.get("full_name")):
        raise GuardError("PR head repository mismatch")
    if b.get("ref") != DEFAULT or not same_repo(base_repo.get("full_name")):
        raise GuardError("PR base identity mismatch")
    body = str(pr.get("body") or "")
    if not re.search(
        rf"(?im)^\s*(?:closes|fixes|resolves)\s+#{issue}(?!\d)", body
    ):
        raise GuardError(f"PR is not bound to Issue #{issue}")


def pr_create(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    require_approved_lineage(ctx["root"], ctx["head"])
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != ctx["head"]:
        raise GuardError("push exact local HEAD before PR creation")
    before = pulls(ctx["root"], ctx["branch"])
    if len(before) > 1:
        raise GuardError("multiple PR identities exist for source branch")
    # Re-observe GitHub immediately before any create, including when a first
    # scan found no PR. A concurrent closed/incompatible identity is authority.
    found = pulls(ctx["root"], ctx["branch"])
    if len(found) > 1 or (before and [p["id"] for p in found] != [p["id"] for p in before]):
        raise GuardError("PR identity changed during discovery; Human reconciliation required")
    attempted = False
    if not found:
        require_task_head(ctx)
        if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != ctx["head"]:
            raise GuardError("remote Task branch moved before PR creation")
        attempted = True
        out = run(
            Path(__file__).resolve().parent, "gh", "pr", "create", "--repo", REPO, "--draft",
            "--base", DEFAULT, "--head", ctx["branch"], "--title", ctx["title"],
            "--body", f"Closes #{issue}", check=False
        )
        found = pulls(ctx["root"], ctx["branch"])
        if not found:
            raise GuardError("Draft PR creation effect absent after full re-observation:\n"
                             + (out.stdout + out.stderr).strip())
    if len(found) != 1:
        raise GuardError("multiple PR identities after create; Human reconciliation required")
    pr = found[0]
    validate_pr(pr, ctx["branch"], ctx["head"], issue)
    if pr.get("draft") is not True:
        raise GuardError("exact source PR is not Draft")
    if attempted and not out.returncode and out.stdout.strip():
        match = re.fullmatch(rf"https://github\.com/{re.escape(REPO)}/pull/(\d+)/?",
                             out.stdout.strip(), flags=re.IGNORECASE)
        if not match or int(match[1]) != pr.get("number"):
            raise GuardError("PR create acknowledgement conflicts with discovered identity")
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != ctx["head"]:
        raise GuardError("remote Task branch moved during PR reconciliation")
    require_task_head(ctx)
    return {
        "status": "CREATED" if attempted else "ADOPTED", "pr": pr.get("number"),
        "url": pr.get("html_url"), "head": ctx["head"],
    }


def publication_pr(root_: Path, pr_number: int, branch_: str, expected_head: str,
                   issue: int) -> None:
    pr = gh_json(root_, "api", f"repos/{REPO}/pulls/{pr_number}")
    if not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] != pr_number:
        raise GuardError("checkpoint PR number/repository mismatch")
    validate_pr(pr, branch_, expected_head, issue)


def publication_principal(root_: Path) -> dict[str, Any]:
    principal = gh_json(root_, "api", "user")
    if (not isinstance(principal, dict) or type(principal.get("id")) is not int
            or principal["id"] <= 0 or not isinstance(principal.get("login"), str)
            or not principal["login"] or not isinstance(principal.get("type"), str)
            or not principal["type"]):
        raise GuardError("authenticated GitHub publication principal has no stable identity")
    return principal


def checkpoint_matches(root_: Path, pr_number: int, marker: str, text: str,
                       principal: dict[str, Any]) -> list[int]:
    """Only exact comments by the authenticated numeric principal are authority."""
    matches = []
    for item in api_pages(root_, f"repos/{REPO}/issues/{pr_number}/comments"):
        body = item.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        author = item.get("user") or {}
        if (not isinstance(author, dict) or type(author.get("id")) is not int
                or author["id"] != principal["id"]):
            continue  # A copied marker posted by another participant is not a checkpoint.
        if author.get("type") != principal["type"]:
            raise GuardError(f"checkpoint principal type conflicts for comment {item['id']}")
        issue_url = item.get("issue_url")
        parsed = urllib.parse.urlsplit(issue_url) if isinstance(issue_url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.netloc != "api.github.com"
                or parsed.query or parsed.fragment or not re.fullmatch(
                    rf"/repos/{re.escape(REPO)}/issues/{pr_number}", parsed.path,
                    flags=re.IGNORECASE,
                )):
            raise GuardError(f"checkpoint repository/PR provenance conflicts for comment {item['id']}")
        if body != text:
            raise GuardError(f"conflicting trusted checkpoint body for comment {item['id']}")
        matches.append(item["id"])
    return sorted(matches)  # Identical concurrent posts: lowest stable GitHub ID wins.


def checkpoint(
    issue: int, pr_number: int, expected_head: str, body: str, cwd: Path | None = None
) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    require_approved_lineage(ctx["root"], ctx["head"])
    if ctx["head"] != expected_head or remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
        raise GuardError("checkpoint requires exact local/remote HEAD")
    if not body.strip() or len(body.encode()) > 60_000:
        raise GuardError("checkpoint body is empty or too large")
    principal = publication_principal(ctx["root"])
    publication_pr(ctx["root"], pr_number, ctx["branch"], expected_head, issue)
    normalized = body.strip()
    marker = (
        f"<!-- source-checkpoint:{issue}:{expected_head}:"
        f"{hashlib.sha256(normalized.encode()).hexdigest()} -->"
    )
    text = marker + "\n" + normalized + "\n"
    matches = checkpoint_matches(ctx["root"], pr_number, marker, text, principal)
    attempted = False
    if not matches:
        # A second full scan immediately before posting converges when another
        # writer posted between our first scan and the mutation boundary.
        publication_pr(ctx["root"], pr_number, ctx["branch"], expected_head, issue)
        matches = checkpoint_matches(ctx["root"], pr_number, marker, text, principal)
        if not matches:
            require_task_head(ctx)
            if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
                raise GuardError("remote Task branch moved before checkpoint post")
            attempted = True
            out = run(
                Path(__file__).resolve().parent, "gh", "pr", "comment", str(pr_number),
                "--repo", REPO, "--body", text, check=False,
            )
    # Re-observe all authoritative facts even when the write acknowledgement is
    # lost; a conflicting principal/PR/body is never an idempotent success.
    if publication_principal(ctx["root"])["id"] != principal["id"]:
        raise GuardError("GitHub publication principal changed during checkpoint")
    publication_pr(ctx["root"], pr_number, ctx["branch"], expected_head, issue)
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
        raise GuardError("remote Task branch moved during checkpoint reconciliation")
    require_task_head(ctx)
    matches = checkpoint_matches(ctx["root"], pr_number, marker, text, principal)
    # Page-by-page reads are not a snapshot: do not report success if a later
    # comment page was fetched while the PR, principal or Task subject changed.
    if publication_principal(ctx["root"])["id"] != principal["id"]:
        raise GuardError("GitHub publication principal changed during checkpoint scan")
    publication_pr(ctx["root"], pr_number, ctx["branch"], expected_head, issue)
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
        raise GuardError("remote Task branch moved during checkpoint scan")
    require_task_head(ctx)
    if not matches:
        error = (out.stdout + out.stderr).strip() if attempted else ""
        raise GuardError("checkpoint effect absent after full re-observation: " + error)
    return {"status": "POSTED" if attempted else "ALREADY_POSTED",
            "pr": pr_number, "comment_id": matches[0]}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worktree", type=Path, required=True,
                   help="mutable Templates source worktree to inspect")
    subs = p.add_subparsers(dest="command", required=True)
    q = subs.add_parser("publication-check"); q.add_argument("issue", type=int)
    q = subs.add_parser("commit"); q.add_argument("issue", type=int); q.add_argument("digest"); q.add_argument("message")
    q = subs.add_parser("push"); q.add_argument("issue", type=int); q.add_argument("head")
    q = subs.add_parser("pr-create"); q.add_argument("issue", type=int)
    q = subs.add_parser("checkpoint"); q.add_argument("issue", type=int); q.add_argument("pr", type=int); q.add_argument("head"); q.add_argument("body")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "publication-check":
            result = publication_check(args.issue, args.worktree)
        elif args.command == "commit":
            result = commit(args.issue, args.digest, args.message, args.worktree)
        elif args.command == "push":
            result = push(args.issue, args.head, args.worktree)
        elif args.command == "pr-create":
            result = pr_create(args.issue, args.worktree)
        else:
            result = checkpoint(args.issue, args.pr, args.head, args.body, args.worktree)
    except GuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
