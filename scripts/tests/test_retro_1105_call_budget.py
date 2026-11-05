"""Retro 2026-11-05 (v0.4.3): network budget. A command reads only the trees it names; other
open parents keep their closed sub-issues unread; independent reads run in parallel; a sub-issue
list whose size is known needs no end-of-list peek; ids and empty comment threads cost nothing."""
import threading
import time

import sdlc_next as s
from sdlc_next import GitHubRest
from tests.test_github_rest import _issue
from tests.test_retro_1027_issue_reads import R, Runner

EPIC = {"labels": [{"name": "type:epic"}]}


def _summary(total):
    return {"total": total, "completed": 0, "percent_completed": 0}


def _world():
    """Epic 10 (one open Task, one closed); standing Epic 50 with 150 closed Tasks and one open."""
    parent = lambda n: f"https://api.github.com/{R}/issues/{n}"  # noqa: E731
    epic = _issue(10, sub_issues_summary=_summary(2), **EPIC)
    task = _issue(11, parent_issue_url=parent(10))
    standing = _issue(50, sub_issues_summary=_summary(151), **EPIC)
    open51 = _issue(51, parent_issue_url=parent(50))
    closed = [_issue(n, state="closed") for n in range(100, 250)]
    return Runner([epic, task, standing, open51],
                  subs={10: [task, _issue(12, state="closed")], 50: [open51, *closed]})


def test_a_focused_command_walks_only_its_own_tree():
    s._FOCUS.update({10})
    runner = _world()
    issues = {i["number"]: i["state"] for i in GitHubRest(runner=runner).issue_list()}
    assert issues[12] == "CLOSED" and 100 not in issues
    assert runner.gets("/sub_issues") == [f"{R}/issues/10/sub_issues?per_page=100"]
    # One open page, one end peek, one sub-issue read: no call scales with other Epics' history.
    assert len(runner.calls) == 3


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


def test_check_epics_closeable_walks_only_parents_with_no_open_child(monkeypatch):
    runner = _world()
    runner.open.append(_issue(60, sub_issues_summary=_summary(1), **EPIC))  # all children closed
    runner.subs[60] = [_issue(61, state="closed")]
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: GitHubRest(runner=runner))
    s.main(["check-epics-closeable"])
    assert runner.gets("/sub_issues") == [f"{R}/issues/60/sub_issues?per_page=100"]


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
