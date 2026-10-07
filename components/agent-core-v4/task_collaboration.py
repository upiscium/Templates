"""Host-gated durable Task/PR collaboration for Agent Core v4.

This module is deliberately a staged facade, not a live GitHub adapter.  The
host installs bounded authenticated readers, the operation-local prerequisite
engines, an independent execution authorizer, a durable intent recorder, and a
prose/content policy.  The facade has no generic GitHub request method and
never publishes #217 metadata itself.

GitHub title/body edits are guarded by exact pre- and post-observations.  The
GitHub API does not provide an atomic compare-and-swap for these fields, so a
concurrent edit (including an ABA edit) cannot be excluded by this client; the
transport should enforce the expected old values where its API permits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

import metadata_codec as codec
import operation_prerequisites as prerequisites
import collaboration_git
import task_record
import task_view
from metadata_ref import MetadataStore


SCHEMA_VERSION = 1
MAX_GITHUB_PAGES = 100
GITHUB_PAGE_SIZE = 100
MAX_GITHUB_ITEMS = MAX_GITHUB_PAGES * GITHUB_PAGE_SIZE
MAX_TITLE_BYTES = 240
MAX_PR_BODY_BYTES = 16 * 1024
MAX_REPORT_BYTES = 4 * 1024
MAX_COMMENT_BODY_BYTES = 32 * 1024
MAX_READ_TITLE_BYTES = 1024
MAX_READ_BODY_BYTES = 64 * 1024
MAX_READ_COMMENT_BODY_BYTES = 64 * 1024
MAX_GITHUB_READ_BYTES = 8 * 1024 * 1024
MAX_KEY_LENGTH = 128
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_IDENT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_CHECKPOINT_MARKER = re.compile(
    r"<!-- agentcore-task-checkpoint/v1 sha256=([0-9a-f]{64}) -->\Z", re.ASCII,
)
_COMMENT_MARKER = re.compile(
    r"<!-- agentcore-task-comment/v1 sha256=([0-9a-f]{64}) -->\Z", re.ASCII,
)
_INTENT_DOMAIN = b"agentcore-task-collaboration-write-intent/v1\n"
_REQUEST_DOMAIN = b"agentcore-operation-request/v1\n"
_VIEW_DOMAIN = b"agentcore-live-task-view-fingerprint/v1\n"
_CHECKPOINT_DOMAIN = b"agentcore-task-collaboration-checkpoint/v1\n"
_COMMENT_DOMAIN = b"agentcore-task-collaboration-comment/v1\n"
_UNCHANGED = object()

OPERATIONS = frozenset({
    "collaboration.bootstrap",
    "collaboration.push",
    "collaboration.draft_pr",
    "collaboration.metadata_update",
    "collaboration.initial_checkpoint",
    "collaboration.turn_checkpoint",
    "collaboration.factual_comment",
})


class TaskCollaborationError(ValueError):
    """A safe error code; callback, Git, and transport details are not exposed."""

    def __init__(
        self,
        code: str,
        *,
        operation: str | None = None,
        diagnosis_bytes: bytes | None = None,
        human_decision_required: bool = False,
    ) -> None:
        if type(code) is not str or not _IDENT.fullmatch(code):
            code = "task_collaboration_error"
        self.code = code
        self.operation = operation if operation in OPERATIONS else None
        self.diagnosis_bytes = diagnosis_bytes
        self.human_decision_required = human_decision_required is True
        super().__init__(code)


@dataclass(frozen=True)
class RepositoryFacts:
    repository: str
    default_branch_ref: str
    repository_id: int


@dataclass(frozen=True)
class PullFacts:
    """Closed facts for one PR, including all fields the facade may edit."""

    number: int
    base_repository: str
    head_repository: str | None
    base_ref: str
    head_ref: str
    head_oid: str | None
    state: str
    draft: bool
    title: str
    body: str


@dataclass(frozen=True)
class IssueFacts:
    repository: str
    number: int
    title: str
    body: str
    state: str


@dataclass(frozen=True)
class CommentFacts:
    repository: str
    kind: str
    number: int
    comment_id: int
    author_id: int
    body: str


class GitHubTransport(Protocol):
    """Operation-specific bounded transport; no raw requests or generic writes."""

    def repository(self) -> RepositoryFacts: ...
    def principal(self) -> int: ...
    def pull_page(self, page: int) -> Sequence[PullFacts]: ...
    def comments_page(self, kind: str, number: int, page: int) -> Sequence[CommentFacts]: ...
    def issue(self, number: int) -> IssueFacts: ...
    def create_draft(self, intent: "CreateDraftIntent") -> object: ...
    def edit_metadata(self, intent: "EditMetadataIntent") -> object: ...
    def post_comment(self, intent: "PostCommentIntent") -> object: ...


@dataclass(frozen=True)
class MetadataBinding:
    """Exact #217/#191/#144 handoff supplied by the existing host writers."""

    metadata_commit: str
    record_id: str
    contract_id: str
    snapshot_id: str


@dataclass(frozen=True)
class WriteIntent:
    """Domain-separated immutable intent acknowledged by the installed host."""

    operation: str
    repository: str
    task: str
    branch_ref: str
    subject: str
    record_id: str
    contract_id: str
    principal: int
    parameters: bytes
    request_id: str
    policy_id: str
    view_id: str
    intent_id: str


@dataclass(frozen=True)
class ExecutionAuthorizationRequest:
    """Separate host execution decision; positive prerequisites are not authority."""

    operation: str
    repository: str
    task: str
    branch_ref: str
    subject: str
    principal: int
    request_id: str
    policy_id: str
    view_id: str
    request_bytes: bytes
    intent_id: str
    intent_parameters: bytes


@dataclass(frozen=True)
class ContentPolicyRequest:
    operation: str
    repository: str
    task: str
    branch_ref: str
    subject: str
    principal: int
    request_id: str
    policy_id: str
    view_id: str
    content: bytes
    intent_id: str


@dataclass(frozen=True)
class CreateDraftIntent:
    repository: str
    task: str
    branch_ref: str
    base_ref: str
    head_oid: str
    title: str
    body: str
    intent: WriteIntent


@dataclass(frozen=True)
class EditMetadataIntent:
    repository: str
    task: str
    number: int
    branch_ref: str
    subject: str
    expected_title: str
    expected_body: str
    new_title: str
    new_body: str
    intent: WriteIntent


@dataclass(frozen=True)
class PostCommentIntent:
    repository: str
    kind: str
    number: int
    body: str
    intent: WriteIntent


@dataclass(frozen=True)
class CollaborationReceipt:
    repository: str
    task: str
    branch_ref: str
    subject: str
    pull_request: int | None
    comment_id: int | None = None
    metadata_commit: str | None = None
    record_id: str | None = None
    contract_id: str | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True)
class _Capture:
    local: object
    remote: str | None
    view: dict[str, Any]
    view_bytes: bytes
    principal: int
    pull: PullFacts | None
    repository_facts: RepositoryFacts
    request: prerequisites.OperationRequest
    request_bytes: bytes
    diagnosis: dict[str, Any]


@dataclass(frozen=True)
class _LocalFacts:
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    worktree: str
    clean: bool
    index_fingerprint: str
    status_fingerprint: str


def _canonical(value: object, code: str = "invalid_payload") -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TaskCollaborationError(code) from None


def _decode_canonical(data: bytes) -> object:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=pairs)
        if _canonical(value) != data:
            raise ValueError("noncanonical")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise TaskCollaborationError("invalid_checkpoint_capsule") from None


