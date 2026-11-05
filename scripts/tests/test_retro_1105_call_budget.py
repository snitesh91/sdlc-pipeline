"""Retro 2026-11-05 (v0.4.3): network budget. A command reads only the trees it names; other
open parents keep their closed sub-issues unread; independent reads run in parallel; a sub-issue
list whose size is known needs no end-of-list peek; ids and empty comment threads cost nothing."""
import json
import threading
import time

import pytest

import sdlc_next as s
from sdlc_next import GitHubRest
from tests.test_github_rest import _issue
from tests.test_retro_1027_issue_reads import R, Runner

EPIC = {"labels": [{"name": "type:epic"}]}


def _summary(total, completed=0):
    return {"total": total, "completed": completed, "percent_completed": 0}


def _world():
    """Epic 10 (one open Task, one closed); standing Epic 50 with 150 closed Tasks and one open."""
    parent = lambda n: f"https://api.github.com/{R}/issues/{n}"  # noqa: E731
    epic = _issue(10, sub_issues_summary=_summary(2), **EPIC)
    task = _issue(11, parent_issue_url=parent(10))
    standing = _issue(50, sub_issues_summary=_summary(151), **EPIC)
    open51 = _issue(51, parent_issue_url=parent(50))
    closed = [_issue(n, state="closed") for n in range(100, 250)]
    opened = [epic, task, standing, open51]
    return Runner(opened, issues={i["number"]: i for i in opened},
                  subs={10: [task, _issue(12, state="closed")], 50: [open51, *closed]})


def test_a_focused_command_reads_only_its_own_tree_and_lists_nothing():
    s._FOCUS.update({11})  # a Task: up to its Epic, then the Epic's whole tree down
    runner = _world()
    issues = {i["number"]: i["state"] for i in GitHubRest(runner=runner).issue_list()}
    assert issues == {10: "OPEN", 11: "OPEN", 12: "CLOSED"}
    assert runner.gets("state=open") == []
    assert runner.gets("/sub_issues") == [f"{R}/issues/10/sub_issues?per_page=100"]
    # The Task, its Epic, the Epic's sub-issues: nothing scales with other Epics or the repo.
    assert len(runner.calls) == 3


def test_a_later_focus_adds_only_its_tree():
    s._FOCUS.update({11})
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    before = len(runner.calls)
    assert 51 in {i["number"] for i in s.focus_issues(gh, {51})}
    assert runner.gets("state=open") == [] and len(runner.calls) == before + 3  # 51, 50, 50's subs


def test_an_unfocused_command_still_sees_every_open_parents_closed_children():
    """Positive control: check-epics-closeable and audits (no number) keep the full view."""
    runner = _world()
    issues = {i["number"] for i in GitHubRest(runner=runner).issue_list()}
    assert {12, 100, 249} <= issues
    assert sorted(runner.gets("/sub_issues")) == [f"{R}/issues/10/sub_issues?per_page=100",
                                                  f"{R}/issues/50/sub_issues?per_page=100"]


def test_a_known_size_sub_issue_list_takes_no_end_peek():
    runner = _world()
    GitHubRest(runner=runner).issue_list()
    # Issue 10's list is short (2 of 100) yet complete by its summary: one call, no peek.
    assert [p for m, path, p in runner.calls if path.endswith("/10/sub_issues?per_page=100")] == [1]


def test_sub_issue_walks_run_in_parallel():
    live, peak, lock = [0], [0], threading.Lock()
    base = _world()

    def runner(argv):
        if any("/sub_issues" in a for a in argv):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            try:
                return base(argv)
            finally:
                with lock:
                    live[0] -= 1
        return base(argv)
    GitHubRest(runner=runner).issue_list()
    assert peak[0] == 2


def test_writes_reuse_ids_already_read():
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh.add_blocked_by(11, 12)
    gh.add_sub_issue(10, 11)
    assert runner.single_gets() == []
    # Control: an issue this process never read is fetched for its id.
    gh.add_blocked_by(11, 777)
    assert runner.single_gets() == [f"{R}/issues/777"]


def test_issue_view_skips_the_comment_read_when_the_issue_has_none():
    runner = Runner(issues={7: _issue(7, comments=0), 8: _issue(8, comments=2)})
    gh = GitHubRest(runner=runner)
    assert gh.issue_view(7)["comments"] == []
    gh.issue_view(8)
    assert runner.gets("/comments") == [f"{R}/issues/8/comments?per_page=100"]


