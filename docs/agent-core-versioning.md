# Agent Core versioning and release identity

This document defines the planned public versioning policy and the identity a
future Agent Core release must bind. It is policy foundation, not a release
record or proof that the release matrix has passed. No stable Agent Core
SemVer release is claimed here. Templates currently distributes Agent Core
with the integer `.automation/VERSION` marker **3**; that marker is not
`3.0.0` and does not mean current Agent Core is SemVer-stable.

## Planned first stable boundary and independent streams

Issue #159 plans the foundation for a first stable Agent Core **1.0.0**,
initially alongside Templates **4.0.0**. Those are independent version
streams: the initial pairing is a planned milestone, not a permanent mapping
between Agent Core and Templates versions. A Templates-only change does not
advance Agent Core, and an Agent Core release version is not derived from the
Templates version.

The zero-base **v4** architecture described by issue #189 is an
internal architecture transition, not public Agent Core SemVer major version
4. It does not imply Agent Core `4.0.0`. After the planned `1.0.0` boundary,
a public incompatible Agent Core change is versioned by the SemVer policy
below, independently of internal architecture numbering.

## Public SemVer policy

Stable Agent Core versions use `MAJOR.MINOR.PATCH`:

* **MAJOR** (`X.0.0`): make a change that is incompatible with a documented
  stable Agent Core contract. This includes removing or changing a documented
  behavior in a way that requires a consumer change. Increment MAJOR and reset
  MINOR and PATCH to zero. Publish migration notes for the breaking transition.
* **MINOR** (`x.Y.0`): add backward-compatible documented behavior or
  capability. Increment MINOR and reset PATCH to zero. Existing documented
  behavior remains available and retains its meaning.
* **PATCH** (`x.y.Z`): make a backward-compatible correction, such as fixing a
  defect or correcting documentation without adding a new public capability
  or changing the documented contract incompatibly. Increment PATCH while
  keeping MAJOR and MINOR unchanged.

For `1.x`, backward compatibility applies to the documented stable Agent Core
contract. It is **not** a promise to support pre-stable implementations,
undocumented internals, or legacy integer markers. In particular, legacy
integer `.automation/VERSION` values **2** and **3** are Admin-only,
one-way migration markers owned by issue #142. They are not versions `2.0.0`
and `3.0.0`, do not map to those SemVer releases, and do not create a general
legacy compatibility promise for stable `1.x`.

There is no capability manifest in this policy or in the planned release
identity. Public compatibility is determined by the documented Agent Core
contract and SemVer rules above.

## Exact release identity

The planned canonical release identity is the root
`distribution/release-identity.json`. It is a planned record, not evidence
that a release exists. A release identity distinguishes these independent
values:

* `agentCoreVersion`: the public Agent Core SemVer claimed by the candidate;
* `agentCorePayload`: the exact Git tree identity of the canonical
  `components/agent-core` subtree in the source revision; and
* `templatesSourceRevision`: the exact immutable Templates source commit
  supplying that subtree.

The subtree tree identity covers the exact Agent Core payload, including file
contents and modes. The source revision is provenance, not a substitute for
the payload tree, and neither is inferred from the Agent Core version. A new
Templates commit may have a new `templatesSourceRevision` while retaining the
same Agent Core payload and version.

At the future v4 cutover, the planned identity must move from `planned` to
`candidate` only for the exact candidate being checked. A `planned` status is
never release-ready; changing it to `candidate` alone does not establish a
release or satisfy release evidence. A published tag points at the immutable
candidate commit: it cannot be retroactively rewritten to say `released`.

The read-only `just agent-core::release-identity-check
[previous-release-source-revision]` checks a **clean, committed** local
Templates HEAD. It reads the tracked version intent, installed Agent Core
marker, and Agent Core subtree from Git objects, and prints the exact identity
with `IDENTITY_VALIDATED` when the checks pass. With no previous revision it
accepts only the first stable `1.0.0` / `4.0.0` pair; for subsequent releases
the optional argument must be the full immutable source commit of the previous
published Templates release. An operator must independently establish that
this is the actual previous published release (including its tag and Release):
an arbitrary commit ID is not publication evidence. The current planned v3
checkout deliberately cannot pass this check, and `IDENTITY_VALIDATED` does
**not** imply CI, downstream dogfood, #141 matrix coverage, a published tag,
or release readiness.

## Required future release checks

A future Agent Core release check must fail closed unless all of the following
hold:

1. The candidate's `agentCoreVersion` exactly matches the Agent Core version
   marker installed in that exact candidate. Issue #142 owns the Admin cutover
   and the one-way handling of the old integer markers; this policy does not
   prescribe an unimplemented marker format or migration command.
2. Its `agentCorePayload` is the exact tree of `components/agent-core` at the
   recorded `templatesSourceRevision`.
3. It is compared with the previous immutable Agent Core release identity:
   the same version with a different payload is rejected, as is a version
   bump with an unchanged payload. When the payload changes, select MAJOR,
   MINOR, or PATCH according to the compatibility impact in the public policy.
4. A Templates-only change with an unchanged Agent Core payload does not cause
   an Agent Core version bump. It may still change the Templates source
   revision and the Templates release stream.
5. Release evidence binds `agentCoreVersion`, `agentCorePayload`, and
   `templatesSourceRevision`, and binds CI and downstream dogfood to the exact
   same candidate (including its immutable source/tree identity). Testing a
   different revision, tree, or installed marker is not candidate evidence.

The local identity check implements the version/payload comparison, but is
not connected to the historical post-merge gate or to a trusted published-tag
lookup. Issue #141 owns the full release matrix and evidence binding; issue
#142 owns the Admin cutover. Merge, tagging, and publication remain separately
human-controlled; no automatic merge, tag, or release is authorized here.
