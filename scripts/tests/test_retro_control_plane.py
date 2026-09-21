"""Regression + positive-control tests for the control-plane retro batch: bare-Epic entry,
audit ordering, verify-only PRs, the operator commands (detach-epic, close-issue
--not-planned, comment), the worktree release hook and design-PR branch cleanup.

Each regression goes RED against the pre-fix code; its positive control stays GREEN either way."""
import json

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_sdlc_next import ScriptedRunner
from tests.test_v2_phase_tasks import FakeGh


def _epic_tree(*extra, epic_labels=("type:epic",)):
    return FakeGh([{"number": 9, "labels": list(epic_labels)}, *extra])


_PHASES = [{"number": 10, "labels": ["type:task"], "parent": 9, "title": "Architecture phase",
            "stage": "architecture"},
           {"number": 11, "labels": ["type:task"], "parent": 9, "title": "LLD phase", "stage": "lld"}]


# ---- 1: a bare Epic run asks for its phase-Tasks instead of a bare `none` ----

def test_next_action_on_a_bare_epic_without_phase_tasks_returns_cut_phase_tasks():
    result = s.decide_next_action(_epic_tree(), 9)
    assert (result["action"], result["epic"], result["unit"]) == ("cut-phase-tasks", 9, "epic")
    assert "cut-phase-tasks 9" in result["reason"] and "next-action 9" in result["reason"]


def test_next_action_on_a_half_cut_epic_also_returns_cut_phase_tasks():
    gh = _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9,
                     "title": "Architecture phase", "state": "CLOSED"})
    assert s.decide_next_action(gh, 9)["action"] == "cut-phase-tasks"


@pytest.mark.parametrize("labels,action", [
    (("type:epic", "epic:standing"), "none"),       # standing: no phase-Tasks by design
    (("type:epic", "epic:architected"), "none"),    # design phase already finished
])
def test_next_action_never_asks_a_standing_or_architected_epic_to_cut(labels, action):
    # Positive control: only a non-standing, not-yet-architected Epic is asked.
    assert s.decide_next_action(_epic_tree(epic_labels=labels), 9)["action"] == action


def test_next_action_delegates_once_the_phase_tasks_exist():
    # Positive control: with both phase-Tasks cut the Epic runs as before.
    result = s.decide_next_action(_epic_tree(*_PHASES), 9)
    assert result["action"] == "delegate" and result["issue"] == 10


def test_next_action_at_the_run_cap_defers_the_cut_like_the_initiative_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 1)
    s._write_run_state(9, {"run_id": "r", "terminal": [5]})
    assert s.decide_next_action(_epic_tree(), 9, run_id="r")["action"] == "stop-at-cap"


# ---- 1b: audit-issues lists the Epic's own gaps after its children's ----

def _audit_tree():
    return FakeGh([{"number": 9, "labels": ["type:epic"], "issue_type": "Epic", "effort": "Low"},
                   {"number": 10, "labels": ["type:task"], "parent": 9, "issue_type": "Task",
                    "priority": "Medium", "effort": "Low", "title": "Architecture phase"},
                   {"number": 11, "labels": ["type:task"], "parent": 9, "issue_type": "Task",
                    "priority": "Medium", "effort": "Low", "title": "LLD phase"}])


def test_audit_issues_orders_the_epics_own_gaps_after_its_children():
    issues = s.cmd_audit_issues(_audit_tree(), epic=9)["issues"]
    assert [i["issue"] for i in issues] == [10, 11, 9]  # 9 sorts first by number pre-fix
    assert issues[-1] == {"issue": 9, "title": "issue 9", "missing": ["Priority"],
                          "repair": "repair-issue 9", "container": True}
    assert all("container" not in i for i in issues[:2])


