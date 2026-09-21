---
name: run
description: Use when the operator invokes /sdlc:run <epic> to drive a GitHub-Issues Initiative or Epic and its child issues through the auto-SDLC stages to merge, or asks to resume, unblock, retro, or continuously run that pipeline. Requires an sdlc-pipeline.config.json in the target repo.
model: sonnet
---

# Auto SDLC over GitHub Issues

One invocation drives one Initiative's or Epic's actionable work, stage by stage, toward
merge; an Initiative run descends into its Epics one after another ("The Initiative loop").
You (the main agent) are the orchestrator: for each unit, delegate one stage to
one subagent, wait, verify, decide what's next. "Parallel" means more than one child's
handoff loop is live at once — never that anything decides on its own.

- GitHub fields and comments are the **visibility log** and crash-resume point; the
  committed docs under `<docRoot>/epic-<n>/` (an Epic's phase-Tasks) and `issue-<n>/` (everything else) are the **source of record**.
- This file and its references change only through the Step 5 retrospective or direct
  operator feedback — never mid-cycle by a stage agent, which flags problems in its
  handoff instead.

## Reference files — read on demand, not upfront

| File | Read it when |
|---|---|
| `${CLAUDE_PLUGIN_ROOT}/references/parallelism.md` | Running more than one unit at once; a git conflict; an agent dying mid-stage; worktree/branch mechanics |
| `${CLAUDE_PLUGIN_ROOT}/references/gates.md` | Opening, checking, passing, skipping a gate; gate feedback |
| `${CLAUDE_PLUGIN_ROOT}/references/rework.md` | A review finding, a reported ambiguity, a possible valve trip, a replacement agent's resume message |
| `${CLAUDE_PLUGIN_ROOT}/references/stage-playbooks.md` | Delegating any stage. Every stage agent Reads it (rules binding every role) |
| `${CLAUDE_PLUGIN_ROOT}/references/design-doc-rules.md` | `product.md`/`architecture.md` content; scope alignment. Read by `product`, `product-review`, `architecture`, `design-review` |
| `${CLAUDE_PLUGIN_ROOT}/references/verification-rules.md` | Proving a claim by running it. Read by `architecture`, `lld`, `development`, `pr-review`, `design-review` |
| `${CLAUDE_PLUGIN_ROOT}/references/review-fanout.md` | Review fan-out. Read by `design-review` |
| `${CLAUDE_PLUGIN_ROOT}/references/epics.md` | Profiles, phase-Tasks, footprints, deviation, epic close, board status |
| `${CLAUDE_PLUGIN_ROOT}/references/operations.md` | Token/repo access, issue fields, auto-merge, local-CI attestation |
| `${CLAUDE_PLUGIN_ROOT}/references/continuous-mode.md` | The operator asked for unattended looping |
| `${CLAUDE_PLUGIN_ROOT}/references/history.md` | You want the incident behind a rule |

## The lifecycle model

Every phase of work is **its own plain child issue** (a "phase-Task"): an Initiative's
`product`, an Epic's `architecture` and `lld`. The Initiative/Epic issue is a pure
container — no stage is ever delegated to it. Where a unit merges depends on its parent:
an Initiative's Product-Roadmap Task, a standing child and a parentless issue gate and merge
`issue-<n>` → `main`; **every child of a non-standing Epic — phase-Tasks and functional Tasks
alike — branches from and merges into `epic-<n>`**, which reaches `main` once, at epic close.

```
initiative: [Product-Roadmap Task: product -> product-review -> Gate A (PR -> main)] -> [orchestrator cuts Epics, ordered by blockedBy] -> [Initiative loop: per Epic, cut-phase-tasks then run-epic]
epic:       [Architecture-phase Task: architecture -> design PR -> arch-review on it -> merged into epic-<n> (pipeline above the skip bar, else the human at Gate B) -> Task closed; LLD-phase Task unblocks]
            [LLD-phase Task: lld (specifies functional + the 2 standing Tasks) -> design PR -> lld-review on it] -> [orchestrator: finish-lld = merge the PR into epic-<n>, create Tasks, close]
task:       development -> [pr-review] -> auto-merge into epic-<n> -> CLOSED
```

- An issue is an Epic only by `pipeline.classification` (`references/epics.md`, "What
  makes an issue an Epic"); profiles decide how it runs ("Epic profiles").
- **Engineering-driven work**: a bare Epic with its scope written in the body, no
  Initiative, no `product.md`; it starts at its Architecture-phase Task, and
  `architecture` returns its questions (`needs-human`) when scope is unclear. An Initiative may wrap
  such Epics for grouping only (no `product.md`; `architecture` reads the Epic's scope).
- **Standing profile** (`epicLevelPhase: false`): technical (RTB) tasks only; no
  phase-Tasks, no epic branch, no human gate. Each child runs only the stages it needs of
  `product -> [product-review] -> architecture -> [arch-review] -> development ->
  [pr-review] -> CLOSED` ("Routing a standing child") and merges straight to `main`.
  **Legacy profile** (`driven: false`): skipped entirely, Tasks included.
- **Every Epic has two standing Tasks** (Integration-test, e2e-test), specified by `lld`,
  running after every functional Task merges.
- **Epic close does not run the full e2e suite:** the e2e-test Task owns that evidence, so
  `close-epic` requires only the exploratory pass (`record-epic-verification --kind e2e`
  is still accepted, informational). GitHub Actions results never gate the epic merge
  (a passing check still satisfies a required suite; a failed or never-started one is
  ignored); the required suites' `record-local-ci` attestations at the epic head remain.
- **`product-review` follows every `product`** unless a standing child is routed past it:
  a blocker bounces `product`; clean goes to Gate A (waived when the profile sets
  `requiresHumanGateA: false` — `references/gates.md`, "Waived gates"). Reviews have no Stage value of their own.
- A non-standing Epic's Tasks are never eligible before it is `epic:architected` (set by
  `finish-lld`); the control plane enforces this.
- **Route every `unstaged` child yourself** (listed in a `none` result, with
  `unstaged_reason`): `set-stage <n> --stage development` when the Epic's `lld.md`
  already covers it, else an Architecture revision (below).
- **Nothing spins off a separate ticket.** Every problem found before merge is fixed
  inline by resuming the subagent that owns the responsible stage
  (`references/rework.md`). Only three things pause a unit — a cross-issue `blockedBy`,
  a `needs-human` verdict, an open gate — and none pauses the invocation ("Looping").

### Cutting an Initiative's Product-Roadmap Task

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

### Cutting Epics from an approved Initiative

Do this yourself, not via a subagent: read the Product-Roadmap Task's approved
`docs/sdlc/issue-<n>/product.md` and cut it into Epics:

- **Each Epic must be independently mergeable to `main` and independently shippable.**
  An Epic that only makes sense once a sibling has merged is cut wrong.
- Each Epic's body carries a pointer to the Initiative's IRD plus its own explicit scope
  carve-out (the slice of the IRD it covers).
- Create each with `create-issue --parent <initiative-n> --type Epic --priority <P>
  --effort <E>`, Priority and Effort from the approved `product.md`'s sizing
  (`references/operations.md`, "Issue taxonomy").
- **Order them from `product.md`'s wave order:** create Epics in wave order and give every
  Epic after the first its prerequisite Epic(s) as native `blockedBy` edges — at creation
  with `--blocked-by <prerequisite-epic-n>` (repeatable), or later with `add-blocked-by
  <epic-n> --on <prerequisite-n>`. Epics with no prerequisite carry no edge. The Initiative
  loop reads only these edges, never `product.md`.
- Do not cut their phase-Tasks by hand: the Initiative loop returns `cut-phase-tasks` for
  each Epic when it becomes runnable (next two sections).

### The Initiative loop

`next-action <initiative>` returns `none` only when nothing is left to do. While cut Epics
are open it walks them, lowest number first, skipping any Epic that is blocked by an open
issue or is legacy:

- **`cut-phase-tasks`** (`epic`) — the next runnable Epic has no phase-Tasks (or only one of
  the two). Run `cut-phase-tasks <epic> --repo-path <p>` (next section), then call
  `next-action <initiative>` again.
- **`run-epic`** (`epic`) — run that Epic's normal Step 1–3 loop inline: `next-action <epic>
  --run-id "$RUN_ID"` and everything below, through its `none`, `check-epics-closeable` /
  `close-epic` (when `pipeline.epicClose.auto` allows) and its Step 4 facts. Then return to
  `next-action <initiative>`. **Use the same `--run-id` for the Initiative and every Epic**:
  `parallelism.maxTasksPerRun` is shared across them, and `stop-at-cap` on either means
  finish the in-flight unit, stop and report.
- An Epic whose loop ends still open (waiting on a human gate, `needs-human`, blocked) is
  parked: call `next-action <initiative> --skip-epic <epic>` (repeatable) for the rest of the
  run so the loop moves to the next runnable Epic. Never re-descend into a parked Epic.
- `none` names each open Epic's state in `reason` (blocked by #n, not driven, parked). When
  every cut Epic is closed it says so: "Closing an Initiative".

### Cutting an Epic's phase-Tasks

`next-action` asks for this (`cut-phase-tasks` action) on an Initiative whose next Epic
lacks them and on a bare Epic run (an Epic created outside an Initiative, an
engineering-driven Epic); do it yourself, never via a subagent:

```bash
python3 "$SDLC" cut-phase-tasks <epic-n> --arch-body "..." --lld-body "..." --repo-path <p>
```

It stands up `epic-<n>` on origin and its worktree first, then creates, stages and orders
both phase-Tasks and adds the Architecture-phase Task's worktree off `origin/epic-<n>`;
idempotent. Both phase-Tasks author their doc at `<docRoot>/epic-<n>/architecture.md` /
`lld.md` on their own `issue-<n>` branch. When the stage returns, `transition` raises the
design PR `issue-<n>` → `epic-<n>` and the review runs on it. Then:

- **Architecture-phase (or revision) Task, `arch-review` clean** → `skip-gate` (confidence
  above the threshold) or `waive-gate` (profile waives Gate B) — each merges the design PR
  into `epic-<n>` and closes the Task — else `open-gate`, which gates that same PR for the
  human to merge. `pass-gate` (or the Action) then closes the Task. On
  `phase_task_complete: false`, fix the `reason` (`design_pr` shows why the merge refused,
  e.g. `behind_base` → `sync-branch <n>`), then re-run the command.
- **LLD-phase Task's `lld-review` is clean** (no gate, any confidence) →
  `finish-lld <lld-task-n> --epic <epic-n> --repo-path <p>`. On `failed_step`, fix the
  cause and re-run it. Never claim the new Tasks; return to Step 1.
- **Close a phase-Task only with `close-issue`, never `mark-issue-closed`.**

Detail (why a Stage is mandatory, base detection, the epic branch):
`references/epics.md`, "How a non-standing Epic runs".

### Architecture deviation — a revision phase-Task

When a unit reports that the Epic's approved `architecture.md` does not fit (new
component boundary, unanticipated data-model change, a premise that doesn't hold), the
Epic has no gate to re-open — cut a revision phase-Task:

```bash
python3 "$SDLC" open-arch-revision <epic-n> --title "Architecture revision: <deviation>" \
  --body "<the deviation, and the unit that found it>" --blocks <affected-n> ... --repo-path <p>
```

It runs `architecture` (editing `epic-<n>/architecture.md` in place) → design PR →
`arch-review` → merge, like the Architecture-phase Task. Park the reporting unit right away
with `pause-for-epic-regate <n> --epic <epic-n> [--gate-pr <pr>] --found-by <stage that found it>`
(`--gate-pr` only once the revision's design PR exists). If `lld.md` must change too,
resume or re-run the LLD pass before the affected Tasks proceed. Escalation (third /
sixth deviation, `mark-needs-human` on the Epic): `references/epics.md`, "Architecture
deviation escalation".

## Hooks — enforced for you

Run from the driven repo's root; everything project-specific is in its `sdlc-pipeline.config.json`.

- **SessionStart** exports `$SDLC` (the control plane) and `GITHUB_TOKEN` (from the config's `tokenEnv` var, else `tokenPath`, overriding any ambient one). If it reports the token missing, get it from the operator first; relay its optional `rtk init` hint once, never block on it. After a compaction it restates the run you were driving; on an off-policy session model it tells you to have the operator restart with `sdlc-run <n>`.
- **PreToolUse (Bash)** denies hand-run GitHub mutations, GraphQL, `git worktree add` (except `--detach`), force-push and rebase, and limits each stage agent to its role's commands. A denial names the `python3 "$SDLC"` command to run instead — run it; never work around the guard. The main thread is guarded only while its session drives a run (a run-state file written by `next-action --run-id` within `guard.mainThreadFreshnessHours`, default 8; `guard.mainThread: "always"` guards every session) — an unguarded call logs one stderr line, so a run without `--run-id` is visible, never silent.
- **PreToolUse (Agent)** sets every `sdlc:*` agent's `model` from `${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json` (config `pipeline.models` / `pipeline.fanout` override it), caps review fan-out, and lets only its `explore` roles launch a read-only `Explore` search. It denies an `sdlc:design-review` prompt lacking the header when the two review roles' models differ.
- **SubagentStart** gives each `sdlc:*` agent `$SDLC`, `docRoot`, `requirementsDir`, `docTemplates`, the references path and the `SDLC-RESULT` format.
- **SubagentStop** keeps an `sdlc:*` agent running until its final message ends with an `SDLC-RESULT` line (`product`/`architecture`/`lld` finishing `done` also need a successful `post-comment` this round). It and **SessionEnd** record each agent's tokens, cost, tool calls and peak context (README, "Metrics"). Never record metrics yourself.

## Deterministic control plane

Every GitHub read, decision and mutation, and every fetch/checkout/merge/push, goes
through `python3 "$SDLC" <command>` — never hand-run `gh`/GraphQL/git. Each command
prints one JSON object. **Exit 0** = valid result, including "nothing to do" and
structured refusals (sync conflict, behind-main merge). **Exit 1** = operational
failure: stop and report; never retry by hand.

**Composites — the normal path.** Each runs its steps in order, stopping at the first
failure.

| Command | Does → returns |
|---|---|
| `start-stage <n> --role <role> [--unit epic] [--base <ref>]` | `worktree-add` then `claim` (Step 2) → worktree result, claim result. Refuses `development` on a unit with an open PR (`failed_step: check-claimable`): resume its agent instead (`references/rework.md`) |
| `transition <n> --expect-stage <s> [--pr <pr>] [--repo-path <p>] [--base <ref>]` | After a subagent returns: `verify-exit` → `sync-branch` → `open-design-pr` (an Epic's `architecture`/`lld` phase-Task only) → `start-comment` when `<s>` is a review role (refuses a `pr-review` whose required-suite `record-local-ci` attestation is not on the post-sync head; a docs-only sync carries forward; `arch-review` also returns `skip_confidence_threshold` for the reviewer) → `ready`, `stopped_at`, per-step results (incl. `handoff_marker_present`, `conflict`, the design `pr`) |
| `cut-phase-tasks <epic> [--arch-body TEXT] [--lld-body TEXT] --repo-path <p>` | `epic-<n>` + its worktree, create + stage both phase-Tasks, LLD blocked by Architecture, Architecture worktree off `epic-<n>`; idempotent → `architecture_task`, `lld_task` |
| `finish-lld <lld-task-n> --epic <e> --repo-path <p>` | `merge-design-pr` → `create-lld-tasks` → `merge-lld-doc` → `close-issue` → `completed_steps`, `failed_step`, per-step results |
| `open-arch-revision <epic> --title TEXT --body TEXT [--blocks N ...] --repo-path <p>` | `epic-<n>` worktree + `create-issue` + `set-stage architecture` + worktree off `epic-<n>` + `add-blocked-by` per `--blocks` unit → `revision_task` |
| `file-closing-delta <epic> --title TEXT --body TEXT [--priority P] [--effort E] [--start --repo-path <p>]` | A closing-run finding as a `Bug` child of the Epic; `--priority` takes Urgent/High/Medium/Low (Blocker/Critical file as Urgent); `--start` (operator-authorised close-blocker lane) also stages `development` and `start-stage`s it → `delta_issue` |

`worktree-add`/`sync-branch` auto-detect the base from the native parent: `origin/epic-<n>` for
every child of a non-standing Epic (phase-Tasks included), `origin/main` otherwise; `--base` is
an override only.

**Building blocks** (use directly only when no composite fits):

| Command | What it owns |
|---|---|
| `next-action <epic\|initiative> --run-id <id> [--skip-epic <n> ...]` | The one unit to work (Step 1); on an Initiative, the Epic to descend into ("The Initiative loop") |
| `list-parallel-ready <epic> --repo-path <p> --run-id <id>` / `list-design-ready <epic> --repo-path <p>` / `list-ready-for-review <epic>` | Dev-lane / standing-epic design-lane / review pools |
| `lld-section --epic <n> --task <m> --repo-path <p>` | Only Task #`<m>`'s subsection of `epic-<n>/lld.md` |
| `worktree-add <n> [--unit epic] [--base <ref>]` | The only way to make a worktree: resumes from `origin/<branch>` (fast-forwards; refuses a diverged branch), else branches off the integration base (recreates a stale local branch with no unique commits; refuses one with) |
| `claim <n> --role <r>` / `start-comment <n> --role <r>` / `sync-branch <n> [--unit epic] [--base <ref>]` / `verify-exit <n> --expect-stage <s> [--pr <pr>]` | The steps inside `start-stage` / `transition` |
| `route <n> --to product\|architecture\|development\|merge --reason "<one line>"` | Skip a standing child ahead ("Routing a standing child"); refuses (exit 0) anything but a forward move on a standing child |
| `set-stage <n> --stage <s>` / `add-blocked-by <n> --on <dep>` / `create-issue --parent <n> --type <T> [--blocked-by <dep> ...]` / `repair-issue <n> [--parent <p>] [--type <T>]` | Stage a unit / order units (Epics: wave order, "Cutting Epics") / the only issue-creation path / fill an existing issue's missing fields |
| `open-design-pr <n>` / `merge-design-pr <pr> --issue <n> [--repo-path <p>]` | A phase-Task's design PR `issue-<n>` → `epic-<e>`: `transition` opens it (idempotent); `merge-design-pr` squash-merges it once the review is recorded clean (never closes the Task, keeps its branch; refuses `behind_base`, missing/`rework` evidence, a review of a head whose doc has since changed (`review_stale`), and an `arch-review` below the skip bar). `skip-gate`/`waive-gate`/`finish-lld` call it |
| `create-lld-tasks <epic> --repo-path <p>` / `merge-lld-doc <epic-n>` / `close-issue <n> [--repo-path <p>] [--not-planned --reason TEXT]` | The steps inside `finish-lld`; `close-issue` is the orchestrator's close (terminal fields, worktree release, a merged design PR's branch deleted); `--not-planned` drops a unit with its reason on the thread (`references/operations.md`, "Dropping a unit") |
| `detach-epic <epic> [--reason TEXT]` / `comment <n> --body TEXT\|--body-file <f>` | Take an Epic out of its Initiative (sub-issue link + sibling `blockedBy` edges, commented on both) / a plain audit-trail comment, no marker (`references/operations.md`, "Dropping a unit") |
| `open-gate` / `check-gate` / `pass-gate` / `skip-gate` / `waive-gate` | Gates (`references/gates.md`); on a phase-Task pass/skip/waive close it (after merging its design PR) instead of claiming a next stage |
| `merge-gate <pr> --issue <n> --stage product\|architecture --operator-confirmed` | Merge an open gate PR (Gate A, or a human-gated design PR) — **only when the operator explicitly told you to merge it** (`references/gates.md`, "Merging a gate PR for the operator"). Refuses without the flag, on `behind_base`, or without green checks; `pass-gate` finishes it |
| `open-dev-pr [--allow-empty]` / `handoff-to-pr-review` / `record-pr-review` / `record-local-ci` / `record-design-review <n> --role <r> --outcome clean\|rework` (also comments on the design PR) / `post-comment <n> --role <r> --body-file <f>` | Stage-agent exit actions (their agent files own them); `--allow-empty` lets a verify-only Task (no code change) open its PR on an empty commit — tell `development` to pass it when the Task's design says verify-only; `post-comment` is `product`/`architecture`/`lld`'s handoff comment |
| `pr-checks <pr>` / `merge-pr <pr> --issue <n> [--run-id <id>]` | CI status / the only merge gate (pass the run's id so the terminal count books under it; refuses behind-base; reports `config_changed`; on an already-merged PR only finishes the bookkeeping, `recovered: true`) |
| `mark-blocked` / `mark-needs-human` / `pause-for-epic-regate <n> --epic <e> --gate-pr <pr> [--found-by <stage>]` | Park a unit (first two release its worktree) |
| `pairing-counts <n>` / `show-config` | Valve strike counts + thresholds / effective tunables and the running `plugin` version (read once per invocation) |
| `list-needs-human` / `check-epics-closeable` / `audit-issues [--epic <n>\|--initiative <n>]` | End-of-invocation sweeps; `audit-issues` also flags open Epics with no phase-Tasks, with the `cut-phase-tasks` repair |
| `resolve-thread --thread-id <id> [--reply TEXT]` | Reply to and resolve a gate PR review thread; the gate-feedback agent runs it for the threads it addressed (`references/gates.md`) |
| `close-epic <n>` / `record-epic-verification <n> --kind e2e\|exploratory [--sha S]` / `provision-epic-stack <n>` / `teardown-epic-stack <n> [--project P] [--profile P]` | Epic close (`references/epics.md`, "Epic closing"); per-epic stack, no-op unless `pipeline.stack.enabled` or a hand-made stack is named; teardown removes nothing while the project's containers still run |
| `check-initiative-closeable <n>` / `record-initiative-verification <n> --outcome met\|unmet --summary` / `close-initiative <n>` | "Closing an Initiative" |
| `mark-feedback-addressed <n>` | Yours, after a gate-feedback agent finished and `transition` verified its push (`references/gates.md`, "Addressing gate feedback"); never the agent's |
| `auto-pass-gate` / `mark-todo` / `mark-issue-closed` / `mark-feedback-received` | CI-triggered paths only — never run them yourself (the shipped workflow runs the first three; `mark-feedback-received` only if the driven repo wires a comment trigger) |

Branch-writing commands never write in the main checkout. For most commands `--repo-path`
may be any path inside the repo, but **`transition` / `verify-exit` must get the unit's own
worktree** — they read that branch's HEAD, so from the main checkout they falsely report
"architecture.md is missing on issue-<n>". Never check out a pipeline branch in the main
checkout.

**The CLI does not decide** — you do: which stage owns a defect found in rework, whether
a bounce trips the valve, whether a reported ambiguity is genuine, and all subagent
prompt, doc and PR prose. It reports but does not apply the escalation valve
(`pipeline.escalation`, default 3 → context-reset replacement, 6 → `needs-human`) and
the continuous-mode cycle cap.

## Epic number is mandatory

`next-action` and the pool queries **require** the number of the Initiative or Epic
being driven (`/sdlc:run <n>`); the pipeline never scans the repo. No number named
→ ask before doing anything. One invocation per Initiative or Epic; an Initiative run
descends into its Epics sequentially ("The Initiative loop") — two invocations on the same
one race, and an Initiative run and an Epic run of its Epic do too. Every Epic must be a native sub-issue of its Initiative, and every Task of its
Epic, to be picked; link with `create-issue --parent`.

## Config can move under you

When `merge-pr` returns `config_changed: true`, re-read the config before the next stage.

## What you decide, and what you take to the operator

- **During `development` and `pr-review`, apply the recommended fix yourself.** An
  agent that ends with "recommend X" has done the analysis.
- **Escalate exactly three things:** a product or scope call, an amendment to a
  **gate-approved** doc, an escalation-valve trip (`mark-needs-human`). When
  `pipeline.epicClose.auto` is on, also an epic close blocked by a Blocker/Critical
  closing delta, an open manual-testing bug child, or a verification that could not be
  run (`references/epics.md`, "Epic closing").
- **Never ask the operator about:** parallelism, worktrees, model tiers, review scoping,
  which stage owns a defect, merging (`references/operations.md`, "PRs merge
  automatically"), or epic close when `pipeline.epicClose.auto` is on (off → closing is
  the operator's call).

## Looping within an invocation

When a unit hits `blocked` / `needs-human` / an open gate, park *that unit* (comment,
fields, stop) and return to Step 1 for the next actionable unit. The invocation ends
only when Step 1 returns `none` (every open child closed, blocked, needs-human,
gate-pending with nothing to address, or `unstaged` and routed); then Step 4. On an
Initiative that is the Initiative's own `none`, after its Epics ("The Initiative loop").
An Epic's `none` says nothing about other Epics. Unattended looping: `references/continuous-mode.md`.

## Concurrency

**Run one unit at a time by default.** Fan out (`list-parallel-ready`,
`list-ready-for-review`, `list-design-ready`) only when the config sets that lane's
`parallelism.devLane` / `prReview` / `designLane` above 1 (all default 1) — and read
`references/parallelism.md` first. Always: a non-standing Epic's phase-Tasks run strictly
in sequence, and rework is one development thread per issue.

## Step 1 — Pick the one unit to work

Generate **one run id per invocation** (any unique string, e.g. `date +%s`-`$$`) before
the first call, and pass it to every `next-action` and `list-parallel-ready` call this
run.

```bash
python3 "$SDLC" next-action <epic> --run-id "$RUN_ID"
```

Every result except `skip`/`none`/`stop-at-cap` carries `unit`: `"issue"`, or `"epic"` for
`run-epic`/`cut-phase-tasks`.

| `action` | Meaning | What to do |
|---|---|---|
| `resume` | A claimed stage's session died | Resume at `stage` from comments + committed docs (Step 3) — unless you are driving that unit right now in this session: then skip it and survey again. When the result flags `likely_live` (a recent `claim_age_seconds`), another session may be driving it: do **not** launch an agent — ask the operator |
| `route` | A fresh standing child | Pick its first stage and `route` it ("Routing a standing child"), then Step 1 again |
| `delegate` | A child is ready | Step 2, then Step 3 |
| `pass-gate` | A gate PR was merged (or a human merged an Architecture-phase Task's design PR before its gate opened) | `references/gates.md`, "Passing a gate" — pass `issue`/`gate_pr`/`stage` verbatim |
| `address-gate-feedback` | Gate PR has unresolved threads or new comments | `references/gates.md`, "Addressing gate feedback" |
| `finish-lld` | An LLD-phase Task's design PR was merged by a human | `finish-lld <issue> --epic <epic> --repo-path <p>` ("Cutting an Epic's phase-Tasks") |
| `cut-phase-tasks` | The Epic named in `epic` has no phase-Tasks (or only one): the next runnable Epic of an Initiative, or the bare Epic you are driving | `cut-phase-tasks <epic> --repo-path <p>`, then Step 1 again ("Cutting an Epic's phase-Tasks") |
| `run-epic` | (Initiative) the next runnable Epic | Run that Epic's Step 1–3 loop inline with the same run-id, then Step 1 on the Initiative again |
| `stop-at-cap` | This run-id hit `parallelism.maxTasksPerRun` (across all Epics of the run) | Finish in-flight units, then stop ("Stop at the run cap") |
| `none` | Nothing actionable | Route any `unstaged` child, then `list-needs-human` + `check-epics-closeable`, then Step 4 |
| `skip` | Epic is legacy | Say so (quote `reason`), then Step 4 |

- **When running concurrently** ("Concurrency"): widen with the pool queries while lane
  headroom remains, and always run `list-parallel-ready` (same `--run-id`) right after
  every `merge-pr` and every `finish-lld` — those events unblock siblings and create
  `development` units.
- **Gate A WIP cap** is enforced in code; a `none` carrying `product_cap` means product
  work was deferred — report it in Step 4 (`references/gates.md`, "Gate A WIP cap").
- **A `blockedBy` edge is not a whole-child stop.** It usually constrains `development`
  onward, not a standing child's `product`/`architecture` — start the design stage
  concurrently and sequence only the dependent stages. Keep the native edge.
- **Keep `epic-<n>` from rotting against `main`.** At the start of each invocation on a
  non-standing Epic, and again after every third merge of a sibling Task or whenever `main`
  moved under it, run `python3 "$SDLC" sync-branch <epic> --unit epic --repo-path <p>`; a
  `conflict` has no stage agent (`references/parallelism.md`, "Git-conflict handling"). It also
  keeps every new phase/Task branch, cut from `epic-<n>`, close to `main`.
- **Before ending on `none`:** `list-needs-human` (skim each reason; clear a stale one
  with a comment), `check-epics-closeable` (idempotent) and `audit-issues --epic <n>` (`--initiative <n>` on an Initiative)
  (run each flagged issue's `repair` command, filling any `<P>`/`<T>`; never re-create
  it). All feed Step 4.
- **When `check-epics-closeable` names an epic and `pipeline.epicClose.auto` is on**,
  close it yourself: `close-epic` (reconciles) → run the exploratory pass →
  record it only if it ran clean → clean the epic worktree (the pass leaves it dirty) →
  `close-epic` again (merges) →
  `teardown-epic-stack <n>`. Escalate instead on the cases in "What you decide". Full
  mechanics: `references/epics.md`, "Epic closing". With the toggle off, just report it.

### Closing an Initiative

When `next-action` on the Initiative reports in a `none` `reason` that every cut Epic is
closed:

1. `python3 "$SDLC" check-initiative-closeable <initiative>`.
2. Delegate `sdlc:initiative-close`: it starts the delivered application and validates
   it against every requirement in the Initiative's `product.md`, and always records
   `record-initiative-verification --outcome met|unmet`.
3. **All met** (`clean`) → `python3 "$SDLC" close-initiative <initiative>` (one call — an
   Initiative has no branch). It refuses while the latest record is `unmet`.
4. **Anything not met** (`rework`) → file the gap (a Task against the relevant Epic, or
   judge it out of scope and say why) and stop; re-run from step 2 once fixed.

No human gate here — Gate A already approved `product.md`.

## Step 2 — Claim it

**Scope alignment before `product` — ask first.** When a unit enters `product` for the
first time (no `product.md` on its branch — a Product-Roadmap Task or a standing child),
do not claim or delegate yet. Read the issue and thread, state back what it covers /
excludes / leaves open, and ask the operator the genuine ambiguities in one
`AskUserQuestion` batch. Pass the answers verbatim into the `product` prompt. Skip on a
rework round or a resume where `product.md` exists. Engineering-driven work never
reaches this step. Detail: `references/design-doc-rules.md`, "Scope alignment before
`product`".

Then:

```bash
python3 "$SDLC" start-stage <n> --role <role>
```

`<role>` is `next-action`'s `stage` verbatim. It creates (or resumes from origin) the
worktree, then claims — never `claim` before the worktree exists. **At a non-standing Epic's first touch,
run `python3 "$SDLC" worktree-add <epic> --unit epic --repo-path <p>`** (cut-phase-tasks and
open-arch-revision already do; this covers an Epic that predates them) — every child's worktree
is cut from `origin/epic-<n>`, which must exist first. **When `pipeline.stack.enabled`, also run
`provision-epic-stack <n>`** — the epic's e2e-running children and closing run use that stack
(`references/parallelism.md`, "Per-epic isolated stack").

## Step 3 — Run this stage, then the next, then the next

Delegate to exactly **one** fresh subagent of the role's `subagent_type` (this plugin's
`sdlc:<role>` agents). There is no orchestrator-direct review path.

| Role | Trigger | `subagent_type` | Model (policy) | Doc it owns |
|---|---|---|---|---|
| `product` | `stage:product` (Product-Roadmap Task or standing child) | `sdlc:product` | opus | `issue-<n>/product.md` |
| `product-review` | right after `product` | `sdlc:product-review` | opus | none (comment); blocker bounces `product`, clean → Gate A |
| `architecture` | `stage:architecture` (Architecture-phase/revision Task or standing child) | `sdlc:architecture` | opus | `epic-<e>/architecture.md` for an Epic's phase-Task (authored on its `issue-<n>` branch, merged into `epic-<e>` by the design PR), else `issue-<n>/architecture.md`; creates no issues; owns the architecture-depth assessment |
| `arch-review` | right after `architecture` (on the design PR for an Epic's phase-Task) | `sdlc:design-review` | opus | none (comment + PR comment) |
| `lld` | `stage:lld` (LLD-phase Task) | `sdlc:lld` | opus | `epic-<e>/lld.md` (authored on its `issue-<n>` branch), one `## Task <KEY>: <title>` section per Task; creates no issues |
| `lld-review` | right after `lld`, on its design PR | `sdlc:design-review` | opus | none — **mandatory, never confidence-skipped, auto-merges on clean, no human gate**; one pass over the whole doc, also judges the Task carving |
| `development` | `stage:development` (Task or standing child) | `sdlc:development` | sonnet | none — PR description + `record-local-ci` attestations; a normal Task writes unit tests only |
| `pr-review` | right after `development` hands off | `sdlc:pr-review` | opus | none (comment); never bounces a normal Task for integration/e2e coverage owned by the standing Tasks |

**There is no `testing` stage** (merged into `development`). Nothing writes the `Testing`
Stage value; it is still read so a stranded child is picked up.

**Model.** The Agent guard sets each agent's model from
`${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json`; don't pick one. Tier aliases only — never
pin a version (no `ANTHROPIC_DEFAULT_OPUS_MODEL`, no versioned id under `pipeline.models`).
Retune the policy only via the retro, with a dated reason in `references/history.md`.

**Where rules live.** A rule binding every stage lives in `references/stage-playbooks.md`;
a rule binding some roles lives in the narrower reference those roles' agent files name;
a rule binding one stage (incl. its exit actions) lives in its `agents/<role>.md`. No
role loads another skill.

### The delegation prompt

The agent must *have* (not necessarily be pasted):

1. The unit's number, title, body, full comment thread; for a phase-Task also its
   parent's number, title, body. **For anything large, give the `gh` command that
   fetches it** — pasted threads truncate prompts.
2. The exact doc path it owns (e.g. `<docRoot>/epic-<e>/lld.md` for an Epic's phase-Task,
   `<docRoot>/issue-<n>/product.md` otherwise). **For a functional
   Task's `development` and `pr-review`**, tell it to read its design with
   `python3 "$SDLC" lld-section --epic <parent> --task <n> --repo-path <worktree>` and
   not to read the whole `epic-<parent>/lld.md`.
3. For `lld`: the Epic's `architecture.md` is the design source of truth; the
   fits-vs-deviates call is its first move.
4. The worktree: "Work in `<worktree-path>` (`<worktrees.root>/<devPrefix><n>`, default
   `/tmp/sdlc-dev-<n>`) — use absolute paths, or a standalone `cd` there; never chain
   `cd <path> && …` compounds (they trip the macOS sandbox permission prompt). Never touch
   the main checkout for anything on this unit." For `development` add: small local commits,
   one push before `open-dev-pr`; if a push is rejected, stop and report; and, when the run
   has a test-env helper script, name it (it must not rebuild network/PG/volumes by hand).
   **For `pr-review`**
   the worktree is its own detached one, never the unit's development worktree (its
   mutation probe must not touch the tree a resumed `development` continues in): make it
   with `git -C <repo-root> fetch origin && git -C <repo-root> worktree add --detach
   <worktrees.root>/<reviewPrefix><n> origin/issue-<n>` and remove it when the review ends
   (`references/parallelism.md`, "Parallel PR review").
5. For any review computing a diff: `git fetch origin` first and diff against `origin/`
   refs, never a local branch.
6. Any command that can outlast the default tool timeout needs `run_in_background` or
   an explicit ≥600s outer timeout. A `development` or run-only agent that backgrounds a
   suite: brief it to stay alive until the suite's exit marker and not hand back early.

**Keep the prompt minimal.** Never restate rules the agent reads from its own agent file
and `references/stage-playbooks.md` — it pays twice and invites the agent to cite your
prompt as a source (`references/stage-playbooks.md`, "Attribution is
falsifiable"). The first line is the header the hooks parse (`EPIC` is the Initiative/Epic
you are driving). Template:

> ROLE: `<role>` ISSUE: `<n>` EPIC: `<epic>`
> Unit: #`<n>` — `<title>`. Worktree: `<path>` (`cd` there first). Doc you own: `<doc-path>`.
> Then the 2–3 unit-specific facts only: the finding to fix, the deviation to judge, the
> sibling PR that just landed. For body/thread/children, the `gh` command that fetches them.

**Track stage agents** for resume-based rework: note each `Agent` call's returned ID
against its role for this unit (session-scoped, never written to GitHub). Send a
finding back with `SendMessage` **yourself**, never via a relay fork. Discard the
mapping once the unit reaches a stopping point.

**Keep your own context lean** — it is re-read every turn for the whole run. To search
or read across the codebase yourself (resolving an ambiguity, reconciling a conflict,
locating something), dispatch a read-only `Explore` subagent and act on its conclusion
instead of grepping and reading inline.

### After the subagent returns

**Read its result.** The last line of its final message is `SDLC-RESULT: {"issue": <n>,
"stage": "<stage>", "outcome": "<outcome>"}`, on a standing child optionally with
`"next"`/`"why"` (`references/stage-playbooks.md`, "The handback is terse"). Route on
`outcome`:

| `outcome` | Do |
|---|---|
| `done` / `clean` | The next step per the agent file's routing; `transition` (below) before the next stage. A `pr-review` `clean` → you run `merge-pr <pr> --issue <n> --run-id "$RUN_ID"` (behind-base refusal: `references/parallelism.md`, "Git-conflict handling") |
| `rework` | Route the finding (`references/rework.md`) |
| `blocked` | Clear the named blocker: resume the stage that owns the question, `open-arch-revision` for a design that does not fit, `mark-blocked` for a cross-issue dependency |
| `needs-human` | Ask the operator if present; else `mark-needs-human <n> --reason "..."`, park, Step 1 |
| `failed` | Inspect the worktree and result yourself, then resume the agent |

**Verify, don't trust** — before delegating the next stage:

```bash
python3 "$SDLC" transition <n> --expect-stage <s> [--pr <pr>] --repo-path <the unit's worktree>
```

`<s>` is the review role about to start (`arch-review`, `lld-review`, `pr-review`, …) or
the stage just finished; `--pr` is required with `pr-review`. **Always pass `--repo-path`
the unit's own worktree** (a phase-Task's `issue-<n>` worktree included) — `verify-exit`
reads that branch's HEAD, and from the main checkout it falsely reports the doc missing.

