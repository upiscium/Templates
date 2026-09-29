# Agent Core current-behavior characterization

This document records the observable behavior at source revision
`d515a229d39a97ff0a77a4265729d3a4a5f56a5c`, before the authority migration
defined by #189. It is a compatibility/migration input, **not** a specification
of the target architecture. “Covered” means the existing suite has direct
assertions for the stated behavior; “partial” means only some boundaries or
fields are asserted; “missing” means no exact characterization was found in
the pre-#190 inventory.

## Coverage matrix

### 1. Exact repository / Task / branch / worktree identity

- **Current owner/surface:** Task Contract validation, Git worktree/ref
  registration, and lifecycle/recovery checks.
- **Existing exact tests:**
  `tests/test_init_contract.py::test_task_branch_identity_mismatch_is_rejected`,
  `::test_task_state_worktree_mismatch_is_rejected`;
  `tests/test_task_contract.py::test_readiness_rejects_live_repository_and_exact_state_identity_mismatch`;
  `tests/test_task_state_recovery.py::test_pull_request_identity_failures_reject_before_recovery`,
  `::test_zero_authority_initial_topologies_recover_identically`.
- **Existing coverage:** **partial** — exact fields and registered-worktree
  conditions are tested in separate boundaries, but were not asserted together
  across a recovery mutation.
- **Current/legacy coupling:** identity is duplicated in Task State and Git
  registration/ref data; current consumers require those copies to agree.
- **Later migration:** #191 moves canonical Task identity/branch binding to the
  minimal Task Record; #144 derives current worktree and HEAD from Git.

### 2. Task Contract source binding and live-Issue/source-drift gating

- **Current owner/surface:** Task Contract hydration, initial/resume checks,
  refresh, and source-side recovery bridges.
- **Existing exact tests:**
  `tests/test_task_contract.py::test_initial_check_rejects_authoritative_issue_change_after_hydration`,
  `::test_authoritative_issue_binding_rejects_same_identity_contract_rewrite`;
  `tests/test_task_contract_refresh.py::test_live_issue_race_fails_and_restores_original_contract_bytes`,
  `::test_stale_maintenance_check_fails_before_refresh_and_local_validation_passes_after`;
  `tests/test_task_state_recovery.py::test_wrong_implementation_revision_fails_before_fetch`,
  `::test_dirty_source_fails_before_fetch`.
- **Existing coverage:** **covered** for changed requirement-bearing Issue
  content and exact source revision/cleanliness gates; title-only and every
  second-read drift combination are not independently enumerated.
- **Current/legacy coupling:** resume and selected local paths depend on live
  Issue equality/source readiness even after a stored contract snapshot exists.
- **Later migration:** #191 makes the immutable contract snapshot authoritative;
  #192 scopes live-source prerequisites to operations that require them.

### 3. Evidence freshness and immutable subject binding

- **Current owner/surface:** verification receipts, PR metadata, maintenance
  review evidence, publication gates, and recovery receipts.
- **Existing exact tests:**
  `tests/test_agent_core_automation.py::test_verification_rejects_head_drift_during_project_check`,
  `::test_dirty_worktree_verification_cannot_authorize_publication`,
  `::test_pr_ready_exact_match_rechecks_before_ready_write`,
  `::test_pr_ready_rejects_stale_live_body_before_write`;
  `tests/test_maintenance_lifecycle.py::test_review_evidence_is_bound_to_exact_maintenance_subject`;
  `tests/test_task_state_recovery.py::test_resume_rejects_base_identity_drift_from_recovery_receipt`.
- **Existing coverage:** **partial** — exact HEAD/body/receipt subjects are
  guarded, but the suite has separate domain-specific rules rather than one
  immutable subject-bound evidence boundary.
- **Current/legacy coupling:** successful verification/review remains tied to
  mutable Task State, status history, and domain-specific evidence files.
- **Later migration:** #145 introduces the minimal immutable subject-bound
  Evidence substrate; #193 migrates verification evidence to it.

### 4. Current Work Unit ↔ verification/review evidence coupling

- **Current owner/surface:** Work Unit history and lifecycle evidence readers
  used by verification/publication and recovery.
- **Existing exact tests:**
  `tests/test_agent_core_automation.py::test_failed_non_review_work_units_do_not_gate_publication`,
  `::test_only_highest_reviewer_sequence_is_authoritative`,
  `::test_verification_receipt_for_another_task_is_rejected`;
  `tests/test_task_lifecycle.py::test_guarded_blocked_publication_recovery_is_exact_locked_status_cas`;
  `tests/test_task_state_recovery.py::test_state_builder_is_conservative_and_schema_bound`,
  `::test_real_registered_fixture_recovers_and_resumes_without_product_mutation`.
