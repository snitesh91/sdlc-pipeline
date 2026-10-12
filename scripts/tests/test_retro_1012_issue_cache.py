"""`GitHubRest.issue_list` index: one listing per process, a single-issue refetch per write
it can attribute, and closed issues served from the disk cache across processes."""
import json
import re

import pytest

import sdlc_next as s
from sdlc_next import GitHubRest
from tests.test_github_rest import ORG_FIELDS, _fv, _issue

R = "repos/owner/repo"


class Runner:
    """Fake `gh api` routed by path prefix (query string included, paging stripped)."""

    def __init__(self, lists, issues):
        self.lists, self.issues, self.calls = lists, issues, []

    def __call__(self, argv):
        args, method, path, i = argv[2:], None, None, 0
        fields = []
        while i < len(args):
            if args[i] == "-X":
                method, i = args[i + 1], i + 2
            elif args[i] in ("-f", "-F", "--jq"):
                fields.append(args[i + 1]) if args[i] != "--jq" else None
                i += 2
            else:
                path, i = args[i], i + 1
        method = method or ("POST" if fields else "GET")
        m = re.search(r"&page=(\d+)$", path)
        page = int(m.group(1)) if m else None
        path = path[:m.start()] if m else path
        self.calls.append((method, path))
        if path == "orgs/owner/issue-fields":
            return json.dumps(ORG_FIELDS if page in (None, 1) else [])
        if method == "GET" and "/issues?" in path:
            state = re.search(r"state=(\w+)", path).group(1)
            return json.dumps(self.lists[state] if page == 1 else [])
        n = re.fullmatch(rf"{R}/issues/(\d+)", path)
        if method == "GET" and n:
            if "--jq" in argv:
                return f"{9000 + int(n.group(1))}\n"
            return json.dumps(self.issues[int(n.group(1))])
        if method == "POST" and path == f"{R}/issues":
            return json.dumps({"number": 99})
        return "{}"

    def lists_called(self):
        return [re.search(r"state=(\w+)", p).group(1) for m, p in self.calls if "/issues?" in p]

    def single_gets(self):
        return [p for m, p in self.calls if m == "GET" and re.fullmatch(rf"{R}/issues/\d+", p)]


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setenv("SDLC_ISSUE_CACHE", "on")
    monkeypatch.setenv("SDLC_CACHE_DIR", str(tmp_path))
    return tmp_path


def _world():
    closed = _issue(5, state="closed", title="old")
    open7 = _issue(7, issue_field_values=[_fv(11, "Stage", "LLD")])
    return Runner({"all": [closed, open7, {**_issue(8), "pull_request": {"url": "x"}}],
                   "open": [open7], "closed": []},
                  {7: open7, 99: _issue(99, title="new")})


def test_one_listing_serves_every_issue_list_call_in_a_process(cache):
    runner = _world()
    gh = GitHubRest(runner=runner)
    first = gh.issue_list()
    assert gh.issue_list() == first and [i["number"] for i in first] == [5, 7]
    assert runner.lists_called() == ["all"]


def test_a_write_refetches_only_the_issue_it_names(cache):
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh.issue_edit(7, add_labels=["x"])
    gh.issue_close(7)
    gh.issue_comment(7, "hi")
    gh.issue_list()
    assert runner.lists_called() == ["all"] and runner.single_gets() == [f"{R}/issues/7"]  # once for two writes


def test_a_created_issue_and_a_new_sub_issue_child_are_refetched_by_number(cache):
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    assert gh.issue_create("t", "b", []) == 99
    gh.add_sub_issue(7, 99)
    numbers = [i["number"] for i in gh.issue_list()]
    assert numbers == [5, 7, 99] and runner.lists_called() == ["all"]


def test_a_pr_merge_relists_since_it_can_close_issues(cache):
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh._api(f"{R}/pulls/3/merge", "-f", "merge_method=merge", method="PUT")
    gh.issue_list()
    assert runner.lists_called() == ["all", "closed", "open"]


def test_a_later_process_lists_open_and_recently_updated_issues_not_all(cache):
    _ = GitHubRest(runner=_world()).issue_list()
    runner = _world()
    runner.lists["open"] = []
    runner.lists["closed"] = [_issue(7, state="closed")]  # #7 closed since
    numbers = {i["number"]: i["state"] for i in GitHubRest(runner=runner).issue_list()}
    assert numbers == {5: "CLOSED", 7: "CLOSED"}
    assert runner.lists_called() == ["closed", "open"]
    since = next(p for m, p in runner.calls if "state=closed" in p)
    assert "since=" in since and "sort=updated" in since


def test_a_reopened_issue_reads_open(cache):
    _ = GitHubRest(runner=_world()).issue_list()
    runner = _world()
    runner.lists["open"] = [_issue(5, title="old"), runner.issues[7]]
    states = {i["number"]: i["state"] for i in GitHubRest(runner=runner).issue_list()}
    assert states == {5: "OPEN", 7: "OPEN"}


def test_the_cache_relists_everything_once_its_ttl_passes(cache, monkeypatch):
    _ = GitHubRest(runner=_world()).issue_list()
    monkeypatch.setattr(s, "_ISSUE_CACHE_TTL_S", -1)
    runner = _world()
    GitHubRest(runner=runner).issue_list()
    assert runner.lists_called() == ["all"]


def test_with_the_cache_off_every_call_relists(monkeypatch):
    monkeypatch.setenv("SDLC_ISSUE_CACHE", "off")
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh.issue_list()
    assert runner.lists_called() == ["all", "all"]
