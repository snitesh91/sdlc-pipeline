"""Issue creation, fields, repair, audits and closeable-epic checks."""
from __future__ import annotations

import re
from typing import Optional

from .config import (
    EFFORT_FIELD_ID, EFFORT_OPTION_IDS, ESCALATION, HUMAN_ASSIGNEE, ISSUE_TYPE_IDS,
    KNOWN_ISSUE_TYPES, PIPELINE, PRIORITY_ALIASES, PRIORITY_FIELD_ID, PRIORITY_OPTION_IDS, REPO,
    Runner,
)
from .github import GhError, GitHub, GitHubRest, WorkItemProvider, _default_runner
from .units import (
    classify_unit_from_issue, doc_root_for, epic_base, is_epic, is_epic_legacy,
    is_epic_standing, is_initiative, issue_type, label_names, pipeline_status,
)
from .evidence import (
    _DESIGN_REVIEW_OUTCOME_MARKER, _NEEDS_HUMAN_REASON, _PR_REVIEW_OUTCOME_MARKER,
    _SYNC_CONFLICT_MARKER, epic_branch,
)
from .next_action import epic_lacks_phase_tasks


def cmd_reparent_issue(gh: WorkItemProvider, issue: int, parent: int,
                       reason: Optional[str] = None) -> dict:
    """Move a plain issue (Task, Bug, ...) under `parent`: drop its current sub-issue link (if
    any), link it under `parent`, and comment on the issue, the old parent and the new parent.
    Idempotent (`already_parented`) when it is already under `parent`. Refuses (exit 0) an
    Epic or Initiative -- `detach-epic` handles an Epic. Returns `{issue, old_parent,
    new_parent, moved}`."""
    info = gh.issue_epic_info(issue)
    kind = classify_unit_from_issue(info)
    old = (info.get("parent") or {}).get("number")
    base = {"issue": issue, "old_parent": old, "new_parent": parent}
    if kind in ("epic", "initiative"):
        return {**base, "moved": False, "refused": True,
                "reason": f"#{issue} is an {kind.title()} -- reparent-issue moves plain issues "
                          f"only. Take an Epic out of its Initiative with `detach-epic {issue}`"}
    if parent == issue:
        return {**base, "moved": False, "refused": True,
                "reason": f"#{issue} cannot be its own parent"}
    if old == parent:
        return {**base, "moved": False, "already_parented": True}
    if old is not None:
        gh.remove_sub_issue(old, issue)
    gh.add_sub_issue(parent, issue)
    why = f" — {reason}" if reason else ""
    moved_from = f"from #{old} " if old is not None else ""
    gh.issue_comment(issue, f"🔀 Moved {moved_from}to #{parent}{why}.")
    if old is not None:
        gh.issue_comment(old, f"🔀 #{issue} moved out of this issue to #{parent}{why}.")
    gh.issue_comment(parent, f"🔀 #{issue} moved here{' from #' + str(old) if old else ''}{why}.")
    return {**base, "moved": True}


