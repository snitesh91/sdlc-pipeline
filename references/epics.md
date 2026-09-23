# Epics: profiles, phase-Tasks, task sizing, and epic lifecycle

An Epic never runs a stage on its own issue: its design runs on two child phase-Tasks
(Architecture-phase, then LLD-phase), its implementation on the Tasks `lld` specifies.

## What makes an issue an Epic

Only `pipeline.classification` makes an issue an Epic — a configured Issue Type or label
(sample config: `type:epic`). Shape means nothing: a parentless `Type: Feature` is not an
Epic. A standing or legacy epic carries the classification **and** its profile label.
`next-action`, `list-parallel-ready` and `list-design-ready` refuse a number that is
neither an Epic nor an Initiative.

## Epic profiles

A profile is a label-matched bundle of toggles in `pipeline.profiles`; the first whose
`match` label is on the epic wins, `"*"` is the catch-all. Never hardcode
`epic:standing`/`epic:legacy` — the client owns the label→profile mapping.

| Toggle | Default | Non-default effect |
|---|---|---|
| `driven` | `true` | `false` = **legacy**: skip the epic and every child |
| `epicLevelPhase` | `true` | `false` = **standing**: no phase-Tasks; each child runs its own full per-issue flow into `main` |
| `childrenNeedArchitectedEpic` | `true` | `false` = children dev-lane eligible without `epic:architected` |
| `closes` | `true` | `false` = never closes, no integration branch |
| `gates.skipConfidenceThreshold` | `80` (global) | per-profile Gate B skip bar |
| `gates.requiresHumanGateA` / `gates.requiresHumanGateB` | `true` (global) | `false` = that gate is waived (`references/gates.md`, "Waived gates") |

Shipped: `legacy` (`driven:false`), `standing` (`epicLevelPhase`,
`childrenNeedArchitectedEpic`, `closes`, both `requiresHumanGate*` all `false`), `default`
(`"*"`). `product-review` runs after every `product` unless a standing child is routed
past it.

## How a non-standing Epic runs

```
Architecture-phase Task: architecture -> design PR -> arch-review -> merged into epic-<n> (skip-gate/waive-gate, or the human at Gate B) -> Task closed
LLD-phase Task:          lld (specifies every Task) -> design PR -> lld-review -> finish-lld (merge PR, create Tasks, close)
Task:                    development -> [pr-review] -> auto-merge into epic-<n> -> CLOSED
```

1. **Cut the phase-Tasks yourself** (not a subagent) as soon as the Epic exists —
   `next-action` returns `cut-phase-tasks` (on the Initiative, or on the bare Epic) until
   both exist:
   ```bash
   python3 "$SDLC" cut-phase-tasks <epic-n> [--arch-body TEXT] [--lld-body TEXT] --repo-path <p>
   ```
   Stands up `epic-<n>` on origin and its worktree (eagerly, so no child ever branches
   from `main`), creates both, stages them `architecture` / `lld`, adds the lld-on-arch
   `blockedBy` edge (the **only** thing ordering them) and the Architecture-phase
   worktree. Idempotent. Returns `architecture_task`, `lld_task`. (An unstaged
   phase-Task would be held back forever.)
2. **Every child of a non-standing Epic — phase-Tasks included — branches from
   `origin/epic-<n>`**; `worktree-add` and `sync-branch` detect this themselves (`--base` is
   only an override). Each writes its design doc at `<docRoot>/epic-<n>/architecture.md` /
   `lld.md` on its own `issue-<n>` branch (a revision edits `architecture.md` in place).
3. When `architecture` or `lld` returns, `transition` raises the **design PR**
   `issue-<n>` → `epic-<n>` (`open-design-pr`, marked so it is never mistaken for a gate or
   development PR, no `Closes #`). `arch-review`/`lld-review` review on it and
   `record-design-review` also comments the outcome there and stamps the marker with the
   PR head it reviewed (`sha:`); `merge-design-pr` refuses (`review_stale`) when the head moved
   and the doc changed since, so re-run the review on the new head and record it again (a
   base-only `sync-branch` merge does not count).
