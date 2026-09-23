"""A non-standing Epic's architecture/lld phase-Task authors its doc at `epic-<e>/` and raises
a design PR `issue-<n>` -> `epic-<e>`: open, review comment, merge (pipeline or human),
and the old `main`-based gates that Product-Roadmap Tasks and standing children keep."""
import io
import json
from contextlib import redirect_stdout

import pytest

import sdlc_next as s
from tests.test_v2_phase_tasks import (DOC, FakeGh, _CARVED_LLD, _design_branch,  # noqa: F401
                                       _ensure_epic_branch, _git, _open_design, _origin_file,
                                       _push_doc_branch, _review, _v2_tree, repo)

ARCH = {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture",
        "status": "in-progress"}
LLD = {"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
       "status": "in-progress"}


def _arch(repo, **kw):
    gh = _v2_tree(dict(ARCH))
    return gh, _open_design(gh, repo, 10, "architecture.md", "# arch\n", **kw)


# --- open-design-pr -----------------------------------------------------------

def test_open_design_pr_raises_a_marked_epic_bound_pr_without_closes(repo):
    gh = _v2_tree(dict(ARCH))
    _design_branch(repo, 10, "architecture.md", "# arch\n")

    result = s.cmd_open_design_pr(gh, 10)

    pr = gh.prs[result["pr"]]
    assert result["created"] is True and result["base"] == "epic-9"
    assert (pr["headRefName"], pr["baseRefName"], pr["isDraft"]) == ("issue-10", "epic-9", False)
    assert "<!-- design-pr: architecture:10 -->" in pr["body"]
    assert s._CLOSES_ISSUE_RE.search(pr["body"]) is None
    assert f"<!-- design-pr-opened: architecture:{result['pr']} @ " in gh.comments_on(10)[-1]


def test_open_design_pr_is_idempotent_and_replaces_a_pr_closed_unmerged(repo):
    gh = _v2_tree(dict(ARCH))
    _design_branch(repo, 10, "architecture.md", "# arch\n")
    first = s.cmd_open_design_pr(gh, 10)

    again = s.cmd_open_design_pr(gh, 10)
    assert again["created"] is False and again["pr"] == first["pr"] and len(gh.prs) == 1

    gh.prs[first["pr"]]["state"] = "CLOSED"
    fresh = s.cmd_open_design_pr(gh, 10)
    assert fresh["created"] is True and fresh["pr"] != first["pr"]


