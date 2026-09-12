from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tools.render_templates import check_template, load_manifest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "components" / "adapters" / "typescript-node"
HELPER = ADAPTER / "just" / "project" / "node.py"
SPEC = importlib.util.spec_from_file_location("typescript_node_adapter", HELPER)
assert SPEC and SPEC.loader
node_adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = node_adapter
SPEC.loader.exec_module(node_adapter)


class TypeScriptNodeAdapterTest(unittest.TestCase):
    def make_project(self, *, scripts: dict[str, str] | None = None) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        package = {
            "name": "fixture",
            "version": "1.0.0",
            "packageManager": "npm@10",
            "engines": {"node": ">=22.6.0 <23"},
            "scripts": scripts or {},
        }
        lock = {
            "name": "fixture",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {"": {"name": "fixture", "version": "1.0.0", "engines": {"node": ">=22.6.0 <23"}}},
        }
        (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        return temporary, root

    def test_manifest_and_generated_template_parity(self) -> None:
        specs = load_manifest(ROOT)
        self.assertEqual(specs["agent-typescript-node"].adapter, "typescript-node")
        self.assertEqual(check_template(ROOT, specs["agent-typescript-node"]), [])

    def test_public_api_and_fixed_script_mapping(self) -> None:
        project = (ADAPTER / "just" / "project" / "mod.just").read_text(encoding="utf-8")
        for recipe in ("doctor:", "format-check:", "lint:", "typecheck:", "test:", "build:", "check:"):
            self.assertIn(recipe, project)
        self.assertEqual(node_adapter.CAPABILITIES["format-check"], "format:check")
        helper = HELPER.read_text(encoding="utf-8")
        for forbidden in ("git ", "gh ", "shell=True", "npm install", "npm ci"):
            self.assertNotIn(forbidden, helper)

    def test_node_engine_contract(self) -> None:
        self.assertTrue(node_adapter.satisfies_node(">=22 <23", (22, 9, 0)))
        self.assertTrue(node_adapter.satisfies_node("22.x", (22, 1, 0)))
        self.assertTrue(node_adapter.satisfies_node("^22.3.0", (22, 8, 0)))
        self.assertTrue(node_adapter.satisfies_node("<=22", (22, 99, 0)))
        self.assertFalse(node_adapter.satisfies_node(">=22.6.0 <23", (22, 5, 1)))
        self.assertFalse(node_adapter.satisfies_node(">=22 <23", (23, 0, 0)))
        with self.assertRaises(node_adapter.NodeError):
            node_adapter.satisfies_node(">=20 || >=22", (22, 0, 0))

    def test_doctor_is_read_only_and_diagnoses_runtime_mismatch(self) -> None:
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        with patch.object(node_adapter.shutil, "which", return_value="/tool"), patch.object(
            node_adapter.subprocess,
            "check_output",
            side_effect=lambda command, **_: "v22.11.0\n" if command[0] == "node" else "10.9.0\n",
        ):
            self.assertEqual(node_adapter.doctor(root), 0)
        self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
        with patch.object(node_adapter.shutil, "which", return_value="/tool"), patch.object(
            node_adapter.subprocess,
            "check_output",
            side_effect=lambda command, **_: "v20.18.0\n" if command[0] == "node" else "10.9.0\n",
        ), self.assertRaises(node_adapter.NodeError):
            node_adapter.doctor(root)

    def test_runtime_rejects_npm_mismatch_and_node_prerelease(self) -> None:
        package = {"packageManager": "npm@10", "engines": {"node": ">=22.6.0 <23"}}
        with patch.object(
            node_adapter.subprocess,
            "check_output",
            side_effect=lambda command, **_: "v22.11.0\n" if command[0] == "node" else "11.1.0\n",
        ), self.assertRaisesRegex(node_adapter.NodeError, "does not match packageManager"):
            node_adapter.validate_runtime(package)
        with self.assertRaisesRegex(node_adapter.NodeError, "prerelease"):
            node_adapter.version("v22.6.0-rc.1")

    def test_package_manager_prerelease_is_rejected_as_metadata(self) -> None:
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        package["packageManager"] = "npm@10.0.0-beta.1"
        (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        with self.assertRaisesRegex(node_adapter.NodeError, "compatible npm version"):
            node_adapter.metadata(root)

    def test_lockfile_ambiguity_and_missing_runtime_fail_closed(self) -> None:
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        (root / "yarn.lock").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(node_adapter.NodeError, "foreign lockfile"):
            node_adapter.metadata(root)
        (root / "yarn.lock").unlink()
        with patch.object(node_adapter.shutil, "which", return_value=None), self.assertRaisesRegex(
            node_adapter.NodeError, "node and npm"
        ):
            node_adapter.metadata(root)

    def test_missing_capability_is_skipped_and_failure_propagates(self) -> None:
        temporary, root = self.make_project(scripts={"lint": "eslint ."})
        self.addCleanup(temporary.cleanup)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        with patch.object(node_adapter.subprocess, "run") as run:
            self.assertEqual(node_adapter.run(root, package, "build"), 0)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess(["npm"], 7)
            self.assertEqual(node_adapter.run(root, package, "lint"), 7)
            run.assert_called_once_with(["npm", "--ignore-scripts", "run", "lint"], cwd=root)

        errors = StringIO()
        with patch.object(node_adapter, "run", side_effect=[0, 7, 0, 0, 0]), redirect_stderr(errors):
            self.assertEqual(node_adapter.check(root, package), 1)
        self.assertIn("check: FAIL", errors.getvalue())

    def test_stale_lockfile_identity_fails_closed(self) -> None:
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        lock["packages"][""]["name"] = "another-project"
        (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        with patch.object(node_adapter.shutil, "which", return_value="/tool"), self.assertRaisesRegex(
            node_adapter.NodeError, "root name"
        ):
            node_adapter.metadata(root)

    def test_incomplete_root_dependency_lock_fails_closed(self) -> None:
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        package["dependencies"] = {"@scope/library": "1.0.0"}
        lock["packages"][""]["dependencies"] = package["dependencies"]
        (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        with patch.object(node_adapter.shutil, "which", return_value="/tool"), self.assertRaisesRegex(
            node_adapter.NodeError, "does not resolve"
        ):
            node_adapter.metadata(root)

    def test_scaffold_has_lockfile_and_no_install_surface(self) -> None:
        package = json.loads((ADAPTER / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ADAPTER / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["packageManager"], "npm@10")
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertNotIn("dependencies", package)
        self.assertNotIn("devDependencies", package)


if __name__ == "__main__":
    unittest.main()
