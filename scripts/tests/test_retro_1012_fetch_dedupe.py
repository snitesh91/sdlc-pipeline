"""`_fetch_origin`: one `git fetch origin` per clone per process until something may have
moved origin (a push, a refspec fetch, a GitHub write)."""
import subprocess

import pytest

import sdlc_next as s


@pytest.fixture
def clone(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_FETCH_DEDUPE", "on")
    s._FETCHED.clear()
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    fetches = []
    real = subprocess.run

    def run(argv, *a, **k):
        if "fetch" in argv:
            fetches.append(argv)
        return real(argv, *a, **k)
    monkeypatch.setattr(s.subprocess, "run", run)
    return work, fetches


def test_a_second_fetch_of_the_same_clone_is_skipped(clone):
    work, fetches = clone
    s._fetch_origin(str(work), s._default_runner)
    s._fetch_origin(str(work), s._default_runner)
    assert len(fetches) == 1


@pytest.mark.parametrize("write", [
    ["git", "-C", "x", "push", "origin", "b"],
    ["gh", "api", "-X", "PUT", "repos/o/r/pulls/1/merge"],
    ["gh", "pr", "merge", "1"],
])
def test_anything_that_may_move_origin_makes_the_next_fetch_run(clone, write):
    work, fetches = clone
    s._fetch_origin(str(work), s._default_runner)
    assert s._may_move_origin(write)
    s._PUSH_GENERATION[0] += 1  # what _default_runner does before running `write`
    s._fetch_origin(str(work), s._default_runner)
    assert len(fetches) == 2


def test_reads_never_count_as_moving_origin():
    for argv in (["gh", "api", "repos/o/r/issues/1"], ["gh", "pr", "view", "1"],
                 ["git", "-C", "x", "fetch", "origin"], ["git", "-C", "x", "rev-parse", "HEAD"]):
        assert not s._may_move_origin(argv)


def test_an_injected_runner_always_fetches(clone):
    work, _ = clone
    calls = []
    s._fetch_origin(str(work), lambda a: calls.append(a) or "")
    s._fetch_origin(str(work), lambda a: calls.append(a) or "")
    assert len(calls) == 2
