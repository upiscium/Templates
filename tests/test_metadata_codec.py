from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components/agent-core-v4/metadata_codec.py"
SPEC = importlib.util.spec_from_file_location("agent_core_v4_metadata_codec", MODULE_PATH)
assert SPEC and SPEC.loader
codec = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codec
SPEC.loader.exec_module(codec)


class MetadataCodecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "body": "opaque payload — café",
            "nested": {"enabled": True, "items": [None, -3, "x"]},
        }
        self.object_id, self.data = codec.encode_object(
            "task-record", "acme/widgets", "217", "a" * 40, self.payload
        )

    def test_encoding_is_deterministic_domain_separated_and_decodes_to_a_dict(self) -> None:
        first = codec.encode_object(
            "task-record", "acme/widgets", "217", "a" * 40, self.payload
        )
        second = codec.encode_object(
            "task-record",
            "acme/widgets",
            "217",
            "a" * 40,
            {"nested": {"items": [None, -3, "x"], "enabled": True}, "body": "opaque payload — café"},
        )
        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(b"agentcore-metadata-object/v1\n" + self.data).hexdigest(),
            self.object_id,
        )
        self.assertIn("opaque payload — café".encode("utf-8"), self.data)
        self.assertNotIn(b"\\u", self.data)

        decoded = codec.decode_object(
            self.data,
            expected_id=self.object_id,
            expected_repository="acme/widgets",
            expected_task="217",
            expected_subject="a" * 40,
            expected_kind="task-record",
        )
        self.assertEqual(
            {
                "schema_version": 1,
                "kind": "task-record",
                "repository": "acme/widgets",
                "task": "217",
                "subject": "a" * 40,
                "payload": self.payload,
            },
            decoded,
        )

    def test_all_four_kinds_and_both_full_oid_lengths_are_supported(self) -> None:
        for kind in sorted(codec.KINDS):
            for subject in ("b" * 40, "c" * 64):
                with self.subTest(kind=kind, oid_length=len(subject)):
                    object_id, data = codec.encode_object(
                        kind, "owner/repo", "1", subject, {}
                    )
                    self.assertEqual(kind, codec.decode_object(data, expected_id=object_id)["kind"])
        with self.assertRaises(codec.MetadataCodecError):
            codec.encode_object("evidence", "owner/repo", "1", "A" * 40, {})

    def test_wire_vectors_pin_utf8_key_order_escaping_and_all_four_kinds(self) -> None:
        vectors = {
            "task-record": "36ae6913680d88633f3c245794eb65402b9842c5383b4f8c0cb241a4132dee65",
            "contract": "acf5b4aff65a67f0f7c91da49e14ffc0ad49ba858a649c5412d4ae3074f21caa",
            "evidence": "c465dbb11eee6082c802177fe8735d72f623721de5490a070fa465042dce03e8",
            "task-view-snapshot": "09fd01520a9c8baabf183d5d1c03a2e35b693ba0ab6a0a2370668d671c1743e5",
        }
        for kind, expected_id in vectors.items():
            with self.subTest(kind=kind):
                object_id, data = codec.encode_object(
                    kind, "acme/widgets", "217", "a" * 40,
                    {"𐀀": -7, "é": 'line\nquote"'},
                )
                expected_data = (
                    '{"kind":"' + kind + '","payload":{"é":"line\\nquote\\\"",'
                    '"𐀀":-7},"repository":"acme/widgets","schema_version":1,'
                    '"subject":"' + "a" * 40 + '","task":"217"}'
                ).encode("utf-8")
                self.assertEqual(expected_data, data)
                self.assertEqual(expected_id, object_id)
                self.assertEqual(
                    f"objects/{kind}/{expected_id[:2]}/{expected_id}.json",
                    codec.object_path(kind, object_id),
                )

    def test_encode_rejects_malformed_identities_and_unknown_kind(self) -> None:
        invalid_cases = (
            ("unknown", "acme/widgets", "217", "a" * 40, {}),
            ("task-record", "acme", "217", "a" * 40, {}),
            ("task-record", "/widgets", "217", "a" * 40, {}),
            ("task-record", "acme/../widgets", "217", "a" * 40, {}),
            ("task-record", "acme\\widgets", "217", "a" * 40, {}),
            ("task-record", "acme/widgets/extra", "217", "a" * 40, {}),
            ("task-record", "acme/widgets", "0", "a" * 40, {}),
            ("task-record", "acme/widgets", "0217", "a" * 40, {}),
            ("task-record", "acme/widgets", 217, "a" * 40, {}),
            ("task-record", "acme/widgets", "217", "a" * 39, {}),
        )
        for arguments in invalid_cases:
            with self.subTest(arguments=arguments), self.assertRaises(codec.MetadataCodecError):
                codec.encode_object(*arguments)

    def test_payload_accepts_only_bounded_json_primitives_and_object_root(self) -> None:
        bad_payloads = (
            [],
            {"float": 1.0},
            {"nan": float("nan")},
            {"integer": 2**63},
            {"integer": -(2**63) - 1},
            {"surrogate": "\ud800"},
            {1: "non-string key"},
            {"tuple": (1, 2)},
        )
        for payload in bad_payloads:
            with self.subTest(payload=repr(payload)), self.assertRaises(codec.MetadataCodecError):
                codec.encode_object("evidence", "acme/widgets", "217", "a" * 40, payload)
        self.assertEqual(
            -(2**63),
            codec.decode_object(
                codec.encode_object("evidence", "acme/widgets", "217", "a" * 40, {"min": -(2**63)})[1]
            )["payload"]["min"],
        )

    def test_oversized_payload_is_rejected_and_exact_limit_is_accepted(self) -> None:
        # The compact JSON for {"x":"..."} adds eight ASCII bytes.
        at_limit = {"x": "a" * (codec.MAX_PAYLOAD_BYTES - 8)}
        _object_id, data = codec.encode_object("contract", "acme/widgets", "217", "a" * 40, at_limit)
        self.assertEqual(at_limit, codec.decode_object(data)["payload"])
        too_large = {"x": "a" * (codec.MAX_PAYLOAD_BYTES - 7)}
        with self.assertRaisesRegex(codec.MetadataCodecError, "1 MiB"):
            codec.encode_object("contract", "acme/widgets", "217", "a" * 40, too_large)

    def test_decode_rejects_oversize_before_parsing_and_deep_payloads(self) -> None:
        with self.assertRaisesRegex(codec.MetadataCodecError, "envelope byte limit"):
            codec.decode_object(b"!" * (codec.MAX_OBJECT_BYTES + 1))
        deep = {}
        for _ in range(codec.MAX_JSON_DEPTH - 1):
            deep = {"child": deep}
        _, at_limit = codec.encode_object("evidence", "acme/widgets", "1", "a" * 40, deep)
        self.assertEqual(deep, codec.decode_object(at_limit)["payload"])
        deep = {"child": deep}
        with self.assertRaisesRegex(codec.MetadataCodecError, "nesting"):
            codec.encode_object("evidence", "acme/widgets", "1", "a" * 40, deep)

    def test_bounded_wide_object_does_not_expand_long_key_into_every_diagnostic(self) -> None:
        # This is well below the 1 MiB wire limit, but rendering the 200 KiB
        # ancestor key for each child used to create gigabytes of string work.
        payload = {"k" * 200_000: {str(index): 0 for index in range(12_000)}}
        object_id, data = codec.encode_object("evidence", "acme/widgets", "1", "a" * 40, payload)
        self.assertLess(len(data), codec.MAX_OBJECT_BYTES)
        self.assertEqual(payload, codec.decode_object(data, expected_id=object_id)["payload"])

    def test_decode_rejects_unknown_versions_extra_fields_and_missing_fields(self) -> None:
        envelope = json.loads(self.data)
        for changed in (
            {**envelope, "schema_version": 2},
            {**envelope, "extra": True},
            {key: value for key, value in envelope.items() if key != "subject"},
        ):
            data = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            with self.subTest(envelope=changed), self.assertRaises(codec.MetadataCodecError):
                codec.decode_object(data)

    def test_decode_rejects_noncanonical_json_bytes(self) -> None:
        canonical_object = json.loads(self.data)
        reverse_order = dict(reversed(tuple(canonical_object.items())))
        variants = (
            self.data + b"\n",
            b" " + self.data,
            json.dumps(reverse_order, ensure_ascii=False, separators=(",", ":")).encode(),
            self.data.replace("café".encode(), b"caf\\u00e9"),
            self.data.replace(b"{", b"{\"kind\":\"task-record\",", 1),
            b"\xff",
            b"{\"x\":1,\"x\":1}",
            b"{\"x\":1.0}",
            b"{\"x\":NaN}",
            b"{\"x\":\"\\ud800\"}",
        )
        for data in variants:
            with self.subTest(data=data[:80]), self.assertRaises(codec.MetadataCodecError):
                codec.decode_object(data)

    def test_decode_verifies_digest_and_each_expected_binding(self) -> None:
        changed = json.loads(self.data)
        changed["payload"]["body"] = "tampered"
        tampered_data = json.dumps(
            changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with self.assertRaisesRegex(codec.MetadataCodecError, "expected_id"):
            codec.decode_object(tampered_data, expected_id=self.object_id)

        for binding in (
            {"expected_repository": "other/widgets"},
            {"expected_task": "218"},
            {"expected_subject": "b" * 40},
            {"expected_kind": "evidence"},
        ):
            with self.subTest(binding=binding), self.assertRaisesRegex(codec.MetadataCodecError, "expected binding"):
                codec.decode_object(self.data, **binding)

    def test_object_path_validates_kind_and_digest_and_uses_two_hex_prefix(self) -> None:
        self.assertEqual(
            f"objects/task-record/{self.object_id[:2]}/{self.object_id}.json",
            codec.object_path("task-record", self.object_id),
        )
        for kind, object_id in (
            ("../../outside", self.object_id),
            ("evidence", "../" + self.object_id),
            ("evidence", "A" * 64),
            ("evidence", "a" * 63),
        ):
            with self.subTest(kind=kind, object_id=object_id), self.assertRaises(codec.MetadataCodecError):
                codec.object_path(kind, object_id)


if __name__ == "__main__":
    unittest.main()
