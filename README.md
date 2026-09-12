# Templates

Nix flake templates for reproducible, Agent-ready development environments.

## Start a new repository

There are two new-repository distribution channels. Use a GitHub Template Repository when creating the repository on GitHub, or use the Nix flake template when initializing a local directory.

GitHub Template Repository targets:

```text
agent-python     -> upiscium/Template-Agent-Python
agent-rust       -> upiscium/Template-Agent-Rust
agent-nix        -> upiscium/Template-Agent-Nix
agent-cpp-cmake  -> upiscium/Template-Agent-Cpp-CMake
agent-typescript-node -> upiscium/Template-Agent-TypeScript-Node
```

The GitHub distribution repositories are generated artifacts synchronized from this repository; they are not independent sources of truth. See `docs/github-template-distribution.md` for setup, publishing, and ownership details.

Local Nix templates remain available:

```sh
nix flake init -t github:upiscium/Templates#agent-base
nix flake init -t github:upiscium/Templates#agent-python
nix flake init -t github:upiscium/Templates#agent-rust
nix flake init -t github:upiscium/Templates#agent-nix
nix flake init -t github:upiscium/Templates#agent-cpp-cmake
nix flake init -t github:upiscium/Templates#agent-typescript-node
```

Compatibility aliases remain available:

```text
python -> agent-python
rust   -> agent-rust
```

Every Agent-ready repository contains the shared Agent Core plus exactly one Project Adapter. Adapter-less repositories are not supported; unknown projects use `base` as the minimum contract.

After instantiating a generated non-Python language/toolchain template, run the one-time project bootstrap before the first validation or development session:

```sh
nix develop --command just project::bootstrap
```

For the Python template, prevent the outer Nix invocation from writing `flake.lock`; the Python Adapter bootstrap owns explicit materialization of missing `flake.lock` and `uv.lock` files:

```sh
nix develop --no-write-lock-file --command just project::bootstrap
```

The optional explicit project name can be supplied when the directory name is not the desired project name. Preserve the same Python-specific flag when using it:

```sh
nix develop --command just project::bootstrap my-project
nix develop --no-write-lock-file --command just project::bootstrap my-project
```

`project::bootstrap` is the explicit state-changing setup step. It resolves generated project-name placeholders and is idempotent. Python uses `--no-write-lock-file` so outer `nix develop` may resolve inputs but cannot take ownership of lockfile mutation; the Adapter then creates missing repository-owned `flake.lock` and `uv.lock` files without refreshing existing lockfiles. It does not commit, push, merge, or change GitHub repository settings. Include bootstrap output in the initial commit, then use `nix develop --no-update-lock-file --command ...` and the corresponding Just checks for normal read-only validation; Python verification uses `uv run --locked` and does not repair missing or stale dependency identity.

## Repository policy

Generated Agent-ready repositories declare a GitHub repository policy in `.automation/repository-policy.json`. The policy expects `main` as the default branch and protects the default branch with a repository ruleset: every change must arrive through a pull request, approving reviews are optional (`0` required), deletion and force pushes are blocked, and no bypass actor is configured.

Repository policy is deliberately separate from project bootstrap and `/init`. Inspect the current GitHub repository read-only with:

```sh
just repository::policy-check
```

Apply the declared policy explicitly with:

```sh
just repository::policy-apply
```

`repository::policy-apply` acts only on the GitHub repository resolved from the current checkout, requires GitHub Administration write permission, and is an Ask operation in OpenCode. If `main` does not exist, it refuses to invent or rename a branch; establish `main` explicitly first. Policy mutation is also refused from Task worktrees. See `.automation/REPOSITORY_POLICY.md` in generated repositories for the complete contract.

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

Bootstrap, GitHub repository policy setup, and session initialization are intentionally separate:

```text
nix flake init ...
  -> just project::bootstrap          # one-time, state-changing project setup
  -> just repository::policy-apply    # optional explicit GitHub repository setup
  -> /init                            # every-session, read-only validation
```

`/init` is read-only. It validates Agent Core version, Adapter identity, branch/worktree/Task State, tools, project doctor, HEAD, and Git status. It never bootstraps, repairs, installs packages, changes Task State, rewrites `AGENTS.md`, or mutates GitHub repository settings.

