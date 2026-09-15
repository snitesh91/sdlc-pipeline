---
name: sdlc-pipeline
description: Use when the operator invokes /sdlc-pipeline <epic> to drive a GitHub-Issues epic and its child issues through the auto-SDLC stages to merge, or asks to resume, unblock, retro, or continuously run that pipeline. Requires an sdlc-pipeline.config.json in the target repo and the sdlc-* agent definitions.
model: opus
---

# Auto SDLC over GitHub Issues

One invocation drives one epic's actionable work stage by stage toward a merge. The
main agent (this conversation) is the orchestrator: for each unit it delegates one
stage to one subagent, waits, verifies, and decides what's next. Parallel means more
than one child's handoff loop is live at once, never that anything decides on its
own. GitHub fields and comments are the **visibility log** and crash-resume point;
the committed docs under `<docRoot>/issue-<n>/` and `epic-<n>/` are the **source of
record**. Comments stay short and point at the docs — hard caps of 2,000 characters
for a stage handoff and 6,000 for a review or evidence-carrying comment
(`references/stage-playbooks.md`, "Comment size is a contract").

**This skill is a living process document.** Friction, dead references, and better
gating decisions are fed back into this file and its references via the Step 5
retrospective or direct operator feedback — never patched mid-cycle by a stage agent,
which flags problems in its handoff instead.

## Reference files — read on demand, not upfront

| File | Read it when |
|---|---|
| `references/parallelism.md` | Starting or resuming any child work or review pool; anything touching worktrees, branches, `sync-branch`, git conflicts, merge freshness, or an agent dying mid-stage |
| `references/gates.md` | Opening, checking, passing, or skipping a human-review gate; addressing gate feedback |
| `references/rework.md` | A review returned a finding, a stage reported ambiguity, a bounce may trip the escalation valve, or you are writing a replacement agent's resume message |
| `references/stage-playbooks.md` | Delegating any stage (its exit actions live here); **every stage subagent is told to Read this one** — the rules that bind every role: docs, citation, one-turn-finish, commenting, rework |
| `references/design-doc-rules.md` | `product.md`/`architecture.md` content rules and the pre-`product` scope-alignment step — `product`, `product-review`, `architecture`, and `design-review` are told to Read it; `lld`, `development`, `pr-review`, `exploratory` are not |
| `references/verification-rules.md` | Proving a claim by running it, not asserting it — `architecture`, `lld`, `development`, `pr-review`, and `design-review` are told to Read it; `product`, `product-review`, `exploratory` are not |
| `references/review-fanout.md` | Subagent-dispatch discipline for a review stage's first round — `product-review`, `design-review`, and `pr-review` are told to Read it; the other five roles are not |
| `references/epics.md` | Epic-level phase, child sizing and footprints, deviation escalation, bug fast-track, epic closing, board status |
| `references/operations.md` | Token and repo access, issue taxonomy (fields), auto-merge policy, local-CI attestation |
| `references/continuous-mode.md` | Operator asked for unattended looping |
| `references/history.md` | A rule looks arbitrary and you want the incident behind it |

## The lifecycle model

