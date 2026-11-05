"""Stage handoffs, review records, local-CI attestation, comments, claim and route."""
from __future__ import annotations

import io
import os
import re
from typing import Optional

from .config import (
    ESCALATION, GATE_PENDING_STATUSES, PIPELINE_STATUS_FIELD_NAMES, RETIRED_ROLES,
    STAGE_FIELD_NAMES, STAGE_OPTION_IDS, issue_branch,
)
from .github import GhError, GitHub, WorkItemProvider, _utc_now_marker
from .units import SKIP_THRESHOLD_HINTS_FILE, _record_hints, is_epic, is_epic_standing
from .evidence import (
    LOCAL_CI_COMMAND_PATTERNS, LOCAL_CI_SUITES, _PR_REVIEW_OUTCOME_MARKER,
    _UNEXPANDED_PLACEHOLDER, LOCAL_CI_CLOUD_MARKER, last_pr_review_outcome, last_transition_to,
    remote_session,
)


def required_check_states(gh: GitHub, pr: int) -> Optional[dict]:
    """The PR head's checks from the required workflows it touches (base-scoped):
    `{failing, infra_suspect, pending}` lists of checks; None when no check reported at all."""
    checks = effective_checks(gh, pr)
    if not checks:
        return None
    base = gh.pr_view(pr, "headRefOid,baseRefName").get("baseRefName")
    files = gh.pr_files(pr)
    required = {w["workflow"] for w in required_workflows(base) if _workflow_applies_to_base(w, base)
                and any(_workflow_covers(w, p) for p in files)}
    mine = [c for c in checks if c.get("workflow", "") in required]
    failed = [c for c in mine if _is_failed_check(c)]
    enrich_failed_checks(gh, failed)
    return {"failing": [c for c in failed if not c["infra_suspect"]],
            "infra_suspect": [c for c in failed if c["infra_suspect"]],
            "pending": [c for c in mine if c.get("bucket") == "pending"]}


HANDOFF_COVERAGE_HEADING = "### Acceptance criteria coverage"


def cmd_handoff_to_pr_review(gh: GitHub, issue: int, pr: int, summary: str) -> dict:
    """Post the `development->pr-review` handoff marker that queues the PR for review.
    Run after every development round, including rework. Refuses (exit 0) while a required
    check has failed for a non-infra reason, or when `summary` (the handoff comment) lacks
    the `### Acceptance criteria coverage` table pr-review reads first. Returns `queued_for`."""
    if HANDOFF_COVERAGE_HEADING not in summary:
        return {"issue": issue, "pr": pr, "refused": "no_coverage_table",
                "hint": f"--summary is the handoff comment (agents/development.md): include "
                        f"its `{HANDOFF_COVERAGE_HEADING}` section, then hand off again"}
    states = required_check_states(gh, pr)
    if states and states["failing"]:
        return {"issue": issue, "pr": pr, "refused": "checks_failed",
                "failing": [{"name": c.get("name"), "failure_excerpt": c.get("failure_excerpt")}
                            for c in states["failing"]],
                "hint": "fix the failing check, push, and hand off again"}
    extra = {key: [c.get("name") for c in states[src]]
             for key, src in (("checks_pending", "pending"), ("infra_suspect", "infra_suspect"))
             if states and states[src]}
    text = f"✅ Development complete. PR #{pr} is queued for `pr-review`.\n\n{summary.strip()}"
    if _handoff_already_posted(gh, issue, text):
        return {"issue": issue, "pr": pr, "queued_for": "pr-review", "already_posted": True, **extra}
    timestamp = _utc_now_marker()
    gh.issue_comment(issue,
        f"{text}\n\n"
        f"<!-- stage-transition: development->pr-review @ {timestamp} -->")
    return {"issue": issue, "pr": pr, "queued_for": "pr-review", **extra}


def _handoff_already_posted(gh: GitHub, issue: int, text: str) -> bool:
    """Whether the latest `development->pr-review` handoff is this very `text` with no
    pr-review outcome since: a repeated call would only duplicate it."""
    comments = gh.issue_view(issue).get("comments", [])
    at = last_transition_to(comments, "pr-review")
    if at is None:
        return False
    outcome = last_pr_review_outcome(comments)
    if outcome is not None and outcome[0] > at:
        return False
    return comments[at].get("body", "").split("\n\n<!-- stage-transition:")[0].strip() == text


