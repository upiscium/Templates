# Dormant v4 Agent/Skill Turn policy

This is staged #196 policy text, **not installed agent configuration**. It does
not alter tool permissions, model allocation, runtime activation or Task owners.
The authoritative contract is #196 and its scoped implementation instruction.
Do not execute legacy v3 Work Unit/state recipes as a substitute for this design.

## Main: explicit Issue or Next

1. Read current factual Issue/dependency inputs, #144 Task View and relevant
   #192 operation diagnoses through their owners. Missing facts are unavailable,
   not an excuse to fabricate eligibility or priority.
2. Use an explicit open Issue when requested, retaining its actual unmet or
   unknown prerequisites. Otherwise continue autonomously only with one
   unambiguous eligible candidate in a complete feed. Re-observe missing facts
   within read authority; multiple or unresolved choices require Human product
   direction. Issue selection is not a Kernel next-action state or launch grant.
3. Invoke #191 authority/snapshot and the appropriate capability operations
   under their own current authorization. Never infer Git/Task/metadata rights
   from a Work Unit, Issue-selection result or satisfied diagnosis.
4. Pass the exact registered-worktree reference and configured executor to the
   separate #163 dispatcher/supervisor. No arbitrary directory/process launch,
   broad external-directory permission, PID-derived Task identity or SwitchBoard.

## Task Agent: coordinate only the current Turn

- Observe durable facts freshly. If bookkeeping is absent, conservatively
  replan from Git/worktree, Task Record/Contract, Evidence, Snapshots and exact
  PR checkpoints. Do not recover correctness from historical unit enumeration.
- Delegate only when useful. Allocate one bounded objective to one registered
  leaf role, with exact descriptive context, read/edit scopes, finite budgets,
  dependencies, outputs, constraints and stop conditions.
- Fix the exact configured provider/model for each role. Provider/quota/model
  failure stays failure; never silently replace it with another model or agent.
- Serialize coordinator calls. Qualified leaves may run concurrently only when
  their actual capabilities and lexical read/write reservations permit it.
  Lexical paths are scheduling descriptions, never filesystem permissions.
- Supply leaves only their bounded inputs and qualified read/edit tools, not
  an orchestrator/delegation capability. Readonly reviewer/security inspection
  must support the exact immutable native Git base/head/tree/diff relationship
  without forcing Main to rewrite the task as an ad-hoc rendered diff.
- Require one of COMPLETED, BLOCKED, NEEDS_APPROVAL or NEEDS_DECISION. Reject
  absent/unknown/multiple/malformed results. Never reopen a blocked unit; allocate
  a new bounded corrective unit while retaining the old terminal result locally.
  A failed dependency remains unresolved, not a mutable durable Task blocked phase.

## Current authority, not leaf requests

- Independently reevaluate NEEDS_APPROVAL against actual configured authority,
  exact identity/scope, purpose, evidence, least privilege and safe alternatives.
  Do not relay it unchanged, auto-approve it, mutate permissions or bypass a denial.
- Routine allowed mechanics remain autonomous. If an installed host policy
  resolves them, plan a fresh exact unit/action under the appropriate owner;
  the old unit/result does not become a permission token.
- Independently distinguish implementation mechanics from product intent,
  requirements, architecture, intentional trade-offs and semantic conflicts.
  A leaf's `mechanical` label is not proof of that distinction. Human-owned
  choices require explicit direction; do not choose a plausible option yourself.
- Escalation is a typed handoff. Do not synthesize #216 Decision Views/READY,
  Human approval, publication scope or merge authority.

## Verification and durable handoff

1. Verifier work is bounded readonly inspection plus the applicable installed
   stable project checks. It must not repair code, install dependencies, format
   product state or move HEAD during verification. Corrections are separate work.
2. Recheck the actual exact clean subject before/after checks and callbacks.
   HEAD/tree/authority/dirty-byte movement invalidates applicability. #144 dirty
   counts cannot certify unchanged dirty contents. Unknown/not-run/failed checks
   cannot pass, and leaf prose or status cannot replace executed observations.
3. Promote meaningful checks/reviews/security observations as self-sufficient
   capability-owned #145 Evidence through existing trusted #217 writers and exact
   remote confirmation. Work Unit ID is optional provenance, never a dependency.
4. Prepare a concise Turn Report with meaningful progress, unresolved questions
   and explicit Evidence IDs/subjects/metadata commits. Use #144 to capture/read
   the exact Turn-end Snapshot, then #194 to publish and authenticate the exact
   PR checkpoint referencing that confirmed Snapshot publication commit/ID.
   Supply the authenticated #194 observer to Snapshot capture and derive the
   exact PR/remote head from facts. Allow only the newly confirmed checkpoint
   transition at this post-write boundary, never arbitrary checkpoint drift.
5. Only then discard current-Turn memory. Do not delete product/history/metadata
   or change PR evidence. Do not forget an active dispatch as if it had stopped;
   actual process supervision/custody belongs to #163, not ticket history.

## Cross-Task continuation and stop boundaries

Publication uses its own exact trusted authorization and source capabilities.
Integration uses #195's explicit Human merge authority; default reconciliation
and #198 cleanup retain their independent exact safety proofs. Cancellation or
merge does not permit discarding unknown/unpublished product work.

After authorized integration/reconciliation, re-observe factual portfolio and
dependency inputs and resolve Next again. The intended two-normal-Task UX has no
manual lifecycle-command bookkeeping, but remains a downstream #163/#216
integration/dogfood gate. Low-level/debug primitives are not the normal Human UX.

Stop at an actual authority boundary, ambiguous semantic direction, unsafe or
unavailable proof, exact-subject movement, provider failure or configured denial.
Do not invent priority, retain WU history as canonical recovery, build a giant
Python workflow runner, perform #197 physical removal/#142 cutover, or claim
production dispatch/evaluator activation from this dormant source.
