"""Required checks, suite evidence, attestation staleness and `merge-pr`."""
from __future__ import annotations

import fnmatch
import io
import json
import os
import re
from typing import Optional

from .config import (
    CI_INFRA_FAILURE_PATTERNS, CONFIG, CONFIG_FILENAME, PIPELINE, Runner, _find_config,
    all_doc_roots, issue_branch,
)
from .github import GhError, GitHub, WorkItemProvider, _default_runner, _utc_now_marker
from .units import cloud_skips_local_cleanup, current_stage, resolve_initiative_profile
from .evidence import (
    _LOCAL_CI_MARKER, _glob_matches, epic_branch, integration_base, local_ci_suites_attested,
    missing_pipeline_evidence,
)
from .run_state import record_terminal_unit
from .worktrees import _docker_cleanup_enabled, docker_sweep, release_worktree, unit_docker_prefix
from .design_gates import DESIGN_STAGE_REVIEW
from .lld_tasks import realised_issues
from .gates import phase_task_parent


def checks_status(checks: list) -> str:
    if not checks:
        return "passed"
    buckets = {c["bucket"] for c in checks}
    if "fail" in buckets or "cancel" in buckets:
        return "failed"
    if buckets - {"pass", "skipping"}:
        return "pending"
    return "passed"


# Workflows required when a PR touches their paths. `workflow` must match the
# workflow file's `name:`; `suite` names the `local-ci` attestation that can stand in for it.
# `excludeGlobs` mirrors the workflow's path negations (`!**/*.md`): a match never counts.
# `bases`: the PR base branches (names or fnmatch globs) this entry applies to (empty = every
# base). `attestable`:
# False when no `local-ci` attestation can stand in for it (a passing GHA check only).
def _normalise_workflows(raw: list) -> tuple:
    return tuple(
        {"workflow": w["workflow"], "suite": w["suite"],
         "prefixes": tuple(w["prefixes"]), "files": tuple(w.get("files", ())),
         "excludeGlobs": tuple(w.get("excludeGlobs", ())),
         "bases": tuple(w.get("bases", ())),
         "attestable": w.get("attestable", True)}
        for w in raw)


REQUIRED_WORKFLOWS = _normalise_workflows(CONFIG["requiredWorkflows"])


_BASE_WORKFLOWS_CACHE: dict = {}


def _base_config_workflows(base_ref: str, runner: "Runner" = None) -> list:
    """`requiredWorkflows` from the config as committed on `origin/<base_ref>`; [] when it
    cannot be read (no such ref, no config there, not a git checkout)."""
    path = _find_config()
    if not path:
        return []
    run = runner or _default_runner
    try:
        top = run(["git", "-C", os.path.dirname(os.path.abspath(path)), "rev-parse",
                   "--show-toplevel"]).strip()
        rel = os.path.relpath(os.path.abspath(path), top)
        raw = json.loads(run(["git", "-C", top, "show", f"origin/{base_ref}:{rel}"]))
        return list(raw.get("requiredWorkflows") or [])
    except (GhError, OSError, ValueError, AttributeError):
        return []


def required_workflows(base_ref: Optional[str] = None) -> tuple:
    """`REQUIRED_WORKFLOWS` plus any entry the base branch's own config adds (a workflow an
    epic or initiative branch introduced before `main` has it). Local entries win by name."""
    if not base_ref or base_ref == "main":
        return REQUIRED_WORKFLOWS
    if base_ref not in _BASE_WORKFLOWS_CACHE:
        try:
            _BASE_WORKFLOWS_CACHE[base_ref] = _normalise_workflows(
                _base_config_workflows(base_ref))
        except (KeyError, TypeError):
            _BASE_WORKFLOWS_CACHE[base_ref] = ()
    names = {w["workflow"] for w in REQUIRED_WORKFLOWS}
    return REQUIRED_WORKFLOWS + tuple(w for w in _BASE_WORKFLOWS_CACHE[base_ref]
                                      if w["workflow"] not in names)


# Suites whose attestation `record-local-ci` refuses (`attestable: false`): only a passing
# GHA check satisfies their required workflow.
NON_ATTESTABLE_SUITES = frozenset(
    w["suite"] for w in CONFIG["requiredWorkflows"] if w.get("attestable") is False)


