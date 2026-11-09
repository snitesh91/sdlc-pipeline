"""Dev PRs, citations and `verify-exit`."""
from __future__ import annotations

import os
import re
from typing import Optional

from .config import (
    PIPELINE_STATUS_FIELD_NAMES, Runner, STAGE_FIELD_NAMES, issue_branch, normalize_stage,
)
from .github import GhError, GitHub, _default_runner
from .units import label_names
from .evidence import (
    REVIEW_ROLES, epic_branch, integration_base, last_transition_to, remote_session,
)
from .footprint import (
    _dev_pr_scope_refusal_reason, acknowledge_footprint_deviations, dev_pr_scope_offenders,
    lld_task_headings_dropped,
)
from .plugin_version import worktree_path
from .worktrees import (
    BranchWorkspace, _registered_worktree, _run_retry_transient, _with_workspace, branch_lock,
    ensure_integration_base, resolve_repo_path,
)
from .design_gates import design_doc_dir, design_phase_epic


_NO_COMMITS_RE = re.compile(r"no commits between", re.IGNORECASE)


def _push_empty_commit(issue: int, repo_path: Optional[str], runner: Runner) -> dict:
    """Land one empty commit on `issue-<n>` (its live worktree, else an ephemeral one) and
    push it, so a verify-only Task can still raise a PR. Returns `sha`."""
    branch = issue_branch(issue)
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        runner(["git", "-C", ws.path, "commit", "--allow-empty", "-m",
                f"chore(#{issue}): verify-only task, no code change"])
        _run_retry_transient(["git", "-C", ws.path, "push", "origin", branch], runner)
        sha = runner(["git", "-C", ws.path, "rev-parse", "HEAD"]).strip()
    return _with_workspace({"branch": branch, "sha": sha}, ws)


PR_CREATE_NO_NUMBER = "the PR create returned no PR number"


_DEV_PR_OPENED_RE = re.compile(r"<!-- dev-pr-opened: #(\d+) -->")


def _dev_pr_comment_posted(gh: GitHub, issue: int, pr: int) -> bool:
    """Whether `open-dev-pr`'s comment for `pr` is on the issue (its marker, or the text an
    earlier version posted)."""
    for c in gh.issue_view(issue).get("comments", []):
        body = c.get("body") or ""
        if any(int(m) == pr for m in _DEV_PR_OPENED_RE.findall(body)) \
                or f"Draft PR: #{pr} —" in body:
            return True
    return False


def _finish_dev_pr(gh: GitHub, issue: int, pr: int, summary: str) -> None:
    """Development's exit on its draft PR: Stage `pr-review` and the PR-opened comment."""
    gh.set_stage_field(issue, "pr-review")
    gh.issue_comment(issue,
        f"✅ {summary} Draft PR: #{pr} — what was built and why is in the PR "
        f"description.\n\n"
        f"Not yet queued for review: `development` still owes `record-local-ci` per "
        f"suite it ran and `handoff-to-pr-review`, which posts the marker "
        f"`list-ready-for-review` reads.\n\n<!-- dev-pr-opened: #{pr} -->")


def _ensure_draft(gh: GitHub, pr: dict) -> dict:
    """`{}` when the PR is a draft, else the outcome of converting it (the review queue
    reads draft status): `converted_to_draft`, or a `draft_warning` when that failed."""
    if pr.get("isDraft"):
        return {}
    try:
        gh.pr_convert_to_draft(pr["number"])
        return {"converted_to_draft": True}
    except GhError as e:
        return {"draft_warning": f"PR #{pr['number']} is not a draft and converting it "
                                 f"failed ({e}); mark it draft by hand before review"}


