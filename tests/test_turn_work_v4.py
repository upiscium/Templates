from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components/agent-core-v4"))

from turn_work import (  # noqa: E402
    DecisionRequest,
    IssueCandidate,
    LeafOutput,
    LeafResult,
    LeafRole,
    OperationalRequest,
    TaskTurnContext,
    TurnBudget,
    TurnCoordinator,
    UnitBudget,
    WorkUnit,
    WorkUnitError,
    select_next_issue,
)


_REPOSITORY = "acme/widgets"
_HEAD = "a" * 40
_TREE = "b" * 40
_HASH = "c" * 64


def context(*, turn_id: str = "turn-1", **overrides: object) -> TaskTurnContext:
    fields: dict[str, object] = {
        "repository": _REPOSITORY,
        "task": "196",
        "branch_ref": "refs/heads/issue-196",
        "turn_id": turn_id,
        "record_id": _HASH,
        "contract_id": "d" * 64,
        "head": _HEAD,
        "tree": _TREE,
        "view_id": "e" * 64,
    }
    fields.update(overrides)
    return TaskTurnContext(**fields)  # type: ignore[arg-type]


def general_role(**overrides: object) -> LeafRole:
    fields: dict[str, object] = {
        "role": "general",
        "provider": "host-provider",
        "model": "host-model",
        "executor_ref": "installed-general",
        "read_only": False,
        "may_delegate": False,
    }
    fields.update(overrides)
    return LeafRole(**fields)  # type: ignore[arg-type]


def unit(
    unit_id: str,
    *,
    ctx: TaskTurnContext | None = None,
    role: LeafRole | None = None,
    read: tuple[str, ...] = ("src",),
    edit: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    expected: tuple[str, ...] = ("report",),
    budget: UnitBudget | None = None,
    **overrides: object,
) -> WorkUnit:
    fields: dict[str, object] = {
        "unit_id": unit_id,
        "context": context() if ctx is None else ctx,
        "role": general_role() if role is None else role,
        "objective": "Inspect the assigned source and return the requested report.",
        "read_scope": read,
        "edit_scope": edit,
        "constraints": ("Stay within the exact lexical scope.",),
        "dependencies": dependencies,
        "expected_outputs": expected,
        "stop_conditions": ("Stop when the report is complete.",),
        "budget": UnitBudget() if budget is None else budget,
    }
    fields.update(overrides)
    return WorkUnit(**fields)  # type: ignore[arg-type]


def completed(unit_id: str, *outputs: str) -> LeafResult:
    names = outputs or ("report",)
    return LeafResult(
        unit_id,
        "COMPLETED",
        "The requested bounded report is complete.",
        tuple(LeafOutput(name, f"{name} checked") for name in names),
    )


def blocked(unit_id: str, failure_code: str = "provider_unavailable") -> LeafResult:
    return LeafResult(
        unit_id, "BLOCKED", "The installed provider did not complete the ticket.",
        failure_code=failure_code,
    )


def approval_request() -> OperationalRequest:
    return OperationalRequest(
        operation_class="repository.write",
        operation_identity="acme/widgets:branch update",
        scope=("src/feature.py",),
        purpose="Apply the requested narrow source update.",
        evidence=("The assigned read-only inspection found the required change.",),
        least_privilege="Write only src/feature.py.",
        safe_alternatives=("Return a patch for host review.",),
        configured_authority="No write authority is currently configured.",
    )


def candidate(
    number: int,
    *,
    repository: str = _REPOSITORY,
    state: str = "open",
    eligibility: str = "eligible",
) -> IssueCandidate:
    fact = f"{number:064x}"[-64:]
    return IssueCandidate(repository, number, state, eligibility, fact)


