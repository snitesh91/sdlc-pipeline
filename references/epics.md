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
| `gates.requiresHumanGateA` | `true` (global) | `false` = Gate A auto-passed |

Shipped: `legacy` (`driven:false`), `standing` (`epicLevelPhase`,
`childrenNeedArchitectedEpic`, `closes` all `false`), `default` (`"*"`). `product-review`
runs after `product` in **every** profile.

## How a non-standing Epic runs

```
Architecture-phase Task: architecture -> [arch-review] -> [Gate B] -> pass-gate/skip-gate publish epic-<n>/architecture.md, close the Task
LLD-phase Task:          lld (specifies every Task) -> [lld-review] -> finish-lld
Task:                    development -> [pr-review] -> auto-merge -> CLOSED
```

1. **Cut the phase-Tasks yourself** (not a subagent) as soon as the Epic exists:
   ```bash
   python3 "$SDLC" cut-phase-tasks <epic-n> [--arch-body TEXT] [--lld-body TEXT] --repo-path <p>
   ```
   Creates both, stages them `architecture` / `lld`, adds the lld-on-arch `blockedBy`
   edge (the **only** thing ordering them) and the Architecture-phase worktree.
   Idempotent. Returns `architecture_task`, `lld_task`. (An unstaged
   phase-Task would be held back forever.)
2. Phase-Tasks branch from `origin/main`; `worktree-add` and `sync-branch` detect this
   themselves (`--base` is only an override).
3. The Architecture-phase Task finishes at its gate: `pass-gate`/`skip-gate` publish
   `architecture.md` and close it (`references/gates.md`).
4. After a clean `lld-review` (no human gate):
   ```bash
   python3 "$SDLC" finish-lld <lld-task-n> --epic <epic-n> --repo-path <p>
   ```
   Runs `publish-doc --doc lld.md` → `create-lld-tasks` → `merge-lld-doc` →
   `close-issue` and stops at the first failure (`failed_step`, `completed_steps`). All
   steps are idempotent: fix the cause and re-run it. Never create Tasks before `lld.md`
   is on origin; never close first.
5. Close a phase-Task with `close-issue`, **never `mark-issue-closed`** — that only sets
   terminal fields after a close and leaves the issue open, Stage-less, handed out again.

`merge-lld-doc` ends the design phase: verifies `epic-<n>/lld.md` on `origin/epic-<n>`,
advances every Stage-less Task under the Epic to `development` (**not** claimed), clears
the Epic's own Stage/Pipeline Status, adds `epic:architected`. **Tasks are never eligible
before `epic:architected`.**

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

- Initiative's child (Product-Roadmap Task) → `product`.
- Standing epic's child or parentless issue → `product`; a `Bug` → `architecture`.
- Non-standing Epic's child → no guess (staged explicitly, or `unstaged`).

## Which epics are exempt

- **Standing** (`epicLevelPhase: false`, shipped matching `epic:standing`) — a permanent
  bug-intake umbrella; the label is applied by hand. Each child runs `product` → `product-review` → `architecture` →
  `development` (bug fast-track included); their design stages may fan out via
  `list-design-ready` when `parallelism.designLane` > 1 (default 1)
  (`references/parallelism.md`, "Design lane"). A non-standing Epic never fans out design.
- **Legacy** (`driven: false`, shipped matching `epic:legacy`) — never run, in any flow.
  `next-action` checks it before crash recovery and returns `action: "skip"` for the
  epic and every child. Applied by hand; an epic that needs any behaviour gets the standing label or the
  default profile instead.

## Doc layout at the epic level

- Paths: `docs/sdlc/epic-<n>/architecture.md` and `.../lld.md`, authored on the
  phase-Tasks' branches (`issue-<n>/architecture.md`, `issue-<n>/lld.md`) and published
  to `epic-<n>` by `publish-doc`. Altitude rules: `references/design-doc-rules.md`. An
  Epic has no `product.md` of its own.
