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
record**. Comments stay short and point at the docs.

**This skill is a living process document.** Friction, dead references, and better
gating decisions are fed back into this file and its references via the Step 5
retrospective or direct operator feedback — never patched mid-cycle by a stage agent,
which flags problems in its handoff instead.

## Reference files — read on demand, not upfront

| File | Read it when |
|---|---|
| `references/parallelism.md` | Starting or resuming any child work or review pool; anything touching worktrees, branches, `sync-branch`, git conflicts, merge freshness, or an agent dying mid-stage |
| `references/gates.md` | Opening, checking, passing, or skipping a human-review gate; addressing gate feedback |
| `references/stage-playbooks.md` | Delegating any stage (its exit actions live here); **the one file a stage subagent is told to Read** — docs, altitude, commenting, rework rules |
| `references/epics.md` | Epic-level phase, child sizing and footprints, deviation escalation, bug fast-track, epic closing, board status |
| `references/operations.md` | Token and repo access, issue taxonomy (fields), auto-merge policy, local-CI attestation |
| `references/continuous-mode.md` | Operator asked for unattended looping |
| `references/history.md` | A rule looks arbitrary and you want the incident behind it |

## The lifecycle model

Which flow an epic runs is set by its **profile** — a label-matched bundle of
behavioural toggles in the config's `pipeline.profiles` (see `references/epics.md`,
"Epic profiles", and `references/operations.md`). The skill no longer hardcodes the
`epic:standing`/`epic:legacy` labels; a client maps labels to profiles and can use
`epic:standing`, `RTB`, or any label it likes. The three shipped default profiles
reproduce the historical behaviour.

A **default-profile epic** runs Product and Architecture **once, at the epic level**;
then each child runs a lighter per-task pipeline:

```
epic:  product -> [product-review] -> [Gate A, human] -> architecture -> [arch-review] -> [Gate B, human or confidence-skip] -> epic:architected
child: lld -> [lld-review, mandatory, no gate] -> development -> testing -> [pr-review] -> auto-merge -> CLOSED
```

A **standing profile** (`epicLevelPhase: false`, e.g. matched to `epic:standing`)
never runs an epic-level phase; each child runs the full flow on its own issue number:

```
product -> [product-review] -> [Gate A] -> architecture -> [arch-review] -> [Gate B] -> development -> testing -> [pr-review] -> CLOSED
```

- **`product-review` is universal** — every unit that runs `product` runs it right
  after, an adversarial opus review of `product.md`. A blocker bounces `product`
  (looping until clean, backstopped by the escalation valve); a clean verdict goes to
  Gate A. See the `product-review` row in the stage table and `references/stage-playbooks.md`.
- **Gate A is profile-configurable.** A profile with `requiresHumanGateA: false` (a
  standing/RTB backlog) **auto-passes** Gate A on a clean `product-review` — the unit
  flows straight to `architecture`, no human. The default profile opens the human Gate A
  as before. See `references/gates.md`, "Gate A configurability".
- **The Gate B confidence bar is per-profile** — `gates.skipConfidenceThreshold`
  (default 95); a standing/RTB profile may lower it (e.g. 90).
