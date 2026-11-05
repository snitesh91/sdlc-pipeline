"""Dev and design lanes: parallel-ready units and footprint collisions."""
from __future__ import annotations

from typing import Optional

from .config import (
    CROSS_EPIC_FOOTPRINT_CHECK, DESIGN_LANE_PARALLELISM, DEV_LANE_PARALLELISM,
    GATE_PENDING_STATUSES, MAX_TASKS_PER_RUN, PRODUCT_WIP_CAP, Runner, issue_branch,
    issue_number_from_branch,
)
from .github import (
    GhError, GitHub, WorkItemProvider, _default_runner, focus_issues, with_scan,
)
from .units import (
    current_stage, effective_gates, in_scope, is_epic, is_epic_architected, is_epic_legacy,
    is_epic_standing, pipeline_status, placement_scope, product_gate_pending,
    product_wip_headroom, resolve_profile, sort_key,
)
from .run_state import note_in_flight, run_cap_reached, run_cap_state, run_completed
from .footprint import footprint_overlaps, read_footprint
from .worktrees import _fetch_origin, active_worktree_branches


# Stages a fresh unit enters the dev lane at; a unit past them (`pr-review`) has started.
DEV_LANE_START_STAGES = ("development", "testing")


def _dev_lane_parked(gh: GitHub, issue: dict) -> Optional[str]:
    """Why an open unit holds no dev-lane slot (parked or blocked), else None."""
    status = pipeline_status(issue)
    if status == "needs-human" or status in GATE_PENDING_STATUSES:
        return f"Pipeline Status is {status!r}"
    if gh.blocked_by(issue["number"]):
        return "blocked by an open dependency"
    return None


def dev_lane_slots(gh: GitHub, repo_path: str, epic: int, all_issues: list,
                   runner: Runner = _default_runner, cap: Optional[int] = None) -> dict:
    """The dev lane's slot arithmetic, one definition for `next-action` and
    `list-parallel-ready`. A slot is held by an open child of `epic` that has started -- a live
    worktree on its branch, Pipeline Status `in-progress`, or Stage `pr-review` -- and is not
    parked or blocked. Other epics' worktrees hold no slot: they are `excluded_other_epic`, or
    `cross_epic` (overlap check only) under `parallelism.crossEpicFootprintCheck`."""
    cap = DEV_LANE_PARALLELISM if cap is None else cap
    active = active_worktree_branches(repo_path, runner=runner)
    by_number = {i["number"]: i for i in all_issues}
    held = {issue_number_from_branch(b) for b in active} - {None}
    if held - by_number.keys():  # other Epics' worktrees: read those units too
        by_number = {i["number"]: i for i in focus_issues(gh, held)}
    holders, stale, other = {}, [], []
    for branch in sorted(active):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        if (issue.get("parent") or {}).get("number") != epic:
            other.append(branch)
            continue
        parked = _dev_lane_parked(gh, issue)
        if parked:
            stale.append({"branch": branch, "reason": parked})
            continue
        holders[number] = {"issue": number, "branch": branch, "worktree": True}
    for issue in all_issues:
        number = issue["number"]
        if (number in holders or issue["state"] != "OPEN" or is_epic(issue)
                or (issue.get("parent") or {}).get("number") != epic):
            continue
        if pipeline_status(issue) != "in-progress" and current_stage(issue) != "pr-review":
            continue
        if _dev_lane_parked(gh, issue) is None:
            holders[number] = {"issue": number, "branch": issue_branch(number), "worktree": False}
    occupied = sorted(holders)
    return {"cap": cap, "occupied": occupied, "free": max(0, cap - len(occupied)),
            "holders": [holders[n] for n in occupied], "stale_worktrees": stale,
            "excluded_other_epic": [] if CROSS_EPIC_FOOTPRINT_CHECK else other,
            "cross_epic": other if CROSS_EPIC_FOOTPRINT_CHECK else [],
            "active_branches": active}


def dev_lane_footprints(lane: dict, repo_path: str, by_number: dict,
                        runner: Runner = _default_runner,
                        gh: Optional[WorkItemProvider] = None) -> list:
    """`(issue, footprint)` for every slot holder and cross-epic branch whose footprint is
    readable on origin; an unreadable one is left out (it still holds its slot)."""
    numbers = [h["issue"] for h in lane["holders"]]
    numbers += [issue_number_from_branch(b) for b in lane["cross_epic"]]
    out = []
    for number in numbers:
        # Footprints live in the unit's own epic's lld.md, so use its parent, not the lane's epic.
        parent = (by_number.get(number) or {}).get("parent")
        fp = read_footprint(repo_path, number, runner=runner,
                            epic=parent["number"] if parent else None, gh=gh)
        if fp:
            out.append((number, fp))
    return out