def _workflow_covers(spec: dict, path: str) -> bool:
    """Whether the workflow's `paths:` filter (prefixes/files minus excludeGlobs) matches.
    A leading `**/` also matches at the repo root, as in GHA."""
    if any(_glob_matches(path, g) for g in spec["excludeGlobs"]):
        return False
    return path.startswith(spec["prefixes"]) or path in spec["files"]


def _workflow_applies_to_base(spec: dict, base_ref: Optional[str]) -> bool:
    """Whether a base-scoped entry applies to a PR into `base_ref`. An entry with no `bases`
    applies to every base; a scoped entry applies only when `base_ref` matches one of its
    names or fnmatch globs (`initiative-*`); an unknown `base_ref` never matches."""
    if not spec["bases"]:
        return True
    return base_ref is not None and any(fnmatch.fnmatchcase(base_ref, b) for b in spec["bases"])


def workflow_check_state(checks: list, workflow: str) -> str:
    """One required workflow's check runs on a head, as `passing` | `failing` | `pending` |
    `skipped` | `missing`. Every gate that accepts a GHA check reads it through this: a
    workflow passes only when no run failed, was cancelled or is still running."""
    buckets = [c.get("bucket") for c in checks if c.get("workflow", "") == workflow]
    if not buckets:
        return "missing"
    if "fail" in buckets or "cancel" in buckets:
        return "failing"
    if set(buckets) - {"pass", "skipping"}:
        return "pending"
    return "passing" if "pass" in buckets else "skipped"


_CARRY_MAX_SYNCS = 5


def effective_checks(gh: GitHub, pr: int) -> list:
    """The PR head's checks, with each required workflow still pending, cancelled or absent
    on a sync merge carried from the head it last passed on (`_carry_checks_across_sync`)."""
    checks = gh.pr_checks(pr)
    if not PIPELINE["ci"].get("carryChecksAcrossSync", True) \
            or not hasattr(gh, "commit_checks"):
        return checks
    try:
        return _carry_checks_across_sync(gh, pr, checks)
    except GhError:
        return checks


def _carriable(checks: list, workflow: str) -> bool:
    buckets = {c.get("bucket") for c in checks if c.get("workflow", "") == workflow}
    return not buckets - {"pending", "cancel"}


def _carry_checks_across_sync(gh: GitHub, pr: int, checks: list) -> list:
    """Walk back through sync merges (two-parent heads, first parent = the PR's own line) to
    the last head a workflow passed on; carry that pass when nothing merged in since touches
    the workflow's paths or the pipeline config (`_suite_reattest_needed`)."""
    view = gh.pr_view(pr, "headRefOid,baseRefName")
    head, base = view.get("headRefOid"), view.get("baseRefName")
    files = gh.pr_files(pr)
    wanted = [spec for spec in required_workflows(base)
              if _workflow_applies_to_base(spec, base)
              and any(_workflow_covers(spec, p) for p in files)
              and _carriable(checks, spec["workflow"])]
    sha, carried = head, {}
    for _ in range(_CARRY_MAX_SYNCS):
        if not wanted:
            break
        parents = gh.commit_parents(sha)
        if len(parents) != 2:
            break
        sha = parents[0]
        delta = gh.files_since(sha, head)
        prior = gh.commit_checks(sha)
        for spec in list(wanted):
            if _suite_reattest_needed(spec, delta):
                wanted.remove(spec)
            elif workflow_check_state(prior, spec["workflow"]) == "passing":
                carried[spec["workflow"]] = sha
                wanted.remove(spec)
    if not carried:
        return checks
    kept = [c for c in checks if c.get("workflow", "") not in carried]
    return kept + [{"name": f"{w} (carried)", "state": "SUCCESS", "bucket": "pass",
                    "link": "", "workflow": w, "carried_from": sha}
                   for w, sha in carried.items()]