- A standing-profile **bug** enters at `architecture` (`references/epics.md`, "Bug
  fast-track"). A bug against a default-profile architected epic starts at `lld`.
- A **legacy profile** (`driven: false`, e.g. matched to `epic:legacy`) is skipped
  entirely, children included.
- Reviews (`product-review`, `arch-review`, `lld-review`, `pr-review`) run immediately
  after the stage before them and have no Stage value of their own.
- **A normal epic's children are never eligible before the epic is
  `epic:architected`** — enforced by `next-action` and `list-parallel-ready`, even
  while the epic itself is gate-pending, blocked, or needs-human.
- **Nothing spins off a separate ticket.** Every problem found before merge is fixed
  inline by resuming the subagent that owns the responsible stage
  (`references/stage-playbooks.md`, "Rework and blockers"). Only three things pause a
  unit: a cross-issue `blockedBy`, a `needs-human` verdict, or an open gate. None of
  them pause the invocation — see "Looping".

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
  subagents the concrete `$SDLC_DIR/references/stage-playbooks.md` path — a
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
| `list-parallel-ready <epic> --repo-path <p>` | Dev-lane pool: `lld`/`development`/`testing` children safe to start/resume concurrently |
| `list-design-ready <epic> --repo-path <p>` | Design-lane pool: a **standing** epic's `product`/`architecture` children safe to start/resume concurrently (empty for a default-profile epic) |
| `list-ready-for-review <epic>` | Review pool: finished PRs awaiting `pr-review` |
| `worktree-add <n> [--unit epic]` | The unit's worktree, the one correct way: resumes from `origin/<branch>` when it exists, else branches off the integration base; initialises the skill submodule and returns `skill_dir` (the per-unit `$SDLC_DIR`) |
| `provision-epic-stack <n>` / `teardown-epic-stack <n>` | The epic's isolated runtime stack (own compose project, ports, env/secrets profile, DB data dir) — at epic start / at epic close; no-op unless `pipeline.stack.enabled` |
| `claim <n> --role <role>` | Stage + In Progress + start comment |
| `start-comment <n> --role <role>` | Start comment alone (`arch-review` / `lld-review` / `pr-review` / `testing`) |
| `sync-branch <n> [--unit epic]` | Reconcile the branch with its integration base; structured conflict result |
| `merge-lld-doc <n>` | Publish a normal-epic child's clean `lld.md` onto the epic branch, then **advance** it to `development` (Stage set, Pipeline Status cleared — never claimed) |
| `verify-exit <n> --expect-stage <s> [--pr <pr>] [--unit epic]` | Post-handoff state check |
| `open-gate` / `check-gate` / `pass-gate` / `skip-gate` | Human-review gates (`references/gates.md`) |
| `open-dev-pr <n> ...` | Draft PR + Stage=Testing + handoff comment |
| `handoff-to-pr-review` / `record-pr-review` | The two review-queue markers |
| `record-local-ci --pr <pr> --suite <s> --sha <HEAD>` | `testing`'s local-CI attestation; `<s>` is a `requiredWorkflows[].suite` from config |
| `record-design-review <n> --role <r> --outcome clean\|rework` | Last action of **every** design review — the bounce marker `pairing-counts` reads |
| `pr-checks <pr>` / `merge-pr <pr> --issue <n>` | CI status / the only merge gate (refuses behind-base; reports `config_changed`) |
| `create-issue --parent <epic>` | The only issue-creation path |
| `mark-blocked` / `mark-needs-human` / `pause-for-epic-regate` | Park a unit (the first two also release its worktree) |
| `pairing-counts <n>` | Marker-derived escalation-valve strike counts, with the configured thresholds |
| `show-config` | Effective tunables (`pipeline` block over defaults) — read it once per invocation |
| `list-needs-human` / `check-epics-closeable` / `retro-check [--mark-done]` | End-of-invocation sweeps |
| `close-epic <n>` / `record-epic-verification <n> --kind e2e\|exploratory` | Epic close, two-call shape (`references/epics.md`, "Epic closing") |
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

`next-action` and the pool queries **require** an epic number. The pipeline never
scans the repo to decide whose turn it is. No epic named = a blocking question; ask
before doing anything. One invocation per epic; two invocations on the *same* epic
race — don't. Every child must be a native sub-issue of its epic to be picked; link
with `create-issue --parent`.

## Config can move under you

The config lives in the repo and merges through the pipeline like any file.
**`merge-pr` returns `config_changed: true` when the merged PR touched it** — re-read
the config before the next stage. If the skill is vendored rather than symlinked, its
own files can move too; then re-read `SKILL.md` and any reference you're about to
apply.

## What you decide, and what you take to the operator

- **During `development`, `testing` and `pr-review`: take the recommended fix
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
`lld`/`development`/`testing` fan out to `parallelism.devLane` children and
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
after a `testing` handoff (`list-ready-for-review`) or when `next-action` returns a
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
entering `product` for the first time (no `product.md` on its branch yet — an epic's
own, or a standing child's), do **not** claim or delegate yet. Epic issues are mostly
one-liners that do not carry the scope the operator has in mind, and scope discovered
after `product.md`, `architecture.md`, Gate A/B and child materialisation cascades
rework through every downstream doc. So: read the issue and its thread, state back a
concise "this epic covers / excludes / the decisions I see open" summary, and put the
genuine ambiguities to the operator as questions (`AskUserQuestion` — scope
boundaries, must-haves vs out-of-scope, decisions the one-liner leaves open) in one
batch. Feed the answers verbatim into the `product` delegation prompt. This is a
pre-product interaction, earlier than and distinct from Gate A, and it is one of the
three things you *do* take to the operator (a product or scope call). Skip it only
on a rework round or a resume where `product.md` already exists. Detail:
`references/stage-playbooks.md`, "Scope alignment before `product`".

## Step 3 — Run this stage, then the next, then the next

Delegate to exactly **one** fresh subagent of the role's `subagent_type`. Each
`sdlc-*` type is an agent definition in the repo's `.claude/agents/` (persona,
procedure, refusal criteria — see `README.md`, "Prerequisites"). There is no
orchestrator-direct review path.

| Role | Trigger | `subagent_type` | Model | Doc it owns |
|---|---|---|---|---|
| `product` | `stage:product` (epic or standing child) | `sdlc-product` | opus | `<unit>-<n>/product.md` |
| `product-review` | right after `product` | `sdlc-product-review` | fable | none (comment only) — universal; blocker bounces `product`, clean goes to Gate A |
| `architecture` | `stage:architecture` (epic or standing child) | `sdlc-architecture` | opus | `<unit>-<n>/architecture.md` (epic level also creates/splits children, sets Effort) |
| `arch-review` | right after `architecture` | `sdlc-design-review` | fable | none (comment only) |
| `lld` | `stage:lld` (normal-epic child) | `sdlc-lld` | sonnet | `issue-<n>/lld.md` |
| `lld-review` | right after `lld` | `sdlc-design-review` | opus | none — **mandatory, never confidence-skipped** |
| `development` | `stage:development` | `sdlc-development` | sonnet | `issue-<n>/development.md` |
| `testing` | `stage:testing` | `sdlc-testing` | sonnet | none — structured handoff comment |
| `pr-review` | right after `testing` passes | `sdlc-pr-review` | opus | none (comment only) |

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
the `product` and `architecture` authoring, and the two last-checks-before-something-irreversible
(`lld-review`, `pr-review`). **`product-review` and `arch-review` run on `fable`** — both are
backstopped by a human gate right after them (Gate A after `product-review`, Gate B after
`arch-review`), so a cheaper adversarial pass is acceptable there; `lld-review`/`pr-review`
stay opus because nothing human follows them. Sonnet on the review-backstopped,
higher-frequency stages (`lld`, `development`, `testing`). Retune in
`references/history.md` with a dated reason, not by guessing here — the latest
stage-by-stage evaluation is the 2026-09-12 entry there.

> **Open interaction to resolve (operator):** a `fable`-run `arch-review` emits the
> confidence marker that can drive a **Gate B confidence-skip** (`gates.skipConfidenceThreshold`);
> a fable review is a weaker signal to auto-skip a human gate on. Decide one of: keep Gate B
> always-open for a `fable`-run `arch-review` (skip applies only to an opus-tier review), raise
> the threshold, or accept fable-driven skips. Not resolved here.

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

`development` is the only role that stacks skills: its agent file invokes
`superpowers:test-driven-development`, `superpowers:verification-before-completion`
and `superpowers:systematic-debugging` first. **Exclude**
`superpowers:finishing-a-development-branch` (integration decision is fixed: draft PR,
stop) and `superpowers:using-git-worktrees` (the orchestrator owns worktrees).

**Agent files carry persona, procedure and `tools:` only.** Every pipeline rule —
stage order, gates, doc set, rework routing, exit actions — lives in
`references/stage-playbooks.md`, and every agent is told to Read it first. Two homes
for one rule is how rules drift.

### The delegation prompt

Every delegation prompt **must** ensure the agent *has* (not necessarily that the
prompt *contains*):

1. The unit's number, title, full body, full comment thread. Epic-level: plus number,
   title, body of **every open child**. **For anything large, give the `gh` command
   that fetches it rather than pasting it** — pasted threads truncate prompts
   mid-instruction (`references/history.md`).
2. Invoke the role's skills first (only `development` has any).
3. One `Read` of `$SDLC_DIR/references/stage-playbooks.md` before anything else,
   **plus** the exact doc path it owns (e.g. `<docRoot>/issue-<n>/lld.md`), spelled
   out. Don't paste the playbook. `$SDLC_DIR` here is the **unit's own** skill copy —
   the `skill_dir` that `worktree-add`/`sync-branch` returned for this worktree
   (Setup), never the main checkout's `.github/sdlc-pipeline`.
4. On genuine ambiguity: **stop and report the specific question in the final
   message** — never guess, create issues, or change fields.
5. For a normal-epic child's `lld`: the epic's `architecture.md` is the design source
   of truth; the fits-vs-deviates call is the first move.
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

**Track stage agents** (for resume-based rework): note each `Agent` call's returned
ID against its role for this unit's run — session-scoped, never written to GitHub. To
send a finding back, `SendMessage` **directly, yourself**, never via a relay fork.
Discard the mapping once the unit reaches a stopping point.

### After the subagent returns

**Verify, don't trust:**

```bash
python3 "$SDLC" verify-exit <n> --expect-stage <stage> [--pr <pr>] [--unit epic]
```

Then, **before delegating the next stage, run `sync-branch`** — every transition
except immediately after `pass-gate`/`skip-gate`, which reconcile internally. A
conflict result routes per `references/parallelism.md`, "Git-conflict handling".
Confirm the previous stage left a comment on the issue; if not, get one.

**On a CLEAN `lld-review` of a normal-epic child**: `record-design-review`, then
`merge-lld-doc <n>` — which publishes the doc **and advances the child to
`development` without claiming it** (`advanced: true, claimed: false`). No gate,
and **no `claim <n> --role development` here**: the child is now a fresh Step 1 unit.
Go back to Step 1 / `list-parallel-ready` and let scheduling pick it — one pass can
then hand out that `development` *and* the sibling `lld`s it just unblocked, by lane
and priority rather than by whichever lld happened to finish first. This is a
scheduling mechanism, not a policy: an independent child still flows straight from
`lld` to `development` on the next pick; nothing waits for "all llds first". Crash-safe
by construction — every persisted state after `merge-lld-doc` maps to one next step
(`references/parallelism.md`, "Publishing lld.md to the epic branch"). A child whose
`lld-review` was recorded clean but whose Stage is still `LLD` has simply not had
`merge-lld-doc` run yet: run it.

**Stage exit actions** — what each stage does last, every verdict branch — live in
`references/stage-playbooks.md`, "Stage-specific exit actions". Follow them exactly;
the gate-opening steps inside them are the orchestrator's, not the agent's.

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
closed since the last retrospective. The watermark file (`pipeline.retro.watermarkFile`,
default `<docRoot>/retro-watermark`) is **state of the driven repo**; the fixes go to
**the skill repo**, which is a separate git repository (`$SDLC_DIR`, typically a
submodule such as `.github/sdlc-pipeline`). When true: grep the recently merged units'
handoff comments and docs for recurring friction — bouncing pairings
(`pairing-counts` gives the marker-backed ones), docs too thin for the next stage, dead
references, gates too strict or loose. **This file and its references are the primary
fix target.** Present findings in chat and ask before editing. Once approved:

1. In `$SDLC_DIR`: `git checkout -B retro/<date> origin/main`, edit `SKILL.md` /
   `references/*` / `agents/*`, append the dated why to `references/history.md`,
   commit, push the branch, and merge it to the skill's `main` (a PR, or a
   fast-forward if the operator says so). A skill edit that stays unpushed in the
   submodule working tree is a failed retro — the next `submodule update` discards it.
2. A finding about an agent's procedure lands twice: the template in `$SDLC_DIR/agents/`
   and the driven repo's filled-in copy in `.claude/agents/`.
3. In the driven repo: bump the submodule to the merged skill commit, run
   `retro-check --mark-done`, and commit the watermark and the submodule pointer
   together (message naming the retrospective). That commit is the record of
   "retro done at skill version X".

**A retrospective is merged only when the lane is quiet** — no live stage agent. Stage
agents Read the playbook from `$SDLC_DIR` mid-run; bumping the submodule under one
changes its instructions between two of its own reads. Park the bump and land it at
the next quiescent point.
