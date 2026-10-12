"""A unit parked by an auto-mode classifier denial resumes on the next run by itself."""
import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh


def _tree():
    return FakeGh([{"number": 90, "labels": ["type:epic", "epic:standing"]},
                   {"number": 1, "labels": ["type:task"], "parent": 90,
                    "stage": "development", "status": "todo"}])


def _park(gh, run_id="run-a"):
    out = s.cmd_mark_needs_human(gh, 1, "classifier denied open-dev-pr",
                                 classifier_denied="open-dev-pr", run_id=run_id)
    assert out["classifier_denied"] == "open-dev-pr" and "released" not in out


def test_the_next_run_resumes_a_classifier_denied_unit_and_flips_it_back_to_in_progress():
    gh = _tree()
    _park(gh)
    result = s.decide_next_action(gh, 90, run_id="run-b")
    assert (result["action"], result["issue"], result["stage"], result["denied_step"]) == \
        ("resume", 1, "development", "open-dev-pr")
    assert gh.issues[1]["status"] == "in-progress"


def test_the_run_that_parked_it_does_not_resume_it():
    gh = _tree()
    _park(gh)
    assert s.decide_next_action(gh, 90, run_id="run-a")["action"] == "none"
    assert gh.issues[1]["status"] == "needs-human"


def test_a_plain_needs_human_park_stays_parked():
    gh = _tree()
    gh.set_pipeline_status_field(1, "needs-human")
    gh.issue_comment(1, "🙋 Needs human input — a scope call")
    assert s.decide_next_action(gh, 90, run_id="run-b")["action"] == "none"


def test_a_later_plain_park_overrides_an_earlier_classifier_one():
    gh = _tree()
    _park(gh)
    s.cmd_mark_needs_human(gh, 1, "a scope call", repo_path="/nonexistent")
    assert s.decide_next_action(gh, 90, run_id="run-b")["action"] == "none"


def _resume_of(gh, number):
    s.cmd_mark_needs_human(gh, number, "classifier denied launch", classifier_denied="launch",
                           run_id="run-a")
    children = [i for i in gh.issue_list() if i["number"] == number]
    return s._classifier_denied_resume(gh, children, "run-b")


def test_a_classifier_denied_epic_resumes_as_an_epic_unit():
    gh = FakeGh([{"number": 80, "labels": ["type:initiative"]},
                 {"number": 90, "labels": ["type:epic"], "parent": 80, "status": "todo"}])
    assert _resume_of(gh, 90)["unit"] == "epic"


def test_a_classifier_denied_task_still_resumes_as_an_issue_unit():
    """Positive control."""
    assert _resume_of(_tree(), 1)["unit"] == "issue"
