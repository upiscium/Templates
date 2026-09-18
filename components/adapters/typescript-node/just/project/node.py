#!/usr/bin/env python3
"""Read-only npm project checks and the adapter's fixed script dispatcher."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CAPABILITIES = {
    "format-check": "format:check",
    "lint": "lint",
    "typecheck": "typecheck",
    "test": "test",
    "build": "build",
}
FOREIGN_LOCKS = ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
VERSION = re.compile(r"^(\d+)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:-[0-9A-Za-z.-]+)?$")


class NodeError(RuntimeError):
    pass


def version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+[^\s]+)?", text.strip())
    if not match:
        raise NodeError(f"malformed or prerelease Node version: {text!r}")
    return tuple(map(int, match.groups()))  # type: ignore[return-value]


def partial(text: str) -> tuple[int, int | None, int | None]:
    match = VERSION.fullmatch(text.strip())
    if not match:
        raise NodeError(f"unsupported Node engine syntax: {text!r}")
    values = match.groups()
    return int(values[0]), None if values[1] in (None, "x", "X", "*") else int(values[1]), None if values[2] in (None, "x", "X", "*") else int(values[2])


def satisfies_node(spec: str, actual: tuple[int, int, int]) -> bool:
    spec = spec.strip()
    if not spec or "||" in spec:
        raise NodeError("unsupported Node engine syntax")
    parts = spec.split()
    constraints: list[tuple[str, tuple[int, int, int]]] = []
    for item in parts:
        match = re.fullmatch(r"(>=|<=|>|<|\^|~|=)?(v?\d+(?:\.\d+|\.x|\.X|\.\*)?(?:\.\d+|\.x|\.X|\.\*)?)", item)
        if not match:
            raise NodeError(f"unsupported Node engine syntax: {item!r}")
        op, raw = match.groups()
        major, minor, patch = partial(raw.lstrip("v"))
        base = (major, minor or 0, patch or 0)
        op = op or "="
        if op == "=":
            if minor is None:
                constraints.extend([(">=", base), ("<", (major + 1, 0, 0))])
            elif patch is None:
                constraints.extend([(">=", base), ("<", (major, minor + 1, 0))])
            else:
                constraints.append(("=", base))
        elif op == "^":
            if major == 0:
                raise NodeError("caret Node ranges below major 1 are unsupported")
            constraints.extend([(">=", base), ("<", (major + 1, 0, 0))])
        elif op == "~":
            upper = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
            constraints.extend([(">=", base), ("<", upper)])
        else:
            if patch is not None:
                constraints.append((op, base))
            elif op in (">=", "<"):
                constraints.append((op, base))
            elif op == ">":
                upper = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
                constraints.append((">=", upper))
            else:  # npm's partial <= range includes the whole named component.
                upper = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
                constraints.append(("<", upper))
    return all({"=": actual == target, ">=": actual >= target, "<": actual < target, "<=": actual <= target, ">": actual > target}[op] for op, target in constraints)


def metadata(root: Path) -> dict:
    for name in ("package.json", "package-lock.json", *FOREIGN_LOCKS):
        path = root / name
        if name in FOREIGN_LOCKS and path.exists():
            raise NodeError(f"unsupported foreign lockfile present: {name}")
        if name in ("package.json", "package-lock.json") and not path.is_file():
            raise NodeError(f"required file missing: {name}")
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeError(f"malformed npm metadata: {exc}") from exc
    if not isinstance(package, dict) or not isinstance(lock, dict) or not isinstance(package.get("scripts", {}), dict):
        raise NodeError("malformed npm metadata")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in package.get("scripts", {}).items()):
        raise NodeError("malformed npm scripts metadata")
    manager = package.get("packageManager")
    if manager is not None and (not isinstance(manager, str) or not re.fullmatch(r"npm@\d+(?:\.\d+){0,2}", manager)):
        raise NodeError("packageManager must be a compatible npm version")
    engines = package.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get("node"), str):
        raise NodeError("package.json engines.node is required")
    if lock.get("lockfileVersion") not in (2, 3):
        raise NodeError("package-lock.json must be npm lockfile version 2 or 3")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise NodeError("package-lock.json is missing the root package identity")
    lock_root = packages[""]
    for field in ("name", "version"):
        if package.get(field) != lock.get(field) or package.get(field) != lock_root.get(field):
            raise NodeError(f"package-lock.json root {field} does not match package.json")
    for field in ("engines", "dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if package.get(field) != lock_root.get(field):
            raise NodeError(f"package-lock.json root {field} does not match package.json")
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        declared = package.get(field, {})
        if not isinstance(declared, dict):
            raise NodeError(f"package.json {field} must be an object")
        for dependency in declared:
            entry = packages.get(f"node_modules/{dependency}")
            if not isinstance(entry, dict):
                raise NodeError(
                    f"package-lock.json does not resolve root {field} entry: {dependency}"
                )
    if not shutil.which("node") or not shutil.which("npm"):
        raise NodeError("node and npm are required")
    return package


def doctor(root: Path) -> int:
    package = metadata(root)
    validate_runtime(package)
    print("TypeScript Node Project Adapter doctor: PASS")
    return 0


def validate_runtime(package: dict) -> None:
    actual = version(subprocess.check_output(["node", "--version"], text=True))
    if not satisfies_node(package["engines"]["node"], actual):
        raise NodeError(f"Node {actual[0]}.{actual[1]}.{actual[2]} does not satisfy engines.node")
    manager = package.get("packageManager")
    if manager is None:
        return
    declared = tuple(int(part) for part in manager.removeprefix("npm@").split("."))
    npm_text = subprocess.check_output(["npm", "--version"], text=True).strip()
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", npm_text):
        raise NodeError(f"malformed npm version: {npm_text!r}")
    actual_npm = tuple(int(part) for part in npm_text.split("."))
    if actual_npm[: len(declared)] != declared:
        raise NodeError(f"npm {npm_text} does not match packageManager {manager}")


def run(root: Path, package: dict, capability: str) -> int:
    script = CAPABILITIES[capability]
    if script not in package["scripts"]:
        print(f"TypeScript Node Project Adapter {capability}: SKIPPED")
        return 0
    # --ignore-scripts suppresses implicit pre/post lifecycle hooks while the
    # explicitly mapped repository-owned capability itself remains runnable.
    result = subprocess.run(["npm", "--ignore-scripts", "run", script], cwd=root)
    if result.returncode == 0:
        print(f"TypeScript Node Project Adapter {capability}: PASS")
    else:
        print(f"TypeScript Node Project Adapter {capability}: FAIL", file=sys.stderr)
    return result.returncode


def check(root: Path, package: dict) -> int:
    failures = sum(run(root, package, capability) != 0 for capability in CAPABILITIES)
    if failures:
        print("TypeScript Node Project Adapter check: FAIL", file=sys.stderr)
        return 1
    print("TypeScript Node Project Adapter check: PASS")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("doctor", "check", *CAPABILITIES):
        print("usage: node.py doctor|check|format-check|lint|typecheck|test|build", file=sys.stderr)
        return 2
    root = Path.cwd()
    try:
        if sys.argv[1] == "doctor":
            return doctor(root)
        package = metadata(root)
        validate_runtime(package)
        if sys.argv[1] == "check":
            return check(root, package)
        return run(root, package, sys.argv[1])
    except (NodeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
