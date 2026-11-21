"""Review 2026-11-21 (v0.4.6): a direct Task PR keeps a suite only the initiative branch runs;
CI failure lines lose every ANSI code; a standing Epic's close checklist names no Epic docs;
`open-gate` re-runs onto the gate PR it made; `teardown-epic-stack` never takes the base
profile or a shell-unsafe name."""
import pytest

import sdlc_next as s
from sdlc import checks
from tests.test_sdlc_next import _enable_stack
from tests.test_v2_phase_tasks import DOC, FakeGh, _push_doc_branch, repo  # noqa: F401

BRANCHED = {"name": "kipsa", "match": {"label": "initiative:branch"}, "branch": True}
STANDING = "epic:standing"


def _spec(suite, *bases):
    return {"workflow": suite.upper(), "suite": suite, "prefixes": ("apps/",), "files": (),
            "excludeGlobs": (), "bases": bases, "attestable": True}


# --- _pr_deferred_suites --------------------------------------------------------------------

def test_a_direct_task_pr_still_owes_a_suite_scoped_to_the_initiative_branch_only(monkeypatch):
    """Regression: deferred to an initiative PR into `main` that never runs it."""
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [BRANCHED])
    monkeypatch.setattr(checks, "required_workflows", lambda base=None: (
        _spec("it", "main", "initiative-*"), _spec("smoke", "initiative-*")))
    gh = FakeGh([{"number": 6, "labels": ["type:initiative", "initiative:branch"]},
                 {"number": 20, "labels": ["type:task"], "parent": 6}])
    deferred = checks._pr_deferred_suites(gh, "issue-20", "initiative-6")
    assert deferred == ("it",)  # positive control: the main-scoped suite is still deferred
    assert checks.merge_gate_status(["apps/a.ts"], [], [], "abc", "initiative-6", deferred) == (
        "missing-checks", ["SMOKE"])


# --- ci-failure-summary ---------------------------------------------------------------------

def test_failure_lines_strip_cursor_codes_as_well_as_colours():
    """Regression: a second, colour-only `_ANSI_RE` replaced the full one."""
    log = "job\tstep\t\x1b[2K\x1b[31mFAIL\x1b[39m src/a.spec.ts\njob\tstep\tPASS src/b.spec.ts"
    assert s._failure_lines(log) == ["FAIL src/a.spec.ts"]
    assert s.failed_specs(log) == ["src/a.spec.ts"]


# --- check-epics-closeable ------------------------------------------------------------------

def test_a_standing_epics_checklist_names_no_epic_docs(monkeypatch):
    """Regression: a Bugs Epic's checklist said its architecture.md and lld.md never merged."""
    from sdlc.config import _INITIATIVE_PROFILE_DEFAULTS
    monkeypatch.setitem(_INITIATIVE_PROFILE_DEFAULTS, "branch", True)
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 9, "labels": ["type:epic", STANDING], "parent": 6},
                 {"number": 12, "labels": ["type:task"], "parent": 9, "state": "CLOSED"}])
    out = s.cmd_check_epics_closeable(gh)["closeable_epics"]
    assert out[0]["epic"] == 9 and out[0]["docs_missing_from_epic_branch"] == []
    assert "epic doc(s) never reached" not in gh.comments_on(9)[-1]


# --- open-gate ------------------------------------------------------------------------------

def test_open_gate_rerun_reuses_the_gate_pr_its_first_run_made(repo):
    """Regression: a failure after the create left every re-run failing on GitHub's 422."""
    class Gh(FakeGh):
        def pr_create(self, base, head, title, body, draft=False):
            if self.pr_list_for_branch(head):
                raise s.GhError(f"HTTP 422: A pull request already exists for o:{head}.")
            return super().pr_create(base, head, title, body, draft)

    gh = Gh([{"number": 90, "labels": ["type:epic", STANDING]},
             {"number": 91, "labels": ["type:task"], "parent": 90, "stage": "architecture"}])
    _push_doc_branch(repo, "issue-91", f"{DOC}/issue-91/architecture.md", "# arch\n")
    first = s.cmd_open_gate(gh, str(repo), 91, "Fix it", "architecture.md", "development", "x")
    again = s.cmd_open_gate(gh, str(repo), 91, "Fix it", "architecture.md", "development", "x")
    assert again["gate_pr"] == first["gate_pr"] and len(gh.prs) == 1


def test_open_gate_still_raises_any_other_create_failure(repo):
    """Positive control."""
    class Gh(FakeGh):
        def pr_create(self, *a, **k):
            raise s.GhError("HTTP 502")

    gh = Gh([{"number": 91, "labels": ["type:task"], "stage": "architecture"}],
            prs={5: {"headRefName": "issue-91", "baseRefName": "main"}})
    _push_doc_branch(repo, "issue-91", f"{DOC}/issue-91/architecture.md", "# arch\n")
    with pytest.raises(s.GhError, match="502"):
        s.cmd_open_gate(gh, str(repo), 91, "Fix it", "architecture.md", "development", "x")


# --- teardown-epic-stack --------------------------------------------------------------------

def test_teardown_refuses_the_base_profile_and_a_shell_unsafe_name(monkeypatch, tmp_path):
    """Regression: `--profile dev` deleted the base env and secrets files."""
    _enable_stack(monkeypatch, tmp_path)
    ran = []
    shell = lambda c, cwd: ran.append(c) or ""  # noqa: E731
    result = s.cmd_teardown_epic_stack(159, profile="dev", shell=shell,
                                       running_probe=lambda p: [])
    assert result["refused"] is True and result["torn_down"] is False
    with pytest.raises(s.GhError, match="--project"):
        s.cmd_teardown_epic_stack(159, project="x; rm -rf ~", shell=shell,
                                  running_probe=lambda p: [])
    assert ran == [] and (tmp_path / ".env.dev").exists() and (tmp_path / ".secrets.dev").exists()
