"""v0.3.18: glob `bases`, prePr/retro config, plugin-version guard, per-Initiative placement,
park-finding, and the retro backlog kept out of every scan.

Each regression goes RED against v0.3.17; its positive control stays GREEN either way."""
import argparse
import json
import os

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_v2_phase_tasks import FakeGh


# ---- 1: requiredWorkflows[].bases take fnmatch globs ----

def _spec(**kw):
    base = {"workflow": "Backend IT", "suite": "backend-it", "prefixes": ("backend/",),
            "files": (), "excludeGlobs": (), "bases": ("main", "initiative-*"),
            "attestable": True}
    base.update(kw)
    return base


@pytest.mark.parametrize("base,applies", [
    ("main", True), ("initiative-1994", True), ("epic-9", False), ("issue-3", False),
    (None, False), ("maintenance", False)])
def test_a_glob_base_matches_by_pattern(base, applies):
    assert s._workflow_applies_to_base(_spec(), base) is applies


def test_an_entry_without_bases_still_applies_everywhere():
    # Positive control: unscoped behaviour is unchanged.
    assert s._workflow_applies_to_base(_spec(bases=()), "epic-9") is True
    assert s._workflow_applies_to_base(_spec(bases=()), None) is True


def test_a_task_pr_into_an_epic_reports_no_missing_workflow_and_no_uncovered_noise(monkeypatch):
    monkeypatch.setattr(s, "REQUIRED_WORKFLOWS", (_spec(),))

    class Gh:
        def pr_checks(self, n):
            return [{"name": "x", "bucket": "pass", "workflow": "Other"}]

        def pr_view(self, n, fields=""):
            return {"comments": [], "headRefOid": "feedface", "baseRefName": "epic-9"}

        def pr_files(self, n):
            return ["backend/a.py"]

    result = s.cmd_pr_checks(Gh(), 42)
    assert result["status"] == "passed"
    assert "uncovered_paths" not in result
    assert s.missing_required_workflows(["backend/a.py"], [], [], "sha", base_ref="epic-9") == []
    # Positive control: into an initiative branch the suite is required.
    assert s.missing_required_workflows(["backend/a.py"], [], [], "sha",
                                        base_ref="initiative-1994") == ["Backend IT"]


def test_defer_suites_still_skip_a_glob_scoped_suite_at_an_initiative_epic_close(monkeypatch):
    monkeypatch.setattr(s, "REQUIRED_WORKFLOWS", (_spec(),))
    out = s._epic_suite_evidence(None, "epic-9", [], ["backend/a.py"], None, False,
                                 base="initiative-7", deferred=("backend-it",))
    assert out["suites"]["backend-it"] == "deferred_to_initiative"
    # Positive control: an Epic closing into an unscoped base never sees the suite at all.
    out = s._epic_suite_evidence(None, "epic-9", [], ["backend/a.py"], None, False,
                                 base="release-1")
    assert "backend-it" not in out["suites"]


# ---- 2: pipeline.prePr / pipeline.retro surface in show-config ----

def test_show_config_carries_the_new_keys_with_their_defaults(monkeypatch):
    monkeypatch.setattr(s, "plugin_version_info", lambda runner=None: {})
    out = s.cmd_show_config()
    assert out["prePr"]["command"] == ""
    assert out["retro"]["parkIssue"] is None
    assert out["labels"]["retro"] == "sdlc:retro"


# ---- 3: plugin version on runs ----

@pytest.mark.parametrize("lo,hi", [
    ("0.3.16", "0.3.17"), ("v0.3.16", "0.3.17"), ("0.3.9", "0.3.10"),
    ("0.3.16", "0.3.16-initiative.1"), ("0.3.16-initiative.1", "0.3.17"),
    ("0.3.16-initiative.1", "0.3.16-initiative.2"), ("0.3", "0.3.1"), ("0.9.9", "1.0.0")])
def test_version_key_orders_tags(lo, hi):
    assert s.version_key(lo) < s.version_key(hi)


def test_version_key_equates_spellings_and_rejects_junk():
    assert s.version_key("v0.3.17") == s.version_key("0.3.17")
    assert s.version_key("0.3") == s.version_key("0.3.0")
    assert s.version_key("main") is None and s.version_key(None) is None


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "PLUGIN_VERSION_GUARD", True)
    monkeypatch.setattr(s, "running_plugin_version", lambda: "0.3.18")
    return tmp_path


def _marker(v):
    return f"sdlc plugin `{v}` now drives this run.\n\n<!-- sdlc:plugin-version v={v} @ t -->"


def _run(gh, root=9, repo_path="."):
    return s.cmd_next_action(gh, argparse.Namespace(epic=root, run_id=None,
                                                    repo_path=repo_path, skip_epic=[]))


def test_an_older_plugin_cannot_advance_a_run_a_newer_one_moved(guard):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"], "comments": [_marker("0.3.19")]}])
    with pytest.raises(GhError, match=r"0\.3\.19.*older 0\.3\.18"):
        _run(gh)
    assert gh.issues[9]["comments"] == [_marker("0.3.19")]


