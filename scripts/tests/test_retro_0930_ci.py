"""Retro 2026-09-30 (CI slice): a CI-infra failure (runner out of disk, dind daemon gone,
runner lost) is told apart from a code failure without hand-run `gh run view --log-failed`,
and `rerun-checks` re-runs the failed jobs without hand-run `gh run rerun --failed`."""
import json

import pytest

import sdlc_next as s
from tests.test_sdlc_next import (ScriptedRunner, _script_terminal_fields, _clean_pipeline_comments,
                                  _issue, _list_argv, _list_response)
from tests.test_epic_close_evidence import _epic

CHECKS_ARGV = ("gh", "pr", "checks", "42", "--repo", "owner/repo",
               "--json", "name,state,bucket,link,workflow")
FILES_ARGV = ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename")
VIEW_ARGV = ("gh", "pr", "view", "42", "--repo", "owner/repo",
             "--json", "comments,headRefOid,baseRefName")


def _link(run, job=None):
    tail = f"/job/{job}" if job else ""
    return f"https://github.com/owner/repo/actions/runs/{run}{tail}"


def _log_argv(job):
    return ("gh", "run", "view", "--job", str(job), "--log-failed", "--repo", "owner/repo")


def _log(*lines):
    return "".join(f"build\tRun tests\t2026-09-30T00:00:00Z {l}\n" for l in lines)


INFRA_LOG = _log("npm ci", "", "npm ERR! nospc ENOSPC: no space left on device, write")
CODE_LOG = _log("FAIL src/cart.spec.ts", "Expected: 3", "Received: 2")


def _pr_checks_runner(checks, logs=None):
    runner = ScriptedRunner({
        CHECKS_ARGV: json.dumps(checks),
        FILES_ARGV: "docs/readme.md\n",
        VIEW_ARGV: json.dumps({"comments": [], "headRefOid": "abc1234", "baseRefName": "main"}),
    })
    for job, out in (logs or {}).items():
        runner.responses[_log_argv(job)] = out
    return runner


def _log_calls(runner):
    return [c for c in runner.calls if c[:3] == ["gh", "run", "view"]]


# ---- 1. pr-checks: failure excerpt + infra suspicion ----

def test_pr_checks_flags_an_out_of_disk_failure_as_infra_suspect():
    # Regression: an ENOSPC runner failure read exactly like a failing test.
    runner = _pr_checks_runner(
        [{"name": "Backend Validate", "bucket": "fail", "link": _link(11, 111)},
         {"name": "Frontend Validate", "bucket": "pass", "link": _link(12, 121)}],
        {111: INFRA_LOG})
    result = s.cmd_pr_checks(s.GitHub(runner=runner), 42)
    assert result["status"] == "failed"
    failed, passed = result["checks"]
    assert failed["infra_suspect"] is True
    assert "ENOSPC" in failed["failure_excerpt"]
    assert "failure_excerpt" not in passed and "infra_suspect" not in passed
    assert result["infra_suspect"] is True
    assert "rerun-checks 42" in result["hint"]
    assert len(_log_calls(runner)) == 1


def test_pr_checks_code_failure_is_not_infra_suspect():
    # Positive control: an ordinary test failure gets its excerpt but no infra flag.
    runner = _pr_checks_runner(
        [{"name": "Backend Validate", "bucket": "fail", "link": _link(11, 111)}], {111: CODE_LOG})
    result = s.cmd_pr_checks(s.GitHub(runner=runner), 42)
    [check] = result["checks"]
    assert check["infra_suspect"] is False
    assert check["failure_excerpt"].splitlines()[-1].endswith("Received: 2")
    assert result["infra_suspect"] is False
    assert "rerun-checks" not in result.get("hint", "")


def test_pr_checks_all_passing_fetches_no_logs():
    runner = _pr_checks_runner([{"name": "Backend Validate", "bucket": "pass", "link": _link(11, 111)}])
    result = s.cmd_pr_checks(s.GitHub(runner=runner), 42)
    assert result["status"] == "passed" and result["infra_suspect"] is False
    assert _log_calls(runner) == []
    assert "failure_excerpt" not in result["checks"][0]


