# Rework, blockers and escalation — the orchestrator's routing

Read when a review returns a finding, a stage reports ambiguity, a bounce may trip the
escalation valve, or you are constructing a resume message for a replacement agent.
Moved out of `references/stage-playbooks.md` on 2026-09-13: every stage agent reads
that file in full, and none of these are a stage agent's decisions. The stage-facing
half stayed there as "Rework and blockers — what it means for you".

(A design-level deviation found by `lld` against a normal epic's approved
`architecture.md` has its own path — `references/epics.md`, "Epic-level deviation
escalation". Everything else below applies to any child of any epic.)

If a later stage (`arch-review`/`lld-review`, `pr-review`) finds a real
problem attributable to an earlier stage, **do not spawn a fresh subagent and do not
open a separate GitHub issue.** The orchestrator resumes the original subagent owning
the responsible stage (via `SendMessage` to its tracked ID) with the specific finding
— that agent still has full working context and only needs to address the delta.

- **A stage agent hits genuine ambiguity mid-work** → it stops and reports the
  specific question in its final message rather than guessing. The orchestrator
  resumes whichever earlier stage owns the question (product for requirements,
  architecture/lld for design), gets the decision, and resumes the blocked agent with
  the answer.
- **A review finds a defect** → resume the stage that produced it with a specific,
  actionable finding (file/line references), wait for the fix, re-run the check that
  failed (often by resuming the reviewing stage's own agent).
- **A review finds a deeper problem** (correct implementation, wrong requirement;
  scope conflict) → resume **product** (or **architecture** for design). That agent
  decides inline: revise the requirement and docs, document an accepted limitation,
  or conclude it genuinely needs the human — only then `mark-needs-human` and park.

**Where a finding this unit will not fix goes.** Reviewers keep rediscovering this, so
it is written down now: **a finding lands on the issue that will act on it**, not in the
thread of the PR that is closing. In order of preference —

1. **The sibling that owns the surface**, if one is open. A `pr-review` finding about a
   config override that silently voids a sibling's planned change, posted on *that
   sibling*, becomes half of the sibling's design instead of a note in a merged PR.
2. **The epic**, when no single child owns it and it must be settled before the epic
   closes.
3. **The standing backlog epic** (the one labelled with the config's
   `pipeline.labels.standing` label) via `create-issue --parent <standing-epic-number>`,
   for anything real but out of this epic's scope — an `lld` is right to refuse to file
   findings against another component into the issue it is designing.

**Never leave it only in a closing PR's thread or in a comment on an issue that is
about to close.** That is a finding deleted: a coverage gap that lives only inside a
review comment on an already-closed issue is invisible to every sibling that needs it,
and survives only if one reviewer happens to notice and carry it over by hand (see
`references/history.md`). This is not the "spin off a ticket instead of fixing it" path
(`SKILL.md`: everything found *before merge on this unit* is still fixed inline by
resuming the responsible stage) — it is where the residue goes once this unit is done
with it.

**When the original agent cannot be resumed, the replacement still starts from the
existing work — never from zero.** The resume-the-owner rule above assumes the owning
agent is alive and holds its context. Sometimes it does not: the session crashed, or the
agent had to be stopped (a stage agent that parks repeatedly waiting on a background job
must be `TaskStop`ped, and its replacement dispatched fresh — see "a subagent cannot wait
across turn boundaries").

A fresh agent has none of that context, and its default behaviour is to do the stage
from the beginning. That is the wrong output twice over: it burns the stage's cost again,
and it can silently discard work that already passed review. Replacement agents have had
to be told by hand what was already committed; nothing in the process required it (see
`references/history.md`).

So a replacement dispatch must carry, explicitly:

- **The doc that already exists and its status** — "`lld.md` is at `<sha>` and passed
  `lld-review` clean in round 2; it is your design, implement it" or "…and has one
  blocking finding, quoted below." Re-entering `lld` for rework means *re-reading the
  approved design and addressing the delta*, never re-deriving it.
- **What is already committed on the branch**, by SHA and one line each, and that it is
  good — the replacement builds on it rather than reworking it.
- **The specific finding**, quoted, with its file:line references.
- **What the earlier rounds settled and must not be re-run**, same inventory the scoped
  review round gets above.
- **Why the original agent is gone**, when the reason is a trap the replacement could
  walk into as well (a stalled install, a suite run that exceeds a tool timeout).

The test for a good replacement prompt: it should be indistinguishable, in what it asks
for, from a `SendMessage` to the original agent. If it reads like a fresh assignment,
the stage will be redone.

**Rework rounds are scoped, not repeated from zero.** A confirming round that
re-derives the whole original review costs roughly 3× the wall clock for no extra
rigour — a measured scoped pass ran in under a third of the original round's time and
still found a blocking issue. The resume message should:

- **Enumerate what the prior round settled** and say plainly "accept these, do not
  re-run" — the inventory, the measurements, the fits-vs-deviates call, the overlap
  enumeration, whichever apply.
- **Scope the new pass to the delta**, plus a regression check that the delta could not
  have disturbed what was accepted. Prove it, don't assume it: a good scoped round
  opens with `git diff --stat origin/main` showing the change was doc-only.
- **Re-derive fully when the delta is code**, or when it touches the premise an earlier
  conclusion rested on. A doc-only delta cannot move a suite result; a code delta can.

**Escalation valve, per stage pairing**: each recurring problem gets its own counter
(e.g. `arch-review` <-> `architecture`, `pr-review` <-> `development`,
`sync-branch-conflict` <-> `development`). The valve has **two stages and a ceiling** —
`pipeline.escalation.replaceAt` bounces with the incumbent agent (default 3), then a
replacement up to `needsHumanAt` (default 6); `pairing-counts` echoes both:

- **Bounces 1–3** — resume the pairing's own tracked agent, as always.
- **At the third bounce without resolution** — do *not* park. **Retire the incumbent
  and dispatch a replacement agent** for that stage (see "Context-reset replacement"
  below). The counter does not reset; the replacement owns bounces 4–6.
- **At the sixth bounce without resolution** — stop looping: `sdlc_next.py
  mark-needs-human <n> --reason "..."` summarizing the repeated pattern *and* naming
  what the replacement round changed and did not change, park the issue, return to
  Step 1.

**One replacement per pairing per unit.** The reset is a one-shot instrument, not a
loop — a second respawn at bounce 6 is the same intervention that already failed, and
the failure is then evidence the problem is not context rot.

For the two marker-backed pairings — `pr-review <-> development` and
`sync-branch-conflict <-> development` — `sdlc_next.py pairing-counts <issue>`
computes the strike counts mechanically from the issue's own comment markers
(`rework_since_last_clean` resets on every clean review), so those counters survive
session boundaries; consult it before deciding a bounce is the third or the sixth. The
unmarked pairings stay the orchestrator's own session-scoped count.

**Context-reset replacement — how bounce 4 differs from bounce 2.** The generic
replacement rule above ("the replacement still starts from the existing work — never
from zero") exists for an agent that *died*: continuity is the goal, because its
context was good. This one is the opposite case. The incumbent is being retired
precisely *because* its context is the suspect — three rounds of its own reasoning are
now sitting in its history, and each round has been anchoring the next on conclusions
that keep turning out wrong in the same narrow area.

**What resets is the reasoning, not the work.** This is a context reset, never a restart
from zero: a replacement that re-opens settled ground re-runs the whole stage at full
cost and hands `arch-review` a brand-new document to review from scratch — which is how
a stuck pairing becomes an unbounded one. The replacement **inherits every artefact and
discards only the incumbent's rationale**. Its dispatch carries:

- **The requirements** — `product.md`, and the acceptance criteria in full. The
  replacement is solving the same problem, not re-scoping it.
- **The current document as the thing to revise** — `architecture.md` (or `lld.md` /
  the branch diff) at its current SHA, named as *its* document to edit in place. It
  revises the disputed sections; it does not rewrite the file.
- **The review feedback, complete** — every outstanding finding verbatim with its
  citations, and the earlier rounds' findings too, since the recurrence across rounds
  is the actual signal.
- **The class, in one sentence** — what has recurred across all three rounds, as the
  thing to close structurally rather than instance by instance.
- **What is settled and out of bounds** — the sections earlier rounds got right, named
  explicitly. Those are not reopened.
- **Why the incumbent was retired**, so the replacement knows it is being asked for a
  different reading of the disputed area, not a faster round 4.

What is **withheld** is narrow and deliberate: the incumbent's rationale for why its
answers were right, its rejected-option reasoning, and its account of the disputed code.
The replacement re-derives *that one area* from the files themselves — that
re-derivation is the entire point of the swap, and inheriting the frame defeats it.

`TaskStop` the incumbent before dispatching, so two agents never hold the same worktree.

The test for a good context-reset prompt: it asks for a fresh reading of one named area
inside an existing document, and a reader could not mistake it for a fresh assignment.
If it reads like round 3 continuing, it produces round 3's answer again; if it reads
like round 1, the loop never terminates.

**The counter counts bounces; the thing that actually repeats is a *class*.** Three
bounces on unrelated defects is a healthy review. Three bounces on three instances of
one class means every fix is landing at instance level, and the counter cannot tell the
difference — it will trip on the third instance on a unit whose real remedy is one
structural change. That is exactly what the context-reset replacement is for, and why
the third bounce swaps the agent instead of parking the unit. So:

- **A reviewer whose new finding is another instance of a class already bounced says
  so, in the verdict** — "REWORK for one new blocking finding *of the same
  silent-skip class*" — and, better, names the escalation shape in advance: "if a third
  round produces another, that is an escalation candidate on the pattern rather than a
  routine bounce".
- **The orchestrator's resume message then asks for the class, not the case.** Units
  handled that way have settled within one round: a `development` that restructures so
  the guarded set is derived live and an unknown case fails instead of passing ("fixes
  the bounced class at the root, not at the symptom"); an `lld` that deletes a citation
  which is correct *today* because its shape rots, and sweeps the next instance before
  it can exist (see `references/history.md`).
- **A same-class third bounce triggers the context-reset replacement even though the
  instance is new** — the class, not the instance, is what the replacement is told to
  close. A same-class *sixth* bounce is the escalation, and the `mark-needs-human`
  reason names the class, not the last instance.

**Exception — `pr-review` <-> `development` only: test-only findings may
merge-and-file.** On what would otherwise be the escalating sixth bounce: if every
outstanding finding is a **test/verification-only gap** (missing/weak coverage, an
environment limitation, a flaky assertion) and **not** a defect in shipped production
code (`pr-review` must have independently re-verified the code sound), then file a
follow-up issue (`create-issue --parent <epic>`, full detail on what's untested and
why) and merge normally, referencing the follow-up in the merge comment. **Never
applies** to correctness, security, data-integrity, or unmet-requirement findings —
those always escalate.

**Genuine cross-issue dependency** — the one case resuming can't fix:
`sdlc_next.py mark-blocked <issue> --dep <dep-issue>` records the native `blockedBy`
relationship and posts the comment. Nothing else to maintain: `next-action` derives
blocked-ness live from the relationship, so the issue becomes eligible on its own the
moment the blocker closes.

