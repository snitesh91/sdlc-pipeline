"""Design PRs, gate PRs, branch sync and the LLD doc merge."""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from .config import LABELS, Runner, STAGE_FIELD_NAMES, issue_branch
from .github import GhError, GitHub, WorkItemProvider, _default_runner, _utc_now_marker
from .units import (
    classify_unit_from_issue, current_stage, doc_root_for, has_label, is_epic_standing,
)
from .evidence import (
    PHASE_TASK_STAGES, _ARCH_CONFIDENCE_MARKER, _DESIGN_PR_BODY_MARKER,
    _DESIGN_PR_OPENED_MARKER, _DESIGN_REVIEW_OUTCOME_MARKER, epic_branch, integration_base,
    unit_branch,
)
from .footprint import _TASK_KEY_MARKER, find_task_footprint, is_numbered_copy, parse_task_headings
from .worktrees import (
    BranchWorkspace, _fetch_origin, _run_retry_transient, _with_workspace, branch_lock,
    origin_branch_exists,
)
from .handoff import _stage_and_status


def git_rev_parse_head(repo_path: str, runner: Runner = _default_runner) -> str:
    return runner(["git", "-C", repo_path, "rev-parse", "HEAD"]).strip()


_GATE_PR_BODY = (
    'Doc-only review gate for #{issue} — see "Human-review gates" in the pipeline '
    'docs. Merging this PR is the approval to proceed to '
    '{next_stage}. Do NOT use Closes/Fixes here — the tracking issue stays open until the '
    'final code PR merges.'
)


def phase_gate_title(gh: WorkItemProvider, issue: int, title: str) -> str:
    """`title` with the parent's title appended when `issue` is a phase-Task (an Initiative's
    Product-Roadmap Task, or a non-standing Epic's architecture/lld/revision Task), whose fixed
    titles would otherwise make every gate PR read alike."""
    parent = (gh.issue_epic_info(issue).get("parent") or {}).get("number")
    if parent is None:
        return title
    parent_info = gh.issue_epic_info(parent)
    kind = classify_unit_from_issue(parent_info)
    if kind == "epic":
        stage = STAGE_FIELD_NAMES.get(gh.issue_fields(issue).get("Stage"))
        if is_epic_standing(parent_info) or stage not in PHASE_TASK_STAGES:
            return title
    elif kind != "initiative":
        return title
    base = re.sub(r"\s+phase$", "", title, flags=re.IGNORECASE)
    parent_title = gh.issue_view(parent)["title"]
    return base if parent_title.lower() in base.lower() else f"{base} - {parent_title}"


# Decoration `open-gate` adds itself; a caller that includes it would see it twice.
_GATE_TITLE_PREFIX = re.compile(r"^\s*gate(?:\s+[ab])?\s*(?::|\s[\-\u2013\u2014])\s*",
                                re.IGNORECASE)


_GATE_TITLE_SUFFIX = r"\s*(?:[\-\u2013\u2014]\s*[\w.-]+\.md\s+for\s+review)?\s*(?:\(#{issue}\))?\s*$"


def gate_pr_title(gh: WorkItemProvider, issue: int, title: Optional[str] = None) -> tuple:
    """`(title, normalized)`: the gate PR's base title -- `title` stripped of a gate-shorthand
    prefix and a `- <doc> for review (#n)` suffix, else the issue's own -- through
    `phase_gate_title`; `normalized` says a caller-supplied title had to be stripped."""
    raw = title if title and title.strip() else gh.issue_view(issue)["title"]
    suffix = re.compile(_GATE_TITLE_SUFFIX.format(issue=issue), re.IGNORECASE)
    base = suffix.sub("", _GATE_TITLE_PREFIX.sub("", raw)).strip() or raw.strip()
    return phase_gate_title(gh, issue, base), bool(title) and base != title.strip()


# The design stages a non-standing Epic's phase-Task authors, and the review that follows each.
DESIGN_STAGE_REVIEW = {"architecture": "arch-review", "lld": "lld-review"}


