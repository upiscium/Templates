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
_MOUNT_ID_RE = re.compile(r"mnt_id:\s+([0-9]+)\s*\Z")
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
_MUTABLE_TASK_STATES = frozenset({"implementing"})


@dataclass(frozen=True)
class EntryIdentity:
    device: int
    inode: int
    mode: int
    mount_id: int


DirectorySnapshot = dict[str, dict[str, EntryIdentity]]


def _require_linux_mount_identity() -> None:
    if not sys.platform.startswith("linux"):
        raise LocalDeleteError("local-delete requires Linux mount identity support")
    if not hasattr(os, "O_PATH") or not hasattr(os, "O_NOFOLLOW"):
        raise LocalDeleteError("platform lacks the required no-follow mount APIs")


def _mount_id(descriptor: int) -> int:
    """Return the Linux mount namespace identity for an open descriptor."""
    _require_linux_mount_identity()
    matches: list[int] = []
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", "r", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("mnt_id:"):
                    match = _MOUNT_ID_RE.fullmatch(line)
                    if match is None:
                        raise LocalDeleteError("Linux mount identity is malformed")
                    matches.append(int(match.group(1)))
    except (OSError, UnicodeError) as exc:
        raise LocalDeleteError("cannot read Linux mount identity") from exc
    if len(matches) != 1:
        raise LocalDeleteError("Linux mount identity is unavailable")
    return matches[0]


def _identity(metadata: os.stat_result, mount_id: int) -> EntryIdentity:
    return EntryIdentity(metadata.st_dev, metadata.st_ino, metadata.st_mode, mount_id)


def _descriptor_identity(descriptor: int, relative: str) -> EntryIdentity:
    try:
        metadata = os.fstat(descriptor)
        mount_id = _mount_id(descriptor)
    except LocalDeleteError:
        raise
    except OSError as exc:
        raise LocalDeleteError(f"cannot inspect descriptor safely: {relative}") from exc
    return _identity(metadata, mount_id)


def _require_identity(
    actual: EntryIdentity,
    expected: EntryIdentity,
    relative: str,
) -> None:
    if actual != expected:
        raise LocalDeleteError(f"path changed during deletion: {relative}")


def _require_same_mount(
    actual: EntryIdentity,
    expected: EntryIdentity,
    relative: str,
) -> None:
    if actual.device != expected.device or actual.mount_id != expected.mount_id:
        raise LocalDeleteError(f"mounted path is not permitted: {relative}")


def _require_metadata_identity(
    metadata: os.stat_result,
    expected: EntryIdentity,
    relative: str,
) -> None:
    if (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
    ) != (
        expected.device,
        expected.inode,
        expected.mode,
    ):
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
    _require_linux_mount_identity()
    if not hasattr(os, "O_DIRECTORY"):
        raise LocalDeleteError("platform lacks the required no-follow directory API")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory(
    name: str | os.PathLike[str],
    *,
    dir_fd: int | None = None,
    expected_mount: EntryIdentity | None = None,
) -> int:
    flags = _nofollow_directory_flags()
    descriptor = -1
    try:
        if dir_fd is None:
            descriptor = os.open(name, flags)
        else:
            descriptor = os.open(name, flags, dir_fd=dir_fd)
        identity = _descriptor_identity(descriptor, str(name))
        if not stat.S_ISDIR(identity.mode):
            os.close(descriptor)
            descriptor = -1
            raise LocalDeleteError(f"path component is not a directory: {name}")
        if expected_mount is not None:
            _require_same_mount(identity, expected_mount, str(name))
        return descriptor
    except LocalDeleteError:
        if descriptor >= 0:
            os.close(descriptor)
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
        identity = _descriptor_identity(descriptor, str(root))
        path_descriptor = _open_directory(root)
        try:
            _require_identity(
                _descriptor_identity(path_descriptor, str(root)),
                identity,
                str(root),
            )
        finally:
            os.close(path_descriptor)
        return descriptor, identity
    except LocalDeleteError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise LocalDeleteError("cannot bind the current worktree root safely") from exc


