#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""sdlc_next.py — deterministic control plane for the sdlc-pipeline skill.

Owns every mechanical GitHub read/decision/mutation described in the skill's
`SKILL.md` so the orchestrating agent never hand-executes
`gh`/GraphQL sequences from prose. The agent's job is calling this CLI, acting on
its JSON verdict, and making the small set of genuine judgment calls (defect
attribution, escalation-to-human, subagent prompt content, doc/PR prose) that
cannot be scripted — see "Deterministic control plane" in SKILL.md.

Exit codes: 0 = command completed with a valid result (including legitimate
"nothing to do" outcomes). 1 = an operational failure (a gh/git call actually
failed, a required marker was missing). Every command prints one JSON object to
stdout regardless of exit code.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

CONFIG_FILENAME = "sdlc-pipeline.config.json"


def _find_config() -> Optional[str]:
    """Locate the consuming repo's config. This skill is generic and lives outside
    the repo it drives (often symlinked in); every project-specific value lives in
    the repo, never here. Resolution order:

      1. `SDLC_CONFIG` -- an explicit path (used by the test suite for its sample).
      2. Walking up from the current directory for `<CONFIG_FILENAME>` at the repo
         root, or under `.config/` or `.claude/` -- the pipeline is invoked from
         the repo root, and the file is harness-agnostic (root by default).

    `sdlc.config.sample.json` ships beside this skill as a template to copy into a
    new repo; it is never loaded automatically, so a missing config is a loud setup
    error rather than a silent run against placeholder ids."""
    env = os.environ.get("SDLC_CONFIG")
    if env:
        return os.path.expanduser(env)
    d = os.getcwd()
    while True:
        for sub in ("", ".config", ".claude"):
            cand = os.path.join(d, sub, CONFIG_FILENAME) if sub else os.path.join(d, CONFIG_FILENAME)
            if os.path.isfile(cand):
                return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _load_config() -> dict:
    path = _find_config()
    if not path:
        raise SystemExit(
            f"sdlc-pipeline: no {CONFIG_FILENAME} found (searched $SDLC_CONFIG and up "
            f"from {os.getcwd()} at ./, .config/, .claude/). Copy sdlc.config.sample.json "
            f"into your repo root as {CONFIG_FILENAME} and fill in its values.")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"sdlc-pipeline: cannot load config at {path}: {e}")


CONFIG = _load_config()
_PF = CONFIG["projectFields"]

REPO = CONFIG["repo"]
# Repo owner/name split out for the GraphQL query templates, which name the
# repository inline (gh's `--repo` flag doesn't reach `api graphql`).
_OWNER, _NAME = REPO.split("/", 1)
# Root folder (under the repo) for the committed per-issue/-epic source-of-record
# docs -- `<DOC_ROOT>/issue-<n>/` and `<DOC_ROOT>/epic-<n>/`.
DOC_ROOT = CONFIG["docRoot"]
# Where the classic-PAT GitHub token is read from; surfaced only in the runnable
# command strings this CLI hands back to the orchestrator.
TOKEN_PATH = CONFIG["tokenPath"]
STAGE_AFTER_GATE = {"product": "architecture", "architecture": "development"}

# How many finished PRs (within the one epic the operator named -- see "Epic
# number is mandatory" in SKILL.md) may be reviewed concurrently, one git
# worktree each --
# the cap `list-ready-for-review` returns by default. Reviews have zero
# dependency on each other, so this is tuned purely against **Docker resource
# contention** (each review agent independently re-runs the real test suite; the
# backend suite alone has already produced OOM kills and flaky runs on its own --
# issues #109/#111), not against any structural/dependency concern. Lower it if
# concurrent reviews start reporting resource-starvation failures rather than
# real defects. See "Parallel PR review" in references/parallelism.md.
PR_REVIEW_PARALLELISM = CONFIG["parallelism"]["prReview"]

# How many `lld`/`development`/`testing` children of one epic may run
# concurrently, one git worktree each -- the cap `list-parallel-ready` returns
# by default. Unlike PR_REVIEW_PARALLELISM, this tier has real cross-child
# dependency risk (one child's code can genuinely need another's), so
# `list-parallel-ready` only ever proposes children that pass both a native
# `blockedBy` check and a footprint-overlap check against every other
# currently-active/eligible child -- see "Parallel implementation lane" in
# SKILL.md. Matches PR_REVIEW_PARALLELISM's value; raised to 3 from an initial
# 2 per operator instruction once the first two concurrent children --
# #185/#187 under epic #110 -- ran clean. Also tuned against the same Docker
# test-suite resource pressure noted above `PR_REVIEW_PARALLELISM` --
# `development`/`testing` each run the real suite, same cost as a review --
# lower it again if concurrent runs start reporting resource starvation rather
# than real defects.
DEV_LANE_PARALLELISM = CONFIG["parallelism"]["devLane"]

# How many *standing*-profile children of one epic may run their `product`/
# `architecture` stages concurrently, one git worktree each -- the cap
# `list-design-ready` returns by default. A standing epic has no epic-level
# design phase (see `is_epic_standing`); every child runs its OWN
# product->architecture->lld->development->testing flow, so without a pool query
# for the design stages the orchestrator could only run one child's product/
# architecture at a time. Deliberately lower than DEV_LANE_PARALLELISM (2 vs 3):
# both stages run the `opus` model (see `pipeline.models`) and cost more per unit
# than the dev lane's sonnet stages. Read via `.get` with a default so a config
# predating this key (or any default-profile repo that never fans design out)
# still loads. A default-profile epic's product/architecture is epic-self and
# single-unit -- `list-design-ready` returns empty for it regardless of this cap.
# See "Design lane" in references/parallelism.md.
DESIGN_LANE_PARALLELISM = CONFIG["parallelism"].get("designLane", 2)

# Only used by check_epics_closeable's one-time "ready to close" notification --
# mark_needs_human/open_gate no longer touch the assignee (operator instruction:
# the Pipeline Status field is the tracking mechanism on its own). See "Assignee
# convention" in references/operations.md.
HUMAN_ASSIGNEE = CONFIG["humanAssignee"]

# Everything below is tunable per repo via the optional `pipeline` block in the
# config; every key has the default shown here so an older config keeps working.
_PIPELINE_DEFAULTS = {
    "labels": {"standing": "epic:standing", "legacy": "epic:legacy",
               "architected": "epic:architected"},
    "branches": {"issuePrefix": "issue-", "epicPrefix": "epic-", "gateSuffix": "-gate-"},
    # `ephemeralPrefix` names the throwaway worktree a branch-touching command
    # stands up when no live worktree holds its target branch -- the main checkout
    # is never a git-write target (see `branch_workspace`).
    "worktrees": {"root": "/tmp", "devPrefix": "sdlc-dev-", "epicPrefix": "sdlc-epic-",
                  "reviewPrefix": "sdlc-review-", "ephemeralPrefix": "sdlc-tmp-"},
    # Per-branch flock so two sessions (or two lanes of one session) never run a
    # branch-touching command on the same branch concurrently. `dir` may use
    # `{worktreesRoot}`; `SDLC_LOCK_DIR` in the environment overrides it (tests).
    "locks": {"dir": "{worktreesRoot}/.sdlc-locks", "waitSeconds": 600},
    # Where the driven repo vendors this skill as a git submodule. `worktree-add`
    # and `sync-branch` run `git submodule update --init` on it inside the unit's
    # worktree, so each epic's stage agents read the playbook/persona files at
    # the skill version *its own branch* pins (`<worktree>/<submodulePath>`), not
    # the shared main checkout's copy. `probeFile` must exist after init or the
    # init is reported failed. Empty `submodulePath` disables the whole feature.
    "skill": {"submodulePath": ".github/sdlc-pipeline", "probeFile": "SKILL.md"},
    # Per-epic isolated runtime stack (`provision-epic-stack` / `teardown-epic-stack`).
    # Config-only isolation: the driven repo's compose must already honour the env
    # keys listed under `ports` plus `COMPOSE_PROJECT_NAME` and `dataDirKey`. Every
    # `{...}` placeholder is formatted with profile/project/envFile/secretsFile/
    # dataDir/workspaceRoot. `enabled: false` makes both commands structured no-ops.
    "stack": {
        "enabled": False,
        "workspaceRoot": ".",
        "baseProfile": "dev",
        "profileTemplate": "epic{n}",
        "envFile": ".env.{profile}",
        "secretsFile": ".secrets.{profile}",
        "composeProjectTemplate": "sdlc-{profile}",
        "ports": {"FRONTEND_PORT": 3000, "BACKEND_PORT": 3001,
                  "DB_PORT_EXPOSE": 5432, "BACKEND_DEBUG_PORT": 9229},
        "portStride": 20,
        "dataDirKey": "POSTGRES_DATA_DIR",
        "dataDirTemplate": ".docker/postgres-data-{profile}",
        "upCommand": "COMPOSE_PROJECT_NAME={project} make fullstack-d PROFILE={profile}",
        "seedCommand": "",
        "downCommand": "COMPOSE_PROJECT_NAME={project} docker compose "
                       "--env-file {envFile} down -v --remove-orphans",
    },
    "gates": {"skipConfidenceThreshold": 95, "requiresHumanGateA": True},
    # Product-stage WIP cap (operator, 2026-08-16; epic #92 produced five parallel
    # Gate A PRs a human could not keep up with). At most `maxGateAPending` open
    # units repo-wide may sit at Stage=Product with an open Gate A (Pipeline Status
    # awaiting-human-review / feedback-received) before `next-action` and
    # `list-design-ready` stop starting *fresh* `product` delegations and loop to
    # other actionable units instead. Resumes, rework rounds, and gate actions
    # (`pass-gate` / `address-gate-feedback`) are never gated. `0` disables.
    "productWip": {"maxGateAPending": 5},
    "escalation": {"replaceAt": 3, "needsHumanAt": 6},
    # `{docRoot}` is formatted with the config's `docRoot` at load time, so the
    # default lands next to the committed docs of the driven repo (e.g.
    # `docs/sdlc/retro-watermark`) rather than inside the skill checkout.
    "retro": {"everyClosedIssues": 5,
              "watermarkFile": "{docRoot}/retro-watermark"},
    "continuous": {"cycleCap": 8},
    # `auto: true` lets the orchestrator invoke the second `close-epic` call itself
    # once the epic is closeable and both closing verifications are clean -- no human
    # trigger. Default `false` keeps the historical human-makes-the-call gate. The
    # mechanical refusals in `cmd_close_epic` (open children, missing/ stale
    # verification, failing checks) are the safety net either way: a Blocker delta
    # filed as an epic child re-trips `open_children`, so auto-close cannot fire over
    # one. See "Epic closing" in references/epics.md.
    "epicClose": {"auto": False},
    "models": {"product": "opus", "product-review": "opus", "architecture": "opus",
               "arch-review": "opus", "lld": "sonnet", "lld-review": "opus",
               "development": "sonnet", "pr-review": "opus"},
    "docTemplates": "_templates",
    # Behavioural profiles, matched to an epic by label (ordered; first hit wins,
    # `"*"` is the terminal catch-all). Each profile is a bundle of toggles that
    # used to be hardcoded to the `epic:standing`/`epic:legacy` labels; the client
    # now owns the label->behaviour mapping. See "Epic profiles" in references/epics.md
    # and `resolve_profile`. Missing toggles inherit `_PROFILE_TOGGLE_DEFAULTS`; a
    # profile's `gates` inherit the global `pipeline.gates`.
    "profiles": [
        {"name": "legacy", "match": {"label": "epic:legacy"}, "driven": False},
        {"name": "standing", "match": {"label": "epic:standing"},
         "epicLevelPhase": False, "childEntryStage": "product",
         "childrenNeedArchitectedEpic": False, "closes": False},
        {"name": "default", "match": "*"},
    ],
}

# Per-toggle fallbacks a matched profile inherits when it omits a key. `gates`
# inherit the effective global `pipeline.gates` (resolved in `resolve_profile`),
# not this dict, so an epic with no explicit profile gates keeps the global bar.
_PROFILE_TOGGLE_DEFAULTS = {
    "driven": True,               # False = legacy: pipeline ignores the epic + children
    "epicLevelPhase": True,       # False = no epic-level product/architecture phase
    "childEntryStage": "lld",     # "lld" | "product" -- where children enter
    "childrenNeedArchitectedEpic": True,  # children gated on epic:architected
    "closes": True,               # epic closes + has an integration branch
}


def _pipeline_config() -> dict:
    """Merge the config's `pipeline` block over `_PIPELINE_DEFAULTS`, one level deep."""
    merged = {}
    user = CONFIG.get("pipeline", {}) or {}
    for key, default in _PIPELINE_DEFAULTS.items():
        if isinstance(default, dict):
            merged[key] = {**default, **(user.get(key) or {})}
        else:
            merged[key] = user.get(key, default)
    merged["retro"]["watermarkFile"] = merged["retro"]["watermarkFile"].format(docRoot=DOC_ROOT)
    return merged


PIPELINE = _pipeline_config()
LABELS = PIPELINE["labels"]
ISSUE_BRANCH_PREFIX = PIPELINE["branches"]["issuePrefix"]
EPIC_BRANCH_PREFIX = PIPELINE["branches"]["epicPrefix"]
# Joins an epic branch name to the gate stage it is carrying a doc for:
# `epic-<n>` + `-gate-` + `product` -> `epic-5-gate-product`. See `epic_gate_branch`.
GATE_BRANCH_SUFFIX = PIPELINE["branches"]["gateSuffix"]
ESCALATION = PIPELINE["escalation"]
# Read at call time by `product_wip_headroom` (never captured at import into a
# default arg) so a test or a one-off run can override it on the module.
PRODUCT_WIP_CAP = PIPELINE["productWip"]["maxGateAPending"]


def issue_branch(number: int) -> str:
    """The per-child working branch (default `issue-<n>`)."""
    return f"{ISSUE_BRANCH_PREFIX}{number}"


def issue_number_from_branch(branch: str) -> Optional[int]:
    """Inverse of `issue_branch`; None for any branch that isn't one."""
    if not branch.startswith(ISSUE_BRANCH_PREFIX):
        return None
    rest = branch[len(ISSUE_BRANCH_PREFIX):]
    return int(rest) if rest.isdigit() else None

# Native GitHub Issue Types (Task/Bug/Feature) and the org-provisioned "Priority"
# issue field replace the old epic/bug/type:bug/priority:pX labels -- see
# "Issue taxonomy" in references/operations.md. IDs looked up once via GraphQL introspection
# against this repo and hardcoded here: they're schema-level IDs for this
# specific repo's provisioned types/fields, not per-issue data, so there's
# nothing to look up dynamically.
ISSUE_TYPE_IDS = _PF["issueTypeIds"]
PRIORITY_FIELD_ID = _PF["priorityFieldId"]
PRIORITY_OPTION_IDS = _PF["priorityOptionIds"]
# Priority rank for sort_key/epic ranking -- lower sorts first, matching the old
# p0(highest)..p3(lowest) convention. No native field value = rank 2 (Medium),
# same default the old "no priority label = p2" rule used.
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
EFFORT_FIELD_ID = _PF["effortFieldId"]
EFFORT_OPTION_IDS = _PF["effortOptionIds"]
# Native "Stage" and "Pipeline Status" issue fields (operator-provisioned) are the
# SOLE source of truth for stage/status -- see "Issue taxonomy" in references/operations.md. The old
# stage:*/status:* labels have been removed from the repo entirely (retired
# 2026-08-16, per operator instruction) -- `epic:standing` is the one label this
# pipeline still reads/writes. Because these fields are now load-bearing (not a
# best-effort board mirror), a write failure here must raise, never be swallowed.
#
# "LLD" was added as its own real option 2026-08-20, per operator instruction --
# see "LLD is its own Stage value" in references/epics.md. Before this, a normal-epic
# child's lighter per-task design pass had no Stage option of its own and
# reused "Architecture" (the same value a standing/legacy-epic child's real,
# full architecture stage uses), relying entirely on prose/agent judgment to
# tell the two apart. That overload is gone: `default_stage()` now returns
# "lld" directly for a normal-epic child, and every place that reads/writes the
# Stage field treats it as an ordinary distinct value, same as any other stage.
STAGE_FIELD_ID = _PF["stageFieldId"]
STAGE_OPTION_IDS = _PF["stageOptionIds"]
STAGE_FIELD_NAMES = {
    "Product": "product", "Architecture": "architecture", "Development": "development",
    "PR Review": "pr-review", "LLD": "lld",
    # `Testing` is a RETIRED stage -- it was merged into `development` on
    # 2026-09-12 (the implementer writes and runs its own tests; `pr-review` judges
    # whether they are any good). Nothing writes this value any more. The read
    # mapping stays so an issue stranded at the old value by an in-flight epic
    # still resolves to a stage the dev lane will pick up instead of reading as
    # "no stage at all".
    "Testing": "testing",
}

# Stages a child can be sitting at when its draft PR is waiting for `pr-review`.
# `pr-review` is what `open-dev-pr` now sets; `testing` is the retired value an
# epic already in flight may have stamped before this change, kept so those
# children still surface in `list-ready-for-review` instead of silently vanishing
# from the queue.
REVIEW_ENTRY_STAGES = ("pr-review", "testing")
PIPELINE_STATUS_FIELD_ID = _PF["pipelineStatusFieldId"]
PIPELINE_STATUS_OPTION_IDS = _PF["pipelineStatusOptionIds"]
PIPELINE_STATUS_FIELD_NAMES = {
    "Todo": "todo", "In Progress": "in-progress",
    "Awaiting Human Review": "awaiting-human-review", "Needs Human": "needs-human",
    "Feedback Received": "feedback-received", "Done": "done",
}
# A gate PR is still "currently open" for an issue at either of these two Pipeline
# Status values -- `Feedback Received` is purely a visibility flip on top of
# `Awaiting Human Review` (see `cmd_mark_feedback_received` / "Human-review gates" in
# SKILL.md), never a second, independent gate state. Every place that used to check
# `pipeline_status(x) == "awaiting-human-review"` to mean "this unit has an open gate"
# must check membership in this tuple instead, or it stops recognizing a
# `feedback-received` issue as gate-pending at all.
GATE_PENDING_STATUSES = ("awaiting-human-review", "feedback-received")

# The board's own native Projects-v2 "Status" field (Todo/In Progress/Done) --
# genuinely distinct from the "Pipeline Status" Issue Field above (see the
# "Note" under "Repo access" in references/operations.md: Projects v2 access was unavailable
# for a while, which is why Pipeline Status exists as a separate Issue Field in
# the first place). This one is a human-facing board convenience, set only for
# a normal epic's own start/close (see "Epic board Status" in references/epics.md) -- never
# read back by any pipeline decision, so a write failure here is always
# swallowed, not raised (see GitHub.set_project_status).
PROJECT_ID = _PF["projectId"]
PROJECT_NUMBER = _PF["projectNumber"]
STATUS_FIELD_ID = _PF["statusFieldId"]
STATUS_OPTION_IDS = _PF["statusOptionIds"]

Runner = Callable[[list], str]


class GhError(RuntimeError):
    pass


def _default_runner(argv: list) -> str:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}")
    return result.stdout


