# Mutable Just recipes cannot authenticate themselves. Do not route publication
# through this worktree: invoke the approved store-installed templates-source.
publication-check issue:
    @printf '%s\n' 'source::* is not a publication authority; use the approved installed templates-source' >&2; exit 2

commit issue scope_digest message:
    @printf '%s\n' 'source::* is not a publication authority; use the approved installed templates-source' >&2; exit 2

push issue expected_head:
    @printf '%s\n' 'source::* is not a publication authority; use the approved installed templates-source' >&2; exit 2

pr-create issue:
    @printf '%s\n' 'source::* is not a publication authority; use the approved installed templates-source' >&2; exit 2

checkpoint issue pr expected_head body:
    @printf '%s\n' 'source::* is not a publication authority; use the approved installed templates-source' >&2; exit 2
