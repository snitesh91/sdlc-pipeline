"""Argument parsing and command dispatch."""
from __future__ import annotations

import argparse
import json
import os
from typing import Optional

from .config import (
    DESIGN_LANE_PARALLELISM, DEV_LANE_PARALLELISM, PR_REVIEW_PARALLELISM, RETIRED_ROLES,
    STAGE_OPTION_IDS, TOKEN_ENV, TOKEN_PATH,
)
from .github import (
    GhError, GitHubRest, _CLOSED_WALK, _FOCUS, _LISTING, get_work_item_provider, kind_filter,
)
from .units import PLACEMENTS, PlacementMismatch, cmd_doc_root, cmd_initiative_profile
from .evidence import LOCAL_CI_SUITES, REVIEW_ROLES
from .epic_close import (
    INITIATIVE_VERIFICATION_OUTCOMES, cmd_check_initiative_closeable, cmd_close_epic,
    brief_close_epic, cmd_close_initiative, cmd_merge_initiative_pr, cmd_open_initiative_pr,
    cmd_record_epic_verification, cmd_record_initiative_verification,
)
from .gate_eval import cmd_check_gate
from .next_action import cmd_list_ready_for_review, cmd_next_action
from .footprint import cmd_lld_section
from .plugin_version import cmd_show_config
from .worktrees import cmd_release_review_worktree, cmd_review_worktree_add, cmd_worktree_add
from .lanes import cmd_list_design_ready, cmd_list_parallel_ready
from .handoff import (
    DESIGN_REVIEW_ROLES, POST_COMMENT_ROLES, PR_REVIEW_OUTCOMES, ROUTE_REASON_CAP,
    ROUTE_TARGETS, STANDING_FLOW, cmd_claim, cmd_handoff_to_pr_review, cmd_post_comment,
    cmd_record_design_review, cmd_record_local_ci, cmd_record_pr_review, cmd_route,
    cmd_set_stage, cmd_start_comment,
)
from .design_gates import (
    cmd_merge_design_pr, cmd_merge_lld_doc, cmd_open_design_pr, cmd_sync_branch,
)
from .lld_tasks import cmd_add_blocked_by, cmd_create_lld_tasks, cmd_remove_blocked_by
from .gates import (
    HUMAN_GATE_TOGGLES, cmd_auto_pass_gate, cmd_mark_feedback_addressed,
    cmd_mark_feedback_received, cmd_merge_gate, cmd_pass_gate, cmd_pause_for_epic_regate,
    cmd_resolve_thread, cmd_skip_gate, cmd_waive_gate,
)
from .dev_pr import cmd_cite, cmd_open_dev_pr, cmd_verify_citations, cmd_verify_exit
from .checks import cmd_ci_failure_summary, cmd_merge_pr, cmd_pr_checks, cmd_rerun_checks, cmd_update_pr_body
from .issue_ops import (
    cmd_close_issue, cmd_mark_blocked, cmd_mark_issue_closed, cmd_mark_needs_human,
    cmd_mark_todo, cmd_place, cmd_prune_stale,
)
from .park import cmd_comment, cmd_edit_issue, cmd_park_finding, cmd_supersede_park_issue
from .human_channel import (
    _read_text_arg, cmd_approve_gate, cmd_record_operator_answer, cmd_request_gate_changes,
)
from .cloud import (
    CLOUD_SESSION_OUTCOMES, HEARTBEAT_STATES, cmd_cloud_heartbeat, cmd_cloud_status,
    cmd_detach_epic, cmd_end_cloud_session, cmd_launch_cloud_epic, cmd_nudge_cloud_epic,
    cmd_record_cloud_session,
)
from .issue_admin import (
    cmd_audit_issues, cmd_check_epics_closeable, cmd_create_issue, cmd_list_needs_human,
    cmd_pairing_counts, cmd_repair_issue, cmd_reparent_issue, cmd_show_issue_fields,
)
from .stack import cmd_provision_epic_stack, cmd_teardown_epic_stack
from .composites import (
    ADVANCE_FROM, ADVANCE_TO, EVIDENCE_CAP, HANDOFF_CAP, _open_gate_cli, cmd_advance_standing,
    cmd_cut_phase_tasks, cmd_file_closing_delta, cmd_finish_gate_feedback, cmd_finish_lld,
    cmd_open_arch_revision, cmd_prepare_rework, cmd_skip_pr_review, cmd_start_stage,
    cmd_transition, comment_cap_refusal,
)


# Argument names that carry issue numbers (never a PR, line, limit or confidence).
_FOCUS_ARGS = ("issue", "epic", "number", "initiative", "parent", "lld_task", "waits_on",
               "skip_epic", "dep", "by", "blocks", "blocked_by")


# Commands naming no issue that read less than every open issue and all their closed
# sub-issues (`_LISTING`, `_CLOSED_WALK`). `list-needs-human` and an unscoped `audit-issues`
# list every open issue: Pipeline Status and Priority/Effort are issue fields, which REST
# list filters cannot match.
_CLOSED_WALK_BY_COMMAND = {"list-needs-human": "none", "check-epics-closeable": "childless"}
_LISTING_BY_COMMAND = {"check-epics-closeable": lambda: [kind_filter("epic")]}


def _focus_numbers(args) -> set:
    """The issue numbers a command names, for `GitHubRest.issue_list`'s closed-issue reads."""
    out: set = set()
    for name in _FOCUS_ARGS:
        value = getattr(args, name, None)
        for v in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(v, int) and not isinstance(v, bool):
                out.add(v)
    return out


def _pr_add_label(gh: GitHubRest, pr: int, label: str) -> dict:
    gh.ensure_label(label)
    gh.pr_add_label(pr, label)
    return {"pr": pr, "label": label, "added": True}


