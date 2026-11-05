"""Epic and Initiative close: verification records, evidence, reconcile and landing."""
from __future__ import annotations

import re
from typing import Callable, Optional

from .config import PIPELINE, Runner, all_doc_roots, issue_branch
from .github import GhError, GitHub, _default_runner, _utc_now_marker
from .units import (
    epic_base, initiative_branch, initiative_of_in, is_epic, is_initiative,
    resolve_initiative_profile, resolve_profile,
)
from .evidence import (
    EPIC_CLOSE_REQUIRED_EVIDENCE, _EPIC_VERIFICATION_MARKER, epic_branch,
    evidence_carries_forward, local_ci_suites_attested, missing_epic_verification,
)


class _EpicEvidenceDelta:
    """`stale_since` for `missing_epic_verification`: how `branch` moved on from a tested
    `sha` (`fresh` | `carried_forward` | `stale` | `uncomparable`, with the files), remembered
    per sha so close-epic can report what it carried forward. With `base`, a delta that
    touches no file the epic itself changed against `base` (main moved on independently)
    also carries forward."""

    def __init__(self, gh, branch: str, carry_forward: bool = True, base: Optional[str] = None):
        self.gh, self.branch, self.carry_forward, self.deltas = gh, branch, carry_forward, {}
        self.base, self._epic_files = base, None

    def _independent_of_epic(self, files: list) -> bool:
        """Whether `files` touch no file the epic changed against `base` (nor the pipeline
        config): what main brought in that the tested tree never depended on."""
        if self.base is None or touches_pipeline_config(files):
            return False
        if self._epic_files is None:
            self._epic_files = self.gh.base_delta_files(self.base, base=self.branch)
        return (len(self._epic_files) < COMPARE_FILES_CAP
                and not set(files) & set(self._epic_files))

    def classify(self, sha: str) -> dict:
        if sha not in self.deltas:
            try:
                files = self.gh.files_since(sha, self.branch)
                status = ("fresh" if not files
                          else "carried_forward" if self.carry_forward and len(files) < 300
                          and (all(evidence_carries_forward(p) for p in files)
                               or self._independent_of_epic(files))
                          else "stale")
            except GhError:
                files, status = [], "uncomparable"
            self.deltas[sha] = {"status": status, "files": files}
        return self.deltas[sha]

    def __call__(self, sha: str) -> Optional[str]:
        status = self.classify(sha)["status"]
        if status == "uncomparable":
            return (f"names a tested head ({sha[:10]}) that `{self.branch}` cannot be compared "
                    f"against -- re-run and re-record it")
        if status == "stale":
            return (f"tested `{self.branch}` at {sha[:10]}, but code has landed since -- it "
                    f"describes a different tree; re-run it and re-record"
                    + ("" if self.carry_forward else " (carry-forward disabled)"))
        return None


def _exploratory_evidence_status(comments: list, delta: _EpicEvidenceDelta,
                                 problems: list) -> str:
    """`fresh` | `carried_forward_from <sha>` | `missing` for close-epic's `evidence` field."""
    if problems:
        return "missing"
    sha = None
    for c in comments:
        m = _EPIC_VERIFICATION_MARKER.search(c.get("body", ""))
        if m and m.group(1) == "exploratory":
            sha = m.group(3)  # the latest record wins, as in `missing_epic_verification`
    if not sha:
        return "missing"
    status = delta.classify(sha)["status"]
    return f"carried_forward_from {sha[:10]}" if status == "carried_forward" else status


def _pr_suite_evidence(gh, spec: dict, view: dict, checks: list, ref: str,
                       carry_forward: bool, carried_files: set) -> Optional[str]:
    """How one PR satisfies `spec` at its head: `passing_check`, `fresh` (attested at the
    head), `carried_forward_from <sha>` (attested earlier and nothing the suite covers changed
    since, the merge-pr rule), or None. The one place check- or attestation-based suite
    satisfaction is decided for epic close. `ref` is what `files_since` compares against."""
    if workflow_check_state(checks, spec["workflow"]) == "passing":
        return "passing_check"
    suite = spec["suite"]
    if suite_not_attestable(suite):
        return None
    comments = view.get("comments", [])
    if suite in local_ci_suites_attested(comments, view.get("headRefOid")):
        return "fresh"
    sha = _latest_attested_sha_by_suite(comments).get(suite) if carry_forward else None
    if not sha:
        return None
    try:
        delta = gh.files_since(sha, ref)
    except GhError:
        return None
    if _suite_reattest_needed(spec, delta):
        return None
    carried_files.update(delta)
    return f"carried_forward_from {sha[:10]}"


def _epic_child_chain(gh, branch: str, children: list, base: str = "main") -> dict:
    """Every merged child PR of the epic (`{pr: {issue, view, files, checks}}`) and the shas
    of the commits on `branch` that came through none of them (`direct`; None when the
    branch has too many commits to enumerate)."""
    prs = {}
    for child in children:
        for pr in gh.pr_list_for_branch(issue_branch(child["number"]), state="merged"):
            n = pr["number"]
            prs[n] = {"issue": child["number"],
                      "view": gh.pr_view(n, "comments,headRefOid,mergeCommit"),
                      "files": gh.pr_files(n), "checks": gh.pr_checks(n)}
    merged = {(p["view"].get("mergeCommit") or {}).get("oid") for p in prs.values()}
    commits = gh.branch_commits(base, branch)
    direct = None if len(commits) >= 250 else [c for c in commits if c not in merged]
    return {"prs": prs, "direct": direct}


PR_FILES_CAP = 3000


COMPARE_FILES_CAP = 300


