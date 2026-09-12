# Agent-ready TypeScript Node adapter

A production-oriented TypeScript project scaffold for Node 22 and npm. The
adapter deliberately keeps package identity in the npm lockfile and never
installs dependencies or changes the system package-manager environment.
`project::doctor` is read-only. Other checks execute only the fixed,
repository-owned script names `format:check`, `lint`, `typecheck`, `test`, and
`build`, with implicit npm pre/post lifecycle hooks disabled.
