"""SubagentStart: tell every sdlc:* agent where the control plane, docs and references are."""
import os

from _common import (CONFIG_DIRS, RESULT_FORMAT, agent_transcript, emit, first_prompt, load_json,
                     plugin_root, prompt_header, read_input, repo_config, run, runs_dir, sdlc_role)

# Written by sdlc_next.py (`doc_roots_for`) in the run-state directory: unit -> resolved roots.
DOC_ROOT_HINTS_FILE = "doc-roots.cache"
# Written by sdlc_next.py (`unit_initiative_profile`): unit -> its Initiative's profile.
INITIATIVE_HINTS_FILE = "initiative-profiles.cache"


def header_units(data: dict) -> list:
    """The ISSUE then EPIC numbers on the delegation prompt's first line (`ROLE: x ISSUE: 5
    EPIC: 4`), read from the payload's prompt or else the agent's own transcript."""
    prompt = data.get("prompt")
    if not prompt and data.get("agent_id") and data.get("transcript_path"):
        prompt = first_prompt(agent_transcript(data))
    header = prompt_header(prompt)
    units = []
    for key in ("issue", "epic"):
        value = str(header.get(key) or "").strip("`#*")
        if value.isdigit() and int(value) not in units:
            units.append(int(value))
    return units


def unit_doc_roots(config: dict, data: dict):
    """`(roots, unit, resolved)`: the unit's `{docRoot, requirementsDir}` from the control
    plane's hint file or a `docRoots` entry naming the unit by number; else the top-level
    values (`resolved` False only when `docRoots` is set and nothing here names the unit)."""
    top = {"docRoot": config.get("docRoot"), "requirementsDir": config.get("requirementsDir")}
    rules = [r for r in (config.get("docRoots") or [])
             if isinstance(r, dict) and r.get("docRoot")]
    units = header_units(data)
    if not rules:
        return top, None, True
    hints = load_json(os.path.join(runs_dir(config), DOC_ROOT_HINTS_FILE))
    for unit in units:
        hint = hints.get(str(unit))
        if isinstance(hint, dict) and hint.get("docRoot"):
            return {"docRoot": hint["docRoot"],
                    "requirementsDir": hint.get("requirementsDir")}, unit, True
        for rule in rules:
            match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
            if unit in {int(n) for n in (match.get("issues") or []) if str(n).isdigit()}:
                return {"docRoot": rule["docRoot"],
                        "requirementsDir": rule.get("requirementsDir", top["requirementsDir"])
                        }, unit, True
    return top, (units[0] if units else None), False


def initiative_lines(config: dict, data: dict) -> list:
    """Lines naming the unit's opted-in Initiative profile (`pipeline.initiativeProfiles`):
    from the control plane's hint file, else a pointer to `initiative-profile`. None without
    the opt-in, so every other repo's context is unchanged."""
    if not (config.get("pipeline") or {}).get("initiativeProfiles"):
        return []
    units = header_units(data)
    hints = load_json(os.path.join(runs_dir(config), INITIATIVE_HINTS_FILE))
    for unit in units:
        hint = hints.get(str(unit))
        if not isinstance(hint, dict):
            continue
        if not hint.get("name"):
            return []
        i, lines = hint.get("initiative"), []
        if hint.get("testTasks") is False:
            lines.append(f"sdlc: Initiative #{i} turns the standing test Tasks off "
                         f"(testTasks: false): carve no Integration-test / e2e-test Task and "
                         f"never require them; a Task's own section names any integration/e2e "
                         f"coverage it needs; the Initiative's final runs gate its merge to main.")
        if hint.get("branch"):
            lines.append(f"sdlc: Initiative #{i}'s Epics integrate into its initiative branch, "
                         f"not main; it reaches main once, at initiative close.")
        return lines
    unit = units[0] if units else "<your issue>"
    return [f"sdlc: this repo opts some Initiatives out of the standing test Tasks: run "
            f"python3 \"$SDLC\" initiative-profile {unit} and follow its testTasks."]


def templates_dir(config: dict, config_path: str, doc_root) -> str:
    """`<unit docRoot>/<docTemplates>` when the repo has it, else under the top-level docRoot."""
    name = (config.get("pipeline") or {}).get("docTemplates") or "_templates"
    repo = os.path.dirname(os.path.abspath(config_path))
    if os.path.basename(repo) in CONFIG_DIRS:
        repo = os.path.dirname(repo)
    if doc_root and os.path.isdir(os.path.join(repo, doc_root, name)):
        return f"{doc_root}/{name}"
    top = config.get("docRoot")
    return f"{top}/{name}" if top else name


def main() -> int:
    data = read_input()
    if not sdlc_role(data.get("agent_type")):
        return 0
    config_path = repo_config(data.get("cwd") or os.getcwd())
    if not config_path:
        return 0
    config, root = load_json(config_path), plugin_root()
    roots, unit, resolved = unit_doc_roots(config, data)
    lines = [
        f'sdlc: run the control plane as python3 "$SDLC" <cmd> in Bash; '
        f'$SDLC={os.path.join(root, "scripts", "sdlc_next.py")}.',
        f"sdlc: docRoot={roots['docRoot']}, requirementsDir={roots['requirementsDir']}, "
        f"docTemplates={templates_dir(config, config_path, roots['docRoot'])} "
        f"(relative to the repo root)"
        + (f"; resolved for #{unit}." if unit is not None and resolved else "."),
    ]
    if not resolved:
        lines.append(
            f"sdlc: this repo keeps each product's docs under its own root (`docRoots`) and "
            f"yours is not known here: run python3 \"$SDLC\" doc-root "
            f"{unit if unit is not None else '<your issue>'} and use its docRoot, "
            f"requirementsDir and docTemplates instead of the line above.")
    lines += initiative_lines(config, data)
    lines += [
        f"sdlc: ${{CLAUDE_PLUGIN_ROOT}} is {root}; its references are "
        f"{os.path.join(root, 'references')}.",
        f"sdlc: your final message's last line is {RESULT_FORMAT}",
    ]
    emit("SubagentStart", additionalContext="\n".join(lines))
    return 0


if __name__ == "__main__":
    run(main)
