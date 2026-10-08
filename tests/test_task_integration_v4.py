from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import task_integration as integration  # noqa: E402
import collaboration_git  # noqa: E402
import evidence  # noqa: E402
import integration_git  # noqa: E402
import operation_prerequisites as prerequisites  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as metadata_fixtures  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "195"
_REAL_PR = 827
_BRANCH = "refs/heads/task/195"
_DEFAULT = "refs/heads/main"
_HEAD = "a" * 40
_BASE = "b" * 40
_TREE = "c" * 40
_MERGE = "d" * 40


def subject(**changes) -> integration.IntegrationSubject:
    values = {
        "schema_version": 1,
        "repository": _REPOSITORY,
        "repository_id": 1234,
        "task": _TASK,
        "pull_request": 28,
        "head_ref": _BRANCH,
        "head_oid": _HEAD,
        "base_ref": _DEFAULT,
        "base_oid": _BASE,
        "default_branch_ref": _DEFAULT,
        "record_id": "1" * 64,
        "contract_id": "2" * 64,
        "merge_method": "merge",
        "expected_merge_tree_oid": _TREE,
        "plan_ref": "plan/opaque-1",
        "ready_reference_id": "3" * 64,
    }
    values.update(changes)
    return integration.IntegrationSubject(**values)


class _FakeGitHub:
    """Authenticated-state fixture with a server-side head/base compare."""

    def __init__(self, proposal: integration.IntegrationSubject) -> None:
        self.repository_facts = integration.RepositoryFacts(
            _REPOSITORY, proposal.repository_id, _DEFAULT, ("merge", "squash"),
        )
        self.pull_facts = integration.PullFacts(
            proposal.pull_request, _REPOSITORY, _REPOSITORY, "main", "task/195",
            proposal.head_oid, proposal.base_oid, "open", False,
        )
        self.default = proposal.base_oid
        self.commits: dict[str, integration.GitHubCommitFacts] = {}
        self.merge_calls: list[integration.MergeIntent] = []
        self.applied_merges = 0
        self.fail_after_apply = False
        self.revoke_human_at_commit = False
        self.ready_current: dict[str, integration.ReadyBinding] = {}
        self.human_current: dict[str, integration.HumanApprovalBinding] = {}
        self.capabilities = integration.MergeTransportCapabilities(True, True, True, True)

    def repository(self):
        return self.repository_facts

    def principal(self):
        return 17

    def pull(self, number: int):
        if number != self.pull_facts.number:
            raise AssertionError("fixture was asked for another PR")
        return self.pull_facts

    def default_head(self):
        return self.default

    def merge_capabilities(self):
        return self.capabilities

    def commit(self, oid: str):
        return self.commits[oid]

    def is_default_ancestor(self, old: str, new: str):
        return old == new or (old == _MERGE and new == self.default)

    def merge_exact(self, intent: integration.MergeIntent):
        self.merge_calls.append(intent)
        proposal = intent.subject
        if self.revoke_human_at_commit:
            self.human_current.pop(intent.human_binding.reference_id, None)
        # This fixture models the required atomic server pair gate.  An
        # advertised expected HEAD alone is deliberately not sufficient. It
        # also checks current, unrevoked proof bindings at commit time.
        if (
            self.pull_facts.state != "open"
            or self.pull_facts.number != proposal.pull_request
            or self.pull_facts.base_repository != proposal.repository
            or self.pull_facts.head_repository != proposal.repository
            or self.pull_facts.head_oid != proposal.head_oid
            or self.pull_facts.base_oid != proposal.base_oid
            or proposal.merge_method not in self.repository_facts.allowed_merge_methods
            or self.ready_current.get(intent.ready_binding.reference_id) != intent.ready_binding
            or self.human_current.get(intent.human_binding.reference_id) != intent.human_binding
        ):
            raise RuntimeError("fixture exact merge precondition failed")
        self.commits[_MERGE] = integration.GitHubCommitFacts(
            _MERGE,
            proposal.expected_merge_tree_oid,
            (proposal.base_oid, proposal.head_oid)
            if proposal.merge_method == "merge" else (proposal.base_oid,),
        )
        self.pull_facts = replace(
            self.pull_facts, state="merged", merge_oid=_MERGE,
            merge_method=proposal.merge_method,
        )
        self.default = _MERGE
        self.applied_merges += 1
        if self.fail_after_apply:
            raise RuntimeError("lost merge acknowledgement")
        return {"merged": True}


