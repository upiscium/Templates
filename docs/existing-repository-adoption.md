# Existing repository adoption

Agent-ready repositories always have a Project Adapter. A repository without a dedicated adapter uses the `base` Adapter rather than entering an adapter-less state.

## Commands

From the Templates repository, use the root dev shell so adoption tooling parses with the declared Just version:

```sh
cd /path/to/Templates
nix develop --command just template::adopt-plan /path/to/repository
nix develop --command just template::adopt-plan /path/to/repository base
nix develop --command just template::adopt-apply /path/to/repository base
nix develop --command just template::adapter-migrate-plan /path/to/repository cpp-cmake
```

`adopt-plan` is read-only. It reports the selected Adapter, selection reason, Agent Core version, dirty-state status, planned file actions, and blockers.

`adopt-apply` performs no commit, push, or merge. It refuses to run when the target working tree is dirty or when the plan contains unresolved collisions. Run it only while the target is under exclusive operator ownership; apply takes a non-blocking advisory lock on the repository directory to serialize adoption processes, then revalidates destinations and uses no-follow descriptor traversal with atomic per-file installation.

TypeScript/Node adoption requires repository-owned `package.json` and
`package-lock.json` npm identity. It rejects pnpm, Yarn, and Bun lockfiles
rather than installing dependencies or materializing scaffold package-manager
metadata into an existing repository.

For a Python repository, adoption deliberately leaves dependency resolution outside the generic adoption engine. Existing `flake.lock` and `uv.lock` files are preserved byte-for-byte. After `template::adopt-apply`, run the explicit bootstrap boundary, review any newly generated lockfiles, verify without lockfile updates, and include the accepted bootstrap output in the adoption pull request:

```sh
nix develop --no-write-lock-file --command just project::bootstrap
nix develop --no-update-lock-file --command just project::check
```

The Python bootstrap entry uses `--no-write-lock-file` so outer Nix resolution cannot update an existing `flake.lock` or materialize a missing one before the Adapter runs. Python `project::bootstrap` resolves the project name before creating only missing lockfiles. Post-bootstrap verification instead uses `--no-update-lock-file`; `project::check` uses `uv run --locked`, and `/init` and verification do not create or update lockfiles. `just project::python::sync` remains the explicit dependency synchronization operation and may update `uv.lock` when project metadata requires it.

After applying Agent Core on an existing bootstrap/adoption branch, `just agent::preflight` can verify tools, required files, VERSION, and Adapter readiness without requiring fabricated Task State. This is not full initialization: `just agent::doctor`, `just agent::context`, and `/init` retain strict branch/Task identity requirements and may intentionally block until the repository is on its default branch or in a registered Task worktree.

## Adapter selection

Explicit `--adapter <id>` always wins when the Adapter exists.

Auto selection uses only dedicated Adapters that are actually present in the current Templates source. Known marker examples are `CMakeLists.txt`, `pyproject.toml`, `Cargo.toml`, `package.json`, and `flake.nix`.

When no dedicated Adapter matches, or more than one dedicated Adapter matches, selection falls back to `base`. It does not guess which dedicated Adapter the repository intended to use.

## Ownership and collisions

Adoption classifies each generated path before changing the repository.

- `create`: path is absent and may be materialized.
- `noop`: existing content already matches.
- `preserve`: the Adapter adoption policy leaves the repository-owned file unchanged.
- `merge`: a specifically defined safe merge strategy exists.
- `blocked`: non-identical content has no safe merge strategy.

The base Adapter preserves existing `flake.nix` and `flake.lock` files. It line-merges `/.worktrees/` into `.gitignore`.

Existing `Justfile` content is retained and only non-conflicting Agent Core module declarations are appended. Existing `AGENTS.md` content is retained and Agent Core rules are appended inside explicit markers.

Existing non-identical `opencode.json`, `.automation/**`, or other Automation Core-owned paths are not silently overwritten. They block adoption until the collision is resolved intentionally.

## Base Adapter

The base Adapter is the minimum Project Adapter contract for unknown repositories. It provides the stable `project::*` API without inventing project-specific verification:

```text
project::doctor        PASS
project::format-check  SKIPPED
project::lint          SKIPPED
project::test          SKIPPED
project::build         SKIPPED
project::check         PASS
```

A skipped project-specific operation remains explicitly `SKIPPED`; it is not represented as work that actually ran.

After adoption, the repository contains the normal Agent Core version, Adapter marker, initialization contract, OpenCode agents, guarded Just API, and worktree lifecycle. The normal read-only `/init` contract applies immediately.

## Migration to a dedicated Adapter

`adapter-migrate-plan` is read-only. It compares the currently active Adapter with the requested target Adapter.

A path currently owned by the active Adapter is replaceable only when the repository copy still matches that Adapter's source. If the repository modified an Adapter-owned path, migration reports a blocker instead of overwriting it.

Agent Core files are not replaced by Adapter migration. Repository extensions are outside Adapter ownership and remain protected.

Actual Adapter migration application is intentionally separate from planning so that future dedicated Adapters can define their ownership and migration rules without weakening the collision policy.
