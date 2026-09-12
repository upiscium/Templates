root := justfile_directory()
script := root / ".automation/bin/agent_core.py"
lifecycle := root / ".automation/bin/task_lifecycle.py"
discard_pristine := root / ".automation/bin/discard_pristine.py"
init := root / ".automation/bin/init_context.py"

[working-directory: root]
preflight:
    python3 {{quote(init)}} preflight

[working-directory: root]
doctor:
    python3 {{quote(init)}} doctor

[working-directory: root]
context:
    python3 {{quote(init)}} context

[working-directory: root]
task-start task slug:
    python3 {{quote(lifecycle)}} start {{quote(task)}} {{quote(slug)}}

[working-directory: root]
task-start-from-issue issue slug:
    python3 {{quote(lifecycle)}} start-from-issue {{quote(issue)}} {{quote(slug)}}

[working-directory: root]
contract-check task='':
    python3 {{quote(lifecycle)}} contract-check {{if task != '' { quote(task) } else { '' }}}

[working-directory: root]
contract-resume-check task='':
    python3 {{quote(lifecycle)}} contract-resume-check {{if task != '' { quote(task) } else { '' }}}

[working-directory: root]
status task:
    python3 {{quote(lifecycle)}} status {{quote(task)}}

[working-directory: root]
state-set task status:
    python3 {{quote(lifecycle)}} state-set {{quote(task)}} {{quote(status)}}

[working-directory: root]
batch-plan +tasks:
    python3 {{quote(lifecycle)}} batch-plan {{tasks}}

[working-directory: root]
work-unit-register task work_unit role objective:
    python3 {{quote(lifecycle)}} work-unit-register {{quote(task)}} {{quote(work_unit)}} {{quote(role)}} {{quote(objective)}}

[working-directory: root]
work-unit-next task:
    python3 {{quote(lifecycle)}} work-unit-next {{quote(task)}}

[working-directory: root]
work-unit-create task role objective:
    python3 {{quote(lifecycle)}} work-unit-create {{quote(task)}} {{quote(role)}} {{quote(objective)}}

[working-directory: root]
work-unit-dispatch-check task work_unit role objective:
    python3 {{quote(lifecycle)}} work-unit-dispatch-check {{quote(task)}} {{quote(work_unit)}} {{quote(role)}} {{quote(objective)}}

[working-directory: root]
work-unit-status task work_unit:
    python3 {{quote(lifecycle)}} work-unit-status {{quote(task)}} {{quote(work_unit)}}

[working-directory: root]
work-unit-state-set task work_unit status evidence provider='' model='' error='':
    python3 {{quote(lifecycle)}} work-unit-state-set {{quote(task)}} {{quote(work_unit)}} {{quote(status)}} {{quote(evidence)}} {{if provider != '' { '--provider ' + quote(provider) } else { '' }}} {{if model != '' { '--model ' + quote(model) } else { '' }}} {{if error != '' { '--error ' + quote(error) } else { '' }}}

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
pr-prepare task:
    python3 {{quote(script)}} agent pr-prepare {{quote(task)}}

[working-directory: root]
pr-edit task:
    python3 {{quote(script)}} agent pr-edit {{quote(task)}}

[working-directory: root]
pr-ready task:
    python3 {{quote(script)}} agent pr-ready {{quote(task)}}

[working-directory: root]
discard-pristine task:
    python3 {{quote(discard_pristine)}} {{quote(task)}}

[working-directory: root]
cleanup task:
    python3 {{quote(lifecycle)}} cleanup {{quote(task)}}
