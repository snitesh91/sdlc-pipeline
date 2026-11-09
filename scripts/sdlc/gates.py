"""Passing, skipping, waiving and merging gates; gate feedback."""
from __future__ import annotations

import re
from typing import Callable, Optional

from .config import (
    EPIC_BRANCH_PREFIX, GATE_PENDING_STATUSES, INITIATIVE_BRANCH_PREFIX, ISSUE_BRANCH_PREFIX, PIPELINE,
    PIPELINE_STATUS_FIELD_NAMES, Runner, STAGE_AFTER_GATE, issue_branch,
)
from .github import GhError, GitHub, _default_runner, _utc_now_marker
from .units import doc_root_for, is_epic, is_epic_standing, is_initiative, resolve_profile
from .evidence import _DESIGN_PR_BODY_MARKER, epic_branch, find_gate_pr, integration_base
from .worktrees import BranchWorkspace, _with_workspace, branch_lock
from .handoff import cmd_claim
from .design_gates import _merge_design_pr_of, find_design_pr, git_reconcile_branch


def _gate_base(gh: GitHub, issue: int) -> str:
    """Where a non-phase gate PR merges: the unit's `integration_base` (a standing child's
    `epic-<n>`, an Initiative child's `initiative-<i>`, else `main`); a parentless issue's is
    `main` with no issue-list read."""
    if not gh.issue_epic_info(issue).get("parent"):
        return "main"
    return integration_base(gh, issue)


def phase_task_parent(gh: GitHub, issue: int) -> Optional[dict]:
    """`{"number", "kind": "initiative"|"epic"}` when `issue`'s parent makes it a
    phase-Task, else None. Only meaningful on a gate path: under an Initiative or
    non-standing Epic, only phase children ever open a gate."""
    parent = gh.issue_epic_info(issue).get("parent")
    if not parent:
        return None
    info = gh.issue_epic_info(parent["number"])
    if is_initiative(info):
        return {"number": parent["number"], "kind": "initiative"}
    if is_epic(info) and not is_epic_standing(info):
        return {"number": parent["number"], "kind": "epic"}
    return None


def _complete_phase_task(gh: GitHub, repo_path: Optional[str], issue: int, stage: str,
                         parent: dict, note: str, runner: Runner, markers: str = "") -> dict:
    """Finish a phase-Task whose gate passed or was skipped: for an Epic's Architecture-phase
    Task, first confirm `architecture.md` is on `epic-<e>` (its design PR merged), then close
    the Task. It has no next stage; stays open if the doc is not on the Epic branch."""
    result = {"issue": issue, "unit": "issue", "phase_task_complete": False,
              "parent": parent["number"]}
    if stage == "architecture" and parent["kind"] == "epic":
        doc = f"{doc_root_for(parent['number'], gh)}/epic-{parent['number']}/architecture.md"
        on_epic = gh.path_on_ref(doc, epic_branch(parent["number"]))
        result["doc_on_epic_branch"] = on_epic
        if not on_epic:
            result["reason"] = (f"`{doc}` is not on `{epic_branch(parent['number'])}` -- the "
                                f"design PR has not merged; the Task stays open. Merge-design-pr "
                                f"(or the human's merge) must land first, then re-run")
            return result
    gh.issue_comment(issue,
        f"✅ {note}\n\nPhase-Task complete — closing it; its parent #{parent['number']} "
        f"carries the flow from here.\n\n"
        f"{markers}<!-- phase-task-complete: {stage} @ {_utc_now_marker()} -->")
    closed = cmd_close_issue(gh, issue, repo_path=repo_path, runner=runner)
    return {**result, "phase_task_complete": True, "closed": True, "worktree": closed["worktree"],
            "cleanup": closed["cleanup"],
            **({"warnings": closed["warnings"]} if closed.get("warnings") else {})}


def _merged_design_pr(gh: GitHub, comments: list, pr: int) -> Optional[tuple]:
    """`(stage, pr)` when `pr` is the issue's design PR and it is already MERGED: a human
    merged it before any gate opened, so there is no gate-pr marker to check against."""
    found = find_design_pr(comments)
    if found and found[1] == pr and gh.pr_view(pr, fields="state").get("state") == "MERGED":
        return found
    return None