def cmd_pairing_counts(gh: GitHub, issue: int) -> dict:
    """Escalation-valve bounce counts read back from comment markers: pr-review rework,
    sync conflicts, and per-role `design_review` counts, each with same-class recurrences.
    Any same-class recurrence >= 1 is an escalation signal on its own."""
    comments = gh.issue_view(issue).get("comments", [])
    rework_since_clean = total_rework = total_clean = sync_conflicts = 0
    pr_review_same_class = 0
    design_review: dict = {}
    for c in comments:
        body = c.get("body", "")
        m = _PR_REVIEW_OUTCOME_MARKER.search(body)
        if m:
            if m.group(1) == "rework":
                total_rework += 1
                rework_since_clean += 1
                if m.group(3) == "true":
                    pr_review_same_class += 1
            else:
                total_clean += 1
                rework_since_clean = 0
        if _SYNC_CONFLICT_MARKER.search(body):
            sync_conflicts += 1
        d = _DESIGN_REVIEW_OUTCOME_MARKER.search(body)
        if d:
            outcome, role = d.group(1), d.group(2)
            counts = design_review.setdefault(
                role, {"rework_since_last_clean": 0, "total_rework": 0, "total_clean": 0,
                       "same_class_recurrence_count": 0})
            if outcome == "rework":
                counts["total_rework"] += 1
                counts["rework_since_last_clean"] += 1
                if d.group(3) == "true":
                    counts["same_class_recurrence_count"] += 1
            else:
                counts["total_clean"] += 1
                counts["rework_since_last_clean"] = 0
    return {"issue": issue,
            "thresholds": {"replace_at": ESCALATION["replaceAt"],
                           "needs_human_at": ESCALATION["needsHumanAt"]},
            "pr_review_rework_since_last_clean": rework_since_clean,
            "pr_review_total_rework": total_rework,
            "pr_review_total_clean": total_clean,
            "pr_review_same_class_recurrence_count": pr_review_same_class,
            "sync_conflict_count": sync_conflicts,
            # Per role: each review role is a separate valve pairing; absent = never ran.
            "design_review": design_review}


def _issue_kind(type_name: str) -> str:
    """The classification kind whose issueType rule names `type_name`, else its lowercase."""
    for kind, rule in PIPELINE.get("classification", {}).items():
        if rule.get("field") == "issueType" and rule.get("value") == type_name:
            return kind
    return type_name.lower()


def _option_name(field: str, value, options: dict, aliases: Optional[dict] = None) -> str:
    """`value` (or an `aliases` entry for it) matched case-insensitively to one of `options`'
    names; GhError listing the valid values if none."""
    if aliases and value is not None and str(value).strip().lower() in aliases:
        value = aliases[str(value).strip().lower()]
    for name in options:
        if value is not None and name.lower() == str(value).strip().lower():
            return name
    valid = ", ".join(sorted(options))
    extra = f" (aliases: {', '.join(sorted(aliases))})" if aliases else ""
    raise GhError(f"{field} {value!r} is not a configured option -- valid values: {valid}{extra}. "
                  f"Refused before creating anything.")


def issue_field_values(priority: Optional[str] = None, effort: Optional[str] = None) -> dict:
    """Validated `{"priority", "effort"}` for `create-issue`, each only when its field is
    configured; an omitted value takes `pipeline.issueDefaults`."""
    defaults = PIPELINE["issueDefaults"]
    out = {}
    if PRIORITY_FIELD_ID:
        out["priority"] = _option_name("Priority", priority or defaults.get("priority"),
                                       PRIORITY_OPTION_IDS, aliases=PRIORITY_ALIASES)
    if EFFORT_FIELD_ID:
        out["effort"] = _option_name("Effort", effort or defaults.get("effort"), EFFORT_OPTION_IDS)
    return out


