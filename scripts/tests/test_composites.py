"""Composite commands and phase-Task integration-base auto-detection.
Happy paths run against a real git origin (`repo` fixture); failure paths
replace one `cmd_*` step to prove later steps never run."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _CARVED_LLD, _advance_origin,  # noqa: F401
                                       _design_branch, _ensure_epic_branch, _git, _open_design,
                                       _origin_file, _push_doc_branch, _review, repo)


Gh = FakeGh  # kept as the name this file's trees were written against


def _tree(*extra, epic_labels=("type:epic",)):
    return Gh([{"number": 9, "labels": list(epic_labels)}, *extra])


def _boom(name):
    def fail(*_a, **_k):
        raise s.GhError(f"{name} exploded")
    return fail


def _must_not_run(name):
    def fail(*_a, **_k):
        raise AssertionError(f"{name} ran after an earlier step failed")
    return fail


def _upstream(repo, branch):
    """Commit SHA of `origin/<branch>`."""
    _git("fetch", "-q", "origin", cwd=repo)
    return _git("rev-parse", f"origin/{branch}", cwd=repo).strip()


# --- integration_base: every child of a non-standing Epic uses its branch ----

@pytest.mark.parametrize("stage", ["architecture", "lld", "development", "pr-review", None])
def test_integration_base_sends_every_child_of_a_non_standing_epic_to_the_epic_branch(stage):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": stage})
    assert s.integration_base(gh, 10) == "epic-9"


def test_integration_base_keeps_an_initiatives_roadmap_task_and_parentless_issues_on_main():
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product"},
                 {"number": 8, "labels": ["type:task"], "stage": "product"}])
    assert s.integration_base(gh, 7) == "main"
    assert s.integration_base(gh, 8) == "main"


@pytest.mark.parametrize("stage", ["architecture", "development", None])
def test_integration_base_keeps_a_standing_epics_child_on_main_at_every_stage(stage):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": stage},
               epic_labels=("type:epic", "epic:standing"))
    assert s.integration_base(gh, 10) == "main"


def test_worktree_add_cuts_a_phase_task_from_the_epic_branch_creating_it_when_absent(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    assert not _git("ls-remote", "--heads", "origin", "epic-9", cwd=repo).strip()
    result = s.cmd_worktree_add(gh, 10, repo_path=str(repo))
    assert result["base"] == "origin/epic-9"
    assert _git("ls-remote", "--heads", "origin", "epic-9", cwd=repo).strip()


def test_worktree_add_cuts_a_phase_task_from_the_current_epic_tip(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/architecture.md", "# arch\n")
    wt = s.cmd_worktree_add(gh, 10, repo_path=str(repo))["path"]
    assert (Path(wt) / DOC / "epic-9" / "architecture.md").read_text() == "# arch\n"


def test_worktree_add_keeps_a_product_roadmap_task_and_standing_child_on_main(repo):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product"}])
    assert s.cmd_worktree_add(gh, 7, repo_path=str(repo))["base"] == "origin/main"


def test_worktree_add_explicit_base_still_overrides(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    _git("push", "-q", "origin", "main:epic-9", cwd=repo)
    result = s.cmd_worktree_add(gh, 10, repo_path=str(repo), base="origin/epic-9")
    assert result["base"] == "origin/epic-9"


def test_sync_branch_reconciles_a_phase_task_with_the_epic_branch(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    wt = s.cmd_worktree_add(gh, 10, repo_path=str(repo))["path"]
    _push_doc_branch(repo, "epic-9", "epic-note.md", "moved\n")
    result = s.cmd_sync_branch(gh, wt, 10)
    assert result["base"] == "epic-9" and result["synced"] is True
    assert (Path(wt) / "epic-note.md").exists()


# --- cut-phase-tasks ----------------------------------------------------------

def test_cut_phase_tasks_creates_stages_blocks_and_stands_up_the_epic_and_arch_worktrees(repo):
    gh = _tree()

    result = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    arch, lld = result["architecture_task"], result["lld_task"]
    assert result["ok"] is True and result["failed_step"] is None
    assert result["completed_steps"] == [
        "epic-worktree", "architecture.create-issue", "architecture.set-stage",
        "lld.create-issue", "lld.set-stage", "add-blocked-by", "worktree-add"]
    assert (gh.issues[arch]["title"], gh.issues[lld]["title"]) == ("Architecture phase", "LLD phase")
    for n in (arch, lld):
        assert gh.issues[n]["parent"] == 9
        assert s.classify_unit_from_issue(gh.issue_epic_info(n)) == "task"  # label applied
    assert gh.issues[arch]["stage"] == "architecture" and gh.issues[lld]["stage"] == "lld"
    for n in (arch, lld):
        assert gh.issues[n]["issue_type"] == "Task"
        assert (gh.issues[n]["priority"], gh.issues[n]["effort"]) == ("Medium", "Medium")
    assert gh.issues[arch]["status"] == "todo"  # set by create-issue, never claimed
    assert gh.blocked_by(lld) == [arch]
    assert result["steps"]["worktree-add"]["base"] == "origin/epic-9"
    epic_wt = result["steps"]["epic-worktree"]
    assert epic_wt["branch"] == "epic-9" and Path(epic_wt["path"]).is_dir()
    assert _git("ls-remote", "--heads", "origin", "epic-9", cwd=repo).strip()
    assert "skill_dir" not in result


def test_cut_phase_tasks_is_idempotent(repo):
    gh = _tree()
    first = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))
    issues_after_first = set(gh.issues)

    second = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert set(gh.issues) == issues_after_first
    assert (second["architecture_task"], second["lld_task"]) == \
        (first["architecture_task"], first["lld_task"])
    assert second["ok"] is True and second["failed_step"] is None
    assert second["steps"]["architecture.create-issue"]["reused"] is True
    assert second["steps"]["add-blocked-by"]["added"] is False
    assert gh.blocked_by(second["lld_task"]) == [second["architecture_task"]]
    assert second["steps"]["worktree-add"]["created"] is False
    assert second["steps"]["epic-worktree"]["created"] is False


def test_cut_phase_tasks_finishes_a_half_cut_epic_without_duplicating(repo):
    # A crash after creating the Architecture Task but before staging it.
    gh = _tree({"number": 10, "title": "Architecture phase", "labels": ["type:task"],
                "parent": 9})

    result = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert result["architecture_task"] == 10
    assert gh.issues[10]["stage"] == "architecture"
    assert [n for n, i in gh.issues.items() if i["title"] == "Architecture phase"] == [10]


def test_cut_phase_tasks_never_rewinds_or_reopens_a_finished_arch_task(repo):
    gh = _tree({"number": 10, "title": "Architecture phase", "labels": ["type:task"],
                "parent": 9, "state": "CLOSED"})

    result = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert result["architecture_task"] == 10 and result["ok"] is True
    assert gh.issues[10]["stage"] is None
    assert "worktree-add" not in result["steps"]
    assert gh.issues[result["lld_task"]]["stage"] == "lld"


@pytest.mark.parametrize("labels", [("type:epic", "epic:standing"), ("type:task",)])
def test_cut_phase_tasks_refuses_anything_but_a_non_standing_epic(labels):
    gh = _tree(epic_labels=labels)
    result = s.cmd_cut_phase_tasks(gh, 9)
    assert result["refused"] is True
    assert set(gh.issues) == {9}


def test_cut_phase_tasks_stops_at_the_first_failed_step(monkeypatch):
    gh = _tree()
    monkeypatch.setattr(s, "cmd_set_stage", _boom("set-stage"))
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, unit="issue", **k: {"path": "/wt"} if unit == "epic"
                        else _must_not_run("worktree-add")())

    result = s.cmd_cut_phase_tasks(gh, 9)

    assert result["ok"] is False
    assert result["failed_step"] == "architecture.set-stage"
    assert result["completed_steps"] == ["epic-worktree", "architecture.create-issue"]
    assert "set-stage exploded" in result["error"]
    assert result["lld_task"] is None
    assert [i["title"] for i in gh.issues.values()] == ["issue 9", "Architecture phase"]


# --- finish-lld ---------------------------------------------------------------

def _lld_tree():
    return _tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
                 {"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                  "status": "in-progress"})


def _lld_ready(gh, repo, text=_CARVED_LLD, review="clean"):
    """LLD Task #11 as `transition` leaves it: doc on issue-11, design PR open, review recorded."""
    pr = _open_design(gh, repo, 11, "lld.md", text)
    _review(gh, 11, "lld-review", review)
    return pr


