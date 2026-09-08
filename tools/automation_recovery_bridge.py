#!/usr/bin/env python3
"""Narrow Templates-root bridge for source-side maintenance recovery."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import quote
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import secrets
import subprocess


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PATH = ROOT / "tools" / "automation_recovery_bridge.py"
ENGINE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
CONTRACT_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_contract.py"
LIFECYCLE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py"
PUBLICATION_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "publication_metadata.py"
AGENT_CORE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "agent_core.py"
MAINTENANCE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "maintenance_lifecycle.py"
CANONICAL_MODULES = (
    ("git_private_state", "components/agent-core/.automation/bin/git_private_state.py"),
    ("task_lifecycle", "components/agent-core/.automation/bin/task_lifecycle.py"),
    ("task_contract", "components/agent-core/.automation/bin/task_contract.py"),
    ("publication_metadata", "components/agent-core/.automation/bin/publication_metadata.py"),
    ("agent_core", "components/agent-core/.automation/bin/agent_core.py"),
    ("automation_upgrade", "components/agent-core/.automation/bin/automation_upgrade.py"),
    ("maintenance_lifecycle", "components/agent-core/.automation/bin/maintenance_lifecycle.py"),
)
_TRUSTED_GIT: Path | None = None
_TRUSTED_GH: Path | None = None
_REVISION_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_UNSAFE_LOCAL_CONFIG = re.compile(
    r"(?:include(?:if)?\..*|url\..*|http\..*|credential\..*|filter\..*|protocol\..*|"
    r"core\.(?:gitproxy|hookspath|sshcommand|worktree)|"
    r"remote\.[^.]+\.(?:proxy|proxyauthmethod|receivepack|uploadpack|vcs))",
    re.IGNORECASE,
)


class BridgeError(RuntimeError):
    pass


class BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BridgeError(message)


def trusted_git() -> Path:
    global _TRUSTED_GIT
    if _TRUSTED_GIT is not None:
        return _TRUSTED_GIT
    candidate = shutil.which("git")
    if not candidate:
        raise BridgeError("trusted Git executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise BridgeError("trusted Git executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BridgeError(f"Git executable is not root-owned and immutable: {executable}")
    _TRUSTED_GIT = executable
    return executable


def trusted_gh() -> Path:
    global _TRUSTED_GH
    if _TRUSTED_GH is not None:
        return _TRUSTED_GH
    candidate = shutil.which("gh")
    if not candidate:
        raise BridgeError("trusted GitHub CLI executable is unavailable")
    executable = Path(candidate).resolve()
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise BridgeError("trusted GitHub CLI executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BridgeError(f"GitHub CLI executable is not root-owned and immutable: {executable}")
    _TRUSTED_GH = executable
    return executable


def trusted_gh_run(command: list[str], **kwargs):
    if not command or command[0] != "gh":
        raise BridgeError("Task Contract GitHub runner only accepts gh commands")
    environment = dict(kwargs.pop("env", os.environ))
    for key in list(environment):
        if key in {"GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"}:
            environment.pop(key, None)
    return subprocess.run([str(trusted_gh()), *command[1:]], env=environment, **kwargs)


def git_bytes(args: list[str], *, cwd: Path) -> bytes:
    if not args or args[0] != "git":
        raise BridgeError("bootstrap Git helper only accepts Git commands")
    environment = {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [str(trusted_git()), "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null", "--no-pager", *args[1:]]
    result = subprocess.run(command, cwd=cwd,
                            capture_output=True, env=environment, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise BridgeError(f"{' '.join(args)}: {detail}")
    return result.stdout


def _revision(root: Path) -> str:
    value = git_bytes(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=root)
    revision = value.decode("ascii", "strict").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise BridgeError("Git HEAD is not a full lowercase commit revision")
    return revision


def _clean_root(root: Path, expected_revision: str | None = None) -> str:
    top = git_bytes(["git", "rev-parse", "--show-toplevel"], cwd=root).decode("utf-8", "strict").strip()
    if Path(top).resolve() != root:
        raise BridgeError(f"recovery bridge must run from the Templates Git root: {top}")
    revision = _revision(root)
    if expected_revision is not None and revision != expected_revision:
        raise BridgeError("Templates HEAD changed during bootstrap")
    if git_bytes(["git", "status", "--porcelain=v1", "-z"], cwd=root):
        raise BridgeError("Templates source worktree must be clean")
    return revision


def _tree_blob(root: Path, revision: str, relative: str) -> tuple[str, int]:
    if not _REVISION_RE.fullmatch(revision):
        raise BridgeError("Git tree lookup requires a full lowercase commit revision")
    raw = git_bytes(["git", "ls-tree", "-z", revision, "--", relative], cwd=root)
    records = raw.split(b"\0")
    records = [record for record in records if record]
    matches: list[tuple[str, int]] = []
    for record in records:
        header, separator, path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3 or path.decode("utf-8", "surrogateescape") != relative:
            continue
        mode, kind, oid = fields
        if kind != b"blob" or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) or mode not in (b"100644", b"100755"):
            raise BridgeError(f"Git tree entry for {relative} is not a safe regular blob")
        matches.append((oid.decode("ascii"), int(mode, 8)))
    if len(matches) != 1:
        raise BridgeError(f"Git revision must contain exactly one regular blob at {relative}")
    return matches[0]


def _blob(root: Path, oid: str) -> bytes:
    return git_bytes(["git", "cat-file", "blob", oid], cwd=root)


def _verify_bootstrap(root: Path, revision: str) -> None:
    if BOOTSTRAP_PATH != Path(__file__).resolve():
        raise BridgeError("bootstrap path is not exactly the expected Templates path")
    oid, mode = _tree_blob(root, revision, "tools/automation_recovery_bridge.py")
    try:
        metadata = BOOTSTRAP_PATH.lstat()
        live = BOOTSTRAP_PATH.read_bytes()
    except OSError as exc:
        raise BridgeError("cannot read the live recovery bootstrap") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o111 != mode & 0o111:
        raise BridgeError("live recovery bootstrap is not a regular file with the HEAD executable mode")
    if live != _blob(root, oid):
        raise BridgeError("live recovery bootstrap does not match its HEAD blob")


def _pinned_run(command, *, cwd=None, check=True, remove_env=(), env_overrides=None,
                input_text=None):
    if not command or command[0] not in {"git", "gh"}:
        raise BridgeError("verified maintenance runner accepts only Git or GitHub commands")
    executable = trusted_git() if command[0] == "git" else trusted_gh()
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_", "LD_", "DYLD_"))
        and key not in {"EMAIL", "GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"}
    }
    if command[0] == "git":
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    for name in remove_env:
        environment.pop(name, None)
    if env_overrides:
        environment.update(env_overrides)
    argv = [str(executable), *command[1:]]
    if command[0] == "git":
        argv = [
            str(executable),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=",
            *command[1:],
        ]
    result = subprocess.run(argv, cwd=cwd, text=True, input=input_text,
                            capture_output=True, env=environment)
    if check and result.returncode:
        raise BridgeError(f"{' '.join(command)}: {result.stderr.strip() or result.stdout.strip() or result.returncode}")
    return result


def _validate_target_git_configuration(target: Path) -> None:
    """Reject consumer-local configuration that can redirect or execute Git work."""
    result = _pinned_run(
        ["git", "config", "--local", "--no-includes", "--null", "--name-only", "--list"],
        cwd=target,
    )
    unsafe = sorted(
        name
        for name in result.stdout.split("\0")
        if name and _UNSAFE_LOCAL_CONFIG.fullmatch(name)
    )
    if unsafe:
        raise BridgeError(
            "consumer repository has unsafe local Git configuration: "
            + ", ".join(unsafe)
        )
    enabled = _pinned_run(
        ["git", "config", "--local", "--no-includes", "--bool", "--get", "extensions.worktreeConfig"],
        cwd=target,
        check=False,
    )
    if enabled.returncode not in {0, 1}:
        raise BridgeError("cannot validate consumer worktree Git configuration")
    if enabled.returncode == 0:
        if enabled.stdout.strip() != "true":
            raise BridgeError("extensions.worktreeConfig must be a valid true boolean")
        worktree = _pinned_run(
            ["git", "config", "--worktree", "--no-includes", "--null", "--name-only", "--list"],
            cwd=target,
        )
        unsafe_worktree = sorted(
            name for name in worktree.stdout.split("\0")
            if name and _UNSAFE_LOCAL_CONFIG.fullmatch(name)
        )
        if unsafe_worktree:
            raise BridgeError(
                "consumer worktree has unsafe Git configuration: "
                + ", ".join(unsafe_worktree)
            )


@contextmanager
def _verified_modules(root: Path, revision: str):
    """Load the complete maintenance bridge exclusively from immutable HEAD blobs."""
    blobs = [(name, path, _tree_blob(root, revision, path)[0])
             for name, path in CANONICAL_MODULES]
    old_path = list(sys.path)
    old_modules = {name: sys.modules.get(name) for name, _ in CANONICAL_MODULES}
    old_bytecode = sys.dont_write_bytecode
    with tempfile.TemporaryDirectory(prefix="automation-maintenance-") as directory:
        private = Path(directory)
        for name, _, oid in blobs:
            path = private / f"{name}.py"
            path.write_bytes(_blob(root, oid))
            os.chmod(path, 0o600)
        sys.path.insert(0, str(private))
        sys.dont_write_bytecode = True
        loaded = {}
        try:
            for name, _, _ in blobs:
                sys.modules.pop(name, None)
                spec = importlib.util.spec_from_file_location(name, private / f"{name}.py")
                if spec is None or spec.loader is None:
                    raise BridgeError(f"cannot create specification for verified {name}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                loaded[name] = module

            git = trusted_git()
            gh = trusted_gh()
            for module in (loaded["task_lifecycle"], loaded["agent_core"],
                           loaded["automation_upgrade"]):
                module.run = _pinned_run
            loaded["automation_upgrade"]._GIT_EXECUTABLE = git
            loaded["automation_upgrade"].git_executable = lambda: git
            loaded["git_private_state"]._GIT_EXECUTABLE = str(git)
            loaded["task_lifecycle"].gh = lambda *args, cwd, check=True: _pinned_run(
                ["gh", *args], cwd=cwd, check=check)
            loaded["agent_core"].gh = lambda *args, cwd=None: _pinned_run(
                ["gh", *args], cwd=cwd).stdout.strip()
            yield loaded
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"cannot load verified maintenance modules: {exc}") from exc
        finally:
            sys.path[:] = old_path
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            sys.dont_write_bytecode = old_bytecode


@contextmanager
def _verified_task_contract(root: Path, revision: str):
    """Load the contract and its dependency exclusively from HEAD Git blobs."""
    contract_oid, contract_mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/task_contract.py")
    lifecycle_oid, lifecycle_mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/task_lifecycle.py")
    private_state_oid, private_state_mode = _tree_blob(
        root, revision, "components/agent-core/.automation/bin/git_private_state.py"
    )
    with tempfile.TemporaryDirectory(prefix="automation-contract-") as directory:
        directory_path = Path(directory)
        lifecycle_path = directory_path / "task_lifecycle.py"
        contract_path = directory_path / "task_contract.py"
        private_state_path = directory_path / "git_private_state.py"
        private_state_path.write_bytes(_blob(root, private_state_oid))
        lifecycle_path.write_bytes(_blob(root, lifecycle_oid))
        contract_path.write_bytes(_blob(root, contract_oid))
        # The Git modes are validated by _tree_blob; the private copies need
        # not retain executable bits and must not be writable by other users.
        os.chmod(lifecycle_path, 0o600)
        os.chmod(contract_path, 0o600)
        os.chmod(private_state_path, 0o600)
        lifecycle_name = f"_templates_verified_lifecycle_{secrets.token_hex(16)}"
        contract_name = f"_templates_verified_contract_{secrets.token_hex(16)}"
        previous_lifecycle = sys.modules.get("task_lifecycle")
        previous_private_state = sys.modules.get("git_private_state")
        old_path = list(sys.path)
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            sys.path.insert(0, str(directory_path))
            lifecycle_spec = importlib.util.spec_from_file_location(lifecycle_name, lifecycle_path)
            if lifecycle_spec is None or lifecycle_spec.loader is None:
                raise BridgeError("cannot create specification for verified Task Contract dependency")
            lifecycle = importlib.util.module_from_spec(lifecycle_spec)
            sys.modules[lifecycle_name] = lifecycle
            sys.modules["task_lifecycle"] = lifecycle
            lifecycle_spec.loader.exec_module(lifecycle)
            sys.modules["git_private_state"]._GIT_EXECUTABLE = str(trusted_git())

            def trusted_lifecycle_run(command, *, cwd=None, check=True):
                if not command or command[0] != "git":
                    raise BridgeError("verified Task Contract attempted a non-Git lifecycle command")
                environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                    "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
                result = subprocess.run(
                    [str(trusted_git()), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                     "-c", "core.pager=", *command[1:]], cwd=cwd, text=True,
                    capture_output=True, env=environment)
                if check and result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                    raise lifecycle.LifecycleError(f"{' '.join(command)}: {detail}")
                return result

            lifecycle.run = trusted_lifecycle_run
            contract_spec = importlib.util.spec_from_file_location(contract_name, contract_path)
            if contract_spec is None or contract_spec.loader is None:
                raise BridgeError("cannot create specification for verified Task Contract")
            contract = importlib.util.module_from_spec(contract_spec)
            sys.modules[contract_name] = contract
            contract_spec.loader.exec_module(contract)
            yield contract
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"cannot load verified Task Contract: {exc}") from exc
        finally:
            sys.modules.pop(contract_name, None)
            sys.modules.pop(lifecycle_name, None)
            if previous_lifecycle is None:
                sys.modules.pop("task_lifecycle", None)
            else:
                sys.modules["task_lifecycle"] = previous_lifecycle
            if previous_private_state is None:
                sys.modules.pop("git_private_state", None)
            else:
                sys.modules["git_private_state"] = previous_private_state
            sys.path[:] = old_path
            sys.dont_write_bytecode = previous_bytecode


@contextmanager
def _verified_engine(root: Path, revision: str):
    oid, mode = _tree_blob(root, revision, "components/agent-core/.automation/bin/automation_upgrade.py")
    engine_bytes = _blob(root, oid)
    private_oid, private_mode = _tree_blob(
        root, revision, "components/agent-core/.automation/bin/git_private_state.py"
    )
    private_bytes = _blob(root, private_oid)
    _clean_root(root, revision)
    with tempfile.TemporaryDirectory(prefix="automation-bridge-") as directory:
        path = Path(directory) / "engine.py"
        private_path = Path(directory) / "git_private_state.py"
        private_path.write_bytes(private_bytes)
        os.chmod(private_path, 0o600)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(engine_bytes)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        name = f"_templates_verified_engine_{secrets.token_hex(16)}"
        previous = sys.dont_write_bytecode
        previous_private_state = sys.modules.get("git_private_state")
        old_path = list(sys.path)
        sys.dont_write_bytecode = True
        spec = None
        try:
            sys.path.insert(0, directory)
            sys.modules.pop("git_private_state", None)
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise BridgeError("cannot create specification for verified recovery engine")
            engine = importlib.util.module_from_spec(spec)
            sys.modules[name] = engine
            spec.loader.exec_module(engine)
            sys.modules["git_private_state"]._GIT_EXECUTABLE = str(trusted_git())
            yield engine
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"cannot load verified recovery engine: {exc}") from exc
        finally:
            sys.modules.pop(name, None)
            if previous_private_state is None:
                sys.modules.pop("git_private_state", None)
            else:
                sys.modules["git_private_state"] = previous_private_state
            sys.path[:] = old_path
            sys.dont_write_bytecode = previous


@contextmanager
def maintenance_environment():
    previous = dict(os.environ)
    os.environ["AUTOMATION_MAINTENANCE"] = "1"
    for key in list(os.environ):
        if key.startswith("GIT_") or key in {"GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"}:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def parser() -> argparse.ArgumentParser:
    result = BoundedArgumentParser(description="Templates source-side maintenance recovery bridge")
    sub = result.add_subparsers(dest="command", required=True, parser_class=BoundedArgumentParser)
    recover = sub.add_parser("recover-maintenance-authority")
    recover.add_argument("target", type=Path)
    commit = sub.add_parser("commit-recovered-maintenance")
    commit.add_argument("target", type=Path)
    commit.add_argument("task")
    commit.add_argument("message", nargs="?", default="")
    issue = sub.add_parser("recover-task-contract-from-issue")
    issue.add_argument("target", type=Path)
    issue.add_argument("issue", type=_issue_argument)
    rebind = sub.add_parser("rebind-maintenance-provenance")
    rebind.add_argument("target", type=Path)
    rebind.add_argument("expected_source_revision", type=_revision_argument)
    resume = sub.add_parser("resume-contract-check")
    resume.add_argument("target", type=Path)
    resume.add_argument("task", type=_issue_argument)
    finalize = sub.add_parser("maintenance-finalize")
    finalize.add_argument("target", type=Path)
    finalize.add_argument("task", type=_issue_argument)
    finalize.add_argument("pr", type=_issue_argument)
    finalize.add_argument("expected_implementation_revision", type=_revision_argument)
    publication = sub.add_parser("publication-recover")
    publication.add_argument("target", type=Path)
    publication.add_argument("task", type=_issue_argument)
    publication.add_argument("expected_implementation_revision", type=_revision_argument)
    return result


def _error_text(exc: BaseException) -> str:
    detail = str(exc).replace("\r", "\\r").replace("\n", "\\n")
    detail = re.sub(r"[^\x20-\x7e]", "?", detail)
    return detail[:1600] or exc.__class__.__name__


def _issue_argument(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise BridgeError("Issue number must be an exact positive decimal integer")
    return value


def _revision_argument(value: str) -> str:
    if not _REVISION_RE.fullmatch(value):
        raise BridgeError("source revision must be a full lowercase immutable Git object ID")
    return value


def _check_resume_contract(contract, target: Path, task: str) -> dict:
    """Check the explicitly named registered Task, without changing it."""
    target_root = contract.lifecycle.repo_root(target)
    if target_root != target:
        raise BridgeError("resume contract check target must be an exact Git worktree root")
    current = contract.lifecycle.current_worktree(target)
    main = contract.lifecycle.main_worktree(target)
    if current.path != target or current.path == main.path:
        raise BridgeError("resume contract check target must be the exact registered Task worktree")
    record = contract.lifecycle.worktree_for_task(target, task)
    if record.path != target:
        raise BridgeError("resume contract check target is not the exact registered Task worktree")
    result = contract.check_resume_contract(target, task, runner=trusted_gh_run)
    if result.get("worktree") != str(target):
        raise BridgeError("canonical resume contract resolved a different Task worktree")
    return result


def _target_git(*args: str, target: Path, check: bool = True) -> str:
    return _pinned_run(["git", *args], cwd=target, check=check).stdout.strip()


def _state_bytes(target: Path, name: str, contract=None) -> bytes:
    path = target / ".task-state" / name
    try:
        if contract is None:
            return path.read_bytes()
        with contract.contract_state_lock(target) as directory_fd:
            content = contract._read_state_file(directory_fd, name)
            contract._assert_state_dir_binding(target, directory_fd)
        if content is None:
            raise BridgeError(f"cannot read required Task evidence: {path}")
        return content
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(f"cannot read required Task evidence: {path}") from exc


def _remote_branch_head(target: Path, repository: str, branch: str) -> str:
    result = _pinned_run(
        ["gh", "api", "--hostname", "github.com", f"repos/{repository}/git/ref/heads/{quote(branch, safe='')}", "--jq", ".object.sha"],
        cwd=target,
    )
    head = result.stdout.strip()
    if not _REVISION_RE.fullmatch(head):
        raise BridgeError("remote Task branch HEAD is not a full immutable revision")
    return head


def _publication_snapshot(modules: dict, target: Path, task: str) -> dict:
    lifecycle = modules["task_lifecycle"]
    agent_core = modules["agent_core"]
    try:
        if lifecycle.repo_root(target) != target:
            raise BridgeError("publication recovery target must be an exact Git worktree root")
        current = lifecycle.current_worktree(target)
        main = lifecycle.main_worktree(target)
        record = lifecycle.worktree_for_task(target, task)
        if current.path != target or record.path != target or current.path == main.path:
            raise BridgeError("publication recovery target must be the exact registered non-default Task worktree")
        lifecycle.require_resolved_contract(record, task)
        branch = agent_core.ensure_task_branch(target, task)
        if record.branch != branch:
            raise BridgeError("registered Task branch identity changed")
        status = lifecycle.state_status(lifecycle.state_path(target))
        if status != "publication-ready":
            raise BridgeError(f"publication recovery requires publication-ready; found {status}")
        repository = agent_core.canonical_repository(target)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(str(exc)) from exc

    head = _target_git("rev-parse", "--verify", "HEAD^{commit}", target=target)
    if not _REVISION_RE.fullmatch(head):
        raise BridgeError("target HEAD is not a full immutable revision")
    local_head = _target_git(
        "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", target=target
    )
    if head != local_head or record.head != head:
        raise BridgeError("target HEAD, local Task branch, and registered worktree HEAD differ")
    if _target_git("status", "--porcelain=v1", "--untracked-files=all", target=target):
        raise BridgeError("target tracked worktree must be clean")
    if _remote_branch_head(target, repository, branch) != head:
        raise BridgeError("remote Task branch does not match the exact target HEAD")

    contract_module = modules["task_contract"]
    work_units = _state_bytes(target, "work-units.json", contract_module)
    verification = _state_bytes(target, "verification.json", contract_module)
    contract = _state_bytes(target, "contract.json", contract_module)
    state = _state_bytes(target, "task.md", contract_module)
    try:
        modules["publication_metadata"].verification_evidence(target, task, head)
        modules["publication_metadata"].completed_reviews(target, task)
    except Exception as exc:
        raise BridgeError(str(exc)) from exc
    return {
        "head": head,
        "branch": branch,
        "repository": repository,
        "record": record,
        "work_units": work_units,
        "verification": verification,
        "contract": contract,
        "state": state,
    }


def _assert_publication_postconditions(modules: dict, target: Path, task: str, before: dict) -> None:
    after = _publication_snapshot_for_post(modules, target, task)
    for name in ("head", "branch", "repository", "work_units", "verification", "contract"):
        if after[name] != before[name]:
            raise BridgeError(f"publication recovery changed immutable target evidence: {name}")
    expected_state = re.sub(
        rb"(?m)^- Status: publication-ready$",
        b"- Status: draft-pr-created",
        before["state"],
        count=1,
    )
    if expected_state == before["state"] or after["state"] != expected_state:
        raise BridgeError("publication recovery changed Task State beyond the guarded lifecycle transition")
    if _target_git("diff", "--name-only", target=target) or _target_git(
        "diff", "--cached", "--name-only", target=target
    ):
        raise BridgeError("publication recovery changed tracked consumer content")


def _publication_snapshot_for_post(modules: dict, target: Path, task: str) -> dict:
    lifecycle = modules["task_lifecycle"]
    agent_core = modules["agent_core"]
    try:
        if lifecycle.repo_root(target) != target:
            raise BridgeError("publication recovery target is no longer the exact Git worktree root")
        current = lifecycle.current_worktree(target)
        main = lifecycle.main_worktree(target)
        record = lifecycle.worktree_for_task(target, task)
        lifecycle.require_resolved_contract(record, task)
        branch = agent_core.ensure_task_branch(target, task)
        status = lifecycle.state_status(lifecycle.state_path(target))
        repository = agent_core.canonical_repository(target)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(str(exc)) from exc
    if current.path != target or record.path != target or current.path == main.path:
        raise BridgeError("publication recovery target worktree identity changed")
    if status != "draft-pr-created":
        raise BridgeError(f"publication recovery did not reach draft-pr-created; found {status}")
    head = _target_git("rev-parse", "--verify", "HEAD^{commit}", target=target)
    local_head = _target_git(
        "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", target=target
    )
    if head != local_head or record.head != head or _remote_branch_head(target, repository, branch) != head:
        raise BridgeError("target Task HEAD identity changed during publication recovery")
    if _target_git("status", "--porcelain=v1", "--untracked-files=all", target=target):
        raise BridgeError("target tracked worktree changed during publication recovery")
    contract_module = modules["task_contract"]
    return {
        "head": head,
        "branch": branch,
        "repository": repository,
        "work_units": _state_bytes(target, "work-units.json", contract_module),
        "verification": _state_bytes(target, "verification.json", contract_module),
        "contract": _state_bytes(target, "contract.json", contract_module),
        "state": _state_bytes(target, "task.md", contract_module),
    }


def _publication_recover(modules: dict, target: Path, task: str) -> dict:
    before = _publication_snapshot(modules, target, task)
    agent_core = modules["agent_core"]
    publication = modules["publication_metadata"]

    def require_persisted_verification(root: Path, requested_task: str) -> None:
        if root.resolve() != target or requested_task != task:
            raise BridgeError("publication recovery verification target changed")
        head = _target_git("rev-parse", "--verify", "HEAD^{commit}", target=target)
        if head != before["head"]:
            raise BridgeError("target HEAD changed before publication")
        if _target_git("status", "--porcelain=v1", "--untracked-files=all", target=target):
            raise BridgeError("target tracked worktree changed before publication")
        publication.verification_evidence(target, task, head)
        if _state_bytes(target, "verification.json", modules["task_contract"]) != before["verification"]:
            raise BridgeError("project verification evidence changed before publication")

    original_verify = agent_core.verify
    output = io.StringIO()
    try:
        agent_core.verify = require_persisted_verification
        with redirect_stdout(output):
            agent_core.pr_prepare(target, task)
            immediately_before = _publication_snapshot(modules, target, task)
            for name in ("head", "branch", "repository", "work_units", "verification", "contract", "state"):
                if immediately_before[name] != before[name]:
                    raise BridgeError(f"publication authority changed before GitHub write: {name}")
            existing = agent_core.pr_for_branch(target, before["branch"], before["repository"])
            if existing is None:
                pr = agent_core.pr_create(target, task)
            else:
                if (
                    not isinstance(existing, dict)
                    or not isinstance(existing.get("number"), int)
                    or isinstance(existing.get("number"), bool)
                ):
                    raise BridgeError("existing Draft PR has an invalid or ambiguous number")
                existing_number = existing["number"]
                agent_core.pr_edit(target, task)
                pr = agent_core.pr_for_branch(target, before["branch"], before["repository"])
                if not pr or pr.get("number") != existing_number:
                    raise BridgeError("existing Draft PR identity changed after canonical repair")
    finally:
        agent_core.verify = original_verify

    if not pr or not isinstance(pr.get("number"), int) or isinstance(pr.get("number"), bool):
        raise BridgeError("published Draft PR cannot be resolved exactly")
    live = agent_core.pr_for_branch(target, before["branch"], before["repository"])
    if not live or live.get("number") != pr["number"]:
        raise BridgeError("published Draft PR identity changed after canonical publication")
    title, _, body = agent_core._validated_local_metadata(target, task, before["head"])
    agent_core._validate_live_pr(
        live,
        branch=before["branch"],
        base=agent_core.default_branch(target),
        head=before["head"],
        title=title,
        body=body,
        draft=True,
    )
    _assert_publication_postconditions(modules, target, task, before)
    return {
        "status": "DRAFT_PR_CREATED",
        "task": task,
        "branch": before["branch"],
        "head": before["head"],
        "repository": before["repository"],
        "pullRequest": pr,
    }


def main() -> int:
    revision = None
    failure = None
    try:
        args = parser().parse_args()
        if Path(__file__).resolve() != BOOTSTRAP_PATH:
            raise BridgeError("bootstrap path is not exactly the expected Templates path")
        revision = _clean_root(
            ROOT,
            args.expected_implementation_revision
            if args.command in {"maintenance-finalize", "publication-recover"} else None,
        )
        _verify_bootstrap(ROOT, revision)
        _clean_root(ROOT, revision)
        target = args.target.resolve()
        if args.command in {"maintenance-finalize", "publication-recover"} and target == ROOT:
            raise BridgeError(f"{args.command} target must not be the source root")
        if args.command in {"maintenance-finalize", "publication-recover"}:
            _validate_target_git_configuration(target)
        with maintenance_environment():
            if args.command in {"recover-task-contract-from-issue", "resume-contract-check"}:
                trusted_git()
                with _verified_task_contract(ROOT, revision) as contract:
                    _clean_root(ROOT, revision)
                    if args.command == "recover-task-contract-from-issue":
                        value = contract.recover_task_from_issue(target, args.issue, runner=trusted_gh_run)
                        result = {"status": "TASK_CONTRACT_RECOVERED", **value}
                    else:
                        result = _check_resume_contract(contract, target, args.task)
                        result["implementationRevision"] = revision
            elif args.command == "maintenance-finalize":
                with _verified_modules(ROOT, revision) as modules:
                    _clean_root(ROOT, revision)
                    value = modules["maintenance_lifecycle"].maintenance_finalize(
                        target, args.task, int(args.pr)
                    )
                    result = {**value, "implementationRevision": revision}
            elif args.command == "publication-recover":
                with _verified_modules(ROOT, revision) as modules:
                    _clean_root(ROOT, revision)
                    value = _publication_recover(modules, target, args.task)
                    result = {**value, "implementationRevision": revision}
            else:
                with _verified_engine(ROOT, revision) as engine:
                    if engine.git_executable().resolve() != trusted_git():
                        raise BridgeError("recovery engine selected a different Git executable")
                    _clean_root(ROOT, revision)
                    if args.command == "recover-maintenance-authority":
                        result = engine.recover_maintenance_authority_from_source(
                            target, ROOT, expected_implementation_revision=revision)
                    elif args.command == "commit-recovered-maintenance":
                        result = engine.commit_recovered_maintenance(
                            target, ROOT, args.task, args.message, expected_implementation_revision=revision)
                    elif args.command == "rebind-maintenance-provenance":
                        result = engine.rebind_maintenance_provenance_from_source(
                            target, ROOT, args.expected_source_revision,
                            expected_implementation_revision=revision)
                    else:  # pragma: no cover
                        raise BridgeError(f"unsupported bridge command: {args.command}")
    except Exception as exc:
        failure = exc
    finally:
        if revision is not None:
            try:
                _clean_root(ROOT, revision)
            except Exception as exc:
                if failure is None:
                    failure = exc
                else:
                    failure = BridgeError(f"{_error_text(failure)}; source recheck failed: {_error_text(exc)}")
    if failure is not None:
        print(f"ERROR: {_error_text(failure)}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
