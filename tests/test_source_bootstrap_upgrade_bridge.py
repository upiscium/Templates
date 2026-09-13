from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_MANIFEST = ROOT / "tests/fixtures/terreate_pre81_v2_cpp_cmake.json"
FIXTURE_IDENTITY = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
SOURCE_REVISION = FIXTURE_IDENTITY["agent_core_source"]
REMOVED = (
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

PROTECTED_REPOSITORY_PATHS = ("just/project/", "REPOSITORY.SENTINEL")
PROTECTED_PRODUCT_PATHS = (
    "PRODUCT.SENTINEL", "src/", "include/", "CMakeLists.txt", "CMakePresets.json",
    "README.md", ".clang-format", ".clang-tidy",
)
PROTECTED_ADAPTER_PATHS = (
    ".automation/ADAPTER",
    ".automation/INIT.fragment.md",
    ".automation/adoption.toml",
)


class BootstrapUpgradeBridgeTest(unittest.TestCase):
    def git(self, cwd: Path, *args: str, check: bool = True) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        }
        result = subprocess.run(("git", *args), cwd=cwd, text=True,
                                capture_output=True, check=False, env=environment)
        if check and result.returncode:
            self.fail(result.stderr or result.stdout or f"git exited {result.returncode}")
        return result.stdout.strip()

    def write(self, path: Path, data: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())

    def materialize(self, repo: Path, revision: str, prefix: str, destination: Path) -> None:
        records = self.git(repo, "ls-tree", "-r", "-z", revision, "--", prefix)
        raw_records = records.encode("utf-8", "surrogateescape").split(b"\0")
        prefix_bytes = (prefix.rstrip("/") + "/").encode()
        for record in raw_records:
            if not record:
                continue
            header, relative = record.split(b"\t", 1)
            mode, kind, _ = header.split()
            self.assertEqual((b"100644", b"blob"), (mode, kind))
            relative = relative[len(prefix_bytes):].decode()
            self.write(destination / relative,
                       subprocess.check_output(("git", "show", f"{revision}:{prefix}/{relative}"), cwd=repo))

    def configure(self, repo: Path) -> None:
        self.git(repo, "config", "user.name", "Bootstrap Upgrade Tests")
        self.git(repo, "config", "user.email", "bootstrap@example.invalid")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap-upgrade-test-")
        self.top = Path(self.temp.name)
        self.candidate = self.top / "templates-candidate"
        self.git(ROOT, "clone", "--shared", "--no-checkout", str(ROOT), str(self.candidate))
        self.git(self.candidate, "switch", "--detach", "HEAD")
        self.configure(self.candidate)
        # The candidate is a distinct immutable revision of the current implementation.
        for relative in (
            "tools/automation_recovery_bridge.py",
            "components/agent-core/.automation/bin/automation_upgrade.py",
            "components/agent-core/.automation/bin/git_private_state.py",
            "components/agent-core/.automation/bin/task_lifecycle.py",
            "components/agent-core/.automation/bin/task_contract.py",
            "components/agent-core/.automation/bin/publication_metadata.py",
            "components/agent-core/.automation/bin/agent_core.py",
            "components/agent-core/.automation/bin/maintenance_lifecycle.py",
        ):
            shutil.copy2(ROOT / relative, self.candidate / relative)
        self.git(self.candidate, "add", "tools/automation_recovery_bridge.py",
                 "components/agent-core/.automation/bin")
        self.git(
            self.candidate,
            "commit",
            "--allow-empty",
            "-m",
            "trusted bootstrap implementation",
        )
        self.implementation_revision = self.git(self.candidate, "rev-parse", "HEAD")
        self.assertEqual(SOURCE_REVISION, self.git(self.candidate, "rev-parse", SOURCE_REVISION))

        self.main = self.top / "consumer-main"
        self.main.mkdir()
        self.materialize(self.candidate, SOURCE_REVISION, "components/agent-core", self.main)
        # The adapter is overlaid onto the synthetic consumer, as template
        # distribution does; it is not retained under the Templates source
        # directory hierarchy.
        adapter = self.main
        self.materialize(self.candidate, SOURCE_REVISION, "components/adapters/cpp-cmake", adapter)
        self.write(self.main / ".automation/VERSION", "2\n")
        self.write(self.main / "PRODUCT.SENTINEL", "product-owned\n")
        self.write(self.main / "REPOSITORY.SENTINEL", "repository-owned\n")
        self.assertEqual("2\n", (self.main / ".automation/VERSION").read_text())
        self.assertEqual("root := justfile_directory() / \"..\" / \"..\"\n",
                         (self.main / ".automation/just/automation.just").read_text().splitlines(True)[0])
        self.git(self.main, "init", "-b", "main")
        self.configure(self.main)
        self.git(self.main, "remote", "add", "origin", FIXTURE_IDENTITY["repository_remote"])
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-m", "synthetic consumer")
        self.main_revision = self.git(self.main, "rev-parse", "HEAD")
        self.git(self.main, "update-ref", "refs/remotes/origin/main", self.main_revision)
        self.git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.task = self.top / "consumer-task"
        self.git(self.main, "worktree", "add", "-b", "fix/226-bootstrap-upgrade",
                 str(self.task), self.main_revision)
        self.exclude = Path(self.git(self.task, "rev-parse", "--git-path", "info/exclude"))
        if not self.exclude.is_absolute():
            self.exclude = self.task / self.exclude
        self.exclude.parent.mkdir(parents=True, exist_ok=True)
        self.exclude.write_text("/.task-state/\n", encoding="utf-8")
        self.write(self.task / ".task-state/task.md",
                   f"# 226\n\n## Identity\n\n- Task ID: 226\n"
                   f"- Branch: fix/226-bootstrap-upgrade\n- Worktree: {self.task.resolve()}\n"
                   f"- Base branch: main\n- Base revision: {self.main_revision}\n\n"
                   "## Purpose\n\nBootstrap the historical Agent Core maintenance upgrade.\n\n"
                   "## Scope\n\n- Upgrade Agent Core v2 to v3 only.\n\n"
                   "## Current state\n\n- Status: initialized\n- Blockers: none\n- Unverified: Task contract\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_bridge(self, expected: str | None = None, check: bool = False):
        expected = self.implementation_revision if expected is None else expected
        env = {**os.environ, "AUTOMATION_MAINTENANCE": "1"}
        return subprocess.run(
            ("python3", "tools/automation_recovery_bridge.py", "bootstrap-upgrade",
             str(self.task), expected), cwd=self.candidate, env=env,
             text=True, capture_output=True, check=check)

    def fail_text(self, result) -> str:
        return result.stdout + result.stderr

    def identity_snapshot(self) -> dict[str, str]:
        return {
            "head": self.git(self.task, "rev-parse", "HEAD"),
            "branch": self.git(self.task, "branch", "--show-current"),
            "main": self.git(self.main, "rev-parse", "HEAD"),
            "main_ref": self.git(self.main, "rev-parse", "refs/heads/main"),
            "task_ref": self.git(self.task, "rev-parse", "refs/heads/fix/226-bootstrap-upgrade"),
            "origin_main": self.git(self.main, "rev-parse", "refs/remotes/origin/main"),
            "origin_head": self.git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD"),
        }

    def authority_path(self) -> Path:
        admin = Path(self.git(self.task, "rev-parse", "--absolute-git-dir"))
        return admin / "agent-core/automation-maintenance/authority.json"

    def protected_files(self) -> dict[str, bytes]:
        """Capture the complete adapter/repository/product surface of the fixture."""
        result: dict[str, bytes] = {}
        for path in self.task.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.is_symlink():
                continue
            relative = path.relative_to(self.task).as_posix()
            if (
                relative in PROTECTED_ADAPTER_PATHS
                or any(relative == prefix or relative.startswith(prefix) for prefix in PROTECTED_REPOSITORY_PATHS)
                or any(relative == prefix or relative.startswith(prefix) for prefix in PROTECTED_PRODUCT_PATHS)
            ):
                result[relative] = path.read_bytes()
        return result

    def test_success_uses_source_snapshot_and_preserves_adapter_product_and_repository(self) -> None:
        protected = self.protected_files()
        # The historical source contains the pre-#81 router.  Successful completion
        # without running `just` proves this bridge uses the Python engine directly.
        self.assertTrue((self.task / ".automation/just/automation.just").is_file())
        result = self.run_bridge()
        self.assertEqual(0, result.returncode, self.fail_text(result))
        output = json.loads(result.stdout)
        self.assertEqual("BOOTSTRAP_UPGRADED", output["status"])
        self.assertEqual(self.implementation_revision, output["implementationRevision"])
        self.assertEqual("3\n", (self.task / ".automation/VERSION").read_text())
        receipt = json.loads((self.task / ".task-state/automation-maintenance.json").read_text())
        self.assertEqual({
            "schema_version": 1, "status": "active", "task_id": "226",
            "branch": "fix/226-bootstrap-upgrade", "worktree": str(self.task.resolve()),
            "source": str(self.candidate.resolve()), "source_revision": self.implementation_revision,
            "current_version": "2", "upstream_version": "3",
            "changed_paths": sorted(json.loads(result.stdout)["changedPaths"]),
            "authority_head": self.main_revision,
        }, {key: receipt[key] for key in (
            "schema_version", "status", "task_id", "branch", "worktree", "source",
            "source_revision", "current_version", "upstream_version", "changed_paths", "authority_head",
        )})
        self.assertRegex(receipt["authority_nonce"], r"^[0-9a-f]{64}$")
        authority = json.loads(self.authority_path().read_text())
        self.assertEqual({
            "schema_version": 1, "task_id": "226", "branch": receipt["branch"],
            "worktree": receipt["worktree"], "authority_nonce": receipt["authority_nonce"],
            "receipt_sha256": hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }, authority)
        self.assertEqual([], [p for p in REMOVED if (self.task / p).exists()])
        self.assertEqual(self.identity_snapshot(), {
            "head": self.main_revision, "branch": "fix/226-bootstrap-upgrade",
            "main": self.main_revision, "main_ref": self.main_revision, "task_ref": self.main_revision,
            "origin_main": self.main_revision, "origin_head": "refs/remotes/origin/main",
        })
        self.assertEqual(protected, {path: (self.task / path).read_bytes() for path in protected})

    def test_historical_fixture_identity_is_explicit_and_exact(self) -> None:
        self.assertEqual(
            {
                "repository": "upiscium/Terreate",
                "historical_main": "0d022ed5140b497b5d3be886408822ecbd981dd3",
                "agent_core_source": SOURCE_REVISION,
                "agent_core_version": "2",
                "adapter": "cpp-cmake",
                "maintenance_task": "226",
                "repository_remote": "https://github.com/upiscium/Terreate.git",
                "broken_router_first_line": 'root := justfile_directory() / ".." / ".."',
            },
            FIXTURE_IDENTITY,
        )
        router = (self.task / ".automation/just/automation.just").read_text()
        self.assertEqual(
            FIXTURE_IDENTITY["broken_router_first_line"],
            router.splitlines()[0],
        )
        self.assertEqual(
            FIXTURE_IDENTITY["repository_remote"],
            self.git(self.task, "remote", "get-url", "origin"),
        )
        self.assertEqual(
            FIXTURE_IDENTITY["agent_core_version"],
            (self.task / ".automation/VERSION").read_text().strip(),
        )
        self.assertEqual(
            FIXTURE_IDENTITY["adapter"],
            (self.task / ".automation/ADAPTER").read_text().strip(),
        )

    def test_missing_canonical_transition_is_rejected_before_version_change(self) -> None:
        migration = self.candidate / "components/agent-core/.automation/migrations.toml"
        self.write(migration, "schema_version = 1\n")
        self.git(self.candidate, "add", str(migration.relative_to(self.candidate)))
        self.git(self.candidate, "commit", "-m", "incomplete migration candidate")
        self.implementation_revision = self.git(self.candidate, "rev-parse", "HEAD")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("exactly one canonical 2 -> 3 migration", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_public_recipe_uses_trusted_source_side_launcher(self) -> None:
        recipe = (ROOT / "just/agent-core.just").read_text()
        self.assertIn("bootstrap-upgrade target expected_source_revision:", recipe)
        self.assertIn("unset LD_PRELOAD LD_LIBRARY_PATH", recipe)
        self.assertIn('"$resolved_python" -I {{quote(tool)}} bootstrap-upgrade', recipe)

    def test_all_canonical_v2_to_v3_removals_are_deleted(self) -> None:
        result = self.run_bridge()
        self.assertEqual(0, result.returncode, self.fail_text(result))
        self.assertEqual(sorted(REMOVED), sorted(json.loads(result.stdout)["migrationRemovals"]))
        self.assertTrue(all(not (self.task / p).exists() for p in REMOVED))

    def test_preflight_and_dry_run_failures_leave_version_two(self) -> None:
        self.write(self.task / ".task-state/recovery.json", "progress\n")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no lifecycle progress", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())
        (self.task / ".task-state/recovery.json").unlink()
        self.write(self.main / ".automation/bin/unexpected-managed.py", "unexpected\n")
        self.git(self.main, "add", ".automation/bin/unexpected-managed.py")
        self.git(self.main, "commit", "-m", "unexpected managed path")
        changed_base = self.git(self.main, "rev-parse", "HEAD")
        self.git(self.task, "merge", "--ff-only", "main")
        state = (self.task / ".task-state/task.md").read_text()
        (self.task / ".task-state/task.md").write_text(
            state.replace(f"- Base revision: {self.main_revision}", f"- Base revision: {changed_base}")
        )
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unexpected Agent Core paths", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_ignored_untracked_unexpected_managed_path_is_rejected(self) -> None:
        self.exclude.write_text("/.task-state/\n/.automation/bin/unexpected-managed.py\n", encoding="utf-8")
        self.write(self.task / ".automation/bin/unexpected-managed.py", "unexpected\n")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unexpected Agent Core paths", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_unsafe_managed_symlink_is_rejected_as_ambiguous_ownership(self) -> None:
        outside = self.top / "outside-managed"
        self.write(outside, "outside\n")
        relative = ".automation/bin/unexpected-managed.py"
        self.exclude.write_text(f"/.task-state/\n/{relative}\n", encoding="utf-8")
        (self.task / relative).symlink_to(outside)
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unexpected Agent Core paths", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_wrong_valid_full_source_revision_and_dirty_source_are_rejected(self) -> None:
        wrong = self.git(self.candidate, "rev-parse", SOURCE_REVISION)
        self.assertRegex(wrong, r"^[0-9a-f]{40}$")
        result = self.run_bridge(wrong)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Templates HEAD", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

        self.write(self.candidate / "source-dirty.txt", "dirty source\n")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Templates source worktree must be clean", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_expected_revision_is_full_exact_and_immutable(self) -> None:
        for expected in (
            self.implementation_revision[:12], SOURCE_REVISION, "0" * 40, "f" * 64, "HEAD"
        ):
            result = self.run_bridge(expected)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())
        self.assertIn(
            "exactly 40 lowercase hexadecimal",
            self.fail_text(self.run_bridge(self.implementation_revision[:12])),
        )

    def test_dirty_progressed_and_current_v3_targets_are_rejected(self) -> None:
        self.write(self.task / "dirty.txt", "dirty\n")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no unignored pending files", self.fail_text(result))
        (self.task / "dirty.txt").unlink()
        self.write(self.task / ".task-state/work-units.json", "{}\n")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no lifecycle progress", self.fail_text(result))
        (self.task / ".task-state/work-units.json").unlink()
        (self.main / ".automation/VERSION").write_text("3\n")
        self.git(self.main, "add", ".automation/VERSION")
        self.git(self.main, "commit", "-m", "current v3 consumer")
        current = self.git(self.main, "rev-parse", "HEAD")
        self.git(self.task, "merge", "--ff-only", "main")
        state = (self.task / ".task-state/task.md").read_text()
        (self.task / ".task-state/task.md").write_text(
            state.replace(f"- Base revision: {self.main_revision}", f"- Base revision: {current}")
        )
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("VERSION 2", self.fail_text(result))
        self.assertEqual("3\n", (self.task / ".automation/VERSION").read_text())

    def test_target_head_branch_and_worktree_identity_drift_is_rejected(self) -> None:
        self.git(self.task, "switch", "-c", "fix/226-other")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("exact registered Task worktree", self.fail_text(result))

    def test_target_head_drift_is_rejected_before_any_upgrade(self) -> None:
        self.write(self.task / "head-drift.txt", "drift\n")
        self.git(self.task, "add", "head-drift.txt")
        self.git(self.task, "commit", "-m", "target head drift")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("pristine recorded main base", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_target_worktree_identity_drift_is_rejected_before_any_upgrade(self) -> None:
        state = (self.task / ".task-state/task.md").read_text()
        (self.task / ".task-state/task.md").write_text(
            state.replace(f"- Worktree: {self.task.resolve()}", "- Worktree: /tmp/not-this-worktree")
        )
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Task State worktree or Task identity is not exact", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_target_main_ref_drift_is_rejected_before_any_upgrade(self) -> None:
        self.write(self.main / "main-drift.txt", "drift\n")
        self.git(self.main, "add", "main-drift.txt")
        self.git(self.main, "commit", "-m", "main drift")
        result = self.run_bridge()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Base revision differs from registered main", self.fail_text(result))
        self.assertEqual("2\n", (self.task / ".automation/VERSION").read_text())

    def test_terminal_retry_has_exact_diagnostic(self) -> None:
        first = self.run_bridge()
        self.assertEqual(0, first.returncode, self.fail_text(first))
        second = self.run_bridge()
        self.assertNotEqual(0, second.returncode)
        self.assertEqual(
            "ERROR: bootstrap-upgrade is terminal and has already been applied; "
            "continue through the canonical automation maintenance lifecycle\n",
            second.stderr,
        )


if __name__ == "__main__":
    unittest.main()