def test_finish_lld_merges_the_design_pr_creates_advances_and_closes(repo):
    gh = _lld_tree()
    pr = _lld_ready(gh, repo)

    result = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert result["ok"] is True and result["failed_step"] is None
    assert result["completed_steps"] == ["merge-design-pr", "create-lld-tasks", "merge-lld-doc",
                                         "close-issue"]
    assert gh.merges == [(pr, False)]
    tasks = result["steps"]["create-lld-tasks"]["tasks"]
    assert {gh.issues[n]["stage"] for n in tasks.values()} == {"development"}
    assert "epic:architected" in gh.issues[9]["labels"]
    assert gh.issues[11]["state"] == "CLOSED" and result["closed"] is True
    assert f"## Task #{tasks['skeleton-health']}" in _origin_file(
        repo, "epic-9", f"{DOC}/epic-9/lld.md")


def test_finish_lld_rerun_keeps_the_numbered_epic_doc(repo):
    gh = _lld_tree()
    _lld_ready(gh, repo)
    s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))
    numbered = _upstream(repo, "epic-9")

    again = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert again["steps"]["merge-design-pr"]["already_merged"] is True
    assert len(gh.merges) == 1
    assert _upstream(repo, "epic-9") == numbered  # nothing reverted, nothing re-numbered


