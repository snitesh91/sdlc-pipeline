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
