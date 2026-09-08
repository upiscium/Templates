# Agent-ready Repository Template Architecture

Related: #4

## 1. Purpose

This document defines the stable architecture and public contracts for Agent-ready repository templates generated from this repository.

The goal is to make generated repositories self-contained execution environments for AI-assisted development while keeping orchestration, project tooling, Git publication, and integration responsibilities explicit and separable.

This document is a design contract for later implementation issues. It intentionally specifies structure, ownership, interfaces, lifecycle boundaries, and permission boundaries without prescribing all script internals.

## 2. Core invariants

1. One implementation Task owns exactly one branch, one Git worktree, one disposable Task State, and one Depth-1 Task Orchestrator session.
2. Multiple Tasks may run concurrently only when they use different worktrees and their dependencies and integration surfaces allow parallel execution.
3. A Task Orchestrator may delegate bounded Work Units to leaf agents, but leaf agents may not delegate again.
4. The Main Orchestrator owns repository-wide scheduling and integration decisions.
5. Task Orchestrators may prepare commits and pull requests, but they may not merge.
6. Merge is a Main Orchestrator operation and remains permission-gated.
7. Repository-local Just recipes are the stable automation API. Agents should not bypass an approved recipe with raw state-changing Git or GitHub commands.
8. Agent Core is independent of language and build system. Project Adapters implement language- and toolchain-specific behavior behind a stable `project::*` contract.
9. Session initialization is read-only. Repository bootstrap and Agent Core upgrades are separate state-changing workflows.
10. Task State is disposable execution state and must never be committed or pushed.
11. Depth-2 leaf agents are non-interactive and may return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
12. The Depth-1 Task Orchestrator is the escalation approval and decision boundary and must independently re-evaluate scope, authority, least-privilege alignment, safety, alternatives, and evidence before resolving any `NEEDS_APPROVAL`/`NEEDS_DECISION` result. An unresolved human decision is asked from Depth 1, not relayed by the Leaf.
13. Task Orchestrator must not automatically convert leaf denials into Ask, propagate requests unchanged, weaken configured permissions, or report unexecuted work as PASS. It may originate a new Depth-1 permission request only after independent re-evaluation and only when that operation is already Ask/allow under its own profile.

## 3. Repository composition model

Templates are composed from reusable components instead of duplicating a complete tree per language.

```text
components/
├── agent-core/
│   ├── opencode.json
│   ├── AGENTS.md
│   ├── Justfile
│   ├── .opencode/
│   │   ├── agents/
│   │   ├── commands/
│   │   └── skills/
│   └── .automation/
│       ├── VERSION
│       ├── UPSTREAM
│       ├── INIT.md
│       ├── policy.toml
│       ├── just/
│       ├── bin/
│       └── templates/
│
└── adapters/
    └── <adapter>/
        ├── flake.nix
        ├── just/project/
        ├── INIT.fragment.md
        └── adapter-specific CI fragments

templates/
└── <generated-template>/
```

Generated templates are build artifacts of the composition process and should not be manually edited when the same source belongs to Agent Core or an Adapter component.

## 4. Generated repository layout

A generated Agent-ready repository should converge on the following structure:

```text
repository/
├── opencode.json
├── AGENTS.md
├── Justfile
├── flake.nix
├── flake.lock
├── .opencode/
│   ├── agents/
│   ├── commands/
│   └── skills/
├── .automation/
│   ├── VERSION
│   ├── UPSTREAM
│   ├── INIT.md
│   ├── policy.toml
│   ├── just/
│   │   ├── agent.just
│   │   └── integrate.just
│   ├── bin/
│   └── templates/
├── just/
│   └── project/
│       ├── mod.just
│       ├── repository.just
│       └── adapter-specific modules
├── .github/
│   └── workflows/
└── .worktrees/
```

`.task-state/` exists only inside dedicated Task worktrees and is not part of the tracked template tree.

## 5. Ownership boundaries

| Area | Owner | May contain | Must not contain |
| --- | --- | --- | --- |
| `opencode.json` | Agent Core | repository-local permissions, model allocation, orchestration topology | project toolchain commands |
| `.opencode/**` | Agent Core | agents, skills, commands | language-specific build logic |
| `AGENTS.md` | Agent Core + generated adapter guidance | durable repository agent rules | mutable Task progress |
| `.automation/**` | Agent Core | lifecycle scripts, Task State template, integration gates | project-specific compiler/test implementation |
| `Justfile` | Agent Core | module routing only | large recipe bodies |
| `just/project/**` | Project Adapter + repository extension | build/test/lint/toolchain recipes | Task lifecycle, PR merge logic |
| `flake.nix` / `flake.lock` | Project Adapter / repository | reproducible tools and project dependencies | Agent lifecycle policy |
| `.task-state/**` | active Task Orchestrator | Task contract, Work Units, evidence, publication state | durable project configuration |
| `.github/workflows/**` | Agent Core + Project Adapter | CI gates and project checks | ad-hoc Task state |

## 6. Automation Core protection boundary

The following paths form the default Automation Core and are protected from ordinary implementation Tasks:

```text
opencode.json
AGENTS.md
Justfile
.opencode/**
.automation/**
.github/workflows/**
```

`flake.nix` and `flake.lock` are not globally immutable because dependency and environment Tasks may legitimately edit them. Their modification must be explicit in the Task scope.