def test_audit_issues_still_flags_the_epic_when_it_is_the_only_gap():
    # Positive control: the Epic's own Priority gap is still reported, just not first.
    gh = _audit_tree()
    for n in (10, 11):
        gh.issues[n]["status"] = "todo"
    issues = s.cmd_audit_issues(gh, epic=9)["issues"]
    assert [i["issue"] for i in issues] == [9] and issues[0]["missing"] == ["Priority"]


# ---- 2: a verify-only Task opens its PR with --allow-empty ----

class _EmptyBranchGh:
    """pr_create refuses until the branch has a commit (as GitHub does)."""

    def __init__(self):
        self.attempts, self.commented = 0, []
        self.has_commit = False

    def pr_list_for_branch(self, branch, state="open"):
        return []

    def issue_list(self):
        return FakeGh([{"number": 9, "labels": ["type:epic"]},
                       {"number": 10, "labels": ["type:task"], "parent": 9}]).issue_list()

    def files_since(self, sha, branch):
        return []

    def pr_create(self, base, head, title, body, draft):
        self.attempts += 1
        if not self.has_commit:
            raise GhError("pull request create failed: GraphQL: No commits between "
                          f"{base} and {head} (createPullRequest)")
        return 42

    def set_stage_field(self, n, stage):
        pass

    def issue_comment(self, n, body):
        self.commented.append(body)


class _PushAwareRunner(ScriptedRunner):
    """Scripted git for the live `issue-10` worktree; a push makes GitHub see the commit."""

    def __init__(self, gh):
        super().__init__({
            ("git", "-C", ".", "worktree", "list", "--porcelain"):
                "worktree /main\nHEAD a\nbranch refs/heads/main\n\n"
                "worktree /tmp/sdlc-dev-10\nHEAD b\nbranch refs/heads/issue-10\n",
            ("git", "-C", "/tmp/sdlc-dev-10", "commit", "--allow-empty", "-m",
             "chore(#10): verify-only task, no code change"): "",
            ("git", "-C", "/tmp/sdlc-dev-10", "push", "origin", "issue-10"): "",
            ("git", "-C", "/tmp/sdlc-dev-10", "rev-parse", "HEAD"): "c0ffee1\n",
        })
        self.gh = gh

    def __call__(self, argv):
        out = super().__call__(argv)
        if argv[3:5] == ["push", "origin"]:
            self.gh.has_commit = True
        return out


def _empty_runner(gh):
    return _PushAwareRunner(gh)


def test_open_dev_pr_allow_empty_lands_an_empty_commit_then_opens_the_pr():
    gh = _EmptyBranchGh()
    runner = _empty_runner(gh)
    result = s.cmd_open_dev_pr(gh, 10, "t", "b", "s", allow_empty=True, runner=runner)
    assert result["created"] is True and result["pr"] == 42
    assert result["empty_commit"] == {"branch": "issue-10", "sha": "c0ffee1"}
    assert gh.attempts == 2
    commit = ["git", "-C", "/tmp/sdlc-dev-10", "commit", "--allow-empty", "-m",
              "chore(#10): verify-only task, no code change"]
    push = ["git", "-C", "/tmp/sdlc-dev-10", "push", "origin", "issue-10"]
    assert runner.calls.index(commit) < runner.calls.index(push)


def test_open_dev_pr_without_allow_empty_refuses_and_names_the_flag():
    gh = _EmptyBranchGh()
    runner = _empty_runner(gh)
    with pytest.raises(GhError, match="--allow-empty"):
        s.cmd_open_dev_pr(gh, 10, "t", "b", "s", runner=runner)
    assert gh.attempts == 1 and not any("commit" in c for c in runner.calls)


def test_open_dev_pr_allow_empty_changes_nothing_for_a_branch_with_commits():
    # Positive control: the flag is inert when GitHub accepts the PR outright.
    gh = _EmptyBranchGh()
    gh.has_commit = True
    runner = ScriptedRunner({})
    result = s.cmd_open_dev_pr(gh, 10, "t", "b", "s", allow_empty=True, runner=runner)
    assert result == {"issue": 10, "pr": 42, "created": True} and runner.calls == []