def cmd_create_issue(gh: GitHub, title: str, body: str, parent: int, labels: list,
                     type_name: str = "Task", priority: Optional[str] = None,
                     effort: Optional[str] = None, blocked_by: Optional[list] = None) -> dict:
    """Create an issue with its native Issue Type (mandatory), parent link, Pipeline Status
    `todo`, Priority/Effort when configured, and a native `blockedBy` edge on each of
    `blocked_by` (how Epics are ordered). Inputs are validated before creating; a later step
    failing returns `ok: False` naming the created issue."""
    known = sorted({*KNOWN_ISSUE_TYPES, *ISSUE_TYPE_IDS})
    if type_name not in known:
        raise GhError(f"--type {type_name!r} is not an issue type (one of {known})")
    if type_name not in ISSUE_TYPE_IDS:
        raise GhError(
            f"Issue Type {type_name!r} is not provisioned: create it under the org's Settings "
            f"-> Issue types and add its GraphQL id to projectFields.issueTypeIds (configured: "
            f"{sorted(ISSUE_TYPE_IDS)}). Refused before creating anything -- every pipeline "
            f"issue carries a native type.")
    fields = issue_field_values(priority, effort)
    rule = PIPELINE.get("classification", {}).get(_issue_kind(type_name)) or {}
    if rule.get("field") == "label" and rule["value"] not in labels:
        labels = [*labels, rule["value"]]
    # Parent first: re-runs of cut-phase-tasks / create-lld-tasks find a half-made
    # issue only through its parent link, so any later failure must not duplicate it.
    steps = [("add_sub_issue", lambda n: gh.add_sub_issue(parent, n)),
             ("set_issue_type", lambda n: gh.set_issue_type(n, type_name)),
             ("set_pipeline_status", lambda n: gh.set_pipeline_status_field(n, "todo"))]
    if "priority" in fields:
        steps.append(("set_priority", lambda n: gh.set_priority_field(n, fields["priority"])))
    if "effort" in fields:
        steps.append(("set_effort", lambda n: gh.set_effort_field(n, fields["effort"])))
    for dep in blocked_by or []:
        steps.append((f"add_blocked_by:{dep}", lambda n, dep=dep: _add_blocked_by_once(gh, n, dep)))
    # Refuse before creating at the parent's cap: a created-but-unlinked issue is invisible to
    # the parent-link lookups re-runs use, so each retry would duplicate it.
    total_of = getattr(gh, "sub_issue_total", None)
    if total_of and total_of(parent) >= SUB_ISSUE_CAP:
        raise GhError(f"#{parent} already has {SUB_ISSUE_CAP} sub-issues (GitHub's cap, closed "
                      f"children count): nothing was created. Unlink closed sub-issues from "
                      f"#{parent}, or rotate to a new parent (references/epics.md, \"The "
                      f"100-sub-issue cap\"), then re-run.")
    number = gh.issue_create(title, body, labels)
    result = {"issue": number, "parent": parent, "type": type_name,
              "pipeline_status": "todo", **fields,
              **({"blocked_by": list(blocked_by)} if blocked_by else {})}
    # The issue now exists: report failures with its number rather than raising,
    # so the caller repairs it instead of retrying into a duplicate.
    capped = None
    for i, (name, step) in enumerate(steps):
        try:
            step(number)
        except GhError as e:
            if name == "add_sub_issue" and _SUB_ISSUE_CAP_RE.search(str(e)):
                capped = str(e)  # retrying cannot link it; finish the fields unlinked
                continue
            todo = ", ".join(n for n, _ in steps[i:])
            edges = "".join(f" add-blocked-by {number} --on {dep};" for dep in blocked_by or []
                            if f"add_blocked_by:{dep}" in [n for n, _ in steps[i:]])
            return {**result, "ok": False, "complete": False, "failed_step": name,
                    "error": str(e),
                    "reason": f"issue #{number} was created but {name} failed ({todo} not "
                              f"done). Do NOT re-run create-issue -- that duplicates it. Run "
                              f"`repair-issue {number} --parent {parent} --type {type_name}"
                              + "".join(f" --{k} {v}" for k, v in fields.items())
                              + "` and" + (f" then{edges}" if edges else "") + " continue."}
    if capped:
        return {**result, "ok": False, "complete": False, "linked": False,
                "failed_step": "add_sub_issue", "reason_code": "sub_issue_cap",
                "error": capped, "reason": sub_issue_cap_advice(number, parent)}
    return result


# GitHub caps a parent at 100 sub-issues, closed ones included.
SUB_ISSUE_CAP = 100
_SUB_ISSUE_CAP_RE = re.compile(r"cannot have more than \d+ sub-issues", re.IGNORECASE)


def sub_issue_cap_advice(number: int, parent: int) -> str:
    return (f"#{number} exists with its fields set but is NOT linked under #{parent}: #{parent} "
            f"is at GitHub's 100-sub-issue cap (closed children count). Do NOT re-run "
            f"create-issue or repair-issue --parent {parent} -- both hit the same cap. Unlink "
            f"closed sub-issues from #{parent}, or rotate to a new parent (references/epics.md, "
            f"\"The 100-sub-issue cap\"), then `repair-issue {number} --parent <parent>`.")


