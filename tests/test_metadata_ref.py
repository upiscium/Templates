from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
V4_COMPONENTS = ROOT / "components/agent-core-v4"
sys.path.insert(0, str(V4_COMPONENTS))

import metadata_codec as codec  # noqa: E402
import metadata_ref as metadata_ref_module  # noqa: E402
from metadata_ref import MetadataConflictError, MetadataRefError, MetadataStore  # noqa: E402


def git(
    *arguments: str,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    if extra_env:
        environment.update(extra_env)
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: "
            f"{completed.stderr.decode('utf-8', errors='replace')}"
        )
    return completed.stdout


class MetadataRefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="metadata-ref-test-")
        self.addCleanup(self.temporary.cleanup)
        self.temp_root = Path(self.temporary.name)
        self.remote_path = self.temp_root / "metadata-remote.git"
        self.product = self.temp_root / "product"
        self.product.mkdir()

        git("init", "--bare", "--object-format=sha1", str(self.remote_path))
        # The fixture models the required host-side guarantee at the prepared
        # ref-transaction boundary, after Git has locked the ref. A client
        # advertisement or a pre-receive-only check is not atomic with CAS.
        transaction_hook = self.remote_path / "hooks" / "reference-transaction"
        transaction_hook.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = prepared ] || exit 0\n"
            "while read -r old new ref; do\n"
            "  if [ \"$ref\" = refs/agentcore/metadata ] && "
            "git symbolic-ref -q refs/agentcore/metadata >/dev/null 2>&1; then\n"
            "    exit 1\n"
            "  fi\n"
            "done\n",
            encoding="utf-8",
        )
        transaction_hook.chmod(0o755)
        git("init", "--object-format=sha1", cwd=self.product)
        git("config", "user.name", "Metadata Ref Test", cwd=self.product)
        git("config", "user.email", "metadata-ref-test@example.invalid", cwd=self.product)
        (self.product / "tracked.txt").write_text("product tree stays untouched\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=self.product)
        git("commit", "-m", "product fixture", cwd=self.product)
        git("remote", "add", "origin", str(self.remote_path), cwd=self.product)
        self.store = self._writer()

    def _writer(
        self,
        root: Path | None = None,
        *,
        remote: str = "origin",
        bare_remote: Path | None = None,
    ) -> MetadataStore:
        source = self.product if root is None else root
        destination = self.remote_path if bare_remote is None else bare_remote
        return MetadataStore(
            source,
            "acme/widgets",
            remote,
            direct_ref_cas=self._fixture_direct_ref_cas(source, destination),
        )

    @staticmethod
    def _git_dir(remote: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        return subprocess.run(
            ["git", "--git-dir", str(remote), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

    def _fixture_direct_ref_cas(
        self, source: Path, bare_remote: Path
    ) -> Callable[[str, str | None], bool]:
        """Transfer fixture objects, then atomically CAS only the direct test ref."""
        def direct_ref_cas(candidate_oid: str, expected_old_oid: str | None) -> bool:
            parent = git(
                "show", "-s", "--format=%P", candidate_oid, cwd=source
            ).decode("ascii").strip()
            if parent != (expected_old_oid or ""):
                return False

            advertised_symref = self._git_dir(
                bare_remote,
                "symbolic-ref",
                "-q",
                metadata_ref_module.METADATA_REF,
            )
            if advertised_symref.returncode != 1:
                return False

            transfer_ref = f"refs/metadata-ref-test/transfer/{candidate_oid}"
            try:
                try:
                    git(
                        "-c",
                        "core.hooksPath=/dev/null",
                        "push",
                        "--no-follow-tags",
                        str(bare_remote),
                        f"{candidate_oid}:{transfer_ref}",
                        cwd=source,
                    )
                except AssertionError:
                    return False

                expected = expected_old_oid or "0" * len(candidate_oid)
                result = self._git_dir(
                    bare_remote,
                    "update-ref",
                    "--no-deref",
                    metadata_ref_module.METADATA_REF,
                    candidate_oid,
                    expected,
                )
                return result.returncode == 0
            finally:
                # The transfer ref exists only to move objects into the bare
                # fixture. It is never a publication destination and is
                # removed whether the final canonical-ref CAS succeeds or not.
                self._git_dir(bare_remote, "update-ref", "-d", transfer_ref)
                remaining = self._git_dir(
                    bare_remote, "show-ref", "--verify", transfer_ref
                )
                if remaining.returncode == 0:
                    raise AssertionError("fixture transfer ref was not removed")

        return direct_ref_cas

    def _object(self, kind: str, task: str, payload: dict[str, Any]) -> tuple[str, bytes]:
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        return codec.encode_object(kind, "acme/widgets", task, subject, payload)

    def _remote_tip(self) -> str | None:
        completed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote_path),
                "show-ref",
                "--verify",
                "--hash",
                "refs/agentcore/metadata",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.decode("ascii").strip()

    def _product_state(self) -> tuple[bytes, ...]:
        index_path_text = (
            git("rev-parse", "--git-path", "index", cwd=self.product)
            .decode("utf-8")
            .strip()
        )
        index_path = Path(index_path_text)
        if not index_path.is_absolute():
            index_path = self.product / index_path
        index_data = index_path.read_bytes() if index_path.exists() else b"<no-index>"
        return (
            git("rev-parse", "HEAD", cwd=self.product),
            git("write-tree", cwd=self.product),
            git("ls-files", "--stage", "--debug", cwd=self.product),
            git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.product),
            index_data,
            git("status", "--porcelain=v1", "--untracked-files=all", cwd=self.product),
            (self.product / "tracked.txt").read_bytes(),
        )

    def _create_remote_child(
        self,
        parent: str,
        *,
        additions: tuple[tuple[str, bytes, str], ...] = (),
        deletions: tuple[str, ...] = (),
    ) -> str:
        parent_tree = (
            git("rev-parse", f"{parent}^{{tree}}", cwd=self.product)
            .decode("ascii")
            .strip()
        )
        with tempfile.TemporaryDirectory(prefix="metadata-ref-test-index-") as directory:
            index = Path(directory) / "index"
            env = {"GIT_INDEX_FILE": str(index)}
            git("read-tree", parent_tree, cwd=self.product, extra_env=env)
            for path, data, mode in additions:
                blob = (
                    git("rev-parse", "HEAD", cwd=self.product)
                    if mode == "160000"
                    else git("hash-object", "-w", "--stdin", cwd=self.product, input_data=data)
                ).decode("ascii").strip()
                git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode},{blob},{path}",
                    cwd=self.product,
                    extra_env=env,
                )
            for path in deletions:
                git("update-index", "--force-remove", "--", path, cwd=self.product, extra_env=env)
            tree = git("write-tree", cwd=self.product, extra_env=env).decode("ascii").strip()
        commit = git(
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            "test metadata mutation",
            cwd=self.product,
        ).decode("ascii").strip()
        self._transfer_fixture_commit(commit)
        return commit

    def _transfer_fixture_commit(self, commit: str) -> None:
        fixture_ref = "refs/metadata-ref-test/transfer"
        try:
            git(
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                str(self.remote_path),
                f"{commit}:{fixture_ref}",
                cwd=self.product,
            )
        finally:
            self._git_dir(self.remote_path, "update-ref", "-d", fixture_ref)
            remaining = self._git_dir(
                self.remote_path, "show-ref", "--verify", fixture_ref
            )
            if remaining.returncode == 0:
                raise AssertionError("fixture transfer ref was not removed")

    def _set_remote_tip(self, new_tip: str, old_tip: str) -> None:
        git(
            "--git-dir",
            str(self.remote_path),
            "update-ref",
            "refs/agentcore/metadata",
            new_tip,
            old_tip,
        )

    def _create_unrelated_genesis(self) -> str:
        empty_tree = git(
            "hash-object", "-t", "tree", "-w", "--stdin", cwd=self.product, input_data=b""
        ).decode("ascii").strip()
        commit = git(
            "commit-tree", empty_tree, "-m", "unrelated metadata genesis", cwd=self.product
        ).decode("ascii").strip()
        self._transfer_fixture_commit(commit)
        return commit

    def test_genesis_fetch_clean_clone_read_cas_and_product_state(self) -> None:
        before = self._product_state()
        hook_marker = self.temp_root / "product-pre-push-hook-ran"
        product_hooks = self.product / ".git" / "hooks"
        product_hooks.mkdir(exist_ok=True)
        pre_push = product_hooks / "pre-push"
        pre_push.write_text(f"#!/bin/sh\ntouch '{hook_marker}'\n", encoding="utf-8")
        pre_push.chmod(0o755)
        first_id, first_data = self._object("task-record", "217", {"title": "initial"})
        first_commit = self.store.publish([first_data], {"217": (None, first_id)})

        self.assertFalse(hook_marker.exists())
        self.assertEqual(first_commit, self._remote_tip())
        first_parents = git(
            "--git-dir",
            str(self.remote_path),
            "rev-list",
            "--parents",
            "-n",
            "1",
            first_commit,
        )
        self.assertEqual(1, len(first_parents.split()))
        self.assertEqual(first_commit, self.store.fetch_tip())
        self.assertEqual(
            {"title": "initial"},
            self.store.read_object(
                first_commit,
                "task-record",
                first_id,
                task="217",
                subject=git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
            )["payload"],
        )

        clean_clone = self.temp_root / "clean-clone"
        git("clone", "--no-checkout", str(self.product), str(clean_clone))
        git("remote", "set-url", "origin", str(self.remote_path), cwd=clean_clone)
        clean_store = MetadataStore(clean_clone, "acme/widgets")
        self.assertEqual(first_commit, clean_store.fetch_tip())
        self.assertEqual(
            "task-record",
            clean_store.read_object(
                first_commit,
                "task-record",
                first_id,
                task="217",
                subject=git("rev-parse", "HEAD", cwd=clean_clone).decode("ascii").strip(),
            )["kind"],
        )

        second_id, second_data = self._object("task-record", "217", {"title": "updated"})
        second_commit = self.store.publish([second_data], {"217": (first_id, second_id)})
        self.assertNotEqual(first_commit, second_commit)
        self.assertEqual(second_commit, self._remote_tip())
        with self.assertRaises(MetadataConflictError):
            self.store.publish([first_data], {"217": (None, first_id)})
        self.assertEqual(second_commit, self._remote_tip())
        self.assertEqual(before, self._product_state())
        self.assertEqual(
            f"refs/agentcore/metadata {second_commit}\n".encode("ascii"),
            git(
                "--git-dir", str(self.remote_path), "for-each-ref",
                "--format=%(refname) %(objectname)",
            ),
        )

    def test_named_remote_fetch_ignores_hostile_configured_refmap(self) -> None:
        _object_id, data = self._object("evidence", "217", {"fetch-isolation": True})
        metadata_tip = self.store.publish([data])
        product_oid = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        existing_ref = "refs/heads/metadata-fetch-collision"
        absent_ref = "refs/heads/metadata-fetch-created"
        git("update-ref", existing_ref, product_oid, cwd=self.product)
        for destination in (existing_ref, absent_ref):
            git(
                "config", "--add", "remote.origin.fetch",
                f"+{metadata_ref_module.METADATA_REF}:{destination}",
                cwd=self.product,
            )
        before_product = self._product_state()
        before_config = git("config", "--local", "--list", "--null", cwd=self.product)
        self.assertEqual(b"", git("for-each-ref", absent_ref, cwd=self.product))
        reader = MetadataStore(self.product, "acme/widgets", "origin")

        self.assertEqual(metadata_tip, reader.fetch_tip())
        self.assertEqual(metadata_tip, reader._read_fetch_head())
        self.assertEqual(
            product_oid.encode("ascii") + b"\n",
            git("show-ref", "--verify", "--hash", existing_ref, cwd=self.product),
        )
        self.assertEqual(b"", git("for-each-ref", absent_ref, cwd=self.product))
        self.assertEqual(before_product, self._product_state())
        self.assertEqual(
            before_config, git("config", "--local", "--list", "--null", cwd=self.product)
        )

    def test_ambient_overrides_and_transport_command_config_are_disabled(self) -> None:
        marker = self.temp_root / "custom-receivepack-ran"
        custom_receivepack = self.temp_root / "custom-receivepack"
        custom_receivepack.write_text(
            f"#!/bin/sh\ntouch '{marker}'\nexec git-receive-pack \"$@\"\n",
            encoding="utf-8",
        )
        custom_receivepack.chmod(0o755)
        git("config", "remote.origin.receivepack", str(custom_receivepack), cwd=self.product)
        _object_id, data = self._object("evidence", "91", {"safe": True})
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.remote_path),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(self.temp_root),
            },
        ):
            with self.assertRaisesRegex(MetadataRefError, "transport integration risk"):
                self.store.publish([data])
        self.assertFalse(marker.exists())

    def test_repository_url_rewrite_is_rejected_as_transport_risk(self) -> None:
        git("config", "url.fake-transport.insteadOf", "origin", cwd=self.product)
        with self.assertRaisesRegex(MetadataRefError, "integration risk"):
            MetadataStore(self.product, "acme/widgets")

    def test_local_protocol_override_is_rejected_and_fsmonitor_never_executes(self) -> None:
        marker = self.temp_root / "fsmonitor-ran"
        fsmonitor = self.temp_root / "fsmonitor"
        fsmonitor.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        fsmonitor.chmod(0o755)
        git("config", "core.fsmonitor", str(fsmonitor), cwd=self.product)
        _object_id, data = self._object("contract", "91", {"safe": True})
        self.store.publish([data])
        self.assertFalse(marker.exists())

        git("config", "protocol.ext.allow", "always", cwd=self.product)
        with self.assertRaisesRegex(MetadataRefError, "transport integration risk"):
            self.store.fetch_tip()
        with self.assertRaisesRegex(MetadataRefError, "transport integration risk"):
            MetadataStore(self.product, "acme/widgets")

    def test_empty_publish_requires_a_valid_existing_remote_tip(self) -> None:
        with self.assertRaises(MetadataRefError):
            self.store.publish([])
        _object_id, data = self._object("evidence", "1", {"ok": True})
        tip = self.store.publish([data])
        self.assertEqual(tip, self.store.publish([]))

    def test_writer_without_trusted_cas_capability_refuses_mutation(self) -> None:
        before_product = self._product_state()
        before_refs = git(
            "--git-dir",
            str(self.remote_path),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        )
        _object_id, data = self._object("evidence", "217", {"no-capability": True})
        unguarded = MetadataStore(self.product, "acme/widgets")

        with self.assertRaisesRegex(MetadataRefError, "trusted direct-ref CAS capability"):
            unguarded.publish([data])

        self.assertEqual(
            before_refs,
            git(
                "--git-dir",
                str(self.remote_path),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            ),
        )
        self.assertEqual(before_product, self._product_state())

    def test_candidate_parent_is_checked_before_trusted_capability(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def capability(candidate: str, expected_old: str | None) -> bool:
            calls.append((candidate, expected_old))
            return True

        writer = MetadataStore(
            self.product,
            "acme/widgets",
            direct_ref_cas=capability,
        )
        product_commit = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()

        with self.assertRaisesRegex(MetadataRefError, "not a direct child"):
            writer._push_commit(product_commit, "0" * len(product_commit))

        self.assertEqual([], calls)
        self.assertIsNone(self._remote_tip())

    def test_expected_old_oid_mismatch_cannot_change_metadata_ref(self) -> None:
        _object_id, initial_data = self._object("evidence", "1", {"base": True})
        base = self.store.publish([initial_data])
        candidate = self._create_remote_child(
            base, additions=(("candidate.txt", b"candidate", "100644"),)
        )
        winner = self._create_remote_child(
            base, additions=(("winner.txt", b"winner", "100644"),)
        )
        self._set_remote_tip(winner, base)

        capability = self.store._direct_ref_cas
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertFalse(capability(candidate, base))
        self.assertEqual(winner, self._remote_tip())
        self.assertEqual(
            f"{metadata_ref_module.METADATA_REF} {winner}\n".encode("ascii"),
            git(
                "--git-dir",
                str(self.remote_path),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            ),
        )

    def test_all_four_kinds_are_retrievable_from_exact_commit_on_clean_clone(self) -> None:
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        before = self._product_state()
        contract_id, contract = self._object("contract", "217", {"requirements": "fixed"})
        record_id, record = self._object("task-record", "217", {"contract_id": contract_id})
        evidence_id, evidence = self._object("evidence", "217", {"subject": subject})
        snapshot_id, snapshot = self._object(
            "task-view-snapshot", "217",
            {"contract_id": contract_id, "record_id": record_id, "evidence_ids": [evidence_id]},
        )
        commit = self.store.publish(
            [contract, record, evidence, snapshot], {"217": (None, record_id)}
        )
        required = tuple(
            (kind, object_id, "217", subject)
            for kind, object_id in (
                ("task-record", record_id), ("contract", contract_id),
                ("evidence", evidence_id), ("task-view-snapshot", snapshot_id),
            )
        )
        self.assertEqual(commit, self.store.confirm(commit, required_objects=required))
        self.assertEqual(before, self._product_state())

        clone = self.temp_root / "fresh-reader"
        git("clone", "--no-checkout", str(self.product), str(clone))
        git("remote", "set-url", "origin", str(self.remote_path), cwd=clone)
        reader = MetadataStore(clone, "acme/widgets")
        self.assertEqual(commit, reader.fetch_tip())
        self.assertEqual(commit, reader.confirm(commit, required_objects=required))
        for kind, object_id, task, exact_subject in required:
            with self.subTest(kind=kind):
                self.assertEqual(
                    kind,
                    reader.read_object(
                        commit, kind, object_id, task=task, subject=exact_subject
                    )["kind"],
                )
        self.assertEqual(
            [evidence_id],
            reader.read_object(
                commit, "task-view-snapshot", snapshot_id, task="217", subject=subject
            )["payload"]["evidence_ids"],
        )
        with self.assertRaises(MetadataRefError):
            reader.confirm(commit, required_objects=(("evidence", evidence_id, "217", "b" * 40),))

    def test_read_object_by_id_resolves_objects_across_tasks_and_subjects(self) -> None:
        first_id, first_data = codec.encode_object(
            "evidence", "acme/widgets", "101", "a" * 40,
            {"supersedes": "prior-evidence"},
        )
        second_id, second_data = codec.encode_object(
            "contract", "acme/widgets", "202", "b" * 40,
            {"supersedes": "prior-contract"},
        )
        commit = self.store.publish([first_data, second_data])

        for kind, object_id, task, subject in (
            ("evidence", first_id, "101", "a" * 40),
            ("contract", second_id, "202", "b" * 40),
        ):
            with self.subTest(kind=kind):
                decoded = self.store.read_object_by_id(commit, kind, object_id)
                self.assertEqual(task, decoded["task"])
                self.assertEqual(subject, decoded["subject"])

    def test_read_object_by_id_rejects_unknown_wrong_kind_and_invalid_identity(self) -> None:
        object_id, data = self._object("evidence", "101", {"id": True})
        commit = self.store.publish([data])

        for kind, requested_id in (
            ("evidence", "0" * 64),
            ("contract", object_id),
            ("evidence", "not-a-metadata-id"),
            ("unknown", object_id),
        ):
            with self.subTest(kind=kind, object_id=requested_id):
                with self.assertRaises(MetadataRefError):
                    self.store.read_object_by_id(commit, kind, requested_id)

    def test_read_object_by_id_rejects_unreachable_and_malformed_commits(self) -> None:
        object_id, data = self._object("evidence", "101", {"reachable": True})
        tip = self.store.publish([data])
        unreachable = self._create_remote_child(tip)

        with self.assertRaises(MetadataConflictError):
            self.store.read_object_by_id(unreachable, "evidence", object_id)

        malformed = git(
            "hash-object", "--literally", "-t", "commit", "-w", "--stdin",
            cwd=self.product, input_data=b"not a valid commit object\n",
        ).decode("ascii").strip()
        with self.assertRaisesRegex(MetadataRefError, "malformed commit"):
            self.store.read_object_by_id(malformed, "evidence", object_id)

    def test_read_object_by_id_rejects_corruption_and_another_repository(self) -> None:
        object_id, data = self._object("evidence", "101", {"safe": True})
        tip = self.store.publish([data])
        corrupt = self._create_remote_child(
            tip,
            additions=((codec.object_path("evidence", object_id), b"tampered bytes", "100644"),),
        )
        self._set_remote_tip(corrupt, tip)
        with self.assertRaises(MetadataRefError):
            self.store.read_object_by_id(corrupt, "evidence", object_id)

        self._set_remote_tip(tip, corrupt)
        other_repository = MetadataStore(self.product, "acme/other")
        with self.assertRaises(MetadataRefError):
            other_repository.read_object_by_id(tip, "evidence", object_id)

    def test_read_object_remains_bound_to_required_task_and_subject(self) -> None:
        object_id, data = codec.encode_object(
            "evidence", "acme/widgets", "101", "a" * 40, {"bound": True}
        )
        commit = self.store.publish([data])
        self.assertEqual(
            {"bound": True},
            self.store.read_object(
                commit, "evidence", object_id, task="101", subject="a" * 40
            )["payload"],
        )

        with self.assertRaises(MetadataRefError):
            self.store.read_object(
                commit, "evidence", object_id, task="202", subject="a" * 40
            )
        with self.assertRaises(MetadataRefError):
            self.store.read_object(
                commit, "evidence", object_id, task="101", subject="b" * 40
            )
        with self.assertRaises(TypeError):
            self.store.read_object(commit, "evidence", object_id)  # type: ignore[call-arg]

    def test_competing_disjoint_task_updates_reconstruct_on_winning_tip(self) -> None:
        competing_store = self._writer()
        first_id, first_data = self._object("task-record", "101", {"writer": "first"})
        second_id, second_data = self._object("task-record", "202", {"writer": "second"})
        original_push = self.store._push_commit
        raced = False

        def advance_then_push(candidate: str, observed: str | None) -> bool:
            nonlocal raced
            if not raced:
                raced = True
                competing_store.publish([second_data], {"202": (None, second_id)})
            return original_push(candidate, observed)

        self.store._push_commit = advance_then_push  # type: ignore[method-assign]
        first_commit = self.store.publish([first_data], {"101": (None, first_id)})
        self.assertTrue(raced)
        self.assertEqual(first_commit, self._remote_tip())
        self.assertEqual(
            {"writer": "first"},
            self.store.read_object(
                first_commit,
                "task-record",
                first_id,
                task="101",
                subject=git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
            )["payload"],
        )
        self.assertEqual(
            {"writer": "second"},
            self.store.read_object(
                first_commit,
                "task-record",
                second_id,
                task="202",
                subject=git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
            )["payload"],
        )

    def test_competing_same_task_pointer_fails_closed(self) -> None:
        old_id, old_data = self._object("task-record", "217", {"version": 1})
        self.store.publish([old_data], {"217": (None, old_id)})
        competing_store = self._writer()
        proposed_id, proposed_data = self._object("task-record", "217", {"version": 2})
        winner_id, winner_data = self._object("task-record", "217", {"version": "other writer"})
        original_push = self.store._push_commit
        raced = False

        def advance_then_push(candidate: str, observed: str | None) -> bool:
            nonlocal raced
            if not raced:
                raced = True
                competing_store.publish([winner_data], {"217": (old_id, winner_id)})
            return original_push(candidate, observed)

        self.store._push_commit = advance_then_push  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError) as raised:
            self.store.publish([proposed_data], {"217": (old_id, proposed_id)})
        self.assertIn(old_id, str(raised.exception))
        self.assertIn(winner_id, str(raised.exception))
        self.assertIn(proposed_id, str(raised.exception))
        self.assertEqual(self._remote_tip(), self.store.fetch_tip())
        winner_tip = self._remote_tip()
        assert winner_tip is not None
        self.assertEqual(winner_id, self.store._read_tree_snapshot(winner_tip).pointers["217"])

    def test_identical_competing_task_record_is_not_mistaken_for_own_candidate(self) -> None:
        old_id, old_data = self._object("task-record", "217", {"revision": 1})
        self.store.publish([old_data], {"217": (None, old_id)})
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        new_id, new_data = self._object("task-record", "217", {"revision": 2})
        competitor = self._writer()
        original_push = self.store._push_commit
        intents: list[str] = []
        winner: list[str] = []

        def compete(candidate: str, observed: str | None) -> bool:
            if not winner:
                winner.append(competitor.publish([new_data], {"217": (old_id, new_id)}))
            return original_push(candidate, observed)

        self.store._push_commit = compete  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError):
            self.store.publish(
                [new_data], {"217": (old_id, new_id)}, on_candidate=intents.append
            )
        self.assertEqual(1, len(intents))
        self.assertNotEqual(winner[0], intents[0])
        self.assertEqual(winner[0], self._remote_tip())
        with self.assertRaises(MetadataConflictError):
            self.store.confirm(
                intents[0], required_objects=(("task-record", new_id, "217", subject),)
            )

    def test_immutable_object_path_corruption_is_rejected(self) -> None:
        object_id, data = self._object("evidence", "1", {"safe": True})
        tip = self.store.publish([data])
        path = codec.object_path("evidence", object_id)
        corrupt = self._create_remote_child(tip, additions=((path, b"tampered bytes", "100644"),))
        self._set_remote_tip(corrupt, tip)
        with self.assertRaises(MetadataRefError):
            self.store.fetch_tip()

    def test_already_fetched_tree_entry_and_blob_limits_fail_closed(self) -> None:
        _first_id, first = self._object("evidence", "1", {"value": "first"})
        _second_id, second = self._object("evidence", "2", {"value": "second"})
        tip = self.store.publish([first, second])
        with mock.patch.object(metadata_ref_module, "MAX_TREE_ENTRIES", 3):
            with self.assertRaisesRegex(MetadataRefError, "entry limit"):
                self.store.fetch_tip()
        with mock.patch.object(metadata_ref_module, "MAX_TREE_TOTAL_BYTES", len(first) + len(second) - 1):
            with self.assertRaisesRegex(MetadataRefError, "total byte limit"):
                self.store.fetch_tip()
        with mock.patch.object(metadata_ref_module, "MAX_OBJECT_BYTES", len(first) - 1):
            with self.assertRaisesRegex(MetadataRefError, "per-file byte limit"):
                self.store.fetch_tip()
        self.assertEqual(tip, self.store.fetch_tip())

    def test_executable_symlink_and_gitlink_modes_fail_before_metadata_read(self) -> None:
        object_id, data = self._object("evidence", "1", {"safe": True})
        tip = self.store.publish([data])
        path = codec.object_path("evidence", object_id)
        for mode in ("100755", "120000", "160000"):
            with self.subTest(mode=mode):
                bad = self._create_remote_child(tip, additions=((path, data, mode),))
                self._set_remote_tip(bad, tip)
                with self.assertRaisesRegex(MetadataRefError, "non-regular"):
                    self.store.fetch_tip()
                self._set_remote_tip(tip, bad)

    def test_malformed_or_wrong_kind_record_pointer_is_rejected(self) -> None:
        record_id, record = self._object("task-record", "217", {"safe": True})
        tip = self.store.publish([record], {"217": (None, record_id)})
        evidence_id, evidence = self._object("evidence", "217", {"safe": True})
        for pointer in (b"not-an-object-id\n", f"{evidence_id}\n".encode("ascii")):
            with self.subTest(pointer=pointer):
                bad = self._create_remote_child(
                    tip,
                    additions=(
                        (codec.object_path("evidence", evidence_id), evidence, "100644"),
                        ("tasks/217/record", pointer, "100644"),
                    ),
                )
                self._set_remote_tip(bad, tip)
                with self.assertRaisesRegex(MetadataRefError, "pointer"):
                    self.store.fetch_tip()
                self._set_remote_tip(tip, bad)

    def test_record_pointer_cannot_rebind_another_task_identity(self) -> None:
        own_id, own_data = self._object("task-record", "217", {"task": "own"})
        other_id, other_data = self._object("task-record", "218", {"task": "other"})
        tip = self.store.publish([own_data, other_data], {"217": (None, own_id)})
        bad = self._create_remote_child(
            tip, additions=(("tasks/217/record", f"{other_id}\n".encode(), "100644"),)
        )
        self._set_remote_tip(bad, tip)
        with self.assertRaisesRegex(MetadataRefError, "wrong repository or Task"):
            self.store.fetch_tip()

    def test_historical_object_deletion_then_restoration_is_rejected(self) -> None:
        object_id, data = self._object("evidence", "1", {"immutable": True})
        first = self.store.publish([data])
        object_path = codec.object_path("evidence", object_id)
        deleted = self._create_remote_child(first, deletions=(object_path,))
        restored = self._create_remote_child(
            deleted,
            additions=((object_path, data, "100644"),),
        )
        self._set_remote_tip(restored, first)

        # The current commit is well-formed and contains the expected object;
        # only traversing every historical edge exposes the illegal deletion.
        self.assertEqual(data, self.store._read_tree_snapshot(restored).objects[object_path].data)
        with self.assertRaises(MetadataRefError):
            self.store.fetch_tip()
        with self.assertRaises(MetadataRefError):
            self.store.publish([data])
        with self.assertRaises(MetadataRefError):
            self.store.read_object(
                restored,
                "evidence",
                object_id,
                task="1",
                subject=git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip(),
            )

    def test_historical_record_pointer_deletion_is_rejected(self) -> None:
        record_id, data = self._object("task-record", "217", {"revision": 1})
        first = self.store.publish([data], {"217": (None, record_id)})
        deleted = self._create_remote_child(first, deletions=("tasks/217/record",))
        self._set_remote_tip(deleted, first)
        with self.assertRaisesRegex(MetadataRefError, "deleted a Task Record pointer"):
            self.store.fetch_tip()

    def test_unauthorized_malformed_tree_path_is_rejected(self) -> None:
        _object_id, data = self._object("contract", "1", {"safe": True})
        tip = self.store.publish([data])
        malformed = self._create_remote_child(
            tip,
            additions=(("untrusted/metadata.txt", b"not part of the protocol", "100644"),),
        )
        self._set_remote_tip(malformed, tip)
        with self.assertRaises(MetadataRefError):
            self.store.fetch_tip()

    def test_remote_symbolic_metadata_ref_cannot_advance_product_branch(self) -> None:
        _first_id, first_data = self._object("evidence", "1", {"version": 1})
        first = self.store.publish([first_data])
        git("--git-dir", str(self.remote_path), "update-ref", "refs/heads/main", first)
        git("--git-dir", str(self.remote_path), "update-ref", "-d", "refs/agentcore/metadata", first)
        git(
            "--git-dir", str(self.remote_path), "symbolic-ref",
            "refs/agentcore/metadata", "refs/heads/main",
        )
        advertisement = git(
            "ls-remote", "--symref", "--refs", str(self.remote_path), "refs/agentcore/metadata",
            cwd=self.product,
        )
        self.assertIn(b"ref: refs/heads/main\trefs/agentcore/metadata", advertisement)
        _next_id, next_data = self._object("evidence", "2", {"version": 2})
        with self.assertRaises(MetadataRefError):
            self.store.publish([next_data])
        self.assertEqual(
            first.encode("ascii") + b"\n",
            git("--git-dir", str(self.remote_path), "rev-parse", "refs/heads/main"),
        )

    def test_remote_ref_retargeted_after_observation_cannot_advance_product_branch(self) -> None:
        _first_id, first_data = self._object("evidence", "1", {"version": 1})
        first = self.store.publish([first_data])
        git("--git-dir", str(self.remote_path), "update-ref", "refs/heads/main", first)
        _next_id, next_data = self._object("evidence", "2", {"version": 2})
        original_push = self.store._push_commit

        def retarget_then_push(candidate: str, observed: str | None) -> bool:
            self.assertEqual(first, observed)
            git(
                "--git-dir", str(self.remote_path), "update-ref",
                "-d", "refs/agentcore/metadata", first,
            )
            git(
                "--git-dir", str(self.remote_path), "symbolic-ref",
                "refs/agentcore/metadata", "refs/heads/main",
            )
            return original_push(candidate, observed)

        self.store._push_commit = retarget_then_push  # type: ignore[method-assign]
        with self.assertRaises(MetadataRefError):
            self.store.publish([next_data])
        self.assertEqual(
            first.encode("ascii") + b"\n",
            git("--git-dir", str(self.remote_path), "rev-parse", "refs/heads/main"),
        )

    def test_ref_type_change_at_server_cas_cannot_update_product_ref(self) -> None:
        _first_id, first_data = self._object("evidence", "1", {"version": 1})
        first = self.store.publish([first_data])
        git("--git-dir", str(self.remote_path), "update-ref", "refs/heads/main", first)
        _next_id, next_data = self._object("evidence", "2", {"version": 2})
        original_git_dir = self._git_dir
        switched = False

        def switch_just_before_update(remote: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
            nonlocal switched
            if (
                not switched
                and args[:3] == ("update-ref", "--no-deref", metadata_ref_module.METADATA_REF)
            ):
                switched = True
                git("--git-dir", str(remote), "update-ref", "-d", metadata_ref_module.METADATA_REF, first)
                git(
                    "--git-dir", str(remote), "symbolic-ref",
                    metadata_ref_module.METADATA_REF, "refs/heads/main",
                )
            return original_git_dir(remote, *args)

        with mock.patch.object(self, "_git_dir", side_effect=switch_just_before_update):
            with self.assertRaises(MetadataRefError):
                self.store.publish([next_data])
        self.assertTrue(switched)
        self.assertEqual(
            first.encode("ascii") + b"\n",
            git("--git-dir", str(self.remote_path), "rev-parse", "refs/heads/main"),
        )

    def test_fetch_head_symlink_is_rejected_without_reading_target(self) -> None:
        _object_id, data = self._object("contract", "1", {"safe": True})
        self.store.publish([data])
        path_text = git("rev-parse", "--git-path", "FETCH_HEAD", cwd=self.product).decode().strip()
        fetch_head = Path(path_text)
        if not fetch_head.is_absolute():
            fetch_head = self.product / fetch_head
        saved = fetch_head.with_name("FETCH_HEAD.fixture-backup")
        fetch_head.rename(saved)
        target = self.temp_root / "oversized-fetch-head"
        target.write_bytes(b"x" * (70 * 1024))
        fetch_head.symlink_to(target)
        with self.assertRaisesRegex(MetadataRefError, "FETCH_HEAD"):
            self.store._read_fetch_head()
        self.assertEqual(70 * 1024, target.stat().st_size)

    def test_fetch_head_fifo_is_rejected_without_waiting_for_a_writer(self) -> None:
        _object_id, data = self._object("contract", "1", {"safe": True})
        self.store.publish([data])
        path_text = git("rev-parse", "--git-path", "FETCH_HEAD", cwd=self.product).decode().strip()
        fetch_head = Path(path_text)
        if not fetch_head.is_absolute():
            fetch_head = self.product / fetch_head
        fetch_head.rename(fetch_head.with_name("FETCH_HEAD.fifo-backup"))
        os.mkfifo(fetch_head)
        with self.assertRaisesRegex(MetadataRefError, "bounded regular file"):
            self.store._read_fetch_head()

    def test_non_fast_forward_force_rewrite_is_not_retried_or_overwritten(self) -> None:
        _base_id, base_data = self._object("evidence", "1", {"base": True})
        base = self.store.publish([base_data])
        _new_id, new_data = self._object("evidence", "2", {"new": True})
        unrelated = self._create_unrelated_genesis()
        original_push = self.store._push_commit
        raced = False

        def force_rewrite_then_push(candidate: str, observed: str | None) -> bool:
            nonlocal raced
            if not raced:
                raced = True
                self._set_remote_tip(unrelated, base)
            return original_push(candidate, observed)

        self.store._push_commit = force_rewrite_then_push  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError):
            self.store.publish([new_data])
        self.assertTrue(raced)
        self.assertEqual(unrelated, self._remote_tip())

    def test_remote_ref_deletion_after_observation_is_not_resurrected(self) -> None:
        _base_id, base_data = self._object("evidence", "1", {"base": True})
        observed = self.store.publish([base_data])
        _next_id, next_data = self._object("evidence", "2", {"next": True})
        original_push = self.store._push_commit
        raced = False

        def delete_then_push(candidate: str, old_oid: str | None) -> bool:
            nonlocal raced
            if not raced:
                raced = True
                self.assertEqual(observed, old_oid)
                git(
                    "--git-dir",
                    str(self.remote_path),
                    "update-ref",
                    "-d",
                    "refs/agentcore/metadata",
                    observed,
                )
            return original_push(candidate, old_oid)

        self.store._push_commit = delete_then_push  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError):
            self.store.publish([next_data])
        self.assertTrue(raced)
        self.assertIsNone(self._remote_tip())

    def test_remote_rewind_after_observation_is_not_replaced(self) -> None:
        _genesis_id, genesis_data = self._object("evidence", "1", {"version": 1})
        genesis = self.store.publish([genesis_data])
        _child_id, child_data = self._object("evidence", "2", {"version": 2})
        observed = self.store.publish([child_data])
        _next_id, next_data = self._object("evidence", "3", {"version": 3})
        original_push = self.store._push_commit
        raced = False

        def rewind_then_push(candidate: str, old_oid: str | None) -> bool:
            nonlocal raced
            if not raced:
                raced = True
                self.assertEqual(observed, old_oid)
                self._set_remote_tip(genesis, observed)
            return original_push(candidate, old_oid)

        self.store._push_commit = rewind_then_push  # type: ignore[method-assign]
        with self.assertRaises(MetadataConflictError):
            self.store.publish([next_data])
        self.assertTrue(raced)
        self.assertEqual(genesis, self._remote_tip())

    def test_candidate_intent_precedes_push_and_confirm_returns_exact_candidate(self) -> None:
        object_id, data = self._object("evidence", "71", {"durable": True})
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        intent: list[str] = []
        events: list[str] = []
        original_push = self.store._push_commit
        later_store = self._writer()
        _later_id, later_data = self._object("evidence", "72", {"later": True})

        class InterruptedAfterRemoteSuccess(RuntimeError):
            pass

        def capture_intent(candidate: str) -> None:
            events.append("intent")
            intent.append(candidate)

        def push_then_interrupt(candidate: str, observed: str | None) -> bool:
            events.append("push")
            result = original_push(candidate, observed)
            later_store.publish([later_data])
            raise InterruptedAfterRemoteSuccess("simulated interruption after remote publication")

        self.store._push_commit = push_then_interrupt  # type: ignore[method-assign]
        with self.assertRaises(InterruptedAfterRemoteSuccess):
            self.store.publish([data], on_candidate=capture_intent)

        self.assertEqual(["intent", "push"], events)
        self.assertEqual(1, len(intent))
        candidate = intent[0]
        self.assertNotEqual(candidate, self._remote_tip())
        self.assertEqual(
            candidate,
            self.store.confirm(
                candidate,
                required_objects=(("evidence", object_id, "71", subject),),
            ),
        )

    def test_lost_push_ack_returns_only_the_exact_reachable_candidate(self) -> None:
        _object_id, data = self._object("evidence", "73", {"ack": "lost"})
        candidate_intent: list[str] = []
        original_push = self.store._push_commit

        def lose_ack(candidate: str, observed: str | None) -> bool:
            self.assertTrue(original_push(candidate, observed))
            return False

        self.store._push_commit = lose_ack  # type: ignore[method-assign]
        returned = self.store.publish([data], on_candidate=candidate_intent.append)
        self.assertEqual(1, len(candidate_intent))
        self.assertEqual(candidate_intent[0], returned)
        self.assertEqual(returned, self._remote_tip())

    def test_confirm_rejects_unreachable_or_identity_mismatched_candidates(self) -> None:
        object_id, data = self._object("contract", "81", {"checkpoint": True})
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        intent: list[str] = []

        def stop_before_push(candidate: str) -> None:
            intent.append(candidate)
            raise RuntimeError("stop before external effect")

        with self.assertRaisesRegex(RuntimeError, "stop before external effect"):
            self.store.publish([data], on_candidate=stop_before_push)
        self.assertIsNone(self._remote_tip())
        with self.assertRaises(MetadataConflictError):
            self.store.confirm(intent[0])

        commit = self.store.publish([data])
        with self.assertRaises(MetadataRefError):
            self.store.confirm(
                commit,
                required_objects=(("contract", object_id, "82", subject),),
            )

    def test_git_command_runtime_is_bounded(self) -> None:
        real_popen = subprocess.Popen

        def sleeping_process(_command: list[str], **options: Any) -> subprocess.Popen[bytes]:
            return real_popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                **options,
            )

        with mock.patch.object(metadata_ref_module.subprocess, "Popen", side_effect=sleeping_process):
            with self.assertRaisesRegex(MetadataRefError, "runtime limit"):
                self.store._git(["simulated-sleep"], timeout=0.05)

    def test_exact_read_shares_a_validation_deadline_with_ancestry_traversal(self) -> None:
        object_id, first_data = self._object("evidence", "1", {"first": True})
        first = self.store.publish([first_data])
        _next_id, next_data = self._object("evidence", "2", {"second": True})
        tip = self.store.publish([next_data])
        subject = git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()
        tip_snapshot = self.store._read_tree_snapshot(tip)
        original_read_tree = self.store._read_tree_snapshot

        def slow_ancestry_read(commit: str):
            time.sleep(0.02)
            return original_read_tree(commit)

        with mock.patch.object(metadata_ref_module, "MAX_HISTORY_VALIDATION_SECONDS", 0.01), mock.patch.object(
            self.store, "_observe_remote", return_value=tip
        ), mock.patch.object(
            self.store, "_validate_full_history", return_value=tip_snapshot
        ), mock.patch.object(
            self.store, "_read_tree_snapshot", side_effect=slow_ancestry_read
        ):
            with self.assertRaisesRegex(MetadataRefError, "validation time limit"):
                self.store.read_object(first, "evidence", object_id, task="1", subject=subject)

    def test_read_by_id_uses_bounded_ancestry_validation(self) -> None:
        object_id, first_data = self._object("evidence", "1", {"first": True})
        first = self.store.publish([first_data])
        _next_id, next_data = self._object("evidence", "2", {"second": True})
        tip = self.store.publish([next_data])
        tip_snapshot = self.store._read_tree_snapshot(tip)
        original_read_tree = self.store._read_tree_snapshot

        def slow_ancestry_read(commit: str):
            time.sleep(0.02)
            return original_read_tree(commit)

        with mock.patch.object(metadata_ref_module, "MAX_HISTORY_VALIDATION_SECONDS", 0.01), mock.patch.object(
            self.store, "_observe_remote", return_value=tip
        ), mock.patch.object(
            self.store, "_validate_full_history", return_value=tip_snapshot
        ), mock.patch.object(
            self.store, "_read_tree_snapshot", side_effect=slow_ancestry_read
        ):
            with self.assertRaisesRegex(MetadataRefError, "validation time limit"):
                self.store.read_object_by_id(first, "evidence", object_id)

    def test_validation_scope_preserves_nested_deadline_and_restores_context(self) -> None:
        self.assertIsNone(self.store._validation_deadline.get())
        with self.store.validation_scope():
            deadline = self.store._validation_deadline.get()
            self.assertIsNotNone(deadline)
            with self.store.validation_scope():
                self.assertEqual(deadline, self.store._validation_deadline.get())
            self.assertEqual(deadline, self.store._validation_deadline.get())
        self.assertIsNone(self.store._validation_deadline.get())

        with mock.patch.object(metadata_ref_module, "MAX_HISTORY_VALIDATION_SECONDS", 0.01):
            with self.assertRaisesRegex(MetadataRefError, "validation time limit"):
                with self.store.validation_scope():
                    time.sleep(0.02)
        self.assertIsNone(self.store._validation_deadline.get())

    def test_remote_failure_and_unreachable_push_never_return_a_checkpoint_oid(self) -> None:
        missing_remote = self.temp_root / "missing.git"
        missing_store = self._writer(
            remote=str(missing_remote), bare_remote=missing_remote
        )
        with self.assertRaises(MetadataRefError):
            missing_store.publish([])

        _initial_id, initial_data = self._object("evidence", "1", {"initial": True})
        initial_tip = self.store.publish([initial_data])
        hook = self.remote_path / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        _next_id, next_data = self._object("evidence", "2", {"next": True})
        with self.assertRaises(MetadataRefError):
            self.store.publish([next_data])
        self.assertEqual(initial_tip, self._remote_tip())

    def test_sha256_repository_uses_full_sha256_git_oids(self) -> None:
        product = self.temp_root / "sha256-product"
        product.mkdir()
        remote = self.temp_root / "sha256-remote.git"
        initialization = subprocess.run(
            ["git", "init", "--object-format=sha256"],
            cwd=product,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            check=False,
        )
        if initialization.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        git("init", "--bare", "--object-format=sha256", str(remote))
        git("config", "user.name", "Metadata Ref SHA-256 Test", cwd=product)
        git("config", "user.email", "metadata-ref-sha256@example.invalid", cwd=product)
        (product / "tracked.txt").write_text("sha256 product\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=product)
        git("commit", "-m", "sha256 product fixture", cwd=product)
        git("remote", "add", "origin", str(remote), cwd=product)

        subject = git("rev-parse", "HEAD", cwd=product).decode("ascii").strip()
        self.assertEqual(64, len(subject))
        object_id, data = codec.encode_object("evidence", "acme/widgets", "1", subject, {"sha256": True})
        store = self._writer(product, bare_remote=remote)
        commit = store.publish([data])
        self.assertEqual(64, len(commit))
        self.assertEqual(commit, store.fetch_tip())
        decoded = store.read_object(commit, "evidence", object_id, task="1", subject=subject)
        self.assertEqual("evidence", decoded["kind"])
        self.assertEqual({"sha256": True}, decoded["payload"])


if __name__ == "__main__":
    unittest.main()
