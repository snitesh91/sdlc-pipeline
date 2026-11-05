"""Cloud sessions: launch, status, nudge, heartbeat and detach."""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import CLOUD_SESSIONS, GATE_PENDING_STATUSES, PIPELINE, Runner, issue_branch
from .github import (
    GhError, GitHub, WorkItemProvider, _default_runner, strip_proxy_footer, with_scan,
)
from .units import (
    PLACEMENTS, _issue_info_lookup, cloud_label, current_stage, doc_root_for, epic_base_in,
    has_label, is_epic, is_epic_legacy, is_initiative, local_task_numbers, pipeline_status,
    resolve_initiative_profile, session_placement, task_local_reason, unit_placement,
)
from .evidence import epic_branch, find_gate_pr
from .footprint import _footprint_prefix, find_footprint, footprint_overlaps, parse_task_headings
from .worktrees import _fetch_origin, _is_ancestor
from .lanes import footprint_collision
from .design_gates import find_design_pr
from .issue_ops import _first_line, cmd_place


# --- Initiative -> cloud Epics ------------------------------------------------------------

# `waiting-local`: only local Tasks (`placement_scope`) remain; a laptop run takes them.
# `context-reset`: the run stopped at a phase boundary to shed context; relaunch-ready at once.
CLOUD_SESSION_OUTCOMES = ("closed", "waiting-human", "waiting-epic", "waiting-local", "stopped",
                          "abandoned", "context-reset")


# A live session's heartbeat (its launch marker's `heartbeat=`) is rewritten at most this often.
HEARTBEAT_MIN_SECONDS = 300


HEARTBEAT_STATES = ("working", "asking")


_CLOUD_SESSION_MARKER = re.compile(r"<!--\s*sdlc:cloud-session\s+([^>]*?)\s*-->")


_CLOUD_SESSION_END_MARKER = re.compile(r"<!--\s*sdlc:cloud-session-end\s+([^>]*?)\s*-->")


_CLOUD_NUDGE_MARKER = re.compile(r"<!--\s*sdlc:cloud-session-nudge\s+([^>]*?)\s*-->")


_MARKER_ATTR = re.compile(r"([a-z_]+)=(\S+)")


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


_SESSION_ID_LINE_RE = re.compile(r"session\s*id\s*[:=]\s*(session_[A-Za-z0-9_-]+)", re.I)


_SESSION_URL_RE = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9_-]+)")


_BARE_SESSION_RE = re.compile(r"\b(session_[A-Za-z0-9_-]{6,})")


NUDGES_BEFORE_ESCALATION = 2


def initiative_epics_placement(initiative: Optional[dict] = None) -> str:
    """Where a laptop Initiative run's Epics run: the matching `initiativeProfiles` entry's
    `placement`, else `pipeline.placement.initiativeEpics`."""
    key, value = "pipeline.placement.initiativeEpics", PIPELINE["placement"].get("initiativeEpics")
    own = resolve_initiative_profile(initiative)["placement"] if initiative else None
    if own is not None:
        key, value = "pipeline.initiativeProfiles[].placement", own
    value = str(value or "local").strip().lower()
    if value not in PLACEMENTS:
        raise GhError(f"{key}={value!r} is neither 'cloud' nor 'local' -- fix the config")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """A GitHub/marker timestamp as an aware datetime; None when absent or unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _default_claude_runner(argv: list, timeout: int = 300) -> str:
    """Run the `claude` CLI; stdout and stderr together. A timeout raises `ClaudeTimeout`
    carrying what it printed so far."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial = "\n".join(x.decode() if isinstance(x, bytes) else (x or "")
                            for x in (e.stdout, e.stderr))
        raise ClaudeTimeout(partial)
    except OSError as e:
        raise GhError(f"cannot run {argv[0]!r}: {e}")
    if proc.returncode != 0:
        raise GhError(f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr}")
    return f"{proc.stdout}\n{proc.stderr}"


def _pty_claude_runner(argv: list, timeout: int = 120) -> str:
    """Run `claude --cloud` on a pseudo-terminal, which it requires, and stop it once it has
    printed the session url; the cloud session outlives it. No url by `timeout` raises
    `ClaudeTimeout`."""
    import select
    master, slave = os.openpty()
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
    except OSError as e:
        os.close(master)
        raise GhError(f"cannot run {argv[0]!r}: {e}")
    finally:
        os.close(slave)
    buf, deadline = b"", time.monotonic() + timeout

    def seen() -> bool:
        return bool(_SESSION_URL_RE.search(_ANSI_RE.sub("", buf.decode(errors="replace"))))

    try:
        while not seen():
            left = deadline - time.monotonic()
            if left <= 0:
                raise ClaudeTimeout(buf.decode(errors="replace"))
            ready, _, _ = select.select([master], [], [], min(left, 1.0))
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # Linux raises EIO once the child closed the terminal
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(master)
    out = buf.decode(errors="replace")
    if not seen() and proc.returncode:
        raise GhError(f"command failed ({proc.returncode}): {' '.join(argv)}\n{out}")
    return out


class ClaudeTimeout(GhError):
    """The `claude` CLI outlived its timeout; `output` is what it printed until then."""

    def __init__(self, output: str):
        super().__init__("the claude CLI timed out")
        self.output = output


