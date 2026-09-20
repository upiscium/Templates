#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path

from render_templates import CompositionError, TemplateSpec, compose_entries


class AdoptionError(RuntimeError):
    pass


JUST_COMPATIBILITY_PREREQUISITE = (
    "Before running Agent Core recipes in the target repository, enter or update "
    "the target repository environment so `just` is at least the required version."
)


@dataclass(frozen=True)
class Action:
    path: str
    action: str
    owner: str
    reason: str


def templates_root() -> Path:
    return Path(__file__).resolve().parents[1]


def required_just_version(source: Path) -> str | None:
    justfile = source / "components" / "agent-core" / "Justfile"
    if not justfile.is_file():
        return None
    for line in justfile.read_text(encoding="utf-8").splitlines():
        prefix = 'set minimum-version := "'
        if line.startswith(prefix) and line.endswith('"'):
            return line[len(prefix):-1]
    return None


def target_just_compatibility(root: Path, required: str | None) -> dict:
    preserved_tooling = [
        path for path in ("flake.nix", "flake.lock") if (root / path).exists()
    ]
    reason = (
        "target repository tooling was not executed during the read-only adoption plan"
    )
    if preserved_tooling:
        reason = (
            "target repository preserves existing tooling files: "
            + ", ".join(preserved_tooling)
        )
    return {
        "requiredJustVersion": required,
        "status": "unknown",
        "reason": reason,
        "postAdoptionPrerequisite": JUST_COMPATIBILITY_PREREQUISITE,
    }


def run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AdoptionError(f"{' '.join(command)}: {detail}")
    return result


def repository_root(path: Path) -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    return Path(result.stdout.strip()).resolve()


def available_adapters(source: Path) -> set[str]:
    root = source / "components" / "adapters"
    if not root.is_dir():
        raise AdoptionError("missing components/adapters")
    return {path.name for path in root.iterdir() if path.is_dir()}


def detect_adapter(target: Path, adapters: set[str]) -> tuple[str, str, list[str]]:
    primary_signals = [
        ("cpp-cmake", "CMakeLists.txt"),
        ("python", "pyproject.toml"),
        ("rust", "Cargo.toml"),
        ("typescript-node", "package.json"),
    ]
    primary = [
        (adapter, marker)
        for adapter, marker in primary_signals
        if adapter in adapters and (target / marker).exists()
    ]
    if len(primary) == 1:
        adapter, marker = primary[0]
        return adapter, f"unique language/toolchain marker: {marker}", [adapter]
    if len(primary) > 1:
        names = [name for name, _ in primary]
        return "base", "language/toolchain adapter detection is ambiguous; using mandatory base fallback", names
    if "nix" in adapters and (target / "flake.nix").exists():
        return "nix", "flake.nix is present and no language/toolchain adapter matched", ["nix"]
    return "base", "no dedicated adapter matched; using mandatory base fallback", []


def select_adapter(source: Path, target: Path, requested: str) -> tuple[str, str, list[str]]:
    adapters = available_adapters(source)
    if "base" not in adapters:
        raise AdoptionError("base adapter is required but missing")
    if requested == "auto":
        return detect_adapter(target, adapters)
    if requested not in adapters:
        raise AdoptionError(
            f"unknown adapter {requested!r}; available: {', '.join(sorted(adapters))}"
        )
    return requested, "explicit adapter selection", [requested]


