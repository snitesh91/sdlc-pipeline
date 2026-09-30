"""Retro 2026-09-30 composite/control-plane regressions: start-stage honours the run cap,
composites report `remaining_steps` and finish-lld re-runs safely, `Depends on:` is read only
from a Task's metadata block, footprint deviations can be acknowledged in the PR body, and
`reparent-issue` moves a plain issue between parents.

Each fix pairs a regression test (RED against the pre-fix code) with a positive control."""
import json

import pytest

import sdlc_next as s
from tests.test_composites import _boom, _cli, _lld_ready, _lld_tree, _must_not_run, _tree
from tests.test_v2_phase_tasks import (DOC, FakeGh, _push_doc_branch, _v2_tree,  # noqa: F401
                                       _write_run, repo, run_state)


# --- 1: start-stage --run-id enforces the run cap -------------------------------------

def _dev_child(**extra):
    return _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development",
                  **extra})


def test_start_stage_with_a_run_id_refuses_a_new_unit_at_the_run_cap(run_state, monkeypatch):
    """Regression: the orchestrator chained list-parallel-ready (stop_at_cap) with start-stage
    and claimed units past the cap -- start-stage never checked it."""
    gh = _dev_child()
    _write_run(run_state, [11, 12])
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))
    monkeypatch.setattr(s, "cmd_claim", _must_not_run("claim"))

    result = s.cmd_start_stage(gh, 10, "development", run_id="run-1")

    assert result["failed_step"] == "run-cap" and result["stop_at_cap"] is True
    assert result["ok"] is True  # a structured refusal: exit 0
    assert result["claimed"] is False and "cap" in result["reason"]
    assert "claim" in result["remaining_steps"]
    assert gh.issues[10]["status"] is None and gh.comments_on(10) == []


def test_cli_start_stage_run_cap_refusal_exits_zero(run_state, monkeypatch):
    gh = _dev_child()
    _write_run(run_state, [11, 12])
    monkeypatch.setattr(s, "cmd_worktree_add", _must_not_run("worktree-add"))

    code, out = _cli(monkeypatch, gh, ["start-stage", "10", "--role", "development",
                                       "--run-id", "run-1"])

    assert (code, out["failed_step"], out["stop_at_cap"]) == (0, "run-cap", True)


def test_start_stage_without_a_run_id_is_not_capped(run_state, monkeypatch):
    """Positive control: no --run-id, no cap (unchanged behaviour)."""
    gh = _dev_child()
    _write_run(run_state, [11, 12])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})

    result = s.cmd_start_stage(gh, 10, "development")

    assert result["claimed"] is True and result["failed_step"] is None
    assert "stop_at_cap" not in result


def test_start_stage_at_cap_still_starts_a_unit_already_in_flight(run_state, monkeypatch):
    """Positive control: a unit this run already handed out (here by next-action on the
    Initiative's file) is never refused -- the cap defers only new work."""
    gh = _dev_child()
    _write_run(run_state, [11, 12])
    (run_state / "epic-6.json").write_text(json.dumps(
        {"run_id": "run-1", "terminal": [], "in_flight": {"10": "development"}}))
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})

    result = s.cmd_start_stage(gh, 10, "development", run_id="run-1")

    assert result["claimed"] is True and result["failed_step"] is None


def test_start_stage_under_the_cap_notes_the_unit_in_flight(run_state, monkeypatch):
    gh = _dev_child()
    _write_run(run_state, [11])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt"})

    result = s.cmd_start_stage(gh, 10, "development", run_id="run-1")

    assert result["claimed"] is True
    state = json.loads((run_state / "epic-9.json").read_text())
    assert state["in_flight"] == {"10": "development"}


# --- 2: remaining_steps; finish-lld re-run from the top is safe --------------------------

def test_finish_lld_reports_the_steps_it_never_ran(monkeypatch):
    """Regression: hand-resuming after a failed create-lld-tasks skipped merge-lld-doc --
    the report never said which steps were still owed."""
    gh = _lld_tree()
    monkeypatch.setattr(s, "_merge_design_pr_of", lambda *a, **k: {"merged": True})
    monkeypatch.setattr(s, "cmd_create_lld_tasks", _boom("create-lld-tasks"))
    monkeypatch.setattr(s, "cmd_merge_lld_doc", _must_not_run("merge-lld-doc"))
    monkeypatch.setattr(s, "cmd_close_issue", _must_not_run("close-issue"))

    result = s.cmd_finish_lld(gh, 11, 9)

    assert result["failed_step"] == "create-lld-tasks"
    assert result["remaining_steps"] == ["merge-lld-doc", "close-issue"]


