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
                    "placement": None, "name": None}
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

    assert result["merged"] is True and result["base"] == "main"
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

    def no_branch(base, head):
        raise s.GhError(f"gh: Not Found (HTTP 404) comparing {base}...{head}")
    gh.branch_commits = no_branch
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


# --- release gating (v0.3.17): unlabelled / unconfigured is exactly v0.3.16 --------------

class ListFails(FakeGh):
    """The issue list read fails (GitHub error or timeout)."""

    def issue_list(self):
        raise s.GhError("gh: timed out reading the issue list")


class AncestryFails(FakeGh):
    """The per-issue ancestry read (`issue_epic_info`) fails."""

    def issue_epic_info(self, n):
        raise s.GhError("gh: HTTP 502 reading the issue")


def _hints(runs_dir):
    import json
    return json.loads((runs_dir / s.INITIATIVE_HINTS_FILE).read_text())


@pytest.mark.parametrize("opt_in", [True, False])
def test_without_initiative_profiles_every_base_is_the_v0316_one(opt_in):
    """No `initiativeProfiles` (most driven repos): even an Initiative carrying the label keeps
    its Epic on main, the Epic's children on `epic-<n>`, the Initiative's own Tasks on main."""
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9},
               {"number": 13, "labels": ["type:task"], "parent": 6}, opt_in=opt_in)
    assert s.integration_base(gh, 9, "epic") == "main"
    assert s.integration_base(gh, 12) == "epic-9"
    assert s.integration_base(gh, 13) == "main"
    assert s.epic_base(NoLookups([]), 9) == "main"


def test_children_of_an_epic_under_an_unlabelled_initiative_keep_the_epic_branch(opted_in):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9},
               {"number": 13, "labels": ["type:task"], "parent": 6}, opt_in=False)
    assert s.integration_base(gh, 9, "epic") == "main"
    assert s.integration_base(gh, 12) == "epic-9"
    assert s.integration_base(gh, 13) == "main"


def test_profiles_without_branch_never_look_up_the_epic_base(monkeypatch):
    """A repo whose profiles only turn test Tasks off has no initiative branch to find."""
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles",
                        [{**PROFILE, "branch": False}])
    assert s.integration_base(NoLookups([]), 9, "epic") == "main"


def test_close_epic_without_initiative_profiles_is_the_v0316_merge_into_main():
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    calls = _closing(gh)

    result = s.cmd_close_epic(gh, 9, runner=lambda argv: "")

    assert result["merged"] is True and result["base"] == "main"
    assert calls["behind_base"] == ["main"]
    assert calls["created"][0]["base"] == "main"
    assert calls["created"][0]["body"] == "Integration of every child of #9.\n\nCloses #9"


def test_create_lld_tasks_without_initiative_profiles_creates_both_standing_tasks(repo):
    gh = _tree()  # carries the label, but nothing in config matches it
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _LLD_WITH_STANDING)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["greet-endpoint", "it", "e2e"]


@pytest.mark.parametrize("opt_in", [True, False])
def test_a_failed_issue_list_never_picks_an_epic_base(opted_in, opt_in):
    """A read error while resolving the profile raises: an unlabelled Initiative's Epic is not
    sent to a branch, nor a labelled one's to main."""
    gh = ListFails([])
    with pytest.raises(s.GhError):
        s.integration_base(gh, 9, "epic")
    with pytest.raises(s.GhError):
        s.cmd_sync_branch(gh, ".", 9, "epic", runner=lambda argv: "")


def test_a_failed_issue_list_skips_the_epic_sync(opted_in):
    out = s.sync_epic_if_due(ListFails([]), 9, "run-1", repo_path=".",
                             runner=lambda argv: pytest.fail(f"ran {argv}"))
    assert out["synced"] is False and "timed out" in out["error"]


def test_a_failed_ancestry_read_never_drops_the_standing_test_tasks(repo, opted_in):
    gh = AncestryFails([{"number": 6, "labels": ["type:initiative", OPT_IN], "title": "Tijori"},
                        {"number": 9, "labels": ["type:epic"], "parent": 6}])
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _LLD_WITH_STANDING)
    before = set(gh.issues)
    with pytest.raises(s.GhError):
        s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    assert set(gh.issues) == before