def cmd_retarget_pr(gh: GitHub, issue: int, repo_path: str = ".",
                    runner: Runner = _default_runner) -> dict:
    """Point `issue-<n>`'s open PR at the unit's current integration base (migration: a
    standing child's PR opened into main before its Epic owned `epic-<n>`), standing that
    base up first. Then `prepare-rework <n>` syncs the branch with it. Returns `retargeted`."""
    prs = gh.pr_list_for_branch(issue_branch(issue))
    if not prs:
        return {"issue": issue, "retargeted": False,
                "reason": f"no open PR from {issue_branch(issue)}"}
    pr = prs[0]["number"]
    base = integration_base(gh, issue)
    old = gh.pr_view(pr, fields="baseRefName").get("baseRefName")
    if old == base:
        return {"issue": issue, "pr": pr, "base": base, "retargeted": False,
                "reason": "already targets its integration base"}
    ensure_integration_base(gh, repo_path, base, runner)
    gh.pr_edit_base(pr, base)
    gh.issue_comment(issue, f"↪️ Retargeted PR #{pr} from `{old}` to `{base}` (its integration "
                            f"base). Run `prepare-rework {issue}` before its next merge.")
    return {"issue": issue, "pr": pr, "from": old, "base": base, "retargeted": True,
            "next": f"prepare-rework {issue}"}


def cmd_open_dev_pr(gh: GitHub, issue: int, title: str, body: str, summary: str,
                    allow_empty: bool = False, repo_path: Optional[str] = None,
                    runner: Runner = _default_runner) -> dict:
    """Open the development draft PR and set Stage to `pr-review`; an already-open
    PR on the branch is reused (`created: False`) -- and, when this command's comment for it
    is missing (a PR made by another path, or a create whose response was lost), adopted:
    made a draft, Stage set, comment posted (`adopted: True`). A create that returns no PR
    number is recovered by looking the PR up by its head branch (`recovered`), else created
    plain and then drafted (`draft_fallback`, `draft_warning` when that fails). A branch with
    no commits ahead of its base gets an empty commit first only with `allow_empty`
    (`empty_commit`), else the refusal names the flag. Returns `pr`, `created`."""
    from .epic_close import CI_LOCAL_MARKER, _ci_local, _label_ci_local, _mark_ci_local
    branch = issue_branch(issue)
    existing = gh.pr_list_for_branch(branch)
    if existing:
        pr_number = existing[0]["number"]
        marked = {}
        # A PR opened by the laptop or another path: mark it now (base read only in the cloud).
        if remote_session() and _ci_local(integration_base(gh, issue)):
            _mark_ci_local(gh, pr_number)
            marked = {"ci_local": True}
        if _dev_pr_comment_posted(gh, issue, pr_number):
            return {"issue": issue, "pr": pr_number, "created": False, **marked,
                    "reason": f"PR #{pr_number} is already open on {branch} -- "
                              f"reusing it rather than opening a duplicate"}
        draft = _ensure_draft(gh, existing[0])
        _finish_dev_pr(gh, issue, pr_number, summary)
        return {"issue": issue, "pr": pr_number, "created": False, "adopted": True,
                "reason": f"PR #{pr_number} was already open on {branch} without this "
                          f"command's exit; adopted it (Stage pr-review, comment posted)",
                **draft, **marked}
    base = integration_base(gh, issue)
    # A cloud PR into a base with `attestable: "cloud"` suites (a standing Epic's Task PR into
    # main) runs its CI in session: the marker, on its own last line, skips the hosted jobs.
    ci_local = _ci_local(base)
    # Refuse a diff that authors a design doc or another Task's footprint file before opening
    # the PR (development authors no design doc; deviations belong in the PR description).
    try:
        changed = gh.files_since(base, branch)
    except GhError:
        changed = []
    offenders, acknowledged = acknowledge_footprint_deviations(
        dev_pr_scope_offenders(gh, issue, changed), body)
    reason = _dev_pr_scope_refusal_reason(offenders)
    if reason:
        return {"issue": issue, "created": False, "refused": True, "reason": reason,
                "offending_design_docs": offenders["design_docs"],
                "offending_footprint_paths": offenders["foreign_footprint"],
                "acknowledged_deviations": acknowledged}
    pr_body = f"{body}\n\nCloses #{issue}" + (f"\n\n{CI_LOCAL_MARKER}" if ci_local else "")
    extra: dict = {}

    def create() -> int:
        try:
            return gh.pr_create(base=base, head=branch, title=title, body=pr_body, draft=True)
        except GhError as e:
            if _NO_COMMITS_RE.search(str(e)):
                raise
            # The create may have landed with its response lost: adopt the PR it made.
            found = gh.pr_list_for_branch(branch)
            if not found and PR_CREATE_NO_NUMBER in str(e):
                # The cloud proxy has failed draft creates whole while plain ones pass:
                # create it plain, then draft it (the review queue reads draft status).
                n = gh.pr_create(base=base, head=branch, title=title, body=pr_body)
                extra["draft_fallback"] = f"draft create failed ({str(e)[:200]}); created plain"
                extra.update(_ensure_draft(gh, {"number": n, "isDraft": False}))
                return n
            if not found:
                raise
            extra["recovered"] = f"{str(e)[:300]} -- found PR #{found[0]['number']} on {branch}"
            extra.update(_ensure_draft(gh, found[0]))
            return found[0]["number"]

    try:
        pr_number = create()
    except GhError as e:
        if not _NO_COMMITS_RE.search(str(e)):
            raise
        if not allow_empty:
            raise GhError(f"{branch} has no commits ahead of {base}, so GitHub "
                          f"refuses the PR. A verify-only Task re-runs open-dev-pr with "
                          f"--allow-empty (lands an empty commit first); otherwise commit and "
                          f"push the work, then re-run.\n{e}")
        extra["empty_commit"] = _push_empty_commit(issue, repo_path, runner)
        pr_number = create()
    _finish_dev_pr(gh, issue, pr_number, summary)
    if ci_local:
        _label_ci_local(gh, pr_number)
        extra["ci_local"] = True
    return {"issue": issue, "pr": pr_number, "created": True,
            **({"acknowledged_deviations": acknowledged} if acknowledged else {}), **extra}


