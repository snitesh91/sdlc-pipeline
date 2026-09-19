"""SubagentStart: tell every sdlc:* agent where the control plane, docs and references are."""
import os

from _common import RESULT_FORMAT, emit, load_json, plugin_root, read_input, repo_config, run, sdlc_role


def main() -> int:
    data = read_input()
    if not sdlc_role(data.get("agent_type")):
        return 0
    config_path = repo_config(data.get("cwd") or os.getcwd())
    if not config_path:
        return 0
    config, root = load_json(config_path), plugin_root()
    emit("SubagentStart", additionalContext="\n".join([
        f'sdlc: run the control plane as python3 "$SDLC" <cmd> in Bash; '
        f'$SDLC={os.path.join(root, "scripts", "sdlc_next.py")}.',
        f"sdlc: docRoot={config.get('docRoot')}, requirementsDir={config.get('requirementsDir')}, "
        f"docTemplates={(config.get('pipeline') or {}).get('docTemplates') or '_templates'} "
        f"(relative to the repo root; docTemplates to docRoot).",
        f"sdlc: ${{CLAUDE_PLUGIN_ROOT}} is {root}; its references are "
        f"{os.path.join(root, 'references')}.",
        f"sdlc: your final message's last line is {RESULT_FORMAT}",
    ]))
    return 0


if __name__ == "__main__":
    run(main)
