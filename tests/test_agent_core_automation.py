from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "components"
    / "agent-core"
    / ".automation"
    / "bin"
    / "agent_core.py"
)
spec = importlib.util.spec_from_file_location("agent_core", MODULE_PATH)
assert spec and spec.loader
agent_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_core)


class AgentCoreSafetyTest(unittest.TestCase):
    def test_automation_core_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".automation").mkdir()
            (root / ".automation" / "policy.toml").write_text(
                '[paths]\nautomation_core = ["Justfile", ".automation/**"]\nsecret_patterns = []\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(agent_core.AutomationError, "Automation Core"):
                agent_core.reject_unsafe_paths(root, ["Justfile"])

    def test_task_state_is_never_committable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".automation").mkdir()
            (root / ".automation" / "policy.toml").write_text(
                '[paths]\nautomation_core = []\nsecret_patterns = []\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(agent_core.AutomationError, "task-state"):
                agent_core.reject_unsafe_paths(root, [".task-state/task.md"])

    @mock.patch.object(agent_core, "default_branch", return_value="main")
    @mock.patch.object(agent_core, "current_branch", return_value="main")
    def test_default_branch_cannot_be_used_as_task_branch(self, _current, _default) -> None:
        with self.assertRaisesRegex(agent_core.AutomationError, "not the Task branch"):
            agent_core.ensure_task_branch(Path("."), "TASK-1")

    @mock.patch.object(agent_core, "default_branch", return_value="main")
    @mock.patch.object(agent_core, "current_branch", return_value="task/TASK-1-example")
    def test_task_branch_is_accepted(self, _current, _default) -> None:
        branch = agent_core.ensure_task_branch(Path("."), "TASK-1")
        self.assertEqual(branch, "task/TASK-1-example")

    def test_integration_merge_rejects_head_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "checkpoint"
            checkpoint.write_text("old-head\n", encoding="utf-8")
            with (
                mock.patch.object(agent_core, "integration_checkpoint", return_value=checkpoint),
                mock.patch.object(
                    agent_core,
                    "validate_integration",
                    return_value={"headRefOid": "new-head", "number": 10},
                ),
                self.assertRaisesRegex(agent_core.AutomationError, "head moved"),
            ):
                agent_core.integrate_merge(root, "10")


if __name__ == "__main__":
    unittest.main()
