"""`next-action`: the orchestrator's next step, epic sync and the review queue."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .config import (
    DEV_LANE_PARALLELISM, GATE_PENDING_STATUSES, MAX_TASKS_PER_RUN, PIPELINE, PRODUCT_WIP_CAP,
    PR_REVIEW_PARALLELISM, RESUME_LIVE_WINDOW_MINUTES, REVIEW_ENTRY_STAGES, Runner,
    issue_branch,
)
from .github import GhError, GitHub, WorkItemProvider, _default_runner, _utc_now_marker
from .units import (
    _parse_marker_ts, cloud_label, current_stage, default_stage, doc_root_for, effective_gates,
    epic_base, forget_initiative_hint, has_label, hint_doc_roots, in_scope, initiative_branch,
    is_epic, is_epic_architected, is_epic_legacy, is_epic_standing, is_initiative,
    local_tasks_config_errors, pipeline_status, placement_scope, product_gate_pending,
    product_wip_headroom, resolve_initiative_profile, session_placement, sort_key,
    unit_initiative_profile,
)
from .evidence import (
    _glob_matches, epic_branch, last_pr_review_outcome, last_transition_to,
    routed_past_pr_review,
)
from .epic_close import initiative_landing
from .gate_eval import evaluate_gate
from .run_state import (
    _write_run_state, config_drift, note_in_flight, read_run_state, run_cap_reached,
    run_cap_state, run_completed,
)


PHASE_TASK_TITLES = {"architecture": "Architecture phase", "lld": "LLD phase"}


def epic_lacks_phase_tasks(all_issues: list, epic: int) -> bool:
    """Whether a non-standing Epic still needs `cut-phase-tasks`: it lacks either of its two
    phase-Tasks, found by exact title, and is not `epic:architected`. Any other child -- a
    pre-filed or reparented Task, a closed superseded issue -- never stands in for them."""
    by_number = {i["number"]: i for i in all_issues}
    if epic in by_number and is_epic_architected(by_number[epic]):
        return False
    titles = {i["title"] for i in all_issues
              if (i.get("parent") or {}).get("number") == epic and not is_epic(i)}
    return not (PHASE_TASK_TITLES["architecture"] in titles and PHASE_TASK_TITLES["lld"] in titles)


def _epic_wait_reason(gh: GitHub, epic_issue: dict) -> Optional[str]:
    """Why an open Epic cannot be run yet (`legacy`, placed in the cloud while this session
    is local, or `blocked by #n`), else None."""
    if is_epic_legacy(epic_issue):
        return "not driven (legacy profile)"
    # Under a cloud-placed Initiative every Epic is cloud, and next-action refused a local session.
    if has_label(epic_issue, cloud_label()) and session_placement()["placement"] != "cloud":
        return f"placed in the cloud (`{cloud_label()}`) -- a cloud session drives it"
    blockers = gh.blocked_by(epic_issue["number"])
    return f"blocked by {', '.join(f'#{n}' for n in blockers)}" if blockers else None


def _initiative_epic_step(gh: GitHub, all_issues: list, initiative: int,
                          skip: list) -> Optional[dict]:
    """The Initiative loop's next move: `cut-phase-tasks` or `run-epic` for the lowest-numbered
    open, unblocked cut Epic not in `skip`; None when no Epic can run."""
    epics = sorted((i for i in all_issues if i["state"] == "OPEN" and is_epic(i)
                    and (i.get("parent") or {}).get("number") == initiative),
                   key=lambda i: i["number"])
    for e in epics:
        n = e["number"]
        if n in skip or _epic_wait_reason(gh, e):
            continue
        base = {"epic": n, "initiative": initiative, "unit": "epic"}
        if not is_epic_standing(e) and epic_lacks_phase_tasks(all_issues, n):
            return {"action": "cut-phase-tasks", **base,
                    "reason": f"Epic #{n} has no phase-Tasks: run `cut-phase-tasks {n} "
                              f"--repo-path <p>`, then `next-action {initiative}` again"}
        return {"action": "run-epic", **base,
                "reason": f"Epic #{n} is the first open, unblocked Epic of Initiative "
                          f"#{initiative}: run its Step 1-3 loop (`next-action {n}` with "
                          f"this --run-id), then return to the Initiative"}
    return None


def _open_epics_reason(gh: GitHub, cut_epics: list, skip: list) -> str:
    """The Initiative `none` reason for open Epics that cannot run: each one's actual state."""
    open_epics = sorted((i for i in cut_epics if i["state"] != "CLOSED"),
                        key=lambda i: i["number"])
    states = [f"#{e['number']} " + ("parked this run" if e["number"] in skip
                                     else _epic_wait_reason(gh, e) or "runnable")
              for e in open_epics]
    closed = len(cut_epics) - len(open_epics)
    return (f"{len(open_epics)} cut Epic(s) open, none runnable now: {'; '.join(states)}"
            + (f" ({closed} closed)" if closed else "") + ".")