class TaskIntegrationV4Test(unittest.TestCase):
    """Closed-schema and full local merge/reconcile fixtures isolated in tempdirs."""

    _fixture_set_up = metadata_fixtures.MetadataRefTest.setUp
    _writer = metadata_fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = metadata_fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(metadata_fixtures.MetadataRefTest._git_dir)

    def _real_task_facade(self, method: str = "merge"):
        """Build one temporary #191 Task and shared-main/task worktree fixture."""
        git = metadata_fixtures.git
        self._fixture_set_up()
        initial_branch = git("symbolic-ref", "--short", "HEAD", cwd=self.product).decode("ascii").strip()
        if initial_branch != "main":
            git("branch", "-M", "main", cwd=self.product)
        git("push", "origin", "main", cwd=self.product)
        base = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        task_root = self.temp_root / "task-195"
        git("worktree", "add", "-b", "task/195", str(task_root), base, cwd=self.product)
        with (task_root / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("approved integration change\n")
        git("add", "tracked.txt", cwd=task_root)
        git("commit", "-m", "Task 195 implementation", cwd=task_root)
        head = git("rev-parse", "HEAD", cwd=task_root).decode("ascii").strip()
        head_tree = git("rev-parse", "HEAD^{tree}", cwd=task_root).decode("ascii").strip()
        git("push", "origin", f"{head}:{_BRANCH}", cwd=task_root)

        task_store = self._writer(root=task_root)
        records = task_record.TaskRecords(task_store, authorize=lambda _request: True)
        record = records.create(
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=base,
            title="Task 195 requirement",
            body="A scoped local fixture requirement.",
        )
        task_git = collaboration_git.TaskGit(
            task_store,
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=base,
            default_branch_ref=_DEFAULT,
            publish=lambda _intent: None,
        )

        class TemporaryGitHub:
            def __init__(self) -> None:
                self.repository_facts = integration.RepositoryFacts(
                    _REPOSITORY, 910195, _DEFAULT, ("merge", "squash"),
                )
                self.pull_facts = integration.PullFacts(
                    _REAL_PR, _REPOSITORY, _REPOSITORY, "main", "task/195",
                    head, base, "open", False,
                )
                self.merge_calls: list[integration.MergeIntent] = []
                self.applied_merges = 0
                self.revoke_human_at_commit = False
                self.fail_after_apply = False
                self.interrupt_after_apply = False
                self.ready_current: dict[str, integration.ReadyBinding] = {}
                self.ready_history: dict[str, integration.ReadyBinding] = {}
                self.human_current: dict[str, integration.HumanApprovalBinding] = {}
                self.human_history: dict[str, integration.HumanApprovalBinding] = {}

            def repository(self):
                return self.repository_facts

            def principal(self):
                return 17

            def pull(self, number: int):
                if number != _REAL_PR:
                    raise AssertionError("fixture received an alternate PR number")
                return self.pull_facts

            def default_head(self):
                raw = git("--git-dir", str(self_outer.remote_path), "rev-parse", _DEFAULT)
                return raw.decode("ascii").strip()

            def merge_plan(self, number: int, selected_method: str):
                if number != _REAL_PR or selected_method != method:
                    raise AssertionError("fixture merge plan scope mismatch")
                return integration.MergePlanFacts(
                    _REPOSITORY, _TASK, number, head, base, _DEFAULT,
                    method, head_tree, True,
                    f"fixture-plan:{method}:{base}:{head}:{head_tree}",
                )

            def commit(self, oid: str):
                raw = git("--git-dir", str(self_outer.remote_path), "cat-file", "commit", oid)
                header = raw.split(b"\n\n", 1)[0].splitlines()
                trees = [line[5:].decode("ascii") for line in header if line.startswith(b"tree ")]
                parents = tuple(
                    line[7:].decode("ascii") for line in header if line.startswith(b"parent ")
                )
                if len(trees) != 1:
                    raise AssertionError("fixture commit has malformed tree")
                return integration.GitHubCommitFacts(oid, trees[0], parents)

            def is_default_ancestor(self, old: str, new: str):
                result = self_outer._git_dir(
                    self_outer.remote_path, "merge-base", "--is-ancestor", old, new,
                )
                if result.returncode == 0:
                    return True
                if result.returncode == 1:
                    return False
                raise RuntimeError("fixture commit graph unavailable")

            def merge_capabilities(self):
                # A negative probe only.  The separately installed opaque host
                # permit below carries the commit-time guarantees.
                return integration.MergeTransportCapabilities(True, True, True, True)

            def merge_exact(self, intent: integration.MergeIntent):
                self.merge_calls.append(intent)
                if self.revoke_human_at_commit:
                    self.human_current.pop(intent.human_binding.reference_id, None)
                proposal = intent.subject
                current_base = self.default_head()
                if (
                    self.pull_facts.state != "open"
                    or intent.subject_id != proposal.subject_id
                    or proposal.repository_id != self.repository_facts.repository_id
                    or proposal.default_branch_ref != self.repository_facts.default_branch_ref
                    or self.pull_facts.number != proposal.pull_request
                    or self.pull_facts.base_repository != proposal.repository
                    or self.pull_facts.head_repository != proposal.repository
                    or self.pull_facts.head_ref != proposal.head_ref[len("refs/heads/"):]
                    or self.pull_facts.base_ref != proposal.base_ref[len("refs/heads/"):]
                    or self.pull_facts.head_oid != proposal.head_oid
                    or self.pull_facts.base_oid != proposal.base_oid
                    or current_base != proposal.base_oid
                    or proposal.record_id != self.expected_record_id
                    or proposal.contract_id != self.expected_contract_id
                    or proposal.merge_method != method
                    or proposal.merge_method not in self.repository_facts.allowed_merge_methods
                    or proposal.expected_merge_tree_oid != head_tree
                    or self.ready_current.get(intent.ready_binding.reference_id) != intent.ready_binding
                    or self.human_current.get(intent.human_binding.reference_id) != intent.human_binding
                ):
                    raise RuntimeError("fixture exact-subject/authentication gate refused")
                parents = ("-p", base)
                if method == "merge":
                    parents += ("-p", head)
                merge_oid = git(
                    "-c", "user.name=Temporary Integration Host",
                    "-c", "user.email=integration-host@example.invalid",
                    "--git-dir", str(self_outer.remote_path),
                    "commit-tree", head_tree, *parents, "-m", "Temporary approved integration",
                ).decode("ascii").strip()
                changed = self_outer._git_dir(
                    self_outer.remote_path,
                    "update-ref", "--no-deref", _DEFAULT, merge_oid, base,
                )
                if changed.returncode != 0:
                    raise RuntimeError("fixture default base compare failed")
                self.pull_facts = replace(
                    self.pull_facts, state="merged", merge_oid=merge_oid,
                    merge_method=method,
                )
                self.applied_merges += 1
                if self.interrupt_after_apply:
                    raise KeyboardInterrupt("simulated process interruption after remote apply")
                if self.fail_after_apply:
                    raise RuntimeError("temporary fixture lost merge acknowledgement")
                return False

        self_outer = self
        github = TemporaryGitHub()
        github.expected_record_id = record.record_id
        github.expected_contract_id = record.contract_id
        default_store = self.store
        fetch_requests: list[integration_git.FetchRequest] = []
        ff_requests: list[integration_git.FastForwardRequest] = []

        def fetch(request: integration_git.FetchRequest):
            fetch_requests.append(request)
            remote = git("--git-dir", str(self.remote_path), "rev-parse", _DEFAULT).decode("ascii").strip()
            merge_reachable = self._git_dir(
                self.remote_path, "merge-base", "--is-ancestor",
                request.merged_oid, request.expected_remote,
            )
            if request.expected_remote != remote or merge_reachable.returncode != 0:
                raise RuntimeError("temporary fixture fetch scope mismatch")
            return git(
                "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                "--refmap=", "origin", request.expected_remote, cwd=self.product,
            )

        def fast_forward(request: integration_git.FastForwardRequest):
            ff_requests.append(request)
            local_head = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
            branch = git("symbolic-ref", "--quiet", "HEAD", cwd=self.product).decode("ascii").strip()
            status = git(
                "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=all", cwd=self.product,
            )
            if (
                request.repository != _REPOSITORY or request.branch_ref != _DEFAULT
                or request.worktree != str(self.product.resolve())
                or request.expected_head != local_head or branch != _DEFAULT or status
            ):
                raise RuntimeError("temporary fixture local compare failed")
            return git(
                "-C", str(self.product), "-c", "core.hooksPath=/dev/null",
                "merge", "--ff-only", request.target_head,
            )

        default_git = integration_git.DefaultGit(
            default_store,
            default_branch_ref=_DEFAULT,
            fetch=fetch,
            fast_forward=fast_forward,
        )

        def operation_engine(operation: str, *, human_owned: bool):
            check = prerequisites.CheckSpec(
                "fixture_authority" if human_owned else "fixture_reconcile",
                "temporary_host",
                "human_authority" if human_owned else None,
            )
            policy = prerequisites.OperationPolicy(
                operation, "temporary_policy",
                "human_owned" if human_owned else "execution", (check,),
            )
            return prerequisites.OperationPrerequisites(
                policy,
                checks={check.check_id: lambda context: prerequisites.CheckResult(
                    context.binding, "SATISFIED", "temporary_fixture_satisfied", {}, {},
                )},
            )

        ready_current: dict[str, integration.ReadyBinding] = github.ready_current
        ready_history: dict[str, integration.ReadyBinding] = github.ready_history
        human_current: dict[str, integration.HumanApprovalBinding] = github.human_current
        human_history: dict[str, integration.HumanApprovalBinding] = github.human_history

        def ready_validator(reference, bound_subject, mode):
            if reference.subject_id != bound_subject.subject_id:
                return None
            return (ready_current if mode == "current" else ready_history).get(reference.reference_id)

        def human_validator(reference, bound_subject, mode):
            if reference.subject_id != bound_subject.subject_id:
                return None
            return (human_current if mode == "current" else human_history).get(reference.reference_id)

        stored_intents: dict[str, integration.MergeIntent] = {}

        def record_intent(intent: integration.MergeIntent):
            reference = "temporary-intent/" + intent.intent_id
            stored_intents[reference] = intent
            return integration.RecordedIntent(reference, intent.intent_id)

        sync_intents: list[integration.ReconciliationIntent] = []

        def record_sync_intent(intent: integration.ReconciliationIntent):
            sync_intents.append(intent)
            return integration.RecordedReconciliationIntent(
                "temporary-sync/" + intent.intent_id, intent.intent_id,
            )

        def qualify(request: integration.MergeTransportQualificationRequest):
            self.assertIs(request.transport, github)
            return integration.MergeTransportQualification(
                github,
                _REPOSITORY,
                910195,
                _DEFAULT,
                ("merge", "squash"),
                "f" * 64,
                True, True, True, True, True, True, True, True, True,
            )

        permit = integration.qualify_merge_transport(
            github,
            repository=_REPOSITORY,
            repository_id=910195,
            default_branch_ref=_DEFAULT,
            allowed_merge_methods=("merge", "squash"),
            host_qualifier=qualify,
        )
        facade = integration.TaskIntegration(
            task_store,
            task_git,
            default_git,
            github,
            merge_transport_permit=permit,
            prerequisites_by_operation={
                "integration.merge": operation_engine("integration.merge", human_owned=True),
                "integration.reconcile": operation_engine("integration.reconcile", human_owned=False),
            },
            ready_validator=ready_validator,
            human_validator=human_validator,
            record_intent=record_intent,
            load_intent=stored_intents.get,
            record_sync_intent=record_sync_intent,
            # The typed absence explicitly comes from this host reader; omitted
            # or raw-None checkpoint observations are rejected by the facade.
            checkpoint_reader=lambda request: integration.CheckpointObservation("absent"),
        )
        return {
            "facade": facade,
            "github": github,
            "base": base,
            "head": head,
            "tree": head_tree,
            "ready_id": "8" * 64,
            "approval_id": "9" * 64,
            "ready_current": ready_current,
            "ready_history": ready_history,
            "human_current": human_current,
            "human_history": human_history,
            "fetch_requests": fetch_requests,
            "ff_requests": ff_requests,
            "sync_intents": sync_intents,
            "default_git": default_git,
            "merge_transport_permit": permit,
            "task_git": task_git,
            "task_store": task_store,
            "records": records,
            "task_record": record,
            "stored_intents": stored_intents,
        }

    @staticmethod
    def _authorize_real_subject(fixture, method: str):
        value = fixture["facade"].capture_subject(_REAL_PR, method, fixture["ready_id"])
        ready = integration.ReadyReference(value.subject_id, fixture["ready_id"])
        human = integration.HumanApprovalReference(value.subject_id, fixture["approval_id"])
        ready_binding = integration.ReadyBinding(
            value.subject_id, fixture["ready_id"], "a" * 64,
        )
        human_binding = integration.HumanApprovalBinding(
            value.subject_id, fixture["approval_id"], 7001,
            "approve_integration", "b" * 64,
        )
        fixture["ready_current"][fixture["ready_id"]] = ready_binding
        fixture["ready_history"][fixture["ready_id"]] = ready_binding
        fixture["human_current"][fixture["approval_id"]] = human_binding
        fixture["human_history"][fixture["approval_id"]] = human_binding
        return value, ready, human

    def test_subject_digest_is_canonical_and_binds_every_material_field(self) -> None:
        original = subject()
        self.assertEqual(original.subject_id, subject().subject_id)
        changes = (
            {"repository": "acme/other"},
            {"repository_id": 1235},
            {"task": "196"},
            {"pull_request": 29},
            {"head_ref": "refs/heads/task/196"},
            {"head_oid": "f" * 40},
            {"base_ref": "refs/heads/stable", "default_branch_ref": "refs/heads/stable"},
            {"base_oid": "f" * 40},
            {"record_id": "4" * 64},
            {"contract_id": "5" * 64},
            {"merge_method": "squash"},
            {"expected_merge_tree_oid": "f" * 40},
            {"plan_ref": "plan/opaque-2"},
            {"ready_reference_id": "6" * 64},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(original.subject_id, subject(**change).subject_id)

        with self.assertRaises(FrozenInstanceError):
            original.head_oid = "f" * 40  # type: ignore[misc]

    def test_subject_rejects_open_schema_invalid_method_bool_ids_and_unbound_refs(self) -> None:
        valid = subject()
        invalid_values = (
            replace(valid, schema_version=True),
            replace(valid, repository_id=True),
            replace(valid, pull_request=True),
            replace(valid, merge_method="rebase"),
            replace(valid, head_oid="A" * 40),
            replace(valid, record_id="not-a-content-address"),
            replace(valid, ready_reference_id="not-a-content-address"),
            replace(valid, base_ref="refs/heads/other"),
            replace(valid, plan_ref="bad\nplan"),
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(integration.TaskIntegrationError):
                integration._validate_subject(value)

    def test_transport_qualification_requires_all_four_literal_capabilities(self) -> None:
        qualified = integration.MergeTransportCapabilities(True, True, True, True)
        self.assertIs(integration._validate_capabilities(qualified), qualified)
        for value in (
            integration.MergeTransportCapabilities(True, False, True, True),
            integration.MergeTransportCapabilities(True, True, False, True),
            integration.MergeTransportCapabilities(True, True, True, False),
            integration.MergeTransportCapabilities(True, True, True, 1),  # type: ignore[arg-type]
            {"exact_head": True, "exact_base": True, "exact_repository": True, "exact_pr": True},
            True,
        ):
            with self.subTest(value=value), self.assertRaises(integration.TaskIntegrationError) as denied:
                integration._validate_capabilities(value)
            self.assertEqual("merge_transport_unqualified", denied.exception.code)

    def test_opaque_host_permit_is_bound_to_exact_adapter_and_authority_guarantees(self) -> None:
        adapter = object()
        scope = {
            "repository": _REPOSITORY,
            "repository_id": 1234,
            "default_branch_ref": _DEFAULT,
            "allowed_merge_methods": ("merge", "squash"),
        }

        def host_qualifier(request: integration.MergeTransportQualificationRequest):
            self.assertIs(request.transport, adapter)
            self.assertEqual(
                {
                    "atomic_exact_head",
                    "atomic_exact_base",
                    "atomic_exact_repository_and_pr",
                    "atomic_full_subject_method_and_tree",
                    "atomic_current_ready_binding",
                    "atomic_current_human_approval",
                },
                set(request.required_guarantees),
            )
            return integration.MergeTransportQualification(
                request.transport,
                request.repository,
                request.repository_id,
                request.default_branch_ref,
                request.allowed_merge_methods,
                "a" * 64,
                True, True, True, True, True, True, True, True, True,
            )

        permit = integration.qualify_merge_transport(
            adapter, host_qualifier=host_qualifier, **scope,
        )
        self.assertIs(
            permit,
            integration._validate_transport_permit(
                permit, transport=adapter, **scope,
            ),
        )
        with self.assertRaises(integration.TaskIntegrationError):
            integration._validate_transport_permit(
                permit, transport=object(), **scope,
            )

        for invalid in (
            True,
            replace(
                host_qualifier(integration.MergeTransportQualificationRequest(
                    adapter, _REPOSITORY, 1234, _DEFAULT, ("merge", "squash"),
                    integration._REQUIRED_MERGE_GUARANTEES,
                )),
                exact_base=False,
            ),
            replace(
                host_qualifier(integration.MergeTransportQualificationRequest(
                    adapter, _REPOSITORY, 1234, _DEFAULT, ("merge", "squash"),
                    integration._REQUIRED_MERGE_GUARANTEES,
                )),
                exact_human_approval=False,
            ),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(integration.TaskIntegrationError):
                integration.qualify_merge_transport(
                    adapter, host_qualifier=lambda _request, proof=invalid: proof, **scope,
                )

    def test_generic_boolean_and_wrong_subject_validator_outputs_are_not_authority(self) -> None:
        proposal = subject()
        ready_reference = integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id)
        approval_reference = integration.HumanApprovalReference(proposal.subject_id, "7" * 64)
        with self.assertRaises(integration.TaskIntegrationError):
            integration._ready_binding(True, ready_reference, proposal)
        with self.assertRaises(integration.TaskIntegrationError):
            integration._human_binding(True, approval_reference, proposal)
        with self.assertRaises(integration.TaskIntegrationError):
            integration._ready_reference(True, proposal)
        with self.assertRaises(integration.TaskIntegrationError):
            integration._approval_reference(True, proposal)

        wrong_ready = integration.ReadyBinding("8" * 64, ready_reference.reference_id, "9" * 64)
        wrong_human = integration.HumanApprovalBinding(
            "8" * 64, approval_reference.reference_id, 42,
            "approve_integration", "a" * 64,
        )
        with self.assertRaises(integration.TaskIntegrationError):
            integration._ready_binding(wrong_ready, ready_reference, proposal)
        with self.assertRaises(integration.TaskIntegrationError):
            integration._human_binding(wrong_human, approval_reference, proposal)

    def test_checkpoint_absence_requires_a_typed_authenticated_reader_result(self) -> None:
        self.assertIsNone(integration._checkpoint_previous(
            integration.CheckpointObservation("absent"), "capture_subject",
        ))
        previous = task_view.PreviousCheckpoint("fixture-checkpoint", _HEAD, _BASE, "a" * 64)
        self.assertIs(
            previous,
            integration._checkpoint_previous(
                integration.CheckpointObservation("observed", previous), "capture_subject",
            ),
        )
        for invalid in (
            None,
            {"state": "absent"},
            integration.CheckpointObservation("observed"),
            integration.CheckpointObservation("absent", previous),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(integration.TaskIntegrationError):
                integration._checkpoint_previous(invalid, "capture_subject")

    def test_merge_intent_digest_binds_human_and_ready_proof_provenance(self) -> None:
        proposal = subject()
        ready = integration.ReadyBinding(proposal.subject_id, proposal.ready_reference_id, "4" * 64)
        human = integration.HumanApprovalBinding(
            proposal.subject_id, "5" * 64, 71, "approve_integration", "6" * 64,
        )
        diagnosis = {"request_id": "7" * 64, "policy_id": "8" * 64, "view_id": "9" * 64}
        intent = integration._make_merge_intent(proposal, ready, human, 81, diagnosis)
        self.assertIs(integration._validate_merge_intent(intent), intent)

        changed_human = replace(intent, human_binding=replace(human, human_id=72))
        with self.assertRaises(integration.TaskIntegrationError) as mismatch:
            integration._validate_merge_intent(changed_human)
        self.assertEqual("merge_intent_digest_mismatch", mismatch.exception.code)

        changed_ready = replace(intent, ready_binding=replace(ready, provenance_id="a" * 64))
        with self.assertRaises(integration.TaskIntegrationError):
            integration._validate_merge_intent(changed_ready)

    def _verifier(self, *, method: str = "merge", parents: tuple[str, ...] | None = None,
                  tree: str = _TREE, default_head: str = _MERGE,
                  reachable: bool = True) -> integration.TaskIntegration:
        """Install only read callbacks into an unconstructed facade instance."""
        instance = object.__new__(integration.TaskIntegration)
        proposal = subject(merge_method=method)
        repository = integration.RepositoryFacts(_REPOSITORY, 1234, _DEFAULT, ("merge", "squash"))
        pull = integration.PullFacts(
            proposal.pull_request,
            _REPOSITORY,
            _REPOSITORY,
            "main",
            "task/195",
            _HEAD,
            _BASE,
            "merged",
            False,
            _MERGE,
            method,
        )
        commit = integration.GitHubCommitFacts(
            _MERGE,
            tree,
            ((_BASE, _HEAD) if method == "merge" else (_BASE,)) if parents is None else parents,
        )
        instance._store = SimpleNamespace(_oid_length=40)
        instance._repository = _REPOSITORY
        instance._default_branch_ref = _DEFAULT
        instance._common_identity = (1, 1)
        instance._repository_facts = repository
        instance._github = object()
        instance._default_git = object()
        instance._default_store = object()
        instance._read_repository = mock.Mock(return_value=repository)
        instance._pull = mock.Mock(return_value=pull)
        instance._github_commit = mock.Mock(return_value=commit)
        instance._default_head = mock.Mock(return_value=default_head)
        instance._default_remote_head = mock.Mock(return_value=default_head)
        instance._github_ancestor = mock.Mock(return_value=reachable)
        instance._assert_common_identity = mock.Mock()
        instance._test_subject = proposal
        return instance

    def test_merge_verification_uses_exact_parent_order_and_planned_tree(self) -> None:
        merge = self._verifier()
        self.assertEqual(_MERGE, merge._verify_merged(merge._test_subject, "merge"))

        squash = self._verifier(method="squash")
        self.assertEqual(_MERGE, squash._verify_merged(squash._test_subject, "recover"))

        wrong_parent = self._verifier(parents=(_HEAD, _BASE))
        with self.assertRaises(integration.TaskIntegrationError) as parent_error:
            wrong_parent._verify_merged(wrong_parent._test_subject, "merge")
        self.assertEqual("merge_commit_binding_mismatch", parent_error.exception.code)

        wrong_tree = self._verifier(tree="f" * 40)
        with self.assertRaises(integration.TaskIntegrationError) as tree_error:
            wrong_tree._verify_merged(wrong_tree._test_subject, "merge")
        self.assertEqual("merge_commit_binding_mismatch", tree_error.exception.code)

    def test_merge_verification_requires_real_merged_flag_and_default_reachability(self) -> None:
        open_pull = self._verifier()
        open_pull._pull.return_value = replace(open_pull._pull.return_value, state="open")
        with self.assertRaises(integration.TaskIntegrationError) as unmerged:
            open_pull._verify_merged(open_pull._test_subject, "merge")
        self.assertEqual("merge_not_confirmed", unmerged.exception.code)

        stale_default = self._verifier(default_head="f" * 40, reachable=False)
        with self.assertRaises(integration.TaskIntegrationError) as unreachable:
            stale_default._verify_merged(stale_default._test_subject, "merge")
        self.assertEqual("merge_not_reachable_from_default", unreachable.exception.code)

        ahead_default = self._verifier(default_head="f" * 40, reachable=True)
        self.assertEqual(_MERGE, ahead_default._verify_merged(ahead_default._test_subject, "merge"))

    def test_commit_facts_are_closed_and_boolean_parent_shapes_are_rejected(self) -> None:
        for value in (
            {"oid": _MERGE, "tree": _TREE, "parents": (_BASE, _HEAD)},
            integration.GitHubCommitFacts(_MERGE, _TREE, [_BASE, _HEAD]),  # type: ignore[arg-type]
            integration.GitHubCommitFacts(_MERGE, _TREE, (True,)),  # type: ignore[arg-type]
        ):
            with self.subTest(value=value), self.assertRaises(integration.TaskIntegrationError):
                integration._validate_commit(value, oid=_MERGE, oid_length=40)

    def test_merge_intent_receipt_requires_typed_exact_ack(self) -> None:
        proposal = subject()
        ready = integration.ReadyBinding(proposal.subject_id, proposal.ready_reference_id, "4" * 64)
        human = integration.HumanApprovalBinding(
            proposal.subject_id, "5" * 64, 71, "approve_integration", "6" * 64,
        )
        intent = integration._make_merge_intent(
            proposal, ready, human, 81,
            {"request_id": "7" * 64, "policy_id": "8" * 64, "view_id": "9" * 64},
        )
        self.assertEqual(
            integration.RecordedIntent("intent/opaque", intent.intent_id),
            integration._recorded_intent(
                integration.RecordedIntent("intent/opaque", intent.intent_id), intent,
            ),
        )
        for value in (True, None, {"intent_ref": "intent/opaque", "intent_id": intent.intent_id}):
            with self.subTest(value=value), self.assertRaises(integration.TaskIntegrationError):
                integration._recorded_intent(value, intent)

    def _merge_harness(self, *, capabilities=None, ready_result=None, human_result=None,
                       intent_callback=None) -> tuple[integration.TaskIntegration, _FakeGitHub,
                                                      integration.IntegrationSubject]:
        proposal = subject()
        ready_ref = integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id)
        human_ref = integration.HumanApprovalReference(proposal.subject_id, "4" * 64)
        ready_binding = integration.ReadyBinding(proposal.subject_id, ready_ref.reference_id, "5" * 64)
        human_binding = integration.HumanApprovalBinding(
            proposal.subject_id, human_ref.reference_id, 77, "approve_integration", "6" * 64,
        )
        hub = _FakeGitHub(proposal)
        hub.ready_current[ready_binding.reference_id] = ready_binding
        hub.human_current[human_binding.reference_id] = human_binding
        if capabilities is not None:
            hub.capabilities = capabilities
        default_facts = SimpleNamespace(
            repository=_REPOSITORY, branch_ref=_DEFAULT, head=_BASE, tree="a" * 40,
            worktree="/temporary/main", clean=True,
            index_fingerprint="b" * 64, status_fingerprint="c" * 64,
        )
        task_facts = integration._LocalTaskFacts(
            _REPOSITORY, _TASK, _BRANCH, _HEAD, "d" * 40,
            "/temporary/task", True, "e" * 64, "f" * 64,
        )
        capture = integration._Capture(
            proposal,
            hub.repository_facts,
            hub.pull_facts,
            integration.MergePlanFacts(
                _REPOSITORY, _TASK, proposal.pull_request, _HEAD, _BASE,
                _DEFAULT, "merge", _TREE, True, "plan/opaque-1",
            ),
            hub.principal(),
            task_facts,
            _HEAD,
            default_facts,
            _BASE,
            _BASE,
            {"factual": "fixture"},
            b'{"factual":"fixture"}',
            hub.capabilities,
        )
        instance = object.__new__(integration.TaskIntegration)
        instance._github = hub
        instance._store = SimpleNamespace(_oid_length=40)
        instance._repository = _REPOSITORY
        instance._default_branch_ref = _DEFAULT
        instance._repository_facts = hub.repository_facts
        instance._merge_transport_permit = integration.qualify_merge_transport(
            hub,
            repository=_REPOSITORY,
            repository_id=hub.repository_facts.repository_id,
            default_branch_ref=_DEFAULT,
            allowed_merge_methods=hub.repository_facts.allowed_merge_methods,
            host_qualifier=lambda request: integration.MergeTransportQualification(
                request.transport,
                request.repository,
                request.repository_id,
                request.default_branch_ref,
                request.allowed_merge_methods,
                "a" * 64,
                True, True, True, True, True, True, True, True, True,
            ),
        )
        instance._default_git = SimpleNamespace(remote_head=lambda: hub.default)
        instance._default_store = object()
        instance._common_identity = (1, 1)
        instance._capture_open_state = mock.Mock(return_value=capture)
        instance._same_capture = mock.Mock(return_value=capture)
        diagnosis = {"request_id": "7" * 64, "policy_id": "8" * 64, "view_id": "9" * 64}
        instance._diagnose = mock.Mock(return_value=(diagnosis, b"canonical diagnosis"))
        instance._ready_validator = lambda _reference, _subject, _mode: (
            ready_binding if ready_result is None else ready_result
        )
        instance._human_validator = lambda _reference, _subject, _mode: (
            human_binding if human_result is None else human_result
        )
        stored: dict[str, integration.MergeIntent] = {}

        def record(intent: integration.MergeIntent):
            if intent_callback is not None:
                intent_callback(hub)
            reference = "intent/" + intent.intent_id
            stored[reference] = intent
            return integration.RecordedIntent(reference, intent.intent_id)

        instance._record_intent = record
        instance._load_intent = stored.get
        instance._assert_common_identity = mock.Mock()
        return instance, hub, proposal

    def test_exact_merge_ignores_ack_and_confirms_actual_state(self) -> None:
        facade, hub, proposal = self._merge_harness()
        hub.fail_after_apply = True
        ready = integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id)
        approval = integration.HumanApprovalReference(proposal.subject_id, "4" * 64)

        receipt = facade.merge(proposal, ready, approval)

        self.assertEqual(1, len(hub.merge_calls))
        self.assertEqual(_MERGE, receipt.merge_oid)
        self.assertEqual(proposal.subject_id, receipt.subject_id)
        self.assertEqual(proposal.base_oid, receipt.base_oid)
        self.assertEqual("merge", receipt.merge_method)
        self.assertEqual(hub.merge_calls[0].intent_id, receipt.intent_id)
        self.assertEqual("4" * 64, receipt.approval_ref)
        self.assertEqual(proposal.ready_reference_id, receipt.ready_ref)

    def test_missing_server_base_cas_and_generic_proofs_never_call_merge(self) -> None:
        unqualified = integration.MergeTransportCapabilities(True, False, True, True)
        facade, hub, proposal = self._merge_harness(capabilities=unqualified)
        with self.assertRaises(integration.TaskIntegrationError) as capability:
            facade.merge(
                proposal,
                integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id),
                integration.HumanApprovalReference(proposal.subject_id, "4" * 64),
            )
        self.assertEqual("merge_transport_unqualified", capability.exception.code)
        self.assertEqual([], hub.merge_calls)

        facade, hub, proposal = self._merge_harness(ready_result=True)
        with self.assertRaises(integration.TaskIntegrationError):
            facade.merge(
                proposal,
                integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id),
                integration.HumanApprovalReference(proposal.subject_id, "4" * 64),
            )
        self.assertEqual([], hub.merge_calls)

        facade, hub, proposal = self._merge_harness(human_result=True)
        with self.assertRaises(integration.TaskIntegrationError):
            facade.merge(
                proposal,
                integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id),
                integration.HumanApprovalReference(proposal.subject_id, "4" * 64),
            )
        self.assertEqual([], hub.merge_calls)

    def test_changed_base_after_durable_intent_is_rechecked_before_server_write(self) -> None:
        def move_base(hub: _FakeGitHub) -> None:
            hub.pull_facts = replace(hub.pull_facts, base_oid="f" * 40)

        facade, hub, proposal = self._merge_harness(intent_callback=move_base)
        # The facade's fourth re-observation is immediately after durable
        # intent recording; it detects the changed PR base and does not call
        # the only mutation capability.
        facade._same_capture.side_effect = [
            facade._same_capture.return_value,
            facade._same_capture.return_value,
            facade._same_capture.return_value,
            integration.TaskIntegrationError("integration_subject_changed", operation="merge"),
        ]
        with self.assertRaises(integration.TaskIntegrationError):
            facade.merge(
                proposal,
                integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id),
                integration.HumanApprovalReference(proposal.subject_id, "4" * 64),
            )
        self.assertEqual([], hub.merge_calls)

    def test_commit_bound_host_gate_rejects_human_revocation_after_final_client_check(self) -> None:
        facade, hub, proposal = self._merge_harness()
        hub.revoke_human_at_commit = True
        with self.assertRaises(integration.TaskIntegrationError) as refused:
            facade.merge(
                proposal,
                integration.ReadyReference(proposal.subject_id, proposal.ready_reference_id),
                integration.HumanApprovalReference(proposal.subject_id, "4" * 64),
            )
        self.assertEqual("merge_not_confirmed", refused.exception.code)
        self.assertEqual(1, len(hub.merge_calls))
        self.assertEqual(0, hub.applied_merges)
        self.assertEqual("open", hub.pull_facts.state)

    def test_real_task_record_merge_and_squash_commits_exist_only_on_temporary_remote_first(self) -> None:
        git = metadata_fixtures.git
        for method in ("merge", "squash"):
            with self.subTest(method=method):
                fixture = self._real_task_facade(method)
                proposal, ready, human = self._authorize_real_subject(fixture, method)
                fixture["github"].fail_after_apply = True
                receipt = fixture["facade"].merge(proposal, ready, human)

                facts = fixture["github"].commit(receipt.merge_oid)
                expected_parents = (
                    (fixture["base"], fixture["head"])
                    if method == "merge" else (fixture["base"],)
                )
                self.assertEqual(expected_parents, facts.parents)
                self.assertEqual(fixture["tree"], facts.tree)
                self.assertEqual(receipt.merge_oid, fixture["github"].default_head())
                self.assertEqual(1, fixture["github"].applied_merges)
                # The merge commit is genuinely in the temporary bare remote,
                # but no merge object is installed in local Main until the
                # separately invoked DefaultGit reconciliation fetches it.
                with self.assertRaises(integration_git.SafeDefaultGitError):
                    fixture["default_git"].commit_facts(receipt.merge_oid)
                self.assertEqual(
                    fixture["base"],
                    git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
                )

    def test_real_reconciliation_fetches_confirmed_remote_merge_then_fast_forwards_shared_main(self) -> None:
        git = metadata_fixtures.git
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        merge_receipt = fixture["facade"].merge(proposal, ready, human)
        self.assertEqual(
            fixture["base"],
            git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
        )
        with self.assertRaises(integration_git.SafeDefaultGitError):
            fixture["default_git"].commit_facts(merge_receipt.merge_oid)

        result = fixture["facade"].reconcile(merge_receipt.intent_ref)

        self.assertEqual(merge_receipt, result.merge_receipt)
        self.assertEqual(merge_receipt.merge_oid, result.default_receipt.new_head)
        self.assertEqual(merge_receipt.merge_oid, result.default_receipt.merged_oid)
        self.assertEqual(1, len(fixture["fetch_requests"]))
        self.assertEqual(merge_receipt.merge_oid, fixture["fetch_requests"][0].expected_remote)
        self.assertEqual(merge_receipt.merge_oid, fixture["fetch_requests"][0].merged_oid)
        self.assertEqual(1, len(fixture["ff_requests"]))
        self.assertEqual(merge_receipt.merge_oid, fixture["ff_requests"][0].target_head)
        self.assertEqual(
            merge_receipt.merge_oid,
            git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
        )
        self.assertTrue(fixture["default_git"].observe().clean)
        self.assertEqual(1, len(fixture["github"].merge_calls))
        self.assertEqual(1, len(fixture["sync_intents"]))

        # A lost reconciliation acknowledgement must not cause another merge,
        # fast-forward, or durable synchronization-intent write on retry.
        retry = fixture["facade"].reconcile(merge_receipt.intent_ref)
        self.assertEqual(merge_receipt, retry.merge_receipt)
        self.assertEqual(merge_receipt.merge_oid, retry.default_receipt.old_head)
        self.assertEqual(merge_receipt.merge_oid, retry.default_receipt.new_head)
        self.assertEqual(1, len(fixture["ff_requests"]))
        self.assertEqual(1, len(fixture["sync_intents"]))
        self.assertEqual(1, len(fixture["github"].merge_calls))

    def test_real_moved_integration_subject_never_reuses_exact_old_approval(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        hub = fixture["github"]
        original_pull = hub.pull_facts
        original_repo = hub.repository_facts
        original_plan = hub.merge_plan
        mutations = (
            ("head", {"head_oid": "f" * 40}),
            ("pr", {"number": _REAL_PR + 1}),
            ("head_ref", {"head_ref": "task/another"}),
            ("base", {"base_oid": "f" * 40}),
            ("base_ref", {"base_ref": "another-base"}),
        )
        for label, fields in mutations:
            with self.subTest(moved=label):
                hub.pull_facts = replace(original_pull, **fields)
                with self.assertRaises(integration.TaskIntegrationError):
                    fixture["facade"].merge(proposal, ready, human)
                self.assertEqual([], hub.merge_calls)
                self.assertEqual(0, hub.applied_merges)
        hub.pull_facts = original_pull
        hub.repository_facts = replace(original_repo, repository_id=original_repo.repository_id + 1)
        with self.assertRaises(integration.TaskIntegrationError):
            fixture["facade"].merge(proposal, ready, human)
        hub.repository_facts = original_repo
        for fields in ({"expected_tree_oid": "f" * 40}, {"plan_ref": "changed-plan"}, {"method": "squash"}):
            with self.subTest(plan_moved=fields), mock.patch.object(
                hub, "merge_plan", side_effect=lambda number, method, changed=fields: replace(
                    original_plan(number, method), **changed,
                ),
            ):
                with self.assertRaises(integration.TaskIntegrationError):
                    fixture["facade"].merge(proposal, ready, human)
                self.assertEqual([], hub.merge_calls)
        self.assertEqual([], hub.merge_calls)
        self.assertEqual(0, hub.applied_merges)

    def test_dirty_default_reconciliation_retains_confirmed_merge_receipt(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        merge_receipt = fixture["facade"].merge(proposal, ready, human)
        with (self.product / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("concurrent local Main edit\n")

        with self.assertRaises(integration.TaskIntegrationError) as blocked:
            fixture["facade"].reconcile(merge_receipt.intent_ref)

        self.assertEqual("default_worktree_not_clean", blocked.exception.code)
        self.assertEqual(merge_receipt, blocked.exception.merge_receipt)
        self.assertEqual([], fixture["ff_requests"])
        self.assertTrue((self.product / "tracked.txt").read_text(encoding="utf-8").endswith(
            "concurrent local Main edit\n",
        ))

    def test_historical_revoked_approval_recovers_exact_merge_without_a_second_merge(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        merge_receipt = fixture["facade"].merge(proposal, ready, human)
        fixture["human_current"].pop(human.reference_id)

        recovered = fixture["facade"].recover(merge_receipt.intent_ref)

        self.assertEqual(merge_receipt, recovered)
        self.assertEqual(1, len(fixture["github"].merge_calls))

    def test_hard_interrupt_after_actual_merge_recovers_only_the_durable_intent(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        fixture["github"].interrupt_after_apply = True

        with self.assertRaises(KeyboardInterrupt):
            fixture["facade"].merge(proposal, ready, human)

        self.assertEqual(1, fixture["github"].applied_merges)
        self.assertEqual(1, len(fixture["stored_intents"]))
        intent_ref = next(iter(fixture["stored_intents"]))
        recovered = fixture["facade"].recover(intent_ref)
        self.assertEqual(proposal.subject_id, recovered.subject_id)
        self.assertEqual(1, fixture["github"].applied_merges)
        self.assertEqual(1, len(fixture["github"].merge_calls))
        with self.assertRaises(integration.TaskIntegrationError):
            fixture["facade"].recover("unknown-or-forged-intent")
        self.assertEqual(1, len(fixture["github"].merge_calls))

    def test_real_pr_base_movement_and_commit_time_revocation_refuse_without_remote_merge(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        fixture["github"].pull_facts = replace(
            fixture["github"].pull_facts, base_oid="f" * 40,
        )
        with self.assertRaises(integration.TaskIntegrationError):
            fixture["facade"].merge(proposal, ready, human)
        self.assertEqual(0, fixture["github"].applied_merges)
        self.assertEqual(fixture["base"], fixture["github"].default_head())

        second = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(second, "merge")
        second["github"].revoke_human_at_commit = True
        with self.assertRaises(integration.TaskIntegrationError) as race:
            second["facade"].merge(proposal, ready, human)
        self.assertEqual("merge_not_confirmed", race.exception.code)
        self.assertEqual(0, second["github"].applied_merges)
        self.assertEqual(second["base"], second["github"].default_head())

    def test_failed_192_diagnosis_exposes_safe_bytes_and_never_calls_merge(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        spec = prerequisites.CheckSpec("fixture_authority", "temporary_host", "human_authority")
        engine = prerequisites.OperationPrerequisites(
            prerequisites.OperationPolicy(
                "integration.merge", "temporary_policy", "human_owned", (spec,),
            ),
            checks={spec.check_id: lambda context: prerequisites.CheckResult(
                context.binding,
                "MISSING_PREREQUISITE",
                "fixture_merge_prerequisite_missing",
                {"required_facts_observed": False},
                {"required_facts_observed": True},
            )},
        )
        fixture["facade"]._engines["integration.merge"] = engine

        with self.assertRaises(integration.TaskIntegrationError) as blocked:
            fixture["facade"].merge(proposal, ready, human)

        self.assertEqual("operation_prerequisites_not_satisfied", blocked.exception.code)
        self.assertIs(type(blocked.exception.diagnosis_bytes), bytes)
        self.assertEqual([], fixture["github"].merge_calls)
        self.assertEqual(0, fixture["github"].applied_merges)

    def test_real_191_contract_change_from_ready_callback_invalidates_subject(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        original_validator = fixture["facade"]._ready_validator
        changed: list[object] = []

        def reauthorize_during_validation(reference, bound_subject, mode):
            result = original_validator(reference, bound_subject, mode)
            if mode == "current" and not changed:
                changed.append(fixture["records"].reauthorize(
                    task=_TASK,
                    branch_ref=_BRANCH,
                    base_revision=fixture["base"],
                    expected_record_id=fixture["task_record"].record_id,
                    title="Requirement changed after ready validation",
                    body="The prior approval subject is now stale.",
                ))
            return result

        fixture["facade"]._ready_validator = reauthorize_during_validation
        with self.assertRaises(integration.TaskIntegrationError) as stale:
            fixture["facade"].merge(proposal, ready, human)
        self.assertEqual("integration_subject_changed", stale.exception.code)
        self.assertEqual(1, len(changed))
        self.assertEqual([], fixture["github"].merge_calls)
        self.assertEqual(0, fixture["github"].applied_merges)

    def test_unrelated_217_append_does_not_invalidate_same_current_record_contract_subject(self) -> None:
        fixture = self._real_task_facade("merge")
        proposal, ready, human = self._authorize_real_subject(fixture, "merge")
        evidence_id, evidence_data = evidence.encode_evidence(
            _REPOSITORY,
            _TASK,
            fixture["head"],
            {
                "schema_version": 1,
                "kind": "integration-fixture/unrelated",
                "producer": {"name": "temporary-test"},
                "created_at": "2025-01-02T03:04:05Z",
                "payload": {"opaque": "unrelated metadata append"},
            },
        )
        original_validator = fixture["facade"]._ready_validator
        published: list[str] = []

        def append_other_metadata_during_check(reference, bound_subject, mode):
            result = original_validator(reference, bound_subject, mode)
            if mode == "current" and not published:
                fixture["task_store"].publish([evidence_data])
                published.append(evidence_id)
            return result

        fixture["facade"]._ready_validator = append_other_metadata_during_check
        receipt = fixture["facade"].merge(proposal, ready, human)
        self.assertEqual([evidence_id], published)
        self.assertEqual(1, fixture["github"].applied_merges)
        self.assertEqual(proposal.subject_id, receipt.subject_id)

    def test_repository_slug_match_does_not_substitute_for_shared_git_common_identity(self) -> None:
        git = metadata_fixtures.git
        fixture = self._real_task_facade("merge")
        separate_main = self.temp_root / "separate-main-clone"
        git(
            "clone", "--branch", "main", str(self.remote_path), str(separate_main),
        )
        separate_store = self._writer(root=separate_main)
        separate_default = integration_git.DefaultGit(
            separate_store,
            default_branch_ref=_DEFAULT,
            fetch=lambda _request: None,
            fast_forward=lambda _request: None,
        )

        with self.assertRaises(integration.TaskIntegrationError) as distinct:
            integration.TaskIntegration(
                fixture["task_store"],
                fixture["task_git"],
                separate_default,
                fixture["github"],
                merge_transport_permit=fixture["merge_transport_permit"],
                prerequisites_by_operation=fixture["facade"]._engines,
                ready_validator=fixture["facade"]._ready_validator,
                human_validator=fixture["facade"]._human_validator,
                record_intent=fixture["facade"]._record_intent,
                load_intent=fixture["facade"]._load_intent,
                record_sync_intent=fixture["facade"]._record_sync_intent,
                checkpoint_reader=fixture["facade"]._checkpoint_reader,
            )
        self.assertEqual("distinct_git_object_store", distinct.exception.code)


if __name__ == "__main__":
    unittest.main()
