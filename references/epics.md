# Epics: profiles, phase-Tasks, task sizing, and epic lifecycle

Referenced from `SKILL.md`. An Epic never runs a stage itself — its design work is its
own phase-Tasks (an Architecture-phase Task, then an LLD-phase Task), and its
implementation is the Tasks the LLD phase specifies. This file owns everything
epic-shaped: how an issue is recognised as an Epic, profiles, which epics are exempt,
task sizing and footprints, the architecture deviation escalation, the standing-epic
bug fast-track, and epic closing/board status.

## What makes an issue an Epic

An issue is an Epic **only** when `pipeline.classification` says so — a configured
Issue Type or label (the sample config uses the `type:epic` label; see
`references/operations.md`). Shape alone means nothing: a parentless `Type: Feature`
issue is not an epic. A standing or legacy epic is an Epic too — it carries the
classification **and** its profile label (`epic:standing` / `epic:legacy`).
`next-action`, `list-parallel-ready` and `list-design-ready` refuse any number that
classifies as neither an Epic nor an Initiative.

## Epic profiles

An epic's behaviour is set by its **profile** — a label-matched bundle of toggles in
`pipeline.profiles` (config; see `references/operations.md`). `resolve_profile(epic)`
walks the ordered list and returns the first profile whose `match` label is on the epic,
falling back to the `"*"` catch-all. The skill does not hardcode the
`epic:standing`/`epic:legacy` labels; a client owns the label→behaviour mapping and can
match `epic:standing`, `RTB`, or anything it likes. The toggles:

| Toggle | Default | Effect when non-default |
|---|---|---|
| `driven` | `true` | `false` = **legacy**: pipeline skips the epic and every child |
| `epicLevelPhase` | `true` | `false` = **standing**: no Architecture-/LLD-phase Tasks; each child runs its own full per-issue flow and integrates into `main` |
| `childrenNeedArchitectedEpic` | `true` | `false` = children eligible for the dev lane without the epic being `epic:architected` |
| `closes` | `true` | `false` = epic never closes and has no integration branch |
| `gates.skipConfidenceThreshold` | `80` (global, operator instruction 2026-09-16 — was 95) | per-profile Gate B skip bar |
| `gates.requiresHumanGateA` | `true` (global) | `false` = Gate A auto-passed (no human) |

The three shipped profiles: `legacy` (`driven:false`), `standing` (`epicLevelPhase`,
`childrenNeedArchitectedEpic` and `closes` all `false`), and `default` (`"*"`, all
defaults). `is_epic_standing()` / `is_epic_legacy()` are thin reads of
`epicLevelPhase` / `driven`. **`product-review` runs after `product` in every
profile** — it is not a per-profile toggle.

## How a non-standing Epic runs

A **non-standing Epic** (the `default` profile) never runs a stage on its own issue. Its design is two
ordinary child issues the orchestrator cuts (see "Cutting an Epic" in `SKILL.md`),
each staged with `set-stage`, gated to `main` like any other issue, and ordered by a
native `blockedBy` edge:

```
Architecture-phase Task: architecture -> [arch-review] -> [Gate B] -> pass-gate/skip-gate publishes epic-<n>/architecture.md, closes the Task
LLD-phase Task:          lld (specifies every Task) -> [lld-review] -> publish-doc, create-lld-tasks, merge-lld-doc <epic>, close-issue
Task:                    development -> [pr-review] -> auto-merge -> CLOSED
```

An Initiative-driven Epic gets its requirements from its Initiative's `product.md`
(already on `main`); an engineering-driven Epic has none — its scope is written in the
issue body, and `architecture` asks the operator directly when it is unclear.

`merge-lld-doc <epic-n>` is what ends the design phase: it verifies
`epic-<n>/lld.md` is on `origin/epic-<n>`, advances every Stage-less Task under the
Epic to `development` (not claimed), clears the Epic's own Stage/Pipeline Status, and
adds `epic:architected`. **An Epic's Tasks are never eligible before it is
`epic:architected`** — `next-action` holds every Stage-less child back until then, and
`list-parallel-ready` returns empty for the epic.

