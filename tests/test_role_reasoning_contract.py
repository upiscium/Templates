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
ROLE_POLICY = {
    "build": ("openai/gpt-6.1-sol", "high"),
    "plan": ("openai/gpt-6.1-sol", "high"),
    "task-orchestrator": ("openai/gpt-6.1-sol", "high"),
    "maintenance-orchestrator": ("openai/gpt-6.1-sol", "high"),
    "architect": ("openai/gpt-6.1-sol", "high"),
    "security-reviewer": ("openai/gpt-6.1-sol", "high"),
    "reviewer": ("openai/gpt-6-luna", "high"),
    "investigator": ("openai/gpt-6-luna", "high"),
    "general": ("openai/gpt-6-luna", "medium"),
    "explore": ("openai/gpt-6-luna", "medium"),
    "scout": ("openai/gpt-6-luna", "medium"),
    "verifier": ("openai/gpt-6-luna", "medium"),
}


def frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    return match.group(1)


def metadata_value(metadata: str, key: str) -> str:
    values = re.findall(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", metadata)
    if len(values) != 1:
        raise AssertionError(f"expected exactly one {key} field; found {len(values)}")
    return values[0]


class RoleReasoningContractTest(unittest.TestCase):
    def test_source_role_set_matches_exact_policy(self) -> None:
        source_roles = {path.stem for path in CORE_AGENTS.glob("*.md")}
        self.assertEqual(source_roles, set(ROLE_POLICY))

    def test_each_role_has_exact_model(self) -> None:
        for role, (model, _) in ROLE_POLICY.items():
            with self.subTest(role=role):
                metadata = frontmatter(CORE_AGENTS / f"{role}.md")
                self.assertEqual(metadata_value(metadata, "model"), model)

    def test_each_role_has_exact_reasoning_effort(self) -> None:
        for role, (_, effort) in ROLE_POLICY.items():
            with self.subTest(role=role):
                metadata = frontmatter(CORE_AGENTS / f"{role}.md")
                self.assertEqual(metadata_value(metadata, "reasoningEffort"), effort)

    def test_generated_templates_match_all_role_sources_byte_for_byte(self) -> None:
        for template in TEMPLATES:
            generated_agents = ROOT / "templates" / template / ".opencode" / "agents"
            for role in sorted(ROLE_POLICY):
                source = CORE_AGENTS / f"{role}.md"
                generated = generated_agents / f"{role}.md"
                self.assertEqual(source.read_bytes(), generated.read_bytes(), f"{template}:{role}")


if __name__ == "__main__":
    unittest.main()
