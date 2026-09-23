#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""sdlc_next.py — deterministic control plane for the sdlc plugin.

Owns every mechanical GitHub/git read, decision and mutation so the orchestrating
agent calls this CLI and acts on its JSON verdict instead of hand-running gh/git.

Exit codes: 0 = a valid result (including "nothing to do"); 1 = an operational
failure (a gh/git call failed, a required marker was missing). Every command
prints one JSON object to stdout regardless of exit code.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
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
from typing import Callable, Optional, Protocol, runtime_checkable

CONFIG_FILENAME = "sdlc-pipeline.config.json"


def _find_config() -> Optional[str]:
    """Path to the driven repo's config: `$SDLC_CONFIG`, else the first
    `<CONFIG_FILENAME>` at `./`, `.config/` or `.claude/` walking up from cwd."""
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
# GraphQL templates name the repository inline (gh's `--repo` doesn't reach `api graphql`).
_OWNER, _NAME = REPO.split("/", 1)
DOC_ROOT = CONFIG["docRoot"]
TOKEN_PATH = CONFIG["tokenPath"]
TOKEN_ENV = CONFIG.get("tokenEnv")
STAGE_AFTER_GATE = {"product": "architecture", "architecture": "development"}

# Lane caps: how many units of one epic run concurrently, one worktree each.
# Every lane defaults to 1 (sequential); raise per repo in `parallelism`.
_PARALLELISM = CONFIG.get("parallelism") or {}
PR_REVIEW_PARALLELISM = _PARALLELISM.get("prReview", 1)
DEV_LANE_PARALLELISM = _PARALLELISM.get("devLane", 1)
DESIGN_LANE_PARALLELISM = _PARALLELISM.get("designLane", 1)
# Whether other epics' live worktrees join the dev lane's footprint-overlap check.
CROSS_EPIC_FOOTPRINT_CHECK = bool(_PARALLELISM.get("crossEpicFootprintCheck", False))
# Units one orchestrator run drives to a terminal state before a resumable stop,
# bounding the orchestrator's own context growth. 0 = unlimited.
MAX_TASKS_PER_RUN = _PARALLELISM.get("maxTasksPerRun", 0)

HUMAN_ASSIGNEE = CONFIG["humanAssignee"]

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(PLUGIN_ROOT, "hooks", "model_policy.json")) as _f:
    _MODEL_POLICY = json.load(_f)

# Everything below is tunable per repo via the optional `pipeline` block in the
# config; every key has the default shown here so an older config keeps working.
_PIPELINE_DEFAULTS = {
    "labels": {"standing": "epic:standing", "legacy": "epic:legacy",
               "architected": "epic:architected", "gate": "sdlc:gate"},
    "branches": {"issuePrefix": "issue-", "epicPrefix": "epic-"},
    # `ephemeralPrefix`: throwaway tree for a branch no live worktree holds, since
    # the main checkout is never a git-write target.
    # `releaseCommand`: shell command run inside a worktree right before it is removed
    # (tear down per-worktree resources); failure is reported, never fatal. "" = none.
    "worktrees": {"root": "/tmp", "devPrefix": "sdlc-dev-", "epicPrefix": "sdlc-epic-",
                  "reviewPrefix": "sdlc-review-", "ephemeralPrefix": "sdlc-tmp-",
                  "releaseCommand": ""},
    # Per-branch flock; `SDLC_LOCK_DIR` in the environment overrides `dir`.
    "locks": {"dir": "{worktreesRoot}/.sdlc-locks", "waitSeconds": 600},
    # Per-epic runtime stack. The driven repo's compose must honour the `ports`
    # keys, `COMPOSE_PROJECT_NAME` and `dataDirKey`; disabled = structured no-ops.
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
    "gates": {"skipConfidenceThreshold": 80, "requiresHumanGateA": True,
              "requiresHumanGateB": True},
    # Repo-wide cap on units at Stage=Product with an open Gate A; at the cap no
    # fresh `product` delegation starts (resumes/rework are never gated). 0 disables.
    "productWip": {"maxGateAPending": 5},
    "escalation": {"replaceAt": 3, "needsHumanAt": 6},
    "continuous": {"cycleCap": 8},
    # A `resume` action younger than `liveWindowMinutes` is flagged `likely_live`: another
    # session may still be driving it, so the orchestrator asks the operator before taking over.
    "resume": {"liveWindowMinutes": 30},
    # `auto`: the orchestrator runs the final `close-epic` itself; close-epic's own
    # refusals (open children, stale verification, failing checks) still apply.
    # `evidenceCarryForward.paths`: globs a commit after the tested head may touch without
    # invalidating the exploratory evidence (`{docRoot}` expands to `docRoot`).
    "epicClose": {"auto": False,
                  "evidenceCarryForward": {"paths": ["**/*.md", "docs/**", "{docRoot}/**"]}},
    # Priority / Effort `create-issue` sets when no flag names one (fields configured only).
    "issueDefaults": {"priority": "Medium", "effort": "Medium"},
    # Stage models and fan-out: one home, hooks/model_policy.json (agent_guard enforces it).
    "models": _MODEL_POLICY.get("models", {}),
    "fanout": _MODEL_POLICY.get("fanout", {}),
    # {"initiative"|"epic"|"task": {"field": "issueType"|"label", "value": ...}};
    # empty means every issue classifies as "other" rather than a guess.
    "classification": {},
    "workItemProvider": {"type": "github"},
    "docTemplates": "_templates",
    # Ordered; the first profile whose `match` label is on the epic wins, "*" is
    # the catch-all. See `resolve_profile`.
    "profiles": [
        {"name": "legacy", "match": {"label": "epic:legacy"}, "driven": False},
        {"name": "standing", "match": {"label": "epic:standing"},
         "epicLevelPhase": False, "childrenNeedArchitectedEpic": False, "closes": False,
         "gates": {"requiresHumanGateA": False, "requiresHumanGateB": False}},
        {"name": "default", "match": "*"},
    ],
}

# Fallbacks for toggles a matched profile omits (its `gates` fall back to `pipeline.gates`).
_PROFILE_TOGGLE_DEFAULTS = {
    "driven": True,               # False = legacy: pipeline ignores the epic + children
    "epicLevelPhase": True,       # False = standing: each child runs its own full flow
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
    return merged


PIPELINE = _pipeline_config()
LABELS = PIPELINE["labels"]
ISSUE_BRANCH_PREFIX = PIPELINE["branches"]["issuePrefix"]
EPIC_BRANCH_PREFIX = PIPELINE["branches"]["epicPrefix"]
ESCALATION = PIPELINE["escalation"]
# Read at call time (never bound as a default arg) so it can be overridden on the module.
PRODUCT_WIP_CAP = PIPELINE["productWip"]["maxGateAPending"]
# A resume younger than this is treated as possibly still live (item: liveness signal).
RESUME_LIVE_WINDOW_MINUTES = PIPELINE["resume"]["liveWindowMinutes"]
EVIDENCE_CARRY_FORWARD_GLOBS = tuple(
    g.replace("{docRoot}", DOC_ROOT.rstrip("/"))
    for g in ((PIPELINE["epicClose"].get("evidenceCarryForward") or {}).get("paths")
              or _PIPELINE_DEFAULTS["epicClose"]["evidenceCarryForward"]["paths"]))


def issue_branch(number: int) -> str:
    """The per-child working branch (default `issue-<n>`)."""
    return f"{ISSUE_BRANCH_PREFIX}{number}"


def issue_number_from_branch(branch: str) -> Optional[int]:
    """Inverse of `issue_branch`; None for any branch that isn't one."""
    if not branch.startswith(ISSUE_BRANCH_PREFIX):
        return None
    rest = branch[len(ISSUE_BRANCH_PREFIX):]
    return int(rest) if rest.isdigit() else None


# Schema-level GraphQL ids for the repo's provisioned Issue Types and fields.
ISSUE_TYPE_IDS = _PF["issueTypeIds"]
# Types `create-issue --type` accepts; each still needs its id in `issueTypeIds`.
KNOWN_ISSUE_TYPES = ("Task", "Bug", "Feature", "Epic", "Initiative")
# Optional fields: unset when the org has not provisioned them.
PRIORITY_FIELD_ID = _PF.get("priorityFieldId")
PRIORITY_OPTION_IDS = _PF.get("priorityOptionIds") or {}
EFFORT_FIELD_ID = _PF.get("effortFieldId")
EFFORT_OPTION_IDS = _PF.get("effortOptionIds") or {}
# Lower sorts first; an issue with no Priority value ranks as Medium.
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
# Common spellings from other trackers -> this repo's top Priority value. Accepted by
# create-issue / repair-issue so `--priority Critical` (or `Blocker`) is not refused.
PRIORITY_ALIASES = {"critical": "Urgent", "blocker": "Urgent"}
# The native Stage / Pipeline Status fields are the sole source of truth for
# stage and status, so a failed write must raise, never be swallowed.
STAGE_FIELD_ID = _PF["stageFieldId"]
STAGE_OPTION_IDS = _PF["stageOptionIds"]
STAGE_FIELD_NAMES = {
    "Product": "product", "Architecture": "architecture", "Development": "development",
    "PR Review": "pr-review", "LLD": "lld",
    # Retired: nothing writes it; still read so an issue left at it is picked up.
    "Testing": "testing",
}

# Every accepted spelling of a Stage (field value or slug, any case, space or hyphen) -> slug.
_STAGE_ALIASES = {spelling.lower().replace(" ", "-"): slug
                  for display, slug in STAGE_FIELD_NAMES.items()
                  for spelling in (display, slug)}


def normalize_stage(name: str) -> Optional[str]:
    """Canonical slug for any accepted spelling of a Stage, or None if unknown."""
    return _STAGE_ALIASES.get((name or "").strip().lower().replace(" ", "-"))


# Stages a child with a draft PR awaiting `pr-review` can be at (`testing` is retired).
REVIEW_ENTRY_STAGES = ("pr-review", "testing")
# Retired role -> the live role that claims a child stranded at it.
RETIRED_ROLES = {"testing": "development"}
PIPELINE_STATUS_FIELD_ID = _PF["pipelineStatusFieldId"]
PIPELINE_STATUS_OPTION_IDS = _PF["pipelineStatusOptionIds"]
PIPELINE_STATUS_FIELD_NAMES = {
    "Todo": "todo", "In Progress": "in-progress",
    "Awaiting Human Review": "awaiting-human-review", "Needs Human": "needs-human",
    "Feedback Received": "feedback-received", "Done": "done",
}
# Statuses meaning "a gate PR is open"; `feedback-received` is only a visibility
# flip on top of `awaiting-human-review`, so test membership, never equality.
GATE_PENDING_STATUSES = ("awaiting-human-review", "feedback-received")

Runner = Callable[[list], str]


class GhError(RuntimeError):
    pass


def _utc_now_marker() -> str:
    """UTC timestamp in the format every marker comment uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_runner(argv: list) -> str:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}")
    return result.stdout


@runtime_checkable
class WorkItemProvider(Protocol):
    """Work-item-tracker contract every `cmd_*` function is written against;
    `GitHub` is the only implementation. Issue management only -- PR/branch/CI
    mechanics are deliberately out of scope."""

    def issue_list(self) -> list: ...
    def issue_view(self, number: int) -> dict: ...
    def issue_edit(self, number: int, add_labels: list = (), remove_labels: list = (),
                    add_assignees: list = (), remove_assignees: list = ()) -> None: ...
    def issue_node_id(self, number: int) -> str: ...
    def issue_fields(self, number: int) -> dict: ...
    def issue_epic_info(self, number: int) -> dict: ...
    def issue_create(self, title: str, body: str, labels: list) -> int: ...
    def issue_comment(self, number: int, body: str) -> None: ...
    def issue_close(self, number: int, reason: str = "completed") -> None: ...
    def blocked_by(self, number: int) -> list: ...
    def add_blocked_by(self, issue_number: int, blocking_number: int) -> None: ...
    def remove_blocked_by(self, issue_number: int, blocking_number: int) -> None: ...
    def blocking(self, number: int) -> list: ...
    def set_issue_type(self, number: int, type_name: str) -> None: ...
    def set_stage_field(self, number: int, stage: str) -> None: ...
    def set_pipeline_status_field(self, number: int, status: str) -> None: ...
    def set_priority_field(self, number: int, priority: str) -> None: ...
    def set_effort_field(self, number: int, effort: str) -> None: ...
    def clear_stage_and_status_fields(self, number: int) -> None: ...
    def clear_stage_field(self, number: int) -> None: ...
    def add_sub_issue(self, parent_number: int, child_number: int) -> None: ...
    def remove_sub_issue(self, parent_number: int, child_number: int) -> None: ...

    def classify_unit(self, number: int) -> str:
        """"initiative", "epic", "task" or "other" for `number`."""
        ...


class GitHub:
    """The only place gh/GraphQL subprocess calls happen. Inject a fake `runner` in tests."""

    def __init__(self, repo: str = REPO, runner: Runner = _default_runner):
        self.repo = repo
        self._run = runner

    def issue_list(self) -> list:
        """Every issue, open and closed, with labels, fields, `issueType` and `parent`
        flattened. GraphQL because `gh issue list --json` exposes neither of the last two."""
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
        """One issue's single-select field values, name -> value (e.g. {"Stage": "LLD"})."""
        data = self.graphql(_ISSUE_FIELDS_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["issueFieldValues"]["nodes"]
        return {n["field"]["name"]: n["name"] for n in nodes if n["__typename"] == "IssueFieldSingleSelectValue"}

    def issue_epic_info(self, number: int) -> dict:
        """One issue's issueType/parent/labels (labels flattened), for classification."""
        data = self.graphql(_ISSUE_EPIC_CHECK_QUERY.format(n=number))
        node = data["repository"]["issue"]
        node["labels"] = node["labels"]["nodes"]
        return node

    def classify_unit(self, number: int) -> str:
        """Classify one issue by number. A caller already holding `issue_list()` dicts
        uses `classify_unit_from_issue` directly to avoid a fetch per issue."""
        return classify_unit_from_issue(self.issue_epic_info(number))

    def blocked_by(self, number: int) -> list:
        """Open issue numbers blocking `number` (native blockedBy); closed ones are omitted."""
        data = self.graphql(_BLOCKED_BY_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blockedBy"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def add_blocked_by(self, issue_number: int, blocking_number: int):
        issue_id = self.issue_node_id(issue_number)
        blocking_id = self.issue_node_id(blocking_number)
        self.graphql(_ADD_BLOCKED_BY_MUTATION.format(issue_id=issue_id, blocking_id=blocking_id))

    def remove_blocked_by(self, issue_number: int, blocking_number: int):
        issue_id = self.issue_node_id(issue_number)
        blocking_id = self.issue_node_id(blocking_number)
        self.graphql(_REMOVE_BLOCKED_BY_MUTATION.format(issue_id=issue_id, blocking_id=blocking_id))

    def blocking(self, number: int) -> list:
        """Open issue numbers that `number` blocks (the reverse of `blocked_by`)."""
        data = self.graphql(_BLOCKING_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blocking"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def set_issue_type(self, number: int, type_name: str):
        if type_name not in ISSUE_TYPE_IDS:
            raise GhError(
                f"type_name={type_name!r} is not in projectFields.issueTypeIds "
                f"(configured: {sorted(ISSUE_TYPE_IDS)}) -- Epic/Initiative "
                f"types need provisioning as real GitHub Issue Types (org-level) and "
                f"their GraphQL ids added to config before "
                f"this call can set them; a bare KeyError here would hide that.")
        issue_id = self.issue_node_id(number)
        type_id = ISSUE_TYPE_IDS[type_name]
        self.graphql(_SET_ISSUE_TYPE_MUTATION.format(issue_id=issue_id, type_id=type_id))

    def _set_single_select(self, number: int, field_id: str, option_id: str):
        issue_id = self.issue_node_id(number)
        self.graphql(_SET_ISSUE_FIELD_MUTATION.format(
            issue_id=issue_id, field_id=field_id, option_id=option_id))

    def set_stage_field(self, number: int, stage: str):
        """Write the Stage field. Never swallows GhError: an unset Stage reads as a fresh issue."""
        self._set_single_select(number, STAGE_FIELD_ID, STAGE_OPTION_IDS[stage])

    def set_pipeline_status_field(self, number: int, status: str):
        """Write the Pipeline Status field (`status` like "in-progress"); never swallows GhError."""
        self._set_single_select(number, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS[status])

    def set_priority_field(self, number: int, priority: str):
        self._set_single_select(number, PRIORITY_FIELD_ID, PRIORITY_OPTION_IDS[priority])

    def set_effort_field(self, number: int, effort: str):
        self._set_single_select(number, EFFORT_FIELD_ID, EFFORT_OPTION_IDS[effort])

    def clear_stage_and_status_fields(self, number: int):
        """Delete both Stage and Pipeline Status: an Epic handing off to its Tasks has no
        stage of its own but is not `Done` either."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=PIPELINE_STATUS_FIELD_ID))

    def clear_stage_field(self, number: int):
        """Delete only the Stage value (Pipeline Status untouched)."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))

    def add_sub_issue(self, parent_number: int, child_number: int):
        parent_id = self.issue_node_id(parent_number)
        child_id = self.issue_node_id(child_number)
        self.graphql(_ADD_SUB_ISSUE_MUTATION.format(parent_id=parent_id, child_id=child_id))

    def remove_sub_issue(self, parent_number: int, child_number: int):
        parent_id = self.issue_node_id(parent_number)
        child_id = self.issue_node_id(child_number)
        self.graphql(_REMOVE_SUB_ISSUE_MUTATION.format(parent_id=parent_id, child_id=child_id))

    def issue_comment(self, number: int, body: str):
        self._run(["gh", "issue", "comment", str(number), "--repo", self.repo, "--body", body])

    def issue_close(self, number: int, reason: str = "completed"):
        self._run(["gh", "issue", "close", str(number), "--repo", self.repo,
                   "--reason", reason])

    def delete_branch(self, branch: str):
        """Delete `origin/<branch>` GitHub-side (REST); a missing ref raises GhError."""
        self._run(["gh", "api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{branch}"])

    def pr_comment(self, number: int, body: str):
        self._run(["gh", "pr", "comment", str(number), "--repo", self.repo, "--body", body])

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
        """Whether `path` exists on `ref`, checked GitHub-side (independent of any local checkout)."""
        try:
            self._run(["gh", "api", f"repos/{self.repo}/contents/{path}?ref={ref}",
                       "--jq", ".sha"])
            return True
        except GhError:
            return False

    def pr_list_for_branch(self, branch: str, state: str = "open") -> list:
        """PRs (default open) whose head branch is exactly `branch`."""
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
            # gh errors instead of returning [] when no check ever reported; a required
            # workflow that never ran is caught later as missing-checks.
            if "no checks reported" in str(e):
                return []
            raise
        return json.loads(out)

    def pr_files(self, number: int) -> list:
        """Every changed path of a PR, via paginated REST `pulls/{n}/files`. `gh pr view
        --json files` caps at 100 and `gh pr diff` 406s past 20k lines; this feeds the
        merge gate, so a truncated list could let code merge without CI."""
        out = self._run(["gh", "api", "--paginate", f"repos/{self.repo}/pulls/{number}/files",
                          "--jq", ".[].filename"])
        return [line for line in out.splitlines() if line.strip()]

    def branch_behind_by(self, head: str, base: str = "main") -> int:
        """Commits `base` has that `head` lacks (REST compare, GitHub-side)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                          "--jq", ".behind_by"])
        return int(out.strip())

    def branch_head_sha(self, branch: str) -> str:
        """Head commit of `branch`, GitHub-side."""
        return self._run(["gh", "api", f"repos/{self.repo}/branches/{branch}",
                          "--jq", ".commit.sha"]).strip()

    def files_since(self, sha: str, branch: str) -> list:
        """Files changed on `branch` since `sha` (REST compare; capped at 300 files)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{sha}...{branch}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def base_delta_files(self, head: str, base: str = "main") -> list:
        """Files `base` changed since its merge-base with `head` (REST compare). GitHub
        returns at most 300 files; callers treat a delta at that cap as truncated."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{head}...{base}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def branch_commits(self, base: str, head: str) -> list:
        """Shas of the commits on `head` that `base` lacks (REST compare, capped at 250)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                          "--jq", ".commits[]?.sha"])
        return [line for line in out.splitlines() if line.strip()]

    def commit_files(self, sha: str) -> list:
        """Files one commit changed against its first parent (capped at 300)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/commits/{sha}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def pr_ready(self, number: int):
        self._run(["gh", "pr", "ready", str(number), "--repo", self.repo])

    def pr_add_label(self, number: int, label: str):
        """Add `label` to a PR, creating it in the repo first (idempotent)."""
        self._run(["gh", "label", "create", label, "--repo", self.repo, "--force"])
        self._run(["gh", "pr", "edit", str(number), "--repo", self.repo, "--add-label", label])

    def pr_merge(self, number: int, delete_branch: bool = True, method: str = "squash",
                 match_head: Optional[str] = None):
        """`match_head` makes GitHub refuse the merge unless the head is still that SHA."""
        argv = ["gh", "pr", "merge", str(number), "--repo", self.repo, f"--{method}"]
        if delete_branch:
            argv.append("--delete-branch")
        if match_head:
            argv += ["--match-head-commit", match_head]
        self._run(argv)

    def reply_review_thread(self, thread_id: str, body: str):
        self.graphql(_REPLY_REVIEW_THREAD_MUTATION, threadId=thread_id, body=body)

    def resolve_review_thread(self, thread_id: str):
        self.graphql(_RESOLVE_REVIEW_THREAD_MUTATION, threadId=thread_id)

    def graphql(self, query: str, **variables: str) -> dict:
        """Run `query`; `variables` pass as GraphQL string variables (no escaping needed)."""
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            argv += ["-f", f"{name}={value}"]
        return json.loads(self._run(argv))["data"]


def get_work_item_provider(runner: Runner = _default_runner) -> WorkItemProvider:
    """The configured `pipeline.workItemProvider`; only "github" exists, anything else raises."""
    provider = PIPELINE.get("workItemProvider", {}).get("type", "github")
    if provider == "github":
        return GitHub(runner=runner)
    raise GhError(f"pipeline.workItemProvider.type={provider!r} is not implemented -- "
                  f"only 'github' exists today (see README.md, 'Initiative-driven lifecycle')")


def label_names(issue: dict) -> set:
    return {l["name"] for l in issue.get("labels", [])}


def has_label(issue: dict, name: str) -> bool:
    return name in label_names(issue)


def current_stage(issue: dict) -> Optional[str]:
    """The issue's Stage field as a stage name, or None."""
    return STAGE_FIELD_NAMES.get(issue.get("fields", {}).get("Stage"))


def pipeline_status(issue: dict) -> Optional[str]:
    """The issue's Pipeline Status as a slug (e.g. "in-progress"), or None when unset."""
    return PIPELINE_STATUS_FIELD_NAMES.get(issue.get("fields", {}).get("Pipeline Status"))


def issue_type(issue: dict) -> Optional[str]:
    t = issue.get("issueType")
    return t.get("name") if t else None


def classify_unit_from_issue(issue: dict) -> str:
    """"initiative"/"epic"/"task"/"other" for an issue dict with flattened `labels`,
    per `pipeline.classification`; "other" when no rule matches."""
    rules = PIPELINE.get("classification", {})
    if not rules:
        return "other"
    it = issue_type(issue)
    labels = label_names(issue)
    for kind, rule in rules.items():
        field, value = rule.get("field"), rule.get("value")
        if field == "issueType" and it == value:
            return kind
        if field == "label" and value in labels:
            return kind
    return "other"


def is_epic(issue: dict) -> bool:
    """Classified "epic" (standing and legacy epics included)."""
    return classify_unit_from_issue(issue) == "epic"


def is_initiative(issue: dict) -> bool:
    """Classified "initiative"."""
    return classify_unit_from_issue(issue) == "initiative"


def resolve_profile(epic_issue: Optional[dict]) -> dict:
    """The epic's first matching `pipeline.profiles` entry, with every toggle, `name`
    and a complete `gates` block filled in from the defaults."""
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
    """The epic profile's resolved `gates` block."""
    return resolve_profile(epic_issue)["gates"]


def is_epic_standing(issue: dict) -> bool:
    """Standing epic (profile `epicLevelPhase` false): no phase-Tasks; each child runs
    its own full flow and integrates into `main`."""
    return not resolve_profile(issue)["epicLevelPhase"]


def is_epic_legacy(issue: dict) -> bool:
    """Epic the pipeline does not drive at all (profile `driven` false)."""
    return not resolve_profile(issue)["driven"]


def is_epic_architected(issue: dict) -> bool:
    """Whether the Epic's design phase is done (label set by `merge-lld-doc`). A label, not
    a marker, so it is readable from the bulk `issue_list()` fetch."""
    return has_label(issue, LABELS["architected"])


def default_stage(parent: Optional[dict]) -> Optional[str]:
    """Stage an unstaged child of `parent` starts at: `product`, or None for an Epic's
    child (a standing child is routed at pickup, a non-standing Epic's is staged explicitly)."""
    return None if parent is not None and is_epic(parent) else "product"


def product_gate_pending(all_issues: list) -> list:
    """Sorted numbers of open issues, repo-wide, at Stage=Product with an open Gate A.
    Repo-wide on purpose: one human reviews Gate A across every epic."""
    return sorted(i["number"] for i in all_issues
                  if i["state"] == "OPEN" and current_stage(i) == "product"
                  and pipeline_status(i) in GATE_PENDING_STATUSES)


def product_wip_headroom(all_issues: list) -> Optional[int]:
    """Fresh `product` delegations allowed before the Gate A queue hits
    `PRODUCT_WIP_CAP`; None when the cap is disabled."""
    cap = PRODUCT_WIP_CAP
    if not cap or cap <= 0:
        return None
    return max(0, cap - len(product_gate_pending(all_issues)))


def priority_rank(issue: dict) -> int:
    """Rank from the native "Priority" field (lower first); unset ranks as Medium."""
    return PRIORITY_RANK.get(issue.get("fields", {}).get("Priority"), 2)


def parse_created_at(issue: dict) -> datetime:
    return datetime.strptime(issue["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _parse_marker_ts(ts: str) -> datetime:
    """Parse a UTC comment/marker timestamp (`%Y-%m-%dT%H:%M:%SZ`) to an aware datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sort_key(issue: dict) -> tuple:
    return (priority_rank(issue), parse_created_at(issue))


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

LOCAL_CI_SUITES = tuple(dict.fromkeys(w["suite"] for w in CONFIG["requiredWorkflows"]))

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


def local_ci_suites_attested(comments: list, head_sha: str) -> set:
    """Set of suite keys with a `local-ci` attestation matching `head_sha`.
    Prefix match either way (short vs full sha); an unknown head trusts nothing."""
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

_REMOVE_SUB_ISSUE_MUTATION = """mutation {{
  removeSubIssue(input: {{issueId: "{parent_id}", subIssueId: "{child_id}"}}) {{
    subIssue {{ number }}
  }}
}}"""

_REMOVE_BLOCKED_BY_MUTATION = """mutation {{
  removeBlockedBy(input: {{issueId: "{issue_id}", blockingIssueId: "{blocking_id}"}}) {{
    issue {{ number }}
  }}
}}"""

_REPLY_REVIEW_THREAD_MUTATION = """mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}"""

_RESOLVE_REVIEW_THREAD_MUTATION = """mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}"""

# Resolve the repo sentinels once; `.format(...)` placeholders are left for call time.
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
    """The long-lived integration branch for a non-standing Epic (default `epic-<n>`)."""
    return f"{EPIC_BRANCH_PREFIX}{epic}"


def unit_branch(unit: str, number: int) -> str:
    """The branch for an `issue` or `epic` unit, using the configured prefixes."""
    return epic_branch(number) if unit == "epic" else issue_branch(number)


# Stages only a non-standing Epic's phase-Tasks (Architecture-phase, Architecture
# revision, LLD-phase) sit at; functional Tasks enter at `development`.
PHASE_TASK_STAGES = frozenset({"architecture", "arch-review", "lld", "lld-review"})


def integration_base(gh: "GitHub", issue: int, unit: str = "issue") -> str:
    """Branch this unit integrates into: `epic-<parent>` for every child of a non-standing
    Epic (phase-Tasks included), else `main` (epics, parentless issues, standing/Initiative
    children)."""
    if unit == "epic":
        return "main"
    # `parent` exists only in `issue_list`'s GraphQL; `gh issue view --json` has no such field.
    issues = {i["number"]: i for i in gh.issue_list()}
    entry = issues.get(issue)
    if entry is None:
        raise GhError(f"issue #{issue} not found in the repo issue list")
    parent = entry.get("parent")
    if not parent:
        return "main"
    parent_number = parent["number"]
    parent_entry = issues.get(parent_number)
    if parent_entry is not None and (is_epic_standing(parent_entry) or is_initiative(parent_entry)):
        return "main"
    return epic_branch(parent_number)


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


# Closing evidence `close-epic` requires. The full e2e suite is not one: every Epic has its own
# standing e2e-test Task, which owns that evidence. `record-epic-verification --kind e2e`
# is still accepted (informational), it just no longer gates the merge.
EPIC_CLOSE_REQUIRED_EVIDENCE = (("exploratory", "exploratory pass"),)


def missing_epic_verification(comments: list, stale_since: Optional[Callable] = None) -> list:
    """Problems with an epic's exploratory closing evidence; evidence older than
    the last `origin/main` reconcile counts as missing. `stale_since(sha)` returns a problem
    when the epic branch moved on from the tested `sha` with code changes. Empty = complete."""
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
        elif reconciled is not None and seen[kind][0] < reconciled:
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


class _EpicEvidenceDelta:
    """`stale_since` for `missing_epic_verification`: how `branch` moved on from a tested
    `sha` (`fresh` | `carried_forward` | `stale` | `uncomparable`, with the files), remembered
    per sha so close-epic can report what it carried forward."""

    def __init__(self, gh, branch: str, carry_forward: bool = True):
        self.gh, self.branch, self.carry_forward, self.deltas = gh, branch, carry_forward, {}

    def classify(self, sha: str) -> dict:
        if sha not in self.deltas:
            try:
                files = self.gh.files_since(sha, self.branch)
                status = ("fresh" if not files
                          else "carried_forward" if self.carry_forward and len(files) < 300
                          and all(evidence_carries_forward(p) for p in files)
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
    if suite in NON_ATTESTABLE_SUITES:
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


def _epic_child_chain(gh, branch: str, children: list) -> dict:
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
    commits = gh.branch_commits("main", branch)
    direct = None if len(commits) >= 250 else [c for c in commits if c not in merged]
    return {"prs": prs, "direct": direct}


def _epic_suite_evidence(gh, branch: str, children: list, epic_files: list,
                         epic_pr: Optional[int], carry_forward: bool = True) -> dict:
    """Per required suite (base `main`) at the epic head: how it is satisfied -- the epic
    PR's own check/attestation, or the child chain (every merged child PR that touched the
    suite is satisfied and no direct epic commit touches it). Returns `suites` (status per
    suite), `unattested` / `awaiting` (attestable / non-attestable suites still `missing`),
    `missing` (their workflows),
    `breaks` (why a chain failed) and `carried_files`."""
    truncated = len(epic_files) >= 300
    view, checks = {}, []
    if epic_pr is not None:
        view = gh.pr_view(epic_pr, "comments,headRefOid")
        checks = gh.pr_checks(epic_pr)
    chain = None
    suites, unattested, awaiting, missing, breaks, carried = {}, [], [], [], {}, set()
    for spec in REQUIRED_WORKFLOWS:
        suite = spec["suite"]
        if not _workflow_applies_to_base(spec, "main"):
            continue
        if not truncated and not any(_workflow_covers(spec, p) for p in epic_files):
            suites[suite] = "not_required"
            continue
        status = _pr_suite_evidence(gh, spec, view, checks, branch, carry_forward, carried) \
            if epic_pr is not None else None
        if status is None and carry_forward:
            chain = chain or _epic_child_chain(gh, branch, children)
            status, why = _chain_suite_evidence(gh, spec, chain, branch, carried)
            if why:
                breaks[suite] = why
        if status is None:
            status = "missing"
            missing.append(spec["workflow"])
            (awaiting if suite in NON_ATTESTABLE_SUITES else unattested).append(suite)
        suites[suite] = status
    return {"suites": suites, "unattested": unattested, "awaiting": awaiting, "missing": missing,
            "breaks": breaks, "carried_files": carried}


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
                                 sha: Optional[str] = None) -> dict:
    """Post one half (`e2e` | `exploratory`) of an epic's closing verification marker, stamped
    with the tested epic-branch head (`sha`, default: the branch head now)."""
    if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise GhError(f"--sha must be a 7-40 char hex commit id, got {sha!r}")
    sha = sha or gh.branch_head_sha(epic_branch(epic))
    timestamp = _utc_now_marker()
    label = "Full e2e suite" if kind == "e2e" else "Exploratory pass"
    gh.issue_comment(epic,
        f"🧪 {label} — closing verification for #{epic} at `{sha[:10]}`. {summary}\n\n"
        f"<!-- epic-verification: {kind}:{epic} sha:{sha} @ {timestamp} -->")
    return {"epic": epic, "kind": kind, "sha": sha, "recorded": True}


def _close_epic_evidence(gh, epic: int, branch: str, children: list, epic_pr: Optional[int],
                         carry_forward: bool) -> dict:
    """The evidence fields every close-epic result carries: `missing_verification` (when
    any), `unattested_suites`, `evidence`, `carried_forward_files`, `evidence_breaks`,
    plus the private `_missing_workflows` the merge gate reads."""
    comments = gh.issue_view(epic).get("comments", [])
    delta = _EpicEvidenceDelta(gh, branch, carry_forward)
    problems = missing_epic_verification(comments, delta)
    files = gh.pr_files(epic_pr) if epic_pr is not None else gh.base_delta_files("main", base=branch)
    suites = _epic_suite_evidence(gh, branch, children, files, epic_pr, carry_forward)
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
    if suites["breaks"]:
        result["evidence_breaks"] = suites["breaks"]
    return result


def _close_epic_exploratory_problems(gh, epic: int, branch: str, carry_forward: bool) -> list:
    """Just the exploratory half of `_close_epic_evidence`, to decide before the epic PR is
    opened whether the second call can proceed at all."""
    return missing_epic_verification(gh.issue_view(epic).get("comments", []),
                                     _EpicEvidenceDelta(gh, branch, carry_forward))


def cmd_close_epic(gh: GitHub, epic: int, repo_path: str = ".",
                    runner: Runner = _default_runner, carry_forward: bool = True) -> dict:
    """Reconcile the epic branch with `main` (first call, then stop), or merge it once
    closing evidence is recorded (second call). Returns `merged`; refusals carry `reason`.
    Two calls because verification must run between the two merges. `carry_forward=False`
    accepts only evidence stamped at the epic head itself."""
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
        timestamp = _utc_now_marker()
        gh.issue_comment(epic,
            f"🔄 Reconciled `{branch}` with `origin/main` ({behind} commit(s) picked up). "
            f"Closing verification must now run against **this** tree — the exploratory pass. "
            f"Evidence recorded before this point describes a "
            f"different tree and will not be accepted.\n\n"
            f"<!-- epic-reconciled: {epic} @ {timestamp} -->")
        existing = gh.pr_list_for_branch(branch)
        evidence = _close_epic_evidence(gh, epic, branch, children,
                                        existing[0]["number"] if existing else None, carry_forward)
        evidence.pop("_missing_workflows")
        return _with_workspace(
            {"epic": epic, "merged": False, "branch": branch, "reconciled": behind, **evidence,
             "reason": f"picked up {behind} commit(s) from main -- run the exploratory pass "
                       f"against the reconciled branch, then re-run close-epic"},
            ws)
    existing = gh.pr_list_for_branch(branch)
    if _close_epic_exploratory_problems(gh, epic, branch, carry_forward):
        evidence = _close_epic_evidence(gh, epic, branch, children,
                                        existing[0]["number"] if existing else None, carry_forward)
        evidence.pop("_missing_workflows")
        return {"epic": epic, "merged": False, "branch": branch, **evidence,
                "reason": f"closing verification incomplete: "
                          f"{'; '.join(evidence['missing_verification'])}"}
    if existing:
        pr_number = existing[0]["number"]
    else:
        pr_number = gh.pr_create(
            base="main", head=branch,
            title=f"{detail['title']} — epic integration (#{epic})",
            body=f"Integration of every child of #{epic}.\n\nCloses #{epic}",
            draft=True)
    # The PR's files, checks and attestations are the gate's inputs, so it must exist first.
    evidence = _close_epic_evidence(gh, epic, branch, children, pr_number, carry_forward)
    missing_workflows = evidence.pop("_missing_workflows")
    if missing_workflows:
        return {"epic": epic, "merged": False, "pr": pr_number, "checks": "missing-checks",
                **evidence,
                "reason": f"PR #{pr_number} required suites unsatisfied at the epic head "
                          f"(no passing check, attestation or child chain from: "
                          f"{', '.join(missing_workflows)})"}
    gh.pr_ready(pr_number)
    gh.pr_merge(pr_number)
    # The PR's `Closes #<n>` closes the epic; its terminal fields are set here.
    cmd_mark_issue_closed(gh, epic)
    return {"epic": epic, "merged": True, "pr": pr_number, "branch": branch, **evidence,
            "worktree": release_worktree(branch, runner=runner, base_repo=repo_path)}


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


def cmd_check_initiative_closeable(gh: GitHub, initiative: int) -> dict:
    """`closeable` when at least one Epic was cut from the Initiative and all are closed;
    otherwise `reason` (and `open_epics`)."""
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    initiative_issue = by_number.get(initiative)
    if initiative_issue is None or not is_initiative(initiative_issue):
        raise GhError(f"#{initiative} is not an Initiative -- pass the Initiative's own issue number")
    cut_epics = [i for i in all_issues if i.get("parent")
                 and i["parent"]["number"] == initiative and is_epic(i)]
    if not cut_epics:
        return {"initiative": initiative, "closeable": False,
                "reason": "no Epics have been cut from this Initiative yet"}
    open_epics = sorted(i["number"] for i in cut_epics if i["state"] != "CLOSED")
    if open_epics:
        return {"initiative": initiative, "closeable": False, "open_epics": open_epics,
                "reason": f"{len(open_epics)} cut Epic(s) still open: "
                          f"{', '.join(f'#{n}' for n in open_epics)}"}
    return {"initiative": initiative, "closeable": True,
            "epics": sorted(i["number"] for i in cut_epics)}


def cmd_close_initiative(gh: GitHub, initiative: int) -> dict:
    """Close the Initiative once every cut Epic is closed and its verification is
    recorded. Returns `closed`; refusals carry `reason`."""
    check = cmd_check_initiative_closeable(gh, initiative)
    if not check["closeable"]:
        return {"initiative": initiative, "closed": False,
                **{k: v for k, v in check.items() if k not in ("closeable",)}}
    detail = gh.issue_view(initiative)
    missing = missing_initiative_verification(detail.get("comments", []))
    if missing:
        return {"initiative": initiative, "closed": False, "missing_verification": missing,
                "reason": f"closing verification incomplete: {'; '.join(missing)}"}
    gh.issue_close(initiative)
    # Also set here so a repo without the `issues: closed` Action still ends correct.
    cmd_mark_issue_closed(gh, initiative)
    return {"initiative": initiative, "closed": True, "epics": check["epics"]}


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
        state = {"run_id": run_id, "terminal": []}
        _write_run_state(epic, state)
    return state


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
                          run_id: Optional[str] = None) -> Optional[dict]:
    """Idempotently count `issue` against its parent's tracked run (from `merge-pr` /
    `close-issue`). Returns the count summary, or None when no run is tracked.
    An empty run-state dir short-circuits before any tracker call.

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
    if issue not in state["terminal"] or str(issue) in state.get("in_flight", {}):
        if issue not in state["terminal"]:
            state["terminal"].append(issue)
        state.get("in_flight", {}).pop(str(issue), None)
        _write_run_state(parent, state)
    return {"epic": parent, "run_id": state.get("run_id"),
            "terminal_count": len(state["terminal"]), "cap": MAX_TASKS_PER_RUN}


