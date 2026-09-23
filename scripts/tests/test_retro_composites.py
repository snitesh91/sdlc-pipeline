"""2026-09-23 composites: advance-standing, skip-pr-review, finish-gate-feedback,
prepare-rework, `next-action --sync-epic` and `close-epic --clean-worktree`.
Each: the happy path, a stop at the first failure (later steps never run), and a
building block's refusal passed through as the stop reason."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import FakeGh, _advance_origin, _git, repo  # noqa: F401

_HANDOFF = "<!-- stage-transition: development->pr-review @ 2026-09-18T00:00:00Z -->"
_BOUNCE = "<!-- pr-review-outcome: rework:42 @ 2026-09-17T00:00:00Z -->"


def _must_not_run(name):
    def fail(*_a, **_k):
        raise AssertionError(f"{name} ran after an earlier step stopped the composite")
    return fail


def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


class Gh(FakeGh):
    """No PRs on any branch unless a test adds them (start-stage's claimable check reads it)."""

    def pr_list_for_branch(self, branch, state="open"):
        return [{"number": n, **p} for n, p in self.prs.items()
                if p.get("headRefName") == branch and p.get("state", "OPEN") == "OPEN"]


def _standing(**child):
    return Gh([{"number": 90, "labels": ["type:epic", "epic:standing"]},
               {"number": 9, "parent": 90, "labels": ["type:task"], **child}])


# --- advance-standing -----------------------------------------------------------

def test_advance_standing_transitions_routes_and_starts_the_target_stage(monkeypatch):
    gh = _standing(stage="product", status="in-progress")
    seen = []
    monkeypatch.setattr(s, "cmd_transition", lambda gh_, n, stage, **k: (
        seen.append((n, stage, k.get("repo_path"))), {"ready": True, "stopped_at": None})[1])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/9"})

    result = s.cmd_advance_standing(gh, 9, "product", "architecture", "product.md is trivial",
                                    repo_path="/wt/9")

    assert result["ok"] is True and result["failed_step"] is None
    assert result["completed_steps"] == ["transition", "route", "start-stage"]
    assert seen == [(9, "product", "/wt/9")]
    assert (result["routed"], result["claimed"], result["path"]) == (True, True, "/wt/9")
    assert result["steps"]["route"]["skipped"] == ["product-review"]
    assert (gh.issues[9]["stage"], gh.issues[9]["status"]) == ("architecture", "in-progress")


def test_advance_standing_from_a_review_verifies_the_stage_it_followed(monkeypatch):
    # A finished review must not get its own start comment re-posted by `transition`.
    gh = _standing(stage="architecture", status="in-progress")
    seen = []
    monkeypatch.setattr(s, "cmd_transition", lambda gh_, n, stage, **k: (
        seen.append(stage), {"ready": True})[1])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/9"})

    result = s.cmd_advance_standing(gh, 9, "arch-review", "development", "small change")

    assert seen == ["architecture"] and result["ok"] is True


def test_advance_standing_at_pickup_skips_the_transition(monkeypatch):
    gh = _standing(issue_type="Bug")
    monkeypatch.setattr(s, "cmd_transition", _must_not_run("transition"))
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/9"})

    result = s.cmd_advance_standing(gh, 9, "none", "development", "one-line fix")

    assert result["completed_steps"] == ["route", "start-stage"]
    assert "transition" not in result["steps"]
    assert (gh.issues[9]["stage"], gh.issues[9]["status"]) == ("development", "in-progress")


def test_advance_standing_stops_at_a_transition_that_is_not_ready(monkeypatch):
    gh = _standing(stage="product", status="in-progress")
    monkeypatch.setattr(s, "cmd_transition", lambda *a, **k: {
        "ready": False, "stopped_at": "sync-branch", "reason": "merge conflict on a.py"})
    monkeypatch.setattr(s, "cmd_route", _must_not_run("route"))
    monkeypatch.setattr(s, "cmd_start_stage", _must_not_run("start-stage"))

    result = s.cmd_advance_standing(gh, 9, "product", "architecture", "r")

    assert (result["ok"], result["failed_step"], result["completed_steps"]) == (
        True, "transition", [])
    assert result["reason"] == "merge conflict on a.py"
    assert result["routed"] is False and result["claimed"] is False


def test_advance_standing_passes_routes_refusal_through_and_never_claims(monkeypatch):
    gh = _standing(stage="architecture", status="in-progress")
    monkeypatch.setattr(s, "cmd_transition", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(s, "cmd_start_stage", _must_not_run("start-stage"))

    code, out = _cli(monkeypatch, gh, ["advance-standing", "9", "--from", "architecture",
                                       "--to", "product", "--reason", "back"])

    assert code == 0 and out["failed_step"] == "route"
    assert "only moves forward" in out["reason"]
    assert gh.issues[9]["stage"] == "architecture"


def test_advance_standing_stops_when_start_stage_refuses(monkeypatch):
    gh = _standing(issue_type="Bug")
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, **k: {"diverged": True, "reason": "local has diverged"})

    result = s.cmd_advance_standing(gh, 9, "none", "development", "fix")

    assert result["failed_step"] == "start-stage" and result["claimed"] is False
    assert result["completed_steps"] == ["route"]
    assert gh.issues[9]["status"] == "todo"  # routed, never claimed


# --- skip-pr-review -------------------------------------------------------------

def test_skip_pr_review_verifies_routes_to_merge_and_merges(monkeypatch):
    gh = _standing(stage="pr-review", status="in-progress", comments=[_HANDOFF])
    merges = []
    monkeypatch.setattr(s, "cmd_verify_exit", lambda gh_, rp, n, stage, pr, **k: {
        "issue": n, "expected_stage_present": stage == "pr-review", "pr": pr})
    monkeypatch.setattr(s, "cmd_merge_pr", lambda gh_, pr, n, rp, run_id=None: (
        merges.append((pr, n, rp, run_id)), {"pr": pr, "merged": True})[1])

    result = s.cmd_skip_pr_review(gh, 9, 42, "README typo", run_id="run-1")

    assert result["ok"] is True and result["merged"] is True
    assert result["completed_steps"] == ["verify-exit", "route", "merge-pr"]
    assert merges == [(42, 9, ".", "run-1")]
    assert "<!-- stage-route: pr-review->merge @ " in gh.comments_on(9)[-1]


def test_skip_pr_review_stops_at_an_unverified_exit(monkeypatch):
    gh = _standing(stage="pr-review", status="in-progress")
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {
        "ok": False, "reason": "no handoff marker", "handoff_marker_present": False})
    monkeypatch.setattr(s, "cmd_route", _must_not_run("route"))
    monkeypatch.setattr(s, "cmd_merge_pr", _must_not_run("merge-pr"))

    code, out = _cli(monkeypatch, gh, ["skip-pr-review", "9", "--pr", "42", "--reason", "x"])

    assert code == 1 and out["failed_step"] == "verify-exit"
    assert out["steps"]["verify-exit"]["handoff_marker_present"] is False


