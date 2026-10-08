# Bounded autonomous v4 cleanup proof facade

**Status:** staged #198 source capability in `components/agent-core-v4/`. It
composes #191 Task Record/Contract authority, #144 factual Task View and #192
operation-local prerequisite diagnosis. It does not activate the v4 runtime,
change VERSION 3, generated templates, installed permissions or adapters, and
does not provide a production deletion backend.

```text
canonical Task authority + actual local Git facts + exact retained Task HEAD
  + complete read-only filesystem inventory + typed owner proofs
  + current operation prerequisites + explicit current host authorization
  + durable exact cleanup intent
  -> separately qualified worktree / branch / ephemeral effect
  -> independently observed postconditions
```

The capability is intentionally narrow: cleanup is not Task lifecycle policy,
not a disposition-based discard rule, not generic path deletion, and not a
transaction manager. [`cleanup_resources.py`](../components/agent-core-v4/cleanup_resources.py)
proves what is present. The [`task_cleanup.py`](../components/agent-core-v4/task_cleanup.py)
facade binds those observations to canonical Task/Git facts, retention evidence,
operation policy and trusted host capabilities. Neither a proof nor a successful
diagnosis by itself grants deletion authority.

## Facade boundary and public operations

[`cleanup_resources.py`](../components/agent-core-v4/cleanup_resources.py)
exposes frozen `TaskRootSpec`, `PathIdentity`,
`NodeObservation` and `WorktreeInventory`, plus
`TaskResourceInspector(spec)` with the read-only operations:

```text
inventory() -> WorktreeInventory
revalidate(expected_inventory) -> WorktreeInventory
relative_nodes(inventory, exact_relative_path) -> tuple[NodeObservation, ...]
is_absent(exact_relative_path) -> bool
root_absent() -> bool
```

The inspector proves a bounded current observation; it cannot establish Task
ownership, decide that content is disposable, or remove any entry. Its
`TaskRootSpec` is an internal facade binding built only after checking current
Git worktree registration and the Task branch/HEAD. Constructing a spec directly
does not establish that binding.

[`task_cleanup.py`](../components/agent-core-v4/task_cleanup.py) exposes
`CoreTaskCleanup` with these bounded operations:

* `plan(task)` (also named `capture`) is read-only and returns the exact
  `CleanupPlan` candidate; it does not journal or delete.
* `cleanup(task)` prepares and durably records one intent, then attempts the
  worktree-removal step followed by a separate local-branch step.
* `recover(intent_reference)` loads only that exact typed host-journaled intent
  and retries only a still-eligible remaining step.
* `cleanup_ephemeral(task, OpaqueResourceReference)` resolves one registered
  Task-local disposable scope; the request supplies no path.

The trusted host assembles the facade with #192 prerequisite engines, an
explicit authorizer, a resource classifier/registrar, durable `record_intent`
and exact `load_intent` callbacks, and operation-specific deletion capabilities.
These are privileged host capabilities, not values to construct from agent
JSON. The agent-facing requests are limited to a canonical positive-decimal
Task ID, an opaque journal reference, or a typed opaque resource reference;
there are no caller-selected branch/path arguments or force flags.

## Exact Task and retained-history checks

The facade resolves the current #191 Record and Contract from the existing
metadata authority and checks their exact IDs, base revision and branch binding.
It reads the factual #144 Task View for the exact current Task HEAD and branch,
and requests a fresh observation of the Task's remote branch. #192 diagnosis is
performed for the specific cleanup operation; `PREREQUISITES_SATISFIED` is a
description of checks, not permission to execute. The separately installed host
authorizer must return literal `True` for the immutable request for each effect.

The supported Main is the original repository worktree: its actual `.git`
directory is the common Git directory, its checked-out symbolic ref is the
current local default, and that ref resolves to its actual HEAD. The Task must
be the unique native Git registration at exactly
`<managedRepoRoot>/.worktrees/<child>`, on its canonical Task branch at the
registered expected commit `H`. The linked-worktree administrative directory
must be exactly the corresponding private entry under
`<originalMainRoot>/.git/worktrees/<name>`, with a matching reciprocal `.git`
pointer and `gitdir`/`commondir` files. Unregistered paths, path-prefix matches,
other roots, arbitrary Git-admin directories and an alternate selected ref are
not accepted. Unsupported or ambiguous topology fails closed.

Retention is deliberately conservative and tied to the **actual local
default**; an approved-looking other ref is not a substitute:

* `H` must be present in the complete, actual local-default commit graph, and
  the Task's immutable base must be an ancestor of `H`. Shallow, grafted,
  replacement-ref, unavailable or over-limit history is not accepted as proof.
* If the authenticated retention reader observes the Task remote branch, its
  full OID must equal exactly `H`. A different OID is a refusal, not a request
  to delete a newer or older branch.
