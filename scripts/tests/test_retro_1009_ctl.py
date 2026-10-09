"""Retro 2026-10-09 (control plane, part B): regression tests with positive controls."""
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _git, _open_design, _review, _v2_tree,  # noqa: F401
                                       repo)


def _local(repo, branch):
    return bool(_git("branch", "--list", branch, cwd=repo).strip())


def _on_origin(repo, branch):
    return bool(_git("ls-remote", "--heads", "origin", branch, cwd=repo).strip())


# --- 1. skip-gate / waive-gate clean branches up from the repo root ------------------

def _phase_task_with_dev_worktree(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "in-progress"})
    _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    gh._run = s._default_runner
    wt = s.cmd_worktree_add(gh, 10, repo_path=str(repo))["path"]
    _review(gh, 10, "arch-review", confidence=99)
    return gh, wt


def test_skip_gate_given_the_dev_worktree_still_cleans_the_branches(repo):
    # The orchestrator passes the phase-Task's own worktree as --repo-path; it is released
    # first, so branch cleanup must not run `git -C <removed worktree>`.
    gh, wt = _phase_task_with_dev_worktree(repo)

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=wt)

    assert result["phase_task_complete"] is True
    assert not Path(wt).exists()
    assert "warnings" not in result, result.get("warnings")
    assert "error" not in result["cleanup"]["local_branch"], result["cleanup"]
    assert not _local(repo, "issue-10")


def test_skip_gate_given_the_repo_root_cleans_the_branches(repo):
    # Positive control: the repo root as --repo-path always worked.
    gh, wt = _phase_task_with_dev_worktree(repo)

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=str(repo))

    assert result["phase_task_complete"] is True
    assert "warnings" not in result
    assert not _local(repo, "issue-10")


# --- 2. transition --expect-stage arch-review surfaces the skip bar at top level ------

def _author_doc(gh, repo, issue, path, text="# design\n"):
    gh._run = s._default_runner
    wt = s.cmd_worktree_add(gh, issue, repo_path=str(repo))["path"]
    target = Path(wt) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=wt)
    _git("commit", "-qm", f"add {path}", cwd=wt)
    _git("push", "-q", "origin", f"issue-{issue}", cwd=wt)
    return wt


def _initiative_tree(*extra):
    return FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:cloud"]},
                   {"number": 9, "labels": ["type:epic"], "parent": 6}, *extra])


def test_transition_into_arch_review_returns_the_epic_profile_skip_threshold(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "profiles", [{"name": "default", "match": "*",
                                                  "gates": {"skipConfidenceThreshold": 85}}])
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [
        {"name": "cloud", "match": {"label": "initiative:cloud"}, "placement": "cloud"}])
    gh = _initiative_tree({"number": 10, "labels": ["type:task"], "parent": 9,
                           "stage": "architecture"})
    wt = _author_doc(gh, repo, 10, f"{DOC}/epic-9/architecture.md")

    result = s.cmd_transition(gh, 10, "arch-review", repo_path=wt)

    assert result["ready"] is True
    assert result["skip_confidence_threshold"] == 85


def test_transition_into_lld_review_has_no_skip_threshold(repo):
    # Positive control: only arch-review carries the bar.
    gh = _initiative_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    wt = _author_doc(gh, repo, 11, f"{DOC}/epic-9/lld.md")

    result = s.cmd_transition(gh, 11, "lld-review", repo_path=wt)

    assert result["ready"] is True
    assert "skip_confidence_threshold" not in result


# --- 3. a plain child of a branch:true Initiative integrates into initiative-<i> ------

_BRANCH_PROFILE = {"name": "tijori", "match": {"label": "initiative:branch"}, "branch": True}


def _branch_tree(*extra, labels=("type:initiative", "initiative:branch")):
    return {i["number"]: i for i in FakeGh([
        {"number": 6, "labels": list(labels), "title": "Tijori"}, *extra]).issue_list()}


def test_a_plain_initiative_child_integrates_into_the_initiative_branch(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_BRANCH_PROFILE])
    issues = _branch_tree({"number": 20, "labels": ["type:bug"], "parent": 6,
                           "title": "tijori-api: crash on empty ledger"})

    assert s.integration_base_in(issues, 20) == "initiative-6"


