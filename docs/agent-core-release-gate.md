# Agent Core release gate

This is the operator guide for releasing a change whose Agent Core behavior may
affect downstream repositories. It is a gate and evidence protocol, not a
merge or release publisher. The canonical path is:

```text
Issue -> Draft PR -> full CI/review/security
      -> release-candidate-check
      -> detached exact candidate
      -> exact downstream dogfood
      -> dogfood-evidence-record
      -> release-ready-check
      -> human Ready/Merge
      -> release-gate-check
      -> release
```

## Boundaries and invariants

* There is no automatic merge and no automatic release publication. A human
  decides whether to merge and separately performs the release.
* Run the operator surfaces only from a trusted operator process and
  environment on a trusted local filesystem without a hostile same-identity
  process. Such a process can replace any operator-owned checkout or evidence
  regardless of mode bits. As with any dynamically linked command, a process
  already started under hostile loader injection is outside the tool's
  recoverable trust boundary. The launcher removes loader variables before
  starting its validated, root-owned Python interpreter.
* A release tag is not a testing primitive. Tests and dogfood bind directly to
  immutable commit IDs and tree IDs; the tag/version is an output of successful
  validation.
* The gate implementation is itself an immutable commit subject. Every command
  requires a clean Templates source worktree, resolves its exact full `HEAD`,
  and verifies the live launcher and Python gate tool against their regular Git
  blobs and executable modes at that commit. It repeats the implementation
  `HEAD`, content, mode, and cleanliness checks before returning success and
  reports the commit as `implementationHead`. No release tag is required.
* Do not release before downstream dogfood for a downstream-sensitive change.
* A moving PR head invalidates all candidate, CI/review/security, and dogfood
  evidence bound to the previous head. Re-run the gate from the new head;
  the preferred repair is another commit on the same Draft PR, not a new PR
  whose history obscures the review context.
* The candidate worktree is a clean, detached checkout of the frozen exact PR
  head. It is suitable as the exact-revision source for
  `agent-core::maintenance-finalize` when an older consumer needs the
  source-side bridge; it is not a place to edit or commit the candidate.

## Agent Core SemVer boundary

This document describes the existing Templates PR, CI, dogfood, and
post-merge tree-equality gate. It is a versionless historical check with
respect to Agent Core SemVer: it is **not** the future v4 Agent Core release
gate, does not make the currently distributed integer `VERSION` 3 a stable
SemVer release, and does not establish that a v4 release is ready. The optional
`[version]` in `release-gate-check` is only for its existing GitHub
Release/tag-absence assertion; it is not an Agent Core SemVer identity check.

The future policy and read-only local identity check are documented in
[`agent-core-versioning.md`](agent-core-versioning.md). A future candidate
must match its installed version marker and its exact
`components/agent-core` payload/source revision identity; evidence must bind
`agentCoreVersion`, `agentCorePayload`, and `templatesSourceRevision` to CI
and dogfood of that exact candidate. A `planned` release identity is never
release-ready. `just agent-core::release-identity-check` performs the local
version/payload comparison, but it is not yet wired to this historical gate
and does not establish the prior published-release identity on its own. Issue
#141 owns the full future matrix and binding evidence; issue #142 owns the
Admin cutover. This gate does not implement or assert those future checks.

## Intended Just surfaces

These are the narrow operator surfaces for the release gate:

```text
just agent-core::release-candidate-check <pr>
just agent-core::release-candidate-worktree <pr> <path>
just agent-core::dogfood-evidence-record <pr> <operation> [task] [downstream_pr]
just agent-core::release-ready-check <pr>
just agent-core::release-gate-check <pr> <merge-commit> <dogfood-head> <dogfood-tree> [version]
```

`release-candidate-check` verifies the current open PR and proves that the
canonical `.github/workflows/template-ci.yml` workflow has exactly one
unambiguous `pull_request` run for its exact head, with the current attempt
completed successfully. The workflow must be the exact active canonical
workflow, and its workflow ID, path, run ID, and attempt are returned as an
immutable Template CI identity; list, detail, and final list observations must
retain that exact run/attempt identity. It also requires the complete GitHub
status rollup to be successful; an unrelated green context cannot substitute for Template CI.
Human review and security review remain explicit operator prerequisites rather
than inferred CI evidence. The result identifies the exact PR head that can be
frozen.

