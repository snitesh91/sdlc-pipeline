# Parallelism — running more than one unit at once

Every lane defaults to 1 (`parallelism.devLane` / `prReview` / `designLane`): one unit at
a time. Read this only when running more than one unit at once (config sets a lane > 1,
or a second invocation on another epic). Exception: "When an agent dies
mid-stage" and "Git-conflict handling" apply to every run.

The control plane decides eligibility, caps, worktrees, branch bases, locks and queue
markers. Call the commands below and act on their result fields. Never hand-track
eligibility.

## Fan-out rules

- Start every unit with `start-stage <n> --role <role>` (it creates the worktree, then
  claims — the **Ordering rule**: never `claim` before the worktree exists).
- Dispatch all selected units in **one parallel `Agent` call**, one subagent per unit,
  each told its own worktree path. Track one stage agent per active unit.
- **Hold the waiting yourself.** A subagent cannot wait across turns. When a stage is
  blocked on a long external job (image build, full suite, stack coming healthy), you run
  the poll (`run_in_background`, generous window) and resume the agent once with the
  result. Prefer the agent launching the job as a detached, daemon-managed container
  that writes its report under a bind mount, never inside an `--rm` container.
- After each subagent returns, run `transition` (SKILL.md) before its next stage.
- Zero candidates is normal. `skipped` says why a unit was excluded.
- Never run two invocations on the same epic.

## Parallel implementation lane

`list-parallel-ready <epic> --repo-path <p> --run-id <id>` → `parallel_ready`: up to
`parallelism.devLane` `development` children (a non-standing Epic's Tasks once it is
`epic:architected`, or a standing child past `architecture`). Start each with
`start-stage`. A child stuck `in-progress` with no worktree is a crashed run: route it
through `next-action`'s `resume`, not this pool.

## Design lane

Standing epics only. `list-design-ready <epic> --repo-path <p>` → `design_ready`: up to
`parallelism.designLane` children at `product`/`architecture`. It returns empty for a
non-standing Epic: that Epic's Architecture-phase and LLD-phase Tasks run one at a time
via `next-action`, never fanned out.

## Parallel PR review

1. `list-ready-for-review <epic> [--limit N]` → `ready_for_review` (up to
   `parallelism.prReview`).
2. One worktree per PR, **detached** at `origin/issue-<n>` — never a local `issue-<n>`
   branch, which `development` must stay free to hold for rework:
   ```bash
   git -C <repo-root> fetch origin
   git -C <repo-root> worktree add --detach <worktrees.root>/<reviewPrefix><n> origin/issue-<n>
   ```
3. `start-comment <n> --role pr-review` for each (skip where `transition` already posted
   it), then one parallel `Agent` call — same `sdlc:pr-review` agent and prompt as a single
   review.
4. If suites starve each other, lower `parallelism.prReview`; never make reviews shallower.
5. Remove each review worktree when its review ends, crash included:
   `git worktree remove --force <path>`.

### The two queue markers

The review queue is two script-posted comment markers. Never hand-type them.

| Marker | Posted by | When |
|---|---|---|
| `<!-- stage-transition: development->pr-review @ … -->` | `handoff-to-pr-review <n> --pr <pr> --summary "..."` | Every `development` finish, rework rounds included, after any `record-local-ci` it owes |
| `<!-- pr-review-outcome: clean\|rework:<pr> @ … -->` | `record-pr-review <n> --pr <pr> --outcome clean\|rework --summary "..."` | `pr-review`'s last action, clean included, before `merge-pr` or resuming `development` |

`handoff_marker_present: false` from `transition`/`verify-exit` → resume `development` to
post the missing marker before any review.

## Rework stays one thread per issue

A finding resumes that issue's own tracked `development` agent in that issue's worktree.
Never start a second development thread for the same issue; further findings queue
behind it. Different issues may each have their own thread. A context-reset replacement
is still one thread: `TaskStop` the incumbent first; the replacement takes over the same
worktree.

## Resource cap — serialize suite-heavy stages

The caps bound agents, not suite runs; the machine's memory/CPU is the real limit.

- Run **at most one suite-heavy stage at a time** machine-wide (a `development` or
  `pr-review` running the integration or e2e suite), whatever the cap. A light stage
  (`lld`, a design review not running suites) may run alongside. Hold the second
  suite-heavy stage until the first finishes.
- A Task whose acceptance criterion is a wall-clock budget holds the Docker VM alone for
  that measurement: quiesce every other Docker-using stage on the machine first, light
  ones included, and hold them until it finishes.
- Tell suite-running agents: a suite too big to run single-process runs in memory-scoped
  batches that partition the whole tree (record which batch covered which directories);
  run the integration suite backgrounded/chunked, never one blocking foreground call.
- A resource kill is never a code finding: name which suites did and did not run; never
  report an unrun suite as passing.
- If runs report starvation rather than defects, lower the caps; never make reviews
  shallower.
- The committed e2e harness config is presumed working: an e2e failure is a finding, not
  a broken suite to route around. Before changing how the test container reaches the
  app, show the change passes where the committed config passes. If e2e cannot run, name
  the unproven surfaces.

## When an agent dies mid-stage

1. **Inspect the worktree yourself** — commits ahead of `origin`, `git status
   --porcelain`, whether a PR exists. Don't ask the agent.
2. **Resume the same agent** with `SendMessage`. Tell it what survived (uncommitted,
   unpushed, untracked) and: **treat nothing as verified; re-run every check.**
3. **Push rejected → the agent stops and reports.** Overwriting a branch is the
   operator's call, after verifying it is a strict content-superset of what it overwrites.

