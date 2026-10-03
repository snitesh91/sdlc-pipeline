# Cloud mode — one Epic, one side

`/sdlc:run <n>` runs on the operator's laptop or in a Claude Code cloud session
(claude.ai/code). The two never work on the same Epic: the placement label decides which
side owns it.

## Placement rules

- **The label:** `pipeline.placement.cloudLabel` (default `sdlc:cloud`).
- **An Epic is cloud-placed** when it or its parent Initiative carries the label; every other
  Epic is local. A Task belongs to its Epic; an Initiative and its Product-Roadmap Task read
  the Initiative's own label. A label on a Task is ignored.
- **The session's placement:** env `SDLC_PLACEMENT` (`cloud` | `local`; any other value is an
  error) wins; else a truthy `CLAUDE_CODE_REMOTE` (`true` / `1`) means cloud; else local.
  `show-config` → `session_placement` reports it and the signal that decided it.
- **Guarded commands:** `next-action`, `start-stage`, `list-parallel-ready`,
  `list-design-ready`, `list-ready-for-review` refuse a unit placed on the other side.
- **An Initiative run on the laptop** skips its cloud-placed Epics (`none` `reason`: "placed
  in the cloud"); a cloud-placed Initiative runs only in the cloud.

## `placement_mismatch`

A guarded command exits 1 with `{"error": "placement_mismatch", "session_placement",
"signal", "unit_placement", "epic", "hint"}`: this session is on the wrong side for `epic`.
Stop driving it, relay `hint` to the operator, and start nothing on it. Never set
`SDLC_PLACEMENT` to get past it; that override exists only for a misdetected session, and only
the operator sets it.

## Handoff with `place`

```bash
python3 "$SDLC" place <n> --where cloud|local [--force] --repo-path <p>
```

- Run on an Epic or Initiative; it adds (creating it if missing) or removes the label.
  Idempotent: an Epic already on that side returns `already: true`.
- It refuses (`refused: true`, `worktrees`) while a live local worktree holds any unit under
  `<n>` (`epic-<n>`, a child's `issue-<m>`, a detached review/dev tree): work not pushed from
  there is invisible to the other side. Push or release them, then re-run; `--force` only on
  the operator's word.
- `--where local` on an Epic whose Initiative is cloud-placed refuses (`inherited_from`):
  place the Initiative instead. An Initiative moved local lists its own cloud-labelled Epics
  in `still_cloud`.
- **Laptop → cloud:** stop the local run of `<n>`, then
  `place <n> --where cloud --repo-path <p>`, then
  `claude --cloud "/sdlc:run <n>" --environment <pipeline.placement.cloudEnvironment>`.
- **Cloud → laptop:** let the cloud run end (or stop it), then `place <n> --where local`
  from either side, then `sdlc-run <n>` on the laptop.

## A cloud run

A cloud session drives its one Epic (or Initiative) through the normal Steps 1–4 loop until
the Epic closes — through `close-epic` when `pipeline.epicClose.auto` allows — then reports
(Step 4) and ends. It parks nothing for later pickup: whatever it cannot finish (a human gate,
`needs-human`) it reports, and the run ends when `next-action` returns `none`.

## Human channel

TODO: in-session questions from a cloud run to the operator — a separate change adds them.
Until then a cloud run surfaces every operator decision through the issue thread
(`mark-needs-human`, gates) and its Step 4 report.
