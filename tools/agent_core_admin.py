#!/usr/bin/env python3
"""Source-side administration for an explicitly approved Agent Core 1.0.0 payload.

This is a callable library, not a trusted launcher.  Every mutating entrypoint
requires a caller-supplied authorization callback.  Target identity is obtained
only through ``agent_core_admin_identity``; task cutovers additionally require
that module's live Issue/PR binding interface and distinct injected Task and
metadata-readiness callbacks.  Test callers may use lambdas for these injected
callbacks; no production launcher is provided.  The payload format is a closed
``manifest.json`` object with exactly ``version``, ``source_revision`` and
``files`` fields.  Each file entry names one regular file by exact relative
path, SHA-256 and Git-style mode (0644 or 0755).

The engine never stages, commits, pushes, changes refs, or removes anything
outside the exact payload/prior inventory path sets.  It does not discard a
consumer's unrelated working-tree, index, or untracked changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import threading
from typing import Any, Iterator


VERSION_BYTES = b"1.0.0\n"
MANIFEST_NAME = "manifest.json"
PRIVATE_DIRECTORY = "agent-core-admin"
PRIVATE_FORMAT = 1
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_OPERATION_RE = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_MODES = {0o644, 0o755}
_PRIVATE_RECORD_LIMIT = 32 * 1024 * 1024
_MANAGED_FILE_LIMIT = 8 * 1024 * 1024
_MANAGED_PATH_LIMIT = 256
_MANAGED_BYTES_LIMIT = 32 * 1024 * 1024
_HISTORY_RECORD_LIMIT = 8192
_HISTORY_BYTES_LIMIT = 64 * 1024 * 1024
_INDEX_BYTES_LIMIT = 32 * 1024 * 1024
_ADMIN_FS_API: object | None = None
_ADMIN_FS_API_LOCK = threading.Lock()
_DYNAMIC_IMPORT_LOCK = sys.__dict__.setdefault(
    "_agent_core_admin_dynamic_import_lock", threading.Lock()
)
_ROOT_MANAGED = {"AGENTS.md", "Justfile", "opencode.json"}
_PROTECTED_AUTOMATION_PATHS = {
    ".automation/ADAPTER",
    ".automation/INIT.fragment.md",
    ".automation/adoption.toml",
}
_INTENT_FIELDS = {
    "format",
    "operation_id",
    "target",
    "request",
    "payload",
    "prior_generation",
    "before",
    "after",
    "temporary_paths",
}
_INSTALLED_FIELDS = {
    "format",
    "state",
    "generation",
    "source_revision",
    "manifest_sha256",
    "files",
}
_COMPLETION_FIELDS = {
    "format",
    "operation_id",
    "status",
}


class AdminError(RuntimeError):
    """An unsafe, ambiguous, or unsupported administration request."""


@dataclass(frozen=True)
class _PayloadFile:
    path: str
    content: bytes
    sha256: str
    mode: int

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {"kind": "file", "sha256": self.sha256, "mode": self.mode}


@dataclass(frozen=True)
class _Payload:
    directory: Path
    manifest_sha256: str
    source_revision: str
    files: dict[str, _PayloadFile]


@dataclass
class _PathBudget:
    """Bound one managed or payload tree before any file bytes are read."""

    paths: int = 0
    bytes: int = 0

    def add_path(
        self,
        relative: str,
        metadata: os.stat_result,
        *,
        include_file_bytes: bool,
    ) -> None:
        self.paths += 1
        if self.paths > _MANAGED_PATH_LIMIT:
            raise AdminError(
                f"managed or payload tree exceeds {_MANAGED_PATH_LIMIT} paths: {relative}"
            )
        if stat.S_ISREG(metadata.st_mode) and include_file_bytes:
            self.add_file_bytes(relative, metadata)

    def add_file_bytes(self, relative: str, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            return
        if metadata.st_size > _MANAGED_FILE_LIMIT:
            raise AdminError(f"file exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}")
        self.bytes += metadata.st_size
        if self.bytes > _MANAGED_BYTES_LIMIT:
            raise AdminError(
                f"managed or payload tree exceeds the {_MANAGED_BYTES_LIMIT}-byte aggregate limit"
            )


@dataclass(frozen=True)
class _Request:
    operation: str
    root: Path
    repository: str
    branch: str
    head: str
    payload: _Payload | None
    legacy_inventory: dict[str, dict[str, Any]]
    issue: int | None
    pr: int | None
    expected_base: str | None
    task_binding: Callable[[object, int, int], bool] | None
    metadata_binding: Callable[[object, int, int], bool] | None

    @property
    def spec(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target": {
                "root": str(self.root),
                "repository": self.repository,
                "branch": self.branch,
                "head": self.head,
            },
            "payload": (
                None
                if self.payload is None
                else {
                    "manifest_sha256": self.payload.manifest_sha256,
                    "source_revision": self.payload.source_revision,
                }
            ),
            "legacy_inventory": self.legacy_inventory,
            "issue": self.issue,
            "pr": self.pr,
            "expected_base": self.expected_base,
        }


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AdminError("cannot encode a canonical administration record") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(content: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AdminError(f"invalid {what}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdminError(f"{what} must be a JSON object")
    return value


def _absolute_path(path: Path, what: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise AdminError(f"{what} must be an absolute Path")
    normalized = Path(os.path.normpath(os.fspath(path)))
    if normalized != path:
        raise AdminError(f"{what} must use an exact normalized path")
    return path


def _path_without_symlink_components(path: Path, what: str) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise AdminError(f"cannot safely inspect {what}: {cursor}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AdminError(f"{what} contains a symlink: {cursor}")


def _check_mount_root(path: Path, metadata: os.stat_result, what: str) -> None:
    parent = path.parent
    try:
        parent_metadata = parent.stat()
    except OSError as exc:
        raise AdminError(f"cannot inspect parent of {what}: {parent}") from exc
    if metadata.st_dev != parent_metadata.st_dev:
        raise AdminError(f"{what} is a mount boundary")
    if _is_mount_point(path):
        raise AdminError(f"{what} is a mount boundary")


def _is_mount_point(path: Path) -> bool:
    if os.path.ismount(path):
        return True
    if not sys.platform.startswith("linux"):
        return False
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_bytes().splitlines()
    except OSError as exc:
        raise AdminError("cannot prove mount topology from /proc/self/mountinfo") from exc
    escaped = re.compile(rb"\\([0-7]{3})")
    requested = os.fsencode(path)
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            raise AdminError("mount topology record is incomplete")
        raw_mountpoint = fields[4]
        decoded = escaped.sub(lambda match: bytes((int(match.group(1), 8),)), raw_mountpoint)
        if decoded == requested:
            return True
    return False


def _validate_relative_path(value: object, *, managed_only: bool = True) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AdminError(f"invalid exact relative path: {value!r}")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise AdminError(f"relative path is not valid UTF-8: {value!r}") from exc
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AdminError(f"relative path contains a control character: {value!r}")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or len(pure.parts) > _MANAGED_PATH_LIMIT
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise AdminError(f"relative path is not normalized: {value!r}")
    if managed_only and not _is_managed_path(value):
        raise AdminError(f"path is outside the exact Agent Core managed surface: {value}")
    return value


def _is_managed_path(value: str) -> bool:
    if value in _ROOT_MANAGED:
        return True
    if value.startswith(".automation/"):
        return not any(
            value == protected or value.startswith(protected + "/")
            for protected in _PROTECTED_AUTOMATION_PATHS
        )
    return value.startswith(".opencode/")


def _check_path_collisions(paths: set[str]) -> None:
    folded: dict[str, str] = {}
    ordered = sorted(paths)
    for value in ordered:
        key = value.casefold()
        previous = folded.get(key)
        if previous is not None and previous != value:
            raise AdminError(f"case-colliding managed paths: {previous} and {value}")
        folded[key] = value
    for value in ordered:
        prefix = value + "/"
        descendant = next((candidate for candidate in ordered if candidate.startswith(prefix)), None)
        if descendant is not None:
            raise AdminError(f"managed path collision: {value} and {descendant}")


def _validate_hash(value: object, what: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AdminError(f"{what} must be 64 lowercase hexadecimal characters")
    return value


def _validate_revision(value: object, what: str) -> str:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise AdminError(f"{what} must be a full lowercase Git object ID")
    return value


def _regular_file_bytes(
    root: Path,
    relative: str,
    root_device: int,
    *,
    allowed_modes: set[int] | None = None,
    require_single_link: bool = True,
    budget: _PathBudget | None = None,
) -> tuple[bytes, int]:
    parts = PurePosixPath(relative).parts
    parent_fd, leaf = _open_parent(root, parts, root_device, create=False)
    if parent_fd is None:
        raise AdminError(f"missing regular file: {relative}")
    try:
        try:
            before_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise AdminError(f"cannot inspect regular file: {relative}") from exc
        if not stat.S_ISREG(before_path.st_mode):
            raise AdminError(f"path is not a regular file: {relative}")
        if before_path.st_dev != root_device:
            raise AdminError(f"file crosses a mount boundary: {relative}")
        if before_path.st_size > _MANAGED_FILE_LIMIT:
            raise AdminError(
                f"file exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
            )
        if budget is not None:
            budget.add_file_bytes(relative, before_path)
        if require_single_link and before_path.st_nlink != 1:
            raise AdminError(f"file has an unexpected hard-link collision: {relative}")
        mode = stat.S_IMODE(before_path.st_mode)
        if allowed_modes is not None and mode not in allowed_modes:
            raise AdminError(f"file has an unsupported mode at {relative}: {mode:o}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise AdminError(f"cannot safely open regular file: {relative}") from exc
        try:
            before_fd = os.fstat(file_fd)
            if not stat.S_ISREG(before_fd.st_mode) or _stat_identity(before_fd) != _stat_identity(before_path):
                raise AdminError(f"file identity changed while opening: {relative}")
            if before_fd.st_size > _MANAGED_FILE_LIMIT:
                raise AdminError(
                f"file exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
                )
            chunks: list[bytes] = []
            total = 0
            while total < before_fd.st_size:
                block = os.read(file_fd, min(1024 * 1024, before_fd.st_size - total))
                if not block:
                    raise AdminError(f"file shortened while reading: {relative}")
                chunks.append(block)
                total += len(block)
            after_fd = os.fstat(file_fd)
            try:
                after_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise AdminError(f"file disappeared while reading: {relative}") from exc
            if (
                _stat_identity(before_fd) != _stat_identity(after_fd)
                or _stat_identity(after_fd) != _stat_identity(after_path)
            ):
                raise AdminError(f"file identity changed while reading: {relative}")
            return b"".join(chunks), mode
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
    )


def _open_root(root: Path, root_device: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = root.lstat()
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise AdminError(f"cannot safely open target directory: {root}") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != root_device
        or _stat_identity(before) != _stat_identity(opened)
    ):
        os.close(descriptor)
        raise AdminError(f"target directory identity changed: {root}")
    return descriptor


def _open_parent(
    root: Path,
    parts: tuple[str, ...],
    root_device: int,
    *,
    create: bool,
) -> tuple[int | None, str]:
    if not parts:
        raise AdminError("managed file path is empty")
    descriptor = _open_root(root, root_device)
    try:
        for index, component in enumerate(parts[:-1]):
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None, parts[-1]
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise AdminError(f"cannot create exact managed parent: {component}") from exc
                try:
                    before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise AdminError(f"cannot inspect created managed parent: {component}") from exc
            except OSError as exc:
                raise AdminError(f"cannot inspect managed parent: {component}") from exc
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise AdminError(f"managed parent is not a real directory: {component}")
            if before.st_dev != root_device:
                raise AdminError(f"managed parent crosses a mount boundary: {component}")
            current_path = root.joinpath(*parts[: index + 1])
            if _is_mount_point(current_path):
                raise AdminError(f"managed parent is a mount boundary: {current_path}")
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise AdminError(f"cannot safely open managed parent: {component}") from exc
            opened = os.fstat(child)
            if _stat_identity(before) != _stat_identity(opened):
                os.close(child)
                raise AdminError(f"managed parent identity changed: {component}")
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _fingerprint(content: bytes, mode: int) -> dict[str, Any]:
    return {
        "kind": "file",
        "sha256": hashlib.sha256(content).hexdigest(),
        "mode": mode,
    }


def _observe_path(
    root: Path,
    relative: str,
    root_device: int,
    *,
    budget: _PathBudget | None = None,
) -> dict[str, Any] | None:
    if _is_mount_point(root / relative):
        raise AdminError(f"managed path is a mount boundary: {relative}")
    parts = PurePosixPath(relative).parts
    parent_fd, leaf = _open_parent(root, parts, root_device, create=False)
    if parent_fd is None:
        return None
    try:
        try:
            metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AdminError(f"cannot inspect managed path: {relative}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AdminError(f"managed path is a symlink: {relative}")
        if not stat.S_ISREG(metadata.st_mode):
            raise AdminError(f"managed path is not a regular file: {relative}")
        if metadata.st_dev != root_device:
            raise AdminError(f"managed file crosses a mount boundary: {relative}")
        if metadata.st_size > _MANAGED_FILE_LIMIT:
            raise AdminError(
                f"managed file exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
            )
        if budget is not None:
            budget.add_path(relative, metadata, include_file_bytes=True)
        if metadata.st_nlink != 1:
            raise AdminError(f"managed file has a hard-link collision: {relative}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode not in _ALLOWED_MODES:
            raise AdminError(f"managed file has an unsupported mode at {relative}: {mode:o}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise AdminError(f"cannot safely open managed file: {relative}") from exc
        try:
            opened = os.fstat(descriptor)
            if _stat_identity(metadata) != _stat_identity(opened):
                raise AdminError(f"managed file identity changed while opening: {relative}")
            if opened.st_size > _MANAGED_FILE_LIMIT:
                raise AdminError(
                    f"managed file exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
                )
            digest = hashlib.sha256()
            total = 0
            while total < opened.st_size:
                block = os.read(descriptor, min(1024 * 1024, opened.st_size - total))
                if not block:
                    raise AdminError(f"managed file shortened while reading: {relative}")
                digest.update(block)
                total += len(block)
            after_fd = os.fstat(descriptor)
            after_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _stat_identity(opened) != _stat_identity(after_fd)
                or _stat_identity(after_fd) != _stat_identity(after_path)
            ):
                raise AdminError(f"managed file identity changed while reading: {relative}")
            return {"kind": "file", "sha256": digest.hexdigest(), "mode": mode}
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _scan_directory_fd(
    root: Path,
    descriptor: int,
    prefix: str,
    root_device: int,
    files: dict[str, dict[str, Any]],
    directories: set[str],
    ignored_paths: set[str],
    budget: _PathBudget,
) -> None:
    try:
        entries = os.scandir(descriptor)
    except OSError as exc:
        raise AdminError(f"cannot inventory managed directory: {prefix}") from exc
    try:
        with entries:
            for entry in entries:
                if not entry.name or entry.name in {".", ".."} or "/" in entry.name or "\\" in entry.name:
                    raise AdminError(f"unsafe name in managed directory: {prefix}")
                relative = f"{prefix}/{entry.name}"
                try:
                    relative.encode("utf-8", "strict")
                    metadata = entry.stat(follow_symlinks=False)
                except (OSError, UnicodeEncodeError) as exc:
                    raise AdminError(f"cannot safely inspect managed path: {relative!r}") from exc
                is_regular = stat.S_ISREG(metadata.st_mode)
                protected_file = relative in _PROTECTED_AUTOMATION_PATHS
                budget.add_path(
                    relative,
                    metadata,
                    include_file_bytes=is_regular and not protected_file,
                )
                if relative in ignored_paths:
                    if metadata.st_dev != root_device or _is_mount_point(root / relative):
                        raise AdminError(f"operation temporary crosses a mount boundary: {relative}")
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise AdminError(f"managed path is a symlink: {relative}")
                if metadata.st_dev != root_device:
                    raise AdminError(f"managed path crosses a mount boundary: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(relative)
                    if relative in _PROTECTED_AUTOMATION_PATHS:
                        raise AdminError(f"protected identity path is not a regular file: {relative}")
                    if _is_mount_point(root / relative):
                        raise AdminError(f"managed directory is a mount boundary: {relative}")
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    try:
                        child = os.open(entry.name, flags, dir_fd=descriptor)
                    except OSError as exc:
                        raise AdminError(f"cannot safely open managed directory: {relative}") from exc
                    try:
                        if _stat_identity(metadata) != _stat_identity(os.fstat(child)):
                            raise AdminError(f"managed directory identity changed: {relative}")
                        _scan_directory_fd(
                            root,
                            child,
                            relative,
                            root_device,
                            files,
                            directories,
                            ignored_paths,
                            budget,
                        )
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise AdminError(f"managed path is a special file: {relative}")
                if _is_mount_point(root / relative):
                    raise AdminError(f"managed file is a mount boundary: {relative}")
                if metadata.st_nlink != 1:
                    raise AdminError(f"managed path has a hard-link collision: {relative}")
                if relative in _PROTECTED_AUTOMATION_PATHS:
                    continue
                if not _is_managed_path(relative):
                    raise AdminError(f"unexpected path in managed directory: {relative}")
                state = _observe_path(root, relative, root_device)
                if state is None:
                    raise AdminError(f"managed file disappeared during inventory: {relative}")
                files[relative] = state
    except OSError as exc:
        raise AdminError(f"cannot inventory managed directory: {prefix}") from exc


def _scan_managed_paths(
    root: Path,
    root_device: int,
    *,
    ignored_paths: set[str] | None = None,
    allowed_directories: set[str] | None = None,
    additional_allowed_directories: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    budget = _PathBudget()
    ignored_paths = set() if ignored_paths is None else ignored_paths
    allowed_directories = set() if allowed_directories is None else allowed_directories
    additional_allowed_directories = (
        set() if additional_allowed_directories is None else additional_allowed_directories
    )
    root_fd = _open_root(root, root_device)
    try:
        for directory_name in (".automation", ".opencode"):
            try:
                metadata = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AdminError(f"cannot inspect managed directory: {directory_name}") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AdminError(f"managed namespace is not a real directory: {directory_name}")
            if metadata.st_dev != root_device:
                raise AdminError(f"managed namespace crosses a mount boundary: {directory_name}")
            if _is_mount_point(root / directory_name):
                raise AdminError(f"managed namespace is a mount boundary: {directory_name}")
            budget.add_path(directory_name, metadata, include_file_bytes=False)
            directories.add(directory_name)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(directory_name, flags, dir_fd=root_fd)
            except OSError as exc:
                raise AdminError(f"cannot safely open managed directory: {directory_name}") from exc
            try:
                if _stat_identity(metadata) != _stat_identity(os.fstat(descriptor)):
                    raise AdminError(f"managed directory identity changed: {directory_name}")
                _scan_directory_fd(
                    root,
                    descriptor,
                    directory_name,
                    root_device,
                    result,
                    directories,
                    ignored_paths,
                    budget,
                )
            finally:
                os.close(descriptor)
        for relative in sorted(_ROOT_MANAGED):
            state = _observe_path(root, relative, root_device, budget=budget)
            if state is not None:
                result[relative] = state
    finally:
        os.close(root_fd)
    expected_directories = set()
    for relative in result:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    ignored_parents: set[str] = set()
    for relative in ignored_paths:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            ignored_parents.add(parent.as_posix())
            parent = parent.parent
    expected_directories.update(directories & ignored_parents)
    expected_directories.update(
        relative for relative in directories if relative in allowed_directories
    )
    expected_directories.update(
        relative for relative in directories if relative in additional_allowed_directories
    )
    expected_directories.update(directories & {".automation", ".opencode"})
    if directories != expected_directories:
        unexpected = sorted(directories - expected_directories)
        missing = sorted(expected_directories - directories)
        details = []
        if unexpected:
            details.append("unexpected directories: " + ", ".join(unexpected))
        if missing:
            details.append("missing directories: " + ", ".join(missing))
        raise AdminError("managed directory entry set differs from exact inventory; " + "; ".join(details))
    _check_path_collisions(set(result))
    return result


def _scan_payload_files(directory: Path, root_device: int) -> tuple[set[str], set[str]]:
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    budget = _PathBudget()
    root_fd = _open_root(directory, root_device)

    def walk(descriptor: int, prefix: str) -> None:
        try:
            entries = os.scandir(descriptor)
        except OSError as exc:
            raise AdminError(f"cannot inventory payload directory: {prefix or directory}") from exc
        try:
            with entries:
                for entry in entries:
                    name = entry.name
                    if not name or name in {".", ".."} or "/" in name or "\\" in name:
                        raise AdminError("payload contains an unsafe path component")
                    relative = f"{prefix}/{name}" if prefix else name
                    try:
                        relative.encode("utf-8", "strict")
                        metadata = entry.stat(follow_symlinks=False)
                    except (OSError, UnicodeEncodeError) as exc:
                        raise AdminError(f"cannot safely inspect payload path: {relative!r}") from exc
                    budget.add_path(
                        relative,
                        metadata,
                        include_file_bytes=stat.S_ISREG(metadata.st_mode),
                    )
                    if stat.S_ISLNK(metadata.st_mode):
                        raise AdminError(f"payload contains a symlink: {relative}")
                    if metadata.st_dev != root_device:
                        raise AdminError(f"payload crosses a mount boundary: {relative}")
                    if stat.S_ISDIR(metadata.st_mode):
                        if _is_mount_point(directory / relative):
                            raise AdminError(f"payload directory is a mount boundary: {relative}")
                        actual_directories.add(relative)
                        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                        try:
                            child = os.open(name, flags, dir_fd=descriptor)
                        except OSError as exc:
                            raise AdminError(f"cannot safely open payload directory: {relative}") from exc
                        try:
                            if _stat_identity(metadata) != _stat_identity(os.fstat(child)):
                                raise AdminError(f"payload directory identity changed: {relative}")
                            walk(child, relative)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(metadata.st_mode):
                        if _is_mount_point(directory / relative):
                            raise AdminError(f"payload file is a mount boundary: {relative}")
                        if metadata.st_nlink != 1:
                            raise AdminError(f"payload file has a hard-link collision: {relative}")
                        actual_files.add(relative)
                    else:
                        raise AdminError(f"payload contains a special file: {relative}")
        except OSError as exc:
            raise AdminError(f"cannot inventory payload directory: {prefix or directory}") from exc

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    return actual_files, actual_directories


def _load_payload(
    payload_directory: Path,
    expected_manifest_sha256: str,
    expected_source_revision: str,
) -> _Payload:
    directory = _absolute_path(payload_directory, "payload directory")
    _path_without_symlink_components(directory, "payload directory")
    try:
        root_metadata = directory.lstat()
    except OSError as exc:
        raise AdminError("payload directory is unavailable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AdminError("payload root must be a real directory")
    _check_mount_root(directory, root_metadata, "payload root")
    expected_manifest_sha256 = _validate_hash(expected_manifest_sha256, "expected manifest SHA-256")
    expected_source_revision = _validate_revision(expected_source_revision, "expected source revision")
    root_device = root_metadata.st_dev
    actual_files, actual_directories = _scan_payload_files(directory, root_device)
    if MANIFEST_NAME not in actual_files:
        raise AdminError("payload is missing its closed manifest.json")
    payload_read_budget = _PathBudget()
    manifest_bytes, _ = _regular_file_bytes(
        directory,
        MANIFEST_NAME,
        root_device,
        allowed_modes={0o644},
        budget=payload_read_budget,
    )
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_hash != expected_manifest_sha256:
        raise AdminError("payload manifest SHA-256 differs from the caller's approved digest")
    manifest = _decode_json(manifest_bytes, "payload manifest")
    if set(manifest) != {"version", "source_revision", "files"}:
        raise AdminError("payload manifest must contain exactly version, source_revision, and files")
    if manifest["version"] != "1.0.0":
        raise AdminError("payload manifest version must be exactly 1.0.0")
    source_revision = _validate_revision(manifest["source_revision"], "manifest source revision")
    if source_revision != expected_source_revision:
        raise AdminError("payload source revision differs from the caller's expected revision")
    raw_files = manifest["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise AdminError("payload manifest files must be a non-empty explicit path list")
    if len(raw_files) + 1 > _MANAGED_PATH_LIMIT:
        raise AdminError(
            f"payload manifest exceeds the {_MANAGED_PATH_LIMIT}-path aggregate limit"
        )
    files: dict[str, _PayloadFile] = {}
    for index, entry in enumerate(raw_files):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "mode"}:
            raise AdminError(f"payload files[{index}] must contain exactly path, sha256, and mode")
        path = _validate_relative_path(entry["path"])
        digest = _validate_hash(entry["sha256"], f"payload files[{index}] SHA-256")
        mode = entry["mode"]
        if type(mode) is not int or mode not in _ALLOWED_MODES:
            raise AdminError(f"payload files[{index}] mode must be integer 0644 or 0755")
        if path in files:
            raise AdminError(f"payload manifest repeats path: {path}")
        content, actual_mode = _regular_file_bytes(
            directory,
            path,
            root_device,
            allowed_modes=_ALLOWED_MODES,
            budget=payload_read_budget,
        )
        if hashlib.sha256(content).hexdigest() != digest or actual_mode != mode:
            raise AdminError(f"payload file bytes or mode differ from the manifest: {path}")
        files[path] = _PayloadFile(path, content, digest, mode)
    _check_path_collisions(set(files))
    if ".automation/VERSION" not in files:
        raise AdminError("Agent Core payload must explicitly list .automation/VERSION")
    version_file = files[".automation/VERSION"]
    if version_file.content != VERSION_BYTES or version_file.mode != 0o644:
        raise AdminError("payload .automation/VERSION must be exactly 1.0.0 followed by LF")
    expected_files = set(files) | {MANIFEST_NAME}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        unexpected = sorted(actual_files - expected_files)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise AdminError("payload directory differs from its closed manifest; " + "; ".join(details))
    expected_directories = {
        parent.as_posix()
        for path in files
        for parent in reversed(PurePosixPath(path).parents)
        if parent.as_posix() != "."
    }
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        details = []
        if missing:
            details.append("missing directories: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected directories: " + ", ".join(unexpected))
        raise AdminError("payload directory structure differs from its closed manifest; " + "; ".join(details))
    return _Payload(directory, manifest_hash, source_revision, files)


def _normalize_legacy_inventory(value: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AdminError("legacy inventory must be an explicit path-to-fingerprint mapping")
    if len(value) > _MANAGED_PATH_LIMIT:
        raise AdminError(f"legacy inventory exceeds the {_MANAGED_PATH_LIMIT}-path limit")
    result: dict[str, dict[str, Any]] = {}
    for raw_path, entry in value.items():
        path = _validate_relative_path(raw_path)
        if not isinstance(entry, Mapping) or set(entry) != {"sha256", "mode"}:
            raise AdminError(f"legacy inventory entry for {path} must contain exactly sha256 and mode")
        digest = _validate_hash(entry["sha256"], f"legacy inventory SHA-256 for {path}")
        mode = entry["mode"]
        if type(mode) is not int or mode not in _ALLOWED_MODES:
            raise AdminError(f"legacy inventory mode for {path} must be integer 0644 or 0755")
        if path in result:
            raise AdminError(f"legacy inventory repeats path: {path}")
        result[path] = {"kind": "file", "sha256": digest, "mode": mode}
    _check_path_collisions(set(result))
    if ".automation/VERSION" not in result:
        raise AdminError("legacy cutover inventory must explicitly include .automation/VERSION")
    return dict(sorted(result.items()))


def _make_request(
    operation: str,
    target_root: Path,
    *,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
    payload_directory: Path | None,
    expected_manifest_sha256: str | None,
    expected_source_revision: str | None,
    legacy_inventory: Mapping[str, Any] | None,
    issue: int | None,
    pr: int | None,
    expected_base: str | None,
    task_binding: Callable[[object, int, int], bool] | None,
    metadata_binding: Callable[[object, int, int], bool] | None,
) -> _Request:
    if operation not in {"install", "replace", "uninstall", "cutover"}:
        raise AdminError("operation must be install, replace, uninstall, or cutover")
    root = _absolute_path(target_root, "target root")
    _path_without_symlink_components(root, "target root")
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise AdminError("target root is unavailable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AdminError("target must be an exact real Git worktree root")
    _check_mount_root(root, root_metadata, "target root")
    if not isinstance(expected_repository, str) or _REPOSITORY_RE.fullmatch(expected_repository) is None:
        raise AdminError("expected repository must be an exact owner/name")
    if not isinstance(expected_branch, str) or not expected_branch or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in expected_branch
    ):
        raise AdminError("expected branch must be a non-empty exact branch name")
    expected_head = _validate_revision(expected_head, "expected target HEAD")
    if type(issue) is not int and issue is not None:
        raise AdminError("Issue number must be a positive integer")
    if type(pr) is not int and pr is not None:
        raise AdminError("PR number must be a positive integer")
    if issue is not None and issue <= 0 or pr is not None and pr <= 0:
        raise AdminError("Issue and PR numbers must be positive integers")
    if (issue is None) != (pr is None):
        raise AdminError("cutover binding requires both Issue and PR numbers")
    if expected_base is not None and (
        not isinstance(expected_base, str) or not expected_base or "\0" in expected_base
    ):
        raise AdminError("expected base branch is invalid")
    if task_binding is not None and not callable(task_binding):
        raise AdminError("task_binding must be a callable or None")
    if metadata_binding is not None and not callable(metadata_binding):
        raise AdminError("metadata_binding must be a callable or None")
    if task_binding is not None and task_binding is metadata_binding:
        raise AdminError("task_binding and metadata_binding must be distinct callbacks")
    if operation != "cutover" and any(
        value is not None
        for value in (issue, pr, expected_base, task_binding, metadata_binding)
    ):
        raise AdminError("Issue/PR binding is only accepted for cutover")
    if operation == "cutover":
        if issue is None and any(
            value is not None for value in (expected_base, task_binding, metadata_binding)
        ):
            raise AdminError("cutover callbacks and expected_base require both Issue and PR numbers")
        if issue is not None:
            if expected_base is None:
                raise AdminError("cutover Issue/PR binding requires expected_base")
            if task_binding is None or metadata_binding is None:
                raise AdminError("cutover Issue/PR binding requires task_binding and metadata_binding")
    if operation == "cutover":
        normalized_legacy = _normalize_legacy_inventory(legacy_inventory)
    else:
        if legacy_inventory:
            raise AdminError("legacy inventory is only accepted for cutover")
        normalized_legacy = {}
    if operation in {"install", "replace", "cutover"}:
        if payload_directory is None or expected_manifest_sha256 is None or expected_source_revision is None:
            raise AdminError(f"{operation} requires payload directory, manifest digest, and source revision")
        payload = _load_payload(
            payload_directory,
            expected_manifest_sha256,
            expected_source_revision,
        )
    else:
        if any(value is not None for value in (payload_directory, expected_manifest_sha256, expected_source_revision)):
            raise AdminError("uninstall does not accept a payload, manifest digest, or source revision")
        payload = None
    return _Request(
        operation,
        root,
        expected_repository,
        expected_branch,
        expected_head,
        payload,
        normalized_legacy,
        issue,
        pr,
        expected_base,
        task_binding,
        metadata_binding,
    )


def _identity_api():
    path = Path(__file__).with_name("agent_core_admin_identity.py")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError("verified agent_core_admin_identity interface is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AdminError("agent_core_admin_identity interface must be a regular non-symlink file")
    name = "_agent_core_admin_identity_for_admin"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AdminError("cannot load agent_core_admin_identity interface")
    with _DYNAMIC_IMPORT_LOCK:
        previous = sys.modules.get(name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise AdminError(f"cannot load agent_core_admin_identity interface: {exc}") from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    required = ("observe_target", "verify_cutover_binding", "AdminIdentityError")
    if any(not hasattr(module, attribute) for attribute in required):
        raise AdminError("agent_core_admin_identity interface is incomplete")
    return module


def _admin_fs_api():
    """Load the mandatory fd-relative renameat2 interface from this checkout."""
    global _ADMIN_FS_API
    if _ADMIN_FS_API is not None:
        return _ADMIN_FS_API
    with _ADMIN_FS_API_LOCK:
        if _ADMIN_FS_API is not None:
            return _ADMIN_FS_API
        _ADMIN_FS_API = _load_admin_fs_api()
        return _ADMIN_FS_API


def _load_admin_fs_api():
    path = Path(__file__).with_name("agent_core_admin_fs.py")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError("verified agent_core_admin_fs interface is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AdminError("agent_core_admin_fs interface must be a regular non-symlink file")
    name = "_agent_core_admin_fs_for_admin"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AdminError("cannot load agent_core_admin_fs interface")
    with _DYNAMIC_IMPORT_LOCK:
        previous = sys.modules.get(name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise AdminError(f"cannot load agent_core_admin_fs interface: {exc}") from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    if any(not hasattr(module, attribute) for attribute in ("move_noreplace", "AdminFsError")):
        raise AdminError("agent_core_admin_fs interface is incomplete")
    return module


def _facts_path(facts: object, field: str) -> Path:
    try:
        value = getattr(facts, field)
        path = Path(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdminError(f"target identity is missing {field}") from exc
    _absolute_path(path, f"target identity {field}")
    _path_without_symlink_components(path, f"target identity {field}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError(f"target identity {field} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AdminError(f"target identity {field} is not a real directory")
    return path


def _observe_identity(request: _Request, identity: object) -> object:
    try:
        facts = identity.observe_target(
            request.root,
            expected_repository=request.repository,
            expected_branch=request.branch,
            expected_head=request.head,
        )
    except Exception as exc:
        if isinstance(exc, AdminError):
            raise
        raise AdminError(f"target identity verification failed: {exc}") from exc
    try:
        facts_root = Path(facts.root)
        repository = facts.repository
        branch = facts.branch
        head = facts.head
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdminError("target identity returned incomplete TargetFacts") from exc
    if facts_root != request.root or facts_root.resolve(strict=True) != request.root:
        raise AdminError("target identity did not confirm the exact expected root")
    if repository != request.repository or branch != request.branch or head != request.head:
        raise AdminError("target identity differs from expected repository, branch, or HEAD")
    root_device = request.root.stat().st_dev
    for field in ("git_dir", "common_dir"):
        path = _facts_path(facts, field)
        if path.stat().st_dev != root_device:
            raise AdminError(f"target identity {field} crosses a mount boundary")
    return facts


def _task_context(request: _Request, facts: object, root_device: int) -> bool:
    branch = getattr(facts, "branch", "")
    # v3 Tasks were not required to use one particular branch prefix. Only
    # the canonical default branch may be considered idle without a Task
    # binding; all other branches require exact Issue/PR evidence.
    if branch != "main":
        return True
    state_path = request.root / ".task-state"
    try:
        metadata = state_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdminError("cannot inspect Task State while binding cutover") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AdminError("Task State path is a symlink")
    if metadata.st_dev != root_device:
        raise AdminError("Task State path crosses a mount boundary")
    if stat.S_ISREG(metadata.st_mode):
        return True
    if not stat.S_ISDIR(metadata.st_mode):
        raise AdminError("Task State path is not a regular file or directory")
    try:
        with os.scandir(state_path) as entries:
            observed = list(entries)
    except OSError as exc:
        raise AdminError("cannot inspect Task State entries") from exc
    for entry in observed:
        try:
            child = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise AdminError("cannot safely inspect Task State entry") from exc
        if stat.S_ISLNK(child.st_mode) or not (
            stat.S_ISREG(child.st_mode) or stat.S_ISDIR(child.st_mode)
        ):
            raise AdminError("Task State contains a symlink or special file")
        if child.st_dev != root_device or _is_mount_point(state_path / entry.name):
            raise AdminError("Task State entry crosses a mount boundary")
    return bool(observed)


def _observe_bound_identity_snapshot(
    request: _Request,
    identity: object,
) -> tuple[object, bytes | None]:
    facts = _observe_identity(request, identity)
    if request.operation != "cutover":
        return facts, None
    root_device = request.root.stat().st_dev
    active_task_or_ambiguity = _task_context(request, facts, root_device)
    if request.issue is None:
        if active_task_or_ambiguity:
            raise AdminError(
                "cutover on an in-flight Task or Task branch requires a verified Issue/PR binding"
            )
        return facts, None
    if request.expected_base is None or request.task_binding is None or request.metadata_binding is None:
        raise AdminError("active cutover requires expected_base, task_binding, and metadata_binding")
    try:
        binding = identity.verify_cutover_binding(
            facts,
            issue=request.issue,
            pr=request.pr,
            expected_base=request.expected_base,
            task_binding=request.task_binding,
        )
    except Exception as exc:
        raise AdminError(f"verified Task cutover binding failed: {exc}") from exc
    if not isinstance(binding, dict):
        raise AdminError("verified Task cutover binding returned no binding facts")
    try:
        metadata_matches = request.metadata_binding(facts, request.issue, request.pr)
    except Exception as exc:
        raise AdminError(f"verified v4 metadata readiness binding failed: {exc}") from exc
    if metadata_matches is not True:
        raise AdminError("v4 metadata readiness binding did not return exactly True")
    return facts, _canonical_json({"binding": binding, "metadata_ready": metadata_matches})


def _observe_bound_identity(request: _Request, identity: object) -> object:
    facts, _ = _observe_bound_identity_snapshot(request, identity)
    return facts


def _validate_fingerprint(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"kind", "sha256", "mode"}:
        raise AdminError(f"invalid file fingerprint in {what}")
    if value["kind"] != "file":
        raise AdminError(f"invalid file kind in {what}")
    digest = _validate_hash(value["sha256"], f"file fingerprint in {what}")
    mode = value["mode"]
    if type(mode) is not int or mode not in _ALLOWED_MODES:
        raise AdminError(f"invalid file mode in {what}")
    return {"kind": "file", "sha256": digest, "mode": mode}


def _validate_file_map(value: object, what: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise AdminError(f"{what} must be an object")
    if len(value) > _MANAGED_PATH_LIMIT:
        raise AdminError(f"{what} exceeds the {_MANAGED_PATH_LIMIT}-path limit")
    result: dict[str, dict[str, Any]] = {}
    for raw_path, fingerprint in value.items():
        path = _validate_relative_path(raw_path)
        if path in result:
            raise AdminError(f"{what} repeats path: {path}")
        result[path] = _validate_fingerprint(fingerprint, f"{what} {path}")
    _check_path_collisions(set(result))
    return dict(sorted(result.items()))


def _validate_installed(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INSTALLED_FIELDS:
        raise AdminError("invalid installed Agent Core identity record")
    if type(value["format"]) is not int or value["format"] != PRIVATE_FORMAT:
        raise AdminError("unsupported installed Agent Core identity record")
    state = value["state"]
    if state not in {"installed", "uninstalled"}:
        raise AdminError("invalid installed Agent Core identity state")
    generation = value["generation"]
    if not isinstance(generation, str) or _OPERATION_RE.fullmatch(generation) is None:
        raise AdminError("invalid installed Agent Core generation")
    source_revision = value["source_revision"]
    manifest_sha256 = value["manifest_sha256"]
    if state == "installed":
        source_revision = _validate_revision(source_revision, "installed source revision")
        manifest_sha256 = _validate_hash(manifest_sha256, "installed manifest SHA-256")
    else:
        if source_revision is not None:
            source_revision = _validate_revision(source_revision, "prior installed source revision")
        if manifest_sha256 is not None:
            manifest_sha256 = _validate_hash(manifest_sha256, "prior installed manifest SHA-256")
    files = _validate_file_map(value["files"], "installed file inventory")
    if state == "installed":
        version = files.get(".automation/VERSION")
        if version != _fingerprint(VERSION_BYTES, 0o644):
            raise AdminError("installed identity does not contain exact Agent Core VERSION 1.0.0")
    elif files:
        raise AdminError("uninstalled identity must have an empty file inventory")
    return {
        "format": PRIVATE_FORMAT,
        "state": state,
        "generation": generation,
        "source_revision": source_revision,
        "manifest_sha256": manifest_sha256,
        "files": files,
    }


def _validate_intent(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INTENT_FIELDS:
        raise AdminError("invalid operation intent record")
    if type(value["format"]) is not int or value["format"] != PRIVATE_FORMAT:
        raise AdminError("unsupported operation intent record")
    operation_id = value["operation_id"]
    if not isinstance(operation_id, str) or _OPERATION_RE.fullmatch(operation_id) is None:
        raise AdminError("invalid operation intent identifier")
    target = value["target"]
    if not isinstance(target, dict) or set(target) != {
        "root", "git_dir", "common_dir", "repository", "branch", "head"
    }:
        raise AdminError("invalid operation target identity")
    for field in ("root", "git_dir", "common_dir"):
        raw_path = target[field]
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise AdminError(f"invalid operation target {field}")
        if os.path.normpath(raw_path) != raw_path:
            raise AdminError(f"operation target {field} is not normalized")
    if not isinstance(target["repository"], str) or _REPOSITORY_RE.fullmatch(target["repository"]) is None:
        raise AdminError("invalid operation target repository")
    if not isinstance(target["branch"], str) or not target["branch"]:
        raise AdminError("invalid operation target branch")
    _validate_revision(target["head"], "operation target HEAD")
    request_spec = value["request"]
    if not isinstance(request_spec, dict) or set(request_spec) != {
        "operation", "target", "payload", "legacy_inventory", "issue", "pr", "expected_base"
    }:
        raise AdminError("invalid operation request specification")
    request_target = request_spec["target"]
    if request_target != {
        "root": target["root"],
        "repository": target["repository"],
        "branch": target["branch"],
        "head": target["head"],
    }:
        raise AdminError("operation request differs from its exact target identity")
    if request_spec["operation"] not in {"install", "replace", "uninstall", "cutover"}:
        raise AdminError("invalid operation intent kind")
    payload = request_spec["payload"]
    if payload is not None:
        if not isinstance(payload, dict) or set(payload) != {"manifest_sha256", "source_revision"}:
            raise AdminError("invalid operation payload identity")
        _validate_hash(payload["manifest_sha256"], "operation manifest SHA-256")
        _validate_revision(payload["source_revision"], "operation source revision")
    if value["payload"] != payload:
        raise AdminError("operation payload differs from its request")
    if not isinstance(request_spec["legacy_inventory"], dict):
        raise AdminError("invalid operation legacy inventory")
    if request_spec["issue"] is not None and (
        type(request_spec["issue"]) is not int or request_spec["issue"] <= 0
    ):
        raise AdminError("invalid operation Issue binding")
    if request_spec["pr"] is not None and (
        type(request_spec["pr"]) is not int or request_spec["pr"] <= 0
    ):
        raise AdminError("invalid operation PR binding")
    expected_base = request_spec["expected_base"]
    if expected_base is not None and (
        not isinstance(expected_base, str) or not expected_base or "\0" in expected_base
    ):
        raise AdminError("invalid operation expected base branch")
    if (request_spec["issue"] is None) != (request_spec["pr"] is None):
        raise AdminError("operation Issue/PR binding is incomplete")
    if request_spec["issue"] is not None and expected_base is None:
        raise AdminError("operation Issue/PR binding has no expected base branch")
    if request_spec["operation"] != "cutover" and any(
        value is not None
        for value in (request_spec["issue"], request_spec["pr"], expected_base)
    ):
        raise AdminError("non-cutover operation contains Issue/PR binding facts")
    prior_generation = value["prior_generation"]
    if prior_generation is not None and (
        not isinstance(prior_generation, str)
        or _OPERATION_RE.fullmatch(prior_generation) is None
    ):
        raise AdminError("invalid prior installed generation")
    before = _validate_state_map(value["before"], "operation before state")
    after = _validate_state_map(value["after"], "operation after state")
    if set(before) != set(after):
        raise AdminError("operation before/after path inventories differ")
    temporary_paths = value["temporary_paths"]
    if not isinstance(temporary_paths, dict):
        raise AdminError("invalid operation temporary path inventory")
    expected_temporaries = {
        path: _temporary_path(operation_id, path)
        for path in before
        if after[path] is not None and before[path] != after[path]
    }
    if temporary_paths != expected_temporaries:
        raise AdminError("operation temporary path inventory differs from exact managed paths")
    identity_material = {
        "target": target,
        "request": request_spec,
        "prior_generation": prior_generation,
    }
    if hashlib.sha256(_canonical_json(identity_material)).hexdigest() != operation_id:
        raise AdminError("operation intent identifier differs from its exact target and generation")
    return {
        "format": PRIVATE_FORMAT,
        "operation_id": operation_id,
        "target": target,
        "request": request_spec,
        "payload": payload,
        "prior_generation": prior_generation,
        "before": before,
        "after": after,
        "temporary_paths": expected_temporaries,
    }


def _validate_state_map(value: object, what: str) -> dict[str, dict[str, Any] | None]:
    if not isinstance(value, dict):
        raise AdminError(f"{what} must be an object")
    if len(value) > _MANAGED_PATH_LIMIT:
        raise AdminError(f"{what} exceeds the {_MANAGED_PATH_LIMIT}-path limit")
    result: dict[str, dict[str, Any] | None] = {}
    for raw_path, state in value.items():
        path = _validate_relative_path(raw_path)
        result[path] = None if state is None else _validate_fingerprint(state, f"{what} {path}")
    _check_path_collisions(set(result))
    return dict(sorted(result.items()))


def _temporary_path(operation_id: str, relative: str) -> str:
    parent = PurePosixPath(relative).parent
    name = f".agent-core-admin-{operation_id[:16]}-{hashlib.sha256(relative.encode()).hexdigest()[:16]}.tmp"
    result = name if parent.as_posix() == "." else f"{parent.as_posix()}/{name}"
    return _validate_relative_path(result, managed_only=False)


def _read_operation_temporary(
    root: Path, relative: str, root_device: int
) -> tuple[bytes, int] | None:
    if _is_mount_point(root / relative):
        raise AdminError(f"operation temporary file is a mount boundary: {relative}")
    parent_fd, leaf = _open_parent(root, PurePosixPath(relative).parts, root_device, create=False)
    if parent_fd is None:
        return None
    try:
        try:
            before_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AdminError(f"cannot inspect operation temporary file: {relative}") from exc
        if not stat.S_ISREG(before_path.st_mode):
            raise AdminError(f"operation temporary path is not a regular file: {relative}")
        if before_path.st_size > _MANAGED_FILE_LIMIT:
            raise AdminError(
                f"operation temporary exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
            )
        if (
            before_path.st_dev != root_device
            or before_path.st_nlink != 1
            or before_path.st_uid != os.geteuid()
        ):
            raise AdminError(f"operation temporary file has an unsafe device or hard-link: {relative}")
        mode = stat.S_IMODE(before_path.st_mode)
        if mode not in {0o600, *_ALLOWED_MODES}:
            raise AdminError(f"operation temporary file has an unexpected mode: {relative}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise AdminError(f"cannot safely open operation temporary file: {relative}") from exc
        try:
            before_fd = os.fstat(descriptor)
            if not stat.S_ISREG(before_fd.st_mode) or _stat_identity(before_fd) != _stat_identity(before_path):
                raise AdminError(f"operation temporary file identity changed: {relative}")
            if before_fd.st_size > _MANAGED_FILE_LIMIT:
                raise AdminError(
                    f"operation temporary exceeds the {_MANAGED_FILE_LIMIT}-byte limit: {relative}"
                )
            chunks: list[bytes] = []
            total = 0
            while total < before_fd.st_size:
                block = os.read(descriptor, min(1024 * 1024, before_fd.st_size - total))
                if not block:
                    raise AdminError(f"operation temporary shortened while reading: {relative}")
                chunks.append(block)
                total += len(block)
            after_fd = os.fstat(descriptor)
            try:
                after_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise AdminError(f"operation temporary file disappeared while reading: {relative}") from exc
            if (
                _stat_identity(before_fd) != _stat_identity(after_fd)
                or _stat_identity(after_fd) != _stat_identity(after_path)
            ):
                raise AdminError(f"operation temporary file identity changed while reading: {relative}")
            return b"".join(chunks), mode
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _operation_temporary_state(
    root: Path,
    relative: str,
    root_device: int,
    content: bytes,
    mode: int,
) -> str | None:
    staged = _read_operation_temporary(root, relative, root_device)
    if staged is None:
        return None
    staged_content, staged_mode = staged
    if staged_content == content and staged_mode == mode:
        return "complete"
    if staged_mode == 0o600 and content.startswith(staged_content):
        return "partial"
    raise AdminError(
        f"operation temporary file cannot be safely recovered (not an approved payload prefix): {relative}"
    )


def _private_directory(path: Path, root_device: int, *, create: bool) -> bool:
    created = False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir(mode=0o700)
            created = True
            _sync_directory(path.parent)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AdminError(f"cannot create Git-private directory: {path}") from exc
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise AdminError(f"cannot inspect created Git-private directory: {path}") from exc
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private directory: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AdminError(f"Git-private path is not a real directory: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot safely open Git-private directory: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(metadata) != _stat_identity(opened):
            raise AdminError(f"Git-private directory identity changed: {path}")
        if opened.st_dev != root_device or opened.st_uid != os.geteuid():
            raise AdminError(f"Git-private directory has unsafe device or ownership: {path}")
        if stat.S_IMODE(opened.st_mode) != 0o700:
            if not created:
                raise AdminError(f"Git-private directory must have mode 0700: {path}")
            os.fchmod(descriptor, 0o700)
        if _is_mount_point(path):
            raise AdminError(f"Git-private directory is a mount boundary: {path}")
        current = path.lstat()
        if _stat_identity(current) != _stat_identity(os.fstat(descriptor)):
            raise AdminError(f"Git-private directory identity changed: {path}")
    finally:
        os.close(descriptor)
    return True


def _private_layout(facts: object, root_device: int, *, create: bool) -> dict[str, Path] | None:
    git_dir = _facts_path(facts, "git_dir")
    base = git_dir / PRIVATE_DIRECTORY
    exists = _private_directory(base, root_device, create=create)
    if not exists:
        return None
    operations = base / "operations"
    intents = operations / "intents"
    receipts = operations / "receipts"
    if create:
        _private_directory(operations, root_device, create=True)
        _private_directory(intents, root_device, create=True)
        _private_directory(receipts, root_device, create=True)
    else:
        if not _private_directory(operations, root_device, create=False):
            raise AdminError("Git-private operation directory is incomplete")
        if not _private_directory(intents, root_device, create=False):
            raise AdminError("Git-private intent directory is incomplete")
        if not _private_directory(receipts, root_device, create=False):
            raise AdminError("Git-private receipt directory is incomplete")
    layout = {
        "base": base,
        "operations": operations,
        "intents": intents,
        "receipts": receipts,
        "installed": base / "installed.json",
        "lock": base / "lock",
    }
    if create:
        _ensure_admin_lock_file(layout, root_device)
    return layout


def _private_file_metadata(path: Path, root_device: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private record: {path}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != root_device
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise AdminError(f"Git-private record has unsafe type, mode, device, or ownership: {path}")
    return metadata


def _ensure_admin_lock_file(layout: dict[str, Path], root_device: int) -> None:
    path = layout["lock"]
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        metadata = _private_file_metadata(path, root_device)
        if metadata.st_size != 0:
            raise AdminError(f"Git-private administration lock is not empty: {path}")
        return
    except OSError as exc:
        raise AdminError(f"cannot create Git-private administration lock: {path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != root_device
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise AdminError(f"Git-private administration lock has unsafe identity: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _private_file_metadata(path, root_device)
    _sync_directory(path.parent)


def _private_file_identity(
    path: Path,
    root_device: int,
    *,
    missing_ok: bool = False,
) -> tuple[int, ...] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise AdminError(f"missing Git-private record: {path}")
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private record: {path}") from exc
    return _stat_identity(_private_file_metadata(path, root_device))


def _private_directory_identity(path: Path, root_device: int) -> tuple[int, ...]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private operation directory: {path}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_dev != root_device
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        raise AdminError(f"Git-private operation directory has unsafe identity: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot safely open Git-private operation directory: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(opened):
            raise AdminError(f"Git-private operation directory identity changed: {path}")
        current = path.lstat()
        if _stat_identity(opened) != _stat_identity(current):
            raise AdminError(f"Git-private operation directory changed while observing: {path}")
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private operation directory: {path}") from exc
    finally:
        os.close(descriptor)


def _operation_directory_identity(
    layout: dict[str, Path], root_device: int
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    return (
        # _ensure_admin_lock_file ran before the preflight snapshot, so taking
        # its nonblocking flock cannot alter this namespace. Include base
        # mtime/ctime to detect unexpected installed/temp entries, too.
        _private_directory_identity(layout["base"], root_device),
        _private_directory_identity(layout["operations"], root_device),
        _private_directory_identity(layout["intents"], root_device),
        _private_directory_identity(layout["receipts"], root_device),
    )


def _read_private_bytes(path: Path, root_device: int, *, missing_ok: bool = False) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise AdminError(f"missing Git-private record: {path}")
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private record: {path}") from exc
    if before.st_size > _PRIVATE_RECORD_LIMIT:
        raise AdminError(
            f"Git-private record exceeds the {_PRIVATE_RECORD_LIMIT}-byte limit: {path}"
        )
    _private_file_metadata(path, root_device)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot safely open Git-private record: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(opened):
            raise AdminError(f"Git-private record identity changed: {path}")
        if opened.st_size > _PRIVATE_RECORD_LIMIT:
            raise AdminError(
                f"Git-private record exceeds the {_PRIVATE_RECORD_LIMIT}-byte limit: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        while total < opened.st_size:
            block = os.read(descriptor, min(1024 * 1024, opened.st_size - total))
            if not block:
                raise AdminError(f"Git-private record shortened while reading: {path}")
            chunks.append(block)
            total += len(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _stat_identity(opened) != _stat_identity(after) or _stat_identity(after) != _stat_identity(current):
            raise AdminError(f"Git-private record changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_private_json(
    path: Path,
    root_device: int,
    *,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    content = _read_private_bytes(path, root_device, missing_ok=missing_ok)
    if content is None:
        return None
    value = _decode_json(content, f"Git-private record {path.name}")
    if content != _canonical_json(value) + b"\n":
        raise AdminError(f"Git-private record is not in its exact canonical representation: {path}")
    return value


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot open directory for synchronization: {path}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AdminError(f"cannot synchronize directory: {path}") from exc
    finally:
        os.close(descriptor)


def _write_private_exclusive(path: Path, value: dict[str, Any], root_device: int) -> None:
    content = _canonical_json(value) + b"\n"
    if len(content) > _PRIVATE_RECORD_LIMIT:
        raise AdminError(f"Git-private operation record exceeds the supported size: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_private_json(path, root_device)
        if existing != value:
            raise AdminError(f"Git-private operation record already differs: {path}")
        return
    except OSError as exc:
        raise AdminError(f"cannot create Git-private operation record: {path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AdminError(f"short write to Git-private record: {path}")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    else:
        os.close(descriptor)
    _private_file_metadata(path, root_device)
    _sync_directory(path.parent)
    if _read_private_json(path, root_device) != value:
        raise AdminError(f"Git-private record failed post-write validation: {path}")


def _remove_private_temporary(path: Path, root_device: int, expected: bytes) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private temporary before recovery: {path}") from exc
    actual = _read_private_bytes(path, root_device)
    if actual != expected:
        raise AdminError(f"Git-private temporary changed before recovery: {path}")
    try:
        current = path.lstat()
    except OSError as exc:
        raise AdminError(f"Git-private temporary changed before recovery: {path}") from exc
    if _stat_identity(before) != _stat_identity(current):
        raise AdminError(f"Git-private temporary changed before recovery: {path}")
    try:
        path.unlink()
    except OSError as exc:
        raise AdminError(f"cannot remove exact Git-private temporary for recovery: {path}") from exc
    _sync_directory(path.parent)


def _complete_private_prefix(path: Path, root_device: int, expected: bytes) -> bool:
    """Finish only an exact, owner-private prefix at its operation-specific name."""
    if len(expected) > _PRIVATE_RECORD_LIMIT:
        raise AdminError(f"Git-private operation record exceeds the supported size: {path}")
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdminError(f"cannot inspect Git-private temporary for recovery: {path}") from exc
    actual = _read_private_bytes(path, root_device)
    if not expected.startswith(actual):
        raise AdminError(
            f"Git-private temporary cannot be safely recovered (not an approved record prefix): {path}"
        )
    if actual == expected:
        return True
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise AdminError(f"cannot safely open Git-private temporary for recovery: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if _stat_identity(before) != _stat_identity(opened) or _stat_identity(opened) != _stat_identity(current):
            raise AdminError(f"Git-private temporary changed before recovery: {path}")
        view = memoryview(expected[len(actual):])
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AdminError(f"short write while reconciling Git-private temporary: {path}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _read_private_bytes(path, root_device) != expected:
        raise AdminError(f"Git-private temporary failed exact recovery validation: {path}")
    return True


def _write_intent_atomic(
    layout: dict[str, Path],
    intent: dict[str, Any],
    root_device: int,
    *,
    retry: bool,
) -> None:
    operation_id = intent["operation_id"]
    path = layout["intents"] / f"{operation_id}.json"
    temporary = layout["intents"] / f".{operation_id}.tmp"
    content = _canonical_json(intent) + b"\n"
    if len(content) > _PRIVATE_RECORD_LIMIT:
        raise AdminError(f"Git-private operation intent exceeds the supported size: {path}")
    current = _read_private_json(path, root_device, missing_ok=True)
    if current is not None:
        if current != intent:
            raise AdminError(f"Git-private operation intent already differs: {path}")
        if _read_private_bytes(temporary, root_device, missing_ok=True) is not None:
            raise AdminError("published operation intent retains a temporary record")
        return

    staged = _read_private_bytes(temporary, root_device, missing_ok=True)
    if staged is not None:
        if not retry:
            raise AdminError("staged operation intent requires retry with its matching operation ID")
        if not content.startswith(staged):
            raise AdminError(
                f"Git-private intent temporary cannot be safely recovered (not an approved record prefix): {temporary}"
            )
        _complete_private_prefix(temporary, root_device, content)
    else:
        _write_private_exclusive(temporary, intent, root_device)
    if _read_private_bytes(temporary, root_device) != content:
        raise AdminError(f"Git-private intent temporary differs from intended operation: {temporary}")
    if _read_private_json(path, root_device, missing_ok=True) is not None:
        raise AdminError(f"Git-private operation intent appeared before publication: {path}")
    try:
        os.replace(temporary, path)
    except OSError as exc:
        raise AdminError(f"cannot atomically publish Git-private operation intent: {path}") from exc
    _sync_directory(path.parent)
    if _read_private_json(path, root_device) != intent:
        raise AdminError(f"Git-private operation intent failed post-publication validation: {path}")


def _atomic_private_json(
    path: Path,
    temporary: Path,
    value: dict[str, Any],
    root_device: int,
    *,
    expected_current: dict[str, Any] | None,
) -> None:
    content = _canonical_json(value) + b"\n"
    if len(content) > _PRIVATE_RECORD_LIMIT:
        raise AdminError(f"Git-private operation record exceeds the supported size: {path}")
    current = _read_private_json(path, root_device, missing_ok=True)
    if current == value:
        staged = _read_private_bytes(temporary, root_device, missing_ok=True)
        if staged is not None:
            if staged != content and not content.startswith(staged):
                raise AdminError(
                    f"Git-private temporary cannot be safely recovered (not an approved record prefix): {temporary}"
                )
            _complete_private_prefix(temporary, root_device, content)
            _remove_private_temporary(temporary, root_device, content)
        return
    if current != expected_current:
        raise AdminError(f"Git-private identity changed before receipt update: {path}")
    staged = _read_private_bytes(temporary, root_device, missing_ok=True)
    if staged is not None:
        if staged != content and not content.startswith(staged):
            raise AdminError(
                f"Git-private temporary cannot be safely recovered (not an approved record prefix): {temporary}"
            )
        _complete_private_prefix(temporary, root_device, content)
    if staged is None:
        _write_private_exclusive(temporary, value, root_device)
        staged = _read_private_bytes(temporary, root_device)
    else:
        staged = _read_private_bytes(temporary, root_device)
    if staged != content:
        raise AdminError(f"Git-private temporary record differs from intended receipt: {temporary}")
    current = _read_private_json(path, root_device, missing_ok=True)
    if current != expected_current:
        raise AdminError(f"Git-private identity changed before atomic receipt update: {path}")
    try:
        os.replace(temporary, path)
    except OSError as exc:
        raise AdminError(f"cannot install Git-private receipt: {path}") from exc
    _sync_directory(path.parent)
    if _read_private_json(path, root_device) != value:
        raise AdminError(f"Git-private receipt failed post-write validation: {path}")


@contextmanager
def _worktree_lock(layout: dict[str, Path], root_device: int) -> Iterator[None]:
    lock_path = layout["lock"]
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise AdminError(f"preflight Admin lock is unavailable: {lock_path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _stat_identity(opened) != _stat_identity(current)
            or opened.st_dev != root_device
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
        ):
            raise AdminError("Git-private administration lock has unsafe identity")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AdminError(f"BUSY: another Agent Core Admin operation holds {lock_path}") from exc
        except OSError as exc:
            raise AdminError(f"cannot acquire bounded Git-private administration lock: {lock_path}") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _index_snapshot(git_dir: Path) -> tuple[tuple[int, ...], str] | None:
    path = git_dir / "index"
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AdminError("cannot inspect exact per-worktree Git index") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AdminError("per-worktree Git index is not an exact regular file")
    if before.st_size > _INDEX_BYTES_LIMIT:
        raise AdminError("per-worktree Git index exceeds its bounded snapshot limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError("cannot safely open exact per-worktree Git index") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(opened):
            raise AdminError("per-worktree Git index changed while opening")
        if opened.st_size > _INDEX_BYTES_LIMIT:
            raise AdminError("per-worktree Git index exceeds its bounded snapshot limit")
        digest = hashlib.sha256()
        total = 0
        while total < opened.st_size:
            block = os.read(descriptor, min(1024 * 1024, opened.st_size - total))
            if not block:
                raise AdminError("per-worktree Git index shortened while reading")
            digest.update(block)
            total += len(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _stat_identity(opened) != _stat_identity(after) or _stat_identity(after) != _stat_identity(current):
            raise AdminError("per-worktree Git index changed while reading")
        return _stat_identity(after), digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_git_local_file(
    base: Path,
    relative: str,
    root_device: int,
    *,
    missing_ok: bool,
    byte_limit: int,
) -> tuple[bytes, tuple[int, ...]] | None:
    """Read one bounded, exact Git metadata file without following path links."""
    relative = _validate_relative_path(relative, managed_only=False)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_root = base.lstat()
        descriptor = os.open(base, flags)
    except OSError as exc:
        raise AdminError(f"cannot safely open Git metadata directory: {base}") from exc
    opened_root = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened_root.st_mode)
        or _stat_identity(before_root) != _stat_identity(opened_root)
        or opened_root.st_dev != root_device
    ):
        os.close(descriptor)
        raise AdminError(f"Git metadata directory identity changed: {base}")
    parts = PurePosixPath(relative).parts
    current_path = base
    try:
        for component in parts[:-1]:
            current_path = current_path / component
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise AdminError(f"missing Git metadata path: {current_path}")
            except OSError as exc:
                raise AdminError(f"cannot inspect Git metadata path: {current_path}") from exc
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISDIR(before.st_mode)
                or before.st_dev != root_device
                or _is_mount_point(current_path)
            ):
                raise AdminError(f"Git metadata path is not an exact directory: {current_path}")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise AdminError(f"cannot safely open Git metadata path: {current_path}") from exc
            if _stat_identity(before) != _stat_identity(os.fstat(child)):
                os.close(child)
                raise AdminError(f"Git metadata path changed while opening: {current_path}")
            os.close(descriptor)
            descriptor = child

        leaf = parts[-1]
        path = base.joinpath(*parts)
        try:
            before = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AdminError(f"missing Git metadata file: {path}")
        except OSError as exc:
            raise AdminError(f"cannot inspect Git metadata file: {path}") from exc
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_dev != root_device
            or before.st_nlink != 1
            or _is_mount_point(path)
        ):
            raise AdminError(f"Git metadata path is not an exact regular file: {path}")
        if before.st_size > byte_limit:
            raise AdminError(f"Git metadata file exceeds its size limit: {path}")
        try:
            file_descriptor = os.open(
                leaf,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise AdminError(f"cannot safely open Git metadata file: {path}") from exc
        try:
            opened = os.fstat(file_descriptor)
            if _stat_identity(before) != _stat_identity(opened):
                raise AdminError(f"Git metadata file changed while opening: {path}")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(file_descriptor, min(1024 * 1024, byte_limit + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > byte_limit:
                    raise AdminError(f"Git metadata file exceeds its size limit: {path}")
            after = os.fstat(file_descriptor)
            try:
                current = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise AdminError(f"Git metadata file changed while reading: {path}") from exc
            if _stat_identity(opened) != _stat_identity(after) or _stat_identity(after) != _stat_identity(current):
                raise AdminError(f"Git metadata file changed while reading: {path}")
            return b"".join(chunks), _stat_identity(after)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _packed_branch_value(content: bytes, reference: bytes) -> str | None:
    if content and not content.endswith(b"\n"):
        raise AdminError("packed-refs file is not newline terminated")
    found: str | None = None
    for line in content.split(b"\n")[:-1] if content else ():
        if line.startswith(b"#"):
            continue
        if line.startswith(b"^"):
            if re.fullmatch(rb"\^[0-9a-f]{40}|\^[0-9a-f]{64}", line) is None:
                raise AdminError("packed-refs contains a malformed peeled object ID")
            continue
        fields = line.split(b" ")
        if (
            len(fields) != 2
            or re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]) is None
            or not fields[1]
        ):
            raise AdminError("packed-refs contains a malformed reference record")
        if fields[1] == reference:
            if found is not None:
                raise AdminError("packed-refs contains a duplicate target branch")
            found = fields[0].decode("ascii")
    return found


def _local_git_identity(
    request: _Request,
    facts: object,
    root_device: int,
) -> tuple[Any, ...]:
    """Check local HEAD and its exact loose/packed branch ref without Git subprocesses."""
    git_dir = _facts_path(facts, "git_dir")
    common_dir = _facts_path(facts, "common_dir")
    try:
        git_entry = (request.root / ".git").lstat()
    except OSError as exc:
        raise AdminError("local worktree .git registration disappeared") from exc
    if stat.S_ISLNK(git_entry.st_mode) or git_entry.st_dev != root_device:
        raise AdminError("local worktree .git registration is unsafe")
    if stat.S_ISDIR(git_entry.st_mode):
        if git_dir != request.root / ".git" or common_dir != git_dir:
            raise AdminError("local Git directory is not registered to this worktree")
        # Creating our own index/HEAD lock changes the Git directory's mtime,
        # not its worktree registration. Bind its inode/device, not timestamps.
        registration = ("main", git_entry.st_dev, git_entry.st_ino, git_entry.st_mode)
    elif stat.S_ISREG(git_entry.st_mode):
        gitfile = _read_git_local_file(
            request.root, ".git", root_device, missing_ok=False, byte_limit=4096
        )
        reciprocal = _read_git_local_file(
            git_dir, "gitdir", root_device, missing_ok=False, byte_limit=4096
        )
        commondir = _read_git_local_file(
            git_dir, "commondir", root_device, missing_ok=False, byte_limit=4096
        )
        assert gitfile is not None and reciprocal is not None and commondir is not None
        if (
            gitfile[0] != b"gitdir: " + os.fsencode(git_dir) + b"\n"
            or reciprocal[0] != os.fsencode(request.root / ".git") + b"\n"
            or commondir[0] != os.fsencode(os.path.relpath(common_dir, git_dir)) + b"\n"
            or git_dir.parent != common_dir / "worktrees"
        ):
            raise AdminError("local linked-worktree reciprocal registration changed")
        registration = ("linked", gitfile[1], reciprocal[1], commondir[1])
    else:
        raise AdminError("local worktree .git registration is not a directory or Gitfile")
    head_file = _read_git_local_file(
        git_dir, "HEAD", root_device, missing_ok=False, byte_limit=1024
    )
    assert head_file is not None
    head_bytes, head_identity = head_file
    expected_reference = f"refs/heads/{request.branch}"
    try:
        reference_bytes = expected_reference.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise AdminError("expected branch is not valid UTF-8 for a local Git reference") from exc
    if head_bytes != b"ref: " + reference_bytes + b"\n":
        raise AdminError("local Git HEAD does not name the exact expected branch")

    loose_relative = _validate_relative_path(expected_reference, managed_only=False)
    if any(part.endswith(".lock") for part in PurePosixPath(loose_relative).parts):
        raise AdminError("expected branch maps to an unsafe local Git reference path")
    loose = _read_git_local_file(
        common_dir, loose_relative, root_device, missing_ok=True, byte_limit=1024
    )
    if loose is not None:
        ref_bytes, ref_identity = loose
        if re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n", ref_bytes) is None:
            raise AdminError("loose target branch ref is not one exact full object ID")
        object_id = ref_bytes[:-1].decode("ascii")
        ref_state: tuple[Any, ...] = (
            "loose",
            ref_identity,
            hashlib.sha256(ref_bytes).hexdigest(),
        )
    else:
        packed = _read_git_local_file(
            common_dir, "packed-refs", root_device, missing_ok=True,
            byte_limit=_PRIVATE_RECORD_LIMIT,
        )
        if packed is None:
            raise AdminError("target branch has neither a loose nor packed local Git ref")
        packed_bytes, packed_identity = packed
        object_id = _packed_branch_value(packed_bytes, reference_bytes)
        if object_id is None:
            raise AdminError("target branch is missing from local packed-refs")
        ref_state = (
            "packed",
            packed_identity,
            hashlib.sha256(packed_bytes).hexdigest(),
            object_id,
        )
    if len(object_id) != len(request.head) or object_id != request.head:
        raise AdminError("local target branch ref differs from the expected HEAD commit")
    return (
        registration,
        (head_identity, hashlib.sha256(head_bytes).hexdigest()),
        ref_state,
        object_id,
    )


def _remove_git_guard(directory_fd: int, name: str, identity: tuple[int, ...]) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AdminError(f"cannot inspect owned Git writer guard: {name}") from exc
    if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != identity:
        raise AdminError(f"Git writer guard identity changed; refusing to remove {name}")
    try:
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise AdminError(f"cannot remove exact Git writer guard: {name}") from exc


@contextmanager
def _git_writer_fence(
    request: _Request,
    facts: object,
    root_device: int,
    *,
    expected_index: tuple[tuple[int, ...], str] | None,
    expected_managed: dict[str, dict[str, Any]],
    allowed_directories: set[str],
) -> Iterator[tuple[tuple[int, ...], str] | None]:
    """Reserve Git's per-worktree index and HEAD writer lock names in order."""
    git_dir = _facts_path(facts, "git_dir")
    index_before = _index_snapshot(git_dir)
    managed_before = _scan_managed_paths(
        request.root, root_device, allowed_directories=allowed_directories
    )
    if index_before != expected_index:
        raise AdminError("Git index identity changed since authorization preflight")
    if managed_before != expected_managed:
        raise AdminError("managed worktree changed since authorization preflight")
    try:
        git_dir_before = git_dir.lstat()
    except OSError as exc:
        raise AdminError("exact per-worktree Git directory disappeared before locking") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(git_dir, flags)
    except OSError as exc:
        raise AdminError("cannot safely open exact per-worktree Git directory") from exc
    git_dir_opened = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(git_dir_opened.st_mode)
        or _stat_identity(git_dir_before) != _stat_identity(git_dir_opened)
        or git_dir_opened.st_dev != root_device
    ):
        os.close(directory_fd)
        raise AdminError("per-worktree Git directory identity changed before locking")
    guards: list[tuple[str, int, tuple[int, ...]]] = []
    try:
        for name in ("index.lock", "HEAD.lock"):
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError as exc:
                raise AdminError(f"BUSY: Git writer lock collision: {git_dir / name}") from exc
            except OSError as exc:
                raise AdminError(f"cannot acquire exact Git writer guard: {git_dir / name}") from exc
            try:
                os.fchmod(descriptor, 0o600)
                opened = os.fstat(descriptor)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                identity = _stat_identity(opened)
                guards.append((name, descriptor, identity))
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or _stat_identity(opened) != _stat_identity(current)
                    or opened.st_dev != git_dir.stat().st_dev
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_size != 0
                ):
                    raise AdminError(f"Git writer guard has unsafe identity: {git_dir / name}")
            except BaseException:
                if not any(owned_fd == descriptor for _, owned_fd, _ in guards):
                    os.close(descriptor)
                raise

        if _index_snapshot(git_dir) != index_before:
            raise AdminError("Git index identity changed while acquiring writer guards")
        if _stat_identity(git_dir.lstat()) != _stat_identity(os.fstat(directory_fd)):
            raise AdminError("per-worktree Git directory identity changed while acquiring writer guards")
        if _scan_managed_paths(
            request.root, root_device, allowed_directories=allowed_directories
        ) != managed_before:
            raise AdminError("managed worktree changed while acquiring Git writer guards")
        yield index_before
    finally:
        first_error: BaseException | None = None
        for name, descriptor, identity in reversed(guards):
            os.close(descriptor)
            try:
                _remove_git_guard(directory_fd, name, identity)
            except BaseException as exc:
                first_error = first_error or exc
        os.close(directory_fd)
        if first_error is not None:
            raise first_error