@pytest.mark.parametrize("tree,issue", [
    ([{"number": 6, "labels": ["type:initiative"]},
      {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product"}], 7),
    ([{"number": 9, "labels": ["type:epic", "epic:standing"]},
      {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"}], 10),
    ([{"number": 10, "labels": ["type:task"], "stage": "architecture"}], 10),
    ([{"number": 9, "labels": ["type:epic"]},
      {"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"}], 10),
])
def test_open_design_pr_refuses_everything_but_an_epics_design_phase_task(tree, issue):
    gh = FakeGh(tree)

    result = s.cmd_open_design_pr(gh, issue)

    assert result["refused"] is True and "no design PR" in result["reason"]
    assert gh.prs == {}


# --- a design PR is not a gate, a dev PR or a merge-pr target -------------------

def _pr(issue=10, base="epic-9", body="", head=None):
    return {"number": 40, "headRefName": head or f"issue-{issue}", "baseRefName": base,
            "state": "MERGED", "mergedAt": "2026-09-19T00:00:00Z", "body": body}


def _gate_ready_tree(status="awaiting-human-review"):
    return _v2_tree({**ARCH, "status": status, "comments": ["<!-- gate-pr: architecture:40 -->"]})


def test_a_marked_design_pr_into_the_epic_branch_is_an_open_gate():
    gh = _gate_ready_tree()
    body = "Design PR.\n\n<!-- design-pr: architecture:10 -->"

    assert s._match_open_gate(gh, _pr(body=body), 40)[:2] == (10, "architecture")


@pytest.mark.parametrize("pr,why", [
    (_pr(body="Work.\n\nCloses #10"), "epic-9 dev PR: no design marker"),
    (_pr(body="<!-- design-pr: architecture:11 -->"), "marker names another issue"),
    (_pr(base="release", body="<!-- design-pr: architecture:10 -->"), "not an epic branch"),
])
def test_only_a_design_marked_pr_into_an_epic_branch_counts_as_a_gate(pr, why):
    with pytest.raises(s._NotAGate):
        s._match_open_gate(_gate_ready_tree(), pr, 40)


def test_a_design_pr_is_not_a_gate_while_its_review_is_still_running():
    gh = _gate_ready_tree(status="in-progress")
    with pytest.raises(s._NotAGate, match="not currently awaiting-human-review"):
        s._match_open_gate(gh, _pr(body="<!-- design-pr: architecture:10 -->"), 40)


def test_a_main_gate_pr_is_still_recognised_as_before():
    gh = _gate_ready_tree()
    assert s._match_open_gate(gh, _pr(base="main", body="Doc-only gate."), 40)[0] == 10


@pytest.mark.parametrize("stage,expect_refused", [("architecture", True), ("lld", True),
                                                   ("development", False)])
def test_merge_pr_refuses_a_phase_task_but_not_a_functional_task(stage, expect_refused,
                                                                   monkeypatch):
    gh = _v2_tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": stage})
    gh.prs[40] = {"state": "MERGED", "headRefName": "issue-10", "baseRefName": "epic-9"}
    gh.pr_files = lambda n: []
    monkeypatch.setattr(s, "_finalize_merged_pr", lambda *a, **k: {"finalized": True})

    result = s.cmd_merge_pr(gh, 40, 10)

    if expect_refused:
        assert result["merged"] is False and "merge-design-pr" in result["reason"]
        assert gh.issues[10]["state"] == "OPEN"
    else:
        assert result == {"finalized": True}


# --- the review is on the PR too ----------------------------------------------

def test_record_design_review_also_comments_on_the_design_pr(repo):
    gh, pr = _arch(repo)

    result = s.cmd_record_design_review(gh, 10, "arch-review", "clean", "structure holds")

    assert result["design_pr"] == pr
    [comment] = gh.prs[pr]["comments"]
    assert "structure holds" in comment and "no findings" in comment
    assert "design-review-outcome" not in comment  # the marker is the issue thread's alone
    head = _git("rev-parse", "origin/issue-10", cwd=repo).strip()
    assert f"<!-- design-review-outcome: clean:arch-review sha:{head} @ " in gh.comments_on(10)[-1]
    assert result["reviewed_sha"] == head


def test_record_design_review_leaves_a_pr_comment_only_where_a_design_pr_exists(repo):
    gh, pr = _arch(repo)

    # The wrong role for the PR's stage, a product review, and an issue with no PR.
    s.cmd_record_design_review(gh, 10, "lld-review", "clean", "x")
    s.cmd_record_design_review(gh, 10, "product-review", "clean", "x")
    bare = _v2_tree(dict(ARCH))
    result = s.cmd_record_design_review(bare, 10, "arch-review", "rework", "gap")

    assert gh.prs[pr]["comments"] == [] and bare.prs == {}
    assert "design_pr" not in result and result["recorded"] is True


# --- merge-design-pr ----------------------------------------------------------

def test_lld_review_clean_merges_the_design_pr_and_keeps_the_task_and_branch(repo):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is True and result["stage"] == "lld" and result["base"] == "epic-9"
    assert gh.merges == [(pr, False)]
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md") == _CARVED_LLD
    assert gh.issues[11]["state"] == "OPEN"
    assert f"<!-- design-pr-merged: lld:{pr} @ " in gh.comments_on(11)[-1]
    assert _git("ls-remote", "--heads", "origin", "issue-11", cwd=repo).strip()  # branch kept


@pytest.mark.parametrize("confidence,merges", [(81, True), (99, True), (80, False), (None, False)])
def test_architecture_merges_without_a_human_only_above_the_skip_threshold(
        repo, confidence, merges):
    gh, pr = _arch(repo)
    _review(gh, 10, "arch-review", confidence=confidence)

    result = s.cmd_merge_design_pr(gh, pr, 10, str(repo))

    assert result["merged"] is merges
    assert bool(gh.merges) is merges
    if not merges:
        assert "open-gate" in result["reason"] and result["missing_evidence"]


def test_a_stale_confidence_from_an_earlier_round_does_not_clear_this_round(repo):
    gh, pr = _arch(repo)
    _review(gh, 10, "arch-review", "rework", confidence=99)
    _review(gh, 10, "arch-review", "clean")  # this round reported no confidence

    assert s.cmd_merge_design_pr(gh, pr, 10, str(repo))["merged"] is False


def test_architecture_merges_when_the_profile_waives_gate_b(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "profiles", [
        {"name": "quiet", "match": {"label": "epic:quiet"},
         "gates": {"requiresHumanGateB": False}}, *s.PIPELINE["profiles"]])
    gh = FakeGh([{"number": 9, "labels": ["type:epic", "epic:quiet"]}, dict(ARCH)])
    pr = _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    _review(gh, 10, "arch-review")

    assert s.cmd_merge_design_pr(gh, pr, 10, str(repo))["merged"] is True


@pytest.mark.parametrize("outcome,needle", [(None, "no `lld-review` outcome"),
                                            ("rework", "not `clean`")])
def test_a_design_pr_merges_only_on_a_recorded_clean_review(repo, outcome, needle):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    if outcome:
        _review(gh, 11, "lld-review", outcome)

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is False and needle in result["reason"]
    assert gh.merges == [] and gh.prs[pr]["state"] == "OPEN"


def test_a_design_pr_behind_its_epic_branch_is_not_merged_until_synced(repo):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")
    gh.behind["issue-11"] = 3

    behind = s.cmd_merge_design_pr(gh, pr, 11, str(repo))
    gh.behind["issue-11"] = 0
    synced = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert behind["merged"] is False and behind["behind_base"] == 3
    assert "sync-branch 11" in behind["reason"]
    assert synced["merged"] is True and len(gh.merges) == 1


@pytest.mark.parametrize("mutate", [
    lambda pr: pr.update(headRefName="issue-12"),
    lambda pr: pr.update(baseRefName="main"),
    lambda pr: pr.update(body="no marker"),
    lambda pr: pr.update(body="<!-- design-pr: lld:12 -->"),
])
def test_merge_design_pr_refuses_a_pr_that_is_not_this_tasks_design_pr(repo, mutate):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")
    mutate(gh.prs[pr])

    result = s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    assert result["merged"] is False and "not #11's design PR" in result["reason"]
    assert gh.merges == []


def test_merge_design_pr_is_idempotent_and_never_touches_a_closed_pr(repo):
    gh = _v2_tree(dict(LLD))
    pr = _open_design(gh, repo, 11, "lld.md", _CARVED_LLD)
    _review(gh, 11, "lld-review")
    s.cmd_merge_design_pr(gh, pr, 11, str(repo))

    again = s.cmd_merge_design_pr(gh, pr, 11, str(repo))
    assert again["already_merged"] is True and again["merged"] is False and len(gh.merges) == 1

    gh.prs[pr]["state"] = "CLOSED"
    assert "closed without merging" in s.cmd_merge_design_pr(gh, pr, 11, str(repo))["reason"]


def test_merge_design_pr_refuses_an_issue_that_is_not_an_epics_phase_task():
    gh = FakeGh([{"number": 10, "labels": ["type:task"], "stage": "architecture"}])
    result = s.cmd_merge_design_pr(gh, 40, 10)
    assert result["merged"] is False and "not a non-standing Epic's phase-Task" in result["reason"]


# --- gates: the human path reuses the design PR --------------------------------

def test_open_gate_on_an_epic_architecture_task_gates_the_existing_design_pr(repo):
    gh, pr = _arch(repo)

    result = s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md",
                             "development", "Design is settled.")

    assert len(gh.prs) == 1 and result["gate_pr"] == pr and result["base"] == "epic-9"
    assert gh.issues[10]["status"] == "awaiting-human-review"
    comment = gh.comments_on(10)[-1]
    assert f"<!-- gate-pr: architecture:{pr} -->" in comment
    assert f"`{DOC}/epic-9/architecture.md`" in comment
    # Reviewer comments made before the gate opened are not human feedback.
    assert "<!-- gate-comments-processed: " in gh.prs[pr]["comments"][-1]
    assert gh.prs[pr]["labels"] == ["sdlc:gate"]


def test_open_gate_refuses_when_the_design_pr_was_never_opened(repo):
    gh = _v2_tree(dict(ARCH))
    _design_branch(repo, 10, "architecture.md", "# arch\n")
    with pytest.raises(s.GhError, match="no architecture design PR to gate"):
        s.cmd_open_gate(gh, str(repo), 10, "t", "architecture.md", "development", "x")


def test_open_gate_still_opens_a_main_pr_for_a_roadmap_task_and_a_standing_child(repo):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product"},
                 {"number": 90, "labels": ["type:epic", "epic:standing"]},
                 {"number": 91, "labels": ["type:task"], "parent": 90, "stage": "architecture"}])
    _push_doc_branch(repo, "issue-7", f"{DOC}/issue-7/product.md", "# prd\n")
    _push_doc_branch(repo, "issue-91", f"{DOC}/issue-91/architecture.md", "# arch\n")

    roadmap = s.cmd_open_gate(gh, str(repo), 7, "Product Roadmap", "product.md",
                              "architecture", "x")
    standing = s.cmd_open_gate(gh, str(repo), 91, "Fix it", "architecture.md", "development", "x")

    assert (roadmap["base"], standing["base"]) == ("main", "main")
    assert {gh.prs[roadmap["gate_pr"]]["baseRefName"],
            gh.prs[standing["gate_pr"]]["baseRefName"]} == {"main"}
    assert f"{DOC}/issue-91/architecture.md" in gh.comments_on(91)[-1]


def test_a_human_merge_of_the_design_pr_closes_the_phase_task_through_auto_pass_gate(repo):
    gh, pr = _arch(repo)
    s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md", "development", "x")
    gh.pr_merge(pr, delete_branch=False)

    result = s.cmd_auto_pass_gate(gh, str(repo), pr)

    assert result["ok"] is True and result["phase_task_complete"] is True
    assert gh.issues[10]["state"] == "CLOSED" and gh.issues[10]["status"] == "done"
    assert _origin_file(repo, "epic-9", f"{DOC}/epic-9/architecture.md") == "# arch\n"
    # pass-gate reconciled the issue branch with its epic branch, not main (a merge commit
    # past the merged PR head); the close still deleted the branch on origin and locally.
    epic_tip = _git("rev-parse", "origin/epic-9", cwd=repo).strip()
    deleted_head = result["cleanup"]["remote_branch"]["head"]
    assert _git("merge-base", "--is-ancestor", epic_tip, deleted_head, cwd=repo) == ""
    assert gh.deleted_branches == ["issue-10"]
    assert not _git("ls-remote", "--heads", "origin", "issue-10", cwd=repo).strip()
    assert not _git("branch", "--list", "issue-10", cwd=repo).strip()


def test_a_design_pr_closed_unmerged_by_the_human_parks_the_task_needs_human(repo):
    gh, pr = _arch(repo)
    s.cmd_open_gate(gh, str(repo), 10, "Architecture phase", "architecture.md", "development", "x")
    gh.prs[pr]["state"] = "CLOSED"

    result = s.cmd_auto_pass_gate(gh, str(repo), pr)

    assert result["status"] == "needs-human" and gh.issues[10]["state"] == "OPEN"


def test_auto_pass_gate_ignores_the_design_pr_the_pipeline_itself_merges(repo):
    gh, pr = _arch(repo)
    _review(gh, 10, "arch-review", confidence=99)
    s.cmd_merge_design_pr(gh, pr, 10, str(repo))  # status is still in-progress: no open gate

    result = s.cmd_auto_pass_gate(gh, str(repo), pr)

    assert result["ok"] is True and "not currently awaiting-human-review" in result["skipped"]
    assert gh.issues[10]["state"] == "OPEN"


def test_waive_gate_merges_the_design_pr_then_closes_an_epic_architecture_task(repo, monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "profiles", [
        {"name": "quiet", "match": {"label": "epic:quiet"},
         "gates": {"requiresHumanGateB": False}}, *s.PIPELINE["profiles"]])
    gh = FakeGh([{"number": 9, "labels": ["type:epic", "epic:quiet"]}, dict(ARCH)])
    pr = _open_design(gh, repo, 10, "architecture.md", "# arch\n")
    _review(gh, 10, "arch-review")

    result = s.cmd_waive_gate(gh, 10, "architecture", "Clean.", repo_path=str(repo))

    assert result["phase_task_complete"] is True and gh.merges == [(pr, False)]
    assert gh.issues[10]["state"] == "CLOSED"


def test_skip_gate_leaves_the_task_open_when_the_design_pr_cannot_merge(repo):
    gh, pr = _arch(repo)
    _review(gh, 10, "arch-review", confidence=99)
    gh.behind["issue-10"] = 2

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=str(repo))

    assert result["phase_task_complete"] is False
    assert result["design_pr"]["behind_base"] == 2 and "sync-branch 10" in result["reason"]
    assert gh.issues[10]["state"] == "OPEN" and gh.merges == []


def test_skip_gate_still_needs_a_recorded_clean_review_before_it_merges(repo):
    gh, pr = _arch(repo)  # confidence is claimed, but no review outcome was ever recorded

    result = s.cmd_skip_gate(gh, 10, "architecture", 99, "clean", repo_path=str(repo))

    assert result["phase_task_complete"] is False and gh.merges == []
    assert "no `arch-review` outcome" in result["reason"]


def test_a_roadmap_task_gate_still_reconciles_with_main_and_closes(repo):
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 7, "labels": ["type:task"], "parent": 6, "stage": "product",
                  "status": "awaiting-human-review", "comments": ["<!-- gate-pr: product:8 -->"]}])
    _push_doc_branch(repo, "issue-7", f"{DOC}/issue-7/product.md", "# prd\n", merge_to_main=True)
    _advance_main = _git("rev-parse", "origin/main", cwd=repo).strip()

    result = s.cmd_pass_gate(gh, str(repo), 7, 8, "product")

    assert result["phase_task_complete"] is True
    assert _git("merge-base", "--is-ancestor", _advance_main, "origin/issue-7", cwd=repo) == ""


