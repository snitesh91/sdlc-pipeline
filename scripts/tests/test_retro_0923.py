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