4. The **pipeline merges the design PR** (`merge-design-pr`, squash, branch kept):
   `lld-review` clean always; `arch-review` clean only above the profile's skip threshold or
   where Gate B is waived — otherwise `open-gate` gates that same PR and the human merges it
   into `epic-<n>`. `skip-gate`/`waive-gate` merge then close the Architecture-phase Task
   (`references/gates.md`).
5. After a clean `lld-review` (no human gate):
   ```bash
   python3 "$SDLC" finish-lld <lld-task-n> --epic <epic-n> --repo-path <p>
   ```
   Runs `merge-design-pr` → `create-lld-tasks` → `merge-lld-doc` → `close-issue` and stops
   at the first failure (`failed_step`, `completed_steps`; a `behind_base` refusal → `sync-branch
   <lld-task-n>`, then re-run). All steps are idempotent: fix the cause and re-run it. Never
   create Tasks before `lld.md` is on `epic-<n>`; never close first. Its numbering guard: a PR
   whose `lld.md` is the unnumbered original of the epic's numbered one merges nothing
   (`up_to_date`), so an LLD revision cannot revert `create-lld-tasks`.
6. Close a phase-Task with `close-issue`, **never `mark-issue-closed`** — that only sets
   terminal fields after a close and leaves the issue open, Stage-less, handed out again.

`merge-lld-doc` ends the design phase: verifies `epic-<n>/lld.md` on `origin/epic-<n>`,
advances every Stage-less Task under the Epic **that the doc carved** (a `## Task #<n>`
subsection carrying a `## Footprint`) to `development` (**not** claimed), clears the
Epic's own Stage/Pipeline Status, adds `epic:architected`. A child with no such
subsection, or one a Task `Realises:`, stays Stage-less and is reported in
`not_advanced` with its reason — never advance it by hand without settling that reason.
**Tasks are never eligible before `epic:architected`.**

Requirements: an Initiative-driven Epic uses its Initiative's `product.md`; an
engineering-driven Epic has only its issue body (`architecture` returns its questions for
the operator when scope is unclear).

**A Stage-less child filed after `epic:architected`** (closing Blocker, manual-testing
bug, anything late) is never staged by `next-action`; it appears in a `none` result's
`unstaged` list. Route it by hand: `set-stage <n> --stage development` when `lld.md`
covers the fix, else an Architecture revision Task.

### Where a new issue starts (`default_stage`)

Stage options: `Product` / `Architecture` / `Development` / `Testing` / `PR Review` /
`LLD` (the LLD-phase Task is staged `lld`).

- Initiative's child (Product-Roadmap Task) or parentless issue → `product`.
- Standing epic's child → none: `next-action` returns `route` and you pick its first
  stage (SKILL.md, "Routing a standing child").
- Non-standing Epic's child → no guess (staged explicitly, or `unstaged`).

## Which epics are exempt