def missing_required_workflows(changed_files: list, checks: list,
                               comments: list = None, head_sha: str = None,
                               base_ref: Optional[str] = None, deferred: tuple = ()) -> list:
    """Names of required workflows the PR touches that have neither a passing GHA check
    from that workflow nor a `local-ci` attestation for `head_sha`. A base-scoped entry
    (`bases`) is skipped unless the PR's `base_ref` matches it."""
    attested = local_ci_suites_attested(comments or [], head_sha)
    missing = []
    for spec in required_workflows(base_ref):
        if not _workflow_applies_to_base(spec, base_ref):
            continue
        if not any(_workflow_covers(spec, p) for p in changed_files):
            continue
        if workflow_check_state(checks, spec["workflow"]) == "passing":
            continue
        if spec.get("suite") in attested or spec.get("suite") in deferred:
            continue
        missing.append(spec["workflow"])
    return missing


def _pr_deferred_suites(gh: WorkItemProvider, head_ref: Optional[str],
                        base_ref: Optional[str]) -> tuple:
    """The Initiative profile's `deferSuites` for an epic PR `epic-<n>` -> `initiative-<i>`
    (what close-epic defers to the initiative -> main merge), else `()`."""
    head, base = _unit_of_branch(head_ref), _unit_of_branch(base_ref)
    if not (head and head[0] == "epic" and base and base[0] == "initiative"):
        return ()
    try:
        info = gh.issue_epic_info(base[1])
    except GhError:
        return ()
    return tuple(resolve_initiative_profile(info)["deferSuites"])


def base_delta_needs_reattest(base_delta_files: list) -> bool:
    """Whether a behind-base PR must re-sync and re-attest, given the files its base
    changed. False only for a fully read delta of docs that touches no required suite
    or pipeline config; an unrecognised path counts as code."""
    # 300 is GitHub's compare-API file cap: the delta may be truncated.
    if len(base_delta_files) >= 300:
        return True
    if touches_pipeline_config(base_delta_files):
        return True
    for spec in REQUIRED_WORKFLOWS:
        if any(_workflow_covers(spec, p) for p in base_delta_files):
            return True
    return not all(_is_doc_path(p) for p in base_delta_files)


def _is_doc_path(path: str) -> bool:
    """True for a path under any configured doc root, any `*.md`, or anything under `docs/`."""
    return (path.startswith(tuple(f"{root}/" for root in all_doc_roots()))
            or path.endswith(".md")
            or path.startswith("docs/"))


_UNCOVERED_HINT = "no required workflow covers these paths; no suite ran for them"


def uncovered_paths(changed_files: list, base_ref: Optional[str] = None) -> list:
    """Changed non-doc files (the pipeline config aside) no required workflow covers on any
    base: a config coverage gap. A path whose suite is scoped to another base is covered."""
    workflows = required_workflows(base_ref)
    return [p for p in changed_files
            if not _is_doc_path(p) and os.path.basename(p) != CONFIG_FILENAME
            and not any(_workflow_covers(w, p) for w in workflows)]


def _with_uncovered(result: dict, changed_files: list, base_ref: Optional[str] = None) -> dict:
    """`result` plus `uncovered_paths`/`uncovered_hint` when any; a warning, never a gate."""
    uncovered = uncovered_paths(changed_files, base_ref)
    return {**result, "uncovered_paths": uncovered, "uncovered_hint": _UNCOVERED_HINT} \
        if uncovered else result


def touches_pipeline_config(changed_files: list) -> bool:
    """True when the changes include the driven repo's pipeline config file."""
    return any(os.path.basename(f) == CONFIG_FILENAME for f in changed_files)


def merge_gate_status(changed_files: list, checks: list,
                      comments: list = None, head_sha: str = None,
                      base_ref: Optional[str] = None, deferred: tuple = ()) -> tuple:
    """`(status, missing_workflows)` for pr-checks and merge-pr. A pending/failed check
    status wins over `missing-checks`, but `missing` is always returned alongside it.
    Suites in `deferred` are never missing (`_pr_deferred_suites`)."""
    status = checks_status(checks)
    missing = missing_required_workflows(changed_files, checks, comments, head_sha, base_ref,
                                         deferred)
    if status != "passed":
        return status, missing
    return ("missing-checks" if missing else "passed"), missing


