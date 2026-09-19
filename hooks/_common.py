"""Shared helpers for the sdlc plugin hooks. Stdlib only."""
import json
import os
import re
import sys
import time

CONFIG_NAME = "sdlc-pipeline.config.json"
CONFIG_DIRS = ("", ".config", ".claude")  # same lookup places as sdlc_next.py
HEADER_RE = re.compile(r"\b([A-Z]+):\s*(\S+)")
OUTCOMES = ("done", "clean", "rework", "blocked", "needs-human", "failed")
RESULT_PREFIX = "SDLC-RESULT:"
RESULT_FORMAT = (RESULT_PREFIX + ' {"issue": <n>, "stage": "<stage>", "outcome": "'
                 + "|".join(OUTCOMES) + '"}')
# A standing child's flow; an optional `"next"` recommends one of these (or `merge`,
# skipping pr-review) with a `"why"` of at most WHY_CAP chars.
STANDING_FLOW = ("product", "product-review", "architecture", "arch-review", "development",
                 "pr-review")
NEXT_STAGES = STANDING_FLOW + ("merge",)
WHY_CAP = 120


def read_input() -> dict:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def repo_config(cwd: str):
    """The driven repo's config, found as sdlc_next.py's `_find_config` finds it
    ($SDLC_CONFIG, else walking up from cwd), or None outside an sdlc repo."""
    cwd = os.path.abspath(cwd or os.getcwd())
    env = os.environ.get("SDLC_CONFIG")
    if env:
        path = os.path.join(cwd, os.path.expanduser(env))
        return path if os.path.isfile(path) else None
    while True:
        for sub in CONFIG_DIRS:
            path = os.path.join(cwd, sub, CONFIG_NAME)
            if os.path.isfile(path):
                return path
        parent = os.path.dirname(cwd)
        if parent == cwd:
            return None
        cwd = parent


def load_json(path: str) -> dict:
    """The JSON object at `path`, or {} when it is missing, unreadable or not an object."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_states(config: dict, session_id: str, max_age: float = None) -> list:
    """This session's control-plane run states, newest first; the directory is found as
    sdlc_next.py's `_run_state_dir` finds it. With `max_age` (seconds), only files written
    that recently: nothing deletes a state file when its run ends."""
    worktrees = ((config.get("pipeline") or {}).get("worktrees") or {})
    root = os.environ.get("SDLC_RUNS_DIR") or os.path.join(worktrees.get("root", "/tmp"),
                                                           ".sdlc-runs")
    try:
        paths = sorted((os.path.join(root, n) for n in os.listdir(root) if n.endswith(".json")),
                       key=os.path.getmtime, reverse=True)
        if max_age is not None:
            now = time.time()
            paths = [p for p in paths if now - os.path.getmtime(p) <= max_age]
    except OSError:
        return []
    states = [load_json(p) for p in paths]
    return [s for s in states if session_id and s.get("session_id") == session_id]


def plugin_root() -> str:
    return os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))


def plugin_data_dir() -> str:
    """Hook state lives here, never in the driven repo's tree (the control plane refuses dirty trees).

    Hooks get $CLAUDE_PLUGIN_DATA; a Bash call (`sdlc_metrics.py`) does not, so an installed
    plugin derives the same `<plugin>-<marketplace>` directory from its cache path."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return env
    parts = os.path.normpath(plugin_root()).split(os.sep)
    if len(parts) > 5 and parts[-4] == "cache" and parts[-5] == "plugins":
        plugins_dir = os.sep.join(parts[:-4])
        return os.path.join(plugins_dir, "data", f"{parts[-2]}-{parts[-3]}")
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                        "plugins", "data", "sdlc")


def policy_file() -> dict:
    return load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_policy.json"))


def model_policy(config: dict) -> dict:
    """hooks/model_policy.json with the config's `pipeline.models` / `pipeline.fanout` merged
    over it per role, as sdlc_next.py's `show-config` reports it; `explore` as shipped."""
    policy = policy_file()
    user = config.get("pipeline") if isinstance(config.get("pipeline"), dict) else {}
    merged = {key: {**(policy.get(key) or {}), **(user.get(key) or {})}
              for key in ("models", "fanout")}
    return {**merged, "explore": policy.get("explore") or []}


def sdlc_role(agent_type) -> str:
    """`<role>` for an `sdlc:<role>` agent type, else ""."""
    agent_type = str(agent_type or "")
    return agent_type[len("sdlc:"):] if agent_type.startswith("sdlc:") else ""


def prompt_header(prompt) -> dict:
    """`KEY: value` pairs on a prompt's first line (`ROLE: arch-review ISSUE: 5`)."""
    first = str(prompt or "").lstrip().split("\n", 1)[0]
    return {k.lower(): v for k, v in HEADER_RE.findall(first)}


def message_text(content) -> str:
    """The text blocks of a transcript message's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def first_prompt(transcript: str) -> str:
    """The first user message of a transcript (a subagent's delegation prompt), or ""."""
    try:
        with open(transcript) as f:
            for line in f:
                if '"user"' not in line:
                    continue
                entry = json.loads(line)
                msg = entry.get("message") if isinstance(entry, dict) else None
                if isinstance(msg, dict) and msg.get("role") == "user":
                    return message_text(msg.get("content"))
    except (OSError, ValueError):
        pass
    return ""


def sdlc_result(text: str):
    """(result, None) for the last valid-looking SDLC-RESULT line in `text`, else (None, problem)."""
    found = None
    for line in str(text or "").splitlines():
        s = line.strip().strip("`*_ ")
        if s.startswith(RESULT_PREFIX):
            found = s[len(RESULT_PREFIX):].strip().strip("`")
    if found is None:
        return None, "no SDLC-RESULT line in your final message"
    try:
        result = json.loads(found)
    except ValueError:
        return None, f"SDLC-RESULT payload is not valid JSON: {found[:200]}"
    if not isinstance(result, dict):
        return None, "SDLC-RESULT payload must be a JSON object"
    issue = result.get("issue")
    if not isinstance(issue, int) or isinstance(issue, bool):
        return None, '"issue" must be an integer'
    if not isinstance(result.get("stage"), str) or not result["stage"]:
        return None, '"stage" must be a non-empty string'
    if result.get("outcome") not in OUTCOMES:
        return None, f'"outcome" must be one of {", ".join(OUTCOMES)}'
    if "next" in result or "why" in result:
        if result.get("next") not in NEXT_STAGES:
            return None, f'"next" must be one of {", ".join(NEXT_STAGES)}, with a "why"'
        why = result.get("why")
        if not isinstance(why, str) or not why.strip() or len(why) > WHY_CAP:
            return None, f'"why" must be a non-empty string of at most {WHY_CAP} chars'
    return result, None


def emit(event: str, **fields) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def run(main) -> None:
    """Run a hook's main(); any internal error fails open (exit 0, no output)."""
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code or 0)
