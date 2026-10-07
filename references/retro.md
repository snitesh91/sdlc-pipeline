# Retrospective (only when the operator asks)

Never decide on your own that a retro is due. Between retros, park each friction finding as
you see it — bouncing pairings (`pairing-counts`), docs too thin for the next stage, dead
references, gates too strict or loose:

```bash
python3 "$SDLC" park-finding --key <stable-slug> --text "<finding>" \
    [--evidence <url>]... [--target <plugin file>]
```

It comments on `pipeline.retro.parkIssue` (plugin version recorded); a repeated `--key`
posts a short "seen again" instead. Unset `parkIssue` → tell the operator; never open
issues for findings yourself.

When the operator runs the retro: read every comment on the park issue, and sweep recently
merged units' handoff comments and docs for more. Fixes go to the **plugin repo**
(`snitesh91/sdlc-pipeline`); **this file and its references are the primary fix target.**
Present findings in chat and ask before editing. Once approved:

1. In a clone of the plugin repo: `git checkout -B retro/<date> origin/main`, edit
   `skills/run/SKILL.md` / `references/*` / `agents/*` / `hooks/*`, bump
   `.claude-plugin/plugin.json` `version`, add a 1–2 line entry to `references/history.md`,
   commit, push, merge to `main` (PR, or fast-forward if the operator says so), and tag it.
   Before tagging, `(cd scripts && SDLC_RELEASE_TAG=<tag> python3 -m pytest -q -k release)`
   must pass (manifest version = tag). An unpushed edit is a failed retro. **A change to
   `scripts/` or `hooks/` must pass `(cd scripts && python3 -m pytest -q)` and
   `python3 -m pytest -q hooks/tests` before pushing.** A control-plane fix also gets a regression test plus a positive
   control; run both against the pre-fix `sdlc_next.py` — the regression must go red,
   the control must not.
2. Reply on the park issue to each finding (`comment <park> --body`, linking it): `fixed
   (<tag>)`, `deferred` or `rejected`, with one line why.
3. Open the next park issue, `sdlc retro backlog (since <tag>)`, labelled `sdlc:retro`, its
   body listing every deferred finding (with links).
4. In the driven repo: bump the plugin pin (the marketplace `ref` in
   `.claude/settings.json`, and the `SDLC_PIPELINE_REF` Actions variable) to the new tag
   and set `pipeline.retro.parkIssue` to the new issue, in one commit naming the
   retrospective. Once it is merged: `supersede-park-issue <old> --by <new>`.

How to apply the bump locally, when it takes effect, and why it waits for a quiet repo:
`references/operations.md`, "Plugin pin".