# Why a required workflow's check can be absent, ordered most-likely first; surfaced by
# pr-checks/merge-pr so `missing-checks` is not mistaken for "a check still running".
_MISSING_WORKFLOW_HINT = (
    "A required workflow reported no check on this head. Likely causes, in order: (1) the "
    "workflow does not run on this PR (e.g. main-only) and its suite is not attested for the "
    "current head -- re-run the suite and `record-local-ci` for this head; (2) the workflow "
    "file is not on the PR branch (added on "
    "the base after the branch was cut) -- run `sync-branch` and push so it can run; (3) the "
    "workflow was renamed/disabled out of step with `requiredWorkflows` -- a config defect, "
    "`mark-needs-human`.")


def _latest_attested_sha_by_suite(comments: list) -> dict:
    """The most recent attested sha per suite from `local-ci` markers (comments in order)."""
    latest = {}
    for c in comments:
        for m in _LOCAL_CI_MARKER.finditer(c.get("body", "")):
            latest[m.group(1)] = m.group(3).lower()
    return latest


def _suite_reattest_needed(spec: dict, delta_files: list) -> bool:
    """Whether the files changed since a suite's attested sha force a re-attest: a truncated
    delta, a pipeline-config change, or any file the suite's workflow covers. Mirrors merge-pr's
    carry-forward rule, scoped to one suite."""
    if len(delta_files) >= 300 or touches_pipeline_config(delta_files):
        return True
    return any(_workflow_covers(spec, p) for p in delta_files)


def _held_suite_reason(suite: str, workflow: str, head: str, attested_sha: Optional[str],
                       check: str) -> str:
    """Why a required suite holds the PR: its attestation state and its check's state."""
    attested = (f"local attestation is at {attested_sha[:7]}, not head {head[:7]},"
                if attested_sha else f"no local attestation on head {head[:7]}")
    return f"`{suite}`: {attested} and check '{workflow}' is {check}"


def pr_stale_attestations(gh: WorkItemProvider, issue: int, pr: int) -> dict:
    """For each attestable required workflow the PR touches (base-scoped), whether its suite is
    satisfied at the PR head: a fresh attestation, a passing GHA check for the workflow on that
    head, or an attestation carried forward when the only files changed since its sha fall
    outside the suite's coverage (the merge-pr carry-forward rule). Returns `{"stale":
    [{suite, workflow, attested_sha, check, reason}], "carried": [suite, ...],
    "passed_by_check": [suite, ...]}`; a suite with no attestation is `stale` with
    `attested_sha: None`. A non-attestable suite is never listed: merge-pr alone gates it."""
    view = gh.pr_view(pr, "comments,headRefOid,baseRefName")
    head = view.get("headRefOid")
    comments = view.get("comments", [])
    base_ref = view.get("baseRefName")
    changed = gh.pr_files(pr)
    checks = effective_checks(gh, pr)
    fresh = local_ci_suites_attested(comments, head)
    latest = _latest_attested_sha_by_suite(comments)
    stale, carried, passed_by_check = [], [], []
    for spec in required_workflows(base_ref):
        suite = spec.get("suite")
        if not suite or suite in NON_ATTESTABLE_SUITES:
            continue
        if not _workflow_applies_to_base(spec, base_ref):
            continue
        if not any(_workflow_covers(spec, p) for p in changed):
            continue
        if suite in fresh:
            continue
        check = workflow_check_state(checks, spec["workflow"])
        if check == "passing":
            passed_by_check.append(suite)
            continue
        attested_sha = latest.get(suite)
        if attested_sha:
            try:
                delta = gh.files_since(attested_sha, issue_branch(issue))
            except GhError:
                delta = None
            if delta is not None and not _suite_reattest_needed(spec, delta):
                carried.append(suite)
                continue
        stale.append({"suite": suite, "workflow": spec["workflow"], "attested_sha": attested_sha,
                      "check": check,
                      "reason": _held_suite_reason(suite, spec["workflow"], head or "",
                                                   attested_sha, check)})
    return {"stale": stale, "carried": carried, "passed_by_check": passed_by_check}


_CHECK_LINK_RE = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")


_FAILURE_EXCERPT_LINES = 20


def _check_run_and_job(check: dict) -> tuple:
    """`(run_id, job_id)` parsed from an Actions check's `link`; either is None when absent."""
    m = _CHECK_LINK_RE.search(check.get("link") or "")
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def _is_failed_check(check: dict) -> bool:
    return check.get("bucket") in ("fail", "cancel")


