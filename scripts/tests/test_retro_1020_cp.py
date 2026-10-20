"""Retro 2026-10-20 control-plane regressions: a bare Epic with pre-filed or superseded
children still gets `cut-phase-tasks` (and is not closeable), `mark-blocked` keeps an existing
edge, `create-lld-tasks` wires an edge onto a key added in the same pass, `edit-issue --title`,
a regate-parked unit is re-claimed as its review starts, and a development diff may correct a
closed Epic's design docs.

Each fix pairs a regression test (RED against the pre-fix code) with a positive control."""
import subprocess

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import DOC, FakeGh, _push_doc_branch, _v2_tree, repo  # noqa: F401


def _phase(number, title, state="CLOSED"):
    return {"number": number, "labels": ["type:task"], "parent": 9, "state": state, "title": title}


# --- 1: a bare Epic's phase-Tasks are found by title, never by "any child" ---------------

def test_bare_epic_with_prefiled_tasks_still_gets_cut_phase_tasks():
    """Regression (#3763, #3764): pre-filed Task children hid the missing phase-Tasks."""
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9},
                  {"number": 11, "labels": ["type:task"], "parent": 9})
    gh.issues[9]["parent"] = None  # a bare Epic

    assert s.decide_next_action(gh, 9)["action"] == "cut-phase-tasks"
    flagged = {i["issue"]: i["missing"] for i in s.cmd_audit_issues(gh, epic=9)["issues"]}
    assert "phase-Tasks" in flagged.get(9, [])


def test_bare_epic_with_both_phase_tasks_is_not_cut_again():
    """Positive control."""
    gh = _v2_tree(_phase(10, "Architecture phase"), _phase(11, "LLD phase"),
                  {"number": 12, "labels": ["type:task"], "parent": 9})
    gh.issues[9]["parent"] = None
    assert s.epic_lacks_phase_tasks(gh.issue_list(), 9) is False


def test_architected_epic_never_needs_phase_tasks():
    """Positive control: an Epic past its design phase is never re-cut."""
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9})
    gh.issues[9]["labels"].append("epic:architected")
    assert s.epic_lacks_phase_tasks(gh.issue_list(), 9) is False


def test_epic_with_only_closed_superseded_children_is_not_closeable():
    """Regression (#2987): closed not-planned children made the Epic read as closeable."""
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    gh.path_on_ref = lambda path, ref="main": True
    assert s.cmd_check_epics_closeable(gh)["closeable_epics"] == []
    assert gh.comments_on(9) == []


