# Agent-ready TypeScript Node adapter

A production-oriented TypeScript project scaffold for Node 22 and npm. The
adapter deliberately keeps package identity in the npm lockfile and never
installs dependencies or changes the system package-manager environment.
`project::doctor` is read-only. Other checks execute only the fixed,
repository-owned script names `format:check`, `lint`, `typecheck`, `test`, and
`build`, with implicit npm pre/post lifecycle hooks disabled.

`package.json.engines.node` is the sole Node-version authority. The adapter
accepts only a bounded range subset: whitespace-separated comparisons, exact
or partial versions, `x`/`X`/`*` wildcards, and caret/tilde ranges. Numeric
components are canonical decimal values of at most six digits; prereleases,
unions, hyphen ranges, tags, and any other unsupported syntax fail closed.
When `packageManager` is present it must be a canonical `npm@major[.minor[.patch]]`
value and is matched deterministically against npm's full runtime version.
