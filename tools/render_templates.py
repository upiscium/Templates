#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
IGNORED_DIRECTORY_NAMES = {"__pycache__"}
IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}


class CompositionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    adapter: str
    description: str


@dataclass(frozen=True)
class SourceEntry:
    component: str
    source: Path
    relative: Path
    kind: str


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_manifest(root: Path) -> dict[str, TemplateSpec]:
    path = root / "templates" / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompositionError(f"missing template manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CompositionError(f"invalid template manifest: {exc}") from exc

    if set(data) != {"templates"} or not isinstance(data["templates"], dict):
        raise CompositionError("manifest must contain exactly one object key: 'templates'")

    specs: dict[str, TemplateSpec] = {}
    for name, raw in data["templates"].items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise CompositionError(f"invalid template name: {name!r}")
        if not isinstance(raw, dict) or set(raw) != {"adapter", "description"}:
            raise CompositionError(
                f"template {name!r} must contain exactly 'adapter' and 'description'"
            )
        adapter = raw["adapter"]
        description = raw["description"]
        if not isinstance(adapter, str) or not NAME_RE.fullmatch(adapter):
            raise CompositionError(f"invalid adapter name for {name!r}: {adapter!r}")
        if not isinstance(description, str) or not description.strip():
            raise CompositionError(f"template {name!r} requires a non-empty description")
        specs[name] = TemplateSpec(name=name, adapter=adapter, description=description)
    return specs


def _iter_entries(component: str, root: Path) -> Iterable[SourceEntry]:
    if not root.is_dir():
        raise CompositionError(f"missing component directory: {root}")

    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            directory for directory in directories if directory not in IGNORED_DIRECTORY_NAMES
        )
        files.sort()

        symlink_directories = [
            directory for directory in directories if (current_path / directory).is_symlink()
        ]
        for directory in symlink_directories:
            source = current_path / directory
            yield SourceEntry(component, source, source.relative_to(root), "symlink")
            directories.remove(directory)

        for filename in files:
            source = current_path / filename
            if source.suffix in IGNORED_FILE_SUFFIXES:
                continue
            kind = "symlink" if source.is_symlink() else "file"
            yield SourceEntry(component, source, source.relative_to(root), kind)


def _has_parent_entry(relative: Path, entries: dict[Path, SourceEntry]) -> Path | None:
    parent = relative.parent
    while parent != Path("."):
        if parent in entries:
            return parent
        parent = parent.parent
    return None


def compose_entries(root: Path, spec: TemplateSpec) -> dict[Path, SourceEntry]:
    sources = [
        ("agent-core", root / "components" / "agent-core"),
        (f"adapter:{spec.adapter}", root / "components" / "adapters" / spec.adapter),
    ]
    entries: dict[Path, SourceEntry] = {}

    for component, source_root in sources:
        for entry in _iter_entries(component, source_root):
            relative = entry.relative
            if relative in entries:
                previous = entries[relative]
                raise CompositionError(
                    f"path collision at {relative}: {previous.component} and {entry.component}"
                )
            parent_entry = _has_parent_entry(relative, entries)
            if parent_entry is not None:
                previous = entries[parent_entry]
                raise CompositionError(
                    f"path collision: {previous.component}:{parent_entry} blocks "
                    f"{entry.component}:{relative}"
                )
            prefix = f"{relative.as_posix()}/"
            child = next(
                (path for path in entries if path.as_posix().startswith(prefix)), None
            )
            if child is not None:
                previous = entries[child]
                raise CompositionError(
                    f"path collision: {entry.component}:{relative} blocks "
                    f"{previous.component}:{child}"
                )
            entries[relative] = entry
    return entries


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def render_template(root: Path, spec: TemplateSpec, destination: Path | None = None) -> Path:
    destination = destination or root / "templates" / spec.name
    entries = compose_entries(root, spec)

    _remove_path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    for relative in sorted(entries, key=lambda item: item.as_posix()):
        entry = entries[relative]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "symlink":
            os.symlink(os.readlink(entry.source), target)
        else:
            shutil.copy2(entry.source, target, follow_symlinks=False)
    return destination


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root: Path) -> dict[str, tuple[str, str, bool]]:
    if not root.is_dir():
        return {}
    result: dict[str, tuple[str, str, bool]] = {}
    for entry in _iter_entries("snapshot", root):
        key = entry.relative.as_posix()
        if entry.kind == "symlink":
            result[key] = ("symlink", os.readlink(entry.source), False)
        else:
            executable = bool(stat.S_IMODE(entry.source.stat().st_mode) & 0o111)
            result[key] = ("file", _file_digest(entry.source), executable)
    return result


