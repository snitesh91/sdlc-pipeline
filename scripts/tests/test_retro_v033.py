"""Regression + positive-control tests for the v0.3.3 retro control-plane fixes.

Each fix pairs a regression test (goes RED against the pre-fix code) with a positive
control (stays GREEN either way, so the guard is not merely always-failing)."""
import json
from types import SimpleNamespace

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_v2_phase_tasks import FakeGh, DOC, repo, _push_doc_branch  # noqa: F401


# ---- item 1: development PR must not touch design docs / other Tasks' footprints ----

_LLD = (
    "## Task #10: Widget <!-- task-key: widget -->\n"
    "### Footprint\n"
    "- `src/widget.py`\n\n"
    "## Task #11: Gadget <!-- task-key: gadget -->\n"
    "### Footprint\n"
    "- `src/gadget.py`\n"
)


def _scope_gh():
    return FakeGh([{"number": 9, "labels": ["type:epic"]},
                   {"number": 10, "labels": ["type:task"], "parent": 9},
                   {"number": 11, "labels": ["type:task"], "parent": 9}])


def _lld_runner(text=_LLD):
    def run(argv):
        if "show" in argv:
            return text
        raise GhError(f"unexpected: {argv}")
    return run


def test_dev_pr_scope_flags_a_foreign_footprint_path():
    offenders = s.dev_pr_scope_offenders(_scope_gh(), 10, ["src/gadget.py"],
                                         runner=_lld_runner())
    assert offenders["foreign_footprint"] == ["src/gadget.py"]
    assert "another Task's Footprint" in s._dev_pr_scope_refusal_reason(offenders)


def test_dev_pr_scope_flags_an_epic_design_doc():
    offenders = s.dev_pr_scope_offenders(_scope_gh(), 10, [f"{DOC}/epic-9/lld.md"],
                                         runner=_lld_runner())
    assert offenders["design_docs"] == [f"{DOC}/epic-9/lld.md"]
    reason = s._dev_pr_scope_refusal_reason(offenders)
    assert "development authors no design doc" in reason


def test_dev_pr_scope_allows_the_units_own_footprint():
    # Positive control: a change inside the unit's own footprint is clean.
    offenders = s.dev_pr_scope_offenders(_scope_gh(), 10, ["src/widget.py"],
                                         runner=_lld_runner())
    assert offenders == {"design_docs": [], "foreign_footprint": []}
    assert s._dev_pr_scope_refusal_reason(offenders) is None


class _OpenDevGh:
    """Minimal write surface for open-dev-pr; records whether a PR was created."""

    def __init__(self, changed):
        self.changed, self.created = changed, False

    def pr_list_for_branch(self, branch, state="open"):
        return []

    def issue_list(self):
        return FakeGh([{"number": 9, "labels": ["type:epic"]},
                       {"number": 10, "labels": ["type:task"], "parent": 9}]).issue_list()

    def files_since(self, sha, branch):
        return self.changed

    def pr_create(self, base, head, title, body, draft):
        self.created = True
        return 42

    def set_stage_field(self, n, stage):
        pass

    def issue_comment(self, n, body):
        pass


def test_open_dev_pr_refuses_a_diff_that_edits_a_design_doc():
    gh = _OpenDevGh([f"{DOC}/epic-9/lld.md"])
    result = s.cmd_open_dev_pr(gh, 10, "t", "b", "s")
    assert result["refused"] is True and result["created"] is False
    assert result["offending_design_docs"] == [f"{DOC}/epic-9/lld.md"]
    assert gh.created is False  # never opened the PR


def test_open_dev_pr_opens_normally_for_an_in_scope_diff():
    # Positive control: an ordinary code diff opens the PR.
    gh = _OpenDevGh(["src/widget.py"])
    result = s.cmd_open_dev_pr(gh, 10, "t", "b", "s")
    assert result.get("created") is True and result["pr"] == 42


# ---- item 2: pr-review start-comment refuses a stale local-ci attestation after sync ----