def test_a_successful_composite_has_no_remaining_steps(monkeypatch):
    """Positive control."""
    gh = _lld_tree()
    monkeypatch.setattr(s, "_merge_design_pr_of", lambda *a, **k: {"already_merged": True})
    monkeypatch.setattr(s, "cmd_create_lld_tasks",
                        lambda *a, **k: {"committed": None, "pushed": False, "tasks": {}})
    monkeypatch.setattr(s, "cmd_merge_lld_doc",
                        lambda *a, **k: {"merged": False, "verified_on_origin": True})
    monkeypatch.setattr(s, "cmd_close_issue", lambda gh, n, **k: {"issue": n, "closed": True})

    result = s.cmd_finish_lld(gh, 11, 9)

    assert result["failed_step"] is None and result["remaining_steps"] == []


def test_close_issue_on_an_already_closed_issue_skips_the_close(repo):
    gh = _lld_tree()
    gh.issues[11]["state"] = "CLOSED"
    gh.issue_close = _must_not_run("gh issue close")

    result = s.cmd_close_issue(gh, 11, repo_path=str(repo))

    assert result["already_closed"] is True and result["closed"] is True
    assert gh.issues[11]["status"] == "done"  # the idempotent bookkeeping still ran


def test_close_issue_on_an_open_issue_closes_it(repo):
    """Positive control."""
    gh = _lld_tree()
    result = s.cmd_close_issue(gh, 11, repo_path=str(repo))
    assert gh.issues[11]["state"] == "CLOSED" and result.get("already_closed", False) is False


def test_finish_lld_rerun_after_a_hand_close_still_architects_the_epic(repo, monkeypatch):
    """The incident: create-lld-tasks ran, the Task was closed by hand, merge-lld-doc never
    ran. Re-running finish-lld from the top finishes the Epic."""
    gh = _lld_tree()
    _lld_ready(gh, repo)
    real_merge = s.cmd_merge_lld_doc
    monkeypatch.setattr(s, "cmd_merge_lld_doc", _boom("merge-lld-doc"))
    first = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))
    assert first["failed_step"] == "merge-lld-doc" and first["remaining_steps"] == ["close-issue"]
    gh.issues[11]["state"] = "CLOSED"  # the operator closed it by hand
    monkeypatch.setattr(s, "cmd_merge_lld_doc", real_merge)

    again = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert again["failed_step"] is None and again["remaining_steps"] == []
    assert "epic:architected" in gh.issues[9]["labels"]
    assert again["steps"]["close-issue"]["already_closed"] is True


_CARVED = (
    "# lld for epic 9\n\n"
    "## Task skeleton-health: Add /health endpoint\n"
    "Design.\n\n"
    "## Footprint\n- `src/health.js`\n\n"
    "## Task greet-endpoint: Add /greet endpoint\n"
    "Depends on: skeleton-health\n\n"
    "## Footprint\n- `src/greet.js`\n")


def test_create_lld_tasks_rerun_adds_a_blocked_by_edge_the_first_pass_missed(repo):
    """Regression: an already-numbered doc returned `tasks: {}` and never retried edges."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED)
    first = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    health, greet = first["tasks"]["skeleton-health"], first["tasks"]["greet-endpoint"]
    gh.blocked[greet] = []  # the first pass's edge never landed

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert again["edges_added"] == [{"issue": greet, "on": health}]
    assert gh.blocked_by(greet) == [health]
    assert again["tasks"] == {} and again["created"] == []


def test_create_lld_tasks_rerun_adds_no_edge_that_already_exists(repo):
    """Positive control: a complete earlier pass re-runs as a no-op."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED)
    first = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    greet = first["tasks"]["greet-endpoint"]

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert again["edges_added"] == [] and again.get("edges_failed", []) == []
    assert gh.blocked[greet] == [first["tasks"]["skeleton-health"]]  # never re-added


# --- 3: `Depends on:` is read only from the Task's metadata block ------------------------

def test_a_depends_on_quoted_in_a_later_subsection_is_not_a_dependency():
    """Regression: a narrative line quoting another Task's `Depends on:` was parsed (self-dep)."""
    section = ("\nDepends on: copy-core\n\n"
               "### Task-local decisions\n\n"
               "1. The sibling carries the line that keeps the two apart:\n"
               "`Depends on: copy-ui`\n")
    assert s.parse_task_depends_on(section) == ["copy-core"]
    assert s.task_dependency_defect(section) is None


