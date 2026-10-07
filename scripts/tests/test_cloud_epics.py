"""Initiative -> cloud Epics: which Epics a laptop Initiative run launches (cap, blockedBy,
footprints, idempotency), `launch-cloud-epic` against a fake `claude` CLI, `cloud-status`
stall detection, `nudge-cloud-epic` escalation and `end-cloud-session`."""
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh

CLOUD = "sdlc:cloud"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CloudGh(FakeGh):
    """FakeGh plus what the cloud commands read: `updatedAt`, comments with `createdAt`,
    open PRs and branch head commit times."""

    def __init__(self, issues, prs=None, commits=None):
        super().__init__(issues, prs)
        self.commits = dict(commits or {})
        self.created_labels = []

    def _node(self, n):
        return {**super()._node(n), "updatedAt": self.issues[n].get("updated")}

    def issue_view(self, n):
        view = super().issue_view(n)
        view["comments"] = [c if isinstance(c, dict) else {"body": c}
                            for c in self.issues[n]["comments"]]
        return view

    def open_prs(self):
        return [{"number": n, "headRefName": p.get("headRefName"), "title": p.get("title"),
                 "isDraft": p.get("isDraft", False), "updatedAt": p.get("updatedAt")}
                for n, p in self.prs.items() if p.get("state", "OPEN") == "OPEN"]

    def branch_last_commit_at(self, branch):
        return self.commits.get(branch)

    def ensure_label(self, label):
        self.created_labels.append(label)


def _launched(sid="session_01AAA", minutes=180):
    return (f"☁️ launched\n\n<!-- sdlc:cloud-session id={sid} "
            f"url=https://claude.ai/code/{sid} launched={_ago(minutes)} -->")


def _ended(sid="session_01AAA", minutes=60, outcome="waiting-human"):
    return f"<!-- sdlc:cloud-session-end id={sid} outcome={outcome} ended={_ago(minutes)} -->"


def _nudged(sid="session_01AAA", minutes=30, n=1):
    return f"<!-- sdlc:cloud-session-nudge id={sid} n={n} at={_ago(minutes)} -->"


def _initiative(*epics, extra=()):
    """Initiative #1 with Epics (dicts merged over `{"number": n, ...}`)."""
    nodes = [{"number": 1, "labels": ["type:initiative"]}]
    for e in epics:
        e = {"number": e} if isinstance(e, int) else e
        nodes.append({"labels": ["type:epic"], "parent": 1, **e})
    return CloudGh([*nodes, *extra])


class FakeGit:
    """`git` runner: `show origin/epic-<n>:.../lld.md` answers from `lld`, fetch is a no-op."""

    def __init__(self, lld=None):
        self.lld = lld or {}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "show" in argv:
            ref = argv[-1]
            for n, text in self.lld.items():
                if ref.startswith(f"origin/epic-{n}:"):
                    return text
            raise s.GhError(f"fatal: invalid object name {ref}")
        return ""


class FakeClaude:
    """The `claude` CLI: records argv, prints `output` (or raises `error`)."""

    def __init__(self, output="", error=None):
        self.output, self.error, self.calls = output, error, []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.error:
            raise self.error
        return self.output


LAUNCH_OUT = ("Creating cloud session...\nSession ID: session_01XYZ\n"
              "View: https://claude.ai/code/session_01XYZ\n")


@pytest.fixture
def cloud_mode(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["placement"], "initiativeEpics", "cloud")


def _survey(gh, candidates=None, git=None, **kw):
    issues = gh.issue_list()
    if candidates is None:
        candidates = [i["number"] for i in issues if s.is_epic(i)]
    return s.cloud_epic_survey(gh, issues, candidates, runner=git or FakeGit(), **kw)


def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


# --- config -----------------------------------------------------------------------------

def test_the_new_config_keys_have_their_defaults():
    assert s.PIPELINE["placement"]["initiativeEpics"] == "local"
    assert s.PIPELINE["placement"]["stallMinutes"] == 90
    assert s.CLOUD_SESSIONS == 3
    assert s.initiative_epics_placement() == "local"


