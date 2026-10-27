"""Retro 2026-10-27: `GitHubRest.issue_list` reads what the unit reaches (open issues, closed
sub-issue trees, parents, the command's own numbers) with no whole-repo disk cache; REST paging
survives a short or empty page mid-list; open-arch-revision keeps every --blocks; post-comment
refuses a stale repeat; pass-gate --doc-only closes a doc-only unit."""
import json
import re

import pytest

import sdlc_next as s
from sdlc_next import GhError, GitHubRest
from tests.test_github_rest import ORG_FIELDS, _fv, _issue

R = "repos/owner/repo"


class Runner:
    """Fake `gh api`: `lists[state]` per page (page 1 only unless `pages` given), single
    issues from `issues`, sub-issues from `subs`. Records (method, path-without-page)."""

    def __init__(self, open_=(), issues=None, subs=None, pages=None):
        self.open, self.issues, self.subs = list(open_), dict(issues or {}), dict(subs or {})
        self.pages = pages  # {page: [items]} served for the open listing, overriding `open`
        self.calls = []

    def __call__(self, argv):
        args, method, path, i, fields = argv[2:], None, None, 0, []
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
        self.calls.append((method, path, page))
        if path == "orgs/owner/issue-fields":
            return json.dumps(ORG_FIELDS if page in (None, 1) else [])
        if method == "GET" and "/issues?state=open" in path:
            if self.pages is not None:
                serve = self.pages.get(page, [])
                return json.dumps(serve.pop(0) if serve and isinstance(serve[0], list) else serve)
            return json.dumps(self.open if page == 1 else [])
        sub = re.fullmatch(rf"{R}/issues/(\d+)/sub_issues\?per_page=100", path)
        if method == "GET" and sub:
            return json.dumps(self.subs.get(int(sub.group(1)), []) if page == 1 else [])
        n = re.fullmatch(rf"{R}/issues/(\d+)", path)
        if method == "GET" and n:
            if "--jq" in argv:
                return f"{9000 + int(n.group(1))}\n"
            if int(n.group(1)) not in self.issues:
                raise GhError("gh: Not Found (HTTP 404)")
            return json.dumps(self.issues[int(n.group(1))])
        if method == "POST" and path == f"{R}/issues":
            return json.dumps({"number": 99})
        return "{}"

    def gets(self, kind):
        """First-page GETs (a listing's start, not its end-of-list peeks)."""
        return [p for m, p, pg in self.calls if m == "GET" and kind in p and pg in (None, 1)]

    def single_gets(self):
        return [p for m, p, _ in self.calls if m == "GET" and re.fullmatch(rf"{R}/issues/\d+", p)]


def _summary(total):
    return {"total": total, "completed": 0, "percent_completed": 0}


def _numbers(gh):
    return {i["number"]: i["state"] for i in gh.issue_list()}


# --- unit-scoped reads ------------------------------------------------------------------

def test_closed_sub_issues_of_an_open_epic_are_read_from_its_sub_issue_list():
    epic = _issue(10, sub_issues_summary=_summary(2))
    task = _issue(11, parent_issue_url=f"https://api.github.com/{R}/issues/10")
    closed = _issue(12, state="closed")  # the sub_issues payload may omit the parent
    runner = Runner([epic, task], subs={10: [task, closed]})
    issues = {i["number"]: i for i in GitHubRest(runner=runner).issue_list()}
    assert {n: i["state"] for n, i in issues.items()} == {10: "OPEN", 11: "OPEN", 12: "CLOSED"}
    assert issues[12]["parent"] == {"number": 10}
    # No whole-repo listing: open issues only, then the one parent's sub-issues.
    assert runner.gets("state=all") == [] and runner.gets("state=closed") == []
    assert runner.gets("/sub_issues") == [f"{R}/issues/10/sub_issues?per_page=100"]


def test_a_parent_whose_sub_issues_are_all_listed_open_is_not_walked():
    epic = _issue(10, sub_issues_summary=_summary(1))
    task = _issue(11, parent_issue_url=f"https://api.github.com/{R}/issues/10")
    runner = Runner([epic, task])
    assert _numbers(GitHubRest(runner=runner)) == {10: "OPEN", 11: "OPEN"}
    assert runner.gets("/sub_issues") == []


def test_a_closed_unit_the_command_names_is_fetched_with_its_tree_and_parent():
    s._FOCUS.update({20})
    closed_epic = _issue(20, state="closed", sub_issues_summary=_summary(1),
                         parent_issue_url=f"https://api.github.com/{R}/issues/30")
    runner = Runner([], issues={20: closed_epic, 30: _issue(30, state="closed")},
                    subs={20: [_issue(21, state="closed")]})
    assert _numbers(GitHubRest(runner=runner)) == {20: "CLOSED", 21: "CLOSED", 30: "CLOSED"}


