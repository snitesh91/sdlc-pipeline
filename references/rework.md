# Rework, blockers and escalation — the orchestrator's routing

A deviation from an Epic's approved `architecture.md` has its own path
(`references/epics.md`, "Architecture deviation escalation"); all other rework is here.

## Routing a finding

When a later stage (`arch-review`/`lld-review`, `pr-review`) finds a problem owned by an
earlier stage, **do not spawn a fresh subagent and do not open a GitHub issue.** Resume
the original agent that owns the responsible stage (`SendMessage` to its tracked ID) with
the specific finding.

- **A stage agent reports genuine ambiguity** → resume the earlier stage that owns the
  question (product for requirements, architecture/lld for design), get the decision,
  then resume the blocked agent with the answer.
- **A review finds a defect** → resume the producing stage with a specific, actionable
  finding (file/line references), wait for the fix, then re-run the failed check (often
  by resuming the reviewer's own agent).
- **A review finds a deeper problem** (right implementation, wrong requirement; scope
  conflict) → resume **product** (or **architecture** for design). It decides: revise
  the requirement and docs, document an accepted limitation, or — only if it genuinely
  needs the human — return `needs-human`; you then `mark-needs-human` and park.

## Where a finding this unit will not fix goes

Everything found before merge on this unit is fixed inline by resuming the responsible
stage. Only the residue this unit will not fix goes elsewhere — **on the issue that will
act on it**, in this order:

1. **The open sibling that owns the surface.**
2. **The epic**, when no single child owns it and it must be settled before the epic
   closes.
3. **The standing backlog epic** (labelled `pipeline.labels.standing`), for anything
   real but out of this epic's scope.

When this needs a new issue, the **orchestrator** files it with `create-issue --parent
<owning epic> --type Bug|Task` (`references/operations.md`, "Issue taxonomy").

**Never leave it only in a closing PR's thread or on an issue about to close** — no
sibling will ever see it there.

## Replacing an agent that cannot be resumed

When the owning agent is gone (session crashed, or it was `TaskStop`ped — e.g. a stage
agent that keeps parking on a background job), the replacement **starts from the
existing work, never from zero**. Its dispatch must carry:

- **The existing doc and its status** — e.g. "`lld.md` is at `<sha>` and passed
  `lld-review` clean in round 2; implement it", or "…has one blocking finding, quoted
  below". Rework means addressing the delta, never re-deriving the design.
- **What is already committed on the branch**, by SHA with one line each, and that it is
  good — build on it.
- **The specific finding**, quoted, with file:line references.
- **What earlier rounds settled and must not be re-run.**
- **Why the original agent is gone**, when the cause is a trap the replacement could hit
  too (a stalled install, a suite run exceeding a tool timeout).

Test: it must ask for what a `SendMessage` to the original would — not a fresh assignment.

A rework round is resumed, never re-claimed: `claim`/`start-stage --role development` refuse
a unit with an open PR. Give a replacement the worktree `worktree-add <n>` restores.

## Rework rounds are scoped, not repeated from zero

The resume message must:

- **List what the prior round settled** and say "accept these, do not re-run" (the
  inventory, measurements, fits-vs-deviates call, overlap enumeration — whichever apply).
- **Scope the new pass to the delta**, plus a regression check that the delta could not
  have disturbed what was accepted. Prove it: open with `git diff --stat origin/main`
  showing the change was doc-only.
- **Re-derive fully when the delta is code**, or when it touches the premise an earlier
  conclusion rested on.

## Escalation valve

Each recurring problem gets its own counter per stage pairing (e.g. `arch-review` <->
`architecture`, `pr-review` <-> `development`, `sync-branch-conflict` <->
`development`). Thresholds: `pipeline.escalation.replaceAt` (default 3) and
`needsHumanAt` (default 6); `pairing-counts` echoes both.

| Bounce | Action |
|---|---|
| 1–3 | Resume the pairing's own tracked agent. |
| 3rd unresolved | Do **not** park. Retire the incumbent and dispatch a context-reset replacement (below). The counter does not reset; the replacement owns bounces 4–6. |
| 6th unresolved | `python3 "$SDLC" mark-needs-human <n> --reason "..."` — summarize the repeated pattern **and** what the replacement round changed and did not change. Park the issue, return to Step 1. |

- **One replacement per pairing per unit.** Never respawn a second one at bounce 6.
- **Read counts from `python3 "$SDLC" pairing-counts <issue>` before deciding a bounce is
  the third or sixth.** It computes them from comment markers, so they survive sessions:
  `pr-review` <-> `development` (`pr_review_rework_since_last_clean`, reset by every
  clean review), `sync-branch-conflict` <-> `development` (`sync_conflict_count`), and
  the design pairings `product-review` <-> `product`, `arch-review` <-> `architecture`,
  `lld-review` <-> `lld` (per role under `design_review`). Unmarked pairings (e.g.
  `testing` <-> `development`) are your own session-scoped count.

### Context-reset replacement

The incumbent's own reasoning is the suspect. **Reset the reasoning, not the work** — the
replacement inherits every artefact and discards only the incumbent's rationale. Its
dispatch carries:

- **The requirements** — `product.md` and the acceptance criteria in full.
- **The current document as the thing to revise** — `architecture.md` (or `lld.md` / the
  branch diff) at its current SHA, named as *its* document to edit in place. It revises
  the disputed sections; it does not rewrite the file.
- **The complete review feedback** — every outstanding finding verbatim with citations,
  plus earlier rounds' findings (the recurrence is the signal).
- **The class, in one sentence** — what has recurred across the rounds, to close
  structurally rather than instance by instance.
- **What is settled and out of bounds** — the sections earlier rounds got right, named.
- **Why the incumbent was retired** — so it gives a different reading of the disputed
  area, not a faster round 4.

**Withhold** only the incumbent's rationale for why its answers were right, its
rejected-option reasoning, and its account of the disputed code. The replacement
re-derives that one area from the files.

`TaskStop` the incumbent before dispatching — two agents never hold one worktree.

Test: it asks for a fresh reading of one named area inside an existing document — neither
round 3 continued nor round 1 restarted.

### The counter counts bounces; the thing that actually repeats is a class

Three unrelated defects is healthy review; three instances of one class means fixes land
at instance level — what the context-reset replacement is for.

#### Same-class recurrence must be a marker, not a sentence

- Reviewers pass `--same-class-recurrence` to `record-design-review` /
  `record-pr-review` when this round's blocking finding is the same defect class as an
  earlier round's on this unit. `pairing-counts` reads it back as
  `same_class_recurrence_count`. Never rely on verdict prose for this.
- **Check `same_class_recurrence_count` before every resume decision** on a pairing with
  a nonzero rework count.
- **Any count ≥ 1 escalates immediately**, without waiting for bounce 3 or 6: dispatch
  the context-reset replacement, or — if this pairing's replacement is already spent —
  `mark-needs-human`. Name the class, not the instance, in the replacement prompt or the
  reason.
- Every resume message on such a pairing asks the agent to fix the class at the root
  (e.g. derive the guarded set live so an unknown case fails instead of passing), not
  the listed case.

## Exception — test-only findings may merge-and-file (`pr-review` <-> `development` only)

On what would otherwise be the escalating sixth bounce: if **every** outstanding finding
is a test/verification-only gap (missing/weak coverage, environment limitation, flaky
assertion) and `pr-review` has independently re-verified the production code sound,
file a follow-up (`create-issue --parent <epic> --type Task`, full detail on what's untested and
why), merge normally, and reference the follow-up in the merge comment. **Never** for
correctness, security, data-integrity or unmet-requirement findings — those always
escalate.

## Genuine cross-issue dependency

The one case resuming can't fix: `python3 "$SDLC" mark-blocked <issue> --dep <dep-issue>`
records the native `blockedBy` edge and posts the comment. `next-action` derives
blocked-ness live, so the issue becomes eligible on its own when the blocker closes.
