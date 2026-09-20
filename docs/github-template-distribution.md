# GitHub Template Repository distribution

`upiscium/Templates` is the only source of truth. Published GitHub Template Repositories are generated distribution artifacts and must not be edited directly.

## Channels

| Intent | Channel |
|---|---|
| Create a new repository on GitHub | GitHub Template Repository |
| Initialize a local directory | Nix flake template |
| Make an existing repository Agent-ready | adoption workflow |
| Update an existing Agent-ready repository | Agent Core upgrade workflow |

Published targets are defined only in `distribution/template-repositories.json`:

```text
agent-python     -> upiscium/Template-Agent-Python
agent-rust       -> upiscium/Template-Agent-Rust
agent-nix        -> upiscium/Template-Agent-Nix
agent-cpp-cmake  -> upiscium/Template-Agent-Cpp-CMake
agent-typescript-node -> upiscium/Template-Agent-TypeScript-Node
```

`agent-base` is intentionally not published as a GitHub Template Repository. It is the minimum/fallback contract used for local Nix initialization and repository adoption; GitHub new-repository users should select a concrete language/toolchain template.

## One-time repository setup

The distribution repositories are external artifacts, so their initial creation is an explicit operator action. Do not grant the recurring publisher repository-administration permission just to automate this one-time step.

Create the five public repositories with an initial branch, using the descriptions from `distribution/template-repositories.json`. For example:

```sh
gh repo create upiscium/Template-Agent-Python \
  --public --add-readme --disable-issues --disable-wiki \
  --description 'Generated Agent-ready Python + uv template. Source: upiscium/Templates; do not edit directly.'
```

Repeat for Rust, Nix, C++/CMake, and TypeScript/Node. Confirm every repository has `main` as its default branch before enabling publication. If the account's new-repository default differs, rename the initial branch to `main` through GitHub before continuing.

Enable each repository as a Template Repository once:

```sh
gh repo edit upiscium/Template-Agent-Python --template
gh repo edit upiscium/Template-Agent-Rust --template
gh repo edit upiscium/Template-Agent-Nix --template
gh repo edit upiscium/Template-Agent-Cpp-CMake --template
gh repo edit upiscium/Template-Agent-TypeScript-Node --template
```

Verify the settings:

```sh
for repo in \
  Template-Agent-Python \
  Template-Agent-Rust \
  Template-Agent-Nix \
  Template-Agent-Cpp-CMake \
  Template-Agent-TypeScript-Node
do
  gh api "repos/upiscium/$repo" --jq '{full_name,default_branch,is_template,description}'
done
```

Expected for each target:

```text
default_branch = main
is_template = true
```

## Publisher credential

Create a fine-grained personal access token scoped to exactly these five distribution repositories. The recurring publisher requires only repository **Contents: Read and write**. Do not grant Administration permission to this token.

Store it only as an Actions secret on `upiscium/Templates`:

```sh
gh secret set TEMPLATE_PUBLISH_TOKEN --repo upiscium/Templates
```

Publication is disabled unless the repository variable is explicitly enabled:

```sh
gh variable set TEMPLATE_PUBLISH_ENABLED \
  --repo upiscium/Templates \
  --body true
```

To stop publication without deleting the credential:

```sh
gh variable set TEMPLATE_PUBLISH_ENABLED \
  --repo upiscium/Templates \
  --body false
```

## Publication contract

`.github/workflows/publish-template-repositories.yml` runs only after `Template CI` completes successfully for `main`.

The workflow:

1. checks out the exact `workflow_run.head_sha` that passed Template CI;
2. verifies the distribution manifest;
3. verifies that the successful SHA is still the current `main` HEAD, preventing a stale successful run from rolling distribution repositories backwards;
4. derives the target matrix from the fixed manifest allowlist;
5. checks out each target using `TEMPLATE_PUBLISH_TOKEN`;
6. records whether the target drifted from the expected generated snapshot;
7. replaces every tracked/worktree file except `.git` with the exact `templates/agent-*` snapshot;
8. verifies content, dotfiles, symlinks, stale-file deletion, and executable mode equivalence;
9. commits and pushes only when the snapshot changed.

There is intentionally no `workflow_dispatch` publication path and no local `distribution-publish` Just recipe. Arbitrary input must not select a publish repository.

## Local verification API

Read-only manifest verification:

```sh
just template::distribution-verify
just template::distribution-matrix
```

To inspect a local checkout of an allowlisted distribution repository:

```sh
just template::distribution-check agent-python /path/to/Template-Agent-Python
```

To reproduce the exact synchronizer against a disposable/local checkout:

```sh
just template::distribution-materialize agent-python /path/to/disposable-checkout
just template::distribution-check agent-python /path/to/disposable-checkout
```

`distribution-materialize` deletes every destination file except `.git`; use it only on a disposable or generated distribution checkout.

## User workflow

### GitHub UI

Use **Use this template** on the corresponding distribution repository, then clone the newly created repository and run:

```sh
nix develop --command just project::bootstrap
opencode
```

Use `/init` after bootstrap for normal read-only session validation.

### GitHub CLI

Example:

```sh
gh repo create my-project \
  --template upiscium/Template-Agent-Python \
  --private --clone
cd my-project
nix develop --command just project::bootstrap
opencode
```

### Nix flake template

The existing local path remains supported independently:

```sh
mkdir my-project
cd my-project
nix flake init -t github:upiscium/Templates#agent-python
nix develop --command just project::bootstrap
```

## Ownership and failure policy

- Changes belong in `components/agent-core/**` or `components/adapters/**`, followed by normal rendering and review in `upiscium/Templates`.
- Never fix a distribution repository directly.
- A distribution drift is evidence of manual/out-of-band mutation; the next verified publication replaces it from the source of truth.
- Publish failures must remain visible as failed Actions jobs.
- Do not force-push distribution branches.
- User-created repositories are independent and are never automatically rewritten by this publisher.
