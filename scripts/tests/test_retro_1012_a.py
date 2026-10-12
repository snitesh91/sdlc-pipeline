"""Retro 2026-10-12 (fork A): standing children under an initiative branch, merge-pr's close on
a non-default base, close-epic suite scope, initiative-close scaffolding check, config read
from the PR base, and close-epic's carry-forward reconcile skip."""
import pytest

import sdlc_next as s
from tests.test_initiative_branch import OPT_IN, PROFILE, _exists_on_origin
from tests.test_v2_phase_tasks import FakeGh, repo  # noqa: F401


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [PROFILE])


def _standing_tree(opt_in=True):
    labels = ["type:initiative"] + ([OPT_IN] if opt_in else [])
    return FakeGh([{"number": 6, "labels": labels, "title": "Tijori"},
                   {"number": 9, "labels": ["type:epic", "epic:standing"], "parent": 6},
                   {"number": 12, "labels": ["type:task"], "parent": 9, "stage": "development"}])


# --- 1. a standing Epic's child bases on its Initiative's branch ------------------------

def test_a_standing_child_under_an_initiative_branch_integrates_there(opted_in):
    assert s.integration_base(_standing_tree(), 12) == "initiative-6"


def test_a_standing_child_without_the_opt_in_stays_on_main(opted_in):
    """Positive control."""
    assert s.integration_base(_standing_tree(opt_in=False), 12) == "main"


def test_worktree_add_cuts_a_standing_child_from_the_initiative_branch(repo, opted_in):
    result = s.cmd_worktree_add(_standing_tree(), 12, repo_path=str(repo))
    assert result["base"] == "origin/initiative-6"
    assert _exists_on_origin(repo, "initiative-6")


# --- 2. merge-pr closes a unit merged into a non-default base ---------------------------

def _finalize(gh, monkeypatch, base):
    monkeypatch.setattr(s, "record_terminal_unit", lambda *a, **k: None)
    monkeypatch.setattr(s, "cleanup_unit", lambda *a, **k: {"worktree": {}})
    return s._finalize_merged_pr(gh, 50, 12, ".", base, [], {})


def test_merge_pr_closes_a_standing_child_merged_into_the_initiative_branch(opted_in, monkeypatch):
    gh = _standing_tree()
    out = _finalize(gh, monkeypatch, s.integration_base(gh, 12))
    assert out["issue_closed"] is True and gh.issues[12]["state"] == "CLOSED"


def test_merge_pr_leaves_closing_a_main_merge_to_github(opted_in, monkeypatch):
    """Positive control: on the default branch `Closes #n` closes it."""
    gh = _standing_tree(opt_in=False)
    out = _finalize(gh, monkeypatch, s.integration_base(gh, 12))
    assert out["issue_closed"] is False and gh.issues[12]["state"] == "OPEN"


# --- 3. close-epic scopes suites to the epic PR's own (complete) file list --------------

class _EpicPrGh:
    def pr_view(self, n, fields=""):
        return {"comments": [], "headRefOid": "feedface"}

    def pr_checks(self, n):
        return [{"name": "Frontend CI", "bucket": "pass", "workflow": "Frontend CI"}]


def test_a_big_epic_pr_requires_only_the_suites_its_files_touch():
    files = [f"frontend/f{i}.ts" for i in range(347)]
    out = s._epic_suite_evidence(_EpicPrGh(), "epic-9", [], files, 77, False)
    assert out["suites"]["backend"] == "not_required"
    assert out["missing"] == []


def test_a_compare_delta_at_its_cap_is_still_treated_as_truncated():
    """Positive control: without an epic PR the 300-file compare list may be cut short."""
    files = [f"frontend/f{i}.ts" for i in range(300)]
    out = s._epic_suite_evidence(_EpicPrGh(), "epic-9", [], files, None, False)
    assert out["suites"]["backend"] == "missing"


# --- 6. close-epic skips a main reconcile that brings only carry-forward files ---------

from tests.test_epic_close_evidence import TESTED, _epic, _marks  # noqa: E402


def _behind_epic(incoming, epic_side=("backend/a.ts",)):
    gh = _epic(_marks())
    gh.branch_behind_by = lambda branch, base="main": 2
    gh.base_delta_files = lambda head, base="main": list(
        incoming if head == "epic-9" else epic_side)
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n, **kw: None
    gh.pr_files = lambda n: list(epic_side)
    gh.pr_checks = lambda n: [{"name": "Backend CI", "bucket": "pass", "workflow": "Backend CI"}]
    return gh


def test_close_epic_skips_a_reconcile_that_brings_only_carry_forward_files(monkeypatch):
    def no_reconcile(*a, **k):
        raise AssertionError("reconciled")
    monkeypatch.setattr(s, "git_reconcile_branch", no_reconcile)
    result = s.cmd_close_epic(_behind_epic(["docs/ops/runner.md"]), 9)
    assert result["merged"] is True
    assert result["reconcile_skipped"] == {"behind": 2, "files": ["docs/ops/runner.md"]}


class _Ws:
    path, retained = "/nowhere", None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("incoming,epic_side", [
    (["backend/b.ts"], ["backend/a.ts"]),        # incoming code
    (["docs/x.md"], ["docs/x.md"]),              # the epic touched the same file
])
def test_close_epic_still_reconciles_code_or_an_overlapping_file(incoming, epic_side, monkeypatch):
    """Positive control."""
    calls = []
    monkeypatch.setattr(s, "BranchWorkspace", _Ws)
    monkeypatch.setattr(s, "git_reconcile_branch", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(s, "_refresh_epic_worktree", lambda *a, **k: None)
    result = s.cmd_close_epic(_behind_epic(incoming, epic_side), 9)
    assert calls and result["reconciled"] == 2 and "reconcile_skipped" not in result
