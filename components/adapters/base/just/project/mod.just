root := justfile_directory()

[working-directory: root]
doctor:
    @echo "Base Project Adapter doctor: PASS"

[working-directory: root]
format-check:
    @echo "Base Project Adapter format-check: SKIPPED"

[working-directory: root]
lint:
    @echo "Base Project Adapter lint: SKIPPED"

[working-directory: root]
test:
    @echo "Base Project Adapter test: SKIPPED"

[working-directory: root]
build:
    @echo "Base Project Adapter build: SKIPPED"

[working-directory: root]
check: doctor format-check lint test build
    @echo "Base Project Adapter check: PASS"
