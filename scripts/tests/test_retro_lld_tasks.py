"""Retro regressions around the LLD carving contract: prose under a `## Task` heading,
pre-existing issues a carved Task realises, and footprint sections with lead-in lines."""
import pytest

import sdlc_next as s
from tests.test_composites import _lld_ready, _lld_tree
from tests.test_v2_phase_tasks import (DOC, FakeGh, _CARVED_LLD, _origin_file,  # noqa: F401
                                       _push_doc_branch, _v2_tree, repo)


# --- Finding 1: a `## Task` heading over prose is not a Task ---------------------------

_CARVING_TABLE = (
    "## Task carving: summary\n"
    "| Task | Realises |\n|---|---|\n| skeleton-health | #574 |\n\n")


def test_create_lld_tasks_skips_a_task_heading_with_no_footprint_and_reports_it(repo):
    """Regression: a carving-summary section headed `## Task carving: summary` became a
    Task issue titled "summary". Positive control: the real sections are still created."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVING_TABLE + _CARVED_LLD)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["skeleton-health", "greet-endpoint"]
    assert "summary" not in {i["title"] for i in gh.issues.values()}
    [skipped] = result["skipped_sections"]
    assert skipped["key"] == "carving" and skipped["heading"] == "## Task carving: summary"
    assert "no `## Footprint` heading" in skipped["reason"]
    # The prose heading is left as it was, not numbered against a phantom issue.
    published = _origin_file(repo, "epic-9", f"{DOC}/epic-9/lld.md")
    assert "## Task carving: summary" in published and result["pushed"] is True


def test_task_section_defect_accepts_a_verify_only_footprint():
    """A standing Task that only runs suites still qualifies: the heading is what counts."""
    assert s.task_section_defect("prose\n\n## Footprint\n**Verify-only:**\n- `test/**`\n") is None
    assert s.task_section_defect("prose with no footprint\n") is not None


# --- Finding 2: `Realises: #<n>` ------------------------------------------------------

_REALISING_LLD = (
    "# lld for epic 9\n\n"
    "## Task skeleton-health: Add /health endpoint\n"
    "Realises: #574, #577\n\n"
    "## Footprint\n- `src/health.js`\n\n"
    "## Task greet-endpoint: Add /greet endpoint\n"
    "- **Realizes:** #999\n\n"
    "## Footprint\n- `src/greet.js`\n")


def _realising_tree(*extra):
    return _v2_tree({"number": 574, "labels": ["type:task"], "parent": 9},
                    {"number": 577, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
                    *extra)


def test_parse_task_realises_reads_hash_references_in_either_spelling():
    assert s.parse_task_realises("Realises: #574, #577 and #574\n") == [574, 577]
    assert s.parse_task_realises("- **Realizes:** `#12`.\n") == [12]
    assert s.parse_task_realises("Realises nothing here 12\n") == []


def test_create_lld_tasks_blocks_a_realised_issue_on_the_task_and_persists_the_relation(repo):
    """Regression: pre-filed children named by the LLD were delegated alongside the new
    Tasks that realise them. Now the old issue is blocked on the new Task and the Task's
    body records what it realises for the close path."""
    gh = _realising_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _REALISING_LLD)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    health, greet = result["tasks"]["skeleton-health"], result["tasks"]["greet-endpoint"]
    assert gh.blocked_by(574) == [health]
    assert s.realised_issues(gh.issues[health]["body"]) == [574, 577]
    assert result["realises"] == [{"issue": health, "realises": 574, "blocked": True},
                                  {"issue": health, "realises": 577, "blocked": False,
                                   "reason": "already closed"}]
    [failed] = result["realises_failed"]
    assert failed == {"issue": greet, "realises": 999,
                      "error": failed["error"]} and "not a pre-existing issue" in failed["error"]
    # Positive control: a Task that realises nothing carries no marker.
    assert s.realised_issues(gh.issues[greet]["body"]) == []


def test_create_lld_tasks_rerun_does_not_re_add_the_realises_edge(repo):
    gh = _realising_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _REALISING_LLD)
    s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    gh.issues[574]["body"] = ""  # nothing else changes; the numbered doc short-circuits

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert again["created"] == [] and again["realises"] == []
    assert len(gh.blocked.get(574, [])) == 1


def test_merge_lld_doc_never_advances_a_realised_or_uncarved_issue(repo):
    """Regression: merge-lld-doc advanced every Stage-less Task child, including ones the
    doc realises through a new Task (#574) and ones it never carved (#628, no footprint)."""
    gh = _realising_tree({"number": 628, "labels": ["type:task"], "parent": 9},
                         {"number": 980, "labels": ["type:task"], "parent": 9,
                          "body": "x\n\n<!-- task-key: skeleton-health -->"},
                         {"number": 981, "labels": ["type:task"], "parent": 9})
    numbered = s.number_task_headings(_REALISING_LLD, {"skeleton-health": 980,
                                                       "greet-endpoint": 981})
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", numbered)

    result = s.cmd_merge_lld_doc(gh, str(repo), 9)

    assert result["merged"] is True and result["advanced_tasks"] == [980, 981]
    held = {h["issue"]: h["reason"] for h in result["not_advanced"]}
    assert set(held) == {574, 628}
    assert "realised by Task #980" in held[574]
    assert "no `## Task #628` subsection with a `## Footprint`" in held[628]
    assert gh.issues[574]["stage"] is None and gh.issues[628]["stage"] is None
    assert gh.issues[980]["stage"] == gh.issues[981]["stage"] == "development"
    [phase_comment] = [c for c in gh.comments_on(9) if "design phase" in c]
    assert "#574 not advanced" in phase_comment and "#628 not advanced" in phase_comment


def test_merge_lld_doc_matches_an_unnumbered_section_by_the_tasks_key_marker(repo):
    """A Task created before the doc was renumbered (a crash) still counts as carved."""
    gh = _v2_tree({"number": 980, "labels": ["type:task"], "parent": 9,
                   "body": "x\n\n<!-- task-key: skeleton-health -->"})
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CARVED_LLD)

    result = s.cmd_merge_lld_doc(gh, str(repo), 9)

    assert result["advanced_tasks"] == [980] and result["not_advanced"] == []