def find_design_pr(comments: list) -> Optional[tuple]:
    """`(stage, pr_number)` of the latest `design-pr-opened` marker in `comments`, or None."""
    found = None
    for c in comments:
        for m in _DESIGN_PR_OPENED_MARKER.finditer(c.get("body", "")):
            found = (m.group(1), int(m.group(2)))
    return found


def design_phase_epic(gh: WorkItemProvider, issue: int, stage: Optional[str]) -> Optional[int]:
    """The Epic number when `issue` is a non-standing Epic's phase-Task authoring `stage`
    (`architecture` | `lld`), else None: standing, parentless and Initiative issues keep
    their own `issue-<n>` docs and `main` gate."""
    if stage not in DESIGN_STAGE_REVIEW:
        return None
    parent = phase_task_parent(gh, issue)
    return parent["number"] if parent and parent["kind"] == "epic" else None


def design_doc_dir(epic: Optional[int], issue: int,
                   gh: Optional[WorkItemProvider] = None) -> str:
    """Where `issue`'s design docs live: `<docRoot>/epic-<e>` for a non-standing Epic's
    phase-Task (authored in place, merged into `epic-<e>`), else `<docRoot>/issue-<n>`;
    `<docRoot>` is the unit's own (`doc_root_for`)."""
    if epic is not None:
        return f"{doc_root_for(epic, gh)}/epic-{epic}"
    return f"{doc_root_for(issue, gh)}/issue-{issue}"


def last_design_review_outcome(comments: list, role: str) -> Optional[tuple]:
    """`(comment_index, outcome)` of the latest `design-review-outcome` for `role`, or None."""
    found = None
    for idx, c in enumerate(comments):
        for m in _DESIGN_REVIEW_OUTCOME_MARKER.finditer(c.get("body", "")):
            if m.group(2) == role:
                found = (idx, m.group(1))
    return found


def last_design_review_sha(comments: list, role: str) -> Optional[str]:
    """The PR head SHA the latest `role` outcome marker names, or None."""
    sha = None
    for c in comments:
        for m in _DESIGN_REVIEW_OUTCOME_MARKER.finditer(c.get("body", "")):
            if m.group(2) == role:
                sha = m.group(4)
    return sha


def missing_design_review_evidence(comments: list, role: str) -> list:
    """Problems blocking a design PR's merge: no recorded `role` outcome, or one that is not
    `clean`. Empty = the review is on record as clean."""
    outcome = last_design_review_outcome(comments, role)
    if outcome is None:
        return [f"no `{role}` outcome recorded (run `record-design-review <issue> --role "
                f"{role} --outcome clean|rework`)"]
    if outcome[1] != "clean":
        return [f"the latest recorded `{role}` outcome is `{outcome[1]}`, not `clean` -- "
                f"address the findings, re-run the review and record a clean outcome"]
    return []


def latest_arch_confidence(comments: list) -> Optional[int]:
    """The confidence in the newest `arch-review-confidence` marker written since the
    previous `arch-review` outcome (i.e. this round's), or None."""
    outcomes = [idx for idx, c in enumerate(comments)
                if any(m.group(2) == "arch-review"
                       for m in _DESIGN_REVIEW_OUTCOME_MARKER.finditer(c.get("body", "")))]
    floor = outcomes[-2] if len(outcomes) > 1 else -1
    found = None
    for idx, c in enumerate(comments):
        for m in _ARCH_CONFIDENCE_MARKER.finditer(c.get("body", "")):
            if idx > floor:
                found = int(m.group(1))
    return found


def _arch_needs_human(gh: WorkItemProvider, issue: int, comments: list) -> list:
    """Why an architecture design PR may NOT be merged by the pipeline alone (empty = it
    may): the profile requires a human at Gate B and the latest confidence does not clear the
    skip threshold."""
    gates = _profile_for_issue(gh, issue)["gates"]
    if not gates["requiresHumanGateB"]:
        return []
    confidence = latest_arch_confidence(comments)
    if confidence is not None and confidence > gates["skipConfidenceThreshold"]:
        return []
    return [f"the profile requires a human at Gate B and arch-review confidence "
            f"({confidence if confidence is not None else 'marker missing'}) does not exceed "
            f"{gates['skipConfidenceThreshold']} -- open-gate and leave the PR for the human "
            f"to merge into its epic branch"]


