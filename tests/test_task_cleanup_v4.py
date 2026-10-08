from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import operation_prerequisites as prerequisites  # noqa: E402
import cleanup_resources as resources  # noqa: E402
import evidence as evidence_module  # noqa: E402
import task_cleanup as cleanup  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as fixtures  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "198"
_BRANCH = "refs/heads/task-198"


class TaskCleanupV4Test(unittest.TestCase):
    """Destructive-path fixtures are confined to the test's temporary repo."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip
    _set_remote_tip = fixtures.MetadataRefTest._set_remote_tip

    def setUp(self) -> None:
        self._fixture_set_up()
        (self.product / "src").mkdir()
        (self.product / "src" / "base.txt").write_text("fixture tracked parent\n", encoding="utf-8")
        fixtures.git("add", "src/base.txt", cwd=self.product)
        fixtures.git("commit", "-m", "tracked parent for cleanup fixtures", cwd=self.product)
        self.head = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.base_head = self.head
        self.default_ref = fixtures.git("symbolic-ref", "--quiet", "HEAD", cwd=self.product).decode("utf-8").strip()
        fixtures.git("update-ref", _BRANCH, self.head, cwd=self.product)
        self.task_root = self.product / ".worktrees" / _TASK
        self.task_root.parent.mkdir(exist_ok=True)
        fixtures.git("worktree", "add", str(self.task_root), _BRANCH.removeprefix("refs/heads/"), cwd=self.product)
        self.records = task_record.TaskRecords(self.store, authorize=lambda _request: True)
        created = self.records.create(
            task=_TASK, branch_ref=_BRANCH, base_revision=self.head,
            title="Cleanup fixture", body="A temp-only cleanup test record.",
        )
        self.record = created
        fixtures.git("push", "origin", f"{_BRANCH}:{_BRANCH}", cwd=self.product)
        self.journal: dict[str, cleanup.RecordedCleanupIntent] = {}
        self.authorizations: list[cleanup.CleanupAuthorizationRequest] = []
        self.worktree_calls: list[cleanup.WorktreeRemovalRequest] = []
        self.branch_calls: list[cleanup.BranchRemovalRequest] = []
        self.ephemeral_calls: list[cleanup.EphemeralRemovalRequest] = []
        self.branch_action = "ack_false"

    @staticmethod
    def _engine(operation: str) -> prerequisites.OperationPrerequisites:
        policy = prerequisites.OperationPolicy(
            operation, "cleanup_fixture", "execution",
            (prerequisites.CheckSpec("fixture_state", "fixture_host"),),
        )

        def check(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            return prerequisites.CheckResult(
                context.binding, "SATISFIED", "fixture_state_verified", {}, {},
            )

        return prerequisites.OperationPrerequisites(policy, checks={"fixture_state": check})

    def _core(
        self,
        *,
        authorize: Any = None,
        branch_action: str | None = None,
        classifier: Any = None,
        retention_reader: Any = None,
        worktree_action: Any = None,
        ephemeral_action: Any = None,
        qualification_flags: dict[str, bool] | None = None,
        install_worktree: bool = True,
        install_branch: bool = True,
    ) -> cleanup.CoreTaskCleanup:
        if branch_action is not None:
            self.branch_action = branch_action
        common = Path(fixtures.git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.product,
        ).decode("utf-8").strip())
        common_identity = cleanup._identity_path(common)

        def qualification(operation: str) -> cleanup.CleanupQualification:
            # Fixture-only descriptor values let the facade exercise its
            # binding checks. They are not a production atomic-custody proof.
            flags = {} if qualification_flags is None else qualification_flags
            return cleanup.CleanupQualification(
                operation=operation,
                qualification_id=("a" if operation == "worktree" else "b" if operation == "branch" else "c") * 64,
                repository=_REPOSITORY,
                common_directory=str(common),
                common_identity=common_identity,
                default_branch_ref=self.default_ref,
                worktree_namespace=str(self.product / ".worktrees"),
                descriptor_pinned=flags.get("descriptor_pinned", True),
                atomic_identity=flags.get("atomic_identity", True),
                atomic_custody=flags.get("atomic_custody", True),
                current_authorization=flags.get("current_authorization", True),
                expected_oid_compare_exchange=flags.get("expected_oid_compare_exchange", True),
                preserves_product_data=flags.get("preserves_product_data", True),
                no_force=flags.get("no_force", True),
                no_remote_refs=flags.get("no_remote_refs", True),
            )

        def remove_worktree(request: cleanup.WorktreeRemovalRequest) -> object:
            self.worktree_calls.append(request)
            if worktree_action is not None:
                return worktree_action(request)
            # Independent deterministic entry check for the temporary fixture.
            # This check plus the following native Git command is not atomic
            # and is not a production custody/qualification proof.
            inspector = resources.TaskResourceInspector(request.spec)
            self.assertEqual(request.inventory, inspector.revalidate(request.inventory))
            self.assertEqual(request.intent.target_head, self._branch_oid())
            self.assertIn(request.authorization, self.authorizations)
            self.assertEqual("worktree", request.authorization.step)
            fixtures.git("worktree", "remove", str(request.spec.worktree_root), cwd=self.product)
            # Returning False confirms that the facade verifies postconditions
            # rather than treating this fixture backend result as a receipt.
            return False

        def remove_branch(request: cleanup.BranchRemovalRequest) -> object:
            self.branch_calls.append(request)
            self.assertIn(request.authorization, self.authorizations)
            self.assertEqual("branch", request.authorization.step)
            self.assertFalse(Path(request.intent.worktree_root).exists())
            self.assertEqual(request.expected_head, self._branch_oid())
            if self.branch_action == "raise_before":
                raise RuntimeError("lost before local ref transaction")
            fixtures.git(
                "update-ref", "--no-deref", "-d", request.branch_ref,
                request.expected_head, cwd=self.product,
            )
            if self.branch_action == "apply_then_raise":
                raise RuntimeError("local ref applied before lost acknowledgement")
            return False

        capabilities: dict[str, cleanup.QualifiedDeletionCapability] = {}
        if install_worktree:
            capabilities["worktree"] = cleanup.QualifiedDeletionCapability(
                "worktree", qualification("worktree"), apply=remove_worktree,
            )
        if install_branch:
            capabilities["branch"] = cleanup.QualifiedDeletionCapability(
                "branch", qualification("branch"), apply=remove_branch,
            )
        if ephemeral_action is not None:
            capabilities["ephemeral"] = cleanup.QualifiedDeletionCapability(
                "ephemeral", qualification("ephemeral"), apply=ephemeral_action,
            )

        def record_intent(intent: cleanup.CleanupIntent) -> cleanup.RecordedCleanupIntent:
            recorded = cleanup.RecordedCleanupIntent(
                f"fixture-intent:{intent.intent_id}", intent.intent_id, intent,
            )
            self.journal[recorded.reference] = recorded
            return recorded

        def load_intent(reference: str) -> cleanup.RecordedCleanupIntent:
            return self.journal[reference]

        def fixture_retention_reader(request: cleanup.TaskRetentionRequest) -> cleanup.TaskRetentionFacts:
            self.assertEqual((_REPOSITORY, _TASK, _BRANCH, self.head), (
                request.repository, request.task, request.branch_ref, request.expected_head,
            ))
            return cleanup.TaskRetentionFacts(
                request.repository, request.task, request.branch_ref,
                request.expected_head, request.expected_head, None,
            )

        authorization_policy = authorize

        def authorize_request(request: cleanup.CleanupAuthorizationRequest) -> object:
            self.authorizations.append(request)
            # Preserve the caller's host policy while retaining an auditable
            # exact immutable request for assertions.
            return True if authorization_policy is None else authorization_policy(request)

        return cleanup.CoreTaskCleanup(
            self.store,
            host_prerequisites={
                "cleanup.worktree": self._engine("cleanup.worktree"),
                "cleanup.branch": self._engine("cleanup.branch"),
                "cleanup.ephemeral": self._engine("cleanup.ephemeral"),
            },
            host_authorizer=authorize_request,
            resource_classifier=(lambda _request: ()) if classifier is None else classifier,
            record_intent=record_intent,
            load_intent=load_intent,
            qualified_deletion_capabilities=capabilities,
            retention_reader=fixture_retention_reader if retention_reader is None else retention_reader,
        )

    def _set_disposition(self, kind: str) -> None:
        disposition = (
            {"kind": "cancelled"}
            if kind == "cancelled"
            else {
                "kind": "superseded",
                "replacement": {"repository": _REPOSITORY, "task": "199"},
            }
        )
        self.record = self.records.set_disposition(
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=self.base_head,
            expected_record_id=self.record.record_id,
            disposition=disposition,
        )

    def _commit_task_change(self, content: str) -> str:
        (self.task_root / "tracked.txt").write_text(content, encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.task_root)
        fixtures.git("commit", "-m", "temporary task-only fixture commit", cwd=self.task_root)
        self.head = fixtures.git("rev-parse", "HEAD", cwd=self.task_root).decode("ascii").strip()
        return self.head

    def _push_task_head(self) -> None:
        fixtures.git("push", "origin", f"{_BRANCH}:{_BRANCH}", cwd=self.product)

    def _commit_on_main_with_task_tree(self) -> str:
        (self.product / "tracked.txt").write_bytes((self.task_root / "tracked.txt").read_bytes())
        fixtures.git("add", "tracked.txt", cwd=self.product)
        fixtures.git("commit", "-m", "temporary main retention fixture", cwd=self.product)
        return fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()

    def _merged_pr_facts(
        self,
        request: cleanup.TaskRetentionRequest,
        *,
        merge_commit: str,
    ) -> cleanup.TaskRetentionFacts:
        return cleanup.TaskRetentionFacts(
            request.repository, request.task, request.branch_ref, request.expected_head,
            None,
            cleanup.TaskPullRequestFacts(
                request.repository, request.repository, 233, "merged", True,
                request.default_branch_ref.removeprefix("refs/heads/"),
                request.branch_ref.removeprefix("refs/heads/"), request.expected_head,
                merge_commit,
            ),
        )

    def _ephemeral_registrar(self, scope: str = "runtime") -> tuple[Any, cleanup.OpaqueResourceReference]:
        owner = "fixture-ephemeral-owner"
        proof_id = "d" * 64
        binding_id = "e" * 64
        admin = fixtures.git("rev-parse", "--absolute-git-dir", cwd=self.task_root).decode().strip()
        spec = resources.TaskRootSpec(
            _REPOSITORY, _TASK, _BRANCH, self.head, str(self.product), str(self.task_root), admin,
        )

        class Registrar:
            def classify(inner_self, request: cleanup.ResourceClassificationRequest) -> tuple[cleanup.DisposableResourceProof, ...]:
                manifest = tuple(
                    node for node in request.inventory.nodes
                    if node.relative_path == scope or node.relative_path.startswith(scope + "/")
                )
                return (cleanup.DisposableResourceProof(
                    request.repository, request.task, request.branch_ref, request.head,
                    request.inventory.inventory_id, scope, "ephemeral_dir", owner,
                    proof_id, manifest,
                ),)

            def resolve_resource(
                inner_self,
                reference: cleanup.OpaqueResourceReference,
                context: dict[str, Any],
            ) -> cleanup.OwnedResourceBinding:
                # The registrar observes its registered scope independently;
                # resolution must not depend on classifier-owned cached state.
                inventory = resources.TaskResourceInspector(spec).inventory()
                if inventory.inventory_id != context["inventory_id"]:
                    raise RuntimeError("fixture registry observation moved")
                manifest = tuple(
                    node for node in inventory.nodes
                    if node.relative_path == scope or node.relative_path.startswith(scope + "/")
                )
                return cleanup.OwnedResourceBinding(
                    context["repository"], context["task"], context["branch_ref"],
                    context["head"], context["inventory_id"], scope,
                    "ephemeral_dir", reference.owner, manifest, binding_id,
                )

        return Registrar(), cleanup.OpaqueResourceReference(owner, "registered-fixture-token")

    def _fixture_ephemeral_remover(self, action: str = "remove") -> Any:
        """Deterministic temp-fixture model, not a production atomicity proof.

        This callback performs ordinary path removals only inside this test's
        TemporaryDirectory. It demonstrates exact-manifest ordering and
        read-back behavior, not race-free custody or production qualification.
        """
        def remove(request: cleanup.EphemeralRemovalRequest) -> object:
            self.ephemeral_calls.append(request)
            self.assertEqual(request.intent.ephemeral_scope, request.relative_scope)
            self.assertIn(request.authorization, self.authorizations)
            self.assertEqual("ephemeral", request.authorization.step)
            self.assertEqual(request.intent.target_head, self._branch_oid())
            inspector = resources.TaskResourceInspector(request.spec)
            self.assertEqual(request.inventory, inspector.revalidate(request.inventory))
            exact_nodes = inspector.relative_nodes(request.inventory, request.relative_scope)
            self.assertEqual(request.expected_nodes, exact_nodes)
            if action == "partial":
                file_node = next(node for node in exact_nodes if node.kind == "file")
                (Path(request.spec.worktree_root) / file_node.relative_path).unlink()
                raise RuntimeError("fixture lost acknowledgement after partial removal")
            for node in sorted(exact_nodes, key=lambda item: (item.relative_path.count("/"), item.relative_path), reverse=True):
                path = Path(request.spec.worktree_root) / node.relative_path
                current = path.lstat()
                self.assertEqual((node.identity.device, node.identity.inode), (current.st_dev, current.st_ino))
                self.assertEqual(node.identity.mode, current.st_mode)
                if node.kind == "file":
                    path.unlink()
                else:
                    path.rmdir()
            if action == "apply_then_raise":
                raise RuntimeError("fixture lost acknowledgement after exact removal")
            return False

        return remove

    def _branch_oid(self) -> str | None:
        result = self._git_dir(self._git_dir_path(), "rev-parse", "--verify", _BRANCH)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def _git_dir_path(self) -> Path:
        return self.product / ".git"

    def test_cleanup_removes_only_registered_temp_worktree_and_exact_local_branch(self) -> None:
        authority = self.records.read(
            self.store.fetch_tip(), task=_TASK, base_revision=self.base_head, branch_ref=_BRANCH,
        )
        self.assertIsNotNone(authority)
        assert authority is not None
        self.assertNotIn("disposition", authority[1]["payload"])
        core = self._core()

        acknowledgement = core.cleanup(_TASK)

        self.assertTrue(acknowledgement.worktree_root_absent)
        self.assertTrue(acknowledgement.git_admin_absent)
        self.assertTrue(acknowledgement.task_branch_absent)
        self.assertTrue(acknowledgement.retained_head_exists)
        self.assertTrue(acknowledgement.canonical_record_preserved)
        self.assertFalse(self.task_root.exists())
        self.assertEqual(self.head, fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip())
        self.assertEqual(1, len(self.worktree_calls))
        self.assertEqual(1, len(self.branch_calls))
        self.assertEqual(["cleanup.worktree", "cleanup.branch"], [item.operation for item in self.authorizations])
        self.assertIsNotNone(acknowledgement.intent_reference)
        remote_branch = self._git_dir(
            self.remote_path, "show-ref", "--verify", "--hash", _BRANCH,
        )
        self.assertEqual(self.head, remote_branch.stdout.decode("ascii").strip())
        resolved = self.records.read(
            self.store.fetch_tip(), task=_TASK, base_revision=self.head, branch_ref=_BRANCH,
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(self.record.record_id, resolved[0])
        self.assertEqual(self.record.contract_id, resolved[1]["payload"]["contract_id"])
        self.assertEqual("contract", resolved[2]["kind"])
        self.assertEqual(b"commit\n", fixtures.git("cat-file", "-t", self.head, cwd=self.product))

    def test_unknown_untracked_content_requires_typed_owner_proof_and_is_preserved(self) -> None:
        mystery = self.task_root / "unknown-private-output.bin"
        mystery.write_bytes(b"must not be silently discarded")
        admin = Path(fixtures.git(
            "rev-parse", "--absolute-git-dir", cwd=self.task_root,
        ).decode("utf-8").strip())
        (admin / "info").mkdir(exist_ok=True)
        with (admin / "info" / "exclude").open("a", encoding="utf-8") as stream:
            stream.write("ignored.cache\n")
        ignored = self.task_root / "ignored.cache"
        ignored.write_bytes(b"ignored is still user data")
        core = self._core()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("unknown_content_requires_human_decision", raised.exception.code)
        self.assertEqual(b"must not be silently discarded", mystery.read_bytes())
        self.assertEqual(b"ignored is still user data", ignored.read_bytes())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_typed_disposable_proof_can_cover_ignored_temp_fixture_content(self) -> None:
        admin = Path(fixtures.git(
            "rev-parse", "--absolute-git-dir", cwd=self.task_root,
        ).decode("utf-8").strip())
        (admin / "info").mkdir(exist_ok=True)
        with (admin / "info" / "exclude").open("a", encoding="utf-8") as stream:
            stream.write("runtime/ignored.cache\n")
        ignored = self.task_root / "runtime" / "ignored.cache"
        ignored.parent.mkdir()
        ignored.write_bytes(b"registered disposable fixture bytes")
        registrar, _reference = self._ephemeral_registrar("runtime")

        def fixture_remove_proven_unknown(request: cleanup.WorktreeRemovalRequest) -> object:
            # Deterministic fixture-only inventory validation and exact known
            # scope removal. This is deliberately not an atomic production
            # filesystem primitive and is never an Agent-facing fallback.
            inspector = resources.TaskResourceInspector(request.spec)
            self.assertEqual(request.inventory, inspector.revalidate(request.inventory))
            self.assertEqual(1, len(request.proofs))
            for node in sorted(
                request.proofs[0].node_manifest,
                key=lambda item: (item.relative_path.count("/"), item.relative_path),
                reverse=True,
            ):
                path = Path(request.spec.worktree_root) / node.relative_path
                current = path.lstat()
                self.assertEqual((node.identity.device, node.identity.inode), (current.st_dev, current.st_ino))
                if node.kind == "file":
                    path.unlink()
                else:
                    path.rmdir()
            fixtures.git("worktree", "remove", str(request.spec.worktree_root), cwd=self.product)
            return False

        acknowledgement = self._core(
            classifier=registrar, worktree_action=fixture_remove_proven_unknown,
        ).cleanup(_TASK)

        self.assertTrue(acknowledgement.worktree_root_absent)
        self.assertTrue(acknowledgement.task_branch_absent)
        self.assertFalse(ignored.exists())
        self.assertEqual(1, len(self.worktree_calls))
        self.assertEqual(1, len(self.branch_calls))

    def test_main_fingerprint_excludes_only_exact_registered_worktree_root(self) -> None:
        sibling_like = self.product / ".worktrees" / "198-sibling"
        sibling_like.mkdir()
        payload = sibling_like / "do-not-hide-by-prefix.txt"
        payload.write_bytes(b"unregistered same-prefix content")
        core = self._core()

        acknowledgement = core.cleanup(_TASK)

        self.assertTrue(acknowledgement.worktree_root_absent)
        self.assertTrue(acknowledgement.task_branch_absent)
        self.assertEqual(b"unregistered same-prefix content", payload.read_bytes())
        self.assertTrue(sibling_like.is_dir())

    def test_registered_sibling_disappearance_is_not_hidden_by_status_filter(self) -> None:
        sibling = self.product / ".worktrees" / "199"
        fixtures.git("worktree", "add", "-b", "task-199", str(sibling), cwd=self.product)

        def collateral_fixture_effect(request: cleanup.WorktreeRemovalRequest) -> None:
            # Deliberately violate the host preservation contract in a temp
            # fixture. The facade must detect it and stop before branch removal.
            fixtures.git("worktree", "remove", str(request.spec.worktree_root), cwd=self.product)
            fixtures.git("worktree", "remove", str(sibling), cwd=self.product)

        core = self._core(worktree_action=collateral_fixture_effect)
        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)
        self.assertEqual("main_repository_state_changed", raised.exception.code)
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.branch_calls)
        self.assertIsNotNone(self.records.read(
            self.store.fetch_tip(), task=_TASK, base_revision=self.base_head, branch_ref=_BRANCH,
        ))

    def test_registered_sibling_is_preserved_on_success(self) -> None:
        sibling = self.product / ".worktrees" / "199"
        fixtures.git("worktree", "add", "-b", "task-199", str(sibling), cwd=self.product)
        payload = sibling / "human-unpublished.txt"
        payload.write_bytes(b"preserve sibling product work")
        sibling_admin = Path(fixtures.git("rev-parse", "--absolute-git-dir", cwd=sibling).decode().strip())
        sibling_inode = sibling.stat().st_ino
        result = self._core().cleanup(_TASK)
        self.assertTrue(result.task_branch_absent)
        self.assertEqual(sibling_inode, sibling.stat().st_ino)
        self.assertTrue(sibling_admin.is_dir())
        self.assertEqual(b"preserve sibling product work", payload.read_bytes())

    def test_locked_registration_after_intent_stops_before_backend(self) -> None:
        core = self._core()
        original_record = core._record_intent

        def lock_after_record(intent: cleanup.CleanupIntent) -> cleanup.RecordedCleanupIntent:
            result = original_record(intent)
            fixtures.git("worktree", "lock", str(self.task_root), cwd=self.product)
            return result

        core._record_intent = lock_after_record
        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)
        self.assertEqual("task_registry_state_changed", raised.exception.code)
        self.assertEqual([], self.worktree_calls)
        self.assertEqual(self.head, self._branch_oid())

    def test_malformed_typed_journal_intent_has_sanitized_error(self) -> None:
        core = self._core()
        recorded = core._record(core.plan(_TASK).intent)
        invalid = replace(recorded.intent, branch_ref="../private-value")
        self.journal[recorded.reference] = cleanup.RecordedCleanupIntent(
            recorded.reference, invalid.intent_id, invalid,
        )
        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.recover(recorded.reference)
        self.assertEqual("invalid_cleanup_intent", raised.exception.code)
        self.assertNotIn("private-value", str(raised.exception))
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_malformed_nested_journal_fields_are_validated_before_hashing(self) -> None:
        core = self._core()
        recorded = core._record(core.plan(_TASK).intent)
        for invalid in (
            replace(recorded.intent, nodes=(None,)),
            replace(recorded.intent, resource_reference="private-malformed-reference"),
        ):
            with self.subTest(field=invalid.nodes == (None,)):
                # Do not compute the invalid DTO's content hash in the fixture:
                # precisely that computation must be protected at load time.
                self.journal[recorded.reference] = cleanup.RecordedCleanupIntent(
                    recorded.reference, recorded.intent_id, invalid,
                )
                with self.assertRaises(cleanup.TaskCleanupError) as raised:
                    core.recover(recorded.reference)
                self.assertEqual("invalid_cleanup_intent", raised.exception.code)
                self.assertNotIn("private-malformed", str(raised.exception))
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_cleanup_preserves_published_evidence_snapshot_record_and_contract(self) -> None:
        evidence_id, evidence_bytes = evidence_module.encode_evidence(
            _REPOSITORY, _TASK, self.base_head,
            {
                "schema_version": 1,
                "kind": "fixture/cleanup-preservation",
                "producer": {"name": "temporary-fixture"},
                "created_at": "2025-01-02T03:04:05Z",
                "payload": {"marker": "immutable-evidence"},
            },
        )
        self.store.publish([evidence_bytes])
        snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
        snapshot = snapshots.capture(
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=self.base_head,
            boundary="turn-end",
            selected_evidence=(task_view.EvidenceRef(evidence_id, self.base_head),),
            default_branch_ref=self.default_ref,
        )
        self.assertEqual(snapshot.metadata_commit, self.store.fetch_tip())

        acknowledgement = self._core().cleanup(_TASK)

        self.assertTrue(acknowledgement.canonical_record_preserved)
        self.assertTrue(acknowledgement.retained_head_exists)
        current_tip = self.store.fetch_tip()
        self.assertIsNotNone(current_tip)
        self.assertEqual(snapshot.metadata_commit, current_tip)
        record = self.records.read(
            current_tip, task=_TASK, base_revision=self.base_head, branch_ref=_BRANCH,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(self.record.record_id, record[0])
        self.assertEqual(self.record.contract_id, record[1]["payload"]["contract_id"])
        persisted_evidence = evidence_module.Evidence(self.store).read(
            current_tip, evidence_id, task=_TASK, subject=self.base_head,
        )
        self.assertEqual("immutable-evidence", persisted_evidence["payload"]["payload"]["marker"])
        persisted_snapshot = snapshots.read(
            snapshot.metadata_commit, snapshot.snapshot_id,
            task=_TASK, subject=snapshot.subject,
        )
        self.assertEqual(snapshot.subject, persisted_snapshot["view"]["subject"])
        self.assertEqual("turn-end", persisted_snapshot["boundary"])
        self.assertEqual(evidence_id, persisted_snapshot["view"]["evidence"][0]["evidence_id"])

    def test_tracked_dirty_worktree_refuses_before_journaling_or_deletion(self) -> None:
        (self.task_root / "tracked.txt").write_text("unpublished human change\n", encoding="utf-8")
        core = self._core()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("task_worktree_dirty", raised.exception.code)
        self.assertEqual({}, self.journal)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())

    def test_host_authorization_denial_leaves_filesystem_and_branch_untouched(self) -> None:
        core = self._core(authorize=lambda _request: False)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("cleanup_authorization_denied", raised.exception.code)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)
        self.assertEqual(1, len(self.journal))

    def test_recovery_resumes_only_known_branch_step_after_worktree_removal(self) -> None:
        core = self._core(branch_action="raise_before")

        with self.assertRaises(cleanup.TaskCleanupError) as interrupted:
            core.cleanup(_TASK)

        self.assertEqual("branch_removal_unconfirmed", interrupted.exception.code)
        self.assertFalse(self.task_root.exists())
        self.assertEqual(self.head, self._branch_oid())
        reference = next(iter(self.journal))
        self.assertEqual(reference, interrupted.exception.intent_reference)

        self.branch_action = "apply_then_raise"
        acknowledgement = core.recover(reference)

        self.assertTrue(acknowledgement.worktree_root_absent)
        self.assertTrue(acknowledgement.task_branch_absent)
        self.assertTrue(acknowledgement.retained_head_exists)
        self.assertEqual(2, len(self.branch_calls))
        self.assertIsNone(self._branch_oid())
        self.assertEqual(reference, acknowledgement.intent_reference)

    def test_noncanonical_task_ids_are_rejected_without_host_callbacks(self) -> None:
        for invalid in ("0", "01", "+198", "198/child", True, 198):
            with self.subTest(task=invalid), self.assertRaises(cleanup.TaskCleanupError):
                cleanup._task_context(invalid)

    def test_staged_edit_is_refused_without_journaling_or_backend_calls(self) -> None:
        tracked = self.task_root / "tracked.txt"
        tracked.write_text("staged human decision\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.task_root)
        core = self._core()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("task_worktree_dirty", raised.exception.code)
        self.assertEqual({}, self.journal)
        self.assertTrue(tracked.exists())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_hidden_skip_worktree_index_flag_is_not_treated_as_clean(self) -> None:
        fixtures.git("update-index", "--skip-worktree", "tracked.txt", cwd=self.task_root)
        core = self._core()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("hidden_index_entry", raised.exception.code)
        self.assertEqual({}, self.journal)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)

    def test_task_only_unpublished_commit_is_not_removed(self) -> None:
        unpublished = self._commit_task_change("unpublished task-only commit\n")
        self.head = unpublished
        core = self._core()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("task_head_not_retained", raised.exception.code)
        self.assertEqual(unpublished, self._branch_oid())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_squash_like_tree_and_merged_pr_claim_do_not_prove_task_ancestry(self) -> None:
        task_head = self._commit_task_change("task tree for squash-like fixture\n")
        self.head = task_head
        self._push_task_head()
        squash_like_head = self._commit_on_main_with_task_tree()
        self.assertNotEqual(task_head, squash_like_head)
        fixtures.git("push", "origin", "--delete", _BRANCH, cwd=self.product)

        core = self._core(retention_reader=lambda request: self._merged_pr_facts(
            request, merge_commit=squash_like_head,
        ))
        authority = core._read_authority(_TASK)
        main = core._main_facts()

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core._retention(authority, main)

        self.assertEqual("task_head_not_merged", raised.exception.code)
        self.assertEqual(task_head, self._branch_oid())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

        mismatched_pr_core = self._core(retention_reader=lambda request: cleanup.TaskRetentionFacts(
            request.repository, request.task, request.branch_ref, request.expected_head,
            None,
            cleanup.TaskPullRequestFacts(
                "fork/widgets", request.repository, 233, "merged", True,
                request.default_branch_ref.removeprefix("refs/heads/"),
                request.branch_ref.removeprefix("refs/heads/"), request.expected_head,
                squash_like_head,
            ),
        ))
        with self.assertRaises(cleanup.TaskCleanupError) as mismatched:
            mismatched_pr_core._retention(
                mismatched_pr_core._read_authority(_TASK), mismatched_pr_core._main_facts(),
            )
        self.assertEqual("pull_request_binding_mismatch", mismatched.exception.code)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_auto_deleted_remote_branch_can_be_cleaned_only_with_actual_merge_ancestry(self) -> None:
        task_head = self._commit_task_change("merged task commit retained by main\n")
        self.head = task_head
        self._push_task_head()
        fixtures.git("merge", "--ff-only", _BRANCH, cwd=self.product)
        self.assertEqual(task_head, fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip())
        fixtures.git("push", "origin", "--delete", _BRANCH, cwd=self.product)

        core = self._core(retention_reader=lambda request: self._merged_pr_facts(
            request, merge_commit=task_head,
        ))
        acknowledgement = core.cleanup(_TASK)

        self.assertTrue(acknowledgement.worktree_root_absent)
        self.assertTrue(acknowledgement.task_branch_absent)
        self.assertTrue(acknowledgement.retained_head_exists)
        self.assertFalse(self.task_root.exists())
        self.assertEqual(task_head, fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip())
        self.assertEqual(b"", self._git_dir(self.remote_path, "show-ref", "--verify", _BRANCH).stdout)
        self.assertEqual(1, len(self.worktree_calls))
        self.assertEqual(1, len(self.branch_calls))

    def test_cancelled_or_superseded_record_does_not_authorize_unknown_data_disposal(self) -> None:
        unknown = self.task_root / "unowned-result.bin"
        unknown.write_bytes(b"must survive either disposition")
        core = self._core()

        for kind in ("cancelled", "superseded"):
            with self.subTest(disposition=kind):
                self._set_disposition(kind)
                with self.assertRaises(cleanup.TaskCleanupError) as raised:
                    core.cleanup(_TASK)
                self.assertEqual("unknown_content_requires_human_decision", raised.exception.code)
                self.assertEqual(b"must survive either disposition", unknown.read_bytes())
                self.assertTrue(self.task_root.is_dir())
                self.assertEqual([], self.worktree_calls)
                self.assertEqual([], self.branch_calls)

    def test_host_authorizer_requires_literal_true_not_truthy_or_dto_values(self) -> None:
        responses: tuple[Any, ...] = (False, None, 1, "yes", {"authorized": True})
        for response in responses:
            with self.subTest(response=type(response).__name__):
                core = self._core(authorize=lambda _request, value=response: value)
                with self.assertRaises(cleanup.TaskCleanupError) as raised:
                    core.cleanup(_TASK)
                self.assertEqual("cleanup_authorization_denied", raised.exception.code)
                self.assertTrue(self.task_root.is_dir())
                self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_late_retention_change_stops_after_worktree_and_preserves_branch(self) -> None:
        calls = 0
        remote_deleted = False

        def changing_reader(request: cleanup.TaskRetentionRequest) -> cleanup.TaskRetentionFacts:
            nonlocal calls, remote_deleted
            calls += 1
            if self.worktree_calls:
                if not remote_deleted:
                    fixtures.git("push", "origin", "--delete", _BRANCH, cwd=self.product)
                    remote_deleted = True
                return self._merged_pr_facts(request, merge_commit=self.head)
            return cleanup.TaskRetentionFacts(
                request.repository, request.task, request.branch_ref,
                request.expected_head, request.expected_head, None,
            )

        core = self._core(retention_reader=changing_reader)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("retention_facts_changed", raised.exception.code)
        self.assertEqual(1, len(self.worktree_calls))
        self.assertEqual([], self.branch_calls)
        self.assertFalse(self.task_root.exists())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual(b"", self._git_dir(self.remote_path, "show-ref", "--verify", _BRANCH).stdout)
        self.assertGreaterEqual(calls, 2)

    def test_ref_observation_failure_noncommit_and_symbolic_ref_are_not_absence(self) -> None:
        core = self._core()

        def unavailable(arguments: list[str], **_options: Any) -> bytes:
            if arguments[:1] == ["for-each-ref"]:
                raise cleanup.TaskCleanupError("git_observation_failed", "branch")
            raise AssertionError("unexpected command in direct-ref probe")

        with mock.patch.object(core, "_git", side_effect=unavailable):
            with self.assertRaises(cleanup.TaskCleanupError) as raised:
                core._branch_oid(_BRANCH)

        self.assertEqual("git_observation_failed", raised.exception.code)
        self.assertEqual(self.head, self._branch_oid())
        blob = fixtures.git(
            "hash-object", "-w", "--stdin", cwd=self.product,
            input_data=b"not a commit object",
        ).decode("ascii").strip()
        listing = f"{_BRANCH}\0{blob}\0blob\0\n".encode("ascii")

        with mock.patch.object(core, "_git", return_value=listing):
            with self.assertRaises(cleanup.TaskCleanupError) as raised:
                core._branch_oid(_BRANCH)

        self.assertEqual("task_ref_not_commit", raised.exception.code)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)
        fixtures.git("symbolic-ref", _BRANCH, self.default_ref, cwd=self.product)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core._branch_oid(_BRANCH)

        self.assertEqual("symbolic_task_ref", raised.exception.code)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)

    def test_authorizer_root_and_task_ref_mutations_are_revalidated_before_backend_entry(self) -> None:
        late_file = self.task_root / "arrived-during-authorization.bin"

        def mutate_root(request: cleanup.CleanupAuthorizationRequest) -> bool:
            if request.step == "worktree":
                late_file.write_bytes(b"late content is not in the intent")
            return True

        core = self._core(authorize=mutate_root)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("task_root_identity_changed", raised.exception.code)
        self.assertEqual(raised.exception.intent_reference, next(iter(self.journal)))
        self.assertEqual(b"late content is not in the intent", late_file.read_bytes())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)
        late_file.unlink()

        tree = fixtures.git("rev-parse", f"{self.head}^{{tree}}", cwd=self.product).decode("ascii").strip()
        replacement = fixtures.git(
            "commit-tree", tree, "-p", self.head, "-m", "same-tree replacement ref fixture",
            cwd=self.product,
        ).decode("ascii").strip()

        def move_ref(request: cleanup.CleanupAuthorizationRequest) -> bool:
            if request.step == "worktree":
                fixtures.git("update-ref", _BRANCH, replacement, self.head, cwd=self.product)
            return True

        core = self._core(authorize=move_ref)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertIn(raised.exception.code, {"cleanup_main_state_changed", "cleanup_intent_authority_changed", "task_branch_changed"})
        self.assertEqual(replacement, self._branch_oid())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)

    def test_false_or_missing_host_qualification_has_no_native_fallback(self) -> None:
        cases = (
            (self._core(qualification_flags={"atomic_custody": False}), "backend_qualification_mismatch"),
            (self._core(install_worktree=False), "qualified_deletion_backend_required"),
        )
        for core, expected_code in cases:
            with self.subTest(expected=expected_code), self.assertRaises(cleanup.TaskCleanupError) as raised:
                core.cleanup(_TASK)
            self.assertEqual(expected_code, raised.exception.code)
            self.assertTrue(self.task_root.is_dir())
            self.assertEqual([], self.worktree_calls)
            self.assertEqual([], self.branch_calls)

    def test_boolean_classifier_result_is_not_an_ownership_proof(self) -> None:
        unknown = self.task_root / "boolean-is-not-proof.bin"
        unknown.write_bytes(b"preserve")
        core = self._core(classifier=lambda _request: True)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup(_TASK)

        self.assertEqual("resource_classification_not_typed", raised.exception.code)
        self.assertEqual(b"preserve", unknown.read_bytes())
        self.assertEqual([], self.worktree_calls)

    def test_recovery_refuses_changed_task_ref(self) -> None:
        core = self._core(branch_action="raise_before")
        with self.assertRaises(cleanup.TaskCleanupError) as interrupted:
            core.cleanup(_TASK)
        reference = interrupted.exception.intent_reference
        self.assertIsNotNone(reference)
        assert reference is not None
        tree = fixtures.git("rev-parse", f"{self.head}^{{tree}}", cwd=self.product).decode("ascii").strip()
        replacement = fixtures.git(
            "commit-tree", tree, "-p", self.head, "-m", "recovery replacement fixture",
            cwd=self.product,
        ).decode("ascii").strip()
        fixtures.git("update-ref", _BRANCH, replacement, self.head, cwd=self.product)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.recover(reference)

        self.assertIn(raised.exception.code, {"cleanup_main_state_changed", "cleanup_intent_authority_changed", "task_branch_changed"})
        self.assertEqual(reference, raised.exception.intent_reference)
        self.assertEqual(replacement, self._branch_oid())
        self.assertEqual(1, len(self.branch_calls))
        fixtures.git("update-ref", _BRANCH, self.head, replacement, cwd=self.product)
        prior = self.journal[reference]
        wrong_intent = replace(prior.intent, branch_ref="refs/heads/task-199")
        self.journal[reference] = cleanup.RecordedCleanupIntent(
            reference, wrong_intent.intent_id, wrong_intent,
        )

        with self.assertRaises(cleanup.TaskCleanupError) as wrong_binding:
            core.recover(reference)

        self.assertEqual("cleanup_intent_authority_changed", wrong_binding.exception.code)
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual(1, len(self.branch_calls))

    def test_recovery_refuses_a_recreated_worktree_root(self) -> None:
        core = self._core(branch_action="raise_before")
        with self.assertRaises(cleanup.TaskCleanupError) as interrupted:
            core.cleanup(_TASK)
        reference = interrupted.exception.intent_reference
        self.assertIsNotNone(reference)
        assert reference is not None
        fixtures.git(
            "worktree", "add", str(self.task_root), _BRANCH.removeprefix("refs/heads/"),
            cwd=self.product,
        )

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.recover(reference)

        self.assertEqual("task_root_identity_changed", raised.exception.code)
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual(self.head, self._branch_oid())
        self.assertEqual(1, len(self.branch_calls))

    def test_disposable_nested_resource_is_removed_by_exact_temp_fixture_manifest(self) -> None:
        resource_root = self.task_root / "runtime" / "state"
        resource_root.mkdir(parents=True)
        (resource_root / "session.json").write_text("fixture-only ephemeral state\n", encoding="utf-8")
        registrar, reference = self._ephemeral_registrar()
        core = self._core(
            classifier=registrar,
            ephemeral_action=self._fixture_ephemeral_remover("remove"),
        )

        acknowledgement = core.cleanup_ephemeral(_TASK, reference)

        self.assertTrue(acknowledgement.ephemeral_scope_absent)
        self.assertEqual("fixture-intent:" + acknowledgement.intent_id, acknowledgement.intent_reference)
        self.assertFalse(resource_root.exists())
        self.assertTrue((self.task_root / "tracked.txt").exists())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)
        self.assertEqual(1, len(self.ephemeral_calls))

    def test_ephemeral_scope_under_tracked_parent_preserves_parent_product_files(self) -> None:
        resource_root = self.task_root / "src" / "runtime"
        resource_root.mkdir()
        (resource_root / "session.json").write_text("nested fixture runtime state\n", encoding="utf-8")
        registrar, reference = self._ephemeral_registrar("src/runtime")
        core = self._core(
            classifier=registrar,
            ephemeral_action=self._fixture_ephemeral_remover("remove"),
        )

        acknowledgement = core.cleanup_ephemeral(_TASK, reference)

        self.assertTrue(acknowledgement.ephemeral_scope_absent)
        self.assertFalse(resource_root.exists())
        self.assertEqual(
            "fixture tracked parent\n",
            (self.task_root / "src" / "base.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.task_root / "tracked.txt").exists())
        self.assertEqual(1, len(self.ephemeral_calls))

    def test_ephemeral_lost_ack_is_recovered_from_exact_known_intent(self) -> None:
        resource_root = self.task_root / "runtime" / "state"
        resource_root.mkdir(parents=True)
        (resource_root / "session.json").write_text("fixture-only state\n", encoding="utf-8")
        registrar, reference = self._ephemeral_registrar()
        core = self._core(
            classifier=registrar,
            ephemeral_action=self._fixture_ephemeral_remover("apply_then_raise"),
        )

        with self.assertRaises(cleanup.TaskCleanupError) as interrupted:
            core.cleanup_ephemeral(_TASK, reference)

        self.assertEqual("ephemeral_removal_uncertain", interrupted.exception.code)
        journal_ref = interrupted.exception.intent_reference
        self.assertIsNotNone(journal_ref)
        assert journal_ref is not None
        self.assertFalse(resource_root.exists())

        acknowledgement = core.recover(journal_ref)

        self.assertTrue(acknowledgement.ephemeral_scope_absent)
        self.assertEqual(journal_ref, acknowledgement.intent_reference)
        self.assertEqual(1, len(self.ephemeral_calls))
        self.assertTrue((self.task_root / "tracked.txt").exists())

    def test_ephemeral_partial_removal_is_not_blindly_retried(self) -> None:
        resource_root = self.task_root / "runtime" / "state"
        resource_root.mkdir(parents=True)
        session = resource_root / "session.json"
        session.write_text("fixture-only state\n", encoding="utf-8")
        registrar, reference = self._ephemeral_registrar()
        core = self._core(
            classifier=registrar,
            ephemeral_action=self._fixture_ephemeral_remover("partial"),
        )

        with self.assertRaises(cleanup.TaskCleanupError) as interrupted:
            core.cleanup_ephemeral(_TASK, reference)
        journal_ref = interrupted.exception.intent_reference
        self.assertIsNotNone(journal_ref)
        assert journal_ref is not None
        self.assertFalse(session.exists())
        self.assertTrue(resource_root.is_dir())

        with self.assertRaises(cleanup.TaskCleanupError) as recovery:
            core.recover(journal_ref)

        self.assertEqual("ephemeral_partial_or_unexpected_change", recovery.exception.code)
        self.assertEqual(1, len(self.ephemeral_calls))
        self.assertTrue(resource_root.is_dir())
        self.assertTrue((self.task_root / "tracked.txt").exists())

    def test_fixture_backend_rejects_deterministic_finalname_swap(self) -> None:
        resource_root = self.task_root / "runtime" / "state"
        resource_root.mkdir(parents=True)
        session = resource_root / "session.json"
        session.write_text("original fixture bytes\n", encoding="utf-8")
        original = session.read_bytes()
        registrar, reference = self._ephemeral_registrar()

        def swap_then_check(request: cleanup.EphemeralRemovalRequest) -> object:
            self.ephemeral_calls.append(request)
            self.assertIn(request.authorization, self.authorizations)
            self.assertEqual("ephemeral", request.authorization.step)
            backup = session.with_name("session.held")
            session.rename(backup)
            session.write_bytes(original)
            try:
                swapped_inspector = resources.TaskResourceInspector(request.spec)
                with self.assertRaises(resources.safeResourceInspectionError):
                    swapped_inspector.revalidate(request.inventory)
            finally:
                session.unlink()
                backup.rename(session)
            raise RuntimeError("fixture final-name swap was rejected")

        core = self._core(classifier=registrar, ephemeral_action=swap_then_check)

        with self.assertRaises(cleanup.TaskCleanupError) as raised:
            core.cleanup_ephemeral(_TASK, reference)

        self.assertEqual("ephemeral_removal_uncertain", raised.exception.code)
        self.assertIsNotNone(raised.exception.intent_reference)
        self.assertEqual(original, session.read_bytes())
        self.assertTrue(self.task_root.is_dir())
        self.assertEqual([], self.worktree_calls)
        self.assertEqual([], self.branch_calls)
        self.assertEqual(1, len(self.ephemeral_calls))

    def test_ephemeral_registrar_cannot_escape_or_select_tracked_product_data(self) -> None:
        escape_registrar, escape_ref = self._ephemeral_registrar("../tracked.txt")
        escape_core = self._core(
            classifier=escape_registrar,
            ephemeral_action=self._fixture_ephemeral_remover("remove"),
        )
        with self.assertRaises(cleanup.TaskCleanupError) as escaped:
            escape_core.cleanup_ephemeral(_TASK, escape_ref)
        self.assertIn(escaped.exception.code, {"protected_resource_scope", "invalid_resource_scope"})
        self.assertTrue((self.task_root / "tracked.txt").exists())
        self.assertEqual([], self.ephemeral_calls)

        product_registrar, product_ref = self._ephemeral_registrar("tracked.txt")
        product_core = self._core(
            classifier=product_registrar,
            ephemeral_action=self._fixture_ephemeral_remover("remove"),
        )
        with self.assertRaises(cleanup.TaskCleanupError) as tracked:
            product_core.cleanup_ephemeral(_TASK, product_ref)
        self.assertEqual("resource_scope_contains_product_data", tracked.exception.code)
        self.assertTrue((self.task_root / "tracked.txt").exists())
        self.assertEqual([], self.ephemeral_calls)


if __name__ == "__main__":
    unittest.main()
