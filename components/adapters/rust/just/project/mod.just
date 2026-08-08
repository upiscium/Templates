root := justfile_directory()
bootstrap_script := root / ".automation/bin/project_bootstrap.py"

mod rust 'rust.just'
mod? repository 'repository.just'

[working-directory: root]
bootstrap name='':
    python3 {{quote(bootstrap_script)}} {{quote(name)}}

[working-directory: root]
doctor:
    @command -v cargo >/dev/null
    @command -v rustc >/dev/null
    @command -v rustfmt >/dev/null
    @cargo clippy --version >/dev/null
    @echo "Rust Project Adapter doctor: PASS"

[working-directory: root]
format-check:
    cargo fmt --all -- --check

[working-directory: root]
lint:
    cargo clippy --all-targets --all-features -- -D warnings

[working-directory: root]
test:
    cargo test --all-targets --all-features

[working-directory: root]
build:
    cargo build --all-targets --all-features

[working-directory: root]
check: doctor format-check lint test build
    @echo "Rust Project Adapter check: PASS"
