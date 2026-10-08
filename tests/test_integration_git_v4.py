from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components/agent-core-v4"))

from integration_git import (  # noqa: E402
    CommitFacts,
    DefaultFacts,
    DefaultGit,
    DefaultReceipt,
    FastForwardRequest,
    FetchRequest,
    SafeDefaultGitError,
)
from metadata_ref import MetadataStore  # noqa: E402


_REPOSITORY = "acme/widgets"
_DEFAULT = "refs/heads/main"


def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


class IntegrationGitV4Test(unittest.TestCase):
    """Integration fixtures mutate only repositories under TemporaryDirectory."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="integration-git-v4-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.remote = self.temp / "origin.git"
        self.main = self.temp / "main"
        self.task = self.temp / "task"
        self.default_link = self.temp / "default-linked"
        self.task_link = self.temp / "task-linked"
        git("init", "--bare", "--initial-branch=main", str(self.remote))
        git("init", "--initial-branch=main", str(self.main))
        self._configure(self.main)
        (self.main / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.main / ".gitignore").write_text("cache/\nreplace-me/cache/\n", encoding="utf-8")
        (self.main / "replace-me").mkdir()
        (self.main / "replace-me" / "tracked").write_text("tracked child\n", encoding="utf-8")
        (self.main / "prefix").mkdir()
        (self.main / "prefix.a").write_text("file before prefix directory\n", encoding="utf-8")
        (self.main / "prefix" / "child").write_text("directory after prefix file\n", encoding="utf-8")
        git("add", ".gitignore", "tracked.txt", "replace-me/tracked", "prefix.a", "prefix/child", cwd=self.main)
        git("commit", "-m", "base", cwd=self.main)
        self.base = self._oid(self.main, "HEAD")
        git("remote", "add", "origin", str(self.remote), cwd=self.main)
        git("push", "--no-follow-tags", "origin", f"{_DEFAULT}:{_DEFAULT}", cwd=self.main)
        git("clone", str(self.remote), str(self.task))
        self._configure(self.task)
        git("switch", "-c", "task/940", cwd=self.task)

        self.store = MetadataStore(self.main, _REPOSITORY)
        self.fetch_requests: list[FetchRequest] = []
        self.ff_requests: list[FastForwardRequest] = []
        self.default_git = self._make_default_git()

    @staticmethod
    def _configure(root: Path) -> None:
        git("config", "user.name", "Integration Git Test", cwd=root)
        git("config", "user.email", "integration-git@example.invalid", cwd=root)

    @staticmethod
    def _oid(root: Path, revision: str) -> str:
        return git("rev-parse", "--verify", revision, cwd=root).decode("ascii").strip()

    def _make_default_git(self, *, root: Path | None = None, fetch=None, fast_forward=None) -> DefaultGit:
        worktree = self.main if root is None else root
        store = self.store if worktree == self.main else MetadataStore(worktree, _REPOSITORY)
        holder: dict[str, DefaultGit] = {}

        def fetch_default(request: FetchRequest) -> object:
            self.fetch_requests.append(request)
            result = b""
            for oid in dict.fromkeys((request.expected_remote, request.merged_oid)):
                result = git(
                    "-C", str(worktree), "fetch", "--no-tags", "--no-write-fetch-head",
                    "--no-recurse-submodules", "--refmap=", "origin", oid,
                )
            return result

        def fixture_fast_forward(request: FastForwardRequest) -> object:
            # This is a controlled TemporaryDirectory fixture: it validates
            # the pinned path/ref/old OID and exact local fingerprints before
            # a non-forcing FF.  It is not a production CAS implementation;
            # production activation still requires the documented host cap.
            self.ff_requests.append(request)
            adapter = holder["adapter"]
            facts = adapter.observe()
            if (
                request.repository != _REPOSITORY
                or request.branch_ref != _DEFAULT
                or request.worktree != str(worktree.resolve())
                or facts.head != request.expected_head
                or not facts.clean
                or facts.index_fingerprint != request.expected_index_fingerprint
                or facts.status_fingerprint != request.expected_status_fingerprint
                or adapter.remote_head() != request.target_head
                or git("symbolic-ref", "HEAD", cwd=worktree).decode().strip() != _DEFAULT
            ):
                raise RuntimeError("fixture host compare failed")
            status = git(
                "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=all", cwd=worktree,
            )
            if status:
                raise RuntimeError("fixture host requires a clean worktree")
            git(
                "-C", str(worktree), "-c", "core.hooksPath=/dev/null",
                "merge", "--ff-only", request.target_head,
            )
            return True

        adapter = DefaultGit(
            store,
            default_branch_ref=_DEFAULT,
            fetch=fetch_default if fetch is None else fetch,
            fast_forward=fixture_fast_forward if fast_forward is None else fast_forward,
        )
        holder["adapter"] = adapter
        return adapter

    def _approved_merge(self, *, replace_directory: bool = False) -> tuple[str, str]:
        """Make a local synthetic two-parent merge and advance only the bare fixture."""
        if replace_directory:
            git("rm", "replace-me/tracked", cwd=self.task)
            (self.task / "replace-me").write_text("replacement file\n", encoding="utf-8")
            git("add", "replace-me", cwd=self.task)
        else:
            with (self.task / "tracked.txt").open("a", encoding="utf-8") as stream:
                stream.write("approved task change\n")
            git("add", "tracked.txt", cwd=self.task)
        git("commit", "-m", "task change", cwd=self.task)
        task_commit = self._oid(self.task, "HEAD")
        tree = self._oid(self.task, "HEAD^{tree}")
        merge = git(
            "commit-tree", tree, "-p", self.base, "-p", task_commit,
            "-m", "Merge approved task change", cwd=self.task,
        ).decode("ascii").strip()
        self._publish_fixture_commit(merge, self.base)
        return merge, task_commit

    def _publish_fixture_commit(self, oid: str, old_default: str) -> None:
        current = git("rev-parse", "--verify", "refs/heads/fixture-merge", cwd=self.task, check=False)
        update = ["update-ref", "refs/heads/fixture-merge", oid]
        if current:
            update.append(current.decode("ascii").strip())
        git(*update, cwd=self.task)
        git(
            # Plain push is deliberately non-forcing; all destinations are the
            # temporary bare fixture's transfer ref.
            "push", "--no-follow-tags", "origin",
            "refs/heads/fixture-merge:refs/heads/fixture-transfer", cwd=self.task,
        )
        git("--git-dir", str(self.remote), "update-ref", _DEFAULT, oid, old_default)

    def _later_tip_unpublished(self, merge: str) -> str:
        tree = self._oid(self.task, f"{merge}^{{tree}}")
        return git(
            "commit-tree", tree, "-p", merge, "-m", "later remote-only commit", cwd=self.task,
        ).decode("ascii").strip()

    def _later_tip(self, merge: str) -> str:
        later_tip = self._later_tip_unpublished(merge)
        self._publish_fixture_commit(later_tip, merge)
        return later_tip

    @staticmethod
    def _refs(root: Path) -> dict[str, str]:
        output = git("for-each-ref", "--format=%(objectname) %(refname)", "refs/", cwd=root)
        refs: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise AssertionError("fixture ref listing was malformed")
            refs[fields[1].decode("ascii")] = fields[0].decode("ascii")
        return refs

    def _snapshot(self) -> tuple[bytes, bytes, bytes, bytes]:
        return (
            git("rev-parse", "HEAD", "HEAD^{tree}", cwd=self.main),
            git("ls-files", "--stage", "-z", cwd=self.main),
            git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=self.main),
            git("rev-parse", "--git-path", "FETCH_HEAD", cwd=self.main),
        )

    def test_public_facts_are_frozen_exact_and_read_only(self) -> None:
        before = self._snapshot()
        facts = self.default_git.observe()
        self.assertIsInstance(facts, DefaultFacts)
        self.assertEqual((_REPOSITORY, _DEFAULT, self.base), (
            facts.repository, facts.branch_ref, facts.head,
        ))
        self.assertEqual(str(self.main.resolve()), facts.worktree)
        self.assertTrue(facts.clean)
        with self.assertRaises(FrozenInstanceError):
            facts.clean = False  # type: ignore[misc]
        self.assertEqual(self.base, self.default_git.remote_head())
        base_facts = self.default_git.commit_facts(self.base)
        self.assertIsInstance(base_facts, CommitFacts)
        self.assertEqual((), base_facts.parents)
        self.assertTrue(self.default_git.is_ancestor(self.base, self.base))
        self.assertEqual(before, self._snapshot())
        self.assertEqual([], self.fetch_requests)
        self.assertEqual([], self.ff_requests)

    def test_reconcile_confirms_two_parent_remote_commit_and_preserves_ignored_cache(self) -> None:
        cache = self.main / "cache" / "local-data.bin"
        cache.parent.mkdir()
        cache.write_bytes(b"must survive fast-forward\0")
        config_before = (self.main / ".git" / "config").read_bytes()
        refs_before = self._refs(self.main)
        fetch_head = self.main / ".git" / "FETCH_HEAD"
        fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None
        merge, task_commit = self._approved_merge()
        intent: list[FastForwardRequest] = []

        receipt = self.default_git.reconcile(
            merged_oid=merge,
            expected_remote_oid=merge,
            on_intent=lambda request: intent.append(request),
        )

        self.assertIsInstance(receipt, DefaultReceipt)
        self.assertEqual((self.base, merge, merge), (receipt.old_head, receipt.new_head, receipt.merged_oid))
        self.assertEqual(self._oid(self.main, "HEAD^{tree}"), receipt.tree)
        self.assertEqual((self.base, task_commit), self.default_git.commit_facts(merge).parents)
        self.assertEqual(1, len(self.fetch_requests))
        self.assertEqual(FetchRequest(_REPOSITORY, _DEFAULT, merge, merge), self.fetch_requests[0])
        self.assertEqual(1, len(self.ff_requests))
        self.assertEqual(self.ff_requests[0], intent[0])
        self.assertEqual((self.base, merge, merge), (
            intent[0].expected_head, intent[0].target_head, intent[0].merged_oid,
        ))
        self.assertEqual((b"tracked.txt",), intent[0].changed_paths)
        self.assertEqual(b"must survive fast-forward\0", cache.read_bytes())
        self.assertTrue(self.default_git.observe().clean)
        self.assertEqual(merge, self.default_git.remote_head())
        refs_after = self._refs(self.main)
        self.assertEqual(merge, refs_after.pop(_DEFAULT))
        refs_before.pop(_DEFAULT)
        self.assertEqual(refs_before, refs_after)
        self.assertEqual(config_before, (self.main / ".git" / "config").read_bytes())
        self.assertEqual(fetch_head_before, fetch_head.read_bytes() if fetch_head.exists() else None)

    def test_merge_ancestor_of_later_remote_tip_fast_forwards_to_actual_tip(self) -> None:
        merge, _task_commit = self._approved_merge()
        later_tip = self._later_tip(merge)

        receipt = self.default_git.reconcile(merged_oid=merge, expected_remote_oid=later_tip)

        self.assertEqual(later_tip, receipt.new_head)
        self.assertEqual(merge, receipt.merged_oid)
        self.assertEqual((later_tip, merge), (
            self.fetch_requests[0].expected_remote, self.fetch_requests[0].merged_oid,
        ))
        self.assertEqual(later_tip, self._oid(self.main, "HEAD"))

    def test_linked_default_and_task_worktrees_are_bound_to_the_shared_common_store(self) -> None:
        merge, _task_commit = self._approved_merge()
        git("switch", "-c", "fixture/admin", cwd=self.main)
        git("worktree", "add", str(self.default_link), "main", cwd=self.main)
        git("worktree", "add", "-b", "task/940-linked", str(self.task_link), self.base, cwd=self.main)
        default_common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.default_link)
        task_common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.task_link)
        self.assertEqual(default_common, task_common)
        self.assertNotEqual(self.main.resolve(), self.default_link.resolve())
        self.assertEqual(_DEFAULT, git("symbolic-ref", "HEAD", cwd=self.default_link).decode().strip())

        linked_git = self._make_default_git(root=self.default_link)
        receipt = linked_git.reconcile(merged_oid=merge, expected_remote_oid=merge)

        self.assertEqual(merge, receipt.new_head)
        self.assertEqual(merge, self._oid(self.default_link, "HEAD"))
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual("refs/heads/task/940-linked", git("symbolic-ref", "HEAD", cwd=self.task_link).decode().strip())
        self.assertEqual(self.base, self._oid(self.task_link, "HEAD"))
        with self.assertRaises(SafeDefaultGitError):
            DefaultGit(
                MetadataStore(self.task_link, _REPOSITORY),
                default_branch_ref=_DEFAULT,
                fetch=lambda _request: None,
                fast_forward=lambda _request: None,
            )

    def test_noop_still_checks_exact_merge_graph_remote_and_clean_state(self) -> None:
        merge, _task_commit = self._approved_merge()
        self.default_git._fetch(FetchRequest(_REPOSITORY, _DEFAULT, merge, merge))
        before = self.default_git.observe()
        target_tree = self._oid(self.task, f"{merge}^{{tree}}")
        target_index = self.default_git._index_for_tree(target_tree)[0]
        self.default_git._fast_forward(
            FastForwardRequest(
                _REPOSITORY, _DEFAULT, str(self.main.resolve()), self.base, merge, merge,
                target_tree, before.index_fingerprint, before.status_fingerprint, target_index, (),
            )
        )
        self.ff_requests.clear()
        before_fetches = len(self.fetch_requests)

        receipt = self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)

        self.assertEqual((merge, merge), (receipt.old_head, receipt.new_head))
        self.assertEqual(before_fetches + 1, len(self.fetch_requests))
        self.assertEqual([], self.ff_requests)

    def test_dirty_staged_and_untracked_data_are_never_cleared(self) -> None:
        merge, _task_commit = self._approved_merge()
        with (self.main / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("staged local change\n")
        git("add", "tracked.txt", cwd=self.main)
        staged_before = git("ls-files", "--stage", "-z", cwd=self.main)
        with (self.main / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("unstaged local change\n")
        unknown = self.main / "untracked-data.txt"
        unknown.write_text("preserve\n", encoding="utf-8")
        content_before = (self.main / "tracked.txt").read_bytes()
        with self.assertRaises(SafeDefaultGitError) as blocked:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual("dirty_default_worktree", blocked.exception.code)
        self.assertEqual(content_before, (self.main / "tracked.txt").read_bytes())
        self.assertEqual(staged_before, git("ls-files", "--stage", "-z", cwd=self.main))
        self.assertEqual("preserve\n", unknown.read_text(encoding="utf-8"))
        self.assertEqual([], self.fetch_requests)
        self.assertEqual([], self.ff_requests)

    def test_clean_local_only_history_is_not_rewritten_to_remote(self) -> None:
        merge, _task_commit = self._approved_merge()
        local_only = self.main / "local-only.txt"
        local_only.write_text("keep local history\n", encoding="utf-8")
        git("add", "local-only.txt", cwd=self.main)
        git("commit", "-m", "local-only commit", cwd=self.main)
        local_head = self._oid(self.main, "HEAD")

        with self.assertRaises(SafeDefaultGitError) as blocked:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)

        self.assertEqual("default_branch_not_fast_forwardable", blocked.exception.code)
        self.assertEqual(local_head, self._oid(self.main, "HEAD"))
        self.assertEqual("keep local history\n", local_only.read_text(encoding="utf-8"))
        self.assertEqual([], self.ff_requests)

    def test_ignored_child_blocks_directory_to_file_replacement(self) -> None:
        ignored = self.main / "replace-me" / "cache" / "keep.bin"
        ignored.parent.mkdir()
        ignored.write_bytes(b"not disposable\0")
        merge, _task_commit = self._approved_merge(replace_directory=True)
        with self.assertRaises(SafeDefaultGitError) as blocked:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual("untracked_path_collision", blocked.exception.code)
        self.assertEqual(b"not disposable\0", ignored.read_bytes())
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

    def test_intent_false_or_exception_refuses_host_ff_after_revalidation(self) -> None:
        merge, _task_commit = self._approved_merge()
        with self.assertRaises(SafeDefaultGitError) as refused:
            self.default_git.reconcile(
                merged_oid=merge,
                expected_remote_oid=merge,
                on_intent=lambda _request: False,
            )
        self.assertEqual("intent_callback_failed", refused.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

        with self.assertRaises(SafeDefaultGitError) as raised:
            self.default_git.reconcile(
                merged_oid=merge,
                expected_remote_oid=merge,
                on_intent=lambda _request: (_ for _ in ()).throw(RuntimeError("do not expose")),
            )
        self.assertEqual("intent_callback_failed", raised.exception.code)
        self.assertNotIn("do not expose", str(raised.exception))
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))

    def test_intent_hook_may_append_only_the_metadata_ref_before_fast_forward(self) -> None:
        merge, _task_commit = self._approved_merge()
        metadata_commit = git(
            "commit-tree", self._oid(self.main, "HEAD^{tree}"),
            "-m", "temporary reconciliation-intent fixture", cwd=self.main,
        ).decode("ascii").strip()

        def record_intent(_request: FastForwardRequest) -> bool:
            git("update-ref", "refs/agentcore/metadata", metadata_commit, cwd=self.main)
            return True

        receipt = self.default_git.reconcile(
            merged_oid=merge,
            expected_remote_oid=merge,
            on_intent=record_intent,
        )

        self.assertEqual(merge, receipt.new_head)
        self.assertEqual(
            metadata_commit,
            git("rev-parse", "refs/agentcore/metadata", cwd=self.main).decode("ascii").strip(),
        )

    def test_lost_fast_forward_ack_is_confirmed_only_from_postcondition(self) -> None:
        merge, _task_commit = self._approved_merge()
        fixture = self._make_default_git()

        def apply_then_lose_ack(request: FastForwardRequest) -> object:
            fixture._fast_forward(request)
            raise RuntimeError("lost host acknowledgement")

        self.default_git = self._make_default_git(fast_forward=apply_then_lose_ack)
        receipt = self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual(merge, receipt.new_head)
        self.assertEqual(merge, self._oid(self.main, "HEAD"))

    def test_false_fast_forward_ack_after_exact_application_is_confirmed_from_poststate(self) -> None:
        merge, _task_commit = self._approved_merge()
        fixture = self._make_default_git()

        def apply_then_return_false(request: FastForwardRequest) -> bool:
            fixture._fast_forward(request)
            return False

        self.default_git = self._make_default_git(fast_forward=apply_then_return_false)
        receipt = self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual(merge, receipt.new_head)
        self.assertEqual(merge, self._oid(self.main, "HEAD"))

    def test_false_fetch_ack_is_not_authority_but_missing_merge_commit_blocks(self) -> None:
        merge, _task_commit = self._approved_merge()

        def false_fetch(request: FetchRequest) -> object:
            self.fetch_requests.append(request)
            for oid in dict.fromkeys((request.expected_remote, request.merged_oid)):
                git(
                    "-C", str(self.main), "fetch", "--no-tags", "--no-write-fetch-head",
                    "--no-recurse-submodules", "--refmap=", "origin", oid,
                )
            return False

        self.default_git = DefaultGit(
            self.store, default_branch_ref=_DEFAULT, fetch=false_fetch,
            fast_forward=self.default_git._fast_forward,
        )
        receipt = self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual(merge, receipt.new_head)

        self.default_git = self._make_default_git()
        absent = "0" * len(merge)
        with self.assertRaises(SafeDefaultGitError):
            self.default_git.reconcile(merged_oid=absent, expected_remote_oid=merge)
        self.assertEqual(merge, self._oid(self.main, "HEAD"))

    def test_remote_mismatch_is_rejected_before_fetch_and_unapproved_target_is_not_substituted(self) -> None:
        merge, _task_commit = self._approved_merge()
        with self.assertRaises(SafeDefaultGitError) as mismatch:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=self.base)
        self.assertEqual("remote_head_mismatch", mismatch.exception.code)
        self.assertEqual([], self.fetch_requests)

        other = git(
            "commit-tree", self._oid(self.main, "HEAD^{tree}"), "-p", self.base,
            "-m", "unapproved local-only fixture commit", cwd=self.main,
        ).decode("ascii").strip()
        with self.assertRaises(SafeDefaultGitError) as not_merged:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=other)
        self.assertEqual("remote_head_mismatch", not_merged.exception.code)

        # The remote remains on the approved merge.  Supplying a different
        # local-only commit as the alleged merge cannot substitute it.
        with self.assertRaises(SafeDefaultGitError) as not_merged:
            self.default_git.reconcile(merged_oid=other, expected_remote_oid=merge)
        self.assertEqual("merged_commit_not_on_remote", not_merged.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))

    def test_hidden_index_flags_and_replaced_root_fail_closed(self) -> None:
        git("update-index", "--assume-unchanged", "tracked.txt", cwd=self.main)
        with self.assertRaises(SafeDefaultGitError) as hidden:
            self.default_git.observe()
        self.assertEqual("hidden_index_entry", hidden.exception.code)
        git("update-index", "--no-assume-unchanged", "tracked.txt", cwd=self.main)
        git("update-index", "--skip-worktree", "tracked.txt", cwd=self.main)
        with self.assertRaises(SafeDefaultGitError) as sparse_index:
            self.default_git.observe()
        self.assertEqual("hidden_index_entry", sparse_index.exception.code)
        git("update-index", "--no-skip-worktree", "tracked.txt", cwd=self.main)
        tracked_content = (self.main / "tracked.txt").read_bytes()

        sparse_file = self.main / ".git" / "info" / "sparse-checkout"
        sparse_file.write_text("/*\n", encoding="ascii")
        with self.assertRaises(SafeDefaultGitError) as sparse:
            self.default_git.observe()
        self.assertEqual("sparse_checkout_unsupported", sparse.exception.code)
        sparse_file.unlink()

        git("config", "--local", "core.ignorecase", "true", cwd=self.main)
        with self.assertRaises(SafeDefaultGitError) as ambiguous_case:
            self.default_git.observe()
        self.assertEqual("unsafe_git_config", ambiguous_case.exception.code)
        git("config", "--local", "--unset-all", "core.ignorecase", cwd=self.main)
        self.assertEqual(tracked_content, (self.main / "tracked.txt").read_bytes())

        saved = self.temp / "main-saved"
        self.main.rename(saved)
        self.main.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(SafeDefaultGitError) as replaced:
            self.default_git.observe()
        self.assertIn(replaced.exception.code, {"symlink_repository_path", "repository_root_changed"})

    def test_changed_gitlinks_are_refused_without_submodule_recursion(self) -> None:
        git(
            "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor/submodule",
            cwd=self.task,
        )
        tree = git("write-tree", cwd=self.task).decode("ascii").strip()
        task_commit = git(
            "commit-tree", tree, "-p", self.base, "-m", "task gitlink fixture", cwd=self.task,
        ).decode("ascii").strip()
        merge = git(
            "commit-tree", tree, "-p", self.base, "-p", task_commit,
            "-m", "approved gitlink fixture", cwd=self.task,
        ).decode("ascii").strip()
        self._publish_fixture_commit(merge, self.base)

        with self.assertRaises(SafeDefaultGitError) as blocked:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)

        self.assertEqual("unsupported_gitlink_or_symlink", blocked.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

    def test_changed_symlinks_are_refused_without_following_them(self) -> None:
        (self.task / "changed-link").symlink_to("tracked.txt")
        git("add", "changed-link", cwd=self.task)
        git("commit", "-m", "task symlink fixture", cwd=self.task)
        task_commit = self._oid(self.task, "HEAD")
        tree = self._oid(self.task, "HEAD^{tree}")
        merge = git(
            "commit-tree", tree, "-p", self.base, "-p", task_commit,
            "-m", "approved symlink fixture", cwd=self.task,
        ).decode("ascii").strip()
        self._publish_fixture_commit(merge, self.base)

        with self.assertRaises(SafeDefaultGitError) as blocked:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)

        self.assertEqual("unsupported_gitlink_or_symlink", blocked.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

    def test_branch_switch_during_intent_is_not_repaired(self) -> None:
        merge, _task_commit = self._approved_merge()

        def switch_branch(_request: FastForwardRequest) -> bool:
            git("switch", "--detach", self.base, cwd=self.main)
            return True

        with self.assertRaises(SafeDefaultGitError):
            self.default_git.reconcile(
                merged_oid=merge,
                expected_remote_oid=merge,
                on_intent=switch_branch,
            )

        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual(b"", git("symbolic-ref", "--quiet", "HEAD", cwd=self.main, check=False))
        self.assertEqual([], self.ff_requests)

    def test_remote_move_during_fetch_blocks_without_fast_forward(self) -> None:
        merge, _task_commit = self._approved_merge()
        original = self.default_git
        later_tip = self._later_tip_unpublished(merge)

        def move_during_fetch(request: FetchRequest) -> object:
            original._fetch(request)
            self._publish_fixture_commit(later_tip, merge)
            return False

        self.default_git = self._make_default_git(fetch=move_during_fetch)
        with self.assertRaises(SafeDefaultGitError) as after_fetch:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual("remote_ref_changed_during_fetch", after_fetch.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

    def test_remote_move_during_intent_blocks_without_fast_forward(self) -> None:
        merge, _task_commit = self._approved_merge()
        later_tip = self._later_tip_unpublished(merge)

        def move_during_intent(_request: FastForwardRequest) -> bool:
            self._publish_fixture_commit(later_tip, merge)
            return True

        with self.assertRaises(SafeDefaultGitError) as after_intent:
            self.default_git.reconcile(
                merged_oid=merge,
                expected_remote_oid=merge,
                on_intent=move_during_intent,
            )
        self.assertEqual("remote_ref_changed", after_intent.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual([], self.ff_requests)

    def test_fast_forward_error_does_not_roll_back_or_clear_partial_data(self) -> None:
        merge, _task_commit = self._approved_merge()

        def fail_before_write(_request: FastForwardRequest) -> object:
            raise RuntimeError("temporary fixture host failure")

        self.default_git = self._make_default_git(fast_forward=fail_before_write)
        with self.assertRaises(SafeDefaultGitError) as failed:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertEqual("fast_forward_failed_unconfirmed", failed.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertTrue(self.default_git.observe().clean)

        def partial_write_then_fail(_request: FastForwardRequest) -> object:
            tracked = self.main / "tracked.txt"
            tracked.write_bytes(b"partial host write must remain\n")
            raise RuntimeError("partial fixture write")

        self.default_git = self._make_default_git(fast_forward=partial_write_then_fail)
        with self.assertRaises(SafeDefaultGitError) as partial:
            self.default_git.reconcile(merged_oid=merge, expected_remote_oid=merge)
        self.assertNotEqual("fast_forward_applied_state_changed", partial.exception.code)
        self.assertEqual(self.base, self._oid(self.main, "HEAD"))
        self.assertEqual(b"partial host write must remain\n", (self.main / "tracked.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
