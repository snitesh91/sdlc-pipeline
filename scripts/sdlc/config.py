"""Repo config, pipeline defaults and the constants derived from them."""
from __future__ import annotations

import json
import os
from typing import Callable, Optional


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


def doc_root_rules() -> list:
    """The config's `docRoots` entries (read at call time), [] when unset."""
    return [r for r in (CONFIG.get("docRoots") or []) if isinstance(r, dict) and r.get("docRoot")]


def all_doc_roots() -> list:
    """The top-level `docRoot` then every `docRoots` entry's, slash-stripped and deduped."""
    roots = []
    for root in [DOC_ROOT, *(r["docRoot"] for r in doc_root_rules())]:
        root = root.rstrip("/")
        if root not in roots:
            roots.append(root)
    return roots


TOKEN_PATH = CONFIG.get("tokenPath")  # optional: a config may name only tokenEnv


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


# Concurrent claude.ai cloud sessions an Initiative run launches for its Epics.
CLOUD_SESSIONS = _PARALLELISM.get("cloudSessions", 3)


HUMAN_ASSIGNEE = CONFIG["humanAssignee"]


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


with open(os.path.join(PLUGIN_ROOT, "hooks", "model_policy.json")) as _f:
    _MODEL_POLICY = json.load(_f)


# Everything below is tunable per repo via the optional `pipeline` block in the
# config; every key has the default shown here so an older config keeps working.
_PIPELINE_DEFAULTS = {
    # `retro`: an issue carrying it (the retro backlog) is never pipeline work.
    "labels": {"standing": "epic:standing", "legacy": "epic:legacy",
               "architected": "epic:architected", "gate": "sdlc:gate", "retro": "sdlc:retro"},
    # `initiativePrefix`: the branch of an Initiative whose `initiativeProfiles` entry sets
    # `branch: true` (its Epics integrate there, not into `main`).
    "branches": {"issuePrefix": "issue-", "epicPrefix": "epic-",
                 "initiativePrefix": "initiative-"},
    # `ephemeralPrefix`: throwaway tree for a branch no live worktree holds, since
    # the main checkout is never a git-write target.
    # `releaseCommand`: shell command run inside a worktree right before it is removed
    # (tear down per-worktree resources); failure is reported, never fatal. "" = none.
    # `dockerCleanup`: on unit cleanup, remove docker containers/volumes/networks named
    # `<unit_docker_prefix>-*` / `_*`, and fall back to a docker `rm -rf` when a
    # root-owned file blocks `worktree remove`.
    "worktrees": {"root": "/tmp", "devPrefix": "sdlc-dev-", "epicPrefix": "sdlc-epic-",
                  "initiativePrefix": "sdlc-initiative-", "reviewPrefix": "sdlc-review-", "ephemeralPrefix": "sdlc-tmp-",
                  "releaseCommand": "", "dockerCleanup": True},
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
    # An Epic (or its Initiative) carrying `cloudLabel` is driven only by a cloud session,
    # every other one only by a local session.
    # `initiativeEpics`: "cloud" makes a laptop Initiative run launch each runnable Epic in its
    # own cloud session (`launch-cloud-epic`) instead of driving it; `stallMinutes`: a live
    # cloud Epic with no GitHub activity this long is `stalled` in `cloud-status`;
    # `askingStallFactor`: a session whose heartbeat says `asking` stalls only after
    # `stallMinutes` x this (a dead session never holds a slot forever).
    # `softOverlapPaths`: fnmatch globs (e.g. a migrations index, a docs table) whose shared
    # edits are small to merge at epic close -- footprint overlap there never holds a launch.
    # `localLabel`: on a Task of a cloud-placed Epic, keeps that one Task on the laptop (the
    # Epic stays in the cloud). `localTasks`: Tasks local with no label -- "e2e-test" /
    # "integration-test" (the standing Tasks, by title) or "label:<name>" (any Task carrying it).
    "placement": {"cloudLabel": "sdlc:cloud", "initiativeEpics": "local", "stallMinutes": 90,
                  "askingStallFactor": 6, "softOverlapPaths": [], "localLabel": "sdlc:local", "localTasks": []},
    # `command`: run by `development` in its worktree, green before `open-dev-pr`
    # (agents/development.md); "" = none.
    "prePr": {"command": ""},
    # `parkIssue`: the open issue `park-finding` comments friction findings on between retros.
    "retro": {"parkIssue": None},
    # Where operator questions and gate approvals go: "github" (needs-human, gate PRs) or
    # "session" (the orchestrator asks with AskUserQuestion and records the answer).
    "humanChannel": "github",
    # A failed CI check whose failed-step log matches one of these (regex, case-insensitive)
    # is flagged `infra_suspect`: the runner broke, not the code -- fix it, `rerun-checks`.
    # `carryChecksAcrossSync`: a required workflow's pass on the head before a sync merge
    # stands for the synced head when the merged-in delta misses its paths.
    "ci": {"carryChecksAcrossSync": True, "infraFailurePatterns": [
        "No space left on device", "ENOSPC", "Cannot connect to the Docker daemon",
        "docker daemon not running", "Is the docker daemon running",
        "The runner has received a shutdown signal", "lost communication with the server",
        "exit code 137", r"\bKilled\s*$", "OOMKilled",
        # OOM kills: specific runtime/kernel wording only, so a test about memory never matches.
        r"\b(?:by|received|with|signal) (?:signal )?SIGKILL\b", r"\bsignal 9\b",
        "JavaScript heap out of memory", "ERR_WORKER_OUT_OF_MEMORY",
        r"Out of memory: Killed process", "Cannot allocate memory",
        r"fatal error: runtime: out of memory"]},
    # `auto`: the orchestrator runs the final `close-epic` itself; close-epic's own
    # refusals (open children, stale verification, failing checks) still apply.
    # `evidenceCarryForward.paths`: globs a commit after the tested head may touch without
    # invalidating the exploratory evidence (`{docRoot}` expands to `docRoot`).
    "epicClose": {"auto": False,
                  "evidenceCarryForward": {"paths": ["**/*.md", "docs/**", "{docRoot}/**"]},
                  # Shell command close-epic runs in the epic worktree before the
                  # exploratory pass (e.g. a dependency install); "" = none.
                  "prepare": "",
                  # `[{path, versionPattern}]`: files whose text change must move a version
                  # string (regex; group 1 if any) past the base's -- close-epic refuses else.
                  "versionedFiles": []},
    # Priority / Effort `create-issue` sets when no flag names one (fields configured only).
    "issueDefaults": {"priority": "Medium", "effort": "Medium"},
    # `schemaGlobs`: an epic sync whose incoming base delta touches one of these is reported
    # `schema_touch` (a shared entity change needs a migration in every consuming app).
    "sync": {"schemaGlobs": ["packages/**/*.entity.ts", "packages/**/entities/**",
                             "packages/**/migrations/**", "packages/**/schema.prisma"]},
    # Stage models and fan-out: one home, hooks/model_policy.json (agent_guard enforces it).
    "orchestrator": {"model": (_MODEL_POLICY.get("orchestrator") or {}).get("model", "sonnet")},
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
    # Opt-in per Initiative, ordered, first `match` label on the Initiative wins; no "*"
    # entry is implied, so an unmatched Initiative keeps the defaults below
    # (`_INITIATIVE_PROFILE_DEFAULTS`). See `resolve_initiative_profile`.
    "initiativeProfiles": [],
}


