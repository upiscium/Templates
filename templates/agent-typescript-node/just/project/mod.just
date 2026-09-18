root := justfile_directory()
bootstrap_script := root / ".automation/bin/project_bootstrap.py"
helper := root / "just/project/node.py"

mod? repository 'repository.just'

[working-directory: root]
bootstrap name='':
    python3 {{quote(bootstrap_script)}} {{quote(name)}}

[working-directory: root]
doctor:
    python3 {{quote(helper)}} doctor

[working-directory: root]
format-check:
    python3 {{quote(helper)}} format-check

[working-directory: root]
lint:
    python3 {{quote(helper)}} lint

[working-directory: root]
typecheck:
    python3 {{quote(helper)}} typecheck

[working-directory: root]
test:
    python3 {{quote(helper)}} test

[working-directory: root]
build:
    python3 {{quote(helper)}} build

[working-directory: root]
check:
    python3 {{quote(helper)}} check
