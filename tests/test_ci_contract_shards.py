from __future__ import annotations

from collections import defaultdict
import io
import unittest

from tools import ci_contract_runner as sharding


class ContractShardProofTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_tests = sharding.discover()
        cls.ids = [test.id() for test in cls.original_tests]

    def test_real_suite_partition_has_complete_unique_test_ids(self) -> None:
        assigned = sharding.assignments(self.ids)
        per_shard = defaultdict(list)
        for test_id in self.ids:
            per_shard[assigned[test_id]].append(test_id)
        self.assertEqual(len(self.ids), len(set(self.ids)))
        self.assertEqual(set(range(sharding.SHARDS)), set(per_shard))
        self.assertEqual(set(self.ids), set().union(*(set(x) for x in per_shard.values())))
        self.assertEqual(len(self.ids), sum(len(x) for x in per_shard.values()))
        # Only the #196 host-checkpoint TestCase may be split across shards.
        by_module = defaultdict(set)
        for identifier, shard in assigned.items():
            by_module[identifier.split(".", 1)[0]].add(shard)
        for module, shards in by_module.items():
            if module != sharding.SPECIAL_MODULE:
                self.assertEqual(1, len(shards), module)

    def test_reordering_suite_does_not_change_file_assignment(self) -> None:
        before = sharding.assignments(self.ids)
        after = sharding.assignments(list(reversed(self.ids)))
        self.assertEqual(before, after)
        for heavy, expected_shard in sharding.PINNED.items():
            matching = [identifier for identifier in self.ids
                        if identifier.startswith(heavy + ".")]
            for identifier in matching:
                self.assertEqual(expected_shard, before[identifier])
        # Main does not yet include #196. A future PR merge must distribute
        # exact tests across the dedicated four high-cost shards.
        heavy_ids = [
            f"test_turn_orchestration_v4.FutureTest.test_synthetic_{i}"
            for i in range(4)
        ]
        future = sharding.assignments([*self.ids, *heavy_ids])
        self.assertEqual(set(sharding.SPECIAL_SHARDS), {future[x] for x in heavy_ids})

    def test_new_contract_module_is_not_silently_excluded(self) -> None:
        extra = "test_future_added.FutureTest.test_added_later"
        assignment = sharding.assignments([*self.ids, extra])
        self.assertIn(extra, assignment)
        self.assertIn(assignment[extra], range(sharding.SHARDS))

    def test_empty_and_duplicate_id_inventory_is_rejected(self) -> None:
        with self.assertRaises(sharding.DiscoveryError):
            sharding.assignments([])
        with self.assertRaises(sharding.DiscoveryError):
            sharding.assignments([*self.ids, self.ids[-1]])

    def test_wrong_number_of_ci_shards_is_rejected(self) -> None:
        with self.assertRaises(sharding.DiscoveryError):
            sharding.assignments(self.ids, sharding.SHARDS - 1)

    def test_synthetic_failure_and_incomplete_run_do_not_return_success(self) -> None:
        class Fails(unittest.TestCase):
            def runTest(self) -> None:
                self.fail("deliberate negative control")

        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0, resultclass=sharding.TimingResult,
        ).run(unittest.TestSuite([Fails()]))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, sharding.exit_code(result, 1))
        self.assertEqual(1, sharding.exit_code(result, 2))
        self.assertIn("runTest", next(iter(result.timings)))

    def _synthetic_reports(self):
        class CaseIdentity:
            def __init__(self, identity):
                self.identity = identity

            def id(self):
                return self.identity

        names = [
            "test_task_collaboration_v4.Dummy.test_a",
            "test_task_cleanup_v4.Dummy.test_b",
            "test_task_integration_v4.Dummy.test_c",
            "test_task_view_v4.Dummy.test_d",
            "test_turn_orchestration_v4.Dummy.test_e",
            "test_turn_orchestration_v4.Dummy.test_f",
            "test_turn_orchestration_v4.Dummy.test_g",
            "test_turn_orchestration_v4.Dummy.test_h",
        ]
        tests = [CaseIdentity(name) for name in names]
        mapping = sharding.assignments(names)
        digest = sharding.inventory_digest(names)
        reports = []
        for shard in range(sharding.SHARDS):
            selected = [name for name in names if mapping[name] == shard]
            reports.append({
                "schema_version": 1,
                "shard": shard,
                "shards": sharding.SHARDS,
                "selected_ids": selected,
                "selected_count": len(selected),
                "tests_run": len(selected),
                "tests_discovered": len(names),
                "inventory_digest": digest,
                "successful": True,
                "failures": 0,
                "errors": 0,
                "unexpected_successes": 0,
                "skipped": 0,
                "skipped_ids": [],
                "unexpected_skip_ids": [],
                "wall_seconds": 1.0,
                "individual_test_seconds": {name: 0.01 for name in selected},
            })
        return tests, reports

    def test_reconciliation_accepts_only_exact_full_suite_receipts(self) -> None:
        tests, reports = self._synthetic_reports()
        result = sharding.verify_reports(tests, reports)
        self.assertEqual("COMPLETE", result["status"])
        self.assertEqual(sharding.SHARDS, result["executed_unique_tests"])

    def test_reconciliation_fails_closed_on_missing_duplicate_failure_or_skip(self) -> None:
        from copy import deepcopy

        tests, reports = self._synthetic_reports()
        variants = []
        variants.append(reports[:-1])
        extra = deepcopy(reports)
        extra[2]["shard"] = 1
        variants.append(extra)
        wrong_digest = deepcopy(reports)
        wrong_digest[1]["inventory_digest"] = "0" * 64
        variants.append(wrong_digest)
        missing_test = deepcopy(reports)
        missing_test[3]["selected_ids"] = []
        variants.append(missing_test)
        failed = deepcopy(reports)
        failed[0]["successful"] = False
        variants.append(failed)
        missing_timing = deepcopy(reports)
        missing_timing[0]["individual_test_seconds"] = {}
        variants.append(missing_timing)
        duplicate_skip = deepcopy(reports)
        duplicate_skip[0]["skipped"] = 2
        duplicate_skip[0]["skipped_ids"] = [duplicate_skip[0]["selected_ids"][0]] * 2
        variants.append(duplicate_skip)
        unreviewed_skip = deepcopy(reports)
        unreviewed_skip[0]["skipped"] = 1
        unreviewed_skip[0]["skipped_ids"] = list(unreviewed_skip[0]["selected_ids"])
        variants.append(unreviewed_skip)
        for field in ("tests_run", "selected_count", "skipped",
                      "failures", "errors", "unexpected_successes"):
            malformed = deepcopy(reports)
            malformed[0][field] = bool(malformed[0][field])
            variants.append(malformed)
        for field, value in (("wall_seconds", float("nan")),
                             ("wall_seconds", float("inf")),
                             ("wall_seconds", -1)):
            malformed = deepcopy(reports)
            malformed[0][field] = value
            variants.append(malformed)
        malformed = deepcopy(reports)
        first_id = malformed[0]["selected_ids"][0]
        malformed[0]["individual_test_seconds"][first_id] = float("nan")
        variants.append(malformed)
        extended = deepcopy(reports)
        extended[0]["unreviewed_new_field"] = True
        variants.append(extended)
        for variant in variants:
            with self.subTest(kind=str(variant)[:80]):
                with self.assertRaises(sharding.DiscoveryError):
                    sharding.verify_reports(tests, variant)

    def test_receipt_json_rejects_nan_duplicate_keys_and_oversize(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="ci-receipt-negative-") as temp:
            receipt = Path(temp) / "contract-shard-0.json"
            for content in (b'{"shard": NaN}', b'{"shard": 0, "shard": 1}',
                            b'{"times": {"test_a": 1, "test_a": 2}}',
                            b"null", b'{"shard": Infinity}'):
                with self.subTest(content=content):
                    receipt.write_bytes(content)
                    with self.assertRaises(sharding.DiscoveryError):
                        sharding.read_report(receipt)
            receipt.write_bytes(b"x" * (sharding.MAX_REPORT_BYTES + 1))
            with self.assertRaises(sharding.DiscoveryError):
                sharding.read_report(receipt)

    def test_receipt_json_canonical_valid_input(self) -> None:
        import tempfile
        import json
        from pathlib import Path
        tests, reports = self._synthetic_reports()
        with tempfile.TemporaryDirectory(prefix="ci-receipt-positive-") as temp:
            receipt = Path(temp) / "contract-shard-0.json"
            receipt.write_text(json.dumps(reports[0], allow_nan=False))
            self.assertEqual(reports[0], sharding.read_report(receipt))

    def test_unreviewed_skip_is_not_an_allowed_ci_skip(self) -> None:
        self.assertEqual(8, len(sharding.KNOWN_CI_SKIP_IDS))
        allowed = next(iter(sharding.KNOWN_CI_SKIP_IDS))
        self.assertEqual([], sharding.unexpected_skip_ids([allowed]))
        self.assertEqual(
            ["test_unknown.UnknownTest.test_unreviewed"],
            sharding.unexpected_skip_ids([allowed, "test_unknown.UnknownTest.test_unreviewed"]),
        )


if __name__ == "__main__":
    unittest.main()
