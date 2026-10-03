"""`pipeline.humanChannel: session`: operator answers recorded on the unit, and in-session
gate decisions -- `approve-gate` (parity with the operator's merge on GitHub) and
`request-gate-changes` (into the existing address-gate-feedback path)."""
import io
import json
from contextlib import redirect_stdout

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _open_design, _origin_file,  # noqa: F401
                                       _push_doc_branch, repo)

ARCH = {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
        "status": "in-progress"}


class GateGh(FakeGh):
    """FakeGh whose PR labels can be removed (the gate label approve-gate drops)."""

    def pr_remove_label(self, n, label):
        self.prs[n]["labels"] = [x for x in self.prs[n].get("labels", []) if x != label]


def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


# --- config ------------------------------------------------------------------------------

def test_human_channel_defaults_to_github_and_validates(monkeypatch):
    monkeypatch.setattr(s, "plugin_version_info", lambda runner=None: {})
    assert s.human_channel() == "github"
    assert s.cmd_show_config()["human_channel"] == "github"
    monkeypatch.setitem(s.PIPELINE, "humanChannel", "Session")
    assert s.cmd_show_config()["human_channel"] == "session"
    monkeypatch.setitem(s.PIPELINE, "humanChannel", "slack")
    assert "error" in s.cmd_show_config()["human_channel"]
    with pytest.raises(s.GhError, match="humanChannel"):
        s.human_channel()


# --- record-operator-answer -------------------------------------------------------------------

def test_record_operator_answer_posts_question_and_answer():
    gh = FakeGh([{"number": 5, "labels": ["type:task"], "stage": "architecture",
                  "status": "in-progress"}])

    result = s.cmd_record_operator_answer(gh, 5, "Queue or cron?", "Cron, nightly")

    body = gh.comments_on(5)[-1]
    assert "**Q:** Queue or cron?" in body and "**A:** Cron, nightly" in body
    assert "<!-- sdlc:operator-answer @ " in body
    assert result == {"issue": 5, "recorded": True, "unparked": False}
    assert gh.issues[5]["status"] == "in-progress"


def test_record_operator_answer_unparks_a_needs_human_unit():
    gh = FakeGh([{"number": 5, "labels": ["type:task"], "stage": "product",
                  "status": "needs-human"}])
    assert s.cmd_record_operator_answer(gh, 5, "Scope?", "Admins only")["unparked"] is True
    assert gh.issues[5]["status"] == "todo"


def test_record_operator_answer_refuses_empty_and_oversized_text(monkeypatch):
    gh = FakeGh([{"number": 5, "labels": ["type:task"]}])
    with pytest.raises(s.GhError, match="non-empty"):
        s.cmd_record_operator_answer(gh, 5, "  ", "x")
    big = s.cmd_record_operator_answer(gh, 5, "q" * 1500, "a" * 600)
    assert big["refused"] is True and gh.comments_on(5) == []
    code, out = _cli(monkeypatch, gh, ["record-operator-answer", "5", "--question", "Q?",
                                       "--answer", "A"])
    assert code == 0 and out["recorded"] is True


# --- approve-gate --------------------------------------------------------------------------------

def _standing_gate(repo):
    """A standing child #91 at an open Gate B (`issue-91` -> main), PR labelled sdlc:gate."""
    gh = GateGh([{"number": 90, "labels": ["type:epic", "epic:standing"]},
                 {"number": 91, "labels": ["type:task"], "parent": 90, "stage": "architecture",
                  "status": "in-progress"}])
    gh.repo = repo
    _push_doc_branch(repo, "issue-91", f"{DOC}/issue-91/architecture.md", "# arch\n")
    pr = s.cmd_open_gate(gh, str(repo), 91, "Fix it", "architecture.md", "development",
                         "x")["gate_pr"]
    gh.prs[pr]["files"] = [f"{DOC}/issue-91/architecture.md"]
    return gh, pr


def _phase_gate(repo):
    """An Epic's Architecture-phase Task #10 with its design PR gated for the human."""
    gh = GateGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6}, dict(ARCH)])
    pr = _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md", "development",
                    "x")
    return gh, pr