def footprint_collision(footprint: Optional[list], others: list) -> Optional[str]:
    """The refusal when `footprint` cannot be shown disjoint from `others` (`(issue, fp)`
    pairs): None (no readable section) means unverifiable, `[]` (verify-only) owns nothing,
    an overlap names the unit. None when clear."""
    if footprint is None:
        return "no readable ## Footprint -- cannot verify non-overlap"
    hit = next((n for n, fp in others if footprint_overlaps(footprint, fp)), None)
    return f"footprint overlaps active/eligible #{hit}" if hit is not None else None


def dev_lane_refusal(lane: dict, number: int, footprint_of) -> Optional[str]:
    """Why a fresh unit cannot enter the lane now, else None: a holder is already admitted;
    otherwise it needs a free slot and, when anything holds one, a footprint disjoint from
    the holders' (`footprint_of(number)` is read only then)."""
    if number in lane["occupied"]:
        return None
    if lane["free"] <= 0:
        return (f"dev lane full: {len(lane['occupied'])}/{lane['cap']} slot(s) held by "
                f"{', '.join(f'#{n}' for n in lane['occupied'])}")
    if not lane.get("footprints"):
        return None
    return footprint_collision(footprint_of(number), lane["footprints"])


def cmd_list_parallel_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                             runner: Runner = _default_runner,
                             run_id: Optional[str] = None) -> dict:
    """Children of `epic` at development/testing that can start now: not active, parked,
    blocked or footprint-overlapping another (a missing footprint is never assumed safe).
    Returns `parallel_ready` (up to free slots), `skipped`, `stale_worktrees` and counts."""
    limit = DEV_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (pipeline.classification does not call it "
                       f"\"epic\") -- pass the epic's own issue number, not a child issue's")
    scope = placement_scope(epic, by_number.get, all_issues)
    cap_state = run_cap_state(epic, run_id)
    cap_enforced = bool(run_id) and MAX_TASKS_PER_RUN > 0
    base = {"parallel_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": [], "cap_enforced": cap_enforced}
    if run_cap_reached(cap_state):
        # The cap must close this lane too, or overflow just moves here from next-action.
        completed = run_completed(cap_state)
        in_flight = sorted(int(n) for n in cap_state.get("in_flight", {}) if str(n).isdigit())
        return {**base, "stop_at_cap": True, "cap": MAX_TASKS_PER_RUN, "completed": completed,
                "in_flight": in_flight,
                "note": f"run has driven {len(completed)} unit(s) to a terminal state, at "
                        f"the maxTasksPerRun cap of {MAX_TASKS_PER_RUN} -- no new work is "
                        f"handed out until a new run (a fresh --run-id) starts. Units "
                        f"already in flight finish through resume/pass-gate as normal."}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if resolve_profile(epic_issue)["childrenNeedArchitectedEpic"] \
            and not is_epic_architected(epic_issue):
        return {**base, "note": f"epic #{epic} is not epic:architected yet -- its children "
                                 f"are not eligible for the implementation lane"}
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]
    children = [i for i in open_issues if not is_epic(i) and i.get("parent")
                and i["parent"]["number"] == epic and in_scope(i, scope)]

    _fetch_origin(repo_path, runner)
    lane = dev_lane_slots(gh, repo_path, epic, all_issues, runner=runner, cap=limit)
    active_branches = lane["active_branches"]

    eligible, skipped = [], []
    selected_footprints = dev_lane_footprints(lane, repo_path, by_number, runner=runner, gh=gh)
    for issue in sorted(children, key=sort_key):
        number = issue["number"]
        branch = issue_branch(number)
        if branch in active_branches:
            skipped.append({"issue": number, "reason": "already active in its own worktree"})
            continue
        stage = current_stage(issue)
        if stage not in ("development", "testing"):
            skipped.append({"issue": number, "reason": f"stage is {stage!r}, not development/testing"})
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        if status == "in-progress":
            skipped.append({"issue": number, "reason": "Pipeline Status is 'in-progress' but this "
                                                         "branch has no active worktree -- likely a "
                                                         "crashed run; resume it via next-action, "
                                                         "don't also start it here"})
            continue
        if gh.blocked_by(number):
            skipped.append({"issue": number, "reason": "blocked by an open dependency"})
            continue
        footprint = read_footprint(repo_path, number, runner=runner, epic=epic, gh=gh)
        if footprint is None:
            skipped.append({"issue": number, "reason": "no ## Footprint section found in its own "
                                                         "architecture.md, or under a "
                                                         f"`## Task #{number}` heading in its Epic's "
                                                         "epic-<n>/lld.md, on origin -- "
                                                         "cannot verify non-overlap"})
            continue
        collision = footprint_collision(footprint, selected_footprints)
        if collision is not None:
            skipped.append({"issue": number, "reason": collision})
            continue
        eligible.append({"issue": number, "branch": branch, "stage": stage, "title": issue["title"]})
        selected_footprints.append((number, footprint))

    # A holder keeps its slot even when its footprint was unreadable.
    active_count = len(lane["occupied"])
    slots = lane["free"]
    selected = eligible[:slots]
    note_in_flight(epic, run_id, {u["issue"]: u["stage"] for u in selected})
    return {"parallel_ready": selected, "count": len(selected), "eligible_total": len(eligible),
            "active_count": active_count,
            "active_branches": sorted(h["branch"] for h in lane["holders"]),
            "limit": limit, "slots_available": slots,
            "slots": {"cap": limit, "occupied": lane["occupied"], "free": slots,
                      "excluded_other_epic": lane["excluded_other_epic"]},
            "epic": epic, "skipped": skipped, "stale_worktrees": lane["stale_worktrees"],
            "cap_enforced": cap_enforced, "stop_at_cap": False}