def test_open_dev_pr_re_raises_any_other_pr_create_failure():
    gh = _EmptyBranchGh()
    gh.pr_create = lambda **kw: (_ for _ in ()).throw(GhError("boom"))
    with pytest.raises(GhError, match="boom"):
        s.cmd_open_dev_pr(gh, 10, "t", "b", "s", allow_empty=True, runner=ScriptedRunner({}))


# ---- 3: close-issue --not-planned, detach-epic, comment ----

def _no_worktree_runner():
    runner = ScriptedRunner({})
    runner.prefix_responses = {("git", "-C", "/r", "worktree", "list"):
                               "worktree /r\nHEAD a\nbranch refs/heads/main\n"}
    return runner


def test_close_issue_not_planned_uses_the_state_reason_and_leaves_the_reason_comment():
    gh = _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"})
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner(),
                               not_planned=True, reason="superseded by #12")
    assert result["closed"] is True and result["state_reason"] == "not_planned"
    assert gh.issues[10]["state"] == "CLOSED" and gh.issues[10]["state_reason"] == "not planned"
    assert gh.issues[10]["status"] == "done" and gh.issues[10]["stage"] is None
    assert gh.comments_on(10) == ["🚫 Closed as not planned — superseded by #12."]


def test_close_issue_default_still_closes_as_completed_without_a_comment():
    # Positive control: the plain close is unchanged.
    gh = _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9})
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner())
    assert result["closed"] is True and "state_reason" not in result
    assert gh.issues[10]["state_reason"] == "completed" and gh.comments_on(10) == []


def test_close_issue_reason_needs_not_planned():
    gh = _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9})
    with pytest.raises(GhError, match="--not-planned"):
        s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner(), reason="why")
    assert gh.issues[10]["state"] == "OPEN"


def _initiative_tree():
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 10, "labels": ["type:epic"], "parent": 6},
                 {"number": 11, "labels": ["type:epic"], "parent": 6},
                 {"number": 12, "labels": ["type:epic"], "parent": 6},
                 {"number": 20, "labels": ["type:task"], "parent": 11}])
    gh.blocked = {11: [10], 12: [11]}
    gh.blocking = lambda n: [d for d, deps in gh.blocked.items() if n in deps]
    return gh


def test_detach_epic_drops_the_parent_link_and_sibling_edges_and_comments_both_sides():
    gh = _initiative_tree()
    result = s.cmd_detach_epic(gh, 11, reason="descoped")
    assert result["detached"] is True and result["initiative"] == 6
    assert result["removed_edges"] == [{"issue": 11, "blocked_by": 10},
                                       {"issue": 12, "blocked_by": 11}]
    assert gh.issues[11]["parent"] is None and gh.blocked == {11: [], 12: []}
    assert "Detached from Initiative #6 — descoped" in gh.comments_on(11)[-1]
    assert "Epic #11 detached" in gh.comments_on(6)[-1]
    assert gh.issues[20]["parent"] == 11  # the Epic's own children stay with it
    # The Initiative loop no longer sees it.
    assert s.decide_next_action(gh, 6)["epic"] == 10


@pytest.mark.parametrize("issue", [20, 6])
def test_detach_epic_refuses_anything_but_an_epic_under_an_initiative(issue):
    gh = _initiative_tree()
    result = s.cmd_detach_epic(gh, issue)
    assert result["refused"] is True and result["detached"] is False
    assert gh.issues[issue]["parent"] == (11 if issue == 20 else None)


def test_detach_epic_leaves_a_bare_epic_alone():
    # Positive control: an Epic with no Initiative has nothing to detach and is untouched.
    gh = _epic_tree()
    assert s.cmd_detach_epic(gh, 9)["refused"] is True and gh.comments_on(9) == []


