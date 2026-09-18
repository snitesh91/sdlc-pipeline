"""sdlc_metrics.py: report aggregation and backfill from transcripts, against a temp store."""
import json

import pytest

import sdlc_metrics as m


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    return tmp_path / "data" / "metrics"


def _rec(agent_id, role, cost, tokens=None, **extra):
    return {"ts": "2026-09-18T10:00:00Z", "repo": "o/r", "session_id": "s", "agent_id": agent_id,
            "agent_type": f"sdlc:{role}", "role": role, "est_cost_usd": cost, "duration_s": 60,
            "tokens": tokens or {"claude-opus-5": {"input": 10, "output": 1, "cache_write": 0,
                                                   "cache_read": 0}}, **extra}


RECORDS = [
    _rec("d1", "development", 2.0, {"claude-sonnet-5": {"input": 100, "output": 10,
                                                        "cache_write": 0, "cache_read": 1000}},
         issue=13, epic=9, outcome="done", duration_s=300),
    _rec("p1", "pr-review", 1.0, issue=13, epic=9, outcome="rework", duration_s=100),
    _rec("p2", "pr-review", 1.5, issue=13, epic=9, outcome="clean", duration_s=200),
    {**_rec("c1", "general-purpose", 0.25), "agent_type": "general-purpose",
     "parent": {"agent_id": "p1"}},
    _rec("o1", "orchestrator", 3.0, epic=9),
    _rec("x1", "development", 9.0, issue=40, epic=10, outcome="done",
         ts="2026-09-01T10:00:00Z"),
]


def test_report_groups_by_role_for_one_epic():
    out = m.report([dict(r) for r in RECORDS], epic=9)
    assert out["totals"]["runs"] == 5 and out["totals"]["est_cost_usd"] == 7.75
    groups = out["groups"]
    assert set(groups) == {"development", "pr-review", "pr-review:fanout", "orchestrator"}
    assert groups["pr-review"]["runs"] == 2 and groups["pr-review"]["rework_rate"] == 0.5
    assert groups["pr-review"]["outcomes"] == {"rework": 1, "clean": 1}
    assert groups["pr-review"]["median_duration_s"] == 150
    assert groups["development"]["tokens"] == {"input": 100, "output": 10, "cache_write": 0,
                                               "cache_read": 1000}


def test_report_by_model_prices_each_model_separately():
    groups = m.report([dict(r) for r in RECORDS], epic=9, by="model")["groups"]
    assert set(groups) == {"claude-opus-5", "claude-sonnet-5"}
    assert groups["claude-sonnet-5"]["est_cost_usd"] == round(
        (3 * (100 + 0.1 * 1000) + 15 * 10) / 1e6, 2)
    assert groups["claude-opus-5"]["runs"] == 4


def test_report_since_and_by_epic():
    out = m.report([dict(r) for r in RECORDS], since="2026-09-10", by="epic")
    assert set(out["groups"]) == {"9"} and out["totals"]["runs"] == 5
    assert m.report([dict(r) for r in RECORDS], by="epic")["groups"]["10"]["runs"] == 1


def test_report_sums_tool_calls_and_keeps_the_peak_context():
    records = [_rec("d1", "development", 1.0, epic=9, tool_calls=30, peak_context=150_000),
               _rec("d2", "development", 1.0, epic=9, tool_calls=12, peak_context=400_000),
               _rec("d3", "development", 1.0, epic=9)]
    dev = m.report(records, epic=9)["groups"]["development"]
    assert (dev["tool_calls"], dev["peak_context"]) == (42, 400_000)


def test_report_run_keeps_one_run_including_its_fanout_children():
    records = [_rec("p1", "pr-review", 1.0, epic=9, run_id="r-1"),
               {**_rec("c1", "general-purpose", 0.5), "parent": {"agent_id": "p1"}},
               _rec("p2", "pr-review", 2.0, epic=9, run_id="r-2")]
    out = m.report(records, run="r-1")
    assert out["filters"]["run"] == "r-1" and out["totals"]["runs"] == 2
    assert out["totals"]["est_cost_usd"] == 1.5


def test_report_cli_reads_the_repo_store(store, capsys):
    store.mkdir(parents=True)
    (store / "o__r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in RECORDS))
    assert m.main(["report", "--repo", "o/r", "--epic", "9", "--by", "issue"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo"] == "o/r" and out["groups"]["13"]["runs"] == 4
    assert m.main(["report", "--repo", "o/r", "--run", "no-such-run"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["runs"] == 0


def _line(**entry):
    return json.dumps(entry) + "\n"


def _assistant(msg_id, model, text="", sidechain=False):
    return _line(type="assistant", timestamp="2026-09-18T10:00:05Z", isSidechain=sidechain,
                 message={"id": msg_id, "role": "assistant", "model": model,
                          "usage": {"input_tokens": 10, "output_tokens": 20},
                          "content": [{"type": "text", "text": text}]})


@pytest.fixture
def projects(tmp_path):
    """One session: a main transcript plus one pre-plugin `sdlc-pr-review` subagent."""
    proj = tmp_path / "proj"
    subs = proj / "sess1" / "subagents"
    subs.mkdir(parents=True)
    (proj / "sess1.jsonl").write_text(
        _line(type="user", cwd=str(tmp_path), timestamp="2026-09-18T10:00:00Z",
              message={"role": "user", "content": "/sdlc:run 9"})
        + _assistant("o1", "claude-opus-5"))
    (subs / "agent-ab.jsonl").write_text(
        _line(type="user", timestamp="2026-09-18T10:00:00Z",
              message={"role": "user", "content": "ROLE: pr-review ISSUE: 13 EPIC: 9\nReview"})
        + _assistant("r1", "claude-opus-5",
                     'SDLC-RESULT: {"issue": 13, "stage": "pr-review", "outcome": "clean"}'))
    (subs / "agent-ab.meta.json").write_text(json.dumps({"agentType": "sdlc-pr-review"}))
    (subs / "agent-empty.jsonl").write_text("")
    return proj


def test_backfill_builds_records_once(store, projects, capsys):
    assert m.main(["backfill", "--projects-dir", str(projects), "--repo", "o/r"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 2
    m.main(["backfill", "--projects-dir", str(projects), "--repo", "o/r"])
    assert json.loads(capsys.readouterr().out) == {
        "added": 0, "skipped_existing": 2, "store": str(store)}
    records = {r["agent_type"]: r for r in m.load_records("o/r")}
    assert set(records) == {"orchestrator", "sdlc:pr-review"}
    review = records["sdlc:pr-review"]
    assert (review["outcome"], review["epic"], review["role"]) == ("clean", 9, "pr-review")
    assert records["orchestrator"]["session_id"] == "sess1"


def test_backfill_takes_the_repo_from_the_session_cwd(store, projects, capsys):
    m.main(["backfill", "--projects-dir", str(projects)])
    # conftest points $SDLC_CONFIG at the sample config, whose repo is owner/repo.
    assert len(m.load_records("owner/repo")) == 2