def test_show_config_reports_the_cloud_session_cap(monkeypatch):
    monkeypatch.setattr(s, "plugin_version_info", lambda runner=None: {})
    out = s.cmd_show_config()
    assert out["parallelism"]["cloudSessions"] == 3
    assert out["placement"]["initiativeEpics"] == "local"


def test_an_invalid_initiative_epics_value_is_an_error(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["placement"], "initiativeEpics", "moon")
    with pytest.raises(s.GhError, match="initiativeEpics"):
        s.initiative_epics_placement()


# --- launch output parsing -------------------------------------------------------------

@pytest.mark.parametrize("text, sid, url", [
    (LAUNCH_OUT, "session_01XYZ", "https://claude.ai/code/session_01XYZ"),
    ("\x1b[1mSession ID:\x1b[0m session_01XYZ\n\x1b[36mView:\x1b[0m "
     "https://claude.ai/code/session_01XYZ", "session_01XYZ",
     "https://claude.ai/code/session_01XYZ"),
    ("View: https://claude.ai/code/session_01XYZ\n", "session_01XYZ",
     "https://claude.ai/code/session_01XYZ"),
    ("session id = session_01XYZ", "session_01XYZ", "https://claude.ai/code/session_01XYZ"),
    ("Started session_01XYZABC in the cloud", "session_01XYZABC",
     "https://claude.ai/code/session_01XYZABC"),
])
def test_parse_cloud_launch_is_tolerant(text, sid, url):
    parsed = s.parse_cloud_launch(text)
    assert (parsed["id"], parsed["url"]) == (sid, url)


def test_parse_cloud_launch_returns_none_on_unrecognised_output():
    assert s.parse_cloud_launch("Error: not logged in")["id"] is None
    assert s.parse_cloud_launch("")["url"] is None


# --- markers ----------------------------------------------------------------------------

def test_cloud_sessions_pair_end_and_nudge_markers_with_their_launch():
    comments = [{"body": _launched("session_A", 300)}, {"body": _ended("session_A", 200)},
                {"body": _launched("session_B", 100)}, {"body": _nudged("session_B", 50)},
                {"body": "a human comment"}]
    sessions = s.cloud_sessions(comments)
    assert [x["id"] for x in sessions] == ["session_A", "session_B"]
    assert sessions[0]["ended"]["outcome"] == "waiting-human"
    assert sessions[1]["ended"] is None and sessions[1]["nudges"] == [_ago(50)]
    assert s.latest_cloud_session(comments)["id"] == "session_B"


# --- selection: cap, blockedBy, footprints, idempotency ----------------------------------

def test_the_survey_launches_up_to_the_cap_lowest_number_first(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_SESSIONS", 2)
    survey = _survey(_initiative(2, 3, 4))
    assert survey["launchable"] == [2, 3]
    assert survey["waiting"] == [{"epic": 4, "reason": "cloud session cap reached (2/2)"}]


def test_running_sessions_hold_cap_slots_repo_wide(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_SESSIONS", 2)
    # Epic #20 belongs to another Initiative but its live session still holds a slot.
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]},
                     3, 4, extra=[{"number": 20, "labels": ["type:epic", CLOUD],
                                   "comments": [_launched("session_Z")]}])
    survey = _survey(gh, [2, 3, 4])
    assert [r["epic"] for r in survey["running"]] == [2, 20]
    assert survey["launchable"] == [] and survey["free"] == 0
    assert {w["epic"] for w in survey["waiting"]} == {3, 4}


def test_an_epic_blocked_by_an_open_epic_waits_until_it_closes():
    gh = _initiative(2, 3)
    gh.blocked[3] = [2]
    survey = _survey(gh)
    assert survey["launchable"] == [2]
    assert survey["waiting"] == [{"epic": 3, "reason": "blocked by #2"}]
    gh.issues[2]["state"] = "CLOSED"
    assert _survey(gh)["launchable"] == [3]


