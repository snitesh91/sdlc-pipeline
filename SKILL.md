---
name: sdlc-pipeline
description: Survey a repo's auto-SDLC pipeline (epic-level product -> architecture, then per-task lld -> development -> testing -> PR review) and drive actionable epics/issues through every remaining stage to merge — PR reviews and lld/development/testing both fan out in bounded, worktree-isolated parallel pools (mechanical eligibility off native blockedBy + declared footprints, not hand-tracked), looping to the next actionable unit whenever the one in hand gets stuck, until none are left. Invoked manually as /sdlc-pipeline.
model: opus
---

# Auto SDLC over GitHub Issues

One invocation drives actionable work stage by stage toward a merge. An epic's own
`product`/`architecture` phase runs strictly one at a time; the implementation lane
(`lld` → `development` → `testing`) and `pr-review` both fan out to small, bounded,
worktree-isolated pools with eligibility computed mechanically, never hand-tracked.
The main agent (this conversation) coordinates every handoff itself: it delegates one
stage to one subagent per active child, waits, verifies, then decides what's next —
parallel means more than one child's handoff loop is live at once, not that anything
decides on its own. GitHub fields and comments are the pipeline's **visibility log**
and crash-resume point, not the decision mechanism — the main agent already knows
what's next, because it just ran the previous stage.

The **detailed source of record** is the committed docs under
`docs/sdlc/issue-<n>/` (and `epic-<n>/`), not the comment thread. Comments
stay short and point at the docs.

**This skill is a living process document.** Friction, dead references, and better
gating decisions get fed back into this file and its references via the Step 5
retrospective (or direct operator feedback) — never patched ad hoc mid-cycle by a
stage agent; it flags problems in its handoff instead.

## Reference files — read on demand, not upfront

| File | Read it when |
|---|---|
| `references/parallelism.md` | Starting/resuming any child work or review pool; anything touching worktrees, branches, `sync-branch`, git conflicts, or merge freshness |
| `references/gates.md` | Opening, checking, passing, or skipping a human-review gate; addressing gate feedback |
| `references/stage-playbooks.md` | Delegating any stage (its exit actions live here); **the one file a stage subagent is told to Read** — docs, altitude, commenting, rework rules |
| `references/epics.md` | Epic-level phase, child sizing/footprints, deviation escalation, bug fast-track, epic closing, board status |
| `references/operations.md` | Token/repo access, issue taxonomy (fields), auto-merge policy, assignee convention |
| `references/continuous-mode.md` | Operator asked for unattended looping |
| `references/history.md` | A rule looks arbitrary and you want the incident behind it |

## The lifecycle model

A **normal epic** (not `epic:standing`, not `epic:legacy`) runs Product and
Architecture **once, at the epic level**, then its children each run a lighter
per-task pipeline:

```
epic: stage:product -> [Gate A, human] -> stage:architecture -> [arch-review] -> [Gate B, human or confidence-skip] -> epic:architected

each child, once the epic is epic:architected:
      stage:lld -> [lld-review, automated, mandatory] -> stage:development -> stage:testing -> [pr-review] -> auto-merge -> CLOSED
```

A **standing epic** (`epic:standing`, e.g. a standing backlog epic) never runs an epic-level phase; each
child cycles the full per-issue flow on its own issue number:

```
stage:product -> [Gate A] -> stage:architecture -> [arch-review] -> [Gate B] -> stage:development -> stage:testing -> [pr-review] -> CLOSED
```

A standing-epic **bug** enters directly at `stage:architecture` (see
`references/epics.md`, "Bug fast-track"). A bug against a normal, architected epic
just starts at `lld` like any child. A **legacy epic** (`epic:legacy`) is skipped
entirely — it and all its children.

Each stage is exactly one subagent call, made by the main agent, waited on, and
verified before the next starts. The issue/epic stays open throughout; reviews
(`arch-review`/`lld-review`/`pr-review`) run immediately after the stage before them
with no Stage value of their own. **A normal epic's children are never eligible
before the epic is `epic:architected`** — enforced mechanically by `next-action` and
`list-parallel-ready`, including while the epic itself is stuck at a gate, blocked,
or needs-human.

