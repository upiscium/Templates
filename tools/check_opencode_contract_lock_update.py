#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

TARGET_INPUT = "opencodeContract"
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class LockUpdateError(RuntimeError):
    pass


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockUpdateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_without_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise LockUpdateError(f"cannot read lock file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LockUpdateError(f"lock file must be a JSON object: {path}")
    if not isinstance(value.get("version"), int):
        raise LockUpdateError(f"lock file has invalid version: {path}")
    if not isinstance(value.get("root"), str):
        raise LockUpdateError(f"lock file has invalid root node identity: {path}")
    nodes = value.get("nodes")
    if not isinstance(nodes, dict):
        raise LockUpdateError(f"lock file has invalid nodes mapping: {path}")
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise LockUpdateError(f"lock file has invalid node: {node_id!r}")
        if "inputs" in node and not isinstance(node["inputs"], dict):
            raise LockUpdateError(f"lock node has invalid inputs mapping: {node_id}")
    if value["root"] not in nodes:
        raise LockUpdateError(f"lock root node is missing: {value['root']}")
    return value


class LockGraph:
    def __init__(self, lock: dict[str, Any]) -> None:
        self.lock = lock
        self.nodes: dict[str, dict[str, Any]] = lock["nodes"]
        self.root: str = lock["root"]
        self.edges = {
            node_id: {
                self.resolve_reference(reference)
                for reference in node.get("inputs", {}).values()
            }
            for node_id, node in self.nodes.items()
        }

    def resolve_reference(
        self,
        reference: Any,
        active: frozenset[tuple[str, str]] = frozenset(),
    ) -> str:
        if isinstance(reference, str):
            if reference not in self.nodes:
                raise LockUpdateError(f"dangling lock node reference: {reference}")
            return reference
        if not isinstance(reference, list) or not reference:
            raise LockUpdateError(f"invalid lock input reference: {reference!r}")
        current = self.root
        for component in reference:
            if not isinstance(component, str) or not component:
                raise LockUpdateError(f"invalid follows path component: {component!r}")
            current = self.resolve_input(current, component, active)
        return current

    def resolve_input(
        self,
        node_id: str,
        input_name: str,
        active: frozenset[tuple[str, str]],
    ) -> str:
        key = (node_id, input_name)
        if key in active:
            raise LockUpdateError(
                f"cyclic follows reference at node={node_id} input={input_name}"
            )
        inputs = self.nodes[node_id].get("inputs", {})
        if input_name not in inputs:
            raise LockUpdateError(
                f"missing follows path input: node={node_id} input={input_name}"
            )
        return self.resolve_reference(inputs[input_name], active | {key})

    def root_inputs(self) -> dict[str, str]:
        inputs = self.nodes[self.root].get("inputs", {})
        return {
            name: self.resolve_reference(reference)
            for name, reference in inputs.items()
        }

    def reachable(self, starts: set[str]) -> set[str]:
        result: set[str] = set()
        pending = list(starts)
        while pending:
            node_id = pending.pop()
            if node_id in result:
                continue
            result.add(node_id)
            pending.extend(self.edges[node_id] - result)
        return result

    def target_exclusive_nodes(self, target: str) -> set[str]:
        root_inputs = self.root_inputs()
        if target not in root_inputs:
            raise LockUpdateError(f"root input is missing: {target}")
        if "nixpkgs" not in root_inputs:
            raise LockUpdateError("root input is missing: nixpkgs")
        target_nodes = self.reachable({root_inputs[target]})
        other_nodes = self.reachable(
            {node_id for name, node_id in root_inputs.items() if name != target}
        )
        return target_nodes - other_nodes


def revision(lock: dict[str, Any], graph: LockGraph, label: str) -> str:
    target_id = graph.root_inputs().get(TARGET_INPUT)
    if target_id is None:
        raise LockUpdateError(f"{label} lock is missing {TARGET_INPUT}")
    locked = graph.nodes[target_id].get("locked")
    if not isinstance(locked, dict):
        raise LockUpdateError(f"{label} {TARGET_INPUT} node has invalid locked metadata")
    expected = {
        "owner": "upiscium",
        "repo": "OpencodeContract",
        "type": "github",
    }
    for field, expected_value in expected.items():
        if locked.get(field) != expected_value:
            raise LockUpdateError(
                f"{label} {TARGET_INPUT} {field} mismatch: {locked.get(field)!r}"
            )
    value = locked.get("rev")
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        raise LockUpdateError(f"{label} {TARGET_INPUT} revision is invalid: {value!r}")
    return value


def validate_update(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
    if {key: value for key, value in before.items() if key != "nodes"} != {
        key: value for key, value in after.items() if key != "nodes"
    }:
        raise LockUpdateError("top-level lock metadata changed")
    if before["root"] != after["root"]:
        raise LockUpdateError("top-level root node identity changed")

    before_graph = LockGraph(before)
    after_graph = LockGraph(after)
    if before["nodes"][before["root"]] != after["nodes"][after["root"]]:
        raise LockUpdateError("root node or root inputs changed")

    before_revision = revision(before, before_graph, "before")
    after_revision = revision(after, after_graph, "after")
    before_exclusive = before_graph.target_exclusive_nodes(TARGET_INPUT)
    after_exclusive = after_graph.target_exclusive_nodes(TARGET_INPUT)

    before_nodes = before["nodes"]
    after_nodes = after["nodes"]
    for node_id in sorted(set(before_nodes) | set(after_nodes)):
        if before_nodes.get(node_id) == after_nodes.get(node_id):
            continue
        if node_id not in before_nodes:
            allowed = node_id in after_exclusive
        elif node_id not in after_nodes:
            allowed = node_id in before_exclusive
        else:
            allowed = node_id in before_exclusive and node_id in after_exclusive
        if not allowed:
            raise LockUpdateError(
                f"consumer-owned, shared, or unreachable lock node changed: {node_id}"
            )

    before_nixpkgs = before_graph.root_inputs()["nixpkgs"]
    after_nixpkgs = after_graph.root_inputs()["nixpkgs"]
    if before_nixpkgs != after_nixpkgs:
        raise LockUpdateError("root nixpkgs node identity changed")
    if before_nodes[before_nixpkgs] != after_nodes[after_nixpkgs]:
        raise LockUpdateError("root nixpkgs node changed")
    if before_revision == after_revision and before != after:
        raise LockUpdateError(
            "flake.lock changed without an OpencodeContract revision change"
        )
    return before_revision, after_revision


def validate_candidate_files(before_path: Path, after_path: Path) -> tuple[bool, str, str]:
    try:
        before_bytes = before_path.read_bytes()
        after_bytes = after_path.read_bytes()
    except OSError as exc:
        raise LockUpdateError(f"cannot read lock file: {exc}") from exc

    before_revision, after_revision = validate_update(
        load_lock(before_path), load_lock(after_path)
    )
    if before_bytes == after_bytes:
        return False, before_revision, after_revision
    if before_revision == after_revision:
        raise LockUpdateError(
            "flake.lock changed without an OpencodeContract revision change"
        )
    return True, before_revision, after_revision


def validate_existing_branch(
    before_path: Path,
    candidate_path: Path,
    branch_path: Path,
    *,
    candidate_sha256: str,
    expected_revision: str,
    changed_paths: list[str],
) -> tuple[str, str]:
    if changed_paths != ["flake.lock"]:
        rendered = ", ".join(changed_paths) if changed_paths else "<none>"
        raise LockUpdateError(
            f"existing branch must change only flake.lock; changed paths: {rendered}"
        )
    try:
        candidate_bytes = candidate_path.read_bytes()
        branch_bytes = branch_path.read_bytes()
    except OSError as exc:
        raise LockUpdateError(f"cannot read existing branch lock candidate: {exc}") from exc
    if hashlib.sha256(candidate_bytes).hexdigest() != candidate_sha256:
        raise LockUpdateError("validated candidate checksum mismatch")
    if branch_bytes != candidate_bytes:
        raise LockUpdateError("existing branch flake.lock does not match validated candidate")

    changed, before_revision, branch_revision = validate_candidate_files(
        before_path, branch_path
    )
    if not changed:
        raise LockUpdateError("existing branch does not contain a lock update")
    if branch_revision != expected_revision:
        raise LockUpdateError(
            "existing branch OpencodeContract revision does not match validated candidate"
        )
    return before_revision, branch_revision


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate a controlled OpencodeContract flake.lock update"
    )
    result.add_argument("before", type=Path)
    result.add_argument("after", type=Path)
    result.add_argument("--candidate", type=Path)
    result.add_argument("--candidate-sha256")
    result.add_argument("--expected-revision")
    result.add_argument("--changed-path", action="append", default=[])
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.candidate is None:
            if args.candidate_sha256 or args.expected_revision or args.changed_path:
                raise LockUpdateError("existing-branch options require --candidate")
            changed, before_revision, after_revision = validate_candidate_files(
                args.before, args.after
            )
        else:
            if not args.candidate_sha256 or not args.expected_revision:
                raise LockUpdateError(
                    "--candidate requires --candidate-sha256 and --expected-revision"
                )
            before_revision, after_revision = validate_existing_branch(
                args.before,
                args.candidate,
                args.after,
                candidate_sha256=args.candidate_sha256,
                expected_revision=args.expected_revision,
                changed_paths=args.changed_path,
            )
            changed = True
    except LockUpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    status = "VALID" if changed else "NOOP"
    print(
        f"{status} OpencodeContract lock update "
        f"old={before_revision} new={after_revision}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