def test_a_second_lld_pr_carrying_the_unnumbered_original_never_reverts_the_numbering(repo):
    """An LLD revision cut before `create-lld-tasks` numbered the doc must not undo it."""
    gh = _lld_tree()
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")
    numbered = s.number_task_headings(_CARVED_LLD, {"skeleton-health": 12, "greet-endpoint": 13})
    _advance_origin(repo, "epic-9", f"{DOC}/epic-9/lld.md", numbered)

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["up_to_date"] is True and result["merged"] is False
    assert gh.merges == [] and gh.prs[pr]["state"] == "OPEN"
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md") == numbered


def test_a_changed_lld_still_merges_over_a_numbered_one(repo):
    """Positive control for the numbering guard: real edits are not swallowed."""
    gh = _lld_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED_LLD)
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD + "More.\n")
    _review(gh, 11, "lld-review")
    numbered = s.number_task_headings(_CARVED_LLD, {"skeleton-health": 12, "greet-endpoint": 13})
    _advance_origin(repo, "epic-9", f"{DOC}/epic-9/lld.md", numbered)

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md").endswith("More.\n")


def test_units_finish_lld_and_a_regate_park_are_never_audited_as_missing_status(repo):
    # Every open non-container unit keeps a Pipeline Status: the audit flags only real gaps.
    gh = _lld_tree()
    _lld_ready(gh, repo)
    tasks = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))["steps"]["create-lld-tasks"]["tasks"]
    paused = min(tasks.values())
    s.cmd_pause_for_epic_regate(gh, paused, 9, 77)

    flagged = {i["issue"]: i["missing"] for i in s.cmd_audit_issues(gh, epic=9)["issues"]}

    assert all("Pipeline Status" not in flagged.get(n, []) for n in tasks.values()), flagged
    assert gh.issues[paused]["status"] == "todo"


def test_finish_lld_never_closes_the_task_when_no_design_pr_was_opened(repo, monkeypatch):
    gh = _lld_tree()
    monkeypatch.setattr(s, "cmd_create_lld_tasks", _must_not_run("create-lld-tasks"))
    monkeypatch.setattr(s, "cmd_close_issue", _must_not_run("close-issue"))

    result = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert result["failed_step"] == "merge-design-pr" and result["completed_steps"] == []
    assert result["ok"] is True  # a structured refusal: exit 0
    assert "no design PR marker" in result["reason"]
    assert list(result["steps"]) == ["merge-design-pr"]
    assert gh.issues[11]["state"] == "OPEN"