Changes to the Automation Core require a dedicated Automation Maintenance Task and stronger review than ordinary implementation work.

## 7. Just module architecture

The top-level `Justfile` is a router, not a monolithic command file.

Conceptually:

```just
mod agent '.automation/just/agent.just'
mod integrate '.automation/just/integrate.just'
mod project 'just/project/mod.just'
mod? local 'just/local.just'
```

The namespaces have separate responsibilities:

- `agent::*`: Task lifecycle and publication API.
- `project::*`: stable project verification and build API.
- `integrate::*`: repository-wide PR integration API.
- `local::*`: optional developer-local commands; never part of the agent auto-allow contract.

Internal helper recipes should remain private and should not be treated as public API.

## 8. Stable Just API

### 8.1 Agent API

Every Agent-ready repository should expose the following logical operations:

```text
just agent::preflight
just agent::doctor
just agent::context
just agent::task-start <TASK-ID> <slug>
just agent::contract-check <TASK-ID>
just agent::contract-resume-check <TASK-ID>
just agent::status <TASK-ID>
just agent::verify <TASK-ID>
just agent::commit <TASK-ID>
just agent::push <TASK-ID>
just agent::pr-create <TASK-ID>
just agent::pr-edit <TASK-ID>
just agent::pr-ready <TASK-ID>
just agent::cleanup <TASK-ID>
```

Semantics:

- `preflight`: validate runtime tools, required files, Agent Core version, and Adapter readiness without branch/Task identity; read-only and suitable for bootstrap/adoption/upgrade diagnostics.
- `doctor`: validate full repository/session readiness, including strict context and branch/Task identity, without repairing it.
- `context`: resolve repository/worktree/branch/Task/adapter context in machine-readable form with strict identity validation.
- `task-start`: create a dedicated branch/worktree and initialize Task State.
- `contract-check`: validate canonical contract integrity plus pristine initial
  launch state and return `mode: initial`; it requires `initialized`, zero Work
  Units, a clean worktree, and `HEAD` equal to the recorded Base revision.
- `contract-resume-check`: validate the same canonical contract and exact
  repository/Task/branch/worktree identity for an already-launched resumable
  Task and return `mode: resume`; it is read-only and does not require pristine
  state.
- `status`: report current Task state and Git relationship.
- `verify`: execute the stable project verification contract and record evidence.
- `commit`: validate scope and create Task-local commits.
- `push`: push only the Task branch through an explicit refspec.
- `pr-create`: create a Draft PR for the Task branch.
- `pr-edit`: update only approved PR metadata for the Task PR.
- `pr-ready`: mark the Task PR ready only after required gates pass.
- `cleanup`: remove Task-local resources after safe completion.

### 8.2 Project Adapter API

Every adapter must provide a stable compatibility layer:

```text
just project::doctor
just project::format-check
just project::lint
just project::test
just project::build
just project::check
```

Adapters may expose additional toolchain-specific submodules such as:

```text
project::cmake::*
project::cargo::*
project::nix::*
project::quality::*
project::repository::*
```

OpenCode permissions should normally target the stable top-level `project::*` contract, not every toolchain implementation detail.

### 8.3 Integration API

Repository-wide integration is exposed separately:

```text
just integrate::check <PR>
just integrate::merge <PR>
```

`integrate::check` is read-only or validation-only. `integrate::merge` performs the final integration boundary and must remain permission-gated.

## 9. OpenCode orchestration topology

The project-local OpenCode configuration uses `subagent_depth = 2`.

```text
Depth 0: Main Orchestrator (`build`)
│
├── Depth 1: Task Orchestrator A
│   ├── Depth 2: explore
│   ├── Depth 2: general
│   ├── Depth 2: verifier
│   └── Depth 2: specialist reviewer/investigator when required
│
└── Depth 1: Task Orchestrator B
    ├── Depth 2: explore
    ├── Depth 2: general
    └── Depth 2: verifier
```

The call graph is intentionally acyclic.


### 9.2 Repository-local planning agent

Agent Core owns a repository-local `plan` primary agent so Agent-ready repositories do not inherit planning authority from a generic global profile. The planning contract is intentionally read-only:

```text
plan
├── primary = openai/gpt-5.6-sol
├── edit = deny
├── question = allow
├── bash = deny
└── delegation
    ├── explore
    ├── architect
    ├── reviewer
    └── security-reviewer
```

`plan` may use native reads and approved read-only leaves to produce requirements analysis and implementation sequencing. It must not delegate to `general`, `verifier`, `investigator`, `task-orchestrator`, or any implementation/execution-capable agent. It must not start Task lifecycle, edit `.task-state/**`, run raw Git/GitHub mutation, or run project-controlled commands.

Because `plan` has `bash: deny`, it cannot complete the executable initialization sequence. Instead it performs planning-only initialization by reading `AGENTS.md`, `.automation/INIT.md`, `.automation/INIT.fragment.md`, and optional Task State, then returns `PLANNING_INITIALIZATION_HANDOFF` semantics: explicit `execution_prerequisites` and `verification_handoff` entries for every unexecuted doctor, context, project, build, test, or policy check. Unexecuted checks remain `UNEXECUTED`; they are never reported as PASS.