def test_legacy_and_closed_epics_are_never_launched():
    gh = _initiative({"number": 2, "labels": ["type:epic", "epic:legacy"]},
                     {"number": 3, "state": "CLOSED"}, 4)
    survey = _survey(gh)
    assert survey["launchable"] == [4]
    assert survey["waiting"] == [{"epic": 2, "reason": "not driven (legacy profile)"}]


FP_A = "## Footprint\n\n- `src/a/**`\n"


def test_an_epic_whose_footprint_overlaps_a_running_one_waits():
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()],
                      "body": FP_A},
                     {"number": 3, "body": "## Footprint\n\n- `src/a/x.py`\n"},
                     {"number": 4, "body": "## Footprint\n\n- `src/b/**`\n"})
    survey = _survey(gh)
    assert survey["launchable"] == [4]
    assert survey["waiting"] == [{"epic": 3, "reason": "footprint overlaps active/eligible #2"}]


def test_footprints_also_separate_two_epics_launched_in_one_pass():
    gh = _initiative({"number": 2, "body": FP_A}, {"number": 3, "body": FP_A})
    survey = _survey(gh)
    assert survey["launchable"] == [2] and survey["waiting"][0]["epic"] == 3


def test_an_unknown_footprint_cannot_be_shown_disjoint_from_a_known_one():
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()],
                      "body": FP_A}, 3)
    survey = _survey(gh)
    assert survey["waiting"] == [{"epic": 3,
                                  "reason": "no readable ## Footprint -- cannot verify "
                                            "non-overlap"}]
    # Positive control: with no known footprint anywhere nothing is held back.
    assert _survey(_initiative(2, 3))["launchable"] == [2, 3]


def test_an_epics_footprint_is_the_union_of_its_lld_tasks():
    lld = ("# lld\n\n## Task #5: a\n\n## Footprint\n\n- `src/a/**`\n\n"
           "## Task #6: b\n\n## Footprint\n\n- `src/c.py`\n")
    git = FakeGit({2: lld})
    assert s.epic_footprint(".", {"number": 2, "body": ""}, git) == ["src/a/**", "src/c.py"]
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]},
                     {"number": 3, "body": "## Footprint\n\n- `src/c.py`\n"})
    assert _survey(gh, git=git)["waiting"][0]["reason"] == "footprint overlaps active/eligible #2"
    assert any("fetch" in c for c in git.calls)


def test_an_ended_session_is_relaunched_only_after_new_activity():
    gh = _initiative({"number": 2, "comments": [_launched(minutes=300), _ended(minutes=200)]},
                     extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                             "updated": _ago(250)}])
    idle = _survey(gh)
    assert idle["launchable"] == [] and "--relaunch" in idle["waiting"][0]["reason"]
    assert _survey(gh, relaunch=True)["launchable"] == [2]
    gh.issues[5]["updated"] = _ago(10)  # e.g. the operator merged its gate on GitHub
    assert _survey(gh)["launchable"] == [2]


# --- launch-cloud-epic ---------------------------------------------------------------------

def test_launch_places_the_epic_starts_the_session_and_records_its_marker():
    gh = _initiative(2)
    claude = FakeClaude(LAUNCH_OUT)

    result = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)

    assert claude.calls == [["claude", "--cloud", "/sdlc:run 2"]]
    assert result["launched"] is True
    assert result["session"] == {"id": "session_01XYZ",
                                 "url": "https://claude.ai/code/session_01XYZ"}
    assert CLOUD in gh.issues[2]["labels"] and result["place"]["label_added"] is True
    marker = gh.comments_on(2)[-1]
    assert "<!-- sdlc:cloud-session id=session_01XYZ url=https://claude.ai/code/session_01XYZ " \
           "launched=" in marker
    assert s.latest_cloud_session([{"body": marker}])["id"] == "session_01XYZ"


def test_launch_is_idempotent_while_the_session_is_live():
    gh = _initiative(2)
    claude = FakeClaude(LAUNCH_OUT)
    s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)

    again = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)

    assert again["launched"] is False and again["already_running"] is True
    assert again["session"]["id"] == "session_01XYZ"
    assert len(claude.calls) == 1 and len(gh.comments_on(2)) == 1


