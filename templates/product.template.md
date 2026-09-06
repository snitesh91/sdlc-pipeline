<!--
Template for <docRoot>/epic-<n>/product.md (a normal epic's own product phase)
or <docRoot>/issue-<n>/product.md (a standing-epic child) -- the product stage's
requirements document.

This document is an IRD -- an initial requirements document, in the same house style as
the repo's existing requirements documents (<requirements-dir>/IRD-*.md). Read the repo's
worked-example IRD before writing one; its section order is the one below.

Fill every section in order. A section with genuinely nothing in it is dropped, not kept
as a placeholder -- except Open Questions, which stays as "None" so a reader knows the
question was asked.

What this document is NOT (see "Document altitude" in
.claude/skills/sdlc-next/references/stage-playbooks.md):
  - not a report on the pipeline -- no stage names, no gate references, no field names,
    no "this document creates no children", no notes on what the product stage did or did
    not do. A reader must not be able to tell from the prose that an automated pipeline
    produced it.
  - not a design document -- state the capability and the observable behaviour that is
    required; the platform, vendor, service and mechanism that deliver it are the
    architecture stage's decisions, not requirements.
  - not a revision log -- there is no "what changed since the last version" section.
    Revisions are recorded in the "Last revised" line and, where a requirement genuinely
    reversed, in the Decisions Log entry itself.
  - not hedged -- no confidence disclaimers, no "this is a hypothesis" framing. Where a
    claim is unverified, say what is unverified in that specific requirement, or put it
    in Open Questions.

Delete this comment block before committing the real document.
-->

# <IRD title — plain product language, no issue number>

**Status**: Draft
**Date**: <YYYY-MM-DD>
**Last revised**: <YYYY-MM-DD — one line on what changed; omit on a first draft>
**Tracking**: <owner>/<repo>#<n>
**Related**: <IRD-00X, other epics this depends on or hands scope to — omit if none>

---

## Background

<!-- Why this work exists, in prose a non-engineer can follow. What is true today, what is
     wrong or missing about it, and who feels it. Two to five paragraphs. This is the first
     thing a reviewer reads -- it replaces any summary box. -->

---

## Goals

<!-- Outcome bullets, not features. Each one is something that becomes true for a user, an
     operator, or the business when this is done. -->

- ...

---

## Functional Scope

<!-- The body of the document. One numbered subsection per functional area, each holding the
     requirements for that area as prose plus bullets. Requirements state observable
     behaviour: what the system must do, what a user or operator must be able to do, what
     must be configurable without a code change. Name the capability, not the product that
     provides it.

     Detail belongs here, inline -- the architecture stage reads this document in full. Do
     not hide requirement detail in collapsed <details> blocks. -->

### 1. <Area>

...

### 2. <Area>

...

---

## User Experience

<!-- Only for user-facing or operator-facing work. Screens, states, and what the person sees
     at each step -- empty states and error states included. An ASCII mock-up or a linked
     image is worth more than a paragraph; include one wherever the layout matters. Do not
     draw a system flow diagram here: how the parts connect is architecture's picture to
     draw, not this document's. Drop the whole section for work with no human-facing
     surface. -->

---

## Non-Functional Requirements

<!-- Performance, availability, security, privacy, retention, cost ceilings, operability.
     Each one measurable -- a number, a bound, or a stated observable condition. -->

- ...

---

## Constraints

<!-- Only genuine constraints the work must respect: an operator ruling, a platform the
     business has already committed to, a regulatory limit, a budget ceiling. Write each as
     the constraint itself, never as a chosen solution ("must run with no persistent agent
     host", not "use vendor X"). Drop the section if there are none. -->

- ...

---

## Acceptance Criteria

<!-- Literal checklist, every item independently testable and written so a failing test can
     be derived from it without reading any code. The testing stage works from this list
     directly. Number them AC1.. and group under a bold line per functional area when the
     list runs long. -->

- [ ] **AC1** — ...

---

## Out of Scope

<!-- What a reader might reasonably expect here and will not get, each with a one-line reason
     and, where it exists, a pointer to the epic or IRD that owns it instead. -->

- ...

---

## Open Questions

<!-- Ranked by how much the answer changes the work. Each carries why it matters and a stated
     default, so it is answerable in seconds. Keep the section with "None" rather than
     dropping it. -->

1. ...

---

## Decisions Log

<!-- Numbered, permanent, append-only. One line per decision with the reasoning compressed
     into it -- the worked-example IRD's style. A decision reversed later is rewritten in place with the
     new ruling and the date, so the log always reads as current truth. -->

1. **<Decision>**: <why, in one or two sentences>
