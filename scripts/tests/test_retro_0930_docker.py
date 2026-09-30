"""Retro 2026-09-30: a unit's docker resources (containers, volumes, networks named with
its worktree basename) go with the unit, and a worktree a container left root-owned files
in is emptied through docker before removal."""
import os
import subprocess
from pathlib import Path

import pytest

import sdlc_next as s
from sdlc_next import GhError, GitHub
from tests.test_retro_cleanup import _merged_pr, _task
from tests.test_v2_phase_tasks import _git, _worktree_with_doc, repo  # noqa: F401

VERSION = ("docker", "version", "--format", "{{.Server.Version}}")
LABEL = '{{.Label "com.docker.compose.project"}}'
PS = ("docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t" + LABEL)
VOLUMES = ("docker", "volume", "ls", "--format", "{{.Name}}\t" + LABEL)
NETWORKS = ("docker", "network", "ls", "--format", "{{.Name}}\t" + LABEL)
RM_EMPTY = "rm -rf /w/* /w/.[!.]*"


class Runner:
    """Canned stdout per exact argv; `failures` maps argv -> GhError text; `real_git`
    sends unscripted git calls to the real runner; any other unscripted call is an
    AssertionError (so an unexpected `docker rm` fails the test)."""

    def __init__(self, responses=None, failures=None, real_git=False, on_docker_run=None):
        self.responses = dict(responses or {})
        self.failures = dict(failures or {})
        self.real_git = real_git
        self.on_docker_run = on_docker_run
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        key = tuple(argv)
        if key in self.failures:
            raise GhError(f"command failed (1): {' '.join(argv)}\n{self.failures[key]}")
        if key in self.responses:
            return self.responses[key]
        if argv[:2] == ["docker", "run"] and self.on_docker_run:
            return self.on_docker_run(argv)
        if argv[0] == "git" and self.real_git:
            return s._default_runner(argv)
        raise AssertionError(f"unexpected invocation: {argv}")

    def docker_calls(self):
        return [c for c in self.calls if c[0] == "docker"]


@pytest.fixture
def docker_on(monkeypatch):
    monkeypatch.setitem(s.PIPELINE["worktrees"], "dockerCleanup", True)


def _listing(prefix="sdlc-dev-12"):
    """A host holding unit 12's stack alongside look-alikes that must survive."""
    return {
        VERSION: "29.2.0\n",
        PS: (f"c1\t{prefix}-app-1\t{prefix}\n"          # compose container, by name
             f"c2\tsdlc-dev-123-app-1\tsdlc-dev-123\n"   # 12 is a prefix of 123: untouched
             f"c3\tpg-scratch\t{prefix}\n"               # renamed, but its project label matches
             f"c4\tother-12_db\t\n"),
        VOLUMES: f"{prefix}_db\t{prefix}\nsdlc-dev-123_db\tsdlc-dev-123\nother-12_db\t\n",
        NETWORKS: f"{prefix}_default\t{prefix}\nbridge\t\nsdlc-dev-12x\t\n",
    }


_REMOVALS = {("docker", "rm", "-f", "-v", "c1"): "", ("docker", "rm", "-f", "-v", "c3"): "",
             ("docker", "volume", "rm", "-f", "sdlc-dev-12_db"): "",
             ("docker", "network", "rm", "sdlc-dev-12_default"): ""}

_NO_WORKTREE_12 = {("git", "-C", ".", "worktree", "list", "--porcelain"):
                   "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
                   ("git", "-C", ".", "ls-remote", "origin", "refs/heads/issue-12"): "",
                   ("git", "-C", ".", "branch", "--list", "issue-12"): "",
                   ("git", "-C", ".", "worktree", "prune"): ""}


def test_unit_docker_prefix_is_the_unit_worktree_basename():
    assert s.unit_docker_prefix(1467) == "sdlc-dev-1467"
    assert s.unit_docker_prefix(1467) == os.path.basename(s.worktree_path("issue", 1467))
    assert s.unit_docker_prefix(88, "epic") == "sdlc-epic-88"


def test_cleanup_unit_removes_the_units_containers_volumes_and_networks(docker_on):
    runner = Runner({**_listing(), **_REMOVALS, **_NO_WORKTREE_12})

    out = s.cleanup_unit(GitHub(runner=runner), 12, "issue", ".", runner)

    assert out["docker"] == {"prefix": "sdlc-dev-12",
                             "containers_removed": ["pg-scratch", "sdlc-dev-12-app-1"],
                             "volumes_removed": ["sdlc-dev-12_db"],
                             "networks_removed": ["sdlc-dev-12_default"], "errors": []}
    removals = [c for c in runner.docker_calls() if "rm" in c]
    assert sorted(map(tuple, removals)) == sorted(_REMOVALS)  # 123 / other-12 / 12x untouched
    # Containers before volumes before networks, all before any worktree is touched.
    first_git = next(i for i, c in enumerate(runner.calls) if c[0] == "git")
    assert max(i for i, c in enumerate(runner.calls) if c[0] == "docker") < first_git
    assert [c[1] for c in removals] == ["rm", "rm", "volume", "network"]


