#!/usr/bin/env python3
"""Read-only local validation of a Templates / Agent Core release identity.

This check validates Git objects only. A successful result is
``IDENTITY_VALIDATED``; it is not a GitHub publication or release-readiness
claim. When a prior revision is supplied, the caller must independently verify
that it is the previous published release. Supplying a SHA does not prove
publication.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from types import ModuleType


CONTRACT_PATH = "distribution/release-identity.json"
VERSION_PATH = "components/agent-core/.automation/VERSION"
PAYLOAD_PATH = "components/agent-core"
IDENTITY_MODULE_PATH = "tools/agent_core_release_identity.py"
CHECKER_PATH = "tools/check_agent_core_release_identity.py"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
UNSAFE_LOCAL_CONFIG_RE = re.compile(
    r"(?:include(?:if)?\..*|filter\..*|"
    r"core\.(?:gitproxy|fsmonitor|hookspath|pager|sshcommand|worktree)|"
    r"remote\..+\.(?:proxy|proxyauthmethod|receivepack|uploadpack|vcs))",
    re.IGNORECASE,
)


class CheckError(RuntimeError):
    """A fail-closed release identity check error."""


def _trusted_git() -> Path:
    candidate = shutil.which("git")
    if not candidate:
        raise CheckError("trusted git executable is unavailable")
    try:
        executable = Path(candidate).resolve(strict=True)
        metadata = executable.stat()
    except OSError as exc:
        raise CheckError("trusted git executable is unavailable") from exc
    if (
        executable.name != "git"
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise CheckError(f"git executable is not root-owned and immutable: {executable}")
    return executable


def _git_environment() -> dict[str, str]:
    # Use an allowlist, discarding every inherited Git variable (including
    # object/index overrides), ambient config paths, and loader variables.
    return {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(root: Path, arguments: list[str]) -> bytes:
    command = [
        str(_trusted_git()),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.pager=",
        "--no-pager",
        "--no-replace-objects",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=_git_environment(),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"Git command could not be completed: {arguments[0]}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise CheckError(
            f"Git {arguments[0]} failed"
            + (f": {detail}" if detail else f" (exit {result.returncode})")
        )
    return result.stdout


def _one_line(raw: bytes, label: str) -> str:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise CheckError(f"Git returned a malformed {label}")
    try:
        return raw[:-1].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise CheckError(f"Git returned a malformed {label}") from exc


def _resolve_commit(root: Path, revision: str) -> str:
    resolved = _one_line(
        _git(root, ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"]),
        "commit identity",
    )
    if SHA_RE.fullmatch(resolved) is None:
        raise CheckError("repository must use full lowercase 40-hex Git commit identities")
    return resolved


def _assert_repository_root(root: Path) -> None:
    top = _one_line(_git(root, ["rev-parse", "--show-toplevel"]), "repository root")
    try:
        actual_root = Path(top).resolve(strict=True)
    except OSError as exc:
        raise CheckError("Git repository root is unavailable") from exc
    if actual_root != root.resolve(strict=True):
        raise CheckError("checker must run from its own Templates repository root")
    inside = _one_line(_git(root, ["rev-parse", "--is-inside-work-tree"]), "worktree state")
    if inside != "true":
        raise CheckError("checker requires a Git worktree")


def _assert_safe_git_configuration(root: Path) -> None:
    raw_names = _git(
        root,
        ["config", "--local", "--no-includes", "--null", "--name-only", "--list"],
    )
    try:
        names = [name.decode("ascii") for name in raw_names.split(b"\0") if name]
    except UnicodeDecodeError as exc:
        raise CheckError("local Git configuration contains a non-ASCII key") from exc
    for name in names:
        if UNSAFE_LOCAL_CONFIG_RE.fullmatch(name):
            raise CheckError(f"unsafe local Git configuration is forbidden: {name}")

    if any(name.lower() == "extensions.worktreeconfig" for name in names):
        value = _one_line(
            _git(root, ["config", "--local", "--bool", "--get", "extensions.worktreeConfig"]),
            "worktree config setting",
        )
        if value != "true":
            raise CheckError("extensions.worktreeConfig must be a valid true boolean")
        raw_worktree_names = _git(
            root,
            ["config", "--worktree", "--no-includes", "--null", "--name-only", "--list"],
        )
        try:
            worktree_names = [
                name.decode("ascii") for name in raw_worktree_names.split(b"\0") if name
            ]
        except UnicodeDecodeError as exc:
            raise CheckError("worktree Git configuration contains a non-ASCII key") from exc
        for name in worktree_names:
            if UNSAFE_LOCAL_CONFIG_RE.fullmatch(name):
                raise CheckError(f"unsafe worktree Git configuration is forbidden: {name}")


def _assert_clean(root: Path) -> None:
    status = _git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
    )
    if status:
        raise CheckError("Templates source worktree is not clean")


def _tracked_blob(root: Path, revision: str, path: str) -> bytes:
    listing = _git(root, ["ls-tree", "-r", "-z", "--full-tree", revision, "--", path])
    records = listing.split(b"\0")
    if len(records) != 2 or records[1] != b"":
        raise CheckError(f"tracked path is missing or ambiguous at {revision}: {path}")
    try:
        metadata, listed_path = records[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CheckError(f"tracked path has an invalid Git entry: {path}") from exc
    if (
        listed_path != path.encode("ascii")
        or mode != "100644"
        or object_type != "blob"
        or SHA_RE.fullmatch(object_id) is None
    ):
        raise CheckError(f"tracked path must be a regular non-executable file: {path}")
    return _git(root, ["cat-file", "blob", object_id])


def _payload_tree(root: Path, revision: str) -> str:
    tree = _one_line(
        _git(
            root,
            [
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}:{PAYLOAD_PATH}",
            ],
        ),
        "Agent Core payload tree identity",
    )
    if SHA_RE.fullmatch(tree) is None:
        raise CheckError("Agent Core payload must use a full lowercase 40-hex Git tree identity")
    object_type = _one_line(_git(root, ["cat-file", "-t", tree]), "payload object type")
    if object_type != "tree":
        raise CheckError("canonical components/agent-core payload is not a Git tree")
    return tree


def _live_regular_file(root: Path, relative_path: str) -> bytes:
    path = root / relative_path
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckError(f"live source file must be a regular file: {relative_path}")
        return path.read_bytes()
    except OSError as exc:
        raise CheckError(f"live source file is unavailable: {relative_path}") from exc


def _load_verified_validator(root: Path, head: str) -> tuple[ModuleType, bytes]:
    # Verify both live entry points against the pinned commit before loading any
    # validator code. Execute the exact verified Git blob bytes (rather than
    # loader-selected bytecode or a later filesystem read) to avoid TOCTOU and
    # stale __pycache__ execution.
    checker_blob = _tracked_blob(root, head, CHECKER_PATH)
    validator_blob = _tracked_blob(root, head, IDENTITY_MODULE_PATH)
    if _live_regular_file(root, CHECKER_PATH) != checker_blob:
        raise CheckError("live release identity checker does not match the pinned HEAD blob")
    if _live_regular_file(root, IDENTITY_MODULE_PATH) != validator_blob:
        raise CheckError("live release identity validator does not match the pinned HEAD blob")

    module_path = root / IDENTITY_MODULE_PATH
    module_name = "_verified_agent_core_release_identity"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None:
        raise CheckError("could not create an import specification for the verified validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        code = compile(validator_blob, str(module_path), "exec")
        exec(code, module.__dict__)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise CheckError("verified release identity validator could not be loaded") from exc
    return module, checker_blob


def _contract_and_marker(
    root: Path, revision: str, validator: ModuleType
) -> tuple[dict[str, object], str]:
    contract_bytes = _tracked_blob(root, revision, CONTRACT_PATH)
    marker_bytes = _tracked_blob(root, revision, VERSION_PATH)
    try:
        marker = marker_bytes.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise CheckError(f"installed Agent Core marker is not ASCII at {revision}") from exc
    # VERSION is a one-line file in both the existing integer generation and
    # the future stable runtime; its terminating newline is not version text.
    if marker.endswith("\n") and marker.count("\n") == 1:
        marker = marker[:-1]
    try:
        contract = validator.parse_contract(contract_bytes)
    except validator.ReleaseIdentityError as exc:
        raise CheckError(f"release identity contract is invalid at {revision}: {exc}") from exc
    return contract, marker


def _prior_identity(
    root: Path, revision: str, validator: ModuleType
) -> dict[str, str]:
    contract, marker = _contract_and_marker(root, revision, validator)
    agent_core = contract["agentCore"]
    templates = contract["templates"]
    assert isinstance(agent_core, dict) and isinstance(templates, dict)
    # The published tag points at the immutable candidate commit. A later
    # status-only commit cannot retroactively change that tagged Git object.
    if agent_core["status"] not in {"candidate", "released"}:
        raise CheckError("previous release contract must be candidate or released")
    if marker != agent_core["version"]:
        raise CheckError("previous installed Agent Core marker does not match its contract version")
    payload = _payload_tree(root, revision)
    return {
        "agentCoreVersion": str(agent_core["version"]),
        "agentCorePayload": payload,
        "templatesVersion": str(templates["version"]),
        "templatesSourceRevision": revision,
    }


def _validate(args: argparse.Namespace) -> dict[str, object]:
    script_path = Path(__file__).resolve(strict=True)
    root = script_path.parent.parent
    _assert_repository_root(root)
    _assert_safe_git_configuration(root)
    _assert_clean(root)

    head = _resolve_commit(root, "HEAD")
    _assert_clean(root)
    validator, checker_blob = _load_verified_validator(root, head)

    prior: dict[str, str] | None = None
    prior_revision: str | None = None
    if args.previous_release_source_revision is not None:
        prior_revision = args.previous_release_source_revision
        if SHA_RE.fullmatch(prior_revision) is None:
            raise CheckError(
                "previous release source revision must be an exact lowercase full 40-hex commit SHA"
            )
        resolved_prior = _resolve_commit(root, prior_revision)
        if resolved_prior != prior_revision:
            raise CheckError("previous release source revision is not the exact commit SHA supplied")
        prior = _prior_identity(root, prior_revision, validator)

    payload = _payload_tree(root, head)
    contract, marker = _contract_and_marker(root, head, validator)
    try:
        identity = validator.evaluate_release(
            contract,
            marker,
            payload,
            head,
            f"v{contract['templates']['version']}",  # type: ignore[index]
            prior,
        )
    except validator.ReleaseIdentityError as exc:
        raise CheckError(f"release identity check blocked: {exc}") from exc

    # Re-pin the moving ref and repeat all cleanliness/source checks immediately
    # before emitting success; a concurrent HEAD/worktree change fails closed.
    if _resolve_commit(root, "HEAD") != head:
        raise CheckError("Templates HEAD changed during the identity check")
    _assert_clean(root)
    if _live_regular_file(root, CHECKER_PATH) != checker_blob:
        raise CheckError("live release identity checker changed during the identity check")
    if _live_regular_file(root, IDENTITY_MODULE_PATH) != _tracked_blob(
        root, head, IDENTITY_MODULE_PATH
    ):
        raise CheckError("live release identity validator changed during the identity check")

    return {
        "result": "IDENTITY_VALIDATED",
        "identity": identity,
        "previousReleaseSourceRevision": prior_revision,
        "publicationEvidence": (
            "A supplied previous revision is not proof of publication; the caller must "
            "independently verify that it is the previous published release."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "previous_release_source_revision",
        nargs="?",
        help=(
            "exact full SHA of the independently verified previous published release "
            "source revision"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _validate(args)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