def _epic_suite_evidence(gh, branch: str, children: list, epic_files: list,
                         epic_pr: Optional[int], carry_forward: bool = True,
                         base: str = "main", deferred: tuple = ()) -> dict:
    """Per required suite (base `base`: `main`, or the opted-in Initiative's branch) at the
    epic head; a suite in `deferred` is `deferred_to_initiative` (the initiative -> main merge
    gates it). Otherwise how it is satisfied -- the epic
    PR's own check/attestation, or the child chain (every merged child PR that touched the
    suite is satisfied and no direct epic commit touches it). Returns `suites` (status per
    suite), `unattested` / `awaiting` (attestable / non-attestable suites still `missing`),
    `missing` (their workflows),
    `breaks` (why a chain failed) and `carried_files`."""
    # The PR files API lists up to 3000 paths; only the compare fallback stops at 300.
    truncated = len(epic_files) >= (PR_FILES_CAP if epic_pr is not None else COMPARE_FILES_CAP)
    view, checks = {}, []
    if epic_pr is not None:
        view = gh.pr_view(epic_pr, "comments,headRefOid")
        # A reconcile merge whose incoming files miss a suite's paths keeps that suite's
        # earlier pass (merge-pr's carry rule), so an independent main never re-requires CI.
        checks = effective_checks(gh, epic_pr) if carry_forward else gh.pr_checks(epic_pr)
    chain = None
    suites, unattested, awaiting, missing, breaks, carried = {}, [], [], [], {}, set()
    for spec in required_workflows(base):
        suite = spec["suite"]
        if not _workflow_applies_to_base(spec, base):
            continue
        if not truncated and not any(_workflow_covers(spec, p) for p in epic_files):
            suites[suite] = "not_required"
            continue
        if suite in deferred:
            suites[suite] = "deferred_to_initiative"
            continue
        status = _pr_suite_evidence(gh, spec, view, checks, branch, carry_forward, carried) \
            if epic_pr is not None else None
        if status is None and carry_forward:
            chain = chain or _epic_child_chain(gh, branch, children, base)
            status, why = _chain_suite_evidence(gh, spec, chain, branch, carried)
            if why:
                breaks[suite] = why
        if status is None:
            status = "missing"
            missing.append(spec["workflow"])
            (awaiting if suite_not_attestable(suite) else unattested).append(suite)
        suites[suite] = status
    return {"suites": suites, "unattested": unattested, "awaiting": awaiting, "missing": missing,
            "breaks": breaks, "carried_files": carried, "checks": checks}


def _chain_suite_evidence(gh, spec: dict, chain: dict, branch: str, carried: set) -> tuple:
    """`(status, breaks)` for one suite over the child chain: `children:#a,#b` when every
    child PR touching the suite is satisfied at its merged head and no direct commit touches
    it; else `(None, [reasons])`."""
    suite, breaks, attested = spec["suite"], [], []
    for n, pr in sorted(chain["prs"].items()):
        if not any(_workflow_covers(spec, p) for p in pr["files"]):
            continue
        head = pr["view"].get("headRefOid") or ""
        if _pr_suite_evidence(gh, spec, pr["view"], pr["checks"], head, True, carried):
            attested.append(pr["issue"])
        else:
            breaks.append(f"child #{pr['issue']} (PR #{n}) touched `{suite}` paths but has no "
                          f"`{suite}` attestation or passing check at its merged head")
    if chain["direct"] is None:
        breaks.append(f"`{branch}` has too many commits to attribute each to a child PR")
    else:
        for sha in chain["direct"]:
            files = gh.commit_files(sha)
            if len(files) >= 300 or any(_workflow_covers(spec, p) for p in files):
                breaks.append(f"commit {sha[:10]} on `{branch}` touches `{suite}` paths outside "
                              f"any child PR")
    if not attested and not breaks:
        breaks.append(f"no merged child PR accounts for the `{suite}` paths the epic changed")
    if breaks:
        return None, breaks
    return "children:" + ",".join(f"#{i}" for i in sorted(set(attested))), []


def cmd_record_epic_verification(gh: GitHub, epic: int, kind: str, summary: str,
                                 sha: Optional[str] = None, repo_path: Optional[str] = None,
                                 runner: Runner = _default_runner) -> dict:
    """Post one half (`e2e` | `exploratory`) of an epic's closing verification marker, stamped
    with the tested epic-branch head: `sha`, else `repo_path`'s HEAD (the tree actually
    tested), else the origin branch head now (`sha_source` arg | worktree | origin). A
    worktree HEAD off the origin tip adds `behind_origin` and `origin_sha`; the close-epic
    delta then judges the record against what landed since."""
    if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise GhError(f"--sha must be a 7-40 char hex commit id, got {sha!r}")
    extra: dict = {}
    if sha:
        source = "arg"
    elif repo_path:
        source, sha = "worktree", runner(["git", "-C", repo_path, "rev-parse", "HEAD"]).strip()
        try:
            origin_sha = gh.branch_head_sha(epic_branch(epic))
        except GhError:
            origin_sha = None
        if origin_sha and origin_sha != sha:
            extra = {"behind_origin": True, "origin_sha": origin_sha}
    else:
        source, sha = "origin", gh.branch_head_sha(epic_branch(epic))
    timestamp = _utc_now_marker()
    label = "Full e2e suite" if kind == "e2e" else "Exploratory pass"
    gh.issue_comment(epic,
        f"🧪 {label} — closing verification for #{epic} at `{sha[:10]}`. {summary}\n\n"
        f"<!-- epic-verification: {kind}:{epic} sha:{sha} @ {timestamp} -->")
    return {"epic": epic, "kind": kind, "sha": sha, "sha_source": source, **extra,
            "recorded": True}


def _close_epic_evidence(gh, epic: int, branch: str, children: list, epic_pr: Optional[int],
                         carry_forward: bool, base: str = "main", deferred: tuple = ()) -> dict:
    """The evidence fields every close-epic result carries: `missing_verification` (when
    any), `unattested_suites`, `evidence`, `carried_forward_files`, `evidence_breaks`,
    plus the private `_missing_workflows` the merge gate reads."""
    comments = gh.issue_view(epic).get("comments", [])
    delta = _EpicEvidenceDelta(gh, branch, carry_forward, base)
    problems = missing_epic_verification(comments, delta)
    files = gh.pr_files(epic_pr) if epic_pr is not None else gh.base_delta_files(base, base=branch)
    suites = _epic_suite_evidence(gh, branch, children, files, epic_pr, carry_forward,
                                  base, deferred)
    carried = set(suites["carried_files"])
    carried.update(f for d in delta.deltas.values() if d["status"] == "carried_forward"
                   for f in d["files"])
    result = {"unattested_suites": suites["unattested"],
              "awaiting_checks": suites["awaiting"],
              "evidence": {"exploratory": _exploratory_evidence_status(comments, delta, problems),
                           "suites": suites["suites"]},
              "carried_forward_files": sorted(carried),
              "_missing_workflows": suites["missing"]}
    if problems:
        result["missing_verification"] = problems
    stale = _stale_evidence_delta(comments, delta)
    if stale:
        result["stale_delta"] = stale
    if suites["breaks"]:
        result["evidence_breaks"] = suites["breaks"]
    failing = [c for c in suites["checks"] if _is_failed_check(c)]
    if failing:
        # Reported only; the suite gate above already decides whether the epic merges.
        result["infra_suspect"] = enrich_failed_checks(gh, failing)
        result["failing_checks"] = failing
    return result


