# Exact Human-approved v4 integration

**Status:** staged #195 source capability. It consumes existing #191/#144
authority/facts and #192 prerequisite diagnosis; it does not redesign #194
collaboration, activate v4, change VERSION 3, or alter generated templates,
installed permissions, adapters, source-publication authority or distribution.

```text
exact repository / Task PR / head / current default base / method / tree
  + operation prerequisites
  + authenticated exact-subject READY binding
  + separate explicit Human integration approval
  -> exact guarded PR merge
  -> authoritative merged postcondition
  -> independent bounded clean-default fetch / fast-forward / facts
```

There is no Task `integration-pending` or `merged` lifecycle state. A confirmed
merge receipt is a factual operation result, not an update to Task Record,
Contract, Evidence or Snapshot authority. Cleanup, worktree/branch deletion and
metadata GC remain #198; #216 evaluator/Decision View, verification semantics,
Work Units and Admin cutover are not implemented here.

## Subject and separate authority references

`components/agent-core-v4/task_integration.py` provides frozen
`IntegrationSubject`, `ReadyReference`/`ReadyBinding`,
`HumanApprovalReference`/`HumanApprovalBinding`, `MergeIntent`/`RecordedIntent`,
`IntegrationReceipt`, reconciliation records and safe operation-local errors.

The closed schema-version-1 integration subject contains exactly:

```text
schema_version, repository, repository_id, task, pull_request,
head_ref, head_oid, base_ref, base_oid, default_branch_ref,
record_id, contract_id, merge_method, expected_merge_tree_oid,
plan_ref, ready_reference_id
```

Its identity is SHA-256 over `agentcore-integration-subject/v1\n` followed by
compact key-sorted UTF-8 JSON without a trailing newline. Full Git OIDs use the
repository's SHA-1/SHA-256 format; object/reference digests use exact lowercase
64-hex IDs. Numeric IDs are positive signed-64-bit integers, not booleans.
The selected supported method is `merge` or `squash`, only if permitted by the
host/repository policy. Rebase is unsupported and never substituted.

The current PR base OID is deliberately separate from #191's immutable
**creation base**, which remains historical provenance. Planned result tree and
plan reference must come from an authenticated merge-plan reader tied to the
exact PR/head/base/method, not an LLM or caller's guessed merge result.

READY and Human approval are independent references bound to this full subject
ID. Host validators must authenticate their sources and return closed typed
bindings with exact subject/reference/provenance; Human binding additionally
identifies the actual Human and purpose `approve_integration`. Generic `True`,
unbound DTOs, CI Green, PR non-Draft/Ready, review approval, implementation
acceptance or an approval for an older subject are not merge authority.

The READY adapter is **future-compatible input only**. It must verify an
applicable outcome from the future owner; #195 neither synthesizes READY nor
parses/invents #216 evaluation/Evidence semantics. Its v1 content-addressed
reference can be an adapter digest over the owner's eventual record. Production
qualification of that future adapter remains an activation gate.

## Trusted transport qualification

An adapter's advertised Boolean capabilities are only negative probes. They
cannot grant merge authority or qualify a head-only endpoint. The trusted host
must separately call:

```python
qualify_merge_transport(
    transport, *, repository, repository_id, default_branch_ref,
    allowed_merge_methods, host_qualifier
) -> MergeTransportPermit
```

The qualifier must verify the concrete authenticated adapter object and return
typed `MergeTransportQualification` covering **atomic** exact head, base,
repository, PR, full subject, method/tree, current READY and current Human
approval at the commit boundary. The resulting opaque permit is sealed and
bound by object identity to that adapter and repository/default/method scope.
Parsed JSON, generic booleans, self-advertised flags, or a permit for another
adapter fail closed. Python's module-private seal prevents accidental/caller
substitution, not arbitrary malicious same-process code able to replace the
trusted factory. Host construction/provenance remains outside Task-controlled
configuration.

**Ordinary GitHub merge APIs with expected-head-only matching are insufficient.**
Client re-reads cannot provide server-side exact-base CAS or close approval
revocation races. A live host must enforce the complete pair and current
authorities atomically, or an equivalent qualified one-shot authority protocol,
at the actual merge. Without that guarantee the transport must not be qualified
and integration remains unavailable. A wrong-base post-read can detect a bad
write but cannot undo it; there is no permissive fallback or rollback rewrite.