def test_skip_pr_review_surfaces_the_bounce_refusal_and_never_merges(monkeypatch):
    gh = _standing(stage="pr-review", status="in-progress", comments=[_BOUNCE, _HANDOFF])
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"expected_stage_present": True})
    monkeypatch.setattr(s, "cmd_merge_pr", _must_not_run("merge-pr"))

    result = s.cmd_skip_pr_review(gh, 9, 42, "x")

    assert (result["ok"], result["failed_step"], result["merged"]) == (True, "route", False)
    assert "never skip it after a bounce" in result["reason"]


def test_skip_pr_review_stops_on_a_behind_base_merge_refusal(monkeypatch):
    gh = _standing(stage="pr-review", status="in-progress", comments=[_HANDOFF])
    monkeypatch.setattr(s, "cmd_verify_exit", lambda *a, **k: {"expected_stage_present": True})
    monkeypatch.setattr(s, "cmd_merge_pr", lambda *a, **k: {
        "merged": False, "behind_base": 2, "reason": "branch is 2 commit(s) behind main"})

    result = s.cmd_skip_pr_review(gh, 9, 42, "x")

    assert (result["ok"], result["failed_step"]) == (True, "merge-pr")
    assert result["steps"]["merge-pr"]["behind_base"] == 2


# --- finish-gate-feedback -------------------------------------------------------