This planning-only branch does not weaken full initialization. `build`, `task-orchestrator`, and other execution-capable workflows still must run `just agent::doctor`, `just agent::context`, and `just project::doctor` before editing, implementation delegation, or project commands.

`agent::preflight` is not a substitute for that initialization sequence. A bootstrap or adoption branch may use it to prove runtime readiness without fabricating Task State, while doctor/context and `/init` continue to reject an unregistered non-default branch.

### 9.1 Allowed delegation graph

```text
build
├── task-orchestrator
├── architect
├── reviewer
├── security-reviewer
├── investigator
└── scout

task-orchestrator
├── explore
├── general
├── verifier
├── reviewer
├── investigator
├── security-reviewer
└── scout

leaf agents
└── no subagents
```

A Task Orchestrator may not call another Task Orchestrator. Leaf agents have `task: deny`.

Depth-2 leaf work is non-interactive. A leaf may return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, and `NEEDS_DECISION`; no leaf may originate direct permission requests outside its configured allowlist.

Depth-1 Task Orchestrator is the decision boundary for `NEEDS_APPROVAL` and `NEEDS_DECISION`, including independent re-checks for scope, authority, safety, least privilege, alternatives, and evidence.
It resolves `NEEDS_DECISION` from the Task Contract/evidence when possible. If human judgment remains necessary, it uses `question` directly from Depth 1 with options, tradeoffs, known facts, and a recommendation, then applies the response.

## 10. Agent responsibilities

### 10.1 Main Orchestrator

Owns repository-wide decisions:

- receives explicitly selected Tasks;
- resolves Task dependencies and likely integration overlap;
- creates one branch/worktree per Task;
- starts Task Orchestrators;
- limits Task-level parallelism;
- observes Draft PR results;
- determines integration order;
- validates CI/review/head SHA before merge;
- performs guarded merge and cleanup.

The Main Orchestrator does not silently select the next available Issue or Task.

### 10.2 Task Orchestrator

Owns one Task only:

- validates initialization and Task boundaries;
- establishes the Task Contract;
- decomposes work into bounded Work Units;
- coordinates and delegates only needed leaf work; avoids speculative or repeated delegation when one Work Unit can be reused;
- preserves a single-workflow focus by assigning each Work Unit once unless new evidence requires a reschedule;
- inspects actual diffs and command output;
- accepts or rejects returned Work Units and all leaf escalations on depth-1 authority.
  - re-validates scope, configured authority, least privilege, safety, alternatives, and evidence before deciding any `NEEDS_APPROVAL`/`NEEDS_DECISION`.
  - may only approve follow-on operations already in its own Task configuration.
  - on rejection, chooses a safe alternative or returns `BLOCKED` with evidence.
  - treats a user-rejected permission decision as final for the exact operation within that Task; records the permission result and never retries, rephrases, re-delegates, or substitutes an equivalent operation.
  - resolves `NEEDS_DECISION` from available evidence or asks the user directly from Depth 1 and continues from the answer.
- does not automatically relay or launder leaf escalation requests or weaken permissions to satisfy them. A new Depth-1 request requires independent justification and existing Ask/allow authority in the orchestrator profile.
- updates Task State;
- verifies and reviews the integrated Task;
- prepares commits and pull-request state;
- stops before merge.

### 10.3 Leaf agents

Leaf agents are execution specialists:

- `explore`: read-only code discovery and reference tracing;
- `general`: bounded implementation within exclusive edit scope;
- `verifier`: executable tests/lint/type-check/build verification;
- `reviewer`: correctness review through native read/search tools only; Bash remains fully denied;
- `investigator`: root-cause diagnosis;
- `security-reviewer`: security-boundary review;
- `scout`: external primary-source research.

Leaf agents never update Task State and never create another generation of subagents.
Leaf escalation protocol:

- report only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
- do not issue raw asks for repository permissions; any escalation is returned through the status channel.
- include evidence sufficient for the Task Orchestrator to independently validate scope, authority, safety, and alternatives.
- when consequential requirements are ambiguous, report `NEEDS_DECISION` with rationale rather than attempting speculative execution.

## 11. Task and Work Unit model

### 11.1 Task

A Task is a user-visible, independently reviewable deliverable.

Invariant:

```text
1 Task = 1 branch = 1 worktree = 1 Task State = 1 Task Orchestrator
```

Tasks may run concurrently only in separate worktrees.

### 11.2 Work Unit

A Work Unit is an internal bounded unit delegated by a Task Orchestrator. It is not a new project Task and does not receive its own worktree.

Every delegated Work Unit must specify:

- ID;
- objective;
- inputs;
- worktree;
- read scope;
- exclusive edit scope;
- deliverable;
- verification requirement;
- dependencies;
- prohibited changes;
- stop conditions.

Independent Work Units may run concurrently when edit scopes and stateful effects do not overlap.

## 12. Worktree contract

Dedicated worktrees use:

```text
.worktrees/<TASK-ID>-<slug>/
```

Task branch names contain the Task ID, for example:

```text
task/<TASK-ID>-<slug>
fix/<TASK-ID>-<slug>
```

The default branch is discovered from Git metadata and is never assumed to be `main` or `master`.

Before worktree creation, automation must validate:

- Task ID is explicit;
- base branch and base revision are resolvable;
- branch does not already conflict;
- path does not already exist;
- branch is not checked out by another worktree;
- existing user work is not overwritten.

