"""LLD task headings, footprints and the dev-PR scope rule."""
from __future__ import annotations

import re
from typing import Optional

from .config import EPIC_BRANCH_PREFIX, Runner, all_doc_roots, issue_branch
from .github import GhError, WorkItemProvider, _default_runner, focus_issues
from .units import doc_root_for
from .evidence import epic_branch, integration_base


# Exactly `Footprint` (optionally numbered, `## 17. Footprint`, or with a colon), so a
# prose heading that merely starts with the word is never taken for the section.
_FOOTPRINT_HEADING = re.compile(r"^#+\s*(?:\d+[.)]\s*)?Footprint\s*:?\s*$",
                                re.IGNORECASE | re.MULTILINE)


_FOOTPRINT_BULLET_PATH = re.compile(r"^-\s+`([^`]+)`")


# The `**Verify-only:**` sub-label ends the owned-path list: what follows is read, not owned.
_FOOTPRINT_VERIFY_ONLY = re.compile(r"^\*\*\s*(?:verify|read)[ -]?only\b", re.IGNORECASE)


_FENCED_BLOCK = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


# A Task heading is `## Task #<n>` or, before issues exist, `## Task <slug-key>: ...`
# (`###` and a missing `#` also parse). Keys must be slugs so prose headings like
# `## Task Breakdown` are never read as Tasks.
_TASK_HEADING_LINE = re.compile(r"^(#{2,3})\s*Task\b\s*(\S+)(.*)$",
                                re.IGNORECASE | re.MULTILINE)


_TASK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


_TASK_KEY_MARKER = re.compile(r"<!--\s*task-key:\s*([a-z0-9][a-z0-9-]*)\s*-->", re.IGNORECASE)


def parse_task_headings(doc_text: str) -> list:
    """Every Task heading (numbered or keyed) in an Epic `lld.md`, in order, as dicts
    with `start`/`end`, `line_start`/`line_end`, `hashes`, `number`, `key`,
    `task_key`, `title`. Both spellings are scanned so section ends are always right."""
    entries = []
    for m in _TASK_HEADING_LINE.finditer(doc_text):
        ident, rest = m.group(2).rstrip(":"), m.group(3)
        number = key = None
        if ident.lstrip("#").isdigit():
            number = int(ident.lstrip("#"))
        elif _TASK_KEY_RE.match(ident):
            key = ident
        else:
            continue  # `## Task Breakdown` and friends: prose, not a Task
        marker = _TASK_KEY_MARKER.search(rest)
        entries.append({"number": number, "key": key,
                        "task_key": marker.group(1) if marker else None,
                        "title": _TASK_KEY_MARKER.sub("", rest).strip().lstrip(":").strip(),
                        "hashes": m.group(1),
                        "line_start": m.start(), "line_end": m.end()})
    for i, entry in enumerate(entries):
        entry["start"] = entry["line_start"]
        entry["end"] = entries[i + 1]["line_start"] if i + 1 < len(entries) else len(doc_text)
    return entries


def number_task_headings(doc_text: str, key_to_number: dict) -> str:
    """`doc_text` with each keyed Task heading in `key_to_number` rewritten to
    `## Task #<n>: <title> <!-- task-key: <key> -->`, as `create-lld-tasks` pushes it."""
    out = doc_text
    for h in parse_task_headings(doc_text):
        if h["key"] in key_to_number:
            out = out.replace(doc_text[h["line_start"]:h["line_end"]],
                              f"{h['hashes']} Task #{key_to_number[h['key']]}: "
                              f"{h['title'] or h['key']} <!-- task-key: {h['key']} -->", 1)
    return out


def is_numbered_copy(src: str, dest: str) -> bool:
    """Whether `dest` is exactly `src` after `create-lld-tasks` numbered its Task headings."""
    numbers = {h["task_key"]: h["number"] for h in parse_task_headings(dest)
               if h["number"] and h["task_key"]}
    return bool(numbers) and number_task_headings(src, numbers) == dest