class GitHub:
    """The only place gh/GraphQL subprocess calls happen. Inject a fake `runner` in tests."""

    def __init__(self, repo: str = REPO, runner: Runner = _default_runner):
        self.repo = repo
        self._run = runner

    def issue_list(self) -> list:
        """All issues (open AND closed) with the fields the picker needs, including
        `issueType`/`parent` -- `gh issue list --json` (porcelain) does not expose
        either field, so this goes straight to GraphQL. Returns both states (not
        just open) because epic-started detection needs to see closed children too;
        callers that want only open issues filter on `issue["state"] == "OPEN"`
        themselves (see decide_next_action, cmd_list_needs_human)."""
        issues = []
        after = "null"
        while True:
            data = self.graphql(_ISSUE_LIST_QUERY.format(after=after))
            conn = data["repository"]["issues"]
            for node in conn["nodes"]:
                node["labels"] = node["labels"]["nodes"]
                node["milestone"] = (node.get("milestone") or {}).get("title")
                node["fields"] = {
                    v["field"]["name"]: v["name"]
                    for v in node.pop("issueFieldValues")["nodes"]
                    if v["__typename"] == "IssueFieldSingleSelectValue"
                }
                issues.append(node)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = f'"{conn["pageInfo"]["endCursor"]}"'
        return issues

    def issue_view(self, number: int) -> dict:
        out = self._run(["gh", "issue", "view", str(number), "--repo", self.repo,
                          "--json", "number,title,labels,body,state,comments"])
        return json.loads(out)

    def issue_edit(self, number: int, add_labels: list = (), remove_labels: list = (),
                    add_assignees: list = (), remove_assignees: list = ()):
        if not add_labels and not remove_labels and not add_assignees and not remove_assignees:
            return
        argv = ["gh", "issue", "edit", str(number), "--repo", self.repo]
        for l in add_labels:
            argv += ["--add-label", l]
        for l in remove_labels:
            argv += ["--remove-label", l]
        for a in add_assignees:
            argv += ["--add-assignee", a]
        for a in remove_assignees:
            argv += ["--remove-assignee", a]
        self._run(argv)

    def issue_node_id(self, number: int) -> str:
        data = self.graphql(_ISSUE_NODE_ID_QUERY.format(n=number))
        return data["repository"]["issue"]["id"]

    def issue_fields(self, number: int) -> dict:
        """Single-issue native-field read (name -> value), e.g. {"Stage": "Testing"}.
        Same shape as the per-issue "fields" dict issue_list() builds in bulk, for a
        caller (verify_exit, ad-hoc checks) that only needs one issue."""
        data = self.graphql(_ISSUE_FIELDS_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["issueFieldValues"]["nodes"]
        return {n["field"]["name"]: n["name"] for n in nodes if n["__typename"] == "IssueFieldSingleSelectValue"}

    def issue_epic_info(self, number: int) -> dict:
        """Minimal single-issue fetch (issueType/parent/labels) for is_epic() and
        friends -- cheaper than issue_list()'s full paginated bulk fetch when a
        caller (cmd_claim, cmd_mark_issue_closed) only needs to classify one issue."""
        data = self.graphql(_ISSUE_EPIC_CHECK_QUERY.format(n=number))
        node = data["repository"]["issue"]
        node["labels"] = node["labels"]["nodes"]
        return node

    def project_item_id(self, number: int) -> Optional[str]:
        """The Projects-v2 item id for `number` within the board (PROJECT_NUMBER)
        -- distinct from the issue's own node id, since Projects v2 field
        mutations are scoped to a project item, not the issue itself. None if
        this issue isn't (yet) on the board."""
        data = self.graphql(_ISSUE_PROJECT_ITEM_QUERY.format(n=number))
        for item in data["repository"]["issue"]["projectItems"]["nodes"]:
            if item["project"]["number"] == PROJECT_NUMBER:
                return item["id"]
        return None

    def set_project_status(self, number: int, status: str):
        """Best-effort write to the board's native Projects-v2 "Status" field
        (Todo/In Progress/Done) -- see "Epic board Status" in references/epics.md. Distinct
        from set_pipeline_status_field above (a different, load-bearing field):
        this one is a human-facing board convenience only, so a missing project
        item or a transient GraphQL failure is swallowed here, never raised --
        it must never block or fail a stage claim or a workflow run."""
        item_id = self.project_item_id(number)
        if item_id is None:
            return
        try:
            self.graphql(_SET_PROJECT_STATUS_MUTATION.format(
                project_id=PROJECT_ID, item_id=item_id, field_id=STATUS_FIELD_ID,
                option_id=STATUS_OPTION_IDS[status]))
        except GhError:
            pass

    def blocked_by(self, number: int) -> list:
        """Open issue numbers blocking `number`, via the native blockedBy relationship
        -- replaces the old body-text '## Dependencies' section parsing. Closed
        blockers are omitted, so an empty result means "not blocked right now",
        never requiring a separate dependency-cleared step (see decide_next_action)."""
        data = self.graphql(_BLOCKED_BY_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blockedBy"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def add_blocked_by(self, issue_number: int, blocking_number: int):
        issue_id = self.issue_node_id(issue_number)
        blocking_id = self.issue_node_id(blocking_number)
        self.graphql(_ADD_BLOCKED_BY_MUTATION.format(issue_id=issue_id, blocking_id=blocking_id))

    def blocking(self, number: int) -> list:
        """Open issue numbers that `number` itself blocks -- the reverse of
        blocked_by. Used to check whether closing an epic's child left some other
        still-open issue waiting on it (see cmd_check_epics_closeable)."""
        data = self.graphql(_BLOCKING_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blocking"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def set_issue_type(self, number: int, type_name: str):
        issue_id = self.issue_node_id(number)
        type_id = ISSUE_TYPE_IDS[type_name]
        self.graphql(_SET_ISSUE_TYPE_MUTATION.format(issue_id=issue_id, type_id=type_id))

    def set_stage_field(self, number: int, stage: str):
        """Writes the native "Stage" issue field -- the sole source of truth
        current_stage() reads back (stage:* labels retired 2026-08-16). Unlike the
        old best-effort mirror, a failure here must propagate: an unset field is
        indistinguishable from a fresh issue (default_stage() would silently
        misclassify a mid-pipeline issue), so this deliberately does NOT swallow
        GhError."""
        issue_id = self.issue_node_id(number)
        option_id = STAGE_OPTION_IDS[stage]
        self.graphql(_SET_ISSUE_FIELD_MUTATION.format(
            issue_id=issue_id, field_id=STAGE_FIELD_ID, option_id=option_id))

    def set_pipeline_status_field(self, number: int, status: str):
        """Writes the native "Pipeline Status" issue field -- the sole source of
        truth pipeline_status() reads back (status:* labels retired 2026-08-16).
        `status` is the label-style suffix (e.g. "in-progress"). Does not swallow
        GhError, for the same reason as set_stage_field above."""
        issue_id = self.issue_node_id(number)
        option_id = PIPELINE_STATUS_OPTION_IDS[status]
        self.graphql(_SET_ISSUE_FIELD_MUTATION.format(
            issue_id=issue_id, field_id=PIPELINE_STATUS_FIELD_ID, option_id=option_id))

    def clear_stage_and_status_fields(self, number: int):
        """Deletes both native field values -- called once an epic completes its
        own Product/Architecture phase and hands off to its children
        (`_complete_epic_architecture`). The epic stays open with children still
        pending, so this is deliberately a blank/deleted state, not `Done` --
        it has no "current stage" of its own anymore, but it isn't finished
        either. See `cmd_mark_issue_closed` for the genuinely-closed case, which
        sets Pipeline Status to `Done` instead of deleting it."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=PIPELINE_STATUS_FIELD_ID))

    def clear_stage_field(self, number: int):
        """Deletes only the Stage value, leaving Pipeline Status untouched --
        used when an issue closes (`cmd_mark_issue_closed`): "current stage" is
        meaningless once closed, but Pipeline Status gets set to `Done` right
        after this call, not deleted."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))

    def clear_pipeline_status_field(self, number: int):
        """Deletes only the Pipeline Status value, leaving Stage untouched -- used
        to park a task issue whose LLD found an architecture deviation and is
        waiting on the owning epic's re-gate (see "Epic-level deviation escalation"
        in SKILL.md). Unlike clear_stage_and_status_fields, the task isn't done --
        its Stage (still `lld`) must survive so the normal per-child eligibility
        loop picks it back up once the epic's re-gate merges."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=PIPELINE_STATUS_FIELD_ID))

    def add_sub_issue(self, parent_number: int, child_number: int):
        parent_id = self.issue_node_id(parent_number)
        child_id = self.issue_node_id(child_number)
        self.graphql(_ADD_SUB_ISSUE_MUTATION.format(parent_id=parent_id, child_id=child_id))

    def issue_comment(self, number: int, body: str):
        self._run(["gh", "issue", "comment", str(number), "--repo", self.repo, "--body", body])

    def issue_close(self, number: int):
        self._run(["gh", "issue", "close", str(number), "--repo", self.repo,
                   "--reason", "completed"])

    def pr_comment(self, number: int, body: str):
        self._run(["gh", "pr", "comment", str(number), "--repo", self.repo, "--body", body])

    def closed_issue_count(self) -> int:
        """Exact repo-wide closed-issue count via the search API's total_count --
        replaces a `gh issue list --limit 200` length check, which silently
        pegged at 200 once the repo outgrew the cap (making any modulo-based
        retro trigger fire forever)."""
        out = self._run(["gh", "api",
                          f"search/issues?q=repo:{self.repo}+type:issue+state:closed&per_page=1",
                          "--jq", ".total_count"])
        return int(out.strip())

    def issue_create(self, title: str, body: str, labels: list) -> int:
        argv = ["gh", "api", f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        for l in labels:
            argv += ["-f", f"labels[]={l}"]
        out = self._run(argv)
        return json.loads(out)["number"]

    def pr_view(self, number: int, fields: str = "state,mergedAt") -> dict:
        out = self._run(["gh", "pr", "view", str(number), "--repo", self.repo, "--json", fields])
        return json.loads(out)

    def path_on_ref(self, path: str, ref: str = "main") -> bool:
        """Whether `path` exists on branch `ref`, checked GitHub-side so it is
        correct regardless of what any local checkout or worktree currently holds.

        Backs the "docs are on the epic branch" item in `cmd_check_epics_closeable`
        (`ref=epic-<n>`). An epic's `architecture.md` that never merged off its
        gate sub-branch is reachable only by SHA, and a SHA quoted from an old
        comment can resolve to a superseded draft -- which is exactly how #209's
        `pr-review` raised a citation finding against an unreachable commit that
        `development` then had to disprove twice (2026-08-20, see
        references/history.md). Since 2026-09-06 the epic branch, not `main`, is
        where a merged epic gate lands the doc, so that is the ref checked for an
        open epic; `close-epic`'s final merge is what carries it to `main`."""
        try:
            self._run(["gh", "api", f"repos/{self.repo}/contents/{path}?ref={ref}",
                       "--jq", ".sha"])
            return True
        except GhError:
            return False

    def pr_list_for_branch(self, branch: str, state: str = "open") -> list:
        """Open PRs whose head branch is `branch` -- how `cmd_list_ready_for_review`
        finds an issue's development PR without parsing it out of a comment's
        prose. `gh pr list --head` is exact-match on the branch name, so this is
        at most one PR in this pipeline's convention (one `issue-<n>` branch, one
        dev PR)."""
        out = self._run(["gh", "pr", "list", "--repo", self.repo, "--head", branch,
                          "--state", state, "--json", "number,isDraft,headRefName,title,url"])
        return json.loads(out)

    def pr_create(self, base: str, head: str, title: str, body: str, draft: bool = False) -> int:
        argv = ["gh", "pr", "create", "--repo", self.repo, "--base", base, "--head", head,
                "--title", title, "--body", body]
        if draft:
            argv.append("--draft")
        out = self._run(argv)
        url = out.strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def pr_checks(self, number: int) -> list:
        try:
            out = self._run(["gh", "pr", "checks", str(number), "--repo", self.repo,
                              "--json", "name,state,bucket,link,workflow"])
        except GhError as e:
            # `gh pr checks` errors out (rather than returning `[]`) when nothing has ever
            # reported against this PR's head SHA at all -- e.g. a docs-only PR where every
            # workflow's `paths:` filter genuinely excludes every changed file. Treat that
            # specific case as zero checks, same as checks_status() already does; a
            # code-touching PR that lands here because its required workflow never
            # triggered is caught downstream by merge_gate_status()'s missing-checks path,
            # not by this branch.
            if "no checks reported" in str(e):
                return []
            raise
        return json.loads(out)

    def pr_files(self, number: int) -> list:
        """Changed paths for a PR, via the paginated REST `pulls/{n}/files` endpoint.
        Two other approaches were tried and rejected, each with its own cap:
        `gh pr view --json files` is backed by a GraphQL connection capped at 100
        entries with no pagination (confirmed against the gh 2.76.1 binary) -- a
        >100-file PR silently drops paths past the cap. `gh pr diff --name-only` has
        no entry-count cap, but the diff itself 406s past 20,000 lines (confirmed:
        `gh pr diff 128276 --repo kubernetes/kubernetes --name-only` -> `HTTP 406:
        ... diff exceeded the maximum number of lines (20000)`) -- reachable here,
        since a backend package-lock.json alone can be ~12k lines, so a single
        lockfile regen in a PR would trip it. `gh api --paginate` walks the REST
        endpoint page by page and has no line-count or fixed-entry cap of its own
        (practically unbounded for a repo this size) -- confirmed to return all
        187/187 files on a PR where both prior approaches failed. It also sidesteps
        `gh pr diff`'s diff-header parsing, which can silently mis-parse C-quoted
        non-ASCII paths and rename-source paths.

        Since this feeds merge_gate_status() -- the sole merge gate in this repo,
        GitHub branch protection being unavailable -- an incomplete file list here
        means a code-touching PR could read as untouched and merge with no CI at
        all."""
        out = self._run(["gh", "api", "--paginate", f"repos/{self.repo}/pulls/{number}/files",
                          "--jq", ".[].filename"])
        return [line for line in out.splitlines() if line.strip()]

    def branch_behind_by(self, head: str, base: str = "main") -> int:
        """How many commits `base` has that `head` lacks, via the REST compare
        endpoint -- the mergeability freshness check `cmd_merge_pr` runs before
        squash-merging. GitHub-side (no local git needed), so it's correct
        regardless of which worktree/checkout state any local repo is in."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                          "--jq", ".behind_by"])
        return int(out.strip())

    def branch_ahead_by(self, head: str, base: str = "main") -> Optional[int]:
        """How many commits `head` has that `base` lacks -- the mirror of
        `branch_behind_by`, same REST compare call, other direction. `None` when
        either ref is absent from origin (the compare 404s), which is a real
        answer to the only question asked of it: `cmd_open_gate` uses it to prove
        an epic's gate doc reached its sub-branch, and "the sub-branch was never
        pushed" and "it carries no commits" are the same defect with the same
        fix. Added 2026-09-06 retro."""
        try:
            out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                              "--jq", ".ahead_by"])
        except GhError:
            return None
        return int(out.strip())

    def pr_ready(self, number: int):
        self._run(["gh", "pr", "ready", str(number), "--repo", self.repo])

    def pr_merge(self, number: int):
        self._run(["gh", "pr", "merge", str(number), "--repo", self.repo, "--squash", "--delete-branch"])

    def graphql(self, query: str) -> dict:
        out = self._run(["gh", "api", "graphql", "-f", f"query={query}"])
        return json.loads(out)["data"]


def label_names(issue: dict) -> set:
    return {l["name"] for l in issue.get("labels", [])}


def has_label(issue: dict, name: str) -> bool:
    return name in label_names(issue)


def current_stage(issue: dict) -> Optional[str]:
    """Reads the native "Stage" issue field -- stage:* labels were retired
    2026-08-16; this field is now the sole source of truth. `issue["fields"]` is
    populated by issue_list()/issue_fields() from the same generic
    IssueFieldSingleSelectValue shape Priority/Effort already use."""
    return STAGE_FIELD_NAMES.get(issue.get("fields", {}).get("Stage"))


def pipeline_status(issue: dict) -> Optional[str]:
    """Reads the native "Pipeline Status" issue field -- replaces the old
    status:in-progress/needs-human/awaiting-human-review labels (retired
    2026-08-16). None means no status set (e.g. a fresh, unclaimed issue)."""
    return PIPELINE_STATUS_FIELD_NAMES.get(issue.get("fields", {}).get("Pipeline Status"))


def issue_type(issue: dict) -> Optional[str]:
    t = issue.get("issueType")
    return t.get("name") if t else None


def is_epic(issue: dict) -> bool:
    """An Epic is a top-level (no parent) Feature-typed issue -- there is no
    separate "Epic" Issue Type; per operator instruction, Type: Feature *is* the
    epic taxonomy. See "Epic number is mandatory" in SKILL.md."""
    return issue_type(issue) == "Feature" and issue.get("parent") is None


def resolve_profile(epic_issue: Optional[dict]) -> dict:
    """Resolve an epic's behavioural profile from `pipeline.profiles`.

    Profiles are an ordered list; the first whose `match` holds against the epic's
    labels wins, and the `"*"` entry is the terminal catch-all. Toggles the matched
    profile omits inherit `_PROFILE_TOGGLE_DEFAULTS`; its `gates` inherit the global
    `pipeline.gates`. The returned dict always carries every toggle plus `name` and a
    complete `gates` block, so callers never have to guess a default.

    This replaced the old hardcoded `epic:standing`/`epic:legacy` label checks: the
    client now owns the label->behaviour mapping (it may use `epic:standing`, `RTB`,
    or any label it likes). See "Epic profiles" in references/epics.md."""
    labels = label_names(epic_issue) if epic_issue else set()
    chosen: dict = {}
    for prof in PIPELINE["profiles"]:
        match = prof.get("match")
        if match == "*":
            chosen = prof
            break
        if isinstance(match, dict) and match.get("label") in labels:
            chosen = prof
            break
    merged = {**_PROFILE_TOGGLE_DEFAULTS,
              **{k: v for k, v in chosen.items() if k not in ("match", "gates")}}
    merged["name"] = chosen.get("name", "default")
    merged["gates"] = {**PIPELINE["gates"], **(chosen.get("gates") or {})}
    return merged


def effective_gates(epic_issue: Optional[dict]) -> dict:
    """The resolved `gates` block (skipConfidenceThreshold, requiresHumanGateA) for
    the epic's profile -- convenience over `resolve_profile(...)["gates"]`."""
    return resolve_profile(epic_issue)["gates"]


def is_epic_standing(issue: dict) -> bool:
    """True for an epic that runs no epic-level Product/Architecture phase -- each
    child runs its own full flow instead. Now a thin read of the epic's profile
    (`epicLevelPhase == False`); the behaviour bundle lives in `pipeline.profiles`,
    not in this label check. See "Epic profiles" in references/epics.md."""
    return not resolve_profile(issue)["epicLevelPhase"]


def is_epic_legacy(issue: dict) -> bool:
    """True for an epic this pipeline no longer drives at all (profile `driven ==
    False`). `decide_next_action` checks this first and returns `action: "skip"` for
    the epic and every child -- no flow at all. The label->this-behaviour mapping is
    a client profile now, not a hardcoded `epic:legacy` check."""
    return not resolve_profile(issue)["driven"]


def is_epic_architected(issue: dict) -> bool:
    """True once an epic's own Product/Architecture phase (epic-level Gate A, then
    Gate B or its confidence-skip) has completed -- its children become eligible
    for `lld` onward only after this is true. Marked with the `epic:architected`
    label rather than a comment marker so `next-action` can read it straight off
    the bulk `issue_list()` fetch (which includes labels) instead of an extra
    per-epic `issue_view` call. See "Epic-level stages" in references/epics.md."""
    return has_label(issue, LABELS["architected"])


def default_stage(issue: dict, parent_epic: Optional[dict] = None) -> str:
    """Stage an unlabeled issue starts at.

    A child of a *normal* epic (not `epic:standing`) always starts at `lld` --
    its own dedicated Stage value (see "LLD is its own Stage value" in
    SKILL.md) -- since that epic's own Product/Architecture phase already
    covered requirements and design (see "Epic-level stages" in references/epics.md).
    Everything else keeps the original per-issue behavior: bug reports
    fast-track straight to `architecture` (skipping `product` unless
    `architecture` itself escalates back to it -- see "Bug fast-track"), every
    other issue starts at `product`.

    Never actually called with an `epic:legacy` parent in practice --
    `decide_next_action` skips a legacy epic and every one of its children
    before any Stage is ever assigned (see `is_epic_legacy`), so there is no
    behavior to preserve for that case here."""
    if parent_epic is not None and resolve_profile(parent_epic)["childEntryStage"] == "lld":
        return "lld"
    if issue_type(issue) == "Bug":
        return "architecture"
    return "product"


def product_gate_pending(all_issues: list) -> list:
    """Open issues, repo-wide, sitting at Stage=Product with an open Gate A --
    Pipeline Status in `GATE_PENDING_STATUSES` (`feedback-received` is a
    visibility flip on top of `awaiting-human-review`, still the same open gate).
    This is the human's product-review queue; `PRODUCT_WIP_CAP` bounds it.
    Repo-wide on purpose: the human reviewing Gate A PRs is one person across
    every epic, so an epic-scoped count would let N invocations each open five."""
    return sorted(i["number"] for i in all_issues
                  if i["state"] == "OPEN" and current_stage(i) == "product"
                  and pipeline_status(i) in GATE_PENDING_STATUSES)


def product_wip_headroom(all_issues: list) -> Optional[int]:
    """How many *fresh* `product` delegations may start before the Gate A queue
    hits `PRODUCT_WIP_CAP`; `None` when the cap is disabled (`<= 0`). Callers that
    start several units in one call (`list-design-ready`) decrement it per unit
    selected, so a single fan-out cannot overshoot the cap either."""
    cap = PRODUCT_WIP_CAP
    if not cap or cap <= 0:
        return None
    return max(0, cap - len(product_gate_pending(all_issues)))


def priority_rank(issue: dict) -> int:
    """Reads the native GitHub "Priority" issue field (Urgent/High/Medium/Low) --
    replaces the old priority:pX label. No value set = rank 2 (Medium), same
    default the old "no priority label = p2" rule used."""
    return PRIORITY_RANK.get(issue.get("fields", {}).get("Priority"), 2)


def parse_created_at(issue: dict) -> datetime:
    return datetime.strptime(issue["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sort_key(issue: dict) -> tuple:
    return (priority_rank(issue), parse_created_at(issue))




_GATE_PR_MARKER = re.compile(r"<!--\s*gate-pr:\s*(\w+):(\d+)\s*-->")
_GATE_CUTOFF_MARKER = re.compile(r"<!--\s*gate-comments-processed:\s*([^\s]+)\s*-->")
_NEEDS_HUMAN_REASON = re.compile(r"🙋 Needs human input — (.+?)(?:\n\n<!--|\Z)", re.DOTALL)
# The generic per-stage handoff marker every stage's exit comment ends with (see
# "Stage-specific exit actions" in references/stage-playbooks.md): `<!-- stage-transition: <from>-><to>
# @ <ISO8601> -->`. The timestamp is optional here purely for robustness -- every
# writer emits one today.
_STAGE_TRANSITION_MARKER = re.compile(r"<!--\s*stage-transition:\s*(\S+?)->(\S+?)\s*(?:@[^>]*?)?-->")
# Records that a `pr-review` pass actually ran and what it concluded --
# `<!-- pr-review-outcome: clean|rework:<pr> @ <ISO8601> -->`, posted by
# `record-pr-review`. Same comment-marker convention as `gate-pr` above. This is
# what stops `list-ready-for-review` from handing the same PR to a second review
# agent: an issue sits at Stage = Testing / Pipeline Status = In Progress both
# before *and* during rework, so neither field can distinguish "awaiting review"
# from "already reviewed, dev is fixing it". See "Parallel PR review" in references/parallelism.md.
_PR_REVIEW_OUTCOME_MARKER = re.compile(r"<!--\s*pr-review-outcome:\s*(\w+):(\d+)\s*(?:@[^>]*?)?-->")
# Posted by `cmd_sync_branch` when reconciling with origin/main hit real unmerged
# paths -- persists the sync-branch-conflict <-> development escalation-valve
# pairing on the issue thread so `cmd_pairing_counts` (and a fresh session) can
# reconstruct the strike count instead of it living only in one session's memory.
# Closing-verification evidence, posted by the two closing runs on an epic:
# `<!-- epic-verification: e2e|exploratory:<epic> @ <ISO8601> -->`. `close-epic`
# refuses to merge the integration branch until both are present *and* newer than
# the last `origin/main` reconcile of the epic branch -- evidence gathered before
# the final merge proves nothing about the tree that actually ships.
_EPIC_VERIFICATION_MARKER = re.compile(r"<!--\s*epic-verification:\s*(\w+):(\d+)\s*(?:@[^>]*?)?-->")

_EPIC_RECONCILED_MARKER = re.compile(r"<!--\s*epic-reconciled:\s*(\d+)\s*(?:@[^>]*?)?-->")

_SYNC_CONFLICT_MARKER = re.compile(r"<!--\s*sync-conflict:\s*(\S+)\s*(?:@[^>]*?)?-->")

# Records that a design review ran and what it concluded --
# `<!-- design-review-outcome: clean|rework:<role> @ <ISO8601> -->`, posted by
# `record-design-review`. Added 2026-08-28 out of epic #98's retrospective: the
# `lld-review <-> lld` pairing consumed roughly nine tenths of that epic's review
# rounds and tripped the valve's context-reset replacement on three of four
# children -- while being the one high-traffic pairing with **no** mechanical
# counter, since `pairing-counts` covered only `pr-review` and sync conflicts. The
# strike count lived solely in one orchestrator's head, so a crashed session (or
# any continuous-mode agent, which has no memory of prior units by design) would
# have resumed with it silently reset to zero, leaving the valve unenforceable
# exactly where it fires most.
_DESIGN_REVIEW_OUTCOME_MARKER = re.compile(
    r"<!--\s*design-review-outcome:\s*(\w+):(\S+?)\s*(?:@[^>]*?)?-->")

# Local-CI attestation, added 2026-09-04 with the main-only-CI cost cut. The
# backend/frontend suites no longer run on child PRs in GitHub Actions (they run
# only on push to `main`; see each workflow's `on:` block) -- but they remain
# MANDATORY for a child PR to merge. The proof just moves from a GHA check to the
# suite the `development` stage runs locally, attested here as
# `<!-- local-ci: <suite>:<pr> @ <sha> -->` by `record-local-ci`, and consumed by
# `missing_required_workflows`. `<sha>` is the exact commit the suite ran against
# (the development worktree's HEAD). `merge-pr` accepts the attestation ONLY when that
# sha matches the PR's current head: an attestation from before the last push
# describes a different tree, so it is treated as absent -- the same freshness
# principle as the behind-base merge gate and every ordering check in this file. A
# rework round that adds a commit therefore invalidates the old attestation and
# forces `development` to re-run and re-attest, which is exactly the intent.
# The attestation carries the run's own captured output, not a reported number:
# `development` both writes and validates its own tests since the `testing` stage
# was merged into it (2026-09-12), so the only thing standing between a claim and
# the merge gate is that the evidence is machine-produced and sha-pinned.
_LOCAL_CI_MARKER = re.compile(
    r"<!--\s*local-ci:\s*(\w+):(\d+)\s*@\s*([0-9a-fA-F]{7,40})\s*-->")

# Suite keys a `local-ci` attestation may carry, one per required GHA workflow that
# went main-only. Kept in step with the `suite` field on REQUIRED_WORKFLOWS below.
LOCAL_CI_SUITES = tuple(dict.fromkeys(w["suite"] for w in CONFIG["requiredWorkflows"]))


def local_ci_suites_attested(comments: list, head_sha: str) -> set:
    """Suite keys (subset of LOCAL_CI_SUITES) whose thread carries a `local-ci`
    attestation whose sha matches `head_sha`.

    Empty when `head_sha` is falsy (unknown head -> trust nothing) or no marker
    matches it. Matching is prefix-tolerant in both directions so a short-sha
    attestation (`git rev-parse --short`) still matches a full 40-char head, and
    vice versa -- but a *different* commit never matches, which is the whole
    point: a stale local run must not satisfy the gate."""
    if not head_sha:
        return set()
    head = head_sha.lower()
    attested = set()
    for c in comments:
        for m in _LOCAL_CI_MARKER.finditer(c.get("body", "")):
            suite, sha = m.group(1), m.group(3).lower()
            if head.startswith(sha) or sha.startswith(head):
                attested.add(suite)
    return attested

_UNRESOLVED_THREADS_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  pullRequest(number: {pr}) {{
    reviewThreads(first: 50) {{ nodes {{ id isResolved comments(first:10){{ nodes{{ body author{{ login }} }} }} }} }}
  }}
}} }}"""

_ISSUE_LIST_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issues(first: 100, after: {after}, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      number title body createdAt state
      labels(first: 20) {{ nodes {{ name }} }}
      issueType {{ name }}
      parent {{ number }}
      milestone {{ title }}
      issueFieldValues(first: 10) {{
        nodes {{
          __typename
          ... on IssueFieldSingleSelectValue {{ field {{ ... on IssueFieldSingleSelect {{ name }} }} name }}
        }}
      }}
    }}
  }}
}} }}"""

_ISSUE_NODE_ID_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ id }}
}} }}"""

_ISSUE_FIELDS_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{
    issueFieldValues(first: 10) {{
      nodes {{
        __typename
        ... on IssueFieldSingleSelectValue {{ field {{ ... on IssueFieldSingleSelect {{ name }} }} name }}
      }}
    }}
  }}
}} }}"""

_ISSUE_EPIC_CHECK_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{
    issueType {{ name }}
    parent {{ number }}
    labels(first: 20) {{ nodes {{ name }} }}
  }}
}} }}"""

_ISSUE_PROJECT_ITEM_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{
    projectItems(first: 10) {{ nodes {{ id project {{ number }} }} }}
  }}
}} }}"""

_SET_PROJECT_STATUS_MUTATION = """mutation {{
  updateProjectV2ItemFieldValue(input: {{
    projectId: "{project_id}"
    itemId: "{item_id}"
    fieldId: "{field_id}"
    value: {{ singleSelectOptionId: "{option_id}" }}
  }}) {{
    projectV2Item {{ id }}
  }}
}}"""

_BLOCKED_BY_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ blockedBy(first: 20) {{ nodes {{ number state }} }} }}
}} }}"""

_BLOCKING_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ blocking(first: 20) {{ nodes {{ number state }} }} }}
}} }}"""

_ADD_BLOCKED_BY_MUTATION = """mutation {{
  addBlockedBy(input: {{issueId: "{issue_id}", blockingIssueId: "{blocking_id}"}}) {{
    issue {{ number }}
  }}
}}"""

_SET_ISSUE_TYPE_MUTATION = """mutation {{
  updateIssueIssueType(input: {{issueId: "{issue_id}", issueTypeId: "{type_id}"}}) {{
    issue {{ number }}
  }}
}}"""

_SET_ISSUE_FIELD_MUTATION = """mutation {{
  updateIssueFieldValue(input: {{
    issueId: "{issue_id}"
    issueField: {{ fieldId: "{field_id}" singleSelectOptionId: "{option_id}" }}
  }}) {{
    issue {{ number }}
  }}
}}"""

_DELETE_ISSUE_FIELD_VALUE_MUTATION = """mutation {{
  deleteIssueFieldValue(input: {{ issueId: "{issue_id}" fieldId: "{field_id}" }}) {{
    issue {{ number }}
  }}
}}"""

_ADD_SUB_ISSUE_MUTATION = """mutation {{
  addSubIssue(input: {{issueId: "{parent_id}", subIssueId: "{child_id}"}}) {{
    subIssue {{ number }}
  }}
}}"""

# gh's `api graphql` names the repository inline (no `--repo` flag), so the query
# templates above carry `__OWNER__`/`__NAME__` sentinels that resolve here, once,
# from the configured repo -- keeping the templates literal-free while the later
# `.format(n=...)` placeholders are left untouched.
for _qname, _qval in list(globals().items()):
    if isinstance(_qval, str) and "__OWNER__" in _qval:
        globals()[_qname] = _qval.replace("__OWNER__", _OWNER).replace("__NAME__", _NAME)


def find_gate_pr(comments: list) -> Optional[tuple]:
    stage = pr = None
    for c in comments:
        m = _GATE_PR_MARKER.search(c.get("body", ""))
        if m:
            stage, pr = m.group(1), int(m.group(2))
    return (stage, pr) if pr is not None else None


REVIEW_ROLES = frozenset({"product-review", "arch-review", "lld-review", "pr-review"})


def epic_branch(epic: int) -> str:
    """The long-lived integration branch for a normal epic.

    Cut from `origin/main` when the epic starts and never committed to directly
    -- it only ever receives merges: the gate branches carrying its own
    `product.md`/`architecture.md`, then each child's `issue-<n>`. At epic close
    it takes one merge *from* `origin/main`, gets verified as a whole, and is
    merged to `main` as a single integration."""
    return f"{EPIC_BRANCH_PREFIX}{epic}"


def epic_gate_branch(epic: int, stage: str) -> str:
    """The short-lived sub-branch an epic-level gate doc is authored on (default
    `epic-<n>-gate-<stage>`, e.g. `epic-5-gate-product`), cut from
    `origin/epic-<n>`. `open-gate --unit epic` opens it against `epic-<n>`, the
    human merges it (squash is fine -- the sub-branch is disposable), and the doc
    lands on the epic branch; `epic-<n>` itself only ever reaches `main` at
    `close-epic`, unsquashed. Decided 2026-09-06 (references/history.md) --
    before that an epic's gate PR went `epic-<n>` -> `main` directly. A
    standing-epic child's gate still runs on `issue-<n>` -> `main`."""
    return f"{epic_branch(epic)}{GATE_BRANCH_SUFFIX}{stage}"


def integration_base(gh: "GitHub", issue: int, unit: str = "issue") -> str:
    """Which branch this unit's work integrates into.

    A child of a normal epic integrates into that epic's branch, so sibling
    conflicts surface once, at epic close, against a tree where every sibling is
    already present -- rather than N times against a moving `main`.

    Two cases integrate straight into `main` instead, and neither is a
    compatibility hedge:

    * **A standing epic's children** (`epic:standing`, e.g. a standing backlog epic). A standing
      epic never closes, so its integration branch would never merge and would
      diverge from `main` without bound.
    * **A top-level issue with no parent epic.** Nothing to integrate into.

    An epic's *own* unit resolves to `main`: the epic branch is what merges
    there at close."""
    if unit == "epic":
        return "main"
    # `parent` comes from `issue_list`'s GraphQL, never from `issue_view` -- `gh
    # issue view --json` has no such field, and asking for one is a hard error.
    # The first cut of this resolver read `issue_view(...).get("parent")`, which
    # is always absent, so every child silently resolved to `main` -- a failure
    # in the direction that looks correct, caught only by running it against real
    # issues whose parent was known.
    issues = {i["number"]: i for i in gh.issue_list()}
    entry = issues.get(issue)
    if entry is None:
        raise GhError(f"issue #{issue} not found in the repo issue list")
    parent = entry.get("parent")
    if not parent:
        return "main"
    parent_number = parent["number"]
    parent_entry = issues.get(parent_number)
    if parent_entry is not None and is_epic_standing(parent_entry):
        return "main"
    return epic_branch(parent_number)


