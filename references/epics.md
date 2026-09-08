# Epic-level stages, exemptions, child sizing, and epic lifecycle

Referenced from `SKILL.md`. Product and Architecture run once per epic, not once per
issue — this file owns everything epic-shaped: the epic-level phase, which epics are
exempt, how the epic architect creates/sizes children, the deviation-escalation path,
the standing-epic bug fast-track, and epic closing/board status.

## Epic profiles

An epic's behaviour is set by its **profile** — a label-matched bundle of toggles in
`pipeline.profiles` (config; see `references/operations.md`). `resolve_profile(epic)`
walks the ordered list and returns the first profile whose `match` label is on the epic,
falling back to the `"*"` catch-all. The skill no longer hardcodes the
`epic:standing`/`epic:legacy` labels; a client owns the label→behaviour mapping and can
match `epic:standing`, `RTB`, or anything it likes. The toggles:

| Toggle | Default | Effect when non-default |
|---|---|---|
| `driven` | `true` | `false` = **legacy**: pipeline skips the epic and every child |
| `epicLevelPhase` | `true` | `false` = **standing**: no epic-level product/architecture; each child runs its own full flow |
| `childEntryStage` | `"lld"` | `"product"` = children enter at `product` (full flow) instead of `lld` |
| `childrenNeedArchitectedEpic` | `true` | `false` = children eligible without the epic being `epic:architected` |
| `closes` | `true` | `false` = epic never closes and has no integration branch |
| `gates.skipConfidenceThreshold` | `95` (global) | per-profile Gate B skip bar |
| `gates.requiresHumanGateA` | `true` (global) | `false` = Gate A auto-passed (no human) |

The three shipped default profiles reproduce the historical behaviour exactly:
`legacy` (`driven:false`), `standing` (the four `false`/`product` toggles above), and
`default` (`"*"`, all defaults). `is_epic_standing()` / `is_epic_legacy()` are now thin
reads of `epicLevelPhase` / `driven`. **`product-review` runs after `product` in every
profile** — it is not a per-profile toggle.

## What runs at the epic level, and what doesn't

A **default-profile epic** — any open, top-level `Type: Feature` issue whose profile has
`epicLevelPhase: true` (matches no `standing`/`legacy` profile) — runs its own Product
and Architecture phase, on the epic issue itself, before any of its children are touched:

```
epic: stage:product -> [Gate A, epic-scoped] -> stage:architecture -> [arch-review] -> [Gate B, epic-scoped] -> epic:architected
```

This is exactly the same product/architecture machinery as the per-issue flow — same
subagent roles, doc-altitude rules, gate mechanics (with `--unit epic`) — run once,
against the epic, instead of once per child. Running it with full visibility into
every child is what catches cross-child scope overlap (the #99/#107 incident — two
children independently scoping the same backend change).

Once an epic is `epic:architected`, each child enters the pipeline **at `lld`** — a
lighter per-task design pass working from the epic's approved `architecture.md`:

```
child issue: stage:lld -> [lld-review, automated, mandatory, no human gate] -> stage:development -> stage:testing -> [pr-review] -> CLOSED
```

`lld` has its own dedicated Stage value (`LLD`), its own doc (`lld.md`), its own
lighter prompt — and **it never opens a human-review gate**: the epic's Gate B already
covered the structural design call. Because there's no human backstop,
**`lld-review` is mandatory every time**, never confidence-skipped.

**A normal epic's children are never eligible before the epic is `epic:architected`.**
`next-action` and `list-parallel-ready` both enforce this mechanically — including
when the epic's own phase is stuck (gate open, needs-human, blocked): the whole epic
parks, not just the epic-self unit. There is no `architecture.md` for a child's `lld`
to work from until the epic-level gate has passed or been skipped.

### LLD is its own Stage value

