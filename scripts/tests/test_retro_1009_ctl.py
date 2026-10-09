"""Retro 2026-10-09 control-plane fixes: update-pr-body, open-dev-pr recovery/adoption,
record-local-ci on an uncovered PR, verify-exit's pushed-head check."""
import json

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_sdlc_next import ScriptedRunner
from tests.test_v2_phase_tasks import FakeGh


class _BodyGh(FakeGh):
    def pr_edit_body(self, n, body):
        self.prs[n]["body"] = body


def _file(tmp_path, text, name="b.md"):
    f = tmp_path / name
    f.write_text(text)
    return str(f)


# --- update-pr-body (dev-rework-pr-body-edit-blocked) ---

def test_update_pr_body_replaces_the_description_and_keeps_the_closing_line(tmp_path):
    gh = _BodyGh([], prs={42: {"headRefName": "issue-9", "body": "old\n\nCloses #9"}})
    out = s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New: rework round 2 changed X."))
    assert out["updated"] is True and out["kept_closing_lines"] == ["Closes #9"]
    assert gh.prs[42]["body"] == "New: rework round 2 changed X.\n\nCloses #9"


def test_update_pr_body_does_not_duplicate_a_closing_line_the_new_body_has(tmp_path):
    gh = _BodyGh([], prs={42: {"body": "old\n\nCloses #9"}})
    s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New.\n\nCloses #9"))
    assert gh.prs[42]["body"] == "New.\n\nCloses #9"


def test_update_pr_body_refuses_a_closed_pr_and_an_empty_file(tmp_path):
    gh = _BodyGh([], prs={42: {"state": "MERGED", "body": "old"}})
    with pytest.raises(GhError, match="not open"):
        s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New."))
    with pytest.raises(GhError, match="empty"):
        s.cmd_update_pr_body(gh, 42, _file(tmp_path, "  \n"))
    assert gh.prs[42]["body"] == "old"


def test_pr_edit_body_is_a_rest_patch_cloud_safe():
    runner = ScriptedRunner({("gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42",
                              "-f", "body=x"): "{}"})
    s.GitHubRest(runner=runner).pr_edit_body(42, "x")
    assert runner.calls == [["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42",
                             "-f", "body=x"]]


