"""Git fetch, worktrees, docker sweeps, branch locks and workspaces."""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Union

from .config import PIPELINE, Runner, issue_branch
from .github import GhError, GitHub, _default_runner
from .evidence import integration_base, unit_branch
from .plugin_version import worktree_path, worktree_path_for_branch


# Network failures a single immediate retry usually clears; anything else fails at once.
_TRANSIENT_NET_RE = re.compile(
    r"(timed out|timeout|connection (?:reset|refused|closed)|could not resolve host|"
    r"temporary failure|ssh_exchange_identification|kex_exchange_identification|"
    r"early eof|the remote end hung up|rpc failed)",
    re.IGNORECASE)


# Common git dir -> push generation at its last `git fetch origin` this process.
_FETCHED: dict = {}


_PUSH_GENERATION = [0]


def _fetch_dedupe_enabled() -> bool:
    return os.environ.get("SDLC_FETCH_DEDUPE", "on").strip().lower() not in ("0", "off", "false")


def _common_git_dir(path: str) -> str:
    """The repo's shared git dir (one per clone, whichever worktree `path` is)."""
    dot_git = os.path.join(path, ".git")
    try:
        if os.path.isfile(dot_git):
            with open(dot_git) as f:
                gitdir = f.read().split("gitdir:", 1)[1].strip()
            gitdir = os.path.join(path, gitdir)
            common = os.path.join(gitdir, "commondir")
            if os.path.isfile(common):
                with open(common) as f:
                    return os.path.realpath(os.path.join(gitdir, f.read().strip()))
            return os.path.realpath(gitdir)
    except (OSError, IndexError):
        pass
    return os.path.realpath(dot_git if os.path.isdir(dot_git) else path)


def _fetch_origin(path: str, runner: Runner):
    """`git fetch origin`, once per clone per process until something pushes: worktrees share
    one ref store, so a second fetch moments later only re-reads the same refs."""
    key = _common_git_dir(path) if _fetch_dedupe_enabled() and runner is _default_runner \
        else None
    if key is not None and _FETCHED.get(key) == _PUSH_GENERATION[0]:
        return ""
    out = _run_retry_transient(["git", "-C", path, "fetch", "origin"], runner)
    if key is not None:
        _FETCHED[key] = _PUSH_GENERATION[0]
    return out


def _run_retry_transient(argv: list, runner: Runner, attempts: int = 2):
    """Run a git network command, retrying once on a transient network error."""
    for i in range(attempts):
        try:
            return runner(argv)
        except GhError as e:
            if i + 1 < attempts and _TRANSIENT_NET_RE.search(str(e)):
                time.sleep(1)
                continue
            raise


def _add_fresh_worktree(repo_path: str, path: str, branch: str, base: str,
                        runner: Runner) -> None:
    """`git worktree add -b <branch> <base>`; a stale local `<branch>` (crashed earlier
    run) is recreated only when it has no commits `base` lacks, else refused."""
    add = ["git", "-C", repo_path, "worktree", "add", path, "-b", branch, base]
    try:
        runner(add)
        return
    except GhError:
        if not runner(["git", "-C", repo_path, "branch", "--list", branch]).strip():
            raise
    unique = _rev_count(repo_path, f"{base}..{branch}", runner)
    if unique:
        raise GhError(f"local branch {branch!r} already exists with {unique} commit(s) not in "
                      f"{base} -- refusing to discard unmerged work; inspect and delete it by hand")
    runner(["git", "-C", repo_path, "branch", "-D", branch])
    runner(add)


