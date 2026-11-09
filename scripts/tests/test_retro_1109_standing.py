"""Retro 2026-11-09 (v0.4.5): every Epic owns `epic-<n>`, standing ones included; every
Initiative owns `initiative-<i>` by default; an Initiative's standing Epic is its Bugs Epic
(runs last, `file-closing-delta --defer`, closed by close-epic); a long-lived standing Epic
lands with `land-epic` and stays open. Each regression has a positive control."""
import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh

STANDING = "epic:standing"
OPT_OUT = {"name": "plain", "match": {"label": "initiative:plain"}, "branch": False}


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    from sdlc.config import _INITIATIVE_PROFILE_DEFAULTS
    monkeypatch.setitem(_INITIATIVE_PROFILE_DEFAULTS, "branch", True)  # the shipped default
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [OPT_OUT])


def _initiative(*extra, labels=("type:initiative",)):
    return FakeGh([{"number": 6, "labels": list(labels), "title": "Init"},
                   {"number": 9, "labels": ["type:epic"], "parent": 6, "title": "Feature"},
                   {"number": 8, "labels": ["type:epic", STANDING], "parent": 6,
                    "title": "Bugs: Init"},
                   *extra])


# --- integration base -------------------------------------------------------------------

def test_a_standing_child_integrates_into_its_epic_branch():
    """Regression: before v0.4.5 a standing child merged straight into main."""
    gh = FakeGh([{"number": 30, "labels": ["type:epic", STANDING], "title": "RTB"},
                 {"number": 31, "labels": ["type:task"], "parent": 30}])
    assert s.integration_base(gh, 31) == "epic-30"


def test_a_parentless_issue_still_integrates_into_main():
    """Positive control: the hotfix path."""
    assert s.integration_base(FakeGh([{"number": 31, "labels": ["type:task"]}]), 31) == "main"


def test_every_initiative_owns_its_branch_by_default():
    """Regression: an Initiative no profile matches used to close its Epics into main."""
    gh = _initiative()
    assert s.integration_base(gh, 9, "epic") == "initiative-6"
    assert s.integration_base(gh, 8, "epic") == "initiative-6"


def test_a_profile_with_branch_false_opts_an_initiative_out():
    """Positive control: the opt-out keeps main."""
    assert s.integration_base(_initiative(labels=("type:initiative", "initiative:plain")),
                              9, "epic") == "main"


# --- the Bugs Epic ----------------------------------------------------------------------

def test_the_initiative_loop_runs_the_bugs_epic_last():
    gh = _initiative()
    step = s.decide_next_action(gh, 6)
    assert step["action"] in ("cut-phase-tasks", "run-epic") and step["epic"] == 9
    gh.issues[9]["state"] = "CLOSED"
    step = s.decide_next_action(gh, 6)
    assert step["action"] == "run-epic" and step["epic"] == 8


def test_the_bugs_epic_waits_while_a_sibling_is_open():
    gh = _initiative()

    def due():
        issues = {i["number"]: i for i in gh.issue_list()}
        return s.bugs_epic_due(issues, issues[8])
    assert due() is False
    gh.issues[9]["state"] = "CLOSED"
    assert due() is True


def test_file_closing_delta_defer_files_under_the_bugs_epic():
    gh = _initiative()
    out = s.cmd_file_closing_delta(gh, 9, "Bug: x", "body", defer=True)
    assert out["parent"] == 8
    assert gh.issues[out["delta_issue"]]["parent"] == 8
    plain = s.cmd_file_closing_delta(gh, 9, "Bug: y", "body")
    assert plain["parent"] == 9 and gh.issues[plain["delta_issue"]]["parent"] == 9


def test_file_closing_delta_defer_refuses_without_a_bugs_epic():
    gh = FakeGh([{"number": 9, "labels": ["type:epic"], "title": "Bare"}])
    out = s.cmd_file_closing_delta(gh, 9, "Bug: x", "body", defer=True)
    assert out["refused"] is True and "without --defer" in out["reason"]


def test_close_epic_closes_an_empty_bugs_epic():
    gh = _initiative()
    gh.issues[9]["state"] = "CLOSED"
    out = s.cmd_close_epic(gh, 8, runner=lambda argv: "")
    assert out["merged"] is True and out["nothing_to_land"] is True
    assert gh.issues[8]["state"] == "CLOSED"


def test_close_epic_refuses_a_standing_epic_with_no_initiative():
    gh = FakeGh([{"number": 30, "labels": ["type:epic", STANDING], "title": "RTB"}])
    out = s.cmd_close_epic(gh, 30, runner=lambda argv: "")
    assert out["merged"] is False and "land-epic 30" in out["reason"]


# --- land-epic --------------------------------------------------------------------------

def _rtb(commits=("c1",)):
    gh = FakeGh([{"number": 30, "labels": ["type:epic", STANDING], "title": "RTB"},
                 {"number": 31, "labels": ["type:task"], "parent": 30, "state": "CLOSED"},
                 {"number": 32, "labels": ["type:task"], "parent": 30}])
    calls = {"merged": [], "created": []}
    gh.branch_commits = lambda base, head: list(commits)
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.pr_list_for_branch = lambda branch, state="open": []
    gh.pr_create = lambda **kw: calls["created"].append(kw) or 77
    gh.pr_checks = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_files = lambda n: []
    gh.files_since = lambda sha, branch: []
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n, method="squash", **kw: calls["merged"].append((n, method))
    return gh, calls


def test_land_epic_merges_with_a_merge_commit_and_keeps_the_epic_open():
    gh, calls = _rtb()
    out = s.cmd_land_epic(gh, 30, runner=lambda argv: "")
    assert out["landed"] is True and calls["merged"] == [(77, "merge")]
    assert calls["created"][0]["base"] == "main" and "Closes" not in calls["created"][0]["body"]
    assert gh.issues[30]["state"] == "OPEN"


def test_land_epic_with_nothing_to_land_opens_no_pr():
    gh, calls = _rtb(commits=())
    out = s.cmd_land_epic(gh, 30, runner=lambda argv: "")
    assert out["landed"] is False and "nothing to land" in out["reason"]
    assert calls["created"] == []


def test_land_epic_refuses_a_non_standing_epic_and_a_bugs_epic():
    assert s.cmd_land_epic(_initiative(), 9)["landed"] is False
    assert "close-epic" in s.cmd_land_epic(_initiative(), 8)["reason"]
