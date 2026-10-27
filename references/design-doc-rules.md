# Design-doc rules: product.md, architecture.md, lld.md

Read by `product`, `architecture`, `lld` (each its own subsection) and by `product-review` / `design-review` ("Document altitude" only).

## Document altitude

A human reads each design doc. Write for that reader, not for the next agent.

- Start `product.md` and `architecture.md` from the template in the `docTemplates` directory your SubagentStart context names, else `${CLAUDE_PLUGIN_ROOT}/templates/`. The template holds the section order. Delete its HTML comments.
- Write in STE-lite (`references/doc-authoring.md`, "Style").
- Say nothing about the pipeline: no stage names, gates, fields, rounds, review history or revision deltas. Put process and deltas in the handoff comment.
- State each fact once, in the section that owns it. Other sections refer to it by section name.
- Never write a "changes to earlier documents" section. Link the earlier document in one line under Related or Constraints. Amend it only through `references/initiatives.md`, "Amending an approved `product.md`".
- Keep each doc as short as its content allows. The targets below are guidance, not limits. A doc well over its target is a sign to cut detail or split the work; it never fails review for length alone.

| Doc | Target length (about) |
|---|---|
| `product.md` | standing child 800; Initiative of one Epic 1,500; Initiative of several Epics 2,500 |
| `architecture.md` | standing child 600; Epic 1,500 |
| `lld.md` | 400 per Task section; 500 for everything outside Task sections |

### `product.md` is for a product owner

The reader decides what the product does. The reader does not read code.

- Describe what the user sees and does, and the business rules. Never describe how the system does it.
- Never write: code, file paths, endpoints, routes, table or column names, JSON, error codes, config keys, library or vendor-API names and parameters, tool or function lists, model names, internal component names, or implementation choices. A vendor appears only as a business commitment (Constraints) with its published limit.
- Cite the vendor's own documentation for every external limit. This is the only source citation the doc carries.
- Each Goal names one observable success signal. Add a counter-signal where the goal can be gamed.
- Write each acceptance criterion as one line a person can check by using the product: an action and what the user then sees. No test-harness design (sample sets, fixtures, phrasing counts). No implementation term.
- Put fixed user-facing wording in the doc only when the wording itself is the decision (a legal line, a price line). Otherwise describe what the message must convey.
- Put an ASCII mockup under User Experience only for a new or changed screen. No system flow diagram.
- No hedging and no meta-commentary: no TL;DR, no confidence disclaimer, no "what changed" section. Put uncertainty in the requirement or in Open Questions.
- Keep detail inline. Never use `<details>`.
- Write a fixed business limit under Constraints as the limit, not as a product that meets it.

### `architecture.md` explains, then specifies

The reader approves the design at Gate B and may not be a specialist in this area.

- Open with a plain-language summary of about 150 words: what is built, how it works in everyday words, what it costs, what could go wrong. No identifiers or paths in it.
- Follow it with one Mermaid diagram of the data and control flow. Add one diagram per new component boundary. A diagram shows the mechanism and where it fails, not boxes named after the feature.
- For each technical choice, first say in plain words what it does, why this way and what it costs. Then give the technical detail.
- Define every term of art (a protocol, pattern or vendor feature) in a few words on first use.
- Use bullets and tables. Cut any paragraph that would not change an `lld`.
- Leave out what `lld` decides: exact files, functions, schemas, edge-case lists, test surfaces, code. Put a long analysis in `<docRoot>/<unit>/decision-<slug>.md` and link it in one line.
- Scope holds the goals (in scope) and non-goals (out of scope, each with its reason). An out-of-scope entry is a real candidate you excluded.
- Put constraints as bullets inside Context. Add one line on how much freedom the design has.
- Name each interface inline with direction, shape and error cases. Sketch it; never paste a schema.
- Give a major decision one comparison table on the axes that differ, then one line on its bet and one line on its fallback. A decision is major when a reviewer could reasonably choose the other way. State every other decision in one sentence with its reason.
- Write non-functional requirements as measurable scenarios. State the cost line, even when it is `$0`.
- Write acceptance criteria as a flat checklist, one line each. Nothing else cites an AC by number.
- Include Data model and Failure modes only when entities or invariants change, or a new failure path appears.
- Include `## Footprint` and `## Implementation notes` only in a standing child's doc. Never in an Epic's doc. They do not count toward the target.
- No decisions-log section.

### `lld.md` is the developer's design

The reader is the developer who builds one Task.

- Give each Task section only what `development` needs to build it without a design decision: files and functions to touch, task-local decisions, criteria, risks, footprint.
- Never restate `architecture.md`. Point to its section.
- Never paste implementation code. A fenced snippet is allowed only for a contract (a signature, a type, a message shape) of at most 15 lines.
- Paste proof-sweep output trimmed to the lines that settle the claim. Put a longer output in `.sdlc-scratch/` and quote its count.
- Aim for about 500 words outside Task sections (the carving summary, `## Epic-close checks`, `## Sibling heads checked`).
- A Task section far over about 400 words often means the Task is too big: consider splitting it.

## Scope alignment before `product`

The orchestrator runs this step before it first claims `product` for a unit with no `product.md`.

1. Read the issue body, thread and existing children.
2. State in a few lines what the unit covers, excludes and leaves open.
3. Ask the operator every genuine ambiguity in one `AskUserQuestion` batch. Invent no questions.
4. Copy the answers verbatim into the `product` prompt as "Operator scope decisions".

Skip this step on a rework round and on a resume where `product.md` exists. Engineering-driven work never reaches it. Gate A still reviews the doc.