The generated repository should ignore `/.worktrees/`.

## 13. Disposable Task State

Each Task worktree contains:

```text
.task-state/task.md
```

`.task-state/` is excluded through the Git common directory's `info/exclude`, not as durable project content.

Task State records at least:

- Task identity and source;
- branch/worktree/base revision;
- execution context;
- Purpose;
- Scope;
- Prohibited changes;
- Dependencies;
- Acceptance Criteria;
- Test plan;
- Stop conditions;
- current status;
- Work Units;
- changed files;
- commands and results;
- review evidence;
- commit / remote branch / PR / published head SHA;
- blockers and unverified requirements;
- follow-up Task candidates.

Task State is written only by the Task Orchestrator. Leaf agents return structured update proposals instead.

Task State is deleted with its worktree and must not be committed, pushed, or copied into a PR.

## 14. Task lifecycle

The baseline Task state machine is:

```text
initialized
  ↓
researching
  ↓
planning
  ↓
implementing
  ↓
verification-pending
  ↓
local-verified
  ↓
review-pending
  ↓
publication-ready
  ↓
draft-pr-created
  ↓
integration-pending
  ↓
merged
```

Exceptional terminal/interruption states:

```text
blocked
cancelled
```

A Task is never considered complete merely because all leaf agents returned successfully. Completion is determined from Acceptance Criteria and evidence.

## 15. Initialization contract

Initialization has two distinct meanings.

### 15.1 Bootstrap

Bootstrap occurs when a repository/template is first created. It may generate or configure tracked repository files such as `AGENTS.md`, adapter files, and initial project configuration.

Bootstrap is state-changing and is not part of ordinary session initialization.

### 15.2 Session `/init`

Session initialization is read-only.

The initialization contract is defined by:

```text
AGENTS.md
.automation/INIT.md
just agent::doctor
just agent::context
just project::doctor
```

Before planning, editing, delegation, or project command execution, a primary agent must:

1. read durable repository guidance;
2. validate Agent Core prerequisites;
3. resolve repository/worktree/branch/Task/adapter context;
4. validate the Project Adapter;
5. capture baseline HEAD and Git status;
6. confirm the Task Contract;
7. begin Work Unit decomposition only after the checks pass.

`/init` does not rewrite `AGENTS.md`, install packages, repair Automation Core, or begin implementation.

## 16. Permission model

Permissions are designed to minimize Ask frequency while preserving explicit remote/integration boundaries.

### 16.1 Auto-allowed operations

Expected auto-allowed categories:

- OpenCode native read/glob/grep/list/lsp;
- selected read-only Git and GitHub inspection;
- stable project verification/build recipes;
- Task-local commit recipe after validation;
- Task-local Draft PR create/edit/ready recipes after their gates pass.

Raw shell commands are not treated as safe merely because their common use is read-only. For example, `echo`, `cat`, `sed`, or `jq` can participate in file writes through shell syntax.

### 16.2 Ask operations

Default Ask boundaries:

- Task branch push;
- PR merge;
- Task/worktree cleanup that deletes state;
- `/tmp/opencode/**` external-directory access;
- unclassified Bash operations.

The Main OpenCode TUI is the user approval hub for permission requests generated by descendant sessions.

Native Depth-2 Ask propagation remains a non-gating upstream compatibility canary for `anomalyco/opencode#13715`.
The release gate for delegated work is the Depth-1 Task Orchestrator decision on `Leaf → Depth-1` escalations (`NEEDS_APPROVAL`/`NEEDS_DECISION`).

### 16.3 Denied operations

Default hard-deny boundaries include:

- force push;
- commit amend;
- rebase;
- destructive reset/clean;
- direct push to the default branch;
- Task Orchestrator merge;
- admin/bypass merge;
- privilege escalation;
- destructive store/filesystem operations.

Raw state-changing Git/GitHub commands should not be auto-allowed when an approved Just API exists.

## 17. Publication boundary

Task Orchestrators may advance a Task through publication preparation, but merge remains outside their authority.

Expected publication sequence:

```text
verify
→ commit
→ push (Ask)
→ create Draft PR
→ edit PR metadata as needed
→ mark ready when gates pass
→ stop at integration-pending
```

The publication API validates at least:

- Task/branch/worktree consistency;
- non-default branch;
- completed required Acceptance Criteria;
- required verification and review;
- no unresolved blockers;
- `.task-state/**` exclusion;
- no unauthorized Automation Core changes;
- explicit publish scope.

Push is restricted to the Task branch with an explicit refspec. Force push is never part of the ordinary Task API.

### 17.1 Automation Maintenance publication

Agent Core upgrades are a separate, dedicated publication workflow, not ordinary Task work. The Task must be registered, non-default, and have ignored disposable Task State. From that Task worktree, use:

```sh
AUTOMATION_MAINTENANCE=1 just automation::upgrade <trusted local Templates checkout> <expected-revision>
```

