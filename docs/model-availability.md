# Model availability

Agent Core assigns each configured role one fixed provider model and effort. The configured pair is authoritative for that role.

| Role | Configured model | Effort |
| --- | --- | --- |
| `build` | `openai/gpt-6.1-sol` | `high` |
| `plan` | `openai/gpt-6.1-sol` | `high` |
| `task-orchestrator` | `openai/gpt-6.1-sol` | `high` |
| `maintenance-orchestrator` | `openai/gpt-6.1-sol` | `high` |
| `architect` | `openai/gpt-6.1-sol` | `high` |
| `security-reviewer` | `openai/gpt-6.1-sol` | `high` |
| `reviewer` | `openai/gpt-6-luna` | `high` |
| `investigator` | `openai/gpt-6-luna` | `high` |
| `general` | `openai/gpt-6-luna` | `medium` |
| `explore` | `openai/gpt-6-luna` | `medium` |
| `scout` | `openai/gpt-6-luna` | `medium` |
| `verifier` | `openai/gpt-6-luna` | `medium` |

The design assigns Sol High to orchestration, planning, architecture, and security-critical review; Luna High to correctness and investigation; and Luna Medium to bounded implementation, exploration, research, and verification. This policy does not introduce `max` or `xhigh` effort settings. `verifier` remains at `medium`; `low` is a future evaluation option only, not a current assignment.

Agent Core does not substitute another model, invoke a fallback agent, or retry the same bounded objective under another model when provider execution is unavailable. The affected Task or Work Unit returns `BLOCKED`, preserves relevant evidence, and reports the exact provider/model failure.

This contract preserves role quality and authority; it does not guarantee provider or model availability.
