"""2026-10-04 retro (cp2): doc-root templates fallback and hints, the epic-close checklist's
doc root, OOM/disk infra signatures, parking releases docker, route's skipped list, and
idempotent sync-conflict / merged-via / handoff comments."""
import json
from types import SimpleNamespace

import pytest

import sdlc_next as s
from tests.test_doc_roots import BOOKSHAW, TIJORI, AncestryGh, ISSUES
from tests.test_v2_phase_tasks import FakeGh, _advance_origin, _git, repo  # noqa: F401
from tests.test_retro_composites import _cut_epic, _epic_tree, runs  # noqa: F401

TEMPLATES = s.PIPELINE["docTemplates"]


@pytest.fixture
def per_product(monkeypatch):
    """Bookshaw is the top-level docRoot; Tijori is matched by its label only."""
    monkeypatch.setattr(s, "DOC_ROOT", BOOKSHAW)
    monkeypatch.setitem(s.CONFIG, "docRoots", [
        {"name": "tijori", "match": {"label": "product:tijori"}, "docRoot": TIJORI}])


# --- 1. templates fall back to the top-level docRoot's ---

def test_doc_templates_falls_back_to_the_top_level_root(per_product, tmp_path, monkeypatch):
    monkeypatch.setattr(s, "config_repo_root", lambda: str(tmp_path))
    assert s.doc_templates_dir(TIJORI) == f"{BOOKSHAW}/{TEMPLATES}"


def test_doc_templates_prefers_the_products_own_dir(per_product, tmp_path, monkeypatch):
    (tmp_path / TIJORI / TEMPLATES).mkdir(parents=True)
    monkeypatch.setattr(s, "config_repo_root", lambda: str(tmp_path))
    assert s.doc_templates_dir(TIJORI) == f"{TIJORI}/{TEMPLATES}"
    assert s.doc_templates_dir(BOOKSHAW) == f"{BOOKSHAW}/{TEMPLATES}"


def test_doc_root_cmd_reports_the_fallback(per_product, tmp_path, monkeypatch):
    gh = AncestryGh()
    monkeypatch.setattr(s, "config_repo_root", lambda: str(tmp_path))
    assert s.cmd_doc_root(gh, 2101)["docTemplates"] == f"{BOOKSHAW}/{TEMPLATES}"
    (tmp_path / TIJORI / TEMPLATES).mkdir(parents=True)
    assert s.cmd_doc_root(gh, 2101)["docTemplates"] == f"{TIJORI}/{TEMPLATES}"


@pytest.mark.parametrize("sub", ["", ".claude", ".config"])
def test_config_repo_root_is_the_dir_holding_the_config(tmp_path, monkeypatch, sub):
    cfg = tmp_path / sub / s.CONFIG_FILENAME
    cfg.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SDLC_CONFIG", str(cfg))
    assert s.config_repo_root() == str(tmp_path)


# --- 2. next-action / prepare-rework write doc-root hints ---

def _next_action(monkeypatch, gh, issue):
    monkeypatch.setattr(s, "check_placement", lambda *a, **k: None)
    monkeypatch.setattr(s, "note_in_flight", lambda *a, **k: None)
    gh.issue_list = lambda: []
    monkeypatch.setattr(s, "decide_next_action",
                        lambda *a, **k: {"action": "delegate", "issue": issue,
                                         "stage": "development"})
    return s.cmd_next_action(gh, SimpleNamespace(epic=2010, run_id=None))


def _hints(tmp_path):
    return json.loads((tmp_path / s.DOC_ROOT_HINTS_FILE).read_text())