PHASE_TASK_TITLES = {"architecture": "Architecture phase", "lld": "LLD phase"}


def epic_lacks_phase_tasks(all_issues: list, epic: int) -> bool:
    """Whether a non-standing Epic still needs `cut-phase-tasks`: it has no non-Epic child,
    or only one of its two phase-Tasks (a half-made cut). By issueType and titles, never labels."""
    kids = [i for i in all_issues if (i.get("parent") or {}).get("number") == epic
            and not is_epic(i)]
    if not kids:
        return True
    titles = {k["title"] for k in kids}
    return (PHASE_TASK_TITLES["architecture"] in titles) != (PHASE_TASK_TITLES["lld"] in titles)


def _epic_wait_reason(gh: GitHub, epic_issue: dict) -> Optional[str]:
    """Why an open Epic cannot be run yet (`legacy`, or `blocked by #n`), else None."""
    if is_epic_legacy(epic_issue):
        return "not driven (legacy profile)"
    blockers = gh.blocked_by(epic_issue["number"])
    return f"blocked by {', '.join(f'#{n}' for n in blockers)}" if blockers else None


def _initiative_epic_step(gh: GitHub, all_issues: list, initiative: int,
                          skip: list) -> Optional[dict]:
    """The Initiative loop's next move: `cut-phase-tasks` or `run-epic` for the lowest-numbered
    open, unblocked cut Epic not in `skip`; None when no Epic can run."""
    epics = sorted((i for i in all_issues if i["state"] == "OPEN" and is_epic(i)
                    and (i.get("parent") or {}).get("number") == initiative),
                   key=lambda i: i["number"])
    for e in epics:
        n = e["number"]
        if n in skip or _epic_wait_reason(gh, e):
            continue
        base = {"epic": n, "initiative": initiative, "unit": "epic"}
        if not is_epic_standing(e) and epic_lacks_phase_tasks(all_issues, n):
            return {"action": "cut-phase-tasks", **base,
                    "reason": f"Epic #{n} has no phase-Tasks: run `cut-phase-tasks {n} "
                              f"--repo-path <p>`, then `next-action {initiative}` again"}
        return {"action": "run-epic", **base,
                "reason": f"Epic #{n} is the first open, unblocked Epic of Initiative "
                          f"#{initiative}: run its Step 1-3 loop (`next-action {n}` with "
                          f"this --run-id), then return to the Initiative"}
    return None


def _open_epics_reason(gh: GitHub, cut_epics: list, skip: list) -> str:
    """The Initiative `none` reason for open Epics that cannot run: each one's actual state."""
    open_epics = sorted((i for i in cut_epics if i["state"] != "CLOSED"),
                        key=lambda i: i["number"])
    states = [f"#{e['number']} " + ("parked this run" if e["number"] in skip
                                     else _epic_wait_reason(gh, e) or "runnable")
              for e in open_epics]
    closed = len(cut_epics) - len(open_epics)
    return (f"{len(open_epics)} cut Epic(s) open, none runnable now: {'; '.join(states)}"
            + (f" ({closed} closed)" if closed else "") + ".")


def _merged_design_action(gh: GitHub, issue: dict, epic: int) -> Optional[dict]:
    """The completion action for an architecture/lld phase-Task whose design PR a human
    merged before any gate opened: `pass-gate` (architecture) or `finish-lld` (lld)."""
    stage = current_stage(issue)
    # Only a claimed unit: a gate-pending one is `pass-gate`'s, an unclaimed one has no PR yet.
    if stage not in DESIGN_STAGE_REVIEW or pipeline_status(issue) != "in-progress":
        return None
    found = find_design_pr(gh.issue_view(issue["number"]).get("comments", []))
    if not found or found[0] != stage \
            or gh.pr_view(found[1], fields="state").get("state") != "MERGED":
        return None
    base = {"issue": issue["number"], "unit": "issue", "design_pr": found[1]}
    if stage == "architecture":
        return {"action": "pass-gate", **base, "gate_pr": found[1], "stage": stage}
    return {"action": "finish-lld", **base, "epic": epic}