When dispatching into an unstable session, tell the agent: **commit and push after each
self-contained step; never batch to the end**; prefer several small `Edit`s over one
whole-file rewrite; do the cheapest, most certain work first.

**Stopping a fan-out parent does not stop its children.** After `TaskStop`, its axis
subagents keep running and report to you later under their own task ids. Salvage what
arrives: post verified parts to the issue as evidence, not a verdict (the review still
stands at zero rounds), and tell the next round not to re-derive them. After any kill,
sweep what the stage could have touched (cloud resources, worktrees, temporary
containers) before calling the lane quiet. Prefer letting a review finish when the
difference is minutes.

## Git-conflict handling

- `sync-branch`/`transition` returns `conflict: true` + `conflicting_files` → rework:
  resume that child's own `development` agent with the files, it resolves in its own
  worktree, re-run `sync-branch`. Escalation pairing `sync-branch-conflict` ↔
  `development`, counted by `pairing-counts <n>` (→ `references/rework.md`).
- `base_missing: true` → nothing to reconcile yet; proceed.
- **Merge-time freshness gate:** `merge-pr` returns `merged: false` + `behind_base` →
  `sync-branch`, wait for the new head's required-workflow checks (a suite with no PR-level
  CI: resume `development` to re-run and `record-local-ci` the new head), re-run `merge-pr`.
  `carried_attestation_forward: true` means it merged; nothing to do.
- `merge-pr` fails because GitHub reports the PR non-mergeable → treat as a sync
  conflict (resume `development`); never blindly retry.
- `finish-lld` / `create-lld-tasks` returns `conflict` → the numbered doc is not on origin;
  re-run. `merge-design-pr` / `skip-gate` / `waive-gate` return `behind_base` → `sync-branch <n>`
  (merges `origin/epic-<e>` into the phase-Task's branch), then re-run. `review_stale` → the
  design PR's doc changed after its review: re-run the review and `record-design-review`.
  `merge-gate` returns `behind_base` the same way.
- An `epic-<n>` ← `main` conflict (`close-epic`, `sync-branch --unit epic`) has no stage
  agent: resolve it yourself in the epic worktree, or dispatch a `development` agent for
  that one reconcile — never a child's tracked agent. A clean textual merge is not enough:
  re-check what no file conflict shows (a count or allow-list both sides pinned, specs
  moved on one side, a footprint the merge widened).

## Working on a branch

- Every unit — child, phase-Task, epic branch — works in its own worktree from
  `start-stage`/`worktree-add`. **Resume base:** these resume from `origin/<branch>`
  when it exists; never recreate a unit's worktree off `main` or the epic branch by hand.
- `worktree-add` result `diverged: true` → unpushed local commits; reconcile
  (`sync-branch`, or push/discard them) and resume. Never `git reset --hard`.
- Nobody commits to `epic-<n>` by hand and no stage agent works in it; the control
  plane writes it (design-PR and Task-PR merges, `create-lld-tasks`, `close-epic`).
- Every stage pushes its commits to `origin/<branch>` as it goes; never leave commits
  unpushed between stages.
- **Keeping a branch current:** `transition` runs `sync-branch` before every stage
  (skip it only right after `pass-gate`/`skip-gate`). Never run `sync-branch` while an
  agent of the same unit is live — it moves that worktree under the agent — and the guard
  denies every stage agent the command: a live agent that finds itself behind stops with
  `blocked` naming `sync-branch <n>` (never a hand `git merge origin/<base>`); you sync,
  then resume it on the new head.
- The gitignored `.env.<profile>` / `.secrets.<profile>` live only in the main checkout.
  For a stage that runs e2e or a live-credential check, symlink them into its worktree
  (`ln -s <repo-root>/.secrets.<profile> <worktree>/`) or name their main-checkout path
  in the prompt; otherwise it wrongly concludes no credentials exist.
- `mark-needs-human`, `mark-blocked` and `merge-pr` release the unit's worktree
  themselves (`worktree` key). Remove by hand only review worktrees and leftovers when
  an invocation ends.

## Concurrent multi-epic isolation

- N invocations on N **different** epics may run at once, one session each.
- The main checkout stays on `main`; never check out a pipeline branch there. A command
  refusing because it is → follow the recovery in the error. If the branch's `/tmp`
  worktree was left on a detached HEAD: commit WIP there, confirm with `git merge-base
  --is-ancestor` that it descends from the branch tip, then `git checkout -B <branch>`
  in that worktree and push.
- Config is read from the command's own checkout: `record-local-ci` in a child worktree
  sees the epic branch's config, `merge-pr`/`close-epic` in the main checkout see
  `main`'s. Land config changes on `main` (their own PR) so the gate enforces what the
  branches attest against.
- Caps and the one-suite-heavy-stage rule are machine-wide, but nothing enforces the
  latter across invocations; keep it by hand. (Cross-epic coordination file: DEFERRED,
  not built.)

### Per-epic isolated stack

When `pipeline.stack.enabled`: `provision-epic-stack <epic>` at the epic's first touch,
`teardown-epic-stack <epic>` after `close-epic`'s merge. Give every e2e-running stage the
returned `use` line and ports, never the shared dev stack's. A child validating its own
e2e fixes builds from its own worktree with the epic profile; the epic-close run builds
from `epic-<n>`. One stack per epic: its children take turns (suite-heavy rule). With the
stack disabled, an e2e stage that needs a build of its own branch stands one up on
remapped ports — never the shared dev stack, which serves `main`.