def test_pr_checks_excerpt_is_the_last_twenty_non_empty_lines():
    lines = [f"line {i}" for i in range(30)]
    runner = _pr_checks_runner([{"name": "b", "bucket": "fail", "link": _link(11, 111)}],
                               {111: _log(*lines[:15]) + "\n  \n" + _log(*lines[15:]) + "\n"})
    [check] = s.cmd_pr_checks(s.GitHub(runner=runner), 42)["checks"]
    excerpt = check["failure_excerpt"].splitlines()
    assert len(excerpt) == 20
    assert excerpt[0].endswith("line 10") and excerpt[-1].endswith("line 29")


def test_pr_checks_cancelled_check_is_enriched_too():
    runner = _pr_checks_runner([{"name": "b", "bucket": "cancel", "link": _link(11, 111)}],
                               {111: _log("The runner has received a shutdown signal.")})
    [check] = s.cmd_pr_checks(s.GitHub(runner=runner), 42)["checks"]
    assert check["infra_suspect"] is True


@pytest.mark.parametrize("link", [_link(11), "", None, "https://ci.example.com/build/7"])
def test_pr_checks_link_without_a_job_id_gives_a_null_excerpt_and_no_fetch(link):
    runner = _pr_checks_runner([{"name": "b", "bucket": "fail", "link": link}])
    result = s.cmd_pr_checks(s.GitHub(runner=runner), 42)
    [check] = result["checks"]
    assert check["failure_excerpt"] is None and check["infra_suspect"] is False
    assert _log_calls(runner) == []


def test_pr_checks_log_fetch_failure_gives_a_null_excerpt_and_never_raises():
    runner = _pr_checks_runner([{"name": "b", "bucket": "fail", "link": _link(11, 111)}])
    runner.fail_on.add(_log_argv(111))
    [check] = s.cmd_pr_checks(s.GitHub(runner=runner), 42)["checks"]
    assert check["failure_excerpt"] is None and check["infra_suspect"] is False


def test_infra_patterns_are_configurable(monkeypatch):
    monkeypatch.setattr(s, "CI_INFRA_FAILURE_PATTERNS", ("flaky runner",))
    runner = _pr_checks_runner([{"name": "b", "bucket": "fail", "link": _link(11, 111)}],
                               {111: _log("Error: FLAKY RUNNER went away")})
    assert s.cmd_pr_checks(s.GitHub(runner=runner), 42)["infra_suspect"] is True


def test_default_infra_patterns_cover_the_known_runner_failures():
    for text in ["No space left on device", "ENOSPC", "Cannot connect to the Docker daemon",
                 "docker daemon not running", "Is the docker daemon running",
                 "The runner has received a shutdown signal",
                 "lost communication with the server", "exit code 137", "Killed", "OOMKilled"]:
        assert s.infra_suspect_text(text.upper()), text
    assert not s.infra_suspect_text("AssertionError: expected 3, got 2")
    # A bare OOM "Killed" line is infra; the word inside ordinary log text is not.
    assert s.infra_suspect_text("job\tstep\t2026-09-30T10:00:00Z Killed\nnext")
    assert not s.infra_suspect_text("test killed the stale session and asserted 404")
    assert s.PIPELINE["ci"]["infraFailurePatterns"] == list(s.CI_INFRA_FAILURE_PATTERNS)


# ---- 2. rerun-checks ----

def _rerun_runner(checks):
    return ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "headRefOid"):
            json.dumps({"headRefOid": "abc1234"}),
        CHECKS_ARGV: json.dumps(checks),
    })


def _rerun_argv(run):
    return ("gh", "run", "rerun", str(run), "--failed", "--repo", "owner/repo")


def test_rerun_checks_reruns_each_failed_run_once():
    runner = _rerun_runner([
        {"name": "a", "bucket": "fail", "link": _link(11, 111)},
        {"name": "b", "bucket": "cancel", "link": _link(11, 112)},
        {"name": "c", "bucket": "fail", "link": _link(13, 131)},
        {"name": "d", "bucket": "pass", "link": _link(14, 141)}])
    runner.responses[_rerun_argv(11)] = ""
    runner.responses[_rerun_argv(13)] = ""
    result = s.cmd_rerun_checks(s.GitHub(runner=runner), 42)
    assert result == {"pr": 42, "head": "abc1234", "rerun": [11, 13], "failed": [],
                      "nothing_to_rerun": False}
    assert [c for c in runner.calls if c[:3] == ["gh", "run", "rerun"]] == \
        [list(_rerun_argv(11)), list(_rerun_argv(13))]