def decide_next_action(gh: GitHub, epic: int, run_id: Optional[str] = None,
                       skip_epics: Optional[list] = None, repo_path: str = ".",
                       runner: Runner = _default_runner) -> dict:
    """Pick the next action among the named Epic's/Initiative's open non-Epic children: `skip` |
    `resume` | `pass-gate` | `finish-lld` | `address-gate-feedback` | `route` | `delegate` |
    `stop-at-cap` | `none`; an Initiative with no such child walks its open Epics: `cut-phase-tasks`
    | `run-epic` (`skip_epics` parks stalled ones). Raises GhError when `epic` is neither.
    Caps gate only fresh work: the run cap never defers a unit this run already handed out, and
    a fresh dev-lane unit needs a free slot and a footprint disjoint from the holders'
    (`dev_lane_slots`); `slots` reports that arithmetic on a dev-lane `delegate` or `none`."""
    skip_epics = skip_epics or []
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None:
        raise GhError(f"#{epic} is not an epic or Initiative -- "
                       f"pass the epic's/Initiative's own issue number, not a child issue's")
    if not is_epic(epic_issue) and not is_initiative(epic_issue):
        raise GhError(f"#{epic} is not an epic or Initiative (pipeline.classification "
                       f"matches neither) -- pass the unit's own issue number, not a child "
                       f"issue's")
    if is_epic_legacy(epic_issue):
        return {"action": "skip", "epic": epic,
                "reason": "epic:legacy -- this pipeline no longer drives this epic or any of "
                          "its children; skipping entirely."}
    children = [i for i in all_issues if i["state"] == "OPEN"
                and i.get("parent") and i["parent"]["number"] == epic and not is_epic(i)]
    # A profile whose Gate A auto-passes never fills the human queue, so it is uncapped.
    product_headroom = (product_wip_headroom(all_issues)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    deferred_by_cap: list = []
    unstaged: list = []
    # Run cap gates only fresh work; refusing in-flight work would strand it mid-pipeline.
    cap_state = run_cap_state(epic, run_id)
    at_cap = run_cap_reached(cap_state)
    # Units this run already handed out (`note_in_flight`): started work the cap never defers.
    in_flight = {int(n) for n in (cap_state or {}).get("in_flight", {}) if str(n).isdigit()}
    deferred_by_run_cap: list = []
    deferred_by_lane: list = []
    lane: dict = {}

    def capped(stage: str) -> bool:
        return stage == "product" and product_headroom is not None and product_headroom <= 0

    def lane_refusal(number: int) -> Optional[str]:
        """Why a fresh dev-lane unit cannot start now, else None; computes the lane once."""
        if not lane:
            try:
                lane.update(dev_lane_slots(gh, repo_path, epic, all_issues, runner=runner))
                if lane["occupied"] or lane["cross_epic"]:
                    runner(["git", "-C", repo_path, "fetch", "origin"])
                    lane["footprints"] = dev_lane_footprints(lane, repo_path, by_number,
                                                             runner=runner)
            except GhError as e:
                lane.update(cap=DEV_LANE_PARALLELISM, occupied=[], free=DEV_LANE_PARALLELISM,
                            excluded_other_epic=[], cross_epic=[], footprints=[],
                            warning=f"dev lane unverifiable ({e}); treating it as empty")
        return dev_lane_refusal(lane, number, lambda n: read_footprint(
            repo_path, n, runner=runner, epic=epic))

    def with_lane(result: dict) -> dict:
        """The lane arithmetic (`slots`) once consulted, and what it deferred (`dev_lane`)."""
        if lane:
            keys = ("cap", "occupied", "free", "excluded_other_epic", "warning")
            result["slots"] = {k: lane[k] for k in keys if k in lane}
        if deferred_by_lane:
            result["dev_lane"] = {
                "deferred": deferred_by_lane,
                "reason": "a fresh dev-lane unit waits on a slot or on a holder's footprint; "
                          "it starts once a tracked agent finishes -- survey again then."}
        return result

    def none_result() -> dict:
        if deferred_by_run_cap:
            # Never `none`: "run is full" must not read as "epic has nothing left".
            completed = run_completed(cap_state)
            return {"action": "stop-at-cap", "epic": epic, "cap": MAX_TASKS_PER_RUN,
                    "completed": completed, "in_flight": sorted(in_flight),
                    "reason": f"this run has driven {len(completed)} unit(s) to a terminal "
                              f"state, reaching the maxTasksPerRun cap of "
                              f"{MAX_TASKS_PER_RUN}. Deferred this pass: "
                              f"{', '.join(f'#{n}' for n in deferred_by_run_cap)}. Every "
                              f"unit's state is already persisted -- start a new run (a "
                              f"fresh --run-id) to carry on with a small context."}
        result = with_lane({"action": "none", "epic": epic})
        if unstaged:
            result["unstaged"] = unstaged
            result["unstaged_reason"] = (
                "Stage-less child(ren) of an architected Epic -- filed after the LLD "
                "phase, so no stage can be guessed. Route each by hand: `set-stage "
                "development` when the Epic's lld.md already covers it, or cut an "
                "Architecture revision phase-Task when it doesn't fit the design.")
        if deferred_by_cap:
            result["product_cap"] = {"limit": PRODUCT_WIP_CAP,
                                     "pending": product_gate_pending(all_issues),
                                     "deferred": deferred_by_cap}
        elif is_initiative(epic_issue) and epic_issue["state"] == "CLOSED":
            result["reason"] = f"Initiative #{epic} is closed -- nothing left to do."
        elif is_initiative(epic_issue):
            # "No open children" is ambiguous; inspect every child, open and closed.
            initiative_children = [i for i in all_issues if i.get("parent")
                                   and i["parent"]["number"] == epic]
            cut_epics = [i for i in initiative_children if is_epic(i)]
            roadmap_tasks = [i for i in initiative_children if not is_epic(i)]
            if cut_epics and all(i["state"] == "CLOSED" for i in cut_epics):
                result["reason"] = ("every cut Epic is closed -- ready for initiative-close "
                                    "validation (SKILL.md, \"Closing an Initiative\").")
            elif cut_epics:
                result["reason"] = _open_epics_reason(gh, cut_epics, skip_epics)
            elif roadmap_tasks and all(i["state"] == "CLOSED" for i in roadmap_tasks):
                result["reason"] = ("Product-Roadmap Task closed -- cut Epics from the "
                                    "approved product.md next (SKILL.md, \"Cutting Epics "
                                    "from an approved Initiative\").")
            elif not initiative_children:
                result["reason"] = ("no Product-Roadmap Task yet -- cut it next (SKILL.md, "
                                    "\"Cutting an Initiative's Product-Roadmap Task\").")
        return result

    # A human may merge a design PR before its gate opens; that is a finished stage, not a
    # dead session to resume.
    if is_epic(epic_issue) and not is_epic_standing(epic_issue):
        for issue in sorted(children, key=sort_key):
            merged = _merged_design_action(gh, issue, epic)
            if merged:
                return merged

    # Crash-recovery: the unit's own open children only.
    in_progress = [i for i in children if pipeline_status(i) == "in-progress"]
    if in_progress:
        target = in_progress[0]
        return {"action": "resume", "issue": target["number"], "unit": "issue",
                "stage": current_stage(target) or default_stage(epic_issue)}

    # Before the Epic is architected a Stage-less child is a Task awaiting
    # `merge-lld-doc`, which only advances Stage-less Tasks -- so leave it untouched.
    design_pending = (is_epic(epic_issue) and not is_epic_standing(epic_issue)
                      and not is_epic_architected(epic_issue))

    for issue in sorted(children, key=sort_key):
        status = pipeline_status(issue)
        if status == "needs-human":
            continue
        if design_pending and current_stage(issue) is None:
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
        stage = current_stage(issue) or default_stage(epic_issue)
        pickup = stage is None and is_epic_standing(epic_issue)
        if stage is None and not pickup:
            unstaged.append(issue["number"])
            continue
        if capped(stage):
            # Before blockedBy and the Stage write: a deferred unit gets no side effects.
            deferred_by_cap.append(issue["number"])
            continue
        if at_cap and issue["number"] not in in_flight:
            deferred_by_run_cap.append(issue["number"])
            continue
        if gh.blocked_by(issue["number"]):
            continue
        if pickup:
            # The orchestrator picks a standing child's first stage from the issue (`route`).
            return with_lane({"action": "route", "issue": issue["number"], "unit": "issue"})
        if stage in DEV_LANE_START_STAGES:
            refusal = lane_refusal(issue["number"])
            if refusal:
                deferred_by_lane.append({"issue": issue["number"], "reason": refusal})
                continue
        if current_stage(issue) is None:
            # Stamp the Stage on first sight so the board never shows it blank.
            gh.set_stage_field(issue["number"], stage)
        return with_lane({"action": "delegate", "issue": issue["number"], "unit": "issue",
                          "stage": stage})

    if is_initiative(epic_issue) and epic_issue["state"] == "OPEN" and not deferred_by_run_cap:
        step = _initiative_epic_step(gh, all_issues, epic, skip_epics)
        if step and at_cap:
            deferred_by_run_cap.append(step["epic"])
        elif step:
            return step
    # A bare Epic run (no Initiative loop above it) still needs its phase-Tasks cut first.
    if (is_epic(epic_issue) and epic_issue["state"] == "OPEN" and not is_epic_standing(epic_issue)
            and not is_epic_architected(epic_issue) and not deferred_by_run_cap
            and epic_lacks_phase_tasks(all_issues, epic)):
        if at_cap:
            deferred_by_run_cap.append(epic)
        else:
            return {"action": "cut-phase-tasks", "epic": epic, "unit": "epic",
                    "reason": f"Epic #{epic} has no phase-Tasks: run `cut-phase-tasks {epic} "
                              f"--repo-path <p>`, then `next-action {epic}` again"}
    return none_result()


def _resume_liveness(gh: GitHub, epic: int, issue: int) -> dict:
    """Liveness hints for a `resume` action: `claimed_at` and `claim_age_seconds` from the unit's
    latest start comment, `claimed_by_run` when the run-state still records who handed it out, and
    `likely_live` when the claim is younger than `RESUME_LIVE_WINDOW_MINUTES` (another session may
    still be driving it). `claim_age_seconds: null` -- said honestly -- when no claim timestamp is
    derivable, never a guess. The `reason` tells the orchestrator what to do about it."""
    claimed_at = None
    try:
        comments = gh.issue_view(issue).get("comments", [])
    except GhError:
        comments = []
    for c in comments:  # latest start comment wins
        if "Picking this up" in c.get("body", "") and c.get("createdAt"):
            claimed_at = c["createdAt"]
    info = {"claimed_at": claimed_at, "claim_age_seconds": None, "likely_live": False}
    state = read_run_state(epic)
    if state and str(issue) in (state.get("in_flight") or {}):
        info["claimed_by_run"] = state.get("run_id")
    age = None
    if claimed_at:
        try:
            age = (datetime.now(timezone.utc) - _parse_marker_ts(claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = None
    window = RESUME_LIVE_WINDOW_MINUTES * 60
    if age is not None:
        info["claim_age_seconds"] = age
        info["likely_live"] = window > 0 and age < window
    if age is None:
        info["reason"] = ("no claim timestamp is derivable (claim_age_seconds: null) -- cannot "
                          "tell whether another session is driving this unit; if unsure, ask the "
                          "operator before resuming.")
    elif info["likely_live"]:
        info["reason"] = (f"claimed {int(age // 60)} min ago, inside the "
                          f"{RESUME_LIVE_WINDOW_MINUTES}-min live window -- another session may "
                          f"still be driving it. If this session is not the one driving it, skip "
                          f"it and ask the operator before taking it over.")
    else:
        info["reason"] = (f"claimed {int(age // 60)} min ago, past the "
                          f"{RESUME_LIVE_WINDOW_MINUTES}-min live window -- likely a crashed or "
                          f"abandoned run; safe to resume.")
    return info


def cmd_next_action(gh: GitHub, args) -> dict:
    """`next-action`: `decide_next_action` plus `cap_enforced` (true only with a
    `--run-id` and a nonzero cap), which describes the invocation, not the decision.
    A `resume` also carries liveness hints (`_resume_liveness`)."""
    run_id = getattr(args, "run_id", None)
    result = decide_next_action(gh, args.epic, run_id=run_id,
                                skip_epics=getattr(args, "skip_epic", None),
                                repo_path=getattr(args, "repo_path", None) or ".")
    # Read liveness before note_in_flight re-stamps this run onto the unit.
    liveness = (_resume_liveness(gh, args.epic, result["issue"])
                if result.get("action") == "resume" else {})
    if "issue" in result:
        note_in_flight(args.epic, run_id,
                       {result["issue"]: result.get("stage") or result["action"]})
    return {**result, **liveness, "cap_enforced": bool(run_id) and MAX_TASKS_PER_RUN > 0}


def cmd_list_ready_for_review(gh: GitHub, epic: int, limit: Optional[int] = None) -> dict:
    """Up to `limit` children of `epic` with an open draft PR and no review outcome
    since the latest development->pr-review handoff (so each rework round re-qualifies).
    Read-only. Returns `ready_for_review`, `count`, `eligible_total`, `skipped`."""
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
        if routed_past_pr_review(comments):
            skipped.append({"issue": number, "reason": "routed past pr-review this round "
                                                        "(`route --to merge`)"})
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


# Exactly `Footprint` (optionally numbered, `## 17. Footprint`, or with a colon), so a
# prose heading that merely starts with the word is never taken for the section.
_FOOTPRINT_HEADING = re.compile(r"^#+\s*(?:\d+[.)]\s*)?Footprint\s*:?\s*$",
                                re.IGNORECASE | re.MULTILINE)
_FOOTPRINT_BULLET_PATH = re.compile(r"^-\s+`([^`]+)`")
# The `**Verify-only:**` sub-label ends the owned-path list: what follows is read, not owned.
_FOOTPRINT_VERIFY_ONLY = re.compile(r"^\*\*\s*(?:verify|read)[ -]?only\b", re.IGNORECASE)
_FENCED_BLOCK = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)
# A Task heading is `## Task #<n>` or, before issues exist, `## Task <slug-key>: ...`
# (`###` and a missing `#` also parse). Keys must be slugs so prose headings like
# `## Task Breakdown` are never read as Tasks.
_TASK_HEADING_LINE = re.compile(r"^(#{2,3})\s*Task\b\s*(\S+)(.*)$",
                                re.IGNORECASE | re.MULTILINE)
_TASK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TASK_KEY_MARKER = re.compile(r"<!--\s*task-key:\s*([a-z0-9][a-z0-9-]*)\s*-->", re.IGNORECASE)


def parse_task_headings(doc_text: str) -> list:
    """Every Task heading (numbered or keyed) in an Epic `lld.md`, in order, as dicts
    with `start`/`end`, `line_start`/`line_end`, `hashes`, `number`, `key`,
    `task_key`, `title`. Both spellings are scanned so section ends are always right."""
    entries = []
    for m in _TASK_HEADING_LINE.finditer(doc_text):
        ident, rest = m.group(2).rstrip(":"), m.group(3)
        number = key = None
        if ident.lstrip("#").isdigit():
            number = int(ident.lstrip("#"))
        elif _TASK_KEY_RE.match(ident):
            key = ident
        else:
            continue  # `## Task Breakdown` and friends: prose, not a Task
        marker = _TASK_KEY_MARKER.search(rest)
        entries.append({"number": number, "key": key,
                        "task_key": marker.group(1) if marker else None,
                        "title": _TASK_KEY_MARKER.sub("", rest).strip().lstrip(":").strip(),
                        "hashes": m.group(1),
                        "line_start": m.start(), "line_end": m.end()})
    for i, entry in enumerate(entries):
        entry["start"] = entry["line_start"]
        entry["end"] = entries[i + 1]["line_start"] if i + 1 < len(entries) else len(doc_text)
    return entries


def number_task_headings(doc_text: str, key_to_number: dict) -> str:
    """`doc_text` with each keyed Task heading in `key_to_number` rewritten to
    `## Task #<n>: <title> <!-- task-key: <key> -->`, as `create-lld-tasks` pushes it."""
    out = doc_text
    for h in parse_task_headings(doc_text):
        if h["key"] in key_to_number:
            out = out.replace(doc_text[h["line_start"]:h["line_end"]],
                              f"{h['hashes']} Task #{key_to_number[h['key']]}: "
                              f"{h['title'] or h['key']} <!-- task-key: {h['key']} -->", 1)
    return out


def is_numbered_copy(src: str, dest: str) -> bool:
    """Whether `dest` is exactly `src` after `create-lld-tasks` numbered its Task headings."""
    numbers = {h["task_key"]: h["number"] for h in parse_task_headings(dest)
               if h["number"] and h["task_key"]}
    return bool(numbers) and number_task_headings(src, numbers) == dest


def _task_heading_matches(entry: dict, task) -> bool:
    """Whether a parsed heading is `task`: a number matches `#<n>`; a slug matches
    the heading's key or its preserved `task-key` marker (so it survives renumbering)."""
    ident = str(task)
    if ident.isdigit():
        return entry["number"] == int(ident)
    return ident in (entry["key"], entry["task_key"])


def find_footprint(doc_text: str) -> Optional[list]:
    """Owned paths (backticked bullets) under the first real `## Footprint` heading, up to
    the next heading; None when the doc has no such heading. Lead-in lines before the
    bullets are skipped; a `**Verify-only:**` sub-label ends the owned list."""
    m = _FOOTPRINT_HEADING.search(_FENCED_BLOCK.sub("", doc_text))
    if not m:
        return None
    rest = m.string[m.end():]
    end = re.search(r"^#+\s", rest, re.MULTILINE)
    body = rest[:end.start()] if end else rest
    paths = []
    for line in body.splitlines():
        stripped = line.strip()
        if _FOOTPRINT_VERIFY_ONLY.match(stripped):
            break
        bm = _FOOTPRINT_BULLET_PATH.match(stripped)
        if bm:
            paths.append(bm.group(1))
    return paths


def parse_footprint(doc_text: str) -> list:
    """`find_footprint` with a missing section flattened to `[]` -- callers that need to
    tell "no section" from "owns nothing" use `find_footprint`."""
    return find_footprint(doc_text) or []


def find_task_footprint(doc_text: str, task_number) -> Optional[list]:
    """`find_footprint` scoped to one Task's subsection of an Epic `lld.md`
    (`task_number` may be an issue number or task key); None when the subsection or
    its `## Footprint` heading is missing."""
    for entry in parse_task_headings(doc_text):
        if _task_heading_matches(entry, task_number):
            return find_footprint(doc_text[entry["line_end"]:entry["end"]])
    return None


def parse_task_footprint(doc_text: str, task_number) -> list:
    """`find_task_footprint` with a missing section flattened to `[]`."""
    return find_task_footprint(doc_text, task_number) or []


def slice_task_subsection(doc_text: str, task_number: int) -> Optional[str]:
    """One Task's subsection of an Epic `lld.md` (heading up to the next Task
    heading), by issue number or task key; None when not found."""
    for entry in parse_task_headings(doc_text):
        if _task_heading_matches(entry, task_number):
            return doc_text[entry["start"]:entry["end"]].rstrip() + "\n"
    return None


def cmd_lld_section(repo_path: str, epic: int, task,
                     runner: Runner = _default_runner) -> dict:
    """Print one Task's subsection of `origin/epic-<n>:.../lld.md` as raw markdown,
    then return `{ok, epic, task, chars}` as the JSON footer. `task` is an issue
    number or task key."""
    try:
        text = runner(["git", "-C", repo_path, "show",
                        f"origin/{epic_branch(epic)}:{DOC_ROOT}/epic-{epic}/lld.md"])
    except GhError:
        raise GhError(f"cannot read {DOC_ROOT}/epic-{epic}/lld.md on "
                       f"origin/{epic_branch(epic)} -- is the epic's lld merged?")
    section = slice_task_subsection(text, task)
    if section is None:
        label = f"#{task}" if str(task).isdigit() else f"{task}"
        raise GhError(f"no `## Task {label}` subsection in {DOC_ROOT}/epic-{epic}/lld.md "
                       f"(headings must read `## Task {label}: ...`, per agents/lld.md)")
    print(section, end="")
    return {"ok": True, "epic": epic, "task": task, "chars": len(section)}


def read_footprint(repo_path: str, issue: int, runner: Runner = _default_runner,
                    epic: Optional[int] = None) -> Optional[list]:
    """An issue's owned footprint from `origin/issue-<n>`'s architecture.md, else (with
    `epic`) from its Task subsection of the Epic's lld.md. None when no `## Footprint`
    section is readable anywhere; `[]` for a section that owns no path (verify-only).
    Reads origin refs via `git show`, so no worktree is needed."""
    try:
        text = runner(["git", "-C", repo_path, "show",
                        f"origin/{issue_branch(issue)}:{DOC_ROOT}/issue-{issue}/architecture.md"])
    except GhError:
        text = None
    if text is not None:
        footprint = find_footprint(text)
        if footprint is not None:
            return footprint
    if epic is not None:
        try:
            text = runner(["git", "-C", repo_path, "show",
                            f"origin/{epic_branch(epic)}:{DOC_ROOT}/epic-{epic}/lld.md"])
        except GhError:
            return None
        return find_task_footprint(text, issue)
    return None


def _footprint_prefix(path: str) -> str:
    """Strip a trailing `*`/`**` glob; footprints are exact paths or directory
    globs, so prefix containment is enough."""
    return path.rstrip("*").rstrip("/")


def footprint_overlaps(a: list, b: list) -> bool:
    """Whether any path in `a` equals or contains/is contained by one in `b`."""
    a_prefixes = [_footprint_prefix(p) for p in a]
    b_prefixes = [_footprint_prefix(p) for p in b]
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/")
               for x in a_prefixes for y in b_prefixes)


# The rule a `development` PR must obey on its diff, quoted verbatim in every refusal/report.
DEV_PR_SCOPE_RULE = ("development authors no design doc; record deviations in the PR description")


def dev_pr_scope_offenders(gh: WorkItemProvider, issue: int, changed_files: list,
                           repo_path: str = ".", runner: Runner = _default_runner) -> dict:
    """Paths a `development` branch/PR must not touch: any Epic design doc under
    `<docRoot>/epic-*/` (e.g. lld.md), and any file listed in ANOTHER open Task's `## Footprint`
    of the same Epic's lld.md that is not in this unit's OWN footprint. Returns
    `{"design_docs": [...], "foreign_footprint": [...]}` (both sorted, deduped); the
    foreign-footprint list is empty when the unit is not a child of a non-standing Epic or the
    Epic lld.md is unreadable (never guessed at)."""
    design_prefix = DOC_ROOT.rstrip("/") + "/epic-"
    design_docs = sorted({p for p in changed_files if p.startswith(design_prefix)})
    foreign: set = set()
    # Only the foreign-footprint arm needs the Epic lld.md; skip it (and its API calls) when the
    # diff has no non-design-doc paths to check.
    non_doc = [p for p in changed_files if not p.startswith(design_prefix)]
    integ = integration_base(gh, issue) if non_doc else "main"
    if integ != "main":
        epic_no = int(integ[len(EPIC_BRANCH_PREFIX):])
        try:
            lld = runner(["git", "-C", repo_path, "show",
                          f"origin/{epic_branch(epic_no)}:{DOC_ROOT}/epic-{epic_no}/lld.md"])
        except GhError:
            lld = None
        if lld:
            own = parse_task_footprint(lld, issue)
            open_children = {i["number"] for i in gh.issue_list()
                             if i["state"] == "OPEN"
                             and (i.get("parent") or {}).get("number") == epic_no
                             and i["number"] != issue}
            others: list = []
            for entry in parse_task_headings(lld):
                if entry["number"] in open_children:
                    others.extend(parse_footprint(lld[entry["line_end"]:entry["end"]]))
            for p in changed_files:
                if p.startswith(design_prefix) or footprint_overlaps([p], own):
                    continue  # a design doc (counted above) or the unit's own footprint
                if others and footprint_overlaps([p], others):
                    foreign.add(p)
    return {"design_docs": design_docs, "foreign_footprint": sorted(foreign)}


def _dev_pr_scope_refusal_reason(offenders: dict) -> Optional[str]:
    """A one-string reason naming the offending paths, or None when the diff is clean."""
    parts = []
    if offenders["design_docs"]:
        parts.append(f"edits Epic design doc(s): {', '.join(offenders['design_docs'])}")
    if offenders["foreign_footprint"]:
        parts.append(f"touches another Task's Footprint file(s): "
                     f"{', '.join(offenders['foreign_footprint'])}")
    if not parts:
        return None
    return (f"this development diff {'; and '.join(parts)} -- {DEV_PR_SCOPE_RULE}. Revert those "
            f"paths and, if the design truly needs to change, escalate an Architecture/LLD "
            f"revision rather than editing the doc from a development branch.")


def worktree_path_for_branch(branch: str, runner: Runner = _default_runner,
                              base_repo: str = ".") -> Optional[str]:
    """Path of the live worktree with `branch` checked out, or None."""
    out = runner(["git", "-C", base_repo, "worktree", "list", "--porcelain"])
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def worktree_path(unit: str, number: int) -> str:
    """Configured worktree path for a unit (default `/tmp/sdlc-dev-<n>`, or
    `/tmp/sdlc-epic-<n>` for `unit="epic"`)."""
    w = PIPELINE["worktrees"]
    prefix = w["epicPrefix"] if unit == "epic" else w["devPrefix"]
    return os.path.join(w["root"], f"{prefix}{number}")


def plugin_version_info(runner: Runner = _default_runner) -> dict:
    """The running plugin's manifest `version` and, when its root is its own git
    checkout, that checkout's HEAD `sha` (else None)."""
    info = {"root": PLUGIN_ROOT, "version": None, "sha": None}
    try:
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json")) as f:
            info["version"] = json.load(f).get("version")
    except (OSError, ValueError):
        pass
    try:
        top, sha = runner(["git", "-C", PLUGIN_ROOT, "rev-parse",
                           "--show-toplevel", "HEAD"]).split()
    except (GhError, ValueError):
        return info
    # An installed copy nested inside some other repo must not report that repo's HEAD.
    if os.path.realpath(top) == os.path.realpath(PLUGIN_ROOT):
        info["sha"] = sha
    return info


def cmd_show_config(runner: Runner = _default_runner) -> dict:
    """Effective config (`pipeline` block over defaults) plus the running plugin version."""
    return {"repo": REPO, "docRoot": DOC_ROOT, "tokenPath": TOKEN_PATH, "tokenEnv": TOKEN_ENV,
            "requirementsDir": CONFIG.get("requirementsDir"),
            "parallelism": {**_PARALLELISM,
                            "devLane": DEV_LANE_PARALLELISM,
                            "prReview": PR_REVIEW_PARALLELISM,
                            "designLane": DESIGN_LANE_PARALLELISM,
                            "crossEpicFootprintCheck": CROSS_EPIC_FOOTPRINT_CHECK,
                            "maxTasksPerRun": MAX_TASKS_PER_RUN},
            "requiredWorkflows": CONFIG["requiredWorkflows"],
            "localCiSuites": list(LOCAL_CI_SUITES),
            "plugin": plugin_version_info(runner),
            **PIPELINE}


# Network failures a single immediate retry usually clears; anything else fails at once.
_TRANSIENT_NET_RE = re.compile(
    r"(timed out|timeout|connection (?:reset|refused|closed)|could not resolve host|"
    r"temporary failure|ssh_exchange_identification|kex_exchange_identification|"
    r"early eof|the remote end hung up|rpc failed)",
    re.IGNORECASE)


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
    Returns `created`, `path`, `branch`, `base`, `resumed` plus sync fields."""
    branch = unit_branch(unit, number)
    path = worktree_path(unit, number)
    existing = worktree_path_for_branch(branch, runner=runner, base_repo=repo_path)
    if existing:
        return _resume_live_worktree(existing, branch, repo_path, runner)
    _run_retry_transient(["git", "-C", repo_path, "fetch", "origin"], runner)
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
        if unit == "epic":
            # An epic branch is shared: every branch-writing command resolves it
            # from origin, so a local-only one is invisible to them.
            _run_retry_transient(["git", "-C", repo_path, "push", "origin",
                                  f"refs/heads/{branch}:refs/heads/{branch}"], runner)
        resumed = False
        synced = {}
    return {"created": True, "path": path, "branch": branch, "base": base,
            "resumed": resumed, **synced}


def _rev_count(repo_path: str, rev_range: str, runner: Runner) -> int:
    """`git rev-list --count <range>` as an int (0 when git prints nothing)."""
    return int(runner(["git", "-C", repo_path, "rev-list", "--count",
                       rev_range]).strip() or 0)


def _resume_live_worktree(path: str, branch: str, repo_path: str, runner: Runner) -> dict:
    """Return the live worktree holding `branch`, fast-forwarded to origin when
    strictly behind. A diverged branch is reported (`diverged`) and never reset.
    Adds `synced_to_origin` and `behind_before` (None when origin lacks the branch)."""
    runner(["git", "-C", repo_path, "fetch", "origin"])
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


def release_worktree(branch: str, runner: Runner = _default_runner,
                      base_repo: str = ".", shell: Optional[Callable] = None) -> dict:
    """Remove the worktree holding `branch` so a parked/finished unit frees its lane
    slot, running `pipeline.worktrees.releaseCommand` in it first (`release_command`).
    Returns `{released, path?, reason?}`; never raises and never destroys work
    (refuses on uncommitted/unpushed changes or the main worktree)."""
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
        release = _run_release_command(path, shell)
        # Safe: the refusals above guarantee a clean tree. Plain `remove` refuses
        # any tree containing submodules.
        runner(["git", "-C", base_repo, "worktree", "remove", "--force", path])
    except GhError as exc:
        return {"released": False, "path": path, "reason": str(exc)}
    return {"released": True, "path": path, **({"release_command": release} if release else {})}


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
    runner(["git", "-C", repo_path, "fetch", "origin"])
    if origin_branch_exists(repo_path, branch, runner=runner):
        return "exists"
    try:
        runner(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}"])
        source, outcome = f"refs/heads/{branch}", "pushed-local"
    except GhError:
        source, outcome = f"refs/remotes/origin/{base}", "created"
    runner(["git", "-C", repo_path, "push", "origin", f"{source}:refs/heads/{branch}"])
    runner(["git", "-C", repo_path, "fetch", "origin"])
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


# Stages a fresh unit enters the dev lane at; a unit past them (`pr-review`) has started.
DEV_LANE_START_STAGES = ("development", "testing")


def _dev_lane_parked(gh: GitHub, issue: dict) -> Optional[str]:
    """Why an open unit holds no dev-lane slot (parked or blocked), else None."""
    status = pipeline_status(issue)
    if status == "needs-human" or status in GATE_PENDING_STATUSES:
        return f"Pipeline Status is {status!r}"
    if gh.blocked_by(issue["number"]):
        return "blocked by an open dependency"
    return None


def dev_lane_slots(gh: GitHub, repo_path: str, epic: int, all_issues: list,
                   runner: Runner = _default_runner, cap: Optional[int] = None) -> dict:
    """The dev lane's slot arithmetic, one definition for `next-action` and
    `list-parallel-ready`. A slot is held by an open child of `epic` that has started -- a live
    worktree on its branch, Pipeline Status `in-progress`, or Stage `pr-review` -- and is not
    parked or blocked. Other epics' worktrees hold no slot: they are `excluded_other_epic`, or
    `cross_epic` (overlap check only) under `parallelism.crossEpicFootprintCheck`."""
    cap = DEV_LANE_PARALLELISM if cap is None else cap
    by_number = {i["number"]: i for i in all_issues}
    active = active_worktree_branches(repo_path, runner=runner)
    holders, stale, other = {}, [], []
    for branch in sorted(active):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        if (issue.get("parent") or {}).get("number") != epic:
            other.append(branch)
            continue
        parked = _dev_lane_parked(gh, issue)
        if parked:
            stale.append({"branch": branch, "reason": parked})
            continue
        holders[number] = {"issue": number, "branch": branch, "worktree": True}
    for issue in all_issues:
        number = issue["number"]
        if (number in holders or issue["state"] != "OPEN" or is_epic(issue)
                or (issue.get("parent") or {}).get("number") != epic):
            continue
        if pipeline_status(issue) != "in-progress" and current_stage(issue) != "pr-review":
            continue
        if _dev_lane_parked(gh, issue) is None:
            holders[number] = {"issue": number, "branch": issue_branch(number), "worktree": False}
    occupied = sorted(holders)
    return {"cap": cap, "occupied": occupied, "free": max(0, cap - len(occupied)),
            "holders": [holders[n] for n in occupied], "stale_worktrees": stale,
            "excluded_other_epic": [] if CROSS_EPIC_FOOTPRINT_CHECK else other,
            "cross_epic": other if CROSS_EPIC_FOOTPRINT_CHECK else [],
            "active_branches": active}


def dev_lane_footprints(lane: dict, repo_path: str, by_number: dict,
                        runner: Runner = _default_runner) -> list:
    """`(issue, footprint)` for every slot holder and cross-epic branch whose footprint is
    readable on origin; an unreadable one is left out (it still holds its slot)."""
    numbers = [h["issue"] for h in lane["holders"]]
    numbers += [issue_number_from_branch(b) for b in lane["cross_epic"]]
    out = []
    for number in numbers:
        # Footprints live in the unit's own epic's lld.md, so use its parent, not the lane's epic.
        parent = (by_number.get(number) or {}).get("parent")
        fp = read_footprint(repo_path, number, runner=runner,
                            epic=parent["number"] if parent else None)
        if fp:
            out.append((number, fp))
    return out


def footprint_collision(footprint: Optional[list], others: list) -> Optional[str]:
    """The refusal when `footprint` cannot be shown disjoint from `others` (`(issue, fp)`
    pairs): None (no readable section) means unverifiable, `[]` (verify-only) owns nothing,
    an overlap names the unit. None when clear."""
    if footprint is None:
        return "no readable ## Footprint -- cannot verify non-overlap"
    hit = next((n for n, fp in others if footprint_overlaps(footprint, fp)), None)
    return f"footprint overlaps active/eligible #{hit}" if hit is not None else None


def dev_lane_refusal(lane: dict, number: int, footprint_of) -> Optional[str]:
    """Why a fresh unit cannot enter the lane now, else None: a holder is already admitted;
    otherwise it needs a free slot and, when anything holds one, a footprint disjoint from
    the holders' (`footprint_of(number)` is read only then)."""
    if number in lane["occupied"]:
        return None
    if lane["free"] <= 0:
        return (f"dev lane full: {len(lane['occupied'])}/{lane['cap']} slot(s) held by "
                f"{', '.join(f'#{n}' for n in lane['occupied'])}")
    if not lane.get("footprints"):
        return None
    return footprint_collision(footprint_of(number), lane["footprints"])


def cmd_list_parallel_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                             runner: Runner = _default_runner,
                             run_id: Optional[str] = None) -> dict:
    """Children of `epic` at development/testing that can start now: not active, parked,
    blocked or footprint-overlapping another (a missing footprint is never assumed safe).
    Returns `parallel_ready` (up to free slots), `skipped`, `stale_worktrees` and counts."""
    limit = DEV_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (pipeline.classification does not call it "
                       f"\"epic\") -- pass the epic's own issue number, not a child issue's")
    cap_state = run_cap_state(epic, run_id)
    cap_enforced = bool(run_id) and MAX_TASKS_PER_RUN > 0
    base = {"parallel_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": [], "cap_enforced": cap_enforced}
    if run_cap_reached(cap_state):
        # The cap must close this lane too, or overflow just moves here from next-action.
        completed = run_completed(cap_state)
        in_flight = sorted(int(n) for n in cap_state.get("in_flight", {}) if str(n).isdigit())
        return {**base, "stop_at_cap": True, "cap": MAX_TASKS_PER_RUN, "completed": completed,
                "in_flight": in_flight,
                "note": f"run has driven {len(completed)} unit(s) to a terminal state, at "
                        f"the maxTasksPerRun cap of {MAX_TASKS_PER_RUN} -- no new work is "
                        f"handed out until a new run (a fresh --run-id) starts. Units "
                        f"already in flight finish through resume/pass-gate as normal."}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if resolve_profile(epic_issue)["childrenNeedArchitectedEpic"] \
            and not is_epic_architected(epic_issue):
        return {**base, "note": f"epic #{epic} is not epic:architected yet -- its children "
                                 f"are not eligible for the implementation lane"}
    open_issues = [i for i in all_issues if i["state"] == "OPEN"]
    children = [i for i in open_issues if not is_epic(i) and i.get("parent")
                and i["parent"]["number"] == epic]

    runner(["git", "-C", repo_path, "fetch", "origin"])
    lane = dev_lane_slots(gh, repo_path, epic, all_issues, runner=runner, cap=limit)
    active_branches = lane["active_branches"]

    eligible, skipped = [], []
    selected_footprints = dev_lane_footprints(lane, repo_path, by_number, runner=runner)
    for issue in sorted(children, key=sort_key):
        number = issue["number"]
        branch = issue_branch(number)
        if branch in active_branches:
            skipped.append({"issue": number, "reason": "already active in its own worktree"})
            continue
        stage = current_stage(issue)
        if stage not in ("development", "testing"):
            skipped.append({"issue": number, "reason": f"stage is {stage!r}, not development/testing"})
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
        footprint = read_footprint(repo_path, number, runner=runner, epic=epic)
        if footprint is None:
            skipped.append({"issue": number, "reason": "no ## Footprint section found in its own "
                                                         "architecture.md, or under a "
                                                         f"`## Task #{number}` heading in its Epic's "
                                                         "epic-<n>/lld.md, on origin -- "
                                                         "cannot verify non-overlap"})
            continue
        collision = footprint_collision(footprint, selected_footprints)
        if collision is not None:
            skipped.append({"issue": number, "reason": collision})
            continue
        eligible.append({"issue": number, "branch": branch, "stage": stage, "title": issue["title"]})
        selected_footprints.append((number, footprint))

    # A holder keeps its slot even when its footprint was unreadable.
    active_count = len(lane["occupied"])
    slots = lane["free"]
    selected = eligible[:slots]
    note_in_flight(epic, run_id, {u["issue"]: u["stage"] for u in selected})
    return {"parallel_ready": selected, "count": len(selected), "eligible_total": len(eligible),
            "active_count": active_count,
            "active_branches": sorted(h["branch"] for h in lane["holders"]),
            "limit": limit, "slots_available": slots,
            "slots": {"cap": limit, "occupied": lane["occupied"], "free": slots,
                      "excluded_other_epic": lane["excluded_other_epic"]},
            "epic": epic, "skipped": skipped, "stale_worktrees": lane["stale_worktrees"],
            "cap_enforced": cap_enforced, "stop_at_cap": False}


