"""Regression + positive-control tests for the 2026-10-04 control-plane retro: not-planned
closes off the run cap, config drift, uncovered paths, edit-issue, and the red-check handoff gate.

Each regression goes RED against the pre-fix code; its positive control stays GREEN either way."""
import json

import pytest

import sdlc_next as s
from tests.test_retro_control_plane import _epic_tree, _no_worktree_runner


# ---- 1: close-issue --not-planned does not count toward the run cap ----

def _tracked_run(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    s._write_run_state(9, {"run_id": "r", "terminal": [], "in_flight": {"10": "development"}})
    return _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9})


def test_close_issue_not_planned_leaves_the_run_cap_untouched(monkeypatch, tmp_path):
    gh = _tracked_run(monkeypatch, tmp_path)
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner(),
                               not_planned=True)
    state = s.read_run_state(9)
    assert state["terminal"] == [] and state["in_flight"] == {}
    assert result["run_terminal"]["terminal_count"] == 0


def test_close_issue_completed_still_counts_toward_the_run_cap(monkeypatch, tmp_path):
    # Positive control: a completed close is booked as before.
    gh = _tracked_run(monkeypatch, tmp_path)
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner())
    state = s.read_run_state(9)
    assert state["terminal"] == [10] and state["in_flight"] == {}
    assert result["run_terminal"]["terminal_count"] == 1
