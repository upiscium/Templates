from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components/agent-core-v4"))

import cleanup_resources  # noqa: E402
from cleanup_resources import (  # noqa: E402
    NodeObservation,
    PathIdentity,
    TaskResourceInspector,
    TaskRootSpec,
    WorktreeInventory,
    safeResourceInspectionError,
)


_REPOSITORY = "acme/widgets"
_TASK = "198"
_BRANCH = "refs/heads/issue-198"


def git(*arguments: str, cwd: Path | None = None) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


@unittest.skipUnless(sys.platform.startswith("linux"), "cleanup proof requires Linux procfs mount IDs")
class CleanupResourcesV4Test(unittest.TestCase):
    """All filesystem and Git mutations in this class are TemporaryDirectory fixtures."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cleanup-resources-v4-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.repository_root = self.temp / "repository"
        self.worktree_root = self.repository_root / ".worktrees" / "t-198"
        self.repository_root.mkdir()
        (self.repository_root / ".worktrees").mkdir()
        git("init", "--initial-branch=main", str(self.repository_root))
        git("config", "user.name", "Cleanup Fixture", cwd=self.repository_root)
        git("config", "user.email", "cleanup-fixture@example.invalid", cwd=self.repository_root)
        (self.repository_root / ".gitignore").write_text("cache/\n*.cache\n", encoding="utf-8")
        (self.repository_root / "tracked.txt").write_bytes(b"tracked fixture data\n")
        git("add", ".gitignore", "tracked.txt", cwd=self.repository_root)
        git("commit", "-m", "temporary cleanup proof fixture", cwd=self.repository_root)
        git(
            "worktree", "add", "-b", "issue-198", str(self.worktree_root), "HEAD",
            cwd=self.repository_root,
        )
        self.git_admin = Path(
            git("rev-parse", "--absolute-git-dir", cwd=self.worktree_root)
            .decode("utf-8", errors="strict").strip()
        )
        (self.worktree_root / "ordinary-untracked.bin").write_bytes(b"unknown fixture payload\x00")
        (self.worktree_root / "cache").mkdir()
        (self.worktree_root / "cache" / "ignored.cache").write_bytes(b"ignored fixture payload\x00\xff")

    def _spec(self, **overrides: str) -> TaskRootSpec:
        head = git("rev-parse", "HEAD", cwd=self.worktree_root).decode("ascii").strip()
        values = {
            "repository": _REPOSITORY,
            "task": _TASK,
            "branch_ref": _BRANCH,
            "head": head,
            "repository_root": str(self.repository_root),
            "worktree_root": str(self.worktree_root),
            "git_admin": str(self.git_admin),
        }
        values.update(overrides)
        return TaskRootSpec(**values)

    def _inspector(self, **overrides: str) -> TaskResourceInspector:
        return TaskResourceInspector(self._spec(**overrides))

    def test_manifest_is_frozen_sorted_and_hashes_tracked_untracked_and_ignored_data(self) -> None:
        inspector = self._inspector()

        inventory = inspector.inventory()

        self.assertIsInstance(inventory, WorktreeInventory)
        self.assertIsInstance(inventory.nodes[0], NodeObservation)
        self.assertIsInstance(inventory.root_identity, PathIdentity)
        self.assertEqual(
            sorted(node.relative_path for node in inventory.nodes),
            [node.relative_path for node in inventory.nodes],
        )
        paths = {node.relative_path for node in inventory.nodes}
        self.assertEqual({
            ".gitignore", "tracked.txt", "ordinary-untracked.bin", "cache",
            "cache/ignored.cache",
        }, paths)
        self.assertNotIn(".git", paths)
        ignored = next(node for node in inventory.nodes if node.relative_path == "cache/ignored.cache")
        self.assertEqual(
            hashlib.sha256(b"ignored fixture payload\x00\xff").hexdigest(),
            ignored.content_sha256,
        )
        self.assertNotIn(b"ignored fixture payload", repr(inventory).encode())
        self.assertEqual(inventory, inspector.revalidate(inventory))
        with self.assertRaises(AttributeError):
            inventory.nodes = ()  # type: ignore[misc]

    def test_exact_relative_manifest_selection_and_bound_absence_only(self) -> None:
        inspector = self._inspector()
        inventory = inspector.inventory()

        selected = inspector.relative_nodes(inventory, "cache")
        self.assertEqual(("cache", "cache/ignored.cache"), tuple(node.relative_path for node in selected))
        self.assertFalse(inspector.is_absent("tracked.txt"))
        self.assertTrue(inspector.is_absent("missing-child"))
        for invalid in (
            "", ".", "/tracked.txt", "../tracked.txt", "cache/../tracked.txt",
            "cache//ignored.cache", ".git", "cache/*", "cache/$HOME", "cache/",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(safeResourceInspectionError):
                inspector.relative_nodes(inventory, invalid)
            with self.subTest(absent_invalid=invalid), self.assertRaises(safeResourceInspectionError):
                inspector.is_absent(invalid)
        with self.assertRaises(safeResourceInspectionError):
            inspector.relative_nodes(inventory, "not-in-manifest")
        with self.assertRaises(safeResourceInspectionError):
            inspector.is_absent("missing-parent/child")

    def test_dirty_or_changed_content_invalidates_the_exact_inventory_without_mutation(self) -> None:
        inspector = self._inspector()
        inventory = inspector.inventory()
        tracked = self.worktree_root / "tracked.txt"
        tracked.write_bytes(b"changed fixture bytes\n")

        with self.assertRaises(safeResourceInspectionError):
            inspector.revalidate(inventory)
        self.assertEqual(b"changed fixture bytes\n", tracked.read_bytes())

    def test_managed_namespace_rejects_default_sibling_and_symlink_roots(self) -> None:
        bad_roots = (
            str(self.repository_root),
            str(self.temp / "sibling-worktree"),
        )
        for root in bad_roots:
            with self.subTest(root=root), self.assertRaises(safeResourceInspectionError):
                self._inspector(worktree_root=root)

        alias = self.repository_root / ".worktrees" / "t-198-alias"
        alias.symlink_to(self.worktree_root, target_is_directory=True)
        with self.assertRaises(safeResourceInspectionError):
            self._inspector(worktree_root=str(alias))

        with self.assertRaises(safeResourceInspectionError):
            self._inspector(git_admin=str(self.repository_root / ".git"))

    def test_typed_task_head_and_ref_grammars_and_resource_bounds_are_strict(self) -> None:
        for overrides in (
            {"task": "0198"},
            {"task": "0"},
            {"branch_ref": "refs/heads/bad..ref"},
            {"branch_ref": "refs/heads/bad ref"},
            {"head": "A" * 40},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(safeResourceInspectionError):
                self._inspector(**overrides)

        inspector = self._inspector()
        with mock.patch.object(cleanup_resources, "MAX_FILE_BYTES", 2):
            with self.assertRaises(safeResourceInspectionError):
                inspector.inventory()
        with self.assertRaises(safeResourceInspectionError):
            inspector.is_absent("x" * 4097)

    def test_symlinks_nested_git_entries_and_nonregular_or_hardlinked_files_fail_closed(self) -> None:
        outside = self.temp / "outside-sentinel"
        outside.write_bytes(b"outside data must not be followed")
        (self.worktree_root / "linked-outside").symlink_to(outside)
        with self.assertRaises(safeResourceInspectionError):
            self._inspector().inventory()
        self.assertEqual(b"outside data must not be followed", outside.read_bytes())
        (self.worktree_root / "linked-outside").unlink()

        nested = self.worktree_root / "nested"
        nested.mkdir()
        (nested / ".git").write_text("not a worktree pointer\n", encoding="utf-8")
        with self.assertRaises(safeResourceInspectionError):
            self._inspector().inventory()
        (nested / ".git").unlink()
        nested.rmdir()

        os.link(self.worktree_root / "tracked.txt", self.worktree_root / "hard-link.txt")
        with self.assertRaises(safeResourceInspectionError):
            self._inspector().inventory()
        (self.worktree_root / "hard-link.txt").unlink()

        if hasattr(os, "mkfifo"):
            os.mkfifo(self.worktree_root / "fixture-fifo")
            with self.assertRaises(safeResourceInspectionError):
                self._inspector().inventory()

    def test_root_replacement_is_not_reported_as_absence_and_both_targets_remain(self) -> None:
        inspector = self._inspector()
        inventory = inspector.inventory()
        self.assertFalse(inspector.root_absent())
        held = self.worktree_root.with_name("retained-original-worktree")
        self.worktree_root.rename(held)
        self.worktree_root.mkdir()

        with self.assertRaises(safeResourceInspectionError):
            inspector.revalidate(inventory)
        with self.assertRaises(safeResourceInspectionError):
            inspector.root_absent()
        self.assertTrue((held / "tracked.txt").is_file())
        self.assertTrue(self.worktree_root.is_dir())

    def test_named_directory_swap_after_open_is_detected_without_removing_either_tree(self) -> None:
        swap_directory = self.worktree_root / "swap-me"
        swap_directory.mkdir()
        (swap_directory / "keep.txt").write_bytes(b"retained directory data")
        inspector = self._inspector()
        retained = self.worktree_root / "retained-swap-me"
        original_assert = inspector._assert_named_fd
        swapped = False

        def exchange_after_open(
            parent_fd: int, name: str, child_fd: int, *, anchor_only: bool = False,
        ) -> PathIdentity:
            nonlocal swapped
            if name == "swap-me" and not swapped:
                swapped = True
                swap_directory.rename(retained)
                swap_directory.mkdir()
            return original_assert(parent_fd, name, child_fd, anchor_only=anchor_only)

        with mock.patch.object(inspector, "_assert_named_fd", side_effect=exchange_after_open):
            with self.assertRaises(safeResourceInspectionError):
                inspector.inventory()
        self.assertTrue(swapped)
        self.assertEqual(b"retained directory data", (retained / "keep.txt").read_bytes())
        self.assertTrue(swap_directory.is_dir())

    def test_shared_absolute_ancestor_metadata_change_does_not_change_worktree_proof(self) -> None:
        inspector = self._inspector()
        baseline = inspector.inventory()
        ancestor_before = self.temp.stat()
        ancestor_inode = ancestor_before.st_ino
        unrelated = self.temp / "unrelated-ancestor-child"
        original_identity = inspector._identity
        changed = False

        def change_shared_ancestor_after_identity(fd: int) -> PathIdentity:
            nonlocal changed
            identity = original_identity(fd)
            if not changed and os.fstat(fd).st_ino == ancestor_inode:
                unrelated.mkdir()
                changed = True
            return identity

        with mock.patch.object(
            inspector, "_identity", side_effect=change_shared_ancestor_after_identity,
        ):
            observed = inspector.inventory()

        self.assertTrue(changed)
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(ancestor_before.st_nlink + 1, self.temp.stat().st_nlink)
        self.assertEqual(baseline, observed)
        self.assertEqual(observed, inspector.revalidate(observed))

    def test_missing_or_changed_mount_identity_proof_fails_without_st_dev_fallback(self) -> None:
        inspector = self._inspector()
        original_mount_id = inspector._mount_id
        expected_root = inspector._expected.root_identity  # type: ignore[union-attr]

        def synthetic_same_device_bind_mount(fd: int) -> int:
            if os.fstat(fd).st_ino == expected_root.inode:
                self.assertEqual(expected_root.device, os.fstat(fd).st_dev)
                return expected_root.mount_id + 1
            return original_mount_id(fd)

        with mock.patch.object(inspector, "_mount_id", side_effect=synthetic_same_device_bind_mount):
            with self.assertRaises(safeResourceInspectionError):
                inspector.inventory()

        with mock.patch.object(
            inspector, "_mount_id", side_effect=safeResourceInspectionError("mount_proof_unavailable"),
        ):
            with self.assertRaises(safeResourceInspectionError):
                inspector.inventory()

    def test_permission_failure_is_fail_closed_and_filesystem_is_left_intact(self) -> None:
        inspector = self._inspector()
        with mock.patch.object(inspector, "_read_regular_at", side_effect=PermissionError):
            with self.assertRaises(safeResourceInspectionError):
                inspector.inventory()
        self.assertEqual(b"tracked fixture data\n", (self.worktree_root / "tracked.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
