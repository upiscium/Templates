#!/usr/bin/env python3
"""Deterministic, exhaustive unittest sharding of the Templates unittest suite.

Prototype for #239. This runner intentionally does not weaken unittest discovery,
skip handling, or failure propagation. The entire suite is discovered on every shard.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"
# python3 tools/ci_contract_runner.py starts with tools/ in sys.path; the
# original python3 -m unittest invocation also exposes repository root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHARDS = 8
SPECIAL_MODULE = "test_turn_orchestration_v4"
SPECIAL_SHARDS = (4, 5, 6, 7)
MAX_REPORT_BYTES = 8 * 1024 * 1024
_REPORT_FIELDS = frozenset({
    "schema_version", "shard", "shards", "inventory_digest",
    "tests_discovered", "selected_count", "selected_ids", "tests_run",
    "skipped", "skipped_ids", "unexpected_skip_ids", "failures",
    "errors", "unexpected_successes", "successful", "wall_seconds",
    "individual_test_seconds",
})
_REPORT_COUNTS = frozenset({
    "shard", "shards", "tests_discovered", "selected_count",
    "tests_run", "skipped", "failures", "errors", "unexpected_successes",
})

# Relative measurements from PR #240's failed 1431-test CI; not portable time guarantees.
# Unknown/new files are always included, using a conservative count-based estimate.
ESTIMATED_SECONDS = {
    "test_task_collaboration_v4": 977,
    "test_task_cleanup_v4": 1509,
    "test_task_integration_v4": 679,
    "test_task_view_v4": 527,
    "test_task_record_v4": 157,
    "test_automation_upgrade": 51,
    "test_metadata_ref": 130,
    "test_task_state_recovery": 62,
    "test_evidence_v4": 87,
    "test_source_bootstrap_upgrade_bridge": 22,
    "test_source_publication_recovery_bridge": 23,
    "test_cleanup_resources_v4": 23,
    "test_integration_git_v4": 51,
    "test_post_merge_finalization": 13,
    "test_collaboration_git_v4": 10,
}
PINNED = {
    "test_task_collaboration_v4": 0,
    "test_task_cleanup_v4": 1,
    "test_task_integration_v4": 2,
    "test_task_view_v4": 3,
}


# Observed per-test intervals from the failing PR #240 1431-test Actions run
# 37900930497. They are *scheduling weights only*, never PASS evidence.
# #196's TestCase creates isolated temporary Git fixtures in setUp per test,
# so splitting this one expensive module by test ID preserves isolation.
# All other modules stay at module/file boundaries.
SPECIAL_TEST_SECONDS = {
    "test_actual_dirty_task_view_cannot_be_overridden_by_a_fixture_clean_flag": 285,
    "test_approval_and_semantic_decision_are_handoffs_not_automatic_actions": 29,
    "test_authorizer_subject_mutation_is_rechecked_before_resolver_or_dispatch": 11,
    "test_check_policy_rejects_unregistered_repair_commands_and_empty_verification_set": 736,
    "test_checkpoint_confirmation_requires_two_exact_owner_reads": 72,
    "test_checkpoint_conflicting_latest_is_not_adopted_after_write": 77,
    "test_checkpoint_nonpositive_principal_is_not_a_confirmed_handoff": 157,
    "test_checkpoint_principal_drift_is_not_a_confirmed_handoff": 452,
    "test_checkpoint_receipt_mismatch_is_rejected_after_exact_snapshot_readback": 85,
    "test_discard_restarts_from_real_task_record_evidence_and_snapshot_facts": 79,
    "test_evidence_ack_requires_remote_reachability_and_never_repairs_a_moved_ref": 95,
    "test_executed_temporary_readonly_check_is_measured_not_fabricated": 98,
    "test_failed_and_not_run_checks_remain_nonpassing_in_persisted_evidence": 135,
    "test_failed_escalation_and_mechanical_callbacks_recheck_exact_subject": 56,
    "test_full_review_response_must_fit_the_aggregate_unit_output_budget": 124,
    "test_general_edits_need_exact_host_path_custody_and_scope_binding": 60,
    "test_handoff_refuses_a_live_serialized_dispatch": 68,
    "test_head_drift_blocks_old_ticket_and_requires_a_fresh_exact_turn": 164,
    "test_leaf_prose_cannot_be_promoted_and_unavailable_checks_stay_unavailable": 58,
    "test_malformed_terminal_response_is_one_blocked_attempt": 68,
    "test_mechanical_return_malformed_after_subject_change_is_not_consumed": 61,
    "test_provider_exception_is_terminal_without_retry_or_model_substitution": 123,
    "test_readonly_dispatch_allows_append_only_metadata_tip_and_promotes_bound_evidence": 77,
    "test_readonly_role_requires_exact_clean_subject_stability_not_status_counts": 21,
    "test_review_observations_are_role_bound_and_never_a_test_pass": 59,
    "test_role_binding_typed_dispatch_and_opaque_registration_are_exact": 12,
    "test_unknown_missing_and_duplicate_project_check_ids_are_rejected": 304,
    "test_unqualified_adapter_model_echo_swap_and_authorization_denial_fail_closed": 1,
}


# Existing CI skips are environment/tooling-gated, not authorization to skip
# additional contract tests silently. Reviewed changes may update this set.
KNOWN_CI_SKIP_IDS = frozenset({
    "test_automation_just_paths.AutomationJustPathTest.test_dry_runs_select_linked_worktree_script",
    "test_automation_just_paths.AutomationJustPathTest.test_dry_runs_select_main_repository_script",
    "test_automation_just_paths.AutomationJustPathTest.test_just_invocation_preserves_exact_sanitized_bridge_argv",
    "test_automation_upgrade.AutomationUpgradeContractTest.test_issue_85_bridge_contract_and_rejection_matrix",
    "test_init_contract.InitContractTest.test_real_repository_identity_modes_preserve_strictness",
    "test_opencode_contract.OpenCodeContractTest.test_maintenance_effective_policy_when_cli_available",
    "test_opencode_contract.OpenCodeContractTest.test_opencode_debug_agent_plan_effective_policy_when_cli_available",
    "test_source_bootstrap_upgrade_bridge.BootstrapUpgradeBridgeTest.test_installed_maintenance_check_preserves_active_receipt_paths",
})


class DiscoveryError(RuntimeError):
    """The canonical suite could not be enumerated completely and uniquely."""


def flatten(suite: unittest.TestSuite):
    for entry in suite:
        if isinstance(entry, unittest.TestSuite):
            yield from flatten(entry)
        else:
            yield entry


def discover() -> list[unittest.TestCase]:
    loader = unittest.TestLoader()
    suite = loader.discover(str(TEST_DIR), pattern="test*.py", top_level_dir=str(TEST_DIR))
    if loader.errors:
        raise DiscoveryError("suite imports failed:\n" + "\n".join(loader.errors))
    tests = list(flatten(suite))
    identifiers = [test.id() for test in tests]
    if not tests or len(identifiers) != len(set(identifiers)):
        raise DiscoveryError("empty suite or duplicated test IDs")
    for identifier in identifiers:
        module = identifier.split(".", 1)[0]
        if not module.startswith("test_") or not (TEST_DIR / (module + ".py")).is_file():
            raise DiscoveryError("test cannot be bound to one local test module: " + identifier)
    return tests


def inventory_digest(identifiers: list[str]) -> str:
    return hashlib.sha256(("\n".join(sorted(identifiers)) + "\n").encode("utf-8")).hexdigest()


def assignments(identifiers: list[str], shard_count: int = SHARDS) -> dict[str, int]:
    """Assign each exact test ID once; only #196's heavy module splits by ID."""
    if shard_count != SHARDS:
        raise DiscoveryError(f"expected exactly {SHARDS} independent shards")
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise DiscoveryError("empty or duplicated test ID inventory")
    modules = Counter(identifier.split(".", 1)[0] for identifier in identifiers)
    if not modules:
        raise DiscoveryError("no contract test modules")
    has_special = SPECIAL_MODULE in modules
    normal_modules = {module: count for module, count in modules.items()
                      if module != SPECIAL_MODULE}
    if has_special and modules[SPECIAL_MODULE] < len(SPECIAL_SHARDS):
        raise DiscoveryError("insufficient tests for dedicated heavy module split")
    # With #196 present, dedicate the upper 4 runners to its independent
    # per-test fixtures. With #196 absent (current main), use all 8 runners.
    normal_shards = tuple(range(4)) if has_special else tuple(range(SHARDS))
    loads = [0.0] * SHARDS
    file_map: dict[str, int] = {}
    for module, shard in PINNED.items():
        if module in normal_modules:
            file_map[module] = shard
            loads[shard] += ESTIMATED_SECONDS[module]
    remaining = sorted(
        (module for module in normal_modules if module not in file_map),
        key=lambda module: (-ESTIMATED_SECONDS.get(
            module, max(1.0, normal_modules[module] / 10.0)), module),
    )
    for module in remaining:
        weight = ESTIMATED_SECONDS.get(module, max(1.0, normal_modules[module] / 10.0))
        shard = min(normal_shards, key=lambda n: (loads[n], n))
        file_map[module] = shard
        loads[shard] += weight
    by_id: dict[str, int] = {}
    for identifier in identifiers:
        module = identifier.split(".", 1)[0]
        if module != SPECIAL_MODULE:
            by_id[identifier] = file_map[module]
    if has_special:
        heavy_ids = sorted(
            (identifier for identifier in identifiers
             if identifier.split(".", 1)[0] == SPECIAL_MODULE),
            key=lambda identifier: (
                -SPECIAL_TEST_SECONDS.get(identifier.rsplit(".", 1)[-1], 60),
                identifier,
            ),
        )
        for identifier in heavy_ids:
            shard = min(SPECIAL_SHARDS, key=lambda n: (loads[n], n))
            by_id[identifier] = shard
            loads[shard] += SPECIAL_TEST_SECONDS.get(identifier.rsplit(".", 1)[-1], 60)
    if set(by_id) != set(identifiers):
        raise DiscoveryError("test inventory not fully assigned")
    chunks = [[identifier for identifier in identifiers if by_id[identifier] == shard]
              for shard in range(SHARDS)]
    if not all(chunks):
        raise DiscoveryError("an expected CI shard is empty")
    actual = [test_id for chunk in chunks for test_id in chunk]
    if len(actual) != len(identifiers) or set(actual) != set(identifiers):
        raise DiscoveryError("missing or duplicated test assignment")
    # Never split any other test file: module/class fixtures may require order.
    for module in normal_modules:
        if len({by_id[t] for t in identifiers if t.split(".", 1)[0] == module}) != 1:
            raise DiscoveryError("non-special module was split")
    return by_id


