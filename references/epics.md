# Epics: profiles, phase-Tasks, task sizing, and epic lifecycle

An Epic never runs a stage on its own issue. Its design runs on two child phase-Tasks
(Architecture-phase, then LLD-phase). Its implementation runs on the Tasks `lld` specifies.

## What makes an issue an Epic

Only `pipeline.classification` makes an issue an Epic: a configured Issue Type or label
(sample config: `type:epic`). Shape means nothing. A standing or legacy epic carries the
classification **and** its profile label. `next-action`, `list-parallel-ready` and
`list-design-ready` refuse a number that is neither an Epic nor an Initiative.

## Epic profiles

A profile is a label-matched set of toggles in `pipeline.profiles`. The first profile whose
`match` label is on the epic wins; `"*"` matches any epic. Never hardcode
`epic:standing`/`epic:legacy`: the config owns the label-to-profile map.

| Toggle | Default | Non-default effect |
|---|---|---|
| `driven` | `true` | `false` = **legacy**: skip the epic and every child |
| `epicLevelPhase` | `true` | `false` = **standing**: no phase-Tasks; each child runs its own flow into its integration base ("The epic integration branch") |
| `childrenNeedArchitectedEpic` | `true` | `false` = children enter the dev lane without `epic:architected` |
| `closes` | `true` | `false` = never closes, no integration branch |
| `gates.skipConfidenceThreshold` | `80` (global) | per-profile Gate B skip bar |
| `gates.requiresHumanGateA` / `gates.requiresHumanGateB` | `true` (global) | `false` = that gate is waived (`references/gates.md`, "Waived gates") |

Shipped profiles: `legacy` (`driven:false`); `standing` (`epicLevelPhase`,
`childrenNeedArchitectedEpic`, `closes` and both `requiresHumanGate*` set `false`); `default`
(`"*"`).

## How a non-standing Epic runs
```
Architecture-phase Task: architecture -> design PR -> arch-review -> merged into epic-<n> -> closed
LLD-phase Task:          lld -> design PR -> lld-review -> finish-lld (merge PR, create Tasks, close)
Task:                    development -> [pr-review] -> auto-merge into epic-<n> -> CLOSED
```

1. **Cut the phase-Tasks yourself** when `next-action` returns `cut-phase-tasks`:
   `cut-phase-tasks <epic-n> [--arch-body TEXT] [--lld-body TEXT] --repo-path <p>`
   (idempotent; LLD is blocked by Architecture).
2. **Every child branches from `origin/epic-<n>`**, phase-Tasks included. A phase-Task writes
   `<docRoot>/epic-<n>/architecture.md` or `lld.md` on its `issue-<n>` branch.
3. `transition` opens the **design PR** `issue-<n>` → `epic-<n>`, and the review runs on it. A
   `review_stale` review re-runs on the new head.
4. Merge and close: SKILL.md, "Cutting an Epic's phase-Tasks". Close a phase-Task only with
   `close-issue`, **never `mark-issue-closed`**. On `finish-lld`'s `failed_step`, fix the cause
   and re-run it whole.

`finish-lld` moves every carved Task to `development` and labels the Epic `epic:architected`.
Settle each `not_advanced` reason before you advance that child by hand. An Initiative-driven
Epic reads its Initiative's `product.md`; an engineering-driven Epic reads its body.

**A Stage-less child filed after `epic:architected`** shows in a `none` result's `unstaged`
list. Route it: `set-stage <n> --stage development` when `lld.md` covers it, else an
Architecture revision.

### Where a new issue starts (`default_stage`)

Stage options: `Product`, `Architecture`, `Development`, `Testing`, `PR Review`, `LLD`.

- An Initiative's child (Product-Roadmap Task) or a parentless issue starts at `product`.
- A standing epic's child gets no Stage: `next-action` returns `route` (SKILL.md, "Routing a
  standing child").
- A non-standing Epic's child gets no guess: it is staged explicitly, or it is `unstaged`.

## Which epics are exempt

- **Standing** (`epicLevelPhase: false`, label `epic:standing`): a permanent umbrella for
  technical (RTB) tasks. Apply the label by hand. Each child runs only the stages it needs
  (SKILL.md, "Routing a standing child"), with no human gate. Its design stages may fan out
  through `list-design-ready` when `parallelism.designLane` > 1 (`references/parallelism.md`,
  "Design lane"). A non-standing Epic never fans out design.
- **Legacy** (`driven: false`, label `epic:legacy`): never run. `next-action` checks it before
  crash recovery and returns `action: "skip"` for the epic and every child. An epic that needs
  any behaviour gets the standing or default profile instead.

### The 100-sub-issue cap

