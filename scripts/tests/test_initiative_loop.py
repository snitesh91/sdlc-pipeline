"""The Initiative's Epic loop (`run-epic` / `cut-phase-tasks`), the audit's phase-Task check,
`merge-gate`, the design-review head SHA, and a design PR a human merged before its gate."""
import io
import json
import os
from contextlib import redirect_stdout

import pytest

import sdlc_next as s
from tests.test_design_pr import ARCH, LLD, _arch, _cli
from tests.test_v2_phase_tasks import (DOC, FakeGh, _CARVED_LLD, _design_branch, _git,
                                       _open_design, _push_doc_branch, _review, _v2_tree, repo)

INIT = {"number": 6, "labels": ["type:initiative"]}


def _epic(n, *labels, **kw):
    return {"number": n, "labels": ["type:epic", *labels], "parent": 6, **kw}


def _phases(epic, first):
    """Both phase-Tasks of `epic`, as `cut-phase-tasks` leaves them."""
    return [{"number": first, "labels": ["type:task"], "parent": epic, "title": "Architecture phase"},
            {"number": first + 1, "labels": ["type:task"], "parent": epic, "title": "LLD phase"}]


def _initiative(*extra):
    return FakeGh([INIT, *extra])


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


# --- item 1: the Initiative descends into its Epics -----------------------------------------

def test_the_lowest_numbered_open_unblocked_epic_is_run_first():
    gh = _initiative(_epic(12), _epic(10), *_phases(10, 100), *_phases(12, 110))

    result = s.decide_next_action(gh, 6)

    assert (result["action"], result["epic"], result["initiative"]) == ("run-epic", 10, 6)
    assert result["unit"] == "epic" and "next-action 10" in result["reason"]


def test_an_epic_blocked_by_an_open_epic_waits_and_runs_once_that_closes():
    gh = _initiative(_epic(10), _epic(11), *_phases(10, 100), *_phases(11, 110))
    gh.blocked[11] = [10]

    assert s.decide_next_action(gh, 6)["epic"] == 10
    gh.issues[10]["state"] = "CLOSED"
    assert s.decide_next_action(gh, 6)["epic"] == 11


def test_a_blocked_epic_is_skipped_for_an_independent_one():
    gh = _initiative(_epic(10), _epic(11), _epic(12), *_phases(10, 100), *_phases(11, 110),
                     *_phases(12, 120))
    gh.blocked[10] = [99]  # blocked by a non-Epic issue
    gh.issues[99] = {**gh.issues[6], "number": 99, "labels": ["type:task"], "parent": None}
    gh.blocked[11] = [12]

    assert s.decide_next_action(gh, 6)["epic"] == 12


def test_skip_epic_parks_a_stalled_epic_for_this_run():
    gh = _initiative(_epic(10), _epic(11), *_phases(10, 100), *_phases(11, 110))

    result = s.decide_next_action(gh, 6, skip_epics=[10])

    assert result["epic"] == 11


def test_the_initiatives_own_roadmap_children_come_before_its_epics():
    gh = _initiative({"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product"},
                     _epic(10), *_phases(10, 100))

    result = s.decide_next_action(gh, 6)

    assert (result["action"], result["issue"]) == ("delegate", 7)


def test_a_standing_epic_is_run_without_cutting_phase_tasks():
    gh = _initiative(_epic(10, "epic:standing"))

    assert s.decide_next_action(gh, 6)["action"] == "run-epic"


def test_a_legacy_epic_is_never_descended_into():
    gh = _initiative(_epic(10, "epic:legacy"), _epic(11), *_phases(11, 110))

    assert s.decide_next_action(gh, 6)["epic"] == 11


def test_epics_are_found_by_issue_type_alone_never_by_label(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "classification", {
        "initiative": {"field": "issueType", "value": "Initiative"},
        "epic": {"field": "issueType", "value": "Epic"},
        "task": {"field": "issueType", "value": "Task"}})
    gh = FakeGh([
        {"number": 6, "issue_type": "Initiative"},
        {"number": 10, "issue_type": "Epic", "parent": 6},
        {"number": 100, "issue_type": "Task", "parent": 10, "title": "Architecture phase"},
        {"number": 101, "issue_type": "Task", "parent": 10, "title": "LLD phase"},
        {"number": 11, "issue_type": "Epic", "parent": 6}])

    first = s.decide_next_action(gh, 6)
    gh.issues[10]["state"] = "CLOSED"
    second = s.decide_next_action(gh, 6)

    assert (first["action"], first["epic"]) == ("run-epic", 10)
    assert (second["action"], second["epic"]) == ("cut-phase-tasks", 11)


