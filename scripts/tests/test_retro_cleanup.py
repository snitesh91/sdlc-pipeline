"""Resource cleanup once a unit is terminal: worktrees (dev, review, ephemeral), origin and
local branches, run-state files, agent scratch. Real git against a bare origin; only
GitHub is faked."""
import json
import os
import time
from pathlib import Path

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh, _git, _v2_tree, _worktree_with_doc, repo  # noqa: F401


def _task(number=5, **kw):
    """A parentless Task (integrates into main), GitHub faked, git real."""
    return _v2_tree({"number": number, "labels": ["type:task"], "stage": "pr-review", **kw})


def _real(gh, repo):
    gh.repo = str(repo)            # delete_branch really deletes on origin
    gh._run = s._default_runner    # merge-pr's git calls go through the provider's runner
    return gh


def _merged_pr(gh, number=5, pr=70):
    gh.prs[pr] = {"headRefName": f"issue-{number}", "baseRefName": "main", "state": "OPEN",
                  "body": "", "comments": []}
    gh.pr_merge(pr)
    return pr


def _on_origin(repo, branch):
    return bool(_git("ls-remote", "--heads", "origin", branch, cwd=repo).strip())


def _local(repo, branch):
    return bool(_git("branch", "--list", branch, cwd=repo).strip())


# --- merge-pr: the merged branch leaves origin and the local clone -------------

def test_merge_pr_deletes_the_merged_branch_on_origin_and_locally(repo):
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    pr = _merged_pr(gh)

    result = s.cmd_merge_pr(gh, pr, 5, repo_path=str(repo))

    # gh's `--delete-branch` used to leave these behind whenever a worktree held the branch.
    assert not _on_origin(repo, "issue-5") and not _local(repo, "issue-5")
    assert not Path(wt).exists()
    assert result["worktree"]["released"] is True
    assert result["cleanup"]["remote_branch"]["deleted"] is True
    assert result["cleanup"]["local_branch"] == {"deleted": True}
    assert gh.merges == [(pr, False)]


def test_merge_pr_keeps_a_branch_that_gained_commits_after_the_merge(repo):
    # Positive control: work pushed after the merged head is on no merged PR -- kept on origin.
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    pr = _merged_pr(gh)
    (Path(wt) / "src" / "b.txt").write_text("late\n")
    _git("add", "src/b.txt", cwd=wt)
    _git("commit", "-qm", "late", cwd=wt)
    _git("push", "-q", "origin", "issue-5", cwd=wt)

    result = s.cmd_merge_pr(gh, pr, 5, repo_path=str(repo))

    assert _on_origin(repo, "issue-5")
    assert result["cleanup"]["remote_branch"]["deleted"] is False
    assert "unmerged" in result["cleanup"]["remote_branch"]["reason"]


# --- release_worktree once origin/<branch> is gone ----------------------------

def test_close_issue_releases_the_worktree_after_origin_deleted_the_branch(repo):
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _merged_pr(gh)
    # GitHub's auto-delete removed the head branch and a later fetch pruned the tracking ref.
    _git("push", "-q", "origin", "--delete", "issue-5", cwd=repo)
    _git("fetch", "-q", "--prune", "origin", cwd=repo)

    result = s.cmd_close_issue(gh, 5, repo_path=str(repo))

    assert result["worktree"]["released"] is True, result["worktree"]
    assert not Path(wt).exists() and not _local(repo, "issue-5")


def test_a_gone_tracking_ref_never_releases_unmerged_local_commits(repo):
    # Positive control: a commit on no merged PR head or base still blocks the release.
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _merged_pr(gh)
    _git("push", "-q", "origin", "--delete", "issue-5", cwd=repo)
    _git("fetch", "-q", "--prune", "origin", cwd=repo)
    (Path(wt) / "src" / "c.txt").write_text("wip\n")
    _git("add", "src/c.txt", cwd=wt)
    _git("commit", "-qm", "wip", cwd=wt)

    result = s.cmd_close_issue(gh, 5, repo_path=str(repo))

    assert result["worktree"]["released"] is False
    assert "origin/issue-5 is gone" in result["worktree"]["reason"]
    assert Path(wt).exists() and _local(repo, "issue-5")
    assert result["cleanup"]["local_branch"]["retained_local_branch"] is True


