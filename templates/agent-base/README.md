# Agent-ready base adapter

This is the minimal Project Adapter used to validate the template composition
pipeline. It intentionally contains no language-specific build, lint, or test
commands.

`agent-base` is also intentionally bootstrap-free. It does not provide a Nix
devShell and does not expose `project::bootstrap`, because the base adapter owns
no project-name placeholders, lockfiles, dependency installation, or other
project state that must be materialized after template instantiation.

After `nix flake init -t github:upiscium/Templates#agent-base`, do not run
`nix develop --command just project::bootstrap`. Use the surrounding repository
or host environment to provide the Agent Core runtime tools, then run the normal
read-only initialization checks. For a new standalone language/toolchain project,
prefer the corresponding concrete adapter template instead.

Language adapters are added independently while preserving the shared Agent
Core component and stable composition rules.