- `architecture.md` describes the design by component/functional area. It never
  creates, sizes or lists Tasks.
- `lld.md` has one `## Task <KEY>: <title>` subsection per Task (renumbered to
  `## Task #<n>: <title>` by `create-lld-tasks`), each with its own Footprint. A Task's
  `development` and `pr-review` read only their subsection, via `lld-section`.
- The Epic's branch is `epic-<n>`; every child, phase-Tasks included, uses `issue-<n>`.

## `lld` specifies the Tasks; `create-lld-tasks` creates them

- **Headings**: `## Task <KEY>: <title>`, `<KEY>` a slug (lowercase letters, digits,
  hyphens), e.g. `## Task skeleton-health: Add /health`. `###` also parses; a non-slug
  heading like `## Task Breakdown` is not a Task.
- **`Depends on: <KEY>`** lines (comma- or `and`-separated) only for genuine ordering.
  `create-lld-tasks` creates each Task as a sibling of the LLD-phase Task, renumbers the
  headings, and adds one `blockedBy` edge per dependency.
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

  Any heading level and a numeric prefix (`## 12. Footprint`) parse. The list ends at the
  next heading. Exact file paths or directory-prefix globs only; no mid-path wildcards.
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
  2. It runs `architecture` → `arch-review` → Gate B from the published
     `epic-<n>/architecture.md`; `pass-gate`/`skip-gate` publish the revised doc over it
     and close the Task.
  3. Once its gate PR is open, park the reporting unit:
     `pause-for-epic-regate <n> --epic <epic-n> --gate-pr <pr>` (resets Pipeline Status
     to `Todo` only, posts a linking comment; the `blockedBy` edge holds it until the revision
     closes). An LLD-phase Task then resumes `lld` against the revised doc; if `lld.md`
     must change, re-run the LLD pass before the affected Tasks proceed.
  4. Valve: count the `lld`/`development` <-> `architecture-revision` pairing on its
     own. The third unsettled deviation on the same Epic swaps in the context-reset
     replacement architect for rounds 4–6; the sixth → `mark-needs-human` **on the Epic**.

All other rework follows `references/rework.md`.

## Bug fast-track — architecture first (standing-epic children only)

A `Bug` under a **standing** epic (or parentless) skips `product` and starts at
`architecture` on a fresh `issue-<n>` branch that `architecture` creates. A bug under a
non-standing Epic is routed by hand (`unstaged`).

On that first `architecture` pass with no `product.md`, decide explicitly whether a
product decision is needed:

- **No** (root cause known, fix scoped, no business tradeoff) → cite the issue body as
  requirements and state "bug fast-track — no product.md". **Gate A is skipped**; Gate B
  is the only human checkpoint.
- **Yes** → don't guess. Spawn a **fresh** `sdlc:product` agent (Opus) with the question
  and full context; its `product.md` gets `product-review` and Gate A (per profile)
  before architecture continues.

Only that first pass is affected; later rework follows the normal paths.

## The epic integration branch

- A non-standing Epic owns `epic-<n>`. `publish-doc` creates it from `main` on first
  publish; `worktree-add <n> --unit epic` pushes a fresh one. No setup step.
- **Functional Tasks branch from `epic-<n>` and merge into it**, never into `main`. At
  close it takes one merge from `origin/main`, is verified whole, and merges to `main`
  once.
- **Straight into `main`** only: a standing epic's children, a parentless issue, an
  Initiative's Product-Roadmap Task. `integration_base()` decides from the native
  `parent` relationship, never a label or branch name.
- **Every gate PR is `issue-<n>` → `main`**, phase-Tasks included. Docs reach `epic-<n>`
  only via `publish-doc` — never commit to it directly, never delete it.
- Epic git work (`publish-doc`, `merge-lld-doc`, `close-epic`) never runs in the main
  checkout — only in `epic-<n>`'s live or ephemeral worktree, under the branch lock.
