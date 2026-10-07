# Operation-local v4 prerequisite diagnosis

**Status:** staged #192 source substrate for AgentCore 1.0.0. It consumes the
merged #144 factual Task View and preserves #191/#145 authority boundaries.
It does not activate v4, change VERSION 3, or alter generated templates,
permissions, adapters, or the installed source-publication workflow.

```text
deterministic factual Task View + exact requested operation
  + operation-specific trusted policy + capability-owned checks
  -> machine-readable diagnosis of that operation only
```

Failure of a prerequisite for operation X is **not** global Task `blocked`,
readiness, or lifecycle state. There is no persistence API, generic initialization
gate, workflow phase, canonical next action, automatic repair, or Decision View
generation. Publication (#194), integration (#195), Verification/Work Units
(#193/#196), cleanup (#198), and Admin cutover (#142) keep their implementations
and policy ownership; operation names in tests are representative fixtures only.

## Trusted host construction, not permission issuance

`components/agent-core-v4/operation_prerequisites.py` exposes frozen dataclasses
`OperationRequest`, `OperationPolicy`, `CheckSpec`, `CheckBinding`, `CheckContext`,
`CheckResult`, and:

```python
OperationPrerequisites(policy, *, checks=host_readonly_registry)
    .diagnose(request, live_task_view) -> dict
encode_diagnosis(diagnosis) -> bytes
```

The **trusted host** selects the policy owner and installs read-only check
capabilities. An agent-supplied JSON policy, producer name, approval parameter,
digest, or successful diagnosis is not a credential. Python code able to replace
host callbacks or evaluator internals is outside this boundary. There is no
general operation registry or centralized commit/push/merge policy in #192.

Callbacks must authenticate and bound their own observations, validate their
capability-specific target and execution authority, and return facts without
performing the requested operation. The evaluator itself performs no Git,
filesystem, network, metadata, product, or Human-authority mutation. It does not
ask a Human, choose an ambiguity's answer, or authorize a later operation.
Execution owners must re-observe exact identities/subjects, enforce caller
permissions and locks, and handle races/recovery at their mutation boundary.

## Closed request and policy

`OperationRequest` has exactly repository, canonical positive-decimal Task,
full `refs/heads/...` branch, full lowercase 40/64-hex subject, operation name,
and an object of operation-owned `parameters`. Identity grammar comes from
#217/#191. Parameters bind targets and expectations through canonical request
bytes; their meaning belongs to the selected capability. No generic `approve`,
remote name, PR number, default-branch role, verification PASS, or Human intent
is inferred from their spelling.

`OperationPolicy` has exactly operation, owner, authority mode, and a tuple of
1–64 `CheckSpec(check_id, owner, category)` declarations. Categories are optional
bounded identifiers, not globally interpreted workflow phases. Check IDs are
unique; declarations are sorted canonically, independent of input order.
Only selected callbacks are copied from the host registry and invoked, in that
order. Extra registry entries are not called; absent/non-callable selected
checks are unavailable rather than satisfied.

Authority mode is `execution` or `human_owned`. The latter must declare a
`human_authority` check. A host must actually validate Human-owned authority;
mechanical convergence cannot satisfy that category. The policy may declare
such a check under execution mode as well. Conversely, routine bounded
Task-local commit, normal non-force push, Draft PR, or factual-comment
operations are **not** promoted to Human Ask merely because they mutate.
Their owners can install execution-mode policies with identity/safety checks.
Default-branch, integration, destructive/data-loss, and Human-intent boundaries
remain separately enforceable by their owning policies. #192 does not weaken
the current installed runtime's permission configuration.

## Factual View and exact binding

The evaluator deep-detaches canonical request and live View bytes before any
callback. It uses #144's existing **pure** `encode_snapshot` API solely to
validate the owner's closed View schema and codec limits. It does not fetch
facts, construct a durable Snapshot, expose a Snapshot ID, or persist/freeze
anything. The live View fingerprint has its own distinct domain:

```text
request_id = SHA256("agentcore-operation-request/v1\n" + canonical request bytes)
policy_id  = SHA256("agentcore-operation-policy/v1\n" + canonical policy bytes)
view_id    = SHA256("agentcore-live-task-view-fingerprint/v1\n" + canonical live View bytes)
```

Canonical JSON is compact, key-sorted UTF-8 without a trailing newline. These
fingerprints bind **byte equality only**, not authenticity, freshness, producer
attestation, or permission. The host supplies an observed #144 live projection;
the substrate is not an alternative factual authority.

Request/View repository, Task, and Task-branch mismatch yields an identity
precheck; subject mismatch yields a stale-subject precheck; a requested-operation
versus installed-policy mismatch yields an authority precheck. Each includes
actual `observed` and request `expected` values and invokes **no callbacks**.
Otherwise, every declared check must have exactly one ordered report. Missing
callbacks generate reports, not a shorter favorable subset.

A frozen `CheckContext` carries canonical immutable request/policy/View bytes
plus a `CheckBinding` containing repository, Task, branch, subject, operation,
all three fingerprints, check ID, and check owner. A typed `CheckResult` must
echo that exact binding. Reuse across a changed request target, policy, View,
subject, check, or owner fails closed. This is reference integrity, not proof
that arbitrary callback bytes describe the world truthfully.

The diagnosis references the selected View Evidence IDs and their exact
subjects. #144/#145 remain owners of Evidence validation and subject equality;
#192 does not read opaque payloads, select latest/effective Evidence, infer
PASS/FAIL/domain validity, or replace capability-owned Verification checks.
#144's count-only dirty facts, `not_inspected` child submodule worktrees, and
caller-selected comparison-ref role cannot substitute for an operation's own
exact scope/index/remote/default-branch guards.

## Operation-scoped reports and aggregation

Each check report has exactly check ID, owner, category, outcome, bounded
reason code, observed/expected JSON objects, and convergence classification.
The check owner defines the factual meaning of observed/expected; #192 does
not turn an opaque `PASS` into authority.

Check outcomes are:

```text
SATISFIED
MISSING_PREREQUISITE
IDENTITY_CONFLICT
AUTHORITY_CONFLICT
STALE_SUBJECT
SEMANTIC_DECISION_REQUIRED
UNAVAILABLE_DEPENDENCY
UNSAFE_TO_CONVERGE
```

With all declared checks satisfied, the diagnosis result is `PREREQUISITES_SATISFIED` for
the **requested operation and captured inputs only**. Otherwise, one headline
failure is selected in this deterministic precedence order:

```text
IDENTITY_CONFLICT, AUTHORITY_CONFLICT, STALE_SUBJECT,
SEMANTIC_DECISION_REQUIRED, UNSAFE_TO_CONVERGE,
UNAVAILABLE_DEPENDENCY, MISSING_PREREQUISITE
```

For ties, the first sorted check supplies the reason code. All reports remain
available; headline precedence neither erases other failures nor selects an
action. `human_decision_required` remains true if any check requires a semantic
decision, even if an identity/authority failure is the headline.

Convergence is one of `none`, `mechanical`, `unsafe`, or `semantic`. Mechanical
classification is accepted only for satisfied or missing prerequisites;
semantic classification must pair with `SEMANTIC_DECISION_REQUIRED`, and unsafe
classification with `UNSAFE_TO_CONVERGE`. Inconsistent pairs are invalid claims.
`mechanical_convergence_candidate` is true only when a check explicitly reports
mechanical convergence and every report is satisfied or a mechanically
convergent missing prerequisite, with no identity/authority/stale/semantic/
unavailable/unsafe gap. It is a policy-reported safe-convergence candidate,
not permission to execute or retry. It does not run convergence, choose a repair,
or make a missing-prerequisite operation executable. Semantic/product/architecture/
requirement choices instead produce a decision-required diagnosis, not a
Human decision or a global block.

`human_authority_required` records the selected policy's explicit boundary.
It is distinct from semantic ambiguity and is not an unconditional UI Ask.

## Failure isolation and wire schema

Local inspection/edit fixtures require only their local checks: a View with
GitHub `unavailable` does not gate them, and unrelated tool/Admin/GitHub
callbacks are never invoked. A policy that does require GitHub, project tools,
or Admin capability reports failure only for its requested operation.

Missing checks and callback exceptions produce safe availability/completion
conditions without raw exception text. Wrong result types, unsupported outcomes,
oversized observations, or inconsistent semantics become safe authority-conflict
reports. Wrong echoed binding becomes identity conflict. These synthetic reason
codes are reserved and cannot be supplied as favorable capability results.

The closed schema-version-1 diagnosis contains:

```text
schema_version, repository, task, branch_ref, subject, operation,
request_id, policy_id, view_id, policy_operation, policy_owner,
authority_mode, required_checks, evidence, result, reason_code,
precheck, human_decision_required, mechanical_convergence_candidate,
human_authority_required, checks
```

`precheck` is null for normal evaluation or a closed outcome/reason/observed/
expected object for the three early binding failures. Prechecks and callback
reports cannot be mixed. `encode_diagnosis` checks the full declared set,
policy digest, operation consistency, precheck facts, aggregation, and flags;
it rejects missing/extra fields including lifecycle status and next action.
It does not authenticate a parsed document or re-resolve its request/View
fingerprints. Consumers must retain and compare the bound source bytes and
trusted policy/producer provenance if needed; no durable reader or authority
is added here.

JSON permits null, literal booleans, signed 64-bit integers, valid Unicode
strings, lists and string-keyed objects, not floats or arbitrary Python objects.
Depth/node/escaped-UTF-8 aggregate byte limits are applied before full document
serialization; each check's observed and expected object is at most 1,024 bytes.
Invalid caller schemas raise bounded validation errors and never imply success.
Hosts own secret rejection/redaction before providing observed/expected values;
the evaluator suppresses exception strings but cannot certify arbitrary
capability data as secret-free. Callbacks must also bound their own time/I/O;
there is no generic untrusted-code sandbox or timeout executor.

## Verification and remaining activation boundaries

Focused and full fixture commands:

```sh
python3 -m unittest discover -s tests -p 'test_operation_prerequisites_v4.py' -v
python3 -m unittest discover -s tests -v
```

Tests exercise policy-specific dependency isolation, normal bounded mutation
diagnoses without blanket Human Ask, separately Human-owned checks, mechanical/
semantic/unsafe distinctions, exact binding replay rejection, precheck observed/
expected facts, canonical ordering, schema/aggregation tampering, callback error
secrecy and JSON bounds. A temporary local #217/#191/#145/#144 fixture confirms
diagnosis leaves product HEAD/tree/index/status/refs/config and metadata tip
unchanged. Test guard implementations are illustrative, not production
commit/push/PR/comment/merge/cleanup policies or execution verification.

No live GitHub operation, project verifier, Work Unit, Admin cutover, cleanup,
publication, integration, runtime dogfood, or Decision View is implemented or
claimed executed. Later capability owners must supply vetted policies/checks
and mutation-time guards; staged code and fixture evidence do not activate them.
