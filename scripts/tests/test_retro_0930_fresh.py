"""Retro 2026-09-30, freshness slice: the exploratory record stamps the sha actually tested,
close-epic names a stale record's delta, and a close-epic after the epic PR merged recovers
instead of failing on the deleted branch."""
import pytest

import sdlc_next as s
from tests.test_epic_close_evidence import HEAD, TESTED, _epic, _marks, _ready_epic

WT = "/tmp/epic-9-worktree"


def _rev_parse(sha):
    def runner(argv):
        assert argv == ["git", "-C", WT, "rev-parse", "HEAD"], argv
        return sha + "\n"
    return runner


# --- 1. record-epic-verification --repo-path -------------------------------------------

def test_record_with_repo_path_stamps_the_worktree_head_not_the_origin_tip():
    # Regression (incident a): a worktree one commit behind origin stamped the origin tip --
    # a sha never tested -- which then read as fresh evidence.
    gh = _epic([])
    out = s.cmd_record_epic_verification(gh, 9, "exploratory", "ok", repo_path=WT,
                                         runner=_rev_parse(TESTED))
    assert out["sha"] == TESTED and out["sha_source"] == "worktree"
    assert out["behind_origin"] is True and out["origin_sha"] == HEAD
    assert f"exploratory:9 sha:{TESTED} @ " in gh.comments_on(9)[0]


def test_the_worktree_stamp_then_reads_stale_when_origin_moved_on():
    gh = _epic([])
    s.cmd_record_epic_verification(gh, 9, "exploratory", "ok", repo_path=WT,
                                   runner=_rev_parse(TESTED))
    gh.files_since = lambda sha, branch: ["backend/src/x.ts"] if sha == TESTED else []
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False and result["evidence"]["exploratory"] == "missing"
    assert result["stale_delta"] == {"exploratory": {"files": ["backend/src/x.ts"], "count": 1}}


def test_record_with_repo_path_at_the_origin_tip_is_not_behind():
    out = s.cmd_record_epic_verification(_epic([]), 9, "exploratory", "ok", repo_path=WT,
                                         runner=_rev_parse(HEAD))
    assert out["sha"] == HEAD and out["sha_source"] == "worktree"
    assert "behind_origin" not in out and "origin_sha" not in out


def test_record_without_repo_path_keeps_the_origin_tip():
    # Positive control: the old default is unchanged.
    out = s.cmd_record_epic_verification(_epic([]), 9, "exploratory", "ok")
    assert out["sha"] == HEAD and out["sha_source"] == "origin"
    assert "behind_origin" not in out


def test_an_explicit_sha_wins_over_repo_path():
    def runner(argv):
        raise AssertionError("--sha given: the worktree must not be read")
    out = s.cmd_record_epic_verification(_epic([]), 9, "exploratory", "ok", sha=TESTED,
                                         repo_path=WT, runner=runner)
    assert out["sha"] == TESTED and out["sha_source"] == "arg"


def test_the_cli_passes_repo_path(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(s, "get_work_item_provider", lambda: _epic([]))
    monkeypatch.setattr(s, "cmd_record_epic_verification",
                        lambda gh, epic, kind, summary, sha, repo_path=None:
                        seen.update(repo_path=repo_path) or {"ok": True})
    s.main(["record-epic-verification", "9", "--kind", "exploratory", "--summary", "x",
            "--repo-path", WT])
    assert seen["repo_path"] == WT


# --- 2. stale_delta ---------------------------------------------------------------------

def test_a_stale_exploratory_record_reports_its_delta_capped_at_50():
    # Regression (incident b): main moved during the pass and the orchestrator had to work
    # out the delta by hand to brief a scoped re-pass.
    files = [f"backend/src/f{i}.ts" for i in range(60)]
    result = s.cmd_close_epic(_epic(_marks(), files_since=files), 9)
    assert result["stale_delta"]["exploratory"] == {"files": files[:50], "count": 60}
    assert set(result["stale_delta"]) == {"exploratory"}


def test_fresh_and_carried_forward_records_get_no_stale_delta():
    # Positive control.
    gh = _ready_epic([_marks()[1]], checks=[])
    assert "stale_delta" not in s.cmd_close_epic(gh, 9)
    docs = _epic(_marks(), files_since=["docs/x.md"])
    assert "stale_delta" not in s.cmd_close_epic(docs, 9)


# --- 3. close-epic recovery after the epic PR merged -------------------------------------

def _merged_epic(state="OPEN"):
    gh = _epic([_marks()[1]])
    gh.issues[9]["state"] = state
    gh.pr_list_for_branch = lambda branch, state="open": (
        [{"number": 38}] if branch == "epic-9" and state == "merged" else [])

    def gone(*a, **kw):
        raise s.GhError("gh: Not Found (HTTP 404)")
    gh.branch_behind_by = gh.branch_head_sha = gh.files_since = gone
    gh.pr_merge = gh.pr_create = gh.pr_ready = gone
    return gh


def test_a_second_close_epic_after_the_merge_recovers_and_closes_the_issue():
    # Regression (incident c): the Epic issue was still OPEN after the merge and a second
    # close-epic exited 1 with the compare 404 on the deleted branch.
    gh = _merged_epic()
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True and result["recovered"] is True and result["pr"] == 38
    assert gh.issues[9]["state"] == "CLOSED"
    assert gh.issues[9]["status"] == "done"
    for key in ("cleanup", "children_cleanup", "run_state", "stack", "worktree"):
        assert key in result


def test_recovery_on_an_already_closed_epic_does_not_close_it_again():
    gh = _merged_epic("CLOSED")
    closes = []
    gh.issue_close = lambda n, reason="completed": closes.append(n)
    result = s.cmd_close_epic(gh, 9)
    assert result["recovered"] is True and closes == []


def test_the_normal_merge_closes_the_epic_issue_explicitly():
    # Regression (incident c): only `Closes #9` in the PR body closed it, asynchronously,
    # so next-action kept returning run-epic.
    gh = _ready_epic([_marks()[1]], checks=[])
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True and "recovered" not in result and gh.merged == [38]
    assert gh.issues[9]["state"] == "CLOSED"


def test_an_open_epic_without_a_merged_pr_still_reconciles(monkeypatch):
    # Positive control: the merged-PR lookup must not short-circuit the reconcile.
    gh = _epic([_marks()[1]])
    gh.branch_behind_by = lambda branch, base="main": 2
    reconciled = []

    class Ws:
        path, retained = "/tmp/ws", None
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(s, "BranchWorkspace", Ws)
    monkeypatch.setattr(s, "git_reconcile_branch",
                        lambda path, branch, base, runner: reconciled.append(branch))
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False and result["reconciled"] == 2
    assert reconciled == ["epic-9"] and "recovered" not in result
    assert gh.issues[9]["state"] == "OPEN"