- **Runtime stack**: when `pipeline.stack.enabled`, run `provision-epic-stack <n>` at the
  epic's first touch and `teardown-epic-stack <n>` after the closing merge
  (`references/parallelism.md`, "Per-epic isolated stack").

## Epic closing

`pipeline.epicClose.auto` (default `false`): **off** — closing is the operator's call;
**on** — the orchestrator runs the closing verification and, if clean, makes the second
`close-epic` call itself.

`check-epics-closeable` (idempotent) posts a one-time checklist on each open epic whose
children are **all** closed and assigns the operator. It auto-verifies children closed,
no open dependents (native `blocking`), and both `architecture.md` and `lld.md` on
`epic-<n>` (`docs_missing_from_epic_branch` — a doc left only on a phase-Task branch
never reaches `main`; publish it). Remaining items: closing verification, then the merge.

**`close-epic <n>` is two calls; every refusal is a structured exit-0 result.**

1. First call: refuses on open children (`open_children`); otherwise reconciles
   `epic-<n>` with `origin/main` and stops.
2. Run the closing verification against the reconciled tree; record each half.
3. Second call: refuses on missing evidence (`missing_verification`) or failing
   integration-PR checks; otherwise merges `epic-<n>` to `main`.
4. `teardown-epic-stack <n>` (no-op unless provisioned).

**Closing verification — two runs in parallel, both by the pipeline**: the **full e2e
suite** (against the epic's stack, built from the reconciled branch) and an
**exploratory pass** (`sdlc:exploratory`). Record each with
`record-epic-verification <n> --kind e2e|exploratory`. Run both in the epic branch's
worktree (`worktree-add <n> --unit epic`), never the main checkout. Evidence older than the last
`origin/main` reconcile counts as missing. **Never record a verification you did not run
clean.**

**With `epicClose.auto` on, escalate instead of closing when:**

- a Blocker/Critical delta or a manual-testing bug was filed against the epic;
- a verification could not be obtained (e.g. the repo's e2e seed does not create the
  users the suite needs). With no record `close-epic` refuses — report the epic as
  close-ready pending a runnable e2e, and stop. Never fabricate the record.

**The full e2e suite runs once, at epic close**, owned by the epic's e2e-test Task —
sequence it after the Tasks whose surfaces it proves. Per-child `development` runs only
the specs for surfaces that child moved.

**Triage closing-run deltas:**

- **Blocker / Critical** — a stable, re-confirmed delta on a spec for a surface this epic
  moved, plus **any** access-boundary delta or 5xx/crash → file against **the epic**; it
  blocks close. Route the `unstaged` child by hand (`set-stage <n> --stage development`,
  or an Architecture revision Task if the fix doesn't fit the design).
- **Normal / Low** — unmapped surface, non-reproducible flake, or a pre-existing failure
  cluster with unchanged membership → file against the standing backlog epic.

Pin the confirmation procedure (retries, workers, what counts as a stable delta) in the
e2e-test Task's `## Task` subsection — two single zero-retry runs are not a comparison.

**Every manual-testing finding is its own `Bug` child of the epic**
(`create-issue --parent <epic> --type Bug`), never folded into the closing comment. It is
`unstaged` under a non-standing Epic — route it like a Blocker. Closing waits for it.

**Standing epics never get this check** — empty for now is not done.

## Epic board Status

The pipeline neither writes nor reads a Projects-v2 board `Status`; Pipeline Status is
the only status it maintains.

### Pipeline Status `Done`

- **Any issue closing** (`merge-pr`'s `Closes #<n>`, the epic integration PR, a human
  close) gets Stage cleared and Pipeline Status `Done` from `gate-auto-advance.yml`'s
  `mark-issue-closed` job. `close-issue` and `close-epic` also set them directly.
- `merge-lld-doc` **clears** the Epic's own fields (the Epic stays open);
  `pause-for-epic-regate` resets only Pipeline Status, to `Todo`.
