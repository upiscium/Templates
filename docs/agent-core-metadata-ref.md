# Agent Core metadata reference protocol

**Status:** v4-only protocol and tested substrate staged for AgentCore 1.0.0.
It is not active in the Agent Core VERSION 3 runtime. Authority, transport,
checkpoint, version, and distribution integration remain separate gates (§11).

This document specifies the storage and publication boundary for the next
metadata module. It does not define the meaning of Task Records, Contracts,
Evidence, Task Views, Git-derived identity, PRs, or checkpoints. Those semantics
remain with the owning issues listed below.

## 1. Scope and ownership

The metadata module stores its complete Git-visible history behind exactly one
ref in the product repository:

```text
refs/agentcore/metadata
```

The ref points to a metadata-only commit history. Object schema version `1`
is a wire-format number, not a public AgentCore release version. The
source/runtime baseline remains Agent Core VERSION 3. Issue #159's version and
release-identity foundation is merged, while explicit v4 runtime/distribution
activation remains separate and incomplete. This module must not be imported
by, copied into, or invoked from the v3 runtime or generated templates. The v4
module remains staged until that activation is explicitly completed.

The only Git ref this protocol may update is the remote
`refs/agentcore/metadata`. In particular, it must not change product branches,
tags, `HEAD`, the product worktree, or the caller's index. A private temporary
index may be used to construct a metadata tree, but it must not replace or
modify the caller's index. Network fetches may add Git objects and update
`FETCH_HEAD`; those are not product-tree or product-ref changes. The protocol
does not commit, push, merge, reset, or check out product content.

The following issues own semantics outside this storage protocol:

| Owner | Boundary |
| --- | --- |
| #189, #191 | Authority migration; Task Record and Contract semantics; caller authority for Task-addressed writes. |
| #140 | Operation-local intent and retry/crash recovery beyond the metadata-specific rules below. |
| #144 | Task View and immutable Snapshot semantics; deriving current worktree and subject identity from Git. |
| #145 | Evidence schemas and evidence semantics. |
| #194 | PR/publication and durable checkpoint posting semantics. |
| #198 | Destructive cleanup; cleanup must not delete this ref or its reachable metadata. |
| #216 | Turn evaluation and Decision View semantics; evaluator writes PR outcomes, not the metadata ref. |
| #159 | Owns the now-merged version/release-identity foundation; this does not activate this module. |

This module does not issue caller authority. A Task-addressed operation must be
invoked with authority already validated under #191 and must bind that
authority to the same repository and Task in the object/pointer. A branch name,
worktree path, metadata object, or possession of the ref is not authority.

## 2. Stored tree and path grammar

Each commit reachable from `refs/agentcore/metadata` has only the following
logical paths:

```text
objects/<kind>/<first-two>/<id>.json
tasks/<task>/record
```

`<kind>` is exactly one of:

```text
task-record
contract
evidence
task-view-snapshot
```

`<id>` is the 64-character lowercase hexadecimal SHA-256 digest defined in
§3. `<first-two>` is exactly the first two characters of `<id>`. `<task>` is
an ASCII positive decimal integer without leading zeroes (at most 128 digits), supplied by the #191 identity layer;
the metadata layer must not silently normalize a different spelling. No other
files, refs, side indexes, or mutable `current` pointers are part of this
protocol.

The record pointer `tasks/<task>/record` is the only mutable tree path. Every
object path is immutable and append-only: an existing object may not be
replaced, edited, chmodded, or deleted. The same path may be encountered again
only with byte-identical content and regular non-executable blob mode. A
different byte sequence at an existing content-addressed path is corruption
and must fail closed.

## 3. Object envelope and identifier

An object is UTF-8 JSON with this exact top-level envelope (no extra or missing
members):

```json
{
  "schema_version": 1,
  "kind": "task-record",
  "repository": "owner/name",
  "task": "217",
  "subject": "<full-git-oid>",
  "payload": {}
}
```