def test_finish_gate_feedback_marks_addressed_once_the_transition_is_ready(monkeypatch):
    gh = _standing(stage="architecture", status="feedback-received")
    seen = []
    monkeypatch.setattr(s, "cmd_transition", lambda gh_, n, stage, **k: (
        seen.append((stage, k.get("repo_path"))), {"ready": True})[1])

    code, out = _cli(monkeypatch, gh, ["finish-gate-feedback", "9", "--expect-stage",
                                       "architecture", "--repo-path", "/wt/9"])

    assert code == 0 and out["ready"] is True
    assert out["completed_steps"] == ["transition", "mark-feedback-addressed"]
    assert seen == [("architecture", "/wt/9")]
    assert out["pipeline_status"] == "awaiting-human-review"
    assert gh.issues[9]["status"] == "awaiting-human-review"


def test_finish_gate_feedback_leaves_feedback_received_on_an_unverified_push(monkeypatch):
    gh = _standing(stage="product", status="feedback-received")
    monkeypatch.setattr(s, "cmd_transition", lambda *a, **k: {
        "ok": False, "ready": False, "stopped_at": "verify-exit", "reason": "Stage is None"})
    monkeypatch.setattr(s, "cmd_mark_feedback_addressed", _must_not_run("mark-feedback-addressed"))

    result = s.cmd_finish_gate_feedback(gh, 9, "product", "/wt/9")

    assert (result["ok"], result["failed_step"], result["ready"]) == (False, "transition", False)
    assert gh.issues[9]["status"] == "feedback-received"


def test_finish_gate_feedback_passes_a_sync_conflict_through(monkeypatch):
    gh = _standing(stage="product", status="feedback-received")
    monkeypatch.setattr(s, "cmd_transition", lambda *a, **k: {
        "ready": False, "stopped_at": "sync-branch", "reason": "merge conflict on product.md"})

    result = s.cmd_finish_gate_feedback(gh, 9, "product", "/wt/9")

    assert (result["ok"], result["failed_step"]) == (True, "transition")
    assert result["reason"] == "merge conflict on product.md"
    assert result["pipeline_status"] is None and gh.issues[9]["status"] == "feedback-received"


# --- prepare-rework -------------------------------------------------------------

def _pushed_issue_branch(repo, n):
    """`issue-<n>` pushed with one backend commit, its worktree released (as merge/park leave it)."""
    _git("checkout", "-q", "-B", f"issue-{n}", "origin/main", cwd=repo)
    (repo / "backend").mkdir(exist_ok=True)
    (repo / "backend" / "a.ts").write_text("a\n")
    _git("add", "backend/a.ts", cwd=repo)
    _git("commit", "-qm", "work", cwd=repo)
    _git("push", "-q", "origin", f"issue-{n}", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    _git("branch", "-qD", f"issue-{n}", cwd=repo)


def test_prepare_rework_recreates_a_released_worktree_syncs_and_reports_suites(repo):
    gh = Gh([{"number": 10, "labels": ["type:task"], "stage": "pr-review"}],
            prs={42: {"headRefName": "issue-10", "baseRefName": "main",
                      "files": ["backend/a.ts"]}})
    gh.repo = repo
    _pushed_issue_branch(repo, 10)
    _advance_origin(repo, "main", "sibling.md", "landed\n")

    result = s.cmd_prepare_rework(gh, 10, repo_path=str(repo))

    assert result["ok"] is True and result["completed_steps"] == ["worktree-add", "sync-branch"]
    assert result["steps"]["worktree-add"]["resumed"] is True  # re-created from origin
    assert (Path(result["path"]) / "sibling.md").exists()  # main merged in
    _git("fetch", "-q", "origin", cwd=repo)
    assert result["head"] == _git("rev-parse", "origin/issue-10", cwd=repo).strip()
    assert (result["pr"], result["conflict"]) == (42, False)
    [needed] = result["suites"]["needed"]
    assert (needed["suite"], needed["check"]) == ("backend", "missing")


def test_prepare_rework_reuses_a_live_worktree(repo):
    gh = Gh([{"number": 10, "labels": ["type:task"], "stage": "development"}])
    _pushed_issue_branch(repo, 10)
    first = s.cmd_prepare_rework(gh, 10, repo_path=str(repo))

    again = s.cmd_prepare_rework(gh, 10, repo_path=str(repo))

    assert again["steps"]["worktree-add"]["created"] is False
    assert again["path"] == first["path"] and again["pr"] is None and "suites" not in again


def test_prepare_rework_never_syncs_a_diverged_worktree(monkeypatch):
    gh = Gh([{"number": 10, "labels": ["type:task"]}])
    monkeypatch.setattr(s, "cmd_worktree_add",
                        lambda *a, **k: {"path": "/wt/10", "diverged": True, "reason": "diverged"})
    monkeypatch.setattr(s, "cmd_sync_branch", _must_not_run("sync-branch"))

    result = s.cmd_prepare_rework(gh, 10)

    assert (result["ok"], result["failed_step"], result["head"]) == (True, "worktree-add", None)


def test_prepare_rework_passes_a_sync_conflict_through(monkeypatch):
    gh = Gh([{"number": 10, "labels": ["type:task"]}])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/10"})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: {
        "synced": False, "conflict": True, "conflicting_files": ["a.py"]})
    monkeypatch.setattr(s, "git_rev_parse_head", _must_not_run("rev-parse"))

    code, out = _cli(monkeypatch, gh, ["prepare-rework", "10"])

    assert code == 0 and out["failed_step"] == "sync-branch"
    assert (out["conflict"], out["conflicting_files"], out["head"]) == (True, ["a.py"], None)