def _require_text(
    value: object,
    maximum: int,
    code: str,
    *,
    allow_empty: bool = True,
    allow_multiline: bool = False,
) -> str:
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value.strip()):
        raise TaskCollaborationError(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise TaskCollaborationError(code) from None
    if len(encoded) > maximum or any(
        (ord(ch) < 0x20 and not (allow_multiline and ch in "\n\t")) or ord(ch) == 0x7F
        for ch in value
    ):
        raise TaskCollaborationError(code)
    return value


def _require_oid(value: object, code: str = "invalid_oid") -> str:
    if type(value) is not str or not _OID.fullmatch(value):
        raise TaskCollaborationError(code)
    return value


def _require_metadata_id(value: object, code: str = "invalid_metadata_id") -> str:
    if type(value) is not str or not _HEX_64.fullmatch(value):
        raise TaskCollaborationError(code)
    return value


def _require_branch(value: object, code: str = "invalid_branch_ref") -> str:
    try:
        if type(value) is not str or len(value) > task_view.MAX_BRANCH_REF_LENGTH:
            raise ValueError
        branch = task_record.validate_branch_ref(value)
        if len(branch.encode("utf-8", errors="strict")) > task_view.MAX_BRANCH_REF_LENGTH:
            raise ValueError
        return branch
    except (task_record.TaskRecordError, TypeError, ValueError, UnicodeEncodeError):
        raise TaskCollaborationError(code) from None


def _valid_repo(value: object) -> str:
    if type(value) is not str:
        raise TaskCollaborationError("repository_binding_mismatch")
    try:
        codec.encode_object("contract", value, "1", "0" * 40, {})
    except (codec.MetadataCodecError, TypeError, ValueError):
        raise TaskCollaborationError("repository_binding_mismatch") from None
    return value


def _principal(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise TaskCollaborationError("github_principal_unavailable")
    return value


def _as_detached_view(value: object) -> tuple[dict[str, Any], bytes]:
    data = _canonical(value, "invalid_live_task_view")
    try:
        detached = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise TaskCollaborationError("invalid_live_task_view") from None
    if type(detached) is not dict:
        raise TaskCollaborationError("invalid_live_task_view")
    return detached, data


def _validate_repo_facts(value: object, repository: str, default_branch: str) -> RepositoryFacts:
    if type(value) is not RepositoryFacts:
        raise TaskCollaborationError("invalid_github_facts")
    _valid_repo(value.repository)
    _require_branch(value.default_branch_ref)
    if (
        value.repository != repository
        or value.default_branch_ref != default_branch
        or type(value.repository_id) is not int
        or value.repository_id <= 0
    ):
        raise TaskCollaborationError("github_repository_binding_mismatch")
    return value


def _validate_pull(value: object, repository: str) -> PullFacts:
    if type(value) is not PullFacts:
        raise TaskCollaborationError("invalid_github_facts")
    if (
        type(value.number) is not int or value.number <= 0
        or type(value.base_ref) is not str or type(value.head_ref) is not str
        or type(value.state) is not str
    ):
        raise TaskCollaborationError("invalid_github_facts")
    _valid_repo(value.base_repository)
    if value.head_repository is not None:
        _valid_repo(value.head_repository)
    _require_branch("refs/heads/" + value.base_ref)
    _require_branch("refs/heads/" + value.head_ref)
    if value.head_oid is not None:
        _require_oid(value.head_oid)
    _require_text(value.title, MAX_READ_TITLE_BYTES, "invalid_github_facts", allow_empty=False)
    _require_text(value.body, MAX_READ_BODY_BYTES, "invalid_github_facts", allow_multiline=True)
    if value.state not in {"open", "closed", "merged"} or type(value.draft) is not bool:
        raise TaskCollaborationError("invalid_github_facts")
    if value.base_repository != repository:
        raise TaskCollaborationError("cross_repository_pull_request")
    return value


def _validate_issue(value: object, repository: str, number: int) -> IssueFacts:
    if type(value) is not IssueFacts:
        raise TaskCollaborationError("invalid_github_facts")
    if (
        type(value.repository) is not str
        or type(value.number) is not int
        or type(value.state) is not str
        or value.repository != repository
        or value.number != number
        or value.state not in {"open", "closed"}
    ):
        raise TaskCollaborationError("issue_binding_mismatch")
    _require_text(value.title, MAX_READ_TITLE_BYTES, "invalid_github_facts")
    _require_text(value.body, MAX_READ_BODY_BYTES, "invalid_github_facts", allow_multiline=True)
    return value


class TaskCollaboration:
    """Durable collaboration facade bound to one exact Task and TaskGit."""

    def __init__(
        self,
        store: MetadataStore,
        git: object,
        github: GitHubTransport,
        *,
        prerequisites_by_operation: Mapping[str, prerequisites.OperationPrerequisites],
        authorize: Callable[[ExecutionAuthorizationRequest], object],
        record_intent: Callable[[WriteIntent], object],
        content_policy: Callable[[ContentPolicyRequest], object],
    ) -> None:
        if not isinstance(store, MetadataStore):
            raise TaskCollaborationError("invalid_metadata_store")
        if not isinstance(prerequisites_by_operation, Mapping):
            raise TaskCollaborationError("operation_prerequisites_required")
        if not callable(authorize) or not callable(record_intent) or not callable(content_policy):
            raise TaskCollaborationError("host_capability_required")
        try:
            bound_store = git.store
            repository = _valid_repo(store.repository)
            git_repository = _valid_repo(git.repository)
            task = git.task
            branch_ref = _require_branch(git.branch_ref)
            base_revision = _require_oid(git.base_revision)
            default_branch_ref = _require_branch(git.default_branch_ref)
        except TaskCollaborationError:
            raise
        except Exception:
            raise TaskCollaborationError("task_git_binding_mismatch") from None
        if (
            bound_store is not store
            or git_repository != repository
            or getattr(bound_store, "root", None) != store.root
            or getattr(git, "root", store.root) != store.root
            or branch_ref == default_branch_ref
            or type(task) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,127}", task, re.ASCII)
        ):
            raise TaskCollaborationError("task_git_binding_mismatch")
        installed: dict[str, prerequisites.OperationPrerequisites] = {}
        for operation, engine in prerequisites_by_operation.items():
            if operation not in OPERATIONS or type(engine) is not prerequisites.OperationPrerequisites:
                raise TaskCollaborationError("invalid_installed_prerequisites")
            installed[operation] = engine
        self._store = store
        self._git = git
        self._github = github
        self._repository = repository
        self._task = task
        self._branch_ref = branch_ref
        self._base_revision = base_revision
        self._default_branch_ref = default_branch_ref
        self._engines = installed
        self._authorize = authorize
        self._record_intent = record_intent
        self._content_policy = content_policy
        self._repo_facts: RepositoryFacts | None = None
        self._repo_facts = self._read_repository()

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def task(self) -> str:
        return self._task

    @property
    def branch_ref(self) -> str:
        return self._branch_ref

    def _read_repository(self) -> RepositoryFacts:
        try:
            value = self._github.repository()
        except Exception:
            raise TaskCollaborationError("github_repository_unavailable") from None
        facts = _validate_repo_facts(value, self._repository, self._default_branch_ref)
        previous = getattr(self, "_repo_facts", None)
        if previous is not None and facts != previous:
            raise TaskCollaborationError("github_repository_identity_changed")
        return facts

    def _read_principal(self) -> int:
        try:
            return _principal(self._github.principal())
        except TaskCollaborationError:
            raise
        except Exception:
            raise TaskCollaborationError("github_principal_unavailable") from None

    def _read_local(self) -> object:
        try:
            facts = self._git.observe()
            values = {
                name: getattr(facts, name)
                for name in (
                    "repository", "task", "branch_ref", "head", "tree", "worktree",
                    "clean", "index_fingerprint", "status_fingerprint",
                )
            }
        except Exception:
            raise TaskCollaborationError("local_facts_unavailable") from None
        if (
            type(values["repository"]) is not str
            or type(values["task"]) is not str
            or type(values["branch_ref"]) is not str
            or type(values["worktree"]) is not str
        ):
            raise TaskCollaborationError("local_task_binding_mismatch")
        try:
            worktree_bytes = values["worktree"].encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise TaskCollaborationError("local_task_binding_mismatch") from None
        if (
            values["repository"] != self._repository
            or values["task"] != self._task
            or values["branch_ref"] != self._branch_ref
            or type(values["head"]) is not str
            or type(values["tree"]) is not str
            or len(values["head"]) != getattr(self._store, "_oid_length", -1)
            or len(values["tree"]) != getattr(self._store, "_oid_length", -1)
            or len(values["head"]) != len(values["tree"])
            or not _OID.fullmatch(values["head"])
            or not _OID.fullmatch(values["tree"])
            or type(values["clean"]) is not bool
            or type(values["worktree"]) is not str
            or type(values["index_fingerprint"]) is not str
            or type(values["status_fingerprint"]) is not str
            or len(worktree_bytes) > 4096
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in values["worktree"])
            or not _HEX_64.fullmatch(values["index_fingerprint"])
            or not _HEX_64.fullmatch(values["status_fingerprint"])
        ):
            raise TaskCollaborationError("local_task_binding_mismatch")
        try:
            if Path(values["worktree"]).resolve(strict=True) != self._store.root.resolve(strict=True):
                raise TaskCollaborationError("local_worktree_binding_mismatch")
        except (OSError, RuntimeError, ValueError):
            raise TaskCollaborationError("local_worktree_binding_mismatch") from None
        # Detach the producer's object. The concrete TaskGit emits a frozen
        # value, but normalizing here also makes duck-typed test adapters unable
        # to mutate a captured guard or authorization context after observation.
        return _LocalFacts(**values)

    def _read_remote_head(self) -> str | None:
        try:
            result = self._git.remote_head()
        except Exception:
            raise TaskCollaborationError("remote_head_unavailable") from None
        if result is not None:
            _require_oid(result, "invalid_remote_head")
        return result

    def _pulls_once(self) -> tuple[PullFacts, ...]:
        result: list[PullFacts] = []
        seen: set[int] = set()
        total_bytes = 0
        for page in range(1, MAX_GITHUB_PAGES + 1):
            try:
                values = self._github.pull_page(page)
            except Exception:
                raise TaskCollaborationError("github_pull_list_unavailable") from None
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                raise TaskCollaborationError("invalid_github_page")
            if len(values) > GITHUB_PAGE_SIZE or len(result) + len(values) > MAX_GITHUB_ITEMS:
                raise TaskCollaborationError("github_page_limit")
            page_values = tuple(_validate_pull(item, self._repository) for item in values)
            for item in page_values:
                total_bytes += len(item.title.encode("utf-8")) + len(item.body.encode("utf-8"))
                if total_bytes > MAX_GITHUB_READ_BYTES:
                    raise TaskCollaborationError("github_read_size_limit")
                if item.number in seen:
                    raise TaskCollaborationError("github_pagination_duplicate")
                seen.add(item.number)
            result.extend(page_values)
            if len(values) < GITHUB_PAGE_SIZE:
                return tuple(sorted(result, key=lambda item: item.number))
        raise TaskCollaborationError("github_page_limit")

    def _stable_pulls(self) -> tuple[PullFacts, ...]:
        repository_before = self._read_repository()
        first = self._pulls_once()
        second = self._pulls_once()
        repository_after = self._read_repository()
        if repository_before != repository_after or first != second:
            raise TaskCollaborationError("github_pagination_drift")
        return second

    def _comments_once(self, kind: str, number: int) -> tuple[CommentFacts, ...]:
        if kind not in {"pr", "issue"} or type(number) is not int or number <= 0:
            raise TaskCollaborationError("invalid_comment_target")
        result: list[CommentFacts] = []
        seen: set[int] = set()
        total_bytes = 0
        for page in range(1, MAX_GITHUB_PAGES + 1):
            try:
                values = self._github.comments_page(kind, number, page)
            except Exception:
                raise TaskCollaborationError("github_comments_unavailable") from None
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                raise TaskCollaborationError("invalid_github_page")
            if len(values) > GITHUB_PAGE_SIZE or len(result) + len(values) > MAX_GITHUB_ITEMS:
                raise TaskCollaborationError("github_page_limit")
            for value in values:
                if type(value) is not CommentFacts:
                    raise TaskCollaborationError("invalid_github_facts")
                if (
                    type(value.repository) is not str
                    or type(value.kind) is not str
                    or type(value.number) is not int
                    or value.repository != self._repository
                    or value.kind != kind
                    or value.number != number
                    or type(value.comment_id) is not int
                    or value.comment_id <= 0
                    or type(value.author_id) is not int
                    or value.author_id <= 0
                ):
                    raise TaskCollaborationError("comment_binding_mismatch")
                _require_text(value.body, MAX_READ_COMMENT_BODY_BYTES, "invalid_github_facts", allow_multiline=True)
                total_bytes += len(value.body.encode("utf-8"))
                if total_bytes > MAX_GITHUB_READ_BYTES:
                    raise TaskCollaborationError("github_read_size_limit")
                if value.comment_id in seen:
                    raise TaskCollaborationError("github_pagination_duplicate")
                seen.add(value.comment_id)
                result.append(value)
            if len(values) < GITHUB_PAGE_SIZE:
                return tuple(sorted(result, key=lambda item: item.comment_id))
        raise TaskCollaborationError("github_page_limit")

    def _stable_comments(self, kind: str, number: int) -> tuple[CommentFacts, ...]:
        first = self._comments_once(kind, number)
        second = self._comments_once(kind, number)
        if first != second:
            raise TaskCollaborationError("github_pagination_drift")
        return second

    def _task_pulls(
        self,
        pulls: Sequence[PullFacts],
        subject: str,
        *,
        operation: str | None = None,
    ) -> tuple[PullFacts, ...]:
        head_name = self._branch_ref[len("refs/heads/"):]
        same_branch = tuple(pull for pull in pulls if pull.head_ref == head_name)
        if len(same_branch) > 1:
            raise TaskCollaborationError("duplicate_task_pull_requests")
        if not same_branch:
            return ()
        pull = same_branch[0]
        if (
            pull.base_repository != self._repository
            or pull.head_repository != self._repository
            or pull.head_ref != head_name
            or pull.base_ref != self._default_branch_ref[len("refs/heads/"):]
        ):
            raise TaskCollaborationError("pull_request_identity_conflict")
        if pull.state != "open":
            raise TaskCollaborationError(
                "historical_pull_request_conflict", operation=operation,
                human_decision_required=True,
            )
        if not pull.draft:
            raise TaskCollaborationError(
                "pull_request_not_draft", operation=operation,
                human_decision_required=True,
            )
        if pull.head_oid != subject:
            raise TaskCollaborationError("pull_request_head_mismatch")
        return (pull,)

    def _exact_pull(
        self,
        number: int,
        subject: str,
        *,
        operation: str | None = None,
    ) -> PullFacts:
        if type(number) is not int or number <= 0:
            raise TaskCollaborationError("invalid_pr_number")
        pulls = self._stable_pulls()
        task_pulls = self._task_pulls(pulls, subject, operation=operation)
        if len(task_pulls) != 1 or task_pulls[0].number != number:
            raise TaskCollaborationError("pull_request_identity_conflict")
        return task_pulls[0]

    def _confirm_pull_adoption(self, capture: _Capture, number: int) -> PullFacts:
        """Re-observe a no-write PR adoption around exact Task facts."""
        self._guard(capture)
        first = self._task_pulls(
            self._stable_pulls(), capture.local.head, operation=capture.request.operation,
        )
        if len(first) != 1 or first[0].number != number:
            raise TaskCollaborationError("pull_request_identity_conflict")
        self._guard(capture)
        second = self._task_pulls(
            self._stable_pulls(), capture.local.head, operation=capture.request.operation,
        )
        if len(second) != 1 or second[0].number != number or second[0] != first[0]:
            raise TaskCollaborationError("pull_request_changed")
        self._guard(capture)
        return second[0]

    def _confirm_pull_metadata(
        self,
        capture: _Capture,
        number: int,
        subject: str,
        title: str,
        body: str,
    ) -> PullFacts:
        """Confirm an already-desired title/body without trusting one read."""
        self._guard(capture, pr_number=number)
        first = self._exact_pull(number, subject, operation=capture.request.operation)
        if (first.title, first.body) != (title, body):
            raise TaskCollaborationError("pull_request_metadata_conflict", human_decision_required=True)
        self._guard(capture, pr_number=number, expected_pull_metadata=(title, body))
        second = self._exact_pull(number, subject, operation=capture.request.operation)
        if second != first:
            raise TaskCollaborationError("pull_request_changed")
        self._guard(capture, pr_number=number, expected_pull_metadata=(title, body))
        return second

    def _github_reader(self, number: int) -> Callable[[task_view.GitHubPullRequestRequest], object]:
        def reader(request: task_view.GitHubPullRequestRequest) -> object:
            if type(request) is not task_view.GitHubPullRequestRequest or request.number != number:
                raise TaskCollaborationError("github_reader_binding_mismatch")
            return self.github_observation(request)
        return reader

    def _observe_view(self, subject: str, pr_number: int | None) -> tuple[dict[str, Any], bytes]:
        try:
            view = task_view.observe_live_task(
                self._store,
                task=self._task,
                branch_ref=self._branch_ref,
                base_revision=self._base_revision,
                default_branch_ref=self._default_branch_ref,
                observe_remote_head=True,
                pr_number=pr_number,
                github_reader=None if pr_number is None else self._github_reader(pr_number),
            )
        except Exception:
            raise TaskCollaborationError("live_task_observation_failed") from None
        detached, data = _as_detached_view(view)
        try:
            authority = detached["authority"]
            if (
                detached["repository"] != self._repository
                or detached["task"] != self._task
                or detached["branch_ref"] != self._branch_ref
                or detached["subject"] != subject
                or authority["base_revision"] != self._base_revision
                or authority["branch_ref"] != self._branch_ref
                or authority["disposition"] is not None
                or type(authority["record_id"]) is not str
                or type(authority["contract_id"]) is not str
                or not _HEX_64.fullmatch(authority["record_id"])
                or not _HEX_64.fullmatch(authority["contract_id"])
            ):
                raise TaskCollaborationError("task_authority_binding_mismatch")
            if pr_number is not None and detached["github"]["state"] != "observed":
                raise TaskCollaborationError("pull_request_observation_unavailable")
        except (KeyError, TypeError):
            raise TaskCollaborationError("invalid_live_task_view") from None
        return detached, data

    def _capture(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        pr_number: int | None = None,
    ) -> _Capture:
        if operation not in OPERATIONS:
            raise TaskCollaborationError("unsupported_operation")
        engine = self._engines.get(operation)
        if engine is None:
            raise TaskCollaborationError("operation_prerequisites_unavailable")
        repository_facts = self._read_repository()
        principal = self._read_principal()
        local = self._read_local()
        subject = local.head
        remote = self._read_remote_head()
        pull = None if pr_number is None else self._exact_pull(pr_number, subject, operation=operation)
        view, view_bytes = self._observe_view(subject, pr_number)
        params = json.loads(_canonical(parameters, "invalid_operation_parameters").decode("utf-8"))
        request = prerequisites.OperationRequest(
            self._repository, self._task, self._branch_ref, subject, operation, params,
        )
        request_bytes = _canonical({
            "schema_version": prerequisites.SCHEMA_VERSION,
            "repository": self._repository,
            "task": self._task,
            "branch_ref": self._branch_ref,
            "subject": subject,
            "operation": operation,
            "parameters": params,
        }, "invalid_operation_parameters")
        try:
            diagnosis = engine.diagnose(request, view)
            diagnosis_bytes = prerequisites.encode_diagnosis(diagnosis)
        except Exception:
            raise TaskCollaborationError("operation_prerequisite_check_failed") from None
        try:
            if _canonical(view, "invalid_live_task_view") != view_bytes:
                raise TaskCollaborationError("live_task_view_mutated")
        except TaskCollaborationError:
            raise TaskCollaborationError("operation_context_mutated") from None
        expected_view_id = hashlib.sha256(_VIEW_DOMAIN + view_bytes).hexdigest()
        if (
            type(diagnosis) is not dict
            or diagnosis.get("view_id") != expected_view_id
        ):
            raise TaskCollaborationError("operation_view_binding_mismatch")
        if diagnosis.get("result") != "PREREQUISITES_SATISFIED":
            raise TaskCollaborationError(
                "operation_prerequisites_not_satisfied",
                operation=operation,
                diagnosis_bytes=diagnosis_bytes,
                human_decision_required=(diagnosis.get("human_decision_required") is True),
            )
        if (
            diagnosis.get("operation") != operation
            or diagnosis.get("policy_operation") != operation
            or diagnosis.get("repository") != self._repository
            or diagnosis.get("task") != self._task
            or diagnosis.get("branch_ref") != self._branch_ref
            or diagnosis.get("subject") != subject
            or type(diagnosis.get("request_id")) is not str
            or not _HEX_64.fullmatch(diagnosis["request_id"])
            or diagnosis["request_id"] != hashlib.sha256(_REQUEST_DOMAIN + request_bytes).hexdigest()
            or type(diagnosis.get("policy_id")) is not str
            or not _HEX_64.fullmatch(diagnosis["policy_id"])
            or type(diagnosis.get("view_id")) is not str
            or not _HEX_64.fullmatch(diagnosis["view_id"])
        ):
            raise TaskCollaborationError(
                "operation_diagnosis_binding_mismatch",
                operation=operation,
                diagnosis_bytes=diagnosis_bytes,
                human_decision_required=(diagnosis.get("human_decision_required") is True),
            )
        capture = _Capture(
            local, remote, view, view_bytes, principal, pull, repository_facts,
            request, request_bytes, diagnosis,
        )
        # Prerequisite callbacks are host-owned but still callbacks: detect any
        # concurrent Task/GitHub/authority movement before accepting their
        # positive diagnosis as the basis for a later execution decision.
        self._guard(capture, pr_number=pr_number)
        return capture

    def _guard(
        self,
        capture: _Capture,
        *,
        pr_number: int | None = None,
        expected_remote: object = _UNCHANGED,
        expected_pull_metadata: tuple[str, str] | None = None,
        expected_checkpoint: tuple[int, str, str, str] | None = None,
    ) -> None:
        repository = self._read_repository()
        local = self._read_local()
        remote = self._read_remote_head()
        principal = self._read_principal()
        view, view_bytes = self._observe_view(local.head, pr_number)
        remote_matches = remote == (capture.remote if expected_remote is _UNCHANGED else expected_remote)
        # Metadata tip advances may add unrelated immutable objects. Bind the
        # Task Record/Contract and every product/GitHub fact, not that unrelated
        # metadata commit ID. A push may change only the exact remote-head fact.
        prior = json.loads(capture.view_bytes.decode("utf-8"))
        current = json.loads(view_bytes.decode("utf-8"))
        prior["authority"]["metadata_commit"] = None
        current["authority"]["metadata_commit"] = None
        if expected_remote is not _UNCHANGED:
            prior["git"]["remote_head"] = None
            current["git"]["remote_head"] = None
        if expected_checkpoint is not None:
            comment_id, checkpoint_subject, metadata_commit, snapshot_id = expected_checkpoint
            expected_previous = {
                "state": "observed",
                "checkpoint_id": str(comment_id),
                "subject": checkpoint_subject,
                "metadata_commit": metadata_commit,
                "snapshot_id": snapshot_id,
                "commits_after": {"state": "observed", "count": 0},
            }
            if current["previous_checkpoint"] != expected_previous:
                raise TaskCollaborationError("checkpoint_view_changed")
            prior["previous_checkpoint"] = None
            current["previous_checkpoint"] = None
        view_matches = _canonical(prior) == _canonical(current)
        if (
            repository != capture.repository_facts
            or local != capture.local
            or not remote_matches
            or principal != capture.principal
            or not view_matches
        ):
            raise TaskCollaborationError("task_facts_changed")
        if pr_number is not None:
            current_pull = self._exact_pull(pr_number, local.head, operation=capture.request.operation)
            if expected_pull_metadata is None and current_pull != capture.pull:
                raise TaskCollaborationError("pull_request_changed")
            if expected_pull_metadata is not None and (
                capture.pull is None
                or current_pull.number != capture.pull.number
                or current_pull.base_repository != capture.pull.base_repository
                or current_pull.head_repository != capture.pull.head_repository
                or current_pull.base_ref != capture.pull.base_ref
                or current_pull.head_ref != capture.pull.head_ref
                or current_pull.head_oid != capture.pull.head_oid
                or current_pull.state != capture.pull.state
                or current_pull.draft != capture.pull.draft
                or (current_pull.title, current_pull.body) != expected_pull_metadata
            ):
                raise TaskCollaborationError("pull_request_changed")

    def _intent(
        self,
        capture: _Capture,
        operation: str,
        parameters: dict[str, Any],
    ) -> WriteIntent:
        authority = capture.view["authority"]
        local_facts = {
            "repository": capture.local.repository,
            "task": capture.local.task,
            "branch_ref": capture.local.branch_ref,
            "head": capture.local.head,
            "tree": capture.local.tree,
            "worktree": capture.local.worktree,
            "clean": capture.local.clean,
            "index_fingerprint": capture.local.index_fingerprint,
            "status_fingerprint": capture.local.status_fingerprint,
        }
        intent_parameters = {
            "operation_parameters": parameters,
            "expected_local_facts": local_facts,
            "expected_remote": capture.remote,
        }
        parameter_bytes = _canonical(intent_parameters, "invalid_intent_parameters")
        fields = {
            "operation": operation,
            "repository": self._repository,
            "task": self._task,
            "branch_ref": self._branch_ref,
            "subject": capture.local.head,
            "record_id": authority["record_id"],
            "contract_id": authority["contract_id"],
            "principal": capture.principal,
            "parameters": base64.b64encode(parameter_bytes).decode("ascii"),
            "request_id": capture.diagnosis["request_id"],
            "policy_id": capture.diagnosis["policy_id"],
            "view_id": capture.diagnosis["view_id"],
        }
        intent_id = hashlib.sha256(_INTENT_DOMAIN + _canonical(fields)).hexdigest()
        return WriteIntent(
            operation, self._repository, self._task, self._branch_ref, capture.local.head,
            authority["record_id"], authority["contract_id"], capture.principal,
            parameter_bytes, capture.diagnosis["request_id"], capture.diagnosis["policy_id"],
            capture.diagnosis["view_id"], intent_id,
        )

    def _authorize_and_record(
        self,
        capture: _Capture,
        intent: WriteIntent,
        *,
        content: bytes | None = None,
        pr_number: int | None = None,
    ) -> None:
        execution = ExecutionAuthorizationRequest(
            intent.operation, intent.repository, intent.task, intent.branch_ref, intent.subject,
            intent.principal, intent.request_id, intent.policy_id, intent.view_id,
            capture.request_bytes, intent.intent_id, intent.parameters,
        )
        try:
            authorized = self._authorize(execution) is True
        except Exception:
            raise TaskCollaborationError("execution_authorization_failed") from None
        if not authorized:
            raise TaskCollaborationError("execution_authorization_denied")
        self._guard(capture, pr_number=pr_number)
        if content is not None:
            request = ContentPolicyRequest(
                intent.operation, intent.repository, intent.task, intent.branch_ref,
                intent.subject, intent.principal, intent.request_id, intent.policy_id,
                intent.view_id, content, intent.intent_id,
            )
            try:
                accepted = self._content_policy(request) is True
            except Exception:
                raise TaskCollaborationError("content_policy_failed") from None
            if not accepted:
                raise TaskCollaborationError("content_policy_denied")
            self._guard(capture, pr_number=pr_number)
        try:
            acknowledged = self._record_intent(intent) is True
        except Exception:
            raise TaskCollaborationError("intent_record_failed") from None
        if not acknowledged:
            raise TaskCollaborationError("intent_record_unacknowledged")
        self._guard(capture, pr_number=pr_number)

    def _contract(self, capture: _Capture) -> tuple[str, str]:
        authority = capture.view["authority"]
        try:
            resolved = task_record.TaskRecords(
                self._store, authorize=lambda _request: False,
            ).read(
                authority["metadata_commit"], task=self._task,
                base_revision=self._base_revision, branch_ref=self._branch_ref,
            )
        except Exception:
            raise TaskCollaborationError("task_contract_unavailable") from None
        if resolved is None or resolved[0] != authority["record_id"]:
            raise TaskCollaborationError("task_authority_changed")
        contract = resolved[2]["payload"]
        title = _require_text(contract.get("title"), MAX_TITLE_BYTES, "invalid_task_contract", allow_empty=False)
        body = _require_text(contract.get("body"), MAX_PR_BODY_BYTES, "invalid_task_contract", allow_multiline=True)
        return title, body

    def _validate_contract_current(self, capture: _Capture) -> None:
        try:
            tip = self._store.fetch_tip()
            if tip is None:
                raise TaskCollaborationError("task_authority_unavailable")
            current = task_record.TaskRecords(
                self._store, authorize=lambda _request: False,
            ).read(tip, task=self._task, base_revision=self._base_revision, branch_ref=self._branch_ref)
        except TaskCollaborationError:
            raise
        except Exception:
            raise TaskCollaborationError("task_authority_unavailable") from None
        authority = capture.view["authority"]
        if current is None or current[0] != authority["record_id"] or current[1]["payload"]["contract_id"] != authority["contract_id"]:
            raise TaskCollaborationError("task_authority_changed")

    def bootstrap(self, *, resume_candidate: str | None = None) -> str:
        """Idempotently establish an absent task branch from its exact base only."""
        local = self._read_local()
        if resume_candidate is not None:
            resume_candidate = _require_oid(resume_candidate, "invalid_bootstrap_candidate")
        if local.head != self._base_revision:
            if resume_candidate is not None and resume_candidate != local.head:
                raise TaskCollaborationError("bootstrap_candidate_head_mismatch")
            try:
                related = self._git.is_ancestor(self._base_revision, local.head)
            except Exception:
                raise TaskCollaborationError("task_history_unavailable", operation="collaboration.bootstrap") from None
            if related is not True:
                raise TaskCollaborationError("task_history_not_ancestor", operation="collaboration.bootstrap")
            operation = "collaboration.bootstrap"
            capture = self._capture(operation, {
                "existing_head": local.head, "target_ref": self._branch_ref,
                "resume_candidate": resume_candidate,
            })
            if capture.local != local:
                raise TaskCollaborationError("task_facts_changed")
            self._guard(capture)
            if resume_candidate is not None:
                try:
                    confirmed = self._git.bootstrap_if_needed(
                        expected_head=local.head, resume_candidate=resume_candidate,
                    )
                except Exception:
                    raise TaskCollaborationError("bootstrap_candidate_unconfirmed", operation=operation) from None
                if confirmed != local.head:
                    raise TaskCollaborationError("bootstrap_candidate_unconfirmed", operation=operation)
            observed = self._read_local()
            if observed != local:
                raise TaskCollaborationError("task_facts_changed")
            self._observe_view(observed.head, None)
            self._guard(capture)
            # An existing branch is not rebased, reset, or repaired here.
            return observed.head
        operation = "collaboration.bootstrap"
        initial = self._capture(operation, {
            "base_revision": self._base_revision,
            "resume_candidate": resume_candidate,
            "target_ref": self._branch_ref,
        })
        if initial.local.head != self._base_revision:
            raise TaskCollaborationError("task_facts_changed")
        authorized_candidates: list[str] = []
        intent_failure: str | None = None

        def on_candidate(candidate: str) -> bool:
            nonlocal intent_failure
            candidate_oid = _require_oid(candidate, "invalid_bootstrap_candidate")
            if resume_candidate is not None and candidate_oid != resume_candidate:
                raise TaskCollaborationError("bootstrap_candidate_mismatch")
            parameters = {"base_revision": self._base_revision, "candidate": candidate_oid, "target_ref": self._branch_ref}
            capture = self._capture(operation, parameters)
            intent = self._intent(capture, operation, parameters)
            try:
                self._authorize_and_record(capture, intent)
            except TaskCollaborationError as error:
                intent_failure = error.code
                raise
            authorized_candidates.append(candidate_oid)
            return True

        mutation_error = False
        bootstrap_failure: collaboration_git.DistinctTaskGitError | None = None
        try:
            self._git.bootstrap_if_needed(
                expected_head=self._base_revision,
                on_candidate=on_candidate,
                resume_candidate=resume_candidate,
            )
        except collaboration_git.DistinctTaskGitError as error:
            mutation_error = True
            bootstrap_failure = error
        except Exception:
            mutation_error = True
        # The worker's return value and exception are not publication receipts.
        # Reconcile only the exact candidate the host authorized before its ref
        # update; do not adopt a different concurrent local branch value.
        try:
            observed = self._read_local()
        except Exception:
            raise TaskCollaborationError("bootstrap_outcome_uncertain") from None
        if intent_failure is not None:
            raise TaskCollaborationError(intent_failure, operation=operation)
        if bootstrap_failure is not None and bootstrap_failure.code != "git_operation_failed":
            # A typed safety/lock/cleanup failure is not a lost acknowledgement.
            # Retain any already-applied candidate, but never continue startup.
            raise TaskCollaborationError(bootstrap_failure.code, operation=operation) from None
        expected_candidate = authorized_candidates[-1] if authorized_candidates else None
        if (
            expected_candidate is None
            or observed.branch_ref != self._branch_ref
            or observed.head != expected_candidate
        ):
            raise TaskCollaborationError(
                "bootstrap_outcome_uncertain" if mutation_error else "bootstrap_postcondition_mismatch",
            )
        if (
            observed.repository != initial.local.repository
            or observed.task != initial.local.task
            or observed.branch_ref != initial.local.branch_ref
            or observed.tree != initial.local.tree
            or observed.worktree != initial.local.worktree
            or observed.clean != initial.local.clean
            or observed.index_fingerprint != initial.local.index_fingerprint
            or observed.status_fingerprint != initial.local.status_fingerprint
        ):
            raise TaskCollaborationError("bootstrap_applied_local_state_changed", operation=operation)
        try:
            ancestry_valid = self._git.is_ancestor(self._base_revision, expected_candidate)
        except Exception:
            raise TaskCollaborationError("task_history_unavailable", operation=operation) from None
        if ancestry_valid is not True:
            raise TaskCollaborationError("task_history_not_ancestor", operation=operation)
        # Verify that the new branch subject still resolves against the same
        # pinned Task Record and Contract; a Git callback result is not enough.
        confirmed_view, _view_bytes = self._observe_view(expected_candidate, None)
        if any(
            confirmed_view["authority"][field] != initial.view["authority"][field]
            for field in ("record_id", "contract_id", "base_revision", "branch_ref", "disposition")
        ):
            raise TaskCollaborationError("task_authority_changed")
        if self._read_principal() != initial.principal:
            raise TaskCollaborationError("github_principal_changed", operation=operation)
        if self._read_remote_head() != initial.remote:
            raise TaskCollaborationError("remote_head_changed", operation=operation)
        current_local = self._read_local()
        if (
            current_local.head != expected_candidate
            or current_local.repository != initial.local.repository
            or current_local.task != initial.local.task
            or current_local.branch_ref != initial.local.branch_ref
            or current_local.tree != initial.local.tree
            or current_local.worktree != initial.local.worktree
            or current_local.clean != initial.local.clean
            or current_local.index_fingerprint != initial.local.index_fingerprint
            or current_local.status_fingerprint != initial.local.status_fingerprint
        ):
            raise TaskCollaborationError("bootstrap_applied_local_state_changed", operation=operation)
        if initial.repository_facts != self._read_repository():
            raise TaskCollaborationError("task_facts_changed")
        final_view, _final_view_bytes = self._observe_view(expected_candidate, None)
        if any(
            final_view["authority"][field] != initial.view["authority"][field]
            for field in ("record_id", "contract_id", "base_revision", "branch_ref", "disposition")
        ):
            raise TaskCollaborationError("task_authority_changed")
        if (
            self._read_remote_head() != initial.remote
            or self._read_principal() != initial.principal
            or self._read_repository() != initial.repository_facts
        ):
            raise TaskCollaborationError("task_facts_changed")
        if self._read_local() != replace(initial.local, head=expected_candidate):
            raise TaskCollaborationError("bootstrap_applied_local_state_changed", operation=operation)
        return expected_candidate

    def push(self) -> str:
        """Push only the observed subject to the exact bound branch, never force."""
        local = self._read_local()
        remote = self._read_remote_head()
        parameters = {
            "expected_head": local.head,
            "expected_remote": remote,
            "target_ref": self._branch_ref,
            "force": False,
        }
        operation = "collaboration.push"
        capture = self._capture(operation, parameters)
        if capture.local.head != local.head or capture.remote != remote:
            raise TaskCollaborationError("task_facts_changed")
        intent = self._intent(capture, operation, parameters)
        try:
            related = self._git.is_ancestor(self._base_revision, local.head)
        except Exception:
            raise TaskCollaborationError("task_history_unavailable", operation=operation) from None
        if related is not True:
            raise TaskCollaborationError("task_history_not_ancestor", operation=operation)
        if remote == local.head:
            self._guard(capture, expected_remote=local.head)
            return local.head

        intent_acknowledged = False
        intent_failure: str | None = None
        def on_intent(branch_push: collaboration_git.BranchPush) -> bool:
            nonlocal intent_acknowledged, intent_failure
            if (
                type(branch_push) is not collaboration_git.BranchPush
                or branch_push.repository != self._repository
                or branch_push.task != self._task
                or branch_push.branch_ref != self._branch_ref
                or branch_push.subject != local.head
                or branch_push.expected_remote != remote
            ):
                raise TaskCollaborationError("push_intent_binding_mismatch")
            try:
                self._authorize_and_record(capture, intent)
            except TaskCollaborationError as error:
                intent_failure = error.code
                raise
            intent_acknowledged = True
            return True

        mutation_error = False
        try:
            self._git.push_exact(
                expected_head=local.head,
                expected_remote=remote,
                on_intent=on_intent,
            )
        except Exception:
            mutation_error = True
        # Ignore any transport acknowledgement and re-read the one exact ref.
        try:
            observed_remote = self._read_remote_head()
        except Exception:
            raise TaskCollaborationError("push_outcome_uncertain") from None
        if observed_remote != local.head:
            raise TaskCollaborationError(
                intent_failure or ("push_outcome_uncertain" if mutation_error else "push_postcondition_mismatch"),
            )
        if not intent_acknowledged:
            raise TaskCollaborationError(intent_failure or "push_intent_unconfirmed", operation=operation)
        self._guard(capture, expected_remote=local.head)
        return local.head

    def ensure_draft_pr(self) -> CollaborationReceipt:
        """Adopt one exact current Draft PR or create one and reobserve it."""
        local = self._read_local()
        operation = "collaboration.draft_pr"
        capture = self._capture(operation, {"base_ref": self._default_branch_ref, "head_ref": self._branch_ref, "head_oid": local.head})
        if capture.local.head != local.head or capture.remote != local.head:
            raise TaskCollaborationError("task_branch_not_published")
        pulls = self._stable_pulls()
        matches = self._task_pulls(pulls, local.head, operation=operation)
        if matches:
            existing = self._confirm_pull_adoption(capture, matches[0].number)
            return self._receipt(local.head, existing.number)

        title, body = self._contract(capture)

        params = {
            "base_ref": self._default_branch_ref,
            "body": body,
            "draft": True,
            "head_oid": local.head,
            "head_ref": self._branch_ref,
            "title": title,
        }
        capture = self._capture(operation, params)
        if capture.local.head != local.head or capture.remote != local.head:
            raise TaskCollaborationError("task_facts_changed")
        intent = self._intent(capture, operation, params)
        content = _canonical({"title": title, "body": body})
        self._authorize_and_record(capture, intent, content=content)
        self._validate_contract_current(capture)
        # Immediately before create, scan every state/page again. A closed,
        # merged, forked, cross-base, or duplicate historical PR is a conflict.
        before = self._stable_pulls()
        if self._task_pulls(before, local.head, operation=operation):
            found = self._task_pulls(before, local.head, operation=operation)[0]
            exact = self._confirm_pull_adoption(capture, found.number)
            return self._receipt(local.head, exact.number)
        self._guard(capture)
        create_intent = CreateDraftIntent(
            self._repository, self._task, self._branch_ref, self._default_branch_ref,
            local.head, title, body, intent,
        )
        mutation_error = False
        try:
            self._github.create_draft(create_intent)
        except Exception:
            mutation_error = True
        # Never trust the raw API acknowledgement; exact postconditions decide.
        try:
            after = self._stable_pulls()
            matches = self._task_pulls(after, local.head, operation=operation)
            self._guard(capture)
        except Exception:
            raise TaskCollaborationError("draft_pr_outcome_uncertain") from None
        if len(matches) != 1 or matches[0].title != title or matches[0].body != body:
            raise TaskCollaborationError("draft_pr_outcome_uncertain" if mutation_error else "draft_pr_postcondition_mismatch")
        return self._receipt(local.head, matches[0].number)

    def draft_pr(self) -> CollaborationReceipt:
        """Public operation spelling for exact Draft PR adoption/creation."""
        return self.ensure_draft_pr()

    def _receipt(
        self,
        subject: str,
        number: int | None,
        *,
        binding: MetadataBinding | None = None,
        comment_id: int | None = None,
    ) -> CollaborationReceipt:
        if binding is None:
            return CollaborationReceipt(self._repository, self._task, self._branch_ref, subject, number, comment_id)
        return CollaborationReceipt(
            self._repository, self._task, self._branch_ref, subject, number, comment_id,
            binding.metadata_commit, binding.record_id, binding.contract_id, binding.snapshot_id,
        )

    def update_metadata(
        self,
        pr_number: int,
        expected_subject: str,
        expected_title: str,
        expected_body: str,
        new_title: str,
        new_body: str,
    ) -> CollaborationReceipt:
        """Guarded two-field PR edit; identity fields are not writable here."""
        subject = _require_oid(expected_subject)
        expected_title = _require_text(expected_title, MAX_TITLE_BYTES, "invalid_title", allow_empty=False)
        expected_body = _require_text(expected_body, MAX_PR_BODY_BYTES, "invalid_body", allow_multiline=True)
        new_title = _require_text(new_title, MAX_TITLE_BYTES, "invalid_title", allow_empty=False)
        new_body = _require_text(new_body, MAX_PR_BODY_BYTES, "invalid_body", allow_multiline=True)
        params = {
            "expected_body": expected_body,
            "expected_title": expected_title,
            "new_body": new_body,
            "new_title": new_title,
            "number": pr_number,
            "subject": subject,
        }
        operation = "collaboration.metadata_update"
        capture = self._capture(operation, params, pr_number=pr_number)
        if capture.local.head != subject or capture.remote != subject:
            raise TaskCollaborationError("stale_pull_request_subject")
        pull = capture.pull
        assert pull is not None
        if (pull.title, pull.body) == (new_title, new_body):
            self._confirm_pull_metadata(capture, pr_number, subject, new_title, new_body)
            return self._receipt(subject, pr_number)
        if (pull.title, pull.body) != (expected_title, expected_body):
            raise TaskCollaborationError("pull_request_metadata_conflict", human_decision_required=True)
        intent = self._intent(capture, operation, params)
        content = _canonical({"title": new_title, "body": new_body})
        self._authorize_and_record(capture, intent, content=content, pr_number=pr_number)
        before = self._exact_pull(pr_number, subject, operation=operation)
        if (before.title, before.body) != (expected_title, expected_body):
            if (before.title, before.body) == (new_title, new_body):
                self._confirm_pull_metadata(capture, pr_number, subject, new_title, new_body)
                return self._receipt(subject, pr_number)
            raise TaskCollaborationError("pull_request_metadata_conflict", human_decision_required=True)
        self._guard(capture, pr_number=pr_number)
        mutation = EditMetadataIntent(
            self._repository, self._task, pr_number, self._branch_ref, subject,
            expected_title, expected_body, new_title, new_body, intent,
        )
        mutation_error = False
        try:
            self._github.edit_metadata(mutation)
        except Exception:
            mutation_error = True
        try:
            after = self._exact_pull(pr_number, subject, operation=operation)
            self._guard(
                capture, pr_number=pr_number,
                expected_pull_metadata=(new_title, new_body),
            )
        except Exception:
            raise TaskCollaborationError("metadata_update_outcome_uncertain") from None
        if (after.title, after.body) != (new_title, new_body):
            raise TaskCollaborationError("metadata_update_outcome_uncertain" if mutation_error else "metadata_update_postcondition_mismatch")
        return self._receipt(subject, pr_number)

    def metadata_update(
        self,
        pr_number: int,
        expected_subject: str,
        expected_title: str,
        expected_body: str,
        new_title: str,
        new_body: str,
    ) -> CollaborationReceipt:
        """Public operation spelling for bounded guarded PR metadata edits."""
        return self.update_metadata(
            pr_number, expected_subject, expected_title, expected_body, new_title, new_body,
        )

    def _binding(self, value: object, subject: str) -> MetadataBinding:
        if type(value) is not MetadataBinding:
            raise TaskCollaborationError("metadata_binding_required")
        binding = MetadataBinding(
            _require_oid(value.metadata_commit, "invalid_metadata_commit"),
            _require_metadata_id(value.record_id),
            _require_metadata_id(value.contract_id),
            _require_metadata_id(value.snapshot_id),
        )
        if len(binding.metadata_commit) != len(subject):
            raise TaskCollaborationError("metadata_binding_mismatch")
        return binding

    def _read_historical_checkpoint(
        self,
        comment: CommentFacts,
        payload: dict[str, Any],
    ) -> task_view.PreviousCheckpoint:
        """Confirm only the selected historical capsule, without a live-view recursion."""
        binding = MetadataBinding(
            payload["metadata_commit"], payload["record_id"],
            payload["contract_id"], payload["snapshot_id"],
        )
        subject = payload["subject"]
        expected_boundary = "explicit-handoff" if payload["boundary"] == "initial" else payload["boundary"]
        required = (
            ("task-record", binding.record_id, self._task, self._base_revision),
            ("contract", binding.contract_id, self._task, self._base_revision),
            (task_view.SNAPSHOT_KIND, binding.snapshot_id, self._task, subject),
        )
        try:
            with self._store.validation_scope():
                if self._store.confirm(binding.metadata_commit, required_objects=required) != binding.metadata_commit:
                    raise ValueError
                resolved = task_record.TaskRecords(
                    self._store, authorize=lambda _request: False,
                ).read(
                    binding.metadata_commit, task=self._task,
                    base_revision=self._base_revision, branch_ref=self._branch_ref,
                )
                if (
                    resolved is None
                    or resolved[0] != binding.record_id
                    or resolved[1]["payload"]["contract_id"] != binding.contract_id
                ):
                    raise ValueError
                snapshot = task_view.TaskViewSnapshots(
                    self._store, authorize=lambda _request: False,
                ).read(
                    binding.metadata_commit, binding.snapshot_id,
                    task=self._task, subject=subject,
                )
        except Exception:
            raise TaskCollaborationError("checkpoint_history_unavailable") from None
        try:
            view = snapshot["view"]
            authority = view["authority"]
            git = view["git"]
            github = view["github"]
            pull = github["pull_request"]
            if (
                snapshot["boundary"] != expected_boundary
                or view["repository"] != self._repository
                or view["task"] != self._task
                or view["branch_ref"] != self._branch_ref
                or view["subject"] != subject
                or authority["state"] != "observed"
                or authority["record_id"] != binding.record_id
                or authority["contract_id"] != binding.contract_id
                or authority["base_revision"] != self._base_revision
                or authority["branch_ref"] != self._branch_ref
                or git["head"] != subject
                or git["branch_ref"] != self._branch_ref
                or git["branch_matches_task"] is not True
                or git["remote_head"].get("state") != "observed"
                or git["remote_head"].get("head_oid") != subject
                or github.get("state") != "observed"
                or pull["number"] != comment.number
                or pull["state"] != "open"
                or pull["draft"] is not True
                or pull["base_repository"] != self._repository
                or pull["head_repository"] != self._repository
                or pull["base_ref"] != self._default_branch_ref[len("refs/heads/"):]
                or pull["head_ref"] != self._branch_ref[len("refs/heads/") :]
                or pull["head_oid"] != subject
                or (
                    payload["boundary"] == "initial"
                    and view["previous_checkpoint"] != {"state": "absent"}
                )
            ):
                raise ValueError
        except Exception:
            raise TaskCollaborationError("checkpoint_history_binding_mismatch") from None
        return task_view.PreviousCheckpoint(
            str(comment.comment_id), subject, binding.metadata_commit, binding.snapshot_id,
        )

    def github_observation(
        self,
        request: task_view.GitHubPullRequestRequest,
    ) -> task_view.GitHubObservation:
        """Return authenticated current PR facts and this principal's latest checkpoint.

        The reader parses all bounded comment pages but validates only the one
        canonical latest checkpoint's exact #217/#191/#144 graph. It never calls
        ``observe_live_task`` and therefore cannot recursively re-enter itself.
        """
        if (
            type(request) is not task_view.GitHubPullRequestRequest
            or request.repository != self._repository
            or request.task != self._task
            or request.branch_ref != self._branch_ref
            or type(request.number) is not int
            or request.number <= 0
        ):
            raise TaskCollaborationError("github_reader_binding_mismatch")
        repository_before = self._read_repository()
        principal_before = self._read_principal()
        local_before = self._read_local()
        remote_before = self._read_remote_head()
        pull_before = self._exact_pull(request.number, local_before.head)
        comments_before = self._stable_comments("pr", request.number)
        canonical_before = self._owned_checkpoints(
            comments_before, principal=principal_before, pr_number=request.number,
        )
        selected_before = canonical_before[-1] if canonical_before else None
        previous = (
            None if selected_before is None
            else self._read_historical_checkpoint(*selected_before)
        )
        comments_after = self._stable_comments("pr", request.number)
        canonical_after = self._owned_checkpoints(
            comments_after, principal=principal_before, pr_number=request.number,
        )
        if tuple((item[0].comment_id, _canonical(item[1])) for item in canonical_after) != tuple(
            (item[0].comment_id, _canonical(item[1])) for item in canonical_before
        ):
            raise TaskCollaborationError("checkpoint_history_changed")
        repository_after = self._read_repository()
        principal_after = self._read_principal()
        local_after = self._read_local()
        remote_after = self._read_remote_head()
        pull_after = self._exact_pull(request.number, local_after.head)
        if (
            repository_after != repository_before
            or principal_after != principal_before
            or local_after != local_before
            or remote_after != remote_before
            or pull_after != pull_before
        ):
            raise TaskCollaborationError("github_observation_changed")
        return task_view.GitHubObservation(task_view.GitHubPullRequestFacts(
            pull_before.base_repository, pull_before.head_repository, pull_before.number,
            pull_before.state, pull_before.draft, pull_before.base_ref,
            pull_before.head_ref, pull_before.head_oid,
        ), previous)

    def _validate_binding(
        self,
        binding_value: object,
        *,
        subject: str,
        pr_number: int,
        capture: _Capture,
        expected_boundary: str | None = None,
        expected_previous: dict[str, Any] | None = None,
    ) -> MetadataBinding:
        binding = self._binding(binding_value, subject)
        authority = capture.view["authority"]
        if (binding.record_id, binding.contract_id) != (authority["record_id"], authority["contract_id"]):
            raise TaskCollaborationError("metadata_authority_mismatch")
        required = (
            ("task-record", binding.record_id, self._task, self._base_revision),
            ("contract", binding.contract_id, self._task, self._base_revision),
            (task_view.SNAPSHOT_KIND, binding.snapshot_id, self._task, subject),
        )
        try:
            confirmed = self._store.confirm(binding.metadata_commit, required_objects=required)
            if confirmed != binding.metadata_commit:
                raise ValueError
            resolved = task_record.TaskRecords(
                self._store, authorize=lambda _request: False,
            ).read(binding.metadata_commit, task=self._task, base_revision=self._base_revision, branch_ref=self._branch_ref)
            if resolved is None or resolved[0] != binding.record_id or resolved[1]["payload"]["contract_id"] != binding.contract_id:
                raise ValueError
            snapshot = task_view.TaskViewSnapshots(self._store, authorize=lambda _request: False).read(
                binding.metadata_commit, binding.snapshot_id, task=self._task, subject=subject,
            )
        except Exception:
            raise TaskCollaborationError("metadata_not_remotely_retrievable") from None
        try:
            view = snapshot["view"]
            authority_view = view["authority"]
            git = view["git"]
            github = view["github"]
            pull = github["pull_request"]
            if (
                snapshot["boundary"] not in task_view.SNAPSHOT_BOUNDARIES
                or (expected_boundary is not None and snapshot["boundary"] != expected_boundary)
                or view["repository"] != self._repository
                or view["task"] != self._task
                or view["subject"] != subject
                or view["branch_ref"] != self._branch_ref
                or (expected_previous is not None and view["previous_checkpoint"] != expected_previous)
                or authority_view["state"] != "observed"
                or authority_view["record_id"] != binding.record_id
                or authority_view["contract_id"] != binding.contract_id
                or authority_view["base_revision"] != self._base_revision
                or authority_view["branch_ref"] != self._branch_ref
                or git["head"] != subject
                or git["branch_ref"] != self._branch_ref
                or git["branch_matches_task"] is not True
                or git["remote_head"].get("state") != "observed"
                or git["remote_head"].get("head_oid") != subject
                or github.get("state") != "observed"
                or pull["number"] != pr_number
                or pull["state"] != "open"
                or pull["draft"] is not True
                or pull["base_repository"] != self._repository
                or pull["head_repository"] != self._repository
                or pull["base_ref"] != self._default_branch_ref[len("refs/heads/"):]
                or pull["head_ref"] != self._branch_ref[len("refs/heads/") :]
                or pull["head_oid"] != subject
            ):
                raise ValueError
        except Exception:
            raise TaskCollaborationError("snapshot_binding_mismatch") from None
        return binding

    def _checkpoint_payload(
        self,
        *,
        boundary: str,
        key: str,
        report: str,
        binding: MetadataBinding,
        pr_number: int,
        subject: str,
        principal: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "repository": self._repository,
            "task": self._task,
            "branch_ref": self._branch_ref,
            "pr_number": pr_number,
            "subject": subject,
            "metadata_commit": binding.metadata_commit,
            "record_id": binding.record_id,
            "contract_id": binding.contract_id,
            "snapshot_id": binding.snapshot_id,
            "boundary": boundary,
            "key": key,
            "principal": principal,
            "report": report,
        }

    def _require_initial_checkpoint(self, capture: _Capture, pr_number: int) -> None:
        """Require this principal's durable initial checkpoint before a new turn write."""
        self._guard(capture, pr_number=pr_number)
        initial = self._owned_checkpoint(
            self._stable_comments("pr", pr_number),
            principal=capture.principal,
            key="initial",
            pr_number=pr_number,
        )
        if initial is None or initial[1]["boundary"] != "initial":
            raise TaskCollaborationError(
                "initial_checkpoint_required",
                operation="collaboration.turn_checkpoint",
            )
        previous = self._read_historical_checkpoint(*initial)
        try:
            ancestor = self._git.is_ancestor(previous.subject, capture.local.head)
        except Exception:
            raise TaskCollaborationError(
                "initial_checkpoint_ancestry_unavailable",
                operation="collaboration.turn_checkpoint",
            ) from None
        if ancestor is not True:
            raise TaskCollaborationError(
                "initial_checkpoint_subject_not_ancestor",
                operation="collaboration.turn_checkpoint",
                human_decision_required=True,
            )
        self._guard(capture, pr_number=pr_number)

    @staticmethod
    def _render_checkpoint(payload: dict[str, Any]) -> tuple[str, str]:
        data = _canonical(payload, "invalid_checkpoint_payload")
        digest = hashlib.sha256(_CHECKPOINT_DOMAIN + data).hexdigest()
        marker = f"<!-- agentcore-task-checkpoint/v1 sha256={digest} -->"
        capsule = base64.b64encode(data).decode("ascii")
        body = f"{marker}\nTask checkpoint ({payload['boundary']}):\n{payload['report']}\nCapsule: {capsule}"
        if len(body.encode("utf-8")) > MAX_COMMENT_BODY_BYTES:
            raise TaskCollaborationError("checkpoint_body_too_large")
        return body, digest

    def _decode_checkpoint(self, body: str) -> tuple[dict[str, Any], str]:
        parts = body.split("\n")
        if len(parts) < 4 or not (match := _CHECKPOINT_MARKER.fullmatch(parts[0])):
            raise TaskCollaborationError("invalid_checkpoint_comment")
        if not parts[1].startswith("Task checkpoint (") or not parts[1].endswith("):") or not parts[-1].startswith("Capsule: "):
            raise TaskCollaborationError("invalid_checkpoint_comment")
        encoded = parts[-1][len("Capsule: "):]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise TaskCollaborationError("invalid_checkpoint_capsule") from None
        if base64.b64encode(data).decode("ascii") != encoded:
            raise TaskCollaborationError("invalid_checkpoint_capsule")
        payload = _decode_canonical(data)
        expected_fields = {
            "schema_version", "repository", "task", "branch_ref", "pr_number", "subject", "metadata_commit",
            "record_id", "contract_id", "snapshot_id", "boundary", "key", "principal", "report",
        }
        if type(payload) is not dict or payload.keys() != expected_fields:
            raise TaskCollaborationError("invalid_checkpoint_schema")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != SCHEMA_VERSION
            or type(payload["pr_number"]) is not int
            or payload["pr_number"] <= 0
            or type(payload["principal"]) is not int
            or payload["principal"] <= 0
            or type(payload["boundary"]) is not str
            or payload["boundary"] not in {"initial", "turn-end", "explicit-handoff"}
        ):
            raise TaskCollaborationError("invalid_checkpoint_schema")
        for field in ("repository", "task", "branch_ref", "subject", "metadata_commit", "record_id", "contract_id", "snapshot_id", "key", "report"):
            if type(payload[field]) is not str:
                raise TaskCollaborationError("invalid_checkpoint_schema")
        _valid_repo(payload["repository"])
        if (
            not re.fullmatch(r"[1-9][0-9]{0,127}", payload["task"], re.ASCII)
            or _require_branch(payload["branch_ref"]) != self._branch_ref
        ):
            raise TaskCollaborationError("invalid_checkpoint_schema")
        _require_oid(payload["subject"])
        _require_oid(payload["metadata_commit"])
        for field in ("record_id", "contract_id", "snapshot_id"):
            _require_metadata_id(payload[field])
        _require_text(payload["key"], MAX_KEY_LENGTH, "invalid_checkpoint_schema", allow_empty=False)
        _require_text(payload["report"], MAX_REPORT_BYTES, "invalid_checkpoint_schema", allow_empty=False, allow_multiline=True)
        if (payload["key"] == "initial") != (payload["boundary"] == "initial"):
            raise TaskCollaborationError("invalid_checkpoint_schema")
        if (
            parts[1] != f"Task checkpoint ({payload['boundary']}):"
            or "\n".join(parts[2:-1]) != payload["report"]
        ):
            raise TaskCollaborationError("invalid_checkpoint_comment")
        expected, _ = self._render_checkpoint(payload)
        if expected != body or match.group(1) != hashlib.sha256(_CHECKPOINT_DOMAIN + data).hexdigest():
            raise TaskCollaborationError("invalid_checkpoint_digest")
        return payload, match.group(1)

    def _owned_checkpoints(
        self,
        comments: Sequence[CommentFacts],
        *,
        principal: int,
        pr_number: int,
    ) -> tuple[tuple[CommentFacts, dict[str, Any]], ...]:
        by_key: dict[str, list[tuple[CommentFacts, dict[str, Any]]]] = {}
        for comment in comments:
            if comment.author_id != principal or "agentcore-task-checkpoint/v1" not in comment.body:
                continue
            payload, _digest = self._decode_checkpoint(comment.body)
            if payload["principal"] != comment.author_id:
                raise TaskCollaborationError("checkpoint_principal_mismatch")
            if (
                payload["repository"] != self._repository
                or payload["task"] != self._task
                or payload["branch_ref"] != self._branch_ref
                or payload["pr_number"] != pr_number
                or comment.number != pr_number
                or comment.kind != "pr"
            ):
                raise TaskCollaborationError("checkpoint_binding_mismatch")
            by_key.setdefault(payload["key"], []).append((comment, payload))
        canonical: list[tuple[CommentFacts, dict[str, Any]]] = []
        for values in by_key.values():
            first_bytes = _canonical(values[0][1])
            if any(_canonical(payload) != first_bytes for _comment, payload in values[1:]):
                raise TaskCollaborationError("checkpoint_key_conflict")
            canonical.append(min(values, key=lambda item: item[0].comment_id))
        return tuple(sorted(canonical, key=lambda item: item[0].comment_id))

    def _owned_checkpoint(
        self,
        comments: Sequence[CommentFacts],
        *,
        principal: int,
        key: str,
        pr_number: int,
    ) -> tuple[CommentFacts, dict[str, Any]] | None:
        return next((
            item for item in self._owned_checkpoints(
                comments, principal=principal, pr_number=pr_number,
            ) if item[1]["key"] == key
        ), None)

    def _confirm_checkpoint_receipt(
        self,
        capture: _Capture,
        binding: MetadataBinding,
        *,
        pr_number: int,
        subject: str,
        boundary: str,
        key: str,
        expected_payload: dict[str, Any],
        expect_current_checkpoint: bool = False,
    ) -> CollaborationReceipt:
        expected_snapshot_boundary = "explicit-handoff" if boundary == "initial" else boundary
        selected = self._owned_checkpoint(
            self._stable_comments("pr", pr_number),
            principal=capture.principal,
            key=key,
            pr_number=pr_number,
        )
        if selected is None or _canonical(selected[1]) != _canonical(expected_payload):
            raise TaskCollaborationError("checkpoint_receipt_unconfirmed")
        selected_checkpoint = (
            selected[0].comment_id,
            selected[1]["subject"],
            selected[1]["metadata_commit"],
            selected[1]["snapshot_id"],
        )
        self._guard(
            capture, pr_number=pr_number,
            expected_checkpoint=selected_checkpoint if expect_current_checkpoint else None,
        )
        expected_previous = capture.view["previous_checkpoint"] if expect_current_checkpoint else None
        self._validate_binding(
            binding, subject=subject, pr_number=pr_number, capture=capture,
            expected_boundary=expected_snapshot_boundary,
            expected_previous=expected_previous,
        )
        final = self._owned_checkpoint(
            self._stable_comments("pr", pr_number),
            principal=capture.principal,
            key=key,
            pr_number=pr_number,
        )
        if final is None or _canonical(final[1]) != _canonical(expected_payload):
            raise TaskCollaborationError("checkpoint_receipt_unconfirmed")
        if final[0].comment_id != selected[0].comment_id:
            raise TaskCollaborationError("checkpoint_receipt_changed")
        final_checkpoint = (
            final[0].comment_id,
            final[1]["subject"],
            final[1]["metadata_commit"],
            final[1]["snapshot_id"],
        )
        self._guard(
            capture, pr_number=pr_number,
            expected_checkpoint=final_checkpoint if expect_current_checkpoint else None,
        )
        return self._receipt(subject, pr_number, binding=binding, comment_id=final[0].comment_id)

    def _post_checkpoint(
        self,
        *,
        boundary: str,
        key: str,
        report: str,
        binding: MetadataBinding,
        pr_number: int,
        capture: _Capture,
    ) -> CollaborationReceipt:
        subject = capture.local.head
        payload = self._checkpoint_payload(
            boundary=boundary, key=key, report=report, binding=binding,
            pr_number=pr_number, subject=subject, principal=capture.principal,
        )
        body, _digest = self._render_checkpoint(payload)
        params = {
            "boundary": boundary, "key": key, "metadata_commit": binding.metadata_commit,
            "pr_number": pr_number, "report_digest": hashlib.sha256(report.encode("utf-8")).hexdigest(),
            "snapshot_id": binding.snapshot_id, "subject": subject,
        }
        intent = self._intent(capture, capture.request.operation, params)
        expected_snapshot_boundary = "explicit-handoff" if boundary == "initial" else boundary
        self._validate_binding(
            binding, subject=subject, pr_number=pr_number, capture=capture,
            expected_boundary=expected_snapshot_boundary,
        )
        existing_comments = self._stable_comments("pr", pr_number)
        existing = self._owned_checkpoint(
            existing_comments, principal=capture.principal, key=key, pr_number=pr_number,
        )
        if existing is not None:
            _comment, old = existing
            if _canonical(old) != _canonical(payload):
                raise TaskCollaborationError("checkpoint_key_conflict")
            return self._confirm_checkpoint_receipt(
                capture, binding, pr_number=pr_number, subject=subject, boundary=boundary,
                key=key, expected_payload=payload,
            )
        expected_previous = capture.view["previous_checkpoint"]
        if boundary == "initial":
            if expected_previous != {"state": "absent"}:
                raise TaskCollaborationError("initial_checkpoint_not_first")
        else:
            self._require_initial_checkpoint(capture, pr_number)
            if expected_previous.get("state") != "observed":
                raise TaskCollaborationError("checkpoint_predecessor_unavailable")
        self._validate_binding(
            binding, subject=subject, pr_number=pr_number, capture=capture,
            expected_boundary=expected_snapshot_boundary,
            expected_previous=expected_previous,
        )
        self._authorize_and_record(capture, intent, content=body.encode("utf-8"), pr_number=pr_number)
        # Reconfirm the full #217 graph immediately before posting. The owner
        # #191/#144 facades remain the only metadata writers/read authorities.
        self._validate_binding(
            binding, subject=subject, pr_number=pr_number, capture=capture,
            expected_boundary=expected_snapshot_boundary,
            expected_previous=expected_previous,
        )
        if boundary != "initial":
            self._require_initial_checkpoint(capture, pr_number)
        before = self._stable_comments("pr", pr_number)
        existing = self._owned_checkpoint(
            before, principal=capture.principal, key=key, pr_number=pr_number,
        )
        if existing is not None:
            _comment, old = existing
            if _canonical(old) != _canonical(payload):
                raise TaskCollaborationError("checkpoint_key_conflict")
            return self._confirm_checkpoint_receipt(
                capture, binding, pr_number=pr_number, subject=subject, boundary=boundary,
                key=key, expected_payload=payload,
            )
        self._guard(capture, pr_number=pr_number)
        comment_intent = PostCommentIntent(self._repository, "pr", pr_number, body, intent)
        try:
            self._github.post_comment(comment_intent)
        except Exception:
            pass
        try:
            return self._confirm_checkpoint_receipt(
                capture, binding, pr_number=pr_number, subject=subject, boundary=boundary,
                key=key, expected_payload=payload, expect_current_checkpoint=True,
            )
        except Exception:
            raise TaskCollaborationError("checkpoint_outcome_uncertain") from None

    def initial_checkpoint(
        self,
        pr_number: int,
        binding: MetadataBinding,
        report: str,
    ) -> CollaborationReceipt:
        report = _require_text(report, MAX_REPORT_BYTES, "invalid_report", allow_empty=False, allow_multiline=True)
        local = self._read_local()
        checked_input = self._binding(binding, local.head)
        operation = "collaboration.initial_checkpoint"
        params = {
            "boundary": "initial", "key": "initial", "pr_number": pr_number,
            "metadata_commit": checked_input.metadata_commit,
            "record_id": checked_input.record_id,
            "contract_id": checked_input.contract_id,
            "snapshot_id": checked_input.snapshot_id,
            "report": report,
            "report_digest": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        }
        capture = self._capture(operation, params, pr_number=pr_number)
        if capture.local != local:
            raise TaskCollaborationError("task_facts_changed")
        if capture.remote != capture.local.head:
            raise TaskCollaborationError("task_branch_not_published")
        checked = self._validate_binding(
            checked_input, subject=capture.local.head, pr_number=pr_number, capture=capture,
            expected_boundary="explicit-handoff",
        )
        return self._post_checkpoint(
            boundary="initial", key="initial", report=report, binding=checked,
            pr_number=pr_number, capture=capture,
        )

    def turn_checkpoint(
        self,
        pr_number: int,
        binding: MetadataBinding,
        checkpoint_key: str,
        report: str,
        *,
        boundary: str = "turn-end",
    ) -> CollaborationReceipt:
        key = _require_text(checkpoint_key, MAX_KEY_LENGTH, "invalid_checkpoint_key", allow_empty=False)
        if key == "initial":
            raise TaskCollaborationError("reserved_checkpoint_key")
        if boundary not in {"turn-end", "explicit-handoff"}:
            raise TaskCollaborationError("invalid_checkpoint_boundary")
        report = _require_text(report, MAX_REPORT_BYTES, "invalid_report", allow_empty=False, allow_multiline=True)
        local = self._read_local()
        checked_input = self._binding(binding, local.head)
        operation = "collaboration.turn_checkpoint"
        params = {
            "boundary": boundary, "key": key, "pr_number": pr_number,
            "metadata_commit": checked_input.metadata_commit,
            "record_id": checked_input.record_id,
            "contract_id": checked_input.contract_id,
            "snapshot_id": checked_input.snapshot_id,
            "report": report,
            "report_digest": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        }
        capture = self._capture(operation, params, pr_number=pr_number)
        if capture.local != local:
            raise TaskCollaborationError("task_facts_changed")
        if capture.remote != capture.local.head:
            raise TaskCollaborationError("task_branch_not_published")
        checked = self._validate_binding(
            checked_input, subject=capture.local.head, pr_number=pr_number, capture=capture,
            expected_boundary=boundary,
        )
        return self._post_checkpoint(
            boundary=boundary, key=key, report=report, binding=checked,
            pr_number=pr_number, capture=capture,
        )

    def _comment_payload(self, kind: str, number: int, key: str, text: str, capture: _Capture) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "repository": self._repository,
            "task": self._task,
            "branch_ref": self._branch_ref,
            "kind": kind,
            "number": number,
            "subject": capture.local.head,
            "record_id": capture.view["authority"]["record_id"],
            "contract_id": capture.view["authority"]["contract_id"],
            "principal": capture.principal,
            "key": key,
            "text": text,
        }

    def _render_factual_comment(self, payload: dict[str, Any]) -> str:
        data = _canonical(payload, "invalid_comment_payload")
        digest = hashlib.sha256(_COMMENT_DOMAIN + data).hexdigest()
        marker = f"<!-- agentcore-task-comment/v1 sha256={digest} -->"
        encoded = base64.b64encode(data).decode("ascii")
        body = f"{marker}\n{payload['text']}\nCapsule: {encoded}"
        if len(body.encode("utf-8")) > MAX_COMMENT_BODY_BYTES:
            raise TaskCollaborationError("comment_body_too_large")
        return body

    def _owned_factual_comment(
        self, comments: Sequence[CommentFacts], *, principal: int, key: str,
    ) -> tuple[CommentFacts, dict[str, Any]] | None:
        found: list[tuple[CommentFacts, dict[str, Any]]] = []
        for comment in comments:
            if comment.author_id != principal or "agentcore-task-comment/v1" not in comment.body:
                continue
            lines = comment.body.split("\n")
            if len(lines) < 3 or not (match := _COMMENT_MARKER.fullmatch(lines[0])) or not lines[-1].startswith("Capsule: "):
                raise TaskCollaborationError("invalid_factual_comment")
            encoded = lines[-1][len("Capsule: "):]
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise TaskCollaborationError("invalid_factual_comment") from None
            if base64.b64encode(data).decode("ascii") != encoded:
                raise TaskCollaborationError("invalid_factual_comment")
            payload = _decode_canonical(data)
            if type(payload) is not dict or payload.keys() != {
                "schema_version", "repository", "task", "branch_ref", "kind", "number", "subject",
                "record_id", "contract_id", "principal", "key", "text",
            }:
                raise TaskCollaborationError("invalid_factual_comment")
            if "\n".join(lines[1:-1]) != payload.get("text"):
                raise TaskCollaborationError("invalid_factual_comment")
            expected_body = self._render_factual_comment(payload)
            if (
                expected_body != comment.body
                or match.group(1) != hashlib.sha256(_COMMENT_DOMAIN + data).hexdigest()
                or payload["principal"] != comment.author_id
            ):
                raise TaskCollaborationError("invalid_factual_comment")
            try:
                if (
                    type(payload["schema_version"]) is not int
                    or payload["schema_version"] != SCHEMA_VERSION
                    or payload["kind"] not in {"pr", "issue"}
                    or type(payload["kind"]) is not str
                    or type(payload["number"]) is not int
                    or payload["number"] <= 0
                    or type(payload["principal"]) is not int
                    or payload["principal"] <= 0
                    or type(payload["repository"]) is not str
                    or type(payload["task"]) is not str
                    or type(payload["branch_ref"]) is not str
                    or type(payload["subject"]) is not str
                    or type(payload["record_id"]) is not str
                    or type(payload["contract_id"]) is not str
                    or type(payload["key"]) is not str
                    or type(payload["text"]) is not str
                ):
                    raise ValueError
                _valid_repo(payload["repository"])
                if (
                    not re.fullmatch(r"[1-9][0-9]{0,127}", payload["task"], re.ASCII)
                    or _require_branch(payload["branch_ref"]) != self._branch_ref
                    or payload["kind"] != comment.kind
                    or payload["number"] != comment.number
                ):
                    raise ValueError
                _require_oid(payload["subject"])
                _require_metadata_id(payload["record_id"])
                _require_metadata_id(payload["contract_id"])
                _require_text(payload["key"], MAX_KEY_LENGTH, "invalid_factual_comment", allow_empty=False)
                _require_text(payload["text"], MAX_REPORT_BYTES, "invalid_factual_comment", allow_empty=False, allow_multiline=True)
            except (KeyError, ValueError, TypeError, TaskCollaborationError):
                raise TaskCollaborationError("invalid_factual_comment") from None
            if payload["repository"] == self._repository and payload["task"] == self._task and payload["key"] == key:
                found.append((comment, payload))
        if not found:
            return None
        first = _canonical(found[0][1])
        if any(_canonical(payload) != first for _comment, payload in found[1:]):
            raise TaskCollaborationError("comment_key_conflict")
        return min(found, key=lambda item: item[0].comment_id)

    def _confirm_factual_comment_receipt(
        self,
        capture: _Capture,
        *,
        kind: str,
        number: int,
        key: str,
        payload: dict[str, Any],
        guarded_pr_number: int | None,
        issue_facts: IssueFacts | None,
    ) -> CollaborationReceipt:
        for _attempt in range(2):
            self._guard(capture, pr_number=guarded_pr_number)
            if kind == "issue":
                try:
                    if _validate_issue(self._github.issue(number), self._repository, number) != issue_facts:
                        raise TaskCollaborationError("issue_changed")
                except TaskCollaborationError:
                    raise
                except Exception:
                    raise TaskCollaborationError("issue_binding_mismatch") from None
            selected = self._owned_factual_comment(
                self._stable_comments(kind, number), principal=capture.principal, key=key,
            )
            if selected is None or _canonical(selected[1]) != _canonical(payload):
                raise TaskCollaborationError("comment_receipt_unconfirmed")
            self._guard(capture, pr_number=guarded_pr_number)
            if kind == "issue":
                try:
                    if _validate_issue(self._github.issue(number), self._repository, number) != issue_facts:
                        raise TaskCollaborationError("issue_changed")
                except TaskCollaborationError:
                    raise
                except Exception:
                    raise TaskCollaborationError("issue_binding_mismatch") from None
            final = self._owned_factual_comment(
                self._stable_comments(kind, number), principal=capture.principal, key=key,
            )
            if final is None or _canonical(final[1]) != _canonical(payload):
                raise TaskCollaborationError("comment_receipt_unconfirmed")
            if final[0].comment_id == selected[0].comment_id:
                self._guard(capture, pr_number=guarded_pr_number)
                return self._receipt(
                    capture.local.head,
                    number if kind == "pr" else None,
                    comment_id=final[0].comment_id,
                )
        raise TaskCollaborationError("comment_receipt_unconfirmed")

    def factual_comment(self, kind: str, number: int, key: str, text: str) -> CollaborationReceipt:
        if kind not in {"pr", "issue"}:
            raise TaskCollaborationError("invalid_comment_target")
        if type(number) is not int or number <= 0:
            raise TaskCollaborationError("invalid_comment_target")
        key = _require_text(key, MAX_KEY_LENGTH, "invalid_comment_key", allow_empty=False)
        text = _require_text(text, MAX_REPORT_BYTES, "invalid_factual_text", allow_empty=False, allow_multiline=True)
        params = {"kind": kind, "number": number, "key": key, "text": text}
        operation = "collaboration.factual_comment"
        if kind == "issue":
            if number != int(self._task):
                raise TaskCollaborationError("issue_binding_mismatch")
            # Bind issue-comment execution to the one current task PR too. This
            # prevents an issue write from silently proceeding when the durable
            # Task branch has a forked, closed, or cross-base PR surface.
            preliminary = self._read_local()
            task_pulls = self._task_pulls(
                self._stable_pulls(), preliminary.head, operation=operation,
            )
            if len(task_pulls) != 1:
                raise TaskCollaborationError("pull_request_not_established")
            guarded_pr_number: int | None = task_pulls[0].number
            params["pull_request"] = guarded_pr_number
        else:
            guarded_pr_number = number
        pr_number = guarded_pr_number
        capture = self._capture(operation, params, pr_number=pr_number)
        if capture.remote != capture.local.head:
            raise TaskCollaborationError("task_branch_not_published")
        issue_facts: IssueFacts | None = None
        if kind == "issue":
            try:
                issue_facts = _validate_issue(self._github.issue(number), self._repository, number)
            except Exception:
                raise TaskCollaborationError("issue_unavailable") from None
            # A Task's issue comment must not conceal a fork, historical, or
            # cross-base PR associated with the task branch.
            pulls = self._stable_pulls()
            self._task_pulls(pulls, capture.local.head, operation=operation)
        else:
            if capture.pull is None or capture.pull.number != number:
                raise TaskCollaborationError("pull_request_identity_conflict")
        payload = self._comment_payload(kind, number, key, text, capture)
        body = self._render_factual_comment(payload)
        intent_params = {**params, "subject": capture.local.head}
        intent = self._intent(capture, operation, intent_params)
        before = self._stable_comments(kind, number)
        existing = self._owned_factual_comment(before, principal=capture.principal, key=key)
        if existing is not None:
            comment, old = existing
            if _canonical(old) != _canonical(payload):
                raise TaskCollaborationError("comment_key_conflict")
            return self._confirm_factual_comment_receipt(
                capture, kind=kind, number=number, key=key, payload=payload,
                guarded_pr_number=pr_number, issue_facts=issue_facts,
            )
        self._authorize_and_record(capture, intent, content=body.encode("utf-8"), pr_number=pr_number)
        if kind == "issue":
            try:
                latest_issue = _validate_issue(self._github.issue(number), self._repository, number)
            except Exception:
                raise TaskCollaborationError("issue_binding_mismatch") from None
            if latest_issue != issue_facts:
                raise TaskCollaborationError("issue_changed")
        before = self._stable_comments(kind, number)
        existing = self._owned_factual_comment(before, principal=capture.principal, key=key)
        if existing is not None:
            comment, old = existing
            if _canonical(old) != _canonical(payload):
                raise TaskCollaborationError("comment_key_conflict")
            return self._confirm_factual_comment_receipt(
                capture, kind=kind, number=number, key=key, payload=payload,
                guarded_pr_number=pr_number, issue_facts=issue_facts,
            )
        self._guard(capture, pr_number=pr_number)
        try:
            self._github.post_comment(PostCommentIntent(self._repository, kind, number, body, intent))
        except Exception:
            pass
        try:
            return self._confirm_factual_comment_receipt(
                capture, kind=kind, number=number, key=key, payload=payload,
                guarded_pr_number=pr_number, issue_facts=issue_facts,
            )
        except Exception:
            raise TaskCollaborationError("comment_outcome_uncertain") from None

    def establish(
        self,
        report: str,
        metadata_provider: Callable[[str, str, str, int, str], object],
    ) -> CollaborationReceipt:
        """Establish branch, push, exact Draft PR, then a durable initial checkpoint.

        ``metadata_provider`` is the installed #191/#144 owner facade. It is
        invoked only when no identical initial checkpoint is present; this
        prevents retrying an immutable snapshot capture and silently changing
        the checkpoint's snapshot ID. No current pointer, lifecycle phase, or
        launch/ready signal is maintained by this facade.

        The provider owns uncertain #144/#217 publication recovery. Its durable
        key must use the stable repository/Task/branch/PR/subject and #191
        record/Contract identities, not the advancing metadata tip. It must
        persist the exact snapshot candidate/publication binding before it can
        be interrupted and return that same binding on retry; recapturing a
        different snapshot before the initial PR comment exists is not safe.
        """
        report = _require_text(report, MAX_REPORT_BYTES, "invalid_report", allow_empty=False, allow_multiline=True)
        if not callable(metadata_provider):
            raise TaskCollaborationError("metadata_provider_required")
        self.bootstrap()
        self.push()
        pr = self.ensure_draft_pr()
        number = pr.pull_request
        subject = pr.subject

        # Look for an already posted initial capsule before calling a snapshot
        # provider. Its immutable binding is the retry record, not a local file.
        capture = self._capture(
            "collaboration.initial_checkpoint",
            {"boundary": "initial", "key": "initial", "pr_number": number, "report": report},
            pr_number=number,
        )
        if capture.remote != subject:
            raise TaskCollaborationError("task_branch_not_published")
        comments = self._stable_comments("pr", number)
        existing = self._owned_checkpoint(
            comments, principal=capture.principal, key="initial", pr_number=number,
        )
        if existing is not None:
            comment, payload = existing
            if (
                payload["boundary"] != "initial"
                or payload["key"] != "initial"
                or payload["report"] != report
                or payload["subject"] != subject
                or payload["branch_ref"] != self._branch_ref
                or payload["repository"] != self._repository
                or payload["task"] != self._task
                or payload["pr_number"] != number
            ):
                raise TaskCollaborationError("checkpoint_key_conflict")
            binding = MetadataBinding(
                payload["metadata_commit"], payload["record_id"],
                payload["contract_id"], payload["snapshot_id"],
            )
            checked = self._validate_binding(
                binding, subject=subject, pr_number=number, capture=capture,
                expected_boundary="explicit-handoff",
            )
            return self._confirm_checkpoint_receipt(
                capture, checked, pr_number=number, subject=subject, boundary="initial",
                key="initial", expected_payload=payload,
            )

        self._guard(capture, pr_number=number)
        try:
            value = metadata_provider(self._repository, self._task, self._branch_ref, number, subject)
        except Exception:
            raise TaskCollaborationError("metadata_provider_failed") from None
        # Metadata publication may advance #217, but the Task/PR/Git identity
        # and current #191 record/Contract binding may not move in its callback.
        self._guard(capture, pr_number=number)
        # Provider returns only an exact #217/#191/#144 binding. It owns its
        # installed metadata authorization and candidate-intent seam.
        checked = self._validate_binding(
            value, subject=subject, pr_number=number, capture=capture,
            expected_boundary="explicit-handoff",
        )
        return self.initial_checkpoint(number, checked, report)


__all__ = [
    "CommentFacts", "ContentPolicyRequest", "CollaborationReceipt",
    "CreateDraftIntent", "EditMetadataIntent", "ExecutionAuthorizationRequest",
    "GitHubTransport", "IssueFacts", "MetadataBinding", "OPERATIONS",
    "PostCommentIntent", "PullFacts", "RepositoryFacts", "TaskCollaboration",
    "TaskCollaborationError", "WriteIntent",
]
