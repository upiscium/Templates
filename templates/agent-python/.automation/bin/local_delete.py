#!/usr/bin/env python3
"""Perform one bounded, descriptor-anchored deletion in the current Task worktree.

The Agent Core local-filesystem trust boundary excludes hostile processes running
as the same effective user. Within that boundary, every mutation is anchored to
the validated worktree descriptors and the preflighted entry identities.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import task_lifecycle as lifecycle


class LocalDeleteError(RuntimeError):
    """A requested deletion is outside the bounded local-delete contract."""


_TARGET_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")
_PROTECTED_NAMES = frozenset(
    {
        ".git",
        ".task-state",
        ".automation",
        ".opencode",
        ".github",
        "AGENTS.md",
        "Justfile",
        "opencode.json",
    }
)
_TERMINAL_TASK_STATES = {"merged", "cancelled"}


@dataclass(frozen=True)
class EntryIdentity:
    device: int
    inode: int
    mode: int


DirectorySnapshot = dict[str, dict[str, EntryIdentity]]


def _identity(metadata: os.stat_result) -> EntryIdentity:
    return EntryIdentity(metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _require_identity(
    metadata: os.stat_result,
    expected: EntryIdentity,
    relative: str,
) -> None:
    actual = _identity(metadata)
    if actual != expected:
        raise LocalDeleteError(f"path changed during deletion: {relative}")


def parse_relative_target(raw: str) -> tuple[str, ...]:
    """Accept only a literal, normalized, single relative path."""
    if not isinstance(raw, str) or not _TARGET_RE.fullmatch(raw):
        raise LocalDeleteError(
            "target must be one normalized relative path with literal safe components"
        )
    parts = tuple(raw.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LocalDeleteError("target must not contain empty, dot, or parent components")
    protected = [part for part in parts if part in _PROTECTED_NAMES]
    if protected:
        raise LocalDeleteError(
            "target contains a protected Agent Core path: " + ", ".join(protected)
        )
    return parts


def _nofollow_directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or not hasattr(os, "O_DIRECTORY"):
        raise LocalDeleteError("platform lacks the required no-follow directory API")
    return os.O_RDONLY | os.O_DIRECTORY | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_directory(
    name: str | os.PathLike[str],
    *,
    dir_fd: int | None = None,
    expected_device: int | None = None,
) -> int:
    flags = _nofollow_directory_flags()
    descriptor = -1
    try:
        if dir_fd is None:
            descriptor = os.open(name, flags)
        else:
            descriptor = os.open(name, flags, dir_fd=dir_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            descriptor = -1
            raise LocalDeleteError(f"path component is not a directory: {name}")
        if expected_device is not None and metadata.st_dev != expected_device:
            os.close(descriptor)
            descriptor = -1
            raise LocalDeleteError(f"mounted path is not permitted: {name}")
        return descriptor
    except LocalDeleteError:
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise LocalDeleteError(
            f"cannot open path component without following symlinks: {name}"
        ) from exc


def _open_bound_root(root: Path) -> tuple[int, EntryIdentity]:
    descriptor = _open_directory(root)
    try:
        identity = _identity(os.fstat(descriptor))
        path_metadata = os.stat(root, follow_symlinks=False)
        _require_identity(path_metadata, identity, str(root))
        return descriptor, identity
    except LocalDeleteError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise LocalDeleteError("cannot bind the current worktree root safely") from exc


def _assert_bound_root(root: Path, descriptor: int, expected: EntryIdentity) -> None:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise LocalDeleteError("current worktree root changed during deletion") from exc
    _require_identity(descriptor_metadata, expected, str(root))
    _require_identity(path_metadata, expected, str(root))


def _open_parent(
    root: Path,
    parts: tuple[str, ...],
    *,
    root_fd: int | None = None,
    root_identity: EntryIdentity | None = None,
) -> tuple[int, str, EntryIdentity]:
    if root_fd is None:
        current = _open_directory(root)
    else:
        if root_identity is None:
            raise LocalDeleteError("missing bound worktree-root identity")
        _assert_bound_root(root, root_fd, root_identity)
        try:
            current = os.dup(root_fd)
        except OSError as exc:
            raise LocalDeleteError("cannot duplicate bound worktree-root descriptor") from exc
    try:
        actual_root = os.fstat(current)
        _require_identity(actual_root, root_identity or _identity(actual_root), str(root))
        bound_identity = root_identity or _identity(actual_root)
        for component in parts[:-1]:
            next_descriptor = _open_directory(
                component,
                dir_fd=current,
                expected_device=bound_identity.device,
            )
            os.close(current)
            current = next_descriptor
        return current, parts[-1], bound_identity
    except BaseException:
        os.close(current)
        raise


def _entry_metadata(
    parent_fd: int,
    name: str,
    relative: str,
    expected_device: int,
) -> os.stat_result:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise LocalDeleteError(f"target does not exist: {relative}") from exc
    except OSError as exc:
        raise LocalDeleteError(f"cannot inspect target safely: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise LocalDeleteError(f"symlink target is not permitted: {relative}")
    if metadata.st_dev != expected_device:
        raise LocalDeleteError(f"mounted path is not permitted: {relative}")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise LocalDeleteError(f"special-file target is not permitted: {relative}")
    return metadata


def _check_child_name(name: str, relative: str) -> None:
    if name in _PROTECTED_NAMES:
        raise LocalDeleteError(f"protected path is not permitted: {relative}")


def _directory_names(directory_fd: int, relative: str, phase: str) -> list[str]:
    try:
        return os.listdir(directory_fd)
    except OSError as exc:
        raise LocalDeleteError(f"cannot enumerate target {phase}: {relative}") from exc


def _preflight_directory(
    directory_fd: int,
    relative: str,
    expected_device: int,
    snapshot: DirectorySnapshot,
) -> None:
    entries: dict[str, EntryIdentity] = {}
    snapshot[relative] = entries
    for name in _directory_names(directory_fd, relative, "safely"):
        child = f"{relative}/{name}"
        _check_child_name(name, child)
        metadata = _entry_metadata(directory_fd, name, child, expected_device)
        entries[name] = _identity(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_device=expected_device,
            )
            try:
                _preflight_directory(child_fd, child, expected_device, snapshot)
            finally:
                os.close(child_fd)


def _revalidate_directory(
    directory_fd: int,
    relative: str,
    expected_device: int,
    snapshot: DirectorySnapshot,
) -> None:
    expected_entries = snapshot.get(relative)
    if expected_entries is None:
        raise LocalDeleteError(f"directory was not present during preflight: {relative}")
    names = _directory_names(directory_fd, relative, "revalidation")
    if set(names) != set(expected_entries):
        raise LocalDeleteError(f"directory contents changed during deletion: {relative}")
    for name, expected in expected_entries.items():
        child = f"{relative}/{name}"
        metadata = _entry_metadata(directory_fd, name, child, expected_device)
        _require_identity(metadata, expected, child)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_device=expected_device,
            )
            try:
                _require_identity(os.fstat(child_fd), expected, child)
                _revalidate_directory(child_fd, child, expected_device, snapshot)
            finally:
                os.close(child_fd)


def _open_file_for_delete(
    parent_fd: int,
    name: str,
    relative: str,
    expected: EntryIdentity,
    expected_device: int,
) -> int:
    flags = (
        getattr(os, "O_PATH", os.O_RDONLY)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != expected_device:
            raise LocalDeleteError(f"file changed during deletion: {relative}")
        _require_identity(metadata, expected, relative)
        return descriptor
    except LocalDeleteError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise LocalDeleteError(f"cannot open file safely: {relative}") from exc


def _delete_directory_contents(
    directory_fd: int,
    relative: str,
    expected_device: int,
    snapshot: DirectorySnapshot,
) -> None:
    _revalidate_directory(directory_fd, relative, expected_device, snapshot)
    expected_entries = snapshot[relative]
    for name in list(expected_entries):
        _revalidate_directory(directory_fd, relative, expected_device, snapshot)
        child = f"{relative}/{name}"
        expected = expected_entries.get(name)
        if expected is None:
            raise LocalDeleteError(f"directory contents changed during deletion: {relative}")
        metadata = _entry_metadata(directory_fd, name, child, expected_device)
        _require_identity(metadata, expected, child)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_device=expected_device,
            )
            try:
                _require_identity(os.fstat(child_fd), expected, child)
                _delete_directory_contents(child_fd, child, expected_device, snapshot)
                _revalidate_directory(child_fd, child, expected_device, snapshot)
                _require_identity(os.fstat(child_fd), expected, child)
            finally:
                os.close(child_fd)
            try:
                _require_identity(
                    _entry_metadata(directory_fd, name, child, expected_device),
                    expected,
                    child,
                )
                os.rmdir(name, dir_fd=directory_fd)
            except OSError as exc:
                raise LocalDeleteError(f"directory changed during deletion: {child}") from exc
        else:
            file_fd = _open_file_for_delete(
                directory_fd,
                name,
                child,
                expected,
                expected_device,
            )
            try:
                _require_identity(
                    _entry_metadata(directory_fd, name, child, expected_device),
                    expected,
                    child,
                )
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise LocalDeleteError(f"file changed during deletion: {child}") from exc
            finally:
                os.close(file_fd)
        del expected_entries[name]
    _revalidate_directory(directory_fd, relative, expected_device, snapshot)


def delete_target(
    root: Path,
    raw_target: str,
    recursive: bool,
    *,
    root_fd: int | None = None,
    root_identity: EntryIdentity | None = None,
) -> str:
    """Delete a validated target without resolving any target component."""
    parts = parse_relative_target(raw_target)
    parent_fd, name, bound_root = _open_parent(
        root,
        parts,
        root_fd=root_fd,
        root_identity=root_identity,
    )
    try:
        relative = "/".join(parts)
        metadata = _entry_metadata(parent_fd, name, relative, bound_root.device)
        target_identity = _identity(metadata)
        if stat.S_ISREG(metadata.st_mode):
            file_fd = _open_file_for_delete(
                parent_fd,
                name,
                relative,
                target_identity,
                bound_root.device,
            )
            try:
                _require_identity(
                    _entry_metadata(parent_fd, name, relative, bound_root.device),
                    target_identity,
                    relative,
                )
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                raise LocalDeleteError(f"file changed during deletion: {relative}") from exc
            finally:
                os.close(file_fd)
            return relative

        target_fd = _open_directory(
            name,
            dir_fd=parent_fd,
            expected_device=bound_root.device,
        )
        try:
            _require_identity(os.fstat(target_fd), target_identity, relative)
            if not recursive:
                names = _directory_names(target_fd, relative, "safely")
                if names:
                    raise LocalDeleteError(
                        f"non-empty directory requires recursive=true: {relative}"
                    )
            else:
                # Complete the no-follow/protected-path preflight before any
                # entry is removed. Deletion itself repeats the no-follow
                # checks to keep the descriptor anchor across the mutation.
                snapshot: DirectorySnapshot = {}
                _preflight_directory(target_fd, relative, bound_root.device, snapshot)
                _delete_directory_contents(
                    target_fd,
                    relative,
                    bound_root.device,
                    snapshot,
                )
                _revalidate_directory(target_fd, relative, bound_root.device, snapshot)
            _require_identity(os.fstat(target_fd), target_identity, relative)
        finally:
            os.close(target_fd)

        try:
            _require_identity(
                _entry_metadata(parent_fd, name, relative, bound_root.device),
                target_identity,
                relative,
            )
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as exc:
            raise LocalDeleteError(f"directory changed during deletion: {relative}") from exc
        return relative
    finally:
        os.close(parent_fd)


def _require_task_worktree(
    root: Path,
) -> tuple[lifecycle.WorktreeRecord, str, int, EntryIdentity]:
    """Revalidate Task identity immediately before the destructive operation."""
    root = root.resolve(strict=True)
    root_fd, root_identity = _open_bound_root(root)
    try:
        current = lifecycle.current_worktree(root)
        if current.path != root:
            raise LocalDeleteError("local-delete must run from the exact current Task worktree")
        state = lifecycle.state_path(root)
        try:
            task = lifecycle.extract_identity_value(state, "Task ID")
        except (OSError, UnicodeError) as exc:
            raise LocalDeleteError("local-delete requires an identified Task worktree") from exc
        if not task:
            raise LocalDeleteError("local-delete requires an identified Task worktree")
        record = lifecycle.require_local_task(root, task)
        if record != current:
            raise LocalDeleteError("Task worktree identity changed during validation")
        lifecycle.require_resolved_contract(record, task)
        if lifecycle.state_status(state) in _TERMINAL_TASK_STATES:
            raise LocalDeleteError("local-delete is unavailable for a terminal Task")
        _assert_bound_root(root, root_fd, root_identity)
        return record, task, root_fd, root_identity
    except BaseException:
        os.close(root_fd)
        raise


def guarded_local_delete(root: Path, raw_target: str, recursive: bool) -> dict[str, object]:
    record, task, root_fd, root_identity = _require_task_worktree(root)
    try:
        target = delete_target(
            record.path,
            raw_target,
            recursive,
            root_fd=root_fd,
            root_identity=root_identity,
        )
    finally:
        os.close(root_fd)
    return {
        "task": task,
        "worktree": str(record.path),
        "target": target,
        "recursive": recursive,
        "status": "deleted",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Guarded Task-local filesystem deletion")
    result.add_argument("target")
    result.add_argument("recursive", nargs="?", choices=("false", "true"), default="false")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        root = lifecycle.repo_root(Path.cwd())
        result = guarded_local_delete(root, args.target, args.recursive == "true")
    except (LocalDeleteError, lifecycle.LifecycleError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