def test_an_open_issue_whose_parent_is_closed_gets_the_parent_by_number():
    child = _issue(11, parent_issue_url=f"https://api.github.com/{R}/issues/10")
    runner = Runner([child], issues={10: _issue(10, state="closed")})
    assert _numbers(GitHubRest(runner=runner)) == {10: "CLOSED", 11: "OPEN"}


def test_a_missing_parent_is_tried_once_not_looped_on():
    child = _issue(11, parent_issue_url=f"https://api.github.com/{R}/issues/10")
    runner = Runner([child])
    assert _numbers(GitHubRest(runner=runner)) == {11: "OPEN"}
    assert runner.single_gets() == [f"{R}/issues/10"]


def test_main_focuses_the_issue_numbers_a_command_names_never_a_pr(monkeypatch):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "cmd_open_arch_revision",
                        lambda *a: seen.append(set(s._FOCUS)) or {"ok": True})
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    s.main(["open-arch-revision", "9", "--title", "t", "--body", "b", "--blocks", "4109",
            "--blocks", "4110"])
    assert seen == [{9, 4109, 4110}]
    assert s._focus_numbers(type("A", (), {"issue": 5, "pr": 77, "gate_pr": 78})()) == {5}


# --- per-process index (kept from the 2026-10-12 cache, minus the disk) ------------------

def _world():
    open7 = _issue(7, issue_field_values=[_fv(11, "Stage", "LLD")])
    return Runner([open7, {**_issue(8), "pull_request": {"url": "x"}}],
                  issues={7: open7, 99: _issue(99, title="new")})


def test_one_listing_serves_every_issue_list_call_in_a_process():
    runner = _world()
    gh = GitHubRest(runner=runner)
    first = gh.issue_list()
    assert gh.issue_list() == first and [i["number"] for i in first] == [7]
    assert len(runner.gets("state=open")) == 1


def test_a_write_refetches_only_the_issue_it_names():
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh.issue_edit(7, add_labels=["x"])
    gh.issue_close(7)
    gh.issue_list()
    assert len(runner.gets("state=open")) == 1 and runner.single_gets() == [f"{R}/issues/7"]


def test_a_created_issue_is_refetched_by_number():
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    assert gh.issue_create("t", "b", []) == 99
    assert [i["number"] for i in gh.issue_list()] == [7, 99]


def test_a_pr_merge_relists_since_it_can_close_issues():
    runner = _world()
    gh = GitHubRest(runner=runner)
    gh.issue_list()
    gh._api(f"{R}/pulls/3/merge", "-f", "merge_method=merge", method="PUT")
    gh.issue_list()
    assert len(runner.gets("state=open")) == 2


def test_no_disk_cache_is_left_behind():
    assert not hasattr(s, "_issue_cache_path") and not hasattr(s, "_ISSUE_CACHE_TTL_S")


# --- REST paging: a short or empty page mid-list is transient ---------------------------

def _full(start):
    return [_issue(n) for n in range(start, start + 100)]


def test_an_empty_page_mid_list_is_refetched_not_taken_as_the_end():
    # page 2 comes back empty once, then full; page 3 is the real short last page.
    runner = Runner(pages={1: _full(1), 2: [[], _full(101)], 3: [_issue(201)]})
    assert len(GitHubRest(runner=runner).issue_list()) == 201


def test_a_short_page_mid_list_is_refetched():
    runner = Runner(pages={1: [_full(1)[:46], _full(1)], 2: [_issue(101)]})
    assert len(GitHubRest(runner=runner).issue_list()) == 101


def test_a_page_that_stays_short_while_the_next_has_items_raises():
    runner = Runner(pages={1: [[_issue(1)]] * 9, 2: [_issue(101)]})
    with pytest.raises(GhError, match="truncated listing"):
        GitHubRest(runner=runner).issue_list()


def test_a_genuine_short_last_page_ends_the_listing_with_one_peek():
    """Positive control: a real last page costs one empty-page peek, no refetch."""
    runner = Runner(pages={1: _full(1), 2: [_issue(101)]})
    assert len(GitHubRest(runner=runner).issue_list()) == 101
    assert [p for _, path, p in runner.calls if "state=open" in path] == [1, 2, 3]


# --- open-arch-revision keeps every --blocks ----------------------------------------------

@pytest.mark.parametrize("argv", [["--blocks", "4109", "--blocks", "4110"],
                                  ["--blocks", "4109", "4110"]])
def test_open_arch_revision_keeps_every_blocks_value(monkeypatch, argv):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_open_arch_revision", lambda *a: seen.append(a[4]) or {})
    s.main(["open-arch-revision", "9", "--title", "t", "--body", "b", *argv])
    assert seen == [[4109, 4110]]


# --- post-comment refuses a stale repeat ---------------------------------------------------

from tests.test_v2_phase_tasks import FakeGh  # noqa: E402


def _post(tmp_path, gh, text, role="architecture"):
    f = tmp_path / "c.md"
    f.write_text(text)
    return s.cmd_post_comment(gh, 5, role, str(f))


