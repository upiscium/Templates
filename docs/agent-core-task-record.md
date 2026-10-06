# Minimal v4 Task Record and immutable Issue contract

**Status:** zero-base #191 capability staged for AgentCore 1.0.0, atop the
merged [#217 metadata substrate](agent-core-metadata-ref.md). This is not
activation of the VERSION 3 runtime or generated templates. It does not
implement Evidence (#145), Task View (#144), checkpoint/PR (#194), orchestration
(#196), reconciliation intent (#140), evaluation (#216), or cleanup/GC (#198).
The independent [release identities](agent-core-versioning.md) remain unchanged.

## Authority and closed schemas

`components/agent-core-v4/task_record.py` owns these two payload schemas.
Both use #217's closed envelope, canonical UTF-8 JSON, domain-separated SHA-256
object IDs, and immutable `objects/<kind>/<first-two>/<id>.json` paths. Payload
`schema_version: 1` is a schema number, not a public release version.

For these two kinds only, envelope `subject` **means the exact creation base
revision**, not current HEAD or an Evidence subject. It is a full lowercase
Git commit OID, validated against the repository's object format and commit
existence at creation. It remains historical provenance after merges, rebases,
or default-branch advancement; mutations never test current HEAD or ancestry.
The envelope's exact-case `repository` and canonical decimal `task` bind both
the Task identity and the GitHub Issue source identity. Identity is explicitly
supplied at the trusted caller boundary, not inferred from checkout paths or
remote/PR metadata.

An Issue contract snapshot has exactly this envelope and payload:

```json
{
  "schema_version": 1,
  "kind": "contract",
  "repository": "upiscium/Templates",
  "task": "191",
  "subject": "3a463dde437b00be268ffc90d6317b3a12634082",
  "payload": {
    "schema_version": 1,
    "title": "Define the minimal v4 Task Record and immutable contract authority",
    "body": "<exact authoritative Issue requirement body>"
  }
}
```

This initial schema supports GitHub Issue-backed Tasks only. The trusted caller
must validate/fetch the exact repository and Issue, then supply its exact
requirement-bearing title (nonblank string) and body (string; empty permitted).
It must not substitute a cached different Issue, silently refresh authority,
or publish credentials/secrets. This module does not fetch GitHub or claim to
authenticate arbitrary supplied strings. Comments, labels, assignees, Issue
state, timestamps, URLs, PR facts, and other incidental live metadata are not
snapshot fields. Human requirements referencing other material remain part of
the captured text; this schema does not independently fetch referenced sources.

A Task Record has exactly this envelope and payload:

```json
{
  "schema_version": 1,
  "kind": "task-record",
  "repository": "upiscium/Templates",
  "task": "191",
  "subject": "3a463dde437b00be268ffc90d6317b3a12634082",
  "payload": {
    "schema_version": 1,
    "branch_ref": "refs/heads/feat/191-agentcore-task-record",
    "contract_id": "<exact 64-lowercase-hex Contract object ID>"
  }
}
```

Examples are pretty-printed for readability; stored bytes are compact, key-sorted
JSON without a trailing newline as specified by #217. No extra or missing fields
are accepted. Full `refs/heads/...` refs must satisfy Git's non-normalizing ref
grammar. There is no current HEAD, worktree path, PR, checkpoint, verification,
review, workflow phase, Work Unit, provider/model, or active/blocked field.
Git owns current ref/HEAD/worktree facts; GitHub owns remote branch/PR facts.

The only optional Record field is `payload.disposition`, with one of exactly:

```json
{"kind":"cancelled"}
```

```json
{"kind":"superseded","replacement":{"repository":"upiscium/Templates","task":"222"}}
```

Replacement uses an explicit repository/Task identity (cross-repository
replacement is possible without rebinding this Record). Self-replacement,
missing/extra fields, null, `active`, generic `blocked`, and every other kind
are rejected. There is no clear-disposition operation. An explicit Human
decision may replace one supported disposition with another, guarded by the
exact prior Record ID; no workflow transition engine is implied. Absence means
only **no Human terminal disposition recorded**, never active. PR closure,
branch deletion, failures, conflicts, inactivity, and missing worktrees are
not inputs and cannot establish a disposition.

## Trusted host boundary and minimal mutation

`TaskRecords(store, authorize=host_capability)` is constructed by a trusted
host, not from Task-controlled configuration. The callback is a semantic
authorization capability, **not** a JSON credential or self-asserted Human flag.
The implementation issues no authority. Arbitrary Python code that can replace
the callback or invoke the underlying generic writer is outside this trust
boundary; do not expose either construction or generic `publish` to agents.

Each call receives a frozen `AuthorityRequest` containing only:

```text
operation: create | disposition | reauthorize
repository, task, branch_ref, base_revision
expected_record_id: exact prior ID, or None for creation
proposed_record_id, contract_id: exact content IDs
```

The proposed Record digest binds the complete proposed disposition, including
replacement, and Contract identity. The trusted host must independently bind
the supplied decision/source bytes to these exact IDs (using the pure encoders)
and validate caller authority for this operation and identity. Creation requires
validated Issue/Task creation authority. Disposition and reauthorization
**additionally require explicit Human-owned intent supplied by that caller
boundary**; ref access, a branch name, or possession of old objects is not
sufficient. Only literal `True` grants authority; missing, exceptional, or other
results fail closed. Canonical proposed bytes are captured before the callback,
so caller-owned dictionaries cannot alter an already-authorized proposal.

The API is intentionally bounded:

* `create`: supplied repository is fixed by the store, Task/full branch/exact
  base and title/body are validated; append Contract and Record and create
  `tasks/<task>/record` only with expected absence (`None`). It never starts a
  workflow phase or disposition.
* `read`: require an exact reachable metadata commit and expected Task, branch,
  and creation base. Resolve the Record pointer and Contract from that **same
  commit**, validating closed schemas, digests, kind, repository, Task and base.
* `set_disposition`: validate exact expected prior Record and bound repository,
  branch and base; preserve identity/Contract; authorize Human decision; append
  new Record and CAS the pointer. Unchanged disposition is rejected as no new
  decision.
* `reauthorize`: validate the same prior binding; snapshot newly supplied Issue
  requirements; authorize Human reauthorization; append a new Contract and
  Record, preserving base/branch/disposition, and CAS the pointer. Byte-identical
  requirements are rejected as not a new snapshot.

Every proposed Record includes a validated, identity-matching Contract in the
publication request. Disposition mutations also revalidate the prior Contract
graph before using it. Only #217 performs persistence: its pointer/ref CAS and
compatible different-Task append reconciliation are reused, not reimplemented.
Stale same-Task CAS never adopts a refreshed expected ID. No secondary metadata
ref, product-branch files, filesystem-private canonical root, or legacy runtime
import is introduced. Legacy cutover belongs exclusively to #142 Admin.

`Publication` returns exact `metadata_commit`, `record_id`, and `contract_id`.
An optional `on_candidate` is passed through to #217 for future #140-owned
intent recording, not a new journal. On uncertain publication, the future caller
must recover its persisted exact candidate with substrate `confirm`, never
infer success from pointer equality or a later tip. No-op object reuse is
byte-identical and append-only; a same-pointer creation retry is still a CAS
conflict, not proof of this operation's success.

## Isolation, retention, and remaining gates

The only canonical ref is `refs/agentcore/metadata`; pointer changes append
metadata commits independently of product history. Old Contracts and Records
remain in later trees and reachable history after reauthorization/disposition.
There is no removal or GC API here. Local fixture tests demonstrate retained
authority after Task branch deletion and reconstruction in another clone;
this is a substrate invariant, not implementation of #198 cleanup policy.

The #217 host-side direct-ref CAS and bounded-transport activation gates remain
mandatory. This staged module is **not** a live GitHub publication endpoint.
No real product metadata refs are used by tests, only temporary local fixtures.
VERSION 3, current distribution payload, release identity, and generated
templates remain unchanged pending separately authorized v4 activation.

Focused verification: `python3 -m unittest discover -s tests -p 'test_task_record_v4.py' -v`.
