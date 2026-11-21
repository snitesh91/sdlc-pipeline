"""Review 2026-11-21 (run and cloud): the run lock's release keeps the state file fresh,
`--skip-epic` holds for cloud launches, a real session claims an `id=unknown` marker,
`nudge-cloud-epic` refuses in a cloud session, close-epic pins the merge to the head it
judged, and the dead `skip-pr-review` / `pr-add-label` commands are gone."""
import argparse
import os
import time

import pytest

import sdlc_next as s
from sdlc import run_state
from tests.test_cloud_epics import (CLOUD, FakeClaude, FakeGit, NOW, _cli, _initiative,
                                    _launched, _stall_tree)
from tests.test_epic_close_evidence import HEAD, _marks, _ready_epic
from tests.test_placement import _tree


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01SELF")


def _next_action(gh, run_id, epic=9):
    return s.cmd_next_action(gh, argparse.Namespace(epic=epic, run_id=run_id, repo_path=".",
                                                    skip_epic=[], sync_epic=False))


# --- run lock ---------------------------------------------------------------------------

def test_a_released_run_state_stays_fresh_for_the_hooks_and_prune_stale(runs):
    # Regression: release set mtime 0, so the hooks' run guard dropped at the first `none`.
    gh = _tree(epic_labels=(s.LABELS["architected"],), task=False)
    assert _next_action(gh, "run-a")["action"] == "none"
    age = time.time() - os.path.getmtime(run_state._run_state_path(9))
    assert s.RESUME_LIVE_WINDOW_MINUTES * 60 < age < s.RESUME_LIVE_WINDOW_MINUTES * 60 + 60
    assert _next_action(gh, "run-b").get("refused") is None  # still released


def test_a_probe_with_no_run_id_never_releases_a_live_run(runs):
    gh = _tree()
    assert _next_action(gh, "run-a")["action"] != "none"
    gh.issues[10]["state"] = "CLOSED"
    gh.issues[9]["labels"].append(s.LABELS["architected"])
    assert _next_action(gh, None)["action"] == "none"
    assert _next_action(gh, "run-b")["refused"] is True


# --- cloud ------------------------------------------------------------------------------

def test_skip_epic_holds_for_a_cloud_launch(monkeypatch):
    # Regression: with no session-creation tool the skipped Epic came back as launchable forever.
    monkeypatch.setitem(s.PIPELINE["placement"], "initiativeEpics", "cloud")
    gh = _initiative(2, 3)
    out = s.decide_next_action(gh, 1, skip_epics=[2], runner=FakeGit())
    assert out["action"] == "launch-cloud-epics" and out["epics"] == [3]
    out = s.decide_next_action(gh, 1, skip_epics=[2, 3], runner=FakeGit())
    assert out["action"] == "none" and "#2 parked this run" in out["reason"]


def test_a_real_session_claims_a_launch_marker_with_no_id(cloud):
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                      "comments": [_launched("unknown")]})
    assert s.cmd_record_cloud_session(gh, 2, None)["recorded"] is True
    assert s.latest_cloud_session(gh.issue_view(2)["comments"])["id"] == "session_01SELF"
    # Positive control: a known live session still holds the Epic.
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]})
    assert s.cmd_record_cloud_session(gh, 2, None)["refused"] is True


def test_nudge_refuses_in_a_cloud_session(cloud):
    claude = FakeClaude("ok")
    out = s.cmd_nudge_cloud_epic(_stall_tree(), 2, claude=claude, now=NOW)
    assert out["refused"] is True and out["nudged"] is False and claude.calls == []


# --- close-epic -------------------------------------------------------------------------

def test_close_epic_pins_the_merge_to_the_head_it_judged():
    gh = _ready_epic([_marks()[1]], checks=[])
    gh.files_since = lambda sha, branch: []
    pinned = []
    gh.pr_merge = lambda n, match_head=None, **kw: pinned.append(match_head)
    assert s.cmd_close_epic(gh, 9, runner=lambda argv: "")["merged"] is True
    assert pinned == [HEAD]


# --- dead commands ----------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["skip-pr-review", "9", "--pr", "4", "--reason", "x"],
                                  ["pr-add-label", "4", "ci:local"]])
def test_the_dead_commands_are_gone(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _cli(monkeypatch, _initiative(2), argv)
