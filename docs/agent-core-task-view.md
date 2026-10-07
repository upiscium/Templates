# Factual v4 Task View and immutable handoff snapshots

**Status:** staged #144 capability on the merged #191 Task Record/Contract,
#145 Evidence, and #217 metadata substrate. It does not activate the v4 runtime,
change VERSION 3 or generated templates, or implement a live publication endpoint.

Task View answers **what is true now**, not what should happen next. It has no
workflow phase, readiness/prerequisite policy, next action, reconciliation,
repair, or CONTINUE/READY/FIX/HUMAN_DECISION semantics. Those responsibilities
remain outside #144, including #192/#194/#195/#216. Normal observation never
reads legacy `.task-state/**`; bounded cutover remains #142.

## Live observation and ownership

`components/agent-core-v4/task_view.py` exposes:

```python
observe_live_task(store, *, task, branch_ref, base_revision,
                  selected_evidence=(), default_branch_ref=None,
                  observe_remote_head=False, pr_number=None,
                  github_reader=None) -> dict
```

The trusted caller supplies exact repository/Task identity through the existing
store, the expected full Task branch ref and historical creation base. The
projector obtains current facts from their owners, not conversation or copied
Task State. The returned schema-version-1 mapping has exactly:

```text
schema_version, repository, task, subject, branch_ref,
git, authority, evidence, github, previous_checkpoint
```

* **Git:** exact local commit HEAD (`subject`), HEAD tree, current symbolic
  branch or detached HEAD, branch equality with the Task binding, porcelain-v2
  superproject index/worktree/untracked/conflict change counts,
  and registered
  worktrees. Paths are canonical base64 of Git's path bytes, avoiding locale
  decoding loss; registration is not proof that another checkout is accessible
  or clean. Bare registrations have a null HEAD. Worktree entries are sorted.
  Status uses `--ignore-submodules=all` and omits gitlinks. A separate
  index-vs-HEAD raw diff (no patch, external diff, textconv, rename detection,
  or child worktree access) reports `staged_gitlinks` as its own count.
  Nested submodule HEAD/content/dirty worktrees are explicitly
  `submodule_worktrees: "not_inspected"`. Zero counts do not assert those child
  repositories are clean. Child repositories have their own executable filter
  config, so superproject observation must not recurse into them.
* **Selected default-branch comparison:** the caller may explicitly select a
  local `default_branch_ref`. Git supplies that ref's commit and ahead/behind/
  diverged/equal counts. The field does **not** establish that the selected ref
  is GitHub's current default branch; the trusted host must resolve that role.
  This local comparison is distinct from current remote-default observation.
* **Remote Task branch:** opt-in exact `ls-remote` observation through #217's
  bounded Git transport runner, never a cached remote-tracking ref. Missing or
  failed observations are explicit. This reuses the substrate's internal runner
  because it has no public product-branch reader; no new transport/writer is
  introduced. Existing transport activation constraints still apply.
* **Record/Contract:** #191 resolves the current Record pointer and immutable
  Contract from one pinned metadata commit. The View records that commit,
  Record ID, Contract ID/digest, historical base, branch and explicit Human
  disposition. It does not copy requirement bodies or infer `active` from an
  absent disposition. Missing or invalid canonical authority fails closed.
* **Evidence:** explicit `EvidenceRef(evidence_id, subject)` selections are
  validated by #145 from the same metadata commit, sorted by identity, and
  duplicate IDs rejected. The projection reports only ID, subject and
  `matches_current_head`. This is exact-subject equality, **not** PASS, validity,
  successful verification, replacement authority, or applicability to dirty
  files. No latest/effective selection or result interpretation is added.

No timestamps, random values, opaque Evidence payloads, requirement bodies,
dirty filenames, private chain-of-thought, or raw tool logs enter the projection.
Equal authoritative inputs and selected observation options produce equal
machine mappings and equal canonical Snapshot bytes/IDs.

## GitHub and previous checkpoint facts

`pr_number` is a positive integer independent of the Issue-backed Task number.
It must be supplied together with a trusted `github_reader`. The frozen
`GitHubPullRequestRequest` binds repository, Task, PR number and Task branch.
The host reader returns a typed `GitHubObservation` containing:

* `GitHubPullRequestFacts`: exact base/head repositories, requested PR number,
  `open`/`closed`/`merged`, draft Boolean, base/head branch names and full head OID;
* optional `PreviousCheckpoint`: opaque durable checkpoint identity, exact
  subject, metadata commit and Snapshot ID.

The reader must authenticate against **current GitHub**, check this exact PR
and its durable checkpoint provenance, and select the latest previous checkpoint
under the owning capability's rules. It must bound its own time/output. The
projector neither invents a checkpoint protocol nor interprets a caller's prose
or LLM output as authority. Same-repository Task PR bindings are supported here;
fork collaboration is not silently adopted.

Omitting the reader produces `not_requested`. Callback failure or invalid facts produces
bounded `unavailable` codes without propagating secret-bearing exception text.
A successful reader with no prior checkpoint produces `absent`. A supplied
checkpoint whose metadata Snapshot cannot be resolved produces checkpoint
`unavailable` while retaining the independently observed PR, Git and authority
facts. Absence/unavailability never manufactures a workflow phase.