def test_a_unit_under_no_initiative_is_hinted_with_the_defaults(opted_in, runs_dir):
    """Regression: a parentless unit (or one under a bare Epic) gets a recorded no-profile hint,
    so the SubagentStart hook adds no `initiative-profile` line for it."""
    gh = FakeGh([{"number": 20, "labels": ["type:epic"]},
                 {"number": 21, "labels": ["type:task"], "parent": 20}])
    assert s.unit_initiative_profile(gh, 21)["name"] is None
    hints = _hints(runs_dir)
    assert hints["21"]["name"] is None and hints["20"]["name"] is None


def test_a_failed_profile_read_at_start_stage_forgets_the_stale_hint(opted_in, runs_dir,
                                                                      monkeypatch):
    """Regression: a stale `testTasks: false` hint must not outlive a failed re-read."""
    s._record_hints(s.INITIATIVE_HINTS_FILE,
                    {10: {"initiative": 6, "name": "tijori", "branch": True,
                          "testTasks": False}})
    gh = FakeGh([{"number": 10, "labels": ["type:task"]}])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})

    def fails(*a, **k):
        raise s.GhError("gh: HTTP 502 reading the issue")
    monkeypatch.setattr(s, "unit_initiative_profile", fails)

    result = s.cmd_start_stage(gh, 10, "development")

    assert "initiative_profile_error" in result
    assert _hints(runs_dir)["10"] is None


def test_without_initiative_profiles_an_initiative_branch_is_not_a_pipeline_unit():
    assert s._unit_of_branch("initiative-7") is None
    assert s._unit_of_branch("epic-7") == ("epic", 7)


def test_with_initiative_profiles_an_initiative_branch_is_a_pipeline_unit(opted_in):
    assert s._unit_of_branch("initiative-7") == ("initiative", 7)


# --- close guard: an initiative branch left unlanded (v0.3.17) ---------------------------

def _verified(gh):
    gh.issues[6]["comments"] = [
        "<!-- initiative-verification: requirements:6 @ 2026-10-07T00:00:00Z -->"]
    return gh


def test_close_refused_while_the_initiative_branch_holds_work_after_the_label_is_removed(
        opted_in):
    """Regression: Epics closed into initiative-6, then the label came off -- the Initiative
    must not close with that work never reaching main."""
    gh = _verified(_all_closed(opt_in=False))
    gh.branch_commits = lambda base, head: (["c1", "c2"] if (base, head) == ("main", "initiative-6")
                                            else pytest.fail(f"compared {base}...{head}"))

    result = s.cmd_check_initiative_closeable(gh, 6)

    assert result["closeable"] is False and result["unlanded_commits"] == 2
    assert "`initiative-6`" in result["reason"] and "restore the label" in result["reason"]
    assert "merge-initiative-pr 6" in result["reason"]
    closed = s.cmd_close_initiative(gh, 6)
    assert closed["closed"] is False and gh.issues[6]["state"] == "OPEN"


def test_close_allowed_once_the_initiative_branch_is_fully_on_main(opted_in):
    """Positive control: the branch exists but main has every commit of it."""
    gh = _verified(_all_closed(opt_in=False))
    gh.branch_commits = lambda base, head: []

    assert s.cmd_check_initiative_closeable(gh, 6)["closeable"] is True
    assert s.cmd_close_initiative(gh, 6)["closed"] is True


def test_a_failed_unlanded_read_never_closes_the_initiative(opted_in):
    gh = _verified(_all_closed(opt_in=False))

    def fails(base, head):
        raise s.GhError("gh: HTTP 502 comparing")
    gh.branch_commits = fails

    with pytest.raises(s.GhError):
        s.cmd_close_initiative(gh, 6)
    assert gh.issues[6]["state"] == "OPEN"


def test_without_initiative_profiles_close_makes_no_branch_call():
    """Positive control: a repo without `initiativeProfiles` (no initiative branch ever)
    closes as v0.3.16, with no compare call at all."""
    gh = _verified(_all_closed(opt_in=False))
    gh.branch_commits = lambda base, head: pytest.fail("compare called without the opt-in")

    assert s.cmd_check_initiative_closeable(gh, 6) == {"initiative": 6, "closeable": True,
                                                        "epics": [9]}
    assert s.cmd_close_initiative(gh, 6)["closed"] is True