def test_launch_refuses_at_the_cap_and_when_blocked_without_running_claude(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_SESSIONS", 1)
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]},
                     3, 4)
    gh.blocked[4] = [2]
    claude = FakeClaude(LAUNCH_OUT)

    capped = s.cmd_launch_cloud_epic(gh, 3, runner=FakeGit(), claude=claude)
    blocked = s.cmd_launch_cloud_epic(gh, 4, runner=FakeGit(), claude=claude)

    assert capped["refused"] is True and "cap reached (1/1)" in capped["reason"]
    assert capped["running"] == [2]
    assert blocked["refused"] is True and blocked["reason"] == "blocked by #2"
    assert claude.calls == [] and CLOUD not in gh.issues[3]["labels"]


def test_launch_stops_when_place_refuses(monkeypatch):
    gh = _initiative(2)
    monkeypatch.setattr(s, "cmd_place", lambda *a, **k: {"refused": True, "reason": "worktrees",
                                                         "worktrees": [{"issue": 5}]})
    claude = FakeClaude(LAUNCH_OUT)

    result = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)

    assert result["refused"] is True and result["place"]["worktrees"] == [{"issue": 5}]
    assert claude.calls == [] and gh.comments_on(2) == []


def test_an_unparsed_launch_still_records_a_marker_so_it_is_never_doubled():
    gh = _initiative(2)
    claude = FakeClaude("Launched.\n")

    result = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)

    assert result["launched"] is True and result["ok"] is False
    assert "Launched." in result["output_tail"]
    assert "id=unknown url=unknown" in gh.comments_on(2)[-1]
    again = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)
    assert again["already_running"] is True and len(claude.calls) == 1


def test_a_failing_claude_cli_records_no_marker():
    gh = _initiative(2)
    claude = FakeClaude(error=s.GhError("command failed (1): claude --cloud"))
    with pytest.raises(s.GhError):
        s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)
    assert gh.comments_on(2) == []


def test_a_launch_that_times_out_keeps_the_output_it_printed():
    gh = _initiative(2)
    claude = FakeClaude(error=s.ClaudeTimeout("Session ID: session_01SLOW\n"))
    result = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)
    assert result["session"]["id"] == "session_01SLOW"


def test_launch_relaunch_overrides_only_the_idle_check():
    gh = _initiative({"number": 2, "comments": [_launched("session_OLD", 300),
                                                _ended("session_OLD", 200)]})
    claude = FakeClaude(LAUNCH_OUT)
    assert s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=claude)["refused"] is True
    result = s.cmd_launch_cloud_epic(gh, 2, relaunch=True, runner=FakeGit(), claude=claude)
    assert result["launched"] is True and len(claude.calls) == 1


def test_launch_refuses_a_task_and_a_closed_epic():
    gh = _initiative({"number": 2, "state": "CLOSED"},
                     extra=[{"number": 5, "labels": ["type:task"], "parent": 2}])
    with pytest.raises(s.GhError, match="not an Epic"):
        s.cmd_launch_cloud_epic(gh, 5, runner=FakeGit(), claude=FakeClaude())
    assert s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=FakeClaude())["refused"]


# --- next-action on an Initiative in cloud mode ---------------------------------------------

def _next(gh, n=1):
    return s.decide_next_action(gh, n, runner=FakeGit())


def test_a_cloud_mode_initiative_returns_launch_cloud_epics(cloud_mode):
    gh = _initiative(2, 3)
    gh.blocked[3] = [2]
    result = _next(gh)
    assert result["action"] == "launch-cloud-epics" and result["epics"] == [2]
    assert result["unit"] == "epic" and result["cloud"]["waiting"][0]["epic"] == 3


def test_a_local_mode_initiative_still_drives_its_epics():
    # Positive control: the default leaves the Initiative loop unchanged.
    assert _next(_initiative(2, 3))["action"] == "cut-phase-tasks"