def cmd_pass_gate(gh: GitHub, repo_path: str, issue: int, gate_pr: int, stage: str,
                   runner: Runner = _default_runner, live: bool = True,
                   doc_only: bool = False) -> dict:
    """Pass a merged gate: reconcile the branch with its integration base, then claim the next stage
    (`live`) or only advance Stage (`live=False`, CI). Refuses a `stage`/`gate_pr` that
    disagrees with the issue's gate-pr marker (or, when no gate opened, its design PR that a
    human merged early); a phase-Task is closed instead."""
    issue_data = gh.issue_view(issue)
    found = (find_gate_pr(issue_data.get("comments", []))
             or _merged_design_pr(gh, issue_data.get("comments", []), gate_pr))
    if not found:
        raise GhError(f"issue #{issue} has no gate-pr marker in its comments -- cannot verify "
                       f"which stage this gate belongs to")
    actual_stage, actual_pr = found
    if actual_pr != gate_pr:
        raise GhError(f"issue #{issue}'s gate-pr marker points at PR #{actual_pr}, not #{gate_pr} "
                       f"-- pass the gate_pr next-action itself returned, not a guessed number")
    if actual_stage != stage:
        raise GhError(f"issue #{issue}'s gate-pr marker says this gate belongs to stage="
                       f"{actual_stage!r}, not stage={stage!r} -- pass next-action's own 'stage' "
                       f"field verbatim; it is the gate's owning doc-stage, not a target you pick")
    branch = issue_branch(issue)
    parent = phase_task_parent(gh, issue)
    if doc_only and (parent is not None or not live):
        raise GhError(f"--doc-only closes a standing child or parentless issue whose gate doc "
                      f"is its whole deliverable; #{issue} is "
                      f"{'a phase-Task (it closes on its own)' if parent else 'passed by CI'}")
    # The gate merged into the unit's integration base (its Epic's branch for a phase-Task
    # of a non-standing Epic), so reconcile the issue branch with that.
    base = (epic_branch(parent["number"]) if parent and parent["kind"] == "epic"
            else _gate_base(gh, issue))
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        git_reconcile_branch(ws.path, branch, base=base, runner=runner)
    if parent is not None:
        return _with_workspace(_complete_phase_task(
            gh, repo_path, issue, stage, parent,
            f"Human review confirmed for `{stage}.md` — merged via #{gate_pr}.", runner),
            ws)
    if doc_only:
        gh.issue_comment(issue, f"✅ Human review confirmed for `{stage}.md` — merged via "
                                f"#{gate_pr}. The doc was this unit's whole deliverable: closing "
                                f"it, no `development` stage.")
        return _with_workspace({**cmd_close_issue(gh, issue, repo_path, runner),
                                "unit": "issue", "next_stage": None, "claimed": False,
                                "doc_only": True}, ws)
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = _utc_now_marker()
    prefix = f"✅ Human review confirmed for `{stage}.md` — merged via #{gate_pr} — "
    marker = f"<!-- stage-transition: human-review:{stage}->{next_stage} @ {timestamp} -->"
    if live:
        gh.issue_comment(issue, f"{prefix}proceeding to `{next_stage}` stage.\n\n{marker}")
        cmd_claim(gh, issue, next_stage)
    else:
        gh.issue_comment(issue,
            f"{prefix}Stage advanced to `{next_stage}`. Pick up with `/sdlc:run` "
            f"whenever you're ready to run this stage.\n\n{marker}")
        gh.set_stage_field(issue, next_stage)
        gh.set_pipeline_status_field(issue, "todo")
    return _with_workspace({"issue": issue, "unit": "issue", "next_stage": next_stage,
                            "claimed": live}, ws)


def _profile_for_issue(gh: GitHub, issue: int) -> dict:
    """The profile of `issue`'s parent epic (profile labels live on the epic), or the
    default profile when it has no parent."""
    parent = gh.issue_epic_info(issue).get("parent")
    info = gh.issue_epic_info(parent["number"]) if parent else None
    return resolve_profile(info)