- **Existing coverage:** **covered** for the current publication-review gate and
  the recovery rule that missing history cannot become fabricated verification
  or review PASS evidence.
- **Current/legacy coupling:** effective review authority is derived from
  Work Unit history; current guarded publication transitions capture
  `work-units.json` with `verification.json`.
- **Later migration:** #145 makes valid Evidence independent of retained Work
  Unit history; #196 separates Work Unit/orchestration state.

### 5. Exact external PR identity at publication/integration boundaries

- **Current owner/surface:** REST PR normalization, publication recovery,
  `pr-edit`/`pr-ready`, finalization, and cleanup.
- **Existing exact tests:**
  `tests/test_task_state_recovery.py::test_pull_request_identity_failures_reject_before_recovery`;
  `tests/test_source_publication_recovery_bridge.py::test_blocked_akv_shaped_stale_draft_recovers_same_pr_and_preserves_subject`,
  `::test_pr_replacement_is_rejected_before_canonical_edit_mutation`;
  `tests/test_source_publication_ready_recovery_bridge.py::test_source_pr_requires_unique_pr_and_validates_exact_pr_29`;
  `tests/test_post_merge_finalization.py::test_wrong_pr_evidence_and_ambiguity_are_rejected`,
  `::test_head_identity_must_remain_exact_during_revalidation`.
- **Existing coverage:** **partial** — exact number/repository/head/base/state
  selection is strongly characterized; the finalization second-read replacement
  of the PR number was not directly asserted.
- **Current/legacy coupling:** publication and integration authority depends on
  live GitHub identity plus Task State publication metadata.
- **Later migration:** #194 owns publication identity; #195 owns integration
  identity and merge evidence.

### 6. GitHub/Git/ref/destructive-operation interruption and TOCTOU

- **Current owner/surface:** publication/recovery bridges, private-state
  receipts, Git ref finalization, task commit/recovery locks, and local deletion.
- **Existing exact tests:**
  `tests/test_source_publication_recovery_bridge.py::test_interrupted_blocked_recovery_retry_converges_same_pr_and_consumes_receipt`,
  `::test_receipt_is_retained_when_pr_changes_before_consumption`;
  `tests/test_source_publication_ready_recovery_bridge.py::test_ready_recovery_retries_after_github_ready_before_local_transition`;
  `tests/test_post_merge_finalization.py::test_ref_movement_after_evidence_revalidation_does_not_mark_merged`;
  `tests/test_automation_upgrade.py::test_apply_rolls_back_when_post_apply_validation_detects_drift`;
  `tests/test_git_private_state.py::test_canonical_directory_replacement_during_traversal_is_revalidated`.
- **Existing coverage:** **partial** — tested interruption points have explicit
  retry/no-mutation behavior; the suite does not claim general hard-crash or
  process-kill durability.
- **Current/legacy coupling:** external writes and local refs are non-atomic;
  operation-specific receipts, locks, and revalidation are required to reconcile
  retries.
- **Later migration:** #140 owns crash journaling; #194, #195, and #198 retain
  operation-specific publication, integration, and cleanup recovery.

### 7. Destructive local-operation confinement and local-only work preservation

- **Current owner/surface:** guarded local deletion, Task lock/state checks,
  Git cleanup receipts, and ref/branch cleanup.
- **Existing exact tests:**
  `tests/test_local_delete.py::test_recursive_delete_is_descriptor_anchored_and_rejects_symlink_escape`,
  `::test_recursive_delete_rejects_a_same_device_top_level_mount_before_mutation`,
  `::test_delete_holds_canonical_lock_until_mutation_finishes`,
  `::test_transition_wins_lock_and_local_delete_revalidates_fresh_state`;
  `tests/test_post_merge_finalization.py::test_local_only_commit_is_preserved_and_rejected`,
  `::test_cleanup_removes_only_expected_registration_and_branch`.
- **Existing coverage:** **covered** for traversal/symlink/mount/state confinement,
  lock serialization, and preserving unpublished local commits.
- **Current/legacy coupling:** local deletion requires mutable Task lifecycle
  status and the Work Unit lock; cleanup requires terminal Task State and
  external publication evidence.
- **Later migration:** #198 moves these rules into the destructive cleanup
  Capability; #192 owns operation-specific prerequisite evaluation.

### 8. Ambient Git/runtime configuration trust boundary

