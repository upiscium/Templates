"""Read-only, descriptor-bound proof of one managed Task worktree's contents.

This module does not decide whether a Task is disposable and has no deletion
API.  A caller must bind the immutable ``TaskRootSpec`` to the current Git
registry facts; a qualified host custody primitive must separately authorize
and atomically remove an exact inventory.  Ordinary ``unlinkat`` is not an
identity compare-and-swap and is deliberately not used here.

The inspector walks only the given worktree, excluding its validated regular
``.git`` pointer.  It retains all unknown, ignored, dirty, or otherwise
ambiguous data in its manifest.  Unsupported filesystem identity proof,
including unavailable Linux/procfs mount IDs, is a hard failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MAX_PATH_BYTES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 10_000
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_GIT_POINTER_BYTES = 4096
_INVENTORY_DOMAIN = b"agentcore-cleanup-inventory/v1\n"
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_TASK = re.compile(r"[1-9][0-9]{0,127}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_GIT_REF_FORBIDDEN = frozenset(" ~^:?*[\\")
_MOUNT_ID = re.compile(rb"mnt_id:\s*([0-9]+)\Z", re.ASCII)


class safeResourceInspectionError(RuntimeError):
    """A fixed, path-redacted error code from a fail-closed inspection."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and _SAFE_CODE.fullmatch(code) else "inspection_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int
    mount_id: int
    mode: int
    uid: int
    nlink: int


@dataclass(frozen=True)
class NodeObservation:
    relative_path: str
    kind: str
    identity: PathIdentity
    size: int
    mtime_ns: int
    ctime_ns: int
    content_sha256: str | None


@dataclass(frozen=True)
class TaskRootSpec:
    """Immutable facade-supplied registry binding; not deletion authority."""

    repository: str
    task: str
    branch_ref: str
    head: str
    repository_root: str
    worktree_root: str
    git_admin: str


@dataclass(frozen=True)
class WorktreeInventory:
    spec: TaskRootSpec
    repository_identity: PathIdentity
    parent_identity: PathIdentity
    root_identity: PathIdentity
    git_pointer_sha256: str
    nodes: tuple[NodeObservation, ...]
    inventory_id: str


@dataclass(frozen=True)
class _StatStamp:
    identity: PathIdentity
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _ExpectedContext:
    repository_identity: PathIdentity
    parent_identity: PathIdentity
    root_identity: PathIdentity
    common_git_identity: PathIdentity
    private_admin_parent_identity: PathIdentity
    admin_identity: PathIdentity
    pointer_identity: PathIdentity
    pointer_sha256: str
    admin_gitdir_identity: PathIdentity
    admin_gitdir_sha256: str
    admin_commondir_identity: PathIdentity
    admin_commondir_sha256: str


@dataclass
class _OpenContext:
    repository_fd: int
    parent_fd: int
    root_fd: int
    common_git_fd: int
    private_admin_parent_fd: int
    admin_fd: int
    repository_identity: PathIdentity
    parent_identity: PathIdentity
    root_identity: PathIdentity
    common_git_identity: PathIdentity
    private_admin_parent_identity: PathIdentity
    admin_identity: PathIdentity
    pointer_identity: PathIdentity
    pointer_data: bytes
    admin_gitdir_identity: PathIdentity
    admin_gitdir_data: bytes
    admin_commondir_identity: PathIdentity
    admin_commondir_data: bytes
    mount_id: int


def _safe_error(code: str) -> safeResourceInspectionError:
    return safeResourceInspectionError(code)