# --- item 1: one run-id, one cap, across Epics -------------------------------------------------

def _drive(epic, run_id, terminal):
    s._write_run_state(epic, {"run_id": run_id, "terminal": terminal})


def test_the_run_cap_is_shared_by_every_epic_of_the_run(monkeypatch):
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 3)
    gh = _initiative(_epic(10), _epic(11), *_phases(10, 100), *_phases(11, 110))
    _drive(10, "run-a", [201, 202])
    assert s.decide_next_action(gh, 6, run_id="run-a")["action"] == "run-epic"

    _drive(11, "run-a", [203])
    capped = s.decide_next_action(gh, 6, run_id="run-a")

    assert capped["action"] == "stop-at-cap"
    assert sorted(capped["completed"]) == [201, 202, 203] and "#10" in capped["reason"]
    # another run's units do not count against this run, and no run-id means no cap
    assert s.decide_next_action(gh, 6, run_id="run-b")["action"] == "run-epic"
    assert s.decide_next_action(gh, 6)["action"] == "run-epic"


def test_a_descended_epic_stops_at_the_cap_the_whole_run_reached(monkeypatch):
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 2)
    gh = _v2_tree({"number": 12, "labels": ["type:task"], "parent": 9, "stage": "development"},
                  {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development"})
    gh.issues[9]["labels"].append("epic:architected")
    _drive(8, "run-a", [201])   # another Epic's unit under the same run
    _drive(9, "run-a", [202])

    result = s.decide_next_action(gh, 9, run_id="run-a")

    assert result["action"] == "stop-at-cap" and sorted(result["completed"]) == [201, 202]


# --- item 2: auto-cut phase-Tasks, and what `none` says -----------------------------------------

def test_an_epic_with_no_phase_tasks_is_cut_before_it_is_run():
    gh = _initiative(_epic(10))

    result = s.decide_next_action(gh, 6)

    assert (result["action"], result["epic"], result["unit"]) == ("cut-phase-tasks", 10, "epic")
    assert "cut-phase-tasks 10" in result["reason"]


def test_a_half_made_cut_is_repaired_by_cutting_again():
    gh = _initiative(_epic(10), _phases(10, 100)[0])

    assert s.decide_next_action(gh, 6)["action"] == "cut-phase-tasks"


def test_a_blocked_epic_without_phase_tasks_waits_instead_of_being_cut():
    gh = _initiative(_epic(10), *_phases(10, 100), _epic(11))
    gh.blocked[11] = [10]

    assert s.decide_next_action(gh, 6)["action"] == "run-epic"
    gh.issues[10]["state"] = "CLOSED"
    assert s.decide_next_action(gh, 6)["action"] == "cut-phase-tasks"


def test_none_names_what_each_open_epic_is_waiting_on():
    gh = _initiative(_epic(10), _epic(11), _epic(12, "epic:legacy"), _epic(13))
    gh.issues[99] = {**gh.issues[6], "number": 99, "labels": ["type:task"], "parent": None}
    for n in (10, 11):
        gh.blocked[n] = [99]
    gh.blocked[13] = [99]

    result = s.decide_next_action(gh, 6, skip_epics=[])

    assert result["action"] == "none"
    assert "4 cut Epic(s) open, none runnable" in result["reason"]
    for needle in ("#10 blocked by #99", "#12 not driven", "#13 blocked by #99"):
        assert needle in result["reason"]


def test_none_reports_a_parked_epic_and_the_closed_count():
    gh = _initiative(_epic(10), *_phases(10, 100), _epic(11, state="CLOSED"))

    result = s.decide_next_action(gh, 6, skip_epics=[10])

    assert result["action"] == "none"
    assert "#10 parked this run" in result["reason"] and "(1 closed)" in result["reason"]


def test_an_initiative_with_every_epic_closed_still_reports_the_close_path():
    gh = _initiative(_epic(10, state="CLOSED"))

    assert "ready for initiative-close" in s.decide_next_action(gh, 6)["reason"]


def test_audit_flags_open_epics_with_no_phase_tasks_and_prints_the_repair():
    gh = _initiative(_epic(10), _epic(11), *_phases(11, 110), _epic(12, "epic:standing"),
                     _epic(13, "epic:legacy"), _epic(14, state="CLOSED"))

    flagged = {i["issue"]: i for i in s.cmd_audit_issues(gh, 6)["issues"]
               if "phase-Tasks" in i["missing"]}

    assert list(flagged) == [10]
    assert flagged[10]["repair"].endswith("cut-phase-tasks 10 --repo-path <p>")


def test_audit_scopes_to_an_initiative_from_the_cli(monkeypatch):
    gh = _initiative(_epic(10), {"number": 30, "labels": ["type:epic"]})

    code, out = _cli(monkeypatch, gh, ["audit-issues", "--initiative", "6"])

    assert code == 0
    assert [i["issue"] for i in out["issues"] if "phase-Tasks" in i["missing"]] == [10]


def test_next_action_cli_takes_skip_epic(monkeypatch):
    gh = _initiative(_epic(10), _epic(11), *_phases(10, 100), *_phases(11, 110))

    code, out = _cli(monkeypatch, gh, ["next-action", "6", "--skip-epic", "10"])

    assert (code, out["epic"], out["action"]) == (0, 11, "run-epic")


def test_create_issue_adds_native_blocked_by_edges_for_an_epics_wave_order():
    gh = _initiative(_epic(10))

    result = s.cmd_create_issue(gh, "Epic 2", "b", 6, [], "Task", blocked_by=[10])

    assert result["blocked_by"] == [10] and gh.blocked[result["issue"]] == [10]
    # an edge that already exists is not re-added (re-adding one errors on GitHub)
    s._add_blocked_by_once(gh, result["issue"], 10)
    assert gh.blocked[result["issue"]] == [10]


def test_create_issue_reports_a_failed_edge_with_the_command_that_finishes_it():
    gh = _initiative(_epic(10))
    gh.add_blocked_by = lambda n, d: (_ for _ in ()).throw(s.GhError("boom"))

    result = s.cmd_create_issue(gh, "Epic 2", "b", 6, [], "Task", blocked_by=[10])

    assert result["ok"] is False and result["failed_step"] == "add_blocked_by:10"
    assert f"add-blocked-by {result['issue']} --on 10" in result["reason"]


def test_create_issue_cli_wires_blocked_by(monkeypatch):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_create_issue", lambda gh, *a: seen.append(a) or {})

    assert s.main(["create-issue", "--title", "t", "--body", "b", "--parent", "6",
                   "--type", "Epic", "--blocked-by", "10", "--blocked-by", "11"]) == 0
    assert seen[0][-1] == [10, 11]


# --- item 5a: the review names the head it covered -----------------------------------------------

def _lld_review_ready(gh, repo):
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")
    return pr


def _push_more(repo, issue, path, text):
    _push_doc_branch(repo, f"issue-{issue}", path, text, base=f"issue-{issue}")


def test_a_design_pr_whose_doc_changed_after_the_review_is_not_merged(repo):
    gh = _v2_tree(dict(LLD))
    pr = _lld_review_ready(gh, repo)
    _push_more(repo, 11, f"{DOC}/epic-9/lld.md", _CARVED_LLD + "\nsneaked in after review\n")

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is False and result["review_stale"] is True
    assert "re-run the `lld-review` review" in result["reason"] and gh.merges == []
    # re-running the review on the new head records a fresh sha and lets it merge
    _review(gh, 11, "lld-review")
    assert s.cmd_merge_design_pr(gh, pr, 11, str(repo))["merged"] is True


def test_a_head_that_moved_without_touching_the_doc_still_merges(repo):
    gh = _v2_tree(dict(LLD))
    pr = _lld_review_ready(gh, repo)
    _push_more(repo, 11, "src/unrelated.js", "// a base-only sync\n")

    assert s.cmd_merge_design_pr(gh, pr, 11, str(repo))["merged"] is True


def test_a_review_recorded_without_a_head_sha_is_stale(repo):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    gh.issue_comment(11, "clean\n\n<!-- design-review-outcome: clean:lld-review @ 2026-09-19T00:00:00Z -->")

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is False and result["review_stale"] is True
    assert "names no reviewed head SHA" in result["reason"]


def test_the_sha_field_does_not_disturb_the_escalation_counts():
    comments = [{"body": "<!-- design-review-outcome: rework:lld-review same-class:true "
                         "sha:abcdef1234567 @ 2026-09-19T00:00:00Z -->"},
                {"body": "<!-- design-review-outcome: clean:lld-review sha:abcdef1234567 @ x -->"}]

    assert s.last_design_review_outcome(comments, "lld-review") == (1, "clean")
    assert s.last_design_review_sha(comments, "lld-review") == "abcdef1234567"
    first = s._DESIGN_REVIEW_OUTCOME_MARKER.search(comments[0]["body"])
    assert (first.group(1), first.group(2), first.group(3)) == ("rework", "lld-review", "true")


# --- item 5b: a human merged the design PR before Gate B opened -----------------------------------

def _merged_early(repo, issue=10, doc="architecture.md", text="# arch\n", task=ARCH):
    gh = _v2_tree(dict(task))
    pr = _open_design(gh, repo, issue, doc, text)
    gh.pr_merge(pr, delete_branch=False)   # the human's squash-merge into epic-9
    return gh, pr


def test_next_action_completes_an_architecture_task_whose_design_pr_a_human_merged(repo):
    gh, pr = _merged_early(repo)

    result = s.decide_next_action(gh, 9)

    assert result == {"action": "pass-gate", "issue": 10, "unit": "issue", "design_pr": pr,
                      "gate_pr": pr, "stage": "architecture"}


def test_pass_gate_closes_the_task_from_the_merged_design_pr_with_no_gate_marker(repo):
    gh, pr = _merged_early(repo)

    result = s.cmd_pass_gate(gh, str(repo), 10, pr, "architecture")

    assert result["phase_task_complete"] is True and gh.issues[10]["state"] == "CLOSED"


def test_pass_gate_still_refuses_a_pr_that_is_neither_the_gate_nor_a_merged_design_pr(repo):
    gh, pr = _arch(repo)   # design PR still OPEN, no gate opened

    with pytest.raises(s.GhError, match="no gate-pr marker"):
        s.cmd_pass_gate(gh, str(repo), 10, pr, "architecture")
    gh.pr_merge(pr, delete_branch=False)
    with pytest.raises(s.GhError, match="no gate-pr marker"):
        s.cmd_pass_gate(gh, str(repo), 10, pr + 1, "architecture")


def test_next_action_finishes_an_lld_task_whose_design_pr_a_human_merged(repo):
    gh, pr = _merged_early(repo, 11, "lld.md", _CARVED_LLD, LLD)

    result = s.decide_next_action(gh, 9)

    assert (result["action"], result["issue"], result["epic"]) == ("finish-lld", 11, 9)
    assert s.cmd_finish_lld(gh, 11, 9, str(repo))["ok"] is True


def test_next_action_still_resumes_a_dead_session_when_the_design_pr_is_open(repo):
    gh, pr = _arch(repo)

    assert s.decide_next_action(gh, 9)["action"] == "resume"


def test_open_gate_refuses_a_design_pr_a_human_already_merged(repo):
    gh, pr = _merged_early(repo)

    result = s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md",
                             "development", "x")

    assert result["refused"] is True and f"pass-gate 10 --gate-pr {pr}" in result["reason"]
    assert gh.issues[10]["status"] == "in-progress"


# --- item 3: merge-gate -----------------------------------------------------------------------

def _gate_tree(parent_labels, stage="product", base="main", body="Doc-only gate.",
               pr_extra=None):
    """One issue (#91) at an open gate whose PR is #40."""
    nodes = [{"number": 91, "labels": ["type:task"], "stage": stage,
              "status": "awaiting-human-review", "comments": [f"<!-- gate-pr: {stage}:40 -->"]}]
    if parent_labels is not None:
        nodes = [{"number": 90, "labels": parent_labels}, {**nodes[0], "parent": 90}]
    return FakeGh(nodes, {40: {"headRefName": "issue-91", "baseRefName": base, "body": body,
                               "files": ["docs/sdlc/issue-91/product.md"], **(pr_extra or {})}})


def _merge_gate(gh, **kw):
    kw.setdefault("operator_confirmed", True)
    return s.cmd_merge_gate(gh, 40, 91, kw.pop("stage", "product"), **kw)


def test_merge_gate_refuses_without_the_operators_confirmation():
    gh = _gate_tree(None)

    result = _merge_gate(gh, operator_confirmed=False)

    assert result["merged"] is False and result["refused"] is True
    assert "--operator-confirmed" in result["reason"] and gh.merges == []


@pytest.mark.parametrize("parent,method", [
    (["type:epic", "epic:standing"], "merge"),   # a standing child's gate is never squashed
    (None, "merge"),                              # nor a parentless issue's
    (["type:initiative"], "squash"),              # a Roadmap Task's may be
])
def test_merge_gate_merges_a_gate_pr_with_the_gate_merge_semantics_and_keeps_the_branch(
        parent, method):
    gh = _gate_tree(parent)

    result = _merge_gate(gh)

    assert result["merged"] is True and result["method"] == method
    assert gh.merges == [(40, False)] and gh.merge_methods == [method]
    assert "pass-gate 91 --gate-pr 40 --stage product" in result["next"]
    assert "operator's instruction" in gh.comments_on(91)[-1]


def test_merge_gate_squashes_a_human_gated_design_pr_into_the_epic_branch(repo):
    gh, pr = _arch(repo)
    s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md", "development", "x")

    result = s.cmd_merge_gate(gh, pr, 10, "architecture", operator_confirmed=True)

    assert result["merged"] is True and result["design_pr"] is True and result["method"] == "squash"
    assert gh.merges == [(pr, False)]
    # the normal path picks it up: next-action -> pass-gate closes the Task
    assert s.decide_next_action(gh, 9)["action"] == "pass-gate"


@pytest.mark.parametrize("kw,needle", [
    ({"body": "Work.\n\nCloses #91"}, "development PR"),
    ({"base": "release"}, "not main"),
])
def test_merge_gate_refuses_a_pr_that_is_not_an_open_gate(kw, needle):
    gh = _gate_tree(None, **kw)

    result = _merge_gate(gh)

    assert result["merged"] is False and needle in result["reason"] and gh.merges == []


def test_merge_gate_refuses_a_pr_the_issue_is_not_gated_on_and_a_wrong_stage():
    gh = _gate_tree(None)
    wrong_stage = _merge_gate(gh, stage="architecture")
    gh.issues[91]["comments"] = ["<!-- gate-pr: product:41 -->"]
    other_pr = _merge_gate(gh)

    assert wrong_stage["refused"] and "not #91's architecture gate" in wrong_stage["reason"]
    assert other_pr["refused"] and gh.merges == []


def test_merge_gate_refuses_an_issue_that_has_no_open_gate():
    gh = _gate_tree(None)
    gh.issues[91]["status"] = "in-progress"

    result = _merge_gate(gh)

    assert result["refused"] is True and "not currently awaiting-human-review" in result["reason"]


def test_merge_gate_refuses_behind_base_with_exit_zero(monkeypatch):
    gh = _gate_tree(None)
    gh.behind["issue-91"] = 2

    code, out = _cli(monkeypatch, gh, ["merge-gate", "40", "--issue", "91", "--stage", "product",
                                       "--operator-confirmed"])

    assert code == 0 and out["merged"] is False and out["behind_base"] == 2
    assert "sync-branch 91" in out["reason"] and gh.merges == []


@pytest.mark.parametrize("checks,needle", [
    ([{"name": "ci", "bucket": "fail", "workflow": "CI"}], "status=failed"),
    ([{"name": "ci", "bucket": "pending", "workflow": "CI"}], "status=pending"),
])
def test_merge_gate_needs_green_checks(checks, needle):
    gh = _gate_tree(None, pr_extra={"checks": checks})

    result = _merge_gate(gh)

    assert result["merged"] is False and needle in result["reason"] and gh.merges == []


def test_merge_gate_is_idempotent_on_a_merged_pr_and_leaves_a_closed_one_alone():
    gh = _gate_tree(None, pr_extra={"state": "MERGED"})
    assert _merge_gate(gh)["already_merged"] is True and gh.merges == []
    gh.prs[40]["state"] = "CLOSED"
    assert "closed without merging" in _merge_gate(gh)["reason"]


def test_merge_gate_without_the_flag_through_the_cli_is_refused_not_an_error(monkeypatch):
    gh = _gate_tree(None)

    code, out = _cli(monkeypatch, gh, ["merge-gate", "40", "--issue", "91", "--stage", "product"])

    assert code == 0 and out["refused"] is True and gh.merges == []