def _stale_evidence_delta(comments: list, delta: _EpicEvidenceDelta) -> dict:
    """`{kind: {files (first 50), count}}` for each required kind whose latest record is
    `stale`, so the orchestrator can brief a re-pass scoped to what landed since."""
    latest = {}
    for c in comments:
        m = _EPIC_VERIFICATION_MARKER.search(c.get("body", ""))
        if m:
            latest[m.group(1)] = m.group(3)
    out = {}
    for kind, _ in EPIC_CLOSE_REQUIRED_EVIDENCE:
        d = delta.deltas.get(latest.get(kind) or "")
        if d and d["status"] == "stale":
            out[kind] = {"files": d["files"][:50], "count": len(d["files"])}
    return out


def _close_epic_exploratory_problems(gh, epic: int, branch: str, carry_forward: bool,
                                     base: Optional[str] = None) -> list:
    """Just the exploratory half of `_close_epic_evidence`, to decide before the epic PR is
    opened whether the second call can proceed at all."""
    return missing_epic_verification(gh.issue_view(epic).get("comments", []),
                                     _EpicEvidenceDelta(gh, branch, carry_forward, base))


def clean_epic_worktree(epic: int, repo_path: str = ".",
                        runner: Runner = _default_runner) -> dict:
    """Discard the closing run's untracked output (evidence JSONs, `uploads/`, ...) from the
    epic's live worktree: `git clean -fd` -- untracked, non-ignored files only (ignored ones
    leave with the tree at cleanup). Refuses (`refused`, `tracked_changes`) and touches
    nothing when any tracked file is modified, staged or deleted: that is not run output."""
    path = worktree_path_for_branch(epic_branch(epic), runner=runner, base_repo=repo_path)
    if path is None:
        return {"cleaned": False, "reason": "no epic worktree"}
    lines = [l for l in runner(["git", "-C", path, "status", "--porcelain"]).splitlines() if l]
    tracked = [l[3:] for l in lines if not l.startswith("??")]
    if tracked:
        return {"cleaned": False, "refused": True, "path": path, "tracked_changes": tracked,
                "reason": f"the epic worktree {path} has {len(tracked)} tracked change(s) -- "
                          f"not closing-run output; inspect them (never commit to epic-{epic} "
                          f"by hand), then re-run"}
    if not lines:
        return {"cleaned": False, "path": path, "removed": [], "reason": "already clean"}
    out = runner(["git", "-C", path, "clean", "-fd"])
    removed = [l[len("Removing "):] for l in out.splitlines() if l.startswith("Removing ")]
    return {"cleaned": True, "path": path, "removed": removed}


def _refresh_epic_worktree(branch: str, repo_path: str, runner: Runner) -> Optional[dict]:
    """Fast-forward the epic's live worktree (where the exploratory pass runs) to
    `origin/<branch>`; None without one. Never raises; a diverged tree is reported."""
    try:
        path = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
        if path is None:
            return None
        out = _resume_live_worktree(path, branch, repo_path, runner)
    except GhError as exc:
        return {"synced_to_origin": False, "error": True, "reason": str(exc)}
    return {k: v for k, v in out.items() if k not in ("created", "resumed", "reason")
            or (k == "reason" and out.get("diverged"))}


_PREPARE_UNSET_WARNING = (
    "pipeline.epicClose.prepare is unset: the epic worktree has no dependencies installed, so "
    "the exploratory pass may return blocked. Set it to the repo's install command "
    "(references/epics.md, \"Epic closing\"); until then run that install in the epic "
    "worktree before the pass.")


def _epic_close_prepare(worktree: Optional[dict], shell: Optional[Callable]) -> Optional[dict]:
    """Run `pipeline.epicClose.prepare` (e.g. a dependency install) in the epic worktree
    so the exploratory pass finds it ready; None when not configured. Never raises."""
    command = (PIPELINE["epicClose"].get("prepare") or "").strip()
    if not command:
        return None
    if not worktree or not worktree.get("path") or not worktree.get("synced_to_origin"):
        return {"command": command, "ok": False, "ran": False,
                "reason": "no epic worktree at origin's head -- `worktree-add <epic> --unit "
                          "epic`, then re-run close-epic (or run the command there yourself)"}
    try:
        output = (shell or _default_shell)(command, worktree["path"])
    except GhError as exc:
        return {"command": command, "ok": False, "ran": True, "path": worktree["path"],
                "error": str(exc)[-2000:]}
    return {"command": command, "ok": True, "ran": True, "path": worktree["path"],
            "output_tail": (output or "")[-500:]}


def _sync_initiative_for_close(gh: GitHub, repo_path: str, initiative: int,
                               runner: Runner) -> dict:
    """Merge `main` into `initiative-<i>` (clean merges only) before an Epic closes into it,
    so the epic PR's CI sees main's fixes. A conflict is reported, never forced."""
    try:
        out = cmd_sync_branch(gh, repo_path, initiative, unit="initiative", runner=runner,
                              base="main")
    except GhError as exc:
        return {"initiative": initiative, "synced": False, "error": True, "reason": str(exc)}
    return {k: v for k, v in out.items() if k != "issue"} | {"initiative": initiative}


