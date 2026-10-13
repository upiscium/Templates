# Disposable native v4 Turn orchestration

**Status: staged source only.** This implements the bounded #196 direction from
[the scoped implementation instruction](https://github.com/upiscium/Templates/issues/196#issuecomment-6061989331)
and [the Main-level UX carry-forward](https://github.com/upiscium/Templates/issues/196#issuecomment-5872265570).
It is not a v3 Work Unit migration, installed runtime, process dispatcher,
publication authority, evaluator, or global Task workflow runner.

The new sources are:

- [`turn_work.py`](../components/agent-core-v4/turn_work.py): pure immutable
  ticket/result types, a disposable in-memory coordinator, and descriptive Main
  Issue-selection policy.
- [`turn_orchestration.py`](../components/agent-core-v4/turn_orchestration.py):
  a trusted-host assembly seam over current #144 observations, #192 operation
  diagnosis, #145/#217 Evidence, #144 Snapshots and #194 checkpoint owners.
- [`turn_policy.md`](../components/agent-core-v4/turn_policy.md): dormant
  Agent/Skill operating policy, not active OpenCode/Codex configuration.

Existing owners, v3 prompts/state, generated templates, adapters, VERSION,
permissions, CI and protected source-publication inputs are unchanged. Physical
v3 retirement belongs to #197; real dispatch to #163; evaluation/Decision View
to #216; Admin activation to its independently reviewed owner.

## Ticket and lifetime

`TaskTurnContext` holds descriptive repository/Task/full branch, Turn correlation,
Record/Contract IDs, exact HEAD/tree and factual-view digest. Its IDs do not grant
authority. The host derives this context from fresh owner-validated facts.

`WorkUnit` binds one objective, one installed `LeafRole`, read/exclusive-edit
scopes, constraints, dependencies, expected outputs, stop conditions and finite
`UnitBudget`. A host roster fixes each role to its exact provider/model/executor;
there is no fallback model, recursive leaf delegation or caller model override.
Verifier/reviewer/security roles are read-only. Root dependencies may be empty;
dependencies can name only earlier units in the same bounded Turn.

The only terminal leaf outcomes are:

```text
COMPLETED
BLOCKED
NEEDS_APPROVAL
NEEDS_DECISION
```

Missing, multiple, malformed and unknown outcomes or outputs fail closed. A
blocked ticket never resumes. Correction creates a new ticket/ID, still with a
separately authorized scope and exact installed model. A pending dependent of a
terminally unsuccessful unit remains unresolved and cannot start, but does not
reserve scope against an independent correction. Old IDs cannot be reused in
the same coordinator.

`TurnCoordinator` offers `add`, `ready`, `start`, `finish`, `result`, `units` and
`discard`. It is a **single serialized host control plane**, not a thread/process
executor. Hosts must serialize its calls even when qualified leaves run in
parallel. Read/write and write/write lexical conflicts prevent simultaneous
scheduling; ordered dependencies may serialize overlap. These comparisons do
not resolve filesystem aliases, symlinks, case folding, hardlinks or mounts and
are **not filesystem authorization**.

Turn defaults are 32 units, 4 running slots and 32 lifetime dispatches. Pure
upper bounds are 128. Unit defaults are 300 seconds, 32 KiB output and 64 KiB
context; upper bounds are 3600 seconds and 1 MiB each. Output must be at least
512 bytes so a bounded terminal failure can always be recorded. Objectives,
paths, lists and serialized results are independently bounded. Host adapters
must enforce actual time, I/O, context and output limits; post-return timing
does not stop a hung process.

Discard clears Python bookkeeping only and closes the coordinator. It never
removes files, Git objects, metadata, Task authority or collaboration history.
The facade refuses reset/discard/handoff during its active dispatch. A replacement
agent constructs a new facade and calls `begin_turn()` to observe durable facts
again; it never needs old ticket enumeration or a session journal. Work Unit IDs
are optional correlation/provenance, not a condition of Evidence validity.

## Host dispatch and exact subject

`TurnOrchestrator` construction is privileged trusted-host assembly. Agent-facing
work is supplied as typed tickets, not process/cwd/PID/permission overrides.
`RegisteredWorktreeReference` is an opaque owner-issued registry handle, not a
path. #163 must resolve and supervise the actual registered worktree/executor.

`QualifiedHostDispatcher` requires explicit scope, subject-custody, no-delegation,
read-only, time and output enforcement contracts. The authorizer receives the
**whole immutable dispatch request and digest**, including scopes and budgets.
A positive #192 diagnosis is descriptive only; separate current host execution
authorization must return literal `True`. Configured policies determine which
operation-specific diagnoses are required; there is no universal global READY.

Every dispatch checks exact role/provider/model, registry/Task/context/HEAD/tree
binding and fresh facts around prerequisite, authorization and resolver callbacks.
Readonly inspection receives a native Git base/HEAD/tree request, not a required
ad-hoc rendered diff. A general-role edit requires an independently host-derived
`EditObservation` matching the before/after strong subject and every changed path
inside its authorized scope. Physical alias/mount/admin confinement belongs to
the qualified host, never lexical strings or leaf prose.

`ExactSubject` is a strong host checkout observation covering index, ignored and
untracked contents and relevant product state. #144 dirty counts alone cannot
prove stable dirty bytes. Readonly work requires a clean exact subject with
unchanged HEAD/tree/Record/Contract/fingerprints before and after inspection.
Nonzero #144 status cannot be overridden by a host value claiming clean.
HEAD/authority movement requires fresh observation/replanning. General edits may
change dirty bytes at the same committed HEAD, but cannot make old verification
apply to the edited checkout. A metadata-only append with unchanged canonical
authority/product subject does not invalidate readonly inspection merely because
the metadata tip advanced.

Provider and protocol failures produce terminal bounded failures with the
attempted provider/model retained. There is no retry under another model or
silent conversion of a failure into completion.

## Approval and semantic direction

`OperationalRequest` retains operation identity/class, scope, purpose, evidence,
least privilege, safe alternatives and configured authority for independent host
reevaluation. A leaf request is not permission. A typed host `allow`/`approved`
resolution is recorded for the parent to plan a **new** bounded unit with fresh
authorization; `deny` stays denied. It does not trigger mutation or auto-approval.
Routine authorized mechanics need no Human Ask merely because they mutate.

Only a host policy independently establishing contract-neutral mechanical scope
may resolve a `mechanical` decision. It must not trust the leaf's category label
as semantic authority. Product, requirement, architecture, intentional trade-off
and semantic-conflict choices remain Human direction. These are typed escalation
handoffs, not fabricated #216 Decision Views, READY references or Human approvals.
Callback failures or subject movement cannot consume a resolution for old facts.

## Self-sufficient Evidence

Verifier/reviewer/security observations are separate typed host observations,
strictly bound to their corresponding role/kind and exact inspected subject.
Leaf prose, `COMPLETED`, ticket IDs and caller-written PASS strings are never
verification evidence.

The host check registry contains stable readonly `project::*` check IDs and
rejects repair, install and bootstrap commands. Each required check must be
accounted for exactly once, with execution state, exit code, elapsed duration,
output digest and optional artifact reference. All executed exit-zero checks
are required for PASS. Failed, missing, unknown, unavailable, not-run or malformed
checks cannot pass. A verifier claiming completion without executed proof is
blocked; valid negative observations can still be promoted as FAIL/UNAVAILABLE.
Review/security observations use OBSERVED, not an inferred verification PASS.

`promote_verification` encodes a capability-owned #145 envelope with exact
HEAD/tree/base, Record/Contract, scope and checkout fingerprints, producer
role/provider/model/executor/qualification, concrete checks/findings and result.
No Work Unit ID/history lookup is required to understand it. This schema is not
a redesign of #193 or a global interpreter of other producers' opaque Evidence.

Publication uses only a narrow trusted publisher backed by the existing #217
writer. The facade confirms the **exact returned candidate** is reachable from
the remote metadata ref with its required Evidence object, then reads it through
#145 and compares canonical bytes. Boolean ACKs, local-only metadata objects or
substituting a newer tip do not prove durable publication. Fresh subject checks
continue around callbacks; a detected move does not delete already-persisted
Evidence or authorize a moved subject.

## Turn report, snapshot and checkpoint

`handoff` records a concise summary and bounded unresolved descriptions, explicit
Evidence references, a #144 Snapshot and a #194 checkpoint receipt. Pending is a
ticket scheduling description, not a fifth terminal leaf outcome or Task phase.
No Work Unit archive is written.

The facade confirms referenced Evidence, calls the existing Snapshot owner and
reads the exact immutable snapshot graph. Its **post-publication metadata commit**
is distinct from the older metadata tip captured inside the View. The new
checkpoint must reference the snapshot publication commit/ID and exact
repository/Task/branch/subject/Record/Contract.

Handoff also requires the installed #194 `checkpoint_observer`: the actual open
PR number and exact remote Task HEAD come from fresh facts, not a caller selector.
That owner reader is passed to #144 Snapshot capture, retaining the current
authenticated PR and predecessor graph so #194 can consume the snapshot. The
writer request explicitly carries this exact PR number.

The #194 writer alone is not a receipt: a separately installed authenticated
owner reader must freshly confirm the exact PR/comment/subject/principal twice,
with subject and immutable-snapshot rechecks. A matching DTO from an unqualified
callback is not an authenticated checkpoint. These writer/reader adapters must
retain #194's actual principal and checkpoint integrity rules. There is no
direct GitHub publication surface here.

After the write, only the exact receipt-derived new checkpoint may replace the
previously observed checkpoint in this handoff's freshness guard. An unchanged
old checkpoint is not final confirmation; an unrelated new checkpoint, wrong PR,
snapshot, subject or metadata commit is refused. This exception is local to the
post-write handoff, not a general allowance for checkpoint drift during dispatch.
Successful handoff ends applicability of the old Turn; further execution needs
a freshly observed Turn context.

After handoff, dropping all Work Units leaves Git, #191 Record/Contract, #145
Evidence, #144 Snapshots and PR/checkpoints intact. Later turns re-observe those
owners; evaluators, resume, integration and cleanup cannot require WU history.

## Main Next and activation gates

Main's pure `select_next_issue` takes factual candidate inputs. An explicit open
Issue is selected descriptively even if prerequisite eligibility is unknown or
ineligible; that does not authorize execution. Autonomous selection requires a
complete feed with exactly one eligible candidate and no unknown open candidate.
Multiple/ambiguous choices require Human product direction, not guessed priority.
Missing facts may first be re-observed within existing read authority. This policy
does not write canonical `next action` or Task lifecycle state.

The target UX is two sequential normal Tasks without manual lifecycle-command
bookkeeping **after #163/#216 downstream integration**. The dormant policy
connects owner capabilities conceptually; no giant runner, current dogfood success,
live process installation, real provider/checkpoint authentication, filesystem
custody qualification or evaluator activation is claimed by this subject.
Python dataclasses and flags are protocol/configuration bindings, not cryptographic
attestation or a sandbox for arbitrary same-process code replacing the host.

## Verification surface

```sh
python3 -B -m unittest discover -s tests -p 'test_turn_*_v4.py' -v
python3 -B -m unittest discover -s tests -v
```

Tests exercise pure ticket bounds and qualified host test doubles over temporary
Git/metadata fixtures. Measured temporary readonly check fixtures are not the
production project's full verification suite or a real model dispatcher. Exact
executed counts, durations, file-sharded versus monolithic results, skipped
checks and independent reviews belong in the immutable implementation handoff;
documentation or qualification flags alone are not PASS evidence.
