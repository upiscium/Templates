root := justfile_directory()
script := root / ".automation/bin/agent_core.py"

[working-directory: root]
doctor:
    python3 {{quote(script)}} agent doctor

[working-directory: root]
context:
    python3 {{quote(script)}} agent context

[working-directory: root]
task-start task slug:
    python3 {{quote(script)}} agent task-start {{quote(task)}} {{quote(slug)}}

[working-directory: root]
status task:
    python3 {{quote(script)}} agent status {{quote(task)}}

[working-directory: root]
verify task:
    python3 {{quote(script)}} agent verify {{quote(task)}}

[working-directory: root]
commit task message='':
    python3 {{quote(script)}} agent commit {{quote(task)}} {{quote(message)}}

[working-directory: root]
push task:
    python3 {{quote(script)}} agent push {{quote(task)}}

[working-directory: root]
pr-create task:
    python3 {{quote(script)}} agent pr-create {{quote(task)}}

[working-directory: root]
pr-edit task:
    python3 {{quote(script)}} agent pr-edit {{quote(task)}}

[working-directory: root]
pr-ready task:
    python3 {{quote(script)}} agent pr-ready {{quote(task)}}

[working-directory: root]
cleanup task:
    python3 {{quote(script)}} agent cleanup {{quote(task)}}
