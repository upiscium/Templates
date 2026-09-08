from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "check_opencode_contract_lock_update.py"
SPEC = importlib.util.spec_from_file_location("check_opencode_contract_lock_update", MODULE_PATH)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class OpencodeContractLockUpdateTest(unittest.TestCase):
    OLD_REV = "a" * 40
    NEW_REV = "b" * 40

    def lock(self, *, unrelated: bool = False) -> dict:
        root_inputs = {
            "flake-utils": "flake-utils",
            "nixpkgs": "nixpkgs",
            "opencodeContract": "opencodeContract",
        }
        nodes = {
            "root": {"inputs": root_inputs},
            "flake-utils": {
                "inputs": {"systems": "systems"},
                "locked": {"rev": "f" * 40},
            },
            "systems": {"locked": {"rev": "e" * 40}},
            "nixpkgs": {"locked": {"rev": "1" * 40}},
            "opencodeContract": {
                "inputs": {"nixpkgs": ["nixpkgs"]},
                "locked": {
                    "owner": "upiscium",
                    "repo": "OpencodeContract",
                    "type": "github",
                    "rev": self.OLD_REV,
                    "narHash": "old",
                    "lastModified": 1,
                },
            },
        }
        if unrelated:
            root_inputs["other"] = "other"
            nodes["other"] = {"locked": {"rev": "2" * 40}}
        return {"nodes": nodes, "root": "root", "version": 7}

    def update_contract(self, lock: dict) -> dict:
        updated = copy.deepcopy(lock)
        updated["nodes"]["opencodeContract"]["locked"].update(
            rev=self.NEW_REV,
            narHash="new",
            lastModified=2,
        )
        return updated

    def assert_invalid(self, before: dict, after: dict, message: str) -> None:
        with self.assertRaisesRegex(checker.LockUpdateError, message):
            checker.validate_update(before, after)

    def write_lock(self, path: Path, lock: dict) -> None:
        path.write_text(json.dumps(lock, sort_keys=True) + "\n", encoding="utf-8")

    def test_identical_revision_and_identical_lock_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before.lock"
            after = Path(directory) / "after.lock"
            self.write_lock(before, self.lock())
            after.write_bytes(before.read_bytes())
            self.assertEqual(
                (False, self.OLD_REV, self.OLD_REV),
                checker.validate_candidate_files(before, after),
            )

    def test_identical_revision_with_exclusive_dependency_change_is_rejected(self) -> None:
        before = self.lock()
        after = copy.deepcopy(before)
        after["nodes"]["opencodeContract"]["inputs"]["tool"] = "contractTool"
        after["nodes"]["contractTool"] = {"locked": {"rev": "3" * 40}}
        self.assert_invalid(
            before,
            after,
            "flake.lock changed without an OpencodeContract revision change",
        )

    def test_contract_revision_update_is_valid(self) -> None:
        before = self.lock()
        self.assertEqual(
            (self.OLD_REV, self.NEW_REV),
            checker.validate_update(before, self.update_contract(before)),
        )

    def test_contract_exclusive_dependency_addition_is_valid(self) -> None:
        before = self.lock()
        after = self.update_contract(before)
        after["nodes"]["opencodeContract"]["inputs"]["tool"] = "contractTool"
        after["nodes"]["contractTool"] = {
            "inputs": {"leaf": "contractLeaf"},
            "locked": {"rev": "3" * 40},
        }
        after["nodes"]["contractLeaf"] = {"locked": {"rev": "4" * 40}}
        checker.validate_update(before, after)

    def test_contract_exclusive_dependency_removal_is_valid(self) -> None:
        before = self.lock()
        before["nodes"]["opencodeContract"]["inputs"]["tool"] = "contractTool"
        before["nodes"]["contractTool"] = {
            "inputs": {"leaf": "contractLeaf"},
            "locked": {"rev": "3" * 40},
        }
        before["nodes"]["contractLeaf"] = {"locked": {"rev": "4" * 40}}
        after = self.update_contract(before)
        del after["nodes"]["opencodeContract"]["inputs"]["tool"]
        del after["nodes"]["contractTool"]
        del after["nodes"]["contractLeaf"]
        checker.validate_update(before, after)

    def test_existing_branch_matching_candidate_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_path = root / "before.lock"
            candidate_path = root / "candidate.lock"
            branch_path = root / "branch.lock"
            before = self.lock()
            candidate = self.update_contract(before)
            self.write_lock(before_path, before)
            self.write_lock(candidate_path, candidate)
            branch_path.write_bytes(candidate_path.read_bytes())
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            self.assertEqual(
                (self.OLD_REV, self.NEW_REV),
                checker.validate_existing_branch(
                    before_path,
                    candidate_path,
                    branch_path,
                    candidate_sha256=digest,
                    expected_revision=self.NEW_REV,
                    changed_paths=["flake.lock"],
                ),
            )

    def test_existing_branch_different_from_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_path = root / "before.lock"
            candidate_path = root / "candidate.lock"
            branch_path = root / "branch.lock"
            before = self.lock()
            candidate = self.update_contract(before)
            branch = copy.deepcopy(candidate)
            branch["nodes"]["opencodeContract"]["locked"]["narHash"] = "manual"
            self.write_lock(before_path, before)
            self.write_lock(candidate_path, candidate)
            self.write_lock(branch_path, branch)
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(
                checker.LockUpdateError, "does not match validated candidate"
            ):
                checker.validate_existing_branch(
                    before_path,
                    candidate_path,
                    branch_path,
                    candidate_sha256=digest,
                    expected_revision=self.NEW_REV,
                    changed_paths=["flake.lock"],
                )

    def test_existing_branch_with_unrelated_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_path = root / "before.lock"
            candidate_path = root / "candidate.lock"
            branch_path = root / "branch.lock"
            before = self.lock()
            candidate = self.update_contract(before)
            self.write_lock(before_path, before)
            self.write_lock(candidate_path, candidate)
            branch_path.write_bytes(candidate_path.read_bytes())
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(checker.LockUpdateError, "only flake.lock"):
                checker.validate_existing_branch(
                    before_path,
                    candidate_path,
                    branch_path,
                    candidate_sha256=digest,
                    expected_revision=self.NEW_REV,
                    changed_paths=["README.md", "flake.lock"],
                )

    def test_nested_follows_shared_dependency_change_is_rejected(self) -> None:
        before = self.lock(unrelated=True)
        before["nodes"]["other"]["inputs"] = {"tool": "sharedTool"}
        before["nodes"]["opencodeContract"]["inputs"]["tool"] = ["other", "tool"]
        before["nodes"]["sharedTool"] = {"locked": {"rev": "5" * 40}}
        after = self.update_contract(before)
        after["nodes"]["sharedTool"]["locked"]["rev"] = "6" * 40
        self.assert_invalid(before, after, "shared")

    def test_exclusive_dependency_becoming_shared_is_rejected(self) -> None:
        before = self.lock(unrelated=True)
        before["nodes"]["opencodeContract"]["inputs"]["tool"] = "contractTool"
        before["nodes"]["contractTool"] = {"locked": {"rev": "7" * 40}}
        after = self.update_contract(before)
        after["nodes"]["other"]["inputs"] = {"tool": "contractTool"}
        self.assert_invalid(before, after, "consumer-owned")

    def test_root_nixpkgs_change_is_rejected(self) -> None:
        before = self.lock()
        after = self.update_contract(before)
        after["nodes"]["nixpkgs"]["locked"]["rev"] = "8" * 40
        self.assert_invalid(before, after, "nixpkgs|shared")

    def test_flake_utils_change_is_rejected(self) -> None:
        before = self.lock()
        after = self.update_contract(before)
        after["nodes"]["flake-utils"]["locked"]["rev"] = "8" * 40
        self.assert_invalid(before, after, "consumer-owned")

    def test_systems_change_through_flake_utils_is_rejected(self) -> None:
        before = self.lock()
        after = self.update_contract(before)
        after["nodes"]["systems"]["locked"]["rev"] = "9" * 40
        self.assert_invalid(before, after, "consumer-owned")

    def test_root_inputs_change_is_rejected(self) -> None:
        before = self.lock()
        after = self.update_contract(before)
        after["nodes"]["root"]["inputs"]["unexpected"] = "unexpected"
        after["nodes"]["unexpected"] = {"locked": {"rev": "9" * 40}}
        self.assert_invalid(before, after, "root node or root inputs changed")

    def test_unrelated_root_dependency_change_is_rejected(self) -> None:
        before = self.lock(unrelated=True)
        after = self.update_contract(before)
        after["nodes"]["other"]["locked"]["rev"] = "a" * 40
        self.assert_invalid(before, after, "consumer-owned")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flake.lock"
            path.write_text('{"version": 7, "version": 8}', encoding="utf-8")
            with self.assertRaisesRegex(checker.LockUpdateError, "duplicate JSON key"):
                checker.load_lock(path)


if __name__ == "__main__":
    unittest.main()