`release-candidate-worktree` creates or verifies a clean detached worktree at
that head; `<path>` must resolve under the source checkout's `.worktrees/` (a
name such as `pr-123-rc` or an explicit `.worktrees/pr-123-rc` both work), must
be disposable, and must not already contain unrelated work. Before any fetch or
checkout it requires the canonical Templates HTTPS `origin` and rejects local
Git include, URL rewrite, HTTP, credential, filter, protocol, SSH/proxy, custom
upload-pack/receive-pack, hooks, and worktree execution overrides. A missing
candidate is materialized as a standalone detached Git worktree: its private
repository is initialized inside the destination, then the exact commit is
fetched only from the pinned canonical HTTPS URL and checked out without
consuming the source repository's local transport/filter configuration. An
existing destination is accepted only when it is already the exact detached,
clean candidate root. Neither surface merges, pushes, tags, or publishes.

`dogfood-evidence-record` records the auditable PASS outcome and concise
operation identifier (and, where applicable, the downstream Task and PR) after
the operator has run AKV against the exact candidate revision. The record binds
the Templates repository, PR, candidate head/tree, canonical downstream
repository, Template CI workflow ID/path and run ID/attempt, operation,
optional IDs, PASS, and timestamp. New records are schema version 2 and carry
all four Template CI identity fields. It does not claim to capture raw command
logs and does not alter downstream Task State, PR metadata, or merge state.

There is exactly one immutable PASS record per candidate head/tree. Its
operation identifier names the overall externally executed dogfood run. When a
run has several required guarded steps, complete or safely resume all of them
and record the aggregate PASS only after every applicable step has passed.

`release-ready-check` is a read-only pre-merge assertion. It requires the PR to
be open and bound to the exact current candidate, an exact successful canonical
Template CI workflow/run/attempt and complete status rollup, and exactly one
strict schema-v2 PASS evidence record matching the current PR, head, tree,
downstream repository, and the same Template CI workflow/run/attempt identity.
It revalidates those bindings immediately before the operator marks the PR
`READY_FOR_MERGE`; it does not mark the PR ready, merge it, or publish a
release. Dogfood described only in a PR body is not canonical release-gate
evidence. For downstream-sensitive changes, human Ready/Merge must not occur
before this check is PASS.

`release-gate-check` is the final read-only assertion. It requires the recorded
evidence, supplied merge commit, exact dogfooded **Templates** PR head/tree,
and (when supplied) the intended version. It independently verifies the merged
PR's canonical repository, base, head, merge state, and merge commit, then
checks that the merge commit has the same tree as the frozen candidate. The
recorded workflow run is read again at the final boundary so its stable
workflow, run, attempt, event, head, status, and conclusion fields must still
match the immutable pre-merge binding. When a version is supplied, both the
GitHub Release and the exact Git tag must be absent; an existing tag fails
closed even without a Release object. It does not
merge, create a release, publish a tag, or claim that a release was published.
This is point-in-time pre-publication evidence: the separate human-controlled
publisher must create the version tag at the verified merge commit with
create-only/fail-if-present semantics, then create the Release from that exact
tag. It must never reuse a tag that appeared after the gate check.

## Operating procedure

1. Start with the authoritative Issue. Confirm scope, acceptance criteria,
   risk, and the Task Contract. Implement through the normal Task lifecycle
   and open a Draft PR through the guarded publication path.
2. Wait for the complete Templates gate: all required CI/checks, human review,
   and security review. Include failures and unverified items in the PR
   evidence; a green subset is not a gate PASS.
3. Run `release-candidate-check <pr>`. Freeze and record its exact PR-head
   OID. If the head moves, stop and repeat this step after the Draft PR fix.
4. Create the detached clean worktree with
   `release-candidate-worktree <pr> <path>`. Verify its `HEAD` equals the
   frozen PR head and its tree is clean. Do not use a tag or a mutable branch
   as the candidate identity.
5. From that worktree, run the exact-revision AKV downstream dogfood. Use the
   `maintenance-finalize` bridge when the consumer requires it, supplying the
   full candidate revision. After the externally executed dogfood has passed,
   record its concise overall operation with `dogfood-evidence-record`; retain
   the detailed command output separately and include any downstream Task/PR
   identity in the narrow record.
6. Require every applicable dogfood operation to PASS. A blocked, skipped, or
   unexecuted operation is not PASS. Preserve and resume partial guarded
   downstream progress rather than resetting it or launching duplicate work.
7. Run `release-ready-check <pr>` immediately before the human marks the PR
   `READY_FOR_MERGE`. This must be PASS for downstream-sensitive changes;
   dogfood claims in the PR body alone do not satisfy the evidence gate.