# --- close-issue --not-planned never deletes unmerged work ----------------------

def test_close_issue_not_planned_keeps_an_unmerged_branch_on_origin(repo):
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    result = s.cmd_close_issue(gh, 5, repo_path=str(repo), not_planned=True, reason="dropped")

    assert _on_origin(repo, "issue-5")
    assert "unmerged" in result["cleanup"]["remote_branch"]["reason"]
    # The worktree and local ref go: every commit is still on the kept origin branch.
    assert result["worktree"]["released"] is True and not Path(wt).exists()
    assert result["cleanup"]["local_branch"] == {"deleted": True}


# --- review worktree ------------------------------------------------------------

def test_review_worktree_add_then_release(repo):
    gh = _real(_task(), repo)
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    made = s.cmd_review_worktree_add(5, str(repo))

    path = Path(made["path"])
    assert path == Path(s.review_worktree_path(5)) and made["reused"] is False
    assert _git("branch", "--show-current", cwd=path).strip() == ""       # detached
    assert made["head"] == _git("rev-parse", "origin/issue-5", cwd=repo).strip()
    (Path(made["scratch"]) / "review.log").write_text("x\n")
    assert _git("status", "--porcelain", cwd=path) == ""                  # scratch is excluded
    assert s.cmd_review_worktree_add(5, str(repo))["reused"] is True
    exclude = Path(_git("rev-parse", "--git-common-dir", cwd=repo).strip())
    exclude = (exclude if exclude.is_absolute() else repo / exclude) / "info" / "exclude"
    assert exclude.read_text().splitlines().count(".sdlc-scratch/") == 1

    released = s.cmd_release_review_worktree(5, str(repo))

    assert released == {"released": True, "path": str(path)} and not path.exists()
    assert s.cmd_release_review_worktree(5, str(repo))["reason"] == "no review worktree"


def test_merge_pr_also_removes_the_units_review_worktree(repo):
    gh = _real(_task(), repo)
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    review = s.cmd_review_worktree_add(5, str(repo))["path"]
    pr = _merged_pr(gh)

    result = s.cmd_merge_pr(gh, pr, 5, repo_path=str(repo))

    assert result["cleanup"]["review_worktree"]["released"] is True
    assert not Path(review).exists()


def test_worktree_add_makes_an_excluded_scratch_dir_that_goes_with_the_tree(repo):
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    resumed = s.cmd_worktree_add(gh, 5, repo_path=str(repo))
    scratch = Path(resumed["scratch"])
    assert scratch == Path(wt) / ".sdlc-scratch" and scratch.is_dir()
    (scratch / "unit.log").write_text("log\n")

    assert s.release_worktree("issue-5", base_repo=str(repo))["released"] is True
    assert not scratch.exists()


# --- prune-stale ------------------------------------------------------------------

def test_prune_stale_cleans_closed_units_and_leaves_open_and_foreign_ones(repo):
    gh = _real(_task(5), repo)
    gh.issues[7] = {**gh.issues[5], "title": "issue 7", "comments": []}
    wt5 = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    review5 = s.cmd_review_worktree_add(5, str(repo))["path"]
    _merged_pr(gh)
    gh.issues[5]["state"] = "CLOSED"
    wt7 = _worktree_with_doc(gh, repo, 7, "src/b.txt", "b\n")
    _git("branch", "feature-x", cwd=repo)

    dry = s.cmd_prune_stale(gh, str(repo), dry_run=True)

    assert Path(wt5).exists() and Path(review5).exists() and _on_origin(repo, "issue-5")
    [unit] = dry["units"]
    assert unit["issue"] == 5 and unit["remote_branch"]["would_delete"] is True
    assert dry["skipped_open"] == [7]

    result = s.cmd_prune_stale(gh, str(repo))

    assert not Path(wt5).exists() and not Path(review5).exists()
    assert not _on_origin(repo, "issue-5") and not _local(repo, "issue-5")
    assert Path(wt7).exists() and _on_origin(repo, "issue-7") and _local(repo, "issue-7")
    assert _local(repo, "feature-x")
    assert result["worktree_pruned"] is True and result["fetch_pruned"] is True


