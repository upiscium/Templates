#!/usr/bin/env python3
"""External, project-noninvasive KAGARI payload bootstrap (Phase 1).

This is deliberately NOT a KAGARI task-runtime activation or OpenCode config
installer. It manages only <project>/.kagari/{runtime,install.json}. All
project-root Flake/Just/OpenCode files and Git data remain project-owned.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterator

SCHEMA = 1
CONTAINER = ".kagari"
RUNTIME = "runtime"
RECEIPT = "install.json"
MAX_FILES = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


class BootstrapError(Exception):
    pass


def require(value: bool, reason: str) -> None:
    if not value:
        raise BootstrapError(reason)


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def regular_file(path: Path, *, label: str) -> bytes:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), f"{label}: not a regular file")
    require(info.st_size <= MAX_FILE_BYTES, f"{label}: file exceeds size limit")
    data = path.read_bytes()
    require(len(data) <= MAX_FILE_BYTES, f"{label}: file grew beyond limit")
    require(path.lstat().st_ino == info.st_ino, f"{label}: changed while reading")
    return data


def valid_relpath(value: object) -> str:
    require(isinstance(value, str) and value and len(value.encode()) <= 240,
            "invalid inventory file path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and
            all(part not in ("", ".", "..") for part in value.split("/")) and
            value == path.as_posix() and not value.startswith(".git/"),
            "unsafe inventory file path")
    return value


def source_payload(source: Path) -> tuple[dict, dict[str, bytes]]:
    require(source.is_dir() and not source.is_symlink(),
            "KAGARI source must be a regular directory")
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    total = 0
    for current, directories, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            require(not path.is_symlink() and path.is_dir(),
                    f"source directory is not regular: {path}")
        for name in names:
            path = current_path / name
            relative = valid_relpath(path.relative_to(source).as_posix())
            contents = regular_file(path, label=f"source {relative}")
            total += len(contents)
            require(len(files) < MAX_FILES and total <= MAX_TOTAL_BYTES,
                    "source exceeds inventory limits")
            files[relative] = contents
            modes[relative] = 0o755 if path.stat().st_mode & 0o111 else 0o644
    require(files, "KAGARI source is empty")
    version = files.get(".automation/VERSION")
    require(version is not None and
            re.fullmatch(rb"[0-9]+(?:\.[0-9]+){0,2}\n", version) is not None,
            "source has no valid KAGARI VERSION")
    inventory = [
        {"path": path, "sha256": hash_bytes(files[path]), "mode": modes[path],
         "size": len(files[path])}
        for path in sorted(files)
    ]
    receipt = {"schema": SCHEMA, "component": "KAGARI",
               "version": version.decode("ascii").strip(), "files": inventory}
    require(len(canonical_json(receipt)) <= MAX_MANIFEST_BYTES,
            "KAGARI receipt exceeds size limit")
    return receipt, files


def validate_receipt(value: object) -> dict:
    require(isinstance(value, dict) and set(value) == {
        "schema", "component", "version", "files"
    }, "invalid KAGARI receipt fields")
    require(value["schema"] == SCHEMA and value["component"] == "KAGARI",
            "unknown KAGARI receipt version/component")
    require(isinstance(value["version"], str) and
            re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value["version"]) is not None,
            "invalid installed KAGARI version")
    rows = value["files"]
    require(isinstance(rows, list) and 1 <= len(rows) <= MAX_FILES,
            "invalid KAGARI receipt inventory")
    total = 0
    previous = ""
    for item in rows:
        require(isinstance(item, dict) and set(item) == {
            "path", "sha256", "mode", "size"
        }, "invalid KAGARI receipt file entry")
        path = valid_relpath(item["path"])
        require(path > previous, "duplicate or unordered KAGARI inventory")
        previous = path
        require(type(item["mode"]) is int and item["mode"] in (0o644, 0o755),
                "invalid KAGARI file mode")
        require(type(item["size"]) is int and 0 <= item["size"] <= MAX_FILE_BYTES,
                "invalid KAGARI file size")
        require(isinstance(item["sha256"], str) and
                re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None,
                "invalid KAGARI file digest")
        total += item["size"]
    require(total <= MAX_TOTAL_BYTES, "installed inventory exceeds limit")
    return value


def git_project(target: Path) -> tuple[Path, Path]:
    require(target.is_dir() and not target.is_symlink(),
            "target must be an existing directory, not a symlink")
    target = target.resolve()
    proc = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        capture_output=True, text=True, check=False
    )
    require(proc.returncode == 0, "target must be a Git worktree")
    output = proc.stdout.splitlines()
    require(len(output) == 2 and Path(output[0]).resolve() == target,
            "target must be the exact Git repository root")
    git_dir = Path(output[1]).resolve()
    require(git_dir.is_dir(), "Git administrative directory unavailable")
    return target, git_dir


def assert_normal_directories(root: Path) -> None:
    for path in (root / CONTAINER, root / CONTAINER / RUNTIME):
        if path.exists() or path.is_symlink():
            require(path.is_dir() and not path.is_symlink(),
                    f"managed path is not a regular directory: {path}")


def inventory_state(root: Path, expected: dict | None) -> dict:
    container = root / CONTAINER
    if not container.exists() and not container.is_symlink():
        return {"state": "ABSENT", "missing": [], "changed": [], "unknown": []}
    assert_normal_directories(root)
    receipt_path = container / RECEIPT
    require(receipt_path.is_file() and not receipt_path.is_symlink(),
            "KAGARI ownership receipt missing or not a regular file")
    raw = regular_file(receipt_path, label="KAGARI receipt")
    require(len(raw) <= MAX_MANIFEST_BYTES, "KAGARI receipt exceeds size limit")
    try:
        receipt = validate_receipt(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BootstrapError("invalid KAGARI ownership receipt") from exc
    require(raw == canonical_json(receipt), "KAGARI receipt encoding is not canonical")
    require(expected is None or receipt == expected,
            "installed KAGARI source differs; explicit replace is not implemented")
    expected_paths = {f"{RUNTIME}/{item['path']}" for item in receipt["files"]}
    known_directories = {RUNTIME}
    for path in expected_paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            known_directories.add(parent.as_posix())
            parent = parent.parent
    observed: set[str] = set()
    unknown_directories: set[str] = set()
    for current, dirs, names in os.walk(container, followlinks=False):
        for name in dirs + names:
            p = Path(current) / name
            require(not p.is_symlink(), f"symlink in KAGARI managed scope: {p}")
            rel = p.relative_to(container).as_posix()
            if p.is_dir():
                if rel not in known_directories:
                    unknown_directories.add(rel + "/")
                continue
            require(p.is_file(), f"special file in KAGARI managed scope: {p}")
            observed.add(rel)
    unknown = sorted((observed - (expected_paths | {RECEIPT})) | unknown_directories)
    missing: list[str] = []
    changed: list[str] = []
    for item in receipt["files"]:
        relative = item["path"]
        file = container / RUNTIME / relative
        if not file.exists() and not file.is_symlink():
            missing.append(relative)
        elif file.is_symlink() or not file.is_file():
            changed.append(relative)
        else:
            data = regular_file(file, label=f"installed {relative}")
            mode = 0o755 if file.stat().st_mode & 0o111 else 0o644
            if hash_bytes(data) != item["sha256"] or mode != item["mode"]:
                changed.append(relative)
    return {"state": ("CONFLICT" if unknown or changed else
                      "REPAIRABLE" if missing else "HEALTHY"),
            "missing": missing, "changed": changed, "unknown": unknown,
            "receipt": receipt}


@contextmanager
def writer_lock(git_dir: Path) -> Iterator[None]:
    lock = git_dir / "kagari-bootstrap.lock"
    with lock.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prepare_install(root: Path, git_dir: Path, receipt: dict,
                    files: dict[str, bytes]) -> None:
    # Stage the complete payload under the same Project filesystem, then rename
    # only into an absent KAGARI-owned path. No root toolchain file is changed.
    stage = Path(tempfile.mkdtemp(prefix=".kagari-stage-", dir=root))
    try:
        runtime = stage / RUNTIME
        runtime.mkdir()
        for item in receipt["files"]:
            path = runtime / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[item["path"]])
            path.chmod(item["mode"])
        (stage / RECEIPT).write_bytes(canonical_json(receipt))
        require(not (root / CONTAINER).exists() and
                not (root / CONTAINER).is_symlink(),
                "KAGARI target appeared during install")
        stage.rename(root / CONTAINER)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def repair_missing(root: Path, receipt: dict, files: dict[str, bytes],
                   missing: list[str]) -> None:
    container = root / CONTAINER / RUNTIME
    lookup = {item["path"]: item for item in receipt["files"]}
    for relative in missing:
        target = container / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # No overwrite, including if a cooperative writer created the path.
        with target.open("xb") as out:
            out.write(files[relative])
        target.chmod(lookup[relative]["mode"])


def remove_owned(root: Path, receipt: dict) -> None:
    container = root / CONTAINER
    runtime = container / RUNTIME
    for item in receipt["files"]:
        path = runtime / item["path"]
        if path.exists() or path.is_symlink():
            path.unlink()
    dirs = {runtime}
    for item in receipt["files"]:
        p = (runtime / item["path"]).parent
        while p != runtime:
            dirs.add(p)
            p = p.parent
    for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    (container / RECEIPT).unlink()
    container.rmdir()


def operate(target: Path, source: Path | None, action: str) -> dict:
    target, git_dir = git_project(target)
    receipt: dict | None = None
    files: dict[str, bytes] | None = None
    if source is not None:
        receipt, files = source_payload(source)
    require(action in ("plan", "install", "repair", "doctor", "uninstall"),
            "unknown KAGARI bootstrap action")
    if action in ("install", "repair"):
        require(receipt is not None and files is not None,
                "install/repair requires an explicit KAGARI source")
    if action in ("plan", "doctor"):
        state = inventory_state(target, receipt)
        return {"operation": action, "status": state["state"],
                "missing": state["missing"], "changed": state["changed"],
                "unknown": state["unknown"], "target": str(target),
                "version": state.get("receipt", receipt or {}).get("version")}
    with writer_lock(git_dir):
        state = inventory_state(target, receipt if action != "uninstall" else None)
        require(state["state"] not in ("CONFLICT",),
                "KAGARI scope contains unknown/modified files; preserve and request review")
        if action == "uninstall":
            if state["state"] == "ABSENT":
                outcome = "ALREADY_ABSENT"
            else:
                remove_owned(target, state["receipt"])
                outcome = "UNINSTALLED"
        elif state["state"] == "ABSENT":
            assert receipt is not None and files is not None
            prepare_install(target, git_dir, receipt, files)
            outcome = "INSTALLED"
        elif state["state"] == "HEALTHY":
            outcome = "UNCHANGED"
        else:
            assert receipt is not None and files is not None
            repair_missing(target, receipt, files, state["missing"])
            outcome = "REPAIRED"
        after = inventory_state(target, receipt if action != "uninstall" else None)
        require(after["state"] == ("ABSENT" if action == "uninstall" else "HEALTHY"),
                "KAGARI postcondition not confirmed")
    return {"operation": action, "status": outcome,
            "target": str(target), "version": (receipt or state.get("receipt") or {}).get("version"),
            "changed_paths": state["missing"] if outcome == "REPAIRED" else []}


def main() -> int:
    p = argparse.ArgumentParser(description="External noninvasive KAGARI payload bootstrap")
    p.add_argument("action", choices=("plan", "install", "repair", "doctor", "uninstall"))
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--source", type=Path)
    args = p.parse_args()
    try:
        result = operate(args.target, args.source, args.action)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    except (BootstrapError, FileNotFoundError, PermissionError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc),
                          "operation": args.action}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