* If that remote branch is absent, the trusted reader must return exact
  same-repository merged PR facts: the actual PR head/ref is the Task branch at
  `H`, and the actual merge commit is `M`. The facade independently requires
  `H` to be an ancestor of `M` and `M` to be in the current actual local-default
  graph. Missing PR evidence, another PR/head/base, an unknown merge OID or
  unavailable graph proof refuses cleanup. A configured upstream or a cached
  tracking ref is not remote evidence.

These checks are repeated against the frozen intent before and after each
operation boundary. The local default HEAD and branch, original Main identity,
other refs, index, mutable/untracked state and repository fingerprints must
remain stable, except for the one exact Task branch ref being removed in its
own later step and its exact registered worktree entry removed in the preceding
step. Every other native worktree registration, root/admin identity and
pointer/backlink binding must remain unchanged, including dirty siblings.

Task disposition is neither required nor sufficient. The current #191 reader
accepts no disposition, `cancelled`, or `superseded`; those values are recorded
as facts and revalidated, but none is a data-disposal grant. All history,
inventory, ownership, operation-prerequisite, current-authorization and backend
checks still apply. No cancelled/superseded state authorizes discarding
unpublished or unknown data.

## Read-only inventory and data-preservation rules

The inspector is Linux/procfs-specific. It requires Linux mount IDs read from
`/proc/self/fdinfo`, descriptor-relative opens with `O_NOFOLLOW` and
`O_NOATIME`, and the required directory/open flags. It never falls back to
device IDs alone or to path-based recursive traversal when a proof is
unsupported. The managed root, repository, `.worktrees` parent and private Git
admin location are opened and identity-checked; the regular `.git` pointer and
the reciprocal private admin files are separately validated.

The inventory includes every entry below the Task root except its separately
validated root `.git` pointer. It is sorted, content-addressed, and binds path,
kind, identity, ownership, link count, size/timestamps and file SHA-256. Current
hard limits in `cleanup_resources.py` are: 4 KiB UTF-8 path, 8 MiB per file,
64 MiB total file bytes, depth 32, 10,000 nodes, 16 MiB encoded inventory and
4 KiB Git pointer. The spec also bounds repository identity to 512 characters,
canonical Task IDs to 128 decimal digits and branch refs to 1,024 characters.
Traversal below the managed repository root must stay on the expected mount.
Absolute shared ancestors are checked by stable directory/name/FD and mount
identity, not by unrelated content timestamps or link counts; actual Task
context and inventory checks retain their stricter comparisons.
Only owned regular files with exactly one hard link and directories are
accepted. Symlinks, hard-linked files, nested `.git`, special nodes, mount
crossings, changed entries and unsupported filesystems fail closed. The
tracked-tree validator also accepts only ordinary `100644`/`100755` blobs;
gitlinks/submodules and hidden index states are unsupported.

The orchestration compares checked-out tracked files against raw blob bytes at
`H` and executable mode, without applying conversion/textconv filters. The
index must equal that tree and the Task worktree must have no tracked edits,
conflicts, staged changes or submodule/gitlink changes. Unknown ordinary and
ignored files are both included. Empty directories, which Git's untracked-path
listing does not report, are also unknown inventory nodes unless they are
ancestors shared with retained tracked files. No ignored/untracked item or
empty directory is presumed disposable.

Every unknown node must be covered exactly by non-overlapping
`DisposableResourceProof` values returned by the trusted host classifier. Each
proof is bound to the repository, Task, branch, `H`, inventory ID, producer,
disposable kind, exact scope and full node manifest. Kinds are limited to
`ephemeral_file`, `ephemeral_dir` and `recovery_state`; a missing, guessed,
overlapping, stale or product-data-covering proof refuses the operation before
deletion. The classifier is re-run against the same current inventory at the
effect boundary and during recovery. Unpublished tracked changes and unknown
data without a valid proof remain in place and block whole-worktree cleanup.

For a separately owned child resource, `OpaqueResourceReference(owner, token)`
is resolved by the trusted registrar to an `OwnedResourceBinding` for the exact
Task/HEAD/inventory/scope/producer and complete node manifest. The API does not
accept a generic path. Only that exact classified disposable scope can be
removed; the surrounding Task worktree, branch and unrelated resources remain.

## Effect qualification, step ordering and recovery

Before the first effect, the host journal must durably acknowledge the exact
canonical `CleanupIntent` with a typed `RecordedCleanupIntent` containing the
same intent ID and intent. The intent binds Task/Record/Contract identity,
metadata tip, `H`, actual default ref/head, Main state fingerprint, registered
root/admin identities, pointer hash, full inventory and proof digests, and
retention facts. A callback return value, locally fabricated reference or
untyped acknowledgement is not durable intent confirmation. This is an
operation-owned cleanup journal; it is not a new generic #140 kernel
transaction protocol, lifecycle record or Task status.

Worktree and branch deletion are distinct higher-level steps with separately
qualified capabilities, current authorization and re-observation:

1. **Worktree step:** immediately revalidate the canonical Task authority,
   actual default and graph, retention observation, complete inventory, owner
   proofs, #192 diagnosis and exact root/admin registration. Only a trusted
   host-installed backend qualified for the exact operation may atomically
   custody and remove that exact managed linked worktree. The facade then
   ignores the backend's return value and independently checks that the
   original root name, exact private admin registration and native Git worktree
   registration are absent, with the pinned parent identities intact. This
   higher-level native worktree operation may remove its own exact private
   `.git/worktrees/<name>` entry and an empty private container; the common
   `.git` store is retained. There is no generic Git-admin deletion API.
2. **Branch step:** it is not attempted until root/admin absence and native
   registration removal have been observed. The branch deletion target is
   limited to the exact local Task ref and expected OID `H`, plus the retained
   local-default ref/head. The backend must perform an expected-OID
   compare-and-delete without force, remote-ref writes or unrelated ref
   changes. The facade verifies exact branch absence and confirms `H` remains
   in the actual default graph.
3. **Ephemeral step:** the exact registrar binding and inventory are refreshed
   around authorization. After the backend call the facade verifies the exact
   scope is absent and every non-target inventory node and identity remains
   unchanged. An uncertain or partial child removal is reported; it is not
   recursively cleaned or broadened.

`CleanupQualification` fields and the module-private Python permit seal are
request-binding/configuration checks, **not host attestation, an unforgeable
security boundary or proof of atomicity**. Code running in the same Python
process can replace Python objects/callbacks. Production requires a separately
audited trusted-host backend that provides descriptor-pinned identity, atomic
custody, current-authorization enforcement, expected-OID CAS and preservation
of product data. There is currently no qualified production backend; absent
one, worktree, branch and ephemeral effects are refused. Ordinary `unlink`,
`rm`, `git clean`, `git branch -D`, `git worktree remove --force`, a force-delete
flag or a post-hoc callback acknowledgement is not an acceptable substitute.
Repeated descriptor inspection alone cannot make the final unlink/rename an
identity compare-and-swap or close its TOCTOU window; only the separately
qualified atomic-custody backend can own that boundary. Unsupported host
qualification refuses the effect with no fallback. Native Git operations in
temporary test callbacks model the facade protocol only; they do not prove
backend atomicity or production host qualification.

If an effect or its acknowledgement is interrupted, the error carries the
exact recorded `intent_reference` when available. Recovery must load that
reference through the installed typed journal reader, revalidate the original
Task/Record/Contract, retention, Main state and identities, and observe actual
filesystem and Git state. It may perform only the exact remaining step under
fresh current authorization. For example, a removed worktree with branch `H`
still present leaves only the independently guarded branch step pending; it is
not treated as complete cleanup. Lost acknowledgements are resolved from root,
private admin/registry and exact branch postconditions, not callback returns.
Changed inventory, replacement root/admin, changed branch OID, lost retained
history, changed authorization/prerequisites or partial unexpected effects
fail closed and preserve all unrelated data. There is no rollback by deleting
more, resetting refs, or retrying against a newly observed target.

## Preserved authority and observation boundaries

Cleanup leaves the canonical #191 Record and Contract, metadata ref/history,
reachable metadata Git objects, Evidence and Task View Snapshots intact. It
does not garbage-collect metadata, rewrite refs/history, mutate the Task
Record/Contract, edit or merge a GitHub PR, alter a GitHub checkpoint, or create
a new Task lifecycle state. The result is a typed operation acknowledgement of
verified postconditions, not a new global Task truth or a rewritten old #144
Task View. Any later Main observation must be a fresh factual view from the
surviving repository context. #195 integration authority remains separate;
the [#198 start authorization](https://github.com/upiscium/Templates/issues/198#issuecomment-6051145984)
authorizes this source implementation and review handoff, not production
deletion or unknown-product-data disposal.

The implementation does not introduce an `AtomicProductStore` or a generic
product transaction abstraction. Exact host-owned cleanup intent persistence
and per-step atomic deletion qualification remain separate responsibilities;
neither subsumes #140 recovery ownership or the existing metadata/Task
lifecycle owners.

## Focused fixture verification and limits

The focused filesystem-inspector tests use temporary Git worktrees.
The cleanup facade tests also confine destructive
callbacks to temporary repositories. These are verification commands to run,
not a claim that they were executed or passed by documenting this staged
capability:

```sh
python3 -m unittest discover -s tests -p 'test_cleanup_resources_v4.py' -v
python3 -m unittest discover -s tests -p 'test_task_cleanup_v4.py' -v
python3 -m unittest discover -s tests -v
```

The first command covers descriptor-bound inventory, raw content hashes,
revalidation, path confinement, mount-ID failure, symlink/hardlink/special-node
refusal and replacement races. The second covers exact registered cleanup,
unknown-content proof requirements, dirty Task refusal, authorization denial,
separate-step recovery and canonical Task-ID validation. The full test discovery
is the broader core regression check; no static file count or documentation
statement is test-pass evidence.

These fixtures do not qualify a production host, prove kernel-level atomic
custody, authenticate a real GitHub/PR reader, activate VERSION 3/v4, or permit
live deletion. Runtime, host-qualification, integration and release gates remain
separate.
