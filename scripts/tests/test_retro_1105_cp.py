"""Retro 2026-11-05 (v0.4.3), control-plane fixes from park issue #4546: gh api error bodies,
stale blockedBy edges, create-issue at the sub-issue cap, close-epic conflicts and independent
main moves, unverified child suites, verifier-tree sweep, versioned-file collisions and
`attestable: "cloud"`.

Each regression goes RED against the pre-fix code; its positive control stays GREEN either way."""
import contextlib
import subprocess
import types

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_epic_close_evidence import _epic, _marks
from tests.test_v2_phase_tasks import DOC, FakeGh, _push_doc_branch, _v2_tree, repo  # noqa: F401


# ---- open-dev-pr-allow-empty-422: `gh api` puts the 422 reason on stdout ----

def _failing_run(stdout, stderr="gh: Validation Failed (HTTP 422)\n"):
    return lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)


def test_a_gh_api_error_keeps_the_response_body_from_stdout(monkeypatch):
    body = '{"message":"Validation Failed","errors":[{"message":"No commits between main and issue-5"}]}'
    monkeypatch.setattr(subprocess, "run", _failing_run(body))
    with pytest.raises(GhError) as exc:
        s._default_runner(["gh", "api", "repos/o/r/pulls", "-X", "POST"])
    assert s._NO_COMMITS_RE.search(str(exc.value))


def test_a_failure_with_empty_stdout_keeps_the_old_message(monkeypatch):
    # Positive control.
    monkeypatch.setattr(subprocess, "run", _failing_run("", stderr="boom\n"))
    with pytest.raises(GhError) as exc:
        s._default_runner(["git", "status"])
    assert str(exc.value) == "command failed (1): git status\nboom\n"


# ---- no-remove-blocked-by ----

def test_remove_blocked_by_drops_an_edge_and_is_a_noop_when_absent():
    gh = FakeGh([{"number": 1}, {"number": 2}])
    gh.add_blocked_by(2, 1)
    assert s.cmd_remove_blocked_by(gh, 2, 1)["removed"] is True and gh.blocked_by(2) == []
    assert s.cmd_remove_blocked_by(gh, 2, 1)["removed"] is False


_CHAINED = (
    "# lld for epic 9\n\n"
    "## Task skeleton-health: Add /health endpoint\nDesign.\n\n## Footprint\n- `src/health.js`\n\n"
    "## Task greet-endpoint: Add /greet endpoint\nDepends on: skeleton-health\nDesign.\n\n"
    "## Footprint\n- `src/greet.js`\n")


def _numbered(repo):
    return subprocess.run(["git", "show", f"origin/epic-9:{DOC}/epic-9/lld.md"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


def test_create_lld_tasks_removes_an_edge_a_revision_dropped(repo):
    """Regression (#4822): an LLD revision dropped ordering edges; the old ones stayed and
    stalled the parallel wave."""
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CHAINED)
    tasks = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))["tasks"]
    greet, health = tasks["greet-endpoint"], tasks["skeleton-health"]
    assert gh.blocked_by(greet) == [health]
    revised = _numbered(repo).replace("Depends on: skeleton-health\n", "")
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", revised, base="epic-9")

    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))

    assert again["edges_removed"] == [{"issue": greet, "on": health}]
    assert gh.blocked_by(greet) == []


def test_create_lld_tasks_keeps_an_edge_the_doc_still_names(repo):
    # Positive control.
    gh = _v2_tree()
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CHAINED)
    tasks = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))["tasks"]
    again = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    assert "edges_removed" not in again
    assert gh.blocked_by(tasks["greet-endpoint"]) == [tasks["skeleton-health"]]


def test_create_lld_tasks_never_removes_an_edge_onto_a_non_sibling(repo):
    # Positive control: a hand-added edge onto an issue outside the Epic's Tasks stays.
    gh = _v2_tree({"number": 50, "labels": ["type:task"]})
    _push_doc_branch(repo, "epic-9", f"{DOC}/epic-9/lld.md", _CHAINED)
    greet = s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))["tasks"]["greet-endpoint"]
    gh.add_blocked_by(greet, 50)
    s.cmd_create_lld_tasks(gh, 9, repo_path=str(repo))
    assert 50 in gh.blocked_by(greet)


# ---- create-issue-not-idempotent-on-link-failure ----

class _CappedGh(FakeGh):
    total = 100

    def sub_issue_total(self, n):
        return self.total


