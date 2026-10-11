"""Non-invasive KAGARI external-bootstrap contract tests.

These tests use disposable Git roots, never users' repositories. They do not
claim that the v3 Agent Core runtime already works from .kagari/runtime.
"""
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
CLI = ROOT / "tools" / "kagari_bootstrap.py"
PAYLOAD = ROOT / "components" / "agent-core"


def call_git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    out = subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return out.stdout.strip()


class KagariBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="kagari-bootstrap-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "product"
        self.root.mkdir()
        call_git(self.root, "init", "-q")
        call_git(self.root, "config", "user.name", "KAGARI Bootstrap Test")
        call_git(self.root, "config", "user.email", "test@example.invalid")
        self.baseline = {
            "README.md": "# product\n",
            "Justfile": 'set minimum-version := "1.55.0"\nexample:\n    @echo owned-by-project\n',
            "flake.nix": "{ definitely-invalid-flake syntax = *;\n",
            "opencode.json": '{"default_agent":"plan","user_setting":true}\n',
            "AGENTS.md": "Project-owned agent instructions.\n",
        }
        for name, content in self.baseline.items():
            (self.root / name).write_text(content)
        call_git(self.root, "add", "-A")
        call_git(self.root, "commit", "-qm", "initial product")
        call_git(self.root, "branch", "-M", "main")
        self.first_head = call_git(self.root, "rev-parse", "HEAD")

    def invoke(self, action: str, *, source: Path | None = PAYLOAD,
               target: Path | None = None) -> tuple[int, dict]:
        command = [sys.executable, str(CLI), action,
                   "--target", str(target or self.root)]
        if source is not None:
            command.extend(["--source", str(source)])
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        text = result.stdout if result.returncode == 0 else result.stderr
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            self.fail(f"{command}: {result.returncode}: {result.stdout}\n{result.stderr}")
            raise exc
        return result.returncode, parsed

    def assert_project_preserved(self) -> None:
        for name, expected in self.baseline.items():
            self.assertEqual(expected, (self.root / name).read_text(),
                             msg=f"unexpected Project mutation: {name}")
        self.assertEqual(self.first_head, call_git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("", call_git(self.root, "diff", "--cached", "--name-only"))
        self.assertEqual("", call_git(self.root, "diff", "--name-only"))
        # An optional .kagari-owned area is the only permitted product status.
        dirty_paths = call_git(self.root, "status", "--porcelain", "--untracked-files=all")
        for row in dirty_paths.splitlines():
            self.assertTrue(row[3:].startswith(".kagari/") or row[3:] == ".kagari", msg=row)

    def test_install_uninstall_reinstall_is_noninvasive(self) -> None:
        rc, initial = self.invoke("plan")
        self.assertEqual((0, "ABSENT"), (rc, initial["status"]))
        rc, installed = self.invoke("install")
        self.assertEqual((0, "INSTALLED"), (rc, installed["status"]))
        receipt = json.loads((self.root / ".kagari" / "install.json").read_text())
        self.assertEqual(("KAGARI", "3"), (receipt["component"], receipt["version"]))
        self.assertGreaterEqual(len(receipt["files"]), 40)
        self.assertTrue((self.root / ".kagari" / "runtime" / ".automation" / "VERSION").exists())
        self.assert_project_preserved()
        self.assertEqual("HEALTHY", self.invoke("doctor", source=None)[1]["status"])
        self.assertEqual("UNCHANGED", self.invoke("install")[1]["status"])
        self.assert_project_preserved()

        self.assertEqual("UNINSTALLED", self.invoke("uninstall")[1]["status"])
        self.assertFalse((self.root / ".kagari").exists())
        self.assertEqual("", call_git(self.root, "status", "--porcelain"))
        self.assert_project_preserved()
        self.assertEqual("ALREADY_ABSENT", self.invoke("uninstall")[1]["status"])
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        self.assert_project_preserved()

    def test_partial_missing_payload_repairs_without_overwrite(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        managed = self.root / ".kagari" / "runtime" / "Justfile"
        original = managed.read_bytes()
        managed.unlink()
        rc, state = self.invoke("doctor", source=None)
        self.assertEqual((0, "REPAIRABLE"), (rc, state["status"]))
        self.assertIn("Justfile", state["missing"])
        self.assertEqual("REPAIRED", self.invoke("repair")[1]["status"])
        self.assertEqual(original, managed.read_bytes())
        self.assertEqual("HEALTHY", self.invoke("doctor", source=None)[1]["status"])
        self.assert_project_preserved()

    def test_uninstall_after_missing_managed_payload_is_recoverable(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        container = self.root / ".kagari" / "runtime"
        missing_leaf = container / ".automation" / "VERSION"
        missing_leaf.unlink()
        self.assertEqual("REPAIRABLE", self.invoke("doctor", source=None)[1]["status"])
        self.assertEqual("UNINSTALLED", self.invoke("uninstall")[1]["status"])
        self.assertEqual("", call_git(self.root, "status", "--porcelain"))
        self.assert_project_preserved()
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])

    def test_empty_unknown_directory_blocks_uninstall_before_deletion(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        sentinel = self.root / ".kagari" / "runtime" / "Justfile"
        expected_bytes = sentinel.read_bytes()
        extra = self.root / ".kagari" / "runtime" / "my-private-dir"
        extra.mkdir()
        diagnosis = self.invoke("doctor", source=None)[1]
        self.assertEqual("CONFLICT", diagnosis["status"])
        self.assertIn("runtime/my-private-dir/", diagnosis["unknown"])
        code, state = self.invoke("uninstall")
        self.assertEqual((2, "BLOCKED"), (code, state["status"]))
        self.assertTrue(extra.is_dir())
        self.assertEqual(expected_bytes, sentinel.read_bytes())
        self.assert_project_preserved()

    def test_modified_managed_payload_is_ambiguous_and_preserved(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        managed = self.root / ".kagari" / "runtime" / "Justfile"
        edited = b"User intentionally customized KAGARI's copy\n"
        managed.write_bytes(edited)
        self.assertEqual("CONFLICT", self.invoke("doctor", source=None)[1]["status"])
        for command in ("install", "repair", "uninstall"):
            rc, result = self.invoke(command, source=PAYLOAD)
            self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
            self.assertEqual(edited, managed.read_bytes())
            self.assert_project_preserved()

    def test_unknown_files_in_managed_scope_block_deletion(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        unknown = self.root / ".kagari" / "runtime" / "custom-code.txt"
        unknown.write_text("important user notes")
        state = self.invoke("plan")[1]
        self.assertEqual("CONFLICT", state["status"])
        self.assertIn("runtime/custom-code.txt", state["unknown"])
        self.assertEqual((2, "BLOCKED"), (
            lambda x: (x[0], x[1]["status"])
        )(self.invoke("uninstall")))
        self.assertTrue(unknown.exists())
        self.assert_project_preserved()

    def test_corrupt_or_missing_receipt_never_adopts_unknown_directory(self) -> None:
        owned = self.root / ".kagari"
        owned.mkdir()
        (owned / "custom.txt").write_text("not KAGARI owned")
        rc, result = self.invoke("install")
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertTrue((owned / "custom.txt").exists())
        self.assert_project_preserved()

    def test_symlink_managed_directory_refused(self) -> None:
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        (outside / "untouched.txt").write_text("safe\n")
        (self.root / ".kagari").symlink_to(outside, target_is_directory=True)
        self.assertEqual((2, "BLOCKED"), (
            lambda x: (x[0], x[1]["status"])
        )(self.invoke("install")))
        self.assertEqual("safe\n", (outside / "untouched.txt").read_text())
        self.assertFalse((outside / "runtime").exists())
        self.assert_project_preserved()

    def test_dirty_product_index_and_untracked_work_are_preserved(self) -> None:
        dirty = self.root / "README.md"
        dirty.write_text("# changed product work\\n")
        call_git(self.root, "add", "README.md")
        untracked = self.root / "unsaved-product.txt"
        untracked.write_text("user content\\n")
        before_index = call_git(self.root, "diff", "--cached", "--binary")
        before_head = call_git(self.root, "rev-parse", "HEAD")

        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        self.assertEqual("UNCHANGED", self.invoke("install")[1]["status"])
        self.assertEqual("UNINSTALLED", self.invoke("uninstall")[1]["status"])
        self.assertEqual(before_head, call_git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(before_index, call_git(self.root, "diff", "--cached", "--binary"))
        self.assertEqual("# changed product work\\n", dirty.read_text())
        self.assertEqual("user content\\n", untracked.read_text())

    def test_install_runs_without_project_just_or_nix_executable(self) -> None:
        git_binary = shutil.which("git")
        self.assertIsNotNone(git_binary)
        # Bootstrap depends only on the external Python interpreter and Git,
        # not on just/nix from the possibly broken Project devShell.
        env = os.environ.copy()
        env["PATH"] = str(Path(git_binary).resolve().parent)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        result = subprocess.run([
            sys.executable, str(CLI), "install",
            "--target", str(self.root), "--source", str(PAYLOAD),
        ], capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("INSTALLED", json.loads(result.stdout)["status"])
        self.assertEqual("HEALTHY", self.invoke("doctor", source=None)[1]["status"])
        self.assert_project_preserved()

    def test_nonexecutable_mode_drift_blocks_uninstall(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        file = self.root / ".kagari" / "runtime" / "AGENTS.md"
        old_bytes = file.read_bytes()
        file.chmod(0o600)
        diagnosis = self.invoke("doctor", source=None)[1]
        self.assertEqual("CONFLICT", diagnosis["status"])
        self.assertIn("AGENTS.md", diagnosis["changed"])
        rc, result = self.invoke("uninstall")
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertEqual(0o600, file.stat().st_mode & 0o777)
        self.assertEqual(old_bytes, file.read_bytes())
        self.assert_project_preserved()

    def test_git_backed_source_ls_files_failure_does_not_scan_untracked_files(self) -> None:
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("kagari_git_scan_test", CLI)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        actual_run = bootstrap.subprocess.run

        def failed_inventory(cmd, *args, **kwargs):
            if "ls-files" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout=b"",
                                                   stderr=b"simulated index error")
            return actual_run(cmd, *args, **kwargs)

        with mock.patch.object(bootstrap.subprocess, "run", side_effect=failed_inventory):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "inventory is unreadable"):
                bootstrap.source_payload(PAYLOAD)
        self.assert_project_preserved()

    def test_extracted_nongit_source_uses_archive_inventory(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kagari_archive_source", CLI)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        archive = Path(self.temp.name) / "archive"
        shutil.copytree(
            PAYLOAD, archive,
            ignore=shutil.ignore_patterns("node_modules", "__pycache__",
                                          ".git", ".pytest_cache", "*.pyc")
        )
        receipt, files = bootstrap.source_payload(archive)
        self.assertEqual("3", receipt["version"])
        self.assertIn(".automation/VERSION", files)
        self.assertNotIn(".opencode/package.json", files)
        self.assert_project_preserved()

    def test_file_mode_drift_does_not_overwrite_user_edits(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        file = self.root / ".kagari" / "runtime" / "AGENTS.md"
        file.chmod(0o755)
        self.assertEqual("CONFLICT", self.invoke("doctor", source=None)[1]["status"])
        code, result = self.invoke("repair")
        self.assertEqual((2, "BLOCKED"), (code, result["status"]))
        self.assertEqual(0o755, file.stat().st_mode & 0o777)
        self.assert_project_preserved()

    def test_unreadable_managed_directory_blocks_uninstall_before_unlink(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        critical = self.root / ".kagari" / "runtime" / "Justfile"
        original = critical.read_bytes()
        hidden = self.root / ".kagari" / "runtime" / ".automation"
        extra = hidden / "unrecorded-notes.txt"
        extra.write_text("important data")
        hidden.chmod(0o300)
        try:
            rc, response = self.invoke("uninstall")
            self.assertEqual((2, "BLOCKED"), (rc, response["status"]))
            self.assertEqual(original, critical.read_bytes())
        finally:
            hidden.chmod(0o755)
        self.assertEqual("important data", extra.read_text())
        self.assert_project_preserved()

    def test_foreign_canonical_receipt_requires_external_source_match(self) -> None:
        container = self.root / ".kagari"
        (container / "runtime").mkdir(parents=True)
        personal = container / "runtime" / "my-data.txt"
        personal.write_bytes(b"Project-owned content")
        import hashlib
        record = {"schema": 1, "component": "KAGARI", "version": "3",
                  "files": [{"path": "my-data.txt", "mode": 0o644, "size": 21,
                             "sha256": hashlib.sha256(personal.read_bytes()).hexdigest()}]}
        (container / "install.json").write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        rc, result = self.invoke("uninstall")
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertEqual(b"Project-owned content", personal.read_bytes())
        self.assert_project_preserved()

    def test_uninstall_does_not_run_without_external_matching_source(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        rc, result = self.invoke("uninstall", source=None)
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertEqual("HEALTHY", self.invoke("doctor", source=None)[1]["status"])
        self.assert_project_preserved()

    def test_repair_failure_does_not_publish_partial_installed_file(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        managed = self.root / ".kagari" / "runtime" / "Justfile"
        managed.unlink()
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("kagari_bootstrap_under_test", CLI)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        with mock.patch.object(bootstrap.os, "link", side_effect=OSError("simulated IO failure")):
            with self.assertRaisesRegex(OSError, "simulated IO failure"):
                bootstrap.operate(self.root, PAYLOAD, "repair")
        self.assertFalse(managed.exists())
        self.assertEqual("REPAIRABLE", self.invoke("doctor", source=None)[1]["status"])
        self.assertEqual("REPAIRED", self.invoke("repair")[1]["status"])
        self.assert_project_preserved()

    def test_non_git_target_is_rejected_without_changes(self) -> None:
        other = Path(self.temp.name) / "not-git"
        other.mkdir()
        rc, value = self.invoke("install", target=other)
        self.assertEqual((2, "BLOCKED"), (rc, value["status"]))
        self.assertEqual([], list(other.iterdir()))
        self.assert_project_preserved()

    def test_source_version_conflict_refuses_automatic_upgrade(self) -> None:
        self.assertEqual("INSTALLED", self.invoke("install")[1]["status"])
        custom = Path(self.temp.name) / "different-source"
        shutil.copytree(PAYLOAD, custom)
        (custom / ".automation" / "VERSION").write_text("4\n")
        rc, result = self.invoke("install", source=custom)
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertEqual("3\n",
                         (self.root / ".kagari" / "runtime" / ".automation" / "VERSION").read_text())
        self.assert_project_preserved()

    def test_source_symlink_rejected_before_installation(self) -> None:
        custom = Path(self.temp.name) / "symlinked-source"
        shutil.copytree(PAYLOAD, custom)
        (custom / ".automation" / "VERSION").unlink()
        (custom / ".automation" / "VERSION").symlink_to(PAYLOAD / ".automation" / "VERSION")
        rc, result = self.invoke("install", source=custom)
        self.assertEqual((2, "BLOCKED"), (rc, result["status"]))
        self.assertFalse((self.root / ".kagari").exists())
        self.assert_project_preserved()


if __name__ == "__main__":
    unittest.main()