The example values are illustrative. The contract is:

* `schema_version` is the JSON integer `1`.
* `kind` is one of the four exact strings in §2 and equals the path's kind.
* `repository` is an exact-case `owner/name` string (at most 512 characters)
  supplied by the caller's #191 identity layer, not a URL or remote name. Each
  component starts with an ASCII letter/digit and continues with ASCII
  letters, digits, dots, underscores, or hyphens. Compare it exactly; do not
  case-fold or derive it from an untrusted payload.
* `task` is the canonical decimal string above and agrees with the task path for a
  `task-record` pointer target.
* `subject` is a full lowercase Git object ID (40 hex for SHA-1 or 64 for
  SHA-256), not a branch, tag, abbreviation, or revision expression. Validate
  its length against the repository's Git object format. Interpretation of which object type or lifecycle fact it
  denotes remains with the owning semantic layer.
* `payload` is a JSON object. The metadata layer treats its members as opaque;
  it must not infer lifecycle or authorization semantics from them. Capability
  owners must reject/redact credentials and secrets before publication; the
  opaque metadata codec cannot certify a payload as secret-free.

`canonical_json_bytes` is UTF-8 encoding of compact JSON with object member
keys sorted by Unicode code-point order at every level, `,` and `:` separators
without spaces, no trailing newline, unescaped valid non-ASCII characters, and
standard JSON escapes for quotation marks, backslash, and control characters.
No Unicode normalization is performed. Inputs may contain only JSON null,
booleans, signed integers from `-2^63` through `2^63-1`, strings without
surrogate code points, arrays, and string-keyed objects; payloads must be
objects. Floats (even integral-valued floats), duplicate keys, NaN/Infinity,
and noncanonical JSON bytes are invalid. This deliberately narrow profile is
implemented by `json.dumps(sort_keys=True, ensure_ascii=False,
separators=(",", ":"), allow_nan=False)` after type validation; readers
parse, reserialize and compare exact bytes. Protocol vectors in
`tests/test_metadata_codec.py` bind the profile. A future change to these
bytes requires a new schema/domain, not a silent serializer upgrade.

The object identifier is:

```text
id = lowercase_hex(
       SHA-256(
         ASCII("agentcore-metadata-object/v1\n")
         || canonical_json_bytes(envelope)
       )
     )
```

The object file stores exactly the bytes returned by
`canonical_json_bytes(envelope)`; it has no extra transport newline. Its path
is derived from the resulting `id`. Readers recompute the digest from the
stored bytes and reject a path/content mismatch. The domain prefix is exact,
including its single trailing LF; the `v1` in the domain separates this digest
domain from later formats and is independent of `schema_version`.

Objects are limited to 1 MiB of canonical payload, 1 MiB + 4096 bytes of
envelope, and 64 levels of JSON nesting. A reader rejects an oversized object
*before* decoding its bytes. The codec validates complete canonical bytes; the
Git store additionally validates repository object format and metadata history.

## 4. Task Record pointer and compare/exchange

`tasks/<task>/record` contains exactly:

```text
<64-lowercase-hex-id>\n
```

It identifies a `task-record` object in the same metadata commit tree. On read,
validate the exact pointer bytes, resolve the corresponding object path in that
same tree, validate its digest and envelope, and require its `repository` and
`task` to equal the caller's canonical repository and the path's Task ID. Do
not resolve a record through a worktree file, a product branch, or a mutable
pointer outside this path.

Changing an existing record pointer is an expected-prior-ID compare/exchange
(CAS):

1. The caller supplies the expected prior record ID and has #191 authority for
   this repository/Task operation.
2. At the observed remote metadata tip, the stored pointer must equal that
   expected ID before the update is constructed.
3. The proposed new ID must resolve to a valid `task-record` object for the
   same repository and Task.
4. The pushed metadata commit changes only that pointer (plus immutable object
   appends). A concurrent change to the same Task pointer is a conflict,
   **even if** it equals the proposed new ID. A caller recovering its *own*
   persisted candidate commit may separately confirm that exact commit;
   a matching pointer or latest tip is not proof of this operation's checkpoint.

