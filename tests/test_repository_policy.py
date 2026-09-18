from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "components" / "agent-core"
SCRIPT = CORE / ".automation" / "bin" / "repository_policy.py"
SPEC = importlib.util.spec_from_file_location("repository_policy", SCRIPT)
assert SPEC and SPEC.loader
policy_runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy_runtime
SPEC.loader.exec_module(policy_runtime)

TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
    "agent-typescript-node",
)


class RepositoryPolicyContractTest(unittest.TestCase):
    def test_policy_requires_main_and_pull_request_without_bypass(self) -> None:
        policy = policy_runtime.load_policy(CORE)
        self.assertEqual(policy["default_branch"], "main")
        ruleset = policy["ruleset"]
        self.assertEqual(ruleset["name"], "Agent repository policy")
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(
            ruleset["conditions"],
            {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        )

        rules = {rule["type"]: rule for rule in ruleset["rules"]}
        self.assertEqual(set(rules), {"pull_request", "deletion", "non_fast_forward"})
        pull_request = rules["pull_request"]["parameters"]
        self.assertEqual(pull_request["required_approving_review_count"], 0)
        self.assertEqual(
            set(pull_request["allowed_merge_methods"]),
            {"merge", "squash", "rebase"},
        )
        self.assertFalse(pull_request["require_code_owner_review"])
        self.assertFalse(pull_request["require_last_push_approval"])

    def test_generated_agent_core_policy_files_match_source(self) -> None:
        paths = (
            ".automation/repository-policy.json",
            ".automation/bin/repository_policy.py",
            ".automation/just/repository.just",
            ".automation/REPOSITORY_POLICY.md",
            ".automation/ownership.toml",
            "Justfile",
            "opencode.json",
        )
        for template in TEMPLATES:
            generated = ROOT / "templates" / template
            for relative in paths:
                self.assertEqual(
                    (CORE / relative).read_bytes(),
                    (generated / relative).read_bytes(),
                    f"generated drift: {template}/{relative}",
                )

    def test_router_and_opencode_permission_boundary(self) -> None:
        justfile = (CORE / "Justfile").read_text(encoding="utf-8")
        self.assertIn("mod repository '.automation/just/repository.just'", justfile)

        module = (CORE / ".automation" / "just" / "repository.just").read_text(
            encoding="utf-8"
        )
        self.assertIn("policy-check:", module)
        self.assertIn("policy-apply:", module)

        config = json.loads((CORE / "opencode.json").read_text(encoding="utf-8"))
        bash = config["permission"]["bash"]
        self.assertEqual(bash["just repository::policy-check"], "allow")
        self.assertEqual(bash["just repository::policy-apply"], "ask")

    def test_policy_apply_is_forbidden_from_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".task-state"
            state.mkdir()
            (state / "task.md").write_text("# Task\n", encoding="utf-8")
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "forbidden from a Task worktree",
            ):
                policy_runtime.command_apply(root)

    def test_policy_apply_refuses_to_invent_missing_main_branch(self) -> None:
        policy = {
            "version": 1,
            "default_branch": "main",
            "ruleset": {},
        }
        before = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "master",
                "expected": "main",
                "expectedBranchExists": False,
                "match": False,
            },
            "ruleset": {"id": None, "match": False},
            "drift": ["default branch drift"],
        }
        with patch.object(policy_runtime, "task_worktree", return_value=False), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(policy_runtime, "inspect", return_value=before), patch.object(
            policy_runtime, "gh_api"
        ) as gh_api:
            with self.assertRaisesRegex(
                policy_runtime.RepositoryPolicyError,
                "branch 'main' does not exist",
            ):
                policy_runtime.command_apply(Path("/tmp/repository"))
            gh_api.assert_not_called()

    def test_policy_apply_sets_main_before_managing_ruleset(self) -> None:
        policy = {
            "version": 1,
            "default_branch": "main",
            "ruleset": {"name": "Agent repository policy"},
        }
        before = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "master",
                "expected": "main",
                "expectedBranchExists": True,
                "match": False,
            },
            "ruleset": {"id": None, "match": False},
            "drift": ["drift"],
        }
        after = {
            "repository": "example/repository",
            "defaultBranch": {
                "actual": "main",
                "expected": "main",
                "expectedBranchExists": True,
                "match": True,
            },
            "ruleset": {"id": 1, "match": True},
            "drift": [],
        }
        calls: list[tuple[str, str, object]] = []

        def fake_api(root, method, endpoint, *, body=None, allow_not_found=False):
            calls.append((method, endpoint, body))
            return {}

        with patch.object(policy_runtime, "task_worktree", return_value=False), patch.object(
            policy_runtime, "load_policy", return_value=policy
        ), patch.object(policy_runtime, "inspect", side_effect=[before, after]), patch.object(
            policy_runtime, "gh_api", side_effect=fake_api
        ):
            self.assertEqual(policy_runtime.command_apply(Path("/tmp/repository")), 0)

        self.assertEqual(calls[0], ("PATCH", "repos/example/repository", {"default_branch": "main"}))
        self.assertEqual(calls[1][0:2], ("POST", "repos/example/repository/rulesets"))

    def test_repository_policy_is_current_repository_only(self) -> None:
        parser = policy_runtime.parser()
        check = parser.parse_args(["check"])
        apply = parser.parse_args(["apply"])
        self.assertEqual(check.command, "check")
        self.assertEqual(apply.command, "apply")
        self.assertNotIn("repository", vars(check))
        self.assertNotIn("repository", vars(apply))

    def test_ownership_and_docs_cover_policy_surfaces(self) -> None:
        ownership = (CORE / ".automation" / "ownership.toml").read_text(encoding="utf-8")
        for path in (
            ".automation/REPOSITORY_POLICY.md",
            ".automation/repository-policy.json",
            ".automation/bin/repository_policy.py",
            ".automation/just/repository.just",
            "opencode.json",
        ):
            self.assertIn(f'"{path}" = "replace"', ownership)

        docs = (CORE / ".automation" / "REPOSITORY_POLICY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("default branch is `main`", docs)
        self.assertIn("pull request is mandatory", docs)
        self.assertIn("zero approving reviews", docs)
        self.assertIn("No bypass actor", docs)
        self.assertIn("does not create or rename branches", docs)


if __name__ == "__main__":
    unittest.main()
