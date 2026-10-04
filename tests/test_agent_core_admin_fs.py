from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import agent_core_admin_fs as admin_fs  # noqa: E402


@unittest.skipUnless(sys.platform.startswith("linux"), "renameat2 is Linux-specific")
class AgentCoreAdminFsTest(unittest.TestCase):
    def open_directory(self, path: Path) -> int:
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def test_move_noreplace_never_clobbers_a_concurrently_created_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").write_bytes(b"source inode bytes\x00\xff")
            parent_fd = self.open_directory(root)
            start = threading.Barrier(2)
            outcomes: dict[str, object] = {}

            def move_source() -> None:
                start.wait()
                try:
                    admin_fs.move_noreplace(parent_fd, "source", parent_fd, "destination")
                    outcomes["move"] = "moved"
                except BaseException as exc:  # Capture thread failures for assertion below.
                    outcomes["move"] = exc

            def create_destination() -> None:
                start.wait()
                try:
                    fd = os.open(
                        "destination",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    outcomes["create"] = "destination-already-existed"
                    return
                except BaseException as exc:  # Capture thread failures for assertion below.
                    outcomes["create"] = exc
                    return
                with os.fdopen(fd, "wb") as output:
                    output.write(b"concurrent creator bytes")
                outcomes["create"] = "created"

            mover = threading.Thread(target=move_source)
            creator = threading.Thread(target=create_destination)
            mover.start()
            creator.start()
            mover.join()
            creator.join()
            os.close(parent_fd)

            self.assertEqual(set(outcomes), {"move", "create"})
            self.assertNotIsInstance(outcomes["create"], BaseException)
            destination_contents = (root / "destination").read_bytes()
            if outcomes["move"] == "moved":
                self.assertIn(outcomes["create"], {"destination-already-existed"})
                self.assertEqual(destination_contents, b"source inode bytes\x00\xff")
                self.assertFalse((root / "source").exists())
            else:
                move_error = outcomes["move"]
                self.assertIsInstance(move_error, admin_fs.AdminFsError)
                self.assertEqual(move_error.errno, errno.EEXIST)
                self.assertEqual(outcomes["create"], "created")
                self.assertEqual(destination_contents, b"concurrent creator bytes")
                self.assertEqual((root / "source").read_bytes(), b"source inode bytes\x00\xff")

    def test_missing_renameat2_fails_closed_without_replacement_or_unlink_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"source remains")
            parent_fd = self.open_directory(root)
            try:
                with (
                    mock.patch.object(
                        admin_fs,
                        "_load_renameat2",
                        side_effect=OSError(errno.ENOSYS, "renameat2 unavailable"),
                    ),
                    mock.patch.object(os, "replace", side_effect=AssertionError("fallback")),
                    mock.patch.object(os, "unlink", side_effect=AssertionError("fallback")),
                    self.assertRaises(admin_fs.AdminFsError) as raised,
                ):
                    admin_fs.move_noreplace(parent_fd, "source", parent_fd, "destination")
            finally:
                os.close(parent_fd)

            self.assertEqual(raised.exception.errno, errno.ENOSYS)
            self.assertEqual(source.read_bytes(), b"source remains")
            self.assertFalse((root / "destination").exists())

    def test_rejects_path_syntax_in_leaf_names_before_calling_libc(self) -> None:
        with mock.patch.object(admin_fs, "_load_renameat2") as load:
            for leaf in ("", ".", "..", "nested/name", "nul\0byte"):
                with self.subTest(leaf=leaf), self.assertRaises(admin_fs.AdminFsError) as raised:
                    admin_fs.move_noreplace(10, leaf, 11, "destination")
                self.assertEqual(raised.exception.errno, errno.EINVAL)
        load.assert_not_called()

    def test_prepared_no_replace_does_not_resolve_libc_during_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").write_bytes(b"known bytes")
            prepared = admin_fs.prepare_no_replace()
            directory = self.open_directory(root)
            try:
                with mock.patch.object(
                    admin_fs, "_load_renameat2",
                    side_effect=AssertionError("loader called inside local phase"),
                ):
                    prepared.move_noreplace(directory, "source", directory, "destination")
            finally:
                os.close(directory)
            self.assertEqual(b"known bytes", (root / "destination").read_bytes())
            self.assertFalse((root / "source").exists())


if __name__ == "__main__":
    unittest.main()
