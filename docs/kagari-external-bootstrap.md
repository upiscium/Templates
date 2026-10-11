# KAGARI external bootstrap — non-invasive Phase 1 candidate

> **Status:** development-only implementation candidate for [#256](https://github.com/upiscium/Templates/issues/256). Not the activated KAGARI v4 / KAGARI 1.0.0 runtime. The existing Agent Core VERSION 3, its normal adoption workflow, and the Project's runtime remain unchanged.

## Boundary

KAGARI must be installable, repairable and removable without evaluating the target Project's `flake.nix`, reading/executing the target's `Justfile`, or importing potentially damaged KAGARI scripts. The external bootstrap uses its own Python 3 standard library and Git CLI; it does **not** need Just, Nix or OpenCode in the target. Git is required because #256 targets existing Git repositories.

For this initial implementation, `tools/kagari_bootstrap.py` is launched from a **separately available KUMIKI source checkout**. The selected KAGARI payload must be explicitly provided by the caller. A dedicated, pinned distribution/executable and v3→v4 cutover are subsequent release tasks; this script must not be confused with a production-ready independent host installer.

## Owned data

The sole Project content area touched by the default bootstrap is:

```text
<project>/
  .kagari/
    install.json        # exact version, file paths, SHA-256 and modes
    runtime/
      .automation/...
      .opencode/...
      Justfile
      opencode.json
      AGENTS.md
```

These files are **inside the dedicated KAGARI-owned area**, not merged into Project root. A small cooperative lock is recorded under the target Git worktree's private Git administrative directory. The bootstrap does not stage, commit, push, rewrite branches, or change `flake.nix`, `flake.lock`, root `Justfile`, root `opencode.json`, root `AGENTS.md`, `.github/workflows/**`, or Project source files.

The contents are a staged copy of the current Agent Core v3 payload; merely copying these files **does not make the existing v3 lifecycle commands function against the Project root**. Those commands assume a root-level integration. An external runtime bridge and verified OpenCode configuration precedence are separate, still-open acceptance gates of #256.

## Example (Python/Git preinstalled on the external host)

```sh
python3 /path/to/KUMIKI/tools/kagari_bootstrap.py plan \
  --target /path/to/existing-project \
  --source /path/to/KUMIKI/components/agent-core

python3 /path/to/KUMIKI/tools/kagari_bootstrap.py install \
  --target /path/to/existing-project \
  --source /path/to/KUMIKI/components/agent-core

python3 /path/to/KUMIKI/tools/kagari_bootstrap.py doctor \
  --target /path/to/existing-project

python3 /path/to/KUMIKI/tools/kagari_bootstrap.py repair \
  --target /path/to/existing-project \
  --source /path/to/KUMIKI/components/agent-core

python3 /path/to/KUMIKI/tools/kagari_bootstrap.py uninstall \
  --target /path/to/existing-project \
  --source /path/to/KUMIKI/components/agent-core
```

The source path is **external to the target's installed KAGARI runtime**. The bootstrap does not invoke candidate/target Just recipes, Nix expressions or OpenCode. An invalid Project Flake is not a blocker for the byte-level payload operations. For a Git-backed KAGARI source, only its **tracked component files** are included; ignored OpenCode npm downloads, Python caches and unrelated local artifacts are not copied. An extracted release source without Git uses a bounded directory scan that excludes known generated caches. Repository modifications remain uncommitted until an independently authorized ordinary guarded Task publication.

## Operation outcomes

| Operation | Expected states | Effects |
| --- | --- | --- |
| `plan` / `doctor` | ABSENT, HEALTHY, REPAIRABLE, CONFLICT | Read-only inspection; conflict reports exact missing, altered and unknown paths |
| `install` | INSTALLED, UNCHANGED, REPAIRED, BLOCKED | Create complete KAGARI area only when absent; otherwise reconcile missing owned files |
| `repair` | INSTALLED, UNCHANGED, REPAIRED, BLOCKED | Same bounded reconciliation engine; no forced overwrite |
| `uninstall` | UNINSTALLED, ALREADY_ABSENT, BLOCKED | Require an explicit matching KAGARI source and remove only its inventoried unchanged managed files |

Malformed/missing installation receipt, symlink or special file in owned scope, unrecognized extra file/directory, user-modified installed KAGARI file, source-version change, or unknown ownership makes a mutating operation **BLOCKED**, preserving Project data. **Uninstall requires a caller-supplied external source with the exact same file inventory as the receipt**; a plausible receipt alone is not enough to claim ownership and delete files. This is intentional: a changed file cannot automatically be classified as damage rather than a user edit. A future explicit replace/upgrade flow must bind that choice to a reviewed plan and the actual file preimage; current `repair` automatically restores only **missing** known files.

A fresh install is built under the private Git administrative directory (required to be on the same filesystem) and renamed into the absent `.kagari` path. Missing-file repair similarly stages complete file bytes privately and links only into absent managed slots. The installer removes its own temporary state on ordinary failures; an abrupt process/host interruption can leave a Git-private `kagari-stage-*` or `kagari-repair-*` residue. This does **not** introduce untracked files at Project root, but private residue reclamation and complete crash recovery remain later work.

## Verification limits / next gates

- Test read-only plan and Install → Doctor → Uninstall → Reinstall → Repair in disposable Git repositories with valid/missing/broken Flake and Justfile plus existing OpenCode configuration.
- Verify byte-identical preservation of root Project files, HEAD, Git index and unrelated dirty work.
- Verify unknown directories, modified owned files, symlinks, source-version mismatch and missing installation receipt fail without guessing/overwriting ownership.
- **Still pending:** real runtime execution from a non-root `.kagari/runtime` payload; optional OpenCode / Just / Nix integration and actual provider configuration; explicit replace/upgrade and ambiguous corruption recovery; interrupted-stage recovery; independently packaged external bootstrap; release qualification and v3→v4 rollout.

Related design: [ADR-0002](adr/0002-detachable-kagari-and-project-ownership.md), [Issue #254](https://github.com/upiscium/Templates/issues/254), [Issue #256](https://github.com/upiscium/Templates/issues/256). Neither this prototype nor the older `tools/adopt_repository.py` is proof of production-ready KAGARI attach/detach.