def cmd_skip_gate(gh: GitHub, issue: int, stage: str, confidence: int, summary: str,
                   repo_path: Optional[str] = None, runner: Runner = _default_runner) -> dict:
    """Skip Gate B when arch-review's confidence exceeds the profile's
    `skipConfidenceThreshold`; claims `development`, or completes a phase-Task (an Epic's
    Architecture-phase Task first merges its design PR into the epic branch)."""
    if stage != "architecture":
        raise GhError(f"only the architecture gate (Gate B) may be skipped, got stage={stage!r}")
    threshold = _profile_for_issue(gh, issue)["gates"]["skipConfidenceThreshold"]
    if confidence <= threshold:
        raise GhError(f"confidence {confidence} does not clear the "
                       f"{threshold} threshold required to skip Gate B")
    parent = phase_task_parent(gh, issue)
    if parent is not None:
        if parent["kind"] == "epic":
            merge = _merge_design_pr_of(gh, issue, repo_path, runner, gate_cleared=True)
            if not (merge.get("merged") or merge.get("already_merged")):
                return {"issue": issue, "unit": "issue", "phase_task_complete": False,
                        "parent": parent["number"], "design_pr": merge,
                        "reason": merge.get("reason", "the design PR did not merge")}
        return _complete_phase_task(
            gh, repo_path, issue, stage, parent,
            f"⚡ Gate B skipped — arch-review reported {confidence}% confidence (> "
            f"{threshold}% threshold) that `architecture.md` is structurally sound. {summary}",
            runner, markers=f"<!-- arch-review-confidence: {confidence} -->\n")
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = _utc_now_marker()
    gh.issue_comment(issue,
        f"⚡ Gate B skipped — arch-review reported {confidence}% confidence "
        f"(> {threshold}% threshold) that `architecture.md` is "
        f"structurally sound. {summary} Proceeding directly to `{next_stage}` without "
        f"human sign-off, per the confidence-skip policy in \"Human-review gates\" "
        f"(references/gates.md).\n\n"
        f"<!-- arch-review-confidence: {confidence} -->\n"
        f"<!-- stage-transition: arch-review->{next_stage} @ {timestamp} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": "issue", "next_stage": next_stage, "skipped": True, "confidence": confidence}


# The doc-stage a gate approves -> the profile `gates` toggle requiring a human there,
# and the review that precedes that gate.
HUMAN_GATE_TOGGLES = {"product": "requiresHumanGateA", "architecture": "requiresHumanGateB"}


_GATE_REVIEW = {"product": "product-review", "architecture": "arch-review"}


def cmd_waive_gate(gh: GitHub, issue: int, stage: str, summary: str,
                   repo_path: Optional[str] = None, runner: Runner = _default_runner) -> dict:
    """Pass `stage`'s gate without a human when the profile's `requiresHumanGateA|B` is
    false: claims the next stage, or completes a phase-Task (an Epic's Architecture-phase Task
    first merges its design PR). Raises when a human is required."""
    toggle = HUMAN_GATE_TOGGLES.get(stage)
    if toggle is None:
        raise GhError(f"stage must be one of {sorted(HUMAN_GATE_TOGGLES)} (the doc the gate "
                      f"approves), got {stage!r}")
    profile = _profile_for_issue(gh, issue)
    if profile["gates"][toggle]:
        raise GhError(f"profile '{profile['name']}' requires a human at this gate "
                      f"({toggle}: true) -- open a gate, do not waive it")
    note = (f"⚡ Human review of `{stage}.md` waived — profile '{profile['name']}' sets "
            f"{toggle}: false. {summary}")
    marker = f"<!-- gate-waived: {stage}:{profile['name']} -->\n"
    parent = phase_task_parent(gh, issue)
    if parent is not None:
        if parent["kind"] == "epic" and stage == "architecture":
            merge = _merge_design_pr_of(gh, issue, repo_path, runner, gate_cleared=True)
            if not (merge.get("merged") or merge.get("already_merged")):
                return {"issue": issue, "unit": "issue", "phase_task_complete": False,
                        "parent": parent["number"], "design_pr": merge,
                        "reason": merge.get("reason", "the design PR did not merge")}
        return _complete_phase_task(gh, repo_path, issue, stage, parent, note, runner,
                                    markers=marker)
    next_stage = STAGE_AFTER_GATE[stage]
    gh.issue_comment(issue, f"{note} Proceeding to `{next_stage}`.\n\n{marker}"
                            f"<!-- stage-transition: {_GATE_REVIEW[stage]}->{next_stage} "
                            f"@ {_utc_now_marker()} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": "issue", "next_stage": next_stage, "waived": True,
            "profile": profile["name"]}


