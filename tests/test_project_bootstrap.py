from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "project_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("project_bootstrap", SCRIPT)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

TEMPLATES = (
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
    "agent-typescript-node",
)


class ProjectBootstrapTest(unittest.TestCase):
    def test_bootstrap_replaces_placeholder_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "My Project"
            root.mkdir()
            (root / "src").mkdir()
            (root / "pyproject.toml").write_text('name = "@@PROJECT_NAME@@"\n', encoding="utf-8")
            (root / "README.md").write_text("# @@PROJECT_NAME@@\n", encoding="utf-8")
            (root / "src" / "main.py").write_text('print("@@PROJECT_NAME@@")\n', encoding="utf-8")

            first = bootstrap.bootstrap(root, None)
            self.assertEqual(first["projectName"], "my-project")
            self.assertEqual(
                sorted(first["changedPaths"]),
                ["README.md", "pyproject.toml", "src/main.py"],
            )
            for relative in ("README.md", "pyproject.toml", "src/main.py"):
                self.assertNotIn("@@PROJECT_NAME@@", (root / relative).read_text(encoding="utf-8"))

            second = bootstrap.bootstrap(root, None)
            self.assertTrue(second["idempotent"])
            self.assertEqual(second["changedPaths"], [])

    def test_invalid_name_is_rejected(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.normalize_name("---")

    def test_generated_templates_expose_bootstrap_and_match_source_helper(self) -> None:
        helper = SCRIPT.read_bytes()
        for template in TEMPLATES:
            generated = ROOT / "templates" / template
            self.assertEqual(helper, (generated / ".automation" / "bin" / "project_bootstrap.py").read_bytes())
            project_mod = (generated / "just" / "project" / "mod.just").read_text(encoding="utf-8")
            self.assertIn("bootstrap name=''", project_mod)
            self.assertIn("project_bootstrap.py", project_mod)

    def test_direnv_does_not_mutate_project_files(self) -> None:
        for adapter in ("python", "rust"):
            text = (ROOT / "components" / "adapters" / adapter / ".envrc").read_text(encoding="utf-8")
            self.assertNotIn("@@PROJECT_NAME@@", text)
            self.assertNotIn("sed -i", text)
            self.assertNotIn("uv sync", text)

    def test_ci_uses_public_bootstrap_path(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(encoding="utf-8")
        self.assertIn("just project::bootstrap smoke-project", workflow)
        self.assertIn(
            "nix develop --no-write-lock-file --command just project::bootstrap smoke-project",
            workflow,
        )
        self.assertNotIn("prepare_smoke_template.py", workflow)
        self.assertIn("unresolved project-name placeholder remains after bootstrap", workflow)
        self.assertIn("--exclude=project_bootstrap.py", workflow)

    def test_python_documented_bootstrap_prevents_outer_nix_lock_writes(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        adoption = (ROOT / "docs" / "existing-repository-adoption.md").read_text(
            encoding="utf-8"
        )
        python_bootstrap = (
            "nix develop --no-write-lock-file --command just project::bootstrap"
        )
        self.assertGreaterEqual(readme.count(python_bootstrap), 2)
        self.assertIn(python_bootstrap, adoption)
        self.assertIn(
            "nix develop --no-update-lock-file --command just project::check", adoption
        )

    def test_python_bootstrap_resolves_name_before_ensuring_lockfiles(self) -> None:
        project = (
            ROOT / "components" / "adapters" / "python" / "just" / "project" / "mod.just"
        ).read_text(encoding="utf-8")
        bootstrap_recipe = project.split("bootstrap name='':", 1)[1].split("\n\n", 1)[0]
        self.assertLess(bootstrap_recipe.index("bootstrap_script"), bootstrap_recipe.index("lockfiles_script"))


if __name__ == "__main__":
    unittest.main()