def parse_cloud_launch(text: str) -> dict:
    """`{id, url}` from `claude --cloud` output (`Session ID: session_...`,
    `View: https://claude.ai/code/session_...`), tolerant of ANSI colour, order and wording;
    a missing url is derived from the id. Either may be None."""
    text = _ANSI_RE.sub("", text or "")
    m = _SESSION_ID_LINE_RE.search(text)
    url = _SESSION_URL_RE.search(text)
    sid = m.group(1) if m else (url.group(1) if url else None)
    if sid is None:
        bare = _BARE_SESSION_RE.search(text)
        sid = bare.group(1) if bare else None
    return {"id": sid,
            "url": url.group(0) if url else (f"https://claude.ai/code/{sid}" if sid else None),
            "url_derived": bool(sid) and not url}


def cloud_sessions(comments: list) -> list:
    """Cloud sessions recorded on an Epic, oldest first: `{id, url, launched, ended, nudges}`
    plus, from the launch marker, `heartbeat` / `state` (the session's own liveness stamp) and
    the `overlap` / `manual` flags.
    `ended`: `{outcome, at, waits}` or None (`waits`: the issues it parked on); `nudges`: their
    timestamps. An end or nudge marker belongs to the latest session with its id, else the
    latest session."""
    sessions: list = []

    def owner(attrs: dict) -> Optional[dict]:
        return next((x for x in reversed(sessions) if x["id"] == attrs.get("id")),
                    sessions[-1] if sessions else None)

    for c in comments:
        body = c.get("body") or ""
        for m in _CLOUD_SESSION_MARKER.finditer(body):
            a = dict(_MARKER_ATTR.findall(m.group(1)))
            sessions.append({"id": a.get("id"), "url": a.get("url"),
                             "launched": a.get("launched") or c.get("createdAt"),
                             "heartbeat": a.get("heartbeat"), "state": a.get("state"),
                             "overlap": a.get("overlap") == "1", "manual": a.get("manual") == "1",
                             "ended": None, "nudges": []})
        for m in _CLOUD_SESSION_END_MARKER.finditer(body):
            a = dict(_MARKER_ATTR.findall(m.group(1)))
            target = owner(a)
            if target is None:
                target = {"id": a.get("id"), "url": None, "launched": None, "ended": None,
                          "nudges": []}
                sessions.append(target)
            waits = [int(x) for x in (a.get("waits") or "").split(",") if x.strip().isdigit()]
            target["ended"] = {"outcome": a.get("outcome"),
                               "at": a.get("ended") or c.get("createdAt"), "waits": waits}
        for m in _CLOUD_NUDGE_MARKER.finditer(body):
            a = dict(_MARKER_ATTR.findall(m.group(1)))
            target = owner(a)
            if target is not None:
                target["nudges"].append(a.get("at") or c.get("createdAt"))
    return sessions


def latest_cloud_session(comments: list) -> Optional[dict]:
    sessions = cloud_sessions(comments)
    return sessions[-1] if sessions else None


def _is_cloud_marker_comment(body: str) -> bool:
    return bool(_CLOUD_SESSION_MARKER.search(body) or _CLOUD_SESSION_END_MARKER.search(body)
                or _CLOUD_NUDGE_MARKER.search(body))


def _descendants(all_issues: list, root: int) -> list:
    kids: dict = {}
    for i in all_issues:
        p = (i.get("parent") or {}).get("number")
        if p:
            kids.setdefault(p, []).append(i)
    out, todo = [], [root]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.append(k)
            todo.append(k["number"])
    return out


def epic_last_activity(gh, epic: int, all_issues: list, comments: list, open_prs: list,
                       branch_commits: bool = True) -> Optional[datetime]:
    """The Epic's latest GitHub activity: its comments (cloud-session markers excluded), every
    descendant issue's `updatedAt`, its open PRs' `updatedAt` and, with `branch_commits`, the
    head commit of each in-progress child's branch and of `epic-<n>`."""
    times = [_parse_ts(c.get("createdAt")) for c in comments
             if not _is_cloud_marker_comment(c.get("body") or "")]
    kids = _descendants(all_issues, epic)
    times += [_parse_ts(k.get("updatedAt")) for k in kids]
    branches = {issue_branch(k["number"]) for k in kids} | {epic_branch(epic)}
    times += [_parse_ts(p.get("updatedAt")) for p in open_prs if p.get("headRefName") in branches]
    if branch_commits:
        live = [issue_branch(k["number"]) for k in kids
                if k.get("state") == "OPEN" and pipeline_status(k) == "in-progress"]
        for branch in [*live, epic_branch(epic)]:
            times.append(_parse_ts(gh.branch_last_commit_at(branch)))
    times = [t for t in times if t is not None]
    return max(times) if times else None


def epic_footprint(repo_path: str, epic_issue: dict,
                   runner: Runner = _default_runner,
                   gh: Optional[WorkItemProvider] = None) -> Optional[list]:
    """An Epic's footprint: the union of its Tasks' `## Footprint`s in `origin/epic-<n>`'s
    `lld.md`, else a `## Footprint` in the Epic's body; None when neither has one."""
    n = epic_issue["number"]
    try:
        text = runner(["git", "-C", repo_path, "show",
                       f"origin/{epic_branch(n)}:{doc_root_for(n, gh)}/epic-{n}/lld.md"])
    except GhError:
        text = None
    if text:
        found = [find_footprint(text[e["line_end"]:e["end"]]) for e in parse_task_headings(text)]
        found = [f for f in found if f is not None]
        if found:
            return sorted({p for f in found for p in f})
    return find_footprint(epic_issue.get("body") or "")


def _cloud_placed(issue: dict, by_number: dict) -> bool:
    label = cloud_label()
    parent = by_number.get((issue.get("parent") or {}).get("number"))
    return has_label(issue, label) or bool(parent and is_initiative(parent)
                                           and has_label(parent, label))


