"""SubagentStop: an sdlc:* stage agent must end its final message with an SDLC-RESULT line, and
a product/architecture/lld agent finishing `done` must have posted its handoff via post-comment;
every agent that finishes in an sdlc repo gets a metrics record."""
import json
import os
import re
import sys

import _metrics
from _common import (RESULT_FORMAT, first_prompt, load_json, message_text, prompt_header,
                     read_input, repo_config, run, run_states, sdlc_result, sdlc_role)
from agent_guard import release_slot

TAIL_BYTES = 2_000_000
COMMENT_ROLES = ("product", "architecture", "lld")
POST_COMMENT_RE = re.compile(r"post-comment\b.*--role[ =]+[\"']?(\w[\w-]*)")


def last_assistant_text(path: str):
    """Text of the final assistant turn (one API message may span several JSONL lines).

    Returns None when the transcript is unreadable, "" when it has no final assistant turn.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # first line is partial
    parts = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = msg.get("role") or entry.get("type")
        if role == "assistant":
            parts.append(message_text(msg.get("content")))
        elif role == "user":
            break
    return "\n".join(reversed([p for p in parts if p]))


def _blocks(entry: dict) -> list:
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = msg.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def posted_handoff(path: str, role: str):
    """Whether the agent's current round ran a successful `post-comment --role <role>`.

    A round starts at the last user turn that is not a tool result (a resume message counts).
    Returns None when the transcript is unreadable (the caller fails open).
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = raw.splitlines()[1 if size > TAIL_BYTES else 0:]
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    start = 0
    for i, entry in enumerate(entries):
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if (msg.get("role") or entry.get("type")) == "user" and not any(
                b.get("type") == "tool_result" for b in _blocks(entry)):
            start = i
    calls = set()
    for entry in entries[start:]:
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                m = POST_COMMENT_RE.search(str((block.get("input") or {}).get("command", "")))
                if m and m.group(1) == role:
                    calls.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                if not block.get("is_error") and '"posted": true' in message_text(block.get("content")):
                    return True
    return False


def release_fanout_slot(data: dict) -> None:
    """A finished fan-out child frees the slot it held under its parent (named by the
    sibling `.meta.json`), so the parent's next launch can seat the remaining axis."""
    path = str(data.get("agent_transcript_path") or "")
    if not path.endswith(".jsonl"):
        return
    parent = load_json(path[:-len(".jsonl")] + ".meta.json").get("parentAgentId")
    if parent:
        release_slot(str(parent), prompt_header(first_prompt(path)).get("axis", ""))


def record(data: dict, config: dict, final_text) -> None:
    """Append this agent's metrics record; never affects the stop decision."""
    try:
        session = str(data.get("session_id") or "")
        _metrics.append(_metrics.subagent_record(
            data["agent_transcript_path"], repo=config.get("repo") or "unknown",
            session_id=session, agent_id=str(data.get("agent_id") or ""),
            agent_type=data.get("agent_type"), final_text=final_text,
            states=run_states(config, session)))
    except Exception:
        pass


def main() -> int:
    data = read_input()
    config_path = repo_config(data.get("cwd") or os.getcwd())
    if not config_path:
        return 0
    release_fanout_slot(data)
    text = data.get("last_assistant_message")
    if sdlc_role(data.get("agent_type")) and not data.get("stop_hook_active"):
        if not isinstance(text, str):
            path = data.get("agent_transcript_path") or ""
            text = last_assistant_text(path) if path else None
        problem = sdlc_result(text)[1] if text is not None else None  # unreadable: fail open
        if problem:
            print(f"sdlc: {problem}. Finish your stage's exit actions now if any are undone, "
                  f"then end your final message with exactly one line:\n{RESULT_FORMAT}",
                  file=sys.stderr)
            return 2
        role = sdlc_role(data.get("agent_type"))
        result = sdlc_result(text)[0] if text is not None else None
        path = data.get("agent_transcript_path") or ""
        if (role in COMMENT_ROLES and result and result.get("outcome") == "done" and path
                and posted_handoff(path, role) is False):
            print(f"sdlc: no handoff comment was posted this round. Write it to a file (<= 2,000 "
                  f"chars) and run python3 \"$SDLC\" post-comment {result['issue']} --role {role} "
                  f"--body-file <file>, then end your final message with the same SDLC-RESULT line.",
                  file=sys.stderr)
            return 2
    record(data, load_json(config_path), text if isinstance(text, str) else None)
    return 0


if __name__ == "__main__":
    run(main)
