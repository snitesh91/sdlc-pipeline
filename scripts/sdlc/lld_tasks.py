"""LLD Task parsing and `create-lld-tasks`."""
from __future__ import annotations

import os
import re
from typing import Optional

from .config import PIPELINE, Runner
from .github import GhError, GitHub, _default_runner, focus_issues
from .units import doc_root_for, local_label, unit_initiative_profile
from .evidence import epic_branch
from .footprint import (
    _FENCED_BLOCK, _TASK_KEY_MARKER, _TASK_KEY_RE, find_footprint, number_task_headings,
    parse_task_headings,
)
from .worktrees import (
    BranchWorkspace, _fetch_origin, _with_workspace, branch_lock, ensure_branch_on_origin,
)
from .design_gates import _PUSH_REJECTED_RE, git_rev_parse_head


def cmd_add_blocked_by(gh: GitHub, issue: int, dep: int) -> dict:
    """Add a native `blockedBy` edge with no other side effect (unlike `mark-blocked`,
    which also parks an in-flight unit)."""
    gh.add_blocked_by(issue, dep)
    return {"issue": issue, "blocked_on": dep, "added": True}


def cmd_remove_blocked_by(gh: GitHub, issue: int, dep: int) -> dict:
    """Remove a native `blockedBy` edge (no-op when it is absent)."""
    if dep not in gh.blocked_by(issue):
        return {"issue": issue, "blocked_on": dep, "removed": False, "reason": "no such edge"}
    gh.remove_blocked_by(issue, dep)
    return {"issue": issue, "blocked_on": dep, "removed": True}


def _task_line(name: str, after_separator: bool = True) -> re.Pattern:
    """A `<name>: <value>` field in a Task section: at line start (bullet, bold and backtick
    wrappers tolerated) or, with `after_separator`, after a ` / ` or ` | ` separating it from a
    sibling field."""
    lead = (r"(?:^[ \t]*(?:[-*][ \t]+)?|[ \t]+[/|][ \t]+)" if after_separator
            else r"^[ \t]*(?:[-*][ \t]+)?")
    return re.compile(rf"{lead}`?(?:\*\*)?`?{name}`?"
                      rf"(?:\*\*)?`?[ \t]*:[ \t]*(?:\*\*)?[ \t]*"
                      rf"(.+?)(?=`?(?:\*\*)?(?:[ \t]+[/|][ \t]|[ \t]*$))",
                      re.IGNORECASE | re.MULTILINE)


# `Depends on:` only ever starts a line of the Task's metadata block (`task_metadata_lines`).
_DEPENDS_ON_LINE = _task_line("Depends on", after_separator=False)


_PRIORITY_LINE = _task_line("Priority")


_EFFORT_LINE = _task_line("Effort")


# `Placement: local — <reason>`: the Task runs on the laptop (`localLabel`), e.g. it needs hosts
# the cloud network blocks.
_PLACEMENT_LINE = _task_line("Placement")


_REALISES_LINE = _task_line("Reali[sz]es")


_ISSUE_REF_RE = re.compile(r"#(\d+)")


# `<!-- realises: #574 #577 -->` in a carved Task's body: the issues its PR delivers.
_REALISES_MARKER = re.compile(r"<!--\s*realises:\s*([^>]*?)\s*-->")


def parse_task_realises(section_text: str) -> list:
    """Issue numbers from a section's `Realises:` line(s) (`#<n>` references only,
    in order, deduplicated); [] when none."""
    numbers = []
    for m in _REALISES_LINE.finditer(section_text):
        for ref in _ISSUE_REF_RE.findall(m.group(1)):
            if int(ref) not in numbers:
                numbers.append(int(ref))
    return numbers


def realised_issues(body: str) -> list:
    """Issue numbers a carved Task's body marks as realised (its `realises` marker)."""
    m = _REALISES_MARKER.search(body or "")
    return [int(n) for n in _ISSUE_REF_RE.findall(m.group(1))] if m else []


def task_section_defect(section_text: str) -> Optional[str]:
    """Why a `## Task <KEY>` section is not a Task to carve (None when it is): a Task
    section carries a `## Footprint` heading; prose under a Task heading does not."""
    if find_footprint(section_text) is None:
        return ("no `## Footprint` heading -- every Task section carries one "
                "(references/epics.md, \"How to size the Tasks\"); prose such as a carving "
                "summary must not sit under a `## Task` heading")
    return None


def parse_task_field(section_text: str, line: re.Pattern) -> Optional[str]:
    """The value of the section's first `line` match (markup stripped), or None."""
    m = line.search(section_text)
    return m.group(1).strip("`*. ") if m else None