def cmd_list_design_ready(gh: GitHub, repo_path: str, epic: int, limit: Optional[int] = None,
                           runner: Runner = _default_runner) -> dict:
    """Children of a standing `epic` at `product`/`architecture` that can start now,
    each in its own worktree, up to the design-lane cap. Read-only apart from a fetch.
    Returns `design_ready`, `skipped`, `stale_worktrees`, `product_cap` and slot counts."""
    limit = DESIGN_LANE_PARALLELISM if limit is None else limit
    all_issues = gh.issue_list()
    by_number = {i["number"]: i for i in all_issues}
    epic_issue = by_number.get(epic)
    if epic_issue is None or not is_epic(epic_issue):
        raise GhError(f"#{epic} is not an epic (pipeline.classification does not call it "
                       f"\"epic\") -- pass the epic's own issue number, not a child issue's")
    # An auto-passing Gate A never queues on the human, so it is exempt from the WIP cap.
    product_headroom = (product_wip_headroom(all_issues)
                        if effective_gates(epic_issue)["requiresHumanGateA"] else None)
    product_cap = {"limit": PRODUCT_WIP_CAP, "pending": product_gate_pending(all_issues)}
    base = {"design_ready": [], "count": 0, "eligible_total": 0, "active_count": 0,
            "active_branches": [], "limit": limit, "slots_available": 0, "epic": epic,
            "skipped": [], "stale_worktrees": [], "product_cap": product_cap}
    if is_epic_legacy(epic_issue):
        return {**base, "note": f"epic #{epic} is epic:legacy -- not driven by this pipeline"}
    if not is_epic_standing(epic_issue):
        return {**base, "note": f"epic #{epic} is not a standing epic -- its design work is "
                                 f"its Architecture-phase and LLD-phase Tasks, driven one at a "
                                 f"time by next-action, not fanned out across children"}
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
        stage = current_stage(issue)
        if stage is None:
            skipped.append({"issue": number, "reason": "no Stage yet -- `route` picks its first stage"})
            continue
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

    # Only design-stage worktrees occupy a design slot; parked or closed ones are stale.
    occupied, stale = [], []
    for branch in sorted(active_branches):
        number = issue_number_from_branch(branch)
        if number is None:
            continue
        issue = by_number.get(number)
        if issue is None or issue["state"] != "OPEN":
            stale.append({"branch": branch, "reason": "issue is closed or not found"})
            continue
        stage = current_stage(issue)
        if stage not in ("product", "architecture"):
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
    """Post the `development->pr-review` handoff marker that queues the PR for review.
    Run after every development round, including rework. Returns `queued_for`."""
    timestamp = _utc_now_marker()
    gh.issue_comment(issue,
        f"✅ Development complete. {summary} "
        f"PR #{pr} is queued for `pr-review`.\n\n"
        f"<!-- stage-transition: development->pr-review @ {timestamp} -->")
    return {"issue": issue, "pr": pr, "queued_for": "pr-review"}


PR_REVIEW_OUTCOMES = ("clean", "rework")


def cmd_record_pr_review(gh: GitHub, issue: int, pr: int, outcome: str, summary: str,
                         same_class_recurrence: bool = False) -> dict:
    """Post the `pr-review-outcome` marker (`clean` | `rework`, optional same-class flag)
    that `list-ready-for-review` and `pairing-counts` read. Returns `recorded`."""
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    if same_class_recurrence and outcome != "rework":
        raise GhError("same_class_recurrence only makes sense on outcome='rework' "
                       "-- a clean verdict has no defect class to recur")
    timestamp = _utc_now_marker()
    headline = ("🔍 PR review complete — no findings; proceeding to merge."
                if outcome == "clean" else
                "🔁 PR review complete — findings sent back to `development` for rework.")
    if same_class_recurrence:
        headline += " **Same defect class as an earlier round — escalation candidate.**"
    same_class_field = " same-class:true" if same_class_recurrence else ""
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- pr-review-outcome: {outcome}:{pr}"
                             f"{same_class_field} @ {timestamp} -->")
    return {"issue": issue, "pr": pr, "outcome": outcome,
            "same_class_recurrence": same_class_recurrence, "recorded": True}


# Tail of a suite run embedded in an attestation; bounded by GitHub's comment body limit.
LOCAL_CI_EVIDENCE_LINES = 40
LOCAL_CI_EVIDENCE_CHARS = 4000


def read_ci_evidence(output_path: str) -> str:
    """The trimmed tail of a suite run's captured output file for `record-local-ci`.
    Raises on an unreadable or empty file: the attestation must carry real output."""
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
    """Post a `local-ci` attestation on the PR: `suite` passed at `sha`, with the command
    and its captured output tail. The merge gate honours it only while `sha` matches
    the PR head. Returns `attested`, `evidence_lines`."""
    if suite not in LOCAL_CI_SUITES:
        raise GhError(f"suite must be one of {LOCAL_CI_SUITES}, got {suite!r}")
    if suite in NON_ATTESTABLE_SUITES:
        raise GhError(f"the {suite!r} suite is configured `attestable: false` -- a local-ci "
                      f"attestation cannot stand in for it; only a passing GitHub Actions check "
                      f"satisfies its required workflow")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
        raise GhError(f"sha must be a 7-40 char hex commit id, got {sha!r}")
    if not (command or "").strip():
        raise GhError("--command is required: the exact command the suite was run with")
    placeholder = _UNEXPANDED_PLACEHOLDER.search(command)
    if placeholder:
        raise GhError(f"--command contains an unexpanded placeholder {placeholder.group(0)!r} -- "
                      f"attest the real command you ran, not a template copied from the docs "
                      f"(substitute every `<...>` token first)")
    pattern = LOCAL_CI_COMMAND_PATTERNS.get(suite)
    if pattern and not re.search(pattern, command):
        raise GhError(f"--command {command.strip()!r} does not match the {suite!r} suite's "
                      f"requiredWorkflows[].commandPattern {pattern!r} -- attest the real "
                      f"suite command")
    evidence = read_ci_evidence(output)
    timestamp = _utc_now_marker()
    fence = "```"
    gh.pr_comment(pr, f"🧪 Local CI attested — `{suite}` suite passed locally against "
                       f"`{sha}` (stands in for its GHA check on the merge gate).\n\n"
                       f"Command: `{command.strip()}`\n\n"
                       f"<details><summary>captured output (tail)</summary>\n\n"
                       f"{fence}\n{evidence}\n{fence}\n\n</details>\n\n"
                       f"<!-- local-ci: {suite}:{pr} @ {sha} -->\n"
                       f"<!-- attested-at: {timestamp} -->")
    return {"pr": pr, "suite": suite, "sha": sha, "command": command.strip(),
            "evidence_lines": len(evidence.splitlines()), "attested": True}