- Proceed only on `ready: true`. Otherwise act on `stopped_at` (exit 1 at `verify-exit` too):
  - **`handoff_marker_present: false`** (a `development` handoff) → resume `development`
    to post its `handoff-to-pr-review` marker; do not start `pr-review`
    (`references/parallelism.md`, "The two queue markers").
  - **`conflict`** → `references/parallelism.md`, "Git-conflict handling".
  - A failed `verify-exit` → the previous stage's exit action did not run; resolve it
    before dispatching anything.
- **Sync before resuming a rework or run-only agent — that is yours**, since stage agents
  are denied `sync-branch` (the denial tells them to stop with `blocked` naming
  `sync-branch <n>`; never sync under a live agent — `references/parallelism.md`, "Working
  on a branch"). After a sibling merges, a `merge-pr` `behind_base` (or a stale
  local-CI attestation) means `sync-branch <n>`, then resume `development` to re-run and
  re-attest on the new head.
- `pass-gate`/`skip-gate`/`waive-gate` reconcile or merge internally — no sync after them.
- An Epic's `architecture`/`lld` phase-Task: `transition` raised the design PR; give the review
  agent its number (`steps.open-design-pr.pr`) — it reviews on that PR.
- A `pr-review` reporting its mutation probe was **denied by the auto-mode classifier** (a
  transient `src` edit): relay it so the operator can add an allow rule — do not treat the
  missing probe as a clean signal.
