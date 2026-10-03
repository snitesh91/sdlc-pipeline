"""Moving a unit between laptop and cloud: `go-cloud` / `go-local` (anchor, refusals, exec vs
JSON), cloud-session self-registration from `next-action`, and `end-cloud-session`."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_placement import CLOUD, PlaceGh, _cli
from tests.test_v2_phase_tasks import repo  # noqa: F401

URL = "https://claude.ai/code/session_01AAA"


def _session(sid="session_01AAA"):
    return (f"☁️ live\n\n<!-- sdlc:cloud-session id={sid} url=https://claude.ai/code/{sid} "
            f"launched=2026-10-03T10:00:00Z -->")


def _ended(sid="session_01AAA", outcome="stopped"):
    return f"<!-- sdlc:cloud-session-end id={sid} outcome={outcome} ended=2026-10-03T11:00:00Z -->"


def _gh(initiative=(), epic=(), epic_comments=(), init_comments=(), extra=()):
    """Initiative #6 > Epic #9 > Task #10; Initiative #6 > Product-Roadmap Task #7;
    parentless Task #20."""
    return PlaceGh([
        {"number": 6, "labels": ["type:initiative", *initiative], "comments": list(init_comments)},
        {"number": 7, "labels": ["type:task"], "parent": 6},
        {"number": 9, "labels": ["type:epic", *epic], "parent": 6,
         "comments": list(epic_comments)},
        {"number": 10, "labels": ["type:task"], "parent": 9},
        {"number": 20, "labels": ["type:task"]},
        *extra,
    ])


class Exec:
    def __init__(self):
        self.calls = []

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))


def _no_worktrees(argv):
    return ""


@pytest.fixture(autouse=True)
def runs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _go(gh, n, where="cloud", tty=False, answer="y", **kw):
    ex = Exec()
    asked = []

    def ask(q):
        asked.append(q)
        return answer
    result = s.cmd_go(gh, n, where, runner=_no_worktrees, isatty=lambda: tty, execvp=ex,
                      ask=ask, **kw)
    return result, ex, asked


def _run_state(n, minutes_ago=1, **state):
    s._write_run_state(n, {"run_id": "r", "terminal": [], **state})
    t = time.time() - minutes_ago * 60
    os.utime(s._run_state_path(n), (t, t))


# --- anchor ---------------------------------------------------------------------------------

@pytest.mark.parametrize("n, anchor", [(6, 6), (9, 9), (10, 9), (7, 6), (20, 20)])
def test_go_cloud_moves_the_anchor_of_any_issue(n, anchor):
    gh = _gh()
    result, ex, _ = _go(gh, n, yes=True)
    assert result["anchor"] == anchor and result["place"]["placed"] == "cloud"
    assert CLOUD in gh.issues[anchor]["labels"]
    assert result["command"] == f"claude --cloud '/sdlc:run {anchor}'" and ex.calls == []


def test_a_child_is_never_moved_without_confirmation_off_a_tty():
    gh = _gh()
    result, _, _ = _go(gh, 10)
    assert (result["ok"], result["error"]) == (False, "anchor_unconfirmed")
    assert "Epic #9" in result["reason"] and CLOUD not in gh.issues[9]["labels"]
    # Positive control: the anchor itself needs no confirmation.
    assert _go(gh, 9)[0]["place"]["placed"] == "cloud"


def test_on_a_tty_the_operator_confirms_the_anchor():
    gh = _gh()
    declined, ex, asked = _go(gh, 7, tty=True, answer="n")
    assert declined["error"] == "cancelled" and ex.calls == []
    assert asked == ["#7 belongs to Initiative #6 — move #6 to the cloud? [y/N] "]
    assert CLOUD not in gh.issues[6]["labels"]
    accepted, ex, _ = _go(gh, 7, tty=True, answer="y")
    assert accepted["exec"] is True and ex.calls == [("claude", ["claude", "--cloud",
                                                                 "/sdlc:run 6"])]


def test_go_local_asks_about_the_laptop():
    _, _, asked = _go(_gh(epic=[CLOUD]), 10, where="local", tty=True, answer="n")
    assert asked == ["#10 belongs to Epic #9 — move #9 to the laptop? [y/N] "]


# --- go-cloud refusals ----------------------------------------------------------------------

def test_go_cloud_refuses_a_closed_anchor():
    gh = _gh()
    gh.issues[9]["state"] = "CLOSED"
    result, _, _ = _go(gh, 9)
    assert (result["error"], result["reason"]) == ("closed", "#9 is closed")
    assert CLOUD not in gh.issues[9]["labels"]


