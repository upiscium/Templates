root := justfile_directory()
bootstrap_script := root / ".automation/bin/project_bootstrap.py"

mod python 'python.just'
mod? repository 'repository.just'

[working-directory: root]
bootstrap name='':
    python3 {{quote(bootstrap_script)}} {{quote(name)}}

[working-directory: root]
doctor:
    @command -v uv >/dev/null
    @command -v ruff >/dev/null
    @command -v mypy >/dev/null
    @command -v pytest >/dev/null
    @test "${UV_PROJECT_ENVIRONMENT:-}" = "$PWD/.venv"
    @test "${PIP_REQUIRE_VIRTUALENV:-}" = "1"
    @echo "Python Project Adapter doctor: PASS"

[working-directory: root]
format-check:
    uv run ruff format --check src tests

[working-directory: root]
lint:
    uv run ruff check src tests
    uv run mypy src tests

[working-directory: root]
test:
    uv run pytest -v

[working-directory: root]
build:
    @echo "Python Project Adapter build: SKIPPED (no build artifact contract configured)"

[working-directory: root]
check: doctor format-check lint test build
    @echo "Python Project Adapter check: PASS"
