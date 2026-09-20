from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import re
import unittest
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core"
AGENTS = CORE / ".opencode" / "agents"

LEAF_PRIMARY_AGENTS = (
    "general",
    "explore",
    "verifier",
    "reviewer",
    "investigator",
    "security-reviewer",
    "scout",
    "architect",
)
TASK_ORCHESTRATOR_LEAVES = (
    "general",
    "explore",
    "verifier",
    "reviewer",
    "investigator",
    "security-reviewer",
    "scout",
)
LEAF_STATUS_SET = {"COMPLETED", "BLOCKED", "NEEDS_APPROVAL", "NEEDS_DECISION"}
LEAF_STATUS_FIELDS = tuple(f"status: {status}" for status in (
    "COMPLETED",
    "BLOCKED",
    "NEEDS_APPROVAL",
    "NEEDS_DECISION",
))
LEAF_CONTRACT_TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
    "agent-typescript-node",
)
READ_ONLY_GIT_COMMANDS = {
    "git status",
    "git status *",
    "git diff",
    "git diff *",
    "git log",
    "git log *",
    "git show *",
    "git blame *",
    "git grep *",
    "git rev-parse *",
    "git ls-files *",
    "git merge-base *",
    "git cat-file *",
    "git branch --list *",
    "git remote -v",
    "git worktree list *",
}
PROJECT_CHECK_COMMANDS = {
    "just project::doctor",
    "just project::eval",
    "just project::format-check",
    "just project::lint",
    "just project::test",
    "just project::build",
    "just project::check",
}
LEAF_ALLOWED_BASH = {
    "general": READ_ONLY_GIT_COMMANDS | PROJECT_CHECK_COMMANDS,
    "explore": READ_ONLY_GIT_COMMANDS,
    "verifier": {"git status", "git status *", "git diff", "git diff *"}
    | PROJECT_CHECK_COMMANDS,
    "reviewer": set(),
    "investigator": READ_ONLY_GIT_COMMANDS | PROJECT_CHECK_COMMANDS,
    "security-reviewer": READ_ONLY_GIT_COMMANDS,
    "scout": set(),
    "architect": READ_ONLY_GIT_COMMANDS,
}


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1]) and value[0] in {'"', "'"}):
        return value[1:-1]
    return value


def _partition_mapping_line(line: str) -> tuple[str, str] | None:
    match = re.match(r'^(?:"([^"]+)"|\'([^\']+)\'|([^:]+)):\s*(.*)$', line)
    if match is None:
        return None
    key = next(group for group in match.groups()[:3] if group is not None)
    return key.strip(), match.group(4).strip()


def _parse_yamlish_mapping(
    lines: list[str], start: int, base_indent: int
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue

        indent = _line_indent(raw)
        if indent < base_indent:
            break
        if indent > base_indent:
            i += 1
            continue

        stripped = raw.strip()
        entry = _partition_mapping_line(stripped)
        if entry is None:
            i += 1
            continue

        key, value = entry

        if value:
            mapping[key] = _unquote(value)
            i += 1
            continue

        # Nested map; advance to next line and parse any deeper lines regardless of spacing.
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines) or _line_indent(lines[i]) <= base_indent:
            mapping[key] = {}
            continue

        child_indent = _line_indent(lines[i])
        nested, i = _parse_yamlish_mapping(lines, i, child_indent)
        mapping[key] = nested

    return mapping, i


def parse_permission_block(frontmatter_text: str) -> dict[str, Any]:
    lines = frontmatter_text.splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^permission:\s*$", line.strip()):
            base_indent = _line_indent(line) + 2
            parsed, _ = _parse_yamlish_mapping(lines, idx + 1, base_indent)
            return parsed
    raise AssertionError("missing permission: frontmatter block")


def _iter_permission_values(permission_value: Any) -> Iterable[str]:
    if isinstance(permission_value, dict):
        for value in permission_value.values():
            yield from _iter_permission_values(value)
    else:
        if isinstance(permission_value, str):
            yield permission_value


def permission_entries_are_scalar_lines(permission: dict[str, Any]) -> bool:
    return all(isinstance(value, (str, dict)) for value in permission.values())


def permission_for(agent: str) -> dict[str, Any]:
    return parse_permission_block(frontmatter(AGENTS / f"{agent}.md"))


def body_text(agent: str) -> str:
    text = (AGENTS / f"{agent}.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n.*?\n---\n(.*)", text, flags=re.S)
    if not match:
        raise AssertionError(f"missing frontmatter body: {agent}")
    return match.group(1)


def status_report_lines(text: str) -> set[str]:
    return set(re.findall(r"\b(?:COMPLETED|BLOCKED|NEEDS_APPROVAL|NEEDS_DECISION)\b", text))