def cmd_worktree_add(gh: GitHub, number: int, unit: str = "issue", repo_path: str = ".",
                     runner: Runner = _default_runner, base: Optional[str] = None) -> dict:
    """Create or resume the unit's worktree: reuse a live one (fast-forwarded), else
    `-B` from `origin/<branch>` when pushed, else `-b` off `base` or the integration base.
    Returns `created`, `path`, `branch`, `base`, `resumed`, `scratch` plus sync fields."""
    branch = unit_branch(unit, number)
    path = worktree_path(unit, number)
    existing = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
    if existing:
        return _with_scratch(_resume_live_worktree(existing, branch, repo_path, runner))
    _fetch_origin(repo_path, runner)
    on_origin = runner(["git", "-C", repo_path, "branch", "-r", "--list",
                        f"origin/{branch}"]).strip()
    if on_origin:
        base = f"origin/{branch}"
        runner(["git", "-C", repo_path, "worktree", "add", path, "-B", branch, base])
        resumed = True
        # None, not 0: the old local ref is discarded unmeasured.
        synced = {"synced_to_origin": True, "behind_before": None}
    else:
        if base is None:
            base_branch = integration_base(gh, number, unit)
            # A pre-existing Epic may have no branch yet; cut it before its child's.
            if base_branch != "main" and not origin_branch_exists(repo_path, base_branch,
                                                                   runner=runner):
                ensure_branch_on_origin(repo_path, base_branch, runner=runner)
            base = f"origin/{base_branch}"
        _add_fresh_worktree(repo_path, path, branch, base, runner)
        if unit in ("epic", "initiative"):
            # An epic (or initiative) branch is shared: every branch-writing command resolves it
            # from origin, so a local-only one is invisible to them.
            _run_retry_transient(["git", "-C", repo_path, "push", "origin",
                                  f"refs/heads/{branch}:refs/heads/{branch}"], runner)
        resumed = False
        synced = {}
    return _with_scratch({"created": True, "path": path, "branch": branch, "base": base,
                          "resumed": resumed, **synced})


def _with_scratch(result: dict) -> dict:
    """Add `scratch` (the worktree's `.sdlc-scratch/`) when the tree exists on disk."""
    scratch = ensure_scratch(result["path"])
    return {**result, "scratch": scratch} if scratch else result


def _rev_count(repo_path: str, rev_range: str, runner: Runner) -> int:
    """`git rev-list --count <range>` as an int (0 when git prints nothing)."""
    return int(runner(["git", "-C", repo_path, "rev-list", "--count",
                       rev_range]).strip() or 0)


def _resume_live_worktree(path: str, branch: str, repo_path: str, runner: Runner) -> dict:
    """Return the live worktree holding `branch`, fast-forwarded to origin when
    strictly behind. A diverged branch is reported (`diverged`) and never reset.
    Adds `synced_to_origin` and `behind_before` (None when origin lacks the branch)."""
    _fetch_origin(repo_path, runner)
    result = {"created": False, "path": path, "branch": branch, "resumed": True,
              "reason": "branch already checked out in a live worktree"}
    if not origin_branch_exists(repo_path, branch, runner=runner):
        return {**result, "synced_to_origin": False, "behind_before": None,
                "origin_missing": True}
    behind = _rev_count(path, f"{branch}..origin/{branch}", runner)
    ahead = _rev_count(path, f"origin/{branch}..{branch}", runner)
    if behind and ahead:
        return {**result, "synced_to_origin": False, "behind_before": behind,
                "diverged": True, "ahead_of_origin": ahead,
                "reason": f"local {branch} has diverged from origin/{branch} ({ahead} "
                          f"local commit(s), {behind} on origin) -- refusing to "
                          f"fast-forward over unpushed work. Reconcile it (sync-branch, "
                          f"or push/discard the local commits), then resume."}
    if behind:
        runner(["git", "-C", path, "merge", "--ff-only", f"origin/{branch}"])
    return {**result, "synced_to_origin": True, "behind_before": behind}


def _run_release_command(path: str, shell: Optional[Callable] = None) -> Optional[dict]:
    """Run `pipeline.worktrees.releaseCommand` inside `path`; None when none is configured.
    Never raises: `{command, ok, error?}` reports a failure and the removal goes ahead."""
    command = PIPELINE["worktrees"].get("releaseCommand") or ""
    if not command:
        return None
    try:
        (shell or _default_shell)(command, path)
    except GhError as exc:
        return {"command": command, "ok": False, "error": str(exc)}
    return {"command": command, "ok": True}


