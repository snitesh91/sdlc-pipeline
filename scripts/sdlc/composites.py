"""Composite commands that run several steps in order."""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .config import (
    EPIC_BRANCH_PREFIX, MAX_TASKS_PER_RUN, PIPELINE, RETIRED_ROLES, Runner, doc_root_rules,
)
from .github import GhError, GitHub, _default_runner, get_work_item_provider
from .units import (
    _issue_info_lookup, check_placement, classify_unit_from_issue, current_stage, doc_root_for,
    bugs_epic_of_in, forget_initiative_hint, hint_doc_roots, initiative_of_in, is_epic,
    is_epic_standing, unit_initiative_profile,
)
from .evidence import REVIEW_ROLES, integration_base, unit_branch
from .run_state import _run_state_dir, note_in_flight, run_cap_reached, run_cap_state, run_completed
from .worktrees import cmd_worktree_add
from .handoff import (
    ROUTE_REASON_CAP, _check_claimable, cmd_claim, cmd_route, cmd_set_stage, cmd_start_comment,
)
from .design_gates import (
    _merge_design_pr_of, cmd_merge_lld_doc, cmd_open_design_pr, cmd_open_gate, cmd_sync_branch,
    design_phase_epic, gate_pr_title, git_rev_parse_head,
)
from .lld_tasks import cmd_add_blocked_by, cmd_create_lld_tasks, task_type_name
from .gates import _profile_for_issue, cmd_mark_feedback_addressed
from .dev_pr import cmd_verify_exit
from .checks import cmd_merge_pr, pr_stale_attestations
from .issue_ops import cmd_close_issue
from .cloud import cloud_heartbeat_quietly
from .issue_admin import cmd_create_issue, cmd_repair_issue


# --- Composite commands: ordered sequences of existing commands ---------------

