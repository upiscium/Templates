from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import agent_core_admin_identity as identity  # noqa: E402


REPOSITORY = "example-owner/example-repo"
BRANCH = "feature/142-cutover"
LINKED_BRANCH = "feature/142-linked"
BASE_BRANCH = "main"


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class AdminIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repository"
        self.root.mkdir()
        run_git(self.root, "init", "--quiet", f"--initial-branch={BRANCH}")
        run_git(self.root, "config", "user.email", "admin-identity@example.invalid")
        run_git(self.root, "config", "user.name", "Admin Identity Test")
        (self.root / "tracked.txt").write_text("identity\n", encoding="utf-8")
        run_git(self.root, "add", "tracked.txt")
        run_git(self.root, "commit", "--quiet", "-m", "initial")
        run_git(self.root, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
        self.head = run_git(self.root, "rev-parse", "HEAD")
        self.issue_number = 141
        self.pr_number = 142

    def tearDown(self) -> None:
        self.temp.cleanup()

    def observe(self, root: Path | None = None) -> identity.TargetFacts:
        return identity.observe_target(
            self.root if root is None else root,
            expected_repository=REPOSITORY,
            expected_branch=BRANCH,
            expected_head=self.head,
        )

    def issue(self, **changes: object) -> dict:
        result = {
            "number": self.issue_number,
            "state": "open",
            "repository_url": f"https://api.github.com/repos/{REPOSITORY}",
        }
        result.update(changes)
        return result

    def pull_request(self, **changes: object) -> dict:
        result = {
            "number": self.pr_number,
            "state": "open",
            "draft": True,
            "body": "A draft PR with no closing-issue text.",
            "base": {"repo": {"full_name": REPOSITORY}, "ref": BASE_BRANCH},
            "head": {
                "repo": {"full_name": REPOSITORY},
                "ref": BRANCH,
                "sha": self.head,
            },
        }
        result.update(changes)
        return result

    def gh_fixture(
        self,
        *,
        issue_values: list[dict] | None = None,
        pr_values: list[dict] | None = None,
    ):
        issue_iter = iter(issue_values or [self.issue(), self.issue()])
        pr_iter = iter(pr_values or [self.pull_request(), self.pull_request()])
        observed: list[list[str]] = []

        def fake(_cwd: Path, args: list[str]):
            observed.append(args)
            if args[0] != "api":
                raise AssertionError(args)
            if args[1] == f"repos/{REPOSITORY}/issues/{self.issue_number}":
                return next(issue_iter)
            if args[1] == f"repos/{REPOSITORY}/pulls/{self.pr_number}":
                return next(pr_iter)
            raise AssertionError(args)

        return fake, observed

    def test_observes_exact_canonical_target_facts(self) -> None:
        self.assertTrue((self.root / ".git").is_dir())
        facts = self.observe()
        self.assertEqual(
            facts,
            identity.TargetFacts(
                root=self.root,
                git_dir=self.root / ".git",
                common_dir=self.root / ".git",
                branch=BRANCH,
                head=self.head,
                repository=REPOSITORY,
            ),
        )

    def test_accepts_canonical_ssh_origin_and_preserves_exact_repository_case(self) -> None:
        run_git(self.root, "remote", "set-url", "origin", f"git@github.com:{REPOSITORY}.git")
        self.assertEqual(self.observe().repository, REPOSITORY)

    def test_rejects_malformed_expected_repository_branch_and_object_ids(self) -> None:
        invalid = (
            {"expected_repository": "example-owner/example-repo/extra"},
            {"expected_branch": "bad..branch"},
            {"expected_head": "a" * 39},
            {"expected_head": "g" * 40},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(identity.AdminIdentityError):
                identity.observe_target(
                    self.root,
                    expected_repository=override.get("expected_repository", REPOSITORY),
                    expected_branch=override.get("expected_branch", BRANCH),
                    expected_head=override.get("expected_head", self.head),
                )

    def test_expected_repository_branch_and_head_must_match_target(self) -> None:
        cases = (
            (REPOSITORY, "other", self.head),
            (REPOSITORY, BRANCH, "a" * 40),
            ("other/repository", BRANCH, self.head),
        )
        for repository, branch, head in cases:
            with self.subTest(repository=repository, branch=branch), self.assertRaises(
                identity.AdminIdentityError
            ):
                identity.observe_target(
                    self.root,
                    expected_repository=repository,
                    expected_branch=branch,
                    expected_head=head,
                )

    def test_rejects_non_repository_root_symlink_and_mounted_root(self) -> None:
        not_root = self.root / "tracked.txt"
        with self.assertRaises(identity.AdminIdentityError):
            self.observe(not_root)

        symlink = Path(self.temp.name) / "root-link"
        symlink.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(identity.AdminIdentityError, "symlink"):
            self.observe(symlink)

        with mock.patch.object(identity.os.path, "ismount", return_value=True):
            with self.assertRaisesRegex(identity.AdminIdentityError, "mounted"):
                self.observe()

        with mock.patch.object(identity.os.path, "ismount", side_effect=[False, True]):
            with self.assertRaisesRegex(identity.AdminIdentityError, "Git directory.*mounted"):
                self.observe()

    def test_observes_canonical_registered_linked_worktree(self) -> None:
        linked = Path(self.temp.name) / "linked"
        run_git(
            self.root,
            "worktree",
            "add",
            "--quiet",
            "-b",
            LINKED_BRANCH,
            str(linked),
            self.head,
        )
        facts = identity.observe_target(
            linked,
            expected_repository=REPOSITORY,
            expected_branch=LINKED_BRANCH,
            expected_head=self.head,
        )
        self.assertEqual(facts.root, linked)
        self.assertEqual(
            facts.git_dir,
            Path(run_git(linked, "rev-parse", "--absolute-git-dir")),
        )
        self.assertEqual(
            facts.common_dir,
            Path(run_git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir")),
        )
        self.assertEqual(facts.branch, LINKED_BRANCH)
        self.assertEqual(facts.head, self.head)
        self.assertEqual(facts.repository, REPOSITORY)

    def test_rejects_forged_linked_worktree_gitfile(self) -> None:
        linked = Path(self.temp.name) / "linked"
        run_git(
            self.root,
            "worktree",
            "add",
            "--quiet",
            "-b",
            LINKED_BRANCH,
            str(linked),
            self.head,
        )
        common_dir = Path(run_git(self.root, "rev-parse", "--absolute-git-dir"))
        forged_git_dir = common_dir / "worktrees" / "forged-registration"
        (linked / ".git").write_text(f"gitdir: {forged_git_dir}\n", encoding="utf-8")
        with self.assertRaisesRegex(identity.AdminIdentityError, "BLOCKED"):
            identity.observe_target(
                linked,
                expected_repository=REPOSITORY,
                expected_branch=LINKED_BRANCH,
                expected_head=self.head,
            )

    def test_rejects_symlinked_linked_worktree_gitfile(self) -> None:
        linked = Path(self.temp.name) / "linked"
        run_git(
            self.root,
            "worktree",
            "add",
            "--quiet",
            "-b",
            LINKED_BRANCH,
            str(linked),
            self.head,
        )
        gitfile = linked / ".git"
        target = linked / "gitfile-target"
        target.write_text(gitfile.read_text(encoding="utf-8"), encoding="utf-8")
        gitfile.unlink()
        gitfile.symlink_to(target)
        with self.assertRaisesRegex(identity.AdminIdentityError, "BLOCKED"):
            identity.observe_target(
                linked,
                expected_repository=REPOSITORY,
                expected_branch=LINKED_BRANCH,
                expected_head=self.head,
            )

    def test_rejects_detached_head(self) -> None:
        run_git(self.root, "checkout", "--quiet", "--detach", self.head)
        with self.assertRaisesRegex(identity.AdminIdentityError, "detached"):
            self.observe()

    def test_rejects_noncanonical_or_ambiguous_origin(self) -> None:
        run_git(self.root, "remote", "set-url", "origin", "https://example.invalid/repo.git")
        with self.assertRaisesRegex(identity.AdminIdentityError, "canonical GitHub"):
            self.observe()

        run_git(self.root, "remote", "set-url", "origin", f"https://github.com/{REPOSITORY}.git")
        run_git(self.root, "remote", "set-url", "--add", "origin", f"git@github.com:{REPOSITORY}.git")
        with self.assertRaisesRegex(identity.AdminIdentityError, "exactly one"):
            self.observe()

    def test_subprocess_scrubs_external_git_environment_overrides(self) -> None:
        real_run = subprocess.run
        captured: list[dict[str, str]] = []

        def inspect(argv, **kwargs):
            if argv[0] == "git":
                captured.append(kwargs["env"])
            return real_run(argv, **kwargs)

        hostile = {
            "GIT_DIR": str(Path(self.temp.name) / "wrong"),
            "GIT_WORK_TREE": str(Path(self.temp.name) / "wrong-worktree"),
            "GIT_INDEX_FILE": str(Path(self.temp.name) / "wrong-index"),
        }
        with mock.patch.dict(os.environ, hostile), mock.patch.object(
            identity.subprocess, "run", side_effect=inspect
        ):
            self.observe()
        self.assertTrue(captured)
        for environment in captured:
            for name in hostile:
                self.assertNotIn(name, environment)
            self.assertFalse(
                set(environment).intersection(hostile),
                "inherited Git variables must not reach subprocesses",
            )
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def test_cutover_returns_snapshot_with_external_task_binding_and_base(self) -> None:
        fake, observed = self.gh_fixture()
        callback = mock.Mock(return_value=True)
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            result = identity.verify_cutover_binding(
                self.observe(),
                issue=self.issue_number,
                pr=self.pr_number,
                expected_base=BASE_BRANCH,
                task_binding=callback,
            )
        self.assertEqual(
            result,
            {
                "target": {
                    "root": str(self.root),
                    "gitDir": str(self.root / ".git"),
                    "commonDir": str(self.root / ".git"),
                    "branch": BRANCH,
                    "head": self.head,
                    "repository": REPOSITORY,
                },
                "issue": {
                    "number": self.issue_number,
                    "repository": REPOSITORY,
                    "state": "open",
                },
                "pullRequest": {
                    "number": self.pr_number,
                    "repository": REPOSITORY,
                    "state": "open",
                    "draft": True,
                    "baseRepository": REPOSITORY,
                    "baseBranch": BASE_BRANCH,
                    "headRepository": REPOSITORY,
                    "headBranch": BRANCH,
                    "head": self.head,
                },
                "association": {
                    "kind": "external-task-binding",
                    "issue": self.issue_number,
                    "pullRequest": self.pr_number,
                    "repository": REPOSITORY,
                },
            },
        )
        callback.assert_called_once_with(self.observe(), self.issue_number, self.pr_number)
        self.assertEqual(len(observed), 4)
        self.assertEqual(
            [entry[1] for entry in observed],
            [
                f"repos/{REPOSITORY}/issues/{self.issue_number}",
                f"repos/{REPOSITORY}/pulls/{self.pr_number}",
                f"repos/{REPOSITORY}/issues/{self.issue_number}",
                f"repos/{REPOSITORY}/pulls/{self.pr_number}",
            ],
        )
        for entry in observed:
            self.assertEqual(entry[0], "api")

    def test_missing_or_non_true_task_binding_fails_closed(self) -> None:
        facts = self.observe()
        with mock.patch.object(identity, "_gh_api") as api:
            with self.assertRaisesRegex(identity.AdminIdentityError, "#194 task_binding"):
                identity.verify_cutover_binding(
                    facts,
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=None,
                )
        api.assert_not_called()

        for decision in (False, 1):
            fake, _ = self.gh_fixture()
            with self.subTest(decision=decision), mock.patch.object(
                identity, "_gh_api", side_effect=fake
            ):
                with self.assertRaisesRegex(identity.AdminIdentityError, "not bound"):
                    identity.verify_cutover_binding(
                        facts,
                        issue=self.issue_number,
                        pr=self.pr_number,
                        expected_base=BASE_BRANCH,
                        task_binding=lambda *_args: decision,
                    )

    def test_task_binding_exception_fails_closed(self) -> None:
        fake, _ = self.gh_fixture()
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            with self.assertRaisesRegex(identity.AdminIdentityError, "task_binding failed"):
                identity.verify_cutover_binding(
                    self.observe(),
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=lambda *_args: (_ for _ in ()).throw(ValueError("binding unavailable")),
                )

    def test_issue_must_be_open_a_real_issue_and_in_the_exact_repository(self) -> None:
        for value, message in (
            (self.issue(state="closed"), "missing, closed"),
            (self.issue(repository_url="https://api.github.com/repos/other/repository"), "another repository"),
            (self.issue(pull_request={"url": "https://api.github.com/repos/example-owner/example-repo/pulls/141"}), "a PR"),
            (self.issue(number=999), "missing, closed"),
        ):
            fake, _ = self.gh_fixture(issue_values=[value])
            with self.subTest(value=value), mock.patch.object(identity, "_gh_api", side_effect=fake):
                with self.assertRaisesRegex(identity.AdminIdentityError, message):
                    identity.verify_cutover_binding(
                        self.observe(),
                        issue=self.issue_number,
                        pr=self.pr_number,
                        expected_base=BASE_BRANCH,
                        task_binding=lambda *_args: True,
                    )

    def test_pr_must_be_open_draft_same_repository_and_exact_branches_and_head(self) -> None:
        cases = (
            (self.pull_request(state="closed"), "must be open, draft"),
            (self.pull_request(draft=False), "must be open, draft"),
            (self.pull_request(head={"repo": {"full_name": "other/repository"}, "ref": BRANCH, "sha": self.head}), "same-repository"),
            (self.pull_request(head={"repo": {"full_name": REPOSITORY}, "ref": "other", "sha": self.head}), "target branch and HEAD"),
            (self.pull_request(head={"repo": {"full_name": REPOSITORY}, "ref": BRANCH, "sha": "a" * 40}), "target branch and HEAD"),
            (self.pull_request(base={"repo": {"full_name": "other/repository"}}), "same-repository"),
            (self.pull_request(base={"repo": {"full_name": REPOSITORY}, "ref": "release"}), "expected base"),
            (self.pull_request(number=999), "different pull request number"),
        )
        for value, message in cases:
            fake, _ = self.gh_fixture(pr_values=[value])
            with self.subTest(value=value), mock.patch.object(identity, "_gh_api", side_effect=fake):
                with self.assertRaisesRegex(identity.AdminIdentityError, message):
                    identity.verify_cutover_binding(
                        self.observe(),
                        issue=self.issue_number,
                        pr=self.pr_number,
                        expected_base=BASE_BRANCH,
                        task_binding=lambda *_args: True,
                    )

    def test_pr_without_closing_text_is_accepted_only_with_task_binding(self) -> None:
        pr_without_closing_text = self.pull_request(body="No closing keyword or Issue reference.")
        fake, _ = self.gh_fixture(pr_values=[pr_without_closing_text, pr_without_closing_text])
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            result = identity.verify_cutover_binding(
                self.observe(),
                issue=self.issue_number,
                pr=self.pr_number,
                expected_base=BASE_BRANCH,
                task_binding=lambda *_args: True,
            )
        self.assertEqual(result["association"]["kind"], "external-task-binding")

        fake, _ = self.gh_fixture(pr_values=[pr_without_closing_text])
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            with self.assertRaisesRegex(identity.AdminIdentityError, "#194 task_binding"):
                identity.verify_cutover_binding(
                    self.observe(),
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=None,
                )

    def test_changed_remote_or_local_identity_during_preflight_is_rejected(self) -> None:
        changed_pr = self.pull_request(
            head={"repo": {"full_name": REPOSITORY}, "ref": BRANCH, "sha": "b" * 40}
        )
        fake, _ = self.gh_fixture(pr_values=[self.pull_request(), changed_pr])
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            with self.assertRaisesRegex(identity.AdminIdentityError, "target branch and HEAD"):
                identity.verify_cutover_binding(
                    self.observe(),
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=lambda *_args: True,
                )

        changed_issue = self.issue(state="closed")
        fake, observed = self.gh_fixture(issue_values=[self.issue(), changed_issue])
        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            with self.assertRaisesRegex(identity.AdminIdentityError, "missing, closed"):
                identity.verify_cutover_binding(
                    self.observe(),
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=lambda *_args: True,
                )
        self.assertEqual(
            [entry[1] for entry in observed],
            [
                f"repos/{REPOSITORY}/issues/{self.issue_number}",
                f"repos/{REPOSITORY}/pulls/{self.pr_number}",
                f"repos/{REPOSITORY}/issues/{self.issue_number}",
            ],
        )

        fake, _ = self.gh_fixture()

        def switch_branch(*_args):
            run_git(self.root, "checkout", "--quiet", "-b", "changed-branch")
            return True

        with mock.patch.object(identity, "_gh_api", side_effect=fake):
            with self.assertRaises(identity.AdminIdentityError):
                identity.verify_cutover_binding(
                    self.observe(),
                    issue=self.issue_number,
                    pr=self.pr_number,
                    expected_base=BASE_BRANCH,
                    task_binding=switch_branch,
                )

    def test_invalid_issue_pr_numbers_and_forged_facts_are_rejected(self) -> None:
        facts = self.observe()
        for issue, pr in ((0, self.pr_number), (True, self.pr_number), (self.issue_number, -1)):
            with self.subTest(issue=issue, pr=pr), self.assertRaises(identity.AdminIdentityError):
                identity.verify_cutover_binding(
                    facts,
                    issue=issue,
                    pr=pr,
                    expected_base=BASE_BRANCH,
                    task_binding=lambda *_args: True,
                )
        forged = identity.TargetFacts(
            facts.root,
            facts.git_dir,
            facts.common_dir,
            facts.branch,
            "a" * 40,
            facts.repository,
        )
        with self.assertRaises(identity.AdminIdentityError):
            identity.verify_cutover_binding(
                forged,
                issue=self.issue_number,
                pr=self.pr_number,
                expected_base=BASE_BRANCH,
                task_binding=lambda *_args: True,
            )

    def test_expected_base_is_required_and_gh_api_rejects_graphql_and_mutations(self) -> None:
        with self.assertRaises(TypeError):
            identity.verify_cutover_binding(  # type: ignore[call-arg]
                self.observe(),
                issue=self.issue_number,
                pr=self.pr_number,
                task_binding=lambda *_args: True,
            )

        response = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(identity, "_run", return_value=response) as runner:
            self.assertEqual(identity._gh_api(self.root, ["api", "repos/example-owner/example-repo/issues/141"]), {})
        self.assertEqual(
            runner.call_args.args[0],
            ["gh", "api", "--hostname", "github.com", "repos/example-owner/example-repo/issues/141"],
        )
        with mock.patch.object(identity, "_run") as runner:
            with self.assertRaises(identity.AdminIdentityError):
                identity._gh_api(self.root, ["api", "repos/example-owner/example-repo/issues/141", "-X", "PATCH"])
        runner.assert_not_called()

        with mock.patch.object(identity, "_run") as runner:
            with self.assertRaisesRegex(identity.AdminIdentityError, "exact repository API reads"):
                identity._gh_api(self.root, ["api", "graphql"])
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
