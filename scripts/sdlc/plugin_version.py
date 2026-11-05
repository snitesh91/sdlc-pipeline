"""Worktree paths, the plugin version guard and `show-config`."""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from .config import (
    CLOUD_SESSIONS, CONFIG, CROSS_EPIC_FOOTPRINT_CHECK, DESIGN_LANE_PARALLELISM,
    DEV_LANE_PARALLELISM, DOC_ROOT, MAX_TASKS_PER_RUN, PIPELINE, PLUGIN_ROOT,
    PR_REVIEW_PARALLELISM, REPO, Runner, TOKEN_ENV, TOKEN_PATH, _PARALLELISM, doc_root_rules,
)
from .github import GhError, GitHub, _default_runner, _utc_now_marker, github_api
from .units import is_epic, is_initiative, local_tasks_config_errors, session_placement
from .evidence import LOCAL_CI_SUITES


def worktree_path_for_branch(branch: str, runner: Runner = _default_runner,
                              base_repo: str = ".") -> Optional[str]:
    """Path of the live worktree with `branch` checked out, or None."""
    out = runner(["git", "-C", base_repo, "worktree", "list", "--porcelain"])
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def worktree_path(unit: str, number: int) -> str:
    """Configured worktree path for a unit (default `/tmp/sdlc-dev-<n>`, or
    `/tmp/sdlc-epic-<n>` for `unit="epic"`)."""
    w = PIPELINE["worktrees"]
    prefix = {"epic": w["epicPrefix"],
              "initiative": w.get("initiativePrefix", "sdlc-initiative-")}.get(unit, w["devPrefix"])
    return os.path.join(w["root"], f"{prefix}{number}")


def plugin_version_info(runner: Runner = _default_runner) -> dict:
    """The running plugin's manifest `version` and, when its root is its own git
    checkout, that checkout's HEAD `sha` (else None)."""
    info = {"root": PLUGIN_ROOT, "version": running_plugin_version(), "sha": None}
    try:
        top, sha = runner(["git", "-C", PLUGIN_ROOT, "rev-parse",
                           "--show-toplevel", "HEAD"]).split()
    except (GhError, ValueError):
        return info
    # An installed copy nested inside some other repo must not report that repo's HEAD.
    if os.path.realpath(top) == os.path.realpath(PLUGIN_ROOT):
        info["sha"] = sha
    return info


