"""Regression + positive-control tests for the 2026-10-04 control-plane retro: not-planned
closes off the run cap, config drift, uncovered paths, edit-issue, and the red-check handoff gate.

Each regression goes RED against the pre-fix code; its positive control stays GREEN either way."""
import json

import pytest

import sdlc_next as s
from tests.test_retro_control_plane import _epic_tree, _no_worktree_runner


# ---- 1: close-issue --not-planned does not count toward the run cap ----

def _tracked_run(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    s._write_run_state(9, {"run_id": "r", "terminal": [], "in_flight": {"10": "development"}})
    return _epic_tree({"number": 10, "labels": ["type:task"], "parent": 9})


def test_close_issue_not_planned_leaves_the_run_cap_untouched(monkeypatch, tmp_path):
    gh = _tracked_run(monkeypatch, tmp_path)
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner(),
                               not_planned=True)
    state = s.read_run_state(9)
    assert state["terminal"] == [] and state["in_flight"] == {}
    assert result["run_terminal"]["terminal_count"] == 0


def test_close_issue_completed_still_counts_toward_the_run_cap(monkeypatch, tmp_path):
    # Positive control: a completed close is booked as before.
    gh = _tracked_run(monkeypatch, tmp_path)
    result = s.cmd_close_issue(gh, 10, repo_path="/r", runner=_no_worktree_runner())
    state = s.read_run_state(9)
    assert state["terminal"] == [10] and state["in_flight"] == {}
    assert result["run_terminal"]["terminal_count"] == 1


# ---- 2: next-action --run-id reports config drift ----

def _drift_run(monkeypatch, tmp_path, origin=None):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(s, "origin_config", lambda runner=None: origin)
    s.run_cap_state(9, "r")


def test_a_local_config_edit_mid_run_is_reported_once(monkeypatch, tmp_path):
    _drift_run(monkeypatch, tmp_path)
    monkeypatch.setattr(s, "CONFIG", {**s.CONFIG, "docRoot": "elsewhere"})
    assert s.config_drift(9, "r") == {"source": "local", "keys": ["docRoot"]}
    assert s.config_drift(9, "r") is None


def test_an_unchanged_config_reports_no_drift(monkeypatch, tmp_path):
    # Positive control.
    _drift_run(monkeypatch, tmp_path, origin=dict(s.CONFIG))
    assert s.config_drift(9, "r") is None
    assert s.config_drift(9, None) is None


def test_an_origin_only_difference_keeps_reporting_until_the_checkout_matches(monkeypatch, tmp_path):
    ahead = {**s.CONFIG, "pipeline": {"x": 1}, "new": True}
    _drift_run(monkeypatch, tmp_path, origin=ahead)
    expected = {"source": "origin", "keys": ["new", "pipeline"]}
    assert s.config_drift(9, "r") == expected and s.config_drift(9, "r") == expected
    monkeypatch.setattr(s, "CONFIG", ahead)
    assert s.config_drift(9, "r")["source"] == "local"
    assert s.config_drift(9, "r") is None


def test_a_state_file_without_a_digest_is_stamped_silently(monkeypatch, tmp_path):
    _drift_run(monkeypatch, tmp_path)
    s._write_run_state(9, {"run_id": "r", "terminal": []})
    assert s.config_drift(9, "r") is None
    assert s.read_run_state(9)["config_digest"] == s.config_digest(s.CONFIG)


def test_next_action_with_a_run_id_carries_config_changed(monkeypatch, tmp_path):
    import argparse
    _drift_run(monkeypatch, tmp_path)
    monkeypatch.setattr(s, "CONFIG", {**s.CONFIG, "docRoot": "elsewhere"})
    gh = _epic_tree()
    args = argparse.Namespace(epic=9, run_id="r", repo_path=".", skip_epic=[])
    assert s.cmd_next_action(gh, args)["config_changed"] == {"source": "local",
                                                            "keys": ["docRoot"]}
    assert "config_changed" not in s.cmd_next_action(gh, args)


def test_origin_config_reads_the_committed_file_and_fails_open(monkeypatch, tmp_path):
    from tests.test_sdlc_next import ScriptedRunner
    cfg = tmp_path / "repo" / ".claude" / s.CONFIG_FILENAME
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}")
    monkeypatch.setenv("SDLC_CONFIG", str(cfg))
    top = str((tmp_path / "repo").resolve())
    show = ("git", "-C", top, "show", f"origin/main:.claude/{s.CONFIG_FILENAME}")
    runner = ScriptedRunner({("git", "-C", str(cfg.parent.resolve()), "rev-parse",
                              "--show-toplevel"): top + "\n", show: '{"repo": "a/b"}'})
    assert s.origin_config(runner) == {"repo": "a/b"}
    runner.fail_on.add(show)
    assert s.origin_config(runner) is None