There is no mutable `tasks/<task>/current` pointer. `expected_id = None` is the
exact expected-absent value for first creation; it succeeds only if the
observed tree has no pointer for that Task. #191 must independently validate
the creating caller's Task authority before invoking this storage API.

## 5. Commit construction and publication

The writer uses the remote tip observed by an explicit fetch as the base for
every metadata commit. For a write against observed tip `R`:

1. Fetch and record the exact full OID `R` of the remote
   `refs/agentcore/metadata`. Validate the entire linear append-only metadata
   history and current tree before changing it. When absent, the authorized
   first nonempty publication makes a parentless metadata genesis commit; an
   empty publication cannot create a ref.
2. Validate caller authority under #191, the expected prior Task Record ID,
   each proposed envelope, every derived path, and all immutable-path
   collisions before publication.
3. Construct a new metadata tree from `R`'s tree. Preserve all existing
   entries. Add only new immutable object blobs and the authorized
   `tasks/<task>/record` CAS result. Do not stage product paths.
4. Create one metadata commit whose sole parent is exactly `R` (or no parent
   for genesis). Its tree must contain only paths permitted by §2, with all
   previous immutable paths preserved. There are no merge commits or commits
   with a product parent. Each newly constructed candidate has a distinct
   random commit-message nonce so two independent, byte-identical same-Task
   proposals cannot accidentally share a commit OID; this does not alter the
   deterministic object serialization, and callers must retain the exact
   candidate SHA rather than recomputing it. Before pushing, an operation-local #140
    `on_candidate` callback may persist the exact candidate OID; callback
    failure prevents the ref mutation. Integration MUST persist this intent before an
    operation requiring crash-safe exact-commit checkpoint recovery.
5. Update only `refs/agentcore/metadata` via a **trusted direct-ref CAS** at
   the server mutation boundary. The capability must atomically require that
   the exact named ref is direct (never dereference a symref), that its old OID
   equals the observed OID or it is absent for genesis, and that the new commit
   is its direct fast-forward child. Reject an alternate namespace, symbolic
   ref, moved ref identity, or unexpected old OID *before* any ref mutation.
   The ordinary API fails closed if no trusted capability is configured.
   A client-only `git push --force-with-lease` tests the old OID, **not the
   remote ref type**: a same-OID symref to `refs/heads/main` could redirect
   that push into a product-branch update. Neither a second client-side
   advertisement check nor a post-write failure can make that update safe.
   There is no normal raw Git push or force-update fallback. Fixture tests
   transfer objects separately, then exercise the contract with a Git
   `reference-transaction` hook checking ref type at the locked `prepared`
   boundary and an expected-old `update-ref --no-deref` on an isolated bare
   repository. `--no-deref` alone can replace a same-OID symref rather than
   rejecting the changed ref identity; the locked type guard is essential.
   A test fixture is not proof that GitHub supplies this capability.
6. After the direct-ref CAS attempt, fetch/observe the remote metadata ref again. Before returning
   success or allowing a #194 checkpoint, prove that the exact full metadata
   commit OID `C` is reachable from the observed remote
   `refs/agentcore/metadata`. Also resolve and validate the exact requested
   object IDs from `C`'s tree. A successful push response alone is not proof of
   remote reachability.

### Concurrent remote advance

A metadata CAS rejected because the remote advanced is not retried by
unconditional force. Re-fetch the ref and compare the new remote tip `R2` with the previously
observed `R`:

* Reconstruct the proposed metadata commit with parent `R2` only if every
  intervening tree change is a compatible append: new immutable object paths,
  or a change to a different Task's record pointer whose own expected-prior-ID
  CAS still holds. An identical already-present immutable object is
  idempotent.
