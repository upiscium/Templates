from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "template_distribution.py"
SPEC = importlib.util.spec_from_file_location("template_distribution", SCRIPT)
assert SPEC and SPEC.loader
distribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = distribution
SPEC.loader.exec_module(distribution)


class TemplateDistributionTest(unittest.TestCase):
    def test_manifest_is_complete_and_allowlisted(self) -> None:
        targets, excluded = distribution.load_distribution(ROOT)
        self.assertEqual(
            {target.full_name for target in targets.values()},
            {
                "upiscium/Template-Agent-Cpp-CMake",
                "upiscium/Template-Agent-Nix",
                "upiscium/Template-Agent-Python",
                "upiscium/Template-Agent-Rust",
                "upiscium/Template-Agent-TypeScript-Node",
            },
        )
        self.assertEqual(set(excluded), {"agent-base"})
        self.assertIn("minimum/fallback", excluded["agent-base"])
        for target in targets.values():
            self.assertEqual(target.default_branch, "main")
            self.assertIn("do not edit directly", target.description)

    def test_matrix_is_derived_from_manifest(self) -> None:
        targets, _ = distribution.load_distribution(ROOT)
        expected = {
            "include": [
                {
                    "template": target.template,
                    "repository": target.full_name,
                    "default_branch": target.default_branch,
                }
                for target in sorted(targets.values(), key=lambda item: item.template)
            ]
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "matrix"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), expected)

    def test_materialize_is_exact_and_removes_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "distribution"
            destination.mkdir()
            (destination / ".git").mkdir()
            (destination / "stale.txt").write_text("stale\n", encoding="utf-8")
            (destination / ".stale-dotfile").write_text("stale\n", encoding="utf-8")

            distribution.materialize(ROOT, "agent-python", destination)
            differences = distribution.check(ROOT, "agent-python", destination)
            self.assertEqual(differences, [])
            self.assertFalse((destination / "stale.txt").exists())
            self.assertFalse((destination / ".stale-dotfile").exists())
            self.assertTrue((destination / ".gitignore").is_file())

            source_mode = (ROOT / "templates" / "agent-python" / ".automation" / "bin" / "agent_core.py").stat().st_mode
            target_mode = (destination / ".automation" / "bin" / "agent_core.py").stat().st_mode
            self.assertEqual(bool(source_mode & 0o111), bool(target_mode & 0o111))

    def test_check_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "distribution"
            destination.mkdir()
            (destination / ".git").mkdir()
            distribution.materialize(ROOT, "agent-rust", destination)
            (destination / "unexpected.txt").write_text("drift\n", encoding="utf-8")
            self.assertIn("unexpected: unexpected.txt", distribution.check(ROOT, "agent-rust", destination))

    def test_materialize_rejects_non_repository_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "not-a-repo"
            destination.mkdir()
            with self.assertRaises(distribution.DistributionError):
                distribution.materialize(ROOT, "agent-python", destination)

    def test_unpublished_base_cannot_be_materialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "distribution"
            destination.mkdir()
            (destination / ".git").mkdir()
            with self.assertRaises(distribution.DistributionError):
                distribution.materialize(ROOT, "agent-base", destination)

    def test_workflow_only_publishes_verified_current_main(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publish-template-repositories.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Template CI"]', workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("workflow_run.conclusion == 'success'", workflow)
        self.assertIn("TEMPLATE_PUBLISH_ENABLED", workflow)
        self.assertIn("github.event.workflow_run.head_sha", workflow)
        self.assertIn("git ls-remote", workflow)
        self.assertIn("TEMPLATE_PUBLISH_TOKEN", workflow)
        self.assertIn("template_distribution.py materialize", workflow)
        self.assertIn("template_distribution.py check", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("git push --force", workflow)

    def test_operator_api_exposes_distribution_checks_not_publish(self) -> None:
        justfile = (ROOT / "just" / "template.just").read_text(encoding="utf-8")
        self.assertIn("distribution-verify", justfile)
        self.assertIn("distribution-matrix", justfile)
        self.assertIn("distribution-materialize", justfile)
        self.assertIn("distribution-check", justfile)
        self.assertNotIn("distribution-publish", justfile)

    def test_root_flake_exposes_default_devshell_tooling(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertIn("devShells.default", flake)
        for tool in ["just", "python3", "git", "gh"]:
            self.assertRegex(flake, rf"\b{tool}\b")


if __name__ == "__main__":
    unittest.main()