def load_policy(source: Path, adapter: str) -> dict:
    path = source / "components" / "adapters" / adapter / ".automation" / "adoption.toml"
    if not path.is_file():
        return {
            "preserve_existing": [],
            "line_merge": {},
            "structured_merge": {},
        }
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        "preserve_existing": list(raw.get("preserve_existing", [])),
        "line_merge": dict(raw.get("line_merge", {})),
        "structured_merge": dict(raw.get("structured_merge", {})),
    }


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def bytes_equal(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    return left.read_bytes() == right.read_bytes()


def just_router_merge(existing: str) -> tuple[str | None, str]:
    required = {
        "agent": "mod agent '.automation/just/agent.just'",
        "integrate": "mod integrate '.automation/just/integrate.just'",
        "project": "mod project 'just/project/mod.just'",
        "local": "mod? local 'just/local.just'",
    }
    lines = existing.splitlines()
    for name, expected in required.items():
        conflicting = [line.strip() for line in lines if line.strip().startswith(f"mod {name} ") or line.strip().startswith(f"mod? {name} ")]
        if conflicting and expected not in conflicting:
            return None, f"existing Just module {name!r} conflicts with Agent Core router"
    missing = [value for value in required.values() if value not in lines]
    if not missing:
        return existing, "Agent module router already present"
    suffix = "\n" if existing.endswith("\n") or not existing else "\n\n"
    merged = existing + suffix + "# Agent Core module router\n" + "\n".join(missing) + "\n"
    return merged, "append non-conflicting Agent Core module router declarations"


def agent_rules_merge(existing: str, core_rules: str) -> tuple[str | None, str]:
    begin = "<!-- BEGIN AGENT CORE RULES -->"
    end = "<!-- END AGENT CORE RULES -->"
    block = f"{begin}\n{core_rules.rstrip()}\n{end}"
    if begin in existing or end in existing:
        if block in existing:
            return existing, "Agent Core rules block already present"
        return None, "existing Agent Core rules marker does not match current source"
    suffix = "\n" if existing.endswith("\n") or not existing else "\n\n"
    return existing + suffix + block + "\n", "append marked Agent Core rules block"


def line_merge(existing: str, required: list[str]) -> tuple[str, list[str]]:
    lines = existing.splitlines()
    missing = [line for line in required if line not in lines]
    if not missing:
        return existing, []
    suffix = "\n" if existing.endswith("\n") or not existing else "\n"
    return existing + suffix + "\n".join(missing) + "\n", missing


def materialized_bytes(source_path: Path) -> bytes:
    if source_path.is_symlink():
        raise AdoptionError(f"symlink adoption is not supported yet: {source_path}")
    return source_path.read_bytes()


def unsafe_destination_reason(root: Path, destination: Path) -> str | None:
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return "destination path is outside repository root"
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return f"destination path traverses symlink: {current.relative_to(root)}"
    try:
        destination.parent.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return "destination parent resolves outside repository root"
    return None


def open_parent_directory(root: Path, relative: Path, *, create: bool) -> int:
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise AdoptionError(f"unsafe adoption destination: {relative}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in relative.parent.parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except (OSError, RuntimeError) as exc:
        os.close(descriptor)
        raise AdoptionError(f"unsafe adoption destination {relative}: {exc}") from exc


@contextmanager
def exclusive_adoption_lock(root: Path):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AdoptionError("another adoption apply is active for this repository") from exc
        yield
    finally:
        os.close(descriptor)


def create_regular_file(root: Path, relative: Path, source: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise AdoptionError(f"unsupported adoption source: {source}")
    mode = stat.S_IMODE(source.stat(follow_symlinks=False).st_mode)
    payload = source.read_bytes()
    parent = open_parent_directory(root, relative, create=True)
    descriptor: int | None = None
    temporary = f".adoption-{os.getpid()}-{relative.name}.tmp"
    installed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise AdoptionError(f"short write while creating adoption destination: {relative}")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.link(
            temporary,
            relative.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
        installed = True
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    except (OSError, AdoptionError) as exc:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        if installed:
            try:
                os.unlink(relative.name, dir_fd=parent)
            except FileNotFoundError:
                pass
        if isinstance(exc, AdoptionError):
            raise
        raise AdoptionError(
            f"adoption destination became unsafe or occupied for {relative}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def read_regular_file(root: Path, relative: Path) -> bytes:
    parent = open_parent_directory(root, relative, create=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AdoptionError(f"adoption destination is not a regular file: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise AdoptionError(f"adoption destination became unsafe for {relative}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def merge_regular_text(
    root: Path,
    relative: Path,
    merge: Callable[[str], tuple[str | None, str]],
) -> None:
    parent = open_parent_directory(root, relative, create=False)
    descriptor: int | None = None
    temporary = f".adoption-{os.getpid()}-{relative.name}.tmp"
    try:
        descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        original_stat = os.fstat(descriptor)
        if not stat.S_ISREG(original_stat.st_mode):
            raise AdoptionError(f"adoption merge destination is not a regular file: {relative}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            current = handle.read()
        updated, detail = merge(current)
        if updated is None:
            raise AdoptionError(f"merge became unsafe for {relative}: {detail}")
        temporary_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(original_stat.st_mode),
            dir_fd=parent,
        )
        try:
            payload = updated.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(temporary_descriptor, view)
                if written == 0:
                    raise AdoptionError(f"short write while merging adoption destination: {relative}")
                view = view[written:]
            os.fchmod(temporary_descriptor, stat.S_IMODE(original_stat.st_mode))
            os.fsync(temporary_descriptor)
        finally:
            os.close(temporary_descriptor)
        current_stat = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (original_stat.st_dev, original_stat.st_ino):
            raise AdoptionError(f"adoption merge destination changed during apply: {relative}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        latest_chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            latest_chunks.append(chunk)
        if b"".join(latest_chunks) != current.encode("utf-8"):
            raise AdoptionError(f"adoption merge destination changed during apply: {relative}")
        os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except (OSError, UnicodeError, AdoptionError) as exc:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        if isinstance(exc, AdoptionError):
            raise
        raise AdoptionError(f"adoption merge destination became unsafe for {relative}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def build_plan(source: Path, target: Path, requested_adapter: str) -> dict:
    root = repository_root(target)
    adapter, reason, detected = select_adapter(source, root, requested_adapter)
    policy = load_policy(source, adapter)
    spec = TemplateSpec(name="adoption", adapter=adapter, description="existing repository adoption")
    try:
        entries = compose_entries(source, spec)
    except CompositionError as exc:
        raise AdoptionError(str(exc)) from exc

    actions: list[Action] = []
    blockers: list[str] = []
    if adapter == "typescript-node":
        for required in ("package.json", "package-lock.json"):
            if not (root / required).is_file():
                blockers.append(
                    f"{required}: existing npm metadata is required for TypeScript/Node adoption"
                )
        foreign_locks = [
            name
            for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
            if (root / name).exists()
        ]
        if foreign_locks:
            blockers.append(
                "unsupported package-manager lockfiles are present: "
                + ", ".join(foreign_locks)
            )
        package_path = root / "package.json"
        lock_path = root / "package-lock.json"
        package: dict | None = None
        lock: dict | None = None
        if package_path.is_file():
            try:
                raw_package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                blockers.append(f"package.json: malformed npm metadata: {exc}")
            else:
                if not isinstance(raw_package, dict):
                    blockers.append("package.json: npm metadata must be an object")
                else:
                    package = raw_package
                    manager = package.get("packageManager")
                    npm_version = r"(?:0|[1-9]\d{0,5})(?:\.(?:0|[1-9]\d{0,5})){0,2}"
                    if manager is not None and (
                        not isinstance(manager, str)
                        or re.fullmatch(rf"npm@{npm_version}", manager) is None
                    ):
                        blockers.append("package.json: packageManager must select canonical npm")
                    engines = package.get("engines")
                    if not isinstance(engines, dict) or not isinstance(engines.get("node"), str):
                        blockers.append("package.json: engines.node is required")
        if lock_path.is_file():
            try:
                raw_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                blockers.append(f"package-lock.json: malformed npm metadata: {exc}")
            else:
                if not isinstance(raw_lock, dict):
                    blockers.append("package-lock.json: npm metadata must be an object")
                else:
                    lock = raw_lock
        if package is not None and lock is not None:
            packages = lock.get("packages")
            lock_root = packages.get("") if isinstance(packages, dict) else None
            if lock.get("lockfileVersion") not in (2, 3) or not isinstance(lock_root, dict):
                blockers.append("package-lock.json: npm lockfile root identity is required")
            else:
                for field in ("name", "version"):
                    identity = package.get(field)
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or lock.get(field) != identity
                        or lock_root.get(field) != identity
                    ):
                        blockers.append(
                            f"package-lock.json: root {field} must match package.json"
                        )
    for relative, entry in sorted(entries.items(), key=lambda item: item[0].as_posix()):
        rel = relative.as_posix()
        destination = root / relative
        owner = entry.component

        unsafe = unsafe_destination_reason(root, destination)
        if unsafe:
            blockers.append(f"{rel}: {unsafe}")
            actions.append(Action(rel, "blocked", owner, unsafe))
            continue

        if not destination.exists() and not destination.is_symlink():
            actions.append(Action(rel, "create", owner, "path does not exist"))
            continue

        if destination.is_file() and entry.source.is_file() and bytes_equal(destination, entry.source):
            actions.append(Action(rel, "noop", owner, "existing content is identical"))
            continue

        if (
            owner.startswith("adapter:")
            and matches(rel, policy["preserve_existing"])
            and destination.is_file()
        ):
            actions.append(Action(rel, "preserve", "repository", "adapter adoption policy preserves existing repository-owned file"))
            continue

        if rel in policy["line_merge"] and destination.is_file():
            _, missing = line_merge(destination.read_text(encoding="utf-8"), policy["line_merge"][rel])
            action = "merge" if missing else "noop"
            actions.append(Action(rel, action, "shared", "line merge: " + (", ".join(missing) if missing else "already satisfied")))
            continue

        strategy = policy["structured_merge"].get(rel)
        if strategy == "agent-module-router" and destination.is_file():
            merged, detail = just_router_merge(destination.read_text(encoding="utf-8"))
            if merged is None:
                blockers.append(f"{rel}: {detail}")
                actions.append(Action(rel, "blocked", "shared", detail))
            else:
                actions.append(Action(rel, "merge" if merged != destination.read_text(encoding="utf-8") else "noop", "shared", detail))
            continue
        if strategy == "agent-rules-block" and destination.is_file():
            merged, detail = agent_rules_merge(destination.read_text(encoding="utf-8"), entry.source.read_text(encoding="utf-8"))
            if merged is None:
                blockers.append(f"{rel}: {detail}")
                actions.append(Action(rel, "blocked", "shared", detail))
            else:
                actions.append(Action(rel, "merge" if merged != destination.read_text(encoding="utf-8") else "noop", "shared", detail))
            continue

        detail = "non-identical existing path has no safe adoption merge strategy"
        blockers.append(f"{rel}: {detail}")
        actions.append(Action(rel, "blocked", owner, detail))

    dirty = bool(run(["git", "status", "--porcelain"], cwd=root).stdout.strip())
    version = (source / "components" / "agent-core" / ".automation" / "VERSION")
    version_value = version.read_text(encoding="utf-8").strip() if version.is_file() else None
    just_version = required_just_version(source)
    compatibility = target_just_compatibility(root, just_version)
    return {
        "repositoryRoot": str(root),
        "requestedAdapter": requested_adapter,
        "selectedAdapter": adapter,
        "adapterSelectionReason": reason,
        "adapterCandidates": detected,
        "agentCoreVersion": version_value,
        "targetJustCompatibility": compatibility,
        "workingTreeDirty": dirty,
        "actions": [asdict(action) for action in actions],
        "blockers": blockers,
        "canApply": not blockers and not dirty,
    }


def apply_plan(source: Path, target: Path, requested_adapter: str) -> dict:
    root = repository_root(target)
    with exclusive_adoption_lock(root):
        return apply_plan_locked(source, root, requested_adapter)


def apply_plan_locked(source: Path, target: Path, requested_adapter: str) -> dict:
    plan = build_plan(source, target, requested_adapter)
    if plan["workingTreeDirty"]:
        raise AdoptionError("adoption refused: target working tree is dirty")
    if plan["blockers"]:
        raise AdoptionError("adoption blocked:\n- " + "\n- ".join(plan["blockers"]))

    root = Path(plan["repositoryRoot"])
    adapter = plan["selectedAdapter"]
    policy = load_policy(source, adapter)
    entries = compose_entries(source, TemplateSpec("adoption", adapter, "existing repository adoption"))
    action_by_path = {item["path"]: item for item in plan["actions"]}

    for relative, entry in sorted(entries.items(), key=lambda item: item[0].as_posix()):
        rel = relative.as_posix()
        destination = root / relative
        action = action_by_path[rel]["action"]
        unsafe = unsafe_destination_reason(root, destination)
        if unsafe:
            raise AdoptionError(f"adoption destination became unsafe for {rel}: {unsafe}")
        if action == "noop":
            current = read_regular_file(root, relative)
            if action_by_path[rel]["reason"] == "existing content is identical":
                valid = current == entry.source.read_bytes()
            else:
                try:
                    text = current.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise AdoptionError(f"adoption no-op destination changed during apply: {rel}") from exc
                if rel in policy["line_merge"]:
                    _, missing = line_merge(text, policy["line_merge"][rel])
                    valid = not missing
                else:
                    strategy = policy["structured_merge"].get(rel)
                    if strategy == "agent-module-router":
                        merged, _ = just_router_merge(text)
                    elif strategy == "agent-rules-block":
                        merged, _ = agent_rules_merge(
                            text,
                            entry.source.read_text(encoding="utf-8"),
                        )
                    else:
                        merged = None
                    valid = merged == text
            if not valid:
                raise AdoptionError(f"adoption no-op destination changed during apply: {rel}")
            continue
        if action == "preserve":
            read_regular_file(root, relative)
            continue
        if action == "create":
            create_regular_file(root, relative, entry.source)
            continue
        if action == "merge":
            if rel in policy["line_merge"]:
                merge_regular_text(
                    root,
                    relative,
                    lambda current: (
                        line_merge(current, policy["line_merge"][rel])[0],
                        "line merge",
                    ),
                )
                continue
            strategy = policy["structured_merge"].get(rel)
            if strategy == "agent-module-router":
                merger = just_router_merge
            elif strategy == "agent-rules-block":
                core_rules = entry.source.read_text(encoding="utf-8")
                merger = lambda current: agent_rules_merge(current, core_rules)
            else:
                raise AdoptionError(f"unexpected merge action for {rel}")
            merge_regular_text(root, relative, merger)
            continue
        raise AdoptionError(f"unexpected action {action!r} for {rel}")

    return {
        "applied": True,
        "repositoryRoot": str(root),
        "adapter": adapter,
        "agentCoreVersion": plan["agentCoreVersion"],
        "targetJustCompatibility": plan["targetJustCompatibility"],
        "commitCreated": False,
        "pushPerformed": False,
        "mergePerformed": False,
    }


def migration_plan(source: Path, target: Path, adapter: str) -> dict:
    root = repository_root(target)
    current_path = root / ".automation" / "ADAPTER"
    if not current_path.is_file():
        raise AdoptionError("repository is not Agent-ready: missing .automation/ADAPTER")
    current = current_path.read_text(encoding="utf-8").strip()
    plan = build_plan(source, root, adapter)
    plan["migration"] = {"from": current, "to": plan["selectedAdapter"]}
    if current == plan["selectedAdapter"]:
        plan["blockers"].append("selected adapter is already active")
        plan["canApply"] = False
    return plan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Adopt Agent Core into an existing Git repository")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument("target", type=Path)
        command.add_argument("--adapter", default="auto")
    migrate = sub.add_parser("migrate-plan")
    migrate.add_argument("target", type=Path)
    migrate.add_argument("--adapter", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    source = templates_root()
    try:
        if args.command == "plan":
            result = build_plan(source, args.target.resolve(), args.adapter)
        elif args.command == "apply":
            result = apply_plan(source, args.target.resolve(), args.adapter)
        elif args.command == "migrate-plan":
            result = migration_plan(source, args.target.resolve(), args.adapter)
        else:  # pragma: no cover
            raise AdoptionError(f"unsupported command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except AdoptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
