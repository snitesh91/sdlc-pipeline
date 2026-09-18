"""PreToolUse(Agent|Task): every sdlc agent runs at its policy model, and review fan-out is capped.

A stage agent gets `models.<role>`; a child launched by an `sdlc:<parent>` agent gets
`fanout.<parent>` and is denied when that parent may not fan out, is out of slots, or the
child is itself a stage. Policy: `_common.model_policy`.
"""
import fcntl
import os
import time

from _common import (emit, first_prompt, load_json, model_policy, plugin_data_dir,
                     prompt_header, read_input, repo_config, run, sdlc_role)

DESIGN_REVIEW = "design-review"
DESIGN_REVIEW_ROLES = ("arch-review", "lld-review")
SLOT_TTL_S = 2 * 86400


def deny(reason: str) -> None:
    emit("PreToolUse", permissionDecision="deny",
         permissionDecisionReason="sdlc agent guard: " + reason)


def set_model(tool_input: dict, model) -> None:
    """Replace only `model`; every other field of the call is kept."""
    if model and tool_input.get("model") != model:
        emit("PreToolUse", permissionDecision="allow", updatedInput={**tool_input, "model": model})


def design_review_role(prompt_role, policies: dict):
    """arch-review or lld-review; None when the prompt names neither and the two policies differ."""
    if prompt_role in DESIGN_REVIEW_ROLES:
        return prompt_role
    arch, lld = (policies.get(r) for r in DESIGN_REVIEW_ROLES)
    return DESIGN_REVIEW_ROLES[0] if arch == lld else None


def parent_transcript(data: dict) -> str:
    """The calling subagent's own transcript: `<session>/subagents/agent-<id>.jsonl`."""
    path, agent = str(data.get("transcript_path") or ""), os.path.basename(str(data["agent_id"]))
    if path.endswith(f"agent-{agent}.jsonl"):
        return path
    return os.path.join(path[:-len(".jsonl")], "subagents", f"agent-{agent}.jsonl")


def claim_slot(agent_id: str, limit: int) -> bool:
    """Count one more child against `agent_id`; False once `limit` are already launched."""
    root = os.path.join(plugin_data_dir(), "fanout")
    os.makedirs(root, exist_ok=True)
    stale = time.time() - SLOT_TTL_S
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < stale:
                os.remove(path)
        except OSError:
            pass
    # Parallel launches from one message run their hooks concurrently.
    with open(os.path.join(root, os.path.basename(agent_id)), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        count = int(f.read().strip() or 0)
        if count >= limit:
            return False
        f.seek(0)
        f.truncate()
        f.write(str(count + 1))
    return True


def main() -> int:
    data = read_input()
    tool_input = data.get("tool_input")
    if data.get("tool_name") not in ("Agent", "Task") or not isinstance(tool_input, dict):
        return 0
    config = repo_config(data.get("cwd") or os.getcwd())
    if not config:
        return 0
    policy = model_policy(load_json(config))
    child = sdlc_role(tool_input.get("subagent_type"))
    header = prompt_header(tool_input.get("prompt"))
    if not data.get("agent_id"):
        if not child:
            return 0
        role = child
        if child == DESIGN_REVIEW:
            role = design_review_role(header.get("role"), policy["models"])
            if role is None:
                deny("start the sdlc:design-review prompt with `ROLE: arch-review|lld-review "
                     "ISSUE: <n>`; the two roles run at different models.")
                return 0
        set_model(tool_input, policy["models"].get(role))
        return 0
    parent = sdlc_role(data.get("agent_type"))
    if not parent:
        return 0
    if child:
        deny(f"a stage agent never launches another stage (sdlc:{child}); report it in your "
             "SDLC-RESULT/handoff and the orchestrator runs it.")
        return 0
    if parent == DESIGN_REVIEW:
        parent = design_review_role(
            prompt_header(first_prompt(parent_transcript(data))).get("role"), policy["fanout"])
        if parent is None:
            return 0
    fanout = policy["fanout"].get(parent) or {}
    if not fanout.get("allowed"):
        deny(f"{parent} reviews in a single pass; work the axes yourself.")
        return 0
    limit = int(fanout.get("maxChildren") or 0)
    if not claim_slot(str(data["agent_id"]), limit):
        deny(f"{parent} may launch at most {limit} fan-out children; work the rest yourself.")
        return 0
    set_model(tool_input, (fanout.get("axisModels") or {}).get(header.get("axis"))
              or fanout.get("model"))
    return 0


if __name__ == "__main__":
    run(main)
