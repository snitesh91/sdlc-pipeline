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
