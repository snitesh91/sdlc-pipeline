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