def test_a_wrapped_prose_line_starting_with_depends_on_is_not_a_dependency():
    section = ("\nIntro prose that quotes the sibling's metadata line:\n"
               "`Depends on: copy-ui`.\n\n"
               "Depends on: copy-core\n")
    assert s.parse_task_depends_on(section) == ["copy-core"]


def test_a_prose_mention_outside_the_metadata_block_is_no_defect():
    section = ("\nDesign.\n\n### Notes\n\nThe router depends on: the old session cache.\n")
    assert s.task_dependency_defect(section) is None


def test_depends_on_is_not_read_after_a_mid_line_separator():
    section = "\nPriority: High / Depends on: copy-core\n"
    assert s.parse_task_depends_on(section) == []
    assert s.task_dependency_defect(section) is not None  # named, not parsed: loud


def test_the_first_depends_on_line_wins():
    assert s.parse_task_depends_on("\nDepends on: a-one\nDepends on: b-two\n") == ["a-one"]


def test_a_hyphenated_key_containing_and_stays_one_key():
    assert s.parse_task_depends_on("\nDepends on: search-and-filter, cart\n") == [
        "search-and-filter", "cart"]


def test_a_partially_parsed_depends_on_is_a_warning():
    section = "\nDepends on: copy-core, the router module\n"
    assert s.parse_task_depends_on(section) == ["copy-core"]
    assert s.task_dependency_defect(section) is not None


def test_create_lld_tasks_ignores_a_quoted_depends_on_in_a_subsection(repo):
    doc = ("# lld\n\n"
           "## Task copy-core: Copy core\nDesign.\n\n## Footprint\n- `src/copy.js`\n\n"
           "## Task copy-ui: Copy UI\n"
           "Intro paragraph.\n\n"
           "Depends on: copy-core\nEffort: Low\n\n"
           "### Overlap\n\nThe core Task never carries this Task's line:\n"
           "`Depends on: copy-ui`\n\n"
           "## Footprint\n- `src/ui.js`\n")
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", doc)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    core, ui = result["tasks"]["copy-core"], result["tasks"]["copy-ui"]
    assert result["skipped_sections"] == [] and result["dependency_parse_warnings"] == []
    assert result["blocked_by"] == [{"issue": ui, "on": core}]
    assert result["blocked_by_failed"] == []


@pytest.mark.parametrize("section,keys", [
    ("\nDepends on: copy-core, paste-core\n", ["copy-core", "paste-core"]),
    ("\n- **Depends on:** none\n", []),
    ("\n`Depends on: copy-core`\n", ["copy-core"]),
    ("\nIntro.\n\n**Depends on:** copy-core\nEffort: Medium\n\n### Search\n", ["copy-core"]),
])
def test_normal_depends_on_lines_still_parse(section, keys):
    """Positive controls: plain, none, backtick-wrapped, and after an intro paragraph."""
    assert s.parse_task_depends_on(section) == keys
    assert s.task_dependency_defect(section) is None


# --- 4: acknowledged footprint deviations -----------------------------------------------

_ACK_BODY = ("What was built.\n\n## Footprint deviations\n\n"
             "- `src/gadget.py` — the shared registry must list the new widget\n\n"
             "## Testing\nran it\n")


def _offenders(monkeypatch, design=(), foreign=()):
    monkeypatch.setattr(s, "dev_pr_scope_offenders", lambda *a, **k: {
        "design_docs": list(design), "foreign_footprint": list(foreign)})


class _OpenDevGh:
    def __init__(self):
        self.created = False

    def pr_list_for_branch(self, branch, state="open"):
        return []

    def issue_list(self):
        return FakeGh([{"number": 9, "labels": ["type:epic"]},
                       {"number": 10, "labels": ["type:task"], "parent": 9}]).issue_list()

    def files_since(self, sha, branch):
        return []

    def pr_create(self, base, head, title, body, draft):
        self.created = True
        return 42

    def set_stage_field(self, n, stage):
        pass

    def issue_comment(self, n, body):
        pass


def test_open_dev_pr_accepts_an_acknowledged_foreign_footprint_path(monkeypatch):
    """Regression: an inherent, documented foreign-footprint touch had no way through."""
    _offenders(monkeypatch, foreign=["src/gadget.py"])
    gh = _OpenDevGh()

    result = s.cmd_open_dev_pr(gh, 10, "t", _ACK_BODY, "s")

    assert result["created"] is True and gh.created is True
    assert result["acknowledged_deviations"] == ["src/gadget.py"]


def test_open_dev_pr_still_refuses_an_unacknowledged_foreign_path(monkeypatch):
    _offenders(monkeypatch, foreign=["src/gadget.py", "src/other.py"])
    gh = _OpenDevGh()

    result = s.cmd_open_dev_pr(gh, 10, "t", _ACK_BODY, "s")

    assert result["refused"] is True and gh.created is False
    assert result["offending_footprint_paths"] == ["src/other.py"]
    assert "## Footprint deviations" in result["reason"]