def unit_docker_prefix(number: int, unit: str = "issue") -> str:
    """The docker name prefix a unit's agents give its compose projects, containers and
    volumes (followed by `-` or `_`): its worktree's basename, `sdlc-dev-<n>` (an epic:
    `sdlc-epic-<n>`). `cleanup_unit` removes whatever carries it."""
    return os.path.basename(worktree_path(unit, number))


def _docker_cleanup_enabled() -> bool:
    return bool(PIPELINE["worktrees"].get("dockerCleanup", True))


def _docker_unavailable(runner: Runner) -> Optional[str]:
    """Why docker cannot be used through `runner` (binary missing, daemon down), else None."""
    try:
        runner(["docker", "version", "--format", "{{.Server.Version}}"])
    except (GhError, OSError) as exc:
        msg = str(exc).strip()
        return f"docker unavailable: {msg.splitlines()[0] if msg else repr(exc)}"
    return None


_COMPOSE_PROJECT_LABEL = '{{.Label "com.docker.compose.project"}}'


def docker_sweep(prefix: str, runner: Runner, dry_run: bool = False) -> dict:
    """Remove the docker containers (by name or compose project label), then volumes and
    networks (by name or label) that equal `<prefix>` or start `<prefix>-` / `<prefix>_`
    (compose's default project name is the worktree's basename itself) -- never a bare
    prefix, so `sdlc-dev-14` never takes `sdlc-dev-145`'s. Listed and matched in Python.
    Never raises: `{skipped}` when docker is unavailable; `dry_run` lists `would_remove`."""
    reason = _docker_unavailable(runner)
    if reason:
        return {"prefix": prefix, "skipped": reason}
    pattern = re.compile(rf"^{re.escape(prefix)}(?:$|[-_])")
    errors: list = []

    def listed(argv: list) -> list:
        try:
            out = runner(argv)
        except (GhError, OSError) as exc:
            errors.append(f"{' '.join(argv[:3])}: {exc}")
            return []
        return [line.split("\t") for line in out.splitlines() if line.strip()]

    containers = {}
    for row in listed(["docker", "ps", "-a", "--format",
                       "{{.ID}}\t{{.Names}}\t" + _COMPOSE_PROJECT_LABEL]):
        cid, name, project = (row + ["", ""])[:3]
        if pattern.match(name) or pattern.match(project):
            containers[cid] = name
    found = {"containers": containers}
    for kind in ("volume", "network"):
        found[f"{kind}s"] = {
            name: name for name, project in
            ((row + [""])[:2] for row in listed(["docker", kind, "ls", "--format",
                                                  "{{.Name}}\t" + _COMPOSE_PROJECT_LABEL]))
            if pattern.match(name) or pattern.match(project)}
    if dry_run:
        return {"prefix": prefix, "would_remove": {k: sorted(v.values()) for k, v in found.items()},
                "errors": errors}
    out: dict = {"prefix": prefix}
    for kind, rm in (("containers", ["docker", "rm", "-f", "-v"]),
                     ("volumes", ["docker", "volume", "rm", "-f"]),
                     ("networks", ["docker", "network", "rm"])):
        removed = []
        for ident, name in sorted(found[kind].items(), key=lambda kv: kv[1]):
            try:
                runner(rm + [ident])
                removed.append(name)
            except (GhError, OSError) as exc:
                errors.append(f"{kind[:-1]} {name}: {exc}")
        out[f"{kind}_removed"] = removed
    out["errors"] = errors
    return out


_PERMISSION_ERROR_RE = re.compile(r"Permission denied|Operation not permitted", re.I)