**A Stage-less child that appears after `epic:architected`** — a closing-verification
Blocker, a manual-testing bug, anything filed late — has no stage `default_stage()` can
guess. `next-action` never stages it; it reports it in a `none` result's `unstaged`
list. Route each one by hand: `set-stage <n> --stage development` when the Epic's
`lld.md` already covers the fix, otherwise cut an Architecture revision Task (below).

### LLD is its own Stage value

The native Stage field has six options: `Product` / `Architecture` / `Development` /
`Testing` / `PR Review` / `LLD`. The LLD-phase Task is cut at `lld` via `set-stage`;
every command treats it as an ordinary distinct value. (It previously overloaded
`Architecture`, relying on prose to tell the two apart.)

### Where a new issue starts (`default_stage`)

- **An Initiative's child** (its Product-Roadmap Task) → `product`.
- **A standing epic's child, or a parentless issue** → `product`; a `Bug` fast-tracks
  to `architecture` (below).
- **A non-standing Epic's child** → no guess (see above).

## Which epics are exempt

Both cases are **profiles** (above), not hardcoded labels:

- **A standing profile** (`epicLevelPhase: false`, shipped matching `epic:standing`) — a
  permanent bug-intake umbrella with no fixed scope to design as a whole; its children
  run the full per-issue `product` → `product-review` → `architecture` →
  `development` flow (bug fast-track included). Resolved via `resolve_profile`;
  `is_epic_standing()` reads it. Those per-child `product`/`architecture` stages
  **fan out concurrently** through the **design lane** (`list-design-ready`, cap
  `parallelism.designLane`, default 2 — set below the dev-lane cap because both stages
  run opus) rather than running one at a time; a non-standing Epic has no such fan-out
  (its design is one Architecture-phase Task then one LLD-phase Task). See
  `references/parallelism.md`, "Design lane".
- **A legacy profile** (`driven: false`, shipped matching `epic:legacy`) — **not run by
  this pipeline at all, in any flow.** `decide_next_action` checks it first — before
  crash-recovery — and returns `action: "skip"` for the epic and every child. Applied by
  hand to an epic the operator has decided is out of scope. Wanting a *behaviour* for a
  legacy epic is a sign it shouldn't match the legacy profile — give it the standing
  profile's label or leave it on the default.

## Doc layout at the epic level

`docs/sdlc/epic-<n>/architecture.md` and `.../lld.md` — authored on the phase-Tasks'
own branches (`issue-<n>/architecture.md`, `issue-<n>/lld.md`) and published to the
Epic-scoped path on `epic-<n>` by `publish-doc`, so every reader finds them at one path
regardless of which Task produced them. Same "Document altitude" rules and templates
as any design doc (see `references/design-doc-rules.md`). An Epic has no `product.md`
of its own — an Initiative-driven Epic's requirements are its Initiative's
`product.md`, already on `main`.

- **`architecture.md` describes the Epic's design by component/functional area** — it
  does not create, size or list Tasks; that is `lld`'s job.
- **`lld.md` gives each Task its own `## Task #<n>: <title>` subsection** (written as
  `## Task <KEY>: <title>` and renumbered by `create-lld-tasks`), each with its own
  parseable `## Footprint`. A Task's `development` and `pr-review` read only their own
  subsection via `lld-section`.

The epic's branch is `epic-<n>` (not `issue-<n>`) — see `references/parallelism.md`,
"Working on a branch". Every child, phase-Tasks included, branches as `issue-<n>`.

## `lld` specifies the Tasks; `create-lld-tasks` creates them

The LLD-phase Task has visibility into the whole Epic design while writing `lld.md`,
so carving the work into Tasks is its job — not `architecture`'s:

- **One `## Task <KEY>: <title>` subsection per Task**, with `Depends on: <KEY>` lines
  only for genuine ordering constraints. Once `lld-review` is clean the orchestrator
  runs `publish-doc`, then `create-lld-tasks <epic> --repo-path <p>`, which creates each
  Task issue as a sibling of the LLD-phase Task, rewrites the headings to real issue
  numbers, and applies a `blockedBy` edge per `Depends on:` line.
