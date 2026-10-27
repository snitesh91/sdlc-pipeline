<!--
Template for <docRoot>/{epic,issue}-<n>/architecture.md: the HLD a human approves at
Gate B and `lld` builds from. Rules: the sdlc plugin's references/design-doc-rules.md,
"Document altitude". In short:
  - Target length (guidance): about 1,500 words for an Epic, about 600 for a standing
    child (Footprint and Implementation notes not counted). Keep it as short as the
    content allows; push code-level detail to lld.md.
  - The reader may not be a specialist. Explain each technical choice in plain words
    first (what it does, why this way, what it costs), then the technical detail.
    Define each term of art on first use.
  - No pipeline talk, no revision history, no lld-depth content (files, functions,
    schemas, edge-case lists, code). A long analysis goes to
    <docRoot>/<unit>/decision-<slug>.md, linked in one line.
  - Bullets and tables. Cut any paragraph that would not change an lld.

A CONDITIONAL section whose trigger did not fire collapses to "N/A — <one-line reason>".
Delete this comment block before committing.
-->

# Issue #<n> — <title> — Architecture

Builds on [`product.md`](./product.md).

<!-- Link: a standing child links its own product.md; an Initiative-driven Epic links the
     Initiative's IRD (<docRoot>/issue-<roadmap-task>/product.md on main); an
     engineering-driven Epic drops the line. -->

## Summary

<!-- About 150 words, everyday language, no identifiers or paths. What gets built, how
     it works, what it costs to run, and the main thing that could go wrong. A reviewer
     who reads only this and the diagram must understand the design. -->

```mermaid
flowchart LR
```

<!-- The data and control flow: the mechanism and where it can fail, not boxes named
     after the feature. -->

## 1. Scope

<!-- Two lists. Everything in "In scope" is a goal; everything in "Out of scope" is a real
     candidate deliberately excluded, with its reason on the same line. -->

**In scope**
- ...

**Out of scope**
- ... — *because ...*

## 2. Context

<!-- Bullets, only what the design turns on: the behaviour it changes, the constraint it
     lives inside, the prior decision it inherits, each with a path. Constraints are
     externally fixed things (budget, runtime, platform, legal), plus one line on how much
     freedom the design has. Backward compatibility is a constraint only if the repo's
     AGENTS.md says it has shipped. -->

- ...

## 3. Design

<!-- The parts, what each is responsible for, and how a request or event moves through
     them. For each part: one plain sentence first, then the technical line. One more
     Mermaid diagram per new component boundary.

     Interfaces inline where they occur: direction, shape sketch, error cases. Never paste
     a schema.

     A major decision (a reviewer could reasonably choose the other way) gets one
     comparison table on the axes that differ, then a bet line and a fallback line. State
     every other decision in one sentence with its reason. -->

### Decision: <name>

<!-- One plain sentence: what is being chosen and why it matters to the product. -->

| | Option A — ... | Option B — ... |
|---|---|---|
| <axis> | | |

**Chosen:** A. **Bets on:** ... **If that's wrong:** ...

## 4. Data model

<!-- CONDITIONAL: only when entities are added or their relationships or invariants
     change. Design-relevant shape only; columns and types are lld's. -->

## 5. Non-functional envelope

<!-- Measurable scenarios. State the cost line even when it is $0. -->

| Dimension | Target | Observed how |
|---|---|---|

## 6. Security and privacy

<!-- Who may do what, which data crosses which boundary, what happens to personal data.
     If nothing changes, one line saying so. -->

## 7. Failure modes

<!-- CONDITIONAL: only for a new way production can break (a new external dependency, an
     async path, a stateful migration, a new process boundary). -->

| Failure | Blast radius | Detection | Response |
|---|---|---|---|

## 8. Compatibility and rollout

<!-- One or two lines: what breaks and for whom. Pre-launch repo: "breaking, no
     migration" is usually right. Phases only if real, each with its exit condition. -->

## 9. Risks

<!-- Specific risks that could change a decision, and debt taken on with what would
     repay it. -->

| Risk | Impact | Likelihood | Measure |
|---|---|---|---|

## 10. Acceptance criteria

<!-- Carried from product.md as a flat checklist, one testable line each. Nothing else
     cites an AC by number. Say inline if this design revised one. -->

- [ ] ...

## 11. Open questions

<!-- Each with a default. "None" is valid. No workable default = escalation. -->

1. **<question>** — <trade-off>. *Default:* ...

## 12. Footprint

<!-- CONDITIONAL: a standing child's doc only; never an Epic's. Parsed by code: one
     backticked exact path or directory-prefix glob per bullet, no mid-path wildcard. -->

- `<backend-dir>/src/<module>/**`

## 13. Implementation notes

<!-- CONDITIONAL: a standing child's doc only (its only design doc for development);
     never an Epic's. -->
