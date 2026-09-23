"""Retro 2026-09-23 control-plane regressions: backtick-wrapped Task field lines, derived
gate-PR titles, docs-only reconciles vs epic evidence, the sub-issue cap, run-wide terminal
counts, and one glob matcher for check coverage."""
import pytest

import sdlc_next as s
from tests.test_composites import _lld_ready, _lld_tree
from tests.test_v2_phase_tasks import (DOC, _origin_file, _push_doc_branch,  # noqa: F401
                                       _v2_tree, repo)


# --- Fix 1: backtick-wrapped Task field lines -----------------------------------------

@pytest.mark.parametrize("line,keys", [
    ("`Depends on: copy-core`", ["copy-core"]),
    ("`Depends on`: copy-core, paste-core", ["copy-core", "paste-core"]),
    ("- **Depends on:** copy-core and paste-core", ["copy-core", "paste-core"]),
    ("Depends on: `copy-core`, `paste-core`.", ["copy-core", "paste-core"]),
])
def test_parse_task_depends_on_tolerates_a_backtick_wrapper(line, keys):
    assert s.parse_task_depends_on(line + "\n") == keys


def test_parse_task_depends_on_plain_line_still_parses():
    """Positive control: the plain shape is unchanged."""
    assert s.parse_task_depends_on("Depends on: skeleton-health, greet\n") == [
        "skeleton-health", "greet"]


def test_parse_task_depends_on_reads_none_as_no_dependency():
    assert s.parse_task_depends_on("Depends on: none\n") == []


@pytest.mark.parametrize("line", ["`Priority: High` / `Effort: Medium`",
                                  "Priority: High / Effort: Medium",
                                  "**Priority:** High | **Effort:** Medium"])
def test_priority_and_effort_parse_from_one_shared_line(line):
    assert s.parse_task_field(line, s._PRIORITY_LINE) == "High"
    assert s.parse_task_field(line, s._EFFORT_LINE) == "Medium"


_WRAPPED_LLD = (
    "# lld for epic 9\n\n"
    "## Task copy-core: Copy core\n"
    "Design.\n\n"
    "## Footprint\n- `src/copy.js`\n\n"
    "## Task copy-ui: Copy UI\n"
    "`Depends on: copy-core`\n"
    "`Priority: High` / `Effort: Low`\n\n"
    "## Footprint\n- `src/ui.js`\n")


def test_create_lld_tasks_wires_a_backtick_wrapped_depends_on(repo):
    """Regression: a backtick-wrapped `Depends on:` line created the Task with no edge."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _WRAPPED_LLD)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    core, ui = result["tasks"]["copy-core"], result["tasks"]["copy-ui"]
    assert gh.blocked_by(ui) == [core]
    assert result["dependency_parse_warnings"] == []


def test_task_dependency_defect_flags_a_mention_no_key_parses_from():
    assert s.task_dependency_defect("Depends on: Copy Core (the module)\n") is not None
    # Positive controls: a parsed key, an explicit none, and prose mention are all fine.
    assert s.task_dependency_defect("Depends on: copy-core\n") is None
    assert s.task_dependency_defect("- **Depends on:** none\n") is None
    assert s.task_dependency_defect("This module depends on the router.\n") is None


_UNPARSEABLE_LLD = _WRAPPED_LLD.replace("`Depends on: copy-core`", "Depends on: Copy Core")


def test_create_lld_tasks_refuses_a_section_whose_depends_on_does_not_parse(repo):
    """A Task whose dependency can't be parsed is never created edge-less; its siblings are."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _UNPARSEABLE_LLD)

    result = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert [c["key"] for c in result["created"]] == ["copy-core"]
    assert result["dependency_parse_warnings"] == ["copy-ui"]
    [skipped] = result["skipped_sections"]
    assert skipped["key"] == "copy-ui" and "no Task key parses" in skipped["reason"]


