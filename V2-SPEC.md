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
  implementation. Only the seam needs to exist, and it needs to be *easy* to plug a
  second one in later, not necessarily done now.
- The "Initiative" hierarchy tier (see below) — real, wanted, but has its own
  prerequisites and cascading changes independent of everything else here.

---

## Work-stream A: provider abstraction

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

### Open questions, work-stream B

1. **Move the architecture-depth assessment to `architecture`?** Leaning yes (engineering
   judgment, not product judgment) — but need to confirm first whether the orchestrator
   today actually uses `product`'s YES/NO to *skip dispatching* `architecture` outright,
   or whether `architecture` always runs and just reads it as a rigor hint. Changes
   whether moving it is a pure relocation or also a routing-logic change.
2. **Is `bookshaw-docs/requirements/IRD-001-platform-vision.md`'s shape what the V2
   agent should assume, or a one-off?** Located (see above) but not yet reviewed for
   content/format — needs reading before the V2 agent is written to depend on its shape
   generically across repos, not just this one instance.
3. **Research depth vs. cost** — operator confirmed cost control and agility are a real
   priority, not just a nice-to-have. Depth should scale with the architecture-depth
   assessment (or whatever replaces it) — needs an explicit stated budget/cap, not left
   implicit.

---

## Work-stream C (separate design pass, not started): Initiatives

New hierarchy tier: `Initiative > Epic > Child`. One IRD per Initiative, written once by
`product`; every Epic under it skips `product` entirely and reads the Initiative's
`product.md` directly (the same way a child today reads its epic's `architecture.md`).
Each Epic still gets its own `architecture.md` — only the requirements layer moves up,
not the design layer.

Real prerequisites and cascading changes, none of them product-agent changes:

- **GitHub-side prerequisite**: "Initiative" needs to be a real Issue Type in the repo's
  type configuration (like `Task`/`Bug`/`Feature` today) before the skill can reference
  it — a repo-settings change, not a skill change.
- **Doc-path resolution** in the orchestration code changes: `architecture` needs to read
  `<docRoot>/initiative-<parent>/product.md` instead of expecting its own unit to have
  written one.
- **Gate cadence** — does Gate A (human review of the IRD) now happen once per Initiative
  instead of once per Epic? Real tradeoff in how often a human touches the pipeline.
- **Overlap with Jira's native hierarchy** — Jira (Advanced Roadmaps) already has a
  native Initiative concept above Epic. If Jira is a near-term work-item target, this
  tier might be *easier* to model there than to bolt onto GitHub's sub-issues — worth
  designing work-streams A and C together rather than independently once Jira is
  actually on the table.

Deliberately not scoped further here — pick this up as its own pass once work-stream B's
agent content is settled and the GitHub Issue Type prerequisite is either created or
explicitly deferred.

---

## Sequencing recommendation (not yet agreed)

Work-streams A and B are independent and could be built in either order or in parallel.
Work-stream C depends on B being settled first (Initiatives change *where* `product`
runs, not *what* it does) and has its own GitHub-config prerequisite regardless of
sequencing. Nothing here commits to an order — flagging it only so the next session
doesn't have to re-derive that B blocks C.