def diff_snapshots(
    expected: dict[str, tuple[str, str, bool]],
    actual: dict[str, tuple[str, str, bool]],
) -> list[str]:
    differences: list[str] = []
    expected_keys = set(expected)
    actual_keys = set(actual)
    for path in sorted(expected_keys - actual_keys):
        differences.append(f"missing: {path}")
    for path in sorted(actual_keys - expected_keys):
        differences.append(f"unexpected: {path}")
    for path in sorted(expected_keys & actual_keys):
        if expected[path] != actual[path]:
            differences.append(f"changed: {path}")
    return differences


def check_template(root: Path, spec: TemplateSpec) -> list[str]:
    committed = root / "templates" / spec.name
    if not committed.is_dir():
        return [f"missing generated template directory: templates/{spec.name}"]
    with tempfile.TemporaryDirectory(prefix=f"template-{spec.name}-") as directory:
        rendered = Path(directory) / spec.name
        render_template(root, spec, rendered)
        return diff_snapshots(snapshot(rendered), snapshot(committed))


def unexpected_template_directories(root: Path, specs: dict[str, TemplateSpec]) -> list[str]:
    templates_root = root / "templates"
    if not templates_root.is_dir():
        return []
    expected = set(specs)
    unexpected = []
    for path in sorted(templates_root.iterdir(), key=lambda item: item.name):
        if path.is_dir() and path.name not in expected:
            unexpected.append(path.name)
    return unexpected


def command_render(root: Path, template: str) -> int:
    specs = load_manifest(root)
    try:
        spec = specs[template]
    except KeyError as exc:
        available = ", ".join(sorted(specs)) or "<none>"
        raise CompositionError(
            f"unknown template {template!r}; available templates: {available}"
        ) from exc
    destination = render_template(root, spec)
    print(f"rendered {spec.name} -> {destination.relative_to(root)}")
    return 0


def command_render_all(root: Path) -> int:
    specs = load_manifest(root)
    for name in sorted(specs):
        render_template(root, specs[name])
        print(f"rendered {name}")
    return 0


def command_check(root: Path) -> int:
    specs = load_manifest(root)
    failed = False
    for name in sorted(specs):
        differences = check_template(root, specs[name])
        if differences:
            failed = True
            print(f"template drift: {name}", file=sys.stderr)
            for difference in differences:
                print(f"  - {difference}", file=sys.stderr)
        else:
            print(f"ok: {name}")

    extras = unexpected_template_directories(root, specs)
    if extras:
        failed = True
        print("unregistered generated template directories:", file=sys.stderr)
        for name in extras:
            print(f"  - templates/{name}", file=sys.stderr)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose Agent Core and Project Adapter templates"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="render one template")
    render.add_argument("template")
    subparsers.add_parser("render-all", help="render all registered templates")
    check = subparsers.add_parser("check", help="check committed generated templates for drift")
    check.add_argument("--root", type=Path, help="source worktree to check (trusted launcher only)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve() if args.command == "check" and args.root else repository_root()
    try:
        if args.command == "render":
            return command_render(root, args.template)
        if args.command == "render-all":
            return command_render_all(root)
        if args.command == "check":
            return command_check(root)
        raise AssertionError(f"unhandled command: {args.command}")
    except CompositionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