def test_a_cloud_mode_initiative_in_a_cloud_session_drives_its_epics_inline(cloud_mode,
                                                                          monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    gh = _initiative(2)
    gh.issues[1]["labels"].append(CLOUD)
    assert _next(gh)["action"] == "cut-phase-tasks"


def test_with_every_epic_running_the_initiative_run_reports_and_ends(cloud_mode):
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD], "comments": [_launched()]},
                     {"number": 3, "state": "CLOSED"})
    result = _next(gh)
    assert result["action"] == "none"
    assert "#2 running in the cloud (https://claude.ai/code/session_01AAA)" in result["reason"]
    assert "(1 closed)" in result["reason"]


def test_with_every_epic_closed_the_initiative_closes_locally(cloud_mode):
    gh = _initiative({"number": 2, "state": "CLOSED"}, {"number": 3, "state": "CLOSED"})
    assert "ready for initiative-close" in _next(gh)["reason"]


# --- cloud-status and stall detection --------------------------------------------------------

def _stall_tree(**epic):
    return _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                        "comments": [_launched(minutes=300)], **epic},
                       extra=[{"number": 5, "labels": ["type:task"], "parent": 2,
                               "stage": "development", "status": "in-progress",
                               "updated": _ago(200)},
                              {"number": 6, "labels": ["type:task"], "parent": 2,
                               "stage": "architecture", "status": "awaiting-human-review",
                               "updated": _ago(250)},
                              {"number": 7, "labels": ["type:task"], "parent": 2,
                               "state": "CLOSED", "updated": _ago(280)}])


def _status(gh, n=2):
    return s.cmd_cloud_status(gh, n, runner=FakeGit(), now=NOW)


def test_cloud_status_reports_an_epics_session_children_prs_and_gates():
    gh = _stall_tree()
    gh.prs[40] = {"headRefName": "issue-5", "title": "Build it", "updatedAt": _ago(200)}

    row = _status(gh)["epics"][0]

    assert row["session"]["url"] == "https://claude.ai/code/session_01AAA" and row["live"]
    assert row["children"] == {"open": 2, "closed": 1,
                               "by_stage": {"development": 1, "architecture": 1},
                               "by_status": {"in-progress": 1, "awaiting-human-review": 1}}
    assert row["open_prs"] == [{"pr": 40, "issue": 5, "title": "Build it", "draft": False,
                                "updated": _ago(200)}]
    assert row["gates_pending"] == [{"issue": 6, "status": "awaiting-human-review"}]
    assert row["last_activity"] == _ago(200) and row["end_marker"] is False


def test_an_epic_idle_past_stall_minutes_is_stalled():
    row = _status(_stall_tree())["epics"][0]
    assert row["idle_minutes"] == 200 and row["stalled"] is True and row["escalate"] is False


@pytest.mark.parametrize("touch", [
    lambda gh: gh.prs.update({40: {"headRefName": "issue-5", "updatedAt": _ago(5)}}),
    lambda gh: gh.issues[6].update(updated=_ago(5)),
    lambda gh: gh.commits.update({"issue-5": _ago(5)}),
    lambda gh: gh.commits.update({"epic-2": _ago(5)}),
    lambda gh: gh.issues[2]["comments"].append({"body": "operator note",
                                                "createdAt": _ago(5)}),
])
def test_any_recent_github_activity_means_not_stalled(touch):
    gh = _stall_tree()
    touch(gh)
    assert _status(gh)["epics"][0]["stalled"] is False


def test_the_pipelines_own_cloud_markers_are_not_activity():
    gh = _stall_tree()
    gh.issues[2]["comments"].append({"body": _nudged(minutes=1), "createdAt": _ago(1)})
    assert _status(gh)["epics"][0]["stalled"] is True


def test_a_fresh_launch_is_not_stalled_and_an_ended_one_never_is():
    fresh = _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                         "comments": [_launched(minutes=10)]})
    assert _status(fresh)["epics"][0]["stalled"] is False
    ended = _stall_tree()
    ended.issues[2]["comments"].append(_ended(minutes=150))
    row = _status(ended)["epics"][0]
    assert row["stalled"] is False and row["end_marker"] is True and row["live"] is False


def test_stall_minutes_is_configurable(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["placement"], "stallMinutes", 300)
    assert _status(_stall_tree())["epics"][0]["stalled"] is False