def test_rerun_checks_with_nothing_failed_reruns_nothing():
    runner = _rerun_runner([{"name": "a", "bucket": "pass", "link": _link(11, 111)}])
    result = s.cmd_rerun_checks(s.GitHub(runner=runner), 42)
    assert result == {"pr": 42, "head": "abc1234", "rerun": [], "failed": [],
                      "nothing_to_rerun": True}


def test_rerun_checks_reports_a_partial_failure_and_exits_zero(monkeypatch, capsys):
    runner = _rerun_runner([{"name": "a", "bucket": "fail", "link": _link(11, 111)},
                            {"name": "b", "bucket": "fail", "link": _link(13, 131)}])
    runner.responses[_rerun_argv(13)] = ""
    runner.fail_on.add(_rerun_argv(11))
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: s.GitHub(runner=runner))
    assert s.main(["rerun-checks", "42"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rerun"] == [13]
    assert [f["run"] for f in out["failed"]] == [11] and "simulated failure" in out["failed"][0]["error"]


def test_rerun_checks_exits_one_when_the_pr_cannot_be_resolved(monkeypatch, capsys):
    runner = _rerun_runner([])
    runner.fail_on.add(("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "headRefOid"))
    monkeypatch.setattr(s, "get_work_item_provider", lambda: s.GitHub(runner=runner))
    assert s.main(["rerun-checks", "42"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# ---- 3. merge-pr names the failing checks and the infra suspicion ----

def _merge_runner(checks, logs):
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        CHECKS_ARGV: json.dumps(checks),
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        FILES_ARGV: "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
    })
    for job, out in logs.items():
        runner.responses[_log_argv(job)] = out
    _script_terminal_fields(runner, 9)
    return runner


def test_merge_pr_refusal_names_failing_checks_and_infra_suspect():
    runner = _merge_runner([{"name": "Backend Validate", "bucket": "fail", "link": _link(11, 111)},
                            {"name": "Frontend Validate", "bucket": "pass", "link": _link(12, 121)}],
                           {111: INFRA_LOG})
    with pytest.raises(s.GhError) as e:
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    msg = str(e.value)
    assert "not passed" in msg and "Backend Validate" in msg and "Frontend Validate" not in msg
    assert "infra_suspect" in msg and "rerun-checks 42" in msg


def test_merge_pr_refusal_on_a_code_failure_names_the_check_without_infra_suspect():
    runner = _merge_runner([{"name": "Backend Validate", "bucket": "fail", "link": _link(11, 111)}],
                           {111: CODE_LOG})
    with pytest.raises(s.GhError) as e:
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    assert "Backend Validate" in str(e.value) and "infra_suspect" not in str(e.value)


def test_merge_pr_pending_checks_fetch_no_logs():
    runner = _merge_runner([{"name": "Backend Validate", "bucket": "pending", "link": _link(11, 111)}], {})
    with pytest.raises(s.GhError, match="status=pending"):
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    assert _log_calls(runner) == []


# ---- 4. close-epic reports the epic PR's failing checks ----

def test_close_epic_reports_failing_checks_and_infra_suspect():
    gh = _epic([], pr=38)
    gh.pr_checks = lambda n: [{"name": "Backend Integration Tests", "bucket": "fail",
                               "link": _link(21, 211)},
                              {"name": "Backend Validate", "bucket": "pass", "link": _link(22, 221)}]
    gh.job_failed_log = lambda job: {211: _log("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")}[job]
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is False
    [failing] = result["failing_checks"]
    assert failing["name"] == "Backend Integration Tests" and failing["infra_suspect"] is True
    assert result["infra_suspect"] is True


def test_close_epic_without_failing_checks_adds_no_fields():
    gh = _epic([], pr=38)
    gh.pr_checks = lambda n: [{"name": "Backend Validate", "bucket": "pass", "link": _link(22, 221)}]
    def no_fetch(job):
        raise AssertionError("no log fetch when nothing failed")
    gh.job_failed_log = no_fetch
    result = s.cmd_close_epic(gh, 9)
    assert "failing_checks" not in result and "infra_suspect" not in result
