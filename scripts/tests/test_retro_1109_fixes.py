"""Retro 2026-11-09 (v0.4.5) fixes: ci-local marker on the last line only, ci-failure-summary
naming the failing test, development's handoff owning Stage pr-review on a rework round, and
pr-review's post-comment at its unclaimed `todo` Stage."""
import sdlc_next as s
from sdlc.epic_close import _mark_ci_local
from tests.test_retro_0930_ci import _link, _log
from tests.test_retro_1105_context import _runner

M = s.CI_LOCAL_MARKER


class _BodyGh:
    def __init__(self, body):
        self.body, self.edits = body, []

    def pr_view(self, pr, fields):
        return {"body": self.body}

    def pr_edit_body(self, pr, body):
        self.edits.append(body)

    def ensure_label(self, name):
        pass

    def pr_add_label(self, pr, name):
        pass


def test_a_quoted_marker_mid_body_still_gets_the_real_marker_appended():
    gh = _BodyGh(f"Gates on `{M}` in the body.\n\nCloses #5")
    _mark_ci_local(gh, 9)
    assert gh.edits and gh.edits[0].endswith(f"\n\n{M}")


def test_a_body_already_ending_in_the_marker_is_left_alone():
    gh = _BodyGh(f"Closes #5\n\n{M}\n")
    _mark_ci_local(gh, 9)
    assert gh.edits == []


NEST_NOISE = _log(*[f"[Nest] 1  - 11/05/2026 ERROR [ExceptionsHandler] boom {i}" for i in range(25)],
                  "\x1b[31mFAIL\x1b[39m src/cart.spec.ts", "  ● Console", "  ● cart › totals",
                  "Tests:       1 failed, 40 passed")


def _summary(log):
    runner = _runner([{"name": "Backend IT", "bucket": "fail", "link": _link(1, 11)}], {11: log})
    [failing] = s.cmd_ci_failure_summary(s.GitHub(runner=runner), 42)["failing"]
    return failing["failures"]


def test_logger_error_lines_are_not_failures_and_ansi_is_stripped():
    assert _summary(NEST_NOISE) == ["FAIL src/cart.spec.ts", "  ● cart › totals",
                                    "Tests:       1 failed, 40 passed"]


def test_no_test_marker_falls_back_to_the_step_name_and_last_lines():
    log = _log(*[f"line {i}" for i in range(30)], "##[error]Process completed with exit code 1")
    lines = _summary(log)
    assert lines[0] == "##[error]Process completed with exit code 1"
    assert "failing step: Run tests" in lines[1]
    assert lines[2:] == [f"line {i}" for i in range(21, 30)]


class _ClaimGh:
    def __init__(self, stage, status):
        self.fields = {"Stage": stage, "Pipeline Status": status}
        self.comments, self.stages = [], []

    def issue_fields(self, n):
        return self.fields

    def issue_view(self, n, *a):
        return {"comments": [{"body": b} for b in self.comments]}

    def issue_comment(self, n, body):
        self.comments.append(body)

    def set_stage_field(self, n, stage):
        self.stages.append(stage)


def test_pr_review_posts_its_comment_while_todo_at_stage_pr_review(tmp_path):
    body = tmp_path / "c.md"
    body.write_text("verdict: clean")
    gh = _ClaimGh("PR Review", "Todo")
    assert s.cmd_post_comment(gh, 5, "pr-review", str(body))["posted"] is True


def test_development_still_needs_its_claim_at_todo(tmp_path):
    body = tmp_path / "c.md"
    body.write_text("rework note")
    gh = _ClaimGh("Development", "Todo")
    gh.issue_view = lambda n, *a: {"comments": []}
    try:
        s.cmd_post_comment(gh, 5, "development", str(body))
    except s.GhError as e:
        assert "not claimed" in str(e)
    else:
        raise AssertionError("development posted while unclaimed")


SUMMARY = "done\n\n### Acceptance criteria coverage\n| AC | test |"


def _handoff(gh, monkeypatch):
    monkeypatch.setattr(s.handoff, "required_check_states", lambda gh, pr: None, raising=False)
    return s.cmd_handoff_to_pr_review(gh, 5, 9, SUMMARY)


def test_a_rework_handoff_moves_stage_back_to_pr_review_even_when_already_posted(monkeypatch):
    gh = _ClaimGh("Development", "In Progress")
    text = f"✅ Development complete. PR #9 is queued for `pr-review`.\n\n{SUMMARY}"
    gh.comments.append(f"{text}\n\n<!-- stage-transition: development->pr-review @ t -->")
    result = _handoff(gh, monkeypatch)
    assert result.get("already_posted") is True and gh.stages == ["pr-review"]


def test_a_handoff_at_stage_pr_review_sets_no_stage(monkeypatch):
    gh = _ClaimGh("PR Review", "In Progress")
    _handoff(gh, monkeypatch)
    assert gh.stages == []
