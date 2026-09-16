"""V2 phase-Task lifecycle against a real git origin and an in-memory work-item
provider.

Every test here reproduces a defect found on 2026-09-15 by driving the redesigned
phase-Task flow live against a throwaway GitHub repo. None of them showed up in
`test_sdlc_next.py`, whose `ScriptedRunner` answers every `git` call with whatever
the test scripted -- so a command sequence git itself rejects (a pathspec commit
of an index-only path) or leaves dirty (a committed path never written to the
working tree) passed there unchanged. Git is real here; only GitHub is faked.
"""
import json
import subprocess
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_sdlc_next import _STAGE_TITLE_CASE, _STATUS_TITLE_CASE

DOC = s.DOC_ROOT


def _git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


class FakeGh:
    """The subset of `WorkItemProvider` these flows touch, backed by a dict.
    An unimplemented method raises AttributeError, so a flow reaching for
    something new fails loudly instead of silently no-op'ing."""

    def __init__(self, issues: list):
        self.issues = {}
        self.blocked: dict = {}
        for i in issues:
            self.issues[i["number"]] = {
                "title": f"issue {i['number']}", "state": "OPEN", "labels": [],
                "parent": None, "stage": None, "status": None, "comments": [],
                "issue_type": None, "created": "2026-08-01T00:00:00Z", "body": "", **i}

    def _node(self, n: int) -> dict:
        i = self.issues[n]
        fields = {}
        if i["stage"]:
            fields["Stage"] = _STAGE_TITLE_CASE[i["stage"]]
        if i["status"]:
            fields["Pipeline Status"] = _STATUS_TITLE_CASE[i["status"]]
        return {"number": n, "title": i["title"], "state": i["state"], "body": i.get("body", ""),
                "createdAt": i["created"],
                "issueType": {"name": i["issue_type"]} if i["issue_type"] else None,
                "labels": [{"name": l} for l in i["labels"]],
                "parent": {"number": i["parent"]} if i["parent"] else None,
                "fields": fields}

    def issue_list(self):
        return [self._node(n) for n in sorted(self.issues)]

    def issue_view(self, n):
        return {**self._node(n), "comments": [{"body": b} for b in self.issues[n]["comments"]]}

    def issue_epic_info(self, n):
        node = self._node(n)
        return {"issueType": node["issueType"], "parent": node["parent"], "labels": node["labels"]}

    def classify_unit(self, n):
        return s.classify_unit_from_issue(self.issue_epic_info(n))

    def issue_comment(self, n, body):
        self.issues[n]["comments"].append(body)

    def issue_close(self, n):
        self.issues[n]["state"] = "CLOSED"

    def issue_edit(self, n, add_labels=(), remove_labels=(), add_assignees=(), **_):
        labels = self.issues[n]["labels"]
        labels.extend(l for l in add_labels if l not in labels)
        self.issues[n]["labels"] = [l for l in labels if l not in remove_labels]

    def set_stage_field(self, n, stage):
        self.issues[n]["stage"] = stage

    def clear_stage_field(self, n):
        self.issues[n]["stage"] = None

    def set_pipeline_status_field(self, n, status):
        self.issues[n]["status"] = status

    def clear_pipeline_status_field(self, n):
        self.issues[n]["status"] = None

    def clear_stage_and_status_fields(self, n):
        self.issues[n]["stage"] = self.issues[n]["status"] = None

    def blocked_by(self, n):
        return [d for d in self.blocked.get(n, []) if self.issues[d]["state"] == "OPEN"]

    def blocking(self, n):
        return []

    # --- write side, for the flows that create work items (create-lld-tasks) ---

    def issue_create(self, title, body, labels):
        number = (max(self.issues) if self.issues else 0) + 1
        self.issues[number] = {
            "title": title, "state": "OPEN", "labels": list(labels), "parent": None,
            "stage": None, "status": None, "comments": [], "issue_type": None,
            "created": "2026-09-16T00:00:00Z", "body": body}
        return number

    def set_issue_type(self, n, type_name):
        self.issues[n]["issue_type"] = type_name

    def add_sub_issue(self, parent_number, child_number):
        self.issues[child_number]["parent"] = parent_number

    def add_blocked_by(self, issue_number, blocking_number):
        self.blocked.setdefault(issue_number, []).append(blocking_number)

    def comments_on(self, n):
        return self.issues[n]["comments"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of a bare origin carrying one commit on `main`; worktrees (live
    and ephemeral) land under tmp_path, never the configured /tmp root."""
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(clone))
    _git("checkout", "-q", "-B", "main", cwd=clone)
    (clone / "README.md").write_text("x\n")
    _git("add", "README.md", cwd=clone)
    _git("commit", "-qm", "init", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    wt_root = tmp_path / "wt"
    wt_root.mkdir()
    monkeypatch.setitem(s.PIPELINE["worktrees"], "root", str(wt_root))
    return clone


def _push_doc_branch(clone, branch: str, path: str, text: str, merge_to_main: bool = False):
    """Push `branch` = origin/main + one doc commit, leaving the clone on `main`
    (a branch held by the main checkout is refused by every write command)."""
    _git("fetch", "-q", "origin", cwd=clone)
    _git("checkout", "-q", "-B", branch, "origin/main", cwd=clone)
    target = clone / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=clone)
    _git("commit", "-qm", f"add {path}", cwd=clone)
    _git("push", "-q", "origin", branch, cwd=clone)
    if merge_to_main:
        _git("push", "-q", "origin", f"{branch}:main", cwd=clone)
    _git("checkout", "-q", "main", cwd=clone)


def _origin_file(clone, ref: str, path: str) -> str:
    _git("fetch", "-q", "origin", cwd=clone)
    return _git("show", f"origin/{ref}:{path}", cwd=clone)


def _v2_tree(*extra):
    return FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic"], "parent": 6},
        *extra,
    ])


# --- bugs 3 + 4: the epic branch must exist on origin -------------------------

def test_publish_doc_creates_the_epic_branch_on_origin_when_nothing_has(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9})
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/architecture.md", "# arch\n")

    result = s.cmd_publish_doc(gh, str(repo), 10, "architecture.md")

    assert result["merged"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"


def test_publish_doc_pushes_an_epic_branch_that_exists_only_locally(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9})
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/architecture.md", "# arch\n")
    _git("branch", "epic-9", "origin/main", cwd=repo)

    result = s.cmd_publish_doc(gh, str(repo), 10, "architecture.md")

    assert result["merged"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"


def test_worktree_add_for_a_fresh_epic_pushes_the_epic_branch(repo):
    gh = _v2_tree()

    s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))

    assert _git("ls-remote", "--heads", "origin", "epic-9", cwd=repo).strip()


# --- bugs 5 + 6: cross-path publish commits, and leaves the tree clean --------

def test_publish_doc_publishes_two_cross_path_docs_through_a_live_epic_worktree(repo, tmp_path):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9},
                  {"number": 11, "labels": ["type:task"], "parent": 9})
    _git("push", "-q", "origin", "main:refs/heads/epic-9", cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    epic_wt = tmp_path / "wt" / "sdlc-epic-9"
    _git("worktree", "add", "-q", str(epic_wt), "-B", "epic-9", "origin/epic-9", cwd=repo)
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/architecture.md", "# arch\n")
    _push_doc_branch(repo, "issue-11", f"{DOC}/issue-11/lld.md", "# lld\n")

    arch = s.cmd_publish_doc(gh, str(repo), 10, "architecture.md")
    lld = s.cmd_publish_doc(gh, str(repo), 11, "lld.md")

    assert arch["merged"] is True
    assert lld["merged"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md") == "# lld\n"
    assert _git("status", "--porcelain", cwd=epic_wt) == ""
    assert (epic_wt / DOC / "epic-9" / "lld.md").read_text() == "# lld\n"


def test_epic_announces_lld_md_once_across_publish_doc_and_merge_lld_doc(repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"},
                  {"number": 13, "labels": ["type:task"], "parent": 9})
    _push_doc_branch(repo, "issue-11", f"{DOC}/issue-11/lld.md", "# lld\n")

    s.cmd_publish_doc(gh, str(repo), 11, "lld.md")
    merged = s.cmd_merge_lld_doc(gh, str(repo), 9, unit="epic")

    assert merged["advanced_tasks"] == [13]
    announcements = [c for c in gh.comments_on(9) if "`lld.md` published" in c]
    assert len(announcements) == 1


# --- bugs 1 + 2: a phase-Task's gate finishes the Task ------------------------

def test_close_issue_closes_the_issue_and_marks_it_done(repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9,
                   "stage": "lld", "status": "in-progress"})

    result = s.cmd_close_issue(gh, 11, repo_path=str(repo))

    assert result["issue"] == 11 and result["closed"] is True
    assert gh.issues[11]["state"] == "CLOSED"
    assert gh.issues[11]["stage"] is None
    assert gh.issues[11]["status"] == "done"


def test_pass_gate_on_an_initiative_roadmap_task_closes_it_instead_of_claiming(repo):
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product",
         "status": "awaiting-human-review", "comments": ["<!-- gate-pr: product:8 -->"]},
    ])
    _push_doc_branch(repo, "issue-7", f"{DOC}/issue-7/product.md", "# prd\n", merge_to_main=True)

    result = s.cmd_pass_gate(gh, str(repo), 7, 8, "product")

    assert result["phase_task_complete"] is True
    assert result.get("claimed") is not True
    assert gh.issues[7]["state"] == "CLOSED"
    assert gh.issues[7]["status"] == "done"
    assert gh.issues[7]["stage"] is None


def test_pass_gate_on_an_architecture_phase_task_publishes_then_closes(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "awaiting-human-review",
                   "comments": ["<!-- gate-pr: architecture:12 -->"]})
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/architecture.md", "# arch\n",
                     merge_to_main=True)

    result = s.cmd_pass_gate(gh, str(repo), 10, 12, "architecture")

    assert result["phase_task_complete"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"
    assert gh.issues[10]["state"] == "CLOSED"
    assert any("`architecture.md` published" in c for c in gh.comments_on(9))


def test_pass_gate_on_an_architecture_phase_task_stays_open_when_nothing_to_publish(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "awaiting-human-review",
                   "comments": ["<!-- gate-pr: architecture:12 -->"]})
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/notes.md", "no arch doc\n",
                     merge_to_main=True)

    result = s.cmd_pass_gate(gh, str(repo), 10, 12, "architecture")

    assert result["phase_task_complete"] is False
    assert gh.issues[10]["state"] == "OPEN"


def test_skip_gate_on_an_architecture_phase_task_publishes_then_closes(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "in-progress"})
    _push_doc_branch(repo, "issue-10", f"{DOC}/issue-10/architecture.md", "# arch\n")

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=str(repo))

    assert result["phase_task_complete"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"
    assert gh.issues[10]["state"] == "CLOSED"
    assert gh.issues[10]["status"] == "done"


# --- bug 7: a V2 Epic is surfaced for closing ---------------------------------

def test_check_epics_closeable_surfaces_a_v2_epic_and_checks_its_own_docs():
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
                  {"number": 11, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    on_branch = {(f"{DOC}/epic-9/architecture.md", "epic-9")}
    gh.path_on_ref = lambda path, ref="main": (path, ref) in on_branch

    result = s.cmd_check_epics_closeable(gh)

    assert [e["epic"] for e in result["closeable_epics"]] == [9]
    assert result["closeable_epics"][0]["docs_missing_from_epic_branch"] == [
        f"{DOC}/epic-9/lld.md"]


# --- bug 8: a V2 Epic's Tasks wait for its lld.md ------------------------------

def test_next_action_never_hands_out_an_unstaged_task_before_the_v2_epic_is_architected():
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
                  {"number": 13, "labels": ["type:task"], "parent": 9})

    result = s.decide_next_action(gh, 9)

    assert result["action"] == "none"
    assert gh.issues[13]["stage"] is None


def test_next_action_prefers_the_staged_lld_phase_task_over_older_unstaged_tasks():
    gh = _v2_tree({"number": 13, "labels": ["type:task"], "parent": 9,
                   "created": "2026-07-01T00:00:00Z"},
                  {"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                   "created": "2026-08-01T00:00:00Z"})

    result = s.decide_next_action(gh, 9)

    assert result == {"action": "delegate", "issue": 11, "unit": "issue", "stage": "lld"}
    assert gh.issues[13]["stage"] is None


def test_next_action_hands_out_advanced_tasks_once_the_v2_epic_is_architected():
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", s.LABELS["architected"]], "parent": 6},
        {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development"},
    ])

    assert s.decide_next_action(gh, 9) == {
        "action": "delegate", "issue": 13, "unit": "issue", "stage": "development"}


# --- Round 2: defects from an independent live run (Initiative #28), 2026-09-15 ---

def _worktree_with_doc(gh, repo, number: int, path: str, text: str) -> str:
    """Stand up a phase-Task's worktree the CLI's way, commit a doc there and
    push it -- the state a real stage agent leaves behind."""
    wt = s.cmd_worktree_add(gh, number, repo_path=str(repo), base="origin/main")["path"]
    target = Path(wt) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=wt)
    _git("commit", "-qm", f"add {path}", cwd=wt)
    _git("push", "-q", "origin", f"issue-{number}", cwd=wt)
    return wt


# D1: a Task heading the docs never pinned down silently dropped the Task.

@pytest.mark.parametrize("heading", [
    "## Task #35: greet endpoint", "## Task 35 — greet endpoint",
    "### Task #35", "### Task 35: greet endpoint"])
def test_parse_task_footprint_accepts_the_task_heading_variants(heading):
    doc = (f"# lld\n\n{heading}\n\n## Footprint\n- `src/a.js`\n\n"
           f"## Task #36: other\n\n## Footprint\n- `src/b.js`\n")

    assert s.parse_task_footprint(doc, 35) == ["src/a.js"]


def test_list_parallel_ready_names_the_task_heading_it_could_not_find(repo):
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", s.LABELS["architected"]], "parent": 6},
        {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development"},
    ])
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md",
                     "# lld\n\n## Implement greet (#13)\n\n## Footprint\n- `src/a.js`\n")

    result = s.cmd_list_parallel_ready(gh, str(repo), 9)

    [skip] = [x for x in result["skipped"] if x["issue"] == 13]
    assert "## Task #13" in skip["reason"]


# D2: the Epic comment cited a blob SHA as a commit; a no-op re-run said merged.

def test_merge_lld_doc_cites_the_epic_branch_commit_and_a_rerun_is_not_merged(repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"},
                  {"number": 13, "labels": ["type:task"], "parent": 9})
    _push_doc_branch(repo, "issue-11", f"{DOC}/issue-11/lld.md", "# lld\n")
    s.cmd_publish_doc(gh, str(repo), 11, "lld.md")

    first = s.cmd_merge_lld_doc(gh, str(repo), 9, unit="epic")
    rerun = s.cmd_merge_lld_doc(gh, str(repo), 9, unit="epic")

    tip = _git("rev-parse", "origin/epic-9", cwd=repo).strip()
    [phase_comment] = [c for c in gh.comments_on(9) if "design phase" in c]
    assert tip in phase_comment
    assert first["merged"] is True
    assert rerun["merged"] is False
    assert rerun["verified_on_origin"] is True
    assert rerun["advanced_tasks"] == []


# D3: gates.md now allows squash-merging a phase-Task's gate; prove pass-gate copes.

def test_pass_gate_on_an_architecture_phase_task_after_a_squash_merged_gate(repo):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
                   "status": "awaiting-human-review",
                   "comments": ["<!-- gate-pr: architecture:12 -->"]})
    doc_path = f"{DOC}/issue-10/architecture.md"
    _push_doc_branch(repo, "issue-10", doc_path, "# arch\n")
    (repo / doc_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / doc_path).write_text("# arch\n")
    _git("add", doc_path, cwd=repo)
    _git("commit", "-qm", "Architecture phase (#12) squashed", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    result = s.cmd_pass_gate(gh, str(repo), 10, 12, "architecture")

    assert result["phase_task_complete"] is True
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"


# D4: the closing checklist said "merged" for children that were only closed.

def test_check_epics_closeable_checklist_counts_closed_children_not_merged():
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    gh.path_on_ref = lambda path, ref="main": True

    s.cmd_check_epics_closeable(gh)

    [checklist] = gh.comments_on(9)
    assert "1 child issue(s) closed" in checklist
    assert "merged\n" not in checklist.splitlines()[0] + "\n"


# D5: mark-issue-closed called a V2 Epic and an Initiative "not an epic".

def test_mark_issue_closed_classifies_v2_epics_and_initiatives():
    gh = _v2_tree()

    assert s.cmd_mark_issue_closed(gh, 9)["is_epic"] is True
    assert s.cmd_mark_issue_closed(gh, 6)["is_initiative"] is True


# D6: next-action on an Initiative gave no reason when fresh, a stale one when closed.

def test_next_action_on_a_fresh_initiative_says_to_cut_its_roadmap_task():
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]}])

    result = s.decide_next_action(gh, 6)

    assert result["action"] == "none"
    assert "Product-Roadmap Task" in result["reason"]


def test_next_action_on_a_closed_initiative_says_it_is_closed():
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"], "state": "CLOSED"},
        {"number": 9, "labels": ["type:epic"], "parent": 6, "state": "CLOSED"},
    ])

    result = s.decide_next_action(gh, 6)

    assert "is closed" in result["reason"]
    assert "ready for initiative-close" not in result["reason"]


# D7: close-initiative left Pipeline Status unset until a CI job ran.

def test_close_initiative_sets_the_terminal_fields_itself():
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"], "status": "in-progress",
         "comments": ["<!-- initiative-verification: requirements:6 @ 2026-09-15T00:00:00Z -->"]},
        {"number": 9, "labels": ["type:epic"], "parent": 6, "state": "CLOSED"},
    ])

    assert s.cmd_close_initiative(gh, 6)["closed"] is True
    assert gh.issues[6]["state"] == "CLOSED"
    assert gh.issues[6]["status"] == "done"


# D8: a finished phase-Task's worktree was left behind as a stale lane slot.

def test_pass_gate_on_a_phase_task_releases_its_worktree(repo):
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product",
         "status": "awaiting-human-review", "comments": ["<!-- gate-pr: product:8 -->"]},
    ])
    wt = _worktree_with_doc(gh, repo, 7, f"{DOC}/issue-7/product.md", "# prd\n")
    _git("push", "-q", "origin", "origin/issue-7:refs/heads/main", cwd=repo)

    result = s.cmd_pass_gate(gh, str(repo), 7, 8, "product")

    assert result["phase_task_complete"] is True
    assert not Path(wt).exists()


def test_close_issue_releases_the_issue_worktree(repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    wt = _worktree_with_doc(gh, repo, 11, f"{DOC}/issue-11/lld.md", "# lld\n")

    result = s.cmd_close_issue(gh, 11, repo_path=str(repo))

    assert result["worktree"]["released"] is True
    assert not Path(wt).exists()


def test_close_epic_sets_the_terminal_fields_when_it_merges():
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"})
    gh.issues[9]["comments"] = [
        "<!-- epic-verification: e2e:9 @ 2026-09-15T00:00:00Z -->",
        "<!-- epic-verification: exploratory:9 @ 2026-09-15T00:01:00Z -->"]
    merged = []
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.pr_list_for_branch = lambda branch: []
    gh.pr_create = lambda **kw: 38
    gh.pr_checks = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_files = lambda n: []
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: merged.append(n)

    result = s.cmd_close_epic(gh, 9)

    assert result["merged"] is True and merged == [38]
    assert gh.issues[9]["status"] == "done"


# =============================================================================
# 2026-09-16 retro. Real git, faked tracker -- the same split as the rest of
# this file, for the same reason: the defects below are about ref state and
# on-disk trees, which a scripted runner answers however the test asked.
#
# Each fix pairs a REGRESSION test with a POSITIVE CONTROL that is green both
# before and after the fix.
# =============================================================================


# --- Fix 3: a resumed worktree is brought up to origin -----------------------

def _live_epic_worktree(repo, tmp_path, epic=9):
    """Push `epic-<n>` and check it out in its own live worktree at the path the
    pipeline uses -- the state a run already in flight leaves behind."""
    _git("push", "-q", "origin", f"main:refs/heads/epic-{epic}", cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    wt = tmp_path / "wt" / f"sdlc-epic-{epic}"
    _git("worktree", "add", "-q", str(wt), "-B", f"epic-{epic}", f"origin/epic-{epic}", cwd=repo)
    return wt


def _advance_origin(repo, branch, path, text):
    """Land a commit on `origin/<branch>` from outside the worktree -- what a
    sibling PR merging into the epic branch looks like to a tree already open."""
    _git("fetch", "-q", "origin", cwd=repo)
    _git("checkout", "-q", "-B", f"advance-{branch}", f"origin/{branch}", cwd=repo)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git("add", path, cwd=repo)
    _git("commit", "-qm", f"advance {branch}", cwd=repo)
    _git("push", "-q", "origin", f"advance-{branch}:{branch}", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)


def test_worktree_add_fast_forwards_a_resumed_worktree_to_origin(repo, tmp_path):
    # epic-53, live: the worktree existed, four PRs had merged into
    # origin/epic-53 since it was opened, and worktree-add handed the stale tree
    # straight back with nothing in the result saying how old it was.
    gh = _v2_tree()
    wt = _live_epic_worktree(repo, tmp_path)
    _advance_origin(repo, "epic-9", "merged.txt", "from a merged PR\n")

    result = s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))

    assert result["created"] is False and result["resumed"] is True
    assert result["behind_before"] == 1
    assert result["synced_to_origin"] is True
    assert (wt / "merged.txt").read_text() == "from a merged PR\n"


def test_worktree_add_refuses_to_force_a_diverged_resumed_worktree(repo, tmp_path):
    # Fast-forward only. A branch with commits on both sides carries unpushed
    # work, and discarding a commit to make a resume look clean is the one
    # outcome this must never have.
    gh = _v2_tree()
    wt = _live_epic_worktree(repo, tmp_path)
    (wt / "local.txt").write_text("unpushed work\n")
    _git("add", "local.txt", cwd=wt)
    _git("commit", "-qm", "local only", cwd=wt)
    _advance_origin(repo, "epic-9", "merged.txt", "from a merged PR\n")

    result = s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))

    assert result["diverged"] is True
    assert result["synced_to_origin"] is False
    assert result["behind_before"] == 1 and result["ahead_of_origin"] == 1
    assert (wt / "local.txt").exists()          # never force-reset
    assert not (wt / "merged.txt").exists()     # and never silently merged


def test_worktree_add_leaves_a_current_worktree_and_its_wip_alone(repo, tmp_path):
    """POSITIVE CONTROL: resuming a tree already at origin's tip must not touch
    it -- in particular it must not reset away uncommitted work. Green before
    the sync fix and after it, so a "fix" that force-resets every resume fails
    here."""
    gh = _v2_tree()
    wt = _live_epic_worktree(repo, tmp_path)
    (wt / "wip.txt").write_text("uncommitted\n")

    result = s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))

    assert result["created"] is False
    assert Path(result["path"]).resolve() == wt.resolve()
    assert (wt / "wip.txt").read_text() == "uncommitted\n"


# --- Fix 5: maxTasksPerRun is enforced, not merely reported ------------------

@pytest.fixture
def run_state(tmp_path, monkeypatch):
    """Point the run-state files at this test's own directory and give the cap a
    real value -- the sample config ships 0, which means unlimited."""
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setenv("SDLC_RUNS_DIR", str(d))
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 2)
    return d


def _write_run(run_state, terminal, run_id="run-1", epic=9):
    (run_state / f"epic-{epic}.json").write_text(
        json.dumps({"run_id": run_id, "terminal": terminal}))


def _architected_epic(**task):
    return FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", s.LABELS["architected"]], "parent": 6},
        {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development", **task},
    ])


def test_next_action_stops_at_the_run_cap_instead_of_handing_out_new_work(run_state):
    gh = _architected_epic()
    _write_run(run_state, [11, 12])

    result = s.decide_next_action(gh, 9, run_id="run-1")

    assert result["action"] == "stop-at-cap"
    assert result["cap"] == 2
    assert result["completed"] == [11, 12]
    # A deferred unit must come away with no side effects at all.
    assert gh.issues[13]["stage"] == "development"
    assert gh.issues[13]["status"] is None


def test_next_action_never_caps_a_resume(run_state):
    # Resumes finish work already in flight. Capping one would strand a unit
    # mid-pipeline rather than bound anything.
    gh = _architected_epic(status="in-progress")
    _write_run(run_state, [11, 12])

    result = s.decide_next_action(gh, 9, run_id="run-1")

    assert result["action"] == "resume" and result["issue"] == 13


def test_next_action_never_caps_a_gate_action(run_state):
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", s.LABELS["architected"]], "parent": 6},
        {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "lld",
         "status": "awaiting-human-review", "comments": ["<!-- gate-pr: lld:14 -->"]},
    ])
    gh.pr_view = lambda n, fields="": {"state": "MERGED"}
    _write_run(run_state, [11, 12])

    result = s.decide_next_action(gh, 9, run_id="run-1")

    assert result["action"] == "pass-gate" and result["issue"] == 13


def test_list_parallel_ready_hands_out_nothing_at_the_run_cap(run_state, repo):
    # Capping next-action alone would just push the overflow into the other
    # lane that hands out fresh work.
    gh = _architected_epic()
    _write_run(run_state, [11, 12])

    result = s.cmd_list_parallel_ready(gh, str(repo), 9, run_id="run-1")

    assert result["parallel_ready"] == []
    assert result["stop_at_cap"] is True
    assert result["cap_enforced"] is True


def test_a_new_run_id_resets_the_cap(run_state):
    gh = _architected_epic()
    _write_run(run_state, [11, 12], run_id="run-1")

    result = s.decide_next_action(gh, 9, run_id="run-2")

    assert result["action"] == "delegate" and result["issue"] == 13
    assert json.loads((run_state / "epic-9.json").read_text()) == {
        "run_id": "run-2", "terminal": []}


def test_next_action_below_the_cap_still_delegates(run_state):
    gh = _architected_epic()
    _write_run(run_state, [11])

    assert s.decide_next_action(gh, 9, run_id="run-1")["action"] == "delegate"


def test_close_issue_counts_the_closed_unit_against_the_run(run_state, repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"})
    _write_run(run_state, [])

    s.cmd_close_issue(gh, 11, repo_path=str(repo))

    assert json.loads((run_state / "epic-9.json").read_text())["terminal"] == [11]


def test_merge_pr_counts_the_merged_unit_against_the_run(run_state, repo):
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "development",
                   "comments": [
                       "<!-- stage-transition: development->pr-review @ 2026-09-16T00:00:00Z -->",
                       "<!-- pr-review-outcome: clean:42 @ 2026-09-16T01:00:00Z -->"]})
    gh._run = s._default_runner
    gh.branch_behind_by = lambda head, base="main": 0
    gh.pr_checks = lambda n: []
    gh.pr_files = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n: None
    gh.pr_comment = lambda n, body: None
    _write_run(run_state, [])

    result = s.cmd_merge_pr(gh, 42, issue=11, repo_path=str(repo))

    assert result["merged"] is True
    assert json.loads((run_state / "epic-9.json").read_text())["terminal"] == [11]


def test_run_cap_is_opt_in_and_ignored_without_a_run_id(run_state):
    """POSITIVE CONTROL: with no --run-id the cap is off, so a run-state file
    already AT the cap changes nothing and work is still handed out. Green
    before enforcement existed and after it -- a cap that fired unconditionally
    would fail here."""
    gh = _architected_epic()
    _write_run(run_state, [11, 12])

    assert s.decide_next_action(gh, 9)["action"] == "delegate"


# --- Fix 7: Task issues are created after the carving is approved ------------

_CARVED_LLD = (
    "# lld for epic 9\n\n"
    "## Task skeleton-health: Add /health endpoint\n"
    "Design for the health endpoint.\n\n"
    "## Footprint\n- `src/health.js`\n\n"
    "## Task greet-endpoint: Add /greet endpoint\n"
    "Depends on: skeleton-health\n\n"
    "## Footprint\n- `src/greet.js`\n")


def test_create_lld_tasks_creates_numbers_and_wires_the_carving(repo):
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED_LLD)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["skeleton-health", "greet-endpoint"]
    health = result["tasks"]["skeleton-health"]
    greet = result["tasks"]["greet-endpoint"]
    # Parented to the Epic, typed, and classifiable as Tasks.
    assert gh.issues[health]["parent"] == 9 and gh.issues[greet]["parent"] == 9
    assert s.classify_unit_from_issue(gh.issue_epic_info(health)) == "task"
    # `Depends on:` became the native edge, in the right direction.
    assert result["blocked_by"] == [{"issue": greet, "on": health}]
    assert gh.blocked_by(greet) == [health]
    # The doc on origin is renumbered in place, slug preserved.
    published = _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md")
    assert f"## Task #{health}: Add /health endpoint <!-- task-key: skeleton-health -->" \
        in published
    assert "## Task skeleton-health" not in published
    assert result["pushed"] is True
    # Each Task's own subsection still resolves -- by number, and by slug.
    assert s.parse_task_footprint(published, health) == ["src/health.js"]
    assert s.parse_task_footprint(published, "greet-endpoint") == ["src/greet.js"]


def test_create_lld_tasks_is_idempotent_on_a_rerun(repo):
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED_LLD)
    first = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    issues_after_first = set(gh.issues)

    second = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert len(first["created"]) == 2
    assert second["created"] == []
    assert second["committed"] is None
    assert "already carries an issue number" in second["reason"]
    assert set(gh.issues) == issues_after_first  # nothing duplicated


def test_create_lld_tasks_repairs_a_crash_between_create_and_push(repo):
    # The crash window: the issue was created but the rewritten doc never
    # reached origin, so the heading is still keyed. The task-key marker in the
    # issue body is what lets a re-run adopt it instead of creating a second
    # issue for the same section.
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED_LLD)
    orphan = gh.issue_create("Add /health endpoint",
                             "x\n\n<!-- task-key: skeleton-health -->", ["type:task"])
    gh.add_sub_issue(9, orphan)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [r["issue"] for r in result["reused"]] == [orphan]
    assert [c["key"] for c in result["created"]] == ["greet-endpoint"]
    published = _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md")
    assert f"## Task #{orphan}: Add /health endpoint" in published


def test_create_lld_tasks_refuses_when_the_epic_lld_is_not_published(repo):
    gh = _v2_tree()
    with pytest.raises(s.GhError, match="publish the Epic's lld.md first"):
        s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))


def test_merge_lld_doc_epic_still_advances_the_tasks_it_finds(repo):
    """POSITIVE CONTROL for moving Task creation out of `lld`: `merge-lld-doc
    --unit epic` keeps its current behaviour, advancing every Stage-less Task
    under the Epic. Green before the change and after it."""
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld"},
                  {"number": 13, "labels": ["type:task"], "parent": 9})
    _push_doc_branch(repo, "issue-11", f"{DOC}/issue-11/lld.md", "# lld\n")
    s.cmd_publish_doc(gh, str(repo), 11, "lld.md")

    merged = s.cmd_merge_lld_doc(gh, str(repo), 9, unit="epic")

    assert merged["advanced_tasks"] == [13]
    assert gh.issues[13]["stage"] == "development"