def exact_status_fields(text: str) -> list[str]:
    return re.findall(r"`(status: (?:COMPLETED|BLOCKED|NEEDS_APPROVAL|NEEDS_DECISION))`", text)


def assert_prompt_contract_for_leaf_statuses(test_case: unittest.TestCase, body: str, name: str) -> None:
    test_case.assertEqual(status_report_lines(body), LEAF_STATUS_SET, name)
    lower = body.lower()
    test_case.assertIn("non-interactive", lower, name)
    test_case.assertIn("start the final response with exactly one `status:", lower, name)
    test_case.assertIn("do not attempt denied operations", lower, name)
    test_case.assertIn("ask the user", lower, name)
    test_case.assertIn("call `question`", lower, name)
    test_case.assertIn("claim any unexecuted command as executed/passed", lower, name)
    for field in (
        "denied_operation",
        "why_needed",
        "supporting_evidence",
        "expected_effect",
        "consequence_if_denied",
        "work_unit_state",
        "safe_continuation_point",
        "safe_alternatives",
    ):
        test_case.assertIn(field, lower, name)
    test_case.assertIn("ambiguity, options with tradeoffs, and recommendation", lower, name)


def assert_leaf_startup_contract(test_case: unittest.TestCase, body: str, name: str) -> None:
    lower = body.lower()
    test_case.assertIn("initialize", lower, name)
    test_case.assertRegex(lower, r"(?:no|not|without|do not|don't)[^\n.]{0,80}initialize", name)
    test_case.assertIn(
        "parent task orchestrator already initialized the task worktree and validated the task contract",
        lower,
        name,
    )
    test_case.assertIn("bounded", lower, name)
    test_case.assertIn("objective", lower, name)
    test_case.assertIn("project::", lower, name)
    test_case.assertIn("as leaf-session startup prerequisites", lower, name)
    for command in ("just agent::doctor", "just agent::context", "just project::doctor"):
        test_case.assertIn(command, lower, name)
    test_case.assertIn(
        "do not run `just agent::doctor`, `just agent::context`, or mandatory "
        "`just project::doctor` as leaf-session startup prerequisites",
        lower,
        name,
    )


def assert_orchestrator_status_contract(test_case: unittest.TestCase, body: str) -> None:
    lower = body.lower()
    for field in LEAF_STATUS_FIELDS:
        test_case.assertIn(field.lower(), lower)
    test_case.assertEqual(set(exact_status_fields(body)), set(LEAF_STATUS_FIELDS))
    for phrase in (
        "unknown, multiple, or missing status field",
        "invalid evidence",
    ):
        test_case.assertIn(phrase, lower)
    invalid_lines = [line for line in lower.splitlines() if "invalid evidence" in line]
    test_case.assertTrue(
        any(
            "only" in line
            and all(term in line for term in ("unknown", "multiple", "missing"))
            for line in invalid_lines
        ),
        "invalid evidence must be limited to unknown, multiple, or missing status fields",
    )
    for source, target in (
        ("COMPLETED", "completed"),
        ("BLOCKED", "blocked"),
        ("NEEDS_APPROVAL", "needs-approval"),
        ("NEEDS_DECISION", "needs-decision"),
    ):
        test_case.assertRegex(
            lower,
            rf"status:\s*{source.lower()}[^\n]*(?:->|→|maps? to|means|becomes)[^\n]*{re.escape(target)}",
            source,
        )


def frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    return match.group(1)


class OpenCodeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((CORE / "opencode.json").read_text(encoding="utf-8"))

    def test_depth_and_default_agent(self) -> None:
        self.assertEqual(self.config["default_agent"], "build")
        self.assertEqual(self.config["subagent_depth"], 2)

    def test_external_directory_boundary(self) -> None:
        rules = self.config["permission"]["external_directory"]
        self.assertEqual(rules["*"], "deny")
        self.assertEqual(rules["/tmp/opencode"], "ask")
        self.assertEqual(rules["/tmp/opencode/**"], "ask")

    def test_git_and_github_writes_require_just_api(self) -> None:
        bash = self.config["permission"]["bash"]
        self.assertEqual(bash["git push *"], "deny")
        self.assertEqual(bash["git commit *"], "deny")
        self.assertEqual(bash["gh pr create *"], "deny")
        self.assertEqual(bash["gh pr edit *"], "deny")
        self.assertEqual(bash["gh pr ready *"], "deny")
        self.assertEqual(bash["gh pr merge *"], "deny")
        self.assertEqual(bash["just agent::commit *"], "allow")
        self.assertEqual(bash["just agent::pr-prepare *"], "allow")
        self.assertEqual(bash["just agent::pr-create *"], "allow")
        self.assertEqual(bash["just agent::pr-edit *"], "allow")
        self.assertEqual(bash["just agent::pr-ready *"], "allow")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["just integrate::merge *"], "ask")
        self.assertEqual(permission_for("task-orchestrator")["bash"]["just agent::pr-prepare *"], "allow")
        self.assertEqual(permission_for("build")["bash"]["just agent::pr-prepare *"], "deny")
        self.assertEqual(permission_for("general")["bash"]["just agent::pr-prepare *"], "deny")

    def test_guarded_finalize_permission_boundaries(self) -> None:
        bash = self.config["permission"]["bash"]
        self.assertEqual(bash["just integrate::finalize *"], "allow")
        self.assertEqual(bash["just agent::state-set *"], "deny")
        self.assertEqual(bash["just agent::cleanup *"], "ask")
        self.assertEqual(bash["just integrate::merge *"], "ask")
        orchestrator = permission_for("task-orchestrator")["bash"]
        self.assertEqual(orchestrator["just integrate::finalize *"], "deny")
        self.assertEqual(orchestrator["just integrate::merge *"], "deny")
        main = permission_for("build")["bash"]
        self.assertEqual(main["just integrate::finalize *"], "allow")
        self.assertEqual(main["just agent::state-set *"], "deny")
        self.assertEqual(main["just agent::cleanup *"], "ask")
        self.assertEqual(main["just integrate::merge *"], "ask")
        self.assertEqual(bash["git fetch *"], "deny")
        self.assertEqual(bash["git pull *"], "deny")

    def test_runtime_preflight_is_narrowly_allowlisted(self) -> None:
        self.assertEqual(
            "allow", self.config["permission"]["bash"]["just agent::preflight"]
        )
        for agent in ("build", "task-orchestrator"):
            self.assertEqual(
                "allow",
                permission_for(agent)["bash"].get("just agent::preflight"),
                agent,
            )

    def test_issue_backed_main_authority_is_explicit_and_gated(self) -> None:
        global_bash = self.config["permission"]["bash"]
        build = permission_for("build")
        build_bash = build["bash"]
        self.assertEqual(global_bash["just agent::task-start-from-issue *"], "deny")
        self.assertEqual(global_bash["just agent::task-start *"], "deny")
        self.assertEqual(global_bash["just agent::contract-check *"], "allow")
        self.assertEqual(global_bash["just agent::contract-resume-check *"], "deny")
        self.assertEqual(build_bash["just agent::task-start-from-issue *"], "allow")
        self.assertEqual(build_bash["just agent::task-start *"], "deny")
        self.assertEqual(build_bash["just agent::contract-check *"], "allow")
        self.assertEqual(build_bash["just agent::contract-resume-check *"], "allow")
        self.assertEqual(build_bash["just agent::state-set *"], "deny")

        main = body_text("build").lower()
        for phrase in (
            "numeric-issue",
            "sole authoritative source",
            "just agent::task-start-from-issue <numeric-issue> <slug>",
            "never call the low-level `just agent::task-start`",
            "just agent::contract-check <numeric-issue>",
            "just agent::contract-resume-check <task>",
            "mode: initial",
            "mode: resume",
            "existing already-launched resumable task",
            "integration-pending",
            "merged",
            "cancelled",
            "status: ready",
            "exactly one `task-orchestrator`",
            "never create or launch a placeholder task",
            "arbitrary url or repository",
            "do not add an llm interpretation step",
        ):
            self.assertIn(phrase, main, phrase)

    def test_task_state_and_lifecycle_mutation_are_not_main_or_orchestrator_authority(self) -> None:
        edit = self.config["permission"]["edit"]
        self.assertEqual(edit[".task-state/**"], "deny")
        for agent in ("build", "task-orchestrator"):
            bash = permission_for(agent)["bash"]
            self.assertEqual(bash.get("just agent::task-start *"), "deny", agent)
            self.assertEqual(bash.get("just agent::task-start-from-issue *"), "deny" if agent == "task-orchestrator" else "allow", agent)
            self.assertEqual(
                bash.get("just agent::state-set *"),
                "deny" if agent == "build" else "allow",
                agent,
            )

        orchestrator = body_text("task-orchestrator").lower()
        for phrase in ("do not start", "hydrate", "pre-initialize", "status: ready", "never use generic `.task-state` edits"):
            self.assertIn(phrase, orchestrator, phrase)

    def test_resume_handoff_is_main_only_and_does_not_reinitialize(self) -> None:
        main = body_text("build").lower()
        orchestrator = body_text("task-orchestrator").lower()
        orchestrator_bash = permission_for("task-orchestrator")["bash"]

        for command in ("just agent::contract-check *", "just agent::contract-resume-check *"):
            self.assertEqual(orchestrator_bash.get(command), "deny", command)
        for phrase in (
            "exactly one authoritative handoff",
            "status: ready",
            "mode: initial",
            "mode: resume",
            "self-approve readiness",
            "do not call either readiness check",
            "reset the task to `initialized`",
            "delete or reopen work units",
            "integration-pending",
            "merged",
            "cancelled",
        ):
            self.assertIn(phrase, orchestrator, phrase)
        for phrase in (
            "contract-resume-check <task>",
            "only when its result is exactly `status: ready` with `mode: resume`",
            "do not call any task-start or hydration path",
            "complete ready evidence",
            "including `task`, `worktree`, and `sha256`",
        ):
            self.assertIn(phrase, main, phrase)

    def test_automation_core_is_not_editable(self) -> None:
        edit = self.config["permission"]["edit"]
        for path in (
            "opencode.json",
            "AGENTS.md",
            "Justfile",
            ".opencode/**",
            ".automation/**",
            ".github/workflows/**",
            ".task-state/**",
        ):
            self.assertEqual(edit[path], "deny", path)

    def test_model_assignment_is_exact(self) -> None:
        expected = {
            "build.md": "openai/gpt-5.6-sol",
            "plan.md": "openai/gpt-5.6-luna",
            "task-orchestrator.md": "openai/gpt-5.6-luna",
            "maintenance-orchestrator.md": "openai/gpt-5.6-sol",
            "general.md": "openai/gpt-5.6-luna",
            "explore.md": "openai/gpt-5.6-luna",
            "verifier.md": "openai/gpt-5.6-luna",
            "reviewer.md": "openai/gpt-5.6-luna",
            "investigator.md": "openai/gpt-5.6-luna",
            "security-reviewer.md": "openai/gpt-5.6-terra",
            "scout.md": "openai/gpt-5.6-luna",
            "architect.md": "openai/gpt-5.6-sol",
        }
        for filename, model in expected.items():
            self.assertIn(f"model: {model}", frontmatter(AGENTS / filename), filename)

    def test_fixed_model_groups_are_exact(self) -> None:
        sol_agents = {
            path.name
            for path in AGENTS.glob("*.md")
            if "model: openai/gpt-5.6-sol" in frontmatter(path)
        }
        luna_agents = {
            path.name for path in AGENTS.glob("*.md")
            if "model: openai/gpt-5.6-luna" in frontmatter(path)
        }
        terra_agents = {
            path.name for path in AGENTS.glob("*.md")
            if "model: openai/gpt-5.6-terra" in frontmatter(path)
        }
        self.assertEqual(
            sol_agents,
            {
                "build.md",
                "maintenance-orchestrator.md",
                "architect.md",
            },
        )
        self.assertEqual(luna_agents, {"plan.md", "task-orchestrator.md", "general.md", "explore.md", "verifier.md", "reviewer.md", "investigator.md", "scout.md"})
        self.assertEqual(terra_agents, {"security-reviewer.md"})
        self.assertFalse(list(AGENTS.glob("*-fallback.md")))

    def test_maintenance_authority_matrix_is_explicit(self) -> None:
        global_bash = json.loads(
            (ROOT / "components" / "agent-core" / "opencode.json").read_text(
                encoding="utf-8"
            )
        )["permission"]["bash"]
        build_bash = permission_for("build")["bash"]
        maintenance_bash = permission_for("maintenance-orchestrator")["bash"]
        expected = {
            "just automation::maintenance-check *": ("allow", "allow", "allow"),
            "just automation::maintenance-review-record *": ("deny", "allow", "deny"),
            "just automation::maintenance-pr-create *": ("deny", "deny", "allow"),
            "just automation::maintenance-finalize *": ("deny", "allow", "deny"),
        }
        for command, actions in expected.items():
            with self.subTest(command=command):
                self.assertEqual(
                    (global_bash.get(command), build_bash.get(command), maintenance_bash.get(command)),
                    actions,
                )
        self.assertEqual(global_bash["just automation::upgrade *"], "ask")
        self.assertEqual(maintenance_bash["just automation::upgrade *"], "ask")
        self.assertEqual(global_bash["just agent::push *"], "ask")
        self.assertEqual(maintenance_bash["just agent::push *"], "ask")
        for command in ("git push *", "gh pr create *", "gh pr merge *"):
            self.assertEqual(global_bash[command], "deny")

    def test_maintenance_effective_policy_when_cli_available(self) -> None:
        opencode = shutil.which("opencode")
        if opencode is None:
            self.skipTest("opencode CLI is not available")

        source_project = ROOT / "templates" / "agent-base"
        with tempfile.TemporaryDirectory(prefix="templates-108-opencode-") as directory:
            project = Path(directory) / "agent-base"
            shutil.copytree(source_project, project)
            config = Path(directory) / "opencode"
            config.mkdir()
            (config / "opencode.json").write_text("{}\n", encoding="utf-8")
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = directory
            env["OPENCODE_CONFIG_HOME"] = str(config)

            effective = {}
            for agent in (
                "build",
                "maintenance-orchestrator",
                "reviewer",
                "security-reviewer",
            ):
                result = subprocess.run(
                    [opencode, "debug", "agent", agent, "--pure"],
                    cwd=project,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                effective[agent] = json.loads(result.stdout)["permission"]

        def last_action(permissions: list[dict], pattern: str) -> str:
            matches = [
                item["action"]
                for item in permissions
                if item["permission"] == "bash" and item.get("pattern") == pattern
            ]
            self.assertTrue(matches, f"missing effective bash permission: {pattern}")
            return matches[-1]

        expected = {
            "just automation::maintenance-check *": ("allow", "allow", "allow"),
            "just automation::maintenance-review-record *": ("allow", "deny", "deny"),
            "just automation::maintenance-pr-create *": ("deny", "allow", "deny"),
            "just automation::maintenance-finalize *": ("allow", "deny", "deny"),
        }
        for command, (build_action, maintenance_action, leaf_action) in expected.items():
            with self.subTest(command=command):
                self.assertEqual(last_action(effective["build"], command), build_action)
                self.assertEqual(
                    last_action(effective["maintenance-orchestrator"], command),
                    maintenance_action,
                )
                self.assertEqual(last_action(effective["reviewer"], command), leaf_action)
                self.assertEqual(
                    last_action(effective["security-reviewer"], command), leaf_action
                )


    def test_plan_agent_repository_local_read_only_contract(self) -> None:
        front = frontmatter(AGENTS / "plan.md")
        body = body_text("plan")
        permission = permission_for("plan")

        self.assertIn("mode: primary", front)
        self.assertIn("model: openai/gpt-5.6-luna", front)
        self.assertEqual(permission.get("edit"), "deny")
        self.assertEqual(permission.get("question"), "allow")
        self.assertEqual(permission.get("skill"), "allow")
        self.assertEqual(permission.get("external_directory"), "deny")
        self.assertEqual(permission.get("doom_loop"), "deny")
        self.assertEqual(permission.get("webfetch"), "deny")
        self.assertEqual(permission.get("websearch"), "deny")
        self.assertEqual(permission.get("bash"), "deny")

        task = permission.get("task")
        self.assertIsInstance(task, dict)
        allowed = {name for name, action in task.items() if action == "allow"}
        self.assertEqual(
            allowed,
            {
                "explore",
                "architect",
                "reviewer",
                "security-reviewer",
            },
        )
        self.assertEqual(task.get("*"), "deny")
        for forbidden in (
            "general",
            "verifier",
            "investigator",
            "task-orchestrator",
            "scout",
        ):
            self.assertEqual(task.get(forbidden), "deny", forbidden)

        lower = body.lower()
        for phrase in (
            "planning_initialization_handoff",
            "execution_prerequisites",
            "verification_handoff",
            "unexecuted",
            "never pass",
            "do not run `just agent::doctor`",
            "do not run `just agent::context`",
            "do not run `just project::doctor`",
            "do not edit files",
            "do not delegate to `general`",
            "do not delegate to `verifier`",
            "do not delegate to `investigator`",
            "do not delegate to `task-orchestrator`",
        ):
            self.assertIn(phrase, lower)

    def test_generated_templates_have_no_fallback_or_recovery_surfaces(self) -> None:
        for template in (
            "agent-base",
            "agent-python",
            "agent-rust",
            "agent-nix",
            "agent-cpp-cmake",
            "agent-typescript-node",
        ):
            generated = ROOT / "templates" / template
            self.assertFalse(list((generated / ".opencode" / "agents").glob("*-fallback.md")), template)
            for path in (
                ".automation/model-fallback.toml",
                ".automation/bin/model_fallback.py",
                ".opencode/commands/task-recover.md",
                ".opencode/commands/task-recover-clear.md",
                ".opencode/skills/task-recovery/SKILL.md",
            ):
                self.assertFalse((generated / path).exists(), f"{template}: {path}")


    def test_opencode_debug_agent_plan_effective_policy_when_cli_available(self) -> None:
        opencode = shutil.which("opencode")
        if opencode is None:
            self.skipTest("opencode CLI is not available")

        source_project = ROOT / "templates" / "agent-base"
        with tempfile.TemporaryDirectory(prefix="templates-56-opencode-") as directory:
            project = Path(directory) / "agent-base"
            shutil.copytree(source_project, project)
            config = Path(directory) / "opencode"
            agents = config / "agents"
            agents.mkdir(parents=True)
            (config / "opencode.json").write_text(
                json.dumps(
                    {
                        "permission": {
                            "task": {"*": "allow", "general": "allow", "verifier": "allow"},
                            "bash": {"*": "allow"},
                            "edit": "allow",
                            "question": "deny",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (agents / "plan.md").write_text(
                """---
description: deliberately permissive global plan
mode: primary
model: openai/gpt-5.6-sol
permission:
  edit: allow
  question: deny
  task:
    "*": allow
    general: allow
    verifier: allow
  bash:
    "*": allow
---

global permissive plan
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = directory
            env["OPENCODE_CONFIG_HOME"] = str(config)
            result = subprocess.run(
                [opencode, "debug", "agent", "plan", "--pure"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        data = json.loads(result.stdout)
        permissions = data["permission"]

        def last_action(permission: str, pattern: str = "*") -> str:
            matches = [
                item["action"]
                for item in permissions
                if item["permission"] == permission and item.get("pattern") == pattern
            ]
            self.assertTrue(matches, f"missing {permission}:{pattern}")
            return matches[-1]

        self.assertEqual(data["description"], "Repository-local read-only planning agent")
        self.assertEqual(data["mode"], "primary")
        self.assertEqual(data["model"], {"providerID": "openai", "modelID": "gpt-5.6-luna"})
        self.assertEqual(last_action("edit"), "deny")
        self.assertEqual(last_action("question"), "allow")
        self.assertEqual(last_action("bash"), "deny")
        self.assertEqual(last_action("task"), "deny")
        for allowed in (
            "explore",
            "architect",
            "reviewer",
            "security-reviewer",
        ):
            self.assertEqual(last_action("task", allowed), "allow", allowed)
        for denied in (
            "general",
            "verifier",
            "investigator",
            "task-orchestrator",
            "scout",
        ):
            self.assertEqual(last_action("task", denied), "deny", denied)

    def test_leaf_agents_cannot_delegate(self) -> None:
        for filename in [f"{leaf}.md" for leaf in LEAF_PRIMARY_AGENTS]:
            self.assertIn("task: deny", frontmatter(AGENTS / filename), filename)

    def test_leaf_agents_have_authority_contract(self) -> None:
        for leaf in LEAF_PRIMARY_AGENTS:
            primary = permission_for(leaf)

            self.assertTrue(permission_entries_are_scalar_lines(primary), leaf)

            self.assertEqual(primary.get("task"), "deny", leaf)

            self.assertEqual(primary.get("question"), "deny", leaf)

            self.assertEqual(primary.get("external_directory"), "deny", leaf)

            self.assertEqual(primary.get("doom_loop"), "deny", leaf)

            for action in _iter_permission_values(primary):
                self.assertNotEqual(action, "ask", leaf)

            if leaf == "scout":
                self.assertEqual(primary.get("webfetch"), "allow", leaf)
                self.assertEqual(primary.get("websearch"), "allow", leaf)
            else:
                self.assertEqual(primary.get("webfetch"), "deny", leaf)
                self.assertEqual(primary.get("websearch"), "deny", leaf)

            if leaf != "general":
                self.assertEqual(primary.get("edit"), "deny", leaf)

            primary_bash = primary.get("bash", {})
            self.assertIsInstance(primary_bash, dict, leaf)

            self.assertEqual(primary_bash.get("*"), "deny", leaf)

            primary_allowed = {cmd for cmd, action in primary_bash.items() if action == "allow"}
            self.assertEqual(primary_allowed, LEAF_ALLOWED_BASH[leaf], leaf)

            for command, action in primary_bash.items():
                if action == "allow":
                    self.assertNotRegex(
                        command,
                        r"(agent::task-start|agent::state-set|agent::batch-plan|agent::commit|agent::push|agent::pr-"
                        r"create|agent::pr-edit|agent::pr-ready|agent::cleanup|integrate::check|integrate::finalize|integrate::merge|"
                        r"integrate::status|project::commit|project::publish|project::release)",
                        leaf,
                    )

    def test_leaf_permissions_are_exact_and_exclude_startup_commands(self) -> None:
        """Leaf profiles are the complete allowlist, not a reduced init profile."""
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            permission = permission_for(leaf)
            bash = permission.get("bash", {})
            self.assertIsInstance(bash, dict, leaf)
            self.assertEqual(
                {command for command, action in bash.items() if action == "allow"},
                LEAF_ALLOWED_BASH[leaf],
                leaf,
            )
            self.assertNotIn("just agent::doctor", bash, leaf)
            self.assertNotIn("just agent::context", bash, leaf)
            if "just project::doctor" in LEAF_ALLOWED_BASH[leaf]:
                self.assertEqual(bash.get("just project::doctor"), "allow", leaf)

    def test_leaf_prompts_require_parent_initialized_contract_and_bounded_objective(self) -> None:
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            assert_leaf_startup_contract(self, body_text(leaf), leaf)

    def test_leaf_status_fields_use_one_canonical_exact_format(self) -> None:
        pattern = re.compile(r"(?m)^\s*-?\s*Start .* exactly one `status: (\w+)`")
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            body = body_text(leaf)
            self.assertTrue(pattern.search(body), leaf)
            self.assertEqual(
                {f"status: {status}" for status in status_report_lines(body)},
                set(LEAF_STATUS_FIELDS),
                leaf,
            )
            self.assertEqual(exact_status_fields(body), list(LEAF_STATUS_FIELDS), leaf)

    def test_task_orchestrator_has_exact_leaf_status_mapping(self) -> None:
        assert_orchestrator_status_contract(self, body_text("task-orchestrator"))

    def test_generated_templates_share_leaf_source_contract(self) -> None:
        for template in LEAF_CONTRACT_TEMPLATES:
            agents = ROOT / "templates" / template / ".opencode" / "agents"
            for leaf in TASK_ORCHESTRATOR_LEAVES:
                source = (AGENTS / f"{leaf}.md").read_text(encoding="utf-8")
                generated = (agents / f"{leaf}.md").read_text(encoding="utf-8")
                self.assertEqual(generated, source, f"{template}: {leaf}")
                assert_leaf_startup_contract(self, generated, f"{template}: {leaf}")
                self.assertEqual(status_report_lines(generated), LEAF_STATUS_SET, f"{template}: {leaf} statuses")
                self.assertEqual(exact_status_fields(generated), list(LEAF_STATUS_FIELDS), f"{template}: {leaf} status format")
                for field in LEAF_STATUS_FIELDS:
                    self.assertIn(field, generated, f"{template}: {leaf}: {field}")

            source_orchestrator = (AGENTS / "task-orchestrator.md").read_text(encoding="utf-8")
            generated_orchestrator = (agents / "task-orchestrator.md").read_text(encoding="utf-8")
            self.assertEqual(generated_orchestrator, source_orchestrator, f"{template}: task-orchestrator")
            assert_orchestrator_status_contract(self, generated_orchestrator)
            for field in LEAF_STATUS_FIELDS:
                self.assertIn(field, generated_orchestrator, f"{template}: task-orchestrator: {field}")
            self.assertEqual(set(exact_status_fields(generated_orchestrator)), set(LEAF_STATUS_FIELDS), f"{template}: task-orchestrator status format")
    def test_leaf_prompts_expose_completion_status_contract(self) -> None:
        for leaf in LEAF_PRIMARY_AGENTS:
            assert_prompt_contract_for_leaf_statuses(self, body_text(leaf), leaf)

    def test_task_orchestrator_fixed_model_policy_contracts(self) -> None:
        primary_text = body_text("task-orchestrator").lower()
        primary_permissions = permission_for("task-orchestrator")
        self.assertEqual(primary_permissions.get("question"), "allow")
        self.assertIn("configured role model is authoritative", primary_text)
        self.assertIn("do not switch models", primary_text)
        self.assertIn("exact provider/model failure", primary_text)
        self.assertIn("work unit or task `blocked`", primary_text)

        self.assertIn("blocked", primary_text)
        self.assertIn("continue", primary_text)
        self.assertTrue(any(term in primary_text for term in ("rejection", "reject")), "task-orchestrator should define rejection behavior")
        self.assertTrue(any(term in primary_text for term in ("continue", "continuing", "continuation")), "task-orchestrator should define continuation behavior")
        self.assertTrue(
            "launder" in primary_text,
            "task-orchestrator should contain anti-laundering language",
        )
        for text, name in ((primary_text, "task-orchestrator"),):
            self.assertIn("needs_decision", text, name)
            self.assertIn("task contract", text, name)
            self.assertIn("question", text, name)
            self.assertIn("options", text, name)
            self.assertIn("tradeoffs", text, name)
            self.assertIn("recommendation", text, name)
            self.assertIn("user-rejected", text, name)
            self.assertIn("final for that exact operation", text, name)
            self.assertIn("never retry, rephrase, re-delegate, or substitute", text, name)
            self.assertNotIn("elevate to depth 0", text, name)

    def test_task_orchestrator_call_graph_is_non_cyclic(self) -> None:
        task_permissions = permission_for("task-orchestrator").get("task", {})
        self.assertIsInstance(task_permissions, dict)
        self.assertEqual(task_permissions.get("*"), "deny")
        for leaf in TASK_ORCHESTRATOR_LEAVES:
            self.assertEqual(task_permissions.get(leaf), "allow", leaf)

        self.assertNotIn("task-orchestrator", task_permissions)
        expected_task_targets = {"*", *TASK_ORCHESTRATOR_LEAVES}
        self.assertEqual(set(task_permissions), expected_task_targets)

    def test_work_unit_contract_has_no_recovery_commands(self) -> None:
        primary = permission_for("task-orchestrator")
        for command in (
            "just agent::work-unit-next *",
            "just agent::work-unit-create *",
            "just agent::work-unit-dispatch-check *",
            "just agent::work-unit-status *",
            "just agent::work-unit-state-set *",
        ):
            self.assertEqual(primary["bash"].get(command), "allow", command)
        global_bash = self.config["permission"]["bash"]
        self.assertNotIn("just agent::work-unit-register *", primary["bash"])
        self.assertEqual(
            "deny", global_bash.get("just agent::work-unit-register *")
        )
        for command in (
            "just agent::work-unit-next *",
            "just agent::work-unit-create *",
            "just agent::work-unit-dispatch-check *",
        ):
            self.assertEqual("deny", global_bash.get(command), command)
        self.assertFalse(any("recovery" in command for command in primary["bash"]))
        self.assertFalse((CORE / ".opencode" / "commands" / "task-recover.md").exists())
        self.assertFalse((CORE / ".opencode" / "skills" / "task-recovery" / "SKILL.md").exists())

    def test_task_autopilot_contract_uses_guarded_fresh_work_units(self) -> None:
        primary = body_text("task-orchestrator").lower()
        skill = (
            CORE / ".opencode" / "skills" / "task-orchestration" / "SKILL.md"
        ).read_text(encoding="utf-8").lower()
        just_api = (CORE / ".automation" / "just" / "agent.just").read_text(
            encoding="utf-8"
        )

        self.assertIn("work-unit-next task:", just_api)
        self.assertIn("work-unit-create task role objective:", just_api)
        self.assertIn("work-unit-dispatch-check task work_unit role objective:", just_api)
        for text, name in ((primary, "task-orchestrator"), (skill, "task-orchestration")):
            self.assertIn("work-unit-create", text, name)
            self.assertIn("work-unit-dispatch-check", text, name)
            self.assertIn("exact same role/objective", text, name)
            self.assertIn("returned id", text, name)
            self.assertIn("never merge", text, name)
            self.assertIn("fresh corrective work unit", text, name)
            self.assertTrue(
                "never hand-author" in text or "rather than hand-authoring" in text,
                name,
            )
        self.assertIn("accept exactly one canonical leaf status field", primary)
        self.assertIn("actual verification", skill)
        self.assertIn("request approval for `push`", skill)

    def test_task_autopilot_generic_blocked_branching_is_explicit(self) -> None:
        primary = body_text("task-orchestrator").lower()
        skill = (
            CORE / ".opencode" / "skills" / "task-orchestration" / "SKILL.md"
        ).read_text(encoding="utf-8").lower()

        for text, name in ((primary, "task-orchestrator"), (skill, "task-orchestration")):
            self.assertIn("persist the blocked work unit as terminal", text, name)
            self.assertIn("inspect its evidence and the task contract", text, name)
            self.assertIn("fresh bounded in-scope corrective work unit", text, name)
            self.assertIn("set the task `blocked`", text, name)
            self.assertIn("surface the blocker, and stop", text, name)
            self.assertIn("never reopen or mutate the blocked work unit", text, name)
            self.assertIn("provider/model", text, name)
            self.assertIn("do not substitute", text, name)
            self.assertIn("persist the affected work unit as terminal `blocked`", text, name)
            self.assertIn("set and surface the task as `blocked`", text, name)

    def test_task_autopilot_publication_verification_gate_is_mandatory(self) -> None:
        primary = body_text("task-orchestrator").lower()
        skill = (
            CORE / ".opencode" / "skills" / "task-orchestration" / "SKILL.md"
        ).read_text(encoding="utf-8").lower()

        required = (
            "before advancing task state to `publication-ready`",
            "before guarded commit, push, or draft pr preparation",
            "relevant focused tests and checks",
            "`git diff --check`",
            "`just project::check`",
            "`just agent::verify <task>`",
            "a reviewer work unit for non-trivial changes",
            "a security-reviewer work unit when trust- or security-sensitive surfaces changed",
            "no unexecuted check or work unit may count as pass",
            "create a fresh corrective work unit and return to verification",
            "only after",
            "evidence gate passes",
        )
        for text, name in ((primary, "task-orchestrator"), (skill, "task-orchestration")):
            for phrase in required:
                self.assertIn(phrase, text, f"{name}: {phrase}")


if __name__ == "__main__":
    unittest.main()