def main(argv: Optional[list] = None) -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        sources = " or ".join(([f"tokenEnv (${TOKEN_ENV})"] if TOKEN_ENV else [])
                              + ([f"tokenPath ({TOKEN_PATH})"] if TOKEN_PATH else [])) or "no source"
        prefix = (f"GITHUB_TOKEN=\"${TOKEN_ENV}\"" if TOKEN_ENV
                  else f"GITHUB_TOKEN=$(cat {TOKEN_PATH})" if TOKEN_PATH else "GITHUB_TOKEN=<classic PAT>")
        print(json.dumps({"error": "GITHUB_TOKEN not set — the sdlc plugin's SessionStart hook "
                                    f"exports it from {sources}; start a new session in the driven "
                                    f"repo, or prefix the call with {prefix}"}))
        return 1
    parser = argparse.ArgumentParser(prog="sdlc_next.py")
    repo_path_help = "Any path inside the repository; never used as the git-write target"
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("next-action")
    p.add_argument("epic", type=int, help="The epic to drive")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the dev lane's slot holders")
    p.add_argument("--run-id", default=None,
                    help="This run's id; enables the maxTasksPerRun cap, shared across an "
                         "Initiative's Epics (omit = no cap)")
    p.add_argument("--skip-epic", type=int, action="append", default=[],
                    help="Initiative only: park this Epic for this run (repeatable), e.g. one "
                         "waiting on a human, so the loop moves to the next runnable Epic")
    p.add_argument("--sync-epic", action="store_true",
                    help="Non-standing Epic with --run-id: sync epic-<n> with main first when "
                         "due (run start, every 3 merges, main moved); result in `epic_sync`")
    p.set_defaults(func=lambda a: cmd_next_action(get_work_item_provider(), a))
    p = sub.add_parser("list-ready-for-review",
                        help="Children whose handed-off draft PR awaits review this round")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Max PRs to return (default {PR_REVIEW_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_ready_for_review(get_work_item_provider(), a.epic, a.limit))
    p = sub.add_parser("list-parallel-ready",
                        help="Dev-lane children safe to start/resume concurrently")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent children (default {DEV_LANE_PARALLELISM})")
    p.add_argument("--run-id", default=None,
                    help="This run's id; enables the maxTasksPerRun cap (omit = no cap)")
    p.set_defaults(func=lambda a: cmd_list_parallel_ready(
        get_work_item_provider(), a.repo_path, a.epic, a.limit, run_id=a.run_id))
    p = sub.add_parser("lld-section",
                        help="Print one Task's subsection of its Epic's lld.md")
    p.add_argument("--epic", type=int, required=True, help="The Epic")
    p.add_argument("--task", required=True,
                    help="Task issue number, or its design-time key before renumbering")
    p.add_argument("--repo-path", default=".",
                    help="Repo with an origin/epic-<n> ref")
    p.set_defaults(func=lambda a: cmd_lld_section(a.repo_path, a.epic, a.task))
    p = sub.add_parser("doc-root",
                        help="Where one unit's docs live: its product's docRoot/requirementsDir "
                             "(per `docRoots`), else the top-level ones")
    p.add_argument("issue", type=int, help="Any unit: Initiative, Epic, Task")
    p.set_defaults(func=lambda a: cmd_doc_root(None, a.issue))
    p = sub.add_parser("list-design-ready",
                        help="A standing epic's product/architecture children safe to run concurrently")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent design children (default {DESIGN_LANE_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_design_ready(get_work_item_provider(), a.repo_path, a.epic, a.limit))
    p = sub.add_parser("handoff-to-pr-review",
                        help="Post the development->pr-review handoff marker")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--summary", required=True,
                    help="The handoff comment (agents/development.md, <= 6,000 chars); must "
                         "hold its `### Acceptance criteria coverage` section")
    p.set_defaults(func=lambda a: cmd_handoff_to_pr_review(get_work_item_provider(), a.issue, a.pr, a.summary))
    p = sub.add_parser("record-pr-review",
                        help="Record a pr-review outcome on the issue")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.add_argument("--same-class-recurrence", action="store_true",
                    help="Finding repeats an earlier round's defect class (escalation signal)")
    p.set_defaults(func=lambda a: cmd_record_pr_review(
        get_work_item_provider(), a.issue, a.pr, a.outcome, a.summary, a.same_class_recurrence))
    p = sub.add_parser("record-local-ci",
                        help="Attest a main-only suite passed locally at a commit")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--suite", required=True, choices=list(LOCAL_CI_SUITES))
    p.add_argument("--sha", required=True,
                    help="Commit tested; honoured only while it is the PR head")
    p.add_argument("--command", required=True,
                    help="The exact command run")
    p.add_argument("--output", required=True,
                    help="File with the run's captured output (tail is embedded)")
    p.set_defaults(func=lambda a: cmd_record_local_ci(get_work_item_provider(), a.pr, a.suite, a.sha,
                                                       a.command, a.output))
    p = sub.add_parser("pr-add-label",
                        help="Add a label to a PR (e.g. ci:local on a cloud epic PR)")
    p.add_argument("pr", type=int)
    p.add_argument("label")
    p.set_defaults(func=lambda a: _pr_add_label(get_work_item_provider(), a.pr, a.label))
    p = sub.add_parser("record-design-review",
                        help="Record an arch-review/lld-review outcome on the unit")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True, choices=list(DESIGN_REVIEW_ROLES))
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.add_argument("--same-class-recurrence", action="store_true",
                    help="Finding repeats an earlier round's defect class (escalation signal)")
    p.set_defaults(func=lambda a: cmd_record_design_review(
        get_work_item_provider(), a.issue, a.role, a.outcome, a.summary, a.same_class_recurrence))
    p = sub.add_parser("post-comment",
                        help="Post a stage's own comment (handoff, rework note or review) "
                             "on its unit from a file")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True, choices=list(POST_COMMENT_ROLES))
    p.add_argument("--body-file", required=True,
                    help=f"File holding the comment text (<= {HANDOFF_CAP:,} chars; "
                         f"<= {EVIDENCE_CAP:,} for a review role)")
    p.set_defaults(func=lambda a: cmd_post_comment(
        get_work_item_provider(), a.issue, a.role, a.body_file))
    p = sub.add_parser("check-gate")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_check_gate(get_work_item_provider(), a))
    p = sub.add_parser("claim")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True)
    p.set_defaults(func=lambda a: cmd_claim(get_work_item_provider(), a.issue, a.role))
    p = sub.add_parser("set-stage",
                        help="Set the Stage field only (no status write, no comment)")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True)
    p.set_defaults(func=lambda a: cmd_set_stage(get_work_item_provider(), a.issue, a.stage))
    p = sub.add_parser("route", help="Route a standing epic's child ahead to a later stage")
    p.add_argument("issue", type=int)
    p.add_argument("--to", required=True, help=f"One of {', '.join(ROUTE_TARGETS)}")
    p.add_argument("--reason", required=True, help=f"One line, <= {ROUTE_REASON_CAP} chars")
    p.add_argument("--from", dest="frm", default=None, choices=list(STANDING_FLOW[:-1]),
                   help="The stage that just ran (never reported as skipped)")
    p.set_defaults(func=lambda a: cmd_route(get_work_item_provider(), a.issue, a.to, a.reason,
                                            finished=a.frm))
    p = sub.add_parser("start-comment")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True,
                   choices=sorted(REVIEW_ROLES))
    p.set_defaults(func=lambda a: cmd_start_comment(get_work_item_provider(), a.issue, a.role))
    p = sub.add_parser("open-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--title", default=None,
                    help="Defaults to the issue's title; the PR title is derived from it")
    p.add_argument("--doc", required=True, choices=["product.md", "architecture.md"])
    p.add_argument("--next-stage", required=True)
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: _open_gate_cli(a))
    p = sub.add_parser("pass-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--gate-pr", type=int, required=True)
    p.add_argument("--stage", required=True, choices=["product", "architecture"])
    p.add_argument("--doc-only", action="store_true",
                    help="The gate doc is the unit's whole deliverable: close it, no development")
    p.set_defaults(func=lambda a: cmd_pass_gate(get_work_item_provider(), a.repo_path, a.issue,
                                                a.gate_pr, a.stage, doc_only=a.doc_only))
    p = sub.add_parser("skip-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True, choices=["architecture"])
    p.add_argument("--confidence", type=int, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (an Epic's Architecture-phase Task only)")
    p.set_defaults(func=lambda a: cmd_skip_gate(get_work_item_provider(), a.issue, a.stage, a.confidence, a.summary, repo_path=a.repo_path))
    p = sub.add_parser("waive-gate",
                        help="Pass a gate without a human when the profile waives it")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True, choices=sorted(HUMAN_GATE_TOGGLES))
    p.add_argument("--summary", required=True)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (phase-Tasks only)")
    p.set_defaults(func=lambda a: cmd_waive_gate(get_work_item_provider(), a.issue, a.stage,
                                                 a.summary, repo_path=a.repo_path))
    p = sub.add_parser("merge-gate",
                        help="Merge an open gate PR at the operator's explicit instruction")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--stage", required=True, choices=sorted(HUMAN_GATE_TOGGLES))
    p.add_argument("--operator-confirmed", action="store_true",
                    help="The operator explicitly told you to merge this gate PR")
    p.set_defaults(func=lambda a: cmd_merge_gate(get_work_item_provider(), a.pr, a.issue,
                                                 a.stage, a.operator_confirmed))
    p = sub.add_parser("auto-pass-gate")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_auto_pass_gate(get_work_item_provider(), a.repo_path, a.pr))
    p = sub.add_parser("mark-feedback-received")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--author", required=True, help="Comment/review author login")
    p.add_argument("--body", default="", help="Comment/review body (empty = skip)")
    p.set_defaults(func=lambda a: cmd_mark_feedback_received(get_work_item_provider(), a.pr, a.author, a.body))
    p = sub.add_parser("resolve-thread", help="Reply to and resolve one PR review thread")
    p.add_argument("--thread-id", required=True)
    p.add_argument("--reply", default=None)
    p.set_defaults(func=lambda a: cmd_resolve_thread(get_work_item_provider(), a.thread_id, a.reply))
    p = sub.add_parser("mark-feedback-addressed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_feedback_addressed(get_work_item_provider(), a.issue))
    p = sub.add_parser("mark-todo")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_todo(get_work_item_provider(), a.issue))
    p = sub.add_parser("mark-issue-closed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_issue_closed(get_work_item_provider(), a.issue))
    p = sub.add_parser("close-issue",
                        help="Close the issue, set terminal fields, release its worktree")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map)")
    p.add_argument("--not-planned", action="store_true",
                    help="Close with state reason NOT_PLANNED (a dropped unit), commenting --reason")
    p.add_argument("--reason", default=None,
                    help="Why it is not planned (one paragraph; with --not-planned only)")
    p.set_defaults(func=lambda a: cmd_close_issue(get_work_item_provider(), a.issue,
                                                  repo_path=a.repo_path,
                                                  not_planned=a.not_planned, reason=a.reason))
    p = sub.add_parser("pause-for-epic-regate")
    p.add_argument("issue", type=int)
    p.add_argument("--epic", type=int, required=True)
    p.add_argument("--gate-pr", type=int, default=None,
                    help="The revision's design/gate PR, once it exists")
    p.add_argument("--found-by", default="lld",
                    help="Stage that hit the deviation (lld, development, pr-review, ...)")
    p.set_defaults(func=lambda a: cmd_pause_for_epic_regate(get_work_item_provider(), a.issue, a.epic, a.gate_pr, a.found_by))
    p = sub.add_parser("verify-exit")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--expect-stage", required=True)
    p.add_argument("--pr", type=int, default=None)
    p.set_defaults(func=lambda a: cmd_verify_exit(
        get_work_item_provider(), a.repo_path, a.issue, a.expect_stage, a.pr))
    p = sub.add_parser("cite",
                        help="Emit a citation block read from the real file")
    p.add_argument("path")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--line", type=int)
    target.add_argument("--lines", help="Inclusive range, e.g. 12-18")
    target.add_argument("--match", help="Literal substring matching exactly one line")
    p.add_argument("--rev", default=None, help="Read at this revision")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_cite(a.path, line=a.line, lines=a.lines,
                                            match=a.match, rev=a.rev,
                                            repo_path=a.repo_path))
    p = sub.add_parser("verify-citations",
                        help="Re-resolve every citation in the given documents")
    p.add_argument("doc", nargs="+")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_verify_citations(a.doc, repo_path=a.repo_path))
    p = sub.add_parser("worktree-add",
                        help="Create or resume the unit's worktree")
    p.add_argument("number", type=int)
    p.add_argument("--unit", choices=["issue", "epic", "initiative"], default="issue")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base (e.g. origin/main)")
    p.set_defaults(func=lambda a: cmd_worktree_add(
        get_work_item_provider(), a.number, a.unit, a.repo_path, base=a.base))
    p = sub.add_parser("review-worktree-add",
                        help="Create/refresh issue-<n>'s detached pr-review worktree at origin/issue-<n>")
    p.add_argument("number", type=int)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_review_worktree_add(a.number, a.repo_path))
    p = sub.add_parser("release-review-worktree",
                        help="Remove issue-<n>'s detached pr-review worktree (idempotent)")
    p.add_argument("number", type=int)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_release_review_worktree(a.number, a.repo_path))
    p = sub.add_parser("prune-stale",
                        help="Remove closed units' worktrees and merged branches; prune git state")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.add_argument("--dry-run", action="store_true", help="Report what would be removed")
    p.set_defaults(func=lambda a: cmd_prune_stale(get_work_item_provider(), a.repo_path,
                                                  dry_run=a.dry_run))

    p = sub.add_parser("sync-branch")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--unit", default="issue", choices=["issue", "epic", "initiative"])
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base")
    p.set_defaults(func=lambda a: cmd_sync_branch(get_work_item_provider(), a.repo_path,
                                                   a.issue, a.unit, base=a.base))
    p = sub.add_parser("merge-lld-doc",
                        help="Verify lld.md is merged into the epic branch, stage its Tasks, mark the Epic architected")
    p.add_argument("epic", type=int, help="The Epic")
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.set_defaults(func=lambda a: cmd_merge_lld_doc(get_work_item_provider(), a.repo_path, a.epic))
    p = sub.add_parser("open-design-pr",
                        help="Raise a phase-Task's design PR issue-<n> -> epic-<e> (transition runs it)")
    p.add_argument("issue", type=int, help="The architecture/lld phase-Task")
    p.set_defaults(func=lambda a: cmd_open_design_pr(get_work_item_provider(), a.issue))
    p = sub.add_parser("merge-design-pr",
                        help="Merge a phase-Task's design PR into its epic branch once its review is clean")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True, help="The phase-Task")
    p.add_argument("--repo-path", default=".", help="Any path inside the repository")
    p.set_defaults(func=lambda a: cmd_merge_design_pr(get_work_item_provider(), a.pr, a.issue,
                                                       a.repo_path))
    p = sub.add_parser("add-blocked-by",
                        help="Add a native blockedBy edge (no other side effect)")
    p.add_argument("issue", type=int)
    p.add_argument("--on", type=int, required=True, dest="dep")
    p.set_defaults(func=lambda a: cmd_add_blocked_by(get_work_item_provider(), a.issue, a.dep))
    p = sub.add_parser("remove-blocked-by",
                        help="Remove a native blockedBy edge (no-op when absent)")
    p.add_argument("issue", type=int)
    p.add_argument("--on", type=int, required=True, dest="dep")
    p.set_defaults(func=lambda a: cmd_remove_blocked_by(get_work_item_provider(), a.issue, a.dep))
    p = sub.add_parser("create-lld-tasks",
                        help="Create one Task per lld.md Task section and renumber the doc (idempotent)")
    p.add_argument("epic", type=int, help="The Epic")
    p.add_argument("--repo-path", default=".",
                    help=repo_path_help)
    p.set_defaults(func=lambda a: cmd_create_lld_tasks(get_work_item_provider(), a.epic,
                                                        a.repo_path))
    p = sub.add_parser("open-dev-pr")
    p.add_argument("issue", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--allow-empty", action="store_true",
                    help="Verify-only Task: land an empty commit when the branch has no "
                         "commits ahead of its base, so the PR can open")
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the empty commit's worktree map)")
    p.set_defaults(func=lambda a: cmd_open_dev_pr(get_work_item_provider(), a.issue, a.title,
                                                  a.body, a.summary, allow_empty=a.allow_empty,
                                                  repo_path=a.repo_path))
    p = sub.add_parser("pr-checks")
    p.add_argument("pr", type=int)
    p.set_defaults(func=lambda a: cmd_pr_checks(get_work_item_provider(), a.pr))
    p = sub.add_parser("ci-failure-summary",
                        help="A PR's failing checks with only their failure lines, bounded")
    p.add_argument("pr", type=int)
    p.set_defaults(func=lambda a: cmd_ci_failure_summary(get_work_item_provider(), a.pr))
    p = sub.add_parser("update-pr-body",
                        help="Replace an open pipeline PR's description from a file (REST)")
    p.add_argument("pr", type=int)
    p.add_argument("--body-file", required=True, help="File holding the new PR description")
    p.set_defaults(func=lambda a: cmd_update_pr_body(get_work_item_provider(), a.pr,
                                                     a.body_file))
    p = sub.add_parser("rerun-checks")
    p.add_argument("pr", type=int)
    p.add_argument("--failed-only", action="store_true",
                   help="Re-run only the failed spec files (deflake), not the whole failed job")
    p.set_defaults(func=lambda a: cmd_rerun_checks(get_work_item_provider(), a.pr,
                                                   failed_only=a.failed_only))
    p = sub.add_parser("merge-pr")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the branch's worktree)")
    p.add_argument("--run-id", default=None,
                    help="Book the terminal-unit count under this run id (else the epic's "
                         "run-state file's current id)")
    p.set_defaults(func=lambda a: cmd_merge_pr(get_work_item_provider(), a.pr, a.issue,
                                               a.repo_path, run_id=a.run_id))
    p = sub.add_parser("show-issue-fields",
                       help="The org's issue fields as projectFields.issueFieldsRest wants them "
                            "(run on the laptop; the cloud cannot read org endpoints)")
    p.set_defaults(func=lambda a: {"issueFieldsRest": cmd_show_issue_fields()})
    p = sub.add_parser("create-issue")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--parent", type=int, required=True,
                    help="Parent issue (Epic for a Task, Initiative for an Epic)")
    p.add_argument("--label", action="append", default=[], dest="labels")
    p.add_argument("--type", default="Task", dest="type_name",
                    help="Native Issue Type, mandatory on every issue (default Task)")
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None, help="Default: pipeline.issueDefaults.effort")
    p.add_argument("--blocked-by", type=int, action="append", default=[], dest="blocked_by",
                    help="Native blockedBy edge on this open issue (repeatable); orders "
                         "an Initiative's Epics")
    p.set_defaults(func=lambda a: cmd_create_issue(get_work_item_provider(), a.title, a.body,
                                                    a.parent, a.labels, a.type_name,
                                                    a.priority, a.effort, a.blocked_by))
    p = sub.add_parser("mark-blocked")
    p.add_argument("issue", type=int)
    p.add_argument("--dep", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the unit's worktree)")
    p.set_defaults(func=lambda a: cmd_mark_blocked(get_work_item_provider(), a.issue, a.dep, a.repo_path))
    p = sub.add_parser("mark-needs-human")
    p.add_argument("issue", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the unit's worktree)")
    p.add_argument("--classifier-denied", default=None, metavar="STEP",
                    help="The auto-mode classifier denied STEP (e.g. open-dev-pr): keep the "
                         "worktree; the next run's next-action resumes the unit")
    p.add_argument("--run-id", default=None, help="This run's id (with --classifier-denied)")
    p.set_defaults(func=lambda a: cmd_mark_needs_human(get_work_item_provider(), a.issue, a.reason, a.repo_path,
                                                       a.classifier_denied, a.run_id))
    p = sub.add_parser("show-config",
                        help="Print the effective config and the running plugin version")
    p.set_defaults(func=lambda a: cmd_show_config())

    p = sub.add_parser("pairing-counts",
                        help="Escalation-valve bounce counts for one issue")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_pairing_counts(get_work_item_provider(), a.issue))
    p = sub.add_parser("list-needs-human")
    p.set_defaults(func=lambda a: cmd_list_needs_human(get_work_item_provider()))
    p = sub.add_parser("audit-issues",
                        help="Open pipeline issues missing issueType/parent/Priority/Effort/Status, and Epics with no phase-Tasks")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--epic", type=int, default=None, help="Only this Epic's subtree (the whole repo when omitted)")
    scope.add_argument("--initiative", type=int, default=None,
                       help="Only this Initiative's tree: its Epics and their children")
    p.set_defaults(func=lambda a: cmd_audit_issues(get_work_item_provider(),
                                                    a.epic if a.epic is not None else a.initiative))
    p = sub.add_parser("repair-issue",
                        help="Set an existing issue's missing parent/type/status/Priority/Effort")
    p.add_argument("issue", type=int)
    p.add_argument("--parent", type=int, default=None, help="Linked only when none is set")
    p.add_argument("--type", default=None, dest="type_name",
                    help="Default: inferred from the classification label")
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None, help="Default: pipeline.issueDefaults.effort")
    p.set_defaults(func=lambda a: cmd_repair_issue(get_work_item_provider(), a.issue, a.parent,
                                                    a.type_name, a.priority, a.effort))
    p = sub.add_parser("close-epic",
                        help="Reconcile and merge the epic branch once verified")
    p.add_argument("epic", type=int)
    p.add_argument("--repo-path", default=".")
    p.add_argument("--no-carry-forward", action="store_false", dest="carry_forward",
                    help="Accept only evidence stamped at the epic head itself: no "
                         "carry-forward over a safe delta, no per-child attestation chain")
    p.add_argument("--clean-worktree", action="store_true",
                    help="Before a reconcile or the merge, discard the closing run's untracked "
                         "output from the epic worktree; refuses on tracked changes")
    p.add_argument("--brief", action="store_true",
                    help="Counts in place of the evidence, file and cleanup lists")
    p.set_defaults(func=lambda a: (brief_close_epic if a.brief else (lambda r: r))(
        cmd_close_epic(get_work_item_provider(), a.epic, a.repo_path,
                       carry_forward=a.carry_forward, clean_worktree=a.clean_worktree)))
    p = sub.add_parser("record-epic-verification",
                        help="Record one half of an epic's closing verification")
    p.add_argument("epic", type=int)
    p.add_argument("--kind", required=True, choices=["e2e", "exploratory"])
    p.add_argument("--summary", required=True)
    p.add_argument("--sha", default=None,
                    help="Epic-branch head that was tested (default: --repo-path's HEAD, "
                         "else the origin branch head now)")
    p.add_argument("--repo-path", default=None,
                    help="The worktree that was tested: record its HEAD, not the origin tip")
    p.set_defaults(func=lambda a: cmd_record_epic_verification(
        get_work_item_provider(), a.epic, a.kind, a.summary, a.sha, repo_path=a.repo_path))
    p = sub.add_parser("check-epics-closeable")
    p.set_defaults(func=lambda a: cmd_check_epics_closeable(get_work_item_provider()))
    p = sub.add_parser("close-initiative",
                        help="Close the Initiative once its Epics are closed and verified")
    p.add_argument("initiative", type=int)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_close_initiative(get_work_item_provider(), a.initiative,
                                                       a.repo_path))
    p = sub.add_parser("record-initiative-verification",
                        help="Record an Initiative's closing verification and its outcome")
    p.add_argument("initiative", type=int)
    p.add_argument("--outcome", required=True, choices=list(INITIATIVE_VERIFICATION_OUTCOMES),
                   help="met = every requirement validated; unmet blocks close-initiative")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: cmd_record_initiative_verification(
        get_work_item_provider(), a.initiative, a.summary, a.outcome))
    p = sub.add_parser("check-initiative-closeable",
                        help="Whether every Epic of the Initiative is closed (and its initiative "
                             "branch, if any, merged to main)")
    p.add_argument("initiative", type=int)
    p.set_defaults(func=lambda a: cmd_check_initiative_closeable(get_work_item_provider(), a.initiative))
    p = sub.add_parser("open-initiative-pr",
                        help="Sync initiative-<i> with main and open (or reuse) its PR into main")
    p.add_argument("initiative", type=int)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_open_initiative_pr(get_work_item_provider(), a.initiative,
                                                         a.repo_path))
    p = sub.add_parser("merge-initiative-pr",
                        help="Merge initiative-<i>'s PR into main once its checks and suite "
                             "attestations are green at the head")
    p.add_argument("initiative", type=int)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_merge_initiative_pr(get_work_item_provider(), a.initiative,
                                                          a.repo_path))
    p = sub.add_parser("initiative-profile",
                        help="The Initiative profile governing a unit: its initiative branch "
                             "and whether lld carves the standing test Tasks")
    p.add_argument("issue", type=int, help="Any unit: Initiative, Epic, Task")
    p.set_defaults(func=lambda a: cmd_initiative_profile(None, a.issue))
    p = sub.add_parser("provision-epic-stack",
                        help="Stand up the epic's isolated runtime stack")
    p.add_argument("epic", type=int)
    p.add_argument("--no-up", action="store_true",
                    help="Generate the profile only")
    p.set_defaults(func=lambda a: cmd_provision_epic_stack(a.epic, up=not a.no_up))
    p = sub.add_parser("teardown-epic-stack",
                        help="Tear down the epic's stack and its generated files")
    p.add_argument("epic", type=int)
    p.add_argument("--keep-data", action="store_true", help="Leave the DB data dir in place")
    p.add_argument("--project", default=None,
                    help="Compose project of a hand-made stack (works with the stack disabled)")
    p.add_argument("--profile", default=None,
                    help="Profile of a hand-made stack (works with the stack disabled)")
    p.set_defaults(func=lambda a: cmd_teardown_epic_stack(a.epic, keep_data=a.keep_data,
                                                          project=a.project, profile=a.profile))
    # --- composites ---
    p = sub.add_parser("cut-phase-tasks",
                        help="Create/reuse an Epic's Architecture and LLD phase-Tasks")
    p.add_argument("epic", type=int)
    p.add_argument("--arch-body", default=None)
    p.add_argument("--lld-body", default=None)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_cut_phase_tasks(
        get_work_item_provider(), a.epic, a.arch_body, a.lld_body, a.repo_path))
    p = sub.add_parser("finish-lld",
                        help="merge-design-pr -> create-lld-tasks -> merge-lld-doc -> close-issue")
    p.add_argument("lld_task", type=int, help="The LLD-phase Task")
    p.add_argument("--epic", type=int, required=True)
    p.add_argument("--repo-path", required=True, help="Any path inside the repository")
    p.set_defaults(func=lambda a: cmd_finish_lld(
        get_work_item_provider(), a.lld_task, a.epic, a.repo_path))
    p = sub.add_parser("open-arch-revision",
                        help="Cut an Architecture revision Task and block --blocks on it")
    p.add_argument("epic", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--blocks", type=int, nargs="+", action="extend", default=[],
                    help="Units that must wait for the revision (repeatable)")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_open_arch_revision(
        get_work_item_provider(), a.epic, a.title, a.body, a.blocks, a.repo_path))
    p = sub.add_parser("file-closing-delta",
                        help="File a closing-run finding as a Bug child of the Epic")
    p.add_argument("epic", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None,
                    help="High | Medium | Low (default: pipeline.issueDefaults.effort)")
    p.add_argument("--start", action="store_true",
                    help="Close-blocker lane (operator-authorised): stage development and start it")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_file_closing_delta(
        get_work_item_provider(), a.epic, a.title, a.body, a.priority, a.effort, a.start,
        a.repo_path))
    p = sub.add_parser("start-stage", help="worktree-add, then claim")
    p.add_argument("number", type=int)
    p.add_argument("--role", required=True, choices=sorted({*STAGE_OPTION_IDS, *RETIRED_ROLES}))
    p.add_argument("--unit", choices=["issue", "epic"], default="issue")
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base (e.g. origin/main)")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.add_argument("--run-id", default=None,
                    help="This run's id: refuse a unit not already in flight once the run is at "
                         "the maxTasksPerRun cap (omit = no cap)")
    p.set_defaults(func=lambda a: cmd_start_stage(
        get_work_item_provider(), a.number, a.role, a.unit, a.repo_path, a.base,
        run_id=a.run_id))
    p = sub.add_parser("transition",
                        help="verify-exit -> sync-branch -> start-comment (review roles)")
    p.add_argument("issue", type=int)
    p.add_argument("--expect-stage", required=True,
                    help="Stage just finished, or review role about to start")
    p.add_argument("--pr", type=int, default=None, help="Required with --expect-stage pr-review")
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map)")
    p.add_argument("--base", default=None,
                    help="Override sync-branch's auto-detected integration base")
    p.set_defaults(func=lambda a: cmd_transition(
        get_work_item_provider(), a.issue, a.expect_stage, a.pr, a.repo_path, a.base))
    p = sub.add_parser("advance-standing",
                        help="Standing child skip-ahead: transition -> route -> start-stage")
    p.add_argument("issue", type=int)
    p.add_argument("--from", required=True, dest="frm", choices=list(ADVANCE_FROM),
                    help="Stage just finished (`none` at pickup: no transition)")
    p.add_argument("--to", required=True, choices=list(ADVANCE_TO))
    p.add_argument("--reason", required=True, help=f"One line, <= {ROUTE_REASON_CAP} chars")
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (the unit's worktree when it has one)")
    p.set_defaults(func=lambda a: cmd_advance_standing(
        get_work_item_provider(), a.issue, a.frm, a.to, a.reason, a.repo_path))
    p = sub.add_parser("skip-pr-review",
                        help="Standing child: verify-exit -> route --to merge -> merge-pr")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--reason", required=True, help=f"One line, <= {ROUTE_REASON_CAP} chars")
    p.add_argument("--run-id", default=None, help="Book the terminal-unit count under this run")
    p.add_argument("--repo-path", default=None,
                    help="The shared main checkout (merge-pr releases the unit's worktree)")
    p.set_defaults(func=lambda a: cmd_skip_pr_review(
        get_work_item_provider(), a.issue, a.pr, a.reason, a.run_id, a.repo_path))
    p = sub.add_parser("finish-gate-feedback",
                        help="transition, then mark-feedback-addressed on ready")
    p.add_argument("issue", type=int)
    p.add_argument("--expect-stage", required=True, choices=["product", "architecture"],
                    help="The gated doc's stage")
    p.add_argument("--repo-path", required=True, help="The unit's own worktree")
    p.set_defaults(func=lambda a: cmd_finish_gate_feedback(
        get_work_item_provider(), a.issue, a.expect_stage, a.repo_path))
    p = sub.add_parser("prepare-rework",
                        help="worktree-add (reuse or re-create) -> sync-branch, before resuming an agent")
    p.add_argument("number", type=int)
    p.add_argument("--unit", choices=["issue", "epic"], default="issue")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_prepare_rework(
        get_work_item_provider(), a.number, a.unit, a.repo_path))
    # --- operator control-plane ---
    p = sub.add_parser("detach-epic",
                        help="Take an Epic out of its Initiative (sub-issue link + sibling blockedBy edges)")
    p.add_argument("epic", type=int)
    p.add_argument("--reason", default=None, help="Why, for the comments on both issues")
    p.set_defaults(func=lambda a: cmd_detach_epic(get_work_item_provider(), a.epic, a.reason))
    p = sub.add_parser("reparent-issue",
                        help="Move a plain issue (Task/Bug) under another parent, with comments")
    p.add_argument("issue", type=int)
    p.add_argument("--parent", type=int, required=True, help="The new parent")
    p.add_argument("--reason", default=None, help="Why, for the comments on all three issues")
    p.set_defaults(func=lambda a: cmd_reparent_issue(get_work_item_provider(), a.issue,
                                                      a.parent, a.reason))
    p = sub.add_parser("place",
                        help="Place an Epic/Initiative in the cloud or local (the cloud label), or "
                             "keep one Task of a cloud Epic local (the local label)")
    p.add_argument("number", type=int, help="The Epic or Initiative")
    p.add_argument("--where", required=True, choices=list(PLACEMENTS))
    p.add_argument("--force", action="store_true",
                    help="Place it even while local worktrees hold its units")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` is checked for its units")
    p.set_defaults(func=lambda a: cmd_place(get_work_item_provider(), a.number, a.where,
                                            a.force, a.repo_path))
    p = sub.add_parser("comment", help="Post a plain comment on an issue (no marker)")
    p.add_argument("issue", type=int)
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("--body", default=None, help=f"The comment text (<= {HANDOFF_CAP:,} chars)")
    text.add_argument("--body-file", default=None, help="File holding the comment text")
    p.set_defaults(func=lambda a: cmd_comment(get_work_item_provider(), a.issue, a.body,
                                              a.body_file))
    p = sub.add_parser("park-finding",
                        help="Park a retro finding on pipeline.retro.parkIssue (REST only)")
    p.add_argument("--key", required=True,
                   help="Stable short slug naming the finding; a repeat posts 'seen again'")
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("--text", default=None, help="The finding")
    text.add_argument("--text-file", default=None, help="File holding the finding")
    p.add_argument("--evidence", action="append", default=[], help="Link (repeatable)")
    p.add_argument("--target", default=None, help="Plugin file the fix likely lands in")
    p.set_defaults(func=lambda a: cmd_park_finding(GitHubRest(), a.key, a.text, a.text_file,
                                                   tuple(a.evidence), a.target))
    p = sub.add_parser("supersede-park-issue",
                        help="Mark a retro park issue superseded by its successor and close it")
    p.add_argument("issue", type=int)
    p.add_argument("--by", type=int, required=True, help="The successor park issue")
    p.set_defaults(func=lambda a: cmd_supersede_park_issue(GitHubRest(), a.issue, a.by))
    p = sub.add_parser("edit-issue", help="Operator-directed edit of an issue's title/body/labels "
                                           "(refuses pipeline-owned labels and in-flight units)")
    p.add_argument("issue", type=int)
    p.add_argument("--title", default=None, help="The new issue title (e.g. a rescoped Task)")
    p.add_argument("--body-file", default=None, help="File holding the new issue body")
    p.add_argument("--add-label", action="append", default=[], help="Label to add (repeatable)")
    p.add_argument("--remove-label", action="append", default=[],
                   help="Label to remove (repeatable)")
    p.set_defaults(func=lambda a: cmd_edit_issue(get_work_item_provider(), a.issue, a.body_file,
                                                 a.add_label, a.remove_label, title=a.title))
    # --- human channel (pipeline.humanChannel: session) ---
    p = sub.add_parser("record-operator-answer",
                        help="Record an operator's in-session answer on the unit it settles")
    p.add_argument("issue", type=int)
    p.add_argument("--question", required=True)
    p.add_argument("--answer", required=True)
    p.set_defaults(func=lambda a: cmd_record_operator_answer(get_work_item_provider(), a.issue,
                                                              a.question, a.answer))
    p = sub.add_parser("approve-gate",
                        help="The operator approved the unit's open gate in session: merge + pass it")
    p.add_argument("issue", type=int)
    p.add_argument("--by-operator-session", action="store_true",
                    help="The operator chose Approve in this session")
    p.add_argument("--note", default=None, help="Optional words of the approval, for the audit comment")
    p.add_argument("--repo-path", default=None, help=repo_path_help)
    p.set_defaults(func=lambda a: cmd_approve_gate(get_work_item_provider(), a.issue,
                                                   a.by_operator_session, a.note, a.repo_path))
    p = sub.add_parser("request-gate-changes",
                        help="Post the operator's in-session change request on the open gate PR")
    p.add_argument("issue", type=int)
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("--feedback", default=None, help=f"<= {HANDOFF_CAP:,} chars")
    text.add_argument("--feedback-file", default=None)
    p.set_defaults(func=lambda a: cmd_request_gate_changes(
        get_work_item_provider(), a.issue, _read_text_arg(a.feedback, a.feedback_file, "feedback")))
    # --- Initiative -> cloud Epics (pipeline.placement.initiativeEpics: cloud) ---
    p = sub.add_parser("launch-cloud-epic",
                        help="Place an Epic in the cloud and start its claude --cloud session")
    p.add_argument("epic", type=int)
    p.add_argument("--relaunch", action="store_true",
                    help="Launch even though its last session's wait has not cleared")
    p.add_argument("--allow-overlap", action="store_true",
                    help="Operator only: skip the footprint-overlap checks (recorded in the marker)")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose worktrees `place` checks and whose origin holds footprints")
    p.set_defaults(func=lambda a: cmd_launch_cloud_epic(get_work_item_provider(), a.epic,
                                                        a.repo_path, a.relaunch,
                                                        allow_overlap=a.allow_overlap))
    p = sub.add_parser("record-cloud-session",
                        help="Register a cloud session started by hand on an Epic")
    p.add_argument("epic", type=int)
    p.add_argument("--session", required=True, help="Its claude.ai/code url or session_... id")
    p.add_argument("--note", default=None, help="One line for the operator")
    p.set_defaults(func=lambda a: cmd_record_cloud_session(get_work_item_provider(), a.epic,
                                                           a.session, a.note))
    p = sub.add_parser("cloud-heartbeat",
                        help="Stamp the live cloud session's heartbeat on its Epic")
    p.add_argument("epic", type=int)
    p.add_argument("--state", default="working", choices=list(HEARTBEAT_STATES),
                    help="asking: waiting on the operator's in-session answer (never stalled)")
    p.set_defaults(func=lambda a: cmd_cloud_heartbeat(get_work_item_provider(), a.epic,
                                                      a.state, force=True))
    p = sub.add_parser("cloud-status",
                        help="Read-only state of an Initiative's (or one Epic's) cloud sessions")
    p.add_argument("number", type=int, help="The Initiative or Epic")
    p.add_argument("--repo-path", default=".", help="Repo whose origin holds Epic footprints")
    p.set_defaults(func=lambda a: cmd_cloud_status(get_work_item_provider(), a.number,
                                                   a.repo_path))
    p = sub.add_parser("nudge-cloud-epic",
                        help="Send continue to an Epic's stalled cloud session")
    p.add_argument("epic", type=int)
    p.add_argument("--force", action="store_true", help="Nudge even when not stalled")
    p.set_defaults(func=lambda a: cmd_nudge_cloud_epic(get_work_item_provider(), a.epic,
                                                       a.force))
    p = sub.add_parser("end-cloud-session",
                        help="Post the end marker for the Epic's cloud session")
    p.add_argument("epic", type=int)
    p.add_argument("--outcome", required=True, choices=list(CLOUD_SESSION_OUTCOMES))
    p.add_argument("--note", default=None, help="One line for the operator")
    p.add_argument("--waits-on", type=int, nargs="+", default=None,
                    help="What it waits on: the Epic(s) for waiting-epic; for waiting-human the "
                         "parked units (default: the Epic's needs-human / gate-pending units)")
    p.set_defaults(func=lambda a: cmd_end_cloud_session(get_work_item_provider(), a.epic,
                                                        a.outcome, a.note, a.waits_on))
    args = parser.parse_args(argv)
    _FOCUS.clear()
    _FOCUS.update(_focus_numbers(args))
    _CLOSED_WALK[0] = _CLOSED_WALK_BY_COMMAND.get(args.command, "all")
    _LISTING[0] = _LISTING_BY_COMMAND.get(args.command, lambda: [""])()
    refusal = comment_cap_refusal(args)
    if refusal:
        print(json.dumps(refusal))
        return 0
    try:
        result = args.func(args)
        print(json.dumps(result))
        # Commands that report their own `ok: False` (auto-pass-gate, composites) exit 1 --
        # except an issue created but left unlinked at the sub-issue cap: a retry would
        # duplicate it, so it is a result to act on (`linked: false`), not a failure.
        return 0 if result.get("ok", True) or result.get("linked") is False else 1
    except PlacementMismatch as e:
        print(json.dumps(e.payload))
        return 1
    except GhError as e:
        print(json.dumps({"error": str(e)}))
        return 1
