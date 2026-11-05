"""Issue status commands, unit cleanup, `prune-stale` and `place`."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Optional

from .config import (
    CONFIG, EPIC_BRANCH_PREFIX, INITIATIVE_BRANCH_PREFIX, ISSUE_BRANCH_PREFIX, PIPELINE,
    PIPELINE_STATUS_FIELD_NAMES, Runner,
)
from .github import (
    GhError, GitHub, WorkItemProvider, _default_runner, _utc_now_marker, focus_issues,
)
from .units import (
    PLACEMENTS, classify_unit_from_issue, cloud_label, cloud_skips_local_cleanup, current_stage,
    epic_base_in, has_label, is_epic, is_initiative, label_names, local_label, pipeline_status,
    sort_key, task_local_reason,
)
from .evidence import _NEEDS_HUMAN_REASON, integration_base, integration_base_in, unit_branch
from .run_state import _run_state_dir, record_terminal_unit
from .plugin_version import worktree_path_for_branch
from .worktrees import (
    _docker_cleanup_enabled, _landed, _run_retry_transient, cmd_release_review_worktree,
    _remove_worktree, docker_sweep, release_detached_dev_worktree, release_worktree,
    unit_docker_prefix, worktree_entries,
)
from .checks import _close_realised_issues, _release_parked_unit


def cmd_mark_blocked(gh: GitHub, issue: int, dep: int, repo_path: str = ".") -> dict:
    """Add a native blockedBy edge on `dep`, reset Pipeline Status to `todo` (so the
    parked unit doesn't read as a crashed in-progress run) and release its worktree and
    docker stack. Idempotent on the edge: one already set (by `add-blocked-by`) is kept."""
    try:
        gh.add_blocked_by(issue, dep)
    except GhError as e:
        # The edge already exists: REST answers 422 "has already been taken" (seen
        # 2026-10-18); the GraphQL wording ("already exists"/"already blocked") is assumed.
        if not re.search(r"already (been taken|exists|blocked)", str(e)):
            raise
    gh.set_pipeline_status_field(issue, "todo")
    gh.issue_comment(issue, f"⏸️ Blocked — waiting on #{dep} to merge.")
    return {"issue": issue, "status": "blocked", "on": dep,
            **_release_parked_unit(issue, repo_path, gh._run)}


def cmd_mark_needs_human(gh: GitHub, issue: int, reason: str, repo_path: str = ".",
                         classifier_denied: Optional[str] = None,
                         run_id: Optional[str] = None) -> dict:
    """Park the unit as needs-human with `reason`; release its worktree and docker stack.
    `classifier_denied` (the denied step) parks it for the next run to resume instead: the
    worktree, with any unpushed work, is kept."""
    gh.set_pipeline_status_field(issue, "needs-human")
    marker = (f"\n\n<!-- sdlc:classifier-denied step={classifier_denied} "
              f"run={run_id or '-'} @ {_utc_now_marker()} -->" if classifier_denied else "")
    gh.issue_comment(issue, f"🙋 Needs human input — {reason}{marker}")
    if classifier_denied:
        return {"issue": issue, "status": "needs-human", "classifier_denied": classifier_denied,
                "resumes": "on the next run (a new --run-id); the worktree is kept"}
    return {"issue": issue, "status": "needs-human",
            **_release_parked_unit(issue, repo_path, gh._run)}


_CLASSIFIER_DENIED_MARKER = re.compile(
    r"<!--\s*sdlc:classifier-denied\s+step=(\S+)\s+run=(\S+)")


def _classifier_denied_resume(gh: GitHub, children: list, run_id: Optional[str]) -> Optional[dict]:
    """A needs-human child parked by an auto-mode classifier denial in an earlier run: flip it
    back to in-progress and resume it. Never within the run that parked it (it would loop)."""
    if not run_id:
        return None
    for issue in sorted(children, key=sort_key):
        if pipeline_status(issue) != "needs-human":
            continue
        comments = gh.issue_view(issue["number"]).get("comments") or []
        latest = next((c for c in reversed(comments)
                       if _NEEDS_HUMAN_REASON.search(c.get("body", ""))), None)
        m = latest and _CLASSIFIER_DENIED_MARKER.search(latest.get("body", ""))
        if not m or m.group(2) == run_id:
            continue
        gh.set_pipeline_status_field(issue["number"], "in-progress")
        gh.issue_comment(issue["number"], f"▶️ Resumed by run `{run_id}` after the auto-mode "
                                          f"classifier denied `{m.group(1)}`.")
        return {"action": "resume", "issue": issue["number"],
                "unit": "epic" if is_epic(issue) else "issue",
                "stage": current_stage(issue), "reason_code": "classifier_denied",
                "denied_step": m.group(1),
                "reason": f"parked by run {m.group(2)} when the classifier denied "
                          f"`{m.group(1)}`: retry that step first (references/operations.md, "
                          f"\"Auto-mode classifier denies a composite\")"}
    return None


def cmd_mark_todo(gh: GitHub, issue: int) -> dict:
    """Set Pipeline Status to `todo` on a new issue; skips if it already has any value."""
    fields = gh.issue_fields(issue)
    current = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    if current is not None:
        return {"issue": issue, "skipped": f"Pipeline Status already {current!r}"}
    gh.set_pipeline_status_field(issue, "todo")
    return {"issue": issue, "pipeline_status": "todo"}


def cmd_mark_issue_closed(gh: GitHub, issue: int) -> dict:
    """Terminal fields for any closed issue (run by CI on `issues: closed`): clear
    Stage, set Pipeline Status `done`. Returns `is_epic`/`is_initiative`."""
    info = gh.issue_epic_info(issue)
    gh.clear_stage_field(issue)
    gh.set_pipeline_status_field(issue, "done")
    return {"issue": issue, "is_epic": is_epic(info), "is_initiative": is_initiative(info),
            "marked_done": True}


def _remote_head(repo_path: str, branch: str, runner: Runner) -> Optional[str]:
    """`origin/<branch>`'s head, read live from origin (`ls-remote`); None when absent."""
    out = _run_retry_transient(["git", "-C", repo_path, "ls-remote", "origin",
                                f"refs/heads/{branch}"], runner).strip()
    return out.split()[0] if out else None


def _cleanup_remote_branch(gh: GitHub, repo_path: str, branch: str, runner: Runner,
                           prs: Callable, safe_refs: Callable, dry_run: bool) -> dict:
    """Delete `origin/<branch>` when its head is on a merged PR head or the base (all its
    work landed); keep it while a PR is open or it carries unmerged commits."""
    try:
        head = _remote_head(repo_path, branch, runner)
    except GhError as exc:
        return {"deleted": False, "reason": f"could not read origin: {exc}"}
    if head is None:
        return {"deleted": False, "absent": True}
    listed = prs()
    if listed is None:
        return {"deleted": False, "head": head, "reason": "could not list the branch's PRs -- kept"}
    open_prs = [p["number"] for p in listed if p.get("state") == "OPEN"]
    if open_prs:
        return {"deleted": False, "head": head, "reason": f"PR #{open_prs[0]} is still open -- kept"}
    if not _landed(repo_path, head, safe_refs(), runner):
        return {"deleted": False, "head": head,
                "reason": f"origin/{branch} carries commits no merged PR or base holds -- "
                          f"unmerged work is kept"}
    if dry_run:
        return {"deleted": False, "head": head, "would_delete": True}
    if (cloud_skips_local_cleanup()
            and any(p.get("state") == "MERGED" and p.get("headRefOid") == head for p in listed)
            and getattr(gh, "auto_deletes_head_branches", bool)()):
        # The cloud proxy refuses ref deletes (403); GitHub drops a merged PR's head itself.
        return {"deleted": False, "head": head, "skipped": "repo auto-deletes merged heads"}
    try:
        gh.delete_branch(branch)
    except GhError as exc:
        try:
            still = _remote_head(repo_path, branch, runner)
        except GhError:
            still = head
        if still:
            return {"deleted": False, "head": head, "error": True, "reason": str(exc)}
    return {"deleted": True, "head": head}


def _cleanup_local_branch(repo_path: str, branch: str, runner: Runner, safe_refs: Callable,
                          remote: dict, dry_run: bool) -> dict:
    """Delete local `<branch>` only when its tip is on a merged PR head, the base, or a
    kept `origin/<branch>`; otherwise `retained_local_branch` with the reason."""
    try:
        listed = runner(["git", "-C", repo_path, "branch", "--list", branch]).strip()
    except GhError as exc:
        return {"deleted": False, "error": True, "reason": f"branch lookup failed: {exc}"}
    if not listed:
        return {"deleted": False, "absent": True}
    kept = {"deleted": False, "retained_local_branch": True}
    if listed.startswith(("*", "+")):
        return {**kept, "reason": "checked out in a worktree"}
    refs = list(safe_refs())
    if remote.get("head") and not remote.get("deleted"):
        refs.append(remote["head"])
    if not _landed(repo_path, f"refs/heads/{branch}", refs, runner):
        return {**kept, "reason": f"local {branch} has commits on no merged PR head, base or "
                                  f"origin/{branch} -- unmerged work is kept"}
    if dry_run:
        return {"deleted": False, "would_delete": True}
    try:
        runner(["git", "-C", repo_path, "branch", "-D", branch])
    except GhError as exc:
        return {**kept, "error": True, "reason": str(exc)}
    return {"deleted": True}


def _repo_path_outside_worktree(repo_path: str, branch: str, runner: Runner) -> str:
    """`repo_path`, or the main checkout when `repo_path` lies inside the worktree holding
    `branch` -- that tree is released first, and later `git -C <it>` calls would fail."""
    try:
        held = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
        if not held:
            return repo_path
        real, held = os.path.realpath(repo_path), os.path.realpath(held)
        if real != held and not real.startswith(held + os.sep):
            return repo_path
        entries = worktree_entries(repo_path, runner)
    except (GhError, OSError):
        return repo_path
    main = entries[0]["path"] if entries else None
    return main if main and os.path.realpath(main) != held else repo_path


def cleanup_unit(gh: GitHub, number: int, unit: str = "issue", repo_path: str = ".",
                 runner: Optional[Runner] = None, base: Optional[str] = None,
                 dry_run: bool = False, shell: Optional[Callable] = None) -> dict:
    """Idempotent cleanup of a terminal unit, in the order that never strands work: its
    docker resources (`docker_sweep` of `unit_docker_prefix`, when `dockerCleanup` is on),
    review worktree, its worktree (releaseCommand first), `origin/<branch>`, then the local
    ref, then `git worktree prune`. Never raises; each part reports what it did or why not
    (`docker`, `review_worktree`, `worktree`, `remote_branch`, `local_branch`,
    `worktree_pruned`). A cloud session releases them too: its container outlives many units,
    and each merged tree (node_modules included) left behind fills its disk.
    `base` defaults to the unit's integration base, resolved only when needed."""
    runner = runner or gh._run
    repo_path = repo_path or "."
    branch = unit_branch(unit, number)
    cache: dict = {}

    def prs():
        if "prs" not in cache:
            try:
                cache["prs"] = gh.pr_heads_for_branch(branch)
            except GhError:
                cache["prs"] = None
        return cache["prs"]

    def safe_refs() -> list:
        if "base" not in cache:
            try:
                cache["base"] = base or integration_base(gh, number, unit)
            except GhError:
                cache["base"] = None
        heads = [p["headRefOid"] for p in (prs() or [])
                 if p.get("state") == "MERGED" and p.get("headRefOid")]
        return heads + [f"refs/remotes/origin/{b}" for b in dict.fromkeys([cache["base"], "main"])
                        if b]

    out: dict = {"branch": branch}
    if _docker_cleanup_enabled():
        # First: a container holding the tree's files (bind mounts) must go before it does.
        out["docker"] = docker_sweep(unit_docker_prefix(number, unit), runner, dry_run)
    # Every later step runs `git -C repo_path`: never from inside the tree released below.
    repo_path = _repo_path_outside_worktree(repo_path, branch, runner)
    if unit == "issue":
        out["review_worktree"] = cmd_release_review_worktree(number, repo_path, runner, shell,
                                                             dry_run)
    if dry_run:
        try:
            held = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
        except GhError:
            held = None
        out["worktree"] = ({"released": False, "path": held, "would_release": True} if held
                           else {"released": False, "reason": "no worktree"})
    else:
        out["worktree"] = release_worktree(branch, runner=runner, base_repo=repo_path,
                                           shell=shell, safe_refs=safe_refs)
    if unit == "issue" and out["worktree"].get("reason") == "no worktree":
        # Before the remote delete: `origin/issue-<n>` is one of the refs that vouch for it.
        detached = release_detached_dev_worktree(number, repo_path, runner, shell, safe_refs,
                                                 dry_run)
        if detached.get("reason") != "no detached dev worktree":
            out["detached_worktree"] = detached
    out["remote_branch"] = _cleanup_remote_branch(gh, repo_path, branch, runner, prs,
                                                  safe_refs, dry_run)
    out["local_branch"] = _cleanup_local_branch(repo_path, branch, runner, safe_refs,
                                                out["remote_branch"], dry_run)
    if not dry_run:
        try:
            runner(["git", "-C", repo_path, "worktree", "prune"])
            out["worktree_pruned"] = True
        except GhError:
            out["worktree_pruned"] = False
    return out


def cleanup_warnings(cleanup: dict) -> list:
    """One operator-facing line per branch deletion that failed (`error`); an intentional
    keep (open PR, unmerged work, checked out) is no warning."""
    branch = cleanup.get("branch", "?")
    out = []
    remote, local = cleanup.get("remote_branch") or {}, cleanup.get("local_branch") or {}
    if remote.get("error"):
        out.append(f"origin/{branch} could not be deleted ({_first_line(remote.get('reason'))})"
                   f" -- the operator deletes it on GitHub")
    if local.get("error"):
        out.append(f"local branch {branch} could not be deleted "
                   f"({_first_line(local.get('reason'))}) -- the operator runs `git branch -D {branch}`")
    return out


def _first_line(text: Optional[str]) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else "unknown error"


def _with_warnings(result: dict, *cleanups: dict) -> dict:
    """`result` plus a top-level `warnings` list when any cleanup failed a deletion."""
    warnings = [w for c in cleanups for w in cleanup_warnings(c)]
    return {**result, "warnings": warnings} if warnings else result


def cmd_close_issue(gh: GitHub, issue: int, repo_path: Optional[str] = None,
                    runner: Runner = _default_runner, not_planned: bool = False,
                    reason: Optional[str] = None) -> dict:
    """Close `issue` (`not_planned`: state reason NOT_PLANNED, with `reason` commented first),
    apply the terminal fields, run `cleanup_unit` (worktrees; branches whose work landed --
    an unmerged branch is kept) and close the issues it `Realises:` -- for a unit that
    never merges through `merge-pr`. Counts toward the run cap unless `not_planned`."""
    if reason and not not_planned:
        raise GhError("--reason only goes with --not-planned")
    view = gh.issue_view(issue)
    body = view.get("body")
    # A re-run (e.g. finish-lld from the top) on a closed issue skips the close itself but still
    # does the idempotent bookkeeping below.
    already_closed = str(view.get("state") or "").upper() == "CLOSED"
    if not already_closed:
        if not_planned:
            gh.issue_comment(issue, f"🚫 Closed as not planned — "
                                    f"{reason or 'dropped by the operator'}.")
        gh.issue_close(issue, "not planned" if not_planned else "completed")
    marked = cmd_mark_issue_closed(gh, issue)
    realised = None if not_planned else _close_realised_issues(gh, issue, body)
    cleanup = cleanup_unit(gh, issue, "epic" if marked.get("is_epic") else "issue",
                           repo_path or ".", runner)
    released = cleanup.pop("worktree")
    if released.get("released") or released.get("reason") != "no worktree":
        released = {**released, "branch": cleanup["branch"]}
    terminal = record_terminal_unit(gh, issue, count=not not_planned)
    return _with_warnings(
        {"issue": issue, "closed": True, "already_closed": already_closed,
         "worktree": released, "cleanup": cleanup,
         **({"state_reason": "not_planned"} if not_planned else {}),
         **({"run_terminal": terminal} if terminal else {}),
         **({"realised_closed": realised} if realised else {})}, cleanup)


_UNIT_BRANCH_RE = re.compile(rf"^(?:(?P<issue>{re.escape(ISSUE_BRANCH_PREFIX)})|"
                             rf"(?P<epic>{re.escape(EPIC_BRANCH_PREFIX)})|"
                             rf"(?P<initiative>{re.escape(INITIATIVE_BRANCH_PREFIX)}))(?P<n>\d+)$")


def _unit_of_branch(branch: Optional[str]) -> Optional[tuple]:
    """`("issue"|"epic"|"initiative", n)` for a pipeline branch name, else None."""
    m = _UNIT_BRANCH_RE.match(branch or "")
    if not m:
        return None
    unit = "epic" if m.group("epic") else "initiative" if m.group("initiative") else "issue"
    if unit == "initiative" and not PIPELINE["initiativeProfiles"]:
        return None  # without the opt-in an `initiative-<n>` branch is not the pipeline's
    return (unit, int(m.group("n")))


def present_units(repo_path: str, runner: Runner = _default_runner) -> set:
    """`(unit, n)` of every pipeline unit with a local ref, an `origin` branch, or a
    worktree (detached review and dev trees under `worktrees.root` included); other branches are
    never listed. `cleanup_unit` never releases the main checkout or a branch it holds."""
    units = set()
    local = runner(["git", "-C", repo_path, "for-each-ref", "--format=%(refname:short)",
                    "refs/heads/"])
    remote = _run_retry_transient(["git", "-C", repo_path, "ls-remote", "--heads", "origin"],
                                  runner)
    names = local.split() + [line.split("refs/heads/", 1)[1] for line in remote.splitlines()
                             if "refs/heads/" in line]
    units.update(u for u in map(_unit_of_branch, names) if u)
    units.update(u for _, u in unit_worktrees(repo_path, runner))
    return units


def unit_worktrees(repo_path: str, runner: Runner = _default_runner) -> list:
    """`(entry, (unit, n))` for every worktree but the main checkout that holds a pipeline
    branch, or is a detached review/dev tree under `worktrees.root`."""
    w = PIPELINE["worktrees"]
    review_re = re.compile(rf"^(?:{re.escape(w['reviewPrefix'])}|{re.escape(w['devPrefix'])})(\d+)$")
    root = os.path.realpath(w["root"])
    found = []
    for i, entry in enumerate(worktree_entries(repo_path, runner)):
        if i == 0:
            continue  # the main checkout
        unit = _unit_of_branch(entry["branch"])
        if not unit and entry["branch"] is None \
                and os.path.dirname(os.path.realpath(entry["path"])) == root:
            m = review_re.match(os.path.basename(entry["path"]))
            unit = ("issue", int(m.group(1))) if m else None
        if unit:
            found.append((entry, unit))
    return found


def sweep_units(gh: GitHub, repo_path: str = ".", runner: Optional[Runner] = None,
                only: Optional[set] = None, issues: Optional[list] = None,
                dry_run: bool = False) -> dict:
    """`cleanup_unit` for every present pipeline unit whose issue is CLOSED (`only`: just
    these numbers). Open and unknown issues are listed, never touched."""
    runner = runner or gh._run
    repo_path = repo_path or "."
    try:
        present = present_units(repo_path, runner)
    except GhError as exc:
        return {"units": [], "error": f"could not list branches/worktrees: {exc}"}
    by_number = {i["number"]: i for i in (issues if issues is not None else gh.issue_list())}
    units, skipped_open, unknown = [], [], []
    for unit, n in sorted(present, key=lambda u: (u[1], u[0])):
        if only is not None and n not in only:
            continue
        issue = by_number.get(n)
        if issue is None:
            unknown.append(unit_branch(unit, n))
            continue
        if issue["state"] != "CLOSED":
            skipped_open.append(n)
            continue
        base = ("main" if unit == "initiative" else epic_base_in(by_number, n) if unit == "epic"
                else integration_base_in(by_number, n))
        units.append({"issue": n, "unit": unit,
                      **cleanup_unit(gh, n, unit, repo_path, runner, base=base, dry_run=dry_run)})
    return {"units": units, "skipped_open": skipped_open, "unknown": unknown}


def _run_state_epics() -> set:
    """The Epic/Initiative numbers that have a run-state file."""
    try:
        names = os.listdir(_run_state_dir())
    except OSError:
        return set()
    return {int(m.group(1)) for m in (re.fullmatch(r"epic-(\d+)\.json", n) for n in names) if m}


def _stale_run_states(closed: set, dry_run: bool) -> list:
    """Delete run-state files of CLOSED epics/initiatives once no longer fresh (older than
    `guard.mainThreadFreshnessHours`) and no fresh file shares their run id -- no live run
    still sums them into its cap, and the hooks ignore them by then."""
    try:
        hours = float((CONFIG.get("guard") or {}).get("mainThreadFreshnessHours", 8))
    except (TypeError, ValueError):
        hours = 8.0
    run_dir, now = _run_state_dir(), time.time()
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        return []
    files = {}
    for name in names:
        path = os.path.join(run_dir, name)
        try:
            with open(path) as f:
                state = json.load(f)
            files[name] = (path, os.path.getmtime(path), state if isinstance(state, dict) else {})
        except (OSError, ValueError):
            continue
    fresh_ids = {st.get("run_id") for _, mtime, st in files.values() if now - mtime <= hours * 3600}
    removed = []
    for name, (path, mtime, state) in files.items():
        m = re.fullmatch(r"epic-(\d+)\.json", name)
        if (not m or int(m.group(1)) not in closed or now - mtime <= hours * 3600
                or state.get("run_id") in fresh_ids):
            continue
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                continue
        removed.append(path)
    return removed


def cmd_prune_stale(gh: GitHub, repo_path: str = ".", runner: Optional[Runner] = None,
                    dry_run: bool = False) -> dict:
    """Sweep what finished units left behind: `cleanup_unit` for every closed issue's
    branches and worktrees (review trees included), stale run-state files of closed epics,
    then `git worktree prune` and `git fetch --prune origin`. `dry_run` reports only."""
    runner = runner or gh._run
    repo_path = repo_path or "."
    # Finished units' leftovers and run states name closed issues a scoped index lacks.
    try:
        wanted = {n for _, n in present_units(repo_path, runner)}
    except GhError:
        wanted = set()
    verifier_trees = _initiative_close_trees(repo_path, runner)
    issues = focus_issues(gh, wanted | set(verifier_trees) | _run_state_epics())
    swept = sweep_units(gh, repo_path, runner, issues=issues, dry_run=dry_run)
    closed = {i["number"] for i in issues if i["state"] == "CLOSED"}
    result = _with_warnings({"dry_run": dry_run, **swept,
                             ("would_remove_run_states" if dry_run else "run_states_removed"):
                                 _stale_run_states(closed, dry_run)},
                            *swept["units"])
    stale_trees = [(n, path) for n, path in sorted(verifier_trees.items()) if n in closed]
    if stale_trees:
        result["initiative_close_trees"] = [
            _remove_verifier_tree(repo_path, n, path, runner, dry_run) for n, path in stale_trees]
    if not dry_run:
        steps = [("worktree_pruned", ["git", "-C", repo_path, "worktree", "prune"]),
                 ("fetch_pruned", ["git", "-C", repo_path, "fetch", "--prune", "origin"])]
        if cloud_skips_local_cleanup() and _docker_cleanup_enabled():
            # A long cloud run's image builds fill its disk; unused build cache only.
            steps.append(("docker_builder_pruned", ["docker", "builder", "prune", "-f"]))
        for key, argv in steps:
            try:
                _run_retry_transient(argv, runner)
                result[key] = True
            except (GhError, OSError) as exc:  # OSError: no docker binary
                result[key] = False
                result[f"{key}_error"] = str(exc)
    return result


def _initiative_close_trees(repo_path: str, runner: Runner) -> dict:
    """`{initiative: path}` of the detached `<ephemeralPrefix>initiative-<n>` trees an
    `initiative-close` agent makes under `worktrees.root` (a dead agent leaves its tree)."""
    w = PIPELINE["worktrees"]
    name_re = re.compile(rf"^{re.escape(w['ephemeralPrefix'])}initiative-(\d+)$")
    root = os.path.realpath(w["root"])
    try:
        entries = worktree_entries(repo_path, runner)
    except GhError:
        return {}
    out = {}
    for e in entries[1:]:
        m = name_re.match(os.path.basename(e["path"]))
        if m and e["branch"] is None and os.path.dirname(os.path.realpath(e["path"])) == root:
            out[int(m.group(1))] = e["path"]
    return out


def _remove_verifier_tree(repo_path: str, n: int, path: str, runner: Runner,
                          dry_run: bool) -> dict:
    """Remove a closed Initiative's read-only verifier tree (it never holds commits)."""
    if dry_run:
        return {"initiative": n, "path": path, "would_release": True}
    try:
        return {"initiative": n, "path": path, "released": True,
                **_remove_worktree(repo_path, path, runner)}
    except GhError as exc:
        return {"initiative": n, "path": path, "released": False, "reason": str(exc)}


def _placement_units(number: int, issues: list) -> set:
    """`(unit, n)` of Epic/Initiative `number` and every descendant: an Epic's `epic-<n>`
    branch, every other issue's `issue-<n>`."""
    kids: dict = {}
    for i in issues:
        p = (i.get("parent") or {}).get("number")
        if p:
            kids.setdefault(p, []).append(i)
    by_number = {i["number"]: i for i in issues}
    units, todo = set(), [number]
    while todo:
        n = todo.pop()
        issue = by_number.get(n)
        if issue is not None and is_epic(issue):
            units.add(("epic", n))
        elif issue is not None and n != number:
            units.add(("issue", n))
        todo.extend(k["number"] for k in kids.get(n, []))
    return units


def _place_task(gh: WorkItemProvider, issue: dict, where: str, force: bool = False,
                repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """`place <task>`: `local` adds `localLabel` (the Task runs on the laptop while its Epic
    stays in the cloud), `cloud` removes it. A `localTasks` match stays local regardless.
    Refuses (`refused`) to move a Task that is claimed (`in-progress`: the side driving it
    would keep going while the other picks it up as a resume) or that a local worktree holds
    (unpushed work), unless `force`."""
    number, label = issue["number"], local_label()
    own = has_label(issue, label)
    if where == "cloud" and not own:
        raise GhError(f"#{number} is a Task, not an Epic or Initiative -- a Task is never "
                      f"placed in the cloud on its own; place its Epic (`place <epic> --where "
                      f"cloud`). `place <task> --where local` keeps one Task on the laptop")
    result = {"issue": number, "task": True, "label": label,
              "label_added": False, "label_removed": False}
    moving = (where == "local") != own
    if moving and not force:
        held = [entry["path"] for entry, (unit, n) in unit_worktrees(repo_path, runner)
                if unit == "issue" and n == number]
        claimed = pipeline_status(issue) == "in-progress"
        if claimed or held:
            why = (f"#{number} is in-progress (claimed by the side driving it)" if claimed
                   else f"a local worktree holds #{number} ({', '.join(held)})")
            return {**result, "refused": True, "worktrees": held, "in_progress": claimed,
                    "reason": f"{why}: moving it now lets both sides drive it or strands "
                              f"unpushed work -- let its stage finish (or push and release the "
                              f"worktree), or pass --force"}
    if where == "local" and not own:
        gh.ensure_label(label)
        gh.issue_edit(number, add_labels=[label])
        result["label_added"] = True
    elif where == "cloud" and own:
        gh.issue_edit(number, remove_labels=[label])
        result["label_removed"] = True
    else:
        result["already"] = True
    others = [{"name": n} for n in label_names(issue) if n != label]
    reason = "label" if where == "local" else task_local_reason({**issue, "labels": others})
    result["placed"] = "local" if reason else "with-epic"
    if where == "cloud" and reason:
        result["still_local"] = reason
    return result


def cmd_place(gh: WorkItemProvider, number: int, where: str, force: bool = False,
              repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """Place Epic/Initiative `number` in the cloud (add the cloud label, creating it) or
    local (remove it). Refuses (`refused`) while a unit under it holds a local worktree,
    unless `force`, and an Epic's `local` while its Initiative carries the label. Idempotent."""
    if where not in PLACEMENTS:
        raise GhError(f"--where must be one of {', '.join(PLACEMENTS)}")
    label = cloud_label()
    issues = gh.issue_list()
    by_number = {i["number"]: i for i in issues}
    issue = by_number.get(number)
    if issue is not None and classify_unit_from_issue(issue) not in ("epic", "initiative") \
            and issue.get("parent"):
        return _place_task(gh, issue, where, force=force, repo_path=repo_path, runner=runner)
    if issue is None or classify_unit_from_issue(issue) not in ("epic", "initiative"):
        raise GhError(f"#{number} is not an Epic or Initiative -- placement is set on the "
                      f"Epic or its Initiative (or, `local` only, on a Task of a cloud Epic); "
                      f"pass that number")
    own = has_label(issue, label)
    parent = by_number.get((issue.get("parent") or {}).get("number"))
    inherited = (parent is not None and is_initiative(parent) and has_label(parent, label))
    current = "cloud" if own or inherited else "local"
    result = {"issue": number, "placed": current, "label": label,
              "label_added": False, "label_removed": False, "worktrees": []}
    if where == "local" and inherited:
        return {**result, "refused": True, "inherited_from": parent["number"],
                "reason": f"Initiative #{parent['number']} carries `{label}`, so every Epic "
                          f"under it is cloud-placed -- `place {parent['number']} --where "
                          f"local` instead"}
    if current == where:
        return {**result, "already": True}
    units = _placement_units(number, issues)
    held = [{"issue": n, "unit": unit, "path": entry["path"], "branch": entry["branch"]}
            for entry, (unit, n) in unit_worktrees(repo_path, runner) if (unit, n) in units]
    result["worktrees"] = held
    if held and not force:
        return {**result, "refused": True,
                "reason": f"{len(held)} local worktree(s) still hold units of #{number}: "
                          f"{', '.join(w['path'] for w in held)}. Work not pushed from them "
                          f"is invisible to the other side -- push or release them (or "
                          f"pass --force), then re-run"}
    if where == "cloud":
        gh.ensure_label(label)
        gh.issue_edit(number, add_labels=[label])
        result["label_added"] = True
    else:
        gh.issue_edit(number, remove_labels=[label])
        result["label_removed"] = True
    if held:
        result["forced"] = True
    if where == "local" and is_initiative(issue):
        still = sorted(k["number"] for k in issues
                       if (k.get("parent") or {}).get("number") == number
                       and is_epic(k) and has_label(k, label) and k["state"] == "OPEN")
        if still:
            result["still_cloud"] = still
    return {**result, "placed": where}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import park  # noqa: E402,F401
from .cli import main  # noqa: E402