# Pipeline statuses that park a unit on the operator; a recorded wait clears once it leaves them.
# (`feedback-received` is not one: the operator answered and the unit is actionable again.)
_OPERATOR_PARKED_STATUSES = ("needs-human", "awaiting-human-review")


def _blocked_only_by(gh, number: int, among: list) -> bool:
    """Whether `number` has open blockers and every one of them is in `among`."""
    if not among:
        return False
    blockers = gh.blocked_by(number)
    return bool(blockers) and set(blockers) <= set(among)


def _gate_pr_answered(gh, comments: list, ended_at: Optional[datetime]) -> bool:
    """Whether the operator acted on the unit's gate or design PR after `ended_at`: it merged
    (a gate's approval leaves the issue's status and thread untouched) or it saw a review, a
    comment or a push since (its `updatedAt`)."""
    prs = {found[1] for found in (find_gate_pr(comments), find_design_pr(comments)) if found}
    for pr in sorted(prs):
        try:
            view = gh.pr_view(pr, "state,mergedAt,updatedAt")
        except GhError:
            continue
        merged = _parse_ts(view.get("mergedAt"))
        if view.get("state") == "MERGED" and (not ended_at or not merged or merged > ended_at):
            return True
        if merged and ended_at and merged <= ended_at:
            continue  # merged before the session ended: not an answer to this wait
        updated = _parse_ts(view.get("updatedAt"))
        if ended_at and updated and updated > ended_at:
            return True
    return False


def session_waits(gh, session: Optional[dict], by_number: dict,
                  epic: Optional[int] = None) -> Optional[dict]:
    """For an ended session that recorded what it parked on (`waits=`), whether that cleared:
    `{"outcome", "waits", "pending", "resolved"}`; None when it recorded nothing. A
    `waiting-epic` wait clears only when the waited Epic closes; under any other outcome a
    waited issue (the Epic itself included) clears when it closes, leaves the operator-parked
    statuses (needs-human, a pending gate), gets a comment after the end (the operator's answer
    on GitHub), or its gate/design PR merged or saw activity since (`_gate_pr_answered`). A `waiting-local` session (its waits are the Epic's local
    Tasks) clears once cloud work reappears under `epic` or no local Task is left open."""
    ended = (session or {}).get("ended") or {}
    waits = ended.get("waits") or []
    if ended.get("outcome") == "context-reset":
        return {"outcome": "context-reset", "waits": [], "pending": [], "resolved": True}
    if ended.get("outcome") == "waiting-local" and epic is not None:
        issues = list(by_number.values())
        pending = local_task_numbers(epic, issues)
        # A cloud Task blocked only by local Tasks is waiting on them too, not cloud work.
        remote = [i["number"] for i in issues if i["state"] == "OPEN"
                  and (i.get("parent") or {}).get("number") == epic and not is_epic(i)
                  and not task_local_reason(i)
                  and not _blocked_only_by(gh, i["number"], pending)]
        held = bool(pending) and not remote
        return {"outcome": "waiting-local", "waits": waits, "pending": pending if held else [],
                "resolved": not held}
    if not waits:
        return None
    ended_at = _parse_ts(ended.get("at"))
    pending = []
    for w in waits:
        issue = by_number.get(w)
        if issue is None or issue.get("state") == "CLOSED":
            continue
        if ended.get("outcome") == "waiting-epic":
            pending.append(w)
            continue
        if pipeline_status(issue) not in _OPERATOR_PARKED_STATUSES:
            continue
        comments = gh.issue_view(w).get("comments", [])
        answered = any((_parse_ts(c.get("createdAt")) or ended_at) > ended_at
                       for c in comments
                       if ended_at and not _is_cloud_marker_comment(c.get("body") or ""))
        if not answered and not _gate_pr_answered(gh, comments, ended_at):
            pending.append(w)
    return {"outcome": ended.get("outcome"), "waits": waits, "pending": pending,
            "resolved": not pending}