- **Every Epic always carries two standing Tasks** — Integration-test and e2e-test —
  specified the same way as functional Tasks. They run after the functional Tasks
  merge and own the coverage a normal Task's unit tests don't.
- **Effort** is set by hand; there is **no `sdlc_next.py` command for it**, and an issue
  filed mid-epic carries **no Effort at all**, by design. Nothing in the lane reads
  Effort — `next-action`, `list-parallel-ready` and every gate ignore it; it is a
  human-facing estimate. Don't burn calls trying to set it programmatically.
- **What each Task genuinely must carry is its own `## Footprint` list** (below), plus
  real `blockedBy` edges for genuine ordering constraints. `list-parallel-ready` derives
  ordering from those, never from prose.

Only a genuine *cross-epic* dependency uses `mark-blocked`.

### How to size the Tasks: one component each, not one unit of effort each

`Effort: High` on its own is **not** a reason to split a Task, and "split anything
big" is not the instinct. The bias is the opposite:

- **Prefer fewer, larger Tasks, each owning one component/module/directory
  boundary end-to-end.** One Task touching one component deeply beats three Tasks
  each touching a slice of the same files.
- **The goal is non-overlapping file footprints, not smaller tasks.** Two siblings
  should be workable without touching the same files — that's what makes them safe to
  parallelize; splitting on size alone produces several small tasks all editing the
  same module, the worst possible shape.
- **The reason to split is footprint collision, not effort.** Work that would
  inevitably interleave with a sibling's files is a real split (or merge). Large but
  self-contained in one component: leave whole, set `Effort: High` honestly.
