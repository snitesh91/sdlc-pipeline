"""Retro 2026-11-09 (deflake): `rerun-checks --failed-only` re-runs only a red suite's failed
spec files on the same head, and the merge gate counts that suite green when the re-run
passes (`apply_deflakes`)."""
import json

import pytest

import sdlc_next as s
from tests.test_retro_0930_ci import (CHECKS_ARGV, _link, _log, _log_argv, _merge_runner)
from tests.test_sdlc_next import ScriptedRunner
from tests.listed_github import graphql_listing  # noqa: F401


@pytest.fixture(autouse=True)
def _graphql_listing(graphql_listing):
    """merge-pr scripts the base client's whole-repo listing."""


HEAD = "a" * 40
WF = "Kipsa Backend Integration Tests"
SPEC = "src/database/migrations/V68__LookupIndexes.it-spec.ts"
RED_LOG = _log("\x1b[1m\x1b[31mFAIL\x1b[39m\x1b[22m " + SPEC, "  ● plans a scan",
               "FAIL src/a.it-spec.ts", "PASS src/b.it-spec.ts", "FAIL src/a.it-spec.ts")
DISPATCH_RUNS_ARGV = ("gh", "api", f"repos/owner/repo/actions/runs?head_sha={HEAD}"
                      f"&event=workflow_dispatch&per_page=100", "--jq",
                      "[.workflow_runs[] | {name, display_title, conclusion, html_url}]")


def _marker(specs=(SPEC,), sha=HEAD, wf=WF):
    return {"body": f'note\n\n<!-- sdlc:deflake workflow="{wf}" sha={sha} specs={",".join(specs)} -->'}


def _red():
    return [{"name": "Integration Tests", "bucket": "fail", "link": _link(11, 111), "workflow": WF},
            {"name": "Backend Validate", "bucket": "pass", "link": _link(12, 121),
             "workflow": "Backend Validate"}]


def _run(title, conclusion="success", name=WF):
    return {"name": name, "display_title": title, "conclusion": conclusion, "html_url": "u"}


def test_failed_specs_parses_jest_fail_lines_ansi_and_prefix_stripped():
    assert s.failed_specs(RED_LOG) == ["src/a.it-spec.ts", SPEC]
    assert s.failed_specs(_log("npm ERR! build failed", "error TS2304")) == []


# ---- rerun-checks --failed-only ----

def _rerun_runner(checks, logs):
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "headRefOid,headRefName"):
            json.dumps({"headRefOid": HEAD, "headRefName": "epic-7"}),
        CHECKS_ARGV: json.dumps(checks),
    })
    for job, out in logs.items():
        runner.responses[_log_argv(job)] = out
    runner.prefix_responses[("gh", "workflow", "run")] = ""
    runner.prefix_responses[("gh", "pr", "comment", "42")] = ""
    return runner


def test_failed_only_dispatches_the_failed_specs_and_records_a_marker():
    checks = _red() + [{"name": "Build", "bucket": "fail", "link": _link(13, 131), "workflow": "Web"}]
    runner = _rerun_runner(checks, {111: RED_LOG, 131: _log("error TS2304: cannot find name")})
    result = s.cmd_rerun_checks(s.GitHub(runner=runner), 42, failed_only=True)
    assert result["deflake"] == [{"workflow": WF, "specs": ["src/a.it-spec.ts", SPEC]}]
    assert result["not_deflakable"] == ["Web"] and result["failed"] == []
    dispatch = [c for c in runner.calls if c[:3] == ["gh", "workflow", "run"]]
    assert dispatch == [["gh", "workflow", "run", WF, "--ref", "epic-7", "--repo", "owner/repo",
                         "-f", f"specs=src/a.it-spec.ts {SPEC}"]]
    body = next(c for c in runner.calls if c[:3] == ["gh", "pr", "comment"])[-1]
    assert s._DEFLAKE_MARKER.search(body).groups() == (WF, HEAD, f"src/a.it-spec.ts,{SPEC}")
    # No whole-job rerun.
    assert not [c for c in runner.calls if c[:3] == ["gh", "run", "rerun"]]


def test_failed_only_reports_a_dispatch_error_and_posts_no_marker():
    runner = _rerun_runner(_red(), {111: RED_LOG})
    runner.prefix_responses.pop(("gh", "workflow", "run"))
    runner.fail_on.add(("gh", "workflow", "run", WF, "--ref", "epic-7", "--repo", "owner/repo",
                        "-f", f"specs=src/a.it-spec.ts {SPEC}"))
    result = s.cmd_rerun_checks(s.GitHub(runner=runner), 42, failed_only=True)
    assert result["deflake"] == [] and result["failed"][0]["workflow"] == WF
    assert not [c for c in runner.calls if c[:3] == ["gh", "pr", "comment"]]


# ---- apply_deflakes: the gate ----

def _gh(runs):
    runner = ScriptedRunner({DISPATCH_RUNS_ARGV: json.dumps(runs)})
    return s.GitHub(runner=runner), runner


def test_a_passing_deflake_run_counts_the_failing_suite_green():
    gh, _ = _gh([_run(f"deflake: {SPEC} src/x.it-spec.ts")])
    out = s.apply_deflakes(gh, _red(), [_marker()], HEAD)
    assert s.workflow_check_state(out, WF) == "passing"
    assert s.checks_status(out) == "passed"
    assert next(c for c in out if c["workflow"] == WF)["deflaked_specs"] == [SPEC]


@pytest.mark.parametrize("runs", [
    [_run(f"deflake: {SPEC}", conclusion="failure")],          # the re-run failed again
    [_run("deflake: src/other.it-spec.ts")],                    # it ran other specs
    [_run(f"deflake: {SPEC}", name="Backend Integration Tests")],  # another workflow
    [_run(f"Kipsa Backend Integration Tests {SPEC}")],          # not a deflake run
    [],
])
def test_the_suite_stays_failing_without_a_matching_passing_deflake(runs):
    gh, _ = _gh(runs)
    assert s.workflow_check_state(s.apply_deflakes(gh, _red(), [_marker()], HEAD), WF) == "failing"


def test_a_marker_for_another_head_fetches_nothing_and_changes_nothing():
    gh, runner = _gh([_run(f"deflake: {SPEC}")])
    checks = _red()
    assert s.apply_deflakes(gh, checks, [_marker(sha="b" * 40)], HEAD) is checks
    assert s.apply_deflakes(gh, checks, [], HEAD) is checks
    assert runner.calls == []


# ---- merge-pr: regression (red without a deflake) and positive control ----

def _merge(comments, runs):
    runner = _merge_runner(_red(), {111: RED_LOG})
    runner.responses[("gh", "pr", "view", "42", "--repo", "owner/repo",
                      "--json", "comments,headRefOid")] = json.dumps(
        {"comments": comments, "headRefOid": HEAD})
    runner.responses[DISPATCH_RUNS_ARGV] = json.dumps(runs)
    return runner


def test_merge_pr_refuses_a_red_suite_with_no_deflake():
    runner = _merge([], [_run(f"deflake: {SPEC}")])
    with pytest.raises(s.GhError, match="not passed"):
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)


def test_merge_pr_gets_past_the_check_gate_on_a_passing_deflake():
    runner = _merge([_marker(("src/a.it-spec.ts", SPEC))],
                    [_run(f"deflake: src/a.it-spec.ts {SPEC}")])
    # The check gate passes; the next call (gh pr ready) is unscripted and stops the merge.
    with pytest.raises(AssertionError, match="unexpected gh invocation.*'ready'"):
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