def _stage_and_status(gh: WorkItemProvider, issue: int) -> tuple:
    """`(Stage slug, Pipeline Status slug)` of one issue, read from `issue_fields`:
    `issue_view` carries no field values."""
    fields = gh.issue_fields(issue)
    return (STAGE_FIELD_NAMES.get(fields.get("Stage")),
            PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status")))


# Authoring stages that hand off with a free-text comment; `post-comment` is their only path.
POST_COMMENT_ROLES = ("product", "architecture", "lld")


def cmd_post_comment(gh: WorkItemProvider, issue: int, role: str, body_file: str) -> dict:
    """Post an authoring stage's handoff comment, tagged with a `role-comment` marker that
    `subagent_stop` looks for. Refused unless `issue` is at `role` and claimed (in-progress, or
    its gate open while the stage revises on feedback)."""
    if role not in POST_COMMENT_ROLES:
        raise GhError(f"role must be one of {POST_COMMENT_ROLES}, got {role!r}")
    try:
        with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
            body = fh.read().strip()
    except OSError as exc:
        raise GhError(f"--body-file must be a readable file holding the comment text: {exc}")
    if not body:
        raise GhError("--body-file is empty -- a handoff comment must say something")
    if len(body) > HANDOFF_CAP:
        return {"refused": True,
                "reason": f"the comment is {len(body):,} chars, over the {HANDOFF_CAP:,}-char "
                          f"handoff cap (references/stage-playbooks.md, \"Comment size is a "
                          f"contract\"); trim it and re-run"}
    stage, status = _stage_and_status(gh, issue)
    if stage != role or status not in ("in-progress", *GATE_PENDING_STATUSES):
        raise GhError(f"#{issue} is at Stage {stage!r} / status {status!r}, not claimed at "
                      f"{role!r} -- post-comment only serves the stage that holds the claim")
    gh.issue_comment(issue, f"{body}\n\n<!-- role-comment: {role} @ {_utc_now_marker()} -->")
    return {"issue": issue, "role": role, "posted": True}


DESIGN_REVIEW_ROLES = ("product-review", "arch-review", "lld-review")


def cmd_record_design_review(gh: GitHub, issue: int, role: str, outcome: str,
                             summary: str, same_class_recurrence: bool = False) -> dict:
    """Post the `design-review-outcome` marker for a design review role (`clean` |
    `rework`, optional same-class flag) that `pairing-counts` reads, and -- for an
    arch/lld-review of a phase-Task -- the same outcome as a comment on its design PR.
    Returns `recorded` (+ `design_pr`)."""
    if role not in DESIGN_REVIEW_ROLES:
        raise GhError(f"role must be one of {DESIGN_REVIEW_ROLES}, got {role!r}")
    if outcome not in PR_REVIEW_OUTCOMES:
        raise GhError(f"outcome must be one of {PR_REVIEW_OUTCOMES}, got {outcome!r}")
    if same_class_recurrence and outcome != "rework":
        raise GhError("same_class_recurrence only makes sense on outcome='rework' "
                       "-- a clean verdict has no defect class to recur")
    timestamp = _utc_now_marker()
    headline = (f"🔍 `{role}` complete — no findings."
                if outcome == "clean" else
                f"🔁 `{role}` complete — findings sent back for rework.")
    if same_class_recurrence:
        headline += " **Same defect class as an earlier round — escalation candidate.**"
    same_class_field = " same-class:true" if same_class_recurrence else ""
    # The review lives on the design PR too; posted first so a failure here leaves no evidence
    # marker behind and a re-run is safe. The marker names the PR head it covered.
    design_pr, sha_field = None, ""
    if role in DESIGN_STAGE_REVIEW.values():
        found = find_design_pr(gh.issue_view(issue).get("comments", []))
        if found and DESIGN_STAGE_REVIEW[found[0]] == role:
            design_pr = found[1]
            gh.pr_comment(design_pr, f"{headline} {summary}")
            head = gh.pr_view(design_pr, fields="headRefOid").get("headRefOid")
            sha_field = f" sha:{head}" if head else ""
    gh.issue_comment(issue, f"{headline} {summary}\n\n"
                             f"<!-- design-review-outcome: {outcome}:{role}"
                             f"{same_class_field}{sha_field} @ {timestamp} -->")
    return {"issue": issue, "unit": "issue", "role": role,
            "outcome": outcome, "same_class_recurrence": same_class_recurrence,
            "recorded": True, **({"design_pr": design_pr} if design_pr else {}),
            **({"reviewed_sha": sha_field.split(":")[1]} if sha_field else {})}


def _post_start_comment(gh: GitHub, issue: int, role: str):
    gh.issue_comment(issue, f"🚧 Picking this up — {role} stage starting.")


def _check_claimable(gh: GitHub, issue: int, role: str) -> dict:
    """Raise unless `role` is claimable on `issue`: a known role and, for `development`,
    no open PR (re-claiming would move Stage off PR Review and drop it from review)."""
    if role not in STAGE_OPTION_IDS:
        raise GhError(f"unknown role {role!r} -- must be one of "
                       f"{sorted(STAGE_OPTION_IDS)}")
    if role == "development":
        open_prs = gh.pr_list_for_branch(issue_branch(issue))
        if open_prs:
            raise GhError(f"issue #{issue} already has open PR #{open_prs[0]['number']} -- "
                          f"resume its development agent for the rework round instead of "
                          f"re-claiming (references/rework.md)")
    return {"issue": issue, "role": role, "claimable": True}


def cmd_claim(gh: GitHub, issue: int, role: str) -> dict:
    role = RETIRED_ROLES.get(role, role)
    _check_claimable(gh, issue, role)
    gh.set_stage_field(issue, role)
    gh.set_pipeline_status_field(issue, "in-progress")
    _post_start_comment(gh, issue, role)
    return {"issue": issue, "claimed": True}


def cmd_set_stage(gh: GitHub, issue: int, stage: str) -> dict:
    """Set the Stage field only -- no Pipeline Status change, no start comment."""
    if stage not in STAGE_OPTION_IDS:
        raise GhError(f"unknown stage {stage!r} -- must be one of {sorted(STAGE_OPTION_IDS)}")
    gh.set_stage_field(issue, stage)
    return {"issue": issue, "stage": stage}


# A standing child's full flow in order; `merge` means "skip pr-review". Only an authoring
# stage (or `merge`) is a route target: a review always follows the stage it reviews.
STANDING_FLOW = ("product", "product-review", "architecture", "arch-review",
                 "development", "pr-review", "merge")
ROUTE_TARGETS = ("product", "architecture", "development", "merge")
ROUTE_REASON_CAP = 200


def _flow_index(stage: Optional[str]) -> Optional[int]:
    """Where a child at Stage `stage` sits in STANDING_FLOW: -1 before its first stage, and
    `development` for Stage `pr-review` (development's exit sets it); None outside the flow."""
    if stage is None:
        return -1
    owner = "development" if stage == "pr-review" else stage
    return STANDING_FLOW.index(owner) if owner in STANDING_FLOW else None


def route_refusal(parent: Optional[dict], stage: Optional[str], status: Optional[str],
                  to: str, reason: str, pr_review_bounced: bool = False) -> Optional[str]:
    """Why `route` must refuse moving a child of `parent` at `stage`/`status` to `to`, or None.
    `pr_review_bounced`: a `pr-review` already asked for rework on this issue."""
    if parent is None or not is_epic(parent) or not is_epic_standing(parent):
        return "route is for a standing epic's child only -- stage anything else with set-stage"
    if to not in ROUTE_TARGETS:
        return (f"--to must be one of {', '.join(ROUTE_TARGETS)}"
                + (" -- a review always follows its stage; route to the next authoring stage "
                   "to skip it" if to in STANDING_FLOW else ""))
    line = reason.strip()
    if not line or "\n" in line:
        return "--reason must be one non-empty line"
    if status in GATE_PENDING_STATUSES:
        return f"a gate PR is open (Pipeline Status {status!r}) -- the human decides; route after it passes"
    at = _flow_index(stage)
    if at is None:
        return f"Stage {stage!r} is not in the standing flow ({', '.join(STANDING_FLOW[:-1])})"
    if to == "merge" and stage != "pr-review":
        return "merge (skipping pr-review) needs development's PR open -- Stage must be PR Review"
    if to == "merge" and pr_review_bounced:
        return "pr-review already asked for rework on this issue -- never skip it after a bounce"
    if STANDING_FLOW.index(to) <= at:
        return f"route only moves forward from {stage!r} -- use set-stage to go back"
    return None


def cmd_route(gh: GitHub, issue: int, to: str, reason: str) -> dict:
    """Route a standing epic's child ahead to `to`, skipping the stages between: Stage `to`
    and Pipeline Status `todo` (no claim; `merge` leaves both), plus a `stage-route` marker.
    Refuses (`routed: False`, exit 0) per `route_refusal`."""
    parent = gh.issue_epic_info(issue).get("parent")
    parent_info = gh.issue_epic_info(parent["number"]) if parent else None
    fields = gh.issue_fields(issue)
    stage = STAGE_FIELD_NAMES.get(fields.get("Stage"))
    bounced = to == "merge" and any(
        m.group(1) == "rework" for c in gh.issue_view(issue).get("comments", [])
        for m in _PR_REVIEW_OUTCOME_MARKER.finditer(c.get("body", "")))
    refusal = route_refusal(parent_info, stage,
                            PIPELINE_STATUS_FIELD_NAMES.get(fields.get("Pipeline Status")),
                            to, reason, pr_review_bounced=bounced)
    if refusal:
        return {"issue": issue, "routed": False, "reason": refusal}
    frm = stage or "pickup"
    skipped = list(STANDING_FLOW[_flow_index(stage) + 1:STANDING_FLOW.index(to)])
    if to != "merge":
        gh.set_stage_field(issue, to)
        gh.set_pipeline_status_field(issue, "todo")
    skipping = f", skipping {', '.join(f'`{s}`' for s in skipped)}" if skipped else ""
    gh.issue_comment(issue, f"🔀 Routed `{frm}` → `{to}`{skipping}: {reason.strip()}\n\n"
                            f"<!-- stage-route: {frm}->{to} @ {_utc_now_marker()} -->")
    return {"issue": issue, "routed": True, "from": frm, "to": to, "skipped": skipped}


def cmd_start_comment(gh: GitHub, issue: int, role: str) -> dict:
    """Post the start comment with no field mutation, for roles with no Stage of their own.

    For `pr-review` -- which `transition` runs AFTER `sync-branch` has moved the head -- it first
    refuses (`ok: False`, no comment posted) when a required suite has neither a `local-ci`
    attestation nor a passing GHA check on the current PR head, so the review is never queued
    against evidence sync just invalidated; a suite whose sync only merged files outside its
    coverage is carried forward instead.

    For `arch-review` it surfaces the effective `skip_confidence_threshold` -- the profile's
    `skipConfidenceThreshold` the reviewer's confidence must exceed for `skip-gate` to skip Gate B
    -- so the reviewer knows the bar before writing its confidence marker."""
    if role == "pr-review":
        prs = gh.pr_list_for_branch(issue_branch(issue))
        carried: list = []
        by_check: list = []
        if prs:
            status = pr_stale_attestations(gh, issue, prs[0]["number"])
            if status["stale"]:
                stale = status["stale"]
                return {"issue": issue, "role": role, "started": False, "ok": False,
                        "refused": True,
                        "stale_suites": [s["suite"] for s in stale],
                        "held": stale,
                        "reason": (f"pr-review not queued -- "
                                   f"{'; '.join(s['reason'] for s in stale)}. A required suite "
                                   f"is satisfied by a `record-local-ci` attestation on the PR "
                                   f"head OR a passing check for its workflow on that head (a "
                                   f"sync that only merged files outside its coverage carries "
                                   f"the attestation forward). Wait for a pending check and "
                                   f"re-run this, or re-run the suite and `record-local-ci` on "
                                   f"the post-sync head.")}
            carried, by_check = status["carried"], status["passed_by_check"]
        _post_start_comment(gh, issue, role)
        result = {"issue": issue, "started": role}
        if carried:
            result["carried_attestation_forward"] = carried
        if by_check:
            result["satisfied_by_check"] = by_check
        return result
    _post_start_comment(gh, issue, role)
    result = {"issue": issue, "started": role}
    if role == "arch-review":
        result["skip_confidence_threshold"] = (
            _profile_for_issue(gh, issue)["gates"]["skipConfidenceThreshold"])
    return result


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


def design_doc_dir(epic: Optional[int], issue: int) -> str:
    """Where `issue`'s design docs live: `<docRoot>/epic-<e>` for a non-standing Epic's
    phase-Task (authored in place, merged into `epic-<e>`), else `<docRoot>/issue-<n>`."""
    return f"{DOC_ROOT}/epic-{epic}" if epic is not None else f"{DOC_ROOT}/issue-{issue}"


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
    base, doc = epic_branch(epic), f"{DOC_ROOT}/epic-{epic}/{stage}.md"
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


def _epic_lld_is_numbered_copy(repo_path: str, issue: int, epic: int, runner: Runner) -> bool:
    """Whether `origin/epic-<e>`'s lld.md is `issue-<n>`'s after `create-lld-tasks` numbered
    it: merging the branch's copy back would revert the numbering."""
    doc = f"{DOC_ROOT}/epic-{epic}/lld.md"
    try:
        runner(["git", "-C", repo_path, "fetch", "origin"])
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
    doc = f"{DOC_ROOT}/epic-{epic}/{stage}.md"
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
    if stage == "lld" and _epic_lld_is_numbered_copy(repo_path, issue, parent["number"], runner):
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
    """Open a human-review gate: a new `issue-<n>` -> `main` PR, or -- for a non-standing Epic's
    architecture phase-Task -- its already-open design PR into `epic-<e>` (no second PR)."""
    stage = doc.rsplit(".", 1)[0]
    epic = design_phase_epic(gh, issue, stage)
    head, base = issue_branch(issue), "main" if epic is None else epic_branch(epic)
    doc_path = f"{design_doc_dir(epic, issue)}/{doc}"
    # Cite the pushed head, not a local HEAD: the PR only contains what is on origin.
    repo_path = repo_path or "."
    runner(["git", "-C", repo_path, "fetch", "origin"])
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
    _run_retry_transient(["git", "-C", repo_path, "fetch", "origin"], runner)
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


def cmd_sync_branch(gh: GitHub, repo_path: Optional[str], issue: int, unit: str = "issue",
                     runner: Runner = _default_runner, base: Optional[str] = None) -> dict:
    """Merge the unit's integration base (or `base`) into its branch and push. A merge
    conflict (`conflict`) or missing base (`base_missing`) is a structured exit-0
    result with `synced: false`, not an error."""
    branch = unit_branch(unit, issue)
    if base:
        base = base[len("origin/"):] if base.startswith("origin/") else base
    else:
        base = "main" if unit == "epic" else integration_base(gh, issue, unit)
    result = {"issue": issue, "unit": unit, "branch": branch, "base": base, "synced": True}
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        # Fetch first so the existence check never reads a stale remote-tracking ref.
        runner(["git", "-C", ws.path, "fetch", "origin"])
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
                # Persisted as a marker so pairing-counts can rebuild it after a crash.
                timestamp = _utc_now_marker()
                gh.issue_comment(issue,
                    f"⚠️ Merge conflict reconciling `{branch}` with `origin/{base}` — "
                    f"{len(e.files)} file(s): {', '.join(f'`{f}`' for f in e.files)}. "
                    f"Routing to `development` for resolution in its own worktree.\n\n"
                    f"<!-- sync-conflict: {branch} @ {timestamp} -->")
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
    doc_path = f"{DOC_ROOT}/epic-{epic}/lld.md"
    with branch_lock(epic_br), BranchWorkspace(epic_br, repo_path, runner) as ws:
        runner(["git", "-C", ws.path, "fetch", "origin"])
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


def cmd_add_blocked_by(gh: GitHub, issue: int, dep: int) -> dict:
    """Add a native `blockedBy` edge with no other side effect (unlike `mark-blocked`,
    which also parks an in-flight unit)."""
    gh.add_blocked_by(issue, dep)
    return {"issue": issue, "blocked_on": dep, "added": True}


def _task_line(name: str) -> re.Pattern:
    """A `<name>: <value>` line in a Task section; bullet and bold markup tolerated."""
    return re.compile(rf"^\s*(?:[-*]\s+)?(?:\*\*)?{name}(?:\*\*)?\s*:\s*(.+?)\s*$",
                      re.IGNORECASE | re.MULTILINE)


_DEPENDS_ON_LINE = _task_line("Depends on")
_PRIORITY_LINE = _task_line("Priority")
_EFFORT_LINE = _task_line("Effort")
_REALISES_LINE = _task_line("Reali[sz]es")
_ISSUE_REF_RE = re.compile(r"#(\d+)")
# `<!-- realises: #574 #577 -->` in a carved Task's body: the issues its PR delivers.
_REALISES_MARKER = re.compile(r"<!--\s*realises:\s*([^>]*?)\s*-->")


def parse_task_realises(section_text: str) -> list:
    """Issue numbers from a section's `Realises:` line(s) (`#<n>` references only,
    in order, deduplicated); [] when none."""
    numbers = []
    for m in _REALISES_LINE.finditer(section_text):
        for ref in _ISSUE_REF_RE.findall(m.group(1)):
            if int(ref) not in numbers:
                numbers.append(int(ref))
    return numbers


def realised_issues(body: str) -> list:
    """Issue numbers a carved Task's body marks as realised (its `realises` marker)."""
    m = _REALISES_MARKER.search(body or "")
    return [int(n) for n in _ISSUE_REF_RE.findall(m.group(1))] if m else []


def task_section_defect(section_text: str) -> Optional[str]:
    """Why a `## Task <KEY>` section is not a Task to carve (None when it is): a Task
    section carries a `## Footprint` heading; prose under a Task heading does not."""
    if find_footprint(section_text) is None:
        return ("no `## Footprint` heading -- every Task section carries one "
                "(references/epics.md, \"How to size the Tasks\"); prose such as a carving "
                "summary must not sit under a `## Task` heading")
    return None


def parse_task_field(section_text: str, line: re.Pattern) -> Optional[str]:
    """The value of the section's first `line` match (markup stripped), or None."""
    m = line.search(section_text)
    return m.group(1).strip("`*. ") if m else None


def parse_task_depends_on(section_text: str) -> list:
    """Task keys from a section's `Depends on:` line(s) (comma- or `and`-separated,
    backticks and trailing period tolerated); [] when none."""
    keys = []
    for m in _DEPENDS_ON_LINE.finditer(section_text):
        for raw in re.split(r",|\band\b", m.group(1)):
            key = raw.strip().strip("`").strip().rstrip(".")
            if _TASK_KEY_RE.match(key) and key not in keys:
                keys.append(key)
    return keys


def _commit_doc_to_epic_branch(repo_path: Optional[str], epic: str, doc_path: str,
                                text: str, runner: Runner, message: str) -> dict:
    """Commit `text` at `doc_path` on branch `epic` and push. Refuses a dirty tree or
    unpushed commits touching other files; a rejected push returns `conflict: True`.
    Returns `committed`, `pushed` (+ `reason`)."""
    with branch_lock(epic):
        ensure_branch_on_origin(repo_path or ".", epic, runner=runner)
        with BranchWorkspace(epic, repo_path, runner) as ws:
            runner(["git", "-C", ws.path, "fetch", "origin"])
            unpushed = runner(["git", "-C", ws.path, "diff", "--name-only",
                               f"origin/{epic}...{epic}"]).split()
            if runner(["git", "-C", ws.path, "status", "--porcelain"]).strip():
                out = {"committed": None, "pushed": False,
                       "reason": f"epic worktree {ws.path} has uncommitted changes -- "
                                 f"refusing to reset it; commit or stash them, then re-run"}
            elif any(f != doc_path for f in unpushed):
                out = {"committed": None, "pushed": False,
                       "reason": f"local {epic} carries unpushed commits touching "
                                 f"{', '.join(f for f in unpushed if f != doc_path)} -- "
                                 f"refusing to reset it; push or discard them, then re-run"}
            else:
                runner(["git", "-C", ws.path, "checkout", "-B", epic, f"origin/{epic}"])
                target = os.path.join(ws.path, doc_path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w") as f:
                    f.write(text)
                runner(["git", "-C", ws.path, "add", "--", doc_path])
                runner(["git", "-C", ws.path, "commit", "-m", message, "--", doc_path])
                sha = git_rev_parse_head(ws.path, runner=runner)
                try:
                    runner(["git", "-C", ws.path, "push", "origin", epic])
                    out = {"committed": sha, "pushed": True}
                except GhError as e:
                    if not _PUSH_REJECTED_RE.search(str(e)):
                        raise
                    out = {"committed": sha, "pushed": False, "conflict": True,
                           "reason": f"push to {epic} was rejected -- the branch moved "
                                     f"under this command. The Task issues exist and are "
                                     f"recorded; re-run to replay the doc rewrite onto the "
                                     f"new tip."}
    return _with_workspace(out, ws)


def task_type_name() -> str:
    """The native Issue Type a Task is created with: classification.task's issueType, else Task."""
    rule = PIPELINE.get("classification", {}).get("task") or {}
    return rule["value"] if rule.get("field") == "issueType" else "Task"


def cmd_create_lld_tasks(gh: GitHub, epic: int, repo_path: str = ".",
                          runner: Runner = _default_runner) -> dict:
    """Create one Task per `## Task <KEY>` section of the Epic's merged `lld.md`,
    renumber the headings, wire `Depends on:` as `blockedBy` edges, and push the doc.
    Idempotent: existing issues are matched back by their `task-key` marker."""
    epic_br = epic_branch(epic)
    doc_path = f"{DOC_ROOT}/epic-{epic}/lld.md"
    runner(["git", "-C", repo_path, "fetch", "origin"])
    try:
        doc = runner(["git", "-C", repo_path, "show", f"origin/{epic_br}:{doc_path}"])
    except GhError:
        raise GhError(f"cannot read {doc_path} on origin/{epic_br} -- merge the LLD-phase "
                       f"Task's design PR first (merge-design-pr), then re-run create-lld-tasks")
    headings = parse_task_headings(doc)
    keyed = [h for h in headings if h["key"]]
    # A `## Task` heading over prose (a carving table, a summary) is never a Task issue.
    defects = {h["key"]: task_section_defect(doc[h["line_end"]:h["end"]]) for h in keyed}
    pending = [h for h in keyed if defects[h["key"]] is None]
    result = {"epic": epic, "doc": doc_path, "created": [], "reused": [],
              "already_numbered": [h["number"] for h in headings if h["number"]],
              "skipped_sections": [
                  {"key": h["key"], "heading": doc[h["line_start"]:h["line_end"]].strip(),
                   "reason": defects[h["key"]]} for h in keyed if defects[h["key"]]],
              "blocked_by": [], "blocked_by_failed": [], "realises": [], "realises_failed": []}
    if not pending:
        return {**result, "committed": None, "pushed": False, "tasks": {},
                "reason": "every `## Task` heading already carries an issue number -- "
                          "nothing to create. A re-run after a completed pass lands here, "
                          "which is what makes this command safe to retry."}
    # A crash between create and push leaves issues the doc doesn't number yet.
    key_to_number = {h["task_key"]: h["number"] for h in headings
                     if h["number"] and h["task_key"]}
    all_issues = {i["number"]: i for i in gh.issue_list()}
    for child in all_issues.values():
        if (child.get("parent") or {}).get("number") != epic:
            continue
        m = _TASK_KEY_MARKER.search(child.get("body") or "")
        if m:
            key_to_number.setdefault(m.group(1), child["number"])
    # `merge-lld-doc` advances only issues that classify as Tasks.
    type_name = task_type_name()
    # Validate every section's Priority/Effort before creating any issue.
    fields = {h["key"]: issue_field_values(
                  parse_task_field(doc[h["line_end"]:h["end"]], _PRIORITY_LINE),
                  parse_task_field(doc[h["line_end"]:h["end"]], _EFFORT_LINE))
              for h in pending}
    for h in pending:
        key = h["key"]
        title = h["title"] or key
        number = key_to_number.get(key)
        if number is None:
            # Only references that resolve are persisted; the rest are reported below.
            realises = [n for n in parse_task_realises(doc[h["line_end"]:h["end"]])
                        if n in all_issues and n not in key_to_number.values()]
            realises_marker = (f"\n<!-- realises: {' '.join(f'#{n}' for n in realises)} -->"
                               if realises else "")
            body = (f"{doc[h['line_end']:h['end']].strip()}\n\n"
                    f"Carved from `{doc_path}` on `{epic_br}` (Epic #{epic}).\n\n"
                    f"<!-- task-key: {key} -->{realises_marker}")
            created = cmd_create_issue(gh, title, body, epic, [], type_name=type_name,
                                       **fields[key])
            if created.get("ok") is False:
                # Never renumber the doc against a half-made issue.
                return {**result, "ok": False, "committed": None, "pushed": False,
                        "tasks": key_to_number, "failed_key": key,
                        "failed_step": created.get("failed_step"),
                        "partial_issue": created.get("issue"),
                        "reason": created.get("reason")}
            number = created["issue"]
            result["created"].append({"key": key, "issue": number, "title": title})
        else:
            result["reused"].append({"key": key, "issue": number, "title": title})
        key_to_number[key] = number
    for h in pending:
        issue_number = key_to_number[h["key"]]
        for dep_key in parse_task_depends_on(doc[h["line_end"]:h["end"]]):
            dep = key_to_number.get(dep_key)
            if dep is None or dep == issue_number:
                result["blocked_by_failed"].append(
                    {"issue": issue_number, "on_key": dep_key,
                     "error": f"no `## Task {dep_key}` section in {doc_path} to depend on"})
                continue
            try:
                gh.add_blocked_by(issue_number, dep)
                result["blocked_by"].append({"issue": issue_number, "on": dep})
            except GhError as e:
                # Re-adding an existing edge errors; a resumed run must not crash on it.
                result["blocked_by_failed"].append(
                    {"issue": issue_number, "on": dep, "error": str(e)})
        # A realised issue waits on the Task that delivers it and closes with that Task's PR.
        for old in parse_task_realises(doc[h["line_end"]:h["end"]]):
            entry = {"issue": issue_number, "realises": old}
            existing = all_issues.get(old)
            if existing is None or old == issue_number or old in key_to_number.values():
                result["realises_failed"].append(
                    {**entry, "error": f"#{old} is not a pre-existing issue -- `Realises:` "
                                       f"names issues that exist before this carving"})
                continue
            if existing["state"] != "OPEN":
                result["realises"].append({**entry, "blocked": False, "reason": "already closed"})
                continue
            try:
                edge = _add_blocked_by_once(gh, old, issue_number)
                result["realises"].append({**entry, "blocked": True,
                                           **({"reason": edge["reason"]} if edge.get("reason") else {})})
            except GhError as e:
                result["realises_failed"].append({**entry, "error": str(e)})
    published = _commit_doc_to_epic_branch(
        repo_path, epic_br, doc_path, number_task_headings(doc, key_to_number), runner,
        message=f"docs(sdlc): number epic-{epic} lld tasks")
    return {**result, "tasks": key_to_number, **published}


def phase_task_parent(gh: GitHub, issue: int) -> Optional[dict]:
    """`{"number", "kind": "initiative"|"epic"}` when `issue`'s parent makes it a
    phase-Task, else None. Only meaningful on a gate path: under an Initiative or
    non-standing Epic, only phase children ever open a gate."""
    parent = gh.issue_epic_info(issue).get("parent")
    if not parent:
        return None
    info = gh.issue_epic_info(parent["number"])
    if is_initiative(info):
        return {"number": parent["number"], "kind": "initiative"}
    if is_epic(info) and not is_epic_standing(info):
        return {"number": parent["number"], "kind": "epic"}
    return None


def _complete_phase_task(gh: GitHub, repo_path: Optional[str], issue: int, stage: str,
                         parent: dict, note: str, runner: Runner, markers: str = "") -> dict:
    """Finish a phase-Task whose gate passed or was skipped: for an Epic's Architecture-phase
    Task, first confirm `architecture.md` is on `epic-<e>` (its design PR merged), then close
    the Task. It has no next stage; stays open if the doc is not on the Epic branch."""
    result = {"issue": issue, "unit": "issue", "phase_task_complete": False,
              "parent": parent["number"]}
    if stage == "architecture" and parent["kind"] == "epic":
        doc = f"{DOC_ROOT}/epic-{parent['number']}/architecture.md"
        on_epic = gh.path_on_ref(doc, epic_branch(parent["number"]))
        result["doc_on_epic_branch"] = on_epic
        if not on_epic:
            result["reason"] = (f"`{doc}` is not on `{epic_branch(parent['number'])}` -- the "
                                f"design PR has not merged; the Task stays open. Merge-design-pr "
                                f"(or the human's merge) must land first, then re-run")
            return result
    gh.issue_comment(issue,
        f"✅ {note}\n\nPhase-Task complete — closing it; its parent #{parent['number']} "
        f"carries the flow from here.\n\n"
        f"{markers}<!-- phase-task-complete: {stage} @ {_utc_now_marker()} -->")
    closed = cmd_close_issue(gh, issue, repo_path=repo_path, runner=runner)
    return {**result, "phase_task_complete": True, "closed": True, "worktree": closed["worktree"]}


def _merged_design_pr(gh: GitHub, comments: list, pr: int) -> Optional[tuple]:
    """`(stage, pr)` when `pr` is the issue's design PR and it is already MERGED: a human
    merged it before any gate opened, so there is no gate-pr marker to check against."""
    found = find_design_pr(comments)
    if found and found[1] == pr and gh.pr_view(pr, fields="state").get("state") == "MERGED":
        return found
    return None


def cmd_pass_gate(gh: GitHub, repo_path: str, issue: int, gate_pr: int, stage: str,
                   runner: Runner = _default_runner, live: bool = True) -> dict:
    """Pass a merged gate: reconcile the branch with its integration base, then claim the next stage
    (`live`) or only advance Stage (`live=False`, CI). Refuses a `stage`/`gate_pr` that
    disagrees with the issue's gate-pr marker (or, when no gate opened, its design PR that a
    human merged early); a phase-Task is closed instead."""
    issue_data = gh.issue_view(issue)
    found = (find_gate_pr(issue_data.get("comments", []))
             or _merged_design_pr(gh, issue_data.get("comments", []), gate_pr))
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
    branch = issue_branch(issue)
    parent = phase_task_parent(gh, issue)
    # The gate merged into `main`, or into its Epic's branch for a phase-Task of a
    # non-standing Epic (its design PR), so reconcile the issue branch with that.
    base = epic_branch(parent["number"]) if parent and parent["kind"] == "epic" else "main"
    with branch_lock(branch), BranchWorkspace(branch, repo_path, runner) as ws:
        git_reconcile_branch(ws.path, branch, base=base, runner=runner)
    if parent is not None:
        return _with_workspace(_complete_phase_task(
            gh, repo_path, issue, stage, parent,
            f"Human review confirmed for `{stage}.md` — merged via #{gate_pr}.", runner),
            ws)
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = _utc_now_marker()
    prefix = f"✅ Human review confirmed for `{stage}.md` — merged via #{gate_pr} — "
    marker = f"<!-- stage-transition: human-review:{stage}->{next_stage} @ {timestamp} -->"
    if live:
        gh.issue_comment(issue, f"{prefix}proceeding to `{next_stage}` stage.\n\n{marker}")
        cmd_claim(gh, issue, next_stage)
    else:
        gh.issue_comment(issue,
            f"{prefix}Stage advanced to `{next_stage}`. Pick up with `/sdlc:run` "
            f"whenever you're ready to run this stage.\n\n{marker}")
        gh.set_stage_field(issue, next_stage)
        gh.set_pipeline_status_field(issue, "todo")
    return _with_workspace({"issue": issue, "unit": "issue", "next_stage": next_stage,
                            "claimed": live}, ws)


def _profile_for_issue(gh: GitHub, issue: int) -> dict:
    """The profile of `issue`'s parent epic (profile labels live on the epic), or the
    default profile when it has no parent."""
    parent = gh.issue_epic_info(issue).get("parent")
    info = gh.issue_epic_info(parent["number"]) if parent else None
    return resolve_profile(info)


def cmd_skip_gate(gh: GitHub, issue: int, stage: str, confidence: int, summary: str,
                   repo_path: Optional[str] = None, runner: Runner = _default_runner) -> dict:
    """Skip Gate B when arch-review's confidence exceeds the profile's
    `skipConfidenceThreshold`; claims `development`, or completes a phase-Task (an Epic's
    Architecture-phase Task first merges its design PR into the epic branch)."""
    if stage != "architecture":
        raise GhError(f"only the architecture gate (Gate B) may be skipped, got stage={stage!r}")
    threshold = _profile_for_issue(gh, issue)["gates"]["skipConfidenceThreshold"]
    if confidence <= threshold:
        raise GhError(f"confidence {confidence} does not clear the "
                       f"{threshold} threshold required to skip Gate B")
    parent = phase_task_parent(gh, issue)
    if parent is not None:
        if parent["kind"] == "epic":
            merge = _merge_design_pr_of(gh, issue, repo_path, runner, gate_cleared=True)
            if not (merge.get("merged") or merge.get("already_merged")):
                return {"issue": issue, "unit": "issue", "phase_task_complete": False,
                        "parent": parent["number"], "design_pr": merge,
                        "reason": merge.get("reason", "the design PR did not merge")}
        return _complete_phase_task(
            gh, repo_path, issue, stage, parent,
            f"⚡ Gate B skipped — arch-review reported {confidence}% confidence (> "
            f"{threshold}% threshold) that `architecture.md` is structurally sound. {summary}",
            runner, markers=f"<!-- arch-review-confidence: {confidence} -->\n")
    next_stage = STAGE_AFTER_GATE[stage]
    timestamp = _utc_now_marker()
    gh.issue_comment(issue,
        f"⚡ Gate B skipped — arch-review reported {confidence}% confidence "
        f"(> {threshold}% threshold) that `architecture.md` is "
        f"structurally sound. {summary} Proceeding directly to `{next_stage}` without "
        f"human sign-off, per the confidence-skip policy in \"Human-review gates\" "
        f"(references/gates.md).\n\n"
        f"<!-- arch-review-confidence: {confidence} -->\n"
        f"<!-- stage-transition: arch-review->{next_stage} @ {timestamp} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": "issue", "next_stage": next_stage, "skipped": True, "confidence": confidence}


# The doc-stage a gate approves -> the profile `gates` toggle requiring a human there,
# and the review that precedes that gate.
HUMAN_GATE_TOGGLES = {"product": "requiresHumanGateA", "architecture": "requiresHumanGateB"}
_GATE_REVIEW = {"product": "product-review", "architecture": "arch-review"}


def cmd_waive_gate(gh: GitHub, issue: int, stage: str, summary: str,
                   repo_path: Optional[str] = None, runner: Runner = _default_runner) -> dict:
    """Pass `stage`'s gate without a human when the profile's `requiresHumanGateA|B` is
    false: claims the next stage, or completes a phase-Task (an Epic's Architecture-phase Task
    first merges its design PR). Raises when a human is required."""
    toggle = HUMAN_GATE_TOGGLES.get(stage)
    if toggle is None:
        raise GhError(f"stage must be one of {sorted(HUMAN_GATE_TOGGLES)} (the doc the gate "
                      f"approves), got {stage!r}")
    profile = _profile_for_issue(gh, issue)
    if profile["gates"][toggle]:
        raise GhError(f"profile '{profile['name']}' requires a human at this gate "
                      f"({toggle}: true) -- open a gate, do not waive it")
    note = (f"⚡ Human review of `{stage}.md` waived — profile '{profile['name']}' sets "
            f"{toggle}: false. {summary}")
    marker = f"<!-- gate-waived: {stage}:{profile['name']} -->\n"
    parent = phase_task_parent(gh, issue)
    if parent is not None:
        if parent["kind"] == "epic" and stage == "architecture":
            merge = _merge_design_pr_of(gh, issue, repo_path, runner, gate_cleared=True)
            if not (merge.get("merged") or merge.get("already_merged")):
                return {"issue": issue, "unit": "issue", "phase_task_complete": False,
                        "parent": parent["number"], "design_pr": merge,
                        "reason": merge.get("reason", "the design PR did not merge")}
        return _complete_phase_task(gh, repo_path, issue, stage, parent, note, runner,
                                    markers=marker)
    next_stage = STAGE_AFTER_GATE[stage]
    gh.issue_comment(issue, f"{note} Proceeding to `{next_stage}`.\n\n{marker}"
                            f"<!-- stage-transition: {_GATE_REVIEW[stage]}->{next_stage} "
                            f"@ {_utc_now_marker()} -->")
    cmd_claim(gh, issue, next_stage)
    return {"issue": issue, "unit": "issue", "next_stage": next_stage, "waived": True,
            "profile": profile["name"]}


_CLOSES_ISSUE_RE = re.compile(r"\bCloses #\d+", re.IGNORECASE)
# Gate PRs are `issue-<n>` -> main, or -- for a non-standing Epic's architecture phase-Task --
# its design PR `issue-<n>` -> `epic-<e>` (recognised by its body marker). An `epic-<n>` head is
# the epic's integration PR, never a gate.
_GATE_BRANCH_RE = re.compile(rf"^{re.escape(ISSUE_BRANCH_PREFIX)}(?P<issue_n>\d+)$")
# Bot logins end in "[bot]"; ignoring them stops a bot comment from looping the workflow.
_BOT_AUTHOR_RE = re.compile(r"\[bot\]$")


class _NotAGate(Exception):
    """Raised by `_match_open_gate` when a PR is not an open gate; carries the skip reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def cmd_merge_gate(gh: GitHub, pr_number: int, issue: int, stage: str,
                   operator_confirmed: bool = False) -> dict:
    """Merge an open human-review gate PR (Gate A's `issue-<n>` -> `main`, or a design PR held
    for the human) because the operator said to. Refuses (exit 0) without `operator_confirmed`,
    for a PR that is no open gate of `issue`/`stage`, when behind its base (`behind_base`) or
    without green required checks. A design PR and a Roadmap Task's gate squash; a standing or
    parentless issue's merges as a merge commit; the branch is always kept. `pass-gate`
    (next-action, or the Action) then finishes the bookkeeping."""
    result = {"pr": pr_number, "issue": issue, "stage": stage, "merged": False}
    if not operator_confirmed:
        return {**result, "refused": True,
                "reason": "merge-gate merges a human-review gate PR only when the operator "
                          "explicitly said to; re-run with --operator-confirmed after they did"}
    pr = gh.pr_view(pr_number, fields="number,headRefName,baseRefName,state,mergedAt,body")
    if pr.get("state") == "MERGED":
        return {**result, "already_merged": True, "next": _pass_gate_hint(issue, pr_number, stage)}
    if pr.get("state") != "OPEN":
        return {**result, "refused": True,
                "reason": f"PR #{pr_number} is closed without merging -- a human decision"}
    try:
        matched, marker_stage, _status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {**result, "refused": True, "reason": f"not an open gate PR: {e.reason}"}
    if matched != issue or marker_stage != stage:
        return {**result, "refused": True,
                "reason": f"PR #{pr_number} is #{matched}'s {marker_stage} gate, not #{issue}'s "
                          f"{stage} gate"}
    head, base = pr["headRefName"], pr["baseRefName"]
    behind = gh.branch_behind_by(head, base=base)
    if behind:
        return {**result, "behind_base": behind,
                "reason": f"branch {head} is {behind} commit(s) behind {base} -- run "
                          f"sync-branch {issue}, wait for fresh checks, then re-run"}
    view = gh.pr_view(pr_number, "comments,headRefOid")
    checks_state, missing = merge_gate_status(gh.pr_files(pr_number), gh.pr_checks(pr_number),
                                              view.get("comments", []), view.get("headRefOid"))
    if checks_state != "passed":
        return {**result, "checks": checks_state, "missing_required_workflows": missing,
                "reason": f"PR #{pr_number} checks not passed (status={checks_state})"
                          + (f"; no passing check or attestation from: {', '.join(missing)}"
                             if missing else "")}
    design = bool(_DESIGN_PR_BODY_MARKER.search(pr.get("body") or ""))
    method = "squash" if design or phase_task_parent(gh, issue) is not None else "merge"
    try:
        gh.pr_merge(pr_number, delete_branch=False, method=method)
    except GhError:
        # GitHub can answer 5xx after the merge already landed; only a PR still open failed.
        if gh.pr_view(pr_number, fields="state").get("state") != "MERGED":
            raise
    gh.pr_comment(pr_number, "Merged by the pipeline at the operator's explicit instruction.")
    gh.issue_comment(issue,
        f"✅ Gate PR #{pr_number} merged into `{base}` at the operator's instruction.\n\n"
        f"<!-- gate-merged: {stage}:{pr_number} @ {_utc_now_marker()} -->")
    return {**result, "merged": True, "method": method, "base": base, "design_pr": design,
            "next": _pass_gate_hint(issue, pr_number, stage)}


def _pass_gate_hint(issue: int, pr: int, stage: str) -> str:
    return f"pass-gate {issue} --gate-pr {pr} --stage {stage} (next-action returns it)"


def _match_open_gate(gh: GitHub, pr: dict, pr_number: int) -> tuple:
    """Identify `pr` as some issue's currently open gate PR (status in
    `GATE_PENDING_STATUSES`, matching gate-pr marker). Returns
    `(issue_number, marker_stage, status)`; raises `_NotAGate(reason)` otherwise."""
    m = _GATE_BRANCH_RE.match(pr.get("headRefName") or "")
    if not m:
        raise _NotAGate(f"PR #{pr_number} head branch {pr.get('headRefName')!r} is not "
                         f"an issue-<n> branch")
    issue_number = int(m.group("issue_n"))
    design = _DESIGN_PR_BODY_MARKER.search(pr.get("body") or "")
    if pr.get("baseRefName") != "main" and not (
            design and int(design.group(2)) == issue_number
            and (pr.get("baseRefName") or "").startswith(EPIC_BRANCH_PREFIX)):
        raise _NotAGate(f"PR #{pr_number} base is {pr.get('baseRefName')!r}, not main or the "
                        f"epic branch of a design PR")

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

    return issue_number, marker_stage, status


def cmd_auto_pass_gate(gh: GitHub, repo_path: str, pr_number: int,
                        runner: Runner = _default_runner) -> dict:
    """CI entry point for any closed PR: a merged gate PR passes the gate with
    `live=False`; one closed unmerged marks the issue needs-human. Any non-gate PR
    returns `{"ok": True, "skipped": reason}` with no mutation."""
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
        issue_number, marker_stage, _status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    try:
        if merged:
            result = cmd_pass_gate(gh, repo_path, issue_number, pr_number, marker_stage,
                                    runner=runner, live=False)
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
    """CI entry point: flip an open gate's Pipeline Status from `awaiting-human-review`
    to `feedback-received` when a human leaves non-empty feedback. Visibility only;
    anything else returns `{"ok": True, "skipped": reason}`."""
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
        issue_number, _marker_stage, status = _match_open_gate(gh, pr, pr_number)
    except _NotAGate as e:
        return {"ok": True, "skipped": e.reason}
    except GhError as e:
        return {"ok": False, "reason": f"could not read the matching issue: {e}"}

    if status != "awaiting-human-review":
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
    """Set Pipeline Status back to `awaiting-human-review` unconditionally, after gate
    feedback is addressed and pushed. Posts no comment."""
    gh.set_pipeline_status_field(issue, "awaiting-human-review")
    return {"issue": issue, "pipeline_status": "awaiting-human-review"}


def cmd_resolve_thread(gh: GitHub, thread_id: str, reply: Optional[str] = None) -> dict:
    """Reply to (when `reply`) and resolve one PR review thread."""
    if reply:
        gh.reply_review_thread(thread_id, reply)
    try:
        gh.resolve_review_thread(thread_id)
    except GhError as e:
        if not reply:
            raise
        return {"thread": thread_id, "replied": True, "resolved": False, "ok": False,
                "error": str(e), "reason": "the reply was posted but resolving failed -- "
                                          "re-run without --reply so it is not posted twice"}
    return {"thread": thread_id, "replied": bool(reply), "resolved": True}


def cmd_pause_for_epic_regate(gh: GitHub, issue: int, epic: int,
                              gate_pr: Optional[int] = None, found_by: str = "lld") -> dict:
    """Park a unit waiting on its Epic's Architecture revision: Pipeline Status `todo` (Stage
    kept) so it re-enters the normal loop once the revision Task closes (its `blockedBy` edge
    holds it until then). `gate_pr` is the revision's design/gate PR once it exists (it is
    only raised after the revision's `architecture` stage finishes); `found_by` names the
    stage that hit the deviation."""
    gh.set_pipeline_status_field(issue, "todo")
    where = (f"see gate PR #{gate_pr}" if gate_pr is not None
             else "its design PR opens once the revision's architecture stage finishes")
    gh.issue_comment(issue,
        f"⏸️ Paused — `{found_by}` found this doesn't fit epic #{epic}'s current "
        f"architecture. Epic #{epic}'s architecture is being revised; {where}. This task "
        f"resumes automatically once the revision closes.")
    return {"issue": issue, "paused_for_epic_regate": epic, "gate_pr": gate_pr,
            "found_by": found_by}


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


def cmd_open_dev_pr(gh: GitHub, issue: int, title: str, body: str, summary: str,
                    allow_empty: bool = False, repo_path: Optional[str] = None,
                    runner: Runner = _default_runner) -> dict:
    """Open the development draft PR and set Stage to `pr-review`; an already-open
    PR on the branch is reused (`created: False`). A branch with no commits ahead of its
    base gets an empty commit first only with `allow_empty` (`empty_commit`), else the
    refusal names the flag. Returns `pr`, `created`."""
    existing = gh.pr_list_for_branch(issue_branch(issue))
    if existing:
        pr_number = existing[0]["number"]
        return {"issue": issue, "pr": pr_number, "created": False,
                "reason": f"PR #{pr_number} is already open on {issue_branch(issue)} -- "
                          f"reusing it rather than opening a duplicate"}
    base = integration_base(gh, issue)
    # Refuse a diff that authors a design doc or another Task's footprint file before opening
    # the PR (development authors no design doc; deviations belong in the PR description).
    try:
        changed = gh.files_since(base, issue_branch(issue))
    except GhError:
        changed = []
    offenders = dev_pr_scope_offenders(gh, issue, changed)
    reason = _dev_pr_scope_refusal_reason(offenders)
    if reason:
        return {"issue": issue, "created": False, "refused": True, "reason": reason,
                "offending_design_docs": offenders["design_docs"],
                "offending_footprint_paths": offenders["foreign_footprint"]}
    pr_body = f"{body}\n\nCloses #{issue}"
    extra: dict = {}
    try:
        pr_number = gh.pr_create(base=base, head=issue_branch(issue), title=title,
                                 body=pr_body, draft=True)
    except GhError as e:
        if not _NO_COMMITS_RE.search(str(e)):
            raise
        if not allow_empty:
            raise GhError(f"{issue_branch(issue)} has no commits ahead of {base}, so GitHub "
                          f"refuses the PR. A verify-only Task re-runs open-dev-pr with "
                          f"--allow-empty (lands an empty commit first); otherwise commit and "
                          f"push the work, then re-run.\n{e}")
        extra["empty_commit"] = _push_empty_commit(issue, repo_path, runner)
        pr_number = gh.pr_create(base=base, head=issue_branch(issue), title=title,
                                 body=pr_body, draft=True)
    gh.set_stage_field(issue, "pr-review")
    gh.issue_comment(issue,
        f"✅ {summary} Draft PR: #{pr_number} — what was built and why is in the PR "
        f"description.\n\n"
        f"Not yet queued for review: `development` still owes `record-local-ci` per "
        f"suite it ran and `handoff-to-pr-review`, which posts the marker "
        f"`list-ready-for-review` reads.")
    return {"issue": issue, "pr": pr_number, "created": True, **extra}


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
            result["dev_pr_scope"] = offenders
            scope_reason = _dev_pr_scope_refusal_reason(offenders)
            if scope_reason:
                result["ok"] = False
                problems.append(scope_reason)
            result["problems"] = problems
    # A non-standing Epic's phase-Task authors `epic-<e>/<doc>` in place (docs merge into the
    # epic branch by its design PR); everything else keeps `issue-<n>/`.
    epic = design_phase_epic(gh, issue, expect)
    docs_rel = design_doc_dir(epic, issue)
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


# Workflows required when a PR touches their paths. `workflow` must match the
# workflow file's `name:`; `suite` names the `local-ci` attestation that can stand in for it.
# `excludeGlobs` mirrors the workflow's path negations (`!**/*.md`): a match never counts.
# `bases`: the PR base branches this entry applies to (empty = every base). `attestable`:
# False when no `local-ci` attestation can stand in for it (a passing GHA check only).
REQUIRED_WORKFLOWS = tuple(
    {"workflow": w["workflow"], "suite": w["suite"],
     "prefixes": tuple(w["prefixes"]), "files": tuple(w.get("files", ())),
     "excludeGlobs": tuple(w.get("excludeGlobs", ())),
     "bases": tuple(w.get("bases", ())),
     "attestable": w.get("attestable", True)}
    for w in CONFIG["requiredWorkflows"]
)

# Suites whose attestation `record-local-ci` refuses (`attestable: false`): only a passing
# GHA check satisfies their required workflow.
NON_ATTESTABLE_SUITES = frozenset(
    w["suite"] for w in CONFIG["requiredWorkflows"] if w.get("attestable") is False)


def _workflow_covers(spec: dict, path: str) -> bool:
    """Whether the workflow's `paths:` filter (prefixes/files minus excludeGlobs) matches.
    A leading `**/` also matches at the repo root, as in GHA."""
    if any(fnmatch.fnmatch(path, g) or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:]))
           for g in spec["excludeGlobs"]):
        return False
    return path.startswith(spec["prefixes"]) or path in spec["files"]


def _workflow_applies_to_base(spec: dict, base_ref: Optional[str]) -> bool:
    """Whether a base-scoped entry applies to a PR into `base_ref`. An entry with no `bases`
    applies to every base; a scoped entry applies only when `base_ref` is one of them (an
    unknown `base_ref` never matches a scoped entry, so it is not over-required)."""
    return not spec["bases"] or base_ref in spec["bases"]


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


def missing_required_workflows(changed_files: list, checks: list,
                               comments: list = None, head_sha: str = None,
                               base_ref: Optional[str] = None) -> list:
    """Names of required workflows the PR touches that have neither a passing GHA check
    from that workflow nor a `local-ci` attestation for `head_sha`. A base-scoped entry
    (`bases`) is skipped unless the PR's `base_ref` matches it."""
    attested = local_ci_suites_attested(comments or [], head_sha)
    missing = []
    for spec in REQUIRED_WORKFLOWS:
        if not _workflow_applies_to_base(spec, base_ref):
            continue
        if not any(_workflow_covers(spec, p) for p in changed_files):
            continue
        if workflow_check_state(checks, spec["workflow"]) == "passing":
            continue
        if spec.get("suite") in attested:
            continue
        missing.append(spec["workflow"])
    return missing


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
        if any(p.startswith(spec["prefixes"]) or p in spec["files"] for p in base_delta_files):
            return True
    return not all(_is_doc_path(p) for p in base_delta_files)