- **Checkable requirement — the `## Footprint` section.** Each Task's `## Task #<n>`
  subsection of the Epic's `lld.md` (or, for a standing-epic child, that child's own
  `architecture.md`) **must** carry a `## Footprint` heading, followed by a plain bullet
  list of the directories/modules it expects to touch, **each path backtick-wrapped,
  one per bullet** — this exact shape, because `list-parallel-ready` parses it
  mechanically (`parse_footprint` / `parse_task_footprint` in `sdlc_next.py`):

  ```markdown
  ## Footprint

  - `backend/src/notifications/**`
  - `frontend/app/(admin)/sellers/**`
  ```

  A numeric section prefix is tolerated — `architecture.template.md` numbers its
  sections, so `## 12. Footprint` parses identically to a bare `## Footprint`. Nothing
  else about the shape is negotiable.

- **Beyond paths — the `## Contract` note.** A path footprint catches two Tasks that
  edit the same file; it is blind to two Tasks that break each other through a shared
  *contract* they never co-locate in a file — an API request/response shape, a pinned
  count or allow-list size, a DB invariant, an enum's membership. This has shipped a
  real break: one Task changed an endpoint's pinned allow-list count and silently broke
  a sibling's assertion of that count, though the two shared no path. So each `## Task`
  subsection that **depends on or changes** such a contract adds a `## Contract`
  heading — a plain bullet list, one contract per bullet, each naming the shared surface
  and whether the Task `reads:` or `changes:` it:

  ```markdown
  ## Contract

  - changes: `GET /api/pinned` response — pinned-count invariant (max 12)
  - reads: seller-role permission set
  ```

  `lld` declares these in the same session it carves the Tasks. The orchestrator raises
  a **cross-session notice** when two in-flight units declare an overlapping contract
  (one `changes:` what another `reads:` or `changes:`), the same way `lld-review` flags
  overlapping path footprints. A contract no sibling touches needs no notice — the note
  costs nothing then and catches the collision the day a later sibling adds one.

  **An Epic's `architecture.md` carries no Footprint section at all.** Nothing ever
  parses one: `read_footprint` reads a standing child's `issue-<n>/architecture.md`, or
  the Task's own subsection of `epic-<n>/lld.md` — never the Epic's architecture doc.

  Exact file paths and directory-prefix globs only; no mid-path wildcards. A
  non-parseable or missing footprint excludes the Task from the parallel lane
  ("cannot verify non-overlap", never "no footprint, no risk") — and is an
  `lld-review` finding in its own right. `lld-review` also flags a Task whose stated
  footprint overlaps a sibling's, including currently-active Tasks from another epic.

## Architecture deviation escalation

The Epic's `architecture.md` is settled at its Architecture-phase Task's Gate B. When a
later unit finds the design doesn't hold — the LLD-phase Task carving Tasks, a Task in
`development`, a review — its first move is deciding whether the work fits the
existing design:

- **Fits** (the overwhelming common case) → proceed, no escalation. True for bugs and
  features alike; "deviation" is about the *design*, not issue type.
- **Contradicts the design** (new component boundary, unanticipated data-model
  change, a permission model the design assumed doesn't hold) → stop and revise the
  design through an **Architecture revision** phase-Task:
  1. Cut it under the Epic, mechanically identical to the Architecture-phase Task:
     ```bash
     python3 "$SDLC" create-issue --parent <epic-n> --title "Architecture revision: <deviation>" ...
     python3 "$SDLC" set-stage <rev-n> --stage architecture
     python3 "$SDLC" worktree-add <rev-n> --base origin/main
     python3 "$SDLC" add-blocked-by <affected-n> --on <rev-n>   # each unit that must wait
     ```
     Its body names the specific deviation and the unit that found it.
  2. It runs `architecture` → `arch-review` → Gate B like any Architecture-phase Task,
     starting from the currently published `epic-<n>/architecture.md`. On
     `pass-gate`/`skip-gate` the revised doc is published over
     `epic-<n>/architecture.md` and the revision Task closes.
  3. Park the reporting unit once the revision's gate PR is open:
     `pause-for-epic-regate <n> --epic <epic-n> --gate-pr <pr>` — clears its Pipeline
     Status only (Stage is kept) and posts a linking comment; its `blockedBy` edge
     keeps it from being picked until the revision closes. An LLD-phase Task that found
     the deviation resumes `lld` against the revised doc.
  4. Escalation valve: track as its own pairing (`lld`/`development` <->
     `architecture-revision`); a third deviation against the same epic's design without
     settling swaps in the context-reset replacement architect for rounds 4–6, and a
     sixth means `mark-needs-human` **on the epic** — the problem is epic-level.

This is deliberately the *only* extra escalation path this model needs — every other
rework/blocker case follows `references/rework.md`.

## Bug fast-track — architecture first (standing-epic children only)

An issue whose Type is `Bug`, filed against a **standing** epic (or with no parent),
skips `product` at filing time and starts directly at `stage:architecture`, on a
freshly created `issue-<n>` branch (architecture creates the branch for these). A bug
under a non-standing Epic is routed by hand instead — see "How a non-standing Epic
runs".

**Architecture's first move on a bug with no `product.md`**: decide explicitly whether
the bug needs a product decision. Most well-diagnosed reports (root cause identified,
fix scoped, no business tradeoff) don't — the issue body is sufficient requirements.

- **No product input needed** → write `architecture.md` citing the issue body as the
  requirements source; state "bug fast-track — no product.md" explicitly. **Gate A is
  skipped entirely** — nothing exists to gate; Gate B becomes the sole human
  checkpoint. Continue as any other architecture stage.
- **Product input needed** → don't guess. Spawn a **fresh** `sdlc-product` agent
  (Opus) with the specific question and full context, get `product.md`
  written and the decision locked, then continue architecture with the answer. The
  new `product.md` gets a normal Gate A before architecture continues.

This governs only the very first `architecture` pass on an issue that started there
with no `product.md`. Any issue that has one keeps it, and later rework follows the
normal paths.

## The epic integration branch

A non-standing Epic owns a long-lived branch, `epic-<n>`. `publish-doc` creates it on
origin from `main` the first time it lands a phase-Task's doc (and `worktree-add
<n> --unit epic` pushes a fresh one when it cuts it). **Every Task's `issue-<n>`
branches from it and merges into it**, not into `main`; the Epic's `architecture.md`
and `lld.md` are published onto it. The branch takes one merge *from* `origin/main` at
close, is verified as a whole, and merges to `main` once.

The phase-Tasks are the exception inside the Epic: their gates target `main`, so they
are cut with `worktree-add <n> --base origin/main` and reconciled with `sync-branch <n>
--base origin/main` — `integration_base` cannot tell them apart from a functional Task.

Alongside the branch, an epic may own a **runtime stack** — `provision-epic-stack
<n>` at its first touch when `pipeline.stack.enabled` — so its e2e-running Tasks
and its closing run never depend on, or wipe, the shared dev stack
(`references/parallelism.md`, "Per-epic isolated stack"). Nothing of the epic's git
work ever runs in the main checkout: `publish-doc`, `merge-lld-doc` and `close-epic`
operate on `epic-<n>` in its live worktree or an ephemeral one, under the branch's lock.

The point is where conflicts surface. Under trunk-based children, every child
integrates against a `main` that moves under it, and two siblings can each be green
independently yet break `main` together — a semantic conflict no textual merge check
catches. Deferring integration to the epic branch means that collision surfaces once,
against a tree where every sibling is already present, and is resolved before anything
reaches `main`.

Three cases still integrate straight into `main`, and none is a compatibility hedge:

- **A standing epic's children** (`epic:standing`, e.g. the standing backlog epic). A standing epic never
  closes, so its integration branch would never merge and would diverge without bound.
- **A top-level issue with no parent epic.** There is nothing to integrate into.
- **An Initiative's Product-Roadmap Task.** An Initiative has no branch of its own.

`integration_base()` decides this mechanically, from the native `parent` relationship —
never from a label or a naming convention. It reads `issue_list`'s GraphQL, because
`gh issue view --json` has no `parent` field at all; the first cut read it from
`issue_view` and every child silently resolved to `main`.

**Every gate PR is `issue-<n>` → `main`**, unsquashed — a standing child's, a
parentless issue's, and a phase-Task's alike. A phase-Task's doc reaches `epic-<n>`
only through `publish-doc` (which `pass-gate`/`skip-gate` run for an Architecture-phase
Task), and for a still-open epic that is where the docs are authoritative and reachable
by name — what `check-epics-closeable` verifies. `close-epic`'s final merge carries
them to `main`, together with everything else the epic produced.

## Epic closing

Who pulls the trigger depends on `pipeline.epicClose.auto` (config, default
`false`). With it **off**, closing an epic is a milestone-level call only the human
makes. With it **on** (Bookshaw), the orchestrator runs the closing verification and,
if it comes back clean, invokes the second `close-epic` call itself — no human
trigger. Either way the CLI tells you the moment an epic is ready, exactly once:

```bash
python3 "$SDLC" check-epics-closeable
```

For every open epic whose children are **all** closed, posts a one-time checklist
comment and assigns the operator (idempotent — detects its own prior marker):

1. All child issues closed *(auto-verified)*
2. No open issue elsewhere depends on a closed child *(auto-verified via the native
   `blocking` relationship)*
3. The Epic's `architecture.md` and `lld.md` are both on `epic-<n>` — the two docs
   `publish-doc` lands there (an Initiative-driven Epic's `product.md` belongs to the
   Initiative and is already on `main`) *(auto-verified —
   `docs_missing_from_epic_branch` in the result)* — a doc left only on a phase-Task's
   `issue-<n>` branch is reachable only by SHA, and a SHA quoted from an old comment
   resolves to whatever draft it pointed at (see `references/history.md`, 2026-08-20);
   it would also never reach `main`, since `close-epic`'s merge of `epic-<n>` is what
   lands the docs there
4. Closing verification run on `epic-<n>` after merging `origin/main` into it — the full
   e2e suite and an exploratory pass, in parallel
5. `epic-<n>` merged to `main`

Every item is mechanical or executed by the pipeline. The judgment items that used to
sit here — "no silent scope drift", "architecture docs reflect final state", "delivered
scope matches stated purpose" — were removed rather than automated: they restated what
Gate A, Gate B and each child's `pr-review` already decide, and a checklist item nobody
can fail is worse than no item, because it reads as verification.

`close-epic <n>` performs the close and refuses, as a structured exit-0 result, until
it can proceed:

```bash
python3 "$SDLC" close-epic <n>
```

It is deliberately **two calls, not one**. The first reconciles `epic-<n>` with
`origin/main` and stops — verification has to run *after* the merge from `main` and
*before* the merge to `main`. The second merges, once both halves of the closing
verification are recorded on the thread. The reconcile runs in the epic branch's own
(usually ephemeral) worktree under its lock, never the main checkout. **After the
merge, `teardown-epic-stack <n>`** — compose down with volumes, remove the generated
profile files and data dir (a no-op unless the epic's stack was provisioned). The
closing e2e run itself targets that stack, built from the reconciled epic branch, not
the shared dev one.

**When `pipeline.epicClose.auto` is on, the orchestrator makes the second call
itself** the moment the close is clean — it does not hand back to the operator. "Clean"
is what the CLI already enforces plus one judgement the CLI cannot: the closing
verification produced **no Blocker/Critical delta** (those are filed against the epic
as a child, which re-trips `close-epic`'s `open_children` refusal anyway) and **no
open manual-testing bug child**. Escalate to the operator, do not auto-close, when any
of these holds:

- a Blocker/Critical delta or a manual-testing bug was filed against the epic (close
  is blocked until it resolves — report it);
- a verification **could not be obtained** — e.g. the full e2e suite cannot run
  (the e2e user-profile seeding gap is exactly this: after a db-reset the backend
  has no profiles, so e2e cannot run and its record must not be fabricated). No record
  → `missing_epic_verification` stays true → `close-epic` refuses. This is the gate,
  not a failure: report that the epic is close-ready pending a runnable e2e, and stop.

Never record a verification you did not actually run clean — "establish a number by
running the thing" (`references/stage-playbooks.md`) applies hardest here, where the
record is the only thing between a green branch and an irreversible merge to `main`.

**The closing verification is two runs in parallel, both by the pipeline:**

- the **full e2e suite**, and
- an **exploratory pass** (`sdlc-exploratory` agent) that goes looking for what a
  scripted suite cannot — the judgment half of what used to be "manual testing done by
  a human".

**The verification agents return their verdict and summary to the orchestrator; the
orchestrator records the marker and posts the epic comment — an agent never calls
`record-epic-verification` itself.** The harness permission system blocks a closing
agent's `record-epic-verification` and its epic comment as external writes, so an agent
that tried would stall on a denied write; the orchestrator holds the close decision, so
it owns the durable record too. Durable evidence lands via
`record-epic-verification <n> --kind e2e|exploratory`, run by the orchestrator once per
returned verdict (`sdlc-exploratory` is written to return, not record).
`missing_epic_verification()` requires both, **and requires each to postdate the last
`origin/main` reconcile** — evidence gathered before the final merge describes a
different tree than the one that ships. Same ordering trap `missing_pipeline_evidence`
guards one altitude down.

**The full e2e suite runs once, at epic close** (operator policy, 2026-08-22) — owned
by the epic's dedicated e2e child, which should therefore be sequenced after the
children whose surfaces it proves. Per-child, `development` runs only the specs covering the
surfaces that child moved; a full suite per child is minutes of Docker for evidence the
scoped run already gives. Triage the closing run's deltas by a stated rule, not case by
case:

- **Blocker / Critical** — a stable, re-confirmed delta on a spec attributable to a
  surface this epic moved, plus unconditionally any access-boundary delta or any
  5xx/crash → **filed against the epic itself**, and it blocks epic close. It is
  filed after `epic:architected`, so `next-action` reports it as `unstaged` — route it
  by hand (`set-stage <n> --stage development`, or an Architecture revision Task when
  the fix doesn't fit the design; see "How a non-standing Epic runs").
- **Normal / Low** — a delta on an unmapped surface, a non-reproducible flake, or a
  pre-existing failure cluster whose membership is unchanged → **filed against the
  standing backlog epic** for human triage.

A "before" and an "after" that are each a single run of a zero-retry suite is not a
comparison — pin the confirmation procedure (retries, workers, and what counts as a
stable delta) in the e2e-test Task's own `## Task` subsection of `lld.md`.

### The close-blocker lane

A Blocker/Critical delta from the closing verification is filed as an epic child and,
by default, routed by hand through the full development / Architecture-revision lane —
heavier than most close-blockers need. With **operator authorisation** — this lane is
never taken on an agent's own initiative — a close-blocker may take a lighter route:

- **lld-skip threshold.** When the fix is small and self-contained — bounded within one
  component, no new component boundary, no data-model or contract change (the *fits*
  test from "Architecture deviation escalation") — skip `product`/`lld` and route the
  child straight to a scoped `development` pass against the epic branch
  (`set-stage <n> --stage development`). A fix that fails the *fits* test still cuts an
  Architecture revision Task. The improvised route this replaces was a raw fix commit
  on the epic branch then re-running the suites; a scoped `development` pass on a child
  is that, with a PR and `pr-review` around it.
- **What a fix invalidates.** Any **non-docs** change to the epic branch invalidates
  the closing e2e *and* exploratory evidence — both must be re-run after it lands. A
  **docs-only** change invalidates neither. The e2e marker only auto-invalidates on a
  `main` reconcile (`missing_epic_verification` postdating the last reconcile); a fix
  committed *after* the reconcile does not trip it, so re-running the two verifications
  here is a **manual discipline**, not something the CLI enforces.
- **Who decides.** The operator authorises the lighter lane. Absent that, a
  close-blocker runs the full lane.

**Any issue caught by manual testing (item 6) gets filed as its own `Bug` child of the
epic** — never folded silently into the closing comment. Use `create-issue --parent
<epic>` (sets `Type: Task`; follow with `gh.set_issue_type()` if it must be `Bug` —
which only changes behavior for a standing-epic child). Under a non-standing Epic it is
Stage-less and reported as `unstaged`, routed by hand like a Blocker delta above. If
the epic was about to close, closing pauses until the new bug child resolves.

**Standing epics never get this check** — momentarily empty is not done; more bugs
will land. Apply the label by hand to any epic meant to work that way; nothing
auto-detects it.

## Epic board Status

The org board (the Projects-v2 project named in config as `projectNumber`) carries a native Projects-v2 `Status`
field (`Todo`/`In Progress`/`Done`) — distinct from the load-bearing `Pipeline Status`
Issue Field. Board `Status` is human-facing convenience only: nothing reads it back,
and every write is best-effort (`set_project_status` swallows failures — must never
block a claim or workflow run).

- **Closing an epic** flips it to `Done` — via the `mark-issue-closed` Action job
  (fired by the integration PR's `Closes #<n>` on merge, whether the operator or, when
  `pipeline.epicClose.auto` is on, the orchestrator triggered `close-epic`), for
  **every** epic regardless of standing/legacy.
- **No per-child board `Status` writes exist or are planned.**

### Pipeline Status `Done`

**Any issue closing** — `merge-pr`'s squash-merge auto-closing via `Closes #<n>`, or a
human closing directly — clears Stage (meaningless once closed) and sets Pipeline
Status to `Done`. This lives **entirely in `gate-auto-advance.yml`'s
`mark-issue-closed` job** (`issues: closed`), not in `cmd_merge_pr` — so a manual
close gets the same cleanup as a pipeline merge. `cmd_merge_pr` merges, confirms the
issue closed, and stops. The one field-*clearing* (not `Done`-setting) case is
`merge-lld-doc` clearing the Epic's own fields as its design phase completes — an epic
that stays open is a mid-pipeline reset, not a terminal state. A child paused by `pause-for-epic-regate`
clears only Pipeline Status, keeping Stage.