def last_transition_to(comments: list, to_role: str) -> Optional[int]:
    """Index (in `comments`, which GitHub returns oldest-first) of the most recent
    comment carrying a `<!-- stage-transition: ...-><to_role> ... -->` marker, or
    None if that handoff never happened. Index rather than timestamp because the
    only question asked of it is ordering *within one issue's own thread* -- see
    `cmd_list_ready_for_review`.

    Matched on the **destination role only**, and on the last `->`-separated
    segment of it. `handoff-to-pr-review` writes the canonical
    `development->pr-review` form, but this marker line has historically been
    hand-written by each stage's own agent, and the real thread history shows the
    from-role is not reliable: issue #115 posted `development->pr-review` and
    #111 posted a mangled `pr-review->development->pr-review` for the same
    handoff. Keying on the destination is what those all genuinely agree on."""
    found = None
    for idx, c in enumerate(comments):
        for m in _STAGE_TRANSITION_MARKER.finditer(c.get("body", "")):
            if m.group(2).rsplit("->", 1)[-1] == to_role:
                found = idx
    return found


def last_pr_review_outcome(comments: list) -> Optional[tuple]:
    """`(comment_index, outcome, pr_number)` for the most recent recorded
    `pr-review` outcome on this issue, or None if no review has ever been
    recorded. See `_PR_REVIEW_OUTCOME_MARKER` / `cmd_record_pr_review`."""
    found = None
    for idx, c in enumerate(comments):
        m = _PR_REVIEW_OUTCOME_MARKER.search(c.get("body", ""))
        if m:
            found = (idx, m.group(1), int(m.group(2)))
    return found


def missing_pipeline_evidence(comments: list) -> list:
    """Which of the two mandatory pre-merge markers this issue's thread is missing.

    Empty list means the thread proves the pipeline actually ran. This is the
    **only** hard gate on stage evidence, and it sits at `merge-pr` deliberately:
    merge is the single irreversible act, so a refusal here cannot be routed
    around by prompt wording, by an agent that skipped its exit action, or by an
    orchestrator that read past a warning. Everything upstream is recoverable.

    Both failure modes it catches are real and have happened:

    * A stage agent returned its verdict to the orchestrator instead of posting
      it, because a dispatch prompt ended with "Return PASS or REJECT" and the
      agent obeyed the more recent instruction over its own definition. Issues
      #237 and #254 reached `pr-review` this way (from the since-retired
      `testing` stage). `verify-exit` had already reported
      `expected_stage_present: false` and was read past -- which is exactly why
      advisory output is not enough here.
    * A `pr-review` that never recorded an outcome leaves no evidence the last
      gate before auto-merge ever ran.
    * A recorded outcome of `rework` means the last gate said *do not merge*.
      The first draft of this very function checked only that an outcome
      *existed* and reported MERGE ALLOWED for an issue whose review had just
      returned REWORK -- modelling the rule instead of running it, which is the
      exact defect class this epic's retro is about.

    Ordering matters as much as presence: an outcome recorded *before* the latest
    `development->pr-review` handoff belongs to a previous round, and a rework
    round that re-handed-off without a fresh review would otherwise merge on a
    stale clean verdict. Same index-ordering rule `cmd_list_ready_for_review` uses."""
    problems = []
    handoff = last_transition_to(comments, "pr-review")
    if handoff is None:
        problems.append("no `development->pr-review` handoff marker (run "
                        "`handoff-to-pr-review <issue> --pr <pr> --summary ...`)")
    outcome = last_pr_review_outcome(comments)
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


def missing_epic_verification(comments: list) -> list:
    """Which closing-verification evidence an epic's thread is still missing.

    Two runs are required and they are deliberately different in kind: the full
    **e2e** suite, and an **exploratory** pass that goes looking for what a
    scripted suite cannot -- both run against the integration branch *after*
    `origin/main` has been merged into it. Per-child `testing` only ever proved
    the surfaces that child moved; nothing before this point has exercised every
    sibling together against the tree that actually ships.

    Ordering is the substance of the check, not bookkeeping. Evidence posted
    *before* the last `origin/main` reconcile describes a different tree, so it
    is treated as absent. This is the same trap `missing_pipeline_evidence`
    guards at the child level, one altitude up."""
    problems = []
    reconciled = None
    for idx, c in enumerate(comments):
        if _EPIC_RECONCILED_MARKER.search(c.get("body", "")):
            reconciled = idx
    seen = {}
    for idx, c in enumerate(comments):
        m = _EPIC_VERIFICATION_MARKER.search(c.get("body", ""))
        if m:
            seen[m.group(1)] = idx
    for kind, label in (("e2e", "full e2e suite"),
                         ("exploratory", "exploratory pass")):
        if kind not in seen:
            problems.append(f"no `{kind}` closing-verification evidence ({label} never recorded)")
        elif reconciled is not None and seen[kind] < reconciled:
            problems.append(f"the `{kind}` evidence predates the last `origin/main` reconcile of "
                            f"the epic branch -- it describes a different tree; re-run it")
    return problems


def cmd_record_epic_verification(gh: GitHub, epic: int, kind: str, summary: str) -> dict:
    """Post one half of an epic's closing verification as durable thread evidence.

    Never hand-type the marker -- `close-epic` reads it back, and an evidence
    line that lives only in a session's memory reads to the next session as a
    run that never happened."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    label = "Full e2e suite" if kind == "e2e" else "Exploratory pass"
    gh.issue_comment(epic,
        f"🧪 {label} — closing verification for #{epic}. {summary}\n\n"
        f"<!-- epic-verification: {kind}:{epic} @ {timestamp} -->")
    return {"epic": epic, "kind": kind, "recorded": True}


def cmd_close_epic(gh: GitHub, epic: int, repo_path: str = ".",
                    runner: Runner = _default_runner) -> dict:
    """Reconcile the epic's integration branch with `main`, then merge it -- the
    single integration point the whole branching model exists for.

    Deliberately two calls, not one. The first run reconciles and stops, because
    verification has to happen *after* the merge from `main` and *before* the
    merge to `main`; the second run merges once the evidence is on the thread.
    Every refusal is a structured exit-0 result, never an exception -- "not ready
    yet" is the normal state, not an operational failure."""
    detail = gh.issue_view(epic)
    if not resolve_profile(detail)["closes"]:
        return {"epic": epic, "merged": False,
                "reason": f"epic profile '{resolve_profile(detail)['name']}' does not close "
                          "(closes: false) -- no integration branch to merge"}
    branch = epic_branch(epic)
    all_issues = gh.issue_list()
    children = [i for i in all_issues if i.get("parent") and i["parent"]["number"] == epic]
    open_children = [c["number"] for c in children if c["state"] != "CLOSED"]
    if open_children:
        return {"epic": epic, "merged": False, "open_children": open_children,
                "reason": f"{len(open_children)} child issue(s) still open: "
                          f"{', '.join(f'#{n}' for n in open_children)}"}
    behind = gh.branch_behind_by(branch, base="main")
    if behind:
        # The epic branch rarely has a live worktree by close time; reconcile in
        # an ephemeral one under the branch lock, never in the main checkout.
        with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
            git_reconcile_branch(ws.path, branch, base="main", runner=runner)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        gh.issue_comment(epic,
            f"🔄 Reconciled `{branch}` with `origin/main` ({behind} commit(s) picked up). "
            f"Closing verification must now run against **this** tree — the full e2e suite and "
            f"an exploratory pass, in parallel. Evidence recorded before this point describes a "
            f"different tree and will not be accepted.\n\n"
            f"<!-- epic-reconciled: {epic} @ {timestamp} -->")
        return _with_workspace(
            {"epic": epic, "merged": False, "branch": branch, "reconciled": behind,
             "reason": f"picked up {behind} commit(s) from main -- run the closing "
                       f"verification against the reconciled branch, then re-run close-epic"},
            ws)
    missing = missing_epic_verification(gh.issue_view(epic).get("comments", []))
    if missing:
        return {"epic": epic, "merged": False, "branch": branch, "missing_verification": missing,
                "reason": f"closing verification incomplete: {'; '.join(missing)}"}
    existing = gh.pr_list_for_branch(branch)
    if existing:
        pr_number = existing[0]["number"]
    else:
        pr_number = gh.pr_create(
            base="main", head=branch,
            title=f"{detail['title']} — epic integration (#{epic})",
            body=f"Integration of every child of #{epic}.\n\nCloses #{epic}",
            draft=True)
    checks = gh.pr_checks(pr_number)
    # A required suite (backend/frontend) went main-only for cost, so it never runs
    # on this epic->main PR pre-merge -- it is satisfied by a fresh `local-ci`
    # attestation instead. Read the PR's comments + head SHA so the merge gate can
    # honour it, exactly as `cmd_pr_checks`/`merge-pr` do (see `local_ci_suites_attested`).
    view = gh.pr_view(pr_number, "comments,headRefOid")
    status, missing_checks = merge_gate_status(
        gh.pr_files(pr_number), checks,
        view.get("comments", []), view.get("headRefOid"))
    if status != "passed":
        detail_msg = f" (no passing check from: {', '.join(missing_checks)})" if missing_checks else ""
        return {"epic": epic, "merged": False, "pr": pr_number, "checks": status,
                "reason": f"PR #{pr_number} checks not passed (status={status}){detail_msg}"}
    gh.pr_ready(pr_number)
    gh.pr_merge(pr_number)
    return {"epic": epic, "merged": True, "pr": pr_number, "branch": branch}


def find_gate_comments_cutoff(pr: dict) -> str:
    cutoff = pr["createdAt"]
    for c in pr.get("comments", []):
        m = _GATE_CUTOFF_MARKER.search(c.get("body", ""))
        if m:
            cutoff = c["createdAt"]
    return cutoff


def new_plain_comments(pr: dict, cutoff: str) -> list:
    return [c for c in pr.get("comments", []) if c["createdAt"] > cutoff]


def unresolved_review_threads(gh: GitHub, pr_number: int) -> list:
    data = gh.graphql(_UNRESOLVED_THREADS_QUERY.format(pr=pr_number))
    nodes = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return [n for n in nodes if not n["isResolved"]]


def evaluate_gate(gh: GitHub, issue_number: int) -> dict:
    issue = gh.issue_view(issue_number)
    found = find_gate_pr(issue["comments"])
    if not found:
        raise GhError(f"issue #{issue_number} has Pipeline Status = awaiting-human-review but no gate-pr marker")
    stage, pr_number = found
    pr_state = gh.pr_view(pr_number, fields="state,mergedAt")
    if pr_state["state"] == "MERGED":
        return {"gate_pr": pr_number, "stage": stage, "status": "satisfied",
                "unresolved_threads": [], "new_comments": []}
    threads = unresolved_review_threads(gh, pr_number)
    pr = gh.pr_view(pr_number, fields="comments,createdAt")
    cutoff = find_gate_comments_cutoff(pr)
    comments = new_plain_comments(pr, cutoff)
    status = "feedback_pending" if (threads or comments) else "not_satisfied"
    return {"gate_pr": pr_number, "stage": stage, "status": status,
            "unresolved_threads": threads, "new_comments": comments}


def cmd_check_gate(gh: GitHub, args) -> dict:
    return evaluate_gate(gh, args.issue)


def decide_next_action(gh: GitHub, epic: int) -> dict:
    """Picks the one unit -- the named epic's own Product/Architecture phase, or
    one of its children -- to work next. `epic` is now a **required** argument
    (see "Epic number is mandatory" in SKILL.md, added 2026-08-20 per operator
    instruction): the operator names which epic to drive end-to-end, and this
    function never looks outside that epic's own tree. This replaced an earlier
    model where the picker scanned every open epic in the repo and ranked them
    against each other (started-epics-first, then Priority, then age) to decide
    which epic's turn it was -- that cross-epic ranking, and the milestone
    scoping that existed purely to keep it from reaching into a not-yet-triaged
    epic, are both gone; naming the epic directly makes both moot.

    Raises `GhError` if `epic` isn't actually a top-level Feature-typed issue
    (see `is_epic`) -- passing a child issue number here is a caller mistake,
    not a legitimate "nothing to do" outcome.

    An `epic:legacy` epic is checked first, before anything else -- including
    crash-recovery -- and short-circuits to `action: "skip"`: this pipeline no
    longer drives that epic or any of its children at all, in any flow. See
    `is_epic_legacy` and "Which epics are exempt" in references/epics.md.

    Otherwise, crash-recovery (Pipeline Status = in-progress) is checked first,
    scoped to the epic itself plus its own open children only. Then: if the
    epic is a normal epic (not `epic:standing`) and open, its own human-review
    gate (if pending) or its own Product/Architecture phase (if not yet
    `epic:architected`) is the candidate unit -- exactly the same gate/
    epic-self logic this always had, just never compared against a second
    epic's candidacy anymore. Once the epic itself has nothing further to offer
    (already architected with no pending gate, or a standing epic that never
    runs this phase at all), its own open children are ranked by `sort_key`
    (Priority, then age) and the first eligible one is picked -- same per-child
    eligibility rules as before (needs-human skip, gate check, blocked check,
    Stage-field-assignment side effect on first sight).

    Every result dict carries a `unit` field ("issue" or "epic"), except
    `action: "skip"` which carries neither -- there is no unit to act on.
    `action: "none"` means this epic genuinely has nothing actionable left
    right now -- not that the whole repo is drained; a different epic may
    still have plenty to do, but this function doesn't know or care, by
    design.

    **Product WIP cap.** A *fresh* `product` delegation (epic-self or child, not
    a resume) is skipped -- the loop moves on to the next actionable unit --
    while `product_gate_pending` is at `PRODUCT_WIP_CAP` (`pipeline.productWip.
    maxGateAPending`, default 5). A `none` reached that way carries
    `product_cap` naming the deferred units, so the orchestrator's report can
    say why nothing started. Gate actions (`pass-gate`, `address-gate-feedback`)
    are what drain the queue and are never gated."""
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (a top-level Type: Feature issue) -- "
                       f"pass the epic's own issue number, not a child issue's")
    if is_epic_legacy(epic_issue):
        return {"action": "skip", "epic": epic,
                "reason": "epic:legacy -- this pipeline no longer drives this epic or any of "
                          "its children; skipping entirely."}

    children = [i for i in all_issues if i["state"] == "OPEN"
                and i.get("parent") and i["parent"]["number"] == epic]
    # A profile whose Gate A auto-passes (`requiresHumanGateA: false`) never puts a
    # unit in the human's queue, so its product delegations are never capped.
    product_headroom = (product_wip_headroom(all_issues)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    deferred_by_cap: list = []

    def capped(stage: str) -> bool:
        return stage == "product" and product_headroom is not None and product_headroom <= 0

    def none_result() -> dict:
        result = {"action": "none", "epic": epic}
        if deferred_by_cap:
            result["product_cap"] = {"limit": PRODUCT_WIP_CAP,
                                     "pending": product_gate_pending(all_issues),
                                     "deferred": deferred_by_cap}
        return result

    def unit_of(issue: dict) -> str:
        return "epic" if is_epic(issue) else "issue"

    # Crash-recovery: the epic itself (if open) plus its own open children only.
    in_progress = [i for i in ([epic_issue] if epic_issue["state"] == "OPEN" else []) + children
                   if pipeline_status(i) == "in-progress"]
    if in_progress:
        target = in_progress[0]
        return {"action": "resume", "issue": target["number"], "unit": unit_of(target),
                "stage": current_stage(target) or default_stage(target, epic_issue)}

    if epic_issue["state"] == "OPEN" and not is_epic_standing(epic_issue):
        status = pipeline_status(epic_issue)
        if status in GATE_PENDING_STATUSES:
            gate = evaluate_gate(gh, epic)
            if gate["status"] == "satisfied":
                return {"action": "pass-gate", "issue": epic, "unit": "epic",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"]}
            if gate["status"] == "feedback_pending":
                return {"action": "address-gate-feedback", "issue": epic, "unit": "epic",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"],
                        "unresolved_threads": gate["unresolved_threads"],
                        "new_comments": gate["new_comments"]}
        elif status != "needs-human" and not is_epic_architected(epic_issue):
            epic_stage = current_stage(epic_issue) or "product"
            if capped(epic_stage):
                deferred_by_cap.append(epic)
            elif not gh.blocked_by(epic):
                return {"action": "delegate", "issue": epic, "unit": "epic",
                        "stage": epic_stage}
        # else: epic-self work is done (architected, no pending gate),
        # needs-human, blocked on another epic, or deferred by the product cap --
        # fall through to children.

    if resolve_profile(epic_issue)["childrenNeedArchitectedEpic"] \
            and not is_epic_architected(epic_issue):
        # A profile whose children need an architected epic (the `default`) never lets
        # a child run before the epic itself is `epic:architected` -- their `lld` works
        # from the epic's approved architecture.md, which doesn't exist yet. This branch
        # is reached when epic-self can't advance right now (its gate is open with
        # nothing to address, it's needs-human, or it's blocked): that parks the whole
        # epic, not just the epic's own phase. Without this guard, those three
        # fall-through cases would delegate a child at `lld` against a design that
        # hasn't been written or approved.
        return none_result()

    for issue in sorted(children, key=sort_key):
        status = pipeline_status(issue)
        if status == "needs-human":
            continue
        if status in GATE_PENDING_STATUSES:
            gate = evaluate_gate(gh, issue["number"])
            if gate["status"] == "satisfied":
                return {"action": "pass-gate", "issue": issue["number"], "unit": "issue",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"]}
            if gate["status"] == "feedback_pending":
                return {"action": "address-gate-feedback", "issue": issue["number"], "unit": "issue",
                        "gate_pr": gate["gate_pr"], "stage": gate["stage"],
                        "unresolved_threads": gate["unresolved_threads"],
                        "new_comments": gate["new_comments"]}
            continue
        stage = current_stage(issue) or default_stage(issue, epic_issue)
        if capped(stage):
            # Checked before the blockedBy call and the Stage-assign write: a unit
            # the cap defers this pass gets no side effects at all.
            deferred_by_cap.append(issue["number"])
            continue
        if gh.blocked_by(issue["number"]):
            continue
        if current_stage(issue) is None:
            # Assign the Stage field the first time an eligible child is seen with
            # none set yet -- operator instruction: a new issue should show its
            # stage directly, not sit blank on the board until claim() eventually
            # picks it up.
            gh.set_stage_field(issue["number"], stage)
        return {"action": "delegate", "issue": issue["number"], "unit": "issue",
                "stage": stage}

    return none_result()


def cmd_next_action(gh: GitHub, args) -> dict:
    return decide_next_action(gh, args.epic)


def cmd_list_ready_for_review(gh: GitHub, epic: int, limit: Optional[int] = None) -> dict:
    """Every open child of `epic` whose development PR is sitting finished-and-
    unreviewed: Stage = `PR Review` (or the retired `Testing`, for an issue an
    in-flight epic stranded there), an open **draft** PR on its `issue-<n>`
    branch, `development`'s own
    `<!-- stage-transition: development->pr-review ... -->` handoff marker
    posted, and no `pr-review` outcome recorded *since* that
    marker. Returns up to `limit` (default `PR_REVIEW_PARALLELISM`) of them,
    each with the issue number, PR number and branch -- enough for the
    orchestrator to stand up one worktree per review. See "Parallel PR review"
    in SKILL.md.

    `epic` is required, same as `next-action` (see "Epic number is mandatory" in
    SKILL.md) -- the review pool this draws from is scoped to the one epic the
    operator is currently driving end-to-end, not the whole repo.

    The "since that marker" ordering is what makes rework rounds work: an issue
    that failed review goes back to `development` while its Stage stays
    `PR Review` and its Pipeline Status stays `In Progress`, so neither field can
    distinguish "awaiting review" from "already reviewed, being fixed". A recorded
    outcome newer than the latest `development->pr-review` handoff means this
    round's review already ran; a *later* handoff marker (posted when
    `development` re-hands-off the fix) makes it eligible again.

    Read-only: never claims, never posts. `skipped` reports every review-entry
    child of this epic that was excluded and why, so "my PR isn't listed" is
    answerable without re-deriving the filter by hand."""
    limit = PR_REVIEW_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]

    ready, skipped = [], []
    at_review_entry = [i for i in open_issues if not is_epic(i)
                       and current_stage(i) in REVIEW_ENTRY_STAGES
                       and i.get("parent") and i["parent"]["number"] == epic]
    for issue in sorted(at_review_entry, key=sort_key):
        number = issue["number"]
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        comments = gh.issue_view(number).get("comments", [])
        handoff = last_transition_to(comments, "pr-review")
        if handoff is None:
            skipped.append({"issue": number, "reason": "no development->pr-review handoff marker "
                                                        "yet -- development hasn't handed this "
                                                        "issue on"})
            continue
        outcome = last_pr_review_outcome(comments)
        if outcome is not None and outcome[0] > handoff:
            skipped.append({"issue": number, "reason": f"pr-review already recorded "
                                                        f"{outcome[1]!r} for this round"})
            continue
        branch = issue_branch(number)
        prs = gh.pr_list_for_branch(branch)
        if not prs:
            skipped.append({"issue": number, "reason": f"no open PR on {branch}"})
            continue
        pr = prs[0]
        if not pr.get("isDraft"):
            skipped.append({"issue": number, "reason": f"PR #{pr['number']} is no longer a draft "
                                                        f"-- already reviewed and marked ready"})
            continue
        ready.append({"issue": number, "pr": pr["number"], "branch": branch,
                       "title": issue["title"], "pr_url": pr.get("url")})

    return {"ready_for_review": ready[:limit], "count": len(ready[:limit]),
            "eligible_total": len(ready), "limit": limit, "epic": epic, "skipped": skipped}


# The optional `17. ` prefix: architecture.template.md numbers its sections, so the
# heading is `## 17. Footprint` there and a bare `## Footprint` in older docs. Both parse.
_FOOTPRINT_HEADING = re.compile(r"^#+\s*(?:\d+[.)]\s*)?Footprint\b.*$",
                                re.IGNORECASE | re.MULTILINE)
_FOOTPRINT_BULLET_PATH = re.compile(r"^-\s+`([^`]+)`")


def parse_footprint(doc_text: str) -> list:
    """Extracts the path list from a design doc's required '## Footprint'
    section -- every normal-epic child's `lld.md`, and every standing/legacy
    child's `architecture.md`, must already name its footprint as a plain
    bullet list near the top (see "How to size the children" in references/epics.md), so
    this reads existing required content rather than asking the architect/lld
    agent to maintain a second, structured artifact. Stops at the next
    markdown heading. Returns `[]` if no such section is found -- callers
    treat that as "cannot verify non-overlap, so not eligible", never as
    "no footprint declared, so no risk of overlap"."""
    m = _FOOTPRINT_HEADING.search(doc_text)
    if not m:
        return []
    rest = doc_text[m.end():]
    end = re.search(r"^#+\s", rest, re.MULTILINE)
    body = rest[:end.start()] if end else rest
    paths = []
    for line in body.splitlines():
        bm = _FOOTPRINT_BULLET_PATH.match(line.strip())
        if bm:
            paths.append(bm.group(1))
    return paths


def read_footprint(repo_path: str, issue: int, runner: Runner = _default_runner) -> list:
    """Reads issue #<issue>'s own declared footprint straight off its committed
    design doc on `origin/issue-<n>`, via `git show` against the shared
    checkout's remote-tracking ref -- deliberately not a worktree read, since
    computing parallel-eligibility must not itself require standing up a
    worktree first. Tries `lld.md` (a normal-epic child) then `architecture.md`
    (a standing/legacy child) -- whichever exists on that branch."""
    for doc in ("lld.md", "architecture.md"):
        try:
            text = runner(["git", "-C", repo_path, "show",
                            f"origin/{issue_branch(issue)}:{DOC_ROOT}/issue-{issue}/{doc}"])
        except GhError:
            continue
        footprint = parse_footprint(text)
        if footprint:
            return footprint
    return []


def _footprint_prefix(path: str) -> str:
    """Strips a trailing '**' or '*' glob suffix so overlap reduces to a plain
    path-prefix containment check -- every footprint declared in this repo's
    docs is either an exact file path or a directory-prefix glob (e.g.
    'backend/src/notifications/**'), never a mid-path wildcard, so
    prefix comparison is sufficient without pulling in real glob matching."""
    return path.rstrip("*").rstrip("/")


def footprint_overlaps(a: list, b: list) -> bool:
    a_prefixes = [_footprint_prefix(p) for p in a]
    b_prefixes = [_footprint_prefix(p) for p in b]
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/")
               for x in a_prefixes for y in b_prefixes)


def worktree_path_for_branch(branch: str, runner: Runner = _default_runner,
                              base_repo: str = ".") -> Optional[str]:
    """Filesystem path of the live git worktree that has `branch` checked out, or
    None. Same porcelain source as active_worktree_branches -- one
    `worktree <path>` line per worktree, followed by its `branch refs/heads/...`
    line when one is checked out."""
    out = runner(["git", "-C", base_repo, "worktree", "list", "--porcelain"])
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def worktree_path(unit: str, number: int) -> str:
    """Where the orchestrator keeps this unit's worktree, from `pipeline.worktrees`
    (default `/tmp/sdlc-dev-<n>` for a child, `/tmp/sdlc-epic-<n>` for an epic)."""
    w = PIPELINE["worktrees"]
    prefix = w["epicPrefix"] if unit == "epic" else w["devPrefix"]
    return os.path.join(w["root"], f"{prefix}{number}")


def init_skill_submodule(worktree: str, runner: Runner = _default_runner) -> dict:
    """Make `<worktree>/<pipeline.skill.submodulePath>` hold the skill at the
    commit this worktree's branch pins -- the per-epic `$SDLC_DIR` stage agents
    read (`SKILL.md`, "Setup"). A linked worktree does NOT populate submodules on
    `git worktree add`, and a merge that moves the gitlink only marks it
    `modified (new commit)`; both need `git submodule update --init` in that
    worktree. Verified empirically 2026-09-12 on git 2.50.1: the submodule's
    gitdir lands under `$GIT_COMMON_DIR/worktrees/<id>/modules/`, so each
    worktree's copy is independent of the main checkout's and of every other
    worktree's. That per-worktree gitdir is what the post-init check asserts --
    a git too old to do it would share one gitdir across worktrees, and the
    check refuses rather than let two epics silently fight over one checkout.

    Structured, never raising for the "no submodule here" case:
    `{"skill_dir": None, "reason": ...}` when the feature is disabled or the
    path is not a tracked gitlink in this tree."""
    sub = (PIPELINE["skill"].get("submodulePath") or "").strip("/")
    if not sub:
        return {"skill_dir": None, "reason": "pipeline.skill.submodulePath is empty"}
    try:
        runner(["git", "-C", worktree, "ls-files", "--error-unmatch", sub])
    except GhError:
        return {"skill_dir": None, "reason": f"{sub} is not tracked in this tree"}
    runner(["git", "-C", worktree, "submodule", "update", "--init", "--", sub])
    skill_dir = os.path.join(worktree, sub)
    gitdir = runner(["git", "-C", skill_dir, "rev-parse", "--absolute-git-dir"]).strip()
    if "/worktrees/" not in gitdir:
        raise GhError(
            f"submodule {sub} in linked worktree {worktree} uses gitdir {gitdir}, which is not "
            f"per-worktree ($GIT_COMMON_DIR/worktrees/<id>/modules/...). This git cannot keep "
            f"one skill checkout per epic; upgrade git (verified working on 2.50.1).")
    probe = PIPELINE["skill"].get("probeFile")
    try:
        runner(["git", "-C", skill_dir, "ls-files", "--error-unmatch", probe] if probe else
               ["git", "-C", skill_dir, "rev-parse", "HEAD"])
    except GhError:
        raise GhError(f"submodule {sub} initialised but {probe or 'HEAD'} is missing in "
                      f"{skill_dir} -- the pinned skill commit is not checked out")
    pinned = runner(["git", "-C", skill_dir, "rev-parse", "HEAD"]).strip()
    return {"skill_dir": skill_dir, "skill_commit": pinned}


def cmd_worktree_add(gh: GitHub, number: int, unit: str = "issue", repo_path: str = ".",
                     runner: Runner = _default_runner) -> dict:
    """Stand up the unit's worktree the one correct way, so the orchestrator never
    hand-types `git worktree add` (SKILL.md, "Deterministic control plane").

    * Branch already checked out somewhere -> no-op, returns that path.
    * `origin/<branch>` exists (a resume, or a later stage) -> `-B <branch>
      origin/<branch>`: the pushed tip, never fresh off `main` (the 2026-09-04
      resume-base bug -- see references/parallelism.md, "Resume base").
    * Otherwise a first touch -> `-b <branch>` off the unit's integration base
      (`origin/epic-<parent>` for a normal-epic child, `origin/main` for a
      standing-epic child, a parentless issue, or an epic's own branch).
    Always fetches first so every origin ref read is current."""
    branch = epic_branch(number) if unit == "epic" else issue_branch(number)
    path = worktree_path(unit, number)
    existing = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
    if existing:
        return {"created": False, "path": existing, "branch": branch,
                "reason": "branch already checked out in a live worktree"}
    runner(["git", "-C", repo_path, "fetch", "origin"])
    on_origin = runner(["git", "-C", repo_path, "branch", "-r", "--list",
                        f"origin/{branch}"]).strip()
    if on_origin:
        base = f"origin/{branch}"
        runner(["git", "-C", repo_path, "worktree", "add", path, "-B", branch, base])
        resumed = True
    else:
        base = f"origin/{integration_base(gh, number, unit)}"
        runner(["git", "-C", repo_path, "worktree", "add", path, "-b", branch, base])
        resumed = False
    # A linked worktree's submodule directory is empty until initialised; do it
    # here so `skill_dir` is the per-unit `$SDLC_DIR` from the first command on.
    skill = init_skill_submodule(path, runner=runner)
    return {"created": True, "path": path, "branch": branch, "base": base,
            "resumed": resumed, **skill}


def release_worktree(branch: str, runner: Runner = _default_runner,
                      base_repo: str = ".") -> dict:
    """Remove the git worktree holding `branch`, if one exists and holds nothing
    unsaved -- the mechanical half of "a parked or finished unit stops consuming
    a dev-lane slot".

    Called by every command that takes a unit to a stopping point
    (`mark-needs-human`, `mark-blocked`, `merge-pr`), because relying on the
    orchestrator to remember `git worktree remove` did not survive contact: a
    real incident on 2026-08-20 left `/tmp/sdlc-dev-186`'s worktree behind after
    #186 was parked `needs-human`, and `list-parallel-ready` -- which counts
    slots off live worktrees -- read the lane as full (3/3) and proposed nothing
    for the rest of the invocation, with no error anywhere to notice.

    **Never destroys work.** Refuses (structured result, no exception) when the
    worktree has uncommitted changes or commits not yet on `origin/<branch>`,
    and when the branch is checked out in the repository's *main* worktree
    (which `git worktree remove` cannot remove anyway). Absent worktree is the
    common, unremarkable case -- `{"released": False, "reason": "no worktree"}`.
    `cmd_list_parallel_ready` self-heals the slot count independently, so a
    refusal here is a disk-hygiene note, never a stuck lane."""
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
        unpushed = runner(["git", "-C", path, "log", "--oneline",
                            f"origin/{branch}..{branch}"]).strip()
        if unpushed:
            return {"released": False, "path": path,
                    "reason": f"{len(unpushed.splitlines())} commit(s) not pushed to origin/{branch}"}
        runner(["git", "-C", base_repo, "worktree", "remove", path])
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    return {"released": True, "path": path}


def resolve_repo_path(repo_path: Optional[str], branch: str,
                       runner: Runner = _default_runner) -> str:
    """Where a **read-only** command (`verify-exit`) looks for a branch's files.
    An explicitly passed `--repo-path` wins; otherwise the branch's live worktree
    from `git worktree list`; "." only when nothing holds the branch. Commands
    that *write* to a branch never use this -- they go through
    `branch_workspace`, which never yields the main checkout (2026-09-12)."""
    if repo_path is not None:
        return repo_path
    return worktree_path_for_branch(branch, runner=runner) or "."


def _lock_dir() -> str:
    """Directory of the per-branch lockfiles. `SDLC_LOCK_DIR` wins (the test suite
    and any operator who wants locks off the worktree root); else the config's
    `pipeline.locks.dir` with `{worktreesRoot}` expanded."""
    env = os.environ.get("SDLC_LOCK_DIR")
    if env:
        return env
    return PIPELINE["locks"]["dir"].format(worktreesRoot=PIPELINE["worktrees"]["root"])


class BranchLocked(GhError):
    """Raised when another process holds the branch's lock past `waitSeconds`."""


@contextlib.contextmanager
def branch_lock(branch: str, wait_seconds: Optional[float] = None):
    """Exclusive per-branch `flock` around every branch-touching command, so two
    sessions (or two lanes of one session) can never fetch/checkout/merge/push
    the same branch at the same time. Keyed by branch name, so operations on
    *different* branches never contend -- a global main-checkout mutex would
    serialize the whole pipeline for no gain, and is not what removed the race:
    the race is gone because the main checkout is no longer a write target at
    all (`branch_workspace`); the lock is the belt for two writers on one branch.

    Blocks up to `pipeline.locks.waitSeconds` (default 600, `0` = fail fast),
    polling non-blocking `flock` every 0.5s, then raises `BranchLocked` (a
    `GhError`, exit 1) naming the lockfile so the operator can see who holds it.
    The lockfile is never deleted -- deleting it would let a third process lock a
    fresh inode while two others still contend on the old one."""
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
    """`(main_path, {branch: path})` from `git worktree list --porcelain`. The
    first `worktree` entry is always the repository's main worktree (git lists
    it first, unconditionally); detached worktrees have no `branch` line and are
    absent from the map."""
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
    """A working tree that has `branch` checked out, for a command that must
    write to that branch -- **never the repository's main checkout.**

    Resolution order:
    1. A live non-main worktree already holds the branch -> use it, leave it.
    2. The *main* checkout holds the branch -> refuse (`GhError`) with the
       recovery recipe. This is the stolen-branch state the 2026-09-10/12
       incidents left behind (`ops_main_checkout_steals_branch`): operating there
       would keep the main checkout off `main` and keep the branch's real
       `/tmp/sdlc-*` worktree detached under whichever agent is using it.
    3. Nothing holds it -> stand up an **ephemeral** worktree at
       `<worktrees.root>/<ephemeralPrefix><branch>-<pid>` from `origin/<branch>`
       (fetch first), operate, and remove it on exit if it is clean and fully
       pushed. A local `<branch>` ref carrying commits not on origin is refused
       rather than reset away; a branch on neither origin nor local is an error.

    Used as a context manager; `.path` is the tree to operate in, `.ephemeral`
    says whether it was created here, and `.retained` is set when an ephemeral
    tree could not be removed (dirty / unpushed -- surfaced in the caller's
    result so the operator knows a tree is left behind)."""

    def __init__(self, branch: str, base_repo: str = ".", runner: Runner = _default_runner):
        self.branch = branch
        self.base_repo = base_repo or "."
        self.runner = runner
        self.path: Optional[str] = None
        self.ephemeral = False
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
        r(["git", "-C", base, "fetch", "origin"])
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


def _with_workspace(result: dict, ws: BranchWorkspace) -> dict:
    """Surface a retained ephemeral tree in a command's JSON result; silent when
    the tree was a live worktree or was cleanly removed."""
    if ws.retained:
        result["retained_worktree"] = ws.retained
    return result


def origin_branch_exists(repo_path: str, branch: str, runner: Runner = _default_runner) -> bool:
    """Whether `origin/<branch>` exists as a remote-tracking ref in `repo_path`.
    Relies on the `git fetch origin` `cmd_list_parallel_ready` runs first, so
    the answer reflects origin's current state, not a stale local ref cache.
    `git show-ref --verify --quiet` exits nonzero (raised as GhError by the
    runner) when the ref is absent -- that's the False case, not a failure."""
    try:
        runner(["git", "-C", repo_path, "show-ref", "--verify", "--quiet",
                 f"refs/remotes/origin/{branch}"])
        return True
    except GhError:
        return False


def active_worktree_branches(repo_path: str, runner: Runner = _default_runner) -> set:
    """Branch names currently checked out in a git worktree under `repo_path`'s
    repository, main worktree included -- the live ground truth for which
    `issue-<n>` branches are actively being developed right now, so
    DEV_LANE_PARALLELISM is enforced off real state instead of hand-tracked
    notes that don't survive a crashed session. `git worktree list --porcelain`
    emits one `branch refs/heads/<name>` line per worktree that has a branch
    checked out -- a *detached* worktree (the kind `pr-review`'s own pool uses)
    has no such line and is correctly excluded, since a PR under review isn't
    development-lane work."""
    out = runner(["git", "-C", repo_path, "worktree", "list", "--porcelain"])
    branches = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/"):])
    return branches


def cmd_list_parallel_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                             runner: Runner = _default_runner) -> dict:
    """Every open child of `epic` at `lld`/`development`/`testing` that's safe to
    start (or resume) concurrently, in its own `git worktree`, right now -- see
    "Parallel implementation lane" in references/parallelism.md. This is the mechanical
    replacement for hand-tracking eligibility against `architecture.md`'s prose:
    a candidate is proposed only if it (a) isn't already running in its own
    worktree, (b) isn't blocked/needs-human/gate-pending, (c) has no open
    native `blockedBy`, and (d) its declared `## Footprint` doesn't overlap any
    currently-active child's, or any other candidate already selected this
    call (checked in `sort_key` order, so higher-priority/older children win a
    footprint collision). `active_count` is read live off `git worktree list`,
    not hand-tracked, so it's correct even after a crashed session.

    Every child issue in this pipeline always works in its own worktree now
    (see "Working on a branch" in references/parallelism.md) -- there is no separate
    "shared-checkout occupant" to account for on top of `active_count`.

    A **never-started** child of an architected epic (Stage `lld` or still
    unset, no `origin/issue-<n>` branch yet) is eligible *without* a footprint
    -- there is no committed `lld.md` to read one from yet, and requiring one
    would deadlock the lane on a chicken-and-egg (the footprint is written *by*
    the `lld` stage this call would start). Starting `lld` itself is safe: it
    writes only that issue's own `{DOC_ROOT}/issue-<n>/` folder, which
    cannot collide with any sibling; that folder is used as the child's
    stand-in footprint for this call's collision bookkeeping. The real
    footprint check kicks in from the next call onward, once `lld.md` is
    committed -- and `lld-review` independently checks the freshly declared
    footprint against active siblings before `development` starts.

    Read-only apart from a `git fetch origin` (needed so origin-ref reads --
    footprints, branch existence -- reflect current remote state): never
    claims, never posts, never creates a worktree. `skipped` names every child
    this call excluded and why."""
    limit = DEV_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (a top-level Type: Feature issue) -- "
                       f"pass the epic's own issue number, not a child issue's")
    base = {"parallel_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": []}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if resolve_profile(epic_issue)["childrenNeedArchitectedEpic"] \
            and not is_epic_architected(epic_issue):
        # Same guard as decide_next_action's children loop: a profile whose children
        # need an architected epic keeps them out of the lane until epic:architected.
        return {**base, "note": f"epic #{epic} is not epic:architected yet -- its children "
                                 f"are not eligible for the implementation lane"}
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]
    children = [i for i in open_issues if not is_epic(i) and i.get("parent")
                and i["parent"]["number"] == epic]

    runner(["git", "-C", repo_path, "fetch", "origin"])
    active_branches = active_worktree_branches(repo_path, runner=runner)
    active_footprints = []
    for branch in sorted(active_branches):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        fp = read_footprint(repo_path, number, runner=runner)
        if fp:
            active_footprints.append((number, fp))

    eligible, skipped = [], []
    selected_footprints = list(active_footprints)
    for issue in sorted(children, key=sort_key):
        number = issue["number"]
        branch = issue_branch(number)
        if branch in active_branches:
            skipped.append({"issue": number, "reason": "already active in its own worktree"})
            continue
        # A brand-new child next-action hasn't surveyed yet has no Stage value;
        # default_stage() says what it would be assigned (`lld` for an
        # architected epic's child). Read-only here -- the field itself is
        # written by next-action's own survey or by `claim`, not this command.
        stage = current_stage(issue) or default_stage(issue, epic_issue)
        if stage not in ("lld", "development", "testing"):
            skipped.append({"issue": number, "reason": f"stage is {stage!r}, not lld/development/testing"})
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        if status == "in-progress":
            skipped.append({"issue": number, "reason": "Pipeline Status is 'in-progress' but this "
                                                         "branch has no active worktree -- likely a "
                                                         "crashed run; resume it via next-action, "
                                                         "don't also start it here"})
            continue
        if gh.blocked_by(number):
            skipped.append({"issue": number, "reason": "blocked by an open dependency"})
            continue
        footprint = read_footprint(repo_path, number, runner=runner)
        if not footprint and stage == "lld" \
                and not origin_branch_exists(repo_path, branch, runner=runner):
            # Never-started child -- no branch, so no lld.md to carry a
            # footprint yet. Safe to start `lld` regardless (see docstring);
            # its own docs folder stands in as the footprint for this call.
            footprint = [f"{DOC_ROOT}/issue-{number}/"]
        if not footprint:
            skipped.append({"issue": number, "reason": "no ## Footprint section found in its "
                                                         "lld.md/architecture.md on origin -- cannot "
                                                         "verify non-overlap"})
            continue
        collision = next((n for n, fp in selected_footprints if footprint_overlaps(footprint, fp)), None)
        if collision is not None:
            skipped.append({"issue": number, "reason": f"footprint overlaps active/eligible #{collision}"})
            continue
        eligible.append({"issue": number, "branch": branch, "stage": stage, "title": issue["title"]})
        selected_footprints.append((number, footprint))

    # Slot count is independent of whether a footprint was readable for an
    # active branch -- a stale/undocumented active branch still consumes a slot.
    # It is *not* independent of whether anything is still working there: a
    # worktree whose issue is parked (needs-human / blocked / gate-pending) or
    # already closed holds a slot nothing can ever use. Releasing it is
    # `release_worktree`'s job at park time; counting it correctly here is the
    # backstop for when that didn't happen -- a crashed session, a hand-parked
    # issue, or the 2026-08-20 incident where a leftover /tmp/sdlc-dev-186 read
    # as a full lane and silently starved the invocation.
    occupied, stale = [], []
    for branch in sorted(active_branches):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            stale.append({"branch": branch, "reason": f"Pipeline Status is {status!r}"})
            continue
        if gh.blocked_by(number):
            stale.append({"branch": branch, "reason": "blocked by an open dependency"})
            continue
        occupied.append(branch)
    active_count = len(occupied)
    slots = max(0, limit - active_count)
    selected = eligible[:slots]
    return {"parallel_ready": selected, "count": len(selected), "eligible_total": len(eligible),
            "active_count": active_count, "active_branches": sorted(occupied),
            "limit": limit, "slots_available": slots, "epic": epic, "skipped": skipped,
            "stale_worktrees": stale}