**Nothing spins off a separate ticket.** Every problem found before merge is fixed
inline by resuming the subagent that owns the responsible stage
(`references/stage-playbooks.md`, "Rework and blockers"). The only things that pause
a unit: a genuine cross-issue dependency (`blockedBy`), a resumed agent concluding
only the operator can decide (`needs-human`), or an open human-review gate. None of
these pause the *invocation* — see "Looping" below.

## Setup — one shell, three values

This skill is generic and lives **outside** the repo it drives (typically imported
via a symlink). Everything project-specific lives in the repo, in
`sdlc-pipeline.config.json` at its root (see `README.md`; the control plane finds it
by walking up from the working directory, or via `$SDLC_CONFIG`). Before the first
command, set for the session:

```bash
export SDLC_DIR="<path-to-this-skill>"          # this skill's root
export SDLC="$SDLC_DIR/scripts/sdlc_next.py"     # the control plane
export GITHUB_TOKEN=$(cat <your GitHub token file>)   # classic PAT (ghp_)
cd <repo-root>                                   # so config + git resolve
```

All commands below are `python3 "$SDLC" <command>`. When a stage subagent is told to
Read the stage playbook (Step 3), hand it the concrete path
`$SDLC_DIR/references/stage-playbooks.md` — the skill is mounted outside the repo, so
a repo-relative path won't resolve for the agent.

## Deterministic control plane

Every mechanical GitHub read, decision, and mutation is owned by the control plane
(`$SDLC`) — never hand-executed `gh`/GraphQL calls, and never a hand-typed git
fetch/checkout/merge/push sequence:

```bash
python3 "$SDLC" <command> ...
```