## Guarded merge and authoritative confirmation

The narrow facade is:

```python
TaskIntegration(
    task_store, task_git, default_git, github_transport,
    merge_transport_permit=qualified_host_permit,
    prerequisites_by_operation=host_engines,
    ready_validator=host_ready_reader,
    human_validator=host_explicit_human_authority_reader,
    record_intent=host_durable_merge_journal,
    load_intent=host_exact_journal_reader,
    record_sync_intent=host_local_sync_journal,
    checkpoint_reader=host_authenticated_checkpoint_reader
)
    .capture_subject(pr_number, method, ready_reference_id) -> IntegrationSubject
    .merge(subject, ready_reference, approval_reference) -> IntegrationReceipt
    .recover(intent_ref) -> IntegrationReceipt
    .reconcile(intent_ref) -> IntegrationReconciliationReceipt
```

Capture is read-only and is reobserved before its return; it does not issue an
approval. Task authority is read through the existing owner. The Task branch,
local and remote head, exact same-repository PR, current default base, repository
numeric identity, permitted method/plan, current Record/Contract and qualified
transport must agree. A non-Draft open PR and current mergeability are technical
requirements, never substitutes for READY/Human authority.

#192's `integration.merge` diagnosis is Human-owned and binds the exact subject
and reference parameters; a positive result is **not** merge permission. Typed
READY and explicit Human validators are separately invoked, revalidated and
compared around prerequisite and journal callbacks. Mutable Git/GitHub and Task
facts are reread immediately before the sole `merge_exact(intent)` mutator.
Head/base/repository/PR/authority/method/tree/plan movement refuses the old
subject rather than renewing approval, selecting another PR, or inferring intent
from prose. Unrelated append-only metadata-tip movement is allowed only while
Record/Contract identity and every other relevant fact stay unchanged.

A typed authenticated `CheckpointObservation` is required. `absent` means the
host actually inspected the trusted namespace and found none; `observed` supplies
the owner's exact previous-checkpoint binding. An omitted/raw-None reader is
unavailable, not invented absence. #144's schema and metadata owners are unchanged.

Before merge, a frozen domain-separated `MergeIntent` binds the full subject,
READY/Human proof provenance, numeric execution principal and #192
request/policy/View fingerprints. A typed `RecordedIntent` must acknowledge the
same digest and durably address that proposal before the side effect. Storage,
provenance and cross-process recovery are host/#140 responsibilities, not a new
general journal format, Task phase, or filesystem-private merge authority.

After **every attempted merge**, even an exception/lost acknowledgement, the
facade ignores the transport result and rereads the same authenticated PR,
merged commit and default graph. Confirmation requires:

* exact repository, PR number, head/ref and expected base ref;
* actual merged state, exact merge OID and selected method;
* ordinary merge parents exactly `(approved_base, approved_head)`, or squash
  parent exactly `(approved_base)`;
* actual result tree exactly the authenticated planned/approved tree;
* the resulting merge reachable from the actual current default tip.

The PR's current base OID may already be the merge/default tip after a merge;
the approved historical base is proved from the commit's first parent, not a
blind comparison with that mutable API field. A later default tip may be a
descendant, but cannot substitute another merge. Unavailable, wrong or ambiguous
postconditions produce an uncertain result; an already-applied write remains
observable and is never reset or force-repaired.

## Recovery is proof, not another merge

`recover(intent_ref)` authenticates the exact host-stored immutable intent and
recomputed digest. It does not create an intent from a current PR or receipt,
adopt an alternate PR, infer success from the latest tip, or call merge again.
Only after the exact recorded merge postcondition is proven may validators use
`historical` mode to authenticate the original READY/Human proof and provenance.
The same merge is reread after those callbacks.

Historical mode cannot authorize a fresh mutation or recover an open/closed-
unmerged PR. Revocation after a proven merge does not retroactively unmerge it,
but the historical authority event must still be verifiable. A new merge attempt
always needs current valid authority for its exact subject. The host must retain
intent/proof records across process loss; v4 Task lifecycle states are not a
recovery substitute.

## Autonomous clean-default reconciliation