GitHub caps a parent at 100 sub-issues, **closed ones included**. At the cap `create-issue`
still creates the issue and sets its fields, but returns `ok: false`, `linked: false`,
`reason_code: sub_issue_cap` (`repair-issue --parent` reports the same). `next-action` never
drives an unlinked issue. Unlink closed sub-issues, or rotate a standing epic before it fills
(e.g. `RTB-2`). Then run `repair-issue <n> --parent <p>`.

## Doc layout at the epic level

- `<docRoot>/epic-<n>/architecture.md` and `lld.md` are the one path every command reads.
  Standing children and Product-Roadmap Tasks keep `issue-<n>/`. An Epic has no `product.md`.
  Altitude rules: `references/design-doc-rules.md`.
- `architecture.md` describes the design by component. It never creates, sizes or lists
  Tasks.
- `lld.md` has one `## Task <KEY>: <title>` section per Task, each with its own Footprint.
  `create-lld-tasks` renumbers it to `## Task #<n>: <title>`. A Task's `development` and
  `pr-review` read only their section, through `lld-section`.

## `lld` specifies the Tasks; `create-lld-tasks` creates them
- **Headings:** `## Task <KEY>: <title>`, `<KEY>` a lowercase slug. A section without a
  `Footprint` is skipped (`skipped_sections`); put prose under a non-`Task` heading.
- **`Depends on: <KEY>`**, only for real ordering. `create-lld-tasks` adds one `blockedBy`
  edge each. An unparsable dependency is refused (`dependency_parse_warnings`) and
  `finish-lld` stops: fix the line on `epic-<n>` and re-run.
- **`Realises: #<n>, #<m>`**, only when the Task delivers existing issues. They are blocked on
  the Task and closed when its PR merges or `close-issue` closes it. A number that is not a
  pre-existing issue goes to `realises_failed`; an already-closed one is listed in `realises`
  (`blocked: false`, `reason: "already closed"`).
- **Two standing Tasks per Epic, Integration-test and e2e-test**, run after the functional
  Tasks and run only the specs they add (`references/verification-rules.md`, "Test scope").
  None under `testTasks: false` ("Initiative branch").
- **`Priority:` / `Effort:`** are optional, else `pipeline.issueDefaults`.
- **`Placement: local — <reason>`** marks a Task the cloud cannot run (denied hosts such as
  Android SDK or Maven, a real phone, Stage). `create-lld-tasks` adds the local label
  (`references/cloud-mode.md`, "Placement rules").
- Ordering comes from Footprints and `blockedBy` edges, never prose. Use `mark-blocked` only
  for a cross-epic dependency.

### How to size the Tasks: one bounded concern each, not one unit of effort each
A Task is what `development` loads into one fresh context and `pr-review` judges as one PR.

- **One Task = one bounded concern, one shippable PR** (a module end to end, or a thin
  vertical slice), never a horizontal layer.
- **Aim for non-overlapping footprints.** Split on a footprint collision or concern boundary,
  never on effort. A large single concern stays whole with `Effort: High`. Never go below a
  shippable slice; a piece that needs another first is a `Depends on:`.