def cmd_close_epic(gh: GitHub, epic: int, repo_path: str = ".",
                    runner: Runner = _default_runner, carry_forward: bool = True,
                    clean_worktree: bool = False, shell: Optional[Callable] = None) -> dict:
    """Reconcile the epic branch with `main` (first call, then stop), or merge it once
    closing evidence is recorded (second call). Returns `merged`; refusals carry `reason`.
    Two calls because verification must run between the two merges. Idempotent: once the
    epic PR is MERGED, only the post-merge bookkeeping runs (`recovered`). `carry_forward=False`
    accepts only evidence stamped at the epic head itself. `clean_worktree` runs
    `clean_epic_worktree` right before a reconcile or the merge -- never on a refusal --
    and refuses the call when the tree holds tracked changes (`worktree_clean`).
    Every result carries the resolved `base`. While verification is still owed (the calls
    before the exploratory pass): an Epic closing into `initiative-<i>` first merges `main`
    into it (`initiative_sync`; a conflict refuses the call), the epic's live worktree is
    fast-forwarded to origin (`epic_worktree`) and `pipeline.epicClose.prepare` runs there
    (`prepare`)."""
    detail = gh.issue_view(epic)
    if not resolve_profile(detail)["closes"]:
        return {"epic": epic, "merged": False,
                "reason": f"epic profile '{resolve_profile(detail)['name']}' does not close "
                          "(closes: false) -- no integration branch to merge"}
    branch = epic_branch(epic)
    all_issues = gh.issue_list()
    children = [i for i in all_issues if i.get("parent") and i["parent"]["number"] == epic]
    # `main`, or -- for an Epic of an opted-in Initiative -- `initiative-<i>`: the target of
    # the reconcile and the epic PR. Suites the profile defers are not demanded here.
    base = epic_base(gh, epic, all_issues)
    deferred: tuple = ()
    initiative = None
    if base != "main":
        by_number = {i["number"]: i for i in all_issues}
        initiative = initiative_of_in(by_number, epic)
        deferred = tuple(resolve_initiative_profile(initiative)["deferSuites"])
    target = {"base": base}
    merged_prs = gh.pr_list_for_branch(branch, state="merged")
    if merged_prs:
        # A repeat call after the merge: `origin/epic-<n>` is gone, so never compare it.
        return _finish_epic_close(gh, epic, detail, merged_prs[0]["number"], children,
                                  all_issues, repo_path, runner, {"recovered": True, **target},
                                  base=base)
    open_children = [c["number"] for c in children if c["state"] != "CLOSED"]
    if open_children:
        return {"epic": epic, "merged": False, "open_children": open_children,
                "reason": f"{len(open_children)} child issue(s) still open: "
                          f"{', '.join(f'#{n}' for n in open_children)}"}
    cleaned: dict = {}

    def clean_refusal() -> Optional[dict]:
        """Run the opt-in clean; the refusal to return when it found tracked changes."""
        if not clean_worktree:
            return None
        cleaned.update(worktree_clean=clean_epic_worktree(epic, repo_path, runner))
        if cleaned["worktree_clean"].get("refused"):
            return {"epic": epic, "merged": False, "branch": branch, **cleaned,
                    "reason": cleaned["worktree_clean"]["reason"]}
        return None

    if base != "main":
        # An Epic cut before its Initiative opted in: stand the initiative branch up first.
        ensure_branch_on_origin(repo_path, base, runner=runner)
    verifying = bool(_close_epic_exploratory_problems(gh, epic, branch, carry_forward, base))
    if verifying and initiative is not None:
        # Only while verification is owed: a later call never re-syncs, so a busy main
        # cannot keep moving the tree under a finished exploratory pass.
        target["initiative_sync"] = _sync_initiative_for_close(gh, repo_path,
                                                               initiative["number"], runner)
        if not target["initiative_sync"].get("synced"):
            sync = target["initiative_sync"]
            if not sync.get("base_missing"):
                return {"epic": epic, "merged": False, "branch": branch, **target,
                        "reason": f"`{base}` could not take `main` cleanly ("
                                  + (f"conflict on {', '.join(sync.get('conflicting_files') or [])}"
                                     if sync.get("conflict") else sync.get("reason", "sync failed"))
                                  + f") -- resolve it in `worktree-add {initiative['number']} "
                                    f"--unit initiative`'s tree, push, then re-run close-epic"}

    def ready_tree() -> dict:
        """The exploratory pass's tree: fast-forwarded, then prepared."""
        tree = _refresh_epic_worktree(branch, repo_path, runner)
        out = {"epic_worktree": tree} if tree else {}
        prepared = _epic_close_prepare(tree, shell)
        if prepared:
            return {**out, "prepare": prepared}
        if tree:
            out["prepare_warning"] = _PREPARE_UNSET_WARNING
        return out

    behind = gh.branch_behind_by(branch, base=base)
    skipped = _reconcile_skippable(gh, branch, base, behind) if carry_forward else None
    if skipped:
        target["reconcile_skipped"] = skipped
        behind = 0
    if behind:
        refused = clean_refusal()
        if refused:
            return refused
        # The epic branch rarely has a live worktree by close time; reconcile in
        # an ephemeral one under the branch lock, never in the main checkout.
        try:
            with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
                git_reconcile_branch(ws.path, branch, base=base, runner=runner)
        except MergeConflict as e:
            return {"epic": epic, "merged": False, "branch": branch, **target, **cleaned,
                    "conflict": True, "conflicting_files": e.files,
                    "reason": f"`{branch}` conflicts with `origin/{base}` on {len(e.files)} "
                              f"file(s) -- resolve it per references/parallelism.md, "
                              f"\"Git-conflict handling\" (epic reconcile), push "
                              f"`{branch}`, then re-run close-epic"}
        timestamp = _utc_now_marker()
        gh.issue_comment(epic,
            f"🔄 Reconciled `{branch}` with `origin/{base}` ({behind} commit(s) picked up). "
            f"Closing verification must now run against **this** tree — the exploratory pass. "
            f"Evidence recorded before this point describes a "
            f"different tree and will not be accepted.\n\n"
            f"<!-- epic-reconciled: {epic} @ {timestamp} -->")
        existing = gh.pr_list_for_branch(branch)
        evidence = _close_epic_evidence(gh, epic, branch, children,
                                        existing[0]["number"] if existing else None, carry_forward,
                                        base, deferred)
        evidence.pop("_missing_workflows")
        return _with_workspace(
            {"epic": epic, "merged": False, "branch": branch, "reconciled": behind, **target,
             **evidence, **cleaned, **ready_tree(),
             "reason": f"picked up {behind} commit(s) from {base} -- run the exploratory pass "
                       f"against the reconciled branch, then re-run close-epic"},
            ws)
    existing = gh.pr_list_for_branch(branch)
    if verifying:
        evidence = _close_epic_evidence(gh, epic, branch, children,
                                        existing[0]["number"] if existing else None, carry_forward,
                                        base, deferred)
        evidence.pop("_missing_workflows")
        return {"epic": epic, "merged": False, "branch": branch, **target, **evidence,
                **ready_tree(),
                "reason": f"closing verification incomplete: "
                          f"{'; '.join(evidence['missing_verification'])}"}
    collisions = _versioned_file_collisions(gh, branch, base, repo_path, runner)
    if collisions:
        return {"epic": epic, "merged": False, "branch": branch, **target,
                "version_collisions": collisions,
                "reason": "a versioned file's text changed but its version did not move past "
                          f"`{base}`'s: {'; '.join(collisions)} -- bump the version on "
                          f"`{branch}` (a Task's rework), then re-run close-epic"}
    if existing:
        pr_number = existing[0]["number"]
    else:
        # `Closes #<n>` only auto-closes on the default branch; close-epic closes it either way.
        pr_number = gh.pr_create(
            base=base, head=branch,
            title=f"{detail['title']} — epic integration (#{epic})",
            body=f"Integration of every child of #{epic}"
                 + (f" into `{base}` (the Initiative's branch; it reaches `main` at "
                    f"initiative close)" if base != "main" else "")
                 + f".\n\nCloses #{epic}",
            draft=True)
    # The PR's files, checks and attestations are the gate's inputs, so it must exist first.
    evidence = _close_epic_evidence(gh, epic, branch, children, pr_number, carry_forward,
                                    base, deferred)
    missing_workflows = evidence.pop("_missing_workflows")
    if missing_workflows:
        return {"epic": epic, "merged": False, "pr": pr_number, "checks": "missing-checks",
                **target, **evidence,
                "reason": f"PR #{pr_number} required suites unsatisfied at the epic head "
                          f"(no passing check, attestation or child chain from: "
                          f"{', '.join(missing_workflows)})"}
    # Cleaned only now the merge will happen, so a refused call keeps the run's output.
    refused = clean_refusal()
    if refused:
        return {**refused, "pr": pr_number, **evidence}
    gh.pr_ready(pr_number)
    gh.pr_merge(pr_number)
    return _finish_epic_close(gh, epic, None, pr_number, children, all_issues, repo_path,
                              runner, {**target, **evidence, **cleaned}, base=base)


