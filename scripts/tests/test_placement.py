"""Cloud/local placement: which session may drive an Epic, the guard on the driving
commands, `place`, and the branch-cleanup `warnings` a failed delete surfaces."""
import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh, _git, _worktree_with_doc, repo  # noqa: F401

CLOUD = "sdlc:cloud"


class PlaceGh(FakeGh):
    def ensure_label(self, label):
        self.created_labels = [*getattr(self, "created_labels", []), label]


def _tree(initiative_labels=(), epic_labels=(), task=True, **task_kw):
    """Initiative #6 > Epic #9 > Task #10."""
    return PlaceGh([
        {"number": 6, "labels": ["type:initiative", *initiative_labels]},
        {"number": 9, "labels": ["type:epic", *epic_labels], "parent": 6},
        *([{"number": 10, "labels": ["type:task"], "parent": 9, **task_kw}] if task else []),
    ])


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")


def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


def _must_not_run(name):
    def fail(*_a, **_k):
        raise AssertionError(f"{name} ran despite a placement mismatch")
    return fail


# --- session placement --------------------------------------------------------------

@pytest.mark.parametrize("env, expected", [
    ({}, ("local", "default")),
    ({"CLAUDE_CODE_REMOTE": "true"}, ("cloud", "CLAUDE_CODE_REMOTE")),
    ({"CLAUDE_CODE_REMOTE": "1"}, ("cloud", "CLAUDE_CODE_REMOTE")),
    ({"CLAUDE_CODE_REMOTE": "false"}, ("local", "default")),
    ({"SDLC_PLACEMENT": "cloud"}, ("cloud", "SDLC_PLACEMENT")),
    ({"SDLC_PLACEMENT": "Local", "CLAUDE_CODE_REMOTE": "true"}, ("local", "SDLC_PLACEMENT")),
])
def test_session_placement_prefers_the_explicit_override_then_the_remote_flag(
        monkeypatch, env, expected):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    got = s.session_placement()
    assert (got["placement"], got["signal"]) == expected


def test_an_invalid_sdlc_placement_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setenv("SDLC_PLACEMENT", "laptop")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    with pytest.raises(s.GhError, match="SDLC_PLACEMENT='laptop'"):
        s.session_placement()


# --- unit placement -------------------------------------------------------------------

def _placement(gh, n):
    return s.unit_placement(n, s._issue_info_lookup(gh))


def test_an_epic_is_cloud_placed_by_its_own_label_or_its_initiatives():
    assert _placement(_tree(), 9) == {"placement": "local", "epic": 9}
    assert _placement(_tree(epic_labels=[CLOUD]), 9) == {"placement": "cloud", "epic": 9,
                                                         "label_on": 9}
    assert _placement(_tree(initiative_labels=[CLOUD]), 9) == {"placement": "cloud", "epic": 9,
                                                               "label_on": 6}


def test_a_task_takes_its_epics_placement_inherited_from_the_initiative():
    assert _placement(_tree(initiative_labels=[CLOUD]), 10)["epic"] == 9
    assert _placement(_tree(epic_labels=[CLOUD]), 10) == {"placement": "cloud", "epic": 9,
                                                          "label_on": 9}
    assert _placement(_tree(), 10) == {"placement": "local", "epic": 9}


def test_a_tasks_own_label_does_not_place_it():
    gh = _tree()
    gh.issues[10]["labels"].append(CLOUD)
    assert _placement(gh, 10) == {"placement": "local", "epic": 9}


def test_an_initiative_reads_only_its_own_label():
    gh = _tree(epic_labels=[CLOUD])
    assert _placement(gh, 6) == {"placement": "local", "epic": 6}
    assert _placement(_tree(initiative_labels=[CLOUD]), 6) == {"placement": "cloud", "epic": 6,
                                                               "label_on": 6}


# --- the guard --------------------------------------------------------------------------

def _next_action(gh, epic, **kw):
    return s.cmd_next_action(gh, argparse.Namespace(epic=epic, run_id=None, repo_path=".",
                                                    skip_epic=[], sync_epic=False, **kw))


def test_next_action_refuses_a_cloud_epic_in_a_local_session():
    gh = _tree(epic_labels=[CLOUD], task=False)
    with pytest.raises(s.PlacementMismatch) as exc:
        _next_action(gh, 9)
    payload = exc.value.payload
    assert payload["error"] == "placement_mismatch"
    assert (payload["session_placement"], payload["signal"], payload["unit_placement"],
            payload["epic"]) == ("local", "default", "cloud", 9)
    assert "place 9 --where local" in payload["hint"]


def test_next_action_refuses_a_local_epic_in_a_cloud_session_before_syncing(cloud, monkeypatch):
    gh = _tree(task=False)
    monkeypatch.setattr(s, "sync_epic_if_due", _must_not_run("sync-epic"))
    with pytest.raises(s.PlacementMismatch) as exc:
        s.cmd_next_action(gh, argparse.Namespace(epic=9, run_id="r", repo_path=".",
                                                 skip_epic=[], sync_epic=True))
    assert exc.value.payload["unit_placement"] == "local"
    assert "place 9 --where cloud" in exc.value.payload["hint"]


