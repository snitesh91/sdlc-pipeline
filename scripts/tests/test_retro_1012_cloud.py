"""Retro 2026-10-12, cloud: a cloud session skips local filesystem cleanup (its container is
discarded), and skips the ref delete the proxy refuses when GitHub auto-deletes merged heads."""
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_retro_cleanup import _merged_pr, _on_origin, _real, _task
from tests.test_retro_composites import _closeable
from tests.test_sdlc_next import _enable_stack
from tests.test_v2_phase_tasks import _git, _worktree_with_doc, repo  # noqa: F401


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("SDLC_PLACEMENT", "cloud")


def test_cloud_merge_pr_keeps_the_worktree_and_still_deletes_the_branch(repo, cloud):
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    pr = _merged_pr(gh)

    result = s.cmd_merge_pr(gh, pr, 5, repo_path=str(repo))

    assert Path(wt).exists()
    assert result["worktree"] == {"released": False, "skipped": "cloud"}
    assert result["cleanup"]["review_worktree"] == {"skipped": "cloud"}
    assert "worktree_pruned" not in result["cleanup"]
    assert result["cleanup"]["remote_branch"]["deleted"] is True
    assert not result.get("warnings")


def test_local_merge_pr_still_releases_the_worktree(repo):
    # Positive control.
    gh = _real(_task(), repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))
    assert not Path(wt).exists() and result["worktree"]["released"] is True


def _refusing(gh):
    def refuse(branch):
        raise s.GhError("HTTP 403 (the cloud GitHub proxy refuses ref deletes)")
    gh.delete_branch = refuse
    return gh


def test_cloud_skips_the_ref_delete_when_the_repo_auto_deletes_heads(repo, cloud):
    gh = _refusing(_real(_task(), repo))
    gh.auto_deletes_head_branches = lambda: True
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))

    assert result["cleanup"]["remote_branch"]["skipped"] == "repo auto-deletes merged heads"
    assert not result.get("warnings")


def test_cloud_still_warns_when_the_repo_keeps_merged_heads(repo, cloud):
    # Positive control: without auto-delete a refused delete is a real leak.
    gh = _refusing(_real(_task(), repo))
    gh.auto_deletes_head_branches = lambda: False
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")

    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))

    assert _on_origin(repo, "issue-5")
    assert any("could not be deleted" in w for w in result.get("warnings", []))


def test_local_deletes_even_when_the_repo_auto_deletes_heads(repo):
    gh = _real(_task(), repo)
    gh.auto_deletes_head_branches = lambda: True
    _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    result = s.cmd_merge_pr(gh, _merged_pr(gh), 5, repo_path=str(repo))
    assert result["cleanup"]["remote_branch"]["deleted"] is True


def test_cloud_prune_stale_skips_worktrees_but_still_prunes_origin(repo, cloud):
    gh = _real(_task(5), repo)
    wt5 = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _merged_pr(gh)
    gh.issues[5]["state"] = "CLOSED"

    result = s.cmd_prune_stale(gh, str(repo))

    assert Path(wt5).exists() and not _on_origin(repo, "issue-5")
    assert result["skipped"] == "cloud" and result["worktree_pruned"] is False
    assert result["fetch_pruned"] is True


def test_cloud_close_epic_clean_worktree_leaves_the_output(repo, cloud):
    gh, wt = _closeable(repo)
    (wt / "evidence.json").write_text("{}")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), clean_worktree=True)

    assert result["merged"] is True
    assert result["worktree_clean"]["skipped"] == "cloud" and (wt / "evidence.json").exists()


def test_cloud_close_epic_clean_worktree_still_refuses_tracked_changes(repo, cloud):
    gh, wt = _closeable(repo)
    (wt / "README.md").write_text("hand edit\n")
    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), clean_worktree=True)
    assert result["merged"] is False and gh.merges == []


def test_cloud_teardown_keeps_the_data_dir(monkeypatch, tmp_path, cloud):
    _enable_stack(monkeypatch, tmp_path)
    s.cmd_provision_epic_stack(159, shell=lambda c, cwd: "", probe=lambda p: True)
    data = tmp_path / ".docker/postgres-data-epic159"
    data.mkdir(parents=True, exist_ok=True)

    result = s.cmd_teardown_epic_stack(159, shell=lambda c, cwd: "", running_probe=lambda p: [])

    assert result["torn_down"] is True and result["data_kept"] == "cloud" and data.exists()
    assert not (tmp_path / ".env.epic159").exists()


def test_cloud_parking_keeps_the_units_worktree(cloud):
    calls = []
    out = s._release_unit_worktree(186, base_repo="/repo", runner=lambda argv: calls.append(argv))
    assert out == {"released": False, "skipped": "cloud"} and calls == []


# --- open-dev-pr: a draft create the proxy failed whole ---------------------------------

from tests.test_retro_1009_ctl_a import _DevPrGh, _exit_posted  # noqa: E402


class _DraftlessGh(_DevPrGh):
    """A draft create fails with no PR made; a plain create works."""

    def pr_create(self, base, head, title, body, draft=False):
        if draft:
            raise s.GhError(f"{s.PR_CREATE_NO_NUMBER}: POST repos/o/r/pulls returned None")
        return super().pr_create(base, head, title, body, False)


def _no_offenders(monkeypatch):
    monkeypatch.setattr(s, "dev_pr_scope_offenders", lambda *a, **k: {
        "design_docs": [], "foreign_footprint": []})


def test_open_dev_pr_falls_back_to_a_plain_create_then_drafts_it(monkeypatch):
    _no_offenders(monkeypatch)
    gh = _DraftlessGh()
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert out["created"] is True and "draft_fallback" in out
    assert out["converted_to_draft"] is True and gh.prs[out["pr"]]["isDraft"] is True
    assert gh.issues[10]["stage"] == "pr-review" and len(_exit_posted(gh, out["pr"])) == 1


def test_open_dev_pr_fallback_warns_when_drafting_fails(monkeypatch):
    _no_offenders(monkeypatch)
    gh = _DraftlessGh(convert_fails=True)
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert "draft_warning" in out and gh.issues[10]["stage"] == "pr-review"


# --- record-local-ci: `e2e` is always a suite key -----------------------------------------

def test_e2e_is_a_local_ci_suite_without_any_config():
    assert "e2e" in s.LOCAL_CI_SUITES


def test_record_local_ci_attests_e2e(tmp_path):
    posted = []
    gh = type("G", (), {"pr_comment": lambda self, pr, body: posted.append(body)})()
    log = tmp_path / "out.txt"
    log.write_text("2 passed (14s)\n")
    out = s.cmd_record_local_ci(gh, 5, "e2e", "a" * 40, "npx playwright test x.spec.ts", str(log))
    assert out["attested"] is True and "`e2e`" in posted[0]


def test_record_local_ci_still_refuses_an_unknown_suite():
    with pytest.raises(s.GhError, match="suite must be one of"):
        s.cmd_record_local_ci(None, 5, "nope", "a" * 40, "x", "ok")
