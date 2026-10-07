# Task-start v4 Git/GitHub collaboration

**Status:** staged #194 source capability over #191 Task Record/Contract,
#144 factual Task View/Snapshot, #192 operation prerequisites, and #217 metadata.
It does not activate v4 or modify VERSION 3, generated templates, installed
permissions, adapters, or the trusted Templates source-publication authority.

A Task PR is the durable collaboration surface **from Task start**, not the
output of a late publication phase. This capability consumes an already-created
Task Record and registered Task worktree and establishes:

```text
exact Task authority and local Git facts
  -> empty bootstrap commit only if HEAD equals creation base
  -> exact non-force Task-branch push
  -> create/adopt one same-repository Draft PR
  -> existing owner publishes/confirms initial metadata and Snapshot
  -> durable initial checkpoint
```

Partial progress remains actual Git/GitHub/metadata facts. A failure does not
roll back confirmed writes, discard product work, create a Task `blocked` phase,
or authorize a first implementation Turn. The host must require the confirmed
initial surface before dispatching that Turn. Receipts are collaboration facts,
not launch/readiness/evaluation authority.

## Modules and installed host capabilities

`components/agent-core-v4/collaboration_git.py` provides `TaskGit`, frozen
`LocalFacts`/`BranchPush`, and safe `DistinctTaskGitError(code, operation)`:

```python
TaskGit(store, *, task, branch_ref, base_revision, default_branch_ref,
        publish=host_normal_branch_writer, fetch_remote=None)
    .observe() -> LocalFacts
    .remote_head() -> exact_oid_or_none
    .is_ancestor(old, new) -> bool
    .bootstrap_if_needed(*, expected_head, on_candidate=None,
                         resume_candidate=None) -> confirmed_head
    .push_exact(*, expected_head, expected_remote,
                on_intent=None) -> confirmed_remote_head
```

`components/agent-core-v4/task_collaboration.py` provides `TaskCollaboration`:

```python
TaskCollaboration(store, task_git, github_transport,
                  prerequisites_by_operation=host_engines,
                  authorize=host_execution_authority,
                  record_intent=host_durable_intent_recorder,
                  content_policy=host_factual_content_policy)
    .establish(report, metadata_provider) -> CollaborationReceipt
    .bootstrap(*, resume_candidate=None) -> confirmed_head
    .push() -> confirmed_remote_head
    .ensure_draft_pr() -> CollaborationReceipt
    .update_metadata(pr_number, expected_subject, expected_title, expected_body,
                     new_title, new_body) -> CollaborationReceipt
    .initial_checkpoint(pr_number, binding, report) -> CollaborationReceipt
    .turn_checkpoint(pr_number, binding, checkpoint_key, report,
                     *, boundary="turn-end") -> CollaborationReceipt
    .factual_comment(kind, number, key, text) -> CollaborationReceipt
    .github_observation(GitHubPullRequestRequest) -> GitHubObservation
```

`draft_pr` and `metadata_update` are operation-spelling aliases, not a second
publication route. `MetadataBinding` contains exact metadata commit, Record ID,
Contract ID, and Snapshot ID. Receipts contain exact repository/Task/branch/
subject and, where relevant, PR/comment/metadata/object identities. There is no
local current-PR pointer, current-checkpoint pointer, lifecycle file, or journal
format owned by this module.

The trusted host installs separate #192 engines for the requested collaboration
operations, execution authority, durable intent recording, and factual-content
policy. A `PREREQUISITES_SATISFIED` diagnosis is **not** permission. Only literal
`True` from the execution/content/intent capabilities permits their write
boundary. Their construction is host-owned, not a JSON credential, conversation
claim, or mutable Task configuration. Routine Task push, Draft PR and factual
comment policies can use execution authority without Human Ask merely because
they mutate; the facade never asks a Human or weakens installed permission rules.

Canonical request/policy/View fingerprints and the frozen `WriteIntent` bind
the exact operation, repository/Task/ref/subject, current Record/Contract,
numeric principal, target parameters, expected local/remote facts and proposed
content. Callback inputs are immutable bytes/value bindings. Guards reobserve
after prerequisite, execution-authority, content-policy and intent callbacks,
before the side effect and at postcondition confirmation. Negative #192 results
retain their encoded operation-local diagnosis; other guards expose bounded
codes, not raw transport/credential exception strings.

## Exact Task Git boundary

