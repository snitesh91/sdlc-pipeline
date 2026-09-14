# Design-doc content rules — product.md and architecture.md

Read by `product`, `product-review`, `architecture`, and `design-review` (its
`arch-review` half only — `lld-review` does not need this file: `lld.md` has no
altitude requirement, per "Document altitude" below). Split out of
`references/stage-playbooks.md` on 2026-09-14 so a role that never touches
`product.md`/`architecture.md` (`lld`, `development`, `pr-review`, `exploratory`) is
not told to read it.

## Document altitude — two different documents, two different contracts

`product.md` and `architecture.md` (epic-level, or issue-level for a standing child)
are both read by a **human** at a gate, but they are not the same kind of document and
do not follow the same rules. `lld.md` has **no** human gate and no altitude
requirement at all — it can stay as technical as the work demands.

Start both from the matching skeleton in `<docRoot>/<pipeline.docTemplates>/` (default `_templates`) — copy it in,
fill the sections in order, delete the template's instructional HTML comments.

### `product.md` is a requirements document — requirements only

It follows the house style of the repo's own requirements docs; **if the repo's
`CLAUDE.md` or doc conventions name a worked example, read it before writing one**.
Section order comes from `product.template.md`: Background, Goals, Functional Scope,
User Experience, Non-Functional Requirements, Constraints, Acceptance Criteria, Out of
Scope, Open Questions, Decisions Log. A section with nothing in it is dropped rather
than kept as `N/A` filler — the one exception is Open Questions, which stays as "None".

Four rules, each of which a real document has broken:

- **Nothing about the pipeline appears in the document.** No stage names, no gate
  references, no field names, no "this document creates no child issues", no note on
  what this stage did or did not do, no link to this skill. A reader must not be able
  to tell from the prose that an automated pipeline produced it. Process belongs in the
  handoff comment, which is where a pipeline reader is already looking.
- **Requirements state observable behaviour; they never choose the technology.**
  "Alert channels can be added or removed by configuration, with no application
  change" is a requirement. "Alerts go out through the cloud provider's managed
  monitoring email channels" is an architecture decision wearing a requirement's
  clothes, and it forecloses the options the architecture stage exists to weigh. Name
  the capability, the observable behaviour, and the bound; leave platform, vendor,
  service and mechanism to `architecture.md`. A genuine constraint the business or the
  operator has already fixed goes under **Constraints**, written as the constraint
  itself ("must run with no persistent agent host"), not as the product that satisfies
  it.
- **No hedging layer and no meta-commentary.** No TL;DR box, no "this is a hypothesis"
  preamble, no confidence disclaimer, no "what changed versus the previous version"
  section. Uncertainty is expressed where it lives — in the specific requirement, or in
  Open Questions. Revisions are recorded in the `**Last revised**` line and, where a
  requirement genuinely reversed, rewritten in place in the Decisions Log.
- **Detail stays inline, uncollapsed.** The architecture stage reads this document in
  full; there is no audience it needs to be hidden from. Do not push requirement detail
  into `<details>` blocks.

**No system flow diagram.** How the parts connect is architecture's picture to draw.
Where the *layout* of a screen matters, an ASCII mock-up or a linked image under User
Experience is worth more than a paragraph — that is the diagram this document wants.

### `architecture.md` is an HLD — what the design *is*, in as few words as carry it

Its own template encodes the structure; don't improvise a different one. Three rules
override every section in it:

- **Nothing about the pipeline appears in the document** — the same rule `product.md`
  already lives under. No round numbers, no stage names, no review history, no "what
  changed since the previous revision", no note on what this stage did. **The delta
  between revisions goes in the handoff comment on the issue**, which is where a
  pipeline reader is already looking and where a scoped `arch-review` round reads it
  from. There is no decisions-log section in the document.
- **Points, not essays.** Bullets and tables are the default; prose is the exception.
  Do not explain what already exists beyond what the design turns on, and never restate
  a requirement to introduce a decision. The test: a paragraph that would not change an
  `lld` is cut.
- **Depth goes down, not in.** Anything an `lld` would decide — exact files, schemas,
  grep commands, edge-case enumerations, test surfaces — is not in this document. A
  decision that genuinely needs long analysis gets a sub-page at
  `<docRoot>/<unit>/decision-<slug>.md`, linked in one line. (This is the one
  sanctioned split; design content still never goes to the repo's own general
  architecture-docs tree.)