def cmd_list_design_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                           runner: Runner = _default_runner) -> dict:
    """Every open child of a **standing** `epic` at `product`/`architecture` that's
    safe to start (or resume) concurrently, in its own `git worktree`, right now --
    the design-stage sibling of `list-parallel-ready` (which only ever proposes
    `lld`/`development`/`testing` children). See "Design lane" in
    references/parallelism.md.

    Standing-profile only. A standing epic has no epic-level Product/Architecture
    phase (`epicLevelPhase == false`, see `is_epic_standing`); each child runs its
    own full `product`->...->`testing` flow, so its design stages are per-child work
    that can fan out. A **default**-profile epic runs `product`/`architecture` once
    at the epic level as a single epic-self unit -- there is nothing to fan out, so
    this returns empty (with a `note`) for it. Gated on the resolved profile's
    `epicLevelPhase`, not the hardcoded `epic:standing` label, so a client's own
    label->profile mapping is honoured (same as everywhere else -- see
    `resolve_profile`).

    Same eligibility gating as the dev lane: a candidate is proposed only if it
    (a) isn't already running in its own worktree, (b) isn't
    blocked/needs-human/gate-pending, and (c) has no open native `blockedBy`.
    Unlike the dev lane there is **no footprint check**: `product`/`architecture`
    for a standing child write only that child's own `{DOC_ROOT}/issue-<n>/`
    docs folder, which cannot collide with a sibling's, so there is no
    cross-child code overlap to verify (the footprint gate exists for the dev
    lane, where children touch shared source trees). `active_count` is read live
    off `git worktree list`, counting only worktrees whose child is itself in a
    design stage -- a sibling that has already moved to the dev lane
    (`lld`/`development`/`testing`) holds a worktree but is the dev lane's
    concern and its own cap, never a design-lane slot.

    Read-only apart from a `git fetch origin` (so the active-worktree read
    reflects current remote state): never claims, posts, or creates a worktree.
    `skipped` names every child this call excluded and why. Mirrors
    `list-parallel-ready`'s JSON shape, with `design_ready` in place of
    `parallel_ready`.

    **Product WIP cap** (`pipeline.productWip.maxGateAPending`, default 5): a
    `product`-stage candidate is proposed only while the repo-wide Gate A queue
    (`product_gate_pending`) plus the `product` candidates already selected in
    this call leave headroom under the cap -- one fan-out cannot push the human's
    review queue past it. `architecture`-stage candidates are never gated (they
    are past Gate A). Result carries `product_cap` with the cap and the pending
    list so the orchestrator can report it."""
    limit = DESIGN_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (a top-level Type: Feature issue) -- "
                       f"pass the epic's own issue number, not a child issue's")
    # Same exemption as decide_next_action: an auto-passing Gate A never queues on
    # the human, so that profile's product candidates are not capped.
    product_headroom = (product_wip_headroom(all_issues)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    product_cap = {"limit": PRODUCT_WIP_CAP, "pending": product_gate_pending(all_issues)}
    base = {"design_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": [], "stale_worktrees": [], "product_cap": product_cap}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if not is_epic_standing(epic_issue):
        # Default-profile epic: its product/architecture is the epic-level phase,
        # a single epic-self unit run in the epic's own worktree -- never fanned
        # out across children. Gate on the resolved profile's epicLevelPhase, not
        # the standing label (see docstring / resolve_profile).
        return {**base, "note": f"epic #{epic} runs product/architecture at the epic level "
                                 f"(profile epicLevelPhase is true) -- its design work is a single "
                                 f"epic-self unit, not fanned out across children"}
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]
    children = [i for i in open_issues if not is_epic(i) and i.get("parent")
                and i["parent"]["number"] == epic]

    runner(["git", "-C", repo_path, "fetch", "origin"])
    active_branches = active_worktree_branches(repo_path, runner=runner)

    eligible, skipped = [], []
    for issue in sorted(children, key=sort_key):
        number = issue["number"]
        branch = issue_branch(number)
        if branch in active_branches:
            skipped.append({"issue": number, "reason": "already active in its own worktree"})
            continue
        # A standing child next-action hasn't surveyed yet has no Stage value;
        # default_stage() says what it would be assigned (`product` -- a standing
        # profile's childEntryStage). Read-only here -- the field itself is
        # written by next-action's own survey or by `claim`, not this command.
        stage = current_stage(issue) or default_stage(issue, epic_issue)
        if stage not in ("product", "architecture"):
            skipped.append({"issue": number, "reason": f"stage is {stage!r}, not product/architecture"})
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            skipped.append({"issue": number, "reason": f"Pipeline Status is {status!r}"})
            continue
        if status == "in-progress":
            skipped.append({"issue": number, "reason": "Pipeline Status is 'in-progress' but this "
                                                         "branch has no active worktree -- likely a "
                                                         "crashed run; resume it via next-action, "
                                                         "don't also start it here"})
            continue
        if gh.blocked_by(number):
            skipped.append({"issue": number, "reason": "blocked by an open dependency"})
            continue
        if stage == "product" and product_headroom is not None:
            if product_headroom <= 0:
                skipped.append({"issue": number,
                                "reason": f"product WIP cap: {len(product_cap['pending'])} unit(s) "
                                          f"already awaiting Gate A review (cap "
                                          f"{PRODUCT_WIP_CAP}) -- not starting a fresh product "
                                          f"stage until one passes"})
                continue
            product_headroom -= 1
        eligible.append({"issue": number, "branch": branch, "stage": stage, "title": issue["title"]})

    # Slot count off live worktrees, restricted to *design*-stage occupants: a
    # sibling worktree in lld/development/testing is the dev lane's slot, not
    # this one's (the two lanes have independent caps). A worktree whose issue is
    # parked (needs-human / blocked / gate-pending) or already closed holds a
    # slot nothing can use -- reported under `stale_worktrees`, same backstop as
    # the dev lane (see cmd_list_parallel_ready).
    occupied, stale = [], []
    for branch in sorted(active_branches):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        stage = current_stage(issue) or default_stage(issue, epic_issue)
        if stage not in ("product", "architecture"):
            # A dev-lane worktree -- counted against DEV_LANE_PARALLELISM by
            # list-parallel-ready, never against the design lane.
            continue
        status = pipeline_status(issue)
        if status == "needs-human" or status in GATE_PENDING_STATUSES:
            stale.append({"branch": branch, "reason": f"Pipeline Status is {status!r}"})
            continue
        if gh.blocked_by(number):
            stale.append({"branch": branch, "reason": "blocked by an open dependency"})
            continue
        occupied.append(branch)
    active_count = len(occupied)
    slots = max(0, limit - active_count)
    selected = eligible[:slots]
    return {"design_ready": selected, "count": len(selected), "eligible_total": len(eligible),
            "active_count": active_count, "active_branches": sorted(occupied),
            "limit": limit, "slots_available": slots, "epic": epic, "skipped": skipped,
            "stale_worktrees": stale, "product_cap": product_cap}


