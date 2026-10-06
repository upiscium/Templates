from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, fields
from typing import Any

import test_metadata_ref as fixtures
import task_record as task_record
from metadata_ref import MetadataConflictError, MetadataRefError


_OBJECT_DOMAIN = b"agentcore-metadata-object/v1\n"
_REPOSITORY = "acme/widgets"


class TaskRecordV4Test(unittest.TestCase):
    """Focused #191 coverage, reusing only the local #217 fixture helpers."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip
    _create_remote_child = fixtures.MetadataRefTest._create_remote_child
    _transfer_fixture_commit = fixtures.MetadataRefTest._transfer_fixture_commit
    _set_remote_tip = fixtures.MetadataRefTest._set_remote_tip

    def _product_state(self) -> tuple[bytes, ...]:
        # Observe without write-tree (which can populate the index's TREE
        # extension) or status's optional refresh writes. The observation
        # itself must not invalidate the byte-identical index assertion.
        from pathlib import Path
        index = Path(fixtures.git("rev-parse", "--git-path", "index", cwd=self.product).decode().strip())
        if not index.is_absolute():
            index = self.product / index
        return (
            fixtures.git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.product),
            fixtures.git("ls-files", "--stage", "--debug", cwd=self.product),
            fixtures.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.product),
            fixtures.git("status", "--porcelain=v1", "--untracked-files=all", cwd=self.product,
                         extra_env={"GIT_OPTIONAL_LOCKS": "0"}),
            index.read_bytes(),
            (self.product / "tracked.txt").read_bytes(),
        )

    def setUp(self) -> None:
        self._fixture_set_up()
        self.base_revision = fixtures.git(
            "rev-parse", "HEAD", cwd=self.product
        ).decode("ascii").strip()
        self.branch_ref = "refs/heads/issue-217"
        self.authority_requests: list[task_record.AuthorityRequest] = []
        self.records = self._records(authorize=self._record_authority)

    def _record_authority(self, request: task_record.AuthorityRequest) -> bool:
        self.authority_requests.append(request)
        return True

    def _records(self, *, authorize: Any = None, store: Any = None) -> task_record.TaskRecords:
        callback = authorize if authorize is not None else (lambda _request: True)
        return task_record.TaskRecords(store or self.store, authorize=callback)

    @staticmethod
    def _branch(task: str) -> str:
        return f"refs/heads/issue-{task}"

    def _create(
        self,
        *,
        task: str = "217",
        branch_ref: str | None = None,
        base_revision: str | None = None,
        title: str = "Fix issue 217",
        body: str = "Keep the requirement snapshot stable.",
        records: task_record.TaskRecords | None = None,
    ) -> task_record.Publication:
        writer = self.records if records is None else records
        return writer.create(
            task=task,
            branch_ref=self._branch(task) if branch_ref is None else branch_ref,
            base_revision=self.base_revision if base_revision is None else base_revision,
            title=title,
            body=body,
        )

    def _publish_record_link(
        self,
        task: str,
        contract_id: str,
        *,
        contract_data: bytes | None = None,
        base_revision: str | None = None,
    ) -> tuple[str, bytes, str]:
        base = self.base_revision if base_revision is None else base_revision
        record_id, record_data = task_record.encode_record(
            _REPOSITORY,
            task,
            base,
            {
                "schema_version": 1,
                "branch_ref": self._branch(task),
                "contract_id": contract_id,
            },
        )
        objects = [record_data]
        if contract_data is not None:
            objects.insert(0, contract_data)
        commit = self.store.publish(objects, {task: (None, record_id)})
        return record_id, record_data, commit

    @staticmethod
    def _raw_envelope(
        kind: str,
        task: object = "217",
        subject: object = "1" * 40,
        payload: object = None,
        *,
        overrides: dict[str, object] | None = None,
    ) -> tuple[str, bytes]:
        envelope: dict[str, object] = {
            "schema_version": 1,
            "kind": kind,
            "repository": _REPOSITORY,
            "task": task,
            "subject": subject,
            "payload": {
                "schema_version": 1,
                "title": "A title",
                "body": "A body",
            } if payload is None else payload,
        }
        if overrides:
            envelope.update(overrides)
        data = json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        object_id = hashlib.sha256(_OBJECT_DOMAIN + data).hexdigest()
        return object_id, data

    def test_contract_and_record_have_stable_golden_bytes_and_ids(self) -> None:
        base = "1" * 40
        title = "Fix #217"
        body = "Requirement body"
        contract_payload = {"schema_version": 1, "title": title, "body": body}
        contract_bytes = (
            b'{"kind":"contract","payload":{"body":"Requirement body",'
            b'"schema_version":1,"title":"Fix #217"},"repository":"acme/widgets",'
            b'"schema_version":1,"subject":"' + base.encode("ascii") + b'","task":"217"}'
        )
        contract_id = hashlib.sha256(_OBJECT_DOMAIN + contract_bytes).hexdigest()

        self.assertEqual(
            (contract_id, contract_bytes),
            task_record.encode_contract(_REPOSITORY, "217", base, contract_payload),
        )
        self.assertEqual(
            (contract_id, contract_bytes),
            task_record.encode_contract(_REPOSITORY, "217", base, contract_payload),
        )

        record_payload = {
            "schema_version": 1,
            "branch_ref": "refs/heads/issue-217",
            "contract_id": contract_id,
        }
        record_bytes = (
            b'{"kind":"task-record","payload":{"branch_ref":"refs/heads/issue-217",'
            b'"contract_id":"' + contract_id.encode("ascii") + b'","schema_version":1},'
            b'"repository":"acme/widgets","schema_version":1,"subject":"'
            + base.encode("ascii") + b'","task":"217"}'
        )
        record_id = hashlib.sha256(_OBJECT_DOMAIN + record_bytes).hexdigest()
        self.assertEqual(
            (record_id, record_bytes),
            task_record.encode_record(_REPOSITORY, "217", base, record_payload),
        )

    def test_contract_and_record_payload_schemas_are_closed_and_typed(self) -> None:
        base = self.base_revision
        branch = self.branch_ref
        contract_id, _contract_data = task_record.encode_contract(
            _REPOSITORY,
            "217",
            base,
            {"schema_version": 1, "title": "Title", "body": "Body"},
        )
        valid_record = {
            "schema_version": 1,
            "branch_ref": branch,
            "contract_id": contract_id,
        }
        bad_contract_payloads = (
            {},
            {"schema_version": 1, "title": "Title"},
            {"schema_version": 1, "body": "Body"},
            {"schema_version": 1, "title": "Title", "body": "Body", "extra": 1},
            {"schema_version": True, "title": "Title", "body": "Body"},
            {"schema_version": 1.0, "title": "Title", "body": "Body"},
            {"schema_version": 2, "title": "Title", "body": "Body"},
            {"schema_version": 1, "title": "  \t", "body": "Body"},
            {"schema_version": 1, "title": 7, "body": "Body"},
            {"schema_version": 1, "title": "Title", "body": False},
        )
        for payload in bad_contract_payloads:
            with self.subTest(contract_payload=payload):
                with self.assertRaises(ValueError):
                    task_record.encode_contract(_REPOSITORY, "217", base, payload)  # type: ignore[arg-type]
                object_id, data = self._raw_envelope(
                    "contract", task="217", subject=base, payload=payload
                )
                with self.assertRaises(ValueError):
                    task_record.decode_contract(
                        data,
                        object_id=object_id,
                        repository=_REPOSITORY,
                        task="217",
                        base_revision=base,
                    )

        bad_record_payloads = (
            {},
            {"schema_version": 1, "branch_ref": branch},
            {"schema_version": 1, "contract_id": contract_id},
            {**valid_record, "extra": True},
            {**valid_record, "schema_version": True},
            {**valid_record, "schema_version": "1"},
            {**valid_record, "schema_version": 2},
            {**valid_record, "branch_ref": 7},
            {**valid_record, "contract_id": "f" * 40},
            {**valid_record, "disposition": None},
        )
        for payload in bad_record_payloads:
            with self.subTest(record_payload=payload):
                with self.assertRaises(ValueError):
                    task_record.encode_record(_REPOSITORY, "217", base, payload)  # type: ignore[arg-type]
                object_id, data = fixtures.codec.encode_object(
                    "task-record", _REPOSITORY, "217", base, payload
                )
                with self.assertRaises(ValueError):
                    task_record.decode_record(
                        data,
                        object_id=object_id,
                        repository=_REPOSITORY,
                        task="217",
                        base_revision=base,
                        branch_ref=branch,
                    )

    def test_decode_rejects_open_and_badly_typed_envelopes(self) -> None:
        valid = {
            "schema_version": 1,
            "kind": "contract",
            "repository": _REPOSITORY,
            "task": "217",
            "subject": self.base_revision,
            "payload": {"schema_version": 1, "title": "Title", "body": "Body"},
        }
        malformed: list[dict[str, object]] = []
        extra = dict(valid)
        extra["unexpected"] = True
        malformed.append(extra)
        missing = dict(valid)
        del missing["task"]
        malformed.append(missing)
        for field, value in (
            ("schema_version", True),
            ("kind", "unknown-kind"),
            ("repository", None),
            ("task", 217),
            ("subject", "not-an-oid"),
            ("payload", []),
        ):
            wrong_type = dict(valid)
            wrong_type[field] = value
            malformed.append(wrong_type)

        for envelope in malformed:
            with self.subTest(envelope=envelope):
                data = json.dumps(
                    envelope,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                object_id = hashlib.sha256(_OBJECT_DOMAIN + data).hexdigest()
                with self.assertRaises(ValueError):
                    task_record.decode_contract(
                        data,
                        object_id=object_id,
                        repository=_REPOSITORY,
                        task="217",
                        base_revision=self.base_revision,
                    )

    def test_all_transient_facts_are_forbidden_record_fields(self) -> None:
        payload = {"schema_version": 1, "branch_ref": self.branch_ref, "contract_id": "a" * 64}
        for field in (
            "head", "current_head", "remote_head", "worktree", "worktree_path", "pr",
            "pr_state", "checkpoint", "verification", "review", "workflow_phase",
            "active", "running", "blocked", "work_unit", "provider", "model",
        ):
            with self.subTest(field=field), self.assertRaises(task_record.TaskRecordError):
                task_record.encode_record(_REPOSITORY, "217", self.base_revision, {**payload, field: "value"})

    def test_byte_identical_contract_object_reuse_is_an_exact_noop(self) -> None:
        publication = self._create()
        contract_id, data = task_record.encode_contract(
            _REPOSITORY, "217", self.base_revision,
            {"schema_version": 1, "title": "Fix issue 217", "body": "Keep the requirement snapshot stable."},
        )
        self.assertEqual(publication.contract_id, contract_id)
        before = self._product_state()
        self.assertEqual(publication.metadata_commit, self.store.publish([data]))
        self.assertEqual(publication.metadata_commit, self.store.publish([data, data]))
        self.assertEqual(before, self._product_state())

    def test_contract_collision_corruption_is_rejected(self) -> None:
        publication = self._create()
        contract_path = fixtures.codec.object_path("contract", publication.contract_id)
        _, wrong_data = task_record.encode_contract(
            _REPOSITORY, "217", self.base_revision,
            {"schema_version": 1, "title": "Different", "body": "Different bytes at the old ID"},
        )
        corrupted = self._create_remote_child(
            publication.metadata_commit, additions=((contract_path, wrong_data, "100644"),),
        )
        self._set_remote_tip(corrupted, publication.metadata_commit)
        with self.assertRaises(MetadataRefError):
            self.records.read(corrupted, task="217", base_revision=self.base_revision, branch_ref=self.branch_ref)

    def test_mutations_require_human_authority_and_matching_task_binding(self) -> None:
        initial = self._create()
        denied = self._records(authorize=lambda _request: False)
        for writer in (denied,):
            with self.assertRaises(task_record.TaskRecordError):
                writer.set_disposition(task="217", branch_ref=self.branch_ref, base_revision=self.base_revision,
                                       expected_record_id=initial.record_id, disposition={"kind": "cancelled"})
            with self.assertRaises(task_record.TaskRecordError):
                writer.reauthorize(task="217", branch_ref=self.branch_ref, base_revision=self.base_revision,
                                   expected_record_id=initial.record_id, title="Changed", body="Changed")
        with self.assertRaises(MetadataConflictError):
            self.records.set_disposition(task="218", branch_ref=self._branch("218"), base_revision=self.base_revision,
                                         expected_record_id=initial.record_id, disposition={"kind": "cancelled"})
        self.assertEqual(initial.metadata_commit, self._remote_tip())

    def test_authorization_cannot_change_captured_disposition_bytes(self) -> None:
        initial = self._create()
        decision = {"kind": "superseded", "replacement": {"repository": _REPOSITORY, "task": "218"}}

        def authorize(request: task_record.AuthorityRequest) -> bool:
            decision["replacement"]["task"] = "999"
            return True

        publication = self._records(authorize=authorize).set_disposition(
            task="217", branch_ref=self.branch_ref, base_revision=self.base_revision,
            expected_record_id=initial.record_id, disposition=decision,
        )
        resolved = self.records.read(publication.metadata_commit, task="217", base_revision=self.base_revision,
                                     branch_ref=self.branch_ref)
        self.assertEqual("218", resolved[1]["payload"]["disposition"]["replacement"]["task"])

    def test_branch_ref_grammar_agrees_with_git_check_ref_format(self) -> None:
        candidates = {
            "refs/heads/main",
            "refs/heads/feature/task-record",
            "refs/heads/nested/path_2",
            "refs/heads/a+b",
            "refs/heads/a=b",
            "refs/heads/a,b",
            "refs/heads/a!b",
            "refs/heads/a%b",
            "refs/heads/caf\u00e9",
            "refs/heads/a@b",
            "refs/heads/-leading-dash",
            "refs/heads/a.locked",
            "main",
            "refs/tags/main",
            "refs/heads",
            "refs/heads/",
            "refs/heads//child",
            "refs/heads/a//b",
            "refs/heads/.hidden",
            "refs/heads/a/.hidden",
            "refs/heads/a.",
            "refs/heads/a.lock",
            "refs/heads/a.lock/child",
            "refs/heads/a..b",
            "refs/heads/a@{b",
            "refs/heads/a b",
            "refs/heads/a~b",
            "refs/heads/a^b",
            "refs/heads/a:b",
            "refs/heads/a?b",
            "refs/heads/a*b",
            "refs/heads/a[b",
            "refs/heads/a\\b",
            "refs/heads/trailing/",
        }
        # Exercise every printable non-space ASCII character in a component;
        # Git's forbidden punctuation set is part of this public binding.
        for codepoint in range(0x21, 0x7F):
            candidates.add(f"refs/heads/x{chr(codepoint)}y")
        for codepoint in (*range(0x01, 0x20), 0x7F):
            candidates.add(f"refs/heads/x{chr(codepoint)}y")

        for candidate in sorted(candidates):
            with self.subTest(branch_ref=candidate):
                try:
                    fixtures.git("check-ref-format", candidate, cwd=self.product)
                except AssertionError:
                    git_accepts = False
                else:
                    git_accepts = True
                try:
                    task_record.validate_branch_ref(candidate)
                except task_record.TaskRecordError:
                    task_record_accepts = False
                else:
                    task_record_accepts = True
                self.assertEqual(git_accepts and candidate.startswith("refs/heads/"), task_record_accepts)

        for non_string in (None, 1, b"refs/heads/main"):
            with self.subTest(branch_ref_type=type(non_string).__name__):
                with self.assertRaises(task_record.TaskRecordError):
                    task_record.validate_branch_ref(non_string)

    def test_contract_and_record_decoders_bind_repo_task_base_and_branch(self) -> None:
        contract_id, contract_data = task_record.encode_contract(
            _REPOSITORY,
            "217",
            self.base_revision,
            {"schema_version": 1, "title": "Title", "body": "Body"},
        )
        for repository, task, base in (
            ("other/widgets", "217", self.base_revision),
            (_REPOSITORY, "218", self.base_revision),
            (_REPOSITORY, "217", "2" * 40),
        ):
            with self.subTest(repository=repository, task=task, base=base):
                with self.assertRaises(ValueError):
                    task_record.decode_contract(
                        contract_data,
                        object_id=contract_id,
                        repository=repository,
                        task=task,
                        base_revision=base,
                    )

        record_payload = {
            "schema_version": 1,
            "branch_ref": self.branch_ref,
            "contract_id": contract_id,
        }
        record_id, record_data = task_record.encode_record(
            _REPOSITORY, "217", self.base_revision, record_payload
        )
        for repository, task, base in (
            ("other/widgets", "217", self.base_revision),
            (_REPOSITORY, "218", self.base_revision),
            (_REPOSITORY, "217", "2" * 40),
        ):
            with self.subTest(repository=repository, task=task, base=base):
                with self.assertRaises(ValueError):
                    task_record.decode_record(
                        record_data,
                        object_id=record_id,
                        repository=repository,
                        task=task,
                        base_revision=base,
                        branch_ref=self.branch_ref,
                    )
        with self.assertRaises(task_record.TaskRecordError):
            task_record.decode_record(
                record_data,
                object_id=record_id,
                repository=_REPOSITORY,
                task="217",
                base_revision=self.base_revision,
                branch_ref="refs/heads/some-other-branch",
            )

    def test_create_requires_an_exact_existing_commit_as_base(self) -> None:
        tree = fixtures.git(
            "rev-parse", f"{self.base_revision}^{{tree}}", cwd=self.product
        ).decode("ascii").strip()
        self.store.validate_base_revision(self.base_revision)
        with self.assertRaises(MetadataRefError):
            self.store.validate_base_revision(tree)
        with self.assertRaises(MetadataRefError):
            self._create(base_revision="0" * 40)
        with self.assertRaises(ValueError):
            self._create(base_revision="HEAD")
        self.assertIsNone(self._remote_tip())
        self.assertEqual([], self.authority_requests)

    def test_create_reads_exact_pinned_record_and_rejects_duplicate_creation(self) -> None:
        publication = self._create()
        self.assertEqual(publication.metadata_commit, self._remote_tip())
        resolved = self.records.read(
            publication.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        record_id, record, contract = resolved
        self.assertEqual(publication.record_id, record_id)
        self.assertEqual(publication.contract_id, record["payload"]["contract_id"])
        self.assertEqual("task-record", record["kind"])
        self.assertEqual("contract", contract["kind"])
        self.assertEqual(
            {"schema_version": 1, "title": "Fix issue 217", "body": "Keep the requirement snapshot stable."},
            contract["payload"],
        )
        self.assertIsNone(
            self.records.read(
                publication.metadata_commit,
                task="218",
                base_revision=self.base_revision,
                branch_ref=self._branch("218"),
            )
        )

        self.authority_requests.clear()
        with self.assertRaises(MetadataConflictError):
            self._create(title="A second authorization", body="Must not replace the first")
        self.assertEqual(publication.metadata_commit, self._remote_tip())
        self.assertEqual(1, len(self.authority_requests))
        request = self.authority_requests[0]
        self.assertEqual("create", request.operation)
        self.assertEqual(_REPOSITORY, request.repository)
        self.assertEqual("217", request.task)
        self.assertEqual(self.branch_ref, request.branch_ref)
        self.assertEqual(self.base_revision, request.base_revision)
        self.assertIsNone(request.expected_record_id)
        second_contract_id, _second_contract_data = task_record.encode_contract(
            _REPOSITORY,
            "217",
            self.base_revision,
            {
                "schema_version": 1,
                "title": "A second authorization",
                "body": "Must not replace the first",
            },
        )
        second_record_id, _second_record_data = task_record.encode_record(
            _REPOSITORY,
            "217",
            self.base_revision,
            {
                "schema_version": 1,
                "branch_ref": self.branch_ref,
                "contract_id": second_contract_id,
            },
        )
        self.assertEqual(second_record_id, request.proposed_record_id)
        self.assertEqual(second_contract_id, request.contract_id)

    def test_authority_request_is_exact_frozen_and_rejection_is_side_effect_free(self) -> None:
        captured: list[task_record.AuthorityRequest] = []

        def authorize(request: task_record.AuthorityRequest) -> bool:
            captured.append(request)
            self.assertEqual(
                {
                    "operation",
                    "repository",
                    "task",
                    "branch_ref",
                    "base_revision",
                    "expected_record_id",
                    "proposed_record_id",
                    "contract_id",
                },
                {field.name for field in fields(request)},
            )
            with self.assertRaises(FrozenInstanceError):
                request.task = "999"  # type: ignore[misc]
            return True

        publisher = self._records(authorize=authorize)
        result = publisher.create(
            task="217",
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            title="Frozen request",
            body="Only exact scope is authorized",
        )
        self.assertEqual(1, len(captured))
        request = captured[0]
        self.assertEqual(
            (
                "create",
                _REPOSITORY,
                "217",
                self.branch_ref,
                self.base_revision,
                None,
                result.record_id,
                result.contract_id,
            ),
            (
                request.operation,
                request.repository,
                request.task,
                request.branch_ref,
                request.base_revision,
                request.expected_record_id,
                request.proposed_record_id,
                request.contract_id,
            ),
        )

        for response in (False, None, 1, "yes", object()):
            with self.subTest(authority_response=type(response).__name__):
                remote_before = self._remote_tip()
                denied = self._records(authorize=lambda _request, value=response: value)
                with self.assertRaises(task_record.TaskRecordError):
                    denied.create(
                        task="218",
                        branch_ref=self._branch("218"),
                        base_revision=self.base_revision,
                        title="Not authorized",
                        body="Only literal True grants authority",
                    )
                self.assertEqual(remote_before, self._remote_tip())

    def test_authority_callback_exception_and_missing_capability_fail_closed(self) -> None:
        def explode(_request: task_record.AuthorityRequest) -> bool:
            raise RuntimeError("host could not validate the fetched Issue")

        with self.assertRaises(task_record.TaskRecordError) as raised:
            self._records(authorize=explode).create(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                title="Title",
                body="Body",
            )
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertIsNone(self._remote_tip())
        with self.assertRaises(task_record.TaskRecordError):
            task_record.TaskRecords(self.store, authorize=None)  # type: ignore[arg-type]

    def test_cancelled_and_superseded_are_explicit_replacements(self) -> None:
        cancelled = self._create(task="217")
        cancelled_update = self.records.set_disposition(
            task="217",
            branch_ref=self._branch("217"),
            base_revision=self.base_revision,
            expected_record_id=cancelled.record_id,
            disposition={"kind": "cancelled"},
        )
        cancelled_read = self.records.read(
            cancelled_update.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self._branch("217"),
        )
        assert cancelled_read is not None
        self.assertEqual({"kind": "cancelled"}, cancelled_read[1]["payload"]["disposition"])

        superseded = self._create(task="218")
        superseded_update = self.records.set_disposition(
            task="218",
            branch_ref=self._branch("218"),
            base_revision=self.base_revision,
            expected_record_id=superseded.record_id,
            disposition={
                "kind": "superseded",
                "replacement": {"repository": _REPOSITORY, "task": "219"},
            },
        )
        superseded_read = self.records.read(
            superseded_update.metadata_commit,
            task="218",
            base_revision=self.base_revision,
            branch_ref=self._branch("218"),
        )
        assert superseded_read is not None
        self.assertEqual(
            {
                "kind": "superseded",
                "replacement": {"repository": _REPOSITORY, "task": "219"},
            },
            superseded_read[1]["payload"]["disposition"],
        )
        self.assertEqual("disposition", self.authority_requests[-1].operation)
        self.assertEqual(superseded.record_id, self.authority_requests[-1].expected_record_id)
        self.assertEqual(superseded_update.record_id, self.authority_requests[-1].proposed_record_id)

    def test_malformed_unchanged_and_self_superseding_dispositions_are_rejected(self) -> None:
        valid_record = {
            "schema_version": 1,
            "branch_ref": self.branch_ref,
            "contract_id": "a" * 64,
        }
        bad_dispositions = (
            None,
            {"kind": "closed"},
            {"kind": "cancelled", "replacement": {"repository": _REPOSITORY, "task": "218"}},
            {"kind": "superseded"},
            {"kind": "superseded", "replacement": {"repository": _REPOSITORY}},
            {"kind": "superseded", "replacement": {"repository": "bad", "task": "218"}},
            {"kind": "superseded", "replacement": {"repository": _REPOSITORY, "task": "01"}},
            {"kind": "superseded", "replacement": {"repository": _REPOSITORY, "task": "217"}},
            {"kind": "superseded", "replacement": {"repository": _REPOSITORY, "task": "218", "extra": 1}},
        )
        for disposition in bad_dispositions:
            with self.subTest(disposition=disposition):
                with self.assertRaises(ValueError):
                    task_record.encode_record(
                        _REPOSITORY,
                        "217",
                        self.base_revision,
                        {**valid_record, "disposition": disposition},
                    )

        initial = self._create()
        first_disposition = self.records.set_disposition(
            task="217",
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            expected_record_id=initial.record_id,
            disposition={"kind": "cancelled"},
        )
        self.authority_requests.clear()
        with self.assertRaises(task_record.TaskRecordError):
            self.records.set_disposition(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id=first_disposition.record_id,
                disposition={"kind": "cancelled"},
            )
        self.assertEqual([], self.authority_requests)
        self.assertEqual(first_disposition.metadata_commit, self._remote_tip())

    def test_explicit_expected_record_cas_and_stale_ids(self) -> None:
        initial = self._create()
        self.authority_requests.clear()
        with self.assertRaises(task_record.TaskRecordError):
            self.records.set_disposition(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id="not-an-object-id",
                disposition={"kind": "cancelled"},
            )
        with self.assertRaises(MetadataConflictError):
            self.records.set_disposition(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id="0" * 64,
                disposition={"kind": "cancelled"},
            )
        self.assertEqual([], self.authority_requests)
        self.assertEqual(initial.metadata_commit, self._remote_tip())

        changed = self.records.set_disposition(
            task="217",
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            expected_record_id=initial.record_id,
            disposition={"kind": "cancelled"},
        )
        self.assertNotEqual(initial.record_id, changed.record_id)
        self.assertEqual(initial.record_id, self.authority_requests[-1].expected_record_id)

    def test_concurrent_same_task_replacement_cannot_overwrite_winner(self) -> None:
        initial = self._create()
        competing_store = self._writer()
        competitor = self._records(store=competing_store)
        original_push = self.store._push_commit
        winner: list[task_record.Publication] = []

        def race(candidate: str, expected_old: str | None) -> bool:
            if not winner:
                winner.append(
                    competitor.reauthorize(
                        task="217",
                        branch_ref=self.branch_ref,
                        base_revision=self.base_revision,
                        expected_record_id=initial.record_id,
                        title="Winning replacement",
                        body="Concurrent writer wins the direct-ref CAS",
                    )
                )
            return original_push(candidate, expected_old)

        self.store._push_commit = race  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError):
            self.records.reauthorize(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id=initial.record_id,
                title="Stale replacement",
                body="Must not overwrite the winner",
            )
        self.assertEqual(1, len(winner))
        self.assertEqual(winner[0].metadata_commit, self._remote_tip())
        resolved = self.records.read(
            winner[0].metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert resolved is not None
        self.assertEqual(winner[0].record_id, resolved[0])
        self.assertEqual("Winning replacement", resolved[2]["payload"]["title"])

    def test_reauthorization_rejects_same_snapshot_and_preserves_old_bytes(self) -> None:
        initial = self._create(title="Original contract", body="Original immutable bytes")
        original = self.records.read(
            initial.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert original is not None
        old_record_id, old_record, old_contract = original
        old_record_bytes = fixtures.codec.encode_object(
            old_record["kind"],
            old_record["repository"],
            old_record["task"],
            old_record["subject"],
            old_record["payload"],
        )[1]
        old_contract_bytes = fixtures.codec.encode_object(
            old_contract["kind"],
            old_contract["repository"],
            old_contract["task"],
            old_contract["subject"],
            old_contract["payload"],
        )[1]

        self.authority_requests.clear()
        with self.assertRaises(task_record.TaskRecordError):
            self.records.reauthorize(
                task="217",
                branch_ref=self.branch_ref,
                base_revision=self.base_revision,
                expected_record_id=initial.record_id,
                title="Original contract",
                body="Original immutable bytes",
            )
        self.assertEqual([], self.authority_requests)
        self.assertEqual(initial.metadata_commit, self._remote_tip())

        replacement = self.records.reauthorize(
            task="217",
            branch_ref=self.branch_ref,
            base_revision=self.base_revision,
            expected_record_id=initial.record_id,
            title="New contract snapshot",
            body="New authorized requirement text",
        )
        self.assertNotEqual(initial.contract_id, replacement.contract_id)
        self.assertNotEqual(old_record_id, replacement.record_id)
        request = self.authority_requests[-1]
        self.assertEqual("reauthorize", request.operation)
        self.assertEqual(initial.record_id, request.expected_record_id)
        self.assertEqual(replacement.record_id, request.proposed_record_id)
        self.assertEqual(replacement.contract_id, request.contract_id)
        self.assertEqual(initial.metadata_commit, fixtures.git(
            "rev-parse", f"{replacement.metadata_commit}^", cwd=self.product
        ).decode("ascii").strip())

        latest = self.records.read(
            replacement.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert latest is not None
        self.assertEqual(replacement.record_id, latest[0])
        self.assertEqual("New contract snapshot", latest[2]["payload"]["title"])
        old_record_after = self.store.read_object(
            replacement.metadata_commit,
            "task-record",
            old_record_id,
            task="217",
            subject=self.base_revision,
        )
        old_contract_after = self.store.read_object(
            replacement.metadata_commit,
            "contract",
            initial.contract_id,
            task="217",
            subject=self.base_revision,
        )
        self.assertEqual(old_record_bytes, fixtures.codec.encode_object(
            old_record_after["kind"], old_record_after["repository"], old_record_after["task"],
            old_record_after["subject"], old_record_after["payload"],
        )[1])
        self.assertEqual(old_contract_bytes, fixtures.codec.encode_object(
            old_contract_after["kind"], old_contract_after["repository"], old_contract_after["task"],
            old_contract_after["subject"], old_contract_after["payload"],
        )[1])
        historical = self.records.read(
            initial.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert historical is not None
        self.assertEqual(old_record_id, historical[0])

    def test_contract_links_must_resolve_in_the_same_metadata_commit(self) -> None:
        contract_id, contract_data = task_record.encode_contract(
            _REPOSITORY,
            "217",
            self.base_revision,
            {"schema_version": 1, "title": "Valid", "body": "Valid body"},
        )
        _record_id, _record_data, commit = self._publish_record_link("217", contract_id)
        with self.assertRaises(MetadataRefError):
            self.records.read(
                commit,
                task="217",
                base_revision=self.base_revision,
                branch_ref=self._branch("217"),
            )

        other_repo_id, other_repo_data = fixtures.codec.encode_object(
            "contract",
            "other/widgets",
            "218",
            self.base_revision,
            {"schema_version": 1, "title": "Other repo", "body": "Wrong repository"},
        )
        self.assertNotEqual(contract_id, other_repo_id)
        with self.assertRaises(ValueError):
            task_record.decode_contract(
                other_repo_data,
                object_id=other_repo_id,
                repository=_REPOSITORY,
                task="218",
                base_revision=self.base_revision,
            )

        # The generic metadata substrate also rejects a cross-repository
        # object before the Task Record capability can treat it as a link.
        _cross_repo_record, _cross_repo_bytes, record_commit = self._publish_record_link(
            "218", other_repo_id
        )
        cross_repo_tip = self._create_remote_child(
            record_commit,
            additions=((fixtures.codec.object_path("contract", other_repo_id), other_repo_data, "100644"),),
        )
        self._set_remote_tip(cross_repo_tip, record_commit)
        with self.assertRaises(MetadataRefError):
            self.records.read(
                cross_repo_tip,
                task="218",
                base_revision=self.base_revision,
                branch_ref=self._branch("218"),
            )

    def test_contract_links_reject_wrong_task_base_and_unknown_payload_schema(self) -> None:
        cases = (
            (
                "wrong-task",
                "217",
                "218",
                self.base_revision,
                {"schema_version": 1, "title": "Wrong task", "body": "Wrong task binding"},
            ),
            (
                "wrong-base",
                "218",
                "218",
                "2" * 40,
                {"schema_version": 1, "title": "Wrong base", "body": "Wrong base binding"},
            ),
            (
                "unknown-payload",
                "219",
                "219",
                self.base_revision,
                {"schema_version": 1, "title": "Unknown", "body": "Extra field", "extra": True},
            ),
        )
        for label, record_task, contract_task, subject, payload in cases:
            with self.subTest(link=label):
                contract_id, data = fixtures.codec.encode_object(
                    "contract", _REPOSITORY, contract_task, subject, payload
                )
                _record_id, _record_data, commit = self._publish_record_link(
                    record_task,
                    contract_id,
                    contract_data=data,
                )
                with self.assertRaises((ValueError, MetadataRefError)):
                    self.records.read(
                        commit,
                        task=record_task,
                        base_revision=self.base_revision,
                        branch_ref=self._branch(record_task),
                    )

    def test_read_rejects_wrong_record_branch_and_base_bindings(self) -> None:
        publication = self._create()
        with self.assertRaises(task_record.TaskRecordError):
            self.records.read(
                publication.metadata_commit,
                task="217",
                base_revision=self.base_revision,
                branch_ref="refs/heads/wrong-branch",
            )
        with self.assertRaises(ValueError):
            self.records.read(
                publication.metadata_commit,
                task="217",
                base_revision="2" * 40,
                branch_ref=self.branch_ref,
            )

    def test_branch_cleanup_and_clean_clone_do_not_remove_metadata(self) -> None:
        branch_oid = self.base_revision
        fixtures.git("update-ref", self.branch_ref, branch_oid, cwd=self.product)
        publication = self._create()
        fixtures.git("update-ref", "-d", self.branch_ref, branch_oid, cwd=self.product)
        with self.assertRaises(AssertionError):
            fixtures.git("show-ref", "--verify", self.branch_ref, cwd=self.product)

        # The capability receives no Issue/PR status input: closing a PR,
        # reporting a failed check, or deleting its product branch cannot
        # manufacture a disposition or erase the metadata record.
        resolved = self.records.read(
            publication.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert resolved is not None
        self.assertNotIn("disposition", resolved[1]["payload"])
        self.assertEqual(publication.metadata_commit, self._remote_tip())

        clean_clone = self.temp_root / "task-record-clean-clone"
        fixtures.git("clone", "--no-checkout", str(self.product), str(clean_clone))
        fixtures.git("remote", "set-url", "origin", str(self.remote_path), cwd=clean_clone)
        clean_store = fixtures.metadata_ref_module.MetadataStore(clean_clone, _REPOSITORY)
        clone_reader = self._records(store=clean_store)
        self.assertEqual(publication.metadata_commit, clean_store.fetch_tip())
        clone_result = clone_reader.read(
            publication.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert clone_result is not None
        self.assertEqual(publication.record_id, clone_result[0])

    def test_external_changes_do_not_implicitly_set_a_disposition(self) -> None:
        publication = self._create()
        request_fields = {field.name for field in fields(self.authority_requests[0])}
        self.assertEqual(
            {
                "operation",
                "repository",
                "task",
                "branch_ref",
                "base_revision",
                "expected_record_id",
                "proposed_record_id",
                "contract_id",
            },
            request_fields,
        )
        # External PR closure/check failure are not metadata inputs. A later
        # read alone remains observational and never derives a disposition.
        resolved = self.records.read(
            publication.metadata_commit,
            task="217",
            base_revision=self.base_revision,
            branch_ref=self.branch_ref,
        )
        assert resolved is not None
        self.assertNotIn("disposition", resolved[1]["payload"])
        self.assertEqual(publication.metadata_commit, self._remote_tip())

    def test_product_head_tree_index_and_dirty_files_are_unchanged_on_success(self) -> None:
        (self.product / "tracked.txt").write_text("staged product edit\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        (self.product / "tracked.txt").write_text("staged plus unstaged product edit\n", encoding="utf-8")
        (self.product / "untracked.txt").write_text("untracked stays local\n", encoding="utf-8")
        before = self._product_state()
        before_untracked = (self.product / "untracked.txt").read_bytes()

        publication = self._create()

        self.assertEqual(before, self._product_state())
        self.assertEqual(before_untracked, (self.product / "untracked.txt").read_bytes())
        self.assertEqual(publication.metadata_commit, self._remote_tip())

    def test_product_head_tree_index_and_dirty_files_are_unchanged_on_failure(self) -> None:
        (self.product / "tracked.txt").write_text("staged failure edit\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        (self.product / "tracked.txt").write_text("unstaged failure edit\n", encoding="utf-8")
        (self.product / "untracked.txt").write_text("untracked failure stays local\n", encoding="utf-8")
        before = self._product_state()
        before_untracked = (self.product / "untracked.txt").read_bytes()
        denied = self._records(authorize=lambda _request: False)

        with self.assertRaises(task_record.TaskRecordError):
            self._create(records=denied)

        self.assertEqual(before, self._product_state())
        self.assertEqual(before_untracked, (self.product / "untracked.txt").read_bytes())
        self.assertIsNone(self._remote_tip())

    def test_historical_base_remains_valid_after_product_head_advances(self) -> None:
        original_base = self.base_revision
        publication = self._create()
        (self.product / "tracked.txt").write_text("advance product HEAD\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        fixtures.git("commit", "-m", "advance product after Task Record", cwd=self.product)
        new_head = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.assertNotEqual(original_base, new_head)

        replacement = self.records.reauthorize(
            task="217",
            branch_ref=self.branch_ref,
            base_revision=original_base,
            expected_record_id=publication.record_id,
            title="Still tied to original base",
            body="Product HEAD advancement does not rebind history",
        )
        resolved = self.records.read(
            replacement.metadata_commit,
            task="217",
            base_revision=original_base,
            branch_ref=self.branch_ref,
        )
        assert resolved is not None
        self.assertEqual(original_base, resolved[1]["subject"])
        with self.assertRaises(ValueError):
            self.records.read(
                replacement.metadata_commit,
                task="217",
                base_revision=new_head,
                branch_ref=self.branch_ref,
            )

    def test_corrupted_content_address_and_unauthorized_path_are_rejected(self) -> None:
        publication = self._create()
        record_path = fixtures.codec.object_path("task-record", publication.record_id)
        corrupted = self._create_remote_child(
            publication.metadata_commit,
            additions=((record_path, b"different bytes at the same immutable path", "100644"),),
        )
        self._set_remote_tip(corrupted, publication.metadata_commit)
        with self.assertRaises(MetadataRefError):
            self.records.read(
                corrupted,
                task="217",
                base_revision=self.base_revision,
                branch_ref=self.branch_ref,
            )

        self._set_remote_tip(publication.metadata_commit, corrupted)
        malformed_path = self._create_remote_child(
            publication.metadata_commit,
            additions=(("untrusted/not-a-metadata-path", b"not authorized", "100644"),),
        )
        self._set_remote_tip(malformed_path, publication.metadata_commit)
        with self.assertRaises(MetadataRefError):
            self.records.read(
                malformed_path,
                task="217",
                base_revision=self.base_revision,
                branch_ref=self.branch_ref,
            )


if __name__ == "__main__":
    unittest.main()
