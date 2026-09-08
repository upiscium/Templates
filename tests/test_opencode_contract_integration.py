from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "agent-base",
    "agent-python",
    "agent-rust",
    "agent-nix",
    "agent-cpp-cmake",
)


class OpencodeContractIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        cls.lock = json.loads((ROOT / "flake.lock").read_text(encoding="utf-8"))

    def test_root_flake_declares_policy_input_and_nixpkgs_follow(self) -> None:
        self.assertIn('opencodeContract', self.flake)
        self.assertIn('url = "github:upiscium/OpencodeContract";', self.flake)
        self.assertIn('inputs.nixpkgs.follows = "nixpkgs";', self.flake)
        self.assertRegex(self.flake, r"outputs\s*=\s*\{[^}]*\bopencodeContract\b")

    def test_lock_pins_contract_repository_and_revision_shape(self) -> None:
        self.assertIn("opencodeContract", self.lock["nodes"]["root"]["inputs"])
        self.assertEqual(
            "opencodeContract",
            self.lock["nodes"]["root"]["inputs"]["opencodeContract"],
        )
        node = self.lock["nodes"]["opencodeContract"]
        self.assertEqual("upiscium", node["locked"]["owner"])
        self.assertEqual("OpencodeContract", node["locked"]["repo"])
        self.assertEqual("7e4b7eb29c2322264c613310d7f802b0237f7b1b", node["locked"]["rev"])
        self.assertEqual("github", node["locked"]["type"])
        self.assertRegex(node["locked"]["rev"], r"^[0-9a-f]{40}$")
        self.assertEqual(["nixpkgs"], node["inputs"]["nixpkgs"])

    def test_linux_only_policy_check_uses_explicit_agent_core_profile_and_self(self) -> None:
        self.assertIn("flake-utils.lib.eachDefaultSystem", self.flake)
        self.assertIn('if system == "x86_64-darwin" then { }', self.flake)
        self.assertIn("optionalAttrs isLinux", self.flake)
        self.assertIn("checks.opencode-contract", self.flake)
        self.assertIn("opencodeContract.packages.${system}.opencode-contract", self.flake)
        self.assertRegex(
            self.flake,
            re.compile(
                r"opencode-contract audit-consumer\s+\\\s+"
                r"--profile agent-core\s+\\\s+"
                r"--consumer \$\{self\}\s+\\\s+"
                r"--strict",
                re.MULTILINE,
            ),
        )

    def test_generated_template_flakes_do_not_receive_policy_input(self) -> None:
        for template in TEMPLATES:
            flake = ROOT / "templates" / template / "flake.nix"
            self.assertNotIn("opencodeContract", flake.read_text(encoding="utf-8"), template)

    def test_agent_core_version_and_upstream_are_expected(self) -> None:
        core = ROOT / "components" / "agent-core" / ".automation"
        self.assertEqual("3\n", (core / "VERSION").read_text(encoding="utf-8"))
        self.assertEqual(
            'repository = "github:upiscium/Templates"\nref = "main"\n'
            'component = "components/agent-core"\n',
            (core / "UPSTREAM").read_text(encoding="utf-8"),
        )

    def test_ci_builds_locked_policy_check_without_cloning_policy(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-ci.yml").read_text(encoding="utf-8")
        self.assertIn("cachix/install-nix-action@v31", workflow)
        self.assertIn(
            "nix flake check --all-systems --no-build --no-update-lock-file",
            workflow,
        )
        self.assertIn(
            "nix build .#checks.x86_64-linux.opencode-contract --no-link --no-update-lock-file",
            workflow,
        )
        self.assertNotRegex(workflow, r"git\s+clone.*OpencodeContract")

    def test_tracked_contract_surfaces_have_no_legacy_identity(self) -> None:
        legacy = (
            "OpenCode" + "Policy",
            "opencode" + "Policy",
            "opencode" + "-policy",
            "opencode" + "_policy",
        )
        tracked_and_new = subprocess.run(
            ("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        matches = []
        for raw_path in tracked_and_new:
            if not raw_path:
                continue
            relative_path = raw_path.decode()
            path = ROOT / relative_path
            if not path.is_file():
                continue
            content = path.read_bytes()
            matches.extend(
                f"{relative_path}: legacy identity in path"
                for forbidden in legacy
                if forbidden in relative_path
            )
            matches.extend(
                f"{relative_path}: {forbidden}"
                for forbidden in legacy
                if forbidden.encode() in content
            )
        self.assertEqual([], matches)


if __name__ == "__main__":
    unittest.main()