def _assert_bound_root(root: Path, descriptor: int, expected: EntryIdentity) -> None:
    try:
        descriptor_identity = _descriptor_identity(descriptor, str(root))
        path_descriptor = _open_directory(root)
        try:
            path_identity = _descriptor_identity(path_descriptor, str(root))
        finally:
            os.close(path_descriptor)
    except OSError as exc:
        raise LocalDeleteError("current worktree root changed during deletion") from exc
    _require_identity(descriptor_identity, expected, str(root))
    _require_identity(path_identity, expected, str(root))


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
        actual_root = _descriptor_identity(current, str(root))
        bound_identity = root_identity or actual_root
        _require_identity(actual_root, bound_identity, str(root))
        for component in parts[:-1]:
            next_descriptor = _open_directory(
                component,
                dir_fd=current,
                expected_mount=bound_identity,
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
    expected_mount: EntryIdentity,
) -> tuple[os.stat_result, EntryIdentity]:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise LocalDeleteError(f"target does not exist: {relative}") from exc
    except OSError as exc:
        raise LocalDeleteError(f"cannot inspect target safely: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise LocalDeleteError(f"symlink target is not permitted: {relative}")
    _require_linux_mount_identity()
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        identity = _descriptor_identity(descriptor, relative)
        _require_metadata_identity(metadata, identity, relative)
        _require_same_mount(identity, expected_mount, relative)
    except LocalDeleteError:
        raise
    except OSError as exc:
        raise LocalDeleteError(f"cannot inspect target descriptor safely: {relative}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise LocalDeleteError(f"special-file target is not permitted: {relative}")
    return metadata, identity


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
    expected_mount: EntryIdentity,
    snapshot: DirectorySnapshot,
) -> None:
    entries: dict[str, EntryIdentity] = {}
    snapshot[relative] = entries
    for name in _directory_names(directory_fd, relative, "safely"):
        child = f"{relative}/{name}"
        _check_child_name(name, child)
        metadata, identity = _entry_metadata(directory_fd, name, child, expected_mount)
        entries[name] = identity
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_mount=expected_mount,
            )
            try:
                _require_identity(
                    _descriptor_identity(child_fd, child),
                    identity,
                    child,
                )
                _preflight_directory(child_fd, child, expected_mount, snapshot)
            finally:
                os.close(child_fd)


def _revalidate_directory(
    directory_fd: int,
    relative: str,
    expected_mount: EntryIdentity,
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
        metadata, identity = _entry_metadata(directory_fd, name, child, expected_mount)
        _require_identity(identity, expected, child)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_mount=expected_mount,
            )
            try:
                _require_identity(_descriptor_identity(child_fd, child), expected, child)
                _revalidate_directory(child_fd, child, expected_mount, snapshot)
            finally:
                os.close(child_fd)