def _arch_gh(comments=()):
    return FakeGh([{"number": 5, "stage": "architecture", "status": "in-progress",
                    "comments": list(comments)}])


def test_post_comment_refuses_a_body_identical_to_the_roles_last_comment(tmp_path):
    gh = _arch_gh()
    assert _post(tmp_path, gh, "Round 1 handoff.")["posted"] is True
    out = _post(tmp_path, gh, "Round 1 handoff.")
    assert out["refused"] is True and out["posted"] is False and "fresh file" in out["reason"]
    assert len(gh.comments_on(5)) == 1


def test_post_comment_posts_a_new_round_and_ignores_other_roles_comments(tmp_path):
    """Positive control: a changed body posts; an identical body from another role does not
    count as this role's last comment."""
    gh = _arch_gh(["Same text.\n\n<!-- role-comment: product @ 2026-10-01T00:00:00Z -->"])
    assert _post(tmp_path, gh, "Same text.")["posted"] is True
    assert _post(tmp_path, gh, "Round 2 handoff.")["posted"] is True
    assert len(gh.comments_on(5)) == 3


# --- pass-gate --doc-only ----------------------------------------------------------------

class _Ws:
    retained = None

    def __init__(self, *a):
        self.path = "/wt"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def gate(monkeypatch):
    gh = FakeGh([{"number": 9, "stage": "product", "status": "awaiting-human-review",
                  "comments": ["<!-- gate-pr: product:40 -->"]}])
    closed, claimed = [], []
    monkeypatch.setattr(s, "phase_task_parent", lambda gh, n: None)
    monkeypatch.setattr(s, "_gate_base", lambda gh, n: "main")
    monkeypatch.setattr(s, "BranchWorkspace", _Ws)
    monkeypatch.setattr(s, "branch_lock", lambda b: _Ws())
    monkeypatch.setattr(s, "git_reconcile_branch", lambda *a, **k: None)
    monkeypatch.setattr(s, "cmd_close_issue",
                        lambda gh, n, repo, runner: closed.append(n) or {"issue": n, "closed": True})
    monkeypatch.setattr(s, "cmd_claim", lambda gh, n, stage: claimed.append((n, stage)))
    return gh, closed, claimed


def test_pass_gate_doc_only_closes_the_unit_instead_of_claiming_development(gate):
    gh, closed, claimed = gate
    out = s.cmd_pass_gate(gh, "/repo", 9, 40, "product", doc_only=True)
    assert closed == [9] and claimed == []
    assert out["doc_only"] is True and out["next_stage"] is None and out["closed"] is True
    assert "whole deliverable" in gh.comments_on(9)[-1]


def test_pass_gate_without_doc_only_still_claims_the_next_stage(gate):
    """Positive control: the default path is unchanged."""
    gh, closed, claimed = gate
    assert s.cmd_pass_gate(gh, "/repo", 9, 40, "product")["next_stage"] == "architecture"
    assert closed == [] and claimed == [(9, "architecture")]


def test_pass_gate_doc_only_refuses_a_phase_task_and_the_ci_path(gate, monkeypatch):
    gh, closed, _ = gate
    with pytest.raises(GhError, match="passed by CI"):
        s.cmd_pass_gate(gh, "/repo", 9, 40, "product", live=False, doc_only=True)
    monkeypatch.setattr(s, "phase_task_parent", lambda gh, n: {"number": 3, "kind": "epic"})
    with pytest.raises(GhError, match="phase-Task"):
        s.cmd_pass_gate(gh, "/repo", 9, 40, "product", doc_only=True)
    assert closed == []


def test_cli_wires_pass_gate_doc_only(monkeypatch):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_pass_gate", lambda *a, **k: seen.append(k) or {})
    s.main(["pass-gate", "9", "--gate-pr", "40", "--stage", "product", "--doc-only"])
    s.main(["pass-gate", "9", "--gate-pr", "40", "--stage", "product"])
    assert seen == [{"doc_only": True}, {"doc_only": False}]


# --- create-lld-tasks applies `Placement: local` ------------------------------------------

from tests.test_v2_phase_tasks import (DOC, _push_doc_branch, _v2_tree,  # noqa: E402,F401
                                       repo)

_PLACED_LLD = (
    "# lld for epic 9\n\n"
    "## Task apk: Android build\n"
    "Placement: local — needs dl.google.com, which the cloud network blocks\n\n"
    "## Footprint\n- `android/build.gradle`\n\n"
    "## Task copy: Copy\n"
    "Design.\n\n"
    "## Footprint\n- `src/copy.js`\n")


def test_create_lld_tasks_labels_a_placement_local_task_and_only_it(repo):
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _PLACED_LLD)
    tasks = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))["tasks"]
    assert s.local_label() in gh.issues[tasks["apk"]]["labels"]
    assert s.local_label() not in gh.issues[tasks["copy"]]["labels"]  # positive control
