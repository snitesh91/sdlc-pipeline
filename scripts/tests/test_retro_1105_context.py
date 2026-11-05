"""Retro 2026-11-05 (orchestrator context): `ci-failure-summary` returns only a failing job's
failure lines, bounded, and `close-epic --brief` reduces the bulky evidence to counts."""
import json

import sdlc_next as s
from tests.test_retro_0930_ci import CHECKS_ARGV, _link, _log, _log_argv
from tests.test_sdlc_next import ScriptedRunner


def _runner(checks, logs):
    runner = ScriptedRunner({CHECKS_ARGV: json.dumps(checks)})
    for job, out in logs.items():
        runner.responses[_log_argv(job)] = out
    return runner


NOISY = _log(*[f"npm http fetch GET 200 pkg-{i}" for i in range(500)],
             "FAIL src/cart.spec.ts", "  ● cart › totals", "    Expected: 3", "    Received: 2",
             "Tests: 1 failed, 40 passed")


def test_ci_failure_summary_keeps_only_failure_lines():
    runner = _runner([{"name": "Backend Validate", "bucket": "fail", "link": _link(1, 11)},
                      {"name": "Frontend Validate", "bucket": "pass", "link": _link(2, 21)}],
                     {11: NOISY})
    result = s.cmd_ci_failure_summary(s.GitHub(runner=runner), 42)
    [failing] = result["failing"]
    assert failing["failures"] == ["FAIL src/cart.spec.ts", "  ● cart › totals",
                                   "    Expected: 3", "    Received: 2",
                                   "Tests: 1 failed, 40 passed"]
    assert failing["truncated"] is False and failing["infra_suspect"] is False
    assert result["passed"] == 1
    assert len(json.dumps(result)) < 1000  # the 500 install lines never reach the orchestrator


def test_ci_failure_summary_is_bounded():
    flood = _log(*[f"FAIL test_{i} assertion error" for i in range(1000)])
    runner = _runner([{"name": f"job{j}", "bucket": "fail", "link": _link(j, 100 + j)}
                      for j in range(5)], {100 + j: flood for j in range(5)})
    result = s.cmd_ci_failure_summary(s.GitHub(runner=runner), 42)
    assert all(len(f["failures"]) <= s.CI_SUMMARY_LINES_PER_JOB for f in result["failing"])
    assert all(f["truncated"] for f in result["failing"])
    assert sum(len(l) for f in result["failing"] for l in f["failures"]) <= s.CI_SUMMARY_CHAR_CAP


def test_ci_failure_summary_survives_a_missing_log():
    runner = _runner([{"name": "b", "bucket": "fail", "link": "https://example.test/x"}], {})
    [failing] = s.cmd_ci_failure_summary(s.GitHub(runner=runner), 42)["failing"]
    assert failing["failures"] is None and failing["name"] == "b"


def test_close_epic_brief_reduces_bulky_fields():
    full = {"epic": 7, "merged": False, "reason": "r", "unattested_suites": ["backend"],
            "evidence": {"exploratory": "missing",
                         "suites": {"backend": {"status": "missing", "checks": ["x"] * 50},
                                    "frontend": "not_required"}},
            "carried_forward_files": [f"f{i}" for i in range(300)],
            "stale_delta": {"exploratory": {"files": ["a"] * 50, "count": 80}},
            "failing_checks": [{"name": "Backend IT", "failure_excerpt": "x" * 3000}],
            "infra_suspect": False, "cleanup": {"big": "x" * 1000}}
    brief = s.brief_close_epic(full)
    assert brief["evidence"] == {"exploratory": "missing",
                                 "suites": {"backend": "missing", "frontend": "not_required"}}
    assert brief["carried_forward_files"] == 300
    assert brief["stale_delta"] == {"exploratory": 80}
    assert brief["failing_checks"] == ["Backend IT"]
    assert "cleanup" not in brief and "ci-failure-summary" in brief["hint"]
    assert brief["reason"] == "r" and brief["unattested_suites"] == ["backend"]
    assert len(full["carried_forward_files"]) == 300  # the input is not mutated


def test_close_epic_brief_is_a_cli_flag(monkeypatch, capsys):
    big = {"epic": 7, "merged": False, "carried_forward_files": ["a", "b"]}
    monkeypatch.setattr(s, "get_work_item_provider", lambda: None)
    monkeypatch.setattr(s, "cmd_close_epic", lambda *a, **k: dict(big))
    s.main(["close-epic", "7", "--brief"])
    assert json.loads(capsys.readouterr().out)["carried_forward_files"] == 2
    s.main(["close-epic", "7"])
    assert json.loads(capsys.readouterr().out)["carried_forward_files"] == ["a", "b"]


def test_a_context_reset_session_is_relaunch_ready_at_once():
    # A cloud run that stops at a phase boundary to shed context must not wait for activity.
    from tests.test_cloud_epics import _initiative, _launched
    gh = _initiative({"number": 2, "comments": [_launched()]})
    s.cmd_end_cloud_session(gh, 2, "context-reset")
    session = s.latest_cloud_session([{"body": b} for b in gh.comments_on(2)])
    by_number = {i["number"]: i for i in gh.issue_list()}
    assert s.session_waits(gh, session, by_number, epic=2)["resolved"] is True
    # Positive control: `stopped` with no waits still waits for new GitHub activity.
    gh = _initiative({"number": 2, "comments": [_launched()]})
    s.cmd_end_cloud_session(gh, 2, "stopped")
    session = s.latest_cloud_session([{"body": b} for b in gh.comments_on(2)])
    assert s.session_waits(gh, session, by_number, epic=2) is None
