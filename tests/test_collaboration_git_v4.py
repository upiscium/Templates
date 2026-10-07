from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components/agent-core-v4"))

from collaboration_git import (  # noqa: E402
    BranchPush,
    DistinctTaskGitError,
    LocalFacts,
    TaskGit,
)
import collaboration_git as collaboration_git_module  # noqa: E402
from metadata_ref import MetadataRefError, MetadataStore  # noqa: E402


_REPOSITORY = "acme/widgets"
_TASK = "194"
_BRANCH = "refs/heads/task/194"
_DEFAULT = "refs/heads/main"


def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


class CollaborationGitV4Test(unittest.TestCase):
    """Actual Git fixtures stay entirely inside a temporary local repository."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="collaboration-git-v4-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.remote = self.temp / "remote.git"
        self.product = self.temp / "product"
        git("init", "--bare", "--initial-branch=main", str(self.remote))
        git("init", "--initial-branch=main", str(self.product))
        self._configure(self.product)
        (self.product / "tracked.txt").write_text("base\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=self.product)
        git("commit", "-m", "base", cwd=self.product)
        self.base = git("rev-parse", "HEAD", cwd=self.product).decode().strip()
        git("remote", "add", "origin", str(self.remote), cwd=self.product)
        git("push", "origin", "main", cwd=self.product)
        git("switch", "-c", "task/194", cwd=self.product)
        self.store = MetadataStore(self.product, _REPOSITORY)
        self.publish_calls: list[BranchPush] = []
        self.task_git = self._task_git()

    @staticmethod
    def _configure(root: Path) -> None:
        git("config", "user.name", "Collaboration Git Test", cwd=root)
        git("config", "user.email", "collaboration-git@example.invalid", cwd=root)

    def _task_git(self, *, publish=None, fetch_remote=None, branch: str = _BRANCH) -> TaskGit:
        def normal_publish(intent: BranchPush) -> None:
            self.publish_calls.append(intent)
            observed = self._remote_head()
            if observed != intent.expected_remote:
                raise RuntimeError("fixture remote compare failed")
            git(
                "push", "--no-follow-tags", "origin",
                f"{intent.subject}:{intent.branch_ref}", cwd=self.product,
            )

        return TaskGit(
            self.store,
            task=_TASK,
            branch_ref=branch,
            base_revision=self.base,
            default_branch_ref=_DEFAULT,
            publish=normal_publish if publish is None else publish,
            fetch_remote=fetch_remote,
        )

    def _remote_head(self) -> str | None:
        raw = git("ls-remote", "--refs", "origin", _BRANCH, cwd=self.product)
        if not raw:
            return None
        return raw.split(b"\t", 1)[0].decode("ascii")

    def _advance_task(self, message: str = "task change") -> str:
        with (self.product / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")
        git("add", "tracked.txt", cwd=self.product)
        git("commit", "-m", message, cwd=self.product)
        return git("rev-parse", "HEAD", cwd=self.product).decode("ascii").strip()

    def _state(self) -> tuple[bytes, bytes, bytes]:
        return (
            git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.product),
            git("ls-files", "--stage", "-z", cwd=self.product),
            git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=self.product),
        )

    def _remote_child_elsewhere(self) -> tuple[str, Path]:
        clone = self.temp / "remote-writer"
        git("clone", str(self.remote), str(clone))
        self._configure(clone)
        git("switch", "-c", "remote-task", cwd=clone)
        with (clone / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("remote only\n")
        git("add", "tracked.txt", cwd=clone)
        git("commit", "-m", "remote only", cwd=clone)
        oid = git("rev-parse", "HEAD", cwd=clone).decode("ascii").strip()
        git("push", "origin", f"{oid}:{_BRANCH}", cwd=clone)
        return oid, clone

    def test_observation_is_frozen_bound_and_read_only(self) -> None:
        before = self._state()
        facts = self.task_git.observe()
        self.assertIsInstance(facts, LocalFacts)
        self.assertEqual((_REPOSITORY, _TASK, _BRANCH, self.base), (
            facts.repository, facts.task, facts.branch_ref, facts.head,
        ))
        self.assertTrue(facts.clean)
        self.assertEqual(str(self.product.resolve()), facts.worktree)
        with self.assertRaises(FrozenInstanceError):
            facts.clean = False  # type: ignore[misc]
        self.assertEqual(before, self._state())
        self.assertTrue(self.task_git.is_ancestor(self.base, self.base))

    def test_bootstrap_is_append_only_stable_and_existing_commit_is_a_noop(self) -> None:
        before = self.task_git.observe()
        candidates: list[str] = []

        def record(candidate: str) -> None:
            candidates.append(candidate)
            tree = git("rev-parse", f"{candidate}^{{tree}}", cwd=self.product).decode().strip()
            self.assertEqual(before.tree, tree)

        bootstrapped = self.task_git.bootstrap_if_needed(
            expected_head=self.base, on_candidate=record,
        )
        self.assertEqual([bootstrapped], candidates)
        self.assertEqual(self.base, git("show", "-s", "--format=%P", bootstrapped, cwd=self.product).decode().strip())
        after = self.task_git.observe()
        self.assertTrue(after.clean)
        self.assertEqual(before.tree, after.tree)
        self.assertEqual(before.index_fingerprint, after.index_fingerprint)
        self.assertEqual(before.status_fingerprint, after.status_fingerprint)
        self.assertEqual(bootstrapped, self.task_git.bootstrap_if_needed(expected_head=bootstrapped))
        self.assertEqual(bootstrapped, self.task_git.bootstrap_if_needed(
            expected_head=bootstrapped, resume_candidate=bootstrapped,
        ))
        with self.assertRaises(DistinctTaskGitError):
            self.task_git.bootstrap_if_needed(expected_head=bootstrapped, resume_candidate=self.base)
        self.assertEqual(1, len(candidates))

    def test_bootstrap_locks_out_candidate_head_switch_and_rejects_staged_gitlink(self) -> None:
        original_branch = git("symbolic-ref", "HEAD", cwd=self.product)
        head_lock = Path(git(
            "rev-parse", "--path-format=absolute", "--git-path", "HEAD", cwd=self.product,
        ).decode().strip()).with_name("HEAD.lock")
        index_lock = Path(git(
            "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=self.product,
        ).decode().strip()).with_name("index.lock")
        real_open = os.open
        lock_order: list[str] = []

        def ordered_open(path, flags, *arguments, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(path)).name
                if name in {"HEAD.lock", "index.lock"}:
                    lock_order.append(name)
            return real_open(path, flags, *arguments, **kwargs)

        def attempt_switch(_candidate: str) -> None:
            self.assertTrue(index_lock.exists())
            self.assertTrue(head_lock.exists())
            result = subprocess.run(
                ["git", "symbolic-ref", "HEAD", _DEFAULT], cwd=self.product,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(0, result.returncode)

        with mock.patch.object(collaboration_git_module.os, "open", side_effect=ordered_open) as wrapped_open:
            # The tracing wrapper forwards dir_fd to the real supported API;
            # register that fact so the fail-closed feature probe remains real.
            with mock.patch.object(
                collaboration_git_module.os, "supports_dir_fd", os.supports_dir_fd | {wrapped_open},
            ):
                result = self.task_git.bootstrap_if_needed(expected_head=self.base, on_candidate=attempt_switch)
        self.assertEqual(["index.lock", "HEAD.lock"], lock_order)
        self.assertEqual(result, self.task_git.observe().head)
        self.assertEqual(original_branch, git("symbolic-ref", "HEAD", cwd=self.product))

        git("update-ref", _BRANCH, self.base, result, cwd=self.product)
        git("update-index", "--add", "--cacheinfo", "160000", self.base, "nested", cwd=self.product)
        facts = self.task_git.observe()
        self.assertFalse(facts.clean)
        with self.assertRaises(DistinctTaskGitError) as blocked:
            self.task_git.bootstrap_if_needed(expected_head=self.base)
        self.assertEqual("dirty_worktree", blocked.exception.code)

    def test_resume_candidate_requires_exact_parent_tree_and_message(self) -> None:
        other = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "other",
            cwd=self.product,
        ).decode("ascii").strip()
        wrong = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", other, "-m", "AgentCore Task 194 bootstrap",
            cwd=self.product,
        ).decode("ascii").strip()
        before = self.task_git.observe()
        with self.assertRaises(DistinctTaskGitError) as rejected:
            self.task_git.bootstrap_if_needed(expected_head=self.base, resume_candidate=wrong)
        self.assertEqual("invalid_candidate", rejected.exception.code)
        self.assertEqual(before, self.task_git.observe())

        valid = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base,
            "-m", "AgentCore Task 194 bootstrap", cwd=self.product,
        ).decode("ascii").strip()
        self.assertEqual(valid, self.task_git.bootstrap_if_needed(
            expected_head=self.base, resume_candidate=valid,
        ))

    def test_candidate_false_is_a_refusal_and_bootstrap_lost_ack_is_confirmed(self) -> None:
        with self.assertRaises(DistinctTaskGitError) as refused:
            self.task_git.bootstrap_if_needed(expected_head=self.base, on_candidate=lambda _oid: False)
        self.assertEqual("candidate_callback_failed", refused.exception.code)
        self.assertEqual(self.base, self.task_git.observe().head)

        original = self.store._git

        def update_then_lose_ack(arguments, **kwargs):
            output = original(arguments, **kwargs)
            if "update-ref" in arguments:
                raise MetadataRefError("private fixture acknowledgement detail")
            return output

        with mock.patch.object(self.store, "_git", side_effect=update_then_lose_ack):
            result = self.task_git.bootstrap_if_needed(expected_head=self.base)
        self.assertEqual(result, self.task_git.observe().head)

    def test_existing_task_bootstrap_noop_rechecks_local_facts(self) -> None:
        existing = self._advance_task()
        competing = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "local noop race",
            cwd=self.product,
        ).decode("ascii").strip()
        original = self.task_git.observe
        changed = False

        def observe_then_move() -> LocalFacts:
            nonlocal changed
            facts = original()
            if not changed:
                changed = True
                git("update-ref", "--no-deref", _BRANCH, competing, existing, cwd=self.product)
            return facts

        with mock.patch.object(self.task_git, "observe", side_effect=observe_then_move):
            with self.assertRaises(DistinctTaskGitError) as blocked:
                self.task_git.bootstrap_if_needed(expected_head=existing)
        self.assertEqual("bootstrap_noop_state_changed", blocked.exception.code)
        self.assertEqual(competing, self.task_git.observe().head)

    def test_unrelated_task_history_is_rejected_for_bootstrap_and_first_push(self) -> None:
        orphan = git(
            "commit-tree", f"{self.base}^{{tree}}", "-m", "unrelated orphan task",
            cwd=self.product,
        ).decode("ascii").strip()
        git("update-ref", "--no-deref", _BRANCH, orphan, self.base, cwd=self.product)
        fetches: list[str] = []
        task_git = self._task_git(fetch_remote=lambda oid: fetches.append(oid))
        with self.assertRaises(DistinctTaskGitError) as bootstrap:
            task_git.bootstrap_if_needed(expected_head=orphan)
        self.assertEqual("task_history_conflict", bootstrap.exception.code)
        with self.assertRaises(DistinctTaskGitError) as push:
            task_git.push_exact(expected_head=orphan, expected_remote=None)
        self.assertEqual("task_history_conflict", push.exception.code)
        self.assertEqual([], fetches)
        self.assertEqual(orphan, task_git.observe().head)
        self.assertIsNone(self._remote_head())

    def test_candidate_callback_failure_is_secret_free_and_does_not_update_ref(self) -> None:
        def fail(_candidate: str) -> None:
            raise RuntimeError("ssh://private-token.invalid/stderr")

        with self.assertRaises(DistinctTaskGitError) as rejected:
            self.task_git.bootstrap_if_needed(expected_head=self.base, on_candidate=fail)
        self.assertEqual("candidate_callback_failed", rejected.exception.code)
        self.assertNotIn("private-token", str(rejected.exception))
        self.assertEqual(self.base, self.task_git.observe().head)

    def test_false_push_intent_is_a_refusal(self) -> None:
        subject = self._advance_task()
        with self.assertRaises(DistinctTaskGitError) as refused:
            self.task_git.push_exact(
                expected_head=subject, expected_remote=None, on_intent=lambda _intent: False,
            )
        self.assertEqual("intent_callback_failed", refused.exception.code)
        self.assertEqual([], self.publish_calls)
        self.assertIsNone(self._remote_head())

    def test_exact_push_normal_noop_and_lost_acknowledgement_confirmation(self) -> None:
        subject = self._advance_task()
        self.assertEqual(subject, self.task_git.push_exact(expected_head=subject, expected_remote=None))
        self.assertEqual(subject, self._remote_head())
        self.assertEqual(1, len(self.publish_calls))
        self.assertEqual(subject, self.task_git.push_exact(expected_head=subject, expected_remote=subject))
        self.assertEqual(1, len(self.publish_calls))

        next_subject = self._advance_task("lost acknowledgement")

        def push_then_lose_ack(intent: BranchPush) -> None:
            self.publish_calls.append(intent)
            git("push", "origin", f"{intent.subject}:{intent.branch_ref}", cwd=self.product)
            raise RuntimeError("remote said ssh://private.invalid/denied")

        lost_ack_git = self._task_git(publish=push_then_lose_ack)
        self.assertEqual(next_subject, lost_ack_git.push_exact(
            expected_head=next_subject, expected_remote=subject,
        ))
        self.assertEqual(next_subject, self._remote_head())

    def test_remote_ahead_and_expected_old_mismatch_fail_without_overwrite(self) -> None:
        remote_oid, _clone = self._remote_child_elsewhere()
        facts = self.task_git.observe()
        with self.assertRaises(DistinctTaskGitError) as unavailable:
            self.task_git.push_exact(expected_head=facts.head, expected_remote=remote_oid)
        self.assertEqual("remote_history_unavailable", unavailable.exception.code)

        def fetch_exact(oid: str) -> None:
            git(
                "fetch", "--no-write-fetch-head", "--no-tags", "--no-recurse-submodules",
                "--refmap=", "origin", oid, cwd=self.product,
            )

        with_fetch = self._task_git(fetch_remote=fetch_exact)
        with self.assertRaises(DistinctTaskGitError) as ahead:
            with_fetch.push_exact(expected_head=facts.head, expected_remote=remote_oid)
        self.assertEqual("non_fast_forward", ahead.exception.code)
        self.assertEqual(remote_oid, self._remote_head())
        with self.assertRaises(DistinctTaskGitError) as mismatch:
            self.task_git.push_exact(expected_head=facts.head, expected_remote=None)
        self.assertEqual("remote_head_mismatch", mismatch.exception.code)

    def test_fetch_capability_receives_only_exact_oid_and_cannot_change_local_facts(self) -> None:
        remote_oid, _clone = self._remote_child_elsewhere()
        refs_before = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes", cwd=self.product)
        names_before = git("for-each-ref", "--format=%(refname)", "refs/remotes", cwd=self.product)
        requests: list[str] = []

        def fetch_exact(oid: str) -> None:
            requests.append(oid)
            git(
                "fetch", "--no-write-fetch-head", "--no-tags", "--no-recurse-submodules",
                "--refmap=", "origin", oid, cwd=self.product,
            )

        capable = self._task_git(fetch_remote=fetch_exact)
        with self.assertRaises(DistinctTaskGitError) as rejected:
            capable.push_exact(expected_head=self.base, expected_remote=remote_oid)
        self.assertEqual("non_fast_forward", rejected.exception.code)
        self.assertEqual([remote_oid], requests)
        refs_after = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes", cwd=self.product)
        self.assertEqual(refs_before, refs_after)
        self.assertEqual(names_before, git("for-each-ref", "--format=%(refname)", "refs/remotes", cwd=self.product))

    def test_remote_symbolic_ref_and_default_branch_are_rejected(self) -> None:
        git("symbolic-ref", _BRANCH, _DEFAULT, cwd=self.remote)
        with self.assertRaises(DistinctTaskGitError) as symbolic:
            self.task_git.remote_head()
        self.assertEqual("symbolic_task_ref", symbolic.exception.code)
        with self.assertRaises(DistinctTaskGitError) as default:
            self._task_git(branch=_DEFAULT)
        self.assertEqual("task_branch_is_default", default.exception.code)

    def test_intent_race_does_not_retry_or_overwrite_concurrent_remote_head(self) -> None:
        subject = self._advance_task()
        mainline = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "remote race",
            cwd=self.product,
        ).decode("ascii").strip()

        def move_remote(_intent: BranchPush) -> None:
            git("push", "origin", f"{mainline}:{_BRANCH}", cwd=self.product)

        raced = self._task_git()
        with self.assertRaises(DistinctTaskGitError):
            raced.push_exact(expected_head=subject, expected_remote=None, on_intent=move_remote)
        self.assertEqual(mainline, self._remote_head())
        self.assertNotEqual(subject, self._remote_head())

    def test_lost_ack_postread_requires_exact_remote_and_unchanged_local_facts(self) -> None:
        subject = self._advance_task()
        competing = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "competing task",
            cwd=self.product,
        ).decode("ascii").strip()

        def push_then_move_remote(intent: BranchPush) -> None:
            git("push", "origin", f"{intent.subject}:{intent.branch_ref}", cwd=self.product)
            transfer = "refs/collaboration-git-test/competing"
            git("push", "origin", f"{competing}:{transfer}", cwd=self.product)
            git(
                "--git-dir", str(self.remote), "update-ref", "--no-deref", _BRANCH,
                competing, subject,
            )

        displaced = self._task_git(publish=push_then_move_remote)
        with self.assertRaises(DistinctTaskGitError) as mismatch:
            displaced.push_exact(expected_head=subject, expected_remote=None)
        self.assertEqual("remote_postcondition_mismatch", mismatch.exception.code)
        self.assertEqual(competing, self._remote_head())

        git(
            "--git-dir", str(self.remote), "update-ref", "--no-deref", _BRANCH,
            subject, competing,
        )
        second = self._advance_task("local postread")
        third = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "local concurrent move",
            cwd=self.product,
        ).decode("ascii").strip()

        def push_then_move_local(intent: BranchPush) -> None:
            git("push", "origin", f"{intent.subject}:{intent.branch_ref}", cwd=self.product)
            git("update-ref", "--no-deref", _BRANCH, third, second, cwd=self.product)

        moved = self._task_git(publish=push_then_move_local)
        with self.assertRaises(DistinctTaskGitError) as local_changed:
            moved.push_exact(expected_head=second, expected_remote=subject)
        self.assertEqual("local_state_changed_after_push", local_changed.exception.code)
        self.assertEqual(third, moved.observe().head)
        self.assertEqual(second, self._remote_head())

    def test_push_noop_rechecks_remote_and_local_facts(self) -> None:
        subject = self._advance_task()
        self.assertEqual(subject, self.task_git.push_exact(expected_head=subject, expected_remote=None))
        competing = git(
            "commit-tree", f"{self.base}^{{tree}}", "-p", self.base, "-m", "no-op race",
            cwd=self.product,
        ).decode("ascii").strip()
        transfer = "refs/collaboration-git-test/noop-race"
        git("push", "origin", f"{competing}:{transfer}", cwd=self.product)

        for move_remote in (True, False):
            original = self.task_git._remote_head
            called = False

            def race_after_first_read() -> str | None:
                nonlocal called
                observed = original()
                if not called:
                    called = True
                    if move_remote:
                        git("--git-dir", str(self.remote), "update-ref", "--no-deref", _BRANCH, competing, subject)
                    else:
                        git("update-ref", "--no-deref", _BRANCH, competing, subject, cwd=self.product)
                return observed

            raced = self._task_git()
            with mock.patch.object(raced, "_remote_head", side_effect=race_after_first_read):
                with self.assertRaises(DistinctTaskGitError) as blocked:
                    raced.push_exact(expected_head=subject, expected_remote=subject)
            self.assertEqual("push_noop_postcondition_mismatch", blocked.exception.code)
            if move_remote:
                git("--git-dir", str(self.remote), "update-ref", "--no-deref", _BRANCH, subject, competing)
            else:
                git("update-ref", "--no-deref", _BRANCH, subject, competing, cwd=self.product)

    def test_config_command_risks_fail_closed(self) -> None:
        git("config", "filter.attack.clean", "touch /tmp/should-not-run", cwd=self.product)
        with self.assertRaises(DistinctTaskGitError) as rejected:
            self.task_git.observe()
        self.assertEqual("unsafe_git_config", rejected.exception.code)

        git("config", "--unset", "filter.attack.clean", cwd=self.product)
        git("config", "remote.origin.receivepack", "touch /tmp/should-not-run", cwd=self.product)
        with self.assertRaises(DistinctTaskGitError) as transport:
            self.task_git.remote_head()
        self.assertEqual("unsafe_git_config", transport.exception.code)

    def test_push_postread_failure_is_not_reported_as_success(self) -> None:
        subject = self._advance_task()
        task_git = self._task_git()
        original = task_git._remote_head
        calls = 0

        def fail_postread() -> str | None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise DistinctTaskGitError("remote_unavailable", "push")
            return original()

        with mock.patch.object(task_git, "_remote_head", side_effect=fail_postread):
            with self.assertRaises(DistinctTaskGitError) as unavailable:
                task_git.push_exact(expected_head=subject, expected_remote=None)
        self.assertEqual("push_postcondition_unavailable", unavailable.exception.code)
        self.assertEqual(subject, self._remote_head())

    def test_bootstrap_detached_admin_cas_preserves_sha256_object_format(self) -> None:
        remote = self.temp / "sha256-remote.git"
        product = self.temp / "sha256-product"
        git("init", "--object-format=sha256", "--bare", "--initial-branch=main", str(remote))
        git("init", "--object-format=sha256", "--initial-branch=main", str(product))
        self._configure(product)
        (product / "tracked.txt").write_text("sha256 base\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=product)
        git("commit", "-m", "sha256 base", cwd=product)
        base = git("rev-parse", "HEAD", cwd=product).decode("ascii").strip()
        git("remote", "add", "origin", str(remote), cwd=product)
        git("push", "origin", "main", cwd=product)
        git("switch", "-c", "task/194", cwd=product)
        store = MetadataStore(product, _REPOSITORY)
        task_git = TaskGit(
            store,
            task=_TASK,
            branch_ref=_BRANCH,
            base_revision=base,
            default_branch_ref=_DEFAULT,
            publish=lambda _intent: None,
        )
        candidate = task_git.bootstrap_if_needed(expected_head=base)
        self.assertEqual(64, len(candidate))
        self.assertEqual(base, git("show", "-s", "--format=%P", candidate, cwd=product).decode().strip())

    def test_bootstrap_detached_admin_cas_supports_registered_linked_worktree(self) -> None:
        linked = self.temp / "linked-task-worktree"
        branch = "refs/heads/task/194-linked"
        git("worktree", "add", "-b", "task/194-linked", str(linked), self.base, cwd=self.product)
        store = MetadataStore(linked, _REPOSITORY)
        task_git = TaskGit(
            store,
            task=_TASK,
            branch_ref=branch,
            base_revision=self.base,
            default_branch_ref=_DEFAULT,
            publish=lambda _intent: None,
        )
        candidate = task_git.bootstrap_if_needed(expected_head=self.base)
        self.assertEqual(candidate, task_git.observe().head)
        self.assertEqual(self.base, git("show", "-s", "--format=%P", candidate, cwd=linked).decode().strip())
        self.assertEqual(self.base, git("rev-parse", _BRANCH, cwd=self.product).decode().strip())

    def test_common_dir_swap_cannot_redirect_cas_or_lock_cleanup_to_foreign_repo(self) -> None:
        foreign = self.temp / "foreign.git"
        git("init", "--bare", "--initial-branch=main", str(foreign))
        git(
            "push", str(foreign), f"{self.base}:{_DEFAULT}", f"{self.base}:{_BRANCH}",
            cwd=self.product,
        )
        common = Path(git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.product,
        ).decode().strip())
        moved = self.temp / "original-common.git"
        original = self.store._git
        swapped = False
        candidates: list[str] = []

        def swap_before_update(arguments, **kwargs):
            nonlocal swapped
            if "update-ref" in arguments and not swapped:
                swapped = True
                common.rename(moved)
                common.symlink_to(foreign, target_is_directory=True)
            return original(arguments, **kwargs)

        with mock.patch.object(self.store, "_git", side_effect=swap_before_update):
            with self.assertRaises(DistinctTaskGitError) as blocked:
                self.task_git.bootstrap_if_needed(expected_head=self.base, on_candidate=candidates.append)
        self.assertTrue(swapped)
        self.assertEqual(1, len(candidates))
        self.assertEqual("bootstrap_postcondition_unavailable", blocked.exception.code)
        self.assertEqual(self.base, git("--git-dir", str(foreign), "rev-parse", _BRANCH).decode().strip())
        self.assertEqual(self.base, git("--git-dir", str(foreign), "rev-parse", _DEFAULT).decode().strip())
        self.assertEqual(candidates[0], git("--git-dir", str(moved), "rev-parse", _BRANCH).decode().strip())
        self.assertEqual(self.base, git("--git-dir", str(moved), "rev-parse", _DEFAULT).decode().strip())
        self.assertFalse((moved / "index.lock").exists())
        self.assertFalse((moved / "HEAD.lock").exists())
        self.assertFalse((foreign / "index.lock").exists())
        self.assertFalse((foreign / "HEAD.lock").exists())


if __name__ == "__main__":
    unittest.main()