def _merged_design_action(gh: GitHub, issue: dict, epic: int) -> Optional[dict]:
    """The completion action for an architecture/lld phase-Task whose design PR a human
    merged before any gate opened: `pass-gate` (architecture) or `finish-lld` (lld)."""
    stage = current_stage(issue)
    # Only a claimed unit: a gate-pending one is `pass-gate`'s, an unclaimed one has no PR yet.
    if stage not in DESIGN_STAGE_REVIEW or pipeline_status(issue) != "in-progress":
        return None
    found = find_design_pr(gh.issue_view(issue["number"]).get("comments", []))
    if not found or found[0] != stage \
            or gh.pr_view(found[1], fields="state").get("state") != "MERGED":
        return None
    base = {"issue": issue["number"], "unit": "issue", "design_pr": found[1]}
    if stage == "architecture":
        return {"action": "pass-gate", **base, "gate_pr": found[1], "stage": stage}
    return {"action": "finish-lld", **base, "epic": epic}


def decide_next_action(gh: GitHub, epic: int, run_id: Optional[str] = None,
                       skip_epics: Optional[list] = None, repo_path: str = ".",
                       runner: Runner = _default_runner,
                       all_issues: Optional[list] = None, scope: Optional[str] = None) -> dict:
    """`scope` (`placement_scope`) limits the children: "cloud" skips local Tasks (listed as
    `local_tasks` on a `none`), "local-tasks" drives only them and no Epic-level step.
    Pick the next action among the named Epic's/Initiative's open non-Epic children: `skip` |
    `resume` | `pass-gate` | `finish-lld` | `address-gate-feedback` | `route` | `delegate` |
    `stop-at-cap` | `none`; an Initiative with no such child walks its open Epics: `cut-phase-tasks`
    | `run-epic` (`skip_epics` parks stalled ones). Raises GhError when `epic` is neither.
    Caps gate only fresh work: the run cap never defers a unit this run already handed out, and
    a fresh dev-lane unit needs a free slot and a footprint disjoint from the holders'
    (`dev_lane_slots`); `slots` reports that arithmetic on a dev-lane `delegate` or `none`."""
    skip_epics = skip_epics or []
    all_issues = gh.issue_list() if all_issues is None else all_issues
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None:
        raise GhError(f"#{epic} is not an epic or Initiative -- "
                       f"pass the epic's/Initiative's own issue number, not a child issue's")
    if not is_epic(epic_issue) and not is_initiative(epic_issue):
        raise GhError(f"#{epic} is not an epic or Initiative (pipeline.classification "
                       f"matches neither) -- pass the unit's own issue number, not a child "
                       f"issue's")
    if is_epic_legacy(epic_issue):
        return {"action": "skip", "epic": epic,
                "reason": "epic:legacy -- this pipeline no longer drives this epic or any of "
                          "its children; skipping entirely."}
    children = [i for i in all_issues if i["state"] == "OPEN"
                and i.get("parent") and i["parent"]["number"] == epic and not is_epic(i)]
    other_side = sorted(i["number"] for i in children if not in_scope(i, scope))
    children = [i for i in children if in_scope(i, scope)]
    # A profile whose Gate A auto-passes never fills the human queue, so it is uncapped.
    product_headroom = (product_wip_headroom(all_issues)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    deferred_by_cap: list = []
    unstaged: list = []
    # Run cap gates only fresh work; refusing in-flight work would strand it mid-pipeline.
    cap_state = run_cap_state(epic, run_id)
    at_cap = run_cap_reached(cap_state)
    # Units this run already handed out (`note_in_flight`): started work the cap never defers.
    in_flight = {int(n) for n in (cap_state or {}).get("in_flight", {}) if str(n).isdigit()}
    deferred_by_run_cap: list = []
    deferred_by_lane: list = []
    lane: dict = {}
    cloud_survey: dict = {}

    def capped(stage: str) -> bool:
        return stage == "product" and product_headroom is not None and product_headroom <= 0

    def lane_refusal(number: int) -> Optional[str]:
        """Why a fresh dev-lane unit cannot start now, else None; computes the lane once."""
        if not lane:
            try:
                lane.update(dev_lane_slots(gh, repo_path, epic, all_issues, runner=runner))
                if lane["occupied"] or lane["cross_epic"]:
                    _fetch_origin(repo_path, runner)
                    lane["footprints"] = dev_lane_footprints(lane, repo_path, by_number,
                                                             runner=runner, gh=gh)
            except GhError as e:
                lane.update(cap=DEV_LANE_PARALLELISM, occupied=[], free=DEV_LANE_PARALLELISM,
                            excluded_other_epic=[], cross_epic=[], footprints=[],
                            warning=f"dev lane unverifiable ({e}); treating it as empty")
        return dev_lane_refusal(lane, number, lambda n: read_footprint(
            repo_path, n, runner=runner, epic=epic, gh=gh))

    def with_lane(result: dict) -> dict:
        """The lane arithmetic (`slots`) once consulted, and what it deferred (`dev_lane`)."""
        if lane:
            keys = ("cap", "occupied", "free", "excluded_other_epic", "warning")
            result["slots"] = {k: lane[k] for k in keys if k in lane}
        if deferred_by_lane:
            result["dev_lane"] = {
                "deferred": deferred_by_lane,
                "reason": "a fresh dev-lane unit waits on a slot or on a holder's footprint; "
                          "it starts once a tracked agent finishes -- survey again then."}
        return result

    def none_result() -> dict:
        if deferred_by_run_cap:
            # Never `none`: "run is full" must not read as "epic has nothing left".
            completed = run_completed(cap_state)
            return {"action": "stop-at-cap", "epic": epic, "cap": MAX_TASKS_PER_RUN,
                    "completed": completed, "in_flight": sorted(in_flight),
                    "reason": f"this run has driven {len(completed)} unit(s) to a terminal "
                              f"state, reaching the maxTasksPerRun cap of "
                              f"{MAX_TASKS_PER_RUN}. Deferred this pass: "
                              f"{', '.join(f'#{n}' for n in deferred_by_run_cap)}. Every "
                              f"unit's state is already persisted -- start a new run (a "
                              f"fresh --run-id) to carry on with a small context."}
        result = with_lane({"action": "none", "epic": epic})
        if scope == "cloud" and other_side:
            result["local_tasks"] = other_side
            if not children:
                result["waiting_local"] = True
                result["reason"] = (
                    f"only local Tasks remain ({', '.join(f'#{n}' for n in other_side)}) -- "
                    f"end this session with `end-cloud-session {epic} --outcome waiting-local`; "
                    f"a laptop `/sdlc:run {epic}` drives them, and the laptop relaunches "
                    f"this Epic's cloud session to close it once they are done")
        elif scope == "local-tasks":
            result["local_tasks_only"] = True
            result["cloud_children"] = other_side
            if not children:
                result["reason"] = (
                    f"no local Task of cloud Epic #{epic} is actionable here -- "
                    + (f"the cloud session drives the rest "
                       f"({', '.join(f'#{n}' for n in other_side)})" if other_side else
                       "the cloud session closes the Epic: `cloud-status` shows it "
                       "`relaunch_ready` once its local Tasks are done"))
            return result
        if unstaged:
            result["unstaged"] = unstaged
            result["unstaged_reason"] = (
                "Stage-less child(ren) of an architected Epic -- filed after the LLD "
                "phase, so no stage can be guessed. Route each by hand: `set-stage "
                "development` when the Epic's lld.md already covers it, or cut an "
                "Architecture revision phase-Task when it doesn't fit the design.")
        if deferred_by_cap:
            result["product_cap"] = {"limit": PRODUCT_WIP_CAP,
                                     "pending": product_gate_pending(all_issues),
                                     "deferred": deferred_by_cap}
        elif is_initiative(epic_issue) and epic_issue["state"] == "CLOSED":
            result["reason"] = f"Initiative #{epic} is closed -- nothing left to do."
        elif is_initiative(epic_issue):
            # "No open children" is ambiguous; inspect every child, open and closed.
            initiative_children = [i for i in all_issues if i.get("parent")
                                   and i["parent"]["number"] == epic]
            cut_epics = [i for i in initiative_children if is_epic(i)]
            roadmap_tasks = [i for i in initiative_children if not is_epic(i)]
            landing = (initiative_landing(gh, epic)
                       if cut_epics and all(i["state"] == "CLOSED" for i in cut_epics)
                       and resolve_initiative_profile(epic_issue)["branch"] else None)
            if landing and not landing["landed"]:
                result["initiative_branch"] = landing
                result["reason"] = (
                    f"every cut Epic is closed -- `{landing['branch']}` must reach main first: "
                    + (f"`merge-initiative-pr {epic}` once PR #{landing['pr']}'s evidence is "
                       f"green" if landing["state"] == "open" else
                       f"`open-initiative-pr {epic} --repo-path <p>`, then "
                       f"`merge-initiative-pr {epic}` once its evidence is green")
                    + " (references/initiatives.md, \"Closing an Initiative\").")
            elif cut_epics and all(i["state"] == "CLOSED" for i in cut_epics):
                result["reason"] = ("every cut Epic is closed -- ready for initiative-close "
                                    "validation (references/initiatives.md, \"Closing an Initiative\").")
            elif cut_epics and cloud_survey:
                result["reason"] = _cloud_epics_reason(cloud_survey, cut_epics)
                result["cloud"] = cloud_survey
            elif cut_epics:
                result["reason"] = _open_epics_reason(gh, cut_epics, skip_epics)
            elif roadmap_tasks and all(i["state"] == "CLOSED" for i in roadmap_tasks):
                result["reason"] = ("Product-Roadmap Task closed -- cut Epics from the "
                                    "approved product.md next (references/initiatives.md, \"Cutting Epics "
                                    "from an approved Initiative\").")
            elif not initiative_children:
                result["reason"] = ("no Product-Roadmap Task yet -- cut it next (references/initiatives.md, "
                                    "\"Cutting an Initiative's Product-Roadmap Task\").")
        return result

    # A human may merge a design PR before its gate opens; that is a finished stage, not a
    # dead session to resume.
    if is_epic(epic_issue) and not is_epic_standing(epic_issue):
        for issue in sorted(children, key=sort_key):
            merged = _merged_design_action(gh, issue, epic)
            if merged:
                return merged

    denied = _classifier_denied_resume(gh, children, run_id)
    if denied:
        return denied

    # Crash-recovery: the unit's own open children only.
    in_progress = [i for i in children if pipeline_status(i) == "in-progress"]
    if in_progress:
        target = in_progress[0]
        return {"action": "resume", "issue": target["number"], "unit": "issue",
                "stage": current_stage(target) or default_stage(epic_issue)}

    # Before the Epic is architected a Stage-less child is a Task awaiting
    # `merge-lld-doc`, which only advances Stage-less Tasks -- so leave it untouched.
    design_pending = (is_epic(epic_issue) and not is_epic_standing(epic_issue)
                      and not is_epic_architected(epic_issue))

    for issue in sorted(children, key=sort_key):
        status = pipeline_status(issue)
        if status == "needs-human":
            continue
        if design_pending and current_stage(issue) is None:
            continue
        if status in GATE_PENDING_STATUSES:
            gate = evaluate_gate(gh, issue["number"])
            if gate["status"] == "satisfied":
                return {"action": "pass-gate", "issue": issue["number"], "unit": "issue",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"]}
            if gate["status"] == "feedback_pending":
                return {"action": "address-gate-feedback", "issue": issue["number"], "unit": "issue",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"],
                        "unresolved_threads": gate["unresolved_threads"],
                        "new_comments": gate["new_comments"]}
            continue
        stage = current_stage(issue) or default_stage(epic_issue)
        pickup = stage is None and is_epic_standing(epic_issue)
        if stage is None and not pickup:
            unstaged.append(issue["number"])
            continue
        if capped(stage):
            # Before blockedBy and the Stage write: a deferred unit gets no side effects.
            deferred_by_cap.append(issue["number"])
            continue
        if at_cap and issue["number"] not in in_flight:
            deferred_by_run_cap.append(issue["number"])
            continue
        if gh.blocked_by(issue["number"]):
            continue
        if pickup:
            # The orchestrator picks a standing child's first stage from the issue (`route`).
            return with_lane({"action": "route", "issue": issue["number"], "unit": "issue"})
        if stage in DEV_LANE_START_STAGES:
            refusal = lane_refusal(issue["number"])
            if refusal:
                deferred_by_lane.append({"issue": issue["number"], "reason": refusal})
                continue
        if current_stage(issue) is None:
            # Stamp the Stage on first sight so the board never shows it blank.
            gh.set_stage_field(issue["number"], stage)
        return with_lane({"action": "delegate", "issue": issue["number"], "unit": "issue",
                          "stage": stage})

    if scope == "local-tasks":
        return none_result()  # Epic-level steps (cut, launch, Initiative loop) are the cloud's
    if (is_initiative(epic_issue) and epic_issue["state"] == "OPEN" and not deferred_by_run_cap
            and initiative_epics_placement(epic_issue) == "cloud"
            and session_placement()["placement"] == "local"):
        # Each Epic runs in its own cloud session; this run only launches them.
        open_epics = [e["number"] for e in _initiative_epics(all_issues, epic)
                      if e["state"] == "OPEN"]
        if open_epics:
            cloud_survey.update(cloud_epic_survey(gh, all_issues, open_epics,
                                                  repo_path=repo_path, runner=runner))
            if cloud_survey["launchable"]:
                return {"action": "launch-cloud-epics", "initiative": epic, "unit": "epic",
                        "epics": cloud_survey["launchable"], "cloud": cloud_survey,
                        "reason": f"this Initiative's Epics are placed in the cloud: run "
                                  f"`launch-cloud-epic <n>` for each of "
                                  f"{', '.join(f'#{n}' for n in cloud_survey['launchable'])}, "
                                  f"then `next-action {epic}` again"}
    elif is_initiative(epic_issue) and epic_issue["state"] == "OPEN" and not deferred_by_run_cap:
        step = _initiative_epic_step(gh, all_issues, epic, skip_epics)
        if step and at_cap:
            deferred_by_run_cap.append(step["epic"])
        elif step:
            return step
    # A bare Epic run (no Initiative loop above it) still needs its phase-Tasks cut first.
    if (is_epic(epic_issue) and epic_issue["state"] == "OPEN" and not is_epic_standing(epic_issue)
            and not is_epic_architected(epic_issue) and not deferred_by_run_cap
            and epic_lacks_phase_tasks(all_issues, epic)):
        if at_cap:
            deferred_by_run_cap.append(epic)
        else:
            return {"action": "cut-phase-tasks", "epic": epic, "unit": "epic",
                    "reason": f"Epic #{epic} has no phase-Tasks: run `cut-phase-tasks {epic} "
                              f"--repo-path <p>`, then `next-action {epic}` again"}
    return none_result()


def _resume_liveness(gh: GitHub, epic: int, issue: int) -> dict:
    """Liveness hints for a `resume` action: `claimed_at` and `claim_age_seconds` from the unit's
    latest start comment, `claimed_by_run` when the run-state still records who handed it out, and
    `likely_live` when the claim is younger than `RESUME_LIVE_WINDOW_MINUTES` (another session may
    still be driving it). `claim_age_seconds: null` -- said honestly -- when no claim timestamp is
    derivable, never a guess. The `reason` tells the orchestrator what to do about it."""
    claimed_at = None
    try:
        comments = gh.issue_view(issue).get("comments", [])
    except GhError:
        comments = []
    for c in comments:  # latest start comment wins
        if "Picking this up" in c.get("body", "") and c.get("createdAt"):
            claimed_at = c["createdAt"]
    info = {"claimed_at": claimed_at, "claim_age_seconds": None, "likely_live": False}
    state = read_run_state(epic)
    if state and str(issue) in (state.get("in_flight") or {}):
        info["claimed_by_run"] = state.get("run_id")
    age = None
    if claimed_at:
        try:
            age = (datetime.now(timezone.utc) - _parse_marker_ts(claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = None
    window = RESUME_LIVE_WINDOW_MINUTES * 60
    if age is not None:
        info["claim_age_seconds"] = age
        info["likely_live"] = window > 0 and age < window
    if age is None:
        info["reason"] = ("no claim timestamp is derivable (claim_age_seconds: null) -- cannot "
                          "tell whether another session is driving this unit; if unsure, ask the "
                          "operator before resuming.")
    elif info["likely_live"]:
        info["reason"] = (f"claimed {int(age // 60)} min ago, inside the "
                          f"{RESUME_LIVE_WINDOW_MINUTES}-min live window -- another session may "
                          f"still be driving it. If this session is not the one driving it, skip "
                          f"it and ask the operator before taking it over.")
    else:
        info["reason"] = (f"claimed {int(age // 60)} min ago, past the "
                          f"{RESUME_LIVE_WINDOW_MINUTES}-min live window -- likely a crashed or "
                          f"abandoned run; safe to resume.")
    return info


# `next-action --sync-epic`: re-sync `epic-<n>` with `main` once this many units of the run
# reached a terminal state (sibling merges) since the last sync.
EPIC_SYNC_EVERY_MERGES = 3


def _origin_sha(repo_path: str, branch: str, runner: Runner) -> Optional[str]:
    """`origin/<branch>`'s sha (callers fetch first), or None when it does not exist."""
    try:
        return runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{branch}"]).strip() or None
    except GhError:
        return None


def sync_epic_if_due(gh: GitHub, epic: int, run_id: Optional[str], repo_path: str = ".",
                     runner: Runner = _default_runner, issues: Optional[list] = None) -> dict:
    """Keep a non-standing Epic's `epic-<n>` from rotting against `main`: `sync-branch <epic>
    --unit epic` on this run-id's first call, after `EPIC_SYNC_EVERY_MERGES` terminal units
    since the last sync, or when `origin/main` moved since then (run-state `epic_sync`).
    Skipped (`skipped`) without a run id, for anything but an open non-standing Epic with open
    children (at close `close-epic` reconciles), or before `epic-<n>` exists. A conflict is
    posted once per (`main`, epic head) pair and then reported `pending` until either moves.
    Never raises: an operational failure comes back as `error`."""
    if not run_id:
        return {"skipped": "no --run-id: the sync cadence is tracked per run"}
    try:
        issues = gh.issue_list() if issues is None else issues
        entry = next((i for i in issues if i["number"] == epic), None)
        if entry is not None and is_initiative(entry):
            return sync_initiative_if_due(gh, entry, run_id, repo_path, runner)
        if (entry is None or not is_epic(entry) or is_epic_standing(entry)
                or is_epic_legacy(entry) or entry["state"] != "OPEN"):
            return {"skipped": f"#{epic} is not an open non-standing Epic -- no epic branch "
                               f"to keep current"}
        if not any(i["state"] == "OPEN" and (i.get("parent") or {}).get("number") == epic
                   and not is_epic(i) for i in issues):
            return {"skipped": "no open children -- close-epic reconciles the branch at close"}
        branch = epic_branch(epic)
        # `main`, or the opted-in Initiative's branch the Epic closes into.
        base = epic_base(gh, epic, issues)
        _fetch_origin(repo_path, runner)
        epic_sha = _origin_sha(repo_path, branch, runner)
        if epic_sha is None:
            return {"skipped": f"origin/{branch} does not exist yet"}
        main_sha = _origin_sha(repo_path, base, runner)
        state = run_cap_state(epic, run_id)
        last = state.get("epic_sync") or {}
        terminal = len(state.get("terminal", []))
        if not last:
            due = "run-start"
        elif terminal - last.get("terminal", 0) >= EPIC_SYNC_EVERY_MERGES:
            due = "sibling-merges"
        elif main_sha != last.get("main"):
            due = "main-moved"
        elif last.get("conflict"):
            due = "conflict-retry"  # the epic head moved: check the resolution landed
        else:
            due = None
        if last.get("conflict") and last.get("main") == main_sha and last.get("epic") == epic_sha:
            # Unresolved and nothing moved: re-merging would only re-post the same conflict.
            return {"synced": False, "conflict": True, "pending": True,
                    "conflicting_files": last["conflict"], "branch": branch,
                    "reason": "the recorded epic-branch <- main conflict is still unresolved"}
        if due is None:
            return {"synced": False, "due": None, "branch": branch,
                    "reason": "not due: no new run, fewer than "
                              f"{EPIC_SYNC_EVERY_MERGES} merges since the last sync, "
                              f"{base} unmoved"}
        result = cmd_sync_branch(gh, repo_path, epic, "epic", runner=runner, base=base)
        if result.get("synced") and main_sha and epic_sha != main_sha:
            try:
                result.update(epic_sync_touches(repo_path, epic, epic_sha, main_sha, runner, gh))
            except GhError as e:
                # The sync already pushed; a failed report must not undo its record.
                result["touches_error"] = str(e)
        _fetch_origin(repo_path, runner)
        record = {"main": main_sha, "epic": _origin_sha(repo_path, branch, runner),
                  "terminal": terminal, "at": _utc_now_marker()}
        if result.get("conflict"):
            record["conflict"] = result.get("conflicting_files", [])
        state["epic_sync"] = record
        _write_run_state(epic, state)
        return {**result, "due": due}
    except GhError as e:
        return {"synced": False, "error": str(e)}


def epic_sync_touches(repo_path: str, epic: int, epic_sha: str, base_sha: str,
                      runner: Runner = _default_runner,
                      gh: Optional[WorkItemProvider] = None) -> dict:
    """What an epic sync brought in from its base (`epic_sha...base_sha`): `design_touch`, the
    files the Epic's `architecture.md`/`lld.md` name (the path or a parent dir of 3+ segments;
    a 2-segment app root is named everywhere);
    `schema_touch`, the files matching `pipeline.sync.schemaGlobs`. Empty lists when none."""
    delta = runner(["git", "-C", repo_path, "diff", "--name-only",
                    f"{epic_sha}...{base_sha}"]).split()
    docs = ""
    for name in ("architecture.md", "lld.md"):
        try:
            docs += runner(["git", "-C", repo_path, "show", f"{epic_sha}:"
                            f"{doc_root_for(epic, gh)}/epic-{epic}/{name}"])
        except GhError:
            pass

    def named(path):
        parts = path.split("/")
        return any("/".join(parts[:i]) in docs for i in range(len(parts), 2, -1))

    globs = PIPELINE["sync"].get("schemaGlobs") or []
    return {"design_touch": [p for p in delta if docs and named(p)],
            "schema_touch": [p for p in delta if any(_glob_matches(p, g) for g in globs)]}


def sync_initiative_if_due(gh: GitHub, entry: dict, run_id: str, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """`--sync-epic` on an Initiative: keep an opted-in (`branch: true`) Initiative's
    `initiative-<i>` current with `main` -- `sync-branch <i> --unit initiative` on this run's
    first call or when `origin/main` moved since the last sync (run-state `initiative_sync`).
    Skipped for an Initiative without the opt-in, a closed one, or before the branch exists
    (the first Epic's `cut-phase-tasks` creates it). A conflict is posted once per (`main`,
    initiative head) pair, then `pending`, as `sync_epic_if_due` does."""
    n = entry["number"]
    if not resolve_initiative_profile(entry)["branch"]:
        return {"skipped": f"Initiative #{n} has no initiative branch (no `initiativeProfiles` "
                           f"entry with `branch: true` matches it)"}
    if entry["state"] != "OPEN":
        return {"skipped": f"Initiative #{n} is closed"}
    branch = initiative_branch(n)
    _fetch_origin(repo_path, runner)
    head = _origin_sha(repo_path, branch, runner)
    if head is None:
        return {"skipped": f"origin/{branch} does not exist yet (the first Epic's "
                           f"cut-phase-tasks creates it)"}
    main_sha = _origin_sha(repo_path, "main", runner)
    state = run_cap_state(n, run_id)
    last = state.get("initiative_sync") or {}
    if last.get("conflict") and last.get("main") == main_sha and last.get("initiative") == head:
        return {"synced": False, "conflict": True, "pending": True, "branch": branch,
                "conflicting_files": last["conflict"],
                "reason": "the recorded initiative-branch <- main conflict is still unresolved"}
    due = ("run-start" if not last else "main-moved" if main_sha != last.get("main")
           else "conflict-retry" if last.get("conflict") else None)
    if due is None:
        return {"synced": False, "due": None, "branch": branch,
                "reason": "not due: this run already synced it and main is unmoved"}
    result = cmd_sync_branch(gh, repo_path, n, "initiative", runner=runner, base="main")
    _fetch_origin(repo_path, runner)
    record = {"main": main_sha, "initiative": _origin_sha(repo_path, branch, runner),
              "at": _utc_now_marker()}
    if result.get("conflict"):
        record["conflict"] = result.get("conflicting_files", [])
    state["initiative_sync"] = record
    _write_run_state(n, state)
    return {**result, "due": due}


def cmd_next_action(gh: GitHub, args) -> dict:
    """`next-action`: `decide_next_action` plus `cap_enforced` (true only with a
    `--run-id` and a nonzero cap), which describes the invocation, not the decision.
    A `resume` also carries liveness hints (`_resume_liveness`). With `--sync-epic`,
    `sync_epic_if_due` runs first and its result is `epic_sync`. Refuses a unit placed
    on the other side (`check_placement`), then a plugin older than the run's last
    (`plugin_version_guard`), before anything else runs."""
    run_id = getattr(args, "run_id", None)
    repo_path = getattr(args, "repo_path", None) or "."
    issues = gh.issue_list()
    by_number = {i["number"]: i for i in issues}
    scope = placement_scope(args.epic, by_number.get, issues)
    version = plugin_version_guard(gh, args.epic, by_number, repo_path=repo_path)
    drift = config_drift(args.epic, run_id)
    epic_sync = (sync_epic_if_due(gh, args.epic, run_id, repo_path, issues=issues)
                 if getattr(args, "sync_epic", False) else None)
    result = decide_next_action(gh, args.epic, run_id=run_id,
                                skip_epics=getattr(args, "skip_epic", None),
                                repo_path=repo_path, all_issues=issues, scope=scope)
    # Read liveness before note_in_flight re-stamps this run onto the unit.
    liveness = (_resume_liveness(gh, args.epic, result["issue"])
                if result.get("action") == "resume" else {})
    if "issue" in result:
        note_in_flight(args.epic, run_id,
                       {result["issue"]: result.get("stage") or result["action"]})
    hint_doc_roots(gh, args.epic, result.get("issue"))
    if PIPELINE["initiativeProfiles"] and result.get("issue") is not None:
        try:  # records the unit's Initiative profile for the SubagentStart hook
            unit_initiative_profile(gh, result["issue"])
        except GhError:
            forget_initiative_hint(result["issue"])
    return {**result, **liveness, **version,
            **({"placement_scope": scope} if scope else {}),
            "cap_enforced": bool(run_id) and MAX_TASKS_PER_RUN > 0,
            **({"epic_sync": epic_sync} if epic_sync is not None else {}),
            **({"config_changed": drift} if drift else {}),
            **({"config_errors": local_tasks_config_errors()}
               if local_tasks_config_errors() else {})}


def cmd_list_ready_for_review(gh: GitHub, epic: int, limit: Optional[int] = None) -> dict:
    """Up to `limit` children of `epic` with an open draft PR and no review outcome
    since the latest development->pr-review handoff (so each rework round re-qualifies).
    Read-only. Returns `ready_for_review`, `count`, `eligible_total`, `skipped`."""
    limit = PR_REVIEW_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    scope = placement_scope(epic, {i["number"]: i for i in all_issues}.get, all_issues)
    open_issues = [i for i in all_issues if i["state"] == "OPEN" and in_scope(i, scope)]

    ready, skipped = [], []
    at_review_entry = [i for i in open_issues if not is_epic(i)
                       and current_stage(i) in REVIEW_ENTRY_STAGES
                       and i.get("parent") and i["parent"]["number"] == epic]
    for issue in sorted(at_review_entry, key=sort_key):
        number = issue["number"]
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        comments = gh.issue_view(number).get("comments", [])
        handoff = last_transition_to(comments, "pr-review")
        if handoff is None:
            skipped.append({"issue": number, "reason": "no development->pr-review handoff marker "
                                                        "yet -- development hasn't handed this "
                                                        "issue on"})
            continue
        outcome = last_pr_review_outcome(comments)
        if outcome is not None and outcome[0] > handoff:
            skipped.append({"issue": number, "reason": f"pr-review already recorded "
                                                        f"{outcome[1]!r} for this round"})
            continue
        if routed_past_pr_review(comments):
            skipped.append({"issue": number, "reason": "routed past pr-review this round "
                                                        "(`route --to merge`)"})
            continue
        branch = issue_branch(number)
        prs = gh.pr_list_for_branch(branch)
        if not prs:
            skipped.append({"issue": number, "reason": f"no open PR on {branch}"})
            continue
        pr = prs[0]
        if not pr.get("isDraft"):
            skipped.append({"issue": number, "reason": f"PR #{pr['number']} is no longer a draft "
                                                        f"-- already reviewed and marked ready"})
            continue
        ready.append({"issue": number, "pr": pr["number"], "branch": branch,
                       "title": issue["title"], "pr_url": pr.get("url")})

    return {"ready_for_review": ready[:limit], "count": len(ready[:limit]),
            "eligible_total": len(ready), "limit": limit, "epic": epic, "skipped": skipped}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import footprint  # noqa: E402,F401
from .footprint import read_footprint  # noqa: E402
from .plugin_version import plugin_version_guard  # noqa: E402
from .worktrees import _fetch_origin  # noqa: E402
from .lanes import (  # noqa: E402
    DEV_LANE_START_STAGES, dev_lane_footprints, dev_lane_refusal, dev_lane_slots,
)
from .design_gates import DESIGN_STAGE_REVIEW, cmd_sync_branch, find_design_pr  # noqa: E402
from .issue_ops import _classifier_denied_resume  # noqa: E402
from .cloud import (  # noqa: E402
    _cloud_epics_reason, _initiative_epics, cloud_epic_survey, initiative_epics_placement,
)