def cmd_list_needs_human(gh: GitHub) -> dict:
    """Open needs-human issues with the reason from their latest mark-needs-human
    comment: `{"needs_human": [{issue, title, reason}]}`."""
    issues = [i for i in gh.issue_list() if i["state"] == "OPEN" and pipeline_status(i) == "needs-human"]
    result = []
    for issue in issues:
        detail = gh.issue_view(issue["number"])
        reason = None
        for c in reversed(detail.get("comments", [])):
            m = _NEEDS_HUMAN_REASON.search(c.get("body", ""))
            if m:
                reason = m.group(1).strip()
                break
        result.append({"issue": issue["number"], "title": issue["title"], "reason": reason})
    return {"needs_human": result}


_KIND_TYPES = {"initiative": "Initiative", "epic": "Epic", "task": "Task"}


def inferred_issue_type(issue: dict) -> Optional[str]:
    """The native type an untyped issue's classification labels name unambiguously, else None."""
    labels = label_names(issue)
    kinds = {kind for kind, rule in PIPELINE.get("classification", {}).items()
             if rule.get("field") == "label" and rule.get("value") in labels}
    return _KIND_TYPES.get(kinds.pop()) if len(kinds) == 1 else None


def cmd_show_issue_fields(runner: Runner = _default_runner) -> list:
    """`GET orgs/{owner}/issue-fields` trimmed to what `GitHubRest` reads, for pasting into
    `projectFields.issueFieldsRest` (a cloud session cannot read org endpoints)."""
    owner = REPO.split("/")[0]
    fields = GitHubRest(runner=runner)._api_items(f"orgs/{owner}/issue-fields")
    return [{"id": f["id"], "node_id": f["node_id"], "name": f["name"],
             "options": [{"id": o["id"], "name": o["name"]} for o in f.get("options") or []]}
            for f in fields]


def cmd_repair_issue(gh: WorkItemProvider, number: int, parent: Optional[int] = None,
                     type_name: Optional[str] = None, priority: Optional[str] = None,
                     effort: Optional[str] = None) -> dict:
    """Set whatever of create-issue's fields `number` lacks, never overwriting a set value --
    except that an explicit `--priority`/`--effort` is set even over an existing value (so a
    half-created issue can be corrected to the requested Priority/Effort). Parent and type keep
    never-overwrite unless a flag is passed. Validates before the first write, so a re-run
    finishes a partial repair."""
    info, fields = gh.issue_epic_info(number), gh.issue_fields(number)
    steps, already = [], []
    if info.get("parent"):
        already.append("parent")
    elif parent is not None:
        steps.append(("parent", lambda: gh.add_sub_issue(parent, number)))
    if issue_type(info):
        already.append("issueType")
    else:
        type_name = type_name or inferred_issue_type(info)
        if type_name is None:
            raise GhError(f"#{number} has no issueType and its classification names none "
                          f"unambiguously -- pass --type (one of {sorted(ISSUE_TYPE_IDS)})")
        if type_name not in ISSUE_TYPE_IDS:
            raise GhError(f"--type {type_name!r} is not in projectFields.issueTypeIds "
                          f"({sorted(ISSUE_TYPE_IDS)})")
        info = {**info, "issueType": {"name": type_name}}
        steps.append(("issueType", lambda: gh.set_issue_type(number, type_name)))
    if fields.get("Pipeline Status"):
        already.append("Pipeline Status")
    elif not (is_initiative(info) or is_epic(info)):  # cleared by design on containers
        steps.append(("Pipeline Status", lambda: gh.set_pipeline_status_field(number, "todo")))
    values = issue_field_values(priority, effort)
    for name, setter, passed in (("Priority", gh.set_priority_field, priority is not None),
                                 ("Effort", gh.set_effort_field, effort is not None)):
        value = values.get(name.lower())
        if value is None:
            continue  # field not configured
        if passed:  # an explicit flag overrides even a value already set
            steps.append((name, lambda setter=setter, value=value: setter(number, value)))
        elif fields.get(name):
            already.append(name)
        else:  # fill the missing field with its default, never overwriting
            steps.append((name, lambda setter=setter, value=value: setter(number, value)))
    capped = None
    for name, step in steps:
        try:
            step()
        except GhError as e:
            if name != "parent" or not _SUB_ISSUE_CAP_RE.search(str(e)):
                raise
            capped = str(e)
    done = [name for name, _ in steps if not (capped and name == "parent")]
    if capped:
        return {"issue": number, "ok": False, "linked": False, "failed_step": "parent",
                "reason_code": "sub_issue_cap", "error": capped,
                "reason": sub_issue_cap_advice(number, parent), "set": done,
                "already_set": already}
    return {"issue": number, "set": done, "already_set": already}