def test_next_action_runs_a_cloud_epic_in_a_cloud_session(cloud):
    gh = _tree(initiative_labels=[CLOUD], task=False)
    assert _next_action(gh, 9)["action"] == "cut-phase-tasks"


def test_next_action_runs_a_local_epic_in_a_local_session():
    assert _next_action(_tree(task=False), 9)["action"] == "cut-phase-tasks"


def test_next_action_cli_prints_the_refusal_and_exits_nonzero(monkeypatch):
    gh = _tree(epic_labels=[CLOUD], task=False)
    code, out = _cli(monkeypatch, gh, ["next-action", "9"])
    assert code == 1
    assert out["error"] == "placement_mismatch" and out["epic"] == 9
    assert set(out) == {"error", "session_placement", "signal", "unit_placement", "epic", "hint"}


def test_an_initiative_loop_in_a_local_session_skips_its_cloud_placed_epics():
    gh = PlaceGh([{"number": 6, "labels": ["type:initiative"]},
                  {"number": 8, "labels": ["type:epic", CLOUD], "parent": 6},
                  {"number": 9, "labels": ["type:epic"], "parent": 6}])
    step = s._initiative_epic_step(gh, gh.issue_list(), 6, [])
    assert step["epic"] == 9
    reason = s._open_epics_reason(gh, [i for i in gh.issue_list() if i["number"] == 8], [])
    assert "placed in the cloud" in reason


def test_start_stage_refuses_a_mismatched_task_before_any_worktree_or_claim(monkeypatch):
    gh = _tree(initiative_labels=[CLOUD], stage="development")
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))
    with pytest.raises(s.PlacementMismatch) as exc:
        s.cmd_start_stage(gh, 10, "development")
    assert exc.value.payload["epic"] == 9
    # The label sits on the Initiative, so that is what the hint says to move.
    assert "place 6 --where local" in exc.value.payload["hint"]
    assert gh.issues[10]["status"] is None and gh.comments_on(10) == []
    code, out = _cli(monkeypatch, gh, ["start-stage", "10", "--role", "development"])
    assert (code, out["error"]) == (1, "placement_mismatch")


def test_start_stage_claims_when_the_placements_agree(cloud, monkeypatch):
    gh = _tree(epic_labels=[CLOUD], stage="development")
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})
    result = s.cmd_start_stage(gh, 10, "development")
    assert result["ok"] is True and result["claimed"] is True


def test_start_stage_unit_epic_is_guarded_on_the_epic_itself(monkeypatch):
    gh = _tree(epic_labels=[CLOUD])
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))
    with pytest.raises(s.PlacementMismatch):
        s.cmd_start_stage(gh, 9, "architecture", unit="epic")


@pytest.mark.parametrize("call", [
    lambda gh: s.cmd_list_parallel_ready(gh, ".", 9),
    lambda gh: s.cmd_list_design_ready(gh, ".", 9),
    lambda gh: s.cmd_list_ready_for_review(gh, 9),
])
def test_the_listing_commands_refuse_a_mismatched_epic(call, cloud):
    with pytest.raises(s.PlacementMismatch):
        call(_tree())
    # Positive control: a cloud-placed Epic lists in a cloud session.
    assert s.cmd_list_ready_for_review(_tree(epic_labels=[CLOUD]), 9)["count"] == 0


def test_show_config_reports_the_session_placement_and_the_defaults(monkeypatch):
    monkeypatch.setattr(s, "plugin_version_info", lambda runner=None: {})
    out = s.cmd_show_config()
    assert out["session_placement"] == {"placement": "local", "signal": "default"}
    assert (out["placement"]["cloudLabel"], out["placement"]["cloudEnvironment"]) == (CLOUD, "")
    monkeypatch.setenv("SDLC_PLACEMENT", "moon")
    assert "error" in s.cmd_show_config()["session_placement"]


# --- place ------------------------------------------------------------------------------

def test_place_adds_the_label_creating_it_and_is_idempotent(repo):
    gh = _tree()
    first = s.cmd_place(gh, 9, "cloud", repo_path=str(repo))
    assert (first["placed"], first["label_added"], first["worktrees"]) == ("cloud", True, [])
    assert CLOUD in gh.issues[9]["labels"] and gh.created_labels == [CLOUD]
    again = s.cmd_place(gh, 9, "cloud", repo_path=str(repo))
    assert again["already"] is True and again["label_added"] is False
    assert gh.created_labels == [CLOUD]


def test_place_local_removes_the_label_and_is_idempotent(repo):
    gh = _tree(epic_labels=[CLOUD])
    first = s.cmd_place(gh, 9, "local", repo_path=str(repo))
    assert (first["placed"], first["label_removed"]) == ("local", True)
    assert CLOUD not in gh.issues[9]["labels"]
    assert s.cmd_place(gh, 9, "local", repo_path=str(repo))["already"] is True