Within that:

- **Scope is the single source of goals and non-goals.** In-scope entries *are* the
  goals; out-of-scope entries *are* the non-goals, each with its reason in the same
  line. There is no separate goals section to keep in sync. An out-of-scope entry is a
  real candidate deliberately excluded — never a negated goal.
- **Constraints are bullets inside Context**, not a section: externally fixed things
  only, plus one line on how much freedom the design actually has, since that is what
  makes an obvious call defensible.
- **The Design section leads with the design, not with a decision log.** Describe the
  shape and the flow first, with a required Mermaid diagram whenever more than one
  component or process boundary is involved — the mechanism and where it fails, never
  boxes named after the feature. Interfaces are named **inline where they occur**, not
  in a section of their own.
- **A major decision gets one comparison table across the axes that actually differ**,
  then one line on what it bets on and one on the fallback — that pair is what `lld`'s
  fits-vs-deviates call tests against. "Major" means a reviewer could reasonably have
  gone the other way. Everything else is simply stated as part of the design with its
  reason in the same sentence: no table, no options list. A decision with one plausible
  option is not a decision.
- **Non-functional requirements are scenarios, not adjectives.** "p95 under 300 ms at
  50 concurrent requests, measured at the API boundary" can go red; "fast" cannot. The
  cost line is stated even when it is `$0` — a stated zero is checkable, silence isn't.
- **Acceptance criteria are a flat checklist and nothing else** — one line each, no
  sub-bullets, no rationale, no evidence notes. **Nothing elsewhere in the document
  cites an AC by number**; cross-references rot the moment the list is revised, and
  `development` maps criteria to tests from the list itself.
- **Data model and Failure modes are conditional** — present only when entities or
  invariants actually change, or when the design introduces a genuinely new way for
  production to break. Do not draw the existing model.
- **`Footprint` and `Implementation notes` appear only in a standing-epic child's
  doc.** That doc is the only design doc `development` ever gets, and the only
  `architecture.md` `parse_footprint` is ever pointed at. **Both are omitted entirely
  from an epic-level doc**: per-child footprints live in each child's `lld.md`, and
  implementation depth is that `lld`'s job — putting it in the epic doc duplicates it
  at the wrong altitude, which is how one epic-level doc grew to many times its useful
  length (see `references/history.md`).

See "Review altitude" under `arch-review` below for how this shapes review findings.


## Scope alignment before `product` — ask before authoring

The `product` stage's input is the issue as written, and an epic issue is usually a
one-liner. It does not carry the scope the operator has in mind, and every downstream
document is built from whatever `product.md` decides that scope is. Gate A comes after
`product.md`; by then the framing is already baked into a requirements document,
and a scope correction there re-runs `product`, `product-review`, Gate A, and — if it
reaches architecture — Gate B and the child decomposition too.

So the orchestrator runs a **pre-product scope alignment** the first time a unit
enters `product` (an epic's own product, or a standing-epic child's — anything with no
`product.md` on its branch yet), *before* claiming the stage or dispatching the
agent:

1. Read the issue body and thread, and (for an epic) whatever child issues already
   exist.
2. State back, in a few lines, the interpretation: what the epic **covers**, what it
   **excludes**, and the **decisions the one-liner leaves open**.
3. Ask the operator the genuine ambiguities in one batch — scope boundaries,
   must-haves vs out-of-scope, any decision the issue does not settle. As many real
   questions as there are, none invented for form.
4. Carry the answers **verbatim** into the `product` delegation prompt as "Operator
   scope decisions", and tell the agent they are settled inputs, not hypotheses.

The `product` agent's side of the contract: if its prompt carries no scope-alignment
answers and the issue is thin, it stops and returns scoping questions in its final
message rather than inventing scope (see the `sdlc-product` definition). Skip the step
on rework rounds and on a resume where `product.md` already exists — the scope has a
document by then, and corrections go through the normal rework path or Gate A.

This complements the gates rather than replacing them: Gate A still reviews the
document; this step makes sure the document is written about the right thing.