def _versioned_file_collisions(gh, branch: str, base: str, repo_path: str,
                               runner: Runner) -> list:
    """`pipeline.epicClose.versionedFiles` entries (`{path, versionPattern}`) the epic changed
    whose version string still equals `base`'s: two branches setting one version for
    different text merge with no conflict, so nothing else catches it."""
    entries = PIPELINE["epicClose"].get("versionedFiles") or []
    if not entries:
        return []
    epic_files = set(gh.base_delta_files(base, base=branch))
    entries = [e for e in entries if e["path"] in epic_files]
    if not entries:
        return []
    _fetch_origin(repo_path, runner)

    def show(ref: str, path: str) -> Optional[str]:
        try:
            return runner(["git", "-C", repo_path, "show", f"origin/{ref}:{path}"])
        except GhError:
            return None  # added or deleted by the epic: no version to collide with

    def version(text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text)
        return (m.group(1) if m.groups() else m.group(0)) if m else None

    out = []
    for e in entries:
        ours, theirs = show(branch, e["path"]), show(base, e["path"])
        if ours is None or theirs is None or ours == theirs:
            continue
        v = version(ours, e["versionPattern"])
        if v == version(theirs, e["versionPattern"]):
            out.append(f"`{e['path']}` (version {v!r})")
    return out


def brief_close_epic(result: dict) -> dict:
    """`close-epic --brief`: the same result with each bulky list or map reduced to what the
    orchestrator acts on -- counts, suite names and failing check names."""
    out = dict(result)
    if "evidence" in out:
        suites = out["evidence"].get("suites") or {}
        out["evidence"] = {"exploratory": out["evidence"].get("exploratory"),
                           "suites": {k: (v.get("status") if isinstance(v, dict) else v)
                                      for k, v in suites.items()}}
    for key in ("carried_forward_files", "children_cleanup"):
        if key in out:
            out[key] = len(out[key])
    if "stale_delta" in out:
        out["stale_delta"] = {k: v.get("count") for k, v in out["stale_delta"].items()}
    if "failing_checks" in out:
        out["failing_checks"] = [c.get("name") for c in out["failing_checks"]]
    out.pop("cleanup", None)
    if "failing_checks" in out or "infra_suspect" in out:
        out["hint"] = (out.get("hint", "") + " " if out.get("hint") else "") + \
            "Run `ci-failure-summary <pr>` for the failing tests."
    return out


def _reconcile_skippable(gh, branch: str, base: str, behind: int) -> Optional[dict]:
    """`{behind, files}` when every file `base` gained lies in the evidence carry-forward set
    and the epic never touched it: a reconcile would only re-stale the closing evidence."""
    if not behind:
        return None
    try:
        incoming = gh.base_delta_files(branch, base=base)
        if not incoming or len(incoming) >= COMPARE_FILES_CAP:
            return None
        if not all(evidence_carries_forward(p) for p in incoming):
            return None
        epic_files = gh.base_delta_files(base, base=branch)
    except GhError:
        return None
    if len(epic_files) >= COMPARE_FILES_CAP or set(incoming) & set(epic_files):
        return None
    return {"behind": behind, "files": sorted(incoming)}


def _finish_epic_close(gh, epic: int, detail: Optional[dict], pr_number: int, children: list,
                       all_issues: list, repo_path: str, runner: Runner, extra: dict,
                       base: str = "main") -> dict:
    """Post-merge bookkeeping, each step safe to repeat: close the Epic issue unless GitHub's
    `Closes #<n>` already has (it lands asynchronously, and `next-action` must see it closed
    now), its terminal fields, the unit cleanup, run-state archive and stack teardown."""
    detail = detail or gh.issue_view(epic)
    if detail.get("state") != "CLOSED":
        try:
            gh.issue_close(epic)
        except GhError:
            pass  # GitHub closed it in between; mark-issue-closed below still runs
    cmd_mark_issue_closed(gh, epic)
    cleanup = cleanup_unit(gh, epic, "epic", repo_path, runner, base=base)
    closed_children = {c["number"] for c in children}
    children_cleanup = sweep_units(gh, repo_path, runner, only=closed_children,
                                   issues=all_issues)["units"]
    return _with_warnings(
        {"epic": epic, "merged": True, **extra, "pr": pr_number, "branch": epic_branch(epic),
         "worktree": cleanup.pop("worktree"), "cleanup": cleanup,
         "children_cleanup": children_cleanup,
         "run_state": archive_run_state(epic),
         "stack": _teardown_stack_after_close(epic)}, cleanup, *children_cleanup)


