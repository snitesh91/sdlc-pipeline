# Parallelism — running more than one unit at once

Every lane defaults to 1 (`parallelism.devLane` / `prReview` / `designLane`). Read this file
when a lane is above 1 or a second invocation runs on another Epic. "When an agent dies
mid-stage", "Git-conflict handling", "Working on a branch" and "Development fan-out from
briefs" apply to every run.

The control plane decides eligibility, caps, worktrees, branch bases, locks and queue
markers. Act on its result fields. Never track eligibility by hand.

## Fan-out rules

- Start every unit with `start-stage <n> --role <role>`, never a bare `claim`.
- Dispatch all selected units in one parallel `Agent` call, one subagent per unit, each with
  its own worktree path.
- **Hold a wait that outlives the agent's turn.** When an agent hands back while a long
  external job still runs, you poll it (`run_in_background`) and resume the agent once with
  the result. Prefer jobs in detached containers that write reports to a bind mount, never
  inside an `--rm` container.
- Run `transition` after each subagent returns, before its next stage.
- Zero candidates is normal. `skipped` says why.
- Never run two invocations on the same Epic.

## Development fan-out from briefs

`development` hands back `blocked` with `"fanout": "unavailable", "slices": [...]` when it
has no `Agent` tool. Then:

1. Read the briefs at `<worktree>/.sdlc-scratch/dev-<n>/briefs/<slice>.md`. Confirm the file
   sets do not overlap; on overlap, resume `development` to fix the briefs.
2. Launch one `sdlc:dev-worker` per slice, at most `pipeline.fanout.development.maxChildren`
   at once, in one parallel `Agent` call. Each prompt: `AXIS: <slice>`, the worktree path,
   the brief path.
3. When all return, resume the `development` agent with each worker's handback. It
   integrates, runs the gates, commits and hands off.

## Parallel implementation lane

`list-parallel-ready <epic> --repo-path <p> --run-id <id>` → `parallel_ready`: up to
`devLane` `development` children. Start each with `start-stage`. A child `in-progress` with no
worktree is a crashed run: route it through `next-action`'s `resume`.

A dev-lane slot is held by an open, started child (live worktree, Pipeline Status
`in-progress`, or Stage `pr-review`) that is not parked or blocked. `next-action` and
`list-parallel-ready` share the cap and report `slots: {cap, occupied, free,
excluded_other_epic}`. A fresh unit needs a free slot and a `## Footprint` disjoint from each
holder's. `next-action` lists held-back units under `dev_lane.deferred`. Other Epics'
worktrees hold no slot; `parallelism.crossEpicFootprintCheck: true` adds them to the overlap
check.

## Design lane

Standing Epics only. `list-design-ready <epic> --repo-path <p>` → `design_ready`: up to
`designLane` children at `product`/`architecture`. A non-standing Epic's phase-Tasks run one
at a time.

## Parallel PR review

1. `list-ready-for-review <epic> [--limit N]` → `ready_for_review` (up to `prReview`).
2. `transition <n> --expect-stage pr-review --pr <pr> --repo-path <dev worktree>` for each.
   Proceed only on `ready: true`. Then one parallel `Agent` call. Each reviewer makes its own
   detached worktree with `review-worktree-add` and releases it with
   `release-review-worktree`.
3. If suites starve each other, lower `prReview`. Never make reviews shallower.
4. After a reviewer crash, run `release-review-worktree <n>` yourself.

### The two queue markers

Scripts post both markers. Never hand-type them.

| Marker | Posted by | When |
|---|---|---|
| `<!-- stage-transition: development->pr-review @ … -->` | `handoff-to-pr-review` | Each `development` finish, after its `record-local-ci` |
| `<!-- pr-review-outcome: clean\|rework:<pr> @ … -->` | `record-pr-review` | Each `pr-review` verdict |

`handoff_marker_present: false` → resume `development` to post the marker.

## Rework stays one thread per issue

A finding resumes the issue's own `development` agent in its worktree. Never start a second
thread for the same issue. A context-reset replacement is still one thread: `TaskStop` the
incumbent first.

## Resource cap — serialize suite-heavy stages

