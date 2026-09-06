# Stage playbooks — docs, comments, rework, and per-stage exit actions

Referenced from `SKILL.md` (Step 3). **This is the one file a stage subagent is told
to Read before doing anything else** — it holds the per-issue doc set, the altitude
rules, the commenting discipline, the rework model, and every stage's exit actions.
(A stage agent's prompt also spells out, verbatim, its own doc path and worktree —
those are issue-specific and not in this file.)

## Per-issue docs (the source of record)

Each child issue gets a folder `docs/sdlc/issue-<n>/`, committed on the
`issue-<n>` branch and merged into `main` with the eventual squash-merge. (A normal
epic's own phase writes into `docs/sdlc/epic-<n>/` instead — see
`references/epics.md`, "Doc layout at the epic level".) Up to three predefined files:

| File | Written by | Required? |
|---|---|---|
| `product.md` | `product` (standing-epic children only) | Required for a standing-epic child, except a bug fast-track where `architecture` determined no product input was needed. **Never written for a normal-epic child** — that work happened in the epic's own `product.md`. |
| `architecture.md` (standing-epic child) or `lld.md` (normal-epic child) | `architecture` or `lld` stage | Always — even a child needing no design decisions beyond the epic's `architecture.md` gets a short `lld.md` saying so, for structural consistency |
| `development.md` | `development` | Always |

`testing`, `arch-review`/`lld-review` and `pr-review` get **no** doc file. The two
reviews are point-in-time passes; `testing`'s output is a **structured handoff
comment** (see its exit action below) rather than a file, deliberately — a file of
self-reported pass/fail numbers is exactly what `pr-review` is told not to trust, so
it was a file written to be distrusted. Findings live in the issue comment thread
(and in whichever doc a resumed stage updates).

Each doc must be **detailed enough that the next stage's agent works from it
independently**, without reconstructing context from the comment history. A doc may
double as the stage's working/scratch space (e.g. an internal checklist inside
`development.md`), but the filenames above are canonical — no alternate names.

A normal-epic child's `lld.md` and every child's design doc must carry a `## Footprint`
section in the exact parseable shape defined in `references/epics.md`, "How to size
the children" — backticked paths, one per bullet.

## Document altitude — two different documents, two different contracts

`product.md` and `architecture.md` (epic-level, or issue-level for a standing child)
are both read by a **human** at a gate, but they are not the same kind of document and
do not follow the same rules. `development.md` and `lld.md` have **no** human gate and
can stay as technical as the work demands; `lld.md` has no altitude requirement at all.

Start both from the matching skeleton in `docs/sdlc/_templates/` — copy it in,
fill the sections in order, delete the template's instructional HTML comments.

### `product.md` is an IRD — requirements only

It follows the house style of your repo's requirements docs; **read IRD-002
before writing one**, it is the worked example. Section order comes from
`product.template.md`: Background, Goals, Functional Scope, User Experience,
Non-Functional Requirements, Constraints, Acceptance Criteria, Out of Scope, Open
Questions, Decisions Log. A section with nothing in it is dropped rather than kept as
`N/A` filler — the one exception is Open Questions, which stays as "None".

Four rules, each of which a real document has broken:

- **Nothing about the pipeline appears in the document.** No stage names, no gate
  references, no field names, no "this document creates no child issues", no note on
  what this stage did or did not do, no link to this skill. A reader must not be able
  to tell from the prose that an automated pipeline produced it. Process belongs in the
  handoff comment, which is where a pipeline reader is already looking.
