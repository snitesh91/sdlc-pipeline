# Design-doc rules: product.md and architecture.md

Read by `product`, `product-review`, `architecture` and `design-review` (`arch-review`). `lld.md` has no altitude rule.

## Document altitude

A human reads `product.md` and `architecture.md` at a gate.

- Start each from its template in the `docTemplates` directory your SubagentStart context names, else `${CLAUDE_PLUGIN_ROOT}/templates/`. Fill the sections in order. Delete the template's HTML comments.
- Write in STE-lite (`stage-playbooks.md`, "Style").
- Say nothing about the pipeline: no stage names, gates, fields, rounds, review history or revision deltas. Put process and deltas in the handoff comment.
- Cite the vendor's own documentation for every external limit (rate limit, size cap, payload ceiling). This is the only source citation a `product.md` carries.

### `product.md` is a requirements document

- Follow the repo's requirements house style. Read its worked example if the repo names one.
- Use the template's section order: Background, Goals, Functional Scope, User Experience, Non-Functional Requirements, Constraints, Acceptance Criteria, Out of Scope, Open Questions, Decisions Log. Drop an empty section. Keep Open Questions with "None".
- State observable behaviour and bounds. Never choose the technology. Write a fixed business limit under Constraints as the limit, not as a product that meets it.
- Each Goal names one observable success signal. Add a counter-signal where the goal can be gamed.
- No hedging and no meta-commentary: no TL;DR, no confidence disclaimer, no "what changed" section. Put uncertainty in the requirement or in Open Questions.
- State each fact once, in the section that owns it. Other sections refer to it.
- Keep detail inline. Never use `<details>`.
- No system flow diagram. Put an ASCII mockup under User Experience for a new or changed screen.
- Length: a standing child fits in 1–2 pages. An Initiative takes what its scope needs.

### `architecture.md` is an HLD

- Use bullets and tables. Cut any paragraph that would not change an `lld`.
- Leave out what `lld` decides: exact files, schemas, edge-case lists, test surfaces. Put a long analysis in `<docRoot>/<unit>/decision-<slug>.md` and link it in one line.
- No decisions-log section.
- Scope holds the goals (in scope) and non-goals (out of scope, each with its reason). An out-of-scope entry is a real candidate you excluded.
- Put constraints as bullets inside Context. Add one line on how much freedom the design has.
- Lead the Design section with the shape and the flow. Include one Mermaid diagram of the data and control flow. Add one Mermaid diagram per new component boundary. A diagram shows the mechanism and where it fails, not boxes named after the feature.
- Name each interface inline with direction, shape and error cases.
- Give a major decision one comparison table on the axes that differ, then one line on its bet and one line on its fallback. A decision is major when a reviewer could reasonably choose the other way. State every other decision in one sentence with its reason.
- Write non-functional requirements as measurable scenarios. State the cost line, even when it is `$0`.
- Write acceptance criteria as a flat checklist, one line each. Nothing else cites an AC by number.
- Include Data model and Failure modes only when entities or invariants change, or a new failure path appears.
- Include `## Footprint` and `## Implementation notes` only in a standing child's doc. Never in an Epic's doc.

## Scope alignment before `product`

The orchestrator runs this step before it first claims `product` for a unit with no `product.md`.

1. Read the issue body, thread and existing children.
2. State in a few lines what the unit covers, excludes and leaves open.
3. Ask the operator every genuine ambiguity in one `AskUserQuestion` batch. Invent no questions.
4. Copy the answers verbatim into the `product` prompt as "Operator scope decisions".

Skip this step on a rework round and on a resume where `product.md` exists. Engineering-driven work never reaches it. Gate A still reviews the doc.