The source must be a trusted clean local Templates Git worktree root. Upgrade
requires the exact expected full immutable source revision; an actual-HEAD
mismatch fails before tracked consumer mutation or receipt/authority
publication, even for byte-identical trees, because commit identity is
provenance. The command pins its full, non-null `HEAD`, materializes only tracked
`components/agent-core` objects into a temporary snapshot, and plans/copies
only from that snapshot. Tracked modifications or non-ignored untracked paths
under Agent Core, an invalid source root, or a source race fail closed; ignored
generated artifacts are structurally absent. The command also refuses the
default branch, an unregistered Task, missing Task State, or a
non-ignored/tracked Task State path. The ambient variable is only an upgrade
opt-in. It cannot authorize a commit; ordinary `just agent::commit <task>`
continues to reject Automation Core paths. Upgrade preserves Adapter,
repository, product, and CI-owned paths, and performs no commit, push, or
merge.

The read-only `automation::check-update <source> [expected-revision]` operation applies the same source
contract: its Templates argument must be a trusted clean Git worktree root
with a full `HEAD`, and tracked or non-ignored untracked changes under
`components/agent-core` are rejected. Ignored generated artifacts are
structurally absent; compatible `VERSION` drift remains detectable and actual
`HEAD` is always reported. It pins
the source `HEAD`, materializes only tracked Agent Core objects into a
temporary snapshot, and plans only from that snapshot. A source race fails
closed.

`automation::bootstrap-receipt <source> <expected-revision>` likewise requires
the exact full immutable revision; it is not permitted to infer provenance from
the current diff or a no-change result.

The canonical publication flows are:

1. **Normal upgrade:** run `automation::upgrade` from the dedicated Automation
   Maintenance Task, inspect the resulting diff, perform normal verification,
   then use `automation::commit`, `agent::push`, and `agent::pr-create`.
2. **Issue #85 source-side recovery:** only when fixing a consumer worktree with
   the exact active receipt and missing authority, run from the Templates
   checkout:

   ```sh
   just agent-core::recover-maintenance-authority <consumer-task-worktree>
   just agent-core::commit-recovered-maintenance <consumer-task-worktree> <task> [message]
   ```

   Do not edit or delete the receipt, replace receipt-bound Agent Core files,
   use `python -c`/monkeypatches, or modify downstream files directly outside
   the bridge. Recovery uses the current clean, pinned Templates `HEAD`; the
   receipt source revision remains historical and is materialized from tracked
   Git objects, without checking out current source to that old revision or
   using live files. `receipt.source` must be the exact same Templates Git
   repository/common object store worktree; unrelated or missing sources fail.
   Recovery leaves the receipt and target files unchanged, writes per-worktree
   schema-2 bridge authority and proof, and reports `AUTHORITY_RECOVERED`.
   Publication uses exact receipt paths, private-index blob/mode validation,
   `commit-tree`, and expected-`HEAD` `update-ref`. A failure before the atomic
   branch update restores the retryable pair; successful expected-`HEAD`
   `update-ref` is the publication boundary. A later finalization error is
   reported as already published and must not be retried as an uncommitted pair.
   It consumes receipt, authority, and proof. It does not push or merge. The existing consumer script need not and must not be
   replaced first. This is not a generic external apply/upgrade/commit
    primitive; normal consumer bootstrap and commit remain distinct.

    Both root bridge recipes use `python3 -I` and a small stdlib-only bootstrap.
    Isolated mode excludes `PYTHONPATH`, the current directory, and user-site
    shadowing from bootstrap imports. The bootstrap resolves the root and a
    trusted, root-owned, non-writable Git repository with scrubbed `GIT_*`, then
    verifies the full `HEAD` and the whole clean Templates worktree. It verifies
    the live bootstrap against the tracked `HEAD` blob, obtains the engine
    regular blob from the verified `HEAD` Git objects, materializes it privately,
    and executes the engine only afterward. The verified `HEAD` is passed into
    the engine, which independently reruns clean-source, root, and `HEAD`
    validation and requires equality before authority publication. Therefore
    `implementation_revision` equals the blob-providing `HEAD`, and source races
    fail closed. Dirty bridge or engine files and module-shadow states reject
    before target authority or publication changes.

    The live bootstrap is the small initial trust anchor. Its self-check detects
    accidental or concurrent divergence; hostile replacement requires an
    external trusted launcher or signing mechanism and is not claimed here.
    Hard-crash consistency remains out of scope, and this does not change Issue
    #85 semantics or imply stronger durability.

  Successful upgrade writes or replaces the ignored `.task-state/automation-maintenance.json` receipt. Its schema-1 content records Task identity (`task_id`, `branch`, `worktree`), source and source revision, current/upstream versions, sorted unique `changed_paths`, the authority `HEAD`, and per-path content/state fingerprints. Receipt and authority publication is a logical pair. Authority records live under the Git-resolved per-worktree administrative directory returned by `--absolute-git-dir`, not an assumed visible `.git` or shared Git directory; linked and special administrative topologies are supported, and worktrees do not share authority. Existing safe legacy shared-common-dir hashed records remain validation/commit compatible. `automation::commit <task> [message]` fails closed unless both records match the current Task/worktree identity and `HEAD`, fingerprints, and complete pending path set. Receipt paths must be Agent Core-managed; any mixed Adapter, repository, product, configured-secret, or `.task-state` scope is rejected. Ambient Git repository/index overrides are scrubbed; exact blobs and modes are staged and rechecked in a private index, committed as that verified tree without hooks, and published only by an expected-HEAD Task branch update. A handled authority-write failure removes the newly written receipt if it is unchanged; an interruption half-state is recoverable only through the strict source-side bridge above. No cross-filesystem atomicity is claimed. The receipt is moved to `.task-state/automation-maintenance.consumed.json` after successful commit; the next successful upgrade with changes replaces the active receipt and removes the prior consumed receipt. A no-change invocation returns `NO_CHANGES` and preserves existing lifecycle evidence.

