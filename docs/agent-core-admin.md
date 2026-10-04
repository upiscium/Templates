# Agent Core Admin source-side library

`tools/agent_core_admin.py` is a development-only, source-side Python library for
planning and applying narrowly inventoried Agent Core file operations. It is not a
trusted launcher, production authorization authority, user-facing command, or part
of the normal v4 runtime. Its implementation can perform filesystem mutation when
called, so its current availability must not be mistaken for approval to use it on
a production consumer.

**Production mutation is not authorized by this library.** That boundary remains
closed until all of the independently trusted inputs are supplied and reviewed:

| Dependency | Required evidence | Current boundary |
| --- | --- | --- |
| #146 | An independently trusted Admin launcher that establishes the execution and source trust boundary. | Not supplied here. A mutable source checkout is not a substitute. |
| #159 | The immutable Agent Core 1.0.0 payload, bound to its approved full source revision and exact manifest digest. | No released payload, digest, or approved revision is asserted by this document. |
| #217 | The authoritative Task metadata binding schema and protocol. | Still undefined; this library does not invent either. |
| #194 | The independently trusted collaboration bindings connecting the active Task, Issue, and PR. | Not supplied; do not infer them from branch names, PR text, or numeric references. |

The library cannot independently provide or verify these missing authorities.
Until they exist together, do not use `apply` or `retry` to mutate a production
checkout. In particular, a caller-supplied `lambda True` is only a permissive test
fixture: it is not production authorization, provenance, or evidence of trusted
Task metadata. Unit tests that use such callbacks and synthetic payload fixtures
exercise the engine in isolated test repositories only; they do not prove or supply
the #146, #159, #217, or #194 dependencies. #220 publication is outside this
change and remains untouched.

## Trust and target identity

The adjacent `tools/agent_core_admin_identity.py` module is a read-only identity
and cutover-preflight interface, not a launcher or authorization provider. It
observes one exact real Git worktree and checks the caller-supplied target root,
canonical GitHub `owner/repository`, named branch, and full commit `HEAD` against
the actual worktree and its single canonical `origin`. Detached heads, identity
mismatches, ambiguous origins, and unsafe or ambiguous worktree registrations fail
closed. The Admin engine re-observes that identity at operation boundaries; it
does not choose the target branch, repository, or revision for the caller.

For cutover preflight, the identity module makes only read-only GitHub API
queries. It verifies the exact open Issue and exact open same-repository Draft PR
whose expected base, branch and head SHA match the request. These GitHub facts
alone do not establish that the Issue and PR belong to the active Task: the
`task_binding` callback must supply that separate #194 authority, and
`metadata_binding` must independently supply #217 readiness. Neither callback
is implemented or inferred from PR text, closing references, or branch names.

Every request must bind all target identity inputs explicitly: the exact worktree
root, owner/repository, branch, and full current `HEAD`. The library does not
discover a “best” checkout, follow a moving branch as approval, or treat a matching
repository name as sufficient identity. A changed branch, head, origin, or
worktree identity invalidates the request and requires a fresh, independently
authorized decision.

## Immutable payload contract

Install, replace, and cutover requests require an externally approved payload
directory, `expected_manifest_sha256`, and `expected_source_revision`. These are
independent exact inputs, not values to infer from a tag, local version file, or
mutable checkout:

* `manifest.json` is a closed JSON object with exactly `version`,
  `source_revision`, and `files`. The manifest itself must be a regular file with
  mode `0644`; duplicate JSON keys are rejected. `version` is exactly `1.0.0`;
  `source_revision` is the approved full lowercase Git object ID (40 or 64
  hexadecimal characters).
* `expected_manifest_sha256` is the approved 64-character lowercase SHA-256 of
  the exact manifest bytes, including any final newline. The digest supplied by
  the caller must match those bytes. `expected_source_revision` must be a full
  lowercase Git object ID and must exactly equal the manifest's
  `source_revision`.
* `files` is a non-empty explicit list. Every entry has exactly `path`, `sha256`,
  and `mode`: a normalized relative managed path, a 64-character lowercase
  SHA-256 of that file's bytes, and integer mode `0644` or `0755`. The bytes and
  mode on disk must match. The payload directory may contain no unlisted or
  missing files or directories, symlinks, special files, or path collisions.
* The payload must list `.automation/VERSION` with exactly the bytes `1.0.0` and
  one trailing LF, mode `0644`.

The development engine caps each payload/managed file at 8 MiB and an operation
inventory at 256 paths / 32 MiB. A larger or ambiguous source/target fails
closed before claiming mutation readiness; increasing these limits requires an
explicitly reviewed Admin capability change.
Git index snapshots are also capped at 32 MiB, including every re-read under
writer guards; larger indexes fail closed before local mutation.

The managed surface is the root files `AGENTS.md`, `Justfile`, and
`opencode.json`, plus files under `.automation/` and `.opencode/`. The protected
identity files `.automation/ADAPTER`, `.automation/INIT.fragment.md`, and
`.automation/adoption.toml` are outside the Admin-owned surface and must not be
claimed by the payload. This format describes what the engine validates; it does
not itself make a payload genuine, immutable, or approved. No mock payload or
unverified digest/revision in development fixtures is release evidence.

