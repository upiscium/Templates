root := justfile_directory()
tool := root / "tools/source_collaboration.py"
set shell := ["/bin/sh", "-cu"]

[working-directory: root]
publication-check issue:
    python3 -I {{quote(tool)}} publication-check {{quote(issue)}}

[working-directory: root]
commit issue scope_digest message:
    python3 -I {{quote(tool)}} commit {{quote(issue)}} {{quote(scope_digest)}} {{quote(message)}}

[working-directory: root]
push issue expected_head:
    python3 -I {{quote(tool)}} push {{quote(issue)}} {{quote(expected_head)}}

[working-directory: root]
pr-create issue:
    python3 -I {{quote(tool)}} pr-create {{quote(issue)}}

[working-directory: root]
checkpoint issue pr expected_head body:
    python3 -I {{quote(tool)}} checkpoint {{quote(issue)}} {{quote(pr)}} {{quote(expected_head)}} {{quote(body)}}