# --- citations: cite / verify-citations / verify-exit's citations_ok ---
# A `cite path=... [rev=...]` fence holds a byte-exact fragment of that file and is
# resolved; `cite-example` is illustrative and never resolved.

_CITE_FENCE_RE = re.compile(r'^(`{3,})(cite-example|cite)\s+(.*?)\s*$')


def _parse_cite_attrs(attr_str: str) -> dict:
    return dict(tok.split("=", 1) for tok in attr_str.split() if "=" in tok)


def parse_cite_blocks(text: str) -> list:
    """Every `cite`/`cite-example` block in `text`, as `{kind, path, rev, body, line_no}`
    in order (`line_no`: first body line, a hint only). A fence closes on a backtick
    run at least as long as the opening one."""
    lines = text.splitlines()
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        m = _CITE_FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, kind, attrs_str = m.groups()
        attrs = _parse_cite_attrs(attrs_str)
        close_re = re.compile(r'^`{' + str(len(fence)) + r',}\s*$')
        body_lines = []
        j = i + 1
        while j < n and not close_re.match(lines[j]):
            body_lines.append(lines[j])
            j += 1
        blocks.append({
            "kind": kind,
            "path": attrs.get("path"),
            "rev": attrs.get("rev"),
            "body": "\n".join(body_lines),
            "line_no": i + 2,
        })
        i = j + 1
    return blocks


