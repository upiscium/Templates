# Guarded Templates source collaboration

Templates source-development worktrees use a source-publication authority installed
**outside** the mutable Task worktree. This exists so AgentCore can be developed
without fabricating consumer Task State or reusing legacy maintenance/publication
lifecycle authority.

After explicit maintainer/Admin bootstrap review, build the approved **full Git
revision**, not the mutable Task checkout, into the Nix store. For example, with
`<approved-full-revision>` replaced by the independently approved commit ID:

```sh
nix build --no-link --print-out-paths \
  'github:upiscium/Templates/<approved-full-revision>#source-collaboration'
```

Pin the resulting store path and reviewed revision in maintainer-controlled
configuration **outside** the Task worktree. A change to pinned code is an
activation/deployment step, distinct from normal source development. Codex must invoke that exact absolute
`/nix/store/<approved-output>/bin/templates-source` path, with the Task worktree
passed only as data. It must not resolve the authority from the Task branch, a
moving tag, the live `flake.nix`, or an environment variable set by the Task.
The package substitutes its Git revision into the installed Python policy as the
immutable authority-history base. The policy does not read it from a caller
environment variable, even when the Python payload is invoked without its shell
wrapper. An uncommitted `path:.` development build has no approved base and cannot authorize
publication. The worktree must contain that approved commit in its history.
The installed policy also has a store-pinned Python shebang, Git/GitHub CLI paths,
and subprocess `PATH`/Git environment; direct execution does not inherit a
caller-selected `git`, `gh`, SSH command or Git config override. An existing
user-owned Unix `SSH_AUTH_SOCK` is retained for canonical SSH-origin authentication.
The canonical commands (shown with a placeholder for that absolute store path) are:

```sh
<trusted-bin>/templates-source --worktree <task-worktree> publication-check <issue>
<trusted-bin>/templates-source --worktree <task-worktree> commit <issue> <scope-digest> "<message>"
<trusted-bin>/templates-source --worktree <task-worktree> push <issue> <expected-head>
<trusted-bin>/templates-source --worktree <task-worktree> pr-create <issue>
<trusted-bin>/templates-source --worktree <task-worktree> checkpoint <issue> <pr> <expected-head> "<body>"
```

`just source::*` is deliberately **disabled**. Its `Justfile`, module and live Python
tool are worktree-controlled, so even a recipe that delegates to the installed
launcher can be redirected before the launcher runs. Executing a changed recipe
is not an authorized ordinary publication. Upgrading the installed authority
requires independent review, a new approved revision, and a new pinned store path.

## Authority boundary

The commands are for the canonical `upiscium/Templates` repository only. Supported
GitHub SSH/HTTPS origins are matched by case-insensitive owner/repository identity
(including `https://github.com/uPiscium/Templates.git`); other hosts, URL forms,
and repositories are rejected. No shared `origin` configuration is rewritten.

They require:

