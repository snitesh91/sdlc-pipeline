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
    gh.pr_list_for_branch = lambda branch, state="open": (
        [{"number": pr}] if pr and state == "open" and branch == "epic-9" else [])
    gh.pr_checks = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": HEAD}
    gh.pr_files = lambda n: list(delta)
    gh.branch_commits = lambda base, head: []
    gh.commit_files = lambda sha: []
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


def test_an_attestation_for_an_older_head_does_not_count_once_the_suite_paths_moved():
    gh = _epic([], files_since=["backend/b.ts"], delta=["backend/a.ts"], pr=40)
    gh.pr_view = lambda n, fields="": {"headRefOid": HEAD, "comments": [
        {"body": f"<!-- local-ci: backend:40 @ {TESTED} -->"}]}
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == ["backend"]
    assert result["evidence"]["suites"]["backend"] == "missing"


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


# --- evidence carry-forward over a closing-delta fix ------------------------------------

FIXTURE_GLOBS = s.EVIDENCE_CARRY_FORWARD_GLOBS + ("backend/test/fixtures/**",)


def test_close_epic_reports_fresh_exploratory_evidence_at_the_head():
    gh = _epic([_marks()[1]], delta=["docs/x.md"])
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: None
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True
    assert result["evidence"]["exploratory"] == "fresh"
    assert result["carried_forward_files"] == []


def test_a_configured_fixtures_only_delta_carries_the_exploratory_evidence_forward(monkeypatch):
    # Regression: any non-doc path after the tested head forced a full re-run; the
    # carry-forward set is now config-driven.
    monkeypatch.setattr(s, "EVIDENCE_CARRY_FORWARD_GLOBS", FIXTURE_GLOBS)
    gh = _epic([_marks()[1]], files_since=["backend/test/fixtures/seed.json", "README.md"],
               delta=["docs/x.md"])
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: None
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True
    assert result["evidence"]["exploratory"] == f"carried_forward_from {TESTED[:10]}"
    assert result["carried_forward_files"] == ["README.md", "backend/test/fixtures/seed.json"]


def test_a_path_outside_the_carry_forward_set_still_forces_a_re_run(monkeypatch):
    # Positive control: widening the set to fixtures does not widen it to source.
    monkeypatch.setattr(s, "EVIDENCE_CARRY_FORWARD_GLOBS", FIXTURE_GLOBS)
    gh = _epic([_marks()[1]], files_since=["backend/test/fixtures/seed.json", "backend/src/a.ts"])
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False
    assert result["evidence"]["exploratory"] == "missing"
    assert any("code has landed since" in p for p in result["missing_verification"])


def test_the_pipeline_config_file_never_carries_forward(monkeypatch):
    monkeypatch.setattr(s, "EVIDENCE_CARRY_FORWARD_GLOBS", ("**/*.json",))
    gh = _epic([_marks()[1]], files_since=[s.CONFIG_FILENAME])
    assert s.cmd_close_epic(gh, 9)["evidence"]["exploratory"] == "missing"


def test_no_carry_forward_requires_the_evidence_at_the_exact_head():
    gh = _epic([_marks()[1]], files_since=["README.md"])
    strict = s.cmd_close_epic(gh, 9, carry_forward=False)
    assert strict["merged"] is False
    assert strict["evidence"]["exploratory"] == "missing"
    assert any("carry-forward disabled" in p for p in strict["missing_verification"])


