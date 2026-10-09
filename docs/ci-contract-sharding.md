# CI contract suite sharding (#239)

This document describes an **unpublished candidate implementation** for the independent
CI latency issue. It does not change the frozen #198 publication or authorize source
publication, branch-protection changes, merge, runtime activation, or real cleanup.

## Baseline and observed limitations

- [PR #237](https://github.com/upiscium/Templates/actions/runs/37716738891):
  1,333 `unittest` tests, 580.396s; the workflow passed in about 10m15s.
- [PR #238](https://github.com/upiscium/Templates/actions/runs/37779216911):
  1,378 `unittest` tests, 2,428.850s, 8 skips; workflow passed in about 41m.
- The same Ubuntu 24.04 runner-image release and Git 2.55.0 appeared in both
  captured runs. A roughly fourfold slowdown also affected several unchanged
  Git/metadata-heavy suites. The precise cause remains unproven.
- [PR #240](https://github.com/upiscium/Templates/actions/runs/37900930497)
  ran **1431 tests in 8006.965s (~2h13m27s)** and ultimately
  **FAILED (errors=1, skipped=8)**. The error was
  `TaskViewError: snapshot_graph_invalid` in
  `test_task_collaboration_v4`; tracked separately under
  [#241](https://github.com/upiscium/Templates/issues/241).
  No exact-head #240 CI PASS is claimed.
- The same #240 log shows about 3563s in `test_turn_orchestration_v4`,
  1509s in `test_task_cleanup_v4`, 977s in `test_task_collaboration_v4`,
  679s in `test_task_integration_v4`, and 527s in `test_task_view_v4`.
  These are approximate intervals between adjacent test start lines and
  include fixture work, not isolated method-body timings.
- The #196 addition has 53 focused tests, 28 host-seam cases with slow authentic
  checkpoint fixtures. Its `test_turn_orchestration_v4` file is reserved for
  shards 4–7, at individual test-ID boundaries, whenever present.
- The longest #238 integration-style single test took approximately 645s;
  no sharding scheme can make CI faster than its slowest required test.
  The aspirational end-to-end 10-minute target is not yet demonstrated.

## Execution model

```text
eight Python jobs (0..7) — each independently discovers the entire test suite
         |   |   |   |   |   |   |   |
         +---+---+---+---+---+---+---+
                                 |
                      eight exact result artifacts
                                 |
                      final `contracts` aggregator
                         /                 \
            independent Nix checks    five template smoke jobs
```

The runner `tools/ci_contract_runner.py` performs the same `TestLoader.discover`
that `python3 -m unittest discover -s tests -v` performs. It refuses import
errors, empty or duplicate test IDs, unbound modules, unexpected shard count, and
missing/empty partitions. Distribution normally occurs at the test-file boundary and preserves
per-module test method order. The **only deliberate exception** is the #196
`test_turn_orchestration_v4` suite: its independent per-test temporary Git
fixtures are divided deterministically across 4 dedicated shards using the
observed #240 test durations as **weights**, not success claims or authority.
The four established heavy modules remain pinned to shards 0–3. Unknown/new
modules are always included by deterministic balancing; when #196 is not
present (current `main`), all eight shards share the other modules.

Each shard discovers the **entire suite**, checks exhaustive assignment, executes
its selected cases, and emits a machine-readable report with the complete
inventory fingerprint, exact selected IDs, pass/fail/skip evidence, and per-test
and wall-clock timing. Downloaded receipts use an exact closed JSON schema:\n8 MiB maximum size, no duplicate keys or non-finite numbers, exact integer\ncounters, and no missing/extra fields. All violations fail closed.\nThe known eight environment-gated skips (six due to absent
`just` and two due to absent `opencode`) are explicit; newly skipped IDs
fail the test-run gate rather than silently reducing coverage.

Python shard and independent support jobs have a **75-minute fail-closed timeout**,
rather than leaving a hung test in the old long-running serial job; the final
aggregate is limited to 15 minutes. This does not declare timed-out tests passed.
Each completed test also emits a `CI_TEST_SECONDS` line for log-level diagnosis.

The final `contracts` job has the same required check name as before and runs
with `always()`. It fails if a Python shard, any original Nix/contract command,
or any generated-template smoke did not report success. It downloads all eight
Python reports and independently re-discovers the full suite to verify each
exact assignment, the same inventory fingerprint, every test executed once,
no additional skips, and no missing/duplicate shard. Skipped, cancelled,
or incomplete jobs cannot produce a green aggregate gate.

## Local reproduction

From a clean isolated checkout of the intended source:

```sh
python3 tools/ci_contract_runner.py verify
python3 tools/ci_contract_runner.py run --shard 0 --report /tmp/contract-shard-0.json
python3 tools/ci_contract_runner.py run --shard 1 --report /tmp/contract-shard-1.json
python3 tools/ci_contract_runner.py run --shard 2 --report /tmp/contract-shard-2.json
python3 tools/ci_contract_runner.py run --shard 3 --report /tmp/contract-shard-3.json
python3 tools/ci_contract_runner.py run --shard 4 --report /tmp/contract-shard-4.json
python3 tools/ci_contract_runner.py run --shard 5 --report /tmp/contract-shard-5.json
python3 tools/ci_contract_runner.py run --shard 6 --report /tmp/contract-shard-6.json
python3 tools/ci_contract_runner.py run --shard 7 --report /tmp/contract-shard-7.json
python3 tools/ci_contract_runner.py reconcile --reports /tmp
```

For isolation, run each shard in a **separate copy/worktree**. Do not run separate
shards concurrently in a single repository root: real Git fixture tests must not
share mutable fixture state.

`tests/test_ci_contract_shards.py` contains positive exhaustive-partition tests,
negative missing/duplicate/changed-digest/failed/extra-skip receipt tests,
and synthetic test-failure propagation checks.

## Operational and security caveats

- Test duration sampling uses `startTest/stopTest`; these measurements exclude
  possible module/class fixture setup time. Always keep total shard wall time.
- The local #239 candidate is based on the last published `main` before #196.
  A separate throwaway clone of PR #240 plus these CI files is used for
  non-publication discovery/contract compatibility only. This neither merges
  nor modifies PR #240. Full GitHub CI remains a future publication gate.
- A passing sharded suite on a local LXC is not a GitHub-hosted CI outcome,
  and concurrent LXC test runtime is not directly comparable to GitHub-hosted
  runner runtime.
- The ordinary case remains file-level; only the isolated #196 TestCase is split by test ID. Some modules contain one
  very expensive case, limiting wall-clock improvement. Subsequent work may
  optimize disposable test fixture construction and measured Git subprocess
  overhead but must preserve real adversarial Git/FS semantics.
- Splitting one runner into eight can increase total billed GitHub Actions
  minutes. Compare both latency and aggregate compute consumption.
- The eight current skips are **not** equivalent to eight executed tests.
  Closing the environment coverage gaps (installing pinned `just`, qualifying
  the `opencode` tests) should be treated as a separate reviewed subtask.
- This CI code is PR-controlled as ordinary source code, not a trusted
  publication, bootstrap, or deletion authority. The final workflow, its
  dependency/required-status topology, and report semantics require a separate
  Correctness/Security review and exact source-publication authorization.