def test_finish_lld_stops_before_creating_tasks_when_the_review_is_not_clean(repo, monkeypatch):
    gh = _lld_tree()
    pr = _lld_ready(gh, repo, review="rework")
    monkeypatch.setattr(s, "cmd_create_lld_tasks", _must_not_run("create-lld-tasks"))

    result = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert result["failed_step"] == "merge-design-pr"
    assert "not `clean`" in result["reason"]
    assert gh.merges == [] and gh.prs[pr]["state"] == "OPEN"


def test_finish_lld_stops_when_create_lld_tasks_fails(monkeypatch):
    gh = _lld_tree()
    monkeypatch.setattr(s, "_merge_design_pr_of", lambda *a, **k: {"merged": True})
    monkeypatch.setattr(s, "cmd_create_lld_tasks",
                        lambda *a, **k: {"committed": "abc", "pushed": False, "conflict": True,
                                         "tasks": {"k": 12}, "reason": "push rejected"})
    monkeypatch.setattr(s, "cmd_merge_lld_doc", _must_not_run("merge-lld-doc"))
    monkeypatch.setattr(s, "cmd_close_issue", _must_not_run("close-issue"))

    result = s.cmd_finish_lld(gh, 11, 9)

    assert result["failed_step"] == "create-lld-tasks"
    assert result["completed_steps"] == ["merge-design-pr"]
    assert result["reason"] == "push rejected" and result["closed"] is False


def test_finish_lld_treats_nothing_left_to_create_as_done(monkeypatch):
    gh = _lld_tree()
    monkeypatch.setattr(s, "_merge_design_pr_of", lambda *a, **k: {"already_merged": True})
    monkeypatch.setattr(s, "cmd_create_lld_tasks",
                        lambda *a, **k: {"committed": None, "pushed": False, "tasks": {}})
    monkeypatch.setattr(s, "cmd_merge_lld_doc",
                        lambda *a, **k: {"merged": False, "verified_on_origin": True})
    monkeypatch.setattr(s, "cmd_close_issue", lambda gh, n, **k: {"issue": n, "closed": True})

    result = s.cmd_finish_lld(gh, 11, 9)

    assert result["failed_step"] is None and result["closed"] is True


def test_finish_lld_refuses_a_task_of_another_epic():
    gh = _lld_tree()
    result = s.cmd_finish_lld(gh, 11, 99)
    assert result["refused"] is True
    assert gh.issues[11]["state"] == "OPEN"


# --- open-arch-revision -------------------------------------------------------

def test_open_arch_revision_cuts_a_staged_task_off_the_epic_branch_and_blocks_the_units(repo):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "stage": "development"},
               {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development"})

    result = s.cmd_open_arch_revision(gh, 9, "cache layer", "the deviation", blocks=[12, 13],
                                      repo_path=str(repo))

    rev = result["revision_task"]
    assert result["ok"] is True and result["failed_step"] is None
    assert gh.issues[rev]["title"] == "Architecture revision: cache layer"
    assert gh.issues[rev]["stage"] == "architecture" and gh.issues[rev]["parent"] == 9
    assert result["steps"]["worktree-add"]["base"] == "origin/epic-9"
    assert result["steps"]["epic-worktree"]["branch"] == "epic-9"
    assert gh.blocked_by(12) == [rev] and gh.blocked_by(13) == [rev]

    again = s.cmd_open_arch_revision(gh, 9, "Architecture revision: cache layer", "x",
                                     blocks=[12], repo_path=str(repo))
    assert again["revision_task"] == rev and again["ok"] is True
    assert gh.blocked_by(12) == [rev]


def test_open_arch_revision_blocks_nothing_when_the_worktree_is_refused(monkeypatch):
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9})
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, unit="issue", **k: {"path": "/wt"} if unit == "epic"
                        else {"diverged": True, "reason": "diverged"})

    result = s.cmd_open_arch_revision(gh, 9, "t", "b", blocks=[12])

    assert result["failed_step"] == "worktree-add" and result["ok"] is True
    assert gh.blocked_by(12) == []


