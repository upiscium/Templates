# Minimal v4 Evidence envelope

**Status:** zero-base #145 Evidence capability staged on the #217 metadata
substrate. This is a durable, immutable envelope for capability-owned facts,
not a workflow engine or a general validity authority. It does not implement
Task View (#144), prerequisites (#192), the Verification capability (#193),
publication/integration, or legacy compatibility/cutover (#142). Staging this
module does not activate the v4 runtime, generated templates, or release.

## Exact shared binding and closed Evidence payload

`components/agent-core-v4/evidence.py` exposes:

```python
encode_evidence(repository, task, subject, envelope) -> (evidence_id, data)
decode_evidence(data, *, evidence_id, repository, task, subject) -> object_dict
Evidence(store).read(commit, evidence_id, *, task, subject) -> object_dict
```

`encode_evidence` and `decode_evidence` are pure codec operations. The canonical
shared envelope is the #217 metadata-v1 envelope: exact `kind: "evidence"`,
repository, Task, and subject bindings, plus a closed object payload. Object
bytes are compact key-sorted UTF-8 JSON without a trailing newline. The
content ID is lowercase SHA-256 over
`agentcore-metadata-object/v1\n` followed by those bytes; the path is
`objects/evidence/<first-two-id-hex>/<id>.json`.

The Evidence-specific schema is nested in the shared #217 envelope's `payload`
member. The following shows the whole object shape (with `supersedes` omitted):

```json
{
  "schema_version": 1,
  "kind": "evidence",
  "repository": "acme/widgets",
  "task": "145",
  "subject": "0123456789abcdef0123456789abcdef01234567",
  "payload": {
    "schema_version": 1,
    "kind": "capability-owned nonblank kind",
    "producer": {"name": "descriptive producer identity"},
    "created_at": "2026-10-06T12:34:56Z",
    "payload": {"capability-owned": "opaque fact"}
  }
}
```

Inside that shared `payload`, the Evidence schema has exactly the shown
required fields and one optional field, `supersedes`:

```json
{
  "schema_version": 1,
  "kind": "capability-owned nonblank kind",
  "producer": {"name": "descriptive producer identity"},
  "created_at": "2026-10-06T12:34:56Z",
  "payload": {"capability-owned": "opaque fact"},
  "supersedes": "<exact 64-character lowercase Evidence ID>"
}
```

`schema_version` must be the integer `1` (not `true`, a float, or a string).
`kind` must be a nonblank string but its vocabulary belongs to the capability
that owns the Evidence. `producer` and `payload` must be JSON objects.
`created_at` is exactly a valid UTC calendar timestamp of the form
`YYYY-MM-DDTHH:MM:SSZ`; fractions and timezone offsets are not accepted.
Evidence's custom payload is closed at its top level. Its nested `payload` is
opaque to this module and remains the capability's schema and semantics.

The shared codec still applies its JSON limits and primitive types (including
no floating-point JSON values). The Evidence module does not assign a generic
meaning to result/status fields, interpret `PASS`/`FAIL`, or decide whether a
Work Unit, task, producer, run, check, or capability is valid. A producer object
is descriptive metadata, not a lookup request or proof that a runtime,
history, source, or producer exists.

## Subject, producer, and immutability limits

The shared `repository`, `task`, and `subject` are exact caller-supplied
bindings. Repository identity uses the #217 owner/name grammar; Task uses its
canonical positive-decimal string. Subject is the exact full lowercase
40- or 64-hex Git object spelling. The codec does **not** normalize abbreviated
or symbolic revisions, capture a dirty tree, establish that the object exists
or is a commit, or authorize that it is the correct subject. A trusted caller
must select the intended immutable subject. Any additional capability-specific
subject identity must be recorded and checked by that capability in the opaque
payload before it interprets a fact.

Encoding does not inspect Git history or producer state. Re-encoding identical
bindings and payload bytes gives the same content ID; changing any byte-bound
field yields another object identity. Persisted objects are immutable and
append-only under #217. Python dictionaries returned by decoding are ordinary
values; immutability describes the canonical persisted object, not a frozen
in-memory mapping.

`Evidence.read` reads the requested ID from one exact metadata commit and
requires exact repository, Task, subject, kind, canonical bytes, and content
identity. It does not choose a “latest” object or consult a timestamp/current
pointer. The caller supplies the commit and ID to read. Historical Evidence
remains addressable in later valid metadata trees; a newer timestamp or newly
written object does not supersede it implicitly.

## Explicit `supersedes` is reference integrity, not replacement authority

Without `supersedes`, no link is inferred. If present, the value must be an
exact 64-character lowercase content ID. `Evidence.read` resolves that ID as an
Evidence-kind object from the **same pinned metadata commit**, validates its
Evidence payload schema, then follows its own explicit link. Missing targets,
wrong object kinds, malformed Evidence targets, malformed links, cycles, and
chains beyond 64 links are rejected. The complete traversal uses #217's
`validation_scope` to share its 60-second validation deadline with all linked
reads and their Git subprocesses; nested scopes cannot extend it. Each lookup
revalidates reachability/history (there is no persistent or cross-call cache),
so a costly graph may fail closed on the resource limit rather than resolve.

The reader checks link identity and integrity only. It does not require a
target to have the same Task, subject, or capability-owned payload `kind` as
the object being read; these differences create no Kernel-owned replacement,
applicability, or validity semantics. In particular, `supersedes` is not a
Task Record disposition, a mutable current pointer, or a claim that one fact
replaces another in a domain. A consumer that needs such semantics must own and
validate them independently.

## Persistence and trust boundary

There is no Evidence writer facade, authorization callback, publication
adapter, enumeration API, current/latest selector, Work Unit validator,
prerequisite evaluator, or Verification capability here. `encode_evidence`
only creates canonical bytes; it grants no permission to publish them. The
underlying `MetadataStore.publish` is the generic #217 writer and must remain
available only to a trusted host/caller that applies #191 Task/repository
authority and its capability-owned publication policy. It must not be exposed directly to agents as an Evidence
capability.

The generic #217 substrate validates shared metadata tree/object integrity.
Evidence-specific schema and supersession-graph validation happens when
`Evidence.read` traverses an object. A structurally valid generic metadata
object with an invalid Evidence payload is not valid Evidence to this reader.
No secondary ref, product-repository file, or product-history mutation is
introduced by Evidence; the existing #217 metadata ref is the persistence
plane. This staged scope makes no real-network, live-host, or runtime-activation
claim.

Task View/Snapshot set selection and Verification's pre/post capture, executed
check schema, and successful-result interpretation are intentionally not
implemented here. In particular a read matching a product HEAD is not proof
that verification ran, that a dirty checkout matches that HEAD, or that an
unexecuted check succeeded. Such claims require the capability's own subject
capture/revalidation and payload policy. Secret rejection/redaction likewise
belongs to the trusted producer/capability before durable storage; an opaque
codec cannot certify arbitrary payloads as secret-free. All #217 direct-ref CAS
and bounded transport activation gates remain unchanged.

Focused local-fixture verification command:

```sh
python3 -m unittest discover -s tests -p 'test_evidence_v4.py' -v
```

The tests reuse the existing #217 local fixture and exercise exact reads,
canonical IDs, explicit reference integrity, clone-independent reads, and
product repository/index isolation. They do not publish to a real repository,
create a product commit as Evidence persistence, push a branch, create a PR,
or create a checkpoint.