class TurnWorkV4Test(unittest.TestCase):
    def assertCode(self, code: str, callback: object) -> None:
        with self.assertRaises(WorkUnitError) as caught:
            callback()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)

    def test_context_uses_closed_owner_task_ref_and_oid_grammars(self) -> None:
        valid = context()
        with self.assertRaises(FrozenInstanceError):
            valid.task = "197"  # type: ignore[misc]

        for field, value in (
            ("repository", "acme/widgets/extra"),
            ("repository", "../widgets"),
            ("task", "0196"),
            ("task", True),
            ("branch_ref", "issue-196"),
            ("branch_ref", "refs/heads/bad..ref"),
            ("branch_ref", "refs/heads/../outside"),
            ("head", "A" * 40),
            ("tree", "b" * 64),
            ("record_id", "c" * 40),
            ("view_id", "G" * 64),
            ("turn_id", "../turn"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(WorkUnitError):
                context(**{field: value})

    def test_installed_role_bindings_are_exact_nondelegating_and_readonly_by_default(self) -> None:
        coordinator = TurnCoordinator(
            context(), (general_role(), LeafRole("verifier", "p", "m", "exec-v", True)),
        )
        coordinator.add(unit("u-general"))
        verifier = LeafRole("verifier", "p", "m", "exec-v", True)
        coordinator.add(unit(
            "u-verifier", role=verifier, read=("docs",), edit=(), expected=("review",),
        ))
        with self.assertRaises(WorkUnitError) as not_installed:
            coordinator.validate_unit(unit("u-other-model", role=general_role(model="agent-choice")))
        self.assertEqual("role_not_installed", not_installed.exception.code)

        for role in (
            "explore", "investigator", "verifier", "reviewer", "security-reviewer",
            "architect", "scout",
        ):
            with self.subTest(role=role), self.assertRaises(WorkUnitError):
                LeafRole(role, "p", "m", "exec", False)
        with self.assertRaises(WorkUnitError) as delegate:
            LeafRole("general", "p", "m", "exec", False, True)
        self.assertEqual("delegation_forbidden", delegate.exception.code)
        with self.assertRaises(WorkUnitError):
            LeafRole("unregistered", "p", "m", "exec", True)

    def test_unit_scope_is_bounded_lexical_and_not_an_authority_grant(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        coordinator.add(unit("u-read", read=(".",)))
        with self.assertRaises(WorkUnitError):
            unit("u-root-edit", read=(".",), edit=(".",))
        for path in (
            "/absolute", "../outside", "src/../outside", "src/*", "src/$HOME",
            "src/--upload", ".git/config", ".task-state/record", ".automation/policy",
            "src//empty", "src\\windows",
        ):
            with self.subTest(path=path), self.assertRaises(WorkUnitError):
                unit("bad", read=("src",), edit=(path,))
        with self.assertRaises(WorkUnitError) as outside:
            unit("u-outside", read=("src",), edit=("docs/readme.md",))
        self.assertEqual("edit_outside_read_scope", outside.exception.code)
        readonly = LeafRole("reviewer", "p", "m", "review", True)
        with self.assertRaises(WorkUnitError) as edits:
            unit("u-readonly-edit", role=readonly, edit=("src/a.py",))
        self.assertEqual("read_only_role_has_edits", edits.exception.code)

        # Lexical case is not folded into filesystem identity here.
        case_scoped = TurnCoordinator(context(), (general_role(),))
        case_scoped.add(unit(
            "u-upper-case", read=("src/Feature.py",), edit=("src/Feature.py",),
        ))
        case_scoped.add(unit(
            "u-lower-case", read=("src/feature.py",), edit=("src/feature.py",),
        ))
        with self.assertRaises(FrozenInstanceError):
            case_scoped.units()[0].objective = "changed"  # type: ignore[misc]

    def test_finite_budgets_reject_bool_zero_float_and_oversized_values(self) -> None:
        for build in (
            lambda: UnitBudget(max_seconds=True),
            lambda: UnitBudget(max_output_bytes=0),
            lambda: UnitBudget(max_context_bytes=1.5),
            lambda: UnitBudget(max_seconds=3601),
            lambda: TurnBudget(max_units=True),
            lambda: TurnBudget(max_running=0),
            lambda: TurnBudget(max_dispatches=129),
            lambda: TurnBudget(max_units=1, max_running=2),
        ):
            with self.subTest(build=build), self.assertRaises(WorkUnitError):
                build()
        with self.assertRaises(WorkUnitError) as too_large:
            unit("u-large", objective="x" * 8193)
        self.assertEqual("invalid_unit", too_large.exception.code)
        with self.assertRaises(WorkUnitError) as aggregate:
            unit(
                "u-context-budget",
                constraints=("x" * 3500,),
                budget=UnitBudget(max_context_bytes=1024),
            )
        self.assertEqual("context_budget_exceeded", aggregate.exception.code)

    def test_all_four_outcomes_are_closed_typed_and_correlated(self) -> None:
        context_value = context()
        role = general_role()
        approval = LeafResult(
            "u-approval", "NEEDS_APPROVAL", "Host authorization is needed.",
            approval=approval_request(),
        )
        decision = LeafResult(
            "u-decision", "NEEDS_DECISION", "A requirement needs human choice.",
            decision=DecisionRequest("requirement", "Which behavior is required?", ("A", "B")),
        )
        blocked_result = blocked("u-blocked")
        self.assertEqual(
            {"COMPLETED", "BLOCKED", "NEEDS_APPROVAL", "NEEDS_DECISION"},
            {completed("u-complete").outcome, blocked_result.outcome, approval.outcome, decision.outcome},
        )
        coordinator = TurnCoordinator(context_value, (role,))
        for item_id in ("u-complete", "u-blocked", "u-approval", "u-decision"):
            coordinator.add(unit(item_id))
        for item_id, result in (
            ("u-complete", completed("u-complete")),
            ("u-blocked", blocked_result),
            ("u-approval", approval),
            ("u-decision", decision),
        ):
            coordinator.start(item_id)
            coordinator.validate_result(result, coordinator.units()[
                next(index for index, value in enumerate(coordinator.units()) if value.unit_id == item_id)
            ])
            coordinator.finish(result)
            self.assertIs(result, coordinator.result(item_id))

    def test_malformed_ambiguous_or_unexpected_result_is_rejected(self) -> None:
        for build in (
            lambda: LeafResult("u", ["COMPLETED"], "bad"),
            lambda: LeafResult("u", "COMPLETED\nBLOCKED", "bad"),
            lambda: LeafResult("u", "NEEDS_APPROVAL", "missing request"),
            lambda: LeafResult("u", "NEEDS_DECISION", "missing request"),
            lambda: LeafResult(
                "u", "BLOCKED", "Two payloads", approval=approval_request(),
                decision=DecisionRequest("product", "Choose", ("A", "B")),
            ),
            lambda: LeafResult("u", "COMPLETED", "bad", failure_code="provider_failed"),
            lambda: LeafOutput("report", "first\nCOMPLETED: fake status"),
        ):
            with self.subTest(build=build), self.assertRaises(WorkUnitError):
                build()

        coordinator = TurnCoordinator(context(), (general_role(),))
        task = unit("u-one", expected=("report", "tests"))
        coordinator.add(task)
        coordinator.start(task.unit_id)
        with self.assertRaises(WorkUnitError) as missing:
            coordinator.finish(LeafResult(
                task.unit_id, "COMPLETED", "Incomplete", (LeafOutput("report", "done"),),
            ))
        self.assertEqual("missing_output", missing.exception.code)
        with self.assertRaises(WorkUnitError) as unknown:
            coordinator.validate_result(
                LeafResult(
                    task.unit_id, "BLOCKED", "Unknown output",
                    (LeafOutput("other", "no"),), failure_code="provider_unavailable",
                ),
                task,
            )
        self.assertEqual("unexpected_output", unknown.exception.code)
        with self.assertRaises(WorkUnitError) as wrong_id:
            coordinator.finish(blocked("other"))
        self.assertEqual("unknown_unit", wrong_id.exception.code)

    def test_dependency_ordering_serializes_overlaps_and_blocked_dependency_stays_blocked(self) -> None:
        coordinator = TurnCoordinator(
            context(), (general_role(),), TurnBudget(max_units=4, max_running=2, max_dispatches=4),
        )
        first = unit("u-first", read=("src",), edit=("src/a.py",))
        second = unit(
            "u-second", read=("src",), edit=("src/a.py",), dependencies=("u-first",),
        )
        third = unit(
            "u-third", read=("src",), edit=("src/a.py",), dependencies=("u-second",),
        )
        coordinator.add(first)
        coordinator.add(second)
        coordinator.add(third)
        self.assertEqual((first,), coordinator.ready())
        coordinator.start(first.unit_id)
        coordinator.finish(blocked(first.unit_id))
        self.assertEqual((), coordinator.ready())
        self.assertCode("unit_not_ready", lambda: coordinator.start(second.unit_id))

        ordered = TurnCoordinator(context(), (general_role(),))
        ordered_first = unit("u-ordered-first", read=("src",), edit=("src/a.py",))
        ordered_second = unit(
            "u-ordered-second", read=("src",), edit=("src/a.py",),
            dependencies=("u-ordered-first",),
        )
        ordered.add(ordered_first)
        ordered.add(ordered_second)
        ordered.start(ordered_first.unit_id)
        ordered.finish(completed(ordered_first.unit_id))
        self.assertEqual((ordered_second,), ordered.ready())
        ordered.start(ordered_second.unit_id)
        ordered.finish(completed(ordered_second.unit_id))

        unordered = TurnCoordinator(context(), (general_role(),))
        unordered.add(unit("u-writer", read=("src",), edit=("src/a.py",)))
        with self.assertRaises(WorkUnitError) as conflict:
            unordered.add(unit("u-reader", read=("src/a.py",), expected=("review",)))
        self.assertEqual("scope_conflict", conflict.exception.code)

        # Scope containment uses path components, not string prefixes.
        sibling = TurnCoordinator(context(), (general_role(),))
        sibling.add(unit("u-src", read=("src",), edit=("src/a.py",)))
        sibling.add(unit("u-src2", read=("src2",), edit=("src2/a.py",)))

    def test_max_units_lifetime_dispatch_ids_and_fresh_turn_are_ephemeral(self) -> None:
        coordinator = TurnCoordinator(
            context(), (general_role(),), TurnBudget(max_units=2, max_running=2, max_dispatches=1),
        )
        first = unit("u-reused")
        coordinator.add(first)
        coordinator.add(unit("u-spare", read=("docs",), expected=("docs",)))
        coordinator.start(first.unit_id)
        coordinator.finish(blocked(first.unit_id))
        self.assertEqual((), coordinator.ready())
        self.assertCode("duplicate_unit_id", lambda: coordinator.add(first))
        with self.assertRaises(WorkUnitError) as full:
            coordinator.add(unit("u-third", read=("tests",)))
        self.assertEqual("max_units_exceeded", full.exception.code)

        coordinator.discard()
        self.assertEqual((), coordinator.units())
        self.assertEqual((), coordinator.ready())
        self.assertIsNone(coordinator.result("u-reused"))
        self.assertCode("turn_discarded", lambda: coordinator.add(first))
        self.assertCode("turn_discarded", lambda: coordinator.start("u-reused"))

        next_context = context(turn_id="turn-2", head="f" * 40, tree="0" * 40)
        fresh = TurnCoordinator(next_context, (general_role(),))
        self.assertEqual((), fresh.units())
        fresh.add(unit("u-reused", ctx=next_context))
        self.assertEqual(("u-reused",), tuple(item.unit_id for item in fresh.units()))

    def test_pending_ticket_and_leaf_cannot_mutate_context_or_role_authority(self) -> None:
        host_role = general_role()
        coordinator = TurnCoordinator(context(), (host_role,))
        ticket = unit("u-role", role=host_role)
        coordinator.add(ticket)
        self.assertCode("turn_context_mismatch", lambda: coordinator.validate_unit(
            unit("u-wrong-context", ctx=context(turn_id="another-turn")),
        ))
        with self.assertRaises(WorkUnitError) as missing_dependency:
            coordinator.add(unit("u-missing-dep", dependencies=("u-absent",)))
        self.assertEqual("missing_dependency", missing_dependency.exception.code)

    def test_explicit_issue_is_descriptive_and_checks_feed_identity(self) -> None:
        eligible = candidate(17)
        ineligible = candidate(18, eligibility="ineligible")
        unknown = candidate(19, eligibility="unknown")
        one = select_next_issue(_REPOSITORY, (eligible,), explicit_issue=17)
        self.assertEqual(("explicit", 17, "explicit_eligible"), (one.kind, one.issue, one.reason_code))
        many = select_next_issue(_REPOSITORY, (eligible, ineligible), explicit_issue=18)
        self.assertEqual(("explicit", 18, "explicit_ineligible"), (many.kind, many.issue, many.reason_code))
        uncertain = select_next_issue(_REPOSITORY, (unknown,), explicit_issue=19)
        self.assertEqual("explicit_eligibility_unknown", uncertain.reason_code)
        absent = select_next_issue(_REPOSITORY, (eligible,), explicit_issue=999)
        self.assertEqual(("needs_decision", "explicit_issue_not_observed"), (absent.kind, absent.reason_code))
        closed = select_next_issue(
            _REPOSITORY, (candidate(20, state="closed"),), explicit_issue=20,
        )
        self.assertEqual("explicit_issue_not_open", closed.reason_code)
        wrong_repo = select_next_issue(
            _REPOSITORY, (candidate(17, repository="other/widgets"),), explicit_issue=17,
        )
        self.assertEqual("invalid_candidate_feed", wrong_repo.reason_code)
        duplicate = select_next_issue(_REPOSITORY, (eligible, eligible))
        self.assertEqual("duplicate_candidate", duplicate.reason_code)

    def test_autonomous_issue_choice_requires_complete_unambiguous_facts_not_priority(self) -> None:
        sole = select_next_issue(_REPOSITORY, (candidate(42),))
        self.assertEqual(("selected", 42, (42,)), (sole.kind, sole.issue, sole.candidates))
        multiple = select_next_issue(_REPOSITORY, (candidate(99), candidate(2)))
        self.assertEqual("needs_decision", multiple.kind)
        self.assertIsNone(multiple.issue)
        self.assertEqual("multiple_eligible_candidates", multiple.reason_code)
        self.assertEqual((99, 2), multiple.candidates)
        uncertain = select_next_issue(_REPOSITORY, (candidate(42), candidate(4, eligibility="unknown")))
        self.assertEqual("candidate_eligibility_unknown", uncertain.reason_code)
        self.assertIsNone(uncertain.issue)
        incomplete = select_next_issue(_REPOSITORY, (candidate(42),), complete=False)
        self.assertEqual("candidate_feed_incomplete", incomplete.reason_code)
        self.assertIsNone(incomplete.issue)
        none = select_next_issue(
            _REPOSITORY,
            (candidate(1, state="closed"), candidate(2, eligibility="ineligible")),
        )
        self.assertEqual(("none", None, "no_eligible_open_candidates"), (none.kind, none.issue, none.reason_code))
        malformed = select_next_issue(_REPOSITORY, (object(),))  # type: ignore[arg-type]
        self.assertEqual(("needs_decision", None, "invalid_candidate_feed"), (
            malformed.kind, malformed.issue, malformed.reason_code,
        ))

    def test_empty_dependency_root_ticket_validates_starts_and_finishes(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        root = unit(
            "u-root-empty-deps", read=("src",), edit=("src/root.py",), dependencies=(),
        )

        coordinator.validate_unit(root)
        coordinator.add(root)
        self.assertEqual((root,), coordinator.ready())
        self.assertEqual(root, coordinator.start(root.unit_id))
        result = completed(root.unit_id)
        coordinator.validate_result(result, root)
        coordinator.finish(result)
        self.assertIs(result, coordinator.result(root.unit_id))

    def test_output_budget_cannot_preclude_a_minimal_terminal_failure(self) -> None:
        for limit in (1, 128, 511):
            with self.subTest(limit=limit), self.assertRaises(WorkUnitError):
                UnitBudget(max_output_bytes=limit)
        coordinator = TurnCoordinator(context(), (general_role(),))
        ticket = unit("u-" + "x" * 60, budget=UnitBudget(max_output_bytes=512))
        coordinator.add(ticket)
        coordinator.start(ticket.unit_id)
        coordinator.finish(blocked(ticket.unit_id))
        self.assertEqual("BLOCKED", coordinator.result(ticket.unit_id).outcome)

    def test_edit_may_be_under_any_read_prefix_but_not_outside_all_prefixes(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        allowed = unit(
            "u-multiple-read-prefixes",
            read=("src", "tests"),
            edit=("src/one.py",),
        )
        coordinator.validate_unit(allowed)
        coordinator.add(allowed)

        with self.assertRaises(WorkUnitError) as outside:
            unit(
                "u-outside-every-read-prefix",
                read=("src", "tests"),
                edit=("docs/one.py",),
            )
        self.assertEqual("edit_outside_read_scope", outside.exception.code)

    def test_duplicate_host_role_names_are_rejected_even_with_different_bindings(self) -> None:
        for roles in (
            (general_role(), general_role()),
            (general_role(), general_role(model="another-host-model")),
        ):
            with self.subTest(roles=roles):
                self.assertCode(
                    "invalid_role_roster",
                    lambda roles=roles: TurnCoordinator(context(), roles),
                )

    def test_context_and_ticket_shape_are_revalidated_after_object_level_bypass(self) -> None:
        valid_context = context()
        forged_context = object.__new__(TaskTurnContext)
        for field in (
            "repository", "task", "branch_ref", "turn_id", "record_id",
            "contract_id", "head", "tree", "view_id",
        ):
            object.__setattr__(forged_context, field, getattr(valid_context, field))
        object.__setattr__(forged_context, "task", "0196")

        with self.assertRaises(WorkUnitError):
            unit("u-forged-at-construction", ctx=forged_context)

        coordinator = TurnCoordinator(valid_context, (general_role(),))
        ticket = unit("u-forged-after-construction", ctx=valid_context)
        object.__setattr__(ticket, "context", forged_context)
        with self.assertRaises(WorkUnitError) as revalidated:
            coordinator.validate_unit(ticket)
        self.assertEqual("invalid_context", revalidated.exception.code)

    def test_blocked_ticket_allows_new_id_correction_without_dependency_but_stays_terminal(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        failed = unit("u-first-attempt", read=("src",), edit=("src/feature.py",))
        coordinator.add(failed)
        coordinator.start(failed.unit_id)
        failed_result = blocked(failed.unit_id)
        coordinator.finish(failed_result)

        correction = unit(
            "u-correction", read=("src",), edit=("src/feature.py",), dependencies=(),
        )
        coordinator.validate_unit(correction)
        coordinator.add(correction)
        self.assertEqual((correction,), coordinator.ready())
        coordinator.start(correction.unit_id)
        correction_result = completed(correction.unit_id)
        coordinator.finish(correction_result)

        self.assertIs(failed_result, coordinator.result(failed.unit_id))
        self.assertIs(correction_result, coordinator.result(correction.unit_id))
        self.assertCode("duplicate_unit_id", lambda: coordinator.add(failed))
        self.assertCode("unit_not_ready", lambda: coordinator.start(failed.unit_id))

    def test_blocked_dependency_never_becomes_ready_after_independent_correction(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        failed = unit("u-blocked-parent", read=("src",), edit=("src/shared.py",))
        dependent = unit(
            "u-blocked-dependent", read=("src",), edit=("src/shared.py",),
            dependencies=(failed.unit_id,),
        )
        coordinator.add(failed)
        coordinator.add(dependent)
        coordinator.start(failed.unit_id)
        parent_result = blocked(failed.unit_id)
        coordinator.finish(parent_result)
        self.assertEqual((), coordinator.ready())
        self.assertIsNone(coordinator.result(dependent.unit_id))

        correction = unit(
            "u-independent-correction", read=("docs",), edit=("docs/fix.py",),
            dependencies=(),
        )
        coordinator.add(correction)
        self.assertEqual((correction,), coordinator.ready())
        coordinator.start(correction.unit_id)
        coordinator.finish(completed(correction.unit_id))
        self.assertEqual((), coordinator.ready())
        self.assertIs(parent_result, coordinator.result(failed.unit_id))
        self.assertIsNone(coordinator.result(dependent.unit_id))
        self.assertCode("unit_not_ready", lambda: coordinator.start(dependent.unit_id))

    def test_failed_dependency_reservation_does_not_preclude_same_scope_correction(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        first = unit("u-failed", read=("src",), edit=("src/feature.py",))
        dependent = unit(
            "u-unstartable", read=("src",), edit=("src/feature.py",),
            dependencies=(first.unit_id,),
        )
        coordinator.add(first)
        coordinator.add(dependent)
        coordinator.start(first.unit_id)
        coordinator.finish(blocked(first.unit_id))
        correction = unit("u-new-correction", read=("src",), edit=("src/feature.py",))
        coordinator.add(correction)
        self.assertEqual((correction,), coordinator.ready())
        coordinator.start(correction.unit_id)
        coordinator.finish(completed(correction.unit_id))
        self.assertEqual("BLOCKED", coordinator.result(first.unit_id).outcome)
        self.assertIsNone(coordinator.result(dependent.unit_id))
        self.assertEqual((), coordinator.ready())
        self.assertCode("unit_not_ready", lambda: coordinator.start(dependent.unit_id))

    def test_completed_writer_does_not_block_dependency_free_readonly_followup(self) -> None:
        writer_role = general_role()
        reviewer_role = LeafRole("reviewer", "host-provider", "review-model", "installed-review", True)
        coordinator = TurnCoordinator(context(), (writer_role, reviewer_role))
        writer = unit(
            "u-completed-writer", role=writer_role, read=("src",), edit=("src/file.py",),
        )
        coordinator.add(writer)
        coordinator.start(writer.unit_id)
        writer_result = completed(writer.unit_id)
        coordinator.finish(writer_result)

        reviewer = unit(
            "u-readonly-review", role=reviewer_role, read=("src",), expected=("review",),
            dependencies=(),
        )
        coordinator.validate_unit(reviewer)
        coordinator.add(reviewer)
        self.assertEqual((reviewer,), coordinator.ready())
        coordinator.start(reviewer.unit_id)
        review_result = completed(reviewer.unit_id, "review")
        coordinator.validate_result(review_result, reviewer)
        coordinator.finish(review_result)
        self.assertIs(writer_result, coordinator.result(writer.unit_id))
        self.assertIs(review_result, coordinator.result(reviewer.unit_id))

    def test_max_running_limits_disjoint_scoped_tickets_without_dispatching_threads(self) -> None:
        coordinator = TurnCoordinator(
            context(), (general_role(),), TurnBudget(max_units=3, max_running=2, max_dispatches=3),
        )
        first = unit("u-parallel-one", read=("src/one.py",), edit=("src/one.py",))
        second = unit("u-parallel-two", read=("tests/two.py",), edit=("tests/two.py",))
        third = unit("u-parallel-three", read=("docs/three.md",), edit=("docs/three.md",))
        for ticket in (first, second, third):
            coordinator.add(ticket)

        self.assertEqual((first, second), coordinator.ready())
        coordinator.start(first.unit_id)
        coordinator.start(second.unit_id)
        self.assertEqual((), coordinator.ready())
        coordinator.finish(completed(first.unit_id))
        self.assertEqual((third,), coordinator.ready())
        coordinator.start(third.unit_id)
        coordinator.finish(completed(second.unit_id))
        coordinator.finish(completed(third.unit_id))

    def test_dispatch_budget_is_consumed_by_failure_and_old_ticket_does_not_resume(self) -> None:
        coordinator = TurnCoordinator(
            context(), (general_role(),), TurnBudget(max_units=2, max_running=1, max_dispatches=1),
        )
        failed = unit("u-budget-failure", read=("src",), edit=("src/file.py",), dependencies=())
        coordinator.add(failed)
        coordinator.start(failed.unit_id)
        failed_result = blocked(failed.unit_id)
        coordinator.finish(failed_result)

        correction = unit(
            "u-budget-correction", read=("src",), edit=("src/file.py",), dependencies=(),
        )
        coordinator.add(correction)
        self.assertEqual((), coordinator.ready())
        self.assertCode("dispatch_limit_reached", lambda: coordinator.start(correction.unit_id))
        self.assertCode("duplicate_unit_id", lambda: coordinator.add(failed))
        self.assertIs(failed_result, coordinator.result(failed.unit_id))

    def test_blocked_provider_ticket_cannot_resume_or_retarget_its_installed_model(self) -> None:
        installed = general_role(model="one-host-installed-model")
        coordinator = TurnCoordinator(context(), (installed,))
        ticket = unit("u-provider-bound", role=installed, read=("src",))
        coordinator.add(ticket)
        coordinator.start(ticket.unit_id)
        failed_result = blocked(ticket.unit_id)
        coordinator.finish(failed_result)

        self.assertCode("duplicate_unit_id", lambda: coordinator.add(ticket))
        self.assertCode("unit_not_ready", lambda: coordinator.start(ticket.unit_id))
        alternate = unit("u-provider-retarget", role=general_role(model="caller-selected-model"))
        self.assertCode("role_not_installed", lambda: coordinator.add(alternate))
        self.assertIs(failed_result, coordinator.result(ticket.unit_id))

    def test_outcomes_are_single_direct_strings_not_escaped_or_multistatus_json(self) -> None:
        coordinator = TurnCoordinator(context(), (general_role(),))
        decision = DecisionRequest("requirement", "Which behavior is required?", ("A", "B"))
        direct = (
            completed("u-direct-completed"),
            blocked("u-direct-blocked"),
            LeafResult(
                "u-direct-approval", "NEEDS_APPROVAL", "Independent host approval needed.",
                approval=approval_request(),
            ),
            LeafResult(
                "u-direct-decision", "NEEDS_DECISION", "Human choice needed.",
                decision=decision,
            ),
        )
        for result in direct:
            coordinator.add(unit(result.unit_id))
            coordinator.validate_result(result, coordinator.units()[-1])
        self.assertEqual(
            {"COMPLETED", "BLOCKED", "NEEDS_APPROVAL", "NEEDS_DECISION"},
            {result.outcome for result in direct},
        )

        for outcome in (
            '["COMPLETED", "BLOCKED"]',
            r'"COMPLETED"',
            r'"COMPLETED\nBLOCKED"',
            '{"outcome":"COMPLETED"}',
            "COMPLETED\nBLOCKED",
        ):
            with self.subTest(outcome=outcome), self.assertRaises(WorkUnitError):
                LeafResult("u-malformed-status", outcome, "Not a single outcome.")
        with self.assertRaises(WorkUnitError):
            LeafResult(
                "u-multiple-payloads", "BLOCKED", "Ambiguous terminal payloads.",
                approval=approval_request(),
                decision=decision,
            )

    def test_issue_selection_has_no_work_unit_or_evidence_authority_binding(self) -> None:
        selection = select_next_issue(_REPOSITORY, (candidate(77),))
        self.assertEqual(("selected", 77), (selection.kind, selection.issue))
        self.assertNotIn("unit_id", IssueCandidate.__dataclass_fields__)
        self.assertNotIn("unit_id", type(selection).__dataclass_fields__)
        self.assertFalse(hasattr(selection, "unit_id"))


if __name__ == "__main__":
    unittest.main()
