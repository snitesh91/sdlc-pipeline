# Working on this repo (the `sdlc` plugin)

This repo is the plugin itself; driven repos consume it. Layout and install: `README.md`.

## Code quality
- Comments explain only a non-obvious *why*. No narratives, dates, incidents or issue numbers in code.
- Docstrings are 1–3 lines. Design rationale goes in the commit message or `references/history.md`.
- Hooks (`hooks/*.py`) are stdlib-only, fast (<100 ms), and fail open on internal errors.

## Tests
- Every `scripts/sdlc_next.py` change ships with a test: `cd scripts && python3 -m pytest -q`.
  A bug fix gets a regression test that goes red on the pre-fix code, plus a positive control.
- Every hook change: `python3 -m pytest -q hooks/tests`.
- `.claude/settings.json` runs the matching suite on `Stop` when `scripts/` or `hooks/` is dirty.

## Docs (`skills/`, `agents/`, `references/`)
- They are LLM instructions paid on every turn: terse, imperative, one rule per sentence.
- One rule, one home. Point to it (`→ references/x.md, "Section"`); never restate it.
- Agents and SKILL.md cite files as `${CLAUDE_PLUGIN_ROOT}/references/…`; agents are `sdlc:<role>`.
- `references/history.md`: 1–2 lines per change, newest first. Only SKILL.md's reference table cites it.
