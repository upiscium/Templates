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
# The live Just routing, guard, and commit-time parity check are one authority chain.
# Changing any part requires a separately reviewed maintainer bootstrap, not source::*.
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
    meta = issue_meta(root_, issue)
    return {
        "root": root_, "issue": issue, "branch": branch_, "head": head(root_),
        "title": meta["title"], "origin": origin,
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


def reject_authority_paths(changed: list[str]) -> None:
    for path in changed:
        if path in SOURCE_AUTHORITY:
            raise GuardError(f"source authority change requires maintainer bootstrap: {path}")


def require_authority_unchanged(root_: Path, current_head: str) -> None:
    """Reject authority changes since the installed, approved bootstrap revision.

    Inspect every branch-only commit, not just the final tree: a change followed by
    a revert must not make authority-changing commits publishable through source::*.
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
    # Walk raw commit objects, not Git's revision walker: grafts and shallow
    # boundaries must not be able to omit an authority-changing parent edge.
    visited: set[str] = set()
    for tip in (tracked.stdout.strip(), current_head):
        pending = [tip]
        while pending:
            commit_sha = pending.pop()
            if commit_sha == base or commit_sha in visited:
                continue
            visited.add(commit_sha)
            raw = git(root_, "cat-file", "commit", commit_sha, binary=True).stdout
            parent_lines = [line.split(b" ", 1)[1] for line in raw.split(b"\n\n", 1)[0].split(b"\n")
                            if line.startswith(b"parent ")]
            if not parent_lines:
                raise GuardError("approved source authority revision must precede main and HEAD")
            for parent in parent_lines:
                if not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", parent):
                    raise GuardError("invalid raw source commit ancestry")
                parent_sha = parent.decode("ascii")
                changed = nul_paths(git(
                    root_, "diff-tree", "--no-ext-diff", "-r", "--name-only", "-z", "--no-renames",
                    "--diff-filter=ACDMRTUXB", parent_sha, commit_sha,
                    "--", *sorted(SOURCE_AUTHORITY), binary=True,
                ).stdout)
                reject_authority_paths(changed)
                pending.append(parent_sha)


def manifest(ctx: dict[str, Any]) -> dict[str, Any]:
    safe_local_git_config(ctx["root"])
    clean_index(ctx["root"])
    require_authority_unchanged(ctx["root"], ctx["head"])
    changed = paths(ctx["root"])
    reject_authority_paths(changed)
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


def verify_staged_scope(root_: Path, value: dict[str, Any]) -> None:
    """Bind the exact index blobs, not only path names, to the reviewed manifest."""
    for entry in value["entries"]:
        path = entry["path"]
        raw = git(root_, "ls-files", "--stage", "-z", "--", path, binary=True).stdout
        if entry["kind"] == "deleted":
            if raw:
                raise GuardError(f"staged source scope changed: {path}")
            continue
        header, separator, name = raw.removesuffix(b"\0").partition(b"\t")
        fields = header.split(b" ")
        if (not separator or name != path.encode("utf-8") or len(fields) != 3
                or fields[2] != b"0" or not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", fields[1])):
            raise GuardError(f"staged source scope changed: {path}")
        expected_mode = b"100755" if entry["mode"] & 0o111 else b"100644"
        if fields[0] != expected_mode:
            raise GuardError(f"staged source scope changed: {path}")
        content = git(root_, "cat-file", "blob", fields[1].decode("ascii"), binary=True).stdout
        try:
            info = (root_ / path).lstat()
            live = (root_ / path).read_bytes()
        except OSError as exc:
            raise GuardError(f"source changed while staging: {path}") from exc
        if (len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]
                or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"]
                or live != content):
            raise GuardError(f"source changed while staging: {path}")


def stage_source_scope(root_: Path, value: dict[str, Any]) -> None:
    """Stage verified bytes directly; never execute candidate Git clean filters."""
    for entry in value["entries"]:
        path = entry["path"]
        if entry["kind"] == "deleted":
            git(root_, "update-index", "--force-remove", "--", path)
            continue
        target = root_ / path
        try:
            info = target.lstat()
            data = target.read_bytes()
        except OSError as exc:
            raise GuardError(f"source changed while staging: {path}") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"]
                or len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]):
            raise GuardError(f"source changed while staging: {path}")
        oid = git(root_, "hash-object", "--no-filters", "-w", "--stdin",
                  binary=True, input_data=data).stdout.decode("ascii").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid):
            raise GuardError(f"invalid staged blob identity: {path}")
        mode = "100755" if entry["mode"] & 0o111 else "100644"
        git(root_, "update-index", "--add", "--cacheinfo", mode, oid, path)


def publication_check(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    value = manifest(ctx)
    safe_local_git_config(ctx["root"])
    check = git(ctx["root"], "diff", "--no-ext-diff", "--check", check=False)
    if check.returncode:
        raise GuardError("git diff --check failed")
    return {"status": "READY", "scope_digest": digest(value), "manifest": value}


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
    try:
        stage_source_scope(ctx["root"], value)
        if paths(ctx["root"], cached=True) != wanted:
            raise GuardError("staged path set does not equal reviewed source scope")
        if paths(ctx["root"], unstaged=True):
            raise GuardError("source changed while staging")
        verify_staged_scope(ctx["root"], value)
        if git(ctx["root"], "diff", "--no-ext-diff", "--cached", "--check", check=False).returncode:
            raise GuardError("git diff --cached --check failed")
        if head(ctx["root"]) != ctx["head"]:
            raise GuardError("HEAD moved before commit")
        out = git(ctx["root"], "commit", "-m", message, check=False)
        if out.returncode:
            raise GuardError("git commit failed:\n" + (out.stdout + out.stderr).strip())
    except Exception:
        git(ctx["root"], "restore", "--staged", "--", *wanted, check=False)
        raise
    new_head = head(ctx["root"])
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
    require_authority_unchanged(ctx["root"], ctx["head"])
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


def pulls(root_: Path, branch_: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "state": "all", "head": f"upiscium:{branch_}", "per_page": "100"
    })
    value = gh_json(root_, "api", f"repos/{REPO}/pulls?{query}")
    if not isinstance(value, list):
        raise GuardError("invalid PR list response")
    return value


def validate_pr(
    pr: dict[str, Any], branch_: str, expected_head: str, issue: int
) -> None:
    h, b = pr.get("head") or {}, pr.get("base") or {}
    if str(pr.get("state", "")).lower() != "open":
        raise GuardError("PR is not open")
    if h.get("ref") != branch_ or h.get("sha") != expected_head:
        raise GuardError("PR head identity mismatch")
    if (h.get("repo") or {}).get("full_name") != REPO:
        raise GuardError("PR head repository mismatch")
    if b.get("ref") != DEFAULT or (b.get("repo") or {}).get("full_name") != REPO:
        raise GuardError("PR base identity mismatch")
    body = str(pr.get("body") or "")
    if not re.search(
        rf"(?im)^\s*(?:closes|fixes|resolves)\s+#{issue}(?!\d)", body
    ):
        raise GuardError(f"PR is not bound to Issue #{issue}")


def pr_create(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    require_authority_unchanged(ctx["root"], ctx["head"])
    if remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != ctx["head"]:
        raise GuardError("push exact local HEAD before PR creation")
    found = pulls(ctx["root"], ctx["branch"])
    if len(found) > 1:
        raise GuardError("multiple PR identities exist for source branch")
    created = False
    if not found:
        out = run(
            Path(__file__).resolve().parent, "gh", "pr", "create", "--repo", REPO, "--draft",
            "--base", DEFAULT, "--head", ctx["branch"], "--title", ctx["title"],
            "--body", f"Closes #{issue}", check=False
        )
        recovered = pulls(ctx["root"], ctx["branch"])
        if out.returncode and not recovered:
            raise GuardError("Draft PR creation failed:\n" + (out.stdout + out.stderr).strip())
        found = recovered
        created = True
    if len(found) != 1:
        raise GuardError("Draft PR postcondition is not unique")
    pr = found[0]
    validate_pr(pr, ctx["branch"], ctx["head"], issue)
    if not bool(pr.get("draft")):
        raise GuardError("exact source PR is not Draft")
    return {
        "status": "CREATED" if created else "ADOPTED", "pr": pr.get("number"),
        "url": pr.get("html_url"), "head": ctx["head"],
    }


def checkpoint(
    issue: int, pr_number: int, expected_head: str, body: str, cwd: Path | None = None
) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    require_authority_unchanged(ctx["root"], ctx["head"])
    if ctx["head"] != expected_head or remote_head(ctx["root"], ctx["branch"], ctx["origin"]) != expected_head:
        raise GuardError("checkpoint requires exact local/remote HEAD")
    if not body.strip() or len(body.encode()) > 60_000:
        raise GuardError("checkpoint body is empty or too large")
    pr = gh_json(ctx["root"], "api", f"repos/{REPO}/pulls/{pr_number}")
    validate_pr(pr, ctx["branch"], expected_head, issue)
    marker = (
        f"<!-- source-checkpoint:{issue}:{expected_head}:"
        f"{hashlib.sha256(body.encode()).hexdigest()} -->"
    )
    comments = gh_json(ctx["root"], "api", f"repos/{REPO}/issues/{pr_number}/comments?per_page=100")
    if not isinstance(comments, list):
        raise GuardError("invalid PR comments response")
    for item in comments:
        if marker in str(item.get("body", "")):
            return {"status": "ALREADY_POSTED", "pr": pr_number, "comment_id": item.get("id")}
    out = run(
        Path(__file__).resolve().parent, "gh", "pr", "comment", str(pr_number), "--repo", REPO,
        "--body", marker + "\n" + body.strip() + "\n", check=False
    )
    if out.returncode:
        comments = gh_json(
            ctx["root"], "api", f"repos/{REPO}/issues/{pr_number}/comments?per_page=100"
        )
        matches = [item for item in comments if marker in str(item.get("body", ""))]
        if len(matches) == 1:
            return {"status": "POSTED", "pr": pr_number, "comment_id": matches[0].get("id")}
        raise GuardError("checkpoint comment failed:\n" + (out.stdout + out.stderr).strip())
    comments = gh_json(ctx["root"], "api", f"repos/{REPO}/issues/{pr_number}/comments?per_page=100")
    matches = [item for item in comments if marker in str(item.get("body", ""))]
    if len(matches) != 1:
        raise GuardError("checkpoint postcondition is not unique")
    return {"status": "POSTED", "pr": pr_number, "comment_id": matches[0].get("id")}


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
