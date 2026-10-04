"""Opt-in initiative branch (`pipeline.initiativeProfiles`): an opted-in Initiative's Epics cut
from, sync with and close into `initiative-<i>`, which reaches `main` once through
`open-initiative-pr` / `merge-initiative-pr`; `testTasks: false` drops the standing test Tasks.
Every behaviour has a positive control: a repo or Initiative without the opt-in is unchanged."""
import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _advance_origin, _git, _origin_file,
                                       _push_doc_branch, repo)  # noqa: F401

OPT_IN = "initiative:branch"
PROFILE = {"name": "tijori", "match": {"label": OPT_IN}, "branch": True, "testTasks": False,
           "deferSuites": ["backend"], "closeSuites": ["e2e"]}


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [PROFILE])


def _tree(*extra, opt_in=True, epic_state="OPEN"):
    labels = ["type:initiative"] + ([OPT_IN] if opt_in else [])
    return FakeGh([{"number": 6, "labels": labels, "title": "Tijori"},
                   {"number": 9, "labels": ["type:epic"], "parent": 6, "state": epic_state},
                   *extra])


def _exists_on_origin(repo, branch):
    return bool(_git("ls-remote", "--heads", "origin", branch, cwd=repo).strip())


class NoLookups(FakeGh):
    """A provider that must not be asked for the issue list."""

    def issue_list(self):
        raise AssertionError("issue_list called without initiativeProfiles configured")


# --- profile resolution -----------------------------------------------------------------

def test_an_initiative_without_the_label_keeps_the_defaults(opted_in):
    gh = _tree(opt_in=False)
    prof = s.resolve_initiative_profile(gh.issue_list()[0])
    assert prof == {"branch": False, "testTasks": True, "deferSuites": [], "closeSuites": [],
                    "name": None}
    assert s.resolve_initiative_profile(_tree().issue_list()[0])["branch"] is True


def test_initiative_profile_reads_up_the_ancestry(opted_in):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9})
    out = s.cmd_initiative_profile(gh, 12)
    assert out["initiative"] == 6 and out["opted_in"] is True and out["testTasks"] is False
    assert out["initiative_branch"] == "initiative-6"


def test_initiative_profile_looks_nothing_up_without_the_config():
    out = s.cmd_initiative_profile(NoLookups([]), 12)
    assert out["opted_in"] is False and out["testTasks"] is True and out["branch"] is False


# --- base detection ---------------------------------------------------------------------

def test_an_opted_in_epic_integrates_into_the_initiative_branch(opted_in):
    gh = _tree()
    assert s.integration_base(gh, 9, "epic") == "initiative-6"
    assert s.integration_base(gh, 6, "initiative") == "main"
    # its children still base on the epic branch
    gh.issues[12] = {**gh.issues[9], "number": 12, "labels": ["type:task"], "parent": 9}
    assert s.integration_base(gh, 12) == "epic-9"


def test_an_epic_of_an_initiative_without_the_opt_in_integrates_into_main(opted_in):
    assert s.integration_base(_tree(opt_in=False), 9, "epic") == "main"


def test_without_initiative_profiles_the_epic_base_is_main_with_no_lookup():
    """Positive control: the v0.3.16 path makes no extra GitHub call."""
    assert s.integration_base(NoLookups([]), 9, "epic") == "main"


def test_worktree_add_cuts_the_initiative_branch_from_main_then_the_epic_from_it(repo, opted_in):
    gh = _tree()

    result = s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))

    assert result["base"] == "origin/initiative-6"
    assert _exists_on_origin(repo, "initiative-6") and _exists_on_origin(repo, "epic-9")
    _advance_origin(repo, "initiative-6", "earlier-epic.txt", "landed by epic 8\n")
    gh.issues[10] = {**gh.issues[9], "number": 10}
    s.cmd_worktree_add(gh, 10, unit="epic", repo_path=str(repo))
    assert _origin_file(repo, "epic-10", "earlier-epic.txt") == "landed by epic 8\n"


def test_cut_phase_tasks_stands_up_the_initiative_branch_for_the_first_epic(repo, opted_in):
    gh = _tree()

    result = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert result["ok"] is True
    assert result["steps"]["epic-worktree"]["base"] == "origin/initiative-6"
    assert _exists_on_origin(repo, "initiative-6")


