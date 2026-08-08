# Templates

Nix flake templates for reproducible, Agent-ready development environments.

## Start a new repository

Published templates:

```sh
nix flake init -t github:upiscium/Templates#agent-base
nix flake init -t github:upiscium/Templates#agent-python
nix flake init -t github:upiscium/Templates#agent-rust
nix flake init -t github:upiscium/Templates#agent-nix
nix flake init -t github:upiscium/Templates#agent-cpp-cmake
```

Compatibility aliases remain available:

```text
python -> agent-python
rust   -> agent-rust
```

Every Agent-ready repository contains the shared Agent Core plus exactly one Project Adapter. Adapter-less repositories are not supported; unknown projects use `base` as the minimum contract.

After instantiating a generated language/toolchain template, run the one-time project bootstrap before the first validation or development session:

```sh
nix develop --command just project::bootstrap
```

The optional explicit project name can be supplied when the directory name is not the desired project name:

```sh
nix develop --command just project::bootstrap my-project
```

`project::bootstrap` is the explicit state-changing setup step. It resolves generated project-name placeholders and is idempotent. It does not commit, push, or merge. After bootstrap, initialize Git as needed and use `/init` or the corresponding Just checks for normal read-only session validation.

## Repository layers

```text
Agent Core
  .automation/**, .opencode/**, AGENTS.md, root Justfile, opencode.json

Project Adapter
  just/project/**, language/toolchain manifests, INIT.fragment.md, ADAPTER

Repository extension
  just/project/repository.just and repository-owned build/configuration files

Local extension
  just/local.just (optional, repository-specific convenience API)
```

Generated files under `templates/<name>/` are artifacts. Edit `components/agent-core/` or `components/adapters/<adapter>/` and regenerate instead.

## Initialization

Bootstrap and initialization are intentionally separate:

```text
nix flake init ...
  -> just project::bootstrap   # one-time, state-changing project setup
  -> /init                     # every-session, read-only validation
```

`/init` is read-only. It validates Agent Core version, Adapter identity, branch/worktree/Task State, tools, project doctor, HEAD, and Git status. It never bootstraps, repairs, installs packages, changes Task State, or rewrites `AGENTS.md`.

Existing-repository adoption and Agent Core upgrade are separate mutating workflows from generated-project bootstrap.

## Existing repositories

Plan first:

```sh
just template::adopt-plan /path/to/repository
```

Apply with an explicit Adapter when appropriate:

```sh
just template::adopt-apply /path/to/repository base
just template::adopt-apply /path/to/repository python
```

Auto-detection prefers CMake/Python/Rust markers; a standalone `flake.nix` selects Nix; unknown or ambiguous repositories fall back to `base`.

Migrate a base-adopted repository by inspecting the read-only migration plan before changing Adapter-owned paths:

```sh
just template::adapter-migrate-plan /path/to/repository python
```

Adoption/migration never commits, pushes, merges, stashes, or resets the target repository.

## Task and worktree lifecycle

Main schedules Tasks. Each Task owns one branch, one repo-local worktree under `.worktrees/`, one disposable `.task-state/task.md`, and one Task Orchestrator. Leaf agents cannot delegate or mutate Task State.

Typical flow:

```text
Main
  -> task-start
  -> Task Orchestrator implements/verifies
  -> guarded commit
  -> Ask: push
  -> Draft PR
  -> integration check
  -> Ask: merge (Main only)
  -> cleanup
```

Raw Git/GitHub writes are denied. Stable Just APIs provide the guarded write path. Push, merge, cleanup, unknown Bash, and designated external paths require Ask.

## Project Adapter API

Generated language/toolchain Adapters expose the one-time bootstrap API plus the stable validation/build API:

```text
just project::bootstrap [name]
just project::doctor
just project::format-check
just project::lint
just project::test
just project::build
just project::check
```

Adapters may expose additional guarded APIs such as `project::eval` or `project::configure`, but broad raw tool commands are not automatically allowed.

To add an Adapter, create `components/adapters/<id>/` with `.automation/ADAPTER`, `.automation/INIT.fragment.md`, `just/project/mod.just`, adoption policy, required project files, and a manifest entry. Add source/generated parity tests and generated-template CI smoke coverage.

## Agent Core version and upstream

Generated repositories contain:

```text
.automation/VERSION
.automation/UPSTREAM
```

Inspect them through:

```sh
just automation::version
```

`UPSTREAM` records the canonical Templates repository/ref/component. Breaking Agent Core changes require a VERSION change and migration notes; compatible implementation/documentation changes may remain within the current version.

## Read-only update check

The repository never fetches or executes upstream code automatically. Check against a trusted local Templates checkout:

```sh
just automation::check-update /path/to/Templates
```

This reports current/upstream versions and the ownership boundaries without mutating the repository. Agent Core upgrades must never be silently mixed into an ordinary Task.

## Agent Core upgrade

Use a dedicated Task worktree. The upgrade command is an Ask operation and additionally requires an explicit maintenance marker:

```sh
export AUTOMATION_MAINTENANCE=1
just automation::upgrade /path/to/Templates
```

It refuses the default branch and repositories without Task State. It materializes only Agent Core-owned paths and preserves Adapter-owned `.automation/ADAPTER`, `.automation/INIT.fragment.md`, `.automation/adoption.toml`, `just/project/**`, local modules, and repository CI.

Upgrade does not commit, push, or merge. Before publication, inspect the diff and run at minimum:

```sh
git diff --check
just agent::doctor
just project::check
```

Then require the repository CI/smoke suite. Automation Core changes must be reviewed as a dedicated maintenance change.

## Template development

```sh
just template::render agent-base
just template::render agent-python
just template::render agent-rust
just template::render agent-nix
just template::render agent-cpp-cmake
just template::render-all
just template::check
```

`template::check` detects generated drift, path collisions, dotfile/mode drift, and unregistered generated directories.

## OpenCode hierarchy

The default Main agent orchestrates Tasks; Task Orchestrators own one Task; leaf agents perform bounded work and cannot re-delegate. Model fallback is role-scoped and only automatic for classified usage/quota/rate-limit failures. Depth-2 Ask behavior has a separate reproducible manual smoke procedure under `docs/opencode-depth2-ask-smoke.md` and is not represented as PASS until executed.