def _remove_worktree(repo_path: str, path: str, runner: Runner) -> dict:
    """`git worktree remove --force path`; `{}` on success, else raises GhError. When a
    permission error (root-owned files a container wrote) blocks it and docker cleanup is
    on, empties the tree once through `docker run --rm alpine` -- only for a path under
    `worktrees.root` -- then removes/prunes it: `{"forced_docker_rm": True}`."""
    try:
        runner(["git", "-C", repo_path, "worktree", "remove", "--force", path])
        return {}
    except GhError as exc:
        if not _PERMISSION_ERROR_RE.search(str(exc)) or not _docker_cleanup_enabled():
            raise
        root = os.path.realpath(PIPELINE["worktrees"]["root"])
        real = os.path.realpath(path)
        if real == root or os.path.commonpath([real, root]) != root:
            raise GhError(f"{exc}\nnot retrying through docker: {path} is outside the "
                          f"worktrees root {root}")
        unavailable = _docker_unavailable(runner)
        if unavailable:
            raise GhError(f"{exc}\nnot retrying through docker: {unavailable}")
        runner(["docker", "run", "--rm", "-v", f"{real}:/w", "alpine",
                "sh", "-c", "rm -rf /w/* /w/.[!.]*"])
    try:  # its `.git` file is gone with the contents, so `remove` may refuse; prune drops it
        runner(["git", "-C", repo_path, "worktree", "remove", "--force", path])
    except GhError:
        pass
    runner(["git", "-C", repo_path, "worktree", "prune"])
    with contextlib.suppress(OSError):
        os.rmdir(path)
    return {"forced_docker_rm": True}


def _is_ancestor(repo_path: str, rev: str, ref: str, runner: Runner) -> bool:
    """Whether `rev` is `ref` or an ancestor of it (False when either is unknown locally)."""
    try:
        runner(["git", "-C", repo_path, "merge-base", "--is-ancestor", rev, ref])
        return True
    except GhError:
        return False


def _landed(repo_path: str, rev: str, refs: list, runner: Runner) -> bool:
    """Whether every non-merge commit of `rev` is on one of `refs` (merged PR heads, bases,
    a kept origin branch): its work is stored elsewhere, so the ref can go. Merge commits are
    exempt -- a post-merge reconcile (pass-gate, sync-branch) adds one after the PR merged."""
    refs = [r for r in dict.fromkeys(refs) if r]
    if rev in refs or any(_is_ancestor(repo_path, rev, ref, runner) for ref in refs):
        return True
    known = []
    for ref in refs:
        try:
            runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
            known.append(ref)
        except GhError:
            pass
    if not known:
        return False
    try:
        return not int(runner(["git", "-C", repo_path, "rev-list", "--count", "--no-merges", rev,
                               "--not", *known]).strip() or 0)
    except (GhError, ValueError):
        return False


def _unpushed_reason(path: str, branch: str, runner: Runner,
                     safe_refs: Union[Callable[[], list], list] = ()) -> Optional[str]:
    """Why `branch`'s tip in `path` is not safely stored elsewhere, else None: compared
    with `origin/<branch>`, or -- once that tracking ref is gone (branch deleted, pruned)
    -- with `safe_refs` (merged PR heads, the base), so a vanished ref never blocks."""
    try:
        unpushed = runner(["git", "-C", path, "log", "--oneline",
                            f"origin/{branch}..{branch}"]).strip()
        return (f"{len(unpushed.splitlines())} commit(s) not pushed to origin/{branch}"
                if unpushed else None)
    except GhError:
        if origin_branch_exists(path, branch, runner=runner):
            raise
    if _landed(path, branch, safe_refs() if callable(safe_refs) else list(safe_refs), runner):
        return None
    return (f"origin/{branch} is gone and {branch}'s tip is on no merged PR head or base "
            f"-- refusing to discard it")


