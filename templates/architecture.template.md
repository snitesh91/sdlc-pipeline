<!--
Template for <docRoot>/{epic,issue}-<n>/architecture.md -- the architecture
stage's Gate B doc. It is an HLD: it says what the design IS, at the altitude a
reviewer can approve and an LLD can be written from.

THREE RULES THAT OVERRIDE EVERY SECTION BELOW.

1. NOTHING ABOUT THE PIPELINE APPEARS IN THIS DOCUMENT. No round numbers, no "what
   changed since the last revision", no stage names, no review history, no
   "restructured on instruction". A reader must not be able to tell an automated
   pipeline produced it. The delta between revisions belongs in the handoff comment
   on the issue, which is where a pipeline reader is already looking.

2. POINTS, NOT ESSAYS. Bullets and tables are the default; prose is the exception and
   earns its place. Do not explain what already exists beyond what the design turns
   on. Do not restate a requirement to introduce a decision. If a paragraph would not
   change an LLD, cut it.

3. DEPTH GOES DOWN, NOT IN. Anything an LLD would decide -- exact files, schemas,
   grep commands, edge-case enumerations, test surfaces -- is not in this document.
   For a decision that genuinely needs long analysis, write a sub-page at
   <docRoot>/<unit>/decision-<slug>.md and link it in one line.

Sections marked CONDITIONAL name their trigger; if it did not fire, collapse to
"N/A — <one-line reason>" rather than deleting. Delete this comment block before
committing.
-->

# Issue #<n> — <title> — Architecture

Builds on [`product.md`](./product.md).

<!-- 2-3 sentences, plain language, no paths or identifiers. What the design is and
     what it turns on. The first and sometimes only thing a reviewer reads. -->
> [!TIP]
> **TL;DR:** ...

## 1. Scope

<!-- In and out, as two lists. Goals and non-goals are read off this and nowhere else:
     everything in "In scope" is a goal, everything in "Out of scope" is a non-goal.
     Do not restate product.md's requirements -- name the surfaces this design covers.
     An out-of-scope entry is a real candidate deliberately excluded, with the reason
     in the same line; never a negated goal ("not slow" is not a non-goal). -->

**In scope**
- ...

**Out of scope**
- ... — *because ...*

## 2. Context

<!-- Points only, and only what the design turns on: the existing behaviour it changes,
     the constraint it must live inside, the prior decision it inherits. Cite with a
     path. Ten bullets is a lot. Do not narrate the current system.
     Constraints live here as bullets, not as their own section -- externally fixed
     things only (budget, runtime, platform, legal), plus one line on how much freedom
     the design actually has, since that is what makes an obvious call defensible.
     This repo is PRE-LAUNCH: backward compatibility is not a constraint. -->

- ...

## 3. Design

<!-- The core of the document, and usually most of it. Order: the design first, then
     the decisions inside it.

     (a) DESCRIBE THE DESIGN. What the shape is, what each part is responsible for,
         how a request or event moves through it. A diagram is required whenever more
         than one component or process boundary is involved -- flowchart or sequence,
         showing the mechanism and where it can fail, never boxes named after the
         feature.

     (b) NAME THE INTERFACES INLINE, where they occur in the design: a changed route,
         response shape, emitted event, config key or shared type -- direction, the
         shape, the error cases. Sketch the contract; never paste an OpenAPI document.

     (c) MAJOR DECISIONS get a comparison across the axes that actually differ -- one
         table, a few rows, no essay per cell. Follow it with one line on what the
         choice bets on and one on the fallback if implementation disproves it.
         A "major" decision is one a reviewer could reasonably have decided the other
         way. Everything else is simply stated as part of the design, with its reason
         in the same sentence -- no table, no options list.
         If a decision needs more than the table holds, sub-page it and link it. -->

...

```mermaid
flowchart LR
```

### Decision: <name>

| | Option A — ... | Option B — ... |
|---|---|---|
| <axis> | | |
| <axis> | | |

**Chosen:** A. **Bets on:** ... **If that's wrong:** ...

## 4. Data model

<!-- CONDITIONAL — only when entities are added or their relationships/invariants
     change. Design-relevant shape only; columns and types are the LLD's. Omit the
     diagram entirely when nothing changes -- do not draw the existing model. -->

## 5. Non-functional envelope

<!-- Measurable scenarios, not adjectives. Each line is something a test or dashboard
     can settle. State the cost line even when it is $0. -->

| Dimension | Target | Observed how |
|---|---|---|

## 6. Security and privacy

<!-- Auth, authorisation, and what data crosses which boundary. Name the PII and what
     happens to it. If nothing here changes, one line saying so -- that is checkable,
     silence is not. -->

## 7. Failure modes

<!-- CONDITIONAL — only when this introduces a new way for production to break: a new
     external dependency, an async path, a stateful migration, a new process boundary.
     One row each: what breaks, who notices, what they do. -->

| Failure | Blast radius | Detection | Response |
|---|---|---|---|

## 8. Compatibility and rollout

<!-- One or two lines. Whether it breaks anything and for whom; pre-launch, "breaking,
     no migration" is usually the right answer. Phases only if there genuinely are
     phases, each with its exit condition. -->

## 9. Risks

<!-- Prioritised, specific, and honest. A risk that could not change a decision is not
     a risk. Debt this design knowingly takes on belongs here with what would repay it.
     "No risks identified" is a finding about the document. -->

| Risk | Impact | Likelihood | Measure |
|---|---|---|---|

## 10. Acceptance criteria

<!-- Carried from product.md as a flat checklist. One line each, no sub-bullets, no
     rationale, no evidence notes, no cross-references from elsewhere in this document
     -- nothing else here cites an AC by number. Each must be observable enough to
     write a failing test from without reading code. Say so inline if this stage
     revised one. -->

- [ ] ...

## 11. Open questions

<!-- Each with a stated default so an unanswered question blocks nothing. "None" is a
     valid answer. A question with no workable default is an escalation, not an open
     question. -->

1. **<question>** — <trade-off>. *Default:* ...

## 12. Footprint

<!-- CONDITIONAL — required ONLY for a standing-epic child's doc, which has no lld.md
     for the footprint to live in. Omit entirely from an epic-level doc: per-child
     footprints belong in each child's lld.md, and nothing parses the epic doc's.
     Shape is parsed mechanically (parse_footprint) -- backticked paths, one per
     bullet, exact paths or directory-prefix globs, no mid-path wildcards. -->

- `<backend-dir>/src/<module>/**`

## 13. Implementation notes

<!-- CONDITIONAL — required ONLY for a standing-epic child's doc, which is the only
     design doc `development` gets. Omit entirely from an epic-level doc: for a normal
     epic, this content is each child's lld.md and putting it here duplicates it at
     the wrong altitude. -->

<details>
<summary>Implementation notes for development</summary>

...

</details>