- **Current owner/surface:** shared Git runners, initialization, upgrade and
  source-side launchers.
- **Existing exact tests:**
  `tests/test_init_contract.py::test_git_runtime_scrubs_ambient_config_and_disables_fsmonitor`;
  `tests/test_task_contract_recovery.py::test_pinned_git_runner_disables_repository_hooks`,
  `::test_target_git_configuration_rejects_execution_and_transport_overrides`;
  `tests/test_automation_upgrade.py::test_ambient_git_execution_and_identity_overrides_are_scrubbed`,
  `::test_git_hooks_cannot_expand_maintenance_commit_scope`;
  `tests/test_worktree_environment.py::test_python_environment_rebinds_to_current_worktree`.
- **Existing coverage:** **covered** for the enumerated ambient `GIT_*`, hooks,
  identity, fsmonitor, and Python environment surfaces; this is not a universal
  claim about every host launcher.
- **Current/legacy coupling:** trust-boundary setup is repeated across runtime,
  Git, recovery, and Admin pathways.
- **Later migration:** #142 isolates Admin; #146 owns a trusted Admin launcher;
  #192 scopes runtime prerequisites to operations that need them.

### 9. Cancellation and terminal authority

- **Current owner/surface:** lifecycle transition table, resume eligibility,
  publication/integration guards, and cleanup receipts.
- **Existing exact tests:**
  `tests/test_task_contract.py::test_resume_check_rejects_initialized_terminal_and_identity_mismatch`
  (terminal statuses; despite its name, this does not vary identity fields);
  `tests/test_task_lifecycle.py::test_generic_state_set_cannot_cross_publication_boundaries`,
  `::test_generic_state_set_cannot_merge_integration_pending_task`;
  `tests/test_post_merge_finalization.py::test_cancelled_missing_upstream_with_unpublished_commit_is_rejected`,
  `::test_cancelled_pristine_receipt_retry_uses_base_revision_fallback`.
- **Existing coverage:** **partial** — cancellation is non-resumable and cleanup
  is guarded, but a direct terminal no-reopen assertion was absent.
- **Current/legacy coupling:** terminal disposition is encoded as a Task State
  label alongside orchestration progress and controls cleanup eligibility.
- **Later migration:** #191 retains only explicit human terminal disposition;
  #198 keeps destructive cleanup authority operation-specific.

### 10. Global initialization and optional-capability coupling

- **Current owner/surface:** mandatory `initialize` Skill, Agent Core doctor/
  context, Project Adapter doctor, and Adapter operation wrappers.
- **Existing exact tests:**
  `tests/test_init_contract.py::test_task_workflows_reuse_initialize_skill`,
  `::test_initialize_skill_preserves_full_init_and_defines_plan_handoff`,
  `::test_adapter_fragment_is_part_of_init_contract`;
  `tests/test_typescript_node_adapter.py::test_missing_capability_is_skipped_and_failure_propagates`.
- **Existing coverage:** **partial** — the global `project::doctor` prerequisite
  and Adapter-level absent-operation skip are explicit, but there is no
  end-to-end test isolating a missing optional capability from unrelated local
  work.
- **Current/legacy coupling:** execution-capable initialization requires
  `just project::doctor` and stops on failure even when a later requested action
  might not need that capability.
- **Later migration:** #192 replaces the global readiness gate with
  operation-specific prerequisite evaluation.

### 11. Templates distribution and generated parity

- **Current owner/surface:** canonical `components/` sources, generated
  `templates/` trees, distribution allowlist/materializer, and CI.
- **Existing exact tests:**
  `tests/test_template_distribution.py::test_manifest_is_complete_and_allowlisted`,
  `::test_materialize_is_exact_and_removes_stale_files`,
  `::test_check_detects_drift`;
  `tests/test_init_contract.py::test_generated_init_files_match_sources`;
  `tests/test_maintenance_lifecycle.py::test_generated_maintenance_surface_matches_canonical`;
  `tests/test_task_state_recovery.py::test_generated_recovery_files_match_canonical_source`.
- **Existing coverage:** **partial** — generated source parity is broad for
  selected surfaces; exact full materialization is executed for the Python
  distribution fixture rather than every published template.
- **Current/legacy coupling:** changes must preserve canonical-source/generated
  parity and the fixed GitHub template allowlist.
- **Later migration:** #141 owns the compatibility/distribution matrix; #197
  owns legacy surface retirement.

### 12. Historical migration fixtures

- **Current owner/surface:** recovery and publication-recovery tests, historical
  Git objects, and fixture documentation.
