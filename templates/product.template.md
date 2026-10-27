<!--
Template for <docRoot>/issue-<n>/product.md: the requirements document (IRD) of an
Initiative's Product-Roadmap Task (the whole Initiative, by functional area) or of a
standing child (itself). An Epic has no product.md of its own.

The reader is a product owner who does not read code. Rules: the sdlc plugin's
references/design-doc-rules.md, "Document altitude". In short:
  - Target length (guidance): about 800 words for a standing child; about 1,500 for an
    Initiative of one Epic; about 2,500 for several. Keep it as short as the content allows.
  - Behaviour the user sees and business rules only. No code, paths, endpoints, tables,
    JSON, error codes, config keys, tool or component names, vendor APIs, or
    implementation choices.
  - No pipeline talk, no "what changed" section, no "changes to earlier documents"
    section (link the earlier document under Related), no hedging.
  - Each fact once, in the section that owns it.

Fill the sections in this order. Drop a section with nothing in it, except Open
Questions, which stays as "None". Delete this comment block before committing.
-->

# <IRD title, plain product language, no issue number>

**Status**: Draft
**Date**: <YYYY-MM-DD>
**Last revised**: <YYYY-MM-DD, one line on what changed; omit on a first draft>
**Tracking**: <owner>/<repo>#<n>
**Related**: <earlier IRDs or epics this depends on, replaces or hands scope to; omit if none>

---

## Background

<!-- Why this work exists, for a non-engineer. What is true today, what is wrong or
     missing, and who feels it. One to three short paragraphs. -->

---

## Goals

<!-- One line each: an outcome for a user, an operator or the business, with its
     observable success signal, plus a counter-signal where the goal can be gamed. -->

- <outcome>. **Signal:** <what we observe>. **Counter-signal:** <what must not get worse>

---

## Functional Scope

<!-- One numbered subsection per functional area. Each: what the user can do and what
     they see, and the business rules (who may, how many, how often, what happens when it
     fails). Name the capability, never the product, component or mechanism behind it.
     A few bullets per area; a long area is a sign to split. -->

### 1. <Area>

...

---

## User Experience

<!-- Only for user-facing or operator-facing work. Each new or changed screen or message:
     what the person sees at each step, empty and error states included. An ASCII mock-up
     where layout matters. Describe what a message must convey; quote exact wording only
     when the wording itself is the decision. Drop the section when nothing is
     user-facing. -->

---

## Non-Functional Requirements

<!-- What the user or business can observe: speed, availability, privacy, retention,
     cost ceiling. Each with a number or an observable condition. -->

- ...

---

## Constraints

<!-- Fixed limits the work must respect: an operator ruling, a platform the business has
     committed to, a regulation, a budget. State the limit, not a solution. Drop if none. -->

- ...

---

## Acceptance Criteria

<!-- One line each: an action a person takes in the product and what they then see.
     Testable without reading code. No test-harness design, no implementation terms.
     Number them AC1..; group under a bold line per functional area when long. -->

- [ ] **AC1** — ...

---

## Out of Scope

<!-- What a reader might expect and will not get, each with a one-line reason and, where
     one exists, the IRD or epic that owns it. -->

- ...

---

## Open Questions

<!-- Ranked by impact. Each with why it matters and a default. "None" if none. -->

1. ...

---

## Decisions Log

<!-- Numbered, append-only, one line each with its reason. A reversed decision is
     rewritten in place with the new ruling and the date. -->

1. **<Decision>**: <why, in one sentence>