def infra_suspect_text(text: Optional[str]) -> bool:
    """Whether a failure log matches any `pipeline.ci.infraFailurePatterns` entry."""
    if not text:
        return False
    for pattern in CI_INFRA_FAILURE_PATTERNS:
        try:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def enrich_failed_checks(gh, checks: list) -> bool:
    """Add `failure_excerpt` (last <=20 non-empty lines of the job's failed-step log, or None
    when it cannot be fetched/parsed) and `infra_suspect` to every fail/cancel check, in place.
    Fetches nothing for other checks and never raises. Returns whether any is infra-suspect."""
    any_suspect = False
    for check in checks:
        if not _is_failed_check(check):
            continue
        excerpt = None
        try:
            _, job = _check_run_and_job(check)
            if job is not None:
                lines = [l for l in gh.job_failed_log(job).splitlines() if l.strip()]
                excerpt = "\n".join(lines[-_FAILURE_EXCERPT_LINES:]) or None
        except Exception:  # a missing log must never mask the check result itself
            excerpt = None
        check["failure_excerpt"] = excerpt
        check["infra_suspect"] = infra_suspect_text(excerpt)
        any_suspect = any_suspect or check["infra_suspect"]
    return any_suspect


# Lines of a failed-step log that name a failure; everything else (install, build chatter) is dropped.
_FAILURE_LINE_RE = re.compile(
    r"\bFAIL(ED)?\b|✕|✗|●|\berror\b|assert|Expected|Received|\bexpect\(|Traceback|"
    r"Exception|exit code [1-9]|ENOSPC|Timeout", re.IGNORECASE)
_LOG_PREFIX_RE = re.compile(r"^[^\t]*\t[^\t]*\t(?:\d{4}-\d\d-\d\dT[\d:.]+Z ?)?")
CI_SUMMARY_LINES_PER_JOB = 25
CI_SUMMARY_CHAR_CAP = 4000


def _failure_lines(log: str) -> list:
    """The failure-naming lines of a `--log-failed` log, job/step/timestamp prefix stripped,
    de-duplicated in order."""
    seen, out = set(), []
    for raw in log.splitlines():
        line = _LOG_PREFIX_RE.sub("", raw).rstrip()
        if line.strip() and _FAILURE_LINE_RE.search(line) and line not in seen:
            seen.add(line)
            out.append(line[:300])
    return out


def cmd_ci_failure_summary(gh: GitHub, pr_number: int) -> dict:
    """A PR's failing checks with only their failure lines (failing tests, assertions,
    errors), bounded per job and in total, so a raw CI log never reaches the orchestrator."""
    checks = gh.pr_checks(pr_number)
    budget = CI_SUMMARY_CHAR_CAP
    out = []
    for check in (c for c in checks if _is_failed_check(c)):
        entry = {"name": check.get("name"), "workflow": check.get("workflow"),
                 "link": check.get("link"), "failures": None}
        try:
            _, job = _check_run_and_job(check)
            lines = _failure_lines(gh.job_failed_log(job)) if job is not None else None
        except Exception:  # a missing log must never mask the failing check itself
            lines = None
        if lines is not None:
            kept = []
            for line in lines[:CI_SUMMARY_LINES_PER_JOB]:
                if len(line) > budget:
                    break
                budget -= len(line)
                kept.append(line)
            entry.update(failures=kept, truncated=len(kept) < len(lines),
                         infra_suspect=infra_suspect_text("\n".join(lines)))
        out.append(entry)
    return {"pr": pr_number, "failing": out,
            "passed": sum(c.get("bucket") == "pass" for c in checks),
            "pending": sum(c.get("bucket") == "pending" for c in checks)}


def _infra_hint(pr_number: int) -> str:
    return (f"A failed check's log matches a CI-infrastructure pattern (runner disk, docker "
            f"daemon, lost runner, OOM) -- infra_suspect, not a code failure. Fix the runner, "
            f"then `rerun-checks {pr_number}`.")