def test_finish_lld_leaves_a_realised_child_blocked_and_unstaged(repo):
    gh = _lld_tree()
    gh.issues[574] = {**gh.issues[11], "title": "issue 574", "stage": None, "status": None,
                      "labels": ["type:task"], "comments": [], "body": ""}
    _lld_ready(gh, repo, text=_REALISING_LLD.replace("- **Realizes:** #999\n", ""))

    result = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert result["ok"] is True
    health = result["steps"]["create-lld-tasks"]["tasks"]["skeleton-health"]
    assert gh.issues[health]["stage"] == "development"
    assert gh.issues[574]["stage"] is None and gh.blocked_by(574) == [health]
    assert [h["issue"] for h in result["steps"]["merge-lld-doc"]["not_advanced"]] == [574]


def _merging_tree(body: str):
    gh = _v2_tree({"number": 574, "labels": ["type:task"], "parent": 9, "stage": "development"},
                  {"number": 577, "labels": ["type:task"], "parent": 9, "state": "CLOSED"},
                  {"number": 980, "labels": ["type:task"], "parent": 9, "stage": "development",
                   "body": body, "comments": [
                       "<!-- stage-transition: development->pr-review @ 2026-09-16T00:00:00Z -->",
                       "<!-- pr-review-outcome: clean:42 @ 2026-09-16T01:00:00Z -->"]})
    gh._run = s._default_runner
    gh.branch_behind_by = lambda head, base="main": 0
    gh.pr_checks = lambda n: []
    gh.pr_files = lambda n: []
    gh.pr_view = lambda n, fields="": {"comments": [], "headRefOid": "abc"}
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n, **kw: None
    gh.pr_comment = lambda n, body: None
    return gh


def test_merge_pr_closes_the_issues_the_task_realises(repo):
    """Regression: `Closes #n` never fires on an `epic-<n>` merge, so realised issues
    stayed open. The control plane closes them, naming the Task and PR."""
    gh = _merging_tree("design\n\n<!-- task-key: skeleton-health -->\n<!-- realises: #574 #577 -->")

    result = s.cmd_merge_pr(gh, 42, issue=980, repo_path=str(repo))

    assert result["issue_closed"] is True
    assert result["realised_closed"] == [{"issue": 574, "closed": True},
                                         {"issue": 577, "closed": False,
                                          "reason": "already closed"}]
    assert gh.issues[574]["state"] == "CLOSED" and gh.issues[574]["status"] == "done"
    [note] = gh.comments_on(574)
    assert "Realised by Task #980 (PR #42)" in note and "<!-- realised-by: 980 pr:42" in note
    assert gh.comments_on(577) == []