def cloud_epic_survey(gh, all_issues: list, candidates: list, repo_path: str = ".",
                      runner: Runner = _default_runner, relaunch: bool = False,
                      comments: Optional[dict] = None, open_prs: Optional[list] = None,
                      allow_overlap: bool = False) -> dict:
    """Which of `candidates` (Epic numbers) may get a cloud session now. `running`: open Epics,
    repo-wide, whose latest session has no end marker; they hold the `parallelism.cloudSessions`
    slots. A candidate is `launchable` when it is open, not legacy, not running, not blocked by
    an open issue, not idle since its last session ended (unless `relaunch`), within the cap,
    and its footprint is disjoint from the running and already-chosen Epics' (unverifiable when
    theirs is known and its own is not) and from every cloud Epic whose work is not on its base
    yet (`settled_overlap`; `needs_base_sync`). Footprint entries matching
    `placement.softOverlapPaths` never collide; `allow_overlap` (the operator's override) skips
    both footprint checks. An Epic whose last session recorded its waits (`session_waits`) is
    launchable once they cleared and waits on them until then, GitHub activity or not; one that
    recorded none waits for new GitHub activity. Launchable Epics with an ended session are also
    in `relaunch`. Every other open candidate is in `waiting` with a reason."""
    all_issues = with_scan(gh, all_issues, "cloud")  # running and settled cloud Epics, repo-wide
    by_number = {i["number"]: i for i in all_issues}
    comments = {} if comments is None else comments

    def comments_of(n: int) -> list:
        if n not in comments:
            comments[n] = gh.issue_view(n).get("comments", [])
        return comments[n]

    open_cloud = [i["number"] for i in all_issues if i["state"] == "OPEN" and is_epic(i)
                  and _cloud_placed(i, by_number)]
    scan = sorted({*open_cloud, *(n for n in candidates
                                  if (by_number.get(n) or {}).get("state") == "OPEN")})
    sessions = {n: latest_cloud_session(comments_of(n)) for n in scan}
    running = [n for n in scan if sessions[n] and not sessions[n]["ended"]]
    cap = CLOUD_SESSIONS
    fetched, footprints, warnings = [False], {}, []

    def footprint_of(n: int) -> Optional[list]:
        if not fetched[0]:
            fetched[0] = True
            try:
                _fetch_origin(repo_path, runner)
            except GhError as e:
                warnings.append(f"git fetch failed ({_first_line(str(e))}); footprints read "
                                f"from the last fetched origin refs")
        if n not in footprints:
            footprints[n] = hard_footprint(epic_footprint(repo_path, by_number[n], runner, gh=gh))
        return footprints[n]

    settled: list = []

    def settled_overlap(n: int) -> Optional[dict]:
        """An Epic no longer running whose footprint overlaps `n`'s and whose work is not yet
        on `n`'s base: an open one whose session ended, or a closed one whose merge commit is
        not an ancestor of `origin/<base>` (`needs_base_sync`)."""
        mine = footprint_of(n)
        if not mine:
            return None
        if not settled:
            settled.append([i["number"] for i in all_issues if is_epic(i)
                            and _cloud_placed(i, by_number) and i["number"] not in running
                            and (i["state"] == "CLOSED"
                                 or (sessions.get(i["number"]) or {}).get("ended"))])
        base = epic_base_in(by_number, n)
        for m in settled[0]:
            if m == n or m in launchable:
                continue
            theirs = footprint_of(m)
            if not theirs or not footprint_overlaps(mine, theirs):
                continue
            if by_number[m]["state"] != "CLOSED":
                return {"reason": f"footprint overlaps #{m} (open; its cloud session ended "
                                  f"before it merged)"}
            merge = _epic_merge_commit(gh, m)
            if merge is None or _is_ancestor(repo_path, merge, f"origin/{base}", runner):
                continue
            return {"reason": f"footprint overlaps closed #{m}, whose merge {merge[:12]} is not "
                              f"on origin/{base} yet -- bring it into `{base}` first (an "
                              f"initiative branch: `sync-branch <i> --unit initiative`), then "
                              f"launch",
                    "needs_base_sync": {"epic": m, "merge_commit": merge, "base": base}}
        return None

    launchable, waiting = [], []
    for n in sorted(set(candidates)):
        epic = by_number.get(n)
        if epic is None or epic["state"] != "OPEN" or n in running:
            continue
        reason = None
        if is_epic_legacy(epic):
            reason = "not driven (legacy profile)"
        else:
            blockers = gh.blocked_by(n)
            if blockers:
                reason = f"blocked by {', '.join(f'#{b}' for b in blockers)}"
        session = sessions.get(n)
        waits = session_waits(gh, session, by_number, epic=n) if session else None
        if reason is None and waits and not waits["resolved"] and not relaunch:
            on = ', '.join(f'#{w}' for w in waits['pending'])
            reason = (f"waiting on its local Tasks ({on}) -- a laptop `/sdlc:run {n}` drives "
                      f"them; it relaunches once cloud work reappears"
                      if waits["outcome"] == "waiting-local" else
                      f"its cloud session ended ({waits['outcome']}) waiting on {on} -- it "
                      f"relaunches once that clears (`--relaunch` to start one now)")
        elif reason is None and session and session["ended"] and not waits and not relaunch:
            ended_at = _parse_ts(session["ended"].get("at"))
            if open_prs is None:
                open_prs = gh.open_prs()
            last = epic_last_activity(gh, n, all_issues, comments_of(n), open_prs,
                                      branch_commits=False)
            if ended_at is None or last is None or last <= ended_at:
                reason = (f"its cloud session ended ({session['ended'].get('outcome')}) with no "
                          f"GitHub activity since -- `launch-cloud-epic {n} --relaunch` to "
                          f"start another")
        if reason is None and len(running) + len(launchable) >= cap:
            reason = f"cloud session cap reached ({len(running) + len(launchable)}/{cap})"
        if reason is None and not allow_overlap:
            others = [(m, fp) for m in [*running, *launchable]
                      for fp in [footprint_of(m)] if fp is not None]
            if others:
                hit = footprint_collision(footprint_of(n), others)
                if hit:
                    reason = hit
        sync = None
        if reason is None and not allow_overlap:
            sync = settled_overlap(n)
            reason = (sync or {}).get("reason")
        if reason:
            waiting.append({"epic": n, "reason": reason,
                            **({"needs_base_sync": sync["needs_base_sync"]}
                               if sync and sync.get("needs_base_sync") else {})})
        else:
            launchable.append(n)
    result = {"cap": cap, "free": max(0, cap - len(running)),
              "running": [{"epic": n, "session": sessions[n]} for n in running],
              "launchable": launchable, "waiting": waiting,
              "relaunch": [n for n in launchable if (sessions.get(n) or {}).get("ended")]}
    if warnings:
        result["warnings"] = warnings
    return result


def hard_footprint(footprint: Optional[list]) -> Optional[list]:
    """`footprint` without the `placement.softOverlapPaths` entries (None stays None)."""
    soft = PIPELINE["placement"].get("softOverlapPaths") or []
    if footprint is None or not soft:
        return footprint
    return [p for p in footprint
            if not any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(_footprint_prefix(p), g)
                       for g in soft)]