- the supplied Task root and its Git administrative directory to be the same
  registered Git worktree (a replaced `.git` pointer cannot impersonate another
  Task's branch);
- a named non-default branch;
- the Issue number to be present as a numeric token in the branch name;
- an open same-repository GitHub Issue;
- the canonical `origin`;
- no pre-staged index before scope capture/commit;
- no secret-like, symlink, or non-regular changed path;
- the installed authority revision is a common historical ancestor of the
  tracked `origin/main` and the candidate Task HEAD; new candidate changes
  to the next source Guard version are **data**, never executable policy;
- source authority files touched in the pending change are reported in
  `review_sensitive_paths` for the ordinary independent review process;
- no candidate-controlled Git configuration capable of executing commands;
- no force push;
- exact local/remote/PR identity checks.

Direct default-branch publication, merge, rebase, amend, force push, arbitrary GitHub
mutation, and destructive cleanup are outside this surface.

## Scope digest

`templates-source publication-check` deterministically describes the complete current source
change set and prints a SHA-256 `scope_digest`.

The digest binds:

- repository;
- Issue;
- source branch;
- current pre-commit HEAD;
- every changed/untracked path;
- deletion state or file mode/size/content digest.

Review the returned path inventory before committing. `templates-source commit` recomputes the
manifest and refuses to prepare a commit if the digest changed.

The commit path also runs:

```sh
git diff --check
<installed parity checker> check --root <task-worktree>
git diff --cached --check  # against a private index, not the shared Task index
```

The commit path hashes only reviewed bytes into a **private** index and creates a
commit object with the exact reviewed tree and old HEAD as explicit parent. It
never uses the real Task index as staging scratch: a concurrent same-path staged
blob cannot be silently overwritten. Before updating the ref it acquires the
worktree `index.lock` **then** `HEAD.lock`, checks that the shared index still
equals the clean old HEAD tree, and rechecks the reviewed worktree scope.
`HEAD.lock` alone is insufficient: real `git checkout` can mutate the index and
worktree before failing its final HEAD update. The two-lock order fences Git's
index changes first; any detected worktree change also fails closed. A Git
old-OID compare-and-swap updates **only** the reviewed full Task branch ref.
The ref update uses a detached temporary Git administrative directory sharing
the same object/reference store; it cannot follow an ambient switched `HEAD`.
Only after exact ref and unchanged-index/worktree re-observation does the
prepared index replace the still-clean shared index. A post-CAS index or
worktree conflict reports that the exact commit was applied but **requires
reconciliation**; concurrent product work is retained. Do not reset or
force-update to resolve a reported conflict. An exact committed ref is re-read
even after a lost acknowledgement; an unrelated moved ref is never overwritten.
`index.lock` is Git's cooperative lock: a process that directly rewrites Git's
index file while ignoring its lock is outside the publication protocol. Detected
such changes fail with reconciliation; no check-and-replace sequence can
atomically protect against an uncooperative direct write in between.

The review-sensitive next-version source paths are `Justfile`,
`just/source.just`, `tools/source_collaboration.py`,
`tools/source_publication_launcher.sh`, `just/template.just`,
`tools/render_templates.py`, and `flake.nix`.
These are **not** a publication deny-list: the installed executor can commit
and push their reviewed bytes through the normal guarded Task branch path.
`publication-check` reports affected entries as `review_sensitive_paths`.
A review policy may require extra code/security review for such changes, but
the Guard does not substitute per-file Ask or signatures for that workflow.
The installed parity checker is packaged beside the installed authority and
reads the candidate worktree as data; it never executes candidate Just recipes,
Python modules, Nix derivations, hooks or Git filters.
A missing `origin/main` tracking ref, or a tip not descended from the
installed authority revision, fails closed. Grafts and shallow history are
rejected, and authority ancestry is checked through raw commit-parent records
rather than a mutable Git revision walker. For a merge commit, one actual raw
parent path reaching the installed revision establishes Git ancestry; an older
or unrelated alternative parent does not invalidate it. The merged result is
still subject to the complete source-scope and review checks. Candidate
changes do not activate the next version of the executor.
Reviewed file blobs are rechecked against the reviewed scope digest, not just
path names. Private preparation hashes verified source bytes without Git filters
and inserts exact blob IDs only into the private index; it does not run `git add`
over candidate-controlled attributes or clean-filter configuration. Real-index
bytes staged by ordinary lock-respecting Git writers are preserved, not included
or overwritten.
The launcher ignores global/system Git config without modifying it, constrains
local Git config, and disables hooks, fsmonitor and signing commands. Network
Git operations use a private temporary bare repository with the candidate object
database as an alternate, not the candidate's mutable Git configuration. The Nix
store artifact and its approved revision—not a same-user writable test install—
are the production trust anchor.
**Publishing source is not activating a new authority.** A stable installed
executor can publish the next version of its own source files, while a separate
versioned, reviewed activation selects a new fixed executable after tests.
Activation must not load a mutable Task checkout as trusted code.
The one-time transition from a previously installed version that rejects all
source-authority path changes is documented below; it is not a new permanent
approval ceremony and does not authorize direct/default-branch publication.

## Push and Draft PR

`templates-source push` accepts an exact expected HEAD. It is idempotent when the remote branch
already equals that HEAD, and otherwise permits only a normal fast-forward branch push.
It also requires exactly one canonical GitHub push destination (including any configured
`pushurl`) and sends to that validated URL rather than resolving `origin` again.
Git URL rewrite rules that would re-interpret this validated destination fail closed.

`templates-source pr-create` requires the remote branch to equal local HEAD. It creates a
same-repository Draft PR against `main`, or adopts the one exact existing Draft PR for
that branch. It scans **all** pages of repository PRs across open, closed and
merged states, re-observes before creation and after a create attempt (including
lost acknowledgements), and refuses an incompatible, Human-closed or ambiguous
identity. It does not mark the PR Ready or merge it.

## Checkpoints

`templates-source checkpoint` verifies the exact open PR, branch, repository, base, remote HEAD,
and local HEAD before posting.

The command adds a deterministic hidden marker to the comment. It scans all
comment pages and accepts only an exact comment body bound to the verified PR,
HEAD, repository, and the **numeric GitHub ID** of the currently authenticated
publication principal (not just a copied marker or login string). A participant
copying the marker cannot impersonate that principal. Equivalent concurrent
posts by that principal use the lowest GitHub comment ID as the one canonical
checkpoint; conflicting trusted bodies fail instead of being silently merged.
After posting, including a lost acknowledgement, the principal, PR and full
comment history are re-observed before reporting success. Retrying an exact
checkpoint is idempotent instead of duplicating an authoritative comment.

Checkpoint text is durable collaboration context, not private chain-of-thought or raw
tool logs.

## Bootstrap

The initial `source::*` landing was bootstrapped by Templates Issue #215.
Issue #219 installed the fixed source executor; #220 introduced the earlier
permanent seven-path deny-list. [ADR-0003](adr/0003-self-hosted-development-and-recovery.md)
and Issue #257 replace that restriction with a **stable-executor + self-hostable
source development** model. The previously installed launcher still rejects
its own seven protected source paths, so migration to the revised code requires
**one explicitly bounded, reviewed bootstrap publication**. Do not bypass that
existing installed authority before migration is reviewed and authorized.
Thereafter, ordinary source Tasks, including Guard source updates, use the
installed launcher without new per-Issue exceptions.

Activation of a newer launcher remains distinct from publishing its source:
select an exact built and reviewed revision, verify operational readiness,
and retain the previously working launcher as a recovery option. Neither a new
Task branch nor its `flake.nix` chooses the trusted installed executable.
`just source::*` continues to be inert. Ordinary source Tasks use the
installed launcher rather than mutable recipes or unguarded raw Git writes.

This bootstrap does not authorize publication or reuse of stale pre-v4 AgentCore
implementation work.
