"""Task-bound, bounded Git observations and exact Task-ref publication.

This is deliberately not a Git executor.  It exposes only read-only facts, one
empty Task bootstrap commit, and one exact non-force push capability.  The
MetadataStore private Git runner is reused for its scrubbed environment, output
limits, and validation deadline; its metadata-ref writer is never used here.

``publish`` is a trusted host capability.  It must validate the destination and
Task identity and perform an ordinary non-force branch publication.  It is not
a client-side proof of server old-OID compare-and-swap and does not activate a
live GitHub transport.  The postcondition is verified by an exact remote read.
The local bootstrap CAS uses a detached temporary admin directory so its ref
reflog cannot contend on the locked product worktree's HEAD.lock. It requires
Linux procfs for stable directory-descriptor addressing and fails closed without
that facility; it never falls back to a mutable common-directory pathname.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from metadata_ref import MetadataRefError, MetadataStore
import task_record


MAX_COMMAND_OUTPUT = 16 * 1024 * 1024
MAX_CONFIG_OUTPUT = 512 * 1024
MAX_WORKTREE_OUTPUT = 1024 * 1024
MAX_HISTORY_COMMITS = 100_000
MAX_GIT_SECONDS = 60.0
MAX_BRANCH_REF_BYTES = 1024
_TASK = re.compile(r"[1-9][0-9]{0,127}\Z", re.ASCII)
_OID = re.compile(r"[0-9a-f]+\Z", re.ASCII)
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_SAFE_OPERATION = frozenset({"construct", "observe", "remote_head", "is_ancestor", "bootstrap", "push"})
_COMMIT_SUBJECT = "AgentCore Task {task} bootstrap"


class DistinctTaskGitError(RuntimeError):
    """A bounded, secret-free Task Git failure code and operation label."""

    def __init__(self, code: str, operation: str) -> None:
        self.code = code if type(code) is str and _SAFE_CODE.fullmatch(code) else "task_git_error"
        self.operation = operation if operation in _SAFE_OPERATION else "observe"
        super().__init__(f"{self.operation}:{self.code}")


@dataclass(frozen=True)
class LocalFacts:
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    worktree: str
    clean: bool
    index_fingerprint: str
    status_fingerprint: str


@dataclass(frozen=True)
class BranchPush:
    repository: str
    task: str
    branch_ref: str
    subject: str
    expected_remote: str | None


class TaskGit:
    """Git facts and narrowly bounded writes for one pinned Task worktree.

    Bootstrap no-ops and pushes require the exact pinned base to remain an
    ancestor; this guard does not rewrite historical Task metadata.
    ``fetch_remote`` is a trusted host callback that receives only one exact
    OID. It must fetch that object without updating refs or FETCH_HEAD; local
    facts and the advertised Task ref are sampled again immediately afterward.
    """

    def __init__(
        self,
        store: MetadataStore,
        *,
        task: str,
        branch_ref: str,
        base_revision: str,
        default_branch_ref: str,
        publish: Callable[[BranchPush], object],
        fetch_remote: Callable[[str], object] | None = None,
    ) -> None:
        operation = "construct"
        if not isinstance(store, MetadataStore):
            raise DistinctTaskGitError("invalid_store", operation)
        if type(task) is not str or len(task) > 128 or not _TASK.fullmatch(task):
            raise DistinctTaskGitError("invalid_task", operation)
        try:
            self._validate_branch_ref(branch_ref)
            self._validate_branch_ref(default_branch_ref)
        except (TypeError, ValueError, UnicodeError):
            raise DistinctTaskGitError("invalid_branch_ref", operation) from None
        if branch_ref == default_branch_ref:
            raise DistinctTaskGitError("task_branch_is_default", operation)
        if not callable(publish) or (fetch_remote is not None and not callable(fetch_remote)):
            raise DistinctTaskGitError("invalid_capability", operation)
        if not store.root.is_dir() or not isinstance(store.repository, str):
            raise DistinctTaskGitError("invalid_repository_binding", operation)
        self._store = store
        self._task = task
        self._branch_ref = branch_ref
        self._base_revision = base_revision
        self._default_branch_ref = default_branch_ref
        self._publish = publish
        self._fetch_remote = fetch_remote
        self._operation_name: ContextVar[str] = ContextVar(
            f"task_git_operation_{id(self)}", default=operation,
        )
        self._admin_path: Path | None = None
        self._common_path: Path | None = None
        self._admin_identity: tuple[int, int] | None = None
        self._common_identity: tuple[int, int] | None = None
        if not self._valid_oid(base_revision):
            raise DistinctTaskGitError("invalid_base_revision", operation)
        try:
            self._check_ref_format(branch_ref, operation)
            self._check_ref_format(default_branch_ref, operation)
            self._config_safety()
            self._reject_incomplete_history()
            self._require_commit(base_revision)
            self._cache_admin_identity()
        except DistinctTaskGitError as error:
            if error.operation == operation:
                raise
            raise DistinctTaskGitError(error.code, operation) from None
        except Exception:
            raise DistinctTaskGitError("repository_unavailable", operation) from None

    @property
    def store(self) -> MetadataStore:
        return self._store

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def repository(self) -> str:
        return self._store.repository

    @property
    def task(self) -> str:
        return self._task

    @property
    def branch_ref(self) -> str:
        return self._branch_ref

    @property
    def base_revision(self) -> str:
        return self._base_revision

    @property
    def default_branch_ref(self) -> str:
        return self._default_branch_ref

    @contextmanager
    def _operation(self, operation: str) -> Iterator[None]:
        token = self._operation_name.set(operation)
        try:
            with self._store.validation_scope():
                yield
        except DistinctTaskGitError as error:
            if error.operation == operation:
                raise
            raise DistinctTaskGitError(error.code, operation) from None
        except Exception:
            raise DistinctTaskGitError("operation_failed", operation) from None
        finally:
            self._operation_name.reset(token)

    def _git(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        max_stdout: int = MAX_COMMAND_OUTPUT,
        extra_env: dict[str, str] | None = None,
    ) -> bytes | None:
        """Invoke only fixed plumbing/read commands through #217's private seam."""
        environment = {"GIT_OPTIONAL_LOCKS": "0"}
        if extra_env:
            environment.update(extra_env)
        try:
            return self._store._git(
                arguments,
                check=check,
                max_stdout=max_stdout,
                timeout=MAX_GIT_SECONDS,
                extra_env=environment,
            )
        except (MetadataRefError, OSError, ValueError):
            raise DistinctTaskGitError("git_operation_failed", self._operation_name.get()) from None

    def _valid_oid(self, value: object) -> bool:
        return (
            type(value) is str
            and len(value) == getattr(self._store, "_oid_length", -1)
            and _OID.fullmatch(value) is not None
        )

    def _oid(self, output: bytes, operation: str) -> str:
        try:
            value = output.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            raise DistinctTaskGitError("invalid_git_output", operation) from None
        if not self._valid_oid(value):
            raise DistinctTaskGitError("invalid_git_output", operation)
        return value

    def _text(self, output: bytes, operation: str) -> str:
        try:
            return output.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise DistinctTaskGitError("invalid_git_output", operation) from None

    def _config_keys(self, scope: str) -> set[str]:
        output = self._git(
            ["config", scope, "--no-includes", "--name-only", "--null", "--list"],
            max_stdout=MAX_CONFIG_OUTPUT,
        )
        assert output is not None
        keys: set[str] = set()
        for raw in output.split(b"\0"):
            if not raw:
                continue
            try:
                keys.add(raw.decode("ascii", errors="strict").lower())
            except UnicodeDecodeError:
                raise DistinctTaskGitError("unsafe_git_config", "observe") from None
        return keys

    def _config_safety(self) -> None:
        local = self._config_keys("--local")
        keys = set(local)
        if "extensions.worktreeconfig" in local:
            keys.update(self._config_keys("--worktree"))
        for key in keys:
            unsafe = (
                key.startswith(("include.", "includeif.", "filter."))
                or (key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof")))
                or key in {
                    "core.gitproxy", "core.sshcommand", "core.hookspath", "core.fsmonitor",
                    "core.attributesfile", "commit.gpgsign", "tag.gpgsign", "gpg.program",
                    "credential.helper",
                }
                or (key.startswith("credential.") and key.endswith(".helper"))
                or (key.startswith("protocol.") and key.endswith(".allow"))
                or (key.startswith("remote.") and key.endswith((
                    ".vcs", ".uploadpack", ".receivepack", ".pushurl", ".pushoption",
                )))
                or (key.startswith("diff.") and key.endswith((".command", ".external")))
            )
            if unsafe:
                raise DistinctTaskGitError("unsafe_git_config", "observe")

    def _reject_incomplete_history(self) -> None:
        shallow = self._git(["rev-parse", "--is-shallow-repository"], max_stdout=64)
        assert shallow is not None
        if shallow.strip() != b"false":
            raise DistinctTaskGitError("incomplete_git_history", "observe")
        raw = self._git(["rev-parse", "--git-path", "info/grafts"], max_stdout=16 * 1024)
        assert raw is not None
        try:
            path_text = raw.decode("utf-8", errors="strict").strip()
            graft_path = Path(path_text)
            if not graft_path.is_absolute():
                graft_path = self.root / graft_path
            graft_path.lstat()
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, ValueError):
            raise DistinctTaskGitError("incomplete_git_history", "observe") from None
        raise DistinctTaskGitError("incomplete_git_history", "observe")

    def _require_commit(self, oid: str) -> None:
        if not self._valid_oid(oid):
            raise DistinctTaskGitError("invalid_oid", "observe")
        output = self._git(["cat-file", "-t", oid], max_stdout=64)
        assert output is not None
        if output.strip() != b"commit":
            raise DistinctTaskGitError("not_a_commit", "observe")

    def _worktree_registered(self, head: str) -> None:
        output = self._git(
            ["worktree", "list", "--porcelain", "-z"], max_stdout=MAX_WORKTREE_OUTPUT,
        )
        assert output is not None
        target_path = os.fsencode(str(self.root))
        found = False
        for record in output.split(b"\0\0"):
            if not record:
                continue
            fields = record.split(b"\0")
            values: dict[bytes, bytes] = {}
            for field in fields:
                key, separator, value = field.partition(b" ")
                if (not separator and key not in {b"detached", b"bare", b"locked", b"prunable"}) or key in values:
                    raise DistinctTaskGitError("invalid_git_output", "observe")
                values[key] = value if separator else b""
            if values.get(b"worktree") != target_path:
                continue
            if values.get(b"HEAD") != head.encode("ascii") or values.get(b"branch") != self.branch_ref.encode("utf-8"):
                raise DistinctTaskGitError("worktree_identity_mismatch", "observe")
            found = True
        if not found:
            raise DistinctTaskGitError("worktree_not_registered", "observe")

    def _observe_once(self) -> LocalFacts:
        self._config_safety()
        self._verified_admin_paths()
        self._reject_incomplete_history()
        top = self._git(["rev-parse", "--show-toplevel"], max_stdout=16 * 1024)
        assert top is not None
        try:
            resolved_top = Path(self._text(top, "observe")).resolve(strict=True)
        except (OSError, ValueError):
            raise DistinctTaskGitError("worktree_identity_mismatch", "observe") from None
        if resolved_top != self.root:
            raise DistinctTaskGitError("worktree_identity_mismatch", "observe")
        branch_raw = self._git(["symbolic-ref", "--quiet", "HEAD"], max_stdout=16 * 1024)
        assert branch_raw is not None
        branch = self._text(branch_raw, "observe")
        if branch != self.branch_ref:
            raise DistinctTaskGitError("task_branch_mismatch", "observe")
        head_raw = self._git(["rev-parse", "--verify", "HEAD^{commit}"], max_stdout=128)
        tree_raw = self._git(["rev-parse", "--verify", "HEAD^{tree}"], max_stdout=128)
        assert head_raw is not None and tree_raw is not None
        head = self._oid(head_raw, "observe")
        tree = self._oid(tree_raw, "observe")
        self._require_commit(head)
        branch_head_raw = self._git(
            ["rev-parse", "--verify", f"{self.branch_ref}^{{commit}}"], max_stdout=128,
        )
        assert branch_head_raw is not None
        if self._oid(branch_head_raw, "observe") != head:
            raise DistinctTaskGitError("task_branch_mismatch", "observe")
        symbolic = self._git(["symbolic-ref", "--quiet", "--no-recurse", self.branch_ref], check=False, max_stdout=4096)
        if symbolic:
            raise DistinctTaskGitError("symbolic_task_ref", "observe")
        self._worktree_registered(head)
        index = self._git(["ls-files", "--stage", "-z"], max_stdout=MAX_COMMAND_OUTPUT)
        status = self._git([
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all",
        ], max_stdout=MAX_COMMAND_OUTPUT)
        cached = self._git([
            "diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames",
            "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "HEAD", "--",
        ], max_stdout=MAX_COMMAND_OUTPUT)
        assert index is not None and status is not None and cached is not None
        return LocalFacts(
            self.repository,
            self.task,
            self.branch_ref,
            head,
            tree,
            str(self.root),
            not status and not cached,
            hashlib.sha256(b"agentcore-task-git-index/v1\n" + index).hexdigest(),
            hashlib.sha256(b"agentcore-task-git-status/v1\n" + status + b"\0" + cached).hexdigest(),
        )

    def observe(self) -> LocalFacts:
        with self._operation("observe"):
            previous: LocalFacts | None = None
            for _attempt in range(3):
                current = self._observe_once()
                if current == previous:
                    return current
                previous = current
            raise DistinctTaskGitError("local_state_unstable", "observe")

    def _assert_task_ref(self, operation: str) -> None:
        try:
            self._validate_branch_ref(self.branch_ref)
        except (TypeError, ValueError, UnicodeError):
            raise DistinctTaskGitError("invalid_branch_ref", operation) from None
        self._check_ref_format(self.branch_ref, operation)
        if self.branch_ref == self.default_branch_ref:
            raise DistinctTaskGitError("task_branch_is_default", operation)

    @staticmethod
    def _validate_branch_ref(value: object) -> str:
        if type(value) is not str or len(value) > MAX_BRANCH_REF_BYTES:
            raise ValueError("invalid bounded branch ref")
        branch = task_record.validate_branch_ref(value)
        if len(branch.encode("utf-8", errors="strict")) > MAX_BRANCH_REF_BYTES:
            raise ValueError("branch ref is too long")
        return branch

    def _check_ref_format(self, ref: str, operation: str) -> None:
        try:
            self._git(["check-ref-format", ref], max_stdout=128)
        except DistinctTaskGitError:
            raise DistinctTaskGitError("invalid_branch_ref", operation) from None

    def _remote_head(self) -> str | None:
        self._config_safety()
        output = self._git(
            ["ls-remote", "--symref", "--refs", self._store.remote, self.branch_ref],
            max_stdout=16 * 1024,
        )
        assert output is not None
        if not output:
            return None
        target = self.branch_ref.encode("utf-8")
        oids: list[str] = []
        symbolic_target = False
        for line in output.splitlines():
            fields = line.split(b"\t")
            if len(fields) != 2:
                raise DistinctTaskGitError("invalid_remote_output", "remote_head")
            if fields[0].startswith(b"ref: "):
                if fields[1] != target:
                    raise DistinctTaskGitError("invalid_remote_output", "remote_head")
                symbolic_target = True
                continue
            if fields[1] != target:
                raise DistinctTaskGitError("invalid_remote_output", "remote_head")
            oids.append(self._oid(fields[0], "remote_head"))
        if symbolic_target:
            raise DistinctTaskGitError("symbolic_task_ref", "remote_head")
        if not oids:
            raise DistinctTaskGitError("invalid_remote_output", "remote_head")
        if len(oids) != 1:
            raise DistinctTaskGitError("ambiguous_remote_ref", "remote_head")
        return oids[0]

    def remote_head(self) -> str | None:
        with self._operation("remote_head"):
            self._assert_task_ref("remote_head")
            return self._remote_head()

    def _ancestor(self, old: str, new: str) -> bool:
        self._verified_admin_paths()
        self._reject_incomplete_history()
        self._require_commit(old)
        self._require_commit(new)
        output = self._git(
            ["rev-list", "--parents", f"--max-count={MAX_HISTORY_COMMITS + 1}", new],
            max_stdout=MAX_COMMAND_OUTPUT,
        )
        assert output is not None
        rows = output.splitlines()
        if len(rows) > MAX_HISTORY_COMMITS:
            raise DistinctTaskGitError("history_limit", "is_ancestor")
        found = False
        for row in rows:
            fields = row.split()
            if not fields:
                raise DistinctTaskGitError("invalid_git_output", "is_ancestor")
            for field in fields:
                self._oid(field, "is_ancestor")
            if fields[0].decode("ascii") == old:
                found = True
        self._verified_admin_paths()
        return found

    def is_ancestor(self, old: str, new: str) -> bool:
        with self._operation("is_ancestor"):
            if not self._valid_oid(old) or not self._valid_oid(new):
                raise DistinctTaskGitError("invalid_oid", "is_ancestor")
            try:
                return self._ancestor(old, new)
            except DistinctTaskGitError as error:
                raise DistinctTaskGitError(error.code, "is_ancestor") from None

    def _ensure_remote_commit(self, oid: str, before: LocalFacts) -> None:
        try:
            self._require_commit(oid)
            return
        except DistinctTaskGitError:
            if self._fetch_remote is None:
                raise DistinctTaskGitError("remote_history_unavailable", "push") from None
        fetch_error = False
        try:
            self._fetch_remote(oid)
        except Exception:
            fetch_error = True
        after: LocalFacts | None = None
        current_remote: str | None = None
        observation_failed = False
        try:
            after = self.observe()
        except DistinctTaskGitError:
            observation_failed = True
        try:
            current_remote = self._remote_head()
        except DistinctTaskGitError:
            observation_failed = True
        if observation_failed:
            raise DistinctTaskGitError("fetch_revalidation_failed", "push")
        if after != before:
            raise DistinctTaskGitError("local_state_changed_during_fetch", "push")
        if current_remote != oid:
            raise DistinctTaskGitError("remote_changed_during_fetch", "push")
        if fetch_error:
            raise DistinctTaskGitError("remote_fetch_failed", "push") from None
        self._require_commit(oid)

    @staticmethod
    def _read_admin_file(path: Path, limit: int = 4096) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("unsafe Git administration file")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
            named = path.lstat()
            if (
                len(data) > limit
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)
                or not stat.S_ISREG(named.st_mode)
            ):
                raise ValueError("Git administration file changed")
            return bytes(data)
        finally:
            os.close(descriptor)

    @staticmethod
    def _admin_path_line(data: bytes) -> Path:
        if not data.endswith(b"\n") or data.count(b"\n") != 1 or b"\0" in data:
            raise ValueError("malformed Git administration path")
        value = data[:-1]
        if not value:
            raise ValueError("empty Git administration path")
        return Path(os.fsdecode(value))

    @staticmethod
    def _open_verified_directory(
        path: Path, expected: tuple[int, int] | None,
    ) -> tuple[int, tuple[int, int]]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            named = path.lstat()
            identity = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(named.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != identity
                or (expected is not None and identity != expected)
            ):
                raise ValueError("Git administration directory identity changed")
            return descriptor, identity
        except Exception:
            os.close(descriptor)
            raise

    def _cache_admin_identity(self) -> None:
        admin, common = self._verified_admin_paths()
        admin_fd, admin_identity = self._open_verified_directory(admin, None)
        try:
            common_fd, common_identity = self._open_verified_directory(common, None)
            try:
                self._admin_path = admin
                self._common_path = common
                self._admin_identity = admin_identity
                self._common_identity = common_identity
                self._verified_admin_paths()
            finally:
                os.close(common_fd)
        finally:
            os.close(admin_fd)

    def _verified_admin_paths(self) -> tuple[Path, Path]:
        admin_raw = self._git(["rev-parse", "--absolute-git-dir"], max_stdout=16 * 1024)
        common_raw = self._git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], max_stdout=16 * 1024,
        )
        assert admin_raw is not None and common_raw is not None
        try:
            admin = Path(self._text(admin_raw, "bootstrap"))
            common = Path(self._text(common_raw, "bootstrap"))
            admin_info = admin.lstat()
            common_info = common.lstat()
            if (
                not admin.is_absolute() or not common.is_absolute()
                or stat.S_ISLNK(admin_info.st_mode) or stat.S_ISLNK(common_info.st_mode)
                or not stat.S_ISDIR(admin_info.st_mode) or not stat.S_ISDIR(common_info.st_mode)
                or admin.resolve(strict=True) != admin or common.resolve(strict=True) != common
                or admin_info.st_dev != common_info.st_dev
            ):
                raise ValueError("unsafe Git administration roots")
            admin_identity = (admin_info.st_dev, admin_info.st_ino)
            common_identity = (common_info.st_dev, common_info.st_ino)
            if self._admin_identity is not None and (
                admin != self._admin_path or admin_identity != self._admin_identity
            ):
                raise ValueError("Git administration directory changed")
            if self._common_identity is not None and (
                common != self._common_path or common_identity != self._common_identity
            ):
                raise ValueError("Git common directory changed")
            pointer = self.root / ".git"
            pointer_info = pointer.lstat()
            if stat.S_ISLNK(pointer_info.st_mode):
                raise ValueError("symbolic Git pointer")
            if admin == common:
                if not stat.S_ISDIR(pointer_info.st_mode) or pointer.resolve(strict=True) != admin:
                    raise ValueError("main worktree administration mismatch")
            else:
                if (
                    admin.parent != common / "worktrees"
                    or not stat.S_ISREG(pointer_info.st_mode)
                    or pointer_info.st_size > 4096
                ):
                    raise ValueError("linked worktree administration mismatch")
                pointer_data = self._read_admin_file(pointer)
                if (
                    not pointer_data.startswith(b"gitdir: ") or not pointer_data.endswith(b"\n")
                    or pointer_data.count(b"\n") != 1
                ):
                    raise ValueError("malformed Git worktree pointer")
                pointer_target = Path(os.fsdecode(pointer_data[len(b"gitdir: "):-1]))
                if not pointer_target.is_absolute():
                    pointer_target = self.root / pointer_target
                if pointer_target.resolve(strict=True) != admin:
                    raise ValueError("Git worktree pointer mismatch")
                backlink = admin / "gitdir"
                backlink_data = self._read_admin_file(backlink)
                backlink_target = self._admin_path_line(backlink_data)
                if not backlink_target.is_absolute():
                    backlink_target = admin / backlink_target
                if backlink_target.resolve(strict=True) != pointer.resolve(strict=True):
                    raise ValueError("Git worktree backlink mismatch")
                common_file = admin / "commondir"
                common_data = self._read_admin_file(common_file)
                common_target = self._admin_path_line(common_data)
                if not common_target.is_absolute():
                    common_target = admin / common_target
                if common_target.resolve(strict=True) != common:
                    raise ValueError("Git common directory mismatch")
            return admin, common
        except (OSError, UnicodeError, ValueError):
            raise DistinctTaskGitError("unsafe_git_administration", "bootstrap") from None

    def _lock_names(self) -> tuple[Path, tuple[str, str]]:
        git_dir, _common = self._verified_admin_paths()
        head_raw = self._git(
            ["rev-parse", "--path-format=absolute", "--git-path", "HEAD"], max_stdout=16 * 1024,
        )
        index_raw = self._git(
            ["rev-parse", "--path-format=absolute", "--git-path", "index"], max_stdout=16 * 1024,
        )
        assert head_raw is not None and index_raw is not None
        try:
            paths = [Path(self._text(item, "bootstrap")) for item in (head_raw, index_raw)]
            if any(not item.is_absolute() or item.parent.resolve(strict=True) != git_dir for item in paths):
                raise ValueError
            if [item.name for item in paths] != ["HEAD", "index"]:
                raise ValueError
            # Match Git's index-then-HEAD lock order used by checkout/switch.
            return git_dir, ("index.lock", "HEAD.lock")
        except (OSError, ValueError):
            raise DistinctTaskGitError("unsafe_lock_path", "bootstrap") from None

    @staticmethod
    def _write_private_temp_file(path: Path, content: bytes) -> None:
        if len(content) > 4096:
            raise ValueError("temporary Git administration file exceeds limit")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("unsafe temporary Git administration file")
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("short Git administration file write")
                offset += written
            after = os.fstat(descriptor)
            named = path.lstat()
            if (
                (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
                or after.st_size != len(content)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(named.st_mode) or named.st_uid != os.getuid()
            ):
                raise ValueError("temporary Git administration file changed")
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_stable_admin_support() -> None:
        if (
            sys.platform != "linux" or not Path("/proc/self/fd").is_dir()
            or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
        ):
            raise DistinctTaskGitError("stable_git_administration_unsupported", "bootstrap")

    @contextmanager
    def _pinned_scratch_admin(self, head: str) -> Iterator[tuple[Path, str]]:
        """Keep Git's common dir bound to its constructor-pinned directory inode."""
        self._require_stable_admin_support()
        _admin, common = self._verified_admin_paths()
        common_fd, common_identity = self._open_verified_directory(common, self._common_identity)
        common_handle = f"/proc/{os.getpid()}/fd/{common_fd}"
        if len(common_handle.encode("ascii")) > 4096:
            os.close(common_fd)
            raise DistinctTaskGitError("unsafe_git_administration", "bootstrap")
        try:
            if os.stat(common_handle).st_ino != common_identity[1] or os.stat(common_handle).st_dev != common_identity[0]:
                raise DistinctTaskGitError("unsafe_git_administration", "bootstrap")
            with tempfile.TemporaryDirectory(prefix="agentcore-task-ref-", dir="/tmp") as directory:
                scratch = Path(directory)
                info = scratch.lstat()
                if (
                    stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or scratch.resolve(strict=True) != scratch or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise DistinctTaskGitError("unsafe_temporary_git_administration", "bootstrap")
                self._write_private_temp_file(scratch / "HEAD", head.encode("ascii") + b"\n")
                self._write_private_temp_file(scratch / "commondir", common_handle.encode("ascii") + b"\n")
                try:
                    yield scratch, common_handle
                finally:
                    fd_info = os.fstat(common_fd)
                    if (fd_info.st_dev, fd_info.st_ino) != self._common_identity:
                        raise DistinctTaskGitError("unsafe_git_administration", "bootstrap")
                    self._verified_admin_paths()
        finally:
            os.close(common_fd)

    def _create_bootstrap_commit(self, tree: str) -> str:
        subject = _COMMIT_SUBJECT.format(task=self.task)
        with self._pinned_scratch_admin(self.base_revision) as (scratch, common_handle):
            created = self._git(
                [
                    "--git-dir", str(scratch), "-c", "user.name=Agent Core",
                    "-c", "user.email=agent-core@users.noreply.github.com",
                    "-c", "commit.gpgsign=false", "commit-tree", tree,
                    "-p", self.base_revision, "-m", subject,
                ],
                max_stdout=128,
                extra_env={"GIT_COMMON_DIR": common_handle},
            )
            assert created is not None
        return self._oid(created, "bootstrap")

    def _update_task_ref(self, old: str, new: str) -> None:
        """CAS in detached scratch HEAD, never borrowing the root HEAD reflog lock."""
        with self._pinned_scratch_admin(old) as (scratch, common_handle):
            self._git(
                ["--git-dir", str(scratch), "update-ref", "--no-deref", self.branch_ref, new, old],
                max_stdout=4096,
                extra_env={"GIT_COMMON_DIR": common_handle},
            )

    @contextmanager
    def _local_locks(self) -> Iterator[None]:
        admin_path, lock_names = self._lock_names()
        admin_fd, _identity = self._open_verified_directory(admin_path, self._admin_identity)
        owned: list[tuple[str, int, int]] = []
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            for name in ("HEAD", "index"):
                info = os.stat(name, dir_fd=admin_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise DistinctTaskGitError("unsafe_lock_path", "bootstrap")
            for name in lock_names:
                descriptor: int | None = None
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=admin_fd)
                    info = os.fstat(descriptor)
                    owned.append((name, info.st_dev, info.st_ino))
                    if (
                        not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077
                    ):
                        raise OSError("not regular")
                except OSError:
                    raise DistinctTaskGitError("local_lock_unavailable", "bootstrap") from None
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            yield
        finally:
            cleanup_failed = False
            for name, device, inode in reversed(owned):
                try:
                    info = os.stat(name, dir_fd=admin_fd, follow_symlinks=False)
                    if (
                        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and (info.st_dev, info.st_ino) == (device, inode)
                    ):
                        os.unlink(name, dir_fd=admin_fd)
                    else:
                        cleanup_failed = True
                except OSError:
                    cleanup_failed = True
            os.close(admin_fd)
            if cleanup_failed:
                raise DistinctTaskGitError("local_lock_cleanup_failed", "bootstrap")

    def _commit_details(self, oid: str) -> tuple[str, tuple[str, ...], bytes]:
        raw = self._git(["cat-file", "commit", oid], max_stdout=1024 * 1024)
        assert raw is not None
        header, separator, message = raw.partition(b"\n\n")
        if not separator:
            raise DistinctTaskGitError("invalid_candidate", "bootstrap")
        tree: str | None = None
        parents: list[str] = []
        for line in header.splitlines():
            if line.startswith(b"tree "):
                if tree is not None:
                    raise DistinctTaskGitError("invalid_candidate", "bootstrap")
                tree = self._oid(line[5:], "bootstrap")
            elif line.startswith(b"parent "):
                parents.append(self._oid(line[7:], "bootstrap"))
        if tree is None:
            raise DistinctTaskGitError("invalid_candidate", "bootstrap")
        return tree, tuple(parents), message

    def _validate_candidate(self, candidate: str, tree: str) -> None:
        if not self._valid_oid(candidate):
            raise DistinctTaskGitError("invalid_candidate", "bootstrap")
        self._require_commit(candidate)
        candidate_tree, parents, message = self._commit_details(candidate)
        if candidate_tree != tree or parents != (self.base_revision,):
            raise DistinctTaskGitError("invalid_candidate", "bootstrap")
        if message != (_COMMIT_SUBJECT.format(task=self.task) + "\n").encode("utf-8"):
            raise DistinctTaskGitError("invalid_candidate", "bootstrap")

    def bootstrap_if_needed(
        self,
        *,
        expected_head: str,
        on_candidate: Callable[[str], object] | None = None,
        resume_candidate: str | None = None,
    ) -> str:
        with self._operation("bootstrap"):
            self._assert_task_ref("bootstrap")
            if not self._valid_oid(expected_head):
                raise DistinctTaskGitError("invalid_expected_head", "bootstrap")
            if on_candidate is not None and not callable(on_candidate):
                raise DistinctTaskGitError("invalid_callback", "bootstrap")
            facts = self.observe()
            if facts.head != expected_head:
                raise DistinctTaskGitError("head_mismatch", "bootstrap")
            if facts.head != self.base_revision:
                if not self.is_ancestor(self.base_revision, facts.head):
                    raise DistinctTaskGitError("task_history_conflict", "bootstrap")
                if resume_candidate is not None:
                    if resume_candidate != facts.head:
                        raise DistinctTaskGitError("resume_candidate_head_mismatch", "bootstrap")
                    self._validate_candidate(resume_candidate, facts.tree)
                final_facts = self.observe()
                if final_facts != facts:
                    raise DistinctTaskGitError("bootstrap_noop_state_changed", "bootstrap")
                return facts.head
            self._require_stable_admin_support()
            if not facts.clean:
                raise DistinctTaskGitError("dirty_worktree", "bootstrap")
            remote = self._remote_head()
            if remote not in (None, self.base_revision):
                raise DistinctTaskGitError("remote_task_ref_conflict", "bootstrap")

            with self._local_locks():
                locked = self.observe()
                if locked != facts:
                    raise DistinctTaskGitError("local_state_changed", "bootstrap")
                if not locked.clean or locked.head != self.base_revision:
                    raise DistinctTaskGitError("dirty_worktree", "bootstrap")
                if self._remote_head() != remote:
                    raise DistinctTaskGitError("remote_task_ref_conflict", "bootstrap")
                if resume_candidate is not None:
                    candidate = resume_candidate
                    self._validate_candidate(candidate, locked.tree)
                else:
                    candidate = self._create_bootstrap_commit(locked.tree)
                    self._validate_candidate(candidate, locked.tree)

                callback_failed = False
                if on_candidate is not None:
                    try:
                        if on_candidate(candidate) is False:
                            callback_failed = True
                    except Exception:
                        callback_failed = True
                after_callback: LocalFacts | None = None
                after_remote: str | None = None
                callback_observation_failed = False
                try:
                    after_callback = self.observe()
                except DistinctTaskGitError:
                    callback_observation_failed = True
                try:
                    after_remote = self._remote_head()
                except DistinctTaskGitError:
                    callback_observation_failed = True
                if callback_failed:
                    raise DistinctTaskGitError("candidate_callback_failed", "bootstrap")
                if callback_observation_failed:
                    raise DistinctTaskGitError("candidate_revalidation_failed", "bootstrap")
                if after_callback != locked or after_remote != remote:
                    raise DistinctTaskGitError("candidate_state_changed", "bootstrap")

                write_error: DistinctTaskGitError | None = None
                write_failed = False
                try:
                    self._update_task_ref(self.base_revision, candidate)
                except DistinctTaskGitError as error:
                    write_error = error
                except Exception:
                    write_failed = True
                post: LocalFacts | None = None
                try:
                    post = self.observe()
                except DistinctTaskGitError:
                    raise DistinctTaskGitError("bootstrap_postcondition_unavailable", "bootstrap") from None
                if (
                    post.head == candidate and post.tree == locked.tree and post.clean
                    and post.branch_ref == self.branch_ref and post.worktree == locked.worktree
                    and post.index_fingerprint == locked.index_fingerprint
                    and post.status_fingerprint == locked.status_fingerprint
                ):
                    return candidate
                if post.head == candidate:
                    raise DistinctTaskGitError("bootstrap_applied_local_state_changed", "bootstrap")
                if post.head == self.base_revision and write_error is not None:
                    if write_error.code == "git_operation_failed":
                        raise DistinctTaskGitError("branch_compare_exchange_failed", "bootstrap") from None
                    raise DistinctTaskGitError(write_error.code, "bootstrap") from None
                if write_failed:
                    raise DistinctTaskGitError("branch_compare_exchange_failed", "bootstrap")
                raise DistinctTaskGitError("bootstrap_postcondition_mismatch", "bootstrap")

    def push_exact(
        self,
        *,
        expected_head: str,
        expected_remote: str | None,
        on_intent: Callable[[BranchPush], object] | None = None,
    ) -> str:
        with self._operation("push"):
            self._assert_task_ref("push")
            if not self._valid_oid(expected_head):
                raise DistinctTaskGitError("invalid_expected_head", "push")
            if expected_remote is not None and not self._valid_oid(expected_remote):
                raise DistinctTaskGitError("invalid_expected_remote", "push")
            if on_intent is not None and not callable(on_intent):
                raise DistinctTaskGitError("invalid_callback", "push")
            facts = self.observe()
            if facts.head != expected_head:
                raise DistinctTaskGitError("head_mismatch", "push")
            if not self.is_ancestor(self.base_revision, expected_head):
                raise DistinctTaskGitError("task_history_conflict", "push")
            remote = self._remote_head()
            if remote != expected_remote:
                raise DistinctTaskGitError("remote_head_mismatch", "push")
            if remote == expected_head:
                final_facts: LocalFacts | None = None
                final_remote: str | None = None
                final_read_failed = False
                try:
                    final_facts = self.observe()
                except DistinctTaskGitError:
                    final_read_failed = True
                try:
                    final_remote = self._remote_head()
                except DistinctTaskGitError:
                    final_read_failed = True
                if final_read_failed:
                    raise DistinctTaskGitError("push_noop_postcondition_unavailable", "push")
                if final_facts != facts or final_remote != expected_head:
                    raise DistinctTaskGitError("push_noop_postcondition_mismatch", "push")
                return expected_head
            if remote is not None:
                self._ensure_remote_commit(remote, facts)
                if not self.is_ancestor(remote, expected_head):
                    raise DistinctTaskGitError("non_fast_forward", "push")
            intent = BranchPush(self.repository, self.task, self.branch_ref, expected_head, expected_remote)
            callback_failed = False
            if on_intent is not None:
                try:
                    if on_intent(intent) is False:
                        callback_failed = True
                except Exception:
                    callback_failed = True
            pre_publish: LocalFacts | None = None
            remote_before: str | None = None
            revalidation_failed = False
            try:
                pre_publish = self.observe()
            except DistinctTaskGitError:
                revalidation_failed = True
            try:
                remote_before = self._remote_head()
            except DistinctTaskGitError:
                revalidation_failed = True
            if callback_failed:
                raise DistinctTaskGitError("intent_callback_failed", "push")
            if revalidation_failed:
                raise DistinctTaskGitError("intent_revalidation_failed", "push")
            if pre_publish != facts or remote_before != expected_remote:
                raise DistinctTaskGitError("intent_state_changed", "push")

            publish_failed = False
            try:
                self._publish(intent)
            except Exception:
                publish_failed = True
            post: LocalFacts | None = None
            post_remote: str | None = None
            post_failed = False
            try:
                post = self.observe()
            except DistinctTaskGitError:
                post_failed = True
            try:
                post_remote = self._remote_head()
            except DistinctTaskGitError:
                post_failed = True
            if post_failed:
                raise DistinctTaskGitError("push_postcondition_unavailable", "push")
            if post != facts:
                raise DistinctTaskGitError("local_state_changed_after_push", "push")
            if post_remote == expected_head:
                return expected_head
            if publish_failed:
                raise DistinctTaskGitError("publish_failed_unconfirmed", "push")
            if post_remote != expected_remote:
                raise DistinctTaskGitError("remote_postcondition_mismatch", "push")
            raise DistinctTaskGitError("push_not_confirmed", "push")


__all__ = [
    "BranchPush",
    "DistinctTaskGitError",
    "LocalFacts",
    "TaskGit",
]