def test_approve_gate_refuses_without_the_operators_session_approval(repo):
    gh, pr = _standing_gate(repo)
    result = s.cmd_approve_gate(gh, 91, by_operator_session=False, repo_path=str(repo))
    assert result["refused"] is True and "--by-operator-session" in result["reason"]
    assert gh.merges == [] and gh.prs[pr]["labels"] == ["sdlc:gate"]


def test_approve_gate_refuses_a_unit_with_no_open_gate():
    gh = GateGh([{"number": 5, "labels": ["type:task"], "status": "in-progress"}])
    assert "no gate-pr marker" in s.cmd_approve_gate(gh, 5, True)["reason"]
    gh.issues[5]["comments"] = ["<!-- gate-pr: product:40 -->"]
    assert "no open gate" in s.cmd_approve_gate(gh, 5, True)["reason"]


@pytest.mark.parametrize("path", ["merged on GitHub", "approved in session"])
def test_a_phase_task_gate_ends_the_same_either_way(repo, path):
    gh, pr = _phase_gate(repo)
    if path == "merged on GitHub":
        # The operator's squash-merge, then the Action's auto-pass-gate.
        gh.pr_merge(pr, delete_branch=False, method="squash")
        result = s.cmd_auto_pass_gate(gh, str(repo), pr)
    else:
        approved = s.cmd_approve_gate(gh, 10, by_operator_session=True, note="LGTM",
                                      repo_path=str(repo))
        assert approved["approved"] is True and approved["merge"]["method"] == "squash"
        result = approved["pass_gate"]

    assert result["phase_task_complete"] is True and gh.merge_methods == ["squash"]
    assert gh.issues[10]["state"] == "CLOSED" and gh.issues[10]["status"] == "done"
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"


def test_approve_gate_leaves_an_audit_trail_and_drops_the_gate_label(repo):
    gh, pr = _phase_gate(repo)

    result = s.cmd_approve_gate(gh, 10, by_operator_session=True, note="LGTM",
                                repo_path=str(repo))

    audit = "\n".join(gh.comments_on(10))
    assert "approved `architecture.md` in session" in audit and "LGTM" in audit
    assert f"<!-- gate-approved-in-session: architecture:{pr} @ " in audit
    assert f"<!-- gate-merged: architecture:{pr} @ " in audit
    assert "Approved by the operator in a Claude Code session" in gh.prs[pr]["comments"][-1]
    # The Action never double-passes it: the label is gone before the merge.
    assert result["gate_label_removed"] is True and gh.prs[pr]["labels"] == []


@pytest.mark.parametrize("path", ["merged on GitHub", "approved in session"])
def test_a_standing_childs_gate_reaches_the_same_stage_either_way(repo, path):
    gh, pr = _standing_gate(repo)
    if path == "merged on GitHub":
        gh.pr_merge(pr, delete_branch=False, method="merge")
        s.cmd_auto_pass_gate(gh, str(repo), pr)
    else:
        result = s.cmd_approve_gate(gh, 91, by_operator_session=True, repo_path=str(repo))
        # Merge commit, branch kept: merge-gate's semantics for a standing child's gate.
        assert result["merge"]["method"] == "merge" and gh.merges == [(pr, False)]
        assert result["pass_gate"]["claimed"] is True

    assert gh.issues[91]["stage"] == "development"
    # The one difference: this session carries on, so it claims the stage it moves to.
    expected = "todo" if path == "merged on GitHub" else "in-progress"
    assert gh.issues[91]["status"] == expected


def test_approve_gate_keeps_merge_gates_refusals_and_the_gate_label(repo):
    gh, pr = _standing_gate(repo)
    gh.behind["issue-91"] = 3

    result = s.cmd_approve_gate(gh, 91, by_operator_session=True, repo_path=str(repo))

    assert result["refused"] is True and result["merge"]["behind_base"] == 3
    assert "sync-branch 91" in result["reason"]
    assert gh.merges == [] and gh.prs[pr]["labels"] == ["sdlc:gate"]
    assert gh.issues[91]["status"] == "awaiting-human-review"


