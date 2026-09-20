from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_AGENTS = ROOT / "components" / "agent-core" / ".opencode" / "agents"
TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
    "agent-typescript-node",
)
LUNA_AGENTS = {
    "plan",
    "task-orchestrator",
    "general",
    "explore",
    "verifier",
    "reviewer",
    "investigator",
    "scout",
}
RETAINED_MODELS = {
    "build": "openai/gpt-5.6-sol",
    "architect": "openai/gpt-5.6-sol",
    "maintenance-orchestrator": "openai/gpt-5.6-sol",
    "security-reviewer": "openai/gpt-5.6-terra",
}


def frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    return match.group(1)


class LunaReasoningContractTest(unittest.TestCase):
    def test_luna_agents_use_max_reasoning(self) -> None:
        for role in sorted(LUNA_AGENTS):
            metadata = frontmatter(CORE_AGENTS / f"{role}.md")
            self.assertIn("model: openai/gpt-5.6-luna", metadata, role)
            self.assertIn("reasoningEffort: max", metadata, role)

    def test_retained_high_tier_models_are_explicit(self) -> None:
        for role, model in RETAINED_MODELS.items():
            metadata = frontmatter(CORE_AGENTS / f"{role}.md")
            self.assertIn(f"model: {model}", metadata, role)
            self.assertNotIn("reasoningEffort: max", metadata, role)

    def test_non_luna_agents_do_not_force_max_reasoning(self) -> None:
        for path in CORE_AGENTS.glob("*.md"):
            if path.stem in LUNA_AGENTS:
                continue
            self.assertNotIn("reasoningEffort: max", frontmatter(path), path.name)

    def test_generated_templates_match_luna_agent_sources(self) -> None:
        for template in TEMPLATES:
            generated_agents = ROOT / "templates" / template / ".opencode" / "agents"
            for role in sorted(LUNA_AGENTS):
                source = CORE_AGENTS / f"{role}.md"
                generated = generated_agents / f"{role}.md"
                self.assertEqual(source.read_bytes(), generated.read_bytes(), f"{template}:{role}")


if __name__ == "__main__":
    unittest.main()
