from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "init_context.py"
spec = importlib.util.spec_from_file_location("init_context", MODULE_PATH)
assert spec and spec.loader
init = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = init
spec.loader.exec_module(init)


class InitContractTest(unittest.TestCase):
    def run_fixture(
        self,
        root: Path,
        *command: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env=env,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
            )
        return result

    def create_runtime(
        self,
        root: Path,
        *,
        version: str = "3",
        adapter: str = "base",
        omit: str | None = None,
    ) -> None:
        files = {
            "AGENTS.md": "# Agent rules\n",
            ".automation/INIT.md": "# Init\n",
            ".automation/VERSION": version + "\n",
            ".automation/ADAPTER": adapter + "\n",
            ".automation/policy.toml": "version = 1\n",
            "just/project/mod.just": "doctor:\n    @true\n",
            "opencode.json": "{}\n",
        }
        for relative, content in files.items():
            if relative == omit:
                continue
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def context_git(self, root: Path, *args: str, check: bool = True) -> str:
        del root, check
        if args == ("rev-parse", "HEAD"):
            return "1" * 40
        if args == ("status", "--short"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    def task_state_text(self, root: Path) -> str:
        sections = "\n".join(
            f"## {section}\n\n- defined\n" for section in init.REQUIRED_TASK_SECTIONS
        )
        return (
            "# TASK-1\n\n"
            "- Task ID: TASK-1\n"
            "- Branch: task/TASK-1-example\n"
            f"- Worktree: {root}\n\n"
            f"{sections}"
        )

    def test_runtime_version_matches_agent_core_version(self) -> None:
        version = (
            ROOT / "components" / "agent-core" / ".automation" / "VERSION"
        ).read_text(encoding="utf-8").strip()
        self.assertEqual("3", version)
        self.assertEqual("3", init.SUPPORTED_AGENT_CORE_VERSION)
        self.assertEqual(version, init.SUPPORTED_AGENT_CORE_VERSION)

    def test_default_branch_rejects_task_state(self) -> None:
        with self.assertRaisesRegex(init.InitError, "Task State must not exist"):
            init.validate_identity(Path("."), "main", "main", {"taskId": "TASK-1"})

    def test_task_branch_identity_mismatch_is_rejected(self) -> None:
        state = {
            "taskId": "TASK-1",
            "branch": "task/TASK-1-example",
            "worktree": str(Path(".").resolve()),
        }
        with self.assertRaisesRegex(init.InitError, "branch/Task identity mismatch"):
            init.validate_identity(Path("."), "task/TASK-2-example", "main", state)

    def test_task_state_worktree_mismatch_is_rejected(self) -> None:
        state = {
            "taskId": "TASK-1",
            "branch": "task/TASK-1-example",
            "worktree": "/different/worktree",
        }
        with self.assertRaisesRegex(init.InitError, "Task State worktree does not match"):
            init.validate_identity(Path("."), "task/TASK-1-example", "main", state)

    def test_task_state_must_be_ignored(self) -> None:
        root = Path(".")
        state = {
            "taskId": "TASK-1",
            "branch": "task/TASK-1-example",
            "worktree": str(root.resolve()),
        }
        with mock.patch.object(init, "task_state_is_ignored", return_value=False), self.assertRaisesRegex(
            init.InitError, ".task-state is not ignored"
        ):
            init.validate_identity(root, "task/TASK-1-example", "main", state)

    def test_version_mismatch_blocks_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / ".automation"
            automation.mkdir()
            (automation / "VERSION").write_text("999\n", encoding="utf-8")
            (automation / "ADAPTER").write_text("base\n", encoding="utf-8")
            with self.assertRaisesRegex(init.InitError, "unsupported Agent Core version"):
                init.context(root)

    def test_bootstrap_branch_preflight_passes_while_context_and_doctor_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"):
                self.assertEqual("PASS", init.preflight(root)["status"])
                with mock.patch.object(init, "current_branch", return_value="bootstrap"), mock.patch.object(
                    init, "default_branch", return_value="main"
                ):
                    for operation in (init.context, init.doctor):
                        with self.subTest(operation=operation.__name__), self.assertRaisesRegex(
                            init.InitError, "non-default branch requires Task State"
                        ):
                            operation(root)

    def test_preflight_never_calls_identity_or_context_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            forbidden = (
                "default_branch",
                "current_branch",
                "task_state",
                "validate_identity",
                "context",
            )
            patches = [
                mock.patch.object(init, name, side_effect=AssertionError(name))
                for name in forbidden
            ]
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"):
                for patch in patches:
                    patch.start()
                try:
                    self.assertEqual("PASS", init.preflight(root)["status"])
                finally:
                    for patch in reversed(patches):
                        patch.stop()

    def test_unresolved_task_contract_blocks_context_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state" / "task.md"
            state.parent.mkdir()
            state.write_text(self.task_state_text(root).replace("## Purpose\n\n- defined", "## Purpose\n\nTBD"), encoding="utf-8")
            before = state.read_bytes()
            with self.assertRaisesRegex(init.InitError, "unresolved required fields"):
                init.task_state(root)
            self.assertEqual(before, state.read_bytes())

    def test_git_runtime_scrubs_ambient_config_and_disables_fsmonitor(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "/tmp/attacker",
            "GIT_SSH_COMMAND": "/tmp/attacker",
        }
        with mock.patch.dict(os.environ, injected), mock.patch.object(
            init.subprocess, "run", return_value=completed
        ) as invoked:
            init.git(Path("/tmp"), "status", "--short")
        command = invoked.call_args.args[0]
        environment = invoked.call_args.kwargs["env"]
        self.assertIn("core.fsmonitor=false", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertFalse(any(key.startswith("GIT_") and key not in {
            "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"
        } for key in environment))
        self.assertEqual(os.devnull, environment["GIT_CONFIG_GLOBAL"])

    def test_github_default_branch_fallback_is_origin_bound(self) -> None:
        def fake_git(_root: Path, *args: str, check: bool = True) -> str:
            if args[:2] == ("symbolic-ref", "--quiet"):
                return ""
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/acme/widgets.git"
            raise AssertionError(args)

        response = subprocess.CompletedProcess(
            [], 0, '{"default_branch":"main"}', ""
        )
        with mock.patch.object(init, "git", side_effect=fake_git), mock.patch.object(
            init, "run", return_value=response
        ) as invoked:
            self.assertEqual("main", init.default_branch(Path("/tmp")))
        self.assertEqual(
            ["gh", "api", "--hostname", "github.com", "repos/acme/widgets"],
            invoked.call_args.args[0],
        )
        self.assertEqual(
            ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_REPOSITORY"),
            invoked.call_args.kwargs["remove_env"],
        )

    def test_orphaned_canonical_contract_marker_blocks_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            state.write_text(
                self.task_state_text(root) + "\n<!-- canonical-contract sha256=" + "a" * 64 + " issue=19 -->\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(init.InitError, "canonical Task Contract is unresolved"):
                init.task_state(root)

    def test_default_branch_preflight_doctor_and_context_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), mock.patch.object(
                init, "current_branch", return_value="main"
            ), mock.patch.object(init, "default_branch", return_value="main"), mock.patch.object(
                init, "git", side_effect=self.context_git
            ):
                self.assertEqual("PASS", init.preflight(root)["status"])
                self.assertIsNone(init.context(root)["taskId"])
                self.assertEqual("PASS", init.doctor(root)["status"])

    def test_registered_task_preflight_doctor_and_context_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            state = root / ".task-state" / "task.md"
            state.parent.mkdir()
            state.write_text(self.task_state_text(root), encoding="utf-8")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), mock.patch.object(
                init, "current_branch", return_value="task/TASK-1-example"
            ), mock.patch.object(init, "default_branch", return_value="main"), mock.patch.object(
                init, "task_state_is_ignored", return_value=True
            ), mock.patch.object(init, "git", side_effect=self.context_git):
                self.assertEqual("PASS", init.preflight(root)["status"])
                self.assertEqual("TASK-1", init.context(root)["taskId"])
                self.assertEqual("PASS", init.doctor(root)["status"])

    def test_detached_head_remains_strict_for_context_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), mock.patch.object(
                init, "current_branch", side_effect=init.InitError("detached HEAD is not supported")
            ):
                self.assertEqual("PASS", init.preflight(root)["status"])
                for operation in (init.context, init.doctor):
                    with self.subTest(operation=operation.__name__), self.assertRaisesRegex(
                        init.InitError, "detached HEAD is not supported"
                    ):
                        operation(root)

    def test_preflight_rejects_missing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            with mock.patch.object(
                init.shutil,
                "which",
                side_effect=lambda tool: None if tool == "gh" else f"/bin/{tool}",
            ), self.assertRaisesRegex(init.InitError, "missing required tools: gh"):
                init.preflight(root)

    def test_preflight_rejects_missing_required_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root, omit="AGENTS.md")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), self.assertRaisesRegex(
                init.InitError, "missing required repository file: AGENTS.md"
            ):
                init.preflight(root)

    def test_preflight_rejects_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root, version="999")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), self.assertRaisesRegex(
                init.InitError, "unsupported Agent Core version"
            ):
                init.preflight(root)

    def test_preflight_rejects_empty_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root, adapter="")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), mock.patch.object(
                init, "current_branch", return_value="main"
            ), mock.patch.object(init, "default_branch", return_value="main"), mock.patch.object(
                init, "git", side_effect=self.context_git
            ):
                with self.assertRaisesRegex(init.InitError, "empty Project Adapter marker"):
                    init.preflight(root)
                self.assertEqual("", init.context(root)["adapter"])
                self.assertEqual("PASS", init.doctor(root)["status"])

    def test_preflight_rejects_missing_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root, omit=".automation/ADAPTER")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"), self.assertRaisesRegex(
                init.InitError, "missing required repository file: .automation/ADAPTER"
            ):
                init.preflight(root)

    def test_preflight_does_not_impose_adapter_naming_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root, adapter="Legacy_Adapter.v1")
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"):
                self.assertEqual("Legacy_Adapter.v1", init.preflight(root)["adapter"])

    def test_preflight_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with mock.patch.object(init.shutil, "which", return_value="/bin/tool"):
                init.preflight(root)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((root / ".task-state").exists())

    def test_preflight_cli_and_just_api_are_exposed(self) -> None:
        self.assertEqual("preflight", init.parser().parse_args(["preflight"]).command)
        recipes = (
            ROOT / "components" / "agent-core" / ".automation" / "just" / "agent.just"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "preflight:\n    env PYTHONDONTWRITEBYTECODE=1 python3 -B "
            "{{quote(init)}} preflight",
            recipes,
        )

    def test_preflight_cli_does_not_require_git_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_runtime(root)
            tools = root / ".test-tools"
            tools.mkdir()
            just = tools / "just"
            just.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            just.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{tools}:{env['PATH']}"
            result = self.run_fixture(
                root,
                sys.executable,
                str(MODULE_PATH),
                "preflight",
                env=env,
            )
            self.assertIn('"status": "PASS"', result.stdout)
            self.assertFalse((root / ".git").exists())

    @unittest.skipUnless(shutil.which("just"), "just is required for runtime preflight smoke")
    def test_real_repository_identity_modes_preserve_strictness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            remote = Path(directory) / "origin.git"
            self.run_fixture(Path(directory), "git", "init", "--bare", "--initial-branch=main", str(remote))
            shutil.copytree(ROOT / "templates" / "agent-base", root)
            gitignore = root / ".gitignore"
            gitignore.write_text(
                gitignore.read_text(encoding="utf-8")
                .replace("__pycache__/\n", "")
                .replace("*.py[oc]\n", ""),
                encoding="utf-8",
            )
            self.assertNotIn("pycache", gitignore.read_text(encoding="utf-8").lower())
            self.run_fixture(root, "git", "init", "-b", "main")
            self.run_fixture(root, "git", "config", "user.name", "Preflight Test")
            self.run_fixture(
                root, "git", "config", "user.email", "preflight@example.invalid"
            )
            self.run_fixture(root, "git", "add", ".")
            self.run_fixture(root, "git", "commit", "-m", "fixture")
            self.run_fixture(root, "git", "remote", "add", "origin", str(remote))
            self.run_fixture(root, "git", "push", "-u", "origin", "main")
            self.run_fixture(
                root,
                "git",
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            )

            for command in (
                "agent::preflight",
                "agent::doctor",
                "agent::context",
                "automation::version",
            ):
                self.run_fixture(root, "just", command)
            bytecode = [
                path.relative_to(root).as_posix()
                for path in (root / ".automation").rglob("*")
                if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
            ]
            self.assertEqual([], bytecode)
            self.assertEqual("", self.run_fixture(root, "git", "status", "--porcelain").stdout)

            self.run_fixture(root, "git", "switch", "-c", "bootstrap-adoption")
            before = self.run_fixture(root, "git", "status", "--porcelain").stdout
            self.run_fixture(root, "just", "agent::preflight")
            after = self.run_fixture(root, "git", "status", "--porcelain").stdout
            self.assertEqual(before, after)
            for command in ("agent::doctor", "agent::context"):
                result = self.run_fixture(root, "just", command, check=False)
                self.assertEqual(2, result.returncode, command)
                self.assertIn(
                    "non-default branch requires Task State",
                    result.stderr,
                    command,
                )

            self.run_fixture(root, "git", "switch", "main")
            self.run_fixture(
                root,
                "just",
                "agent::task-start",
                "SMOKE-PREFLIGHT",
                "runtime-preflight",
            )
            task = root / ".worktrees" / "SMOKE-PREFLIGHT-runtime-preflight"
            self.run_fixture(task, "just", "agent::preflight")
            for command in ("agent::doctor", "agent::context"):
                blocked = self.run_fixture(task, "just", command, check=False)
                self.assertEqual(2, blocked.returncode)
                self.assertIn("unresolved required fields", blocked.stderr)

            state = task / ".task-state/task.md"
            resolved = state.read_text(encoding="utf-8")
            resolved = resolved.replace("## Purpose\n\nTBD", "## Purpose\n\nOffline lifecycle fixture")
            resolved = resolved.replace("## Scope\n\n- TBD", "## Scope\n\n- Fixture-only lifecycle validation")
            resolved = resolved.replace(
                "- [ ] Define Task-specific acceptance criteria",
                "- [ ] Exercise strict identity modes",
            )
            resolved = resolved.replace("- Unverified: Task contract", "- Unverified: fixture checks")
            state.write_text(resolved, encoding="utf-8")
            for command in ("agent::doctor", "agent::context"):
                self.run_fixture(task, "just", command)

    def test_init_runtime_contains_no_repository_mutation_commands(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "git worktree add",
            "git checkout",
            "git switch",
            "git commit",
            "git push",
            "write_text(",
            "mkdir(",
            "unlink(",
            "rmtree(",
        ):
            self.assertNotIn(forbidden, text)

    def test_init_command_overrides_builtin_as_read_only(self) -> None:
        command = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "commands"
            / "init.md"
        ).read_text(encoding="utf-8")
        self.assertIn("overrides OpenCode's built-in `/init`", command)
        self.assertIn("Do not generate, rewrite, or repair `AGENTS.md`", command)

    def test_task_workflows_reuse_initialize_skill(self) -> None:
        commands = ROOT / "components" / "agent-core" / ".opencode" / "commands"
        for name in ("task-run.md", "task-batch.md"):
            text = (commands / name).read_text(encoding="utf-8")
            self.assertIn("initialize", text, name)
            self.assertIn("Stop if initialization fails", text, name)


    def test_initialize_skill_preserves_full_init_and_defines_plan_handoff(self) -> None:
        skill = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "skills"
            / "initialize"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Full initialization for execution-capable", skill)
        self.assertIn("Run `just agent::doctor`; stop on failure", skill)
        self.assertIn("Run `just agent::context`; retain", skill)
        self.assertIn("Run `just project::doctor`; stop on failure", skill)
        self.assertIn("Planning-only initialization", skill)
        self.assertIn("PLANNING_INITIALIZATION_HANDOFF", skill)
        self.assertIn("execution_prerequisites", skill)
        self.assertIn("UNEXECUTED", skill)

    def test_adapter_fragment_is_part_of_init_contract(self) -> None:
        core = (
            ROOT / "components" / "agent-core" / ".automation" / "INIT.md"
        ).read_text(encoding="utf-8")
        fragment = (
            ROOT
            / "components"
            / "adapters"
            / "base"
            / ".automation"
            / "INIT.fragment.md"
        ).read_text(encoding="utf-8")
        self.assertIn(".automation/INIT.fragment.md", core)
        self.assertIn("just project::doctor", fragment)

    def test_generated_init_files_match_sources(self) -> None:
        pairs = [
            ("components/agent-core/.automation/INIT.md", "templates/agent-base/.automation/INIT.md"),
            ("components/agent-core/.automation/VERSION", "templates/agent-base/.automation/VERSION"),
            ("components/agent-core/.automation/bin/init_context.py", "templates/agent-base/.automation/bin/init_context.py"),
            ("components/agent-core/.automation/just/agent.just", "templates/agent-base/.automation/just/agent.just"),
            ("components/adapters/base/.automation/ADAPTER", "templates/agent-base/.automation/ADAPTER"),
            ("components/adapters/base/.automation/INIT.fragment.md", "templates/agent-base/.automation/INIT.fragment.md"),
            ("components/agent-core/.opencode/skills/initialize/SKILL.md", "templates/agent-base/.opencode/skills/initialize/SKILL.md"),
            ("components/agent-core/.opencode/commands/init.md", "templates/agent-base/.opencode/commands/init.md"),
            ("components/agent-core/.opencode/commands/task-run.md", "templates/agent-base/.opencode/commands/task-run.md"),
            ("components/agent-core/.opencode/commands/task-batch.md", "templates/agent-base/.opencode/commands/task-batch.md"),
            ("components/agent-core/.opencode/agents/build.md", "templates/agent-base/.opencode/agents/build.md"),
            ("components/agent-core/.opencode/agents/plan.md", "templates/agent-base/.opencode/agents/plan.md"),
            ("components/agent-core/.opencode/agents/task-orchestrator.md", "templates/agent-base/.opencode/agents/task-orchestrator.md"),
            ("components/agent-core/AGENTS.md", "templates/agent-base/AGENTS.md"),
        ]
        for source, generated in pairs:
            self.assertEqual(
                (ROOT / source).read_bytes(),
                (ROOT / generated).read_bytes(),
                source,
            )


if __name__ == "__main__":
    unittest.main()