_DESIGN_PR_BODY = (
    "Design PR for #{issue}: `{doc}` of Epic #{epic}, reviewed on this PR by `{review}`. "
    "It merges into `{base}`, never `main`. The pipeline merges it on a clean review "
    "({who}). Do NOT use Closes/Fixes here: the phase-Task closes only after this merges."
    "\n\n<!-- design-pr: {stage}:{issue} -->"
)


def cmd_open_design_pr(gh: GitHub, issue: int, runner: Runner = _default_runner) -> dict:
    """Raise the design PR `issue-<n>` -> `epic-<e>` for a non-standing Epic's architecture
    or lld phase-Task, once the stage has finished and its branch is pushed. Idempotent: an
    open one is reused (`created: False`). Standing, parentless and Initiative issues are
    refused (exit 0): their gate PR is `open-gate`'s. Returns `pr`, `created`, `base`."""
    stage, _status = _stage_and_status(gh, issue)
    epic = design_phase_epic(gh, issue, stage)
    if epic is None:
        return {"issue": issue, "refused": True,
                "reason": f"#{issue} (Stage {stage!r}) is not a non-standing Epic's "
                          f"architecture/lld phase-Task -- it has no design PR"}
    base, doc = epic_branch(epic), f"{doc_root_for(epic, gh)}/epic-{epic}/{stage}.md"
    info = gh.issue_view(issue)
    found = find_design_pr(info.get("comments", []))
    if found and found[0] == stage:
        state = gh.pr_view(found[1], fields="state").get("state")
        if state in ("OPEN", "MERGED"):
            return {"issue": issue, "pr": found[1], "created": False, "base": base,
                    "stage": stage, "doc": doc, "pr_state": state,
                    "reason": f"design PR #{found[1]} is already {state}"}
    who = ("a human merges it at Gate B unless arch-review clears the confidence bar"
           if stage == "architecture" else "no human gate")
    pr_number = gh.pr_create(
        base=base, head=issue_branch(issue),
        title=f"{phase_gate_title(gh, issue, info['title'])} — {stage}.md for review (#{issue})",
        body=_DESIGN_PR_BODY.format(issue=issue, doc=doc, epic=epic, review=DESIGN_STAGE_REVIEW[stage],
                                    base=base, who=who, stage=stage),
        draft=False)
    gh.issue_comment(issue,
        f"📄 Design PR #{pr_number} opened: `{issue_branch(issue)}` -> `{base}` for `{doc}`. "
        f"`{DESIGN_STAGE_REVIEW[stage]}` reviews it there.\n\n"
        f"<!-- design-pr-opened: {stage}:{pr_number} @ {_utc_now_marker()} -->")
    return {"issue": issue, "pr": pr_number, "created": True, "base": base, "stage": stage,
            "doc": doc}


def _epic_lld_is_numbered_copy(repo_path: str, issue: int, epic: int, runner: Runner,
                               gh: Optional[WorkItemProvider] = None) -> bool:
    """Whether `origin/epic-<e>`'s lld.md is `issue-<n>`'s after `create-lld-tasks` numbered
    it: merging the branch's copy back would revert the numbering."""
    doc = f"{doc_root_for(epic, gh)}/epic-{epic}/lld.md"
    try:
        _fetch_origin(repo_path, runner)
        src = runner(["git", "-C", repo_path, "show", f"origin/{issue_branch(issue)}:{doc}"])
        dest = runner(["git", "-C", repo_path, "show", f"origin/{epic_branch(epic)}:{doc}"])
    except GhError:
        return False
    return is_numbered_copy(src, dest)


