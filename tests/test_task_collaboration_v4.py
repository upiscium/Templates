from __future__ import annotations

import copy
import ast
import sys
import unittest
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import operation_prerequisites as prerequisites  # noqa: E402
import collaboration_git  # noqa: E402
import task_collaboration as collaboration  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as fixtures  # noqa: E402
from metadata_ref import MetadataStore  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "194"
_BRANCH = "refs/heads/issue-194"
_DEFAULT = "refs/heads/main"
_SUBJECT = "a" * 40
_TREE = "d" * 40


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


class FakeTaskGit:
    def __init__(self, store: Any, base_revision: str) -> None:
        self.store = store
        self.repository = _REPOSITORY
        self.task = _TASK
        self.branch_ref = _BRANCH
        self.base_revision = base_revision
        self.default_branch_ref = _DEFAULT
        self.head = _SUBJECT
        self.remote = _SUBJECT
        self.tree = _TREE
        self.clean = True
        self.index_fingerprint = "1" * 64
        self.status_fingerprint = "2" * 64
        self.bootstrap_post_callback = None
        self.bootstrap_calls = 0
        self.push_calls = 0
        self.publish_actions = 0
        self.on_candidate: Any = None
        self.on_intent: Any = None
        self.push_loses_ack = False

    def observe(self) -> LocalFacts:
        return LocalFacts(
            self.repository, self.task, self.branch_ref, self.head, self.tree,
            str(self.store.root), self.clean, self.index_fingerprint, self.status_fingerprint,
        )

    def remote_head(self) -> str | None:
        return self.remote

    def is_ancestor(self, old: str, new: str) -> bool:
        return old == new or (old == self.base_revision and new in {self._head_from_base(), self.head})

    def _head_from_base(self) -> str:
        return _SUBJECT

    def bootstrap_if_needed(self, expected_head: str, on_candidate=None, resume_candidate: str | None = None) -> str:
        self.bootstrap_calls += 1
        self.on_candidate = on_candidate
        if self.head == expected_head:
            candidate = resume_candidate or "b" * 40
            if on_candidate is not None:
                on_candidate(candidate)
            self.head = candidate
            if self.bootstrap_post_callback is not None:
                self.bootstrap_post_callback(self)
        return self.head

    def push_exact(self, expected_head: str, expected_remote: str | None, on_intent=None) -> str:
        self.push_calls += 1
        self.on_intent = on_intent
        if self.remote != expected_remote:
            raise RuntimeError("fixture remote changed")
        if self.remote != expected_head:
            if on_intent is not None:
                on_intent(collaboration_git.BranchPush(
                    _REPOSITORY, _TASK, _BRANCH, expected_head, expected_remote,
                ))
            self.publish_actions += 1
            self.remote = expected_head
        if self.push_loses_ack:
            raise RuntimeError("lost exact push acknowledgement")
        return expected_head


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_facts = collaboration.RepositoryFacts(_REPOSITORY, _DEFAULT, 1001)
        self.user_id = 77
        self.pulls: list[collaboration.PullFacts] = []
        self.comments: dict[tuple[str, int], list[collaboration.CommentFacts]] = {}
        self.issues: dict[int, collaboration.IssueFacts] = {
            int(_TASK): collaboration.IssueFacts(_REPOSITORY, int(_TASK), "Task 194", "Factual issue", "open"),
        }
        self.next_pr = 91
        self.next_comment = 201
        self.create_loses_ack = False
        self.create_hard_interrupt = False
        self.edit_loses_ack = False
        self.edit_hard_interrupt = False
        self.comment_loses_ack = False
        self.comment_hard_interrupt = False
        self.create_duplicate_race = False
        self.pull_page_hook = None
        self.pull_page_calls = 0
        self.comments_page_hook = None
        self.comments_page_calls = 0
        self.write_calls: list[object] = []

    def repository(self) -> collaboration.RepositoryFacts:
        return self.repository_facts

    def principal(self) -> int:
        return self.user_id

    def pull_page(self, page: int) -> tuple[collaboration.PullFacts, ...]:
        self.pull_page_calls += 1
        if self.pull_page_hook is not None:
            self.pull_page_hook(self, page, self.pull_page_calls)
        start = (page - 1) * collaboration.GITHUB_PAGE_SIZE
        return tuple(self.pulls[start:start + collaboration.GITHUB_PAGE_SIZE])

    def comments_page(self, kind: str, number: int, page: int) -> tuple[collaboration.CommentFacts, ...]:
        self.comments_page_calls += 1
        values = sorted(self.comments.get((kind, number), []), key=lambda item: item.comment_id)
        start = (page - 1) * collaboration.GITHUB_PAGE_SIZE
        result = tuple(values[start:start + collaboration.GITHUB_PAGE_SIZE])
        if self.comments_page_hook is not None:
            self.comments_page_hook(self, kind, number, page, self.comments_page_calls)
        return result

    def issue(self, number: int) -> collaboration.IssueFacts:
        return self.issues[number]

    def _new_pull(
        self,
        title: str,
        body: str,
        number: int | None = None,
        *,
        base_ref: str = "main",
        head_ref: str = "issue-194",
        head_oid: str = _SUBJECT,
    ) -> collaboration.PullFacts:
        return collaboration.PullFacts(
            self.next_pr if number is None else number, _REPOSITORY, _REPOSITORY,
            base_ref, head_ref, head_oid, "open", True, title, body,
        )

    def create_draft(self, intent: collaboration.CreateDraftIntent) -> object:
        self.write_calls.append(intent)
        self.pulls.append(self._new_pull(
            intent.title,
            intent.body,
            base_ref=intent.base_ref.removeprefix("refs/heads/"),
            head_ref=intent.branch_ref.removeprefix("refs/heads/"),
            head_oid=intent.head_oid,
        ))
        if self.create_duplicate_race:
            self.pulls.append(self._new_pull(intent.title, intent.body, self.next_pr + 1))
        if self.create_loses_ack:
            raise RuntimeError("lost acknowledgement with private detail")
        if self.create_hard_interrupt:
            raise KeyboardInterrupt("simulated process interruption")
        return {"untrusted": "acknowledgement is ignored"}

    def edit_metadata(self, intent: collaboration.EditMetadataIntent) -> object:
        self.write_calls.append(intent)
        for index, pull in enumerate(self.pulls):
            if pull.number == intent.number:
                self.pulls[index] = replace(pull, title=intent.new_title, body=intent.new_body)
                break
        if self.edit_loses_ack:
            raise RuntimeError("lost edit acknowledgement")
        if self.edit_hard_interrupt:
            raise KeyboardInterrupt("simulated process interruption")
        return object()

    def post_comment(self, intent: collaboration.PostCommentIntent) -> object:
        self.write_calls.append(intent)
        comment = collaboration.CommentFacts(
            _REPOSITORY, intent.kind, intent.number, self.next_comment,
            self.user_id, intent.body,
        )
        self.next_comment += 1
        self.comments.setdefault((intent.kind, intent.number), []).append(comment)
        if self.comment_loses_ack:
            raise RuntimeError("lost comment acknowledgement")
        if self.comment_hard_interrupt:
            raise KeyboardInterrupt("simulated process interruption")
        return object()