def _is_doc_path(path: str) -> bool:
    """True for a path under `docRoot`, any `*.md`, or anything under `docs/`."""
    return (path.startswith(DOC_ROOT.rstrip("/") + "/")
            or path.endswith(".md")
            or path.startswith("docs/"))


def touches_pipeline_config(changed_files: list) -> bool:
    """True when the changes include the driven repo's pipeline config file."""
    return any(os.path.basename(f) == CONFIG_FILENAME for f in changed_files)


def merge_gate_status(changed_files: list, checks: list,
                      comments: list = None, head_sha: str = None,
                      base_ref: Optional[str] = None) -> tuple:
    """`(status, missing_workflows)` for pr-checks and merge-pr. A pending/failed check
    status wins over `missing-checks`, but `missing` is always returned alongside it."""
    status = checks_status(checks)
    missing = missing_required_workflows(changed_files, checks, comments, head_sha, base_ref)
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
    checks = gh.pr_checks(pr)
    fresh = local_ci_suites_attested(comments, head)
    latest = _latest_attested_sha_by_suite(comments)
    stale, carried, passed_by_check = [], [], []
    for spec in REQUIRED_WORKFLOWS:
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


def cmd_pr_checks(gh: GitHub, pr_number: int) -> dict:
    checks = gh.pr_checks(pr_number)
    view = gh.pr_view(pr_number, "comments,headRefOid,baseRefName")
    status, missing = merge_gate_status(
        gh.pr_files(pr_number), checks,
        view.get("comments", []), view.get("headRefOid"), view.get("baseRefName"))
    result = {"pr": pr_number, "status": status,
              "missing_required_workflows": missing, "checks": checks}
    if missing:
        result["hint"] = _MISSING_WORKFLOW_HINT
    return result


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
    checks = gh.pr_checks(pr_number)
    files = gh.pr_files(pr_number)
    # `base` (integration_base) is the PR's base branch -- no extra field fetch needed.
    status, missing = merge_gate_status(files, checks, view.get("comments", []), head, base)
    if status != "passed":
        detail = f" (no passing check or fresh local-ci attestation from: {', '.join(missing)})" if missing else ""
        hint = f" {_MISSING_WORKFLOW_HINT}" if status == "missing-checks" and missing else ""
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
    return _finalize_merged_pr(gh, pr_number, issue, repo_path, base, files, carried_forward,
                               run_id=run_id)


def _finalize_merged_pr(gh: GitHub, pr_number: int, issue: int, repo_path: str,
                        base: str, files: list, extra: dict,
                        run_id: Optional[str] = None) -> dict:
    """Post-merge bookkeeping: close an epic-branch child `Closes #<n>` didn't, the
    `Merged via` note, run-cap accounting and releasing the worktree."""
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
        gh.issue_comment(issue, f"Merged via #{pr_number}.")
        realised = _close_realised_issues(gh, issue, view.get("body"), pr=pr_number)
    terminal = record_terminal_unit(gh, issue, run_id=run_id)
    return {"pr": pr_number, "issue": issue, "merged": True, "issue_closed": issue_closed,
            "config_changed": touches_pipeline_config(files),
            **extra,
            **({"run_terminal": terminal} if terminal else {}),
            **({"realised_closed": realised} if realised else {}),
            "worktree": release_worktree(issue_branch(issue), base_repo=repo_path, runner=gh._run)}


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


def _release_unit_worktree(issue: int, base_repo: str = ".",
                            runner: Runner = _default_runner) -> dict:
    """Release whichever of `issue-<n>` / `epic-<n>` currently has a worktree."""
    for branch in (issue_branch(issue), epic_branch(issue)):
        result = release_worktree(branch, runner=runner, base_repo=base_repo)
        if result.get("released") or result.get("reason") != "no worktree":
            return {**result, "branch": branch}
    return {"released": False, "reason": "no worktree"}


def cmd_mark_blocked(gh: GitHub, issue: int, dep: int, repo_path: str = ".") -> dict:
    """Add a native blockedBy edge on `dep`, reset Pipeline Status to `todo` (so the
    parked unit doesn't read as a crashed in-progress run) and release its worktree."""
    gh.add_blocked_by(issue, dep)
    gh.set_pipeline_status_field(issue, "todo")
    gh.issue_comment(issue, f"⏸️ Blocked — waiting on #{dep} to merge.")
    return {"issue": issue, "status": "blocked", "on": dep,
            "worktree": _release_unit_worktree(issue, base_repo=repo_path, runner=gh._run)}


def cmd_mark_needs_human(gh: GitHub, issue: int, reason: str, repo_path: str = ".") -> dict:
    """Park the unit as needs-human with `reason` and release its worktree."""
    gh.set_pipeline_status_field(issue, "needs-human")
    gh.issue_comment(issue, f"🙋 Needs human input — {reason}")
    return {"issue": issue, "status": "needs-human",
            "worktree": _release_unit_worktree(issue, base_repo=repo_path, runner=gh._run)}


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


def _delete_merged_design_pr_branch(gh: GitHub, issue: int) -> Optional[dict]:
    """Delete `origin/issue-<n>` once the unit's design PR is MERGED (merge-design-pr keeps
    the branch until the Task closes); None when there is no design PR, never raises."""
    try:
        found = find_design_pr(gh.issue_view(issue).get("comments", []))
        if not found:
            return None
        pr = found[1]
        if gh.pr_view(pr, fields="state").get("state") != "MERGED":
            return {"pr": pr, "deleted": False, "reason": "design PR is not merged"}
        gh.delete_branch(issue_branch(issue))
        return {"pr": pr, "deleted": True, "branch": issue_branch(issue)}
    except GhError as e:
        return {"deleted": False, "reason": str(e)}


def cmd_close_issue(gh: GitHub, issue: int, repo_path: Optional[str] = None,
                    runner: Runner = _default_runner, not_planned: bool = False,
                    reason: Optional[str] = None) -> dict:
    """Close `issue` (`not_planned`: state reason NOT_PLANNED, with `reason` commented first),
    apply the terminal fields, release its worktree, delete a merged design PR's branch
    (`design_pr_branch`, non-fatal) and close the issues it `Realises:` -- for a unit that
    never merges through `merge-pr`. Counts toward the run cap."""
    if reason and not not_planned:
        raise GhError("--reason only goes with --not-planned")
    body = gh.issue_view(issue).get("body")
    if not_planned:
        gh.issue_comment(issue, f"🚫 Closed as not planned — {reason or 'dropped by the operator'}.")
    gh.issue_close(issue, "not planned" if not_planned else "completed")
    cmd_mark_issue_closed(gh, issue)
    realised = None if not_planned else _close_realised_issues(gh, issue, body)
    released = _release_unit_worktree(issue, base_repo=repo_path or ".", runner=runner)
    design_branch = _delete_merged_design_pr_branch(gh, issue)
    terminal = record_terminal_unit(gh, issue)
    return {"issue": issue, "closed": True, "worktree": released,
            **({"state_reason": "not_planned"} if not_planned else {}),
            **({"design_pr_branch": design_branch} if design_branch else {}),
            **({"run_terminal": terminal} if terminal else {}),
            **({"realised_closed": realised} if realised else {})}


