"""Point sdlc_next at the shipped sample config (it loads config at import time)
and isolate its lock/run-state dirs from any live pipeline on this machine."""
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
# Importable as `sdlc_next` / `tests.*` whether pytest runs from scripts/ or the repo root.
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("SDLC_CONFIG", str(SCRIPTS.parent / "sdlc.config.sample.json"))
# Branch lockfiles in a throwaway dir, never the /tmp locks a live pipeline uses.
os.environ.setdefault("SDLC_LOCK_DIR", tempfile.mkdtemp(prefix="sdlc-test-locks-"))
# Run-cap state in a fresh dir so no test reads another process's run state.
os.environ.setdefault("SDLC_RUNS_DIR", tempfile.mkdtemp(prefix="sdlc-test-runs-"))
# Run state records the calling Claude session; tests run outside one.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
# Every test runs as a local session unless it sets a placement itself.
os.environ.pop("SDLC_PLACEMENT", None)
os.environ.pop("CLAUDE_CODE_REMOTE", None)
os.environ.pop("SDLC_GITHUB_API", None)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_host_docker(monkeypatch):
    """Unit cleanup sweeps docker by default; no test may reach the host's docker (some run
    real runners), so it is off unless a test turns `dockerCleanup` back on with a fake."""
    import sdlc_next
    monkeypatch.setitem(sdlc_next.PIPELINE["worktrees"], "dockerCleanup", False)


@pytest.fixture(autouse=True)
def _fresh_doc_roots_cache():
    """Per-product doc roots are cached per process; no test may see another's."""
    import sdlc_next
    sdlc_next._DOC_ROOTS_CACHE.clear()
    yield
    sdlc_next._DOC_ROOTS_CACHE.clear()