def test_cleanup_unit_sweeps_an_epic_by_its_own_prefix(docker_on):
    runner = Runner({VERSION: "", PS: "e1\tsdlc-epic-9_db-1\tsdlc-epic-9\n",
                     VOLUMES: "sdlc-epic-9_pg\t\nsdlc-epic9_pg\tsdlc-epic9\n", NETWORKS: "",
                     ("docker", "rm", "-f", "-v", "e1"): "",
                     ("docker", "volume", "rm", "-f", "sdlc-epic-9_pg"): ""})
    out = s.docker_sweep(s.unit_docker_prefix(9, "epic"), runner)
    # `sdlc-epic9` is the epic runtime stack's compose project: its own teardown owns it.
    assert out["containers_removed"] == ["sdlc-epic-9_db-1"]
    assert out["volumes_removed"] == ["sdlc-epic-9_pg"]


def test_docker_unavailable_is_skipped_and_cleanup_still_succeeds(docker_on):
    runner = Runner(_NO_WORKTREE_12, failures={VERSION: "Cannot connect to the Docker daemon"})

    out = s.cleanup_unit(GitHub(runner=runner), 12, "issue", ".", runner)

    assert out["docker"]["skipped"].startswith("docker unavailable")
    assert runner.docker_calls() == [list(VERSION)]
    assert out["worktree_pruned"] is True and out["worktree"]["reason"] == "no worktree"


def test_docker_binary_missing_is_skipped_not_raised(docker_on):
    def runner(argv):
        raise FileNotFoundError(2, "No such file or directory", "docker")
    assert "skipped" in s.docker_sweep("sdlc-dev-12", runner)


def test_a_failed_removal_is_reported_and_the_rest_still_go(docker_on):
    runner = Runner({**_listing(), **_REMOVALS},
                    failures={("docker", "volume", "rm", "-f", "sdlc-dev-12_db"): "volume in use"})
    out = s.docker_sweep("sdlc-dev-12", runner)
    assert out["volumes_removed"] == [] and out["networks_removed"] == ["sdlc-dev-12_default"]
    assert len(out["errors"]) == 1 and "sdlc-dev-12_db" in out["errors"][0]


def test_toggle_off_makes_no_docker_calls():
    assert s.PIPELINE["worktrees"]["dockerCleanup"] is False  # conftest default for tests
    runner = Runner(_NO_WORKTREE_12)
    out = s.cleanup_unit(GitHub(runner=runner), 12, "issue", ".", runner)
    assert "docker" not in out and runner.docker_calls() == []


def test_shipped_default_is_on():
    assert s._PIPELINE_DEFAULTS["worktrees"]["dockerCleanup"] is True


def test_dry_run_lists_and_removes_nothing(docker_on):
    runner = Runner({**_listing(), **_NO_WORKTREE_12,
                     ("git", "-C", ".", "rev-parse", "--verify", "--quiet",
                      "refs/heads/issue-12"): ""})
    out = s.cleanup_unit(GitHub(runner=runner), 12, "issue", ".", runner, dry_run=True)
    assert out["docker"]["would_remove"] == {
        "containers": ["pg-scratch", "sdlc-dev-12-app-1"], "volumes": ["sdlc-dev-12_db"],
        "networks": ["sdlc-dev-12_default"]}
    assert not [c for c in runner.docker_calls() if "rm" in c]


# --- worktree remove blocked by root-owned files ----------------------------------------

def _blocked_release(path, extra=None, failures=None):
    """release_worktree of issue-12 at `path`, whose first `worktree remove` hits EACCES."""
    remove = ("git", "-C", "/repo", "worktree", "remove", "--force", path)
    responses = {
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            f"worktree /repo\nHEAD a\nbranch refs/heads/main\n\n"
            f"worktree {path}\nHEAD b\nbranch refs/heads/issue-12\n",
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", path, "status", "--porcelain"): "",
        ("git", "-C", path, "log", "--oneline", "origin/issue-12..issue-12"): "",
        **(extra or {})}

    class FirstRemoveFails(Runner):
        removes = 0

        def __call__(self, argv):
            if tuple(argv) == remove:
                self.removes += 1
                if self.removes == 1:
                    self.calls.append(list(argv))
                    raise GhError("command failed (255): git worktree remove\nerror: failed "
                                  f"to delete '{path}/.docker/pg': Permission denied")
            return super().__call__(argv)

    return FirstRemoveFails({**responses, remove: ""}, failures=failures)