# --- start-stage --------------------------------------------------------------

def test_start_stage_adds_the_worktree_before_claiming(repo, monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    order = []
    for name in ("cmd_worktree_add", "cmd_claim"):
        real = getattr(s, name)
        monkeypatch.setattr(s, name, lambda *a, _r=real, _n=name, **k: (order.append(_n),
                                                                        _r(*a, **k))[1])

    result = s.cmd_start_stage(gh, 10, "architecture", repo_path=str(repo))

    # A child of a non-standing Epic first ensures the epic branch + worktree, then its own.
    assert order == ["cmd_worktree_add", "cmd_worktree_add", "cmd_claim"]
    assert result["completed_steps"] == ["check-claimable", "epic-worktree", "worktree-add",
                                         "claim"]
    assert result["claimed"] is True and result["ok"] is True
    assert Path(result["path"]).is_dir()
    assert result["steps"]["worktree-add"]["base"] == "origin/epic-9"
    assert result["steps"]["epic-worktree"]["branch"] == "epic-9"
    assert gh.issues[10]["status"] == "in-progress"


def test_start_stage_refuses_unit_epic_for_a_task(monkeypatch):
    # #1233: `--unit epic` on a phase-Task cut a stray `epic-<n>` branch + worktree.
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))

    with pytest.raises(s.GhError, match="not an Epic"):
        s.cmd_start_stage(gh, 10, "architecture", unit="epic")
    assert gh.issues[10]["status"] is None


def test_start_stage_does_not_claim_when_worktree_add_refuses(monkeypatch):
    # Parentless (main-based): no epic-worktree step, so the child worktree-add is the one to fail.
    gh = _tree({"number": 10, "labels": ["type:task"]})
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, **k: {"diverged": True, "reason": "local has diverged"})

    result = s.cmd_start_stage(gh, 10, "architecture")

    assert result["failed_step"] == "worktree-add" and result["claimed"] is False
    assert result["reason"] == "local has diverged"
    assert gh.issues[10]["status"] is None and gh.comments_on(10) == []


def test_start_stage_stops_at_epic_worktree_when_it_refuses(monkeypatch):
    # A child of a non-standing Epic whose epic-worktree step refuses never reaches its own.
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9})
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, **k: {"diverged": True, "reason": "local has diverged"})

    result = s.cmd_start_stage(gh, 10, "architecture")

    assert result["failed_step"] == "epic-worktree" and result["claimed"] is False
    assert gh.issues[10]["status"] is None and gh.comments_on(10) == []


def test_start_stage_development_with_an_open_pr_stops_before_worktree_and_claim(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "pr-review"})
    gh.prs = {77: {"headRefName": "issue-10"}}
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))

    result = s.cmd_start_stage(gh, 10, "development")

    assert (result["ok"], result["failed_step"], result["claimed"]) == (
        False, "check-claimable", False)
    assert "open PR #77" in result["error"]
    assert gh.issues[10]["stage"] == "pr-review" and gh.comments_on(10) == []


def test_start_stage_development_claims_when_only_another_branch_has_a_pr(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"})
    gh.prs = {77: {"headRefName": "issue-11"}}
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})

    result = s.cmd_start_stage(gh, 10, "development")

    assert result["ok"] is True and result["claimed"] is True
    assert result["completed_steps"] == ["check-claimable", "epic-worktree", "worktree-add",
                                         "claim"]


# --- transition ---------------------------------------------------------------

def _author_doc(gh, repo, issue, path, text="# design\n"):
    """Stand up `issue`'s worktree and leave a committed, pushed doc there, as its agent does."""
    wt = s.cmd_worktree_add(gh, issue, repo_path=str(repo))["path"]
    target = Path(wt) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=wt)
    _git("commit", "-qm", f"add {path}", cwd=wt)
    _git("push", "-q", "origin", f"issue-{issue}", cwd=wt)
    return wt


