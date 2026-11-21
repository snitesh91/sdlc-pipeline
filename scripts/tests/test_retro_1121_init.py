"""Retro 2026-11-21: a Task parented directly to an Initiative is a supported unit (base
`initiative-<i>`); `reparent-issue` never leaves an issue parentless; `file-closing-delta
--defer` falls back to the Initiative; a profile that omits `branch` is flagged."""
import pytest

import sdlc_next as s
from sdlc import checks
from tests.test_v2_phase_tasks import FakeGh

BRANCHED = {"name": "kipsa", "match": {"label": "initiative:branch"}, "branch": True}


@pytest.fixture(autouse=True)
def profiles(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [BRANCHED])


def _tree(*extra):
    return FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:branch"]},
                   {"number": 9, "labels": ["type:epic"], "parent": 6},
                   {"number": 20, "labels": ["type:task"], "parent": 6, "stage": "development"},
                   {"number": 21, "labels": ["type:task"], "parent": 9, "stage": "development"},
                   *extra])


# --- a direct child of an Initiative ------------------------------------------------------

def _trees(monkeypatch):
    added = []
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda gh, n, unit="issue", *a, **k: added.append((n, unit)) or {"path": "/wt"})
    return added


def test_start_stage_claims_a_direct_child_of_an_initiative(monkeypatch):
    """Regression: `initiative-6` was parsed as an epic branch (ValueError 'ative-6')."""
    added = _trees(monkeypatch)
    result = s.cmd_start_stage(_tree(), 20, "development")
    assert result["claimed"] is True and added == [(20, "issue")]


def test_start_stage_still_stands_up_the_epic_tree_for_an_epic_child(monkeypatch):
    """Positive control."""
    added = _trees(monkeypatch)
    assert s.cmd_start_stage(_tree(), 21, "development")["claimed"] is True
    assert added == [(9, "epic"), (21, "issue")]


def test_scope_offenders_reads_no_lld_for_a_direct_child_of_an_initiative():
    """Regression: the same parse crashed open-dev-pr, transition and verify-exit."""
    def no_git(argv):
        raise AssertionError(f"no lld.md to read: {argv}")
    assert s.dev_pr_scope_offenders(_tree(), 20, ["apps/api/x.ts"], runner=no_git) == {
        "design_docs": [], "foreign_footprint": []}


def test_scope_offenders_still_reads_the_epic_lld_for_an_epic_child():
    """Positive control."""
    read = []
    s.dev_pr_scope_offenders(_tree(), 21, ["apps/api/x.ts"],
                             runner=lambda argv: read.append(argv[-1]) or "")
    assert read and read[0].startswith("origin/epic-9:")


def _scoped(monkeypatch):
    spec = {"workflow": "IT", "suite": "it", "prefixes": ("apps/",), "files": (),
            "excludeGlobs": (), "bases": ("main", "initiative-*"), "attestable": True}
    monkeypatch.setattr(checks, "required_workflows", lambda base=None: (spec,))


def test_a_task_pr_into_the_initiative_branch_owes_no_base_scoped_suite(monkeypatch):
    """Regression: the initiative PR runs the full suites, not each Task PR into it."""
    _scoped(monkeypatch)
    deferred = checks._pr_deferred_suites(_tree(), "issue-20", "initiative-6")
    assert deferred == ("it",)
    assert checks.merge_gate_status(["apps/a.ts"], [], [], "abc", "initiative-6", deferred) == (
        "passed", [])


def test_an_epic_pr_into_the_initiative_branch_still_owes_the_suite(monkeypatch):
    """Positive control: only a Task PR is exempt."""
    _scoped(monkeypatch)
    deferred = checks._pr_deferred_suites(_tree(), "epic-9", "initiative-6")
    assert deferred == ()
    assert checks.merge_gate_status(["apps/a.ts"], [], [], "abc", "initiative-6", deferred) == (
        "missing-checks", ["IT"])
    assert checks._pr_deferred_suites(_tree(), "issue-20", "main") == ()


# --- reparent-issue -----------------------------------------------------------------------

def test_reparent_restores_the_old_parent_when_the_new_one_refuses():
    """Regression: a parent at the sub-issue cap left the issue parentless."""
    gh = _tree()
    link = gh.add_sub_issue

    def capped(parent, child):
        if parent == 9:
            raise s.GhError("HTTP 422: an issue cannot have more than 100 sub-issues")
        link(parent, child)
    gh.add_sub_issue = capped
    with pytest.raises(s.GhError):
        s.cmd_reparent_issue(gh, 20, 9)
    assert gh.issues[20]["parent"] == 6


def test_reparent_moves_the_issue():
    """Positive control."""
    gh = _tree()
    assert s.cmd_reparent_issue(gh, 20, 9)["moved"] is True and gh.issues[20]["parent"] == 9


# --- file-closing-delta --defer -----------------------------------------------------------

def test_defer_without_a_bugs_epic_files_under_the_initiative():
    """Regression: the finding was refused and left unfiled."""
    gh = _tree()
    result = s.cmd_file_closing_delta(gh, 9, "Low bug", "body", defer=True)
    assert result["parent"] == 6 and gh.issues[result["delta_issue"]]["parent"] == 6


def test_defer_prefers_the_bugs_epic():
    """Positive control."""
    gh = _tree({"number": 8, "labels": ["type:epic", "epic:standing"], "parent": 6,
                "body": s.BUGS_EPIC_MARKER})
    assert s.cmd_file_closing_delta(gh, 9, "Low bug", "body", defer=True)["parent"] == 8


def test_defer_without_an_initiative_is_refused_with_the_reason():
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]}])
    result = s.cmd_file_closing_delta(gh, 9, "Low bug", "body", defer=True)
    assert result["refused"] is True and "no open Initiative" in result["reason"]


# --- a profile states `branch` ------------------------------------------------------------

def test_a_profile_that_omits_branch_is_flagged(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles",
                        [BRANCHED, {"name": "cloud-epics", "match": {"label": "x"}}])
    warnings = s.cmd_initiative_profile(_tree(), 20)["warnings"]
    assert len(warnings) == 1 and "'cloud-epics'" in warnings[0]
    assert s.cmd_show_config()["warnings"] == warnings


def test_explicit_profiles_raise_no_warning():
    """Positive control."""
    assert "warnings" not in s.cmd_initiative_profile(_tree(), 20)
    assert "warnings" not in s.cmd_show_config()
