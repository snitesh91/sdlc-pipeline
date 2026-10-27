"""PreToolUse(Edit|Write|MultiEdit|NotebookEdit): a review role edits files only inside its own
detached review worktree (`<worktrees.root>/<reviewPrefix><n>`, `n` its prompt's ISSUE) —
`pr-review`'s reverted mutation probe is the one edit a reviewer makes. A review role's fan-out
child edits nothing. Every other role, and the main thread, is unaffected. For a review role the
guard fails closed: no config, path or unit number is a denial.
"""
import os

from _common import (REVIEW_ROLES, agent_transcript, emit, first_prompt, load_json, prompt_header,
                     read_input, repo_config, review_child_role, run, sdlc_role)

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def review_root(config: dict) -> tuple:
    """(worktrees root with symlinks resolved — macOS /tmp — , the review worktree prefix)."""
    worktrees = (config.get("pipeline") or {}).get("worktrees") or {}
    return (os.path.realpath(worktrees.get("root", "/tmp")),
            worktrees.get("reviewPrefix", "sdlc-review-"))


def allowed(path: str, config: dict, cwd: str, unit: str) -> bool:
    """True for a file inside `<root>/<reviewPrefix><unit>/`, never the worktree dir itself."""
    root, prefix = review_root(config)
    target = os.path.realpath(os.path.join(cwd, path))
    parts = os.path.relpath(target, root).split(os.sep)
    return parts[0] == f"{prefix}{unit}" and len(parts) > 1


def own_unit(data: dict) -> str:
    """The reviewer's ISSUE number from its delegation prompt's header, or ""."""
    if not data.get("agent_id"):
        return ""
    value = str(prompt_header(first_prompt(agent_transcript(data))).get("issue") or "").strip("`#*")
    return value if value.isdigit() else ""


def denial(data: dict) -> str:
    """Why this reviewer's edit is denied, or "" to allow it."""
    cwd = data.get("cwd") or os.getcwd()
    config_path = repo_config(cwd)
    if not config_path:
        return "no sdlc-pipeline.config.json found from this cwd, so the review worktree is unknown."
    config = load_json(config_path)
    root, prefix = review_root(config)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    unit = own_unit(data)
    if not unit:
        return (f"your prompt's first line names no `ISSUE: <n>`, so your review worktree "
                f"({os.path.join(root, prefix)}<n>/) is unknown.")
    if not isinstance(path, str) or not path or not allowed(path, config, cwd, unit):
        return (f"a review role edits only inside its own detached review worktree "
                f"({os.path.join(root, prefix)}{unit}/) — the mutation probe, reverted with "
                f"`git restore`. Describe every other change as a finding; never edit the unit's "
                f"worktree, another unit's review tree or the main checkout.")
    return ""


def main() -> int:
    data = read_input()
    if data.get("tool_name") not in EDIT_TOOLS:
        return 0
    if sdlc_role(data.get("agent_type")) in REVIEW_ROLES:
        try:
            reason = denial(data)
        except Exception as e:  # fail closed: a reviewer's edit is never let through unchecked
            reason = f"the edit could not be checked ({type(e).__name__})."
    elif review_child_role(data):
        reason = ("a review fan-out child is read-only: put the change in your reply as a "
                  "candidate finding; the reviewer decides.")
    else:
        return 0
    if reason:
        emit("PreToolUse", permissionDecision="deny",
             permissionDecisionReason="sdlc guard: " + reason)
    return 0


if __name__ == "__main__":
    run(main)