def cmd_list_design_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                           runner: Runner = _default_runner) -> dict:
    """Children of a standing `epic` at `product`/`architecture` that can start now,
    each in its own worktree, up to the design-lane cap. Read-only apart from a fetch.
    Returns `design_ready`, `skipped`, `stale_worktrees`, `product_cap` and slot counts."""
    limit = DESIGN_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (pipeline.classification does not call it "
                       f"\"epic\") -- pass the epic's own issue number, not a child issue's")
    scope = placement_scope(epic, by_number.get, all_issues)
    # An auto-passing Gate A never queues on the human, so it is exempt from the WIP cap.
    gates = with_scan(gh, all_issues, "gate")  # the Gate A queue is repo-wide
    product_headroom = (product_wip_headroom(gates)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    product_cap = {"limit": PRODUCT_WIP_CAP, "pending": product_gate_pending(gates)}
    base = {"design_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": [], "stale_worktrees": [], "product_cap": product_cap}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if not is_epic_standing(epic_issue):
        return {**base, "note": f"epic #{epic} is not a standing epic -- its design work is "
                                 f"its Architecture-phase and LLD-phase Tasks, driven one at a "
                                 f"time by next-action, not fanned out across children"}
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]
    children = [i for i in open_issues if not is_epic(i) and i.get("parent")
                and i["parent"]["number"] == epic and in_scope(i, scope)]

    _fetch_origin(repo_path, runner)
    active_branches = active_worktree_branches(repo_path, runner=runner)

    eligible, skipped = [], []
    for issue in sorted(children, key=sort_key):
        number = issue["number"]
        branch = issue_branch(number)
        if branch in active_branches:
            skipped.append({"issue": number, "reason": "already active in its own worktree"})
            continue
        stage = current_stage(issue)
        if stage is None:
            skipped.append({"issue": number, "reason": "no Stage yet -- `route` picks its first stage"})
            continue
        if stage not in ("product", "architecture"):
            skipped.append({"issue": number, "reason": f"stage is {stage!r}, not product/architecture"})
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        if status == "in-progress":
            skipped.append({"issue": number, "reason": "Pipeline Status is 'in-progress' but this "
                                                         "branch has no active worktree -- likely a "
                                                         "crashed run; resume it via next-action, "
                                                         "don't also start it here"})
            continue
        if gh.blocked_by(number):
            skipped.append({"issue": number, "reason": "blocked by an open dependency"})
            continue
        if stage == "product" and product_headroom is not None:
            if product_headroom <= 0:
                skipped.append({"issue": number,
                                "reason": f"product WIP cap: {len(product_cap['pending'])} unit(s) "
                                          f"already awaiting Gate A review (cap "
                                          f"{PRODUCT_WIP_CAP}) -- not starting a fresh product "
                                          f"stage until one passes"})
                continue
            product_headroom -= 1
        eligible.append({"issue": number, "branch": branch, "stage": stage, "title": issue["title"]})

    # Only design-stage worktrees occupy a design slot; parked or closed ones are stale.
    occupied, stale = [], []
    for branch in sorted(active_branches):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        stage = current_stage(issue)
        if stage not in ("product", "architecture"):
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            stale.append({"branch": branch, "reason": f"Pipeline Status is {status!r}"})
            continue
        if gh.blocked_by(number):
            stale.append({"branch": branch, "reason": "blocked by an open dependency"})
            continue
        occupied.append(branch)
    active_count = len(occupied)
    slots = max(0, limit - active_count)
    selected = eligible[:slots]
    return {"design_ready": selected, "count": len(selected), "eligible_total": len(eligible),
            "active_count": active_count, "active_branches": sorted(occupied),
            "limit": limit, "slots_available": slots, "epic": epic, "skipped": skipped,
            "stale_worktrees": stale, "product_cap": product_cap}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import handoff  # noqa: E402,F401
