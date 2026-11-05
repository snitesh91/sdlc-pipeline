"""The human channel: operator answers and in-session gate decisions."""
from __future__ import annotations

import io
import os
from typing import Optional

from .config import GATE_PENDING_STATUSES, LABELS, PIPELINE, PIPELINE_STATUS_FIELD_NAMES, Runner
from .github import GhError, GitHub, WorkItemProvider, _default_runner, _utc_now_marker
from .evidence import find_gate_pr
from .gates import _NotAGate, _merge_gate_pr, cmd_pass_gate


# --- human channel: in-session operator answers and gate approvals ---------------------

HUMAN_CHANNELS = ("github", "session")


def human_channel() -> str:
    """`pipeline.humanChannel`: "github" (needs-human, gate PRs) or "session" (AskUserQuestion)."""
    value = str(PIPELINE.get("humanChannel") or "github").strip().lower()
    if value not in HUMAN_CHANNELS:
        raise GhError(f"pipeline.humanChannel={PIPELINE.get('humanChannel')!r} is neither "
                      f"'github' nor 'session' -- fix the config")
    return value


def _human_channel_or_error():
    try:
        return human_channel()
    except GhError as e:
        return {"error": str(e)}


def _read_text_arg(text: Optional[str], path: Optional[str], flag: str) -> str:
    """`text`, or the contents of `path`; exactly one of them, non-empty."""
    if (text is None) == (path is None):
        raise GhError(f"pass exactly one of --{flag} / --{flag}-file")
    if path is not None:
        try:
            with io.open(os.path.expanduser(path), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise GhError(f"--{flag}-file must be a readable file: {exc}")
    text = (text or "").strip()
    if not text:
        raise GhError(f"--{flag} is empty")
    return text


def _issue_pipeline_status(gh: WorkItemProvider, issue: int) -> Optional[str]:
    return PIPELINE_STATUS_FIELD_NAMES.get(gh.issue_fields(issue).get("Pipeline Status"))


def cmd_record_operator_answer(gh: WorkItemProvider, issue: int, question: str,
                               answer: str) -> dict:
    """Record an operator's in-session answer on `issue` (question + answer, one comment); a
    unit parked `needs-human` goes back to `todo`, its question settled."""
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        raise GhError("--question and --answer must both be non-empty")
    if len(question) + len(answer) > HANDOFF_CAP:
        return {"issue": issue, "refused": True,
                "reason": f"question + answer are {len(question) + len(answer):,} chars, over "
                          f"the {HANDOFF_CAP:,}-char cap; trim the question to its gist and "
                          f"re-run"}
    gh.issue_comment(issue,
        f"🗣️ Operator decision, asked in session\n\n**Q:** {question}\n\n**A:** {answer}\n\n"
        f"<!-- sdlc:operator-answer @ {_utc_now_marker()} -->")
    unparked = _issue_pipeline_status(gh, issue) == "needs-human"
    if unparked:
        gh.set_pipeline_status_field(issue, "todo")
    return {"issue": issue, "recorded": True, "unparked": unparked}


def _open_gate_of(gh: WorkItemProvider, issue: int) -> tuple:
    """`(stage, gate_pr, status)` of `issue`'s open gate; raises `_NotAGate` when it has none."""
    found = find_gate_pr(gh.issue_view(issue).get("comments", []))
    if not found:
        raise _NotAGate(f"#{issue} has no gate-pr marker -- no gate was opened on it")
    status = _issue_pipeline_status(gh, issue)
    if status not in GATE_PENDING_STATUSES:
        raise _NotAGate(f"#{issue} has no open gate (Pipeline Status={status!r})")
    return found[0], found[1], status


def cmd_approve_gate(gh: GitHub, issue: int, by_operator_session: bool = False,
                     note: Optional[str] = None, repo_path: Optional[str] = None,
                     runner: Runner = _default_runner) -> dict:
    """The operator approved `issue`'s open gate in session: what their merge on GitHub does
    (merge the gate PR, then `pass-gate`), plus an audit comment; the next stage is claimed
    because this session carries on. Refuses (exit 0) without `by_operator_session`, with no
    open gate, or on `merge-gate`'s refusals."""
    result = {"issue": issue, "approved": False}
    if not by_operator_session:
        return {**result, "refused": True,
                "reason": "approve-gate records an approval the operator gave in this session; "
                          "re-run with --by-operator-session only after they chose Approve"}
    try:
        stage, pr, _status = _open_gate_of(gh, issue)
    except _NotAGate as e:
        return {**result, "refused": True, "reason": e.reason}
    result.update(gate_pr=pr, stage=stage)
    gate_label = LABELS["gate"]
    label = {"removed": False}

    def drop_gate_label():
        # The Action wakes only for a labelled PR; this session passes the gate itself.
        try:
            gh.pr_remove_label(pr, gate_label)
            label["removed"] = True
        except GhError:
            pass

    note_text = f" {note.strip()}" if note and note.strip() else ""
    marker = f"\n<!-- gate-approved-in-session: {stage}:{pr} @ {_utc_now_marker()} -->"
    try:
        merge = _merge_gate_pr(
            gh, pr, issue, stage,
            pr_note="Approved by the operator in a Claude Code session; merged by the pipeline.",
            issue_note=lambda base: f"✅ The operator approved `{stage}.md` in session — gate "
                                    f"PR #{pr} merged into `{base}`.{note_text}",
            extra_markers=marker, before_merge=drop_gate_label)
    except GhError:
        if label["removed"]:
            gh.pr_add_label(pr, gate_label)
        raise
    if not (merge.get("merged") or merge.get("already_merged")):
        return {**result, "refused": True, "merge": merge, "reason": merge.get("reason")}
    if merge.get("already_merged"):
        gh.issue_comment(issue, f"✅ The operator approved `{stage}.md` in session; gate PR "
                                f"#{pr} was already merged.{note_text}{marker}")
    passed = cmd_pass_gate(gh, repo_path, issue, pr, stage, runner=runner, live=True)
    out = {**result, "approved": True, "merge": merge, "pass_gate": passed,
           "gate_label_removed": label["removed"]}
    if passed.get("warnings"):
        out["warnings"] = passed["warnings"]
    return out


def cmd_request_gate_changes(gh: WorkItemProvider, issue: int, feedback: str) -> dict:
    """The operator asked for changes to `issue`'s open gate in session: post the text on the
    gate PR as feedback and flip Pipeline Status to `feedback-received`, so `next-action`
    returns `address-gate-feedback`."""
    if len(feedback) > HANDOFF_CAP:
        return {"issue": issue, "refused": True,
                "reason": f"the feedback is {len(feedback):,} chars, over the {HANDOFF_CAP:,}-"
                          f"char cap; split it or trim it and re-run"}
    try:
        stage, pr, _status = _open_gate_of(gh, issue)
    except _NotAGate as e:
        return {"issue": issue, "refused": True, "reason": e.reason}
    if gh.pr_view(pr, fields="state").get("state") != "OPEN":
        return {"issue": issue, "gate_pr": pr, "refused": True,
                "reason": f"gate PR #{pr} is no longer open -- next-action returns what to do "
                          f"(pass-gate on a merge)"}
    gh.pr_comment(pr, f"Operator feedback, given in a Claude Code session:\n\n{feedback}")
    gh.set_pipeline_status_field(issue, "feedback-received")
    gh.issue_comment(issue,
        f"💬 The operator requested changes on #{pr} in session; the pipeline revises the doc."
        f"\n\n<!-- gate-changes-in-session: {stage}:{pr} @ {_utc_now_marker()} -->")
    return {"issue": issue, "gate_pr": pr, "stage": stage, "pipeline_status": "feedback-received",
            "next": "next-action returns address-gate-feedback for this unit"}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import cloud  # noqa: E402,F401
from .composites import HANDOFF_CAP  # noqa: E402
