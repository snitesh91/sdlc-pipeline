# Design-doc content rules — product.md and architecture.md

Read by `product`, `product-review`, `architecture`, and `design-review` (its
`arch-review` half only). `lld.md` has no human gate and no altitude requirement; it can
be as technical as the work demands.

## Document altitude — two different documents, two different contracts

`product.md` (an Initiative's, or a standing child's) and `architecture.md` (an Epic's,
or a standing child's) are both read by a human at a gate. Each has its own rules below.

Start each from its skeleton — the repo's `<docRoot>/<pipeline.docTemplates>/` (default
`_templates`) if present, else the plugin's `templates/`: copy it in, fill the sections in
order, and delete the template's instructional HTML comments.

**Both documents: nothing about the pipeline.** Leave out stage names, gate
references, field names, round numbers, review history, "what changed since the previous
revision", notes on what this stage did or did not do, "this document creates no child
issues", and links to this skill. A reader must not be able to tell that a pipeline
produced the document. Process and the delta between revisions go in the handoff comment
on the issue, where a scoped `arch-review` round reads the delta.

**Both documents: an external limit cites its vendor.** A third-party API or platform
limit the document relies on (a rate limit, a list-size cap, a payload ceiling, a free-tier
bound) links the vendor's own documentation and states the limit it gives — never from
memory. It is the one source citation a `product.md` carries.

### `product.md` is a requirements document — requirements only

- Follow the house style of the repo's own requirements docs. If the repo's `CLAUDE.md`
  or doc conventions name a worked example, read it before writing.
- Section order (from `product.template.md`): Background, Goals, Functional Scope, User
  Experience, Non-Functional Requirements, Constraints, Acceptance Criteria, Out of Scope,
  Open Questions, Decisions Log. Drop an empty section instead of filling it with `N/A`.
  The exception is Open Questions, which stays with "None".
- **Requirements state observable behaviour; they never choose the technology.** Name
  the capability, the observable behaviour, and the bound. Leave platform, vendor,
  service and mechanism to `architecture.md`. ("Alert channels can be added or removed by
  configuration, with no application change" is a requirement. "Alerts go out through the
  cloud provider's managed monitoring email channels" is not.) A constraint the business
  or operator has already fixed goes under **Constraints**, written as the constraint
  ("must run with no persistent agent host"), not as a product that satisfies it.
- **No hedging and no meta-commentary:** no TL;DR box, "this is a hypothesis" preamble,
  confidence disclaimer, or "what changed versus the previous version" section. Put
  uncertainty in the specific requirement or in Open Questions. Record revisions in the
  `**Last revised**` line. If a requirement genuinely reversed, rewrite it in place in the
  Decisions Log.
- **Keep detail inline.** Never hide requirement detail in `<details>` blocks.
- **No system flow diagram**; that belongs to architecture. When a screen's layout
  matters, put an ASCII mock-up or a linked image under User Experience.

### `architecture.md` is an HLD — what the design *is*, in as few words as carry it

Follow its template's structure. These rules override every section:

- **Points, not essays.** Default to bullets and tables. Explain existing code only as
  far as the design depends on it, and never restate a requirement to introduce a
  decision. Cut any paragraph that would not change an `lld`.
- **Depth goes down, not in.** Leave out anything an `lld` decides: exact files, schemas,
  grep commands, edge-case lists, test surfaces. A decision that needs long analysis gets
  a sub-page at `<docRoot>/<unit>/decision-<slug>.md`, linked in one line. That is the
  only allowed split; design content never goes into the repo's general
  architecture-docs tree.
- **No decisions-log section.**

Within that:

- **Scope is the single source of goals and non-goals.** In-scope entries are the goals.
  Out-of-scope entries are the non-goals, each with its reason on the same line. Have no
  separate goals section. An out-of-scope entry is a real candidate that was deliberately
  excluded, never a negated goal.
- **Constraints are bullets inside Context**, not a section of their own. List only
  externally fixed things, plus one line on how much freedom the design actually has.
- **The Design section leads with the design,** not a decision log: the shape and flow
  first. Include a Mermaid diagram whenever more than one component or process boundary is
  involved. It shows the mechanism and where it fails, never boxes named after the
  feature. Name interfaces inline where they occur, not in a separate section.
- **A major decision gets one comparison table** across the axes that actually differ,
  then one line on what it bets on and one line on the fallback. `lld` tests its
  fits-vs-deviates call against that pair. "Major" means a reviewer could reasonably have
  chosen the other way. State every other decision as part of the design, with its
  reason in the same sentence and no table or options list. A decision with only one
  plausible option is not a decision.
- **Non-functional requirements are scenarios, not adjectives:** "p95 under 300 ms at 50
  concurrent requests, measured at the API boundary", not "fast". State the cost line
  even when it is `$0`.
- **Acceptance criteria are a flat checklist and nothing else:** one line each, no
  sub-bullets, rationale, or evidence notes. **Nothing else in the document cites an AC
  by number.** `development` maps criteria to tests from the list itself.
- **Data model and Failure modes are conditional.** Include them only when entities or
  invariants actually change, or when the design adds a genuinely new way for production
  to break. Do not draw the existing model.
- **`Footprint` and `Implementation notes` appear only in a standing-epic child's doc**
  (the only design doc its `development` gets, and the only `architecture.md`
  `parse_footprint` reads). **Leave both out of an Epic's doc:** per-Task footprints live
  in each Task's subsection of the Epic's `lld.md`, and implementation depth is `lld`'s
  job.

## Scope alignment before `product` — ask before authoring

The orchestrator runs this step the first time a unit enters `product` (an Initiative's
own, or a standing child's: anything with no `product.md` on its branch yet). It happens
**before** claiming the stage or dispatching the agent, because a scope correction after
Gate A re-runs every downstream document.

1. Read the issue body and thread, and any child issues that already exist.
2. In a few lines, state what the unit **covers**, what it **excludes**, and the
   **decisions the issue leaves open**.
3. Ask the operator all the genuine ambiguities in one batch (`AskUserQuestion`): scope
   boundaries, must-haves vs out-of-scope, and any decision the issue does not settle.
   Ask every real question and invent none.
4. Copy the answers **verbatim** into the `product` delegation prompt as "Operator scope
   decisions", marked as settled inputs, not hypotheses.

- Skip this step on rework rounds, and on a resume where `product.md` already exists.
  Corrections then go through the normal rework path or Gate A.
- Engineering-driven work never reaches this step. It has no Initiative and no
  `product.md`: its scope is written on the Epic, and `architecture` asks clarifying
  questions if needed.
- **`product` agent:** if your prompt has no scope-alignment answers and the issue is
  thin, stop and return scoping questions in your final message. Do not invent scope.
- This step does not replace Gate A. Gate A still reviews the document; this step makes
  sure the document covers the right thing.
