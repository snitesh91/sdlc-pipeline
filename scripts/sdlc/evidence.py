"""Comment markers, branch names and the pipeline evidence a unit must carry."""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Callable, Optional

from .config import CONFIG, EPIC_BRANCH_PREFIX, EVIDENCE_CARRY_FORWARD_GLOBS, PIPELINE, issue_branch
from .github import GhError
from .units import (
    epic_base, epic_base_in, initiative_branch, is_epic_standing, is_initiative,
    resolve_initiative_profile,
)


_GATE_PR_MARKER = re.compile(r"<!--\s*gate-pr:\s*(\w+):(\d+)\s*-->")


_GATE_CUTOFF_MARKER = re.compile(r"<!--\s*gate-comments-processed:\s*([^\s]+)\s*-->")


_NEEDS_HUMAN_REASON = re.compile(r"🙋 Needs human input — (.+?)(?:\n\n<!--|\Z)", re.DOTALL)


# `<!-- stage-transition: <from>-><to> @ <ISO8601> -->`; the timestamp is optional.
_STAGE_TRANSITION_MARKER = re.compile(r"<!--\s*stage-transition:\s*(\S+?)->(\S+?)\s*(?:@[^>]*?)?-->")


# `<!-- stage-route: <from>-><to> @ <ISO8601> -->`, posted by `route`.
_STAGE_ROUTE_MARKER = re.compile(r"<!--\s*stage-route:\s*(\S+?)->(\S+?)\s*(?:@[^>]*?)?-->")


# `<!-- pr-review-outcome: clean|rework:<pr> @ <ISO8601> -->`. Stage/status fields
# can't tell "awaiting review" from "reviewed, in rework"; this marker can.
_PR_REVIEW_OUTCOME_MARKER = re.compile(
    r"<!--\s*pr-review-outcome:\s*(\w+):(\d+)(?:\s+same-class:(true|false))?"
    r"\s*(?:@[^>]*?)?-->")


# `<!-- epic-verification: e2e|exploratory:<epic> sha:<tested head> @ <ISO8601> -->`; must be
# newer than the last `epic-reconciled` marker, and its sha must be current, to count.
_EPIC_VERIFICATION_MARKER = re.compile(
    r"<!--\s*epic-verification:\s*(\w+):(\d+)(?:\s+sha:([0-9a-fA-F]{7,40}))?\s*(?:@[^>]*?)?-->")


_EPIC_RECONCILED_MARKER = re.compile(r"<!--\s*epic-reconciled:\s*(\d+)\s*(?:@[^>]*?)?-->")


# Persists sync-branch conflicts so `cmd_pairing_counts` can rebuild the strike count.
_SYNC_CONFLICT_MARKER = re.compile(r"<!--\s*sync-conflict:\s*(\S+)\s*(?:@[^>]*?)?-->")


# `<!-- design-review-outcome: clean|rework:<role> [same-class:true] [sha:<head>] @ <ISO8601> -->`;
# lets the design-review escalation valve be counted from the thread, not session memory, and
# names the design PR head a review covered.
_DESIGN_REVIEW_OUTCOME_MARKER = re.compile(
    r"<!--\s*design-review-outcome:\s*(\w+):(\S+?)(?:\s+same-class:(true|false))?"
    r"(?:\s+sha:([0-9a-fA-F]{7,40}))?\s*(?:@[^>]*?)?-->")


# A non-standing Epic's architecture/lld phase-Task raises one PR `issue-<n>` -> `epic-<e>`.
# The body marker (`<stage>:<issue>`) tells it from a gate or development PR; the issue-comment
# marker (`<stage>:<pr>`) is how the commands find it again.
_DESIGN_PR_BODY_MARKER = re.compile(r"<!--\s*design-pr:\s*(architecture|lld):(\d+)\s*-->")


_DESIGN_PR_OPENED_MARKER = re.compile(
    r"<!--\s*design-pr-opened:\s*(architecture|lld):(\d+)\s*(?:@[^>]*?)?-->")


_ARCH_CONFIDENCE_MARKER = re.compile(r"<!--\s*arch-review-confidence:\s*(\d+)\s*-->")


# `<!-- local-ci: <suite>:<pr> @ <sha> -->`: a locally run required suite. Counts
# only when <sha> matches the PR head, so any later push invalidates it.
_LOCAL_CI_MARKER = re.compile(
    r"<!--\s*local-ci:\s*([\w-]+):(\d+)\s*@\s*([0-9a-fA-F]{7,40})\s*-->")


# Plus every Initiative profile's `closeSuites` (attested on the initiative PR only).
# `e2e` is built in: the e2e-test Task attests its own specs' run even where no workflow or
# profile names an e2e suite (no merge gate requires it then; it is the PR's record).
LOCAL_CI_SUITES = tuple(dict.fromkeys(
    [w["suite"] for w in CONFIG["requiredWorkflows"]]
    + [suite for prof in PIPELINE["initiativeProfiles"] or [] for suite in
       prof.get("closeSuites") or []] + ["e2e"]))