PR_REVIEW_OUTCOMES = ("clean", "rework")


def review_escalation(rework_before: int, same_class_before: int, outcome: str,
                      same_class: bool) -> Optional[dict]:
    """The escalation report for a review pairing once this round is counted, or None:
    `recommend` `replace` (context-reset replacement) at the first same-class round or
    `escalation.replaceAt` consecutive bounces, `needs-human` at a second same-class round or
    `escalation.needsHumanAt` bounces (references/rework.md). A report only: the orchestrator
    decides, and goes to `needs-human` when the pairing's replacement is already spent."""
    if outcome != "rework":
        return None
    rounds = rework_before + 1
    same = same_class_before + (1 if same_class else 0)
    if same >= 2 or rounds >= ESCALATION["needsHumanAt"]:
        recommend = "needs-human"
    elif same >= 1 or rounds >= ESCALATION["replaceAt"]:
        recommend = "replace"
    else:
        return None
    return {"recommend": recommend, "same_class_rounds": same,
            "rework_since_last_clean": rounds,
            "thresholds": {"replace_at": ESCALATION["replaceAt"],
                           "needs_human_at": ESCALATION["needsHumanAt"]}}


def _pairing_counts_or_empty(gh: GitHub, issue: int) -> dict:
    """`pairing-counts` before a verdict is recorded; `{}` when the thread cannot be read
    (the record still posts, only without an escalation report)."""
    try:
        return cmd_pairing_counts(gh, issue)
    except GhError:
        return {}


def cmd_record_pr_review(gh: GitHub, issue: int, pr: int, outcome: str, summary: str,
                         same_class_recurrence: bool = False) -> dict:
    """Post the `pr-review-outcome` marker (`clean` | `rework`, optional same-class flag)
    that `list-ready-for-review` and `pairing-counts` read. Returns `recorded`."""
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    if same_class_recurrence and outcome != "rework":
        raise GhError("same_class_recurrence only makes sense on outcome='rework' "
                       "-- a clean verdict has no defect class to recur")
    timestamp = _utc_now_marker()
    headline = ("🔍 PR review complete — no findings; proceeding to merge."
                if outcome == "clean" else
                "🔁 PR review complete — findings sent back to `development` for rework.")
    if same_class_recurrence:
        headline += " **Same defect class as an earlier round — escalation candidate.**"
    same_class_field = " same-class:true" if same_class_recurrence else ""
    before = _pairing_counts_or_empty(gh, issue) if outcome == "rework" else {}
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- pr-review-outcome: {outcome}:{pr}"
                             f"{same_class_field} @ {timestamp} -->")
    escalation = review_escalation(before.get("pr_review_rework_since_last_clean", 0),
                                   before.get("pr_review_same_class_recurrence_count", 0),
                                   outcome, same_class_recurrence)
    return {"issue": issue, "pr": pr, "outcome": outcome,
            "same_class_recurrence": same_class_recurrence, "recorded": True,
            **({"escalation": escalation} if escalation else {})}


# Tail of a suite run embedded in an attestation; bounded by GitHub's comment body limit.
LOCAL_CI_EVIDENCE_LINES = 40


LOCAL_CI_EVIDENCE_CHARS = 4000