def cmd_handoff_to_pr_review(gh: GitHub, issue: int, pr: int, summary: str) -> dict:
    """`development`'s exit action once its own suites are green: posts the
    canonical `<!-- stage-transition: development->pr-review @ <ts> -->` handoff
    marker that puts this issue into `list-ready-for-review`'s pool. Run it every
    time development finishes -- including after a rework round, since a fresh
    handoff marker is exactly what makes an issue reviewable again after a
    recorded `rework` outcome (see `cmd_list_ready_for_review`).

    Exists as a command rather than prose because the marker is now load-bearing
    (it's the queue), not just a visibility breadcrumb. It had been left to each
    stage agent to hand-write, and the real thread history shows that does not
    hold up: #115 wrote `development->pr-review`, #111 wrote a mangled
    `pr-review->development->pr-review`, #130 wrote none at all. Same reasoning as
    `start-comment` -- fixed, judgment-free wording belongs here, not in a
    hand-typed `gh issue comment`. See "Deterministic control plane" in SKILL.md.

    Does not touch Stage (already `PR Review` from `open-dev-pr`) or Pipeline
    Status (still `In Progress`) -- a queued-for-review issue is not a *new* state,
    it's the same one, now with a marked handoff."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.issue_comment(issue,
        f"✅ Development complete. {summary} "
        f"PR #{pr} is queued for `pr-review`.\n\n"
        f"<!-- stage-transition: development->pr-review @ {timestamp} -->")
    return {"issue": issue, "pr": pr, "queued_for": "pr-review"}


PR_REVIEW_OUTCOMES = ("clean", "rework")


def cmd_record_pr_review(gh: GitHub, issue: int, pr: int, outcome: str, summary: str) -> dict:
    """Records that a `pr-review` pass ran on this issue's PR and what it found --
    the last action of every review, clean or not, **before** `merge-pr` or a
    rework resume. Posts one comment carrying the
    `<!-- pr-review-outcome: <outcome>:<pr> @ <ts> -->` marker
    `list-ready-for-review` reads back, so a PR under rework is never handed to a
    second, concurrent review agent (see `cmd_list_ready_for_review`).

    `outcome` is `clean` (nothing found; merging next) or `rework` (findings sent
    back to `development`). Recording it on the clean path too is not redundant:
    the merge can fail or be delayed by CI, and the issue stays listable until it
    actually closes."""
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headline = ("🔍 PR review complete — no findings; proceeding to merge."
                if outcome == "clean" else
                "🔁 PR review complete — findings sent back to `development` for rework.")
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- pr-review-outcome: {outcome}:{pr} @ {timestamp} -->")
    return {"issue": issue, "pr": pr, "outcome": outcome, "recorded": True}


# How much of a suite run's captured output an attestation embeds. Enough to show
# the summary line and the last failures a real run would print; short enough that
# the PR thread stays readable and the comment stays inside GitHub's body limit.
LOCAL_CI_EVIDENCE_LINES = 40
LOCAL_CI_EVIDENCE_CHARS = 4000


def read_ci_evidence(output_path: str) -> str:
    """The tail of a suite run's own captured stdout/stderr, for `record-local-ci`.

    Reads the file the stage redirected its run into. Refuses an unreadable or
    empty one: the point of the attestation is that the merge gate sees the
    runner's words rather than the agent's summary of them, and an empty file is
    exactly the claim-without-evidence this replaced."""
    try:
        with io.open(os.path.expanduser(output_path), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise GhError(f"--output must be a readable file holding the suite run's own "
                      f"captured output: {exc}")
    text = text.strip()
    if not text:
        raise GhError("--output file is empty -- an attestation must carry the run's own "
                      "output, not a summary of it")
    lines = text.splitlines()
    if len(lines) > LOCAL_CI_EVIDENCE_LINES:
        lines = ["... (earlier output trimmed) ..."] + lines[-LOCAL_CI_EVIDENCE_LINES:]
    trimmed = "\n".join(lines)
    if len(trimmed) > LOCAL_CI_EVIDENCE_CHARS:
        trimmed = "... (trimmed) ...\n" + trimmed[-LOCAL_CI_EVIDENCE_CHARS:]
    return trimmed


def cmd_record_local_ci(gh: GitHub, pr: int, suite: str, sha: str,
                         command: str, output: str) -> dict:
    """Attest that a required suite (`backend`/`frontend`) passed locally against a
    specific commit -- the merge-gate stand-in for the GHA check that no longer
    runs on a child PR (both suites went main-only for cost on 2026-09-04; see each
    workflow's `on:` block and `REQUIRED_WORKFLOWS`).

    Run by the `development` stage once per suite it actually ran, against the
    exact commit it ran against. Posts one comment on the *PR* (not the issue --
    the attestation is bound to this PR's head commit, and `merge-pr`/`pr-checks`
    read it back from the PR thread alongside the head SHA) carrying
    `<!-- local-ci: <suite>:<pr> @ <sha> -->`.

    **The attestation carries evidence, not a claim.** `--command` is the exact
    command run and `--output` a file holding that run's own captured stdout/stderr;
    its tail is embedded in the comment. This is the whole of what replaced the
    retired `testing` stage's independent re-run (2026-09-12): the implementer now
    writes and runs its own tests, so the merge gate's protection is that the
    evidence is machine-produced and pinned to a sha, not that a second agent
    repeated the work. A summary with no captured output is refused here rather
    than discovered at `pr-review`.

    `merge-pr` honours it only while `sha` matches the PR's current head: push a new
    commit (a rework round) and the attestation goes stale and must be re-run. Pass
    the tested worktree's `git rev-parse HEAD`; a short sha is fine (prefix-matched).
    Not a substitute for the `testing->pr-review` handoff or the `pr-review` outcome
    -- those still gate merge via `missing_pipeline_evidence`; this is only the CI
    half."""
    if suite not in LOCAL_CI_SUITES:
        raise GhError(f"suite must be one of {LOCAL_CI_SUITES}, got {suite!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        raise GhError(f"sha must be a 7-40 char hex commit id, got {sha!r}")
    if not (command or "").strip():
        raise GhError("--command is required: the exact command the suite was run with")
    evidence = read_ci_evidence(output)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fence = "```"
    gh.pr_comment(pr, f"🧪 Local CI attested — `{suite}` suite passed locally against "
                       f"`{sha}` (main-only GHA CI; this is the merge-gate stand-in).\n\n"
                       f"Command: `{command.strip()}`\n\n"
                       f"<details><summary>captured output (tail)</summary>\n\n"
                       f"{fence}\n{evidence}\n{fence}\n\n</details>\n\n"
                       f"<!-- local-ci: {suite}:{pr} @ {sha} -->\n"
                       f"<!-- attested-at: {timestamp} -->")
    return {"pr": pr, "suite": suite, "sha": sha, "command": command.strip(),
            "evidence_lines": len(evidence.splitlines()), "attested": True}


DESIGN_REVIEW_ROLES = ("product-review", "arch-review", "lld-review")


def cmd_record_design_review(gh: GitHub, issue: int, role: str, outcome: str,
                             summary: str, unit: str = "issue") -> dict:
    """Records that `arch-review` or `lld-review` ran and what it concluded --
    the design-side twin of `record-pr-review`, and the last action of every
    design review, clean or not, before the orchestrator resumes the design agent
    or moves the unit on.

    Posts one comment carrying
    `<!-- design-review-outcome: <outcome>:<role> @ <ts> -->`, which
    `cmd_pairing_counts` reads back per role. Added 2026-08-28 out of epic #98's
    retrospective -- see `_DESIGN_REVIEW_OUTCOME_MARKER` for why the pairing that
    fires the valve most often had no counter until then.

    This does **not** replace the `arch-review-confidence` marker, which
    `skip-gate` reads and which answers a different question (how much to trust a
    *clean* verdict). Confidence cannot stand in for a bounce count: it is
    meaningless on a rework verdict, which is precisely the verdict a valve counts."""
    if role not in DESIGN_REVIEW_ROLES:
        raise GhError(f"role must be one of {DESIGN_REVIEW_ROLES}, got {role!r}")
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headline = (f"🔍 `{role}` complete — no findings."
                if outcome == "clean" else
                f"🔁 `{role}` complete — findings sent back for rework.")
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- design-review-outcome: {outcome}:{role} @ {timestamp} -->")
    return {"issue": issue, "unit": unit, "role": role,
            "outcome": outcome, "recorded": True}


def _post_start_comment(gh: GitHub, issue: int, role: str):
    gh.issue_comment(issue, f"🚧 Picking this up — {role} stage starting.")


def cmd_claim(gh: GitHub, issue: int, role: str) -> dict:
    if role not in STAGE_OPTION_IDS:
        # A typo'd role used to silently skip the Stage write while still
        # claiming In Progress and posting a start comment naming the bogus
        # role -- surface it loudly instead; every claimable role is a real
        # Stage option.
        raise GhError(f"unknown role {role!r} -- must be one of "
                       f"{sorted(STAGE_OPTION_IDS)}")
    gh.set_stage_field(issue, role)
    gh.set_pipeline_status_field(issue, "in-progress")
    _maybe_mark_epic_in_progress(gh, issue)
    _post_start_comment(gh, issue, role)
    return {"issue": issue, "claimed": True}


def _maybe_mark_epic_in_progress(gh: GitHub, issue: int):
    """Sets the board's native Status field to "In Progress" the moment a normal
    epic starts or resumes its own Product/Architecture phase -- see "Epic board
    Status" in references/epics.md. A no-op for a child issue. Deliberately excluded for a
    standing epic (`is_epic_standing`) -- it never claims an epic-level stage
    (its children run the old per-issue flow instead), so this never fires for
    it either way; the operator tracks its board Status by hand. (An
    `epic:legacy` epic is never passed here at all -- `decide_next_action`
    skips it before `claim` is ever called on it or any of its children.)
    Best-effort by construction (set_project_status swallows its own
    failures), so this can never block or fail the claim itself."""
    info = gh.issue_epic_info(issue)
    if is_epic(info) and not is_epic_standing(info):
        gh.set_project_status(issue, "in-progress")


def cmd_start_comment(gh: GitHub, issue: int, role: str) -> dict:
    """Post the mandatory '🚧 Picking this up' start comment with no field mutation --
    for transitions into a role that has no Stage value of its own (arch-review,
    lld-review, pr-review) or where Pipeline Status is already "In Progress" from
    the prior stage (testing, picked up right after development's open-dev-pr
    already set Stage to "Testing"). Keeps this fixed, judgment-free wording out of
    hand-typed `gh` calls -- see "Deterministic control plane" in SKILL.md.

    `lld-review` was added 2026-08-22: it is mandatory on every normal-epic child and
    ran ~15 times across epic #156 without posting a single start comment, because
    the only accepted role string was `arch-review` and the playbook's instruction to
    reuse it would have produced a comment naming the wrong stage. Every other stage
    announces itself; a silent mandatory stage is invisible to crash recovery."""
    _post_start_comment(gh, issue, role)
    return {"issue": issue, "started": role}


def git_rev_parse_head(repo_path: str, runner: Runner = _default_runner) -> str:
    return runner(["git", "-C", repo_path, "rev-parse", "HEAD"]).strip()


_GATE_PR_BODY = (
    'Doc-only review gate for #{issue} — see "Human-review gates" in the pipeline '
    'docs. Merging this PR is the approval to proceed to '
    '{next_stage}. Do NOT use Closes/Fixes here — the tracking issue stays open until the '
    'final code PR merges.'
)


def cmd_open_gate(gh: GitHub, repo_path: Optional[str], issue: int, title: str, doc: str,
                   next_stage: str, summary: str, unit: str = "issue",
                   runner: Runner = _default_runner) -> dict:
    stage = doc.rsplit(".", 1)[0]  # "product.md" -> "product", "architecture.md" -> "architecture"
    branch = f"{unit}-{issue}"
    # A standing-epic child's gate is `issue-<n>` -> `main`. An epic-level gate is
    # authored on a disposable sub-branch cut from the epic branch and opened
    # against it -- `epic-<n>-gate-<stage>` -> `epic-<n>` -- so the doc lands on
    # the epic branch when the human merges (squash or not), and `epic-<n>` alone
    # ever merges to `main`, unsquashed, at `close-epic`. See `epic_gate_branch`.
    if unit == "epic":
        head, base = epic_gate_branch(issue, stage), epic_branch(issue)
        # The epic branch is merge-only. If the doc was committed straight onto
        # `epic-<n>` instead of the sub-branch, the gate is either unpushed or
        # empty -- and an empty gate PR reads to the human as "nothing to review"
        # while the design sits unreviewed on the integration branch. Both show up
        # as the sub-branch having nothing over its base, so refuse loudly here
        # rather than leaving an orchestrator to notice by eye. Recovery is branch
        # surgery on a shared branch, so it names the operator explicitly. Added
        # 2026-09-06 retro, after `architecture.md` reached `epic-345` directly.
        ahead = gh.branch_ahead_by(head, base=base)
        if not ahead:
            detail = ("does not exist on origin" if ahead is None
                      else f"carries no commits over {base}")
            raise GhError(
                f"gate branch {head} {detail} -- an epic's {doc} is authored on that "
                f"disposable sub-branch, never committed to {base} directly (see "
                f"\"Opening a gate\" in references/gates.md). If the doc is already "
                f"committed on {base}, this needs operator approval: move those "
                f"commits onto {head} (cut from the epic's clean base), force-rewind "
                f"{base} to it, push both, then re-run open-gate.")
    else:
        head, base = branch, "main"
    # The SHA the gate comment cites is the PUSHED head -- `origin/<head>` after a
    # fetch -- never a local worktree's HEAD. The PR is opened against origin, so
    # a local-only commit could only ever produce a comment citing a SHA the PR
    # does not contain; and reading origin needs no working tree at all, so this
    # command no longer touches (or steals) any checkout. `repo_path` is only
    # where `git fetch` runs (default: the current directory).
    repo_path = repo_path or "."
    runner(["git", "-C", repo_path, "fetch", "origin"])
    sha = runner(["git", "-C", repo_path, "rev-parse", f"origin/{head}"]).strip()
    pr_number = gh.pr_create(
        base=base, head=head,
        title=f"{title} — {doc} for review (#{issue})",
        body=_GATE_PR_BODY.format(issue=issue, next_stage=next_stage),
        draft=False,
    )
    gh.set_pipeline_status_field(issue, "awaiting-human-review")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_verb = "Requirements locked" if stage == "product" else "Design locked"
    comment = (
        f"✅ {doc_verb} — see `{DOC_ROOT}/{branch}/{doc}` (`{sha}`). {summary}\n\n"
        f"⏸️ Awaiting human review — see #{pr_number}. Merge it to approve and continue to "
        f"`{next_stage}`, or leave review comments on it for anything that needs to change "
        f"(leave it unmerged — the pipeline picks up your comments and revises the doc "
        f"automatically). Run `/sdlc-pipeline` again once you've merged it, or any time after "
        f"leaving comments if you'd like the revision done sooner.\n\n"
        f"<!-- gate-pr: {stage}:{pr_number} -->\n"
        f"<!-- stage-transition: {stage}->human-review:{stage} @ {timestamp} -->"
    )
    gh.issue_comment(issue, comment)
    return {"issue": issue, "unit": unit, "gate_pr": pr_number, "stage": stage, "sha": sha,
            "head": head, "base": base}


def git_reconcile_branch(repo_path: str, branch: str, base: str = "main",
                          runner: Runner = _default_runner):
    """Fetch, merge `origin/<base>` into `branch`, and push the result straight back to
    origin -- so the branch handed off to the next stage's agent is already in sync
    with origin, and that agent (or the orchestrator, before delegating) never needs
    to separately notice/push a local-only merge commit.

    Always a real merge, never a rebase -- deliberately. Development PRs are
    squash-merged into `main` (see `cmd_merge_pr`), so a squashed commit shares no
    ancestry with the original per-commit history it replaced; rebasing any other
    still-open issue/epic branch onto a `main` that contains that squash commit
    would try to replay commits `main` already has under different SHAs, producing
    spurious conflicts or duplicated diffs. A merge has no such problem -- it only
    ever looks at current tree content, never shared ancestry -- so it's the only
    safe way to keep a branch current here. A per-issue gate PR (`issue-<n>` ->
    `main`), by contrast, is never squash-merged (see "Passing a gate" in
    references/gates.md) specifically so a later `git_reconcile_branch` merge of
    `main` back into that same branch is a clean no-op, not a phantom diff. An
    epic-level gate never touches `main` at all: its sub-branch merges into
    `epic-<n>` (squash allowed -- the sub-branch is disposable), and `cmd_pass_gate`
    then calls this with `base=epic-<n>` so the epic worktree just fast-forwards
    to `origin/epic-<n>`.

    `checkout` here relies on `runner` raising on a nonzero exit (the default
    `_default_runner` does) so a checkout collision -- e.g. this exact branch
    already checked out in another worktree -- aborts loudly instead of silently
    merging/pushing onto whatever branch happened to be checked out in
    `repo_path` at the time. That failure mode is exactly what happened once by
    hand in this pipeline's own operation, from typing the fetch/checkout/merge/
    push sequence directly instead of going through this function -- see
    "Deterministic control plane" in SKILL.md for why every mechanical git
    sequence belongs here, not retyped in a shell.

    Raises `MergeConflict` (a `GhError` subclass, carrying the conflicting
    file paths) specifically when the `merge` step fails with real unmerged
    paths -- distinguished from any other merge failure (e.g. uncommitted
    local changes) by checking `git diff --diff-filter=U` after the failed
    merge; if that comes back empty, the original error is re-raised as-is,
    since it isn't a content conflict this function knows how to characterize.
    On a real conflict, the merge is aborted (`git merge --abort`) before
    raising, so `repo_path` is left clean rather than mid-conflict. Only
    `cmd_sync_branch` below catches this specially; every other caller (e.g.
    `cmd_pass_gate`) lets it propagate as an ordinary operational failure,
    since a gate PR is doc-only and a real conflict there is unexpected enough
    to warrant stopping loudly rather than routing through rework. See
    "Parallel implementation lane" / git-conflict handling in references/parallelism.md."""
    runner(["git", "-C", repo_path, "fetch", "origin"])
    runner(["git", "-C", repo_path, "checkout", branch])
    try:
        runner(["git", "-C", repo_path, "merge", f"origin/{base}"])
    except GhError:
        conflicted = [f for f in runner(["git", "-C", repo_path, "diff", "--name-only",
                                          "--diff-filter=U"]).splitlines() if f.strip()]
        if not conflicted:
            raise
        runner(["git", "-C", repo_path, "merge", "--abort"])
        raise MergeConflict(conflicted, base=base)
    runner(["git", "-C", repo_path, "push", "origin", branch])


class MergeConflict(GhError):
    """Raised by `git_reconcile_branch` when merging `origin/main` into a
    branch hits real unmerged paths -- see that function's docstring. A
    `GhError` subclass so any caller that doesn't specifically catch it (every
    one except `cmd_sync_branch`) still gets the existing "operational
    failure, exit 1" behavior via `main()`'s generic `except GhError`."""

    def __init__(self, files: list, base: str = "main"):
        super().__init__(f"merge conflict reconciling with origin/{base} on {len(files)} "
                          f"file(s): {', '.join(files)}")
        self.files = files
        self.base = base


def cmd_sync_branch(gh: GitHub, repo_path: Optional[str], issue: int, unit: str = "issue",
                     runner: Runner = _default_runner) -> dict:
    """Reconcile `<unit>-<issue>`'s branch with `origin/main` -- see "Keeping a
    branch current" in references/parallelism.md. Run this before delegating to *any* new stage's
    subagent, not just at a gate pass (which already reconciles internally via
    `cmd_pass_gate`) -- a plain internal stage transition (e.g. `arch-review`
    finishing clean, `testing` handing off to `pr-review`) does not otherwise
    touch `main` at all, and a sibling issue's PR can merge into `main` at any
    point while this issue is mid-flight.

    A real merge conflict is a **valid result**, not an operational failure --
    it's exactly the kind of content judgment call ("whose defect is this,
    who resolves it") that stays a prose decision for the orchestrator, per
    "Deterministic control plane" in SKILL.md, not something this command
    should crash on. So `MergeConflict` is caught here specifically and
    reported as `{"synced": false, "conflict": true, "conflicting_files": [...]}`
    at exit 0, instead of propagating as `GhError` (exit 1) the way every
    other git failure from this command still does."""
    branch = f"{unit}-{issue}"
    base = "main" if unit == "epic" else integration_base(gh, issue, unit)
    result = {"issue": issue, "unit": unit, "branch": branch, "base": base, "synced": True}
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        try:
            git_reconcile_branch(ws.path, branch, base=base, runner=runner)
            # The merge may have moved the skill submodule's gitlink; a merge
            # alone leaves the working files at the OLD pin ("modified (new
            # commit)"). Re-init here -- sync-branch runs between stage agents,
            # so this is the one naturally quiet point per worktree. Only for a
            # live worktree an agent will read from; an ephemeral tree exists
            # for the git op alone.
            if not ws.ephemeral:
                skill = init_skill_submodule(ws.path, runner=runner)
                if skill.get("skill_dir"):
                    result.update(skill)
        except MergeConflict as e:
            # Persist the conflict on the issue -- the JSON result alone doesn't
            # survive a crashed session, and the sync-branch-conflict <->
            # development escalation-valve pairing must be reconstructible from the
            # thread (cmd_pairing_counts reads this marker back).
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            gh.issue_comment(issue,
                f"⚠️ Merge conflict reconciling `{branch}` with `origin/{base}` — "
                f"{len(e.files)} file(s): {', '.join(f'`{f}`' for f in e.files)}. "
                f"Routing to `development` for resolution in its own worktree.\n\n"
                f"<!-- sync-conflict: {branch} @ {timestamp} -->")
            result.update({"synced": False, "conflict": True, "conflicting_files": e.files})
    return _with_workspace(result, ws)


# A `git push` refused by origin because the remote ref moved under us
# (someone else pushed the epic branch between our fetch and our push). This is
# a content race, not an operational git failure -- `cmd_merge_lld_doc` reports
# it as a structured `conflict` result at exit 0, the same spirit as
# `cmd_sync_branch`'s `MergeConflict` catch, and lets the orchestrator re-run
# rather than crashing. Every other `git` failure (bad ref, dirty tree, network)
# still propagates as GhError (exit 1).
_PUSH_REJECTED_RE = re.compile(
    r"\[rejected\]|\[remote rejected\]|non-fast-forward|fetch first|updates were rejected",
    re.IGNORECASE)


def cmd_merge_lld_doc(gh: GitHub, repo_path: Optional[str], issue: int,
                       runner: Runner = _default_runner) -> dict:
    """Publish a normal-epic child's `lld.md` onto its epic branch the instant
    `lld-review` comes back CLEAN -- see "Publishing lld.md to the epic branch"
    in references/parallelism.md -- rather than waiting for the whole child
    pipeline to merge. Two payoffs: the low-level design is durable on the epic
    branch independent of the `issue-<n>` branch, and every sibling picks it up
    in-tree on its next `sync-branch`, so cross-child overlap checks see the real
    committed design instead of only what's reachable on a still-open child
    branch.

    **Scope: normal-epic children only.** A top-level issue with no parent epic,
    or a child of a standing epic (`epic:standing`, e.g. a standing backlog epic -- its children
    integrate straight into `main`, not an epic branch), has no epic branch to
    publish to; both return a structured no-op at exit 0, never an error. The
    parent lookup reuses `issue_list()`'s GraphQL `parent`, exactly as
    `integration_base` does -- `gh issue view --json` has no `parent` field.

    Publishes **only** `{DOC_ROOT}/issue-<n>/lld.md`, taken verbatim from
    `origin/issue-<n>` (its committed, already-reviewed version -- the file
    already survives a crash there, see references/history.md), as a single
    doc-only commit on `epic-<parent>`. This is deliberately NOT a merge of the
    whole child branch: only the design doc is durable-early, none of the child's
    in-progress code. Runs under the epic branch's lock, in the epic branch's
    live worktree or an ephemeral one (`BranchWorkspace`) -- never the main
    checkout.

    **Idempotent, judged on origin.** If `origin/<epic>` already holds the
    byte-identical `lld.md` blob that `origin/issue-<n>` has, this returns
    `{"merged": false, "reason": "up-to-date", "verified_on_origin": true}` --
    safe to run more than once. `merged: true` is reported only after a
    post-push fetch confirms the blob is on `origin/<epic>`; see
    `_publish_lld_doc` for the reconcile loop and the defect it replaced.

    A push refused because the epic branch advanced concurrently (after one
    internal replay from the new tip), or no `lld.md` on `origin/issue-<n>`,
    returns a structured result at exit 0 (`conflict`/`reason`), not an uncaught
    crash -- modelled on `cmd_sync_branch`'s conflict handling. A genuine
    operational git failure still propagates as GhError (exit 1).

    **Advance-not-claim (2026-09-12).** Once the doc is verified on origin
    (`merged: true`, or `up-to-date` with `verified_on_origin`), this command
    also advances the child's Stage to `development` and clears its Pipeline
    Status -- exactly what `pass-gate --unit epic` / `live=False` do, and
    deliberately **not** a `claim`. The child then surfaces as a fresh
    `next-action` / `list-parallel-ready` unit at `development`, so one
    orchestrator pass can return a *mix* of lanes (this child's `development`
    plus the sibling `lld`s it just unblocked) instead of being forced to chain
    straight into development for whichever child's lld finished first. This is a
    scheduling mechanism only, never an "all llds before any development"
    policy -- an independent child still flows lld->development without waiting
    on siblings. See `_advance_after_lld_publish` for the crash-safety argument."""
    issues = {i["number"]: i for i in gh.issue_list()}
    entry = issues.get(issue)
    if entry is None:
        raise GhError(f"issue #{issue} not found in the repo issue list")
    parent = entry.get("parent")
    if not parent:
        return {"issue": issue, "merged": False,
                "reason": "issue has no parent epic — nothing to publish to"}
    parent_number = parent["number"]
    parent_entry = issues.get(parent_number)
    if parent_entry is not None and is_epic_standing(parent_entry):
        return {"issue": issue, "merged": False,
                "reason": f"parent epic #{parent_number} is epic:standing — its children "
                          f"integrate into main, not an epic branch"}
    epic = epic_branch(parent_number)
    doc_path = f"{DOC_ROOT}/issue-{issue}/lld.md"
    src_ref = f"origin/{issue_branch(issue)}"
    with branch_lock(epic), BranchWorkspace(epic, repo_path, runner) as ws:
        result = _publish_lld_doc(gh, ws.path, issue, epic, doc_path, src_ref, runner)
    result = _advance_after_lld_publish(gh, issue, entry, result)
    return _with_workspace(result, ws)


def _advance_after_lld_publish(gh: GitHub, issue: int, entry: dict, result: dict) -> dict:
    """Stage -> `development`, Pipeline Status cleared -- **no claim** -- once
    `_publish_lld_doc` has verified the doc on origin. Returns `result` with
    `advanced`/`next_stage`/`claimed` added.

    Gated on `verified_on_origin`, not on `merged`: the doc reaches origin and
    the field write are two separate side effects, and a crash between them
    must leave a re-run that lands on `up-to-date` still able to advance.
    Gated on the child's Stage being `lld`: a re-run after the advance (or on a
    child already past it) writes nothing -- idempotent, like every other
    marker/field write here.

    Write order matters. Stage is set **before** Pipeline Status is cleared, so
    the only crash-window state is `Stage=development, Pipeline Status=in-progress`,
    which `next-action` reads as `resume` at `development` -- the same work,
    picked up from `origin/issue-<n>` + the published `lld.md`. The other order
    would leave `Stage=lld, Pipeline Status=unset`, which `next-action` would
    read as a *fresh* `lld` and redo a reviewed design. Before this command
    advanced anything, the crash-window state after `merge-lld-doc` was
    `Stage=lld, in-progress` with a clean review marker -- an ambiguous resume
    that the orchestrator had to disambiguate from the thread. Now every
    persisted state maps to exactly one next step."""
    if not result.get("verified_on_origin"):
        return {**result, "advanced": False}
    stage = current_stage(entry)
    if stage != "lld":
        return {**result, "advanced": False,
                "reason_not_advanced": f"Stage is {stage!r}, not 'lld' — nothing to advance"}
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.set_stage_field(issue, "development")
    gh.clear_pipeline_status_field(issue)
    gh.issue_comment(issue,
        f"➡️ `lld-review` clean and `lld.md` published — Stage advanced to `development` "
        f"(not claimed). `next-action` / `list-parallel-ready` pick it up as a fresh unit, "
        f"alongside any sibling `lld` it unblocked.\n\n"
        f"<!-- stage-transition: lld-review->development @ {timestamp} -->")
    return {**result, "advanced": True, "next_stage": "development", "claimed": False}


def _blob_at(repo_path: str, ref: str, path: str, runner: Runner) -> Optional[str]:
    """Blob SHA of `path` at `ref`, or None when the ref has no such path."""
    try:
        return runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                       f"{ref}:{path}"]).strip() or None
    except GhError:
        return None


def _publish_lld_doc(gh: GitHub, epic_path: str, issue: int, epic: str, doc_path: str,
                     src_ref: str, runner: Runner, attempts: int = 2) -> dict:
    """The reconcile loop behind `cmd_merge_lld_doc`. **Every decision is made
    against `origin/<epic>`, never the local tree** -- the 2026-09-12 defect
    (`sdlc_merge_lld_doc_branch_steal_bug`) was exactly the old shape: a doc
    committed on a stale local base, push rejected, and the re-run comparing the
    working tree (which already held the doc) to itself and reporting
    `up-to-date` while `origin/<epic>` never received the file.

    Per attempt: fetch; compare the doc's blob on `origin/issue-<n>` with its
    blob on `origin/<epic>` (identical -> genuinely up-to-date, verified on
    origin); otherwise hard-reset the epic worktree to `origin/<epic>` (a stale
    doc-only commit from a rejected earlier attempt is discarded and replayed;
    anything else unpushed on the local epic branch refuses first), stage the
    doc from `origin/issue-<n>`, commit, push. After the push: fetch again and
    **verify the blob is now on `origin/<epic>`** -- only then is `merged: true`
    reported. A rejected push (origin moved between fetch and push) retries once
    from the new origin tip; still rejected -> a structured `conflict` result
    that says the doc is NOT published. Never a success it did not verify."""
    runner(["git", "-C", epic_path, "fetch", "origin"])
    src_blob = _blob_at(epic_path, src_ref, doc_path, runner)
    if src_blob is None:
        return {"issue": issue, "merged": False, "epic_branch": epic,
                "reason": f"no lld.md on {src_ref} — nothing to publish"}
    if _blob_at(epic_path, f"origin/{epic}", doc_path, runner) == src_blob:
        return {"issue": issue, "merged": False, "epic_branch": epic, "reason": "up-to-date",
                "verified_on_origin": True}
    if runner(["git", "-C", epic_path, "status", "--porcelain"]).strip():
        return {"issue": issue, "merged": False, "epic_branch": epic,
                "reason": f"epic worktree {epic_path} has uncommitted changes — refusing to "
                          f"reset it; commit or stash them, then re-run"}
    # Unpushed local commits on the epic branch: a stale doc-only commit from a
    # rejected earlier attempt is exactly what the reset below replays and is
    # safe to drop; anything touching other paths is somebody's work -- refuse.
    unpushed_files = runner(["git", "-C", epic_path, "diff", "--name-only",
                             f"origin/{epic}...{epic}"]).split()
    if any(f != doc_path for f in unpushed_files):
        return {"issue": issue, "merged": False, "epic_branch": epic,
                "reason": f"local {epic} carries unpushed commits touching "
                          f"{', '.join(f for f in unpushed_files if f != doc_path)} — "
                          f"refusing to reset it; push or discard them, then re-run"}
    last_error = None
    for attempt in range(1, attempts + 1):
        runner(["git", "-C", epic_path, "checkout", "-B", epic, f"origin/{epic}"])
        runner(["git", "-C", epic_path, "checkout", src_ref, "--", doc_path])
        runner(["git", "-C", epic_path, "commit", "-m",
                f"docs(sdlc): publish issue-{issue} lld.md to {epic}", "--", doc_path])
        sha = git_rev_parse_head(epic_path, runner=runner)
        try:
            runner(["git", "-C", epic_path, "push", "origin", epic])
        except GhError as e:
            if not _PUSH_REJECTED_RE.search(str(e)):
                raise
            last_error = str(e).strip().splitlines()[-1] if str(e).strip() else str(e)
            runner(["git", "-C", epic_path, "fetch", "origin"])
            continue
        runner(["git", "-C", epic_path, "fetch", "origin"])
        if _blob_at(epic_path, f"origin/{epic}", doc_path, runner) != src_blob:
            return {"issue": issue, "merged": False, "epic_branch": epic, "conflict": True,
                    "commit": sha,
                    "reason": f"push to {epic} returned success but origin/{epic} does not "
                              f"carry {doc_path} at the published blob — refusing to report "
                              f"merged; inspect origin/{epic} and re-run"}
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        gh.issue_comment(issue,
            f"📄 Published `lld.md` to `{epic}` (`{sha}`) — the low-level design is now durable "
            f"on the epic branch, independent of `{issue_branch(issue)}`, and siblings pick it "
            f"up on their next `sync-branch`.\n\n"
            f"<!-- lld-doc-published: {epic}:{sha} @ {timestamp} -->")
        return {"issue": issue, "merged": True, "epic_branch": epic, "commit": sha,
                "verified_on_origin": True, "attempts": attempt}
    return {"issue": issue, "merged": False, "epic_branch": epic, "conflict": True,
            "reason": f"push to {epic} rejected {attempts}× in a row — the epic branch keeps "
                      f"advancing under this command; lld.md is NOT on origin/{epic}. "
                      f"Re-run once the branch is quiet. Last git error: {last_error}"}