def test_cli_wires_update_pr_body(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_update_pr_body", lambda *a: seen.append(a) or {"updated": True})
    assert s.main(["update-pr-body", "42", "--body-file", "b.md"]) == 0
    assert seen == [("GH", 42, "b.md")]


# --- verify-exit refuses a detached or unpushed unit worktree (reviewer-detached-dev-worktree) ---

from tests.test_v2_phase_tasks import _git  # noqa: E402


@pytest.fixture
def unit_tree(tmp_path, monkeypatch):
    """A clone with `issue-9` pushed and its dev worktree at the configured path."""
    monkeypatch.setattr(s, "VERIFY_EXIT_HEAD_CHECK", True)
    monkeypatch.setitem(s.PIPELINE["worktrees"], "root", str(tmp_path / "wt"))
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    _git("clone", "-q", str(origin), str(repo))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git("config", k, v, cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("push", "-q", "origin", "HEAD:main", cwd=repo)
    path = s.worktree_path("issue", 9)
    _git("worktree", "add", "-q", "-b", "issue-9", path, cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "doc", cwd=path)
    _git("push", "-q", "origin", "issue-9", cwd=path)
    return str(repo), path


def _verify(repo, stage="architecture"):
    gh = FakeGh([{"number": 9, "stage": stage, "status": "in-progress"}])
    return s.cmd_verify_exit(gh, repo, 9, stage)


def test_verify_exit_passes_a_pushed_unit_worktree(unit_tree):
    # Positive control: on its branch and pushed.
    repo, _ = unit_tree
    result = _verify(repo)
    assert result.get("ok") is not False and "head_check" not in result


def test_verify_exit_refuses_a_detached_unit_worktree(unit_tree):
    """Regression: a reviewer's `git checkout --detach` in the dev tree left the architect
    committing on no branch; `git push origin issue-9` pushed nothing and it said 'pushed'."""
    repo, path = unit_tree
    _git("checkout", "-q", "--detach", "origin/issue-9", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "rework on a detached head", cwd=path)
    _git("push", "-q", "origin", "issue-9", cwd=path)  # pushes nothing new
    result = _verify(repo)
    assert result["ok"] is False and result["head_check"]["detached"] is True
    assert "detached HEAD" in result["reason"] and "review-worktree-add" in result["reason"]


@pytest.mark.parametrize("stage", ["product", "architecture", "lld", "pr-review"])
def test_verify_exit_refuses_unpushed_work(unit_tree, stage):
    repo, path = unit_tree
    _git("commit", "-q", "--allow-empty", "-m", "not pushed", cwd=path)
    result = _verify(repo, stage)
    assert result["ok"] is False and "not pushed" in result["reason"]
    assert result["head_check"]["local_head"] != result["head_check"]["origin_head"]


def test_verify_exit_refuses_a_never_pushed_branch(tmp_path, unit_tree):
    repo, path = unit_tree
    _git("push", "-q", "origin", "--delete", "issue-9", cwd=path)
    _git("fetch", "-q", "--prune", "origin", cwd=path)
    result = _verify(repo)
    assert result["ok"] is False and result["head_check"]["origin_head"] is None


def test_verify_exit_skips_the_head_check_without_a_dev_worktree(unit_tree):
    repo, path = unit_tree
    _git("worktree", "remove", "--force", path, cwd=repo)
    assert "head_check" not in _verify(repo)


def test_review_worktree_add_leaves_the_dev_worktree_on_its_branch(unit_tree):
    # A design/product reviewer's own detached tree never moves the unit's dev tree.
    repo, path = unit_tree
    out = s.cmd_review_worktree_add(9, repo)
    assert out["path"] != path and out["head"] == _git("rev-parse", "origin/issue-9",
                                                         cwd=repo).strip()
    assert _git("symbolic-ref", "--short", "HEAD", cwd=path).strip() == "issue-9"
    assert s.cmd_release_review_worktree(9, repo)["released"] is True


# --- open-dev-pr: a lost create response, adoption (cloud-draft-pr-create) ---

class _DevPrGh(FakeGh):
    """FakeGh whose PR create can land the PR yet lose the response, as the cloud proxy did."""

    def __init__(self, lose_response=False, draft_sticks=True, convert_fails=False, **kw):
        super().__init__([{"number": 9, "labels": ["type:epic"]},
                          {"number": 10, "labels": ["type:task"], "parent": 9,
                           "stage": "development", "status": "in-progress"}], **kw)
        self.lose_response, self.draft_sticks = lose_response, draft_sticks
        self.convert_fails, self.converted = convert_fails, []

    def files_since(self, sha, branch):
        return []

    def pr_create(self, base, head, title, body, draft=False):
        n = super().pr_create(base, head, title, body, draft and self.draft_sticks)
        if self.lose_response:
            raise GhError(f"{s.PR_CREATE_NO_NUMBER}: POST repos/o/r/pulls returned None")
        return n

    def pr_convert_to_draft(self, n):
        if self.convert_fails:
            raise GhError("no route")
        self.converted.append(n)
        self.prs[n]["isDraft"] = True


def _exit_posted(gh, pr):
    return [c for c in gh.comments_on(10) if f"<!-- dev-pr-opened: #{pr} -->" in c]


def test_open_dev_pr_recovers_a_create_whose_response_was_lost(monkeypatch):
    """Regression: the cloud proxy answered the draft create with an empty body; the PR
    existed but open-dev-pr failed, and the operator finished its exit by hand."""
    monkeypatch.setattr(s, "dev_pr_scope_offenders", lambda *a, **k: {
        "design_docs": [], "foreign_footprint": []})
    gh = _DevPrGh(lose_response=True)
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert out["created"] is True and out["pr"] == 101 and "recovered" in out
    assert gh.issues[10]["stage"] == "pr-review" and len(_exit_posted(gh, 101)) == 1
    assert gh.converted == []


def test_open_dev_pr_drafts_a_recovered_pr_the_draft_flag_missed(monkeypatch):
    monkeypatch.setattr(s, "dev_pr_scope_offenders", lambda *a, **k: {
        "design_docs": [], "foreign_footprint": []})
    gh = _DevPrGh(lose_response=True, draft_sticks=False)
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert out["converted_to_draft"] is True and gh.prs[101]["isDraft"] is True


def test_open_dev_pr_still_raises_when_no_pr_was_made(monkeypatch):
    # Control: a failed create that made nothing is not papered over.
    monkeypatch.setattr(s, "dev_pr_scope_offenders", lambda *a, **k: {
        "design_docs": [], "foreign_footprint": []})
    gh = _DevPrGh()
    gh.pr_create = lambda *a, **k: (_ for _ in ()).throw(GhError("HTTP 422"))
    with pytest.raises(GhError, match="422"):
        s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert gh.issues[10]["stage"] == "development" and gh.comments_on(10) == []


def test_open_dev_pr_adopts_an_open_pr_made_by_another_path():
    """Regression: re-running open-dev-pr on a PR made by hand (MCP) reused it but skipped
    set-stage and the comment, so the unit never left development."""
    gh = _DevPrGh(prs={77: {"headRefName": "issue-10", "isDraft": False}})
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert out["created"] is False and out["adopted"] is True and out["pr"] == 77
    assert gh.issues[10]["stage"] == "pr-review" and len(_exit_posted(gh, 77)) == 1
    assert gh.converted == [77] and out["converted_to_draft"] is True
    # Idempotent: a second run is a plain reuse, no second comment.
    again = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert "adopted" not in again and len(_exit_posted(gh, 77)) == 1


def test_open_dev_pr_adoption_warns_when_drafting_fails():
    gh = _DevPrGh(prs={77: {"headRefName": "issue-10", "isDraft": False}}, convert_fails=True)
    out = s.cmd_open_dev_pr(gh, 10, "t", "b", "Built it.")
    assert out["adopted"] is True and "draft_warning" in out


def test_rest_pr_create_raises_on_an_empty_response():
    runner = ScriptedRunner()
    runner.prefix_responses = {("gh", "api", "-X", "POST", "repos/owner/repo/pulls"): ""}
    with pytest.raises(GhError, match=s.PR_CREATE_NO_NUMBER):
        s.GitHubRest(runner=runner).pr_create("main", "issue-9", "t", "b", draft=True)


def test_rest_convert_to_draft_uses_the_ccr_route_in_the_cloud(monkeypatch):
    monkeypatch.setattr(s, "session_placement", lambda: {"placement": "cloud"})
    runner = ScriptedRunner({("gh", "api", "-X", "POST",
                              "repos/owner/repo/pulls/7/ccr/convert_to_draft"): ""})
    s.GitHubRest(runner=runner).pr_convert_to_draft(7)
    assert len(runner.calls) == 1