def test_the_cli_flag_turns_carry_forward_off(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    seen = {}
    monkeypatch.setattr(s, "cmd_close_epic",
                        lambda gh, epic, repo_path=".", carry_forward=True:
                        seen.update(carry_forward=carry_forward) or {"ok": True})
    assert s.main(["close-epic", "9", "--no-carry-forward"]) == 0
    assert seen == {"carry_forward": False}


def test_an_epic_pr_attestation_carries_forward_over_files_the_suite_does_not_cover():
    gh = _epic([], files_since=["frontend/x.tsx"], delta=["backend/a.ts", "frontend/x.tsx"],
               pr=40)
    gh.pr_view = lambda n, fields="": {"headRefOid": HEAD, "comments": [
        {"body": f"<!-- local-ci: backend:40 @ {TESTED} -->"}]}
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == ["frontend"]
    assert result["evidence"]["suites"]["backend"] == f"carried_forward_from {TESTED[:10]}"
    assert result["carried_forward_files"] == ["frontend/x.tsx"]


# --- per-child attestation chain --------------------------------------------------------

C1, C2 = "c" * 40, "d" * 40
DIRECT = "e" * 40


def _chain_epic(children, direct=(), comments=(), delta=("backend/a.ts",)):
    """`children`: (issue, pr, files, attested_sha|None, merge_sha); `direct`: (sha, files)
    for commits on `epic-9` that came through no child PR."""
    issues = [{"number": 6, "labels": ["type:initiative"]},
              {"number": 9, "labels": ["type:epic"], "parent": 6, "comments": list(comments)}]
    issues += [{"number": c[0], "labels": ["type:task"], "parent": 9, "state": "CLOSED"}
               for c in children]
    gh = FakeGh(issues)
    by_pr = {c[1]: c for c in children}
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.branch_head_sha = lambda branch: HEAD
    gh.files_since = lambda sha, ref: []
    gh.base_delta_files = lambda head, base="main": list(delta)
    gh.pr_list_for_branch = lambda branch, state="open": (
        [{"number": c[1]} for c in children if state == "merged" and branch == f"issue-{c[0]}"])
    gh.pr_checks = lambda n: []
    gh.pr_files = lambda n: list(by_pr[n][2]) if n in by_pr else list(delta)
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.merged = []
    gh.pr_merge = lambda n: gh.merged.append(n)
    gh.branch_commits = lambda base, head: [c[4] for c in children] + [d[0] for d in direct]
    gh.commit_files = lambda sha: next(list(d[1]) for d in direct if d[0] == sha)

    def pr_view(n, fields=""):
        if n not in by_pr:
            return {"comments": [], "headRefOid": HEAD}
        _, pr, _, attested, merge_sha = by_pr[n]
        comments = [{"body": f"<!-- local-ci: backend:{pr} @ {attested} -->"}] if attested else []
        return {"comments": comments, "headRefOid": C1 if n == 41 else C2,
                "mergeCommit": {"oid": merge_sha}}
    gh.pr_view = pr_view
    return gh


def test_every_child_attested_makes_the_epic_head_attested_for_the_suite():
    # Regression: close-epic asked for a backend attestation on the epic PR although each
    # merged child PR already carried one for the files it touched.
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40),
                      (12, 42, ["backend/b.ts", "docs/x.md"], C2, "2" * 40)])
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == []
    assert result["evidence"]["suites"]["backend"] == "children:#11,#12"


def test_one_child_without_an_attestation_breaks_the_chain_and_is_named():
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40),
                      (12, 42, ["backend/b.ts"], None, "2" * 40)])
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == ["backend"]
    assert result["evidence"]["suites"]["backend"] == "missing"
    assert any("#12" in b and "PR #42" in b for b in result["evidence_breaks"]["backend"])


def test_a_child_that_touched_no_suite_path_needs_no_attestation_for_it():
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40),
                      (12, 42, ["docs/x.md"], None, "2" * 40)])
    assert s.cmd_close_epic(gh, 9)["evidence"]["suites"]["backend"] == "children:#11"


def test_a_direct_epic_commit_touching_suite_paths_breaks_the_chain_and_is_listed():
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40)],
                     direct=[(DIRECT, ["backend/fix.ts"])])
    result = s.cmd_close_epic(gh, 9)
    assert result["unattested_suites"] == ["backend"]
    assert any(DIRECT[:10] in b for b in result["evidence_breaks"]["backend"])


def test_a_direct_docs_commit_leaves_the_chain_intact():
    # Positive control for the direct-commit rule: only suite-covered paths break it.
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40)],
                     direct=[(DIRECT, ["docs/sdlc/epic-9/lld.md"])])
    assert s.cmd_close_epic(gh, 9)["evidence"]["suites"]["backend"] == "children:#11"


def test_a_child_attestation_older_than_its_merged_head_carries_over_uncovered_files():
    gh = _chain_epic([(11, 41, ["backend/a.ts", "docs/x.md"], TESTED, "1" * 40)])
    gh.files_since = lambda sha, ref: ["docs/x.md"] if sha == TESTED else []
    assert s.cmd_close_epic(gh, 9)["evidence"]["suites"]["backend"] == "children:#11"


def test_the_chain_satisfies_the_final_merge_gate():
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40)], comments=[_marks()[1]])
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True and gh.merged == [38]
    assert result["evidence"] == {"exploratory": "fresh",
                                  "suites": {"backend": "children:#11",
                                             "frontend": "not_required"}}


def test_no_carry_forward_ignores_the_child_chain():
    gh = _chain_epic([(11, 41, ["backend/a.ts"], C1, "1" * 40)], comments=[_marks()[1]])
    result = s.cmd_close_epic(gh, 9, carry_forward=False)
    assert result["merged"] is False and result["checks"] == "missing-checks"
    assert result["unattested_suites"] == ["backend"]