def cmd_comment(gh: WorkItemProvider, issue: int, body: Optional[str] = None,
                body_file: Optional[str] = None) -> dict:
    """Post a plain comment on `issue` from `body` or `body_file` (one of them), capped at
    `HANDOFF_CAP` chars. No marker: it is the orchestrator's audit-trail note."""
    if (body is None) == (body_file is None):
        raise GhError("pass exactly one of --body / --body-file")
    if body_file is not None:
        try:
            with io.open(os.path.expanduser(body_file), encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            raise GhError(f"--body-file must be a readable file holding the comment text: {exc}")
    body = (body or "").strip()
    if not body:
        raise GhError("the comment is empty -- say something or do not comment")
    if len(body) > HANDOFF_CAP:
        return {"issue": issue, "refused": True,
                "reason": f"the comment is {len(body):,} chars, over the {HANDOFF_CAP:,}-char "
                          f"cap (references/stage-playbooks.md, \"Comment size is a "
                          f"contract\"); trim it and re-run"}
    gh.issue_comment(issue, body)
    return {"issue": issue, "commented": True, "chars": len(body)}


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


def cmd_pairing_counts(gh: GitHub, issue: int) -> dict:
    """Escalation-valve bounce counts read back from comment markers: pr-review rework,
    sync conflicts, and per-role `design_review` counts, each with same-class recurrences.
    Any same-class recurrence >= 1 is an escalation signal on its own."""
    comments = gh.issue_view(issue).get("comments", [])
    rework_since_clean = total_rework = total_clean = sync_conflicts = 0
    pr_review_same_class = 0
    design_review: dict = {}
    for c in comments:
        body = c.get("body", "")
        m = _PR_REVIEW_OUTCOME_MARKER.search(body)
        if m:
            if m.group(1) == "rework":
                total_rework += 1
                rework_since_clean += 1
                if m.group(3) == "true":
                    pr_review_same_class += 1
            else:
                total_clean += 1
                rework_since_clean = 0
        if _SYNC_CONFLICT_MARKER.search(body):
            sync_conflicts += 1
        d = _DESIGN_REVIEW_OUTCOME_MARKER.search(body)
        if d:
            outcome, role = d.group(1), d.group(2)
            counts = design_review.setdefault(
                role, {"rework_since_last_clean": 0, "total_rework": 0, "total_clean": 0,
                       "same_class_recurrence_count": 0})
            if outcome == "rework":
                counts["total_rework"] += 1
                counts["rework_since_last_clean"] += 1
                if d.group(3) == "true":
                    counts["same_class_recurrence_count"] += 1
            else:
                counts["total_clean"] += 1
                counts["rework_since_last_clean"] = 0
    return {"issue": issue,
            "thresholds": {"replace_at": ESCALATION["replaceAt"],
                           "needs_human_at": ESCALATION["needsHumanAt"]},
            "pr_review_rework_since_last_clean": rework_since_clean,
            "pr_review_total_rework": total_rework,
            "pr_review_total_clean": total_clean,
            "pr_review_same_class_recurrence_count": pr_review_same_class,
            "sync_conflict_count": sync_conflicts,
            # Per role: each review role is a separate valve pairing; absent = never ran.
            "design_review": design_review}


def _issue_kind(type_name: str) -> str:
    """The classification kind whose issueType rule names `type_name`, else its lowercase."""
    for kind, rule in PIPELINE.get("classification", {}).items():
        if rule.get("field") == "issueType" and rule.get("value") == type_name:
            return kind
    return type_name.lower()


def _option_name(field: str, value, options: dict, aliases: Optional[dict] = None) -> str:
    """`value` (or an `aliases` entry for it) matched case-insensitively to one of `options`'
    names; GhError listing the valid values if none."""
    if aliases and value is not None and str(value).strip().lower() in aliases:
        value = aliases[str(value).strip().lower()]
    for name in options:
        if value is not None and name.lower() == str(value).strip().lower():
            return name
    valid = ", ".join(sorted(options))
    extra = f" (aliases: {', '.join(sorted(aliases))})" if aliases else ""
    raise GhError(f"{field} {value!r} is not a configured option -- valid values: {valid}{extra}. "
                  f"Refused before creating anything.")


def issue_field_values(priority: Optional[str] = None, effort: Optional[str] = None) -> dict:
    """Validated `{"priority", "effort"}` for `create-issue`, each only when its field is
    configured; an omitted value takes `pipeline.issueDefaults`."""
    defaults = PIPELINE["issueDefaults"]
    out = {}
    if PRIORITY_FIELD_ID:
        out["priority"] = _option_name("Priority", priority or defaults.get("priority"),
                                       PRIORITY_OPTION_IDS, aliases=PRIORITY_ALIASES)
    if EFFORT_FIELD_ID:
        out["effort"] = _option_name("Effort", effort or defaults.get("effort"), EFFORT_OPTION_IDS)
    return out


def cmd_create_issue(gh: GitHub, title: str, body: str, parent: int, labels: list,
                     type_name: str = "Task", priority: Optional[str] = None,
                     effort: Optional[str] = None, blocked_by: Optional[list] = None) -> dict:
    """Create an issue with its native Issue Type (mandatory), parent link, Pipeline Status
    `todo`, Priority/Effort when configured, and a native `blockedBy` edge on each of
    `blocked_by` (how Epics are ordered). Inputs are validated before creating; a later step
    failing returns `ok: False` naming the created issue."""
    known = sorted({*KNOWN_ISSUE_TYPES, *ISSUE_TYPE_IDS})
    if type_name not in known:
        raise GhError(f"--type {type_name!r} is not an issue type (one of {known})")
    if type_name not in ISSUE_TYPE_IDS:
        raise GhError(
            f"Issue Type {type_name!r} is not provisioned: create it under the org's Settings "
            f"-> Issue types and add its GraphQL id to projectFields.issueTypeIds (configured: "
            f"{sorted(ISSUE_TYPE_IDS)}). Refused before creating anything -- every pipeline "
            f"issue carries a native type.")
    fields = issue_field_values(priority, effort)
    rule = PIPELINE.get("classification", {}).get(_issue_kind(type_name)) or {}
    if rule.get("field") == "label" and rule["value"] not in labels:
        labels = [*labels, rule["value"]]
    # Parent first: re-runs of cut-phase-tasks / create-lld-tasks find a half-made
    # issue only through its parent link, so any later failure must not duplicate it.
    steps = [("add_sub_issue", lambda n: gh.add_sub_issue(parent, n)),
             ("set_issue_type", lambda n: gh.set_issue_type(n, type_name)),
             ("set_pipeline_status", lambda n: gh.set_pipeline_status_field(n, "todo"))]
    if "priority" in fields:
        steps.append(("set_priority", lambda n: gh.set_priority_field(n, fields["priority"])))
    if "effort" in fields:
        steps.append(("set_effort", lambda n: gh.set_effort_field(n, fields["effort"])))
    for dep in blocked_by or []:
        steps.append((f"add_blocked_by:{dep}", lambda n, dep=dep: _add_blocked_by_once(gh, n, dep)))
    number = gh.issue_create(title, body, labels)
    result = {"issue": number, "parent": parent, "type": type_name,
              "pipeline_status": "todo", **fields,
              **({"blocked_by": list(blocked_by)} if blocked_by else {})}
    # The issue now exists: report failures with its number rather than raising,
    # so the caller repairs it instead of retrying into a duplicate.
    for i, (name, step) in enumerate(steps):
        try:
            step(number)
        except GhError as e:
            todo = ", ".join(n for n, _ in steps[i:])
            edges = "".join(f" add-blocked-by {number} --on {dep};" for dep in blocked_by or []
                            if f"add_blocked_by:{dep}" in [n for n, _ in steps[i:]])
            return {**result, "ok": False, "complete": False, "failed_step": name,
                    "error": str(e),
                    "reason": f"issue #{number} was created but {name} failed ({todo} not "
                              f"done). Do NOT re-run create-issue -- that duplicates it. Run "
                              f"`repair-issue {number} --parent {parent} --type {type_name}"
                              + "".join(f" --{k} {v}" for k, v in fields.items())
                              + "` and" + (f" then{edges}" if edges else "") + " continue."}
    return result


def cmd_list_needs_human(gh: GitHub) -> dict:
    """Open needs-human issues with the reason from their latest mark-needs-human
    comment: `{"needs_human": [{issue, title, reason}]}`."""
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


_KIND_TYPES = {"initiative": "Initiative", "epic": "Epic", "task": "Task"}


def inferred_issue_type(issue: dict) -> Optional[str]:
    """The native type an untyped issue's classification labels name unambiguously, else None."""
    labels = label_names(issue)
    kinds = {kind for kind, rule in PIPELINE.get("classification", {}).items()
             if rule.get("field") == "label" and rule.get("value") in labels}
    return _KIND_TYPES.get(kinds.pop()) if len(kinds) == 1 else None


def cmd_repair_issue(gh: WorkItemProvider, number: int, parent: Optional[int] = None,
                     type_name: Optional[str] = None, priority: Optional[str] = None,
                     effort: Optional[str] = None) -> dict:
    """Set whatever of create-issue's fields `number` lacks, never overwriting a set value --
    except that an explicit `--priority`/`--effort` is set even over an existing value (so a
    half-created issue can be corrected to the requested Priority/Effort). Parent and type keep
    never-overwrite unless a flag is passed. Validates before the first write, so a re-run
    finishes a partial repair."""
    info, fields = gh.issue_epic_info(number), gh.issue_fields(number)
    steps, already = [], []
    if info.get("parent"):
        already.append("parent")
    elif parent is not None:
        steps.append(("parent", lambda: gh.add_sub_issue(parent, number)))
    if issue_type(info):
        already.append("issueType")
    else:
        type_name = type_name or inferred_issue_type(info)
        if type_name is None:
            raise GhError(f"#{number} has no issueType and its classification names none "
                          f"unambiguously -- pass --type (one of {sorted(ISSUE_TYPE_IDS)})")
        if type_name not in ISSUE_TYPE_IDS:
            raise GhError(f"--type {type_name!r} is not in projectFields.issueTypeIds "
                          f"({sorted(ISSUE_TYPE_IDS)})")
        info = {**info, "issueType": {"name": type_name}}
        steps.append(("issueType", lambda: gh.set_issue_type(number, type_name)))
    if fields.get("Pipeline Status"):
        already.append("Pipeline Status")
    elif not (is_initiative(info) or is_epic(info)):  # cleared by design on containers
        steps.append(("Pipeline Status", lambda: gh.set_pipeline_status_field(number, "todo")))
    values = issue_field_values(priority, effort)
    for name, setter, passed in (("Priority", gh.set_priority_field, priority is not None),
                                 ("Effort", gh.set_effort_field, effort is not None)):
        value = values.get(name.lower())
        if value is None:
            continue  # field not configured
        if passed:  # an explicit flag overrides even a value already set
            steps.append((name, lambda setter=setter, value=value: setter(number, value)))
        elif fields.get(name):
            already.append(name)
        else:  # fill the missing field with its default, never overwriting
            steps.append((name, lambda setter=setter, value=value: setter(number, value)))
    for _, step in steps:
        step()
    return {"issue": number, "set": [name for name, _ in steps], "already_set": already}


def cmd_audit_issues(gh: WorkItemProvider, epic: Optional[int] = None) -> dict:
    """Read-only: open issues in the Initiative/Epic trees (plus parentless ones), or only
    `epic`'s (or an Initiative's) subtree when given, missing issueType, parent (Tasks and other
    non-containers), Priority, Effort or Pipeline Status, each with a `repair-issue` command;
    and open non-standing Epics with no phase-Tasks, with the `cut-phase-tasks` command.
    Children come first; an Epic's/Initiative's own gaps (`container: true`) come last, so a
    scoped audit never leads with the container's Priority/Effort."""
    issues = gh.issue_list()
    by_number = {i["number"]: i for i in issues}
    children: dict = {}
    for i in issues:
        if i.get("parent"):
            children.setdefault(i["parent"]["number"], []).append(i["number"])
    stack = [epic] if epic is not None else [
        i["number"] for i in issues if is_initiative(i) or is_epic(i)]
    scope = set() if epic is not None else {i["number"] for i in issues if not i.get("parent")}
    seen: set = set()
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(children.get(n, []))
    optional = [(name, fid) for name, fid in (("Priority", PRIORITY_FIELD_ID),
                                              ("Effort", EFFORT_FIELD_ID)) if fid]
    found = []
    for n in sorted(scope | seen):
        i = by_number.get(n)
        if not i or i["state"] != "OPEN":
            continue
        container = is_initiative(i) or is_epic(i)
        missing = [] if issue_type(i) else ["issueType"]
        # Engineering-driven Epics are parentless by design.
        if not i.get("parent") and not container:
            missing.append("parent")
        missing += [name for name, _ in optional if not i.get("fields", {}).get(name)]
        # An Epic/Initiative's Pipeline Status is cleared while its Tasks carry the work.
        if pipeline_status(i) is None and not container:
            missing.append("Pipeline Status")
        repairs = []
        if missing:
            repair = f"repair-issue {n}" + (" --parent <P>" if "parent" in missing else "")
            if "issueType" in missing and not inferred_issue_type(i):
                repair += " --type <T>"
            repairs.append(repair)
        if (is_epic(i) and not is_epic_legacy(i) and not is_epic_standing(i)
                and epic_lacks_phase_tasks(issues, n)):
            missing = [*missing, "phase-Tasks"]
            repairs.append(f"cut-phase-tasks {n} --repo-path <p>")
        if missing:
            found.append({"issue": n, "title": i["title"], "missing": missing,
                          "repair": "; ".join(repairs), **({"container": True} if container else {})})
    found.sort(key=lambda f: (f.get("container", False), f["issue"]))
    return {"issues": found, "count": len(found)}


_EPIC_CLOSEABLE_MARKER = "<!-- epic-closeable-checklist-posted -->"


def cmd_check_epics_closeable(gh: GitHub) -> dict:
    """For each open non-standing epic with every child closed, post a one-time
    closing checklist and assign the human; never closes the epic. Idempotent via
    `_EPIC_CLOSEABLE_MARKER`. Returns `closeable_epics`."""
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
        # An open epic's docs live on its epic branch; `close-epic` carries them to main.
        branch = epic_branch(epic["number"])
        doc_names = ("architecture.md", "lld.md")
        missing_docs = [f"{DOC_ROOT}/epic-{epic['number']}/{name}"
                        for name in doc_names
                        if not gh.path_on_ref(f"{DOC_ROOT}/epic-{epic['number']}/{name}", branch)]
        docs_line = (
            f"- [ ] {len(missing_docs)} epic doc(s) never reached `{branch}` "
            f"({', '.join(f'`{d}`' for d in missing_docs)}) — merge their design PRs before "
            "closing, or they stay reachable only on an unmerged phase-Task branch and never "
            "reach `main`\n"
            if missing_docs else
            f"- [x] This epic's `{doc_names[0]}` and `{doc_names[1]}` are both on `{branch}` "
            "(auto-verified) — `close-epic`'s merge carries them to `main`\n"
        )
        dependents_line = (
            f"- [ ] {len(open_dependents)} still-open issue(s) reference/depend on a closed child of this epic "
            f"({', '.join(f'#{n}' for n in open_dependents)}) — resolve or consciously accept before closing\n"
            if open_dependents else
            "- [x] No open issue elsewhere depends on a closed child of this epic (auto-verified)\n"
        )
        checklist = (
            f"## 🏁 Epic ready to close — all {len(children)} child issue(s) closed\n\n"
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
# Each epic gets its own compose project, ports, env/secrets profile and data dir.
# Isolation is config-only: the driven repo's compose must read the keys in `pipeline.stack`.

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


def _stack_layout(epic: int, profile: Optional[str] = None) -> dict:
    """Every path/name the two stack commands share, derived from config once;
    `profile` overrides the epic's own (a hand-made stack)."""
    s = PIPELINE["stack"]
    profile = profile or stack_profile(epic)
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
    """Host ports for this epic's stack: base ports plus a non-zero stride offset
    derived from the epic number, bumped while any port is bound. Returns {key: port}."""
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
    """The base env text with each `overrides` key replaced in place; missing keys
    are appended under a generated-section marker."""
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
    """Generate the epic's env profile (distinct ports, compose project, data dir),
    copy the base secrets file, then run `upCommand`/`seedCommand`. Idempotent: an
    existing profile is reused; `stack.enabled: false` is a structured no-op."""
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


def _docker_compose_running(project: str) -> list:
    """Ids of running containers carrying compose's project label (none without docker)."""
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter",
                              f"label=com.docker.compose.project={project}"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        return []
    return out.stdout.split()


def cmd_teardown_epic_stack(epic: int, keep_data: bool = False,
                            shell: Shell = _default_shell,
                            project: Optional[str] = None, profile: Optional[str] = None,
                            running_probe: Callable[[str], list] = _docker_compose_running) -> dict:
    """Run `downCommand`, confirm no container of the project still runs, then remove
    the generated env/secrets files and data dir. A failing or no-op down removes
    nothing. `project`/`profile` name a hand-made stack, even with the stack disabled."""
    s = PIPELINE["stack"]
    named = bool(project or profile)
    if not s["enabled"] and not named:
        return {"epic": epic, "torn_down": False,
                "reason": "pipeline.stack.enabled is false — pass --project/--profile "
                          "to tear down a hand-made stack"}
    lay = _stack_layout(epic, profile)
    if project:
        lay["project"] = project
    if not named and not os.path.exists(lay["env_file"]):
        return {"epic": epic, "torn_down": False, "profile": lay["profile"],
                "reason": f"no stack provisioned ({lay['env_file']} absent)"}
    fmt = {"profile": lay["profile"], "project": lay["project"], "envFile": lay["env_file_rel"],
           "secretsFile": os.path.relpath(lay["secrets_file"], lay["workspace_root"]),
           "dataDir": lay["data_dir"], "workspaceRoot": lay["workspace_root"], "n": epic}
    cmd = (s.get("downCommand") or "").format(**fmt)
    if cmd.strip():
        shell(cmd, lay["workspace_root"])
    still_running = running_probe(lay["project"])
    if still_running:
        return {"epic": epic, "torn_down": False, "profile": lay["profile"],
                "project": lay["project"], "down_command": cmd, "still_running": still_running,
                "reason": f"{len(still_running)} container(s) of {lay['project']} still run after "
                          f"the down command; nothing removed (does --env-file resolve from "
                          f"{lay['workspace_root']}?)"}
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


# --- Composite commands: ordered sequences of existing commands ---------------

class StepSequence:
    """Runs a composite's `cmd_*` steps in order, recording each, stopping at the first
    failure: `GhError` or `ok: False` (exit 1), or a `failed` predicate hit (exit 0)."""

    def __init__(self):
        self.steps: dict = {}
        self.completed: list = []
        self.failed_step: Optional[str] = None
        self.ok = True
        self.error: Optional[str] = None

    @property
    def stopped(self) -> bool:
        return self.failed_step is not None

    def run(self, name: str, call: Callable[[], dict],
            failed: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
        """The step's result, or None when it failed or an earlier step had."""
        if self.stopped:
            return None
        try:
            result = call()
        except GhError as e:
            self.steps[name] = {"error": str(e)}
            self.failed_step, self.ok, self.error = name, False, str(e)
            return None
        self.steps[name] = result
        if result.get("ok") is False:
            self.failed_step, self.ok = name, False
        elif failed is not None and failed(result):
            self.failed_step = name
        else:
            self.completed.append(name)
            return result
        return None

    def report(self, failed_key: str = "failed_step", **fields) -> dict:
        out = {**fields, "ok": self.ok, "completed_steps": self.completed,
               failed_key: self.failed_step, "steps": self.steps}
        if self.error:
            out["error"] = self.error
        elif self.stopped:
            failed = self.steps[self.failed_step]
            out["reason"] = (failed.get("reason") or failed.get("misuse")
                             or f"step {self.failed_step} did not succeed -- see steps")
        return out


def _not_a_phased_epic(issues: dict, epic: int) -> Optional[str]:
    """None when `epic` is a non-standing Epic, else the refusal reason."""
    entry = issues.get(epic)
    if entry is None:
        raise GhError(f"issue #{epic} not found in the repo issue list")
    if not is_epic(entry):
        return f"#{epic} is not an Epic -- only a non-standing Epic has phase-Tasks"
    if is_epic_standing(entry):
        return f"#{epic} is a standing epic -- it has no phase-Tasks or epic branch"
    return None


def _child_titled(issues: dict, parent: int, title: str,
                  open_only: bool = False) -> Optional[dict]:
    """`parent`'s child titled `title` -- an open one first, else (unless
    `open_only`) a closed one; lowest number wins a tie."""
    matches = sorted((i for i in issues.values()
                      if (i.get("parent") or {}).get("number") == parent and i["title"] == title
                      and (i["state"] == "OPEN" or not open_only)),
                     key=lambda i: (i["state"] != "OPEN", i["number"]))
    return matches[0] if matches else None


def _ensure_staged_task(seq: StepSequence, gh: GitHub, issues: dict, epic: int, key: str,
                        title: str, body: str, stage: str, open_only: bool = False) -> Optional[dict]:
    """Reuse or create `title` under `epic` and give it `stage` if it has none.
    Returns `{"issue", "open"}`, or None when a step failed."""
    existing = _child_titled(issues, epic, title, open_only=open_only)
    if existing:
        task = seq.run(f"{key}.create-issue", lambda: {
            "issue": existing["number"], "reused": True, "state": existing["state"]})
    else:
        task = seq.run(f"{key}.create-issue", lambda: cmd_create_issue(
            gh, title, body, epic, [], type_name=task_type_name()))
    if task is None:
        return None
    n = task["issue"]
    is_open = existing is None or existing["state"] == "OPEN"
    staged = current_stage(existing) if existing else None
    if not is_open or staged:
        # A closed phase-Task is finished; a staged one may have moved on -- never rewind it.
        done = seq.run(f"{key}.set-stage", lambda: {
            "issue": n, "stage": staged, "unchanged": True,
            **({} if is_open else {"reason": "issue is closed"})})
    else:
        done = seq.run(f"{key}.set-stage", lambda: cmd_set_stage(gh, n, stage))
    return {"issue": n, "open": is_open} if done is not None else None


def _worktree_refused(result: dict) -> bool:
    return bool(result.get("diverged"))


def _add_blocked_by_once(gh: GitHub, issue: int, dep: int) -> dict:
    """`cmd_add_blocked_by` unless the edge already exists (re-adding one errors)."""
    if dep in gh.blocked_by(issue):
        return {"issue": issue, "blocked_on": dep, "added": False, "reason": "edge already present"}
    return cmd_add_blocked_by(gh, issue, dep)


def cmd_cut_phase_tasks(gh: GitHub, epic: int, arch_body: Optional[str] = None,
                        lld_body: Optional[str] = None, repo_path: str = ".",
                        runner: Runner = _default_runner) -> dict:
    """Stand up a non-standing Epic's branch `epic-<n>` and its worktree, create (or reuse
    by title) and stage its Architecture- and LLD-phase Tasks, block LLD on Architecture,
    and add the Architecture worktree off `origin/epic-<n>`. Returns `architecture_task`,
    `lld_task` plus the step record."""
    issues = {i["number"]: i for i in gh.issue_list()}
    refusal = _not_a_phased_epic(issues, epic)
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    seq.run("epic-worktree", lambda: cmd_worktree_add(
        gh, epic, unit="epic", repo_path=repo_path, runner=runner), failed=_worktree_refused)
    arch = None if seq.stopped else _ensure_staged_task(
        seq, gh, issues, epic, "architecture", "Architecture phase",
        arch_body or f"Architecture-phase Task for Epic #{epic}: `architecture` -> "
                     f"`arch-review`; authors `epic-{epic}/architecture.md`, merged into "
                     f"`epic-{epic}` by its design PR.",
        "architecture")
    lld = arch and _ensure_staged_task(
        seq, gh, issues, epic, "lld", "LLD phase",
        lld_body or f"LLD-phase Task for Epic #{epic}: `lld` -> `lld-review`; authors "
                    f"`epic-{epic}/lld.md`, merged into `epic-{epic}` by its design PR, "
                    f"from which the Epic's Tasks are created.",
        "lld")
    if lld:
        a, l = arch["issue"], lld["issue"]
        if arch["open"]:
            seq.run("add-blocked-by", lambda: _add_blocked_by_once(gh, l, a))
            seq.run("worktree-add", lambda: cmd_worktree_add(
                gh, a, repo_path=repo_path, runner=runner), failed=_worktree_refused)
        else:
            seq.run("add-blocked-by", lambda: {"issue": l, "blocked_on": a, "added": False,
                                               "reason": "Architecture-phase Task is closed"})
    return seq.report(epic=epic, architecture_task=arch and arch["issue"],
                      lld_task=lld and lld["issue"])


def cmd_open_arch_revision(gh: GitHub, epic: int, title: str, body: str,
                           blocks: Optional[list] = None, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """Cut an Architecture revision phase-Task under a non-standing Epic: ensure `epic-<n>`
    and its worktree, create (or reuse an open one by title), stage `architecture`,
    worktree off `origin/epic-<n>`, then block each unit in `blocks` on it.
    Returns `revision_task`."""
    if not title.startswith("Architecture revision"):
        title = f"Architecture revision: {title}"
    issues = {i["number"]: i for i in gh.issue_list()}
    refusal = _not_a_phased_epic(issues, epic)
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    seq.run("epic-worktree", lambda: cmd_worktree_add(
        gh, epic, unit="epic", repo_path=repo_path, runner=runner), failed=_worktree_refused)
    task = None if seq.stopped else _ensure_staged_task(
        seq, gh, issues, epic, "revision", title, body, "architecture", open_only=True)
    if task:
        seq.run("worktree-add", lambda: cmd_worktree_add(
            gh, task["issue"], repo_path=repo_path, runner=runner), failed=_worktree_refused)
    for unit in blocks or []:
        seq.run(f"add-blocked-by:{unit}",
                lambda unit=unit: _add_blocked_by_once(gh, unit, task["issue"]))
    return seq.report(epic=epic, revision_task=task and task["issue"])


def cmd_file_closing_delta(gh: GitHub, epic: int, title: str, body: str,
                           priority: Optional[str] = None, effort: Optional[str] = None,
                           start: bool = False, repo_path: str = ".",
                           runner: Runner = _default_runner) -> dict:
    """File a closing-run finding as a `Bug` child of a non-standing Epic. It stays `unstaged`
    (full lane) unless `start`, the operator-authorised close-blocker lane: then stage
    `development` and `start-stage` it. Returns `delta_issue`."""
    refusal = _not_a_phased_epic({i["number"]: i for i in gh.issue_list()}, epic)
    if refusal:
        return {"epic": epic, "refused": True, "reason": refusal}
    seq = StepSequence()
    created = seq.run("create-issue", lambda: cmd_create_issue(
        gh, title, body, epic, [], "Bug", priority, effort))
    number = (created or seq.steps.get("create-issue") or {}).get("issue")
    if created is None and number is not None and seq.failed_step == "create-issue":
        # create-issue left a half-made issue (a mid-create failure). Finish it in place with the
        # requested Priority/Effort rather than re-running create-issue, which would duplicate it.
        seq.failed_step, seq.ok, seq.error = None, True, None
        seq.completed.append("create-issue")
        created = seq.run("repair-issue", lambda: cmd_repair_issue(
            gh, number, parent=epic, type_name="Bug", priority=priority, effort=effort))
    if start and created:
        seq.run("set-stage", lambda: cmd_set_stage(gh, number, "development"))
        seq.run("start-stage", lambda: cmd_start_stage(gh, number, "development", "issue",
                                                       repo_path, runner=runner))
    return seq.report(epic=epic, delta_issue=number)


def cmd_finish_lld(gh: GitHub, lld_task: int, epic: int, repo_path: str = ".",
                   runner: Runner = _default_runner) -> dict:
    """After a clean `lld-review`: merge-design-pr (lld.md into `epic-<n>`) ->
    create-lld-tasks -> merge-lld-doc -> close-issue, stopping before the close if anything
    earlier failed. Returns `completed_steps`, `failed_step`, `steps`."""
    parent = (gh.issue_epic_info(lld_task).get("parent") or {}).get("number")
    if parent != epic:
        return {"lld_task": lld_task, "epic": epic, "refused": True,
                "reason": f"#{lld_task}'s parent is #{parent}, not Epic #{epic}"}
    seq = StepSequence()
    # Already merged (a re-run) or a numbered lld.md the merge would revert both count as done.
    seq.run("merge-design-pr", lambda: _merge_design_pr_of(gh, lld_task, repo_path, runner),
            failed=lambda r: not (r.get("merged") or r.get("already_merged")
                                  or r.get("up_to_date")))
    # Nothing to create (`tasks == {}`) is a completed earlier run, not a failure.
    seq.run("create-lld-tasks", lambda: cmd_create_lld_tasks(gh, epic, repo_path,
                                                             runner=runner),
            failed=lambda r: not r.get("pushed") and r.get("tasks") != {})
    seq.run("merge-lld-doc", lambda: cmd_merge_lld_doc(gh, repo_path, epic, runner=runner),
            failed=lambda r: not r.get("verified_on_origin"))
    closed = seq.run("close-issue", lambda: cmd_close_issue(gh, lld_task, repo_path=repo_path,
                                                            runner=runner))
    return seq.report(lld_task=lld_task, epic=epic, closed=bool(closed))


def cmd_start_stage(gh: GitHub, number: int, role: str, unit: str = "issue",
                    repo_path: str = ".", base: Optional[str] = None,
                    runner: Runner = _default_runner) -> dict:
    """Refusal check, `worktree-add`, then `claim` -- worktree first, so the unit is never
    claimed without a live tree, and nothing runs when the claim would be refused.
    Returns `path`, `claimed` plus the steps."""
    role = RETIRED_ROLES.get(role, role)
    seq = StepSequence()
    seq.run("check-claimable", lambda: _check_claimable(gh, number, role))
    # A child of a non-standing Epic integrates into `epic-<parent>`. Ensure that branch AND its
    # worktree exist first (idempotently, exactly as cut-phase-tasks does) so `next-action` never
    # delegates a child onto a base branch that isn't there yet -- a live worktree is fast-forwarded.
    if unit == "issue" and not seq.stopped:
        integ = integration_base(gh, number)
        if integ != "main":
            epic_no = int(integ[len(EPIC_BRANCH_PREFIX):])
            seq.run("epic-worktree", lambda: cmd_worktree_add(
                gh, epic_no, unit="epic", repo_path=repo_path, runner=runner),
                failed=_worktree_refused)
    worktree = seq.run("worktree-add", lambda: cmd_worktree_add(
        gh, number, unit, repo_path, runner=runner, base=base), failed=_worktree_refused)
    claim = seq.run("claim", lambda: cmd_claim(gh, number, role))
    return seq.report(issue=number, role=role, path=(worktree or {}).get("path"),
                      claimed=bool(claim))


# A review role with no Stage value of its own is verified against the stage it follows.
_REVIEW_FOLLOWS_STAGE = {"product-review": "product", "arch-review": "architecture",
                         "lld-review": "lld"}


def cmd_transition(gh: GitHub, issue: int, expect_stage: str, pr: Optional[int] = None,
                   repo_path: Optional[str] = None, base: Optional[str] = None,
                   runner: Runner = _default_runner) -> dict:
    """After a stage agent returns: verify-exit -> sync-branch -> open-design-pr (a
    non-standing Epic's architecture/lld phase-Task only) -> start-comment (review roles only).
    Stops at an unverified exit, a missing pr-review handoff marker, or a sync conflict /
    missing base. Returns `ready`, `stopped_at`, `steps`."""
    if expect_stage == "pr-review" and pr is None:
        raise GhError("transition --expect-stage pr-review needs --pr: the handoff-marker "
                      "check only runs with the PR in hand")
    verify_stage = _REVIEW_FOLLOWS_STAGE.get(expect_stage, expect_stage)
    seq = StepSequence()
    # An unverified exit (missing handoff marker included) is `ok: False`: exit 1.
    seq.run("verify-exit", lambda: cmd_verify_exit(gh, repo_path, issue, verify_stage, pr,
                                                   runner=runner))
    seq.run("sync-branch", lambda: cmd_sync_branch(gh, repo_path, issue, runner=runner,
                                                   base=base),
            failed=lambda r: not r.get("synced"))
    if not seq.stopped and design_phase_epic(gh, issue, verify_stage) is not None:
        seq.run("open-design-pr", lambda: cmd_open_design_pr(gh, issue, runner=runner),
                failed=lambda r: not r.get("pr"))
    if expect_stage in REVIEW_ROLES:
        seq.run("start-comment", lambda: cmd_start_comment(gh, issue, expect_stage))
    return seq.report(failed_key="stopped_at", issue=issue, expect_stage=expect_stage,
                      verified_stage=verify_stage, ready=not seq.stopped)


# references/stage-playbooks.md, "Comment size is a contract": command -> (flag, cap).
HANDOFF_CAP, EVIDENCE_CAP = 2_000, 6_000
COMMENT_CAPS = {
    "handoff-to-pr-review": ("summary", EVIDENCE_CAP),
    "record-pr-review": ("summary", EVIDENCE_CAP),
    "record-design-review": ("summary", EVIDENCE_CAP),
    "open-dev-pr": ("summary", HANDOFF_CAP),
    "open-gate": ("summary", HANDOFF_CAP),
    "skip-gate": ("summary", HANDOFF_CAP),
    "waive-gate": ("summary", HANDOFF_CAP),
    "record-epic-verification": ("summary", HANDOFF_CAP),
    "record-initiative-verification": ("summary", HANDOFF_CAP),
    "mark-needs-human": ("reason", HANDOFF_CAP),
    "resolve-thread": ("reply", HANDOFF_CAP),
    "route": ("reason", ROUTE_REASON_CAP),
    "close-issue": ("reason", HANDOFF_CAP),
    "comment": ("body", HANDOFF_CAP),
    "detach-epic": ("reason", HANDOFF_CAP),
}


def comment_cap_refusal(args) -> Optional[dict]:
    """A refusal when the command's comment text is over its cap, else None."""
    flag, cap = COMMENT_CAPS.get(args.command, (None, 0))
    text = getattr(args, flag, None) if flag else None
    if not text or len(text) <= cap:
        return None
    return {"refused": True,
            "reason": f"--{flag} is {len(text):,} chars, over the {cap:,}-char cap for this "
                      f"comment (references/stage-playbooks.md, \"Comment size is a "
                      f"contract\"); trim it and re-run"}


def _open_gate_cli(a) -> dict:
    gh = get_work_item_provider()
    return cmd_open_gate(gh, a.repo_path, a.issue, phase_gate_title(gh, a.issue, a.title),
                         a.doc, a.next_stage, a.summary)


def main(argv: Optional[list] = None) -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        source = f"tokenEnv (${TOKEN_ENV}) or " if TOKEN_ENV else ""
        print(json.dumps({"error": "GITHUB_TOKEN not set — the sdlc plugin's SessionStart hook "
                                    f"exports it from {source}tokenPath ({TOKEN_PATH}); start a new "
                                    "session in the driven repo, or prefix the call with "
                                    f"GITHUB_TOKEN=$(cat {TOKEN_PATH})"}))
        return 1
    parser = argparse.ArgumentParser(prog="sdlc_next.py")
    repo_path_help = "Any path inside the repository; never used as the git-write target"
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("next-action")
    p.add_argument("epic", type=int, help="The epic to drive")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the dev lane's slot holders")
    p.add_argument("--run-id", default=None,
                    help="This run's id; enables the maxTasksPerRun cap, shared across an "
                         "Initiative's Epics (omit = no cap)")
    p.add_argument("--skip-epic", type=int, action="append", default=[],
                    help="Initiative only: park this Epic for this run (repeatable), e.g. one "
                         "waiting on a human, so the loop moves to the next runnable Epic")
    p.set_defaults(func=lambda a: cmd_next_action(get_work_item_provider(), a))
    p = sub.add_parser("list-ready-for-review",
                        help="Children whose handed-off draft PR awaits review this round")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Max PRs to return (default {PR_REVIEW_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_ready_for_review(get_work_item_provider(), a.epic, a.limit))
    p = sub.add_parser("list-parallel-ready",
                        help="Dev-lane children safe to start/resume concurrently")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent children (default {DEV_LANE_PARALLELISM})")
    p.add_argument("--run-id", default=None,
                    help="This run's id; enables the maxTasksPerRun cap (omit = no cap)")
    p.set_defaults(func=lambda a: cmd_list_parallel_ready(
        get_work_item_provider(), a.repo_path, a.epic, a.limit, run_id=a.run_id))
    p = sub.add_parser("lld-section",
                        help="Print one Task's subsection of its Epic's lld.md")
    p.add_argument("--epic", type=int, required=True, help="The Epic")
    p.add_argument("--task", required=True,
                    help="Task issue number, or its design-time key before renumbering")
    p.add_argument("--repo-path", default=".",
                    help="Repo with an origin/epic-<n> ref")
    p.set_defaults(func=lambda a: cmd_lld_section(a.repo_path, a.epic, a.task))
    p = sub.add_parser("list-design-ready",
                        help="A standing epic's product/architecture children safe to run concurrently")
    p.add_argument("epic", type=int, help="The epic")
    p.add_argument("--repo-path", default=".",
                    help="Repo whose `git worktree list` gives the live active-branch count")
    p.add_argument("--limit", type=int, default=None,
                    help=f"Total concurrent design children (default {DESIGN_LANE_PARALLELISM})")
    p.set_defaults(func=lambda a: cmd_list_design_ready(get_work_item_provider(), a.repo_path, a.epic, a.limit))
    p = sub.add_parser("handoff-to-pr-review",
                        help="Post the development->pr-review handoff marker")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--summary", required=True,
                    help="One sentence: what was built and tested")
    p.set_defaults(func=lambda a: cmd_handoff_to_pr_review(get_work_item_provider(), a.issue, a.pr, a.summary))
    p = sub.add_parser("record-pr-review",
                        help="Record a pr-review outcome on the issue")
    p.add_argument("issue", type=int)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.add_argument("--same-class-recurrence", action="store_true",
                    help="Finding repeats an earlier round's defect class (escalation signal)")
    p.set_defaults(func=lambda a: cmd_record_pr_review(
        get_work_item_provider(), a.issue, a.pr, a.outcome, a.summary, a.same_class_recurrence))
    p = sub.add_parser("record-local-ci",
                        help="Attest a main-only suite passed locally at a commit")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--suite", required=True, choices=list(LOCAL_CI_SUITES))
    p.add_argument("--sha", required=True,
                    help="Commit tested; honoured only while it is the PR head")
    p.add_argument("--command", required=True,
                    help="The exact command run")
    p.add_argument("--output", required=True,
                    help="File with the run's captured output (tail is embedded)")
    p.set_defaults(func=lambda a: cmd_record_local_ci(get_work_item_provider(), a.pr, a.suite, a.sha,
                                                       a.command, a.output))
    p = sub.add_parser("record-design-review",
                        help="Record an arch-review/lld-review outcome on the unit")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True, choices=list(DESIGN_REVIEW_ROLES))
    p.add_argument("--outcome", required=True, choices=list(PR_REVIEW_OUTCOMES))
    p.add_argument("--summary", required=True,
                    help="One sentence: what the review checked and concluded")
    p.add_argument("--same-class-recurrence", action="store_true",
                    help="Finding repeats an earlier round's defect class (escalation signal)")
    p.set_defaults(func=lambda a: cmd_record_design_review(
        get_work_item_provider(), a.issue, a.role, a.outcome, a.summary, a.same_class_recurrence))
    p = sub.add_parser("post-comment",
                        help="Post a product/architecture/lld stage's handoff comment from a file")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True, choices=list(POST_COMMENT_ROLES))
    p.add_argument("--body-file", required=True,
                    help="File holding the comment text (<= 2,000 chars)")
    p.set_defaults(func=lambda a: cmd_post_comment(
        get_work_item_provider(), a.issue, a.role, a.body_file))
    p = sub.add_parser("check-gate")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_check_gate(get_work_item_provider(), a))
    p = sub.add_parser("claim")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True)
    p.set_defaults(func=lambda a: cmd_claim(get_work_item_provider(), a.issue, a.role))
    p = sub.add_parser("set-stage",
                        help="Set the Stage field only (no status write, no comment)")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True)
    p.set_defaults(func=lambda a: cmd_set_stage(get_work_item_provider(), a.issue, a.stage))
    p = sub.add_parser("route", help="Route a standing epic's child ahead to a later stage")
    p.add_argument("issue", type=int)
    p.add_argument("--to", required=True, help=f"One of {', '.join(ROUTE_TARGETS)}")
    p.add_argument("--reason", required=True, help=f"One line, <= {ROUTE_REASON_CAP} chars")
    p.set_defaults(func=lambda a: cmd_route(get_work_item_provider(), a.issue, a.to, a.reason))
    p = sub.add_parser("start-comment")
    p.add_argument("issue", type=int)
    p.add_argument("--role", required=True,
                   choices=sorted(REVIEW_ROLES))
    p.set_defaults(func=lambda a: cmd_start_comment(get_work_item_provider(), a.issue, a.role))
    p = sub.add_parser("open-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--title", required=True)
    p.add_argument("--doc", required=True, choices=["product.md", "architecture.md"])
    p.add_argument("--next-stage", required=True)
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: _open_gate_cli(a))
    p = sub.add_parser("pass-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--gate-pr", type=int, required=True)
    p.add_argument("--stage", required=True, choices=["product", "architecture"])
    p.set_defaults(func=lambda a: cmd_pass_gate(get_work_item_provider(), a.repo_path, a.issue, a.gate_pr, a.stage))
    p = sub.add_parser("skip-gate")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True, choices=["architecture"])
    p.add_argument("--confidence", type=int, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (an Epic's Architecture-phase Task only)")
    p.set_defaults(func=lambda a: cmd_skip_gate(get_work_item_provider(), a.issue, a.stage, a.confidence, a.summary, repo_path=a.repo_path))
    p = sub.add_parser("waive-gate",
                        help="Pass a gate without a human when the profile waives it")
    p.add_argument("issue", type=int)
    p.add_argument("--stage", required=True, choices=sorted(HUMAN_GATE_TOGGLES))
    p.add_argument("--summary", required=True)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (phase-Tasks only)")
    p.set_defaults(func=lambda a: cmd_waive_gate(get_work_item_provider(), a.issue, a.stage,
                                                 a.summary, repo_path=a.repo_path))
    p = sub.add_parser("merge-gate",
                        help="Merge an open gate PR at the operator's explicit instruction")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--stage", required=True, choices=sorted(HUMAN_GATE_TOGGLES))
    p.add_argument("--operator-confirmed", action="store_true",
                    help="The operator explicitly told you to merge this gate PR")
    p.set_defaults(func=lambda a: cmd_merge_gate(get_work_item_provider(), a.pr, a.issue,
                                                 a.stage, a.operator_confirmed))
    p = sub.add_parser("auto-pass-gate")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_auto_pass_gate(get_work_item_provider(), a.repo_path, a.pr))
    p = sub.add_parser("mark-feedback-received")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--author", required=True, help="Comment/review author login")
    p.add_argument("--body", default="", help="Comment/review body (empty = skip)")
    p.set_defaults(func=lambda a: cmd_mark_feedback_received(get_work_item_provider(), a.pr, a.author, a.body))
    p = sub.add_parser("resolve-thread", help="Reply to and resolve one PR review thread")
    p.add_argument("--thread-id", required=True)
    p.add_argument("--reply", default=None)
    p.set_defaults(func=lambda a: cmd_resolve_thread(get_work_item_provider(), a.thread_id, a.reply))
    p = sub.add_parser("mark-feedback-addressed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_feedback_addressed(get_work_item_provider(), a.issue))
    p = sub.add_parser("mark-todo")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_todo(get_work_item_provider(), a.issue))
    p = sub.add_parser("mark-issue-closed")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_mark_issue_closed(get_work_item_provider(), a.issue))
    p = sub.add_parser("close-issue",
                        help="Close the issue, set terminal fields, release its worktree")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map)")
    p.add_argument("--not-planned", action="store_true",
                    help="Close with state reason NOT_PLANNED (a dropped unit), commenting --reason")
    p.add_argument("--reason", default=None,
                    help="Why it is not planned (one paragraph; with --not-planned only)")
    p.set_defaults(func=lambda a: cmd_close_issue(get_work_item_provider(), a.issue,
                                                  repo_path=a.repo_path,
                                                  not_planned=a.not_planned, reason=a.reason))
    p = sub.add_parser("pause-for-epic-regate")
    p.add_argument("issue", type=int)
    p.add_argument("--epic", type=int, required=True)
    p.add_argument("--gate-pr", type=int, default=None,
                    help="The revision's design/gate PR, once it exists")
    p.add_argument("--found-by", default="lld",
                    help="Stage that hit the deviation (lld, development, pr-review, ...)")
    p.set_defaults(func=lambda a: cmd_pause_for_epic_regate(get_work_item_provider(), a.issue, a.epic, a.gate_pr, a.found_by))
    p = sub.add_parser("verify-exit")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--expect-stage", required=True)
    p.add_argument("--pr", type=int, default=None)
    p.set_defaults(func=lambda a: cmd_verify_exit(
        get_work_item_provider(), a.repo_path, a.issue, a.expect_stage, a.pr))
    p = sub.add_parser("cite",
                        help="Emit a citation block read from the real file")
    p.add_argument("path")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--line", type=int)
    target.add_argument("--lines", help="Inclusive range, e.g. 12-18")
    target.add_argument("--match", help="Literal substring matching exactly one line")
    p.add_argument("--rev", default=None, help="Read at this revision")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_cite(a.path, line=a.line, lines=a.lines,
                                            match=a.match, rev=a.rev,
                                            repo_path=a.repo_path))
    p = sub.add_parser("verify-citations",
                        help="Re-resolve every citation in the given documents")
    p.add_argument("doc", nargs="+")
    p.add_argument("--repo-path", default=".")
    p.set_defaults(func=lambda a: cmd_verify_citations(a.doc, repo_path=a.repo_path))
    p = sub.add_parser("worktree-add",
                        help="Create or resume the unit's worktree")
    p.add_argument("number", type=int)
    p.add_argument("--unit", choices=["issue", "epic"], default="issue")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base (e.g. origin/main)")
    p.set_defaults(func=lambda a: cmd_worktree_add(
        get_work_item_provider(), a.number, a.unit, a.repo_path, base=a.base))

    p = sub.add_parser("sync-branch")
    p.add_argument("issue", type=int)
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.add_argument("--unit", default="issue", choices=["issue", "epic"])
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base")
    p.set_defaults(func=lambda a: cmd_sync_branch(get_work_item_provider(), a.repo_path,
                                                   a.issue, a.unit, base=a.base))
    p = sub.add_parser("merge-lld-doc",
                        help="Verify lld.md is merged into the epic branch, stage its Tasks, mark the Epic architected")
    p.add_argument("epic", type=int, help="The Epic")
    p.add_argument("--repo-path", default=None,
                    help=repo_path_help)
    p.set_defaults(func=lambda a: cmd_merge_lld_doc(get_work_item_provider(), a.repo_path, a.epic))
    p = sub.add_parser("open-design-pr",
                        help="Raise a phase-Task's design PR issue-<n> -> epic-<e> (transition runs it)")
    p.add_argument("issue", type=int, help="The architecture/lld phase-Task")
    p.set_defaults(func=lambda a: cmd_open_design_pr(get_work_item_provider(), a.issue))
    p = sub.add_parser("merge-design-pr",
                        help="Merge a phase-Task's design PR into its epic branch once its review is clean")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True, help="The phase-Task")
    p.add_argument("--repo-path", default=".", help="Any path inside the repository")
    p.set_defaults(func=lambda a: cmd_merge_design_pr(get_work_item_provider(), a.pr, a.issue,
                                                       a.repo_path))
    p = sub.add_parser("add-blocked-by",
                        help="Add a native blockedBy edge (no other side effect)")
    p.add_argument("issue", type=int)
    p.add_argument("--on", type=int, required=True, dest="dep")
    p.set_defaults(func=lambda a: cmd_add_blocked_by(get_work_item_provider(), a.issue, a.dep))
    p = sub.add_parser("create-lld-tasks",
                        help="Create one Task per lld.md Task section and renumber the doc (idempotent)")
    p.add_argument("epic", type=int, help="The Epic")
    p.add_argument("--repo-path", default=".",
                    help=repo_path_help)
    p.set_defaults(func=lambda a: cmd_create_lld_tasks(get_work_item_provider(), a.epic,
                                                        a.repo_path))
    p = sub.add_parser("open-dev-pr")
    p.add_argument("issue", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--allow-empty", action="store_true",
                    help="Verify-only Task: land an empty commit when the branch has no "
                         "commits ahead of its base, so the PR can open")
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the empty commit's worktree map)")
    p.set_defaults(func=lambda a: cmd_open_dev_pr(get_work_item_provider(), a.issue, a.title,
                                                  a.body, a.summary, allow_empty=a.allow_empty,
                                                  repo_path=a.repo_path))
    p = sub.add_parser("pr-checks")
    p.add_argument("pr", type=int)
    p.set_defaults(func=lambda a: cmd_pr_checks(get_work_item_provider(), a.pr))
    p = sub.add_parser("merge-pr")
    p.add_argument("pr", type=int)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the branch's worktree)")
    p.add_argument("--run-id", default=None,
                    help="Book the terminal-unit count under this run id (else the epic's "
                         "run-state file's current id)")
    p.set_defaults(func=lambda a: cmd_merge_pr(get_work_item_provider(), a.pr, a.issue,
                                               a.repo_path, run_id=a.run_id))
    p = sub.add_parser("create-issue")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--parent", type=int, required=True,
                    help="Parent issue (Epic for a Task, Initiative for an Epic)")
    p.add_argument("--label", action="append", default=[], dest="labels")
    p.add_argument("--type", default="Task", dest="type_name",
                    help="Native Issue Type, mandatory on every issue (default Task)")
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None, help="Default: pipeline.issueDefaults.effort")
    p.add_argument("--blocked-by", type=int, action="append", default=[], dest="blocked_by",
                    help="Native blockedBy edge on this open issue (repeatable); orders "
                         "an Initiative's Epics")
    p.set_defaults(func=lambda a: cmd_create_issue(get_work_item_provider(), a.title, a.body,
                                                    a.parent, a.labels, a.type_name,
                                                    a.priority, a.effort, a.blocked_by))
    p = sub.add_parser("mark-blocked")
    p.add_argument("issue", type=int)
    p.add_argument("--dep", type=int, required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the unit's worktree)")
    p.set_defaults(func=lambda a: cmd_mark_blocked(get_work_item_provider(), a.issue, a.dep, a.repo_path))
    p = sub.add_parser("mark-needs-human")
    p.add_argument("issue", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--repo-path", default=".",
                    help="Repo root (to release the unit's worktree)")
    p.set_defaults(func=lambda a: cmd_mark_needs_human(get_work_item_provider(), a.issue, a.reason, a.repo_path))
    p = sub.add_parser("show-config",
                        help="Print the effective config and the running plugin version")
    p.set_defaults(func=lambda a: cmd_show_config())

    p = sub.add_parser("pairing-counts",
                        help="Escalation-valve bounce counts for one issue")
    p.add_argument("issue", type=int)
    p.set_defaults(func=lambda a: cmd_pairing_counts(get_work_item_provider(), a.issue))
    p = sub.add_parser("list-needs-human")
    p.set_defaults(func=lambda a: cmd_list_needs_human(get_work_item_provider()))
    p = sub.add_parser("audit-issues",
                        help="Open pipeline issues missing issueType/parent/Priority/Effort/Status, and Epics with no phase-Tasks")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--epic", type=int, default=None, help="Only this Epic's subtree (the whole repo when omitted)")
    scope.add_argument("--initiative", type=int, default=None,
                       help="Only this Initiative's tree: its Epics and their children")
    p.set_defaults(func=lambda a: cmd_audit_issues(get_work_item_provider(),
                                                    a.epic if a.epic is not None else a.initiative))
    p = sub.add_parser("repair-issue",
                        help="Set an existing issue's missing parent/type/status/Priority/Effort")
    p.add_argument("issue", type=int)
    p.add_argument("--parent", type=int, default=None, help="Linked only when none is set")
    p.add_argument("--type", default=None, dest="type_name",
                    help="Default: inferred from the classification label")
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None, help="Default: pipeline.issueDefaults.effort")
    p.set_defaults(func=lambda a: cmd_repair_issue(get_work_item_provider(), a.issue, a.parent,
                                                    a.type_name, a.priority, a.effort))
    p = sub.add_parser("close-epic",
                        help="Reconcile and merge the epic branch once verified")
    p.add_argument("epic", type=int)
    p.add_argument("--repo-path", default=".")
    p.add_argument("--no-carry-forward", action="store_false", dest="carry_forward",
                    help="Accept only evidence stamped at the epic head itself: no "
                         "carry-forward over a safe delta, no per-child attestation chain")
    p.set_defaults(func=lambda a: cmd_close_epic(get_work_item_provider(), a.epic, a.repo_path,
                                                  carry_forward=a.carry_forward))
    p = sub.add_parser("record-epic-verification",
                        help="Record one half of an epic's closing verification")
    p.add_argument("epic", type=int)
    p.add_argument("--kind", required=True, choices=["e2e", "exploratory"])
    p.add_argument("--summary", required=True)
    p.add_argument("--sha", default=None,
                    help="Epic-branch head that was tested (default: the branch head now)")
    p.set_defaults(func=lambda a: cmd_record_epic_verification(
        get_work_item_provider(), a.epic, a.kind, a.summary, a.sha))
    p = sub.add_parser("check-epics-closeable")
    p.set_defaults(func=lambda a: cmd_check_epics_closeable(get_work_item_provider()))
    p = sub.add_parser("close-initiative",
                        help="Close the Initiative once its Epics are closed and verified")
    p.add_argument("initiative", type=int)
    p.set_defaults(func=lambda a: cmd_close_initiative(get_work_item_provider(), a.initiative))
    p = sub.add_parser("record-initiative-verification",
                        help="Record an Initiative's closing verification and its outcome")
    p.add_argument("initiative", type=int)
    p.add_argument("--outcome", required=True, choices=list(INITIATIVE_VERIFICATION_OUTCOMES),
                   help="met = every requirement validated; unmet blocks close-initiative")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=lambda a: cmd_record_initiative_verification(
        get_work_item_provider(), a.initiative, a.summary, a.outcome))
    p = sub.add_parser("check-initiative-closeable",
                        help="Whether every Epic of the Initiative is closed")
    p.add_argument("initiative", type=int)
    p.set_defaults(func=lambda a: cmd_check_initiative_closeable(get_work_item_provider(), a.initiative))
    p = sub.add_parser("provision-epic-stack",
                        help="Stand up the epic's isolated runtime stack")
    p.add_argument("epic", type=int)
    p.add_argument("--no-up", action="store_true",
                    help="Generate the profile only")
    p.set_defaults(func=lambda a: cmd_provision_epic_stack(a.epic, up=not a.no_up))
    p = sub.add_parser("teardown-epic-stack",
                        help="Tear down the epic's stack and its generated files")
    p.add_argument("epic", type=int)
    p.add_argument("--keep-data", action="store_true", help="Leave the DB data dir in place")
    p.add_argument("--project", default=None,
                    help="Compose project of a hand-made stack (works with the stack disabled)")
    p.add_argument("--profile", default=None,
                    help="Profile of a hand-made stack (works with the stack disabled)")
    p.set_defaults(func=lambda a: cmd_teardown_epic_stack(a.epic, keep_data=a.keep_data,
                                                          project=a.project, profile=a.profile))
    # --- composites ---
    p = sub.add_parser("cut-phase-tasks",
                        help="Create/reuse an Epic's Architecture and LLD phase-Tasks")
    p.add_argument("epic", type=int)
    p.add_argument("--arch-body", default=None)
    p.add_argument("--lld-body", default=None)
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_cut_phase_tasks(
        get_work_item_provider(), a.epic, a.arch_body, a.lld_body, a.repo_path))
    p = sub.add_parser("finish-lld",
                        help="merge-design-pr -> create-lld-tasks -> merge-lld-doc -> close-issue")
    p.add_argument("lld_task", type=int, help="The LLD-phase Task")
    p.add_argument("--epic", type=int, required=True)
    p.add_argument("--repo-path", required=True, help="Any path inside the repository")
    p.set_defaults(func=lambda a: cmd_finish_lld(
        get_work_item_provider(), a.lld_task, a.epic, a.repo_path))
    p = sub.add_parser("open-arch-revision",
                        help="Cut an Architecture revision Task and block --blocks on it")
    p.add_argument("epic", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--blocks", type=int, nargs="*", default=[],
                    help="Units that must wait for the revision")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_open_arch_revision(
        get_work_item_provider(), a.epic, a.title, a.body, a.blocks, a.repo_path))
    p = sub.add_parser("file-closing-delta",
                        help="File a closing-run finding as a Bug child of the Epic")
    p.add_argument("epic", type=int)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--priority", default=None, help="Default: pipeline.issueDefaults.priority")
    p.add_argument("--effort", default=None,
                    help="High | Medium | Low (default: pipeline.issueDefaults.effort)")
    p.add_argument("--start", action="store_true",
                    help="Close-blocker lane (operator-authorised): stage development and start it")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_file_closing_delta(
        get_work_item_provider(), a.epic, a.title, a.body, a.priority, a.effort, a.start,
        a.repo_path))
    p = sub.add_parser("start-stage", help="worktree-add, then claim")
    p.add_argument("number", type=int)
    p.add_argument("--role", required=True, choices=sorted({*STAGE_OPTION_IDS, *RETIRED_ROLES}))
    p.add_argument("--unit", choices=["issue", "epic"], default="issue")
    p.add_argument("--base", default=None,
                    help="Override the auto-detected integration base (e.g. origin/main)")
    p.add_argument("--repo-path", default=".", help="The shared main checkout")
    p.set_defaults(func=lambda a: cmd_start_stage(
        get_work_item_provider(), a.number, a.role, a.unit, a.repo_path, a.base))
    p = sub.add_parser("transition",
                        help="verify-exit -> sync-branch -> start-comment (review roles)")
    p.add_argument("issue", type=int)
    p.add_argument("--expect-stage", required=True,
                    help="Stage just finished, or review role about to start")
    p.add_argument("--pr", type=int, default=None, help="Required with --expect-stage pr-review")
    p.add_argument("--repo-path", default=None,
                    help="Any path inside the repository (base for the worktree map)")
    p.add_argument("--base", default=None,
                    help="Override sync-branch's auto-detected integration base")
    p.set_defaults(func=lambda a: cmd_transition(
        get_work_item_provider(), a.issue, a.expect_stage, a.pr, a.repo_path, a.base))
    # --- operator control-plane ---
    p = sub.add_parser("detach-epic",
                        help="Take an Epic out of its Initiative (sub-issue link + sibling blockedBy edges)")
    p.add_argument("epic", type=int)
    p.add_argument("--reason", default=None, help="Why, for the comments on both issues")
    p.set_defaults(func=lambda a: cmd_detach_epic(get_work_item_provider(), a.epic, a.reason))
    p = sub.add_parser("comment", help="Post a plain comment on an issue (no marker)")
    p.add_argument("issue", type=int)
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("--body", default=None, help=f"The comment text (<= {HANDOFF_CAP:,} chars)")
    text.add_argument("--body-file", default=None, help="File holding the comment text")
    p.set_defaults(func=lambda a: cmd_comment(get_work_item_provider(), a.issue, a.body,
                                              a.body_file))
    args = parser.parse_args(argv)
    refusal = comment_cap_refusal(args)
    if refusal:
        print(json.dumps(refusal))
        return 0
    try:
        result = args.func(args)
        print(json.dumps(result))
        # Commands that report their own `ok: False` (auto-pass-gate, composites) exit 1.
        return 0 if result.get("ok", True) else 1
    except GhError as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
