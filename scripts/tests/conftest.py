"""The pipeline script loads its repo config at import time. The suite is generic
and drives no real repo, so point it at the shipped neutral sample before any test
module imports sdlc_next. Real deployments resolve their own config from the repo
they run in (see `_find_config`)."""
import os
from pathlib import Path

os.environ.setdefault(
    "SDLC_CONFIG",
    str(Path(__file__).resolve().parents[2] / "sdlc.config.sample.json"))
# Per-branch lockfiles (`branch_lock`) go to a throwaway directory, not the
# configured `<worktrees.root>/.sdlc-locks`, so the suite never touches /tmp
# state shared with a live pipeline on the same machine.
import tempfile
os.environ.setdefault("SDLC_LOCK_DIR", tempfile.mkdtemp(prefix="sdlc-test-locks-"))
# Same isolation for the run-cap state files (`maxTasksPerRun`), which otherwise
# live at `<worktrees.root>/.sdlc-runs` -- i.e. under /tmp, where a real pipeline
# run on this machine may already have some. A test must never read another
# process's run state, and `record_terminal_unit` short-circuits on an empty
# directory, so pointing this at a fresh temp dir keeps the default path honest.
os.environ.setdefault("SDLC_RUNS_DIR", tempfile.mkdtemp(prefix="sdlc-test-runs-"))