def cmd_pr_checks(gh: GitHub, pr_number: int) -> dict:
    checks = effective_checks(gh, pr_number)
    view = gh.pr_view(pr_number, "comments,headRefOid,baseRefName")
    files = gh.pr_files(pr_number)
    # An epic PR into its Initiative's branch owes no deferred suite, as in close-epic.
    deferred: tuple = ()
    if (_unit_of_branch(view.get("baseRefName")) or ("",))[0] == "initiative":
        head_ref = gh.pr_view(pr_number, "headRefName").get("headRefName")
        deferred = _pr_deferred_suites(gh, head_ref, view.get("baseRefName"))
    status, missing = merge_gate_status(
        files, checks,
        view.get("comments", []), view.get("headRefOid"), view.get("baseRefName"), deferred)
    infra = enrich_failed_checks(gh, checks)
    result = {"pr": pr_number, "status": status,
              "missing_required_workflows": missing, "checks": checks, "infra_suspect": infra}
    if deferred:
        result["deferred_to_initiative"] = list(deferred)
    hints = ([_MISSING_WORKFLOW_HINT] if missing else []) + ([_infra_hint(pr_number)] if infra else [])
    if hints:
        result["hint"] = " ".join(hints)
    return _with_uncovered(result, files, view.get("baseRefName"))


PR_BODY_CAP = 65_536  # GitHub's PR body limit


_CLOSES_RE = re.compile(r"^(?:Closes|Fixes|Resolves) #\d+\s*$", re.IGNORECASE | re.MULTILINE)


def cmd_update_pr_body(gh: GitHub, pr: int, body_file: str) -> dict:
    """Replace an open PR's description with `body_file`'s text (a rework round keeps the
    description current). A `Closes #<n>` line the old body had and the new one lacks is kept,
    so the merge still closes the issue. Refuses a closed/merged PR."""
    try:
        with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
            body = fh.read().strip()
    except OSError as exc:
        raise GhError(f"--body-file must be a readable file holding the PR description: {exc}")
    if not body:
        raise GhError("--body-file is empty -- a PR description must say something")
    view = gh.pr_view(pr, fields="state,body")
    if view.get("state") != "OPEN":
        raise GhError(f"PR #{pr} is {view.get('state')}, not open -- only an open PR's "
                      f"description is updated")
    kept = [m.group(0).strip() for m in _CLOSES_RE.finditer(view.get("body") or "")
            if m.group(0).strip().lower() not in body.lower()]
    if kept:
        body = body + "\n\n" + "\n".join(kept)
    if len(body) > PR_BODY_CAP:
        return {"pr": pr, "refused": True, "updated": False,
                "reason": f"the description is {len(body):,} chars, over GitHub's "
                          f"{PR_BODY_CAP:,}-char PR body limit; nothing was changed"}
    gh.pr_edit_body(pr, body)
    return {"pr": pr, "updated": True, "chars": len(body), "kept_closing_lines": kept}


def cmd_rerun_checks(gh: GitHub, pr_number: int) -> dict:
    """Re-run the failed/cancelled jobs of every Actions run behind the PR head's failed
    checks (`gh run rerun <run> --failed`, once per run). A rerun that errors is reported in
    `failed`, never raised; only failing to resolve the PR raises."""
    head = gh.pr_view(pr_number, "headRefOid").get("headRefOid")
    runs = []
    for check in gh.pr_checks(pr_number):
        run, _ = _check_run_and_job(check)
        if _is_failed_check(check) and run is not None and run not in runs:
            runs.append(run)
    rerun, failed = [], []
    for run in runs:
        try:
            gh.run_rerun_failed(run)
            rerun.append(run)
        except GhError as e:
            failed.append({"run": run, "error": str(e)})
    return {"pr": pr_number, "head": head, "rerun": rerun, "failed": failed,
            "nothing_to_rerun": not runs}