- **Existing exact tests:**
  `tests/test_task_state_recovery.py::test_production_shape_proves_original_base_when_main_and_pr_base_differ`,
  `::test_real_registered_fixture_recovers_and_resumes_without_product_mutation`,
  `::test_recovery_and_resume_succeed_when_registered_main_checkout_is_stale`,
  `::test_retry_refreshes_default_and_pr_base_observations_after_main_advances`,
  `::test_pull_request_identity_failures_reject_before_recovery`;
  `tests/test_source_publication_recovery_bridge.py::test_blocked_akv_shaped_stale_draft_recovers_same_pr_and_preserves_subject`.
- **Existing coverage:** **partial** — the core #163 stranded-Task shape is in
  tests; provenance across #181/#183/#203/#206 and the old-consumer command
  surface was not summarized as a durable migration fixture.
- **Current/legacy coupling:** recovery depends on the surviving Git branch,
  registered worktree, exact external PR, current Issue, compatibility source,
  and zero-authority Task State topology even when ignored Task State is gone.
- **Later migration:** #147 defines the supported legacy-consumer/EOL boundary;
  #191/#197 import and retire legacy state without treating these fixtures as
  new architecture.

## Preserved historical recovery fixture

The #163 / PR #176 incident remains a migration input, not a path or recipe
contract:

```text
Task:                 163
PR:                   176 (OPEN Draft)
branch:               task/163-worktree-dispatch
Task HEAD:            42d0b0216fb2b338d3973484af7198cc77e52abb
historical Task Base: f9a9ba13e2366e21703847f9edf411e1cb2052a2
current default:      271cfe06d2410f6616e74a465c50a827fe42d319
observed PR base OID: f9a9ba13e2366e21703847f9edf411e1cb2052a2
```

The historical Task Base is not the current default. The PR base OID is a
captured PR fact, not current-default authority. Recovery preserves the exact
registered Git identity and PR selection, leaves tracked Task content/HEAD
unchanged, and reconstructs no verification/review PASS. The existing tests
also exercise refresh of the PR-base observation when the default branch moves.

The #163 Task tree predates the current source-side recovery entry point and
does not necessarily contain the current Justfile command surface. This is an
observed stranded-consumer condition only: no exact temporary worktree path,
Justfile layout, recipe name, or historical state-label sequence is an
architecture invariant.

| History | Characterization input retained here |
|---|---|
| #163 / PR #176 | Exact Task/branch/HEAD/Base/PR identity; absent ignored Task State can be recovered while Git identity survives. |
| #181 | Source-side recovery of only conservative Task authority; no fabricated verification/review evidence or product mutation. |
| #183 | Actual REST PR identity shape, exact same-repository PR selection, and fail-closed mismatch cases. |
| #203 | Historical Task Base, current default, and observed PR base are distinct evidence roles; stale registered main is not current-default authority. |
| #206 | Recovery validates those separate roles and retries from immutable observations; this is characterization input, not a request to retain the legacy recovery architecture. |

## #190 additions

Only the pre-inventory gaps were extended:

- `tests/test_task_state_recovery.py::test_zero_authority_initial_topologies_recover_identically`
  now asserts exact Task/repository/branch/worktree/HEAD/PR output identity,
  preserved branch ref and worktree registry, unchanged Task content, and
  unchanged exact PR identity while missing ignored Task State is reconstructed.
  It also asserts that no verification or Work Unit PASS history appears.
- `::test_production_shape_proves_original_base_when_main_and_pr_base_differ`
  pins the actual #163 Task HEAD/Base/default OID values in a production-shaped
  parent/unique-merge-base fixture, verifies the Git arguments, and keeps the
  PR-base observation separate from current-default authority (the mocked Git
  response does not claim those objects are available in every clone);
  `::test_receipt_refresh_uses_github_pr_base_oid_without_git_ancestry`
  asserts that the recorded PR base equals the PR observation and remains
  distinct from current-default authority.
- `tests/test_post_merge_finalization.py::test_pr_number_identity_must_remain_exact_during_revalidation`
  injects a PR-number replacement between reads and asserts finalization leaves
  Task State byte-identical and still `integration-pending`.
- `tests/test_task_lifecycle.py::test_cancelled_task_is_terminal_and_cannot_be_reopened`
  records the current allowed cancellation transition and exact no-mutation
  rejection of a later attempt to reopen the terminal Task.

No production files or authority semantics are intentionally changed. The
remaining **partial** classifications above are deliberate boundaries for the
later listed migration Issues (for example, no general process-kill durability
claim and no end-to-end proof that unused optional capabilities cannot block
local work before #192).