The native Stage field has six options: `Product` / `Architecture` / `Development` /
`Testing` / `PR Review` / `LLD`. `default_stage()` returns `lld` for any child of a
normal, architected epic; every command treats it as an ordinary distinct value. (It
previously overloaded `Architecture`, relying on prose to tell a normal-epic child's
light pass apart from a standing child's full architecture stage.)

## Which epics are exempt

Both cases are now **profiles** (above), not hardcoded labels:

- **A standing profile** (`epicLevelPhase: false`, shipped matching `epic:standing`) — a
  permanent bug-intake umbrella with no fixed scope to batch-architect; its children run
  the full per-issue `product` → `product-review` → `architecture` flow (bug fast-track
  included). Resolved via `resolve_profile`; `is_epic_standing()` reads it.
- **A legacy profile** (`driven: false`, shipped matching `epic:legacy`) — **not run by
  this pipeline at all, in any flow.** `decide_next_action` checks it first — before
  crash-recovery — and returns `action: "skip"` for the epic and every child. Applied by
  hand to an epic the operator has decided is out of scope. Wanting a *behaviour* for a
  legacy epic is a sign it shouldn't match the legacy profile — give it the standing
  profile's label or leave it on the default.

## Doc layout at the epic level

`docs/sdlc/epic-<n>/product.md` and `.../architecture.md` — same filenames,
same "Document altitude" rules and templates as per-issue docs (see
`references/stage-playbooks.md`), and the two are structured differently from each
other at the epic level:

- **`product.md` is organised by functional area, never by child issue.** At the time
  it is written the children usually do not exist — `architecture` creates them — and
  a requirement is the same requirement whoever ends up implementing it.
- **`architecture.md` gives each child its own labeled subsection** (design notes,
  acceptance criteria carried forward, per-child decisions, under a heading naming that
  child, e.g. `### #99 — Seller notification preferences UI`) — a gate reviewer or `lld`
  agent working one child jumps straight to its subsection.

The epic's branch is `epic-<n>` (not `issue-<n>`) — see `references/parallelism.md`,
"Working on a branch". Children still branch as `issue-<n>`.

## Epic architecture creates/splits/modifies child issues

The epic-level architect has visibility into every child while writing
`architecture.md`, so it is expected — not exceptional — for it to:

- **Modify a child's body/scope** when the epic-level design reveals the child's ask
  needs adjusting.
- **Split or merge children** — `sdlc_next.py create-issue --parent <epic>` for a
  split; close a redundant child and fold its scope into a sibling for a merge.
- **Set each child's native "Effort" field** in this pass — the epic architect has the
  full picture to estimate every child at once. There is **no `sdlc_next.py` command
  for this**: `Effort` is set by hand, and an issue filed mid-epic by a review stage
  (rather than by the epic architect) therefore carries **no Effort at all**, by
  design. That is fine — nothing in the lane reads Effort. `next-action`,
  `list-parallel-ready` and every gate ignore it; it is a human-facing estimate. Don't
  burn calls trying to set it programmatically.
- **Always create a dedicated e2e-test child task** (`Type: Task`): one child whose
  sole job is Playwright e2e coverage for what the epic ships — never leave e2e
  coverage as an implicit side-effect of functional children. Title it plainly (e.g.
  "e2e coverage: <epic feature>"); body names the user-facing flow(s). It runs the
  normal `lld` → ... → `pr-review` pipeline.
- **State the execution order for a human reader** (optional but encouraged) near the
  footprint content. `list-parallel-ready` does not parse it — it derives ordering
  from `blockedBy` plus per-child footprints — but it helps anyone reading
  `architecture.md` by eye. **What each child's `lld.md`/`architecture.md` genuinely
  must carry is its own `## Footprint` list** (below), plus real `blockedBy` edges for
  genuine ordering constraints.

None of this needs `mark-blocked` or an escalation path — it's the epic architect
doing its job with a wider view. Only a genuine *cross-epic* dependency uses
`mark-blocked`.

### How to size the children: one component each, not one unit of effort each

`Effort: High` on its own is **not** a reason to split a child, and "split anything
big" is not the instinct. The bias is the opposite:

- **Prefer fewer, larger child tasks, each owning one component/module/directory
  boundary end-to-end.** One child touching one component deeply beats three children
  each touching a slice of the same files.
- **The goal is non-overlapping file footprints, not smaller tasks.** Two siblings
  should be workable without touching the same files — that's what makes them safe to
  parallelize; splitting on size alone produces several small tasks all editing the
  same module, the worst possible shape.
- **The reason to split is footprint collision, not effort.** Work that would
  inevitably interleave with a sibling's files is a real split (or merge). Large but
  self-contained in one component: leave whole, set `Effort: High` honestly.
- **Checkable requirement — the `## Footprint` section.** Each child's design doc
  (`lld.md` for a normal-epic child, `architecture.md` for a standing-epic child)
  **must** carry a `## Footprint` heading near the top, followed by a plain bullet
  list of the directories/modules it expects to touch, **each path backtick-wrapped,
  one per bullet** — this exact shape, because `list-parallel-ready` parses it
  mechanically (`parse_footprint` in `sdlc_next.py`):

  ```markdown
  ## Footprint

  - `backend/src/notifications/**`
  - `frontend/app/(admin)/sellers/**`
  ```

  A numeric section prefix is tolerated — `architecture.template.md` numbers its
  sections, so `## 12. Footprint` parses identically to a bare `## Footprint`. Nothing
  else about the shape is negotiable.

  **An epic-level `architecture.md` carries no Footprint section at all.** Nothing ever
  parses one: `read_footprint` resolves `origin/issue-<n>` only, never the epic branch.
  The footprint that matters is each child's, in its own `lld.md` (or, for a
  standing-epic child, in that child's `architecture.md` — the one case where the
  architecture doc's Footprint is read).

  Exact file paths and directory-prefix globs only; no mid-path wildcards. A
  non-parseable or missing footprint excludes the child from the parallel lane
  ("cannot verify non-overlap", never "no footprint, no risk") — and is an
  `lld-review` finding in its own right. `lld-review` also flags a child whose stated
  footprint overlaps a sibling's (including the currently-active siblings a
  bootstrapped fresh child couldn't be checked against before its `lld.md` existed —
  see `references/parallelism.md`, "Bootstrap rule").

## Epic-level deviation escalation (bugs, and any `lld` finding a design gap)

A bug against a normal, architected epic starts at `lld` like any other child (no
fast-track). `lld`'s first move is deciding whether the fix fits the epic's existing
`architecture.md`:

- **Fits** (the overwhelming common case) → proceed straight through `lld` →
  `lld-review` → `development` → ... — no escalation. True for bugs and features
  alike; "deviation" is about the *design*, not issue type.
- **Contradicts the design** (new component boundary, unanticipated data-model
  change, a permission model the epic's design assumed doesn't hold) → stop and
  escalate to the **epic's own architecture stage**:
  1. Park this child: `sdlc_next.py pause-for-epic-regate <child> --epic <epic>
     --gate-pr <n>` (call it *after* step 2 opens the new gate PR, since it needs the
     number) — clears the child's Pipeline Status only (Stage stays `lld`, so it
     re-enters normal eligibility once the re-gate merges) and posts a linking
     comment.
  2. Resume the epic's architecture agent (or spawn fresh if the session ended) with
     the specific deviation. It revises the relevant subsection of the epic's
     `architecture.md` on a fresh `epic-<n>-gate-architecture` sub-branch (re-cut
     from the current `origin/epic-<n>`), commits, pushes, and — since Gate B already
     passed once — opens a **second Gate B round**: `sdlc_next.py open-gate <epic>
     --title "..." --doc architecture.md --next-stage development --unit epic
     --summary "..."`.
  3. Once that gate merges, `pass-gate`/`skip-gate --unit epic` re-marks
     `epic:architected` (idempotent), and the paused child becomes pickable again.
  4. Escalation valve: track as its own pairing (`lld` <-> `epic-architecture`); a
     third deviation against the same epic's design without settling swaps in the
     context-reset replacement architect for rounds 4–6, and a sixth means
     `mark-needs-human` **on the epic** — the problem is epic-level.

This is deliberately the *only* extra escalation path this model needs — every other
rework/blocker case follows `references/stage-playbooks.md`, "Rework and blockers".

## Bug fast-track — architecture first (standing-epic children only)

An issue whose Type is `Bug`, filed against a **standing** epic, skips `product` at
filing time and starts directly at `stage:architecture`, on a freshly created
`issue-<n>` branch (architecture creates the branch for these).

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

A normal epic owns a long-lived branch, `epic-<n>`, cut from `origin/main` when the
epic starts. **Everything the epic produces branches from it and merges into it**, not
into `main`: its own gate docs (on `epic-<n>-gate-<stage>` sub-branches) and each
child's `issue-<n>`. The branch takes one merge *from* `origin/main` at close, is
verified as a whole, and merges to `main` once.

The point is where conflicts surface. Under trunk-based children, every child
integrates against a `main` that moves under it, and two siblings can each be green
independently yet break `main` together — a semantic conflict no textual merge check
catches. Deferring integration to the epic branch means that collision surfaces once,
against a tree where every sibling is already present, and is resolved before anything
reaches `main`.

Two cases still integrate straight into `main`, and neither is a compatibility hedge:

- **A standing epic's children** (`epic:standing`, e.g. the standing backlog epic). A standing epic never
  closes, so its integration branch would never merge and would diverge without bound.
- **A top-level issue with no parent epic.** There is nothing to integrate into.

`integration_base()` decides this mechanically, from the native `parent` relationship —
never from a label or a naming convention. It reads `issue_list`'s GraphQL, because
`gh issue view --json` has no `parent` field at all; the first cut read it from
`issue_view` and every child silently resolved to `main`.

**Gate PRs follow the same shape** (decided 2026-09-06 — `references/history.md`).
The epic's `product.md`/`architecture.md` are authored on a sub-branch
`epic-<n>-gate-<stage>` cut from `origin/epic-<n>`; `open-gate --unit epic` opens it
against `epic-<n>`; the human merges it (squash is fine — the sub-branch is
disposable) and the doc lands on the epic branch; `pass-gate --unit epic` fast-forwards
the epic worktree to `origin/epic-<n>`. Nothing about an epic gate touches `main`.
Until 2026-09-06 epic gates went `epic-<n>` → `main` unsquashed, specifically so the
docs were on `main` — reachable by name, not by a SHA quoted from an old comment —
before any child forced the epic branch there. That guarantee has moved one branch
over: for a still-open epic the docs are authoritative and reachable by name on
`origin/epic-<n>`, which is what `check-epics-closeable` now verifies; `close-epic`'s
final merge is what carries them to `main`, together with everything else the epic
produced. A standing epic's children still gate `issue-<n>` → `main`, unsquashed.

## Epic closing

The pipeline never closes an epic — a milestone-level call only the human makes. It
tells you the moment an epic is ready, exactly once:

```bash
python3 "$SDLC" check-epics-closeable
```

For every open epic whose children are **all** closed, posts a one-time checklist
comment and assigns the operator (idempotent — detects its own prior marker):

1. All child issues closed *(auto-verified)*
2. No child closed as won't-fix in a way that silently shrinks delivered scope
3. No open issue elsewhere depends on a closed child *(auto-verified via the native
   `blocking` relationship)*
4. The epic's own `product.md` and `architecture.md` are both on `epic-<n>`
   *(auto-verified — `docs_missing_from_epic_branch` in the result)* — a doc left
   on an unmerged `epic-<n>-gate-<stage>` sub-branch is reachable only by SHA, and a
   SHA quoted from an old comment resolves to whatever draft it pointed at (see
   `references/history.md`, 2026-08-20); it would also never reach `main`, since
   `close-epic`'s merge of `epic-<n>` is what lands the docs there
5. Architecture docs the epic touched reflect final state
6. Delivered scope matches the epic's stated purpose
7. Manual testing done — a human end-to-end pass outside the pipeline's coverage

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
verification are recorded on the thread.

**The closing verification is two runs in parallel, both by the pipeline:**

- the **full e2e suite**, and
- an **exploratory pass** (`sdlc-exploratory` agent) that goes looking for what a
  scripted suite cannot — the judgment half of what used to be "manual testing done by
  a human".

Each records durable evidence via `record-epic-verification <n> --kind e2e|exploratory`.
`missing_epic_verification()` requires both, **and requires each to postdate the last
`origin/main` reconcile** — evidence gathered before the final merge describes a
different tree than the one that ships. Same ordering trap `missing_pipeline_evidence`
guards one altitude down.

**The full e2e suite runs once, at epic close** (operator policy, 2026-08-22) — owned
by the epic's dedicated e2e child, which should therefore be sequenced after the
children whose surfaces it proves. Per-child `testing` runs only the specs covering the
surfaces that child moved; a full suite per child is minutes of Docker for evidence the
scoped run already gives. Triage the closing run's deltas by a stated rule, not case by
case:

- **Blocker / Critical** — a stable, re-confirmed delta on a spec attributable to a
  surface this epic moved, plus unconditionally any access-boundary delta or any
  5xx/crash → **filed against the epic itself**, and it blocks epic close.
- **Normal / Low** — a delta on an unmapped surface, a non-reproducible flake, or a
  pre-existing failure cluster whose membership is unchanged → **filed against the
  standing backlog epic** for human triage.

A "before" and an "after" that are each a single run of a zero-retry suite is not a
comparison — pin the confirmation procedure (retries, workers, and what counts as a
stable delta) in the e2e child's own design doc.

**Any issue caught by manual testing (item 6) gets filed as its own `Bug` child of the
epic** — never folded silently into the closing comment. Use `create-issue --parent
<epic>` (sets `Type: Task`; follow with `gh.set_issue_type()` if it must be `Bug` —
which only changes behavior for a standing-epic child; a normal epic's bug enters at
`lld` regardless of Type). If the epic was about to close, closing pauses until the
new bug child resolves.

**Standing epics never get this check** — momentarily empty is not done; more bugs
will land. Apply the label by hand to any epic meant to work that way; nothing
auto-detects it.

## Epic board Status

The org board (the Projects-v2 project named in config as `projectNumber`) carries a native Projects-v2 `Status`
field (`Todo`/`In Progress`/`Done`) — distinct from the load-bearing `Pipeline Status`
Issue Field. Board `Status` is human-facing convenience only: nothing reads it back,
and every write is best-effort (`set_project_status` swallows failures — must never
block a claim or workflow run).

- **A normal epic's Product/Architecture phase starting or resuming** flips board
  `Status` to `In Progress` — wired into `cmd_claim`
  (`_maybe_mark_epic_in_progress`), crash-recovery re-claims included. Excluded for a
  standing epic (never claims an epic-level stage; its board Status is tracked by
  hand).
- **Closing an epic** flips it to `Done` — via the `mark-issue-closed` Action job (the
  pipeline never closes epics), for **every** epic regardless of standing/legacy.
- **No per-child board `Status` writes exist or are planned.**

### Pipeline Status `Done`

**Any issue closing** — `merge-pr`'s squash-merge auto-closing via `Closes #<n>`, or a
human closing directly — clears Stage (meaningless once closed) and sets Pipeline
Status to `Done`. This lives **entirely in `gate-auto-advance.yml`'s
`mark-issue-closed` job** (`issues: closed`), not in `cmd_merge_pr` — so a manual
close gets the same cleanup as a pipeline merge. `cmd_merge_pr` merges, confirms the
issue closed, and stops. The one field-*clearing* (not `Done`-setting) case remains
`_complete_epic_architecture` — an epic finishing its own phase while staying open is
a mid-pipeline reset, not a terminal state. A child paused by `pause-for-epic-regate`
clears only Pipeline Status, keeping Stage.