- Confirm the previous stage left a comment on the issue; if not, get one.
- On an LLD-phase Task's clean `lld-review` → `finish-lld` ("Cutting an Epic's
  phase-Tasks").
- A handoff that proposes a new issue (a split, a finding this unit will not fix) → file
  it yourself (`references/rework.md`, "Where a finding this unit will not fix goes").

Each stage performs its own exit actions (`agents/<role>.md`; routing table in
`references/stage-playbooks.md`, "Stage exit actions live in the agent files"). **Yours,
after the agent returns:** opening a human-review gate (or skipping/waiving it), the design PR and
the review `start-comment` (via `transition`), `merge-pr` after a clean `pr-review`, the
closing-verification records (`references/epics.md`, "Epic closing"), and `finish-lld`.

Repeat Steps 2–3 until the unit is merged or closed, blocked, or needs a human.

### Routing a standing child

A standing child's flow is decided as it goes, never planned upfront: at pickup
(`next-action` → `route`) and after every stage returns `done`/`clean`, pick the next stage
from the issue and the last `SDLC-RESULT`'s `next`/`why`. Run a stage only when it earns
its place:

- `product` — only when a product or business decision is open.
- `architecture` — only for a new component or interface, a data-model change, a
  cross-module change, or an unclear approach.
- `product-review` / `arch-review` — skip when the artifact before it is trivial.
- `pr-review` — the strong default. Skip (`--to merge`) only for a trivially low-risk
  change, saying why in `--reason`; never after `pr-review` bounced it.
