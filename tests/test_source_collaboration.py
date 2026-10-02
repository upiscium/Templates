from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "tools" / "source_collaboration.py"
spec = importlib.util.spec_from_file_location("source_collaboration", MODULE)
assert spec and spec.loader
source = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = source
spec.loader.exec_module(source)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class SourceCollaborationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Source Collaboration Test")
        (self.root / "file.txt").write_text("one\n", encoding="utf-8")
        git(self.root, "add", "file.txt")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "branch", "-M", "main")
        git(self.root, "remote", "add", "origin", "git@github.com:upiscium/Templates.git")
        git(self.root, "checkout", "-qb", "feat/215-source-collaboration")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(self, root: Path, number: int):
        return {"number": number, "state": "OPEN", "title": "Source collaboration", "url": "x"}

    def ctx(self):
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            return source.context(215, self.root)

    def test_secret_classifier_allows_envrc(self) -> None:
        self.assertFalse(source.secret_like(".envrc"))
        self.assertFalse(source.secret_like(".env.example"))
        self.assertTrue(source.secret_like(".env"))
        self.assertTrue(source.secret_like(".env.production"))
        self.assertTrue(source.secret_like("config/client-secret.json"))

    def test_context_rejects_default_branch_and_unbound_issue(self) -> None:
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "does not bind"):
                source.context(999, self.root)
        git(self.root, "checkout", "-q", "main")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "default-branch"):
                source.context(215, self.root)

    def test_scope_digest_detects_race(self) -> None:
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        value = source.manifest(self.ctx())
        expected = source.digest(value)
        (self.root / "file.txt").write_text("three\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ):
            with self.assertRaisesRegex(source.GuardError, "source scope changed"):
                source.commit(215, expected, "test: source guard", self.root)
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))

    def test_commit_is_exact_and_accepts_envrc(self) -> None:
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        (self.root / ".envrc").write_text("use flake\n", encoding="utf-8")
        value = source.manifest(self.ctx())
        expected = source.digest(value)
        old = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ):
            result = source.commit(215, expected, "test: source guard", self.root)
        self.assertEqual("COMMITTED", result["status"])
        self.assertNotEqual(old, result["head"])
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_push_refuses_dirty_worktree(self) -> None:
        (self.root / "file.txt").write_text("dirty\n", encoding="utf-8")
        current = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "must be clean"):
                source.push(215, current, self.root)

    def test_pr_adoption_requires_exact_draft(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        pr = {
            "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
            "body": "Closes #215",
            "head": {"ref": "feat/215-source-collaboration", "sha": current,
                     "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[pr]):
            result = source.pr_create(215, self.root)
        self.assertEqual("ADOPTED", result["status"])
        self.assertEqual(7, result["pr"])

    def test_pr_adoption_rejects_wrong_issue_binding(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        pr = {
            "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
            "body": "Closes #999",
            "head": {"ref": "feat/215-source-collaboration", "sha": current,
                     "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[pr]):
            with self.assertRaisesRegex(source.GuardError, "not bound to Issue #215"):
                source.pr_create(215, self.root)

    def test_push_recovers_lost_acknowledgement(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        ctx = {
            "root": self.root, "issue": 215, "branch": "feat/215-source-collaboration",
            "head": current, "title": "Source collaboration",
        }
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        with mock.patch.object(source, "context", return_value=ctx), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "remote_head", side_effect=[None, current]), mock.patch.object(
            source, "git", return_value=failed
        ):
            result = source.push(215, current, self.root)
        self.assertEqual("PUSHED", result["status"])

    def test_checkpoint_recovers_lost_acknowledgement(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        ctx = {
            "root": self.root, "issue": 215, "branch": "feat/215-source-collaboration",
            "head": current, "title": "Source collaboration",
        }
        pr = {
            "number": 7, "state": "open", "draft": True, "body": "Closes #215",
            "head": {"ref": ctx["branch"], "sha": current, "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        body = "checkpoint"
        marker = (
            f"<!-- source-checkpoint:215:{current}:"
            f"{source.hashlib.sha256(body.encode()).hexdigest()} -->"
        )
        api_values = iter([pr, [], [{"id": 88, "body": marker + "\ncheckpoint"}]])
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        with mock.patch.object(source, "context", return_value=ctx), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "remote_head", return_value=current), mock.patch.object(
            source, "gh_json", side_effect=lambda *args: next(api_values)
        ), mock.patch.object(source, "run", return_value=failed):
            result = source.checkpoint(215, 7, current, body, self.root)
        self.assertEqual("POSTED", result["status"])
        self.assertEqual(88, result["comment_id"])


if __name__ == "__main__":
    unittest.main()
