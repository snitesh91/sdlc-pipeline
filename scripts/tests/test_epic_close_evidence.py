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
    assert len(result["missing_verification"]) == 2


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
    gh = _epic(["<!-- epic-verification: e2e:9 @ 2026-09-15T00:00:00Z -->",
                "<!-- epic-reconciled: 9 @ 2026-09-16T00:00:00Z -->"])
    result = s.cmd_close_epic(gh, 9)
    assert any("predates the last `origin/main` reconcile" in p
               for p in result["missing_verification"])