**V2 (2026-09-14, redesigned 2026-09-15): the normal-epic flow is a different
lifecycle from V1, not an extension of it** — every V2 phase (an Initiative's
`product`, an Epic's `architecture` and `lld`) is now **its own plain child
issue** ("phase-Task"), not something the Initiative/Epic issue itself ever
runs. Standing and legacy profiles are untouched by this; only the (former)
"default profile" flow changed shape, described below as **Initiative-driven**.
See `README.md`'s "V2: Initiative-driven lifecycle" section for a high-level
summary.

**Why phase-Tasks, not an epic-self phase (settled 2026-09-15, replacing the
first V2 cut's epic/initiative-branch machinery):** every phase-Task reuses
V1's existing, already-battle-tested per-issue mechanics verbatim — its own
`issue-<n>` branch, `worktree-add`, `open-gate`/`pass-gate`, `record-design-
review` — with zero special-casing in the control plane for "is this an
Initiative" or "is this a V2 Epic". The Initiative/Epic issue itself becomes a
pure container: `decide_next_action` never delegates a stage to it directly:

```
initiative: [Product-Roadmap Task: product -> product-review -> Gate A] -> [orchestrator cuts Epics]
epic:       [Architecture-phase Task: architecture -> arch-review -> Gate B] -> [pass-gate publishes architecture.md, closes the Task; LLD-phase Task unblocks]
            [LLD-phase Task: lld (creates functional + the 2 standing Tasks) -> lld-review] -> [orchestrator: publish-doc, merge-lld-doc advances them, close-issue]
task:       development -> [pr-review] -> auto-merge -> CLOSED
```

Every Epic always carries two standing Tasks alongside its functional ones — an
Integration-test Task and an e2e-test Task, created by the LLD-phase Task the
same way as any other Task. They run after every functional Task has merged,
each writing whatever coverage is missing and fixing failures they find (the
same full pipeline any Task runs). **Epic close does not re-run what they already
proved on the same tree:** `close-epic` still requires both closing-verification
records (`record-epic-verification --kind e2e` and `--kind exploratory`), and when
its reconcile with `main` picked up nothing, the `e2e` record cites the e2e-test
Task's local-CI attestation instead of a fresh run. A reconcile that did pick up
commits means a different tree — run the suite. The exploratory pass always runs.

**Engineering-driven** — no product motivation, no Initiative, no `product.md` at
all. A bare Epic is created directly with its scope written manually in the issue
body. It skips straight to `architecture`, which **asks the operator clarifying
questions itself** if that manual scope is unclear (there is no upstream requirements
stage to have caught the ambiguity first) — everything after `architecture` is
identical to the Initiative-driven flow:

```
epic: [scope written manually, no product.md] -> architecture (asks clarifying questions if unclear) -> [arch-review] -> [Gate B] -> lld (creates Tasks) -> [lld-review]
task: development -> [pr-review] -> auto-merge -> CLOSED -> epic close (same two standing Tasks, same no-rerun reuse)
```

An Initiative *may* still wrap engineering-driven work for program-management
grouping — if so, it carries no `product.md` and `architecture` still reads only the
Epic's own manual scope, never an Initiative IRD.

**Standing profile** (`epicLevelPhase: false`, e.g. matched to `epic:standing`) —
**unchanged by V2**: never runs an epic-level phase; each child runs the full flow on
its own issue number, `lld` still task-level:

```
product -> [product-review] -> [Gate A] -> architecture -> [arch-review] -> [Gate B] -> development -> [pr-review] -> CLOSED
```

- **`product-review` is universal** on any unit that runs `product`** — an adversarial
  opus review of `product.md`. A blocker bounces `product` (looping until clean,
  backstopped by the escalation valve); a clean verdict goes to Gate A. See the
  `product-review` row in the stage table and `references/stage-playbooks.md`.
- **Gate A is profile-configurable.** A profile with `requiresHumanGateA: false` (a
  standing/RTB backlog) **auto-passes** Gate A on a clean `product-review` — the unit
  flows straight to the next stage, no human. The default (Initiative-driven) profile
  opens the human Gate A as before. See `references/gates.md`, "Gate A configurability".
- **The Gate B confidence bar is per-profile** — `gates.skipConfidenceThreshold`
  (default 95); a standing/RTB profile may lower it (e.g. 90). Unchanged by V2.
- A standing-profile **bug** enters at `architecture` (`references/epics.md`, "Bug
  fast-track") — the same shape engineering-driven work uses on the Initiative-driven
  side.
- A **legacy profile** (`driven: false`, e.g. matched to `epic:legacy`) is skipped
  entirely, Tasks included.
- Reviews (`product-review`, `arch-review`, `lld-review`, `pr-review`) run immediately
  after the stage before them and have no Stage value of their own.
- **A normal Epic's Tasks are never eligible before the Epic is
  `epic:architected`** — enforced by `next-action` and `list-parallel-ready`, even
  while the Epic itself is gate-pending, blocked, or needs-human.
- **Nothing spins off a separate ticket.** Every problem found before merge is fixed
  inline by resuming the subagent that owns the responsible stage
  (`references/rework.md`). Only three things pause a
  unit: a cross-issue `blockedBy`, a `needs-human` verdict, or an open gate. None of
  them pause the invocation — see "Looping".

### Cutting an Initiative's Product-Roadmap Task — new orchestrator responsibility

An Initiative never runs `product` itself. Immediately after creating the
Initiative issue, the orchestrator cuts its **one** Product-Roadmap Task:

```bash
python3 "$SDLC" create-issue --parent <initiative-n> --title "Product Roadmap" \
  --body "..." --type Task  # --label per pipeline.classification.task if label-based
```

This Task then runs the plain `unit="issue"` flow like any other issue —
`product` → `product-review` → Gate A — with **no special-casing anywhere in
the control plane**: `default_stage()` already gives an Initiative's child
`"product"` (not the default profile's `"lld"` — see its own docstring), so
`decide_next_action` picks it up exactly the way it picks up any fresh child.
Once its Gate A merges (its own `issue-<n>` branch, straight to `main`, same
shape a standing child's gate always had), `pass-gate` closes the Task — its
parent is an Initiative, so no next stage is claimed (see "Cutting an Epic's
phase-Tasks") — and
`decide_next_action` on the Initiative itself reports it via `reason:
"Product-Roadmap Task closed -- cut Epics..."` — see "Closing an Initiative"
for the mechanical check.

### Cutting Epics from an approved Initiative — new orchestrator responsibility

On the Product-Roadmap Task's Gate A passing, the orchestrator — not a
subagent — reads the approved `docs/sdlc/issue-<n>/product.md` and cuts it into
Epics:

- **Each Epic must be independently mergeable to `main` and independently
  shippable on its own** — an Epic that only makes sense once a sibling Epic has
  also merged is cut wrong. Same non-overlap spirit as the Task-footprint rule,
  applied here to shippability rather than files.
- Each Epic's issue body carries a pointer to the Initiative's IRD, plus its own
  explicit scope carve-out — the slice of the IRD this Epic covers.
- `sdlc_next.py create-issue --parent <initiative-n> --type Epic` per Epic.
  **Also pass whatever `pipeline.classification.epic` actually checks** — read
  `show-config` first: the sample config's default is label-based
  (`--label type:epic`), because `--type` alone only sets the native GitHub Issue
  Type field, and `create-issue` does not invent a label from the type name on its
  own. Skipping this makes the Epic uncreated-in-vain from `classify_unit`'s point
  of view — it exists, but nothing can tell it apart from a Task later. Every Epic
  is a native sub-issue of its Initiative, same invariant as Epic-number-mandatory
  below one level up.
  `--type Epic` is best-effort: custom GitHub Issue Types are organization-level
  only, unavailable on a personal repo at any plan tier, so `create-issue`
  silently skips the native-type write whenever `pipeline.classification.epic`
  is label-based (the label is the real signal there) — it only fails loudly
  when a kind has neither a provisioned native type nor a label rule to fall
  back on, meaning nothing could ever classify it later.
- This is the one point in the Initiative-driven flow that is **not** a subagent
  delegation — the orchestrator does it directly, the same way it already owns
  Step 1's routing decisions without delegating them.

### Cutting an Epic's phase-Tasks — new orchestrator responsibility

Immediately after cutting (or manually creating, engineering-driven) an Epic,
the orchestrator cuts its two phase-Tasks — this is what actually runs
`architecture` and `lld`, since a V2 Epic has no phase of its own:

```bash
python3 "$SDLC" create-issue --parent <epic-n> --title "Architecture phase" \
  --body "..." --type Task  # --label per pipeline.classification.task
python3 "$SDLC" set-stage <architecture-task-n> --stage architecture
python3 "$SDLC" worktree-add <architecture-task-n> --unit issue --base origin/main

python3 "$SDLC" create-issue --parent <epic-n> --title "LLD phase" \
  --body "..." --type Task
python3 "$SDLC" set-stage <lld-task-n> --stage lld
python3 "$SDLC" add-blocked-by <lld-task-n> --on <architecture-task-n>
```

- **`set-stage` is required for both phase-Tasks.** `set-stage` writes only the
  Stage field — no claim, no start comment — so the Task stays fresh until an
  actual `/sdlc-pipeline` invocation claims and delegates it. The
  Architecture-phase Task's `architecture` is not the default profile's
  `childEntryStage`, so `default_stage()` cannot guess it. The LLD-phase Task's
  `lld` is, but `next-action` holds back every **Stage-less** child of a V2 Epic
  until the Epic is `epic:architected`: those are the Tasks the LLD-phase Task
  creates, and they must wait for `merge-lld-doc` to advance them. An unstaged
  LLD-phase Task would be held back with them, forever.
- **`worktree-add --base origin/main` is required for the Architecture-phase
  Task** (and would be for the LLD-phase Task too, if it were stood up before
  its own turn) — `integration_base`'s auto-detection has no way to tell a
  phase-Task apart from an ordinary functional Task under the same
  (non-standing) Epic without inventing a new classification this project
  deliberately avoided; see `integration_base`'s own docstring. Getting this
  wrong cuts the Task's branch from the epic branch instead of `main`, which
  its Gate (targeting `main`) cannot then merge cleanly.
- **`add-blocked-by`** orders the LLD-phase Task after the Architecture-phase
  Task via the native `blockedBy` edge — the *only* thing enforcing this
  ordering; there is no epic-level gate for it anymore.
- **The Epic branch needs no setup step.** `publish-doc` creates `epic-<n>` on
  origin from `main` the first time it publishes (or pushes a branch that exists
  only locally), and `worktree-add --unit epic` pushes a fresh epic branch when
  it cuts one.
- **On the Architecture-phase Task's Gate B passing or being skipped**, the gate
  command finishes the Task itself. A gate-bearing child of a V2 Epic can only be
  its Architecture-phase Task, so `pass-gate` / `skip-gate` (`--repo-path` on
  `skip-gate`) claim no next stage. Instead they publish
  `docs/sdlc/issue-<n>/architecture.md` from the Task's own branch to
  `docs/sdlc/epic-<n>/architecture.md` on the epic branch — the one path every
  reader (the LLD-phase Task, functional Tasks) expects — post the doc link on
  the Epic, then close the Task (`close-issue`, which also removes its worktree). The result carries
  `phase_task_complete: true`. When the publish is not verified on origin, the
  Task stays open with `phase_task_complete: false` and a `reason`: fix it, then
  `publish-doc <n> --doc architecture.md` and `close-issue <n>`.
- **On the LLD-phase Task's `lld-review` coming back clean** (no gate — `lld`
  has no human review), publish, advance, then close — in this order, never
  closing first:
  ```bash
  python3 "$SDLC" publish-doc <lld-task-n> --doc lld.md
  python3 "$SDLC" merge-lld-doc <epic-n> --unit epic
  python3 "$SDLC" close-issue <lld-task-n>
  ```
  `merge-lld-doc --unit epic` advances every Stage-less Task under the Epic to
  `development` and marks the Epic `epic:architected`; only then does
  `next-action` hand those Tasks out.
- **Close with `close-issue`, never `mark-issue-closed`.** `mark-issue-closed`
  only sets the terminal fields in reaction to a close that already happened
  (the `issues: closed` Action job); as a close step it leaves the issue open
  with no Stage, and `next-action` hands it back out as fresh work.
- This is the one point in the Epic-driven flow that is **not** a subagent
  delegation — the orchestrator does it directly, same as cutting Epics from
  an Initiative one tier up.

## Setup — one shell, three values

The skill lives **outside** the repo it drives (symlinked or vendored). Everything
project-specific lives in the repo's `sdlc-pipeline.config.json` (see `README.md`).
Before the first command:

```bash
export SDLC="<stable-skill-path>/scripts/sdlc_next.py"  # the control plane (bootstrap, stable)
export GITHUB_TOKEN=$(cat <your GitHub token file>)     # classic PAT (ghp_)
cd <repo-root>                                          # so config + git resolve
```

**Two paths, resolved from two places — do not conflate them:**

- **`$SDLC` (control plane) is a stable bootstrap path** — the main checkout's
  `.github/sdlc-pipeline/scripts/sdlc_next.py`, or any fixed clone. It has to exist
  *before* the unit's worktree does (it is what runs `worktree-add`), so it can never
  live only inside a per-epic worktree. It is read-only tooling; it never makes the
  main checkout a git-write target.
- **`$SDLC_DIR` (agent-facing files: `references/`, `agents/`) is per-unit** — the
  skill submodule *inside the unit's own worktree*:
  `<worktree>/<pipeline.skill.submodulePath>` (default
  `/tmp/sdlc-epic-<n>/.github/sdlc-pipeline`, or the child's `/tmp/sdlc-dev-<n>/…`).
  `worktree-add` initialises that submodule and returns it as `skill_dir`;
  `sync-branch` re-syncs it to the branch's pin after every merge. Each epic's stage
  agents therefore read exactly the skill version its own branch pins — a submodule
  bump for one epic cannot change another epic's instructions mid-run, and the
  shared-main-checkout copy is never handed to an agent. Set `$SDLC_DIR` per unit
  from `worktree-add`/`sync-branch`'s `skill_dir` before delegating, and hand
  subagents the concrete `$SDLC_DIR` value in their prompt — each agent file's own
  "first move" names the `references/*.md` paths it reads relative to that; a
  repo-relative path won't resolve for them. (The config file is committed on the
  branch too, so it is already per-branch.) Mechanics and the git requirement:
  `references/parallelism.md`, "Concurrent multi-epic isolation".

## Deterministic control plane

Every mechanical GitHub read, decision, and mutation is owned by the control plane —
never hand-executed `gh`/GraphQL calls, never a hand-typed fetch/checkout/merge/push:

```bash
python3 "$SDLC" <command> ...
```

Every command prints one JSON object. Exit 0 = valid result, including "nothing to
do" and structured refusals (a sync conflict, a behind-main merge). Exit 1 =
operational failure: stop and report, never retry by hand.

| Command | What it owns |
|---|---|
| `next-action <epic>` | Pick the one unit to work (Step 1) |
| `list-parallel-ready <epic> --repo-path <p>` | Dev-lane pool: `lld`/`development` children safe to start/resume concurrently |
| `list-design-ready <epic> --repo-path <p>` | Design-lane pool: a **standing** epic's `product`/`architecture` children safe to start/resume concurrently (empty for a default-profile epic) |
| `list-ready-for-review <epic>` | Review pool: finished PRs awaiting `pr-review` |
| `worktree-add <n> [--unit epic]` | The unit's worktree, the one correct way: resumes from `origin/<branch>` when it exists, else branches off the integration base; initialises the skill submodule and returns `skill_dir` (the per-unit `$SDLC_DIR`) |
| `provision-epic-stack <n>` / `teardown-epic-stack <n>` | The epic's isolated runtime stack (own compose project, ports, env/secrets profile, DB data dir) — at epic start / at epic close; no-op unless `pipeline.stack.enabled` |
| `claim <n> --role <role>` | Stage + In Progress + start comment |
| `start-comment <n> --role <role>` | Start comment alone (`arch-review` / `lld-review` / `pr-review`) |
| `sync-branch <n> [--unit epic]` | Reconcile the branch with its integration base; structured conflict result |
| `merge-lld-doc <n> [--unit issue\|epic]` | `--unit issue` (default, V1): publish a normal-epic child's clean `lld.md` onto the epic branch, then **advance** it to `development`. `--unit epic` (V2): verify `epic-<n>/lld.md` is on `origin/epic-<n>` (put there by `publish-doc` from the LLD-phase Task), then advance every Stage-less Task under the Epic and mark it `epic:architected`. Both: Stage set, Pipeline Status cleared, never claimed |
| `set-stage <n> --stage <s>` / `add-blocked-by <n> --on <dep>` / `publish-doc <n> --doc <d>` | V2 phase-Task cutting and doc publishing ("Cutting an Epic's phase-Tasks"); `publish-doc` creates the epic branch on origin if it does not exist yet |
| `close-issue <n> [--repo-path <p>]` | Close an issue, set its terminal fields and release its worktree — the orchestrator's close for a V2 phase-Task (`mark-issue-closed` only reacts to a close). `close-epic`'s merge and `close-initiative` set the terminal fields themselves too |
| `verify-exit <n> --expect-stage <s> [--pr <pr>] [--unit epic]` | Post-handoff state check |
| `open-gate` / `check-gate` / `pass-gate` / `skip-gate` / `auto-pass-gate-a` | Human-review gates (`references/gates.md`); `auto-pass-gate-a` advances Gate A with no human review when the resolved profile sets `requiresHumanGateA: false`. On a V2 phase-Task (child of an Initiative or V2 Epic), `pass-gate`/`skip-gate` finish and close the Task instead of claiming a next stage |
| `open-dev-pr <n> ...` | Draft PR + Stage=PR Review + handoff comment (posts **no** queue marker) |
| `handoff-to-pr-review` / `record-pr-review` | The two review-queue markers |
| `record-local-ci --pr <pr> --suite <s> --sha <HEAD> --command <cmd> --output <file>` | `development`'s evidence-carrying local-CI attestation; `<s>` is a `requiredWorkflows[].suite` from config |
| `record-design-review <n> --role <r> --outcome clean\|rework` | Last action of **every** design review — the bounce marker `pairing-counts` reads |
| `pr-checks <pr>` / `merge-pr <pr> --issue <n>` | CI status / the only merge gate (refuses behind-base; reports `config_changed`) |
| `create-issue --parent <epic>` | The only issue-creation path |
| `mark-blocked` / `mark-needs-human` / `pause-for-epic-regate` | Park a unit (the first two also release its worktree) |
| `pairing-counts <n>` | Marker-derived escalation-valve strike counts, with the configured thresholds |
| `show-config` | Effective tunables (`pipeline` block over defaults) — read it once per invocation |
| `list-needs-human` / `check-epics-closeable` / `retro-check [--mark-done --count <n>]` | End-of-invocation sweeps |
| `sync-skill [--ref <ref>]` | Bump the skill submodule + re-vendor `.claude/agents/` (Step 5's manual bump/re-vendor, automated); stages both, does not commit |
| `close-epic <n>` / `record-epic-verification <n> --kind e2e\|exploratory` | Epic close, two-call shape (`references/epics.md`, "Epic closing") |
| `check-initiative-closeable <n>` / `record-initiative-verification <n> --summary` / `close-initiative <n>` | V2 Initiative close, one-call shape ("Closing an Initiative", above) |
| `auto-pass-gate` / `mark-feedback-received` / `mark-feedback-addressed` / `mark-todo` / `mark-issue-closed` | CI-triggered real-time paths (the gate-auto-advance workflow) |

**Branch-writing commands never touch the main checkout.** `sync-branch`,
`merge-lld-doc`, `pass-gate` and `close-epic` take a per-branch lock, then operate in
the branch's own live worktree or — when nothing holds the branch, the usual case for
an epic branch — an ephemeral worktree they remove afterwards. `--repo-path` is any
path inside the repository (where the worktree map is read from), not "the checkout
to write in"; a branch found checked out in the main checkout is refused with the
recovery recipe. `references/parallelism.md`, "Concurrent multi-epic isolation".

**What the CLI does not decide**, and this skill does: which stage owns a defect found
in rework, whether a bounce trips the valve, whether a reported ambiguity is genuine,
and all subagent prompt, doc, and PR prose. Thresholds it *reports* but does not
apply: the escalation valve (`pipeline.escalation`, default 3 → context-reset
replacement, 6 → `needs-human`) and the continuous-mode cycle cap.

## Epic number is mandatory

`next-action` and the pool queries **require** an epic number — an Initiative
invocation names the Initiative's own number the same way (`/sdlc-pipeline
<initiative>`); it is a lighter, product-only unit until the orchestrator cuts Epics
from it. The pipeline never scans the repo to decide whose turn it is. No number
named = a blocking question; ask before doing anything. One invocation per
Initiative/Epic; two invocations on the *same* one race — don't. Every Epic must be a
native sub-issue of its Initiative, and every Task a native sub-issue of its Epic, to
be picked; link with `create-issue --parent`.

## Config can move under you

The config lives in the repo and merges through the pipeline like any file.
**`merge-pr` returns `config_changed: true` when the merged PR touched it** — re-read
the config before the next stage. If the skill is vendored rather than symlinked, its
own files can move too; then re-read `SKILL.md` and any reference you're about to
apply.

## What you decide, and what you take to the operator

- **During `development` and `pr-review`: take the recommended fix
  yourself.** An agent that ends with "recommend X" has done the analysis — apply it.
- **Escalate exactly three things**: a product or scope call, an amendment to a
  **gate-approved** doc, and an escalation-valve trip (`mark-needs-human`). Plus, when
  `pipeline.epicClose.auto` is on, an epic close blocked by a Blocker/Critical closing
  delta, an open manual-testing bug child, or a verification that could not be run
  (`references/epics.md`, "Epic closing").
- **Never operator questions**: parallelism, worktrees, model tiers, review scoping,
  which stage owns a defect, and merging (`references/operations.md`, "PRs merge
  automatically"). Epic close itself is the orchestrator's too **when
  `pipeline.epicClose.auto` is on**; with it off, closing is the operator's call.

## Looping within an invocation

When a unit hits `blocked` / `needs-human` / an open gate: park *that unit* (comment,
fields, stop) and return to Step 1 for the next actionable unit in the same epic. The
invocation ends only when Step 1 returns `none`: every open child and the epic's own
phase is closed, blocked, needs-human, or gate-pending with nothing to address. Then
Step 4. `none` says nothing about other epics. Unattended operation:
`references/continuous-mode.md`.

## Concurrency — summary

Default-profile epic-self `product`/`architecture` is strictly sequential in its own
`epic-<n>` worktree (a single epic-level unit — never fanned out).
`lld`/`development` fan out to `parallelism.devLane` children and
`pr-review` to `parallelism.prReview` PRs (config; default 3 each), every branch in
its own worktree, eligibility via the pool queries. A **standing** epic has no
epic-level design phase — each child runs its own `product`→`architecture` too, and
those fan out to `parallelism.designLane` children (config; default **2**, below
devLane because both stages run opus) via `list-design-ready`. Rework is **one
development thread per issue**, always. Full mechanics: `references/parallelism.md` —
read it before starting any concurrent work.

**Across epics:** N invocations on N *different* epics may run at once — every
branch-writing command locks its branch and works in its own worktree (never the
main checkout), each unit's agents read their own skill copy, and each epic can own
its own runtime stack. The one thing that still races is two invocations on the
*same* epic. `references/parallelism.md`, "Concurrent multi-epic isolation".

## Step 1 — Pick the one unit to work

```bash
python3 "$SDLC" next-action <epic>
```

Every result except `skip`/`none` carries `unit` (`issue` or `epic`); pass it through
to any command taking `--unit`.

| `action` | Meaning | What to do |
|---|---|---|
| `resume` | A real claim happened and the session died mid-stage (never a CI-advanced gate) | Resume at the returned `stage` from comments + committed docs, per Step 3. If you are *currently* driving that unit in this session's lane, it isn't crashed — skip and survey again |
| `delegate` | The epic itself, or a child, is ready | Claim (Step 2), delegate (Step 3) |
| `pass-gate` | A gate PR was merged | `references/gates.md`, "Passing a gate" — pass `issue`/`gate_pr`/`stage`/`unit` verbatim |
| `address-gate-feedback` | Gate PR has unresolved threads or new comments | `references/gates.md`, "Addressing gate feedback" |
| `none` | Nothing actionable in this epic | `list-needs-human` + `check-epics-closeable`, then Step 4 |
| `skip` | Epic is `epic:legacy` | Say so (quote `reason`), then Step 4 |

**Widen Step 1 with the pool queries** whenever lane headroom remains — typically
after a `development` handoff (`list-ready-for-review`) or when `next-action` returns a
child (`list-parallel-ready`). For a **standing** epic, also run `list-design-ready`
to fan out children still in `product`/`architecture` (up to `parallelism.designLane`,
default 2); for a default-profile epic it returns empty, since epic-self design is a
single serial unit. **Always run `list-parallel-ready` immediately after
every `merge-pr` and every `merge-lld-doc`**: a merge is the event that unblocks a
sibling, and `merge-lld-doc` is the event that turns a child into a `development`
unit — an un-requeried child idles through a whole stage.

**Product WIP cap — at most `pipeline.productWip.maxGateAPending` (default 5) units
awaiting Gate A, repo-wide.** `next-action` and `list-design-ready` will not start a
*fresh* `product` delegation (an epic's own, or a standing child's) while that many
open units already sit at Stage `Product` with an open Gate A; they loop to the next
actionable unit instead, and a `none` reached that way carries `product_cap` naming
what was deferred — report it in Step 4. Resumes, rework rounds, `pass-gate` and
`address-gate-feedback` are never gated: passing gates is what drains the queue.
Enforced in code; the why is in `references/gates.md`, "Gate A WIP cap".

**A `blockedBy` edge is not automatically a whole-child stop.** It usually constrains
`development` onward, not `lld` — start the design stage concurrently and sequence
only the dependent stages. Keep the native edge; `list-parallel-ready` reads it.

Before ending on `none`: `list-needs-human` (skim each reason; clear a stale one with
a comment) and `check-epics-closeable` (idempotent). Both feed Step 4.

**When `check-epics-closeable` names an epic and `pipeline.epicClose.auto` is on**
(`show-config`), the orchestrator closes it rather than handing it to the operator:
run `close-epic` (first call reconciles), run the two closing verifications, record
each only if it ran clean, then run `close-epic` again to merge, then
`teardown-epic-stack <n>` (no-op unless the stack was provisioned). Escalate instead of
closing on a Blocker/Critical delta, an open manual-testing bug child, or a
verification that could not be run — full mechanics and the escalation cases in
`references/epics.md`, "Epic closing". With the toggle off, `check-epics-closeable`
just feeds Step 4 for the operator to act on.

### Closing an Initiative — V2, fully automated

`decide_next_action` on the Initiative notices once every Epic cut from it is
closed, and says so in a `none` result's `reason` rather than dispatching
anything itself — same "next-action surfaces it, the orchestrator acts" shape
as `check-epics-closeable` above, one tier up. On seeing that:

1. `python3 "$SDLC" check-initiative-closeable <initiative>` — confirms every cut Epic
   is actually closed (mechanical re-check, not a re-derivation).
2. Delegate `sdlc-initiative-close` — a product-manager-role pass that starts the
   delivered application (every cut Epic is already merged to `main`) and validates it
   against every requirement in the Initiative's own `product.md`. It always records
   its verification, met or not (`record-initiative-verification`).
3. **Every requirement met** → `python3 "$SDLC" close-initiative <initiative>` — one
   call, not two: an Initiative has no branch of its own at all (its Gate A doc merged
   straight to `main` via its Product-Roadmap Task, see "Cutting an Initiative's
   Product-Roadmap Task" above), so there is nothing to reconcile or merge here, just
   the issue to close.
4. **Anything not met** → file the gap (a Task against the relevant cut Epic, or judge
   it out of scope and say why) and stop — do not close. Re-run from step 2 once fixed.

**No human gate anywhere in this flow** — Gate A (the human review of `product.md`
itself) already happened before any Epic was cut; validating the delivered result
against that already-approved doc is the pipeline's own job, same as an Epic's e2e+
exploratory pair needing no separate human sign-off either.

## Step 2 — Claim it

```bash
python3 "$SDLC" claim <number> --role <role>
```

`<role>` is `next-action`'s `stage` verbatim. **Create the unit's worktree before
claiming** (`references/parallelism.md`, "Ordering rule"), always via the control
plane:

```bash
python3 "$SDLC" worktree-add <number> [--unit epic]
```

It fetches, resumes from `origin/<branch>` when that branch exists (never fresh off
`main`, which drops already-pushed work), and otherwise branches off the unit's
integration base. Never hand-type `git worktree add`. Its `skill_dir` is the unit's
`$SDLC_DIR` (Setup). **At an epic's first touch, also `provision-epic-stack <n>`**
when `pipeline.stack.enabled` — the epic's e2e-running children and its closing run
use that stack, never the shared dev one (`references/parallelism.md`, "Per-epic
isolated stack").

**Scope alignment before `product` — ask first, author second.** When the unit is
entering `product` for the first time (no `product.md` on its branch yet — an
Initiative's own, or a standing child's), do **not** claim or delegate yet. Initiative
issues are mostly one-liners that do not carry the scope the operator has in mind, and
scope discovered after `product.md`, Gate A, the Epic cut, `architecture.md` and Gate
B cascades rework through every downstream doc and every Epic cut from it. So: read
the issue and its thread, state back a concise "this Initiative covers / excludes /
the decisions I see open" summary, and put the genuine ambiguities to the operator as
questions (`AskUserQuestion` — scope boundaries, must-haves vs out-of-scope, decisions
the one-liner leaves open) in one batch. Feed the answers verbatim into the `product`
delegation prompt. This is a pre-product interaction, earlier than and distinct from
Gate A, and it is one of the three things you *do* take to the operator (a product or
scope call). Skip it only on a rework round or a resume where `product.md` already
exists. **Engineering-driven work never reaches this step at all** — no Initiative,
no `product.md`; its scope is written manually on the Epic directly, and
`architecture` (not the orchestrator) asks clarifying questions if that's unclear.
Detail: `references/design-doc-rules.md`, "Scope alignment before `product`".

## Step 3 — Run this stage, then the next, then the next

Delegate to exactly **one** fresh subagent of the role's `subagent_type`. Each
`sdlc-*` type is an agent definition in the repo's `.claude/agents/` (persona,
procedure, refusal criteria — see `README.md`, "Prerequisites"). There is no
orchestrator-direct review path.

| Role | Trigger | `subagent_type` | Model | Doc it owns |
|---|---|---|---|---|
| `product` | `stage:product` (an Initiative's Product-Roadmap Task, or a standing child) | `sdlc-product` | opus | `issue-<n>/product.md` — same path for both; the Initiative itself owns nothing |
| `product-review` | right after `product` | `sdlc-product-review` | opus | none (comment only) — universal; blocker bounces `product`, clean goes to Gate A |
| `architecture` | `stage:architecture` (an Epic's Architecture-phase Task, either Initiative-driven or engineering-driven, or a standing child) | `sdlc-architecture` | opus | `issue-<n>/architecture.md` on the Task's own branch; `publish-doc` copies it to `epic-<n>/architecture.md` once Gate B passes (V2: no longer creates/sizes anything below it — no children, no Tasks; also owns the architecture-depth assessment, moved here from `product`) |
| `arch-review` | right after `architecture` | `sdlc-design-review` | opus | none (comment only) |
| `lld` | `stage:lld` (V2: an Epic's LLD-phase Task — creates the functional Task issues; V1/standing child: task-level, unchanged) | `sdlc-lld` | sonnet | `issue-<n>/lld.md` on the Task's own branch (V2, one subsection per functional Task); `publish-doc` copies it to `epic-<n>/lld.md` once `lld-review` is clean. V1/standing: `issue-<n>/lld.md` unchanged |
| `lld-review` | right after `lld` | `sdlc-design-review` | opus | none — **mandatory, never confidence-skipped**; V2: one pass over the whole epic-level document, also judges whether the Task-carving itself was sound |
| `development` | `stage:development` (a Task, V2, or a child, V1) | `sdlc-development` | sonnet | none — the PR description is the record; suite evidence is the `record-local-ci` attestations. V2: a normal Task writes unit tests only; the epic's two standing Integration-test/e2e-test Tasks carry full coverage for the whole epic |
| `pr-review` | right after `development` hands off | `sdlc-pr-review` | opus | none (comment only). V2: reviews what's present only — does not bounce a normal Task for integration/e2e coverage that's deferred to the two standing Tasks |

**There is no `testing` stage.** It was merged into `development` on 2026-09-12: the
implementer writes and runs its own tests, and `pr-review` judges whether those tests
are any good (`references/history.md`, that date). Nothing writes the `Testing` Stage
value any more; it is still *read* so a child an in-flight epic stranded there is
picked up rather than lost.

`next-action`'s `stage` says directly whether a child runs `lld` or full
`architecture`. The Model column is the default; the config's `pipeline.models.<role>` overrides it
(`show-config`). Pass the result as the `Agent` call's `model` param —
`opus`/`sonnet` are harness tier aliases, not version pins, **and the aliases must
track the latest model of their tier**: never redirect them to a fixed version (no
`ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-<x>` in the driven repo's
`.claude/settings.json`, no versioned id under `pipeline.models`). A pinned alias
silently runs the pipeline on a stale model as newer ones ship (operator, 2026-09-12;
the earlier 4.8 pin is retired). The tier is pinned here
at the call site, not in the agent files, so one definition can run at two tiers and
a retune is a one-word edit. Opus sits where a mistake has no human in front of it:
the `product` and `architecture` authoring, the two last-checks-before-something-irreversible
(`lld-review`, `pr-review`), and the two adversarial reviews backstopped by a human gate
right after them (`product-review` before Gate A, `arch-review` before Gate B) — see
2026-09-14 below for why those two are opus, not a cheaper tier. Sonnet on the
review-backstopped, higher-frequency stages (`lld`, `development`). Retune in
`references/history.md` with a dated reason, not by guessing here — the latest
stage-by-stage evaluation is the 2026-09-12 entry there.

> **Reverted (operator, 2026-09-14):** `product-review` and `arch-review` moved back to
> `opus`. The 2026-09-12 move to `fable` was reasoned as "a cheaper adversarial pass" —
> that premise was wrong. Fable is priced at $10/$50 per MTok against opus's $5/$25 (see
> `references/history.md`, "Fable pricing"): **fable costs 2x opus, not less.** The
> 2026-09-12(d) Gate-B-confidence-skip resolution for a fable-run `arch-review` is now
> moot — `arch-review` is opus again, so the standard opus-confidence-skip applies with
> no special case.

**The table is the default, not a floor — downgrade a genuinely small task** (a
one-line config change, a typo fix, a rework round applying a fix already specified
verbatim). Judge the *work*, not the stage label. Four rules are not negotiable:

- **Say so in the stage's own issue comment** — tier used and a one-line reason. An
  undeclared cheap run reads as full rigour to the next agent.
- **Never downgrade a last-check-before-irreversible review** (`pr-review`,
  `lld-review`) by default. A trivially bounded diff may be reviewed cheaply, but as an
  explicit judgement about the diff.
- **Never downgrade after a bounce.** Escalation-valve rounds run at table tier or
  above.
- **Keep the `subagent_type`.** The `sdlc-*` files carry the refusal criteria that
  make a stage a gate. Change the tier, not the type.

An upgrade above the table is the same deal in reverse: allowed, stated in the
comment.

No role loads an external skill plugin. `development`'s working discipline —
test-first, root-cause-every-failure, verify-by-running — is inlined at the top of
`agents/sdlc-development.md`, so the pipeline has no dependency on any plugin being
installed in the driven repo.

**One rule, one home — and the home is chosen by scope** (2026-09-13, refined
2026-09-14). A rule that binds every stage (citation discipline, the no-park contract,
the doc set, commenting) lives once in `references/stage-playbooks.md`, which every
agent Reads first. A rule that binds some, but not all, roles lives once in the
narrower file that names exactly which roles read it: `references/design-doc-rules.md`
(the `product.md`/`architecture.md` content rules, scope alignment before `product` —
`product`, `product-review`, `architecture`, `design-review`), `references/verification-rules.md`
(what counts as verification, run-the-thing/completeness-sweep — `architecture`, `lld`,
`development`, `pr-review`, `design-review`), and `references/review-fanout.md`
(subagent-dispatch discipline — `product-review`, `design-review`, `pr-review`). A rule
that binds one stage — how that stage works, and **its own exit actions** — lives once
in that stage's `agents/sdlc-*.md`. Each agent's own file names exactly which
`references/*.md` files it Reads, so a rule reaches whoever needs it without being
stated twice or read by a role it does not bind.

This replaced "agent files carry persona, procedure and `tools:` only", which the skill
stated and did not follow: the no-park rule had four homes and had drifted into three
different strengths, the weakest being what the agent that stalled on 2026-09-13 was
reading. Exit actions were 459 lines of the playbook that every agent read to use one
eighth of; on 2026-09-14 the same shape recurred one level down — every role read the
altitude, verification, and fan-out rules regardless of whether its own role needed
them, so those three moved to their own files with each role's own file naming which
ones it Reads. Two homes for one rule really is how rules drift — the fix was to give
each rule exactly one home, not to move them all to the same file.

### The delegation prompt

Every delegation prompt **must** ensure the agent *has* (not necessarily that the
prompt *contains*):

1. The unit's number, title, full body, full comment thread. Epic-level: plus number,
   title, body of **every open child**. **For anything large, give the `gh` command
   that fetches it rather than pasting it** — pasted threads truncate prompts
   mid-instruction (`references/history.md`).
2. The role's own working discipline is inlined in its `agents/sdlc-<role>.md` and
   loaded when the agent Reads its definition — no external skill to invoke.
3. `$SDLC_DIR` in the prompt, so the agent's own "first move" instructions — a `Read`
   of `references/stage-playbooks.md` plus whichever narrower `references/*.md` files
   its own role names (design-doc, verification, or review-fanout rules) — resolve.
   **Plus** the exact doc path it owns (e.g. `<docRoot>/issue-<n>/lld.md`), spelled
   out. Don't paste any of these files. `$SDLC_DIR` here is the **unit's own** skill copy —
   the `skill_dir` that `worktree-add`/`sync-branch` returned for this worktree
   (Setup), never the main checkout's `.github/sdlc-pipeline`.
4. On genuine ambiguity: **stop and report the specific question in the final
   message** — never guess, create issues, or change fields.
5. For `lld` (V2: the Epic; V1/standing: the child): the Epic's `architecture.md` is
   the design source of truth; the fits-vs-deviates call is the first move.
6. The exact worktree to work in:
   > Work in `<worktree-path>` (`<worktrees.root>/<devPrefix><n>` for a child,
   > `<worktrees.root>/<epicPrefix><n>` for an epic's own stage — default
   > `/tmp/sdlc-dev-<n>` / `/tmp/sdlc-epic-<n>`) — `cd` there before any git command (`worktree-add` returns the exact path). Never touch the
   > main checkout for anything on this unit.

   For `development` additionally: small local commits, one push before
   `open-dev-pr`, never mark ready or merge; if a push is rejected, stop and report.
7. For any review computing a diff: `git fetch origin` first and diff against the
   `origin/` ref, never a local branch.
8. Any command that can outlast the default tool timeout needs `run_in_background`
   or an explicit ≥600s outer timeout — the agent cannot discover this without dying.

**Keep the prompt minimal — the agent re-derives the rest.** The list above is what
the agent must *have*, not what you must *type*. Every standing rule — citation
discipline, the no-park/one-turn-finish contract, the repo's Docker and testing
commands, the completeness-sweep rule, the exit action — is already in front of the
agent the moment it Reads its own `agents/sdlc-<role>.md` and
`references/stage-playbooks.md`, which its first move does. Restating any of it in the
prompt pays for those tokens twice, in the context that can least afford it, and worse
invites the agent to cite your prompt as a source — a fabricated-quotation class
`lld-review` and `pr-review` bounce (`references/stage-playbooks.md`, "The delegation
prompt is not a citable source"). So pass only what is unit-specific and cannot be
read from a file:

> Role: `<role>`. Unit: #`<n>` — `<title>`. Worktree: `<path>` (`cd` there first).
> `$SDLC_DIR`: `<skill_dir>`. Doc you own: `<doc-path>`. Then the 2–3 task-specific
> facts and nothing else: the finding to fix, the deviation to judge, the sibling PR
> that just landed. For the body, the comment thread, or every open child, give the
> `gh` command that fetches it (item 1) rather than pasting it.

A prompt that restates rules the agent reads for itself is the dispatch-side twin of an
over-cap handback: trim it to the unit-specific facts.

**Track stage agents** (for resume-based rework): note each `Agent` call's returned
ID against its role for this unit's run — session-scoped, never written to GitHub. To
send a finding back, `SendMessage` **directly, yourself**, never via a relay fork.
Discard the mapping once the unit reaches a stopping point.

**Keep your own context lean — you are the one that is re-read every turn.** A stage
agent's context dies when its turn ends; yours accumulates for the whole run and is
re-read on every request, so a file dump you pull into your own context is paid for
again and again. When you need to search or read across the codebase yourself — not to
drive a stage, but to resolve an ambiguity, reconcile a conflict, or locate something
— dispatch a **read-only `Explore` subagent** and act on its conclusion, rather than
running the greps and reading the files inline. Routing to `sdlc_next.py` (already
terse) and to `Explore` is how the orchestrator's context stays small across a long
epic; inline exploration is the main avoidable source of its growth.

### After the subagent returns

**Verify, don't trust:**

```bash
python3 "$SDLC" verify-exit <n> --expect-stage <stage> [--pr <pr>] [--unit epic]
```

Then, **before delegating the next stage, run `sync-branch`** — every transition
except immediately after `pass-gate`/`skip-gate`, which reconcile internally. A
conflict result routes per `references/parallelism.md`, "Git-conflict handling".
Confirm the previous stage left a comment on the issue; if not, get one.

**On a CLEAN `lld-review` of a normal-epic child (V1) or standing child** —
unchanged: `record-design-review`, then `merge-lld-doc <n>` — which publishes the doc
**and advances the child to `development` without claiming it** (`advanced: true,
claimed: false`). No gate, and **no `claim <n> --role development` here**: the child
is now a fresh Step 1 unit. Go back to Step 1 / `list-parallel-ready` and let
scheduling pick it — one pass can then hand out that `development` *and* the sibling
`lld`s it just unblocked, by lane and priority rather than by whichever lld happened
to finish first. This is a scheduling mechanism, not a policy: an independent child
still flows straight from `lld` to `development` on the next pick; nothing waits for
"all llds first". Crash-safe by construction — every persisted state after
`merge-lld-doc` maps to one next step (`references/parallelism.md`, "Publishing
lld.md to the epic branch"). A child whose `lld-review` was recorded clean but whose
Stage is still `LLD` has simply not had `merge-lld-doc` run yet: run it.

**On a CLEAN `lld-review` of an Epic's lld.md (V2)**: `record-design-review`, then
`merge-lld-doc <epic-n> --unit epic` — built 2026-09-14. Unlike V1's per-child path,
there is no separate child branch to publish *from*: `lld` already pushed
`epic-<n>/lld.md` directly onto `origin/epic-<n>` as part of its own turn, so this
call only verifies the doc actually reached origin, then advances **every** Task
`lld` created under this Epic (functional and the two standing
Integration-test/e2e-test ones alike) that has no Stage set yet — the same
advance-not-claim field write V1 does for one child, looped over every Task this
stage just created. Idempotent the same way: a Task already advanced on an earlier
run is skipped, not re-touched. Go back to Step 1 / `list-parallel-ready` afterward —
same scheduling discipline as V1, just handing out N fresh units instead of one.

**Stage exit actions** — what each stage does last, every verdict branch — live in
that stage's own `agents/sdlc-*.md`, with the routing table in
`references/stage-playbooks.md`, "Stage exit actions live in the agent files". The
agent performs its own; **opening a human-review gate, posting `start-comment` before a
review, and `merge-lld-doc` stay yours**, after the agent returns.

Repeat Steps 2–3 until the unit is merged or architected, blocked, or needs a human.

## Step 4 — Report back to the user

When Step 1 finds nothing actionable: summarize every unit touched, stages run, rework
and resume cycles per pairing, what changed, current state of each. Above the fold:

- Every `needs-human` unit **with its reason text** (flag reasons that look stale).
- Every blocked unit and what it waits on.
- Every gate-pending unit: gate PR, doc, level, whether feedback was addressed.
- Every merged PR and closed issue; every epic newly `epic:architected`.
- Any epic `check-epics-closeable` newly notified.

## Step 5 — Retrospective checkpoint (only after a merge closed an issue)

```bash
python3 "$SDLC" retro-check
```

`run_retro` is true once `pipeline.retro.everyClosedIssues` (default 5) issues have
closed since the last retrospective. **Capture the `closed_count` this invocation
returns — that number is what `--mark-done` stamps later, not whatever the live count
has become by then.** The watermark file (`pipeline.retro.watermarkFile`, default
`<docRoot>/retro-watermark`) is **state of the driven repo**; the fixes go to **the
skill repo**, which is a separate git repository (`$SDLC_DIR`, typically a submodule
such as `.github/sdlc-pipeline`). When true: grep the recently merged units' handoff
comments and docs for recurring friction — bouncing pairings (`pairing-counts` gives
the marker-backed ones), docs too thin for the next stage, dead references, gates too
strict or loose. **This file and its references are the primary fix target.** Present
findings in chat and ask before editing. Once approved:

1. In `$SDLC_DIR`: `git checkout -B retro/<date> origin/main`, edit `SKILL.md` /
   `references/*` / `agents/*`, append the dated why to `references/history.md`,
   commit, push the branch, and merge it to the skill's `main` (a PR, or a
   fast-forward if the operator says so). A skill edit that stays unpushed in the
   submodule working tree is a failed retro — the next `submodule update` discards it.

   **The skill has a test suite, and any change to `scripts/sdlc_next.py` must run it
   green before the branch is pushed**: `cd $SDLC_DIR/scripts && python3 -m pytest
   tests/ -q`. It pins real invocations, so a behavioural change shows up as a failing
   expectation rather than as silence — adding `--force` to one `git worktree remove`
   turned two tests red immediately. **A control-plane fix also gets a regression test
   paired with a positive control that must stay green**, so a "fix" that merely
   deletes the check cannot pass; verify by running all four against the pre-fix
   `sdlc_next.py`, where the regressions must go red and the controls must not. This
   step is spelled out because on 2026-09-13 two sessions each shipped control-plane
   changes untested, neither knowing the suite existed — in the same retrospective
   where both were writing rules about not asserting coverage nobody had checked.
2. A finding about an agent's procedure lands twice: the template in `$SDLC_DIR/agents/`
   and the driven repo's filled-in copy in `.claude/agents/`. `sync-skill` (below)
   re-vendors this half mechanically; it does not write `references/history.md` or
   any prose — that stays a hand-authored part of step 1.
3. In the driven repo: `python3 "$SDLC" sync-skill` bumps the submodule to the merged
   skill commit and re-vendors `.claude/agents/`, staging both — then run
   `retro-check --mark-done --count <the closed_count captured at Step 5's start>`,
   and commit the watermark and the submodule pointer together (message naming the
   retrospective). That commit is the record of "retro done at skill version X".
   **Always pass `--count`.** Fix work and the evidence sweep take real time, during
   which more issues close; `--mark-done` without `--count` falls back to whatever
   the live count is *at that later moment* and stamps it as the watermark, silently
   marking every issue closed in between as covered by a sweep that never read them
   (2026-09-14: the fallback exists only for backward compatibility and flags itself
   via `count_was_live_fallback` in the result — treat that flag as true meaning
   "re-run with the right `--count`", not as a pass).

**A retrospective is merged only when the lane is quiet** — no live stage agent. Stage
agents Read the playbook from `$SDLC_DIR` mid-run; bumping the submodule under one
changes its instructions between two of its own reads. Park the bump and land it at
the next quiescent point.