def _epic_merge_commit(gh, epic: int) -> Optional[str]:
    """The merge commit of `epic`'s merged integration PR; None when it never merged."""
    try:
        prs = gh.pr_list_for_branch(epic_branch(epic), state="merged")
        if not prs:
            return None
        return ((gh.pr_view(prs[0]["number"], "mergeCommit").get("mergeCommit") or {})
                .get("oid"))
    except GhError:
        return None


def _initiative_epics(all_issues: list, initiative: int) -> list:
    return sorted((i for i in all_issues if is_epic(i)
                   and (i.get("parent") or {}).get("number") == initiative),
                  key=lambda i: i["number"])


def _cloud_epics_reason(survey: dict, cut_epics: list) -> str:
    """The Initiative `none` reason in cloud mode: each open Epic's cloud state."""
    parts = [f"#{r['epic']} running in the cloud ({(r['session'] or {}).get('url') or 'url unknown'})"
             for r in survey["running"]]
    parts += [f"#{w['epic']} {w['reason']}" for w in survey["waiting"]]
    closed = sum(1 for e in cut_epics if e["state"] == "CLOSED")
    return (f"cloud mode: {len(cut_epics) - closed} cut Epic(s) open, none to launch now: "
            f"{'; '.join(parts) or 'none'}" + (f" ({closed} closed)" if closed else "")
            + ". Re-run `/sdlc:run` on the Initiative (or watch `cloud-status`) to launch the "
              "rest as these close.")


def cmd_launch_cloud_epic(gh: GitHub, epic: int, repo_path: str = ".", relaunch: bool = False,
                          runner: Runner = _default_runner,
                          claude: Callable[[list], str] = _pty_claude_runner,
                          allow_overlap: bool = False) -> dict:
    """Place `epic` in the cloud and start `claude --cloud "/sdlc:run <epic>"`, recording a
    session marker on it. Idempotent: an Epic whose latest session has no end marker is never
    relaunched (`already_running`). Refuses (exit 0) an Epic `cloud_epic_survey` would not
    launch, and on `place`'s refusal. `allow_overlap` (operator only) skips the footprint
    checks and says so in the marker (`overlap=1`); the cap still holds."""
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    issue = by_number.get(epic)
    if issue is None or not is_epic(issue):
        raise GhError(f"#{epic} is not an Epic -- launch-cloud-epic takes an Epic number")
    result = {"epic": epic, "launched": False}
    if issue["state"] != "OPEN":
        return {**result, "refused": True, "reason": f"Epic #{epic} is closed"}
    comments = {epic: gh.issue_view(epic).get("comments", [])}
    session = latest_cloud_session(comments[epic])
    if session and not session["ended"]:
        return {**result, "already_running": True, "session": session}
    survey = cloud_epic_survey(gh, all_issues, [epic], repo_path=repo_path, runner=runner,
                               relaunch=relaunch, comments=comments, allow_overlap=allow_overlap)
    if epic not in survey["launchable"]:
        wait = next((w for w in survey["waiting"] if w["epic"] == epic), {})
        return {**result, "refused": True, "reason": wait.get("reason", "not launchable"),
                **({"needs_base_sync": wait["needs_base_sync"]}
                   if wait.get("needs_base_sync") else {}),
                "running": [r["epic"] for r in survey["running"]], "cap": survey["cap"]}
    placed = cmd_place(gh, epic, "cloud", repo_path=repo_path, runner=runner)
    if placed.get("refused"):
        return {**result, "refused": True, "reason": placed["reason"], "place": placed}
    argv = ["claude", "--cloud", f"/sdlc:run {epic}"]
    try:
        output = claude(argv)
    except ClaudeTimeout as e:
        output = e.output
    parsed = parse_cloud_launch(output)
    sid, url = parsed["id"] or "unknown", parsed["url"] or "unknown"
    overlap = (" Launched with `--allow-overlap` (operator): its footprint was not checked "
               "against the other cloud Epics'." if allow_overlap else "")
    gh.issue_comment(epic,
        f"☁️ Cloud session launched for this Epic: {parsed['url'] or 'its URL was not reported'}"
        f".\n\nIt drives the Epic until it closes, asking the operator in session, then posts "
        f"an end marker. From the laptop: `cloud-status {epic}`.{overlap}\n\n"
        f"<!-- sdlc:cloud-session id={sid} url={url} launched={_iso(_utc_now())}"
        f"{' overlap=1' if allow_overlap else ''} -->")
    out = {**result, "launched": True, "session": {"id": parsed["id"], "url": parsed["url"]},
           "place": placed}
    if session and session["ended"]:
        out["relaunched"] = True
    if allow_overlap:
        out["allow_overlap"] = True
    if parsed["url_derived"]:
        out["url_derived"] = True
    if not parsed["id"]:
        tail = "\n".join(_ANSI_RE.sub("", output or "").strip().splitlines()[-10:])
        out.update(ok=False, output_tail=tail,
                   reason="claude --cloud printed no session id; the marker records id=unknown "
                          "so the Epic is not relaunched -- find the session on claude.ai/code "
                          "and tell the operator")
    return out