def _design_review_stale(gh: GitHub, issue: int, stage: str, epic: int, comments: list,
                         head: Optional[str]) -> Optional[dict]:
    """A refusal when the recorded clean review does not cover the design PR's head: it names
    another SHA and the doc changed since (a base-only sync merge leaves the doc alone)."""
    role = DESIGN_STAGE_REVIEW[stage]
    reviewed = last_design_review_sha(comments, role)
    rerun = (f"re-run the `{role}` review on the current head and `record-design-review "
             f"{issue} --role {role} --outcome clean`, then merge again")
    if reviewed is None:
        return {"review_stale": True, "head_sha": head,
                "reason": f"the recorded `{role}` outcome names no reviewed head SHA -- {rerun}"}
    if head is None or head.startswith(reviewed) or reviewed.startswith(head):
        return None
    doc = f"{doc_root_for(epic, gh)}/epic-{epic}/{stage}.md"
    try:
        changed = gh.files_since(reviewed, issue_branch(issue))
    except GhError:
        changed = None  # the reviewed commit is gone from the branch
    if changed is not None and len(changed) < 300 and doc not in changed:
        return None
    return {"review_stale": True, "reviewed_sha": reviewed, "head_sha": head,
            "reason": f"the design PR head moved since `{role}` reviewed it "
                      f"({reviewed[:10]} -> {head[:10]}) and `{doc}` changed -- {rerun}"}


def cmd_merge_design_pr(gh: GitHub, pr_number: int, issue: int, repo_path: str = ".",
                        runner: Runner = _default_runner, gate_cleared: bool = False) -> dict:
    """Squash-merge a phase-Task's design PR into its epic branch once its review is on record
    as clean; never closes the Task and never deletes its branch. `lld-review` clean always
    merges; `arch-review` only above the skip threshold or where Gate B is waived
    (`gate_cleared` = skip-gate/waive-gate already decided that). Refuses (exit 0) when behind
    the epic branch (`sync-branch` first), on missing/`rework` evidence, when the PR head moved
    since the review (`review_stale`), or when the epic's numbered lld.md would be reverted.
    An already-MERGED PR returns `already_merged`."""
    result = {"pr": pr_number, "issue": issue, "merged": False}
    parent = phase_task_parent(gh, issue)
    if parent is None or parent["kind"] != "epic":
        return {**result, "reason": f"#{issue} is not a non-standing Epic's phase-Task -- "
                                     f"only its design PR is merged by merge-design-pr"}
    base = epic_branch(parent["number"])
    pr = gh.pr_view(pr_number, fields="state,mergedAt,headRefName,baseRefName,body,headRefOid")
    marker = _DESIGN_PR_BODY_MARKER.search(pr.get("body") or "")
    if (not marker or int(marker.group(2)) != issue or pr.get("headRefName") != issue_branch(issue)
            or pr.get("baseRefName") != base):
        return {**result, "reason": f"PR #{pr_number} is not #{issue}'s design PR "
                                     f"(`{issue_branch(issue)}` -> `{base}` with a design-pr marker)"}
    stage = marker.group(1)
    result.update({"stage": stage, "base": base})
    if pr.get("state") == "MERGED":
        return {**result, "already_merged": True}
    if pr.get("state") != "OPEN":
        return {**result, "reason": f"PR #{pr_number} was closed without merging -- a human "
                                     f"decision; mark-needs-human"}
    comments = gh.issue_view(issue).get("comments", [])
    missing = missing_design_review_evidence(comments, DESIGN_STAGE_REVIEW[stage])
    if not missing and stage == "architecture" and not gate_cleared:
        missing = _arch_needs_human(gh, issue, comments)
    if missing:
        return {**result, "missing_evidence": missing,
                "reason": f"#{issue} cannot merge its design PR: {'; '.join(missing)}"}
    stale = _design_review_stale(gh, issue, stage, parent["number"], comments, pr.get("headRefOid"))
    if stale:
        return {**result, **stale}
    behind = gh.branch_behind_by(issue_branch(issue), base=base)
    if behind:
        return {**result, "behind_base": behind,
                "reason": f"branch {issue_branch(issue)} is {behind} commit(s) behind {base} -- "
                          f"run sync-branch {issue}, then re-run"}
    if stage == "lld" and _epic_lld_is_numbered_copy(repo_path, issue, parent["number"], runner, gh):
        return {**result, "up_to_date": True,
                "reason": f"`{base}` already carries this lld.md with its Tasks numbered -- "
                          f"merging would revert the numbering, so nothing is merged"}
    try:
        gh.pr_merge(pr_number, delete_branch=False)
    except GhError:
        # GitHub can answer 5xx after the squash already landed; only a PR still open failed.
        if gh.pr_view(pr_number, fields="state").get("state") != "MERGED":
            raise
    gh.pr_comment(pr_number, f"Auto-merged into `{base}` by the pipeline: "
                              f"`{DESIGN_STAGE_REVIEW[stage]}` is recorded clean.")
    gh.issue_comment(issue,
        f"✅ Design PR #{pr_number} merged into `{base}` (`{DESIGN_STAGE_REVIEW[stage]}` clean); "
        f"its branch `{issue_branch(issue)}` is kept until the Task closes.\n\n"
        f"<!-- design-pr-merged: {stage}:{pr_number} @ {_utc_now_marker()} -->")
    return {**result, "merged": True}