def test_create_issue_refuses_at_the_parent_cap_before_creating():
    """Regression (#3702): each retry of a create that failed only its link made a copy."""
    gh = _CappedGh([{"number": 9, "labels": ["type:epic"]}])
    with pytest.raises(GhError, match="nothing was created"):
        s.cmd_create_issue(gh, "t", "b", 9, [])
    assert set(gh.issues) == {9}


def test_create_issue_below_the_cap_creates_and_links():
    # Positive control.
    gh = _CappedGh([{"number": 9, "labels": ["type:epic"]}])
    gh.total = 99
    out = s.cmd_create_issue(gh, "t", "b", 9, [])
    assert gh.issues[out["issue"]]["parent"] == 9


def test_an_unlinked_create_exits_zero_so_a_retry_never_duplicates(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: None)
    monkeypatch.setattr(s, "cmd_create_issue", lambda *a, **k: {
        "issue": 7, "ok": False, "linked": False, "failed_step": "add_sub_issue"})
    assert s.main(["create-issue", "--title", "t", "--body", "b", "--parent", "9"]) == 0
    monkeypatch.setattr(s, "cmd_create_issue", lambda *a, **k: {"issue": 7, "ok": False})
    assert s.main(["create-issue", "--title", "t", "--body", "b", "--parent", "9"]) == 1


# ---- close-epic-reconcile-conflict-unstructured ----

def test_close_epic_reports_a_reconcile_conflict_as_a_structured_refusal(monkeypatch):
    gh = _epic([])
    gh.branch_behind_by = lambda branch, base="main": 3
    monkeypatch.setattr(s, "branch_lock", lambda b: contextlib.nullcontext())

    class _Ws:
        def __init__(self, *a):
            self.path, self.retained = "/tmp/ws", None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def conflict(*a, **k):
        raise s.MergeConflict(["a.ts", "b.ts"])

    monkeypatch.setattr(s, "BranchWorkspace", _Ws)
    monkeypatch.setattr(s, "git_reconcile_branch", conflict)
    out = s.cmd_close_epic(gh, 9)
    assert out["conflict"] is True and out["conflicting_files"] == ["a.ts", "b.ts"]
    assert out["merged"] is False and "Git-conflict handling" in out["reason"]


# ---- close-epic-recheck-on-independent-main ----

def test_a_main_delta_the_epic_never_touched_keeps_the_exploratory_record():
    """Regression (#4540): an infra tfvars commit on main re-required the whole close."""
    gh = _epic(_marks(), files_since=["infra/prod/terraform.tfvars"], delta=("backend/a.ts",))
    out = s.cmd_close_epic(gh, 9)
    assert "missing_verification" not in out
    assert out["evidence"]["exploratory"].startswith("carried_forward_from")


def test_a_main_delta_on_an_epic_file_still_stales_the_record():
    # Positive control.
    gh = _epic(_marks(), files_since=["backend/a.ts"], delta=("backend/a.ts",))
    out = s.cmd_close_epic(gh, 9)
    assert any("code has landed since" in p for p in out["missing_verification"])


def test_close_epic_reads_suite_checks_through_the_sync_carry(monkeypatch):
    seen = []
    monkeypatch.setattr(s, "effective_checks", lambda gh, pr: seen.append(pr) or [])
    s.cmd_close_epic(_epic(_marks(), pr=38), 9)
    assert seen == [38]


# ---- child-prs-merge-without-suite-evidence ----

_IT = {"workflow": "IT", "suite": "it", "prefixes": ("api/",), "files": (), "excludeGlobs": (),
       "bases": ("main",), "attestable": False}


def test_merge_pr_names_suites_a_task_pr_touched_with_no_evidence(monkeypatch):
    monkeypatch.setattr(s, "required_workflows", lambda base=None: (_IT,))
    assert s._unverified_suites(["api/x.ts"], [], [], "abc", "epic-9") == ["it"]


def test_merge_pr_names_none_for_main_or_an_untouched_or_attested_suite(monkeypatch):
    # Positive control.
    monkeypatch.setattr(s, "required_workflows", lambda base=None: (_IT,))
    assert s._unverified_suites(["api/x.ts"], [], [], "abc1234", "main") == []
    assert s._unverified_suites(["web/x.ts"], [], [], "abc1234", "epic-9") == []
    attested = [{"body": "<!-- local-ci: it:5 @ abc1234 -->"}]
    assert s._unverified_suites(["api/x.ts"], [], attested, "abc1234", "epic-9") == []


# ---- prune-stale-misses-ephemeral-trees ----