# A metadata field line (`Depends on:`, `Priority:`, `Effort:`, `Realises:`), however marked up.
_TASK_FIELD_START = re.compile(r"^[ \t]*(?:[-*][ \t]+)?`?(?:\*\*)?`?"
                               r"(?:Depends on|Priority|Effort|Reali[sz]es|Placement)`?(?:\*\*)?`?[ \t]*:",
                               re.IGNORECASE)


_MD_HEADING_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]")


_LIST_ITEM_LINE = re.compile(r"^[ \t]*[-*][ \t]+")


def task_metadata_lines(section_text: str) -> list:
    """A Task section's metadata block: the lines before its first `#` subheading (fenced
    blocks dropped) that start a paragraph or list item, or follow another metadata field line.
    A wrapped prose line -- one continuing a prose line -- is never metadata, nor is anything
    under a subheading (`### Task-local decisions`, `## Footprint`, ...)."""
    lines, prev = [], ""
    for line in _FENCED_BLOCK.sub("", section_text).splitlines():
        if _MD_HEADING_LINE.match(line):
            break
        if line.strip() and (not prev.strip() or _TASK_FIELD_START.match(prev)
                             or _LIST_ITEM_LINE.match(line)):
            lines.append(line)
        prev = line
    return lines


def _depends_on_value(section_text: str) -> Optional[str]:
    """The value of the first `Depends on:` line of the metadata block, or None."""
    for line in task_metadata_lines(section_text):
        m = _DEPENDS_ON_LINE.search(line)
        if m:
            return m.group(1)
    return None


def _split_depends_on(value: str) -> tuple:
    """`(keys, unparsed)` from a `Depends on:` value: comma-separated Task keys (a standalone
    ` and ` also separates; `search-and-filter` stays one key). No-dependency words drop out;
    anything else that is not a key lands in `unparsed`."""
    keys, unparsed = [], []
    for raw in re.split(r",|[ \t]+and[ \t]+", value):
        key = raw.strip("`*. \t")
        if key.lower() in _NO_DEPENDENCY:
            continue
        if _TASK_KEY_RE.match(key):
            if key not in keys:
                keys.append(key)
        elif key not in unparsed:
            unparsed.append(key)
    return keys, unparsed


def parse_task_depends_on(section_text: str) -> list:
    """Task keys from the first `Depends on:` line of a section's metadata block
    (`task_metadata_lines`); backticks, bold and a trailing period tolerated. [] when none."""
    value = _depends_on_value(section_text)
    return _split_depends_on(value)[0] if value is not None else []


# Any `Depends on:` mention, however marked up -- to catch one the strict parser misses.
_DEPENDS_ON_MENTION = re.compile(r"depends[ \t]+on[`* \t]{0,4}:[ \t]*(.*)$",
                                 re.IGNORECASE | re.MULTILINE)


_NO_DEPENDENCY = {"", "none", "n/a", "-", "\u2014", "nothing"}


def task_dependency_defect(section_text: str) -> Optional[str]:
    """Why a Task section's `Depends on:` cannot be trusted (None when it can): its line names
    something that is not a Task key (the whole value, or part of it), or the metadata block
    mentions a dependency the strict parser does not read -- either way the Task would be
    created missing a `blockedBy` edge and race its dependency."""
    value = _depends_on_value(section_text)
    if value is not None:
        keys, unparsed = _split_depends_on(value)
        if not unparsed:
            return None
        line = f"Depends on: {value.strip()}"
        if not keys:
            return (f"`{line}` names a dependency but no Task key parses from it -- the Task "
                    f"would be created with no `blockedBy` edge and race its dependency. Write "
                    f"the line plain (`Depends on: <key>, <key>`, slug keys), then re-run")
        return (f"`{line}` parses only {', '.join(keys)}; "
                f"{', '.join(repr(u) for u in unparsed)} is not a Task key -- that edge would "
                f"be silently dropped. Write the line as comma-separated slug keys only, then "
                f"re-run")
    for m in _DEPENDS_ON_MENTION.finditer("\n".join(task_metadata_lines(section_text))):
        if m.group(1).strip("`*. \t").lower() not in _NO_DEPENDENCY:
            return (f"`{m.group(0).strip()}` names a dependency but no Task key parses from "
                    f"it -- the Task would be created with no `blockedBy` edge and race its "
                    f"dependency. Put it on its own line (`Depends on: <key>`, no backtick "
                    f"wrapper, slug keys) in the metadata block under the Task heading, then "
                    f"re-run")
    return None


