"""Standing-child routing (`route`, the routed-merge evidence) and profile-waived gates
(`waive-gate`), against the in-memory work-item provider."""
import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh

_ROUTE = "<!-- stage-route: {} @ 2026-09-18T00:00:00Z -->"
_HANDOFF = "<!-- stage-transition: development->pr-review @ 2026-09-18T00:00:00Z -->"


class Gh(FakeGh):
    """FakeGh plus the reads `route`/`waive-gate` make."""

    def issue_fields(self, n):
        return self._node(n)["fields"]

    def pr_list_for_branch(self, branch, state="open"):
        return []


def _tree(child: dict, epic_labels=("epic:standing",)) -> Gh:
    return Gh([{"number": 90, "labels": ["type:epic", *epic_labels]},
               {"number": 9, "parent": 90, **child}])


# --- route ------------------------------------------------------------------

def test_route_at_pickup_stages_the_target_unclaimed_and_records_the_decision():
    gh = _tree({"issue_type": "Bug"})

    result = s.cmd_route(gh, 9, "development", "One-line null check; no interface change.")

    assert result == {"issue": 9, "routed": True, "from": "pickup", "to": "development",
                      "skipped": ["product", "product-review", "architecture", "arch-review"]}
    assert (gh.issues[9]["stage"], gh.issues[9]["status"]) == ("development", "todo")
    [comment] = gh.comments_on(9)
    assert "One-line null check" in comment
    assert "<!-- stage-route: pickup->development @ " in comment


def test_route_mid_flow_skips_only_the_stages_between():
    gh = _tree({"stage": "product", "status": "in-progress"})

    result = s.cmd_route(gh, 9, "architecture", "product.md is two lines; nothing to review.")

    assert result["skipped"] == ["product-review"]
    assert gh.issues[9]["stage"] == "architecture"


@pytest.mark.parametrize("child, to, reason, refusal", [
    ({}, "arch-review", "x", "a review always follows its stage"),
    ({}, "testing", "x", "--to must be one of"),
    ({}, "development", "  ", "one non-empty line"),
    ({}, "development", "two\nlines", "one non-empty line"),
    ({"stage": "architecture"}, "product", "x", "only moves forward"),
    ({"stage": "development"}, "development", "x", "only moves forward"),
    ({"stage": "pr-review"}, "development", "x", "only moves forward"),
    ({"stage": "lld"}, "development", "x", "not in the standing flow"),
    ({"stage": "product", "status": "awaiting-human-review"}, "architecture", "x",
     "a gate PR is open"),
    ({"stage": "pr-review"}, "merge", "x", "--to must be one of"),
])
def test_route_refuses_without_touching_the_issue(child, to, reason, refusal):
    gh = _tree(child)
    before = dict(gh.issues[9], comments=list(gh.issues[9]["comments"]))

    result = s.cmd_route(gh, 9, to, reason)

    assert result["routed"] is False and refusal in result["reason"]
    assert gh.issues[9] == before


def test_route_refuses_a_non_standing_epics_child():
    gh = _tree({}, epic_labels=())

    result = s.cmd_route(gh, 9, "development", "x")

    assert result["routed"] is False and "standing epic's child only" in result["reason"]
    assert gh.issues[9]["stage"] is None


# --- a route marker is never merge evidence ----------------------------------

@pytest.mark.parametrize("bodies, problem", [
    ([_HANDOFF], "no `pr-review` outcome"),
    # A route marker never stands in for the review.
    ([_HANDOFF, _ROUTE.format("pr-review->merge")], "no `pr-review` outcome"),
    ([_HANDOFF, "<!-- pr-review-outcome: rework:42 @ 2026-09-18T01:00:00Z -->",
      _ROUTE.format("pr-review->merge")], "`rework`, not `clean`"),
    ([_ROUTE.format("pr-review->merge")], "no `development->pr-review` handoff"),
])
def test_merge_evidence_needs_a_current_clean_review(bodies, problem):
    problems = s.missing_pipeline_evidence([{"body": b} for b in bodies])
    assert any(problem in p for p in problems)


@pytest.mark.parametrize("stage, next_stage, review", [
    ("product", "architecture", "product-review"),
    ("architecture", "development", "arch-review"),
])
def test_waive_gate_passes_a_standing_childs_gate_and_claims_the_next_stage(
        stage, next_stage, review):
    gh = _tree({"stage": stage, "status": "in-progress"})

    result = s.cmd_waive_gate(gh, 9, stage, "Clean review.")

    assert result == {"issue": 9, "unit": "issue", "next_stage": next_stage, "waived": True,
                      "profile": "standing"}
    assert (gh.issues[9]["stage"], gh.issues[9]["status"]) == (next_stage, "in-progress")
    waived = gh.comments_on(9)[0]
    assert f"<!-- gate-waived: {stage}:standing -->" in waived
    assert f"<!-- stage-transition: {review}->{next_stage} @ " in waived


@pytest.mark.parametrize("stage, toggle", [("product", "requiresHumanGateA"),
                                           ("architecture", "requiresHumanGateB")])
def test_waive_gate_refuses_when_the_profile_needs_a_human(stage, toggle):
    # Positive control: a default-profile child still waits for its human gate.
    gh = _tree({"stage": stage, "status": "in-progress"}, epic_labels=())

    with pytest.raises(s.GhError, match=toggle):
        s.cmd_waive_gate(gh, 9, stage, "x")
    assert gh.comments_on(9) == [] and gh.issues[9]["stage"] == stage


def test_waive_gate_refuses_a_stage_with_no_gate():
    with pytest.raises(s.GhError, match="stage must be one of"):
        s.cmd_waive_gate(_tree({}), 9, "development", "x")


def test_waive_gate_closes_a_phase_task_instead_of_claiming(monkeypatch):
    # An Initiative's Product-Roadmap Task has no next stage: a waived gate closes it.
    monkeypatch.setitem(s.PIPELINE, "gates", {**s.PIPELINE["gates"], "requiresHumanGateA": False})
    gh = Gh([{"number": 6, "labels": ["type:initiative"]},
             {"number": 7, "parent": 6, "stage": "product", "status": "in-progress"}])
    main_only = lambda argv: "worktree /repo\nHEAD x\nbranch refs/heads/main\n"

    result = s.cmd_waive_gate(gh, 7, "product", "Clean.", runner=main_only)

    assert result["phase_task_complete"] is True
    assert gh.issues[7]["state"] == "CLOSED" and gh.issues[7]["status"] == "done"
