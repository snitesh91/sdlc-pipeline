"""Issue classification, profiles, doc roots and session/Task placement."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import (
    CONFIG, CONFIG_FILENAME, DOC_ROOT, GATE_PENDING_STATUSES, INITIATIVE_BRANCH_PREFIX, LABELS,
    PIPELINE, PIPELINE_STATUS_FIELD_NAMES, PRIORITY_RANK, PRODUCT_WIP_CAP, STAGE_FIELD_NAMES,
    _INITIATIVE_PROFILE_DEFAULTS, _PROFILE_TOGGLE_DEFAULTS, _find_config, doc_root_rules,
)
from .github import GhError, WorkItemProvider, get_work_item_provider


def label_names(issue: dict) -> set:
    return {l["name"] for l in issue.get("labels", [])}


def without_retro_issues(issues: list) -> list:
    """`issues` minus the retro backlog (`labels.retro`): no scan ever treats it as work."""
    return [i for i in issues if LABELS["retro"] not in label_names(i)]


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
    its own full flow and integrates into its integration base (`integration_base`)."""
    return not resolve_profile(issue)["epicLevelPhase"]


def is_epic_legacy(issue: dict) -> bool:
    """Epic the pipeline does not drive at all (profile `driven` false)."""
    return not resolve_profile(issue)["driven"]


def resolve_initiative_profile(initiative_issue: Optional[dict]) -> dict:
    """The Initiative's first matching `pipeline.initiativeProfiles` entry (by `match` label,
    or `"*"`), its toggles filled from `_INITIATIVE_PROFILE_DEFAULTS`; `name` is None when
    nothing matched -- the v0.3.16 behaviour: no initiative branch, standing test Tasks on."""
    labels = label_names(initiative_issue) if initiative_issue else set()
    chosen: dict = {}
    for prof in PIPELINE["initiativeProfiles"] or []:
        match = prof.get("match")
        if match == "*" or (isinstance(match, dict) and match.get("label") in labels):
            chosen = prof
            break
    merged = {**_INITIATIVE_PROFILE_DEFAULTS,
              **{k: v for k, v in chosen.items() if k in _INITIATIVE_PROFILE_DEFAULTS}}
    merged["deferSuites"] = list(merged.get("deferSuites") or [])
    merged["closeSuites"] = list(merged.get("closeSuites") or [])
    merged["name"] = chosen.get("name") if chosen else None
    return merged


def initiative_branch(initiative: int) -> str:
    """An opted-in Initiative's long-lived branch (default `initiative-<n>`)."""
    return f"{INITIATIVE_BRANCH_PREFIX}{initiative}"


def initiative_of_in(issues: dict, number: int) -> Optional[dict]:
    """The Initiative `number` is or sits under (Task -> Epic -> Initiative), or None."""
    seen, n = set(), number
    while n is not None and n not in seen and len(seen) < 4:
        seen.add(n)
        entry = issues.get(n)
        if entry is None:
            return None
        if is_initiative(entry):
            return entry
        n = (entry.get("parent") or {}).get("number")
    return None


def epic_initiative_branch_in(issues: dict, epic: int) -> Optional[str]:
    """`initiative-<i>` when `epic`'s parent is an Initiative whose profile sets `branch`,
    else None. Only the direct parent counts: an Epic is a native sub-issue of its Initiative."""
    entry = issues.get(epic)
    parent = (entry or {}).get("parent") or {}
    parent_entry = issues.get(parent.get("number"))
    if parent_entry is None or not is_initiative(parent_entry):
        return None
    if not resolve_initiative_profile(parent_entry)["branch"]:
        return None
    return initiative_branch(parent_entry["number"])


def epic_base_in(issues: dict, epic: int) -> str:
    """Where an Epic's branch is cut from, synced with and closes into: its opted-in
    Initiative's branch, else `main`."""
    return epic_initiative_branch_in(issues, epic) or "main"


def epic_base(gh: "WorkItemProvider", epic: int, issues: Optional[list] = None) -> str:
    """`epic_base_in` that looks nothing up while no `initiativeProfiles` entry sets
    `branch`, so a repo without that opt-in makes exactly the calls it made before. A failed
    issue-list read raises: it never falls back to `main` (nor to a branch)."""
    if not any(prof.get("branch") for prof in PIPELINE["initiativeProfiles"] or []):
        return "main"
    return epic_base_in({i["number"]: i for i in (issues if issues is not None
                                                  else gh.issue_list())}, epic)