def cmd_merge_pr(gh: GitHub, pr_number: int, issue: int, repo_path: str = ".",
                 run_id: Optional[str] = None) -> dict:
    """Squash-merge the PR after its evidence and checks pass. Refuses (exit 0) when
    behind a base delta that needs re-attest; otherwise carries the attestation forward.
    Idempotent: an already-MERGED PR only gets the post-merge bookkeeping (`recovered`).
    Sets the terminal Stage/Pipeline Status fields on the closed issue.
    `run_id` books the terminal-unit count under that run, not whatever id last wrote the
    epic's run-state file (which may be a throwaway probe run's)."""
    entry = next((i for i in gh.issue_list() if i["number"] == issue), None)
    if entry is not None and current_stage(entry) in DESIGN_STAGE_REVIEW:
        parent = phase_task_parent(gh, issue)
        if parent is not None and parent["kind"] == "epic":
            # `_finalize_merged_pr` would close it before create-lld-tasks/merge-lld-doc ran.
            return {"pr": pr_number, "issue": issue, "merged": False,
                    "reason": f"#{issue} is a phase-Task: its PR is a design PR -- use "
                              f"`merge-design-pr {pr_number} --issue {issue}` (skip-gate/"
                              f"waive-gate/finish-lld run it), never merge-pr"}
    if gh.pr_view(pr_number, fields="state").get("state") == "MERGED":
        return _finalize_merged_pr(gh, pr_number, issue, repo_path, integration_base(gh, issue),
                                   gh.pr_files(pr_number), {"recovered": True}, run_id=run_id)
    base = integration_base(gh, issue)
    behind = gh.branch_behind_by(issue_branch(issue), base=base)
    carried_forward = {}
    if behind:
        base_delta = gh.base_delta_files(issue_branch(issue), base=base)
        if base_delta_needs_reattest(base_delta):
            return {"pr": pr_number, "issue": issue, "merged": False, "behind_base": behind,
                    "base": base,
                    "reason": f"branch {issue_branch(issue)} is {behind} commit(s) behind {base} -- CI ran "
                              f"on a stale base that changed suite-covered files; run sync-branch, "
                              f"wait for fresh CI green, then re-run merge-pr"}
        carried_forward = {"behind_base": behind, "base": base,
                            "base_delta_files": len(base_delta),
                            "carried_attestation_forward": True}
    missing = missing_pipeline_evidence(gh.issue_view(issue).get("comments", []))
    if missing:
        return {"pr": pr_number, "issue": issue, "merged": False,
                "missing_evidence": missing,
                "reason": f"issue #{issue} is missing pipeline evidence on GitHub: "
                          f"{'; '.join(missing)}. The stage ran only if the thread says so -- "
                          f"a stage whose evidence lives in one session's memory reads to a "
                          f"resumed session as a stage that never ran. Post the stage's own "
                          f"exit comment and re-run its exit action (handoff-to-pr-review / "
                          f"record-pr-review), then re-run merge-pr"}
    # Head first: the merge is pinned to it, so a push after this point can only make the
    # merge fail, never land a commit whose checks were not the ones read below.
    view = gh.pr_view(pr_number, "comments,headRefOid")
    head = view.get("headRefOid")
    checks = effective_checks(gh, pr_number)
    files = gh.pr_files(pr_number)
    # `base` (integration_base) is the PR's base branch -- no extra field fetch needed.
    status, missing = merge_gate_status(files, checks, view.get("comments", []), head, base)
    if status != "passed":
        detail = f" (no passing check or fresh local-ci attestation from: {', '.join(missing)})" if missing else ""
        hint = f" {_MISSING_WORKFLOW_HINT}" if status == "missing-checks" and missing else ""
        infra = enrich_failed_checks(gh, checks)
        failing = [c.get("name") or "?" for c in checks if _is_failed_check(c)]
        if failing:
            detail += f"; failing checks: {', '.join(failing)}"
        if infra:
            hint += f" {_infra_hint(pr_number)}"
        raise GhError(f"PR #{pr_number} checks not passed (status={status}){detail}{hint}")
    gh.pr_ready(pr_number)
    try:
        gh.pr_merge(pr_number, match_head=head)
    except GhError:
        # GitHub can answer 5xx after the squash already landed; only a PR still open failed.
        if gh.pr_view(pr_number, fields="state").get("state") != "MERGED":
            raise
    gh.pr_comment(pr_number, f"Auto-merged under the pipeline's scoped PR-merge override — "
                              f"see \"PRs merge automatically\" in the pipeline docs.")
    return _with_uncovered(_finalize_merged_pr(gh, pr_number, issue, repo_path, base, files,
                                               carried_forward, run_id=run_id), files, base)