def test_epic_with_closed_phase_tasks_is_closeable():
    """Positive control."""
    gh = _v2_tree(_phase(10, "Architecture phase"), _phase(11, "LLD phase"),
                  {"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    gh.path_on_ref = lambda path, ref="main": True
    assert [e["epic"] for e in s.cmd_check_epics_closeable(gh)["closeable_epics"]] == [9]


# --- 2: mark-blocked keeps an edge that already exists ------------------------------------

class _EdgeGh(FakeGh):
    def __init__(self, error):
        super().__init__([{"number": 6, "labels": ["type:task"]},
                          {"number": 7, "labels": ["type:task"]}])
        self.error = error

    def add_blocked_by(self, issue_number, blocking_number):
        raise s.GhError(self.error)


def test_mark_blocked_on_an_existing_edge_still_parks_the_unit(monkeypatch):
    """Regression (#3408): GitHub's 422 for a present edge made mark-blocked exit 1."""
    monkeypatch.setattr(s, "_release_parked_unit", lambda *a, **k: {})
    gh = _EdgeGh("HTTP 422: Validation Failed (Target issue has already been taken)")
    gh._run = None
    result = s.cmd_mark_blocked(gh, 7, dep=6)
    assert result["status"] == "blocked" and gh.issues[7]["status"] == "todo"


def test_mark_blocked_still_fails_on_another_error(monkeypatch):
    """Positive control: only the duplicate-edge 422 is swallowed."""
    monkeypatch.setattr(s, "_release_parked_unit", lambda *a, **k: {})
    gh = _EdgeGh("HTTP 404: Not Found")
    gh._run = None
    with pytest.raises(s.GhError):
        s.cmd_mark_blocked(gh, 7, dep=6)


# --- 3: create-lld-tasks wires a numbered Task's edge onto a key added in the same pass ---

_FIRST = (
    "# lld for epic 9\n\n"
    "## Task skeleton-health: Add /health endpoint\n"
    "Design.\n\n"
    "## Footprint\n- `src/health.js`\n\n"
    "## Task greet-endpoint: Add /greet endpoint\n"
    "Design.\n\n"
    "## Footprint\n- `src/greet.js`\n")


def _revise(repo, add_dependency: bool):
    """A revision adds `auth-guard`; with `add_dependency`, the numbered greet Task depends on it."""
    numbered = subprocess.run(["git", "show", f"origin/epic-9:{DOC}/epic-9/lld.md"], cwd=repo,
                              check=True, capture_output=True, text=True).stdout
    if add_dependency:
        head, sep, rest = numbered.partition("<!-- task-key: greet-endpoint -->\n")
        numbered = f"{head}{sep}Depends on: auth-guard\n{rest}"
    numbered += ("\n## Task auth-guard: Guard the endpoints\nDesign.\n\n"
                 "## Footprint\n- `src/auth.js`\n")
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", numbered, base="epic-9")


def test_create_lld_tasks_wires_an_edge_onto_a_key_added_in_the_same_pass(repo):
    """Regression (#3747/#3751): the edge failed until a second call."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _FIRST)
    greet = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))["tasks"]["greet-endpoint"]
    _revise(repo, add_dependency=True)

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    guard = again["tasks"]["auth-guard"]
    assert again["edges_failed"] == []
    assert again["edges_added"] == [{"issue": greet, "on": guard}]
    assert gh.blocked_by(greet) == [guard]


def test_create_lld_tasks_revision_without_new_edges_adds_none(repo):
    """Positive control: a new key nobody depends on adds the Task and no edge."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _FIRST)
    s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    _revise(repo, add_dependency=False)

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in again["created"]] == ["auth-guard"]
    assert again["edges_added"] == [] and again["edges_failed"] == []


# --- 4: edit-issue --title ----------------------------------------------------------------

class _TitleGh(FakeGh):
    def issue_set_title(self, n, title):
        self.issues[n]["title"] = title


def test_edit_issue_retitles_an_issue(tmp_path, monkeypatch):
    """Regression (#3548 rescope): no command could rename a Task."""
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    gh = _TitleGh([{"number": 5, "labels": ["type:task"]}])
    result = s.cmd_edit_issue(gh, 5, title="  Rescoped name ")
    assert result["edited"] == ["title"] and gh.issues[5]["title"] == "Rescoped name"


def test_edit_issue_refuses_an_empty_title(tmp_path, monkeypatch):
    """Positive control: a title is never blanked."""
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    gh = _TitleGh([{"number": 5, "labels": ["type:task"]}])
    with pytest.raises(s.GhError):
        s.cmd_edit_issue(gh, 5, title="  ")
    assert gh.issues[5]["title"] == "issue 5"


# --- 5: a regate-parked unit is re-claimed as its review starts ---------------------------

def _paused_lld(gh):
    s.cmd_pause_for_epic_regate(gh, 11, 9, found_by="lld")


def test_review_start_after_a_regate_resume_lets_the_reviewer_post(tmp_path):
    """Regression (#3434): transition's start comment ended the parked state without a
    claim, so lld-review's post-comment was refused."""
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                   "status": "in-progress"})
    _paused_lld(gh)
    result = s.cmd_start_comment(gh, 11, "lld-review")
    body = tmp_path / "review.md"
    body.write_text("clean")

    assert result.get("reclaimed") is True and gh.issues[11]["status"] == "in-progress"
    assert s.cmd_post_comment(gh, 11, "lld-review", str(body))["posted"] is True


def test_review_start_on_a_claimed_unit_changes_no_status():
    """Positive control: a unit never paused is not touched."""
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                   "status": "in-progress"})
    result = s.cmd_start_comment(gh, 11, "lld-review")
    assert "reclaimed" not in result and gh.issues[11]["status"] == "in-progress"


# --- 6: a development diff may correct a closed Epic's design docs ------------------------

def _doc_tree(epic_state):
    return FakeGh([{"number": 6, "labels": ["type:initiative"]},
                   {"number": 9, "labels": ["type:epic"], "parent": 6, "state": epic_state},
                   {"number": 20, "labels": ["type:task"]}])


def test_dev_pr_may_correct_a_closed_epics_design_doc():
    """Regression (#2092, #3304): a standing child's doc-drift fix had no route to merge."""
    doc = f"{DOC}/epic-9/architecture.md"
    offenders = s.dev_pr_scope_offenders(_doc_tree("CLOSED"), 20, [doc])
    assert offenders["design_docs"] == []


def test_dev_pr_still_refuses_an_open_epics_design_doc():
    """Positive control."""
    doc = f"{DOC}/epic-9/lld.md"
    offenders = s.dev_pr_scope_offenders(_doc_tree("OPEN"), 20, [doc])
    assert offenders["design_docs"] == [doc]