def _open_file_for_delete(
    parent_fd: int,
    name: str,
    relative: str,
    expected: EntryIdentity,
    expected_mount: EntryIdentity,
) -> int:
    _require_linux_mount_identity()
    flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        identity = _descriptor_identity(descriptor, relative)
        if not stat.S_ISREG(identity.mode):
            raise LocalDeleteError(f"file changed during deletion: {relative}")
        _require_same_mount(identity, expected_mount, relative)
        _require_identity(identity, expected, relative)
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
    expected_mount: EntryIdentity,
    snapshot: DirectorySnapshot,
) -> None:
    _revalidate_directory(directory_fd, relative, expected_mount, snapshot)
    expected_entries = snapshot[relative]
    for name in list(expected_entries):
        _revalidate_directory(directory_fd, relative, expected_mount, snapshot)
        child = f"{relative}/{name}"
        expected = expected_entries.get(name)
        if expected is None:
            raise LocalDeleteError(f"directory contents changed during deletion: {relative}")
        metadata, identity = _entry_metadata(directory_fd, name, child, expected_mount)
        _require_identity(identity, expected, child)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(
                name,
                dir_fd=directory_fd,
                expected_mount=expected_mount,
            )
            try:
                _require_identity(_descriptor_identity(child_fd, child), expected, child)
                _delete_directory_contents(child_fd, child, expected_mount, snapshot)
                _revalidate_directory(child_fd, child, expected_mount, snapshot)
                _require_identity(_descriptor_identity(child_fd, child), expected, child)
            finally:
                os.close(child_fd)
            try:
                _, current_identity = _entry_metadata(
                    directory_fd,
                    name,
                    child,
                    expected_mount,
                )
                _require_identity(
                    current_identity,
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
                expected_mount,
            )
            try:
                _, current_identity = _entry_metadata(
                    directory_fd,
                    name,
                    child,
                    expected_mount,
                )
                _require_identity(
                    current_identity,
                    expected,
                    child,
                )
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise LocalDeleteError(f"file changed during deletion: {child}") from exc
            finally:
                os.close(file_fd)
        del expected_entries[name]
    _revalidate_directory(directory_fd, relative, expected_mount, snapshot)


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
        metadata, target_identity = _entry_metadata(parent_fd, name, relative, bound_root)
        if stat.S_ISREG(metadata.st_mode):
            file_fd = _open_file_for_delete(
                parent_fd,
                name,
                relative,
                target_identity,
                bound_root,
            )
            try:
                _, current_identity = _entry_metadata(
                    parent_fd,
                    name,
                    relative,
                    bound_root,
                )
                _require_identity(
                    current_identity,
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
            expected_mount=bound_root,
        )
        try:
            _require_identity(
                _descriptor_identity(target_fd, relative),
                target_identity,
                relative,
            )
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
                _preflight_directory(target_fd, relative, bound_root, snapshot)
                _delete_directory_contents(
                    target_fd,
                    relative,
                    bound_root,
                    snapshot,
                )
                _revalidate_directory(target_fd, relative, bound_root, snapshot)
            _require_identity(_descriptor_identity(target_fd, relative), target_identity, relative)
        finally:
            os.close(target_fd)

        try:
            _, current_identity = _entry_metadata(
                parent_fd,
                name,
                relative,
                bound_root,
            )
            _require_identity(
                current_identity,
                target_identity,
                relative,
            )
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as exc:
            raise LocalDeleteError(f"directory changed during deletion: {relative}") from exc
        return relative
    finally:
        os.close(parent_fd)


def _resolve_task_candidate(
    root: Path,
) -> tuple[Path, lifecycle.WorktreeRecord, str]:
    """Resolve only enough trusted identity to locate the canonical lifecycle lock."""
    root = root.resolve(strict=True)
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
        raise LocalDeleteError("Task worktree identity changed during lock resolution")
    return root, record, task


def _require_locked_mutable_task(
    root: Path,
    candidate: lifecycle.WorktreeRecord,
    candidate_task: str,
    directory_fd: int,
) -> tuple[lifecycle.WorktreeRecord, str, int, EntryIdentity]:
    """Revalidate all mutation authority after the canonical lifecycle lock is held."""
    current = lifecycle.current_worktree(root)
    if current.path != root:
        raise LocalDeleteError("local-delete must run from the exact current Task worktree")
    state = lifecycle.state_path(root)
    try:
        task = lifecycle.extract_identity_value(state, "Task ID")
    except (OSError, UnicodeError) as exc:
        raise LocalDeleteError("local-delete requires an identified Task worktree") from exc
    if not task or task != candidate_task:
        raise LocalDeleteError("Task identity changed while acquiring the lifecycle lock")
    record = lifecycle.require_local_task(root, task)
    if record != current or record != candidate:
        raise LocalDeleteError("Task worktree identity changed while holding the lifecycle lock")
    lifecycle.require_resolved_contract(record, task, directory_fd=directory_fd)
    status = lifecycle.state_status(state)
    if status not in _MUTABLE_TASK_STATES:
        raise LocalDeleteError(
            "local-delete requires the Task to be in an explicit mutable state "
            f"({', '.join(sorted(_MUTABLE_TASK_STATES))}); found {status}"
        )
    root_fd, root_identity = _open_bound_root(root)
    return record, task, root_fd, root_identity


def guarded_local_delete(root: Path, raw_target: str, recursive: bool) -> dict[str, object]:
    root, candidate, candidate_task = _resolve_task_candidate(root)
    with lifecycle.work_units_lock(candidate) as directory_fd:
        record, task, root_fd, root_identity = _require_locked_mutable_task(
            root,
            candidate,
            candidate_task,
            directory_fd,
        )
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
