---
description: Run an explicit Automation Maintenance Task with its dedicated orchestrator
agent: build
---

First load the `initialize` skill and complete the mandatory read-only initialization checks for the current Main worktree.

Then load the `maintenance-orchestration` skill. The arguments must identify exactly one existing Task ID. The trusted Templates source worktree and full immutable revision come only from that Task's validated maintenance receipt.

Run `just automation::maintenance-check <task>` from Main, then `just automation::dispatch-start <task>`. The dispatcher launches exactly one fixed `maintenance-orchestrator` only on a complete `status: READY`, `mode: maintenance` result and passes the exact revalidated evidence. It derives the trusted Templates source path and immutable revision from the validated active or consumed maintenance receipt; a pristine Task without that receipt is blocked rather than accepting a caller-selected path. Reconcile through `dispatch-status` and relay only exact human-selected permission decisions through `dispatch-respond`; never supply a target path, agent, host, port, password, or session ID.

If committed-stage `reviewEvidence` is incomplete, Main must first delegate each exact returned reviewer/security-reviewer objective and record only the canonical completed leaf result through `just automation::maintenance-review-record`. Re-run `maintenance-check`; the Maintenance Orchestrator has no review-recording authority.

Do not route this Task through normal `/task-run`, `contract-resume-check`, or product Task State transitions. Stop before merge.
