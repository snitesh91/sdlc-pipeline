"""SessionEnd: a metrics record for the main thread (the orchestrator) of a session in an sdlc repo."""
import os

import _metrics
from _common import load_json, read_input, repo_config, run, run_states


def main() -> int:
    data = read_input()
    config_path = repo_config(data.get("cwd") or os.getcwd())
    transcript = data.get("transcript_path")
    if not config_path or not transcript:
        return 0
    config, session = load_json(config_path), str(data.get("session_id") or "")
    _metrics.append(_metrics.main_record(transcript, repo=config.get("repo") or "unknown",
                                         session_id=session,
                                         states=run_states(config, session)))
    return 0


if __name__ == "__main__":
    run(main)