def _task_heading_matches(entry: dict, task) -> bool:
    """Whether a parsed heading is `task`: a number matches `#<n>`; a slug matches
    the heading's key or its preserved `task-key` marker (so it survives renumbering)."""
    ident = str(task)
    if ident.isdigit():
        return entry["number"] == int(ident)
    return ident in (entry["key"], entry["task_key"])


def find_footprint(doc_text: str) -> Optional[list]:
    """Owned paths (backticked bullets) under the first real `## Footprint` heading, up to
    the next heading; None when the doc has no such heading. Lead-in lines before the
    bullets are skipped; a `**Verify-only:**` sub-label ends the owned list."""
    m = _FOOTPRINT_HEADING.search(_FENCED_BLOCK.sub("", doc_text))
    if not m:
        return None
    rest = m.string[m.end():]
    end = re.search(r"^#+\s", rest, re.MULTILINE)
    body = rest[:end.start()] if end else rest
    paths = []
    for line in body.splitlines():
        stripped = line.strip()
        if _FOOTPRINT_VERIFY_ONLY.match(stripped):
            break
        bm = _FOOTPRINT_BULLET_PATH.match(stripped)
        if bm:
            paths.append(bm.group(1))
    return paths


def parse_footprint(doc_text: str) -> list:
    """`find_footprint` with a missing section flattened to `[]` -- callers that need to
    tell "no section" from "owns nothing" use `find_footprint`."""
    return find_footprint(doc_text) or []


def find_task_footprint(doc_text: str, task_number) -> Optional[list]:
    """`find_footprint` scoped to one Task's subsection of an Epic `lld.md`
    (`task_number` may be an issue number or task key); None when the subsection or
    its `## Footprint` heading is missing."""
    for entry in parse_task_headings(doc_text):
        if _task_heading_matches(entry, task_number):
            return find_footprint(doc_text[entry["line_end"]:entry["end"]])
    return None


def parse_task_footprint(doc_text: str, task_number) -> list:
    """`find_task_footprint` with a missing section flattened to `[]`."""
    return find_task_footprint(doc_text, task_number) or []


def slice_task_subsection(doc_text: str, task_number: int) -> Optional[str]:
    """One Task's subsection of an Epic `lld.md` (heading up to the next Task
    heading), by issue number or task key; None when not found."""
    for entry in parse_task_headings(doc_text):
        if _task_heading_matches(entry, task_number):
            return doc_text[entry["start"]:entry["end"]].rstrip() + "\n"
    return None


def lld_task_headings_dropped(base_text: str, new_text: str) -> list:
    """Task headings on the epic branch's `lld.md` that a revised copy no longer carries
    (as `#<n>` or the key), so `lld-section` would stop finding them."""
    def ids(text):
        return {f"#{h['number']}" if h["number"] else (h["task_key"] or h["key"])
                for h in parse_task_headings(text)}
    return sorted(ids(base_text) - ids(new_text))


def cmd_lld_section(repo_path: str, epic: int, task,
                     runner: Runner = _default_runner,
                     gh: Optional[WorkItemProvider] = None) -> dict:
    """Print one Task's subsection of `origin/epic-<n>:<epic's docRoot>/.../lld.md` as raw
    markdown, then return `{ok, epic, task, chars}` as the JSON footer. `task` is an issue
    number or task key."""
    doc = f"{doc_root_for(epic, gh)}/epic-{epic}/lld.md"
    try:
        text = runner(["git", "-C", repo_path, "show", f"origin/{epic_branch(epic)}:{doc}"])
    except GhError:
        raise GhError(f"cannot read {doc} on "
                       f"origin/{epic_branch(epic)} -- is the epic's lld merged?")
    section = slice_task_subsection(text, task)
    if section is None:
        label = f"#{task}" if str(task).isdigit() else f"{task}"
        raise GhError(f"no `## Task {label}` subsection in {doc} "
                       f"(headings must read `## Task {label}: ...`, per agents/lld.md)")
    print(section, end="")
    return {"ok": True, "epic": epic, "task": task, "chars": len(section)}