def _read_operation_records(
    layout: dict[str, Path], root_device: int
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Path],
]:
    intents: dict[str, dict[str, Any]] = {}
    completions: dict[str, dict[str, Any]] = {}
    intent_temporaries: dict[str, Path] = {}
    record_count = 0
    record_bytes = 0
    for name, directory, destination, validator in (
        ("intent", layout["intents"], intents, _validate_intent),
        ("completion", layout["receipts"], completions, _validate_completion),
    ):
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise AdminError(f"cannot inspect Git-private {name} directory") from exc
        try:
            with entries:
                for entry in entries:
                    record_count += 1
                    try:
                        record_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError as exc:
                        raise AdminError(f"cannot inspect Git-private {name} record size") from exc
                    if record_count > _HISTORY_RECORD_LIMIT or record_bytes > _HISTORY_BYTES_LIMIT:
                        raise AdminError(
                            "Git-private Admin operation history exceeds its bounded read limit; "
                            "explicit ADMIN retention/reconciliation is required"
                        )
                    if name == "intent" and re.fullmatch(r"\.[0-9a-f]{64}\.tmp", entry.name):
                        operation_id = entry.name[1:-4]
                        _private_file_metadata(directory / entry.name, root_device)
                        if operation_id in intent_temporaries:
                            raise AdminError("duplicate Git-private intent temporary for operation")
                        intent_temporaries[operation_id] = directory / entry.name
                        continue
                    if name == "completion" and re.fullmatch(r"\.[0-9a-f]{64}\.tmp", entry.name):
                        _private_file_metadata(directory / entry.name, root_device)
                        continue
                    if not re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                        raise AdminError(f"unexpected entry in Git-private {name} directory: {entry.name}")
                    path = directory / entry.name
                    _private_file_metadata(path, root_device)
                    value = _read_private_json(path, root_device)
                    assert value is not None
                    record = validator(value)
                    operation_id = record["operation_id"]
                    if entry.name != f"{operation_id}.json" or operation_id in destination:
                        raise AdminError(f"Git-private {name} filename does not match record identity")
                    destination[operation_id] = record
        except OSError as exc:
            raise AdminError(f"cannot inspect Git-private {name} directory") from exc
    if set(completions) - set(intents):
        raise AdminError("Git-private operation receipt has no matching intent")
    for operation_id, completion in completions.items():
        intent = intents[operation_id]
        expected_completion = _completion_record(intent)
        if completion != expected_completion:
            raise AdminError("Git-private operation receipt differs from its intent")
    return intents, completions, intent_temporaries


