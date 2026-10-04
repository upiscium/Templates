from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
    "agent-typescript-node",
)


class AgentCoreOpenCodeSelfContainmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        cls.lock = json.loads((ROOT / "flake.lock").read_text(encoding="utf-8"))

    def test_root_has_no_external_opencode_policy_dependency(self) -> None:
        self.assertNotIn("opencodeContract", self.flake)
        self.assertNotIn("OpencodeContract", self.flake)
        self.assertNotIn("opencodeContract", self.lock["nodes"]["root"]["inputs"])
        self.assertNotIn("opencodeContract", self.lock["nodes"])

    def test_generated_template_flakes_are_policy_self_contained(self) -> None:
        for template in TEMPLATES:
            with self.subTest(template=template):
                text = (ROOT / "templates" / template / "flake.nix").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("opencodeContract", text)
                self.assertNotIn("OpencodeContract", text)
                self.assertNotIn("github:upiscium/dotnix", text)

    def test_agent_core_version_and_upstream_are_templates_owned(self) -> None:
        core = ROOT / "components" / "agent-core" / ".automation"
        self.assertEqual("3\n", (core / "VERSION").read_text(encoding="utf-8"))
        self.assertEqual(
            'repository = "github:upiscium/Templates"\n'
            'ref = "main"\n'
            'component = "components/agent-core"\n',
            (core / "UPSTREAM").read_text(encoding="utf-8"),
        )

    def test_ci_has_no_external_opencode_policy_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("OpencodeContract", workflow)
        self.assertNotIn("opencode-contract", workflow)
        self.assertNotIn("github:upiscium/dotnix", workflow)


if __name__ == "__main__":
    unittest.main()