def test_prepare_rework_treats_a_missing_base_as_nothing_to_sync(monkeypatch):
    gh = Gh([{"number": 10, "labels": ["type:task"]}])
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/10"})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: {"synced": False,
                                                               "base_missing": True})
    monkeypatch.setattr(s, "git_rev_parse_head", lambda path, runner=None: "abc")

    result = s.cmd_prepare_rework(gh, 10)

    assert result["failed_step"] is None and result["head"] == "abc"


# --- next-action --sync-epic -----------------------------------------------------

@pytest.fixture
def runs(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setenv("SDLC_RUNS_DIR", str(d))
    return d


def _epic_tree(child_state="OPEN", epic_labels=("type:epic",)):
    return Gh([{"number": 9, "labels": list(epic_labels)},
               {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development",
                "state": child_state}])


def _cut_epic(repo):
    _git("push", "-q", "origin", "main:epic-9", cwd=repo)


def _epic_has(repo, path):
    _git("fetch", "-q", "origin", cwd=repo)
    return path in _git("ls-tree", "-r", "--name-only", "origin/epic-9", cwd=repo).split()


def test_sync_epic_runs_at_run_start_then_only_when_due(repo, runs):
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "main", "m1.md", "1\n")

    first = s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    quiet = s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    _advance_origin(repo, "main", "m2.md", "2\n")
    moved = s.sync_epic_if_due(gh, 9, "run-1", str(repo))

    assert (first["synced"], first["due"]) == (True, "run-start") and _epic_has(repo, "m1.md")
    assert quiet["due"] is None and quiet["synced"] is False
    assert (moved["synced"], moved["due"]) == (True, "main-moved") and _epic_has(repo, "m2.md")
    assert s.sync_epic_if_due(gh, 9, "run-2", str(repo))["due"] == "run-start"


def test_sync_epic_is_due_again_after_three_sibling_merges(repo, runs):
    gh = _epic_tree()
    _cut_epic(repo)
    s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    state = s.read_run_state(9)
    state["terminal"] = [11, 12, 13]
    s._write_run_state(9, state)

    assert s.sync_epic_if_due(gh, 9, "run-1", str(repo))["due"] == "sibling-merges"


def test_sync_epic_reports_a_conflict_once_then_pending_until_something_moves(repo, runs):
    gh = _epic_tree()
    _cut_epic(repo)
    _advance_origin(repo, "epic-9", "shared.txt", "epic side\n")
    _advance_origin(repo, "main", "shared.txt", "main side\n")

    first = s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    again = s.sync_epic_if_due(gh, 9, "run-1", str(repo))

    assert (first["conflict"], first["conflicting_files"]) == (True, ["shared.txt"])
    assert (again["conflict"], again["pending"]) == (True, True)
    assert sum("sync-conflict: epic-9" in c for c in gh.comments_on(9)) == 1


@pytest.mark.parametrize("gh, run_id, why", [
    (_epic_tree(), None, "no --run-id"),
    (_epic_tree(epic_labels=("type:epic", "epic:standing")), "r", "not an open non-standing"),
    (_epic_tree(child_state="CLOSED"), "r", "close-epic reconciles"),
])
def test_sync_epic_skips_without_touching_git(gh, run_id, why, runs):
    result = s.sync_epic_if_due(gh, 9, run_id, "/nonexistent",
                                runner=_must_not_run("git"))
    assert why in result["skipped"]