def _commit_doc_to_epic_branch(repo_path: Optional[str], epic: str, doc_path: str,
                                text: str, runner: Runner, message: str) -> dict:
    """Commit `text` at `doc_path` on branch `epic` and push. Refuses a dirty tree or
    unpushed commits touching other files; a rejected push returns `conflict: True`.
    Returns `committed`, `pushed` (+ `reason`)."""
    with branch_lock(epic):
        ensure_branch_on_origin(repo_path or ".", epic, runner=runner)
        with BranchWorkspace(epic, repo_path, runner) as ws:
            _fetch_origin(ws.path, runner)
            unpushed = runner(["git", "-C", ws.path, "diff", "--name-only",
                               f"origin/{epic}...{epic}"]).split()
            if runner(["git", "-C", ws.path, "status", "--porcelain"]).strip():
                out = {"committed": None, "pushed": False,
                       "reason": f"epic worktree {ws.path} has uncommitted changes -- "
                                 f"refusing to reset it; commit or stash them, then re-run"}
            elif any(f != doc_path for f in unpushed):
                out = {"committed": None, "pushed": False,
                       "reason": f"local {epic} carries unpushed commits touching "
                                 f"{', '.join(f for f in unpushed if f != doc_path)} -- "
                                 f"refusing to reset it; push or discard them, then re-run"}
            else:
                runner(["git", "-C", ws.path, "checkout", "-B", epic, f"origin/{epic}"])
                target = os.path.join(ws.path, doc_path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w") as f:
                    f.write(text)
                runner(["git", "-C", ws.path, "add", "--", doc_path])
                runner(["git", "-C", ws.path, "commit", "-m", message, "--", doc_path])
                sha = git_rev_parse_head(ws.path, runner=runner)
                try:
                    runner(["git", "-C", ws.path, "push", "origin", epic])
                    out = {"committed": sha, "pushed": True}
                except GhError as e:
                    if not _PUSH_REJECTED_RE.search(str(e)):
                        raise
                    out = {"committed": sha, "pushed": False, "conflict": True,
                           "reason": f"push to {epic} was rejected -- the branch moved "
                                     f"under this command. The Task issues exist and are "
                                     f"recorded; re-run to replay the doc rewrite onto the "
                                     f"new tip."}
    return _with_workspace(out, ws)


def task_type_name() -> str:
    """The native Issue Type a Task is created with: classification.task's issueType, else Task."""
    rule = PIPELINE.get("classification", {}).get("task") or {}
    return rule["value"] if rule.get("field") == "issueType" else "Task"


def _wire_numbered_task_edges(gh: GitHub, doc: str, numbered: list, key_to_number: dict,
                              all_issues: dict, result: dict) -> None:
    """Reconcile each already-numbered Task's `blockedBy` edges onto sibling Tasks with its
    `Depends on:` lines: add the missing ones (an open dependency only, never twice) into
    `result["edges_added"]` / `result["edges_failed"]`, and remove a sibling edge the doc no
    longer names (an LLD revision dropped it) into `result["edges_removed"]`."""
    siblings = set(key_to_number.values())
    for h in numbered:
        issue_number = h["number"]
        if (all_issues.get(issue_number) or {}).get("state", "OPEN") != "OPEN":
            continue  # a closed Task waits on nothing
        section = doc[h["line_end"]:h["end"]]
        if not task_dependency_defect(section):
            wanted = {key_to_number.get(k) for k in parse_task_depends_on(section)}
            try:
                for dep in gh.blocked_by(issue_number):
                    if dep in siblings and dep not in wanted:
                        gh.remove_blocked_by(issue_number, dep)
                        result.setdefault("edges_removed", []).append(
                            {"issue": issue_number, "on": dep})
            except GhError as e:
                result["edges_failed"].append({"issue": issue_number, "error": str(e)})
        for dep_key in parse_task_depends_on(section):
            dep = key_to_number.get(dep_key)
            if dep is None or dep == issue_number:
                result["edges_failed"].append(
                    {"issue": issue_number, "on_key": dep_key,
                     "error": f"no other `## Task {dep_key}` section in the doc to depend on"})
                continue
            if (all_issues.get(dep) or {}).get("state", "OPEN") != "OPEN":
                continue  # a closed dependency blocks nothing
            try:
                edge = _add_blocked_by_once(gh, issue_number, dep)
            except GhError as e:
                result["edges_failed"].append({"issue": issue_number, "on": dep, "error": str(e)})
                continue
            if edge.get("added"):
                result["edges_added"].append({"issue": issue_number, "on": dep})


# The two standing Tasks `lld` carves by default; an Initiative profile's `testTasks: false`
# turns them off, and create-lld-tasks then skips a section titled as one.
_STANDING_TEST_TASK_TITLE = re.compile(
    r"\s*(?:standing\s+)?(?:integration|e2e)[\s_-]*tests?(?:\s+task)?\s*$", re.IGNORECASE)


STANDING_TEST_TASKS_OFF = ("a standing Integration-test / e2e-test Task, but this Epic's "
                           "Initiative turns them off (`testTasks: false`) -- not created; drop "
                           "the section from lld.md (the coverage it owned goes in the functional "
                           "Tasks' own sections, or to the Initiative's final runs)")


def cmd_create_lld_tasks(gh: GitHub, epic: int, repo_path: str = ".",
                          runner: Runner = _default_runner) -> dict:
    """Create one Task per `## Task <KEY>` section of the Epic's merged `lld.md`,
    renumber the headings, wire `Depends on:` as `blockedBy` edges, and push the doc.
    Idempotent: existing issues are matched back by their `task-key` marker."""
    epic_br = epic_branch(epic)
    doc_path = f"{doc_root_for(epic, gh)}/epic-{epic}/lld.md"
    _fetch_origin(repo_path, runner)
    try:
        doc = runner(["git", "-C", repo_path, "show", f"origin/{epic_br}:{doc_path}"])
    except GhError:
        raise GhError(f"cannot read {doc_path} on origin/{epic_br} -- merge the LLD-phase "
                       f"Task's design PR first (merge-design-pr), then re-run create-lld-tasks")
    headings = parse_task_headings(doc)
    keyed = [h for h in headings if h["key"]]
    # A `## Task` heading over prose (a carving table, a summary) is never a Task issue.
    # So is one whose `Depends on:` doesn't parse: created, it would race its dependency.
    dep_defects = {h["key"]: task_dependency_defect(doc[h["line_end"]:h["end"]])
                   for h in keyed}
    defects = {h["key"]: task_section_defect(doc[h["line_end"]:h["end"]])
               or dep_defects[h["key"]] for h in keyed}
    if any(_STANDING_TEST_TASK_TITLE.match(h["title"] or "") for h in keyed) \
            and not unit_initiative_profile(gh, epic)["testTasks"]:
        for h in keyed:
            if defects[h["key"]] is None and _STANDING_TEST_TASK_TITLE.match(h["title"] or ""):
                defects[h["key"]] = STANDING_TEST_TASKS_OFF
    pending = [h for h in keyed if defects[h["key"]] is None]
    # An already-numbered Task's `Depends on:` is re-read on every run, so a missed edge heals.
    numbered = [h for h in headings if h["number"]]
    numbered_defects = {h["task_key"] or f"#{h['number']}":
                        task_dependency_defect(doc[h["line_end"]:h["end"]]) for h in numbered}
    result = {"epic": epic, "doc": doc_path, "created": [], "reused": [],
              "already_numbered": [h["number"] for h in numbered],
              "skipped_sections": [
                  {"key": h["key"], "heading": doc[h["line_start"]:h["line_end"]].strip(),
                   "reason": defects[h["key"]]} for h in keyed if defects[h["key"]]],
              "dependency_parse_warnings": [k for k, d in {**dep_defects,
                                                           **numbered_defects}.items() if d],
              "blocked_by": [], "blocked_by_failed": [], "realises": [], "realises_failed": [],
              "edges_added": [], "edges_failed": []}
    # A crash between create and push leaves issues the doc doesn't number yet.
    key_to_number = {h["task_key"]: h["number"] for h in headings
                     if h["number"] and h["task_key"]}
    realised = {n for h in pending for n in parse_task_realises(doc[h["line_end"]:h["end"]])}
    all_issues = ({i["number"]: i for i in focus_issues(gh, realised)}
                  if (pending or numbered) else {})
    for child in all_issues.values():
        if (child.get("parent") or {}).get("number") != epic:
            continue
        m = _TASK_KEY_MARKER.search(child.get("body") or "")
        if m:
            key_to_number.setdefault(m.group(1), child["number"])
    if not pending:
        _wire_numbered_task_edges(gh, doc, numbered, key_to_number, all_issues, result)
        return {**result, "committed": None, "pushed": False, "tasks": {},
                "reason": ("every `## Task` heading already carries an issue number -- "
                           "nothing to create. A re-run after a completed pass lands here, "
                           "which is what makes this command safe to retry.")
                          + (" But a numbered Task's `blockedBy` edge could not be added "
                             "(`edges_failed`) -- fix it, then re-run."
                             if result["edges_failed"] else "")}
    # `merge-lld-doc` advances only issues that classify as Tasks.
    type_name = task_type_name()
    # Validate every section's Priority/Effort before creating any issue.
    fields = {h["key"]: issue_field_values(
                  parse_task_field(doc[h["line_end"]:h["end"]], _PRIORITY_LINE),
                  parse_task_field(doc[h["line_end"]:h["end"]], _EFFORT_LINE))
              for h in pending}
    for h in pending:
        key = h["key"]
        title = h["title"] or key
        number = key_to_number.get(key)
        if number is None:
            # Only references that resolve are persisted; the rest are reported below.
            realises = [n for n in parse_task_realises(doc[h["line_end"]:h["end"]])
                        if n in all_issues and n not in key_to_number.values()]
            realises_marker = (f"\n<!-- realises: {' '.join(f'#{n}' for n in realises)} -->"
                               if realises else "")
            body = (f"{doc[h['line_end']:h['end']].strip()}\n\n"
                    f"Carved from `{doc_path}` on `{epic_br}` (Epic #{epic}).\n\n"
                    f"<!-- task-key: {key} -->{realises_marker}")
            placement = parse_task_field(doc[h["line_end"]:h["end"]], _PLACEMENT_LINE) or ""
            labels = [local_label()] if placement.lower().startswith("local") else []
            for label in labels:
                gh.ensure_label(label)
            created = cmd_create_issue(gh, title, body, epic, labels, type_name=type_name,
                                       **fields[key])
            if created.get("ok") is False:
                # Never renumber the doc against a half-made issue.
                return {**result, "ok": False, "committed": None, "pushed": False,
                        "tasks": key_to_number, "failed_key": key,
                        "failed_step": created.get("failed_step"),
                        "partial_issue": created.get("issue"),
                        "reason": created.get("reason")}
            number = created["issue"]
            result["created"].append({"key": key, "issue": number, "title": title})
        else:
            result["reused"].append({"key": key, "issue": number, "title": title})
        key_to_number[key] = number
    # After the creates, so a numbered Task's edge onto a key added in this pass resolves now.
    _wire_numbered_task_edges(gh, doc, numbered, key_to_number, all_issues, result)
    for h in pending:
        issue_number = key_to_number[h["key"]]
        for dep_key in parse_task_depends_on(doc[h["line_end"]:h["end"]]):
            dep = key_to_number.get(dep_key)
            if dep is None or dep == issue_number:
                result["blocked_by_failed"].append(
                    {"issue": issue_number, "on_key": dep_key,
                     "error": f"no `## Task {dep_key}` section in {doc_path} to depend on"})
                continue
            try:
                gh.add_blocked_by(issue_number, dep)
                result["blocked_by"].append({"issue": issue_number, "on": dep})
            except GhError as e:
                # Re-adding an existing edge errors; a resumed run must not crash on it.
                result["blocked_by_failed"].append(
                    {"issue": issue_number, "on": dep, "error": str(e)})
        # A realised issue waits on the Task that delivers it and closes with that Task's PR.
        for old in parse_task_realises(doc[h["line_end"]:h["end"]]):
            entry = {"issue": issue_number, "realises": old}
            existing = all_issues.get(old)
            if existing is None or old == issue_number or old in key_to_number.values():
                result["realises_failed"].append(
                    {**entry, "error": f"#{old} is not a pre-existing issue -- `Realises:` "
                                       f"names issues that exist before this carving"})
                continue
            if existing["state"] != "OPEN":
                result["realises"].append({**entry, "blocked": False, "reason": "already closed"})
                continue
            try:
                edge = _add_blocked_by_once(gh, old, issue_number)
                result["realises"].append({**entry, "blocked": True,
                                           **({"reason": edge["reason"]} if edge.get("reason") else {})})
            except GhError as e:
                result["realises_failed"].append({**entry, "error": str(e)})
    published = _commit_doc_to_epic_branch(
        repo_path, epic_br, doc_path, number_task_headings(doc, key_to_number), runner,
        message=f"docs(sdlc): number epic-{epic} lld tasks")
    return {**result, "tasks": key_to_number, **published}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import gates  # noqa: E402,F401
from .issue_admin import cmd_create_issue, issue_field_values  # noqa: E402
from .composites import _add_blocked_by_once  # noqa: E402
