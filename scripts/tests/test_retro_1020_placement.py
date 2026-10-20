"""Retro 2026-10-20: Task-level placement -- a Task of a cloud Epic can stay on the laptop
(`localLabel`, or a `pipeline.placement.localTasks` kind), so the Epic keeps running in the
cloud and a laptop run drives only those Tasks."""
import argparse

import pytest

import sdlc_next as s
from tests.test_placement import CLOUD, PlaceGh, _must_not_run

LOCAL = "sdlc:local"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")


@pytest.fixture
def local_kinds(monkeypatch):
    def set_kinds(*kinds):
        monkeypatch.setitem(s.PIPELINE["placement"], "localTasks", list(kinds))
    return set_kinds


def _epic(*tasks):
    """Initiative #6 > cloud Epic #9 (architected) > the given Tasks."""
    return PlaceGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", CLOUD, s.LABELS["architected"]], "parent": 6},
        *[{"labels": ["type:task"], "parent": 9, "stage": "development", **t} for t in tasks],
    ])


def _next(gh, epic=9):
    return s.cmd_next_action(gh, argparse.Namespace(epic=epic, run_id=None, repo_path=".",
                                                    skip_epic=[], sync_epic=False))


def _no_lane(monkeypatch):
    monkeypatch.setattr(s, "dev_lane_refusal", lambda *a, **k: None)
    monkeypatch.setattr(s, "dev_lane_slots", lambda *a, **k: {
        "cap": 4, "occupied": [], "free": 4, "excluded_other_epic": [], "cross_epic": []})


def test_default_config_has_no_local_tasks_and_a_cloud_epic_still_refuses_the_laptop():
    assert s.PIPELINE["placement"]["localTasks"] == []
    gh = _epic({"number": 10, "title": "E2E tests"})
    assert s.task_local_reason(gh.issue_list()[2]) is None
    with pytest.raises(s.PlacementMismatch) as exc:
        _next(gh)
    assert "place <task> --where local" in exc.value.payload["hint"]


def test_a_cloud_session_skips_a_labelled_local_task(cloud, monkeypatch):
    _no_lane(monkeypatch)
    gh = _epic({"number": 10, "labels": ["type:task", LOCAL]}, {"number": 11})
    result = _next(gh)
    assert (result["action"], result["issue"]) == ("delegate", 11)
    assert result["placement_scope"] == "cloud"


def test_a_cloud_session_with_only_local_tasks_left_waits_local(cloud):
    gh = _epic({"number": 10, "labels": ["type:task", LOCAL]})
    result = _next(gh)
    assert result["action"] == "none"
    assert result["local_tasks"] == [10] and result["waiting_local"] is True
    assert "--outcome waiting-local" in result["reason"]


def test_a_laptop_run_on_a_cloud_epic_drives_only_its_local_tasks(monkeypatch):
    _no_lane(monkeypatch)
    gh = _epic({"number": 10}, {"number": 11, "labels": ["type:task", LOCAL]})
    result = _next(gh)
    assert (result["action"], result["issue"]) == ("delegate", 11)
    assert result["placement_scope"] == "local-tasks"
    gh.issues[11]["state"] = "CLOSED"
    # Positive control: no local Task left -> the laptop is refused again.
    with pytest.raises(s.PlacementMismatch):
        _next(gh)


def test_a_laptop_run_ends_none_once_its_local_tasks_are_waiting(monkeypatch):
    gh = _epic({"number": 10}, {"number": 11, "labels": ["type:task", LOCAL],
                                "status": "needs-human"})
    monkeypatch.setattr(s, "_initiative_epic_step", _must_not_run("initiative step"))
    result = _next(gh)
    assert result["action"] == "none" and result["local_tasks_only"] is True
    assert result["cloud_children"] == [10]