def test_transition_into_arch_review_verifies_syncs_opens_the_design_pr_and_starts_review(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    wt = _author_doc(gh, repo, 10, f"{DOC}/epic-9/architecture.md")

    result = s.cmd_transition(gh, 10, "arch-review", repo_path=wt)

    assert result["ready"] is True and result["stopped_at"] is None
    assert result["verified_stage"] == "architecture"
    assert result["completed_steps"] == ["verify-exit", "sync-branch", "open-design-pr",
                                         "start-comment"]
    assert result["steps"]["sync-branch"]["base"] == "epic-9"
    pr = result["steps"]["open-design-pr"]["pr"]
    assert (gh.prs[pr]["headRefName"], gh.prs[pr]["baseRefName"]) == ("issue-10", "epic-9")
    assert _upstream(repo, "issue-10")  # sync pushed the branch
    assert any("arch-review stage starting" in c for c in gh.comments_on(10))


def test_transition_into_lld_review_opens_the_lld_design_pr(repo):
    gh = _tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    wt = _author_doc(gh, repo, 11, f"{DOC}/epic-9/lld.md")

    result = s.cmd_transition(gh, 11, "lld-review", repo_path=wt)

    assert result["completed_steps"] == ["verify-exit", "sync-branch", "open-design-pr",
                                         "start-comment"]
    assert result["steps"]["open-design-pr"]["stage"] == "lld"


def test_transition_stops_at_verify_exit_when_the_doc_is_at_the_old_issue_path(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    wt = _author_doc(gh, repo, 10, f"{DOC}/issue-10/architecture.md")

    result = s.cmd_transition(gh, 10, "arch-review", repo_path=wt)

    assert result["stopped_at"] == "verify-exit" and result["ready"] is False
    assert f"{DOC}/epic-9/architecture.md is missing" in result["reason"]
    assert gh.prs == {}


def test_transition_never_opens_a_design_pr_for_a_standing_child(repo):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"},
               epic_labels=("type:epic", "epic:standing"))
    wt = _author_doc(gh, repo, 10, f"{DOC}/issue-10/architecture.md")

    result = s.cmd_transition(gh, 10, "arch-review", repo_path=wt)

    assert result["completed_steps"] == ["verify-exit", "sync-branch", "start-comment"]
    assert result["steps"]["sync-branch"]["base"] == "main"
    assert gh.prs == {}


def test_transition_after_a_non_review_stage_posts_no_start_comment(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"expected_stage_present": True})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: {"synced": True})

    result = s.cmd_transition(gh, 10, "architecture")

    assert result["ready"] is True and "start-comment" not in result["steps"]


def test_transition_stops_on_an_unverified_exit(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"})
    monkeypatch.setattr(s, "cmd_sync_branch", _must_not_run("sync-branch"))
    monkeypatch.setattr(s, "cmd_start_comment", _must_not_run("start-comment"))

    result = s.cmd_transition(gh, 10, "arch-review", repo_path="/nonexistent",
                              runner=lambda argv: "")

    assert result["ready"] is False and result["stopped_at"] == "verify-exit"
    assert result["ok"] is False  # exits 1, as verify-exit does on its own
    assert "expected 'architecture'" in result["reason"]


def test_transition_stops_when_the_pr_review_handoff_marker_is_missing(monkeypatch):
    gh = Gh([{"number": 9, "labels": ["type:epic"]},
             {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "pr-review"}],
            prs={42: {"isDraft": True, "headRefName": "issue-10", "baseRefName": "epic-9"}})
    monkeypatch.setattr(s, "cmd_sync_branch", _must_not_run("sync-branch"))

    result = s.cmd_transition(gh, 10, "pr-review", pr=42, repo_path="/nonexistent",
                              runner=lambda argv: "")

    assert result["stopped_at"] == "verify-exit" and result["ok"] is False  # exit 1
    assert result["steps"]["verify-exit"]["handoff_marker_present"] is False


def test_transition_pr_review_requires_the_pr():
    with pytest.raises(s.GhError, match="--pr"):
        s.cmd_transition(_tree(), 10, "pr-review")


@pytest.mark.parametrize("sync", [
    {"synced": False, "conflict": True, "conflicting_files": ["a.py"]},
    {"synced": False, "base_missing": True, "reason": "no base"},
])
def test_transition_stops_before_the_start_comment_on_a_sync_refusal(monkeypatch, sync):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"expected_stage_present": True})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: sync)
    monkeypatch.setattr(s, "cmd_start_comment", _must_not_run("start-comment"))

    result = s.cmd_transition(gh, 10, "arch-review")

    assert result["stopped_at"] == "sync-branch" and result["ready"] is False
    assert result["ok"] is True  # a structured refusal: exit 0