def _epic_cloud_status(gh, epic: int, by_number: dict, all_issues: list, comments: list,
                       open_prs: list, now: datetime) -> dict:
    issue = by_number[epic]
    session = latest_cloud_session(comments)
    kids = [i for i in all_issues if (i.get("parent") or {}).get("number") == epic]
    open_kids = [k for k in kids if k["state"] == "OPEN"]
    by_stage, by_status = {}, {}
    for k in open_kids:
        stage, status = current_stage(k) or "none", pipeline_status(k) or "none"
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    heads = {issue_branch(k["number"]): k["number"] for k in kids}
    heads[epic_branch(epic)] = epic
    prs = [{"pr": p["number"], "issue": heads[p["headRefName"]], "title": p.get("title"),
            "draft": bool(p.get("isDraft")), "updated": p.get("updatedAt")}
           for p in open_prs if p.get("headRefName") in heads]
    gates = [{"issue": k["number"], "status": pipeline_status(k)} for k in open_kids
             if pipeline_status(k) in GATE_PENDING_STATUSES]
    needs_human = [k["number"] for k in [issue, *open_kids] if pipeline_status(k) == "needs-human"]
    last = epic_last_activity(gh, epic, all_issues, comments, open_prs)
    launched = _parse_ts((session or {}).get("launched"))
    heartbeat = _parse_ts((session or {}).get("heartbeat"))
    last = max((t for t in (last, launched, heartbeat) if t is not None), default=None)
    is_open = issue["state"] == "OPEN"
    live = bool(session) and not session["ended"]
    idle = round((now - last).total_seconds() / 60, 1) if last else None
    stall_minutes = PIPELINE["placement"].get("stallMinutes", 90)
    # A session asking the operator in session is waiting on them: it stalls only after a
    # longer bound, so one that died mid-question still frees its slot.
    asking = live and (session or {}).get("state") == "asking"
    bound = stall_minutes * (PIPELINE["placement"].get("askingStallFactor", 6) if asking else 1)
    stalled = is_open and live and idle is not None and idle >= bound
    waits = (session_waits(gh, session, by_number, epic=epic)
             if session and session["ended"] else None)
    nudges = [t for t in (session or {}).get("nudges", [])
              if last is None or (_parse_ts(t) or now) > last]
    return {"epic": epic, "title": issue.get("title"), "state": issue["state"],
            "session": session, "live": live, "end_marker": bool(session and session["ended"]),
            "children": {"open": len(open_kids), "closed": len(kids) - len(open_kids),
                         "by_stage": by_stage, "by_status": by_status},
            "open_prs": prs, "gates_pending": gates, "needs_human": needs_human,
            "last_activity": _iso(last) if last else None, "idle_minutes": idle,
            "heartbeat": (session or {}).get("heartbeat"), "asking": asking,
            "stalled": stalled, "nudges_since_activity": len(nudges),
            "escalate": stalled and len(nudges) >= NUDGES_BEFORE_ESCALATION,
            "waits": waits,
            "relaunch_ready": bool(is_open and waits and waits["resolved"])}


def cmd_cloud_status(gh, number: int, repo_path: str = ".", runner: Runner = _default_runner,
                     now: Optional[datetime] = None) -> dict:
    """Read-only state of an Initiative's cloud Epics (or one Epic): per Epic its session,
    children by stage/status, open PRs, pending gates, needs-human, last GitHub activity,
    `stalled` and `escalate`. On an Initiative in cloud mode also `launchable`, and
    `attention` when something needs the laptop (launchable, stalled, all Epics closed)."""
    now = now or _utc_now()
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    issue = by_number.get(number)
    if issue is None or not (is_epic(issue) or is_initiative(issue)):
        raise GhError(f"#{number} is not an Epic or Initiative")
    epics = _initiative_epics(all_issues, number) if is_initiative(issue) else [issue]
    comments = {e["number"]: gh.issue_view(e["number"]).get("comments", []) for e in epics}
    open_prs = gh.open_prs()
    tracked = [e for e in epics if latest_cloud_session(comments[e["number"]])
               or _cloud_placed(e, by_number)]
    rows = [_epic_cloud_status(gh, e["number"], by_number, all_issues, comments[e["number"]],
                               open_prs, now) for e in tracked]
    result = {"number": number, "kind": "initiative" if is_initiative(issue) else "epic",
              "now": _iso(now), "stall_minutes": PIPELINE["placement"].get("stallMinutes", 90),
              "epics": rows,
              "not_in_cloud": [e["number"] for e in epics if e not in tracked]}
    attention = any(r["stalled"] or r["relaunch_ready"] for r in rows)
    if is_initiative(issue):
        result["all_closed"] = bool(epics) and all(e["state"] == "CLOSED" for e in epics)
        attention = attention or result["all_closed"]
        if initiative_epics_placement(issue) == "cloud":
            survey = cloud_epic_survey(gh, all_issues, [e["number"] for e in epics],
                                       repo_path=repo_path, runner=runner, comments=comments,
                                       open_prs=open_prs)
            result.update(launchable=survey["launchable"], waiting=survey["waiting"],
                          cap=survey["cap"], running=[r["epic"] for r in survey["running"]])
            attention = attention or bool(survey["launchable"])
    result["attention"] = attention
    return result