def _validate_completion(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _COMPLETION_FIELDS:
        raise AdminError("invalid operation completion receipt")
    if type(value["format"]) is not int or value["format"] != PRIVATE_FORMAT:
        raise AdminError("unsupported operation completion receipt")
    operation_id = value["operation_id"]
    if not isinstance(operation_id, str) or _OPERATION_RE.fullmatch(operation_id) is None:
        raise AdminError("invalid operation completion identifier")
    if value["status"] != "complete":
        raise AdminError("invalid operation completion status")
    return dict(value)


def _completion_record(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": PRIVATE_FORMAT,
        "operation_id": intent["operation_id"],
        "status": "complete",
    }


def _read_layout_records(
    layout: dict[str, Path] | None,
    root_device: int,
) -> tuple[
    dict[str, Any] | None,
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Path],
]:
    if layout is None:
        return None, {}, {}, {}
    try:
        base_entries = os.scandir(layout["base"])
    except OSError as exc:
        raise AdminError("cannot inspect Git-private administration directory") from exc
    try:
        with base_entries:
            for entry in base_entries:
                if entry.name == "lock":
                    _private_file_metadata(layout["lock"], root_device)
                elif entry.name in {"installed.json", "operations"}:
                    continue
                elif re.fullmatch(r"installed\.[0-9a-f]{64}\.tmp", entry.name):
                    continue
                else:
                    raise AdminError(
                        f"unexpected entry in Git-private administration directory: {entry.name}"
                    )
    except OSError as exc:
        raise AdminError("cannot inspect Git-private administration directory") from exc
    installed_raw = _read_private_json(layout["installed"], root_device, missing_ok=True)
    installed = None if installed_raw is None else _validate_installed(installed_raw)
    try:
        operation_entries = os.scandir(layout["operations"])
    except OSError as exc:
        raise AdminError("cannot inspect Git-private operation directory") from exc
    found: set[str] = set()
    try:
        with operation_entries:
            for entry in operation_entries:
                if entry.name not in {"intents", "receipts"} or entry.name in found:
                    raise AdminError("Git-private operation directory has unexpected entries")
                found.add(entry.name)
    except OSError as exc:
        raise AdminError("cannot inspect Git-private operation directory") from exc
    if found != {"intents", "receipts"}:
        raise AdminError("Git-private operation directory has unexpected entries")
    intents, completions, intent_temporaries = _read_operation_records(layout, root_device)
    return installed, intents, completions, intent_temporaries