- Otherwise the default next stage.

To skip ahead after a stage returns: `transition <n> --expect-stage <the stage just
finished>`, then `route <n> --to <stage> --reason "<one line>"`, then Step 2 with
`--role <stage>`. At pickup the child has no Stage and nothing to verify: go straight to
`route` (or to Step 2 with `--role product` when it needs the full flow). To skip
`pr-review`: `verify-exit <n> --expect-stage pr-review --pr <pr>`, `route <n> --to merge`,
then `merge-pr` (the route marker is its evidence). `route` only moves forward; when a
later stage finds an open product decision, `set-stage <n> --stage product` and run a
fresh `product`.

### Stop at the run cap — reset your context between batches

`parallelism.maxTasksPerRun` caps how many units this run drives to a **terminal state**
(a merge, or an Epic reaching `epic:architected`). `next-action` and
`list-parallel-ready` count per `--run-id` and enforce it — one count across the Initiative
and every Epic it descends into.

- **`0` = unlimited**; `--run-id` may then be omitted.
- On `stop-at-cap`: finish the unit in flight, then stop — do not start another or call
  `next-action`/`list-parallel-ready` again. Report what you completed and that work
  remains; the next `/sdlc:run` run (fresh run-id, fresh context) resumes exactly
  there.
- **Never capped:** `resume`, `pass-gate`, `address-gate-feedback`. Only fresh
  `delegate` work and the dev-lane pool count.
