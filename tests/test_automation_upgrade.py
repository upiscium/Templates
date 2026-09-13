from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sys
import tempfile
import unittest
import os
import shutil
import shlex
import subprocess
import tarfile
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "automation_upgrade.py"
SPEC = importlib.util.spec_from_file_location("automation_upgrade", SCRIPT)
assert SPEC and SPEC.loader
upgrade = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upgrade
SPEC.loader.exec_module(upgrade)

BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "automation_recovery_bridge_for_tests", ROOT / "tools" / "automation_recovery_bridge.py"
)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
bridge_bootstrap = importlib.util.module_from_spec(BRIDGE_SPEC)
BRIDGE_SPEC.loader.exec_module(bridge_bootstrap)

AGENT_SPEC = importlib.util.spec_from_file_location(
    "agent_core_for_upgrade_tests", ROOT / "components" / "agent-core" / ".automation" / "bin" / "agent_core.py"
)
assert AGENT_SPEC and AGENT_SPEC.loader
agent_core = importlib.util.module_from_spec(AGENT_SPEC)
AGENT_SPEC.loader.exec_module(agent_core)


class AutomationUpgradeContractTest(unittest.TestCase):
    TEMPLATE_NAMES = ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake")
    AGENT_KNOWLEDGE_VAULT_19 = {
        "task": "19",
        "branch": "task/19-agent-core-v3-1-1",
        "authoritative_external_baseline": "1e3a795d5e2717f9c670a812777c4a38c9592db0",
        "baseline_pack_sha256": "e4bb9e9240d40543404bdb094446104ec7984f8cb72a6bcce7d042dc3e670bab",
        "templates_source_pack_sha256": "54e7d83478d67499c5afd522a3fc27766048ce669c7169c9e8bc8cb2a04fe5cd",
        "receipt_source_revision": "076653b054f5d8cbce4a28bcb6b381e9f30ee669",
        "expected_source_revision": "835203b6f1ae342d31ed74372728e9862b9b36f0",
        "maintenance_commit": None,
        "push": False,
        "pull_request": False,
    }
    V3_REMOVED_PATHS = (
        ".automation/model-fallback.toml",
        ".automation/bin/model_fallback.py",
        ".opencode/commands/task-recover.md",
        ".opencode/commands/task-recover-clear.md",
        ".opencode/skills/task-recovery/SKILL.md",
        ".opencode/agents/architect-fallback.md",
        ".opencode/agents/build-fallback.md",
        ".opencode/agents/explore-fallback.md",
        ".opencode/agents/general-fallback.md",
        ".opencode/agents/investigator-fallback.md",
        ".opencode/agents/plan-fallback.md",
        ".opencode/agents/reviewer-fallback.md",
        ".opencode/agents/scout-fallback.md",
        ".opencode/agents/security-reviewer-fallback.md",
        ".opencode/agents/task-orchestrator-fallback.md",
        ".opencode/agents/verifier-fallback.md",
    )

    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _field(self, value, name: str):
        if isinstance(value, dict):
            return value[name]
        return getattr(value, name)

    def _require_migration_api(self) -> None:
        for fn in ("load_migrations", "build_plan", "apply"):
            if not hasattr(upgrade, fn):
                self.skipTest(f"{fn} API is not present in this checkout")

    def _core_root_source(self, source_root: Path, *, version: int = 3, migrations: str | None = None) -> Path:
        source = source_root / "components" / "agent-core"
        auto = source / ".automation"
        auto.mkdir(parents=True)

        migration_body = migrations if migrations is not None else "schema_version = 1\n"
        self._write_file(auto / "migrations.toml", migration_body)
        self._write_file(auto / "VERSION", f"{version}\n")

        self._write_file(
            source / "AGENTS.md",
            """<!-- BEGIN AGENT CORE RULES -->
core rules
<!-- END AGENT CORE RULES -->
""",
        )
        self._write_file(
            source / "Justfile",
            """# Agent Core module router
mod agent '.automation/just/agent.just'
mod project 'just/project/mod.just'
""",
        )
        self._write_file(source / "opencode.json", "{}\n")
        self._write_file(auto / "UPSTREAM", 'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n')
        return source

    def _destination_repo(self, repo_root: Path, *, version: int = 2) -> Path:
        self._write_file(repo_root / ".automation" / "VERSION", f"{version}\n")
        self._write_file(
            repo_root / ".automation" / "ADAPTER",
            "base\n",
        )
        self._write_file(
            repo_root / ".automation" / "UPSTREAM",
            'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n',
        )
        self._write_file(
            repo_root / ".automation" / "ownership.toml",
            'version = 1\n\n[paths]\n"AGENTS.md" = "replace"\n"Justfile" = "replace"\n',
        )

        self._write_file(
            repo_root / "AGENTS.md",
            """<!-- BEGIN AGENT CORE RULES -->
existing core rules
<!-- END AGENT CORE RULES -->
""",
        )
        self._write_file(
            repo_root / "Justfile",
            """# Agent Core module router
mod agent '.automation/just/agent.just'
mod project 'just/project/mod.just'
""",
        )
        self._write_file(repo_root / "opencode.json", "{}\n")
        return repo_root

    def _plan_action(self, plan: dict, path: str):
        for item in plan["actions"]:
            if item["path"] == path:
                return item
        return None

    def _manifest(self, transitions: list[str], *, extra_top: str = "") -> str:
        body = ["schema_version = 1", ""]
        if extra_top:
            body.append(extra_top)
            body.append("")
        body.extend(transitions)
        return "\n".join(body)

    def _migration_manifest(
        self,
        from_version: int = 2,
        to_version: int = 3,
        *,
        remove_paths: tuple[str, ...] = (),
        require_absent_paths: tuple[str, ...] = (),
    ) -> str:
        def render(values: tuple[str, ...]) -> str:
            return "[" + ", ".join(json.dumps(value) for value in values) + "]"

        return (
            "schema_version = 1\n\n"
            "[[migrations]]\n"
            f"from_version = {from_version}\n"
            f"to_version = {to_version}\n"
            f"remove_paths = {render(remove_paths)}\n"
            f"require_absent_paths = {render(require_absent_paths)}\n"
        )

    def _git(self, args: list[str], cwd: Path) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def _expected_source_revision(self, source: Path) -> str:
        """Return the full source repository HEAD for the current API contract."""
        candidate = source
        if not (candidate / ".git").exists() and not (candidate / ".git").is_file():
            candidate = source.parent.parent
        return self._git(["rev-parse", "HEAD"], candidate)

    def _init_source_git(self, source_root: Path) -> str:
        self._git(["init", "-b", "main"], source_root)
        self._git(["config", "user.name", "Test User"], source_root)
        self._git(["config", "user.email", "test@example.invalid"], source_root)
        self._git(["add", "components/agent-core"], source_root)
        self._git(["commit", "-m", "source fixture"], source_root)
        return self._git(["rev-parse", "HEAD"], source_root)

    def _ignore_source_path(self, source_root: Path, pattern: str) -> None:
        exclude = source_root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write(pattern + "\n")

    def _current_source_copy(self, source_root: Path) -> Path:
        shutil.copytree(
            ROOT / "components" / "agent-core",
            source_root / "components" / "agent-core",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        self._init_source_git(source_root)
        return source_root

    def _materialize_release_template(self, destination: Path) -> None:
        release = "a42ce1cc30e1a73e33c268a65c8957debc54d4cd"
        self.assertEqual(release, self._git(["rev-parse", "v3.0.0^{commit}"], ROOT))
        self._materialize_template_revision(destination, release)

    def _materialize_template_revision(self, destination: Path, revision: str) -> None:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", revision, "templates/agent-base"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
        prefix = "templates/agent-base/"
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                relative = member.name.removeprefix(prefix)
                if not relative or relative == member.name:
                    continue
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    target.write_bytes(extracted.read())
                    target.chmod(member.mode)

    def _real_cli_fixture(self, root: Path) -> tuple[Path, Path, dict]:
        main = root / "consumer-main"
        main.mkdir(parents=True)
        self._materialize_release_template(main)
        self._write_file(root / "outside-product", "product\n")
        (main / "product-link").symlink_to(root / "outside-product")
        self._git(["init", "-b", "main"], main)
        self._git(["config", "user.name", "Test User"], main)
        self._git(["config", "user.email", "test@example.invalid"], main)
        self._git(["add", "-A"], main)
        self._git(["commit", "-m", "release fixture"], main)
        head = self._git(["rev-parse", "HEAD"], main)
        self._git(["update-ref", "refs/remotes/origin/main", head], main)
        self._git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], main)
        task = root / "consumer-task"
        self._git(["worktree", "add", "-b", "task/TASK-78-maintenance", str(task)], main)
        self._write_file(task / ".task-state/task.md", f"- Task ID: TASK-78\n- Branch: task/TASK-78-maintenance\n- Worktree: {task.resolve()}\n")
        exclude = Path(self._git(["rev-parse", "--git-path", "info/exclude"], task))
        if not exclude.is_absolute():
            exclude = task / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("/.task-state/\n", encoding="utf-8")
        self.assertEqual("origin/main", self._git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], task))
        self.assertIn(str(task.resolve()), self._git(["worktree", "list", "--porcelain"], main))
        ignored = subprocess.run(["git", "check-ignore", "-q", ".task-state/task.md"], cwd=task)
        self.assertEqual(0, ignored.returncode)
        source = root / "templates-source"
        shutil.copytree(
            ROOT / "components" / "agent-core",
            source / "components" / "agent-core",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self._git(["init", "-b", "main"], source)
        self._git(["config", "user.name", "Test User"], source)
        self._git(["config", "user.email", "test@example.invalid"], source)
        self._git(["add", "components/agent-core"], source)
        self._git(["commit", "-m", "current Agent Core source"], source)
        return task, source, {"release_head": head}

    def _cli(self, repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["AUTOMATION_MAINTENANCE"] = "1"
        result = subprocess.run(["python3", ".automation/bin/automation_upgrade.py", *args], cwd=repo, env=environment, text=True, capture_output=True, check=False)
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result

    def _cli_bootstrap(self, repo: Path, source: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._cli(
            repo,
            [
                "bootstrap-receipt",
                "--source",
                str(source),
                "--expected-source-revision",
                self._expected_source_revision(source),
            ],
            check=check,
        )

    def _source_recovery_bridge_fixture(self, root: Path) -> tuple[Path, Path, Path, dict]:
        """Build the two-generation, shared-object source-recovery topology."""
        bridge = root / "templates-bridge"
        self._git(["clone", "--shared", "--no-checkout", str(ROOT), str(bridge)], ROOT)
        self._git(["switch", "--detach", "HEAD"], bridge)
        implementation = (
            "components/agent-core/.automation/bin/automation_upgrade.py",
            "components/agent-core/.automation/bin/git_private_state.py",
            "tools/automation_recovery_bridge.py",
            "just/agent-core.just",
        )
        for relative in implementation:
            destination = bridge / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        self._git(["config", "user.name", "Test User"], bridge)
        self._git(["config", "user.email", "test@example.invalid"], bridge)
        self._git(["add", *implementation], bridge)
        self._git(["commit", "--allow-empty", "-m", "source recovery bridge implementation"], bridge)
        bridge_head = self._git(["rev-parse", "HEAD"], bridge)
        source = root / "source-v3.0.1"
        source_revision = "b61341feee36eaf16fc37c91323f1da9cb3d671f"
        self.assertEqual(source_revision, self._git(["rev-parse", "v3.0.1^{commit}"], bridge))
        self._git(["worktree", "add", "--detach", str(source), source_revision], bridge)
        self.assertEqual(source_revision, self._git(["rev-parse", "HEAD"], source))
        self.assertNotEqual(source_revision, bridge_head)
        _, task, _ = self._separate_git_topology_fixture(root / "consumer")
        self.assertTrue((task / ".git").is_file())
        old_script = task / ".automation/bin/automation_upgrade.py"
        old_result = json.loads(self._cli(task, ["upgrade", "--source", str(source)]).stdout)
        self.assertEqual("APPLIED", old_result["status"])
        old_script_bytes = old_script.read_bytes()
        # The receipt-bound consumer is intentionally old and does not yet
        # expose Issue #97's expected-revision argument.
        bootstrap_result = json.loads(
            self._cli(task, ["bootstrap-receipt", "--source", str(source)]).stdout
        )
        self.assertEqual("RECEIPT_BOOTSTRAPPED", bootstrap_result["status"])
        receipt = self._receipt(task)
        receipt_bound = {
            path: ((task / Path(*path.split("/"))).read_bytes(), (task / Path(*path.split("/"))).stat().st_mode & 0o777)
            for path in receipt["changed_paths"]
        }
        new_authority, legacy_authority = upgrade._authority_locations(task)
        self.assertFalse(new_authority.exists())
        self.assertIsNotNone(legacy_authority)
        assert legacy_authority is not None
        self.assertTrue(legacy_authority.is_file())
        legacy_authority.unlink()
        metadata = {
            "bridge_head": bridge_head,
            "source_revision": source_revision,
            "old_script": old_script_bytes,
            "receipt_bound": receipt_bound,
            "receipt": receipt,
            "old_result": old_result,
            "legacy_authority": legacy_authority,
            "remote_refs": self._git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], task),
        }
        return bridge, source, task, metadata

    def _bridge_cli(self, bridge: Path, command: str, target: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["AUTOMATION_MAINTENANCE"] = "1"
        result = subprocess.run(
            ["python3", "-I", "tools/automation_recovery_bridge.py", command, str(target), *args],
            cwd=bridge, env=environment, text=True, capture_output=True, check=False,
        )
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result

    def _lifecycle_cli(self, repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["python3", ".automation/bin/task_lifecycle.py", *args],
            cwd=repo,
            env={**os.environ, "AUTOMATION_MAINTENANCE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result

    def _old_process_fixture(self, root: Path) -> tuple[Path, Path, dict]:
        task, source, metadata = self._real_cli_fixture(root)
        metadata["release_version"] = (task / ".automation/VERSION").read_text(encoding="utf-8").strip()
        metadata["source_version"] = (source / "components/agent-core/.automation/VERSION").read_text(encoding="utf-8").strip()
        recipe = (task / ".automation/just/automation.just").read_text(encoding="utf-8")
        self.assertIn("upgrade source:", recipe)
        self.assertIn("python3 '{{script}}' upgrade --source '{{source}}'", recipe)
        result = json.loads(self._cli(task, ["upgrade", "--source", str(source)]).stdout)
        self.assertEqual("APPLIED", result["status"])
        self.assertFalse(upgrade.receipt_path(task).exists())
        self.assertFalse(upgrade.authority_path(task).exists())
        return task, source, result | metadata

    def _separate_git_topology_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        """Create a separate-git-dir checkout and a linked worktree from it."""
        primary = root / "primary"
        primary.mkdir(parents=True)
        self._materialize_release_template(primary)
        admin = root / "primary-admin"
        self._git(["init", "-b", "main", "--separate-git-dir", str(admin)], primary)
        self._git(["config", "user.name", "Test User"], primary)
        self._git(["config", "user.email", "test@example.invalid"], primary)
        self._git(["add", "-A"], primary)
        self._git(["commit", "-m", "release fixture"], primary)
        head = self._git(["rev-parse", "HEAD"], primary)
        self._git(["update-ref", "refs/remotes/origin/main", head], primary)
        self._git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], primary)
        self._git(["switch", "-c", "task/TASK-83-primary"], primary)
        linked = root / "linked"
        self._git(["worktree", "add", "-b", "task/TASK-83-linked", str(linked), "HEAD"], primary)
        for repo, branch in ((primary, "task/TASK-83-primary"), (linked, "task/TASK-83-linked")):
            self._write_file(
                repo / ".task-state/task.md",
                f"- Task ID: TASK-83\n- Branch: {branch}\n- Worktree: {repo.resolve()}\n",
            )
            exclude = Path(self._git(["rev-parse", "--git-path", "info/exclude"], repo))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write("/.task-state/\n")
        source_root = root / "source"
        self._core_root_source(source_root, version=3)
        self._init_source_git(source_root)
        return primary, linked, source_root

    def _task_repo(self, repo: Path, *, task: str = "TASK-78") -> Path:
        repo.mkdir(parents=True)
        self._git(["init", "-b", "main"], repo)
        self._git(["config", "user.name", "Test User"], repo)
        self._git(["config", "user.email", "test@example.invalid"], repo)
        self._write_file(repo / "README.md", "repository\n")
        self._git(["add", "README.md"], repo)
        self._git(["commit", "-m", "initial"], repo)
        self._git(["switch", "-c", f"task/{task}-maintenance"], repo)
        head = self._git(["rev-parse", "HEAD"], repo)
        self._git(["update-ref", "refs/remotes/origin/main", head], repo)
        self._git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], repo)
        state = f"- Task ID: {task}\n- Branch: task/{task}-maintenance\n- Worktree: {repo.resolve()}\n"
        self._write_file(repo / ".task-state/task.md", state)
        exclude = repo / ".git/info/exclude"
        exclude.write_text("/.task-state/\n", encoding="utf-8")
        return repo

    def _maintenance_fixture(self, root: Path, *, destination_version: int = 2) -> tuple[Path, Path]:
        repo = self._task_repo(root / "repo")
        self._destination_repo(repo, version=destination_version)
        self._git(["add", "-A"], repo)
        self._git(["commit", "-m", "fixture"], repo)
        source = self._core_root_source(root, version=3)
        self._init_source_git(root)
        return repo, source

    def _apply_fixture(self, root: Path, *, destination_version: int = 2) -> tuple[Path, Path]:
        repo, source = self._maintenance_fixture(root, destination_version=destination_version)
        with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
            upgrade.apply(repo, root, self._expected_source_revision(root))
        return repo, source

    def _receipt(self, repo: Path) -> dict:
        return json.loads(upgrade.receipt_path(repo).read_text(encoding="utf-8"))

    def test_issue_87_bridge_tree_blob_binds_ls_tree_to_captured_revision(self) -> None:
        revision_a = "a" * 40
        relative = "tools/automation_recovery_bridge.py"
        git_bytes = mock.Mock(
            side_effect=[
                f"100755 blob {'b' * 40}\t{relative}\0".encode("ascii"),
            ]
        )
        with mock.patch.object(bridge_bootstrap, "git_bytes", git_bytes):
            self.assertEqual(
                ("b" * 40, 0o100755),
                bridge_bootstrap._tree_blob(Path("/bridge"), revision_a, relative),
            )
        self.assertEqual(
            ["git", "ls-tree", "-z", revision_a, "--", relative],
            git_bytes.call_args.args[0],
        )
        self.assertNotIn("HEAD", git_bytes.call_args.args[0])

    def test_issue_87_bridge_error_renderer_is_bounded_and_printable(self) -> None:
        rendered = bridge_bootstrap._error_text(Exception("x" * 2000 + "\n\r\x00\x1b"))
        self.assertLessEqual(len(rendered), 1600)
        self.assertEqual(rendered, rendered.replace("\n", "").replace("\r", ""))
        self.assertTrue(all(" " <= character <= "~" for character in rendered))

    def _bootstrap_receipt(self, repo: Path, source_root: Path) -> dict:
        with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
            return upgrade.bootstrap_receipt(repo, source_root, self._expected_source_revision(source_root))

    def _commit_error(self, repo: Path, task: str = "TASK-78") -> str:
        with self.assertRaises(upgrade.UpgradeError) as raised:
            upgrade.commit(repo, task, "maintenance")
        return str(raised.exception)

    def _mock_maintenance(self, repo: Path) -> ExitStack:
        (repo / ".task-state").mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(
                upgrade,
                "require_maintenance",
                return_value=("TASK-TEST", "task/TASK-TEST-test", repo),
            )
        )
        stack.enter_context(mock.patch.object(upgrade, "authority_exists", return_value=False))
        stack.enter_context(mock.patch.object(upgrade, "write_authority"))
        stack.enter_context(mock.patch.object(upgrade.private_state, "prepare"))
        stack.enter_context(
            mock.patch.object(
                upgrade.private_state,
                "mutation_lock",
                return_value=nullcontext(),
            )
        )
        stack.enter_context(mock.patch.object(upgrade, "common_git_dir", return_value=repo))
        stack.enter_context(mock.patch.object(upgrade, "worktree_admin_dir", return_value=repo))
        return stack

    def test_upstream_and_generated_parity(self) -> None:
        upstream = ROOT / "components" / "agent-core" / ".automation" / "UPSTREAM"
        ownership = ROOT / "components" / "agent-core" / ".automation" / "ownership.toml"
        self.assertIn('repository = "github:upiscium/Templates"', upstream.read_text())
        self.assertIn('"AGENTS.md" = "replace"', ownership.read_text())
        for template in ("agent-base", "agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertEqual(
                upstream.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "UPSTREAM").read_bytes(),
            )
            self.assertEqual(
                ownership.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "ownership.toml").read_bytes(),
            )
            self.assertEqual(
                SCRIPT.read_bytes(),
                (ROOT / "templates" / template / ".automation" / "bin" / "automation_upgrade.py").read_bytes(),
            )

    def test_issue_19_authoritative_fixture_metadata_is_explicit(self) -> None:
        fixture = self.AGENT_KNOWLEDGE_VAULT_19
        self.assertEqual("19", fixture["task"])
        self.assertEqual("task/19-agent-core-v3-1-1", fixture["branch"])
        self.assertEqual("1e3a795d5e2717f9c670a812777c4a38c9592db0", fixture["authoritative_external_baseline"])
        self.assertEqual("076653b054f5d8cbce4a28bcb6b381e9f30ee669", fixture["receipt_source_revision"])
        self.assertEqual("835203b6f1ae342d31ed74372728e9862b9b36f0", fixture["expected_source_revision"])
        self.assertIsNone(fixture["maintenance_commit"])
        self.assertFalse(fixture["push"])
        self.assertFalse(fixture["pull_request"])
        # Consumer baseline and expected Templates source are distinct identities.
        self.assertNotEqual(fixture["authoritative_external_baseline"], fixture["expected_source_revision"])

    def test_root_router_and_permissions(self) -> None:
        justfile = (ROOT / "components" / "agent-core" / "Justfile").read_text()
        self.assertIn("mod automation '.automation/just/automation.just'", justfile)
        cfg = json.loads((ROOT / "components" / "agent-core" / "opencode.json").read_text())
        bash = cfg["permission"]["bash"]
        self.assertEqual(bash["just automation::version"], "allow")
        self.assertEqual(bash["just automation::check-update *"], "allow")
        self.assertEqual(bash["just automation::upgrade *"], "ask")
        self.assertEqual(bash["just automation::bootstrap-receipt *"], "ask")
        self.assertEqual(bash["just automation::rebind-maintenance-provenance *"], "ask")
        recipe = (ROOT / "components" / "agent-core" / ".automation" / "just" / "automation.just").read_text()
        self.assertIn("bootstrap-receipt source expected_revision:", recipe)
        self.assertIn(
            "env PYTHONDONTWRITEBYTECODE=1 python3 -B {{quote(script)}} "
            "upgrade --source {{quote(source)}} "
            "--expected-source-revision {{quote(expected_revision)}}",
            recipe,
        )
        self.assertIn(
            "env PYTHONDONTWRITEBYTECODE=1 python3 -B {{quote(script)}} "
            "bootstrap-receipt --source {{quote(source)}} "
            "--expected-source-revision {{quote(expected_revision)}}",
            recipe,
        )
        self.assertIn(
            "check-update --source {{quote(source)}} {{if expected_revision != \"\"",
            recipe,
        )

    def test_upgrade_preserves_adapter_and_repository_owned_paths(self) -> None:
        script = SCRIPT.read_text()
        readme = (ROOT / "README.md").read_text()
        for protected in (
            ".automation/ADAPTER",
            ".automation/INIT.fragment.md",
            ".automation/adoption.toml",
        ):
            self.assertIn(protected, script)
        self.assertIn("just/project/**", readme)
        self.assertIn("repository CI", readme)
        self.assertIn("AUTOMATION_MAINTENANCE", script)
        self.assertIn("operation refused on the default branch", script)
        self.assertIn("commitCreated", script)
        self.assertIn("pushPerformed", script)
        self.assertIn("mergePerformed", script)

    def test_existing_repository_plan_merges_managed_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            automation = repo / ".automation"
            automation.mkdir()
            (automation / "VERSION").write_text("1\n")
            (automation / "ADAPTER").write_text("base\n")
            (automation / "UPSTREAM").write_text(
                'repository = "github:upiscium/Templates"\nref = "main"\ncomponent = "components/agent-core"\n'
            )
            (automation / "ownership.toml").write_text(
                'version = 1\n\n[paths]\n"AGENTS.md" = "replace"\n"Justfile" = "replace"\n'
            )
            core_rules = (ROOT / "components" / "agent-core" / "AGENTS.md").read_text()
            (repo / "AGENTS.md").write_text(
                "# Existing Repository Rules\n\n"
                "<!-- BEGIN AGENT CORE RULES -->\nold core rules\n<!-- END AGENT CORE RULES -->\n"
            )
            (repo / "Justfile").write_text(
                "default:\n    @echo existing\n\n"
                "# Agent Core module router\n"
                "mod agent '.automation/just/agent.just'\n"
                "mod integrate '.automation/just/integrate.just'\n"
                "mod project 'just/project/mod.just'\n"
                "mod? local 'just/local.just'\n"
            )

            plan = upgrade.build_plan(repo, ROOT)
            actions = {item["path"]: item for item in plan["actions"]}
            self.assertEqual(actions["AGENTS.md"]["action"], "merge")
            self.assertEqual(actions["Justfile"]["action"], "merge")
            self.assertTrue(plan["canApply"], plan["blockers"])

            merged_agents, _ = upgrade.replace_agent_rules(
                (repo / "AGENTS.md").read_text(), core_rules
            )
            assert merged_agents is not None
            self.assertIn("# Existing Repository Rules", merged_agents)
            self.assertIn(core_rules.rstrip(), merged_agents)

            merged_just, _ = upgrade.merge_just_router(
                (repo / "Justfile").read_text(),
                (ROOT / "components" / "agent-core" / "Justfile").read_text(),
            )
            assert merged_just is not None
            self.assertIn("@echo existing", merged_just)
            self.assertIn("mod automation '.automation/just/automation.just'", merged_just)

    def test_malformed_rules_and_conflicting_router_block_upgrade(self) -> None:
        bad_agents, reason = upgrade.replace_agent_rules(
            "<!-- BEGIN AGENT CORE RULES -->\nmissing end\n", "new rules\n"
        )
        self.assertIsNone(bad_agents)
        self.assertIn("malformed", reason)

        bad_just, reason = upgrade.merge_just_router(
            "mod agent 'custom/agent.just'\n",
            (ROOT / "components" / "agent-core" / "Justfile").read_text(),
        )
        self.assertIsNone(bad_just)
        self.assertIn("repository-owned path", reason)

    def test_readme_covers_operational_entrypoints(self) -> None:
        readme = (ROOT / "README.md").read_text()
        for phrase in (
            "just automation::version",
            "just automation::check-update",
            "just automation::upgrade",
            "just template::adopt-plan",
            "base",
            "Task Orchestrator",
            "Ask",
            "just project::check",
        ):
            self.assertIn(phrase, readme)

    def test_load_migrations_accepts_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations="schema_version = 1\n")
            self.assertEqual([], upgrade.load_migrations(source))

    def test_load_migrations_accepts_valid_transition(self) -> None:
        manifest = self._migration_manifest(
            remove_paths=(".automation/obsolete.py",),
            require_absent_paths=(".task-state/recovery.json",),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), version=3, migrations=manifest)
            migrations = upgrade.load_migrations(source)
            self.assertEqual(1, len(migrations))
            self.assertEqual((2, 3), (migrations[0].from_version, migrations[0].to_version))

    def test_load_migrations_rejects_unknown_top_level_key(self) -> None:
        manifest = "schema_version = 1\nlegacy = true\n"
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "unknown top-level"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_unknown_migration_field(self) -> None:
        manifest = self._migration_manifest() + "legacy = true\n"
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "unknown fields"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_non_integer_versions(self) -> None:
        manifest = self._migration_manifest().replace("from_version = 2", 'from_version = "2"')
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "positive integers"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_non_consecutive_versions(self) -> None:
        manifest = self._migration_manifest(1, 3)
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "exactly one version"):
                upgrade.load_migrations(source)

    def test_load_migrations_rejects_duplicate_transition(self) -> None:
        transition = self._migration_manifest().split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + transition + "\n" + transition
        with tempfile.TemporaryDirectory() as directory:
            source = self._core_root_source(Path(directory), migrations=manifest)
            with self.assertRaisesRegex(upgrade.UpgradeError, "duplicates transition"):
                upgrade.load_migrations(source)

    def test_remove_path_safety_rejects_absolute_traversal_and_glob(self) -> None:
        for path in ("/tmp/escape", "../escape", ".opencode/*.md"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                source = self._core_root_source(
                    Path(directory), migrations=self._migration_manifest(remove_paths=(path,))
                )
                with self.assertRaisesRegex(upgrade.UpgradeError, "unsafe|exact repository-relative"):
                    upgrade.load_migrations(source)

    def test_remove_path_safety_rejects_unmanaged_and_protected_paths(self) -> None:
        for path in (".automation/ADAPTER", "just/project/mod.just", "README.md", "AGENTS.md"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                source = self._core_root_source(
                    Path(directory), migrations=self._migration_manifest(remove_paths=(path,))
                )
                with self.assertRaisesRegex(upgrade.UpgradeError, "not Agent Core managed"):
                    upgrade.load_migrations(source)

    def test_build_plan_adds_delete_action_for_v2_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".automation/obsolete.py", "obsolete\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete.py",))
            )
            action = self._plan_action(upgrade.build_plan(repo, root), ".automation/obsolete.py")
            self.assertEqual("delete", action["action"])
            self.assertEqual("removed by Agent Core migration 2 -> 3", action["reason"])

    def test_build_plan_absent_obsolete_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete.py",))
            )
            plan = upgrade.build_plan(repo, root)
            self.assertEqual("noop", self._plan_action(plan, ".automation/obsolete.py")["action"])

    def test_build_plan_require_absent_present_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".task-state/recovery.json", "{}\n")
            self._core_root_source(
                root,
                version=3,
                migrations=self._migration_manifest(require_absent_paths=(".task-state/recovery.json",)),
            )
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            message = "\n".join(plan["blockers"])
            self.assertIn("migration 2 -> 3", message)
            self.assertIn(".task-state/recovery.json", message)
            self.assertIn("operator must resolve", message)

    def test_build_plan_require_absent_absent_can_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._core_root_source(
                root,
                version=3,
                migrations=self._migration_manifest(require_absent_paths=(".task-state/recovery.json",)),
            )
            self.assertTrue(upgrade.build_plan(repo, root)["canApply"])

    def test_build_plan_v1_to_v3_selects_later_migrations(self) -> None:
        first = self._migration_manifest(1, 2, remove_paths=(".automation/one",)).split("\n\n", 1)[1]
        second = self._migration_manifest(2, 3, remove_paths=(".automation/two",)).split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + first + "\n" + second
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=1)
            self._write_file(repo / ".automation/one", "1\n")
            self._write_file(repo / ".automation/two", "2\n")
            self._core_root_source(root, version=3, migrations=manifest)
            plan = upgrade.build_plan(repo, root)
            self.assertEqual("delete", self._plan_action(plan, ".automation/one")["action"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/two")["action"])

    def test_later_precondition_accepts_path_planned_for_earlier_deletion(self) -> None:
        first = self._migration_manifest(1, 2, remove_paths=(".automation/old",)).split("\n\n", 1)[1]
        second = self._migration_manifest(
            2,
            3,
            require_absent_paths=(".automation/old",),
        ).split("\n\n", 1)[1]
        manifest = "schema_version = 1\n\n" + first + "\n" + second
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=1)
            self._write_file(repo / ".automation/old", "old\n")
            self._core_root_source(root, version=3, migrations=manifest)
            plan = upgrade.build_plan(repo, root)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/old")["action"])

    def test_build_plan_rejects_downgrade(self) -> None:
        self._require_migration_api()
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=4)
            source = self._core_root_source(tmp, version=3)
            plan = upgrade.build_plan(repo, tmp)
            self.assertFalse(plan["canApply"])
            self.assertIn("downgrade", "\n".join(plan["blockers"]).lower())

    def test_apply_deletes_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".automation/stale.py", "to delete\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/stale.py",))
            )
            self._init_source_git(root)
            with self._mock_maintenance(repo):
                result = upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertIn(".automation/stale.py", result["changedPaths"])
            self.assertFalse((repo / ".automation/stale.py").exists())

    def test_migration_catches_up_residue_after_version_already_advanced(self) -> None:
        manifest = self._migration_manifest(remove_paths=(".automation/legacy.py",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=3)
            self._write_file(repo / ".automation/legacy.py", "legacy\n")
            self._write_file(repo / "opencode.json", "before\n")
            source = self._core_root_source(root, version=3, migrations=manifest)
            self._write_file(source / "opencode.json", "after\n")
            self._init_source_git(root)

            plan = upgrade.build_plan(repo, root)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, ".automation/legacy.py")["action"])

            with self._mock_maintenance(repo):
                upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertFalse((repo / ".automation/legacy.py").exists())
            self.assertEqual("3\n", (repo / ".automation/VERSION").read_text())
            self.assertEqual("after\n", (repo / "opencode.json").read_text())

    def test_apply_symlink_removed_without_deleting_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = repo / "target.txt"
            self._write_file(target, "target\n")
            os.symlink(target, repo / ".automation/link.py")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/link.py",))
            )
            self._init_source_git(root)
            with self._mock_maintenance(repo):
                upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertFalse((repo / ".automation/link.py").exists())
            self.assertTrue(target.exists())

    def test_directory_delete_collision_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            (repo / ".automation/obsolete").mkdir()
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete",))
            )
            self._init_source_git(root)
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("refuses directory deletion", "\n".join(plan["blockers"]))

    def test_managed_destination_symlink_blocks_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = root / "external.json"
            self._write_file(target, "external\n")
            (repo / "opencode.json").unlink()
            os.symlink(target, repo / "opencode.json")
            self._core_root_source(root, version=2)
            self._init_source_git(root)
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("destination symlink", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual("external\n", target.read_text())

    def test_non_directory_managed_parent_blocks_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            self._write_file(repo / ".opencode", "collision\n")
            source = self._core_root_source(root, version=3)
            self._write_file(source / ".opencode/agents/new.md", "new\n")
            self._write_file(repo / "opencode.json", "before\n")
            self._write_file(source / "opencode.json", "after\n")
            self._init_source_git(root)
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("non-directory ancestor .opencode", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual("2\n", (repo / ".automation/VERSION").read_text())
            self.assertEqual("before\n", (repo / "opencode.json").read_text())

    def test_managed_symlink_ancestor_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            target = root / "external-agents"
            target.mkdir()
            (repo / ".opencode").symlink_to(target, target_is_directory=True)
            source = self._core_root_source(root, version=2)
            self._write_file(source / ".opencode/agents/new.md", "new\n")
            plan = upgrade.build_plan(repo, root)
            self.assertFalse(plan["canApply"])
            self.assertIn("symlink ancestor .opencode", "\n".join(plan["blockers"]))
            self.assertEqual([], list(target.iterdir()))

    def test_apply_blocked_path_prevents_all_mutations_and_version_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._destination_repo(root / "repo", version=2)
            (repo / ".automation/obsolete").mkdir()
            self._write_file(repo / "opencode.json", "{\"before\":1}\n")
            self._core_root_source(
                root, version=3, migrations=self._migration_manifest(remove_paths=(".automation/obsolete",))
            )
            self._init_source_git(root)
            with self._mock_maintenance(repo):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual((repo / ".automation" / "VERSION").read_text(), "2\n")
            self.assertEqual((repo / "opencode.json").read_text(), "{\"before\":1}\n")

    def test_apply_writes_VERSION_last_after_source_actions(self) -> None:
        manifest = self._migration_manifest(remove_paths=(".automation/stale.py",))
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=2)
            self._write_file(repo / ".automation/stale.py", "stale\n")
            self._write_file(repo / "opencode.json", "{\"before\":1}\n")
            source = self._core_root_source(tmp, version=3, migrations=manifest)
            self._write_file(source / "opencode.json", "{\"before\":2}\n")
            self._init_source_git(tmp)

            calls: list[str] = []
            original_write = upgrade._anchored_write

            def _mock_write(parent_fd, name, content, mode):
                if name != "VERSION":
                    self.assertEqual("2\n", (repo / ".automation/VERSION").read_text())
                calls.append(name)
                return original_write(parent_fd, name, content, mode)

            with self._mock_maintenance(repo):
                with mock.patch.object(upgrade, "_anchored_write", side_effect=_mock_write):
                    upgrade.apply(repo, tmp, self._expected_source_revision(tmp))
            self.assertTrue(calls, calls)
            self.assertEqual(calls[-1], "VERSION")
            self.assertFalse((repo / ".automation/stale.py").exists())
            self.assertEqual((repo / ".automation" / "VERSION").read_text(), "3\n")

    def test_adapter_is_preserved_by_plan_and_apply(self) -> None:
        manifest = "schema_version = 1\n"
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = self._destination_repo(tmp / "repo", version=2)
            source = self._core_root_source(tmp, version=2, migrations=manifest)
            source_adp = source / ".automation" / "ADAPTER"
            source_parent = source_adp.parent
            source_parent.mkdir(parents=True, exist_ok=True)
            self._write_file(source_adp, "python\n")
            self._init_source_git(tmp)
            plan = upgrade.build_plan(repo, tmp)
            self.assertIsNone(self._plan_action(plan, ".automation/ADAPTER"))
            with self._mock_maintenance(repo):
                with mock.patch.object(upgrade.shutil, "copy2", side_effect=shutil.copy2):
                    upgrade.apply(repo, tmp, self._expected_source_revision(tmp))
            self.assertEqual((repo / ".automation" / "ADAPTER").read_text(), "base\n")

    def test_current_v2_to_v3_migration_removes_obsolete_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            source_root = self._current_source_copy(root / "source")
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            (repo / ".automation" / "VERSION").write_text("2\n", encoding="utf-8")
            for path in self.V3_REMOVED_PATHS:
                self._write_file(repo / path, "obsolete\n")
            plan = upgrade.build_plan(repo, ROOT)
            self.assertTrue(plan["canApply"], plan["blockers"])
            for path in self.V3_REMOVED_PATHS:
                self.assertEqual("delete", self._plan_action(plan, path)["action"], path)

            migrations = upgrade.load_migrations(ROOT / "components/agent-core")
            self.assertEqual(1, len(migrations))
            migration = migrations[0]
            self.assertEqual((2, 3), (migration.from_version, migration.to_version))
            self.assertEqual(tuple(map(Path, self.V3_REMOVED_PATHS)), migration.remove_paths)
            self.assertEqual((Path(".task-state/recovery.json"),), migration.require_absent_paths)

            with self._mock_maintenance(repo):
                upgrade.apply(repo, source_root, self._expected_source_revision(source_root))
            for path in self.V3_REMOVED_PATHS:
                self.assertFalse((repo / path).exists(), path)
            self.assertEqual("3\n", (repo / ".automation" / "VERSION").read_text())

    def test_current_v2_to_v3_active_recovery_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            source_root = self._current_source_copy(root / "source")
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            (repo / ".automation" / "VERSION").write_text("2\n", encoding="utf-8")
            obsolete = repo / self.V3_REMOVED_PATHS[0]
            self._write_file(obsolete, "obsolete\n")
            recovery = repo / ".task-state" / "recovery.json"
            self._write_file(recovery, "{}\n")

            plan = upgrade.build_plan(repo, ROOT)
            self.assertFalse(plan["canApply"])
            self.assertIn(".task-state/recovery.json", "\n".join(plan["blockers"]))
            with self._mock_maintenance(repo):
                with self.assertRaisesRegex(upgrade.UpgradeError, "recovery.json"):
                    upgrade.apply(repo, source_root, self._expected_source_revision(source_root))
            self.assertTrue(obsolete.is_file())
            self.assertTrue(recovery.is_file())
            self.assertEqual("2\n", (repo / ".automation" / "VERSION").read_text())

    def test_current_v3_catches_up_obsolete_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            source_root = self._current_source_copy(root / "source")
            shutil.copytree(ROOT / "templates/agent-base", repo, symlinks=True)
            obsolete = repo / self.V3_REMOVED_PATHS[-1]
            self._write_file(obsolete, "obsolete\n")
            plan = upgrade.build_plan(repo, ROOT)
            self.assertTrue(plan["canApply"], plan["blockers"])
            self.assertEqual("delete", self._plan_action(plan, self.V3_REMOVED_PATHS[-1])["action"])
            with self._mock_maintenance(repo):
                upgrade.apply(repo, source_root, self._expected_source_revision(source_root))
            self.assertFalse(obsolete.exists())
            self.assertEqual("3\n", (repo / ".automation" / "VERSION").read_text())

    def test_template_migrations_and_upgrade_script_parity_after_render(self) -> None:
        for template in self.TEMPLATE_NAMES:
            template_core = ROOT / "templates" / template
            self.assertTrue((template_core / ".automation" / "migrations.toml").exists())
            self.assertEqual(
                (ROOT / "components" / "agent-core" / ".automation" / "migrations.toml").read_bytes(),
                (template_core / ".automation" / "migrations.toml").read_bytes(),
            )
            self.assertEqual(
                SCRIPT.read_bytes(),
                (template_core / ".automation" / "bin" / "automation_upgrade.py").read_bytes(),
            )

    def test_generated_automation_script_just_and_opencode_parity(self) -> None:
        generated = (
            ".automation/bin/automation_upgrade.py",
            ".automation/just/automation.just",
            "opencode.json",
        )
        for template in self.TEMPLATE_NAMES:
            with self.subTest(template=template):
                template_root = ROOT / "templates" / template
                for relative in generated:
                    self.assertEqual(
                        (ROOT / "components" / "agent-core" / relative).read_bytes(),
                        (template_root / relative).read_bytes(),
                        relative,
                    )

    def test_apply_writes_complete_receipt_and_replaces_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source = self._apply_fixture(Path(directory))
            receipt = self._receipt(repo)
            self.assertEqual(1, receipt["schema_version"])
            self.assertEqual("TASK-78", receipt["task_id"])
            self.assertEqual("task/TASK-78-maintenance", receipt["branch"])
            self.assertEqual(str(repo.resolve()), receipt["worktree"])
            self.assertEqual(str(Path(directory).resolve()), receipt["source"])
            self.assertEqual(upgrade.source_revision(Path(directory)), receipt["source_revision"])
            self.assertEqual(["2", "3"], [receipt["current_version"], receipt["upstream_version"]])
            self.assertEqual(sorted(receipt["changed_paths"]), receipt["changed_paths"])
            self.assertTrue(receipt["path_fingerprints"])
            agents = (repo / "AGENTS.md").read_text()
            self.assertIn("BEGIN AGENT CORE RULES", agents)
            self.assertIn("core rules", agents)
            self.assertIn("END AGENT CORE RULES", agents)
            self.assertIn("mod agent", (repo / "Justfile").read_text())
            self.assertEqual("{}\n", (repo / "opencode.json").read_text())
            self.assertEqual("base\n", (repo / ".automation/ADAPTER").read_text())
            self.assertEqual(source, Path(receipt["source"]) / "components" / "agent-core")

    def test_valid_receipt_commit_uses_real_git_repo_and_consumes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            result = upgrade.commit(repo, "TASK-78", "automation maintenance")
            self.assertEqual("COMMITTED", result["status"])
            self.assertEqual(result["commit_sha"], upgrade.git_head(repo))
            self.assertFalse(upgrade.receipt_path(repo).exists())
            consumed = json.loads(upgrade.consumed_receipt_path(repo).read_text())
            self.assertEqual("consumed", consumed["status"])
            self.assertEqual(result["commit_sha"], consumed["commit_sha"])
            self.assertEqual([], upgrade.pending_paths(repo))

    def test_normal_primary_and_linked_worktree_authority_topology(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = self._task_repo(root / "primary")
            linked = root / "linked"
            self._git(["worktree", "add", "-b", "task/TASK-79-linked", str(linked), "HEAD"], primary)

            primary_admin = Path(self._git(["rev-parse", "--absolute-git-dir"], primary))
            linked_admin = Path(self._git(["rev-parse", "--absolute-git-dir"], linked))
            self.assertTrue((primary / ".git").is_dir())
            self.assertTrue((linked / ".git").is_file())
            for repo, admin in ((primary, primary_admin), (linked, linked_admin)):
                with self.subTest(repo=repo.name):
                    self.assertTrue(admin.is_absolute())
                    self.assertTrue(admin.is_dir())
                    authority = upgrade.authority_path(repo).resolve()
                    self.assertIn(admin.resolve(), authority.parents)
                    if (repo / ".git").is_file():
                        self.assertNotIn(repo / ".git", authority.parents)
            self.assertNotEqual(primary_admin.resolve(), linked_admin.resolve())
            self.assertNotEqual(upgrade.authority_path(primary), upgrade.authority_path(linked))

    def test_authority_is_beneath_exact_admin_dir_for_separate_and_linked_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, linked, source = self._separate_git_topology_fixture(Path(directory))
            for repo, task, branch in (
                (primary, "TASK-83", "task/TASK-83-primary"),
                (linked, "TASK-83", "task/TASK-83-linked"),
            ):
                with self.subTest(repo=repo.name):
                    admin = Path(self._git(["rev-parse", "--absolute-git-dir"], repo))
                    self.assertTrue(admin.is_absolute())
                    self.assertTrue(admin.is_dir())
                    visible_git = repo / ".git"
                    self.assertTrue(visible_git.is_file())
                    self.assertNotIn(visible_git, upgrade.authority_path(repo).parents)
                    with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                        upgrade.apply(repo, source, self._expected_source_revision(source))
                    authority = upgrade.authority_path(repo).resolve()
                    self.assertIn(admin.resolve(), authority.parents)
                    self.assertNotIn(visible_git, authority.parents)
            self.assertNotEqual(upgrade.authority_path(primary), upgrade.authority_path(linked))
            self.assertNotEqual(
                upgrade.authority_path(primary).parent.resolve(),
                upgrade.authority_path(linked).parent.resolve(),
            )
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.validate_authority(primary, self._receipt(linked))
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.validate_authority(linked, self._receipt(primary))
            self.assertEqual("COMMITTED", upgrade.commit(primary, "TASK-83", "maintenance")["status"])
            self.assertEqual("COMMITTED", upgrade.commit(linked, "TASK-83", "maintenance")["status"])

    def test_second_successful_upgrade_replaces_consumed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root / "first")
            first = self._receipt(repo)
            upgrade.commit(repo, "TASK-78", "first maintenance")
            consumed = upgrade.consumed_receipt_path(repo)
            self.assertTrue(consumed.exists())

            second_root = root / "second"
            second_source = self._core_root_source(second_root, version=4)
            (second_source / "AGENTS.md").write_bytes((repo / "AGENTS.md").read_bytes())
            self._init_source_git(second_root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                upgrade.apply(repo, second_root, self._expected_source_revision(second_root))
            second = self._receipt(repo)
            self.assertTrue(upgrade.receipt_path(repo).exists())
            self.assertFalse(consumed.exists())
            self.assertNotEqual(first["authority_head"], second["authority_head"])
            self.assertEqual("3", second["current_version"])
            self.assertEqual("4", second["upstream_version"])
            self.assertEqual([".automation/VERSION"], second["changed_paths"])
            self.assertNotIn("README.md", second["changed_paths"])

    def test_apply_rolls_back_receipt_when_authority_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            before = {
                path: (repo / path).read_bytes()
                for path in (".automation/VERSION", "AGENTS.md", "Justfile", "opencode.json")
            }
            status_before = self._git(["status", "--porcelain"], repo)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(upgrade, "write_authority", side_effect=upgrade.UpgradeError("injected authority failure")):
                with self.assertRaisesRegex(upgrade.UpgradeError, "injected authority failure"):
                    upgrade.apply(repo, source.parents[1], self._expected_source_revision(source.parents[1]))
            self.assertEqual(before, {path: (repo / path).read_bytes() for path in before})
            self.assertEqual(status_before, self._git(["status", "--porcelain"], repo))
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.authority_path(repo).exists())
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                self.assertEqual(
                    "APPLIED",
                    upgrade.apply(
                        repo,
                        source.parents[1],
                        self._expected_source_revision(source.parents[1]),
                    )["status"],
                )

    def test_apply_rolls_back_when_stale_consumed_receipt_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            consumed = upgrade.consumed_receipt_path(repo)
            consumed.write_bytes(b"stale\n")
            before = {
                path: (repo / path).read_bytes()
                for path in (".automation/VERSION", "AGENTS.md", "Justfile", "opencode.json")
            }
            original_unlink = Path.unlink
            failed = False

            def fail_once(path: Path, *args, **kwargs):
                nonlocal failed
                if path == consumed and not failed:
                    failed = True
                    raise OSError("injected stale receipt cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_once):
                with self.assertRaisesRegex(upgrade.UpgradeError, "cannot remove stale consumed receipt"):
                    upgrade.apply(repo, source.parents[1], self._expected_source_revision(source.parents[1]))
            self.assertTrue(failed)
            self.assertEqual(
                before,
                {path: (repo / path).read_bytes() for path in before},
            )
            self.assertEqual(b"stale\n", consumed.read_bytes())
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.authority_path(repo).exists())

    def test_apply_rolls_back_when_post_apply_validation_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            before = {
                path: (repo / path).read_bytes()
                for path in (".automation/VERSION", "AGENTS.md", "Justfile", "opencode.json")
            }

            def reject_post_apply() -> None:
                raise upgrade.UpgradeError("injected target identity drift")

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "target identity drift"):
                    upgrade.apply(
                        repo,
                        source.parents[1],
                        self._expected_source_revision(source.parents[1]),
                        reject_post_apply,
                    )
            self.assertEqual(before, {path: (repo / path).read_bytes() for path in before})
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.authority_path(repo).exists())

    def test_apply_holds_task_state_lock_through_post_apply_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            events: list[str] = []

            @contextmanager
            def state_lock():
                events.append("locked")
                try:
                    yield
                finally:
                    events.append("released")

            def validate() -> None:
                self.assertEqual(["locked"], events)
                events.append("validated")

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                upgrade.apply(
                    repo,
                    source.parents[1],
                    self._expected_source_revision(source.parents[1]),
                    validate,
                    state_lock,
                )
            self.assertEqual(["locked", "validated", "released"], events)

    def test_apply_rejects_direct_destination_symlink_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            outside = root / "outside-version"
            outside.write_text("outside\n", encoding="utf-8")
            original_write = upgrade._anchored_write
            swapped = False

            def swap_destination(parent_fd, name, content, mode):
                nonlocal swapped
                if name == "VERSION" and not swapped:
                    swapped = True
                    target = repo / ".automation" / name
                    target.unlink()
                    target.symlink_to(outside)
                return original_write(parent_fd, name, content, mode)

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(upgrade, "_anchored_write", side_effect=swap_destination):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, source.parents[1], self._expected_source_revision(source.parents[1]))
            self.assertTrue(swapped)
            self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))
            self.assertEqual("2\n", (repo / ".automation/VERSION").read_text(encoding="utf-8"))
            self.assertFalse(upgrade.authority_path(repo).exists())

    def test_apply_rejects_ancestor_directory_symlink_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            self._write_file(source / ".opencode/agents/new.md", "new\n")
            self._init_source_git(root)
            outside = root / "outside-opencode"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_text("outside\n", encoding="utf-8")
            original_write = upgrade._anchored_write
            swapped = False

            def swap_ancestor(parent_fd, name, content, mode):
                nonlocal swapped
                if name == "new.md" and not swapped:
                    swapped = True
                    managed_parent = repo / ".opencode"
                    moved = repo / ".opencode-real"
                    managed_parent.rename(moved)
                    managed_parent.symlink_to(outside, target_is_directory=True)
                return original_write(parent_fd, name, content, mode)

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(upgrade, "_anchored_write", side_effect=swap_ancestor):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.apply(repo, source.parents[1], self._expected_source_revision(source.parents[1]))
            self.assertTrue(swapped)
            self.assertEqual("outside\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual("2\n", (repo / ".automation/VERSION").read_text(encoding="utf-8"))
            self.assertFalse(upgrade.authority_path(repo).exists())

    def test_issue_pair_does_not_overwrite_a_concurrent_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            receipt = self._receipt(repo)
            authority = upgrade.authority_path(repo)
            upgrade.receipt_path(repo).unlink()
            authority.unlink()
            first = dict(receipt)
            second = dict(receipt, authority_nonce="1" * 64)
            entered = threading.Event()
            release = threading.Event()
            first_errors: list[BaseException] = []
            second_errors: list[BaseException] = []
            original_write = upgrade.write_authority

            def blocked_write(target: Path, value: dict) -> None:
                if value["authority_nonce"] == first["authority_nonce"]:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("timed out waiting to release first issuer")
                original_write(target, value)

            def issue(value: dict, errors: list[BaseException]) -> None:
                try:
                    upgrade.issue_pair(repo, value)
                except BaseException as exc:  # capture thread failures for the test thread
                    errors.append(exc)

            with mock.patch.object(upgrade, "write_authority", side_effect=blocked_write):
                first_thread = threading.Thread(target=issue, args=(first, first_errors))
                first_thread.start()
                self.assertTrue(entered.wait(timeout=5))
                second_thread = threading.Thread(target=issue, args=(second, second_errors))
                second_thread.start()
                second_thread.join(timeout=5)
                self.assertFalse(second_thread.is_alive())
                release.set()
                first_thread.join(timeout=5)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual([], first_errors)
            self.assertEqual(1, len(second_errors))
            self.assertIsInstance(second_errors[0], upgrade.UpgradeError)
            self.assertEqual(first, self._receipt(repo))
            self.assertEqual(authority, upgrade.validate_authority(repo, first))

    def test_task_state_symlink_is_rejected_without_writing_outside_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            state = repo / ".task-state"
            outside = root / "outside-state"
            outside.mkdir()
            shutil.rmtree(state)
            state.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(upgrade.UpgradeError):
                self._bootstrap_receipt(repo, source.parents[1])
            self.assertEqual([], list(outside.iterdir()))

    def test_commit_rolls_back_when_consumed_receipt_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            active = upgrade.receipt_path(repo)
            authority = upgrade.authority_path(repo)
            active_bytes = active.read_bytes()
            authority_bytes = authority.read_bytes()
            original_replace = upgrade.os.replace

            def fail_active_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                if Path(source) == active and Path(destination) == upgrade.consumed_receipt_path(repo):
                    raise OSError("injected receipt consume failure")
                original_replace(source, destination)

            with mock.patch.object(upgrade.os, "replace", side_effect=fail_active_replace):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.commit(repo, "TASK-78", "maintenance")
            self.assertEqual(active_bytes, active.read_bytes())
            self.assertEqual(authority_bytes, authority.read_bytes())
            self.assertFalse(upgrade.consumed_receipt_path(repo).exists())
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "maintenance")["status"])

    def test_commit_rolls_back_when_authority_unlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            active = upgrade.receipt_path(repo)
            authority = upgrade.authority_path(repo)
            active_bytes = active.read_bytes()
            authority_bytes = authority.read_bytes()
            original_unlink = upgrade.private_state.unlink

            def fail_authority_unlink(path: Path, *args, **kwargs) -> None:
                if path == authority:
                    raise OSError("injected authority consume failure")
                original_unlink(path, *args, **kwargs)

            with mock.patch.object(upgrade.private_state, "unlink", side_effect=fail_authority_unlink):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.commit(repo, "TASK-78", "maintenance")
            self.assertEqual(active_bytes, active.read_bytes())
            self.assertEqual(authority_bytes, authority.read_bytes())
            self.assertFalse(upgrade.consumed_receipt_path(repo).exists())
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "maintenance")["status"])

    def test_fresh_bootstrap_rolls_back_receipt_when_authority_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, source, _ = self._old_process_fixture(Path(directory))
            with mock.patch.object(
                upgrade,
                "write_authority",
                side_effect=upgrade.UpgradeError("injected authority failure"),
            ):
                with self.assertRaisesRegex(upgrade.UpgradeError, "injected authority failure"):
                    self._bootstrap_receipt(task, source)
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            result = self._bootstrap_receipt(task, source)
            self.assertEqual("RECEIPT_BOOTSTRAPPED", result["status"])
            self.assertEqual("COMMITTED", upgrade.commit(task, "TASK-78", "maintenance")["status"])

    def test_existing_receipt_without_authority_is_recovered_without_rewriting_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source = self._apply_fixture(Path(directory))
            receipt_path = upgrade.receipt_path(repo)
            receipt_bytes = receipt_path.read_bytes()
            upgrade.authority_path(repo).unlink()
            result = self._bootstrap_receipt(repo, source.parents[1])
            self.assertEqual("AUTHORITY_RECOVERED", result["status"])
            self.assertEqual(receipt_bytes, receipt_path.read_bytes())
            self.assertTrue(upgrade.authority_path(repo).is_file())
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "maintenance")["status"])

    def test_rebootstrap_with_receipt_and_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source = self._apply_fixture(Path(directory))
            with self.assertRaisesRegex(upgrade.UpgradeError, "existing receipt or authority"):
                self._bootstrap_receipt(repo, source.parents[1])

    def test_bootstrap_recovery_fails_closed_for_receipt_and_pending_state_tampering(self) -> None:
        receipt_fields = (
            ("schema_version", 2),
            ("task_id", "OTHER"),
            ("branch", "task/OTHER-maintenance"),
            ("worktree", "/other/worktree"),
            ("source", "/other/source"),
            ("source_revision", "0" * 40),
            ("authority_head", "0" * 40),
        )
        for field, value in receipt_fields:
            with self.subTest(kind=f"receipt:{field}"), tempfile.TemporaryDirectory() as directory:
                repo, source = self._apply_fixture(Path(directory))
                upgrade.authority_path(repo).unlink()
                receipt = self._receipt(repo)
                receipt[field] = value
                upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
                with self.assertRaises(upgrade.UpgradeError):
                    self._bootstrap_receipt(repo, source.parents[1])
                self.assertFalse(upgrade.authority_path(repo).exists())

        for kind in ("changed_paths", "fingerprint", "head", "source_revision", "pending_added", "pending_removed", "content", "mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, source = self._apply_fixture(root)
                upgrade.authority_path(repo).unlink()
                receipt = self._receipt(repo)
                if kind == "changed_paths":
                    receipt["changed_paths"] = receipt["changed_paths"][:-1]
                elif kind == "fingerprint":
                    path = receipt["changed_paths"][0]
                    receipt["path_fingerprints"][path]["content_sha256"] = "0" * 64
                elif kind == "head":
                    self._write_file(repo / "README.md", "head changed\n")
                    self._git(["add", "README.md"], repo)
                    self._git(["commit", "-m", "changed head"], repo)
                elif kind == "source_revision":
                    self._write_file(source / "source-change", "changed\n")
                    self._git(["add", "source-change"], source)
                    self._git(["commit", "-m", "changed source"], source)
                elif kind == "pending_added":
                    self._write_file(repo / ".automation/pending-added", "added\n")
                else:
                    path = receipt["changed_paths"][0]
                    target = repo / Path(*path.split("/"))
                    if kind == "pending_removed":
                        self._git(["restore", "--source=HEAD", "--", path], repo)
                    elif kind == "content":
                        target.write_bytes(target.read_bytes() + b"tampered")
                    elif kind == "mode":
                        target.chmod(target.stat().st_mode ^ 0o100)
                if kind in ("changed_paths", "fingerprint"):
                    upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
                with self.assertRaises(upgrade.UpgradeError):
                    self._bootstrap_receipt(repo, source.parents[1])
                self.assertFalse(upgrade.authority_path(repo).exists())

    def test_legacy_authority_is_consumed_only_when_new_record_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            new_path, legacy_path = upgrade._authority_locations(repo)
            self.assertIsNotNone(legacy_path)
            assert legacy_path is not None
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(new_path.read_bytes())
            new_path.unlink()
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "legacy maintenance")["status"])
            self.assertFalse(legacy_path.exists())

    def test_commit_rejects_unsafe_canonical_authority_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            authority = upgrade.authority_path(repo)
            authority.chmod(0o666)
            self.assertIn("unsafe canonical private-state record mode", self._commit_error(repo))

    def test_linked_worktree_ignores_legacy_authority_under_escaped_common_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, linked, source = self._separate_git_topology_fixture(root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                upgrade.apply(linked, source, self._expected_source_revision(source))
            receipt = self._receipt(linked)
            new_path, legacy_path = upgrade._authority_locations(linked)
            self.assertIsNotNone(legacy_path)
            assert legacy_path is not None
            authority_bytes = new_path.read_bytes()
            new_path.unlink()
            self.assertFalse(new_path.exists())

            common = upgrade.common_git_dir(linked)
            external = root / "escaped-authority"
            external.mkdir()
            common_opencode = common / "opencode"
            self.assertFalse(common_opencode.exists())
            common_opencode.symlink_to(external, target_is_directory=True)
            (external / "automation-maintenance").mkdir()
            legacy_path.write_bytes(authority_bytes)
            external_authority = external / "automation-maintenance" / legacy_path.name
            external_before = external_authority.read_bytes()

            locations = upgrade._authority_locations(linked)
            self.assertIsNone(locations[1])
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.validate_authority(linked, receipt)
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.commit(linked, "TASK-83", "maintenance")
            self.assertEqual(external_before, external_authority.read_bytes())
            self.assertEqual(authority_bytes, external_authority.read_bytes())

            with self.assertRaises(upgrade.UpgradeError):
                upgrade._write_authority_at(
                    common / "opencode" / "automation-maintenance" / "escaped.json",
                    json.loads(authority_bytes),
                    admin=common.resolve(),
                )
            self.assertEqual(external_before, external_authority.read_bytes())

    def test_legacy_authority_is_restored_when_commit_fails_after_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            new_path, legacy_path = upgrade._authority_locations(repo)
            self.assertIsNotNone(legacy_path)
            assert legacy_path is not None
            authority_bytes = new_path.read_bytes()
            active_bytes = upgrade.receipt_path(repo).read_bytes()
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(authority_bytes)
            new_path.unlink()

            original_run = upgrade.run

            def fail_staging_check(command, *args, **kwargs):
                if command == ["git", "diff", "--no-ext-diff", "--cached", "--check"]:
                    raise upgrade.UpgradeError("injected staging check failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(upgrade, "run", side_effect=fail_staging_check):
                with self.assertRaisesRegex(upgrade.UpgradeError, "injected staging check failure"):
                    upgrade.commit(repo, "TASK-78", "legacy rollback")
            self.assertEqual(active_bytes, upgrade.receipt_path(repo).read_bytes())
            current_new, current_fallback = upgrade._authority_locations(repo)
            restored = current_new if current_new.exists() else current_fallback
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(authority_bytes, restored.read_bytes())
            self.assertFalse(upgrade.consumed_receipt_path(repo).exists())
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "legacy retry")["status"])
            self.assertFalse(legacy_path.exists())

    def test_malformed_new_authority_blocks_legacy_fallback_and_non_directory_common_git_is_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            new_path, legacy_path = upgrade._authority_locations(repo)
            assert legacy_path is not None
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(new_path.read_bytes())
            new_path.write_text("not json\n", encoding="utf-8")
            self.assertRegex(
                self._commit_error(repo),
                "invalid successful-upgrade authority|invalid legacy private-state record",
            )
            new_path.unlink()
            common_file = root / "common-file"
            common_file.write_text("not a git directory\n", encoding="utf-8")
            with mock.patch.object(upgrade, "common_git_dir", return_value=common_file):
                self.assertRegex(
                    self._commit_error(repo),
                    "missing or invalid successful-upgrade authority|cannot inspect private-state namespace",
                )

    def test_no_change_upgrade_preserves_consumed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            upgrade.commit(repo, "TASK-78", "maintenance")
            consumed_before = upgrade.consumed_receipt_path(repo).read_bytes()

            no_changes = {
                "currentVersion": "3",
                "upstreamVersion": "3",
                "actions": [],
                "blockers": [],
            }
            with mock.patch.dict(
                os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False
            ), mock.patch.object(upgrade, "build_plan", return_value=no_changes):
                result = upgrade.apply(repo, root, self._expected_source_revision(root))

            self.assertEqual("NO_CHANGES", result["status"])
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertEqual(consumed_before, upgrade.consumed_receipt_path(repo).read_bytes())

    def test_forged_receipt_without_upgrade_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            upgrade.authority_path(repo).unlink()
            self.assertIn("successful-upgrade authority", self._commit_error(repo))

    def test_git_hooks_cannot_expand_maintenance_commit_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            hooks = Path(directory) / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf 'hooked\\n' > product-from-hook.txt\ngit add product-from-hook.txt\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            self._git(["config", "core.hooksPath", str(hooks)], repo)

            upgrade.commit(repo, "TASK-78", "maintenance")

            self.assertFalse((repo / "product-from-hook.txt").exists())
            committed = self._git(["show", "--pretty=", "--name-only", "HEAD"], repo).splitlines()
            self.assertNotIn("product-from-hook.txt", committed)

    def test_ambient_git_execution_and_identity_overrides_are_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            external_diff = Path(directory) / "external-diff"
            marker = Path(directory) / "external-diff-ran"
            external_diff.write_text(
                f"#!/bin/sh\ntouch {marker}\n",
                encoding="utf-8",
            )
            external_diff.chmod(0o755)

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_EXTERNAL_DIFF": str(external_diff),
                    "GIT_AUTHOR_NAME": "Injected Author",
                    "GIT_AUTHOR_EMAIL": "injected@example.invalid",
                    "GIT_COMMITTER_NAME": "Injected Committer",
                    "GIT_COMMITTER_EMAIL": "injected@example.invalid",
                },
                clear=False,
            ):
                upgrade.commit(repo, "TASK-78", "maintenance")

            self.assertFalse(marker.exists())
            self.assertEqual("Test User", self._git(["show", "-s", "--format=%an", "HEAD"], repo))
            self.assertEqual("Test User", self._git(["show", "-s", "--format=%cn", "HEAD"], repo))

    def test_ordinary_task_commit_still_rejects_automation_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nautomation_core = ["Justfile", ".automation/**"]\nsecret_patterns = []\n')
            self._write_file(repo / "Justfile", "changed\n")
            with mock.patch.object(agent_core, "ensure_task_branch", return_value="task/TASK-78-maintenance"), \
                    mock.patch.object(agent_core, "pending_paths", return_value=["Justfile"]), \
                    mock.patch.object(agent_core, "run") as run:
                with self.assertRaisesRegex(agent_core.AutomationError, "Automation Core"):
                    agent_core.commit_task(repo, "TASK-78", "ordinary")
            run.assert_not_called()

    def test_maintenance_environment_alone_is_not_commit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nautomation_core = []\nsecret_patterns = []\n')
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                error = self._commit_error(repo)
            self.assertIn("no active successful automation upgrade receipt", error)

    def test_receipt_rejects_product_adapter_repository_secret_and_task_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            self._write_file(repo / ".automation/policy.toml", '[paths]\nsecret_patterns = ["secret"]\n')
            for path in ("product.txt", ".automation/ADAPTER", "just/project/mod.just", ".automation/secret.json", ".task-state/task.md"):
                with self.subTest(path=path):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.receipt_paths(repo, {"changed_paths": [path]})

    def test_receipt_rejects_missing_extra_and_identity_mismatched_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            original = self._receipt(repo)
            original["changed_paths"] = original["changed_paths"][:-1]
            original["path_fingerprints"].pop(sorted(self._receipt(repo)["changed_paths"])[-1])
            upgrade.atomic_json_write(upgrade.receipt_path(repo), original)
            self.assertIn("pending paths do not exactly match", self._commit_error(repo))

            receipt = self._receipt(repo)
            self._write_file(repo / ".automation/extra", "extra\n")
            self.assertIn("pending paths do not exactly match", self._commit_error(repo))

    def test_receipt_rejects_wrong_task_branch_worktree_stale_head_and_fingerprint(self) -> None:
        for field, value, expected in (
            ("task_id", "OTHER", "identity"),
            ("branch", "task/TASK-78-other", "identity"),
            ("worktree", "/other/worktree", "identity"),
            ("authority_head", "0" * 40, "HEAD"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                repo, _ = self._apply_fixture(Path(directory))
                receipt = self._receipt(repo)
                receipt[field] = value
                upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
                self.assertIn(expected, self._commit_error(repo))

        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            path = self._receipt(repo)["changed_paths"][0]
            target = repo / Path(*path.split("/"))
            target.write_bytes(target.read_bytes() + b"changed")
            self.assertIn("fingerprint changed", self._commit_error(repo))
            target.chmod(target.stat().st_mode ^ 0o100)
            # Restore content, then mode alone must still invalidate the receipt.
            target.write_bytes(target.read_bytes()[:-7])
            self.assertIn("fingerprint changed", self._commit_error(repo))

    def test_task_state_identity_mismatches_are_rejected(self) -> None:
        for replacement, expected in (
            ("- Task ID: OTHER", "does not match requested Task"),
            ("- Branch: task/OTHER-maintenance", "not the Task branch"),
            ("- Worktree: /other/worktree", "does not match the current worktree"),
        ):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                repo, _ = self._apply_fixture(Path(directory))
                state = repo / ".task-state/task.md"
                text = state.read_text(encoding="utf-8")
                label, value = replacement.split(": ", 1)
                lines = [line if not line.startswith(label) else replacement for line in text.splitlines()]
                state.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self.assertIn(expected, self._commit_error(repo))

        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            state = repo / ".task-state/task.md"
            state.write_text(
                state.read_text(encoding="utf-8").replace(
                    "- Branch: task/TASK-78-maintenance", "- Branch: task/TASK-78-unregistered"
                ),
                encoding="utf-8",
            )
            self.assertIn("not registered", self._commit_error(repo))

    def test_missing_consumed_receipt_and_cached_diff_failure_restore_active_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._apply_fixture(Path(directory))
            receipt = self._receipt(repo)
            upgrade.receipt_path(repo).unlink()
            self.assertIn("no active", self._commit_error(repo))
            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            upgrade.atomic_json_write(upgrade.consumed_receipt_path(repo), receipt | {"status": "consumed"})
            upgrade.receipt_path(repo).unlink()
            self.assertIn("no active", self._commit_error(repo))

            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            path = receipt["changed_paths"][0]
            target = repo / Path(*path.split("/"))
            original_bytes = target.read_bytes()
            target.write_bytes(original_bytes.rstrip(b"\n") + b"  \n")
            updated = self._receipt(repo)
            updated["path_fingerprints"][path] = upgrade.file_fingerprint(repo, path)
            upgrade.atomic_json_write(upgrade.receipt_path(repo), updated)
            upgrade.authority_path(repo).unlink()
            upgrade.write_authority(repo, updated)
            self.assertIn("git diff --no-ext-diff --cached --check", self._commit_error(repo))
            target.write_bytes(original_bytes)
            self._git(["reset", "--mixed", "HEAD"], repo)
            upgrade.atomic_json_write(upgrade.receipt_path(repo), receipt)
            upgrade.authority_path(repo).unlink()
            upgrade.write_authority(repo, receipt)
            original_run = upgrade.run

            def fake_run(command, *, cwd, **kwargs):
                if command == [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--cached",
                    "--name-only",
                ]:
                    return subprocess.CompletedProcess(command, 0, stdout="AGENTS.md\nextra\n", stderr="")
                return original_run(command, cwd=cwd, **kwargs)

            with mock.patch.object(
                upgrade, "pending_paths", return_value=receipt["changed_paths"]
            ), mock.patch.object(upgrade, "run", side_effect=fake_run):
                self.assertIn("staged paths", self._commit_error(repo))
            self.assertTrue(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.consumed_receipt_path(repo).exists())

    def test_ignored_source_artifacts_are_absent_from_public_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            self._ignore_source_path(root, "__pycache__/")
            self._ignore_source_path(root, "*.pyc")
            self._ignore_source_path(root, "*.pyo")
            pyc = source / ".automation/bin/__pycache__/upgrade.cpython-313.pyc"
            stale = source / ".automation/bin/model_fallback.pyc"
            generated = source / ".automation/generated/cache.txt"
            self._write_file(pyc, "pyc\n")
            self._write_file(stale, "stale\n")
            self._ignore_source_path(root, "/components/agent-core/.automation/generated/")
            self._write_file(generated, "generated\n")

            plan = upgrade.check_update(repo, root)
            planned = {item["path"] for item in plan["actions"]}
            self.assertNotIn(".automation/bin/__pycache__/upgrade.cpython-313.pyc", planned)
            self.assertNotIn(".automation/bin/model_fallback.pyc", planned)
            self.assertNotIn(".automation/generated/cache.txt", planned)
            self.assertFalse(any(".pyc" in path for path in planned))
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                result = upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual("APPLIED", result["status"])
            for relative in (pyc, stale, generated):
                self.assertFalse((repo / relative.relative_to(source)).exists())

    def test_untracked_nonignored_source_rejects_check_update_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            self._write_file(source / ".automation/bin/new.py", "new\n")
            with self.assertRaisesRegex(upgrade.UpgradeError, "source components/agent-core must be clean"):
                upgrade.check_update(repo, root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "source components/agent-core must be clean"):
                    upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertFalse(upgrade.receipt_path(repo).exists())

    def test_tracked_source_modification_rejects_check_update_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            self._write_file(source / "opencode.json", "changed\n")
            with self.assertRaisesRegex(upgrade.UpgradeError, "source components/agent-core must be clean"):
                upgrade.check_update(repo, root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "source components/agent-core must be clean"):
                    upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertFalse(upgrade.receipt_path(repo).exists())

    def test_clean_tracked_source_plan_apply_and_revision_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            head = self._git(["rev-parse", "HEAD"], root)
            plan = upgrade.check_update(repo, root)
            self.assertEqual(head, plan["sourceRevision"])
            self.assertTrue(plan["canApply"], plan["blockers"])
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                result = upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(head, result["sourceRevision"])
            self.assertEqual(head, self._receipt(repo)["source_revision"])
            self.assertEqual(head, upgrade.source_revision(root))

    def test_expected_source_revision_guard_stores_exact_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root)
            expected = self._expected_source_revision(root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                result = upgrade.apply(repo, root, expected)
            self.assertEqual(expected, result["sourceRevision"])
            self.assertEqual(expected, self._receipt(repo)["source_revision"])

    def test_expected_source_revision_mismatch_precedes_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._maintenance_fixture(root)
            expected = self._expected_source_revision(root)
            before = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in (repo / "AGENTS.md", repo / "Justfile", repo / "opencode.json")
            }
            self._git(["commit", "--allow-empty", "-m", "byte-identical source HEAD"], root)
            actual = self._expected_source_revision(root)
            self.assertNotEqual(expected, actual)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "expected source revision"):
                    upgrade.apply(repo, root, expected)
            self.assertEqual(before, {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in before
            })
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.authority_path(repo).exists())

    def test_source_revision_inputs_are_exact_and_check_update_reports_actual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._maintenance_fixture(root)
            actual = self._expected_source_revision(root)
            invalid = (actual[:39], actual.upper(), "HEAD", "v3.0.0", "f" * 41, "f" * 63)
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.check_update(repo, root, value)
            self._git(["commit", "--allow-empty", "-m", "source movement"], root)
            current = self._expected_source_revision(root)
            with self.assertRaises(upgrade.UpgradeError):
                upgrade.check_update(repo, root, actual)
            plan = upgrade.check_update(repo, root)
            self.assertEqual(current, plan["sourceRevision"])

    def test_rebind_changes_only_provenance_and_commit_consumes_rebound_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            old_receipt = self._receipt(repo)
            old_authority = upgrade.authority_path(repo).read_bytes()
            status_before = self._git(["status", "--porcelain=v1"], repo)
            target_snapshot = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in repo.glob("**/*")
                if path.is_file() and ".git" not in path.parts and ".task-state" not in path.parts
            }
            self._git(["commit", "--allow-empty", "-m", "byte-identical source revision"], root)
            expected = self._expected_source_revision(root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                result = upgrade.rebind_maintenance_provenance(repo, root, expected)
            self.assertEqual("PROVENANCE_REBOUND", result["status"])
            rebound = self._receipt(repo)
            self.assertEqual(expected, rebound["source_revision"])
            self.assertNotEqual(old_receipt["authority_nonce"], rebound["authority_nonce"])
            self.assertNotEqual(old_authority, upgrade.authority_path(repo).read_bytes())
            self.assertEqual(target_snapshot, {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in target_snapshot
            })
            self.assertEqual(status_before, self._git(["status", "--porcelain=v1"], repo))
            self.assertEqual("COMMITTED", upgrade.commit(repo, "TASK-78", "rebound maintenance")["status"])

    def test_rebind_already_correct_pair_is_byte_and_inode_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            expected = self._expected_source_revision(root)
            receipt = upgrade.receipt_path(repo)
            authority = upgrade.authority_path(repo)
            before = {
                path: (path.read_bytes(), path.stat().st_ino)
                for path in (receipt, authority)
            }
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                result = upgrade.rebind_maintenance_provenance(repo, root, expected)
            self.assertEqual("PROVENANCE_ALREADY_BOUND", result["status"])
            self.assertEqual(before, {
                path: (path.read_bytes(), path.stat().st_ino)
                for path in before
            })

    def _wrong_provenance_fixture(self, root: Path) -> tuple[Path, Path, str]:
        repo, _ = self._apply_fixture(root)
        self._git(["commit", "--allow-empty", "-m", "byte-identical expected source revision"], root)
        return repo, root, self._expected_source_revision(root)

    def _assert_active_pair_bytes(self, repo: Path, expected: tuple[bytes, bytes]) -> None:
        self.assertEqual(expected[0], upgrade.receipt_path(repo).read_bytes())
        self.assertEqual(expected[1], upgrade.authority_path(repo).read_bytes())

    def test_rebind_rejects_changed_head_consumed_state_and_unauthorized_paths(self) -> None:
        cases = ("head", "consumed", "product", "adapter", "content", "mode")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo, source, expected = self._wrong_provenance_fixture(Path(directory))
                pair = (upgrade.receipt_path(repo).read_bytes(), upgrade.authority_path(repo).read_bytes())
                receipt = self._receipt(repo)
                target = repo / Path(*receipt["changed_paths"][0].split("/"))
                if case == "head":
                    self._git(["commit", "--allow-empty", "-m", "unexpected consumer HEAD"], repo)
                elif case == "consumed":
                    upgrade.atomic_json_write(upgrade.consumed_receipt_path(repo), {"status": "consumed"})
                elif case == "product":
                    self._write_file(repo / "product.txt", "not Agent Core\n")
                elif case == "adapter":
                    self._write_file(repo / ".automation/ADAPTER", "python\n")
                elif case == "content":
                    target.write_bytes(target.read_bytes() + b"tampered\n")
                elif case == "mode":
                    target.chmod(0o755 if target.stat().st_mode & 0o111 == 0 else 0o644)
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.rebind_maintenance_provenance(repo, source, expected)
                self._assert_active_pair_bytes(repo, pair)

    def test_rebind_rejects_missing_or_mismatched_authority_without_reissuing_it(self) -> None:
        for case in ("missing", "mismatched"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo, source, expected = self._wrong_provenance_fixture(Path(directory))
                receipt_raw = upgrade.receipt_path(repo).read_bytes()
                authority = upgrade.authority_path(repo)
                if case == "missing":
                    authority.unlink()
                else:
                    authority.write_text("{}\n", encoding="utf-8")
                authority_raw = authority.read_bytes() if authority.exists() else None
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.rebind_maintenance_provenance(repo, source, expected)
                self.assertEqual(receipt_raw, upgrade.receipt_path(repo).read_bytes())
                self.assertEqual(authority_raw, authority.read_bytes() if authority.exists() else None)

    def test_rebind_rejects_expected_source_with_different_canonical_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._apply_fixture(root)
            pair = (upgrade.receipt_path(repo).read_bytes(), upgrade.authority_path(repo).read_bytes())
            self._write_file(root / "components/agent-core/opencode.json", '{"different":true}\n')
            self._git(["add", "components/agent-core/opencode.json"], root)
            self._git(["commit", "-m", "different expected Agent Core"], root)
            expected = self._expected_source_revision(root)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "expected provenance"):
                    upgrade.rebind_maintenance_provenance(repo, root, expected)
            self._assert_active_pair_bytes(repo, pair)

    def test_rebind_does_not_overwrite_authority_changed_at_publication_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source, expected = self._wrong_provenance_fixture(Path(directory))
            receipt_raw = upgrade.receipt_path(repo).read_bytes()
            authority = upgrade.authority_path(repo)
            concurrent = b'{"concurrent":true}\n'
            original = upgrade._replace_standard_pair

            def mutate_then_replace(*args, **kwargs):
                authority.write_bytes(concurrent)
                return original(*args, **kwargs)

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(upgrade, "_replace_standard_pair", side_effect=mutate_then_replace):
                with self.assertRaisesRegex(upgrade.UpgradeError, "provenance replacement failed"):
                    upgrade.rebind_maintenance_provenance(repo, source, expected)
            self.assertEqual(receipt_raw, upgrade.receipt_path(repo).read_bytes())
            self.assertEqual(concurrent, authority.read_bytes())

    def test_rebind_parent_directory_swap_is_dirfd_anchored_and_rolls_back(self) -> None:
        for kind in ("receipt", "authority"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, source, expected = self._wrong_provenance_fixture(root)
                receipt = upgrade.receipt_path(repo)
                authority = upgrade.authority_path(repo)
                old_receipt = receipt.read_bytes()
                old_authority = authority.read_bytes()
                parent = receipt.parent if kind == "receipt" else authority.parent
                moved = parent.with_name(parent.name + "-moved")
                attacker = root / f"attacker-{kind}"
                attacker.mkdir()
                marker = attacker / "marker"
                marker.write_text("untouched\n", encoding="utf-8")
                real_rename = os.rename
                calls = 0

                def swap_parent_once(src, dst, *, source_dir_fd, destination_dir_fd):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        real_rename(parent, moved)
                        parent.symlink_to(attacker, target_is_directory=True)
                    return real_rename(
                        src,
                        dst,
                        src_dir_fd=source_dir_fd,
                        dst_dir_fd=destination_dir_fd,
                    )

                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                        mock.patch.object(upgrade, "_rename_record_at", side_effect=swap_parent_once):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "post-publication validation"):
                        upgrade.rebind_maintenance_provenance(repo, source, expected)

                self.assertEqual("untouched\n", marker.read_text(encoding="utf-8"))
                self.assertEqual([marker], list(attacker.iterdir()))
                anchored_receipt = moved / receipt.name if kind == "receipt" else receipt
                anchored_authority = moved / authority.name if kind == "authority" else authority
                self.assertEqual(old_receipt, anchored_receipt.read_bytes())
                self.assertEqual(old_authority, anchored_authority.read_bytes())

    def test_rebind_write_failure_removes_private_temporary_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, source, expected = self._wrong_provenance_fixture(Path(directory))
            receipt = upgrade.receipt_path(repo)
            authority = upgrade.authority_path(repo)
            pair = (receipt.read_bytes(), authority.read_bytes())
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False), \
                    mock.patch.object(upgrade.os, "fsync", side_effect=OSError("injected fsync failure")):
                with self.assertRaisesRegex(OSError, "injected fsync failure"):
                    upgrade.rebind_maintenance_provenance(repo, source, expected)
            self._assert_active_pair_bytes(repo, pair)
            for parent in (receipt.parent, authority.parent):
                self.assertFalse(any(path.name.startswith((".automation-maintenance.json.replace-", ".authority.json.replace-"))
                                     for path in parent.iterdir()))

    def test_agent_knowledge_vault_19_source_bridge_rebinds_then_standard_commit_succeeds(self) -> None:
        fixture = self.AGENT_KNOWLEDGE_VAULT_19
        old_revision = fixture["receipt_source_revision"]
        expected_revision = fixture["expected_source_revision"]
        baseline_revision = fixture["authoritative_external_baseline"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "templates-bridge"
            bridge.mkdir()
            self._git(["init", "-b", "main"], bridge)
            self._git(["config", "user.name", "Test User"], bridge)
            self._git(["config", "user.email", "test@example.invalid"], bridge)
            source_pack = ROOT / "tests/fixtures/agent-knowledge-vault-19-templates-source.pack"
            self.assertEqual(
                fixture["templates_source_pack_sha256"],
                hashlib.sha256(source_pack.read_bytes()).hexdigest(),
            )
            subprocess.run(
                ["git", "index-pack", "--stdin"],
                cwd=bridge,
                input=source_pack.read_bytes(),
                capture_output=True,
                check=True,
            )
            self._git(["update-ref", "refs/heads/main", expected_revision], bridge)
            self._git(["reset", "--hard", expected_revision], bridge)
            self.assertEqual(old_revision, self._git(["rev-parse", f"{old_revision}^{{commit}}"], bridge))
            self.assertEqual(expected_revision, self._git(["rev-parse", f"{expected_revision}^{{commit}}"], bridge))
            self.assertEqual(
                0,
                subprocess.run(
                    ["git", "diff", "--quiet", old_revision, expected_revision, "--", "components/agent-core"],
                    cwd=bridge,
                    check=False,
                ).returncode,
                "the fixture-owned source commits must remain byte/tree-identical for Agent Core",
            )
            implementation = (
                "components/agent-core/.automation/bin/automation_upgrade.py",
                "components/agent-core/.automation/bin/git_private_state.py",
                "components/agent-core/.automation/bin/task_contract.py",
                "components/agent-core/.automation/bin/task_lifecycle.py",
                "tools/automation_recovery_bridge.py",
                "just/agent-core.just",
            )
            for relative in implementation:
                destination = bridge / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            self._git(["add", *implementation], bridge)
            self._git(["commit", "-m", "Issue 97 verified recovery implementation"], bridge)
            implementation_revision = self._git(["rev-parse", "HEAD"], bridge)
            self.assertNotEqual(expected_revision, implementation_revision)

            old_source = root / "templates-receipt-source"
            self._git(["worktree", "add", "--detach", str(old_source), old_revision], bridge)
            self.assertNotIn(
                "def rebind_maintenance_provenance(",
                (old_source / "components/agent-core/.automation/bin/automation_upgrade.py").read_text(encoding="utf-8"),
            )

            main = root / "agent-knowledge-vault-main"
            main.mkdir()
            self._git(["init", "-b", "main"], main)
            self._git(["config", "user.name", "Test User"], main)
            self._git(["config", "user.email", "test@example.invalid"], main)
            self._git(["remote", "add", "origin", "https://github.com/upiscium/AgentKnowledgeVault"], main)
            baseline_pack = ROOT / "tests/fixtures/agent-knowledge-vault-19-baseline.pack"
            self.assertEqual(
                fixture["baseline_pack_sha256"],
                hashlib.sha256(baseline_pack.read_bytes()).hexdigest(),
            )
            subprocess.run(
                ["git", "index-pack", "--stdin"],
                cwd=main,
                input=baseline_pack.read_bytes(),
                capture_output=True,
                check=True,
            )
            self._git(["update-ref", "refs/heads/main", baseline_revision], main)
            self._git(["reset", "--hard", baseline_revision], main)
            local_baseline = self._git(["rev-parse", "HEAD"], main)
            self.assertEqual(fixture["authoritative_external_baseline"], local_baseline)
            self._git(["update-ref", "refs/remotes/origin/main", local_baseline], main)
            self._git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], main)

            task = root / "agent-knowledge-vault-task-19"
            self._git(["worktree", "add", "-b", fixture["branch"], str(task), local_baseline], main)
            self._write_file(
                task / ".task-state/task.md",
                f"- Task ID: {fixture['task']}\n- Branch: {fixture['branch']}\n- Worktree: {task.resolve()}\n",
            )
            exclude = Path(self._git(["rev-parse", "--git-path", "info/exclude"], task))
            if not exclude.is_absolute():
                exclude = task / exclude
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("/.task-state/\n", encoding="utf-8")

            # Hydrate the canonical contract while the target is still pristine.
            # This deliberately loads the current source-side implementation,
            # rather than the old consumer copy that the upgrade will install.
            contract_spec = importlib.util.spec_from_file_location(
                "agent_knowledge_vault_19_source_contract",
                ROOT / "components/agent-core/.automation/bin/task_contract.py",
            )
            self.assertIsNotNone(contract_spec)
            assert contract_spec and contract_spec.loader
            contract = importlib.util.module_from_spec(contract_spec)
            contract_spec.loader.exec_module(contract)
            contract.lifecycle.initialize_state(
                task, fixture["task"], fixture["branch"], "main", local_baseline
            )
            issue_payload = {
                "number": 19,
                "url": "https://github.com/upiscium/AgentKnowledgeVault/issues/19",
                "title": "Resume the Agent Core upgrade",
                "body": "Resume this existing Task without resetting its Task State.",
                "state": "open",
                "repository": "upiscium/AgentKnowledgeVault",
                "labels": [],
                "assignees": [],
                "milestone": None,
            }
            hydrated = contract.hydrate_task_contract(
                task, fixture["task"], "19", issue_payload, "upiscium/AgentKnowledgeVault"
            )
            self.assertEqual(19, hydrated["issue"])
            self.assertTrue((task / ".task-state/issue.json").is_file())
            self.assertTrue((task / ".task-state/contract.json").is_file())

            # The installed v3.1.0 consumer intentionally lacks Issue #97's
            # expected-revision/rebind implementation and issues the stranded pair.
            applied = json.loads(
                self._cli(task, ["upgrade", "--source", str(old_source)]).stdout
            )
            self.assertEqual("APPLIED", applied["status"])
            receipt_before = self._receipt(task)
            self.assertEqual(old_revision, receipt_before["source_revision"])
            self.assertEqual(local_baseline, receipt_before["authority_head"])
            _, installed_legacy_authority = upgrade._authority_locations(task)
            self.assertIsNotNone(installed_legacy_authority)
            assert installed_legacy_authority is not None
            self.assertTrue(installed_legacy_authority.is_file())
            self.assertFalse(upgrade.consumed_receipt_path(task).exists())
            self.assertEqual(local_baseline, self._git(["rev-parse", "HEAD"], task))

            # Use the upgraded consumer's canonical lifecycle only after the
            # pending upgrade.
            self._lifecycle_cli(
                task,
                ["work-unit-register", fixture["task"], "WU-19-existing", "general", "Record existing terminal evidence"],
            )
            self._lifecycle_cli(
                task,
                ["work-unit-state-set", fixture["task"], "WU-19-existing", "completed", "Existing terminal Work Unit evidence"],
            )
            self._lifecycle_cli(task, ["state-set", fixture["task"], "blocked"])
            units_before_resume = json.loads(
                self._lifecycle_cli(task, ["work-unit-status", fixture["task"], "WU-19-existing"]).stdout
            )
            self.assertEqual("completed", units_before_resume["state"])
            self.assertEqual(
                "blocked",
                json.loads(self._lifecycle_cli(task, ["status", fixture["task"]]).stdout)["status"],
            )

            tracked_before = {
                path: ((task / Path(*path.split("/"))).read_bytes(),
                       (task / Path(*path.split("/"))).stat().st_mode & 0o777)
                for path in receipt_before["changed_paths"]
            }
            self.assertTrue(receipt_before["changed_paths"])
            self.assertTrue(self._git(["diff", "--name-only"], task).splitlines())
            status_before = self._git(["status", "--porcelain=v1"], task)
            rebound = json.loads(
                self._bridge_cli(
                    bridge,
                    "rebind-maintenance-provenance",
                    task,
                    expected_revision,
                ).stdout
            )
            self.assertEqual("PROVENANCE_REBOUND", rebound["status"])
            self.assertEqual(expected_revision, rebound["sourceRevision"])
            receipt_after = self._receipt(task)
            self.assertEqual(expected_revision, receipt_after["source_revision"])
            self.assertEqual(fixture["expected_source_revision"], receipt_after["source_revision"])
            self.assertEqual(receipt_before["changed_paths"], receipt_after["changed_paths"])
            self.assertEqual(tracked_before, {
                path: ((task / Path(*path.split("/"))).read_bytes(),
                       (task / Path(*path.split("/"))).stat().st_mode & 0o777)
                for path in receipt_after["changed_paths"]
            })
            self.assertEqual(status_before, self._git(["status", "--porcelain=v1"], task))
            subprocess.run(["git", "diff", "--check"], cwd=task, check=True)
            self.assertEqual("3", json.loads(self._cli(task, ["version"]).stdout)["version"])

            tracked_before_resume = {
                path: ((task / Path(*path.split("/"))).read_bytes(),
                       (task / Path(*path.split("/"))).stat().st_mode & 0o777)
                for path in receipt_after["changed_paths"]
            }
            status_before_resume = self._git(["status", "--porcelain=v1"], task)
            state_before_resume = {
                path.relative_to(task): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in (task / ".task-state").rglob("*")
                if path.is_file()
            }
            issue_api_payload = {
                "number": 19,
                "repository_url": "https://api.github.com/repos/upiscium/AgentKnowledgeVault",
                "html_url": "https://github.com/upiscium/AgentKnowledgeVault/issues/19",
                "title": issue_payload["title"],
                "body": issue_payload["body"],
                "state": "open",
                "labels": [],
                "assignees": [],
                "milestone": None,
            }

            def issue_runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(issue_api_payload), stderr=""
                )

            with mock.patch.object(bridge_bootstrap, "ROOT", bridge), \
                mock.patch.object(bridge_bootstrap, "trusted_gh_run", side_effect=issue_runner), \
                mock.patch.object(
                    sys,
                    "argv",
                    ["automation_recovery_bridge.py", "resume-contract-check", str(task), fixture["task"]],
                ), mock.patch("sys.stdout", new_callable=io.StringIO) as resume_output:
                self.assertEqual(0, bridge_bootstrap.main())
            resumed = json.loads(resume_output.getvalue())
            self.assertEqual("READY", resumed["status"])
            self.assertEqual("resume", resumed["mode"])
            self.assertEqual("blocked", resumed["taskStatus"])
            self.assertEqual(fixture["task"], resumed["task"])
            self.assertEqual(19, resumed["issue"])
            self.assertEqual("upiscium/AgentKnowledgeVault", resumed["repository"])
            self.assertRegex(resumed["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(implementation_revision, resumed["implementationRevision"])
            self.assertEqual(local_baseline, self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(tracked_before_resume, {
                path: ((task / Path(*path.split("/"))).read_bytes(),
                       (task / Path(*path.split("/"))).stat().st_mode & 0o777)
                for path in receipt_after["changed_paths"]
            })
            self.assertEqual(status_before_resume, self._git(["status", "--porcelain=v1"], task))
            self.assertEqual(state_before_resume, {
                path.relative_to(task): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in (task / ".task-state").rglob("*")
                if path.is_file()
            })
            self.assertEqual(units_before_resume, json.loads(
                self._lifecycle_cli(task, ["work-unit-status", fixture["task"], "WU-19-existing"]).stdout
            ))
            transitioned = json.loads(
                self._lifecycle_cli(task, ["state-set", fixture["task"], "verification-pending"]).stdout
            )
            self.assertEqual("verification-pending", transitioned["status"])
            self.assertEqual("completed", json.loads(
                self._lifecycle_cli(task, ["work-unit-status", fixture["task"], "WU-19-existing"]).stdout
            )["state"])
            self.assertEqual(local_baseline, self._git(["rev-parse", "HEAD"], task))

            authority_locations = upgrade._authority_locations(task)
            self.assertEqual(
                1,
                sum(path is not None and path.is_file() for path in authority_locations),
                authority_locations,
            )
            committed = json.loads(
                self._cli(task, ["commit", fixture["task"], "fix: upgrade Agent Core v3.1.2"]).stdout
            )
            self.assertEqual("COMMITTED", committed["status"])
            self.assertEqual(
                receipt_after["changed_paths"],
                sorted(self._git(["diff-tree", "--no-commit-id", "--name-only", "-r", committed["commit_sha"]], task).splitlines()),
            )
            consumed = json.loads(upgrade.consumed_receipt_path(task).read_text(encoding="utf-8"))
            self.assertEqual(committed["commit_sha"], consumed["commit_sha"])
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertEqual("", self._git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/task"], task))
            self.assertEqual(
                "",
                self._git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/task/19"], task),
            )

    def test_same_version_committed_compatible_drift_is_detected_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source = self._maintenance_fixture(root, destination_version=3)
            self._write_file(source / "opencode.json", '{"drift": true}\n')
            self._git(["add", "components/agent-core/opencode.json"], root)
            self._git(["commit", "-m", "compatible source drift"], root)
            plan = upgrade.check_update(repo, root)
            self.assertEqual(3, int(plan["currentVersion"]))
            self.assertEqual(3, int(plan["upstreamVersion"]))
            self.assertEqual("replace", self._plan_action(plan, "opencode.json")["action"])
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual('{"drift": true}\n', (repo / "opencode.json").read_text())

    def test_source_race_fails_closed_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self._maintenance_fixture(root)
            status_before = self._git(["status", "--porcelain"], repo)
            version_before = (repo / ".automation/VERSION").read_bytes()
            calls = 0

            def race(_source: Path, _revision: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise upgrade.UpgradeError("source changed during upgrade planning or mutation")

            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with mock.patch.object(upgrade, "revalidate_source", side_effect=race):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "source changed during"):
                        upgrade.apply(repo, root, self._expected_source_revision(root))
            self.assertEqual(2, calls)
            self.assertFalse(upgrade.receipt_path(repo).exists())
            self.assertFalse(upgrade.authority_path(repo).exists())
            self.assertEqual(status_before, self._git(["status", "--porcelain"], repo))
            self.assertEqual(version_before, (repo / ".automation/VERSION").read_bytes())

    def test_pending_paths_handles_nul_delimited_git_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._task_repo(Path(directory) / "repo")
            odd = repo / ".automation" / "pending\nname"
            self._write_file(odd, "pending\n")
            self.assertEqual([".automation/pending\nname"], upgrade.pending_paths(repo))

    def test_real_two_generation_cli_bootstrap_and_commit_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, source, old_result = self._old_process_fixture(Path(directory))
            self.assertEqual(old_result["release_head"], self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(sorted(old_result["changedPaths"]), upgrade.pending_paths(task))
            self.assertEqual("product\n", (Path(directory) / "outside-product").read_text())
            boot = json.loads(self._cli_bootstrap(task, source).stdout)
            self.assertEqual("RECEIPT_BOOTSTRAPPED", boot["status"])
            receipt = self._receipt(task)
            self.assertEqual(str(source.resolve()), receipt["source"])
            self.assertEqual(self._git(["rev-parse", "HEAD"], source), receipt["source_revision"])
            self.assertEqual(
                [old_result["release_version"], old_result["source_version"]],
                [receipt["current_version"], receipt["upstream_version"]],
            )
            self.assertEqual(sorted(old_result["changedPaths"]), receipt["changed_paths"])
            self.assertEqual(set(receipt["changed_paths"]), set(receipt["path_fingerprints"]))
            self.assertTrue(upgrade.authority_path(task).is_file())
            committed = json.loads(self._cli(task, ["commit", "TASK-78", "maintenance"]).stdout)
            self.assertEqual("COMMITTED", committed["status"])
            self.assertFalse(upgrade.receipt_path(task).exists())
            paths = sorted(self._git(["show", "--pretty=", "--name-only", "HEAD"], task).splitlines())
            self.assertEqual(receipt["changed_paths"], paths)
            self.assertFalse(any(path.startswith("just/project/") for path in paths))
            self.assertNotIn(".automation/ADAPTER", paths)
            self.assertNotIn("product-link", paths)

    def test_real_bridge_rejects_protected_paths_and_core_tampering(self) -> None:
        for pending_path in ("product.txt", ".automation/ADAPTER", "just/project/mod.just"):
            with self.subTest(pending_path=pending_path), tempfile.TemporaryDirectory() as directory:
                task, source, _ = self._old_process_fixture(Path(directory))
                self._write_file(task / pending_path, "unauthorized\n")
                result = self._cli_bootstrap(task, source, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending path", result.stderr)
                self.assertFalse(upgrade.receipt_path(task).exists())
                self.assertFalse(upgrade.authority_path(task).exists())
        with tempfile.TemporaryDirectory() as directory:
            task, source, old_result = self._old_process_fixture(Path(directory))
            path = old_result["changedPaths"][0]
            target = task / Path(*path.split("/"))
            target.write_bytes(target.read_bytes() + b"tampered\n")
            result = self._cli_bootstrap(task, source, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("reconstructed", result.stderr)
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())

        with tempfile.TemporaryDirectory() as directory:
            task, source, _ = self._old_process_fixture(Path(directory))
            self._write_file(source / "components/agent-core/.automation/dirty", "dirty\n")
            result = self._cli_bootstrap(task, source, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must be clean", result.stderr)
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())

    def test_real_bridge_rejects_no_change_without_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, source, _ = self._old_process_fixture(Path(directory))
            self._git(["add", "-A"], task)
            self._git(["commit", "-m", "simulate already published upgrade"], task)
            result = self._cli_bootstrap(task, source, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("non-empty pending paths", result.stderr)
            self.assertFalse(upgrade.authority_path(task).exists())

    def test_issue_85_real_bridge_recovers_and_commits_only_the_old_upgrade(self) -> None:
        repository_status = self._git(["status", "--porcelain=v1"], ROOT)
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            receipt_before = metadata["receipt"]
            receipt_bytes = upgrade.receipt_path(task).read_bytes()
            recovered = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", recovered["status"])
            self.assertEqual(metadata["source_revision"], recovered["receiptSourceRevision"])
            self.assertNotEqual(metadata["source_revision"], recovered["implementationRevision"])
            self.assertEqual(metadata["source_revision"], receipt_before["source_revision"])
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertEqual(metadata["old_script"], (task / ".automation/bin/automation_upgrade.py").read_bytes())
            for path, (content, mode) in metadata["receipt_bound"].items():
                target = task / Path(*path.split("/"))
                self.assertEqual(content, target.read_bytes(), path)
                self.assertEqual(mode, target.stat().st_mode & 0o777, path)
            proof = json.loads(upgrade.source_recovery_proof_path(task).read_text(encoding="utf-8"))
            authority = json.loads(upgrade.authority_path(task).read_text(encoding="utf-8"))
            self.assertEqual(1, proof["schema_version"])
            self.assertEqual("source-recovery-proof", proof["kind"])
            self.assertEqual(2, authority["schema_version"])
            self.assertEqual("source-recovery-bridge", authority["kind"])
            self.assertEqual(str(bridge.resolve()), proof["implementation_source"])
            self.assertEqual(metadata["bridge_head"], proof["implementation_revision"])

            old_head = self._git(["rev-parse", "HEAD"], task)
            committed = json.loads(self._bridge_cli(
                bridge, "commit-recovered-maintenance", task, "TASK-83", "source recovery"
            ).stdout)
            self.assertEqual("COMMITTED", committed["status"])
            self.assertTrue(committed["commitCreated"])
            self.assertFalse(committed["pushPerformed"])
            self.assertFalse(committed["mergePerformed"])
            self.assertEqual(receipt_before["changed_paths"], committed["changedPaths"])
            self.assertEqual(committed["commit_sha"], self._git(["rev-parse", "HEAD"], task))
            self.assertNotEqual(receipt_before["authority_head"], committed["commit_sha"])
            self.assertEqual(receipt_before["authority_head"], old_head)
            self.assertEqual(old_head, self._git(["show", "-s", "--format=%P", "HEAD"], task))
            self.assertEqual(receipt_before["changed_paths"], sorted(self._git(
                ["show", "--pretty=", "--name-only", "HEAD"], task
            ).splitlines()))
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())
            consumed = json.loads(upgrade.consumed_receipt_path(task).read_text(encoding="utf-8"))
            self.assertEqual("consumed", consumed["status"])
            self.assertEqual(committed["commit_sha"], consumed["commit_sha"])
            self.assertNotIn(".automation/ADAPTER", receipt_before["changed_paths"])
            self.assertFalse(any(path.startswith("just/project/") for path in receipt_before["changed_paths"]))
            self.assertNotIn(".task-state/task.md", receipt_before["changed_paths"])
            self.assertEqual(metadata["remote_refs"], self._git(
                ["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], task
            ))
        self.assertEqual(repository_status, self._git(["status", "--porcelain=v1"], ROOT))

    def test_issue_85_bridge_contract_and_rejection_matrix(self) -> None:
        root_justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
        self.assertIn("mod agent-core 'just/agent-core.just'", root_justfile)
        justfile = (ROOT / "just/agent-core.just").read_text(encoding="utf-8")
        self.assertIn("root := justfile_directory()", justfile)
        self.assertIn("tools/automation_recovery_bridge.py", justfile)
        self.assertIn("recover-maintenance-authority", justfile)
        self.assertIn("commit-recovered-maintenance", justfile)
        for forbidden in ("git push", "git merge", "apply"):
            self.assertNotIn(forbidden, justfile)
        if shutil.which("just") is None:
            self.skipTest("just is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target with spaces"
            for recipe, arguments in (
                ("agent-core::recover-maintenance-authority", [str(target)]),
                ("agent-core::commit-recovered-maintenance", [str(target), "TASK-83", "quoted message"]),
            ):
                result = subprocess.run(
                    ["just", "--dry-run", recipe, *arguments],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                output = result.stdout + result.stderr
                self.assertIn("python3 -I", output)
                self.assertIn(str((ROOT / "tools/automation_recovery_bridge.py").resolve()), output)
                self.assertIn(shlex.quote(str(target)), output)
                self.assertNotIn(".automation/bin/automation_upgrade.py", output)
                if recipe.endswith("commit-recovered-maintenance"):
                    self.assertIn(shlex.quote("TASK-83"), output)
                    self.assertIn(shlex.quote("quoted message"), output)

        for kind in ("missing", "unrelated", "unknown-revision", "unclean-bridge"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                bridge, source, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
                if kind == "missing":
                    missing = Path(directory) / "missing-source"
                    receipt = metadata["receipt"] | {"source": str(missing.resolve())}
                    upgrade.atomic_json_write(upgrade.receipt_path(task), receipt)
                elif kind == "unrelated":
                    unrelated = Path(directory) / "unrelated"
                    self._task_repo(unrelated)
                    receipt = metadata["receipt"] | {"source": str(unrelated.resolve())}
                    upgrade.atomic_json_write(upgrade.receipt_path(task), receipt)
                elif kind == "unknown-revision":
                    receipt = metadata["receipt"] | {"source_revision": "0" * 40}
                    upgrade.atomic_json_write(upgrade.receipt_path(task), receipt)
                else:
                    self._write_file(bridge / "dirty-bridge", "dirty\n")
                result = self._bridge_cli(bridge, "recover-maintenance-authority", task, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(upgrade.authority_path(task).exists())

    def test_issue_87_dirty_live_engine_is_rejected_before_engine_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(root)
            marker = root / "live-engine-executed"
            engine = bridge / "components/agent-core/.automation/bin/automation_upgrade.py"
            with engine.open("a", encoding="utf-8") as stream:
                stream.write(f"\nPath({str(marker)!r}).write_text('executed')\n")
            receipt_bytes = upgrade.receipt_path(task).read_bytes()
            branch = self._git(["rev-parse", "HEAD"], task)
            authority = upgrade.authority_path(task)
            proof = upgrade.source_recovery_proof_path(task)
            result = self._bridge_cli(bridge, "recover-maintenance-authority", task, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertEqual(branch, self._git(["rev-parse", "HEAD"], task))
            self.assertFalse(authority.exists())
            self.assertFalse(proof.exists())
            self.assertEqual(metadata["bridge_head"], self._git(["rev-parse", "HEAD"], bridge))

    def test_issue_87_dirty_tracked_bridge_file_is_rejected_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
            self._write_file(
                bridge / "tools/automation_recovery_bridge.py",
                (bridge / "tools/automation_recovery_bridge.py").read_text(encoding="utf-8")
                + "\n# harmless tracked checkout modification\n",
            )
            receipt_bytes = upgrade.receipt_path(task).read_bytes()
            head = self._git(["rev-parse", "HEAD"], task)
            result = self._bridge_cli(bridge, "recover-maintenance-authority", task, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertEqual(head, self._git(["rev-parse", "HEAD"], task))
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

    def test_issue_87_isolated_bridge_ignores_shadow_modules_and_rejects_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _, task, _ = self._source_recovery_bridge_fixture(root)
            marker = root / "shadow-module-executed"
            payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
            self._write_file(bridge / "json.py", payload)
            self._write_file(bridge / "pathlib.py", payload)
            result = self._bridge_cli(bridge, "recover-maintenance-authority", task, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

    def test_issue_87_isolated_bridge_ignores_external_pythonpath_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _, task, _ = self._source_recovery_bridge_fixture(root)
            fake_root = root / "fake-pythonpath"
            marker = root / "external-module-executed"
            self._write_file(
                fake_root / "json.py",
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
            )
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(fake_root)}, clear=False):
                recovered = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", recovered["status"])
            self.assertFalse(marker.exists())

    def test_issue_87_bridge_disables_local_and_ambient_git_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _, task, _ = self._source_recovery_bridge_fixture(root)
            fsmonitor = root / "fsmonitor-marker.sh"
            marker = root / "fsmonitor-executed"
            fsmonitor.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
            fsmonitor.chmod(0o755)
            self._git(["config", "--local", "core.fsmonitor", str(fsmonitor)], bridge)

            fake_home = root / "home"
            fake_xdg = root / "xdg"
            fake_home.mkdir()
            (fake_xdg / "git").mkdir(parents=True)
            (fake_xdg / "git" / "config").write_text(
                f"[core]\n\tfsmonitor = {fsmonitor}\n", encoding="utf-8"
            )
            environment = {
                "HOME": str(fake_home),
                "XDG_CONFIG_HOME": str(fake_xdg),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                recovered = json.loads(self._bridge_cli(
                    bridge, "recover-maintenance-authority", task
                ).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", recovered["status"])
            self.assertFalse(marker.exists())

    def test_issue_87_subprocess_error_output_is_bounded_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
            bridge_status = self._git(["status", "--porcelain=v1"], bridge)
            receipt = upgrade.receipt_path(task)
            authority = upgrade.authority_path(task)
            proof = upgrade.source_recovery_proof_path(task)
            target_records = {
                path: path.read_bytes() if path.exists() else None
                for path in (receipt, authority, proof)
            }
            target_head = self._git(["rev-parse", "HEAD"], task)
            oversized_subcommand = ("!\n\x01" * 1200) + "!"
            result = self._bridge_cli(
                bridge,
                oversized_subcommand,
                task,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertTrue(result.stderr.startswith("ERROR:"), result.stderr[:80])
            self.assertNotIn("Traceback", result.stderr)
            self.assertLessEqual(len(result.stderr), 1610)
            self.assertTrue(result.stderr.endswith("\n"))
            self.assertTrue(all(" " <= character <= "~" for character in result.stderr[:-1]))
            self.assertEqual(bridge_status, self._git(["status", "--porcelain=v1"], bridge))
            self.assertEqual(target_head, self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(target_records, {
                path: path.read_bytes() if path.exists() else None
                for path in target_records
            })

    def test_issue_87_wrong_binding_and_source_resolution_race_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            receipt_bytes = upgrade.receipt_path(task).read_bytes()
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "expected implementation revision"):
                    upgrade.recover_maintenance_authority_from_source(
                        task, bridge, expected_implementation_revision="0" * 40
                    )
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

            self._bridge_cli(bridge, "recover-maintenance-authority", task)
            record_bytes = {
                path: path.read_bytes()
                for path in (
                    upgrade.receipt_path(task),
                    upgrade.authority_path(task),
                    upgrade.source_recovery_proof_path(task),
                )
            }
            branch = self._git(["rev-parse", "HEAD"], task)
            with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                with self.assertRaisesRegex(upgrade.UpgradeError, "expected implementation revision"):
                    upgrade.commit_recovered_maintenance(
                        task, bridge, "TASK-83", "wrong binding",
                        expected_implementation_revision="0" * 40,
                    )
            self.assertEqual(branch, self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(record_bytes, {
                path: path.read_bytes() for path in record_bytes
            })
            upgrade.authority_path(task).unlink()
            upgrade.source_recovery_proof_path(task).unlink()

            calls = 0
            original_resolve = upgrade.resolve_clean_source_worktree

            def race(path: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise upgrade.UpgradeError("injected source resolution race")
                return original_resolve(path)

            with mock.patch.object(upgrade, "resolve_clean_source_worktree", side_effect=race):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "source resolution race"):
                        upgrade.recover_maintenance_authority_from_source(
                            task, bridge, expected_implementation_revision=metadata["bridge_head"]
                        )
            self.assertEqual(2, calls)
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

    def test_issue_85_bridge_rolls_back_source_recovery_publication_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, source, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            original = upgrade._write_authority_at
            calls = 0

            def fail_once(path: Path, value: dict, *, admin: Path | None = None) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise upgrade.UpgradeError("injected bridge publication failure")
                original(path, value, admin=admin)

            with mock.patch.object(upgrade, "_write_authority_at", side_effect=fail_once):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "injected bridge publication failure"):
                        upgrade.recover_maintenance_authority_from_source(
                            task, bridge, expected_implementation_revision=metadata["bridge_head"]
                        )
            self.assertTrue(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())
            retry = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", retry["status"])

    def test_issue_85_recovery_rejects_authority_appearing_during_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            with mock.patch.object(upgrade, "authority_exists", return_value=True):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "changed before source-recovery bridge publication"):
                        upgrade.recover_maintenance_authority_from_source(
                            task, bridge, expected_implementation_revision=metadata["bridge_head"]
                        )
            self.assertTrue(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

    def test_issue_85_proof_only_interruption_retries_without_rewriting_receipt_or_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            first = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", first["status"])
            receipt_bytes = upgrade.receipt_path(task).read_bytes()
            proof_bytes = upgrade.source_recovery_proof_path(task).read_bytes()
            authority = upgrade.authority_path(task)
            authority_record = json.loads(authority.read_text(encoding="utf-8"))
            self.assertEqual(2, authority_record["schema_version"])
            authority.unlink()
            retry = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", retry["status"])
            self.assertEqual(receipt_bytes, upgrade.receipt_path(task).read_bytes())
            self.assertEqual(proof_bytes, upgrade.source_recovery_proof_path(task).read_bytes())
            self.assertTrue(upgrade.authority_path(task).is_file())
            committed = json.loads(self._bridge_cli(
                bridge, "commit-recovered-maintenance", task, "TASK-83", "proof retry"
            ).stdout)
            self.assertEqual("COMMITTED", committed["status"])

    def test_issue_85_cli_rejects_unregistered_outside_and_malformed_targets(self) -> None:
        for kind in ("unregistered", "outside", "malformed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
                target = task
                if kind == "unregistered":
                    state = task / ".task-state/task.md"
                    state.write_text(state.read_text(encoding="utf-8").replace(
                        "task/TASK-83-linked", "task/TASK-83-unregistered"
                    ), encoding="utf-8")
                elif kind == "outside":
                    target = Path(directory) / "outside-target"
                    self._task_repo(target, task="TASK-OUTSIDE")
                else:
                    upgrade.receipt_path(task).write_text("not-json\n", encoding="utf-8")
                result = self._bridge_cli(bridge, "recover-maintenance-authority", target, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("ERROR:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(upgrade.source_recovery_proof_path(task).exists())
                self.assertFalse(upgrade.authority_path(task).exists())
                self.assertFalse(upgrade.source_recovery_proof_path(target).exists())
                self.assertFalse(upgrade.authority_path(target).exists())

    def test_issue_85_tampered_proof_cannot_commit_or_change_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
            self._bridge_cli(bridge, "recover-maintenance-authority", task)
            active = upgrade.receipt_path(task)
            authority = upgrade.authority_path(task)
            proof = upgrade.source_recovery_proof_path(task)
            active_bytes = active.read_bytes()
            authority_bytes = authority.read_bytes()
            record = json.loads(proof.read_text(encoding="utf-8"))
            record["proof_sha256"] = "0" * 64
            proof.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            head = self._git(["rev-parse", "HEAD"], task)
            refs = self._git(["for-each-ref", "--format=%(refname) %(objectname)"], task)
            result = self._bridge_cli(
                bridge, "commit-recovered-maintenance", task, "TASK-83", "tampered proof", check=False
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(active_bytes, active.read_bytes())
            self.assertEqual(authority_bytes, authority.read_bytes())
            self.assertEqual(head, self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(refs, self._git(["for-each-ref", "--format=%(refname) %(objectname)"], task))

    def test_issue_85_commit_cli_rejects_malformed_and_invalid_utf8_active_receipts(self) -> None:
        for payload in (b"not-json\n", b"\xff\xfe\n"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
                self._bridge_cli(bridge, "recover-maintenance-authority", task)
                upgrade.receipt_path(task).write_bytes(payload)
                upgrade.source_recovery_proof_path(task).unlink()
                upgrade.authority_path(task).unlink()
                result = self._bridge_cli(
                    bridge, "commit-recovered-maintenance", task, "TASK-83", "malformed receipt", check=False
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("ERROR:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(upgrade.source_recovery_proof_path(task).exists())
                self.assertFalse(upgrade.authority_path(task).exists())

    def test_issue_85_published_commit_finalization_failure_is_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
            self._bridge_cli(bridge, "recover-maintenance-authority", task)
            old_head = self._git(["rev-parse", "HEAD"], task)
            remote_refs = self._git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], task)
            original_run = upgrade.run

            def fail_final_reset(command, *args, **kwargs):
                if len(command) >= 3 and command[:3] == ["git", "reset", "-q"]:
                    raise OSError("injected final reset failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(upgrade, "run", side_effect=fail_final_reset):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(
                        upgrade.UpgradeError,
                        r"commit [0-9a-f]{40,64} was published but finalization failed",
                    ):
                        upgrade.commit_recovered_maintenance(
                            task, bridge, "TASK-83", "published failure",
                            expected_implementation_revision=self._git(["rev-parse", "HEAD"], bridge),
                        )
            consumed = json.loads(upgrade.consumed_receipt_path(task).read_text(encoding="utf-8"))
            commit_sha = consumed["commit_sha"]
            self.assertEqual("consumed", consumed["status"])
            self.assertEqual(commit_sha, self._git(["rev-parse", "HEAD"], task))
            self.assertEqual(commit_sha, self._git(["rev-parse", "refs/heads/task/TASK-83-linked"], task))
            self.assertEqual(old_head, self._git(["show", "-s", "--format=%P", "HEAD"], task))
            self.assertEqual("1", self._git(["rev-list", "--count", f"{old_head}..{commit_sha}"], task))
            self.assertNotEqual(old_head, commit_sha)
            self.assertFalse(upgrade.receipt_path(task).exists())
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())
            self.assertEqual(remote_refs, self._git(
                ["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], task
            ))

    def test_issue_85_source_commit_failure_restores_bridge_records_and_cli_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, _ = self._source_recovery_bridge_fixture(Path(directory))
            recovered = json.loads(self._bridge_cli(bridge, "recover-maintenance-authority", task).stdout)
            self.assertEqual("AUTHORITY_RECOVERED", recovered["status"])
            active = upgrade.receipt_path(task)
            authority = upgrade.authority_path(task)
            proof = upgrade.source_recovery_proof_path(task)
            active_bytes, authority_bytes, proof_bytes = active.read_bytes(), authority.read_bytes(), proof.read_bytes()
            original_run = upgrade.run

            def fail_staging_check(command, *args, **kwargs):
                if command == ["git", "diff", "--no-ext-diff", "--cached", "--check"]:
                    raise upgrade.UpgradeError("injected source commit staging failure")
                return original_run(command, *args, **kwargs)

            with mock.patch.object(upgrade, "run", side_effect=fail_staging_check):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "injected source commit staging failure"):
                        upgrade.commit_recovered_maintenance(
                            task, bridge, "TASK-83", "injected failure",
                            expected_implementation_revision=self._git(["rev-parse", "HEAD"], bridge),
                        )
            self.assertEqual(active_bytes, active.read_bytes())
            self.assertEqual(authority_bytes, authority.read_bytes())
            self.assertEqual(proof_bytes, proof.read_bytes())
            self.assertFalse(upgrade.consumed_receipt_path(task).exists())
            retry = json.loads(self._bridge_cli(
                bridge, "commit-recovered-maintenance", task, "TASK-83", "retry"
            ).stdout)
            self.assertEqual("COMMITTED", retry["status"])

    def test_issue_85_receipt_change_and_target_rejections_leave_no_bridge_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
            active = upgrade.receipt_path(task)
            original_read_bytes = Path.read_bytes
            reads = 0

            def changing_receipt(path: Path):
                nonlocal reads
                value = original_read_bytes(path)
                if path == active:
                    reads += 1
                    if reads == 2:
                        return value + b"changed during recovery"
                return value

            with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=changing_receipt):
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaisesRegex(upgrade.UpgradeError, "receipt or target changed"):
                        upgrade.recover_maintenance_authority_from_source(
                            task, bridge, expected_implementation_revision=metadata["bridge_head"]
                        )
            self.assertFalse(upgrade.authority_path(task).exists())
            self.assertFalse(upgrade.source_recovery_proof_path(task).exists())

        for kind in ("version", "fingerprint", "head", "pending", "content", "mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                bridge, _, task, metadata = self._source_recovery_bridge_fixture(Path(directory))
                receipt = metadata["receipt"]
                path = receipt["changed_paths"][0]
                target = task / Path(*path.split("/"))
                if kind == "version":
                    upgrade.atomic_json_write(active := upgrade.receipt_path(task), receipt | {"current_version": "999"})
                elif kind == "fingerprint":
                    fingerprints = dict(receipt["path_fingerprints"])
                    fingerprints[path] = dict(fingerprints[path], content_sha256="0" * 64)
                    upgrade.atomic_json_write(upgrade.receipt_path(task), receipt | {"path_fingerprints": fingerprints})
                elif kind == "head":
                    self._write_file(task / "HEAD-tamper", "head\n")
                    self._git(["add", "HEAD-tamper"], task)
                    self._git(["commit", "-m", "head tamper"], task)
                elif kind == "pending":
                    self._write_file(task / ".automation/pending-extra", "pending\n")
                elif kind == "content":
                    target.write_bytes(target.read_bytes() + b"tampered")
                else:
                    target.chmod(target.stat().st_mode ^ 0o100)
                with mock.patch.dict(os.environ, {"AUTOMATION_MAINTENANCE": "1"}, clear=False):
                    with self.assertRaises(upgrade.UpgradeError):
                        upgrade.recover_maintenance_authority_from_source(
                            task, bridge, expected_implementation_revision=metadata["bridge_head"]
                        )
                self.assertFalse(upgrade.authority_path(task).exists())
                self.assertFalse(upgrade.source_recovery_proof_path(task).exists())


if __name__ == "__main__":
    unittest.main()