def cmd_nudge_cloud_epic(gh, epic: int, force: bool = False,
                         claude: Callable[[list], str] = _default_claude_runner,
                         now: Optional[datetime] = None) -> dict:
    """Send `continue: /sdlc:run <epic>` to the Epic's stalled cloud session and record the
    nudge. After `NUDGES_BEFORE_ESCALATION` nudges with no activity since, it sends nothing
    and returns `escalate: true` (ask the operator). `force` nudges a session not yet stalled."""
    now = now or _utc_now()
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    if epic not in by_number or not is_epic(by_number[epic]):
        raise GhError(f"#{epic} is not an Epic")
    comments = gh.issue_view(epic).get("comments", [])
    status = _epic_cloud_status(gh, epic, by_number, all_issues, comments, gh.open_prs(), now)
    session = status["session"]
    result = {"epic": epic, "nudged": False, "escalate": False, "status": status}
    if not status["live"] or status["state"] != "OPEN":
        return {**result, "refused": True,
                "reason": "no live cloud session on this Epic (none launched, ended, or the "
                          "Epic is closed)"}
    if not status["stalled"] and not force:
        return {**result, "reason": f"not stalled (idle {status['idle_minutes']} min, threshold "
                                    f"{PIPELINE['placement'].get('stallMinutes', 90)})"}
    sid = session.get("id")
    if status["nudges_since_activity"] >= NUDGES_BEFORE_ESCALATION or not sid or sid == "unknown":
        return {**result, "escalate": True,
                "reason": (f"{status['nudges_since_activity']} nudge(s) with no GitHub activity "
                           f"since" if sid and sid != "unknown" else "the session id is unknown")
                          + f" -- ask the operator (session {session.get('url')})"}
    argv = ["claude", "-p", f"continue: /sdlc:run {epic}", "--cloud", sid]
    timed_out = False
    try:
        claude(argv)
    except ClaudeTimeout:
        timed_out = True
    count = status["nudges_since_activity"] + 1
    gh.issue_comment(epic,
        f"👉 Nudged the cloud session ({count}/{NUDGES_BEFORE_ESCALATION}): no GitHub activity "
        f"for {status['idle_minutes']} min.\n\n"
        f"<!-- sdlc:cloud-session-nudge id={sid} n={count} at={_iso(now)} -->")
    return {**result, "nudged": True, "nudges": count,
            **({"reply_timed_out": True} if timed_out else {})}


def cmd_end_cloud_session(gh: WorkItemProvider, epic: int, outcome: str,
                          note: Optional[str] = None, waits_on: Optional[list] = None) -> dict:
    """Post the end marker for the Epic's latest cloud session (id `unknown` when none was
    launched through `launch-cloud-epic`). Idempotent on an already-ended session. The marker
    records what the session waits on (`waits=`), so the laptop relaunches it once that clears
    (`session_waits`): `waiting-epic` needs `waits_on` (the Epics it waits for); `waiting-human`
    takes `waits_on` or defaults to the Epic's open units parked on the operator (needs-human,
    an open gate)."""
    if outcome not in CLOUD_SESSION_OUTCOMES:
        raise GhError(f"--outcome must be one of {', '.join(CLOUD_SESSION_OUTCOMES)}")
    if not is_epic(gh.issue_epic_info(epic)):
        raise GhError(f"#{epic} is not an Epic")
    waits = sorted({int(w) for w in (waits_on or [])})
    if outcome == "waiting-epic":
        if not waits:
            raise GhError("--outcome waiting-epic needs --waits-on <epic> (the Epic it waits for)")
        for w in waits:
            if not is_epic(gh.issue_epic_info(w)):
                raise GhError(f"--waits-on #{w} is not an Epic")
    session = latest_cloud_session(gh.issue_view(epic).get("comments", []))
    if session and session["ended"]:
        return {"epic": epic, "already_ended": True, "session": session}
    if outcome == "waiting-local" and not waits:
        waits = local_task_numbers(epic, gh.issue_list())
    if outcome == "waiting-human" and not waits:
        all_issues = gh.issue_list()
        epic_node = next((i for i in all_issues if i["number"] == epic), None)
        waits = sorted(i["number"] for i in [epic_node, *_descendants(all_issues, epic)]
                       if i and i["state"] == "OPEN"
                       and pipeline_status(i) in _OPERATOR_PARKED_STATUSES)
    sid = (session or {}).get("id") or "unknown"
    text = f" {note.strip()}" if note and note.strip() else ""
    on = f" Waits on {', '.join(f'#{w}' for w in waits)}." if waits else ""
    gh.issue_comment(epic,
        f"☁️ Cloud session ended — outcome `{outcome}`.{text}{on}\n\n"
        f"<!-- sdlc:cloud-session-end id={sid} outcome={outcome} ended={_iso(_utc_now())}"
        f"{' waits=' + ','.join(map(str, waits)) if waits else ''} -->")
    return {"epic": epic, "ended": True, "id": sid, "outcome": outcome,
            **({"waits": waits} if waits else {})}


def cmd_record_cloud_session(gh: WorkItemProvider, epic: int, session: str,
                             note: Optional[str] = None) -> dict:
    """Register a cloud session the operator started by hand (`claude --cloud`, the claude.ai
    UI) on `epic`: the cloud label if missing, then the launch marker (`manual=1`), so
    `cloud-status`, the cap and the stall checks see it. `session` is its url or `session_...`
    id. Refuses (exit 0) while another session is live on the Epic; the same id is a no-op."""
    if not is_epic(gh.issue_epic_info(epic)):
        raise GhError(f"#{epic} is not an Epic")
    parsed = parse_cloud_launch(session)
    if not parsed["id"]:
        raise GhError(f"--session {session!r} names no session_... id or claude.ai/code url")
    view = gh.issue_view(epic)
    latest = latest_cloud_session(view.get("comments", []))
    if latest and not latest["ended"]:
        if latest["id"] == parsed["id"]:
            return {"epic": epic, "recorded": False, "already_recorded": True, "session": latest}
        return {"epic": epic, "recorded": False, "refused": True, "session": latest,
                "reason": f"session {latest['id']} is still live on #{epic}: end-mark it "
                          f"(`end-cloud-session {epic} --outcome stopped`) before recording another"}
    labelled = cloud_label() in [(l.get("name") if isinstance(l, dict) else l)
                                 for l in view.get("labels", [])]
    if not labelled:
        gh.ensure_label(cloud_label())
        gh.issue_edit(epic, add_labels=[cloud_label()])
    text = f" {note.strip()}" if note and note.strip() else ""
    gh.issue_comment(epic,
        f"☁️ Cloud session recorded for this Epic (started by hand): {parsed['url']}.{text}\n\n"
        f"<!-- sdlc:cloud-session id={parsed['id']} url={parsed['url']} "
        f"launched={_iso(_utc_now())} manual=1 -->")
    return {"epic": epic, "recorded": True, "session": {"id": parsed["id"], "url": parsed["url"]},
            "labelled": not labelled}


