from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_MODULE = ".automation/just/automation.just"
TEMPLATE_NAMES = ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake")
RECIPES = (
    ("automation::version",),
    ("automation::check-update", "."),
    ("automation::upgrade", ".", "a" * 40),
    ("automation::bootstrap-receipt", ".", "a" * 40),
    ("automation::rebind-maintenance-provenance", ".", "a" * 40),
    ("automation::commit", "TASK-81", "message"),
)
LOADER_VARIABLES = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
    "DYLD_VERSIONED_LIBRARY_PATH",
    "DYLD_VERSIONED_FRAMEWORK_PATH",
    "DYLD_ROOT_PATH",
    "DYLD_IMAGE_SUFFIX",
)


class AutomationJustPathTest(unittest.TestCase):
    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)

    @staticmethod
    def _dry_run(repo: Path, *recipe: str) -> str:
        result = subprocess.run(
            ("just", "--dry-run", "--justfile", str(repo / "Justfile"), *recipe),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    @staticmethod
    def _resolved_scripts(output: str) -> list[Path]:
        """Read script arguments from Just's shell-quoted dry-run commands."""
        scripts: list[Path] = []
        for line in output.splitlines():
            for token in shlex.split(line):
                if token.endswith("automation_upgrade.py"):
                    scripts.append(Path(token).expanduser().resolve())
        return scripts

    def test_source_and_generated_automation_modules_use_their_own_root(self) -> None:
        source = ROOT / "components" / "agent-core" / AUTOMATION_MODULE
        self.assertEqual(source.read_text(encoding="utf-8").splitlines()[0], "root := justfile_directory()")
        self.assertNotIn("../..", source.read_text(encoding="utf-8"))

        for name in TEMPLATE_NAMES:
            module_dir = ROOT / "templates" / name / ".automation" / "just"
            for path in sorted(module_dir.glob("*.just")):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[0], "root := justfile_directory()", path)
                self.assertNotIn("../..", text, path)

    def test_agent_core_just_modules_keep_justfile_directory_root(self) -> None:
        module_dir = ROOT / "components" / "agent-core" / ".automation" / "just"
        for path in sorted(module_dir.glob("*.just")):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.splitlines()[0], "root := justfile_directory()", path)
            self.assertNotIn("../..", text, path)

    def test_source_rebind_recipe_uses_python_isolated_bridge(self) -> None:
        justfile = ROOT / "just" / "agent-core.just"
        text = justfile.read_text(encoding="utf-8")
        self.assertIn("rebind-maintenance-provenance target expected_revision:", text)
        self.assertIn(
            "python3 -I {{quote(tool)}} rebind-maintenance-provenance "
            "{{quote(target)}} {{quote(expected_revision)}}",
            text,
        )

    def test_source_resume_contract_recipe_uses_python_isolated_bridge(self) -> None:
        justfile = ROOT / "just" / "agent-core.just"
        text = justfile.read_text(encoding="utf-8")
        self.assertIn("resume-contract-check target task:", text)
        self.assertIn(
            "python3 -I {{quote(tool)}} resume-contract-check "
            "{{quote(target)}} {{quote(task)}}",
            text,
        )

    def test_source_maintenance_finalize_recipe_uses_expected_revision(self) -> None:
        root_justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
        self.assertIn('set shell := ["/bin/sh", "-cu"]', root_justfile)
        text = (ROOT / "just" / "agent-core.just").read_text(encoding="utf-8")
        self.assertIn("maintenance-finalize target task pr expected_implementation_revision:", text)
        self.assertIn('set shell := ["/bin/sh", "-cu"]', text)
        self.assertIn(
            '"$resolved_python" -I {{quote(tool)}} maintenance-finalize '
            "{{quote(target)}} {{quote(task)}} {{quote(pr)}} "
            "{{quote(expected_implementation_revision)}}",
            text,
        )
        self.assertIn("os.path.realpath(sys.executable)", text)
        self.assertIn("/run/current-system/sw/bin/python3", text)
        self.assertLess(text.index("unset LD_PRELOAD"), text.index("selected_python="))

    def test_source_publication_recover_recipe_uses_expected_revision(self) -> None:
        text = (ROOT / "just" / "agent-core.just").read_text(encoding="utf-8")
        self.assertIn(
            "publication-recover target task expected_implementation_revision:", text
        )
        self.assertIn(
            '"$resolved_python" -I {{quote(tool)}} publication-recover '
            "{{quote(target)}} {{quote(task)}} "
            "{{quote(expected_implementation_revision)}}",
            text,
        )
        recipe = text[text.index("publication-recover target task"):]
        self.assertIn("unset LD_PRELOAD", recipe)
        self.assertIn("os.path.realpath(sys.executable)", recipe)
        self.assertLess(recipe.index("unset LD_PRELOAD"), recipe.index("selected_python="))

    @staticmethod
    def _fake_python(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        loader_format = "|".join("%s" for _ in LOADER_VARIABLES)
        loader_values = " ".join(
            f'"${{{name}-unset}}"' for name in LOADER_VARIABLES
        )
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"${1-}\" = -I ] && [ \"${2-}\" = -c ]; then\n"
            f"  printf '{loader_format}' {loader_values} > \"$FAKE_PYTHON_RESOLUTION_ENV_LOG\"\n"
            "  printf %s \"${FAKE_PYTHON_CANONICAL-}\"\n"
            "  exit \"${FAKE_PYTHON_RESOLUTION_STATUS-0}\"\n"
            "fi\n"
            f"printf '{loader_format}' {loader_values} > \"$FAKE_PYTHON_FINAL_ENV_LOG\"\n"
            "printf '%s\\n' \"$0\" > \"$FAKE_PYTHON_LOG\"\n"
            "for argument in \"$@\"; do printf '%s\\n' \"$argument\" >> \"$FAKE_PYTHON_LOG\"; done\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    @staticmethod
    def _maintenance_recipe_command(root: Path) -> str:
        line = next(
            line.strip()
            for line in (ROOT / "just" / "agent-core.just").read_text(encoding="utf-8").splitlines()
            if "selected_python=" in line
        )
        replacements = {
            "/nix/store": str(root / "nix/store"),
            "/usr/bin/python3": str(root / "usr/bin/python3"),
            "/run/current-system/sw/bin/python3": str(root / "run/current-system/sw/bin/python3"),
            "/nix/var/nix/profiles/default/bin/python3": str(
                root / "nix/var/nix/profiles/default/bin/python3"
            ),
            "{{quote(tool)}}": "bridge.py",
            "{{quote(target)}}": "consumer-main",
            "{{quote(task)}}": "22",
            "{{quote(pr)}}": "23",
            "{{quote(expected_implementation_revision)}}": "a" * 40,
        }
        for old, new in replacements.items():
            line = line.replace(old, shlex.quote(new))
        return line

    def _run_python_resolver(
        self,
        root: Path,
        selected: Path,
        canonical: str,
        loader_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        log = root / "executed.log"
        environment = {
            "PATH": str(selected.parent),
            "FAKE_PYTHON_CANONICAL": canonical,
            "FAKE_PYTHON_LOG": str(log),
            "FAKE_PYTHON_RESOLUTION_ENV_LOG": str(root / "resolution-env.log"),
            "FAKE_PYTHON_FINAL_ENV_LOG": str(root / "final-env.log"),
        }
        environment.update(loader_environment or {})
        return subprocess.run(
            ("/bin/sh", "-cu", self._maintenance_recipe_command(root)),
            text=True,
            capture_output=True,
            env=environment,
        )

    def test_trusted_python_resolver_accepts_direct_store_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "nix/store/python/bin/python3"
            self._fake_python(selected)
            result = self._run_python_resolver(root, selected, str(selected))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "executed.log").is_file())

    def test_trusted_python_resolver_executes_profile_symlink_store_target(self) -> None:
        profiles = (
            "run/current-system/sw/bin/python3",
            "nix/var/nix/profiles/default/bin/python3",
        )
        for relative in profiles:
            with self.subTest(profile=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "nix/store/python/bin/python3.14"
                selected = root / relative
                self._fake_python(target)
                selected.parent.mkdir(parents=True)
                selected.symlink_to(target)
                result = self._run_python_resolver(root, selected, str(target))
                self.assertEqual(result.returncode, 0, result.stderr)
                executed = (root / "executed.log").read_text(encoding="utf-8").splitlines()
                self.assertEqual(executed[0], str(target), executed)

    def test_trusted_python_resolver_rejects_user_controlled_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "user/bin/python3"
            self._fake_python(selected)
            result = self._run_python_resolver(root, selected, str(selected))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "executed.log").exists())

    def test_loader_environment_is_absent_from_interpreter_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "nix/store/python/bin/python3"
            self._fake_python(selected)
            result = self._run_python_resolver(
                root,
                selected,
                str(selected),
                {name: f"/attacker/{name}" for name in LOADER_VARIABLES},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (root / "resolution-env.log").read_text(encoding="utf-8"),
                "|".join("unset" for _ in LOADER_VARIABLES),
            )

    def test_loader_environment_is_absent_from_final_bridge_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "nix/store/python/bin/python3"
            self._fake_python(selected)
            result = self._run_python_resolver(
                root,
                selected,
                str(selected),
                {name: f"/attacker/{name}" for name in LOADER_VARIABLES},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (root / "final-env.log").read_text(encoding="utf-8"),
                "|".join("unset" for _ in LOADER_VARIABLES),
            )

    def test_just_invocation_preserves_exact_sanitized_bridge_argv(self) -> None:
        if shutil.which("just") is None:
            self.skipTest("Just executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_python = root / "nix/store/python/bin/python3"
            self._fake_python(target_python)
            module = (ROOT / "just" / "agent-core.just").read_text(encoding="utf-8")
            for old, new in {
                "/nix/store": str(root / "nix/store"),
                "/usr/bin/python3": str(root / "usr/bin/python3"),
                "/run/current-system/sw/bin/python3": str(
                    root / "run/current-system/sw/bin/python3"
                ),
                "/nix/var/nix/profiles/default/bin/python3": str(
                    root / "nix/var/nix/profiles/default/bin/python3"
                ),
            }.items():
                module = module.replace(old, new)
            (root / "just").mkdir()
            (root / "just" / "agent-core.just").write_text(module, encoding="utf-8")
            (root / "Justfile").write_text(
                'set minimum-version := "1.55.0"\n'
                'set shell := ["/bin/sh", "-cu"]\n'
                "mod agent-core 'just/agent-core.just'\n",
                encoding="utf-8",
            )
            consumer = root / "consumer main"
            revision = "a" * 40
            environment = {
                "PATH": str(target_python.parent),
                "FAKE_PYTHON_CANONICAL": str(target_python),
                "FAKE_PYTHON_LOG": str(root / "executed.log"),
                "FAKE_PYTHON_RESOLUTION_ENV_LOG": str(root / "resolution-env.log"),
                "FAKE_PYTHON_FINAL_ENV_LOG": str(root / "final-env.log"),
                **{name: f"/attacker/{name}" for name in LOADER_VARIABLES},
            }
            result = subprocess.run(
                (
                    shutil.which("just") or "just",
                    "--justfile",
                    str(root / "Justfile"),
                    "agent-core::maintenance-finalize",
                    str(consumer),
                    "22",
                    "23",
                    revision,
                ),
                cwd=root,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (root / "executed.log").read_text(encoding="utf-8").splitlines(),
                [
                    str(target_python),
                    "-I",
                    str(root / "tools/automation_recovery_bridge.py"),
                    "maintenance-finalize",
                    str(consumer),
                    "22",
                    "23",
                    revision,
                ],
            )
            expected_environment = "|".join("unset" for _ in LOADER_VARIABLES)
            self.assertEqual(
                (root / "resolution-env.log").read_text(encoding="utf-8"),
                expected_environment,
            )
            self.assertEqual(
                (root / "final-env.log").read_text(encoding="utf-8"),
                expected_environment,
            )

    def test_trusted_python_resolver_rejects_broken_or_ambiguous_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "run/current-system/sw/bin/python3"
            selected.parent.mkdir(parents=True)
            selected.symlink_to(root / "missing/python3")
            broken = self._run_python_resolver(root, selected, "")
            self.assertNotEqual(broken.returncode, 0)

            self._fake_python(selected.parent / "python3-real")
            selected.unlink()
            selected.symlink_to(selected.parent / "python3-real")
            ambiguous = self._run_python_resolver(
                root,
                selected,
                str(root / "nix/store/python/bin/python3") + "\nextra",
            )
            self.assertNotEqual(ambiguous.returncode, 0)
            self.assertFalse((root / "executed.log").exists())

    def test_dry_runs_select_main_repository_script(self) -> None:
        self._assert_dry_run_selects_local_script(linked=False)

    def test_dry_runs_select_linked_worktree_script(self) -> None:
        self._assert_dry_run_selects_local_script(linked=True)

    def _assert_dry_run_selects_local_script(self, *, linked: bool) -> None:
        if shutil.which("just") is None:
            self.skipTest("Just executable is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory) / "nested" / "main-repository"
            outer.mkdir(parents=True)
            shutil.copytree(ROOT / "templates" / "agent-base", outer, symlinks=True, dirs_exist_ok=True)
            self._git(outer, "init", "-b", "main")
            self._git(outer, "config", "user.name", "Automation Just Test")
            self._git(outer, "config", "user.email", "automation-just@example.invalid")
            self._git(outer, "add", "-A")
            self._git(outer, "commit", "-m", "agent-base fixture")

            decoy_paths = (
                outer.parent / ".automation" / "bin" / "automation_upgrade.py",
                outer.parent.parent / ".automation" / "bin" / "automation_upgrade.py",
            )
            for decoy in decoy_paths:
                decoy.parent.mkdir(parents=True, exist_ok=True)
                decoy.write_text("decoy\n", encoding="utf-8")

            target = outer
            if linked:
                target = outer / ".worktrees" / "linked-task"
                self._git(outer, "worktree", "add", "-b", "task/81-paths", str(target))

            expected = (target / ".automation" / "bin" / "automation_upgrade.py").resolve()
            forbidden = {
                (outer / ".automation" / "bin" / "automation_upgrade.py").resolve(),
                *(path.resolve() for path in decoy_paths),
            }
            for recipe in RECIPES:
                scripts = self._resolved_scripts(self._dry_run(target, *recipe))
                self.assertEqual(scripts, [expected], recipe)
                self.assertTrue(scripts[0].is_relative_to(target.resolve()), recipe)
                disallowed = forbidden if linked else {path.resolve() for path in decoy_paths}
                self.assertNotIn(scripts[0], disallowed, recipe)


if __name__ == "__main__":
    unittest.main()