- **Footprint** (parsed by `list-parallel-ready`): each Task section (or a standing child's
  `architecture.md`) has a heading exactly `Footprint`, then one backticked path per `- `
  bullet. Use exact paths or directory-prefix globs, no mid-path wildcards. Paths under a
  `**Verify-only:**` label are read, not owned. A numeric prefix or trailing colon on the
  heading parses; `## Footprint overlap`, a backticked mention or a fenced example does not.
  A Task that owns nothing still schedules. A missing section excludes the Task from the
  parallel lane and is an `lld-review` finding, as is an overlap with another active Task.

  ```markdown
  ## Footprint

  - `backend/src/notifications/**`
  ```
- **Contract:** a shared contract no path shows (an API shape, a pinned count, a DB invariant)
  gets a `Contract` heading with `reads:` / `changes:` bullets. `lld-review`, and you before
  running two Tasks at once, treat a `changes:` that another in-flight Task `reads:` or
  `changes:` as a footprint overlap.
- An Epic's `architecture.md` carries no Footprint.

## Architecture deviation escalation
`architecture.md` is settled at Gate B. Every later unit first decides whether its work fits.
**Fits** → proceed. **Contradicts it** (new boundary, unplanned data-model change, a broken
premise) → revise:

1. `open-arch-revision <epic-n> --title "Architecture revision: <deviation>" --body "<the
   deviation, the unit that found it>" --blocks <affected-n> ... --repo-path <p>` (returns
   `revision_task`).
2. It runs `architecture` → design PR → `arch-review`, edits `architecture.md` in place, and
   updates the affected Tasks' `lld.md` sections in the same PR. Only a new Task needs a
   fresh `lld` pass.
3. Park the reporting unit: `pause-for-epic-regate <n> --epic <epic-n> [--gate-pr <pr>]
   --found-by <stage>`. When the revision closes, resume its agent directly. **Never
   `set-stage` or `claim` it**: `verify-exit` would refuse. `transition`'s review start
   re-claims it (`reclaimed: true`).
4. Valve: count the `lld`/`development` ↔ `architecture-revision` pairing on its own at `pipeline.escalation` (default 3 / 6). The third
   unsettled deviation brings the replacement architect; the sixth → `mark-needs-human` **on
   the Epic**.

### LLD correction — drift that does not change the design
A wrong `lld.md` statement while the design holds (a stale path, a false claim about code) is
a doc edit, not a deviation. Cut `open-arch-revision <epic-n> --title "LLD correction: <what>"
--body "<drift, section, finder>" --repo-path <p>` with no `--blocks`. Its `architecture`
agent edits only those `lld.md` sections and keeps every `## Task` heading. Do not park the
finder unless its work depends on the corrected text. A drift that changes an
`architecture.md` decision is a deviation.

All other rework: `references/rework.md`.

## The epic integration branch
- A non-standing Epic owns `epic-<n>`, created from `main` on first need. **Its Tasks branch
  from and merge into it.** At close it takes one merge from `origin/main` and merges to its
  base once.
- **Straight into `main`:** standing children, parentless issues, Product-Roadmap Tasks. A
  standing Epic under an initiative branch sends its children to `initiative-<i>`. `integration_base()` decides from the native
  parent, never a label or branch name.
- Design docs reach `epic-<n>` only through design PRs and `create-lld-tasks`' numbering
  commit. Never commit to it by hand or delete it while the Epic is open.
- `next-action --sync-epic` merges `main` into it at run start, every third merge and when
  `main` moved (`epic_sync`).
- When `pipeline.stack.enabled`, run `provision-epic-stack <n>` at first touch
  (`references/parallelism.md`, "Per-epic isolated stack").

## Initiative branch (opt-in)
An Initiative whose label matches a `pipeline.initiativeProfiles` entry with `branch: true`
owns `initiative-<i>`. Without it, Epics close into `main`.

- **Its Epics cut from, sync with and close into `initiative-<i>`** (`close-epic` result
  `base`). Each Epic closes on its own.
- `next-action <initiative> --sync-epic` keeps it current (`unit: initiative`). On `conflict`:
  `worktree-add <i> --unit initiative`, merge `origin/main`, resolve (delegate `development` for anything beyond a mechanical fix),
  push, call again.
- `close-epic` into it skips the profile's `deferSuites` (`deferred_to_initiative`); they run
  on the initiative PR. `testTasks: false` drops both standing test Tasks.
- **Into `main` once, after every cut Epic closed:** `references/initiatives.md`, "Closing an
  Initiative", step 0. `close-initiative` refuses until it lands, and while
  `origin/initiative-<i>` holds commits `main` lacks (`unlanded_commits`), even if the label
  was removed. Label removed mid-flight: restore the label, then `open-initiative-pr` /
  `merge-initiative-pr`, or land the branch. A later Epic closing into a re-created branch
  un-lands it (a new PR is owed).

## Epic closing
`pipeline.epicClose.auto` (default `false`): **off**, closing is the operator's call; **on**,
you run the closing verification and, if clean, make the second `close-epic` call.

`check-epics-closeable` (idempotent) posts a one-time checklist on each open epic whose
children are **all** closed, and assigns the operator. It checks children, open dependents,
and both docs on `epic-<n>` (`docs_missing_from_epic_branch`: merge the design PR that holds
the doc). An Epic with no phase-Tasks and no `epic:architected` goes to `needs_phase_tasks`:
`cut-phase-tasks` it, or report finished pre-phase-Task work to the operator.

**`close-epic <n>` is two calls. Every refusal is a structured exit-0 result.**

1. First call: refuses on `open_children`. When `epic-<n>` is behind `origin/main` it
   reconciles and stops (`reconciled`), unless everything `main` gained is in the
   carry-forward set and untouched by the epic (`reconcile_skipped`). Every result carries
   `unattested_suites` (run and attest), `awaiting_checks` (`attestable: false`: only the epic
   PR's check satisfies them), and `evidence` per kind (`fresh` | `carried_forward_from <sha>`
   | `children:#a,#b` | `passing_check` | `not_required` | `missing`, plus
   `carried_forward_files` and `evidence_breaks`).
2. Run the exploratory pass and record it. Run each `unattested_suites` suite whose workflow
   does not run on the epic PR with a fresh `sdlc:development` agent and a run-only brief.
   Every other suite waits for the epic PR's check (`references/verification-rules.md`,
   "Test scope").
3. Second call: refuses on `missing_verification` or a `missing` suite (`checks:
   missing-checks`); otherwise merges `epic-<n>` into its base. The epic PR exists from this
   call: attest each named suite on it (`record-local-ci --pr <pr>`), wait for
   `awaiting_checks`, and call again. After the merge it closes the Epic and cleans up
   (`cleanup`, `children_cleanup`, `run_state`, `stack`; `recovered: true` on an
   already-merged epic).

**Evidence carry-forward:** the exploratory record survives a delta that touches only
`pipeline.epicClose.evidenceCarryForward.paths` (default `**/*.md`, `docs/**`, `<docRoot>/**`)
and is under 300 files. Never widen the set to source or tests. A suite attestation carries
forward when nothing its workflow covers changed since its sha. A suite with no epic-PR
attestation is satisfied when every merged child PR that touched its paths was attested or
checked at its merged head and no direct `epic-<n>` commit touched them; else
`evidence_breaks` names the break. `--no-carry-forward` accepts only evidence at the epic
head. Stale evidence returns `stale_delta` (`{<kind>: {files, count}}`): brief a delta re-pass
scoped to those files.

**Closing verification, the exploratory pass** (`sdlc:exploratory`):

- Preflight the machine first (`references/parallelism.md`, "Resource cap — serialize
  suite-heavy stages").
- Run it in the epic worktree (`worktree-add <n> --unit epic`) and name `close-epic`'s `base`
  in the brief. `close-epic` runs `pipeline.epicClose.prepare` there first (`ok: false` → fix
  it). Unset `prepare` returns `prepare_warning`: run the dependency install yourself. A pass
  `blocked` on missing dependencies: install, then resume it.
- It also runs `lld.md`'s `## Epic-close checks`. **The pipeline never runs the full e2e
  suite**; `record-epic-verification --kind e2e` never gates.
- **You** record it: `record-epic-verification <n> --kind exploratory --summary "..."
  --repo-path <the epic worktree>` (`--sha` for an earlier tested head). **Never record a
  verification you did not run clean.**

**With `epicClose.auto` on, escalate instead of closing** when a Blocker/Critical delta or a
manual-testing bug was filed, or the exploratory pass could not be obtained (report the epic
close-ready pending it; never fabricate the record).

**The e2e-test Task's specs run against a stack built from `epic-<n>`**, never the shared dev
stack, and attest with `record-local-ci --suite e2e`. On a cloud Epic with no cloud e2e
profile it is a local Task (`references/cloud-mode.md`, "A cloud run").

**Triage new-spec and exploratory deltas:**

- **Blocker / Critical** (a re-confirmed failure on a surface this epic moved, or **any**
  access-boundary failure, 5xx, crash or data-integrity problem): a `Bug` child of **the
  epic**, `unstaged`, blocking close. Route it by hand.
- **Normal / Low** (unmapped surface, flake, unchanged pre-existing failures): file it on the
  standing backlog epic.
- **Architecture drift with no defect:** never blocks close. Correct the doc or file it on the
  backlog epic.

**Close-blocker lane, only with operator authorisation.** A Blocker/Critical fix that fits the
design may skip `lld`: `set-stage <n> --stage development`. A design gap goes through
`open-arch-revision`; never ask `development` to edit an open Epic's docs.

**A closed Epic's design docs are its as-built record, and a correction to them routes
through `development`.** `open-dev-pr` accepts that diff. An open Epic's docs change only
through its phase-Tasks or a revision.

**Pass `--clean-worktree` on the `close-epic` call after the closing run.** It discards
untracked output before a reconcile or the merge, and refuses on a tracked change
(`worktree_clean.tracked_changes`).

**Every manual-testing finding is its own `Bug` child:** `file-closing-delta <epic> --title ..
--body .. [--priority P] [--effort E] [--start]` (`--start` = close-blocker lane). Closing waits
for it.

**Standing epics never get this check.**

## Epic board Status

The pipeline neither writes nor reads a Projects-v2 board `Status`. Pipeline Status is the only
status it maintains.

### Pipeline Status `Done`

- A pipeline close (`merge-pr`, `close-issue`, `close-epic`, `close-initiative`, a realised
  issue) clears Stage and sets Pipeline Status `Done`. A human close leaves them; every reader
  filters to open issues.
- `merge-lld-doc` clears the Epic's own fields (the Epic stays open). `pause-for-epic-regate`
  resets only Pipeline Status, to `Todo`.
