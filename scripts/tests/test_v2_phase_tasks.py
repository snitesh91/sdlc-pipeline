"""V2 phase-Task lifecycle against a real git origin and an in-memory work-item
provider.

Every test here reproduces a defect found on 2026-09-15 by driving the redesigned
phase-Task flow live against a throwaway GitHub repo. None of them showed up in
`test_sdlc_next.py`, whose `ScriptedRunner` answers every `git` call with whatever
the test scripted -- so a command sequence git itself rejects (a pathspec commit
of an index-only path) or leaves dirty (a committed path never written to the
working tree) passed there unchanged. Git is real here; only GitHub is faked.
"""
import subprocess

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
        for i in issues:
            self.issues[i["number"]] = {
                "title": f"issue {i['number']}", "state": "OPEN", "labels": [],
                "parent": None, "stage": None, "status": None, "comments": [],
                "issue_type": None, "created": "2026-08-01T00:00:00Z", **i}

    def _node(self, n: int) -> dict:
        i = self.issues[n]
        fields = {}
        if i["stage"]:
            fields["Stage"] = _STAGE_TITLE_CASE[i["stage"]]
        if i["status"]:
            fields["Pipeline Status"] = _STATUS_TITLE_CASE[i["status"]]
        return {"number": n, "title": i["title"], "state": i["state"], "body": "",
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
        return []

    def blocking(self, n):
        return []

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

def test_close_issue_closes_the_issue_and_marks_it_done():
    gh = _v2_tree({"number": 11, "labels": ["type:task"], "parent": 9,
                   "stage": "lld", "status": "in-progress"})

    result = s.cmd_close_issue(gh, 11)

    assert result == {"issue": 11, "closed": True}
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