def unit_initiative_profile(gh: Optional["WorkItemProvider"], number: int) -> dict:
    """The resolved Initiative profile governing unit `number` (`initiative`: its number or
    None), read up its ancestry; the defaults, looking nothing up, without the opt-in."""
    if not PIPELINE["initiativeProfiles"] or number is None:
        return {**resolve_initiative_profile(None), "initiative": None}
    gh = gh or get_work_item_provider()
    chain, n = [], int(number)
    while n is not None and n not in chain and len(chain) < _ANCESTRY_DEPTH:
        chain.append(n)
        info = gh.issue_epic_info(n)
        if classify_unit_from_issue(info) == "initiative":
            prof = {**resolve_initiative_profile(info), "initiative": n}
            _record_hints(INITIATIVE_HINTS_FILE, {m: prof for m in chain})
            return prof
        n = (info.get("parent") or {}).get("number")
    prof = {**resolve_initiative_profile(None), "initiative": None}
    # Recorded too, so the SubagentStart hook knows a unit under no Initiative needs no line.
    _record_hints(INITIATIVE_HINTS_FILE, {m: prof for m in chain})
    return prof


def forget_initiative_hint(*numbers: Optional[int]) -> None:
    """Drop the recorded profile of `numbers` after a failed read, so the SubagentStart hook
    points the agent at `initiative-profile` instead of repeating a possibly stale one."""
    _record_hints(INITIATIVE_HINTS_FILE, {n: None for n in numbers if n is not None})


def is_epic_architected(issue: dict) -> bool:
    """Whether the Epic's design phase is done (label set by `merge-lld-doc`). A label, not
    a marker, so it is readable from the bulk `issue_list()` fetch."""
    return has_label(issue, LABELS["architected"])


# --- Per-product doc roots ------------------------------------------------------------
# A repo hosting several products lists them in the optional top-level `docRoots`:
#   [{"name": "tijori", "match": {"issues": [1994]} | {"label": "product:tijori"},
#     "docRoot": "...", "requirementsDir": "..."}]
# A unit takes the first entry matching the unit itself or any ancestor (Task -> Epic ->
# Initiative), else the top-level `docRoot` / `requirementsDir`. Without `docRoots` nothing
# is looked up and every unit gets the top-level values, exactly as before.

_DOC_ROOTS_CACHE: dict = {}


# Unit -> resolved roots, read by the SubagentStart hook (which has no GitHub access).
DOC_ROOT_HINTS_FILE = "doc-roots.cache"


# Unit -> its Initiative's resolved profile (`unit_initiative_profile`), same reader.
INITIATIVE_HINTS_FILE = "initiative-profiles.cache"


# Unit -> its arch-review skip bar (`skip_confidence_threshold`), same reader.
SKIP_THRESHOLD_HINTS_FILE = "skip-thresholds.cache"


_ANCESTRY_DEPTH = 4


def _default_doc_roots() -> dict:
    return {"docRoot": DOC_ROOT, "requirementsDir": CONFIG.get("requirementsDir"),
            "product": None}


def _doc_root_rule_matches(rule: dict, chain: list) -> bool:
    """Whether `rule.match` names (by number or label) any `(number, labels)` in `chain`."""
    match = rule.get("match") or {}
    numbers = {int(n) for n in (match.get("issues") or [])}
    label = match.get("label")
    return any(n in numbers or (label and label in labels) for n, labels in chain)


def _resolve_doc_roots(chain: list) -> dict:
    for rule in doc_root_rules():
        if _doc_root_rule_matches(rule, chain):
            return {"docRoot": rule["docRoot"],
                    "requirementsDir": rule.get("requirementsDir", CONFIG.get("requirementsDir")),
                    "product": rule.get("name")}
    return _default_doc_roots()


def unit_ancestry(gh: "WorkItemProvider", number: int) -> list:
    """`[(number, labels), ...]` from `number` up its parents (Task -> Epic -> Initiative)."""
    chain, n = [], number
    while n is not None and len(chain) < _ANCESTRY_DEPTH and n not in {c[0] for c in chain}:
        info = gh.issue_epic_info(n)
        chain.append((n, label_names(info)))
        n = (info.get("parent") or {}).get("number")
    return chain


