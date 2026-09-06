"""The pipeline script loads its repo config at import time. The suite is generic
and drives no real repo, so point it at the shipped neutral sample before any test
module imports sdlc_next. Real deployments resolve their own config from the repo
they run in (see `_find_config`)."""
import os
from pathlib import Path

os.environ.setdefault(
    "SDLC_CONFIG",
    str(Path(__file__).resolve().parents[2] / "sdlc.config.sample.json"))