def test_next_action_hints_the_epic_and_the_unit(per_product, tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.setitem(s.CONFIG, "docRoots", [
        {"name": "tijori", "match": {"issues": [1994]}, "docRoot": TIJORI}])
    gh = AncestryGh()
    assert _next_action(monkeypatch, gh, 2011)["issue"] == 2011
    assert {k: v["docRoot"] for k, v in _hints(tmp_path).items()} == {
        "2011": TIJORI, "2010": TIJORI, "1994": TIJORI}


def test_next_action_ignores_an_unreadable_ancestry(per_product, tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    gh = AncestryGh()

    def broken(n):
        raise s.GhError("graphql: 502")
    gh.issue_epic_info = broken
    assert _next_action(monkeypatch, gh, 2011)["action"] == "delegate"
    assert not (tmp_path / s.DOC_ROOT_HINTS_FILE).exists()


def test_next_action_without_doc_roots_looks_nothing_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    monkeypatch.delitem(s.CONFIG, "docRoots", raising=False)
    gh = AncestryGh()
    _next_action(monkeypatch, gh, 2011)
    assert gh.calls == [] and not (tmp_path / s.DOC_ROOT_HINTS_FILE).exists()


def test_prepare_rework_hints_the_unit(per_product, tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    gh = AncestryGh()
    gh.pr_list_for_branch = lambda *a, **k: []
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/2101"})
    monkeypatch.setattr(s, "cmd_sync_branch", lambda *a, **k: {"synced": True})
    monkeypatch.setattr(s, "git_rev_parse_head", lambda *a, **k: "abc")
    assert s.cmd_prepare_rework(gh, 2101)["ok"] is True
    assert _hints(tmp_path)["2101"]["docRoot"] == TIJORI


# --- 3. the epic-close checklist reads the epic's own doc root ---

def _closeable(epic_labels):
    return FakeGh([{"number": 1326, "labels": ["type:initiative"]},
                   {"number": 1755, "parent": 1326, "labels": ["type:epic", *epic_labels]},
                   {"number": 1756, "parent": 1755, "labels": ["type:task"],
                    "state": "CLOSED"}])


@pytest.mark.parametrize("labels,root", [([], BOOKSHAW), (["product:tijori"], TIJORI)])
def test_epic_close_checklist_looks_under_the_epics_own_root(per_product, labels, root,
                                                            monkeypatch):
    gh = _closeable(labels)
    monkeypatch.setattr(s, "get_work_item_provider", lambda *a, **k: gh)
    gh.refs = {(f"{root}/epic-1755/{d}", "epic-1755") for d in ("architecture.md", "lld.md")}

    [result] = s.cmd_check_epics_closeable(gh)["closeable_epics"]

    assert result["docs_missing_from_epic_branch"] == []


# --- 4. OOM / killed / disk-full runner failures are infra-suspect ---

@pytest.mark.parametrize("log", [
    "Process completed with exit code 137.",
    "npm error command failed\nnpm error signal SIGKILL",
    "Error: Process terminated by signal 9",
    "worker received SIGKILL",
    "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
    "Error [ERR_WORKER_OUT_OF_MEMORY]: Worker terminated due to reaching memory limit",
    "kernel: Out of memory: Killed process 4242 (node)",
    "fork: Cannot allocate memory",
    "fatal error: runtime: out of memory",
    "write /tmp/x: no space left on device",
    "Error: ENOSPC: no space left on device, write",
    "##[error]The runner has received a shutdown signal.",
    "lost communication with the server",
    "Killed",
])
def test_runner_kills_and_disk_full_are_infra_suspect(log):
    assert s.infra_suspect_text(log), log


@pytest.mark.parametrize("log", [
    "AssertionError: expected 3, got 2",
    "FAIL test/memory.spec.ts > reports out of memory to the user",
    "expect(received).toBe(expected) // SIGKILL handler not installed",
    "TypeError: Cannot read properties of undefined (reading 'signal')",
    "test killed the stale session and asserted 404",
])
def test_ordinary_test_failures_are_not_infra_suspect(log):
    assert not s.infra_suspect_text(log), log


# --- 5. parking a unit releases its docker stack ---

class DockerRunner:
    """Docker over a fixed inventory (records `rm` calls); git sees no unit worktree."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["docker", "version"]:
            return "27.0\n"
        if argv[:2] == ["docker", "ps"]:
            return ("c1\tsdlc-dev-1058-db-1\tsdlc-dev-1058\n"
                    "c2\tsdlc-dev-10580-db-1\tsdlc-dev-10580\n")
        if argv[:3] == ["docker", "volume", "ls"]:
            return "sdlc-dev-1058_pg\t\nsdlc-dev-1058\t\nother_pg\t\n"
        if argv[:3] == ["docker", "network", "ls"]:
            return "sdlc-dev-1058_default\tsdlc-dev-1058\n"
        if argv[0] == "docker":
            return ""
        if "worktree" in argv and "list" in argv:
            return "worktree /repo\nHEAD x\nbranch refs/heads/main\n"
        raise AssertionError(f"unexpected call: {argv}")

    def removed(self):
        return sorted(c[-1] for c in self.calls if c[0] == "docker" and "rm" in c)


@pytest.mark.parametrize("park", [
    lambda gh: s.cmd_mark_blocked(gh, 1058, 1000),
    lambda gh: s.cmd_mark_needs_human(gh, 1058, "needs a decision"),
])
def test_parking_removes_the_units_docker_stack(park, monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "dockerCleanup", True)
    gh = FakeGh([{"number": 1000}, {"number": 1058}])
    gh._run = DockerRunner()

    result = park(gh)

    assert gh._run.removed() == ["c1", "sdlc-dev-1058", "sdlc-dev-1058_default",
                                 "sdlc-dev-1058_pg"]
    assert result["worktree"] == {"released": False, "reason": "no worktree"}
    assert [d["prefix"] for d in result["docker"]] == ["sdlc-dev-1058", "sdlc-epic-1058"]


def test_parking_survives_a_broken_docker(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "dockerCleanup", True)
    gh = FakeGh([{"number": 935}])
    runner = DockerRunner()
    gh._run = lambda argv: (_ for _ in ()).throw(s.GhError("daemon down")) \
        if argv[0] == "docker" else runner(argv)

    result = s.cmd_mark_needs_human(gh, 935, "stuck")

    assert result["status"] == "needs-human" and gh.issues[935]["status"] == "needs-human"
    assert all("skipped" in d for d in result["docker"])


def test_parking_with_docker_cleanup_off_touches_no_docker():
    gh = FakeGh([{"number": 1058}])
    gh._run = DockerRunner()
    assert "docker" not in s.cmd_mark_needs_human(gh, 1058, "stuck")
    assert not any(c[0] == "docker" for c in gh._run.calls)


# --- 6. route never names the stage that just ran as skipped ---

def _standing(stage):
    return FakeGh([{"number": 90, "labels": ["type:epic", "epic:standing"]},
                   {"number": 9, "parent": 90, "labels": ["type:task"], "stage": stage,
                    "status": "in-progress"}])


def test_advance_standing_after_a_clean_arch_review_skips_nothing(monkeypatch):
    gh = _standing("architecture")
    monkeypatch.setattr(s, "cmd_transition", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(s, "cmd_worktree_add", lambda *a, **k: {"path": "/wt/9"})

    result = s.cmd_advance_standing(gh, 9, "arch-review", "development", "review clean")

    assert result["steps"]["route"]["skipped"] == []
    [routed] = [c for c in gh.comments_on(9) if "Routed" in c]
    assert "skipping" not in routed and "`arch-review` → `development`" in routed


def test_route_from_architecture_still_names_the_skipped_review():
    gh = _standing("architecture")
    result = s.cmd_route(gh, 9, "development", "trivial design")
    assert result["skipped"] == ["arch-review"]
    assert "skipping `arch-review`" in gh.comments_on(9)[-1]


def test_route_cli_takes_the_finished_stage(monkeypatch):
    gh = _standing("product")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    assert s.main(["route", "9", "--to", "architecture", "--reason", "clean",
                   "--from", "product-review"]) == 0
    assert "skipping" not in gh.comments_on(9)[-1]


# --- 7. the same unchanged sync conflict is posted once across runs ---

def _conflicted_epic(repo):
    _cut_epic(repo)
    _advance_origin(repo, "epic-9", "shared.txt", "epic side\n")
    _advance_origin(repo, "main", "shared.txt", "main side\n")


def test_unchanged_epic_conflict_is_not_reposted_by_a_new_run(repo, runs):
    gh = _epic_tree()
    _conflicted_epic(repo)

    first = s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    second = s.sync_epic_if_due(gh, 9, "run-2", str(repo))

    assert first["conflict"] and second["conflict"] and second["already_posted"] is True
    assert sum("sync-conflict: epic-9" in c for c in gh.comments_on(9)) == 1


def test_a_moved_main_reposts_the_conflict(repo, runs):
    gh = _epic_tree()
    _conflicted_epic(repo)
    s.sync_epic_if_due(gh, 9, "run-1", str(repo))
    _advance_origin(repo, "main", "shared.txt", "main side, again\n")

    moved = s.sync_epic_if_due(gh, 9, "run-1", str(repo))

    assert moved["conflict"] and "already_posted" not in moved
    assert sum("sync-conflict: epic-9" in c for c in gh.comments_on(9)) == 2


# --- 8. merged-via and the development handoff are never double-posted ---

def _finalize(gh, monkeypatch):
    monkeypatch.setattr(s, "record_terminal_unit", lambda *a, **k: None)
    monkeypatch.setattr(s, "cleanup_unit", lambda *a, **k: {"worktree": {}})
    return s._finalize_merged_pr(gh, 1982, 1975, ".", "epic-1966", [], {})


def test_merged_via_is_not_reposted_on_a_recovered_rerun(monkeypatch):
    gh = FakeGh([{"number": 1975, "state": "CLOSED", "comments": ["Merged via #1982."]}])
    assert _finalize(gh, monkeypatch)["merged"] is True
    assert gh.comments_on(1975).count("Merged via #1982.") == 1


def test_merged_via_is_posted_once_on_a_first_merge(monkeypatch):
    gh = FakeGh([{"number": 1975, "state": "CLOSED", "comments": ["Merged via #1900."]}])
    _finalize(gh, monkeypatch)
    assert gh.comments_on(1975)[-1] == "Merged via #1982."


def _handoffs(gh):
    return sum("development->pr-review" in c for c in gh.comments_on(9))


def test_repeated_handoff_is_not_reposted(monkeypatch):
    monkeypatch.setattr(s, "required_check_states", lambda gh, pr: None)
    gh = FakeGh([{"number": 9}])
    s.cmd_handoff_to_pr_review(gh, 9, 42, "10 passed.")
    again = s.cmd_handoff_to_pr_review(gh, 9, 42, "10 passed.")
    assert again["already_posted"] is True and _handoffs(gh) == 1


def test_a_rework_round_handoff_is_posted_again(monkeypatch):
    monkeypatch.setattr(s, "required_check_states", lambda gh, pr: None)
    gh = FakeGh([{"number": 9}])
    s.cmd_handoff_to_pr_review(gh, 9, 42, "10 passed.")
    s.cmd_record_pr_review(gh, 9, 42, "rework", "fix the guard")
    s.cmd_handoff_to_pr_review(gh, 9, 42, "10 passed.")
    s.cmd_handoff_to_pr_review(gh, 9, 42, "11 passed.")  # a different round's summary
    assert _handoffs(gh) == 3