# ---- 3: pr-checks / merge-pr name changed paths no required workflow covers ----

class _ChecksGh:
    def __init__(self, files, checks=(), base="main"):
        self.files, self.checks, self.base = list(files), [dict(c) for c in checks], base

    def pr_checks(self, n):
        return self.checks

    def pr_view(self, n, fields=""):
        return {"comments": [], "headRefOid": "feedface", "baseRefName": self.base}

    def pr_files(self, n):
        return self.files

    def job_failed_log(self, job):
        return "assert 1 == 2"


def test_uncovered_paths_skips_docs_config_and_covered_files():
    files = ["src/x.py", "backend/a.py", "docs/sdlc/n.md", "README.md",
             f".claude/{s.CONFIG_FILENAME}", "backend/notes.md"]
    assert s.uncovered_paths(files) == ["src/x.py"]


def test_uncovered_paths_counts_a_spec_scoped_to_another_base_as_covering(monkeypatch):
    scoped = [{**w, "bases": ("main", "initiative-*")} for w in s.REQUIRED_WORKFLOWS]
    monkeypatch.setattr(s, "REQUIRED_WORKFLOWS", tuple(scoped))
    # A task PR into epic-<n>: the suite runs at a later merge, not a coverage gap.
    assert s.uncovered_paths(["backend/a.py"]) == []
    # Positive control: a path no entry covers on any base is still reported.
    assert s.uncovered_paths(["backend/a.py", "src/x.py"]) == ["src/x.py"]


def test_pr_checks_warns_about_uncovered_paths_without_changing_status():
    result = s.cmd_pr_checks(_ChecksGh(["src/x.py"], [{"name": "ci", "bucket": "pass"}]), 42)
    assert result["status"] == "passed"
    assert result["uncovered_paths"] == ["src/x.py"]
    assert result["uncovered_hint"] == ("no required workflow covers these paths; "
                                        "no suite ran for them")


def test_pr_checks_with_every_path_covered_adds_no_warning():
    # Positive control.
    gh = _ChecksGh(["backend/a.py", "docs/sdlc/x.md"],
                   [{"name": "b", "bucket": "pass", "workflow": "Backend CI"}])
    result = s.cmd_pr_checks(gh, 42)
    assert result["status"] == "passed" and "uncovered_paths" not in result


def _merge_runner_with_files(files):
    from tests.test_merge_gate_and_citations import _merge_runner
    runner = _merge_runner()
    runner.responses[("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files",
                      "--jq", ".[].filename")] = "".join(f"{f}\n" for f in files)
    return runner


def test_merge_pr_reports_uncovered_paths_and_still_merges():
    result = s.cmd_merge_pr(s.GitHub(runner=_merge_runner_with_files(["src/x.py"])), 42, issue=9)
    assert result["merged"] is True and result["uncovered_paths"] == ["src/x.py"]
    assert "no suite ran" in result["uncovered_hint"]


def test_merge_pr_on_a_docs_only_pr_adds_no_warning():
    # Positive control.
    runner = _merge_runner_with_files(["docs/sdlc/issue-9/product.md"])
    result = s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    assert result["merged"] is True and "uncovered_paths" not in result


# ---- 4: edit-issue, refusing pipeline-owned labels and in-flight units ----

class _EditGh:
    def __init__(self):
        self.calls = []

    def issue_set_body(self, n, body):
        self.calls.append(("body", n, body))

    def ensure_label(self, label):
        self.calls.append(("ensure", label))

    def issue_edit(self, n, add_labels=(), remove_labels=(), **_):
        self.calls.append(("labels", n, list(add_labels), list(remove_labels)))