def test_start_stage_guards_each_side_off_the_others_tasks(cloud, monkeypatch):
    gh = _epic({"number": 10, "labels": ["type:task", LOCAL]}, {"number": 11})
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))
    with pytest.raises(s.PlacementMismatch) as exc:
        s.cmd_start_stage(gh, 10, "development")
    assert "local Task of cloud Epic #9" in exc.value.payload["hint"]
    monkeypatch.delenv("CLAUDE_CODE_REMOTE")
    with pytest.raises(s.PlacementMismatch):
        s.cmd_start_stage(gh, 11, "development")
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})
    assert s.cmd_start_stage(gh, 10, "development")["claimed"] is True


@pytest.mark.parametrize("kind, title, labels, local", [
    ("e2e-test", "E2E tests", [], True),
    ("e2e-test", "Integration tests", [], False),
    ("integration-test", "Standing integration test task", [], True),
    ("label:needs:stage", "Wire Stage webhook", ["needs:stage"], True),
    ("label:needs:stage", "Wire Stage webhook", [], False),
])
def test_local_task_kinds_from_config(local_kinds, kind, title, labels, local):
    local_kinds(kind)
    issue = {"number": 10, "title": title, "labels": [{"name": n} for n in ["type:task", *labels]],
             "parent": {"number": 9}}
    assert bool(s.task_local_reason(issue)) is local


def test_an_unknown_local_task_kind_is_an_error(local_kinds):
    local_kinds("phone")
    with pytest.raises(s.GhError, match="localTasks entry 'phone'"):
        s.task_local_reason({"number": 10, "title": "x", "labels": []})


def test_a_configured_kind_makes_the_e2e_task_local_without_a_label(cloud, local_kinds):
    local_kinds("e2e-test")
    gh = _epic({"number": 10, "title": "e2e tests"})
    assert _next(gh)["local_tasks"] == [10]


def test_list_parallel_ready_skips_local_tasks_in_the_cloud(cloud, monkeypatch):
    gh = _epic({"number": 10, "labels": ["type:task", LOCAL]}, {"number": 11})
    seen = {}
    monkeypatch.setattr(s, "_fetch_origin", lambda *a, **k: None)
    monkeypatch.setattr(s, "dev_lane_slots", lambda *a, **k: {
        "cap": 4, "occupied": [], "free": 4, "excluded_other_epic": [], "cross_epic": [],
        "active_branches": [], "holders": [], "stale_worktrees": []})
    monkeypatch.setattr(s, "dev_lane_footprints", lambda *a, **k: [])
    monkeypatch.setattr(s, "read_footprint",
                        lambda _p, n, **k: seen.setdefault(n, ["a/" + str(n)]))
    result = s.cmd_list_parallel_ready(gh, ".", 9)
    assert [u["issue"] for u in result["parallel_ready"]] == [11] and 10 not in seen


def test_place_a_task_local_and_back(local_kinds):
    gh = _epic({"number": 10, "title": "E2E tests"})
    out = s.cmd_place(gh, 10, "local")
    assert out["label_added"] and out["placed"] == "local" and LOCAL in gh.issues[10]["labels"]
    assert s.cmd_place(gh, 10, "local")["already"] is True
    local_kinds("e2e-test")
    back = s.cmd_place(gh, 10, "cloud")
    assert back["label_removed"] and back["still_local"] == "e2e-test"


def test_end_cloud_session_accepts_waiting_local():
    assert "waiting-local" in s.CLOUD_SESSION_OUTCOMES


def test_a_waiting_local_epic_is_not_relaunched_while_only_local_tasks_remain():
    from tests.test_cloud_epics import _ago, _ended, _initiative, _launched, _survey
    gh = _initiative({"number": 2, "comments": [_launched(minutes=300),
                                                _ended(minutes=200, outcome="waiting-local")]},
                     extra=[{"number": 5, "labels": ["type:task", LOCAL], "parent": 2,
                             "updated": _ago(10)}])
    survey = _survey(gh)
    assert survey["launchable"] == [] and "local Tasks (#5)" in survey["waiting"][0]["reason"]
    # Positive control: once the local Task closes and cloud work appears, it relaunches.
    gh.issues[5]["state"] = "CLOSED"
    gh.issues[6] = {**gh.issues[5], "number": 6, "state": "OPEN", "labels": ["type:task"]}
    assert _survey(gh)["launchable"] == [2]