class TaskCollaborationV4Test(unittest.TestCase):
    """Local-only tests with mocked GitHub side effects and temporary metadata."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)

    def setUp(self) -> None:
        self._fixture_set_up()
        fixtures.git("branch", "-m", "issue-194", cwd=self.product)
        self.base_revision = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.records = task_record.TaskRecords(self.store, authorize=lambda _request: True)
        self.publication = self.records.create(
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=self.base_revision,
            title="Task 194 requirement",
            body="Initial requirements for the task.",
        )
        self.git = FakeTaskGit(self.store, self.base_revision)
        self.github = FakeGitHub()
        self.intents: list[collaboration.WriteIntent] = []
        self.authorization_requests: list[collaboration.ExecutionAuthorizationRequest] = []
        self.content_requests: list[collaboration.ContentPolicyRequest] = []
        self.snapshot_views: dict[str, dict[str, Any]] = {}
        self.deny_execution = False
        self.deny_content = False
        self.engines = {
            operation: self._engine(operation)
            for operation in collaboration.OPERATIONS
        }
        self.facade = self._facade()
        self.observe_patch = mock.patch.object(
            collaboration.task_view, "observe_live_task", side_effect=self._observe_view,
        )
        self.observe_patch.start()
        self.addCleanup(self.observe_patch.stop)
        self.snapshot_patch = mock.patch.object(
            collaboration.task_view.TaskViewSnapshots, "read", side_effect=self._read_snapshot,
        )
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        self.confirm_patch = mock.patch.object(
            self.store, "confirm", side_effect=lambda commit, **_kwargs: commit,
        )
        self.confirm_patch.start()
        self.addCleanup(self.confirm_patch.stop)

    def test_actual_task_view_snapshot_owner_round_trip(self) -> None:
        """Exercise the real #144/#217 writer and reader on temporary local Git."""
        fixtures.git("push", "origin", f"{self.base_revision}:{_BRANCH}", cwd=self.product)
        pull = collaboration.PullFacts(
            91, _REPOSITORY, _REPOSITORY, "main", "issue-194", self.base_revision,
            "open", True, "Task 194 requirement", "Initial requirements for the task.",
        )
        self.github.pulls = [pull]

        def reader(request: task_view.GitHubPullRequestRequest) -> object:
            self.assertEqual(91, request.number)
            return task_view.GitHubObservation(task_view.GitHubPullRequestFacts(
                _REPOSITORY, _REPOSITORY, 91, "open", True,
                "main", "issue-194", self.base_revision,
            ))

        self.observe_patch.stop()
        self.snapshot_patch.stop()
        self.confirm_patch.stop()
        try:
            snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
            publication = snapshots.capture(
                task=_TASK,
                branch_ref=_BRANCH,
                base_revision=self.base_revision,
                boundary="explicit-handoff",
                default_branch_ref=_DEFAULT,
                observe_remote_head=True,
                pr_number=91,
                github_reader=reader,
            )
            decoded = snapshots.read(
                publication.metadata_commit, publication.snapshot_id,
                task=_TASK, subject=self.base_revision,
            )
            clone = self.temp_root / "snapshot-reader-clone"
            fixtures.git(
                "clone", "--no-checkout", "--branch", "issue-194",
                str(self.remote_path), str(clone),
            )
            clone_store = MetadataStore(clone, _REPOSITORY)
            clone_snapshot = task_view.TaskViewSnapshots(
                clone_store, authorize=lambda _request: False,
            ).read(
                publication.metadata_commit, publication.snapshot_id,
                task=_TASK, subject=self.base_revision,
            )
        finally:
            self.observe_patch.start()
            self.snapshot_patch.start()
            self.confirm_patch.start()
        self.assertEqual(self.base_revision, decoded["view"]["subject"])
        self.assertEqual(self.publication.record_id, decoded["view"]["authority"]["record_id"])
        self.assertEqual(self.publication.contract_id, decoded["view"]["authority"]["contract_id"])
        self.assertEqual(91, decoded["view"]["github"]["pull_request"]["number"])
        self.assertEqual(self.base_revision, decoded["view"]["git"]["remote_head"]["head_oid"])
        self.assertEqual(_REPOSITORY, clone_snapshot["view"]["repository"])
        self.assertEqual(_TASK, clone_snapshot["view"]["task"])
        self.assertEqual(self.base_revision, clone_snapshot["view"]["subject"])

    def test_establish_end_to_end_uses_real_taskgit_and_real_snapshot_confirmation(self) -> None:
        """Real temporary Git/#217/#144 paths; GitHub remains an in-memory fake."""
        published: list[collaboration_git.BranchPush] = []

        def publish(intent: collaboration_git.BranchPush) -> None:
            published.append(intent)
            fixtures.git(
                "push", "--no-follow-tags", "origin",
                f"{intent.subject}:{intent.branch_ref}", cwd=self.product,
            )

        actual_git = collaboration_git.TaskGit(
            self.store,
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=self.base_revision,
            default_branch_ref=_DEFAULT,
            publish=publish,
        )
        self.git = actual_git
        self.facade = self._facade()
        self.observe_patch.stop()
        self.snapshot_patch.stop()
        self.confirm_patch.stop()
        provider_calls: list[tuple[str, str, str, int, str]] = []
        snapshot_captures: list[task_view.SnapshotPublication] = []
        # Fixture-only host intent journal survives the injected BaseException;
        # the key deliberately excludes the #217 metadata tip.
        provider_receipts: dict[tuple[str, str, str, int, str, str, str], collaboration.MetadataBinding] = {}
        interrupt_after_snapshot = True

        def provider(
            repository: str, task: str, branch_ref: str, number: int, subject: str,
        ) -> collaboration.MetadataBinding:
            nonlocal interrupt_after_snapshot
            self.assertEqual(1, len(self.github.pulls))
            pull = self.github.pulls[0]
            self.assertEqual((number, subject), (pull.number, pull.head_oid))
            provider_calls.append((repository, task, branch_ref, number, subject))
            stable_key = (
                repository, task, branch_ref, number, subject,
                self.publication.record_id, self.publication.contract_id,
            )
            if stable_key in provider_receipts:
                return provider_receipts[stable_key]

            def github_reader(request: task_view.GitHubPullRequestRequest) -> object:
                self.assertEqual((repository, task, number, branch_ref), (
                    request.repository, request.task, request.number, request.branch_ref,
                ))
                observation = self.facade.github_observation(request)
                self.assertIsNone(observation.previous_checkpoint)
                return observation

            publication = task_view.TaskViewSnapshots(
                self.store, authorize=lambda _request: True,
            ).capture(
                task=task,
                branch_ref=branch_ref,
                base_revision=self.base_revision,
                boundary="explicit-handoff",
                default_branch_ref=_DEFAULT,
                observe_remote_head=True,
                pr_number=number,
                github_reader=github_reader,
            )
            snapshot_captures.append(publication)
            binding = collaboration.MetadataBinding(
                publication.metadata_commit,
                self.publication.record_id,
                self.publication.contract_id,
                publication.snapshot_id,
            )
            # Simulate hard interruption after the owner writer published the
            # exact candidate but before #194 could post its initial comment.
            provider_receipts[stable_key] = binding
            if interrupt_after_snapshot:
                interrupt_after_snapshot = False
                raise KeyboardInterrupt("simulated host interruption after #144 publication")
            return binding

        try:
            with self.assertRaises(KeyboardInterrupt):
                self.facade.establish("Initial factual report.\nSecond factual line.", provider)
            self.assertEqual([], self.github.comments.get(("pr", 91), []))
            self.assertIsNone(self.facade.github_observation(
                task_view.GitHubPullRequestRequest(_REPOSITORY, _TASK, 91, _BRANCH),
            ).previous_checkpoint)
            receipt = self.facade.establish("Initial factual report.\nSecond factual line.", provider)
            self.assertEqual(2, len(provider_calls))
            self.assertEqual(1, len(snapshot_captures))
            self.assertEqual(1, len(published))
            self.assertEqual(1, len(self.github.pulls))
            self.assertIsNotNone(receipt.comment_id)
            self.assertIsNotNone(receipt.metadata_commit)
            self.assertEqual(snapshot_captures[0].metadata_commit, receipt.metadata_commit)
            self.assertEqual(snapshot_captures[0].snapshot_id, receipt.snapshot_id)
            self.assertEqual(self.github.pulls[0].head_oid, receipt.subject)
            self.assertEqual(receipt.subject, actual_git.remote_head())
            initial_observation = self.facade.github_observation(
                task_view.GitHubPullRequestRequest(_REPOSITORY, _TASK, 91, _BRANCH),
            )
            self.assertIsNotNone(initial_observation.previous_checkpoint)
            self.assertEqual(str(receipt.comment_id), initial_observation.previous_checkpoint.checkpoint_id)
            self.assertEqual(receipt.metadata_commit, initial_observation.previous_checkpoint.metadata_commit)
            self.assertEqual(receipt.snapshot_id, initial_observation.previous_checkpoint.snapshot_id)
            self.assertEqual("initial", self.facade._decode_checkpoint(
                self.github.comments[("pr", receipt.pull_request)][0].body,
            )[0]["key"])
            turn_publication = task_view.TaskViewSnapshots(
                self.store, authorize=lambda _request: True,
            ).capture(
                task=_TASK,
                branch_ref=_BRANCH,
                base_revision=self.base_revision,
                boundary="turn-end",
                default_branch_ref=_DEFAULT,
                observe_remote_head=True,
                pr_number=91,
                github_reader=self.facade.github_observation,
            )
            turn_binding = collaboration.MetadataBinding(
                turn_publication.metadata_commit,
                self.publication.record_id,
                self.publication.contract_id,
                turn_publication.snapshot_id,
            )
            turn_snapshot = task_view.TaskViewSnapshots(
                self.store, authorize=lambda _request: False,
            ).read(
                turn_publication.metadata_commit, turn_publication.snapshot_id,
                task=_TASK, subject=receipt.subject,
            )
            self.assertEqual("observed", turn_snapshot["view"]["previous_checkpoint"]["state"])
            self.assertEqual(str(receipt.comment_id), turn_snapshot["view"]["previous_checkpoint"]["checkpoint_id"])
            turn_receipt = self.facade.turn_checkpoint(
                91, turn_binding, "turn-1", "Turn-end report.", boundary="turn-end",
            )
            latest = self.facade.github_observation(
                task_view.GitHubPullRequestRequest(_REPOSITORY, _TASK, 91, _BRANCH),
            ).previous_checkpoint
            self.assertIsNotNone(latest)
            self.assertEqual(str(turn_receipt.comment_id), latest.checkpoint_id)
            self.assertEqual(turn_receipt.metadata_commit, latest.metadata_commit)
            self.assertEqual(turn_receipt.snapshot_id, latest.snapshot_id)
            fresh = task_view.observe_live_task(
                self.store, task=_TASK, branch_ref=_BRANCH, base_revision=self.base_revision,
                default_branch_ref=_DEFAULT, observe_remote_head=True, pr_number=91,
                github_reader=self.facade.github_observation,
            )
            self.assertEqual(str(turn_receipt.comment_id), fresh["previous_checkpoint"]["checkpoint_id"])
            clone = self.temp_root / "turn-reader-clone"
            fixtures.git(
                "clone", "--no-checkout", "--branch", "issue-194",
                str(self.remote_path), str(clone),
            )
            clone_snapshots = task_view.TaskViewSnapshots(
                MetadataStore(clone, _REPOSITORY), authorize=lambda _request: False,
            )
            clone_turn = clone_snapshots.read(
                turn_publication.metadata_commit, turn_publication.snapshot_id,
                task=_TASK, subject=receipt.subject,
            )
            self.assertEqual(str(receipt.comment_id), clone_turn["view"]["previous_checkpoint"]["checkpoint_id"])
            # A hard retry re-reads the #217 snapshot graph and exact comment;
            # it does not call the capture provider a second time.
            retry = self.facade.establish("Initial factual report.\nSecond factual line.", provider)
            self.assertEqual(receipt.comment_id, retry.comment_id)
            self.assertEqual(2, len(provider_calls))
            self.assertEqual(1, len(snapshot_captures))
        finally:
            self.observe_patch.start()
            self.snapshot_patch.start()
            self.confirm_patch.start()

    def _engine(self, operation: str) -> prerequisites.OperationPrerequisites:
        policy = prerequisites.OperationPolicy(
            operation, "test_host_policy", "execution",
            (prerequisites.CheckSpec("fixture_facts", "fixture_host"),),
        )

        def check(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            return prerequisites.CheckResult(
                context.binding, "SATISFIED", "fixture_facts_current", {}, {}, "none",
            )

        return prerequisites.OperationPrerequisites(policy, checks={"fixture_facts": check})

    def _facade(self) -> collaboration.TaskCollaboration:
        def authorize(request: collaboration.ExecutionAuthorizationRequest) -> bool:
            self.authorization_requests.append(request)
            return not self.deny_execution

        def record_intent(intent: collaboration.WriteIntent) -> bool:
            self.intents.append(intent)
            return True

        def content_policy(request: collaboration.ContentPolicyRequest) -> bool:
            self.content_requests.append(request)
            return not self.deny_content

        return collaboration.TaskCollaboration(
            self.store, self.git, self.github,
            prerequisites_by_operation=self.engines,
            authorize=authorize,
            record_intent=record_intent,
            content_policy=content_policy,
        )

    def _view(self, pr_number: int | None = None) -> dict[str, Any]:
        pull = next((item for item in self.github.pulls if item.number == pr_number), None)
        record_id = self.publication.record_id
        contract_id = self.publication.contract_id
        if hasattr(self.git, "head"):
            head = self.git.head
            tree = _TREE
            remote = self.git.remote
        else:
            local = self.git.observe()
            head = local.head
            tree = local.tree
            remote = self.git.remote_head()
        if pull is None:
            previous_checkpoint = {"state": "not_requested"}
        else:
            observation = self.facade.github_observation(task_view.GitHubPullRequestRequest(
                _REPOSITORY, _TASK, pull.number, _BRANCH,
            ))
            previous = observation.previous_checkpoint
            if previous is None:
                previous_checkpoint = {"state": "absent"}
            else:
                previous_checkpoint = {
                    "state": "observed",
                    "checkpoint_id": previous.checkpoint_id,
                    "subject": previous.subject,
                    "metadata_commit": previous.metadata_commit,
                    "snapshot_id": previous.snapshot_id,
                    "commits_after": (
                        {"state": "observed", "count": 0}
                        if previous.subject == head
                        else {"state": "unknown", "reason": "observation_failed"}
                    ),
                }
        return {
            "schema_version": 1,
            "repository": _REPOSITORY,
            "task": _TASK,
            "subject": head,
            "branch_ref": _BRANCH,
            "git": {
                "head": head,
                "tree": tree,
                "branch_ref": _BRANCH,
                "branch_matches_task": True,
                "status": {
                    "index_changes": 0,
                    "worktree_changes": 0,
                    "untracked": 0,
                    "conflicts": 0,
                    "submodule_changes": 0,
                },
                "worktrees": [],
                "default_branch": {"state": "not_requested"},
                "remote_head": (
                    {"state": "unavailable", "error_code": "ref_absent"}
                    if remote is None
                    else {"state": "observed", "head_oid": remote}
                ),
                "submodule_worktrees": "not_inspected",
                "staged_gitlinks": 0,
            },
            "authority": {
                "state": "observed",
                "metadata_commit": self.publication.metadata_commit,
                "record_id": record_id,
                "contract_id": contract_id,
                "base_revision": self.base_revision,
                "branch_ref": _BRANCH,
                "disposition": None,
            },
            "evidence": [],
            "github": {
                "state": "not_requested" if pull is None else "observed",
                **({} if pull is None else {"pull_request": {
                    "base_repository": pull.base_repository,
                    "head_repository": pull.head_repository,
                    "number": pull.number,
                    "state": pull.state,
                    "draft": pull.draft,
                    "base_ref": pull.base_ref,
                    "head_ref": pull.head_ref,
                    "head_oid": pull.head_oid,
                }}),
            },
            "previous_checkpoint": previous_checkpoint,
        }

    def _observe_view(self, _store: Any, **kwargs: Any) -> dict[str, Any]:
        number = kwargs.get("pr_number")
        return self._view(number)

    def _snapshot(self, subject: str, pr_number: int, snapshot_id: str | None = None) -> dict[str, Any]:
        if snapshot_id is not None and snapshot_id in self.snapshot_views:
            return copy.deepcopy(self.snapshot_views[snapshot_id])
        view = self._view(pr_number)
        view["subject"] = subject
        view["git"]["head"] = subject
        view["git"]["remote_head"] = {"state": "observed", "head_oid": subject}
        return {"boundary": "explicit-handoff", "view": copy.deepcopy(view)}

    def _read_snapshot(self, _commit: str, _snapshot_id: str, *, task: str, subject: str) -> dict[str, Any]:
        self.assertEqual(_TASK, task)
        snapshot = self._snapshot(subject, 91, _snapshot_id)
        if snapshot["view"]["subject"] != subject:
            raise ValueError("snapshot fixture subject mismatch")
        return snapshot

    def _binding(self, snapshot_id: str = "e" * 64) -> collaboration.MetadataBinding:
        binding = collaboration.MetadataBinding(
            self.publication.metadata_commit,
            self.publication.record_id,
            self.publication.contract_id,
            snapshot_id,
        )
        if snapshot_id not in self.snapshot_views:
            pull = self.github.pulls[0]
            view = self._view(pull.number)
            view["git"]["remote_head"] = {"state": "observed", "head_oid": self.git.head}
            self.snapshot_views[snapshot_id] = {
                "boundary": "explicit-handoff",
                "view": copy.deepcopy(view),
            }
        return binding

    def _add_pull(self, **overrides: Any) -> collaboration.PullFacts:
        pull = collaboration.PullFacts(
            91, _REPOSITORY, _REPOSITORY, "main", "issue-194", self.git.head,
            "open", True, "Task 194 requirement", "Initial requirements for the task.",
        )
        pull = replace(pull, **overrides)
        self.github.pulls.append(pull)
        return pull

    def _initial_comment(self, report: str = "Initial task facts.") -> collaboration.CollaborationReceipt:
        self.github.create_loses_ack = False
        self.facade.ensure_draft_pr()
        return self.facade.initial_checkpoint(91, self._binding(), report)

    def test_frozen_intents_and_authorization_are_separate_from_prerequisites(self) -> None:
        self.deny_execution = True
        with self.assertRaises(collaboration.TaskCollaborationError) as denied:
            self.facade.ensure_draft_pr()
        self.assertEqual("execution_authorization_denied", denied.exception.code)
        self.assertEqual([], self.github.write_calls)
        self.assertEqual([], self.intents)

        self.deny_execution = False
        receipt = self.facade.ensure_draft_pr()
        self.assertEqual(91, receipt.pull_request)
        request = self.authorization_requests[-1]
        with self.assertRaises(FrozenInstanceError):
            request.intent_id = "mutated"  # type: ignore[misc]
        intent = self.intents[-1]
        with self.assertRaises(FrozenInstanceError):
            intent.subject = "b" * 40  # type: ignore[misc]
        with self.assertRaises(TypeError):
            intent.parameters[0] = 0  # type: ignore[index]
        self.assertEqual("collaboration.draft_pr", intent.operation)
        self.assertNotIn("ready", repr(receipt).lower())

    def test_create_adopts_one_exact_open_draft_and_lost_ack_is_reconciled(self) -> None:
        self.github.create_loses_ack = True
        receipt = self.facade.ensure_draft_pr()
        self.assertEqual(91, receipt.pull_request)
        self.assertEqual(1, len(self.github.pulls))
        self.assertEqual("Task 194 requirement", self.github.pulls[0].title)
        self.assertEqual("Initial requirements for the task.", self.github.pulls[0].body)
        writes = len(self.github.write_calls)
        self.assertEqual(91, self.facade.ensure_draft_pr().pull_request)
        self.assertEqual(writes, len(self.github.write_calls))

    def test_exact_push_is_nonforce_and_lost_ack_uses_remote_postcondition(self) -> None:
        self.git.remote = None
        self.git.push_loses_ack = True
        pushed = self.facade.push()
        self.assertEqual(_SUBJECT, pushed)
        self.assertEqual(_SUBJECT, self.git.remote)
        self.assertEqual(1, len(self.intents))
        intent = self.intents[0]
        parameters = intent.parameters.decode("utf-8")
        self.assertIn('"force":false', parameters)
        self.assertIn('"expected_remote":null', parameters)
        self.assertIn('"target_ref":"refs/heads/issue-194"', parameters)

    def test_core_uses_the_actual_taskgit_bootstrap_and_exact_push_api(self) -> None:
        published: list[collaboration_git.BranchPush] = []

        def publish(intent: collaboration_git.BranchPush) -> None:
            published.append(intent)
            fixtures.git(
                "push", "--no-follow-tags", "origin",
                f"{intent.subject}:{intent.branch_ref}", cwd=self.product,
            )

        actual_git = collaboration_git.TaskGit(
            self.store,
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=self.base_revision,
            default_branch_ref=_DEFAULT,
            publish=publish,
        )
        self.git = actual_git
        self.facade = self._facade()
        candidate = self.facade.bootstrap()
        self.assertNotEqual(self.base_revision, candidate)
        self.assertEqual(candidate, actual_git.observe().head)
        self.assertEqual(self.base_revision, fixtures.git(
            "show", "-s", "--format=%P", candidate, cwd=self.product,
        ).decode("ascii").strip())
        self.assertEqual(candidate, self.facade.push())
        self.assertEqual(candidate, actual_git.remote_head())
        self.assertEqual(candidate, self.facade.bootstrap(resume_candidate=candidate))
        self.assertEqual(1, len(published))
        self.assertEqual(_BRANCH, published[0].branch_ref)
        self.assertEqual(candidate, published[0].subject)
        self.assertTrue(any(intent.operation == "collaboration.bootstrap" for intent in self.intents))
        self.assertTrue(any(intent.operation == "collaboration.push" for intent in self.intents))

    def test_bootstrap_refusal_preserves_applied_candidate_but_never_continues_to_push_or_pr(self) -> None:
        mutations = {
            "tree": lambda git: setattr(git, "tree", "c" * 40),
            "clean": lambda git: setattr(git, "clean", False),
            "index_fingerprint": lambda git: setattr(git, "index_fingerprint", "3" * 64),
            "status_fingerprint": lambda git: setattr(git, "status_fingerprint", "4" * 64),
        }
        for field, mutation in mutations.items():
            with self.subTest(field=field):
                git = FakeTaskGit(self.store, self.base_revision)
                git.head = self.base_revision
                git.remote = None
                git.bootstrap_post_callback = mutation
                self.git = git
                self.facade = self._facade()
                with self.assertRaises(collaboration.TaskCollaborationError) as refused:
                    self.facade.establish("Do not continue after local bootstrap drift.", lambda *_args: self.fail("provider ran"))
                self.assertEqual("bootstrap_applied_local_state_changed", refused.exception.code)
                self.assertNotEqual(self.base_revision, git.head)
                self.assertEqual([], self.github.write_calls)
                self.assertEqual(0, git.push_calls)

    def test_bootstrap_cleanup_failure_is_not_reconciled_as_lost_ack(self) -> None:
        self.git.head = self.base_revision
        self.git.remote = None
        original = self.git.bootstrap_if_needed

        def applied_then_cleanup_failed(**kwargs):
            original(**kwargs)
            raise collaboration_git.DistinctTaskGitError("local_lock_cleanup_failed", "bootstrap")

        with mock.patch.object(self.git, "bootstrap_if_needed", side_effect=applied_then_cleanup_failed):
            with self.assertRaises(collaboration.TaskCollaborationError) as failure:
                self.facade.establish("Do not mask failed lock cleanup.", lambda *_args: self.fail("provider ran"))
        self.assertEqual("local_lock_cleanup_failed", failure.exception.code)
        self.assertNotEqual(self.base_revision, self.git.head)
        self.assertEqual(0, self.git.push_calls)
        self.assertEqual([], self.github.write_calls)

    def test_bootstrap_final_view_race_checks_all_local_facts(self) -> None:
        self.git.head = self.base_revision
        self.git.remote = None
        original = self.facade._observe_view
        post_views = 0

        def late_index_change(subject, pr_number):
            nonlocal post_views
            result = original(subject, pr_number)
            if subject != self.base_revision:
                post_views += 1
                if post_views == 2:
                    self.git.index_fingerprint = "8" * 64
            return result

        with mock.patch.object(self.facade, "_observe_view", side_effect=late_index_change):
            with self.assertRaises(collaboration.TaskCollaborationError) as failure:
                self.facade.establish("Do not mask late index movement.", lambda *_args: self.fail("provider ran"))
        self.assertEqual("bootstrap_applied_local_state_changed", failure.exception.code)
        self.assertEqual(2, post_views)
        self.assertEqual(0, self.git.push_calls)
        self.assertEqual([], self.github.write_calls)

    def test_remote_change_during_authorization_prevents_push_attempt(self) -> None:
        self.git.remote = None

        def racing_authorize(_request: collaboration.ExecutionAuthorizationRequest) -> bool:
            self.git.remote = "b" * 40
            return True

        self.facade._authorize = racing_authorize
        with self.assertRaises(collaboration.TaskCollaborationError) as changed:
            self.facade.push()
        self.assertEqual("task_facts_changed", changed.exception.code)
        self.assertEqual("b" * 40, self.git.remote)
        self.assertEqual(0, self.git.publish_actions)
        self.assertEqual([], self.intents)

    def test_duplicate_race_and_closed_forked_cross_base_or_wrong_head_pr_refuse(self) -> None:
        self.github.create_duplicate_race = True
        with self.assertRaises(collaboration.TaskCollaborationError) as race:
            self.facade.ensure_draft_pr()
        self.assertIn(race.exception.code, {"draft_pr_outcome_uncertain", "duplicate_task_pull_requests"})
        self.assertEqual(2, len(self.github.pulls))

        for pull in (
            replace(self._new_pull(), state="closed"),
            replace(self._new_pull(), head_repository="fork/widgets"),
            replace(self._new_pull(), base_ref="release"),
            replace(self._new_pull(), head_oid="b" * 40),
        ):
            with self.subTest(pull=pull):
                self.github.pulls[:] = [pull]
                with self.assertRaises(collaboration.TaskCollaborationError):
                    self.facade.ensure_draft_pr()
                self.assertEqual([pull], self.github.pulls)

    def _new_pull(self) -> collaboration.PullFacts:
        return collaboration.PullFacts(
            92, _REPOSITORY, _REPOSITORY, "main", "issue-194", self.git.head,
            "open", True, "Task 194 requirement", "Initial requirements for the task.",
        )

    def test_cross_repository_base_and_malformed_transport_facts_fail_closed(self) -> None:
        self.github.pulls = [replace(self._new_pull(), base_repository="other/widgets")]
        with self.assertRaises(collaboration.TaskCollaborationError) as cross_base:
            self.facade.ensure_draft_pr()
        self.assertEqual("cross_repository_pull_request", cross_base.exception.code)

        malformed = replace(self._new_pull(), state=[])  # type: ignore[arg-type]
        self.github.pulls = [malformed]
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.ensure_draft_pr()

    def test_repository_id_and_task_branch_binding_are_independently_pinned(self) -> None:
        self.github.repository_facts = replace(self.github.repository_facts, repository_id=1002)
        with self.assertRaises(collaboration.TaskCollaborationError) as changed_repo:
            self.facade.push()
        self.assertEqual("github_repository_identity_changed", changed_repo.exception.code)
        self.assertEqual([], self.github.write_calls)

        bad_git = FakeTaskGit(self.store, self.base_revision)
        bad_git.default_branch_ref = bad_git.branch_ref
        with self.assertRaises(collaboration.TaskCollaborationError) as same_branch:
            collaboration.TaskCollaboration(
                self.store,
                bad_git,
                self.github,
                prerequisites_by_operation=self.engines,
                authorize=lambda _request: True,
                record_intent=lambda _intent: True,
                content_policy=lambda _request: True,
            )
        self.assertEqual("task_git_binding_mismatch", same_branch.exception.code)

    def test_pull_pagination_is_complete_bounded_deduplicated_and_reobserved(self) -> None:
        many = [
            collaboration.PullFacts(
                index + 1, _REPOSITORY, _REPOSITORY, "main", f"other-{index}", _SUBJECT,
                "closed", False, "Old pull", "Old body",
            )
            for index in range(collaboration.MAX_GITHUB_ITEMS)
        ]
        self.github.pulls = many
        with self.assertRaises(collaboration.TaskCollaborationError) as cap:
            self.facade._pulls_once()
        self.assertEqual("github_page_limit", cap.exception.code)

        duplicate = self._new_pull()
        self.github.pulls = [duplicate, duplicate]
        with self.assertRaises(collaboration.TaskCollaborationError) as repeated:
            self.facade._pulls_once()
        self.assertEqual("github_pagination_duplicate", repeated.exception.code)

        self.github.pulls = []
        self.github.pull_page_calls = 0
        mutated = False

        def drifting(github: FakeGitHub, _page: int, calls: int) -> None:
            nonlocal mutated
            if calls == 1 and not mutated:
                mutated = True
            elif calls == 2 and mutated and not github.pulls:
                github.pulls.append(self._new_pull())

        self.github.pull_page_hook = drifting
        with self.assertRaises(collaboration.TaskCollaborationError) as drift:
            self.facade._stable_pulls()
        self.assertEqual("github_pagination_drift", drift.exception.code)

        self.github.pull_page_hook = None
        self.github.pull_page_calls = 0
        self.github.pulls = [self._new_pull(), replace(self._new_pull(), number=93)]

        def reorder_between_scans(github: FakeGitHub, _page: int, calls: int) -> None:
            if calls == 2:
                github.pulls.reverse()

        self.github.pull_page_hook = reorder_between_scans
        self.assertEqual((92, 93), tuple(item.number for item in self.facade._stable_pulls()))

    def test_unrelated_deleted_fork_and_large_multiline_metadata_do_not_block_task_pr(self) -> None:
        unrelated = collaboration.PullFacts(
            14, _REPOSITORY, None, "main", "deleted-fork-branch", None,
            "closed", False, "x" * 300, "Ordinary multiline markdown.\n" + "x" * 20_000,
        )
        self.github.pulls = [unrelated]
        self.assertEqual((unrelated,), self.facade._stable_pulls())
        self.github.pulls = []
        receipt = self.facade.ensure_draft_pr()
        self.assertEqual(91, receipt.pull_request)

    def test_metadata_update_uses_only_two_fields_and_reconciles_lost_ack(self) -> None:
        self._add_pull()
        self.github.edit_loses_ack = True
        receipt = self.facade.update_metadata(
            91, self.git.head, "Task 194 requirement", "Initial requirements for the task.",
            "Task 194 updated", "Updated factual body.",
        )
        self.assertEqual(91, receipt.pull_request)
        self.assertEqual("Task 194 updated", self.github.pulls[0].title)
        self.assertEqual("Updated factual body.", self.github.pulls[0].body)
        update_intent = self.github.write_calls[-1]
        self.assertIs(type(update_intent), collaboration.EditMetadataIntent)
        self.assertFalse(hasattr(update_intent, "new_head"))
        self.assertFalse(hasattr(update_intent, "new_base"))

        # Exact already-desired values are idempotent; a third-party value is
        # never overwritten under the stale expected-old request.
        count = len(self.github.write_calls)
        self.facade.update_metadata(
            91, self.git.head, "old title", "old body", "Task 194 updated", "Updated factual body.",
        )
        self.assertEqual(count, len(self.github.write_calls))
        self.github.pulls[0] = replace(self.github.pulls[0], body="Human edit")
        with self.assertRaises(collaboration.TaskCollaborationError) as conflict:
            self.facade.update_metadata(
                91, self.git.head, "Task 194 updated", "Updated factual body.",
                "Replacement", "Replacement body",
            )
        self.assertEqual("pull_request_metadata_conflict", conflict.exception.code)
        self.assertEqual("Human edit", self.github.pulls[0].body)

    def test_metadata_update_race_during_host_callback_never_overwrites_human_text(self) -> None:
        self._add_pull()

        def race(_request: collaboration.ContentPolicyRequest) -> bool:
            self.github.pulls[0] = replace(self.github.pulls[0], body="Concurrent human edit")
            return True

        self.facade._content_policy = race
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.metadata_update(
                91, self.git.head, "Task 194 requirement", "Initial requirements for the task.",
                "Proposed title", "Proposed body",
            )
        self.assertEqual("Concurrent human edit", self.github.pulls[0].body)
        self.assertEqual([], self.github.write_calls)

    def test_intent_callback_mutating_local_facts_is_detected_before_external_write(self) -> None:
        original_head = self.git.head

        def racing_intent(_intent: collaboration.WriteIntent) -> bool:
            self.intents.append(_intent)
            self.git.head = "b" * 40
            return True

        self.facade._record_intent = racing_intent
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.ensure_draft_pr()
        self.assertNotEqual(original_head, self.git.head)
        self.assertEqual([], self.github.write_calls)

    def test_snapshot_wrong_bindings_and_unretrievable_metadata_are_rejected(self) -> None:
        self._add_pull()
        wrong = self._snapshot(self.git.head, 91)
        wrong["view"]["repository"] = "other/widgets"
        with mock.patch.object(
            collaboration.task_view.TaskViewSnapshots, "read", return_value=wrong,
        ):
            with self.assertRaises(collaboration.TaskCollaborationError) as binding:
                self.facade.initial_checkpoint(91, self._binding(), "Initial report.")
        self.assertEqual("snapshot_binding_mismatch", binding.exception.code)
        self.assertEqual([], self.github.write_calls)

        with mock.patch.object(
            self.store, "confirm", side_effect=RuntimeError("unreachable metadata"),
        ):
            with self.assertRaises(collaboration.TaskCollaborationError) as unreachable:
                self.facade.initial_checkpoint(91, self._binding(), "Initial report.")
        self.assertEqual("metadata_not_remotely_retrievable", unreachable.exception.code)
        self.assertEqual([], self.github.write_calls)

    def test_snapshot_binding_and_checkpoint_capsule_are_remote_exact_and_retry_safe(self) -> None:
        self._add_pull()
        self.github.comment_loses_ack = True
        receipt = self.facade.initial_checkpoint(91, self._binding(), "Initial task facts.")
        self.assertEqual(201, receipt.comment_id)
        self.assertEqual(self.publication.record_id, receipt.record_id)
        self.assertEqual("e" * 64, receipt.snapshot_id)
        self.assertEqual(1, len(self.content_requests))
        post = self.github.comments[("pr", 91)][0]
        payload, digest = self.facade._decode_checkpoint(post.body)
        self.assertEqual("initial", payload["key"])
        self.assertEqual(self.git.head, payload["subject"])
        self.assertEqual(self.github.user_id, payload["principal"])
        self.assertEqual(64, len(digest))
        auth_bytes = self.authorization_requests[-1].request_bytes.decode("utf-8")
        self.assertIn(self.publication.metadata_commit, auth_bytes)
        self.assertIn("snapshot_id", auth_bytes)
        self.assertIn("report_digest", auth_bytes)

        count = len(self.github.write_calls)
        repeated = self.facade.initial_checkpoint(91, self._binding(), "Initial task facts.")
        self.assertEqual(201, repeated.comment_id)
        self.assertEqual(count, len(self.github.write_calls))
        with self.assertRaises(collaboration.TaskCollaborationError) as conflict:
            self.facade.initial_checkpoint(91, self._binding(), "Different initial facts.")
        self.assertEqual("checkpoint_key_conflict", conflict.exception.code)

    def test_checkpoint_capsule_rejects_duplicate_json_and_noncanonical_base64(self) -> None:
        self._add_pull()
        body, _digest = self.facade._render_checkpoint({
            "schema_version": 1, "repository": _REPOSITORY, "task": _TASK,
            "branch_ref": _BRANCH, "pr_number": 91, "subject": self.git.head,
            "metadata_commit": self.publication.metadata_commit,
            "record_id": self.publication.record_id, "contract_id": self.publication.contract_id,
            "snapshot_id": "e" * 64, "boundary": "initial", "key": "initial",
            "principal": self.github.user_id, "report": "Facts",
        })
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade._decode_checkpoint(body.replace("Capsule: ", "Capsule: !!", 1))
        import base64

        duplicate_keys = base64.b64encode(b'{"key":"first","key":"second"}').decode("ascii")
        bad = body.split("\n")
        bad[3] = "Capsule: " + duplicate_keys
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade._decode_checkpoint("\n".join(bad))
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade._decode_checkpoint(body + "\nextra")

    def test_checkpoint_actor_spoof_duplicate_order_and_principal_switch(self) -> None:
        self._add_pull()
        exact = self._initial_comment()
        body = self.github.comments[("pr", 91)][0].body
        # A copied marker from a different author is not this principal's
        # checkpoint; the current actor gets its own exact idempotency record.
        copied = collaboration.CommentFacts(_REPOSITORY, "pr", 91, 14, 999, body)
        self.github.comments[("pr", 91)].append(copied)
        self.github.user_id = 88
        second = self.facade.initial_checkpoint(91, self._binding(), "Initial task facts.")
        self.assertNotEqual(exact.comment_id, second.comment_id)
        owned = [item for item in self.github.comments[("pr", 91)] if item.author_id == 88]
        self.assertEqual(1, len(owned))

        # Under one current principal exact duplicates converge on the lowest
        # numeric ID; no comment body is overwritten.
        payload, _digest = self.facade._decode_checkpoint(owned[0].body)
        body_again, _digest_again = self.facade._render_checkpoint(payload)
        self.github.comments[("pr", 91)].extend([
            collaboration.CommentFacts(_REPOSITORY, "pr", 91, 44, 88, body_again),
            collaboration.CommentFacts(_REPOSITORY, "pr", 91, 33, 88, body_again),
        ])
        selected = self.facade._owned_checkpoint(
            self.facade._stable_comments("pr", 91), principal=88, key="initial", pr_number=91,
        )
        self.assertEqual(33, selected[0].comment_id)  # type: ignore[index]

    def test_turn_checkpoint_key_conflict_and_principal_provenance(self) -> None:
        self._add_pull()
        initial = self.facade.initial_checkpoint(91, self._binding(), "Initial task checkpoint.")
        turn_binding = self._binding("f" * 64)
        first = self.facade.turn_checkpoint(
            91, turn_binding, "turn-01", "Facts at end of turn.", boundary="explicit-handoff",
        )
        self.assertEqual(initial.comment_id + 1, first.comment_id)
        same = self.facade.turn_checkpoint(
            91, turn_binding, "turn-01", "Facts at end of turn.", boundary="explicit-handoff",
        )
        self.assertEqual(first.comment_id, same.comment_id)
        with self.assertRaises(collaboration.TaskCollaborationError) as conflict:
            self.facade.turn_checkpoint(
                91, turn_binding, "turn-01", "Changed report.", boundary="explicit-handoff",
            )
        self.assertEqual("checkpoint_key_conflict", conflict.exception.code)
        with self.assertRaises(collaboration.TaskCollaborationError) as reserved:
            self.facade.turn_checkpoint(91, self._binding(), "initial", "Facts")
        self.assertEqual("reserved_checkpoint_key", reserved.exception.code)

    def test_turn_checkpoint_requires_owned_initial_before_any_comment_write(self) -> None:
        self._add_pull()
        binding = self._binding()
        with self.assertRaises(collaboration.TaskCollaborationError) as required:
            self.facade.turn_checkpoint(
                91, binding, "turn-before-initial", "Turn report.",
                boundary="explicit-handoff",
            )
        self.assertEqual("initial_checkpoint_required", required.exception.code)
        self.assertEqual([], self.github.comments.get(("pr", 91), []))
        self.assertEqual([], self.github.write_calls)

    def test_turn_checkpoint_refuses_predecessor_change_after_snapshot_capture(self) -> None:
        self._add_pull()
        initial = self.facade.initial_checkpoint(91, self._binding(), "Initial checkpoint.")
        binding = self._binding("f" * 64)
        baseline_writes = len(self.github.write_calls)

        def race(_request: collaboration.ContentPolicyRequest) -> bool:
            payload = self.facade._checkpoint_payload(
                boundary="explicit-handoff",
                key="concurrent-turn",
                report="Concurrent checkpoint.",
                binding=binding,
                pr_number=91,
                subject=self.git.head,
                principal=self.github.user_id,
            )
            body, _digest = self.facade._render_checkpoint(payload)
            self.github.comments[("pr", 91)].append(collaboration.CommentFacts(
                _REPOSITORY, "pr", 91, 301, self.github.user_id, body,
            ))
            return True

        self.facade._content_policy = race
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.turn_checkpoint(
                91, binding, "turn-after-race", "This must not be posted.",
                boundary="explicit-handoff",
            )
        self.assertEqual(initial.comment_id, 201)
        self.assertEqual(baseline_writes, len(self.github.write_calls))
        self.assertEqual(2, len(self.github.comments[("pr", 91)]))

    def test_establish_creates_pr_before_provider_and_retry_skips_provider(self) -> None:
        calls: list[tuple[str, str, str, int, str]] = []

        def provider(repo: str, task: str, branch: str, number: int, subject: str) -> collaboration.MetadataBinding:
            self.assertEqual(1, len(self.github.pulls), "PR must be durable before metadata provider")
            calls.append((repo, task, branch, number, subject))
            return self._binding()

        first = self.facade.establish("Initial report.", provider)
        self.assertEqual(1, len(calls))
        self.assertEqual(91, first.pull_request)
        self.assertIsNotNone(first.comment_id)
        second = self.facade.establish("Initial report.", provider)
        self.assertEqual(1, len(calls), "retry must reuse the exact initial capsule")
        self.assertEqual(first.comment_id, second.comment_id)
        self.assertEqual(first.snapshot_id, second.snapshot_id)

    def test_metadata_update_then_establish_retry_adopts_same_pr_without_provider(self) -> None:
        provider_calls: list[int] = []

        def provider(_repo: str, _task: str, _branch: str, number: int, _subject: str) -> collaboration.MetadataBinding:
            provider_calls.append(number)
            return self._binding()

        initial = self.facade.establish("Initial stable report.", provider)
        self.facade.metadata_update(
            91, self.git.head, "Task 194 requirement", "Initial requirements for the task.",
            "Human-approved title", "Human-approved body\nwith a second paragraph.",
        )
        self.assertEqual("Human-approved body\nwith a second paragraph.", self.github.pulls[0].body)
        retried = self.facade.establish("Initial stable report.", provider)
        self.assertEqual(1, len(provider_calls))
        self.assertEqual(initial.pull_request, retried.pull_request)
        self.assertEqual(initial.comment_id, retried.comment_id)

    def test_pr_adoption_rechecks_identity_and_comment_adoption_detects_deletion(self) -> None:
        pull = self._add_pull(title="Human title", body="Human body\nthat is multiline.")
        # Existing PR prose is not task identity and is never overwritten by
        # adoption. The exact PR identity is read again before the receipt.
        self.assertEqual(pull.number, self.facade.draft_pr().pull_request)

        self.github.pull_page_calls = 0

        def delete_during_adoption(github: FakeGitHub, _page: int, calls: int) -> None:
            if calls == 2:
                github.pulls.clear()

        self.github.pull_page_hook = delete_during_adoption
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.draft_pr()
        self.github.pull_page_hook = None
        self.github.pulls[:] = [pull]

        self.github.comments[("pr", pull.number)] = []
        posted = self.facade.initial_checkpoint(pull.number, self._binding(), "Stable report.")
        self.assertIsNotNone(posted.comment_id)
        removed = False

        def delete_after_first_read(github: FakeGitHub, _kind: str, _number: int, _page: int, calls: int) -> None:
            nonlocal removed
            if calls == github.comments_page_calls and not removed:
                removed = True
                github.comments[("pr", pull.number)] = []

        self.github.comments_page_hook = delete_after_first_read
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.initial_checkpoint(pull.number, self._binding(), "Stable report.")

    def test_issue_comment_requires_exact_task_issue_and_current_pr(self) -> None:
        self._add_pull()
        receipt = self.facade.factual_comment("issue", int(_TASK), "issue-fact-1", "Observed a local test fact.")
        self.assertIsNone(receipt.pull_request)
        self.assertEqual(201, receipt.comment_id)
        self.assertEqual("issue", self.github.write_calls[-1].kind)
        self.assertIn("pull_request", self.authorization_requests[-1].intent_parameters.decode("utf-8"))

        with self.assertRaises(collaboration.TaskCollaborationError) as bad_task:
            self.facade.factual_comment("issue", 999, "issue-fact-2", "Another fact.")
        self.assertEqual("issue_binding_mismatch", bad_task.exception.code)
        self.github.issues[int(_TASK)] = replace(self.github.issues[int(_TASK)], repository="fork/widgets")
        with self.assertRaises(collaboration.TaskCollaborationError):
            self.facade.factual_comment("issue", int(_TASK), "issue-fact-3", "Another fact.")

    def test_factual_comment_idempotency_and_same_key_conflict(self) -> None:
        self._add_pull()
        first = self.facade.factual_comment("pr", 91, "fact-1", "The test fixture completed.")
        count = len(self.github.write_calls)
        again = self.facade.factual_comment("pr", 91, "fact-1", "The test fixture completed.")
        self.assertEqual(first.comment_id, again.comment_id)
        self.assertEqual(count, len(self.github.write_calls))
        with self.assertRaises(collaboration.TaskCollaborationError) as conflict:
            self.facade.factual_comment("pr", 91, "fact-1", "A different factual text.")
        self.assertEqual("comment_key_conflict", conflict.exception.code)

    def test_every_prose_write_requires_literal_host_content_approval(self) -> None:
        self.deny_content = True
        with self.assertRaises(collaboration.TaskCollaborationError) as denied:
            self.facade.ensure_draft_pr()
        self.assertEqual("content_policy_denied", denied.exception.code)
        self.assertEqual([], self.github.write_calls)
        self.assertEqual([], self.intents)
        self.assertTrue(self.content_requests)

    def test_no_unscoped_github_writer_or_ready_surface_is_exposed(self) -> None:
        syntax = ast.parse(Path(collaboration.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(syntax):
            if isinstance(node, ast.ClassDef):
                names = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
                self.assertEqual(len(names), len(set(names)), f"shadowed method in {node.name}")
        for name in ("request", "graphql", "merge", "close", "delete", "ready", "launch"):
            self.assertFalse(hasattr(self.facade, name), name)
        required = {
            "repository", "principal", "pull_page", "comments_page", "issue",
            "create_draft", "edit_metadata", "post_comment",
        }
        self.assertTrue(all(callable(getattr(collaboration.GitHubTransport, name)) for name in required))
        self.assertNotIn("ready", collaboration.OPERATIONS)
        self.assertNotIn("merge", collaboration.OPERATIONS)

    def test_external_acknowledgements_are_ignored_and_exact_postconditions_win(self) -> None:
        self.github.create_loses_ack = True
        created = self.facade.ensure_draft_pr()
        self.assertEqual(91, created.pull_request)
        self.assertEqual(1, len(self.github.pulls))

        self.github.comment_loses_ack = True
        comment = self.facade.factual_comment("pr", 91, "factual-ack", "The write was observed.")
        self.assertEqual(201, comment.comment_id)
        self.assertNotIn("lost acknowledgement", repr(comment))

    def test_hard_interruption_retries_reobserve_pr_and_checkpoint_without_duplicate(self) -> None:
        self.github.create_hard_interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.facade.ensure_draft_pr()
        self.assertEqual(1, len(self.github.pulls))
        self.github.create_hard_interrupt = False
        self.assertEqual(91, self.facade.ensure_draft_pr().pull_request)
        self.assertEqual(1, len(self.github.pulls))

        self.github.edit_hard_interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.facade.update_metadata(
                91, self.git.head, "Task 194 requirement", "Initial requirements for the task.",
                "Hard-interrupted title", "Hard-interrupted body\nsecond paragraph.",
            )
        self.github.edit_hard_interrupt = False
        edit_retry = self.facade.update_metadata(
            91, self.git.head, "Task 194 requirement", "Initial requirements for the task.",
            "Hard-interrupted title", "Hard-interrupted body\nsecond paragraph.",
        )
        self.assertEqual(91, edit_retry.pull_request)
        self.assertEqual(2, len(self.github.write_calls))

        self.github.comment_hard_interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.facade.initial_checkpoint(91, self._binding(), "Hard-interruption report.")
        self.assertEqual(1, len(self.github.comments[("pr", 91)]))
        self.github.comment_hard_interrupt = False
        receipt = self.facade.initial_checkpoint(91, self._binding(), "Hard-interruption report.")
        self.assertEqual(201, receipt.comment_id)
        self.assertEqual(1, len(self.github.comments[("pr", 91)]))

    def test_callback_exception_is_safely_mapped_and_has_no_secret_detail(self) -> None:
        self.facade._authorize = lambda _request: (_ for _ in ()).throw(RuntimeError("TOKEN=secret"))
        with self.assertRaises(collaboration.TaskCollaborationError) as failed:
            self.facade.ensure_draft_pr()
        self.assertEqual("execution_authorization_failed", failed.exception.code)
        self.assertNotIn("secret", str(failed.exception))
        self.assertEqual([], self.github.write_calls)


if __name__ == "__main__":
    unittest.main()