- **Standing** (`epicLevelPhase: false`, shipped matching `epic:standing`) — a permanent
  umbrella for technical (RTB) tasks only; the label is applied by hand. Each child runs
  only the stages it needs of `product` → `product-review` → `architecture` →
  `arch-review` → `development` → `pr-review`, chosen as it goes (SKILL.md, "Routing a
  standing child"), with no human gate. Its design stages may fan out via
  `list-design-ready` when `parallelism.designLane` > 1 (default 1)
  (`references/parallelism.md`, "Design lane"). A non-standing Epic never fans out design.
- **Legacy** (`driven: false`, shipped matching `epic:legacy`) — never run, in any flow.
  `next-action` checks it before crash recovery and returns `action: "skip"` for the
  epic and every child. Applied by hand; an epic that needs any behaviour gets the standing label or the
  default profile instead.

## Doc layout at the epic level

- Paths: `docs/sdlc/epic-<n>/architecture.md` and `.../lld.md` — the one path, authored
  directly there on the phase-Tasks' `issue-<n>` branches and merged into `epic-<n>` by their
  design PRs (`verify-exit`, `lld-section`, `create-lld-tasks`, `merge-lld-doc` and
  `check-epics-closeable` all read exactly it). Standing children and Product-Roadmap Tasks
  keep `issue-<n>/`. Altitude rules: `references/design-doc-rules.md`. An Epic has no
  `product.md` of its own.
- `architecture.md` describes the design by component/functional area. It never
  creates, sizes or lists Tasks.
- `lld.md` has one `## Task <KEY>: <title>` subsection per Task (renumbered to
  `## Task #<n>: <title>` by `create-lld-tasks`), each with its own Footprint. A Task's
  `development` and `pr-review` read only their subsection, via `lld-section`.
- The Epic's branch is `epic-<n>`; every child, phase-Tasks included, uses `issue-<n>` and
  branches from `epic-<n>`.

## `lld` specifies the Tasks; `create-lld-tasks` creates them

- **Headings**: `## Task <KEY>: <title>`, `<KEY>` a slug (lowercase letters, digits,
  hyphens), e.g. `## Task skeleton-health: Add /health`. `###` also parses; a non-slug
  heading like `## Task Breakdown` is not a Task. **A `## Task` heading over prose** (a
  carving summary, a Task-to-issue table) **is not a Task either**: a Task section carries
  a `## Footprint`, and `create-lld-tasks` skips any that doesn't, reporting it in
  `skipped_sections` — put such prose under a non-`Task` heading.
- **`Depends on: <KEY>`** lines (comma- or `and`-separated) only for genuine ordering.
  `create-lld-tasks` creates each Task as a sibling of the LLD-phase Task, renumbers the
  headings, and adds one `blockedBy` edge per dependency.
- **`Realises: #<n>, #<m>`** — one line, only when the Task delivers issues that already
  exist (Epic children filed before the LLD, a bug it fixes). `create-lld-tasks` blocks
  each named issue on the new Task and records the relation in the Task's body;
  `merge-lld-doc` never advances a realised issue; when the Task's PR merges (or the Task
  is closed by `close-issue`) the pipeline closes each realised issue with a comment naming
  the Task and PR (`Closes #<n>` never fires on an `epic-<n>` merge). A `#<n>` that does
  not exist is reported in `realises_failed`. Never head a section `## Task #<n>` for a
  pre-existing issue unless that issue *is* the Task as carved.
- **Every Epic carries two standing Tasks, Integration-test and e2e-test**, specified like
  functional Tasks; they run after the functional Tasks merge and own the coverage unit
  tests don't.