- The cap only batches throughput — it never changes Task carving or `blockedBy` order.

## Step 4 — Report back to the user

When Step 1 finds nothing actionable (or you stopped at the cap): summarize every unit
touched, stages run, rework and resume cycles per pairing, what changed, and each unit's
current state. For an Initiative run, group by Epic (cut, run, parked, closed, blocked and on
what) and end with the Initiative's own `none` reason. Above the fold:

- Every `needs-human` unit **with its reason text** (flag reasons that look stale).
- Every blocked unit and what it waits on.
- Every gate-pending unit: gate PR, doc, level, whether feedback was addressed.
- Every merged PR and closed issue; every epic newly `epic:architected`; every Epic cut,
  run to completion or parked this run.
- Any epic `check-epics-closeable` newly notified; any `product_cap` deferral.
- Every issue `audit-issues` flagged (an Epic with no phase-Tasks included) and whether you repaired it.
- One cost line: `python3 "$(dirname "$SDLC")/sdlc_metrics.py" report --run <run-id>` →
  `totals` (est. USD, tokens, tool calls, peak context) for this run's agents.

## Step 5 — Retrospective (only when the operator asks)

Never decide on your own that a retro is due. Between retros, note each friction finding
as you see it — bouncing pairings (`pairing-counts`), docs too thin for the next stage,
dead references, gates too strict or loose — with the plugin version it was seen on
(`show-config` → `plugin`). The operator decides where parked findings live; never file
them as issues unless told to.

