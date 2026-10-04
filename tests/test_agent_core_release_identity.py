from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "agent_core_release_identity.py"
CONTRACT_PATH = ROOT / "distribution" / "release-identity.json"
spec = importlib.util.spec_from_file_location("agent_core_release_identity", MODULE_PATH)
assert spec and spec.loader
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class AgentCoreReleaseIdentityTest(unittest.TestCase):
    def contract(
        self,
        *,
        agent_version: str = "1.0.0",
        templates_version: str = "4.0.0",
        status: str = "candidate",
    ) -> dict:
        return {
            "schema": "agent-core-release-identity",
            "schemaVersion": 1,
            "agentCore": {
                "version": agent_version,
                "architecture": "v4",
                "status": status,
            },
            "templates": {"version": templates_version},
        }

    @staticmethod
    def prior(
        *,
        agent_version: str = "1.0.0",
        payload: str = "a" * 40,
        templates_version: str = "4.0.0",
        source_revision: str = "b" * 40,
    ) -> dict[str, str]:
        return {
            "agentCoreVersion": agent_version,
            "agentCorePayload": payload,
            "templatesVersion": templates_version,
            "templatesSourceRevision": source_revision,
        }

    def evaluate(
        self,
        contract: dict | None = None,
        *,
        installed: str = "1.0.0",
        payload: str = "a" * 40,
        source_revision: str = "c" * 40,
        tag: str = "v4.0.0",
        prior: dict | None = None,
    ) -> dict[str, str]:
        return identity.evaluate_release(
            self.contract() if contract is None else contract,
            installed,
            payload,
            source_revision,
            tag,
            prior,
        )

    def test_release_intent_metadata_records_planned_v4_release_separately(self) -> None:
        raw = CONTRACT_PATH.read_bytes()
        contract = identity.parse_contract(raw)
        self.assertEqual("agent-core-release-identity", contract["schema"])
        self.assertEqual(1, contract["schemaVersion"])
        self.assertEqual(
            {"version": "1.0.0", "architecture": "v4", "status": "planned"},
            contract["agentCore"],
        )
        self.assertEqual({"version": "4.0.0"}, contract["templates"])
        self.assertEqual(
            "3",
            (ROOT / "components/agent-core/.automation/VERSION").read_text().strip(),
        )

    def test_first_stable_pair_is_bound_to_payload_tree_and_source_revision(self) -> None:
        result = self.evaluate()
        self.assertEqual(
            {
                "agentCoreVersion": "1.0.0",
                "agentCorePayload": "a" * 40,
                "templatesVersion": "4.0.0",
                "templatesSourceRevision": "c" * 40,
            },
            result,
        )

    def test_planned_contract_does_not_pass_even_for_first_stable_pair(self) -> None:
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "status must be candidate"):
            self.evaluate(self.contract(status="planned"))

    def test_released_contract_is_not_a_new_release_candidate(self) -> None:
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "status must be candidate"):
            self.evaluate(self.contract(status="released"))

    def test_first_stable_pair_is_required_without_prior_release(self) -> None:
        for agent_version, templates_version in (
            ("1.0.1", "4.0.0"),
            ("1.0.0", "4.0.1"),
            ("2.0.0", "5.0.0"),
        ):
            with self.subTest(agent_version=agent_version, templates_version=templates_version):
                with self.assertRaisesRegex(identity.ReleaseIdentityError, "without a verified prior"):
                    self.evaluate(
                        self.contract(
                            agent_version=agent_version,
                            templates_version=templates_version,
                        ),
                        installed=agent_version,
                        tag=f"v{templates_version}",
                    )

    def test_installed_marker_must_be_exact_stable_agentcore_semver(self) -> None:
        for marker in ("3", "2", "1", "1.0.0\n", "01.0.0", "1.0.0-rc.1", "1.0.0+build"):
            with self.subTest(marker=marker):
                with self.assertRaises(identity.ReleaseIdentityError):
                    self.evaluate(installed=marker)
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "does not match"):
            self.evaluate(installed="1.0.1")

    def test_proposed_templates_tag_must_exactly_match_contract_version(self) -> None:
        for tag in ("4.0.0", "v4", "v04.0.0", "v4.0.0-rc.1", "v4.0.0+build"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(identity.ReleaseIdentityError, "proposed Templates tag"):
                    self.evaluate(tag=tag)

    def test_next_templates_release_can_leave_agentcore_payload_and_version_unchanged(self) -> None:
        result = self.evaluate(
            self.contract(templates_version="4.1.0"),
            tag="v4.1.0",
            prior=self.prior(),
        )
        self.assertEqual("1.0.0", result["agentCoreVersion"])
        self.assertEqual("a" * 40, result["agentCorePayload"])
        self.assertEqual("4.1.0", result["templatesVersion"])

    def test_templates_version_must_increment_from_prior(self) -> None:
        for templates_version in ("3.9.9", "4.0.0"):
            with self.subTest(templates_version=templates_version):
                with self.assertRaisesRegex(identity.ReleaseIdentityError, "Templates version must increment"):
                    self.evaluate(
                        self.contract(templates_version=templates_version),
                        tag=f"v{templates_version}",
                        prior=self.prior(),
                    )

    def test_replaying_agentcore_version_with_different_payload_is_rejected(self) -> None:
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "unchanged AgentCore version"):
            self.evaluate(
                self.contract(templates_version="4.1.0"),
                payload="d" * 40,
                tag="v4.1.0",
                prior=self.prior(),
            )

    def test_bumping_agentcore_version_without_payload_change_is_rejected(self) -> None:
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "changed AgentCore version"):
            self.evaluate(
                self.contract(agent_version="1.0.1", templates_version="4.1.0"),
                installed="1.0.1",
                tag="v4.1.0",
                prior=self.prior(),
            )

    def test_agentcore_version_increase_with_changed_payload_is_allowed(self) -> None:
        result = self.evaluate(
            self.contract(agent_version="2.0.0", templates_version="5.0.0"),
            installed="2.0.0",
            payload="d" * 40,
            tag="v5.0.0",
            prior=self.prior(),
        )
        self.assertEqual("2.0.0", result["agentCoreVersion"])
        self.assertEqual("d" * 40, result["agentCorePayload"])

    def test_agentcore_version_must_not_decrease(self) -> None:
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "must not decrease"):
            self.evaluate(
                self.contract(agent_version="0.9.0", templates_version="4.1.0"),
                installed="0.9.0",
                payload="d" * 40,
                tag="v4.1.0",
                prior=self.prior(),
            )

    def test_semver_comparison_is_numeric_not_lexical(self) -> None:
        result = self.evaluate(
            self.contract(agent_version="1.10.0", templates_version="4.10.0"),
            installed="1.10.0",
            payload="d" * 40,
            tag="v4.10.0",
            prior=self.prior(agent_version="1.9.0", templates_version="4.9.0"),
        )
        self.assertEqual("1.10.0", result["agentCoreVersion"])

    def test_legacy_integer_versions_prereleases_builds_and_leading_zeros_are_rejected(self) -> None:
        invalid_versions = (
            2,
            3,
            "2",
            "3",
            "01.0.0",
            "1.00.0",
            "1.0.01",
            "1.0.0-rc.1",
            "1.0.0+meta",
        )
        for version in invalid_versions:
            with self.subTest(version=version):
                contract = self.contract()
                contract["agentCore"]["version"] = version
                with self.assertRaises(identity.ReleaseIdentityError):
                    identity.parse_contract(json.dumps(contract).encode())

                contract = self.contract()
                contract["templates"]["version"] = version
                with self.assertRaises(identity.ReleaseIdentityError):
                    identity.parse_contract(json.dumps(contract).encode())

    def test_contract_requires_exact_schema_keys_and_types(self) -> None:
        invalid_contracts = []
        extra_top = self.contract()
        extra_top["distributionManifestVersion"] = 1
        invalid_contracts.append(extra_top)
        extra_agent_core = self.contract()
        extra_agent_core["agentCore"]["released"] = True
        invalid_contracts.append(extra_agent_core)
        invalid_contracts.extend(
            (
                {**self.contract(), "schemaVersion": True},
                {**self.contract(), "schemaVersion": "1"},
                {**self.contract(), "schema": "template-repositories"},
                {**self.contract(), "agentCore": {"version": "1.0.0", "architecture": "v3", "status": "candidate"}},
                {**self.contract(), "agentCore": {"version": "1.0.0", "architecture": "v4", "status": True}},
            )
        )
        for contract in invalid_contracts:
            with self.subTest(contract=contract):
                with self.assertRaises(identity.ReleaseIdentityError):
                    identity.parse_contract(json.dumps(contract).encode())

    def test_duplicate_json_keys_are_rejected_at_every_object_depth(self) -> None:
        raw = (
            b'{"schema":"agent-core-release-identity","schema":"agent-core-release-identity",'
            b'"schemaVersion":1,"agentCore":{"version":"1.0.0","architecture":"v4",'
            b'"status":"candidate"},"templates":{"version":"4.0.0"}}'
        )
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "duplicate key"):
            identity.parse_contract(raw)

        nested_duplicate = (
            b'{"schema":"agent-core-release-identity","schemaVersion":1,"agentCore":'
            b'{"version":"1.0.0","version":"1.0.0","architecture":"v4",'
            b'"status":"candidate"},"templates":{"version":"4.0.0"}}'
        )
        with self.assertRaisesRegex(identity.ReleaseIdentityError, "duplicate key"):
            identity.parse_contract(nested_duplicate)

    def test_invalid_json_encoding_constants_and_non_byte_input_are_rejected(self) -> None:
        for raw in (b"\xff", b"{} trailing", b"NaN"):
            with self.subTest(raw=raw):
                with self.assertRaises(identity.ReleaseIdentityError):
                    identity.parse_contract(raw)
        with self.assertRaises(identity.ReleaseIdentityError):
            identity.parse_contract(bytearray(b"{}"))  # type: ignore[arg-type]

    def test_payload_and_source_revision_must_be_lowercase_40_hex_identities(self) -> None:
        for kwargs in (
            {"payload": "a" * 39},
            {"payload": "A" * 40},
            {"payload": "g" * 40},
            {"source_revision": "c" * 41},
            {"source_revision": "C" * 40},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(identity.ReleaseIdentityError):
                    self.evaluate(**kwargs)

    def test_prior_identity_must_have_exact_keys_and_valid_stable_values(self) -> None:
        invalid_prior_values = (
            {**self.prior(), "unexpected": "value"},
            {**self.prior(), "agentCoreVersion": "3"},
            {**self.prior(), "agentCoreVersion": "1.0.0-rc.1"},
            {**self.prior(), "agentCorePayload": "A" * 40},
            {key: value for key, value in self.prior().items() if key != "templatesVersion"},
        )
        for prior in invalid_prior_values:
            with self.subTest(prior=prior):
                with self.assertRaises(identity.ReleaseIdentityError):
                    self.evaluate(
                        self.contract(templates_version="4.1.0"),
                        tag="v4.1.0",
                        prior=prior,
                    )


if __name__ == "__main__":
    unittest.main()