def cmd_audit_issues(gh: WorkItemProvider, epic: Optional[int] = None) -> dict:
    """Read-only: open issues in the Initiative/Epic trees (plus parentless ones), or only
    `epic`'s (or an Initiative's) subtree when given, missing issueType, parent (Tasks and other
    non-containers), Priority, Effort or Pipeline Status, each with a `repair-issue` command;
    and open non-standing Epics with no phase-Tasks, with the `cut-phase-tasks` command.
    Children come first; an Epic's/Initiative's own gaps (`container: true`) come last, so a
    scoped audit never leads with the container's Priority/Effort."""
    issues = gh.issue_list()
    by_number = {i["number"]: i for i in issues}
    children: dict = {}
    for i in issues:
        if i.get("parent"):
            children.setdefault(i["parent"]["number"], []).append(i["number"])
    stack = [epic] if epic is not None else [
        i["number"] for i in issues if is_initiative(i) or is_epic(i)]
    scope = set() if epic is not None else {i["number"] for i in issues if not i.get("parent")}
    seen: set = set()
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(children.get(n, []))
    optional = [(name, fid) for name, fid in (("Priority", PRIORITY_FIELD_ID),
                                              ("Effort", EFFORT_FIELD_ID)) if fid]
    found = []
    for n in sorted(scope | seen):
        i = by_number.get(n)
        if not i or i["state"] != "OPEN":
            continue
        container = is_initiative(i) or is_epic(i)
        missing = [] if issue_type(i) else ["issueType"]
        # Engineering-driven Epics are parentless by design.
        if not i.get("parent") and not container:
            missing.append("parent")
        missing += [name for name, _ in optional if not i.get("fields", {}).get(name)]
        # An Epic/Initiative's Pipeline Status is cleared while its Tasks carry the work.
        if pipeline_status(i) is None and not container:
            missing.append("Pipeline Status")
        repairs = []
        if missing:
            repair = f"repair-issue {n}" + (" --parent <P>" if "parent" in missing else "")
            if "issueType" in missing and not inferred_issue_type(i):
                repair += " --type <T>"
            repairs.append(repair)
        if (is_epic(i) and not is_epic_legacy(i) and not is_epic_standing(i)
                and epic_lacks_phase_tasks(issues, n)):
            missing = [*missing, "phase-Tasks"]
            repairs.append(f"cut-phase-tasks {n} --repo-path <p>")
        if missing:
            found.append({"issue": n, "title": i["title"], "missing": missing,
                          "repair": "; ".join(repairs), **({"container": True} if container else {})})
    found.sort(key=lambda f: (f.get("container", False), f["issue"]))
    return {"issues": found, "count": len(found)}


_EPIC_CLOSEABLE_MARKER = "<!-- epic-closeable-checklist-posted -->"