def test_cloud_status_on_an_initiative_lists_launchable_and_flags_attention(cloud_mode):
    gh = _stall_tree()
    gh.issues[3] = {**gh.issues[2], "labels": ["type:epic"], "comments": [], "parent": 1}
    gh.issues[3]["title"] = "issue 3"

    out = _status(gh, 1)

    assert out["kind"] == "initiative" and out["running"] == [2] and out["launchable"] == [3]
    assert [r["epic"] for r in out["epics"]] == [2] and out["not_in_cloud"] == [3]
    assert out["attention"] is True and out["all_closed"] is False


def test_cloud_status_attention_when_every_epic_closed_and_quiet_otherwise(cloud_mode):
    closed = _initiative({"number": 2, "state": "CLOSED"})
    assert _status(closed, 1)["all_closed"] is True and _status(closed, 1)["attention"] is True
    quiet = _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                         "comments": [_launched(minutes=10)]})
    assert _status(quiet, 1)["attention"] is False


def test_cloud_status_is_read_only_and_exempt_from_the_placement_guard(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")  # a cloud session asking about a local Epic
    gh = _initiative(2)
    code, out = _cli(monkeypatch, gh, ["cloud-status", "1"])
    assert code == 0 and out["kind"] == "initiative"
    assert all(gh.comments_on(n) == [] for n in gh.issues)


# --- nudge-cloud-epic --------------------------------------------------------------------------

def _nudge(gh, claude, **kw):
    return s.cmd_nudge_cloud_epic(gh, 2, claude=claude, now=NOW, **kw)


def test_a_stalled_session_is_nudged_twice_then_escalated():
    gh = _stall_tree()
    claude = FakeClaude("ok")

    first, second = _nudge(gh, claude), _nudge(gh, claude)
    third = _nudge(gh, claude)

    assert claude.calls == [["claude", "-p", "continue: /sdlc:run 2", "--cloud",
                             "session_01AAA"]] * 2
    assert (first["nudged"], first["nudges"], first["escalate"]) == (True, 1, False)
    assert (second["nudged"], second["nudges"], second["escalate"]) == (True, 2, False)
    assert third["nudged"] is False and third["escalate"] is True
    assert "ask the operator" in third["reason"]
    assert len([c for c in gh.comments_on(2) if "sdlc:cloud-session-nudge" in c]) == 2


def test_new_activity_resets_the_nudge_count():
    gh = _stall_tree()
    claude = FakeClaude("ok")
    _nudge(gh, claude)
    _nudge(gh, claude)
    # Activity after the nudges, then a new stall: the old nudges no longer count.
    gh.issues[5]["updated"] = (NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = NOW + timedelta(minutes=200)
    result = s.cmd_nudge_cloud_epic(gh, 2, claude=claude, now=later)
    assert result["nudged"] is True and result["nudges"] == 1


def test_a_session_that_is_not_stalled_is_not_nudged_unless_forced():
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                      "comments": [_launched(minutes=10)]})
    claude = FakeClaude("ok")
    assert _nudge(gh, claude)["nudged"] is False and claude.calls == []
    assert _nudge(gh, claude, force=True)["nudged"] is True


def test_nudge_refuses_without_a_live_session():
    gh = _stall_tree()
    gh.issues[2]["comments"].append(_ended())
    claude = FakeClaude("ok")
    assert _nudge(gh, claude)["refused"] is True and claude.calls == []


def test_an_unknown_session_id_escalates_at_once():
    gh = _stall_tree(comments=[_launched("unknown", 300).replace(
        "url=https://claude.ai/code/unknown", "url=unknown")])
    claude = FakeClaude("ok")
    result = _nudge(gh, claude)
    assert result["escalate"] is True and claude.calls == []


def test_a_nudge_whose_reply_times_out_still_counts():
    gh = _stall_tree()
    result = _nudge(gh, FakeClaude(error=s.ClaudeTimeout("")))
    assert result["nudged"] is True and result["reply_timed_out"] is True


# --- end-cloud-session -----------------------------------------------------------------------