# --- CLI exit codes -----------------------------------------------------------

def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


def test_cli_start_stage_resumes_a_child_stranded_at_testing_as_development(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "testing",
                "status": "in-progress"})
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})
    code, out = _cli(monkeypatch, gh, ["start-stage", "10", "--role", "testing"])
    assert (code, out["role"], out["claimed"]) == (0, "development", True)
    assert gh.issues[10]["stage"] == "development"


# --- repair-issue --------------------------------------------------------------

def test_repair_issue_sets_every_missing_field_and_is_idempotent():
    gh = _tree({"number": 12, "labels": ["type:task"]})

    first = s.cmd_repair_issue(gh, 12, parent=9)
    second = s.cmd_repair_issue(gh, 12, parent=9)

    assert first == {"issue": 12, "already_set": [], "set": [
        "parent", "issueType", "Pipeline Status", "Priority", "Effort"]}
    i = gh.issues[12]
    assert (i["parent"], i["issue_type"], i["status"], i["priority"], i["effort"]) == (
        9, "Task", "todo", "Medium", "Medium")
    assert second["set"] == [] and len(second["already_set"]) == 5


def test_repair_issue_never_overwrites_parent_type_status_but_priority_effort_flags_override():
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "issue_type": "Bug",
                "status": "in-progress", "priority": "High", "effort": "Low"})
    result = s.cmd_repair_issue(gh, 12, parent=5, type_name="Task", priority="Low",
                                effort="High")
    # parent/type/status keep never-overwrite; an explicit --priority/--effort is set anyway,
    # so a half-created issue can be corrected to the requested values.
    assert result["set"] == ["Priority", "Effort"]
    i = gh.issues[12]
    assert (i["parent"], i["issue_type"], i["status"], i["priority"], i["effort"]) == (
        9, "Bug", "in-progress", "Low", "High")


def test_repair_issue_without_flags_leaves_a_set_priority_and_effort_untouched():
    gh = _tree({"number": 12, "labels": ["type:task"], "parent": 9, "issue_type": "Bug",
                "status": "in-progress", "priority": "High", "effort": "Low"})
    result = s.cmd_repair_issue(gh, 12)
    assert result["set"] == [] and set(result["already_set"]) >= {"Priority", "Effort"}
    i = gh.issues[12]
    assert (i["priority"], i["effort"]) == ("High", "Low")


def test_repair_issue_types_an_epic_but_leaves_its_status_cleared():
    gh = _tree({"number": 12, "labels": ["type:epic"], "priority": "Low", "effort": "Low"})
    result = s.cmd_repair_issue(gh, 12)
    assert result["set"] == ["issueType"]
    assert gh.issues[12]["issue_type"] == "Epic" and gh.issues[12]["status"] is None


def test_repair_issue_refuses_an_unclassifiable_untyped_issue_before_any_write():
    gh = _tree({"number": 12})
    with pytest.raises(s.GhError, match="pass --type"):
        s.cmd_repair_issue(gh, 12, parent=9)
    assert gh.issues[12]["parent"] is None
    assert s.cmd_repair_issue(gh, 12, parent=9, type_name="Bug")["set"][:2] == [
        "parent", "issueType"]