def test_prune_stale_finds_a_dead_initiative_close_tree(monkeypatch, tmp_path):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "root", str(tmp_path))
    prefix = s.PIPELINE["worktrees"]["ephemeralPrefix"]
    listing = (f"worktree /main\nHEAD a\nbranch refs/heads/main\n\n"
               f"worktree {tmp_path}/{prefix}initiative-12\nHEAD b\ndetached\n\n"
               f"worktree {tmp_path}/{prefix}epic-9-123\nHEAD c\nbranch refs/heads/epic-9\n")
    trees = s._initiative_close_trees(".", lambda argv: listing)
    assert trees == {12: f"{tmp_path}/{prefix}initiative-12"}
    calls = []
    out = s._remove_verifier_tree(".", 12, trees[12], lambda argv: calls.append(argv) or "", False)
    assert out["released"] is True and calls[0][3:6] == ["worktree", "remove", "--force"]


# ---- policy-version-silent-collision ----

_VERSION = 'export const POLICY_VERSION = "{}";\n'


def _versioned(monkeypatch, files, epic_files=("web/policy/privacy.tsx",)):
    """`files`: `{(ref, path): text}` for origin/epic-9 and origin/main."""
    monkeypatch.setitem(s.PIPELINE["epicClose"], "versionedFiles", [
        {"versionFile": "shared/policy/version.ts",
         "versionPattern": r'POLICY_VERSION = "([^"]+)"', "textPaths": ["web/policy/**"]}])
    gh = types.SimpleNamespace(base_delta_files=lambda head, base="main": list(epic_files))

    def runner(argv):
        if argv[3] == "show":
            ref, path = argv[4][len("origin/"):].split(":", 1)
            if (ref, path) not in files:
                raise GhError("missing")
            return files[(ref, path)]
        return ""
    return s._versioned_file_collisions(gh, "epic-9", "main", ".", runner)


def test_policy_text_changed_under_main_s_current_version_is_a_collision(monkeypatch):
    """Regression (#4853/#4132): both Epics set version.ts to one string; the epic's policy
    page text differs from main's while the version equals main's current one."""
    out = _versioned(monkeypatch, {
        ("epic-9", "shared/policy/version.ts"): _VERSION.format("2026-11-04"),
        ("main", "shared/policy/version.ts"): _VERSION.format("2026-11-04"),
        ("epic-9", "web/policy/privacy.tsx"): "A",
        ("main", "web/policy/privacy.tsx"): "B"})
    assert len(out) == 1 and "'2026-11-04'" in out[0] and "web/policy/privacy.tsx" in out[0]


def test_a_differing_version_or_unchanged_text_is_no_collision(monkeypatch):
    # Positive control.
    base = {("main", "shared/policy/version.ts"): _VERSION.format("2026-11-04"),
            ("main", "web/policy/privacy.tsx"): "B"}
    assert _versioned(monkeypatch, {**base,
        ("epic-9", "shared/policy/version.ts"): _VERSION.format("2026-11-05"),
        ("epic-9", "web/policy/privacy.tsx"): "A"}) == []
    assert _versioned(monkeypatch, {**base,
        ("epic-9", "shared/policy/version.ts"): _VERSION.format("2026-11-04"),
        ("epic-9", "web/policy/privacy.tsx"): "B"}) == []
    assert _versioned(monkeypatch, base, epic_files=("api/x.ts",)) == []


# ---- cloud-ci-local-checks: attestable "cloud" ----

_CLOUD_IT = {"workflow": "IT", "suite": "it", "prefixes": ("api/",), "files": (),
             "excludeGlobs": (), "bases": ("main",), "attestable": "cloud"}
_ATTEST = "<!-- local-ci: it:5 @ abc1234 -->"


def test_a_cloud_suite_is_attestable_only_under_the_harness_signal(monkeypatch):
    monkeypatch.setattr(s, "CLOUD_ATTESTABLE_SUITES", frozenset({"it"}))
    monkeypatch.setenv("SDLC_PLACEMENT", "cloud")  # the laptop override never counts
    assert s.suite_not_attestable("it") is True
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert s.suite_not_attestable("it") is False


def test_a_false_suite_stays_non_attestable_in_the_cloud(monkeypatch):
    # Positive control.
    monkeypatch.setattr(s, "NON_ATTESTABLE_SUITES", frozenset({"it"}))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert s.suite_not_attestable("it") is True and s.suite_not_attestable("other") is False