def _complete_epic_architecture(gh: GitHub, epic_number: int, note: str) -> dict:
    """Marks an epic's own Product/Architecture phase complete -- its children
    become eligible for `lld` onward starting the next `next-action` run. Clears
    the epic's own Stage/Pipeline Status fields (an architected epic has no
    "current stage" of its own anymore, same spirit as a merged issue) and adds
    the `epic:architected` label, which `next-action`/`default_stage` read
    straight off the bulk issue list rather than an extra per-epic call. Safe to
    call more than once for the same epic (idempotent field-clear/label-add) --
    this is exactly what a second Gate B round from a deviation found mid-`lld`
    does. See "Epic-level stages" in references/epics.md."""
    gh.clear_stage_and_status_fields(epic_number)
    gh.issue_edit(epic_number, add_labels=[LABELS["architected"]])
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.issue_comment(epic_number,
        f"🏗️ Epic architecture phase complete — {note} Child issues become eligible for "
        f"`lld` onward starting the next `/sdlc-pipeline` pass.\n\n"
        f"<!-- stage-transition: epic-architecture->children @ {timestamp} -->")
    return {"issue": epic_number, "unit": "epic", "epic_architecture_complete": True}


def cmd_pass_gate(gh: GitHub, repo_path: str, issue: int, gate_pr: int, stage: str,
                   unit: str = "issue", runner: Runner = _default_runner,
                   live: bool = True) -> dict:
    """`stage` should be next-action's own returned `stage` field, passed verbatim --
    but since a wrong value here silently corrupts issue fields/comments (it did,
    once, back when this was a label; see the sdlc-next retrospective that added
    this check), self-derive the real stage from the issue's own
    `<!-- gate-pr: stage:pr -->` marker and refuse to proceed on any mismatch,
    rather than trusting the caller-supplied argument blindly.

    `unit="epic"` at `stage="architecture"` is special: passing an epic's own
    architecture gate does not move the epic to `development` (epics don't
    develop) -- it completes the epic's Product/Architecture phase instead,
    handing off to its children. See "Epic-level stages" in references/epics.md.

    `live=True` (the default, used by the orchestrator's own manual "Passing a
    gate" flow) claims the next stage outright -- Pipeline Status -> in-progress,
    start comment posted -- because the orchestrator continues straight into that
    stage's delegation in the same invocation; there's no gap between "gate
    passed" and "work starts". `live=False` (used only by `cmd_auto_pass_gate`,
    which fires in real time the instant a human merges the gate PR on GitHub,
    with no agent running at all) must NOT make that same claim -- it only
    advances the Stage field and clears Pipeline Status back to unset, leaving the
    next actual `/sdlc-pipeline` run to claim the stage for real, whenever that turns
    out to be. Without this distinction, a CI-driven gate merge looks identical to
    a crashed-mid-stage issue (Pipeline Status stuck at in-progress with a stale
    "picking this up" comment nobody acted on) -- `next-action`'s `resume` outcome
    can no longer tell "actually crashed" from "gate advanced by CI, not yet
    started" apart."""
    issue_data = gh.issue_view(issue)
    found = find_gate_pr(issue_data.get("comments", []))
    if not found:
        raise GhError(f"issue #{issue} has no gate-pr marker in its comments -- cannot verify "
                       f"which stage this gate belongs to")
    actual_stage, actual_pr = found
    if actual_pr != gate_pr:
        raise GhError(f"issue #{issue}'s gate-pr marker points at PR #{actual_pr}, not #{gate_pr} "
                       f"-- pass the gate_pr next-action itself returned, not a guessed number")
    if actual_stage != stage:
        raise GhError(f"issue #{issue}'s gate-pr marker says this gate belongs to stage="
                       f"{actual_stage!r}, not stage={stage!r} -- pass next-action's own 'stage' "
                       f"field verbatim; it is the gate's owning doc-stage, not a target you pick")
    branch = f"{unit}-{issue}"
    # A merged per-issue gate landed on `main`, so the issue branch reconciles
    # with `origin/main`. A merged epic gate landed on `epic-<n>` itself (its head
    # was the `epic-<n>-gate-<stage>` sub-branch), so the epic worktree reconciles
    # with `origin/epic-<n>` -- `main` is not involved until `close-epic`. The
    # epic's integration base is still `main`; `sync-branch --unit epic` keeps
    # using it. See "The epic integration branch" in references/epics.md.
    # Under the branch lock, in the branch's own (or an ephemeral) worktree --
    # an epic branch usually has no live worktree at gate-pass time, and this is
    # the command that most often borrowed the main checkout for it (epic #365).
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        git_reconcile_branch(ws.path, branch,
                              base=epic_branch(issue) if unit == "epic" else "main",
                              runner=runner)
    if unit == "epic" and stage == "architecture":
        return _with_workspace(_complete_epic_architecture(
            gh, issue, f"human review confirmed for `architecture.md` — merged via #{gate_pr}."),
            ws)
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if live:
        gh.issue_comment(issue,
            f"✅ Human review confirmed for `{stage}.md` — merged via #{gate_pr} — "
            f"proceeding to `{next_stage}` stage.\n\n"
            f"<!-- stage-transition: human-review:{stage}->{next_stage} @ {timestamp} -->")
        cmd_claim(gh, issue, next_stage)
    else:
        gh.issue_comment(issue,
            f"✅ Human review confirmed for `{stage}.md` — merged via #{gate_pr} — "
            f"Stage advanced to `{next_stage}`. Pick up with `/sdlc-pipeline` whenever "
            f"you're ready to run this stage.\n\n"
            f"<!-- stage-transition: human-review:{stage}->{next_stage} @ {timestamp} -->")
        gh.set_stage_field(issue, next_stage)
        gh.clear_pipeline_status_field(issue)
    return _with_workspace({"issue": issue, "unit": unit, "next_stage": next_stage,
                            "claimed": live}, ws)


GATE_B_SKIP_CONFIDENCE_THRESHOLD = PIPELINE["gates"]["skipConfidenceThreshold"]


def _profile_for_unit(gh: GitHub, issue: int, unit: str) -> dict:
    """The behavioural profile governing `issue`: its own for unit='epic', else its
    parent epic's (a child inherits its epic's profile -- the profile-selecting label,
    e.g. `epic:standing`/`RTB`, lives on the epic, not the child). Falls back to the
    all-defaults profile if the epic can't be resolved."""
    info = gh.issue_epic_info(issue)
    if unit != "epic":
        parent = info.get("parent")
        info = gh.issue_epic_info(parent["number"]) if parent else None
    return resolve_profile(info)


def cmd_skip_gate(gh: GitHub, issue: int, stage: str, confidence: int, summary: str,
                   unit: str = "issue") -> dict:
    """Skip the Gate B human-review PR entirely when arch-review returned a clean
    verdict with high enough self-reported confidence -- see "Human-review gates" in
    SKILL.md. Gate A (product) has no confidence skip; whether it needs a human at all
    is a profile decision (`requiresHumanGateA`), applied by `cmd_auto_pass_gate_a`.

    The confidence bar is per-profile: `resolve_profile(epic).gates.skipConfidenceThreshold`
    (default 95). A standing/RTB profile can lower it (e.g. 90) without touching the
    global default. `unit="epic"` completes the epic's Product/Architecture phase
    directly (see `_complete_epic_architecture`) instead of claiming `development` --
    an epic never develops, its children do."""
    if stage != "architecture":
        raise GhError(f"only the architecture gate (Gate B) may be skipped, got stage={stage!r}")
    threshold = _profile_for_unit(gh, issue, unit)["gates"]["skipConfidenceThreshold"]
    if confidence <= threshold:
        raise GhError(f"confidence {confidence} does not clear the "
                       f"{threshold} threshold required to skip Gate B")
    if unit == "epic":
        return _complete_epic_architecture(
            gh, issue, f"arch-review reported {confidence}% confidence (> "
                       f"{threshold}% threshold) that `architecture.md` is "
                       f"structurally sound — skipped Gate B. {summary}")
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.issue_comment(issue,
        f"⚡ Gate B skipped — arch-review reported {confidence}% confidence "
        f"(> {threshold}% threshold) that `architecture.md` is "
        f"structurally sound. {summary} Proceeding directly to `{next_stage}` without "
        f"human sign-off, per the confidence-skip policy in \"Human-review gates\" "
        f"(references/gates.md).\n\n"
        f"<!-- arch-review-confidence: {confidence} -->\n"
        f"<!-- stage-transition: arch-review->{next_stage} @ {timestamp} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": unit, "next_stage": next_stage, "skipped": True, "confidence": confidence}


def cmd_auto_pass_gate_a(gh: GitHub, issue: int, stage: str, summary: str,
                          unit: str = "issue") -> dict:
    """Advance past Gate A (the product gate) with no human review, when the epic's
    profile says `requiresHumanGateA: false`. Called by the orchestrator only after a
    clean `product-review`; it is the configurable counterpart to Gate A's default
    hard stop. Refuses if the resolved profile still requires a human -- then the
    orchestrator opens a real gate instead.

    Advances `product -> architecture` and claims `architecture` in the same
    invocation (both an epic's own product phase and a standing/RTB child's product
    stage move to `architecture`). See "Gate A configurability" in references/gates.md."""
    if stage != "product":
        raise GhError(f"Gate A is the product gate; got stage={stage!r} -- "
                       f"the architecture gate uses skip-gate, not auto-pass-gate-a")
    profile = _profile_for_unit(gh, issue, unit)
    if profile["gates"]["requiresHumanGateA"]:
        raise GhError(f"profile '{profile['name']}' requires a human at Gate A "
                       f"(requiresHumanGateA: true) -- open a gate, do not auto-pass")
    next_stage = STAGE_AFTER_GATE[stage]  # "architecture"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.issue_comment(issue,
        f"⚡ Gate A auto-passed — profile '{profile['name']}' needs no human review of "
        f"`product.md` (requiresHumanGateA: false). {summary} Proceeding directly to "
        f"`{next_stage}` per the profile's Gate A policy (see \"Gate A configurability\" "
        f"in references/gates.md).\n\n"
        f"<!-- gate-a-auto-passed: {profile['name']} -->\n"
        f"<!-- stage-transition: product-review->{next_stage} @ {timestamp} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": unit, "next_stage": next_stage,
            "auto_passed": True, "profile": profile["name"]}


_CLOSES_ISSUE_RE = re.compile(r"\bCloses #\d+", re.IGNORECASE)
# Matches both gate-branch head shapes: `issue-<n>` (a per-issue gate on a
# standing-epic child, opened against `main`) and `epic-<n>-gate-<stage>` (an
# epic-level Gate A/B sub-branch, opened against `epic-<n>` -- see "Human-review
# gates" in references/gates.md and `epic_gate_branch`). Exactly one of the
# `issue_n`/`epic_n` groups is set; `stage` is set only for the epic shape. A bare
# `epic-<n>` head is deliberately *not* a gate any more -- that is the epic's
# own integration PR (`close-epic`), and matching it would let a merged epic
# close fire the gate backstop. Before 2026-08-20 this only matched `issue-<n>`;
# from then until 2026-09-06 it also matched the bare `epic-<n>` head an epic
# gate used to be opened from (see references/history.md).
_GATE_BRANCH_RE = re.compile(
    rf"^(?:{re.escape(ISSUE_BRANCH_PREFIX)}(?P<issue_n>\d+)"
    rf"|{re.escape(EPIC_BRANCH_PREFIX)}(?P<epic_n>\d+){re.escape(GATE_BRANCH_SUFFIX)}"
    rf"(?P<stage>product|architecture))$")
# GitHub's own convention for a bot account's login (e.g. "github-actions[bot]") --
# used by `cmd_mark_feedback_received` to ignore automated comments/reviews (most
# importantly this workflow's own prior runs, and any other bot integration on the
# repo), so a bot commenting on a gate PR never flips Pipeline Status or fires a
# second webhook run in a loop.
_BOT_AUTHOR_RE = re.compile(r"\[bot\]$")