# Fallbacks for toggles a matched profile omits (its `gates` fall back to `pipeline.gates`).
_PROFILE_TOGGLE_DEFAULTS = {
    "driven": True,               # False = legacy: pipeline ignores the epic + children
    "epicLevelPhase": True,       # False = standing: each child runs its own full flow
    "childrenNeedArchitectedEpic": True,  # children gated on epic:architected
    "closes": True,               # epic closes + has an integration branch
}


# Fallbacks for an Initiative profile's toggles (and for an Initiative no profile matches).
_INITIATIVE_PROFILE_DEFAULTS = {
    "branch": False,      # True = `initiative-<i>`: its Epics cut from and close into it
    "testTasks": True,    # False = `lld` carves no Integration-test / e2e-test Tasks
    "deferSuites": [],    # suites close-epic does not demand of an Epic closing into the
                          # initiative branch; they gate the initiative -> main merge instead
    "closeSuites": [],    # suite keys (any name, e.g. "e2e") `merge-initiative-pr` also needs
                          # a `record-local-ci` attestation for at the initiative PR's head
    "placement": None,    # "cloud"|"local" overrides `pipeline.placement.initiativeEpics`
    "reviewDepth": "full",  # "light": reviews run single-pass (no fan-out), pr-review skips
                            # the mutation probe -- for throwaway or feasibility work
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


INITIATIVE_BRANCH_PREFIX = PIPELINE["branches"]["initiativePrefix"]


ESCALATION = PIPELINE["escalation"]


# Read at call time (never bound as a default arg) so it can be overridden on the module.
PRODUCT_WIP_CAP = PIPELINE["productWip"]["maxGateAPending"]


# A resume younger than this is treated as possibly still live (item: liveness signal).
RESUME_LIVE_WINDOW_MINUTES = PIPELINE["resume"]["liveWindowMinutes"]


# Read at call time so a test can override it on the module.
CI_INFRA_FAILURE_PATTERNS = tuple(PIPELINE["ci"]["infraFailurePatterns"])


# `{docRoot}` expands once per doc root: the top-level one and every `docRoots` entry's.
EVIDENCE_CARRY_FORWARD_GLOBS = tuple(dict.fromkeys(
    g.replace("{docRoot}", root)
    for g in ((PIPELINE["epicClose"].get("evidenceCarryForward") or {}).get("paths")
              or _PIPELINE_DEFAULTS["epicClose"]["evidenceCarryForward"]["paths"])
    for root in all_doc_roots()))


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


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import github  # noqa: E402,F401
