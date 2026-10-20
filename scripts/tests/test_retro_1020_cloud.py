"""Retro 2026-10-20, cloud session lifecycle: end markers that record what a session waits on
(`waiting-epic`, `waiting-human`), relaunch once that clears, `launch-cloud-epic
--allow-overlap`, `record-cloud-session` for a session started by hand, and the heartbeat a
cloud session stamps into its launch marker."""
import re
from datetime import timedelta

import pytest

import sdlc_next as s
from tests.test_cloud_epics import (CLOUD, FP_A, LAUNCH_OUT, NOW, CloudGh, FakeClaude, FakeGit,
                                    _ago, _ended, _initiative, _launched, _status, _survey)


class EditGh(CloudGh):
    """CloudGh whose comments can be edited in place (the heartbeat's PATCH)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.edits = []

    def issue_comment_edit(self, comment, body):
        self.edits.append(body)
        for i in self.issues.values():
            for k, c in enumerate(i["comments"]):
                text = c["body"] if isinstance(c, dict) else c
                if text == comment["body"]:
                    i["comments"][k] = {**c, "body": body} if isinstance(c, dict) else body
                    return
        raise AssertionError("edited a comment that is not on any issue")


def _edit_initiative(*epics, extra=()):
    base = _initiative(*epics, extra=extra)
    gh = EditGh([])
    gh.issues = base.issues
    return gh


def _end(gh, epic, outcome, **kw):
    return s.cmd_end_cloud_session(gh, epic, outcome, **kw)


# --- end markers that record what the session waits on ------------------------------------

def test_waiting_epic_needs_the_epic_it_waits_on():
    gh = _initiative({"number": 2, "comments": [_launched()]}, 3,
                     extra=[{"number": 5, "labels": ["type:task"], "parent": 2}])
    with pytest.raises(s.GhError, match="needs --waits-on"):
        _end(gh, 2, "waiting-epic")
    with pytest.raises(s.GhError, match="not an Epic"):
        _end(gh, 2, "waiting-epic", waits_on=[5])
    out = _end(gh, 2, "waiting-epic", waits_on=[3])
    assert out["waits"] == [3]
    assert "outcome=waiting-epic" in gh.comments_on(2)[-1]
    assert " waits=3 -->" in gh.comments_on(2)[-1]


def test_waiting_human_defaults_to_the_units_parked_on_the_operator():
    gh = _initiative({"number": 2, "comments": [_launched()]},
                     extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                             "status": "needs-human"},
                            {"number": 6, "labels": ["type:task"], "parent": 2,
                             "status": "awaiting-human-review"},
                            {"number": 7, "labels": ["type:task"], "parent": 2,
                             "status": "in-progress"},
                            {"number": 8, "labels": ["type:task"], "parent": 2,
                             "status": "needs-human", "state": "CLOSED"}])
    out = _end(gh, 2, "waiting-human", note="gate on #6")
    assert out["waits"] == [5, 6]
    session = s.latest_cloud_session([{"body": b} for b in gh.comments_on(2)])
    assert session["ended"] == {"outcome": "waiting-human", "at": session["ended"]["at"],
                                "waits": [5, 6]}


def test_an_old_end_marker_without_waits_parses_as_before():
    session = s.latest_cloud_session([{"body": _launched()}, {"body": _ended()}])
    assert session["ended"]["waits"] == [] and session["heartbeat"] is None


# --- relaunch once the recorded wait clears -----------------------------------------------

def _waiting_epic_tree(dep_state="OPEN"):
    return _initiative({"number": 2, "comments": [_launched(minutes=300),
                                                  _ended(minutes=200, outcome="waiting-epic")
                                                  .replace(" -->", " waits=3 -->")]},
                       {"number": 3, "state": dep_state},
                       extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                               "updated": _ago(10)}])


def test_a_waiting_epic_session_waits_for_its_epic_despite_new_activity():
    survey = _survey(_waiting_epic_tree(), candidates=[2])
    assert survey["launchable"] == []
    assert "waiting on #3" in survey["waiting"][0]["reason"]
    # Positive control: --relaunch still overrides.
    assert _survey(_waiting_epic_tree(), candidates=[2], relaunch=True)["launchable"] == [2]


def test_a_waiting_epic_session_relaunches_once_its_epic_closed_without_activity():
    gh = _waiting_epic_tree(dep_state="CLOSED")
    gh.issues[5]["updated"] = _ago(250)  # nothing happened on #2 since it ended
    survey = _survey(gh, candidates=[2])
    assert survey["launchable"] == [2] and survey["relaunch"] == [2]


def _waiting_human_tree(child_status="needs-human", child_comments=()):
    return _initiative({"number": 2, "comments": [
                            _launched(minutes=300),
                            _ended(minutes=200).replace(" -->", " waits=5 -->")]},
                       extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                               "status": child_status, "updated": _ago(10),
                               "comments": list(child_comments)}])


def test_a_waiting_human_session_waits_until_its_unit_is_answered():
    gh = _waiting_human_tree()
    survey = _survey(gh, candidates=[2])
    assert survey["launchable"] == [] and "waiting on #5" in survey["waiting"][0]["reason"]
    # A comment from before the end is not an answer.
    gh.issues[5]["comments"].append({"body": "earlier", "createdAt": _ago(250)})
    assert _survey(gh, candidates=[2])["launchable"] == []


def test_a_waiting_human_session_relaunches_on_a_github_answer():
    gh = _waiting_human_tree(child_comments=[{"body": "go with B", "createdAt": _ago(30)}])
    assert _survey(gh, candidates=[2])["launchable"] == [2]


def _gate_tree(pr):
    gh = _waiting_human_tree(child_status="awaiting-human-review", child_comments=[
        {"body": "<!-- gate-pr: architecture:40 -->", "createdAt": _ago(250)}])
    gh.issues[5]["updated"] = _ago(250)
    gh.prs[40] = {"number": 40, **pr}
    return gh


def test_a_waiting_human_gate_clears_when_the_operator_merges_the_gate_pr():
    # humanChannel github: merging the gate PR is the approval; status and thread stay put.
    gh = _gate_tree({"state": "MERGED", "mergedAt": _ago(30), "updatedAt": _ago(30)})
    assert _survey(gh, candidates=[2])["launchable"] == [2]


def test_a_waiting_human_gate_clears_on_pr_activity_after_the_end():
    gh = _gate_tree({"state": "OPEN", "mergedAt": None, "updatedAt": _ago(30)})
    assert _survey(gh, candidates=[2])["launchable"] == [2]


def test_a_waiting_human_gate_pr_untouched_since_the_end_still_waits():
    gh = _gate_tree({"state": "OPEN", "mergedAt": None, "updatedAt": _ago(250)})
    survey = _survey(gh, candidates=[2])
    assert survey["launchable"] == [] and "waiting on #5" in survey["waiting"][0]["reason"]


def test_a_waiting_human_epic_itself_clears_on_an_answer():
    gh = _initiative({"number": 2, "status": "needs-human", "comments": [
                          _launched(minutes=300),
                          _ended(minutes=200).replace(" -->", " waits=2 -->")]})
    assert _survey(gh, candidates=[2])["launchable"] == []
    gh.issues[2]["comments"].append({"body": "close it as is", "createdAt": _ago(30)})
    assert _survey(gh, candidates=[2])["launchable"] == [2]


@pytest.mark.parametrize("status", ["todo", "feedback-received", "in-progress"])
def test_a_waiting_human_session_relaunches_once_its_unit_left_the_parked_statuses(status):
    assert _survey(_waiting_human_tree(child_status=status), candidates=[2])["launchable"] == [2]


def test_launch_relaunches_a_cleared_wait_without_the_flag():
    gh = _waiting_epic_tree(dep_state="CLOSED")
    out = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=FakeClaude(LAUNCH_OUT))
    assert out["launched"] is True and out["relaunched"] is True


def test_cloud_status_flags_a_cleared_wait_for_relaunch():
    gh = _waiting_epic_tree(dep_state="CLOSED")
    gh.issues[2]["labels"].append(CLOUD)
    result = _status(gh)
    row = result["epics"][0]
    assert row["relaunch_ready"] is True and row["waits"]["resolved"] is True
    assert result["attention"] is True
    pending = _status(_waiting_epic_tree())["epics"][0]
    assert pending["relaunch_ready"] is False and pending["waits"]["pending"] == [3]


# --- launch-cloud-epic --allow-overlap -----------------------------------------------------

def _overlap_tree():
    return _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()],
                        "body": FP_A},
                       {"number": 3, "body": "## Footprint\n\n- `src/a/x.py`\n"})


def test_allow_overlap_launches_an_overlapping_epic_and_says_so():
    gh = _overlap_tree()
    refused = s.cmd_launch_cloud_epic(gh, 3, runner=FakeGit(), claude=FakeClaude(LAUNCH_OUT))
    assert refused["refused"] is True and "overlaps" in refused["reason"]
    out = s.cmd_launch_cloud_epic(gh, 3, runner=FakeGit(), claude=FakeClaude(LAUNCH_OUT),
                                  allow_overlap=True)
    assert out["launched"] is True and out["allow_overlap"] is True
    marker = gh.comments_on(3)[-1]
    assert "overlap=1 -->" in marker and "--allow-overlap" in marker
    assert s.latest_cloud_session([{"body": marker}])["overlap"] is True


def test_allow_overlap_still_respects_the_cap(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_SESSIONS", 1)
    out = s.cmd_launch_cloud_epic(_overlap_tree(), 3, runner=FakeGit(),
                                  claude=FakeClaude(LAUNCH_OUT), allow_overlap=True)
    assert out["refused"] is True and "cap" in out["reason"]


# --- record-cloud-session ----------------------------------------------------------------

def test_record_cloud_session_labels_the_epic_and_posts_a_manual_marker():
    gh = _initiative(2)
    out = s.cmd_record_cloud_session(gh, 2, "https://claude.ai/code/session_01HAND")
    assert out["recorded"] is True and out["labelled"] is True
    assert CLOUD in gh.issues[2]["labels"]
    session = s.latest_cloud_session([{"body": b} for b in gh.comments_on(2)])
    assert session["id"] == "session_01HAND" and session["manual"] is True
    assert session["ended"] is None
    again = s.cmd_record_cloud_session(gh, 2, "session_01HAND")
    assert again["already_recorded"] is True and len(gh.comments_on(2)) == 1


def test_record_cloud_session_refuses_while_another_is_live_and_bad_ids():
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]})
    out = s.cmd_record_cloud_session(gh, 2, "session_01OTHER")
    assert out["refused"] is True and "still live" in out["reason"]
    with pytest.raises(s.GhError, match="names no session"):
        s.cmd_record_cloud_session(gh, 2, "not a session")
    # Positive control: once the live one is end-marked, recording works.
    gh.issues[2]["comments"].append(_ended())
    assert s.cmd_record_cloud_session(gh, 2, "session_01OTHER")["recorded"] is True


def test_a_recorded_session_holds_a_cap_slot():
    gh = _initiative(2, 3)
    s.cmd_record_cloud_session(gh, 2, "session_01HAND")
    survey = _survey(gh, candidates=[3])
    assert [r["epic"] for r in survey["running"]] == [2]


# --- heartbeat --------------------------------------------------------------------------

def _hb_tree():
    return _edit_initiative({"number": 2, "labels": ["type:epic", CLOUD],
                             "comments": [_launched(minutes=300)]},
                            extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                                    "updated": _ago(200)}])


def test_heartbeat_stamps_the_launch_marker_in_place_and_throttles():
    gh = _hb_tree()
    out = s.cmd_cloud_heartbeat(gh, 2, now=NOW)
    assert out["stamped"] is True and len(gh.comments_on(2)) == 1
    marker = gh.comments_on(2)[0]
    assert re.search(rf"heartbeat={_ago(0)} state=working -->", marker)
    assert "launched=" in marker and "url=https://claude.ai/code/session_01AAA" in marker
    soon = s.cmd_cloud_heartbeat(gh, 2, now=NOW + timedelta(seconds=60))
    assert soon["stamped"] is False and len(gh.edits) == 1
    # A state change is stamped at once.
    asking = s.cmd_cloud_heartbeat(gh, 2, state="asking", now=NOW + timedelta(seconds=60))
    assert asking["stamped"] is True and "state=asking" in gh.comments_on(2)[0]


def test_heartbeat_is_a_noop_without_a_live_session():
    gh = _edit_initiative({"number": 2, "comments": [_launched(), _ended()]})
    assert s.cmd_cloud_heartbeat(gh, 2, now=NOW)["stamped"] is False and gh.edits == []


def test_a_fresh_heartbeat_keeps_an_idle_epic_from_stalling():
    gh = _hb_tree()
    assert _status(gh)["epics"][0]["stalled"] is True  # control: idle 200 min, no heartbeat
    s.cmd_cloud_heartbeat(gh, 2, now=NOW - timedelta(minutes=10))
    row = _status(gh)["epics"][0]
    assert row["stalled"] is False and row["idle_minutes"] == 10
    assert row["heartbeat"] == _ago(10)


def test_an_old_heartbeat_still_stalls():
    gh = _hb_tree()
    s.cmd_cloud_heartbeat(gh, 2, now=NOW - timedelta(minutes=150))
    assert _status(gh)["epics"][0]["stalled"] is True


def test_a_session_asking_the_operator_does_not_stall_at_the_normal_bound():
    gh = _hb_tree()
    s.cmd_cloud_heartbeat(gh, 2, state="asking", now=NOW - timedelta(minutes=150))
    row = _status(gh)["epics"][0]
    assert row["asking"] is True and row["stalled"] is False


def test_a_session_asking_too_long_still_stalls_and_escalates():
    # 90 min x 6 = 540: a session that died mid-question frees its slot eventually.
    gh = _edit_initiative({"number": 2, "labels": ["type:epic", CLOUD],
                           "comments": [_launched(minutes=700)]},
                          extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                                  "updated": _ago(650)}])
    s.cmd_cloud_heartbeat(gh, 2, state="asking", now=NOW - timedelta(minutes=600))
    row = _status(gh)["epics"][0]
    assert row["asking"] is True and row["stalled"] is True
    # Control: the same session asking for 500 minutes is still within the bound.
    gh2 = _edit_initiative({"number": 2, "labels": ["type:epic", CLOUD],
                            "comments": [_launched(minutes=700)]},
                           extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                                   "updated": _ago(650)}])
    s.cmd_cloud_heartbeat(gh2, 2, state="asking", now=NOW - timedelta(minutes=500))
    assert _status(gh2)["epics"][0]["stalled"] is False


def test_heartbeat_quietly_runs_only_in_the_cloud_and_never_raises(monkeypatch):
    gh = _hb_tree()
    assert s.cloud_heartbeat_quietly(gh, 5) is None and gh.edits == []
    monkeypatch.setenv("SDLC_PLACEMENT", "cloud")
    assert s.cloud_heartbeat_quietly(gh, 5) is None and len(gh.edits) == 1

    def boom(*a, **kw):
        raise s.GhError("HTTP 502")
    monkeypatch.setattr(gh, "issue_comment_edit", boom)
    gh.issues[2]["comments"][0] = _launched(minutes=300)  # unstamped again
    assert "HTTP 502" in s.cloud_heartbeat_quietly(gh, 5)


def test_rest_comment_edit_patches_by_rest_id():
    calls = []
    rest = s.GitHubRest(repo="o/r", runner=lambda argv: calls.append(argv) or "")
    rest.issue_comment_edit({"id": "IC_x", "databaseId": 42, "body": "a"}, "b")
    assert calls == [["gh", "api", "-X", "PATCH", "repos/o/r/issues/comments/42", "-f", "body=b"]]
    with pytest.raises(s.GhError, match="no REST id"):
        rest.issue_comment_edit({"id": "IC_x", "body": "a"}, "b")
