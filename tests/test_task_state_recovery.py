from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"
sys.path.insert(0, str(BIN))
import task_state_recovery as recovery


BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "task_state_recovery_bridge_test", ROOT / "tools" / "automation_recovery_bridge.py"
)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
bridge = importlib.util.module_from_spec(BRIDGE_SPEC)
sys.modules[BRIDGE_SPEC.name] = bridge
BRIDGE_SPEC.loader.exec_module(bridge)


class TaskStateRecoveryTest(unittest.TestCase):
    TASK = "163"
    PR = 176
    BRANCH = "task/163-worktree-dispatch"
    HEAD = "42d0b0216fb2b338d3973484af7198cc77e52abb"
    BASE = "f9a9ba13e2366e21703847f9edf411e1cb2052a2"
    MAIN = "271cfe06d2410f6616e74a465c50a827fe42d319"

    def setUp(self) -> None:
        self.remote_default_revision: str | None = None
        self.remote_default_branch: str | None = None
        self.fetch_ref_override: str | None = None
        self.fetch_head_override: str | None = None
        self._network_fetch = mock.patch.object(
            recovery.lifecycle,
            "network_git",
            side_effect=self.fake_network_fetch,
        )
        self._network_fetch.start()
        self.addCleanup(self._network_fetch.stop)

    def test_recovery_rejects_non_integer_pr_numbers_before_io(self) -> None:
        for requested_pr in (True, 1.0, 1.9, "1", 0, -1):
            with self.subTest(requested_pr=requested_pr):
                with self.assertRaisesRegex(
                    recovery.TaskStateRecoveryError,
                    "pull request number must be a positive integer",
                ):
                    recovery.recover_missing_task_state(
                        ROOT,
                        Path("/path/that/must/not/be-read"),
                        self.TASK,
                        requested_pr,
                        self.MAIN,
                    )

    @staticmethod
    def git(*args: str, cwd: Path) -> str:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def fake_network_fetch(
        self, *args: str, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[str]:
        if args == ("ls-remote", "--symref", "origin", "HEAD"):
            revision = self.remote_default_revision
            if revision is None:
                revision = self.git(
                    "git", "rev-parse", "--verify", "refs/remotes/origin/main", cwd=cwd
                )
            branch = self.remote_default_branch or "main"
            return subprocess.CompletedProcess(
                ["git", *args],
                0,
                f"ref: refs/heads/{branch}\tHEAD\n{revision}\tHEAD\n",
                "",
            )
        self.assertEqual(args[:3], ("fetch", "--no-tags", "origin"))
        source_ref, separator, temporary_ref = args[3].partition(":")
        self.assertEqual(source_ref, "refs/heads/main")
        self.assertEqual(separator, ":")
        self.assertTrue(temporary_ref.startswith("refs/agent-core/recovery-default/"))
        fetched_ref = self.fetch_ref_override or self.remote_default_revision
        if fetched_ref is None:
            fetched_ref = self.git(
                "git", "rev-parse", "--verify", "refs/remotes/origin/main", cwd=cwd
            )
        self.git("git", "update-ref", temporary_ref, fetched_ref, cwd=cwd)
        fetch_head = Path(
            self.git("git", "rev-parse", "--git-path", "FETCH_HEAD", cwd=cwd)
        )
        if not fetch_head.is_absolute():
            fetch_head = cwd / fetch_head
        fetch_head.write_text(
            f"{self.fetch_head_override or fetched_ref}\t\tbranch 'main' of origin\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    def exact_fixture(self, root: Path) -> tuple[Path, Path, Path, str, str, str]:
        repository = root / "repository"
        repository.mkdir()
        self.git("git", "init", "-b", "main", cwd=repository)
        self.git("git", "config", "user.name", "Task State Recovery Test", cwd=repository)
        self.git("git", "config", "user.email", "task-state-recovery@example.invalid", cwd=repository)
        self.git("git", "remote", "add", "origin", "https://github.com/upiscium/Templates.git", cwd=repository)
        (repository / ".gitignore").write_text(".task-state/\n", encoding="utf-8")
        template = repository / "components/agent-core/.automation/templates/task-state.md"
        template.parent.mkdir(parents=True)
        template.write_bytes(
            (ROOT / "components/agent-core/.automation/templates/task-state.md").read_bytes()
        )
        (repository / "fixture.txt").write_text("original base\n", encoding="utf-8")
        self.git("git", "add", ".", cwd=repository)
        self.git("git", "commit", "-m", "original Task base", cwd=repository)
        base = self.git("git", "rev-parse", "HEAD", cwd=repository)
        (repository / "main.txt").write_text("current main\n", encoding="utf-8")
        self.git("git", "add", "main.txt", cwd=repository)
        self.git("git", "commit", "-m", "advance main", cwd=repository)
        main = self.git("git", "rev-parse", "HEAD", cwd=repository)
        self.remote_default_revision = main
        self.git("git", "update-ref", "refs/remotes/origin/main", main, cwd=repository)
        self.git(
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=repository,
        )
        worktrees = root / ".worktrees"
        worktrees.mkdir()
        source = worktrees / "181-recover-missing-task-state"
        target = worktrees / "163-worktree-dispatch"
        self.git(
            "git",
            "worktree",
            "add",
            "-b",
            "fix/181-recover-missing-task-state",
            str(source),
            main,
            cwd=repository,
        )
        (source / "implementation.txt").write_text("trusted implementation\n", encoding="utf-8")
        self.git("git", "add", "implementation.txt", cwd=source)
        self.git("git", "commit", "-m", "trusted implementation", cwd=source)
        implementation = self.git("git", "rev-parse", "HEAD", cwd=source)
        self.git(
            "git",
            "worktree",
            "add",
            "-b",
            self.BRANCH,
            str(target),
            base,
            cwd=repository,
        )
        (target / "task-product.txt").write_text("existing product change\n", encoding="utf-8")
        self.git("git", "add", "task-product.txt", cwd=target)
        self.git("git", "commit", "-m", "existing Task product change", cwd=target)
        self.assertEqual(self.git("git", "rev-parse", "HEAD^", cwd=target), base)
        return repository, source, target, base, main, implementation

    def issue_runner(self) -> subprocess.CompletedProcess[str]:
        payload = {
            "number": 163,
            "html_url": "https://github.com/upiscium/Templates/issues/163",
            "title": "Recover exact Task State fixture",
            "body": "A real Issue body for the recovery fixture.",
            "state": "open",
            "repository": {"full_name": "upiscium/Templates"},
            "repository_url": "https://api.github.com/repos/upiscium/Templates",
            "labels": [],
            "assignees": [],
            "milestone": None,
        }
        return subprocess.CompletedProcess(
            ["gh", "api"], 0, json.dumps(payload), ""
        )

    def normalized_pr(
        self, head: str, base_ref_oid: str | None = None, **changes: object
    ) -> dict:
        value = {
            "number": self.PR,
            "state": "OPEN",
            "merged_at": None,
            "draft": True,
            "headRefName": self.BRANCH,
            "headRefOid": head,
            "headRepository": "upiscium/Templates",
            "baseRefName": "main",
            "baseRefOid": base_ref_oid or self.BASE,
            "baseRepository": "upiscium/Templates",
            "isCrossRepository": False,
            "mergeCommit": {"oid": None},
        }
        value.update(changes)
        return value

    def rest_pr(self, head: str, base_ref_oid: str | None = None) -> dict:
        return {
            "number": self.PR,
            "state": "open",
            "merged_at": None,
            "draft": True,
            "head": {
                "ref": self.BRANCH,
                "sha": head,
                "repo": {"full_name": "upiscium/Templates"},
            },
            "base": {
                "ref": "main",
                "sha": base_ref_oid or self.BASE,
                "repo": {"full_name": "upiscium/Templates"},
            },
            "merge_commit_sha": None,
        }

    def test_generated_recovery_files_match_canonical_source(self) -> None:
        source = (ROOT / "components/agent-core/.automation/bin/task_state_recovery.py").read_bytes()
        generated = sorted(
            ROOT.glob("templates/agent-*/.automation/bin/task_state_recovery.py")
        )
        self.assertEqual(6, len(generated))
        for path in generated:
            with self.subTest(path=path):
                self.assertEqual(source, path.read_bytes())

    def plan(self, target: Path) -> dict:
        state = (
            f"- Task ID: {self.TASK}\n"
            f"- Branch: {self.BRANCH}\n"
            f"- Worktree: {target}\n"
            f"- Base branch: main\n"
            f"- Base revision: {self.BASE}\n"
            "## Current state\n\n- Status: implementing\n"
        ).encode()
        issue = b'{"issue":163}\n'
        contract = b'{"issue":163}\n'
        receipt = {
            "schema_version": 1,
            "kind": "lost-ignored-task-state",
            "repository": "upiscium/Templates",
            "task_id": self.TASK,
            "worktree": str(target),
            "branch": self.BRANCH,
            "head": self.HEAD,
            "tree": "a" * 40,
            "base_branch": "main",
            "base_revision": self.BASE,
            "default_revision": self.MAIN,
            "remote_branch_head": self.HEAD,
            "pr_number": self.PR,
            "pr_state": "OPEN",
            "pr_draft": True,
            "pr_head_ref": self.BRANCH,
            "pr_head_oid": self.HEAD,
            "pr_base_ref": "main",
            "pr_base_oid": self.BASE,
            "issue_sha256": "b" * 64,
            "implementation_source": str(ROOT),
            "implementation_revision": self.MAIN,
            "reconstructed_file_sha256": {
                "task.md": recovery._sha256(state),
                "issue.json": recovery._sha256(issue),
                "contract.json": recovery._sha256(contract),
            },
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        return {
            "task": self.TASK,
            "repository": "upiscium/Templates",
            "branch": self.BRANCH,
            "head": self.HEAD,
            "tree": "a" * 40,
            "base": self.BASE,
            "default_revision": self.MAIN,
            "remote_head": self.HEAD,
            "pr": self.normalized_pr(self.HEAD),
            "issue_digest": "b" * 64,
            "issue_bytes": issue,
            "contract_bytes": contract,
            "state_bytes": state,
            "receipt": receipt,
            "receipt_bytes": receipt_bytes,
            "reconstructed": receipt["reconstructed_file_sha256"],
        }

    def test_production_shape_proves_original_base_when_main_and_pr_base_differ(self) -> None:
        self.assertNotEqual(self.BASE, self.MAIN)
        values = {
            "rev-list": f"{self.HEAD} {self.BASE}\n",
            "merge-base": f"{self.BASE}\n",
        }

        def fake_git(_root: Path, command: str, *args: str, **_: object) -> str:
            if command == "rev-list":
                return values["rev-list"]
            if command == "merge-base":
                return values["merge-base"]
            raise AssertionError((command, args))

        pr = {"baseRefOid": self.BASE, "baseRefName": "main"}
        with mock.patch.object(recovery, "_git", side_effect=fake_git):
            self.assertEqual(
                recovery._prove_base(Path("/tmp/163"), self.HEAD, self.MAIN, pr),
                self.BASE,
            )

    def test_fetched_default_ref_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _repository, source, _target, base, main, _implementation = self.exact_fixture(
                Path(directory)
            )
            self.fetch_head_override = base
            self.assertEqual(self.remote_default_revision, main)
            with self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "fetched default FETCH_HEAD does not match its temporary ref",
            ):
                recovery._fetched_default_revision(source, "main", main)

    def test_remote_default_change_between_advertisement_and_fetch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _repository, source, _target, base, main, implementation = self.exact_fixture(
                Path(directory)
            )
            self.fetch_ref_override = base
            with self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "remote default moved between advertisement and fetch",
            ):
                recovery._require_source_and_default(source, implementation)

    def test_non_fast_forward_remote_default_fails_without_replacing_tracking_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, source, _target, base, main, _implementation = self.exact_fixture(
                Path(directory)
            )
            tree = self.git("git", "rev-parse", f"{base}^{{tree}}", cwd=repository)
            rewound = self.git(
                "git", "commit-tree", tree, "-p", base, "-m", "rewound main", cwd=repository
            )
            self.remote_default_revision = rewound
            with self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "origin default branch moved non-fast-forward",
            ):
                recovery._fetched_default_revision(source, "main", rewound)
            self.assertEqual(
                main,
                self.git(
                    "git", "rev-parse", "refs/remotes/origin/main", cwd=repository
                ),
            )
            self.assertEqual(
                "",
                self.git(
                    "git",
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/agent-core/recovery-default",
                    cwd=repository,
                ),
            )

    def test_non_main_remote_default_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _repository, source, _target, _base, _main, implementation = self.exact_fixture(
                Path(directory)
            )
            self.remote_default_branch = "release"
            with self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "default branch is not main: release",
            ):
                recovery._require_source_and_default(source, implementation)

    def test_wrong_implementation_revision_fails_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _repository, source, _target, _base, _main, implementation = self.exact_fixture(
                Path(directory)
            )
            with (
                mock.patch.object(recovery.lifecycle, "network_git") as fetch,
                self.assertRaisesRegex(
                    recovery.TaskStateRecoveryError,
                    "source HEAD does not match the implementation revision",
                ),
            ):
                recovery._require_source_and_default(source, "a" * 40)
            fetch.assert_not_called()

    def test_dirty_source_fails_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _repository, source, _target, _base, _main, implementation = self.exact_fixture(
                Path(directory)
            )
            (source / "unrelated-dirty-file").write_text("dirty\n", encoding="utf-8")
            with (
                mock.patch.object(recovery.lifecycle, "network_git") as fetch,
                self.assertRaisesRegex(
                    recovery.TaskStateRecoveryError,
                    "source worktree must be clean",
                ),
            ):
                recovery._require_source_and_default(source, implementation)
            fetch.assert_not_called()

    def test_pull_request_base_oid_must_be_a_full_oid(self) -> None:
        values = {
            "rev-list": f"{self.HEAD} {self.BASE}\n",
            "merge-base": f"{self.BASE}\n",
        }

        def fake_git(_root: Path, command: str, *args: str, **_: object) -> str:
            if command == "rev-list":
                return values["rev-list"]
            if command == "merge-base":
                return values["merge-base"]
            raise AssertionError((command, args))

        with (
            mock.patch.object(recovery, "_git", side_effect=fake_git),
            self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "pull request base revision must be a full lowercase immutable revision",
            ),
        ):
            recovery._prove_base(
                Path("/tmp/163"),
                self.HEAD,
                self.MAIN,
                {"baseRefOid": "not-a-full-oid", "baseRefName": "main"},
            )

    def test_task_parent_must_match_unique_current_main_merge_base(self) -> None:
        values = {
            "rev-list": f"{self.HEAD} {self.BASE}\n",
            "merge-base": f"{'c' * 40}\n",
        }

        def fake_git(_root: Path, command: str, *args: str, **_: object) -> str:
            if command == "rev-list":
                return values["rev-list"]
            if command == "merge-base":
                return values["merge-base"]
            raise AssertionError((command, args))

        with (
            mock.patch.object(recovery, "_git", side_effect=fake_git),
            self.assertRaisesRegex(
                recovery.TaskStateRecoveryError,
                "Task original base is ambiguous or does not match its parent",
            ),
        ):
            recovery._prove_base(
                Path("/tmp/163"),
                self.HEAD,
                self.MAIN,
                {"baseRefOid": self.BASE, "baseRefName": "main"},
            )

    def test_ambiguous_original_base_fails_closed(self) -> None:
        with mock.patch.object(
            recovery,
            "_git",
            side_effect=[f"{self.HEAD} {self.BASE}\n", f"{self.BASE}\n{'c' * 40}\n"],
        ):
            with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "ambiguous"):
                recovery._prove_base(
                    Path("/tmp/163"),
                    self.HEAD,
                    self.MAIN,
                    {"baseRefOid": self.BASE, "baseRefName": "main"},
                )

    def test_state_builder_is_conservative_and_schema_bound(self) -> None:
        template = (ROOT / "components/agent-core/.automation/templates/task-state.md").read_bytes()
        state = recovery._build_state(
            template,
            163,
            "d" * 64,
            self.BRANCH,
            Path("/tmp/163-worktree-dispatch"),
            self.BASE,
        )
        text = state.decode()
        self.assertIn("- Status: implementing", text)
        self.assertIn(f"- Base revision: {self.BASE}", text)
        self.assertIn("verification and review evidence require fresh validation", text)
        self.assertNotIn("verification.json", text)
        self.assertNotIn("reviewer", text)
        self.assertNotIn("security-reviewer", text)
        self.assertEqual(text.count("canonical-contract sha256="), 1)

    def test_preexisting_partial_state_is_rejected_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            state = target / ".task-state"
            state.mkdir()
            (state / "issue.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "partial"):
                recovery._state_topology(target, False)

    def test_zero_authority_initial_topologies_recover_identically(self) -> None:
        normalized_states: list[bytes] = []
        for topology in ("absent", "empty", "lock"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as directory:
                (
                    _repository,
                    source,
                    target,
                    base,
                    main,
                    implementation,
                ) = self.exact_fixture(Path(directory))
                state = target / ".task-state"
                if topology != "absent":
                    state.mkdir(mode=0o700)
                    state.chmod(0o700)
                if topology == "lock":
                    lock = state / "work-units.lock"
                    lock.write_bytes(b"")
                    lock.chmod(0o600)
                target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
                pull_request = self.normalized_pr(target_head, base)
                pull_request_before = json.loads(json.dumps(pull_request))
                tracked_before = self.git(
                    "git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target
                )
                with (
                    mock.patch.object(
                        recovery.lifecycle, "pull_requests_for_branch", return_value=[pull_request]
                    ),
                    mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                    mock.patch.object(recovery.lifecycle, "remote_branch_head", return_value=target_head),
                ):
                    result = recovery.recover_missing_task_state(
                        source, target, self.TASK, self.PR, implementation
                    )
                self.assertEqual(result["status"], "TASK_STATE_RECOVERED")
                self.assertEqual(result["taskStatus"], "implementing")
                self.assertEqual(result["resume"]["status"], "READY")
                self.assertEqual(result["resume"]["mode"], "resume")
                self.assertEqual(result["resume"]["taskStatus"], "implementing")
                self.assertEqual(
                    tracked_before,
                    self.git("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target),
                )
                self.assertEqual(target_head, self.git("git", "rev-parse", "HEAD", cwd=target))
                self.assertEqual(pull_request_before, pull_request)
                self.assertIn(
                    "- Status: implementing",
                    (state / "task.md").read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    {"contract.json", "issue.json", "task.md", "work-units.lock"},
                    {path.name for path in state.iterdir()},
                )
                lock_metadata = (state / "work-units.lock").lstat()
                self.assertTrue(stat.S_ISREG(lock_metadata.st_mode))
                self.assertEqual(os.geteuid(), lock_metadata.st_uid)
                self.assertFalse(stat.S_IMODE(lock_metadata.st_mode) & 0o022)
                self.assertFalse((state / "verification.json").exists())
                self.assertFalse((state / "work-units.json").exists())
                normalized_states.append(
                    re.sub(
                        rb"[0-9a-f]{40}",
                        b"<oid>",
                        (state / "task.md").read_bytes().replace(str(target).encode(), b"<target>"),
                    )
                )
        self.assertEqual(1, len({state for state in normalized_states}))

    def test_planning_succeeds_when_registered_main_checkout_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, source, target, base, main, implementation = self.exact_fixture(
                Path(directory)
            )
            self.git("git", "reset", "--hard", base, cwd=repository)
            self.git("git", "update-ref", "refs/remotes/origin/main", base, cwd=repository)
            target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
            pull_request = self.normalized_pr(target_head, base)
            with (
                mock.patch.object(
                    recovery.lifecycle, "pull_requests_for_branch", return_value=[pull_request]
                ),
                mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                mock.patch.object(
                    recovery.lifecycle, "remote_branch_head", return_value=target_head
                ),
                mock.patch.object(
                    recovery.lifecycle,
                    "main_worktree",
                    side_effect=AssertionError("recovery must not depend on a main worktree"),
                ),
            ):
                plan = recovery._plan(source, target, self.TASK, self.PR, implementation)
            self.assertEqual(base, self.git("git", "rev-parse", "HEAD", cwd=repository))
            self.assertEqual(main, self.git("git", "rev-parse", "refs/remotes/origin/main", cwd=repository))
            self.assertEqual(plan["base"], base)
            self.assertEqual(plan["default_revision"], main)
            self.assertEqual(plan["receipt"]["base_revision"], base)
            self.assertEqual(plan["receipt"]["default_revision"], main)
            self.assertEqual(plan["receipt"]["pr_base_oid"], base)

    def test_recovery_retries_after_default_and_pr_base_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, source, target, base, first_main, implementation = self.exact_fixture(
                Path(directory)
            )
            target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
            listing_calls = 0

            def list_prs(_target: Path, _branch: str, _repository: str) -> list[dict]:
                nonlocal listing_calls
                listing_calls += 1
                if listing_calls == 1:
                    (repository / "main-after-first-plan.txt").write_text(
                        "advance default between planning passes\n", encoding="utf-8"
                    )
                    self.git("git", "add", "main-after-first-plan.txt", cwd=repository)
                    self.git(
                        "git", "commit", "-m", "advance main during recovery plan", cwd=repository
                    )
                    self.remote_default_revision = self.git(
                        "git", "rev-parse", "HEAD", cwd=repository
                    )
                    return [self.normalized_pr(target_head, base)]
                return [self.normalized_pr(target_head, self.remote_default_revision)]

            with (
                mock.patch.object(
                    recovery.lifecycle, "pull_requests_for_branch", side_effect=list_prs
                ),
                mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                mock.patch.object(
                    recovery.lifecycle, "remote_branch_head", return_value=target_head
                ),
            ):
                first = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )

                first_recovered_default = self.remote_default_revision
                (repository / "main-after-retry.txt").write_text(
                    "advance default before recovery retry\n", encoding="utf-8"
                )
                self.git("git", "add", "main-after-retry.txt", cwd=repository)
                self.git("git", "commit", "-m", "advance main before recovery retry", cwd=repository)
                self.remote_default_revision = self.git(
                    "git", "rev-parse", "HEAD", cwd=repository
                )
                retry = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )

            receipt_path = recovery.private_state.lost_ignored_task_state_receipt(target)
            stored = recovery._read_receipt(receipt_path)
            assert stored is not None
            final_default = self.remote_default_revision
            self.assertNotEqual(base, first_recovered_default)
            self.assertNotEqual(first_recovered_default, final_default)
            self.assertEqual(first["status"], "TASK_STATE_RECOVERED")
            self.assertEqual(retry["status"], "TASK_STATE_ALREADY_RECOVERED")
            self.assertEqual(stored[1]["base_revision"], base)
            self.assertEqual(stored[1]["default_revision"], final_default)
            self.assertEqual(stored[1]["pr_base_oid"], final_default)
            self.assertEqual(target_head, self.git("git", "rev-parse", "HEAD", cwd=target))

    def test_authority_or_history_without_receipt_is_rejected(self) -> None:
        cases = (
            "task.md",
            "issue.json",
            "contract.json",
            "verification.json",
            "work-units.json",
            "unknown.json",
            "unknown-directory",
        )
        for name in cases:
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                state = target / ".task-state"
                state.mkdir(mode=0o700)
                state.chmod(0o700)
                entry = state / name
                if name == "unknown-directory":
                    entry.mkdir(mode=0o700)
                else:
                    entry.write_bytes(b"{}\n")
                    entry.chmod(0o600)
                with self.assertRaises(recovery.TaskStateRecoveryError):
                    recovery._state_topology(target, False)

    def test_unknown_historical_evidence_is_rejected_with_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            state = target / ".task-state"
            state.mkdir(mode=0o700)
            (state / "verification.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "unsupported partial"):
                recovery._state_topology(target, True)

    def test_every_reconstructed_state_file_must_be_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.git("git", "init", "-b", "main", cwd=target)
            self.git("git", "config", "user.name", "Task State Recovery Test", cwd=target)
            self.git("git", "config", "user.email", "task-state-recovery@example.invalid", cwd=target)
            (target / ".gitignore").write_text(".task-state/task.md\n", encoding="utf-8")
            (target / "seed").write_text("seed\n", encoding="utf-8")
            self.git("git", "add", ".", cwd=target)
            self.git("git", "commit", "-m", "seed", cwd=target)
            with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "issue.json"):
                recovery._require_ignored_state(target)

    def test_exact_fixture_reconstructs_only_three_state_files_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            plan = self.plan(target)
            receipt_path = target / "private" / "lost-ignored-task-state.json"
            receipt_path.parent.mkdir()
            calls = iter((None, None))

            def read_receipt(_path: Path):
                try:
                    return next(calls)
                except StopIteration:
                    return (plan["receipt_bytes"], plan["receipt"])

            def write_receipt(path: Path, content: bytes, **_: object):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            with (
                mock.patch.object(recovery, "_plan", return_value=plan),
                mock.patch.object(recovery, "_read_receipt", side_effect=read_receipt),
                mock.patch.object(recovery.private_state, "lost_ignored_task_state_receipt", return_value=receipt_path),
                mock.patch.object(recovery.private_state, "_validate_canonical"),
                mock.patch.object(recovery.private_state, "topology"),
                mock.patch.object(recovery.private_state, "mutation_lock", return_value=nullcontext()),
                mock.patch.object(recovery.private_state, "exclusive_write_bytes", side_effect=write_receipt),
                mock.patch.object(recovery.contract, "check_resume_contract", return_value={"mode": "resume", "taskStatus": "implementing"}),
                mock.patch.object(recovery, "_git", return_value=""),
            ):
                first = recovery.recover_missing_task_state(
                    ROOT,
                    target,
                    self.TASK,
                    self.PR,
                    self.MAIN,
                )
                self.assertEqual(first["status"], "TASK_STATE_RECOVERED")
                self.assertEqual(first["githubMutations"], 0)
                second = recovery.recover_missing_task_state(
                    ROOT,
                    target,
                    self.TASK,
                    self.PR,
                    self.MAIN,
                )
            self.assertEqual(second["status"], "TASK_STATE_ALREADY_RECOVERED")
            self.assertEqual(
                sorted(path.name for path in (target / ".task-state").iterdir()),
                ["contract.json", "issue.json", "task.md", "work-units.lock"],
            )
            self.assertFalse((target / ".task-state/verification.json").exists())

    def test_real_registered_fixture_recovers_and_resumes_without_product_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                _repository,
                source,
                target,
                base,
                main,
                implementation,
            ) = self.exact_fixture(Path(directory))
            target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
            legacy_private_state = recovery.private_state.admin_git_dir(target) / "opencode/automation-maintenance/legacy.json"
            legacy_private_state.parent.mkdir(parents=True)
            legacy_private_state.write_bytes(b"historical private state\n")
            rest_payload = self.rest_pr(target_head, base)
            rest_response = subprocess.CompletedProcess(
                ["gh", "api"], 0, json.dumps([[rest_payload]]), ""
            )
            before = self.git("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target)
            with (
                mock.patch.object(recovery.lifecycle, "gh", return_value=rest_response),
                mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                mock.patch.object(recovery.lifecycle, "remote_branch_head", return_value=target_head),
            ):
                plan = recovery._plan(source, target, self.TASK, self.PR, implementation)
                first = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )
                second = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )
                conflicting_issue = target / ".task-state/issue.json"
                conflicting_issue.write_bytes(b"conflicting\n")
                with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "conflicting"):
                    recovery.recover_missing_task_state(
                        source, target, self.TASK, self.PR, implementation
                    )
                self.assertEqual(conflicting_issue.read_bytes(), b"conflicting\n")
            self.assertEqual(plan["base"], base)
            self.assertEqual(plan["default_revision"], main)
            self.assertNotEqual(base, main)
            self.assertEqual(plan["receipt"]["base_revision"], base)
            self.assertEqual(plan["receipt"]["default_revision"], main)
            self.assertEqual(plan["receipt"]["pr_base_oid"], base)
            self.assertEqual(first["status"], "TASK_STATE_RECOVERED")
            self.assertEqual(second["status"], "TASK_STATE_ALREADY_RECOVERED")
            self.assertEqual(before, self.git("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target))
            self.assertEqual(target_head, self.git("git", "rev-parse", "HEAD", cwd=target))
            self.assertEqual(
                {"contract.json", "issue.json", "task.md", "work-units.lock"},
                {path.name for path in (target / ".task-state").iterdir()},
            )
            self.assertFalse((target / ".task-state/verification.json").exists())
            self.assertEqual(first["taskStatus"], "implementing")
            receipt = recovery.private_state.lost_ignored_task_state_receipt(target)
            self.assertTrue(receipt.is_file())
            self.assertEqual(legacy_private_state.read_bytes(), b"historical private state\n")

    def test_retry_refreshes_default_and_pr_base_observations_after_main_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, source, target, base, first_main, implementation = self.exact_fixture(
                Path(directory)
            )
            target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
            first_pr = self.normalized_pr(target_head, base)
            tracked_before = self.git(
                "git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target
            )
            with (
                mock.patch.object(
                    recovery.lifecycle,
                    "pull_requests_for_branch",
                    return_value=[first_pr],
                ),
                mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                mock.patch.object(
                    recovery.lifecycle, "remote_branch_head", return_value=target_head
                ),
            ):
                first = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )
            self.assertEqual(first["status"], "TASK_STATE_RECOVERED")

            (repository / "main-advanced-after-recovery.txt").write_text(
                "default advanced after initial recovery\n", encoding="utf-8"
            )
            self.git("git", "add", "main-advanced-after-recovery.txt", cwd=repository)
            self.git("git", "commit", "-m", "advance main after recovery", cwd=repository)
            current_main = self.git("git", "rev-parse", "HEAD", cwd=repository)
            self.remote_default_revision = current_main
            updated_pr = self.normalized_pr(target_head, current_main)
            with (
                mock.patch.object(
                    recovery.lifecycle,
                    "pull_requests_for_branch",
                    return_value=[updated_pr],
                ),
                mock.patch.object(recovery, "_issue_runner", return_value=self.issue_runner()),
                mock.patch.object(
                    recovery.lifecycle, "remote_branch_head", return_value=target_head
                ),
            ):
                retry = recovery.recover_missing_task_state(
                    source, target, self.TASK, self.PR, implementation
                )

            receipt_path = recovery.private_state.lost_ignored_task_state_receipt(target)
            receipt = recovery._read_receipt(receipt_path)
            assert receipt is not None
            self.assertEqual(retry["status"], "TASK_STATE_ALREADY_RECOVERED")
            self.assertEqual(receipt[1]["base_revision"], base)
            self.assertEqual(receipt[1]["default_revision"], current_main)
            self.assertEqual(receipt[1]["pr_base_oid"], current_main)
            self.assertEqual(target_head, self.git("git", "rev-parse", "HEAD", cwd=target))
            self.assertEqual(
                tracked_before,
                self.git("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target),
            )

    def test_real_rest_pr_payload_is_normalized_for_recovery(self) -> None:
        payload = self.rest_pr(self.HEAD, self.BASE)
        response = subprocess.CompletedProcess(
            ["gh", "api"], 0, json.dumps([[payload]]), ""
        )
        with mock.patch.object(recovery.lifecycle, "gh", return_value=response) as query:
            normalized = recovery.lifecycle.pull_requests_for_branch(
                Path("/tmp/163"), self.BRANCH, "uPiscium/Templates"
            )
        self.assertEqual(
            normalized,
            [
                self.normalized_pr(self.HEAD),
            ],
        )
        self.assertEqual(query.call_args.args[:4], ("api", "--method", "GET", "--paginate"))
        self.assertIn("repos/uPiscium/Templates/pulls", query.call_args.args)
        self.assertIn("head=uPiscium:task/163-worktree-dispatch", query.call_args.args)

    def test_pull_request_identity_failures_reject_before_recovery(self) -> None:
        def copy_payload(payload: dict) -> dict:
            return json.loads(json.dumps(payload))

        cases = (
            ("missing", lambda _raw: []),
            ("duplicate", lambda raw: [[raw, copy_payload(raw)]]),
            (
                "duplicate across pages",
                lambda raw: [[copy_payload(raw)], [copy_payload(raw)]],
            ),
            ("boolean-number", lambda raw: [[{**raw, "number": True}]]),
            ("non-object-entry", lambda _raw: [[None]]),
            (
                "abbreviated-head-sha",
                lambda raw: [[{**raw, "head": {**raw["head"], "sha": "a" * 41}}]],
            ),
            (
                "abbreviated-base-sha",
                lambda raw: [[{**raw, "base": {**raw["base"], "sha": "b" * 63}}]],
            ),
            ("wrong-number", lambda raw: [[{**raw, "number": 999}]]),
            ("closed", lambda raw: [[{**raw, "state": "closed"}]]),
            ("invalid-state", lambda raw: [[{**raw, "state": "merged"}]]),
            (
                "open-merged",
                lambda raw: [[{**raw, "state": "open", "merged_at": "2026-01-01T00:00:00Z"}]],
            ),
            (
                "merged",
                lambda raw: [[{**raw, "state": "closed", "merged_at": "2026-01-01T00:00:00Z"}]],
            ),
            (
                "missing-merged-at",
                lambda raw: [[{key: value for key, value in raw.items() if key != "merged_at"}]],
            ),
            ("empty-merged-at", lambda raw: [[{**raw, "merged_at": ""}]]),
            ("malformed-merged-at", lambda raw: [[{**raw, "merged_at": "not-a-timestamp"}]]),
            ("ready", lambda raw: [[{**raw, "draft": False}]]),
            (
                "missing-head-repository",
                lambda raw: [[{**raw, "head": {key: value for key, value in raw["head"].items() if key != "repo"}}]],
            ),
            (
                "wrong-head-repository",
                lambda raw: [[
                    {
                        **raw,
                        "head": {**raw["head"], "repo": {"full_name": "other/repository"}},
                    }
                ]],
            ),
            (
                "cross-repository",
                lambda raw: [[
                    {
                        **raw,
                        "base": {**raw["base"], "repo": {"full_name": "other/repository"}},
                    }
                ]],
            ),
            (
                "wrong-head-ref",
                lambda raw: [[{**raw, "head": {**raw["head"], "ref": "task/163-other"}}]],
            ),
            (
                "wrong-head-sha",
                lambda raw: [[{**raw, "head": {**raw["head"], "sha": "f" * 40}}]],
            ),
            (
                "wrong-base-ref",
                lambda raw: [[{**raw, "base": {**raw["base"], "ref": "release"}}]],
            ),
            (
                "malformed-base-sha",
                lambda raw: [[{**raw, "base": {**raw["base"], "sha": "not-a-full-oid"}}]],
            ),
            (
                "wrong-base-repository",
                lambda raw: [[
                    {
                        **raw,
                        "base": {**raw["base"], "repo": {"full_name": "other/repository"}},
                    }
                ]],
            ),
            ("malformed", lambda _raw: {"not": "pages"}),
            ("api-failure", None),
        )
        for name, response_factory in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                (
                    _repository,
                    source,
                    target,
                    base,
                    main,
                    implementation,
                ) = self.exact_fixture(Path(directory))
                target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
                raw = self.rest_pr(target_head, base)
                if response_factory is None:
                    response = subprocess.CompletedProcess(
                        ["gh", "api"], 1, "", "GitHub API failure\n"
                    )
                else:
                    response = subprocess.CompletedProcess(
                        ["gh", "api"], 0, json.dumps(response_factory(raw)), ""
                    )
                receipt = recovery.private_state.lost_ignored_task_state_receipt(target)
                before = self.git(
                    "git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target
                )
                with (
                    mock.patch.object(recovery.lifecycle, "gh", return_value=response),
                ):
                    with self.assertRaises(recovery.TaskStateRecoveryError):
                        recovery.recover_missing_task_state(
                            source, target, self.TASK, self.PR, implementation
                        )
                self.assertFalse((target / ".task-state").exists())
                self.assertFalse(receipt.exists())
                self.assertEqual(
                    before,
                    self.git("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=target),
                )

    def test_remote_branch_and_dirty_target_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                _repository,
                source,
                target,
                base,
                main,
                implementation,
            ) = self.exact_fixture(Path(directory))
            target_head = self.git("git", "rev-parse", "HEAD", cwd=target)
            pull_request = self.normalized_pr(target_head, base)
            with (
                mock.patch.object(
                    recovery.lifecycle, "pull_requests_for_branch", return_value=[pull_request]
                ),
                mock.patch.object(recovery.lifecycle, "remote_branch_head", return_value=None),
            ):
                with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "remote Task branch"):
                    recovery._plan(source, target, self.TASK, self.PR, implementation)

            (target / "dirty.txt").write_text("untracked\n", encoding="utf-8")
            with self.assertRaisesRegex(recovery.TaskStateRecoveryError, "clean"):
                recovery._plan(source, target, self.TASK, self.PR, implementation)

    def test_bridge_exposes_source_side_command_without_generic_state_set(self) -> None:
        args = bridge.parser().parse_args(
            ["recover-missing-task-state", "/tmp/target", "163", "176", self.MAIN]
        )
        self.assertEqual(
            (args.command, args.issue, args.pr, args.expected_implementation_revision),
            ("recover-missing-task-state", "163", "176", self.MAIN),
        )
        self.assertIn(
            ("task_state_recovery", "components/agent-core/.automation/bin/task_state_recovery.py"),
            bridge.CANONICAL_MODULES,
        )
        with self.assertRaises(bridge.BridgeError):
            bridge.parser().parse_args(
                ["recover-missing-task-state", "/tmp/target", "163", "0176", self.MAIN]
            )

    def test_admin_recipe_uses_the_trusted_python_launcher(self) -> None:
        recipe = (ROOT / "just/agent-core.just").read_text(encoding="utf-8")
        start = recipe.index("recover-missing-task-state target")
        end = recipe.index("\n\n", start)
        command = recipe[start:end]
        self.assertIn("unset LD_PRELOAD", command)
        self.assertIn("selected_python=", command)
        self.assertIn("resolved_python=", command)
        self.assertIn("-I {{quote(tool)}} recover-missing-task-state", command)

    def test_bridge_dispatches_to_verified_source_recovery(self) -> None:
        recovery_module = mock.Mock()
        recovery_module.recover_missing_task_state.return_value = {
            "status": "TASK_STATE_RECOVERED"
        }
        target = Path("/tmp/163-worktree-dispatch").resolve()
        with (
            mock.patch.object(bridge, "_clean_root", return_value=self.MAIN),
            mock.patch.object(bridge, "_verify_bootstrap"),
            mock.patch.object(bridge, "_validate_target_git_configuration"),
            mock.patch.object(bridge, "_verified_modules") as verified,
            mock.patch.object(bridge, "maintenance_environment"),
            mock.patch.object(
                sys,
                "argv",
                ["bridge", "recover-missing-task-state", str(target), "163", "176", self.MAIN],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            verified.return_value.__enter__.return_value = {
                "task_state_recovery": recovery_module
            }
            self.assertEqual(bridge.main(), 0)
        recovery_module.recover_missing_task_state.assert_called_once_with(
            bridge.ROOT, target, "163", 176, self.MAIN
        )
        self.assertEqual(json.loads(output.getvalue())["implementationRevision"], self.MAIN)
