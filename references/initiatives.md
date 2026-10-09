# Initiatives

How an Initiative run cuts its Product-Roadmap Task and Epics, walks its Epics, and closes.

## Cutting an Initiative's Product-Roadmap Task

An Initiative never runs `product` itself. Right after the Initiative issue exists, cut
its **one** Product-Roadmap Task:

```bash
python3 "$SDLC" create-issue --parent <initiative-n> --title "Product Roadmap" \
  --body "..." --type Task
```

It runs the plain issue flow (`product` → `product-review` → Gate A) with no special
casing. When its Gate A merges, `pass-gate` closes it (no next stage is claimed), and
`next-action` on the Initiative returns `reason: "Product-Roadmap Task closed -- cut
Epics..."`.

## Cutting Epics from an approved Initiative

Do this yourself, not via a subagent: read the Product-Roadmap Task's approved
`docs/sdlc/issue-<n>/product.md` and cut it into Epics:

- **Each Epic must be independently mergeable to `main` and independently shippable**
  (into `initiative-<i>` under an initiative branch). An Epic that only makes sense once a
  sibling has merged is cut wrong.
- Each Epic's body carries a pointer to the Initiative's IRD plus its own explicit scope
  carve-out (the slice of the IRD it covers).
- **Each Epic's body carries a coarse `## Footprint`** (directory globs, backticked, one per
  bullet), estimated against the current code at cut time: until its `lld.md` exists it is
  the only footprint the overlap checks can read, and an Epic with none never launches in
  parallel (`references/cloud-mode.md`, "Initiative → cloud Epics").
- **Carve for parallel running.** Set boundaries and `blockedBy` from feature dependencies
  **and** file hotspots (a migrations index, the app module, a shared route list or test
  util): Epics that must edit the same hotspot are sequenced, or the hotspot goes to one Epic
  and the others' bodies state the constraint for their architecture/LLD. A small shared file
  whose conflict is mechanical to resolve at epic close (a registration line, a docs table)
  goes in `pipeline.placement.softOverlapPaths` instead — it does not serialise Epics.
- Create each with `create-issue --parent <initiative-n> --type Epic --priority <P>
  --effort <E>`, Priority and Effort from the approved `product.md`'s sizing
  (`references/operations.md`, "Issue taxonomy").
- **Order them from `product.md`'s wave order:** create Epics in wave order and give every
  Epic after the first its prerequisite Epic(s) as native `blockedBy` edges — at creation
  with `--blocked-by <prerequisite-epic-n>` (repeatable), or later with `add-blocked-by
  <epic-n> --on <prerequisite-n>`. Epics with no prerequisite carry no edge. The Initiative
  loop reads only these edges, never `product.md`.
- Do not cut their phase-Tasks by hand: the Initiative loop returns `cut-phase-tasks` for
  each Epic when it becomes runnable (SKILL.md, "Cutting an Epic's phase-Tasks").

## The Initiative loop

`next-action <initiative>` returns `none` only when nothing is left to do. While cut Epics
are open it walks them, lowest number first, skipping any Epic that is blocked by an open
issue or is legacy:

- **`cut-phase-tasks`** (`epic`) — the next runnable Epic has no phase-Tasks (or only one of
  the two). Run `cut-phase-tasks <epic> --repo-path <p>` (SKILL.md, "Cutting an Epic's phase-Tasks"), then call
  `next-action <initiative>` again.
- **`run-epic`** (`epic`) — run that Epic's normal SKILL.md Step 1–3 loop inline: `next-action <epic>
  --run-id "$RUN_ID" --sync-epic` and everything after it, through its `none`, `check-epics-closeable` /
  `close-epic` (when `pipeline.epicClose.auto` allows) and its Step 4 facts. Then return to
  `next-action <initiative>`. **Use the same `--run-id` for the Initiative and every Epic**:
  `parallelism.maxTasksPerRun` is shared across them, and `stop-at-cap` on either means
  finish the in-flight unit, stop and report.
- **`launch-cloud-epics`** (`epics`) — `pipeline.placement.initiativeEpics` is `cloud`: the
  Epics run in their own cloud sessions, never inline. `launch-cloud-epic <epic> --repo-path <p>`
  for each, then `next-action <initiative>` again (`references/cloud-mode.md`, "Initiative →
  cloud Epics").
- An Epic whose loop ends still open (waiting on a human gate, `needs-human`, blocked) is
  parked: call `next-action <initiative> --skip-epic <epic>` (repeatable) for the rest of the
  run so the loop moves to the next runnable Epic. Never re-descend into a parked Epic.
- **Initiative branch** (`branch: true`): call `next-action <initiative> --run-id "$RUN_ID"
  --sync-epic` — its `epic_sync` keeps `initiative-<i>` current with `main` (`unit:
  initiative`; a `conflict` → `references/epics.md`, "Initiative branch"). Each Epic still
  runs and closes as in SKILL.md, Step 1; `close-epic` merges it into `initiative-<i>` (result `base`).
- `none` names each open Epic's state in `reason` (blocked by #n, not driven, parked). When
  every cut Epic is closed it says so: "Closing an Initiative".

## Closing an Initiative

When `next-action` on the Initiative reports in a `none` `reason` that every cut Epic is
closed:

0. **Initiative branch only** (the `reason` names `open-initiative-pr`): run
   `open-initiative-pr <initiative> --repo-path <p>` (syncs `initiative-<i>` with `main`, opens
   or reuses its PR into `main`, returns the `evidence` owed). Tell the operator the PR and the
   owed suites; their final full runs land as passing checks or `record-local-ci --pr <pr>`
   attestations at the head (`closeSuites`, e.g. `e2e`). Then `merge-initiative-pr
   <initiative>`: on `merged: false` act on `reason` (`evidence`, `behind_base` → re-run
   `open-initiative-pr`, re-attest the new head) and stop until it is green — never merge the
   PR by hand. Once merged, continue with step 1 (validation runs on `main`).
1. Delegate `sdlc:initiative-close`: it starts the delivered application and validates
   it against every requirement in the Initiative's `product.md`, and always records
   `record-initiative-verification --outcome met|unmet`.
2. **All met** (`clean`) → `python3 "$SDLC" close-initiative <initiative>` (one call; it
   re-checks closeability — the initiative branch landed, if any — and the record itself).
3. **Anything not met** (`rework`) → file the gap (a Task against the relevant Epic, or
   judge it out of scope and say why) and stop; re-run from step 1 once fixed. When the
   owning Epic is already closed, file it without asking: a `Bug` under the repo's standing
   backlog Epic (`create-issue --parent <standing epic> --type Bug`) if one exists, else a
   new gap Epic under the Initiative.

No human gate here — Gate A already approved `product.md`.