- **Requirements state observable behaviour; they never choose the technology.**
  "Alert channels can be added or removed by configuration, with no application
  change" is a requirement. "Alerts go out through GCP Cloud Monitoring email
  notification channels" is an architecture decision wearing a requirement's clothes,
  and it forecloses the options the architecture stage exists to weigh. Name the
  capability, the observable behaviour, and the bound; leave platform, vendor, service
  and mechanism to `architecture.md`. A genuine constraint the business or the operator
  has already fixed goes under **Constraints**, written as the constraint itself ("must
  run with no persistent agent host"), not as the product that satisfies it.
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
  `docs/sdlc/<unit>/decision-<slug>.md`, linked in one line. (This is the one
  sanctioned split; design content still never goes to `your repo's architecture docs`.)

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
  `testing` maps criteria to tests from the list itself.
- **Data model and Failure modes are conditional** — present only when entities or
  invariants actually change, or when the design introduces a genuinely new way for
  production to break. Do not draw the existing model.
- **`Footprint` and `Implementation notes` appear only in a standing-epic child's
  doc.** That doc is the only design doc `development` ever gets, and the only
  `architecture.md` `parse_footprint` is ever pointed at. **Both are omitted entirely
  from an epic-level doc**: per-child footprints live in each child's `lld.md`, and
  implementation depth is that `lld`'s job — putting it in the epic doc duplicates it
  at the wrong altitude, which is how epic #98's doc reached 20k words.

See "Review altitude" under `arch-review` below for how this shapes review findings.

## Citation discipline — every stage, not just `testing`

Every stage in this pipeline cites the codebase and the docs, and **every stage has
shipped a wrong citation.** In one cycle (epic #110, 2026-08-20): `lld` cited two
epic-doc line numbers that an addendum had shifted 19 lines; `development` cited
`account.spec.ts:438` for a test at `:448`; and `pr-review` raised a blocking finding
against `origin/epic-110:architecture.md:87-99` after resolving the branch from a SHA
in an old comment link — an **unreachable draft commit**, superseded an hour after it
was written. `development` had to disprove that one twice before it was withdrawn.

The rules that came out of it:

- **Anchor to something stable, not to a line number.** Cite a section heading
  (`"### Shared files"`), an exact quoted test title, or a grep-anchored quote. Line
  numbers drift the moment anyone inserts a paragraph above them; headings and titles
  survive. Where a line number genuinely helps a reader, treat it as a hint alongside
  the durable anchor, never as the anchor itself.
- **Never cite a file you did not open in this session.** Not from the issue body, not
  from a prior stage's doc, not from memory. The body's line numbers are usually stale
  by the time you read them — several of the above came from exactly that.
- **When resolving a doc from a branch, `git fetch origin` first, then verify the ref
  is reachable**: `git merge-base --is-ancestor <sha> origin/<branch>`. A SHA that no
  branch reaches is the tell that you are reading a superseded draft. Pin *which* ref
  you resolved and how, so a reviewer can reproduce it.
- **To check what a commit changed, use `git show <sha> --stat` or three-dot
  `git diff origin/main...<branch>` — never a two-dot range.** A two-dot range that
  spans a merge of `origin/main` attributes every merged commit to the agent:
  `git diff 3fee947 b3a518e --stat` reported 4 files where #245's agent reported 1, and
  a "you touched files you shouldn't have" correction was nearly sent on that basis.
  The agent was right; the check was wrong.
- **A stale citation in a doc that is about to merge is a real finding**, not a nit —
  it merges into `main` as a record that actively misleads the next reader. That was
  the blocking finding on #209's `development.md`, which still documented a locator the
  `testing` stage had empirically disproved.
- **A citation can be correct when written and wrong when merged — nothing re-checks
  it after a merge.** #274 shipped six stale line numbers, and **four of them were
  broken by that PR's own merge commit**, which pulled sibling #284's script rewrites
  onto the branch after the docs were authored. One was operator-facing and reached
  `main`. Every stage had verified them honestly; every one drifted afterwards.

  So, concretely: **in a document that ships — a runbook, an HLD, anything under
  your repo's doc/ops trees — prefer the grep-anchored quote alone and drop
  the line number.** A quote survives a sibling's refactor; a number does not, and
  correcting numbers just resets a clock that the next `scripts/**` edit restarts.
  Keep line numbers where they are genuinely a working aid — a stage's own evidence
  log, a review comment — and treat them there as hints, per the first rule above.

  For the same reason, **when `sync-branch` pulls a sibling's work onto a branch whose
  docs cite that sibling's files, re-check those citations before the PR merges.** It
  is the one moment the pipeline creates staleness by itself rather than inheriting it.

## Compile-checking is not verification

`playwright test --list`, `tsc --noEmit`, and a lint pass tell you the code *parses*.
They cannot tell you a test **ran**, and a skipped test is indistinguishable from a
passing one in a `--list` output.

This is not hypothetical: #209's two new E2E tests shipped with a locator
(`li p.font-semibold`) that also matched `OrderStatusTracker`'s always-rendered step
labels and yielded `NaN`. `development`'s `--list` check was clean. The tests skip
under `make e2e` (a pre-existing credential gap), so they would have merged broken and
silently stayed broken. `testing` caught it only by executing them for real.

So: **no stage may report a test as covering an acceptance criterion unless it observed
that test execute.** If it could not run, say so plainly, name the reason, and say
which criterion is therefore unproven — `pr-review` can then weigh a known gap instead
of trusting a coverage claim that was never true.

## Establish a number by running the thing, not by modelling it

The dominant defect class of epic #156 — at least eighteen confidently-stated, wrong
measurements across six children and the epic's own architecture — had one shape every
time: **a number produced by a grep that models a rule, rather than by running the
rule.** Every blocking finding in that epic was found by building the change and
running the real tool; none was found by reading. Three rules, each from a real
incident:

- **Run the rule, don't regex-model it.** #236's `lld` counted violations with a
  counting grep carrying a `*.module` exemption that the shipped `eslint.config.mjs`
  did not have — the exemption existed only in the grep and in the AC prose. Applied as
  designed it would have banned **8 legitimate Nest DI imports** and shipped a CI-red
  PR, and the obvious late fix would have invalidated the barrel cycle analysis the
  same doc rested on. The same applies to prose rules: #245's `lld` shipped a criterion
  whose meaning lived only in the design doc, which — applied as written — wrongly
  exempted three real files. **If the artifact is executable (a lint config, a script,
  a test, a build), apply the change in a scratch copy and run it. Report that output.**
- **Scope the search to the whole tree, not to the module you are thinking about.**
  `src/database/database.module.ts` was missed **twice**, by #235 and #236, for the
  identical reason: a grep scoped to `src/modules/` in a repo whose composition file
  lives in `src/database/`. #236's version would have broken `npx eslint` on `main`'s
  own composition file. Anchor the pattern to the *symbol*, not to a path prefix you
  assume: `from '[^']*sellers/` found what `from '(\.\./)+sellers/` could not.
- **Prove the detector detects before reporting an absence.** A "0 cycles", "0
  violations", "no diff" result is worth exactly as much as the demonstration that the
  check *can* go non-zero. The move used repeatedly and well on #236 is the **positive
  control**: flip `ProductVariant` to route through the barrel, watch a new 16-file SCC
  appear, revert, watch it go. Four separate agents ran that control on the same claim,
  which is why it is trustworthy.

**Vacuous-pass tells** — a check that passed because it examined nothing. All four were
hit live on #156:

- A **stale `tsbuildinfo`** made `tsc` emit 92 files instead of 533 and **exit 0**. Two
  different agents hit it. Clear build state and assert the emit count before using it.
- A graph built with **too narrow an edge pattern** gave the barrels zero outgoing edges
  and a vacuous "0 cycles" — indistinguishable from a real clean result. Assert
  non-vacuity (edge count, node count) before reading a graph result.
- **Structurally skipped tests.** `frontend/Makefile` never forwarded
  `E2E_SELLER_*`, so ~35 seller/data-entry tests skipped on *every run this repo has
  ever done*, and a before/after comparison "matched" over them. **A skip is not a
  cover** — count executed tests, not listed ones.
- **A mutation that discriminates nothing.** #236's `testing` mutated a permission both
  roles already held, stayed green, and correctly called that *a bad discriminator, not
  a finding about the test*. If a mutation stays green, first ask whether it was a real
  mutation.

**Your own tooling gets the same scrutiny as any other claim.** Two agents on #236
caught false positives in scripts they had just written, before treating the output as
evidence, and said so in the handoff. That is the bar.

## Commenting discipline

The main agent never needs to *read* a comment to decide what happens next — it just
ran the previous stage. Comments are the **visibility record** for a human and the
**resume point** if a session dies. Leaner than the docs — a pointer and a summary:

- **Start comment is mandatory.** `🚧 Picking this up — <role> stage starting.` —
  posted before delegating, every time. When the orchestrator posts it itself
  (`arch-review`, `lld-review`, `pr-review`, and `testing` right after `open-dev-pr`):
  `sdlc_next.py start-comment <n> --role <role>` — never hand-typed. (For roles
  claimed via `claim`/`pass-gate` the comment is already posted by that call — don't
  post twice.)
- **Comment per milestone, not per step.** Batch a stage's sub-steps into one comment
  at a coherent unit of progress.
- **Floor: one comment per stage** (the handoff/exit comment). **Ceiling: one per
  genuine milestone.**
- **Link to the doc, don't paste it** — a few sentences plus doc path + commit SHA.
- **The last comment of a stage must carry real information**, not just a marker.
- **Enough to resume from a crash**: if a fresh session couldn't tell from comments
  plus docs where a crashed run left off, there weren't enough.

Each handoff comment ends with the hidden marker
`<!-- stage-transition: <from-role>-><to-role> @ <ISO8601> -->` — posted by the
script wherever a script command owns the transition (`open-dev-pr`,
`handoff-to-pr-review`, `open-gate`, `pass-gate`, `skip-gate`).

**Issue comments are status updates, not the source of record** — the real detail
lives in committed artifacts (the doc files, commits, the issue body for durable
requirements). Pipeline tooling (`.claude/skills/`, `_bmad/`) is tracked in the repo:
when a stage touches it, commit that change with the related code.

## Rework and blockers — resume the responsible stage's agent

(A design-level deviation found by `lld` against a normal epic's approved
`architecture.md` has its own path — `references/epics.md`, "Epic-level deviation
escalation". Everything else below applies to any child of any epic.)

If a later stage (`arch-review`/`lld-review`, `testing`, `pr-review`) finds a real
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

1. **The sibling that owns the surface**, if one is open. #235's `pr-review` found that
   `offer.entity.ts`'s eslint override silently voids any pattern added to the base
   block; posting it on **#236** made it half of #236's design instead of a note in a
   merged PR.
2. **The epic**, when no single child owns it and it must be settled before the epic
   closes.
3. **The standing backlog epic** via `create-issue --parent <standing-epic-number>`, for anything real
   but out of this epic's scope. #250 came out of #238's `lld` correctly refusing to
   file three backend IT/unit findings into an e2e issue.

**Never leave it only in a closing PR's thread or in a comment on an issue that is
about to close.** That is a finding deleted: the
`scripts/check-controller-protection.mjs` coverage gap lived only inside #243's review
comment on an already-closed issue, where #236–#238 would never have seen it, and
survived only because one reviewer noticed and carried it to all three by hand. This is
not the "spin off a ticket instead of fixing it" path (`SKILL.md`: everything found
*before merge on this unit* is still fixed inline by resuming the responsible stage) —
it is where the residue goes once this unit is done with it.

**When the original agent cannot be resumed, the replacement still starts from the
existing work — never from zero.** The resume-the-owner rule above assumes the owning
agent is alive and holds its context. Sometimes it does not: the session crashed, or the
agent had to be stopped (a stage agent that parks repeatedly waiting on a background job
must be `TaskStop`ped, and its replacement dispatched fresh — see "a subagent cannot wait
across turn boundaries").

A fresh agent has none of that context, and its default behaviour is to do the stage
from the beginning. That is the wrong output twice over: it burns the stage's cost again,
and it can silently discard work that already passed review. On #238 and #251 the
replacement agents had to be told by hand what was already committed; nothing in the
process required it.

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
rigour — one measured pass took 277s against the original's 913s, and still found a
blocking issue. The resume message should:

- **Enumerate what the prior round settled** and say plainly "accept these, do not
  re-run" — the inventory, the measurements, the fits-vs-deviates call, the overlap
  enumeration, whichever apply.
- **Scope the new pass to the delta**, plus a regression check that the delta could not
  have disturbed what was accepted. Prove it, don't assume it: every good scoped round
  on #156 opened with `git diff --stat origin/main` showing the change was doc-only.
- **Re-derive fully when the delta is code**, or when it touches the premise an earlier
  conclusion rested on. A doc-only delta cannot move a suite result; a code delta can.

**Escalation valve, per stage pairing**: each recurring problem gets its own counter
(e.g. `arch-review` <-> `architecture`, `testing`/`pr-review` <-> `development`,
`sync-branch-conflict` <-> `development`). The valve has **two stages and a ceiling of
six bounces — 3 with the incumbent agent, then 3 with a replacement**:

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
  so, in the verdict.** #234's round 2 did (*"REWORK for one new blocking finding **of
  the same silent-skip class**"*); #243's round 2 went further and named the escalation
  shape in advance (*"If a third round produces another, that is an escalation candidate
  on the pattern … rather than a routine bounce"*).
- **The orchestrator's resume message then asks for the class, not the case.** Both
  units settled within one round of that: #234's `development` restructured so the route
  set is derived live and an unknown decorator fails instead of passing — *"fixes the
  bounced class at the root, not at the symptom"*; #243's `lld` deleted a citation that
  was correct *today* because the shape rots, and swept a fourth instance before it
  could exist.
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

## Stage-specific exit actions

The stage agent performs these itself as its last step — **except** opening a
human-review gate, which the orchestrator does after the agent returns (see
`references/gates.md`). Exit actions update the same issue's fields in place — never
a new issue for a normal handoff.

- **`product` done, `unit: "epic"`** — In the epic's own worktree
  (`/tmp/sdlc-epic-<n>`), on `epic-<n>` created from `main` (first stage to
  touch it). Write `docs/sdlc/epic-<n>/product.md` as an IRD covering the whole
  epic, per Document altitude — organised by **functional area, never by child issue**.
  Children are the architecture stage's output; a product document that names them is
  either pre-empting that decomposition or reporting on it, and the requirement is the
  same whoever ends up implementing it. Commit, push. Update the epic's body with a
  pointer + brief summary. Set the epic's Effort, and each child's if the epic already
  has children. Then the orchestrator opens Gate A (`open-gate ... --doc product.md --next-stage
  architecture --unit epic`) — never set the Stage field to `Architecture` directly.

- **`product` done, `unit: "issue"` (standing-epic child)** — Create `issue-<n>` from
  `main`. Write `issue-<n>/product.md` as an IRD with full requirements and acceptance
  criteria, per Document altitude. Commit, push. Update the issue body with a pointer
  + summary. Set Effort (if `High`, strongly consider splitting via `create-issue`).
  Priority normally lives on the epic; set it on the child only to jump the sibling
  queue. Then Gate A opens — never set Stage to `Architecture` directly.

- **`architecture` done, `unit: "epic"`** — Continue on `epic-<n>` in the epic's
  worktree. Write
  `epic-<n>/architecture.md` per Document altitude: one design subsection per child
  plus shared decisions; create/split/merge/modify children here as needed (see
  `references/epics.md`). Commit, push, short handoff comment linking the doc. **Do
  not change the Stage field** — stays `Architecture` while `arch-review` runs.

- **`architecture` done, `unit: "issue"` (standing-epic child)** — Continue on
  `issue-<n>` (or create it, for a bug fast-track). For a fast-track bug, first make
  the explicit product-input call (`references/epics.md`, "Bug fast-track"). Write
  `issue-<n>/architecture.md` per Document altitude — if there are genuinely no
  decisions beyond `product.md`, say so in a short version, but still write it.
  Commit, push, short handoff comment. **Do not change the Stage field** — stays
  `Architecture` while `arch-review` runs.

- **`arch-review`** — Orchestrator posts `start-comment <n> --role arch-review`, then
  spawns a fresh Opus subagent of type `sdlc-design-review` reviewing
  `architecture.md` (and `product.md`) against the real codebase. Instruct it to end
  its handoff comment with `<!-- arch-review-confidence: N -->` (0-100; only
  meaningful on a clean verdict; report low confidence if it found anything).

  **Review altitude — structural soundness, not implementation pre-specification.**
  Check component boundaries, data/control flow, security and permission logic, major
  tradeoffs — what's expensive to get wrong and hard for `development` to catch. Do
  **not** hunt for exhaustive file/line enumerations — verifying the doc's own claimed
  references is fine when that's how you'd check a boundary or security claim;
  manufacturing new ones is `development`'s job. A finding that would only add
  mechanical detail isn't a finding. If something genuinely can't be verified sound
  without that detail, it's fair game — the bar is "does this matter to soundness".

  Three sections are load-bearing for this review and worth reading first: **Scope**
  (out-of-scope entries are the declared non-goals — a finding against one is a scope
  dispute for the human, not a review finding), **Design** (the bet/fallback pair under
  each major decision — an unstated bet is the finding, and so is a design whose
  diagram shows boxes rather than the mechanism), and **Non-functional envelope** (an
  adjective where a measurable scenario belongs is a finding, because nothing
  downstream can ever prove it met). A collapsed `N/A — <reason>` on a conditional
  section is only a finding when the trigger demonstrably *did* fire.

  **Length is itself reviewable.** This document is an HLD: prose that would not change
  an `lld`, an explanation of what already exists, or implementation depth that belongs
  in a child's `lld.md` are all findings — not stylistic notes. Equally, a *missing*
  decision hidden by brevity is the more serious finding; brevity is not the goal,
  altitude is.
  For an epic-level review, also check across children: overlapping or conflicting
  subsection scope is a structural finding (the exact #99/#107 pattern).

  - **Clean, confidence > threshold** → `skip-gate` per `references/gates.md` ("Gate B
    confidence skip") — issue: continue straight into `development`; epic: epic
    becomes `epic:architected`, continue into Step 1. Don't park.
  - **Clean, confidence <= threshold or missing** → orchestrator opens Gate B, parks
    the unit. Never set Stage to `Development` directly.
  - **Fixable design issue** → resume the `architecture` agent with the finding;
    re-verify after. Counts toward the pairing's valve.
  - **Deeper/requirements-level problem** → resume the `product` agent instead.

- **`lld` (normal-epic child) done** — In the child's worktree on `issue-<n>` (create
  if first stage). Read the epic's `docs/sdlc/epic-<parent>/architecture.md`
  as the design source of truth; the **first move** is the fits-vs-deviates call
  (`references/epics.md`, "Epic-level deviation escalation"). If it fits → write
  `issue-<n>/lld.md` (no altitude requirement): exact files/functions to touch, how it
  maps to the epic design, task-local decisions — **including the parseable
  `## Footprint` section**. Commit, push, short handoff comment. **Do not change the
  Stage field** — stays `LLD` while `lld-review` runs. If it doesn't fit → don't write
  `lld.md`; follow the deviation escalation.

- **`lld-review`** — Orchestrator posts `start-comment <n> --role lld-review`, then
  spawns a fresh Opus subagent exactly as `arch-review` (same Review altitude, same
  confidence-marker
  instruction), reviewing `lld.md` against the epic's `architecture.md` and the real
  codebase — including footprint parseability and overlap vs active siblings.

  - **Clean (any confidence)** → **no gate, ever** — `lld-review` is the only review
    a normal-epic child's design gets, deliberately. After `record-design-review`,
    publish the design to the epic branch:
    `sdlc_next.py merge-lld-doc <n>` — commits this child's `lld.md` onto
    `epic-<parent>` and pushes it, so the low-level design is durable there independent
    of the child branch and siblings pick it up on their next `sync-branch` (better
    cross-child overlap checks). Scoped no-op for a standing-epic child or a parentless
    issue; a structured conflict result (never a crash) if the epic branch moved under
    it. Then claim `development` directly:
    `sdlc_next.py claim <n> --role development` (never `skip-gate`/`open-gate` here).
    Continue immediately.
  - **Fixable task-level issue** → resume the `lld` agent; re-verify. Valve pairing
    `lld-review` <-> `lld`.
  - **Doesn't fit the epic's design after all** → deviation escalation, as if `lld`
    itself had found it.

  **Every design review — `arch-review` and `lld-review`, clean or not — ends with
  `record-design-review`**, before the orchestrator resumes the design agent or moves
  the unit on:

  ```bash
  sdlc_next.py record-design-review <n> --role lld-review --outcome clean|rework \
      --summary "..." [--unit epic]
  ```

  This is the design-side twin of `record-pr-review`, and it exists because the valve
  could not see the pairing that fires it most. On epic #98 the `lld-review` <-> `lld`
  pairing ran roughly nine tenths of the epic's review rounds and tripped the
  context-reset replacement on three of four children, while `pairing-counts` tracked
  only `pr-review` and sync conflicts — so the strike count lived in one
  orchestrator's head and any crashed session, or any continuous-mode agent, would
  have resumed it at zero. `pairing-counts` now reports these per role under
  `design_review`; read it before deciding whether a bounce is routine.

  **The confidence marker does not substitute for this.** `skip-gate` reads
  confidence, which answers "how far do I trust a *clean* verdict" — it is meaningless
  on a rework verdict, which is exactly the verdict a valve counts. Post both.

  **State your axis coverage in the handoff.** A design review that fans out to
  parallel axes must say, in its own comment, **how many it launched and how many had
  returned when it formed the verdict** — and if it dropped one, which. On #274 a
  review posted CLEAN with **zero of three axes returned**; the two that landed
  afterwards carried the worst defect of that round, and the issue came within one
  orchestrator decision of moving to `development` on it. The reviewer disclosed it
  itself, which is the only reason it was caught. Treat an unqualified claim of
  coverage as unverified: ask for the count.

- **`development` done** — Implement with TDD per repo conventions, in the child's
  worktree on `issue-<n>`, in small logical **local** commits. Write
  `issue-<n>/development.md` (what was built, how it maps to the design doc, commit
  list, how to verify) and commit it alongside the code.

  **Push discipline — batch, don't push per commit (operator directive, 2026-09-04).**
  Commit locally as often as is natural, but **push once per stage cycle**, not after
  each commit. A push is what a reviewer/CI acts on and, on any `pull_request`-triggered
  workflow, what spins a runner — so N pushes in one cycle is N× the wasted signal for
  the same delivered work. Concretely:
  - **Do not** `git push` after each local commit. Let the commits accumulate on the
    local `issue-<n>` branch during the cycle.
  - Push **once**, immediately before `open-dev-pr`, so the branch the PR opens against
    already carries the whole cycle.
  - On a **rework** round, same rule: make all the fix commits locally, then push
    **once** before re-handing off. One push per round, not one per fix.
  - A mid-cycle push is justified only to hand work off to a human or to unblock a
    genuinely blocked teammate — not as routine "push as you go". If in doubt, hold the
    push until the end of the cycle.

  Open the draft PR via
  `sdlc_next.py open-dev-pr <n> --title "..." --body "..." --summary "..."` — appends
  `Closes #<n>`, sets Stage to `Testing`, posts the handoff comment. Do not mark it
  ready or merge it yourself. If a push is ever rejected, stop and report — don't work
  around it. **On a blocker** (ambiguous requirement, missing design decision): stop
  and report the specific question in your final message — never create issues or
  change fields yourself; the orchestrator resumes the right earlier stage.

  **Three completion gates — all three before `open-dev-pr`, not after.** Each exists
  because it was skipped once and something shipped broken:

  1. **Every risk flagged by the design doc is closed against the real system, not
     mocked away.** A unit test against a mock does not close an integration risk — it
     tests the mock. Close it against a real Postgres (the `test:it` suites run against
     one), a real HTTP call, the real queue. If it genuinely cannot be closed here,
     say so explicitly in `development.md` and name what would close it; do not let
     the mock stand in for the answer.
  2. **"Manually verified" claims cite evidence, not assertion.** A terminal
     transcript, a log excerpt, a response body, or numbered repro steps someone else
     can re-run. The words "manually verified" with nothing attached are treated as
     not verified — by `testing`, by `pr-review`, and here.
  3. **Golden-path behaviour is explicitly re-confirmed, not assumed.** Whenever the
     change touches shared code or error handling, re-run the pre-existing
     non-edge-case behaviour and record the result. Fixing an edge case while breaking
     the normal path is the specific failure this gate catches.
  4. **Every acceptance criterion is checked against the real diff, not against your
     own commit messages.** Run `git diff origin/<base>...HEAD --name-only` and, for
     each AC, name the file in that list which satisfies it. An AC whose satisfying
     file is not in the diff is not done.

     Added 2026-08-28. On #284 this stage reported the build complete across ten
     commits, and it was not: the design's validators had been written and
     unit-tested, and **wired into none of the four entrypoints**. `grep -ln
     "gcp_require_valid_dry_run" scripts/*.sh` returned nothing, and no entrypoint
     file appeared in the diff at all. The commit messages read as a finished build;
     the diff did not. It was caught only because the next agent checked the ACs
     against the diff instead of the summary — one stage later, and after `testing`
     would already have been dispatched at a design that could not have passed it.
     This is the same class `lld-review` had spent a full round closing one level
     down: *a unit test of a function in isolation cannot prove the call site exists.*

- **`testing`** — `open-dev-pr` already set Stage=`Testing` and posted development's
  handoff; that is not testing's start comment. Orchestrator posts
  `start-comment <n> --role testing` before delegating.

- **`testing` done** — On `issue-<n>` (not `main`). **No doc file and no commit** —
  the output is the structured handoff comment below. `testing` is read-only on the
  tree for the same reason the two reviews are: every gap it finds, including a
  missing or weak test, goes back to `development` through the rework valve rather
  than being quietly fixed around it. Its one temporary write is the mutation check,
  which it reverts before finishing (`git status` clean).

  **Refuse an incomplete handoff.** If `development.md` is missing what was built,
  the commands to run, the acceptance criteria it claims to cover, or what it
  deferred — stop before validating anything, name the missing field, and resume the
  `development` agent. Do not begin validation of an incomplete handoff.

  **The mandate: independent verification.** `development.md`'s numbers are an input
  to check, never evidence. Whatever it reports, verify all of this yourself:
  - **Run the exact commands it claims were run** and confirm the output matches the
    reported numbers. Report *your* numbers. Backend commands run in Docker only —
    `make lint`, `make build`, `npm run test:it` inside the container; never `npm` or
    `nest` on the host. Frontend: `make lint`, `make typecheck`, `make build`; `make e2e`
    from the workspace root when the change touches a user-facing flow — **scoped to
    the specs covering the surfaces this child moved**, not the whole suite (operator
    policy, 2026-08-22; the full suite runs once at epic close — see
    `references/epics.md`, "Epic closing"). `make e2e` is not a known-broken suite:
    the committed config works, and the reasons earlier attempts failed are in
    `references/parallelism.md`, "`make e2e` works". Any command that can outlast the
    default tool-call timeout needs `run_in_background` or an explicit ≥600s outer
    timeout.
  - **Confirm each acceptance criterion has at least one test that would fail if the
    criterion were violated** — a behaviour test, not an existence test. Map criterion
    → test file and test name, one line each. A criterion with no such test is a
    coverage gap, and a gap is a finding.
  - **Mutation-check the guards that matter.** Deliberately break the behaviour under
    test, confirm the test goes red, revert. State the mutation you made and what went
    red. A test that stays green against deliberately broken code is a decoration, not
    a guard — this check has repeatedly separated the two on this repo.
  - **Cite evidence, never assertion.** Every claim carries a command and its real
    output, or a grep-anchored quote (`grep -n "<literal>" <path>`). **Never cite a
    `file:line` you have not opened in this session** — fabricated citations have
    shipped from this stage before, which is why the anchor is a quote, not a number.
  - **Account for what could not be executed and why** — a suite that needs an
    external service, a flow only reachable through the UI. Silence reads as "ran and
    passed"; say it explicitly instead.

  **Reject tests that don't test** — existence-only assertions
  (`expect(service).toBeDefined()`), no meaningful assertion about output or state,
  a test asserting on its own mock's return value, or a test that survives the
  mutation check unchanged. That is a `Fail` sent back to `development` with the
  specific feedback, not a note in passing. The worked examples and the
  four-question self-check are in the `sdlc-testing` agent definition.

  **The handoff comment shape** (this is the whole of testing's output; pass it as
  `--summary`, or post it as its own comment immediately before the handoff call when
  it runs long):

  ```markdown
  ### Commands run
  | Command | Where | Result |
  |---|---|---|
  | `<exact command>` | backend container / frontend / root | 142 passed, 0 failed |

  ### Acceptance criteria coverage
  | Criterion | Test | Mutation-checked? |
  |---|---|---|
  | <criterion text> | `<file>` › `<test name>` | yes — inverted the guard, went red |

  ### Not executed
  - <what, and why it could not run>

  ### Verdict
  PASS | FAIL — <what the evidence above proves>
  ```

  - **Pass** → first, for **each main-only suite this round actually re-ran**
    (backend and/or frontend — see the independent-verification commands above),
    `sdlc_next.py record-local-ci --pr <pr> --suite backend|frontend --sha <HEAD>`,
    where `<HEAD>` is the tested worktree's `git rev-parse HEAD`. This is the
    merge-gate stand-in for the GHA check that no longer runs on a child PR (both
    suites went main-only for cost — see `references/operations.md`, "Local-CI
    attestation"). Skip it only for a suite you did **not** run (e.g. a
    backend-only change never ran the frontend suite, and its tree isn't touched so
    the gate won't ask for it). A stale attestation from a prior head does not count,
    so run this on the **current** head, after the last push. Then
    `sdlc_next.py handoff-to-pr-review <n> --pr <pr> --summary "..."` —
    **always**, including after rework (the marker is the review queue; never
    hand-type it). Then either continue straight into `pr-review` in this invocation
    or leave it for the next `list-ready-for-review` batch — both valid. Either way
    the start comment is posted when the review actually starts.
  - **Fail** → **post the structured handoff comment above as its own comment
    first**, then resume the `development` agent with the failing test details; once
    fixed, resume the `testing` agent to re-verify (don't spawn fresh — the one
    exception is the valve's own context-reset replacement at the third bounce).
    Valve pairing. The comment is not optional on this branch: the PASS path posts one
    through `handoff-to-pr-review` and the FAIL path posts nothing by default, so a
    failing round is the one round the pipeline silently loses. On #232 a 29-minute
    `testing` round produced a FAIL verdict and live probe evidence between two
    identical start comments and left **no trace on the issue**; `pr-review` then
    blocked because the evidence existed nowhere. Before delegating any next stage,
    confirm the previous stage left a comment — if it didn't, get one.

- **`pr-review`** — Orchestrator posts `start-comment <n> --role pr-review`, spawns a
  fresh Opus subagent of type `sdlc-pr-review` reviewing the PR diff adversarially: real
  bugs, security, correctness; verify `development.md`'s claims and `testing`'s handoff
  comment against the actual diff and by re-running the suite. Check CI via `sdlc_next.py pr-checks
  <pr>`. May be one of up to `PR_REVIEW_PARALLELISM` concurrent reviews, each in its
  own detached worktree (`references/parallelism.md`). Whatever the verdict, the
  **last** action before merging or resuming anyone: `sdlc_next.py record-pr-review
  <n> --pr <pr> --outcome clean|rework --summary "..."`.
  - **Clean review** → `sdlc_next.py merge-pr <pr> --issue <n>` — marks ready,
    squash-merges, deletes the branch, posts the audit-trail comment and (if the
    issue auto-closed) the closing confirmation. It re-checks CI internally and
    raises if not green — don't call `pr-checks` right before purely to pre-confirm;
    use `pr-checks` only when you need the pending/failed/missing distinction. It
    also **refuses with `{"merged": false, "behind_main": N}`** when the branch is
    behind `origin/main` — run `sync-branch` (re-triggers CI), wait for green, re-run
    `merge-pr` (see `references/parallelism.md`, "Merge-time freshness gate").
  - **`status: missing-checks`** → never poll it (no GHA run is coming on a child
    PR). Two causes, distinguished by whether the named suite is a main-only one:
    - **backend/frontend suite not yet attested for this head** (the common case,
      not a defect) → `testing` passed but didn't run `record-local-ci`, or a rework
      push staled a prior attestation. Fix: run
      `record-local-ci --pr <pr> --suite <suite> --sha <HEAD>` on the current head
      (re-running the suite first if the diff changed since it last passed), then
      re-check. Do **not** `mark-needs-human` for this.
    - **a genuine config defect** — some other required workflow renamed out of step
      with `REQUIRED_WORKFLOWS`, disabled, or `paths:`-mismatched →
      `mark-needs-human <n> --reason "required workflow reported no check: <names>"`
      and park — *unless* the PR's own diff touches `.github/workflows/**`, in which
      case resume `development` with the missing workflow names.
  - **CI pending** → re-check `pr-checks` with reasonable backoff.
  - **Real findings, or CI failed** → resume the `development` agent with specific
    findings; once fixed, a fresh `pr-review` pass (the diff changed). Valve
    pairing; on the third bounce dispatch the context-reset replacement `development`
    agent, on the sixth check the test-only merge-and-file exception above, else
    `mark-needs-human` and park.
  - **Deeper problem** → standing-epic child: resume `product` (or `architecture`);
    normal-epic child: resume `lld` if task-local, or the epic deviation escalation
    if it contradicts the epic's design. PR stays draft meanwhile. If the resumed
    agent concludes it needs the human → `mark-needs-human` (on the epic, if the
    epic's architecture was the resumed stage) and park.