def read_footprint(repo_path: str, issue: int, runner: Runner = _default_runner,
                    epic: Optional[int] = None,
                    gh: Optional[WorkItemProvider] = None) -> Optional[list]:
    """An issue's owned footprint from `origin/issue-<n>`'s architecture.md, else (with
    `epic`) from its Task subsection of the Epic's lld.md. None when no `## Footprint`
    section is readable anywhere; `[]` for a section that owns no path (verify-only).
    Reads origin refs via `git show`, so no worktree is needed."""
    try:
        text = runner(["git", "-C", repo_path, "show",
                        f"origin/{issue_branch(issue)}:{doc_root_for(issue, gh)}/issue-{issue}"
                        f"/architecture.md"])
    except GhError:
        text = None
    if text is not None:
        footprint = find_footprint(text)
        if footprint is not None:
            return footprint
    if epic is not None:
        try:
            text = runner(["git", "-C", repo_path, "show",
                            f"origin/{epic_branch(epic)}:{doc_root_for(epic, gh)}/epic-{epic}/lld.md"])
        except GhError:
            return None
        return find_task_footprint(text, issue)
    return None


def _footprint_prefix(path: str) -> str:
    """Strip a trailing `*`/`**` glob; footprints are exact paths or directory
    globs, so prefix containment is enough."""
    return path.rstrip("*").rstrip("/")


def footprint_overlaps(a: list, b: list) -> bool:
    """Whether any path in `a` equals or contains/is contained by one in `b`."""
    a_prefixes = [_footprint_prefix(p) for p in a]
    b_prefixes = [_footprint_prefix(p) for p in b]
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/")
               for x in a_prefixes for y in b_prefixes)


# The rule a `development` PR must obey on its diff, quoted verbatim in every refusal/report.
DEV_PR_SCOPE_RULE = ("development authors no design doc; record deviations in the PR description")


def _design_doc_epic(path: str, design_prefix: tuple) -> Optional[int]:
    """The Epic number `n` of a `<docRoot>/epic-<n>/...` path, else None."""
    for prefix in design_prefix:
        if path.startswith(prefix):
            m = re.match(r"(\d+)/", path[len(prefix):])
            return int(m.group(1)) if m else None
    return None


