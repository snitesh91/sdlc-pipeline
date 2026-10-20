"""PreToolUse(Edit|Write|MultiEdit|NotebookEdit): a review role edits files only inside its own
detached review worktree (`<worktrees.root>/<reviewPrefix>*`) — `pr-review`'s reverted mutation
probe is the one edit a reviewer makes. Every other role, and the main thread, is unaffected.
"""
import os

from _common import emit, load_json, read_input, repo_config, run, sdlc_role

REVIEW_ROLES = {"product-review", "design-review", "pr-review"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def review_root(config: dict) -> tuple:
    """(worktrees root with symlinks resolved — macOS /tmp — , the review worktree prefix)."""
    worktrees = (config.get("pipeline") or {}).get("worktrees") or {}
    return (os.path.realpath(worktrees.get("root", "/tmp")),
            worktrees.get("reviewPrefix", "sdlc-review-"))


def allowed(path: str, config: dict, cwd: str) -> bool:
    """True for a file inside `<root>/<reviewPrefix><n>/`, never the worktree dir itself."""
    root, prefix = review_root(config)
    target = os.path.realpath(os.path.join(cwd, path))
    parts = os.path.relpath(target, root).split(os.sep)
    return parts[0] != ".." and parts[0].startswith(prefix) and len(parts) > 1


def main() -> int:
    data = read_input()
    if data.get("tool_name") not in EDIT_TOOLS:
        return 0
    role = sdlc_role(data.get("agent_type"))
    if role not in REVIEW_ROLES:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    config_path = repo_config(cwd)
    if not config_path:
        return 0
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return 0
    config = load_json(config_path)
    if not allowed(path, config, cwd):
        emit("PreToolUse", permissionDecision="deny",
             permissionDecisionReason=(
                 f"sdlc guard: a review role edits only inside its own detached review worktree "
                 f"({os.path.join(*review_root(config))}<n>/) — the mutation probe, reverted with `git restore`. "
                 f"Describe every other change as a finding; never edit the unit's worktree or "
                 f"the main checkout."))
    return 0


if __name__ == "__main__":
    run(main)
