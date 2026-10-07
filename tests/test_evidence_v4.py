from __future__ import annotations

import hashlib
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import test_metadata_ref as fixtures
import evidence as evidence_module
from metadata_ref import MetadataRefError


_OBJECT_DOMAIN = b"agentcore-metadata-object/v1\n"
_REPOSITORY = "acme/widgets"


class EvidenceV4Test(unittest.TestCase):
    """Focused #145 coverage, reusing only the local #217 fixture helpers."""

    _fixture_set_up = fixtures.MetadataRefTest.setUp
    _writer = fixtures.MetadataRefTest._writer
    _fixture_direct_ref_cas = fixtures.MetadataRefTest._fixture_direct_ref_cas
    _git_dir = staticmethod(fixtures.MetadataRefTest._git_dir)
    _remote_tip = fixtures.MetadataRefTest._remote_tip

    def setUp(self) -> None:
        self._fixture_set_up()
        self.subject = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.reader = evidence_module.Evidence(self.store)

    def _payload(
        self,
        *,
        kind: object = "capability/result-v1",
        producer: object | None = None,
        created_at: object = "2024-02-29T12:34:56Z",
        payload: object | None = None,
        supersedes: object | None = None,
        include_supersedes: bool = False,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "schema_version": 1,
            "kind": kind,
            "producer": {"name": "fixture-runner", "version": "3"} if producer is None else producer,
            "created_at": created_at,
            "payload": {"result": "PASS", "unicode": "café"} if payload is None else payload,
        }
        if include_supersedes or supersedes is not None:
            envelope["supersedes"] = supersedes
        return envelope

    def _encode(
        self,
        *,
        repository: str = _REPOSITORY,
        task: str = "217",
        subject: str | None = None,
        **payload_options: Any,
    ) -> tuple[str, bytes]:
        return evidence_module.encode_evidence(
            repository,
            task,
            self.subject if subject is None else subject,
            self._payload(**payload_options),
        )

    def _product_state(self) -> tuple[bytes, ...]:
        # Do not use write-tree or status with optional refreshes here: the
        # observation itself must not perturb the index bytes being asserted.
        index_path_text = fixtures.git("rev-parse", "--git-path", "index", cwd=self.product).decode().strip()
        index_path = Path(index_path_text)
        if not index_path.is_absolute():
            index_path = self.product / index_path
        return (
            fixtures.git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.product),
            fixtures.git("ls-files", "--stage", "--debug", cwd=self.product),
            fixtures.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.product),
            fixtures.git(
                "status", "--porcelain=v1", "--untracked-files=all", cwd=self.product,
                extra_env={"GIT_OPTIONAL_LOCKS": "0"},
            ),
            index_path.read_bytes(),
            (self.product / "tracked.txt").read_bytes(),
        )

    def test_evidence_codec_golden_vector_pins_literal_bytes_and_digest(self) -> None:
        # This is the shared codec's evidence-kind golden vector. The Evidence
        # wrapper has a distinct capability payload schema, tested below.
        object_id, data = fixtures.codec.encode_object(
            "evidence", _REPOSITORY, "217", "a" * 40,
            {"𐀀": -7, "é": 'line\nquote"'},
        )
        expected_data = (
            b'{"kind":"evidence","payload":{"\xc3\xa9":"line\\nquote\\\"",'
            b'"\xf0\x90\x80\x80":-7},"repository":"acme/widgets",'
            b'"schema_version":1,"subject":"' + b"a" * 40 + b'","task":"217"}'
        )
        self.assertEqual(expected_data, data)
        self.assertEqual(
            "c465dbb11eee6082c802177fe8735d72f623721de5490a070fa465042dce03e8",
            object_id,
        )

    def test_evidence_encoding_has_stable_literal_bytes_and_unicode_key_order(self) -> None:
        expected_data = (
            b'{"kind":"evidence","payload":{"created_at":"2024-02-29T12:34:56Z",'
            b'"kind":"capability/result-v1","payload":{"result":"PASS",'
            b'"unicode":"caf\xc3\xa9"},"producer":{"name":"fixture-runner",'
            b'"version":"3"},"schema_version":1},"repository":"acme/widgets",'
            b'"schema_version":1,"subject":"' + b"a" * 40 + b'","task":"217"}'
        )
        first = evidence_module.encode_evidence(
            _REPOSITORY,
            "217",
            "a" * 40,
            self._payload(),
        )
        reordered = evidence_module.encode_evidence(
            _REPOSITORY,
            "217",
            "a" * 40,
            {
                "payload": {"unicode": "café", "result": "PASS"},
                "created_at": "2024-02-29T12:34:56Z",
                "producer": {"version": "3", "name": "fixture-runner"},
                "kind": "capability/result-v1",
                "schema_version": 1,
            },
        )
        self.assertEqual(expected_data, first[1])
        self.assertEqual(first, reordered)
        self.assertEqual(hashlib.sha256(_OBJECT_DOMAIN + expected_data).hexdigest(), first[0])

    def test_payload_schema_is_closed_and_exactly_typed(self) -> None:
        valid = self._payload()
        malformed: list[dict[str, object]] = []
        for required_field in ("schema_version", "kind", "producer", "created_at", "payload"):
            missing = dict(valid)
            del missing[required_field]
            malformed.append(missing)
        extra = {**valid, "unexpected": True}
        malformed.append(extra)

        for field, value in (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("schema_version", "1"),
            ("schema_version", 2),
            ("kind", ""),
            ("kind", "  \t"),
            ("kind", None),
            ("kind", 7),
            ("producer", []),
            ("producer", None),
            ("created_at", None),
            ("created_at", 123),
            ("payload", []),
            ("payload", None),
            ("supersedes", None),
            ("supersedes", "A" * 64),
            ("supersedes", "f" * 40),
            ("supersedes", 1),
        ):
            wrong = dict(valid)
            wrong[field] = value
            malformed.append(wrong)

        for timestamp in (
            "2024-02-29T12:34:56+00:00",
            "2024-02-29T12:34:56.000Z",
            "2024-2-29T12:34:56Z",
            "2023-02-29T12:34:56Z",
            "2024-02-30T12:34:56Z",
            "2024-02-29T24:00:00Z",
            "2024-02-29T12:34:60Z",
        ):
            wrong = dict(valid)
            wrong["created_at"] = timestamp
            malformed.append(wrong)

        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                evidence_module.encode_evidence(
                    _REPOSITORY, "217", self.subject, payload  # type: ignore[arg-type]
                )
            # Exercise Evidence's reader-side schema check whenever the shared
            # metadata codec can represent the malformed capability payload.
            try:
                malformed_id, malformed_data = fixtures.codec.encode_object(
                    "evidence", _REPOSITORY, "217", self.subject, payload  # type: ignore[arg-type]
                )
            except ValueError:
                continue
            with self.subTest(decoded_payload=payload), self.assertRaises(ValueError):
                evidence_module.decode_evidence(
                    malformed_data,
                    evidence_id=malformed_id,
                    repository=_REPOSITORY,
                    task="217",
                    subject=self.subject,
                )

    def test_decode_binds_exact_repository_task_subject_and_content_id(self) -> None:
        object_id, data = self._encode()
        decoded = evidence_module.decode_evidence(
            data,
            evidence_id=object_id,
            repository=_REPOSITORY,
            task="217",
            subject=self.subject,
        )
        self.assertEqual("evidence", decoded["kind"])
        self.assertEqual(object_id, hashlib.sha256(_OBJECT_DOMAIN + data).hexdigest())
        self.assertEqual(_REPOSITORY, decoded["repository"])
        self.assertEqual("217", decoded["task"])
        self.assertEqual(self.subject, decoded["subject"])

        for repository, task, subject in (
            ("other/widgets", "217", self.subject),
            (_REPOSITORY, "218", self.subject),
            (_REPOSITORY, "217", "f" * 40),
        ):
            with self.subTest(repository=repository, task=task, subject=subject):
                with self.assertRaises(ValueError):
                    evidence_module.decode_evidence(
                        data,
                        evidence_id=object_id,
                        repository=repository,
                        task=task,
                        subject=subject,
                    )
        with self.assertRaises(ValueError):
            evidence_module.decode_evidence(
                data,
                evidence_id="0" * 64,
                repository=_REPOSITORY,
                task="217",
                subject=self.subject,
            )

        for field in ("evidence_id", "repository", "task", "subject"):
            bindings = dict(evidence_id=object_id, repository=_REPOSITORY, task="217", subject=self.subject)
            bindings[field] = None
            with self.subTest(unbound=field), self.assertRaises(ValueError):
                evidence_module.decode_evidence(data, **bindings)

        subject_64 = "c" * 64
        long_subject_id, long_subject_data = evidence_module.encode_evidence(
            _REPOSITORY, "217", subject_64, self._payload()
        )
        self.assertEqual(
            subject_64,
            evidence_module.decode_evidence(
                long_subject_data,
                evidence_id=long_subject_id,
                repository=_REPOSITORY,
                task="217",
                subject=subject_64,
            )["subject"],
        )

        for repository, task, subject in (
            ("acme", "217", self.subject),
            (_REPOSITORY, "0217", self.subject),
            (_REPOSITORY, "217", "not-a-full-git-oid"),
        ):
            with self.subTest(encode_repository=repository, encode_task=task, encode_subject=subject):
                with self.assertRaises(ValueError):
                    evidence_module.encode_evidence(repository, task, subject, self._payload())

    def test_payload_is_opaque_and_producer_is_descriptive_not_a_runtime_lookup(self) -> None:
        opaque_payload = {
            "schema_version": "capability-specific/v9",
            "result": {"status": "FAIL", "passed": False, "code": 73},
            "work_unit": "WU-145-OPAQUE",
            "arbitrary": [None, True, {"meaning": "owned by the capability"}],
        }
        producer = {
            "name": "external producer",
            "runtime_id": "runtime-does-not-exist",
            "history_commit": "f" * 40,
            "history_ref": "refs/heads/no-such-producer-history",
        }
        object_id, data = evidence_module.encode_evidence(
            _REPOSITORY,
            "217",
            "f" * 40,
            self._payload(producer=producer, payload=opaque_payload),
        )
        decoded = evidence_module.decode_evidence(
            data,
            evidence_id=object_id,
            repository=_REPOSITORY,
            task="217",
            subject="f" * 40,
        )
        self.assertEqual(opaque_payload, decoded["payload"]["payload"])
        self.assertEqual(producer, decoded["payload"]["producer"])
        # Neither PASS-like / FAIL-like strings nor Work Unit-shaped labels are
        # interpreted as a Kernel disposition, validity, or runtime lookup.
        self.assertEqual("FAIL", decoded["payload"]["payload"]["result"]["status"])
        self.assertNotIn("valid", decoded["payload"])

    def test_byte_identical_reuse_is_a_noop_and_has_no_writer_or_latest_api(self) -> None:
        object_id, data = self._encode()
        first_commit = self.store.publish([data])
        before = self._product_state()

        self.assertEqual(first_commit, self.store.publish([data]))
        self.assertEqual(first_commit, self.store.publish([data, data]))
        self.assertEqual(before, self._product_state())
        resolved = self.reader.read(first_commit, object_id, task="217", subject=self.subject)
        self.assertEqual(
            data,
            fixtures.codec.encode_object(
                resolved["kind"], resolved["repository"], resolved["task"],
                resolved["subject"], resolved["payload"],
            )[1],
        )
        for name in ("publish", "create", "latest", "effective", "list"):
            self.assertFalse(hasattr(self.reader, name), name)

    def test_exact_object_history_is_preserved_and_timestamps_do_not_select_latest(self) -> None:
        earlier_id, earlier_data = self._encode(created_at="2030-01-01T00:00:00Z", payload={"fact": "earlier"})
        first_commit = self.store.publish([earlier_data])
        later_id, later_data = self._encode(created_at="2000-01-01T00:00:00Z", payload={"fact": "later"})
        second_commit = self.store.publish([later_data])

        earlier = self.reader.read(second_commit, earlier_id, task="217", subject=self.subject)
        self.assertEqual(
            (earlier_id, earlier_data),
            fixtures.codec.encode_object(
                earlier["kind"], earlier["repository"], earlier["task"],
                earlier["subject"], earlier["payload"],
            ),
        )
        self.assertEqual("earlier", self.reader.read(first_commit, earlier_id, task="217", subject=self.subject)["payload"]["payload"]["fact"])
        self.assertEqual("later", self.reader.read(second_commit, later_id, task="217", subject=self.subject)["payload"]["payload"]["fact"])

    def test_explicit_supersedes_resolves_only_inside_the_pinned_metadata_commit(self) -> None:
        prior_id, prior_data = self._encode(payload={"fact": "prior"})
        prior_commit = self.store.publish([prior_data])
        current_id, current_data = self._encode(
            payload={"fact": "current"}, supersedes=prior_id
        )
        current_commit = self.store.publish([current_data])

        current = self.reader.read(current_commit, current_id, task="217", subject=self.subject)
        self.assertEqual("current", current["payload"]["payload"]["fact"])
        with self.assertRaises(MetadataRefError):
            self.reader.read(prior_commit, current_id, task="217", subject=self.subject)
        self.assertEqual(
            "prior",
            self.reader.read(current_commit, prior_id, task="217", subject=self.subject)["payload"]["payload"]["fact"],
        )

    def test_supersedes_does_not_impose_task_subject_or_payload_kind_replacement_semantics(self) -> None:
        prior_id, prior_data = self._encode(
            task="218", subject="b" * 40, kind="different-capability-kind", payload={"fact": "old"}
        )
        current_id, current_data = self._encode(
            task="217", subject=self.subject, kind="current-capability-kind",
            payload={"fact": "new"}, supersedes=prior_id,
        )
        commit = self.store.publish([prior_data, current_data])

        current = self.reader.read(commit, current_id, task="217", subject=self.subject)
        self.assertEqual("current-capability-kind", current["payload"]["kind"])
        # The link's integrity target is still an Evidence-kind metadata object,
        # but its Task, subject, and capability-owned payload kind are not
        # treated as Kernel-level replacement or applicability semantics.
        self.assertEqual(
            "different-capability-kind",
            self.reader.read(commit, prior_id, task="218", subject="b" * 40)["payload"]["kind"],
        )

    def test_missing_wrong_kind_and_malformed_supersedes_targets_fail_closed(self) -> None:
        missing_id = "a" * 64
        missing_root_id, missing_root_data = self._encode(supersedes=missing_id)
        missing_commit = self.store.publish([missing_root_data])
        with self.assertRaises(MetadataRefError):
            self.reader.read(missing_commit, missing_root_id, task="217", subject=self.subject)

        contract_id, contract_data = fixtures.codec.encode_object(
            "contract", _REPOSITORY, "217", self.subject, {"opaque": True}
        )
        wrong_kind_id, wrong_kind_data = self._encode(supersedes=contract_id)
        wrong_kind_commit = self.store.publish([contract_data, wrong_kind_data])
        with self.assertRaises(MetadataRefError):
            self.reader.read(wrong_kind_commit, wrong_kind_id, task="217", subject=self.subject)

        malformed_target_id, malformed_target_data = fixtures.codec.encode_object(
            "evidence",
            _REPOSITORY,
            "217",
            self.subject,
            {"schema_version": True, "kind": "broken", "producer": {}, "created_at": "bad", "payload": {}},
        )
        malformed_root_id, malformed_root_data = self._encode(supersedes=malformed_target_id)
        malformed_commit = self.store.publish([malformed_target_data, malformed_root_data])
        with self.assertRaises(evidence_module.EvidenceError):
            self.reader.read(malformed_commit, malformed_root_id, task="217", subject=self.subject)

    def test_missing_link_in_a_supersession_chain_is_not_fetched_from_elsewhere(self) -> None:
        absent_id = "b" * 64
        intermediate_id, intermediate_data = self._encode(supersedes=absent_id)
        root_id, root_data = self._encode(supersedes=intermediate_id)
        commit = self.store.publish([intermediate_data, root_data])

        with self.assertRaises(MetadataRefError):
            self.reader.read(commit, root_id, task="217", subject=self.subject)

    def test_supersedes_target_added_later_cannot_satisfy_an_older_pinned_read(self) -> None:
        target_id, target_data = self._encode(payload={"fact": "target"})
        root_id, root_data = self._encode(supersedes=target_id, payload={"fact": "root"})
        pinned_without_target = self.store.publish([root_data])

        with self.assertRaises(MetadataRefError):
            self.reader.read(pinned_without_target, root_id, task="217", subject=self.subject)

        later_commit = self.store.publish([target_data])
        with self.assertRaises(MetadataRefError):
            self.reader.read(pinned_without_target, root_id, task="217", subject=self.subject)
        self.assertEqual(
            "root",
            self.reader.read(later_commit, root_id, task="217", subject=self.subject)["payload"]["payload"]["fact"],
        )

    def test_supersession_chain_is_bounded_to_64_links(self) -> None:
        terminal_id, terminal_data = self._encode(payload={"step": "terminal"})
        objects = [terminal_data]
        prior_id = terminal_id
        for index in range(evidence_module.MAX_SUPERSESSION_LINKS):
            prior_id, data = self._encode(supersedes=prior_id, payload={"step": index})
            objects.append(data)
        within_limit = self.store.publish(objects)
        # Exercise the walk bound without 64 redundant transport/history reads;
        # exact pinned-tree lookup itself is covered by the smaller graph tests.
        objects_by_id = {
            hashlib.sha256(_OBJECT_DOMAIN + data).hexdigest(): fixtures.codec.decode_object(data)
            for data in objects
        }
        with mock.patch.object(self.store, "read_object_by_id", side_effect=lambda _commit, _kind, object_id: objects_by_id[object_id]):
            within = self.reader.read(within_limit, prior_id, task="217", subject=self.subject)
            self.assertEqual(63, within["payload"]["payload"]["step"])

        over_limit_id, over_limit_data = self._encode(supersedes=prior_id, payload={"step": "over-limit"})
        over_limit_commit = self.store.publish([over_limit_data])
        with mock.patch.object(self.store, "read_object_by_id", side_effect=lambda _commit, _kind, object_id: objects_by_id[object_id]):
            with self.assertRaisesRegex(evidence_module.EvidenceError, "exceeds the validation limit"):
                self.reader.read(over_limit_commit, over_limit_id, task="217", subject=self.subject)

    def test_supersession_validation_uses_a_bounded_deadline(self) -> None:
        prior_id, prior_data = self._encode(payload={"fact": "prior"})
        object_id, data = self._encode(supersedes=prior_id)
        commit = self.store.publish([prior_data, data])
        deadlines = []

        def consume_budget(*args, **kwargs):
            value = fixtures.codec.decode_object(data)
            deadlines.append(self.store._validation_deadline.get())
            time.sleep(0.06)
            return value

        original_target = self.store.read_object_by_id

        def linked_read(*args, **kwargs):
            self.assertEqual(deadlines[0], self.store._validation_deadline.get())
            return original_target(*args, **kwargs)

        with mock.patch.object(fixtures.metadata_ref_module, "MAX_HISTORY_VALIDATION_SECONDS", 0.05), mock.patch.object(
            self.store, "read_object", side_effect=consume_budget
        ), mock.patch.object(self.store, "read_object_by_id", side_effect=linked_read):
            with self.assertRaisesRegex(MetadataRefError, "validation time limit"):
                self.reader.read(commit, object_id, task="217", subject=self.subject)
        self.assertIsNone(self.store._validation_deadline.get())
        self.assertEqual(1, len(deadlines))

    def test_subject_advancement_requires_new_exact_binding_and_old_evidence_stays_readable(self) -> None:
        old_id, old_data = self._encode(payload={"subject": "old"})
        old_metadata_commit = self.store.publish([old_data])

        (self.product / "tracked.txt").write_text("advance product subject\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        fixtures.git("commit", "-m", "advance product subject", cwd=self.product)
        new_subject = fixtures.git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        self.assertNotEqual(self.subject, new_subject)

        new_id, new_data = self._encode(
            subject=new_subject, payload={"subject": "new"}, supersedes=old_id
        )
        new_metadata_commit = self.store.publish([new_data])
        self.assertEqual("new", self.reader.read(new_metadata_commit, new_id, task="217", subject=new_subject)["payload"]["payload"]["subject"])
        self.assertEqual("old", self.reader.read(new_metadata_commit, old_id, task="217", subject=self.subject)["payload"]["payload"]["subject"])
        self.assertEqual("old", self.reader.read(old_metadata_commit, old_id, task="217", subject=self.subject)["payload"]["payload"]["subject"])
        with self.assertRaises(MetadataRefError):
            self.reader.read(new_metadata_commit, old_id, task="217", subject=new_subject)

    def test_clean_clone_reads_evidence_without_the_original_producer_or_runtime(self) -> None:
        object_id, data = self._encode(
            producer={"name": "offline producer", "runtime": "private-and-absent"},
            payload={"fact": "durable"},
        )
        commit = self.store.publish([data])
        clean_clone = self.temp_root / "evidence-clean-clone"
        fixtures.git("clone", "--no-checkout", str(self.product), str(clean_clone))
        fixtures.git("remote", "set-url", "origin", str(self.remote_path), cwd=clean_clone)
        clone_store = fixtures.metadata_ref_module.MetadataStore(clean_clone, _REPOSITORY)
        clone_reader = evidence_module.Evidence(clone_store)

        self.assertEqual(commit, clone_store.fetch_tip())
        resolved = clone_reader.read(commit, object_id, task="217", subject=self.subject)
        self.assertEqual("durable", resolved["payload"]["payload"]["fact"])
        self.assertEqual("private-and-absent", resolved["payload"]["producer"]["runtime"])

    def test_product_head_tree_index_and_dirty_files_are_unchanged_on_success(self) -> None:
        (self.product / "tracked.txt").write_text("staged product edit\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        (self.product / "tracked.txt").write_text("staged plus unstaged product edit\n", encoding="utf-8")
        (self.product / "untracked.txt").write_text("untracked stays local\n", encoding="utf-8")
        before = self._product_state()
        untracked_before = (self.product / "untracked.txt").read_bytes()
        object_id, data = self._encode()

        commit = self.store.publish([data])

        self.assertEqual(before, self._product_state())
        self.assertEqual(untracked_before, (self.product / "untracked.txt").read_bytes())
        self.assertEqual(commit, self._remote_tip())
        self.assertEqual(
            "evidence",
            self.reader.read(commit, object_id, task="217", subject=self.subject)["kind"],
        )

    def test_product_head_tree_index_and_dirty_files_are_unchanged_on_failed_publish(self) -> None:
        (self.product / "tracked.txt").write_text("staged failure edit\n", encoding="utf-8")
        fixtures.git("add", "tracked.txt", cwd=self.product)
        (self.product / "tracked.txt").write_text("unstaged failure edit\n", encoding="utf-8")
        (self.product / "untracked.txt").write_text("untracked failure stays local\n", encoding="utf-8")
        before = self._product_state()
        untracked_before = (self.product / "untracked.txt").read_bytes()
        untrusted_store = fixtures.metadata_ref_module.MetadataStore(self.product, _REPOSITORY)
        untrusted_reader = evidence_module.Evidence(untrusted_store)
        _object_id, data = self._encode()

        with self.assertRaisesRegex(MetadataRefError, "trusted direct-ref CAS capability"):
            untrusted_store.publish([data])

        self.assertFalse(hasattr(untrusted_reader, "publish"))
        self.assertEqual(before, self._product_state())
        self.assertEqual(untracked_before, (self.product / "untracked.txt").read_bytes())
        self.assertIsNone(self._remote_tip())

if __name__ == "__main__":
    unittest.main()