8. After the human marks the PR ready and merges it, obtain the actual merge commit and run
   `release-gate-check <pr> <merge-commit> <dogfood-head> <dogfood-tree>
   [version]`. Release only after this check is PASS and the operator has
   retained the evidence.

## Scope of the gate

Treat a change as downstream-sensitive when it touches or changes behavior in
any of these areas, including their permissions and authority boundaries:

* automation upgrade and maintenance lifecycle;
* Task lifecycle, Task Contract, Work Units, and recovery/resume behavior;
* PR publication and post-merge finalization;
* cleanup/discard and recovery paths;
* source bridges, including exact-revision maintenance finalization;
* authority or permission changes affecting any of the paths above.

For these changes, dogfood the exact candidate, not an installed version
selected by a tag, branch, or floating upstream. Do not edit merged PR
metadata or mutate Task State merely to make a check pass. Fix the source
Draft PR, or stop for human handling when the canonical workflow cannot
continue.

## Evidence and recovery

Evidence is local and auditable under:

```text
.agent-core-release-gate/evidence/
```

Each record is immutable and must not overwrite an earlier record. Schema v2
binds the Templates PR number and exact frozen PR head/tree, the canonical AKV
repository, the exact Template CI workflow ID/path and run ID/attempt,
operation, PASS outcome, timestamp, and Task/downstream PR when present. The
loader accepts schema v1 structurally so old files remain readable; post-merge
trusts v1 only for the narrow hardcoded issue #117 PR/head/tree/run/workflow/
attempt/merge binding and exact safely read evidence-file SHA-256. There is no
migration or evidence-upgrade command; do
not rewrite evidence files. This is a narrow local operator self-attestation within the trusted
operator/filesystem boundary, not a cryptographic downstream receipt. Its
filename is exactly the SHA-256 digest of the canonical repository,
PR, head, tree, and downstream-repository subject followed by `.json`; records
under arbitrary or mismatched names are rejected even when their JSON schema is
otherwise valid. Run evidence recording and final gate consumption from the
same operator source checkout so this ignored local directory remains
available. Keep detailed downstream logs alongside the operational record, and
retain or archive them according to repository practice.

If a guarded downstream operation stops part-way through, preserve its
receipts and resume through its canonical recovery/resume surface. Do not
delete evidence, recreate a Task to hide a failure, rewrite the Task Contract,
or manually change Task State. A new PR head requires a new candidate binding
and new evidence, even if the resulting files are byte-identical.

## Historical fixture (PR #115)

PR #115 is a historical fixture for checking merge/tree reasoning only:

```text
PR head:    35b562fca9a4dbc0656b79f98a356c6183034469
PR tree:    0eeaad174d2ff519a2db4b23ac80252c48d6bec7
merge:      e50b541e23e1ddb2014f6bfc94855262f47e8162
merge tree: 0eeaad174d2ff519a2db4b23ac80252c48d6bec7
```

It is historical documentation, not a production input or a substitute for
current CI, review, security, dogfood, or human merge evidence. Agent Core
`VERSION` was integer **3** at the time of this fixture; this is not an Agent
Core SemVer release identity.

## Issue #117 legacy compatibility binding

This is the sole v1 post-merge compatibility exception. It is an exact,
hardcoded binding and is not a general legacy trust rule:

```text
PR:         117
head:       9eb86882bbb569aeae356c59e7d459e9fd8ba4f9
tree:       7c5c6fb0bd5e0e881510e256f0b76e95c304c9c5
merge:      67fbc64e1127c19b9e424ab99dff4626f67be63b
workflow:   .github/workflows/template-ci.yml (ID 329870494)
run:        33584314368 (attempt 1)
evidence:   317dd08ea4840dac4a820043630315ece7c098e13ff77c992af63568442190ce
```

The evidence value is the SHA-256 of the exact existing serialized file bytes.
The gate opens the canonical evidence file without following symlinks, checks
its owner, regular-file type, private mode, schema, and subject-derived
filename, and hashes the bytes read. Semantically equivalent JSON with different
bytes is not the known record. The old file is neither rewritten nor migrated.

Post-merge accepts a singleton `pull_requests` association only when it matches
the merged PR exactly. An empty association is accepted only with immutable v2
evidence, or with the exact guarded issue #117 compatibility binding above
after merged-PR proof succeeds;
non-empty conflicting or ambiguous associations always fail.