def test_approve_gate_restores_the_label_when_the_merge_fails(repo):
    gh, pr = _standing_gate(repo)

    def boom(*_a, **_k):
        raise s.GhError("HTTP 502")
    gh.pr_merge = boom

    with pytest.raises(s.GhError):
        s.cmd_approve_gate(gh, 91, by_operator_session=True, repo_path=str(repo))
    assert gh.prs[pr]["labels"] == ["sdlc:gate"]


def test_approve_gate_on_a_gate_the_human_already_merged_just_passes_it(repo):
    gh, pr = _standing_gate(repo)
    gh.pr_merge(pr, delete_branch=False, method="merge")

    result = s.cmd_approve_gate(gh, 91, by_operator_session=True, repo_path=str(repo))

    assert result["approved"] is True and result["merge"]["already_merged"] is True
    assert gh.merges == [(pr, False)] and gh.issues[91]["stage"] == "development"


def test_approve_gate_through_the_cli_needs_the_flag(monkeypatch):
    gh = GateGh([{"number": 5, "labels": ["type:task"], "status": "awaiting-human-review",
                  "comments": ["<!-- gate-pr: product:40 -->"]}])
    code, out = _cli(monkeypatch, gh, ["approve-gate", "5"])
    assert code == 0 and out["refused"] is True and gh.merges == []


# --- request-gate-changes ---------------------------------------------------------------------

def _open_gate(status="awaiting-human-review", pr_state="OPEN"):
    return GateGh([{"number": 5, "labels": ["type:task"], "stage": "product", "status": status,
                    "comments": ["<!-- gate-pr: product:40 -->"]}],
                  {40: {"headRefName": "issue-5", "baseRefName": "main", "state": pr_state,
                        "createdAt": "2026-10-01T00:00:00Z"}})


def test_request_gate_changes_posts_feedback_and_routes_to_address_gate_feedback():
    gh = _open_gate()

    result = s.cmd_request_gate_changes(gh, 5, "Drop the CSV export; admins only.")

    assert result["pipeline_status"] == "feedback-received" and result["gate_pr"] == 40
    assert "Drop the CSV export; admins only." in gh.prs[40]["comments"][-1]
    assert gh.issues[5]["status"] == "feedback-received"
    assert "<!-- gate-changes-in-session: product:40 @ " in gh.comments_on(5)[-1]


def test_the_posted_feedback_is_what_evaluate_gate_reads_as_pending():
    gh = _open_gate()
    gh.unresolved_review_threads = lambda pr: []
    real_view = gh.pr_view

    def view(n, fields=""):
        pr = real_view(n, fields)
        pr["comments"] = [{"body": c["body"], "createdAt": "2026-10-03T00:00:00Z"}
                          for c in pr["comments"]]
        return pr
    gh.pr_view = view
    assert s.evaluate_gate(gh, 5)["status"] == "not_satisfied"

    s.cmd_request_gate_changes(gh, 5, "Rename the endpoint.")

    gate = s.evaluate_gate(gh, 5)
    assert gate["status"] == "feedback_pending"
    assert "Rename the endpoint." in gate["new_comments"][0]["body"]


@pytest.mark.parametrize("status,pr_state,needle", [
    ("in-progress", "OPEN", "no open gate"),
    ("awaiting-human-review", "MERGED", "no longer open"),
])
def test_request_gate_changes_refuses_without_an_open_gate(status, pr_state, needle):
    gh = _open_gate(status, pr_state)
    result = s.cmd_request_gate_changes(gh, 5, "x")
    assert result["refused"] is True and needle in result["reason"]
    assert gh.prs[40]["comments"] == []


def test_request_gate_changes_cli_takes_text_or_a_file(monkeypatch, tmp_path):
    gh = _open_gate()
    f = tmp_path / "fb.md"
    f.write_text("From a file.\n")
    code, out = _cli(monkeypatch, gh, ["request-gate-changes", "5", "--feedback-file", str(f)])
    assert code == 0 and "From a file." in gh.prs[40]["comments"][-1]
    big = s.cmd_request_gate_changes(_open_gate(), 5, "x" * 2500)
    assert big["refused"] is True
