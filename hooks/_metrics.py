"""Per-agent cost records: one streamed pass over a transcript becomes one JSON line in the store.

Shared by the SubagentStop / SessionEnd hooks and `scripts/sdlc_metrics.py`.
"""
import fcntl
import json
import os
from datetime import datetime

from _common import load_json, message_text, plugin_data_dir, prompt_header, sdlc_result, sdlc_role

# USD per million tokens (input, output); cache writes and reads are priced off input.
PRICES_PER_MTOK = {"opus": (5, 25), "sonnet": (3, 15), "haiku": (1, 5), "fable": (10, 50)}
CACHE_WRITE_X, CACHE_READ_X = 1.25, 0.1
USAGE_KEYS = {"input_tokens": "input", "output_tokens": "output",
              "cache_creation_input_tokens": "cache_write", "cache_read_input_tokens": "cache_read"}


def model_family(model: str) -> str:
    return next((f for f in PRICES_PER_MTOK if f in str(model).lower()), "")


def cost_usd(model: str, tokens: dict) -> float:
    """Estimated cost of `tokens` ({input, output, cache_write, cache_read}); 0 for an unknown model."""
    price_in, price_out = PRICES_PER_MTOK.get(model_family(model), (0, 0))
    return (price_in * (tokens.get("input", 0) + CACHE_WRITE_X * tokens.get("cache_write", 0)
                        + CACHE_READ_X * tokens.get("cache_read", 0))
            + price_out * tokens.get("output", 0)) / 1e6


def _seconds(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def scan(path: str, main_thread: bool = False) -> dict:
    """Stream a transcript once: per-model tokens (one API message spans several lines with
    the same id, so each id counts once), turns, tool calls, peak context (the largest
    single request's input + cache tokens), first/last timestamp, the first prompt and the
    final assistant turn's text. `main_thread` skips sidechain lines."""
    usage, models, first_ts, last_ts, prompt, final = {}, {}, None, None, None, []
    tool_ids = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or (main_thread and entry.get("isSidechain")):
                continue
            ts = entry.get("timestamp")
            if isinstance(ts, str):
                first_ts, last_ts = first_ts or ts, ts
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user":
                final = []
                if prompt is None:
                    prompt = message_text(msg.get("content"))
            elif msg.get("role") == "assistant":
                content = msg.get("content")
                final.append(message_text(content))
                if isinstance(content, list):
                    tool_ids.update(b.get("id") for b in content if isinstance(b, dict)
                                    and b.get("type") == "tool_use" and b.get("id"))
                model, msg_id, used = msg.get("model"), msg.get("id"), msg.get("usage")
                if not msg_id or not isinstance(used, dict) or model == "<synthetic>":
                    continue
                models[msg_id] = model or "unknown"
                seen = usage.setdefault(msg_id, {})
                for key, name in USAGE_KEYS.items():
                    value = used.get(key)
                    if isinstance(value, int):
                        seen[name] = max(seen.get(name, 0), value)
    tokens = {}
    for msg_id, counts in usage.items():
        per_model = tokens.setdefault(models[msg_id], dict.fromkeys(USAGE_KEYS.values(), 0))
        for name, value in counts.items():
            per_model[name] += value
    start, end = _seconds(first_ts), _seconds(last_ts)
    peak = max((c.get("input", 0) + c.get("cache_write", 0) + c.get("cache_read", 0)
                for c in usage.values()), default=0)
    return {"tokens": tokens, "turns": len(usage), "tool_calls": len(tool_ids),
            "peak_context": peak, "first_ts": first_ts, "last_ts": last_ts,
            "duration_s": round(end - start, 1) if start is not None and end is not None else None,
            "prompt": prompt or "", "final_text": "\n".join(p for p in final if p)}


def epic_of(issue, states: list):
    """The epic whose run handed out `issue`, from this session's run states."""
    for state in states:
        if str(issue) in (state.get("in_flight") or {}) or issue in state.get("terminal", []):
            return state.get("epic")
    return None


def build_record(scanned: dict, *, repo: str, session_id: str, agent_id: str, agent_type: str,
                 meta: dict = None, final_text: str = None, states: list = ()) -> dict:
    """The stored record for one finished agent (the main thread: agent_type "orchestrator")."""
    header = prompt_header(scanned["prompt"])
    tokens = scanned["tokens"]
    record = {"ts": scanned["last_ts"], "repo": repo, "session_id": session_id,
              "agent_id": agent_id, "agent_type": agent_type,
              "role": header.get("role") or sdlc_role(agent_type) or agent_type,
              "models": sorted(tokens), "tokens": tokens,
              "est_cost_usd": round(sum(cost_usd(m, t) for m, t in tokens.items()), 4),
              "duration_s": scanned["duration_s"], "turns": scanned["turns"],
              "tool_calls": scanned["tool_calls"], "peak_context": scanned["peak_context"]}
    meta = meta or {}
    parent = {k: meta[m] for k, m in (("agent_id", "parentAgentId"), ("spawn_depth", "spawnDepth"),
                                      ("tool_use_id", "toolUseId")) if m in meta}
    if parent:
        record["parent"] = parent
    if sdlc_role(agent_type):
        result, _ = sdlc_result(scanned["final_text"] if final_text is None else final_text)
        if result:
            record.update(issue=result["issue"], stage=result["stage"], outcome=result["outcome"])
            record["role"] = header.get("role") or result["stage"]
    epic = header.get("epic")
    if epic and epic.isdigit():
        record["epic"] = int(epic)
    elif agent_type == "orchestrator" and states:
        record["epic"] = states[0].get("epic")
    elif "issue" in record and epic_of(record["issue"], states) is not None:
        record["epic"] = epic_of(record["issue"], states)
    run_id = next((s.get("run_id") for s in states
                   if "epic" in record and s.get("epic") == record["epic"]), None)
    if run_id:
        record["run_id"] = run_id
    return record


def store_dir() -> str:
    return os.path.join(plugin_data_dir(), "metrics")


def store_path(repo: str) -> str:
    return os.path.join(store_dir(), repo.replace("/", "__") + ".jsonl")


def append(record: dict) -> None:
    os.makedirs(store_dir(), exist_ok=True)
    with open(store_path(record["repo"]), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def subagent_record(transcript: str, *, repo: str, session_id: str, agent_id: str,
                    agent_type: str = None, final_text: str = None, states: list = ()) -> dict:
    """The record for one subagent transcript, with its sibling `.meta.json`."""
    meta = load_json(transcript[:-len(".jsonl")] + ".meta.json")
    return build_record(scan(transcript), repo=repo, session_id=session_id, agent_id=agent_id,
                        agent_type=agent_type or meta.get("agentType") or "", meta=meta,
                        final_text=final_text, states=states)


def main_record(transcript: str, *, repo: str, session_id: str, states: list = ()) -> dict:
    """The orchestrator's record: the session transcript's main-thread lines only."""
    return build_record(scan(transcript, main_thread=True), repo=repo, session_id=session_id,
                        agent_id=session_id, agent_type="orchestrator", states=states)