# Fail at config load on a suite key the marker regex could never match.
for _suite in LOCAL_CI_SUITES:
    if not re.fullmatch(r"[\w-]+", _suite):
        raise ValueError(f"requiredWorkflows suite key {_suite!r} must match [A-Za-z0-9_-]+")


# Optional suite -> regex the `record-local-ci --command` must contain.
LOCAL_CI_COMMAND_PATTERNS = {
    w["suite"]: w["commandPattern"]
    for w in CONFIG["requiredWorkflows"] if w.get("commandPattern")}


# An unexpanded `<placeholder>` token in a copied-from-docs command (e.g. `<node_modules-volume>`).
# Requires an identifier immediately after `<` and no spaces, so shell input redirection
# (`< file`), heredocs (`<<EOF`) and `2>&1` never match.
_UNEXPANDED_PLACEHOLDER = re.compile(r"<[A-Za-z_][\w.-]*>")


# Stamped by `record-local-ci`: where the run happened, from CLAUDE_CODE_REMOTE (the
# harness's own signal), never the `SDLC_PLACEMENT` override a laptop can set.
LOCAL_CI_CLOUD_MARKER = "<!-- local-ci-placement: cloud -->"


def remote_session() -> bool:
    """Whether this process runs in a Claude cloud session (CLAUDE_CODE_REMOTE)."""
    return os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in ("true", "1")


def local_ci_suites_attested(comments: list, head_sha: str, cloud_only: bool = False) -> set:
    """Set of suite keys with a `local-ci` attestation matching `head_sha` (`cloud_only`:
    only attestations stamped from a cloud session).
    Prefix match either way (short vs full sha); an unknown head trusts nothing."""
    if not head_sha:
        return set()
    head = head_sha.lower()
    attested = set()
    for c in comments:
        if cloud_only and LOCAL_CI_CLOUD_MARKER not in c.get("body", ""):
            continue
        for m in _LOCAL_CI_MARKER.finditer(c.get("body", "")):
            suite, sha = m.group(1), m.group(3).lower()
            if head.startswith(sha) or sha.startswith(head):
                attested.add(suite)
    return attested


def find_gate_pr(comments: list) -> Optional[tuple]:
    stage = pr = None
    for c in comments:
        m = _GATE_PR_MARKER.search(c.get("body", ""))
        if m:
            stage, pr = m.group(1), int(m.group(2))
    return (stage, pr) if pr is not None else None


REVIEW_ROLES = frozenset({"product-review", "arch-review", "lld-review", "pr-review"})


def epic_branch(epic: int) -> str:
    """The long-lived integration branch every Epic owns (default `epic-<n>`)."""
    return f"{EPIC_BRANCH_PREFIX}{epic}"


def unit_branch(unit: str, number: int) -> str:
    """The branch for an `issue`, `epic` or `initiative` unit, using the configured prefixes."""
    if unit == "initiative":
        return initiative_branch(number)
    return epic_branch(number) if unit == "epic" else issue_branch(number)


# Stages only a non-standing Epic's phase-Tasks (Architecture-phase, Architecture
# revision, LLD-phase) sit at; functional Tasks enter at `development`.
PHASE_TASK_STAGES = frozenset({"architecture", "arch-review", "lld", "lld-review"})


def integration_base(gh: "GitHub", issue: int, unit: str = "issue") -> str:
    """Branch this unit integrates into: `epic-<parent>` for every child of an Epic, standing
    or not (phase-Tasks included); for an Epic, its Initiative's `initiative-<i>`
    (`epic_base`); else `main` (initiatives, parentless issues -- the hotfix path --, and an
    Initiative's Product-Roadmap Task)."""
    if unit == "initiative":
        return "main"
    if unit == "epic":
        return epic_base(gh, issue)
    # `parent` exists only in `issue_list`'s GraphQL; `gh issue view --json` has no such field.
    return integration_base_in({i["number"]: i for i in gh.issue_list()}, issue)


def integration_base_in(issues: dict, issue: int) -> str:
    """`integration_base` over an already-fetched `{number: issue}` map."""
    entry = issues.get(issue)
    if entry is None:
        raise GhError(f"issue #{issue} not found in the repo issue list")
    parent = entry.get("parent")
    if not parent:
        return "main"
    parent_number = parent["number"]
    parent_entry = issues.get(parent_number)
    if parent_entry is not None and is_initiative(parent_entry):
        # A plain child of an Initiative with `branch: true` works where its product code
        # lives; the Product-Roadmap Task (its docs gate) still integrates into main.
        if (not _is_roadmap_task(entry)
                and resolve_initiative_profile(parent_entry)["branch"]):
            return initiative_branch(parent_number)
        return "main"
    return epic_branch(parent_number)


def _is_roadmap_task(entry: dict) -> bool:
    """An Initiative's Product-Roadmap Task (cut titled "Product Roadmap",
    references/initiatives.md)."""
    return "product roadmap" in str(entry.get("title") or "").lower()


def last_transition_to(comments: list, to_role: str,
                       marker: re.Pattern = _STAGE_TRANSITION_MARKER) -> Optional[int]:
    """Index of the latest comment with a `marker` (default stage-transition) into `to_role`,
    or None. Matches the destination only (last `->` segment): hand-written from-roles are unreliable."""
    found = None
    for idx, c in enumerate(comments):
        for m in marker.finditer(c.get("body", "")):
            if m.group(2).rsplit("->", 1)[-1] == to_role:
                found = idx
    return found