* For the same Task pointer, even an exact match to the proposed new ID fails
  the original expected-prior-ID CAS. A #140 recovery path may call `confirm`
  with its recorded exact candidate commit; it must not infer success from
  pointer equality or the newest ref tip. Any other current pointer value conflicts
  with the caller's expected prior ID; stop without overwriting or merging it.
* Reject intervening deletion or modification of immutable objects, unknown
  paths, invalid tree modes, unexpected ancestry, or any change that cannot be
  proved compatible. Do not rebase or amend product commits. “Rebase” here
  means reconstruct an unpublished metadata-only commit on the newly observed
  metadata tip; never rewrite a commit already published to the metadata ref.

On a same-Task CAS conflict, preserve the remote state and report the exact
expected, observed, and proposed IDs. Do not retry with a newly inferred
expected ID; a caller with fresh #191 authority must make a new decision.

## 6. Read validation, reference graph, and security boundary

A reader MUST pin an exact metadata commit OID before resolving objects. It must
not use “latest” lookup, filesystem globbing, an untrusted path, or a payload
value as a Git revision expression. For every tree it consumes, validate:

* the commit is reachable from the intended metadata ref when reachability is
  required, and all reads are from the exact pinned commit's tree;
* every tree path matches §2 exactly; reject extra paths, malformed IDs,
  traversal components, symlink blobs, executable blobs, and gitlinks;
* every object is a regular non-executable blob whose bytes are valid UTF-8
  JSON, whose envelope has exactly the defined members and types, and whose
  content digest and path agree;
* each `task-record` pointer resolves in that same tree to a matching
  `task-record` object with matching repository and Task identity;
* subject spelling is a full OID matching the repository's object format;
  semantic claims about the subject are checked by #144 or the owning schema;
* JSON parsing rejects duplicate object member names and non-JSON values. No
  payload is executed, evaluated as code, or trusted as authority.