def _teardown_stack_after_close(epic: int) -> dict:
    """`teardown-epic-stack` once the epic merged (a no-op with the stack disabled); never raises."""
    try:
        return cmd_teardown_epic_stack(epic)
    except (GhError, OSError) as exc:
        return {"epic": epic, "torn_down": False, "reason": str(exc)}


_INITIATIVE_VERIFICATION_MARKER = re.compile(
    r"<!--\s*initiative-verification:\s*(\w+):(\d+)(?:\s+outcome:(\w+))?\s*(?:@[^>]*?)?-->")


INITIATIVE_VERIFICATION_OUTCOMES = ("met", "unmet")


def missing_initiative_verification(comments: list) -> list:
    """Problems with an Initiative's closing evidence: no `requirements` marker, or the latest
    one recorded `unmet`. No ordering check: an Initiative has no branch of its own."""
    latest = None
    for c in comments:
        m = _INITIATIVE_VERIFICATION_MARKER.search(c.get("body", ""))
        if m:
            latest = m.group(3) or "met"
    if latest is None:
        return ["no `requirements` closing-verification evidence (a PM-role pass "
                "validating the delivered app against product.md was never recorded)"]
    if latest != "met":
        return ["the latest `requirements` verification recorded outcome `unmet` -- fix the "
                "gaps it names, re-run initiative-close, and record a `met` outcome"]
    return []


def cmd_record_initiative_verification(gh: GitHub, initiative: int, summary: str,
                                       outcome: str = "met") -> dict:
    """Post an Initiative's `requirements` closing-verification marker with its outcome;
    `close-initiative` refuses while the latest one is `unmet`."""
    if outcome not in INITIATIVE_VERIFICATION_OUTCOMES:
        raise GhError(f"outcome must be one of {INITIATIVE_VERIFICATION_OUTCOMES}, got {outcome!r}")
    timestamp = _utc_now_marker()
    verdict = "every requirement met" if outcome == "met" else "requirements NOT met"
    gh.issue_comment(initiative,
        f"🧪 Requirements validation — closing verification for #{initiative}: {verdict}. "
        f"{summary}\n\n"
        f"<!-- initiative-verification: requirements:{initiative} outcome:{outcome} "
        f"@ {timestamp} -->")
    return {"initiative": initiative, "kind": "requirements", "outcome": outcome,
            "recorded": True}


def _initiative_epics_state(gh: GitHub, initiative: int) -> tuple:
    """`(initiative_issue, refusal)`: the refusal (`closeable: false` + `reason`, `open_epics`)
    while no Epic was cut or one is open, else `epics` in place of a refusal."""
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    initiative_issue = by_number.get(initiative)
    if initiative_issue is None or not is_initiative(initiative_issue):
        raise GhError(f"#{initiative} is not an Initiative -- pass the Initiative's own issue number")
    cut_epics = [i for i in all_issues if i.get("parent")
                 and i["parent"]["number"] == initiative and is_epic(i)]
    if not cut_epics:
        return initiative_issue, {"initiative": initiative, "closeable": False,
                                  "reason": "no Epics have been cut from this Initiative yet"}
    open_epics = sorted(i["number"] for i in cut_epics if i["state"] != "CLOSED")
    if open_epics:
        return initiative_issue, {"initiative": initiative, "closeable": False,
                                  "open_epics": open_epics,
                                  "reason": f"{len(open_epics)} cut Epic(s) still open: "
                                            f"{', '.join(f'#{n}' for n in open_epics)}"}
    return initiative_issue, {"epics": sorted(i["number"] for i in cut_epics)}


def initiative_landing(gh: GitHub, initiative: int) -> dict:
    """Whether an opted-in Initiative's branch reached `main`: `landed` when its latest PR
    `initiative-<i>` -> main is merged and the branch is gone or still at the merged head
    (a later Epic closing into a re-created branch un-lands it). Also `branch`, `pr`, `state`
    (`merged` | `open` | `none` for that latest PR)."""
    branch = initiative_branch(initiative)
    prs = sorted(gh.pr_heads_for_branch(branch), key=lambda p: p["number"])
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    merged = [p for p in prs if p.get("state") == "MERGED"]
    out = {"branch": branch, "pr": None, "state": "none", "landed": False}
    if open_prs:
        out.update(pr=open_prs[-1]["number"], state="open")
        return out
    if not merged:
        return out
    last = merged[-1]
    out.update(pr=last["number"], state="merged")
    try:
        head = gh.branch_head_sha(branch)
    except GhError:
        head = None  # deleted after the merge
    out["landed"] = not head or head == last.get("headRefOid")
    if not out["landed"]:
        out["state"] = "none"  # new work on the branch since that merge needs its own PR
        out["unlanded_head"] = head
    return out


def initiative_unlanded_commits(gh: GitHub, initiative: int) -> list:
    """Shas on `origin/initiative-<i>` that `main` lacks (one GitHub compare, capped at 250);
    `[]` when the branch does not exist. Any other read error raises."""
    try:
        return gh.branch_commits("main", initiative_branch(initiative))
    except GhError as e:
        if "HTTP 404" in str(e) or "Not Found" in str(e):
            return []
        raise


def cmd_check_initiative_closeable(gh: GitHub, initiative: int) -> dict:
    """`closeable` when at least one Epic was cut from the Initiative and all are closed --
    and, for an Initiative with an initiative branch, that branch reached `main`
    (`initiative_landing`); otherwise `reason` (and `open_epics` / `initiative_branch`).
    Whatever its labels now say, it also refuses while `initiative-<i>` holds work `main`
    lacks (`unlanded_commits`), e.g. Epics closed into it before its label was removed. That
    check runs only while some `initiativeProfiles` entry sets `branch` (no call otherwise:
    without one the control plane never closes an Epic into an initiative branch)."""
    initiative_issue, state = _initiative_epics_state(gh, initiative)
    if "epics" not in state:
        return state
    if not resolve_initiative_profile(initiative_issue)["branch"] and any(
            prof.get("branch") for prof in PIPELINE["initiativeProfiles"] or []):
        stray = initiative_unlanded_commits(gh, initiative)
        if stray:
            branch = initiative_branch(initiative)
            return {"initiative": initiative, "closeable": False, "epics": state["epics"],
                    "initiative_branch": {"branch": branch, "landed": False},
                    "unlanded_commits": len(stray),
                    "reason": f"`{branch}` holds {len(stray)}{'+' if len(stray) >= 250 else ''} "
                              f"commit(s) not on main, but #{initiative} no longer matches an "
                              f"`initiativeProfiles` entry with `branch: true` (its label was "
                              f"removed?) -- restore the label, then `open-initiative-pr "
                              f"{initiative} --repo-path <p>` and `merge-initiative-pr "
                              f"{initiative}`; or land (or deliberately delete) the branch by "
                              f"hand, then re-run"}
    if resolve_initiative_profile(initiative_issue)["branch"]:
        landing = initiative_landing(gh, initiative)
        if not landing["landed"]:
            step = (f"`merge-initiative-pr {initiative}` once its evidence is green"
                    if landing["state"] == "open" else
                    f"`open-initiative-pr {initiative} --repo-path <p>`, then "
                    f"`merge-initiative-pr {initiative}` once its evidence is green")
            return {"initiative": initiative, "closeable": False, "epics": state["epics"],
                    "initiative_branch": landing,
                    "reason": f"every cut Epic is closed, but `{landing['branch']}` has not "
                              f"reached main: {step} (references/initiatives.md, \"Closing an Initiative\")"}
    return {"initiative": initiative, "closeable": True, "epics": state["epics"]}