def test_cli_repair_issue(monkeypatch):
    gh = _tree({"number": 12, "labels": ["type:task"]})
    code, out = _cli(monkeypatch, gh, ["repair-issue", "12", "--parent", "9",
                                       "--priority", "high"])
    assert code == 0 and "Priority" in out["set"] and gh.issues[12]["priority"] == "High"


def test_cli_transition_exits_0_on_a_sync_conflict_and_1_on_an_unverified_exit(monkeypatch):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: {"synced": False, "conflict": True})
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"expected_stage_present": True})
    code, out = _cli(monkeypatch, gh, ["transition", "10", "--expect-stage", "arch-review"])
    assert (code, out["stopped_at"]) == (0, "sync-branch")

    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"ok": False, "reason": "no"})
    code, out = _cli(monkeypatch, gh, ["transition", "10", "--expect-stage", "arch-review"])
    assert (code, out["stopped_at"]) == (1, "verify-exit")


def test_cli_cut_phase_tasks_reports_an_operational_failure_with_exit_1(monkeypatch):
    gh = _tree()
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})
    monkeypatch.setattr(s, "cmd_create_issue", _boom("create-issue"))
    code, out = _cli(monkeypatch, gh, ["cut-phase-tasks", "9"])
    assert code == 1
    assert out["failed_step"] == "architecture.create-issue"
    assert "create-issue exploded" in out["error"]


def test_cut_phase_tasks_rerun_after_a_failed_type_write_reuses_the_half_made_task(repo):
    # create-issue links the parent before any other field, so the re-run finds the Task.
    gh = _tree()
    real, failed = gh.set_issue_type, []

    def fail_once(n, type_name):
        if not failed:
            failed.append(n)
            raise s.GhError("type write failed")
        real(n, type_name)
    gh.set_issue_type = fail_once

    first = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))
    second = s.cmd_cut_phase_tasks(gh, 9, repo_path=str(repo))

    assert first["ok"] is False and first["failed_step"] == "architecture.create-issue"
    assert second["ok"] is True and second["architecture_task"] == failed[0]
    assert [i["title"] for i in gh.issues.values()].count("Architecture phase") == 1


# --- file-closing-delta ---------------------------------------------------------

def _delta_ready(monkeypatch):
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})
    return _tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})


def test_file_closing_delta_makes_an_unstaged_bug_child_of_the_epic(monkeypatch):
    gh = _delta_ready(monkeypatch)

    result = s.cmd_file_closing_delta(gh, 9, "Images 500 in dev", "body", effort="Low")

    n = result["delta_issue"]
    i = gh.issues[n]
    assert (result["ok"], i["parent"], i["issue_type"], i["stage"], i["effort"]) == (
        True, 9, "Bug", None, "Low")
    assert i["status"] == "todo" and result["completed_steps"] == ["create-issue"]


def test_file_closing_delta_start_stages_development_and_claims_it(monkeypatch):
    gh = _delta_ready(monkeypatch)

    result = s.cmd_file_closing_delta(gh, 9, "Blocker", "body", start=True)

    i = gh.issues[result["delta_issue"]]
    assert result["completed_steps"] == ["create-issue", "set-stage", "start-stage"]
    assert (i["stage"], i["status"]) == ("development", "in-progress")


def test_file_closing_delta_refuses_a_standing_epic_and_an_unknown_effort(monkeypatch):
    gh = _tree(epic_labels=("type:epic", "epic:standing"))
    assert s.cmd_file_closing_delta(gh, 9, "t", "b")["refused"] is True
    gh = _delta_ready(monkeypatch)
    result = s.cmd_file_closing_delta(gh, 9, "t", "b", effort="Small")
    assert (result["ok"], result["failed_step"], result["delta_issue"]) == (False, "create-issue", None)
    assert "Effort" in result["error"] and not [n for n in gh.issues if n > 10]


def test_cli_file_closing_delta(monkeypatch):
    gh = _delta_ready(monkeypatch)
    code, out = _cli(monkeypatch, gh, ["file-closing-delta", "9", "--title", "t", "--body", "b",
                                       "--effort", "High"])
    assert code == 0 and gh.issues[out["delta_issue"]]["effort"] == "High"