Task Git facts come from bounded, scrubbed Git plumbing, not copied state.
The root must be the exact registered named worktree/Task branch; symbolic Task
refs, default-ref targeting, command-bearing Git configuration, shallow history
and grafts fail closed. Current commit/tree and logical index/status fingerprints
are reobserved; optional index refresh is disabled. Child submodule worktrees
are not inspected: status ignores them, while an index-only raw diff includes
staged gitlinks. These facts do not certify uncommitted file-content identity or
child cleanliness.

All pushes, including a first push to an absent ref, require the exact creation
base to be an ancestor of local HEAD. An existing remote head must additionally
be an ancestor of the requested head. This is a conservative #194 no-rewrite
guard, not a change to #191's historical-base schema. Rebased/unrelated histories
need separately authorized handling outside this capability; it never resets,
force-pushes, merges, or invents a replacement expected OID.

If HEAD is already a valid Task-specific commit, bootstrap creates no extra
commit. Otherwise it requires clean root/index facts and creates one empty-tree-
delta commit with the creation base as its sole parent. The full exact Task ref
is compare/exchanged against that base; a saved candidate can be resumed only
with its exact parent, tree, message and current ref binding. After an applied
candidate, only the intended HEAD may differ: tree, clean state, index/status,
worktree, authority and external identities are rechecked. Safety or lock-cleanup
failure cannot be disguised as a lost acknowledgement or continue establishment.

Bootstrap holds cooperative **index then HEAD** locks and uses a detached
administrative HEAD to avoid the product HEAD reflog-lock conflict. Common/admin
directory device/inode identities are pinned. The subprocess addresses the held
common directory through Linux `/proc/<parent-pid>/fd/<fd>`, not a mutable common-
directory pathname; lock cleanup uses the held admin-directory descriptor and
only removes its owned locks. Missing directory-FD/procfs support fails closed;
there is no unsafe pathname fallback. This bootstrap implementation therefore
requires Linux support; it does not activate a cross-platform consumer runtime.

The narrow `publish(BranchPush)` host capability must authenticate/validate the
single destination and perform an ordinary non-force full-OID-to-full-Task-ref
update. The module does **not** use #217's metadata ref writer for product refs.
Optional `fetch_remote(oid)` may obtain only that exact object without changing
refs or FETCH_HEAD; local and advertised remote facts are checked afterward.
Private #217 Git-runner seams supply scrubbed environment and subprocess limits.
Production transport authentication, downloaded-pack/quota limits and server
ref enforcement remain separately vetted host responsibilities. Normal push
plus client observations is not server-side expected-old CAS; exact remote
post-read decides the receipt, not the callback acknowledgement.

## GitHub identity and bounded metadata

The typed `GitHubTransport` offers only repository/principal reads, all-state
PR pages, comment pages, Issue reads, Draft creation, title/body edits and
comment posts. There is no generic HTTP request, merge, Ready, ref-deletion or
arbitrary GitHub mutation endpoint. Its readers must authenticate and bound
their own time/I/O. Repository numeric identity and current default branch are
pinned/rechecked, and PR number is independent of Issue-backed Task number.

Full all-state scans reject duplicate Task PRs, fork/cross-repository heads,
wrong bases/subjects, non-Draft or Human-closed/merged historical Task PRs.
Closed history is not automatically replaced. Scans are bounded to 100 pages
of 100 items and an 8 MiB aggregate read budget; duplicate IDs and unstable
repeated scans fail closed. Unrelated deleted-fork or multiline prose does not
become Task authority or a false matching PR. Reads have distinct bounds from
the narrower write bounds.

One exact existing Draft is adopted, not duplicated. Its prose is **not** a
canonical Task phase/authority check: a legitimate prior metadata edit does not
force replacement or wholesale body repair. Initial proposed metadata derives
from the immutable Contract and passes host factual/secret policy. Normal edits
are restricted to bounded title/body and carry exact expected-old values; a
third Human value is conflict/decision-required, not an overwrite instruction.
An already-desired value is reobserved as a no-write postcondition.

GitHub has no atomic create-if-no-matching-history transaction or ordinary
title/body CAS. Pre/post scans catch observable races, but do not exclude every
intervening/ABA edit. The transport must honor expected-old constraints where
possible; concurrent ambiguity returns conflict/uncertain, never a fabricated
success, broad repair, changed intent, or merge authority. No receipt claims an
atomic transaction across Git, GitHub and metadata.

## Initial and Turn checkpoint protocol

