# Guarded Templates source collaboration

Templates source-development worktrees use a dedicated root collaboration surface.
This exists so AgentCore can be developed without fabricating consumer Task State or
reusing legacy maintenance/publication lifecycle authority.

The stable commands are:

```sh
just source::publication-check <issue>
just source::commit <issue> <scope-digest> "<message>"
just source::push <issue> <expected-head>
just source::pr-create <issue>
just source::checkpoint <issue> <pr> <expected-head> "<body>"
```

## Authority boundary

The commands are for the canonical `upiscium/Templates` repository only.

They require:

- a named non-default branch;
- the Issue number to be present as a numeric token in the branch name;
- an open same-repository GitHub Issue;
- the canonical `origin`;
- no pre-staged index before scope capture/commit;
- no secret-like, symlink, or non-regular changed path;
- no force push;
- exact local/remote/PR identity checks.

Direct default-branch publication, merge, rebase, amend, force push, arbitrary GitHub
mutation, and destructive cleanup are outside this surface.

## Scope digest

`source::publication-check` deterministically describes the complete current source
change set and prints a SHA-256 `scope_digest`.

The digest binds:

- repository;
- Issue;
- source branch;
- current pre-commit HEAD;
- every changed/untracked path;
- deletion state or file mode/size/content digest.

Review the returned path inventory before committing. `source::commit` recomputes the
manifest and refuses to stage anything if the digest changed.

The commit path also runs:

```sh
git diff --check
just template::check
git diff --cached --check
```

and stages only the paths bound by the manifest.

## Push and Draft PR

`source::push` accepts an exact expected HEAD. It is idempotent when the remote branch
already equals that HEAD, and otherwise permits only a normal fast-forward branch push.

`source::pr-create` requires the remote branch to equal local HEAD. It creates a
same-repository Draft PR against `main`, or adopts the one exact existing Draft PR for
that branch. It does not mark the PR Ready or merge it.

## Checkpoints

`source::checkpoint` verifies the exact open PR, branch, repository, base, remote HEAD,
and local HEAD before posting.

The command adds a deterministic hidden marker to the comment. Retrying the same exact
checkpoint is idempotent instead of duplicating the comment.

Checkpoint text is durable collaboration context, not private chain-of-thought or raw
tool logs.

## Bootstrap

The first landing of this capability is the one explicit maintainer-controlled
bootstrap permitted by Templates Issue #215. After that landing, ordinary Templates
source-development publication should use this guarded surface rather than raw
`git add`, `git commit`, `git push`, or `gh pr create`.

This bootstrap does not authorize publication or reuse of stale pre-v4 AgentCore
implementation work.