def test_merge_pr_of_a_task_that_realises_nothing_closes_nothing_else(repo):
    gh = _merging_tree("design\n\n<!-- task-key: skeleton-health -->")

    result = s.cmd_merge_pr(gh, 42, issue=980, repo_path=str(repo))

    assert result["issue_closed"] is True and "realised_closed" not in result
    assert gh.issues[574]["state"] == "OPEN"


def test_close_issue_also_closes_the_issues_the_unit_realises(repo):
    gh = _v2_tree({"number": 574, "labels": ["type:task"], "parent": 9},
                  {"number": 11, "labels": ["type:task"], "parent": 9, "stage": "lld",
                   "body": "<!-- realises: #574 -->"})

    result = s.cmd_close_issue(gh, 11, repo_path=str(repo))

    assert result["closed"] is True and result["realised_closed"] == [{"issue": 574, "closed": True}]
    assert gh.issues[574]["state"] == "CLOSED"


# --- Finding 3: footprint sections with lead-in lines, and verify-only Tasks ----------

def test_find_footprint_tells_a_verify_only_section_from_a_missing_one():
    """Regression: a section opening with `**Verify-only:**` read as "no ## Footprint"."""
    verify_only = "## Footprint\n**Verify-only:**\n- `test/e2e/**`\n"
    assert s.find_footprint(verify_only) == []
    assert s.find_footprint("# Doc\n\nNo section here.\n") is None
    assert s.parse_footprint(verify_only) == []


@pytest.mark.parametrize("lead_in", [
    "**Edits:**\n", "Paths this Task owns:\n\n", "\n\n", "**Owned**\nsome prose\n"])
def test_find_footprint_tolerates_lead_in_lines_before_the_bullets(lead_in):
    doc = f"## Footprint\n{lead_in}- `src/a.ts`\n- `src/b.ts`\n**Verify-only:**\n- `test/**`\n"
    assert s.find_footprint(doc) == ["src/a.ts", "src/b.ts"]


def test_find_footprint_ignores_a_backticked_mention_and_a_fenced_example():
    """Regression: prose mentioning the heading, or quoting its shape in a code fence,
    was taken for the section (and its prose consumed as the body)."""
    mention = "- `## Footprint` heading required\n- `src/decoy.ts`\n\nMore prose.\n"
    fenced = "Shape:\n\n```markdown\n## Footprint\n\n- `src/decoy.ts`\n```\n"
    assert s.find_footprint(mention) is None
    assert s.find_footprint(fenced) is None
    # Positive control: the real heading after the fence still parses.
    assert s.find_footprint(fenced + "\n## Footprint\n- `src/real.ts`\n") == ["src/real.ts"]


def test_find_footprint_heading_tolerates_trailing_whitespace():
    assert s.find_footprint("## Footprint   \n- `a/b.ts`\n") == ["a/b.ts"]


def test_list_parallel_ready_schedules_a_verify_only_task(repo):
    """Regression: standing Tasks whose footprint is all `**Verify-only:**` were skipped as
    "no ## Footprint section". They own nothing, so they collide with nothing."""
    gh = FakeGh([
        {"number": 6, "labels": ["type:initiative"]},
        {"number": 9, "labels": ["type:epic", s.LABELS["architected"]], "parent": 6},
        {"number": 13, "labels": ["type:task"], "parent": 9, "stage": "development"},
        {"number": 14, "labels": ["type:task"], "parent": 9, "stage": "development"},
        {"number": 15, "labels": ["type:task"], "parent": 9, "stage": "development"},
    ])
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md",
                     "# lld\n\n## Task #13: build\n\n## Footprint\n- `src/a.js`\n\n"
                     "## Task #14: e2e-test\n\n## Footprint\n**Verify-only:**\n- `src/a.js`\n\n"
                     "## Task #15: uncarved\n\nno footprint here\n")

    result = s.cmd_list_parallel_ready(gh, str(repo), 9, limit=5)

    assert [u["issue"] for u in result["parallel_ready"]] == [13, 14]
    [skip] = result["skipped"]
    assert skip["issue"] == 15 and "no ## Footprint" in skip["reason"]