def cmd_check_epics_closeable(gh: GitHub) -> dict:
    """For each open non-standing epic with every child closed, post a one-time
    closing checklist and assign the human; never closes the epic. Idempotent via
    `_EPIC_CLOSEABLE_MARKER`. Returns `closeable_epics`, and `needs_phase_tasks`: Epics whose
    children are all closed but that never had both phase-Tasks and are not architected (closed
    superseded children only, or an Epic that predates phase-Tasks) -- listed, not notified, so
    the orchestrator decides: `cut-phase-tasks`, or close it by hand."""
    all_issues = gh.issue_list()
    open_epics = [i for i in all_issues if i["state"] == "OPEN" and is_epic(i) and not is_epic_standing(i)]
    results, needs_phase_tasks = [], []
    for epic in open_epics:
        children = [i for i in all_issues if i.get("parent") and i["parent"]["number"] == epic["number"]]
        if not children or any(c["state"] != "CLOSED" for c in children):
            continue
        # Closed superseded children are not a finished Epic: it still owes its phase-Tasks.
        if epic_lacks_phase_tasks(all_issues, epic["number"]):
            needs_phase_tasks.append(epic["number"])
            continue
        detail = gh.issue_view(epic["number"])
        already_posted = any(_EPIC_CLOSEABLE_MARKER in c.get("body", "") for c in detail.get("comments", []))
        if already_posted:
            results.append({"epic": epic["number"], "title": epic["title"], "already_notified": True})
            continue
        open_dependents = sorted({d for c in children for d in gh.blocking(c["number"])})
        # `main`, or the opted-in Initiative's branch this epic closes into.
        target = epic_base(gh, epic["number"], all_issues)
        # An open epic's docs live on its epic branch; `close-epic` carries them to main.
        branch = epic_branch(epic["number"])
        doc_names = ("architecture.md", "lld.md")
        epic_docs = f"{doc_root_for(epic['number'], gh)}/epic-{epic['number']}"
        missing_docs = [f"{epic_docs}/{name}" for name in doc_names
                        if not gh.path_on_ref(f"{epic_docs}/{name}", branch)]
        docs_line = (
            f"- [ ] {len(missing_docs)} epic doc(s) never reached `{branch}` "
            f"({', '.join(f'`{d}`' for d in missing_docs)}) — merge their design PRs before "
            "closing, or they stay reachable only on an unmerged phase-Task branch and never "
            "reach `main`\n"
            if missing_docs else
            f"- [x] This epic's `{doc_names[0]}` and `{doc_names[1]}` are both on `{branch}` "
            "(auto-verified) — `close-epic`'s merge carries them to `main`\n"
        )
        dependents_line = (
            f"- [ ] {len(open_dependents)} still-open issue(s) reference/depend on a closed child of this epic "
            f"({', '.join(f'#{n}' for n in open_dependents)}) — resolve or consciously accept before closing\n"
            if open_dependents else
            "- [x] No open issue elsewhere depends on a closed child of this epic (auto-verified)\n"
        )
        checklist = (
            f"## 🏁 Epic ready to close — all {len(children)} child issue(s) closed\n\n"
            f"- [x] All child issues closed (auto-verified: {len(children)}/{len(children)})\n"
            f"{dependents_line}"
            f"{docs_line}"
            f"- [ ] Closing verification run on `{epic_branch(epic['number'])}` after merging "
            f"`origin/{target}` into it — an exploratory pass\n"
            f"- [ ] `{epic_branch(epic['number'])}` merged to `{target}`\n\n"
            f"Run `sdlc_next.py close-epic {epic['number']}` to do all of it — it reconciles the "
            "epic branch with `main`, refuses while verification evidence is missing, and merges "
            "the integration PR when it is present. Findings from the closing run are triaged by "
            "the rule in `references/epics.md`: Blocker/Critical become children of this epic and "
            "block the close; Normal/Low go to the standing backlog epic.\n\n"
            f"{_EPIC_CLOSEABLE_MARKER}"
        )
        gh.issue_comment(epic["number"], checklist)
        gh.issue_edit(epic["number"], add_assignees=[HUMAN_ASSIGNEE])
        results.append({"epic": epic["number"], "title": epic["title"], "notified": True,
                         "open_dependents": open_dependents,
                         "docs_missing_from_epic_branch": missing_docs})
    return {"closeable_epics": results,
            **({"needs_phase_tasks": needs_phase_tasks} if needs_phase_tasks else {})}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import stack  # noqa: E402,F401
from .composites import _add_blocked_by_once  # noqa: E402
