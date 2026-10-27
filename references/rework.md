# Rework, blockers and escalation — the orchestrator's routing

A deviation from an Epic's approved `architecture.md` has its own path
(`references/epics.md`, "Architecture deviation escalation"). All other rework is here.

## Routing a finding

Print the blocking findings in chat before you route them. When a later stage finds a
problem an earlier stage owns, resume the owning agent (`SendMessage` to its tracked ID) with
the finding. Never spawn a fresh agent. Never open a GitHub issue for it.

- **Genuine ambiguity** → resume the stage that owns the question (`product` for requirements,
  `architecture`/`lld` for design). Then resume the blocked agent with the answer.
- **A defect** → resume the producing stage with the finding and `file:line`. Then re-run the
  failed check, usually by resuming the reviewer.
- **A deeper problem** (wrong requirement, scope conflict) → resume `product` (or
  `architecture`). It revises the docs, records an accepted limitation, or returns
  `needs-human`; then `mark-needs-human` and park.
- **`done-with-concerns`** from `development` → `transition` as for `done`. Put the concern
  line first in the `pr-review` brief: `Concern from development: <line>. Check it first.`

## Where a finding this unit will not fix goes

Fix everything found before merge inline. Put the residue on the issue that will act on it,
in this order: the open sibling that owns the surface; the Epic, when it must be settled
before close; the standing backlog Epic, for out-of-scope work. You file a new issue with
`create-issue --parent <epic> --type Bug|Task`. Never leave it only on a closing PR or an
issue about to close.

## Replacing an agent that cannot be resumed

The replacement starts from the existing work. Its dispatch carries:

- the existing doc and its status ("`lld.md` at `<sha>` passed `lld-review` in round 2");
- the commits on the branch, by SHA, one line each;
- the open finding, quoted, with `file:line`;
- what earlier rounds settled and must not be re-run;
- why the original agent is gone, when the cause is a trap the replacement could hit.

A rework round is resumed, never re-claimed. Give a replacement the worktree that
`worktree-add <n>` restores.

## Scope each rework round

The resume message lists what the prior round settled ("accept these, do not re-run") and
scopes the pass to the delta plus a regression check. Prove a doc-only delta with
`git diff --stat origin/<base>`. Re-derive fully when the delta is code or touches a premise.

## Escalation valve

Each pairing (`arch-review` ↔ `architecture`, `pr-review` ↔ `development`,
`sync-branch-conflict` ↔ `development`, …) has its own counter. Thresholds:
`pipeline.escalation.replaceAt` (default 3) and `needsHumanAt` (default 6).

| Bounce | Action |
|---|---|
| Below `replaceAt` | Resume the pairing's tracked agent. |
| At `replaceAt`, unresolved | Retire the incumbent. Dispatch a context-reset replacement. The counter does not reset. |
| Between the two | Resume the replacement. |
| At `needsHumanAt`, unresolved | `mark-needs-human <n> --reason "..."` with the repeated pattern and what the replacement changed. Park. With `humanChannel: "session"`, ask first (`references/cloud-mode.md`, "Human channel"). |

- One replacement per pairing per unit.
- Read counts from `python3 "$SDLC" pairing-counts <issue>` before you call a bounce the third
  or sixth. An unmarked pairing (gate feedback rounds) is your session count.

### Context-reset replacement

Reset the reasoning, not the work. The dispatch carries: the requirements in full; the current
doc or diff at its SHA, to revise in place; every outstanding and earlier finding verbatim;
the recurring class in one sentence; the settled sections, named; why the incumbent was
retired. Withhold the incumbent's rationale and its account of the disputed code. `TaskStop`
the incumbent first: two agents never hold one worktree.

### Same-class recurrence

- Reviewers pass `--same-class-recurrence` to `record-design-review` / `record-pr-review`.
  `pairing-counts` returns `same_class_recurrence_count`.
- Check it before each resume on a pairing with rework. A count ≥ 1 escalates at once: the
  replacement, or `mark-needs-human` when the replacement is spent. Name the class.
- An `escalation` field in a `record-*` result (`recommend: replace|needs-human`) decides.
- A repeat flagged only in prose: on its first round, resume with a fix-the-class instruction;
  flagged again next round, escalate as if the count were 1.

## Exception — test-only findings may merge and file (`pr-review` ↔ `development` only)

At the sixth bounce, when every open finding is a test or verification gap and `pr-review`
re-verified the production code: file a follow-up Task (`create-issue --parent <epic> --type
Task`), merge, and link the follow-up. Never for correctness, security, data-integrity or
unmet-requirement findings.

## Genuine cross-issue dependency

`python3 "$SDLC" mark-blocked <issue> --dep <dep-issue>`. The issue becomes eligible when the
blocker closes.
