from __future__ import annotations

import hashlib
import subprocess
import sys
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import evidence as evidence_module  # noqa: E402
import operation_prerequisites as prerequisites  # noqa: E402
import task_collaboration  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as fixtures  # noqa: E402
import turn_orchestration as orchestration  # noqa: E402
import turn_work as work  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "196"


class TurnOrchestrationV4Test(unittest.TestCase):
    """Host seams use only a temporary local Git/metadata fixture.

    Dispatcher callbacks never launch a model/provider process, GitHub request,
    or production adapter. Fixture project::check/project::test callbacks run
    isolated Python assertions/processes against this temporary repository only;
    they are not the repository's test suite. The head-race case uses local Git
    helpers only on this temporary repository. Other observations are explicitly
    UNAVAILABLE/NOT_RUN, never fabricated PASS results.
    """

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip
    _set_remote_tip = fixtures.MetadataRefTest._set_remote_tip
    _product_state = fixtures.MetadataRefTest._product_state

    def setUp(self) -> None:
        self._fixture_set_up()
        # Task and PR-base roles are distinct even in the qualified test double.
        fixtures.git("switch", "-c", "task-196", cwd=self.product)
        self.base_revision = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.branch_ref = fixtures.git("symbolic-ref", "--quiet", "HEAD", cwd=self.product).decode("utf-8").strip()
        self.record_publication = task_record.TaskRecords(
            self.store, authorize=lambda _request: True,
        ).create(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            title="Temporary fixture requirement",
            body="Only local test-fixture authority; not a live Issue or production policy.",
        )
        self.dirty = False
        self.subject_reads = 0
        self.checkpoint_pr_number: int | None = None
        self.checkpoint_previous: task_view.PreviousCheckpoint | None = None

    def _view(self) -> dict[str, Any]:
        if self.checkpoint_pr_number is not None:
            return task_view.observe_live_task(
                self.store,
                task=_TASK,
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                observe_remote_head=True,
                pr_number=self.checkpoint_pr_number,
                github_reader=self._checkpoint_github_observation,
            )
        return task_view.observe_live_task(
            self.store,
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
        )

    def _enable_checkpoint_owner_facts(self) -> None:
        self.checkpoint_pr_number = 233
        self.checkpoint_previous = None
        fixtures.git(
            "-c", "core.hooksPath=/dev/null", "push", "--no-follow-tags", "origin",
            f"{self.branch_ref}:{self.branch_ref}", cwd=self.product,
        )

    def _checkpoint_github_observation(
        self, request: task_view.GitHubPullRequestRequest,
    ) -> task_view.GitHubObservation:
        if (
            type(request) is not task_view.GitHubPullRequestRequest
            or request.repository != _REPOSITORY
            or request.task != _TASK
            or request.number != self.checkpoint_pr_number
            or request.branch_ref != self.branch_ref
        ):
            raise ValueError("fixture checkpoint observer binding mismatch")
        current_head = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        return task_view.GitHubObservation(
            task_view.GitHubPullRequestFacts(
                _REPOSITORY,
                _REPOSITORY,
                request.number,
                "open",
                True,
                "main",
                self.branch_ref.removeprefix("refs/heads/"),
                current_head,
            ),
            self.checkpoint_previous,
        )

    def _subject(self) -> orchestration.ExactSubject:
        view = self._view()
        self.subject_reads += 1
        index_text = fixtures.git("rev-parse", "--git-path", "index", cwd=self.product).decode().strip()
        index_path = Path(index_text)
        if not index_path.is_absolute():
            index_path = self.product / index_path
        index_fingerprint = hashlib.sha256(index_path.read_bytes()).hexdigest()
        status_fingerprint = hashlib.sha256(
            b"fixture-status-state:"
            + (b"dirty" if self.dirty else b"clean")
            + (self.product / "tracked.txt").read_bytes()
        ).hexdigest()
        return orchestration.ExactSubject(
            _REPOSITORY,
            _TASK,
            self.branch_ref,
            view["subject"],
            view["git"]["tree"],
            view["authority"]["record_id"],
            view["authority"]["contract_id"],
            not self.dirty,
            index_fingerprint,
            status_fingerprint,
        )

    @staticmethod
    def _role(name: str = "general") -> work.LeafRole:
        return work.LeafRole(
            name,
            "fixture-provider",
            "fixture model name",
            f"registered-{name}-executor",
            name != "general",
        )

    def _reference(self) -> orchestration.RegisteredWorktreeReference:
        return orchestration.RegisteredWorktreeReference(
            _REPOSITORY, _TASK, self.branch_ref, "opaque-test-worktree-token",
        )

    def _registration(
        self,
        reference: orchestration.RegisteredWorktreeReference,
        request: orchestration.DispatchRequest,
    ) -> orchestration.WorktreeRegistration:
        binding = request.binding
        return orchestration.WorktreeRegistration(
            "fixture-registration",
            binding.repository,
            binding.task,
            binding.branch_ref,
            binding.head,
            binding.tree,
            binding.record_id,
            binding.contract_id,
            binding.view_id,
            binding.unit_id,
            binding.role,
            binding.provider,
            binding.model,
            binding.executor_ref,
            reference.token,
        )

    def _capability(self, dispatch, *, checks: tuple[str, ...] = ()) -> orchestration.QualifiedHostDispatcher:
        return orchestration.QualifiedHostDispatcher(
            self._registration,
            dispatch,
            "test-fixture-only",
            True,
            True,
            True,
            True,
            checks,
            scope_enforced=True,
            subject_custody_enforced=True,
        )

    def _orchestrator(
        self,
        *,
        roles: tuple[work.LeafRole, ...] | None = None,
        dispatch=None,
        checks: tuple[str, ...] = (),
        authorizer=None,
        prerequisites_engine=None,
        publish_evidence=None,
        snapshots=None,
        checkpoint_writer=None,
        checkpoint_reader=None,
        checkpoint_observer=None,
        escalation_policy=None,
        decision_policy=None,
    ) -> orchestration.TurnOrchestrator:
        installed_roles = (self._role(),) if roles is None else roles
        capability = None if dispatch is None else self._capability(dispatch, checks=checks)
        return orchestration.TurnOrchestrator(
            _REPOSITORY,
            _TASK,
            installed_roles,
            observe_view=self._view,
            observe_subject=self._subject,
            dispatch_capability=capability,
            worktree_reference=self._reference(),
            prerequisites=prerequisites_engine,
            authorizer=(lambda _request: True) if authorizer is None else authorizer,
            evidence_store=self.store if publish_evidence is not None else None,
            publish_evidence=publish_evidence,
            snapshots=snapshots,
            checkpoint_writer=checkpoint_writer,
            checkpoint_reader=checkpoint_reader,
            checkpoint_observer=checkpoint_observer,
            escalation_policy=escalation_policy,
            decision_policy=decision_policy,
        )

    @staticmethod
    def _unit(
        context, role: work.LeafRole, unit_id: str = "u-one", *, output: str = "report",
        read: tuple[str, ...] = ("tracked.txt",), edit: tuple[str, ...] | None = None,
    ) -> work.WorkUnit:
        edits = (("tracked.txt",) if role.role == "general" else ()) if edit is None else edit
        return work.WorkUnit(
            unit_id,
            context,
            role,
            "Return one concise bounded fixture observation.",
            read,
            edits,
            ("Do not infer authority from output prose.",),
            (),
            (output,),
            ("Stop after the typed response.",),
            work.UnitBudget(),
        )

    @staticmethod
    def _leaf(unit_id: str, *, outcome: str = "COMPLETED", approval=None, decision=None) -> work.LeafResult:
        outputs = () if outcome != "COMPLETED" else (work.LeafOutput("report", "fixture response"),)
        return work.LeafResult(
            unit_id,
            outcome,
            "The fixture response is descriptive only.",
            outputs,
            approval,
            decision,
        )

    def _dispatch_response(
        self,
        request: orchestration.DispatchRequest,
        *,
        result: work.LeafResult | None = None,
        observation: orchestration.VerificationObservation | None = None,
        edit_observation: orchestration.EditObservation | None = None,
    ) -> orchestration.DispatchResponse:
        return orchestration.DispatchResponse(
            request.binding,
            request.context,
            self._leaf(request.binding.unit_id) if result is None else result,
            observation,
            edit_observation,
        )

    def test_role_binding_typed_dispatch_and_opaque_registration_are_exact(self) -> None:
        calls: list[orchestration.DispatchBinding] = []
        authorizations: list[orchestration.DispatchAuthorizationRequest] = []

        def dispatch(registration, request):
            self.assertEqual(request.binding.worktree_token, registration.worktree_token)
            self.assertIsNone(request.inspection)
            self.assertEqual(self.base_revision, request.subject.head)
            calls.append(request.binding)
            return self._dispatch_response(request)

        def authorize(request):
            authorizations.append(request)
            return True

        role = self._role()
        engine = self._orchestrator(roles=(role,), dispatch=dispatch, authorizer=authorize)
        coordinator = engine.begin_turn()
        context = engine.context
        unit = self._unit(context, role)
        engine.add(unit)
        self.assertEqual((unit,), engine.ready())
        with self.assertRaises(FrozenInstanceError):
            context.task = "197"  # type: ignore[misc]

        result = engine.dispatch(unit.unit_id)
        self.assertEqual("COMPLETED", result.outcome)
        self.assertEqual(1, len(calls))
        self.assertEqual(unit.unit_id, calls[0].unit_id)
        self.assertEqual(context.view_id, calls[0].view_id)
        self.assertEqual(1, len(authorizations))
        self.assertEqual(unit.objective, authorizations[0].request.objective)
        self.assertEqual(unit.read_scope, authorizations[0].request.read_scope)
        self.assertEqual(unit.edit_scope, authorizations[0].request.edit_scope)
        self.assertEqual(unit.budget.max_output_bytes, authorizations[0].request.max_output_bytes)
        request = authorizations[0].request
        wire = {
            "binding": request.binding.__dict__,
            "context": request.context.__dict__,
            "subject": orchestration.TurnOrchestrator._subject_wire(request.subject),
            "objective": request.objective,
            "read_scope": request.read_scope,
            "edit_scope": request.edit_scope,
            "constraints": request.constraints,
            "dependencies": request.dependencies,
            "expected_outputs": request.expected_outputs,
            "stop_conditions": request.stop_conditions,
            "max_seconds": request.max_seconds,
            "max_output_bytes": request.max_output_bytes,
            "max_context_bytes": request.max_context_bytes,
            "inspection": None if request.inspection is None else request.inspection.__dict__,
        }
        expected_digest = hashlib.sha256(
            b"agentcore-turn-dispatch-authorization/v1\n"
            + orchestration._canonical(wire, maximum=request.max_context_bytes)
        ).hexdigest()
        self.assertEqual(expected_digest, authorizations[0].request_digest)
        self.assertEqual((), engine.ready())
        engine.discard()
        self.assertEqual((), coordinator.units())

    def test_unqualified_adapter_model_echo_swap_and_authorization_denial_fail_closed(self) -> None:
        role = self._role()
        for changed in ("provider", "model", "executor_ref", "view_id", "worktree_token"):
            with self.subTest(changed=changed):
                def dispatch(_registration, request, field=changed):
                    binding = request.binding
                    bad = orchestration.DispatchBinding(
                        **{**binding.__dict__, field: "attacker-substitution"},
                    )
                    return orchestration.DispatchResponse(
                        bad, request.context, self._leaf(request.binding.unit_id),
                    )

                engine = self._orchestrator(roles=(role,), dispatch=dispatch)
                engine.begin_turn()
                unit = self._unit(engine.context, role)
                engine.add(unit)
                self.assertEqual("BLOCKED", engine.dispatch(unit.unit_id).outcome)
                self.assertEqual("invalid_dispatch_response", engine.result(unit.unit_id).failure_code)

        broken = orchestration.QualifiedHostDispatcher(
            self._registration, lambda _registration, _request: self.fail("must not dispatch"),
            "fixture-not-qualified", True, True, True, True,
        )
        with self.assertRaises(orchestration.TurnOrchestrationError) as unqualified:
            orchestration.TurnOrchestrator(
                _REPOSITORY, _TASK, (role,), observe_view=self._view,
                observe_subject=self._subject, dispatch_capability=broken,
                worktree_reference=self._reference(), authorizer=lambda _request: True,
            )
        self.assertEqual("unqualified_dispatch_capability", unqualified.exception.code)

        policy = prerequisites.OperationPolicy(
            "turn.dispatch", "fixture-policy", "execution",
            (prerequisites.CheckSpec("local_facts", "fixture-host"),),
        )
        engine_prereq = prerequisites.OperationPrerequisites(
            policy,
            checks={"local_facts": lambda context: prerequisites.CheckResult(
                context.binding, "SATISFIED", "fixture_facts_observed", {}, {},
            )},
        )
        dispatches: list[bool] = []
        denied = self._orchestrator(
            roles=(role,),
            dispatch=lambda _registration, request: (dispatches.append(True), self._dispatch_response(request))[1],
            prerequisites_engine=engine_prereq,
            authorizer=lambda _request: False,
        )
        denied.begin_turn()
        unit = self._unit(denied.context, role)
        denied.add(unit)
        result = denied.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("execution_authorization_denied", result.failure_code)
        self.assertEqual([], dispatches)

    def test_provider_exception_is_terminal_without_retry_or_model_substitution(self) -> None:
        role = self._role()
        calls: list[tuple[str, str]] = []

        def fail_once(_registration, request):
            calls.append((request.binding.provider, request.binding.model))
            raise RuntimeError("private adapter detail must not leak")

        engine = self._orchestrator(roles=(role,), dispatch=fail_once)
        engine.begin_turn()
        unit = self._unit(engine.context, role)
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("provider_failure", result.failure_code)
        self.assertEqual([("fixture-provider", "fixture model name")], calls)
        self.assertNotIn("private adapter detail", repr(result))
        failure = engine.provider_failure(unit.unit_id)
        self.assertIs(type(failure), orchestration.ProviderFailure)
        self.assertEqual("general", failure.role)
        self.assertEqual("fixture-provider", failure.provider)
        self.assertEqual("fixture model name", failure.model)
        self.assertEqual("RuntimeError", failure.exception_class)
        self.assertEqual(unit.context, failure.context)
        self.assertEqual("provider_failure", failure.failure_code)
        self.assertEqual((), engine.ready())

    def test_malformed_terminal_response_is_one_blocked_attempt(self) -> None:
        role = self._role()
        attempts: list[tuple[str, str]] = []

        def malformed(_registration, request):
            attempts.append((request.binding.provider, request.binding.model))
            return orchestration.DispatchResponse(
                request.binding, request.context, object(),
            )

        engine = self._orchestrator(roles=(role,), dispatch=malformed)
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-malformed")
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("invalid_leaf_result", result.failure_code)
        self.assertEqual([("fixture-provider", "fixture model name")], attempts)
        self.assertEqual((), engine.ready())

    def test_head_drift_blocks_old_ticket_and_requires_a_fresh_exact_turn(self) -> None:
        role = self._role()

        def move_task_head(_registration, request):
            (self.product / "tracked.txt").write_text("temporary head-drift fixture\n", encoding="utf-8")
            fixtures.git("add", "tracked.txt", cwd=self.product)
            fixtures.git("commit", "-m", "temporary head-drift fixture", cwd=self.product)
            return self._dispatch_response(request)

        engine = self._orchestrator(roles=(role,), dispatch=move_task_head)
        old = engine.begin_turn()
        old_context = engine.context
        unit = self._unit(old_context, role)
        engine.add(unit)
        blocked = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", blocked.outcome)
        self.assertEqual("changed_verification_subject", blocked.failure_code)
        with self.assertRaises(orchestration.TurnOrchestrationError) as stale:
            engine.add(self._unit(old_context, role, "u-stale"))
        self.assertEqual("fresh_turn_required", stale.exception.code)

        fresh = engine.begin_turn()
        self.assertNotEqual(old_context.head, engine.context.head)
        self.assertEqual((), fresh.units())
        self.assertEqual((), old.units())

    def test_handoff_refuses_a_live_serialized_dispatch(self) -> None:
        role = self._role()
        callback_errors: list[str] = []
        engine: orchestration.TurnOrchestrator

        def dispatch(_registration, request):
            with self.assertRaises(orchestration.TurnOrchestrationError) as active:
                engine.handoff("Do not snapshot while this unit is active.")
            callback_errors.append(active.exception.code)
            return self._dispatch_response(request)

        engine = self._orchestrator(roles=(role,), dispatch=dispatch)
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-active")
        engine.add(unit)
        self.assertEqual("COMPLETED", engine.dispatch(unit.unit_id).outcome)
        self.assertEqual(["unit_still_running"], callback_errors)

    def test_authorizer_subject_mutation_is_rechecked_before_resolver_or_dispatch(self) -> None:
        role = self._role()
        dispatches: list[bool] = []

        def mutating_authorizer(_request):
            (self.product / "tracked.txt").write_text("authorization callback drift\n", encoding="utf-8")
            self.dirty = True
            return True

        engine = self._orchestrator(
            roles=(role,),
            dispatch=lambda _registration, _request: (dispatches.append(True), None)[1],
            authorizer=mutating_authorizer,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-authorizer-drift")
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("turn_context_changed", result.failure_code)
        self.assertEqual([], dispatches)
        with self.assertRaises(orchestration.TurnOrchestrationError) as stale:
            engine.add(self._unit(engine.context, role, "u-after-authorizer-drift"))
        self.assertEqual("fresh_turn_required", stale.exception.code)

    def test_general_edits_need_exact_host_path_custody_and_scope_binding(self) -> None:
        role = self._role()
        path = "tracked.txt"
        expected_scope = (path,)
        authorization: list[orchestration.DispatchAuthorizationRequest] = []

        def authorize(request):
            authorization.append(request)
            # The fixture authorizer examines the full request instead of
            # treating lexical WorkUnit validation as filesystem permission.
            return request.request.edit_scope == expected_scope

        def edit(_registration, request, *, report_path: str = path):
            (self.product / path).write_text("fixture-scoped edit\n", encoding="utf-8")
            self.dirty = True
            after = self._subject()
            receipt = orchestration.EditObservation(
                request.binding,
                request.subject,
                after,
                (report_path,),
                "fixture-only-confined-write-observation",
            )
            return self._dispatch_response(request, edit_observation=receipt)

        inside = self._orchestrator(roles=(role,), dispatch=edit, authorizer=authorize)
        inside.begin_turn()
        unit = self._unit(
            inside.context, role, "u-edit-inside", read=("tracked.txt",), edit=expected_scope,
        )
        inside.add(unit)
        completed = inside.dispatch(unit.unit_id)
        self.assertEqual("COMPLETED", completed.outcome)
        self.assertEqual(expected_scope, authorization[0].request.edit_scope)
        self.assertIsNone(inside._observations.get(unit.unit_id))

        # A second independent temporary worktree/state observation lets the
        # malicious path claim be rejected without substituting a model or
        # falling back to an unbounded directory.
        fixtures.git("checkout", "--", "tracked.txt", cwd=self.product)
        self.dirty = False
        outside = self._orchestrator(
            roles=(role,),
            dispatch=lambda registration, request: edit(
                registration, request, report_path="outside/private.txt",
            ),
            authorizer=authorize,
        )
        outside.begin_turn()
        outside_unit = self._unit(
            outside.context, role, "u-edit-outside", read=("tracked.txt",), edit=expected_scope,
        )
        outside.add(outside_unit)
        blocked = outside.dispatch(outside_unit.unit_id)
        self.assertEqual("BLOCKED", blocked.outcome)
        self.assertEqual("verification_subject_mismatch", blocked.failure_code)
        self.assertIsNone(outside._observations.get(outside_unit.unit_id))
        with self.assertRaises(orchestration.TurnOrchestrationError) as invalidated:
            outside.add(self._unit(outside.context, role, "u-after-unconfined-edit"))
        self.assertEqual("fresh_turn_required", invalidated.exception.code)

    def test_readonly_role_requires_exact_clean_subject_stability_not_status_counts(self) -> None:
        role = self._role("verifier")
        observation_holder: dict[str, orchestration.VerificationObservation] = {}

        def dispatch(_registration, request):
            self.assertIsNotNone(request.inspection)
            self.assertEqual(self.base_revision, request.inspection.base_revision)
            self.assertEqual(request.binding.head, request.inspection.head)
            binding = request.binding
            observation_holder["value"] = orchestration.VerificationObservation(
                "verification", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                (orchestration.ExecutedCheck(
                    "project::test", "project::test", "UNAVAILABLE", None, 0,
                    hashlib.sha256(b"").hexdigest(),
                ),),
            )
            self.dirty = True
            return self._dispatch_response(request, observation=observation_holder["value"])

        engine = self._orchestrator(roles=(role,), dispatch=dispatch, checks=("project::test",))
        engine.begin_turn()
        unit = self._unit(engine.context, role)
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("changed_verification_subject", result.failure_code)
        self.assertGreater(self.subject_reads, 2)

        # A superficially clean status projection cannot override the separate
        # ExactSubject reader's dirty/changed certificate.
        self.dirty = True
        second = self._orchestrator(
            roles=(role,), dispatch=lambda _r, req: self._dispatch_response(req),
            checks=("project::test",),
        )
        second.begin_turn()
        readonly = self._unit(second.context, role, "u-dirty")
        second.add(readonly)
        blocked = second.dispatch(readonly.unit_id)
        self.assertEqual("BLOCKED", blocked.outcome)
        self.assertEqual("changed_verification_subject", blocked.failure_code)

    def test_actual_dirty_task_view_cannot_be_overridden_by_a_fixture_clean_flag(self) -> None:
        role = self._role("verifier")
        (self.product / "tracked.txt").write_text("actual dirty checkout bytes\n", encoding="utf-8")
        # The fixture's subject callback deliberately claims clean; Task View's
        # independently observed nonzero status must still prevent turn start.
        self.dirty = False
        engine = self._orchestrator(roles=(role,), dispatch=lambda _r, _q: None, checks=("project::test",))
        with self.assertRaises(orchestration.TurnOrchestrationError) as mismatch:
            engine.begin_turn()
        self.assertEqual("exact_subject_binding_mismatch", mismatch.exception.code)

    def test_readonly_dispatch_allows_append_only_metadata_tip_and_promotes_bound_evidence(self) -> None:
        role = self._role("verifier")
        appended: list[str] = []

        def append_other_evidence(_registration, request):
            _other_id, other_bytes = evidence_module.encode_evidence(
                _REPOSITORY,
                _TASK,
                request.binding.head,
                {
                    "schema_version": 1,
                    "kind": "fixture/unrelated-v1",
                    "producer": {"name": "fixture-owner-append"},
                    "created_at": "2025-01-02T03:04:05Z",
                    "payload": {"result": "UNAVAILABLE", "check_ran": False},
                },
            )
            appended.append(self.store.publish([other_bytes]))
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "verification", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                (orchestration.ExecutedCheck(
                    "project::test", "project::test", "UNAVAILABLE", None, 0,
                    hashlib.sha256(b"").hexdigest(),
                ),),
            )
            # Deliberately optimistic leaf prose is not a verification result.
            return self._dispatch_response(
                request,
                result=work.LeafResult(
                    binding.unit_id, "COMPLETED", "The report is ready.",
                    (work.LeafOutput("report", "No test result is claimed here."),),
                ),
                observation=observation,
            )

        def publish(request: orchestration.EvidencePromotionRequest):
            commit = self.store.publish([request.canonical_bytes])
            return orchestration.RecordedEvidence(commit, request.evidence_id, request.subject)

        engine = self._orchestrator(
            roles=(role,), dispatch=append_other_evidence,
            checks=("project::test",), publish_evidence=publish,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-append-only")
        original_context = engine.context
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("verification_unavailable", result.failure_code)
        self.assertEqual(1, len(appended))
        self.assertEqual(appended[0], self.store.fetch_tip())

        reference = engine.promote_verification(unit.unit_id)
        self.assertNotEqual(appended[0], reference.metadata_commit)
        self.assertEqual(reference.metadata_commit, self._remote_tip())
        saved = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=original_context.head,
        )
        payload = saved["payload"]["payload"]
        self.assertEqual("UNAVAILABLE", payload["result"])
        self.assertEqual(original_context.record_id, payload["record_id"])
        self.assertEqual(original_context.contract_id, payload["contract_id"])
        self.assertEqual({
            "role", "provider", "model", "executor_ref", "dispatch_qualification",
        }, set(payload["producer_binding"]))
        self.assertEqual("verifier", payload["producer_binding"]["role"])
        self.assertEqual("fixture-provider", payload["producer_binding"]["provider"])
        self.assertEqual("fixture model name", payload["producer_binding"]["model"])
        self.assertEqual("registered-verifier-executor", payload["producer_binding"]["executor_ref"])
        self.assertEqual("test-fixture-only", payload["producer_binding"]["dispatch_qualification"])
        self.assertNotIn("unit_id", payload)
        self.assertNotIn("unit_id", payload["producer_binding"])

        engine.discard()
        after_discard = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=original_context.head,
        )
        self.assertEqual(saved, after_discard)

    def test_check_policy_rejects_unregistered_repair_commands_and_empty_verification_set(self) -> None:
        role = self._role("verifier")
        for check_ids in (("project::format",), ("project::install",)):
            with self.subTest(check_ids=check_ids), self.assertRaises(
                orchestration.TurnOrchestrationError,
            ) as invalid:
                self._orchestrator(
                    roles=(role,), dispatch=lambda _registration, _request: None,
                    checks=check_ids,
                )
            self.assertEqual("invalid_check_policy", invalid.exception.code)

        with self.assertRaises(orchestration.TurnOrchestrationError) as missing_policy:
            self._orchestrator(
                roles=(role,), dispatch=lambda _registration, _request: None,
            )
        self.assertEqual("invalid_check_policy", missing_policy.exception.code)
        engine = self._orchestrator(
            roles=(role,), dispatch=lambda _registration, _request: None,
            checks=("project::test",),
        )
        engine.begin_turn()
        request = orchestration.DispatchRequest(
            orchestration.DispatchBinding(
                _REPOSITORY, _TASK, self.branch_ref, engine.context.head,
                engine.context.tree, engine.context.record_id, engine.context.contract_id,
                engine.context.view_id, "u-empty-checks", role.role, role.provider,
                role.model, role.executor_ref, self._reference().token,
            ),
            engine.context,
            engine._initial_subject,
            "Explicitly no verification set.",
            ("tracked.txt",), (), (), (), ("report",), (),
            30, 1024, 1024, None,
        )
        observation = orchestration.VerificationObservation(
            "verification", _REPOSITORY, _TASK, self.branch_ref,
            engine.context.head, engine.context.tree,
            engine.context.record_id, engine.context.contract_id,
            engine.context.view_id,
        )
        with self.assertRaises(orchestration.TurnOrchestrationError) as no_checks:
            engine._validate_observation(observation, request.binding, role)
        self.assertEqual("verification_policy_mismatch", no_checks.exception.code)

    def test_review_observations_are_role_bound_and_never_a_test_pass(self) -> None:
        reviewer = self._role("reviewer")
        engine = self._orchestrator(roles=(reviewer,))
        engine.begin_turn()
        context = engine.context
        binding = orchestration.DispatchBinding(
            _REPOSITORY, _TASK, self.branch_ref, context.head, context.tree,
            context.record_id, context.contract_id, context.view_id,
            "u-review-shape", reviewer.role, reviewer.provider, reviewer.model,
            reviewer.executor_ref, self._reference().token,
        )
        review = orchestration.VerificationObservation(
            "review", _REPOSITORY, _TASK, self.branch_ref, context.head,
            context.tree, context.record_id, context.contract_id, context.view_id,
            findings=(),
            review_summary="Fixture-only source-review shape; no production review was run.",
        )
        engine._validate_observation(review, binding, reviewer)
        empty = orchestration.VerificationObservation(
            "review", _REPOSITORY, _TASK, self.branch_ref, context.head,
            context.tree, context.record_id, context.contract_id, context.view_id,
        )
        with self.assertRaises(orchestration.TurnOrchestrationError) as no_observation:
            engine._validate_observation(empty, binding, reviewer)
        self.assertEqual("verification_policy_mismatch", no_observation.exception.code)

        security = self._role("security-reviewer")
        security_engine = self._orchestrator(roles=(security,))
        security_engine.begin_turn()
        other_context = security_engine.context
        security_binding = orchestration.DispatchBinding(
            _REPOSITORY, _TASK, self.branch_ref, other_context.head, other_context.tree,
            other_context.record_id, other_context.contract_id, other_context.view_id,
            "u-security-shape", security.role, security.provider, security.model,
            security.executor_ref, self._reference().token,
        )
        with self.assertRaises(orchestration.TurnOrchestrationError) as cross_role:
            security_engine._validate_observation(review, security_binding, security)
        self.assertEqual("verification_subject_mismatch", cross_role.exception.code)

    def test_full_review_response_must_fit_the_aggregate_unit_output_budget(self) -> None:
        role = self._role("reviewer")

        def oversized(_registration, request):
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "review", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                findings=(
                    orchestration.ReviewFinding("finding-one", "medium", "src/a.py", "a" * 800),
                    orchestration.ReviewFinding("finding-two", "low", "src/b.py", "b" * 800),
                ),
                review_summary="Two bounded fixture findings.",
            )
            return self._dispatch_response(request, observation=observation)

        engine = self._orchestrator(roles=(role,), dispatch=oversized)
        engine.begin_turn()
        unit = work.WorkUnit(
            "u-oversized-review", engine.context, role,
            "Return only bounded findings.", ("src",), (),
            ("Inspect only the exact source subject.",), (), ("report",),
            ("Stop at the response budget.",),
            work.UnitBudget(max_seconds=30, max_output_bytes=1024, max_context_bytes=4096),
        )
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("resource_limit_exceeded", result.failure_code)
        self.assertIsNone(engine._observations.get(unit.unit_id))

    def test_leaf_prose_cannot_be_promoted_and_unavailable_checks_stay_unavailable(self) -> None:
        role = self._role("verifier")

        def prose_only(_registration, request):
            claimed = work.LeafResult(
                request.binding.unit_id,
                "COMPLETED",
                "PASS: project::test passed (leaf prose only).",
                (work.LeafOutput("report", "PASS"),),
            )
            return self._dispatch_response(request, result=claimed)

        def no_op_publisher(_request):
            self.fail("leaf prose without a typed observation must not be published")

        prose = self._orchestrator(
            roles=(role,), dispatch=prose_only, checks=("project::test",),
            publish_evidence=no_op_publisher,
        )
        prose.begin_turn()
        unit = self._unit(prose.context, role)
        prose.add(unit)
        self.assertEqual("BLOCKED", prose.dispatch(unit.unit_id).outcome)
        with self.assertRaises(orchestration.TurnOrchestrationError) as no_proof:
            prose.promote_verification(unit.unit_id)
        self.assertEqual("verification_unavailable", no_proof.exception.code)

        # A typed host observation that explicitly says UNAVAILABLE is stored
        # as UNAVAILABLE even when model prose claims success. No command ran.
        def publisher(request: orchestration.EvidencePromotionRequest) -> orchestration.RecordedEvidence:
            commit = self.store.publish([request.canonical_bytes])
            return orchestration.RecordedEvidence(commit, request.evidence_id, request.subject)

        def unavailable(_registration, request):
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "verification", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                (orchestration.ExecutedCheck(
                    "project::test", "project::test", "UNAVAILABLE", None, 0,
                    hashlib.sha256(b"").hexdigest(),
                ),),
            )
            prose_result = work.LeafResult(
                binding.unit_id,
                "COMPLETED",
                "The task is done; all tests PASS.",
                (work.LeafOutput("report", "The test suite is green."),),
            )
            return self._dispatch_response(request, result=prose_result, observation=observation)

        exact = self._orchestrator(
            roles=(role,), dispatch=unavailable, checks=("project::test",),
            publish_evidence=publisher,
        )
        exact.begin_turn()
        verified = self._unit(exact.context, role, "u-unavailable")
        exact.add(verified)
        self.assertEqual("BLOCKED", exact.dispatch(verified.unit_id).outcome)
        reference = exact.promote_verification(verified.unit_id)
        envelope = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=self.base_revision,
        )
        self.assertEqual("UNAVAILABLE", envelope["payload"]["payload"]["result"])
        self.assertNotEqual("PASS", envelope["payload"]["payload"]["result"])

        exact.discard()
        restarted = self._orchestrator(roles=(role,), dispatch=None)
        new_coordinator = restarted.begin_turn()
        prior_turn_id = verified.context.turn_id
        self.assertNotEqual(prior_turn_id, restarted.context.turn_id)
        saved = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=self.base_revision,
        )
        self.assertEqual(envelope, saved)
        self.assertEqual((), new_coordinator.units())

    def test_executed_temporary_readonly_check_is_measured_not_fabricated(self) -> None:
        role = self._role("verifier")

        def publish(request: orchestration.EvidencePromotionRequest):
            commit = self.store.publish([request.canonical_bytes])
            return orchestration.RecordedEvidence(commit, request.evidence_id, request.subject)

        def run_registered_fixture_check(_registration, request):
            # This test-only registered project::test implementation runs an
            # actual bounded, read-only Python assertion against the temporary
            # fixture checkout. It does not run the repository's test suite.
            script = (
                "from pathlib import Path; import sys; "
                "data=Path(sys.argv[1]).read_text(encoding='utf-8'); "
                "raise SystemExit(0 if data == 'product tree stays untouched\\n' else 1)"
            )
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, str(self.product / "tracked.txt")],
                cwd=self.product,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            output_digest = hashlib.sha256(completed.stdout).hexdigest()
            state = "EXECUTED" if completed.returncode == 0 else "FAILED"
            check = orchestration.ExecutedCheck(
                "project::test",
                "project::test",
                state,
                completed.returncode,
                elapsed_ms,
                output_digest,
            )
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "verification",
                binding.repository,
                binding.task,
                binding.branch_ref,
                binding.head,
                binding.tree,
                binding.record_id,
                binding.contract_id,
                binding.view_id,
                (check,),
            )
            return self._dispatch_response(request, observation=observation)

        engine = self._orchestrator(
            roles=(role,),
            dispatch=run_registered_fixture_check,
            checks=("project::test",),
            publish_evidence=publish,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-real-fixture-check")
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("COMPLETED", result.outcome)
        reference = engine.promote_verification(unit.unit_id)
        envelope = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=self.base_revision,
        )
        recorded = envelope["payload"]["payload"]
        self.assertEqual("PASS", recorded["result"])
        self.assertEqual(["project::test"], recorded["required_check_ids"])
        self.assertEqual("EXECUTED", recorded["checks"][0]["state"])
        self.assertEqual(0, recorded["checks"][0]["exit_code"])
        self.assertEqual(hashlib.sha256(b"").hexdigest(), recorded["checks"][0]["output_sha256"])
        self.assertNotIn("tracked.txt", repr(recorded["checks"][0]))

    def test_failed_and_not_run_checks_remain_nonpassing_in_persisted_evidence(self) -> None:
        role = self._role("verifier")

        def publish(request: orchestration.EvidencePromotionRequest):
            commit = self.store.publish([request.canonical_bytes])
            return orchestration.RecordedEvidence(commit, request.evidence_id, request.subject)

        def run_failed_check(_registration, request):
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", "raise SystemExit(1)"],
                cwd=self.product,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "verification", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                (
                    orchestration.ExecutedCheck(
                        "project::check", "project::check", "FAILED",
                        completed.returncode,
                        int((time.monotonic() - started) * 1000),
                        hashlib.sha256(completed.stdout).hexdigest(),
                    ),
                    orchestration.ExecutedCheck(
                        "project::test", "project::test", "NOT_RUN", None, 0,
                        hashlib.sha256(b"").hexdigest(),
                    ),
                ),
            )
            return self._dispatch_response(request, observation=observation)

        engine = self._orchestrator(
            roles=(role,), dispatch=run_failed_check,
            checks=("project::check", "project::test"), publish_evidence=publish,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-failed-and-skipped")
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("verification_failed", result.failure_code)
        reference = engine.promote_verification(unit.unit_id)
        evidence = evidence_module.Evidence(self.store).read(
            reference.metadata_commit, reference.evidence_id,
            task=_TASK, subject=self.base_revision,
        )["payload"]["payload"]
        self.assertEqual("FAIL", evidence["result"])
        self.assertEqual(["FAILED", "NOT_RUN"], [item["state"] for item in evidence["checks"]])
        self.assertEqual(1, evidence["checks"][0]["exit_code"])
        self.assertIsNone(evidence["checks"][1]["exit_code"])

    def test_unknown_missing_and_duplicate_project_check_ids_are_rejected(self) -> None:
        role = self._role("verifier")
        engine = self._orchestrator(
            roles=(role,), dispatch=lambda _registration, _request: None,
            checks=("project::check", "project::test"),
        )
        engine.begin_turn()
        context = engine.context
        binding = orchestration.DispatchBinding(
            _REPOSITORY, _TASK, self.branch_ref, context.head, context.tree,
            context.record_id, context.contract_id, context.view_id,
            "u-check-shape", role.role, role.provider, role.model,
            role.executor_ref, self._reference().token,
        )

        def check(check_id: str) -> orchestration.ExecutedCheck:
            return orchestration.ExecutedCheck(
                check_id, check_id, "NOT_RUN", None, 0, hashlib.sha256(b"").hexdigest(),
            )

        observations = (
            (check("project::check"),),
            (check("project::check"), check("project::check")),
            (check("project::check"), check("project::unknown")),
        )
        for items in observations:
            with self.subTest(check_ids=tuple(item.check_id for item in items)), self.assertRaises(
                orchestration.TurnOrchestrationError,
            ) as invalid:
                engine._validate_observation(
                    orchestration.VerificationObservation(
                        "verification", _REPOSITORY, _TASK, self.branch_ref,
                        context.head, context.tree, context.record_id,
                        context.contract_id, context.view_id, items,
                    ),
                    binding,
                    role,
                )
            self.assertEqual("verification_policy_mismatch", invalid.exception.code)

    def test_failed_escalation_and_mechanical_callbacks_recheck_exact_subject(self) -> None:
        general = self._role()
        approval = work.OperationalRequest(
            "repository.write", "acme/widgets:fixture", ("tracked.txt",),
            "Temporary request.", ("No execution grant.",),
            "Only the exact fixture path.", ("Return a diff.",), "No authority configured.",
        )

        def approval_dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(request.binding.unit_id, outcome="NEEDS_APPROVAL", approval=approval),
            )

        def mutate_then_raise(_request):
            (self.product / "tracked.txt").write_text("owner policy drift\n", encoding="utf-8")
            self.dirty = True
            raise RuntimeError("private policy detail")

        escalated = self._orchestrator(
            roles=(general,), dispatch=approval_dispatch, escalation_policy=mutate_then_raise,
        )
        escalated.begin_turn()
        unit = self._unit(escalated.context, general, "u-approval-drift")
        escalated.add(unit)
        result = escalated.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("turn_context_changed", result.failure_code)
        with self.assertRaises(orchestration.TurnOrchestrationError) as stale:
            escalated.add(self._unit(escalated.context, general, "u-after-approval-drift"))
        self.assertEqual("fresh_turn_required", stale.exception.code)

        fixtures.git("checkout", "--", "tracked.txt", cwd=self.product)
        self.dirty = False
        decision = work.DecisionRequest(
            "mechanical", "Which bounded spelling?", ("A", "B"),
        )

        def decision_dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(request.binding.unit_id, outcome="NEEDS_DECISION", decision=decision),
            )

        def mutate_then_malformed(_request):
            (self.product / "tracked.txt").write_text("mechanical policy drift\n", encoding="utf-8")
            self.dirty = True
            return object()

        mechanical = self._orchestrator(
            roles=(general,), dispatch=decision_dispatch, decision_policy=mutate_then_malformed,
        )
        mechanical.begin_turn()
        mechanical_unit = self._unit(mechanical.context, general, "u-mechanical-drift")
        mechanical.add(mechanical_unit)
        blocked = mechanical.dispatch(mechanical_unit.unit_id)
        self.assertEqual("BLOCKED", blocked.outcome)
        self.assertEqual("turn_context_changed", blocked.failure_code)

    def test_evidence_ack_requires_remote_reachability_and_never_repairs_a_moved_ref(self) -> None:
        role = self._role("verifier")
        initial_tip = self._remote_tip()
        self.assertIsNotNone(initial_tip)

        def move_remote_back_after_local_publication(request: orchestration.EvidencePromotionRequest):
            candidate = self.store.publish([request.canonical_bytes])
            self._set_remote_tip(initial_tip, candidate)
            return orchestration.RecordedEvidence(candidate, request.evidence_id, request.subject)

        def unavailable(_registration, request):
            binding = request.binding
            observation = orchestration.VerificationObservation(
                "verification", binding.repository, binding.task, binding.branch_ref,
                binding.head, binding.tree, binding.record_id, binding.contract_id,
                binding.view_id,
                (orchestration.ExecutedCheck(
                    "project::test", "project::test", "UNAVAILABLE", None, 0,
                    hashlib.sha256(b"").hexdigest(),
                ),),
            )
            return self._dispatch_response(request, observation=observation)

        engine = self._orchestrator(
            roles=(role,), dispatch=unavailable, checks=("project::test",),
            publish_evidence=move_remote_back_after_local_publication,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-remote-ack")
        engine.add(unit)
        self.assertEqual("BLOCKED", engine.dispatch(unit.unit_id).outcome)
        before_product = self._product_state()
        with self.assertRaises(orchestration.TurnOrchestrationError) as refused:
            engine.promote_verification(unit.unit_id)
        self.assertEqual("evidence_publication_unconfirmed", refused.exception.code)
        self.assertEqual(initial_tip, self._remote_tip())
        self.assertEqual(before_product, self._product_state())

    def test_mechanical_return_malformed_after_subject_change_is_not_consumed(self) -> None:
        role = self._role()
        decision = work.DecisionRequest(
            "mechanical", "Which exact bounded option applies?", ("A", "B"),
        )

        def dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(request.binding.unit_id, outcome="NEEDS_DECISION", decision=decision),
            )

        def mutate_then_return_malformed(_request):
            (self.product / "tracked.txt").write_text("mechanical callback drift\n", encoding="utf-8")
            self.dirty = True
            return object()

        engine = self._orchestrator(
            roles=(role,), dispatch=dispatch, decision_policy=mutate_then_return_malformed,
        )
        engine.begin_turn()
        unit = self._unit(engine.context, role, "u-late-mechanical")
        engine.add(unit)
        result = engine.dispatch(unit.unit_id)
        self.assertEqual("BLOCKED", result.outcome)
        self.assertEqual("turn_context_changed", result.failure_code)
        self.assertIsNone(engine.mechanical_decision(unit.unit_id))

    def test_approval_and_semantic_decision_are_handoffs_not_automatic_actions(self) -> None:
        role = self._role()
        approval = work.OperationalRequest(
            "repository.write",
            "acme/widgets:branch update",
            ("tracked.txt",),
            "The assigned leaf requests a narrow write.",
            ("No host grant has been issued.",),
            "Write only the one assigned file.",
            ("Return a patch for host review.",),
            "No configured execution authority.",
        )
        decisions: list[orchestration.OperationalResolutionRequest] = []

        def approved(policy_request):
            decisions.append(policy_request)
            return orchestration.OperationalResolution(
                "approved", policy_request.binding, policy_request.request,
            )

        def approval_dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(request.binding.unit_id, outcome="NEEDS_APPROVAL", approval=approval),
            )

        ask = self._orchestrator(
            roles=(role,), dispatch=approval_dispatch, escalation_policy=approved,
        )
        ask.begin_turn()
        approval_unit = self._unit(ask.context, role)
        ask.add(approval_unit)
        result = ask.dispatch(approval_unit.unit_id)
        self.assertEqual("NEEDS_APPROVAL", result.outcome)
        self.assertEqual(1, len(decisions))
        self.assertEqual("repository.write", decisions[0].request.operation_class)
        self.assertEqual("approved", ask.operational_resolution(approval_unit.unit_id).disposition)

        decision = work.DecisionRequest(
            "requirement", "Which externally visible behavior is required?", ("A", "B"),
        )
        escalation_calls: list[bool] = []

        def semantic_dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(request.binding.unit_id, outcome="NEEDS_DECISION", decision=decision),
            )

        no_choice = self._orchestrator(
            roles=(role,), dispatch=semantic_dispatch,
            escalation_policy=lambda _request: (escalation_calls.append(True), None)[1],
            decision_policy=lambda _request: self.fail("semantic requirements are never selected by policy"),
        )
        no_choice.begin_turn()
        decision_unit = self._unit(no_choice.context, role)
        no_choice.add(decision_unit)
        outcome = no_choice.dispatch(decision_unit.unit_id)
        self.assertEqual("NEEDS_DECISION", outcome.outcome)
        self.assertEqual([], escalation_calls)
        self.assertEqual("requirement", outcome.decision.category)
        self.assertIsNone(no_choice.mechanical_decision(decision_unit.unit_id))

        mechanical = work.DecisionRequest(
            "mechanical", "Which allowed spelling should the formatter use?", ("A", "B"),
        )

        def mechanical_dispatch(_registration, request):
            return self._dispatch_response(
                request,
                result=self._leaf(
                    request.binding.unit_id, outcome="NEEDS_DECISION", decision=mechanical,
                ),
            )

        def select_mechanical(request):
            return orchestration.MechanicalDecisionResolution(
                request.binding, request.decision, "B", "fixture-policy-ref-17",
            )

        mechanical_turn = self._orchestrator(
            roles=(role,), dispatch=mechanical_dispatch, decision_policy=select_mechanical,
        )
        mechanical_turn.begin_turn()
        mechanical_unit = self._unit(mechanical_turn.context, role, "u-mechanical")
        mechanical_turn.add(mechanical_unit)
        mechanical_result = mechanical_turn.dispatch(mechanical_unit.unit_id)
        self.assertEqual("NEEDS_DECISION", mechanical_result.outcome)
        self.assertEqual("B", mechanical_turn.mechanical_decision(mechanical_unit.unit_id).selected_option)

        def denied(policy_request):
            return orchestration.OperationalResolution(
                "deny", policy_request.binding, policy_request.request,
            )

        deny = self._orchestrator(
            roles=(role,), dispatch=approval_dispatch, escalation_policy=denied,
        )
        deny.begin_turn()
        denied_unit = self._unit(deny.context, role)
        deny.add(denied_unit)
        denied_result = deny.dispatch(denied_unit.unit_id)
        self.assertEqual("BLOCKED", denied_result.outcome)
        self.assertEqual("operational_approval_denied", denied_result.failure_code)
        self.assertEqual("deny", deny.operational_resolution(denied_unit.unit_id).disposition)

    def test_discard_restarts_from_real_task_record_evidence_and_snapshot_facts(self) -> None:
        evidence_id, evidence_bytes = evidence_module.encode_evidence(
            _REPOSITORY,
            _TASK,
            self.base_revision,
            {
                "schema_version": 1,
                "kind": "fixture/observation-v1",
                "producer": {"name": "temporary-test-owner"},
                "created_at": "2025-01-02T03:04:05Z",
                "payload": {"result": "UNAVAILABLE", "command_executed": False},
            },
        )
        evidence_commit = self.store.publish([evidence_bytes])
        self.assertIsNotNone(task_record.TaskRecords(
            self.store, authorize=lambda _request: False,
        ).read(
            evidence_commit,
            task=_TASK,
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        ))
        snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
        publication = snapshots.capture(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            boundary="explicit-handoff",
            selected_evidence=(task_view.EvidenceRef(evidence_id, self.base_revision),),
        )
        self.assertEqual(publication.metadata_commit, self._remote_tip())

        role = self._role()
        engine = self._orchestrator(roles=(role,), snapshots=snapshots)
        old = engine.begin_turn()
        old_context = engine.context
        old.add(self._unit(old_context, role, "u-ephemeral"))
        engine.discard()
        saved_evidence = evidence_module.Evidence(self.store).read(
            publication.metadata_commit, evidence_id,
            task=_TASK, subject=self.base_revision,
        )
        saved_snapshot = snapshots.read(
            publication.metadata_commit, publication.snapshot_id,
            task=_TASK, subject=self.base_revision,
        )
        self.assertNotEqual(
            publication.metadata_commit,
            saved_snapshot["view"]["authority"]["metadata_commit"],
        )
        self.assertEqual("UNAVAILABLE", saved_evidence["payload"]["payload"]["result"])
        self.assertEqual(self.record_publication.record_id, saved_snapshot["view"]["authority"]["record_id"])
        self.assertEqual(self.record_publication.contract_id, saved_snapshot["view"]["authority"]["contract_id"])
        self.assertEqual(evidence_id, saved_snapshot["view"]["evidence"][0]["evidence_id"])

        fresh = self._orchestrator(roles=(role,), snapshots=snapshots)
        fresh_coordinator = fresh.begin_turn()
        self.assertEqual((), fresh_coordinator.units())
        self.assertEqual(old_context.head, fresh.context.head)
        self.assertEqual(old_context.record_id, fresh.context.record_id)
        self.assertNotEqual(old_context.turn_id, fresh.context.turn_id)
        self.assertEqual(publication.metadata_commit, self._remote_tip())

    def test_checkpoint_receipt_mismatch_is_rejected_after_exact_snapshot_readback(self) -> None:
        role = self._role()
        snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
        self._enable_checkpoint_owner_facts()

        for changed_field in ("repository", "metadata_commit", "snapshot_id", "pull_request"):
            def wrong_owner_receipt(request: orchestration.CheckpointRequest, field=changed_field):
                receipt_fields = {
                    "repository": request.repository,
                    "task": request.task,
                    "branch_ref": request.branch_ref,
                    "subject": request.subject,
                    "pull_request": 233,
                    "comment_id": 99,
                    "metadata_commit": request.snapshot.metadata_commit,
                    "record_id": request.record_id,
                    "contract_id": request.contract_id,
                    "snapshot_id": request.snapshot.snapshot_id,
                }
                receipt_fields[field] = {
                    "repository": "other/widgets",
                    "metadata_commit": "f" * 40,
                    "snapshot_id": "e" * 64,
                    "pull_request": 17,
                }[field]
                return task_collaboration.CollaborationReceipt(**receipt_fields)

            # The callback is intentionally not an owner implementation; none
            # of its matching-looking DTO variants may become a handoff.
            engine = self._orchestrator(
                roles=(role,), snapshots=snapshots, checkpoint_writer=wrong_owner_receipt,
                checkpoint_reader=lambda receipt: orchestration.ConfirmedCheckpoint(
                    receipt, 17, "fixture-owner-unreached",
                ),
                checkpoint_observer=self._checkpoint_github_observation,
            )
            engine.begin_turn()
            with self.subTest(changed_field=changed_field), self.assertRaises(
                orchestration.TurnOrchestrationError,
            ) as mismatch:
                engine.handoff("Temporary test handoff; no checkpoint was written.")
            self.assertEqual("checkpoint_binding_mismatch", mismatch.exception.code)
        tip = self._remote_tip()
        self.assertIsNotNone(tip)
        # Snapshot is a valid immutable #144 object even though the untrusted
        # #194-shaped callback did not provide a matching confirmed receipt.
        self.assertEqual(tip, self.store.fetch_tip())

    def test_checkpoint_confirmation_requires_two_exact_owner_reads(self) -> None:
        role = self._role()
        snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
        self._enable_checkpoint_owner_facts()

        def writer_without_reader(_request):
            return self.fail("constructor must refuse an unconfirmed checkpoint writer")

        with self.assertRaises(orchestration.TurnOrchestrationError) as missing:
            self._orchestrator(
                roles=(role,), snapshots=snapshots, checkpoint_writer=writer_without_reader,
            )
        self.assertEqual("checkpoint_confirmation_required", missing.exception.code)

        with self.assertRaises(orchestration.TurnOrchestrationError) as no_observer:
            self._orchestrator(
                roles=(role,), snapshots=snapshots,
                checkpoint_writer=lambda _request: None,
                checkpoint_reader=lambda _receipt: None,
            )
        self.assertEqual("checkpoint_observation_required", no_observer.exception.code)

        self.checkpoint_pr_number = None
        before_unobserved_handoff = self._remote_tip()
        unobserved = self._orchestrator(
            roles=(role,), snapshots=snapshots,
            checkpoint_writer=lambda _request: self.fail("no PR facts means no snapshot or writer"),
            checkpoint_reader=lambda _receipt: None,
            checkpoint_observer=self._checkpoint_github_observation,
        )
        unobserved.begin_turn()
        with self.assertRaises(orchestration.TurnOrchestrationError) as no_pr:
            unobserved.handoff("No owner-authenticated PR is in the current view.")
        self.assertEqual("checkpoint_observation_required", no_pr.exception.code)
        self.assertEqual(before_unobserved_handoff, self._remote_tip())
        self.checkpoint_pr_number = 233

        owner_facts: dict[int, task_collaboration.CollaborationReceipt] = {}
        reader_calls: list[task_collaboration.CollaborationReceipt] = []
        confirmations: list[orchestration.ConfirmedCheckpoint] = []
        next_comment_id = [743]

        def qualified_fixture_writer(request: orchestration.CheckpointRequest):
            # Test-only semantic owner capability: stores exact observed facts
            # locally, with no GitHub request or live checkpoint mutation.
            comment_id = next_comment_id[0]
            next_comment_id[0] += 1
            receipt = task_collaboration.CollaborationReceipt(
                request.repository,
                request.task,
                request.branch_ref,
                request.subject,
                233,
                comment_id=comment_id,
                metadata_commit=request.snapshot.metadata_commit,
                record_id=request.record_id,
                contract_id=request.contract_id,
                snapshot_id=request.snapshot.snapshot_id,
            )
            owner_facts[comment_id] = receipt
            self.checkpoint_previous = task_view.PreviousCheckpoint(
                str(comment_id), receipt.subject, receipt.metadata_commit, receipt.snapshot_id,
            )
            return receipt

        def qualified_fixture_reader(receipt):
            reader_calls.append(receipt)
            observed = owner_facts.get(receipt.comment_id)
            if observed != receipt:
                raise ValueError("fixture owner has no exact matching receipt")
            confirmation = orchestration.ConfirmedCheckpoint(
                observed, 2718, f"fixture-principal-2718-comment-{receipt.comment_id}",
            )
            confirmations.append(confirmation)
            return confirmation

        engine = self._orchestrator(
            roles=(role,), snapshots=snapshots,
            checkpoint_writer=qualified_fixture_writer,
            checkpoint_reader=qualified_fixture_reader,
            checkpoint_observer=self._checkpoint_github_observation,
        )
        engine.begin_turn()
        pending = self._unit(engine.context, role, "u-pending")
        engine.add(pending)
        report = engine.handoff("Temporary fixture summary; no external comment was written.")
        self.assertEqual(2, len(reader_calls))
        self.assertEqual(reader_calls[0], reader_calls[1])
        self.assertEqual(report.checkpoint, confirmations[0].receipt)
        self.assertEqual(2718, confirmations[0].principal_id)
        self.assertEqual(confirmations[0], confirmations[1])
        self.assertEqual(report.snapshot.metadata_commit, report.checkpoint.metadata_commit)
        self.assertEqual(report.snapshot.snapshot_id, report.checkpoint.snapshot_id)
        self.assertEqual(report.checkpoint, report.checkpoint_confirmation.receipt)
        self.assertEqual(2718, report.checkpoint_confirmation.principal_id)
        self.assertEqual("pending", report.unresolved[0].state)
        self.assertIsNone(report.unresolved[0].outcome)

        second_turn = self._orchestrator(
            roles=(role,), snapshots=snapshots,
            checkpoint_writer=qualified_fixture_writer,
            checkpoint_reader=qualified_fixture_reader,
            checkpoint_observer=self._checkpoint_github_observation,
        )
        second_turn.begin_turn()
        second_report = second_turn.handoff("Temporary C0-to-C1 fixture checkpoint.")
        self.assertEqual(4, len(reader_calls))
        self.assertNotEqual(report.checkpoint.comment_id, second_report.checkpoint.comment_id)
        second_snapshot = snapshots.read(
            second_report.snapshot.metadata_commit,
            second_report.snapshot.snapshot_id,
            task=_TASK,
            subject=self.base_revision,
        )
        self.assertEqual(
            str(report.checkpoint.comment_id),
            second_snapshot["view"]["previous_checkpoint"]["checkpoint_id"],
        )

    def _checkpoint_rejection_fixture(self, mode: str) -> str:
        # Independent negative cases use fresh fixtures, not a growing chain
        # of successful snapshots merely to exercise one principal/ref refusal.
        self._enable_checkpoint_owner_facts()
        snapshots = task_view.TaskViewSnapshots(self.store, authorize=lambda _request: True)
        owner_facts: dict[int, task_collaboration.CollaborationReceipt] = {}
        principal_values = iter((2718, 2719))

        def writer(request):
            receipt = task_collaboration.CollaborationReceipt(
                request.repository, request.task, request.branch_ref, request.subject,
                self.checkpoint_pr_number, 743, request.snapshot.metadata_commit,
                request.record_id, request.contract_id, request.snapshot.snapshot_id,
            )
            owner_facts[743] = receipt
            self.checkpoint_previous = task_view.PreviousCheckpoint(
                "999999" if mode == "unrelated" else "743",
                receipt.subject, receipt.metadata_commit, receipt.snapshot_id,
            )
            return receipt

        def reader(receipt):
            self.assertEqual(owner_facts[743], receipt)
            principal = 0 if mode == "invalid" else next(principal_values) if mode == "drift" else 2718
            return orchestration.ConfirmedCheckpoint(receipt, principal, "fixture-owner-observation")

        engine = self._orchestrator(
            snapshots=snapshots, checkpoint_writer=writer, checkpoint_reader=reader,
            checkpoint_observer=self._checkpoint_github_observation,
        )
        engine.begin_turn()
        with self.assertRaises(orchestration.TurnOrchestrationError) as refused:
            engine.handoff("One bounded checkpoint rejection fixture.")
        return refused.exception.code

    def test_checkpoint_principal_drift_is_not_a_confirmed_handoff(self) -> None:
        self.assertEqual("invalid_checkpoint_confirmation", self._checkpoint_rejection_fixture("drift"))

    def test_checkpoint_nonpositive_principal_is_not_a_confirmed_handoff(self) -> None:
        self.assertEqual("invalid_checkpoint_confirmation", self._checkpoint_rejection_fixture("invalid"))

    def test_checkpoint_conflicting_latest_is_not_adopted_after_write(self) -> None:
        self.assertEqual("checkpoint_binding_mismatch", self._checkpoint_rejection_fixture("unrelated"))


if __name__ == "__main__":
    unittest.main()