class _NotAGate(Exception):
    """Internal control-flow signal used only within `_match_open_gate` /
    `cmd_auto_pass_gate` below: `pr` does not match a currently open gate for any
    issue. Carries the human-readable skip reason; caught once, right where it's
    raised from, and never escapes `cmd_auto_pass_gate` itself."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _match_open_gate(gh: GitHub, pr: dict, pr_number: int) -> tuple:
    """Shared identification logic for `cmd_auto_pass_gate`'s two branches (a merged
    gate PR, or one closed without merging) and `cmd_mark_feedback_received` --
    everything about deciding *whether* `pr` is some issue's currently open gate PR
    is identical across all three call sites; only what happens after a match
    differs. "Currently open" means Pipeline Status is either
    `awaiting-human-review` or `feedback-received` (see `GATE_PENDING_STATUSES`) --
    the latter is purely a visibility flip on top of the former, not a second,
    independent state a gate PR could be closed/merged out of. Returns
    `(issue_number, marker_stage, status, unit)` on a clean match -- `status` is the
    issue's exact current Pipeline Status value, for a caller (like
    `cmd_mark_feedback_received`) that needs to distinguish the two pending states;
    `unit` is "issue" (an `issue-<n>` head branch, based on `main`) or "epic" (an
    `epic-<n>-gate-<stage>` head branch based on `epic-<n>`, i.e. an epic-level
    Gate A/B) -- or raises `_NotAGate(reason)`."""
    m = _GATE_BRANCH_RE.match(pr.get("headRefName") or "")
    if not m:
        raise _NotAGate(f"PR #{pr_number} head branch {pr.get('headRefName')!r} is not "
                         f"an issue-<n> or epic-<n>-gate-<stage> branch")
    if m.group("issue_n") is not None:
        unit, issue_number = "issue", int(m.group("issue_n"))
        expected_base = "main"
    else:
        unit, issue_number = "epic", int(m.group("epic_n"))
        expected_base = epic_branch(issue_number)
    if pr.get("baseRefName") != expected_base:
        raise _NotAGate(f"PR #{pr_number} base is {pr.get('baseRefName')!r}, not "
                         f"{expected_base}")

    if _CLOSES_ISSUE_RE.search(pr.get("body") or ""):
        raise _NotAGate(f"PR #{pr_number} body contains 'Closes #' -- this is the "
                         f"development PR (opened/closed by the pipeline itself), not "
                         f"a human-reviewed gate PR")

    issue_data = gh.issue_view(issue_number)
    fields = gh.issue_fields(issue_number)
    status = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    if status not in GATE_PENDING_STATUSES:
        raise _NotAGate(f"issue #{issue_number} is not currently awaiting-human-review "
                         f"or feedback-received (Pipeline Status={status!r}) -- no open "
                         f"gate for this PR")

    found = find_gate_pr(issue_data.get("comments", []))
    if not found:
        raise _NotAGate(f"issue #{issue_number} has no gate-pr marker comment")
    marker_stage, marker_pr = found
    if marker_pr != pr_number:
        raise _NotAGate(f"issue #{issue_number}'s open gate marker points at PR "
                         f"#{marker_pr}, not #{pr_number} -- not the currently open gate")
    if unit == "epic" and m.group("stage") != marker_stage:
        # The marker is the authority on which stage a gate belongs to; a head
        # branch named for the other stage is not the gate the epic is waiting on.
        raise _NotAGate(f"PR #{pr_number} head branch {pr.get('headRefName')!r} is a "
                         f"{m.group('stage')} gate branch but epic #{issue_number}'s open "
                         f"gate marker is for stage {marker_stage!r}")

    return issue_number, marker_stage, status, unit


def cmd_auto_pass_gate(gh: GitHub, repo_path: str, pr_number: int,
                        runner: Runner = _default_runner) -> dict:
    """Entry point for `.github/workflows/gate-auto-advance.yml` -- reacts to any
    closed PR in the repo (merged or not) and, if (and only if) it recognizes it as a
    currently-open gate PR (see "Human-review gates" in references/gates.md), drives the same
    deterministic field mutation a human running `/sdlc-pipeline` by hand would reach --
    but never the "claim this stage now" side effect, since no agent is actually
    about to run:
    - **Merged** -> calls `cmd_pass_gate` with `live=False`: advances the Stage
      field but leaves Pipeline Status unset (not in-progress) and skips the start
      comment, since nothing is actually about to work this stage in real time --
      that only happens once a live `/sdlc-pipeline` run claims it for real. See
      `cmd_pass_gate`'s own `live` docstring for why this distinction exists.
    - **Closed without merging** -> calls the same `cmd_mark_needs_human` logic --
      see "Edge cases" under "Human-review gates" in references/gates.md: a bare close carries
      no actionable content to revise against, so this is `status:needs-human`, not
      a guess.

    Either way, this fires the moment the human acts, instead of waiting for the next
    manual `/sdlc-pipeline` invocation to notice via `check-gate`. This is a real-time
    backstop alongside that existing detection, not a replacement for it --
    `next-action` stays fully idempotent: once this has already advanced the fields
    (or marked the issue needs-human), it simply finds nothing left to do there.

    Deliberately conservative: this fires for *every* closed PR in the repo (the
    workflow can't filter more precisely at the trigger level), so every branch below
    that isn't a clean, unambiguous gate-PR match returns a `skipped` result with no
    mutation, never an error -- a still-open PR, a wrong-base, non-gate-headed, or
    `Closes #`-carrying (development / epic-integration) PR is all completely normal traffic
    here, not a problem to surface. Uses `dict.get("ok", ...)` for its result
    convention (checked by `main()` below to set the exit code) rather than raising
    GhError on a mismatch -- unlike every other subcommand, a "not a gate PR" outcome
    here is the *expected* common case, not an operational failure."""
    try:
        pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    except GhError as e:
        return {"ok": False, "reason": f"could not fetch PR #{pr_number}: {e}"}

    merged = pr.get("state") == "MERGED" and bool(pr.get("mergedAt"))
    closed_unmerged = pr.get("state") == "CLOSED" and not merged
    if not merged and not closed_unmerged:
        return {"ok": True, "skipped": f"PR #{pr_number} is not closed "
                                        f"(state={pr.get('state')!r})"}

    try:
        issue_number, marker_stage, _status, unit = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    try:
        if merged:
            # `unit` comes off the head branch (`issue-<n>` vs
            # `epic-<n>-gate-<stage>`), so a merged epic-level Gate B correctly routes through
            # `_complete_epic_architecture` instead of advancing the epic to a
            # stage it never runs.
            result = cmd_pass_gate(gh, repo_path, issue_number, pr_number, marker_stage,
                                    unit=unit, runner=runner, live=False)
        else:
            result = cmd_mark_needs_human(
                gh, issue_number,
                reason=f"Gate PR #{pr_number} closed without merging — human rejected "
                       f"the doc, needs direct operator input.",
                repo_path=repo_path)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    result["ok"] = True
    return result


def cmd_mark_feedback_received(gh: GitHub, pr_number: int, author: str, body: str = "") -> dict:
    """Entry point for `.github/workflows/gate-auto-advance.yml`'s
    `pull_request_review`/`issue_comment` triggers -- flips Pipeline Status forward
    from `awaiting-human-review` to `feedback-received` the instant a human leaves
    real feedback (a review comment, a review carrying a body, or a plain PR/issue
    comment) on a currently open gate PR. Purely a visibility marker closing the gap
    where Pipeline Status sits at `Awaiting Human Review` for the entire time a gate
    PR is open, indistinguishable from "nothing has happened yet" -- `evaluate_gate`'s
    own feedback_pending detection (unresolved review threads / new plain comments,
    re-derived live from the PR every time) remains the sole authority on whether
    there's actually something to address; this never substitutes for that.

    Deliberately conservative, same spirit as `cmd_auto_pass_gate`: every branch that
    isn't a clean "human left real feedback on a currently-awaiting-human-review gate
    PR" match returns a `skipped` result with no mutation, never an error -- most
    traffic here (a bot comment, an approval with no body, a comment on some
    unrelated PR, a second comment once already `feedback-received`) is completely
    normal, not a problem to surface."""
    if _BOT_AUTHOR_RE.search(author or ""):
        return {"ok": True, "skipped": f"comment/review author {author!r} is a bot"}
    if not (body or "").strip():
        return {"ok": True, "skipped": "no body text on this review/comment -- not "
                                        "actual feedback (e.g. an approval with no comment)"}

    try:
        pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    except GhError as e:
        return {"ok": False, "reason": f"could not fetch PR #{pr_number}: {e}"}

    try:
        issue_number, _marker_stage, status, _unit = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    if status != "awaiting-human-review":
        # Only ever flip forward from awaiting-human-review -- a second comment while
        # already feedback-received is a clean no-op (not an error, and not a second
        # comment posted to the issue), and this never touches needs-human or any
        # other status _match_open_gate wouldn't have matched in the first place.
        return {"ok": True, "skipped": f"issue #{issue_number} Pipeline Status is "
                                        f"{status!r}, not awaiting-human-review -- "
                                        f"nothing to flip forward"}

    try:
        gh.set_pipeline_status_field(issue_number, "feedback-received")
        gh.issue_comment(issue_number,
            f"💬 Feedback landed on gate PR #{pr_number} — now awaiting pipeline action "
            f"(`Feedback Received`). The `address-gate-feedback` step picks this up "
            f"automatically; see \"Human-review gates\" in the pipeline docs.")
    except GhError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "issue": issue_number, "gate_pr": pr_number,
            "pipeline_status": "feedback-received"}


def cmd_mark_feedback_addressed(gh: GitHub, issue: int) -> dict:
    """Flips Pipeline Status back to `awaiting-human-review` -- the mirror of
    `cmd_mark_feedback_received`'s forward flip. Run as the last step of "Addressing
    gate feedback" in SKILL.md, unconditionally, once the revision is committed and
    pushed: whether or not the forward flip ever actually fired (e.g. the Action
    didn't run for some reason, or feedback was only noticed via `check-gate`), the
    gate is once again simply awaiting human review, so this sets the field directly
    rather than checking the current value first -- same unconditional-write spirit
    as `cmd_claim`. Deliberately does not itself post a comment: "Addressing gate
    feedback" step 5 already has the fresh stage agent post its own comment
    summarizing what changed, and a second, generic comment here would just be
    noise on top of that."""
    gh.set_pipeline_status_field(issue, "awaiting-human-review")
    return {"issue": issue, "pipeline_status": "awaiting-human-review"}


def cmd_pause_for_epic_regate(gh: GitHub, issue: int, epic: int, gate_pr: int) -> dict:
    """Parks a task issue whose `lld` found an architecture deviation while the
    owning epic's re-gate (a second Product/Architecture review round -- see
    "Epic-level deviation escalation" in references/epics.md) is pending. Clears only
    Pipeline Status (Stage stays `lld`) so the task re-enters the normal
    per-child eligibility loop -- rather than sitting in the crash-recovery
    in-progress slot forever -- the moment the epic's re-gate merges and
    `next-action` runs again."""
    gh.clear_pipeline_status_field(issue)
    gh.issue_comment(issue,
        f"⏸️ Paused — `lld` found this doesn't fit epic #{epic}'s current architecture. "
        f"Epic #{epic}'s architecture is being revised; see gate PR #{gate_pr}. This task "
        f"resumes automatically once that gate merges.")
    return {"issue": issue, "paused_for_epic_regate": epic, "gate_pr": gate_pr}


def cmd_open_dev_pr(gh: GitHub, issue: int, title: str, body: str, summary: str) -> dict:
    """Opens the development draft PR -- or, if one is already open on this
    branch, reports it instead of opening a second.

    The duplicate guard exists because a resumed `development` agent, or a
    concurrent session that got there first, has no reliable way to know a PR
    already exists; opening a second one splits review history across two PRs
    for one branch. Reported as a structured exit-0 result with
    `created: False`, not an error -- an already-open PR is the normal, correct
    state to continue from."""
    existing = gh.pr_list_for_branch(issue_branch(issue))
    if existing:
        pr_number = existing[0]["number"]
        return {"issue": issue, "pr": pr_number, "created": False,
                "reason": f"PR #{pr_number} is already open on {issue_branch(issue)} -- "
                          f"reusing it rather than opening a duplicate"}
    base = integration_base(gh, issue)
    pr_number = gh.pr_create(base=base, head=issue_branch(issue), title=title,
                              body=f"{body}\n\nCloses #{issue}", draft=True)
    gh.set_stage_field(issue, "pr-review")
    gh.issue_comment(issue,
        f"✅ {summary} Draft PR: #{pr_number} — what was built and why is in the PR "
        f"description.\n\n"
        f"Not yet queued for review: `development` still owes `record-local-ci` per "
        f"suite it ran and `handoff-to-pr-review`, which posts the marker "
        f"`list-ready-for-review` reads.")
    return {"issue": issue, "pr": pr_number, "created": True}


# --- citations: cite / verify-citations / verify-exit's citations_ok ---
#
# A citation block is a fenced code block whose info line names a file path
# (and, optionally, a pinned revision) and whose body is a byte-exact fragment
# copied out of that file. The body is the authority; a line number is never
# stored, only derived at resolve time as a hint (see issue #194's
# architecture.md, "Design"). Two kinds of block share one info-string token:
# `cite` is *asserted* -- the resolver resolves and counts it; `cite-example`
# is *illustrative* -- parsed so a spec of the format can show one, but never
# resolved or counted. The token must match exactly, so `cite-example` is
# never misread as a `cite` block with a stray word.

_CITE_FENCE_RE = re.compile(r'^(`{3,})(cite-example|cite)\s+(.*?)\s*$')


def _parse_cite_attrs(attr_str: str) -> dict:
    return dict(tok.split("=", 1) for tok in attr_str.split() if "=" in tok)


def parse_cite_blocks(text: str) -> list:
    """Parses every `cite`/`cite-example` block out of a document's text.
    Returns one dict per block: `{kind, path, rev, body, line_no}` in document
    order, where `line_no` is the 1-based line number of the block's first
    body line (a hint for a human reader, never used for resolution). A
    fence's closing line is any run of backticks at least as long as the
    opening one -- `cite` always emits an exact-length match, but a body that
    itself contains a run of backticks needs the opening fence to be longer
    still (see `cmd_cite`'s fence-length choice), which this closing rule
    accommodates without special-casing it."""
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
    """Resolves one citation's body against the real file: the working tree,
    or `git show <rev>:<path>` when a revision is pinned. Matching is a fixed-
    string (`str.find`/`.count`) search, never a regex, so a body containing
    `$ { } * \\`` resolves against exactly those bytes with no escaping burden
    on the author (architecture.md AC9). Never raises -- a missing file, bad
    path, or bad rev is reported as unresolved with the reason, per
    architecture.md's "Where the resolver can fail is the point of the
    design": the failure that matters is reported data, not a crash."""
    result = {"path": path, "rev": rev}
    try:
        if rev:
            content = runner(["git", "-C", repo_path, "show", f"{rev}:{path}"])
        else:
            with open(os.path.join(repo_path, path), "r") as f:
                content = f.read()
    except (GhError, OSError) as e:
        result["resolved"] = False
        result["cited_vs_found"] = (f"could not read {path}"
                                     f"{f' at rev {rev}' if rev else ''}: {e}")
        return result
    count = content.count(body) if body else 0
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
    """Shared resolver core: parses `text`, resolves every *asserted* block
    against `repo_path`, and skips every *illustrative* one. Both
    `cmd_verify_citations` (a document at a time) and `cmd_verify_exit`'s
    `citations_ok` (the single record a completing stage authored) build on
    this one function, so a block that resolves in one resolves in the
    other."""
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
    """Generates a citation block by reading the real file (or
    `git show <rev>:<path>`) and selecting the target fragment by exact line,
    an inclusive line range, or the first line literally containing `match`.
    This is the structural anti-fabrication property (architecture.md AC7):
    there is nothing to copy for content that is not actually in the file, so
    a citation for it cannot be produced. Emits nothing (no `block` key,
    `ok: False` instead) on any error -- an out-of-range line/range, or a
    `match` that finds zero or several lines and is therefore ambiguous."""
    try:
        if rev:
            content = runner(["git", "-C", repo_path, "show", f"{rev}:{path}"])
        else:
            with open(os.path.join(repo_path, path), "r") as f:
                content = f.read()
    except (GhError, OSError) as e:
        return {"ok": False,
                "reason": f"could not read {path}{f' at rev {rev}' if rev else ''}: {e}"}
    file_lines = content.splitlines()
    if line is not None:
        start = end = line
    elif lines is not None:
        a, b = lines.split("-", 1)
        start, end = int(a), int(b)
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
    # Fence longer than any backtick run in the body, so arbitrary file
    # content (including a body that itself contains ``` ```) round-trips
    # without the fence being mistaken for a close inside the body.
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
    """Re-resolves every asserted citation in one or more documents. A single
    document's result is returned flat (`document`/`citations`/
    `examples_skipped`/`all_resolved`, per architecture.md's Interfaces);
    several documents are wrapped under `documents`, with an aggregate
    `all_resolved` across all of them. `ok` mirrors `all_resolved` either way,
    so the CLI dispatch's `result.get("ok", True)` exit-code contract applies
    with no new plumbing."""
    documents = []
    for p in paths:
        with open(os.path.join(repo_path, p), "r") as f:
            text = f.read()
        doc_result = verify_citations_text(text, repo_path=repo_path, runner=runner)
        doc_result = {"document": p, **doc_result}
        documents.append(doc_result)
    all_ok = all(d["all_resolved"] for d in documents)
    result = dict(documents[0]) if len(documents) == 1 else {"documents": documents,
                                                              "all_resolved": all_ok}
    result["ok"] = all_ok
    return result


# The canonical filename of the single record a completing stage authored --
# `cmd_verify_exit`'s `citations_ok` resolves only this file (architecture.md
# AC12/AC14: "the completing stage's *own* record", scoped so an older
# doc's rotted citation never fails a later stage). `testing` and the review
# roles write no doc file at all and are deliberately absent from this map --
# there is nothing to re-check for them, so `citations_ok` is simply omitted.
STAGE_RECORD_FILENAMES = {
    "product": "product.md",
    "architecture": "architecture.md",
    "lld": "lld.md",
}


def cmd_verify_exit(gh: GitHub, repo_path: Optional[str], issue: int, expect_stage: str,
                     pr: Optional[int] = None, unit: str = "issue",
                     runner: Runner = _default_runner) -> dict:
    """One-call post-handoff check: does the issue carry the expected native Stage
    field value, are the canonical per-issue docs present on disk, what are the
    last few commits, and (when a PR is in play) is it still a draft on the right
    branches. Replaces the hand-typed `gh issue view` + `gh pr view` + `ls` +
    `git log` sequence the orchestrator otherwise repeats identically after every
    dev/testing handoff."""
    repo_path = resolve_repo_path(repo_path, f"{unit}-{issue}", runner=runner)
    issue_data = gh.issue_view(issue)
    labels = label_names(issue_data)
    fields = gh.issue_fields(issue)
    actual_stage = STAGE_FIELD_NAMES.get(fields.get("Stage"))
    actual_status = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    result = {"issue": issue, "labels": sorted(labels), "stage": actual_stage,
              "pipeline_status": actual_status, "expected_stage_present": actual_stage == expect_stage}
    # Exit nonzero when the Stage field does not say what the caller expected.
    # This used to be reported and read past: #238 returned
    # `expected_stage_present: false` and the orchestrator dispatched the next
    # stage anyway, so the pipeline advanced on an unverified handoff. A result
    # nobody is forced to look at is not a check.
    #
    # `REVIEW_ROLES` are excluded because they legitimately have no Stage value
    # of their own -- a review runs immediately after the stage before it and
    # inherits that stage's value (see the lifecycle model in SKILL.md). Asking
    # `--expect-stage pr-review` is therefore always misuse, and is reported as
    # such rather than as a pipeline failure, so the two cases stay
    # distinguishable.
    if expect_stage in REVIEW_ROLES:
        result["ok"] = False
        result["misuse"] = (f"`{expect_stage}` is a review role and has no Stage field value of "
                            f"its own -- it inherits the preceding stage's. Verify a review by "
                            f"its marker (`record-pr-review` / the arch-review confidence "
                            f"marker), not by --expect-stage.")
    elif not result["expected_stage_present"]:
        result["ok"] = False
        result["reason"] = (f"Stage is {actual_stage!r}, expected {expect_stage!r} -- the "
                            f"previous stage's exit action did not run, or ran against a "
                            f"different issue. Do not dispatch the next stage until this is "
                            f"resolved; advancing on an unverified handoff is how a stage's "
                            f"evidence ends up existing only in one session's memory.")
    if pr is not None:
        pr_data = gh.pr_view(pr, fields="isDraft,headRefName,baseRefName")
        result["pr"] = pr
        result["pr_is_draft"] = pr_data.get("isDraft")
        result["pr_head"] = pr_data.get("headRefName")
        result["pr_base"] = pr_data.get("baseRefName")
    docs_dir = os.path.join(repo_path, DOC_ROOT, f"{unit}-{issue}")
    result["docs_present"] = sorted(os.listdir(docs_dir)) if os.path.isdir(docs_dir) else []
    # Citation gate: re-checks only the single record the completing stage
    # itself authored (never the whole docs_dir) -- an older, already-merged
    # doc's rotted citation must not fail a later stage's own exit check
    # (architecture.md AC14). A stage with no canonical record (`testing`,
    # the review roles) or whose record isn't on disk yet is left alone --
    # `docs_present` already surfaces a missing doc; this gate only ever
    # fires once there is an actual record to re-check.
    record_filename = STAGE_RECORD_FILENAMES.get(expect_stage)
    if record_filename:
        record_path = os.path.join(docs_dir, record_filename)
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
    log = runner(["git", "-C", repo_path, "log", "--oneline", "-5"]).strip()
    result["recent_commits"] = log.splitlines() if log else []
    return result


def checks_status(checks: list) -> str:
    if not checks:
        return "passed"
    buckets = {c["bucket"] for c in checks}
    if "fail" in buckets or "cancel" in buckets:
        return "failed"
    if buckets - {"pass", "skipping"}:
        return "pending"
    return "passed"


# Workflows whose run is REQUIRED when a PR touches their paths. `workflow` must
# match the workflow file's `name:` exactly -- if you rename a workflow, rename it
# here in the same commit. Mirrors each workflow's own `paths` filter; keep in step.
#
# `suite` names the `local-ci` attestation that stands in for a GHA check on a child
# PR. As of 2026-09-04 these two workflows are main-only in GitHub Actions (they no
# longer run on child PRs; see each workflow's `on:` block), so on a child PR the
# suite is proven by a `record-local-ci --suite <suite>` attestation instead -- see
# `local_ci_suites_attested` and `references/operations.md`, "Local-CI attestation".
REQUIRED_WORKFLOWS = tuple(
    {"workflow": w["workflow"], "suite": w["suite"],
     "prefixes": tuple(w["prefixes"]), "files": tuple(w.get("files", ()))}
    for w in CONFIG["requiredWorkflows"]
)


def missing_required_workflows(changed_files: list, checks: list,
                               comments: list = None, head_sha: str = None) -> list:
    """Required workflows a PR touches whose suite has neither passed in GHA nor
    been locally attested for the current head.

    Deliberately NOT a total-check-count test. A count is satisfied by *any*
    workflow reporting -- notification-taxonomy-guard.yml, say -- which says
    nothing about whether the backend/frontend suite actually ran. Requiring at
    least one `pass` bucket *from the named workflow* also covers the "every job
    skipped" variant, which checks_status() reads as green.

    A suite is satisfied by EITHER a passing GHA check from its workflow OR a
    fresh `local-ci` attestation for its `suite` (see `local_ci_suites_attested`).
    The second path exists because these workflows went main-only for cost: they
    don't run on a child PR at all, so on a child PR the local attestation is the
    only proof there is -- and it counts only when its sha matches `head_sha`, so a
    stale local run cannot satisfy the gate. `comments`/`head_sha` default to
    None/empty (the GHA-only behaviour) so existing 2-arg callers are unchanged."""
    passing = {c.get("workflow", "") for c in checks if c.get("bucket") == "pass"}
    attested = local_ci_suites_attested(comments or [], head_sha)
    missing = []
    for spec in REQUIRED_WORKFLOWS:
        touched = any(p.startswith(spec["prefixes"]) or p in spec["files"] for p in changed_files)
        if not touched:
            continue
        if spec["workflow"] in passing:
            continue
        if spec.get("suite") in attested:
            continue
        missing.append(spec["workflow"])
    return missing


def touches_pipeline_config(changed_files: list) -> bool:
    """True when a PR changed the pipeline's own config file in the driven repo.

    The skill itself lives outside the repo it drives, so only its config
    (`CONFIG_FILENAME`) ships through this repo's own merges. A merge that changes
    it can change how every later command behaves in the same invocation, so
    surfacing it as a merge-pr result field (`config_changed`) makes re-reading the
    config mechanical rather than something the orchestrator must remember."""
    return any(os.path.basename(f) == CONFIG_FILENAME for f in changed_files)


def merge_gate_status(changed_files: list, checks: list,
                      comments: list = None, head_sha: str = None) -> tuple:
    """Single source of truth for "is this PR mergeable", shared by pr-checks
    (surfaces the problem during review) and merge-pr (enforces it). Returns
    the underlying pending/failed status verbatim rather than reporting
    missing-checks over it -- a still-running required workflow reads as
    `pending` (retryable), not as a configuration error.

    `missing` is always computed and returned, regardless of `status` -- a PR
    touching two required trees where one fails/is-pending and the *other's*
    workflow never reported at all must surface both facts together. Discarding
    `missing` whenever status != "passed" would hide that second, genuinely
    different problem (a config defect, not a code failure) behind whichever
    check happened to fail or still be running -- exactly the kind of masked
    signal this gate exists to prevent."""
    status = checks_status(checks)
    missing = missing_required_workflows(changed_files, checks, comments, head_sha)
    if status != "passed":
        return status, missing
    return ("missing-checks" if missing else "passed"), missing


def cmd_pr_checks(gh: GitHub, pr_number: int) -> dict:
    checks = gh.pr_checks(pr_number)
    # A required suite (backend/frontend) can be satisfied by a `local-ci`
    # attestation on this PR instead of a GHA check -- both went main-only for cost
    # and no longer run on a child PR. Read the PR's comments + head SHA so the
    # merge gate can honour a fresh attestation (see `local_ci_suites_attested`).
    view = gh.pr_view(pr_number, "comments,headRefOid")
    status, missing = merge_gate_status(
        gh.pr_files(pr_number), checks,
        view.get("comments", []), view.get("headRefOid"))
    return {"pr": pr_number, "status": status,
            "missing_required_workflows": missing, "checks": checks}


def cmd_merge_pr(gh: GitHub, pr_number: int, issue: int, repo_path: str = ".") -> dict:
    """Squash-merges the PR. Does **not** touch the Stage/Pipeline Status fields
    itself, even when this closes the tracking issue -- that's
    `cmd_mark_issue_closed`'s job now, fired in real time by
    `.github/workflows/gate-auto-advance.yml` on the `issues: closed` event this
    merge triggers (via the PR's `Closes #<n>`). Moved 2026-08-20, per operator
    instruction: doing it here only ever covered issues closed *through*
    `merge-pr` specifically, silently missing an issue a human closed by hand;
    the same `issues: closed` trigger, calling `cmd_mark_issue_closed`, covers
    every closure path uniformly.

    Also refuses -- as a structured exit-0 result, not an error -- when the
    branch is *behind* `origin/main`. Green CI on a stale base proves nothing
    about the combined state: with the parallel dev lane, two sibling PRs can
    each be green independently yet break `main` together (a semantic conflict
    no textual merge check catches). The orchestrator's fix is mechanical:
    `sync-branch` (merges origin/main in, pushing re-triggers CI), wait for
    green, re-run `merge-pr`."""
    base = integration_base(gh, issue)
    behind = gh.branch_behind_by(issue_branch(issue), base=base)
    if behind:
        return {"pr": pr_number, "issue": issue, "merged": False, "behind_base": behind,
                "base": base,
                "reason": f"branch {issue_branch(issue)} is {behind} commit(s) behind {base} -- CI ran "
                          f"on a stale base; run sync-branch, wait for fresh CI green, then "
                          f"re-run merge-pr"}
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
    checks = gh.pr_checks(pr_number)
    files = gh.pr_files(pr_number)
    # A required suite that went main-only (backend/frontend) is proven on a child
    # PR by a fresh `local-ci` attestation instead of a GHA check -- read from the
    # PR thread, honoured only when its sha matches the current head (a stale local
    # run must not merge). See `local_ci_suites_attested` / `record-local-ci`.
    view = gh.pr_view(pr_number, "comments,headRefOid")
    status, missing = merge_gate_status(
        files, checks, view.get("comments", []), view.get("headRefOid"))
    if status != "passed":
        detail = f" (no passing check or fresh local-ci attestation from: {', '.join(missing)})" if missing else ""
        raise GhError(f"PR #{pr_number} checks not passed (status={status}){detail}")
    gh.pr_ready(pr_number)
    gh.pr_merge(pr_number)
    gh.pr_comment(pr_number, f"Auto-merged under the pipeline's scoped PR-merge override — "
                              f"see \"PRs merge automatically\" in the pipeline docs.")
    issue_state = gh.issue_view(issue)["state"]
    issue_closed = issue_state == "CLOSED"
    if not issue_closed and base != "main":
        # Merging into an epic branch does NOT fire the PR's `Closes #<n>` -- GitHub
        # only auto-closes an issue when its PR merges to the default branch. Close
        # the child explicitly so it cannot linger open: a merged-but-open child
        # phantom-resumes next-action (it re-delegates a done issue) and blocks the
        # epic's all-children-closed gate. The reactive gate-auto-advance.yml
        # `issues: closed` trigger then syncs its Stage/Pipeline Status fields, and
        # `mark-issue-closed` stays available for the field-sync-only path. Replaces
        # a two-step (pr-review manually running `gh issue close` after merge) that
        # a session dying mid-close silently skipped. Added 2026-09-06 retro.
        gh.issue_close(issue)
        issue_closed = True
    if issue_closed:
        gh.issue_comment(issue, f"Merged via #{pr_number}.")
    # Merged is the terminal stopping point -- release the worktree here rather
    # than in cmd_mark_issue_closed, which runs in CI where no dev worktree
    # exists. Everything is on origin by definition at this point, so the
    # unpushed/dirty guards in release_worktree should be no-ops.
    return {"pr": pr_number, "issue": issue, "merged": True, "issue_closed": issue_closed,
            "config_changed": touches_pipeline_config(files),
            "worktree": release_worktree(issue_branch(issue), base_repo=repo_path, runner=gh._run)}


def _release_unit_worktree(issue: int, base_repo: str = ".",
                            runner: Runner = _default_runner) -> dict:
    """Release whichever of `issue-<n>` / `epic-<n>` currently has a worktree --
    parking commands take a unit number without knowing which kind it is."""
    for branch in (issue_branch(issue), epic_branch(issue)):
        result = release_worktree(branch, runner=runner, base_repo=base_repo)
        if result.get("released") or result.get("reason") != "no worktree":
            return {**result, "branch": branch}
    return {"released": False, "reason": "no worktree"}


def cmd_mark_blocked(gh: GitHub, issue: int, dep: int, repo_path: str = ".") -> dict:
    """Records the dependency as a native blockedBy relationship (not a label or a
    body-text edit) -- next-action's decide_next_action derives blocked-ness
    straight from this relationship, so it auto-clears on its own once `dep`
    closes, with no separate dependency-cleared step to run.

    Also releases the unit's worktree (see `release_worktree`): a blocked unit
    is a stopping point, and a worktree left behind at one consumes a dev-lane
    slot that nothing is working in.

    Also resets Pipeline Status to `todo`. Blocked-ness itself is derived from the
    native relationship, not the field -- but leaving the field at `in-progress` makes
    a parked unit indistinguishable from a crashed run to both `decide_next_action`
    and `list_parallel_ready`, whose shared heuristic is "in-progress with no
    worktree". On 2026-09-03 #310 sat blocked all session while `next-action` kept
    offering it as a `resume`. There is no `blocked` option on the field; `todo` is
    the honest one, and it is also correct the moment `dep` closes."""
    gh.add_blocked_by(issue, dep)
    gh.set_pipeline_status_field(issue, "todo")
    gh.issue_comment(issue, f"⏸️ Blocked — waiting on #{dep} to merge.")
    return {"issue": issue, "status": "blocked", "on": dep,
            "worktree": _release_unit_worktree(issue, base_repo=repo_path, runner=gh._run)}


def cmd_mark_needs_human(gh: GitHub, issue: int, reason: str, repo_path: str = ".") -> dict:
    """Parks the unit for the operator, and releases its worktree (see
    `release_worktree`) so the abandoned checkout stops consuming a dev-lane
    slot -- the failure mode that silently stalled the lane on 2026-08-20."""
    gh.set_pipeline_status_field(issue, "needs-human")
    gh.issue_comment(issue, f"🙋 Needs human input — {reason}")
    return {"issue": issue, "status": "needs-human",
            "worktree": _release_unit_worktree(issue, base_repo=repo_path, runner=gh._run)}


def cmd_mark_todo(gh: GitHub, issue: int) -> dict:
    """Sets Pipeline Status to `todo` on a freshly opened issue -- the one native
    option this pipeline never otherwise writes (see "Issue taxonomy" in
    SKILL.md), added purely so a new issue shows *some* Pipeline Status on the
    board instead of a blank field, before anything else has claimed it. Skips
    cleanly (no mutation) if the issue already has any Pipeline Status value --
    covers the workflow re-firing, or `issues: opened` landing after some other
    process (e.g. a fast first `/sdlc-pipeline` claim) already set a real status;
    never clobber an already-meaningful value with `todo`."""
    fields = gh.issue_fields(issue)
    current = PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status"))
    if current is not None:
        return {"issue": issue, "skipped": f"Pipeline Status already {current!r}"}
    gh.set_pipeline_status_field(issue, "todo")
    return {"issue": issue, "pipeline_status": "todo"}


def cmd_mark_issue_closed(gh: GitHub, issue: int) -> dict:
    """Marks `issue` done -- called by .github/workflows/gate-auto-advance.yml
    the instant *any* issue in the repo closes (`issues: closed`), regardless of
    why: a `merge-pr` squash-merge auto-closing it via `Closes #<n>`, a human
    closing it directly, or an epic closing once its children are all done --
    the epic-integration PR's `Closes #<n>` fires this on merge whether a human
    or (when `pipeline.epicClose.auto`) the orchestrator triggered `close-epic`
    (see "Epic closing" in SKILL.md). Added 2026-08-20, per operator instruction, replacing the old
    approach of `cmd_merge_pr` synchronously deleting both fields itself --
    that only ever covered issues closed *through* the pipeline's own
    `merge-pr` call, silently missing any issue a human closed by hand. Moving
    this to the same real-time `issues: closed` trigger `mark-epic-done`
    (this command's predecessor) already used covers every closure path
    uniformly, the same way `mark-epic-done` already did for the board Status
    half of this.

    Every closed issue: Stage is cleared (`clear_stage_field` -- "current
    stage" is meaningless once closed) and Pipeline Status is set to `Done`
    (`set_pipeline_status_field(..., "done")` -- **not** deleted, unlike the
    old behavior; a closed issue has a real, meaningful terminal state now that
    the field has a `Done` option, added 2026-08-20 alongside this change).
    If `issue` is also an epic, the board's native Status field additionally
    flips to `Done` too (unchanged from `mark-epic-done`'s prior behavior) --
    applies to every epic regardless of standing/legacy status, since a human
    closing any epic is real completion signal worth reflecting on the board,
    even one whose children never ran the epic-level flow in the first place."""
    info = gh.issue_epic_info(issue)
    gh.clear_stage_field(issue)
    gh.set_pipeline_status_field(issue, "done")
    epic = is_epic(info)
    if epic:
        gh.set_project_status(issue, "done")
    return {"issue": issue, "is_epic": epic, "marked_done": True}


# Tracked file holding the closed-issue count at the last completed retrospective
# -- committed alongside the retro's own skill edits (Step 5 commits anyway), so
# the trigger is a watermark ("5 or more closed since the last retro") instead of
# the old `count % 5 == 0`, which both skipped whenever two issues closed between
# checks and fired forever once the count query capped out.
RETRO_WATERMARK_FILE = PIPELINE["retro"]["watermarkFile"]
RETRO_EVERY = PIPELINE["retro"]["everyClosedIssues"]


def cmd_retro_check(gh: GitHub, repo_path: str = ".", mark_done: bool = False) -> dict:
    """`run_retro` is true once >= 5 issues have closed since the last completed
    retrospective. `--mark-done` records the current count as the new watermark
    (a local file write -- commit it with the retro's own skill-edit commit)."""
    path = os.path.join(repo_path, RETRO_WATERMARK_FILE)
    watermark = 0
    if os.path.isfile(path):
        with open(path) as f:
            watermark = int(f.read().strip() or 0)
    count = gh.closed_issue_count()
    if mark_done:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(f"{count}\n")
        return {"closed_count": count, "watermark": count, "marked_done": True}
    return {"closed_count": count, "watermark": watermark,
            "run_retro": count - watermark >= RETRO_EVERY}


def cmd_pairing_counts(gh: GitHub, issue: int) -> dict:
    """Mechanical bounce counts for the escalation-valve pairings whose events
    leave comment markers -- so the valve's thresholds (3 -> context-reset
    replacement agent, 6 -> needs-human) survive session boundaries
    (continuous-mode cycle agents have zero memory of prior units by design)
    instead of living only in one orchestrator's bookkeeping.

    Covers the marker-backed pairings: `pr-review <-> development`
    (`pr-review-outcome` markers; `rework_since_last_clean` resets on every
    clean outcome), `sync-branch-conflict <-> development` (`sync-conflict`
    markers, total count), and -- since 2026-08-28 -- the design pairings
    `product-review <-> product`, `arch-review <-> architecture` and
    `lld-review <-> lld` (`design-review-outcome` markers, reported per role under
    `design_review`).
    `testing <-> development` still leaves no marker and remains the
    orchestrator's own session-scoped count."""
    comments = gh.issue_view(issue).get("comments", [])
    rework_since_clean = total_rework = total_clean = sync_conflicts = 0
    design_review: dict = {}
    for c in comments:
        body = c.get("body", "")
        m = _PR_REVIEW_OUTCOME_MARKER.search(body)
        if m:
            if m.group(1) == "rework":
                total_rework += 1
                rework_since_clean += 1
            else:
                total_clean += 1
                rework_since_clean = 0
        if _SYNC_CONFLICT_MARKER.search(body):
            sync_conflicts += 1
        d = _DESIGN_REVIEW_OUTCOME_MARKER.search(body)
        if d:
            outcome, role = d.group(1), d.group(2)
            counts = design_review.setdefault(
                role, {"rework_since_last_clean": 0, "total_rework": 0, "total_clean": 0})
            if outcome == "rework":
                counts["total_rework"] += 1
                counts["rework_since_last_clean"] += 1
            else:
                counts["total_clean"] += 1
                counts["rework_since_last_clean"] = 0
    return {"issue": issue,
            "thresholds": {"replace_at": ESCALATION["replaceAt"],
                           "needs_human_at": ESCALATION["needsHumanAt"]},
            "pr_review_rework_since_last_clean": rework_since_clean,
            "pr_review_total_rework": total_rework,
            "pr_review_total_clean": total_clean,
            "sync_conflict_count": sync_conflicts,
            # Keyed by role (`arch-review` / `lld-review`) rather than flattened:
            # a unit can bounce on both, and the valve counts them as separate
            # pairings. Absent key == that review never ran on this unit.
            "design_review": design_review}


def cmd_create_issue(gh: GitHub, title: str, body: str, parent: int, labels: list) -> dict:
    """The one remaining path that creates a new issue (product splitting an
    oversized issue -- see "Repo access" in references/operations.md). Enforces the "every issue
    has a parent epic" invariant at its one entry point: sets Type: Task (a
    split-off issue is never itself a fresh epic) and links it as a sub-issue of
    `parent` immediately, rather than leaving that to a follow-up step that could
    be skipped."""
    number = gh.issue_create(title, body, labels)
    gh.set_issue_type(number, "Task")
    gh.add_sub_issue(parent, number)
    return {"issue": number, "parent": parent, "type": "Task"}


def cmd_list_needs_human(gh: GitHub) -> dict:
    """Every open issue with Pipeline Status = "Needs Human", with the reason text
    captured from its own mark-needs-human comment -- so Step 1/Step 4 can surface
    *why* each one is stuck, not just that it is. Blockers only go stale silently if
    nobody re-reads the reason; this makes that reason cheap to re-surface every
    time next-action finds nothing else actionable, instead of requiring a human to
    notice and point it out."""
    issues = [i for i in gh.issue_list() if i["state"] == "OPEN" and pipeline_status(i) == "needs-human"]
    result = []
    for issue in issues:
        detail = gh.issue_view(issue["number"])
        reason = None
        for c in reversed(detail.get("comments", [])):
            m = _NEEDS_HUMAN_REASON.search(c.get("body", ""))
            if m:
                reason = m.group(1).strip()
                break
        result.append({"issue": issue["number"], "title": issue["title"], "reason": reason})
    return {"needs_human": result}


_EPIC_CLOSEABLE_MARKER = "<!-- epic-closeable-checklist-posted -->"


def cmd_check_epics_closeable(gh: GitHub) -> dict:
    """Surfaces every open, non-`epic:standing` epic whose children are all closed
    -- posts a one-time checklist comment and assigns the human operator, but never
    closes the epic itself (a milestone-level call, same spirit as the two
    human-review gates -- see "Human-review gates" in references/gates.md). Idempotent: skips
    epics that already carry the marker comment from a prior run, so this is safe
    to call every time next-action finds nothing else actionable. `epic:standing`
    epics (permanent backlog umbrellas) are never proposed for closing, even when
    momentarily empty of open children. See "Epic closing" in references/epics.md."""
    all_issues = gh.issue_list()
    open_epics = [i for i in all_issues if i["state"] == "OPEN" and is_epic(i) and not is_epic_standing(i)]
    results = []
    for epic in open_epics:
        children = [i for i in all_issues if i.get("parent") and i["parent"]["number"] == epic["number"]]
        if not children or any(c["state"] != "CLOSED" for c in children):
            continue
        detail = gh.issue_view(epic["number"])
        already_posted = any(_EPIC_CLOSEABLE_MARKER in c.get("body", "") for c in detail.get("comments", []))
        if already_posted:
            results.append({"epic": epic["number"], "title": epic["title"], "already_notified": True})
            continue
        open_dependents = sorted({d for c in children for d in gh.blocking(c["number"])})
        # An epic's gate PRs land its docs on `epic-<n>` (the gate sub-branch merges
        # there, not to `main` -- see `epic_gate_branch`); `close-epic`'s final
        # merge is what carries them to `main`. So for a still-open epic the branch
        # to check is the epic branch, GitHub-side.
        branch = epic_branch(epic["number"])
        missing_docs = [f"{DOC_ROOT}/epic-{epic['number']}/{name}"
                        for name in ("product.md", "architecture.md")
                        if not gh.path_on_ref(f"{DOC_ROOT}/epic-{epic['number']}/{name}", branch)]
        docs_line = (
            f"- [ ] {len(missing_docs)} epic doc(s) never reached `{branch}` "
            f"({', '.join(f'`{d}`' for d in missing_docs)}) — merge their gate PRs before "
            "closing, or they stay reachable only by an unmerged gate branch ref and never "
            "reach `main`\n"
            if missing_docs else
            f"- [x] This epic's `product.md` and `architecture.md` are both on `{branch}` "
            "(auto-verified) — `close-epic`'s merge carries them to `main`\n"
        )
        dependents_line = (
            f"- [ ] {len(open_dependents)} still-open issue(s) reference/depend on a closed child of this epic "
            f"({', '.join(f'#{n}' for n in open_dependents)}) — resolve or consciously accept before closing\n"
            if open_dependents else
            "- [x] No open issue elsewhere depends on a closed child of this epic (auto-verified)\n"
        )
        checklist = (
            f"## 🏁 Epic ready to close — all {len(children)} child issue(s) merged\n\n"
            f"- [x] All child issues closed (auto-verified: {len(children)}/{len(children)})\n"
            f"{dependents_line}"
            f"{docs_line}"
            f"- [ ] Closing verification run on `{epic_branch(epic['number'])}` after merging "
            "`origin/main` into it — the full e2e suite and an exploratory pass, run in parallel\n"
            f"- [ ] `{epic_branch(epic['number'])}` merged to `main`\n\n"
            f"Run `sdlc_next.py close-epic {epic['number']}` to do all of it — it reconciles the "
            "epic branch with `main`, refuses while verification evidence is missing, and merges "
            "the integration PR when it is present. Findings from the closing run are triaged by "
            "the rule in `references/epics.md`: Blocker/Critical become children of this epic and "
            "block the close; Normal/Low go to the standing backlog epic.\n\n"
            f"{_EPIC_CLOSEABLE_MARKER}"
        )
        gh.issue_comment(epic["number"], checklist)
        gh.issue_edit(epic["number"], add_assignees=[HUMAN_ASSIGNEE])
        results.append({"epic": epic["number"], "title": epic["title"], "notified": True,
                         "open_dependents": open_dependents,
                         "docs_missing_from_epic_branch": missing_docs})
    return {"closeable_epics": results}


# --- Per-epic isolated runtime stack -----------------------------------------
#
# The git half of "N pipeline instances on N epics with zero shared mutable
# state" is `BranchWorkspace` + `branch_lock`. This is the runtime half: every
# epic gets its own compose project, ports, env/secrets profile and database
# data directory, provisioned at epic start (or before its first e2e-running
# child) and torn down at epic close. Isolation is config-only -- the driven
# repo's compose must already read the keys named in `pipeline.stack` (proven
# on the origin repo: COMPOSE_PROJECT_NAME, *_PORT, DB_PORT_EXPOSE,
# BACKEND_DEBUG_PORT, POSTGRES_DATA_DIR). See "Per-epic isolated stack" in
# references/parallelism.md.

Shell = Callable[[str, str], str]  # (command, cwd) -> stdout; raises GhError on nonzero


def _default_shell(command: str, cwd: str) -> str:
    result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed ({result.returncode}) in {cwd}: {command}\n"
                      f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result.stdout


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def stack_profile(epic: int) -> str:
    return PIPELINE["stack"]["profileTemplate"].format(n=epic)


def _stack_layout(epic: int) -> dict:
    """Every path/name the two stack commands share, derived from config once."""
    s = PIPELINE["stack"]
    profile = stack_profile(epic)
    root = os.path.abspath(s["workspaceRoot"])
    fmt = {"profile": profile, "n": epic, "workspaceRoot": root}
    env_file = s["envFile"].format(**fmt)
    secrets_file = s["secretsFile"].format(**fmt)
    return {
        "profile": profile,
        "project": s["composeProjectTemplate"].format(**fmt),
        "workspace_root": root,
        "env_file": os.path.join(root, env_file),
        "secrets_file": os.path.join(root, secrets_file),
        "base_env_file": os.path.join(root, s["envFile"].format(profile=s["baseProfile"], n=epic,
                                                                 workspaceRoot=root)),
        "base_secrets_file": os.path.join(root, s["secretsFile"].format(
            profile=s["baseProfile"], n=epic, workspaceRoot=root)),
        "data_dir": s["dataDirTemplate"].format(**fmt),
        "env_file_rel": env_file,
    }


def pick_stack_ports(epic: int, probe: Callable[[int], bool] = _port_free) -> dict:
    """Distinct host ports for this epic's stack: each configured base port plus
    a stride-multiple offset derived from the epic number, bumped by another
    stride while any port in the set is already bound. Deterministic per epic
    when the machine is quiet, and never colliding with the base profile (offset
    is never zero) or another provisioned epic (its ports are bound)."""
    s = PIPELINE["stack"]
    stride = int(s["portStride"])
    base_ports = {k: int(v) for k, v in s["ports"].items()}
    slots = 45
    start = (epic % slots) + 1
    for i in range(slots):
        offset = stride * (((start + i - 1) % slots) + 1)
        candidate = {k: v + offset for k, v in base_ports.items()}
        if all(probe(p) for p in candidate.values()):
            return candidate
    raise GhError(f"no free port set found for epic #{epic} across {slots} stride slots "
                  f"(stride {stride}); tear down stale epic stacks first")


_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def render_env_profile(base_text: str, overrides: dict) -> str:
    """The base profile's text with every `KEY=` line in `overrides` replaced in
    place and any missing key appended under a marker -- so the epic profile
    stays a faithful copy of the base plus exactly the isolation keys."""
    seen = set()
    out = []
    for line in base_text.splitlines():
        m = _ENV_LINE_RE.match(line)
        key = m.group(1) if m else None
        if key in overrides:
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in overrides if k not in seen]
    if missing:
        out.append("")
        out.append("# --- sdlc-pipeline per-epic isolation (generated) ---")
        out.extend(f"{k}={overrides[k]}" for k in missing)
    return "\n".join(out) + "\n"


def _read_env_ports(path: str) -> dict:
    ports = {}
    keys = set(PIPELINE["stack"]["ports"])
    with open(path) as f:
        for line in f:
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1) in keys:
                try:
                    ports[m.group(1)] = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return ports


def cmd_provision_epic_stack(epic: int, up: bool = True, shell: Shell = _default_shell,
                             probe: Callable[[int], bool] = _port_free) -> dict:
    """Stand up the epic's isolated stack: generate `<envFile>` for profile
    `epic<n>` from the base profile with distinct ports + compose project + data
    dir, copy the base secrets file (a plain copy -- no secret is ever read),
    then run `upCommand` and, if set, `seedCommand`. Idempotent: an existing
    profile is reused as-is (its ports read back), only the up/seed commands
    re-run. `enabled: false` -> structured no-op, so a repo without a
    parametrised compose is unaffected."""
    s = PIPELINE["stack"]
    if not s["enabled"]:
        return {"epic": epic, "provisioned": False,
                "reason": "pipeline.stack.enabled is false — shared stack in use"}
    lay = _stack_layout(epic)
    created = False
    if os.path.exists(lay["env_file"]):
        ports = _read_env_ports(lay["env_file"])
    else:
        if not os.path.isfile(lay["base_env_file"]):
            raise GhError(f"base profile {lay['base_env_file']} not found -- set "
                          f"pipeline.stack.baseProfile/envFile to the repo's dev profile")
        if not os.path.isfile(lay["base_secrets_file"]):
            raise GhError(f"base secrets file {lay['base_secrets_file']} not found -- the epic "
                          f"profile is a copy of it (pipeline.stack.secretsFile)")
        ports = pick_stack_ports(epic, probe=probe)
        overrides = {"COMPOSE_PROJECT_NAME": lay["project"], **{k: str(v) for k, v in ports.items()},
                     s["dataDirKey"]: lay["data_dir"]}
        with open(lay["base_env_file"]) as f:
            base_text = f.read()
        with open(lay["env_file"], "w") as f:
            f.write(render_env_profile(base_text, overrides))
        shutil.copyfile(lay["base_secrets_file"], lay["secrets_file"])
        os.makedirs(os.path.join(lay["workspace_root"], lay["data_dir"]), exist_ok=True)
        created = True
    fmt = {"profile": lay["profile"], "project": lay["project"], "envFile": lay["env_file_rel"],
           "secretsFile": os.path.relpath(lay["secrets_file"], lay["workspace_root"]),
           "dataDir": lay["data_dir"], "workspaceRoot": lay["workspace_root"], "n": epic}
    ran = []
    if up:
        for key in ("upCommand", "seedCommand"):
            cmd = (s.get(key) or "").format(**fmt)
            if cmd.strip():
                shell(cmd, lay["workspace_root"])
                ran.append(cmd)
    return {"epic": epic, "provisioned": True, "created": created, "profile": lay["profile"],
            "project": lay["project"], "env_file": lay["env_file"],
            "secrets_file": lay["secrets_file"], "data_dir": lay["data_dir"], "ports": ports,
            "commands_run": ran,
            "use": f"COMPOSE_PROJECT_NAME={lay['project']} make <target> PROFILE={lay['profile']}"}


def cmd_teardown_epic_stack(epic: int, keep_data: bool = False,
                            shell: Shell = _default_shell) -> dict:
    """The inverse: `downCommand` (volumes included), then remove the generated
    env + secrets profile files and the data directory. Structured no-op when
    nothing is provisioned. A failing down command still removes nothing, so a
    half-torn stack is visible rather than orphaned."""
    s = PIPELINE["stack"]
    if not s["enabled"]:
        return {"epic": epic, "torn_down": False,
                "reason": "pipeline.stack.enabled is false — shared stack in use"}
    lay = _stack_layout(epic)
    if not os.path.exists(lay["env_file"]):
        return {"epic": epic, "torn_down": False, "profile": lay["profile"],
                "reason": f"no stack provisioned ({lay['env_file']} absent)"}
    fmt = {"profile": lay["profile"], "project": lay["project"], "envFile": lay["env_file_rel"],
           "secretsFile": os.path.relpath(lay["secrets_file"], lay["workspace_root"]),
           "dataDir": lay["data_dir"], "workspaceRoot": lay["workspace_root"], "n": epic}
    cmd = (s.get("downCommand") or "").format(**fmt)
    if cmd.strip():
        shell(cmd, lay["workspace_root"])
    removed = []
    for p in (lay["env_file"], lay["secrets_file"]):
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    data_abs = os.path.join(lay["workspace_root"], lay["data_dir"])
    if not keep_data and os.path.isdir(data_abs):
        shutil.rmtree(data_abs, ignore_errors=True)
        removed.append(data_abs)
    return {"epic": epic, "torn_down": True, "profile": lay["profile"], "project": lay["project"],
            "down_command": cmd, "removed": removed}


def main(argv: Optional[list] = None) -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        print(json.dumps({"error": "GITHUB_TOKEN not set — prefix the call with "
                                    f"GITHUB_TOKEN=$(cat {TOKEN_PATH})"}))
        return 1
    parser = argparse.ArgumentParser(prog="sdlc_next.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("next-action")
    p.add_argument("epic", type=int, help="The epic issue number to drive end-to-end -- required, "
                                           "see \"Epic number is mandatory\" in SKILL.md")
    p.set_defaults(func=lambda a: cmd_next_action(GitHub(), a))
    p = sub.add_parser("list-ready-for-review",
                        help="Up to PR_REVIEW_PARALLELISM of this epic's children whose draft PR "
                             "is finished, handed off by development, and not yet reviewed this "
                             "round")
    p.add_argument("epic", type=int, help="Scope the review pool to this epic's own children")
    p.add_argument("--limit", type=int, default=None,
                    help=f"How many PRs to return (default: PR_REVIEW_PARALLELISM = "
                         f"{PR_REVIEW_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_ready_for_review(GitHub(), a.epic, a.limit))
    p = sub.add_parser("list-parallel-ready",
                        help="Up to DEV_LANE_PARALLELISM of this epic's lld/development/testing "
                             "children safe to start/resume concurrently, each in its own worktree "
                             "-- not blockedBy anything open, footprint doesn't collide with an "
                             "active/eligible sibling")
    p.add_argument("epic", type=int, help="Scope the parallel-lane pool to this epic's own children")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent children allowed (default: DEV_LANE_PARALLELISM = "
                         f"{DEV_LANE_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_parallel_ready(GitHub(), a.repo_path, a.epic, a.limit))
    p = sub.add_parser("list-design-ready",
                        help="Up to DESIGN_LANE_PARALLELISM of this STANDING epic's product/"
                             "architecture children safe to start/resume concurrently, each in its "
                             "own worktree -- not blockedBy anything open, not parked/gate-pending. "
                             "Empty for a default-profile epic, whose product/architecture is a "
                             "single epic-self unit, not fanned out.")
    p.add_argument("epic", type=int, help="Scope the design-lane pool to this epic's own children")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent design-stage children allowed (default: "
                         f"DESIGN_LANE_PARALLELISM = {DESIGN_LANE_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_design_ready(GitHub(), a.repo_path, a.epic, a.limit))
    p = sub.add_parser("handoff-to-pr-review",
                        help="development's exit action once its suites are green: posts the "
                             "canonical development->pr-review marker that queues this PR for "
                             "review")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--summary", required=True,
                    help="One sentence: what was built and tested, and the result")
    p.set_defaults(func=lambda a: cmd_handoff_to_pr_review(GitHub(), a.issue, a.pr, a.summary))
    p = sub.add_parser("record-pr-review",
                        help="Record a pr-review pass's outcome on the issue (last action of "
                             "every review, before merge-pr or a rework resume)")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.set_defaults(func=lambda a: cmd_record_pr_review(GitHub(), a.issue, a.pr, a.outcome, a.summary))
    p = sub.add_parser("record-local-ci",
                        help="development's evidence-carrying attestation that a main-only "
                             "suite (backend/frontend) passed locally against a given commit -- "
                             "the merge-gate stand-in for the GHA check that no longer runs "
                             "on a child PR")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--suite", required=True, choices=list(LOCAL_CI_SUITES))
    p.add_argument("--sha", required=True,
                    help="The commit the suite ran against (the development worktree's "
                         "`git rev-parse HEAD`); merge-pr honours it only while it matches "
                         "the PR's current head")
    p.add_argument("--command", required=True,
                    help="The exact command the suite was run with, as run")
    p.add_argument("--output", required=True,
                    help="Path to a file holding that run's own captured stdout/stderr; its "
                         "tail is embedded in the attestation comment. A summary is not "
                         "accepted in its place")
    p.set_defaults(func=lambda a: cmd_record_local_ci(GitHub(), a.pr, a.suite, a.sha,
                                                       a.command, a.output))
    p = sub.add_parser("record-design-review",
                        help="Record an arch-review/lld-review outcome on the unit (last action "
                             "of every design review, before a rework resume or moving on)")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True, choices=list(DESIGN_REVIEW_ROLES))
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_record_design_review(
        GitHub(), a.issue, a.role, a.outcome, a.summary, a.unit))
    p = sub.add_parser("check-gate")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_check_gate(GitHub(), a))
    p = sub.add_parser("claim")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True)
    p.set_defaults(func=lambda a: cmd_claim(GitHub(), a.issue, a.role))
    p = sub.add_parser("start-comment")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True,
                   choices=["product-review", "arch-review", "lld-review", "pr-review", "testing"])
    p.set_defaults(func=lambda a: cmd_start_comment(GitHub(), a.issue, a.role))
    p = sub.add_parser("open-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map); the command operates in the branch's own live worktree or an ephemeral one, never the main checkout")
    p.add_argument("--title", required=True)
    p.add_argument("--doc", required=True, choices=["product.md", "architecture.md"])
    p.add_argument("--next-stage", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_open_gate(
        GitHub(), a.repo_path, a.issue, a.title, a.doc, a.next_stage, a.summary, a.unit))
    p = sub.add_parser("pass-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map); the command operates in the branch's own live worktree or an ephemeral one, never the main checkout")
    p.add_argument("--gate-pr", type=int, required=True)
    p.add_argument("--stage", required=True, choices=["product", "architecture"])
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_pass_gate(GitHub(), a.repo_path, a.issue, a.gate_pr, a.stage, a.unit))
    p = sub.add_parser("skip-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True, choices=["architecture"])
    p.add_argument("--confidence", type=int, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_skip_gate(GitHub(), a.issue, a.stage, a.confidence, a.summary, a.unit))
    p = sub.add_parser("auto-pass-gate-a",
                        help="Advance past Gate A with no human review when the epic's "
                             "profile sets requiresHumanGateA:false (after a clean product-review)")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", default="product", choices=["product"])
    p.add_argument("--summary", required=True)
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_auto_pass_gate_a(GitHub(), a.issue, a.stage, a.summary, a.unit))
    p = sub.add_parser("auto-pass-gate")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_auto_pass_gate(GitHub(), a.repo_path, a.pr))
    p = sub.add_parser("mark-feedback-received")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--author", required=True, help="login of the comment/review author")
    p.add_argument("--body", default="", help="comment/review body text (empty = no-op skip)")
    p.set_defaults(func=lambda a: cmd_mark_feedback_received(GitHub(), a.pr, a.author, a.body))
    p = sub.add_parser("mark-feedback-addressed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_feedback_addressed(GitHub(), a.issue))
    p = sub.add_parser("mark-todo")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_todo(GitHub(), a.issue))
    p = sub.add_parser("mark-issue-closed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_issue_closed(GitHub(), a.issue))
    p = sub.add_parser("pause-for-epic-regate")
    p.add_argument("issue", type=int)
    p.add_argument("--epic", type=int, required=True)
    p.add_argument("--gate-pr", type=int, required=True)
    p.set_defaults(func=lambda a: cmd_pause_for_epic_regate(GitHub(), a.issue, a.epic, a.gate_pr))
    p = sub.add_parser("verify-exit")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map); the command operates in the branch's own live worktree or an ephemeral one, never the main checkout")
    p.add_argument("--expect-stage", required=True)
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_verify_exit(
        GitHub(), a.repo_path, a.issue, a.expect_stage, a.pr, a.unit))
    p = sub.add_parser("cite",
                        help="Generate a citation block by reading the real file (or "
                             "git show <rev>:<path>) -- selects the target fragment by "
                             "--line, --lines, or --match, and errors with nothing "
                             "emitted if it is not actually there")
    p.add_argument("path")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--line", type=int)
    target.add_argument("--lines", help="inclusive range, e.g. 12-18")
    target.add_argument("--match", help="literal substring; the fragment is the first "
                                         "line containing it, erroring if that is zero "
                                         "or several lines")
    p.add_argument("--rev", default=None, help="pin to this revision instead of the "
                                                "working tree")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_cite(a.path, line=a.line, lines=a.lines,
                                            match=a.match, rev=a.rev,
                                            repo_path=a.repo_path))
    p = sub.add_parser("verify-citations",
                        help="Re-resolve every asserted citation in one or more "
                             "documents against the working tree or each citation's "
                             "pinned revision")
    p.add_argument("doc", nargs="+")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_verify_citations(a.doc, repo_path=a.repo_path))
    p = sub.add_parser("worktree-add",
                        help="Create (or find) the unit's worktree: resumes from "
                             "origin/<branch> when it exists, else branches off the "
                             "integration base")
    p.add_argument("number", type=int)
    p.add_argument("--unit", choices=["issue", "epic"], default="issue")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_worktree_add(GitHub(), a.number, a.unit, a.repo_path))

    p = sub.add_parser("sync-branch")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map); the command operates in the branch's own live worktree or an ephemeral one, never the main checkout")
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.set_defaults(func=lambda a: cmd_sync_branch(GitHub(), a.repo_path, a.issue, a.unit))
    p = sub.add_parser("merge-lld-doc",
                        help="Publish a normal-epic child's lld.md onto its epic branch as soon "
                             "as lld-review is CLEAN, then advance its Stage to development "
                             "(NOT claimed -- next-action/list-parallel-ready pick it up as a "
                             "fresh unit); no-op for a standing-epic child or a parentless issue")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository; the doc is published from the epic branch's own live worktree or an ephemeral one, never the main checkout")
    p.set_defaults(func=lambda a: cmd_merge_lld_doc(GitHub(), a.repo_path, a.issue))
    p = sub.add_parser("open-dev-pr")
    p.add_argument("issue", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: cmd_open_dev_pr(GitHub(), a.issue, a.title, a.body, a.summary))
    p = sub.add_parser("pr-checks")
    p.add_argument("pr", type=int)
    p.set_defaults(func=lambda a: cmd_pr_checks(GitHub(), a.pr))
    p = sub.add_parser("merge-pr")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root whose worktree list is searched to release the "
                         "merged branch's worktree")
    p.set_defaults(func=lambda a: cmd_merge_pr(GitHub(), a.pr, a.issue, a.repo_path))
    p = sub.add_parser("create-issue")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--parent", type=int, required=True, help="Epic issue number this becomes a sub-issue of")
    p.add_argument("--label", action="append", default=[], dest="labels")
    p.set_defaults(func=lambda a: cmd_create_issue(GitHub(), a.title, a.body, a.parent, a.labels))
    p = sub.add_parser("mark-blocked")
    p.add_argument("issue", type=int)
    p.add_argument("--dep", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root whose worktree list is searched to release this "
                         "unit's worktree")
    p.set_defaults(func=lambda a: cmd_mark_blocked(GitHub(), a.issue, a.dep, a.repo_path))
    p = sub.add_parser("mark-needs-human")
    p.add_argument("issue", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root whose worktree list is searched to release this "
                         "unit's worktree")
    p.set_defaults(func=lambda a: cmd_mark_needs_human(GitHub(), a.issue, a.reason, a.repo_path))
    p = sub.add_parser("retro-check")
    p.add_argument("--repo-path", default=".",
                    help="Repo root holding the tracked retro-watermark file")
    p.add_argument("--mark-done", action="store_true",
                    help="Record the current closed count as the new watermark "
                         "(commit the file with the retro's own commit)")
    p.set_defaults(func=lambda a: cmd_retro_check(GitHub(), a.repo_path, a.mark_done))
    p = sub.add_parser("show-config",
                        help="Print the effective pipeline tunables (config `pipeline` "
                             "block merged over defaults) plus repo/docRoot/parallelism")
    p.set_defaults(func=lambda a: {"repo": REPO, "docRoot": DOC_ROOT,
                                   # designLane is defaulted in code (not required in the
                                   # config), so surface the effective value alongside the
                                   # raw devLane/prReview rather than the raw block alone.
                                   "parallelism": {**CONFIG["parallelism"],
                                                   "designLane": DESIGN_LANE_PARALLELISM},
                                   "requiredWorkflows": CONFIG["requiredWorkflows"],
                                   "localCiSuites": list(LOCAL_CI_SUITES),
                                   **PIPELINE})

    p = sub.add_parser("pairing-counts",
                        help="Marker-derived escalation-valve bounce counts for one issue "
                             "(pr-review rework since last clean, sync-conflict count)")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_pairing_counts(GitHub(), a.issue))
    p = sub.add_parser("list-needs-human")
    p.set_defaults(func=lambda a: cmd_list_needs_human(GitHub()))
    p = sub.add_parser("close-epic",
                        help="Reconcile the epic's integration branch with main, then merge it "
                             "once closing verification evidence is on the thread")
    p.add_argument("epic", type=int)
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_close_epic(GitHub(), a.epic, a.repo_path))
    p = sub.add_parser("record-epic-verification",
                        help="Record one half of an epic's closing verification (e2e | exploratory)")
    p.add_argument("epic", type=int)
    p.add_argument("--kind", required=True, choices=["e2e", "exploratory"])
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: cmd_record_epic_verification(GitHub(), a.epic, a.kind, a.summary))
    p = sub.add_parser("check-epics-closeable")
    p.set_defaults(func=lambda a: cmd_check_epics_closeable(GitHub()))
    p = sub.add_parser("provision-epic-stack",
                        help="Stand up the epic's isolated runtime stack (own compose project, "
                             "ports, env/secrets profile, DB data dir) -- see pipeline.stack; "
                             "no-op when pipeline.stack.enabled is false")
    p.add_argument("epic", type=int)
    p.add_argument("--no-up", action="store_true",
                    help="Generate the profile only; skip upCommand/seedCommand")
    p.set_defaults(func=lambda a: cmd_provision_epic_stack(a.epic, up=not a.no_up))
    p = sub.add_parser("teardown-epic-stack",
                        help="Compose down -v the epic's stack and remove its generated profile "
                             "files and data dir")
    p.add_argument("epic", type=int)
    p.add_argument("--keep-data", action="store_true", help="Leave the DB data dir in place")
    p.set_defaults(func=lambda a: cmd_teardown_epic_stack(a.epic, keep_data=a.keep_data))
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        print(json.dumps(result))
        # Most subcommands' results carry no "ok" key at all -- default True keeps
        # them at exit 0 exactly as before. `auto-pass-gate` is the one subcommand
        # that reports its own success/failure via an explicit "ok" key rather than
        # raising GhError on the "nothing to do" case (see its docstring) -- honor
        # that convention here instead of always exiting 0.
        return 0 if result.get("ok", True) else 1
    except GhError as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