def test_cut_phase_tasks_without_the_opt_in_cuts_from_main(repo, opted_in):
    gh = _tree(opt_in=False)

    result = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert result["steps"]["epic-worktree"]["base"] == "origin/main"
    assert not _exists_on_origin(repo, "initiative-6")


# --- keeping the branches current -------------------------------------------------------

def test_next_action_sync_on_the_initiative_merges_main_into_its_branch(repo, opted_in):
    gh = _tree()
    s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))
    _advance_origin(repo, "main", "hotfix.txt", "on main\n")

    first = s.sync_epic_if_due(gh, 6, "run-1", repo_path=str(repo))

    assert first["synced"] is True and first["due"] == "run-start" and first["unit"] == "initiative"
    assert _origin_file(repo, "initiative-6", "hotfix.txt") == "on main\n"
    assert s.sync_epic_if_due(gh, 6, "run-1", repo_path=str(repo))["due"] is None
    _advance_origin(repo, "main", "second.txt", "again\n")
    assert s.sync_epic_if_due(gh, 6, "run-1", repo_path=str(repo))["due"] == "main-moved"


def test_initiative_sync_is_skipped_without_the_opt_in(repo, opted_in):
    result = s.sync_epic_if_due(_tree(opt_in=False), 6, "run-1", repo_path=str(repo))
    assert "no initiative branch" in result["skipped"]


def test_initiative_sync_reports_a_conflict(repo, opted_in):
    gh = _tree()
    s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))
    _advance_origin(repo, "initiative-6", "README.md", "initiative\n")
    _advance_origin(repo, "main", "README.md", "main\n")

    result = s.sync_epic_if_due(gh, 6, "run-1", repo_path=str(repo))

    assert result["conflict"] is True and result["conflicting_files"] == ["README.md"]
    assert "--unit initiative" in gh.issues[6]["comments"][-1]
    assert s.sync_epic_if_due(gh, 6, "run-1", repo_path=str(repo))["pending"] is True


def test_epic_sync_merges_the_initiative_branch_not_main(repo, opted_in):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9})
    s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))
    _advance_origin(repo, "main", "main-only.txt", "x\n")
    _advance_origin(repo, "initiative-6", "sibling-epic.txt", "y\n")

    result = s.sync_epic_if_due(gh, 9, "run-1", repo_path=str(repo))

    assert result["synced"] is True and result["base"] == "initiative-6"
    assert _origin_file(repo, "epic-9", "sibling-epic.txt") == "y\n"
    with pytest.raises(Exception):
        _origin_file(repo, "epic-9", "main-only.txt")


# --- close-epic --------------------------------------------------------------------------

def _closing(gh, files=(), checks=()):
    gh.issues[9]["comments"] = [
        "<!-- epic-verification: exploratory:9 sha:abc1234 @ 2026-09-15T00:01:00Z -->"]
    calls = {"merged": [], "created": [], "behind_base": []}
    gh.files_since = lambda sha, branch: []
    gh.branch_behind_by = lambda branch, base="main": calls["behind_base"].append(base) or 0
    gh.pr_list_for_branch = lambda branch, state="open": []
    gh.pr_create = lambda **kw: calls["created"].append(kw) or 38
    gh.pr_checks = lambda n: list(checks)
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_files = lambda n: list(files)
    gh.branch_commits = lambda base, head: []
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: calls["merged"].append(n)
    return calls


def test_close_epic_merges_an_opted_in_epic_into_the_initiative_branch(opted_in):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    calls = _closing(gh)

    result = s.cmd_close_epic(gh, 9, runner=lambda argv: "")

    assert result["merged"] is True and result["base"] == "initiative-6"
    assert calls["created"][0]["base"] == "initiative-6" and calls["behind_base"] == ["initiative-6"]
    assert gh.issues[9]["state"] == "CLOSED" and gh.issues[9]["status"] == "done"


