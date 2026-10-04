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


# ---- 2: next-action --run-id reports config drift ----

def _drift_run(monkeypatch, tmp_path, origin=None):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(s, "origin_config", lambda runner=None: origin)
    s.run_cap_state(9, "r")


def test_a_local_config_edit_mid_run_is_reported_once(monkeypatch, tmp_path):
    _drift_run(monkeypatch, tmp_path)
    monkeypatch.setattr(s, "CONFIG", {**s.CONFIG, "docRoot": "elsewhere"})
    assert s.config_drift(9, "r") == {"source": "local", "keys": ["docRoot"]}
    assert s.config_drift(9, "r") is None


def test_an_unchanged_config_reports_no_drift(monkeypatch, tmp_path):
    # Positive control.
    _drift_run(monkeypatch, tmp_path, origin=dict(s.CONFIG))
    assert s.config_drift(9, "r") is None
    assert s.config_drift(9, None) is None


def test_an_origin_only_difference_keeps_reporting_until_the_checkout_matches(monkeypatch, tmp_path):
    ahead = {**s.CONFIG, "pipeline": {"x": 1}, "new": True}
    _drift_run(monkeypatch, tmp_path, origin=ahead)
    expected = {"source": "origin", "keys": ["new", "pipeline"]}
    assert s.config_drift(9, "r") == expected and s.config_drift(9, "r") == expected
    monkeypatch.setattr(s, "CONFIG", ahead)
    assert s.config_drift(9, "r")["source"] == "local"
    assert s.config_drift(9, "r") is None


def test_a_state_file_without_a_digest_is_stamped_silently(monkeypatch, tmp_path):
    _drift_run(monkeypatch, tmp_path)
    s._write_run_state(9, {"run_id": "r", "terminal": []})
    assert s.config_drift(9, "r") is None
    assert s.read_run_state(9)["config_digest"] == s.config_digest(s.CONFIG)


def test_next_action_with_a_run_id_carries_config_changed(monkeypatch, tmp_path):
    import argparse
    _drift_run(monkeypatch, tmp_path)
    monkeypatch.setattr(s, "CONFIG", {**s.CONFIG, "docRoot": "elsewhere"})
    gh = _epic_tree()
    args = argparse.Namespace(epic=9, run_id="r", repo_path=".", skip_epic=[])
    assert s.cmd_next_action(gh, args)["config_changed"] == {"source": "local",
                                                            "keys": ["docRoot"]}
    assert "config_changed" not in s.cmd_next_action(gh, args)


def test_origin_config_reads_the_committed_file_and_fails_open(monkeypatch, tmp_path):
    from tests.test_sdlc_next import ScriptedRunner
    cfg = tmp_path / "repo" / ".claude" / s.CONFIG_FILENAME
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}")
    monkeypatch.setenv("SDLC_CONFIG", str(cfg))
    top = str((tmp_path / "repo").resolve())
    show = ("git", "-C", top, "show", f"origin/main:.claude/{s.CONFIG_FILENAME}")
    runner = ScriptedRunner({("git", "-C", str(cfg.parent.resolve()), "rev-parse",
                              "--show-toplevel"): top + "\n", show: '{"repo": "a/b"}'})
    assert s.origin_config(runner) == {"repo": "a/b"}
    runner.fail_on.add(show)
    assert s.origin_config(runner) is None