For a resolved prior checkpoint, Git establishes the number of commits after
its subject when it is an ancestor of current HEAD. Non-ancestry is explicit;
missing/corrupt objects or command errors give `unknown`, not a fabricated count.
Dirty/uncommitted work is separately reported as current counts. It is not
possible to attribute that work temporally to the checkpoint from these facts.

## Immutable Snapshot boundary

```python
encode_snapshot(repository, task, subject, boundary, view) -> (snapshot_id, bytes)
decode_snapshot(bytes, *, snapshot_id, repository, task, subject) -> payload
TaskViewSnapshots(store, authorize=host_capability).capture(...) -> SnapshotPublication
TaskViewSnapshots(store, authorize=host_capability).read(
    metadata_commit, snapshot_id, *, task, subject) -> payload
```

The shared #217 envelope kind is `task-view-snapshot`, with exact repository,
Task and product subject. Its closed payload is:

```json
{"schema_version":1,"boundary":"turn-end","view":"<the machine mapping above>"}
```

The example's `view` placeholder stands for the actual object, not a string.
Only `turn-end` and `explicit-handoff` boundaries are accepted. The host chooses
the explicit boundary; intermediate tools do not automatically create snapshots.
The subject is exact Git HEAD, **not a digest of dirty contents**. Canonical
bytes are #217's compact key-sorted UTF-8 JSON without a trailing newline; the
Snapshot ID is SHA-256 over `agentcore-metadata-object/v1\n` plus envelope bytes.
The closed schema rejects extra fields, including future/new checkpoint IDs.

Capture freezes canonical bytes before authorization. A frozen authorization
request binds repository/Task, subject, Snapshot ID, Record/Contract IDs and
boundary; only literal `True` from a trusted host capability permits storage.
The callback is not a credential or an authority issuer. The host must control
construction and generic metadata writers and apply its own caller and secret
publication policy. A well-formed persisted object is not an attestation that
an arbitrary producer measured its reported Git/GitHub facts.

Before persistence, at #217's candidate seam (before and after optional host
`on_candidate` intent recording), and after exact remote confirmation, capture
reobserves Git, Task authority, PR and previous checkpoint facts. Observed drift
rejects the receipt rather than silently refreshing the frozen object. The
post-confirmation check also covers #217's identical-object/no-op path. The
optional exact-candidate callback lets a host retain uncertain-publication
intent; the intent/recovery protocol remains #140. A post-publication failure
may leave the immutable object remotely stored; it is not rollback or permission
to post a checkpoint. Preserve intent and re-observe rather than infer success.

Persistence appends only the Snapshot object through `MetadataStore.publish`,
then confirms the **exact metadata commit and object** before returning
`SnapshotPublication(metadata_commit, snapshot_id, subject, boundary)`. There
is no current Snapshot pointer, Task pointer update, product branch commit,
GitHub write, PR creation or checkpoint posting. #217 may fetch metadata objects
and update FETCH_HEAD; product HEAD/tree/index/status/refs/config remain unchanged.

Exact reads validate the closed Snapshot schema and its referenced immutable
Record/Contract/Evidence graphs at their captured metadata commits, not refreshed
current authority. Previous Snapshot links are iteratively cycle checked and
bounded to 64 links under one shared #217 validation deadline. Historical
Contract bases need not be available in the product clone. A clean replacement
clone can resolve the object with metadata commit + ID + Task + subject.

The one-way relation is **previous checkpoint -> Snapshot -> new checkpoint**.
A later #194-owned checkpoint can reference the returned subject, Snapshot ID
and metadata commit; its own identity is never needed to construct the Snapshot.
Historical reads return historical facts unchanged after HEAD or authority
moves. They do not authorize that moved subject or perform #216 evaluation.

## Observation limits and verification

Observations are sequential, not an atomic Git/metadata/GitHub transaction.
Guards detect changes at defined seams, not future changes or every ABA event.
Dirty status is count-only: same-count content edits are indistinguishable.
Git reads disable optional index locks, hooks, fsmonitor and replacement objects;
superproject status never invokes child-submodule worktree inspection;
reject command-bearing config/includes and shallow/grafted history; and bound
runtime and output. Trusted executables/host callbacks and #217's separately
vetted direct-ref CAS and bounded metadata transport remain activation gates.

Focused fixture verification:

```sh
python3 -m unittest discover -s tests -p 'test_task_view_v4.py' -v
python3 -m unittest discover -s tests -v
```

Tests use temporary local Git/metadata repositories only. They cover canonical
schemas, independent Task/PR identity, unavailable external facts, exact Evidence
subjects, previous-checkpoint ancestry, bounded graph traversal, capture races,
authorization, command safety, fresh-clone reads and product-state isolation.
They do not publish real metadata, create a real checkpoint, or claim live-host
runtime activation. Generated template smoke remains the unchanged VERSION 3
runtime's separate CI surface, not evidence of staged Task View activation.