def test_root_owned_worktree_is_emptied_through_docker_then_removed(docker_on):
    path = os.path.join(s.PIPELINE["worktrees"]["root"], "sdlc-dev-12")
    real = os.path.realpath(path)
    rm = ("docker", "run", "--rm", "-v", f"{real}:/w", "alpine", "sh", "-c", RM_EMPTY)
    runner = _blocked_release(path, {VERSION: "", rm: "",
                                     ("git", "-C", "/repo", "worktree", "prune"): ""})

    result = s.release_worktree("issue-12", runner=runner, base_repo="/repo")

    assert result == {"released": True, "path": path, "forced_docker_rm": True}
    assert list(rm) in runner.calls
    tail = runner.calls[runner.calls.index(list(rm)):]
    assert tail[1:] == [["git", "-C", "/repo", "worktree", "remove", "--force", path],
                        ["git", "-C", "/repo", "worktree", "prune"]]


def test_docker_rm_retry_is_refused_outside_the_worktrees_root(docker_on, tmp_path, monkeypatch):
    # Pin the root: the default `/tmp` contains pytest's tmp_path on Linux runners.
    monkeypatch.setitem(s.PIPELINE["worktrees"], "root", str(tmp_path / "root"))
    path = str(tmp_path / "elsewhere" / "sdlc-dev-12")
    runner = _blocked_release(path, {VERSION: ""})

    result = s.release_worktree("issue-12", runner=runner, base_repo="/repo")

    assert result["released"] is False and "outside the worktrees root" in result["reason"]
    assert not [c for c in runner.calls if c[:2] == ["docker", "run"]]


def test_docker_rm_retry_is_off_with_the_toggle():
    path = os.path.join(s.PIPELINE["worktrees"]["root"], "sdlc-dev-12")
    runner = _blocked_release(path)
    result = s.release_worktree("issue-12", runner=runner, base_repo="/repo")
    assert result["released"] is False and "Permission denied" in result["reason"]
    assert runner.docker_calls() == []


def test_docker_rm_retry_is_not_attempted_for_a_non_permission_failure(docker_on):
    path = os.path.join(s.PIPELINE["worktrees"]["root"], "sdlc-dev-12")
    remove = ("git", "-C", "/repo", "worktree", "remove", "--force", path)
    runner = _blocked_release(path, failures={remove: "fatal: is locked"})
    runner.removes = 1  # skip the scripted EACCES; the lock failure is the only one
    result = s.release_worktree("issue-12", runner=runner, base_repo="/repo")
    assert result["released"] is False and "is locked" in result["reason"]
    assert runner.docker_calls() == []


# --- real git: prune-stale and a genuinely unremovable tree ----------------------------

def _with_stack(prefix):
    return {VERSION: "", PS: f"c1\t{prefix}-app-1\t{prefix}\n",
            VOLUMES: f"{prefix}_db\t{prefix}\n{prefix}3_db\t\n", NETWORKS: "",
            ("docker", "rm", "-f", "-v", "c1"): "",
            ("docker", "volume", "rm", "-f", f"{prefix}_db"): ""}


def test_prune_stale_dry_run_lists_then_the_real_run_removes(repo, docker_on):
    gh = _task(5)
    gh.repo = str(repo)
    runner = Runner(_with_stack("sdlc-dev-5"), real_git=True)
    gh._run = runner
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    _merged_pr(gh)
    gh.issues[5]["state"] = "CLOSED"

    dry = s.cmd_prune_stale(gh, str(repo), runner=runner, dry_run=True)

    assert dry["units"][0]["docker"]["would_remove"] == {
        "containers": ["sdlc-dev-5-app-1"], "volumes": ["sdlc-dev-5_db"], "networks": []}
    assert not [c for c in runner.docker_calls() if "rm" in c] and Path(wt).exists()

    result = s.cmd_prune_stale(gh, str(repo), runner=runner)

    docker = result["units"][0]["docker"]
    assert docker["containers_removed"] == ["sdlc-dev-5-app-1"]
    assert docker["volumes_removed"] == ["sdlc-dev-5_db"]      # sdlc-dev-53_db kept
    assert not Path(wt).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits that block removal")
def test_real_worktree_blocked_by_unwritable_files_is_released(repo, docker_on):
    gh = _task(5)
    gh.repo = str(repo)
    wt = _worktree_with_doc(gh, repo, 5, "src/a.txt", "a\n")
    locked = Path(wt, ".docker", "pg")      # stands in for a root-owned postgres data dir
    locked.mkdir(parents=True)
    (locked / "PG_VERSION").write_text("16\n")
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as f:  # a clean tree
        f.write(".docker/\n")
    locked.chmod(0o555)

    def emulate_alpine(argv):  # what the container does, as root
        host = argv[argv.index("-v") + 1].rsplit(":/w", 1)[0]
        locked.chmod(0o755)
        subprocess.run(["sh", "-c", RM_EMPTY.replace("/w", host)], check=True)
        return ""

    runner = Runner({VERSION: ""}, real_git=True, on_docker_run=emulate_alpine)
    try:
        result = s.release_worktree("issue-5", runner=runner, base_repo=str(repo))
    finally:
        if locked.exists():
            locked.chmod(0o755)

    assert result["released"] is True and result["forced_docker_rm"] is True
    assert not Path(wt).exists()
    assert wt not in _git("worktree", "list", cwd=repo)