@pytest.mark.parametrize("where", ["cloud", "local"])
def test_an_epic_placed_through_its_initiative_refuses_to_move(where):
    result, _, _ = _go(_gh(initiative=[CLOUD]), 9, where=where)
    assert result["error"] == "inherited" and result["inherited_from"] == 6
    assert "move Initiative #6 instead" in result["reason"]


def test_go_cloud_refuses_a_unit_with_a_live_session_and_names_it():
    gh = _gh(epic=[CLOUD], epic_comments=[_session()])
    result, ex, _ = _go(gh, 9, tty=True)
    assert result["error"] == "cloud_session_live" and ex.calls == []
    assert URL in result["reason"] and "attach: claude --cloud session_01AAA" in result["reason"]


def test_go_cloud_relaunches_a_cloud_unit_whose_session_ended():
    gh = _gh(epic=[CLOUD], epic_comments=[_session(), _ended()])
    result, _, _ = _go(gh, 9)
    assert result["place"]["already"] is True and result["command"]


def test_go_cloud_refuses_while_a_laptop_run_drives_the_unit_unless_forced():
    gh = _gh()
    _run_state(9, minutes_ago=5)
    result, _, _ = _go(gh, 9)
    assert result["error"] == "local_run_live" and "5 min ago" in result["reason"]
    assert [r["issue"] for r in result["local_runs"]] == [9]
    assert CLOUD not in gh.issues[9]["labels"]
    forced, _, _ = _go(gh, 9, force=True)
    assert forced["place"]["placed"] == "cloud"


def test_an_initiative_is_refused_while_a_laptop_run_drives_one_of_its_epics():
    _run_state(9)
    assert _go(_gh(), 6)[0]["error"] == "local_run_live"


@pytest.mark.parametrize("minutes_ago, state", [(31, {}), (1, {"closed": True})])
def test_a_stale_or_closed_run_state_is_no_live_run(minutes_ago, state):
    _run_state(9, minutes_ago=minutes_ago, **state)
    assert _go(_gh(), 9)[0]["place"]["placed"] == "cloud"


def test_the_local_run_window_is_configurable(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["placement"], "localRunMinutes", 60)
    _run_state(9, minutes_ago=45)
    assert _go(_gh(), 9)[0]["error"] == "local_run_live"


def test_go_cloud_passes_place_worktree_refusal_through_and_force(repo):
    gh = _gh()
    gh.issues[10]["stage"] = "development"
    s.cmd_worktree_add(gh, 10, repo_path=str(repo), base="origin/main")
    kw = dict(isatty=lambda: False, execvp=Exec(), repo_path=str(repo))
    refused = s.cmd_go(gh, 9, "cloud", **kw)
    assert refused["error"] == "worktrees" and refused["ok"] is False
    assert "invisible to the other side" in refused["reason"]
    forced = s.cmd_go(gh, 9, "cloud", force=True, **kw)
    assert forced["place"]["forced"] is True


# --- go-local -------------------------------------------------------------------------------

def test_go_local_unplaces_and_returns_the_remote_control_command():
    gh = _gh(epic=[CLOUD], epic_comments=[_session(), _ended()])
    result, _, _ = _go(gh, 9, where="local")
    assert result["place"]["placed"] == "local" and CLOUD not in gh.issues[9]["labels"]
    model = s.orchestrator_model()
    assert result["command"] == (f"claude --model {model} --remote-control sdlc-9 "
                                 f"'/sdlc:run 9'")


def test_go_local_execs_on_a_tty_with_the_name_before_the_prompt(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["orchestrator"], "model", "opus")
    result, ex, _ = _go(_gh(), 9, where="local", tty=True)
    assert result["place"]["already"] is True
    assert ex.calls == [("claude", ["claude", "--model", "opus", "--remote-control", "sdlc-9",
                                    "/sdlc:run 9"])]


def test_go_local_refuses_a_live_cloud_session_unless_forced():
    gh = _gh(epic=[CLOUD], epic_comments=[_session()])
    result, _, _ = _go(gh, 9, where="local")
    assert result["error"] == "cloud_session_live" and "stop it on claude.ai" in result["reason"]
    assert CLOUD in gh.issues[9]["labels"] and len(gh.issues[9]["comments"]) == 1
    forced, _, _ = _go(gh, 9, where="local", force=True)
    assert forced["ended"]["outcome"] == "abandoned" and forced["place"]["placed"] == "local"
    assert "id=session_01AAA outcome=abandoned" in gh.issues[9]["comments"][-1]
    assert s.live_cloud_session(gh.issue_view(9)["comments"]) is None


