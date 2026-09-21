"""Regression + positive-control tests: a required suite on a PR is satisfied by a fresh
local-ci attestation OR a passing GHA check for its workflow on the PR head; a held suite
carries the reason (attestation state + check state); `attestable: false` suites are never
asked for a local run anywhere (pr-review start, epic close)."""
import pytest

import sdlc_next as s
from tests.test_epic_close_evidence import _epic, HEAD


class _Gh:
    """PR surface for pr_stale_attestations / start-comment(pr-review), with check runs."""

    def __init__(self, head, comments=(), changed=("backend/x.py",), checks=(), delta=None):
        self.head, self.comments, self.changed = head, list(comments), list(changed)
        self.checks, self.delta = list(checks), delta or {}
        self.posted = []

    def pr_view(self, n, fields=""):
        return {"headRefOid": self.head, "comments": self.comments, "baseRefName": "epic-9"}

    def pr_files(self, n):
        return self.changed

    def pr_checks(self, n):
        return self.checks

    def files_since(self, sha, branch):
        return self.delta.get(sha, [])

    def pr_list_for_branch(self, branch, state="open"):
        return [{"number": 55, "headRefName": branch}]

    def issue_comment(self, n, body):
        self.posted.append(body)


def _check(bucket, workflow="Backend CI"):
    return {"name": f"{workflow} / job", "bucket": bucket, "workflow": workflow}


def _attestation(sha):
    return [{"body": f"<!-- local-ci: backend:55 @ {sha} -->"}]


# ---- the OR rule: a passing check satisfies the suite without any attestation ----

def test_a_passing_check_satisfies_a_suite_with_no_attestation():
    gh = _Gh("bbbbbbb", checks=[_check("pass")])
    status = s.pr_stale_attestations(gh, 55, 55)
    assert status["stale"] == [] and status["passed_by_check"] == ["backend"]


def test_a_passing_check_satisfies_a_suite_whose_attestation_went_stale():
    gh = _Gh("bbbbbbb", _attestation("aaaaaaa"), checks=[_check("pass")],
             delta={"aaaaaaa": ["backend/y.py"]})  # sync touched suite files
    status = s.pr_stale_attestations(gh, 55, 55)
    assert status["stale"] == [] and status["passed_by_check"] == ["backend"]


def test_a_fresh_attestation_still_satisfies_without_any_check():
    # Positive control: the local-attestation leg of the OR rule is unchanged.
    gh = _Gh("bbbbbbb", _attestation("bbbbbbb"))
    status = s.pr_stale_attestations(gh, 55, 55)
    assert status["stale"] == [] and status["passed_by_check"] == []


def test_an_empty_diff_pr_requires_nothing():
    gh = _Gh("bbbbbbb", changed=[], checks=[_check("pending")])
    assert s.pr_stale_attestations(gh, 55, 55)["stale"] == []


@pytest.mark.parametrize("bucket, state", [("pending", "pending"), ("fail", "failing"),
                                           ("cancel", "failing"), ("skipping", "skipped")])
def test_a_non_passing_check_and_no_attestation_holds_with_the_check_state(bucket, state):
    gh = _Gh("bbbbbbb", checks=[_check(bucket)])
    [held] = s.pr_stale_attestations(gh, 55, 55)["stale"]
    assert held["suite"] == "backend" and held["check"] == state
    assert held["reason"] == (f"`backend`: no local attestation on head bbbbbbb and check "
                              f"'Backend CI' is {state}")


def test_a_missing_check_and_a_stale_attestation_name_both_in_the_reason():
    gh = _Gh("bbbbbbb", _attestation("aaaaaaa"), delta={"aaaaaaa": ["backend/y.py"]})
    [held] = s.pr_stale_attestations(gh, 55, 55)["stale"]
    assert held["check"] == "missing" and held["attested_sha"] == "aaaaaaa"
    assert held["reason"] == ("`backend`: local attestation is at aaaaaaa, not head bbbbbbb, "
                              "and check 'Backend CI' is missing")


def test_one_failed_run_makes_the_whole_workflow_failing():
    # A workflow with one passing and one failed job is not `passing`.
    checks = [_check("pass"), _check("fail")]
    assert s.workflow_check_state(checks, "Backend CI") == "failing"
    assert s.workflow_check_state([_check("pass"), _check("pending")], "Backend CI") == "pending"
    assert s.workflow_check_state([_check("pass"), _check("skipping")], "Backend CI") == "passing"
    assert s.workflow_check_state([_check("pass", "Other")], "Backend CI") == "missing"


# ---- start-comment (the transition into pr-review) applies the same rule ----

def test_start_comment_pr_review_proceeds_on_a_passing_check_without_attestation():
    gh = _Gh("bbbbbbb", checks=[_check("pass")])
    result = s.cmd_start_comment(gh, 55, "pr-review")
    assert result["started"] == "pr-review" and len(gh.posted) == 1
    assert result["satisfied_by_check"] == ["backend"]


def test_start_comment_pr_review_refusal_says_why_each_suite_is_held():
    gh = _Gh("bbbbbbb", checks=[_check("pending")])
    result = s.cmd_start_comment(gh, 55, "pr-review")
    assert result["refused"] is True and gh.posted == []
    assert result["held"][0]["check"] == "pending"
    assert "check 'Backend CI' is pending" in result["reason"]
    assert "record-local-ci" in result["reason"]  # the local leg remains an option


# ---- attestable: false -- the check is the only gate; never held at pr-review start ----

def test_a_non_attestable_suite_is_never_held_at_pr_review_start(monkeypatch):
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset({"backend"}))
    gh = _Gh("bbbbbbb", checks=[_check("fail")])
    assert s.pr_stale_attestations(gh, 55, 55)["stale"] == []
    # Positive control: attestable, the same failing check holds it.
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset())
    assert [x["suite"] for x in s.pr_stale_attestations(gh, 55, 55)["stale"]] == ["backend"]


def test_close_epic_reports_a_non_attestable_suite_as_awaiting_its_check(monkeypatch):
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset({"frontend"}))
    result = s.cmd_close_epic(_epic([], delta=["backend/a.ts", "frontend/b.tsx"]), 9)
    assert result["unattested_suites"] == ["backend"]
    assert result["awaiting_checks"] == ["frontend"]


def test_close_epic_drops_a_non_attestable_suite_whose_check_passed(monkeypatch):
    # Positive control: a passing check on the epic PR clears it from both lists.
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset({"frontend"}))
    gh = _epic([], delta=["backend/a.ts", "frontend/b.tsx"], pr=40)
    gh.pr_checks = lambda n: [_check("pass", "Frontend CI")]
    gh.pr_view = lambda n, fields="": {"headRefOid": HEAD, "comments": []}
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == ["backend"] and result["awaiting_checks"] == []
