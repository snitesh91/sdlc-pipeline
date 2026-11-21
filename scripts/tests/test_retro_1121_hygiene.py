"""Retro 2026-11-21, hygiene: a passed phase-Task gate pushes nothing to its merged branch, a
merged head GitHub auto-deletes is never deleted by hand, one run per Epic per machine, and
`prune-stale` reports host disk."""
import argparse
import os
from collections import namedtuple

import pytest

import sdlc_next as s
from sdlc import run_state
from tests.test_placement import _tree
from tests.test_retro_cleanup import _merged_pr, _on_origin, _real, _task
from tests.test_v2_phase_tasks import (  # noqa: F401
    _git, _open_design, _v2_tree, _worktree_with_doc, repo,
)


# --- branch-delete-after-merge ---------------------------------------------------------

def _passed_arch_gate(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "in-progress"})
    pr = _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md", "development", "ok")
    gh.pr_merge(pr, delete_branch=False)
    return gh, pr


def test_pass_gate_never_pushes_a_phase_task_branch_after_its_design_pr_merged(repo):
    gh, pr = _passed_arch_gate(repo)
    _git("push", "-q", "origin", "--delete", "issue-10", cwd=repo)  # GitHub's auto-delete
    gh.delete_branch = lambda branch: pytest.fail(f"{branch} was pushed back to origin")

    result = s.cmd_pass_gate(gh, str(repo), 10, pr, "architecture")

    assert result["phase_task_complete"] is True
    assert not _on_origin(repo, "issue-10")


def test_pass_gate_still_reconciles_a_standing_child_with_its_base(repo):
    # Positive control: a unit with a next stage keeps its branch current.
    gh = s_fake = _v2_tree({"number": 90, "labels": ["type:epic", "epic:standing"]},
                           {"number": 91, "labels": ["type:task"], "parent": 90,
                            "stage": "architecture", "status": "in-progress"})
    gh.repo = repo
    _git("push", "-q", "origin", "origin/main:refs/heads/epic-90", cwd=repo)
    _worktree_with_doc(gh, repo, 91, f"{s.DOC_ROOT}/issue-91/architecture.md", "# arch\n")
    gate = s.cmd_open_gate(s_fake, str(repo), 91, "Fix", "architecture.md", "development", "ok")
    gh.pr_merge(gate["gate_pr"], delete_branch=False)

    result = s.cmd_pass_gate(gh, str(repo), 91, gate["gate_pr"], "architecture")

    assert result["next_stage"] == "development"
    epic_tip = _git("rev-parse", "origin/epic-90", cwd=repo).strip()
    assert _git("merge-base", "--is-ancestor", epic_tip, "origin/issue-91", cwd=repo) == ""


def test_a_merged_head_is_left_to_github_when_the_repo_auto_deletes(repo):
    gh = _real(_task(), repo)
    gh.auto_deletes_head_branches = lambda: True
    gh.delete_branch = lambda branch: pytest.fail("deleted a head GitHub deletes itself")
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))

    assert result["cleanup"]["remote_branch"]["skipped"] == "repo auto-deletes merged heads"
    assert not result.get("warnings")


def test_a_merged_head_is_deleted_when_the_repo_keeps_heads(repo):
    # Positive control.
    gh = _real(_task(), repo)
    gh.auto_deletes_head_branches = lambda: False
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))

    assert result["cleanup"]["remote_branch"]["deleted"] is True
    assert not _on_origin(repo, "issue-5")


# --- two-runs-same-epic ----------------------------------------------------------------

@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))


def _next_action(gh, run_id, **kw):
    return s.cmd_next_action(gh, argparse.Namespace(epic=9, run_id=run_id, repo_path=".",
                                                    skip_epic=[], sync_epic=False, **kw))


def test_next_action_refuses_a_second_run_on_a_live_epic(runs):
    gh = _tree()
    first = _next_action(gh, "run-a")

    second = _next_action(gh, "run-b")

    assert first.get("refused") is None
    assert second["action"] == "none" and second["refused"] is True
    assert second["likely_live"] is True and second["run_id"] == "run-a"
    assert run_state.read_run_state(9)["run_id"] == "run-a"


def test_the_same_run_a_stale_run_and_a_take_over_all_pass(runs):
    # Positive controls.
    gh = _tree()
    _next_action(gh, "run-a")
    assert _next_action(gh, "run-a").get("refused") is None
    assert _next_action(gh, "run-b", take_over=True).get("refused") is None

    old = os.path.getmtime(run_state._run_state_path(9)) - s.RESUME_LIVE_WINDOW_MINUTES * 60 - 1
    os.utime(run_state._run_state_path(9), (old, old))
    assert _next_action(gh, "run-c").get("refused") is None


def test_a_new_run_in_the_same_session_passes(runs, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    gh = _tree()
    _next_action(gh, "run-a")
    assert _next_action(gh, "run-b").get("refused") is None


# --- host-disk-preflight ---------------------------------------------------------------

_Usage = namedtuple("_Usage", "total used free")


def _prune_with_free(repo, monkeypatch, free_gb):
    monkeypatch.setattr(s.shutil, "disk_usage",
                        lambda path: _Usage(500e9, 500e9 - free_gb * 1e9, free_gb * 1e9))
    gh = _real(_task(5), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    return wt, s.cmd_prune_stale(gh, str(repo), dry_run=True)


def test_prune_stale_warns_on_a_low_host_disk_and_sizes_the_worktrees(repo, monkeypatch):
    wt, result = _prune_with_free(repo, monkeypatch, 5)

    disk = result["host_disk"]
    assert disk["free_gb"] == 5.0
    assert [os.path.realpath(w["path"]) for w in disk["worktrees"]] == [os.path.realpath(wt)]
    assert any("host disk low: 5.0 GB free" in w for w in result["warnings"])


def test_prune_stale_reports_a_roomy_host_disk_without_a_warning(repo, monkeypatch):
    # Positive control.
    _, result = _prune_with_free(repo, monkeypatch, 200)

    assert not any("host disk low" in w for w in result.get("warnings", []))
