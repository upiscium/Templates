"""Fail-closed Linux no-clobber move for fd-anchored creation staging.

``move_noreplace(source_parent_fd, source_leaf, dest_parent_fd, dest_leaf)``
moves the source only if the destination is absent; it never replaces an
existing destination. It is not a compare-and-swap against bytes or identity
previously observed by a caller. The helper requires libc's ``renameat2``
symbol and Linux ``RENAME_NOREPLACE``. Unsupported primitives and all operation
failures raise ``AdminFsError``. There is deliberately no emulation,
``os.replace``/unlink fallback, or other filesystem operation in this module.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Any, NoReturn


RENAME_NOREPLACE = 1
_C_INT_MAX = (1 << 31) - 1


class AdminFsError(OSError):
    """A fail-closed atomic-rename error with the original errno when available."""

    def __init__(
        self,
        operation: str,
        source_leaf: object,
        dest_leaf: object,
        error_number: int,
    ) -> None:
        self.operation = operation
        self.source_leaf = source_leaf
        self.dest_leaf = dest_leaf
        message = (
            f"renameat2 {operation} failed for {source_leaf!r} "
            f"-> {dest_leaf!r}"
        )
        super().__init__(error_number, message)


def _raise_admin_fs_error(
    operation: str,
    source_leaf: object,
    dest_leaf: object,
    cause: BaseException,
    *,
    fallback_errno: int,
) -> NoReturn:
    error_number = getattr(cause, "errno", None)
    if (
        isinstance(error_number, bool)
        or not isinstance(error_number, int)
        or error_number <= 0
    ):
        error_number = fallback_errno
    raise AdminFsError(operation, source_leaf, dest_leaf, error_number) from cause


def _invalid_argument(
    operation: str, source_leaf: object, dest_leaf: object
) -> NoReturn:
    raise AdminFsError(operation, source_leaf, dest_leaf, errno.EINVAL)


def _validate_fd(
    fd: object, operation: str, source_leaf: object, dest_leaf: object
) -> int:
    # Negative values include AT_FDCWD, which would make this path-based rather
    # than fd-anchored. Reject bool as well, despite bool being an int subclass.
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0 or fd > _C_INT_MAX:
        _invalid_argument(operation, source_leaf, dest_leaf)
    return fd


def _validate_leaf(
    leaf: object, operation: str, source_leaf: object, dest_leaf: object
) -> bytes:
    if not isinstance(leaf, (str, bytes)):
        _invalid_argument(operation, source_leaf, dest_leaf)
    try:
        encoded = os.fsencode(leaf) if isinstance(leaf, str) else leaf
    except (UnicodeError, ValueError):
        _invalid_argument(operation, source_leaf, dest_leaf)
    if (
        not encoded
        or b"/" in encoded
        or b"\0" in encoded
        or encoded in (b".", b"..")
    ):
        _invalid_argument(operation, source_leaf, dest_leaf)
    return encoded


def _load_renameat2() -> Any:
    """Resolve the mandatory libc symbol; never substitute another primitive."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "renameat2 is available only on Linux")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "libc does not provide renameat2") from exc
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function


def _move_noreplace(
    source_parent_fd: object,
    source_leaf: object,
    dest_parent_fd: object,
    dest_leaf: object,
    *,
    prepared_syscall: Any | None = None,
) -> None:
    operation = "move_noreplace"
    source_fd = _validate_fd(source_parent_fd, operation, source_leaf, dest_leaf)
    destination_fd = _validate_fd(dest_parent_fd, operation, source_leaf, dest_leaf)
    source_name = _validate_leaf(source_leaf, operation, source_leaf, dest_leaf)
    destination_name = _validate_leaf(dest_leaf, operation, source_leaf, dest_leaf)

    function = prepared_syscall
    if function is None:
        try:
            function = _load_renameat2()
        except (AttributeError, OSError) as exc:
            _raise_admin_fs_error(
                operation,
                source_leaf,
                dest_leaf,
                exc,
                fallback_errno=errno.ENOSYS,
            )

    ctypes.set_errno(0)
    try:
        result = function(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
            RENAME_NOREPLACE,
        )
    except OSError as exc:
        _raise_admin_fs_error(
            operation,
            source_leaf,
            dest_leaf,
            exc,
            fallback_errno=errno.EIO,
        )

    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        cause = OSError(error_number, os.strerror(error_number))
        _raise_admin_fs_error(
            operation,
            source_leaf,
            dest_leaf,
            cause,
            fallback_errno=errno.EIO,
        )


def move_noreplace(
    source_parent_fd: int,
    source_leaf: str | bytes,
    dest_parent_fd: int,
    dest_leaf: str | bytes,
) -> None:
    """Atomically move a leaf only when the destination name does not exist.

    The destination cannot be clobbered, but its contents are not compared to
    any earlier observation. All errors, including unsupported-kernel errors,
    are reported as ``AdminFsError`` with ``errno`` preserved where available.
    """
    _move_noreplace(
        source_parent_fd,
        source_leaf,
        dest_parent_fd,
        dest_leaf,
    )


class PreparedNoReplace:
    """A syscall resolved during external preflight, not under Git writer locks."""

    def __init__(self, syscall: Any) -> None:
        self._syscall = syscall

    def move_noreplace(
        self,
        source_parent_fd: int,
        source_leaf: str | bytes,
        dest_parent_fd: int,
        dest_leaf: str | bytes,
    ) -> None:
        _move_noreplace(
            source_parent_fd, source_leaf, dest_parent_fd, dest_leaf,
            prepared_syscall=self._syscall,
        )


def prepare_no_replace() -> PreparedNoReplace:
    """Fail before acquiring writer locks if the required libc API is missing."""
    try:
        return PreparedNoReplace(_load_renameat2())
    except (AttributeError, OSError) as exc:
        _raise_admin_fs_error(
            "move_noreplace", "<preflight>", "<preflight>", exc,
            fallback_errno=errno.ENOSYS,
        )
