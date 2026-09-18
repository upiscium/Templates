#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tomllib
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
    current = root
    for part in destination.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return f"destination path traverses symlink: {current.relative_to(root)}"
    try:
        destination.parent.resolve(strict=False).relative_to(root)
    except ValueError:
        return "destination parent resolves outside repository root"
    return None


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

        if owner.startswith("adapter:") and matches(rel, policy["preserve_existing"]):
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
        if action in {"noop", "preserve"}:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if action == "create":
            if entry.source.is_symlink():
                os.symlink(os.readlink(entry.source), destination)
            else:
                shutil.copy2(entry.source, destination, follow_symlinks=False)
            continue
        if action == "merge":
            if rel in policy["line_merge"]:
                merged, _ = line_merge(destination.read_text(encoding="utf-8"), policy["line_merge"][rel])
                destination.write_text(merged, encoding="utf-8")
                continue
            strategy = policy["structured_merge"].get(rel)
            if strategy == "agent-module-router":
                merged, detail = just_router_merge(destination.read_text(encoding="utf-8"))
            elif strategy == "agent-rules-block":
                merged, detail = agent_rules_merge(destination.read_text(encoding="utf-8"), entry.source.read_text(encoding="utf-8"))
            else:
                raise AdoptionError(f"unexpected merge action for {rel}")
            if merged is None:
                raise AdoptionError(f"merge became unsafe for {rel}: {detail}")
            destination.write_text(merged, encoding="utf-8")
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