The required sequence is `git diff --check`, `just agent::doctor`, `just project::check`, and the repository CI/smoke suite, followed by `just automation::commit <task> [message]`, existing `just agent::push <task>`, and `just agent::pr-create <task>` (Draft PR). Raw Git/GitHub bypass is not permitted. Merge is excluded from this workflow and remains a separately gated Main Orchestrator operation.

#### Issue #112 source-side first-adoption finalization

For a consumer whose installed Agent Core cannot run the maintenance
finalizer, the exact source-side bootstrap command is run from a clean,
trusted Templates source worktree:

```sh
just agent-core::maintenance-finalize <consumer-main-worktree> <task> <pr> <expected-implementation-revision>
```

The Templates source must be clean and at the exact full immutable `HEAD`
specified by `<expected-implementation-revision>`. The target is the
consumer's actual clean default-branch/Main worktree; it must not be a Task
worktree and must not be the source root. The source-side bridge verifies its
bootstrap and loads the implementation and maintenance modules from the
verified source Git blobs at that revision, rather than copying or trusting
live source files. A source race, revision mismatch, dirty source, or invalid
target fails closed before target lifecycle publication.

The operation applies the canonical validations for the consumer contract,
maintenance receipt and authority, exact repository/worktree identity,
publication/review evidence, and merged PR identity. It synchronizes the
actual default branch only by a clean fast-forward-only update, then
revalidates the exact merged PR, published head, merge commit, and canonical
publication reconstruction against the synchronized branch. Only after those
checks does it write the dedicated maintenance terminal transition
`initialized -> merged` and its publication evidence.

Idempotency is exact: a second invocation is accepted only when the Task is
already `merged` with the one identical finalized publication record and the
same PR/commit evidence; it returns the already-finalized result and does not
perform another lifecycle transition. Cleanup remains separate and
approval-gated. The ordinary consumer finalizer remains
`just automation::maintenance-finalize <task> <pr>`; this source-side bridge is not a
generic authority-expansion or arbitrary consumer-mutation primitive. AKV
#22/#23 motivate this first-adoption example, but their execution is not
claimed. Agent Core VERSION remains 3.

#### Issue #131 source-side publication recovery

`agent-core::publication-recover` is a bounded recovery boundary for an
already-complete in-flight consumer Task whose installed publication tooling is
defective. It is not an alternative normal publication workflow: ordinary
Tasks use `just agent::pr-prepare` and `just agent::pr-create`.

```sh
just agent-core::publication-recover <consumer-task-worktree> <task> <expected-implementation-revision>
```

The implementation revision is a required full exact Templates commit. The
bridge reuses the maintenance recovery trust chain: the live bootstrap equals
its tracked blob and mode, the source remains clean at the exact revision,
canonical modules come only from verified commit blobs, and Git/GitHub
execution is pinned and environment-sanitized. Consumer-local Agent Core code
never becomes publication authority.

The bridge binds the exact registered non-default worktree, Task/branch/contract
identity, `publication-ready` state, local worktree and branch HEAD, GitHub
remote branch OID, canonical repository, clean product status, fresh head-bound
verification receipt, and effective review evidence. It uses the verified
source implementation's canonical preparation, then canonical Draft creation
when no PR exists or canonical Draft repair when the exact Task PR exists.
Product HEAD and tracked bytes remain unchanged; Work Unit, verification, and
contract evidence remain byte-identical; only private publication metadata and
the guarded `publication-ready -> draft-pr-created` transition may change.

Canonical `pr_create` remains strict and reconciles only an already-canonical
existing Draft. The bridge does not broaden it: after preparation, absence of a
PR selects `pr_create`, while presence selects canonical `pr_edit`. That edit
authority requires the exact OPEN same-repository Draft for the captured
branch, base, and current head, and requires its internal lookup to equal the
bridge-captured PR number before changing only canonical title/body. It then
re-reads and validates the same PR identity. If GitHub editing succeeds before
the lifecycle transition is interrupted, a retry selects that same PR and
converges idempotently. Wrong PR state is never edited or adopted. This surface
does not mark Ready, upgrade the consumer, mutate product history, or expose
arbitrary source-side Agent Core execution.

#### Issue #97 provenance correction

The active-consumer correction is deliberately narrow, not generic receipt
editing or deletion:

```sh
AUTOMATION_MAINTENANCE=1 just automation::rebind-maintenance-provenance <trusted-source-at-expected-HEAD> <expected-revision>
```

For older consumers, the Templates source bridge is:

```sh
just agent-core::rebind-maintenance-provenance <consumer-worktree> <expected-revision>
```