def test_sync_epic_reports_an_operational_failure_as_error_and_records_nothing(runs, monkeypatch):
    gh = _epic_tree()
    monkeypatch.setattr(s, "_origin_sha", lambda *a: "abc")
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: (_ for _ in ()).throw(
        s.GhError("push rejected")))

    sync = s.sync_epic_if_due(gh, 9, "run-1", ".", runner=lambda argv: "")

    assert sync == {"synced": False, "error": "push rejected"}
    assert "epic_sync" not in s.read_run_state(9)  # still due on the next call


def test_next_action_carries_epic_sync_only_with_the_flag(runs, monkeypatch):
    gh = _epic_tree()
    monkeypatch.setattr(s, "decide_next_action", lambda *a, **k: {"action": "none", "epic": 9})
    monkeypatch.setattr(s, "sync_epic_if_due", lambda *a, **k: {"synced": True, "due": "run-start"})

    code, out = _cli(monkeypatch, gh, ["next-action", "9", "--run-id", "r", "--sync-epic"])
    assert code == 0 and out["epic_sync"] == {"synced": True, "due": "run-start"}

    code, out = _cli(monkeypatch, gh, ["next-action", "9", "--run-id", "r"])
    assert code == 0 and "epic_sync" not in out


# --- close-epic --clean-worktree ------------------------------------------------

HEAD = "a" * 40
_EXPLORED = f"<!-- epic-verification: exploratory:9 sha:{'b' * 40} @ 2026-09-15T00:01:00Z -->"


def _closeable(repo, comments=(_EXPLORED,)):
    """Epic #9 whose only child is closed, verified, with a live dirty-able epic worktree."""
    gh = Gh([{"number": 9, "labels": ["type:epic"], "comments": list(comments)},
             {"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED"}])
    gh.branch_behind_by = lambda branch, base="main": 0
    gh.branch_head_sha = lambda branch: HEAD
    gh.files_since = lambda sha, branch: []
    gh.base_delta_files = lambda head, base="main": ["docs/x.md"]
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": HEAD, "state": "OPEN"}
    gh.pr_files = lambda n: ["docs/x.md"]
    gh.pr_checks = lambda n: []
    gh.pr_ready = lambda n: None
    gh.branch_commits = lambda base, head: []
    gh.commit_files = lambda sha: []
    wt = Path(s.cmd_worktree_add(gh, 9, unit="epic", repo_path=str(repo))["path"])
    return gh, wt


def test_close_epic_clean_worktree_discards_untracked_output_then_merges(repo):
    gh, wt = _closeable(repo)
    (wt / "uploads").mkdir()
    (wt / "uploads" / "shot.png").write_text("x")
    (wt / "evidence.json").write_text("{}")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), clean_worktree=True)

    assert result["merged"] is True and len(gh.merges) == 1
    assert sorted(result["worktree_clean"]["removed"]) == ["evidence.json", "uploads/"]
    assert result["worktree"]["released"] is True and not wt.exists()


def test_close_epic_clean_worktree_refuses_tracked_changes_and_never_merges(repo):
    gh, wt = _closeable(repo)
    (wt / "README.md").write_text("hand edit\n")
    (wt / "evidence.json").write_text("{}")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), clean_worktree=True)

    assert result["merged"] is False and gh.merges == []
    assert result["worktree_clean"]["tracked_changes"] == ["README.md"]
    assert "tracked change" in result["reason"]
    assert (wt / "evidence.json").exists() and (wt / "README.md").read_text() == "hand edit\n"


def test_close_epic_clean_worktree_keeps_the_output_when_the_call_is_refused(repo):
    gh, wt = _closeable(repo, comments=())
    (wt / "evidence.json").write_text("{}")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo), clean_worktree=True)

    assert result["merged"] is False and "missing_verification" in result
    assert "worktree_clean" not in result and (wt / "evidence.json").exists()


def test_close_epic_without_the_flag_leaves_the_worktree_as_it_was(repo):
    gh, wt = _closeable(repo)
    (wt / "evidence.json").write_text("{}")

    result = s.cmd_close_epic(gh, 9, repo_path=str(repo))

    assert result["merged"] is True and "worktree_clean" not in result
    assert result["worktree"]["released"] is False and (wt / "evidence.json").exists()