_CLOSES_ISSUE_RE = re.compile(r"\bCloses #\d+", re.IGNORECASE)


# Gate PRs are `issue-<n>` -> main, or -- for a non-standing Epic's architecture phase-Task --
# its design PR `issue-<n>` -> `epic-<e>` (recognised by its body marker). An `epic-<n>` head is
# the epic's integration PR, never a gate.
_GATE_BRANCH_RE = re.compile(rf"^{re.escape(ISSUE_BRANCH_PREFIX)}(?P<issue_n>\d+)$")


# Bot logins end in "[bot]"; ignoring them stops a bot comment from looping the workflow.
_BOT_AUTHOR_RE = re.compile(r"\[bot\]$")


class _NotAGate(Exception):
    """Raised by `_match_open_gate` when a PR is not an open gate; carries the skip reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def cmd_merge_gate(gh: GitHub, pr_number: int, issue: int, stage: str,
                   operator_confirmed: bool = False) -> dict:
    """Merge an open human-review gate PR (Gate A's `issue-<n>` -> `main`, or a design PR held
    for the human) because the operator said to. Refuses (exit 0) without `operator_confirmed`,
    for a PR that is no open gate of `issue`/`stage`, when behind its base (`behind_base`) or
    without green required checks. A design PR and a Roadmap Task's gate squash; a standing or
    parentless issue's merges as a merge commit; the branch is always kept. `pass-gate`
    (next-action, or the Action) then finishes the bookkeeping."""
    if not operator_confirmed:
        return {"pr": pr_number, "issue": issue, "stage": stage, "merged": False, "refused": True,
                "reason": "merge-gate merges a human-review gate PR only when the operator "
                          "explicitly said to; re-run with --operator-confirmed after they did"}
    return _merge_gate_pr(
        gh, pr_number, issue, stage,
        pr_note="Merged by the pipeline at the operator's explicit instruction.",
        issue_note=lambda base: f"✅ Gate PR #{pr_number} merged into `{base}` at the "
                                f"operator's instruction.")


def _merge_gate_pr(gh: GitHub, pr_number: int, issue: int, stage: str, pr_note: str,
                   issue_note: Callable[[str], str], extra_markers: str = "",
                   before_merge: Optional[Callable[[], None]] = None) -> dict:
    """`merge-gate`'s checks and merge, shared with `approve-gate`: refuses (exit 0) a PR that is
    no open gate of `issue`/`stage`, behind its base or without green checks; `before_merge`
    runs only once every check passed."""
    result = {"pr": pr_number, "issue": issue, "stage": stage, "merged": False}
    pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    if pr.get("state") == "MERGED":
        return {**result, "already_merged": True, "next": _pass_gate_hint(issue, pr_number, stage)}
    if pr.get("state") != "OPEN":
        return {**result, "refused": True,
                "reason": f"PR #{pr_number} is closed without merging -- a human decision"}
    try:
        matched, marker_stage, _status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {**result, "refused": True, "reason": f"not an open gate PR: {e.reason}"}
    if matched != issue or marker_stage != stage:
        return {**result, "refused": True,
                "reason": f"PR #{pr_number} is #{matched}'s {marker_stage} gate, not #{issue}'s "
                          f"{stage} gate"}
    head, base = pr["headRefName"], pr["baseRefName"]
    behind = gh.branch_behind_by(head, base=base)
    if behind:
        return {**result, "behind_base": behind,
                "reason": f"branch {head} is {behind} commit(s) behind {base} -- run "
                          f"sync-branch {issue}, wait for fresh checks, then re-run"}
    view = gh.pr_view(pr_number, "comments,headRefOid")
    checks_state, missing = merge_gate_status(gh.pr_files(pr_number), gh.pr_checks(pr_number),
                                              view.get("comments", []), view.get("headRefOid"))
    if checks_state != "passed":
        return {**result, "checks": checks_state, "missing_required_workflows": missing,
                "reason": f"PR #{pr_number} checks not passed (status={checks_state})"
                          + (f"; no passing check or attestation from: {', '.join(missing)}"
                             if missing else "")}
    design = bool(_DESIGN_PR_BODY_MARKER.search(pr.get("body") or ""))
    method = "squash" if design or phase_task_parent(gh, issue) is not None else "merge"
    if before_merge is not None:
        before_merge()
    try:
        gh.pr_merge(pr_number, delete_branch=False, method=method)
    except GhError:
        # GitHub can answer 5xx after the merge already landed; only a PR still open failed.
        if gh.pr_view(pr_number, fields="state").get("state") != "MERGED":
            raise
    gh.pr_comment(pr_number, pr_note)
    gh.issue_comment(issue,
        f"{issue_note(base)}\n\n"
        f"<!-- gate-merged: {stage}:{pr_number} @ {_utc_now_marker()} -->{extra_markers}")
    return {**result, "merged": True, "method": method, "base": base, "design_pr": design,
            "next": _pass_gate_hint(issue, pr_number, stage)}


def _pass_gate_hint(issue: int, pr: int, stage: str) -> str:
    return f"pass-gate {issue} --gate-pr {pr} --stage {stage} (next-action returns it)"


def _match_open_gate(gh: GitHub, pr: dict, pr_number: int) -> tuple:
    """Identify `pr` as some issue's currently open gate PR (status in
    `GATE_PENDING_STATUSES`, matching gate-pr marker). Returns
    `(issue_number, marker_stage, status)`; raises `_NotAGate(reason)` otherwise."""
    m = _GATE_BRANCH_RE.match(pr.get("headRefName") or "")
    if not m:
        raise _NotAGate(f"PR #{pr_number} head branch {pr.get('headRefName')!r} is not "
                         f"an issue-<n> branch")
    issue_number = int(m.group("issue_n"))
    # main, or an integration branch: a design PR's or a standing child's `epic-<n>`, or an
    # Initiative child's `initiative-<i>`. The marker check below pins the exact PR.
    base = pr.get("baseRefName") or ""
    if base != "main" and not base.startswith((EPIC_BRANCH_PREFIX, INITIATIVE_BRANCH_PREFIX)):
        raise _NotAGate(f"PR #{pr_number} base is {base!r}, not main or an integration branch")
    design = _DESIGN_PR_BODY_MARKER.search(pr.get("body") or "")
    if design and int(design.group(2)) != issue_number:
        raise _NotAGate(f"PR #{pr_number}'s design-pr marker names #{design.group(2)}, not "
                        f"#{issue_number}")

    if _CLOSES_ISSUE_RE.search(pr.get("body") or ""):
        raise _NotAGate(f"PR #{pr_number} body contains 'Closes #' -- this is the "
                         f"development PR (opened/closed by the pipeline itself), not "
                         f"a human-reviewed gate PR")

    issue_data = gh.issue_view(issue_number)
    fields = gh.issue_fields(issue_number)
    status = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    if status not in GATE_PENDING_STATUSES:
        raise _NotAGate(f"issue #{issue_number} is not currently awaiting-human-review "
                         f"or feedback-received (Pipeline Status={status!r}) -- no open "
                         f"gate for this PR")

    found = find_gate_pr(issue_data.get("comments", []))
    if not found:
        raise _NotAGate(f"issue #{issue_number} has no gate-pr marker comment")
    marker_stage, marker_pr = found
    if marker_pr != pr_number:
        raise _NotAGate(f"issue #{issue_number}'s open gate marker points at PR "
                         f"#{marker_pr}, not #{pr_number} -- not the currently open gate")

    return issue_number, marker_stage, status


def cmd_auto_pass_gate(gh: GitHub, repo_path: str, pr_number: int,
                        runner: Runner = _default_runner) -> dict:
    """CI entry point for any closed PR: a merged gate PR passes the gate with
    `live=False`; one closed unmerged marks the issue needs-human. Any non-gate PR
    returns `{"ok": True, "skipped": reason}` with no mutation."""
    try:
        pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    except GhError as e:
        return {"ok": False, "reason": f"could not fetch PR #{pr_number}: {e}"}

    merged = pr.get("state") == "MERGED" and bool(pr.get("mergedAt"))
    closed_unmerged = pr.get("state") == "CLOSED" and not merged
    if not merged and not closed_unmerged:
        return {"ok": True, "skipped": f"PR #{pr_number} is not closed "
                                        f"(state={pr.get('state')!r})"}

    try:
        issue_number, marker_stage, _status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    try:
        if merged:
            result = cmd_pass_gate(gh, repo_path, issue_number, pr_number, marker_stage,
                                    runner=runner, live=False)
        else:
            result = cmd_mark_needs_human(
                gh, issue_number,
                reason=f"Gate PR #{pr_number} closed without merging — human rejected "
                       f"the doc, needs direct operator input.",
                repo_path=repo_path)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    result["ok"] = True
    return result


def cmd_mark_feedback_received(gh: GitHub, pr_number: int, author: str, body: str = "") -> dict:
    """CI entry point: flip an open gate's Pipeline Status from `awaiting-human-review`
    to `feedback-received` when a human leaves non-empty feedback. Visibility only;
    anything else returns `{"ok": True, "skipped": reason}`."""
    if _BOT_AUTHOR_RE.search(author or ""):
        return {"ok": True, "skipped": f"comment/review author {author!r} is a bot"}
    if not (body or "").strip():
        return {"ok": True, "skipped": "no body text on this review/comment -- not "
                                        "actual feedback (e.g. an approval with no comment)"}

    try:
        pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    except GhError as e:
        return {"ok": False, "reason": f"could not fetch PR #{pr_number}: {e}"}

    try:
        issue_number, _marker_stage, status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    if status != "awaiting-human-review":
        return {"ok": True, "skipped": f"issue #{issue_number} Pipeline Status is "
                                        f"{status!r}, not awaiting-human-review -- "
                                        f"nothing to flip forward"}

    try:
        gh.set_pipeline_status_field(issue_number, "feedback-received")
        gh.issue_comment(issue_number,
            f"💬 Feedback landed on gate PR #{pr_number} — now awaiting pipeline action "
            f"(`Feedback Received`). The `address-gate-feedback` step picks this up "
            f"automatically; see \"Human-review gates\" in the pipeline docs.")
    except GhError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "issue": issue_number, "gate_pr": pr_number,
            "pipeline_status": "feedback-received"}


def cmd_mark_feedback_addressed(gh: GitHub, issue: int) -> dict:
    """Set Pipeline Status back to `awaiting-human-review` unconditionally, after gate
    feedback is addressed and pushed. Posts no comment."""
    gh.set_pipeline_status_field(issue, "awaiting-human-review")
    return {"issue": issue, "pipeline_status": "awaiting-human-review"}


def cmd_resolve_thread(gh: GitHub, thread_id: str, reply: Optional[str] = None) -> dict:
    """Reply to (when `reply`) and resolve one PR review thread."""
    if reply:
        gh.reply_review_thread(thread_id, reply)
    try:
        gh.resolve_review_thread(thread_id)
    except GhError as e:
        if not reply:
            raise
        return {"thread": thread_id, "replied": True, "resolved": False, "ok": False,
                "error": str(e), "reason": "the reply was posted but resolving failed -- "
                                          "re-run without --reply so it is not posted twice"}
    return {"thread": thread_id, "replied": bool(reply), "resolved": True}


def cmd_pause_for_epic_regate(gh: GitHub, issue: int, epic: int,
                              gate_pr: Optional[int] = None, found_by: str = "lld") -> dict:
    """Park a unit waiting on its Epic's Architecture revision: Pipeline Status `todo` (Stage
    kept) so it re-enters the normal loop once the revision Task closes (its `blockedBy` edge
    holds it until then). `gate_pr` is the revision's design/gate PR once it exists (it is
    only raised after the revision's `architecture` stage finishes); `found_by` names the
    stage that hit the deviation."""
    gh.set_pipeline_status_field(issue, "todo")
    where = (f"see gate PR #{gate_pr}" if gate_pr is not None
             else "its design PR opens once the revision's architecture stage finishes")
    gh.issue_comment(issue,
        f"⏸️ Paused — `{found_by}` found this doesn't fit epic #{epic}'s current "
        f"architecture. Epic #{epic}'s architecture is being revised; {where}. This task "
        f"resumes automatically once the revision closes.")
    return {"issue": issue, "paused_for_epic_regate": epic, "gate_pr": gate_pr,
            "found_by": found_by}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import dev_pr  # noqa: E402,F401
from .checks import merge_gate_status  # noqa: E402
from .issue_ops import cmd_close_issue, cmd_mark_needs_human  # noqa: E402