## Operations and ownership

The Python API provides read-only `inspect` (also exposed as the `plan` alias),
mutating `apply`, and operation-ID-bound `retry`. The operation is explicitly one of
`install`, `replace`, `uninstall`, or `cutover`.

* **Install** requires an empty Agent Core managed-file inventory and no existing
  installed identity (or a previously recorded uninstalled identity). It refuses
  `main`: the caller must establish an exact non-default bootstrap branch first.
  `inspect` returns a `publicationHandoff` bound to the operation ID, original
  branch/HEAD and changed paths. #194 must provide the eventual guarded commit,
  push and Draft PR path; Admin does not implement or silently skip that handoff.
* **Replace** and **uninstall** require the Git-private installed identity from a
  prior Admin operation and an exact match between that recorded inventory and
  the current managed files. A mismatch is not guessed around or overwritten.
* **Cutover** is one-way. It refuses an existing Admin installation identity and
  requires a complete caller-supplied legacy path inventory, with each path's
  SHA-256 and mode, including `.automation/VERSION`. The inventory must exactly
  match every currently observed Agent Core managed file; the old version file
  must contain exactly `1`, `2`, or `3` followed by LF. Only that exact prior
  inventory can be replaced by the approved payload. Protected adapter/identity
  files and unrelated product files are not part of that inventory.

### Three-phase mutation and concurrency boundary

An Admin mutation must keep external evidence work outside the local writer
fence. The required sequence is:

1. **Phase 1 — external preflight, with no locks.** Observe the requested worktree
   identity and, when applicable, live Issue/PR facts; evaluate the supplied
   authorization and Task/metadata binding callbacks. No Admin flock or Git
   writer guard is held. These observations are preflight evidence, not a lease:
   remote facts can change before or during the local operation.
2. **Phase 2 — bounded local operation.** Acquire the worktree-specific Admin
   flock exclusively and nonblocking; if it is occupied, report `BUSY` rather
   than wait. Then reserve the per-worktree Git writer names in this order:
   `index.lock`, then `HEAD.lock`. A collision is also `BUSY`. While these guards
   are held, run only deterministic, request-bounded local work: the current
   operation's exact intent/receipt is checked against the unlocked history
   snapshot rather than rescanning all historical records. No GitHub or
   other network access, arbitrary callbacks, or subprocesses. Revalidate local
   identity, index and managed-path state. For every changed leaf, accept only
   its exact recorded preimage (bytes and mode), or absence when creation is
   expected; verify the exact postimage (or absence for removal). Unknown or
   changed paths stop the operation without overwriting them. Persist and
   validate the exact local completion receipt before releasing the guards.
3. **Phase 3 — post-operation observation, with no locks.** Release all writer
   guards, then re-observe identity, remote Issue/PR facts, and the applicable
   callbacks. If those facts changed or are no longer available, report that
   alongside the exact local completion (operation ID, changed paths, and
   receipt); do not roll back completed local work and do not perform an
   external write. Any required interrupted-operation journaling or recovery
   belongs to the operation-specific #140 path, not an Admin rollback.

Git writer guards are released in reverse acquisition order: `HEAD.lock`,
`index.lock`, then the Admin flock. The Admin lock serializes cooperative Admin
operations, and the Git lock names coordinate with Git writers that honor the
protocol. This is not a general filesystem transaction: raw filesystem writers
that ignore these locks are outside the supported concurrency model, and this
library does not claim to protect against their races.

The source-side engine separates these phases and validates local `HEAD` and
loose/packed branch refs directly during Phase 2 rather than calling Git there.
This behavior still requires exact-subject verification and independent review;
it is not production-enabled. Production mutation remains blocked by the missing
trusted inputs listed above.

The local path operations are limited to each managed leaf's exact known
preimage and postimage. Creation is only for an absent leaf and uses the Linux
fd-anchored `renameat2(RENAME_NOREPLACE)` primitive; if unavailable, creation
fails closed. An unexpected or changed managed path is left untouched and
stops the operation. The library does not move unknown content aside or
automatically preserve/adopt unknown managed paths. It does not recursively
delete directories.

The legacy inventory is an ownership assertion, not a discovery-based permission
to delete files. Paths omitted from it cannot be silently treated as approved
legacy Agent Core state. Unexpected or changed paths stop the operation before
they are replaced or removed.

For every operation, `inspect` validates the request, target identity, payload
where applicable, inventory, and pending-operation state without writing target
files or creating Admin receipts. It reports the exact target, an operation ID,
and per-path actions, with `readOnly: true`. It is a planning/preflight result,
not authorization and not a lock guaranteeing that later facts remain unchanged.
If the exact operation intent has only a recoverable private temporary, `inspect`
reports `RETRY` with that same operation ID; mutation still requires the explicit
ID-bound `retry` API rather than silently starting a new operation.

