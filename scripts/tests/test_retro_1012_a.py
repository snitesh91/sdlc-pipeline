"""Retro 2026-10-12 (fork A): standing children under an initiative branch, merge-pr's close on
a non-default base, close-epic suite scope, initiative-close scaffolding check, config read
from the PR base, and close-epic's carry-forward reconcile skip."""
import pytest

import sdlc_next as s
from tests.test_initiative_branch import OPT_IN, PROFILE, _exists_on_origin
from tests.test_v2_phase_tasks import FakeGh, repo  # noqa: F401


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setitem(s.PIPELINE, "initiativeProfiles", [PROFILE])


def _standing_tree(opt_in=True):
    labels = ["type:initiative"] + ([OPT_IN] if opt_in else [])
    return FakeGh([{"number": 6, "labels": labels, "title": "Tijori"},
                   {"number": 9, "labels": ["type:epic", "epic:standing"], "parent": 6},
                   {"number": 12, "labels": ["type:task"], "parent": 9, "stage": "development"}])


# --- 1. a standing Epic's child bases on its Initiative's branch ------------------------

def test_a_standing_child_under_an_initiative_branch_integrates_there(opted_in):
    assert s.integration_base(_standing_tree(), 12) == "initiative-6"


def test_a_standing_child_without_the_opt_in_stays_on_main(opted_in):
    """Positive control."""
    assert s.integration_base(_standing_tree(opt_in=False), 12) == "main"


def test_worktree_add_cuts_a_standing_child_from_the_initiative_branch(repo, opted_in):
    result = s.cmd_worktree_add(_standing_tree(), 12, repo_path=str(repo))
    assert result["base"] == "origin/initiative-6"
    assert _exists_on_origin(repo, "initiative-6")


# --- 2. merge-pr closes a unit merged into a non-default base ---------------------------

def _finalize(gh, monkeypatch, base):
    monkeypatch.setattr(s, "record_terminal_unit", lambda *a, **k: None)
    monkeypatch.setattr(s, "cleanup_unit", lambda *a, **k: {"worktree": {}})
    return s._finalize_merged_pr(gh, 50, 12, ".", base, [], {})


def test_merge_pr_closes_a_standing_child_merged_into_the_initiative_branch(opted_in, monkeypatch):
    gh = _standing_tree()
    out = _finalize(gh, monkeypatch, s.integration_base(gh, 12))
    assert out["issue_closed"] is True and gh.issues[12]["state"] == "CLOSED"


def test_merge_pr_leaves_closing_a_main_merge_to_github(opted_in, monkeypatch):
    """Positive control: on the default branch `Closes #n` closes it."""
    gh = _standing_tree(opt_in=False)
    out = _finalize(gh, monkeypatch, s.integration_base(gh, 12))
    assert out["issue_closed"] is False and gh.issues[12]["state"] == "OPEN"


# --- 3. close-epic scopes suites to the epic PR's own (complete) file list --------------

class _EpicPrGh:
    def pr_view(self, n, fields=""):
        return {"comments": [], "headRefOid": "feedface"}

    def pr_checks(self, n):
        return [{"name": "Frontend CI", "bucket": "pass", "workflow": "Frontend CI"}]


def test_a_big_epic_pr_requires_only_the_suites_its_files_touch():
    files = [f"frontend/f{i}.ts" for i in range(347)]
    out = s._epic_suite_evidence(_EpicPrGh(), "epic-9", [], files, 77, False)
    assert out["suites"]["backend"] == "not_required"
    assert out["missing"] == []


def test_a_compare_delta_at_its_cap_is_still_treated_as_truncated():
    """Positive control: without an epic PR the 300-file compare list may be cut short."""
    files = [f"frontend/f{i}.ts" for i in range(300)]
    out = s._epic_suite_evidence(_EpicPrGh(), "epic-9", [], files, None, False)
    assert out["suites"]["backend"] == "missing"


# --- 6. close-epic skips a main reconcile that brings only carry-forward files ---------

from tests.test_epic_close_evidence import TESTED, _epic, _marks  # noqa: E402


def _behind_epic(incoming, epic_side=("backend/a.ts",)):
    gh = _epic(_marks())
    gh.branch_behind_by = lambda branch, base="main": 2
    gh.base_delta_files = lambda head, base="main": list(
        incoming if head == "epic-9" else epic_side)
    gh.pr_create = lambda **kw: 38
    gh.pr_ready = lambda n: None
    gh.pr_merge = lambda n, **kw: None
    gh.pr_files = lambda n: list(epic_side)
    gh.pr_checks = lambda n: [{"name": "Backend CI", "bucket": "pass", "workflow": "Backend CI"}]
    return gh


def test_close_epic_skips_a_reconcile_that_brings_only_carry_forward_files(monkeypatch):
    def no_reconcile(*a, **k):
        raise AssertionError("reconciled")
    monkeypatch.setattr(s, "git_reconcile_branch", no_reconcile)
    result = s.cmd_close_epic(_behind_epic(["docs/ops/runner.md"]), 9)
    assert result["merged"] is True
    assert result["reconcile_skipped"] == {"behind": 2, "files": ["docs/ops/runner.md"]}


