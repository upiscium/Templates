from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "agent-base"


def run(
    *args: str,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check, env=env)


class WorktreeLifecycleSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        temporary_root = Path(self.temporary.name)
        remote = temporary_root / "origin.git"
        run("git", "init", "--bare", "--initial-branch=main", str(remote), cwd=temporary_root)
        self.repo = temporary_root / "repo"
        shutil.copytree(TEMPLATE, self.repo)
        run("git", "init", "-b", "main", cwd=self.repo)
        run("git", "config", "user.email", "smoke@example.invalid", cwd=self.repo)
        run("git", "config", "user.name", "Smoke", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-m", "initial", cwd=self.repo)
        run("git", "remote", "add", "origin", str(remote), cwd=self.repo)
        run("git", "push", "-u", "origin", "main", cwd=self.repo)
        run("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        self.script = self.repo / ".automation" / "bin" / "task_lifecycle.py"

        fake_bin = temporary_root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "if [ \"$1 $2\" = \"repo view\" ]; then\n"
            "  printf '%s\\n' '{\"nameWithOwner\":\"acme/widgets\"}'\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"api\" ]; then\n"
            "  printf '%s\\n' '[[]]'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = f"{fake_bin}:{self.env['PATH']}"

    def start_task(self, task: str = "TASK-1", slug: str = "smoke") -> Path:
        started = run(
            "python3", str(self.script), "start", task, slug,
            cwd=self.repo, env=self.env,
        )
        self.assertIn('"status": "initialized"', started.stdout)
        worktree = self.repo / ".worktrees" / f"{task}-{slug}"
        state = worktree / ".task-state/task.md"
        resolved = state.read_text(encoding="utf-8")
        resolved = resolved.replace("## Purpose\n\nTBD", "## Purpose\n\nOffline lifecycle fixture")
        resolved = resolved.replace("## Scope\n\n- TBD", "## Scope\n\n- Lifecycle smoke coverage")
        resolved = resolved.replace(
            "- [ ] Define Task-specific acceptance criteria",
            "- [ ] Complete the bounded lifecycle smoke scenario",
        )
        resolved = resolved.replace("- Unverified: Task contract", "- Unverified: lifecycle checks")
        state.write_text(resolved, encoding="utf-8")
        return worktree

    def test_start_work_units_duplicate_and_cleanup(self) -> None:
        worktree = self.start_task()
        state = worktree / ".task-state" / "task.md"
        self.assertTrue(state.is_file())
        self.assertNotIn(".task-state", run("git", "status", "--porcelain", cwd=worktree).stdout)

        duplicate_task = run(
            "python3", str(self.script), "start", "TASK-1", "smoke",
            cwd=self.repo, check=False, env=self.env,
        )
        self.assertNotEqual(duplicate_task.returncode, 0)

        objective = "Implement the bounded fixed-model fixture"
        registered = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-1", "general", objective,
            cwd=worktree, env=self.env,
        )
        unit = json.loads(registered.stdout)
        self.assertEqual("in-flight", unit["state"])
        self.assertEqual(objective, unit["objective"])
        self.assertEqual(hashlib.sha256(objective.encode()).hexdigest(), unit["semantic_sha256"])

        persisted = json.loads((worktree / ".task-state" / "work-units.json").read_text(encoding="utf-8"))
        self.assertEqual("TASK-1", persisted["task_id"])
        self.assertEqual(unit, persisted["units"]["WU-1"])
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("## Work Units", state_text)
        self.assertNotIn("## Work Units\n\nNone yet.", state_text)
        self.assertIn("work_unit_registered=WU-1", state_text)
        self.assertIn(f"semantic_sha256={unit['semantic_sha256']}", state_text)

        duplicate_unit = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-1", "general", objective,
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(duplicate_unit.returncode, 0)
        self.assertIn("Work Unit already exists", duplicate_unit.stderr)

        status = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-1",
            cwd=worktree, env=self.env,
        )
        self.assertEqual(unit, json.loads(status.stdout))

        completed = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "completed",
            "Leaf returned COMPLETED after implementing the bounded fixture",
            cwd=worktree, env=self.env,
        )
        completed_unit = json.loads(completed.stdout)
        self.assertEqual("completed", completed_unit["state"])
        self.assertEqual(
            "Leaf returned COMPLETED after implementing the bounded fixture",
            completed_unit["transitions"][0]["evidence"],
        )
        state_text = state.read_text(encoding="utf-8")
        self.assertIn("work_unit_state=WU-1; previous=in-flight; state=completed", state_text)
        self.assertIn("evidence_sha256=", state_text)

        reopen = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-1", "in-flight",
            "Attempt to reopen a terminal unit",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(reopen.returncode, 0)
        self.assertIn("invalid Work Unit transition", reopen.stderr)

        unknown = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-UNKNOWN",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown Work Unit", unknown.stderr)

        blocked = run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-2", "verifier",
            "Run the bounded fixed-model verification", cwd=worktree, env=self.env,
        )
        self.assertEqual("in-flight", json.loads(blocked.stdout)["state"])
        blocked = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-2", "blocked",
            "Configured verifier model was unavailable",
            "--provider", "openai", "--model", "gpt-6-luna", "--error", "model unavailable",
            cwd=worktree, env=self.env,
        )
        failure = json.loads(blocked.stdout)["transitions"][0]["provider_failure"]
        self.assertEqual(
            {"provider": "openai", "model": "gpt-6-luna", "error": "model unavailable"},
            failure,
        )

        run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-3", "verifier",
            "Reject mismatched provider model evidence", cwd=worktree, env=self.env,
        )
        work_units_path = worktree / ".task-state" / "work-units.json"
        state_before_rejections = state.read_text(encoding="utf-8")
        work_units_before_rejections = work_units_path.read_text(encoding="utf-8")
        mismatched = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-3", "blocked",
            "Reported model did not match the configured verifier model",
            "--provider", "openai", "--model", "gpt-5.6-terra", "--error", "model unavailable",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn("does not match configured Work Unit role", mismatched.stderr)
        self.assertEqual(work_units_before_rejections, work_units_path.read_text(encoding="utf-8"))
        self.assertEqual(state_before_rejections, state.read_text(encoding="utf-8"))
        unchanged = json.loads(work_units_path.read_text(encoding="utf-8"))["units"]["WU-3"]
        self.assertEqual("in-flight", unchanged["state"])
        self.assertEqual([], unchanged["transitions"])

        partial = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-3", "blocked",
            "Partial provider failure evidence", "--provider", "openai",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(partial.returncode, 0)
        self.assertIn("requires provider, model, and error together", partial.stderr)
        self.assertEqual(work_units_before_rejections, work_units_path.read_text(encoding="utf-8"))
        self.assertEqual(state_before_rejections, state.read_text(encoding="utf-8"))

        run(
            "python3", str(self.script), "work-unit-register", "TASK-1", "WU-4", "general",
            "Reject contradictory completion evidence", cwd=worktree, env=self.env,
        )
        contradictory = run(
            "python3", str(self.script), "work-unit-state-set", "TASK-1", "WU-4", "completed",
            "Contradictory provider failure",
            "--provider", "openai", "--model", "gpt-6-luna", "--error", "model unavailable",
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(contradictory.returncode, 0)
        self.assertIn("only valid for a blocked Work Unit", contradictory.stderr)
        unchanged = run(
            "python3", str(self.script), "work-unit-status", "TASK-1", "WU-4",
            cwd=worktree, env=self.env,
        )
        self.assertEqual("in-flight", json.loads(unchanged.stdout)["state"])

        run("python3", str(self.script), "state-set", "TASK-1", "cancelled", cwd=worktree, env=self.env)
        cleanup = run("python3", str(self.script), "cleanup", "TASK-1", cwd=self.repo, env=self.env)
        self.assertIn('"taskStateDiscarded": true', cleanup.stdout)
        self.assertFalse(worktree.exists())

    def test_auto_allocation_dispatch_and_concurrent_creation(self) -> None:
        worktree = self.start_task("TASK-2", "autopilot")

        next_result = run(
            "python3", str(self.script), "work-unit-next", "TASK-2",
            cwd=worktree, env=self.env,
        )
        self.assertEqual("WU-TASK-2-01", json.loads(next_result.stdout)["next_work_unit"])

        objective = "Implement the bounded Autopilot fixture"
        created = run(
            "python3", str(self.script), "work-unit-create", "TASK-2", "general", objective,
            cwd=worktree, env=self.env,
        )
        unit = json.loads(created.stdout)
        self.assertEqual("WU-TASK-2-01", unit["id"])
        self.assertEqual("in-flight", unit["state"])

        ready = run(
            "python3", str(self.script), "work-unit-dispatch-check", "TASK-2",
            unit["id"], "general", objective, cwd=worktree, env=self.env,
        )
        dispatch = json.loads(ready.stdout)
        self.assertEqual("READY", dispatch["status"])
        self.assertEqual("openai/gpt-6-luna", dispatch["configured_model"])
        self.assertEqual(unit["semantic_sha256"], dispatch["semantic_sha256"])

        for role, delegated_objective, expected in (
            ("verifier", objective, "dispatch role mismatch"),
            ("general", objective + " changed", "dispatch objective mismatch"),
        ):
            rejected = run(
                "python3", str(self.script), "work-unit-dispatch-check", "TASK-2",
                unit["id"], role, delegated_objective,
                cwd=worktree, check=False, env=self.env,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn(expected, rejected.stderr)

        run(
            "python3", str(self.script), "work-unit-state-set", "TASK-2", unit["id"],
            "completed", "Implementation leaf returned exactly status: COMPLETED",
            cwd=worktree, env=self.env,
        )
        terminal_dispatch = run(
            "python3", str(self.script), "work-unit-dispatch-check", "TASK-2",
            unit["id"], "general", objective,
            cwd=worktree, check=False, env=self.env,
        )
        self.assertNotEqual(0, terminal_dispatch.returncode)
        self.assertIn("is not dispatchable", terminal_dispatch.stderr)

        # Compatibility registrations with legacy IDs do not disturb canonical allocation.
        run(
            "python3", str(self.script), "work-unit-register", "TASK-2", "legacy-review",
            "reviewer", "Review legacy registration compatibility",
            cwd=worktree, env=self.env,
        )
        run(
            "python3", str(self.script), "work-unit-register", "TASK-2", "WU-TASK-2-07",
            "reviewer", "Reserve a later canonical allocation",
            cwd=worktree, env=self.env,
        )
        next_result = run(
            "python3", str(self.script), "work-unit-next", "TASK-2",
            cwd=worktree, env=self.env,
        )
        self.assertEqual("WU-TASK-2-08", json.loads(next_result.stdout)["next_work_unit"])

        processes = [
            subprocess.Popen(
                (
                    "python3", str(self.script), "work-unit-create", "TASK-2", "general",
                    f"Concurrent bounded objective {index}",
                ),
                cwd=worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
            )
            for index in range(12)
        ]
        results = [process.communicate() for process in processes]
        for process, (_, stderr) in zip(processes, results, strict=True):
            self.assertEqual(0, process.returncode, stderr)
        allocated = {json.loads(stdout)["id"] for stdout, _ in results}
        self.assertEqual({f"WU-TASK-2-{index:02d}" for index in range(8, 20)}, allocated)

        persisted = json.loads(
            (worktree / ".task-state" / "work-units.json").read_text(encoding="utf-8")
        )
        self.assertTrue(allocated <= set(persisted["units"]))
        self.assertEqual(15, len(persisted["units"]))

        for terminal_state in ("needs-approval", "needs-decision"):
            terminal = run(
                "python3", str(self.script), "work-unit-create", "TASK-2", "general",
                f"Exercise terminal state {terminal_state}", cwd=worktree, env=self.env,
            )
            terminal_unit = json.loads(terminal.stdout)
            run(
                "python3", str(self.script), "work-unit-state-set", "TASK-2",
                terminal_unit["id"], terminal_state,
                f"Leaf returned canonical {terminal_state} evidence",
                cwd=worktree, env=self.env,
            )
            reopen = run(
                "python3", str(self.script), "work-unit-state-set", "TASK-2",
                terminal_unit["id"], "in-flight", "Attempted terminal reuse",
                cwd=worktree, check=False, env=self.env,
            )
            self.assertNotEqual(0, reopen.returncode)
            self.assertIn("invalid Work Unit transition", reopen.stderr)

    def test_oversized_auto_allocated_id_fails_without_persistence(self) -> None:
        task = "T" * 124
        worktree = self.start_task(task, "oversized")
        work_units_path = worktree / ".task-state" / "work-units.json"

        for command in (
            ("work-unit-next", task),
            ("work-unit-create", task, "general", "Must not persist an invalid ID"),
        ):
            rejected = run(
                "python3", str(self.script), *command,
                cwd=worktree, check=False, env=self.env,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("generated Work Unit ID is invalid", rejected.stderr)
            self.assertFalse(work_units_path.exists())

    def test_synthetic_corrective_autopilot_lifecycle(self) -> None:
        worktree = self.start_task("TASK-3", "corrective")

        def create(role: str, objective: str) -> dict:
            result = run(
                "python3", str(self.script), "work-unit-create", "TASK-3", role, objective,
                cwd=worktree, env=self.env,
            )
            return json.loads(result.stdout)

        def finish(unit: dict, status: str, evidence: str) -> None:
            run(
                "python3", str(self.script), "work-unit-state-set", "TASK-3", unit["id"],
                status, evidence, cwd=worktree, env=self.env,
            )

        implementation = create("general", "Implement the synthetic feature")
        finish(implementation, "completed", "status: COMPLETED; feature implemented")
        verifier = create("verifier", "Run focused tests for the synthetic feature")
        finish(verifier, "blocked", "status: BLOCKED; focused regression test is missing")
        corrective = create("general", "Add the missing focused regression test")
        self.assertNotEqual(implementation["id"], corrective["id"])
        finish(corrective, "completed", "status: COMPLETED; focused regression test added")
        final_verifier = create("verifier", "Re-run focused tests and project checks")
        finish(final_verifier, "completed", "status: COMPLETED; focused and project checks PASS")

        for state in (
            "planning", "implementing", "verification-pending", "local-verified",
            "review-pending", "publication-ready",
        ):
            run(
                "python3", str(self.script), "state-set", "TASK-3", state,
                cwd=worktree, env=self.env,
            )
        status = run(
            "python3", str(self.script), "status", "TASK-3", cwd=worktree, env=self.env,
        )
        self.assertEqual("publication-ready", json.loads(status.stdout)["status"])

        persisted = json.loads(
            (worktree / ".task-state" / "work-units.json").read_text(encoding="utf-8")
        )["units"]
        self.assertEqual("completed", persisted[implementation["id"]]["state"])
        self.assertEqual("blocked", persisted[verifier["id"]]["state"])
        self.assertEqual("completed", persisted[corrective["id"]]["state"])
        self.assertEqual("completed", persisted[final_verifier["id"]]["state"])


if __name__ == "__main__":
    unittest.main()
