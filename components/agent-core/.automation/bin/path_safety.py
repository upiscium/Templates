"""Canonical classifier for paths that may contain secrets."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


_SUFFIX_PATTERNS = {".pem", ".key"}
_REQUIRED_POLICY_PATTERNS = frozenset(
    {
        ".env",
        "credentials",
        "secret",
        "id_rsa",
        "id_ed25519",
        ".pem",
        ".key",
    }
)
_REQUIRED_PATTERNS = frozenset(
    {
        ".env",
        "credentials",
        "secret",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_xmss",
        ".pem",
        ".key",
    }
)
_OPENSSH_PUBLIC_KEY_NAMES = frozenset(
    {
        "id_rsa.pub",
        "id_dsa.pub",
        "id_ecdsa.pub",
        "id_ecdsa_sk.pub",
        "id_ed25519.pub",
        "id_ed25519_sk.pub",
        "id_xmss.pub",
    }
)
_UNSUPPORTED_PATTERN_CHARACTERS = frozenset("*?[]{}")


def _path_components(path: str) -> tuple[str, ...]:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
    ):
        raise ValueError("secret path must be a non-empty relative POSIX path")
    components = tuple(path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("secret path must be a normalized relative POSIX path")
    return tuple(component.casefold() for component in components)


def _validated_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    if isinstance(patterns, (str, bytes, bytearray)) or not isinstance(
        patterns, Sequence
    ):
        raise ValueError("secret patterns must be a sequence of strings")

    validated: list[str] = []
    for pattern in patterns:
        if (
            not isinstance(pattern, str)
            or not pattern
            or pattern != pattern.strip()
            or "/" in pattern
            or "\\" in pattern
            or "\x00" in pattern
            or any(character in _UNSUPPORTED_PATTERN_CHARACTERS for character in pattern)
            or not any(character.isalnum() for character in pattern)
        ):
            raise ValueError("secret patterns must be non-empty component markers or suffixes")
        validated.append(pattern.casefold())
    if not _REQUIRED_POLICY_PATTERNS.issubset(validated):
        raise ValueError("secret patterns must include the required Agent Core baseline")
    return tuple(sorted(_REQUIRED_PATTERNS.union(validated)))


def load_policy(root: Path) -> dict:
    """Read and validate policy.toml from a stable, no-symlink repository path."""
    policy_path = root / ".automation" / "policy.toml"
    if tomllib is None:
        raise ValueError("Python 3.11+ is required to parse policy.toml")
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise ValueError("secure policy file loading is unavailable")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        automation_fd = os.open(".automation", directory_flags, dir_fd=root_fd)
        descriptors.append(automation_fd)
        policy_fd = os.open("policy.toml", file_flags, dir_fd=automation_fd)
        descriptors.append(policy_fd)
        before = os.fstat(policy_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"policy is not a singly linked regular file: {policy_path}")
        with os.fdopen(policy_fd, "rb", closefd=False) as stream:
            contents = stream.read()
        after = os.fstat(policy_fd)
        identity = lambda metadata: (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        if identity(before) != identity(after) or len(contents) != after.st_size:
            raise ValueError(f"policy changed while being read: {policy_path}")
        try:
            policy = tomllib.loads(contents.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"policy is invalid: {policy_path}") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"missing policy: {policy_path}") from exc
    except OSError as exc:
        raise ValueError(f"policy is unavailable or unsafe: {policy_path}") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    if not isinstance(policy, dict):
        raise ValueError(f"policy is invalid: {policy_path}")
    paths = policy.get("paths")
    patterns = paths.get("secret_patterns") if isinstance(paths, dict) else None
    try:
        _validated_patterns(patterns)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid secret path classifier configuration: {exc}") from exc
    return policy


def _contains_token(component: str, token: str) -> bool:
    start = 0
    while True:
        index = component.find(token, start)
        if index < 0:
            return False
        end = index + len(token)
        left_boundary = index == 0 or not component[index - 1].isalnum()
        right_boundary = end == len(component) or not component[end].isalnum()
        if left_boundary and right_boundary:
            return True
        start = index + 1


def matches_secret_path(path: str, patterns: Sequence[str]) -> bool:
    """Match secret markers by path component, using case-insensitive rules.

    ``.env`` blocks the exact basename and every ``.env.<suffix>`` basename,
    except exact ``.env.example``; ``.envrc`` is not part of that family.
    ``.pem`` and ``.key`` are filename suffixes. Every other configured entry
    is a whole-token marker in a component, bounded by non-alphanumeric
    characters (including underscores). Only exact OpenSSH ``id_*.pub``
    basenames are exempt from private-key markers. Malformed paths or patterns
    raise ``ValueError`` so callers cannot silently interpret invalid policy as
    safe.
    """
    components = _path_components(path)
    validated = _validated_patterns(patterns)

    for index, component in enumerate(components):
        if index == len(components) - 1 and component in _OPENSSH_PUBLIC_KEY_NAMES:
            continue
        for pattern in validated:
            if pattern == ".env":
                if component == ".env" or (
                    component.startswith(".env.")
                    and (component != ".env.example" or index != len(components) - 1)
                ):
                    return True
            elif pattern in _SUFFIX_PATTERNS:
                if component.endswith(pattern):
                    return True
            elif _contains_token(component, pattern):
                return True
    return False