def _known_admin_directories(intents: Mapping[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for intent in intents.values():
        for relative in set(intent["before"]) | set(intent["after"]):
            parent = PurePosixPath(relative).parent
            while parent.as_posix() != ".":
                result.add(parent.as_posix())
                parent = parent.parent
    return result


def _installed_after(intent: dict[str, Any]) -> dict[str, Any]:
    operation = intent["request"]["operation"]
    payload = intent["payload"]
    files = {
        path: state
        for path, state in sorted(intent["after"].items())
        if state is not None
    }
    if operation == "uninstall":
        source_revision = None
        manifest_sha256 = None
        state = "uninstalled"
    else:
        if payload is None:
            raise AdminError("operation payload is unavailable for installed identity")
        source_revision = payload["source_revision"]
        manifest_sha256 = payload["manifest_sha256"]
        state = "installed"
    return _validate_installed(
        {
            "format": PRIVATE_FORMAT,
            "state": state,
            "generation": intent["operation_id"],
            "source_revision": source_revision,
            "manifest_sha256": manifest_sha256,
            "files": files,
        }
    )


def _target_identity(request: _Request, facts: object) -> dict[str, str]:
    return {
        "root": str(request.root),
        "git_dir": str(_facts_path(facts, "git_dir")),
        "common_dir": str(_facts_path(facts, "common_dir")),
        "repository": request.repository,
        "branch": request.branch,
        "head": request.head,
    }


def _intent_id(
    request: _Request,
    installed_before: dict[str, Any] | None,
    target: dict[str, str],
) -> str:
    material = {
        "target": target,
        "request": request.spec,
        "prior_generation": None if installed_before is None else installed_before["generation"],
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _build_intent(
    request: _Request,
    root_device: int,
    installed_before: dict[str, Any] | None,
    operation_id: str,
    target: dict[str, str],
    known_directories: set[str],
) -> dict[str, Any]:
    snapshot = _scan_managed_paths(
        request.root, root_device, allowed_directories=known_directories
    )
    if request.operation == "install":
        if request.branch == "main":
            raise AdminError(
                "fresh install requires an exact non-default bootstrap branch; "
                "the #194 publication handoff must commit and PR that branch"
            )
        if installed_before is not None and installed_before["state"] != "uninstalled":
            raise AdminError("Agent Core is already installed; use replace or uninstall")
        if snapshot:
            raise AdminError("fresh install found pre-existing Agent Core-managed files")
        old_files: dict[str, dict[str, Any]] = {}
        assert request.payload is not None
        new_files = {path: item.fingerprint for path, item in request.payload.files.items()}
    elif request.operation == "replace":
        if installed_before is None or installed_before["state"] != "installed":
            raise AdminError("replacement requires a matching prior Admin installed identity")
        old_files = installed_before["files"]
        if snapshot != old_files:
            raise AdminError("prior installed identity differs from live managed paths")
        assert request.payload is not None
        new_files = {path: item.fingerprint for path, item in request.payload.files.items()}
    elif request.operation == "uninstall":
        if installed_before is None or installed_before["state"] != "installed":
            raise AdminError("uninstall requires a matching prior Admin installed identity")
        old_files = installed_before["files"]
        if snapshot != old_files:
            raise AdminError("prior installed identity differs from live managed paths")
        new_files = {}
    else:
        if installed_before is not None:
            raise AdminError("one-way cutover refuses an existing Admin installation identity")
        old_files = request.legacy_inventory
        if snapshot != old_files:
            raise AdminError("live Agent Core paths differ from the explicit legacy prior inventory")
        version_content, version_mode = _regular_file_bytes(
            request.root,
            ".automation/VERSION",
            root_device,
            allowed_modes=_ALLOWED_MODES,
        )
        if version_content not in {b"1\n", b"2\n", b"3\n"} or version_mode not in _ALLOWED_MODES:
            raise AdminError("one-way cutover only accepts exact Agent Core VERSION 1, 2, or 3")
        assert request.payload is not None
        new_files = {path: item.fingerprint for path, item in request.payload.files.items()}

    all_paths = set(old_files) | set(new_files)
    _check_path_collisions(all_paths)
    before: dict[str, dict[str, Any] | None] = {}
    after: dict[str, dict[str, Any] | None] = {}
    for path in sorted(all_paths):
        before[path] = old_files.get(path)
        after[path] = new_files.get(path)
    temporary_paths = {
        path: _temporary_path(operation_id, path)
        for path in sorted(all_paths)
        if after[path] is not None and before[path] != after[path]
    }
    if set(temporary_paths.values()) & all_paths:
        raise AdminError("payload collides with an operation-specific temporary path")
    for temp_path in temporary_paths.values():
        if _operation_temporary_present(request.root, temp_path, root_device):
            raise AdminError(f"operation temporary path already exists: {temp_path}")
    intent = {
        "format": PRIVATE_FORMAT,
        "operation_id": operation_id,
        "target": target,
        "request": request.spec,
        "payload": request.spec["payload"],
        "prior_generation": None if installed_before is None else installed_before["generation"],
        "before": before,
        "after": after,
        "temporary_paths": temporary_paths,
    }
    return _validate_intent(intent)


def _ordered_operation_paths(intent: dict[str, Any]) -> list[str]:
    before = intent["before"]
    after = intent["after"]

    def order(path: str) -> tuple[int, int, str]:
        if path == ".automation/VERSION":
            return (4, 0, path)
        if before[path] is not None and after[path] is None:
            return (0, -path.count("/"), path)
        if before[path] is not None and after[path] is not None:
            return (1, -path.count("/"), path)
        if before[path] is None and after[path] is not None:
            return (2, path.count("/"), path)
        return (3, 0, path)

    return sorted(before, key=order)


def _action(path: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    if before == after:
        kind = "noop"
    elif before is None:
        kind = "create"
    elif after is None:
        kind = "remove"
    else:
        kind = "replace"
    return {"path": path, "action": kind}


def _read_current_installed(layout: dict[str, Path] | None, root_device: int) -> dict[str, Any] | None:
    if layout is None:
        return None
    value = _read_private_json(layout["installed"], root_device, missing_ok=True)
    return None if value is None else _validate_installed(value)


def _validate_current_operation_state(
    request: _Request,
    intent: dict[str, Any],
    layout: dict[str, Path] | None,
    installed: dict[str, Any] | None,
    root_device: int,
    *,
    complete: bool,
    known_directories: set[str] | None = None,
) -> None:
    before = intent["before"]
    after = intent["after"]
    if len(before) > _MANAGED_PATH_LIMIT:
        raise AdminError(f"operation exceeds the {_MANAGED_PATH_LIMIT}-path limit")

    temporary_paths = set(intent["temporary_paths"].values())
    operation_directories: set[str] = set()
    for relative in set(before) | set(after):
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            operation_directories.add(parent.as_posix())
            parent = parent.parent
    snapshot = _scan_managed_paths(
        request.root,
        root_device,
        ignored_paths=temporary_paths,
        allowed_directories=known_directories,
        additional_allowed_directories=operation_directories,
    )
    current = {path: snapshot.get(path) for path in before}
    for path in before:
        if complete:
            if current[path] != after[path]:
                raise AdminError(f"managed path differs from completed operation: {path}")
        elif current[path] not in (before[path], after[path]):
            raise AdminError(f"managed path has an unknown or dirty preimage: {path}")
    allowed = {
        path: state
        for path, state in current.items()
        if state is not None
    }
    if snapshot != allowed:
        unexpected = sorted(set(snapshot) - set(allowed))
        missing = sorted(set(allowed) - set(snapshot))
        details = []
        if unexpected:
            details.append("unexpected managed paths: " + ", ".join(unexpected))
        if missing:
            details.append("missing managed paths: " + ", ".join(missing))
        raise AdminError("managed directory entry set differs from operation state; " + "; ".join(details))

    for path, temporary in intent["temporary_paths"].items():
        payload_file = request.payload.files.get(path) if request.payload is not None else None
        if payload_file is None or payload_file.fingerprint != after[path]:
            raise AdminError(f"approved payload bytes are unavailable for operation temporary: {path}")
        _operation_temporary_state(
            request.root, temporary, root_device, payload_file.content, payload_file.mode
        )
    if complete and any(_operation_temporary_present(request.root, path, root_device) for path in temporary_paths):
        raise AdminError("completed operation retains an operation temporary")

    expected_prior = intent["prior_generation"]
    if installed is not None and installed["generation"] not in {expected_prior, intent["operation_id"]}:
        raise AdminError("installed identity is a third generation during operation")
    expected_after_identity = _installed_after(intent)
    if complete and installed != expected_after_identity:
        raise AdminError("installed identity does not match completed operation")
    if not complete and installed is not None:
        if installed["generation"] == expected_prior:
            expected_before_files = {
                path: state for path, state in intent["before"].items() if state is not None
            }
            if installed["files"] != expected_before_files:
                raise AdminError("prior installed identity differs from exact operation preimage")
        elif installed != expected_after_identity:
            raise AdminError("installed identity changed before operation completion")
    elif not complete and expected_prior is not None:
        raise AdminError("prior installed identity disappeared during operation")


def _staged_index_paths(request: _Request) -> set[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    arguments = [
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
        "diff",
        "--cached",
        "--name-only",
        "--no-renames",
        "-z",
    ]
    try:
        result = subprocess.run(
            arguments,
            cwd=request.root,
            env=environment,
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise AdminError("cannot inspect target Git index for staged-path overlap") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AdminError(
            "cannot inspect target Git index for staged-path overlap"
            + (f": {detail}" if detail else "")
        )
    if result.stdout and not result.stdout.endswith(b"\0"):
        raise AdminError("target Git index path inventory is not NUL-terminated")
    raw_paths = result.stdout.split(b"\0")[:-1] if result.stdout else []
    paths: set[str] = set()
    for raw_path in raw_paths:
        try:
            path = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError:
            # Managed paths are UTF-8. An unrelated non-UTF-8 index path cannot
            # be their exact or directory-prefix collision.
            continue
        if not path or path.startswith("/"):
            raise AdminError("target Git index contains an unsafe staged path")
        paths.add(path)
    return paths


def _index_paths_overlap(left: str, right: str) -> bool:
    left_folded = left.casefold()
    right_folded = right.casefold()
    return (
        left_folded == right_folded
        or left_folded.startswith(right_folded + "/")
        or right_folded.startswith(left_folded + "/")
    )


def _check_index_overlap(request: _Request, intent: dict[str, Any]) -> None:
    changed_paths = {
        path
        for path, before in intent["before"].items()
        if before != intent["after"][path]
    }
    changed_paths.update(intent["temporary_paths"].values())
    if not changed_paths:
        return
    staged_paths = _staged_index_paths(request)
    for operation_path in sorted(changed_paths):
        overlap = next(
            (
                staged_path
                for staged_path in sorted(staged_paths)
                if _index_paths_overlap(operation_path, staged_path)
            ),
            None,
        )
        if overlap is not None:
            raise AdminError(
                "staged index path overlaps an exact Agent Core operation change: "
                f"{overlap} overlaps {operation_path}"
            )


def _resolve_operation(
    request: _Request,
    layout: dict[str, Path] | None,
    root_device: int,
    facts: object,
    *,
    expected_operation_id: str | None = None,
    check_index: bool = True,
    allow_staged_inspection: bool = False,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    target = _target_identity(request, facts)
    installed, intents, completions, intent_temporaries = _read_layout_records(layout, root_device)
    pending = sorted(set(intents) - set(completions))
    if pending:
        if intent_temporaries:
            raise AdminError("Git-private intent temporary conflicts with a published operation intent")
        matching_pending = [
            operation_id
            for operation_id in pending
            if intents[operation_id]["request"] == request.spec
            and intents[operation_id]["target"] == target
        ]
        if len(matching_pending) != 1:
            raise AdminError("another incomplete Agent Core Admin operation must be retried first")
        if (
            expected_operation_id is not None
            and matching_pending[0] != expected_operation_id
        ):
            raise AdminError("expected operation ID does not match the persisted operation intent")
        existing = intents[matching_pending[0]]
        _validate_current_operation_state(
            request,
            existing,
            layout,
            installed,
            root_device,
            complete=False,
            known_directories=_known_admin_directories(intents),
        )
        _validate_private_temporary_state(layout, existing, root_device, completed=False)
        if check_index:
            _check_index_overlap(request, existing)
        return existing, "pending", installed

    matching_completed = [
        operation_id
        for operation_id, intent in intents.items()
        if operation_id in completions
        and intent["request"] == request.spec
        and intent["target"] == target
    ]
    for operation_id in matching_completed:
        if intent_temporaries:
            raise AdminError("Git-private intent temporary conflicts with a published operation intent")
        if expected_operation_id is not None and operation_id != expected_operation_id:
            raise AdminError("expected operation ID does not match the persisted operation intent")
        existing = intents[operation_id]
        if installed == _installed_after(existing):
            _validate_current_operation_state(
                request,
                existing,
                layout,
                installed,
                root_device,
                complete=True,
                known_directories=_known_admin_directories(intents),
            )
            _validate_private_temporary_state(layout, existing, root_device, completed=True)
            if check_index:
                _check_index_overlap(request, existing)
            return existing, "complete", installed

    operation_id = _intent_id(request, installed, target)
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise AdminError("expected operation ID does not match the computed operation")
    if operation_id in intents:
        raise AdminError("prior operation identity exists but no longer matches current target state")
    intent = _build_intent(
        request,
        root_device,
        installed,
        operation_id,
        target,
        _known_admin_directories(intents),
    )
    temporary_id = operation_id if operation_id in intent_temporaries else None
    if intent_temporaries and (
        temporary_id is None
        or len(intent_temporaries) != 1
        or (expected_operation_id != operation_id and not allow_staged_inspection)
    ):
        _validate_private_unknown_entries(layout, root_device)
        raise AdminError("Git-private intent temporary has no matching retry operation ID")
    _validate_private_unknown_entries(
        layout, root_device, allowed_intent_temporary_id=temporary_id
    )
    if temporary_id is not None:
        assert layout is not None
        temporary_content = _canonical_json(intent) + b"\n"
        staged = _read_private_bytes(intent_temporaries[temporary_id], root_device)
        if staged is None or not temporary_content.startswith(staged):
            raise AdminError(
                "Git-private intent temporary cannot be safely recovered (not the exact computed operation prefix)"
            )
    _validate_current_operation_state(
        request,
        intent,
        layout,
        installed,
        root_device,
        complete=False,
        known_directories=_known_admin_directories(intents),
    )
    if check_index:
        _check_index_overlap(request, intent)
    if temporary_id is not None:
        return intent, "intent-staged", installed
    return intent, "ready", installed


def _validate_locked_preflight_snapshot(
    request: _Request,
    layout: dict[str, Path],
    intent: dict[str, Any],
    status: str,
    root_device: int,
    known_directories: set[str],
    *,
    expected_directories: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    expected_installed: dict[str, Any] | None,
    expected_installed_identity: tuple[int, ...] | None,
) -> dict[str, Any] | None:
    if _operation_directory_identity(layout, root_device) != expected_directories:
        raise AdminError("Git-private operation directories changed since preflight")
    try:
        with os.scandir(layout["operations"]) as entries:
            children = set()
            for entry in entries:
                if entry.name not in {"intents", "receipts"}:
                    raise AdminError("Git-private operation directory contains an unknown entry")
                children.add(entry.name)
    except OSError as exc:
        raise AdminError("cannot inspect Git-private operation directory under lock") from exc
    if children != {"intents", "receipts"}:
        raise AdminError("Git-private operation directory is incomplete under lock")

    installed = _read_current_installed(layout, root_device)
    installed_identity = _private_file_identity(
        layout["installed"], root_device, missing_ok=True
    )
    if installed != expected_installed or installed_identity != expected_installed_identity:
        raise AdminError("Git-private installed identity changed since preflight")

    operation_id = intent["operation_id"]
    intent_path = layout["intents"] / f"{operation_id}.json"
    receipt_path = layout["receipts"] / f"{operation_id}.json"
    intent_temporary = layout["intents"] / f".{operation_id}.tmp"
    stored_intent = _read_private_json(intent_path, root_device, missing_ok=True)
    stored_receipt = _read_private_json(receipt_path, root_device, missing_ok=True)
    staged_intent = _read_private_bytes(intent_temporary, root_device, missing_ok=True)
    expected_intent_bytes = _canonical_json(intent) + b"\n"

    if status in {"ready", "intent-staged"}:
        if stored_intent is not None or stored_receipt is not None:
            raise AdminError("current operation records changed since preflight")
        if status == "intent-staged":
            if staged_intent is None or not expected_intent_bytes.startswith(staged_intent):
                raise AdminError("staged operation intent changed since preflight")
        elif staged_intent is not None:
            raise AdminError("an operation intent temporary appeared since preflight")
    elif status == "pending":
        if stored_intent is None or _validate_intent(stored_intent) != intent:
            raise AdminError("persisted operation intent changed since preflight")
        if stored_receipt is not None or staged_intent is not None:
            raise AdminError("pending operation records changed since preflight")
    elif status == "complete":
        if stored_intent is None or _validate_intent(stored_intent) != intent:
            raise AdminError("persisted operation intent changed since preflight")
        expected_receipt = _completion_record(intent)
        if stored_receipt is None or _validate_completion(stored_receipt) != expected_receipt:
            raise AdminError("persisted operation receipt changed since preflight")
        if staged_intent is not None:
            raise AdminError("completed operation retains an intent temporary")
    else:
        raise AdminError(f"unsupported preflight operation status: {status}")

    _validate_private_temporary_state(
        layout,
        intent,
        root_device,
        completed=status == "complete",
        check_directory_entries=False,
    )
    _validate_current_operation_state(
        request,
        intent,
        layout,
        installed,
        root_device,
        complete=status == "complete",
        known_directories=known_directories,
    )
    return installed


def _validate_private_unknown_entries(
    layout: dict[str, Path] | None,
    root_device: int,
    *,
    allowed_intent_temporary_id: str | None = None,
) -> None:
    if layout is None:
        return
    try:
        with os.scandir(layout["base"]) as base_entries:
            for entry in base_entries:
                if entry.name in {"installed.json", "lock", "operations"}:
                    continue
                if re.fullmatch(r"installed\.[0-9a-f]{64}\.tmp", entry.name):
                    raise AdminError("staged installed identity requires its matching retry")
                raise AdminError("unexpected Git-private administration entry")

        allowed_intent_temporary = (
            None
            if allowed_intent_temporary_id is None
            else f".{allowed_intent_temporary_id}.tmp"
        )
        found_intent_temporary = False
        with os.scandir(layout["intents"]) as intent_entries:
            for entry in intent_entries:
                if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                    continue
                if entry.name == allowed_intent_temporary:
                    found_intent_temporary = True
                    continue
                raise AdminError("unexpected or unmatched Git-private intent temporary")
        if allowed_intent_temporary is not None and not found_intent_temporary:
            raise AdminError("matching Git-private intent temporary disappeared")

        with os.scandir(layout["receipts"]) as receipt_entries:
            for entry in receipt_entries:
                if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                    raise AdminError("staged operation receipt requires its matching retry")
    except OSError as exc:
        raise AdminError("cannot inspect Git-private operation entries") from exc


def _validate_private_temporary_state(
    layout: dict[str, Path] | None,
    intent: dict[str, Any],
    root_device: int,
    *,
    completed: bool,
    check_directory_entries: bool = True,
) -> None:
    if layout is None:
        return
    operation_id = intent["operation_id"]
    installed_temp = layout["base"] / f"installed.{operation_id}.tmp"
    completion = _completion_record(intent)
    completion_temp = layout["receipts"] / f".{operation_id}.tmp"
    installed_bytes = _read_private_bytes(installed_temp, root_device, missing_ok=True)
    completion_bytes = _read_private_bytes(completion_temp, root_device, missing_ok=True)
    if completed and (installed_bytes is not None or completion_bytes is not None):
        raise AdminError("completed operation retains an unexpected private temporary")
    expected_installed = _canonical_json(_installed_after(intent)) + b"\n"
    expected_completion = _canonical_json(completion) + b"\n"
    if installed_bytes is not None and (
        installed_bytes != expected_installed and not expected_installed.startswith(installed_bytes)
    ):
        raise AdminError(
            f"staged installed identity cannot be safely recovered (not an approved record prefix): {installed_temp}"
        )
    if completion_bytes is not None and (
        completion_bytes != expected_completion and not expected_completion.startswith(completion_bytes)
    ):
        raise AdminError(
            f"staged operation receipt cannot be safely recovered (not an approved record prefix): {completion_temp}"
        )
    if check_directory_entries:
        try:
            allowed_base = {"installed.json", "lock", "operations"}
            if installed_bytes is not None:
                allowed_base.add(installed_temp.name)
            with os.scandir(layout["base"]) as base_entries:
                for entry in base_entries:
                    if entry.name not in allowed_base:
                        raise AdminError("unexpected entry in Git-private administration directory")
            allowed_receipt_temporary = None if completed else f".{operation_id}.tmp"
            with os.scandir(layout["receipts"]) as receipt_entries:
                for entry in receipt_entries:
                    if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                        continue
                    if entry.name != allowed_receipt_temporary:
                        raise AdminError(
                            "unexpected entry in Git-private operation receipt directory"
                        )
        except OSError as exc:
            raise AdminError("cannot inspect Git-private temporary entries") from exc


def _authorize(facts: object, caller_authorizer: Callable[[object], bool]) -> None:
    if not callable(caller_authorizer):
        raise AdminError("mutation requires an independently verified caller authorization callback")
    try:
        authorized = caller_authorizer(facts)
    except Exception as exc:
        raise AdminError(f"independent caller authorization failed: {exc}") from exc
    if authorized is not True:
        raise AdminError("independent caller authorization did not return exactly True")


def _inspect_request(request: _Request, identity: object) -> dict[str, Any]:
    facts = _observe_bound_identity(request, identity)
    root_device = request.root.stat().st_dev
    layout = _private_layout(facts, root_device, create=False)
    intent, status, _ = _resolve_operation(
        request, layout, root_device, facts, allow_staged_inspection=True
    )
    actions = [
        _action(path, intent["before"][path], intent["after"][path])
        for path in _ordered_operation_paths(intent)
    ]
    return {
        "status": {
            "ready": "READY",
            "pending": "RETRY",
            "intent-staged": "RETRY",
            "complete": "ALREADY_APPLIED",
        }[status],
        "operation": request.operation,
        "operationId": intent["operation_id"],
        "target": {
            "root": str(request.root),
            "repository": request.repository,
            "branch": request.branch,
            "head": request.head,
        },
        "actions": actions,
        # A fresh install has no ordinary Task state from which publication
        # can be inferred. The future collaboration capability consumes this
        # exact bootstrap subject, never the Admin engine's own Git writes.
        "publicationHandoff": (
            {
                "capability": "#194-bootstrap-publication",
                "repository": request.repository,
                "branch": request.branch,
                "baseHead": request.head,
                "operationId": intent["operation_id"],
                "paths": [action["path"] for action in actions if action["action"] != "noop"],
                "requiresDraftPr": True,
            }
            if request.operation == "install" else None
        ),
        "canApply": status != "complete",
        "requiresRetry": status == "intent-staged",
        "readOnly": True,
    }


def inspect(
    operation: str,
    target_root: Path,
    *,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
    payload_directory: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_source_revision: str | None = None,
    legacy_inventory: Mapping[str, Any] | None = None,
    issue: int | None = None,
    pr: int | None = None,
    expected_base: str | None = None,
    task_binding: Callable[[object, int, int], bool] | None = None,
    metadata_binding: Callable[[object, int, int], bool] | None = None,
) -> dict[str, Any]:
    """Inspect or plan an exact operation without writing target or Git-private state."""
    request = _make_request(
        operation,
        target_root,
        expected_repository=expected_repository,
        expected_branch=expected_branch,
        expected_head=expected_head,
        payload_directory=payload_directory,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_revision=expected_source_revision,
        legacy_inventory=legacy_inventory,
        issue=issue,
        pr=pr,
        expected_base=expected_base,
        task_binding=task_binding,
        metadata_binding=metadata_binding,
    )
    identity = _identity_api()
    return _inspect_request(request, identity)


def plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for the read-only ``inspect`` operation."""
    return inspect(*args, **kwargs)


def _stage_present(parent: int, leaf: str, temporary: str) -> bool:
    try:
        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdminError(f"cannot inspect operation temporary file: {temporary}") from exc


def _operation_temporary_present(root: Path, relative: str, root_device: int) -> bool:
    parent, leaf = _open_parent(
        root, PurePosixPath(relative).parts, root_device, create=False
    )
    if parent is None:
        return False
    try:
        try:
            os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AdminError(f"cannot inspect operation temporary file: {relative}") from exc
    finally:
        os.close(parent)


def _ensure_staged_payload(
    root: Path,
    temporary: str,
    temp_parent: int,
    temp_leaf: str,
    content: bytes,
    mode: int,
    root_device: int,
) -> None:
    if _stage_present(temp_parent, temp_leaf, temporary):
        state = _operation_temporary_state(root, temporary, root_device, content, mode)
        if state == "partial":
            staged = _read_operation_temporary(root, temporary, root_device)
            if staged is None:
                raise AdminError(f"known operation temporary disappeared during retry: {temporary}")
            partial, staged_mode = staged
            if staged_mode != 0o600 or not content.startswith(partial):
                raise AdminError(f"operation temporary is not an Admin-owned approved prefix: {temporary}")
            try:
                descriptor = os.open(
                    temp_leaf,
                    os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=temp_parent,
                )
            except OSError as exc:
                raise AdminError(f"cannot safely reconcile operation temporary: {temporary}") from exc
            try:
                opened = os.fstat(descriptor)
                current = os.stat(temp_leaf, dir_fd=temp_parent, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or _stat_identity(opened) != _stat_identity(current)
                ):
                    raise AdminError(f"operation temporary identity changed before retry: {temporary}")
                view = memoryview(content[len(partial):])
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise AdminError(f"short write while reconciling operation temporary: {temporary}")
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif state != "complete":
            raise AdminError(f"operation temporary state is not recoverable: {temporary}")
        if _operation_temporary_state(root, temporary, root_device, content, mode) != "complete":
            raise AdminError(f"operation temporary recovery failed exact post-check: {temporary}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temp_leaf, flags, 0o600, dir_fd=temp_parent)
    except FileExistsError:
        raise AdminError(f"operation temporary appeared during exclusive staging: {temporary}")
    except OSError as exc:
        raise AdminError(f"cannot create operation temporary file: {temporary}") from exc
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AdminError(f"short write to operation temporary file: {temporary}")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _operation_temporary_state(root, temporary, root_device, content, mode) != "complete":
        raise AdminError(f"operation temporary failed its exact post-write check: {temporary}")


def _write_target_file(
    root: Path,
    relative: str,
    content: bytes,
    mode: int,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    temporary: str,
    root_device: int,
    admin_fs_api: Any | None,
) -> None:
    temp_parent, temp_leaf = _open_parent(
        root, PurePosixPath(temporary).parts, root_device, create=True
    )
    if temp_parent is None:
        raise AdminError(f"cannot create operation temporary parent for {relative}")
    try:
        target_parent, target_leaf = _open_parent(
            root, PurePosixPath(relative).parts, root_device, create=True
        )
    except BaseException:
        os.close(temp_parent)
        raise
    if target_parent is None:
        os.close(temp_parent)
        raise AdminError(f"cannot create managed parent for {relative}")
    try:
        if _stat_identity(os.fstat(temp_parent)) != _stat_identity(os.fstat(target_parent)):
            raise AdminError(f"managed parent changed while preparing {relative}")
        current = _observe_path(root, relative, root_device)
        staged = _stage_present(temp_parent, temp_leaf, temporary)
        if current == after:
            if staged:
                raise AdminError(f"same-operation postimage has an unexpected retained stage: {relative}")
            return
        if current != before:
            raise AdminError(f"managed path has an unknown or dirty preimage: {relative}")

        _ensure_staged_payload(
            root, temporary, temp_parent, temp_leaf, content, mode, root_device
        )
        os.fsync(temp_parent)
        # Reobserve the exact known preimage immediately before the one-name operation.
        if _observe_path(root, relative, root_device) != before:
            raise AdminError(f"managed path changed immediately before replacement: {relative}")
        if before is None:
            if admin_fs_api is None:
                raise AdminError("atomic no-replace interface was not loaded during preflight")
            try:
                admin_fs_api.move_noreplace(
                    temp_parent, temp_leaf, target_parent, target_leaf
                )
            except Exception as exc:
                raise AdminError(
                    f"cannot create absent managed path with atomic no-replace: {relative}"
                ) from exc
        else:
            try:
                os.replace(
                    temp_leaf,
                    target_leaf,
                    src_dir_fd=temp_parent,
                    dst_dir_fd=target_parent,
                )
            except OSError as exc:
                raise AdminError(f"cannot atomically replace exact managed file: {relative}") from exc
        os.fsync(target_parent)
        if temp_parent != target_parent:
            os.fsync(temp_parent)
        if _stage_present(temp_parent, temp_leaf, temporary):
            raise AdminError(f"operation temporary remains after atomic install: {temporary}")
        if _observe_path(root, relative, root_device) != after:
            raise AdminError(f"managed path failed exact post-replacement check: {relative}")
    finally:
        os.close(target_parent)
        os.close(temp_parent)


def _remove_target_file(
    root: Path,
    relative: str,
    before: dict[str, Any],
    root_device: int,
) -> None:
    parts = PurePosixPath(relative).parts
    parent, leaf = _open_parent(root, parts, root_device, create=False)
    if parent is None:
        raise AdminError(f"managed parent disappeared before removal: {relative}")
    try:
        current = _observe_path(root, relative, root_device)
        if current is None:
            return
        if current != before:
            raise AdminError(f"managed path has an unknown or dirty preimage before removal: {relative}")
        if _observe_path(root, relative, root_device) != before:
            raise AdminError(f"managed path changed immediately before removal: {relative}")
        try:
            os.unlink(leaf, dir_fd=parent)
        except OSError as exc:
            raise AdminError(f"cannot unlink exact known managed file: {relative}") from exc
        os.fsync(parent)
        if _observe_path(root, relative, root_device) is not None:
            raise AdminError(f"managed path reappeared after exact unlink: {relative}")
    finally:
        os.close(parent)


def _apply_one_path(
    request: _Request,
    intent: dict[str, Any],
    path: str,
    layout: dict[str, Path],
    root_device: int,
    admin_fs_api: Any | None,
) -> None:
    before = intent["before"][path]
    after = intent["after"][path]
    if after is None:
        if before is not None:
            _remove_target_file(
                request.root,
                path,
                before,
                root_device,
            )
        return
    assert request.payload is not None
    payload_file = request.payload.files.get(path)
    if payload_file is None or payload_file.fingerprint != after:
        raise AdminError(f"approved payload bytes are unavailable for managed path: {path}")
    _write_target_file(
        request.root,
        path,
        payload_file.content,
        payload_file.mode,
        before,
        after,
        intent["temporary_paths"][path],
        root_device,
        admin_fs_api,
    )


def _installed_temp_path(layout: dict[str, Path], operation_id: str) -> Path:
    return layout["base"] / f"installed.{operation_id}.tmp"


def _completion_temp_path(layout: dict[str, Path], operation_id: str) -> Path:
    return layout["receipts"] / f".{operation_id}.tmp"


def _write_installed_after(
    layout: dict[str, Path], intent: dict[str, Any], root_device: int
) -> None:
    value = _installed_after(intent)
    current = _read_current_installed(layout, root_device)
    if current == value:
        expected_current = value
    elif current is None and intent["prior_generation"] is None:
        expected_current = None
    elif current is not None and current["generation"] == intent["prior_generation"]:
        expected_current = current
    else:
        raise AdminError("installed generation changed before identity update")
    _atomic_private_json(
        layout["installed"],
        _installed_temp_path(layout, intent["operation_id"]),
        value,
        root_device,
        expected_current=expected_current,
    )


def _write_completion(
    layout: dict[str, Path], intent: dict[str, Any], root_device: int
) -> None:
    record = _completion_record(intent)
    path = layout["receipts"] / f"{intent['operation_id']}.json"
    temporary = _completion_temp_path(layout, intent["operation_id"])
    _atomic_private_json(path, temporary, record, root_device, expected_current=None)


def _verify_final_state(
    request: _Request,
    intent: dict[str, Any],
    layout: dict[str, Path],
    root_device: int,
    known_directories: set[str],
) -> None:
    installed = _read_current_installed(layout, root_device)
    _validate_current_operation_state(
        request,
        intent,
        layout,
        installed,
        root_device,
        complete=True,
        known_directories=known_directories,
    )


def _reobserve_local_operation(
    request: _Request,
    facts: object,
    intent: dict[str, Any],
    layout: dict[str, Path],
    root_device: int,
    expected_index: tuple[tuple[int, ...], str] | None,
    expected_git_state: tuple[Any, ...],
    known_directories: set[str],
) -> str:
    """Collect local-only receipt, worktree, index, and ref evidence after unlock."""
    changed_paths = [
        path
        for path, before in intent["before"].items()
        if before != intent["after"][path]
    ]
    details: list[str] = [
        "intent changed paths=" + json.dumps(changed_paths, ensure_ascii=True)
    ]
    try:
        git_dir = _facts_path(facts, "git_dir")
        current_git_state = _local_git_identity(request, facts, root_device)
        details.append(
            "Git HEAD/ref=unchanged"
            if current_git_state == expected_git_state
            else "Git HEAD/ref=changed"
        )
    except Exception as exc:
        details.append(f"Git HEAD/ref=unavailable ({exc})")
    try:
        git_dir = _facts_path(facts, "git_dir")
        current_index = _index_snapshot(git_dir)
        details.append(
            "index=unchanged"
            if current_index == expected_index
            else "index=changed"
        )
    except Exception as exc:
        details.append(f"index=unavailable ({exc})")
    try:
        installed, intents, completions, _ = _read_layout_records(layout, root_device)
        stored_intent = intents.get(intent["operation_id"])
        if stored_intent != intent:
            details.append("receipt=missing-or-mismatched-intent")
        elif intent["operation_id"] in completions:
            _validate_private_temporary_state(
                layout, intent, root_device, completed=True
            )
            _validate_current_operation_state(
                request,
                intent,
                layout,
                installed,
                root_device,
                complete=True,
                known_directories=known_directories,
            )
            details.append("receipt=complete; managed-state=exact-postimage")
        else:
            _validate_private_temporary_state(
                layout, intent, root_device, completed=False
            )
            _validate_current_operation_state(
                request,
                intent,
                layout,
                installed,
                root_device,
                complete=False,
                known_directories=known_directories,
            )
            details.append("receipt=pending; managed-state=known-preimage-or-postimage")
    except Exception as exc:
        details.append(f"receipt-or-managed-state=unavailable ({exc})")
    return "; ".join(details)


def _apply_request(
    request: _Request,
    caller_authorizer: Callable[[object], bool],
    *,
    expected_operation_id: str | None = None,
) -> dict[str, Any]:
    if not callable(caller_authorizer):
        raise AdminError("mutation requires an independently verified caller authorization callback")
    retrying = expected_operation_id is not None
    identity = _identity_api()
    facts, preflight_binding_snapshot = _observe_bound_identity_snapshot(request, identity)
    _authorize(facts, caller_authorizer)
    root_device = request.root.stat().st_dev
    preflight_git_dir = _facts_path(facts, "git_dir")
    preflight_index = _index_snapshot(preflight_git_dir)
    preflight_git_state = _local_git_identity(request, facts, root_device)
    layout = _private_layout(facts, root_device, create=False)
    if retrying and layout is None:
        raise AdminError("retry requires a persisted matching operation intent")
    if layout is None:
        layout = _private_layout(facts, root_device, create=True)
    if layout is None:
        raise AdminError("retry requires a persisted matching operation intent")
    _ensure_admin_lock_file(layout, root_device)

    # Snapshot the exact private namespace around every phase-1 history read.
    # Directory mtimes/ctimes make a concurrent cooperating operation visible;
    # the installed file identity binds the parsed installed state as well.
    history_before = _operation_directory_identity(layout, root_device)
    installed_identity_before = _private_file_identity(
        layout["installed"], root_device, missing_ok=True
    )
    installed_from_history, known_intents, _, _ = _read_layout_records(layout, root_device)
    known_directories = _known_admin_directories(known_intents)
    preflight_managed = _scan_managed_paths(
        request.root,
        root_device,
        allowed_directories=known_directories,
    )
    # Phase 1 completes every Git/GitHub/caller observation, index-overlap
    # query, and intent calculation before acquiring any writer lock.
    preflight_intent, preflight_status, preflight_installed = _resolve_operation(
        request,
        layout,
        root_device,
        facts,
        expected_operation_id=expected_operation_id,
        check_index=False,
    )
    history_after = _operation_directory_identity(layout, root_device)
    installed_identity_after = _private_file_identity(
        layout["installed"], root_device, missing_ok=True
    )
    if (
        history_after != history_before
        or installed_identity_after != installed_identity_before
        or installed_from_history != preflight_installed
    ):
        raise AdminError("Git-private operation history or installed identity changed during preflight")
    preflight_directories = history_after
    preflight_installed_identity = installed_identity_after
    operation_id = preflight_intent["operation_id"]
    _check_index_overlap(request, preflight_intent)
    if _index_snapshot(preflight_git_dir) != preflight_index:
        raise AdminError("Git index identity changed during authorization preflight")
    if _scan_managed_paths(
        request.root,
        root_device,
        allowed_directories=known_directories,
    ) != preflight_managed:
        raise AdminError("managed worktree changed during authorization preflight")
    admin_fs_api = None
    if any(
        before is None and preflight_intent["after"][path] is not None
        for path, before in preflight_intent["before"].items()
    ):
        # Avoid the loader's process-local mutex from ever being reached under
        # an Admin/index/HEAD writer lock.
        admin_fs_api = _admin_fs_api().prepare_no_replace()
    phase2_result: dict[str, Any] | None = None
    admin_lock_acquired = False
    try:
        with _worktree_lock(layout, root_device):
            admin_lock_acquired = True
            with _git_writer_fence(
                request,
                facts,
                root_device,
                expected_index=preflight_index,
                expected_managed=preflight_managed,
                allowed_directories=known_directories,
            ) as locked_index:
                if _local_git_identity(request, facts, root_device) != preflight_git_state:
                    raise AdminError("local Git HEAD/ref changed since authorization preflight")
                if _index_snapshot(preflight_git_dir) != locked_index:
                    raise AdminError("Git index identity changed under writer guards")
                installed = _validate_locked_preflight_snapshot(
                    request,
                    layout,
                    preflight_intent,
                    preflight_status,
                    root_device,
                    known_directories,
                    expected_directories=preflight_directories,
                    expected_installed=preflight_installed,
                    expected_installed_identity=preflight_installed_identity,
                )
                intent = preflight_intent
                status = preflight_status
                if retrying and status == "ready":
                    raise AdminError("retry requires a persisted matching operation intent")
                if status == "complete":
                    phase2_result = {
                        "status": "ALREADY_APPLIED",
                        "operation": request.operation,
                        "operationId": operation_id,
                        "changedPaths": [],
                    }
                else:
                    if status in {"ready", "intent-staged"}:
                        _write_intent_atomic(
                            layout,
                            intent,
                            root_device,
                            retry=retrying and status == "intent-staged",
                        )
                        intent_path = layout["intents"] / f"{operation_id}.json"
                        persisted = _read_private_json(intent_path, root_device)
                        assert persisted is not None
                        intent = _validate_intent(persisted)
                    changed = [
                        path
                        for path in _ordered_operation_paths(intent)
                        if intent["before"][path] != intent["after"][path]
                    ]
                    for path in _ordered_operation_paths(intent):
                        before = intent["before"][path]
                        after = intent["after"][path]
                        if before == after:
                            continue
                        observed = _observe_path(request.root, path, root_device)
                        if observed != before and observed != after:
                            raise AdminError(f"managed path has an unknown or dirty preimage: {path}")
                        _apply_one_path(
                            request,
                            intent,
                            path,
                            layout,
                            root_device,
                            admin_fs_api,
                        )
                        after_observed = _observe_path(request.root, path, root_device)
                        if after_observed != after:
                            raise AdminError(f"managed path did not reach its exact post-state: {path}")
                    _validate_current_operation_state(
                        request,
                        intent,
                        layout,
                        _read_current_installed(layout, root_device),
                        root_device,
                        complete=False,
                        known_directories=known_directories,
                    )
                    _write_installed_after(layout, intent, root_device)
                    _verify_final_state(request, intent, layout, root_device, known_directories)
                    _write_completion(layout, intent, root_device)
                    _verify_final_state(request, intent, layout, root_device, known_directories)
                    phase2_result = {
                        "status": "APPLIED" if changed else "APPLIED_NO_FILE_CHANGES",
                        "operation": request.operation,
                        "operationId": operation_id,
                        "changedPaths": changed,
                    }

                # Final local-only checks happen while every writer guard is
                # still held. No identity, authorization, Git subprocess, or
                # callback is used in phase 2.
                _verify_final_state(request, intent, layout, root_device, known_directories)
                if _index_snapshot(preflight_git_dir) != locked_index:
                    raise AdminError("Git index identity changed before writer guards were released")
                if _local_git_identity(request, facts, root_device) != preflight_git_state:
                    raise AdminError("local Git HEAD/ref changed before writer guards were released")
    except Exception as exc:
        if not admin_lock_acquired:
            raise
        evidence = _reobserve_local_operation(
            request,
            facts,
            preflight_intent,
            layout,
            root_device,
            preflight_index,
            preflight_git_state,
            known_directories,
        )
        raise AdminError(
            f"operation {operation_id} phase two stopped: {exc}; local evidence: {evidence}"
        ) from exc

    assert phase2_result is not None
    try:
        post_facts, post_binding_snapshot = _observe_bound_identity_snapshot(request, identity)
        _authorize(post_facts, caller_authorizer)
        if _target_identity(request, post_facts) != _target_identity(request, facts):
            raise AdminError("target identity drifted after the locked local operation")
        if post_binding_snapshot != preflight_binding_snapshot:
            raise AdminError("cutover Issue/PR snapshot drifted after the locked local operation")
        if _local_git_identity(request, post_facts, root_device) != preflight_git_state:
            raise AdminError("local Git HEAD/ref drifted after the locked operation")
    except Exception as exc:
        changed_paths = [
            path
            for path, before in preflight_intent["before"].items()
            if before != preflight_intent["after"][path]
        ]
        evidence = _reobserve_local_operation(
            request,
            facts,
            preflight_intent,
            layout,
            root_device,
            preflight_index,
            preflight_git_state,
            known_directories,
        )
        raise AdminError(
            f"operation {operation_id} post-lock identity/authorization check failed; "
            f"local effect may have completed; changed paths from intent: "
            f"{json.dumps(changed_paths, ensure_ascii=True)}; local evidence: {evidence}; cause: {exc}"
        ) from exc
    return phase2_result


def _request_from_arguments(
    operation: str,
    target_root: Path,
    *,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
    payload_directory: Path | None,
    expected_manifest_sha256: str | None,
    expected_source_revision: str | None,
    legacy_inventory: Mapping[str, Any] | None,
    issue: int | None,
    pr: int | None,
    expected_base: str | None,
    task_binding: Callable[[object, int, int], bool] | None,
    metadata_binding: Callable[[object, int, int], bool] | None,
) -> _Request:
    return _make_request(
        operation,
        target_root,
        expected_repository=expected_repository,
        expected_branch=expected_branch,
        expected_head=expected_head,
        payload_directory=payload_directory,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_revision=expected_source_revision,
        legacy_inventory=legacy_inventory,
        issue=issue,
        pr=pr,
        expected_base=expected_base,
        task_binding=task_binding,
        metadata_binding=metadata_binding,
    )


def apply(
    operation: str,
    target_root: Path,
    *,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
    caller_authorizer: Callable[[object], bool],
    payload_directory: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_source_revision: str | None = None,
    legacy_inventory: Mapping[str, Any] | None = None,
    issue: int | None = None,
    pr: int | None = None,
    expected_base: str | None = None,
    task_binding: Callable[[object, int, int], bool] | None = None,
    metadata_binding: Callable[[object, int, int], bool] | None = None,
) -> dict[str, Any]:
    """Apply one exact intent after independent caller authorization."""
    request = _request_from_arguments(
        operation,
        target_root,
        expected_repository=expected_repository,
        expected_branch=expected_branch,
        expected_head=expected_head,
        payload_directory=payload_directory,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_revision=expected_source_revision,
        legacy_inventory=legacy_inventory,
        issue=issue,
        pr=pr,
        expected_base=expected_base,
        task_binding=task_binding,
        metadata_binding=metadata_binding,
    )
    return _apply_request(request, caller_authorizer)


def retry(
    operation: str,
    target_root: Path,
    *,
    expected_operation_id: str,
    expected_repository: str,
    expected_branch: str,
    expected_head: str,
    caller_authorizer: Callable[[object], bool],
    payload_directory: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_source_revision: str | None = None,
    legacy_inventory: Mapping[str, Any] | None = None,
    issue: int | None = None,
    pr: int | None = None,
    expected_base: str | None = None,
    task_binding: Callable[[object, int, int], bool] | None = None,
    metadata_binding: Callable[[object, int, int], bool] | None = None,
) -> dict[str, Any]:
    """Retry only a persisted operation whose immutable ID is explicitly approved."""
    if not isinstance(expected_operation_id, str) or _OPERATION_RE.fullmatch(expected_operation_id) is None:
        raise AdminError("expected_operation_id must be a full lowercase SHA-256 operation ID")
    request = _request_from_arguments(
        operation,
        target_root,
        expected_repository=expected_repository,
        expected_branch=expected_branch,
        expected_head=expected_head,
        payload_directory=payload_directory,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_source_revision=expected_source_revision,
        legacy_inventory=legacy_inventory,
        issue=issue,
        pr=pr,
        expected_base=expected_base,
        task_binding=task_binding,
        metadata_binding=metadata_binding,
    )
    return _apply_request(
        request,
        caller_authorizer,
        expected_operation_id=expected_operation_id,
    )
