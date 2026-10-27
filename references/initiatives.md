# Initiatives

How an Initiative run cuts its Product-Roadmap Task and Epics, walks its Epics, and closes.

## Cutting an Initiative's Product-Roadmap Task

An Initiative never runs `product` itself. Right after it exists, cut its **one**
Product-Roadmap Task, unless it only groups engineering-driven Epics (then it has none):

```bash
python3 "$SDLC" create-issue --parent <initiative-n> --title "Product Roadmap" \
  --body "..." --type Task
```

It runs the plain flow (`product` → `product-review` → Gate A). When its Gate A merges,
`pass-gate` closes it, and `next-action` on the Initiative returns `reason:
"Product-Roadmap Task closed -- cut Epics..."`.

## Cutting Epics from an approved Initiative

Do this yourself, not through a subagent. Read the approved `<docRoot>/issue-<n>/product.md`
and cut it into Epics:

- **Each Epic is independently mergeable and shippable** into its base (`main`, or
  `initiative-<i>`). An Epic that needs a sibling merged first is cut wrong.
- Each Epic's body points to the Initiative's IRD and states the slice it covers.
- **Each Epic's body carries a coarse `## Footprint`** (backticked directory globs, one per
  bullet). Until `lld.md` exists it is the only footprint the overlap checks read; an Epic
  without one never launches in parallel (`references/cloud-mode.md`, "Initiative → cloud
  Epics").
- **Carve for parallel running.** Set boundaries and `blockedBy` from feature dependencies
  **and** file hotspots (a migrations index, the app module, a shared route list). Sequence
  Epics that edit the same hotspot, or give the hotspot to one Epic and state the constraint in
  the others. Put a small file with mechanical conflicts in
  `pipeline.placement.softOverlapPaths` instead.
- Create each with `create-issue --parent <initiative-n> --type Epic --priority <P> --effort
  <E>`, sized from `product.md` (`references/operations.md`, "Issue taxonomy").
- **Order them by `product.md`'s waves.** Give each later Epic its prerequisites as native
  `blockedBy` edges (`--blocked-by <n>`, repeatable, or `add-blocked-by <epic-n> --on <n>`).
  The Initiative loop reads only these edges.
- Do not cut phase-Tasks by hand. The loop returns `cut-phase-tasks` for each Epic when it
  becomes runnable.

## Amending an approved `product.md`

Any change to the Initiative's approved `product.md` is an amendment and needs operator
approval again (an in-session approval counts: `approve-gate --by-operator-session`).

- **Standalone amendment:** cut a Task under the Initiative (`create-issue --parent
  <initiative-n> --type Task`). It runs `product` → `product-review` → Gate A; `product` edits
  the approved doc in place. Name that path (`issue-<r>/product.md`) in the prompt.
- **An Epic's design needs it:** its Architecture-phase Task amends `product.md` on the same
  design PR, and `arch-review` reviews both docs. Never `skip-gate` it; open Gate B.

## The Initiative loop

`next-action <initiative>` walks open cut Epics, lowest number first, skipping blocked and
legacy ones. It returns `none` only when nothing is left.

- **`cut-phase-tasks`** (`epic`): SKILL.md, "Cutting an Epic's phase-Tasks"; then call
  `next-action <initiative>` again.
- **`run-epic`** (`epic`): run that Epic's SKILL.md Step 1–3 loop inline (`next-action <epic>
  --run-id "$RUN_ID" --sync-epic` through its `none`, `check-epics-closeable` / `close-epic`
  and its Step 4 facts), then return to `next-action <initiative>`. **Use one `--run-id` for
  the Initiative and every Epic**: `parallelism.maxTasksPerRun` is shared, and `stop-at-cap`
  on either means finish the in-flight unit, stop and report.
- **`launch-cloud-epics`** (`epics`): run `launch-cloud-epic <epic> --repo-path <p>` for each,
  then `next-action <initiative>` again (`references/cloud-mode.md`, "Initiative → cloud
  Epics").
- An Epic whose loop ends open (gate, `needs-human`, blocked) is parked: pass `--skip-epic
  <epic>` (repeatable) to `next-action <initiative>` for the rest of the run. Never re-enter a
  parked Epic.
- **Initiative branch** (`branch: true`): call `next-action <initiative> --run-id "$RUN_ID"
  --sync-epic`; its `epic_sync` keeps `initiative-<i>` current (`unit: initiative`; on
  `conflict` → `references/epics.md`, "Initiative branch").
- `none` names each open Epic's state in `reason`. When every cut Epic is closed it says so:
  "Closing an Initiative".

## Closing an Initiative

When a `none` `reason` says every cut Epic is closed:

0. **Initiative branch only** (`reason` names `open-initiative-pr`): run `open-initiative-pr
   <initiative> --repo-path <p>`. It syncs `initiative-<i>`, opens or reuses its PR into
   `main`, and returns the `evidence` owed. Tell the operator the PR and the owed suites
   (`closeSuites`); they land as passing checks or `record-local-ci --pr <pr>` attestations.
   Then run `merge-initiative-pr <initiative>`. On `merged: false` act on `reason`
   (`behind_base` → re-run `open-initiative-pr` and re-attest) and stop until green. Never
   merge the PR by hand. Once merged, clear each `scaffolding` hit (fix it, or file it under
   the standing backlog Epic).
1. Delegate `sdlc:initiative-close`. It validates the delivered app against every requirement
   in `product.md` and records `record-initiative-verification --outcome met|unmet`.
2. **All met** (`clean`) → `python3 "$SDLC" close-initiative <initiative>`.
3. **Anything unmet** (`rework`) → file the gap (a Task against the owning Epic, or judge it
   out of scope and say why), stop, and re-run from step 1 once fixed. When the owning Epic is
   closed, file it without asking: a `Bug` under the standing backlog Epic
   (`create-issue --parent <standing epic> --type Bug`) if one exists, else a new gap Epic under
   the Initiative.

No human gate here: Gate A already approved `product.md`.