def test_close_epic_without_the_opt_in_still_merges_into_main(opted_in):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
               opt_in=False)
    calls = _closing(gh)

    result = s.cmd_close_epic(gh, 9, runner=lambda argv: "")

    assert result["merged"] is True and "base" not in result
    assert calls["created"][0]["base"] == "main"


def test_close_epic_into_the_initiative_branch_does_not_demand_deferred_suites(opted_in):
    """`backend` is deferred: its missing check does not hold the merge into initiative-6;
    `frontend` is not, so it still does."""
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    _closing(gh, files=["backend/app.py"])
    result = s.cmd_close_epic(gh, 9, runner=lambda argv: "")
    assert result["merged"] is True
    assert result["evidence"]["suites"]["backend"] == "deferred_to_initiative"
    assert result["unattested_suites"] == [] and result["awaiting_checks"] == []

    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    _closing(gh, files=["backend/app.py", "frontend/app.js"])
    held = s.cmd_close_epic(gh, 9, runner=lambda argv: "")
    assert held["merged"] is False and held["unattested_suites"] == ["frontend"]


def test_close_epic_into_main_never_defers_a_suite(opted_in):
    """Positive control: deferral needs the initiative branch; into main the suite is owed."""
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
               opt_in=False)
    _closing(gh, files=["backend/app.py"])
    result = s.cmd_close_epic(gh, 9, runner=lambda argv: "")
    assert result["merged"] is False and result["unattested_suites"] == ["backend"]


# --- lld without the standing test Tasks ------------------------------------------------

_LLD_WITH_STANDING = (
    "# lld for epic 9\n\n"
    "## Task greet-endpoint: Add /greet endpoint\n\n## Footprint\n- `src/greet.js`\n\n"
    "## Task it: Integration-test\nDepends on: greet-endpoint\n\n## Footprint\n"
    "**Verify-only:**\n- `test/**`\n\n"
    "## Task e2e: e2e-test\nDepends on: greet-endpoint\n\n## Footprint\n- `e2e/greet.spec.ts`\n")


def test_create_lld_tasks_skips_standing_test_tasks_when_they_are_off(repo, opted_in):
    gh = _tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _LLD_WITH_STANDING)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["greet-endpoint"]
    assert {k["key"] for k in result["skipped_sections"]} == {"it", "e2e"}
    assert all("testTasks: false" in k["reason"] for k in result["skipped_sections"])


def test_create_lld_tasks_creates_the_standing_tasks_by_default(repo, opted_in):
    """Positive control: an Initiative without the opt-in still gets both standing Tasks."""
    gh = _tree(opt_in=False)
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _LLD_WITH_STANDING)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["greet-endpoint", "it", "e2e"]


@pytest.mark.parametrize("title,standing", [
    ("Integration-test", True), ("e2e-test", True), ("E2E tests", True),
    ("Integration test Task", True), ("standing e2e-test", True),
    ("Integration test harness for payments", False), ("Add e2e fixtures", False)])
def test_standing_test_task_titles(title, standing):
    assert bool(s._STANDING_TEST_TASK_TITLE.match(title)) is standing


# --- closing the Initiative --------------------------------------------------------------

def _all_closed(opt_in=True, prs=None):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"] + ([OPT_IN] if opt_in else []),
                  "title": "Tijori"},
                 {"number": 9, "labels": ["type:epic"], "parent": 6, "state": "CLOSED"}],
                prs=prs)
    gh.branch_head_sha = lambda branch: "abcd0000"
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.pr_ready = lambda n: None
    return gh


def test_check_initiative_closeable_refuses_until_the_initiative_branch_lands(opted_in):
    gh = _all_closed()
    result = s.cmd_check_initiative_closeable(gh, 6)
    assert result["closeable"] is False and "open-initiative-pr 6" in result["reason"]
    assert s.cmd_close_initiative(gh, 6)["closed"] is False

    gh.prs[300] = {"headRefName": "initiative-6", "baseRefName": "main", "state": "MERGED",
                   "headRefOid": "abcd0000"}
    assert s.cmd_check_initiative_closeable(gh, 6)["closeable"] is True


