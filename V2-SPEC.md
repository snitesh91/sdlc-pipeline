# sdlc-pipeline V2 — design spec (discussion phase, nothing implemented yet)

This is a planning document, not a rulebook — the skill's normal "one rule, one home"
discipline (`references/history.md`) doesn't apply here yet because nothing here is a
rule; it's a record of what's been decided, what's open, and why, so a future session
(mine or the operator's) doesn't have to reconstruct this from chat history. Lives only
on the `v2` branch until it's real.

## Why V2

Two independent motivations, discovered in the same conversation but not the same problem:

1. **`sdlc-product` today is pure process, no product-management substance.** It enforces
   document shape, sizing, and when-to-stop — nothing about market landscape, competitors,
   product vision, or prioritization against a strategy. It cannot do any of that even if
   instructed to: its frontmatter tool list (`Read, Grep, Glob, Bash, Write, Edit`) has no
   `WebSearch`/`WebFetch`.
2. **The skill is hard-wired to GitHub** — issue tracking, project fields, PR mechanics,
   and doc storage (git-committed) are all GitHub-specific, or at least conflated as if
   they were one thing. This is a personal open-source project, not tied to any one
   org's tooling, and should support Jira as a work-tracker and Confluence as a doc store
   without a rewrite each time.

Both are real, both are large, and they're orthogonal — hence one `v2` branch, but treat
them as two work-streams that can land independently.

## Non-goals for this pass (explicitly deferred, not forgotten)

- A working `CodeHostProvider` abstraction (GitLab/Bitbucket implementations). Code
  hosting stays hardcoded to github.com. Only the *seam* needs to exist, not a second
  implementation.
- A working Confluence doc-store implementation. Docs stay git-committed under
  `<docRoot>`. Only the seam needs to exist.
- A working Jira work-item implementation. GitHub Issues stays the only wired
  implementation. **The seam itself is now built** (see "Implemented," below) — what's
  deferred is a second class satisfying it, not the interface.
- The "Initiative" hierarchy tier (see below) — real, wanted, but has its own
  prerequisites and cascading changes independent of everything else here.

---

## Work-stream A: provider abstraction

### Implemented (2026-09-14) — the interface, GitHub as the only implementation

Per operator: build the pluggable design for **issue management, not code
management** — code hosting stays git-protocol/github.com-only per the non-goals
above; only the Work-Item Provider seam needed building now. Done, in
`scripts/sdlc_next.py`:

- **`WorkItemProvider`** — a `typing.Protocol` (structural, not an ABC — `GitHub`
  satisfies it by already having the right methods, no inheritance change needed)
  listing the full issue-management contract every `cmd_*` function is written
  against: list/view/edit/comment/close/create an issue, native-field get/set,
  hierarchy links (`blocked_by`, `add_sub_issue`), and `classify_unit`.
  Deliberately excludes `pr_*`/`branch_*_by`/`path_on_ref`/`graphql` — those are
  Code-Host/transport concerns, out of scope by the same decision as above.
- **`GitHub.classify_unit(number) -> "initiative"|"epic"|"task"|"other"`** — new
  method, config-driven via `pipeline.classification` (per-kind `{"field":
  "issueType"|"label", "value": "..."}` rules). Empty by default; returns `"other"`
  rather than guessing when nothing is configured or nothing matches — a repo that
  hasn't provisioned Initiative/Epic/Task classification yet gets an honest
  "don't know," not a silent misroute. Deliberately independent of the existing
  `is_epic()`/`resolve_profile()` V1 heuristic (Feature-type-plus-no-parent) — V2 is
  a different lifecycle, not an extension, and entangling the two risked regressing
  V1's still-live profile matching.
- **`get_work_item_provider()`** — the one place a provider gets instantiated; every
  CLI command now calls this instead of bare `GitHub()` (37 call sites replaced,
  zero behavior change — verified by the full existing suite staying green
  unmodified). Reads `pipeline.workItemProvider.type`, default `"github"`. Naming
  anything else is a clear refusal (`GhError`), never a silent fallback to GitHub.
- 8 new regression tests (factory default/override/refusal, Protocol conformance,
  `classify_unit` positive/negative controls for both `issueType` and `label` rules),
  verified red against pre-fix `sdlc_next.py`.

**What this does not do yet**: no second class implements `WorkItemProvider`. Jira
stays exactly as deferred as before — the difference is there's now a real,
tested contract for it to satisfy, not just a described one.

### The landscape has (at least) three seams, not one

GitHub today does double duty as both the work-item tracker and the code host, which is
why it reads as "one GitHub dependency" when it's really two:

- **Work-Item Provider** — issue/ticket CRUD, comments, hierarchy (parent/child), fields,
  transitions, search/list. Candidates: GitHub Issues (today, via `gh` CLI — already an
  existing tool, not something this skill wrote), Jira.
- **Code-Host Provider** — worktree/branch management (this part is *already*
  host-agnostic, it's just git), PR/MR create+merge, required-check/CI verification.
  **Scoped down per operator decision: git-protocol-level operations are the only part
  abstracted now; PR/merge/CI-check mechanics stay hardcoded to github.com.** If a real
  second code host is ever wanted, revisit then — not blocking anything now.
- **Doc Store** — where `product.md`/`architecture.md`/`lld.md` physically live. Today:
  committed to the driven repo's own git tree, which is already host-agnostic (works
  identically on GitHub/GitLab/Bitbucket). Wanted eventually: Confluence. Deferred.

### Decision: reuse existing tools/skills, don't write new API clients

Operator directive: **do not write a bespoke Jira REST client or a bespoke GitHub API
client** — leverage what already exists, and since this is a personal open-source
project, everything reused must be generic/self-hostable, **not tied to any specific
org's tenant or SSO** (ruled out anything Adobe-specific found in this environment's
existing skills).