Mutation requires a caller-provided `caller_authorizer` callback that returns
exactly `True`; errors, false values, and truthy non-`True` values fail closed.
Under the required phase boundary, authorization callbacks run only during
unlocked Phase 1 and Phase 3 observations, never while writer guards are held.
The engine cannot determine whether a callback is independent, trustworthy,
bound to #217/#194, or authorized for production. That decision belongs to the
absent trusted launcher and collaboration protocol. A local callback or test
lambda must never be represented as satisfying that trust boundary.

## Side effects, receipts, and dirty product work

The engine limits target-file effects to the union of the explicitly supplied
payload paths and the prior installed or legacy inventory. For absent-file
creation it requires Linux fd-anchored `renameat2(RENAME_NOREPLACE)` and fails
closed if that primitive is unavailable. Replacement and unlink require the
exact known preimage under the cooperative fence above; removals are bounded to
inventoried leaf files, never recursive directory deletion. For each changed
path it verifies the exact before/after content and mode, including expected
absence for creation or removal. It rejects
symlinks, special files, unsafe mount boundaries, path collisions, unexpected
managed files, and staged index paths that overlap an operation change. It does
not clean a checkout or discard unrelated staged, unstaged, or untracked product
work. Unrelated product paths are outside the operation's mutation scope; an
unknown managed path is not automatically moved aside or adopted. A conflict
on an operation-owned path is a stop condition, not a reason to overwrite the
user's change.

The engine never stages files, creates commits, changes Git refs, pushes, creates
or updates a PR, or publishes a release. Apply bookkeeping is kept under the
target worktree's Git-private Admin directory (the worktree-specific Git
directory), rather than in product files: a private format-1 installed-identity
record, operation-specific intents, completion receipts, and a lock. Git-private
records are limited to 32 MiB each. Unlocked history reconstruction also stops
at 8,192 private entries or 64 MiB of total record bytes; exceeding that bound
requires explicit Admin retention/reconciliation, never automatic deletion or
a longer unbounded scan under Git writer locks.
Completion evidence must be durably published before the writer guards are
released: write and fsync the private record, atomically publish it, fsync its
containing directory, then reread and validate the exact receipt. There is no
tombstone. The removed format-2 tombstone experiment is not imported as a
compatibility authority; those experimental records fail closed without silent
migration. These records are recovery evidence, not Git commits or proof of
production authorization. No publication workflow, publication command, or
#220 behavior is added or changed here.

Repository-wide ruleset/policy setup is a separate high-authority Admin
capability, not a normal Task prerequisite and not an implicit side effect of
file installation. Release/distribution qualification belongs to #141; neither
is supplied by this file-operation library. They must not be inferred from a
successful payload installation or used to block unrelated healthy Tasks.

An operation ID binds the exact request and prior installed identity. `retry` requires
`expected_operation_id` from the read-only plan or the persisted intent; it cannot
initiate an operation without a matching persisted intent or its exact recoverable
intent temporary. A matching pending request can be
retried only against its recorded exact preimage or postimage; unrelated
incomplete work must be resolved first. Retry only resumes the same exact
operation, payload digest/revision, target identity, inventory, and (for cutover)
Issue/PR binding. It is not a general rollback or a way to adopt a different
request. A path is recoverable only when it is the exact recorded preimage or
the exact postimage for this same operation; a postimage from any other
operation is not accepted. If a managed path is in a third state, an
identity changes, a receipt is ambiguous, a temporary is not an exact
recoverable prefix, or ownership cannot be proved, stop without manual
receipt/temp cleanup or broad restoration. Preserve
the current files and private evidence and escalate to a human maintainer/Admin
for operation-specific recovery. Do not guess a repair or rerun a different
operation over an unresolved intent.

## In-flight Task cutover is gated

The engine treats any branch other than `main`, or a worktree containing Task State,
as an in-flight Task or ambiguity. A cutover on such a target must include both
the exact Issue and PR numbers, `expected_base`, and **two distinct** externally
supplied callbacks: `task_binding` (#194) and `metadata_binding` (#217). During
Phase 1, the identity boundary checks live Issue/PR facts and requires both
callbacks to return exactly `True` for the observed target and those numbers.
They are not called while Phase 2 writer guards are held. Phase 3 re-observes the
remote facts and callbacks after releasing every guard. A changed or unavailable
fact is reported together with the exact local completion; it does not trigger
rollback or an external write. If a binding is missing, unavailable, false, or
ambiguous at preflight, cutover stops before local mutation; a branch number,
PR-body mention, or permissive lambda is not a substitute.

The #217 metadata schema/protocol and #194 collaboration binding are not defined
by this source-side library. Consequently it cannot perform an authoritative
in-flight Task cutover today. Do not invent a callback implementation to make
preflight pass. The missing #146 trusted launcher, immutable #159 payload, #217
metadata protocol, and #194 collaboration bindings are required inputs before
any production mutation can be considered; until then this is development-only
code, not a normal v4 runtime capability.