class _StaleGh:
    """PR surface for pr_stale_attestations / start-comment(pr-review)."""

    def __init__(self, head, comments, changed, delta_by_sha):
        self.head, self.comments = head, comments
        self.changed, self.delta_by_sha = changed, delta_by_sha
        self.posted = []

    def pr_view(self, n, fields=""):
        return {"headRefOid": self.head, "comments": self.comments, "baseRefName": "epic-9"}

    def pr_files(self, n):
        return self.changed

    def pr_checks(self, n):
        return []

    def files_since(self, sha, branch):
        return self.delta_by_sha.get(sha, [])

    def pr_list_for_branch(self, branch, state="open"):
        return [{"number": 55, "headRefName": branch}]

    def issue_comment(self, n, body):
        self.posted.append(body)


def _attestation(sha):
    return [{"body": f"<!-- local-ci: backend:55 @ {sha} -->"}]


def test_pr_stale_attestations_flags_an_attestation_that_is_not_the_head():
    gh = _StaleGh("bbbbbbb", _attestation("aaaaaaa"), ["backend/x.py"],
                  {"aaaaaaa": ["backend/y.py"]})  # sync touched suite files -> stale
    status = s.pr_stale_attestations(gh, 55, 55)
    assert [x["suite"] for x in status["stale"]] == ["backend"] and status["carried"] == []


def test_pr_stale_attestations_carries_forward_a_docs_only_sync():
    # Positive control: sync only merged files outside the suite's coverage -> carried forward.
    gh = _StaleGh("bbbbbbb", _attestation("aaaaaaa"), ["backend/x.py"],
                  {"aaaaaaa": ["docs/readme.md"]})
    status = s.pr_stale_attestations(gh, 55, 55)
    assert status["stale"] == [] and status["carried"] == ["backend"]


def test_start_comment_pr_review_refuses_a_stale_attestation_without_posting():
    gh = _StaleGh("bbbbbbb", _attestation("aaaaaaa"), ["backend/x.py"],
                  {"aaaaaaa": ["backend/y.py"]})
    result = s.cmd_start_comment(gh, 55, "pr-review")
    assert result["refused"] is True and result["ok"] is False
    assert result["stale_suites"] == ["backend"] and gh.posted == []  # no start comment posted


def test_start_comment_pr_review_proceeds_when_the_attestation_is_fresh():
    # Positive control: a fresh attestation at head lets the start comment post.
    gh = _StaleGh("bbbbbbb", _attestation("bbbbbbb"), ["backend/x.py"], {})
    result = s.cmd_start_comment(gh, 55, "pr-review")
    assert result["started"] == "pr-review" and len(gh.posted) == 1


# ---- item 3: record-local-ci refuses an unexpanded placeholder ----

def _rec_gh():
    return FakeGh([{"number": 1, "labels": ["type:task"]}], prs={42: {"headRefName": "issue-1"}})


def test_record_local_ci_refuses_an_unexpanded_placeholder(tmp_path):
    out = tmp_path / "o.txt"
    out.write_text("ok\n")
    with pytest.raises(GhError, match="unexpanded placeholder"):
        s.cmd_record_local_ci(_rec_gh(), 42, "backend", "abc1234",
                              "docker run -v <node_modules-volume>:/n make backend-it",
                              str(out))


@pytest.mark.parametrize("command", [
    "make backend-it 2>&1 | tee out.log",   # redirection, not a placeholder
    "pytest -q < input.txt",                 # input redirection with a space
    "diff <(a) <(b) && make test",           # process substitution
])
def test_record_local_ci_accepts_real_commands_with_shell_redirection(tmp_path, command):
    # Positive control: legit shell syntax is not mistaken for a placeholder.
    out = tmp_path / "o.txt"
    out.write_text("ok\n")
    result = s.cmd_record_local_ci(_rec_gh(), 42, "backend", "abc1234", command, str(out))
    assert result["attested"] is True


# ---- item 4: Critical/Blocker priority aliases + repair overrides ----

def test_issue_field_values_accepts_critical_and_blocker_as_urgent():
    assert s.issue_field_values(priority="Critical")["priority"] == "Urgent"
    assert s.issue_field_values(priority="blocker")["priority"] == "Urgent"