def test_comment_posts_a_plain_comment_from_body_or_body_file(tmp_path):
    gh = _epic_tree()
    assert s.cmd_comment(gh, 9, body="  dropped: superseded  ") == {
        "issue": 9, "commented": True, "chars": 19}
    f = tmp_path / "c.md"
    f.write_text("from a file\n")
    s.cmd_comment(gh, 9, body_file=str(f))
    assert gh.comments_on(9) == ["dropped: superseded", "from a file"]


@pytest.mark.parametrize("kwargs", [{}, {"body": "x", "body_file": "/f"}, {"body": "   "}])
def test_comment_refuses_no_text_both_sources_or_an_empty_body(kwargs):
    gh = _epic_tree()
    with pytest.raises(GhError):
        s.cmd_comment(gh, 9, **kwargs)
    assert gh.comments_on(9) == []


def test_comment_refuses_a_body_file_over_the_handoff_cap(tmp_path):
    f = tmp_path / "c.md"
    f.write_text("x" * (s.HANDOFF_CAP + 1))
    gh = _epic_tree()
    result = s.cmd_comment(gh, 9, body_file=str(f))
    assert result["refused"] is True and gh.comments_on(9) == []


# ---- 6a: pipeline.worktrees.releaseCommand runs inside the tree before removal ----

def _release_runner():
    return ScriptedRunner({
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD a\nbranch refs/heads/main\n\n"
            "worktree /tmp/sdlc-dev-186\nHEAD b\nbranch refs/heads/issue-186\n",
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain"): "",
        ("git", "-C", "/tmp/sdlc-dev-186", "log", "--oneline", "origin/issue-186..issue-186"): "",
        ("git", "-C", "/repo", "worktree", "remove", "--force", "/tmp/sdlc-dev-186"): "",
    })


def test_release_worktree_runs_the_release_command_in_the_tree_before_removing_it(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "releaseCommand", "make it-down")
    runner, order = _release_runner(), []

    def shell(command, cwd):
        order.append(("shell", command, cwd, len(runner.calls)))
        return ""
    result = s.release_worktree("issue-186", runner=runner, base_repo="/repo", shell=shell)
    assert result == {"released": True, "path": "/tmp/sdlc-dev-186",
                      "release_command": {"command": "make it-down", "ok": True}}
    assert order == [("shell", "make it-down", "/tmp/sdlc-dev-186", 4)]
    assert runner.calls[-1] == ["git", "-C", "/repo", "worktree", "remove", "--force",
                                "/tmp/sdlc-dev-186"]


def test_a_failing_release_command_is_reported_and_the_worktree_is_still_removed(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "releaseCommand", "make it-down")
    runner = _release_runner()

    def shell(command, cwd):
        raise GhError("command failed (2) in /tmp/sdlc-dev-186: make it-down")
    result = s.release_worktree("issue-186", runner=runner, base_repo="/repo", shell=shell)
    assert result["released"] is True
    assert result["release_command"]["ok"] is False
    assert "make it-down" in result["release_command"]["error"]
    assert runner.calls[-1][3:5] == ["worktree", "remove"]


def test_release_worktree_without_a_release_command_is_unchanged(monkeypatch):
    # Positive control: no command configured -> no shell call, no extra key.
    monkeypatch.setitem(s.PIPELINE["worktrees"], "releaseCommand", "")
    runner = _release_runner()

    def shell(command, cwd):
        raise AssertionError("must not run")
    assert s.release_worktree("issue-186", runner=runner, base_repo="/repo", shell=shell) == {
        "released": True, "path": "/tmp/sdlc-dev-186"}


def test_the_release_command_never_runs_on_a_tree_that_is_not_removed(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "releaseCommand", "make it-down")
    runner = _release_runner()
    runner.responses[("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain")] = " M a.py\n"

    def shell(command, cwd):
        raise AssertionError("must not run on a dirty tree")
    result = s.release_worktree("issue-186", runner=runner, base_repo="/repo", shell=shell)
    assert result["released"] is False and result["reason"] == "uncommitted changes"