def plan(tests: list[unittest.TestCase]) -> dict:
    identifiers = [test.id() for test in tests]
    mapping = assignments(identifiers)
    counts = Counter(mapping[identifier] for identifier in identifiers)
    by_file: dict[int, set[str]] = {}
    for identifier, shard in mapping.items():
        by_file.setdefault(shard, set()).add(identifier.split(".", 1)[0])
    heavy_locations = sorted({mapping[identifier] for identifier in identifiers
                              if identifier.split(".", 1)[0] == SPECIAL_MODULE})
    return {
        "schema_version": 1,
        "inventory_digest": inventory_digest(identifiers),
        "tests_discovered": len(identifiers),
        "modules_discovered": len({x.split(".", 1)[0] for x in identifiers}),
        "shards": SHARDS,
        "selected_counts": {str(shard): counts[shard] for shard in range(SHARDS)},
        "modules": {str(shard): sorted(by_file.get(shard, set())) for shard in range(SHARDS)},
        "split_modules": {SPECIAL_MODULE: heavy_locations} if heavy_locations else {},
    }


def _closed_json_pairs(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate receipt keys at every JSON nesting level."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise DiscoveryError("duplicate receipt JSON key")
        value[key] = item
    return value


def _reject_nonfinite_json(_value: str) -> None:
    raise DiscoveryError("non-finite JSON number in receipt")


def read_report(path: Path) -> dict:
    """Bound and strictly parse one untrusted downloaded CI artifact."""
    if not path.is_file() or path.is_symlink():
        raise DiscoveryError("receipt is not a regular file")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise DiscoveryError("receipt size limit exceeded")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_REPORT_BYTES:
            raise DiscoveryError("receipt size limit exceeded")
        result = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_closed_json_pairs,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise DiscoveryError("invalid receipt JSON") from None
    if type(result) is not dict:
        raise DiscoveryError("receipt must be one closed JSON object")
    return result


def verify_reports(tests: list[unittest.TestCase], reports: list[dict]) -> dict:
    """Fail closed unless exactly the canonical discovered suite ran once."""
    ids = [test.id() for test in tests]
    by_test_id = assignments(ids)
    fingerprint = inventory_digest(ids)
    if len(reports) != SHARDS:
        raise DiscoveryError("missing/extra shard reports")
    visited: set[int] = set()
    union: set[str] = set()
    skipped: list[str] = []
    times: dict[str, float] = {}
    for report in reports:
        if type(report) is not dict or report.keys() != _REPORT_FIELDS:
            raise DiscoveryError("malformed or extended shard report")
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise DiscoveryError("unsupported shard receipt schema")
        if any(type(report.get(key)) is not int or report[key] < 0
               for key in _REPORT_COUNTS):
            raise DiscoveryError("invalid receipt count or shard identity")
        shard = report["shard"]
        if shard not in range(SHARDS) or shard in visited:
            raise DiscoveryError("duplicated/invalid shard identity")
        visited.add(shard)
        expected = [x for x in ids if by_test_id[x] == shard]
        actual = report.get("selected_ids")
        if type(actual) is not list or actual != expected:
            raise DiscoveryError(f"shard {shard} did not execute its exact assigned tests")
        if len(actual) != len(set(actual)) or union.intersection(actual):
            raise DiscoveryError("duplicate test execution across shards")
        union.update(actual)
        if report.get("inventory_digest") != fingerprint or report.get("tests_discovered") != len(ids):
            raise DiscoveryError("test discovery fingerprint differs between workers")
        if report.get("shards") != SHARDS or report.get("selected_count") != len(expected):
            raise DiscoveryError("unexpected shard shape")
        if (
            report.get("tests_run") != len(expected)
            or report.get("successful") is not True
            or report.get("failures") != 0
            or report.get("errors") != 0
            or report.get("unexpected_successes") != 0
        ):
            raise DiscoveryError("a shard did not finish successfully")
        current_skips = report.get("skipped_ids")
        if (
            type(current_skips) is not list
            or len(current_skips) != report.get("skipped")
            or len(current_skips) != len(set(current_skips))
        ):
            raise DiscoveryError("invalid skip evidence")
        per_test_seconds = report.get("individual_test_seconds")
        if type(per_test_seconds) is not dict or set(per_test_seconds) != set(expected):
            raise DiscoveryError("missing or extra per-test timing evidence")
        if any(
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 0 <= duration < 86_400
            for duration in per_test_seconds.values()
        ):
            raise DiscoveryError("invalid per-test timing evidence")
        if any(x not in actual for x in current_skips) or unexpected_skip_ids(current_skips):
            raise DiscoveryError("unreviewed skip in shard evidence")
        if report.get("unexpected_skip_ids") != []:
            raise DiscoveryError("unreviewed skip evidence")
        skipped.extend(current_skips)
        elapsed = report.get("wall_seconds")
        if (
            type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or not 0 <= elapsed < 86_400
        ):
            raise DiscoveryError("invalid wall-clock evidence")
        times[str(shard)] = elapsed
    if union != set(ids) or visited != set(range(SHARDS)):
        raise DiscoveryError("some tests or shards were not executed")
    return {
        "status": "COMPLETE",
        "shards": SHARDS,
        "executed_unique_tests": len(ids),
        "inventory_digest": fingerprint,
        "skipped": len(skipped),
        "skipped_ids": sorted(skipped),
        "elapsed_seconds_per_shard": times,
    }


class TimingResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.timings: dict[str, float] = {}
        self._started: dict[str, float] = {}

    def startTest(self, test):
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        started = self._started.pop(test.id(), None)
        if started is not None:
            elapsed = round(time.monotonic() - started, 6)
            self.timings[test.id()] = elapsed
            # Live Actions text log is a diagnostic stream, not test authority.
            # The final structured evidence remains exhaustive and required.
            self.stream.writeln(f"CI_TEST_SECONDS {test.id()} {elapsed:.3f}")
            self.stream.flush()
        super().stopTest(test)


def unexpected_skip_ids(skipped_ids: list[str]) -> list[str]:
    return sorted(set(skipped_ids) - KNOWN_CI_SKIP_IDS)


def exit_code(result: unittest.TestResult, expected: int) -> int:
    return 0 if result.wasSuccessful() and result.testsRun == expected else 1


def run(tests: list[unittest.TestCase], shard: int, report_path: str | None) -> int:
    if not (0 <= shard < SHARDS):
        raise DiscoveryError("shard index out of range")
    identifiers = [test.id() for test in tests]
    mapping = assignments(identifiers)
    selected = [test for test in tests if mapping[test.id()] == shard]
    if not selected:
        raise DiscoveryError("empty selected shard")
    start = time.monotonic()
    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimingResult)
    result = runner.run(unittest.TestSuite(selected))
    wall_seconds = round(time.monotonic() - start, 6)
    skipped_ids = sorted(test.id() for test, _reason in result.skipped)
    unexpected_skips = unexpected_skip_ids(skipped_ids)
    success = exit_code(result, len(selected)) == 0 and not unexpected_skips
    if unexpected_skips:
        print("UNREVIEWED_SKIPS: " + ", ".join(unexpected_skips), file=sys.stderr)
    report = {
        "schema_version": 1,
        "shard": shard,
        "shards": SHARDS,
        "inventory_digest": inventory_digest(identifiers),
        "tests_discovered": len(identifiers),
        "selected_count": len(selected),
        "selected_ids": [test.id() for test in selected],
        "tests_run": result.testsRun,
        "skipped": len(result.skipped),
        "skipped_ids": skipped_ids,
        "unexpected_skip_ids": unexpected_skips,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": success,
        "wall_seconds": wall_seconds,
        "individual_test_seconds": result.timings,
    }
    if report_path is not None:
        dest = Path(report_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(
        f"CONTRACT_SHARD={shard}/{SHARDS} discovered={len(identifiers)} "
        f"selected={len(selected)} executed={result.testsRun} skipped={len(result.skipped)} "
        f"elapsed={wall_seconds}s success={success} digest={report['inventory_digest']}",
        flush=True,
    )
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="operation", required=True)
    check = modes.add_parser("verify")
    check.add_argument("--expect-count", type=int, default=None)
    action = modes.add_parser("run")
    action.add_argument("--shard", type=int, required=True)
    action.add_argument("--report", default=None)
    reconcile = modes.add_parser("reconcile")
    reconcile.add_argument("--reports", required=True)
    args = parser.parse_args()
    try:
        tests = discover()
        if args.operation == "verify":
            result = plan(tests)
            if args.expect_count is not None and result["tests_discovered"] != args.expect_count:
                raise DiscoveryError("baseline suite count mismatch")
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.operation == "reconcile":
            report_files = sorted(Path(args.reports).glob("**/contract-shard-*.json"))
            if len(report_files) != SHARDS:
                raise DiscoveryError("missing/extra downloaded CI shard reports")
            reports = [read_report(path) for path in report_files]
            print(json.dumps(verify_reports(tests, reports), indent=2, sort_keys=True))
            return 0
        return run(tests, args.shard, args.report)
    except (DiscoveryError, OSError, ValueError) as exc:
        print(f"CONTRACT_SHARD_FAIL_CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