def _merge_design_pr_of(gh: GitHub, issue: int, repo_path: Optional[str], runner: Runner,
                        gate_cleared: bool = False) -> dict:
    """`cmd_merge_design_pr` for the design PR recorded on `issue`'s thread; a structured
    refusal when none was opened."""
    found = find_design_pr(gh.issue_view(issue).get("comments", []))
    if not found:
        return {"issue": issue, "merged": False,
                "reason": f"#{issue} has no design PR marker -- run `transition` (or "
                          f"`open-design-pr {issue}`) after its stage finishes"}
    return cmd_merge_design_pr(gh, found[1], issue, repo_path or ".", runner=runner,
                               gate_cleared=gate_cleared)


def cmd_open_gate(gh: GitHub, repo_path: Optional[str], issue: int, title: str, doc: str,
                   next_stage: str, summary: str, runner: Runner = _default_runner) -> dict:
    """Open a human-review gate: a new `issue-<n>` PR into its integration base, or -- for a
    non-standing Epic's architecture phase-Task -- its already-open design PR into `epic-<e>`."""
    stage = doc.rsplit(".", 1)[0]
    epic = design_phase_epic(gh, issue, stage)
    head = issue_branch(issue)
    base = _gate_base(gh, issue) if epic is None else epic_branch(epic)
    doc_path = f"{design_doc_dir(epic, issue, gh)}/{doc}"
    # Cite the pushed head, not a local HEAD: the PR only contains what is on origin.
    repo_path = repo_path or "."
    _fetch_origin(repo_path, runner)
    sha = runner(["git", "-C", repo_path, "rev-parse", f"origin/{head}"]).strip()
    if epic is None:
        pr_number = gh.pr_create(
            base=base, head=head,
            title=f"{title} — {doc} for review (#{issue})",
            body=_GATE_PR_BODY.format(issue=issue, next_stage=next_stage),
            draft=False,
        )
    else:
        found = find_design_pr(gh.issue_view(issue).get("comments", []))
        if not found or found[0] != stage:
            raise GhError(f"#{issue} has no {stage} design PR to gate -- run `transition "
                          f"{issue} --expect-stage arch-review` (or `open-design-pr {issue}`) first")
        pr_number = found[1]
        if gh.pr_view(pr_number, fields="state").get("state") == "MERGED":
            return {"issue": issue, "refused": True, "design_pr": pr_number,
                    "reason": f"design PR #{pr_number} is already merged into `{base}` (a human "
                              f"merged it) -- there is no gate to open; run `pass-gate {issue} "
                              f"--gate-pr {pr_number} --stage {stage}`"}
        # Reviewer comments made before the gate opened are not human feedback.
        gh.pr_comment(pr_number,
            f"Human review requested: merging this PR into `{base}` approves `{doc}`; leave "
            f"review comments on it to change something.\n\n"
            f"<!-- gate-comments-processed: {_utc_now_marker()} -->")
    try:
        # The workflow only wakes a runner for labelled PRs; unlabelled, `next-action` still
        # detects the merge, just later.
        gh.pr_add_label(pr_number, LABELS["gate"])
    except GhError:
        pass
    gh.set_pipeline_status_field(issue, "awaiting-human-review")
    timestamp = _utc_now_marker()
    doc_verb = "Requirements locked" if stage == "product" else "Design locked"
    comment = (
        f"✅ {doc_verb} — see `{doc_path}` (`{sha}`). {summary}\n\n"
        f"⏸️ Awaiting human review — see #{pr_number}. Merge it to approve and continue to "
        f"`{next_stage}`, or leave review comments on it for anything that needs to change "
        f"(leave it unmerged — the pipeline picks up your comments and revises the doc "
        f"automatically). Run `/sdlc:run` again once you've merged it, or any time after "
        f"leaving comments if you'd like the revision done sooner.\n\n"
        f"<!-- gate-pr: {stage}:{pr_number} -->\n"
        f"<!-- stage-transition: {stage}->human-review:{stage} @ {timestamp} -->"
    )
    gh.issue_comment(issue, comment)
    return {"issue": issue, "unit": "issue", "gate_pr": pr_number, "stage": stage, "sha": sha,
            "head": head, "base": base}