def test_issue_field_values_still_rejects_a_real_nonsense_priority():
    # Positive control: a genuinely invalid value is still refused, listing valid values.
    with pytest.raises(GhError, match="valid values: High, Low, Medium, Urgent"):
        s.issue_field_values(priority="Nonsense")


def test_create_issue_accepts_critical_priority_as_urgent():
    from tests.test_sdlc_next import _RecordingGh
    result = s.cmd_create_issue(_RecordingGh(), "t", "b", 9, [], priority="Critical")
    assert result["priority"] == "Urgent"


def test_file_closing_delta_self_repairs_a_half_created_issue():
    # create-issue fails mid-way (set_pipeline_status); file-closing-delta repairs in place
    # with the requested priority rather than duplicating the issue on retry.
    from tests.test_sdlc_next import _RecordingGh

    class _Gh(_RecordingGh):
        def __init__(self):
            super().__init__(number=77)
            self._failed_once = False

        def set_pipeline_status_field(self, n, status):
            if not self._failed_once:  # one-shot transient failure mid-create
                self._failed_once = True
                raise GhError("transient")
            self.calls.append(("set_pipeline_status", n, status))

        def issue_list(self):
            return [{"number": 9, "labels": [{"name": "type:epic"}], "parent": None,
                     "state": "OPEN", "issueType": None, "issueFieldValues": {"nodes": []}}]

        def issue_epic_info(self, n):
            return {"issueType": {"name": "Bug"}, "parent": {"number": 9}, "labels": []}

        def issue_fields(self, n):
            return {}  # nothing set yet -> repair fills status + priority + effort

    gh = _Gh()
    result = s.cmd_file_closing_delta(gh, 9, "bug", "body", priority="Critical")
    assert result["delta_issue"] == 77 and result["ok"] is True
    assert "repair-issue" in result["completed_steps"]
    assert ("set_priority", 77, "Urgent") in gh.calls  # repaired to the requested priority
    assert [c[0] for c in gh.calls].count("issue_create") == 1  # never duplicated


# ---- item 5: next-action resume carries liveness info ----

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _ts(minutes_ago):
    from datetime import timedelta
    return (_now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _ResumeGh(FakeGh):
    def __init__(self, claim_minutes_ago):
        super().__init__([{"number": 90, "labels": ["type:epic", "epic:standing"]},
                          {"number": 1, "labels": ["type:task"], "parent": 90,
                           "stage": "development", "status": "in-progress"}])
        self._claim_minutes = claim_minutes_ago

    def issue_view(self, n):
        view = super().issue_view(n)
        if self._claim_minutes is not None:
            view["comments"] = [{"body": "🚧 Picking this up — development stage starting.",
                                 "createdAt": _ts(self._claim_minutes)}]
        return view


def test_next_action_resume_flags_a_fresh_claim_as_likely_live():
    result = s.cmd_next_action(_ResumeGh(5), SimpleNamespace(epic=90, run_id=None, skip_epic=[]))
    assert result["action"] == "resume" and result["likely_live"] is True
    assert result["claim_age_seconds"] is not None
    assert "another session may" in result["reason"]


def test_next_action_resume_old_claim_is_not_live():
    # Positive control: a claim past the window is safe to resume.
    result = s.cmd_next_action(_ResumeGh(90), SimpleNamespace(epic=90, run_id=None, skip_epic=[]))
    assert result["action"] == "resume" and result["likely_live"] is False
    assert "safe to resume" in result["reason"]


def test_next_action_resume_reports_null_age_when_no_claim_timestamp():
    result = s.cmd_next_action(_ResumeGh(None), SimpleNamespace(epic=90, run_id=None, skip_epic=[]))
    assert result["claim_age_seconds"] is None and result["likely_live"] is False
    assert "no claim timestamp" in result["reason"]


# ---- item 6: merge-pr books the terminal count under the passed run id ----

def test_record_terminal_unit_books_under_the_passed_run_id_not_a_stale_one(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 10)
    # A stale probe run wrote the epic's state file.
    s._write_run_state(9, {"run_id": "probe-83507", "terminal": [], "in_flight": {"5": "dev"}})
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 5, "labels": ["type:task"], "parent": 9}])
    summary = s.record_terminal_unit(gh, 5, run_id="real-run-1")
    assert summary["run_id"] == "real-run-1"  # not probe-83507
    assert s.read_run_state(9)["run_id"] == "real-run-1"