def test_the_roadmap_task_and_unopted_initiatives_still_integrate_into_main(monkeypatch):
    # Positive controls: the Product-Roadmap Task, and any child of an Initiative without
    # `branch: true`, keep `main`.
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_BRANCH_PROFILE])
    issues = _branch_tree({"number": 7, "labels": ["type:task"], "parent": 6,
                           "title": "Product Roadmap"})
    assert s.integration_base_in(issues, 7) == "main"
    plain = _branch_tree({"number": 20, "labels": ["type:bug"], "parent": 6, "title": "bug"},
                         labels=("type:initiative",))
    assert s.integration_base_in(plain, 20) == "main"


# --- 9. a sync-conflict comment is posted once per standing conflict -----------------

def _sync_conflicts(gh, branch="issue-30"):
    return sum(f"sync-conflict: {branch}" in c for c in gh.comments_on(30))


def _conflicted_task(repo):
    from tests.test_v2_phase_tasks import _advance_origin
    gh = FakeGh([{"number": 30, "labels": ["type:task"], "stage": "development"}])
    gh._run = s._default_runner
    _advance_origin(repo, "main", "shared.txt", "base\n")
    _git("push", "-q", "origin", "origin/main:refs/heads/issue-30", cwd=repo)
    _advance_origin(repo, "issue-30", "shared.txt", "task side\n")
    _advance_origin(repo, "main", "shared.txt", "main side\n")
    return gh, _advance_origin


def test_a_conflict_is_not_reposted_when_both_heads_move_on(repo):
    gh, advance = _conflicted_task(repo)
    first = s.cmd_sync_branch(gh, str(repo), 30)
    advance(repo, "main", "other.txt", "unrelated\n")       # main moves (the PR 2581 case)
    advance(repo, "issue-30", "mine.txt", "more work\n")    # and so does the branch

    second = s.cmd_sync_branch(gh, str(repo), 30)

    assert first["conflict"] and second["conflict"] and second["already_posted"] is True
    assert _sync_conflicts(gh) == 1


def test_a_new_conflict_after_a_resolution_is_posted(repo):
    # Positive control: once resolved (the branch merged its base), a later conflict on the
    # same file is a new one.
    gh, advance = _conflicted_task(repo)
    s.cmd_sync_branch(gh, str(repo), 30)
    _git("fetch", "-q", "origin", cwd=repo)
    _git("checkout", "-q", "-B", "fix", "origin/issue-30", cwd=repo)
    _git("merge", "-q", "-X", "ours", "origin/main", "-m", "resolve", cwd=repo)
    _git("push", "-q", "origin", "fix:issue-30", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    advance(repo, "issue-30", "shared.txt", "task side 2\n")
    advance(repo, "main", "shared.txt", "main side 2\n")

    again = s.cmd_sync_branch(gh, str(repo), 30)

    assert again["conflict"] and "already_posted" not in again
    assert _sync_conflicts(gh) == 2


# --- 4/10. close-epic readies the epic worktree for the exploratory pass ---------------

def _git_backed(gh, repo):
    """GitHub-side compares answered from the real origin."""
    gh.repo, gh._run = str(repo), s._default_runner
    gh.pr_list_for_branch = lambda branch, state="open": []

    def behind(head, base="main"):
        _git("fetch", "-q", "origin", cwd=repo)
        return int(_git("rev-list", "--count", f"origin/{head}..origin/{base}", cwd=repo))
    gh.branch_behind_by = behind
    gh.base_delta_files = lambda head, base="main": []
    return gh


def _epic_closing(repo, gh):
    """Epic 9's children are closed, no verification yet; its live worktree is at origin."""
    _git_backed(gh, repo)
    wt = s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))["path"]
    return wt


def _head(path, ref="HEAD"):
    return _git("rev-parse", ref, cwd=path).strip()


def test_close_epic_fast_forwards_a_stale_epic_worktree(repo):
    from tests.test_v2_phase_tasks import _advance_origin
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    wt = _epic_closing(repo, gh)
    _advance_origin(repo, "epic-9", "task.txt", "a Task merged on GitHub\n")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert _head(wt) == _head(repo, "origin/epic-9")
    assert result["merged"] is False and result["base"] == "main"
    assert result["epic_worktree"]["behind_before"] == 1
    assert "prepare" not in result          # nothing configured