def test_open_dev_pr_never_accepts_a_listed_design_doc(monkeypatch):
    doc = f"{DOC}/epic-9/lld.md"
    _offenders(monkeypatch, design=[doc])
    gh = _OpenDevGh()
    body = f"x\n\n## Footprint deviations\n- `{doc}` — needed\n"

    result = s.cmd_open_dev_pr(gh, 10, "t", body, "s")

    assert result["refused"] is True and gh.created is False
    assert result["offending_design_docs"] == [doc]


def test_acknowledged_deviations_parse_only_backticked_bullets_in_the_section():
    body = ("- `src/outside.py` — not in the section\n\n"
            "## Footprint deviations\n"
            "- `src/a.py` — why\n"
            "* `src/b.py`: why\n"
            "- src/c.py — no backticks\n\n"
            "## Next\n- `src/d.py` — another section\n")
    assert s.acknowledged_footprint_deviations(body) == ["src/a.py", "src/b.py"]


def _verify_exit_tree(repo, body):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "pr-review",
                  "comments": ["<!-- stage-transition: development->pr-review "
                               "@ 2026-09-30T00:00:00Z -->"]}],
                prs={42: {"headRefName": "issue-10", "baseRefName": "epic-9", "isDraft": True,
                          "body": body}})
    return gh


def test_verify_exit_accepts_an_acknowledged_foreign_footprint_path(repo, monkeypatch):
    _offenders(monkeypatch, foreign=["src/gadget.py"])
    gh = _verify_exit_tree(repo, _ACK_BODY)

    result = s.cmd_verify_exit(gh, str(repo), 10, "pr-review", 42)

    assert result["acknowledged_deviations"] == ["src/gadget.py"]
    assert result["problems"] == []


def test_verify_exit_refuses_an_unacknowledged_foreign_footprint_path(repo, monkeypatch):
    """Positive control: without the section the touch is still refused, with a how-to."""
    _offenders(monkeypatch, foreign=["src/gadget.py"])
    gh = _verify_exit_tree(repo, "What was built.\n")

    result = s.cmd_verify_exit(gh, str(repo), 10, "pr-review", 42)

    assert result["ok"] is False
    assert any("## Footprint deviations" in p for p in result["problems"])


# --- 5: reparent-issue ------------------------------------------------------------------

def _two_epics():
    return FakeGh([{"number": 9, "labels": ["type:epic", "epic:standing"]},
                   {"number": 20, "labels": ["type:epic", "epic:standing"]},
                   {"number": 30, "labels": ["type:task"], "issue_type": "Bug", "parent": 9},
                   {"number": 31, "labels": ["type:task"]}])


def test_reparent_issue_moves_a_bug_between_two_epics_with_an_audit_trail():
    gh = _two_epics()

    result = s.cmd_reparent_issue(gh, 30, 20, reason="belongs to search")

    assert result == {"issue": 30, "old_parent": 9, "new_parent": 20, "moved": True}
    assert gh.issues[30]["parent"] == 20
    for n in (30, 9, 20):
        assert any("belongs to search" in c for c in gh.comments_on(n)), n


def test_reparent_issue_is_idempotent_when_already_under_the_parent():
    gh = _two_epics()
    s.cmd_reparent_issue(gh, 30, 20)
    comments = {n: list(gh.comments_on(n)) for n in (30, 9, 20)}

    again = s.cmd_reparent_issue(gh, 30, 20)

    assert again["already_parented"] is True and again["moved"] is False
    assert {n: gh.comments_on(n) for n in (30, 9, 20)} == comments


def test_reparent_issue_links_a_parentless_issue():
    gh = _two_epics()
    result = s.cmd_reparent_issue(gh, 31, 20)
    assert result["old_parent"] is None and result["moved"] is True
    assert gh.issues[31]["parent"] == 20


def test_reparent_issue_refuses_an_epic():
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 7, "labels": ["type:initiative"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6}])
    result = s.cmd_reparent_issue(gh, 9, 7)
    assert result["refused"] is True and "detach-epic" in result["reason"]
    assert gh.issues[9]["parent"] == 6


def test_cli_reparent_issue(monkeypatch):
    gh = _two_epics()
    code, out = _cli(monkeypatch, gh, ["reparent-issue", "30", "--parent", "20",
                                       "--reason", "moved"])
    assert code == 0 and out["moved"] is True and gh.issues[30]["parent"] == 20