- **Preflight the machine before a stack-building stage** (a `development`/`pr-review` that
  needs a stack, the e2e-test Task, the exploratory pass, `initiative-close`): check `docker
  system df`, VM disk (< ~80%) and free memory. Prune the build cache yourself (never a stage
  agent) and stop stale stacks. A run with crash-class failures (`Page crashed`, `ENOSPC`,
  OOM) is an environment failure: fix and re-run, never record it.
- Run at most one suite-heavy stage at a time per machine. Light stages may run alongside.
- A wall-clock-budget criterion holds the Docker VM alone: quiesce every other Docker stage.
- A whole suite too big for one process runs in memory-scoped batches that partition the
  tree, backgrounded. Record which batch covered what.
- A resource kill is never a code finding. Name the suites that did not run.
- The committed e2e harness config is presumed working. Before you change how the test
  container reaches the app, show the change passes where the committed config passes.

## When an agent dies mid-stage

1. Inspect the worktree yourself: commits ahead of `origin`, `git status --porcelain`, the PR.
2. Resume the same agent. Tell it what survived and to re-run every check the stage owes.
3. A rejected push: the agent stops. Overwriting a branch is the operator's call.

In an unstable session, say so in the prompt. The agent then pushes after each green commit
while no PR exists, prefers small `Edit`s, and does the most certain work first.

**Stopping a fan-out parent does not stop its children.** Salvage what they report as
evidence, not a verdict. After any kill, sweep what the stage could touch (worktrees,
containers, cloud resources).

## Git-conflict handling

- `conflict: true` + `conflicting_files` (from `sync-branch`/`transition`/`prepare-rework`) →
  resume the child's `development` agent with the files. Then re-run `sync-branch`. This
  counts toward the `sync-branch-conflict` ↔ `development` valve.
- `base_missing: true` → nothing to reconcile.
- `merge-pr` `behind_base`, or `transition` `held` → `prepare-rework <n>`, satisfy
  `suites.needed`, re-run `merge-pr`. `carried_attestation_forward: true` means it merged.
- GitHub reports the PR non-mergeable → treat as a sync conflict. Never retry blindly.
- `finish-lld` / `create-lld-tasks` `conflict` → re-run. `merge-design-pr` / `skip-gate` /
  `waive-gate` / `merge-gate` `behind_base` → `sync-branch <n>`, then re-run. `review_stale`
  → re-run the review and `record-design-review`.
- An `epic-<n>` ← `main` conflict has no stage agent. Resolve it in the epic worktree, or
  dispatch one `development` agent for that reconcile. Re-check what no file conflict shows
  (pinned counts, moved specs, widened footprints). It runs only the specs the resolution
  touched.

## Working on a branch

- Each unit works in its own worktree from `start-stage`/`worktree-add`, which resume from
  `origin/<branch>`. Never recreate a worktree by hand.
- `diverged: true` → reconcile unpushed commits. Never `git reset --hard`.
- Nobody commits to `epic-<n>` by hand.
- Push commits to `origin/<branch>` as you go.
- `transition` runs `sync-branch` before each stage. Never run `sync-branch` under a live agent
  of the same unit. An agent that finds itself behind stops with `blocked`; you run
  `prepare-rework <n>` and resume it.
- `.env.<profile>` / `.secrets.<profile>` live only in the main checkout. For an e2e or
  live-credential stage, symlink them into the worktree or name their path in the prompt.
- `mark-needs-human`, `mark-blocked`, `merge-pr` and `close-issue` release the worktree.
  `prune-stale` cleans leftovers.

## Concurrent multi-epic isolation

- N invocations on N different Epics may run at once, one session each.
- The main checkout stays on `main`. A worktree left on a detached HEAD: commit the WIP,
  confirm with `git merge-base --is-ancestor`, then `git checkout -B <branch>` there and push.
- Config is read from the command's own checkout. Land config changes on `main`.
- The one-suite-heavy-stage rule is machine-wide and not enforced across invocations.

### Per-epic isolated stack

When `pipeline.stack.enabled`: `provision-epic-stack <epic>` at the Epic's first touch;
`close-epic` tears it down. Give each e2e stage the returned `use` line and ports. Children
take turns on the one stack. With the stack disabled, an e2e stage stands up its own stack on
remapped ports, never the shared dev stack.