_SCAFFOLDING_HITS_CAP = 50


def initiative_scaffolding(gh: GitHub, initiative: int, repo_path: str = ".",
                           runner: Runner = _default_runner,
                           all_issues: Optional[list] = None) -> list:
    """What still names the landed initiative branch or defers work to "Initiative close":
    `{path, line, text}` grep hits on `origin/main` (design docs aside) and `{issue, title}`
    for closed descendants whose body says so. A list to clear, never a gate."""
    needles = (initiative_branch(initiative), "Initiative close")
    hits: list = []
    try:
        _run_retry_transient(["git", "-C", repo_path, "fetch", "origin", "main"], runner)
        out = runner(["git", "-C", repo_path, "grep", "-n", "-I", "-F",
                      *[a for n in needles for a in ("-e", n)], "origin/main", "--"])
    except GhError:
        out = ""  # git grep exits 1 on no match
    roots = tuple(f"{r}/" for r in all_doc_roots())
    for raw in out.splitlines():
        parts = raw.split(":", 3)
        if len(parts) < 4 or parts[1].startswith(roots):
            continue
        hits.append({"path": parts[1], "line": int(parts[2]), "text": parts[3].strip()[:200]})
    issues = all_issues if all_issues is not None else gh.issue_list()
    for i in _descendants(issues, initiative):
        if i.get("state") == "CLOSED" and any(n in (i.get("body") or "") for n in needles):
            hits.append({"issue": i["number"], "title": i.get("title")})
    return hits[:_SCAFFOLDING_HITS_CAP]


def cmd_close_initiative(gh: GitHub, initiative: int, repo_path: str = ".",
                         runner: Runner = _default_runner) -> dict:
    """Close the Initiative once every cut Epic is closed and its verification is
    recorded. Returns `closed`; refusals carry `reason`. Closing one that had a branch also
    returns `scaffolding` (`initiative_scaffolding`)."""
    check = cmd_check_initiative_closeable(gh, initiative)
    if not check["closeable"]:
        return {"initiative": initiative, "closed": False,
                **{k: v for k, v in check.items() if k not in ("closeable",)}}
    detail = gh.issue_view(initiative)
    missing = missing_initiative_verification(detail.get("comments", []))
    if missing:
        return {"initiative": initiative, "closed": False, "missing_verification": missing,
                "reason": f"closing verification incomplete: {'; '.join(missing)}"}
    extra: dict = {}
    if (PIPELINE["initiativeProfiles"]
            and resolve_initiative_profile(gh.issue_epic_info(initiative))["branch"]):
        extra["scaffolding"] = initiative_scaffolding(gh, initiative, repo_path, runner)
    gh.issue_close(initiative)
    # Also set here so a repo without the `issues: closed` Action still ends correct.
    cmd_mark_issue_closed(gh, initiative)
    return {"initiative": initiative, "closed": True, "epics": check["epics"], **extra}


def _initiative_branch_refusal(gh: GitHub, initiative: int, action: str) -> tuple:
    """`(initiative_issue, epics, refusal)` shared by the two initiative-PR commands: refused
    for an Initiative without an initiative branch, or while an Epic is open / none was cut."""
    initiative_issue, state = _initiative_epics_state(gh, initiative)
    if not resolve_initiative_profile(initiative_issue)["branch"]:
        return initiative_issue, [], {
            "initiative": initiative, action: False,
            "reason": f"Initiative #{initiative} has no initiative branch (no "
                      f"`pipeline.initiativeProfiles` entry with `branch: true` matches its "
                      f"labels) -- its Epics merged to main at their own close"}
    if "epics" not in state:
        return initiative_issue, [], {**{k: v for k, v in state.items() if k != "closeable"},
                                      action: False}
    return initiative_issue, state["epics"], None


def _initiative_pr_evidence(gh: GitHub, initiative_issue: dict, pr: int) -> dict:
    """The initiative PR's merge gate at its head: every required workflow its files touch
    (base `main`, deferred suites included) by passing check or `local-ci` attestation, plus
    the profile's `closeSuites` attested at the head. `ready` when nothing is owed."""
    view = gh.pr_view(pr, "comments,headRefOid")
    head = view.get("headRefOid")
    comments = view.get("comments", [])
    checks = gh.pr_checks(pr)
    files = gh.pr_files(pr)
    status, missing = merge_gate_status(files, checks, comments, head, "main")
    attested = local_ci_suites_attested(comments, head)
    close_suites = resolve_initiative_profile(initiative_issue)["closeSuites"]
    unattested = [suite for suite in close_suites if suite not in attested]
    out = {"head": head, "checks": status, "missing_workflows": missing,
           "close_suites": close_suites, "unattested_close_suites": unattested,
           "ready": status == "passed" and not unattested}
    failing = [c for c in checks if _is_failed_check(c)]
    if failing:
        out["failing_checks"] = [c.get("name") or "?" for c in failing]
        out["infra_suspect"] = enrich_failed_checks(gh, failing)
    return _with_uncovered(out, files)