def test_a_newer_plugin_on_the_initiative_above_also_refuses(guard):
    gh = FakeGh([{"number": 1, "labels": ["type:initiative"], "comments": [_marker("v0.4.0")]},
                 {"number": 9, "labels": ["type:epic"], "parent": 1}])
    with pytest.raises(GhError, match="#1 was last moved"):
        _run(gh)


def test_the_same_version_continues_without_a_new_stamp(guard):
    # Positive control.
    gh = FakeGh([{"number": 9, "labels": ["type:epic"], "comments": [_marker("v0.3.18")]}])
    result = _run(gh)
    assert result["action"] == "cut-phase-tasks"
    assert "plugin_version_recorded" not in result and len(gh.issues[9]["comments"]) == 1


def test_a_newer_plugin_continues_and_restamps(guard):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"], "comments": [_marker("0.3.17")]}])
    assert _run(gh)["plugin_version_recorded"] == "0.3.18"
    assert s.recorded_plugin_version([{"body": b} for b in gh.issues[9]["comments"]]) == "0.3.18"


def _pinned_repo(tmp_path, ref):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"extraKnownMarketplaces": {"sdlc-pipeline": {"source": {"source": "github",
                                                                  "ref": ref}}}}))
    (tmp_path / "sub").mkdir()
    return str(tmp_path / "sub")


def test_a_new_run_older_than_the_repo_pin_only_warns(guard):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]}])
    result = _run(gh, repo_path=_pinned_repo(guard, "v0.3.19"))
    assert "older than the repo's pin v0.3.19" in result["plugin_warning"]
    assert result["plugin_version_recorded"] == "0.3.18"


def test_a_new_run_at_or_above_the_pin_does_not_warn(guard):
    # Positive control.
    gh = FakeGh([{"number": 9, "labels": ["type:epic"]}])
    result = _run(gh, repo_path=_pinned_repo(guard, "v0.3.18"))
    assert "plugin_warning" not in result


def test_the_guard_off_makes_no_github_call(monkeypatch):
    gh = FakeGh([{"number": 9, "labels": ["type:epic"], "comments": [_marker("9.9.9")]}])
    assert _run(gh)["action"] == "cut-phase-tasks"


# Release check: the manifest's version is the tag being released.

def _release_tag():
    """`SDLC_RELEASE_TAG` (the retro's pre-tag run), else the tag a GitHub tag push built."""
    tag = os.environ.get("SDLC_RELEASE_TAG")
    if not tag and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
    return tag


def test_plugin_manifest_version_equals_the_release_tag():
    tag = _release_tag()
    if not tag:
        pytest.skip("not a release: no SDLC_RELEASE_TAG or tag ref")
    assert s.version_key(tag) == s.version_key(s.running_plugin_version()), (
        f"plugin.json version {s.running_plugin_version()} != release tag {tag}")


# ---- 4: per-Initiative placement ----

@pytest.fixture
def profiles(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [
        {"name": "cloudy", "match": {"label": "init:cloud"}, "placement": "cloud"},
        {"name": "plain", "match": {"label": "init:plain"}}])


def _initiative(label):
    return {"number": 1, "labels": [{"name": "type:initiative"}, {"name": label}]}


def test_an_initiative_profile_placement_overrides_the_global_key(profiles):
    assert s.PIPELINE["placement"]["initiativeEpics"] == "local"
    assert s.initiative_epics_placement(_initiative("init:cloud")) == "cloud"


def test_without_a_profile_placement_the_global_key_rules(profiles, monkeypatch):
    # Positive control.
    assert s.initiative_epics_placement(_initiative("init:plain")) == "local"
    monkeypatch.setitem(s.PIPELINE["placement"], "initiativeEpics", "cloud")
    assert s.initiative_epics_placement(_initiative("init:plain")) == "cloud"
    assert s.initiative_epics_placement() == "cloud"


def test_an_invalid_profile_placement_is_an_error(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [
        {"name": "x", "match": "*", "placement": "moon"}])
    with pytest.raises(GhError, match=r"initiativeProfiles\[\]\.placement='moon'"):
        s.initiative_epics_placement(_initiative("any"))


# ---- 5: park-finding / supersede-park-issue / retro issues out of scans ----

class ParkGh:
    def __init__(self, issues):
        self.issues = {n: {"state": "OPEN", "comments": [], **i} for n, i in issues.items()}

    def issue_view(self, n):
        i = self.issues[n]
        return {"number": n, "state": i["state"],
                "comments": [{"body": b, "url": f"https://x/{n}#c{k}"}
                             for k, b in enumerate(i["comments"])]}

    def issue_comment(self, n, body):
        self.issues[n]["comments"].append(body)

    def issue_close(self, n, reason="completed"):
        self.issues[n]["state"] = "CLOSED"


