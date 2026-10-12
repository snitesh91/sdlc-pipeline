"""Retro 2026-10-12 (fork B): an Architecture revision Task may update lld.md sections,
keeping every Task heading."""
import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import DOC, _git, _push_doc_branch, _v2_tree, repo  # noqa: F401

REV = {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
       "status": "in-progress"}
LLD = ("# lld\n\n## Task #12: Save <!-- task-key: save -->\nold body\n\n"
       "## Task #13: List <!-- task-key: list -->\nbody\n")


def _revision_tree(repo, tmp_path, new_lld):
    """`epic-9` carries the numbered lld.md; `issue-10` (the revision) edits it."""
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", LLD)
    _push_doc_branch(repo, "issue-10", f"{DOC}/epic-9/architecture.md", "# arch v2\n",
                     base="epic-9")
    wt = tmp_path / "rev"
    _git("worktree", "add", "-q", str(wt), "issue-10", cwd=repo)
    (wt / DOC / "epic-9" / "lld.md").write_text(new_lld)
    _git("commit", "-qam", "lld section", cwd=wt)
    _git("push", "-q", "origin", "issue-10", cwd=wt)
    return str(wt)


def test_revision_may_edit_an_lld_section_body(repo, tmp_path):
    # Positive control: body edits keep every heading.
    wt = _revision_tree(repo, tmp_path, LLD.replace("old body", "new body"))
    result = s.cmd_verify_exit(_v2_tree(dict(REV)), wt, 10, "architecture")
    assert result.get("ok") is not False and "lld_headings_dropped" not in result


def test_revision_dropping_an_lld_task_heading_is_refused(repo, tmp_path):
    """Regression: a revision that rewrote a heading would silently break `lld-section`."""
    wt = _revision_tree(repo, tmp_path, LLD.replace("## Task #13: List", "## List"))
    result = s.cmd_verify_exit(_v2_tree(dict(REV)), wt, 10, "architecture")
    assert result["ok"] is False and result["lld_headings_dropped"] == ["#13"]
    assert "keep every `## Task` heading" in result["reason"]


def test_lld_task_headings_dropped_ignores_added_and_body_changes():
    assert s.lld_task_headings_dropped(LLD, LLD + "\n## Task new-key: X\n") == []
    assert s.lld_task_headings_dropped(LLD, "# empty\n") == ["#12", "#13"]


# --- epic sync reports what main brought in (stale-locked-design, sync-schema-change) ---

from tests.test_retro_composites import _cut_epic, _epic_tree, runs  # noqa: E402,F401
from tests.test_v2_phase_tasks import _advance_origin  # noqa: E402


def test_epic_sync_reports_design_and_schema_touch(repo, runs):
    """Regression: a shared-package change merged from main staled a locked design silently."""
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "epic-9", f"{DOC}/epic-9/architecture.md",
                    "Uses `packages/outbox/src` for sends.\n")
    _advance_origin(repo, "main", "packages/outbox/src/outbox.entity.ts", "x\n")
    _advance_origin(repo, "main", "apps/other/README.md", "y\n")

    sync = s.sync_epic_if_due(gh, 9, "run-1", str(repo))

    assert sync["synced"] is True
    assert sync["design_touch"] == ["packages/outbox/src/outbox.entity.ts"]
    assert sync["schema_touch"] == ["packages/outbox/src/outbox.entity.ts"]


def test_epic_sync_reports_no_touch_for_an_unrelated_delta(repo, runs):
    # Positive control: main moved on files the design never names.
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "epic-9", f"{DOC}/epic-9/architecture.md", "Uses `packages/outbox`.\n")
    _advance_origin(repo, "main", "apps/other/src/x.ts", "y\n")

    sync = s.sync_epic_if_due(gh, 9, "run-1", str(repo))

    assert sync["synced"] is True
    assert not sync.get("design_touch") and not sync.get("schema_touch")


def test_epic_sync_ignores_a_two_segment_app_root_the_design_names(repo, runs):
    """Regression: naming `apps/api` flagged every sync that touched the app."""
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "epic-9", f"{DOC}/epic-9/architecture.md", "Lives in `apps/api`.\n")
    _advance_origin(repo, "main", "apps/api/src/x.ts", "y\n")
    assert not s.sync_epic_if_due(gh, 9, "run-1", str(repo)).get("design_touch")


def test_a_failed_touch_report_keeps_the_pushed_sync(repo, runs, monkeypatch):
    def boom(*a, **k):
        raise s.GhError("git show failed")
    monkeypatch.setattr(s, "epic_sync_touches", boom)
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "main", "apps/other/src/x.ts", "y\n")
    sync = s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    assert sync["synced"] is True and sync["touches_error"] == "git show failed"