def last_pr_review_outcome(comments: list) -> Optional[tuple]:
    """`(comment_index, outcome, pr_number)` of the latest pr-review outcome, or None."""
    found = None
    for idx, c in enumerate(comments):
        m = _PR_REVIEW_OUTCOME_MARKER.search(c.get("body", ""))
        if m:
            found = (idx, m.group(1), int(m.group(2)))
    return found


def routed_past_pr_review(comments: list) -> bool:
    """Whether this round (since the latest `development->pr-review` handoff) was routed
    straight to merge by `route --to merge`."""
    handoff = last_transition_to(comments, "pr-review")
    routed = last_transition_to(comments, "merge", _STAGE_ROUTE_MARKER)
    return handoff is not None and routed is not None and routed > handoff


def missing_pipeline_evidence(comments: list) -> list:
    """Problems blocking merge: a missing pr-review handoff, or a pr-review outcome that is
    missing, older than the latest handoff, or not `clean` -- a round routed past `pr-review`
    (`route --to merge`) needs no outcome: the recorded route is its evidence. Empty = mergeable."""
    problems = []
    handoff = last_transition_to(comments, "pr-review")
    if handoff is None:
        problems.append("no `development->pr-review` handoff marker (run "
                        "`handoff-to-pr-review <issue> --pr <pr> --summary ...`)")
    outcome = last_pr_review_outcome(comments)
    reviewed = outcome is not None and handoff is not None and outcome[0] > handoff
    if not reviewed and routed_past_pr_review(comments):
        return problems
    if outcome is None:
        problems.append("no `pr-review` outcome recorded (run "
                        "`record-pr-review <issue> --pr <pr> --outcome clean|rework`)")
    elif handoff is not None and outcome[0] < handoff:
        problems.append(f"the recorded `pr-review` outcome ({outcome[1]}) predates the latest "
                        f"`development->pr-review` handoff -- it belongs to an earlier round; "
                        f"re-run `pr-review` on the current branch and record it")
    elif outcome[1] != "clean":
        problems.append(f"the latest recorded `pr-review` outcome is `{outcome[1]}`, not `clean` -- "
                        f"the review asked for rework. Address it, re-run `pr-review`, and record "
                        f"a clean outcome before merging")
    return problems


# Closing evidence `close-epic` requires. The full e2e suite is not one: the pipeline never
# runs it (the driven repo does, outside the pipeline); the e2e-test Task runs its new specs. `record-epic-verification --kind e2e`
# is still accepted (informational), it just no longer gates the merge.
EPIC_CLOSE_REQUIRED_EVIDENCE = (("exploratory", "exploratory pass"),)


def missing_epic_verification(comments: list, stale_since: Optional[Callable] = None) -> list:
    """Problems with an epic's exploratory closing evidence. `stale_since(sha)` returns a
    problem when the epic branch moved on from the tested `sha` with code changes -- a `main`
    reconcile included; without it (or a sha) evidence older than the last reconcile counts
    as missing. Empty = complete."""
    problems = []
    reconciled = None
    for idx, c in enumerate(comments):
        if _EPIC_RECONCILED_MARKER.search(c.get("body", "")):
            reconciled = idx
    seen = {}
    for idx, c in enumerate(comments):
        m = _EPIC_VERIFICATION_MARKER.search(c.get("body", ""))
        if m:
            seen[m.group(1)] = (idx, m.group(3))
    for kind, label in EPIC_CLOSE_REQUIRED_EVIDENCE:
        if kind not in seen:
            problems.append(f"no `{kind}` closing-verification evidence ({label} never recorded)")
        # The tested-sha delta spans the reconcile merge, so it alone judges a stamped record.
        elif reconciled is not None and seen[kind][0] < reconciled and not (
                stale_since is not None and seen[kind][1]):
            problems.append(f"the `{kind}` evidence predates the last `origin/main` reconcile of "
                            f"the epic branch -- it describes a different tree; re-run it")
        elif stale_since is not None:
            sha = seen[kind][1]
            reason = (f"was recorded without a tested head sha -- re-run and re-record it"
                      if not sha else stale_since(sha))
            if reason:
                problems.append(f"the `{kind}` evidence {reason}")
    return problems


def _glob_matches(path: str, glob: str) -> bool:
    """fnmatch with GHA's rule that a leading `**/` also matches at the repo root."""
    return fnmatch.fnmatch(path, glob) or (glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]))


def evidence_carries_forward(path: str) -> bool:
    """Whether a file changed after the exploratory evidence was stamped leaves it valid: one
    of `pipeline.epicClose.evidenceCarryForward.paths`; the pipeline config never is."""
    if touches_pipeline_config([path]):
        return False
    return any(_glob_matches(path, g) for g in EVIDENCE_CARRY_FORWARD_GLOBS)


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import epic_close  # noqa: E402,F401
from .checks import touches_pipeline_config  # noqa: E402