def git_reconcile_branch(repo_path: str, branch: str, base: str = "main",
                          runner: Runner = _default_runner):
    """Fetch, fast-forward `branch` to its origin tip, merge `origin/<base>` and push.
    Always a merge, never a rebase: squash-merged PRs share no ancestry with their
    branches. Raises `MergeConflict` (after `merge --abort`) on real unmerged paths."""
    _fetch_origin(repo_path, runner)
    runner(["git", "-C", repo_path, "checkout", branch])
    # A stale local tip would make the final push non-fast-forward; ff-only so a
    # genuinely diverged branch fails loudly instead of being reset.
    if origin_branch_exists(repo_path, branch, runner=runner):
        runner(["git", "-C", repo_path, "merge", "--ff-only", f"origin/{branch}"])
    try:
        runner(["git", "-C", repo_path, "merge", f"origin/{base}"])
    except GhError:
        conflicted = [f for f in runner(["git", "-C", repo_path, "diff", "--name-only",
                                          "--diff-filter=U"]).splitlines() if f.strip()]
        if not conflicted:
            raise
        runner(["git", "-C", repo_path, "merge", "--abort"])
        raise MergeConflict(conflicted, base=base)
    _run_retry_transient(["git", "-C", repo_path, "push", "origin", branch], runner)


class MergeConflict(GhError):
    """Real unmerged paths while reconciling a branch; `files` lists them."""

    def __init__(self, files: list, base: str = "main"):
        super().__init__(f"merge conflict reconciling with origin/{base} on {len(files)} "
                          f"file(s): {', '.join(files)}")
        self.files = files
        self.base = base


def _sync_conflict_key(path: str, branch: str, base: str, files: list, runner: Runner) -> str:
    """`<base>.<merge-base sha>.<files digest>`: what makes a sync conflict a new one. Never
    a head sha: `base` or the branch moving on while the same conflict stands is the same
    conflict; resolving it (the branch merges `base`) moves the merge-base."""
    try:
        fork = runner(["git", "-C", path, "merge-base", f"origin/{base}",
                       f"origin/{branch}"]).strip() or "none"
    except GhError:
        fork = "none"
    digest = hashlib.sha1("\n".join(sorted(files)).encode()).hexdigest()[:12]
    return ".".join([base, fork[:12], digest])


def _sync_conflict_posted(gh: GitHub, issue: int, branch: str, key: str) -> bool:
    """Whether the latest `sync-conflict` marker for `branch` on `issue` carries `key`."""
    pattern = re.compile(rf"<!--\s*sync-conflict:\s*{re.escape(branch)}\s*@[^>]*?\bkey:(\S+)")
    try:
        comments = gh.issue_view(issue).get("comments", [])
    except GhError:
        return False
    keys = [m.group(1) for c in comments for m in pattern.finditer(c.get("body") or "")]
    return bool(keys) and keys[-1] == key