def resolve_citation(path: str, body: str, rev: Optional[str] = None,
                      repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """Fixed-string match of `body` in the working-tree file (or `git show rev:path`).
    Returns `resolved`, `line_hint` (+ `match_count`/`line_hints`) or `cited_vs_found`;
    never raises."""
    result = {"path": path, "rev": rev}
    if not path:
        result["resolved"] = False
        result["cited_vs_found"] = "cite block has no path= attribute"
        return result
    try:
        if rev:
            content = runner(["git", "-C", repo_path, "show", f"{rev}:{path}"])
        else:
            with open(os.path.join(repo_path, path), "r") as f:
                content = f.read()
    except (GhError, OSError, ValueError) as e:
        result["resolved"] = False
        result["cited_vs_found"] = (f"could not read {path}"
                                     f"{f' at rev {rev}' if rev else ''}: {e}")
        return result
    if not body.strip():
        result["resolved"] = False
        result["cited_vs_found"] = "empty citation body"
        return result
    count = content.count(body)
    result["resolved"] = count >= 1
    if result["resolved"]:
        line_hints = []
        start = 0
        for _ in range(count):
            idx = content.find(body, start)
            line_hints.append(content.count("\n", 0, idx) + 1)
            start = idx + 1
        result["line_hint"] = line_hints[0]
        if count > 1:
            result["match_count"] = count
            result["line_hints"] = line_hints
    else:
        result["cited_vs_found"] = (f"cited fragment not found in {path}"
                                     f"{f' at rev {rev}' if rev else ''}")
    return result


def verify_citations_text(text: str, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """Resolve every `cite` block in `text` (skipping `cite-example`). Returns
    `citations`, `examples_skipped`, `all_resolved`."""
    blocks = parse_cite_blocks(text)
    citations = []
    examples_skipped = 0
    for b in blocks:
        if b["kind"] == "cite-example":
            examples_skipped += 1
            continue
        citations.append(resolve_citation(b["path"], b["body"], rev=b["rev"],
                                           repo_path=repo_path, runner=runner))
    return {"citations": citations, "examples_skipped": examples_skipped,
            "all_resolved": all(c["resolved"] for c in citations)}


def cmd_cite(path: str, line: Optional[int] = None, lines: Optional[str] = None,
             match: Optional[str] = None, rev: Optional[str] = None,
             repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """Build a `cite` block from the real file by `line`, inclusive `lines` range, or
    the single line containing `match`. Returns `block`, `path`, `line_range`; on any
    out-of-range or ambiguous selection `ok: False` and no block."""
    try:
        if rev:
            content = runner(["git", "-C", repo_path, "show", f"{rev}:{path}"])
        else:
            with open(os.path.join(repo_path, path), "r") as f:
                content = f.read()
    except (GhError, OSError, ValueError) as e:
        return {"ok": False,
                "reason": f"could not read {path}{f' at rev {rev}' if rev else ''}: {e}"}
    file_lines = content.splitlines()
    if line is not None:
        start = end = line
    elif lines is not None:
        try:
            a, b = lines.split("-", 1)
            start, end = int(a), int(b)
        except ValueError:
            return {"ok": False, "reason": f"--lines {lines!r} is not a <start>-<end> range"}
    elif match is not None:
        matches = [i + 1 for i, l in enumerate(file_lines) if match in l]
        if len(matches) == 0:
            return {"ok": False, "reason": f"no line in {path} contains {match!r}"}
        if len(matches) > 1:
            return {"ok": False,
                     "reason": f"{match!r} matches {len(matches)} lines in {path} "
                               f"({matches}) -- ambiguous, narrow the match"}
        start = end = matches[0]
    else:
        return {"ok": False, "reason": "one of --line, --lines, or --match is required"}
    if start < 1 or end < start or end > len(file_lines):
        return {"ok": False,
                 "reason": f"line range {start}-{end} is out of range for {path} "
                           f"({len(file_lines)} lines)"}
    body = "\n".join(file_lines[start - 1:end])
    if not body.strip():
        return {"ok": False,
                "reason": f"lines {start}-{end} of {path} are blank -- a blank citation "
                          f"never resolves; select a line with content"}
    # Fence longer than any backtick run in the body so it can't close early.
    fence_len = 3
    for m in re.finditer(r'`+', body):
        fence_len = max(fence_len, len(m.group()) + 1)
    fence = "`" * fence_len
    info = f"cite path={path}" + (f" rev={rev}" if rev else "")
    result = {"path": path, "line_range": [start, end],
              "block": f"{fence}{info}\n{body}\n{fence}"}
    if rev:
        result["rev"] = rev
    return result


def cmd_verify_citations(paths: list, repo_path: str = ".",
                          runner: Runner = _default_runner) -> dict:
    """Resolve every citation in `paths`. One document returns flat; several nest
    under `documents` with an aggregate `all_resolved`. `ok` mirrors `all_resolved`;
    an unreadable document is unresolved with a `reason`, never an exception."""
    documents = []
    for p in paths:
        try:
            with open(os.path.join(repo_path, p), "r") as f:
                text = f.read()
        except (OSError, ValueError) as e:
            documents.append({"document": p, "citations": [], "examples_skipped": 0,
                              "all_resolved": False, "reason": f"could not read {p}: {e}"})
            continue
        doc_result = verify_citations_text(text, repo_path=repo_path, runner=runner)
        doc_result = {"document": p, **doc_result}
        documents.append(doc_result)
    all_ok = all(d["all_resolved"] for d in documents)
    result = dict(documents[0]) if len(documents) == 1 else {"documents": documents,
                                                              "all_resolved": all_ok}
    result["ok"] = all_ok
    return result


# The one doc each stage authors; `citations_ok` checks only that doc, so an older
# doc's rotted citation never fails a later stage. Stages absent here write no doc.
STAGE_RECORD_FILENAMES = {
    "product": "product.md",
    "architecture": "architecture.md",
    "lld": "lld.md",
}


# verify-exit checks the unit's dev worktree is on its branch and pushed for these stages (the
# authoring stages, and development's exit, verified as `pr-review`). Tests' fake runners
# script no worktree, so conftest turns it off; its own tests turn it back on.
VERIFY_EXIT_HEAD_CHECK = True


HEAD_CHECKED_STAGES = ("product", "architecture", "lld", "development", "pr-review")


def unit_head_problem(issue: int, repo_path: str,
                      runner: Runner = _default_runner) -> Optional[dict]:
    """Why the unit's dev worktree does not hold its pushed work, or None: a detached HEAD or
    another branch there (commits made there are on no `issue-<n>`, so `git push origin
    issue-<n>` pushes nothing), or a local HEAD that differs from `origin/issue-<n>`. None
    when no dev worktree is registered or origin cannot be fetched (`skipped`)."""
    branch, path = issue_branch(issue), worktree_path("issue", issue)
    try:
        entry = _registered_worktree(repo_path, path, runner)
    except GhError as exc:
        return {"ok": True, "skipped": f"worktree lookup failed: {exc}"}
    if entry is None:
        return None
    if entry["branch"] != branch:
        held = "a detached HEAD" if entry["branch"] is None else f"branch {entry['branch']}"
        return {"ok": False, "path": path, "detached": entry["branch"] is None,
                "reason": f"{path} is on {held}, not {branch}: commits made there are on no "
                          f"{branch}, and `git push origin {branch}` pushes nothing, so the "
                          f"work is not on origin. Check out {branch} in {path} (bring any "
                          f"commits made while detached onto it), push, then re-run. Reviewers "
                          f"never check out in a unit's dev worktree: they use "
                          f"review-worktree-add."}
    remote: Optional[str] = None
    try:
        _run_retry_transient(["git", "-C", path, "fetch", "origin", branch], runner)
        remote = runner(["git", "-C", path, "rev-parse", f"origin/{branch}"]).strip()
    except GhError as exc:
        if "couldn't find remote ref" not in str(exc).lower():
            return {"ok": True, "skipped": f"could not fetch origin/{branch}: {exc}"}
    local = runner(["git", "-C", path, "rev-parse", "HEAD"]).strip()
    if local == remote:
        return None
    return {"ok": False, "path": path, "local_head": local, "origin_head": remote,
            "reason": f"{path}'s HEAD {local[:10]} is not origin/{branch} "
                      f"({remote[:10] if remote else 'absent'}): the stage's work is not pushed "
                      f"(or origin moved under it). Push {branch} (`git -C {path} push origin "
                      f"{branch}`), then re-run."}


def cmd_verify_exit(gh: GitHub, repo_path: Optional[str], issue: int, expect_stage: str,
                     pr: Optional[int] = None, runner: Runner = _default_runner) -> dict:
    """Post-handoff check: Stage field matches `expect_stage`, the stage's docs and
    citations are present, recent commits, and (with `pr`) the PR's draft/branches.
    Sets `ok: False` (exit 1) with `reason`/`misuse` when the handoff is unverified."""
    repo_path = resolve_repo_path(repo_path, issue_branch(issue), runner=runner)
    issue_data = gh.issue_view(issue)
    labels = label_names(issue_data)
    fields = gh.issue_fields(issue)
    actual_stage = STAGE_FIELD_NAMES.get(fields.get("Stage"))
    actual_status = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    # Any spelling ("PR Review", "pr review") compares as its slug; an unknown one stays raw.
    expect = normalize_stage(expect_stage) or expect_stage
    result = {"issue": issue, "labels": sorted(labels), "stage": actual_stage,
              "pipeline_status": actual_status, "expected_stage_present": actual_stage == expect}
    # Review roles inherit the preceding stage's value, so asking for one is misuse --
    # except `pr-review`, which is a real Stage value (development's exit writes it).
    if expect in REVIEW_ROLES and expect not in STAGE_FIELD_NAMES.values():
        result["ok"] = False
        result["misuse"] = (f"`{expect}` is a review role and has no Stage field value of "
                            f"its own -- it inherits the preceding stage's. Verify a review by "
                            f"its marker (`record-pr-review` / the arch-review confidence "
                            f"marker), not by --expect-stage.")
    elif not result["expected_stage_present"]:
        result["ok"] = False
        accepted = "" if normalize_stage(expect_stage) else (
            f" (unrecognised --expect-stage; accepted: "
            f"{', '.join(sorted(set(STAGE_FIELD_NAMES.values())))} or their field names)")
        result["reason"] = (f"Stage is {actual_stage!r}, expected {expect!r} -- the "
                            f"previous stage's exit action did not run, or ran against a "
                            f"different issue. Do not dispatch the next stage until this is "
                            f"resolved; advancing on an unverified handoff is how a stage's "
                            f"evidence ends up existing only in one session's memory." + accepted)
    if pr is not None:
        pr_data = gh.pr_view(pr, fields="isDraft,headRefName,baseRefName")
        result["pr"] = pr
        result["pr_is_draft"] = pr_data.get("isDraft")
        result["pr_head"] = pr_data.get("headRefName")
        result["pr_base"] = pr_data.get("baseRefName")
        # `open-dev-pr` moves the Stage field but only `handoff-to-pr-review` posts the
        # marker; check it here. Reported, never posted: this must not forge its own evidence.
        if expect == "pr-review":
            problems = []
            marker_present = last_transition_to(
                issue_data.get("comments", []), "pr-review") is not None
            result["handoff_marker_present"] = marker_present
            if not marker_present:
                result["ok"] = False
                problems.append(
                    f"no `development->pr-review` handoff marker on #{issue} -- "
                    f"`development` skipped its exit action. Run "
                    f"`handoff-to-pr-review {issue} --pr {pr} --summary ...`. Until it "
                    f"is posted, `list-ready-for-review` never queues this PR and "
                    f"`merge-pr` refuses it as missing_pipeline_evidence.")
            # A development PR must author no design doc and touch no other Task's footprint.
            try:
                offenders = dev_pr_scope_offenders(gh, issue, gh.pr_files(pr),
                                                   repo_path=repo_path, runner=runner)
            except GhError:
                offenders = {"design_docs": [], "foreign_footprint": []}
            acknowledged: list = []
            if offenders["foreign_footprint"]:
                # A foreign-footprint touch the PR body lists under `## Footprint deviations`
                # is accepted; a design doc never is.
                offenders, acknowledged = acknowledge_footprint_deviations(
                    offenders, gh.pr_view(pr, fields="body").get("body"))
            if acknowledged:
                result["acknowledged_deviations"] = acknowledged
            result["dev_pr_scope"] = offenders
            scope_reason = _dev_pr_scope_refusal_reason(offenders)
            if scope_reason:
                result["ok"] = False
                problems.append(scope_reason)
            result["problems"] = problems
    # A non-standing Epic's phase-Task authors `epic-<e>/<doc>` in place (docs merge into the
    # epic branch by its design PR); everything else keeps `issue-<n>/`.
    epic = design_phase_epic(gh, issue, expect)
    docs_rel = design_doc_dir(epic, issue, gh)
    docs_dir = os.path.join(repo_path, docs_rel)
    result["docs_dir"] = docs_rel
    result["docs_present"] = sorted(os.listdir(docs_dir)) if os.path.isdir(docs_dir) else []
    # Only the completing stage's own record is re-checked, so an older doc's rotted
    # citation can't fail a later stage's exit.
    record_filename = STAGE_RECORD_FILENAMES.get(expect)
    if record_filename:
        record_path = os.path.join(docs_dir, record_filename)
        if epic is not None and not os.path.isfile(record_path):
            result["ok"] = False
            result.setdefault(
                "reason",
                f"{docs_rel}/{record_filename} is missing on {issue_branch(issue)} -- a "
                f"non-standing Epic's phase-Task authors the doc at exactly that path "
                f"(not `issue-{issue}/`); write it there, commit and push.")
        if os.path.isfile(record_path):
            with open(record_path, "r") as f:
                record_text = f.read()
            citation_result = verify_citations_text(record_text, repo_path=repo_path,
                                                      runner=runner)
            result["citations"] = citation_result["citations"]
            result["citations_ok"] = citation_result["all_resolved"]
            if not citation_result["all_resolved"]:
                result["ok"] = False
                result.setdefault(
                    "reason",
                    f"{record_filename} has an unresolved citation -- see "
                    f"result['citations'] for which one and what was cited vs. found.")
    if epic is not None and expect == "architecture":
        # A revision Task may also update `lld.md` Task sections; it must keep every heading.
        lld_path = os.path.join(docs_dir, "lld.md")
        if os.path.isfile(lld_path):
            try:
                base_lld = runner(["git", "-C", repo_path, "show",
                                   f"origin/{epic_branch(epic)}:{docs_rel}/lld.md"])
            except GhError:
                base_lld = None
            if base_lld is not None:
                with open(lld_path, "r") as f:
                    dropped = lld_task_headings_dropped(base_lld, f.read())
                if dropped:
                    result["ok"] = False
                    result["lld_headings_dropped"] = dropped
                    result.setdefault(
                        "reason",
                        f"{docs_rel}/lld.md drops Task heading(s) {', '.join(dropped)} that "
                        f"origin/{epic_branch(epic)} carries -- a revision edits a Task's "
                        f"section body only; keep every `## Task` heading as it is.")
    if VERIFY_EXIT_HEAD_CHECK and expect in HEAD_CHECKED_STAGES:
        head = unit_head_problem(issue, repo_path, runner)
        if head is not None:
            result["head_check"] = head
            if head.get("ok") is False:
                result["ok"] = False
                result.setdefault("reason", head["reason"])
    log = runner(["git", "-C", repo_path, "log", "--oneline", "-5"]).strip()
    result["recent_commits"] = log.splitlines() if log else []
    return result


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import checks  # noqa: E402,F401
