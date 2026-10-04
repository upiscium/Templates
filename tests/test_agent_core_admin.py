from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "agent_core_admin.py"
SPEC = importlib.util.spec_from_file_location("agent_core_admin", SCRIPT)
assert SPEC and SPEC.loader
admin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin
SPEC.loader.exec_module(admin)


def command(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class FakeIdentity:
    class AdminIdentityError(RuntimeError):
        pass

    def __init__(self) -> None:
        self.binding_revision = "stable"

    def observe_target(
        self,
        root: Path,
        *,
        expected_repository: str,
        expected_branch: str,
        expected_head: str,
    ) -> SimpleNamespace:
        actual_root = Path(command("git", "rev-parse", "--show-toplevel", cwd=root)).resolve()
        branch = command("git", "branch", "--show-current", cwd=root)
        head = command("git", "rev-parse", "HEAD", cwd=root)
        if actual_root != root.resolve():
            raise self.AdminIdentityError("target is not an exact Git root")
        if expected_repository != "acme/widgets" or expected_branch != branch or expected_head != head:
            raise self.AdminIdentityError("expected Git identity mismatch")
        git_dir = Path(command("git", "rev-parse", "--absolute-git-dir", cwd=root)).resolve()
        common = Path(command("git", "rev-parse", "--git-common-dir", cwd=root))
        if not common.is_absolute():
            common = (root / common).resolve()
        return SimpleNamespace(
            root=root.resolve(),
            git_dir=git_dir,
            common_dir=common.resolve(),
            branch=branch,
            head=head,
            repository="acme/widgets",
        )

    def verify_cutover_binding(
        self,
        facts: object,
        *,
        issue: int,
        pr: int,
        expected_base: str,
        task_binding,
    ) -> dict:
        if expected_base != "main" or task_binding(facts, issue, pr) is not True:
            raise self.AdminIdentityError("cutover binding mismatch")
        return {
            "issue": issue,
            "pr": pr,
            "base": expected_base,
            "binding_revision": self.binding_revision,
        }


class AgentCoreAdminTest(unittest.TestCase):
    def repository(self, root: Path, *, branch: str = "bootstrap/agent-core") -> tuple[Path, str]:
        repo = root / "consumer"
        repo.mkdir()
        command("git", "init", "-b", branch, cwd=repo)
        command("git", "config", "user.name", "Agent Core Admin Test", cwd=repo)
        command("git", "config", "user.email", "admin-test@example.invalid", cwd=repo)
        command("git", "remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=repo)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        (repo / "product.txt").write_text("product\n", encoding="utf-8")
        command("git", "add", "seed.txt", "product.txt", cwd=repo)
        command("git", "commit", "-m", "seed", cwd=repo)
        return repo.resolve(), command("git", "rev-parse", "HEAD", cwd=repo)

    def payload(
        self,
        root: Path,
        revision: str,
        files: dict[str, tuple[bytes, int]] | None = None,
    ) -> tuple[Path, str, dict[str, tuple[bytes, int]]]:
        contents = files or {
            ".automation/VERSION": (b"1.0.0\n", 0o644),
            ".automation/bin/admin.py": (b"admin payload\n", 0o644),
            "AGENTS.md": (b"managed instructions\n", 0o644),
        }
        directory = root / f"payload-{len(list(root.glob('payload-*'))) + 1}"
        directory.mkdir()
        entries = []
        for relative, (data, mode) in sorted(contents.items()):
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(mode)
            entries.append(
                {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
            )
        manifest = {"version": "1.0.0", "source_revision": revision, "files": entries}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        (directory / "manifest.json").write_bytes(encoded)
        (directory / "manifest.json").chmod(0o644)
        return directory, hashlib.sha256(encoded).hexdigest(), contents

    def kwargs(
        self,
        repo: Path,
        head: str,
        payload: tuple[Path, str, dict[str, tuple[bytes, int]]] | None,
        *,
        operation: str = "install",
    ) -> dict:
        result = {
            "expected_repository": "acme/widgets",
            "expected_branch": command("git", "branch", "--show-current", cwd=repo),
            "expected_head": head,
        }
        if operation != "uninstall":
            if payload is None:
                raise AssertionError("payload required")
            directory, digest, _ = payload
            result.update(
                {
                    "payload_directory": directory,
                    "expected_manifest_sha256": digest,
                    "expected_source_revision": head,
                }
            )
        return result

    def invoke(self, identity: FakeIdentity, function, *args, **kwargs):
        with mock.patch.object(admin, "_identity_api", return_value=identity):
            return function(*args, **kwargs)

    def git_index_bytes(self, repo: Path) -> bytes:
        index = Path(command("git", "rev-parse", "--git-path", "index", cwd=repo))
        if not index.is_absolute():
            index = repo / index
        return index.read_bytes()

    def dirty_product_state(self, repo: Path) -> dict[str, bytes | str]:
        staged = b"staged product bytes\n"
        working = b"unstaged product bytes\n"
        untracked = b"untracked product note\n"
        (repo / "product.txt").write_bytes(staged)
        command("git", "add", "product.txt", cwd=repo)
        (repo / "product.txt").write_bytes(working)
        (repo / "product-note.txt").write_bytes(untracked)
        state: dict[str, bytes | str] = {
            "product": (repo / "product.txt").read_bytes(),
            "untracked": (repo / "product-note.txt").read_bytes(),
            "head": command("git", "rev-parse", "HEAD", cwd=repo),
            "history": command("git", "log", "--format=%H", "--all", cwd=repo),
            "cached_diff": command(
                "git", "diff", "--cached", "--binary", "--", "product.txt", cwd=repo
            ),
            "working_diff": command("git", "diff", "--binary", "--", "product.txt", cwd=repo),
            "index": self.git_index_bytes(repo),
        }
        if not state["cached_diff"] or not state["working_diff"]:
            raise AssertionError("product fixture must contain staged and unstaged changes")
        return state

    def assert_dirty_product_state(self, repo: Path, expected: dict[str, bytes | str]) -> None:
        actual: dict[str, bytes | str] = {
            "product": (repo / "product.txt").read_bytes(),
            "untracked": (repo / "product-note.txt").read_bytes(),
            "head": command("git", "rev-parse", "HEAD", cwd=repo),
            "history": command("git", "log", "--format=%H", "--all", cwd=repo),
            "cached_diff": command(
                "git", "diff", "--cached", "--binary", "--", "product.txt", cwd=repo
            ),
            "working_diff": command("git", "diff", "--binary", "--", "product.txt", cwd=repo),
            "index": self.git_index_bytes(repo),
        }
        self.assertEqual(expected, actual)

    def assert_admin_and_git_writer_locks_held(self, repo: Path) -> None:
        private = repo / ".git" / admin.PRIVATE_DIRECTORY
        self.assertTrue((repo / ".git" / "index.lock").is_file())
        self.assertTrue((repo / ".git" / "HEAD.lock").is_file())
        descriptor = admin.os.open(private / "lock", admin.os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            admin.os.close(descriptor)

    def git_dir(self, repo: Path) -> Path:
        return Path(command("git", "rev-parse", "--absolute-git-dir", cwd=repo)).resolve()

    def assert_admin_lock_available(self, repo: Path) -> None:
        lock = self.git_dir(repo) / admin.PRIVATE_DIRECTORY / "lock"
        if not lock.exists():
            return
        descriptor = admin.os.open(lock, admin.os.O_RDWR)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.fail(f"Agent Core Admin lock remains held: {lock}")
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            admin.os.close(descriptor)

    def assert_no_git_writer_guards(self, repo: Path) -> None:
        git_dir = self.git_dir(repo)
        self.assertFalse((git_dir / "index.lock").exists())
        self.assertFalse((git_dir / "HEAD.lock").exists())

    def assert_no_writer_locks(self, repo: Path) -> None:
        self.assert_no_git_writer_guards(repo)
        self.assert_admin_lock_available(repo)

    def test_install_receipt_is_minimal_and_same_operation_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            payload = self.payload(root, head)
            identity = FakeIdentity()
            args = self.kwargs(repo, head, payload)
            planned = self.invoke(identity, admin.inspect, "install", repo, **args)
            self.assertEqual("READY", planned["status"])

            result = self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **args,
            )
            repeated = self.invoke(
                identity,
                admin.retry,
                "install",
                repo,
                expected_operation_id=result["operationId"],
                caller_authorizer=lambda _facts: True,
                **args,
            )

            self.assertEqual("APPLIED", result["status"])
            self.assertEqual("ALREADY_APPLIED", repeated["status"])
            self.assertEqual([], repeated["changedPaths"])
            private = repo / ".git" / admin.PRIVATE_DIRECTORY
            intent_path = private / "operations" / "intents" / f"{result['operationId']}.json"
            receipt_path = private / "operations" / "receipts" / f"{result['operationId']}.json"
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "format", "operation_id", "target", "request", "payload",
                    "prior_generation", "before", "after", "temporary_paths",
                },
                set(intent),
            )
            self.assertEqual({"format", "operation_id", "status"}, set(receipt))
            self.assertEqual("complete", receipt["status"])
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))
            self.assertEqual(b"product\n", (repo / "product.txt").read_bytes())

    def test_replace_uses_exact_preimage_and_atomic_file_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            first = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, first),
            )
            second = self.payload(
                root,
                head,
                {
                    ".automation/VERSION": (b"1.0.0\n", 0o644),
                    ".automation/bin/admin.py": (b"replacement\n", 0o755),
                    "AGENTS.md": (b"new instructions\n", 0o644),
                },
            )
            real_replace = admin.os.replace
            replaced_leafs: list[str] = []

            def observe_replace(source, destination, *args, **kwargs):
                if destination == "AGENTS.md":
                    replaced_leafs.append(destination)
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(admin.os, "replace", side_effect=observe_replace):
                result = self.invoke(
                    identity,
                    admin.apply,
                    "replace",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, second, operation="replace"),
                )

            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(["AGENTS.md"], replaced_leafs)
            self.assertEqual(b"new instructions\n", (repo / "AGENTS.md").read_bytes())
            self.assertEqual(0o755, stat.S_IMODE((repo / ".automation/bin/admin.py").stat().st_mode))
            self.assertFalse((repo / ".automation/bin/admin.py").read_bytes() == b"admin payload\n")
            private = repo / ".git" / admin.PRIVATE_DIRECTORY
            self.assertEqual(
                {"lock", "operations", "installed.json"},
                {entry.name for entry in private.iterdir()},
            )

    def test_unknown_managed_overlap_fails_before_mutation_and_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            unknown = b"consumer-owned instructions\n"
            (repo / "AGENTS.md").write_bytes(unknown)
            payload = self.payload(root, head)
            identity = FakeIdentity()

            with self.assertRaisesRegex(admin.AdminError, "pre-existing Agent Core-managed files"):
                self.invoke(
                    identity,
                    admin.apply,
                    "install",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, payload),
                )

            self.assertEqual(unknown, (repo / "AGENTS.md").read_bytes())
            self.assertFalse((repo / ".automation").exists())
            private = repo / ".git" / admin.PRIVATE_DIRECTORY
            self.assertFalse((private / "installed.json").exists())
            self.assertEqual([], list((private / "operations" / "intents").iterdir()))
            self.assertEqual([], list((private / "operations" / "receipts").iterdir()))
            self.assert_no_writer_locks(repo)
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))

    def test_dirty_known_preimage_fails_replace_without_changing_product_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            first = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, first),
            )
            product = b"unsaved product bytes\n"
            (repo / "product.txt").write_bytes(product)
            unknown = b"user-edited managed file\n"
            (repo / "AGENTS.md").write_bytes(unknown)
            second = self.payload(
                root,
                head,
                {
                    ".automation/VERSION": (b"1.0.0\n", 0o644),
                    ".automation/bin/admin.py": (b"replacement\n", 0o644),
                    "AGENTS.md": (b"replacement instructions\n", 0o644),
                },
            )

            with self.assertRaisesRegex(admin.AdminError, "prior installed identity"):
                self.invoke(
                    identity,
                    admin.apply,
                    "replace",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, second, operation="replace"),
                )

            self.assertEqual(unknown, (repo / "AGENTS.md").read_bytes())
            self.assertEqual(product, (repo / "product.txt").read_bytes())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))

    def test_uninstall_unlinks_only_exact_files_and_retry_accepts_its_postimage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            payload = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, payload),
            )
            args = self.kwargs(repo, head, None, operation="uninstall")
            result = self.invoke(
                identity,
                admin.apply,
                "uninstall",
                repo,
                caller_authorizer=lambda _facts: True,
                **args,
            )
            repeated = self.invoke(
                identity,
                admin.retry,
                "uninstall",
                repo,
                expected_operation_id=result["operationId"],
                caller_authorizer=lambda _facts: True,
                **args,
            )

            self.assertEqual("APPLIED", result["status"])
            self.assertEqual("ALREADY_APPLIED", repeated["status"])
            for relative in payload[2]:
                self.assertFalse((repo / relative).exists())
            self.assertTrue((repo / ".automation").is_dir())
            self.assertEqual(b"product\n", (repo / "product.txt").read_bytes())

    def test_legacy_cutover_requires_exact_inventory_and_retries_multi_file_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            command("git", "checkout", "-b", "task/142-admin", cwd=repo)
            (repo / ".automation").mkdir()
            (repo / ".automation/VERSION").write_bytes(b"3\n")
            (repo / ".automation/old.py").write_bytes(b"legacy\n")
            inventory = {
                ".automation/VERSION": {"sha256": hashlib.sha256(b"3\n").hexdigest(), "mode": 0o644},
                ".automation/old.py": {"sha256": hashlib.sha256(b"legacy\n").hexdigest(), "mode": 0o644},
            }
            payload = self.payload(root, head)
            args = self.kwargs(repo, head, payload)
            args["legacy_inventory"] = inventory
            identity = FakeIdentity()
            binding = {
                "issue": 142,
                "pr": 217,
                "expected_base": "main",
                "task_binding": lambda _facts, issue, pr: issue == 142 and pr == 217,
                "metadata_binding": lambda _facts, issue, pr: issue == 142 and pr == 217,
            }
            planned = self.invoke(identity, admin.inspect, "cutover", repo, **binding, **args)
            original = admin._apply_one_path
            calls = 0

            def interrupt_after_one(request, intent, path, layout, root_device, admin_fs_api):
                nonlocal calls
                result = original(request, intent, path, layout, root_device, admin_fs_api)
                calls += 1
                if calls == 1:
                    raise RuntimeError("injected interruption")
                return result

            with mock.patch.object(admin, "_apply_one_path", side_effect=interrupt_after_one):
                with self.assertRaisesRegex(RuntimeError, "phase two stopped: injected interruption"):
                    self.invoke(
                        identity,
                        admin.apply,
                        "cutover",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **binding,
                        **args,
                    )
            private = repo / ".git" / admin.PRIVATE_DIRECTORY
            self.assert_no_writer_locks(repo)
            self.assertTrue(
                (private / "operations" / "intents" / f"{planned['operationId']}.json").is_file()
            )
            self.assertFalse(
                (private / "operations" / "receipts" / f"{planned['operationId']}.json").exists()
            )
            resumed = self.invoke(
                identity,
                admin.retry,
                "cutover",
                repo,
                expected_operation_id=planned["operationId"],
                caller_authorizer=lambda _facts: True,
                **binding,
                **args,
            )

            self.assertEqual("APPLIED", resumed["status"])
            self.assertFalse((repo / ".automation/old.py").exists())
            self.assertEqual(b"1.0.0\n", (repo / ".automation/VERSION").read_bytes())
            self.assertEqual(b"admin payload\n", (repo / ".automation/bin/admin.py").read_bytes())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))
            receipt = json.loads(
                (private / "operations" / "receipts" / f"{planned['operationId']}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("complete", receipt["status"])
            self.assert_no_writer_locks(repo)

    def test_admin_owned_partial_payload_prefix_is_completed_only_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            payload = self.payload(root, head)
            identity = FakeIdentity()
            args = self.kwargs(repo, head, payload)
            planned = self.invoke(identity, admin.inspect, "install", repo, **args)
            temporary_path = repo / admin._temporary_path(planned["operationId"], "AGENTS.md")
            real_write = admin.os.write
            interrupted = False

            def interrupt_stage_write(descriptor, content):
                nonlocal interrupted
                try:
                    opened_path = Path(admin.os.readlink(f"/proc/self/fd/{descriptor}"))
                except OSError:
                    opened_path = None
                if opened_path == temporary_path and not interrupted:
                    block = bytes(content)
                    real_write(descriptor, block[: max(1, len(block) // 2)])
                    interrupted = True
                    raise KeyboardInterrupt("partial payload stage")
                return real_write(descriptor, content)

            with mock.patch.object(admin.os, "write", side_effect=interrupt_stage_write):
                with self.assertRaisesRegex(KeyboardInterrupt, "partial payload stage"):
                    self.invoke(
                        identity,
                        admin.apply,
                        "install",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **args,
                    )
            prefix = temporary_path.read_bytes()
            self.assertTrue(payload[2]["AGENTS.md"][0].startswith(prefix))
            self.assertEqual(0o600, stat.S_IMODE(temporary_path.stat().st_mode))

            result = self.invoke(
                identity,
                admin.retry,
                "install",
                repo,
                expected_operation_id=planned["operationId"],
                caller_authorizer=lambda _facts: True,
                **args,
            )
            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(payload[2]["AGENTS.md"][0], (repo / "AGENTS.md").read_bytes())
            self.assertFalse(temporary_path.exists())

    def test_unexpected_empty_managed_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            unexpected = repo / ".automation" / "consumer-cache"
            unexpected.mkdir(parents=True)
            payload = self.payload(root, head)

            with self.assertRaisesRegex(admin.AdminError, "directory entry set"):
                self.invoke(
                    FakeIdentity(),
                    admin.apply,
                    "install",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, payload),
                )

            self.assertTrue(unexpected.is_dir())
            self.assertFalse((repo / "AGENTS.md").exists())

    def test_git_lock_collisions_are_refused_without_touching_the_colliding_lock(self) -> None:
        for name in ("index.lock", "HEAD.lock"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, head = self.repository(root)
                payload = self.payload(root, head)
                lock = repo / ".git" / name
                original = b"another Git writer owns this lock\n"
                lock.write_bytes(original)

                with self.assertRaisesRegex(admin.AdminError, "Git writer lock collision"):
                    self.invoke(
                        FakeIdentity(),
                        admin.apply,
                        "install",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **self.kwargs(repo, head, payload),
                    )

                self.assertEqual(original, lock.read_bytes())
                self.assertFalse((repo / "AGENTS.md").exists())
                other_lock = "HEAD.lock" if name == "index.lock" else "index.lock"
                self.assertFalse((repo / ".git" / other_lock).exists())
                self.assert_admin_lock_available(repo)

    def test_busy_admin_lock_never_acquires_git_writer_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            original = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, original),
            )
            replacement = self.payload(
                root,
                head,
                {
                    ".automation/VERSION": (b"1.0.0\n", 0o644),
                    ".automation/bin/admin.py": (b"replacement\n", 0o644),
                    "AGENTS.md": (b"replacement instructions\n", 0o644),
                },
            )
            lock = self.git_dir(repo) / admin.PRIVATE_DIRECTORY / "lock"
            descriptor = admin.os.open(lock, admin.os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch.object(
                    admin, "_git_writer_fence", wraps=admin._git_writer_fence
                ) as writer_fence:
                    with self.assertRaisesRegex(admin.AdminError, "BUSY: another Agent Core Admin"):
                        self.invoke(
                            identity,
                            admin.apply,
                            "replace",
                            repo,
                            caller_authorizer=lambda _facts: True,
                            **self.kwargs(repo, head, replacement, operation="replace"),
                        )
                    writer_fence.assert_not_called()
                self.assert_no_git_writer_guards(repo)
                self.assertEqual(b"managed instructions\n", (repo / "AGENTS.md").read_bytes())
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                admin.os.close(descriptor)
            self.assert_admin_lock_available(repo)

    def test_first_admin_lock_does_not_invalidate_private_history_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            facts = FakeIdentity().observe_target(
                repo, expected_repository="acme/widgets",
                expected_branch="bootstrap/agent-core", expected_head=head,
            )
            layout = admin._private_layout(facts, repo.stat().st_dev, create=True)
            self.assertIsNotNone(layout)
            admin._ensure_admin_lock_file(layout, repo.stat().st_dev)
            before = admin._operation_directory_identity(layout, repo.stat().st_dev)
            with admin._worktree_lock(layout, repo.stat().st_dev):
                self.assertEqual(before, admin._operation_directory_identity(layout, repo.stat().st_dev))

    def test_unexpected_private_entry_before_lock_blocks_mutation(self) -> None:
        for location in ("base", "operations"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, head = self.repository(root)
                payload = self.payload(root, head)
                original_lock = admin._worktree_lock
                base = repo / ".git" / admin.PRIVATE_DIRECTORY
                directory = base if location == "base" else base / "operations"
                unexpected = directory / f"installed.{'a' * 64}.tmp"

                @contextmanager
                def add_entry_before_lock(layout, device):
                    unexpected.write_bytes(b"unknown private work\n")
                    with original_lock(layout, device):
                        yield

                with mock.patch.object(admin, "_worktree_lock", side_effect=add_entry_before_lock):
                    with self.assertRaisesRegex(admin.AdminError, "operation directories changed since preflight"):
                        self.invoke(
                            FakeIdentity(), admin.apply, "install", repo,
                            caller_authorizer=lambda _facts: True,
                            **self.kwargs(repo, head, payload),
                        )
                self.assertEqual(b"unknown private work\n", unexpected.read_bytes())
                self.assertFalse((repo / "AGENTS.md").exists())
                self.assert_no_git_writer_guards(repo)

    def test_external_identity_binding_and_authorization_callbacks_are_outside_writer_locks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            command("git", "checkout", "-b", "task/142-admin", cwd=repo)
            (repo / ".automation").mkdir()
            (repo / ".automation/VERSION").write_bytes(b"3\n")
            (repo / ".automation/old.py").write_bytes(b"legacy\n")
            legacy_inventory = {
                ".automation/VERSION": {
                    "sha256": hashlib.sha256(b"3\n").hexdigest(),
                    "mode": 0o644,
                },
                ".automation/old.py": {
                    "sha256": hashlib.sha256(b"legacy\n").hexdigest(),
                    "mode": 0o644,
                },
            }
            payload = self.payload(root, head)
            identity = FakeIdentity()
            original_observe = identity.observe_target
            original_verify = identity.verify_cutover_binding
            original_apply_one_path = admin._apply_one_path
            events: list[str] = []
            phase2_started = False
            phase2_finished = False
            phase2_path_count = 0

            def record_external(name: str) -> None:
                self.assert_no_writer_locks(repo)
                events.append(name)

            def observe_target(*args, **kwargs):
                nonlocal phase2_finished
                if phase2_started and not phase2_finished:
                    events.append("phase2-after")
                    phase2_finished = True
                record_external("observe_target")
                return original_observe(*args, **kwargs)

            def verify_cutover_binding(*args, **kwargs):
                record_external("verify_cutover_binding")
                return original_verify(*args, **kwargs)

            def task_binding(_facts, issue, pr):
                record_external("task_binding")
                return issue == 142 and pr == 217

            def metadata_binding(_facts, issue, pr):
                record_external("metadata_binding")
                return issue == 142 and pr == 217

            def caller_authorizer(_facts):
                record_external("authorizer")
                return True

            def mark_local_phase2(request, intent, path, layout, root_device, admin_fs_api):
                nonlocal phase2_path_count, phase2_started
                if not phase2_started:
                    self.assert_admin_and_git_writer_locks_held(repo)
                    events.append("phase2-before")
                    phase2_started = True
                result = original_apply_one_path(request, intent, path, layout, root_device, admin_fs_api)
                phase2_path_count += 1
                events.append("phase2-path-returned")
                changed_paths = [
                    candidate
                    for candidate in admin._ordered_operation_paths(intent)
                    if intent["before"][candidate] != intent["after"][candidate]
                ]
                if phase2_path_count == len(changed_paths):
                    events.append("phase2-paths-applied")
                return result

            binding = {
                "issue": 142,
                "pr": 217,
                "expected_base": "main",
                "task_binding": task_binding,
                "metadata_binding": metadata_binding,
            }
            args = self.kwargs(repo, head, payload)
            args["legacy_inventory"] = legacy_inventory
            with mock.patch.object(identity, "observe_target", side_effect=observe_target):
                with mock.patch.object(
                    identity, "verify_cutover_binding", side_effect=verify_cutover_binding
                ):
                    with mock.patch.object(
                        admin, "_apply_one_path", side_effect=mark_local_phase2
                    ):
                        result = self.invoke(
                            identity,
                            admin.apply,
                            "cutover",
                            repo,
                            caller_authorizer=caller_authorizer,
                            **binding,
                            **args,
                        )

            self.assertEqual("APPLIED", result["status"])
            self.assertTrue(phase2_started)
            self.assertTrue(phase2_finished)
            phase2_before = events.index("phase2-before")
            phase2_paths_applied = events.index("phase2-paths-applied")
            phase2_after = events.index("phase2-after")
            self.assertLess(phase2_before, phase2_after)
            self.assertLess(phase2_paths_applied, phase2_after)
            last_path_return = max(i for i, event in enumerate(events) if event == "phase2-path-returned")
            self.assertGreater(events.index("phase2-after"), last_path_return)
            self.assertTrue(any(event == "authorizer" for event in events[:phase2_before]))
            self.assertTrue(any(event == "verify_cutover_binding" for event in events[:phase2_before]))
            external_events = {
                "observe_target",
                "verify_cutover_binding",
                "task_binding",
                "metadata_binding",
                "authorizer",
            }
            self.assertFalse(
                any(event in external_events for event in events[phase2_before + 1 : phase2_after])
            )
            self.assertTrue(any(event == "authorizer" for event in events[phase2_after:]))
            self.assertTrue(any(event == "verify_cutover_binding" for event in events[phase2_after:]))
            self.assert_no_writer_locks(repo)

    def test_remote_binding_drift_after_local_apply_retains_exact_receipt_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            command("git", "checkout", "-b", "task/142-admin", cwd=repo)
            (repo / ".automation").mkdir()
            (repo / ".automation/VERSION").write_bytes(b"3\n")
            (repo / ".automation/old.py").write_bytes(b"legacy\n")
            legacy_inventory = {
                ".automation/VERSION": {
                    "sha256": hashlib.sha256(b"3\n").hexdigest(),
                    "mode": 0o644,
                },
                ".automation/old.py": {
                    "sha256": hashlib.sha256(b"legacy\n").hexdigest(),
                    "mode": 0o644,
                },
            }
            payload = self.payload(root, head)
            identity = FakeIdentity()
            task_binding = lambda _facts, issue, pr: issue == 142 and pr == 217
            metadata_binding = lambda _facts, issue, pr: issue == 142 and pr == 217
            binding = {
                "issue": 142,
                "pr": 217,
                "expected_base": "main",
                "task_binding": task_binding,
                "metadata_binding": metadata_binding,
            }
            args = self.kwargs(repo, head, payload)
            args["legacy_inventory"] = legacy_inventory
            planned = self.invoke(identity, admin.inspect, "cutover", repo, **binding, **args)
            original_apply_one_path = admin._apply_one_path
            changed_remote_binding = False

            def drift_only_identity_field(request, intent, path, layout, root_device, admin_fs_api):
                nonlocal changed_remote_binding
                self.assert_admin_and_git_writer_locks_held(repo)
                result = original_apply_one_path(request, intent, path, layout, root_device, admin_fs_api)
                if not changed_remote_binding:
                    identity.binding_revision = "moved-after-phase2"
                    changed_remote_binding = True
                return result

            with mock.patch.object(
                admin, "_apply_one_path", side_effect=drift_only_identity_field
            ):
                with self.assertRaises(admin.AdminError) as raised:
                    self.invoke(
                        identity,
                        admin.apply,
                        "cutover",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **binding,
                        **args,
                    )

            message = str(raised.exception)
            self.assertIn(
                f"operation {planned['operationId']} post-lock identity/authorization check failed",
                message,
            )
            self.assertIn("receipt=complete; managed-state=exact-postimage", message)
            self.assertIn(
                "cause: cutover Issue/PR snapshot drifted after the locked local operation",
                message,
            )
            self.assertTrue(changed_remote_binding)
            self.assert_no_writer_locks(repo)
            for relative, (content, mode) in payload[2].items():
                self.assertEqual(content, (repo / relative).read_bytes())
                self.assertEqual(mode, stat.S_IMODE((repo / relative).stat().st_mode))
            self.assertFalse((repo / ".automation/old.py").exists())

            private = repo / ".git" / admin.PRIVATE_DIRECTORY
            receipt_path = (
                private
                / "operations"
                / "receipts"
                / f"{planned['operationId']}.json"
            )
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            self.assertEqual("complete", receipt["status"])

            retried = self.invoke(
                identity,
                admin.retry,
                "cutover",
                repo,
                expected_operation_id=planned["operationId"],
                caller_authorizer=lambda _facts: True,
                **binding,
                **args,
            )

            self.assertEqual("ALREADY_APPLIED", retried["status"])
            self.assertEqual(planned["operationId"], retried["operationId"])
            self.assertEqual([], retried["changedPaths"])
            self.assertEqual(receipt_bytes, receipt_path.read_bytes())
            for relative, (content, _mode) in payload[2].items():
                self.assertEqual(content, (repo / relative).read_bytes())
            self.assertFalse((repo / ".automation/old.py").exists())
            self.assert_no_writer_locks(repo)

    def test_writer_locks_are_acquired_admin_then_index_then_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            payload = self.payload(root, head)
            acquisition_order: list[str] = []
            real_open = admin.os.open
            real_flock = admin.fcntl.flock

            def observe_open(path, flags, *args, **kwargs):
                if path == "index.lock" and flags & admin.os.O_CREAT:
                    acquisition_order.append("index")
                elif path == "HEAD.lock" and flags & admin.os.O_CREAT:
                    acquisition_order.append("HEAD")
                return real_open(path, flags, *args, **kwargs)

            def observe_flock(descriptor, operation):
                result = real_flock(descriptor, operation)
                if operation == (admin.fcntl.LOCK_EX | admin.fcntl.LOCK_NB):
                    acquisition_order.append("Admin")
                return result

            with mock.patch.object(admin.os, "open", side_effect=observe_open), mock.patch.object(
                admin.fcntl, "flock", side_effect=observe_flock
            ):
                result = self.invoke(
                    FakeIdentity(),
                    admin.apply,
                    "install",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, payload),
                )

            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(["Admin", "index", "HEAD"], acquisition_order)
            self.assert_no_writer_locks(repo)

    def test_packed_loose_and_linked_registration_drift_fail_before_lock_creation(self) -> None:
        cases = (
            "packed-only",
            "loose-over-packed",
            "linked-registration",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, head = self.repository(root)
                target = repo
                branch = "bootstrap/agent-core"
                if case == "linked-registration":
                    target = root / "linked"
                    branch = "bootstrap/linked"
                    command(
                        "git", "worktree", "add", "-b", branch, str(target), head, cwd=repo
                    )

                git_dir = self.git_dir(target)
                reference = f"refs/heads/{branch}"
                loose_ref = git_dir / "refs" / "heads" / Path(branch)
                packed_refs = git_dir / "packed-refs"
                packed_header = b"# pack-refs with: peeled fully-peeled sorted\n"
                if case == "packed-only":
                    packed_refs.write_bytes(
                        packed_header + f"{head} {reference}\n".encode("ascii")
                    )
                    loose_ref.unlink()
                elif case == "loose-over-packed":
                    packed_refs.write_bytes(
                        packed_header + f"{'1' * len(head)} {reference}\n".encode("ascii")
                    )

                payload = self.payload(root, head)
                identity = FakeIdentity()
                original_observe = identity.observe_target

                def observe_then_drift(*args, **kwargs):
                    facts = original_observe(*args, **kwargs)
                    if case in {"packed-only", "loose-over-packed"}:
                        if case == "packed-only":
                            packed_refs.write_bytes(
                                packed_header
                                + f"{'2' * len(head)} {reference}\n".encode("ascii")
                            )
                        else:
                            loose_ref.write_bytes(f"{'2' * len(head)}\n".encode("ascii"))
                    else:
                        (Path(facts.git_dir) / "gitdir").write_bytes(
                            os.fsencode(root / "wrong-worktree" / ".git") + b"\n"
                        )
                    return facts

                expected_error = (
                    "local target branch ref differs from the expected HEAD commit"
                    if case != "linked-registration"
                    else "local linked-worktree reciprocal registration changed"
                )
                with mock.patch.object(
                    identity, "observe_target", side_effect=observe_then_drift
                ):
                    with self.assertRaisesRegex(admin.AdminError, expected_error):
                        self.invoke(
                            identity,
                            admin.apply,
                            "install",
                            target,
                            caller_authorizer=lambda _facts: True,
                            **self.kwargs(target, head, payload),
                        )

                self.assertFalse((target / "AGENTS.md").exists())
                self.assertFalse((target / ".automation").exists())
                self.assertFalse((git_dir / admin.PRIVATE_DIRECTORY).exists())
                self.assertFalse((git_dir / "index.lock").exists())
                self.assertFalse((git_dir / "HEAD.lock").exists())

    def test_index_change_and_checkout_race_after_preflight_fail_without_admin_mutation(self) -> None:
        for race in ("index", "checkout"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, head = self.repository(root)
                payload = self.payload(root, head)
                identity = FakeIdentity()
                original_lock = admin._worktree_lock

                @contextmanager
                def race_before_lock(layout, device):
                    self.assert_no_writer_locks(repo)
                    if race == "index":
                        (repo / "product.txt").write_text("dirty product\n", encoding="utf-8")
                        command("git", "add", "product.txt", cwd=repo)
                    else:
                        command("git", "checkout", "-b", "raced-checkout", cwd=repo)
                    with original_lock(layout, device):
                        yield

                expected_message = "index identity changed" if race == "index" else "local Git HEAD does not name"
                with mock.patch.object(admin, "_worktree_lock", side_effect=race_before_lock), self.assertRaisesRegex(admin.AdminError, expected_message):
                    self.invoke(
                        identity,
                        admin.apply,
                        "install",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **self.kwargs(repo, head, payload),
                    )

                self.assertFalse((repo / "AGENTS.md").exists())
                if race == "index":
                    self.assertEqual(b"dirty product\n", (repo / "product.txt").read_bytes())
                    self.assertIn("product.txt", command("git", "diff", "--cached", "--name-only", cwd=repo))
                else:
                    self.assertEqual("raced-checkout", command("git", "branch", "--show-current", cwd=repo))

    def test_cooperating_admin_writer_lock_serializes_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            original = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, original),
            )
            replacement = self.payload(
                root,
                head,
                {
                    ".automation/VERSION": (b"1.0.0\n", 0o644),
                    ".automation/bin/admin.py": (b"next payload\n", 0o644),
                    "AGENTS.md": (b"next instructions\n", 0o644),
                },
            )
            layout = repo / ".git" / admin.PRIVATE_DIRECTORY
            descriptor = admin.os.open(layout / "lock", admin.os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            waiting = threading.Event()
            real_lock = admin._worktree_lock

            @contextmanager
            def observed_lock(*args):
                waiting.set()
                with real_lock(*args):
                    yield

            try:
                with mock.patch.object(admin, "_worktree_lock", side_effect=observed_lock):
                    with ThreadPoolExecutor(max_workers=1) as workers:
                        future = workers.submit(
                            self.invoke,
                            identity,
                            admin.apply,
                            "replace",
                            repo,
                            caller_authorizer=lambda _facts: True,
                            **self.kwargs(repo, head, replacement, operation="replace"),
                        )
                        try:
                            self.assertTrue(waiting.wait(timeout=5))
                            with self.assertRaisesRegex(admin.AdminError, "BUSY: another Agent Core Admin"):
                                future.result(timeout=5)
                            self.assertEqual(b"managed instructions\n", (repo / "AGENTS.md").read_bytes())
                            self.assert_no_git_writer_guards(repo)
                        finally:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                admin.os.close(descriptor)

            result = self.invoke(
                identity, admin.apply, "replace", repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, replacement, operation="replace"),
            )
            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(b"next instructions\n", (repo / "AGENTS.md").read_bytes())

    def test_dirty_product_bytes_and_committed_history_survive_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            linked = root / "linked"
            command("git", "worktree", "add", "-b", "bootstrap/linked", str(linked), head, cwd=repo)
            product = b"unsaved product bytes\n"
            note = b"untracked note\n"
            (linked / "product.txt").write_bytes(product)
            (linked / "note.txt").write_bytes(note)
            payload = self.payload(root, head)

            result = self.invoke(
                FakeIdentity(),
                admin.apply,
                "install",
                linked,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(linked, head, payload),
            )

            self.assertEqual("APPLIED", result["status"])
            self.assertEqual(product, (linked / "product.txt").read_bytes())
            self.assertEqual(note, (linked / "note.txt").read_bytes())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=linked))
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))

    def test_successful_operations_preserve_staged_unstaged_untracked_product_and_index(self) -> None:
        for operation in ("install", "replace", "uninstall", "cutover"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, head = self.repository(root)
                identity = FakeIdentity()
                legacy_inventory = None
                binding = None

                if operation in {"replace", "uninstall"}:
                    initial = self.payload(root, head)
                    installed = self.invoke(
                        identity,
                        admin.apply,
                        "install",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **self.kwargs(repo, head, initial),
                    )
                    self.assertEqual("APPLIED", installed["status"])
                elif operation == "cutover":
                    command("git", "checkout", "-b", "task/142-admin", cwd=repo)
                    (repo / ".automation").mkdir()
                    (repo / ".automation/VERSION").write_bytes(b"3\n")
                    (repo / ".automation/old.py").write_bytes(b"legacy\n")
                    legacy_inventory = {
                        ".automation/VERSION": {
                            "sha256": hashlib.sha256(b"3\n").hexdigest(),
                            "mode": 0o644,
                        },
                        ".automation/old.py": {
                            "sha256": hashlib.sha256(b"legacy\n").hexdigest(),
                            "mode": 0o644,
                        },
                    }
                    binding = {
                        "issue": 142,
                        "pr": 217,
                        "expected_base": "main",
                        "task_binding": lambda _facts, issue, pr: issue == 142 and pr == 217,
                        "metadata_binding": lambda _facts, issue, pr: issue == 142 and pr == 217,
                    }

                dirty = self.dirty_product_state(repo)
                payload = self.payload(
                    root,
                    head,
                    {
                        ".automation/VERSION": (b"1.0.0\n", 0o644),
                        ".automation/bin/admin.py": (
                            b"replacement payload\n" if operation == "replace" else b"admin payload\n",
                            0o644,
                        ),
                        "AGENTS.md": (
                            b"replacement instructions\n"
                            if operation == "replace"
                            else b"managed instructions\n",
                            0o644,
                        ),
                    },
                )
                if operation == "cutover":
                    assert binding is not None and legacy_inventory is not None
                    args = self.kwargs(repo, head, payload)
                    args["legacy_inventory"] = legacy_inventory
                    result = self.invoke(
                        identity,
                        admin.apply,
                        operation,
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **binding,
                        **args,
                    )
                else:
                    operation_payload = None if operation == "uninstall" else payload
                    result = self.invoke(
                        identity,
                        admin.apply,
                        operation,
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **self.kwargs(repo, head, operation_payload, operation=operation),
                    )

                self.assertEqual("APPLIED", result["status"])
                self.assert_dirty_product_state(repo, dirty)

    def test_unknown_preimages_injected_under_writer_locks_fail_before_admin_path_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            payload = self.payload(root, head)
            install_args = self.kwargs(repo, head, payload)
            planned = self.invoke(identity, admin.inspect, "install", repo, **install_args)
            self.assertEqual("READY", planned["status"])
            original_write = admin._write_target_file
            unexpected_install_bytes = b"unexpected file created after planning\n"
            write_injected = False

            def inject_before_write(target_root, relative, *args, **kwargs):
                nonlocal write_injected
                if relative == "AGENTS.md" and not write_injected:
                    self.assert_admin_and_git_writer_locks_held(repo)
                    (target_root / relative).write_bytes(unexpected_install_bytes)
                    write_injected = True
                return original_write(target_root, relative, *args, **kwargs)

            index_before = self.git_index_bytes(repo)
            with mock.patch.object(admin, "_write_target_file", side_effect=inject_before_write):
                with self.assertRaisesRegex(admin.AdminError, "unknown or dirty preimage"):
                    self.invoke(
                        identity,
                        admin.apply,
                        "install",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **install_args,
                    )

            self.assertTrue(write_injected)
            self.assertEqual(unexpected_install_bytes, (repo / "AGENTS.md").read_bytes())
            self.assertFalse((repo / ".automation").exists())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))
            self.assertEqual(index_before, self.git_index_bytes(repo))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            payload = self.payload(root, head)
            self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, payload),
            )
            uninstall_args = self.kwargs(repo, head, None, operation="uninstall")
            planned = self.invoke(identity, admin.inspect, "uninstall", repo, **uninstall_args)
            self.assertEqual("READY", planned["status"])
            original_remove = admin._remove_target_file
            unexpected_uninstall_bytes = b"unexpected managed preimage before unlink\n"
            remove_injected = False

            def inject_before_remove(target_root, relative, before, root_device):
                nonlocal remove_injected
                if relative == ".automation/bin/admin.py" and not remove_injected:
                    self.assert_admin_and_git_writer_locks_held(repo)
                    (target_root / relative).write_bytes(unexpected_uninstall_bytes)
                    remove_injected = True
                return original_remove(target_root, relative, before, root_device)

            index_before = self.git_index_bytes(repo)
            known_agents = (repo / "AGENTS.md").read_bytes()
            known_version = (repo / ".automation/VERSION").read_bytes()
            with mock.patch.object(admin, "_remove_target_file", side_effect=inject_before_remove):
                with self.assertRaisesRegex(admin.AdminError, "unknown or dirty preimage before removal"):
                    self.invoke(
                        identity,
                        admin.apply,
                        "uninstall",
                        repo,
                        caller_authorizer=lambda _facts: True,
                        **uninstall_args,
                    )

            self.assertTrue(remove_injected)
            self.assertEqual(unexpected_uninstall_bytes, (repo / ".automation/bin/admin.py").read_bytes())
            self.assertEqual(known_agents, (repo / "AGENTS.md").read_bytes())
            self.assertEqual(known_version, (repo / ".automation/VERSION").read_bytes())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))
            self.assertEqual(index_before, self.git_index_bytes(repo))

    def test_uninstall_does_not_recursively_delete_an_unknown_automation_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            payload = self.payload(root, head)
            installed = self.invoke(
                identity,
                admin.apply,
                "install",
                repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, payload),
            )
            self.assertEqual("APPLIED", installed["status"])

            unknown_child = repo / ".automation" / "consumer-cache" / "nested" / "state.bin"
            unknown_bytes = b"consumer-owned cache state\x00\xff\n"
            unknown_child.parent.mkdir(parents=True)
            unknown_child.write_bytes(unknown_bytes)
            known_files = {
                relative: (repo / relative).read_bytes()
                for relative in payload[2]
            }

            with self.assertRaisesRegex(admin.AdminError, "prior installed identity"):
                self.invoke(
                    identity,
                    admin.apply,
                    "uninstall",
                    repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, None, operation="uninstall"),
                )

            self.assertEqual(unknown_bytes, unknown_child.read_bytes())
            for relative, content in known_files.items():
                self.assertEqual(content, (repo / relative).read_bytes())
            self.assertTrue(unknown_child.parent.is_dir())
            self.assertEqual(head, command("git", "rev-parse", "HEAD", cwd=repo))

    def test_oversized_operation_record_is_rejected_before_creating_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "oversized.json"
            value = {"data": "x" * admin._PRIVATE_RECORD_LIMIT}
            with self.assertRaisesRegex(admin.AdminError, "exceeds the supported size"):
                admin._write_private_exclusive(record, value, root.stat().st_dev)
            self.assertFalse(record.exists())

    def test_oversized_git_index_is_refused_before_locks_or_managed_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            payload = self.payload(root, head)
            index = repo / ".git" / "index"
            with index.open("r+b") as handle:
                handle.truncate(admin._INDEX_BYTES_LIMIT + 1)
            with self.assertRaisesRegex(admin.AdminError, "bounded snapshot limit"):
                self.invoke(
                    FakeIdentity(), admin.apply, "install", repo,
                    caller_authorizer=lambda _facts: True,
                    **self.kwargs(repo, head, payload),
                )
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".git" / admin.PRIVATE_DIRECTORY).exists())
            self.assertFalse((repo / ".git" / "index.lock").exists())
            self.assertFalse((repo / ".git" / "HEAD.lock").exists())
            self.assertEqual(admin._INDEX_BYTES_LIMIT + 1, index.stat().st_size)

    def test_inspect_recovers_id_from_exact_partial_intent_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            payload = self.payload(root, head)
            args = self.kwargs(repo, head, payload)
            identity = FakeIdentity()
            facts = identity.observe_target(
                repo, expected_repository=args["expected_repository"],
                expected_branch=args["expected_branch"], expected_head=head,
            )
            request = admin._make_request(
                "install", repo, **args, legacy_inventory=None, issue=None, pr=None,
                expected_base=None, task_binding=None, metadata_binding=None,
            )
            target = admin._target_identity(request, facts)
            operation_id = admin._intent_id(request, None, target)
            intent = admin._build_intent(
                request, repo.stat().st_dev, None, operation_id, target, set(),
            )
            layout = admin._private_layout(facts, repo.stat().st_dev, create=True)
            self.assertIsNotNone(layout)
            temporary_intent = layout["intents"] / f".{operation_id}.tmp"
            partial = (admin._canonical_json(intent) + b"\n")[:100]
            temporary_intent.write_bytes(partial)
            temporary_intent.chmod(0o600)

            inspected = self.invoke(identity, admin.inspect, "install", repo, **args)
            self.assertEqual("RETRY", inspected["status"])
            self.assertTrue(inspected["requiresRetry"])
            self.assertEqual(operation_id, inspected["operationId"])
            self.assertEqual(partial, temporary_intent.read_bytes())
            self.assertFalse((repo / "AGENTS.md").exists())

    def test_historical_record_limit_fails_before_mutating_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, head = self.repository(root)
            identity = FakeIdentity()
            payload = self.payload(root, head)
            self.invoke(
                identity, admin.apply, "install", repo,
                caller_authorizer=lambda _facts: True,
                **self.kwargs(repo, head, payload),
            )
            before = (repo / "AGENTS.md").read_bytes()
            with mock.patch.object(admin, "_HISTORY_RECORD_LIMIT", 1):
                with self.assertRaisesRegex(admin.AdminError, "bounded read limit"):
                    self.invoke(
                        identity, admin.inspect, "uninstall", repo,
                        **self.kwargs(repo, head, None, operation="uninstall"),
                    )
            self.assertEqual(before, (repo / "AGENTS.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