The storage layer validates only the graph it owns: a task pointer to its
`task-record` object and content-addressed object paths. Because `payload` is
opaque here, references inside a Contract, Evidence, or Task View must be
validated by its owning schema (#191, #145, or #144). A syntactically
valid and reachable object is not by itself proof that a referenced object is
semantically valid or authorized.

A `task-view-snapshot` may refer only to a checkpoint that was already
durable before that snapshot was created, using the reference semantics owned
by #144. It must not refer to a checkpoint being created from its own metadata
commit. A new checkpoint is not required merely to create or publish a
snapshot. This storage protocol does not impose a field name or schema for that
checkpoint reference.

The staged Git store limits a tree to 50,000 entries, 64 MiB of blob bytes,
and 8 MiB of `ls-tree` output; a commit to 1 MiB; scanned history to 100,000
commits, a cumulative 512 MiB validation budget, and a shared 60-second
validation deadline across each read/publish request and its Git/history
subcalls. Each Git command also has its own 60-second maximum. The trusted
host CAS capability must bound its own external mutation call. These are
validation bounds *after fetching*, not a pack
size/quota guarantee. Fetching arbitrary hostile packs into the product object
database is not approved by this staged module alone. Runtime activation
requires a separately owned trusted, resource-bounded metadata transport for
both writer and clean-clone reader. Neither #194's current checkpoint
acceptance criteria nor #216's evaluation contract explicitly owns fetch pack
quota/isolation; propose a dedicated transport-capability Issue under #189
before activation. Do not fetch arbitrary URLs/objects named by payload data.

## 7. Checkpoint handoff and Arca reads

The metadata writer returns success only after the post-push remote reachability
check in §5.6. A caller recovering a persisted candidate uses `confirm` with
that exact commit and required `(kind, id, task, subject)` identities; the
latest tip is never substituted. Only then may #194 post a checkpoint. That
checkpoint binds, at minimum, the exact:

* product subject Git OID;
* Task-view snapshot object ID; and
* metadata commit Git OID `C`.

The checkpoint must store the exact full OIDs/IDs returned by the writer. It
must not store a branch name or resolve the IDs again through a later tip. The
checkpoint format and posting are owned by #194; #140 owns operation-local
intent and interrupted publication recovery. This protocol does not introduce
another journal or claim an atomic transaction between the remote ref and PR.

Arca reads the exact checkpoint-bound commit and object IDs, not whatever
objects happen to be newest at read time. From a clean clone, Arca explicitly
fetches `refs/agentcore/metadata`, verifies that the checkpoint's exact metadata
commit is reachable from the fetched ref, loads each requested object from that
commit's tree, and verifies the path, digest, envelope, Task, repository, and
subject bindings. If the pinned commit is unavailable or unreachable, Arca
fails closed; it does not silently substitute the current metadata tip or
another object with similar payload.

## 8. Retry and interruption matrix

The rules below are metadata-specific outcomes, layered on #140's operation-local
intent and #194's checkpoint contract. They do not claim cross-filesystem atomicity
or stronger hard-crash durability than #140 defines.

| Interruption/observation | Required recovery |
| --- | --- |
| Before push; candidate metadata commit is not remotely reachable | Do not checkpoint. Re-fetch. Retry from the same observed tip only if it is still current; otherwise apply the compatible-append/CAS rules in §5. |
| Push reports rejection because the ref advanced | Re-fetch and classify the intervening append. Reconstruct on the new tip only if compatible; same-Task pointer conflict fails closed. |
| Push result is ambiguous, including process interruption before acknowledgement | Re-fetch before any retry. If the exact candidate commit is reachable, validate its tree and resume the same #140 checkpoint handoff using that exact commit SHA. Do not create a duplicate metadata commit. If it is not reachable, reconcile against the latest tip under §5; an equal pointer value without the journal-bound commit is not proof of completion. |
| Push succeeded, but the process stopped before checkpoint advancement | Re-fetch and prove the exact pushed commit is reachable. If so, resume only the checkpoint for the same subject, task-view ID, and commit SHA. Do not roll back or republish the metadata commit. |
| Checkpoint write failed after remote reachability was proved | Keep the immutable remote metadata commit. Retry the same #140 checkpoint operation with the same exact IDs; never advance to an inferred newer metadata tip. |
| Exact candidate commit is no longer reachable (for example, the remote ref was externally rewound) | Do not checkpoint, force-push, or reconstruct the same operation as success. Return failure with the expected commit and observed remote tip; require #140 recovery policy. |
| Same Task pointer now names a different ID | Stop with CAS conflict. No automatic reread-and-overwrite retry. |
| Remote is unavailable or reachability cannot be checked | Do not report success and do not advance a checkpoint. Preserve retry evidence under #140. |

An exact candidate commit may remain reachable as an ancestor after later
compatible metadata commits. The checkpoint still names that exact candidate
commit, not the later tip. Metadata objects and commits already published are
never rewritten to make a retry appear successful.

## 9. Cleanup and retention

Task cleanup under #198 may remove the Task worktree, Task branch, and
Task-local disposable state only within its own authorized boundary. It must
not delete `refs/agentcore/metadata`, rewrite its history, or delete metadata
objects reachable from that ref. The metadata ref is independent of the
Task-local worktree lifecycle. This protocol defines append-only reachable
objects and no object-pruning operation; any future retention or archival policy
requires an explicit compatible protocol change.

## 10. Conformance tests required before implementation integration

The implementation owning this protocol must add focused tests for at least
the following cases. These are test requirements, not tests claimed as run by
this documentation change.

1. Envelope acceptance/rejection: all four kinds; exact required fields;
   wrong/missing/extra fields; bad types; invalid repository/task identity;
   incomplete or malformed subject OIDs; non-object payload; duplicate JSON
   keys; invalid UTF-8; malformed JSON.
2. Canonical bytes and IDs: shared byte-for-byte vectors for each kind,
   including Unicode, escaping, numeric values accepted by the chosen profile,
   exact domain-prefix LF, no file trailer, digest, shard, and object path.
3. Path and tree hardening: invalid kind/ID/shard/task components, traversal,
   unknown paths, symlink/executable/gitlink entries, object corruption, and
   same-path non-identical immutable content all fail before publication.
4. Pointer invariants: exact pointer bytes; target kind/repository/task checks;
   expected-prior-ID CAS success; stale expected ID failure; expected-absent
   creation and parentless genesis; exact candidate confirmation on retry;
   no `current` pointer or other mutable path.
5. Remote concurrency: parent equals observed remote tip; atomic direct-ref
   expected-old CAS plus fast-forward-only candidate; compatible independent Task append; immutable append
   union; same-Task concurrent CAS conflict; unknown/deletion/modification race
   rejection; no force push.
6. Product isolation: before/after assertions that product tree, product
   branches/tags, `HEAD`, worktree, and caller index are byte/ref-identical for
   success and failure paths; only the metadata namespace ref may be remotely
   updated.
7. Reachability and checkpoint: an acknowledged push is insufficient without
   a fresh remote reachability check; checkpoint is withheld on unavailable or
   unreachable commit; checkpoint records the exact subject, task-view ID, and
   metadata commit; retry after push-before-checkpoint uses the same commit.
8. Arca clean-clone behavior: explicit ref fetch; exact commit and IDs resolve
   from the pinned commit; absent/unreachable ID, stale latest pointer, or
   wrong subject fails closed.
9. Snapshot and lifecycle integration: snapshots refer only to a previous
   durable checkpoint; no new checkpoint is required; #198 cleanup leaves the
   metadata ref and reachable objects intact.
10. Runtime isolation: v3 VERSION and generated templates remain unchanged;
    no v3 runtime import/call path reaches this module. #159's version identity
    foundation is present; v3 runtime/generated-template isolation remains
    required until explicit v4 runtime/distribution activation.

## 11. Integration handoff and remaining gates

The codec, genesis/expected-absence behavior, transport CAS, history validation,
and exact-commit recovery primitive are implemented and tested in the staged
`components/agent-core-v4/` module. This is **not** the production v4 runtime.
Before activating publication, the owners must:

1. #191 binds canonical repository, Task and semantic caller authority. #217
   owns the exact metadata ref and mutation boundary: an approved host-side
   `direct_ref_cas` must bind that repository and remote to this one direct ref,
   compare exact old OID under the same atomic update, and never dereference a
   ref into a product ref. A callback name or boolean alone is **not** proof
   of this host guarantee. Until a vetted endpoint supplies it, the writer
   fails closed. Do not expose bare `publish` to arbitrary Task agents.
2. Have #140 persist each exact candidate commit and full operation identity
   before the push, including any newly reconstructed candidate after a race.
   `on_candidate` is a hook, not a durable journal by itself. On retry, use
   `confirm(exact_commit, required_objects=...)`; a pointer match or newest tip
   must never be substituted for that exact operation.
3. Have #144/#145/#191 validate their payload schemas and links (especially the
   Snapshot -> applicable Evidence/Contract graph). #194 posts the checkpoint
   only after exact remote confirmation and owns remote/PR identity validation.
   Arca must have a verified way to fetch this custom ref from a clean clone.
4. A **dedicated bounded metadata transport capability** (new follow-up Issue
   proposed under #189) must bind the remote URL/repository and bound downloaded
   pack size, disk/object count, decompression and processing before objects
   enter the product store. #194 consumes it for writer collaboration and #216
   for evaluator reads, but neither current Issue contains this acceptance
   criterion. #217 bounds already-fetched objects and histories only; it must
   not misrepresent Git subprocess timeouts as fetch-pack quotas or acquire a
   speculative network stack. #140 handles interrupted operation reconciliation.
5. #159's version contract is now merged and present. Integrate the v4 module
   only after the v4 runtime and distribution work are ready. Keep VERSION 3
   and generated templates unchanged here. Confirm #198 cleanup preserves the
   metadata ref/history.