def test_show_config_reports_the_release_command_default():
    assert s.PIPELINE["worktrees"]["releaseCommand"] == ""


# ---- 6b: closing a Task deletes its merged design PR's branch ----

def _design_task(pr_state):
    gh = _epic_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                     "comments": ["📄 Design PR #50 opened.\n\n"
                                  "<!-- design-pr-opened: lld:50 @ 2026-09-20T00:00:00Z -->"]},
                    )
    gh.prs[50] = {"state": pr_state, "headRefName": "issue-11", "baseRefName": "epic-9",
                  "body": "", "comments": []}
    return gh


def test_close_issue_deletes_the_merged_design_pr_branch_on_origin():
    gh = _design_task("MERGED")
    result = s.cmd_close_issue(gh, 11, repo_path="/r", runner=_no_worktree_runner())
    assert result["design_pr_branch"] == {"pr": 50, "deleted": True, "branch": "issue-11"}
    assert gh.deleted_branches == ["issue-11"]


def test_close_issue_keeps_the_branch_of_an_unmerged_design_pr():
    # Positive control: a still-open design PR keeps its branch (the human may merge it).
    gh = _design_task("OPEN")
    result = s.cmd_close_issue(gh, 11, repo_path="/r", runner=_no_worktree_runner())
    assert result["design_pr_branch"]["deleted"] is False
    assert not getattr(gh, "deleted_branches", [])


def test_close_issue_without_a_design_pr_reports_nothing_about_branches():
    gh = _epic_tree({"number": 11, "labels": ["type:task"], "parent": 9})
    result = s.cmd_close_issue(gh, 11, repo_path="/r", runner=_no_worktree_runner())
    assert "design_pr_branch" not in result and result["closed"] is True


def test_a_failed_branch_delete_never_fails_the_close():
    gh = _design_task("MERGED")
    gh.delete_branch = lambda b: (_ for _ in ()).throw(GhError("422 ref does not exist"))
    result = s.cmd_close_issue(gh, 11, repo_path="/r", runner=_no_worktree_runner())
    assert result["closed"] is True and gh.issues[11]["state"] == "CLOSED"
    assert result["design_pr_branch"]["deleted"] is False
    assert "422" in result["design_pr_branch"]["reason"]


# ---- CLI wiring ----

@pytest.mark.parametrize("argv,fn,expected_args,expected_kwargs", [
    (["detach-epic", "9", "--reason", "r"], "cmd_detach_epic", (9, "r"), {}),
    (["comment", "5", "--body", "hi"], "cmd_comment", (5, "hi", None), {}),
    (["comment", "5", "--body-file", "/f"], "cmd_comment", (5, None, "/f"), {}),
    (["close-issue", "5", "--not-planned", "--reason", "r"], "cmd_close_issue", (5,),
     {"repo_path": None, "not_planned": True, "reason": "r"}),
    (["open-dev-pr", "5", "--title", "t", "--body", "b", "--summary", "s", "--allow-empty"],
     "cmd_open_dev_pr", (5, "t", "b", "s"), {"allow_empty": True, "repo_path": None}),
])
def test_cli_wires_the_new_commands_and_flags(monkeypatch, capsys, argv, fn, expected_args,
                                              expected_kwargs):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, fn, lambda gh, *a, **k: seen.append((gh, a, k)) or {})
    assert s.main(argv) == 0
    assert seen == [("GH", expected_args, expected_kwargs)]
    assert json.loads(capsys.readouterr().out) == {}


def test_cli_comment_needs_exactly_one_text_source(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    with pytest.raises(SystemExit):
        s.main(["comment", "5"])
    with pytest.raises(SystemExit):
        s.main(["comment", "5", "--body", "x", "--body-file", "/f"])
