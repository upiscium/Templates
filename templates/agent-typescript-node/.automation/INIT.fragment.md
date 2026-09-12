## TypeScript Node Project Adapter initialization

- `just project::bootstrap [name]` reuses Agent Core `project_bootstrap`; it does not install packages or rewrite an existing package manifest, lockfile, foreign lockfile, flake, CI configuration, or repository scripts.
- The npm package-lock-only baseline is repository-owned. `package.json` and `package-lock.json` are required, and pnpm, Yarn, and Bun lockfiles are rejected rather than guessed around.
- `just project::doctor` is read-only. It validates npm metadata, the `npm` package manager declaration, Node's `engines.node` range, and the required Node/npm executables.
- The adapter uses only the fixed conventional scripts `format:check`, `lint`, `typecheck`, `test`, and `build`; absent scripts report `SKIPPED`. These checks execute trusted repository-owned code, while implicit npm pre/post lifecycle hooks are disabled.
- Node version authority is `package.json` `engines.node`. Unsupported or malformed ranges fail closed; no package installation or environment mutation occurs during verification.