def test_end_cloud_session_marks_the_latest_session_and_is_idempotent():
    gh = _initiative({"number": 2, "comments": [_launched("session_A")]})

    first = s.cmd_end_cloud_session(gh, 2, "closed", note="Epic merged to main")
    again = s.cmd_end_cloud_session(gh, 2, "closed")

    assert first == {"epic": 2, "ended": True, "id": "session_A", "outcome": "closed"}
    assert "<!-- sdlc:cloud-session-end id=session_A outcome=closed ended=" in gh.comments_on(2)[-1]
    assert again["already_ended"] is True and len(gh.comments_on(2)) == 2
    assert s.latest_cloud_session(gh.issue_view(2)["comments"])["ended"]["outcome"] == "closed"


def test_end_cloud_session_without_a_launch_marker_records_an_unknown_id():
    gh = _initiative(2)
    assert s.cmd_end_cloud_session(gh, 2, "stopped")["id"] == "unknown"


def test_end_cloud_session_validates_its_outcome_and_unit(monkeypatch):
    gh = _initiative(2, extra=[{"number": 5, "labels": ["type:task"], "parent": 2}])
    with pytest.raises(s.GhError, match="--outcome"):
        s.cmd_end_cloud_session(gh, 2, "done")
    with pytest.raises(s.GhError, match="not an Epic"):
        s.cmd_end_cloud_session(gh, 5, "closed")
    code, out = _cli(monkeypatch, gh, ["end-cloud-session", "2", "--outcome", "waiting-human"])
    assert code == 0 and out["outcome"] == "waiting-human"


def test_an_ended_session_frees_its_cap_slot(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_SESSIONS", 1)
    gh = _initiative({"number": 2, "labels": ["type:epic", CLOUD],
                      "comments": [_launched("session_A")]}, 3)
    assert _survey(gh, [3])["launchable"] == []
    s.cmd_end_cloud_session(gh, 2, "closed")
    gh.issues[2]["state"] = "CLOSED"
    assert _survey(gh, [3])["launchable"] == [3]


def _fake_cli(tmp_path, body):
    """An executable standing in for `claude`: refuses without a terminal, like `--cloud`."""
    exe = tmp_path / "claude"
    exe.write_text("#!/usr/bin/env python3\nimport sys, time\n"
                   "if not sys.stdout.isatty():\n"
                   "    print('Error: --cloud requires an interactive terminal.'); sys.exit(1)\n"
                   + body)
    exe.chmod(0o755)
    return str(exe)


def test_launch_runner_gives_claude_a_terminal_and_stops_it_after_the_url(tmp_path):
    exe = _fake_cli(tmp_path, "print('Created cloud session: x'); "
                              "print('View: https://claude.ai/code/session_01PTY?from=cli', "
                              "flush=True)\ntime.sleep(60)\n")
    started = datetime.now()
    out = s._pty_claude_runner([exe, "--cloud", "/sdlc:run 2"], timeout=20)
    assert (datetime.now() - started).total_seconds() < 15
    assert s.parse_cloud_launch(out)["id"] == "session_01PTY"


def test_launch_runner_times_out_without_a_url(tmp_path):
    exe = _fake_cli(tmp_path, "print('Creating...', flush=True)\ntime.sleep(60)\n")
    with pytest.raises(s.ClaudeTimeout) as e:
        s._pty_claude_runner([exe, "--cloud", "x"], timeout=2)
    assert "Creating" in e.value.output


def test_launch_runner_reports_a_failed_cli(tmp_path):
    exe = _fake_cli(tmp_path, "print('Error: not logged in'); sys.exit(3)\n")
    with pytest.raises(s.GhError, match="not logged in"):
        s._pty_claude_runner([exe, "--cloud", "x"], timeout=10)


def test_the_plain_runner_is_what_claude_refuses(tmp_path):
    exe = _fake_cli(tmp_path, "print('View: https://claude.ai/code/session_01PTY')\n")
    with pytest.raises(s.GhError, match=r"command failed \(1\)"):
        s._default_claude_runner([exe, "--cloud", "x"])
