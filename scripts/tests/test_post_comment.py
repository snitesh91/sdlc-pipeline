"""post-comment: an authoring stage's handoff comment, role-tagged and claim-checked."""
import json

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh


def _gh(stage="product", status="in-progress", comments=()):
    return FakeGh([{"number": 5, "stage": stage, "status": status, "comments": list(comments)}])


def _body(tmp_path, text="Handoff: doc at docs/x.md, Effort Low."):
    f = tmp_path / "c.md"
    f.write_text(text)
    return str(f)


def test_posts_the_body_with_a_role_marker(tmp_path):
    gh = _gh()
    out = s.cmd_post_comment(gh, 5, "product", _body(tmp_path))
    assert out == {"issue": 5, "role": "product", "posted": True}
    (comment,) = gh.comments_on(5)
    assert comment.startswith("Handoff: doc at docs/x.md")
    assert "<!-- role-comment: product @ " in comment


@pytest.mark.parametrize("status", ["awaiting-human-review", "feedback-received"])
def test_a_stage_revising_on_gate_feedback_may_still_post(tmp_path, status):
    assert s.cmd_post_comment(_gh(status=status), 5, "product", _body(tmp_path))["posted"]


@pytest.mark.parametrize("stage,status", [("architecture", "in-progress"), (None, "in-progress"),
                                          ("product", "todo"), ("product", "needs-human")])
def test_refused_unless_the_issue_is_claimed_at_that_role(tmp_path, stage, status):
    gh = _gh(stage, status)
    with pytest.raises(s.GhError, match="not claimed"):
        s.cmd_post_comment(gh, 5, "product", _body(tmp_path))
    assert gh.comments_on(5) == []


def test_reads_stage_and_status_from_issue_fields_not_issue_view(tmp_path):
    """Regression: `gh issue view` has no `fields`, so reading them there always saw
    Stage None / status None and refused every claimed stage."""
    gh = _gh("lld", "in-progress")
    assert "fields" not in gh.issue_view(5)
    assert s.cmd_post_comment(gh, 5, "lld", _body(tmp_path))["posted"] is True


def test_refuses_a_review_role_and_an_empty_or_missing_file(tmp_path):
    gh = _gh()
    with pytest.raises(s.GhError, match="role must be"):
        s.cmd_post_comment(gh, 5, "exploratory", _body(tmp_path))
    with pytest.raises(s.GhError, match="empty"):
        s.cmd_post_comment(gh, 5, "product", _body(tmp_path, "  \n"))
    with pytest.raises(s.GhError, match="readable"):
        s.cmd_post_comment(gh, 5, "product", str(tmp_path / "nope.md"))


def test_over_cap_body_is_refused_before_any_tracker_call(tmp_path):
    gh = _gh()
    out = s.cmd_post_comment(gh, 5, "product", _body(tmp_path, "x" * (s.HANDOFF_CAP + 1)))
    assert out["refused"] is True and f"{s.HANDOFF_CAP:,}" in out["reason"]
    assert gh.comments_on(5) == []
    assert s.cmd_post_comment(gh, 5, "product", _body(tmp_path, "x" * s.HANDOFF_CAP))["posted"]


def test_cli_wires_post_comment(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_post_comment", lambda *a: seen.append(a) or {"posted": True})
    assert s.main(["post-comment", "5", "--role", "lld", "--body-file", "f.md"]) == 0
    assert seen == [("GH", 5, "lld", "f.md")]
    assert json.loads(capsys.readouterr().out) == {"posted": True}


# --- review roles, development's rework note, a parked unit (retro 2026-10-09) ---

@pytest.mark.parametrize("role,stage", [("product-review", "product"),
                                        ("arch-review", "architecture"),
                                        ("lld-review", "lld"), ("pr-review", "pr-review")])
def test_a_review_role_posts_its_review_at_the_stage_it_follows(tmp_path, role, stage):
    """Regression (cloud-review-comment-path): review agents had no post path."""
    gh = _gh(stage)
    assert s.cmd_post_comment(gh, 5, role, _body(tmp_path))["posted"] is True
    assert f"<!-- role-comment: {role} @ " in gh.comments_on(5)[-1]


@pytest.mark.parametrize("role,stage", [("arch-review", "lld"), ("lld-review", "architecture"),
                                        ("pr-review", "development"),
                                        ("product-review", "architecture")])
def test_a_review_role_is_refused_at_another_stage(tmp_path, role, stage):
    gh = _gh(stage)
    with pytest.raises(s.GhError, match="not claimed"):
        s.cmd_post_comment(gh, 5, role, _body(tmp_path))
    assert gh.comments_on(5) == []


@pytest.mark.parametrize("stage", ["development", "pr-review"])
def test_development_posts_its_rework_note(tmp_path, stage):
    """Regression (dev-rework-post-comment-denied): rework runs at Stage pr-review."""
    gh = _gh(stage)
    assert s.cmd_post_comment(gh, 5, "development", _body(tmp_path))["posted"] is True


def test_a_review_comment_gets_the_evidence_cap(tmp_path):
    gh = _gh("architecture")
    assert s.cmd_post_comment(gh, 5, "arch-review",
                              _body(tmp_path, "x" * s.EVIDENCE_CAP))["posted"] is True
    out = s.cmd_post_comment(gh, 5, "arch-review", _body(tmp_path, "x" * (s.EVIDENCE_CAP + 1)))
    assert out["refused"] is True and out["posted"] is False
    assert out["cap"] == s.EVIDENCE_CAP and out["chars"] == s.EVIDENCE_CAP + 1
    assert "NOTHING WAS POSTED" in out["reason"] and "6,000" in out["reason"]
    assert len(gh.comments_on(5)) == 1


def test_a_handoff_over_its_cap_says_nothing_was_posted(tmp_path):
    out = s.cmd_post_comment(_gh(), 5, "development", _body(tmp_path, "x" * (s.HANDOFF_CAP + 1)))
    assert out["refused"] is True and out["cap"] == s.HANDOFF_CAP
    assert "NOTHING WAS POSTED" in out["reason"]


PAUSE = ("\u23f8\ufe0f Paused \u2014 `lld` found this doesn't fit epic #9's current architecture. "
         "Epic #9's architecture is being revised; see gate PR #77.")


def test_a_unit_parked_for_epic_regate_still_takes_its_stages_comment(tmp_path):
    """Regression (post-comment-refused-while-parked): pause-for-epic-regate sets status
    `todo`, and the parked stage's own handoff was refused."""
    gh = _gh("lld", "todo", comments=["\U0001f6a7 Picking this up \u2014 lld stage starting.",
                                      PAUSE])
    assert s.cmd_post_comment(gh, 5, "lld", _body(tmp_path))["posted"] is True


@pytest.mark.parametrize("comments", [
    [],                                                     # never parked
    [PAUSE, "\U0001f6a7 Picking this up \u2014 lld stage starting."],  # resumed since
])
def test_a_todo_unit_that_is_not_parked_is_still_refused(tmp_path, comments):
    gh = _gh("lld", "todo", comments=comments)
    with pytest.raises(s.GhError, match="not claimed"):
        s.cmd_post_comment(gh, 5, "lld", _body(tmp_path))


def test_a_parked_unit_takes_only_its_own_stages_comment(tmp_path):
    gh = _gh("lld", "todo", comments=[PAUSE])
    with pytest.raises(s.GhError, match="not claimed"):
        s.cmd_post_comment(gh, 5, "architecture", _body(tmp_path))
