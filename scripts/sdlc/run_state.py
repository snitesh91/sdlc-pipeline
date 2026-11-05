"""Per-run state: in-flight units, the run cap and config drift."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .config import CONFIG, MAX_TASKS_PER_RUN, PIPELINE, Runner, _find_config
from .github import GhError, WorkItemProvider, _default_runner, _utc_now_marker


# --- Run-scoped task cap (`parallelism.maxTasksPerRun`) -----------------------
# State outlives a single command, so it is one small JSON file per epic.
# `MAX_TASKS_PER_RUN` is read at call time so tests can override it on the module.

def _run_state_dir() -> str:
    """Run-state directory: `$SDLC_RUNS_DIR`, else `<worktrees.root>/.sdlc-runs`."""
    env = os.environ.get("SDLC_RUNS_DIR")
    if env:
        return env
    return os.path.join(PIPELINE["worktrees"]["root"], ".sdlc-runs")


def _run_state_path(epic: int) -> str:
    return os.path.join(_run_state_dir(), f"epic-{epic}.json")


def read_run_state(epic: int) -> Optional[dict]:
    """`{"run_id", "terminal": [issue numbers]}` for `epic`, or None.
    A corrupt file reads as None: restarting the count beats failing the run."""
    try:
        with open(_run_state_path(epic)) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    state.setdefault("terminal", [])
    return state


def _write_run_state(epic: int, state: dict) -> None:
    """Also stamps the epic and the orchestrating session, which the compaction hook matches on."""
    state["epic"] = epic
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        state["session_id"] = session
    os.makedirs(_run_state_dir(), exist_ok=True)
    with open(_run_state_path(epic), "w") as f:
        json.dump(state, f)


def note_in_flight(epic: int, run_id: Optional[str], units: dict) -> None:
    """Record units ({issue: stage}) this run handed out, until `record_terminal_unit`."""
    if not run_id or not units:
        return
    state = run_cap_state(epic, run_id)
    state.setdefault("in_flight", {}).update({str(n): stage for n, stage in units.items()})
    _write_run_state(epic, state)


def run_cap_state(epic: int, run_id: Optional[str]) -> Optional[dict]:
    """Run-state for `epic` under `run_id`, reset when the stored id is another run's.
    None when no `run_id` is given (the cap is opt-in)."""
    if not run_id:
        return None
    state = read_run_state(epic)
    if state is None or state.get("run_id") != run_id:
        state = {"run_id": run_id, "terminal": [], "config_digest": config_digest(CONFIG)}
        _write_run_state(epic, state)
    return state


def config_digest(config: dict) -> dict:
    """sha256 of each top-level config key's canonical JSON."""
    return {k: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"))
                              .encode()).hexdigest() for k, v in config.items()}


def _changed_keys(old: dict, new: dict) -> list:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def origin_config(runner: Runner = _default_runner) -> Optional[dict]:
    """The config file as committed on `origin/main` (no fetch), or None on any git failure."""
    path = _find_config()
    if not path:
        return None
    path = os.path.realpath(path)
    try:
        top = runner(["git", "-C", os.path.dirname(path), "rev-parse",
                      "--show-toplevel"]).strip()
        rel = os.path.relpath(path, os.path.realpath(top))
        config = json.loads(runner(["git", "-C", top, "show", f"origin/main:{rel}"]))
    except (GhError, OSError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def config_drift(epic: int, run_id: Optional[str],
                 runner: Runner = _default_runner) -> Optional[dict]:
    """`{source, keys}` when the config changed since the run began (`local`, reported once)
    or the checkout's differs from `origin/main` (`origin`, until they match); else None."""
    state = run_cap_state(epic, run_id)
    if state is None:
        return None
    current = config_digest(CONFIG)
    stored = state.get("config_digest")
    if stored != current:
        state["config_digest"] = current
        _write_run_state(epic, state)
        if stored is not None:
            return {"source": "local", "keys": _changed_keys(stored, current)}
    origin = origin_config(runner)
    keys = _changed_keys(current, config_digest(origin)) if origin is not None else []
    return {"source": "origin", "keys": keys} if keys else None


def archive_run_state(epic: int) -> Optional[dict]:
    """Mark a closed epic's run-state file `closed` rather than delete it: an Initiative run's
    cap sums every Epic's file under its run id (`run_completed`), and the hooks read the
    session's files for the guard and resume. `prune-stale` deletes it once no longer fresh."""
    state = read_run_state(epic)
    if state is None:
        return None
    if not state.get("closed"):
        state["closed"] = True
        state["closed_at"] = _utc_now_marker()
        with open(_run_state_path(epic), "w") as f:
            json.dump(state, f)
    return {"path": _run_state_path(epic), "archived": True}


def run_completed(state: Optional[dict]) -> list:
    """Units driven to a terminal state under `state`'s run id in any Epic's state file:
    one `--run-id` spans an Initiative's Epics and shares the cap across them."""
    if not state:
        return []
    done = list(state.get("terminal", []))
    try:
        names = sorted(os.listdir(_run_state_dir()))
    except OSError:
        return done
    for name in names:
        try:
            with open(os.path.join(_run_state_dir(), name)) as f:
                other = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(other, dict) and other.get("run_id") == state.get("run_id"):
            done.extend(n for n in other.get("terminal", []) if n not in done)
    return done


def run_cap_reached(state: Optional[dict]) -> bool:
    """Whether this run already drove `MAX_TASKS_PER_RUN` units to a terminal state (0 = never)."""
    return bool(state) and MAX_TASKS_PER_RUN > 0 \
        and len(run_completed(state)) >= MAX_TASKS_PER_RUN


def record_terminal_unit(gh: WorkItemProvider, issue: int,
                          run_id: Optional[str] = None, count: bool = True) -> Optional[dict]:
    """Idempotently count `issue` against its parent's tracked run (from `merge-pr` /
    `close-issue`). Returns the count summary, or None when no run is tracked.
    An empty run-state dir short-circuits before any tracker call. `count=False` only drops
    it from `in_flight` (a not-planned close is no completed work).

    With an explicit `run_id` (merge-pr's `--run-id`), the count is booked under that run,
    adopting it if the state file holds a different (e.g. stale probe) id -- so the terminal
    count is never attributed to a throwaway probe run. Without it, the state file's current
    run id stands (the most recent run that touched this epic)."""
    try:
        if not os.listdir(_run_state_dir()):
            return None
    except OSError:
        return None
    parent = None
    for entry in gh.issue_list():
        if entry["number"] == issue:
            parent = (entry.get("parent") or {}).get("number")
            break
    if parent is None:
        return None
    state = run_cap_state(parent, run_id) if run_id else read_run_state(parent)
    if state is None:
        return None
    if (count and issue not in state["terminal"]) or str(issue) in state.get("in_flight", {}):
        if count and issue not in state["terminal"]:
            state["terminal"].append(issue)
        state.get("in_flight", {}).pop(str(issue), None)
        _write_run_state(parent, state)
    # The cap is run-wide (`run_completed`), so the count reported against it is too.
    return {"epic": parent, "run_id": state.get("run_id"),
            "terminal_count": len(run_completed(state)),
            "epic_terminal_count": len(state["terminal"]), "cap": MAX_TASKS_PER_RUN}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import next_action  # noqa: E402,F401
