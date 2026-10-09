"""Retro 2026-10-09 (control plane, part B): regression tests with positive controls."""
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _git, _open_design, _review, _v2_tree,  # noqa: F401
                                       repo)


def _local(repo, branch):
    return bool(_git("branch", "--list", branch, cwd=repo).strip())


def _on_origin(repo, branch):
    return bool(_git("ls-remote", "--heads", "origin", branch, cwd=repo).strip())


# --- 1. skip-gate / waive-gate clean branches up from the repo root ------------------

def _phase_task_with_dev_worktree(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "in-progress"})
    _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    gh._run = s._default_runner
    wt = s.cmd_worktree_add(gh, 10, repo_path=str(repo))["path"]
    _review(gh, 10, "arch-review", confidence=99)
    return gh, wt


def test_skip_gate_given_the_dev_worktree_still_cleans_the_branches(repo):
    # The orchestrator passes the phase-Task's own worktree as --repo-path; it is released
    # first, so branch cleanup must not run `git -C <removed worktree>`.
    gh, wt = _phase_task_with_dev_worktree(repo)

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=wt)

    assert result["phase_task_complete"] is True
    assert not Path(wt).exists()
    assert "warnings" not in result, result.get("warnings")
    assert "error" not in result["cleanup"]["local_branch"], result["cleanup"]
    assert not _local(repo, "issue-10")


def test_skip_gate_given_the_repo_root_cleans_the_branches(repo):
    # Positive control: the repo root as --repo-path always worked.
    gh, wt = _phase_task_with_dev_worktree(repo)

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=str(repo))

    assert result["phase_task_complete"] is True
    assert "warnings" not in result
    assert not _local(repo, "issue-10")


# --- 2. transition --expect-stage arch-review surfaces the skip bar at top level ------

def _author_doc(gh, repo, issue, path, text="# design\n"):
    gh._run = s._default_runner
    wt = s.cmd_worktree_add(gh, issue, repo_path=str(repo))["path"]
    target = Path(wt) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=wt)
    _git("commit", "-qm", f"add {path}", cwd=wt)
    _git("push", "-q", "origin", f"issue-{issue}", cwd=wt)
    return wt


def _initiative_tree(*extra):
    return FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:cloud"]},
                   {"number": 9, "labels": ["type:epic"], "parent": 6}, *extra])


def test_transition_into_arch_review_returns_the_epic_profile_skip_threshold(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "profiles", [{"name": "default", "match": "*",
                                                  "gates": {"skipConfidenceThreshold": 85}}])
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [
        {"name": "cloud", "match": {"label": "initiative:cloud"}, "placement": "cloud"}])
    gh = _initiative_tree({"number": 10, "labels": ["type:task"], "parent": 9,
                           "stage": "architecture"})
    wt = _author_doc(gh, repo, 10, f"{DOC}/epic-9/architecture.md")

    result = s.cmd_transition(gh, 10, "arch-review", repo_path=wt)

    assert result["ready"] is True
    assert result["skip_confidence_threshold"] == 85


def test_transition_into_lld_review_has_no_skip_threshold(repo):
    # Positive control: only arch-review carries the bar.
    gh = _initiative_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    wt = _author_doc(gh, repo, 11, f"{DOC}/epic-9/lld.md")

    result = s.cmd_transition(gh, 11, "lld-review", repo_path=wt)

    assert result["ready"] is True
    assert "skip_confidence_threshold" not in result


# --- 3. a plain child of a branch:true Initiative integrates into initiative-<i> ------

_BRANCH_PROFILE = {"name": "tijori", "match": {"label": "initiative:branch"}, "branch": True}


def _branch_tree(*extra, labels=("type:initiative", "initiative:branch")):
    return {i["number"]: i for i in FakeGh([
        {"number": 6, "labels": list(labels), "title": "Tijori"}, *extra]).issue_list()}


def test_a_plain_initiative_child_integrates_into_the_initiative_branch(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_BRANCH_PROFILE])
    issues = _branch_tree({"number": 20, "labels": ["type:bug"], "parent": 6,
                           "title": "tijori-api: crash on empty ledger"})

    assert s.integration_base_in(issues, 20) == "initiative-6"


def test_the_roadmap_task_and_unopted_initiatives_still_integrate_into_main(monkeypatch):
    # Positive controls: the Product-Roadmap Task, and any child of an Initiative without
    # `branch: true`, keep `main`.
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [_BRANCH_PROFILE])
    issues = _branch_tree({"number": 7, "labels": ["type:task"], "parent": 6,
                           "title": "Product Roadmap"})
    assert s.integration_base_in(issues, 7) == "main"
    plain = _branch_tree({"number": 20, "labels": ["type:bug"], "parent": 6, "title": "bug"},
                         labels=("type:initiative",))
    assert s.integration_base_in(plain, 20) == "main"