def test_edit_issue_sets_the_body_and_labels(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    body = tmp_path / "b.md"
    body.write_text("new body\n")
    gh = _EditGh()
    result = s.cmd_edit_issue(gh, 5, str(body), ["priority:high"], ["wip"])
    assert result == {"issue": 5, "refused": False, "reason": None,
                      "edited": ["body", "add-label:priority:high", "remove-label:wip"]}
    assert ("body", 5, "new body\n") in gh.calls
    assert ("labels", 5, ["priority:high"], ["wip"]) in gh.calls


@pytest.mark.parametrize("label", ["sdlc:gate", "epic:architected", "sdlc:cloud",
                                   "stage:development", "status:needs-human", "pr-review",
                                   "Needs Human"])
def test_edit_issue_refuses_a_pipeline_owned_label(monkeypatch, tmp_path, label):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    gh = _EditGh()
    result = s.cmd_edit_issue(gh, 5, add_labels=["ok", label])
    assert result["refused"] is True and label in result["reason"] and gh.calls == []
    assert s.cmd_edit_issue(gh, 5, remove_labels=[label])["refused"] is True


def test_edit_issue_refuses_an_issue_a_live_run_has_in_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    s._write_run_state(9, {"run_id": "r", "terminal": [], "in_flight": {"5": "development"}})
    gh = _EditGh()
    result = s.cmd_edit_issue(gh, 5, add_labels=["ok"])
    assert result["refused"] is True and "live run r" in result["reason"] and gh.calls == []
    # Positive control: an archived run no longer holds it.
    s.archive_run_state(9)
    assert s.cmd_edit_issue(gh, 5, add_labels=["ok"])["edited"] == ["add-label:ok"]


def test_edit_issue_needs_something_to_edit():
    with pytest.raises(s.GhError, match="--body-file"):
        s.cmd_edit_issue(_EditGh(), 5)


def test_rest_issue_set_body_patches_the_issue():
    from tests.test_sdlc_next import ScriptedRunner
    argv = ("gh", "api", "-X", "PATCH", "repos/owner/repo/issues/5", "-f", "body=hi")
    runner = ScriptedRunner({argv: "{}"})
    s.GitHubRest(runner=runner).issue_set_body(5, "hi")
    assert runner.calls == [list(argv)]


def test_cli_wires_edit_issue(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_edit_issue", lambda gh, *a: seen.append((gh, a)) or {})
    assert s.main(["edit-issue", "5", "--body-file", "/f", "--add-label", "a",
                   "--add-label", "b", "--remove-label", "c"]) == 0
    assert seen == [("GH", (5, "/f", ["a", "b"], ["c"]))]


# ---- 5: handoff-to-pr-review refuses while a required check is red ----

class _HandoffGh(_ChecksGh):
    def __init__(self, files, checks, logs=None):
        super().__init__(files, checks)
        self.logs, self.comments = logs or {}, []

    def job_failed_log(self, job):
        return self.logs.get(job, "")

    def issue_comment(self, n, body):
        self.comments.append(body)

    def issue_view(self, n, *fields):
        return {"comments": [{"body": b} for b in self.comments]}


def _check(name, bucket, job=None, workflow="Backend CI"):
    link = f"https://github.com/o/r/actions/runs/1/job/{job}" if job else ""
    return {"name": name, "bucket": bucket, "workflow": workflow, "link": link}


def test_handoff_refuses_on_a_failed_required_check():
    gh = _HandoffGh(["backend/a.py"], [_check("backend", "fail", job=7)],
                    logs={7: "AssertionError: expected 2"})
    result = s.cmd_handoff_to_pr_review(gh, 42, 77, "done")
    assert result["refused"] == "checks_failed" and gh.comments == []
    assert result["failing"] == [{"name": "backend",
                                  "failure_excerpt": "AssertionError: expected 2"}]
    assert result["hint"] == "fix the failing check, push, and hand off again"


def test_handoff_proceeds_past_pending_and_infra_suspect_checks():
    gh = _HandoffGh(["backend/a.py"],
                    [_check("backend", "fail", job=7), _check("backend-it", "pending")],
                    logs={7: "No space left on device"})
    result = s.cmd_handoff_to_pr_review(gh, 42, 77, "done")
    assert result["queued_for"] == "pr-review" and "refused" not in result
    assert result["checks_pending"] == ["backend-it"] and result["infra_suspect"] == ["backend"]
    assert len(gh.comments) == 1


def test_handoff_ignores_a_failed_check_outside_the_required_workflows():
    # Positive control: an unrelated workflow's failure, or a workflow the PR does not touch.
    gh = _HandoffGh(["backend/a.py"], [_check("lint", "fail", workflow="Other"),
                                       _check("fe", "fail", workflow="Frontend CI"),
                                       _check("backend", "pass")])
    result = s.cmd_handoff_to_pr_review(gh, 42, 77, "done")
    assert result == {"issue": 42, "pr": 77, "queued_for": "pr-review"}


def test_handoff_without_pr_level_ci_behaves_as_before():
    # Positive control.
    gh = _HandoffGh(["backend/a.py"], [])
    assert s.cmd_handoff_to_pr_review(gh, 42, 77, "done") == {
        "issue": 42, "pr": 77, "queued_for": "pr-review"}
    assert len(gh.comments) == 1
