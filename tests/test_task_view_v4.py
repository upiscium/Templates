from __future__ import annotations

import base64
import copy
import os
import sys
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import evidence as evidence_module  # noqa: E402
import metadata_codec as codec  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as fixtures  # noqa: E402
from metadata_ref import MetadataStore  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "144"


class TaskViewV4Test(unittest.TestCase):
    """Focused #144 tests, reusing only the local #217 Git/metadata fixture."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip
    _create_remote_child = fixtures.MetadataRefTest._create_remote_child
    _create_unrelated_genesis = fixtures.MetadataRefTest._create_unrelated_genesis
    _transfer_fixture_commit = fixtures.MetadataRefTest._transfer_fixture_commit
    _set_remote_tip = fixtures.MetadataRefTest._set_remote_tip

    def setUp(self) -> None:
        self._fixture_set_up()
        self.base_revision = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.branch_ref = fixtures.git("symbolic-ref", "--quiet", "HEAD", cwd=self.product).decode("utf-8").strip()
        self.records = task_record.TaskRecords(self.store, authorize=lambda _request: True)
        self.record_publication = self.records.create(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            title="Private requirement title",
            body="Private requirement body must not enter the live view.",
        )
        self.reader = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: False)

    def _product_state(self) -> tuple[bytes, ...]:
        """Observe product HEAD/tree/index/status/refs/config without refreshing the index."""
        index_text = fixtures.git("rev-parse", "--git-path", "index", cwd=self.product).decode("utf-8").strip()
        index = Path(index_text)
        if not index.is_absolute():
            index = self.product / index
        return (
            fixtures.git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.product),
            fixtures.git("ls-files", "--stage", "--debug", cwd=self.product),
            fixtures.git("status", "--porcelain=v1", "--untracked-files=all", cwd=self.product,
                         extra_env={"GIT_OPTIONAL_LOCKS": "0"}),
            fixtures.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.product),
            fixtures.git("config", "--local", "--null", "--list", cwd=self.product),
            index.read_bytes() if index.exists() else b"<no-index>",
        )

    def _observe(self, **overrides: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "task": _TASK,
            "branch_ref": self.branch_ref,
            "base_revision": self.base_revision,
        }
        options.update(overrides)
        return task_view.observe_live_task(self.store, **options)

    def _github_observation(
        self,
        previous: task_view.PreviousCheckpoint | None = None,
        *,
        number: int = 233,
        state: str = "open",
        draft: bool = False,
    ) -> task_view.GitHubObservation:
        return task_view.GitHubObservation(
            task_view.GitHubPullRequestFacts(
                _REPOSITORY,
                _REPOSITORY,
                number,
                state,
                draft,
                "main",
                self.branch_ref.removeprefix("refs/heads/"),
                self.base_revision,
            ),
            previous,
        )

    def _snapshots(self, authorize=None) -> task_view.TaskViewSnapshots:
        return task_view.TaskViewSnapshots(
            self.store,
            authorize=(lambda _request: True) if authorize is None else authorize,
        )

    def _evidence(self, *, subject: str | None = None, marker: str = "opaque") -> tuple[str, bytes, str]:
        exact_subject = self.base_revision if subject is None else subject
        evidence_id, data = evidence_module.encode_evidence(
            _REPOSITORY,
            _TASK,
            exact_subject,
            {
                "schema_version": 1,
                "kind": "fixture/result-v1",
                "producer": {"name": "fixture"},
                "created_at": "2025-01-02T03:04:05Z",
                "payload": {"opaque": marker, "result": "PASS"},
            },
        )
        self.store.publish([data])
        return evidence_id, data, exact_subject

    def _advance_product_head(self, message: str = "advance task view subject") -> str:
        path = self.product / "tracked.txt"
        path.write_text(f"{message}\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        fixtures.git("commit", "-m", message, cwd=self.product)
        return fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()

    def test_live_view_reports_current_git_facts_default_relation_and_registered_worktrees(self) -> None:
        linked = self.temp_root / "registered-worktree"
        fixtures.git("worktree", "add", "--detach", str(linked), self.base_revision, cwd=self.product)
        self._transfer_fixture_commit(self.base_revision)
        fixtures.git("update-ref", self.branch_ref, self.base_revision, cwd=self.remote_path)
        before = self._product_state()

        view = self._observe(default_branch_ref=self.branch_ref, observe_remote_head=True)

        git_facts = view["git"]
        self.assertEqual(self.base_revision, view["subject"])
        self.assertEqual(self.base_revision, git_facts["head"])
        self.assertEqual(fixtures.git("rev-parse", "HEAD^{tree}", cwd=self.product).decode("ascii").strip(),
                         git_facts["tree"])
        self.assertEqual(self.branch_ref, git_facts["branch_ref"])
        self.assertTrue(git_facts["branch_matches_task"])
        self.assertEqual(0, sum(git_facts["status"].values()))
        self.assertEqual("observed", git_facts["default_branch"]["state"])
        self.assertEqual("equal", git_facts["default_branch"]["relation"])
        self.assertEqual("observed", git_facts["remote_head"]["state"])
        self.assertEqual(self.base_revision, git_facts["remote_head"]["head_oid"])
        paths = [base64.b64decode(item["path_b64"]).decode("utf-8") for item in git_facts["worktrees"]]
        self.assertIn(str(self.product.resolve()), paths)
        self.assertIn(str(linked.resolve()), paths)
        self.assertEqual(sorted(item["path_b64"] for item in git_facts["worktrees"]),
                         [item["path_b64"] for item in git_facts["worktrees"]])
        self.assertEqual(before, self._product_state())

    def test_dirty_status_is_read_only_and_reports_counts_without_paths(self) -> None:
        tracked = self.product / "tracked.txt"
        tracked.write_text("staged change\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        tracked.write_text("staged and unstaged change\n", encoding="utf-8")
        (self.product / "untracked private name.txt").write_text("untracked\n", encoding="utf-8")
        before = self._product_state()

        view = self._observe()

        status = view["git"]["status"]
        self.assertEqual({
            "index_changes", "worktree_changes", "untracked", "conflicts", "submodule_changes",
        }, set(status))
        self.assertGreater(sum(status.values()), 0)
        self.assertGreaterEqual(status["index_changes"], 1)
        self.assertGreaterEqual(status["worktree_changes"], 1)
        self.assertGreaterEqual(status["untracked"], 1)
        self.assertNotIn("untracked private name.txt", repr(view))
        self.assertEqual(before, self._product_state())

    def test_same_count_dirty_edits_have_no_content_identity(self) -> None:
        tracked = self.product / "tracked.txt"
        tracked.write_text("first dirty version\n", encoding="utf-8")
        first = self._observe()
        tracked.write_text("second dirty version with different bytes\n", encoding="utf-8")
        second = self._observe()
        self.assertEqual(first["subject"], second["subject"])
        self.assertEqual(first["git"]["tree"], second["git"]["tree"])
        self.assertEqual(first["git"]["status"], second["git"]["status"])
        self.assertNotIn("first dirty version", repr(first))
        self.assertNotIn("second dirty version", repr(second))

    def test_porcelain_submodule_counts_and_bare_worktree_head_are_explicit(self) -> None:
        oid = self.base_revision
        dirty_submodule = (
            f"1 .. S.M. 160000 160000 160000 {oid} {oid} nested-module\0".encode("ascii")
        )
        self.assertEqual({
            "index_changes": 0,
            "worktree_changes": 0,
            "untracked": 0,
            "conflicts": 0,
            "submodule_changes": 1,
        }, task_view._parse_status(dirty_submodule))
        bare = task_view._parse_worktrees(b"worktree /bare-repository\0bare\0\0", len(oid))
        self.assertEqual(1, len(bare))
        self.assertIsNone(bare[0]["head"])
        self.assertTrue(bare[0]["bare"])

    def test_default_ref_absence_is_explicit_and_tracking_refs_are_not_remote_observations(self) -> None:
        branch_name = self.branch_ref.removeprefix("refs/heads/")
        fixtures.git(
            "update-ref", f"refs/remotes/origin/{branch_name}", self.base_revision, cwd=self.product,
        )
        view = self._observe(default_branch_ref="refs/heads/not-created", observe_remote_head=True)
        self.assertEqual({"state": "unavailable", "error_code": "ref_absent"}, view["git"]["default_branch"])
        self.assertEqual({"state": "unavailable", "error_code": "ref_absent"}, view["git"]["remote_head"])
        self.assertEqual({"state": "not_requested"}, self._observe()["git"]["remote_head"])

    def test_explicit_evidence_is_sorted_exactly_bound_and_never_interpreted(self) -> None:
        first_id, _first_data, first_subject = self._evidence(marker="first secret result")
        second_id, _second_data, second_subject = self._evidence(marker="second opaque payload")
        before = self._product_state()

        view = self._observe(selected_evidence=(
            task_view.EvidenceRef(second_id, second_subject),
            task_view.EvidenceRef(first_id, first_subject),
        ))

        expected = sorted((first_id, second_id))
        self.assertEqual(expected, [item["evidence_id"] for item in view["evidence"]])
        self.assertTrue(all(item["matches_current_head"] for item in view["evidence"]))
        self.assertNotIn("PASS", repr(view))
        self.assertNotIn("secret result", repr(view))
        self.assertNotIn("opaque payload", repr(view))
        self.assertNotIn("Private requirement body", repr(view))
        self.assertEqual(before, self._product_state())
        with self.assertRaises(task_view.TaskViewError) as duplicate:
            self._observe(selected_evidence=(task_view.EvidenceRef(first_id, first_subject),
                                             task_view.EvidenceRef(first_id, first_subject)))
        self.assertEqual("duplicate_evidence_id", duplicate.exception.code)
        with self.assertRaises(task_view.TaskViewError):
            self._observe(selected_evidence=(task_view.EvidenceRef(first_id, "f" * len(first_subject)),))

    def test_stale_evidence_is_only_an_exact_subject_mismatch_not_a_result(self) -> None:
        evidence_id, _data, evidence_subject = self._evidence(marker="STALE-PASS")
        current_head = self._advance_product_head()
        view = self._observe(selected_evidence=(task_view.EvidenceRef(evidence_id, evidence_subject),))
        self.assertEqual(current_head, view["subject"])
        self.assertNotEqual(evidence_subject, view["subject"])
        self.assertEqual({
            "evidence_id": evidence_id,
            "subject": evidence_subject,
            "matches_current_head": False,
        }, view["evidence"][0])
        self.assertNotIn("STALE-PASS", repr(view))

    def test_historical_contract_base_is_not_rebound_to_current_head(self) -> None:
        original_base = self.base_revision
        new_head = self._advance_product_head("head moved after record")
        view = self._observe()
        self.assertEqual(new_head, view["subject"])
        self.assertEqual(original_base, view["authority"]["base_revision"])
        self.assertNotEqual(view["authority"]["base_revision"], view["subject"])
        self.assertEqual(self.record_publication.record_id, view["authority"]["record_id"])

    def test_missing_or_mismatched_task_authority_fails_closed(self) -> None:
        with self.assertRaises(task_view.TaskViewError) as absent:
            self._observe(task="145", branch_ref="refs/heads/issue-145")
        self.assertEqual("authority_absent", absent.exception.code)
        with self.assertRaises(task_view.TaskViewError):
            self._observe(branch_ref="refs/heads/not-the-task-branch")

    def test_github_pr_number_is_independent_and_closed_observation_preserves_local_authority(self) -> None:
        secret = "https://user:auth-token@example.invalid/private"

        def failing(_request):
            raise RuntimeError(secret)

        before = self._product_state()
        unavailable = self._observe(pr_number=233, github_reader=failing)
        self.assertEqual({"state": "unavailable", "error_code": "callback_failed"}, unavailable["github"])
        self.assertEqual({"state": "unavailable", "error_code": "callback_failed"},
                         unavailable["previous_checkpoint"])
        self.assertEqual("observed", unavailable["authority"]["state"])
        self.assertEqual(self.base_revision, unavailable["git"]["head"])
        self.assertNotIn(secret, repr(unavailable))
        self.assertEqual(before, self._product_state())

        calls = []

        def valid_reader(request):
            calls.append(request)
            return task_view.GitHubObservation(
                task_view.GitHubPullRequestFacts(
                    _REPOSITORY,
                    _REPOSITORY,
                    233,
                    "open",
                    False,
                    "main",
                    self.branch_ref.removeprefix("refs/heads/"),
                    self.base_revision,
                ),
            )

        observed = self._observe(pr_number=233, github_reader=valid_reader)
        self.assertEqual(1, len(calls))
        self.assertEqual(("144", 233, self.branch_ref),
                         (calls[0].task, calls[0].number, calls[0].branch_ref))
        self.assertEqual("observed", observed["github"]["state"])
        self.assertEqual(233, observed["github"]["pull_request"]["number"])
        self.assertEqual("open", observed["github"]["pull_request"]["state"])
        self.assertFalse(observed["github"]["pull_request"]["draft"])
        self.assertEqual({"state": "absent"}, observed["previous_checkpoint"])
        task_view.encode_snapshot(_REPOSITORY, _TASK, observed["subject"], "turn-end", observed)

        for state in ("closed", "merged"):
            accepted = self._observe(
                pr_number=233,
                github_reader=lambda _request, pr_state=state: task_view.GitHubObservation(
                    task_view.GitHubPullRequestFacts(
                        _REPOSITORY, _REPOSITORY, 233, pr_state, False, "main",
                        self.branch_ref.removeprefix("refs/heads/"), self.base_revision,
                    ),
                ),
            )
            self.assertEqual(state, accepted["github"]["pull_request"]["state"])

        mismatch = self._observe(
            pr_number=233,
            github_reader=lambda _request: task_view.GitHubObservation(
                task_view.GitHubPullRequestFacts(
                    _REPOSITORY, _REPOSITORY, 145, "open", False, "main",
                    self.branch_ref.removeprefix("refs/heads/"), self.base_revision,
                ),
            ),
        )
        self.assertEqual({"state": "unavailable", "error_code": "binding_mismatch"}, mismatch["github"])
        self.assertEqual({"state": "unavailable", "error_code": "binding_mismatch"},
                         mismatch["previous_checkpoint"])
        boolean_number = self._observe(
            pr_number=233,
            github_reader=lambda _request: task_view.GitHubObservation(
                task_view.GitHubPullRequestFacts(
                    _REPOSITORY, _REPOSITORY, True, "open", False, "main",
                    self.branch_ref.removeprefix("refs/heads/"), self.base_revision,
                ),
            ),
        )
        self.assertEqual({"state": "unavailable", "error_code": "binding_mismatch"},
                         boolean_number["github"])
        cross_repository = self._observe(
            pr_number=233,
            github_reader=lambda _request: task_view.GitHubObservation(
                task_view.GitHubPullRequestFacts(
                    _REPOSITORY, "fork/widgets", 233, "open", False, "main",
                    self.branch_ref.removeprefix("refs/heads/"), self.base_revision,
                ),
            ),
        )
        self.assertEqual({"state": "unavailable", "error_code": "binding_mismatch"},
                         cross_repository["github"])
        descriptive_dict = self._observe(pr_number=233, github_reader=lambda _request: {
            "pull_request": {}, "previous_checkpoint": None,
        })
        self.assertEqual({"state": "unavailable", "error_code": "invalid_facts"}, descriptive_dict["github"])
        self.assertEqual({"state": "unavailable", "error_code": "invalid_facts"},
                         descriptive_dict["previous_checkpoint"])
        self.assertEqual({"state": "not_requested"}, self._observe()["previous_checkpoint"])
        with self.assertRaises(task_view.TaskViewError):
            self._observe(github_reader=valid_reader)
        for bad_number in (None, True, 0, -1):
            with self.subTest(pr_number=bad_number), self.assertRaises(task_view.TaskViewError):
                self._observe(pr_number=bad_number, github_reader=valid_reader)
        with self.assertRaises(task_view.TaskViewError):
            self._observe(pr_number=233)

    def test_refs_prefix_branch_name_is_a_valid_github_observation(self) -> None:
        branch_ref = "refs/heads/refs/foo"
        request = task_view.GitHubPullRequestRequest(_REPOSITORY, _TASK, 233, branch_ref)
        facts = task_view.GitHubPullRequestFacts(
            _REPOSITORY, _REPOSITORY, 233, "open", False,
            "refs/main", "refs/foo", self.base_revision,
        )
        observed = task_view._validate_github_facts(
            facts, request=request, repository=_REPOSITORY,
            branch_ref=branch_ref, oid_length=len(self.base_revision),
        )
        self.assertEqual("observed", observed["state"])
        task_view._validate_github_projection(
            observed, len(self.base_revision), repository=_REPOSITORY,
            branch_ref=branch_ref,
        )

    def test_git_operational_error_is_not_reported_as_non_ancestry(self) -> None:
        original = task_view._git_output

        def error_on_merge_base(root, arguments, **options):
            if arguments[0] == "merge-base":
                raise task_view.TaskViewError("git_command_failed")
            return original(root, arguments, **options)

        with mock.patch.object(task_view, "_git_output", side_effect=error_on_merge_base):
            self.assertEqual(
                {"state": "unknown", "reason": "observation_failed"},
                task_view._commits_after(self.product, self.base_revision, self.base_revision),
            )
        # A real Git fatal exit (128), even with check=False, is not absence.
        with self.assertRaises(task_view.TaskViewError):
            task_view._git_output(self.product, ("cat-file", "-t", "f" * 40), check=False)

    def test_noop_snapshot_publish_still_rechecks_subject(self) -> None:
        view = self._observe()
        snapshot_id, data = task_view.encode_snapshot(
            _REPOSITORY, _TASK, view["subject"], "turn-end", view,
        )
        original_publish = self.store.publish
        intents = []

        def authorize(request):
            self.assertEqual(snapshot_id, request.snapshot_id)
            original_publish([data])
            return True

        def reuse_then_move(objects, **options):
            result = original_publish(objects, **options)
            self._advance_product_head("move after identical object reuse")
            return result

        with mock.patch.object(self.store, "publish", side_effect=reuse_then_move):
            with self.assertRaises(task_view.TaskViewError) as moved:
                self._snapshots(authorize).capture(
                    task=_TASK, branch_ref=self.branch_ref,
                    base_revision=self.base_revision, boundary="turn-end",
                    on_candidate=intents.append,
                )
        self.assertEqual("live_git_state_changed", moved.exception.code)
        self.assertEqual([], intents)

    def test_snapshot_schema_is_canonical_closed_and_bound_to_exact_subject(self) -> None:
        view = self._observe()
        snapshot_id, data = task_view.encode_snapshot(
            _REPOSITORY, _TASK, view["subject"], "turn-end", view,
        )
        self.assertEqual(
            (snapshot_id, data),
            task_view.encode_snapshot(_REPOSITORY, _TASK, view["subject"], "turn-end", view),
        )
        decoded = task_view.decode_snapshot(
            data,
            snapshot_id=snapshot_id,
            repository=_REPOSITORY,
            task=_TASK,
            subject=view["subject"],
        )
        self.assertEqual({"schema_version", "boundary", "view"}, set(decoded))
        self.assertEqual(view, decoded["view"])
        self.assertEqual("turn-end", decoded["boundary"])
        self.assertEqual(view["subject"], codec.decode_object(data)["subject"])
        self.assertNotIn("timestamp", repr(decoded).lower())
        self.assertNotIn("checkpoint_id", repr(decoded["view"]["authority"]))
        with self.assertRaises(task_view.TaskViewError):
            task_view.encode_snapshot(_REPOSITORY, _TASK, view["subject"], "checkpoint", view)

        payload = codec.decode_object(data)["payload"]
        malformed_payloads = []
        malformed_payloads.append({**payload, "extra": True})
        malformed_payloads.append({**payload, "schema_version": 2})
        malformed_payloads.append({**payload, "boundary": "future-checkpoint"})
        malformed_view = copy.deepcopy(payload["view"])
        malformed_view["future_checkpoint_requirement"] = True
        malformed_payloads.append({**payload, "view": malformed_view})
        malformed_view = copy.deepcopy(payload["view"])
        malformed_view["subject"] = "f" * len(view["subject"])
        malformed_payloads.append({**payload, "view": malformed_view})
        for malformed in malformed_payloads:
            with self.subTest(payload=malformed):
                forged_id, forged_data = codec.encode_object(
                    task_view.SNAPSHOT_KIND, _REPOSITORY, _TASK, view["subject"], malformed,
                )
                with self.assertRaises(task_view.TaskViewError):
                    task_view.decode_snapshot(
                        forged_data,
                        snapshot_id=forged_id,
                        repository=_REPOSITORY,
                        task=_TASK,
                        subject=view["subject"],
                    )
        with self.assertRaises(task_view.TaskViewError):
            task_view.decode_snapshot(data, snapshot_id=snapshot_id, repository=_REPOSITORY,
                                      task=_TASK, subject="f" * len(view["subject"]))
        with self.assertRaises(task_view.TaskViewError):
            task_view.decode_snapshot(data, snapshot_id="0" * 64, repository=_REPOSITORY,
                                      task=_TASK, subject=view["subject"])

    def test_snapshot_publishes_only_object_confirms_exact_receipt_and_reads_from_fresh_clone(self) -> None:
        evidence_id, _data, evidence_subject = self._evidence(marker="private evidence payload")
        before = self._product_state()
        requests: list[task_view.SnapshotAuthorizationRequest] = []

        def authorize(request):
            requests.append(request)
            self.assertEqual({
                "task", "repository", "subject", "snapshot_id", "record_id", "contract_id", "boundary",
            }, {field.name for field in fields(request)})
            return True

        publication = self._snapshots(authorize).capture(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            boundary="explicit-handoff",
            selected_evidence=(task_view.EvidenceRef(evidence_id, evidence_subject),),
            default_branch_ref=self.branch_ref,
        )
        self.assertEqual(1, len(requests))
        self.assertEqual(publication.snapshot_id, requests[0].snapshot_id)
        self.assertEqual(self.record_publication.record_id, requests[0].record_id)
        self.assertEqual("explicit-handoff", publication.boundary)
        self.assertEqual(publication.metadata_commit, self._remote_tip())
        self.assertEqual(before, self._product_state())

        saved = self.reader.read(
            publication.metadata_commit,
            publication.snapshot_id,
            task=_TASK,
            subject=publication.subject,
        )
        self.assertEqual("explicit-handoff", saved["boundary"])
        self.assertEqual(self.record_publication.record_id, saved["view"]["authority"]["record_id"])
        self.assertNotIn("private evidence payload", repr(saved))
        self.assertNotIn("Private requirement body", repr(saved))
        resolved = self.records.read(
            publication.metadata_commit,
            task=_TASK,
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(self.record_publication.record_id, resolved[0])

        clone = self.temp_root / "task-view-fresh-clone"
        fixtures.git("clone", "--no-checkout", str(self.product), str(clone))
        fixtures.git("remote", "set-url", "origin", str(self.remote_path), cwd=clone)
        clone_store = MetadataStore(clone, _REPOSITORY)
        clone_reader = task_view.TaskViewSnapshots(clone_store, authorize=lambda _request: False)
        clone_saved = clone_reader.read(
            publication.metadata_commit,
            publication.snapshot_id,
            task=_TASK,
            subject=publication.subject,
        )
        self.assertEqual(saved, clone_saved)

    def test_snapshot_subject_is_head_and_dirty_projection_contains_counts_only(self) -> None:
        (self.product / "tracked.txt").write_text("dirty but not snapshot content\n", encoding="utf-8")
        before = self._product_state()
        publication = self._snapshots().capture(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            boundary="turn-end",
        )
        saved = self.reader.read(
            publication.metadata_commit,
            publication.snapshot_id,
            task=_TASK,
            subject=self.base_revision,
        )
        self.assertEqual(self.base_revision, publication.subject)
        self.assertEqual(self.base_revision, saved["view"]["subject"])
        self.assertGreater(saved["view"]["git"]["status"]["worktree_changes"], 0)
        self.assertEqual({
            "index_changes", "worktree_changes", "untracked", "conflicts", "submodule_changes",
        }, set(saved["view"]["git"]["status"]))
        self.assertNotIn("dirty but not snapshot content", repr(saved))
        self.assertEqual(before, self._product_state())

    def test_authorization_requires_literal_true_is_frozen_and_errors_do_not_leak(self) -> None:
        for response in (False, None, 1, "yes", object()):
            with self.subTest(response=type(response).__name__):
                before = self._remote_tip()
                with self.assertRaises(task_view.TaskViewError) as raised:
                    self._snapshots(lambda _request, answer=response: answer).capture(
                        task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision,
                        boundary="turn-end",
                    )
                self.assertEqual("snapshot_authorization_denied", raised.exception.code)
                self.assertEqual(before, self._remote_tip())

        captured = []

        def frozen(request):
            captured.append(request)
            with self.assertRaises(FrozenInstanceError):
                request.snapshot_id = "0" * 64
            return True

        publication = self._snapshots(frozen).capture(
            task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
        )
        self.assertEqual(publication.snapshot_id, captured[0].snapshot_id)

        secret = "https://user:private-token@example.invalid"
        with self.assertRaises(task_view.TaskViewError) as failure:
            self._snapshots(lambda _request: (_ for _ in ()).throw(RuntimeError(secret))).capture(
                task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
            )
        self.assertEqual("snapshot_authorization_failed", failure.exception.code)
        self.assertNotIn(secret, str(failure.exception))

    def test_snapshot_refuses_head_and_tree_changes_during_authorization(self) -> None:
        before = self._remote_tip()

        def move_product(_request):
            self._advance_product_head("move during authorization")
            return True

        with self.assertRaises(task_view.TaskViewError) as raised:
            self._snapshots(move_product).capture(
                task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
            )
        self.assertEqual("live_git_state_changed", raised.exception.code)
        self.assertEqual(before, self._remote_tip())

    def test_snapshot_refuses_dirty_worktree_change_during_authorization(self) -> None:
        before = self._remote_tip()

        def dirty_product(_request):
            (self.product / "tracked.txt").write_text("dirty during authorization\n", encoding="utf-8")
            return True

        with self.assertRaises(task_view.TaskViewError) as raised:
            self._snapshots(dirty_product).capture(
                task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
            )
        self.assertEqual("live_git_state_changed", raised.exception.code)
        self.assertEqual(before, self._remote_tip())

    def test_snapshot_refuses_branch_ref_change_even_when_head_stays_fixed(self) -> None:
        other_branch = "refs/heads/task-view-race"
        fixtures.git("update-ref", other_branch, self.base_revision, cwd=self.product)
        before = self._remote_tip()

        def switch_branch(_request):
            fixtures.git("symbolic-ref", "HEAD", other_branch, cwd=self.product)
            return True

        with self.assertRaises(task_view.TaskViewError) as raised:
            self._snapshots(switch_branch).capture(
                task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
            )
        self.assertEqual("live_git_state_changed", raised.exception.code)
        self.assertEqual(before, self._remote_tip())
        self.assertEqual(self.base_revision, fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip())

    def test_on_candidate_rechecks_git_before_metadata_side_effect(self) -> None:
        original = task_view._observe_git
        calls = 0

        def move_at_final_check(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                self._advance_product_head("move at candidate seam")
            return original(*args, **kwargs)

        before = self._remote_tip()
        with mock.patch.object(task_view, "_observe_git", side_effect=move_at_final_check):
            with self.assertRaises(task_view.TaskViewError) as raised:
                self._snapshots().capture(
                    task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
                )
        self.assertEqual("live_git_state_changed", raised.exception.code)
        self.assertEqual(4, calls)
        self.assertEqual(before, self._remote_tip())

    def test_on_candidate_callback_is_bracketed_by_guards(self) -> None:
        before = self._remote_tip()
        intents: list[str] = []

        def record_and_move(candidate: str) -> None:
            intents.append(candidate)
            self._advance_product_head("move during candidate intent callback")

        with self.assertRaises(task_view.TaskViewError) as changed:
            self._snapshots().capture(
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                boundary="turn-end",
                on_candidate=record_and_move,
            )
        self.assertEqual("live_git_state_changed", changed.exception.code)
        self.assertEqual(1, len(intents))
        self.assertEqual(before, self._remote_tip())

    def test_candidate_intent_failure_aborts_without_leaking_exception_text(self) -> None:
        secret = "https://user:private-token@example.invalid"
        candidates: list[str] = []

        def broken_intent(candidate: str) -> None:
            candidates.append(candidate)
            raise RuntimeError(secret)

        before = self._remote_tip()
        with self.assertRaises(task_view.TaskViewError) as raised:
            self._snapshots().capture(
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                boundary="turn-end",
                on_candidate=broken_intent,
            )
        self.assertEqual("candidate_intent_failed", raised.exception.code)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(1, len(candidates))
        self.assertEqual(before, self._remote_tip())

    def test_github_change_before_persistence_refuses_snapshot(self) -> None:
        calls = 0

        def reader(_request):
            nonlocal calls
            calls += 1
            state = "open" if calls == 1 else "closed"
            return self._github_observation(state=state)

        before = self._remote_tip()
        with self.assertRaises(task_view.TaskViewError) as changed:
            self._snapshots().capture(
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                boundary="turn-end",
                pr_number=233,
                github_reader=reader,
            )
        self.assertEqual("external_observation_changed", changed.exception.code)
        self.assertEqual(2, calls)
        self.assertEqual(before, self._remote_tip())

    def test_post_confirm_previous_change_refuses_receipt_and_candidate_intent_is_preserved(self) -> None:
        previous_publication = self._snapshots().capture(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            boundary="turn-end",
        )
        previous = task_view.PreviousCheckpoint(
            "opaque previous id",
            previous_publication.subject,
            previous_publication.metadata_commit,
            previous_publication.snapshot_id,
        )
        calls = 0

        def reader(_request):
            nonlocal calls
            calls += 1
            return self._github_observation(previous=previous if calls == 5 else None)

        candidates: list[str] = []
        with self.assertRaises(task_view.TaskViewError) as changed:
            self._snapshots().capture(
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                boundary="explicit-handoff",
                pr_number=233,
                github_reader=reader,
                on_candidate=candidates.append,
            )
        self.assertEqual("external_observation_changed", changed.exception.code)
        self.assertEqual(5, calls)
        self.assertEqual(1, len(candidates))
        self.assertEqual(candidates[0], self._remote_tip())

    def test_snapshot_refuses_changed_authority_after_authorization(self) -> None:
        before = self._remote_tip()
        changed: list[task_record.Publication] = []

        def replace_authority(_request):
            changed.append(self.records.reauthorize(
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id=self.record_publication.record_id,
                title="Changed contract after authorization request",
                body="Must not be used to refresh the pending snapshot.",
            ))
            return True

        with self.assertRaises(task_view.TaskViewError) as raised:
            self._snapshots(replace_authority).capture(
                task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
            )
        self.assertEqual("authority_changed", raised.exception.code)
        self.assertEqual(1, len(changed))
        self.assertNotEqual(before, self._remote_tip())
        self.assertEqual(changed[0].metadata_commit, self._remote_tip())

    def test_previous_checkpoint_is_bound_and_commits_after_uses_actual_ancestry(self) -> None:
        first = self._snapshots().capture(
            task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
        )
        self._advance_product_head("one commit after snapshot")
        previous_input = task_view.PreviousCheckpoint(
            "opaque #194 checkpoint id", first.subject, first.metadata_commit, first.snapshot_id,
        )
        view = self._observe(
            pr_number=233,
            github_reader=lambda _request: self._github_observation(previous_input),
        )
        previous = view["previous_checkpoint"]
        self.assertEqual("observed", previous["state"])
        self.assertEqual("opaque #194 checkpoint id", previous["checkpoint_id"])
        self.assertEqual({"state": "observed", "count": 1}, previous["commits_after"])
        self.assertEqual(first.subject, previous["subject"])
        self.assertNotEqual(first.subject, view["subject"])
        old_snapshot = self.reader.read(
            first.metadata_commit, first.snapshot_id, task=_TASK, subject=first.subject,
        )
        self.assertEqual(first.subject, old_snapshot["view"]["subject"])
        self.assertNotEqual(view["subject"], old_snapshot["view"]["subject"])

        absent = "f" * len(view["subject"])
        self.assertEqual({"state": "unknown", "reason": "observation_failed"},
                         task_view._commits_after(self.product, absent, view["subject"]))
        unrelated = self._create_unrelated_genesis()
        self.assertEqual({"state": "unknown", "reason": "not_ancestor"},
                         task_view._commits_after(self.product, unrelated, view["subject"]))

    def test_previous_graph_traversal_is_iterative_cycle_checked_and_bounded(self) -> None:
        class Store:
            @contextmanager
            def validation_scope(self):
                yield

        store = Store()
        record_id = "1" * 64
        contract_id = "2" * 64
        commit = self.base_revision

        def node(snapshot_id: str, previous_id: str | None) -> dict[str, Any]:
            previous = {"state": "not_requested"}
            if previous_id is not None:
                previous = {
                    "state": "observed",
                    "checkpoint_id": "opaque",
                    "subject": self.base_revision,
                    "metadata_commit": commit,
                    "snapshot_id": previous_id,
                    "commits_after": {"state": "observed", "count": 0},
                }
            return {
                "task": _TASK,
                "evidence": [],
                "previous_checkpoint": previous,
                "authority": {
                    "metadata_commit": commit,
                    "base_revision": self.base_revision,
                    "branch_ref": self.branch_ref,
                    "record_id": record_id,
                    "contract_id": contract_id,
                    "disposition": None,
                },
            }

        resolved = (
            record_id,
            {"payload": {"contract_id": contract_id, "disposition": None}},
            {"kind": "contract"},
        )
        record_views = {f"{index:064x}": node(f"{index:064x}", f"{index + 1:064x}") for index in range(1, 66)}

        def prior_reader(_store, previous, _task):
            return {"view": record_views[previous.snapshot_id]}

        class EmptyEvidence:
            def __init__(self, _store):
                pass

            def read(self, *_args, **_kwargs):
                raise AssertionError("no Evidence refs in fake graph")

        with mock.patch.object(task_view, "_read_authority", return_value=resolved), \
             mock.patch.object(task_view, "_read_previous_snapshot_object", side_effect=prior_reader), \
             mock.patch.object(task_view.evidence_module, "Evidence", EmptyEvidence):
            with self.assertRaises(task_view.TaskViewError) as limit:
                task_view._validate_snapshot_graph(store, node("0" * 64, f"{1:064x}"), root_snapshot_id="0" * 64)
            self.assertEqual("previous_checkpoint_limit", limit.exception.code)

            with self.assertRaises(task_view.TaskViewError) as cycle:
                task_view._validate_snapshot_graph(store, node("0" * 64, "0" * 64), root_snapshot_id="0" * 64)
            self.assertEqual("previous_checkpoint_cycle", cycle.exception.code)

    def test_bad_previous_checkpoint_is_unavailable_without_blocking_live_facts(self) -> None:
        first = self._snapshots().capture(
            task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
        )
        for previous in (
            task_view.PreviousCheckpoint("opaque", "f" * len(first.subject), first.metadata_commit, first.snapshot_id),
            task_view.PreviousCheckpoint("opaque", first.subject, "f" * len(first.metadata_commit), first.snapshot_id),
            task_view.PreviousCheckpoint("opaque", first.subject, first.metadata_commit, "F" * 64),
        ):
            with self.subTest(previous=previous):
                view = self._observe(
                    pr_number=233,
                    github_reader=lambda _request, value=previous: self._github_observation(value),
                )
                self.assertEqual("observed", view["github"]["state"])
                self.assertEqual("observed", view["authority"]["state"])
                self.assertEqual("unavailable", view["previous_checkpoint"]["state"])

    def test_unsafe_filter_config_is_rejected_without_execution_and_fsmonitor_is_disabled(self) -> None:
        for key, value in (
            ("url.fake.pushInsteadOf", "origin"),
        ):
            with self.subTest(unsafe_config=key):
                fixtures.git("config", "--add", key, value, cwd=self.product)
                with self.assertRaises(task_view.TaskViewError) as unsafe:
                    self._observe()
                self.assertEqual("unsafe_git_config", unsafe.exception.code)
                fixtures.git("config", "--unset-all", key, cwd=self.product)
        for include_key in (b"include.path\0", b"includeif.gitdir:/repo.path\0"):
            with self.subTest(include_key=include_key), mock.patch.object(
                task_view, "_git_output", return_value=include_key,
            ), self.assertRaises(task_view.TaskViewError) as unsafe_include:
                task_view._check_git_configuration(self.product)
            self.assertEqual("unsafe_git_config", unsafe_include.exception.code)

        marker = self.temp_root / "filter-command-ran"
        filter_command = self.temp_root / "filter-command"
        filter_command.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        filter_command.chmod(0o755)
        fixtures.git("config", "filter.hostile.process", str(filter_command), cwd=self.product)
        with self.assertRaises(task_view.TaskViewError) as raised:
            self._observe()
        self.assertEqual("unsafe_git_config", raised.exception.code)
        self.assertFalse(marker.exists())

        fixtures.git("config", "--unset", "filter.hostile.process", cwd=self.product)
        fsmonitor_marker = self.temp_root / "fsmonitor-command-ran"
        fsmonitor = self.temp_root / "fsmonitor-command"
        fsmonitor.write_text(f"#!/bin/sh\ntouch '{fsmonitor_marker}'\n", encoding="utf-8")
        fsmonitor.chmod(0o755)
        fixtures.git("config", "core.fsmonitor", str(fsmonitor), cwd=self.product)
        self.assertEqual(self.base_revision, self._observe()["git"]["head"])
        self.assertFalse(fsmonitor_marker.exists())

    def test_nested_submodule_filter_never_executes_during_observation(self) -> None:
        child = self.product / "child"
        child.mkdir()
        fixtures.git("init", "-b", "main", cwd=child)
        fixtures.git("config", "user.name", "Submodule Fixture", cwd=child)
        fixtures.git("config", "user.email", "fixture@example.invalid", cwd=child)
        (child / "tracked.txt").write_text("initial\n", encoding="utf-8")
        (child / ".gitattributes").write_text("tracked.txt filter=hostile\n", encoding="utf-8")
        fixtures.git("add", ".", cwd=child)
        fixtures.git("commit", "-m", "trusted child fixture", cwd=child)
        child_head = fixtures.git("rev-parse", "HEAD", cwd=child).decode().strip()
        (self.product / ".gitmodules").write_text(
            '[submodule "child"]\n\tpath = child\n\turl = ./child\n', encoding="utf-8",
        )
        fixtures.git("add", ".gitmodules", cwd=self.product)
        fixtures.git("update-index", "--add", "--cacheinfo", f"160000,{child_head},child", cwd=self.product)
        fixtures.git("commit", "-m", "trusted superproject gitlink", cwd=self.product)
        marker = self.temp_root / "nested-filter-ran"
        command = self.temp_root / "nested-clean-filter"
        command.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n', encoding="utf-8")
        command.chmod(0o755)
        fixtures.git("config", "filter.hostile.clean", str(command), cwd=child)
        (child / "tracked.txt").write_text("dirty child\n", encoding="utf-8")
        view = self._observe()
        self.assertFalse(marker.exists())
        self.assertEqual("not_inspected", view["git"]["submodule_worktrees"])
        self.assertEqual(0, view["git"]["status"]["submodule_changes"])
        # A child modification is not falsely asserted absent: its worktree was
        # explicitly not inspected. The parent still reports staged gitlinks.
        fixtures.git("update-index", "--cacheinfo", f"160000,{self.base_revision},child", cwd=self.product)
        staged = self._observe()
        self.assertGreater(staged["git"]["staged_gitlinks"], 0)
        self.assertFalse(marker.exists())

    def test_git_environment_is_scrubbed_and_time_and_output_are_bounded(self) -> None:
        other = self.temp_root / "not-this-repository"
        other.mkdir()
        with mock.patch.dict(os.environ, {
            "GIT_DIR": str(other),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(self.temp_root),
        }):
            output = task_view._git_output(self.product, ("rev-parse", "--show-toplevel"))
        self.assertEqual(str(self.product.resolve()), output.decode("utf-8").strip())
        with self.assertRaises(task_view.TaskViewError) as output_limit:
            task_view._git_output(self.product, ("--version",), max_stdout=1)
        self.assertEqual("git_output_limit", output_limit.exception.code)

        real_popen = task_view.subprocess.Popen

        def sleep_process(_command, **options):
            return real_popen([sys.executable, "-c", "import time; time.sleep(2)"], **options)

        with mock.patch.object(task_view.subprocess, "Popen", side_effect=sleep_process):
            with self.assertRaises(task_view.TaskViewError) as timeout:
                task_view._git_output(self.product, ("rev-parse", "HEAD"), timeout=0.05)
        self.assertEqual("git_timeout", timeout.exception.code)

    def test_shallow_and_grafted_repositories_refuse_ancestry_claims(self) -> None:
        shallow_text = fixtures.git("rev-parse", "--git-path", "shallow", cwd=self.product).decode().strip()
        shallow_path = Path(shallow_text)
        if not shallow_path.is_absolute():
            shallow_path = self.product / shallow_path
        shallow_path.write_text(self.base_revision + "\n", encoding="ascii")
        with self.assertRaises(task_view.TaskViewError) as shallow:
            self._observe(default_branch_ref=self.branch_ref)
        self.assertEqual("incomplete_git_history", shallow.exception.code)
        shallow_path.unlink()

        grafts_text = fixtures.git("rev-parse", "--git-path", "info/grafts", cwd=self.product).decode().strip()
        grafts_path = Path(grafts_text)
        if not grafts_path.is_absolute():
            grafts_path = self.product / grafts_path
        grafts_path.parent.mkdir(parents=True, exist_ok=True)
        grafts_path.write_bytes(b"")
        with self.assertRaises(task_view.TaskViewError) as grafted:
            self._observe()
        self.assertEqual("incomplete_git_history", grafted.exception.code)

    def test_snapshot_reader_uses_exact_pinned_authority_and_has_no_legacy_reader(self) -> None:
        publication = self._snapshots().capture(
            task=_TASK, branch_ref=self.branch_ref, base_revision=self.base_revision, boundary="turn-end",
        )
        saved_before = self.reader.read(
            publication.metadata_commit, publication.snapshot_id,
            task=_TASK, subject=publication.subject,
        )
        changed = self.records.set_disposition(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            expected_record_id=self.record_publication.record_id,
            disposition={"kind": "cancelled"},
        )
        self.assertEqual(changed.metadata_commit, self._remote_tip())
        saved_after = self.reader.read(
            publication.metadata_commit, publication.snapshot_id,
            task=_TASK, subject=publication.subject,
        )
        self.assertEqual(saved_before, saved_after)
        self.assertIsNone(saved_after["view"]["authority"]["disposition"])
        self.assertFalse(hasattr(task_view, "TaskStateReader"))
        source = Path(task_view.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".task-state", source)


if __name__ == "__main__":
    unittest.main()
