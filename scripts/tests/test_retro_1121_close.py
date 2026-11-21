"""Retro 2026-11-21 (epic close): close-epic installs nothing, counts a real deflake run,
goes on after a reconcile that keeps the evidence, takes an operator override and prints
brief by default; land-epic reads only the landing PR; REST reads retry a transient 5xx."""
import json

import pytest

import sdlc_next as s
from tests.test_epic_close_evidence import _epic, _marks, _ready_epic
from tests.test_retro_1012_a import _Ws, _behind_epic
from tests.test_retro_1109_deflake import HEAD, RED_RUN, SPEC, WF, _gh, _marker, _red
from tests.test_retro_1109_standing import _rtb


# --- close-epic never runs epicClose.prepare -------------------------------------------

def test_close_epic_never_runs_prepare_and_prepare_epic_tree_does(monkeypatch):
    ran = []
    tree = {"path": "/wt", "synced_to_origin": True}
    monkeypatch.setitem(s.PIPELINE["epicClose"], "prepare", "npm ci")
    monkeypatch.setattr(s, "_refresh_epic_worktree", lambda *a, **k: dict(tree))
    shell = lambda command, cwd: ran.append((command, cwd)) or "ok"
    result = s.cmd_close_epic(_epic([]), 9, shell=shell)
    assert result["missing_verification"] and "prepare" not in result and not ran
    assert "prepare-epic-tree 9" in result["next"]
    # Positive control: the separate command installs.
    out = s.cmd_prepare_epic_tree(9, shell=shell)
    assert out["prepare"]["ok"] is True and ran == [("npm ci", "/wt")]


# --- a deflake run named by `run-name` still counts -------------------------------------

def _dispatched(path=RED_RUN["path"]):
    title = f"deflake: {SPEC}"
    return {"name": title, "display_title": title, "conclusion": "success", "html_url": "u",
            "event": "workflow_dispatch", "path": path}


def test_a_deflake_run_whose_name_is_its_run_name_counts_the_suite_green():
    gh, _ = _gh([_dispatched()])
    checks = s.apply_deflakes(gh, _red(), [_marker()], HEAD)
    assert s.workflow_check_state(checks, WF) == "passing"


def test_a_deflake_run_of_another_workflow_file_does_not_count():
    """Positive control."""
    gh, _ = _gh([_dispatched(path=".github/workflows/other-it.yml")])
    checks = s.apply_deflakes(gh, _red(), [_marker()], HEAD)
    assert s.workflow_check_state(checks, WF) == "failing"


# --- a reconcile that keeps the evidence goes on to the merge gate ----------------------

@pytest.fixture
def reconcile(monkeypatch):
    monkeypatch.setattr(s, "BranchWorkspace", _Ws)
    monkeypatch.setattr(s, "git_reconcile_branch", lambda *a, **k: None)
    monkeypatch.setattr(s, "_refresh_epic_worktree", lambda *a, **k: None)


def test_close_epic_merges_in_the_call_that_reconciled_when_the_evidence_holds(reconcile):
    result = s.cmd_close_epic(_behind_epic(["backend/b.ts"]), 9)
    assert result["merged"] is True and result["base_moved"] is True
    assert result["reconciled"] == 2


def test_close_epic_stops_after_a_reconcile_that_stales_the_evidence(reconcile):
    """Positive control: the sibling's commits touched what the pass tested."""
    gh = _behind_epic(["backend/a.ts"])
    gh.files_since = lambda sha, branch: ["backend/a.ts"]
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False and result["base_moved"] is True
    assert result["missing_verification"] and "exploratory pass" in result["reason"]


# --- operator override ------------------------------------------------------------------

def test_operator_override_merges_past_unsatisfied_suites_and_records_the_reason():
    gh = _ready_epic([_marks()[1]], checks=[], attested=False)
    assert s.cmd_close_epic(gh, 9)["checks"] == "missing-checks" and not gh.merged
    result = s.cmd_close_epic(gh, 9, operator_override="IT flake on devices-race")
    assert result["merged"] is True and gh.merged == [38]
    assert result["operator_override"]["skipped"]
    assert any("Operator override" in c and "devices-race" in c for c in gh.comments_on(9))


def test_operator_override_never_passes_missing_verification_or_an_empty_reason():
    """Positive control."""
    gh = _ready_epic([], checks=[], attested=False)
    result = s.cmd_close_epic(gh, 9, operator_override="flake")
    assert result["merged"] is False and result["missing_verification"] and not gh.merged
    with pytest.raises(s.GhError, match="--reason"):
        s.cmd_close_epic(gh, 9, operator_override=" ")


# --- brief is the default output --------------------------------------------------------

def test_close_epic_prints_counts_unless_full(monkeypatch, capsys):
    monkeypatch.setattr(s, "get_work_item_provider", lambda: None)
    monkeypatch.setattr(s, "cmd_close_epic",
                        lambda *a, **k: {"epic": 7, "carried_forward_files": ["a", "b"]})
    s.main(["close-epic", "7"])
    assert json.loads(capsys.readouterr().out)["carried_forward_files"] == 2
    s.main(["close-epic", "7", "--full"])
    assert json.loads(capsys.readouterr().out)["carried_forward_files"] == ["a", "b"]


# --- land-epic reads only the landing PR ------------------------------------------------

def _landing(checks):
    gh, calls = _rtb()
    branches = []
    gh.pr_list_for_branch = lambda branch, state="open": branches.append(branch) or []
    gh.pr_files = lambda n: ["backend/a.ts"]
    gh.pr_checks = lambda n: list(checks)
    return gh, calls, branches


def test_land_epic_never_reads_the_merged_children_prs():
    gh, calls, branches = _landing([])
    out = s.cmd_land_epic(gh, 30, runner=lambda argv: "")
    assert out["landed"] is False and out["awaiting_checks"] == ["Backend CI"]
    assert branches == ["epic-30"] and not calls["merged"]


def test_land_epic_lands_once_the_landing_pr_checks_pass():
    """Positive control."""
    gh, calls, _ = _landing([{"name": "ci", "bucket": "pass", "workflow": "Backend CI"}])
    assert s.cmd_land_epic(gh, 30, runner=lambda argv: "")["landed"] is True
    assert calls["merged"] == [(77, "merge")]


# --- REST reads retry a transient 5xx ---------------------------------------------------

def _flaky(error, fails=1):
    calls = []

    def runner(argv):
        calls.append(argv)
        if len(calls) <= fails:
            raise s.GhError(f"command failed (1): gh api x\ngh: {error}")
        return '{"ok": true}'
    return s.GitHubRest(repo="owner/repo", runner=runner), calls


def test_a_rest_read_retries_a_transient_503(monkeypatch):
    monkeypatch.setattr(s.time, "sleep", lambda n: None)
    gh, calls = _flaky("HTTP 503")
    assert gh._api_json("repos/owner/repo/pulls/1") == {"ok": True} and len(calls) == 2


def test_a_write_or_a_404_is_never_retried(monkeypatch):
    """Positive control."""
    monkeypatch.setattr(s.time, "sleep", lambda n: None)
    gh, calls = _flaky("HTTP 503")
    with pytest.raises(s.GhError):
        gh._api_json("repos/owner/repo/issues/1/comments", "-f", "body=x")
    assert len(calls) == 1
    gh, calls = _flaky("HTTP 404")
    with pytest.raises(s.GhError):
        gh._api_json("repos/owner/repo/pulls/1")
    assert len(calls) == 1
