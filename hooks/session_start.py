"""SessionStart: export $SDLC, and GITHUB_TOKEN from the config's tokenEnv var or tokenPath, for later Bash calls;
flag an orchestrator model off policy; after a compaction, restate the run being driven."""
import os
import shlex
import shutil

from _common import emit, load_json, plugin_root, policy_file, read_input, repo_config, run, run_states
from _metrics import model_family

MAX_RUNS_SHOWN = 3


def _token_path(config: dict, config_path: str):
    path = config.get("tokenPath")
    if not isinstance(path, str) or not path:
        return None
    # Absolute, so the export works from any worktree cwd.
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), os.path.expanduser(path))


def _token_env(config: dict):
    name = config.get("tokenEnv")
    return name if isinstance(name, str) and name.isidentifier() else None


def _token_value(env_name, token_file):
    """The token the exports will select, or None when neither source yields one."""
    value = os.environ.get(env_name, "") if env_name else ""
    if not value and token_file and os.path.isfile(token_file):
        try:
            with open(token_file) as f:
                value = f.read().strip()
        except OSError:
            value = ""
    return value or None


def model_hint(session_model) -> list:
    """One line when the session's model family is not the policy's orchestrator model."""
    if isinstance(session_model, dict):
        session_model = session_model.get("id")
    wanted = (policy_file().get("orchestrator") or {}).get("model")
    have = model_family(session_model or "")
    if not wanted or not have or have == wanted:
        return []
    return [f"sdlc: Session model is {have}; the sdlc policy runs the orchestrator on {wanted} "
            f"-- if you are about to run /sdlc:run, tell the operator to restart with "
            f"`sdlc-run <n>`."]


def resume_lines(states: list) -> list:
    """≤ 5 lines restating this session's runs after a compaction; [] when it drives none."""
    lines = []
    for state in states[:MAX_RUNS_SHOWN]:
        flight = ", ".join(f"#{n} ({stage})" for n, stage in (state.get("in_flight") or {}).items())
        done = ", ".join(f"#{n}" for n in state.get("terminal", []))
        lines.append(f"sdlc: driving #{state.get('epic')} with --run-id {state.get('run_id')}; "
                     f"in flight: {flight or 'none'}; done this run: {done or 'none'}.")
    if lines:
        lines.insert(0, "sdlc: context was compacted mid-run; this is the run state you were driving.")
        lines.append('sdlc: re-survey with python3 "$SDLC" next-action <epic> --run-id <same id> '
                     "before delegating anything.")
    return lines


def main() -> int:
    data = read_input()
    config = repo_config(data.get("cwd") or os.getcwd())
    if not config:
        return 0
    sdlc = os.path.join(plugin_root(), "scripts", "sdlc_next.py")
    exports = [f"export SDLC={shlex.quote(sdlc)}"]
    context = []
    cfg = load_json(config)
    token = _token_path(cfg, config)
    env_name = _token_env(cfg)
    token_file = token if token and os.path.isfile(token) else None
    if env_name or token_file:
        # Evaluated per Bash call; the configured source overrides any ambient GITHUB_TOKEN
        # (a stray one would otherwise shadow it), and the secret never lands in the file.
        branches = []
        if env_name:
            branches.append(f'if [ -n "${{{env_name}:-}}" ]; then export GITHUB_TOKEN="${env_name}"')
        if token_file:
            branches.append(f'{"elif" if env_name else "if"} [ -f {shlex.quote(token_file)} ]; '
                            f'then export GITHUB_TOKEN="$(cat {shlex.quote(token_file)})"')
        exports.append("; ".join(branches) + "; fi")
        value = _token_value(env_name, token_file)
        if value is None:
            context.append("sdlc: the config's tokenEnv/tokenPath yield no token; ask the operator "
                           "to set that env var or fill the file with a classic PAT (ghp_).")
        elif not value.startswith("ghp_"):
            context.append("sdlc: the configured token is not a classic PAT (ghp_); fine-grained "
                           "tokens fail check-runs and merges on personal-org repos.")
    elif not os.environ.get("GITHUB_TOKEN"):
        context.append(
            "sdlc: GITHUB_TOKEN is unset and the config sets no usable tokenEnv or tokenPath; "
            "ask the operator to export a classic PAT (ghp_) as GITHUB_TOKEN, name an env var in "
            "tokenEnv, or create the tokenPath file.")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a") as f:
            f.write("\n".join(exports) + "\n")
    else:
        context.append(f"sdlc: CLAUDE_ENV_FILE unavailable; run the control plane as python3 {shlex.quote(sdlc)}.")
    if shutil.which("rtk") is None:
        context.append("sdlc: optional token saver `rtk` is not installed; suggest once that "
                       "the operator installs it and runs `rtk init`. Never block on it.")
    context += model_hint(data.get("model"))
    if data.get("source") == "compact":
        context += resume_lines(run_states(load_json(config), str(data.get("session_id") or "")))
    if context:
        emit("SessionStart", additionalContext="\n".join(context))
    return 0


if __name__ == "__main__":
    run(main)