def test_finish_lld_stops_before_closing_when_a_depends_on_does_not_parse(repo):
    gh = _lld_tree()
    _lld_ready(gh, repo, text=_UNPARSEABLE_LLD)

    result = s.cmd_finish_lld(gh, 11, 9, repo_path=str(repo))

    assert result["failed_step"] == "create-lld-tasks"
    assert gh.issues[11]["state"] == "OPEN"


# --- Fix 2: open-gate derives its PR title --------------------------------------------

from tests.test_gate_title import _tree as _gate_tree  # noqa: E402


def _roadmap_tree():
    return _gate_tree({"number": 2, "labels": ["type:task"], "parent": 1, "stage": "product",
                       "title": "Product Roadmap"})


@pytest.mark.parametrize("given", [
    "Gate A: Product Roadmap - product.md for review",
    "Product Roadmap — product.md for review (#2)",
    "Gate A - Product Roadmap - Object storage",
])
def test_gate_pr_title_strips_gate_decoration_a_caller_included(given):
    """Regression: a caller-built title got the `— <doc> for review` suffix a second time."""
    assert s.gate_pr_title(_roadmap_tree(), 2, given) == ("Product Roadmap - Object storage", True)


def test_gate_pr_title_defaults_to_the_issue_title_composed_with_its_parent():
    assert s.gate_pr_title(_roadmap_tree(), 2) == ("Product Roadmap - Object storage", False)


def test_gate_pr_title_leaves_a_plain_title_and_a_foreign_issue_reference_alone():
    """Positive control: nothing to strip -> unchanged, not reported as normalised."""
    gh = _gate_tree({"number": 11, "labels": ["type:task"], "stage": "product"})
    assert s.gate_pr_title(gh, 11, "Fix widget (#40)") == ("Fix widget (#40)", False)
    assert s.gate_pr_title(gh, 11, "Gate-keeper retries") == ("Gate-keeper retries", False)


def test_open_gate_cli_title_is_optional_and_reported(monkeypatch, capsys):
    gh = _roadmap_tree()
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    monkeypatch.setattr(s, "cmd_open_gate",
                        lambda *a, **k: seen.setdefault("args", a) and {"gate_pr": 5})
    s.main(["open-gate", "2", "--doc", "product.md", "--next-stage", "architecture",
            "--summary", "x"])
    assert seen["args"][3] == "Product Roadmap - Object storage"
    assert '"title_normalized": false' in capsys.readouterr().out


# --- Fix 3: a docs-only main reconcile keeps exploratory evidence ----------------------

from tests.test_epic_close_evidence import TESTED, _epic, _marks  # noqa: E402

_RECONCILED = "<!-- epic-reconciled: 9 @ 2026-09-16T00:00:00Z -->"


def test_a_docs_only_reconcile_after_the_tested_head_keeps_the_evidence():
    """Regression: an unrelated product.md landing on main via the reconcile invalidated
    clean exploratory evidence, though the delta since the tested sha was docs only."""
    gh = _epic([_marks()[1], _RECONCILED],
               files_since=["docs/sdlc/issue-700/product.md"])
    assert s.missing_epic_verification(gh.issue_view(9)["comments"],
                                       s._EpicEvidenceDelta(gh, "epic-9")) == []


def test_a_reconcile_that_brought_code_still_invalidates_the_evidence():
    """Positive control: incoming code via the reconcile still forces a re-run."""
    gh = _epic([_marks()[1], _RECONCILED],
               files_since=["docs/x.md", "backend/src/other.ts"])
    assert len(s.missing_epic_verification(gh.issue_view(9)["comments"],
                                           s._EpicEvidenceDelta(gh, "epic-9"))) == 1


def test_close_epic_carries_evidence_over_a_docs_only_reconcile():
    gh = _epic([_marks()[1], _RECONCILED], files_since=["README.md"], delta=["docs/x.md"])
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n, **kw: None
    result = s.cmd_close_epic(gh, 9)
    assert result["merged"] is True
    assert result["evidence"]["exploratory"] == f"carried_forward_from {TESTED[:10]}"
