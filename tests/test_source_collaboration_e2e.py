from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_FILES = (
    "Justfile",
    "just/source.just",
    "tools/source_collaboration.py",
    "tools/source_publication_launcher.sh",
    "just/template.just",
    "tools/render_templates.py",
    "flake.nix",
)
ISSUE_VIEW = "issue view 215 --repo upiscium/Templates --json number,state,title,url"


def git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }
    )
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class SourceCollaborationE2ETest(unittest.TestCase):
    """Exercise the installed shell entrypoint against disposable Git worktrees."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="source-collaboration-e2e-")
        self.sandbox = Path(self.temporary.name)
        self.repo = self.sandbox / "mutable-worktree"
        self.install = self.sandbox / "installed-source"
        self.fake_bin = self.sandbox / "fake-bin"
        self.gh_log = self.sandbox / "gh.log"
        self.repo.mkdir()
        self.fake_bin.mkdir()

        self._seed_repo()
        self._install_launcher()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_repo(self) -> None:
        for relative in AUTHORITY_FILES:
            source = SOURCE_ROOT / relative
            self.assertTrue(source.is_file(), f"missing checked-out authority source: {source}")
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            self.assertTrue(destination.is_file())
            self.assertFalse(destination.is_symlink())

        (self.repo / "README.md").write_text("temporary source fixture\n", encoding="utf-8")
        (self.repo / "normal.txt").write_text("before\n", encoding="utf-8")

        manifest = {
            "templates": {
                "smoke": {"adapter": "smoke", "description": "E2E parity fixture"}
            }
        }
        (self.repo / "templates").mkdir()
        (self.repo / "templates" / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        component_files = {
            "components/agent-core/core.txt": "core fixture\n",
            "components/adapters/smoke/adapter.txt": "adapter fixture\n",
            "templates/smoke/core.txt": "core fixture\n",
            "templates/smoke/adapter.txt": "adapter fixture\n",
        }
        for relative, contents in component_files.items():
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")

        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "source-collaboration@example.invalid")
        git(self.repo, "config", "user.name", "Source Collaboration E2E")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fixture base")
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "remote", "add", "origin", "git@github.com:upiscium/Templates.git")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.approved_base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-qb", "feat/215-source-collaboration")

    @staticmethod
    def _shell_literal_contents(value: str) -> str:
        # The launcher template already puts each placeholder inside single quotes.
        return value.replace("'", "'\\''")

    def _install_launcher(self) -> None:
        self.install.mkdir()
        bin_dir = self.install / "bin"
        share_dir = self.install / "share"
        bin_dir.mkdir()
        share_dir.mkdir()
        self.payload = share_dir / "source_collaboration.py"
        source_payload = (SOURCE_ROOT / "tools" / "source_collaboration.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("@TRUSTED_SOURCE_BASE@", source_payload)
        shutil.copy2(SOURCE_ROOT / "tools" / "render_templates.py", share_dir / "render_templates.py")

        fake_gh = self.fake_bin / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(self.gh_log))}\n"
            "case \"$*\" in\n"
            f"  '{ISSUE_VIEW}')\n"
            "    printf '%s\\n' '{\"number\":215,\"state\":\"OPEN\","
            "\"title\":\"Source collaboration\",\"url\":\"x\"}'\n"
            "    exit 0\n"
            "    ;;\n"
            "  *) printf 'unexpected fake gh operation: %s\\n' \"$*\" >&2; exit 97 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)

        git_executable = shutil.which("git")
        if git_executable is None:
            self.fail("git is required for the source collaboration E2E fixture")
        path_entries = [self.fake_bin, Path(git_executable).resolve().parent]
        path_entries.extend(Path(path) for path in ("/usr/bin", "/bin") if Path(path).is_dir())
        trusted_path = os.pathsep.join(dict.fromkeys(str(path) for path in path_entries))
        self.trusted_path = trusted_path
        replacements_in_payload = {
            "@TRUSTED_SOURCE_BASE@": self.approved_base,
            "@TRUSTED_GIT@": str(Path(git_executable).resolve()),
            "@TRUSTED_GH@": str(fake_gh),
            "@TRUSTED_PATH@": trusted_path,
            "#!/usr/bin/env python3": f"#!{sys.executable} -I",
        }
        for marker, value in replacements_in_payload.items():
            self.assertIn(marker, source_payload)
            source_payload = source_payload.replace(marker, value)
        self.payload.write_text(source_payload, encoding="utf-8")
        self.payload.chmod(0o755)

        launcher_template = (SOURCE_ROOT / "tools" / "source_publication_launcher.sh").read_text(
            encoding="utf-8"
        )
        replacements = {
            "@PATH@": trusted_path,
            "@PYTHON@": sys.executable,
            "@PAYLOAD@": str(self.payload),
        }
        for marker, value in replacements.items():
            self.assertIn(marker, launcher_template)
            launcher_template = launcher_template.replace(
                marker, self._shell_literal_contents(value)
            )
        self.launcher = bin_dir / "templates-source"
        self.launcher.write_text(launcher_template, encoding="utf-8")
        self.launcher.chmod(0o755)

        self.assertFalse(self.launcher.is_relative_to(self.repo))
        self.assertFalse(self.payload.is_relative_to(self.repo))
        self.launcher_env = os.environ.copy()
        self.launcher_env.update(
            {
                "HOME": str(self.sandbox / "home"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

    def invoke(
        self, *args: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = self.launcher_env.copy()
        if environment:
            env.update(environment)
        return subprocess.run(
            [str(self.launcher), "--worktree", str(self.repo), *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def invoke_payload(
        self, *args: str, payload: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.launcher_env.copy()
        env["PATH"] = self.trusted_path
        if environment:
            env.update(environment)
        command = ([str(self.payload)] if payload is None else [sys.executable, "-I", str(payload)])
        return subprocess.run(
            [*command, "--worktree", str(self.repo), *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def gh_calls(self) -> list[str]:
        if not self.gh_log.exists():
            return []
        return self.gh_log.read_text(encoding="utf-8").splitlines()

    def assert_only_issue_views(self, count: int) -> None:
        self.assertEqual([ISSUE_VIEW] * count, self.gh_calls())

    def assert_authority_rejected(
        self, result: subprocess.CompletedProcess[str], path: str
    ) -> None:
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("maintainer bootstrap", result.stderr)
        self.assertIn(path, result.stderr)

    def write_command_sentinel(self, path: Path) -> str:
        return f"printf '%s\\n' executed > {shlex.quote(str(path))}"

    def pending_digest(self) -> str:
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        result = self.invoke("publication-check", "215")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        ready = json.loads(result.stdout)
        self.assertEqual("READY", ready["status"])
        return ready["scope_digest"]

    def test_redirected_live_justfile_is_rejected_without_running_recipe(self) -> None:
        sentinel = self.sandbox / "justfile-sentinel"
        (self.repo / "Justfile").write_text(
            "publication-check:\n"
            f"    @{self.write_command_sentinel(sentinel)}\n",
            encoding="utf-8",
        )

        result = self.invoke("publication-check", "215")

        self.assert_authority_rejected(result, "Justfile")
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(1)

    def test_redirected_live_source_justfile_is_rejected_without_running_recipe(self) -> None:
        sentinel = self.sandbox / "source-just-sentinel"
        (self.repo / "just" / "source.just").write_text(
            "publication-check issue:\n"
            f"    @{self.write_command_sentinel(sentinel)}\n",
            encoding="utf-8",
        )

        result = self.invoke("publication-check", "215")

        self.assert_authority_rejected(result, "just/source.just")
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(1)

    def test_replaced_live_policy_is_rejected_without_running_stub(self) -> None:
        sentinel = self.sandbox / "policy-sentinel"
        stub = (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed\\n', encoding='utf-8')\n"
            "print('{\"status\":\"READY\"}')\n"
        )
        (self.repo / "tools" / "source_collaboration.py").write_text(stub, encoding="utf-8")

        result = self.invoke("publication-check", "215")

        self.assert_authority_rejected(result, "tools/source_collaboration.py")
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(1)

    def test_reverted_authority_commit_is_rejected_before_publication_operations(self) -> None:
        target = self.repo / "tools" / "source_collaboration.py"
        trusted = target.read_bytes()
        target.write_text("# unauthorized authority change\n", encoding="utf-8")
        git(self.repo, "add", "tools/source_collaboration.py")
        git(self.repo, "commit", "-qm", "unauthorized authority change")
        target.write_bytes(trusted)
        git(self.repo, "add", "tools/source_collaboration.py")
        git(self.repo, "commit", "-qm", "revert authority change")
        current_head = git(self.repo, "rev-parse", "HEAD")

        invocations = (
            ("publication-check", "215"),
            ("pr-create", "215"),
            ("checkpoint", "215", "1", current_head, "test checkpoint"),
        )
        for args in invocations:
            with self.subTest(command=args[0]):
                self.assert_authority_rejected(
                    self.invoke(*args), "tools/source_collaboration.py"
                )
        # The fake CLI accepted only Issue #215 metadata; no GitHub API call was attempted.
        self.assert_only_issue_views(len(invocations))

    def test_moved_tracking_ref_cannot_hide_reverted_authority_history(self) -> None:
        target = self.repo / "tools" / "source_collaboration.py"
        trusted = target.read_bytes()
        target.write_text("# unauthorized\n", encoding="utf-8")
        git(self.repo, "add", "tools/source_collaboration.py")
        git(self.repo, "commit", "-qm", "unauthorized authority")
        target.write_bytes(trusted)
        git(self.repo, "add", "tools/source_collaboration.py")
        git(self.repo, "commit", "-qm", "revert authority")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assert_authority_rejected(
            self.invoke("pr-create", "215"), "tools/source_collaboration.py"
        )
        self.assert_only_issue_views(1)

    def test_grafted_history_cannot_hide_reverted_authority_change(self) -> None:
        target = self.repo / "Justfile"
        trusted = target.read_bytes()
        target.write_text("# untrusted\n", encoding="utf-8")
        git(self.repo, "add", "Justfile")
        git(self.repo, "commit", "-qm", "untrusted authority")
        target.write_bytes(trusted)
        git(self.repo, "add", "Justfile")
        git(self.repo, "commit", "-qm", "revert authority")
        current = git(self.repo, "rev-parse", "HEAD")
        (self.repo / ".git" / "info" / "grafts").write_text(
            f"{current} {self.approved_base}\n", encoding="ascii"
        )
        result = self.invoke("pr-create", "215")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("grafted or shallow", result.stderr)
        self.assert_only_issue_views(1)

    def test_candidate_git_execution_config_is_rejected_without_running_it(self) -> None:
        sentinel = self.sandbox / "filter-sentinel"
        git(self.repo, "config", "filter.sneaky.clean", self.write_command_sentinel(sentinel))
        result = self.invoke("publication-check", "215")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("unsafe local Git configuration", result.stderr)
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(0)

    def test_unapproved_local_build_cannot_authorize_publication(self) -> None:
        self.payload.write_text(
            self.payload.read_text(encoding="utf-8").replace(
                self.approved_base, "0000000000000000000000000000000000000000"
            ), encoding="utf-8",
        )
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        result = self.invoke("publication-check", "215")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("approved source authority revision is unavailable", result.stderr)
        self.assert_only_issue_views(0)

    def test_environment_cannot_change_installed_base_via_launcher_or_direct_payload(self) -> None:
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        results = []
        for invoke in (self.invoke, self.invoke_payload):
            with self.subTest(entrypoint=invoke.__name__):
                result = invoke(
                    "publication-check", "215", environment={"TEMPLATES_SOURCE_BASE": "f" * 40}
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                ready = json.loads(result.stdout)
                self.assertEqual("READY", ready["status"])
                results.append(ready)
        self.assertEqual(results[0], results[1])
        self.assert_only_issue_views(2)

    def test_direct_payload_ignores_hostile_command_environment(self) -> None:
        attacker_bin = self.sandbox / "attacker-bin"
        attacker_bin.mkdir()
        sentinel = self.sandbox / "attacker-command-executed"
        for name in ("git", "gh"):
            fake = attacker_bin / name
            fake.write_text(
                f"#!/bin/sh\nprintf '%s\\n' {name} >> {shlex.quote(str(sentinel))}\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        hostile = {
            "PATH": str(attacker_bin),
            "HOME": str(self.repo),
            "GIT_SSH_COMMAND": str(attacker_bin / "git"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.https://attacker.invalid/.insteadOf",
            "GIT_CONFIG_VALUE_0": "git@github.com:",
            "TEMPLATES_SOURCE_BASE": "f" * 40,
        }
        for invoke in (self.invoke, self.invoke_payload):
            with self.subTest(entrypoint=invoke.__name__):
                result = invoke("publication-check", "215", environment=hostile)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("READY", json.loads(result.stdout)["status"])
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(2)

    def test_environment_cannot_mask_committed_authority_with_head(self) -> None:
        (self.repo / "tools" / "source_collaboration.py").write_text(
            "# unauthorized authority\n", encoding="utf-8"
        )
        git(self.repo, "add", "tools/source_collaboration.py")
        git(self.repo, "commit", "-qm", "unauthorized authority")
        current = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        for invoke in (self.invoke, self.invoke_payload):
            with self.subTest(entrypoint=invoke.__name__):
                self.assert_authority_rejected(
                    invoke("pr-create", "215", environment={"TEMPLATES_SOURCE_BASE": current}),
                    "tools/source_collaboration.py",
                )
        self.assert_only_issue_views(2)

    def test_unsubstituted_source_payload_rejects_environment_base(self) -> None:
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        result = self.invoke_payload(
            "publication-check", "215", payload=SOURCE_ROOT / "tools" / "source_collaboration.py",
            environment={"TEMPLATES_SOURCE_BASE": self.approved_base},
        )
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("approved source authority revision is unavailable", result.stderr)
        self.assert_only_issue_views(0)

    def test_unrelated_change_is_ready_and_committed_through_installed_entrypoint(self) -> None:
        (self.repo / "normal.txt").write_text("after\n", encoding="utf-8")
        checked = self.invoke("publication-check", "215")
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        ready = json.loads(checked.stdout)
        self.assertEqual("READY", ready["status"])
        self.assertEqual(["normal.txt"], [entry["path"] for entry in ready["manifest"]["entries"]])

        committed = self.invoke(
            "commit", "215", ready["scope_digest"], "test: commit ordinary fixture change"
        )

        self.assertEqual(0, committed.returncode, committed.stdout + committed.stderr)
        value = json.loads(committed.stdout)
        self.assertEqual("COMMITTED", value["status"])
        self.assertEqual(["normal.txt"], value["paths"])
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual("test: commit ordinary fixture change", git(self.repo, "log", "-1", "--format=%s"))
        self.assert_only_issue_views(2)

    def test_installed_parity_rejects_normal_generated_template_drift(self) -> None:
        (self.repo / "components" / "agent-core" / "core.txt").write_text(
            "drifted component\n", encoding="utf-8"
        )
        checked = self.invoke("publication-check", "215")
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        digest = json.loads(checked.stdout)["scope_digest"]
        result = self.invoke("commit", "215", digest, "test: reject template drift")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("template parity check failed", result.stderr)
        self.assertIn("template drift: smoke", result.stderr)
        self.assertEqual("", git(self.repo, "diff", "--cached", "--name-only"))
        self.assertEqual("fixture base", git(self.repo, "log", "-1", "--format=%s"))
        self.assert_only_issue_views(2)

    def test_live_template_justfile_cannot_bypass_installed_parity(self) -> None:
        digest = self.pending_digest()
        sentinel = self.sandbox / "template-just-sentinel"
        (self.repo / "just" / "template.just").write_text(
            "check:\n" f"    @{self.write_command_sentinel(sentinel)}\n", encoding="utf-8"
        )

        result = self.invoke("commit", "215", digest, "test: should be blocked")

        self.assert_authority_rejected(result, "just/template.just")
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(2)

    def test_live_renderer_stub_cannot_bypass_installed_parity(self) -> None:
        digest = self.pending_digest()
        sentinel = self.sandbox / "renderer-sentinel"
        stub = (
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed\\n', encoding='utf-8')\n"
            "raise SystemExit(0)\n"
        )
        (self.repo / "tools" / "render_templates.py").write_text(stub, encoding="utf-8")

        result = self.invoke("commit", "215", digest, "test: should be blocked")

        self.assert_authority_rejected(result, "tools/render_templates.py")
        self.assertFalse(sentinel.exists())
        self.assert_only_issue_views(2)


if __name__ == "__main__":
    unittest.main()