def _finalize_merged_pr(gh: GitHub, pr_number: int, issue: int, repo_path: str,
                        base: str, files: list, extra: dict,
                        run_id: Optional[str] = None) -> dict:
    """Post-merge bookkeeping: close an epic-branch child `Closes #<n>` didn't, the
    `Merged via` note, run-cap accounting and `cleanup_unit` (worktrees, branches)."""
    view = gh.issue_view(issue)
    issue_closed = view["state"] == "CLOSED"
    if not issue_closed and base != "main":
        # `Closes #<n>` only fires on the default branch; a merged-but-open child
        # would be re-delegated and block the epic's close.
        gh.issue_close(issue)
        issue_closed = True
    realised = []
    if issue_closed:
        cmd_mark_issue_closed(gh, issue)
        note = f"Merged via #{pr_number}."
        # A recovered re-run of the merge finds its note already posted.
        if not any((c.get("body") or "").strip() == note for c in view.get("comments") or []):
            gh.issue_comment(issue, note)
        realised = _close_realised_issues(gh, issue, view.get("body"), pr=pr_number)
    terminal = record_terminal_unit(gh, issue, run_id=run_id)
    cleanup = cleanup_unit(gh, issue, "issue", repo_path, gh._run, base=base)
    return _with_warnings(
        {"pr": pr_number, "issue": issue, "merged": True, "issue_closed": issue_closed,
         "config_changed": touches_pipeline_config(files),
         **extra,
         **({"run_terminal": terminal} if terminal else {}),
         **({"realised_closed": realised} if realised else {}),
         "worktree": cleanup.pop("worktree"), "cleanup": cleanup}, cleanup)


def _close_realised_issues(gh: GitHub, issue: int, body: Optional[str],
                           pr: Optional[int] = None) -> list:
    """Close every still-open issue `issue`'s body marks as realised, naming the Task and
    PR that delivered it (`Closes #<n>` never fires on an `epic-<n>` merge). Returns
    `[{issue, closed}]`; never counts toward the run cap."""
    out = []
    for old in realised_issues(body):
        if gh.issue_view(old)["state"] != "OPEN":
            out.append({"issue": old, "closed": False, "reason": "already closed"})
            continue
        via = f" (PR #{pr})" if pr else ""
        gh.issue_comment(old, f"✅ Realised by Task #{issue}{via} — closed by the pipeline.\n\n"
                              f"<!-- realised-by: {issue} pr:{pr or 'none'} @ {_utc_now_marker()} -->")
        gh.issue_close(old)
        cmd_mark_issue_closed(gh, old)
        out.append({"issue": old, "closed": True})
    return out


def _release_parked_unit(issue: int, base_repo: str, runner: Runner) -> dict:
    """A parked unit's `docker` resources (both unit prefixes, before the tree they may mount;
    best effort, as `cleanup_unit`) and its `worktree`."""
    out: dict = {}
    if _docker_cleanup_enabled():
        sweeps = []
        for unit in ("issue", "epic"):
            prefix = unit_docker_prefix(issue, unit)
            try:
                sweeps.append(docker_sweep(prefix, runner))
            except Exception as exc:  # parking must never fail on cleanup
                sweeps.append({"prefix": prefix, "errors": [str(exc)]})
        out["docker"] = sweeps
    out["worktree"] = _release_unit_worktree(issue, base_repo=base_repo, runner=runner)
    return out


def _release_unit_worktree(issue: int, base_repo: str = ".",
                            runner: Runner = _default_runner) -> dict:
    """Release whichever of `issue-<n>` / `epic-<n>` currently has a worktree (kept in the
    cloud; a resume reuses it)."""
    if cloud_skips_local_cleanup():
        return {"released": False, "skipped": "cloud"}
    for branch in (issue_branch(issue), epic_branch(issue)):
        result = release_worktree(branch, runner=runner, base_repo=base_repo)
        if result.get("released") or result.get("reason") != "no worktree":
            return {**result, "branch": branch}
    return {"released": False, "reason": "no worktree"}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import issue_ops  # noqa: E402,F401
from .issue_ops import (  # noqa: E402
    _unit_of_branch, _with_warnings, cleanup_unit, cmd_mark_issue_closed,
)
