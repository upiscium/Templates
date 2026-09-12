#!/usr/bin/env python3
"""Narrow Templates-root bridge for source-side maintenance recovery."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from urllib.parse import quote


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
_TRUSTED_GH_CONFIG_DIR: Path | None = None
_TRUSTED_GH_TOKEN: str | None = None
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


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if source is None else source
    environment = {
        key: values[key]
        for key in ("NO_COLOR",)
        if key in values
    }
    environment.update(
        {
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _operator_github_token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            if any(character.isspace() for character in token) or len(token) > 4096:
                raise BridgeError(f"{name} is not a valid bounded GitHub token")
            return token

    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        config_root = (account_home / ".config").resolve(strict=True)
        gh_config = (config_root / "gh").resolve(strict=True)
        metadata = gh_config.stat()
    except (KeyError, OSError) as exc:
        raise BridgeError(
            "trusted GitHub authentication is unavailable; set GH_TOKEN or GITHUB_TOKEN"
        ) from exc
    if (
        not gh_config.is_dir()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise BridgeError("canonical GitHub configuration directory is unsafe")
    environment = sanitized_environment()
    environment.update(
        {
            "HOME": str(account_home),
            "XDG_CONFIG_HOME": str(config_root),
            "GH_CONFIG_DIR": str(gh_config),
            "GH_HOST": "github.com",
        }
    )
    result = subprocess.run(
        [str(trusted_gh()), "auth", "token", "--hostname", "github.com"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    token = result.stdout.strip()
    if (
        result.returncode
        or not token
        or any(character.isspace() for character in token)
        or len(token) > 4096
    ):
        raise BridgeError(
            "trusted GitHub authentication is unavailable; set GH_TOKEN or GITHUB_TOKEN"
        )
    return token


def trusted_gh_environment() -> dict[str, str]:
    global _TRUSTED_GH_TOKEN
    if _TRUSTED_GH_CONFIG_DIR is None:
        raise BridgeError("trusted GitHub environment is unavailable outside maintenance recovery")
    if _TRUSTED_GH_TOKEN is None:
        _TRUSTED_GH_TOKEN = _operator_github_token()
    return {
        "GH_CONFIG_DIR": str(_TRUSTED_GH_CONFIG_DIR),
        "GH_HOST": "github.com",
        "GH_TOKEN": _TRUSTED_GH_TOKEN,
    }


def trusted_gh_run(command: list[str], **kwargs):
    if not command or command[0] != "gh":
        raise BridgeError("Task Contract GitHub runner only accepts gh commands")
    environment = sanitized_environment(kwargs.pop("env", None))
    environment.update(trusted_gh_environment())
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
    environment = sanitized_environment()
    if command[0] == "gh":
        environment.update(trusted_gh_environment())
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
    global _TRUSTED_GH_CONFIG_DIR, _TRUSTED_GH_TOKEN
    previous = dict(os.environ)
    previous_config = _TRUSTED_GH_CONFIG_DIR
    previous_token = _TRUSTED_GH_TOKEN
    with tempfile.TemporaryDirectory(prefix="automation-gh-") as directory:
        private_config = Path(directory)
        os.chmod(private_config, 0o700)
        _TRUSTED_GH_CONFIG_DIR = private_config
        _TRUSTED_GH_TOKEN = None
        os.environ["AUTOMATION_MAINTENANCE"] = "1"
        for key in list(os.environ):
            if key.startswith("GIT_") or key in {
                "GH_REPO", "GH_HOST", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN",
                "GITHUB_REPOSITORY",
            }:
                os.environ.pop(key, None)
        try:
            yield
        finally:
            _TRUSTED_GH_TOKEN = previous_token
            _TRUSTED_GH_CONFIG_DIR = previous_config
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
    publication_ready = sub.add_parser("publication-ready-recover")
    publication_ready.add_argument("target", type=Path)
    publication_ready.add_argument("task", type=_issue_argument)
    publication_ready.add_argument("expected_implementation_revision", type=_revision_argument)
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


def _optional_state_bytes(target: Path, name: str, contract) -> bytes | None:
    path = target / ".task-state" / name
    try:
        with contract.contract_state_lock(target) as directory_fd:
            content = contract._read_state_file(directory_fd, name)
            contract._assert_state_dir_binding(target, directory_fd)
        return content
    except Exception as exc:
        raise BridgeError(f"cannot read optional Task evidence: {path}") from exc


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
        if status not in {"blocked", "publication-ready", "draft-pr-created"}:
            raise BridgeError(
                "publication recovery requires blocked, publication-ready, or "
                f"draft-pr-created; found {status}"
            )
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
    issue = _optional_state_bytes(target, "issue.json", contract_module)
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
        "issue": issue,
        "state": state,
        "status": status,
    }


def _assert_publication_postconditions(modules: dict, target: Path, task: str, before: dict) -> None:
    after = _publication_snapshot_for_post(modules, target, task)
    for name in ("record", "head", "branch", "repository", "work_units", "verification", "contract", "issue"):
        if after.get(name) != before.get(name):
            raise BridgeError(f"publication recovery changed immutable target evidence: {name}")
    if before["status"] == "publication-ready":
        expected_state = re.sub(
            rb"(?m)^- Status: publication-ready$",
            b"- Status: draft-pr-created",
            before["state"],
            count=1,
        )
        if expected_state == before["state"] or after["state"] != expected_state:
            raise BridgeError("publication recovery changed Task State beyond the guarded lifecycle transition")
    elif before["status"] == "draft-pr-created":
        if after["state"] != before["state"]:
            raise BridgeError("publication recovery changed Task State from draft-pr-created")
    elif before["status"] == "blocked":
        expected_state = re.sub(
            rb"(?m)^- Status: blocked$", b"- Status: publication-ready", before["state"], count=1
        )
        expected_state = re.sub(
            rb"(?m)^- Status: publication-ready$",
            b"- Status: draft-pr-created",
            expected_state,
            count=1,
        )
        if expected_state == before["state"] or after["state"] != expected_state:
            raise BridgeError("publication recovery changed Task State beyond the guarded lifecycle transition")
    else:  # pragma: no cover - the initial snapshot rejects this state
        raise BridgeError(f"publication recovery started from an unsupported state: {before['status']}")
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
        "record": record,
        "head": head,
        "branch": branch,
        "repository": repository,
        "work_units": _state_bytes(target, "work-units.json", contract_module),
        "verification": _state_bytes(target, "verification.json", contract_module),
        "contract": _state_bytes(target, "contract.json", contract_module),
        "issue": _optional_state_bytes(target, "issue.json", contract_module),
        "state": _state_bytes(target, "task.md", contract_module),
        "status": status,
    }


def _replace_publication_status(state: bytes, expected: bytes, target: bytes) -> bytes:
    updated, count = re.subn(
        rb"(?m)^- Status: " + re.escape(expected) + rb"$",
        b"- Status: " + target,
        state,
        count=1,
    )
    if count != 1:
        raise BridgeError(
            f"cannot derive recovery Task State transition {expected.decode()} -> {target.decode()}"
        )
    return updated


def _recovery_states(snapshot: dict) -> tuple[bytes, bytes, bytes]:
    state = snapshot["state"]
    if snapshot["status"] == "blocked":
        blocked = state
        ready = _replace_publication_status(blocked, b"blocked", b"publication-ready")
        draft = _replace_publication_status(ready, b"publication-ready", b"draft-pr-created")
    elif snapshot["status"] == "publication-ready":
        ready = state
        blocked = _replace_publication_status(ready, b"publication-ready", b"blocked")
        draft = _replace_publication_status(ready, b"publication-ready", b"draft-pr-created")
    elif snapshot["status"] == "draft-pr-created":
        draft = state
        ready = _replace_publication_status(draft, b"draft-pr-created", b"publication-ready")
        blocked = _replace_publication_status(ready, b"publication-ready", b"blocked")
    else:  # pragma: no cover - snapshots reject all other states
        raise BridgeError("unsupported publication recovery status")
    return blocked, ready, draft


def _publication_recovery_receipt(
    target: Path, task: str, snapshot: dict, base: str, pr_number: int
) -> bytes:
    blocked, ready, draft = _recovery_states(snapshot)
    base_match = re.search(
        rb"(?m)^- Base revision: ([0-9a-fA-F]{40,64})$", snapshot["state"]
    )
    if base_match is None:
        raise BridgeError("Task State has no valid Base revision")

    def digest(content: bytes | None) -> str | None:
        return hashlib.sha256(content).hexdigest() if content is not None else None

    value = {
        "schema_version": 1,
        "kind": "blocked-publication-recovery",
        "repository": snapshot["repository"],
        "task_id": task,
        "worktree": str(target),
        "branch": snapshot["branch"],
        "head": snapshot["head"],
        "base_branch": base,
        "base_revision": base_match.group(1).decode("ascii").lower(),
        "pr_number": pr_number,
        "blocked_state_sha256": digest(blocked),
        "publication_ready_state_sha256": digest(ready),
        "draft_pr_created_state_sha256": digest(draft),
        "work_units_sha256": digest(snapshot["work_units"]),
        "verification_sha256": digest(snapshot["verification"]),
        "contract_sha256": digest(snapshot["contract"]),
        "issue_sha256": digest(snapshot["issue"]),
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_publication_recovery_receipt(modules: dict, target: Path) -> bytes | None:
    private = modules["git_private_state"]
    try:
        private.prepare(target, admin=True)
        path = private.publication_recovery_receipt(target)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        return private.read_bytes(path, "publication recovery receipt")
    except Exception as exc:
        raise BridgeError(str(exc)) from exc


def _validate_publication_recovery_receipt(
    receipt: bytes, target: Path, task: str, snapshot: dict, base: str
) -> dict:
    try:
        value = json.loads(receipt.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("publication recovery receipt is invalid") from exc
    if not isinstance(value, dict):
        raise BridgeError("publication recovery receipt is invalid")
    blocked, ready, draft = _recovery_states(snapshot)

    def digest(content: bytes | None) -> str | None:
        return hashlib.sha256(content).hexdigest() if content is not None else None

    expected = {
        "repository": snapshot["repository"],
        "task_id": task,
        "worktree": str(target),
        "branch": snapshot["branch"],
        "head": snapshot["head"],
        "base_branch": base,
        "blocked_state_sha256": digest(blocked),
        "publication_ready_state_sha256": digest(ready),
        "draft_pr_created_state_sha256": digest(draft),
        "work_units_sha256": digest(snapshot["work_units"]),
        "verification_sha256": digest(snapshot["verification"]),
        "contract_sha256": digest(snapshot["contract"]),
        "issue_sha256": digest(snapshot["issue"]),
    }
    mismatches = [name for name, expected_value in expected.items() if value.get(name) != expected_value]
    base_match = re.search(
        rb"(?m)^- Base revision: ([0-9a-fA-F]{40,64})$", snapshot["state"]
    )
    if base_match is None or value.get("base_revision") != base_match.group(1).decode("ascii").lower():
        mismatches.append("base_revision")
    number = value.get("pr_number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        mismatches.append("pr_number")
    if mismatches:
        raise BridgeError(
            "publication recovery receipt does not match the exact current subject: "
            + ", ".join(sorted(set(mismatches)))
        )
    return value


def _publication_recover(modules: dict, target: Path, task: str) -> dict:
    before = _publication_snapshot(modules, target, task)
    lifecycle = modules["task_lifecycle"]
    agent_core = modules["agent_core"]
    publication = modules["publication_metadata"]
    recovery_receipt = _read_publication_recovery_receipt(modules, target)
    receipt_value = None
    receipt_base = None
    if recovery_receipt is not None:
        receipt_base = agent_core.default_branch(target)
        receipt_value = _validate_publication_recovery_receipt(
            recovery_receipt, target, task, before, receipt_base
        )

    def existing_pr_number(existing: object) -> int:
        if (
            not isinstance(existing, dict)
            or not isinstance(existing.get("number"), int)
            or isinstance(existing.get("number"), bool)
            or existing.get("number") < 1
        ):
            raise BridgeError("existing Draft PR has an invalid or ambiguous number")
        return existing["number"]

    def validate_blocked_pr(existing: object, expected_base: str) -> int:
        number = existing_pr_number(existing)
        if (
            existing.get("state") != "OPEN"
            or existing.get("isDraft") is not True
            or existing.get("isCrossRepository") is not False
            or existing.get("headRefName") != before["branch"]
            or existing.get("baseRefName") != expected_base
            or existing.get("headRefOid") != before["head"]
        ):
            raise BridgeError("blocked publication recovery requires the exact existing Draft PR")
        return number

    def prove_canonical_metadata() -> None:
        base_match = re.search(
            rb"(?m)^- Base revision: ([0-9a-fA-F]{40,64})$", before["state"]
        )
        if base_match is None:
            raise BridgeError("Task State has no valid Base revision")
        base = base_match.group(1).decode("ascii")
        paths = _target_git(
            "diff", "--name-only", f"{base}...{before['head']}", target=target
        ).splitlines()
        try:
            publication.canonical_metadata(
                target, task, head=before["head"], changed_paths=paths
            )
        except Exception as exc:
            raise BridgeError(str(exc)) from exc

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
            initial_pr_number = None
            blocked_base = None
            authority_before = before
            if before["status"] == "blocked":
                initial_pr = agent_core.pr_for_branch(
                    target, before["branch"], before["repository"]
                )
                if initial_pr is None:
                    raise BridgeError(
                        "blocked publication recovery requires the existing Draft PR"
                    )
                blocked_base = agent_core.default_branch(target)
                initial_pr_number = validate_blocked_pr(initial_pr, blocked_base)
                if receipt_value is not None and receipt_value["pr_number"] != initial_pr_number:
                    raise BridgeError(
                        "existing Draft PR identity differs from recovery receipt"
                    )
                prove_canonical_metadata()
                immediately_before = _publication_snapshot(modules, target, task)
                for name in (
                    "record", "head", "branch", "repository", "work_units", "verification",
                    "contract", "issue", "state", "status",
                ):
                    if immediately_before[name] != before[name]:
                        raise BridgeError(
                            f"publication authority changed before blocked recovery: {name}"
                        )
                confirmed = agent_core.pr_for_branch(
                    target, before["branch"], before["repository"]
                )
                if agent_core.default_branch(target) != blocked_base:
                    raise BridgeError("default branch changed before blocked recovery")
                if (
                    confirmed is None
                    or validate_blocked_pr(confirmed, blocked_base) != initial_pr_number
                ):
                    raise BridgeError("existing Draft PR identity changed before blocked recovery")
                if recovery_receipt is None:
                    recovery_receipt = _publication_recovery_receipt(
                        target, task, before, blocked_base, initial_pr_number
                    )
                try:
                    lifecycle.recover_blocked_publication_ready(
                        before["record"],
                        task,
                        before["state"],
                        {
                            "work-units.json": before["work_units"],
                            "verification.json": before["verification"],
                            "contract.json": before["contract"],
                            "issue.json": before["issue"],
                        },
                        recovery_receipt,
                    )
                except Exception as exc:
                    raise BridgeError(str(exc)) from exc
                authority_before = {
                    **before,
                    "state": re.sub(
                        rb"(?m)^- Status: blocked$",
                        b"- Status: publication-ready",
                        before["state"],
                        count=1,
                    ),
                    "status": "publication-ready",
                }
                recovered = _publication_snapshot(modules, target, task)
                for name in (
                    "record", "head", "branch", "repository", "work_units",
                    "verification", "contract", "issue", "state", "status",
                ):
                    if recovered.get(name) != authority_before.get(name):
                        raise BridgeError(
                            f"blocked publication recovery changed unexpected authority: {name}"
                        )
            if before["status"] in {"publication-ready", "draft-pr-created"} and receipt_value is not None:
                initial_pr = agent_core.pr_for_branch(
                    target, before["branch"], before["repository"]
                )
                if initial_pr is None:
                    raise BridgeError(
                        f"{before['status']} blocked-recovery retry requires the receipt-bound Draft PR"
                    )
                initial_pr_number = validate_blocked_pr(initial_pr, receipt_base)
                if initial_pr_number != receipt_value["pr_number"]:
                    raise BridgeError(
                        "existing Draft PR identity differs from recovery receipt"
                    )
            elif before["status"] == "draft-pr-created":
                initial_pr = agent_core.pr_for_branch(
                    target, before["branch"], before["repository"]
                )
                if initial_pr is None:
                    raise BridgeError(
                        "draft-pr-created recovery requires the existing Draft PR"
                    )
                initial_pr_number = existing_pr_number(initial_pr)
            agent_core.pr_prepare(target, task)
            immediately_before = _publication_snapshot(modules, target, task)
            names = [
                "record", "head", "branch", "repository", "work_units", "verification",
                "contract", "state", "status",
            ]
            if "issue" in before:
                names.append("issue")
            for name in names:
                if immediately_before.get(name) != authority_before.get(name):
                    raise BridgeError(f"publication authority changed before GitHub write: {name}")
            existing = agent_core.pr_for_branch(target, before["branch"], before["repository"])
            if existing is None:
                if (
                    before["status"] != "publication-ready"
                    or initial_pr_number is not None
                    or recovery_receipt is not None
                ):
                    raise BridgeError(
                        f"{before['status']} recovery requires the existing Draft PR"
                    )
                pr = agent_core.pr_create(target, task)
            else:
                existing_number = existing_pr_number(existing)
                if initial_pr_number is not None and existing_number != initial_pr_number:
                    raise BridgeError(
                        "existing Draft PR identity changed before canonical repair"
                    )
                if receipt_value is not None:
                    validate_blocked_pr(existing, receipt_base)
                agent_core.pr_edit(
                    target,
                    task,
                    expected_pr_number=existing_number,
                )
                pr = agent_core.pr_for_branch(target, before["branch"], before["repository"])
                if not pr or pr.get("number") != existing_number:
                    raise BridgeError("existing Draft PR identity changed after canonical repair")
    finally:
        agent_core.verify = original_verify

    if (
        not pr
        or not isinstance(pr.get("number"), int)
        or isinstance(pr.get("number"), bool)
        or pr.get("number") < 1
    ):
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
    if recovery_receipt is not None:
        receipt_live = agent_core.pr_for_branch(
            target, before["branch"], before["repository"]
        )
        if (
            not receipt_live
            or receipt_live.get("number") != json.loads(recovery_receipt)["pr_number"]
        ):
            raise BridgeError(
                "receipt-bound Draft PR identity changed before receipt consumption"
            )
        agent_core._validate_live_pr(
            receipt_live,
            branch=before["branch"],
            base=agent_core.default_branch(target),
            head=before["head"],
            title=title,
            body=body,
            draft=True,
        )
        _, _, final_state = _recovery_states(before)
        try:
            lifecycle.complete_blocked_publication_recovery(
                before["record"],
                task,
                final_state,
                {
                    "work-units.json": before["work_units"],
                    "verification.json": before["verification"],
                    "contract.json": before["contract"],
                    "issue.json": before["issue"],
                },
                recovery_receipt,
            )
        except Exception as exc:
            raise BridgeError(str(exc)) from exc
    return {
        "status": "DRAFT_PR_CREATED",
        "task": task,
        "branch": before["branch"],
        "head": before["head"],
        "repository": before["repository"],
        "pullRequest": pr,
    }


def _source_publication_snapshot(modules: dict, target: Path, task: str) -> dict:
    lifecycle = modules["task_lifecycle"]
    core = modules["agent_core"]
    contract = modules["task_contract"]
    try:
        if lifecycle.repo_root(target) != target:
            raise BridgeError("target is not an exact Git worktree root")
        current = lifecycle.current_worktree(target)
        main = lifecycle.main_worktree(target)
        record = lifecycle.worktree_for_task(target, task)
        if current.path != target or record.path != target or current.path == main.path:
            raise BridgeError("target is not the exact registered non-default Task worktree")
        lifecycle.require_resolved_contract(record, task)
        branch = core.ensure_task_branch(target, task)
        if record.branch != branch:
            raise BridgeError("registered Task branch identity changed")
        repository = core.canonical_repository(target)
        base = core.default_branch(target)
        state = _state_bytes(target, "task.md", contract)
        base_branch = re.search(rb"(?m)^- Base branch: ([^\r\n]+)$", state)
        base_revision = re.search(rb"(?m)^- Base revision: ([0-9a-f]{40,64})$", state)
        if not base_branch or base_branch.group(1).decode() != base or not base_revision:
            raise BridgeError("Task base branch or revision is not canonical and stable")
        status = lifecycle.state_status(lifecycle.state_path(target))
        if status not in {"draft-pr-created", "integration-pending"}:
            raise BridgeError(f"publication-ready recovery requires draft-pr-created or integration-pending; found {status}")
        head = _target_git("rev-parse", "--verify", "HEAD^{commit}", target=target)
        local = _target_git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", target=target)
        if head != local or record.head != head or _remote_branch_head(target, repository, branch) != head:
            raise BridgeError("Task HEAD, local branch, and remote branch differ")
        if _target_git("status", "--porcelain=v1", "--untracked-files=all", target=target):
            raise BridgeError("target worktree must be clean")
        verification = _state_bytes(target, "verification.json", contract)
        if modules["publication_metadata"].verification_evidence(target, task, head) is None:
            raise BridgeError("missing persisted verification evidence")
        modules["publication_metadata"].completed_reviews(target, task)
        changed = _target_git("diff", "--name-only", f"{base_revision.group(1).decode()}...{head}", target=target).splitlines()
        title, body = modules["publication_metadata"].canonical_metadata(
            target, task, head=head, changed_paths=changed
        )
        persisted_title, persisted_body = modules["publication_metadata"].read_and_validate_metadata(
            target, receipt=json.loads(verification.decode("utf-8"))
        )
        if title != persisted_title or not modules["publication_metadata"].canonical_pr_body_matches(body, persisted_body):
            raise BridgeError("persisted publication metadata is not canonical")
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(str(exc)) from exc
    return {
        "record": record, "head": head, "branch": branch, "repository": repository,
        "base": base, "state": state, "status": status, "verification": verification,
        "work_units": _state_bytes(target, "work-units.json", contract),
        "contract": _state_bytes(target, "contract.json", contract),
        "issue": _optional_state_bytes(target, "issue.json", contract),
        "tree": _target_git("rev-parse", "HEAD^{tree}", target=target),
        "title": title, "body": body,
    }


def _source_pr_list(target: Path, repository: str, branch: str) -> list[dict]:
    result = _pinned_run(["gh", "pr", "list", "--repo", repository, "--head", branch,
                          "--state", "all", "--limit", "100", "--json",
                          "number,title,body,headRefName,baseRefName,isDraft,isCrossRepository,state,headRefOid"], cwd=target)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError("invalid pull request list returned by GitHub") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise BridgeError("GitHub pull request list is not an array of objects")
    if len(value) != 1:
        raise BridgeError("exactly one Task-branch pull request is required")
    return value


def _source_pr(core, target: Path, snapshot: dict, *, ready: bool | None) -> tuple[dict, int]:
    listed = _source_pr_list(target, snapshot["repository"], snapshot["branch"])
    pr = core.pr_for_branch(target, snapshot["branch"], snapshot["repository"])
    if not isinstance(pr, dict) or pr.get("number") != listed[0].get("number"):
        raise BridgeError("canonical pull request resolution disagrees with the unique source listing")
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise BridgeError("pull request number must be a positive non-bool integer")
    is_draft = pr.get("isDraft")
    if not isinstance(is_draft, bool):
        raise BridgeError("pull request Draft state is invalid")
    expected_draft = is_draft if ready is None else not ready
    try:
        core._validate_live_pr(pr, branch=snapshot["branch"], base=snapshot["base"],
                               head=snapshot["head"], title=snapshot["title"],
                               body=snapshot["body"], draft=expected_draft)
    except Exception as exc:
        raise BridgeError(str(exc)) from exc
    if pr.get("isCrossRepository") is not False:
        raise BridgeError("cross-repository pull requests are not valid recovery targets")
    return pr, number


def _publication_ready_recover(modules: dict, target: Path, task: str) -> dict:
    before = _source_publication_snapshot(modules, target, task)
    try:
        if _read_publication_recovery_receipt(modules, target) is not None:
            raise BridgeError("publication-recovery receipt exists; refusing to proceed")
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(str(exc)) from exc
    expected_ready = True if before["status"] == "integration-pending" else None
    pr, number = _source_pr(modules["agent_core"], target, before, ready=expected_ready)
    if before["status"] == "integration-pending":
        terminal = _source_publication_snapshot(modules, target, task)
        if terminal != before:
            raise BridgeError("Ready recovery terminal subject changed during validation")
        if _read_publication_recovery_receipt(modules, target) is not None:
            raise BridgeError("publication-recovery receipt appeared during terminal validation")
        final, final_number = _source_pr(
            modules["agent_core"], target, terminal, ready=True
        )
        if final_number != number:
            raise BridgeError("Ready recovery pull request identity changed during terminal validation")
        return {"status": "INTEGRATION_PENDING", "task": task, "pullRequest": final,
                "branch": before["branch"], "head": before["head"], "repository": before["repository"]}
    # Re-read every guarded input immediately before the sole canonical mutation.
    latest = _source_publication_snapshot(modules, target, task)
    if any(latest[key] != before[key] for key in ("record", "head", "branch", "repository", "base", "state", "verification", "work_units", "contract", "issue", "tree", "title", "body")):
        raise BridgeError("Ready recovery subject changed before canonical pr_ready")
    _, latest_number = _source_pr(modules["agent_core"], target, latest, ready=None)
    if latest_number != number:
        raise BridgeError("pull request identity changed before canonical pr_ready")
    core = modules["agent_core"]
    original_verify = core.verify

    def exact_verify(root: Path, requested_task: str) -> None:
        if root.resolve() != target or requested_task != task:
            raise BridgeError("Ready recovery verification target changed")
        _validate_target_git_configuration(target)
        current = _source_publication_snapshot(modules, target, task)
        protected = (
            "record", "head", "branch", "repository", "base", "state", "status",
            "verification", "work_units", "contract", "issue", "tree", "title", "body",
        )
        if any(current[name] != before[name] for name in protected):
            raise BridgeError("Ready recovery subject changed inside canonical pr_ready")
        if _read_publication_recovery_receipt(modules, target) is not None:
            raise BridgeError("publication-recovery receipt appeared before Ready mutation")
    try:
        core.verify = exact_verify
        core.pr_ready(target, task, expected_pr_number=number)
    finally:
        core.verify = original_verify
    after = _source_publication_snapshot(modules, target, task)
    if after["status"] != "integration-pending":
        raise BridgeError("Ready recovery canonical pr_ready did not reach integration-pending")
    if any(after[key] != before[key] for key in ("record", "head", "branch", "repository", "base", "verification", "work_units", "contract", "issue", "tree")):
        raise BridgeError("Ready recovery changed protected evidence or product content")
    if _target_git("status", "--porcelain=v1", "--untracked-files=all", target=target):
        raise BridgeError("Ready recovery changed tracked product content")
    if _read_publication_recovery_receipt(modules, target) is not None:
        raise BridgeError("Ready recovery receipt appeared after canonical pr_ready")
    expected_state = re.sub(
        rb"(?m)^- Status: draft-pr-created$", b"- Status: integration-pending",
        before["state"], count=1,
    )
    if expected_state == before["state"] or after["state"] != expected_state:
        raise BridgeError("Ready recovery changed Task State beyond draft-pr-created -> integration-pending")
    final, final_number = _source_pr(core, target, after, ready=True)
    if final_number != number:
        raise BridgeError("Ready recovery pull request identity changed after canonical pr_ready")
    return {"status": "INTEGRATION_PENDING", "task": task, "pullRequest": final,
            "branch": before["branch"], "head": before["head"], "repository": before["repository"]}


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
            if args.command in {"maintenance-finalize", "publication-ready-recover", "publication-recover"} else None,
        )
        _verify_bootstrap(ROOT, revision)
        _clean_root(ROOT, revision)
        target = args.target.resolve()
        if args.command in {"maintenance-finalize", "publication-ready-recover", "publication-recover"} and target == ROOT:
            raise BridgeError(f"{args.command} target must not be the source root")
        if args.command in {"maintenance-finalize", "publication-ready-recover", "publication-recover"}:
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
            elif args.command == "publication-ready-recover":
                with _verified_modules(ROOT, revision) as modules:
                    _clean_root(ROOT, revision)
                    value = _publication_ready_recover(modules, target, args.task)
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