def test_record_terminal_unit_without_run_id_keeps_the_state_file_id(monkeypatch, tmp_path):
    # Positive control: with no --run-id, the state file's current id stands.
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    s._write_run_state(9, {"run_id": "run-A", "terminal": []})
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 5, "labels": ["type:task"], "parent": 9}])
    summary = s.record_terminal_unit(gh, 5)
    assert summary["run_id"] == "run-A"


# ---- item 7: start-stage ensures the epic branch + worktree first ----

def test_start_stage_ensures_the_epic_worktree_before_the_childs(monkeypatch):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"}])
    calls = []
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda g, n, unit="issue", *a, **k: (calls.append((n, unit)),
                                                             {"path": "/wt", "branch": f"{unit}-{n}"})[1])
    monkeypatch.setattr(s, "cmd_claim", lambda g, n, r: {"claimed": True})
    result = s.cmd_start_stage(gh, 10, "development")
    assert (9, "epic") in calls and calls.index((9, "epic")) < calls.index((10, "issue"))
    assert result["completed_steps"] == ["check-claimable", "epic-worktree", "worktree-add",
                                         "claim"]


def test_start_stage_skips_the_epic_worktree_for_a_main_based_unit(monkeypatch):
    # Positive control: a parentless (main-based) unit adds only its own worktree.
    gh = FakeGh([{"number": 10, "labels": ["type:task"]}])
    calls = []
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda g, n, unit="issue", *a, **k: (calls.append((n, unit)),
                                                             {"path": "/wt"})[1])
    monkeypatch.setattr(s, "cmd_claim", lambda g, n, r: {"claimed": True})
    result = s.cmd_start_stage(gh, 10, "development")
    assert calls == [(10, "issue")]
    assert "epic-worktree" not in result["completed_steps"]


# ---- item 8: per-base scope, attestable:false, and the missing-workflow hint ----

def _spec(**kw):
    base = {"workflow": "Backend CI", "suite": "backend", "prefixes": ("backend/",),
            "files": (), "excludeGlobs": (), "bases": (), "attestable": True}
    base.update(kw)
    return base


def test_missing_required_workflows_honours_a_base_scope(monkeypatch):
    it = _spec(workflow="Backend IT", suite="backend-it", bases=("main",))
    monkeypatch.setattr(s, "REQUIRED_WORKFLOWS", (it,))
    # Into an epic branch: the main-only entry does not apply.
    assert s.missing_required_workflows(["backend/x.py"], [], [], "sha", base_ref="epic-9") == []
    # Positive control: into main it is required.
    assert s.missing_required_workflows(["backend/x.py"], [], [], "sha",
                                        base_ref="main") == ["Backend IT"]


def test_record_local_ci_refuses_a_non_attestable_suite(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "LOCAL_CI_SUITES", ("backend", "backend-it"))
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset({"backend-it"}))
    out = tmp_path / "o.txt"
    out.write_text("ok\n")
    with pytest.raises(GhError, match="attestable: false"):
        s.cmd_record_local_ci(_rec_gh(), 42, "backend-it", "abc1234", "make it", str(out))
    # Positive control: an attestable suite still records.
    assert s.cmd_record_local_ci(_rec_gh(), 42, "backend", "abc1234", "make test",
                                 str(out))["attested"] is True


# ---- item 10: arch-review start-comment surfaces the skip threshold ----

def test_start_comment_arch_review_surfaces_the_skip_threshold():
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 10, "labels": ["type:task"], "parent": 9}])
    result = s.cmd_start_comment(gh, 10, "arch-review")
    assert result["skip_confidence_threshold"] == 80


def test_start_comment_other_review_role_has_no_threshold():
    # Positive control: a non-arch review role carries no threshold field.
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]},
                 {"number": 10, "labels": ["type:task"], "parent": 9}])
    result = s.cmd_start_comment(gh, 10, "lld-review")
    assert "skip_confidence_threshold" not in result
