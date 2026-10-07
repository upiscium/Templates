from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import operation_prerequisites as prerequisites  # noqa: E402
import evidence as evidence_module  # noqa: E402
import metadata_codec as codec  # noqa: E402
import task_record  # noqa: E402
import task_view  # noqa: E402
import test_metadata_ref as fixtures  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "192"
_BRANCH = "refs/heads/issue-192"
_SUBJECT = "a" * 40


class OperationPrerequisitesV4Test(unittest.TestCase):
    """Operation-local policy fixtures; no production operation registry."""

    def _view(self) -> dict[str, Any]:
        contract_id = "c" * 64
        record_id, _ = task_record.encode_record(
            _REPOSITORY,
            _TASK,
            _SUBJECT,
            {"schema_version": 1, "branch_ref": _BRANCH, "contract_id": contract_id},
        )
        return {
            "schema_version": 1,
            "repository": _REPOSITORY,
            "task": _TASK,
            "subject": _SUBJECT,
            "branch_ref": _BRANCH,
            "git": {
                "head": _SUBJECT,
                "tree": "d" * 40,
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
                "remote_head": {"state": "not_requested"},
                "submodule_worktrees": "not_inspected",
                "staged_gitlinks": 0,
            },
            "authority": {
                "state": "observed",
                "metadata_commit": "b" * 40,
                "record_id": record_id,
                "contract_id": contract_id,
                "base_revision": _SUBJECT,
                "branch_ref": _BRANCH,
                "disposition": None,
            },
            "evidence": [],
            "github": {"state": "not_requested"},
            "previous_checkpoint": {"state": "not_requested"},
        }

    def _mutation_view(self) -> dict[str, Any]:
        view = self._view()
        view["git"]["default_branch"] = {
            "state": "observed",
            "ref": "refs/heads/main",
            "head_oid": "f" * 40,
            "relation": "ahead",
            "default_only_commits": 0,
            "task_only_commits": 1,
        }
        view["git"]["remote_head"] = {"state": "observed", "head_oid": "e" * 40}
        return view

    def _request(self, operation: str = "inspect", parameters: dict[str, Any] | None = None) -> prerequisites.OperationRequest:
        return prerequisites.OperationRequest(
            _REPOSITORY,
            _TASK,
            _BRANCH,
            _SUBJECT,
            operation,
            {} if parameters is None else parameters,
        )

    @staticmethod
    def _policy(
        operation: str = "inspect",
        checks: tuple[prerequisites.CheckSpec, ...] | None = None,
        *,
        authority_mode: str = "execution",
    ) -> prerequisites.OperationPolicy:
        selected = (
            (prerequisites.CheckSpec("local_facts", "local_owner"),)
            if checks is None else checks
        )
        return prerequisites.OperationPolicy(operation, "fixture_policy", authority_mode, selected)

    @staticmethod
    def _result(
        context: prerequisites.CheckContext,
        outcome: str = "SATISFIED",
        *,
        reason: str = "fixture_satisfied",
        observed: dict[str, Any] | None = None,
        expected: dict[str, Any] | None = None,
        convergence: str = "none",
        binding: prerequisites.CheckBinding | None = None,
    ) -> prerequisites.CheckResult:
        return prerequisites.CheckResult(
            context.binding if binding is None else binding,
            outcome,
            reason,
            {} if observed is None else observed,
            {} if expected is None else expected,
            convergence,
        )

    def _mutation_parameters(self, operation: str) -> dict[str, Any]:
        if operation == "git.commit":
            return {"target_ref": _BRANCH, "scope": ["tracked.txt"], "force": False}
        if operation == "git.push":
            return {
                "remote_name": "origin", "target_ref": _BRANCH,
                "expected_remote_head": "e" * 40, "force": False,
            }
        if operation == "github.create_draft_pr":
            return {"head_ref": _BRANCH, "base_ref": "refs/heads/main", "draft": True}
        if operation == "github.comment_factual":
            return {"pull_request": 23, "body": "Implemented the bounded Task 192 change."}
        return {}

    def _mutation_request(self, operation: str, **overrides: Any) -> prerequisites.OperationRequest:
        parameters = self._mutation_parameters(operation)
        parameters.update(overrides)
        return self._request(operation, parameters)

    def _fixture_mutation_guard(self, context: prerequisites.CheckContext) -> prerequisites.CheckResult:
        """Fixture-only operation guard which inspects exact request and Task facts."""
        request = json.loads(context.request_bytes)
        view = json.loads(context.view_bytes)
        operation = request["operation"]
        parameters = request["parameters"]
        git = view["git"]
        status = git["status"]
        if not git["branch_matches_task"] or request["branch_ref"] != view["branch_ref"]:
            return self._result(
                context, "IDENTITY_CONFLICT", reason="task_branch_mismatch",
                observed={"branch_ref": view["branch_ref"]},
                expected={"branch_ref": request["branch_ref"]},
            )
        if any(status[field] for field in ("conflicts", "index_changes", "worktree_changes", "untracked")):
            return self._result(
                context, "MISSING_PREREQUISITE", reason="worktree_not_clean",
                observed={"status": status},
                expected={"conflicts": 0, "index_changes": 0, "worktree_changes": 0, "untracked": 0},
            )
        default = git["default_branch"]
        if operation in {"git.commit", "git.push"}:
            if default["state"] != "observed" or default["relation"] == "diverged":
                return self._result(
                    context, "MISSING_PREREQUISITE", reason="default_branch_not_converged",
                    observed={"state": default["state"], "relation": default.get("relation")},
                    expected={"state": "observed", "relation": "ahead"},
                )
            if parameters.get("target_ref") != request["branch_ref"] or parameters.get("target_ref") == default["ref"]:
                return self._result(
                    context, "IDENTITY_CONFLICT", reason="target_ref_mismatch",
                    observed={"target_ref": parameters.get("target_ref")},
                    expected={"target_ref": request["branch_ref"]},
                )
            if parameters.get("force") is True:
                return self._result(
                    context, "UNSAFE_TO_CONVERGE", reason="force_update_not_allowed",
                    observed={"force": True}, expected={"force": False}, convergence="unsafe",
                )
        if operation == "git.commit":
            scope = parameters.get("scope")
            if type(scope) is not list or not scope or len(scope) > 4 or any(type(path) is not str for path in scope):
                return self._result(
                    context, "AUTHORITY_CONFLICT", reason="commit_scope_unbounded",
                    observed={"scope_count": len(scope) if type(scope) is list else -1},
                    expected={"maximum_scope_count": 4},
                )
            protected = {".github/workflows/release.yml", "secrets.env", "private/credentials.json"}
            if protected.intersection(scope):
                return self._result(
                    context, "AUTHORITY_CONFLICT", reason="protected_path_in_scope",
                    observed={"protected_path_requested": True}, expected={"protected_path_requested": False},
                )
            return self._result(context, observed={"scope_count": len(scope), "index_clean": True})
        if operation == "git.push":
            remote = git["remote_head"]
            if (
                remote["state"] != "observed"
                or parameters.get("expected_remote_head") != remote.get("head_oid")
                or parameters.get("remote_name") != "origin"
            ):
                return self._result(
                    context, "IDENTITY_CONFLICT", reason="remote_expectation_mismatch",
                    observed={"remote_head": remote.get("head_oid")},
                    expected={"remote_head": parameters.get("expected_remote_head")},
                )
            return self._result(context, observed={"force": False, "target_ref": parameters["target_ref"]})
        if operation == "github.create_draft_pr":
            if (
                parameters.get("head_ref") != request["branch_ref"]
                or parameters.get("base_ref") != default.get("ref")
                or parameters.get("draft") is not True
            ):
                return self._result(
                    context, "IDENTITY_CONFLICT", reason="draft_pr_target_mismatch",
                    observed={"head_ref": parameters.get("head_ref"), "base_ref": parameters.get("base_ref")},
                    expected={"head_ref": request["branch_ref"], "base_ref": default.get("ref")},
                )
            return self._result(context, observed={"draft": True})
        if operation == "github.comment_factual":
            body = parameters.get("body")
            if type(parameters.get("pull_request")) is not int or parameters["pull_request"] < 1:
                return self._result(context, "IDENTITY_CONFLICT", reason="pull_request_target_invalid")
            if type(body) is not str or len(body) > 256:
                return self._result(
                    context, "AUTHORITY_CONFLICT", reason="comment_scope_unbounded",
                    observed={"body_length": len(body) if type(body) is str else -1},
                    expected={"maximum_body_length": 256},
                )
            if any(secret in body.lower() for secret in ("token=", "password=", "ghp_", "secret=")):
                return self._result(
                    context, "AUTHORITY_CONFLICT", reason="sensitive_comment_content",
                    observed={"sensitive_content": True}, expected={"sensitive_content": False},
                )
            return self._result(context, observed={"factual_comment_bounded": True})
        return self._result(context, "AUTHORITY_CONFLICT", reason="fixture_operation_unhandled")

    def test_local_operation_uses_only_declared_check_and_needs_no_github(self) -> None:
        calls: list[str] = []

        def local(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            calls.append(context.binding.check_id)
            view = json.loads(context.view_bytes)
            self.assertEqual({"state": "unavailable", "error_code": "callback_failed"}, view["github"])
            return self._result(context, observed={"dirty_counts": view["git"]["status"]})

        policy = self._policy(checks=(prerequisites.CheckSpec("local_facts", "fixture"),))
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={
                "local_facts": local,
                "github_probe": lambda _context: self.fail("undeclared GitHub callback ran"),
                "admin_probe": lambda _context: self.fail("undeclared Admin callback ran"),
                "project_tool": lambda _context: self.fail("undeclared project tool callback ran"),
            },
        )
        unavailable_github_view = self._view()
        unavailable_github_view["github"] = {"state": "unavailable", "error_code": "callback_failed"}
        diagnosis = engine.diagnose(self._request(), unavailable_github_view)
        self.assertEqual("PREREQUISITES_SATISFIED", diagnosis["result"])
        self.assertFalse(diagnosis["human_decision_required"])
        self.assertFalse(diagnosis["human_authority_required"])
        self.assertEqual(["local_facts"], calls)
        self.assertNotIn("next_action", diagnosis)
        self.assertNotIn("status", diagnosis)
        prerequisites.encode_diagnosis(diagnosis)

    def test_normal_bounded_mutations_are_not_promoted_to_human_decisions(self) -> None:
        operations = ("git.commit", "git.push", "github.create_draft_pr", "github.comment_factual")
        for operation in operations:
            with self.subTest(operation=operation):
                specs = [
                    prerequisites.CheckSpec("identity", "git_owner"),
                    prerequisites.CheckSpec("operation_guard", "operation_owner"),
                ]
                registry = {
                    "identity": lambda context: self._result(context),
                    "operation_guard": self._fixture_mutation_guard,
                }
                if operation.startswith("github."):
                    specs.append(prerequisites.CheckSpec("github_capability", "github_host", "github"))
                    registry["github_capability"] = lambda context: self._result(
                        context, observed={"fixture_capability_installed": True},
                    )
                specs.sort(key=lambda spec: spec.check_id)
                policy = self._policy(operation, tuple(specs))
                engine = prerequisites.OperationPrerequisites(
                    policy,
                    checks=registry,
                )
                diagnosis = engine.diagnose(self._mutation_request(operation), self._mutation_view())
                self.assertEqual("PREREQUISITES_SATISFIED", diagnosis["result"])
                self.assertFalse(diagnosis["human_decision_required"])
                self.assertFalse(diagnosis["human_authority_required"])
                self.assertEqual(operation, diagnosis["operation"])

    def test_positive_diagnosis_is_not_an_execution_or_recovery_capability(self) -> None:
        engine = prerequisites.OperationPrerequisites(
            self._policy("git.commit", (prerequisites.CheckSpec("guard", "fixture"),)),
            checks={
                "guard": lambda context: self._result(context, convergence="mechanical"),
                "execute": lambda _context: self.fail("diagnosis invoked mutation"),
                "recover": lambda _context: self.fail("diagnosis invoked recovery"),
            },
        )
        diagnosis = engine.diagnose(self._request("git.commit", {"approve": True}), self._view())
        self.assertEqual("PREREQUISITES_SATISFIED", diagnosis["result"])
        self.assertTrue(diagnosis["mechanical_convergence_candidate"])
        self.assertFalse(diagnosis["human_authority_required"])
        self.assertNotIn("authorization", diagnosis)
        self.assertNotIn("automatic_convergence_safe", diagnosis)
        self.assertFalse(hasattr(engine, "execute"))
        self.assertFalse(hasattr(engine, "recover"))

    def test_semantic_ambiguity_is_distinct_from_mechanical_convergence_and_history_loss(self) -> None:
        cases = (
            ("fast_forward", "MISSING_PREREQUISITE", "mechanical", False, True),
            ("clean_merge", "SATISFIED", "mechanical", False, True),
            ("mechanical_conflict", "MISSING_PREREQUISITE", "mechanical", False, True),
            ("semantic_ambiguity", "SEMANTIC_DECISION_REQUIRED", "semantic", True, False),
            ("history_loss", "UNSAFE_TO_CONVERGE", "unsafe", False, False),
        )
        for label, outcome, convergence, human, safe in cases:
            with self.subTest(classification=label):
                policy = self._policy(checks=(prerequisites.CheckSpec("merge_guard", "fixture", "convergence"),))
                engine = prerequisites.OperationPrerequisites(
                    policy,
                    checks={"merge_guard": lambda context, o=outcome, c=convergence: self._result(
                        context, o, reason=label, convergence=c,
                    )},
                )
                diagnosis = engine.diagnose(self._request(), self._view())
                self.assertEqual(outcome if outcome != "SATISFIED" else "PREREQUISITES_SATISFIED", diagnosis["result"])
                self.assertEqual(human, diagnosis["human_decision_required"])
                self.assertEqual(safe, diagnosis["mechanical_convergence_candidate"])

    def test_fixture_guards_are_policy_owned_not_kernel_operation_defaults(self) -> None:
        cases = (
            ("default_branch_diverged", "git.push", "MISSING_PREREQUISITE"),
            ("force_push_required", "git.push", "UNSAFE_TO_CONVERGE"),
            ("secret_in_factual_comment", "github.comment_factual", "AUTHORITY_CONFLICT"),
            ("unbounded_commit_request", "git.commit", "AUTHORITY_CONFLICT"),
            ("protected_commit_path", "git.commit", "AUTHORITY_CONFLICT"),
            ("conflicting_index", "git.commit", "MISSING_PREREQUISITE"),
        )
        for label, operation, outcome in cases:
            with self.subTest(guard=label):
                policy = self._policy(operation, (prerequisites.CheckSpec("fixture_guard", "test_fixture"),))
                request = self._mutation_request(operation)
                view = self._mutation_view()
                if label == "default_branch_diverged":
                    view["git"]["default_branch"].update({
                        "relation": "diverged", "default_only_commits": 1, "task_only_commits": 1,
                    })
                elif label == "force_push_required":
                    request = self._mutation_request(operation, force=True)
                elif label == "secret_in_factual_comment":
                    request = self._mutation_request(operation, body="Token=ghp_fixture-secret")
                elif label == "unbounded_commit_request":
                    request = self._mutation_request(operation, scope=[f"file-{index}.txt" for index in range(5)])
                elif label == "protected_commit_path":
                    request = self._mutation_request(operation, scope=[".github/workflows/release.yml"])
                elif label == "conflicting_index":
                    view["git"]["status"]["conflicts"] = 1
                engine = prerequisites.OperationPrerequisites(
                    policy,
                    checks={"fixture_guard": self._fixture_mutation_guard},
                )
                diagnosis = engine.diagnose(request, view)
                self.assertEqual(outcome, diagnosis["result"])
                self.assertFalse(diagnosis["human_decision_required"])
                self.assertNotIn("next_action", diagnosis)

    def test_missing_tool_dependency_gates_only_the_declaring_operation(self) -> None:
        verify = self._policy("project.verify", (
            prerequisites.CheckSpec("project_tool", "project_tool_owner", "project_tool"),
        ))
        missing = prerequisites.OperationPrerequisites(verify, checks={})
        unavailable = missing.diagnose(self._request("project.verify"), self._view())
        self.assertEqual("UNAVAILABLE_DEPENDENCY", unavailable["result"])
        self.assertEqual("check_unavailable", unavailable["checks"][0]["reason_code"])

        admin = self._policy("admin.cutover", (
            prerequisites.CheckSpec("admin_authority", "admin_host", "admin"),
        ))

        def failing_host(_context: prerequisites.CheckContext) -> object:
            raise RuntimeError("private callback detail")

        admin_failure = prerequisites.OperationPrerequisites(
            admin,
            checks={"admin_authority": failing_host},
        ).diagnose(self._request("admin.cutover"), self._view())
        self.assertEqual("UNAVAILABLE_DEPENDENCY", admin_failure["result"])
        self.assertEqual("check_callback_failed", admin_failure["checks"][0]["reason_code"])

        github = self._policy("github.create_draft_pr", (
            prerequisites.CheckSpec("github_reader", "github_host", "github"),
        ))
        github_failure = prerequisites.OperationPrerequisites(
            github,
            checks={"github_reader": failing_host},
        ).diagnose(self._request("github.create_draft_pr"), self._view())
        self.assertEqual("UNAVAILABLE_DEPENDENCY", github_failure["result"])

        local_policy = self._policy("edit")
        local = prerequisites.OperationPrerequisites(
            local_policy,
            checks={
                "local_facts": lambda context: self._result(context),
                "admin_authority": lambda _context: self.fail("undeclared Admin callback ran"),
                "project_tool": lambda _context: self.fail("undeclared tool callback ran"),
                "github_reader": lambda _context: self.fail("undeclared GitHub callback ran"),
            },
        )
        unavailable_github_view = self._view()
        unavailable_github_view["github"] = {"state": "unavailable", "error_code": "callback_failed"}
        self.assertEqual("PREREQUISITES_SATISFIED", local.diagnose(
            self._request("edit"), unavailable_github_view,
        )["result"])

    def test_human_owned_policy_requires_host_checked_human_authority_without_forcing_ask(self) -> None:
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as missing:
            prerequisites.OperationPrerequisites(
                self._policy(authority_mode="human_owned"), checks={},
            )
        self.assertEqual("human_authority_check_required", missing.exception.code)

        policy = self._policy(
            "task.disposition",
            (prerequisites.CheckSpec("human_authority", "host", "human_authority"),),
            authority_mode="human_owned",
        )
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"human_authority": lambda context: self._result(
                context, observed={"validated_by_host": True},
            )},
        )
        diagnosis = engine.diagnose(self._request("task.disposition"), self._view())
        self.assertEqual("PREREQUISITES_SATISFIED", diagnosis["result"])
        self.assertTrue(diagnosis["human_authority_required"])
        self.assertFalse(diagnosis["human_decision_required"])

    def test_human_owned_denial_and_missing_authority_do_not_trust_approve_parameter(self) -> None:
        # These operation labels are policy fixtures only; no operation runs.
        for operation in ("task.integration", "task.cleanup", "task.disposition"):
            with self.subTest(operation=operation):
                policy = self._policy(
                    operation,
                    (prerequisites.CheckSpec("human_authority", "host", "human_authority"),),
                    authority_mode="human_owned",
                )
                request = self._request(operation, {"approve": True})
                missing = prerequisites.OperationPrerequisites(policy, checks={}).diagnose(request, self._view())
                self.assertEqual("UNAVAILABLE_DEPENDENCY", missing["result"])
                self.assertTrue(missing["human_authority_required"])
                self.assertFalse(missing["human_decision_required"])

                def host_denial(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
                    request_value = json.loads(context.request_bytes)
                    self.assertIs(request_value["parameters"]["approve"], True)
                    return self._result(
                        context,
                        "MISSING_PREREQUISITE",
                        reason="human_authority_absent",
                        observed={"human_authority_validated": False},
                        expected={"human_authority_validated": True},
                    )

                denied = prerequisites.OperationPrerequisites(
                    policy, checks={"human_authority": host_denial},
                ).diagnose(request, self._view())
                self.assertEqual("MISSING_PREREQUISITE", denied["result"])
                self.assertTrue(denied["human_authority_required"])
                self.assertFalse(denied["human_decision_required"])
                self.assertNotIn("approve", repr(denied))

    def test_authority_check_cannot_claim_mechanical_convergence(self) -> None:
        policy = self._policy(checks=(
            prerequisites.CheckSpec("human_authority", "host", "human_authority"),
        ))
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"human_authority": lambda context: self._result(
                context, convergence="mechanical",
            )},
        )
        diagnosis = engine.diagnose(self._request(), self._view())
        self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
        self.assertEqual("invalid_check_result", diagnosis["checks"][0]["reason_code"])

    def test_semantic_and_mechanical_classes_must_match_their_outcomes(self) -> None:
        invalid = (
            ("SEMANTIC_DECISION_REQUIRED", "none"),
            ("MISSING_PREREQUISITE", "semantic"),
            ("IDENTITY_CONFLICT", "mechanical"),
        )
        policy = self._policy(checks=(prerequisites.CheckSpec("convergence_guard", "fixture", "convergence"),))
        for outcome, convergence in invalid:
            with self.subTest(outcome=outcome, convergence=convergence):
                diagnosis = prerequisites.OperationPrerequisites(
                    policy,
                    checks={"convergence_guard": lambda context, o=outcome, c=convergence: self._result(
                        context, o, convergence=c,
                    )},
                ).diagnose(self._request(), self._view())
                self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
                self.assertEqual({"result_valid": False}, diagnosis["checks"][0]["observed"])

    def test_identity_subject_and_policy_mismatches_do_not_invoke_callbacks(self) -> None:
        calls: list[str] = []
        policy = self._policy("inspect")
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"local_facts": lambda context: calls.append(context.binding.check_id)},
        )
        view = self._view()
        repo_conflict = engine.diagnose(
            prerequisites.OperationRequest("other/widgets", _TASK, _BRANCH, _SUBJECT, "inspect", {}),
            view,
        )
        self.assertEqual("IDENTITY_CONFLICT", repo_conflict["result"])
        self.assertEqual({"repository": "other/widgets", "task": _TASK, "branch_ref": _BRANCH}, repo_conflict["precheck"]["expected"])
        self.assertEqual({"repository": "acme/widgets", "task": _TASK, "branch_ref": _BRANCH}, repo_conflict["precheck"]["observed"])
        stale = engine.diagnose(
            prerequisites.OperationRequest(_REPOSITORY, _TASK, _BRANCH, "e" * 40, "inspect", {}),
            view,
        )
        self.assertEqual("STALE_SUBJECT", stale["result"])
        self.assertEqual({"subject": _SUBJECT}, stale["precheck"]["observed"])
        self.assertEqual({"subject": "e" * 40}, stale["precheck"]["expected"])
        operation_mismatch = engine.diagnose(self._request("git.push"), view)
        self.assertEqual("AUTHORITY_CONFLICT", operation_mismatch["result"])
        self.assertEqual({"operation": "inspect"}, operation_mismatch["precheck"]["observed"])
        self.assertEqual({"operation": "git.push"}, operation_mismatch["precheck"]["expected"])
        self.assertEqual([], calls)
        for diagnosis in (repo_conflict, stale, operation_mismatch):
            self.assertEqual([], diagnosis["checks"])
            prerequisites.encode_diagnosis(diagnosis)
        without_precheck = copy.deepcopy(repo_conflict)
        without_precheck["precheck"] = None
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(without_precheck)
        false_precheck = copy.deepcopy(repo_conflict)
        false_precheck["precheck"]["observed"] = copy.deepcopy(false_precheck["precheck"]["expected"])
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(false_precheck)

    def test_request_parameters_are_canonical_digest_bound_and_not_returned(self) -> None:
        first = self._request(parameters={"target": {"branch": "refs/heads/x", "count": 2}})
        reordered = self._request(parameters={"target": {"count": 2, "branch": "refs/heads/x"}})
        changed = self._request(parameters={"target": {"branch": "refs/heads/y", "count": 2}})
        policy = self._policy()
        callback = lambda context: self._result(context)
        engine = prerequisites.OperationPrerequisites(policy, checks={"local_facts": callback})
        first_result = engine.diagnose(first, self._view())
        reordered_result = engine.diagnose(reordered, self._view())
        changed_result = engine.diagnose(changed, self._view())
        self.assertEqual(first_result["request_id"], reordered_result["request_id"])
        self.assertNotEqual(first_result["request_id"], changed_result["request_id"])
        self.assertNotIn("parameters", first_result)
        self.assertEqual(first_result, reordered_result)

    def test_inputs_are_detached_before_callbacks_and_output_is_deterministic(self) -> None:
        params = {"target": {"ref": "refs/heads/topic"}}
        request = self._request(parameters=params)
        view = self._view()
        original_view = copy.deepcopy(view)
        captured: list[tuple[bytes, bytes, bytes]] = []

        def mutating_callback(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            captured.append((context.request_bytes, context.policy_bytes, context.view_bytes))
            params["target"]["ref"] = "mutated"
            view["git"]["status"]["conflicts"] = 8
            return self._result(context)

        policy = self._policy(checks=(prerequisites.CheckSpec("local_facts", "fixture"),))
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"local_facts": mutating_callback, "ignored_extra": object()},
        )
        first = engine.diagnose(request, view)
        second = engine.diagnose(self._request(parameters={"target": {"ref": "refs/heads/topic"}}), original_view)
        self.assertEqual(first, second)
        self.assertEqual(2, len(captured))
        self.assertEqual(captured[0], captured[1])
        self.assertIn(b"refs/heads/topic", captured[0][0])
        self.assertIn(b'"conflicts":0', captured[0][2])

    def test_context_and_policy_snapshot_cannot_be_mutated_through_results(self) -> None:
        contexts: list[prerequisites.CheckContext] = []
        policy = self._policy(checks=(prerequisites.CheckSpec("local_facts", "fixture-owner"),))
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"local_facts": lambda context: (contexts.append(context), self._result(context))[1]},
        )
        first = engine.diagnose(self._request(), self._view())
        with self.assertRaises(FrozenInstanceError):
            contexts[0].binding = contexts[0].binding  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            contexts[0].binding.check_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            contexts[0].view_bytes[0] = 0  # type: ignore[index]

        first["required_checks"][0]["owner"] = "mutated-owner"
        second = engine.diagnose(self._request(), self._view())
        self.assertEqual(first["policy_id"], second["policy_id"])
        self.assertEqual("fixture-owner", second["required_checks"][0]["owner"])

    def test_replayed_results_conflict_on_request_policy_check_and_view_bindings(self) -> None:
        saved: list[prerequisites.CheckResult] = []
        original_policy = self._policy(checks=(prerequisites.CheckSpec("alpha", "owner_a"),))

        def capture(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            result = self._result(context)
            saved.append(result)
            return result

        first = prerequisites.OperationPrerequisites(original_policy, checks={"alpha": capture})
        original_view = self._view()
        original_request = self._request(parameters={"target": "first"})
        original_diagnosis = first.diagnose(original_request, original_view)
        self.assertEqual("PREREQUISITES_SATISFIED", original_diagnosis["result"])

        parameter_replay = prerequisites.OperationPrerequisites(
            original_policy, checks={"alpha": lambda _context: saved[0]},
        ).diagnose(self._request(parameters={"target": "second"}), original_view)
        self.assertNotEqual(original_diagnosis["request_id"], parameter_replay["request_id"])
        self.assertEqual("IDENTITY_CONFLICT", parameter_replay["result"])

        different_policy = prerequisites.OperationPolicy(
            "inspect", "other_policy_owner", "execution",
            (prerequisites.CheckSpec("alpha", "owner_a"),),
        )
        policy_replay = prerequisites.OperationPrerequisites(
            different_policy, checks={"alpha": lambda _context: saved[0]},
        ).diagnose(original_request, original_view)
        self.assertNotEqual(original_diagnosis["policy_id"], policy_replay["policy_id"])
        self.assertEqual("IDENTITY_CONFLICT", policy_replay["result"])

        changed_view = self._view()
        changed_view["git"]["status"]["untracked"] = 1
        view_replay = prerequisites.OperationPrerequisites(
            original_policy, checks={"alpha": lambda _context: saved[0]},
        ).diagnose(original_request, changed_view)
        self.assertNotEqual(original_diagnosis["view_id"], view_replay["view_id"])
        self.assertEqual("IDENTITY_CONFLICT", view_replay["result"])

        binding_changes = {
            "check_id": "attacker_check",
            "check_owner": "attacker_owner",
            "policy_id": "f" * 64,
            "view_id": "f" * 64,
            "request_id": "f" * 64,
            "subject": "e" * 40,
            "operation": "git.push",
            "repository": "other/widgets",
            "task": "193",
            "branch_ref": "refs/heads/other",
        }
        for field, changed in binding_changes.items():
            with self.subTest(binding_field=field):
                def replay_with_wrong_binding(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
                    bad_binding = replace(context.binding, **{field: changed})
                    return self._result(context, binding=bad_binding)

                diagnosis = prerequisites.OperationPrerequisites(
                    original_policy, checks={"alpha": replay_with_wrong_binding},
                ).diagnose(original_request, original_view)
                self.assertEqual("IDENTITY_CONFLICT", diagnosis["result"])
                self.assertEqual({"binding_matches": False}, diagnosis["checks"][0]["observed"])
                self.assertEqual("alpha", diagnosis["checks"][0]["check_id"])
                self.assertNotIn("attacker", repr(diagnosis))

    def test_registry_and_input_mapping_order_do_not_change_diagnosis(self) -> None:
        specs = (
            prerequisites.CheckSpec("alpha", "owner_a"),
            prerequisites.CheckSpec("beta", "owner_b"),
        )
        policy = self._policy(checks=specs)
        callback = lambda context: self._result(context)
        one = prerequisites.OperationPrerequisites(policy, checks={"alpha": callback, "beta": callback})
        two = prerequisites.OperationPrerequisites(policy, checks={"beta": callback, "alpha": callback})
        request_one = self._request(parameters={"b": 2, "a": 1})
        request_two = self._request(parameters={"a": 1, "b": 2})
        self.assertEqual(one.diagnose(request_one, self._view()), two.diagnose(request_two, self._view()))

    def test_policy_check_order_is_normalized_and_duplicates_or_untrusted_policies_fail_closed(self) -> None:
        reverse = self._policy(checks=(
            prerequisites.CheckSpec("zeta", "owner"),
            prerequisites.CheckSpec("alpha", "owner"),
        ))
        canonical = self._policy(checks=(
            prerequisites.CheckSpec("alpha", "owner"),
            prerequisites.CheckSpec("zeta", "owner"),
        ))
        callback = lambda context: self._result(context)
        first = prerequisites.OperationPrerequisites(reverse, checks={"zeta": callback, "alpha": callback})
        second = prerequisites.OperationPrerequisites(canonical, checks={"alpha": callback, "zeta": callback})
        first_diagnosis = first.diagnose(self._request(), self._view())
        second_diagnosis = second.diagnose(self._request(), self._view())
        self.assertEqual(first_diagnosis["policy_id"], second_diagnosis["policy_id"])
        self.assertEqual(first_diagnosis["checks"], second_diagnosis["checks"])
        self.assertEqual(prerequisites.encode_diagnosis(first_diagnosis), prerequisites.encode_diagnosis(second_diagnosis))
        duplicate = self._policy(checks=(
            prerequisites.CheckSpec("alpha", "owner"),
            prerequisites.CheckSpec("alpha", "owner"),
        ))
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.OperationPrerequisites(duplicate, checks={})
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.OperationPrerequisites(object(), checks={})  # type: ignore[arg-type]

    def test_missing_exception_and_malformed_callbacks_never_infer_success(self) -> None:
        policy = self._policy(checks=(
            prerequisites.CheckSpec("alpha", "owner_a"),
            prerequisites.CheckSpec("beta", "owner_b"),
            prerequisites.CheckSpec("delta", "owner_d"),
            prerequisites.CheckSpec("gamma", "owner_c"),
        ))

        def raises(_context: prerequisites.CheckContext) -> object:
            raise RuntimeError("SECRET-DETAIL-MUST-NOT-LEAK")

        engine = prerequisites.OperationPrerequisites(policy, checks={
            "alpha": raises,
            "beta": lambda _context: {"outcome": "SATISFIED"},
            "gamma": lambda context: self._result(context, outcome="NOT_A_REAL_OUTCOME"),
            # delta is deliberately absent
        })
        diagnosis = engine.diagnose(self._request(), self._view())
        self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
        self.assertEqual(
            ["UNAVAILABLE_DEPENDENCY", "AUTHORITY_CONFLICT", "UNAVAILABLE_DEPENDENCY", "AUTHORITY_CONFLICT"],
            [report["outcome"] for report in diagnosis["checks"]],
        )
        self.assertNotIn("SECRET-DETAIL-MUST-NOT-LEAK", repr(diagnosis))
        self.assertEqual("check_callback_failed", diagnosis["checks"][0]["reason_code"])
        self.assertEqual({"check_completed": False}, diagnosis["checks"][0]["observed"])
        self.assertEqual({"check_completed": True}, diagnosis["checks"][0]["expected"])
        self.assertEqual("check_unavailable", diagnosis["checks"][2]["reason_code"])
        self.assertEqual({"check_available": False}, diagnosis["checks"][2]["observed"])
        self.assertEqual({"check_available": True}, diagnosis["checks"][2]["expected"])

    def test_wrong_context_binding_is_identity_conflict_and_preserves_other_reports(self) -> None:
        policy = self._policy(checks=(
            prerequisites.CheckSpec("alpha", "owner_a"),
            prerequisites.CheckSpec("beta", "owner_b"),
        ))

        def wrong(context: prerequisites.CheckContext) -> prerequisites.CheckResult:
            bad = prerequisites.CheckBinding(
                **{**context.binding.__dict__, "subject": "e" * 40},
            )
            return self._result(context, binding=bad)

        engine = prerequisites.OperationPrerequisites(policy, checks={
            "alpha": wrong,
            "beta": lambda context: self._result(context, "MISSING_PREREQUISITE", reason="other_missing"),
        })
        diagnosis = engine.diagnose(self._request(), self._view())
        self.assertEqual("IDENTITY_CONFLICT", diagnosis["result"])
        self.assertEqual({"binding_matches": False}, diagnosis["checks"][0]["observed"])
        self.assertEqual({"binding_matches": True}, diagnosis["checks"][0]["expected"])
        self.assertEqual(["IDENTITY_CONFLICT", "MISSING_PREREQUISITE"], [r["outcome"] for r in diagnosis["checks"]])

    def test_bad_observations_and_wrong_result_types_become_safe_authority_conflicts(self) -> None:
        policy = self._policy(checks=(
            prerequisites.CheckSpec("alpha", "owner_a"),
            prerequisites.CheckSpec("beta", "owner_b"),
        ))
        bad = lambda context: self._result(context, observed={"float": 1.5})
        wrong = lambda _context: object()
        diagnosis = prerequisites.OperationPrerequisites(
            policy, checks={"alpha": bad, "beta": wrong},
        ).diagnose(self._request(), self._view())
        self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
        self.assertEqual(["AUTHORITY_CONFLICT", "AUTHORITY_CONFLICT"], [r["outcome"] for r in diagnosis["checks"]])
        self.assertEqual(
            [{"result_valid": False}, {"result_valid": False}],
            [r["observed"] for r in diagnosis["checks"]],
        )
        self.assertEqual(
            [{"result_valid": True}, {"result_valid": True}],
            [r["expected"] for r in diagnosis["checks"]],
        )

    def test_fixed_failure_precedence_and_all_failures_remain_in_reports(self) -> None:
        specs = tuple(prerequisites.CheckSpec(name, "fixture") for name in ("alpha", "beta", "gamma"))
        outcomes = {
            "alpha": "MISSING_PREREQUISITE",
            "beta": "AUTHORITY_CONFLICT",
            "gamma": "SEMANTIC_DECISION_REQUIRED",
        }
        engine = prerequisites.OperationPrerequisites(
            self._policy(checks=specs),
            checks={name: lambda context, name=name: self._result(
                context, outcomes[name], reason=f"{name}_reason",
                convergence="semantic" if outcomes[name] == "SEMANTIC_DECISION_REQUIRED" else "none",
            ) for name in outcomes},
        )
        diagnosis = engine.diagnose(self._request(), self._view())
        self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
        self.assertEqual("beta_reason", diagnosis["reason_code"])
        self.assertTrue(diagnosis["human_decision_required"])
        self.assertFalse(diagnosis["mechanical_convergence_candidate"])
        self.assertEqual(3, len(diagnosis["checks"]))

    def test_diagnosis_encoder_rejects_tampered_aggregation_and_open_schema(self) -> None:
        policy = self._policy()
        engine = prerequisites.OperationPrerequisites(
            policy, checks={"local_facts": lambda context: self._result(context)},
        )
        diagnosis = engine.diagnose(self._request(), self._view())
        wire = prerequisites.encode_diagnosis(diagnosis)
        self.assertEqual(json.dumps(diagnosis, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(), wire)

        bad_aggregate = copy.deepcopy(diagnosis)
        bad_aggregate["result"] = "MISSING_PREREQUISITE"
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as error:
            prerequisites.encode_diagnosis(bad_aggregate)
        self.assertEqual("diagnosis_aggregation_mismatch", error.exception.code)
        extra_action = copy.deepcopy(diagnosis)
        extra_action["next_action"] = "retry"
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(extra_action)
        old_schema = copy.deepcopy(diagnosis)
        old_schema["schema_version"] = 0
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(old_schema)
        boolean_schema = copy.deepcopy(diagnosis)
        boolean_schema["schema_version"] = True
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(boolean_schema)

    def test_json_string_identity_observation_and_view_schema_bounds(self) -> None:
        engine = prerequisites.OperationPrerequisites(
            self._policy(), checks={"local_facts": lambda context: self._result(context)},
        )
        invalid_requests = (
            (self._request(parameters={"float": 1.25}), "unsupported_json_value"),
            (self._request(parameters={"text": "x" * (prerequisites.MAX_JSON_BYTES + 1)}), "json_string_too_long"),
            (self._request(parameters={"\ud800": "value"}), "invalid_json_string"),
        )
        for request, code in invalid_requests:
            with self.subTest(code=code):
                with self.assertRaises(prerequisites.OperationPrerequisiteError) as error:
                    engine.diagnose(request, self._view())
                self.assertEqual(code, error.exception.code)

        deep: dict[str, Any] = {}
        cursor = deep
        for _ in range(prerequisites.MAX_JSON_DEPTH + 2):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as too_deep:
            engine.diagnose(self._request(parameters=deep), self._view())
        self.assertEqual("json_depth_exceeded", too_deep.exception.code)

        overlong_repository = "a" * (codec.MAX_REPOSITORY_LENGTH + 1)
        malformed_identity = prerequisites.OperationRequest(
            overlong_repository, _TASK, _BRANCH, _SUBJECT, "inspect", {},
        )
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as identity_error:
            engine.diagnose(malformed_identity, self._view())
        self.assertEqual("invalid_task_identity", identity_error.exception.code)
        overlong_branch = prerequisites.OperationRequest(
            _REPOSITORY, _TASK, "refs/heads/" + "x" * task_view.MAX_BRANCH_REF_LENGTH,
            _SUBJECT, "inspect", {},
        )
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as branch_error:
            engine.diagnose(overlong_branch, self._view())
        self.assertEqual("invalid_branch_ref", branch_error.exception.code)

        unicode_request = self._request(parameters={"control": "nul\u0000 café"})
        self.assertEqual("PREREQUISITES_SATISFIED", engine.diagnose(unicode_request, self._view())["result"])

        for extra in ("next_action", "status"):
            with self.subTest(extra_view_field=extra):
                invalid_view = self._view()
                invalid_view[extra] = "legacy-shape"
                with self.assertRaises(prerequisites.OperationPrerequisiteError) as view_error:
                    engine.diagnose(self._request(), invalid_view)
                self.assertEqual("invalid_live_task_view", view_error.exception.code)

    def test_oversized_callback_observation_is_a_safe_authority_conflict(self) -> None:
        engine = prerequisites.OperationPrerequisites(
            self._policy(),
            checks={"local_facts": lambda context: self._result(
                context, observed={"text": "x" * (prerequisites.MAX_CHECK_DATA_BYTES + 1)},
            )},
        )
        diagnosis = engine.diagnose(self._request(), self._view())
        self.assertEqual("AUTHORITY_CONFLICT", diagnosis["result"])
        self.assertEqual("invalid_check_result", diagnosis["checks"][0]["reason_code"])
        self.assertEqual({"result_valid": False}, diagnosis["checks"][0]["observed"])

    def test_encoder_rejects_operation_policy_contradiction(self) -> None:
        engine = prerequisites.OperationPrerequisites(
            self._policy(), checks={"local_facts": lambda context: self._result(context)},
        )
        diagnosis = engine.diagnose(self._request(), self._view())
        diagnosis["operation"] = "git.push"
        with self.assertRaises(prerequisites.OperationPrerequisiteError) as error:
            prerequisites.encode_diagnosis(diagnosis)
        self.assertEqual("diagnosis_operation_binding_mismatch", error.exception.code)

    def test_synthetic_condition_flags_are_booleans_not_integer_aliases(self) -> None:
        engine = prerequisites.OperationPrerequisites(self._policy(), checks={})
        diagnosis = engine.diagnose(self._request(), self._view())
        diagnosis["checks"][0]["observed"]["check_available"] = 0
        with self.assertRaises(prerequisites.OperationPrerequisiteError):
            prerequisites.encode_diagnosis(diagnosis)

    def test_json_aggregate_size_is_bounded_before_document_serialization(self) -> None:
        engine = prerequisites.OperationPrerequisites(
            self._policy(), checks={"local_facts": lambda _context: self.fail("oversized request reached check")},
        )
        small_part = "x" * (prerequisites.MAX_JSON_BYTES // 4)
        escaped_part = "\x00é" * (prerequisites.MAX_JSON_BYTES // 32)
        original_dumps = json.dumps

        def refuse_oversized_document(value, *args, **kwargs):
            if type(value) is dict and "parameters" in value:
                self.fail("aggregate overflow reached whole-request serialization")
            return original_dumps(value, *args, **kwargs)

        for part in (small_part, escaped_part):
            with self.subTest(escaped=part is escaped_part), mock.patch.object(
                prerequisites.json, "dumps", side_effect=refuse_oversized_document,
            ):
                with self.assertRaises(prerequisites.OperationPrerequisiteError) as error:
                    engine.diagnose(self._request(parameters={"parts": [part] * 5}), self._view())
                self.assertEqual("json_size_exceeded", error.exception.code)

    def test_policy_digest_binds_check_ownership_and_full_selected_set(self) -> None:
        left = prerequisites.OperationPrerequisites(
            self._policy(checks=(prerequisites.CheckSpec("local_facts", "owner_a"),)),
            checks={"local_facts": lambda context: self._result(context)},
        )
        right = prerequisites.OperationPrerequisites(
            self._policy(checks=(prerequisites.CheckSpec("local_facts", "owner_b"),)),
            checks={"local_facts": lambda context: self._result(context)},
        )
        first = left.diagnose(self._request(), self._view())
        second = right.diagnose(self._request(), self._view())
        self.assertNotEqual(first["policy_id"], second["policy_id"])


class OperationPrerequisitesLiveFixtureTest(unittest.TestCase):
    """One real #217/#191/#144 live fixture verifies read-only integration."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip

    def setUp(self) -> None:
        self._fixture_set_up()
        self.subject = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.branch_ref = fixtures.git("symbolic-ref", "--quiet", "HEAD", cwd=self.product).decode("utf-8").strip()
        records = task_record.TaskRecords(self.store, authorize=lambda _request: True)
        records.create(
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.subject,
            title="Fixture Task 192",
            body="Local fixture only.",
        )
        self.evidence_id, evidence_bytes = evidence_module.encode_evidence(
            _REPOSITORY,
            _TASK,
            self.subject,
            {
                "schema_version": 1,
                "kind": "fixture/result-v1",
                "producer": {"name": "operation-prerequisites-test"},
                "created_at": "2025-01-02T03:04:05Z",
                "payload": {"result": "opaque; not interpreted by the evaluator"},
            },
        )
        self.store.publish([evidence_bytes])
        self.view = task_view.observe_live_task(
            self.store,
            task=_TASK,
            branch_ref=self.branch_ref,
            base_revision=self.subject,
            selected_evidence=(task_view.EvidenceRef(self.evidence_id, self.subject),),
        )

    def _product_state(self) -> tuple[bytes, ...]:
        index_path = Path(fixtures.git("rev-parse", "--git-path", "index", cwd=self.product).decode().strip())
        if not index_path.is_absolute():
            index_path = self.product / index_path
        return (
            fixtures.git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.product),
            fixtures.git("ls-files", "--stage", "--debug", cwd=self.product),
            fixtures.git("status", "--porcelain=v1", "--untracked-files=all", cwd=self.product,
                         extra_env={"GIT_OPTIONAL_LOCKS": "0"}),
            fixtures.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.product),
            fixtures.git("config", "--local", "--null", "--list", cwd=self.product),
            index_path.read_bytes(),
        )

    def test_real_live_view_diagnosis_does_not_write_product_or_metadata(self) -> None:
        before_product = self._product_state()
        before_metadata = self._remote_tip()
        request = prerequisites.OperationRequest(
            _REPOSITORY, _TASK, self.branch_ref, self.subject, "inspect", {"target": "local"},
        )
        policy = prerequisites.OperationPolicy(
            "inspect", "local_fixture", "execution",
            (prerequisites.CheckSpec("local_facts", "fixture"),),
        )
        engine = prerequisites.OperationPrerequisites(
            policy,
            checks={"local_facts": lambda context: prerequisites.CheckResult(
                context.binding,
                "SATISFIED",
                "fixture_facts_checked",
                {"local_head": self.subject},
                {"subject": self.subject},
            )},
        )
        diagnosis = engine.diagnose(request, self.view)
        self.assertEqual("PREREQUISITES_SATISFIED", diagnosis["result"])
        self.assertEqual(self.subject, diagnosis["subject"])
        self.assertEqual([{"evidence_id": self.evidence_id, "subject": self.subject}], diagnosis["evidence"])
        self.assertNotIn("opaque; not interpreted", repr(diagnosis))
        self.assertEqual(before_product, self._product_state())
        self.assertEqual(before_metadata, self._remote_tip())
        prerequisites.encode_diagnosis(diagnosis)


if __name__ == "__main__":
    unittest.main()