def cmd_sync_branch(gh: GitHub, repo_path: Optional[str], issue: int, unit: str = "issue",
                     runner: Runner = _default_runner, base: Optional[str] = None) -> dict:
    """Merge the unit's integration base (or `base`) into its branch and push. A merge
    conflict (`conflict`) or missing base (`base_missing`) is a structured exit-0
    result with `synced: false`, not an error."""
    branch = unit_branch(unit, issue)
    if base:
        base = base[len("origin/"):] if base.startswith("origin/") else base
    else:
        base = integration_base(gh, issue, unit)
    result = {"issue": issue, "unit": unit, "branch": branch, "base": base, "synced": True}
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        # Fetch first so the existence check never reads a stale remote-tracking ref.
        _fetch_origin(ws.path, runner)
        if not origin_branch_exists(ws.path, base, runner=runner):
            result.update({
                "synced": False, "base_missing": True,
                "reason": f"origin/{base} does not exist -- there is nothing to reconcile "
                          f"`{branch}` with, so it is already as current as it can be. If "
                          f"this unit integrates somewhere else, pass `--base <ref>`; "
                          f"otherwise create origin/{base} first."})
        else:
            try:
                git_reconcile_branch(ws.path, branch, base=base, runner=runner)
            except MergeConflict as e:
                # Persisted as a marker so pairing-counts can rebuild it after a crash; keyed
                # so the same unchanged conflict is never re-posted by a later run.
                key = _sync_conflict_key(ws.path, branch, base, e.files, runner)
                if _sync_conflict_posted(gh, issue, branch, key):
                    result["already_posted"] = True
                else:
                    gh.issue_comment(issue,
                        f"⚠️ Merge conflict reconciling `{branch}` with `origin/{base}` — "
                        f"{len(e.files)} file(s): {', '.join(f'`{f}`' for f in e.files)}. "
                        + (f"Resolve it in `worktree-add {issue} --unit initiative`'s tree "
                           f"(merge `origin/{base}`, fix, push), then re-run the sync.\n\n"
                           if unit == "initiative" else
                           f"Routing to `development` for resolution in its own worktree.\n\n")
                        + f"<!-- sync-conflict: {branch} @ {_utc_now_marker()} key:{key} -->")
                result.update({"synced": False, "conflict": True, "conflicting_files": e.files})
    return _with_workspace(result, ws)


# A push rejected because origin moved under us: a retryable race, not a git failure.
_PUSH_REJECTED_RE = re.compile(
    r"\[rejected\]|\[remote rejected\]|non-fast-forward|fetch first|updates were rejected",
    re.IGNORECASE)