def _live_session_comment(comments: list) -> Optional[dict]:
    """The comment holding the latest session's launch marker while that session is live."""
    session = latest_cloud_session(comments)
    if not session or session["ended"]:
        return None
    for c in reversed(comments):
        for m in _CLOUD_SESSION_MARKER.finditer(c.get("body") or ""):
            if dict(_MARKER_ATTR.findall(m.group(1))).get("id") == session["id"]:
                return c
    return None


def cmd_cloud_heartbeat(gh: WorkItemProvider, epic: int, state: str = "working",
                        now: Optional[datetime] = None, force: bool = False) -> dict:
    """Stamp `heartbeat=<now> state=<state>` into the live session's launch marker (an edit of
    that comment, never a new one); `cloud-status` judges a stall from it. `asking` marks a
    session waiting on the operator's in-session answer (never `stalled`). The same state is
    re-stamped at most every `HEARTBEAT_MIN_SECONDS` unless `force`."""
    if state not in HEARTBEAT_STATES:
        raise GhError(f"--state must be one of {', '.join(HEARTBEAT_STATES)}")
    now = now or _utc_now()
    comments = gh.issue_view(epic).get("comments", [])
    comment = _live_session_comment(comments)
    if comment is None:
        return {"epic": epic, "stamped": False, "reason": "no live cloud session on this Epic"}
    body = comment.get("body") or ""
    m = list(_CLOUD_SESSION_MARKER.finditer(body))[-1]
    attrs = dict(_MARKER_ATTR.findall(m.group(1)))
    last = _parse_ts(attrs.get("heartbeat"))
    if (not force and attrs.get("state") == state and last is not None
            and (now - last).total_seconds() < HEARTBEAT_MIN_SECONDS):
        return {"epic": epic, "stamped": False, "reason": "stamped recently", "heartbeat": attrs["heartbeat"]}
    attrs.update(heartbeat=_iso(now), state=state)
    marker = "<!-- sdlc:cloud-session " + " ".join(f"{k}={v}" for k, v in attrs.items()) + " -->"
    # The proxy re-appends its footer on every write; strip the old one so edits never stack
    # footers (the REST provider strips on read, the GraphQL one does not).
    gh.issue_comment_edit(comment, strip_proxy_footer(body[:m.start()] + marker + body[m.end():]))
    return {"epic": epic, "stamped": True, "heartbeat": attrs["heartbeat"], "state": state}


def cloud_heartbeat_quietly(gh, number: int) -> Optional[str]:
    """In a cloud session, `cloud-heartbeat` the Epic `number` belongs to; never raises.
    Returns a warning line on failure, else None."""
    try:
        if session_placement()["placement"] != "cloud":
            return None
        epic = unit_placement(number, _issue_info_lookup(gh)).get("epic")
        if epic:
            cmd_cloud_heartbeat(gh, epic)
    except Exception as e:  # liveness bookkeeping must never fail a stage step
        return f"cloud heartbeat failed: {_first_line(str(e))}"
    return None


def cmd_detach_epic(gh: GitHub, epic: int, reason: Optional[str] = None) -> dict:
    """Take an Epic out of its Initiative: drop the sub-issue link the Initiative loop walks
    and every `blockedBy` edge between it and the Initiative's other Epics (either direction),
    then comment on both. The Epic stays open; close it with `close-issue --not-planned` or
    run it bare. Refuses (exit 0) an issue that is not an Epic under an Initiative."""
    issues = {i["number"]: i for i in gh.issue_list()}
    entry = issues.get(epic)
    if entry is None:
        raise GhError(f"issue #{epic} not found in the repo issue list")
    parent = (entry.get("parent") or {}).get("number")
    if not is_epic(entry) or parent is None or not is_initiative(issues.get(parent) or {}):
        return {"epic": epic, "detached": False, "refused": True,
                "reason": f"#{epic} is not an Epic under an Initiative -- nothing to detach"}
    siblings = {n for n, i in issues.items()
                if (i.get("parent") or {}).get("number") == parent and is_epic(i) and n != epic}
    removed = []
    for blocker in gh.blocked_by(epic):
        if blocker in siblings:
            gh.remove_blocked_by(epic, blocker)
            removed.append({"issue": epic, "blocked_by": blocker})
    for dependent in gh.blocking(epic):
        if dependent in siblings:
            gh.remove_blocked_by(dependent, epic)
            removed.append({"issue": dependent, "blocked_by": epic})
    gh.remove_sub_issue(parent, epic)
    why = f" — {reason}" if reason else ""
    gh.issue_comment(epic, f"🔗 Detached from Initiative #{parent}{why}. It is no longer in "
                           f"that Initiative's Epic loop; its own state is unchanged.")
    gh.issue_comment(parent, f"🔗 Epic #{epic} detached from this Initiative{why}.")
    return {"epic": epic, "initiative": parent, "detached": True, "removed_edges": removed}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import issue_admin  # noqa: E402,F401
