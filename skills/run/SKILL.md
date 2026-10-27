---
name: run
description: Use when the operator invokes /sdlc:run <epic> to drive a GitHub-Issues Initiative or Epic and its child issues through the auto-SDLC stages to merge, or asks to resume, unblock, retro, or continuously run that pipeline. Requires an sdlc-pipeline.config.json in the target repo.
---

# Auto SDLC over GitHub Issues

One invocation drives one Initiative's or Epic's work toward merge; an Initiative run descends
into its Epics one after another (`references/initiatives.md`, "The Initiative loop"). You are
the orchestrator: per unit, delegate one stage to one subagent, wait, verify, decide the next
step. "Parallel" means more than one child's handoff loop is live; nothing decides on its own.

- GitHub fields and comments are the **visibility log** and resume point. The committed docs
  under `<docRoot>/epic-<n>/` (an Epic's phase-Tasks) and `issue-<n>/` are the **source of record**.
- This file and its references change only through the Step 5 retro or operator feedback. A
  stage agent flags problems in its handoff instead.

## Reference files — read on demand, not upfront

All under `${CLAUDE_PLUGIN_ROOT}/references/`.

| File | Read it when |
|---|---|
| `parallelism.md` | More than one unit at once; a stack-building stage; a git conflict; an agent dying mid-stage; worktree mechanics; a `development` fan-out hand-back |
| `gates.md` | Opening, passing, skipping a gate; gate feedback; deciding a gate in session |
| `rework.md` | A review finding, a reported ambiguity, a possible valve trip, a replacement agent |
| `stage-playbooks.md` | Delegating any stage (every stage agent reads it) |
| `design-doc-rules.md` | `product.md`/`architecture.md` content; scope alignment |
| `verification-rules.md` | Writing a `development`/`pr-review` brief or a reconcile ("Test scope") |
| `review-fanout.md` | Review fan-out |
| `epics.md` | Profiles, phase-Tasks, footprints, deviation, epic close, board status |
| `operations.md` | Token/repo access, issue fields, auto-merge, local-CI attestation, auto-mode denials |
| `continuous-mode.md` | Unattended looping |
| `cloud-mode.md` | `placement_mismatch`; cloud hand-off, a cloud run, its GitHub access; `humanChannel: "session"`; `launch-cloud-epics`, `cloud-status` |
| `control-plane.md` | Any command not in the Composites table |
| `initiatives.md` | Driving an Initiative |
| `retro.md` | Parking a finding; running a retro |
| `history.md` | The incident behind a rule |

## The lifecycle model

Every phase is **its own plain child issue** (a "phase-Task"): an Initiative's `product`, an
Epic's `architecture` and `lld`. Never delegate a stage to the Initiative/Epic issue itself.

- An Initiative's Product-Roadmap Task, a standing child and a parentless issue merge
  `issue-<n>` → `main`.
- **Every child of a non-standing Epic** branches from and merges into `epic-<n>`, which reaches
  `main` once, at epic close. Under an Initiative whose label matches a
  `pipeline.initiativeProfiles` entry with `branch: true`, `epic-<n>` closes into `initiative-<i>`,
  which reaches `main` at initiative close (`references/epics.md`, "Initiative branch").

```
initiative: Product-Roadmap Task (product -> product-review -> Gate A) -> cut Epics -> per Epic: cut-phase-tasks, run-epic
epic:       Architecture-phase Task (architecture -> design PR -> arch-review -> merge: skip bar or Gate B) -> LLD-phase Task (lld -> design PR -> lld-review -> finish-lld)
task:       development -> [pr-review] -> auto-merge into epic-<n>
```

- `pipeline.classification` alone makes an issue an Epic (`references/epics.md`, "What makes an
  issue an Epic"); profiles decide how it runs ("Epic profiles").
- **Engineering-driven work**: a bare Epic with its scope in the body and no `product.md`. It
  starts at its Architecture-phase Task; `architecture` returns `needs-human` when scope is unclear.
  An Initiative may wrap such Epics for grouping only: no Product-Roadmap Task, no `product.md`;
  `architecture` reads each Epic's scope.
- **Standing profile** (`epicLevelPhase: false`): RTB tasks only, no phase-Tasks, no epic branch,
  no human gate. Each child runs only the stages it needs ("Routing a standing child") and merges
  into its integration base (`references/epics.md`, "The epic integration branch").
  **Legacy profile** (`driven: false`): skip it, Tasks included.
- **Every Epic has two standing Tasks** (Integration-test, e2e-test) that run after every
  functional Task merges, unless its Initiative's profile sets `testTasks: false`.
- **Test scope** (`references/verification-rules.md`, "Test scope"): the whole integration suite
  runs once, as the epic PR's CI into `main`. The pipeline never runs the full e2e suite. Epic
  close needs the exploratory pass (`references/epics.md`, "Epic closing").
- **`product-review` follows every `product`** unless a standing child is routed past it. Clean
  goes to Gate A (waived by `requiresHumanGateA: false` — `references/gates.md`, "Waived gates").
- **Route every `unstaged` child yourself**: `set-stage <n> --stage development` when the Epic's
  `lld.md` covers it, else an Architecture revision.
- **Nothing spins off a separate ticket.** Fix every pre-merge problem by resuming the subagent
  that owns the stage (`references/rework.md`). Only a cross-issue `blockedBy`, a `needs-human`
  verdict or an open gate pauses a unit, and none pauses the invocation.

### Initiatives

An Initiative never runs a stage. Read `references/initiatives.md` before acting on any
`next-action <initiative>` result.

### Cutting an Epic's phase-Tasks

On a `cut-phase-tasks` action, run it yourself (idempotent):

```bash
python3 "$SDLC" cut-phase-tasks <epic-n> --arch-body "..." --lld-body "..." --repo-path <p>
```

Each phase-Task authors its doc at `<docRoot>/epic-<n>/architecture.md` / `lld.md` on its own
`issue-<n>` branch; `transition` raises the design PR into `epic-<n>`, and the review runs on it.

- **`arch-review` clean** → `skip-gate` (confidence above the threshold) or `waive-gate` (profile
  waives Gate B); each merges the design PR and closes the Task. Else `open-gate`; `pass-gate`
  then closes it. On `phase_task_complete: false`, fix the `reason` (e.g. `design_pr`
  `behind_base` → `sync-branch <n>`) and re-run.
- **`lld-review` clean** (any confidence) → `finish-lld <lld-task-n> --epic <epic-n> --repo-path <p>`.
  On `failed_step`, fix the cause and re-run it from the top. Never claim the new Tasks; go to Step 1.
- **Close a phase-Task only with `close-issue`, never `mark-issue-closed`.**

Detail: `references/epics.md`, "How a non-standing Epic runs".

### Architecture deviation — a revision phase-Task

When a unit reports that the approved `architecture.md` does not fit, cut a revision and park
the unit (`references/epics.md`, "Architecture deviation escalation"):

```bash
python3 "$SDLC" open-arch-revision <epic-n> --title "Architecture revision: <deviation>" \
  --body "<the deviation, and the unit that found it>" --blocks <affected-n> ... --repo-path <p>
python3 "$SDLC" pause-for-epic-regate <n> --epic <epic-n> [--gate-pr <pr>] --found-by <stage>
```

## Hooks — enforced for you

Run from the driven repo's root. What each hook does: `README.md`, "Hooks". You act on three:

- **A Bash denial names the command to run instead: run it.** Operator-directed work outside the
  pipeline passes: `gh pr create --head <branch>`, `gh pr merge <branch>`, `edit-issue`. A missing
  token at session start: get it from the operator (`references/operations.md`, "Repo access").
- **Launch `sdlc:dev-worker` only from a `"fanout": "unavailable"` hand-back**, each prompt
  starting `AXIS: <slice> ISSUE: <n>`.
- **An agent runs until it ends with `SDLC-RESULT`.** The hooks record metrics; never record them yourself.

## Deterministic control plane

Every GitHub read and mutation, and every fetch/checkout/merge/push, goes through
`python3 "$SDLC" <command>`. Never hand-run a GitHub mutation or GraphQL. Hand-run git only where
a reference names it (conflict resolution, break-glass). Each command prints one JSON
object. **Exit 0** = a valid result, refusals included. **Exit 1** = failure: stop and report;
never retry by hand. Classifier denial: `references/operations.md`, "Auto-mode classifier denies
a composite". Run `merge-pr`, `finish-lld`, `start-stage`, `close-epic`, `cut-phase-tasks` and
`open-initiative-pr` with `run_in_background`.

**Composites** run their steps in order, stop at the first failure and return `remaining_steps`.

| Command | Does → returns |
|---|---|
| `start-stage <n> --role <role> --run-id <id> [--unit epic] [--base <ref>]` | `worktree-add` → `claim`. Refuses `development` on a unit with an open PR (exit 0: `refused`, `reason`, `pr`; resume its agent, see the `resume` row) and at the run cap (`run-cap`, `stop_at_cap: true`) |
| `transition <n> --expect-stage <s> [--pr <pr>] [--repo-path <p>] [--base <ref>]` | `verify-exit` → `sync-branch` → `open-design-pr` (phase-Task) → `start-comment` (review role) → `ready`, `stopped_at`, `handoff_marker_present`, `conflict`, design `pr`, `held` (no suite evidence — `references/operations.md`, "Local-CI attestation"), `skip_confidence_threshold` |
| `advance-standing <n> --from none\|product\|product-review\|architecture\|arch-review --to product\|architecture\|development --reason "<one line>" [--repo-path <p>]` | `transition` → `route` → `start-stage` → `routed`, `path`, `claimed`, `failed_step`, `reason` |
| `skip-pr-review <n> --pr <pr> --reason "<one line>" --run-id <id>` | `verify-exit` → `route --to merge` (refused after a `pr-review` bounce) → `merge-pr` → `merged`, `failed_step` (`behind_base` → `prepare-rework`, re-run) |
| `finish-gate-feedback <n> --expect-stage product\|architecture --repo-path <unit worktree>` | `transition`, then `mark-feedback-addressed` on `ready` → `ready`, `pipeline_status`, `failed_step` |
| `prepare-rework <n> [--unit epic] [--repo-path <p>]` | `worktree-add` → `sync-branch` → `path`, `head`, `conflict`, `conflicting_files`, `pr`, `suites.needed`, `suites.carried`, `suites.passed_by_check` (`needed`: `pending` = wait for CI; `missing` = resume `development` to run it and `record-local-ci`; an integration suite on a Task PR into `epic-<n>` = a config entry lacking `bases`: fix the config, never run it) |
| `cut-phase-tasks <epic> [--arch-body TEXT] [--lld-body TEXT] --repo-path <p>` | `epic-<n>`, both phase-Tasks, LLD blocked by Architecture → `architecture_task`, `lld_task` |
| `finish-lld <lld-task-n> --epic <e> --repo-path <p>` | `merge-design-pr` → `create-lld-tasks` → `merge-lld-doc` → `close-issue` → `completed_steps`, `failed_step`. Re-run from the top; never hand-run `remaining_steps` |
| `open-arch-revision <epic> --title TEXT --body TEXT [--blocks N ...] --repo-path <p>` | Revision Task at `architecture`, worktree off `epic-<n>`, `blockedBy` per `--blocks` → `revision_task` |
| `file-closing-delta <epic> --title TEXT --body TEXT [--priority P] [--effort E] [--start --repo-path <p>]` | A closing finding as a `Bug` child (Blocker/Critical → Urgent); `--start` (operator-authorised) also starts `development` → `delta_issue` |

The base comes from the native parent: `origin/epic-<n>`, `origin/initiative-<i>` for an Epic
under an initiative branch, else `origin/main`. Every other command:
`references/control-plane.md` (read it first). `transition` / `verify-exit` need the unit's own
worktree. Never check out a pipeline branch in the main checkout.

**The CLI does not decide; you do:** which stage owns a defect, whether a bounce trips the valve
(`pipeline.escalation`: default 3 → context-reset replacement, 6 → `needs-human`), whether an
ambiguity is genuine, and all prompt, doc and PR prose.

## Epic number is mandatory

`next-action` and the pool queries require the Initiative or Epic number (`/sdlc:run <n>`). No
number → ask first. One invocation per Initiative or Epic: two on the same one race, and so do
an Initiative run and a run of its Epic. Link every Epic and Task as a native sub-issue
(`create-issue --parent`).

## Placement

A cloud session drives only cloud-placed Epics; a local session the rest, plus a cloud Epic's
local Tasks (`localLabel`). On `placement_mismatch`, stop, relay its `hint`, start nothing. A
cloud run asks in session and keeps working; it ends only when nothing can move
(`references/cloud-mode.md`, "A cloud run").

## Config can move under you

On `config_changed` from `merge-pr` or `next-action`, re-read the config before the next stage
and tell the operator the changed `keys`.

## What you decide, and what you take to the operator

- **During `development` and `pr-review`, apply the recommended fix yourself.**
- **How you ask** follows `pipeline.humanChannel` (`references/cloud-mode.md`, "Human channel").
- **Escalate exactly three things:** a product or scope call, an amendment to a gate-approved
  doc, a valve trip (`mark-needs-human`). With `pipeline.epicClose.auto` on, also an epic close
  blocked by a Blocker/Critical delta, an open manual-testing bug, or a verification that could
  not run (`references/epics.md`, "Epic closing").
- **Never ask about** parallelism, worktrees, models, review scoping, defect ownership, merging
  (`references/operations.md`, "PRs merge automatically"), or epic close with auto on.

## Looping within an invocation

When a unit hits `blocked` / `needs-human` / an open gate, park it and return to Step 1. End only
when Step 1 returns `none` (on an Initiative, its own `none`), then Step 4. Unattended looping:
`references/continuous-mode.md`.

## Concurrency

**Run one unit at a time by default.** Fan out (`list-parallel-ready`, `list-ready-for-review`,
`list-design-ready`) only when `parallelism.devLane` / `prReview` / `designLane` is above 1; read
`references/parallelism.md` first. Phase-Tasks always run in sequence; rework is one thread per issue.

## Step 1 — Pick the one unit to work

Make **one run id per invocation** (e.g. `date +%s`-`$$`) and pass it to every `next-action`,
`list-parallel-ready` and `start-stage`. At start, run `python3 "$SDLC" prune-stale --repo-path <p>`.

```bash
python3 "$SDLC" next-action <epic> --run-id "$RUN_ID" --sync-epic
```

| `action` | What to do |
|---|---|
| `resume` | A session died mid-stage. If you drive that unit now, skip it and survey again. On `likely_live`, launch nothing; ask the operator. Else, with an open PR: `prepare-rework <n>`, then dispatch per `references/rework.md`, "Replacing an agent that cannot be resumed"; never `start-stage`. Without one: resume at `stage` from comments and committed docs (Step 3) |
| `route` | Fresh standing child: `advance-standing <n> --from none --to <stage>` ("Routing a standing child"; Step 2's scope alignment first for `product`) |
| `delegate` | Step 2, then Step 3 |
| `pass-gate` | `references/gates.md`, "Passing a gate"; pass `issue`/`gate_pr`/`stage` verbatim |
| `address-gate-feedback` | `references/gates.md`, "Addressing gate feedback" |
| `finish-lld` | `finish-lld <issue> --epic <epic> --repo-path <p>` |
| `cut-phase-tasks` | `cut-phase-tasks <epic> --repo-path <p>`, then Step 1 |
| `run-epic` | Run that Epic's Step 1–3 loop inline, same run-id, then Step 1 on the Initiative |
| `launch-cloud-epics` | `launch-cloud-epic <epic>` for each in `epics`, then Step 1 |
| `stop-at-cap` | Finish in-flight units, then stop ("Stop at the run cap") |
| `none` | Route `unstaged` children, run the end checks, then Step 4 |
| `skip` | Legacy Epic: quote `reason`, then Step 4 |

- **With `devLane` > 1:** run `list-parallel-ready` after every `merge-pr` and `finish-lld`.
- **`product_cap`** on `none`: product work was deferred; report it.
- **A `blockedBy` edge** usually constrains `development` onward only. Start a standing child's
  design stages anyway.
- **`epic_sync`:** `conflict` → `references/parallelism.md`, "Git-conflict handling"; `error` or
  `touches_error` → report it. `design_touch` → judge whether the design holds, else
  `open-arch-revision`. `schema_touch` → confirm each app using that package has its migration, else route the gap to the owning unit.
- **End checks on `none`:** `list-needs-human` (clear stale reasons), `check-epics-closeable`,
  `audit-issues --epic <n>` (`--initiative <n>`; run each `repair`, never re-create), `prune-stale`.
- **A closeable epic with `pipeline.epicClose.auto` on:** `close-epic` → exploratory pass →
  record it only if clean → `close-epic --clean-worktree` (`references/epics.md`, "Epic closing").
  Escalate the cases in "What you decide". Off: report it.

Closing an Initiative: `references/initiatives.md`, "Closing an Initiative".

## Step 2 — Claim it

**Scope alignment before `product`.** When a unit enters `product` with no `product.md`, state
back what it covers, excludes and leaves open, and ask the genuine ambiguities in one
`AskUserQuestion` batch. Pass the answers verbatim to `product` (`references/design-doc-rules.md`,
"Scope alignment before `product`").

```bash
python3 "$SDLC" start-stage <n> --role <role> --run-id "$RUN_ID"
```

`<role>` is `next-action`'s `stage`. Tell the operator in one line what you picked up. With
`pipeline.stack.enabled`, run `provision-epic-stack <n>` at the Epic's first touch.

## Step 3 — Run this stage, then the next, then the next

Delegate to exactly **one** fresh `sdlc:<role>` subagent (`arch-review` and `lld-review` use
`sdlc:design-review`). `product-review` follows `product`; `arch-review` follows `architecture`;
`lld-review` follows `lld` (mandatory, no gate); `pr-review` follows `development`. Never pick a
model (no `ANTHROPIC_DEFAULT_OPUS_MODEL`, no versioned id under `pipeline.models`); retune the
policy only via the retro. `development` owns no doc; its suite evidence is required checks or
`record-local-ci`. `pr-review` never bounces a normal Task for coverage the standing test Tasks
own. There is no `testing` stage; a child left at the retired `Testing` Stage is still picked up.

### The delegation prompt

Give the agent (by `gh` command for anything large; in the cloud a `gh api` GET):

1. The unit's number, title, body and thread; for a phase-Task, its parent's too.
2. The doc path it owns, under the unit's `<docRoot>` (`start-stage`'s `doc_root` or `doc-root <n>`).
   For a functional Task's `development` and `pr-review`: read the design with
   `python3 "$SDLC" lld-section --epic <parent> --task <n> --repo-path <worktree>`.
3. For `lld`: `architecture.md` is the source of truth; fits-vs-deviates comes first.
4. Its worktree, with "use absolute paths or a standalone `cd`; never chain `cd <path> && …`; never touch the main
   checkout". For `development`, the test-env helper and `Integration tests: mandatory` only per
   "Test scope"; never brief a whole-suite run "Test scope" does not owe. For `pr-review`, the repo root.
5. For a review: diff against `origin/` refs after `git fetch origin`.
6. Prior art only you can read: export it to `<worktree>/.sdlc-scratch/prior-art/` and name each file in the prompt.

Never restate the agent file's rules. Template (`EPIC` is the Initiative/Epic you are driving):

> ROLE: <role> ISSUE: <n> EPIC: <epic>
> Unit: #<n> — <title>. Worktree: <path>. Doc you own: <doc-path>.
> The 2–3 unit-specific facts.

Write the first line plain, with no backticks: the hooks parse it. SubagentStart adds
`arch-review`'s `Skip threshold`.

Note each agent's ID per role for resume-based rework (session only; drop it at the unit's
stopping point). Send findings back with `SendMessage` yourself, never via a relay fork. Send
cross-file searches to a read-only `Explore` subagent.

### After the subagent returns

Its last line is `SDLC-RESULT: {"issue": <n>, "stage": "<stage>", "outcome": "<outcome>"}`
(`references/stage-playbooks.md`, "The handback is terse").

| `outcome` | Do |
|---|---|
| `done` / `clean` | `transition`, then the next stage. `pr-review` `clean` → `merge-pr <pr> --issue <n> --run-id "$RUN_ID"` |
| `done-with-concerns` (`development` only) | As `done`; start the `pr-review` brief with `Concern from development: <the result's concern>. Check it first.` |
| `rework` | Print each blocking finding (what, `file:line`, owning stage), then route it (`references/rework.md`) |
| `blocked` + `"fanout": "unavailable"`, review role | Launch one agent per axis from its briefs, resume the reviewer (`references/review-fanout.md`, "The orchestrator fans out for you") |
| `blocked` + `"fanout": "unavailable"`, `development` | One `sdlc:dev-worker` per `slices` entry (≤ `maxChildren`), then resume `development` (`references/parallelism.md`, "Development fan-out from briefs") |
| `blocked` | Clear the named blocker: resume the stage that owns the question, `open-arch-revision` for a design that does not fit, `mark-blocked` for a cross-issue dependency |
| `needs-human` | `references/cloud-mode.md`, "Human channel" (`"github"`: `mark-needs-human <n> --reason "..."`, park) |
| `failed` | Inspect the worktree yourself, then resume the agent |

**Verify, don't trust:**

```bash
python3 "$SDLC" transition <n> --expect-stage <s> [--pr <pr>] --repo-path <the unit's worktree>
```

`<s>` is the review about to start or the stage just finished; `--pr` with `pr-review`.
**Always pass the unit's own worktree**: from the main checkout `verify-exit` falsely reports the doc missing.

- Proceed only on `ready: true`. `handoff_marker_present: false` → resume `development` to post
  its marker. `conflict` → "Git-conflict handling". A failed `verify-exit` → fix the missed exit action.
- Before resuming a rework or run-only agent, and on `behind_base` or `held`: `prepare-rework <n>`.
  An agent that handed back is not live until you resume it; never sync while one runs.
  `pass-gate`/`skip-gate`/`waive-gate` reconcile internally; no sync after them.
- Give a phase-Task's review agent the design PR (`steps.open-design-pr.pr`).
- Summarise the approved `architecture.md` after a clean `arch-review`, and `product.md` before Gate A.
  Never summarise `lld.md`.
- A classifier-denied mutation probe is not a clean signal: `references/operations.md`, "Auto-mode
  allow rule for the `pr-review` mutation probe".
- A review with a verdict but no exit action: run `record-pr-review` from it yourself and tear its
  stack down. Re-run the stage only when the report has no verdict.
- Confirm the stage left an issue comment.
- A proposed new issue: file it (`references/rework.md`, "Where a finding this unit will not fix goes").

Your exit actions: gates, `transition`, `merge-pr`, closing-verification records, `finish-lld`.
Repeat Steps 2–3 until the unit is merged, blocked, or needs a human.

### Routing a standing child

After pickup and every `done`/`clean`, pick the next stage from the issue and the result's
`next`/`why`. Run a stage only when it earns its place:

- `product`: an open product decision.
- `architecture`: a new component or interface, a data-model, cross-module or external-API change, or an unclear approach.
- `product-review` / `arch-review`: skip after a trivial artifact.
- `pr-review`: the default. Skip (`skip-pr-review <n> --pr <pr> --reason "..." --run-id "$RUN_ID"`)
  only for a trivially low-risk change; never after a bounce.

Skip ahead with `advance-standing <n> --from <stage> --to <stage> --reason "<one line>"`. `route`
only moves forward; for a late product decision, `set-stage <n> --stage product`. When the gate
doc is the whole deliverable, `open-gate` it even if waived, then `pass-gate <n> --doc-only`.

### Stop at the run cap — reset your context between batches

`parallelism.maxTasksPerRun` (0 = unlimited) caps units driven to a terminal state per run id.
On `stop-at-cap` (or `stop_at_cap: true`), finish in-flight work and stop; do not call
`next-action`/`list-parallel-ready` again. The next `/sdlc:run` resumes there. `resume`,
`pass-gate`, `address-gate-feedback` and `in_flight` units are never capped;
`close-issue --not-planned` never counts. The cap never changes Task carving or `blockedBy` order.

## Step 4 — Report back to the user

Summarise every unit touched, stages run, rework rounds and current state. For an Initiative run,
group by Epic (cut, run, parked, closed, blocked and on what) and end with the Initiative's own
`none` reason. Above the fold: `needs-human` units with reasons; blocked units and their waits;
rework findings; gate-pending units; merged PRs and closed issues; `warnings`, `uncovered_paths`
and `plugin_warning` verbatim; cloud sessions with urls; newly closeable epics; `product_cap`;
`audit-issues` flags. End with one cost line:
`python3 "$(dirname "$SDLC")/sdlc_metrics.py" report --run <run-id>`. Pass the id `next-action`
got, never a regenerated one; a `warning` lists `recent_runs` to pick from.

## Step 5 — Retrospective (only when the operator asks)

Never start a retro on your own. Park friction findings with `park-finding`
(`references/retro.md`).
