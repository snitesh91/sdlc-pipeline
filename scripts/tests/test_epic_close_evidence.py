"""Epic-close evidence: verification is stamped with the tested head and goes stale on later
code; close-epic lists the required suites still lacking an attestation."""
import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh

HEAD = "a" * 40
TESTED = "b" * 40


def _epic(comments, files_since=(), delta=("backend/a.ts",), pr=None):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6, "comments": list(comments)},
                 {"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"}])
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.branch_head_sha = lambda branch: HEAD
    gh.files_since = lambda sha, branch: list(files_since)
    gh.base_delta_files = lambda head, base="main": list(delta)
    gh.pr_list_for_branch = lambda branch: [{"number": pr}] if pr else []
    gh.pr_checks = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": HEAD}
    return gh


def _marks(sha=TESTED):
    tag = f" sha:{sha}" if sha else ""
    return [f"<!-- epic-verification: e2e:9{tag} @ 2026-09-15T00:00:00Z -->",
            f"<!-- epic-verification: exploratory:9{tag} @ 2026-09-15T00:01:00Z -->"]


def test_record_stamps_the_marker_with_the_epic_branch_head():
    gh = _epic([])
    out = s.cmd_record_epic_verification(gh, 9, "e2e", "216 pass")
    assert out["sha"] == HEAD
    assert f"<!-- epic-verification: e2e:9 sha:{HEAD} @ " in gh.comments_on(9)[0]


def test_record_takes_an_explicit_sha_and_rejects_a_bad_one():
    gh = _epic([])
    assert s.cmd_record_epic_verification(gh, 9, "e2e", "ok", sha=TESTED)["sha"] == TESTED
    with pytest.raises(s.GhError, match="--sha"):
        s.cmd_record_epic_verification(gh, 9, "e2e", "ok", sha="not-a-sha")


def test_evidence_goes_stale_when_code_lands_after_the_tested_head():
    # Regression: only a `main` reconcile invalidated the record; a later code merge did not.
    gh = _epic(_marks(), files_since=["backend/src/upload.ts"])
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False
    assert any("code has landed since" in p for p in result["missing_verification"])
    assert len(result["missing_verification"]) == 1


def test_docs_only_commits_after_the_tested_head_keep_the_evidence_valid():
    # Positive control: the stale check must not fire on documentation-only movement.
    gh = _epic(_marks(), files_since=["docs/sdlc/epic-9/lld.md", "README.md"])
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: None
    gh.pr_files = lambda n: []
    result = s.cmd_close_epic(gh, 9)
    assert "missing_verification" not in result


def test_a_record_without_a_tested_sha_is_not_accepted():
    result = s.cmd_close_epic(_epic(_marks(sha=None)), 9)
    assert any("without a tested head sha" in p for p in result["missing_verification"])


def test_an_uncomparable_tested_sha_counts_as_stale():
    gh = _epic(_marks())
    def boom(sha, branch):
        raise s.GhError("404")
    gh.files_since = boom
    result = s.cmd_close_epic(gh, 9)
    assert any("cannot be compared" in p for p in result["missing_verification"])


def test_close_epic_lists_suites_still_unattested_at_the_epic_head():
    result = s.cmd_close_epic(_epic([], delta=["backend/a.ts", "frontend/b.tsx", "README.md"]), 9)
    assert result["unattested_suites"] == ["backend", "frontend"]


def test_an_attested_suite_drops_off_the_unattested_list():
    gh = _epic([], delta=["backend/a.ts", "frontend/b.tsx"], pr=40)
    gh.pr_view = lambda n, fields="": {"headRefOid": HEAD, "comments": [
        {"body": f"<!-- local-ci: backend:40 @ {HEAD} -->"}]}
    assert s.cmd_close_epic(gh, 9)["unattested_suites"] == ["frontend"]


def test_an_attestation_for_an_older_head_does_not_count():
    gh = _epic([], delta=["backend/a.ts"], pr=40)
    gh.pr_view = lambda n, fields="": {"headRefOid": HEAD, "comments": [
        {"body": f"<!-- local-ci: backend:40 @ {TESTED} -->"}]}
    assert s.cmd_close_epic(gh, 9)["unattested_suites"] == ["backend"]


def test_docs_only_epics_need_no_suite_attestation():
    result = s.cmd_close_epic(_epic([], delta=["docs/x.md"]), 9)
    assert result["unattested_suites"] == []


def test_evidence_older_than_a_main_reconcile_is_still_rejected():
    # Control that holds before and after the sha stamp: the reconcile rule is unchanged.
    gh = _epic(["<!-- epic-verification: exploratory:9 @ 2026-09-15T00:00:00Z -->",
                "<!-- epic-reconciled: 9 @ 2026-09-16T00:00:00Z -->"])
    result = s.cmd_close_epic(gh, 9)
    assert any("predates the last `origin/main` reconcile" in p
               for p in result["missing_verification"])


def _ready_epic(comments, checks, delta=("backend/a.ts",), attested=True):
    gh = _epic(comments, delta=delta, pr=38)
    gh.pr_checks = lambda n: list(checks)
    body = [{"body": f"<!-- local-ci: backend:38 @ {HEAD} -->"}] if attested else []
    gh.pr_view = lambda n, fields="": {"comments": body, "headRefOid": HEAD}
    gh.pr_files = lambda n: list(delta)
    gh.pr_ready = lambda n: None
    gh.merged = []
    gh.pr_merge = lambda n: gh.merged.append(n)
    return gh


def test_the_full_e2e_record_is_not_required_to_close():
    # Regression: close-epic refused with "no `e2e` closing-verification evidence" although the
    # e2e-test Task owns that evidence. Only the exploratory half is required now.
    gh = _ready_epic([_marks()[1]], checks=[])
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True and gh.merged == [38]


def test_the_exploratory_record_is_still_required_to_close():
    # Positive control: dropping the e2e half must not drop the exploratory one.
    result = s.cmd_close_epic(_ready_epic([_marks()[0]], checks=[]), 9)
    assert result["merged"] is False
    assert any("`exploratory` closing-verification evidence" in p
               for p in result["missing_verification"])


def test_a_failed_github_actions_check_does_not_block_epic_close():
    # Regression: a never-started Actions run (spending limit) reads as a failed check.
    failed = [{"bucket": "fail", "name": "Doc reference accuracy",
               "workflow": "Documentation Accuracy Check"}]
    gh = _ready_epic([_marks()[1]], checks=failed)
    assert s.cmd_close_epic(gh, 9)["merged"] is True


def test_a_pending_github_actions_check_does_not_block_epic_close():
    gh = _ready_epic([_marks()[1]], checks=[{"bucket": "pending", "workflow": "Backend CI"}])
    assert s.cmd_close_epic(gh, 9)["merged"] is True


def test_a_required_suite_with_no_attestation_still_blocks_epic_close():
    # Positive control: only Actions results were dropped; local-CI attestations still gate.
    gh = _ready_epic([_marks()[1]], checks=[], attested=False)
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False and result["checks"] == "missing-checks"


def test_a_passing_github_actions_check_still_satisfies_a_required_suite():
    passing = [{"bucket": "pass", "workflow": "Backend CI"}]
    gh = _ready_epic([_marks()[1]], checks=passing, attested=False)
    assert s.cmd_close_epic(gh, 9)["merged"] is True