def dev_pr_scope_offenders(gh: WorkItemProvider, issue: int, changed_files: list,
                           repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """Paths a `development` branch/PR must not touch: any Epic design doc under
    `<docRoot>/epic-*/` of any configured doc root (e.g. lld.md) whose Epic is still open (a
    closed Epic's docs are its as-built record: a correction to them, e.g. a standing child's
    doc-drift fix, has no other route and is allowed), and any file listed in
    ANOTHER open Task's `## Footprint` of the same Epic's lld.md that is not in this unit's
    OWN footprint. Returns
    `{"design_docs": [...], "foreign_footprint": [...]}` (both sorted, deduped); the
    foreign-footprint list is empty when the unit is not a child of a non-standing Epic or the
    Epic lld.md is unreadable (never guessed at)."""
    # Every product's Epic design docs: a development branch authors none of them.
    design_prefix = tuple(f"{root}/epic-" for root in all_doc_roots())
    design_docs = sorted({p for p in changed_files if p.startswith(design_prefix)})
    if design_docs:
        epics = {_design_doc_epic(p, design_prefix) for p in design_docs}
        states = {i["number"]: i["state"] for i in focus_issues(gh, epics)}
        design_docs = [p for p in design_docs
                       if states.get(_design_doc_epic(p, design_prefix)) != "CLOSED"]
    foreign: set = set()
    # Only the foreign-footprint arm needs the Epic lld.md; skip it (and its API calls) when the
    # diff has no non-design-doc paths to check.
    non_doc = [p for p in changed_files if not p.startswith(design_prefix)]
    integ = integration_base(gh, issue) if non_doc else "main"
    if integ != "main":
        epic_no = int(integ[len(EPIC_BRANCH_PREFIX):])
        try:
            lld = runner(["git", "-C", repo_path, "show",
                          f"origin/{epic_branch(epic_no)}:{doc_root_for(epic_no, gh)}"
                          f"/epic-{epic_no}/lld.md"])
        except GhError:
            lld = None
        if lld:
            own = parse_task_footprint(lld, issue)
            open_children = {i["number"] for i in gh.issue_list()
                             if i["state"] == "OPEN"
                             and (i.get("parent") or {}).get("number") == epic_no
                             and i["number"] != issue}
            others: list = []
            for entry in parse_task_headings(lld):
                if entry["number"] in open_children:
                    others.extend(parse_footprint(lld[entry["line_end"]:entry["end"]]))
            for p in changed_files:
                if p.startswith(design_prefix) or footprint_overlaps([p], own):
                    continue  # a design doc (counted above) or the unit's own footprint
                if others and footprint_overlaps([p], others):
                    foreign.add(p)
    return {"design_docs": design_docs, "foreign_footprint": sorted(foreign)}


def _dev_pr_scope_refusal_reason(offenders: dict) -> Optional[str]:
    """A one-string reason naming the offending paths, or None when the diff is clean."""
    parts = []
    if offenders["design_docs"]:
        parts.append(f"edits Epic design doc(s): {', '.join(offenders['design_docs'])}")
    if offenders["foreign_footprint"]:
        parts.append(f"touches another Task's Footprint file(s): "
                     f"{', '.join(offenders['foreign_footprint'])}")
    if not parts:
        return None
    ack = (" A foreign-footprint touch that is an inherent side effect of this Task can be "
           "acknowledged instead: list each such path in the PR body under a "
           "`## Footprint deviations` heading as a bullet starting with the backticked path "
           "(e.g. - `path/x.ts` — why). Epic design docs can never be acknowledged."
           if offenders["foreign_footprint"] else "")
    return (f"this development diff {'; and '.join(parts)} -- {DEV_PR_SCOPE_RULE}. Revert those "
            f"paths and, if the design truly needs to change, escalate an Architecture/LLD "
            f"revision rather than editing the doc from a development branch.{ack}")


_FOOTPRINT_DEVIATIONS_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*Footprint deviations[ \t]*$",
                                           re.IGNORECASE | re.MULTILINE)


_DEVIATION_BULLET = re.compile(r"^[ \t]*[-*][ \t]+`([^`\n]+)`")


def acknowledged_footprint_deviations(pr_body: Optional[str]) -> list:
    """Paths a PR body acknowledges as footprint deviations: each bullet under its
    `## Footprint deviations` heading (up to the next heading) that starts with a backticked
    path, in order, deduplicated. [] when the section is absent."""
    m = _FOOTPRINT_DEVIATIONS_HEADING.search(pr_body or "")
    if not m:
        return []
    rest = m.string[m.end():]
    end = re.search(r"^[ \t]*#{1,6}[ \t]", rest, re.MULTILINE)
    paths = []
    for line in (rest[:end.start()] if end else rest).splitlines():
        bm = _DEVIATION_BULLET.match(line)
        if bm and bm.group(1).strip() not in paths:
            paths.append(bm.group(1).strip())
    return paths


def acknowledge_footprint_deviations(offenders: dict, pr_body: Optional[str]) -> tuple:
    """`(offenders, acknowledged)`: the foreign-footprint offenders the PR body acknowledges
    move to `acknowledged`; design-doc offenders are never acknowledgeable."""
    listed = set(acknowledged_footprint_deviations(pr_body))
    acknowledged = [p for p in offenders["foreign_footprint"] if p in listed]
    return ({**offenders, "foreign_footprint": [p for p in offenders["foreign_footprint"]
                                                if p not in listed]}, acknowledged)


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import plugin_version  # noqa: E402,F401