def release_worktree(branch: str, runner: Runner = _default_runner,
                      base_repo: str = ".", shell: Optional[Callable] = None,
                      safe_refs: Union[Callable[[], list], list] = ()) -> dict:
    """Remove the worktree holding `branch` so a parked/finished unit frees its lane
    slot, running `pipeline.worktrees.releaseCommand` in it first (`release_command`).
    Returns `{released, path?, reason?}`; never raises and never destroys work
    (refuses on uncommitted/unpushed changes or the main worktree). `safe_refs` stand in
    for `origin/<branch>` once that tracking ref is gone (see `_unpushed_reason`)."""
    try:
        path = worktree_path_for_branch(branch, runner=runner, base_repo=base_repo)
    except GhError as exc:
        return {"released": False, "reason": f"worktree lookup failed: {exc}"}
    if path is None:
        return {"released": False, "reason": "no worktree"}
    try:
        main_path = runner(["git", "-C", base_repo, "rev-parse",
                            "--path-format=absolute", "--git-common-dir"]).strip()
    except GhError:
        main_path = None
    if main_path and os.path.realpath(os.path.join(path, ".git")) == os.path.realpath(main_path):
        return {"released": False, "path": path,
                "reason": "branch is checked out in the repository's main worktree"}
    try:
        if runner(["git", "-C", path, "status", "--porcelain"]).strip():
            return {"released": False, "path": path, "reason": "uncommitted changes"}
        unpushed = _unpushed_reason(path, branch, runner, safe_refs)
        if unpushed:
            return {"released": False, "path": path, "reason": unpushed}
        release = _run_release_command(path, shell)
        # Safe: the refusals above guarantee a clean tree. Plain `remove` refuses
        # any tree containing submodules.
        forced = _remove_worktree(base_repo, path, runner)
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    return {"released": True, "path": path, **forced,
            **({"release_command": release} if release else {})}


# Agent scratch (logs, captured output, probe files) lives inside the unit's worktree, so it
# goes when the worktree does; `info/exclude` keeps it out of `git status` and commits.
SCRATCH_DIR = ".sdlc-scratch"