def _record_doc_root_hints(resolved: dict) -> None:
    """Merge `{number: roots}` into the hint file the SubagentStart hook reads; best effort."""
    _record_hints(DOC_ROOT_HINTS_FILE, resolved)


def _record_hints(filename: str, resolved: dict) -> None:
    """Merge `{number: value}` into the run-state hint file `filename`; best effort."""
    path = os.path.join(_run_state_dir(), filename)
    try:
        os.makedirs(_run_state_dir(), exist_ok=True)
        try:
            with open(path) as f:
                hints = json.load(f)
        except (OSError, ValueError):
            hints = {}
        if not isinstance(hints, dict):
            hints = {}
        hints.update({str(n): r for n, r in resolved.items()})
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(hints, f)
        os.replace(tmp, path)
    except OSError:
        pass


def doc_roots_for(number: Optional[int], gh: Optional["WorkItemProvider"] = None) -> dict:
    """`{docRoot, requirementsDir, product}` for unit `number`: the first `docRoots` entry
    matching it or an ancestor, else the top-level values. Looks nothing up (and needs no
    `gh`) when `docRoots` is unset; otherwise cached per process, each ancestor too."""
    if number is None or not doc_root_rules():
        return _default_doc_roots()
    number = int(number)
    if number in _DOC_ROOTS_CACHE:
        return _DOC_ROOTS_CACHE[number]
    chain = unit_ancestry(gh or get_work_item_provider(), number)
    resolved = {chain[i][0]: _resolve_doc_roots(chain[i:]) for i in range(len(chain))}
    _DOC_ROOTS_CACHE.update(resolved)
    _record_doc_root_hints(resolved)
    return resolved.get(number) or _default_doc_roots()


def doc_root_for(number: Optional[int], gh: Optional["WorkItemProvider"] = None) -> str:
    """Unit `number`'s doc root, slash-stripped (see `doc_roots_for`)."""
    return doc_roots_for(number, gh)["docRoot"].rstrip("/")


def config_repo_root() -> str:
    """The repo root holding the config (its dir, or the parent of `.config/` / `.claude/`)."""
    d = os.path.dirname(os.path.abspath(_find_config() or CONFIG_FILENAME))
    return os.path.dirname(d) if os.path.basename(d) in (".config", ".claude") else d


def doc_templates_dir(root: str) -> str:
    """`<root>/<docTemplates>` when that dir exists in the repo, else the top-level docRoot's,
    so a product tree without its own templates borrows the shared ones."""
    sub = PIPELINE["docTemplates"]
    own = f"{root.rstrip('/')}/{sub}"
    if os.path.isdir(os.path.join(config_repo_root(), own)):
        return own
    return f"{DOC_ROOT.rstrip('/')}/{sub}"


def hint_doc_roots(gh: Optional["WorkItemProvider"], *numbers: Optional[int]) -> None:
    """Resolve (and so hint, for the SubagentStart hook) each unit's doc roots; best effort,
    and a no-op without `docRoots`."""
    if not doc_root_rules():
        return
    for n in numbers:
        if n is None:
            continue
        try:
            doc_roots_for(n, gh)
        except GhError:
            pass


def cmd_initiative_profile(gh: Optional["WorkItemProvider"], number: int) -> dict:
    """`initiative-profile <n>`: the Initiative profile governing unit `n` (`initiative`,
    `profile`, `branch` -> `initiative_branch`, `testTasks`, `deferSuites`, `closeSuites`);
    `opted_in: false` and the defaults when nothing matches or no profile is configured."""
    prof = unit_initiative_profile(gh, number)
    out = {"issue": number, "initiative": prof["initiative"], "profile": prof["name"],
           "opted_in": prof["name"] is not None,
           **{k: prof[k] for k in _INITIATIVE_PROFILE_DEFAULTS}}
    if prof["branch"] and prof["initiative"] is not None:
        out["initiative_branch"] = initiative_branch(prof["initiative"])
    return out