def test_place_refuses_while_a_unit_holds_a_local_worktree_unless_forced(repo):
    gh = _tree(stage="development")
    wt = s.cmd_worktree_add(gh, 10, repo_path=str(repo), base="origin/main")["path"]

    refused = s.cmd_place(gh, 9, "cloud", repo_path=str(repo))
    assert refused["refused"] is True and refused["placed"] == "local"
    assert [w["issue"] for w in refused["worktrees"]] == [10]
    assert "invisible to the other side" in refused["reason"] and wt in refused["reason"]
    assert CLOUD not in gh.issues[9]["labels"]
    # The Initiative covers the same units.
    assert s.cmd_place(gh, 6, "cloud", repo_path=str(repo))["refused"] is True

    forced = s.cmd_place(gh, 9, "cloud", force=True, repo_path=str(repo))
    assert forced["forced"] is True and forced["label_added"] is True
    assert CLOUD in gh.issues[9]["labels"]


def test_place_ignores_worktrees_of_other_epics(repo):
    gh = PlaceGh([{"number": 9, "labels": ["type:epic"]},
                  {"number": 12, "labels": ["type:task"], "parent": 30},
                  {"number": 30, "labels": ["type:epic"]}])
    s.cmd_worktree_add(gh, 12, repo_path=str(repo), base="origin/main")
    result = s.cmd_place(gh, 9, "cloud", repo_path=str(repo))
    assert result["label_added"] is True and result["worktrees"] == []


def test_place_local_refuses_an_epic_whose_initiative_is_cloud(repo):
    gh = _tree(initiative_labels=[CLOUD])
    result = s.cmd_place(gh, 9, "local", repo_path=str(repo))
    assert result["refused"] is True and result["inherited_from"] == 6
    assert "place 6 --where local" in result["reason"]


def test_place_refuses_a_task():
    with pytest.raises(s.GhError, match="not an Epic or Initiative"):
        s.cmd_place(_tree(), 10, "cloud")


# --- branch cleanup warnings ---------------------------------------------------------------

def _real_task(repo):
    gh = PlaceGh([{"number": 5, "labels": ["type:task"], "stage": "pr-review"}])
    gh.repo = str(repo)
    gh._run = s._default_runner
    return gh


def _merged(gh, pr=70):
    gh.prs[pr] = {"headRefName": "issue-5", "baseRefName": "main", "state": "OPEN",
                  "body": "", "comments": []}
    gh.pr_merge(pr)
    return pr


def test_a_failed_origin_delete_surfaces_a_warning(repo, monkeypatch):
    gh = _real_task(repo)
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    pr = _merged(gh)

    def refuse(branch):
        raise s.GhError("HTTP 403: Resource not accessible\nmore detail")
    monkeypatch.setattr(gh, "delete_branch", refuse)

    result = s.cmd_merge_pr(gh, pr, 5, repo_path=str(repo))

    assert result["cleanup"]["remote_branch"]["error"] is True
    assert result["warnings"] == [
        "origin/issue-5 could not be deleted (HTTP 403: Resource not accessible) -- the "
        "operator deletes it on GitHub"]


def test_an_already_absent_origin_branch_is_no_warning(repo):
    gh = _real_task(repo)
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _merged(gh)
    _git("push", "-q", "origin", "--delete", "issue-5", cwd=repo)

    result = s.cmd_close_issue(gh, 5, repo_path=str(repo))

    assert result["cleanup"]["remote_branch"].get("absent") is True
    assert "warnings" not in result


def test_a_branch_kept_for_its_open_pr_is_no_warning(repo):
    gh = _real_task(repo)
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    gh.prs[71] = {"headRefName": "issue-5", "state": "OPEN"}

    result = s.cmd_close_issue(gh, 5, repo_path=str(repo))

    assert "still open" in result["cleanup"]["remote_branch"]["reason"]
    assert "warnings" not in result


def test_cleanup_warnings_cover_failed_local_deletes_but_not_intentional_keeps():
    failed = {"branch": "issue-5", "remote_branch": {"deleted": True},
              "local_branch": {"deleted": False, "retained_local_branch": True, "error": True,
                               "reason": "error: cannot lock ref"}}
    kept = {"branch": "issue-6", "remote_branch": {"deleted": False, "reason": "PR #1 is open"},
            "local_branch": {"deleted": False, "retained_local_branch": True,
                             "reason": "checked out in a worktree"}}
    assert s.cleanup_warnings(failed) == [
        "local branch issue-5 could not be deleted (error: cannot lock ref) -- "
        "the operator runs `git branch -D issue-5`"]
    assert s.cleanup_warnings(kept) == []
    assert s._with_warnings({"x": 1}, kept) == {"x": 1}
    assert s._with_warnings({"x": 1}, kept, failed)["warnings"] == s.cleanup_warnings(failed)


def test_a_composite_lifts_its_steps_warnings_to_the_top_level():
    seq = s.StepSequence()
    seq.run("merge-pr", lambda: {"merged": True, "warnings": ["origin/issue-5 ..."]})
    seq.run("other", lambda: {"ok": True})
    assert seq.report()["warnings"] == ["origin/issue-5 ..."]
    quiet = s.StepSequence()
    quiet.run("merge-pr", lambda: {"merged": True})
    assert "warnings" not in quiet.report()