def cmd_open_initiative_pr(gh: GitHub, initiative: int, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """Once every cut Epic is closed: sync `initiative-<i>` with `main`, then open (or reuse)
    the PR `initiative-<i>` -> `main`. Idempotent; refuses (`opened: false`, `reason`) without
    the opt-in, while an Epic is open, on a sync conflict, or once the branch already landed.
    Returns `pr`, `sync` and the PR's `evidence` owed (`_initiative_pr_evidence`)."""
    initiative_issue, epics, refusal = _initiative_branch_refusal(gh, initiative, "opened")
    if refusal:
        return refusal
    landing = initiative_landing(gh, initiative)
    branch = landing["branch"]
    if landing["landed"]:
        return {"initiative": initiative, "opened": False, "pr": landing["pr"], "landed": True,
                "reason": f"`{branch}` already reached main via PR #{landing['pr']} -- "
                          f"nothing to open; run initiative-close next"}
    sync = cmd_sync_branch(gh, repo_path, initiative, "initiative", runner=runner, base="main")
    if not sync.get("synced"):
        return {"initiative": initiative, "opened": False, "branch": branch, "sync": sync,
                "reason": sync.get("reason") or (
                    f"`{branch}` conflicts with main ({', '.join(sync.get('conflicting_files', []))})"
                    f" -- resolve it in `worktree-add {initiative} --unit initiative`'s tree, "
                    f"push, then re-run open-initiative-pr")}
    if landing["state"] == "open":
        pr, created = landing["pr"], False
    else:
        pr = gh.pr_create(
            base="main", head=branch,
            title=f"{initiative_issue['title']} — initiative integration (#{initiative})",
            body=(f"Every Epic of Initiative #{initiative} "
                  f"({', '.join(f'#{n}' for n in epics)}), integrated on `{branch}`.\n\n"
                  f"Merged by `merge-initiative-pr {initiative}` once the required checks and "
                  f"suite attestations are green at its head; #{initiative} then closes after "
                  f"its requirements validation (`close-initiative`)."),
            draft=False)
        created = True
    return {"initiative": initiative, "opened": True, "created": created, "pr": pr,
            "branch": branch, "epics": epics, "sync": sync,
            "evidence": _initiative_pr_evidence(gh, initiative_issue, pr)}


def cmd_merge_initiative_pr(gh: GitHub, initiative: int, repo_path: str = ".",
                            runner: Runner = _default_runner) -> dict:
    """The initiative branch's only path to `main`: merge its open PR when every cut Epic is
    closed, the branch is not behind main, and `_initiative_pr_evidence` is `ready` (required
    checks green or attested, `closeSuites` attested at the head). Then clean the branch up.
    Refusals are exit 0 (`merged: false`, `reason`); an already-merged PR only re-runs the
    cleanup (`recovered: true`)."""
    initiative_issue, epics, refusal = _initiative_branch_refusal(gh, initiative, "merged")
    if refusal:
        return refusal
    landing = initiative_landing(gh, initiative)
    branch = landing["branch"]
    if landing["landed"]:
        cleanup = cleanup_unit(gh, initiative, "initiative", repo_path, runner, base="main")
        return _with_warnings({"initiative": initiative, "merged": True, "recovered": True,
                               "pr": landing["pr"], "branch": branch, "cleanup": cleanup}, cleanup)
    if landing["state"] != "open":
        return {"initiative": initiative, "merged": False, "branch": branch,
                "reason": f"no open PR `{branch}` -> main -- run "
                          f"`open-initiative-pr {initiative} --repo-path <p>` first"}
    pr = landing["pr"]
    behind = gh.branch_behind_by(branch, base="main")
    if behind:
        return {"initiative": initiative, "merged": False, "pr": pr, "behind_base": behind,
                "reason": f"`{branch}` is {behind} commit(s) behind main -- re-run "
                          f"`open-initiative-pr {initiative}` (it syncs the branch), let the "
                          f"checks and the final suite runs re-attest the new head, then re-run"}
    evidence = _initiative_pr_evidence(gh, initiative_issue, pr)
    if not evidence["ready"]:
        owed = [*(f"workflow `{w}`" for w in evidence["missing_workflows"]),
                *(f"`{s}` attestation" for s in evidence["unattested_close_suites"])]
        return {"initiative": initiative, "merged": False, "pr": pr, "evidence": evidence,
                "reason": f"PR #{pr} is not ready at head {str(evidence['head'])[:10]}: checks "
                          f"{evidence['checks']}"
                          + (f"; owed: {', '.join(owed)}" if owed else "")
                          + (f" ({_MISSING_WORKFLOW_HINT})" if evidence["missing_workflows"]
                             else "")}
    gh.pr_ready(pr)
    try:
        gh.pr_merge(pr, match_head=evidence["head"])
    except GhError:
        if gh.pr_view(pr, fields="state").get("state") != "MERGED":
            raise
    gh.issue_comment(initiative,
        f"🚢 `{branch}` merged to `main` via PR #{pr} (Epics "
        f"{', '.join(f'#{n}' for n in epics)}). Next: requirements validation, then "
        f"`close-initiative {initiative}`.\n\n"
        f"<!-- initiative-merged: {initiative} pr:{pr} @ {_utc_now_marker()} -->")
    cleanup = cleanup_unit(gh, initiative, "initiative", repo_path, runner, base="main")
    return _with_warnings({"initiative": initiative, "merged": True, "pr": pr, "branch": branch,
                           "epics": epics, "evidence": evidence, "cleanup": cleanup,
                           "scaffolding": initiative_scaffolding(gh, initiative, repo_path,
                                                                 runner)}, cleanup)


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import gate_eval  # noqa: E402,F401
from .run_state import archive_run_state  # noqa: E402
from .plugin_version import worktree_path_for_branch  # noqa: E402
from .worktrees import (  # noqa: E402
    BranchWorkspace, _fetch_origin, _resume_live_worktree, _run_retry_transient, _with_workspace, branch_lock,
    ensure_branch_on_origin,
)
from .design_gates import MergeConflict, cmd_sync_branch, git_reconcile_branch  # noqa: E402
from .checks import (  # noqa: E402
    _MISSING_WORKFLOW_HINT, _is_failed_check,
    _latest_attested_sha_by_suite, _suite_reattest_needed, _with_uncovered,
    _workflow_applies_to_base, _workflow_covers, effective_checks, enrich_failed_checks,
    merge_gate_status, required_workflows, suite_not_attestable, touches_pipeline_config,
    workflow_check_state,
)
from .issue_ops import (  # noqa: E402
    _with_warnings, cleanup_unit, cmd_mark_issue_closed, sweep_units,
)
from .cloud import _descendants  # noqa: E402
from .stack import _default_shell, cmd_teardown_epic_stack  # noqa: E402
