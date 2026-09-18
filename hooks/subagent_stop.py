"""SubagentStop: an sdlc:* stage agent must end its final message with an SDLC-RESULT line;
every agent that finishes in an sdlc repo gets a metrics record."""
import json
import os
import sys

import _metrics
from _common import (RESULT_FORMAT, load_json, message_text, read_input, repo_config, run,
                     run_states, sdlc_result, sdlc_role)

TAIL_BYTES = 2_000_000


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
    record(data, load_json(config_path), text if isinstance(text, str) else None)
    return 0


if __name__ == "__main__":
    run(main)
