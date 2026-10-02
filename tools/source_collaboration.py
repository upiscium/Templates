#!/usr/bin/env python3
"""Guarded collaboration API for Templates source-development worktrees."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any

REPO = "upiscium/Templates"
DEFAULT = "main"
REMOTE = "origin"


class GuardError(RuntimeError):
    pass


def run(root: Path, *args: str, binary: bool = False, check: bool = True):
    return subprocess.run(
        list(args), cwd=root, text=not binary, capture_output=True, check=check
    )


def git(root: Path, *args: str, binary: bool = False, check: bool = True):
    return run(root, "git", *args, binary=binary, check=check)


def gh_json(root: Path, *args: str) -> Any:
    value = run(root, "gh", *args).stdout
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise GuardError("GitHub CLI returned invalid JSON") from exc


def root(cwd: Path | None = None) -> Path:
    base = Path.cwd() if cwd is None else cwd
    path = Path(run(base, "git", "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if not path.is_dir():
        raise GuardError("repository root is unavailable")
    return path


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
        r"^git@github\.com:(.+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(.+?)(?:\.git)?$",
        r"^https://github\.com/(.+?)(?:\.git)?/?$",
    ):
        match = re.fullmatch(pattern, url)
        if match:
            return match.group(1)
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
    root_ = root(cwd)
    origin = git(root_, "remote", "get-url", REMOTE).stdout.strip()
    if remote_repo(origin) != REPO:
        raise GuardError(f"origin must be exactly {REPO}")
    branch_ = branch(root_)
    if branch_ == DEFAULT:
        raise GuardError("default-branch source mutation is forbidden")
    if not re.search(rf"(?<!\d){issue}(?!\d)", branch_):
        raise GuardError(f"branch {branch_!r} does not bind Issue #{issue}")
    meta = issue_meta(root_, issue)
    return {
        "root": root_, "issue": issue, "branch": branch_, "head": head(root_),
        "title": meta["title"],
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
    out = git(root_, "diff", "--cached", "--quiet", "--exit-code", check=False)
    if out.returncode not in (0, 1):
        raise GuardError("cannot inspect staged index")
    if out.returncode:
        raise GuardError("index must be clean before source scope capture")


def paths(root_: Path, *, cached: bool = False, unstaged: bool = False) -> list[str]:
    args = ["diff"]
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


def manifest(ctx: dict[str, Any]) -> dict[str, Any]:
    clean_index(ctx["root"])
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


def publication_check(issue: int, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    value = manifest(ctx)
    check = git(ctx["root"], "diff", "--check", check=False)
    if check.returncode:
        raise GuardError("git diff --check failed")
    return {"status": "READY", "scope_digest": digest(value), "manifest": value}


def parity(root_: Path) -> None:
    if shutil.which("just") is None:
        raise GuardError("just is required")
    out = run(root_, "just", "template::check", check=False)
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
    if git(ctx["root"], "diff", "--check", check=False).returncode:
        raise GuardError("git diff --check failed")
    parity(ctx["root"])
    wanted = sorted(entry["path"] for entry in value["entries"])
    try:
        git(ctx["root"], "add", "-A", "--", *wanted)
        if paths(ctx["root"], cached=True) != wanted:
            raise GuardError("staged path set does not equal reviewed source scope")
        if paths(ctx["root"], unstaged=True):
            raise GuardError("source changed while staging")
        if git(ctx["root"], "diff", "--cached", "--check", check=False).returncode:
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
    if git(root_, "status", "--porcelain=v1", "-z", binary=True).stdout:
        raise GuardError("source worktree must be clean")


def remote_head(root_: Path, branch_: str) -> str | None:
    ref = f"refs/heads/{branch_}"
    out = git(root_, "ls-remote", "--heads", REMOTE, ref, check=False)
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


def push(issue: int, expected_head: str, cwd: Path | None = None) -> dict[str, Any]:
    ctx = context(issue, cwd)
    require_clean(ctx["root"])
    if ctx["head"] != expected_head:
        raise GuardError("local HEAD moved")
    remote = remote_head(ctx["root"], ctx["branch"])
    if remote == expected_head:
        return {"status": "ALREADY_PUSHED", "head": expected_head, "branch": ctx["branch"]}
    if remote:
        if git(ctx["root"], "cat-file", "-e", f"{remote}^{{commit}}", check=False).returncode:
            git(ctx["root"], "fetch", "--no-tags", REMOTE,
                f"refs/heads/{ctx['branch']}:refs/remotes/{REMOTE}/{ctx['branch']}")
        if git(ctx["root"], "merge-base", "--is-ancestor", remote, expected_head, check=False).returncode:
            raise GuardError("remote branch diverged; push would not be fast-forward")
    out = git(ctx["root"], "push", REMOTE, f"{expected_head}:refs/heads/{ctx['branch']}", check=False)
    if out.returncode:
        if remote_head(ctx["root"], ctx["branch"]) == expected_head:
            return {"status": "PUSHED", "head": expected_head, "branch": ctx["branch"]}
        raise GuardError("source push failed:\n" + (out.stdout + out.stderr).strip())
    if remote_head(ctx["root"], ctx["branch"]) != expected_head:
        raise GuardError("remote push postcondition mismatch")
    return {"status": "PUSHED", "head": expected_head, "branch": ctx["branch"]}


def pulls(root_: Path, branch_: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "state": "open", "head": f"upiscium:{branch_}", "base": DEFAULT, "per_page": "100"
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
    if remote_head(ctx["root"], ctx["branch"]) != ctx["head"]:
        raise GuardError("push exact local HEAD before PR creation")
    found = pulls(ctx["root"], ctx["branch"])
    if len(found) > 1:
        raise GuardError("multiple open PRs exist for source branch")
    created = False
    if not found:
        out = run(
            ctx["root"], "gh", "pr", "create", "--repo", REPO, "--draft",
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
    if ctx["head"] != expected_head or remote_head(ctx["root"], ctx["branch"]) != expected_head:
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
        ctx["root"], "gh", "pr", "comment", str(pr_number), "--repo", REPO,
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
            result = publication_check(args.issue)
        elif args.command == "commit":
            result = commit(args.issue, args.digest, args.message)
        elif args.command == "push":
            result = push(args.issue, args.head)
        elif args.command == "pr-create":
            result = pr_create(args.issue)
        else:
            result = checkpoint(args.issue, args.pr, args.head, args.body)
    except GuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