Every command prints one JSON object; exit 0 = valid result (including "nothing to
do" and structured refusals like a sync conflict or a behind-main merge), exit 1 =
operational failure — treat nonzero as "stop and report", never "retry by hand".

| Command | What it owns |
|---|---|
| `next-action <epic>` | Pick the one unit to work (Step 1) |
| `list-parallel-ready <epic> --repo-path <p>` | Dev-lane pool: children safe to start/resume concurrently |
| `list-ready-for-review <epic>` | Review pool: finished PRs awaiting `pr-review` |
| `claim <n> --role <role>` | Stage + In Progress + start comment |
| `start-comment <n> --role <role>` | Start comment alone (arch-review / lld-review / pr-review / testing) |
| `sync-branch <n> [--unit epic]` | Reconcile branch with origin/main (structured conflict result + marker comment; worktree auto-resolved when `--repo-path` omitted) |
| `merge-lld-doc <n>` | Publish a normal-epic child's `lld.md` onto its epic branch the instant `lld-review` is CLEAN (durable design + sibling visibility); no-op for a standing-epic child or a parentless issue |
| `verify-exit <n> --expect-stage <s> [--pr <pr>] [--unit epic]` | Post-handoff state check (worktree auto-resolved) |
| `open-gate` / `check-gate` / `pass-gate` / `skip-gate` | Human-review gates (`references/gates.md`) |
| `open-dev-pr <n> ...` | Draft PR + Stage=Testing + handoff comment |
| `handoff-to-pr-review` / `record-pr-review` | The two review-queue markers |
| `record-local-ci --pr <pr> --suite backend\|frontend --sha <HEAD>` | testing's local-CI attestation — merge-gate stand-in for the main-only backend/frontend GHA suites (`references/operations.md`, "Local-CI attestation") |
| `record-design-review <n> --role arch-review\|lld-review --outcome clean\|rework` | Last action of **every** design review — the bounce marker `pairing-counts` reads back |
| `pr-checks <pr>` / `merge-pr <pr> --issue <n>` | CI status / the only merge gate (refuses behind-main; reports `config_changed`) |
| `create-issue --parent <epic>` | The only issue-creation path |
| `mark-blocked` / `mark-needs-human` / `pause-for-epic-regate` | Parking a unit (the first two also release its worktree, so the dev lane stops counting it) |
| `pairing-counts <n>` | Marker-derived escalation-valve strike counts (pr-review rework, sync conflicts, and per-role design-review bounces) |
| `list-needs-human` / `check-epics-closeable` / `retro-check [--mark-done]` | End-of-invocation sweeps; retro trigger is a committed watermark |
| `auto-pass-gate` / `mark-feedback-received` / `mark-feedback-addressed` / `mark-todo` / `mark-issue-closed` | CI-triggered real-time paths (gate-auto-advance.yml) |

What the CLI does **not** decide: which stage's agent owns a defect found in rework,
whether a bounce trips the valve (third bounce → context-reset replacement agent, sixth
→ `needs-human`), whether a reported ambiguity is genuine, and all subagent prompt/doc/PR
prose. Those stay this skill's judgment calls.

## Epic number is mandatory

`next-action` (and both pool queries) **require** an epic issue number — the operator
names which epic to drive; the pipeline never scans the repo to decide whose turn it
is. If the invocation wasn't told which epic, that's a blocking question — ask before
doing anything. `decide_next_action` raises if the number isn't a top-level
`Type: Feature` issue. Running more than one epic at once = one separate
`/sdlc-pipeline <epic>` invocation per epic (safe: field writes are epic-scoped, and
every branch — child and epic-self alike — lives in its own worktree, so invocations
never contend for a checkout — see `references/parallelism.md`). Two invocations on
the *same* epic would race — don't.

Every child must be linked as a native sub-issue of its epic to ever be picked — an
orphan simply never appears. Link with `gh.add_sub_issue()` or `create-issue
--parent`.

## Config can move under you — re-read it when it does

The skill itself lives outside the repo and does not change mid-invocation, but its
**config** (`sdlc-pipeline.config.json`) lives in the repo and merges through the
pipeline like any other file. A change to it (repo, doc root, model-relevant fields,
required workflows, schema ids) can alter how later commands behave in the same run.

So this is mechanical: **`merge-pr` returns `config_changed: true` whenever the PR it
just merged touched the config file.** When you see it, re-read the config before the
next stage. (If you *vendor* the skill into the repo rather than symlinking it, the
skill files themselves can also move mid-run — then re-read `SKILL.md` and any
reference whose rules you are about to apply. See `references/history.md` for the
incident that made this mechanical.)

## What you decide, and what you take to the operator

Set on 2026-08-22 after four mid-epic operator asks, two of which were implementation
calls the orchestrator could have made itself:

- **During `development`, `testing` and `pr-review`: take the recommended fix
  yourself.** A stage or review agent that ends with "recommend X" has already done the
  analysis — apply it, don't relay it.
- **Escalate exactly three things**: a product or scope call (what the thing should
  do, what's in or out), an amendment to a **gate-approved** doc, and an
  escalation-valve trip (`mark-needs-human`).
- **Parallelism, worktrees, model tiers, review scoping and which stage owns a defect
  are never operator questions.** Neither is a merge — see `references/operations.md`,
  "PRs merge automatically — no human review gate".

## Looping within an invocation

Whenever a unit hits `blocked`/`needs-human`/an open gate: park *that unit* (comment,
fields, stop) and return to Step 1 for the next actionable unit **within the same
epic** — same invocation. The invocation ends only when Step 1 finds nothing
actionable left in this epic: every open child (and the epic's own phase) is closed,
blocked on an unmet dependency, needs-human, or gate-pending with no unresolved
feedback to address. Then stop and report (Step 4). `action: "none"` says nothing
about other epics — driving a different epic is a separate, explicitly named
invocation. For unattended operation, see `references/continuous-mode.md`.

## Concurrency — summary

| Work | Concurrency |
|---|---|
| Epic-self `product`/`architecture` | Never — strictly sequential, in its own `epic-<n>` worktree |
| `lld`/`development`/`testing` | Up to `DEV_LANE_PARALLELISM` (3) children, worktree-isolated, eligibility via `list-parallel-ready` |
| `pr-review` | Up to `PR_REVIEW_PARALLELISM` (3) PRs, detached worktrees, via `list-ready-for-review` |
| Rework | One development thread **per issue**, always — several issues may each have one; a single issue never has two |

Full mechanics, worktree rules, git-conflict layers, and the merge-freshness gate:
`references/parallelism.md`. Read it before starting any concurrent work.

## Step 1 — Pick the one issue to work

```bash
python3 "$SDLC" next-action <epic>
```

Act on `action`. Every result except `skip`/`none` carries `unit` (`"issue"` or
`"epic"`) — pass it through to any command taking `--unit`; `unit: "epic"` means the
returned `issue` is the epic itself in its own Product/Architecture phase.

| `action` | Meaning | What to do |
|---|---|---|
| `resume` | Crash recovery — a real claim happened and the session died mid-stage (never a CI-advanced gate) | Resume at the returned `stage`, reconstructing from comments + committed docs, per Step 3. If the unit has a live worktree and you are *currently* driving it in this session's parallel lane, it isn't crashed — skip it and survey again |
| `delegate` | The epic itself, or a child, is ready | Claim (Step 2), delegate per Step 3 |
| `pass-gate` | A gate PR was merged | `references/gates.md`, "Passing a gate" — pass `issue`/`gate_pr`/`stage`/`unit` verbatim |
| `address-gate-feedback` | Gate PR has unresolved threads / new comments | `references/gates.md`, "Addressing gate feedback" |
| `none` | Nothing actionable in this epic right now | Run `list-needs-human` + `check-epics-closeable` (below), then Step 4 |
| `skip` | Epic is `epic:legacy` | Say so (quote `reason`), then Step 4 — nothing to report per-issue |

**Two companion queries widen Step 1 where work is genuinely parallel** — both
epic-scoped, read-only, never replacing `next-action`:

- `list-ready-for-review <epic>` — finished PRs awaiting review; run a pool per
  `references/parallelism.md`, "Parallel PR review".
- `list-parallel-ready <epic> --repo-path <repo-root>` — children safe to
  start/resume concurrently; run the lane per `references/parallelism.md`, "Parallel
  implementation lane".

Run either whenever useful — typically after a `testing` handoff (review pool) or
when `next-action` returns a child and lane headroom remains (dev pool). **Always run
`list-parallel-ready` immediately after every `merge-pr`**: a merge is the one event
that can unblock a sibling, and a freshly-unblocked child that nobody re-queries sits
idle through a whole stage of its sibling's work. Epic #156 ran the lane at 1-2 of 3
for most of its length partly for that reason.

**A `blockedBy` edge is not automatically a whole-child stop.** Check *which stages*
actually depend on the blocker: it usually constrains `development` onward, not `lld`
— start the design stage concurrently and sequence only the genuinely dependent
stages. Keep the native edge either way; `list-parallel-ready` reads it.

Before ending on `none`, always run
`list-needs-human` (repo-wide; skim each reason — if one looks stale, re-check it,
clear it yourself with a comment if resolved) and `check-epics-closeable` (repo-wide,
idempotent). Step 4's report includes each needs-human reason text and any newly
notified closeable epic.

## Step 2 — Claim it

```bash
python3 "$SDLC" claim <number> --role <role>
```

Works for a child or the epic itself — no `--unit` needed. `<role>` is `next-action`'s
returned `stage` verbatim (including `lld`); an unknown role is refused loudly.
Create the unit's worktree **before** claiming (`references/parallelism.md`,
"Ordering rule") — branch-touching commands then auto-resolve it, no `--repo-path`
needed. **On a resume, base the worktree on `origin/issue-<n>` when that branch
exists — never fresh off `main`**, which drops the already-pushed `lld.md`/code (see
`references/parallelism.md`, "Ordering rule").

## Step 3 — Run this stage, then the next, then the next

For the current stage, delegate to exactly **one** fresh subagent of the role's
`subagent_type` below. Each `sdlc-*` type is a project agent definition in
`.claude/agents/` — the persona, procedure and refusal criteria load at system-prompt
level, so there is no "invoke the skill first" step for the persona any more. There is
no orchestrator-direct review path — reviews are ordinary subagent delegations.

| Role | Trigger | `subagent_type` | Skills it invokes first | Model | Doc it owns |
|---|---|---|---|---|---|
| `product` (epic) | `unit: "epic"`, `stage:product` | `sdlc-product` | — | **opus** | `epic-<n>/product.md` |
| `architecture` (epic) | `unit: "epic"`, `stage:architecture` | `sdlc-architecture` | — | **opus** | `epic-<n>/architecture.md` (also creates/splits children, sets Effort) |
| `arch-review` | right after `architecture` (either level) | `sdlc-design-review` | — | **opus** | none (comment only) |
| `product` (standing child) | `unit: "issue"`, `stage:product` | `sdlc-product` | — | **opus** | `issue-<n>/product.md` |
| `architecture` (standing child) | `unit: "issue"`, `stage:architecture` | `sdlc-architecture` | — | **opus** | `issue-<n>/architecture.md` |
| `lld` (normal-epic child) | `unit: "issue"`, `stage:lld` | `sdlc-lld` | — | **sonnet** | `issue-<n>/lld.md` |
| `lld-review` | right after `lld` | `sdlc-design-review` | — | **opus** | none — **mandatory every time, no confidence-skip** |
| `development` | `stage:development` | `sdlc-development` | `superpowers:test-driven-development`, `superpowers:verification-before-completion`, `superpowers:systematic-debugging` | **sonnet** | `issue-<n>/development.md` |
| `testing` | `stage:testing` | `sdlc-testing` | — | **sonnet** | none — structured handoff comment |
| `pr-review` | right after `testing` passes | `sdlc-pr-review` | — | **opus** | none (comment only) |

`next-action`'s `stage` field tells you directly whether a child runs `lld` or full
`architecture` — distinct Stage values, no inference. Pass the Model column as the
`Agent` call's `model` param. **The model is pinned here, at the call site, and
deliberately not in the agent files' frontmatter** — one agent definition can then run
at two tiers where that earns something (`sdlc-design-review` serves both
`arch-review` and `lld-review`, and was split across tiers until 2026-08-28; nothing is
split today), and a tuning change is a one-word edit in this table rather than a change
to a file.

**The table is the default, not a floor — downgrade a genuinely small task.** Both
columns are the *right* choice for a stage-sized piece of work, which is what most
dispatches are. They are not a tax to pay on work that is obviously smaller than the
stage that owns it: a one-line tfvars change, a doc-only correction of a typo or a stale
reference, a rework round whose entire delta is applying a fix already specified verbatim
in the finding. Dispatching Opus to re-read 1,500 lines so it can change `true` to
`false` buys nothing.

So the orchestrator may drop a dispatch to a cheaper tier, and in the smallest cases
skip the subagent and make the edit itself. Judge it on the *work*, not the stage
label — how much has to be read, how much has to be decided, and what breaks if it is
wrong.

Four things that are not negotiable, because each one has cost this pipeline something:

- **Say so, in the stage's own comment on the issue.** An undeclared cheap run is
  indistinguishable from a full one in the thread, and the next agent — or the next
  session — will read a downgraded pass as the rigour the table promises. State the
  tier used and the one-line reason.
- **Never downgrade a review that is the last check before something irreversible.**
  `pr-review` has no backstop at all before auto-merge, and `lld-review` is mandatory
  precisely because nothing after it re-examines the design. A trivially bounded diff
  can still be reviewed cheaply — but that is a judgement about the *diff*, made
  explicitly, not a default.
- **Never downgrade after a bounce.** If the pairing already failed at the table's
  tier, the cheaper tier is not the one that will resolve it. Escalation-valve rounds
  run at the table tier or above.
- **Keep the `subagent_type`.** The `sdlc-*` agent files carry the persona, procedure
  and *refusal criteria* that make a stage a gate rather than a task — `sdlc-testing`
  rejecting existence-only tests, `sdlc-development` refusing an incomplete handoff.
  Swapping in a generic agent silently removes the gate. Change the tier, not the type,
  unless the work genuinely has no stage semantics at all.

An upgrade above the table is the same deal in reverse: allowed, and stated in the
comment with the reason. The point is that the choice is visible, not that it is fixed.

**`opus`/`sonnet` here are tier aliases, not version pins.** What a tier resolves to
is the harness's own default for it (pin a specific version in your harness config if
you need one); the `Agent` call's `model` accepts only the aliases. This table
controls the *tier*, chosen at the call site so one agent definition can run at more
than one tier.

The split follows where judgment sits with no human in front of it: **Opus** on
`product`, the `architecture`/`arch-review` design pairing (both levels), and the two
last-checks-before-something-irreversible — `lld-review` (nothing re-examines the
design after it) and `pr-review` (no backstop before auto-merge). **Sonnet** on the
review-backstopped, higher-frequency `lld`, `development`, and `testing`. These are
cost-vs-quality calls that have been retuned repeatedly; the dated rationale and the
incidents behind each move live in `references/history.md`. Retune there, with a
reason — not by guessing here.

`development` is the only role that still stacks skills: agent definitions cannot be
composed, so its file makes invoking the three `superpowers:*` skills its first
instruction. Those are plugin-scoped (full name); the agent types are project-local.

**Exclude** `superpowers:finishing-a-development-branch` (integration decision is
fixed: draft PR, stop) and `superpowers:using-git-worktrees` (worktrees are created
and removed by the *orchestrator*; a stage agent is told which directory to use).

**Agent files carry persona, procedure and `tools:` only.** Every *pipeline* rule —
stage order, gates, doc set, rework routing, exit actions — stays in
`references/stage-playbooks.md`, and every agent is told to read it first. Keep it
that way: two homes for the same rule is how the plugin this layer was ported from
ended up with a dispatchable-by-nobody agent file.

Every delegation prompt **must** ensure the agent *has* (not necessarily that the
prompt *contains*) the following:

1. The issue's/epic's number, title, full body, full comment thread. For an
   epic-level delegation: plus the number, title, and full body of **every open
   child** — cross-child visibility is the point. **For anything large — a long issue
   body, a full comment thread, sibling docs — give the agent the `gh` command that
   fetches it rather than pasting the content.** A pasted thread is what makes a
   prompt large enough to truncate: on epic #156 the very first `sdlc-product`
   dispatch ended mid-prompt at a heading followed by an empty code fence where the
   epic body should have been, and the agent started work without the body or half
   its instructions. Every later dispatch handed over a fetch command instead and it
   never recurred.
2. Invoke the table's skills (if any) as the very first step — only
   `development` has any.
3. Read the stage playbook (one `Read` call) — hand over the concrete
   `$SDLC_DIR/references/stage-playbooks.md` path (see Setup), not a repo-relative one
   — before doing anything else, **plus**, spelled out verbatim, the exact doc path it
   owns (e.g. `<DOC_ROOT>/issue-<n>/lld.md`). Don't paste that file's text
   into the prompt.
4. On genuine ambiguity: **stop and report the specific question in the final
   message** — never guess, never create issues, never change fields; the
   orchestrator resumes the right earlier stage.
5. For a normal-epic child's `lld`: the epic's
   `docs/sdlc/epic-<parent>/architecture.md` is the design source of truth;
   the fits-vs-deviates call is the first move.
6. For every stage — child or epic-self — the exact worktree to work in —
   > Work in `<worktree-path>` (`/tmp/sdlc-dev-<n>` for a child issue,
   > `/tmp/sdlc-epic-<n>` for an epic's own stage) — `cd` there before any git
   > command. The branch already exists there if a prior stage ran, or create it
   > with `git worktree add <path> -b <branch> origin/main` if you're first.
   > Never touch the main checkout for anything on this unit.

   For `development` additionally: small logical commits, push, open the draft PR via
   `open-dev-pr` — never mark ready or merge; if a push is rejected, stop and report.
7. For any review computing a diff against `main`: `git fetch origin main` first and
   diff against `origin/main`, never local `main` (stale local `main` silently
   pollutes the review — it has happened).

**Tracking stage agents (for resume-based rework)**: note each `Agent` call's
returned ID against its role for this issue's run — session-scoped bookkeeping, never
written to GitHub. To send a finding back, call `SendMessage` **directly, yourself**
— never through a relay fork (the original agent's completion lands with its spawner
either way; a relay only adds a spawn-and-wait cycle). Discard the mapping once the
issue reaches a stopping point.

**Wait for the subagent to finish; then verify, don't trust.** One call:

```bash
python3 "$SDLC" verify-exit <n> \
  --expect-stage <stage> [--pr <pr>] [--unit epic]   # worktree auto-resolved; --repo-path only to override
```

**Then, before delegating the next stage, run `sync-branch`** — every transition,
except immediately after `pass-gate`/`skip-gate` (they reconcile internally; a second
call is a harmless no-op). A conflict result routes per `references/parallelism.md`,
"Git-conflict handling". Then repeat Steps 2-3 until the unit is merged/architected,
blocked, or needs a human.

**On a CLEAN `lld-review` of a normal-epic child, run `merge-lld-doc <n>` right after
`record-design-review`, before claiming `development`** — it publishes that child's
`lld.md` onto the epic branch immediately, so the design is durable there independent
of the child branch and siblings pick it up on their next `sync-branch`. It is a
scoped no-op for a standing-epic child or a parentless issue, and a structured
conflict result (never a crash) if the epic branch moved under it. See
`references/stage-playbooks.md`, the `lld-review` exit action.

**Stage exit actions** — what each stage does as its last step, every verdict branch
included — live in `references/stage-playbooks.md`, "Stage-specific exit actions".
Follow them exactly; the gate-opening steps within them are the orchestrator's, not
the agent's.

## Step 4 — Report back to the user

When the invocation ends (Step 1 found nothing actionable): summarize every unit
touched, which stages ran, rework/resume cycles per pairing, what changed, current
state of each. Call out above the fold:

- Every `needs-human` unit **with its reason text** from `list-needs-human` (flag
  reasons that look stale).
- Every blocked unit and what it waits on.
- Every gate-pending unit, which gate PR and doc, epic-level or per-issue, and
  whether feedback was addressed this invocation.
- Every merged PR and closed issue; every epic newly `epic:architected`.
- Any epic `check-epics-closeable` newly notified.

One-unit invocations collapse to a report on that unit.

## Step 5 — Retrospective checkpoint (only after closing a feature via merge)

```bash
python3 "$SDLC" retro-check
```

`run_retro` is true once 5 or more issues have closed since the last completed
retrospective (a committed watermark file, `retro-watermark`
— no modulo tricks, so nothing is skipped when several issues close between checks).
When true: grep the recently merged issues' handoff comments and docs for recurring
friction — pairings that keep bouncing (`pairing-counts` gives the marker-backed
ones), docs too thin for the next stage, dead references, gates too strict/loose.
**This file and its references are the primary fix target.** Present findings in chat
and ask before editing `SKILL.md`/`references/*`/`AGENTS.md`/`CLAUDE.md`; once
approved, edit, run `retro-check --mark-done`, and commit the edits together with the
updated watermark file (message naming the retrospective), appending the dated why to
`references/history.md`.

**A retrospective is authored on its own branch, in its own worktree, and merged only
when the lane is quiet** — no live dev-lane or review-pool agent. Every stage agent is
told to Read `references/stage-playbooks.md` **from its own worktree**, and several
`git fetch origin main` and merge mid-run, so merging a skill edit under a running
agent changes its instructions between two of its own reads. "The skill can move under
you" (above) is the reader's half of this; this is the author's half. Park the branch
and merge at the next quiescent point rather than blocking on it.