def test_the_merge_gate_takes_a_cloud_suite_only_from_a_cloud_stamped_attestation(monkeypatch):
    """Review: `SDLC_PLACEMENT=cloud record-local-ci` on a laptop satisfied a cloud suite, and
    missing_required_workflows ignored `attestable`."""
    monkeypatch.setattr(s, "required_workflows", lambda base=None: (_CLOUD_IT,))
    laptop = [{"body": _ATTEST}]
    assert s.missing_required_workflows(["api/x.ts"], [], laptop, "abc1234", "main") == ["IT"]
    cloud = [{"body": f"{_ATTEST}\n{s.LOCAL_CI_CLOUD_MARKER}"}]
    assert s.missing_required_workflows(["api/x.ts"], [], cloud, "abc1234", "main") == []


def test_the_merge_gate_never_takes_an_attestation_for_a_false_suite(monkeypatch):
    monkeypatch.setattr(s, "required_workflows",
                        lambda base=None: ({**_CLOUD_IT, "attestable": False},))
    cloud = [{"body": f"{_ATTEST}\n{s.LOCAL_CI_CLOUD_MARKER}"}]
    assert s.missing_required_workflows(["api/x.ts"], [], cloud, "abc1234", "main") == ["IT"]
    # Positive control: an attestable suite still takes a plain attestation.
    monkeypatch.setattr(s, "required_workflows",
                        lambda base=None: ({**_CLOUD_IT, "attestable": True},))
    assert s.missing_required_workflows(["api/x.ts"], [], [{"body": _ATTEST}], "abc1234",
                                        "main") == []


def test_close_epic_takes_a_cloud_suite_only_from_a_cloud_stamped_attestation(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setattr(s, "CLOUD_ATTESTABLE_SUITES", frozenset({"it"}))
    view = {"headRefOid": "abc1234", "comments": [{"body": _ATTEST}]}
    gh = types.SimpleNamespace(files_since=lambda sha, ref: [])
    assert s._pr_suite_evidence(gh, _CLOUD_IT, view, [], "abc1234", True, set()) is None
    view["comments"] = [{"body": f"{_ATTEST}\n{s.LOCAL_CI_CLOUD_MARKER}"}]
    assert s._pr_suite_evidence(gh, _CLOUD_IT, view, [], "abc1234", True, set()) == "fresh"


def test_record_local_ci_stamps_the_placement_only_under_the_harness_signal(monkeypatch):
    monkeypatch.setenv("SDLC_PLACEMENT", "cloud")
    posted = []
    gh = types.SimpleNamespace(pr_comment=lambda pr, body: posted.append(body))
    monkeypatch.setattr(s, "read_ci_evidence", lambda path: "ok")
    suite = s.LOCAL_CI_SUITES[0]
    monkeypatch.setattr(s, "suite_not_attestable", lambda suite: False)
    monkeypatch.setitem(s.LOCAL_CI_COMMAND_PATTERNS, suite, None)
    s.cmd_record_local_ci(gh, 5, suite, "abc1234", "make test", "out.txt")
    assert s.LOCAL_CI_CLOUD_MARKER not in posted[-1]
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    s.cmd_record_local_ci(gh, 5, suite, "abc1234", "make test", "out.txt")
    assert s.LOCAL_CI_CLOUD_MARKER in posted[-1]


# ---- the cloud epic PR is marked in its body at creation ----

def _cloud_close(monkeypatch, attestable):
    monkeypatch.setattr(s, "required_workflows",
                        lambda base=None: ({**_CLOUD_IT, "attestable": attestable},))
    gh = _epic([_marks()[0], _marks()[1]])
    created, labels = [], []
    gh.pr_create = lambda **kw: created.append(kw) or 38
    gh.ensure_label = lambda label: None
    gh.pr_add_label = lambda n, label: labels.append((n, label))

    class _Stop(Exception):
        pass

    def stop(n):
        raise _Stop  # past the create; the merge is not under test

    gh.pr_ready = stop
    try:
        s.cmd_close_epic(gh, 9)
    except _Stop:
        pass
    return created, labels


def test_a_cloud_epic_pr_carries_the_ci_local_marker_from_creation(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    created, labels = _cloud_close(monkeypatch, "cloud")
    assert s.CI_LOCAL_MARKER in created[0]["body"] and labels == [(38, "ci:local")]


def test_a_laptop_or_non_cloud_suite_epic_pr_has_no_marker(monkeypatch):
    # Positive control.
    created, labels = _cloud_close(monkeypatch, "cloud")
    assert s.CI_LOCAL_MARKER not in created[0]["body"] and labels == []
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    created, labels = _cloud_close(monkeypatch, True)
    assert s.CI_LOCAL_MARKER not in created[0]["body"] and labels == []