It verifies bootstrap/engine trust, reconstructs old and expected immutable
objects from the same Templates object database, requires the exact registered
maintenance Task/worktree/branch/HEAD, one standard active receipt matching
exactly one authority, safe pending Agent Core paths/fingerprints, no consumed,
source-recovery, or ambiguous state, and an identical expected canonical diff.
Tracked files remain unchanged; success reports `PROVENANCE_REBOUND`, while an
already-correct pair reports `PROVENANCE_ALREADY_BOUND`. Missing authority
remains the #85 route; committed or consumed state, including a crossed guarded
publication boundary, rejects.
Rebind is operator-serialized: older pending consumers require quiescence with
no concurrent commit or authority mutation. Current lock-aware revisions also
share ordered common/admin migration fences for rebind and commit. Handled
failures roll back safely, and concurrency among those current revisions fails
closed. No
cross-filesystem atomicity or hard-crash durability is claimed; that remains
#89 scope.

For AgentKnowledgeVault #19, run exactly:

```sh
just agent-core::rebind-maintenance-provenance /path/to/AgentKnowledgeVault/.worktrees/19-agent-core-v3-1-1 835203b6f1ae342d31ed74372728e9862b9b36f0
```

`076653b054f5d8cbce4a28bcb6b381e9f30ee669` is the old receipt source revision,
not the expected `835203...`; `1e3a795d5e2717f9c670a812777c4a38c9592db0` is
baseline metadata. This is hermetic recovery: no commit, push, or PR, and no
tracked-byte change. Verify, then use normal `automation::commit 19`. Issue
#97 is a compatible maintenance fix: VERSION remains 3, and #83/#85 semantics
are unchanged.

#### Issue #99 resume-contract handoff

Initial launch and resume are disjoint gates. Initial `READY` is restricted to
the pristine `initialized` state. Resume `READY` accepts only `researching`,
`planning`, `implementing`, `verification-pending`, `local-verified`,
`review-pending`, `publication-ready`, `draft-pr-created`, or `blocked` after
strict canonical Issue snapshot, digest, metadata, required-section,
repository, Task, branch, and unique-worktree validation. Resume rejects
`initialized`, `integration-pending`, `merged`, and `cancelled`. Both gates are
read-only. Resume never resets status, deletes/reopens Work Units, or grants
generic Task State mutation.
Both readiness modes fetch the current open authoritative Issue and require its
canonical filtered payload to match the stored digest. The ignored snapshot,
metadata, and marker therefore cannot be coherently rewritten into a
self-authenticating replacement.

Pre-fix consumers use the clean, verified Templates source bridge:

```sh
just agent-core::resume-contract-check <consumer-task-worktree> <numeric-task>
```

The isolated bootstrap verifies its tracked `HEAD`, loads `task_contract.py`
and `task_lifecycle.py` only from that verified Git tree, uses the trusted
GitHub CLI for the authoritative Issue binding, and requires the explicit
target itself to be the uniquely registered non-main Task worktree.
It returns bounded `status: READY`, `mode: resume` evidence including the
verified Templates `implementationRevision`; target tracked files and the
entire ignored Task State remain byte-for-byte unchanged. It performs no
lifecycle or Git/GitHub mutation.

For AgentKnowledgeVault #19 after `PROVENANCE_REBOUND` bound the active receipt
to `835203b6f1ae342d31ed74372728e9862b9b36f0`, this source-side resume `READY`
is the formal launch handoff equivalent for the older installed startup
contract. Main passes the complete evidence, including `sha256`, and may
relaunch one Task Orchestrator only while that digest still matches the
initialized Task Contract marker. The orchestrator preserves the existing
terminal Work Units, follows the canonical `blocked -> verification-pending`
transition, and then uses the existing `automation::commit 19` publication
path. No reset to `initialized`, raw Git authority, push, PR, merge, or cleanup
is part of the bridge. This compatible fix keeps Agent Core VERSION 3.

## 18. Integration boundary

Only the Main Orchestrator uses `integrate::*`.

Before merge, integration checks must validate:

- PR is open and targets the expected default branch;
- Task branch identity is valid;
- required CI passes;
- required review is complete;
- security review is complete when applicable;
- dependencies are already integrated;
- merge conflict status is acceptable;
- reviewed/verified head SHA still matches current PR head;
- Automation Core changes are expected when present.

Merge itself remains an Ask operation.

Administrative bypass is not part of the normal API.

## 19. Project Adapter contract

The Project Adapter is responsible for reproducible project-specific tooling while preserving the same agent-facing interface across languages.

Examples:

- Python adapter may implement `project::check` using Ruff, Mypy, Pytest, and uv.
- Rust adapter may implement it using rustfmt, Clippy, Cargo test, and Cargo build.
- Nix adapter may implement it using flake evaluation/check/build and Nix-specific linting.
- C++/CMake adapter may implement it using CMake, Ninja, CTest, clang-format, and clang-tidy.

Agent Core calls the stable API and does not infer these implementation details.

Adapters may define additional nested toolchain namespaces, but those are not automatically part of the OpenCode allowlist.

## 20. Repository extension and local modules

Project-specific operations that are not generic to the language/toolchain belong under:

```text
project::repository::*
```

Developer-machine-specific commands belong in an optional local module such as:

```text
local::*
```

Local commands are never automatically exposed to agents.

## 21. Model allocation contract

The intended initial model allocation is:

| Role | Model family | Responsibility |
| --- | --- | --- |
| Main Orchestrator / planning / architecture | GPT-5.6 Sol | decomposition, orchestration, integration decisions |
| Task Orchestrator | GPT-5.6 Sol | bounded Task orchestration |
| general / explore | GPT-5.6 Luna | implementation and discovery |
| verifier / scout | GPT-5.6 Luna | deterministic verification and lightweight external research |
| reviewer / investigator / security-reviewer | GPT-5.6 Terra | analysis, diagnosis, review |

This split assigns Sol to orchestration and architecture, Luna to bounded implementation/reconnaissance/verification/research, and Terra to review/investigation/security. Each role has one fixed model; unavailable execution returns `BLOCKED` without model substitution.
High-quality analysis roles remain as configured (reviewer/investigator/security-reviewer on Terra).

Exact provider model IDs are validated at implementation time. Missing model IDs must not be silently substituted with similar names.

## 22. Parallelism contract

Two separate kinds of parallelism are supported.

### 22.1 Task-level parallelism

The Main Orchestrator may run multiple Task Orchestrators in parallel when:

- Task dependencies permit it;
- likely edit regions do not create obvious integration hazards;
- shared lockfiles, schemas, generated outputs, databases, containers, ports, and other stateful resources are accounted for.

Each parallel Task uses its own worktree.

### 22.2 Work-Unit-level parallelism

A Task Orchestrator may run bounded leaf Work Units in parallel when:

- files do not overlap;
- shared manifests/lockfiles are not concurrently modified;
- one Work Unit does not depend on another's output;
- stateful external resources are isolated.

Parallelism is bounded rather than unlimited. Exact initial concurrency limits are implementation policy, not part of this architecture contract.

## 23. External resource isolation

Git worktrees isolate checked-out files and index state, but do not automatically isolate external resources.

Adapters and repository extensions must account for shared resources such as:

- TCP ports;
- Docker Compose project names;
- container names;
- temporary databases;
- Unix sockets;
- `/tmp` files;
- build output directories;
- coverage output;
- generated files;
- Nix result symlinks.

Where safe, Task ID should be used as a namespace. If a safe namespace cannot be derived from repository policy, automation stops instead of inventing one.

## 24. Configuration layering

Generated repositories own their development policy through repository-local configuration.

Global OpenCode configuration should be limited to user-level concerns such as provider configuration, credentials integration, and TUI preferences.

Repository-local configuration owns:

- orchestration topology;
- model allocation;
- `subagent_depth`;
- Just API permissions;
- Task lifecycle;
- project-local commands and skills.

Repository-local policy is therefore part of the trusted repository surface and is included in the Automation Core protection boundary.

## 25. Template generation contract

The Templates repository should eventually generate concrete templates by composing:

```text
Agent Core + one Project Adapter + generated metadata
```

Composition must be deterministic and must not silently overwrite conflicting files.

Generated artifacts should carry enough metadata to identify their Agent Core version, upstream source, and selected adapter.

The generation mechanism and upgrade/versioning implementation are delegated to later issues.

## 26. Non-goals

This architecture does not require:

- an OpenCode process manager or daemon;
- automatic approval of Ask permissions;
- automatic Task selection;
- a permanent central Task Ledger;
- Task-Orchestrator-driven merge;
- automatic branch-protection bypass;
- all language adapters to be implemented at once;
- local Ollama models to be assigned to agents.

## 27. Migration guidance for durable escalation architecture (breaking behavior)

This contract changes leaf execution semantics and advances Agent Core from version 1 to version 2. Existing generated repositories must apply this as a dedicated Automation Maintenance upgrade rather than copying individual prompts or permission rules into an ordinary implementation Task:

1. Deploy the Task Orchestrator prompt (including exact status contract and escalation re-check rules) to every generated repository path that carries it.
2. Update leaf agent instructions and any agent templates so Depth-2 units are non-interactive and return only `COMPLETED`, `BLOCKED`, `NEEDS_APPROVAL`, or `NEEDS_DECISION`.
3. Add/validate tooling checks that treat any unrecognized leaf status as invalid evidence and block the Task.
4. Require depth-1 re-evaluation on `NEEDS_APPROVAL`/`NEEDS_DECISION`, including explicit checks for:
   - allowed scope and worktree
   - configured authority and prohibited changes
   - least-privilege and safety implications
   - alternative actions
   - current evidence quality.
5. Enforce that leaf-to-depth-1 escalation is a release gate; only completion evidence or explicit `BLOCKED` with rationale passes the boundary.
6. Preserve existing lifecycle invariants: initialization contract, fixed-model fail-closed behavior, no merge from depth-1, no sibling worktree access.
7. Run `docs/opencode-depth2-ask-smoke.md` as an upstream compatibility canary, but treat it as non-gating for release.

## 28. Follow-up implementation mapping

This contract intentionally splits implementation across the Epic issues:

- #5: component + adapter template generation;
- #6: Agent Core Just modules and scripts;
- #7: repository-local OpenCode configuration and hierarchical agents;
- #8: worktree Task lifecycle and Task State;
- #9: `AGENTS.md` and initialization contract;
- #10: Python/Rust adapter migration;
- #11: Nix adapter;
- #12: C++/CMake adapter;
- #13: CI, negative tests, and Ask propagation smoke tests;
- #14: versioning, upgrades, and user documentation.

Later issues should treat the stable contracts in this document as authoritative unless an explicit architecture change updates this document first.