def read_ci_evidence(output_path: str) -> str:
    """The trimmed tail of a suite run's captured output file for `record-local-ci`.
    Raises on an unreadable or empty file: the attestation must carry real output."""
    try:
        with io.open(os.path.expanduser(output_path), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise GhError(f"--output must be a readable file holding the suite run's own "
                      f"captured output: {exc}")
    text = text.strip()
    if not text:
        raise GhError("--output file is empty -- an attestation must carry the run's own "
                      "output, not a summary of it")
    lines = text.splitlines()
    if len(lines) > LOCAL_CI_EVIDENCE_LINES:
        lines = ["... (earlier output trimmed) ..."] + lines[-LOCAL_CI_EVIDENCE_LINES:]
    trimmed = "\n".join(lines)
    if len(trimmed) > LOCAL_CI_EVIDENCE_CHARS:
        trimmed = "... (trimmed) ...\n" + trimmed[-LOCAL_CI_EVIDENCE_CHARS:]
    return trimmed


def cmd_record_local_ci(gh: GitHub, pr: int, suite: str, sha: str,
                         command: str, output: str) -> dict:
    """Post a `local-ci` attestation on the PR: `suite` passed at `sha`, with the command
    and its captured output tail. The merge gate honours it only while `sha` matches
    the PR head. Returns `attested`, `evidence_lines`."""
    if suite not in LOCAL_CI_SUITES:
        raise GhError(f"suite must be one of {LOCAL_CI_SUITES}, got {suite!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        raise GhError(f"sha must be a 7-40 char hex commit id, got {sha!r}")
    if not (command or "").strip():
        raise GhError("--command is required: the exact command the suite was run with")
    placeholder = _UNEXPANDED_PLACEHOLDER.search(command)
    if placeholder:
        raise GhError(f"--command contains an unexpanded placeholder {placeholder.group(0)!r} -- "
                      f"attest the real command you ran, not a template copied from the docs "
                      f"(substitute every `<...>` token first)")
    pattern = LOCAL_CI_COMMAND_PATTERNS.get(suite)
    if pattern and not re.search(pattern, command):
        raise GhError(f"--command {command.strip()!r} does not match the {suite!r} suite's "
                      f"requiredWorkflows[].commandPattern {pattern!r} -- attest the real "
                      f"suite command")
    accepted = (_non_attestable_suite_uncovered(gh, pr, suite)
                if suite_not_attestable(suite) else {})
    evidence = read_ci_evidence(output)
    timestamp = _utc_now_marker()
    fence = "```"
    gh.pr_comment(pr, f"🧪 Local CI attested — `{suite}` suite passed locally against "
                       f"`{sha}` (stands in for its GHA check on the merge gate).\n\n"
                       f"Command: `{command.strip()}`\n\n"
                       f"<details><summary>captured output (tail)</summary>\n\n"
                       f"{fence}\n{evidence}\n{fence}\n\n</details>\n\n"
                       f"<!-- local-ci: {suite}:{pr} @ {sha} -->\n"
                       f"<!-- attested-at: {timestamp} -->"
                       + (f"\n{LOCAL_CI_CLOUD_MARKER}" if remote_session() else ""))
    return {"pr": pr, "suite": suite, "sha": sha, "command": command.strip(),
            "evidence_lines": len(evidence.splitlines()), "attested": True, **accepted}


def _non_attestable_suite_uncovered(gh: GitHub, pr: int, suite: str) -> dict:
    """An `attestable: false` suite is attested only as a record, when none of its required
    workflows applies to the PR's base and covers a changed path (a PR into `epic-*` while the
    workflow is scoped to `main`): nothing else records that the suite ran. Raises when one
    does -- only its passing GitHub Actions check satisfies it. Returns `accepted_because`."""
    base = gh.pr_view(pr, fields="baseRefName").get("baseRefName")
    files = gh.pr_files(pr)
    covering = [w["workflow"] for w in required_workflows(base)
                if w["suite"] == suite and _workflow_applies_to_base(w, base)
                and any(_workflow_covers(w, p) for p in files)]
    if covering:
        raise GhError(f"the {suite!r} suite is configured `attestable: false` (or `attestable: "
                      f"\"cloud\"` outside a cloud session) and its required "
                      f"workflow ({', '.join(covering)}) covers this PR into {base!r} -- a "
                      f"local-ci attestation cannot stand in for it; only a passing GitHub "
                      f"Actions check satisfies it")
    return {"accepted_because": f"`attestable: false` suite recorded, not standing in for a "
                                f"check: no required workflow for {suite!r} covers this PR's "
                                f"{len(files)} changed path(s) on base {base!r}",
            "stands_in_for_check": False}


def _stage_and_status(gh: WorkItemProvider, issue: int) -> tuple:
    """`(Stage slug, Pipeline Status slug)` of one issue, read from `issue_fields`:
    `issue_view` carries no field values."""
    fields = gh.issue_fields(issue)
    return (STAGE_FIELD_NAMES.get(fields.get("Stage")),
            PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status")))


# Stages that post a free-text comment on their unit; `post-comment` is their only path (a
# stage agent's `gh issue/pr comment` is denied, and a cloud session has no other write path).
# Authoring stages post their handoff; `development` a rework note; review roles their full
# review (the verdict marker stays `record-design-review` / `record-pr-review`).
POST_COMMENT_REVIEW_ROLES = ("product-review", "arch-review", "lld-review", "pr-review")


POST_COMMENT_ROLES = ("product", "architecture", "lld", "development", *POST_COMMENT_REVIEW_ROLES)


# role -> the Stage values its unit sits at while the role holds it: a review role has no Stage
# of its own but the one it follows; development's rework runs at Stage `pr-review`.
POST_COMMENT_STAGES = {"development": ("development", "pr-review"),
                       "product-review": ("product",), "arch-review": ("architecture",),
                       "lld-review": ("lld",), "pr-review": ("pr-review",)}


_PAUSED_FOR_REGATE_PREFIX = "⏸️ Paused — `"  # cmd_pause_for_epic_regate's comment


def post_comment_cap(role: str) -> int:
    """A review is an evidence-carrying comment (EVIDENCE_CAP); every other role's is a
    stage handoff (HANDOFF_CAP) -- references/stage-playbooks.md, "Comment size is a contract"."""
    return EVIDENCE_CAP if role in POST_COMMENT_REVIEW_ROLES else HANDOFF_CAP


def parked_for_epic_regate(gh: WorkItemProvider, issue: int) -> bool:
    """Whether `issue` is parked by `pause-for-epic-regate`: its latest pause comment is newer
    than its latest stage-start comment (a resumed unit is re-claimed, which posts one)."""
    paused = started = -1
    for i, c in enumerate(gh.issue_view(issue).get("comments", [])):
        body = c.get("body") or ""
        if body.startswith(_PAUSED_FOR_REGATE_PREFIX):
            paused = i
        elif "Picking this up" in body:
            started = i
    return paused > started


def cmd_post_comment(gh: WorkItemProvider, issue: int, role: str, body_file: str) -> dict:
    """Post a stage's own comment on its unit, tagged with a `role-comment` marker that
    `subagent_stop` looks for. Refused unless `issue` is at the role's Stage and claimed
    (in-progress, its gate open while the stage revises on feedback, or parked `todo` by
    `pause-for-epic-regate`). Over the role's cap (2,000 chars; 6,000 for a review) nothing
    is posted: `refused` with the length and the cap."""
    if role not in POST_COMMENT_ROLES:
        raise GhError(f"role must be one of {POST_COMMENT_ROLES}, got {role!r}")
    try:
        with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
            body = fh.read().strip()
    except OSError as exc:
        raise GhError(f"--body-file must be a readable file holding the comment text: {exc}")
    if not body:
        raise GhError("--body-file is empty -- a handoff comment must say something")
    cap = post_comment_cap(role)
    if len(body) > cap:
        kind = "review (evidence-carrying)" if role in POST_COMMENT_REVIEW_ROLES else "handoff"
        return {"refused": True, "posted": False, "chars": len(body), "cap": cap,
                "reason": f"NOTHING WAS POSTED: the comment is {len(body):,} chars, over the "
                          f"{cap:,}-char cap for a `{role}` {kind} comment "
                          f"(references/stage-playbooks.md, \"Comment size is a contract\"). "
                          f"Cut it to <= {cap:,} chars (check with `wc -c`) and re-run; the "
                          f"control plane never truncates or splits a comment"}
    stage, status = _stage_and_status(gh, issue)
    stages = POST_COMMENT_STAGES.get(role, (role,))
    claimed = status in ("in-progress", *GATE_PENDING_STATUSES) or (
        status == "todo" and stage in stages and parked_for_epic_regate(gh, issue))
    if stage not in stages or not claimed:
        raise GhError(f"#{issue} is at Stage {stage!r} / status {status!r}, not claimed at "
                      f"{role!r} -- post-comment only serves the stage that holds the claim")
    tag = f"<!-- role-comment: {role} @ "
    last = next((c for c in reversed(gh.issue_view(issue).get("comments") or [])
                 if tag in (c.get("body") or "")), None)
    if last and last["body"].split(tag, 1)[0].strip() == body:
        return {"refused": True, "posted": False, "duplicate_of": last.get("url"),
                "reason": f"NOTHING WAS POSTED: the body is identical to this issue's last "
                          f"`{role}` comment -- a stale body file from an earlier round. Write "
                          f"this round's comment to a fresh file and re-run"}
    gh.issue_comment(issue, f"{body}\n\n{tag}{_utc_now_marker()} -->")
    return {"issue": issue, "role": role, "posted": True}


DESIGN_REVIEW_ROLES = ("product-review", "arch-review", "lld-review")


def cmd_record_design_review(gh: GitHub, issue: int, role: str, outcome: str,
                             summary: str, same_class_recurrence: bool = False) -> dict:
    """Post the `design-review-outcome` marker for a design review role (`clean` |
    `rework`, optional same-class flag) that `pairing-counts` reads, and -- for an
    arch/lld-review of a phase-Task -- the same outcome as a comment on its design PR.
    Returns `recorded` (+ `design_pr`)."""
    if role not in DESIGN_REVIEW_ROLES:
        raise GhError(f"role must be one of {DESIGN_REVIEW_ROLES}, got {role!r}")
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    if same_class_recurrence and outcome != "rework":
        raise GhError("same_class_recurrence only makes sense on outcome='rework' "
                       "-- a clean verdict has no defect class to recur")
    timestamp = _utc_now_marker()
    headline = (f"🔍 `{role}` complete — no findings."
                if outcome == "clean" else
                f"🔁 `{role}` complete — findings sent back for rework.")
    if same_class_recurrence:
        headline += " **Same defect class as an earlier round — escalation candidate.**"
    same_class_field = " same-class:true" if same_class_recurrence else ""
    # The review lives on the design PR too; posted first so a failure here leaves no evidence
    # marker behind and a re-run is safe. The marker names the PR head it covered.
    design_pr, sha_field = None, ""
    if role in DESIGN_STAGE_REVIEW.values():
        found = find_design_pr(gh.issue_view(issue).get("comments", []))
        if found and DESIGN_STAGE_REVIEW[found[0]] == role:
            design_pr = found[1]
            gh.pr_comment(design_pr, f"{headline} {summary}")
            head = gh.pr_view(design_pr, fields="headRefOid").get("headRefOid")
            sha_field = f" sha:{head}" if head else ""
    before = (((_pairing_counts_or_empty(gh, issue).get("design_review") or {}).get(role) or {})
              if outcome == "rework" else {})
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- design-review-outcome: {outcome}:{role}"
                             f"{same_class_field}{sha_field} @ {timestamp} -->")
    escalation = review_escalation(before.get("rework_since_last_clean", 0),
                                   before.get("same_class_recurrence_count", 0),
                                   outcome, same_class_recurrence)
    return {"issue": issue, "unit": "issue", "role": role,
            "outcome": outcome, "same_class_recurrence": same_class_recurrence,
            "recorded": True, **({"design_pr": design_pr} if design_pr else {}),
            **({"escalation": escalation} if escalation else {}),
            **({"reviewed_sha": sha_field.split(":")[1]} if sha_field else {})}


def _post_start_comment(gh: GitHub, issue: int, role: str):
    gh.issue_comment(issue, f"🚧 Picking this up — {role} stage starting.")


def _check_claimable(gh: GitHub, issue: int, role: str, refuse: bool = False) -> dict:
    """Raise unless `role` is claimable on `issue`: a known role and, for `development`,
    no open PR (re-claiming would move Stage off PR Review and drop it from review).
    With `refuse`, the open-PR case returns `refused` (`reason`, `pr`) instead of raising."""
    if role not in STAGE_OPTION_IDS:
        raise GhError(f"unknown role {role!r} -- must be one of "
                       f"{sorted(STAGE_OPTION_IDS)}")
    if role == "development":
        open_prs = gh.pr_list_for_branch(issue_branch(issue))
        if open_prs:
            pr = open_prs[0]["number"]
            reason = (f"issue #{issue} already has open PR #{pr} -- resume its development "
                      f"agent, or after a session death run `prepare-rework {issue}` and "
                      f"dispatch a replacement (references/rework.md, \"Replacing an agent "
                      f"that cannot be resumed\"); never re-claim")
            if refuse:
                return {"issue": issue, "role": role, "claimable": False, "refused": True,
                        "reason": reason, "pr": pr}
            raise GhError(reason)
    return {"issue": issue, "role": role, "claimable": True}


def cmd_claim(gh: GitHub, issue: int, role: str) -> dict:
    role = RETIRED_ROLES.get(role, role)
    _check_claimable(gh, issue, role)
    gh.set_stage_field(issue, role)
    gh.set_pipeline_status_field(issue, "in-progress")
    _post_start_comment(gh, issue, role)
    return {"issue": issue, "claimed": True}


def cmd_set_stage(gh: GitHub, issue: int, stage: str) -> dict:
    """Set the Stage field only -- no Pipeline Status change, no start comment."""
    if stage not in STAGE_OPTION_IDS:
        raise GhError(f"unknown stage {stage!r} -- must be one of {sorted(STAGE_OPTION_IDS)}")
    gh.set_stage_field(issue, stage)
    return {"issue": issue, "stage": stage}


# A standing child's full flow in order; `merge` means "skip pr-review". Only an authoring
# stage (or `merge`) is a route target: a review always follows the stage it reviews.
STANDING_FLOW = ("product", "product-review", "architecture", "arch-review",
                 "development", "pr-review", "merge")


ROUTE_TARGETS = ("product", "architecture", "development", "merge")


ROUTE_REASON_CAP = 200


def _flow_index(stage: Optional[str]) -> Optional[int]:
    """Where a child at Stage `stage` sits in STANDING_FLOW: -1 before its first stage, and
    `development` for Stage `pr-review` (development's exit sets it); None outside the flow."""
    if stage is None:
        return -1
    owner = "development" if stage == "pr-review" else stage
    return STANDING_FLOW.index(owner) if owner in STANDING_FLOW else None


def route_refusal(parent: Optional[dict], stage: Optional[str], status: Optional[str],
                  to: str, reason: str, pr_review_bounced: bool = False) -> Optional[str]:
    """Why `route` must refuse moving a child of `parent` at `stage`/`status` to `to`, or None.
    `pr_review_bounced`: a `pr-review` already asked for rework on this issue."""
    if parent is None or not is_epic(parent) or not is_epic_standing(parent):
        return "route is for a standing epic's child only -- stage anything else with set-stage"
    if to not in ROUTE_TARGETS:
        return (f"--to must be one of {', '.join(ROUTE_TARGETS)}"
                + (" -- a review always follows its stage; route to the next authoring stage "
                   "to skip it" if to in STANDING_FLOW else ""))
    line = reason.strip()
    if not line or "\n" in line:
        return "--reason must be one non-empty line"
    if status in GATE_PENDING_STATUSES:
        return f"a gate PR is open (Pipeline Status {status!r}) -- the human decides; route after it passes"
    at = _flow_index(stage)
    if at is None:
        return f"Stage {stage!r} is not in the standing flow ({', '.join(STANDING_FLOW[:-1])})"
    if to == "merge" and stage != "pr-review":
        return "merge (skipping pr-review) needs development's PR open -- Stage must be PR Review"
    if to == "merge" and pr_review_bounced:
        return "pr-review already asked for rework on this issue -- never skip it after a bounce"
    if STANDING_FLOW.index(to) <= at:
        return f"route only moves forward from {stage!r} -- use set-stage to go back"
    return None


def cmd_route(gh: GitHub, issue: int, to: str, reason: str,
              finished: Optional[str] = None) -> dict:
    """Route a standing epic's child ahead to `to`, skipping the stages between: Stage `to`
    and Pipeline Status `todo` (no claim; `merge` leaves both), plus a `stage-route` marker.
    `finished`: the stage that just ran (a review leaves Stage on its authoring stage), so it
    is never named as skipped. Refuses (`routed: False`, exit 0) per `route_refusal`."""
    parent = gh.issue_epic_info(issue).get("parent")
    parent_info = gh.issue_epic_info(parent["number"]) if parent else None
    fields = gh.issue_fields(issue)
    stage = STAGE_FIELD_NAMES.get(fields.get("Stage"))
    bounced = to == "merge" and any(
        m.group(1) == "rework" for c in gh.issue_view(issue).get("comments", [])
        for m in _PR_REVIEW_OUTCOME_MARKER.finditer(c.get("body", "")))
    refusal = route_refusal(parent_info, stage,
                            PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status")),
                            to, reason, pr_review_bounced=bounced)
    if refusal:
        return {"issue": issue, "routed": False, "reason": refusal}
    at, frm = _flow_index(stage), stage or "pickup"
    if finished in STANDING_FLOW and STANDING_FLOW.index(finished) > at:
        at, frm = STANDING_FLOW.index(finished), finished
    skipped = list(STANDING_FLOW[at + 1:STANDING_FLOW.index(to)])
    if to != "merge":
        gh.set_stage_field(issue, to)
        gh.set_pipeline_status_field(issue, "todo")
    skipping = f", skipping {', '.join(f'`{s}`' for s in skipped)}" if skipped else ""
    gh.issue_comment(issue, f"🔀 Routed `{frm}` → `{to}`{skipping}: {reason.strip()}\n\n"
                            f"<!-- stage-route: {frm}->{to} @ {_utc_now_marker()} -->")
    return {"issue": issue, "routed": True, "from": frm, "to": to, "skipped": skipped}


def _reclaim_after_regate(gh: GitHub, issue: int) -> dict:
    """A unit parked `todo` by `pause-for-epic-regate` and resumed by message (never re-claimed)
    is claimed again (`in-progress`) as its review starts: the start comment ends the parked
    state, and the reviewer's `post-comment` needs the claim. `{"reclaimed": True}` or `{}`."""
    if not parked_for_epic_regate(gh, issue):
        return {}
    gh.set_pipeline_status_field(issue, "in-progress")
    return {"reclaimed": True}


def cmd_start_comment(gh: GitHub, issue: int, role: str) -> dict:
    """Post the start comment with no field mutation, for roles with no Stage of their own.

    For `pr-review` -- which `transition` runs AFTER `sync-branch` has moved the head -- it first
    refuses (`ok: False`, no comment posted) when a required suite has neither a `local-ci`
    attestation nor a passing GHA check on the current PR head, so the review is never queued
    against evidence sync just invalidated; a suite whose sync only merged files outside its
    coverage is carried forward instead.

    For `arch-review` it surfaces the effective `skip_confidence_threshold` -- the profile's
    `skipConfidenceThreshold` the reviewer's confidence must exceed for `skip-gate` to skip Gate B
    -- so the reviewer knows the bar before writing its confidence marker."""
    if role == "pr-review":
        prs = gh.pr_list_for_branch(issue_branch(issue))
        carried: list = []
        by_check: list = []
        if prs:
            status = pr_stale_attestations(gh, issue, prs[0]["number"])
            if status["stale"]:
                stale = status["stale"]
                return {"issue": issue, "role": role, "started": False, "ok": False,
                        "refused": True,
                        "stale_suites": [s["suite"] for s in stale],
                        "held": stale,
                        "reason": (f"pr-review not queued -- "
                                   f"{'; '.join(s['reason'] for s in stale)}. A required suite "
                                   f"is satisfied by a `record-local-ci` attestation on the PR "
                                   f"head OR a passing check for its workflow on that head (a "
                                   f"sync that only merged files outside its coverage carries "
                                   f"the attestation forward). Wait for a pending check and "
                                   f"re-run this, or re-run the suite and `record-local-ci` on "
                                   f"the post-sync head.")}
            carried, by_check = status["carried"], status["passed_by_check"]
        reclaimed = _reclaim_after_regate(gh, issue)
        _post_start_comment(gh, issue, role)
        result = {"issue": issue, "started": role, **reclaimed}
        if carried:
            result["carried_attestation_forward"] = carried
        if by_check:
            result["satisfied_by_check"] = by_check
        return result
    reclaimed = _reclaim_after_regate(gh, issue)
    _post_start_comment(gh, issue, role)
    result = {"issue": issue, "started": role, **reclaimed}
    if role == "arch-review":
        threshold = _profile_for_issue(gh, issue)["gates"]["skipConfidenceThreshold"]
        result["skip_confidence_threshold"] = threshold
        _record_hints(SKIP_THRESHOLD_HINTS_FILE, {issue: threshold})  # the reviewer's hook line
    return result


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import design_gates  # noqa: E402,F401
from .design_gates import DESIGN_STAGE_REVIEW, find_design_pr  # noqa: E402
from .gates import _profile_for_issue  # noqa: E402
from .checks import (  # noqa: E402
    _is_failed_check, _workflow_applies_to_base, _workflow_covers, suite_not_attestable,
    effective_checks, enrich_failed_checks, pr_stale_attestations, required_workflows,
)
from .issue_admin import cmd_pairing_counts  # noqa: E402
from .composites import EVIDENCE_CAP, HANDOFF_CAP  # noqa: E402