# --- pause-for-epic-regate: the revision's PR does not exist yet ----------------

def test_pause_for_epic_regate_parks_a_unit_before_the_revision_pr_exists():
    gh = _v2_tree({"number": 12, "labels": ["type:task"], "parent": 9, "stage": "development"})

    result = s.cmd_pause_for_epic_regate(gh, 12, 9, found_by="development")

    assert result["gate_pr"] is None and gh.issues[12]["status"] == "todo"
    assert "once the revision's architecture stage finishes" in gh.comments_on(12)[-1]


# --- CLI ------------------------------------------------------------------------

def _cli(monkeypatch, gh, argv):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    out = io.StringIO()
    with redirect_stdout(out):
        code = s.main(argv)
    return code, json.loads(out.getvalue())


def test_cli_wires_open_and_merge_design_pr_and_publish_doc_is_gone(monkeypatch, repo):
    gh = _v2_tree(dict(LLD))
    _design_branch(repo, 11, "lld.md", _CARVED_LLD)
    gh.repo = repo

    code, opened = _cli(monkeypatch, gh, ["open-design-pr", "11"])
    _review(gh, 11, "lld-review")
    code2, merged = _cli(monkeypatch, gh, ["merge-design-pr", str(opened["pr"]), "--issue", "11",
                                           "--repo-path", str(repo)])

    assert (code, opened["created"]) == (0, True)
    assert (code2, merged["merged"]) == (0, True)
    with pytest.raises(SystemExit):
        s.main(["publish-doc", "11", "--doc", "lld.md"])