- **`Priority:` / `Effort:`** lines are optional per Task (`agents/lld.md`, "The
  document"); `create-lld-tasks` sets them, else `pipeline.issueDefaults`.
- A Task must carry its own Footprint and real `blockedBy` edges — `list-parallel-ready`
  orders from those, never from prose. Only a *cross-epic* dependency uses `mark-blocked`.

### How to size the Tasks: one bounded concern each, not one unit of effort each

A Task is what `development` loads into a fresh context and `pr-review` judges as one PR.

- **One Task = one bounded concern, one shippable PR** — a component/module end-to-end, or
  one capability as a thin vertical slice; never a horizontal layer (layers make every
  Task touch the same files).
- **The goal is non-overlapping footprints, not small Tasks.** Splitting on size alone
  yields several Tasks editing one module — the worst shape.
- **Split on a footprint collision or a concern boundary, never on effort.** A Task
  carrying more than one independently shippable capability is too big; large but one
  concern stays whole with `Effort: High`.
- **Never below a shippable slice.** Each Task is implementable, unit-testable and a
  meaningful PR alone. Two pieces sharing a footprint, or one inert without the other,
  are one Task; a piece that only makes sense after another settles is a `Depends on:`,
  not a merge into an unrelated Task.
- **Size for a provable design, never a target count.**
- **Footprint format — parsed by `list-parallel-ready`; not negotiable.** Each Task
  subsection of the Epic's `lld.md` (or a standing child's own `architecture.md`)
  **must** carry a `Footprint` heading followed by one backtick-wrapped path per `- `
  bullet:

  ```markdown
  ## Footprint

  - `backend/src/notifications/**`
  - `frontend/app/(admin)/sellers/**`
  ```

  The heading is exactly `Footprint` at any level (a numeric prefix `## 12. Footprint` or a
  trailing colon parse; `## Footprint overlap ...`, a backticked mention or a fenced
  example does not). Lines before the bullets (a bold label, a sentence) are skipped. The
  owned list ends at the next heading or at a `**Verify-only:**` sub-label: paths under it
  are read, not owned, and never count as overlap — a Task that owns nothing (a standing
  test Task) still schedules; only a *missing* section excludes a Task. Exact file paths
  or directory-prefix globs only; no mid-path wildcards.
- **A shared contract no path shows gets a `Contract` heading** in the Task subsection:
  an API shape, a pinned count or allow-list size, a DB invariant, an enum's members. One
  bullet each, `reads:` or `changes:` —

  ```markdown
  ## Contract

  - changes: `GET /api/pinned` response — pinned-count max 12
  - reads: seller-role permission set
  ```

  `lld` declares them when it carves the Tasks. `lld-review`, and the orchestrator before
  running two Tasks at once, treat a `changes:` that another in-flight Task `reads:` or
  `changes:` like a footprint overlap.
- **An Epic's `architecture.md` carries no Footprint** — nothing reads one there.
- A missing or unparseable footprint excludes the Task from the parallel lane ("cannot
  verify non-overlap", never "no risk") and is an `lld-review` finding, as is a footprint
  overlapping a sibling's or another epic's active Task.

## Architecture deviation escalation

The Epic's `architecture.md` is settled at its Gate B. Any later unit (LLD-phase Task, a
Task in `development`, a review) first decides whether its work fits it:

- **Fits** (the common case, bug or feature) → proceed.
- **Contradicts it** (new component boundary, unanticipated data-model change, a
  permission model the design assumed that doesn't hold) → stop and revise:
  1. Cut an Architecture revision Task, with `--blocks` for every unit that must wait:
     ```bash
     python3 "$SDLC" open-arch-revision <epic-n> --title "Architecture revision: <deviation>" \
       --body "<the deviation, and the unit that found it>" --blocks <affected-n> ... --repo-path <p>
     ```
     Returns `revision_task`.
  2. It runs `architecture` → design PR → `arch-review` from the merged
     `epic-<n>/architecture.md`, edited in place; the revised doc reaches `epic-<n>` the same
     way (`skip-gate`/`waive-gate`, or the human's merge at Gate B) and the Task closes.
  3. Park the reporting unit at once:
     `pause-for-epic-regate <n> --epic <epic-n> [--gate-pr <pr>] --found-by <stage>` (resets
     Pipeline Status to `Todo` only, posts a linking comment; `--gate-pr` only once the
     revision's design PR exists; the `blockedBy` edge holds it until the revision closes). An LLD-phase Task then resumes `lld` against the revised doc; if `lld.md`
     must change, re-run the LLD pass before the affected Tasks proceed.
  4. Valve: count the `lld`/`development` <-> `architecture-revision` pairing on its
     own. The third unsettled deviation on the same Epic swaps in the context-reset
     replacement architect for rounds 4–6; the sixth → `mark-needs-human` **on the Epic**.

All other rework follows `references/rework.md`.

## The epic integration branch

- A non-standing Epic owns `epic-<n>`, created eagerly: `cut-phase-tasks` and
  `open-arch-revision` run `worktree-add <n> --unit epic` (which pushes a fresh branch from
  `main`), and any child's `worktree-add` cuts a missing one first.
- **Functional Tasks branch from `epic-<n>` and merge into it**, never into `main`. At
  close it takes one merge from `origin/main`, is verified whole, and merges to `main`
  once.
- **Straight into `main`** only: a standing epic's children, a parentless issue, an
  Initiative's Product-Roadmap Task (their gate PRs are `issue-<n>` → `main`).
  `integration_base()` decides from the native `parent` relationship, never a label,
  stage or branch name.
- **An Epic's design docs reach `epic-<n>` only through a phase-Task's design PR**
  (`issue-<n>` → `epic-<n>`, merged by `merge-design-pr` or the human at Gate B) and
  `create-lld-tasks`' numbering commit — never commit to it by hand, never delete it while the
  Epic is open. The `close-epic` merge is the one deletion, once the Epic is done.
- **Epic branch rot:** `epic-<n>` takes one merge from `main` at close, so long-lived Epics
  drift. Run `sync-branch <epic> --unit epic --repo-path <p>` at the start of each
  invocation on the Epic and whenever `main` moved under it (SKILL.md, Step 1), so every
  branch cut from it starts close to `main` and `close-epic`'s reconcile stays small.
- Epic git work (`merge-lld-doc`, `create-lld-tasks`, `close-epic`) never runs in the main
  checkout — only in `epic-<n>`'s live or ephemeral worktree, under the branch lock.
- **Runtime stack**: when `pipeline.stack.enabled`, run `provision-epic-stack <n>` at the
  epic's first touch; `close-epic`'s merge tears it down (`references/parallelism.md`,
  "Per-epic isolated stack").

## Epic closing

`pipeline.epicClose.auto` (default `false`): **off** — closing is the operator's call;
**on** — the orchestrator runs the closing verification and, if clean, makes the second
`close-epic` call itself.

`check-epics-closeable` (idempotent) posts a one-time checklist on each open epic whose
children are **all** closed and assigns the operator. It auto-verifies children closed,
no open dependents (native `blocking`), and both `architecture.md` and `lld.md` on
`epic-<n>` (`docs_missing_from_epic_branch` — a doc left only on a phase-Task branch
never reaches `main`; merge its design PR). Remaining items: closing verification, then the merge.

**`close-epic <n>` is two calls; every refusal is a structured exit-0 result.**

1. First call: refuses on open children (`open_children`); otherwise, when `epic-<n>` is
   behind `origin/main`, reconciles it and stops (`reconciled`); when already current it
   goes straight to the evidence check below. Every result carries the required suites the
   epic's changes cover that nothing yet satisfies at the epic head — `unattested_suites`
   (attestable: run and attest) and `awaiting_checks` (`attestable: false`: only the epic
   PR's check can satisfy them; never run locally) — and `evidence` (`exploratory` and each
   suite: `fresh` | `carried_forward_from <sha>` | `children:#a,#b` | `passing_check` |
   `not_required` | `missing`), with `carried_forward_files` (the delta it judged safe) and
   `evidence_breaks` (why a suite's child chain failed: the child PR or direct commit that
   breaks it).
2. Run the exploratory pass against the reconciled tree and record it. Also run
   every suite in `unattested_suites`.
3. Second call: refuses on missing or stale evidence (`missing_verification`) or a required
   suite still `missing` (`checks: missing-checks`); otherwise merges `epic-<n>` to `main`.
   A failed, pending or never-started GitHub Actions run never blocks this merge by itself —
   it only leaves a suite unsatisfied, which matters for one in `awaiting_checks`. The epic
   PR exists only from this call, so attest each suite it names right after it
   (`record-local-ci --pr <pr>` on the epic head), wait for `awaiting_checks` to pass, and
   call again. After the merge it cleans up (`cleanup`, `children_cleanup`): the epic's and
   its closed children's worktrees and landed branches on origin and locally, the run-state
   file archived (`run_state`; `prune-stale` deletes it once stale), and
   `teardown-epic-stack` (`stack`; no-op unless provisioned).

**Evidence carry-forward — what a later commit on `epic-<n>` does to recorded evidence:**

- The exploratory record stays valid across a delta that touches only
  `pipeline.epicClose.evidenceCarryForward.paths` (default: `**/*.md`, `docs/**`,
  `<docRoot>/**`; the pipeline config never). Any other path, or a delta of 300+ files,
  stales it: re-run and re-record. Widen the set in config for fixture or evidence
  directories you know cannot change runtime behaviour; never widen it to source or tests.
- A suite attestation on the epic PR carries forward when nothing the suite's workflow
  covers changed since its stamped sha (the `merge-pr` rule).
- A suite with no attestation on the epic PR is still satisfied when every merged child PR
  that touched its paths carries an attestation or passing check at its merged head and no
  direct commit on `epic-<n>` (a `main` reconcile, a closing-delta fix) touches those paths;
  otherwise `evidence_breaks` names the child PR or commit, and that suite must run on the
  epic head.
- `close-epic <n> --no-carry-forward` accepts only evidence stamped at the epic head itself:
  no delta carry-forward, no child chain.

**Preflight the machine before an e2e run** (the e2e-test Task's, or any you run yourself). An exhausted Docker VM makes a run
unrecordable (`Page crashed`, `ENOSPC`, container OOM kills). Check `docker system df`, VM disk
use (under ~80%) and host free memory; prune build cache and stop stale stacks first; restart
the frontend container between e2e chunks. A run with crash-class failures is an environment
failure: fix it and re-run, never record it.

**Closing verification — the exploratory pass** (`sdlc:exploratory`), run in the epic
branch's worktree (`worktree-add <n> --unit epic`), never the main checkout. **The full e2e
suite is not a close requirement:** every Epic has its own standing e2e-test Task, which owns
that evidence, so `close-epic` no longer asks for it. `record-epic-verification --kind e2e` is
still accepted (informational, never gates). Run every suite in `unattested_suites` with a
fresh `sdlc:development` agent and a run-only brief (fix nothing, report). **You** record the
exploratory half with `record-epic-verification <n> --kind exploratory --summary "..."` (it
stamps the tested epic-branch head; `--sha` names an earlier tested head; the summary is the
findings comment the agent returned) — an agent never records it. Evidence older than the last
`origin/main` reconcile, or predating a later change on `epic-<n>` outside the carry-forward
set ("Evidence carry-forward" above), counts as missing. **Never record a verification you
did not run clean.**

**With `epicClose.auto` on, escalate instead of closing when:**

- a Blocker/Critical delta or a manual-testing bug was filed against the epic;
- the exploratory pass could not be obtained. With no record `close-epic` refuses —
  report the epic as close-ready pending it, and stop. Never fabricate the record.

**The full e2e suite runs once, in the epic's e2e-test Task** —
sequence it after the Tasks whose surfaces it proves. Per-child `development` runs only
the specs for surfaces that child moved.

**Triage e2e-run and exploratory deltas:**

- **Blocker / Critical** — a stable, re-confirmed delta on a spec for a surface this epic
  moved, plus **any** access-boundary delta or 5xx/crash → file against **the epic**; it
  blocks close. Route the `unstaged` child by hand (`set-stage <n> --stage development`,
  or an Architecture revision Task if the fix doesn't fit the design).
- **Normal / Low** — unmapped surface, non-reproducible flake, or a pre-existing failure
  cluster with unchanged membership → file against the standing backlog epic.

The e2e-test Task's `## Task` subsection states what counts as a stable delta (its
evidence goal — two single zero-retry runs are not a comparison); how the suite runs is
`development`'s, never the LLD's (`agents/lld.md`, "The document").

**Close-blocker lane — only with operator authorisation.** A Blocker/Critical fix that
passes the *fits* test ("Architecture deviation escalation") may skip `lld` and go
straight to `set-stage <n> --stage development` against the epic branch; without the
operator's say-so it runs the full lane. A fix that lands on `epic-<n>` after the tested head
is judged by "Evidence carry-forward" above: `close-epic`'s `evidence` field says what still
stands and what must be re-run and re-recorded.

**Clean the epic worktree before the closing merge.** The exploratory pass and the
`unattested_suites` runs leave the epic-branch worktree dirty (evidence JSONs, `uploads/`,
build output); `close-epic`'s reconcile/merge works from that worktree. Discard those
untracked/working-tree changes yourself (they are never committed to `epic-<n>`) before the
second `close-epic` call, or the merge carries them.

**Every manual-testing finding is its own `Bug` child of the epic**
(`file-closing-delta <epic> --title .. --body .. [--priority P] [--effort High|Medium|Low]`),
never folded into the closing comment. It is `unstaged` under a non-standing Epic — route it
like a Blocker, or add `--start` (operator-authorised close-blocker lane) to stage `development`
and start it in one call. Closing waits for it.

**Standing epics never get this check** — empty for now is not done.

## Epic board Status

The pipeline neither writes nor reads a Projects-v2 board `Status`; Pipeline Status is
the only status it maintains.

### Pipeline Status `Done`

- **A pipeline close** — `merge-pr`, `close-issue`, `close-epic`, `close-initiative`, a
  realised issue — clears Stage and sets Pipeline Status `Done` itself. A human close leaves
  them as they were; every reader filters to open issues, so it is cosmetic.
- `merge-lld-doc` **clears** the Epic's own fields (the Epic stays open);
  `pause-for-epic-regate` resets only Pipeline Status, to `Todo`.