def _validate_git_ref(value: object) -> bool:
    if type(value) is not str or len(value) > 1024 or not value.startswith("refs/heads/"):
        return False
    name = value[len("refs/heads/"):]
    if not name or name.startswith("/") or name.endswith("/") or name.endswith("."):
        return False
    if ".." in name or "@{" in name or "//" in name or name == "@":
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char in _GIT_REF_FORBIDDEN for char in name):
        return False
    for part in name.split("/"):
        if not part or part.startswith(".") or part.endswith(".lock"):
            return False
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _path_text(value: object) -> str:
    if type(value) is not str or not value or "\0" in value or value.startswith("//"):
        raise _safe_error("invalid_path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _safe_error("invalid_path") from None
    if len(encoded) > MAX_PATH_BYTES or not value.startswith("/"):
        raise _safe_error("invalid_path")
    path = Path(value)
    if str(path) != value or any(part in {".", ".."} for part in path.parts[1:]):
        raise _safe_error("noncanonical_path")
    return value


def _validate_spec(spec: object) -> TaskRootSpec:
    if type(spec) is not TaskRootSpec:
        raise _safe_error("invalid_spec")
    if (
        type(spec.repository) is not str
        or len(spec.repository) > 512
        or not _REPOSITORY.fullmatch(spec.repository)
        or type(spec.task) is not str
        or not _TASK.fullmatch(spec.task)
        or not _validate_git_ref(spec.branch_ref)
        or type(spec.head) is not str
        or not _OID.fullmatch(spec.head)
    ):
        raise _safe_error("invalid_spec")

    repository_root = Path(_path_text(spec.repository_root))
    worktree_root = Path(_path_text(spec.worktree_root))
    git_admin = Path(_path_text(spec.git_admin))
    if (
        worktree_root.parent.name != ".worktrees"
        or worktree_root.parent.parent != repository_root
        or worktree_root.name in {"", ".", "..", ".git"}
        or repository_root == worktree_root
        or git_admin == worktree_root
        or git_admin.is_relative_to(worktree_root)
        or git_admin.parent.name != "worktrees"
        or git_admin.parent.parent != repository_root / ".git"
        or git_admin.name in {"", ".", ".."}
    ):
        raise _safe_error("task_root_outside_managed_namespace")
    return spec


def _stat_tuple(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_uid),
        int(info.st_nlink), int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


class TaskResourceInspector:
    """Inspect only one spec-bound managed Task worktree using no-follow FDs.

    ``TaskRootSpec`` must be made by the facade from the current Git worktree
    registry.  This low-level type can prove current filesystem observations,
    but cannot establish Task ownership, dirty-state disposition, publication,
    or authority to remove anything.
    """

    def __init__(self, spec: TaskRootSpec) -> None:
        try:
            self._spec = _validate_spec(spec)
            self._expected: _ExpectedContext | None = None
            with self._open_context() as context:
                self._expected = self._expected_from(context)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("inspection_unavailable") from None

    @property
    def spec(self) -> TaskRootSpec:
        return self._spec

    def _dir_flags(self, *, noatime: bool = False) -> int:
        required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NOATIME")
        if not sys.platform.startswith("linux") or any(not hasattr(os, flag) for flag in required):
            raise _safe_error("unsupported_filesystem_proof")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        if noatime:
            flags |= os.O_NOATIME
        return flags

    def _mount_id(self, fd: int) -> int:
        """Return Linux mount identity; never degrade to ``st_dev`` alone."""
        if not sys.platform.startswith("linux"):
            raise _safe_error("unsupported_mount_proof")
        try:
            info_fd = os.open(f"/proc/self/fdinfo/{fd}", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                data = bytearray()
                while len(data) <= 4096:
                    chunk = os.read(info_fd, min(1024, 4097 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > 4096:
                    raise _safe_error("mount_proof_too_large")
            finally:
                os.close(info_fd)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("mount_proof_unavailable") from None
        matches = [match.group(1) for line in bytes(data).splitlines() if (match := _MOUNT_ID.fullmatch(line))]
        if len(matches) != 1:
            raise _safe_error("mount_proof_unavailable")
        try:
            return int(matches[0])
        except ValueError:
            raise _safe_error("mount_proof_unavailable") from None

    def _identity(self, fd: int) -> PathIdentity:
        try:
            info = os.fstat(fd)
            return PathIdentity(
                int(info.st_dev), int(info.st_ino), self._mount_id(fd), int(info.st_mode),
                int(info.st_uid), int(info.st_nlink),
            )
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None

    @staticmethod
    def _stamp(info: os.stat_result, mount_id: int) -> _StatStamp:
        return _StatStamp(
            PathIdentity(
                int(info.st_dev), int(info.st_ino), mount_id, int(info.st_mode),
                int(info.st_uid), int(info.st_nlink),
            ),
            int(info.st_mode), int(info.st_uid), int(info.st_nlink), int(info.st_size),
            int(info.st_mtime_ns), int(info.st_ctime_ns),
        )

    @staticmethod
    def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
        return _stat_tuple(left) == _stat_tuple(right)

    @staticmethod
    def _same_directory_stat(left: os.stat_result, right: os.stat_result) -> bool:
        """Compare a directory anchor without treating shared metadata as identity."""
        return (
            int(left.st_dev), int(left.st_ino), int(left.st_mode), int(left.st_uid)
        ) == (
            int(right.st_dev), int(right.st_ino), int(right.st_mode), int(right.st_uid)
        )

    @staticmethod
    def _same_directory_anchor(left: PathIdentity, right: PathIdentity) -> bool:
        """Compare a parent directory's stable identity, excluding link count.

        Removing the one bound root directory decrements its parent's nlink;
        ``root_absent`` accounts for exactly that expected transition below.
        All other identity fields remain pinned.
        """
        return (
            left.device, left.inode, left.mount_id, left.mode, left.uid
        ) == (
            right.device, right.inode, right.mount_id, right.mode, right.uid
        )

    @staticmethod
    def _assert_absent_name(parent_fd: int, name: str) -> None:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except Exception:
            raise _safe_error("absence_check_failed") from None
        raise _safe_error("entry_replaced")

    def _assert_named_fd(
        self, parent_fd: int, name: str, child_fd: int, *, anchor_only: bool = False,
    ) -> PathIdentity:
        probe_fd = -1
        same_stat = self._same_directory_stat if anchor_only else self._same_stat
        try:
            opened = os.fstat(child_fd)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            identity = self._identity(child_fd)
            if not stat.S_ISDIR(named.st_mode) or not same_stat(opened, named):
                raise _safe_error("entry_replaced")
            probe_fd = os.open(name, self._dir_flags(noatime=False), dir_fd=parent_fd)
            probe_info = os.fstat(probe_fd)
            probe_identity = self._identity(probe_fd)
            named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except Exception:
            raise _safe_error("entry_replaced") from None
        finally:
            if probe_fd >= 0:
                try:
                    os.close(probe_fd)
                except OSError:
                    pass
        if (
            not same_stat(opened, probe_info)
            or not same_stat(probe_info, named_after)
            or (
                not self._same_directory_anchor(probe_identity, identity)
                if anchor_only else probe_identity != identity
            )
        ):
            raise _safe_error("entry_replaced")
        return identity

    def _open_child_directory(
        self,
        parent_fd: int,
        name: str,
        expected_mount_id: int,
        *,
        noatime: bool = True,
        expected_info: os.stat_result | None = None,
    ) -> tuple[int, PathIdentity]:
        if type(name) is not str or not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise _safe_error("invalid_component")
        child_fd = -1
        keep_fd = False
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if expected_info is not None and not self._same_stat(before, expected_info):
                raise _safe_error("entry_replaced")
            if not stat.S_ISDIR(before.st_mode):
                raise _safe_error("unsafe_directory")
            child_fd = os.open(name, self._dir_flags(noatime=noatime), dir_fd=parent_fd)
            after = os.fstat(child_fd)
            if not self._same_stat(before, after):
                raise _safe_error("entry_replaced")
            identity = self._identity(child_fd)
            if identity.mount_id != expected_mount_id:
                raise _safe_error("mount_crossing")
            self._assert_named_fd(parent_fd, name, child_fd)
            keep_fd = True
            return child_fd, identity
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None
        finally:
            if child_fd >= 0 and not keep_fd:
                try:
                    os.close(child_fd)
                except OSError:
                    pass

    def _open_absolute_directory(self, path_text: str) -> tuple[int, PathIdentity]:
        """Open a canonical absolute directory without following any component."""
        path_text = _path_text(path_text)
        path = Path(path_text)
        stack: list[int] = []
        try:
            current = os.open("/", self._dir_flags(noatime=False))
            stack.append(current)
            for part in path.parts[1:]:
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise _safe_error("unsafe_path_component")
                child = os.open(part, self._dir_flags(noatime=False), dir_fd=current)
                stack.append(child)
                after = os.fstat(child)
                if not self._same_directory_stat(before, after):
                    raise _safe_error("entry_replaced")
                current = child
            # Verify the complete path still names the opened descriptor chain.
            for index in range(1, len(stack)):
                name = path.parts[index]
                self._assert_named_fd(stack[index - 1], name, stack[index], anchor_only=True)
            final_fd = stack[-1]
            identity = self._identity(final_fd)
            stack.pop()
            return final_fd, identity
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None
        finally:
            for fd in reversed(stack):
                try:
                    os.close(fd)
                except OSError:
                    pass

    @staticmethod
    def _close_all(fds: list[int]) -> None:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass

    def _read_regular_at(
        self,
        parent_fd: int,
        name: str,
        limit: int,
        expected_mount_id: int,
        *,
        expected_info: os.stat_result | None = None,
    ) -> tuple[bytes, PathIdentity, _StatStamp]:
        fd = -1
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if expected_info is not None and not self._same_stat(before, expected_info):
                raise _safe_error("entry_replaced")
            if not stat.S_ISREG(before.st_mode):
                raise _safe_error("unsupported_entry")
            if before.st_nlink != 1:
                raise _safe_error("hardlinked_file")
            if before.st_size < 0 or before.st_size > limit:
                raise _safe_error("file_size_limit")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOATIME
            fd = os.open(name, flags, dir_fd=parent_fd)
            opened = os.fstat(fd)
            if not self._same_stat(before, opened):
                raise _safe_error("entry_replaced")
            identity = self._identity(fd)
            if identity.mount_id != expected_mount_id:
                raise _safe_error("mount_crossing")
            digest = bytearray()
            while len(digest) <= limit:
                chunk = os.read(fd, min(64 * 1024, limit + 1 - len(digest)))
                if not chunk:
                    break
                digest.extend(chunk)
            if len(digest) > limit or len(digest) != opened.st_size:
                raise _safe_error("file_size_limit")
            after = os.fstat(fd)
            if not self._same_stat(opened, after):
                raise _safe_error("entry_changed_during_read")
            named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not self._same_stat(after, named_after):
                raise _safe_error("entry_replaced")
            return bytes(digest), identity, self._stamp(after, identity.mount_id)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _reference_matches(self, data: bytes, *, prefix: bytes | None, base: str, expected: str) -> bool:
        if prefix is not None:
            if not data.startswith(prefix):
                return False
            data = data[len(prefix):]
        if not data.endswith(b"\n") or data.count(b"\n") != 1 or b"\0" in data or b"\r" in data:
            return False
        try:
            value = data[:-1].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        if not value:
            return False
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(base) / candidate
        try:
            normalized = os.path.normpath(str(candidate))
        except (TypeError, ValueError):
            return False
        return normalized == expected

    @contextmanager
    def _open_context(self) -> Iterator[_OpenContext]:
        """Open protected roots/admin and validate the reciprocal Git pointer."""
        fds: list[int] = []
        try:
            repository_fd, repository_identity = self._open_absolute_directory(self._spec.repository_root)
            fds.append(repository_fd)
            mount_id = repository_identity.mount_id
            common_git_fd, common_git_identity = self._open_child_directory(
                repository_fd, ".git", mount_id,
            )
            fds.append(common_git_fd)
            private_admin_parent_fd, private_admin_parent_identity = self._open_child_directory(
                common_git_fd, "worktrees", mount_id,
            )
            fds.append(private_admin_parent_fd)
            admin_name = Path(self._spec.git_admin).name
            admin_fd, admin_identity = self._open_child_directory(
                private_admin_parent_fd, admin_name, mount_id,
            )
            fds.append(admin_fd)
            parent_fd, parent_identity = self._open_child_directory(
                repository_fd, ".worktrees", mount_id,
            )
            fds.append(parent_fd)
            root_fd, root_identity = self._open_child_directory(
                parent_fd, Path(self._spec.worktree_root).name, mount_id,
            )
            fds.append(root_fd)

            pointer_data, pointer_identity, _pointer_stamp = self._read_regular_at(
                root_fd, ".git", MAX_GIT_POINTER_BYTES, mount_id,
            )
            if not self._reference_matches(
                pointer_data, prefix=b"gitdir: ", base=self._spec.worktree_root,
                expected=self._spec.git_admin,
            ):
                raise _safe_error("git_pointer_mismatch")
            admin_gitdir_data, admin_gitdir_identity, _ = self._read_regular_at(
                admin_fd, "gitdir", MAX_GIT_POINTER_BYTES, mount_id,
            )
            if not self._reference_matches(
                admin_gitdir_data, prefix=None, base=self._spec.git_admin,
                expected=f"{self._spec.worktree_root}/.git",
            ):
                raise _safe_error("git_admin_backlink_mismatch")
            admin_commondir_data, admin_commondir_identity, _ = self._read_regular_at(
                admin_fd, "commondir", MAX_GIT_POINTER_BYTES, mount_id,
            )
            if not self._reference_matches(
                admin_commondir_data, prefix=None, base=self._spec.git_admin,
                expected=f"{self._spec.repository_root}/.git",
            ):
                raise _safe_error("git_common_dir_mismatch")

            context = _OpenContext(
                repository_fd, parent_fd, root_fd, common_git_fd, private_admin_parent_fd,
                admin_fd, repository_identity, parent_identity, root_identity,
                common_git_identity, private_admin_parent_identity, admin_identity,
                pointer_identity, pointer_data, admin_gitdir_identity, admin_gitdir_data,
                admin_commondir_identity, admin_commondir_data, mount_id,
            )
            self._assert_open_context_names(context)
            if self._expected is not None:
                self._assert_expected(context)
            yield context
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None
        finally:
            self._close_all(fds)

    def _assert_open_context_names(self, context: _OpenContext) -> None:
        try:
            if self._assert_named_fd(
                context.repository_fd, ".git", context.common_git_fd,
            ) != context.common_git_identity:
                raise _safe_error("context_changed")
            if self._assert_named_fd(
                context.common_git_fd, "worktrees", context.private_admin_parent_fd,
            ) != context.private_admin_parent_identity:
                raise _safe_error("context_changed")
            if self._assert_named_fd(
                context.private_admin_parent_fd, Path(self._spec.git_admin).name, context.admin_fd,
            ) != context.admin_identity:
                raise _safe_error("context_changed")
            if self._assert_named_fd(
                context.repository_fd, ".worktrees", context.parent_fd,
            ) != context.parent_identity:
                raise _safe_error("context_changed")
            if self._assert_named_fd(
                context.parent_fd, Path(self._spec.worktree_root).name, context.root_fd,
            ) != context.root_identity:
                raise _safe_error("context_changed")
            pointer_data, pointer_identity, _ = self._read_regular_at(
                context.root_fd, ".git", MAX_GIT_POINTER_BYTES, context.mount_id,
            )
            admin_gitdir, admin_gitdir_identity, _ = self._read_regular_at(
                context.admin_fd, "gitdir", MAX_GIT_POINTER_BYTES, context.mount_id,
            )
            admin_commondir, admin_commondir_identity, _ = self._read_regular_at(
                context.admin_fd, "commondir", MAX_GIT_POINTER_BYTES, context.mount_id,
            )
            if (
                pointer_data != context.pointer_data
                or pointer_identity != context.pointer_identity
                or admin_gitdir != context.admin_gitdir_data
                or admin_gitdir_identity != context.admin_gitdir_identity
                or admin_commondir != context.admin_commondir_data
                or admin_commondir_identity != context.admin_commondir_identity
                or not self._reference_matches(
                    pointer_data, prefix=b"gitdir: ", base=self._spec.worktree_root,
                    expected=self._spec.git_admin,
                )
                or not self._reference_matches(
                    admin_gitdir, prefix=None, base=self._spec.git_admin,
                    expected=f"{self._spec.worktree_root}/.git",
                )
                or not self._reference_matches(
                    admin_commondir, prefix=None, base=self._spec.git_admin,
                    expected=f"{self._spec.repository_root}/.git",
                )
            ):
                raise _safe_error("git_metadata_changed")
            current_repo_fd, current_repo_identity = self._open_absolute_directory(self._spec.repository_root)
            try:
                if current_repo_identity != context.repository_identity:
                    raise _safe_error("repository_root_changed")
            finally:
                os.close(current_repo_fd)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("context_changed") from None

    def _expected_from(self, context: _OpenContext) -> _ExpectedContext:
        return _ExpectedContext(
            context.repository_identity,
            context.parent_identity,
            context.root_identity,
            context.common_git_identity,
            context.private_admin_parent_identity,
            context.admin_identity,
            context.pointer_identity,
            hashlib.sha256(context.pointer_data).hexdigest(),
            context.admin_gitdir_identity,
            hashlib.sha256(context.admin_gitdir_data).hexdigest(),
            context.admin_commondir_identity,
            hashlib.sha256(context.admin_commondir_data).hexdigest(),
        )

    def _assert_expected(self, context: _OpenContext) -> None:
        expected = self._expected
        if expected is None:
            raise _safe_error("context_unbound")
        actual = self._expected_from(context)
        if actual != expected:
            raise _safe_error("context_changed")

    @staticmethod
    def _identity_wire(identity: PathIdentity) -> dict[str, int]:
        return {
            "device": identity.device,
            "inode": identity.inode,
            "mount_id": identity.mount_id,
            "mode": identity.mode,
            "uid": identity.uid,
            "nlink": identity.nlink,
        }

    def _inventory_digest(
        self,
        repository_identity: PathIdentity,
        parent_identity: PathIdentity,
        root_identity: PathIdentity,
        git_pointer_sha256: str,
        nodes: tuple[NodeObservation, ...],
    ) -> str:
        payload = {
            "spec": {
                "repository": self._spec.repository,
                "task": self._spec.task,
                "branch_ref": self._spec.branch_ref,
                "head": self._spec.head,
                "repository_root": self._spec.repository_root,
                "worktree_root": self._spec.worktree_root,
                "git_admin": self._spec.git_admin,
            },
            "repository_identity": self._identity_wire(repository_identity),
            "parent_identity": self._identity_wire(parent_identity),
            "root_identity": self._identity_wire(root_identity),
            "git_pointer_sha256": git_pointer_sha256,
            "nodes": [
                {
                    "relative_path": node.relative_path,
                    "kind": node.kind,
                    "identity": self._identity_wire(node.identity),
                    "size": node.size,
                    "mtime_ns": node.mtime_ns,
                    "ctime_ns": node.ctime_ns,
                    "content_sha256": node.content_sha256,
                }
                for node in nodes
            ],
        }
        try:
            encoded = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise _safe_error("inventory_encoding_failed") from None
        if len(encoded) > MAX_INVENTORY_BYTES:
            raise _safe_error("inventory_output_limit")
        return hashlib.sha256(_INVENTORY_DOMAIN + encoded).hexdigest()

    def _directory_names(self, fd: int) -> tuple[str, ...]:
        try:
            before = os.fstat(fd)
            names: list[str] = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        encoded = name.encode("utf-8", errors="strict")
                    except (AttributeError, UnicodeEncodeError):
                        raise _safe_error("invalid_filename") from None
                    if (
                        not name or name in {".", ".."} or "/" in name or "\0" in name
                        or len(encoded) > MAX_PATH_BYTES
                    ):
                        raise _safe_error("invalid_filename")
                    names.append(name)
            after = os.fstat(fd)
            if not self._same_stat(before, after):
                raise _safe_error("directory_changed_during_read")
            ordered = tuple(sorted(names, key=lambda value: value.encode("utf-8")))
            if len(ordered) != len(set(ordered)):
                raise _safe_error("duplicate_filename")
            return ordered
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None

    def _stamp_fd(self, fd: int) -> _StatStamp:
        try:
            info = os.fstat(fd)
            identity = self._identity(fd)
            return self._stamp(info, identity.mount_id)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("filesystem_unavailable") from None

    @staticmethod
    def _stamp_from_stat(info: os.stat_result, mount_id: int) -> _StatStamp:
        return TaskResourceInspector._stamp(info, mount_id)

    def _assert_named_stamp(
        self, parent_fd: int, name: str, stamp: _StatStamp, mount_id: int,
    ) -> None:
        fd = -1
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not self._same_stamp(self._stamp_from_stat(current, mount_id), stamp):
                raise _safe_error("entry_replaced")
            if stat.S_ISDIR(current.st_mode):
                fd = os.open(name, self._dir_flags(noatime=True), dir_fd=parent_fd)
            elif stat.S_ISREG(current.st_mode) and current.st_nlink == 1:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOATIME,
                    dir_fd=parent_fd,
                )
            else:
                raise _safe_error("unsupported_entry")
            opened = os.fstat(fd)
            identity = self._identity(fd)
            named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except Exception:
            raise _safe_error("entry_replaced") from None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if (
            not self._same_stat(current, opened)
            or not self._same_stat(opened, named_after)
            or identity != stamp.identity
        ):
            raise _safe_error("entry_replaced")

    @staticmethod
    def _same_stamp(left: _StatStamp, right: _StatStamp) -> bool:
        return left == right

    def _walk_directory(
        self,
        fd: int,
        prefix: str,
        depth: int,
        mount_id: int,
        nodes: list[NodeObservation],
        total_bytes: list[int],
    ) -> None:
        directory_before = self._stamp_fd(fd)
        names_before = self._directory_names(fd)
        children: dict[str, _StatStamp] = {}
        for name in names_before:
            if name == ".git":
                if not prefix:
                    # The only non-inventory entry: separately validated,
                    # protected regular linked-worktree pointer.
                    continue
                raise _safe_error("nested_git_metadata")
            relative_path = f"{prefix}/{name}" if prefix else name
            if len(relative_path.encode("utf-8", errors="strict")) > MAX_PATH_BYTES:
                raise _safe_error("path_size_limit")
            if depth + 1 > MAX_DEPTH:
                raise _safe_error("directory_depth_limit")
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except Exception:
                raise _safe_error("entry_changed_during_read") from None
            if stat.S_ISLNK(before.st_mode):
                raise _safe_error("symlink_entry")
            if stat.S_ISDIR(before.st_mode):
                child_depth = depth + 1
                if child_depth > MAX_DEPTH:
                    raise _safe_error("directory_depth_limit")
                if len(nodes) >= MAX_NODES:
                    raise _safe_error("node_count_limit")
                child_fd, identity = self._open_child_directory(
                    fd, name, mount_id, noatime=True, expected_info=before,
                )
                try:
                    child_stamp = self._stamp_fd(child_fd)
                    self._walk_directory(
                        child_fd, relative_path, child_depth, mount_id, nodes, total_bytes,
                    )
                    after_stamp = self._stamp_fd(child_fd)
                    if child_stamp != after_stamp:
                        raise _safe_error("directory_changed_during_read")
                    node = NodeObservation(
                        relative_path, "directory", identity, child_stamp.size,
                        child_stamp.mtime_ns, child_stamp.ctime_ns, None,
                    )
                    self._assert_named_stamp(fd, name, child_stamp, mount_id)
                    children[name] = child_stamp
                finally:
                    os.close(child_fd)
                nodes.append(node)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise _safe_error("hardlinked_file")
                if before.st_size < 0 or before.st_size > MAX_FILE_BYTES:
                    raise _safe_error("file_size_limit")
                total_bytes[0] += int(before.st_size)
                if total_bytes[0] > MAX_TOTAL_FILE_BYTES:
                    raise _safe_error("aggregate_file_size_limit")
                if len(nodes) >= MAX_NODES:
                    raise _safe_error("node_count_limit")
                content, identity, stamp = self._read_regular_at(
                    fd, name, MAX_FILE_BYTES, mount_id, expected_info=before,
                )
                if len(content) != before.st_size:
                    raise _safe_error("entry_changed_during_read")
                node = NodeObservation(
                    relative_path, "file", identity, stamp.size, stamp.mtime_ns,
                    stamp.ctime_ns, hashlib.sha256(content).hexdigest(),
                )
                del content
                children[name] = stamp
                nodes.append(node)
            else:
                # FIFOs, sockets, devices, and every unknown node type are not
                # safe inventory objects.
                raise _safe_error("unsupported_entry")

        names_after = self._directory_names(fd)
        directory_after = self._stamp_fd(fd)
        if names_before != names_after or directory_before != directory_after:
            raise _safe_error("directory_changed_during_read")
        for name, stamp in children.items():
            self._assert_named_stamp(fd, name, stamp, mount_id)

    def inventory(self) -> WorktreeInventory:
        try:
            with self._open_context() as context:
                nodes_list: list[NodeObservation] = []
                self._walk_directory(
                    context.root_fd, "", 0, context.mount_id, nodes_list, [0],
                )
                # Directory nodes were appended after their descendants.  The
                # wire/API order is a stable lexical order over exact paths.
                nodes = tuple(sorted(nodes_list, key=lambda node: node.relative_path.encode("utf-8")))
                if len(nodes) > MAX_NODES:
                    raise _safe_error("node_count_limit")
                self._assert_open_context_names(context)
                pointer_data, pointer_identity, _ = self._read_regular_at(
                    context.root_fd, ".git", MAX_GIT_POINTER_BYTES, context.mount_id,
                )
                if (
                    pointer_data != context.pointer_data
                    or pointer_identity != context.pointer_identity
                    or not self._reference_matches(
                        pointer_data, prefix=b"gitdir: ", base=self._spec.worktree_root,
                        expected=self._spec.git_admin,
                    )
                ):
                    raise _safe_error("git_pointer_changed")
                git_pointer_sha256 = hashlib.sha256(pointer_data).hexdigest()
                inventory_id = self._inventory_digest(
                    context.repository_identity, context.parent_identity,
                    context.root_identity, git_pointer_sha256, nodes,
                )
                return WorktreeInventory(
                    self._spec, context.repository_identity, context.parent_identity,
                    context.root_identity, git_pointer_sha256, nodes, inventory_id,
                )
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("inspection_failed") from None

    def revalidate(self, expected: WorktreeInventory) -> WorktreeInventory:
        try:
            if type(expected) is not WorktreeInventory or expected.spec != self._spec:
                raise _safe_error("inventory_binding_mismatch")
            current = self.inventory()
            if current != expected:
                raise _safe_error("inventory_changed")
            return current
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("revalidation_failed") from None

    @staticmethod
    def _validate_relative_path(relative_path: object) -> tuple[str, ...]:
        if type(relative_path) is not str or not relative_path or "\0" in relative_path:
            raise _safe_error("invalid_relative_path")
        try:
            encoded = relative_path.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _safe_error("invalid_relative_path") from None
        if (
            len(encoded) > MAX_PATH_BYTES
            or relative_path.startswith("/")
            or relative_path.endswith("/")
            or "\\" in relative_path
            or any(char in relative_path for char in "*?[]{}$~`")
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in relative_path)
        ):
            raise _safe_error("invalid_relative_path")
        parts = tuple(relative_path.split("/"))
        if any(not part or part in {".", "..", ".git"} for part in parts):
            raise _safe_error("invalid_relative_path")
        return parts

    def relative_nodes(
        self, inventory: WorktreeInventory, relative_path: str,
    ) -> tuple[NodeObservation, ...]:
        try:
            self.revalidate(inventory)
            self._validate_relative_path(relative_path)
            selected = tuple(
                node for node in inventory.nodes
                if node.relative_path == relative_path
                or node.relative_path.startswith(relative_path + "/")
            )
            if not selected:
                raise _safe_error("relative_path_absent")
            target = next((node for node in selected if node.relative_path == relative_path), None)
            if target is None:
                raise _safe_error("relative_path_absent")
            if target.kind == "file" and len(selected) != 1:
                raise _safe_error("invalid_manifest")
            return tuple(sorted(selected, key=lambda node: node.relative_path.encode("utf-8")))
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("relative_inspection_failed") from None

    def _open_existing_entry(
        self, parent_fd: int, name: str, mount_id: int,
    ) -> None:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except Exception:
            raise _safe_error("entry_changed_during_read") from None
        if stat.S_ISLNK(info.st_mode):
            raise _safe_error("symlink_entry")
        if stat.S_ISDIR(info.st_mode):
            fd, identity = self._open_child_directory(
                parent_fd, name, mount_id, noatime=True, expected_info=info,
            )
            try:
                self._assert_named_fd(parent_fd, name, fd)
                if identity.mount_id != mount_id:
                    raise _safe_error("mount_crossing")
            finally:
                os.close(fd)
            return
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise _safe_error("hardlinked_file")
            fd = -1
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOATIME,
                    dir_fd=parent_fd,
                )
                opened = os.fstat(fd)
                identity = self._identity(fd)
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not self._same_stat(opened, info)
                    or not self._same_stat(named, opened)
                    or identity.mount_id != mount_id
                ):
                    raise _safe_error("entry_replaced")
            except safeResourceInspectionError:
                raise
            except Exception:
                raise _safe_error("filesystem_unavailable") from None
            finally:
                if fd >= 0:
                    os.close(fd)
            return
        raise _safe_error("unsupported_entry")

    def is_absent(self, relative_path: str) -> bool:
        try:
            parts = self._validate_relative_path(relative_path)
            with self._open_context() as context:
                parent_fd = context.root_fd
                opened: list[int] = []
                named_chain: list[tuple[int, str, int]] = []
                try:
                    for part in parts[:-1]:
                        child_fd, _identity = self._open_child_directory(
                            parent_fd, part, context.mount_id, noatime=True,
                        )
                        opened.append(child_fd)
                        named_chain.append((parent_fd, part, child_fd))
                        parent_fd = child_fd
                    final = parts[-1]
                    try:
                        os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        for named_parent, name, child_fd in named_chain:
                            self._assert_named_fd(named_parent, name, child_fd)
                        self._assert_open_context_names(context)
                        self._assert_absent_name(parent_fd, final)
                        for named_parent, name, child_fd in named_chain:
                            self._assert_named_fd(named_parent, name, child_fd)
                        self._assert_open_context_names(context)
                        self._assert_absent_name(parent_fd, final)
                        return True
                    except Exception:
                        raise _safe_error("filesystem_unavailable") from None
                    self._open_existing_entry(parent_fd, final, context.mount_id)
                    for named_parent, name, child_fd in named_chain:
                        self._assert_named_fd(named_parent, name, child_fd)
                    self._assert_open_context_names(context)
                    return False
                finally:
                    self._close_all(opened)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("absence_check_failed") from None

    def root_absent(self) -> bool:
        """Prove only exact absence under the original parent; replacements fail."""
        try:
            expected = self._expected
            if expected is None:
                raise _safe_error("context_unbound")
            fds: list[int] = []
            try:
                repository_fd, repository_identity = self._open_absolute_directory(self._spec.repository_root)
                fds.append(repository_fd)
                if repository_identity != expected.repository_identity:
                    raise _safe_error("repository_root_changed")
                mount_id = repository_identity.mount_id
                parent_fd, parent_identity = self._open_child_directory(
                    repository_fd, ".worktrees", mount_id,
                )
                fds.append(parent_fd)
                if not self._same_directory_anchor(parent_identity, expected.parent_identity):
                    raise _safe_error("task_parent_changed")
                root_name = Path(self._spec.worktree_root).name
                try:
                    info = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if parent_identity.nlink != expected.parent_identity.nlink - 1:
                        raise _safe_error("task_parent_changed")
                    current_repo_fd, current_repo_identity = self._open_absolute_directory(
                        self._spec.repository_root,
                    )
                    try:
                        if current_repo_identity != expected.repository_identity:
                            raise _safe_error("repository_root_changed")
                        if self._assert_named_fd(current_repo_fd, ".worktrees", parent_fd) != parent_identity:
                            raise _safe_error("task_parent_changed")
                        self._assert_absent_name(parent_fd, root_name)
                        if self._assert_named_fd(current_repo_fd, ".worktrees", parent_fd) != parent_identity:
                            raise _safe_error("task_parent_changed")
                        self._assert_absent_name(parent_fd, root_name)
                    finally:
                        os.close(current_repo_fd)
                    return True
                except Exception:
                    raise _safe_error("filesystem_unavailable") from None
                if not stat.S_ISDIR(info.st_mode):
                    raise _safe_error("root_replaced")
                root_fd, root_identity = self._open_child_directory(
                    parent_fd, root_name, mount_id, noatime=True, expected_info=info,
                )
                fds.append(root_fd)
                if root_identity != expected.root_identity:
                    raise _safe_error("root_replaced")
                pointer, pointer_identity, _ = self._read_regular_at(
                    root_fd, ".git", MAX_GIT_POINTER_BYTES, mount_id,
                )
                if (
                    pointer_identity != expected.pointer_identity
                    or hashlib.sha256(pointer).hexdigest() != expected.pointer_sha256
                    or not self._reference_matches(
                        pointer, prefix=b"gitdir: ", base=self._spec.worktree_root,
                        expected=self._spec.git_admin,
                    )
                ):
                    raise _safe_error("git_pointer_changed")
                if (
                    parent_identity != expected.parent_identity
                    or self._assert_named_fd(parent_fd, root_name, root_fd) != expected.root_identity
                    or self._assert_named_fd(repository_fd, ".worktrees", parent_fd)
                    != expected.parent_identity
                ):
                    raise _safe_error("task_parent_changed")
                current_repo_fd, current_repo_identity = self._open_absolute_directory(
                    self._spec.repository_root,
                )
                try:
                    if current_repo_identity != expected.repository_identity:
                        raise _safe_error("repository_root_changed")
                    if self._assert_named_fd(current_repo_fd, ".worktrees", parent_fd) != expected.parent_identity:
                        raise _safe_error("task_parent_changed")
                    self._assert_named_fd(parent_fd, root_name, root_fd)
                finally:
                    os.close(current_repo_fd)
                return False
            finally:
                self._close_all(fds)
        except safeResourceInspectionError:
            raise
        except Exception:
            raise _safe_error("absence_check_failed") from None