def test_orchestrator_model_follows_the_config_else_the_policy(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["orchestrator"], "model", " ")
    assert s.orchestrator_model() == s._MODEL_POLICY["orchestrator"]["model"]
    monkeypatch.setitem(s.PIPELINE["orchestrator"], "model", "claude-opus-5-5")
    assert s.orchestrator_model() == "claude-opus-5-5"


# --- the CLI ----------------------------------------------------------------------------------

def test_the_cli_prints_the_command_off_a_tty_and_exits_1_on_a_refusal(monkeypatch):
    gh = _gh()
    monkeypatch.setattr(s, "unit_worktrees", lambda *a, **k: [])
    code, out = _cli(monkeypatch, gh, ["go-cloud", "9"])
    assert code == 0 and out["command"] == "claude --cloud '/sdlc:run 9'"
    code, out = _cli(monkeypatch, gh, ["go-local", "10"])
    assert code == 1 and out["error"] == "anchor_unconfirmed"
    code, out = _cli(monkeypatch, gh, ["go-local", "10", "--yes"])
    assert code == 0 and out["anchor"] == 9 and out["place"]["placed"] == "local"


# --- self-registration ----------------------------------------------------------------------

def _cloud_next(monkeypatch, gh, n=9, sid="cse_01REG", remote=True, result=None):
    if remote:
        monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    if sid:
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", sid)
    monkeypatch.setattr(s, "decide_next_action",
                        lambda *a, **k: dict(result or {"action": "none", "reason": "idle"}))
    return s.cmd_next_action(gh, argparse.Namespace(epic=n, repo_path="."))


def test_next_action_in_a_cloud_session_registers_it_once(monkeypatch):
    gh = _gh(epic=[CLOUD])
    url = "https://claude.ai/code/session_01REG"

    first = _cloud_next(monkeypatch, gh)
    second = _cloud_next(monkeypatch, gh)

    assert first["cloud_session"] == {"issue": 9, "registered": True, "id": "session_01REG",
                                      "url": url}
    assert first["action"] == "none" and "warnings" not in first
    assert second["cloud_session"]["registered"] is False and len(gh.issues[9]["comments"]) == 1
    assert (f"<!-- sdlc:cloud-session id=session_01REG url={url} launched="
            in gh.issues[9]["comments"][-1])
    assert s.live_cloud_session(gh.issue_view(9)["comments"])["id"] == "session_01REG"


def test_a_new_session_registers_over_an_older_one(monkeypatch):
    gh = _gh(epic=[CLOUD], epic_comments=[_session(), _ended()])
    assert _cloud_next(monkeypatch, gh)["cloud_session"]["registered"] is True
    assert s.latest_cloud_session(gh.issue_view(9)["comments"])["id"] == "session_01REG"


def test_an_initiative_registers_on_itself_and_its_epics_add_nothing(monkeypatch):
    gh = _gh(initiative=[CLOUD])
    assert _cloud_next(monkeypatch, gh, n=6)["cloud_session"]["issue"] == 6
    assert _cloud_next(monkeypatch, gh, n=9)["cloud_session"] == {
        "issue": 6, "registered": False, "id": "session_01REG",
        "url": "https://claude.ai/code/session_01REG"}
    assert len(gh.issues[6]["comments"]) == 1 and gh.issues[9]["comments"] == []


def test_registration_is_a_no_op_on_the_laptop(monkeypatch):
    gh = _gh()
    result = _cloud_next(monkeypatch, gh, remote=False)
    assert "cloud_session" not in result and gh.issues[9]["comments"] == []


def test_registration_is_a_no_op_without_the_session_id(monkeypatch):
    gh = _gh(epic=[CLOUD])
    result = _cloud_next(monkeypatch, gh, sid=None)
    assert "cloud_session" not in result and gh.issues[9]["comments"] == []


@pytest.mark.parametrize("sid, broken", [("cse_01REG", True), ("not-an-id", False)])
def test_a_failed_registration_is_a_warning_not_an_error(monkeypatch, sid, broken):
    gh = _gh(epic=[CLOUD])
    if broken:
        def refuse(*a, **k):
            raise s.GhError("HTTP 403")
        monkeypatch.setattr(gh, "issue_comment", refuse)

    result = _cloud_next(monkeypatch, gh, sid=sid, result={"action": "none", "reason": "x",
                                                           "warnings": ["earlier"]})

    assert result["action"] == "none" and "cloud_session" not in result
    assert result["warnings"][0] == "earlier"
    assert result["warnings"][1].startswith("cloud session not registered for #9")