def ensure_scratch(worktree: str) -> Optional[str]:
    """Create `<worktree>/.sdlc-scratch/` and list it in the repo's shared `info/exclude`
    (idempotent). Returns the scratch path, or None when `worktree` is no git tree on disk."""
    dot_git = os.path.join(worktree, ".git")
    try:
        if os.path.isfile(dot_git):
            with open(dot_git) as f:
                gitdir = f.read().split("gitdir:", 1)[1].strip()
            gitdir = os.path.join(worktree, gitdir)
            common = gitdir
            if os.path.isfile(os.path.join(gitdir, "commondir")):
                with open(os.path.join(gitdir, "commondir")) as f:
                    common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
        elif os.path.isdir(dot_git):
            common = dot_git
        else:
            return None
        exclude = os.path.join(common, "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        existing = ""
        if os.path.exists(exclude):
            with open(exclude) as f:
                existing = f.read()
        entry = f"{SCRATCH_DIR}/"
        if entry not in existing.splitlines():
            with open(exclude, "a") as f:
                f.write(("" if not existing or existing.endswith("\n") else "\n") + entry + "\n")
        scratch = os.path.join(worktree, SCRATCH_DIR)
        os.makedirs(scratch, exist_ok=True)
        return scratch
    except (OSError, IndexError):
        return None


def review_worktree_path(number: int) -> str:
    """The detached pr-review worktree for `issue-<n>` (default `/tmp/sdlc-review-<n>`)."""
    w = PIPELINE["worktrees"]
    return os.path.join(w["root"], f"{w['reviewPrefix']}{number}")


def worktree_entries(repo_path: str, runner: Runner = _default_runner) -> list:
    """Every worktree as `{path, branch}` (`branch` None when detached), main first."""
    out = runner(["git", "-C", repo_path, "worktree", "list", "--porcelain"])
    entries = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            entries.append({"path": line[len("worktree "):], "branch": None})
        elif line.startswith("branch refs/heads/") and entries:
            entries[-1]["branch"] = line[len("branch refs/heads/"):]
    return entries


def _registered_worktree(repo_path: str, path: str, runner: Runner) -> Optional[dict]:
    """The worktree entry at `path` (symlinks resolved: /tmp is /private/tmp on macOS)."""
    real = os.path.realpath(path)
    return next((e for e in worktree_entries(repo_path, runner)
                 if os.path.realpath(e["path"]) == real), None)


def cmd_review_worktree_add(number: int, repo_path: str = ".",
                            runner: Runner = _default_runner) -> dict:
    """Create (or refresh) `issue-<n>`'s detached pr-review worktree at `origin/issue-<n>`,
    with its scratch dir. Never a local `issue-<n>` branch: `development` must stay free
    to hold it for rework."""
    branch, path = issue_branch(number), review_worktree_path(number)
    _fetch_origin(repo_path, runner)
    if not origin_branch_exists(repo_path, branch, runner=runner):
        raise GhError(f"origin/{branch} does not exist -- nothing to review")
    entry = _registered_worktree(repo_path, path, runner)
    if entry and entry["branch"]:
        raise GhError(f"{path} holds branch {entry['branch']}, not a detached review worktree "
                      f"-- inspect it by hand")
    if entry:
        runner(["git", "-C", path, "checkout", "--detach", "--force", f"origin/{branch}"])
    else:
        runner(["git", "-C", repo_path, "worktree", "add", "--detach", path, f"origin/{branch}"])
    head = runner(["git", "-C", path, "rev-parse", "HEAD"]).strip()
    return {"path": path, "branch": branch, "head": head, "reused": entry is not None,
            "scratch": ensure_scratch(path)}


def cmd_release_review_worktree(number: int, repo_path: str = ".",
                                runner: Runner = _default_runner,
                                shell: Optional[Callable] = None, dry_run: bool = False) -> dict:
    """Remove `issue-<n>`'s detached review worktree (releaseCommand first); idempotent.
    Force-removal loses nothing: a review tree is read-only on the branch."""
    path = review_worktree_path(number)
    try:
        entry = _registered_worktree(repo_path, path, runner)
    except GhError as exc:
        return {"released": False, "path": path, "reason": f"worktree lookup failed: {exc}"}
    if entry is None:
        return {"released": False, "path": path, "reason": "no review worktree"}
    if entry["branch"]:
        return {"released": False, "path": path,
                "reason": f"holds branch {entry['branch']}, not a detached review worktree"}
    if dry_run:
        return {"released": False, "path": path, "would_release": True}
    release = _run_release_command(path, shell)
    try:
        forced = _remove_worktree(repo_path, path, runner)
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    return {"released": True, "path": path, **forced,
            **({"release_command": release} if release else {})}


def release_detached_dev_worktree(number: int, repo_path: str = ".",
                                  runner: Runner = _default_runner,
                                  shell: Optional[Callable] = None,
                                  safe_refs: Union[Callable[[], list], list] = (),
                                  dry_run: bool = False) -> dict:
    """Remove `issue-<n>`'s dev-path worktree when it is detached (a hand-made or
    checked-out-away tree `release_worktree` cannot find by branch). Never destroys work:
    refuses a dirty tree, or a HEAD no merged PR head / base / `origin/issue-<n>` holds."""
    path = worktree_path("issue", number)
    try:
        entry = _registered_worktree(repo_path, path, runner)
    except GhError as exc:
        return {"released": False, "path": path, "reason": f"worktree lookup failed: {exc}"}
    if entry is None or entry["branch"]:
        return {"released": False, "path": path, "reason": "no detached dev worktree"}
    try:
        if runner(["git", "-C", path, "status", "--porcelain"]).strip():
            return {"released": False, "path": path, "reason": "uncommitted changes"}
        head = runner(["git", "-C", path, "rev-parse", "HEAD"]).strip()
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    refs = list(safe_refs() if callable(safe_refs) else safe_refs)
    refs.append(f"refs/remotes/origin/{issue_branch(number)}")
    if not _landed(repo_path, head, refs, runner):
        return {"released": False, "path": path, "head": head,
                "reason": "detached HEAD carries commits no merged PR, base or origin branch holds"}
    if dry_run:
        return {"released": False, "path": path, "would_release": True}
    release = _run_release_command(path, shell)
    try:
        forced = _remove_worktree(repo_path, path, runner)
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    return {"released": True, "path": path, **forced,
            **({"release_command": release} if release else {})}


def resolve_repo_path(repo_path: Optional[str], branch: str,
                       runner: Runner = _default_runner) -> str:
    """Where a read-only command reads a branch's files: `repo_path` if given, else
    the branch's live worktree, else ".". Writers use BranchWorkspace instead."""
    if repo_path is not None:
        return repo_path
    return worktree_path_for_branch(branch, runner=runner) or "."


def _lock_dir() -> str:
    """Per-branch lockfile directory: `SDLC_LOCK_DIR`, else `pipeline.locks.dir`."""
    env = os.environ.get("SDLC_LOCK_DIR")
    if env:
        return env
    return PIPELINE["locks"]["dir"].format(worktreesRoot=PIPELINE["worktrees"]["root"])


class BranchLocked(GhError):
    """Raised when another process holds the branch's lock past `waitSeconds`."""


@contextlib.contextmanager
def branch_lock(branch: str, wait_seconds: Optional[float] = None):
    """Exclusive per-branch `flock`; waits up to `pipeline.locks.waitSeconds`, then
    raises `BranchLocked`. Yields the lockfile path, which is never deleted (a new
    inode would let a third process lock alongside two contending on the old one)."""
    if wait_seconds is None:
        wait_seconds = float(PIPELINE["locks"]["waitSeconds"])
    lock_dir = _lock_dir()
    os.makedirs(lock_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    path = os.path.join(lock_dir, f"{safe}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BranchLocked(
                        f"branch {branch} is locked by another sdlc-pipeline process "
                        f"({path}); waited {wait_seconds:g}s. Another session is operating "
                        f"this branch -- let it finish, or raise pipeline.locks.waitSeconds")
                time.sleep(0.5)
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n".encode())
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def worktree_map(base_repo: str = ".", runner: Runner = _default_runner) -> tuple:
    """`(main_path, {branch: path})` from `git worktree list`; git always lists the
    main worktree first, and detached worktrees are omitted."""
    out = runner(["git", "-C", base_repo, "worktree", "list", "--porcelain"])
    main_path, path, by_branch = None, None, {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if main_path is None:
                main_path = path
        elif line.startswith("branch refs/heads/") and path is not None:
            by_branch[line[len("branch refs/heads/"):]] = path
    return main_path, by_branch


class BranchWorkspace:
    """Context manager yielding a tree with `branch` checked out for writing, never the
    main checkout: the branch's live worktree, else an ephemeral one from origin that is
    removed on exit when clean. `.retained` names an ephemeral tree left behind."""

    def __init__(self, branch: str, base_repo: str = ".", runner: Runner = _default_runner):
        self.branch = branch
        self.base_repo = base_repo or "."
        self.runner = runner
        self.path: Optional[str] = None
        self.ephemeral = False
        self.created_ref = False
        self.retained: Optional[str] = None

    def __enter__(self) -> "BranchWorkspace":
        r, base, branch = self.runner, self.base_repo, self.branch
        main_path, by_branch = worktree_map(base, runner=r)
        live = by_branch.get(branch)
        if live is not None:
            if main_path is not None and os.path.realpath(live) == os.path.realpath(main_path):
                raise GhError(
                    f"branch {branch} is checked out in the repository's MAIN checkout "
                    f"({main_path}); the pipeline never writes there. Recovery: commit any "
                    f"WIP there, `git -C {main_path} checkout main`, then re-run -- the "
                    f"command will use the branch's own worktree or an ephemeral one.")
            self.path = live
            return self
        _fetch_origin(base, r)
        on_origin = origin_branch_exists(base, branch, runner=r)
        try:
            r(["git", "-C", base, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
            local = True
        except GhError:
            local = False
        if not on_origin and not local:
            raise GhError(f"branch {branch} exists neither on origin nor locally -- nothing to "
                          f"operate on (stand it up with worktree-add first)")
        if on_origin and local:
            unpushed = r(["git", "-C", base, "log", "--oneline",
                          f"origin/{branch}..{branch}"]).strip()
            if unpushed:
                raise GhError(
                    f"local ref {branch} carries {len(unpushed.splitlines())} commit(s) not on "
                    f"origin/{branch} and no worktree holds it; refusing to reset it. Push or "
                    f"discard those commits, then re-run.")
        w = PIPELINE["worktrees"]
        self.path = os.path.join(w["root"], f"{w['ephemeralPrefix']}{branch}-{os.getpid()}")
        if on_origin:
            r(["git", "-C", base, "worktree", "add", self.path, "-B", branch, f"origin/{branch}"])
        else:
            r(["git", "-C", base, "worktree", "add", self.path, branch])
        self.ephemeral = True
        # `-B` made a local ref that did not exist; drop it again with the tree.
        self.created_ref = on_origin and not local
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.ephemeral or self.path is None:
            return False
        r, base, path = self.runner, self.base_repo, self.path
        try:
            dirty = r(["git", "-C", path, "status", "--porcelain"]).strip()
            unpushed = ""
            if origin_branch_exists(base, self.branch, runner=r):
                unpushed = r(["git", "-C", path, "log", "--oneline",
                              f"origin/{self.branch}..{self.branch}"]).strip()
            if dirty or unpushed:
                self.retained = path
                return False
            r(["git", "-C", base, "worktree", "remove", path])
        except GhError:
            self.retained = path
            return False
        if self.created_ref:
            try:
                # Pushed (checked above), so the ref holds nothing origin lacks.
                r(["git", "-C", base, "branch", "-D", self.branch])
            except GhError:
                pass
        return False


def _with_workspace(result: dict, ws: BranchWorkspace) -> dict:
    """Add `retained_worktree` to `result` when an ephemeral tree was left behind."""
    if ws.retained:
        result["retained_worktree"] = ws.retained
    return result


def origin_branch_exists(repo_path: str, branch: str, runner: Runner = _default_runner) -> bool:
    """Whether the remote-tracking ref `origin/<branch>` exists; callers fetch first."""
    try:
        runner(["git", "-C", repo_path, "show-ref", "--verify", "--quiet",
                 f"refs/remotes/origin/{branch}"])
        return True
    except GhError:
        return False


def ensure_branch_on_origin(repo_path: str, branch: str, base: str = "main",
                            runner: Runner = _default_runner) -> str:
    """Make `origin/<branch>` exist before writing to it. Returns `"exists"`,
    `"pushed-local"` (local ref pushed as-is) or `"created"` (cut from `origin/<base>`)."""
    _fetch_origin(repo_path, runner)
    if origin_branch_exists(repo_path, branch, runner=runner):
        return "exists"
    try:
        runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}"])
        source, outcome = f"refs/heads/{branch}", "pushed-local"
    except GhError:
        source, outcome = f"refs/remotes/origin/{base}", "created"
    runner(["git", "-C", repo_path, "push", "origin", f"{source}:refs/heads/{branch}"])
    _fetch_origin(repo_path, runner)
    return outcome


def active_worktree_branches(repo_path: str, runner: Runner = _default_runner) -> set:
    """Branches checked out in any worktree (main included). Detached review
    worktrees are excluded, so they don't count against the dev lane."""
    out = runner(["git", "-C", repo_path, "worktree", "list", "--porcelain"])
    branches = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/"):])
    return branches


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import lanes  # noqa: E402,F401
from .stack import _default_shell  # noqa: E402
