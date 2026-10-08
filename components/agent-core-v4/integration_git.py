"""Bounded Git facts and a host-capability fast-forward for the default branch.

This module is intentionally not a general Git executor.  Reads use the
MetadataStore's private, scrubbed Git runner.  Fetching and updating the
checked-out default worktree are separate trusted host capabilities.  In
particular, ``fast_forward`` is not implemented here: its host must pin the
repository/admin directory, direct default ref, expected old OID and worktree,
then perform a data-safe, locked fast-forward only.  A bare ``git merge
--ff-only`` is not by itself a worktree compare-and-swap guarantee.

There is no push, force-update, reset, rebase, cleanup, metadata-GC, or
lifecycle API in this module.  The fetch capability may make the exact observed
remote OID and merge OID available but must not update refs or FETCH_HEAD.  Its
return value is not evidence that the fetch happened;
reconciliation verifies the actual remote ref and commit graph.  Likewise, a
fast-forward callback acknowledgement is not evidence of success; the local
postcondition is read back before returning a receipt.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from bisect import bisect_left
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from metadata_ref import METADATA_REF, MetadataRefError, MetadataStore


MAX_GIT_OUTPUT = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_CHANGED_PATHS = 50_000
MAX_CHANGED_BYTES = 8 * 1024 * 1024
MAX_IGNORED_PATHS = 100_000
MAX_IGNORED_BYTES = 16 * 1024 * 1024
MAX_HISTORY_COMMITS = 100_000
MAX_COMMIT_BYTES = 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_GIT_SECONDS = 60.0

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_INDEX_DOMAIN = b"agentcore-default-git-index/v1\n"
_STATUS_DOMAIN = b"agentcore-default-git-status/v1\n"
_OPERATIONS = frozenset({
    "construct", "observe", "remote_head", "commit_facts", "is_ancestor", "reconcile",
})
_REGULAR_MODES = frozenset({b"100644", b"100755"})
_TREE_MODES = {
    b"100644": b"blob",
    b"100755": b"blob",
    b"120000": b"blob",
    b"160000": b"commit",
}


class SafeDefaultGitError(RuntimeError):
    """A safe, bounded Git failure code and fixed operation name."""

    def __init__(self, code: str, operation: str) -> None:
        self.code = code if type(code) is str and _SAFE_CODE.fullmatch(code) else "default_git_error"
        self.operation = operation if operation in _OPERATIONS else "observe"
        super().__init__(f"{self.operation}:{self.code}")


@dataclass(frozen=True)
class DefaultFacts:
    repository: str
    branch_ref: str
    head: str
    tree: str
    worktree: str
    clean: bool
    index_fingerprint: str
    status_fingerprint: str


@dataclass(frozen=True)
class CommitFacts:
    oid: str
    tree: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class FetchRequest:
    repository: str
    branch_ref: str
    expected_remote: str
    merged_oid: str


@dataclass(frozen=True)
class FastForwardRequest:
    repository: str
    branch_ref: str
    worktree: str
    expected_head: str
    target_head: str
    merged_oid: str
    target_tree: str
    expected_index_fingerprint: str
    expected_status_fingerprint: str
    target_index_fingerprint: str
    changed_paths: tuple[bytes, ...]


@dataclass(frozen=True)
class DefaultReceipt:
    repository: str
    branch_ref: str
    worktree: str
    old_head: str
    new_head: str
    merged_oid: str
    tree: str


@dataclass(frozen=True)
class _TreeEntry:
    mode: bytes
    oid: str


@dataclass(frozen=True)
class _SideState:
    refs: tuple[tuple[bytes, str, bytes], ...]
    fetch_head: bytes | None
    config: tuple[tuple[str, bytes], ...]


class DefaultGit:
    """Observe and reconcile one pinned, checked-out default-branch worktree.

    ``fetch`` receives only the repository identity, exact full default ref,
    previously observed remote OID, and approved merge OID.  It may fetch only
    those exact commits/objects and must not change refs or FETCH_HEAD.
    ``fast_forward`` is a
    trusted host capability, not an ordinary ambient ``git merge`` fallback;
    it must compare the pinned local branch/worktree against
    ``expected_head`` and apply only a data-safe fast-forward to ``target_head``.
    """

    def __init__(
        self,
        store: MetadataStore,
        *,
        default_branch_ref: str,
        fetch: Callable[[FetchRequest], object],
        fast_forward: Callable[[FastForwardRequest], object],
    ) -> None:
        operation = "construct"
        if not isinstance(store, MetadataStore):
            raise SafeDefaultGitError("invalid_store", operation)
        if not callable(fetch) or not callable(fast_forward):
            raise SafeDefaultGitError("invalid_capability", operation)
        if (
            type(default_branch_ref) is not str
            or len(default_branch_ref) > 1024
            or not default_branch_ref.startswith("refs/heads/")
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in default_branch_ref)
        ):
            raise SafeDefaultGitError("invalid_branch_ref", operation)

        self._store = store
        self._root = store.root
        self._repository = store.repository
        self._remote = store.remote
        self._default_branch_ref = default_branch_ref
        self._fetch = fetch
        self._fast_forward = fast_forward
        self._operation_name: ContextVar[str] = ContextVar(
            f"agentcore_default_git_operation_{id(self)}", default=operation,
        )
        self._root_identity: tuple[int, int] | None = None
        self._git_entry_identity: tuple[int, int] | None = None
        self._git_entry_mode: int | None = None
        self._git_entry_data: bytes | None = None
        self._preliminary_admin_path: Path | None = None
        self._preliminary_admin_identity: tuple[int, int] | None = None
        self._preliminary_common_path: Path | None = None
        self._preliminary_common_identity: tuple[int, int] | None = None
        self._admin_path: Path | None = None
        self._common_path: Path | None = None
        self._admin_identity: tuple[int, int] | None = None
        self._common_identity: tuple[int, int] | None = None
        self._oid_length = getattr(store, "_oid_length", -1)
        if self._oid_length not in {40, 64}:
            raise SafeDefaultGitError("unsupported_object_format", operation)
        if type(self._repository) is not str or not self._repository:
            raise SafeDefaultGitError("invalid_repository_binding", operation)

        try:
            self._check_root_path()
            root_info = self._root.lstat()
            self._root_identity = (root_info.st_dev, root_info.st_ino)
            git_entry = self._root / ".git"
            git_info = git_entry.lstat()
            if stat.S_ISLNK(git_info.st_mode) or not (
                stat.S_ISDIR(git_info.st_mode) or stat.S_ISREG(git_info.st_mode)
            ):
                raise SafeDefaultGitError("unsafe_git_pointer", operation)
            self._git_entry_identity = (git_info.st_dev, git_info.st_ino)
            self._git_entry_mode = stat.S_IFMT(git_info.st_mode)
            if stat.S_ISREG(git_info.st_mode):
                self._git_entry_data = self._read_regular(git_entry, 4096, operation)
                if not self._git_entry_data.startswith(b"gitdir: "):
                    raise SafeDefaultGitError("invalid_git_pointer", operation)
                preliminary = self._linked_path_value(
                    self._git_entry_data[len(b"gitdir: "):], self._root, operation,
                )
                self._check_path_components(preliminary, operation)
                preliminary = preliminary.resolve(strict=True)
            else:
                preliminary = git_entry
            preliminary_info = preliminary.lstat()
            if stat.S_ISLNK(preliminary_info.st_mode) or not stat.S_ISDIR(preliminary_info.st_mode):
                raise SafeDefaultGitError("unsafe_git_administration", operation)
            self._preliminary_admin_path = preliminary
            self._preliminary_admin_identity = (preliminary_info.st_dev, preliminary_info.st_ino)
            if preliminary == git_entry:
                preliminary_common = preliminary
            else:
                if preliminary.parent.name != "worktrees":
                    raise SafeDefaultGitError("git_worktree_admin_mismatch", operation)
                preliminary_common = preliminary.parent.parent
            self._check_path_components(preliminary_common, operation)
            common_info = preliminary_common.lstat()
            if stat.S_ISLNK(common_info.st_mode) or not stat.S_ISDIR(common_info.st_mode):
                raise SafeDefaultGitError("unsafe_git_administration", operation)
            self._preliminary_common_path = preliminary_common
            self._preliminary_common_identity = (common_info.st_dev, common_info.st_ino)
            self._check_common_config_path(operation)
            self._initial_config_safety()
            self._check_worktree_state_paths(operation)
            self._validate_ref_format(default_branch_ref, operation)
            object_format = self._initial_git(["rev-parse", "--show-object-format"], 128)
            if object_format.strip() not in {b"sha1", b"sha256"}:
                raise SafeDefaultGitError("unsupported_object_format", operation)
            if (object_format.strip() == b"sha1") != (self._oid_length == 40):
                raise SafeDefaultGitError("object_format_mismatch", operation)
            top = self._initial_git(["rev-parse", "--show-toplevel"], 16 * 1024)
            if self._decode_text(top, operation) != str(self._root):
                raise SafeDefaultGitError("worktree_identity_mismatch", operation)
            admin = self._decode_path(self._initial_git(["rev-parse", "--absolute-git-dir"], 16 * 1024))
            common = self._decode_path(self._initial_git([
                "rev-parse", "--path-format=absolute", "--git-common-dir",
            ], 16 * 1024))
            if admin != self._preliminary_admin_path or common != self._preliminary_common_path:
                raise SafeDefaultGitError("git_admin_path_mismatch", operation)
            if admin.resolve(strict=True) != admin or common.resolve(strict=True) != common:
                raise SafeDefaultGitError("unsafe_git_administration", operation)
            admin_info = admin.lstat()
            common_info = common.lstat()
            if (
                stat.S_ISLNK(admin_info.st_mode)
                or stat.S_ISLNK(common_info.st_mode)
                or not stat.S_ISDIR(admin_info.st_mode)
                or not stat.S_ISDIR(common_info.st_mode)
                or (admin_info.st_dev, admin_info.st_ino) != self._preliminary_admin_identity
                or (common_info.st_dev, common_info.st_ino) != self._preliminary_common_identity
            ):
                raise SafeDefaultGitError("unsafe_git_administration", operation)
            self._admin_path = admin
            self._common_path = common
            self._admin_identity = (admin_info.st_dev, admin_info.st_ino)
            self._common_identity = (common_info.st_dev, common_info.st_ino)
            self._verify_admin_layout(operation)
            self._assert_filesystem_identity(operation)
            self._config_safety()
            self._reject_incomplete_history()
            self._reject_sparse_checkout()
            self._observe_once()
        except SafeDefaultGitError:
            raise
        except Exception:
            raise SafeDefaultGitError("repository_unavailable", operation) from None

    @property
    def store(self) -> MetadataStore:
        return self._store

    @property
    def root(self) -> Path:
        return self._root

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def default_branch_ref(self) -> str:
        return self._default_branch_ref

    @contextmanager
    def _operation(self, operation: str) -> Iterator[None]:
        token = self._operation_name.set(operation)
        try:
            with self._store.validation_scope():
                self._assert_filesystem_identity(operation)
                yield
                self._assert_filesystem_identity(operation)
        except SafeDefaultGitError as error:
            if error.operation == operation:
                raise
            raise SafeDefaultGitError(error.code, operation) from None
        except Exception:
            raise SafeDefaultGitError("operation_failed", operation) from None
        finally:
            self._operation_name.reset(token)

    def _initial_git(self, arguments: list[str], max_stdout: int) -> bytes:
        self._check_root_path()
        self._check_git_entry_stable("construct")
        self._check_worktree_state_paths("construct")
        self._check_common_config_path("construct")
        try:
            output = self._store._git(
                arguments,
                max_stdout=max_stdout,
                timeout=MAX_GIT_SECONDS,
                extra_env={"GIT_OPTIONAL_LOCKS": "0"},
            )
        except (MetadataRefError, OSError, ValueError):
            raise SafeDefaultGitError("git_operation_failed", "construct") from None
        self._check_root_path()
        self._check_git_entry_stable("construct")
        self._check_worktree_state_paths("construct")
        self._check_common_config_path("construct")
        if output is None:
            raise SafeDefaultGitError("git_operation_failed", "construct")
        return output

    def _git(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        max_stdout: int = MAX_GIT_OUTPUT,
    ) -> bytes | None:
        operation = self._operation_name.get()
        self._assert_filesystem_identity(operation)
        try:
            output = self._store._git(
                arguments,
                check=check,
                max_stdout=max_stdout,
                timeout=MAX_GIT_SECONDS,
                extra_env={"GIT_OPTIONAL_LOCKS": "0"},
            )
        except (MetadataRefError, OSError, ValueError):
            raise SafeDefaultGitError("git_operation_failed", operation) from None
        self._assert_filesystem_identity(operation)
        return output

    @staticmethod
    def _decode_text(output: bytes, operation: str) -> str:
        try:
            return output.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise SafeDefaultGitError("invalid_git_output", operation) from None

    def _decode_oid(self, output: bytes, operation: str) -> str:
        try:
            value = output.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            raise SafeDefaultGitError("invalid_git_output", operation) from None
        if not self._valid_oid(value):
            raise SafeDefaultGitError("invalid_git_output", operation)
        return value

    def _valid_oid(self, value: object) -> bool:
        return (
            type(value) is str
            and len(value) == self._oid_length
            and _OID.fullmatch(value) is not None
        )

    def _check_root_path(self) -> None:
        if not isinstance(self._root, Path) or not self._root.is_absolute():
            raise SafeDefaultGitError("invalid_repository_root", "construct")
        current = Path(self._root.anchor)
        for part in self._root.parts[1:]:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SafeDefaultGitError("symlink_repository_path", "construct")
            if current != self._root and not stat.S_ISDIR(info.st_mode):
                raise SafeDefaultGitError("invalid_repository_root", "construct")
        info = self._root.lstat()
        if not stat.S_ISDIR(info.st_mode) or self._root.resolve(strict=True) != self._root:
            raise SafeDefaultGitError("invalid_repository_root", "construct")
        if self._root_identity is not None and (info.st_dev, info.st_ino) != self._root_identity:
            raise SafeDefaultGitError("repository_root_changed", "observe")

    @staticmethod
    def _safe_directory(
        path: Path, expected: tuple[int, int] | None, operation: str,
    ) -> tuple[int, int]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SafeDefaultGitError("unsafe_git_administration", operation)
        identity = (info.st_dev, info.st_ino)
        if expected is not None and identity != expected:
            raise SafeDefaultGitError("git_administration_changed", operation)
        return identity

    @staticmethod
    def _check_path_components(path: Path, operation: str) -> None:
        if not path.is_absolute():
            raise SafeDefaultGitError("unsafe_git_administration", operation)
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SafeDefaultGitError("unsafe_git_administration", operation)
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise SafeDefaultGitError("unsafe_git_administration", operation)

    def _check_worktree_state_paths(self, operation: str) -> None:
        path = self._preliminary_admin_path
        if path is None:
            return
        self._check_path_components(path, operation)
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or (self._preliminary_admin_identity is not None
                and (info.st_dev, info.st_ino) != self._preliminary_admin_identity)
        ):
            raise SafeDefaultGitError("unsafe_git_administration", operation)
        for name in ("HEAD", "index"):
            state = (path / name).lstat()
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
                raise SafeDefaultGitError("unsafe_git_state_file", operation)

    def _check_common_config_path(self, operation: str) -> None:
        common = self._preliminary_common_path or self._common_path
        if common is None:
            return
        self._check_path_components(common, operation)
        info = common.lstat()
        expected = self._preliminary_common_identity or self._common_identity
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or (expected is not None and (info.st_dev, info.st_ino) != expected)
        ):
            raise SafeDefaultGitError("unsafe_git_administration", operation)
        config = common / "config"
        self._read_regular(config, MAX_CONFIG_BYTES, operation)

    def _initial_config_safety(self) -> None:
        output = self._initial_git([
            "config", "--local", "--no-includes", "--name-only", "--null", "--list",
        ], MAX_CONFIG_BYTES)
        keys = set()
        for raw in output.split(b"\0"):
            if not raw:
                continue
            try:
                keys.add(raw.decode("ascii", errors="strict").lower())
            except UnicodeDecodeError:
                raise SafeDefaultGitError("unsafe_git_config", "construct") from None
        self._validate_config_keys(keys, "construct")

    def _check_git_entry_stable(self, operation: str) -> None:
        if self._git_entry_identity is None or self._git_entry_mode is None:
            return
        entry = self._root / ".git"
        try:
            info = entry.lstat()
        except OSError:
            raise SafeDefaultGitError("git_pointer_changed", operation) from None
        identity = (info.st_dev, info.st_ino)
        if identity != self._git_entry_identity or stat.S_IFMT(info.st_mode) != self._git_entry_mode:
            raise SafeDefaultGitError("git_pointer_changed", operation)
        if stat.S_ISREG(info.st_mode):
            data = self._read_regular(entry, 4096, operation)
            if data != self._git_entry_data:
                raise SafeDefaultGitError("git_pointer_changed", operation)
        elif not stat.S_ISDIR(info.st_mode):
            raise SafeDefaultGitError("unsafe_git_pointer", operation)

    @staticmethod
    def _linked_path_value(data: bytes | None, base: Path, operation: str) -> Path:
        if (
            data is None or not data.endswith(b"\n") or data.count(b"\n") != 1
            or b"\0" in data or b"\r" in data
        ):
            raise SafeDefaultGitError("invalid_git_pointer", operation)
        try:
            path = Path(os.fsdecode(data[:-1]))
        except (TypeError, ValueError):
            raise SafeDefaultGitError("invalid_git_pointer", operation) from None
        if not path.is_absolute():
            path = base / path
        return path

    def _verify_admin_layout(self, operation: str) -> None:
        assert self._admin_path is not None and self._common_path is not None
        self._check_git_entry_stable(operation)
        self._check_path_components(self._common_path, operation)
        self._check_path_components(self._admin_path, operation)
        entry = self._root / ".git"
        info = entry.lstat()
        if stat.S_ISDIR(info.st_mode):
            if self._admin_path != entry or self._common_path != entry:
                raise SafeDefaultGitError("git_administration_binding_mismatch", operation)
            return
        if not stat.S_ISREG(info.st_mode) or self._admin_path == self._common_path:
            raise SafeDefaultGitError("unsafe_git_pointer", operation)
        raw_pointer = self._read_regular(entry, 4096, operation)
        if raw_pointer is None or not raw_pointer.startswith(b"gitdir: "):
            raise SafeDefaultGitError("invalid_git_pointer", operation)
        pointer = self._linked_path_value(raw_pointer[len(b"gitdir: "):], self._root, operation)
        self._check_path_components(pointer, operation)
        if pointer.resolve(strict=True) != self._admin_path:
            raise SafeDefaultGitError("git_pointer_target_mismatch", operation)
        worktrees = self._common_path / "worktrees"
        self._check_path_components(worktrees, operation)
        if self._admin_path.parent != worktrees:
            raise SafeDefaultGitError("git_worktree_admin_mismatch", operation)
        back_data = self._read_regular(self._admin_path / "gitdir", 4096, operation)
        back = self._linked_path_value(back_data, self._admin_path, operation)
        self._check_path_components(back, operation)
        if back.resolve(strict=True) != entry:
            raise SafeDefaultGitError("git_worktree_backlink_mismatch", operation)
        common_data = self._read_regular(self._admin_path / "commondir", 4096, operation)
        common = self._linked_path_value(common_data, self._admin_path, operation)
        self._check_path_components(common, operation)
        if common.resolve(strict=True) != self._common_path:
            raise SafeDefaultGitError("git_worktree_common_mismatch", operation)

    def _assert_filesystem_identity(self, operation: str) -> None:
        self._check_root_path()
        self._check_git_entry_stable(operation)
        if (
            self._store.root != self._root
            or self._store.repository != self._repository
            or self._store.remote != self._remote
        ):
            raise SafeDefaultGitError("repository_binding_changed", operation)
        if self._admin_path is None or self._common_path is None:
            return
        self._safe_directory(self._admin_path, self._admin_identity, operation)
        self._safe_directory(self._common_path, self._common_identity, operation)
        self._verify_admin_layout(operation)
        # HEAD and index are per-worktree even when this default checkout is a
        # linked worktree.  A symlink or non-regular replacement is refused.
        for name in ("HEAD", "index"):
            path = self._admin_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SafeDefaultGitError("unsafe_git_state_file", operation)
        config = self._common_path / "config"
        config_info = config.lstat()
        if stat.S_ISLNK(config_info.st_mode) or not stat.S_ISREG(config_info.st_mode):
            raise SafeDefaultGitError("unsafe_git_config_file", operation)
        info_dir = self._admin_path / "info"
        try:
            info = info_dir.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SafeDefaultGitError("unsafe_git_info_directory", operation)

    def _decode_path(self, output: bytes) -> Path:
        try:
            path = Path(output.decode("utf-8", errors="strict").strip())
        except (UnicodeDecodeError, ValueError):
            raise SafeDefaultGitError("invalid_git_output", "construct") from None
        if not path.is_absolute():
            raise SafeDefaultGitError("invalid_git_output", "construct")
        return path

    def _validate_ref_format(self, ref: str, operation: str) -> None:
        if not ref.startswith("refs/heads/"):
            raise SafeDefaultGitError("invalid_branch_ref", operation)
        result = self._initial_git(["check-ref-format", ref], 128)
        # A valid ref produces no output; check-ref-format failures are caught
        # by the private runner as an unavailable command result.
        if result:
            raise SafeDefaultGitError("invalid_branch_ref", operation)

    def _config_keys(self, scope: str) -> set[str]:
        output = self._git([
            "config", scope, "--no-includes", "--name-only", "--null", "--list",
        ], max_stdout=MAX_CONFIG_BYTES)
        if output is None:
            raise SafeDefaultGitError("git_config_unavailable", self._operation_name.get())
        keys: set[str] = set()
        for raw in output.split(b"\0"):
            if not raw:
                continue
            try:
                keys.add(raw.decode("ascii", errors="strict").lower())
            except UnicodeDecodeError:
                raise SafeDefaultGitError("unsafe_git_config", self._operation_name.get()) from None
        return keys

    def _config_safety(self) -> None:
        local = self._config_keys("--local")
        self._validate_config_keys(local, self._operation_name.get())

    @staticmethod
    def _validate_config_keys(keys: set[str], operation: str) -> None:
        unsafe_exact = {
            "core.gitproxy", "core.sshcommand", "core.hookspath", "core.fsmonitor",
            "core.attributesfile", "core.ignorecase", "core.sparsecheckout", "core.worktree",
            "core.autocrlf", "commit.gpgsign", "tag.gpgsign", "gpg.program",
            "credential.helper", "index.sparse", "extensions.partialclone",
            "extensions.worktreeconfig",
        }
        for key in keys:
            unsafe = (
                key.startswith(("include.", "includeif.", "filter."))
                or (key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof")))
                or key in unsafe_exact
                or (key.startswith("credential.") and key.endswith(".helper"))
                or (key.startswith("protocol.") and key.endswith(".allow"))
                or (key.startswith("remote.") and key.endswith((
                    ".vcs", ".uploadpack", ".receivepack", ".pushurl", ".pushoption",
                    ".promisor", ".partialclonefilter",
                )))
                or (key.startswith("diff.") and key.endswith((".command", ".external", ".textconv")))
                or (key.startswith("merge.") and key.endswith(".driver"))
            )
            if unsafe:
                raise SafeDefaultGitError("unsafe_git_config", operation)

    def _reject_incomplete_history(self) -> None:
        shallow = self._git(["rev-parse", "--is-shallow-repository"], max_stdout=64)
        if shallow is None or shallow.strip() != b"false":
            raise SafeDefaultGitError("incomplete_git_history", self._operation_name.get())
        assert self._admin_path is not None
        self._safe_info_directory(self._operation_name.get())
        for name in ("shallow", "info/grafts"):
            path = self._admin_path / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise SafeDefaultGitError("incomplete_git_history", self._operation_name.get()) from None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SafeDefaultGitError("incomplete_git_history", self._operation_name.get())
            raise SafeDefaultGitError("incomplete_git_history", self._operation_name.get())

    def _reject_sparse_checkout(self) -> None:
        assert self._admin_path is not None
        if not self._safe_info_directory(self._operation_name.get()):
            return
        path = self._admin_path / "info" / "sparse-checkout"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise SafeDefaultGitError("sparse_checkout_state_unavailable", self._operation_name.get()) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SafeDefaultGitError("unsafe_sparse_checkout_state", self._operation_name.get())
        raise SafeDefaultGitError("sparse_checkout_unsupported", self._operation_name.get())

    def _safe_info_directory(self, operation: str) -> bool:
        assert self._admin_path is not None
        path = self._admin_path / "info"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise SafeDefaultGitError("git_info_directory_unavailable", operation) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SafeDefaultGitError("unsafe_git_info_directory", operation)
        return True

    def _registered_main_worktree(self, head: str) -> None:
        output = self._git(["worktree", "list", "--porcelain", "-z"], max_stdout=1024 * 1024)
        if output is None:
            raise SafeDefaultGitError("worktree_list_unavailable", self._operation_name.get())
        target_path = os.fsencode(str(self._root))
        root_records = 0
        branch_records = 0
        for record in output.split(b"\0\0"):
            if not record:
                continue
            values: dict[bytes, bytes] = {}
            for field in record.split(b"\0"):
                key, separator, value = field.partition(b" ")
                if key in values or (not separator and key not in {b"detached", b"bare", b"locked", b"prunable"}):
                    raise SafeDefaultGitError("invalid_git_output", self._operation_name.get())
                values[key] = value if separator else b""
            if values.get(b"branch") == self._default_branch_ref.encode("utf-8"):
                branch_records += 1
            if values.get(b"worktree") != target_path:
                continue
            root_records += 1
            if (
                values.get(b"HEAD") != head.encode("ascii")
                or values.get(b"branch") != self._default_branch_ref.encode("utf-8")
                or b"detached" in values
                or b"bare" in values
                or b"locked" in values
                or b"prunable" in values
            ):
                raise SafeDefaultGitError("worktree_identity_mismatch", self._operation_name.get())
        if root_records != 1 or branch_records != 1:
            raise SafeDefaultGitError("worktree_registration_mismatch", self._operation_name.get())

    def _index_flags(self) -> bytes:
        output = self._git(["ls-files", "-v", "-z"], max_stdout=MAX_GIT_OUTPUT)
        if output is None:
            raise SafeDefaultGitError("index_unavailable", self._operation_name.get())
        records = output.split(b"\0") if output else []
        if records and records[-1] == b"":
            records.pop()
        if len(records) > MAX_TREE_ENTRIES:
            raise SafeDefaultGitError("index_entry_limit", self._operation_name.get())
        for record in records:
            if len(record) < 3 or record[1:2] != b" " or not record[2:]:
                raise SafeDefaultGitError("invalid_index_output", self._operation_name.get())
            tag = record[:1]
            # `ls-files -v` lowercases assume-unchanged entries and uses S for
            # skip-worktree.  Both hide state from ordinary clean/status checks.
            if tag != b"H":
                raise SafeDefaultGitError("hidden_index_entry", self._operation_name.get())
        return output

    def _observe_once(self) -> DefaultFacts:
        operation = self._operation_name.get()
        self._assert_filesystem_identity(operation)
        self._config_safety()
        self._reject_incomplete_history()
        self._reject_sparse_checkout()
        top = self._git(["rev-parse", "--show-toplevel"], max_stdout=16 * 1024)
        if top is None or self._decode_text(top, operation) != str(self._root):
            raise SafeDefaultGitError("worktree_identity_mismatch", operation)
        bare = self._git(["rev-parse", "--is-bare-repository"], max_stdout=64)
        if bare is None or bare.strip() != b"false":
            raise SafeDefaultGitError("not_worktree_repository", operation)
        branch_raw = self._git(["symbolic-ref", "--quiet", "HEAD"], max_stdout=4096)
        if branch_raw is None:
            raise SafeDefaultGitError("default_branch_detached", operation)
        branch = self._decode_text(branch_raw, operation)
        if branch != self._default_branch_ref:
            raise SafeDefaultGitError("default_branch_mismatch", operation)
        head_raw = self._git(["rev-parse", "--verify", "HEAD^{commit}"], max_stdout=128)
        tree_raw = self._git(["rev-parse", "--verify", "HEAD^{tree}"], max_stdout=128)
        branch_head_raw = self._git([
            "rev-parse", "--verify", f"{self._default_branch_ref}^{{commit}}",
        ], max_stdout=128)
        if head_raw is None or tree_raw is None or branch_head_raw is None:
            raise SafeDefaultGitError("default_branch_unavailable", operation)
        head = self._decode_oid(head_raw, operation)
        tree = self._decode_oid(tree_raw, operation)
        if self._decode_oid(branch_head_raw, operation) != head:
            raise SafeDefaultGitError("default_branch_ref_mismatch", operation)
        symbolic = self._git([
            "symbolic-ref", "--quiet", "--no-recurse", self._default_branch_ref,
        ], check=False, max_stdout=4096)
        if symbolic:
            raise SafeDefaultGitError("symbolic_default_ref", operation)
        self._registered_main_worktree(head)
        flags = self._index_flags()
        index = self._git(["ls-files", "--stage", "-z"], max_stdout=MAX_GIT_OUTPUT)
        status = self._git([
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all",
        ], max_stdout=MAX_GIT_OUTPUT)
        cached = self._git([
            "diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames",
            "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "HEAD", "--",
        ], max_stdout=MAX_GIT_OUTPUT)
        if index is None or status is None or cached is None:
            raise SafeDefaultGitError("worktree_state_unavailable", operation)
        if len(index) > MAX_INDEX_BYTES or len(flags) > MAX_INDEX_BYTES:
            raise SafeDefaultGitError("index_size_limit", operation)
        status_fingerprint = hashlib.sha256(
            _STATUS_DOMAIN + status + b"\0" + cached,
        ).hexdigest()
        return DefaultFacts(
            self._repository,
            self._default_branch_ref,
            head,
            tree,
            str(self._root),
            not status and not cached,
            hashlib.sha256(_INDEX_DOMAIN + index + b"\0" + flags).hexdigest(),
            status_fingerprint,
        )

    def observe(self) -> DefaultFacts:
        with self._operation("observe"):
            previous: DefaultFacts | None = None
            for _attempt in range(3):
                current = self._observe_once()
                if current == previous:
                    return current
                previous = current
            raise SafeDefaultGitError("local_state_unstable", "observe")

    def _remote_head(self) -> str:
        self._config_safety()
        output = self._git([
            "ls-remote", "--symref", "--refs", self._remote, self._default_branch_ref,
        ], max_stdout=16 * 1024)
        if output is None:
            raise SafeDefaultGitError("remote_read_failed", self._operation_name.get())
        if not output:
            raise SafeDefaultGitError("remote_ref_absent", self._operation_name.get())
        lines = output.splitlines()
        if len(lines) != 1:
            raise SafeDefaultGitError("ambiguous_remote_ref", self._operation_name.get())
        fields = lines[0].split(b"\t")
        if len(fields) != 2 or fields[1] != self._default_branch_ref.encode("utf-8"):
            raise SafeDefaultGitError("invalid_remote_output", self._operation_name.get())
        if fields[0].startswith(b"ref: "):
            raise SafeDefaultGitError("symbolic_remote_ref", self._operation_name.get())
        try:
            oid = fields[0].decode("ascii", errors="strict")
        except UnicodeDecodeError:
            raise SafeDefaultGitError("invalid_remote_output", self._operation_name.get()) from None
        if not self._valid_oid(oid):
            raise SafeDefaultGitError("invalid_remote_oid", self._operation_name.get())
        return oid

    def remote_head(self) -> str:
        with self._operation("remote_head"):
            return self._remote_head()

    def _commit_facts(self, oid: str) -> CommitFacts:
        operation = self._operation_name.get()
        if not self._valid_oid(oid):
            raise SafeDefaultGitError("invalid_oid", operation)
        kind = self._git(["cat-file", "-t", oid], max_stdout=64)
        if kind is None or kind.strip() != b"commit":
            raise SafeDefaultGitError("not_a_commit", operation)
        raw = self._git(["cat-file", "commit", oid], max_stdout=MAX_COMMIT_BYTES)
        if raw is None or b"\0" in raw:
            raise SafeDefaultGitError("invalid_commit", operation)
        header, separator, _message = raw.partition(b"\n\n")
        if not separator:
            raise SafeDefaultGitError("invalid_commit", operation)
        trees: list[str] = []
        parents: list[str] = []
        for line in header.splitlines():
            if line.startswith(b"tree "):
                trees.append(self._decode_oid(line[5:], operation))
            elif line.startswith(b"parent "):
                parents.append(self._decode_oid(line[7:], operation))
                if len(parents) > 64:
                    raise SafeDefaultGitError("commit_parent_limit", operation)
        if len(trees) != 1:
            raise SafeDefaultGitError("invalid_commit", operation)
        tree = trees[0]
        tree_kind = self._git(["cat-file", "-t", tree], max_stdout=64)
        if tree_kind is None or tree_kind.strip() != b"tree":
            raise SafeDefaultGitError("invalid_commit_tree", operation)
        for parent in parents:
            parent_kind = self._git(["cat-file", "-t", parent], max_stdout=64)
            if parent_kind is None or parent_kind.strip() != b"commit":
                raise SafeDefaultGitError("invalid_commit_parent", operation)
        return CommitFacts(oid, tree, tuple(parents))

    def commit_facts(self, oid: str) -> CommitFacts:
        with self._operation("commit_facts"):
            return self._commit_facts(oid)

    def _is_ancestor(self, old: str, new: str) -> bool:
        operation = self._operation_name.get()
        if not self._valid_oid(old) or not self._valid_oid(new):
            raise SafeDefaultGitError("invalid_oid", operation)
        if self._commit_facts(old).oid != old or self._commit_facts(new).oid != new:
            raise SafeDefaultGitError("invalid_commit", operation)
        output = self._git([
            "rev-list", "--parents", f"--max-count={MAX_HISTORY_COMMITS + 1}", new,
        ], max_stdout=MAX_GIT_OUTPUT)
        if output is None:
            raise SafeDefaultGitError("history_unavailable", operation)
        rows = output.splitlines()
        if len(rows) > MAX_HISTORY_COMMITS:
            raise SafeDefaultGitError("history_limit", operation)
        found = False
        for row in rows:
            fields = row.split()
            if not fields:
                raise SafeDefaultGitError("invalid_git_output", operation)
            for field in fields:
                self._decode_oid(field, operation)
            if fields[0].decode("ascii") == old:
                found = True
        return found

    def is_ancestor(self, old: str, new: str) -> bool:
        with self._operation("is_ancestor"):
            return self._is_ancestor(old, new)

    @staticmethod
    def _validate_tree_path(path: bytes, operation: str) -> None:
        if (
            not path
            or len(path) > MAX_PATH_BYTES
            or path.startswith(b"/")
            or b"\0" in path
            or b"//" in path
        ):
            raise SafeDefaultGitError("invalid_tree_path", operation)
        parts = path.split(b"/")
        if any(part in {b"", b".", b"..", b".git"} for part in parts):
            raise SafeDefaultGitError("unsafe_tree_path", operation)

    def _tree_entries(self, tree: str) -> dict[bytes, _TreeEntry]:
        operation = self._operation_name.get()
        if not self._valid_oid(tree):
            raise SafeDefaultGitError("invalid_tree_oid", operation)
        output = self._git(["ls-tree", "-r", "-z", "--full-tree", tree], max_stdout=MAX_TREE_BYTES)
        if output is None:
            raise SafeDefaultGitError("tree_unavailable", operation)
        records = output.split(b"\0") if output else []
        if records and records[-1] == b"":
            records.pop()
        if len(records) > MAX_TREE_ENTRIES:
            raise SafeDefaultGitError("tree_entry_limit", operation)
        entries: dict[bytes, _TreeEntry] = {}
        for record in records:
            try:
                header, path = record.split(b"\t", 1)
                mode, object_type, oid_raw = header.split(b" ")
            except ValueError:
                raise SafeDefaultGitError("invalid_tree_output", operation) from None
            self._validate_tree_path(path, operation)
            if mode not in _TREE_MODES or object_type != _TREE_MODES[mode]:
                raise SafeDefaultGitError("unsupported_tree_entry", operation)
            oid = self._decode_oid(oid_raw, operation)
            if path in entries:
                raise SafeDefaultGitError("duplicate_tree_path", operation)
            entries[path] = _TreeEntry(mode, oid)
        return entries

    def _index_for_tree(self, tree: str) -> tuple[str, dict[bytes, _TreeEntry]]:
        entries = self._tree_entries(tree)
        lines = bytearray()
        flags = bytearray()
        for path in sorted(entries):
            entry = entries[path]
            lines.extend(entry.mode + b" " + entry.oid.encode("ascii") + b" 0\t" + path + b"\0")
            flags.extend(b"H " + path + b"\0")
            if len(lines) + len(flags) > MAX_INDEX_BYTES:
                raise SafeDefaultGitError("index_size_limit", self._operation_name.get())
        return hashlib.sha256(_INDEX_DOMAIN + bytes(lines) + b"\0" + bytes(flags)).hexdigest(), entries

    def _diff_paths(
        self,
        old_tree: str,
        target_tree: str,
        old_entries: dict[bytes, _TreeEntry],
        target_entries: dict[bytes, _TreeEntry],
    ) -> tuple[bytes, ...]:
        operation = self._operation_name.get()
        output = self._git([
            "diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-ext-diff",
            "--no-textconv", "--no-commit-id", old_tree, target_tree,
        ], max_stdout=MAX_CHANGED_BYTES)
        if output is None:
            raise SafeDefaultGitError("changed_paths_unavailable", operation)
        records = output.split(b"\0") if output else []
        if records and records[-1] == b"":
            records.pop()
        if len(records) % 2:
            raise SafeDefaultGitError("invalid_diff_output", operation)
        if len(records) // 2 > MAX_CHANGED_PATHS:
            raise SafeDefaultGitError("changed_path_limit", operation)
        changed: dict[bytes, tuple[bytes, bytes]] = {}
        for index in range(0, len(records), 2):
            metadata, path = records[index], records[index + 1]
            self._validate_tree_path(path, operation)
            fields = metadata.split()
            if len(fields) != 5 or not fields[0].startswith(b":"):
                raise SafeDefaultGitError("invalid_diff_output", operation)
            old_mode = fields[0][1:]
            new_mode = fields[1]
            old_oid = fields[2]
            new_oid = fields[3]
            status = fields[4]
            expected_old = old_entries.get(path)
            expected_new = target_entries.get(path)
            if (
                old_mode != (expected_old.mode if expected_old is not None else b"000000")
                or new_mode != (expected_new.mode if expected_new is not None else b"000000")
                or old_oid != (expected_old.oid.encode("ascii") if expected_old is not None else b"0" * self._oid_length)
                or new_oid != (expected_new.oid.encode("ascii") if expected_new is not None else b"0" * self._oid_length)
            ):
                raise SafeDefaultGitError("diff_tree_mismatch", operation)
            if status not in {b"A", b"D", b"M"}:
                raise SafeDefaultGitError("unsupported_tree_transition", operation)
            if status == b"A" and (old_mode != b"000000" or new_mode == b"000000"):
                raise SafeDefaultGitError("invalid_diff_status", operation)
            if status == b"D" and (old_mode == b"000000" or new_mode != b"000000"):
                raise SafeDefaultGitError("invalid_diff_status", operation)
            if status == b"M" and (old_mode == b"000000" or new_mode == b"000000"):
                raise SafeDefaultGitError("invalid_diff_status", operation)
            if (
                (old_mode != b"000000" and old_mode not in _REGULAR_MODES)
                or (new_mode != b"000000" and new_mode not in _REGULAR_MODES)
            ):
                # Symlinks and gitlinks are deliberately not traversed or
                # checked out by this adapter.
                raise SafeDefaultGitError("unsupported_gitlink_or_symlink", operation)
            if path in changed:
                raise SafeDefaultGitError("duplicate_diff_path", operation)
            changed[path] = (old_mode, new_mode)
        expected = {
            path for path in set(old_entries) | set(target_entries)
            if old_entries.get(path) != target_entries.get(path)
        }
        if set(changed) != expected:
            raise SafeDefaultGitError("incomplete_diff_output", operation)
        return tuple(sorted(changed))

    def _other_paths(self) -> tuple[bytes, ...]:
        operation = self._operation_name.get()
        ordinary = self._git([
            "ls-files", "--others", "--exclude-standard", "-z",
        ], max_stdout=MAX_IGNORED_BYTES)
        ignored = self._git([
            "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
        ], max_stdout=MAX_IGNORED_BYTES)
        if ordinary is None or ignored is None:
            raise SafeDefaultGitError("untracked_paths_unavailable", operation)
        paths: set[bytes] = set()
        total = 0
        for output in (ordinary, ignored):
            records = output.split(b"\0") if output else []
            if records and records[-1] == b"":
                records.pop()
            if len(records) > MAX_IGNORED_PATHS:
                raise SafeDefaultGitError("untracked_path_limit", operation)
            for path in records:
                total += len(path) + 1
                if total > MAX_IGNORED_BYTES:
                    raise SafeDefaultGitError("untracked_path_size_limit", operation)
                self._validate_tree_path(path, operation)
                paths.add(path)
        if len(paths) > MAX_IGNORED_PATHS:
            raise SafeDefaultGitError("untracked_path_limit", operation)
        return tuple(sorted(paths))

    def _check_collisions(
        self,
        changed_paths: tuple[bytes, ...],
        other_paths: tuple[bytes, ...],
        old_entries: dict[bytes, _TreeEntry],
    ) -> None:
        operation = self._operation_name.get()
        unknown_set = set(other_paths)
        ordered_unknown = tuple(sorted(unknown_set))
        old_ordered = tuple(sorted(old_entries))
        changed_set = set(changed_paths)
        for changed in changed_paths:
            if changed in unknown_set:
                raise SafeDefaultGitError("untracked_path_collision", operation)
            # A non-ignored unknown file may block a changed descendant.
            for index, byte in enumerate(changed):
                if byte == ord("/") and changed[:index] in unknown_set:
                    raise SafeDefaultGitError("untracked_path_collision", operation)
            # An ignored/untracked descendant may be erased when a tracked
            # directory is replaced by a file.  A lower-bound lookup keeps
            # this check linearithmic even at the documented path limits.
            descendant_prefix = changed + b"/"
            position = bisect_left(ordered_unknown, descendant_prefix)
            if position < len(ordered_unknown) and ordered_unknown[position].startswith(descendant_prefix):
                raise SafeDefaultGitError("untracked_path_collision", operation)

        root_bytes = os.fsencode(str(self._root))
        for path in changed_paths:
            parts = path.split(b"/")
            current = root_bytes
            missing = False
            for part in parts[:-1]:
                current = os.path.join(current, part)
                if missing:
                    continue
                try:
                    info = os.lstat(current)
                except FileNotFoundError:
                    missing = True
                    continue
                except OSError:
                    raise SafeDefaultGitError("changed_path_unavailable", operation) from None
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise SafeDefaultGitError("unsafe_path_ancestor", operation)
            if missing:
                continue
            full_path = os.path.join(current, parts[-1])
            try:
                final = os.lstat(full_path)
            except FileNotFoundError:
                continue
            except OSError:
                raise SafeDefaultGitError("changed_path_unavailable", operation) from None
            if stat.S_ISREG(final.st_mode):
                continue
            if stat.S_ISDIR(final.st_mode):
                # A tracked directory may become one regular file.  Permit
                # this only when every old tracked descendant is included in
                # the transition; ignored/untracked descendants were rejected
                # above before the host checkout capability is invoked.
                prefix = path + b"/"
                index = bisect_left(old_ordered, prefix)
                found_descendant = False
                all_descendants_change = True
                while index < len(old_ordered) and old_ordered[index].startswith(prefix):
                    found_descendant = True
                    if old_ordered[index] not in changed_set:
                        all_descendants_change = False
                        break
                    index += 1
                if found_descendant and all_descendants_change:
                    continue
            raise SafeDefaultGitError("unsafe_changed_path_type", operation)

    @staticmethod
    def _read_regular(path: Path, limit: int, operation: str, absent_ok: bool = False) -> bytes | None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise SafeDefaultGitError("no_follow_reads_unsupported", operation)
        try:
            named = path.lstat()
        except FileNotFoundError:
            if absent_ok:
                return None
            raise SafeDefaultGitError("git_state_file_absent", operation) from None
        except OSError:
            raise SafeDefaultGitError("git_state_file_unavailable", operation) from None
        if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode) or named.st_size > limit:
            raise SafeDefaultGitError("unsafe_git_state_file", operation)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise SafeDefaultGitError("unsafe_git_state_file", operation)
                data = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
            current = path.lstat()
        except SafeDefaultGitError:
            raise
        except OSError:
            raise SafeDefaultGitError("git_state_file_unavailable", operation) from None
        if (
            len(data) > limit
            or before.st_size != after.st_size
            or after.st_size > limit
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise SafeDefaultGitError("git_state_file_changed", operation)
        return data

    def _refs_snapshot(self) -> tuple[tuple[bytes, str, bytes], ...]:
        operation = self._operation_name.get()
        output = self._git([
            "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)", "refs/",
        ], max_stdout=MAX_GIT_OUTPUT)
        if output is None:
            raise SafeDefaultGitError("refs_unavailable", operation)
        lines = output.splitlines()
        if len(lines) > MAX_TREE_ENTRIES:
            raise SafeDefaultGitError("ref_limit", operation)
        refs: list[tuple[bytes, str, bytes]] = []
        seen: set[bytes] = set()
        for line in lines:
            fields = line.split(b"\0")
            if len(fields) != 3 or not fields[0].startswith(b"refs/"):
                raise SafeDefaultGitError("invalid_ref_output", operation)
            name, raw_oid, symref = fields
            try:
                oid = raw_oid.decode("ascii", errors="strict")
            except UnicodeDecodeError:
                raise SafeDefaultGitError("invalid_ref_output", operation) from None
            if not self._valid_oid(oid) or name in seen:
                raise SafeDefaultGitError("invalid_ref_output", operation)
            seen.add(name)
            refs.append((name, oid, symref))
        refs.sort()
        return tuple(refs)

    def _config_file_state(self) -> tuple[tuple[str, bytes], ...]:
        assert self._common_path is not None
        operation = self._operation_name.get()
        content = self._read_regular(self._common_path / "config", MAX_CONFIG_BYTES, operation)
        return (("config", content or b""),)

    def _side_state(self) -> _SideState:
        assert self._admin_path is not None
        operation = self._operation_name.get()
        self._assert_filesystem_identity(operation)
        fetch_head = self._read_regular(self._admin_path / "FETCH_HEAD", 64 * 1024, operation, absent_ok=True)
        state = _SideState(self._refs_snapshot(), fetch_head, self._config_file_state())
        self._assert_filesystem_identity(operation)
        return state

    @staticmethod
    def _expected_refs_after_ff(
        before: _SideState, branch_ref: str, old: str, target: str,
    ) -> tuple[tuple[bytes, str, bytes], ...]:
        refs = {name: (oid, symref) for name, oid, symref in before.refs}
        branch = branch_ref.encode("utf-8")
        prior = refs.get(branch)
        if prior != (old, b""):
            raise SafeDefaultGitError("default_ref_changed", "reconcile")
        refs[branch] = (target, b"")
        return tuple(sorted((name, oid, symref) for name, (oid, symref) in refs.items()))

    def _intent_side_state_allowed(
        self, before: _SideState, after: _SideState, *, intent_callback_installed: bool,
    ) -> bool:
        """Allow only the callback's one append-only metadata-intent ref write."""
        if before.fetch_head != after.fetch_head or before.config != after.config:
            return False
        previous = {name: (oid, symref) for name, oid, symref in before.refs}
        current = {name: (oid, symref) for name, oid, symref in after.refs}
        changed = {
            name for name in set(previous) | set(current)
            if previous.get(name) != current.get(name)
        }
        metadata_ref = METADATA_REF.encode("ascii")
        if not changed:
            return True
        if not intent_callback_installed or changed != {metadata_ref}:
            return False
        old = previous.get(metadata_ref)
        new = current.get(metadata_ref)
        if new is None or new[1] or (old is not None and old[1]):
            return False
        new_commit = self._commit_facts(new[0])
        if old is None:
            return not new_commit.parents
        return old[0] == new[0] or self._is_ancestor(old[0], new[0])

    def _observe_against(
        self,
        expected_facts: DefaultFacts,
        expected_remote: str,
        expected_side: _SideState,
        *,
        operation: str,
    ) -> None:
        facts = self.observe()
        remote = self.remote_head()
        side = self._side_state()
        if facts != expected_facts:
            raise SafeDefaultGitError("local_state_changed", operation)
        if remote != expected_remote:
            raise SafeDefaultGitError("remote_ref_changed", operation)
        if side != expected_side:
            raise SafeDefaultGitError("repository_side_state_changed", operation)

    def _capture_after_attempt(
        self,
    ) -> tuple[DefaultFacts | None, str | None, _SideState | None, tuple[bytes, ...] | None, bool]:
        facts: DefaultFacts | None = None
        remote: str | None = None
        side: _SideState | None = None
        other_paths: tuple[bytes, ...] | None = None
        failed = False
        try:
            facts = self.observe()
        except SafeDefaultGitError:
            failed = True
        try:
            remote = self.remote_head()
        except SafeDefaultGitError:
            failed = True
        try:
            side = self._side_state()
        except SafeDefaultGitError:
            failed = True
        try:
            other_paths = self._other_paths()
        except SafeDefaultGitError:
            failed = True
        return facts, remote, side, other_paths, failed

    def reconcile(
        self,
        *,
        merged_oid: str,
        expected_remote_oid: str,
        on_intent: Callable[[FastForwardRequest], object] | None = None,
    ) -> DefaultReceipt:
        with self._operation("reconcile"):
            if not self._valid_oid(merged_oid) or not self._valid_oid(expected_remote_oid):
                raise SafeDefaultGitError("invalid_oid", "reconcile")
            if on_intent is not None and not callable(on_intent):
                raise SafeDefaultGitError("invalid_callback", "reconcile")

            before = self.observe()
            if not before.clean:
                raise SafeDefaultGitError("dirty_default_worktree", "reconcile")
            remote_before = self.remote_head()
            if remote_before != expected_remote_oid:
                raise SafeDefaultGitError("remote_head_mismatch", "reconcile")
            side_before = self._side_state()
            other_paths_before = self._other_paths()
            # Do not cross the fetch boundary based on facts that went stale
            # while side-state snapshots were collected.
            self._observe_against(before, remote_before, side_before, operation="reconcile")

            fetch_request = FetchRequest(
                self._repository, self._default_branch_ref, remote_before, merged_oid,
            )
            try:
                self._fetch(fetch_request)
            except Exception:
                # The host may have received objects before losing its
                # acknowledgement.  Verify state and the graph below instead
                # of treating callback acknowledgement as authority.
                pass

            (after_fetch, remote_after_fetch, side_after_fetch, other_paths_after_fetch,
             fetch_observation_failed) = self._capture_after_attempt()
            if (
                fetch_observation_failed or after_fetch is None or remote_after_fetch is None
                or side_after_fetch is None or other_paths_after_fetch is None
            ):
                raise SafeDefaultGitError("fetch_revalidation_failed", "reconcile")
            if after_fetch != before:
                raise SafeDefaultGitError("local_state_changed_during_fetch", "reconcile")
            if remote_after_fetch != remote_before:
                raise SafeDefaultGitError("remote_ref_changed_during_fetch", "reconcile")
            if side_after_fetch != side_before:
                raise SafeDefaultGitError("fetch_changed_repository_state", "reconcile")
            if other_paths_after_fetch != other_paths_before:
                raise SafeDefaultGitError("local_untracked_state_changed_during_fetch", "reconcile")

            # The exact merge commit must be available, and it must be part of
            # the actual remote default-branch history.  The remote root is
            # compared again after the fetch; a callback result is irrelevant.
            merged_facts = self._commit_facts(merged_oid)
            target_head = remote_after_fetch
            target_commit = self._commit_facts(target_head)
            old_commit = self._commit_facts(before.head)
            if old_commit.tree != before.tree:
                raise SafeDefaultGitError("local_tree_changed", "reconcile")
            if not self._is_ancestor(merged_oid, target_head):
                raise SafeDefaultGitError("merged_commit_not_on_remote", "reconcile")
            if not self._is_ancestor(before.head, target_head):
                raise SafeDefaultGitError("default_branch_not_fast_forwardable", "reconcile")

            target_index, target_entries = self._index_for_tree(target_commit.tree)
            old_index, old_entries = self._index_for_tree(old_commit.tree)
            if old_index != before.index_fingerprint:
                raise SafeDefaultGitError("index_does_not_match_head", "reconcile")
            changed_paths = self._diff_paths(old_commit.tree, target_commit.tree, old_entries, target_entries)
            self._check_collisions(changed_paths, other_paths_before, old_entries)

            if target_head == before.head:
                # A no-op is still a fresh, exact graph/remote/worktree
                # confirmation; it is not inferred from callback success.
                self._observe_against(before, target_head, side_before, operation="reconcile")
                if self._other_paths() != other_paths_before:
                    raise SafeDefaultGitError("noop_untracked_state_changed", "reconcile")
                if target_commit.tree != before.tree or target_index != before.index_fingerprint:
                    raise SafeDefaultGitError("noop_postcondition_mismatch", "reconcile")
                return DefaultReceipt(
                    self._repository, self._default_branch_ref, str(self._root),
                    before.head, before.head, merged_facts.oid, before.tree,
                )

            request = FastForwardRequest(
                repository=self._repository,
                branch_ref=self._default_branch_ref,
                worktree=str(self._root),
                expected_head=before.head,
                target_head=target_head,
                merged_oid=merged_facts.oid,
                target_tree=target_commit.tree,
                expected_index_fingerprint=before.index_fingerprint,
                expected_status_fingerprint=before.status_fingerprint,
                target_index_fingerprint=target_index,
                changed_paths=changed_paths,
            )

            intent_failed = False
            if on_intent is not None:
                try:
                    intent_failed = on_intent(request) is False
                except Exception:
                    intent_failed = True
            # Always re-read after the caller's intent hook, including False
            # and exceptions.  A refusal never turns into a host FF attempt.
            (intent_facts, intent_remote, side_after_intent, intent_other_paths,
             intent_observation_failed) = self._capture_after_attempt()
            if (
                intent_observation_failed or intent_facts is None or intent_remote is None
                or side_after_intent is None or intent_other_paths is None
            ):
                raise SafeDefaultGitError("intent_revalidation_failed", "reconcile")
            if intent_facts != before:
                raise SafeDefaultGitError("local_state_changed", "reconcile")
            if intent_remote != target_head:
                raise SafeDefaultGitError("remote_ref_changed", "reconcile")
            if not self._intent_side_state_allowed(
                side_before, side_after_intent, intent_callback_installed=on_intent is not None,
            ):
                raise SafeDefaultGitError("repository_side_state_changed", "reconcile")
            if intent_other_paths != other_paths_before:
                raise SafeDefaultGitError("intent_untracked_state_changed", "reconcile")
            self._check_collisions(changed_paths, intent_other_paths, old_entries)
            if intent_failed:
                raise SafeDefaultGitError("intent_callback_failed", "reconcile")

            ff_failed = False
            try:
                # This is a trusted host-only mutation capability.  Its return
                # value is deliberately ignored; only the postcondition below
                # can create a receipt.
                self._fast_forward(request)
            except Exception:
                ff_failed = True

            (post, post_remote, post_side, post_other_paths,
             post_unavailable) = self._capture_after_attempt()
            if (
                post_unavailable or post is None or post_remote is None
                or post_side is None or post_other_paths is None
            ):
                raise SafeDefaultGitError("fast_forward_postcondition_unavailable", "reconcile")
            expected_refs = self._expected_refs_after_ff(
                side_after_intent, self._default_branch_ref, before.head, target_head,
            )
            state_confirmed = (
                post.repository == self._repository
                and post.branch_ref == self._default_branch_ref
                and post.worktree == before.worktree
                and post.head == target_head
                and post.tree == target_commit.tree
                and post.clean
                and post.index_fingerprint == target_index
                and post.status_fingerprint == before.status_fingerprint
                and post_remote == target_head
                and post_side.refs == expected_refs
                and post_side.fetch_head == side_after_intent.fetch_head
                and post_side.config == side_after_intent.config
                and post_other_paths == other_paths_before
            )
            if state_confirmed:
                # Include the exact merge ancestry and target graph in the
                # postcondition, even when the host returned False or raised.
                if not self._is_ancestor(merged_facts.oid, target_head):
                    raise SafeDefaultGitError("postcondition_graph_mismatch", "reconcile")
                return DefaultReceipt(
                    self._repository, self._default_branch_ref, str(self._root),
                    before.head, target_head, merged_facts.oid, target_commit.tree,
                )
            if post.head == target_head:
                raise SafeDefaultGitError("fast_forward_applied_state_changed", "reconcile")
            if ff_failed:
                raise SafeDefaultGitError("fast_forward_failed_unconfirmed", "reconcile")
            raise SafeDefaultGitError("fast_forward_not_confirmed", "reconcile")


__all__ = [
    "CommitFacts",
    "DefaultFacts",
    "DefaultGit",
    "DefaultReceipt",
    "FastForwardRequest",
    "FetchRequest",
    "SafeDefaultGitError",
]