Candidates found (2026-09-14 web research, not yet vetted or chosen):

- **GitHub work-items**: the `gh` CLI (already what `sdlc_next.py` uses — no change
  needed) or GitHub's official `github-mcp-server`
  ([github.com/github/github-mcp-server](https://github.com/github/github-mcp-server)) —
  official, generic, no tenant lock-in, remote-hosted or local Docker image.
- **Jira (and later Confluence) work-items**: two generic, self-hostable, non-Adobe
  candidates —
  - [`atlassian/atlassian-mcp-server`](https://github.com/atlassian/atlassian-mcp-server)
    — Atlassian's own **official** MCP server, covers Jira, Confluence, Jira Service
    Management, Bitbucket, and Compass in one server, OAuth 2.1 or API-token auth. Covers
    both the Jira work-item seam *and* the Confluence doc-store seam with one
    integration, which is worth weighing given both are wanted eventually.
  - [`sooperset/mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) — popular
    community open-source alternative, supports both Atlassian Cloud and
    Server/Data Center (self-hosted Jira), API-token based.
  - Neither has been evaluated against this skill's actual needs yet (hierarchy
    modeling, comment-marker support — see below). That's real work before either is
    "the" answer, not a rubber stamp.

### The trickiest thing to abstract: the comment-marker state mechanism

The rework/escalation valve (`pairing-counts`, `same-class:true`, `arch-review-confidence`,
`stage-transition`) works by embedding hidden HTML comments in GitHub issue comment
bodies and regex-parsing them back — free and invisible on GitHub's markdown renderer.
Jira comments are Atlassian Document Format, not raw HTML; the same trick likely doesn't
port byte-for-byte.

**Decision: keep the state-in-comment-text approach** (operator confirmed) — each
provider owns its own embed/parse convention for the same semantic markers, rather than
moving this state onto native provider fields. Keeps the core orchestration's read path
(`pairing-counts` et al.) provider-agnostic in shape even though the wire format differs
per provider.

### Proposed layering

```
Layer 1 — Core orchestration engine (fully provider-agnostic; already mostly is)
  Stage state machine, rework/escalation valve semantics, retro process,
  doc-altitude content rules, agent definitions' persona/procedure prose.
  Today's coupling to GitHub here is narrow: mostly the "Exit actions" sections in
  each agent file, which call sdlc_next.py commands that are GitHub-specific under
  the hood.

Layer 2 — Provider interfaces (contracts, defined once, GitHub is the reference impl)
  WorkItemProvider: create/read/comment/transition/link-hierarchy/search
  (CodeHostProvider and DocStore interfaces: seam only, no second implementation yet)

  **`classify_unit` is part of this contract, not a Layer-1 assumption.** Whether a
  given issue *is* an Initiative, an Epic, or a Task is answered differently per
  provider — GitHub might use a real Issue Type, or a label, depending on what the
  client's plan/org supports; Jira has its own native Initiative/Epic/Story hierarchy
  levels that may map directly. **The classification rule itself is client-configured,
  not hardcoded per provider** — two GitHub-backed clients could use two different
  signals (Issue Type vs. label) for the same concept. Core orchestration (Layer 1)
  only ever calls `classify_unit(issue) -> initiative | epic | task | other` and never
  inspects a provider-specific field directly to make that call.

Layer 3 — Concrete sub-skills (pluggable, GitHub wired, others deferred)
  github-work-items (today's `GitHub` class / gh CLI, ~as-is)
  jira-work-items (deferred — candidate: one of the two MCP servers above)
  confluence-doc-store (deferred — candidate: atlassian-mcp-server, same server as Jira
  if that's the chosen path, which is an argument for picking it over the two-server
  split)
```

### Open questions, work-stream A

1. Which Jira/Confluence integration gets chosen — `atlassian-mcp-server` (one server,
   both Jira+Confluence, official) vs `sooperset/mcp-atlassian` (community, explicit
   self-hosted Server/Data Center support)? Depends partly on whether self-hosted Jira
   (not just Cloud) needs to be supported.
2. Config shape for a provider-agnostic install — today's `sdlc-pipeline.config.json`
   `projectFields` are GitHub-Projects-v2 GraphQL schema ids specifically. A
   provider-agnostic config needs a `providers.<name>.*` section per backend instead of
   one flat GitHub-shaped block.
3. Hierarchy semantics differ by provider — GitHub's native sub-issues vs Jira's
   Epic/Story/Sub-task (and Jira Advanced Roadmaps' own Initiative concept, which may
   overlap with work-stream B's Initiative idea below — worth designing together, not
   independently, if Jira is a near-term target).

---

## Work-stream B: V2 product agent (PM substance)

### Tool grant

Add `WebSearch`, `WebFetch` to `sdlc-product.md`'s frontmatter. Nothing works without
this; today it has neither.

### New workflow (supersedes V1's pure-process flow, keeps every V1 rule that's still correct)

0. **Scope alignment** (existing, unchanged) — orchestrator's pre-`product` interview.
1. **Vision check.** Reads a standing product-vision/strategy doc. **Not gathered
   per-epic** — this is a *skill installation-time* deliverable (see below), so the
   agent's job here is just "read it," not "ask for it."
2. **Desk research**, scaled to the work (skip for trivial/all-six-NO work per the
   architecture-depth assessment — see open question below on whether that assessment
   stays in `product` at all). Starts from a curated competitor list (also
   installation-time, see below) but isn't capped by it — free to research further if
   the curated set doesn't cover the epic's actual feature area.
3. **Draft the IRD** — V1's five authoring rules unchanged (requirements-not-tech, no
   pipeline leakage, no hedging, no collapsed detail, say-each-fact-once), now informed
   by real market/competitive context instead of pure inference from the issue text.
   Findings fold into the requirement language itself — no separate "research report"
   section, no source citations in the doc (operator: "we should only be concerned about
   its findings and decisions," not showing its work).
4. **Low-fidelity mockup**, promoted from incidental to a deliberate step whenever User
   Experience describes a new or materially-changed screen/flow — structural only
   (boxes/labels), no styling. Skipped entirely for backend-only work.
5. **Architecture-depth assessment** — **recommended to move to `sdlc-architecture.md`**
   (still open, see below), since all six questions are engineering judgment calls about
   the codebase, and `architecture` is the agent that actually reads the codebase deeply
   as its first move.
6. **Priority/vision-alignment statement.** Checks the epic's priority against **both**
   the vision doc **and** the pending backlog (operator: not vision in isolation).
   Handoff-only, same as today — process reasoning doesn't belong in the document itself.
7. **Sizing/decomposition, exit actions** (existing, unchanged).

### Skill-installation-time additions (not per-epic, not in the product agent itself)

- **Product-vision doc gathering** — a one-time setup step (new logic needed in the
  install/config flow, `README.md`'s "Installing the shipped pieces" or a dedicated
  script) that produces a vision doc via an interview with the operator. **Confirmed
  found for bookshaw: `bookshaw-docs/requirements/IRD-001-platform-vision.md`** — this
  already exists, was not created by this V2 effort, and hasn't been read/reviewed yet
  for whether its shape is what the V2 agent should assume. The install-time step this
  spec describes still needs building generically (for a repo that doesn't already have
  one); bookshaw itself is a case where the step would find the doc already there.
- **Competitor list gathering** — same installation moment: one research pass proposes
  candidate competitors, operator curates/filters into a config-tracked list. Per-epic
  desk research starts from this list, not limited to it.

### Work-stream B — settled

1. **Architecture-depth assessment moves to `architecture`.** Decided. Still need to
   confirm, when writing the actual change, whether the orchestrator today uses
   `product`'s YES/NO to skip dispatching `architecture` outright or just reads it as a
   rigor hint — a mechanical detail of the move, not an open design question anymore.
2. **`IRD-001-platform-vision.md`'s current shape doesn't need to be designed around.**
   It was an early one-off; the skill's V2 install-time vision-gathering step defines
   its own generic shape, and the operator will redo bookshaw's actual vision doc to
   match when configuring V2 for this repo. Not a dependency for writing the V2 agent.
3. **No numeric research budget — "be wise" is the actual instruction.** Scale desk
   research effort to the size/risk of the work by judgment, not a stated cap. Written
   into the agent as a real instruction (proportion research to the work), not left
   implicit by omission.

---

## Work-stream C: Initiatives — a radically different lifecycle from V1, not an extension of it

**V1's Epic/Child shape is explicitly not being extended here — this is a rethought
lifecycle**, prompted directly by this morning's epic #157 finding (per-task `lld.md`
+ per-task `lld-review` is where 3-5-round bounces concentrated). The new shape
collapses today's two design docs (epic-level `architecture.md`, task-level `lld.md`)
into one design doc per epic, and moves integration/e2e testing off the per-task path
entirely.

**Initiative creation itself is manual** — an operator decides to start one; nothing in
the pipeline generates one on its own. What *is* pluggable is how the pipeline
recognizes an issue as an Initiative once created — see work-stream A's
`classify_unit`: GitHub might signal it via Issue Type or a label depending on the
client's setup, Jira via its native hierarchy level. The GitHub Issue Type
prerequisite noted below is one possible `classify_unit` implementation for GitHub,
not the only one a client could configure.

### The new hierarchy and flow

```
Initiative (product-driven, created manually by the operator)
  -> product writes the IRD (product.md), per work-stream B's content rules
  -> Gate A approval
  -> the ORCHESTRATOR (not a dispatched agent) creates the list of Epics from the
     approved IRD -- mechanical decomposition, the same role the orchestrator
     already plays for today's Epic/Child bookkeeping, one tier up. Each Epic's
     issue body carries a pointer to the Initiative's IRD, plus its own explicit
     scope carve-out: which slice of the IRD this Epic covers.

**Engineering-driven work bypasses the Initiative/IRD path entirely — settled.** No
`product.md` at all. The Epic is created directly, and its scope is laid out manually
(by the operator) in the Epic's own body — there is no IRD to derive it from. `Epic`
still skips straight to `architecture`, which now carries a real responsibility it
doesn't have on the Initiative path: **if the manually-laid-out scope is unclear,
`architecture` asks the operator clarifying questions directly**, the same "ask before
you assume" posture `product` has today, since there's no upstream IRD step to have
caught the ambiguity first. An Initiative wrapper *may* still exist around an
engineering-driven Epic, but if so it's a program-management/grouping artifact only —
it does not imply a `product.md` gets written, and `architecture` does not read one.

**Epic-cut rule: every Epic must be independently mergeable to main and independently
shippable on its own.** This is the actual constraint on whoever cuts Epics from an
Initiative's IRD (the orchestrator) -- an Epic that only makes sense once a sibling
Epic has also merged is cut wrong. Same non-overlap spirit as today's per-child
footprint rule, applied one level up and to shippability, not just files.

Epic
  -> architecture reads the FULL Initiative IRD + this Epic's own scope slice,
     writes architecture.md. Mechanically unchanged from today -- same STOP
     protocol, same doc-altitude rules, same Gate B human-gate-with-confidence-
     autopass (default profile 95, standing profile 90 -- unchanged).
  -> lld MOVES UP to epic level (was task/child level in V1). One lld.md per
     epic, not per task.
     - Contains a per-task `## Footprint` subsection, mirroring how epic-level
       architecture.md today has one design subsection per child -- this is
       what preserves list-parallel-ready's collision-safety check once there's
       no separate per-task lld.md to read a footprint from.
     - lld-review is ONE pass over the whole epic-level document, not one pass
       per task.
  -> Task -> development, implementing against its own subsection of the shared
     epic-level lld.md. UNIT TESTS ONLY at this level -- no per-task integration
     tests.
  -> Once every task in the epic is merged: two standing tasks every epic
     always has -- an Integration-test task and an e2e-test task, run once,
     epic-wide, after the functional tasks land. The e2e task's job is not just
     to run the suite: it writes whatever e2e coverage is missing and fixes any
     failure it finds (app code or test), the same full lld -> ... -> pr-review
     cycle V1's dedicated e2e child already runs today -- continuity, not new.
  -> Epic close (merge epic branch to main) follows the same guideline as
     today's epic-closing process, with one efficiency rule made explicit: the
     close does **not** re-run the test suites again -- it reuses the
     Integration-test and e2e-test tasks' own attestations as the merge
     evidence, rather than independently re-verifying what those two tasks
     just proved.
```

### This reverses a V1 change made earlier today, on purpose

Earlier today, `sdlc-development.md` was given IT-scoping (run only the
touched-domain `test/<domain>` subset per task, not the full suite) in response to a
different ask (keep per-task IT, just make it cheaper). This new model removes
per-task IT entirely rather than scoping it. Confirmed intentional, not an oversight
— but worth being explicit that **if V2's lifecycle ships, that specific V1 fix
becomes dead code in the V2 path** (still correct and live for any repo staying on
V1's Epic/Child shape).

### Completion-gate change — settled

`sdlc-pr-review.md`'s (and `sdlc-development.md`'s) "no integration test for a
design-flagged risk is a real gap" rule does not apply at task level in V2. **Decision:
`pr-review` reviews what's present only** — it judges the task's own diff and its own
unit tests on their own merits, and does not check for or bounce on the *absence* of
integration coverage, since that's never supposed to exist at this level. The
mock-doesn't-close-an-integration-risk discipline still holds, just relocated
entirely to the epic-close Integration-test task, which is the one place it's now
checked.

**Scope confirmed: `sdlc-development.md` and `sdlc-pr-review.md` change *only* on
testing guidelines — the IT-scope removal above and this completion-gate change.**
Everything else in both files (TDD discipline, the completion-gate checklist beyond
the IT item, the review stance, the fan-out layers, the empirical-not-diff-only bar,
the mutation-check rule) stays as-is. Not a rewrite the way `lld` is.

### Settled: LLD's core principle, and how the epic-level shape falls out of it

**`lld` creates the Task issues**, not `architecture` — confirmed, and re-derived from
first principles rather than assumed, because that derivation is what actually shapes
the rewrite. LLD has exactly three invariant jobs, unchanged by V1 vs. V2:

1. **Resolution, not re-design.** `architecture.md` is fixed and trusted; LLD closes
   every task-local decision architecture deliberately left open ("depth goes down,
   not in" is architecture's own discipline — it pushes detail downward on purpose).
   LLD never re-litigates what architecture already decided — a design that doesn't
   actually fit is an escalation, never a quiet workaround (the existing
   "fits vs. deviates" call, now made once at epic granularity instead of per-task).
2. **Proof over assertion.** Architecture is allowed to hypothesize ("this bets on Y,
   falls back to Z"); LLD is not allowed to hypothesize about anything checkable.
   Every negative claim, threshold, boundary's real behavior, completeness claim, and
   runtime/library mechanism claim must be backed by something actually executed — a
   grep with output, a quoted source sentence, an opened implementation, a sweep, a
   positive/negative control. Unchanged by the epic-level move.
3. **Collision-safety declaration.** Whatever unit LLD's output maps to is what gets
   dispatched concurrently, so LLD also owns declaring blast radius (the Footprint)
   completely enough that the orchestrator can parallelize safely — a distinct job
   from design resolution, not a byproduct of it.

**Scaling these three up to epic level is what produces the new shape — it isn't a
mechanical port of the V1 file to a bigger scope:**

- Principle 1 ("resolution") now has to include *deciding what the tasks are*, since
  nothing upstream of `lld` has carved them at epic level. That is the honest reason
  task-creation belongs in `lld` and not `architecture`: carving concrete task
  boundaries *is* the depth architecture is explicitly told to leave out. Not a new
  responsibility bolted on — principle 1 applied at a coarser starting grain.
- Principle 2 ("proof over assertion") is unchanged — every task's subsection inside
  the one epic-level `lld.md` carries the same evidence bar as a V1 task-level
  `lld.md` did, just written N times in one document instead of N separate ones.
- Principle 3 ("collision-safety") gets structurally *stronger*, not just relocated:
  with every task's Footprint inside one document, overlap between tasks is a direct
  comparison inside one file instead of an inference across N separate documents.
  **This makes the "post a finding to the sibling's own issue" fix built into
  `sdlc-lld.md` earlier today unnecessary by construction in V2** — there is no
  cross-document boundary left for a coordination note to get lost across, because
  there is only one document. (Flagging this the same way as the IT-scoping and
  completion-gate reversals above: a real V1 fix that goes dead once V2 ships.)
- `lld-review`'s one pass gains a third thing to adjudicate that V1's per-task review
  never had to consider: not just "is each task's resolution sound" and "do
  footprints overlap," but **was the task-carving itself good** — right-sized,
  correctly sequenced, non-overlapping by construction. This is the actual structural
  answer to this morning's epic #157 finding (3-5 round bounces concentrated in
  per-task `lld-review` cycles): a bad carving decision now has one reviewable place
  to be caught, before any task's `development` even starts, instead of surfacing
  piecemeal across N separate per-task review cycles.

So the epic-level `lld` is the same three invariants as V1's task-level `lld`, with
task-carving now explicitly inside principle 1's territory because there is nothing
upstream of `lld` to have done it already — not a redesign of what LLD *is*, a
consequence of where it now starts from.

### Other prerequisites and open questions

- **A `classify_unit` implementation must exist for whichever provider is in use
  before Initiatives work at all** — for GitHub, the likely default is a real Issue
  Type in the repo's type configuration (a repo-settings action, not a skill change),
  but per the note above, a client could configure a label-based signal instead if
  Issue Types aren't available on their plan.
- **Doc-path resolution** in the orchestration code: on the Initiative path,
  `architecture` needs to read `<docRoot>/initiative-<n>/product.md` (filtered by the
  Epic's own scope carve-out) instead of expecting its own unit to have written a
  `product.md`. On the engineering-driven path, there is no `product.md` to resolve at
  all — `architecture` reads the Epic's own manually-written scope directly.
- **Overlap with Jira's native hierarchy** — Jira (Advanced Roadmaps) already models
  Initiative above Epic natively. If Jira is a near-term work-item target (work-stream
  A), this tier may be easier to model there than on GitHub's sub-issues — worth
  designing A and C together once Jira is actually on the table, not independently.

---

## Sequencing recommendation (not yet agreed)

Work-streams A and B are independent and could be built in either order or in parallel.
Work-stream C depends on B being settled first on the Initiative path (the
orchestrator-created Epic list reads the IRD work-stream B's `product` agent
produces) — the engineering-driven path has no such dependency, since it never reads
a `product.md` at all. C also edits things B and the base agent files already own: the
architecture-depth-assessment relocation (now settled as part of B) lands inside
`sdlc-architecture.md`, and the `pr-review`/`development` completion-gate change (now
settled: review what's present only) lands inside those two files — so C is not purely
additive once started. Nothing here commits to an
order — flagging it only so the next session doesn't have to re-derive the
dependency.
