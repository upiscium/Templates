# OpenCode policy ownership

Status: accepted design; dependency removal is tracked in #221 / #224 and
optional environment model consumption in #222. This document does not claim
that the optional model path has already been implemented or runtime-tested.

## Decision

Templates owns the policy and verification needed for AgentCore's minimum
correct operation. A generated repository must not need dotnix, OpencodeContract,
or a replacement shared policy repository to establish its own authority,
permissions, result requirements, or guarded-operation safety.

There is no shared canonical role/model/permission registry between these
independent configuration owners, now or as a planned future extension.
Coincidentally identical configuration is independently owned, not synchronized.

This does not prohibit functional dependencies. OpenCode, Git, Nix, model
providers, and AgentCore distribution/adoption are examples of functional
interfaces. Consuming functionality does not transfer the receiving repository's
policy ownership to its provider. Generating or upgrading AgentCore from
Templates is product distribution, not a dependency on a user's Global policy.

## Responsibility boundary

Templates owns AgentCore role purpose, prompts, delegation, permissions, result
and evidence requirements, guarded Git/GitHub and filesystem operations, and
repository-local verification. Task identity, worktree confinement, unpublished
work preservation, and cleanup preconditions belong here when required by the
AgentCore implementation; they do not belong in a Global configuration package.

The execution environment owns its provider registration, endpoint addresses,
credentials, concrete environment-selected model identifiers, accelerator
inventory, and optional agents. Templates must not import dotnix, read its
private manifest, copy its Local model registry, or require its agent names.

OpencodeContract assets are not bulk-copied into Templates. Retain only useful
AgentCore-local behavior and test cases. Delete the external policy input,
contract updater, shared conformance manifest, and cross-consumer comparisons.

## Self-sufficient baseline

The baseline is defined relative to explicit functional prerequisites: a
supported OpenCode runtime, required development tools, and a usable configured
provider/session model with the necessary credentials and capabilities.
Self-sufficiency does not promise inference with no usable model or credentials.
Missing required prerequisites must produce a precise diagnostic.

With those prerequisites satisfied, absence of dotnix, a Global agent/model
override, or a Local inference service must not prevent the repository-owned
baseline from being selected. Optional capability absence must never remove a
required review, verification, evidence, or approval step.

AgentCore's existing explicit model assignments and failure behavior remain
unchanged by the OpencodeContract dependency-removal PR. Optional model selection
is a separate, reviewed change under #222.

## Optional environment capabilities

A repository may explicitly choose to use a compatible environment-provided
model or agent. It may also decline that capability. The consuming repository
continues to own the purpose, permitted scope, evidence acceptance, and safety
requirements of the invocation.

Availability in an agent registry proves discoverability, not model reachability,
quality, read-only authority, or permission confinement. A naming prefix, a
hidden flag, or an advisory prompt is not a security boundary. Do not add an
unconditional `local-*` delegation allowlist as a substitute for admission checks.

Same-name environment model inheritance and separately named environment agents
are different mechanisms. #222 currently proposes model-only consumption for
selected repository-owned roles. Its implementation must demonstrate the actual
merge and invocation behavior of the supported runtime before enabling it;
merely deleting model literals is not sufficient evidence.

Templates must not acquire Local provider names, addresses, GPU information, or
concrete Local model IDs to implement this selection. Test fixtures should use
synthetic provider/model identifiers, not a private deployment.

## Selection and failure behavior

Select and record the effective role and model before a bounded invocation.
An absent optional assignment may lead to the documented repository/session
baseline before work starts. That is not permission to retry a failed invocation
under an arbitrary different model.

A configured optional model that times out, is unavailable, lacks a required
tool, or returns invalid evidence is distinct from an absent assignment. Preserve
the failure, never report it as successful evidence, and apply the explicit
repository-owned failure/recovery policy. Do not silently introduce automatic
model fallback or relax existing quality gates during this migration.

For an advisory attempt, a separately authorized baseline attempt may be a valid
future policy, but must have an explicit owner and bounded retry semantics. It is
not implicitly authorized by this design decision.

## Required evidence for optional consumption

Use the exact supported OpenCode version/revision, isolated Global and project
configuration directories, and deterministic mock providers where practical.
Record the resolved agent, selected model, and effective permissions. Verify:

- no Global configuration and no optional assignment: the declared baseline works;
- a compatible environment model exists: the intended role uses it without Local
  deployment information in Templates;
- Global and project define the same role with conflicting prompts, mode, task
  permissions, read/edit rules, or extra tool rules: repository safety is not
  weakened by the effective merged configuration;
- an environment agent exists but is not admitted: it cannot gain authority by
  being discoverable, hidden, or conveniently named;
- the configured optional provider is unavailable or returns invalid evidence:
  no unauthorized model retry, skipped gate, or false completion occurs;
- generated templates behave consistently with the AgentCore source.

A local permission rule is not assumed to be a universal deny ceiling over every
agent-specific override. Verify the effective runtime stack. If safe confinement
cannot be established with the supported configuration interface, do not enable
that optional consumption path; keep the baseline and address the gap locally.

A hostile executable, credential-stealing plugin, or administrator-controlled
process is outside a configuration-only isolation claim. Supporting declared
configuration layers does not mean promising safety against arbitrary privileged
code in the deployment environment.

## Migration completion

Dependency retirement and optional capability support are independent. #221 can
finish without #222. Before claiming OpencodeContract removal complete, update
the active README and CI instructions, remove live dependency/updater references,
retain local safety tests, verify generated output, and record exact-revision
results. Older deployed repositories are upgraded through their normal explicit
maintenance path; this PR does not silently mutate their installations.