`components/agent-core-v4/integration_git.py` exposes frozen `DefaultFacts`,
`CommitFacts`, `FetchRequest`, `FastForwardRequest`, `DefaultReceipt`, and:

```python
DefaultGit(default_store, *, default_branch_ref,
           fetch=host_exact_object_fetch,
           fast_forward=host_pinned_data_safe_local_ff)
    .observe() -> DefaultFacts
    .remote_head() -> exact_default_oid
    .commit_facts(oid) -> CommitFacts
    .is_ancestor(old, new) -> bool
    .reconcile(*, merged_oid, expected_remote_oid, on_intent=None) -> DefaultReceipt
```

The default root must be the exact unique registered clean default-branch
worktree, original or linked, with bounded reciprocal admin/common-directory
proofs and pinned filesystem identities. Task and default stores must share
the same actual common Git object-store identity: equal repository slugs alone
are insufficient. A Task worktree or another ref is not silently adopted as Main.

After recovering the confirmed merge, #192's separate reconciliation diagnosis
uses execution mode, not another Human merge request. Exact current upstream
OID and merge identity are fetched first; local graph checks occur **after**
that fetch, so a server-created merge object need not already exist locally.
Callbacks may not update refs, FETCH_HEAD or config while fetching. Confirmation
requires the merge to be an ancestor of the fetched default tip and the clean
local head to fast-forward to it. Divergence/local-only work is not discarded.

The worker refuses command-bearing config, incomplete history, sparse/hidden
index states and unsafe roots/refs. Root status excludes child submodule
recursion; staged gitlinks remain visible in raw index comparisons. Bounded tree
and changed-path checks refuse changed symlink/gitlink transitions, symlink
ancestors and ignored/untracked collisions, including directory replacement
that could erase ignored descendants. Unaffected ignored caches are retained.
Logical fingerprints do not certify adversarial dirty-content identity or
uninspected child cleanliness.

The bounded fetch may add the exact requested objects to the local Git object
database, without moving refs or FETCH_HEAD. The only local worktree/default-ref
mutation is a host-installed `fast_forward(FastForwardRequest)`
capability tied to the exact root, direct default ref, expected old head/index/
status and verified target tree/index. It must supply pinned administration,
locking/CAS and data-safe checkout guarantees. A bare ambient `git merge
--ff-only` alone is **not** a production worktree CAS. The client does not expose
push/reset/rebase or a generic default-ref writer. It rechecks after intent and
FF callbacks and ignores their acknowledgements; only exact clean HEAD/tree/
index/root/ref/config/other-ref/untracked postconditions yield a receipt.
Partial/uncertain checkout is retained and reported, not rolled back or cleared.

No-op/retry reconciliation still confirms the exact graph and clean default
facts and performs no second FF or sync-intent write. A confirmed GitHub merge
receipt remains attached to a later dirty/diverged/unavailable reconciliation
failure, rather than turning synchronization failure into global Task state
or another Human merge request. Metadata, Task worktrees/branches, Contract,
Evidence and Snapshots are retained. The only optionally permitted journal-side
ref movement is an append-only metadata-intent ref; no GC or cleanup is performed.

## Verification and activation limits

```sh
python3 -m unittest discover -s tests -p 'test_task_integration_v4.py' -v
python3 -m unittest discover -s tests -p 'test_integration_git_v4.py' -v
python3 -m unittest discover -s tests -v
```

Temporary Git/bare-host fixtures and authenticated in-memory GitHub/proof
registries exercise explicit authority rejection, moved exact subjects,
commit-time revocation, merge/squash parent/tree proof, lost-ack and interrupted
intent recovery, remote-only merge fetching, linked Main/Task identity, dirty/
diverged/ignored collision preservation and FF/no-op acknowledgement handling.
They do not prove a production GitHub server, future #216 evaluator, external
authority ledger, or filesystem FF backend.

Live activation requires separately qualified full-subject-and-authorities
atomic merge transport, READY/Human provenance validators, durable intent store,
authenticated checkpoint reader, #192 policies, exact bounded fetch and pinned
data-safe FF capabilities. Ordinary head-only GitHub merge cannot qualify.
No live merge, runtime dogfood, metadata publication/GC or generated-language
build success is claimed by this staged source-only capability. Later host,
integration, release and supported-platform gates retain ownership.
