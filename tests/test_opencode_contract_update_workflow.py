from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "update-opencode-contract.yml"


class OpencodeContractUpdateWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.trigger = cls.workflow.split("permissions:", 1)[0]
        cls.validate = cls.workflow.split("  validate:", 1)[1].split(
            "  publish:", 1
        )[0]
        cls.publish = cls.workflow.split("  publish:", 1)[1]

    def test_identity_is_fully_migrated(self) -> None:
        for identity in (
            "OpencodeContract",
            "opencodeContract",
            "opencode-contract",
            "check_opencode_contract_lock_update.py",
        ):
            self.assertIn(identity, self.workflow)
        legacy = ("OpenCode" + "Policy", "opencode" + "Policy", "opencode" + "-policy")
        for identity in legacy:
            self.assertNotIn(identity, self.workflow)

    def test_trigger_and_concurrency_are_manual_and_serialized(self) -> None:
        self.assertIn("workflow_dispatch:", self.trigger)
        self.assertNotRegex(self.trigger, r"(?m)^\s+schedule:")
        self.assertNotRegex(self.trigger, r"(?m)^\s+pull_request:")
        self.assertNotRegex(self.trigger, r"(?m)^\s+push:")
        self.assertIn("group: update-opencode-contract", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_dispatch_repository_and_input_are_trusted(self) -> None:
        self.assertIn('DISPATCH_REF: ${{ github.ref }}', self.validate)
        self.assertRegex(self.validate, r"DISPATCH_REF.*refs/heads/main[\s\S]*exit 1")
        self.assertIn("ref: main", self.validate)
        self.assertIn("fetch-depth: 0", self.validate)
        self.assertIn("persist-credentials: false", self.validate)
        self.assertIn('git remote add origin "https://github.com/$GITHUB_REPOSITORY.git"', self.publish)
        self.assertIn('nix flake update opencodeContract', self.validate)
        for field, value in (("owner", "upiscium"), ("repo", "OpencodeContract"), ("type", "github")):
            self.assertIn(f'"{field}": "{value}"', self.validate)

    def test_contract_check_and_agent_core_profile_are_used(self) -> None:
        check = ".#checks.x86_64-linux.opencode-contract"
        environment = f"nix develop --no-update-lock-file {check} --command"
        self.assertIn(f"nix build {check} --no-link --no-update-lock-file", self.validate)
        self.assertIn(f"{environment} opencode-contract validate", self.validate)
        self.assertEqual(2, self.validate.count(environment))
        self.assertIn("opencode-contract audit-consumer", self.validate)
        self.assertIn("--profile agent-core", self.validate)
        self.assertIn("--strict", self.validate)

    def test_candidate_is_lock_only_and_sha256_bound(self) -> None:
        self.assertIn('test "$(git status --short)" = " M flake.lock"', self.validate)
        self.assertIn("candidate_sha256", self.workflow)
        self.assertIn("hashlib.sha256(content).hexdigest()", self.workflow)
        self.assertIn("validated candidate checksum mismatch", self.publish)
        self.assertIn("--candidate-sha256", self.publish)
        self.assertIn("--expected-revision", self.publish)
        self.assertIn("git add -- flake.lock", self.publish)
        self.assertNotRegex(self.publish, r"git add (?:\.|-A|--all)\b")

    def test_validation_steps_include_render_and_distribution(self) -> None:
        self.assertIn("python3 -m unittest discover -s tests -v", self.validate)
        self.assertIn("python3 tools/render_templates.py check", self.validate)
        self.assertIn("nix flake check --all-systems --no-build --no-update-lock-file", self.validate)
        self.assertIn("just template::distribution-verify", self.validate)
        self.assertIn("SUMMARY", self.validate)
        self.assertIn("DIFF=0", self.validate)
        self.assertIn("MISSING=0", self.validate)

    def test_duplicate_pull_request_behavior_is_safe(self) -> None:
        self.assertIn('BRANCH="chore/opencode-contract-${SHORT_REV}"', self.publish)
        self.assertIn("multiple open pull requests exist", self.publish)
        self.assertIn("remote branch exists without an open pull request", self.publish)
        self.assertIn("existing_pr", self.publish)
        self.assertIn("no branch, commit, push, or new pull request was created", self.publish)

    def test_publication_is_draft_only(self) -> None:
        self.assertIn("--draft", self.publish)
        self.assertIn('git commit -m "chore: update OpencodeContract to $SHORT_REV"', self.publish)
        self.assertIn('--title "chore: update OpencodeContract to $SHORT_REV"', self.publish)
        self.assertIn("--body-file", self.publish)
        self.assertRegex(self.publish, r"### OpencodeContract update")
        self.assertIn("## Contract audit", self.publish)
        self.assertNotRegex(self.publish, r"(?:gh\s+pr\s+merge|git\s+merge|--auto(?:-merge)?)\b")
        self.assertNotRegex(self.publish, r"git push[^\n]*(?:--force|\s-f(?:\s|$))")

    def test_updater_does_not_duplicate_generated_runtime_matrix(self) -> None:
        for template in ("agent-python", "agent-rust", "agent-nix", "agent-cpp-cmake"):
            self.assertNotIn(template, self.workflow)


if __name__ == "__main__":
    unittest.main()