def test_a_later_commit_on_the_initiative_branch_unlands_it(opted_in):
    gh = _all_closed(prs={300: {"headRefName": "initiative-6", "state": "MERGED",
                                "headRefOid": "0123abcd"}})
    result = s.cmd_check_initiative_closeable(gh, 6)
    assert result["closeable"] is False
    assert result["initiative_branch"]["unlanded_head"] == "abcd0000"


def test_check_initiative_closeable_without_the_opt_in_needs_no_branch(opted_in):
    assert s.cmd_check_initiative_closeable(_all_closed(opt_in=False), 6)["closeable"] is True


def test_next_action_on_the_initiative_names_the_initiative_pr_step(opted_in):
    result = s.decide_next_action(_all_closed(), 6)
    assert result["action"] == "none" and "open-initiative-pr 6" in result["reason"]
    plain = s.decide_next_action(_all_closed(opt_in=False), 6)
    assert "ready for initiative-close" in plain["reason"]


def test_open_initiative_pr_syncs_then_opens_once(repo, opted_in):
    gh = _all_closed()
    gh.repo = str(repo)
    gh.branch_head_sha = lambda branch: "x"
    _git("push", "-q", "origin", "origin/main:refs/heads/initiative-6", cwd=repo)
    _advance_origin(repo, "main", "hotfix.txt", "main moved\n")

    first = s.cmd_open_initiative_pr(gh, 6, repo_path=str(repo))

    assert first["opened"] is True and first["created"] is True
    assert gh.prs[first["pr"]]["baseRefName"] == "main"
    assert gh.prs[first["pr"]]["headRefName"] == "initiative-6"
    assert _origin_file(repo, "initiative-6", "hotfix.txt") == "main moved\n"
    again = s.cmd_open_initiative_pr(gh, 6, repo_path=str(repo))
    assert again["created"] is False and again["pr"] == first["pr"]


def test_open_initiative_pr_refuses_while_an_epic_is_open(opted_in):
    gh = _all_closed()
    gh.issues[9]["state"] = "OPEN"
    result = s.cmd_open_initiative_pr(gh, 6)
    assert result["opened"] is False and result["open_epics"] == [9]


def test_open_initiative_pr_refuses_without_the_opt_in(opted_in):
    result = s.cmd_open_initiative_pr(_all_closed(opt_in=False), 6)
    assert result["opened"] is False and "no initiative branch" in result["reason"]


def _initiative_pr(gh, comments=(), checks=(), files=("backend/app.py",)):
    gh.prs[300] = {"headRefName": "initiative-6", "baseRefName": "main", "state": "OPEN",
                   "headRefOid": "abcd0000", "comments": list(comments),
                   "checks": list(checks), "files": list(files)}


_PASS = {"workflow": "Backend CI", "name": "backend", "bucket": "pass"}


def test_merge_initiative_pr_refuses_until_checks_and_close_suites_are_green(opted_in):
    gh = _all_closed()
    _initiative_pr(gh)
    held = s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")
    assert held["merged"] is False
    assert held["evidence"]["missing_workflows"] == ["Backend CI"]  # deferred suites owed here
    assert held["evidence"]["unattested_close_suites"] == ["e2e"]

    _initiative_pr(gh, checks=[_PASS])
    held = s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")
    assert held["merged"] is False and "`e2e` attestation" in held["reason"]
    assert gh.merges == []


def test_merge_initiative_pr_merges_with_the_evidence_and_unblocks_close(opted_in):
    gh = _all_closed()
    _initiative_pr(gh, checks=[_PASS],
                   comments=["<!-- local-ci: e2e:300 @ abcd0000 -->"])

    result = s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")

    assert result["merged"] is True and gh.merges == [(300, False)]
    assert "initiative-merged: 6 pr:300" in gh.issues[6]["comments"][-1]
    assert s.cmd_check_initiative_closeable(gh, 6)["closeable"] is True
    again = s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")
    assert again["merged"] is True and again["recovered"] is True


def test_merge_initiative_pr_refuses_a_branch_behind_main(opted_in):
    gh = _all_closed()
    _initiative_pr(gh, checks=[_PASS], comments=["<!-- local-ci: e2e:300 @ abcd0000 -->"])
    gh.branch_behind_by = lambda branch, base="main": 2
    result = s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")
    assert result["merged"] is False and result["behind_base"] == 2