# --- end-cloud-session ------------------------------------------------------------------------

def test_cloud_sessions_pair_each_end_marker_with_its_session():
    comments = [{"body": _session("session_A")}, {"body": _ended("session_A")},
                {"body": _session("session_B")}, {"body": "a human comment"},
                {"body": _ended("session_C")}]
    sessions = s.cloud_sessions(comments)
    assert [x["id"] for x in sessions] == ["session_A", "session_B", "session_C"]
    assert sessions[0]["ended"]["outcome"] == "stopped" and sessions[1]["ended"] is None
    # An end marker for an unrecorded session leaves the latest recorded one live.
    assert s.live_cloud_session(comments)["id"] == "session_B"
    assert s.live_cloud_session([*comments, {"body": _ended("session_B")}]) is None


def test_end_cloud_session_marks_the_latest_session_and_is_idempotent():
    gh = _gh(epic=[CLOUD], epic_comments=[_session("session_A")])
    first = s.cmd_end_cloud_session(gh, 9, "closed", note="Epic merged to main")
    again = s.cmd_end_cloud_session(gh, 9, "closed")
    assert first == {"issue": 9, "ended": True, "id": "session_A", "outcome": "closed"}
    assert "<!-- sdlc:cloud-session-end id=session_A outcome=closed ended=" in \
        gh.issues[9]["comments"][-1]
    assert again["already_ended"] is True and len(gh.issues[9]["comments"]) == 2


@pytest.mark.parametrize("n, holder", [(6, 6), (20, 20), (7, 6)])
def test_end_cloud_session_lands_on_initiatives_and_parentless_issues(n, holder):
    gh = _gh(initiative=[CLOUD], extra=[])
    gh.issues[20]["labels"].append(CLOUD)
    result = s.cmd_end_cloud_session(gh, n, "waiting-human")
    assert result["issue"] == holder and result["id"] == "unknown"
    assert "outcome=waiting-human" in gh.issues[holder]["comments"][-1]


def test_end_cloud_session_defaults_to_this_cloud_sessions_id(monkeypatch):
    gh = _gh(epic=[CLOUD], epic_comments=[_session("session_OTHER")])
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01ME")
    assert s.cmd_end_cloud_session(gh, 10, "stopped")["id"] == "session_01ME"
    # Another session's end marker leaves the older one live.
    assert s.live_cloud_session(gh.issue_view(9)["comments"])["id"] == "session_OTHER"


def test_end_cloud_session_validates_its_outcome_and_cli(monkeypatch):
    gh = _gh(epic=[CLOUD])
    with pytest.raises(s.GhError, match="--outcome"):
        s.cmd_end_cloud_session(gh, 9, "done")
    code, out = _cli(monkeypatch, gh, ["end-cloud-session", "9", "--outcome", "abandoned"])
    assert code == 0 and out["outcome"] == "abandoned"


# --- what is gone ---------------------------------------------------------------------------

def test_an_initiative_run_drives_its_epics_even_with_the_old_cloud_key(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["placement"], "initiativeEpics", "cloud")
    gh = _gh()
    gh.issues[7]["state"] = "CLOSED"
    gh.issues[10]["state"] = "CLOSED"
    gh.blocked_by = lambda n: []
    result = s.decide_next_action(gh, 6, runner=_no_worktrees)
    assert result["action"] in ("cut-phase-tasks", "run-epic") and result["epic"] == 9


def test_a_config_with_the_removed_cloud_keys_still_loads(tmp_path):
    sample = Path(s.PLUGIN_ROOT) / "sdlc.config.sample.json"
    config = json.loads(sample.read_text())
    config.setdefault("parallelism", {})["cloudSessions"] = 3
    config["pipeline"]["placement"].update(initiativeEpics="cloud", stallMinutes=90)
    path = tmp_path / "sdlc-pipeline.config.json"
    path.write_text(json.dumps(config))
    env = {**os.environ, "SDLC_CONFIG": str(path), "GITHUB_TOKEN": "x"}
    proc = subprocess.run([sys.executable, str(Path(s.__file__)), "show-config"],
                          capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["placement"]["cloudLabel"] == CLOUD and out["placement"]["localRunMinutes"] == 30
