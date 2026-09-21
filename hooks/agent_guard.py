"""PreToolUse(Agent|Task): every sdlc agent runs at its policy model, and review fan-out is capped.

A stage agent gets `models.<role>`; a child launched by an `sdlc:<parent>` agent gets
`fanout.<parent>` and is denied when that parent may not fan out, is out of slots, or the
child is itself a stage. An `explore` role's read-only Explore search passes untouched.
Policy: `_common.model_policy`.
"""
import fcntl
import json
import os
import time

from _common import (emit, first_prompt, load_json, model_policy, plugin_data_dir,
                     prompt_header, read_input, repo_config, run, sdlc_role)

DESIGN_REVIEW = "design-review"
DESIGN_REVIEW_ROLES = ("arch-review", "lld-review")
SLOT_TTL_S = 2 * 86400
RETRY_HINT = ("re-run this launch when any holder reports; work an axis yourself only when no "
              "holder is left to wait for")


def deny(reason: str) -> None:
    emit("PreToolUse", permissionDecision="deny",
         permissionDecisionReason="sdlc agent guard: " + reason)


def set_model(tool_input: dict, model, warning: str = "") -> None:
    """Replace only `model`; every other field of the call is kept. A `warning` rides along
    as context even when the model already matches."""
    fields = {}
    if model and tool_input.get("model") != model:
        fields["updatedInput"] = {**tool_input, "model": model}
    if warning:
        fields["additionalContext"] = "sdlc agent guard: " + warning
    if fields:
        emit("PreToolUse", permissionDecision="allow", **fields)


def design_review_role(prompt_role, policies: dict):
    """arch-review or lld-review; None when the prompt names neither and the two policies differ."""
    if prompt_role in DESIGN_REVIEW_ROLES:
        return prompt_role
    arch, lld = (policies.get(r) for r in DESIGN_REVIEW_ROLES)
    return DESIGN_REVIEW_ROLES[0] if arch == lld else None


def stricter_fanout_role(policies: dict) -> str:
    """The design-review role with the tighter fan-out policy: denied beats allowed, then the
    lower `maxChildren`. The fallback when the parent's role cannot be read: never uncapped."""
    def key(role):
        p = policies.get(role) or {}
        return (bool(p.get("allowed")), int(p.get("maxChildren") or 0))
    return min(DESIGN_REVIEW_ROLES, key=key)


def fanout_label(header: dict, tool_input: dict) -> str:
    """What a slot holder is called back to the parent: its `AXIS:`, else its description."""
    return str(header.get("axis") or tool_input.get("description")
               or tool_input.get("subagent_type") or "child")


def parent_transcript(data: dict) -> str:
    """The calling subagent's own transcript: `<session>/subagents/agent-<id>.jsonl`."""
    path, agent = str(data.get("transcript_path") or ""), os.path.basename(str(data["agent_id"]))
    if path.endswith(f"agent-{agent}.jsonl"):
        return path
    return os.path.join(path[:-len(".jsonl")], "subagents", f"agent-{agent}.jsonl")


def _slot_file(agent_id: str) -> str:
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
    return os.path.join(root, os.path.basename(agent_id))


def _holders(raw: str) -> list:
    """The slot file's holders; a bare count (an older file) reads as that many unnamed ones."""
    raw = raw.strip()
    if raw.isdigit():
        return ["child"] * int(raw)
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(h) for h in data] if isinstance(data, list) else []


def claim_slot(agent_id: str, limit: int, holder: str = "child") -> tuple:
    """Seat `holder` as one more running child of `agent_id`. Returns `(claimed, holders)`:
    False once `limit` are still running, with `holders` naming them so the parent can
    wait for one to report (`release_slot`) and re-launch."""
    # Parallel launches from one message run their hooks concurrently.
    with open(_slot_file(agent_id), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        holders = _holders(f.read())
        if len(holders) >= limit:
            return False, holders
        holders.append(holder)
        f.seek(0)
        f.truncate()
        f.write(json.dumps(holders))
    return True, holders


def release_slot(agent_id: str, holder: str = "") -> bool:
    """Free the slot `holder` holds under `agent_id` (the oldest when unnamed or unknown);
    False when none is held."""
    path = _slot_file(agent_id)
    if not os.path.exists(path):
        return False
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        holders = _holders(f.read())
        if not holders:
            return False
        holders.pop(holders.index(holder) if holder in holders else 0)
        f.seek(0)
        f.truncate()
        f.write(json.dumps(holders))
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
    # The parent is a stage agent only for an `sdlc:*` agent type; the main thread and a
    # non-sdlc subagent (continuous mode's cycle agent) both launch stages as an orchestrator.
    parent = sdlc_role(data.get("agent_type")) if data.get("agent_id") else ""
    if not parent:
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
    if child:
        deny(f"a stage agent never launches another stage (sdlc:{child}); report it in your "
             "SDLC-RESULT/handoff and the orchestrator runs it.")
        return 0
    if tool_input.get("subagent_type") == "Explore" and parent in policy["explore"]:
        return 0
    warning = ""
    if parent == DESIGN_REVIEW:
        parent = design_review_role(
            prompt_header(first_prompt(parent_transcript(data))).get("role"), policy["fanout"])
        if parent is None:
            parent = stricter_fanout_role(policy["fanout"])
            warning = (f"the parent's ROLE header is unreadable; applying the stricter "
                       f"{parent} fan-out policy.")
    fanout = policy["fanout"].get(parent) or {}
    suffix = f" ({warning})" if warning else ""
    if not fanout.get("allowed"):
        if parent in policy["fanout"]:
            deny(f"{parent} reviews in a single pass; work the axes yourself.{suffix}")
        elif parent in policy["explore"]:
            deny(f"{parent} may launch only the read-only Explore agent; do the rest yourself.")
        else:
            deny(f"{parent} launches no subagents; do the work yourself.")
        return 0
    limit = int(fanout.get("maxChildren") or 0)
    claimed, holders = claim_slot(str(data["agent_id"]), limit, fanout_label(header, tool_input))
    if not claimed:
        retry = json.dumps({"waiting_on": holders, "hint": RETRY_HINT})
        deny(f"{parent} may run at most {limit} fan-out children at once; {len(holders)} still "
             f"running. retry_after={retry}{suffix}")
        return 0
    set_model(tool_input, (fanout.get("axisModels") or {}).get(header.get("axis"))
              or fanout.get("model"), warning)
    return 0


if __name__ == "__main__":
    run(main)