def test_close_epic_runs_the_configured_prepare_in_the_epic_worktree(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE["epicClose"], "prepare", "echo installed > prepared.txt")
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    wt = _epic_closing(repo, gh)

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert result["prepare"]["ok"] is True and result["prepare"]["ran"] is True
    assert (Path(wt) / "prepared.txt").read_text() == "installed\n"
    assert result["epic_worktree"]["behind_before"] == 0


def test_close_epic_reports_prepare_without_an_epic_worktree(repo, monkeypatch):
    # Positive control: no live worktree -- nothing is fast-forwarded and prepare does not run.
    monkeypatch.setitem(s.PIPELINE["epicClose"], "prepare", "false")
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    _git_backed(gh, repo)
    _git("push", "-q", "origin", "origin/main:refs/heads/epic-9", cwd=repo)

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert "epic_worktree" not in result
    assert result["prepare"]["ran"] is False and result["prepare"]["ok"] is False


def test_close_epic_without_a_worktree_or_prepare_adds_nothing(repo):
    # Positive control: no live worktree, no `prepare` configured -- the result is unchanged.
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    _git_backed(gh, repo)
    _git("push", "-q", "origin", "origin/main:refs/heads/epic-9", cwd=repo)

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert result["merged"] is False and result["missing_verification"]
    assert "epic_worktree" not in result and "prepare" not in result


# --- 5. close-epic into initiative-<i> first merges main into it ----------------------

_OPT_IN = {"name": "tijori", "match": {"label": "initiative:branch"}, "branch": True}


def _initiative_epic(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_OPT_IN])
    gh = FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:branch"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6},
                 {"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"}])
    _git_backed(gh, repo)
    _git("push", "-q", "origin", "origin/main:refs/heads/initiative-6", cwd=repo)
    _git("push", "-q", "origin", "origin/main:refs/heads/epic-9", cwd=repo)
    return gh


def test_close_epic_into_an_initiative_branch_first_syncs_it_with_main(repo, monkeypatch):
    from tests.test_v2_phase_tasks import _advance_origin, _origin_file
    gh = _initiative_epic(repo, monkeypatch)
    _advance_origin(repo, "main", "main-fix.txt", "fix\n")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert result["base"] == "initiative-6" and result["merged"] is False
    assert result["initiative_sync"]["synced"] is True
    assert _origin_file(repo, "initiative-6", "main-fix.txt") == "fix\n"


def test_close_epic_refuses_when_main_conflicts_with_the_initiative_branch(repo, monkeypatch):
    from tests.test_v2_phase_tasks import _advance_origin
    gh = _initiative_epic(repo, monkeypatch)
    _advance_origin(repo, "initiative-6", "shared.txt", "initiative side\n")
    _advance_origin(repo, "main", "shared.txt", "main side\n")
    before = _head(repo, "origin/initiative-6")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert result["merged"] is False and result["initiative_sync"]["conflict"] is True
    assert "shared.txt" in result["reason"] and "reconciled" not in result
    _git("fetch", "-q", "origin", cwd=repo)
    assert _head(repo, "origin/initiative-6") == before


def test_close_epic_into_main_never_syncs_an_initiative(repo):
    # Positive control: an Epic closing into main has no initiative branch to sync.
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    _epic_closing(repo, gh)
    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))
    assert result["merged"] is False and "initiative_sync" not in result


# --- 6. pr-checks honours the Initiative profile's deferSuites like close-epic ---------

_DEFER = {"name": "tijori", "match": {"label": "initiative:branch"}, "branch": True,
          "deferSuites": ["backend"]}


def _epic_pr(head, base):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:branch"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6}])
    gh.pr_checks = lambda n: []
    gh.pr_files = lambda n: ["backend/app.py"]
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc1234",
                                       "baseRefName": base, "headRefName": head}
    return gh


def test_pr_checks_does_not_list_a_deferred_suite_on_an_epic_to_initiative_pr(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_DEFER])
    result = s.cmd_pr_checks(_epic_pr("epic-9", "initiative-6"), 38)
    assert result["missing_required_workflows"] == []
    assert result["status"] == "passed"
    assert result["deferred_to_initiative"] == ["backend"]


def test_pr_checks_still_demands_the_suite_on_a_pr_into_main(monkeypatch):
    # Positive control: deferral needs epic -> initiative; into main the suite is owed.
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_DEFER])
    result = s.cmd_pr_checks(_epic_pr("epic-9", "main"), 38)
    assert result["missing_required_workflows"] and result["status"] == "missing-checks"
    assert "deferred_to_initiative" not in result