When the operator runs the retro: sweep recently merged units' handoff comments and docs
for that friction. Fixes go to the **plugin repo** (`snitesh91/sdlc-pipeline`); **this
file and its references are the primary fix target.** Present findings in chat and ask
before editing. Once approved:

1. In a clone of the plugin repo: `git checkout -B retro/<date> origin/main`, edit
   `skills/run/SKILL.md` / `references/*` / `agents/*` / `hooks/*`, add a 1–2 line entry
   to `references/history.md`, commit, push, merge to `main` (PR, or fast-forward if the
   operator says so), and tag it. An unpushed edit is a failed retro. **A change to
   `scripts/` or `hooks/` must pass `(cd scripts && python3 -m pytest -q)` and
   `python3 -m pytest -q hooks/tests` before pushing.** A control-plane fix also gets a regression test plus a positive
   control; run both against the pre-fix `sdlc_next.py` — the regression must go red,
   the control must not.
2. In the driven repo: bump the plugin pin (the marketplace `ref` in
   `.claude/settings.json`, and the `SDLC_PIPELINE_REF` Actions variable) to the new tag
   and commit it, naming the retrospective.

**Bump the pin only when no run is live on the driven repo.** The new version loads in
the next session (after `claude plugin marketplace update sdlc-pipeline`).