def cmd_merge_lld_doc(gh: GitHub, repo_path: Optional[str], epic: int,
                       runner: Runner = _default_runner) -> dict:
    """Verify `epic-<n>/lld.md` is on `origin/epic-<n>` (merged there by the LLD-phase Task's design PR), advance every open, Stage-less
    Task child that the doc carved (a `## Task #<n>` subsection with a `## Footprint`, and
    not realised by another Task) to `development`, and label the Epic architected.
    Idempotent. Returns `merged`, `verified_on_origin`, `advanced_tasks`, `not_advanced`."""
    epic_br = epic_branch(epic)
    doc_path = f"{doc_root_for(epic, gh)}/epic-{epic}/lld.md"
    with branch_lock(epic_br), BranchWorkspace(epic_br, repo_path, runner) as ws:
        _fetch_origin(ws.path, runner)
        blob = _blob_at(ws.path, f"origin/{epic_br}", doc_path, runner)
        tip = (runner(["git", "-C", ws.path, "rev-parse", f"origin/{epic_br}"]).strip()
               if blob is not None else None)
        doc = (runner(["git", "-C", ws.path, "show", f"origin/{epic_br}:{doc_path}"])
               if blob is not None else None)
    if blob is None:
        return _with_workspace({"epic": epic, "merged": False,
                "reason": f"no lld.md on origin/{epic_br} yet — the LLD-phase design PR has not merged"}, ws)
    all_issues = {i["number"]: i for i in gh.issue_list()}
    tasks = [i for i in all_issues.values()
             if i["state"] == "OPEN" and i.get("parent") and i["parent"]["number"] == epic
             and current_stage(i) is None and classify_unit_from_issue(i) == "task"]
    realised_by = {}
    for h in parse_task_headings(doc):
        for old in parse_task_realises(doc[h["line_end"]:h["end"]]):
            realised_by.setdefault(old, h["number"] or h["key"])
    timestamp = _utc_now_marker()
    advanced, not_advanced = [], []
    for task in tasks:
        number = task["number"]
        held = _lld_hold_reason(doc, task, realised_by)
        if held:
            not_advanced.append({"issue": number, "reason": held})
            continue
        gh.set_stage_field(number, "development")
        gh.set_pipeline_status_field(number, "todo")
        gh.issue_comment(number,
            f"➡️ Epic #{epic}'s `lld-review` clean and `lld.md` merged — Stage "
            f"advanced to `development` (not claimed). `next-action` / "
            f"`list-parallel-ready` pick it up as a fresh unit.\n\n"
            f"<!-- stage-transition: lld-review->development @ {timestamp} -->")
        advanced.append(number)
    # The only place `epic:architected` is set: the Epic's Tasks may now proceed.
    completed_now = not has_label(gh.issue_view(epic), LABELS["architected"])
    if completed_now:
        gh.clear_stage_and_status_fields(epic)
        gh.issue_edit(epic, add_labels=[LABELS["architected"]])
        tasks_line = (f"Tasks advanced to `development`: {', '.join(f'#{n}' for n in advanced)}."
                      if advanced else "No fresh Tasks to advance this run.")
        held_line = ("".join(f"\n- #{h['issue']} not advanced: {h['reason']}" for h in not_advanced)
                     if not_advanced else "")
        gh.issue_comment(epic,
            f"📐 Epic design phase (architecture + lld) complete — `{doc_path}` "
            f"(`{tip}`). {tasks_line}{held_line}\n\n"
            f"<!-- stage-transition: epic-lld->children @ {timestamp} -->")
    if not advanced and not completed_now:
        return _with_workspace({"epic": epic, "merged": False, "verified_on_origin": True,
                                "advanced_tasks": [], "not_advanced": not_advanced,
                                "reason": "up-to-date — lld.md is on origin, the Epic is already "
                                          "architected, and no Stage-less Task is left to advance"},
                               ws)
    return _with_workspace({"epic": epic, "merged": True, "verified_on_origin": True,
                            "advanced_tasks": advanced, "not_advanced": not_advanced}, ws)


def _lld_hold_reason(doc: str, task: dict, realised_by: dict) -> Optional[str]:
    """Why `merge-lld-doc` must not advance `task` into the development pool (None when
    it may): the doc realises it through another Task, or never carved it (no
    `## Task #<n>` subsection carrying a `## Footprint` -- matched by number or task-key)."""
    number = task["number"]
    if number in realised_by:
        by = realised_by[number]
        label = f"#{by}" if isinstance(by, int) else f"`{by}`"
        return (f"realised by Task {label} -- it is blocked on that Task and closes when "
                f"that Task's PR merges")
    idents = [number]
    key_marker = _TASK_KEY_MARKER.search(task.get("body") or "")
    if key_marker:
        idents.append(key_marker.group(1))
    if all(find_task_footprint(doc, ident) is None for ident in idents):
        return (f"no `## Task #{number}` subsection with a `## Footprint` heading in lld.md -- "
                f"not carved by `lld`; carve it there, or declare `Realises: #{number}` on "
                f"the Task that covers it")
    return None


def _blob_at(repo_path: str, ref: str, path: str, runner: Runner) -> Optional[str]:
    """Blob SHA of `path` at `ref`, or None when the ref has no such path."""
    try:
        return runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                       f"{ref}:{path}"]).strip() or None
    except GhError:
        return None


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import lld_tasks  # noqa: E402,F401
from .lld_tasks import parse_task_realises  # noqa: E402
from .gates import _gate_base, _profile_for_issue, phase_task_parent  # noqa: E402
