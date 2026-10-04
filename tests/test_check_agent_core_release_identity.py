from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_agent_core_release_identity.py"
VALIDATOR = ROOT / "tools" / "agent_core_release_identity.py"
LAUNCHER = ROOT / "tools" / "run_agent_core_release_gate.sh"


class ReleaseIdentityCLITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.git_environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        self.git_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        self.git(["init", "-q"])
        self.git(["config", "user.name", "Release Identity Test"])
        self.git(["config", "user.email", "release-identity-test@example.invalid"])
        self._write_source_files(
            status="planned",
            agent_core_version="1.0.0",
            templates_version="4.0.0",
            marker="3",
        )
        self.previous_revision = self.commit("initial planned fixture")

    def git(self, arguments: list[str]) -> bytes:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            env=self.git_environment,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            self.fail(
                "fixture Git command failed: "
                + result.stderr.decode("utf-8", "replace").strip()
            )
        return result.stdout

    def write(self, relative_path: str, value: bytes | str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)

    def _write_contract(
        self,
        *,
        status: str,
        agent_core_version: str,
        templates_version: str,
    ) -> None:
        contract = {
            "schema": "agent-core-release-identity",
            "schemaVersion": 1,
            "agentCore": {
                "version": agent_core_version,
                "architecture": "v4",
                "status": status,
            },
            "templates": {"version": templates_version},
        }
        self.write(
            "distribution/release-identity.json",
            json.dumps(contract, indent=2) + "\n",
        )

    def _write_source_files(
        self,
        *,
        status: str,
        agent_core_version: str,
        templates_version: str,
        marker: str,
        component_as_file: bool = False,
        marker_as_symlink: bool = False,
    ) -> None:
        self._write_contract(
            status=status,
            agent_core_version=agent_core_version,
            templates_version=templates_version,
        )
        if component_as_file:
            self.write("components/agent-core", "not-a-tree")
        else:
            if marker_as_symlink:
                marker_path = self.root / "components/agent-core/.automation/VERSION"
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                marker_path.unlink(missing_ok=True)
                marker_path.symlink_to(marker)
            else:
                self.write("components/agent-core/.automation/VERSION", marker)
            self.write("components/agent-core/payload.txt", "stable Agent Core payload\n")
        self.write("templates-only.txt", "Templates content\n")
        tools = self.root / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CHECKER, tools / CHECKER.name)
        shutil.copyfile(VALIDATOR, tools / VALIDATOR.name)

    def commit(self, message: str) -> str:
        self.git(["add", "-A"])
        self.git(["commit", "-q", "-m", message])
        return self.git(["rev-parse", "HEAD"]).decode("ascii").strip()

    def set_candidate(
        self,
        *,
        agent_core_version: str = "1.0.0",
        templates_version: str = "4.0.0",
        marker: str | None = None,
    ) -> None:
        self._write_contract(
            status="candidate",
            agent_core_version=agent_core_version,
            templates_version=templates_version,
        )
        if marker is not None:
            self.write("components/agent-core/.automation/VERSION", marker)

    def run_checker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.root / "tools/check_agent_core_release_identity.py"),
                *arguments,
            ],
            cwd=self.root,
            env=self.git_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_blocked(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(text, result.stderr)
        self.assertNotIn("IDENTITY_VALIDATED", result.stdout)

    def test_planned_v3_fixture_is_blocked(self) -> None:
        result = self.run_checker()
        self.assert_blocked(result, "status must be candidate")

    def test_future_candidate_v4_first_release_is_validated_from_git_objects(self) -> None:
        self.set_candidate(marker="1.0.0\n")
        head = self.commit("candidate v4 first release")

        result = self.run_checker()

        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("IDENTITY_VALIDATED", output["result"])
        self.assertEqual(head, output["identity"]["templatesSourceRevision"])
        expected_tree = self.git(["rev-parse", f"{head}:components/agent-core"]).decode().strip()
        self.assertEqual(expected_tree, output["identity"]["agentCorePayload"])
        self.assertEqual("1.0.0", output["identity"]["agentCoreVersion"])
        self.assertEqual("4.0.0", output["identity"]["templatesVersion"])
        self.assertIsNone(output["previousReleaseSourceRevision"])
        self.assertNotIn("RELEASE_READY", result.stdout)

    def _make_released_prior(self) -> str:
        self._write_contract(
            status="candidate",
            agent_core_version="1.0.0",
            templates_version="4.0.0",
        )
        self.write("components/agent-core/.automation/VERSION", "1.0.0\n")
        return self.commit("tagged candidate v4.0.0 fixture")

    def test_payload_change_without_agent_core_version_bump_is_blocked(self) -> None:
        # A CLI fixture cannot represent an Agent Core version bump with an
        # identical payload tree: the canonical VERSION marker is inside that
        # tree. The pure evaluator's direct unchanged-tree bump case is covered
        # by test_agent_core_release_identity.py.
        prior = self._make_released_prior()
        self.set_candidate(templates_version="4.1.0")
        self.write("components/agent-core/payload.txt", "changed payload\n")
        self.commit("candidate changes payload without Agent Core bump")

        result = self.run_checker(prior)

        self.assert_blocked(result, "unchanged AgentCore version")

    def test_templates_only_change_can_keep_agent_core_tree(self) -> None:
        prior = self._make_released_prior()
        prior_tree = self.git(
            ["rev-parse", f"{prior}:components/agent-core"]
        ).decode("ascii").strip()
        self.set_candidate(templates_version="4.1.0")
        self.write("templates-only.txt", "new Templates-only content\n")
        head = self.commit("Templates-only candidate change")

        result = self.run_checker(prior)

        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("IDENTITY_VALIDATED", output["result"])
        self.assertEqual(prior_tree, output["identity"]["agentCorePayload"])
        self.assertEqual(head, output["identity"]["templatesSourceRevision"])
        self.assertEqual(prior, output["previousReleaseSourceRevision"])
        self.assertIn("not proof of publication", output["publicationEvidence"])

    def test_noncanonical_marker_is_blocked(self) -> None:
        self.set_candidate(marker="1.0.0\n\n")
        self.commit("candidate with noncanonical marker")

        result = self.run_checker()

        self.assert_blocked(result, "installed AgentCore marker")

    def test_symlink_marker_and_non_tree_payload_are_blocked(self) -> None:
        self._write_source_files(
            status="candidate",
            agent_core_version="1.0.0",
            templates_version="4.0.0",
            marker="1.0.0",
            marker_as_symlink=True,
        )
        self.commit("candidate with symlink marker")
        result = self.run_checker()
        self.assert_blocked(result, "regular non-executable file")

        component_root = self.root / "components/agent-core"
        if component_root.is_dir():
            shutil.rmtree(component_root)
        self.write("components/agent-core", "not-a-tree")
        self.commit("candidate with non-tree payload object")
        result = self.run_checker()
        self.assert_blocked(result, "canonical components/agent-core payload is not a Git tree")

    def test_prior_revision_must_be_full_sha_and_a_released_matching_identity(self) -> None:
        self.set_candidate(marker="1.0.0")
        self.commit("candidate fixture")
        self.assert_blocked(self.run_checker("abcd"), "exact lowercase full 40-hex")
        self.assert_blocked(self.run_checker("f" * 40), "Git rev-parse failed")

        self.assert_blocked(
            self.run_checker(self.previous_revision),
            "previous release contract must be candidate or released",
        )

        prior = self._make_released_prior()
        self.write("components/agent-core/.automation/VERSION", "1.0.1")
        mismatched_prior = self.commit("released fixture with marker mismatch")
        self.assertNotEqual(prior, mismatched_prior)
        result = self.run_checker(mismatched_prior)
        self.assert_blocked(result, "previous installed Agent Core marker")

    def test_without_prior_only_the_first_stable_pair_is_allowed(self) -> None:
        self.set_candidate(
            agent_core_version="1.0.1",
            templates_version="4.1.0",
            marker="1.0.1",
        )
        self.commit("candidate beyond the first pair without prior")

        result = self.run_checker()

        self.assert_blocked(result, "without a verified prior release")

    def test_dirty_source_worktree_is_blocked(self) -> None:
        self.write("templates-only.txt", "uncommitted edit\n")

        result = self.run_checker()

        self.assert_blocked(result, "worktree is not clean")

    def test_just_recipe_uses_the_trusted_interpreter_launcher(self) -> None:
        recipe = (ROOT / "just/agent-core.just").read_text(encoding="utf-8")
        identity_recipe = recipe.split("release-identity-check previous_release_source_revision=", 1)[1]
        identity_recipe = identity_recipe.split("\n[working-directory:", 1)[0]
        self.assertIn("/bin/sh {{quote(release_gate_launcher)}} {{quote(release_identity_check)}}", identity_recipe)
        self.assertNotIn("python3 -I {{quote(release_identity_check)}}", identity_recipe)

    def test_untrusted_python_on_path_cannot_forge_validation(self) -> None:
        fake_bin = self.root / "untrusted-bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text("#!/bin/sh\nprintf 'IDENTITY_VALIDATED\\n'\n", encoding="utf-8")
        python.chmod(0o755)
        environment = {**os.environ, "PATH": str(fake_bin)}
        result = subprocess.run(
            ["/bin/sh", str(LAUNCHER), str(self.root / "tools/check_agent_core_release_identity.py"), "--"],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selected Python interpreter path is not trusted", result.stderr)
        self.assertNotIn("IDENTITY_VALIDATED", result.stdout)


if __name__ == "__main__":
    unittest.main()
