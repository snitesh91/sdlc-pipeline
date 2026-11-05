"""Retro findings, comments and `edit-issue`."""
from __future__ import annotations

import io
import json
import os
import re
from typing import Optional

from .config import LABELS, PIPELINE, PIPELINE_STATUS_FIELD_NAMES, normalize_stage
from .github import GhError, WorkItemProvider
from .units import cloud_label
from .run_state import _run_state_dir
from .plugin_version import running_plugin_version


_FINDING_MARKER = re.compile(r"<!--\s*sdlc:finding\s+key=([a-z0-9][a-z0-9-]*)[^>]*-->")


_SUPERSEDED_MARKER = re.compile(r"<!--\s*sdlc:superseded-by\s+#(\d+)\s*-->")


_FINDING_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


_PARK_HOPS = 4


def _read_text(text: Optional[str], text_file: Optional[str], what: str) -> str:
    """Exactly one of `text` / the contents of `text_file`, stripped; raises when empty."""
    if (text is None) == (text_file is None):
        raise GhError(f"pass exactly one of --{what} / --{what}-file")
    if text_file is not None:
        try:
            with io.open(os.path.expanduser(text_file), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise GhError(f"--{what}-file must be a readable file: {exc}")
    text = (text or "").strip()
    if not text:
        raise GhError(f"the {what} is empty")
    return text


def _live_park_issue(gh: WorkItemProvider) -> tuple:
    """`pipeline.retro.parkIssue`, following `sdlc:superseded-by` markers from a closed one:
    `(number, comments)` of the open issue findings go to."""
    number = PIPELINE["retro"].get("parkIssue")
    if not number:
        raise GhError("pipeline.retro.parkIssue is not set -- name the open retro-backlog issue "
                      "in the config before parking findings")
    number = int(number)
    for _ in range(_PARK_HOPS):
        view = gh.issue_view(number)
        comments = view.get("comments", [])
        if view.get("state", "OPEN").upper() != "CLOSED":
            return number, comments
        nxt = next((int(m.group(1)) for c in reversed(comments)
                    for m in [_SUPERSEDED_MARKER.search(c.get("body") or "")] if m), None)
        if nxt is None:
            raise GhError(f"retro park issue #{number} is closed and names no successor "
                          f"(sdlc:superseded-by marker) -- point pipeline.retro.parkIssue at "
                          f"the open one")
        number = nxt
    raise GhError(f"retro park issue: more than {_PARK_HOPS} superseded hops from "
                  f"#{PIPELINE['retro'].get('parkIssue')} -- update pipeline.retro.parkIssue")


def cmd_park_finding(gh: WorkItemProvider, key: str, text: Optional[str] = None,
                     text_file: Optional[str] = None, evidence: tuple = (),
                     target: Optional[str] = None) -> dict:
    """Post one friction finding on the retro park issue, or, when a comment there already
    carries `key`, a short "seen again" comment linking it. Returns `issue`, `posted`
    (`finding` | `seen-again`) and `original` (the earlier comment's url) on a repeat."""
    key = (key or "").strip().lower()
    if not _FINDING_KEY.fullmatch(key):
        raise GhError(f"--key {key!r} must be a short slug ([a-z0-9-], at most 64 chars)")
    text = _read_text(text, text_file, "text")
    version = running_plugin_version() or "unknown"
    number, comments = _live_park_issue(gh)
    original = next((c for c in comments
                     if any(m.group(1) == key for m in _FINDING_MARKER.finditer(c.get("body") or ""))),
                    None)
    links = "".join(f"\n- {e}" for e in evidence)
    if original is not None:
        first = text.splitlines()[0][:200]
        gh.issue_comment(number, f"Seen again on plugin `{version}`: {first}\n\n"
                                 f"Original: {original.get('url') or 'above'}"
                                 + (f"\n\nEvidence:{links}" if links else "")
                                 + f"\n\n<!-- sdlc:finding-seen key={key} v={version} -->")
        return {"issue": number, "key": key, "posted": "seen-again",
                "original": original.get("url")}
    body = (f"**Finding** `{key}` — plugin `{version}`\n\n{text}"
            + (f"\n\nTarget: `{target}`" if target else "")
            + (f"\n\nEvidence:{links}" if links else "")
            + f"\n\n<!-- sdlc:finding key={key} v={version} -->")
    if len(body) > EVIDENCE_CAP:
        return {"issue": number, "refused": True,
                "reason": f"the finding is {len(body):,} chars, over the {EVIDENCE_CAP:,}-char "
                          f"cap; trim it and re-run"}
    gh.issue_comment(number, body)
    return {"issue": number, "key": key, "posted": "finding"}


def cmd_supersede_park_issue(gh: WorkItemProvider, old: int, new: int) -> dict:
    """Point a retro park issue at its successor (`sdlc:superseded-by` marker), then close it."""
    if old == new:
        raise GhError("an issue cannot supersede itself")
    view = gh.issue_view(old)
    if not any(_SUPERSEDED_MARKER.search(c.get("body") or "") for c in view.get("comments", [])):
        gh.issue_comment(old, f"Superseded by #{new}.\n\n<!-- sdlc:superseded-by #{new} -->")
    if view.get("state", "OPEN").upper() != "CLOSED":
        gh.issue_close(old)
    return {"issue": old, "superseded_by": new, "closed": True}


def cmd_comment(gh: WorkItemProvider, issue: int, body: Optional[str] = None,
                body_file: Optional[str] = None) -> dict:
    """Post a plain comment on `issue` from `body` or `body_file` (one of them), capped at
    `HANDOFF_CAP` chars. No marker: it is the orchestrator's audit-trail note."""
    if (body is None) == (body_file is None):
        raise GhError("pass exactly one of --body / --body-file")
    if body_file is not None:
        try:
            with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            raise GhError(f"--body-file must be a readable file holding the comment text: {exc}")
    body = (body or "").strip()
    if not body:
        raise GhError("the comment is empty -- say something or do not comment")
    if len(body) > HANDOFF_CAP:
        return {"issue": issue, "refused": True,
                "reason": f"the comment is {len(body):,} chars, over the {HANDOFF_CAP:,}-char "
                          f"cap (references/stage-playbooks.md, \"Comment size is a "
                          f"contract\"); trim it and re-run"}
    gh.issue_comment(issue, body)
    return {"issue": issue, "commented": True, "chars": len(body)}


def pipeline_owned_label(label: str) -> bool:
    """Whether the pipeline writes or reads `label` as its own state: the gate, architected and
    cloud-placement labels, or one naming a Stage or Pipeline Status."""
    if label in (LABELS["gate"], LABELS["architected"], cloud_label()):
        return True
    name = label.strip().lower()
    bare = name.split(":", 1)[1] if name.startswith(("stage:", "status:")) else name
    return (normalize_stage(bare) is not None
            or bare.replace(" ", "-") in PIPELINE_STATUS_FIELD_NAMES.values()
            or name != bare)


def live_runs_holding(issue: int) -> list:
    """Run ids of open (unarchived) run-states that have `issue` in flight."""
    try:
        names = sorted(os.listdir(_run_state_dir()))
    except OSError:
        return []
    runs = []
    for name in names:
        try:
            with open(os.path.join(_run_state_dir(), name)) as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and not state.get("closed") \
                and str(issue) in (state.get("in_flight") or {}):
            runs.append(state.get("run_id"))
    return runs


def cmd_edit_issue(gh: WorkItemProvider, issue: int, body_file: Optional[str] = None,
                   add_labels: list = (), remove_labels: list = (),
                   title: Optional[str] = None) -> dict:
    """Operator-directed edit of `issue`'s title, body and labels. Refuses (exit 0) a
    pipeline-owned label and an issue a live run has in flight."""
    add_labels, remove_labels = list(add_labels or ()), list(remove_labels or ())
    if title is not None and not title.strip():
        raise GhError("--title is empty -- an issue title is never blanked here")
    if body_file is None and title is None and not add_labels and not remove_labels:
        raise GhError("pass --title, --body-file, --add-label or --remove-label")
    result = {"issue": issue, "edited": [], "refused": False, "reason": None}
    owned = [l for l in add_labels + remove_labels if pipeline_owned_label(l)]
    if owned:
        return {**result, "refused": True,
                "reason": f"pipeline-owned label(s) {', '.join(owned)}: the pipeline sets "
                          f"them through its own commands, never by hand"}
    held = live_runs_holding(issue)
    if held:
        return {**result, "refused": True,
                "reason": f"#{issue} is in flight in live run {', '.join(map(str, held))}; "
                          f"edit it once the run stops or the unit is terminal"}
    body = None
    if body_file is not None:
        try:
            with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            raise GhError(f"--body-file must be a readable file holding the issue body: {exc}")
        if not body.strip():
            raise GhError("the body file is empty -- an issue body is never blanked here")
    if title is not None:
        gh.issue_set_title(issue, title.strip())
        result["edited"].append("title")
    if body is not None:
        gh.issue_set_body(issue, body)
        result["edited"].append("body")
    for label in add_labels:
        gh.ensure_label(label)
    if add_labels or remove_labels:
        gh.issue_edit(issue, add_labels=add_labels, remove_labels=remove_labels)
    result["edited"] += ([f"add-label:{l}" for l in add_labels]
                         + [f"remove-label:{l}" for l in remove_labels])
    return result


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import human_channel  # noqa: E402,F401
from .composites import EVIDENCE_CAP, HANDOFF_CAP  # noqa: E402