def test_prune_stale_removes_a_closed_units_detached_dev_worktree(repo):
    # A dev-path tree checked out away from `issue-<n>` (detached) is invisible to
    # `release_worktree`, which finds trees by branch -- it used to survive every sweep.
    gh = _real(_task(5), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _git("checkout", "--detach", cwd=wt)
    _merged_pr(gh)
    gh.issues[5]["state"] = "CLOSED"

    dry = s.cmd_prune_stale(gh, str(repo), dry_run=True)

    assert Path(wt).exists()
    assert dry["units"][0]["detached_worktree"]["would_release"] is True

    result = s.cmd_prune_stale(gh, str(repo))

    assert not Path(wt).exists()
    assert result["units"][0]["detached_worktree"]["released"] is True
    assert not _on_origin(repo, "issue-5") and not _local(repo, "issue-5")


def test_prune_stale_keeps_a_detached_dev_worktree_with_unlanded_commits(repo):
    # Positive control: a detached HEAD carrying a commit nothing else holds is kept.
    gh = _real(_task(5), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _git("checkout", "--detach", cwd=wt)
    Path(wt, "src", "extra.txt").write_text("extra\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "unlanded", cwd=wt)
    _merged_pr(gh)
    gh.issues[5]["state"] = "CLOSED"

    result = s.cmd_prune_stale(gh, str(repo))

    assert Path(wt).exists()
    detached = result["units"][0]["detached_worktree"]
    assert detached["released"] is False and "commits" in detached["reason"]


# --- close-epic: epic ref, children, run state, stack ------------------------------

def test_close_epic_sweeps_the_epic_and_its_childrens_branches(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    gh = _real(_v2_tree({"number": 10, "labels": ["type:task"], "parent": 9,
                         "state": "CLOSED"}), repo)
    for branch in ("epic-9", "issue-10"):
        _git("push", "-q", "origin", f"main:refs/heads/{branch}", cwd=repo)
        _git("branch", branch, "origin/main", cwd=repo)
    s._write_run_state(9, {"run_id": "r1", "terminal": [10]})
    gh.issues[9]["comments"] = [
        "<!-- epic-verification: e2e:9 sha:abc1234 @ 2026-09-15T00:00:00Z -->",
        "<!-- epic-verification: exploratory:9 sha:abc1234 @ 2026-09-15T00:01:00Z -->"]
    gh.files_since = lambda sha, branch: []
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.pr_list_for_branch = lambda branch: []
    gh.pr_create = lambda **kw: 38
    gh.pr_checks = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_files = lambda n: []
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: None

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), runner=s._default_runner)

    assert result["merged"] is True
    for branch in ("epic-9", "issue-10"):
        assert not _on_origin(repo, branch) and not _local(repo, branch), branch
    assert [u["issue"] for u in result["children_cleanup"]] == [10]
    assert result["run_state"]["archived"] is True
    assert result["stack"]["torn_down"] is False and "enabled is false" in result["stack"]["reason"]


# --- run-state files ----------------------------------------------------------------

def test_archiving_a_closed_epics_run_state_keeps_the_initiative_run_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    s._write_run_state(9, {"run_id": "r1", "terminal": [10, 11]})
    s._write_run_state(12, {"run_id": "r1", "terminal": [13]})

    assert s.archive_run_state(9)["archived"] is True

    assert json.loads((tmp_path / "epic-9.json").read_text())["closed"] is True
    assert sorted(s.run_completed(s.read_run_state(12))) == [10, 11, 13]


def test_prune_removes_only_stale_run_states_of_closed_epics(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    old = time.time() - 3 * 24 * 3600
    for epic, run_id, stale in ((9, "gone", True), (12, "live", True), (14, "live", False),
                                (15, "gone2", True)):
        s._write_run_state(epic, {"run_id": run_id, "terminal": []})
        if stale:
            os.utime(tmp_path / f"epic-{epic}.json", (old, old))

    removed = s._stale_run_states({9, 12, 14}, dry_run=False)

    # 12 shares a fresh run's id, 14 is fresh, 15's epic is still open.
    assert removed == [str(tmp_path / "epic-9.json")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["epic-12.json", "epic-14.json",
                                                         "epic-15.json"]