class _Ws:
    path, retained = "/nowhere", None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("incoming,epic_side", [
    (["backend/b.ts"], ["backend/a.ts"]),        # incoming code
    (["docs/x.md"], ["docs/x.md"]),              # the epic touched the same file
])
def test_close_epic_still_reconciles_code_or_an_overlapping_file(incoming, epic_side, monkeypatch):
    """Positive control."""
    calls = []
    monkeypatch.setattr(s, "BranchWorkspace", _Ws)
    monkeypatch.setattr(s, "git_reconcile_branch", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(s, "_refresh_epic_worktree", lambda *a, **k: None)
    result = s.cmd_close_epic(_behind_epic(incoming, epic_side), 9)
    assert calls and result["reconciled"] == 2 and "reconcile_skipped" not in result


# --- 5. required workflows include the ones the PR base's own config adds ---------------

_TIJORI_IT = {"workflow": "Tijori IT", "suite": "tijori-it", "prefixes": ["apps/tijori-api/"]}


@pytest.fixture
def base_config(monkeypatch):
    monkeypatch.setattr(s, "_BASE_WORKFLOWS_CACHE", {})
    monkeypatch.setattr(s, "_base_config_workflows",
                        lambda base, runner=None: [_TIJORI_IT] if base == "initiative-6" else [])


def test_a_workflow_only_the_base_branch_config_lists_is_required_there(base_config):
    files = ["apps/tijori-api/src/x.ts"]
    assert s.missing_required_workflows(files, [], base_ref="initiative-6") == ["Tijori IT"]
    assert s.uncovered_paths(files, "initiative-6") == []


def test_main_reads_only_the_local_config(base_config):
    """Positive control: a PR into main sees the local entries alone."""
    files = ["apps/tijori-api/src/x.ts"]
    assert s.missing_required_workflows(files, [], base_ref="main") == []
    assert s.uncovered_paths(files, "main") == files


def test_a_base_config_never_overrides_a_local_entry(base_config, monkeypatch):
    monkeypatch.setattr(s, "_base_config_workflows", lambda base, runner=None: [
        {"workflow": "Backend CI", "suite": "backend", "prefixes": ["elsewhere/"]}])
    [backend] = [w for w in s.required_workflows("epic-9") if w["workflow"] == "Backend CI"]
    assert backend["prefixes"] == ("backend/",)


def test_base_config_workflows_reads_the_committed_config_on_origin(repo, monkeypatch):
    import json, os
    from tests.test_v2_phase_tasks import _push_doc_branch
    cfg = json.load(open(os.environ["SDLC_CONFIG"]))
    cfg["requiredWorkflows"].append(_TIJORI_IT)
    _push_doc_branch(repo, "initiative-6", s.CONFIG_FILENAME, json.dumps(cfg))
    from tests.test_v2_phase_tasks import _git
    _git("fetch", "-q", "origin", cwd=repo)
    monkeypatch.setenv("SDLC_CONFIG", str(repo / s.CONFIG_FILENAME))
    names = [w["workflow"] for w in s._base_config_workflows("initiative-6")]
    assert "Tijori IT" in names
    assert s._base_config_workflows("no-such-branch") == []


# --- 4. initiative close lists what still names the initiative branch ------------------

def test_initiative_scaffolding_lists_repo_hits_and_closed_issues(repo):
    from tests.test_v2_phase_tasks import _git, _push_doc_branch
    _push_doc_branch(repo, "main", ".github/workflows/deploy.yml",
                     "on: workflow_dispatch\n# run with --ref initiative-6\n")
    _push_doc_branch(repo, "main", "docs/sdlc/epic-9/architecture.md", "initiative-6 history\n")
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]},
                 {"number": 9, "labels": ["type:epic"], "parent": 6, "state": "CLOSED"},
                 {"number": 10, "labels": ["type:task"], "parent": 9, "state": "CLOSED",
                  "body": "Remove the shim at Initiative close."},
                 {"number": 11, "labels": ["type:task"], "parent": 9, "state": "CLOSED",
                  "body": "Done."}])
    hits = s.initiative_scaffolding(gh, 6, repo_path=str(repo))
    assert {"path": ".github/workflows/deploy.yml", "line": 2,
            "text": "# run with --ref initiative-6"} in hits
    assert {"issue": 10, "title": "issue 10"} in hits
    assert not any(h.get("path", "").startswith("docs/sdlc/") or h.get("issue") == 11
                   for h in hits)


def test_initiative_scaffolding_is_empty_when_nothing_names_it(repo):
    """Positive control."""
    gh = FakeGh([{"number": 6, "labels": ["type:initiative"]}])
    assert s.initiative_scaffolding(gh, 6, repo_path=str(repo)) == []


# --- review: a standing child's gate PR targets its integration base ---------------------

def _gate_pr_base(gh, issue):
    s.cmd_open_gate(gh, ".", issue, "Gate", "architecture.md", "development", "ok",
                    runner=lambda argv: "cafe1234\n")
    return next(pr["baseRefName"] for pr in gh.prs.values() if pr["headRefName"] == f"issue-{issue}")


def test_a_standing_childs_gate_under_an_initiative_branch_targets_it(opted_in):
    assert _gate_pr_base(_standing_tree(), 12) == "initiative-6"


def test_a_standing_childs_gate_without_the_opt_in_targets_main(opted_in):
    """Positive control."""
    assert _gate_pr_base(_standing_tree(opt_in=False), 12) == "main"
