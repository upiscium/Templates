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
configuration **outside** the Task worktree. Codex must invoke that exact absolute
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

- a named non-default branch;
- the Issue number to be present as a numeric token in the branch name;
- an open same-repository GitHub Issue;
- the canonical `origin`;
- no pre-staged index before scope capture/commit;
- no secret-like, symlink, or non-regular changed path;
- no changes to the source collaboration authority, pending or already committed
  on the branch since the installed launcher’s approved base revision;
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
manifest and refuses to stage anything if the digest changed.

The commit path also runs:

```sh
git diff --check
<installed parity checker> check --root <task-worktree>
git diff --cached --check
```

and stages only the paths bound by the manifest.

The protected bootstrap chain is `Justfile`, `just/source.just`,
`tools/source_collaboration.py`, `tools/source_publication_launcher.sh`,
`just/template.just`, `tools/render_templates.py`, and `flake.nix`.
The installed parity checker is packaged beside the installed authority and
reads the candidate worktree as data; it never executes that worktree's Just
recipes or Python files. A missing `origin/main` tracking ref, or one unrelated
to the installed approved revision, fails closed. Moving that local ref cannot
hide authority-changing commits: history is checked from the installed revision.
Grafts and shallow history are rejected, and authority history is traversed
through raw commit-parent records rather than Git's mutable revision walker.
Staged blobs are rechecked against the reviewed scope
digest before commit, not just staged path names. Staging hashes verified source
bytes without Git filters and inserts exact blob IDs into the index; it does not
run `git add` over candidate-controlled attributes or clean-filter configuration.
The launcher ignores global/system Git config without modifying it, constrains
local Git config, and disables hooks, fsmonitor and signing commands. Network
Git operations use a private temporary bare repository with the candidate object
database as an alternate, not the candidate's mutable Git configuration. The Nix
store artifact and its approved revision—not a same-user writable test install—
are the production trust anchor.
Changing these bootstrap inputs requires a separately reviewed maintainer/Admin
bootstrap; the installed ordinary authority must not publish its own upgrade,
including this repair.

## Push and Draft PR

`templates-source push` accepts an exact expected HEAD. It is idempotent when the remote branch
already equals that HEAD, and otherwise permits only a normal fast-forward branch push.
It also requires exactly one canonical GitHub push destination (including any configured
`pushurl`) and sends to that validated URL rather than resolving `origin` again.
Git URL rewrite rules that would re-interpret this validated destination fail closed.

`templates-source pr-create` requires the remote branch to equal local HEAD. It creates a
same-repository Draft PR against `main`, or adopts the one exact existing Draft PR for
that branch. It does not mark the PR Ready or merge it.

## Checkpoints

`templates-source checkpoint` verifies the exact open PR, branch, repository, base, remote HEAD,
and local HEAD before posting.

The command adds a deterministic hidden marker to the comment. Retrying the same exact
checkpoint is idempotent instead of duplicating the comment.

Checkpoint text is durable collaboration context, not private chain-of-thought or raw
tool logs.

## Bootstrap

The initial `source::*` landing was bootstrapped by Templates Issue #215. Issue #219
changes that authority and likewise requires an explicit, independently reviewed
maintainer/Admin bootstrap: the current live recipes must **not** publish this fix.
Only after #219 lands and the approved revision is pinned outside Task worktrees
should ordinary Templates source publication use the installed launcher instead
of live recipes or raw `git add`, `git commit`, `git push`, or `gh pr create`.

This bootstrap does not authorize publication or reuse of stale pre-v4 AgentCore
implementation work.