def cmd_doc_root(gh: Optional["WorkItemProvider"], number: int) -> dict:
    """Where unit `number`'s docs live (`docRoot`, `requirementsDir`, `docTemplates`)."""
    roots = doc_roots_for(number, gh)
    return {"issue": number, **roots, "docRoot": roots["docRoot"].rstrip("/"),
            "docTemplates": doc_templates_dir(roots["docRoot"]),
            "per_product": bool(doc_root_rules())}


PLACEMENTS = ("cloud", "local")


def cloud_label() -> str:
    return PIPELINE["placement"]["cloudLabel"]


def session_placement() -> dict:
    """Where this session runs, and the `signal` that decided it: `SDLC_PLACEMENT`
    (cloud|local, anything else raises), else a truthy `CLAUDE_CODE_REMOTE` (cloud), else local."""
    explicit = os.environ.get("SDLC_PLACEMENT", "").strip()
    if explicit:
        if explicit.lower() not in PLACEMENTS:
            raise GhError(f"SDLC_PLACEMENT={explicit!r} is neither 'cloud' nor 'local' -- "
                          f"fix or unset it")
        return {"placement": explicit.lower(), "signal": "SDLC_PLACEMENT"}
    if os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in ("true", "1"):
        return {"placement": "cloud", "signal": "CLAUDE_CODE_REMOTE"}
    return {"placement": "local", "signal": "default"}


def cloud_skips_local_cleanup() -> bool:
    """A cloud session's container, `/tmp` worktrees included, is discarded when it ends, so
    local filesystem cleanup there is skipped (an invalid `SDLC_PLACEMENT` counts as local)."""
    try:
        return session_placement()["placement"] == "cloud"
    except GhError:
        return False


def unit_placement(number: int, lookup: Callable[[int], Optional[dict]]) -> dict:
    """`{placement, epic}` (+ `label_on` when cloud): a unit belongs to its Epic (an Initiative,
    or an Initiative's own Task, to the Initiative), which is cloud-placed when it or an ancestor
    carries the cloud label. `lookup(n)` returns an issue dict with flattened `labels`, `parent`,
    `issueType`."""
    label = cloud_label()
    anchor, n = None, number
    for _ in range(4):
        info = lookup(n)
        if info is None:
            break
        kind = classify_unit_from_issue(info)
        if anchor is None and kind in ("epic", "initiative"):
            anchor = n
        if anchor is not None and has_label(info, label):
            if n != number and task_local_reason(lookup(number)):
                return {"placement": "local", "epic": anchor, "task_local": True}
            return {"placement": "cloud", "epic": anchor, "label_on": n}
        parent = (info.get("parent") or {}).get("number")
        if kind == "initiative" or not parent:
            break
        n = parent
    if anchor is None:
        info = lookup(number)
        if info and has_label(info, label):
            return {"placement": "cloud", "epic": number, "label_on": number}
        return {"placement": "local", "epic": number}
    return {"placement": "local", "epic": anchor}


class PlacementMismatch(GhError):
    """This session's placement differs from the unit's; `payload` is the JSON refusal."""

    def __init__(self, payload: dict):
        super().__init__(f"placement_mismatch: {payload['hint']}")
        self.payload = payload


def check_placement(number: int, lookup: Callable[[int], Optional[dict]]) -> dict:
    """Raise `PlacementMismatch` unless this session's placement is the unit's; returns
    the session placement."""
    session = session_placement()
    unit = unit_placement(number, lookup)
    if session["placement"] == unit["placement"]:
        return session
    epic, label = unit["epic"], cloud_label()
    if unit["placement"] == "cloud":
        holder = unit["label_on"]
        on = "" if holder == epic else f" on #{holder}"
        hint = (f"#{epic} is placed in the cloud (label `{label}`{on}): only a cloud session "
                f"drives it. Leave it to that session; to take it back, `place {holder} --where "
                f"local` once no cloud session is driving it. To keep one Task of it on the "
                f"laptop instead, `place <task> --where local`; a laptop run then drives only "
                f"those Tasks.")
    else:
        hint = ((f"#{number} is a local Task of cloud Epic #{epic} (`{local_label()}` label or "
                 f"`pipeline.placement.localTasks`): only a laptop session drives it. Leave it; "
                 f"the cloud session ends `waiting-local` once only local Tasks remain.")
                if unit.get("task_local") else
                (f"#{epic} is placed local (no `{label}` label): only a local session drives it. "
                 f"Hand it over from the laptop with `place {epic} --where cloud` first."))
    hint += (f" Session placement came from {session['signal']}; set SDLC_PLACEMENT=cloud|local "
             f"only if that is wrong.")
    raise PlacementMismatch({"error": "placement_mismatch",
                             "session_placement": session["placement"],
                             "signal": session["signal"],
                             "unit_placement": unit["placement"], "epic": epic, "hint": hint})


