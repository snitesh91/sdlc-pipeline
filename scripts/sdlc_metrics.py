#!/usr/bin/env python3
"""sdlc_metrics.py — cost and outcome report over the per-agent records the plugin's hooks store.

`report` prints one JSON object; `backfill` builds records from existing transcripts with the
same parser the hooks use. Records: `hooks/_metrics.py`.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "hooks"))
import _metrics  # noqa: E402
from _common import load_json, repo_config  # noqa: E402

TOKEN_KINDS = ("input", "output", "cache_write", "cache_read")


def load_records(repo: str | None) -> list:
    """Every stored record, or only `repo`'s ("owner/name")."""
    paths = ([_metrics.store_path(repo)] if repo
             else glob.glob(os.path.join(_metrics.store_dir(), "*.jsonl")))
    records = []
    for path in paths:
        try:
            with open(path) as f:
                records += [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            continue
    return records


def attribute_children(records: list) -> list:
    """A fan-out child inherits its parent agent's epic/issue/run; its role becomes
    `<parent-role>:fanout`."""
    by_agent = {r.get("agent_id"): r for r in records}
    own_role = {id(r): r.get("role") for r in records}
    for r in records:
        parent = by_agent.get((r.get("parent") or {}).get("agent_id"))
        if parent and parent is not r:
            r["role"] = f"{own_role[id(parent)]}:fanout"
            for key in ("epic", "issue", "run_id"):
                if key in parent:
                    r.setdefault(key, parent[key])
    return records


def _blank() -> dict:
    return {"runs": 0, "tokens": dict.fromkeys(TOKEN_KINDS, 0), "est_cost_usd": 0.0,
            "tool_calls": 0, "peak_context": 0, "durations": [], "outcomes": {}}


def _add(group: dict, tokens: dict, cost: float, record: dict) -> None:
    group["runs"] += 1
    for kind in TOKEN_KINDS:
        group["tokens"][kind] += tokens.get(kind, 0)
    group["est_cost_usd"] += cost
    group["tool_calls"] += record.get("tool_calls") or 0
    group["peak_context"] = max(group["peak_context"], record.get("peak_context") or 0)
    if record.get("duration_s") is not None:
        group["durations"].append(record["duration_s"])
    if record.get("outcome"):
        group["outcomes"][record["outcome"]] = group["outcomes"].get(record["outcome"], 0) + 1


def _finish(group: dict) -> dict:
    durations, outcomes = group.pop("durations"), group["outcomes"]
    group["est_cost_usd"] = round(group["est_cost_usd"], 2)
    group["median_duration_s"] = statistics.median(durations) if durations else None
    verdicts = outcomes.get("clean", 0) + outcomes.get("rework", 0)
    group["rework_rate"] = round(outcomes.get("rework", 0) / verdicts, 2) if verdicts else None
    return group


def report(records: list, epic: int | None = None, since: str | None = None,
           by: str = "role", run: str | None = None) -> dict:
    """Totals plus per-group runs, tokens, cost, tool calls, peak context, median duration
    and outcome counts; `run` keeps one control-plane run (`next-action --run-id`)."""
    records = attribute_children(records)
    if since:
        records = [r for r in records if (r.get("ts") or "") >= since]
    if epic is not None:
        records = [r for r in records if epic in (r.get("epic"), r.get("issue"))]
    if run:
        records = [r for r in records if r.get("run_id") == run]
    totals, groups = _blank(), {}
    for r in records:
        per_model = r.get("tokens") or {}
        summed = {k: sum(t.get(k, 0) for t in per_model.values()) for k in TOKEN_KINDS}
        _add(totals, summed, r.get("est_cost_usd", 0), r)
        if by == "model":
            for model, tokens in per_model.items():
                _add(groups.setdefault(model, _blank()), tokens,
                     _metrics.cost_usd(model, tokens), r)
        else:
            key = str(r.get(by) or "unattributed")
            _add(groups.setdefault(key, _blank()), summed, r.get("est_cost_usd", 0), r)
    return {"filters": {"epic": epic, "since": since, "by": by, "run": run},
            "totals": _finish(totals),
            "groups": {k: _finish(g) for k, g in sorted(groups.items())}}


def _session_repo(transcript: str, fallback: str) -> str:
    """The repo a session ran in: the config found from its first recorded cwd."""
    try:
        with open(transcript) as f:
            for line in f:
                cwd = json.loads(line).get("cwd")
                if cwd:
                    config = repo_config(cwd)
                    return (load_json(config).get("repo") if config else None) or fallback
    except (OSError, ValueError):
        pass
    return fallback


def backfill(projects_dir: str, repo: str | None = None) -> dict:
    """Append records for every session and subagent transcript not already stored."""
    stored = {(r.get("session_id"), r.get("agent_id")) for r in load_records(None)}
    added = skipped = 0
    for main in sorted(glob.glob(os.path.join(projects_dir, "*.jsonl"))):
        session = os.path.basename(main)[:-len(".jsonl")]
        session_repo = repo or _session_repo(main, os.path.basename(projects_dir.rstrip("/")))
        items = [(session, "orchestrator", main)]
        for sub in sorted(glob.glob(os.path.join(projects_dir, session, "subagents",
                                                 "agent-*.jsonl"))):
            agent_type = load_json(sub[:-len(".jsonl")] + ".meta.json").get("agentType") or ""
            # Agents from before the plugin were named `sdlc-<role>`.
            if agent_type.startswith("sdlc-"):
                agent_type = "sdlc:" + agent_type[len("sdlc-"):]
            items.append((os.path.basename(sub)[len("agent-"):-len(".jsonl")], agent_type, sub))
        for agent, agent_type, path in items:
            if (session, agent) in stored:
                skipped += 1
                continue
            try:
                record = (_metrics.main_record(path, repo=session_repo, session_id=session)
                          if agent_type == "orchestrator" else
                          _metrics.subagent_record(path, repo=session_repo, session_id=session,
                                                   agent_id=agent, agent_type=agent_type))
            except (OSError, ValueError):
                continue
            if record["turns"]:
                _metrics.append(record)
                added += 1
    return {"added": added, "skipped_existing": skipped, "store": _metrics.store_dir()}


def _current_repo() -> str | None:
    config = repo_config(os.getcwd())
    return load_json(config).get("repo") if config else None


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sdlc_metrics.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("report", help="Totals and per-group cost/tokens/outcomes")
    p.add_argument("--repo", default=None, help="owner/name (default: this repo, else all)")
    p.add_argument("--epic", type=int, default=None)
    p.add_argument("--since", default=None, help="YYYY-MM-DD")
    p.add_argument("--by", default="role", choices=["role", "model", "issue", "epic"])
    p.add_argument("--run", default=None, help="One run's records (the run id next-action got)")
    p = sub.add_parser("backfill", help="Build records from existing transcripts")
    p.add_argument("--projects-dir", required=True,
                   help="One project's transcript dir, e.g. ~/.claude/projects/<slug>")
    p.add_argument("--repo", default=None, help="owner/name (default: from each session's cwd)")
    args = parser.parse_args(argv)
    if args.command == "report":
        repo = args.repo or _current_repo()
        result = {"repo": repo, **report(load_records(repo), args.epic, args.since, args.by,
                                              args.run)}
    else:
        result = backfill(os.path.expanduser(args.projects_dir), args.repo)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