For bootstrap, adoption, or upgrade diagnostics that need only runtime readiness, run `just agent::preflight` from the root of an installed Agent Core. It checks required tools/files, Agent Core version, and a non-empty Adapter marker without resolving or relaxing branch/Task identity. Preflight is read-only, but `preflight = PASS` does not mean the checkout is a valid Agent session: `/init`, `agent::doctor`, and `agent::context` remain strict and may intentionally block a non-default branch without registered Task State.

Existing-repository adoption and Agent Core upgrade are separate mutating workflows from generated-project bootstrap.

## Existing repositories

Plan first:

```sh
cd /path/to/Templates
nix develop --command just template::adopt-plan /path/to/repository
```

Apply with an explicit Adapter when appropriate:

```sh
cd /path/to/Templates
nix develop --command just template::adopt-apply /path/to/repository base
nix develop --command just template::adopt-apply /path/to/repository python
```

Auto-detection prefers CMake/Python/Rust/TypeScript-Node markers; a standalone `flake.nix` selects Nix; unknown or ambiguous repositories fall back to `base`.

Migrate a base-adopted repository by inspecting the read-only migration plan before changing Adapter-owned paths:

```sh
cd /path/to/Templates
nix develop --command just template::adapter-migrate-plan /path/to/repository python
```

Adoption/migration never commits, pushes, merges, stashes, or resets the target repository.


## Planning Agent

Generated Agent-ready repositories include a repository-local `plan` agent for read-only planning. It is primary, uses `openai/gpt-5.6-sol`, denies `edit` and `bash`, allows `question` for consequential requirement clarification, and may delegate only to read-only inspection leaves: `explore`, `architect`, `reviewer`, and `security-reviewer`.

`plan` never starts the Task lifecycle, edits Task State, runs executable doctor/check commands, or reports unexecuted verification as PASS. It reads `AGENTS.md`, `.automation/INIT.md`, adapter initialization guidance, and optional Task State, then returns confirmed facts, assumptions, open decisions, a bounded implementation plan, `execution_prerequisites`, and `verification_handoff` entries for an execution-capable workflow.

## Task and worktree lifecycle

Main schedules Tasks. Each Task owns one branch, one repo-local worktree under `.worktrees/`, one disposable `.task-state/task.md`, and one Task Orchestrator. Leaf agents cannot delegate or mutate Task State.

For downstream-sensitive Agent Core changes, use the documented release gate
from Issue through exact-revision AKV dogfood and post-merge tree equality:
[`docs/agent-core-release-gate.md`](docs/agent-core-release-gate.md). It is
human-gated and does not automate merge or release publication.

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
  -> guarded post-merge finalize and default-branch sync (Main only)
  -> Ask: cleanup
  -> dependency re-evaluation and next Task