def running_plugin_version() -> Optional[str]:
    """The running plugin's manifest `version`, or None when unreadable."""
    try:
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json")) as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def version_key(version: Optional[str]) -> Optional[tuple]:
    """A sortable key for `v0.3.17` / `0.3.16-initiative.1`; None when unparseable. A suffixed
    tag is a build cut from its release, so it sorts after it and before the next release."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.\-]+))?", str(version or "").strip())
    if not m:
        return None
    core = tuple(int(x) for x in m.group(1).split("."))
    core += (0,) * max(0, 3 - len(core))
    suffix = tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                   for p in re.split(r"[.\-]", m.group(2) or "") if p)
    return (core, bool(suffix), suffix)


_PLUGIN_VERSION_MARKER = re.compile(r"<!--\s*sdlc:plugin-version\s+v=(\S+?)\s*(?:@[^>]*?)?-->")


# Tests turn it off globally (their fake clients lack comments); its own tests turn it on.
PLUGIN_VERSION_GUARD = True


def recorded_plugin_version(comments: list) -> Optional[str]:
    """The plugin version the latest `sdlc:plugin-version` marker records, or None."""
    for c in reversed(comments or []):
        m = _PLUGIN_VERSION_MARKER.search(c.get("body") or "")
        if m:
            return m.group(1)
    return None


def repo_plugin_pin(repo_path: str = ".") -> Optional[str]:
    """The driven repo's pinned plugin ref (`.claude/settings.json`
    `extraKnownMarketplaces.sdlc-pipeline.source.ref`), found walking up from `repo_path`."""
    path = os.path.abspath(repo_path or ".")
    while True:
        settings = os.path.join(path, ".claude", "settings.json")
        if os.path.isfile(settings):
            try:
                with open(settings) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return None
            source = (((data.get("extraKnownMarketplaces") or {}).get("sdlc-pipeline") or {})
                      .get("source") or {})
            return source.get("ref")
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _run_roots(by_number: dict, root: int) -> list:
    """`root` plus every Epic/Initiative above it."""
    roots, n = [], root
    while n is not None and n not in roots and len(roots) < 4:
        roots.append(n)
        n = ((by_number.get(n) or {}).get("parent") or {}).get("number")
    return [r for r in roots if r == root or (r in by_number and (
        is_epic(by_number[r]) or is_initiative(by_number[r])))]


def plugin_version_guard(gh: GitHub, root: int, by_number: dict, repo_path: str = ".") -> dict:
    """Refuse when a newer plugin last moved this run (`root` or an Epic/Initiative above it);
    else stamp `root` with this version when it is new there. A run's first stamp warns
    (`plugin_warning`) when this version is older than the driven repo's pin."""
    running = running_plugin_version()
    mine = version_key(running)
    if not PLUGIN_VERSION_GUARD or mine is None:
        return {}
    comments = {n: gh.issue_view(n).get("comments", []) for n in _run_roots(by_number, root)}
    for n, thread in comments.items():
        recorded = recorded_plugin_version(thread)
        if version_key(recorded) is not None and version_key(recorded) > mine:
            raise GhError(f"#{n} was last moved by sdlc plugin {recorded}; this session runs "
                          f"the older {running} -- continue this run from a session on "
                          f"{recorded} or newer")
    recorded = recorded_plugin_version(comments.get(root, []))
    out: dict = {}
    if recorded is None:
        pin = repo_plugin_pin(repo_path)
        if version_key(pin) is not None and version_key(pin) > mine:
            out["plugin_warning"] = (f"this session runs sdlc plugin {running}, older than the "
                                     f"repo's pin {pin}; start a new session to load {pin}")
    if recorded is None or version_key(recorded) is None or version_key(recorded) < mine:
        gh.issue_comment(root, f"sdlc plugin `{running}` now drives this run.\n\n"
                               f"<!-- sdlc:plugin-version v={running} @ {_utc_now_marker()} -->")
        out["plugin_version_recorded"] = running
    return out


def cmd_show_config(runner: Runner = _default_runner) -> dict:
    """Effective config (`pipeline` block over defaults) plus the running plugin version."""
    return {"repo": REPO, "docRoot": DOC_ROOT, "tokenPath": TOKEN_PATH, "tokenEnv": TOKEN_ENV,
            "requirementsDir": CONFIG.get("requirementsDir"),
            "docRoots": doc_root_rules(),
            "parallelism": {**_PARALLELISM,
                            "devLane": DEV_LANE_PARALLELISM,
                            "prReview": PR_REVIEW_PARALLELISM,
                            "designLane": DESIGN_LANE_PARALLELISM,
                            "crossEpicFootprintCheck": CROSS_EPIC_FOOTPRINT_CHECK,
                            "maxTasksPerRun": MAX_TASKS_PER_RUN,
                            "cloudSessions": CLOUD_SESSIONS},
            "requiredWorkflows": CONFIG["requiredWorkflows"],
            "localCiSuites": list(LOCAL_CI_SUITES),
            "plugin": plugin_version_info(runner),
            "session_placement": _session_placement_or_error(),
            "human_channel": _human_channel_or_error(),
            "github_api": _github_api_or_error(),
            **({"config_errors": local_tasks_config_errors()}
               if local_tasks_config_errors() else {}),
            **PIPELINE}


def _github_api_or_error() -> dict:
    try:
        return github_api()
    except GhError as e:
        return {"error": str(e)}


def _session_placement_or_error() -> dict:
    try:
        return session_placement()
    except GhError as e:
        return {"error": str(e)}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import worktrees  # noqa: E402,F401
from .human_channel import _human_channel_or_error  # noqa: E402