def test_an_issue_is_read_once_until_a_write():
    runner = Runner(issues={7: _issue(7)})
    gh = GitHubRest(runner=runner)
    gh.issue_fields(7)
    gh.issue_epic_info(7)
    assert runner.single_gets() == [f"{R}/issues/7"]
    gh.issue_close(7)
    gh.issue_fields(7)
    assert runner.single_gets() == [f"{R}/issues/7"] * 2


def test_list_needs_human_reads_no_closed_sub_issues(monkeypatch):
    runner = _world()
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: GitHubRest(runner=runner))
    s.main(["list-needs-human"])
    assert runner.gets("/sub_issues") == []


def test_check_epics_closeable_lists_epics_and_walks_only_finished_ones(monkeypatch):
    runner = _world()
    runner.open.append(_issue(60, sub_issues_summary=_summary(1, 1), **EPIC))  # all closed
    runner.subs[60] = [_issue(61, state="closed")]
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: GitHubRest(runner=runner))
    s.main(["check-epics-closeable"])
    assert runner.gets("/sub_issues") == [f"{R}/issues/60/sub_issues?per_page=100"]
    assert all("labels=type%3Aepic" in p for p in runner.gets("state=open"))


def test_branch_heads_come_from_one_shared_ls_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_FETCH_DEDUPE", "on")
    s._REMOTE_HEADS.clear()
    calls = []

    def runner(argv):
        calls.append(argv)
        return "aaa\trefs/heads/issue-1\nbbb\trefs/heads/issue-2\n"
    runner.real = True
    assert s._remote_head(str(tmp_path), "issue-2", runner) == "bbb"
    assert s._remote_head(str(tmp_path), "issue-9", runner) is None
    assert len(calls) == 1
    s._PUSH_GENERATION[0] += 1  # a write may have moved origin: list again
    s._remote_head(str(tmp_path), "issue-1", runner)
    assert len(calls) == 2


def test_prs_for_a_branch_are_read_once_until_a_write():
    runner = Runner()
    gh = GitHubRest(runner=runner)
    gh.pr_list_for_branch("issue-9")
    gh.pr_list_for_branch("issue-9")
    assert len([c for c in runner.calls if "pulls?head=" in c[1]]) == 1
    gh.issue_comment(9, "x")
    gh.pr_list_for_branch("issue-9")
    assert len([c for c in runner.calls if "pulls?head=" in c[1]]) == 2


@pytest.mark.parametrize("argv", [
    ["next-action", "10", "--run-id", "r"], ["list-parallel-ready", "10"],
    ["list-design-ready", "10"], ["list-ready-for-review", "10"],
    ["audit-issues", "--epic", "10"], ["cloud-status", "10"], ["close-epic", "10"],
    ["prune-stale", "--dry-run"]])
def test_a_numbered_command_never_lists_open_issues(monkeypatch, argv):
    """Read-only commands (close-epic refuses on the open child before any write)."""
    runner = _world()
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: GitHubRest(runner=runner))
    try:
        s.main(argv)
    except Exception:  # noqa: BLE001 -- the fake answers little; only the reads matter here
        pass
    assert runner.single_gets() or runner.gets("/sub_issues")  # it did read the unit
    assert [p for p in runner.gets("/issues?state=") if "labels=" not in p] == []
    assert not any("/50/sub_issues" in p for p in runner.gets("/sub_issues"))


def test_the_gate_scan_reads_open_gate_prs_units_only():
    runner = _world()
    runner.issues[70] = _issue(70)
    real = runner.__call__

    def call(argv):
        if any(a.startswith(f"{R}/pulls?state=open") for a in argv):
            return json.dumps([{"head": {"ref": "issue-70"}, "labels": [{"name": "sdlc:gate"}]},
                               {"head": {"ref": "issue-71"}, "labels": []}])
        return real(argv)
    s._FOCUS.update({11})
    gh = GitHubRest(runner=call)
    assert 70 in {i["number"] for i in s.with_scan(gh, gh.issue_list(), "gate")}
    assert f"{R}/issues/71" not in runner.single_gets()