A bounded checkpoint is human-readable factual report text plus a canonical
JSON capsule transported in ASCII base64 and a domain-separated SHA-256 marker.
Its closed schema-version-1 payload binds repository, Task, full branch, PR,
subject, exact metadata commit, Record/Contract/Snapshot IDs, boundary, stable
checkpoint key, numeric authenticated principal and report. The digest domain
is `agentcore-task-collaboration-checkpoint/v1\n`. Factual comments use their
separate `agentcore-task-collaboration-comment/v1\n` domain and exact Issue/PR
target. LF/tab multiline prose is bounded; CR/other disallowed controls fail.
The host content policy owns rejection/redaction of secrets, raw tool logs,
private chain-of-thought and unmade Human decisions; syntax alone cannot prove
arbitrary prose factual. Transport-altered text fails exact postcondition reads.

Only comments by the currently authenticated numeric principal in the exact
repository/target namespace count as owned checkpoints. Copied markers from
other actors are not authority. Duplicate identical same-key comments by that
principal use the lowest numeric ID as canonical, without deleting history.
Different same-key capsules conflict. Latest owned checkpoint means the highest
canonical comment ID across keys, not a guessed timestamp or prose interpretation.
The host must retain a stable authorized producer identity or separately handle
principal migration; this module does not infer another author's authority.

`github_observation` supplies these real checkpoint facts to #144 and to the
installed #192 policies; it does not report `absent` after an owned checkpoint
exists. It validates only the selected latest historical #217/#191/#144 graph,
not every historic graph, and does not recursively invoke live Task View.
Malformed, conflicting, incomplete or moved history is error/unavailable, not
fabricated absence. Older subjects remain historical and are not refreshed.

Before a new checkpoint, its metadata commit must be remotely confirmed with
the exact Record/Contract/Snapshot objects, and the owner readers must validate
their semantic graphs. New checkpoint subject, current authority, Task branch,
PR identity and remote head must match. Initial checkpoints use key `initial`
and an `explicit-handoff` Snapshot with no predecessor. A Turn requires an
already confirmed owned initial checkpoint on preserved history; its supplied
Snapshot must have the requested boundary and exact captured predecessor.

The relation remains **previous checkpoint -> Snapshot -> new checkpoint**.
Post-write guards allow only the exact confirmed new checkpoint fact, not
arbitrary predecessor movement. A retry of an already-posted immutable capsule
uses that same identity; it does not recapture a Snapshot or rewrite history.
Exact no-op/adoption receipts also reobserve local/remote/authority/PR/comment
facts before returning, rather than trusting a formerly matching scan.

## Interruption, provider journal and scope limits

Every attempted external write, including a lost acknowledgement, is followed
by exact authoritative postcondition reads. Matching intended state can be
confirmed; missing/unavailable/conflicting state is uncertain and retained for
reobservation. The host intent recorder must durably acknowledge the immutable
proposal before the side effect. It defines its own #140-style journal/recovery
storage; #194 adds no general journal, generic Task status or filesystem-private
collaboration authority.

`establish` skips its metadata provider when the exact initial checkpoint already
exists. Before that comment is confirmed, the provider must durably deduplicate
by stable repository/Task/branch/PR/subject/authority context and retain the exact
owner #144/#217 candidate/receipt. After interruption between Snapshot publication
and comment, it must return the original binding, not capture a newer Snapshot
merely because metadata tip advanced. The provider owns #191/#144 authorization
and #217 publication/confirmation; the facade does not expose generic metadata
publish. Unrelated append-only metadata-tip movement is allowed, but changed
Record/Contract/disposition/product/PR identities are not silently adopted.

No generic v3 publication phase/state reader, Verification PASS interpretation,
Work Units (#196), Admin cutover (#142), cleanup (#198), or merge/integration
(#195) is included. Ready transition is deliberately absent until the applicable
exact-subject #216 evaluation capability exists; no READY or Human merge decision
is invented. GitHub outage gates collaboration, not unrelated local inspection
or implementation. Local production work remains recoverable, not a completed
durable Turn, until its branch/metadata/checkpoint postconditions are confirmed.

Focused and full verification:

```sh
python3 -m unittest discover -s tests -p 'test_collaboration_git_v4.py' -v
python3 -m unittest discover -s tests -p 'test_task_collaboration_v4.py' -v
python3 -m unittest discover -s tests -v
```

Fixtures use actual temporary product Git/bare metadata, existing #191/#144/#217
owners and an in-memory authenticated GitHub model. They exercise early Draft
establishment, initial/Turn predecessor facts, publication interruption and
provider reuse, clone-independent Snapshot reads, non-force/ancestry guards,
descriptor and local-state races, pagination, actor spoofing, no-op confirmation,
bounded metadata and exact postconditions. They do not activate a live GitHub
adapter, publish real metadata, run runtime dogfood, or prove hosted transport
CAS/quota guarantees. Those activation gates and generated-language CI/release
lanes retain their separate ownership.
