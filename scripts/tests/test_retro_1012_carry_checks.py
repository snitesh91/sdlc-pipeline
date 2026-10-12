"""A required workflow's pass before a sync merge stands for the synced head when the
merged-in delta misses the workflow's paths (`effective_checks`)."""
import pytest

import sdlc_next as s


def _check(workflow, bucket):
    return {"name": f"{workflow} job", "state": bucket.upper(), "bucket": bucket, "link": "",
            "workflow": workflow}


class Gh:
    """Head `h2` = sync merge of `h1` (tested) and main; `delta` = files main brought in."""

    def __init__(self, head_checks, prior_checks, delta, parents=("h1", "m1")):
        self.head_checks, self.prior_checks = head_checks, prior_checks
        self.delta, self.parents = delta, list(parents)

    def pr_checks(self, pr):
        return list(self.head_checks)

    def pr_view(self, pr, fields=""):
        return {"headRefOid": "h2", "baseRefName": "main", "comments": []}

    def pr_files(self, pr):
        return ["backend/app.py"]

    def commit_parents(self, sha):
        return self.parents if sha == "h2" else ["h0"]

    def files_since(self, sha, head):
        return list(self.delta)

    def commit_checks(self, sha):
        return list(self.prior_checks) if sha == "h1" else []


@pytest.mark.parametrize("head_bucket", ["pending", "cancel", None])
def test_a_pass_before_a_docs_only_sync_carries(head_bucket):
    head = [_check("Backend CI", head_bucket)] if head_bucket else []
    gh = Gh(head, [_check("Backend CI", "pass")], ["frontend/x.ts", "README.md"])
    checks = s.effective_checks(gh, 1)
    assert s.workflow_check_state(checks, "Backend CI") == "passing"
    assert s.merge_gate_status(gh.pr_files(1), checks, [], "h2", "main") == ("passed", [])
    assert checks[-1]["carried_from"] == "h1"


def test_a_sync_bringing_in_the_workflows_own_paths_does_not_carry():
    gh = Gh([_check("Backend CI", "pending")], [_check("Backend CI", "pass")],
            ["backend/other.py"])
    assert s.workflow_check_state(s.effective_checks(gh, 1), "Backend CI") == "pending"


def test_a_failing_run_on_the_head_is_never_overridden():
    gh = Gh([_check("Backend CI", "fail")], [_check("Backend CI", "pass")], ["frontend/x.ts"])
    assert s.workflow_check_state(s.effective_checks(gh, 1), "Backend CI") == "failing"


def test_a_head_that_is_not_a_merge_does_not_carry():
    gh = Gh([_check("Backend CI", "pending")], [_check("Backend CI", "pass")],
            ["frontend/x.ts"], parents=("h1",))
    assert s.workflow_check_state(s.effective_checks(gh, 1), "Backend CI") == "pending"


def test_the_config_switch_turns_it_off(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["ci"], "carryChecksAcrossSync", False)
    gh = Gh([_check("Backend CI", "pending")], [_check("Backend CI", "pass")],
            ["frontend/x.ts"])
    assert s.workflow_check_state(s.effective_checks(gh, 1), "Backend CI") == "pending"