```

Raw Git/GitHub writes are denied. Stable Just APIs provide the guarded write path. After merge, Main uses `just integrate::finalize <task> <pr>` to bind actual GitHub merge evidence to a narrow fast-forward-only default-branch synchronization and the dedicated `integration-pending -> merged` transition. `task-start` repeats that synchronization at execution time. Push, merge, cleanup, unknown Bash, and designated external paths require Ask; finalization is non-destructive and cleanup remains separate.

### Issue #112: source-side first-adoption finalization

When a consumer's installed Agent Core is too old to run its maintenance
finalizer, run this source-side bootstrap from a clean, trusted Templates
source checkout at the exact expected implementation revision:

```sh
just agent-core::maintenance-finalize <consumer-main-worktree> <task> <pr> <expected-implementation-revision>
```

The source checkout must be the trusted Templates worktree at the exact full
`HEAD` named by `<expected-implementation-revision>`, with the relevant source
clean. The target must be the consumer's actual clean default-branch/Main
worktree, not a Task worktree or a source checkout. The bridge verifies the
source bootstrap and loads the maintenance implementation from verified Git
blobs at that revision; it does not copy or execute live source files.
The launcher requires `python3` to resolve either directly from the immutable
Nix store, through the root-controlled NixOS system/default profile, or from
the trusted `/usr/bin/python3` system installation. It canonicalizes approved
profile symlinks and executes only the resolved immutable store/system target.

The command performs the canonical contract, receipt, publication, review,
repository, and worktree validations, re-reads the exact merged PR and its
publication metadata, and synchronizes the consumer default branch
fast-forward-only. It revalidates the merge and publication evidence after
synchronization, then records the dedicated maintenance terminal transition
`initialized -> merged`. A repeated invocation is exactly idempotent: it
requires the same finalized publication evidence and returns the already-
finalized result without a second lifecycle transition. Cleanup remains a
separate approval-gated operation. Ordinary consumers continue to use
`just automation::maintenance-finalize <task> <pr>`; this bridge adds no generic authority
or arbitrary consumer mutation surface. AKV #22/#23 are the motivating example
for this first-adoption path; that example is not claimed as executed. Agent
Core VERSION remains 3.

### Issues #131 and #148: source-side publication recovery

An already-complete in-flight Task can be publication-blocked when its installed
Agent Core predates a correction to canonical publication semantics. After that
correction is released, an operator may use a clean Templates checkout at the
exact full release implementation revision:

```sh
just agent-core::publication-recover <consumer-task-worktree> <task> <expected-implementation-revision>
```

This is not a normal publication route. Ordinary Tasks continue to use
`just agent::pr-prepare` followed by `just agent::pr-create`. Recovery requires
an exact registered non-default Task worktree in `publication-ready`,
`draft-pr-created`, or a narrowly recoverable publication-only `blocked` state,
a clean product tree, identical local and remote Task HEADs,
a resolved canonical Task Contract, fresh persisted verification for that HEAD,
and effective completed review evidence. It executes canonical `pr_prepare`,
then routes to canonical `pr_create` only for `publication-ready` with no PR, or
canonical `pr_edit` when the exact Task PR already exists. A
`draft-pr-created` Task without its existing Draft fails closed and never
recreates a PR. A `blocked` Task is not generally publishable: recovery first
requires an existing exact same-repository OPEN Draft for the Task branch,
default base, and current head, plus current canonical verification and
effective completed review evidence. The bridge captures and revalidates that
Draft before a dedicated locked compare-and-swap changes only `blocked ->
publication-ready`; a missing or changed Draft fails before that state mutation.
Before that durable transition, it publishes a protected Git-private recovery
receipt bound to repository, Task/worktree, branch, exact HEAD, base, original
PR number, Task State, and evidence digests. Retries from `blocked`,
`publication-ready`, or `draft-pr-created` must resolve that same receipt-bound
Draft; absence or replacement fails closed and cannot select `pr_create`. It
then uses canonical `pr_prepare` and number-bound `pr_edit`. It never creates
a PR for a `blocked` Task, and the generic lifecycle/state-set transition table
does not gain a bypass. Canonical modules are loaded only from verified immutable
Templates commit blobs; consumer `.automation/bin` files are not publication
authority.

The operation never upgrades the consumer or changes its product HEAD. Work
Unit, verification, and contract evidence remain byte-identical. Starting from
`publication-ready`, only ignored publication metadata and the guarded
`publication-ready -> draft-pr-created` transition may change; starting from
`draft-pr-created`, Task State remains byte-identical. Starting from qualifying
`blocked`, only the status transitions through `publication-ready` to
`draft-pr-created`; all other Task State bytes and optional Issue evidence remain
unchanged. An interruption after the first transition can safely retry through
the receipt-bound publication-ready path. The receipt is consumed only after
exact Draft validation and convergence to `draft-pr-created`, so successful
recovery leaves no stale authority. The exact receipt-bound canonical Draft is
re-read immediately before consumption; replacement retains the receipt and
fails closed. Existing-PR repair
delegates the mutation boundary to canonical `pr_edit`, which requires the
exact same-repository OPEN Draft at the
captured branch, base, and current head and binds its internal lookup to the
bridge-captured PR number before replacing stale title/body, then re-reads and
validates the same PR number. A retry after an edit succeeds but
the lifecycle transition is interrupted converges on that same PR. Mismatching
PRs are neither edited nor adopted, strict `pr_create` reconciliation remains
unchanged, and the bridge never marks a PR Ready. AgentKnowledgeVault Task #13
and Draft PR #29, whose old `97dbf036...` Validation metadata remained after
the live head reached `8313bcfc...`, are the motivating downstream fixture, not
an execution performed by this change.

Issue #103 permits that separate guarded cleanup to recover when GitHub has
already deleted a merged Task's remote branch. A configured upstream is not
treated as a live revision: cleanup queries origin directly, requires status
`merged`, a clean registered worktree, one matching merged PR, and exact
equality among the PR `headRefOid`, recorded published head when present, and
the local Task branch. Any local-only commit, PR identity mismatch, remote
head mismatch, or unavailable evidence fails closed. Cancelled Tasks do not
receive this exception. A private cleanup receipt and expected-OID ref deletion
make the worktree-removal/branch-removal tail retryable. The operation remains
Main-owned, human-approved, and Agent Core VERSION 3.

Each role uses one fixed GPT-5.6 model. Agent Core does not substitute models or retry an objective under another model. If the configured provider/model is unavailable, preserve Task and Work Unit evidence, report the exact failure, and return `BLOCKED`.

Task Orchestrators autonomously drive the persisted Work Unit loop through the
guarded `work-unit-next`, `work-unit-create`, and `work-unit-dispatch-check`
APIs. These Task-local APIs are globally denied and available only to the Task
Orchestrator. Every dispatch carries an exact role and objective; only
`COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, and `NEEDS_DECISION` are canonical
leaf statuses. Failed review, security, verifier, or check evidence requires
a fresh corrective Work Unit; needs-* statuses stop for human handling.

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
just automation::check-update /path/to/Templates [expected-revision]
```

This reports the actual source `HEAD` and optionally asserts an exact full
immutable expected revision, alongside current/upstream versions and ownership
boundaries, without mutation. Agent Core upgrades must never be silently mixed
into an ordinary Task.

Both `automation::check-update` and `automation::upgrade` accept only a trusted
Git worktree root for the Templates source. The source worktree must have a
full, non-null `HEAD` and a clean `components/agent-core` scope: tracked
modifications and non-ignored untracked paths there are rejected. Ignored
generated artifacts are not part of the source input and are structurally
absent from the operation. The command pins the source `HEAD`, materializes
only the tracked `components/agent-core` objects into a temporary snapshot, and
plans/copies only from that snapshot. A compatible Agent Core `VERSION` drift
is still reported; a source race after pinning fails closed.

## Agent Core upgrade

Use a dedicated, registered, non-default **Automation Maintenance Task** worktree. The complete canonical publication workflow is:

```sh
AUTOMATION_MAINTENANCE=1 just automation::upgrade <trusted local Templates checkout> <expected-revision>
```

The source must be a trusted clean local Templates Git worktree root. Upgrade
requires the exact expected full immutable source revision; an actual-HEAD
mismatch fails before tracked consumer mutation or receipt/authority
publication, even for byte-identical trees, because commit identity is
provenance. The command pins its full `HEAD`, materializes only tracked
`components/agent-core` objects into a temporary snapshot, and plans/copies
only from that snapshot. Tracked modifications or non-ignored untracked paths
under Agent Core, a source race, or an invalid source root fail closed; ignored
generated artifacts are structurally absent. The command also refuses the
default branch, an unregistered Task, missing Task State, or a
non-ignored/tracked Task State path. The ambient `AUTOMATION_MAINTENANCE=1`
variable grants upgrade opt-in only; it does not grant commit authority, and
ordinary `just agent::commit <task>` still rejects Automation Core changes.

The receipt-reconstruction form also requires the exact revision:

```sh
AUTOMATION_MAINTENANCE=1 just automation::bootstrap-receipt <trusted local Templates checkout> <expected-revision>
```

Issue #97 is a compatible maintenance fix: `.automation/VERSION` remains 3,
and #83/#85 semantics remain unchanged.

It materializes only Agent Core-owned paths and preserves Adapter-owned `.automation/ADAPTER`, `.automation/INIT.fragment.md`, `.automation/adoption.toml`, `just/project/**`, local modules, and repository CI. On success it creates or replaces the ignored `.task-state/automation-maintenance.json` receipt; it does not commit, push, or merge. Inspect the diff and run:

```sh
git diff --check
just agent::doctor
just project::check
# repository CI/smoke suite
just automation::commit <task> [message]
just agent::push <task>
just agent::pr-create <task>
```

Issue #97 provides a narrow provenance correction. The canonical active-consumer
command is:

```sh
AUTOMATION_MAINTENANCE=1 just automation::rebind-maintenance-provenance <trusted-source-at-expected-HEAD> <expected-revision>
```

This is a standard receipt+authority correction, not generic editing or
deletion. For older consumers, the Templates source bridge is:

```sh
just agent-core::rebind-maintenance-provenance <consumer-worktree> <expected-revision>
```

It verifies bootstrap/engine trust, reconstructs old and expected immutable
objects from the same Templates object database, requires an unchanged expected
canonical diff and exact safe pending Agent Core paths/fingerprints, leaves
tracked files unchanged, and reports `PROVENANCE_REBOUND` or idempotent
`PROVENANCE_ALREADY_BOUND`. Then run ordinary consumer verification and the
existing `automation::commit`.

Rebind is an operator-serialized maintenance transition: no commit or other
authority mutation may run concurrently in the target worktree. Current Agent
Core revisions additionally fence rebind and commit with the same ordered
common/admin migration locks; explicit quiescence preserves the older pending
consumer needed to finish an in-flight upgrade.

Eligibility requires the exact registered maintenance Task/worktree/branch/HEAD,
one standard active receipt matching exactly one authority, no consumed,
source-recovery, or ambiguous state, and old/expected revisions in the same
Templates object database. Missing authority remains the Issue #85 route;
committed or consumed state, including a crossed guarded publication boundary,
is rejected. Handled failures
rollback safely; operations by current lock-aware revisions fail closed under
the shared fence. No cross-filesystem atomicity or
hard-crash durability claim is made; that remains Issue #89 scope.

For AgentKnowledgeVault Issue #19, the exact command is:

```sh
just agent-core::rebind-maintenance-provenance /path/to/AgentKnowledgeVault/.worktrees/19-agent-core-v3-1-1 835203b6f1ae342d31ed74372728e9862b9b36f0
```

The receipt's `076653b054f5d8cbce4a28bcb6b381e9f30ee669` is the old source
revision, not the expected revision; `1e3a795d5e2717f9c670a812777c4a38c9592db0`
is baseline metadata. This hermetic recovery makes no commit, push, or PR;
tracked bytes remain unchanged. Verify, then use normal `automation::commit 19`.

Issue #85 provides a narrower Templates source-side bridge for a consumer
worktree with the exact active receipt but missing authority. From the Templates
checkout, run:

```sh
just agent-core::recover-maintenance-authority <consumer-task-worktree>
just agent-core::commit-recovered-maintenance <consumer-task-worktree> <task> [message]
```

Use it only before fixing that consumer. Do not edit or delete the receipt,
replace receipt-bound Agent Core files, use `python -c` or a monkeypatch, or
modify downstream files directly outside the bridge. Recovery uses the current
clean, pinned Templates `HEAD`; the receipt source revision remains historical
and is materialized from tracked Git objects, without checking out current
source to that old revision or using live files. `receipt.source` must be the
exact same Templates Git repository/common object store worktree; unrelated or
missing sources are rejected.

Recovery leaves the receipt and target files unchanged, writes per-worktree
schema-2 bridge authority and proof, and reports `AUTHORITY_RECOVERED`.
Publication uses current guarded semantics: exact receipt paths, private-index
blob-and-mode validation, `commit-tree`, and expected-`HEAD` `update-ref`. A
failure before the atomic branch update restores the retryable pair; successful
expected-`HEAD` `update-ref` is the publication boundary. A later finalization
error is reported as already published and must not be retried as an
uncommitted pair. There is no push or merge. The existing consumer script need not and must not
be replaced first. This is not a generic external apply/upgrade/commit
primitive; normal consumer bootstrap and commit remain separate workflows.

Both root bridge recipes use `python3 -I` for their small stdlib-only bootstrap:
isolated mode excludes `PYTHONPATH`, the current directory, and user-site
shadowing from bootstrap imports. The bootstrap resolves the root and trusted,
root-owned, non-writable Git repository with scrubbed `GIT_*`, then verifies the
full `HEAD` and the whole clean Templates worktree. It verifies the live
bootstrap against the tracked `HEAD` blob, obtains the engine regular blob from
the verified `HEAD` Git objects, materializes it privately, and executes the
engine only afterward. The verified `HEAD` is passed to the engine; the engine
independently repeats clean-source, root, and `HEAD` validation and requires
equality before publishing authority. Thus `implementation_revision` is proven
to be the blob-providing `HEAD`, and source races fail closed. Dirty bridge or
engine files and module-shadow states are rejected before target authority or
publication changes.

The live bootstrap is the small initial trust anchor: its self-check detects
accidental or concurrent divergence, but does not claim to defeat hostile
replacement. That requires an external trusted launcher or signing mechanism.

Issue #95 adds source-side recovery of a pristine registered Task Contract when
the consumer Task State is still the v3.1.1 placeholder. For AgentKnowledgeVault
Issue #19, run from a clean Templates checkout (replace the path with the
already-registered consumer worktree):

```sh
just agent-core::recover-task-contract-from-issue /path/to/AgentKnowledgeVault/.worktrees/19-agent-core-v3-1-1 19
cd /path/to/AgentKnowledgeVault/.worktrees/19-agent-core-v3-1-1
just agent::preflight
just agent::doctor
just agent::context
just project::doctor
just project::check
```

The bridge resolves the exact registered worktree and lets the canonical
`task_contract.py` validate identity, pristine state, and the same-repository
open Issue before writing only ignored `.task-state` metadata. It is
idempotent for the same snapshot. Do not manually edit `.task-state`; after
recovery, use the strict initialization and normal guarded Task workflow.
Hard-crash consistency remains out of scope; these safeguards do not change
Issue #85 semantics or claim stronger durability.

Issue #99 separates pristine initial-launch readiness from read-only resume
readiness. A new Task still requires:

```sh
just agent::contract-check <task>
```

and receives `status: READY`, `mode: initial` only while it is `initialized`,
has no Work Units or pending tracked changes, and remains at its Base revision.
An already-launched Task instead uses:

```sh
just agent::contract-resume-check <task>
```

This validates the same canonical Issue snapshot, digest, metadata, required
sections, repository, Task, branch, and uniquely registered worktree without
requiring a clean tree, zero Work Units, or `HEAD == Base revision`. It accepts
the existing `researching`, `planning`, `implementing`,
`verification-pending`, `local-verified`, `review-pending`,
`publication-ready`, `draft-pr-created`, and `blocked` states. It rejects
`initialized` (which uses the initial gate), `integration-pending`, `merged`,
and `cancelled`. Neither check changes Task State or Work Units.
Both readiness modes re-read the open authoritative GitHub Issue and require
its filtered content digest to remain identical to the stored snapshot. A
coherently rewritten set of ignored Task State files therefore cannot
self-authenticate. An unavailable or changed Issue fails closed.

Issue #101 makes pull request publication metadata evidence-backed and
fail-closed. After current-head `just agent::verify <task>` evidence exists and
the Task is `publication-ready`, run:

```sh
just agent::pr-prepare <task>
just agent::pr-create <task>
```

Preparation deterministically writes only ignored `.task-state/pr-title.txt`
and `.task-state/pr-body.md` from the resolved Task Contract, changed paths,
the persisted successful project check, and completed reviewer/security
reviewer Work Units. Publication rejects untouched placeholders, stale
metadata, and a `NOT RUN` claim that contradicts persisted PASS evidence.
The PR renders non-empty Task `Blockers` and `Unverified` values instead of
claiming that no risks are recorded; missing Current-state evidence fails
closed. Issue-backed acceptance criteria are labeled as authoritative
requirements, while completion evidence remains in Validation and Reviews.
Creation and repair rerun project verification, and publication requires a
completed reviewer Work Unit; any present security-reviewer unit must also be
completed before its evidence can be rendered.
`pr-edit` repairs the same existing open Draft PR after preparation;
`pr-ready` reruns verification and requires exact canonical/live metadata
before marking Ready and entering `integration-pending`. Generic `state-set`
cannot cross either publication boundary. Merge remains Main-owned and Agent
Core VERSION remains 3.

For a pre-fix consumer such as AgentKnowledgeVault Task #19, run the verified
source-side bridge from a clean Templates checkout containing Issue #99's fix:

```sh
just agent-core::resume-contract-check /path/to/AgentKnowledgeVault/.worktrees/19-agent-core-v3-1-1 19
```

For the blocked Task with the rebound receipt source revision
`835203b6f1ae342d31ed74372728e9862b9b36f0`, success includes bounded evidence
like:

```json
{
  "status": "READY",
  "mode": "resume",
  "task": "19",
  "taskStatus": "blocked",
  "repository": "upiscium/AgentKnowledgeVault",
  "implementationRevision": "<verified Templates HEAD>"
}
```

That source-side `READY` result is the formal launch handoff equivalent for an
older installed Task Orchestrator contract that cannot produce consumer-side
resume readiness. The bridge loads its contract implementation from verified
Templates `HEAD` blobs, uses the trusted GitHub CLI to bind the snapshot to the
current authoritative Issue, and leaves all tracked consumer files and all
Task State bytes unchanged. It performs no transition, commit, push, PR,
merge, or cleanup. Main passes the complete READY evidence, including its
`sha256`, when relaunching exactly one Task Orchestrator; the orchestrator
requires the initialized contract marker to retain that same digest. It then
continues from `blocked` through an existing canonical transition such as
`blocked -> verification-pending`, preserves all Work Units, and uses the
existing `automation::commit 19` path. Resetting to `initialized`, deleting or
reopening Work Units, and manual `.task-state` edits remain forbidden. This is
a compatible Agent Core change; VERSION remains 3.

After a successful commit, the active receipt is consumed as `.task-state/automation-maintenance.consumed.json`. A later successful upgrade with changes replaces the active receipt and removes the prior consumed receipt; a no-change invocation returns `NO_CHANGES` and preserves existing receipt lifecycle evidence. There is no raw Git/GitHub bypass, and merge is excluded: the Main Orchestrator performs the separately gated integration/merge workflow.

The upgrade system supports versioned removal migrations. Removals name explicit, exact Agent Core-managed paths; repository-owned and protected paths cannot be removed. Migration preconditions are checked before any mutation, and `.automation/VERSION` advances only after all deletion, creation, replacement, and merge actions succeed.

Upgrade does not commit, push, or merge. Before publication, inspect the diff and run at minimum:

```sh
git diff --check
just agent::doctor
just project::check
```

Then require the repository CI/smoke suite. Automation Core changes must be reviewed as a dedicated maintenance change.

## Template development

### OpencodeContract dependency

The Templates source repository pins [`upiscium/OpencodeContract`](https://github.com/upiscium/OpencodeContract) through the root `flake.lock` and audits this checkout against the explicit `agent-core` profile. OpencodeContract owns the shared contract and compatibility policy; Templates remains the Agent Core implementation and distribution owner.

The dependency is a Templates development and validation gate only. It is not rendered into generated templates, does not change `.automation/UPSTREAM`, and does not generate OpenCode configuration.

Contract revisions advance only through an explicit dependency update. The recommended path is **GitHub Actions → Update OpencodeContract → Branch = main → Run workflow**, or:

```sh
gh workflow run update-opencode-contract.yml \
  --repo upiscium/Templates \
  --ref main
```

This workflow runs only through an explicit `workflow_dispatch`; it has no schedule. It validates lock hygiene, Templates contracts, generated drift, distribution metadata, and the strict `agent-core` profile before creating a Draft pull request containing only `flake.lock`.

For a local update:

```sh
nix flake update opencodeContract
nix flake check --no-update-lock-file
python3 -m unittest discover -s tests -v
just template::check
```

Review the resulting `flake.lock` diff and contract audit before merging. OpencodeContract `main` moving does not change Templates until this lockfile is deliberately updated. Generated templates do not receive the OpencodeContract input and are not regenerated by this dependency update. The resulting Draft pull request runs normal Template CI as a second gate, including the generated-template runtime smoke matrix.

```sh
just template::render agent-base
just template::render agent-python
just template::render agent-rust
just template::render agent-nix
just template::render agent-cpp-cmake
just template::render agent-typescript-node
just template::render-all
just template::check
just template::distribution-verify
```

`template::check` detects generated drift, path collisions, dotfile/mode drift, and unregistered generated directories. `template::distribution-verify` validates the fixed GitHub distribution allowlist; publication itself is CI-only after a successful Template CI run on current `main`.

## OpenCode hierarchy

The default Main agent orchestrates Tasks; Task Orchestrators own one Task; leaf agents perform bounded work and cannot re-delegate. Configured role models are fixed and fail closed when unavailable. Depth-2 Ask behavior has a separate reproducible manual smoke procedure under `docs/opencode-depth2-ask-smoke.md` and is not represented as PASS until executed.