class StepSequence:
    """Runs a composite's `cmd_*` steps in order, recording each, stopping at the first
    failure: `GhError` or `ok: False` (exit 1), or a `failed` predicate hit (exit 0)."""

    def __init__(self):
        self.steps: dict = {}
        self.completed: list = []
        self.remaining: list = []  # steps never run because an earlier one stopped the sequence
        self.failed_step: Optional[str] = None
        self.ok = True
        self.error: Optional[str] = None

    @property
    def stopped(self) -> bool:
        return self.failed_step is not None

    def skip(self, *names: str) -> None:
        """Record steps a composite leaves out once stopped, so `remaining_steps` names them."""
        if self.stopped:
            self.remaining.extend(n for n in names
                                  if n not in self.remaining and n not in self.steps)

    def run(self, name: str, call: Callable[[], dict],
            failed: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
        """The step's result, or None when it failed or an earlier step had."""
        if self.stopped:
            self.skip(name)
            return None
        try:
            result = call()
        except GhError as e:
            self.steps[name] = {"error": str(e)}
            self.failed_step, self.ok, self.error = name, False, str(e)
            return None
        self.steps[name] = result
        if result.get("ok") is False:
            self.failed_step, self.ok = name, False
        elif failed is not None and failed(result):
            self.failed_step = name
        else:
            self.completed.append(name)
            return result
        return None

    def report(self, failed_key: str = "failed_step", **fields) -> dict:
        out = {**fields, "ok": self.ok, "completed_steps": self.completed,
               failed_key: self.failed_step, "remaining_steps": self.remaining,
               "steps": self.steps}
        warnings = [w for step in self.steps.values() for w in (step.get("warnings") or [])]
        if warnings:
            out["warnings"] = warnings
        if self.error:
            out["error"] = self.error
        elif self.stopped:
            failed = self.steps[self.failed_step]
            out["reason"] = (failed.get("reason") or failed.get("misuse")
                             or f"step {self.failed_step} did not succeed -- see steps")
        return out


def _not_a_phased_epic(issues: dict, epic: int) -> Optional[str]:
    """None when `epic` is a non-standing Epic, else the refusal reason."""
    entry = issues.get(epic)
    if entry is None:
        raise GhError(f"issue #{epic} not found in the repo issue list")
    if not is_epic(entry):
        return f"#{epic} is not an Epic -- only a non-standing Epic has phase-Tasks"
    if is_epic_standing(entry):
        return f"#{epic} is a standing epic -- it has no phase-Tasks"
    return None


def _child_titled(issues: dict, parent: int, title: str,
                  open_only: bool = False) -> Optional[dict]:
    """`parent`'s child titled `title` -- an open one first, else (unless
    `open_only`) a closed one; lowest number wins a tie."""
    matches = sorted((i for i in issues.values()
                      if (i.get("parent") or {}).get("number") == parent and i["title"] == title
                      and (i["state"] == "OPEN" or not open_only)),
                     key=lambda i: (i["state"] != "OPEN", i["number"]))
    return matches[0] if matches else None


def _ensure_staged_task(seq: StepSequence, gh: GitHub, issues: dict, epic: int, key: str,
                        title: str, body: str, stage: str, open_only: bool = False) -> Optional[dict]:
    """Reuse or create `title` under `epic` and give it `stage` if it has none.
    Returns `{"issue", "open"}`, or None when a step failed."""
    existing = _child_titled(issues, epic, title, open_only=open_only)
    if existing:
        task = seq.run(f"{key}.create-issue", lambda: {
            "issue": existing["number"], "reused": True, "state": existing["state"]})
    else:
        task = seq.run(f"{key}.create-issue", lambda: cmd_create_issue(
            gh, title, body, epic, [], type_name=task_type_name()))
    if task is None:
        return None
    n = task["issue"]
    is_open = existing is None or existing["state"] == "OPEN"
    staged = current_stage(existing) if existing else None
    if not is_open or staged:
        # A closed phase-Task is finished; a staged one may have moved on -- never rewind it.
        done = seq.run(f"{key}.set-stage", lambda: {
            "issue": n, "stage": staged, "unchanged": True,
            **({} if is_open else {"reason": "issue is closed"})})
    else:
        done = seq.run(f"{key}.set-stage", lambda: cmd_set_stage(gh, n, stage))
    return {"issue": n, "open": is_open} if done is not None else None


def _worktree_refused(result: dict) -> bool:
    return bool(result.get("diverged"))


def _add_blocked_by_once(gh: GitHub, issue: int, dep: int) -> dict:
    """`cmd_add_blocked_by` unless the edge already exists (re-adding one errors)."""
    if dep in gh.blocked_by(issue):
        return {"issue": issue, "blocked_on": dep, "added": False, "reason": "edge already present"}
    return cmd_add_blocked_by(gh, issue, dep)


def cmd_cut_phase_tasks(gh: GitHub, epic: int, arch_body: Optional[str] = None,
                        lld_body: Optional[str] = None, repo_path: str = ".",
                        runner: Runner = _default_runner) -> dict:
    """Stand up a non-standing Epic's branch `epic-<n>` and its worktree, create (or reuse
    by title) and stage its Architecture- and LLD-phase Tasks, block LLD on Architecture,
    and add the Architecture worktree off `origin/epic-<n>`. Returns `architecture_task`,
    `lld_task` plus the step record."""
    issues = {i["number"]: i for i in gh.issue_list()}
    refusal = _not_a_phased_epic(issues, epic)
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    seq.run("epic-worktree", lambda: cmd_worktree_add(
        gh, epic, unit="epic", repo_path=repo_path, runner=runner), failed=_worktree_refused)
    arch = None if seq.stopped else _ensure_staged_task(
        seq, gh, issues, epic, "architecture", "Architecture phase",
        arch_body or f"Architecture-phase Task for Epic #{epic}: `architecture` -> "
                     f"`arch-review`; authors `epic-{epic}/architecture.md`, merged into "
                     f"`epic-{epic}` by its design PR.",
        "architecture")
    lld = arch and _ensure_staged_task(
        seq, gh, issues, epic, "lld", "LLD phase",
        lld_body or f"LLD-phase Task for Epic #{epic}: `lld` -> `lld-review`; authors "
                    f"`epic-{epic}/lld.md`, merged into `epic-{epic}` by its design PR, "
                    f"from which the Epic's Tasks are created.",
        "lld")
    if lld:
        a, l = arch["issue"], lld["issue"]
        if arch["open"]:
            seq.run("add-blocked-by", lambda: _add_blocked_by_once(gh, l, a))
            seq.run("worktree-add", lambda: cmd_worktree_add(
                gh, a, repo_path=repo_path, runner=runner), failed=_worktree_refused)
        else:
            seq.run("add-blocked-by", lambda: {"issue": l, "blocked_on": a, "added": False,
                                               "reason": "Architecture-phase Task is closed"})
    seq.skip(*(f"{k}.{step}" for k in ("architecture", "lld")
               for step in ("create-issue", "set-stage")), "add-blocked-by", "worktree-add")
    return seq.report(epic=epic, architecture_task=arch and arch["issue"],
                      lld_task=lld and lld["issue"])


def cmd_open_arch_revision(gh: GitHub, epic: int, title: str, body: str,
                           blocks: Optional[list] = None, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """Cut an Architecture revision phase-Task under a non-standing Epic: ensure `epic-<n>`
    and its worktree, create (or reuse an open one by title), stage `architecture`,
    worktree off `origin/epic-<n>`, then block each unit in `blocks` on it.
    Returns `revision_task`."""
    if not title.startswith("Architecture revision"):
        title = f"Architecture revision: {title}"
    issues = {i["number"]: i for i in gh.issue_list()}
    refusal = _not_a_phased_epic(issues, epic)
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    seq.run("epic-worktree", lambda: cmd_worktree_add(
        gh, epic, unit="epic", repo_path=repo_path, runner=runner), failed=_worktree_refused)
    task = None if seq.stopped else _ensure_staged_task(
        seq, gh, issues, epic, "revision", title, body, "architecture", open_only=True)
    if task:
        seq.run("worktree-add", lambda: cmd_worktree_add(
            gh, task["issue"], repo_path=repo_path, runner=runner), failed=_worktree_refused)
    seq.skip("revision.create-issue", "revision.set-stage", "worktree-add")
    for unit in blocks or []:
        seq.run(f"add-blocked-by:{unit}",
                lambda unit=unit: _add_blocked_by_once(gh, unit, task["issue"]))
    return seq.report(epic=epic, revision_task=task and task["issue"])


def cmd_file_closing_delta(gh: GitHub, epic: int, title: str, body: str,
                           priority: Optional[str] = None, effort: Optional[str] = None,
                           start: bool = False, repo_path: str = ".",
                           runner: Runner = _default_runner, defer: bool = False) -> dict:
    """File a closing-run finding as a `Bug` child of a non-standing Epic. It stays `unstaged`
    (full lane) unless `start`, the operator-authorised close-blocker lane: then stage
    `development` and `start-stage` it. `defer` files it under the Epic's Initiative's Bugs
    Epic instead (fixed before initiative close). Returns `delta_issue` and `parent`."""
    issues = {i["number"]: i for i in gh.issue_list()}
    refusal = _not_a_phased_epic(issues, epic)
    parent = epic
    if defer:
        initiative = initiative_of_in(issues, epic)
        bugs = bugs_epic_of_in(issues, initiative["number"]) if initiative else None
        refusal = ("--defer and --start conflict: a deferred bug waits for the Bugs Epic"
                   if start else None if bugs else
                   f"#{epic} has no Initiative with an open standing Bugs Epic -- file it "
                   f"without --defer")
        parent = bugs["number"] if bugs else epic
        body = f"{body}\n\nDeferred from Epic #{epic}."
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    created = seq.run("create-issue", lambda: cmd_create_issue(
        gh, title, body, parent, [], "Bug", priority, effort))
    number = (created or seq.steps.get("create-issue") or {}).get("issue")
    if created is None and number is not None and seq.failed_step == "create-issue":
        # create-issue left a half-made issue (a mid-create failure). Finish it in place with the
        # requested Priority/Effort rather than re-running create-issue, which would duplicate it.
        seq.failed_step, seq.ok, seq.error = None, True, None
        seq.completed.append("create-issue")
        created = seq.run("repair-issue", lambda: cmd_repair_issue(
            gh, number, parent=parent, type_name="Bug", priority=priority, effort=effort))
    if start and created:
        seq.run("set-stage", lambda: cmd_set_stage(gh, number, "development"))
        seq.run("start-stage", lambda: cmd_start_stage(gh, number, "development", "issue",
                                                       repo_path, runner=runner))
    if start:
        seq.skip("set-stage", "start-stage")
    return seq.report(epic=epic, delta_issue=number, parent=parent)


def cmd_finish_lld(gh: GitHub, lld_task: int, epic: int, repo_path: str = ".",
                   runner: Runner = _default_runner) -> dict:
    """After a clean `lld-review`: merge-design-pr (lld.md into `epic-<n>`) ->
    create-lld-tasks -> merge-lld-doc -> close-issue, stopping before the close if anything
    earlier failed. Returns `completed_steps`, `failed_step`, `steps`."""
    parent = (gh.issue_epic_info(lld_task).get("parent") or {}).get("number")
    if parent != epic:
        return {"lld_task": lld_task, "epic": epic, "refused": True,
                "reason": f"#{lld_task}'s parent is #{parent}, not Epic #{epic}"}
    seq = StepSequence()
    # Already merged (a re-run) or a numbered lld.md the merge would revert both count as done.
    seq.run("merge-design-pr", lambda: _merge_design_pr_of(gh, lld_task, repo_path, runner),
            failed=lambda r: not (r.get("merged") or r.get("already_merged")
                                  or r.get("up_to_date")))
    # Nothing to create (`tasks == {}`) is a completed earlier run, not a failure.
    seq.run("create-lld-tasks", lambda: cmd_create_lld_tasks(gh, epic, repo_path,
                                                             runner=runner),
            failed=lambda r: (not r.get("pushed") and r.get("tasks") != {})
            or bool(r.get("dependency_parse_warnings")) or bool(r.get("edges_failed")))
    seq.run("merge-lld-doc", lambda: cmd_merge_lld_doc(gh, repo_path, epic, runner=runner),
            failed=lambda r: not r.get("verified_on_origin"))
    closed = seq.run("close-issue", lambda: cmd_close_issue(gh, lld_task, repo_path=repo_path,
                                                            runner=runner))
    return seq.report(lld_task=lld_task, epic=epic, closed=bool(closed))


def run_in_flight(state: Optional[dict]) -> set:
    """Issue numbers handed out (`note_in_flight`) under `state`'s run id, in any Epic's
    state file -- next-action notes them under the run's top-level issue."""
    if not state:
        return set()
    found = {str(n) for n in state.get("in_flight", {})}
    try:
        names = sorted(os.listdir(_run_state_dir()))
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(_run_state_dir(), name)) as f:
                other = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(other, dict) and other.get("run_id") == state.get("run_id"):
            found.update(str(n) for n in (other.get("in_flight") or {}))
    return {int(n) for n in found if n.isdigit()}


def _start_stage_run_cap(gh: GitHub, number: int, role: str, unit: str,
                         run_id: str) -> dict:
    """start-stage's `run-cap` step: refuse (`stop_at_cap`) a unit this run has not handed
    out once the run is at `maxTasksPerRun`. Returns the run-state `epic` it books under."""
    parent = None if unit == "epic" else \
        ((gh.issue_epic_info(number).get("parent") or {}).get("number"))
    epic = parent or number
    state = run_cap_state(epic, run_id)
    if number not in run_in_flight(state) and run_cap_reached(state):
        completed = run_completed(state)
        return {"stop_at_cap": True, "cap": MAX_TASKS_PER_RUN, "completed": completed,
                "reason": f"run {run_id} has driven {len(completed)} unit(s) to a terminal "
                          f"state, at the maxTasksPerRun cap of {MAX_TASKS_PER_RUN}, and "
                          f"#{number} is not in flight for it -- start no new work this run "
                          f"(a fresh --run-id starts a new one)"}
    return {"stop_at_cap": False, "epic": epic}


def cmd_start_stage(gh: GitHub, number: int, role: str, unit: str = "issue",
                    repo_path: str = ".", base: Optional[str] = None,
                    runner: Runner = _default_runner, run_id: Optional[str] = None) -> dict:
    """Run-cap check (with `run_id`), refusal check, `worktree-add`, then `claim` -- worktree
    first, so the unit is never claimed without a live tree, and nothing runs when the claim
    would be refused. With `run_id`, a unit not already in flight for that run is refused
    (`failed_step: "run-cap"`, `stop_at_cap`, exit 0) once the run is at its cap.
    Returns `path`, `claimed` plus the steps."""
    role = RETIRED_ROLES.get(role, role)
    lookup = _issue_info_lookup(gh)
    check_placement(number, lookup)
    if unit == "epic" and classify_unit_from_issue(lookup(number)) != "epic":
        # `--unit epic` on a Task would cut a stray `epic-<n>` branch + worktree.
        raise GhError(f"#{number} is not an Epic -- start-stage --unit epic is only for an "
                      f"Epic; a Task (phase-Task included) takes the default --unit issue")
    seq = StepSequence()
    if run_id:
        cap = seq.run("run-cap", lambda: _start_stage_run_cap(gh, number, role, unit, run_id),
                      failed=lambda r: r.get("stop_at_cap"))
        if cap is None and seq.failed_step == "run-cap":
            seq.skip("check-claimable", "worktree-add", "claim")
            return seq.report(issue=number, role=role, path=None, claimed=False,
                              stop_at_cap=True)
    claimable = seq.run("check-claimable", lambda: _check_claimable(gh, number, role, refuse=True),
                        failed=lambda r: r.get("refused"))
    if claimable is None and seq.failed_step == "check-claimable" and seq.ok:
        # An open PR on a `development` unit is a structured refusal (exit 0), not a failure.
        seq.skip("epic-worktree", "worktree-add", "claim")
        refusal = seq.steps["check-claimable"]
        return seq.report(issue=number, role=role, path=None, claimed=False, refused=True,
                          pr=refusal["pr"])
    # A child of a non-standing Epic integrates into `epic-<parent>`. Ensure that branch AND its
    # worktree exist first (idempotently, exactly as cut-phase-tasks does) so `next-action` never
    # delegates a child onto a base branch that isn't there yet -- a live worktree is fast-forwarded.
    if unit == "issue" and not seq.stopped:
        integ = integration_base(gh, number)
        if integ != "main":
            epic_no = int(integ[len(EPIC_BRANCH_PREFIX):])
            seq.run("epic-worktree", lambda: cmd_worktree_add(
                gh, epic_no, unit="epic", repo_path=repo_path, runner=runner),
                failed=_worktree_refused)
    worktree = seq.run("worktree-add", lambda: cmd_worktree_add(
        gh, number, unit, repo_path, runner=runner, base=base), failed=_worktree_refused)
    claim = seq.run("claim", lambda: cmd_claim(gh, number, role))
    if run_id and claim:
        note_in_flight(seq.steps["run-cap"]["epic"], run_id, {number: role})
    # Per-product doc roots: resolving here also records the unit for the SubagentStart hook.
    roots: dict = {}
    if doc_root_rules():
        try:
            roots["doc_root"] = doc_root_for(number, gh)
        except GhError as e:
            roots["doc_root_error"] = str(e)
    if PIPELINE["initiativeProfiles"]:
        # Likewise the unit's Initiative profile (standing test Tasks off, initiative branch).
        try:
            prof = unit_initiative_profile(gh, number)
            if prof["name"] is not None:
                roots["initiative_profile"] = {k: prof[k] for k in
                                               ("initiative", "name", "branch", "testTasks")}
        except GhError as e:
            roots["initiative_profile_error"] = str(e)
            forget_initiative_hint(number)
    if claim:
        hb = cloud_heartbeat_quietly(gh, number)
        if hb:
            roots["heartbeat_warning"] = hb
    return seq.report(issue=number, role=role, path=(worktree or {}).get("path"),
                      claimed=bool(claim), **roots)


# A review role with no Stage value of its own is verified against the stage it follows.
_REVIEW_FOLLOWS_STAGE = {"product-review": "product", "arch-review": "architecture",
                         "lld-review": "lld"}


def cmd_transition(gh: GitHub, issue: int, expect_stage: str, pr: Optional[int] = None,
                   repo_path: Optional[str] = None, base: Optional[str] = None,
                   runner: Runner = _default_runner) -> dict:
    """After a stage agent returns: verify-exit -> sync-branch -> open-design-pr (a
    non-standing Epic's architecture/lld phase-Task only) -> start-comment (review roles only).
    Stops at an unverified exit, a missing pr-review handoff marker, or a sync conflict /
    missing base. Returns `ready`, `stopped_at`, `steps`."""
    if expect_stage == "pr-review" and pr is None:
        raise GhError("transition --expect-stage pr-review needs --pr: the handoff-marker "
                      "check only runs with the PR in hand")
    verify_stage = _REVIEW_FOLLOWS_STAGE.get(expect_stage, expect_stage)
    seq = StepSequence()
    # An unverified exit (missing handoff marker included) is `ok: False`: exit 1.
    seq.run("verify-exit", lambda: cmd_verify_exit(gh, repo_path, issue, verify_stage, pr,
                                                   runner=runner))
    seq.run("sync-branch", lambda: cmd_sync_branch(gh, repo_path, issue, runner=runner,
                                                   base=base),
            failed=lambda r: not r.get("synced"))
    if not seq.stopped and design_phase_epic(gh, issue, verify_stage) is not None:
        seq.run("open-design-pr", lambda: cmd_open_design_pr(gh, issue, runner=runner),
                failed=lambda r: not r.get("pr"))
    if expect_stage in REVIEW_ROLES:
        seq.run("start-comment", lambda: cmd_start_comment(gh, issue, expect_stage))
    extra = {}
    if expect_stage == "arch-review":
        # Top level, resolved even when a step stopped: the unit's profile bar (an Epic
        # phase-Task's is its Epic's profile), else the global `gates` default.
        extra["skip_confidence_threshold"] = (
            (seq.steps.get("start-comment") or {}).get("skip_confidence_threshold")
            or _profile_for_issue(gh, issue)["gates"]["skipConfidenceThreshold"])
    hb = cloud_heartbeat_quietly(gh, issue)
    if hb:
        extra["heartbeat_warning"] = hb
    return seq.report(failed_key="stopped_at", issue=issue, expect_stage=expect_stage,
                      verified_stage=verify_stage, ready=not seq.stopped, **extra)


# `advance-standing --from`: the stage a standing child just finished (`none` = at pickup). A
# review role verifies as the stage it follows, so `transition` never posts its start comment.
ADVANCE_FROM = ("none", "product", "product-review", "architecture", "arch-review")


ADVANCE_TO = ("product", "architecture", "development")


def cmd_advance_standing(gh: GitHub, issue: int, frm: str, to: str, reason: str,
                         repo_path: Optional[str] = None,
                         runner: Runner = _default_runner) -> dict:
    """Skip a standing child ahead after a stage returns (or at pickup): `transition
    --expect-stage <frm>` (none at pickup) -> `route --to <to>` -> `start-stage --role <to>`.
    Returns `routed`, `path`, `claimed` plus the step record."""
    if frm not in ADVANCE_FROM:
        raise GhError(f"--from must be one of {', '.join(ADVANCE_FROM)}")
    if to not in ADVANCE_TO:
        raise GhError(f"--to must be one of {', '.join(ADVANCE_TO)} -- skipping pr-review "
                      f"is `skip-pr-review`")
    seq = StepSequence()
    if frm != "none":
        seq.run("transition", lambda: cmd_transition(
            gh, issue, _REVIEW_FOLLOWS_STAGE.get(frm, frm), repo_path=repo_path, runner=runner),
            failed=lambda r: not r.get("ready"))
    routed = seq.run("route", lambda: cmd_route(gh, issue, to, reason, finished=frm),
                     failed=lambda r: not r.get("routed"))
    started = seq.run("start-stage", lambda: cmd_start_stage(
        gh, issue, to, "issue", repo_path or ".", runner=runner),
        failed=lambda r: r.get("failed_step") is not None)
    return seq.report(issue=issue, **{"from": frm}, to=to, routed=bool(routed),
                      path=(started or {}).get("path"), claimed=bool(started))


def cmd_skip_pr_review(gh: GitHub, issue: int, pr: int, reason: str,
                       run_id: Optional[str] = None, repo_path: Optional[str] = None,
                       runner: Runner = _default_runner) -> dict:
    """Merge a standing child's PR without `pr-review`: `verify-exit --expect-stage pr-review
    --pr` (handoff marker, PR scope) -> `route --to merge` (its marker is the merge evidence;
    refused after a `pr-review` rework bounce) -> `merge-pr`. Returns `merged` plus the steps."""
    seq = StepSequence()
    seq.run("verify-exit", lambda: cmd_verify_exit(gh, repo_path, issue, "pr-review", pr,
                                                   runner=runner))
    seq.run("route", lambda: cmd_route(gh, issue, "merge", reason),
            failed=lambda r: not r.get("routed"))
    merged = seq.run("merge-pr", lambda: cmd_merge_pr(gh, pr, issue, repo_path or ".",
                                                     run_id=run_id),
                     failed=lambda r: not r.get("merged"))
    return seq.report(issue=issue, pr=pr, merged=bool(merged))


def cmd_finish_gate_feedback(gh: GitHub, issue: int, expect_stage: str,
                             repo_path: Optional[str] = None,
                             runner: Runner = _default_runner) -> dict:
    """After a gate-feedback agent returns `done`: `transition --expect-stage <s>` (verifies the
    push, syncs), then -- only on `ready` -- `mark-feedback-addressed`, returning the issue to
    `Awaiting Human Review`. Returns `ready`, `pipeline_status` plus the steps."""
    seq = StepSequence()
    seq.run("transition", lambda: cmd_transition(gh, issue, expect_stage, repo_path=repo_path,
                                                 runner=runner),
            failed=lambda r: not r.get("ready"))
    marked = seq.run("mark-feedback-addressed", lambda: cmd_mark_feedback_addressed(gh, issue))
    return seq.report(issue=issue, expect_stage=expect_stage,
                      ready="transition" in seq.completed,
                      pipeline_status=(marked or {}).get("pipeline_status"))


def _suite_evidence_needs(gh: GitHub, issue: int, pr: int) -> dict:
    """`pr_stale_attestations` on the PR's current head, reshaped for `prepare-rework`:
    `needed` (per suite: its `check` state -- `pending` = wait for CI, `missing` = no PR-level
    CI, re-run and `record-local-ci`), `carried`, `passed_by_check`. Never raises."""
    try:
        status = pr_stale_attestations(gh, issue, pr)
    except GhError as e:
        return {"error": str(e)}
    return {"needed": status["stale"], "carried": status["carried"],
            "passed_by_check": status["passed_by_check"]}


def cmd_prepare_rework(gh: GitHub, number: int, unit: str = "issue", repo_path: str = ".",
                       runner: Runner = _default_runner) -> dict:
    """Before resuming a rework/run-only agent (or after a `behind_base`/`held`): `worktree-add`
    (reuses a live tree, else re-creates a released one from origin) -> `sync-branch`. Returns
    `path`, the post-sync `head`, `conflict`/`conflicting_files`, the open `pr` and `suites`
    -- which required suites the new head still needs a check or attestation for."""
    hint_doc_roots(gh, number)
    seq = StepSequence()
    tree = seq.run("worktree-add", lambda: cmd_worktree_add(gh, number, unit, repo_path,
                                                             runner=runner),
                   failed=_worktree_refused)
    # A missing base leaves nothing to reconcile: the agent resumes as is.
    sync = seq.run("sync-branch", lambda: cmd_sync_branch(gh, repo_path, number, unit,
                                                          runner=runner),
                   failed=lambda r: not (r.get("synced") or r.get("base_missing")))
    synced = seq.steps.get("sync-branch") or {}
    out = {"unit": unit, "path": (tree or {}).get("path"), "head": None,
           "conflict": bool(synced.get("conflict")), "pr": None}
    if synced.get("conflict"):
        out["conflicting_files"] = synced.get("conflicting_files", [])
    if sync is not None:
        out["head"] = git_rev_parse_head(out["path"], runner=runner)
        prs = gh.pr_list_for_branch(unit_branch(unit, number)) if unit == "issue" else []
        if prs:
            out["pr"] = prs[0]["number"]
            out["suites"] = _suite_evidence_needs(gh, number, out["pr"])
    return seq.report(issue=number, **out)


# references/stage-playbooks.md, "Comment size is a contract": command -> (flag, cap).
HANDOFF_CAP, EVIDENCE_CAP = 2_000, 6_000


COMMENT_CAPS = {
    "handoff-to-pr-review": ("summary", EVIDENCE_CAP),
    "record-pr-review": ("summary", EVIDENCE_CAP),
    "record-design-review": ("summary", EVIDENCE_CAP),
    "open-dev-pr": ("summary", HANDOFF_CAP),
    "open-gate": ("summary", HANDOFF_CAP),
    "skip-gate": ("summary", HANDOFF_CAP),
    "waive-gate": ("summary", HANDOFF_CAP),
    "record-epic-verification": ("summary", HANDOFF_CAP),
    "record-initiative-verification": ("summary", HANDOFF_CAP),
    "mark-needs-human": ("reason", HANDOFF_CAP),
    "resolve-thread": ("reply", HANDOFF_CAP),
    "route": ("reason", ROUTE_REASON_CAP),
    "advance-standing": ("reason", ROUTE_REASON_CAP),
    "skip-pr-review": ("reason", ROUTE_REASON_CAP),
    "close-issue": ("reason", HANDOFF_CAP),
    "end-cloud-session": ("note", ROUTE_REASON_CAP),
    "record-cloud-session": ("note", ROUTE_REASON_CAP),
    "comment": ("body", HANDOFF_CAP),
    "detach-epic": ("reason", HANDOFF_CAP),
    "reparent-issue": ("reason", HANDOFF_CAP),
}


def comment_cap_refusal(args) -> Optional[dict]:
    """A refusal when the command's comment text is over its cap, else None."""
    flag, cap = COMMENT_CAPS.get(args.command, (None, 0))
    text = getattr(args, flag, None) if flag else None
    if not text or len(text) <= cap:
        return None
    return {"refused": True,
            "reason": f"--{flag} is {len(text):,} chars, over the {cap:,}-char cap for this "
                      f"comment (references/stage-playbooks.md, \"Comment size is a "
                      f"contract\"); trim it and re-run"}


def _open_gate_cli(a) -> dict:
    gh = get_work_item_provider()
    title, normalized = gate_pr_title(gh, a.issue, a.title)
    result = cmd_open_gate(gh, a.repo_path, a.issue, title, a.doc, a.next_stage, a.summary)
    return {**result, "title": title, "title_normalized": normalized}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import cli  # noqa: E402,F401