_LOCAL_TASK_KINDS = {
    "e2e-test": re.compile(r"\s*(?:standing\s+)?e2e[\s_-]*tests?(?:\s+task)?\s*$", re.I),
    "integration-test": re.compile(
        r"\s*(?:standing\s+)?integration[\s_-]*tests?(?:\s+task)?\s*$", re.I),
}


def local_label() -> str:
    return PIPELINE["placement"].get("localLabel") or "sdlc:local"


def task_local_reason(issue: Optional[dict]) -> Optional[str]:
    """Why a Task (never an Epic/Initiative) runs on the laptop while its Epic is in the
    cloud: `label` (`localLabel`), a `localTasks` kind, or `label:<name>`; else None."""
    if not issue or classify_unit_from_issue(issue) in ("epic", "initiative"):
        return None
    if has_label(issue, local_label()):
        return "label"
    title = issue.get("title") or ""
    for entry in PIPELINE["placement"].get("localTasks") or []:
        entry = str(entry)
        if entry.startswith("label:"):
            if has_label(issue, entry[len("label:"):]):
                return entry
        elif entry in _LOCAL_TASK_KINDS and _LOCAL_TASK_KINDS[entry].match(title):
            return entry
        # An unknown entry matches nothing; `local_tasks_config_errors` reports it.
    return None


def local_tasks_config_errors() -> list:
    """One line per `pipeline.placement.localTasks` entry that is neither a known kind nor
    `label:<name>` -- reported by `show-config` and `next-action` (`config_errors`), never
    raised, so a typo cannot fail every call."""
    return [f"pipeline.placement.localTasks entry {str(e)!r} is neither "
            f"{' / '.join(sorted(_LOCAL_TASK_KINDS))} nor label:<name>; it is ignored"
            for e in PIPELINE["placement"].get("localTasks") or []
            if not str(e).startswith("label:") and str(e) not in _LOCAL_TASK_KINDS
            or str(e) == "label:"]


def placement_scope(number: int, lookup: Callable[[int], Optional[dict]],
                    issues: list) -> Optional[str]:
    """Which children of Epic/Initiative `number` this session may drive: None (all), "cloud"
    (a cloud session: every child except local Tasks) or "local-tasks" (a laptop session on a
    cloud Epic that has open local Tasks: only those). Raises `PlacementMismatch` otherwise."""
    try:
        session = check_placement(number, lookup)
    except PlacementMismatch as exc:
        if exc.payload["session_placement"] == "local" and local_task_numbers(number, issues):
            return "local-tasks"
        raise
    return "cloud" if (session or {}).get("placement") == "cloud" else None


def local_task_numbers(epic: int, issues: list) -> list:
    """Open Task children of `epic` that run local (`task_local_reason`)."""
    return sorted(i["number"] for i in issues if i["state"] == "OPEN"
                  and (i.get("parent") or {}).get("number") == epic and not is_epic(i)
                  and task_local_reason(i))


def in_scope(issue: dict, scope: Optional[str]) -> bool:
    """Whether a child is this session's to drive under `placement_scope`'s `scope`."""
    if scope is None:
        return True
    local = bool(task_local_reason(issue))
    return local if scope == "local-tasks" else not local


def _issue_info_lookup(gh: WorkItemProvider) -> Callable[[int], Optional[dict]]:
    """A memoised `issue_epic_info` for `unit_placement`, so each issue is fetched once."""
    cache: dict = {}

    def lookup(n: int) -> Optional[dict]:
        if n not in cache:
            cache[n] = gh.issue_epic_info(n)
        return cache[n]
    return lookup


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


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import evidence  # noqa: E402,F401
from .run_state import _run_state_dir  # noqa: E402