@pytest.fixture
def park(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["retro"], "parkIssue", 50)
    monkeypatch.setattr(s, "running_plugin_version", lambda: "0.3.18")


def test_park_finding_without_a_park_issue_fails_loudly(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["retro"], "parkIssue", None)
    with pytest.raises(GhError, match="parkIssue is not set"):
        s.cmd_park_finding(ParkGh({}), "k", text="x")


def test_park_finding_posts_one_comment_with_version_target_and_evidence(park):
    gh = ParkGh({50: {}})
    out = s.cmd_park_finding(gh, "pr-review-bounce", text="Bounced twice on a red check",
                             evidence=("https://pr/1",), target="agents/pr-review.md")
    assert out == {"issue": 50, "key": "pr-review-bounce", "posted": "finding"}
    [body] = gh.issues[50]["comments"]
    assert "`0.3.18`" in body and "agents/pr-review.md" in body and "https://pr/1" in body
    assert "<!-- sdlc:finding key=pr-review-bounce v=0.3.18 -->" in body


def test_a_repeat_key_posts_seen_again_linking_the_original(park):
    gh = ParkGh({50: {}})
    s.cmd_park_finding(gh, "dup", text="first")
    out = s.cmd_park_finding(gh, "dup", text="again\nmore")
    assert out["posted"] == "seen-again" and out["original"] == "https://x/50#c0"
    assert gh.issues[50]["comments"][1].startswith("Seen again on plugin `0.3.18`: again")
    # A third repeat still links the original, not the seen-again comment.
    assert s.cmd_park_finding(gh, "dup", text="x")["original"] == "https://x/50#c0"


def test_a_different_key_is_a_new_finding(park):
    # Positive control.
    gh = ParkGh({50: {}})
    s.cmd_park_finding(gh, "dup", text="first")
    assert s.cmd_park_finding(gh, "dup-2", text="other")["posted"] == "finding"


def test_park_finding_follows_superseded_markers_from_a_closed_issue(park):
    gh = ParkGh({50: {"state": "CLOSED"}, 60: {"state": "CLOSED"}, 70: {}})
    gh.issues[50]["comments"] = ["Superseded by #60.\n\n<!-- sdlc:superseded-by #60 -->"]
    gh.issues[60]["comments"] = ["Superseded by #70.\n\n<!-- sdlc:superseded-by #70 -->"]
    assert s.cmd_park_finding(gh, "k", text="x")["issue"] == 70
    assert len(gh.issues[70]["comments"]) == 1


def test_a_closed_park_issue_without_a_successor_refuses(park):
    gh = ParkGh({50: {"state": "CLOSED"}})
    with pytest.raises(GhError, match="names no successor"):
        s.cmd_park_finding(gh, "k", text="x")


def test_a_superseded_loop_stops_after_a_few_hops(park):
    gh = ParkGh({50: {"state": "CLOSED", "comments": ["<!-- sdlc:superseded-by #51 -->"]},
                 51: {"state": "CLOSED", "comments": ["<!-- sdlc:superseded-by #50 -->"]}})
    with pytest.raises(GhError, match="superseded hops"):
        s.cmd_park_finding(gh, "k", text="x")


def test_park_finding_rejects_a_bad_key(park):
    with pytest.raises(GhError, match="--key"):
        s.cmd_park_finding(ParkGh({50: {}}), "Has Spaces", text="x")


def test_supersede_park_issue_marks_and_closes_once():
    gh = ParkGh({50: {}})
    assert s.cmd_supersede_park_issue(gh, 50, 61)["closed"] is True
    s.cmd_supersede_park_issue(gh, 50, 61)
    assert gh.issues[50]["comments"] == ["Superseded by #61.\n\n<!-- sdlc:superseded-by #61 -->"]
    assert gh.issues[50]["state"] == "CLOSED"


def test_the_rest_client_drops_retro_issues_from_issue_list():
    pages = {"repos/o/r/issues?state=all&per_page=100&sort=created&direction=asc&page=1": [
        {"number": 1, "title": "t", "state": "open", "labels": [{"name": "sdlc:retro"}]},
        {"number": 2, "title": "t", "state": "open", "labels": [{"name": "type:task"}]}]}

    def runner(argv):
        return json.dumps(pages.get(argv[-1], []))

    gh = s.GitHubRest(repo="o/r", runner=runner)
    assert [i["number"] for i in gh.issue_list()] == [2]


def test_audit_issues_never_lists_the_retro_issue():
    raw = [{"number": 50, "labels": [{"name": "sdlc:retro"}], "state": "OPEN",
            "title": "backlog", "parent": None, "issueType": None, "fields": {}},
           {"number": 51, "labels": [], "state": "OPEN", "title": "stray", "parent": None,
            "issueType": None, "fields": {}}]

    class Gh:
        def issue_list(self):
            return s.without_retro_issues(raw)

    assert [f["issue"] for f in s.cmd_audit_issues(Gh())["issues"]] == [51]
