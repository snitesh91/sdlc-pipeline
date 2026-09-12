# Stage playbooks — docs, comments, rework, and per-stage exit actions

Referenced from `SKILL.md` (Step 3). **This is the one file a stage subagent is told
to Read before doing anything else** — it holds the per-issue doc set, the altitude
rules, the commenting discipline, the rework model, and every stage's exit actions.
(A stage agent's prompt also spells out, verbatim, its own doc path and worktree —
those are issue-specific and not in this file.)

Everything repo-specific — the exact lint/build/test commands, where they must run
(host or container), suite names, memory flags, port numbers — lives in the driven
repo's own `CLAUDE.md` and in the stage's agent definition, never here. Where this file
says "the repo's own commands", that is where to look.

## Per-issue docs (the source of record)

Each child issue gets a folder `<docRoot>/issue-<n>/`, committed on the
`issue-<n>` branch and merged into `main` with the eventual squash-merge. (A normal
epic's own phase writes into `<docRoot>/epic-<n>/` instead — see
`references/epics.md`, "Doc layout at the epic level".) **Three filenames exist in
this pipeline and no others:**

| File | Written by | Required? |
|---|---|---|
| `product.md` | `product` (standing-epic children only) | Required for a standing-epic child, except a bug fast-track where `architecture` determined no product input was needed. **Never written for a normal-epic child** — that work happened in the epic's own `product.md`. |
| `architecture.md` (standing-epic child) or `lld.md` (normal-epic child) | `architecture` or `lld` stage | Always — even a child needing no design decisions beyond the epic's `architecture.md` gets a short `lld.md` saying so, for structural consistency |

**`development`, `arch-review`/`lld-review` and `pr-review` write no doc file at all.**
The two reviews are point-in-time passes whose findings live in the issue comment
thread. `development`'s record is the **PR description** — what was built and why,
travelling with the diff into `main` — and its evidence is the `record-local-ci`
attestations, which carry each suite run's own captured output pinned to a head SHA.
A committed file of self-reported pass/fail numbers is exactly what `pr-review` is
told not to trust, so it was a file written to be distrusted (`development.md` and the
never-specified `testing.md` both died this way on 2026-09-12 — `references/history.md`).

**Writing any other file under `<docRoot>/issue-<n>/` is a defect, not initiative.**
The list above is closed. If a stage believes it needs a fourth document, that is a
question for a retrospective, not a call it makes mid-run.

Each doc must be **detailed enough that the next stage's agent works from it
independently**, without reconstructing context from the comment history. A doc may
double as the stage's working/scratch space (e.g. an internal checklist inside
`lld.md`), but the filenames above are canonical — no alternate names.

A normal-epic child's `lld.md` and every child's design doc must carry a `## Footprint`
section in the exact parseable shape defined in `references/epics.md`, "How to size
the children" — backticked paths, one per bullet.

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

## Citation discipline — every stage, without exception

Every stage in this pipeline cites the codebase and the docs, and **every stage has
shipped a wrong citation** — line numbers shifted by an addendum, a test cited ten
lines off, a blocking review finding raised against an unreachable draft commit
resolved from a SHA in an old comment link (see `references/history.md`).

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
  spans a merge of `origin/main` attributes every merged commit to the agent, and a
  "you touched files you shouldn't have" correction has been nearly sent on exactly
  that basis when the agent was right and the check was wrong (see
  `references/history.md`).
- **A stale citation in a doc that is about to merge is a real finding**, not a nit —
  it merges into `main` as a record that actively misleads the next reader. That has
  been a blocking finding on a design doc that still documented a locator the suite
  run had empirically disproved.
- **A citation can be correct when written and wrong when merged — nothing re-checks
  it after a merge.** A merge commit that pulls a sibling's refactor onto the branch
  after the docs were authored breaks every line number that pointed into the
  refactored files, and every stage will have verified them honestly (see
  `references/history.md`).

  So, concretely: **in a document that ships — a runbook, an HLD, anything under the
  repo's doc/ops trees — prefer the grep-anchored quote alone and drop the line
  number.** A quote survives a sibling's refactor; a number does not, and correcting
  numbers just resets a clock that the next sibling edit restarts. Keep line numbers
  where they are genuinely a working aid — a stage's own evidence log, a review comment
  — and treat them there as hints, per the first rule above.

  For the same reason, **when `sync-branch` pulls a sibling's work onto a branch whose
  docs cite that sibling's files, re-check those citations before the PR merges.** It
  is the one moment the pipeline creates staleness by itself rather than inheriting it.

### Attribution is falsifiable — run the check, do not recall it

The rules above govern *where* a citation points. They do not govern whether the words
you attribute to a source are actually in it, and that is the gap epic #159 lost four
review rounds to — two of them buying nothing but prose edits, on a branch whose code
was already proven sound.

Every one of these passed a conscientious author's own reading:

- A `development.md` wrote *Per the footprint note ("keep your changes compatible with
  that shape and do not remove or relocate those guards")*. That sentence exists in no
  artifact: zero hits across the whole doc tree, the issue body, and its fifteen
  comments. Its real origin was **the delegation prompt**. The agent quoted an
  instruction it had been given and cited it to a design doc.
- The next round, the same document asserted in four separate places that a spec and
  its timeouts were untouched, while its own later section correctly described raising
  them. Self-contradiction inside one file.
- Another presented a comma-joined paraphrase inside a fenced block introduced as a
  command "run for real". The real script prints one row per line with identifiers.
  Its stated diff stat was stale in the same paragraph.

So, as a mechanical pass before any handoff, not as a habit of care:

- **Every quoted string must be reproducible by grep against the file you cite.** If
  `grep -F "<the quoted words>" <cited file>` returns nothing, the quotation is wrong —
  delete it or fix it. Quote spans short enough to survive reflowing.
- **The delegation prompt is not a citable source.** Nothing in it is an artifact;
  it does not merge, a reader cannot open it, and the next agent gets a different one.
  A constraint that reached you only through your prompt goes in the document's own
  voice, unquoted and unattributed.
- **A fenced block introduced as command output must be bytes you captured.** Redirect
  to a file and paste from the file, or cite the `record-local-ci` attestation, which
  already embeds a real run's captured output. Prose describing what a command showed
  is always acceptable; a fence is a claim of literalness.
- **Generate every "untouched" / "unchanged" / "out of scope" claim from the diff**,
  with `git diff --name-only origin/<base>...HEAD`, at the moment you write the
  sentence. These claims are the most likely in any document to have been true when
  drafted and false by handoff, because the author keeps working after writing them.

A document that ships squash-merges into `main` as a permanent record. A fabricated
quotation there invents a directive that some future reader will follow.

## Compile-checking is not verification

A test runner's `--list`/dry-run mode, a type-check, and a lint pass tell you the code
*parses*. They cannot tell you a test **ran**, and a skipped test is indistinguishable
from a passing one in a `--list` output.

This is not hypothetical: new end-to-end tests have shipped with a locator that matched
the wrong element and yielded `NaN`, past a clean `--list` check, in a suite where they
skipped under the repo's default e2e invocation — they would have merged broken and
silently stayed broken; it was caught only by executing them for real (see
`references/history.md`).

So: **no stage may report a test as covering an acceptance criterion unless it observed
that test execute.** If it could not run, say so plainly, name the reason, and say
which criterion is therefore unproven — `pr-review` can then weigh a known gap instead
of trusting a coverage claim that was never true.

**A green build is not a build either — clear stale incremental state first.** An
incremental TypeScript build (`nest build`, `tsc -b`) with a stale, gitignored
`*.tsbuildinfo` on disk decides nothing changed, **emits nothing, and exits 0** — a
vacuous green. It happened twice in one epic (#323, in `development` and again in the
since-retired `testing` stage, both reporting a passing build that produced no
`dist/main.js`; see `references/history.md`, 2026-09-12). Standing step for
`development` and `pr-review`, whenever a
build is used as a verification gate: **before** the build, remove the stale cache
(`find <package> -name '*.tsbuildinfo' -delete`, or the repo's clean target) — **or**,
after it, assert the expected artifact exists and is newer than the sources
(`test -f dist/main.js && find dist -newer src -type f | head -1`). Exit 0 alone is
never evidence; state which of the two you did in your handoff.

## Subagents finish in one turn — never park awaiting a wake

Every stage agent is a subagent, and a subagent is **not** re-invoked across turns:
nothing wakes it once its turn ends. So a stage must complete everything it needs while
its turn is live. For a long command — the integration suite, a container/image build,
the e2e suite — run it with `run_in_background` and **wait on it in-turn via the Monitor
tool** (foreground `sleep` is blocked). What must never happen is ending the turn
"standing by" for a background job or a Monitor notification to resume the agent: the
notification never arrives, the stage stalls until a human nudges it, and it registers
as no progress. This recurred across every implementing agent of epic #430
and killed two agents outright on earlier epics (see `references/history.md`). Those
three agent definitions used to restate it, and had drifted into three different
strengths — the weakest of them is what the agent that stalled on 2026-09-13 was
reading. They now carry the one-sentence contract and point here for the rest.

**Restating this rule has stopped working — so it is now a contract on the final
message.** It is already stated here, and again in three agent definitions, with two
agents killed by it on earlier epics and a recurrence across every implementing agent
of epic #430. On 2026-09-13 an epic-159 agent did it again: it started an e2e run,
set up a Monitor, and ended its turn saying it would resume when the notification
arrived. Nothing wakes a subagent. It sat idle until the orchestrator noticed, drove
the run by hand, and re-messaged it.

**Your final message must declare a terminal state**, and there are exactly three:
finished, blocked on something named, or stopped for a decision you have stated. Any
final message whose last act is to wait — "I'll pick this up when the run lands",
"monitoring for completion", "no further action needed until the notification" — is a
stall, no matter how much real work preceded it. If a run is still going, wait on it
in-turn; if you cannot, say so and name what you need, which is a terminal state.

**Orchestrator side:** read every returning agent's final message for this shape before
acting on its content. A returned agent that declared waiting has not finished, and its
issue is not at the stage its handoff implies. Re-message it with the result it was
waiting for rather than treating the stage as complete.

## Establish a number by running the thing, not by modelling it

The dominant defect class of one whole epic — over a dozen confidently-stated, wrong
measurements across its children and its own architecture — had one shape every time:
**a number produced by a grep that models a rule, rather than by running the rule.**
Every blocking finding in that epic was found by building the change and running the
real tool; none was found by reading (see `references/history.md`). Three rules, each
from a real incident:

- **Run the rule, don't regex-model it.** A counting grep that carries an exemption the
  shipped lint config does not have produces a number that is true of the grep and
  false of the codebase; applied as designed it bans legitimate imports and ships a
  CI-red PR, and the obvious late fix invalidates whatever analysis rested on the
  count. The same applies to prose rules: a criterion whose meaning lives only in the
  design doc will, applied as written, exempt files it should not. **If the artifact is
  executable (a lint config, a script, a test, a build), apply the change in a scratch
  copy and run it. Report that output.**
- **Scope the search to the whole tree, not to the module you are thinking about.**
  The same composition file has been missed twice, by two siblings, for the identical
  reason: a grep scoped to the directory the agent was thinking about, in a repo whose
  composition file lives one directory over. Anchor the pattern to the *symbol*, not to
  a path prefix you assume — a pattern that matches the imported module name wherever
  it appears finds what a relative-path pattern cannot.
- **Prove the detector detects before reporting an absence.** A "0 cycles", "0
  violations", "no diff" result is worth exactly as much as the demonstration that the
  check *can* go non-zero. The move that works is the **positive control**: introduce
  one deliberate instance of the thing you are checking for, watch the count go
  non-zero, revert, watch it go back. A claim that several separate agents have each
  run that control on is trustworthy; one that none has is not.

**Vacuous-pass tells** — a check that passed because it examined nothing. All four have
been hit live:

- A **stale incremental-build cache** made the compiler emit a fraction of the tree and
  **exit 0**. Two different agents hit it. Clear build state and assert the emit count
  before using it.
- A graph built with **too narrow an edge pattern** gave the nodes under test zero
  outgoing edges and a vacuous "0 cycles" — indistinguishable from a real clean result.
  Assert non-vacuity (edge count, node count) before reading a graph result.
- **Structurally skipped tests.** A harness that never forwards the credentials a
  subset of tests needs skips that subset on *every run the repo has ever done*, and a
  before/after comparison "matches" over them. **A skip is not a cover** — count
  executed tests, not listed ones.
- **A mutation that discriminates nothing.** A mutation of a permission both roles
  already hold stays green, and the correct reading is *a bad discriminator, not a
  finding about the test*. If a mutation stays green, first ask whether it was a real
  mutation.

**Your own tooling gets the same scrutiny as any other claim.** Agents have caught
false positives in scripts they had just written, before treating the output as
evidence, and said so in the handoff. That is the bar.

## A completeness claim over a footprint is a sweep, not a list

This is the same rule as "establish a number by running the thing", applied to the
one place it bounces hardest: a child whose acceptance is a **class of surfaces** —
"every interactive control is ≥44px", "no fixed bar overlaps the nav", "every
on-screen file is audited for readability". One whole epic's audit/hardening children
each cost two predictable `lld-review` ↔ `lld` or `pr-review` ↔ `development` rounds
to the same failure, every time (see `references/history.md`): the doc asserted
completeness in prose — "§3 lists every file", "these two bars are all of them" — the
reviewer's completeness lens found one more instance, the author patched that named
instance, and the next round found the next one. A prose enumeration is a claim about
the author's attention, and the reviewer can only falsify it one instance at a time.

For any such child, at **every** stage that touches the class:

- **State completeness as a reproducible sweep, not an instance list.** The `lld`
  defines the class by a mechanical rule — a `grep`/`find` pattern anchored to the
  symbol or attribute, plus the stated partition of what the sweep covers and what is
  excluded and why. Paste the command and its output. "I looked at every file" is not
  a sweep; `comm -23 <sorted-find> <sorted-inventory>` returning empty is.
- **Cover every dimension the acceptance names.** If the criterion is "44×44px", a
  detector that models height only will pass a 44×10px control — a real bounce. Model
  each dimension the AC states as a separate term, and run a **positive control per
  dimension** (introduce one deliberate violation on that axis, watch the count go
  non-zero, revert). A guard that has only ever gone non-zero on one axis does not
  cover the other.
- **`development` applies the class rule per instance; it does not re-judge the
  class.** The `lld` states the rule once ("every `fixed bottom-0` bar this child adds
  or finds takes `bottom-14 md:bottom-0`"); `development`'s handoff reports that the
  rule was applied to each swept instance, not merely that the AC "passes". A per-file checklist
  that `development` works item by item is exactly where a missed-because-unlisted file
  ships looking identical to an audited-clean one — so the inventory the checklist is
  built from must be the sweep's output, not a hand-typed list.

**The narrow scoping of this section is itself a trap — the failure is not confined to
audit/hardening children.** Any criterion → test map is a completeness claim in prose,
and the positive control is what falsifies it; a child with an ordinary feature shape
fails the same way when several criteria share one assertion shape. On #494 — a feature
child, no class-of-surfaces acceptance anywhere in it — four criteria mapped to tests
whose only assertion was the reply's *type*, which every reply in that family shares.
Green suite, complete-looking map, no coverage; the `pr-review` positive control that
exposed it took one run. So read the rule above as scoped to *any* stage claiming a set
of criteria is covered, and see the family positive control in `sdlc-development.md`
("How to write the tests") for the cheap form: one control per family of sibling
expected values, not one per criterion.

The population of the class is a **requirements** fact, not a `development` call. If
which controls or which dimensions count is ambiguous ("interactive control" —
icon-only, or text buttons and pagination too?), that is pinned at `architecture`/`lld`
and escalated when unclear — never narrowed silently at `development`, which
`pr-review` will (correctly) bounce as an unauthorised scope reduction. A reviewer's
"same-class, second consecutive bounce" note is the signal to stop patching the next
named instance and close the class at its root with a sweep.

## Commenting discipline

The main agent never needs to *read* a comment to decide what happens next — it just
ran the previous stage. Comments are the **visibility record** for a human and the
**resume point** if a session dies. Leaner than the docs — a pointer and a summary:

- **Start comment is mandatory.** `🚧 Picking this up — <role> stage starting.` —
  posted before delegating, every time. When the orchestrator posts it itself
  (`arch-review`, `lld-review`, and `pr-review`):
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

**Comment size is a contract, not a style preference.** Measured before this rule
existed: `arch-review` rounds of 24,284 / 18,689 / 16,070 characters, an `lld-review`
of 15,365, a `pr-review` of 14,640 — against stage handoffs that owned a doc and stayed
at 1.4–3.3K. A round-4 architect dispatch pulled 59K characters of review text across
three fetches. Comment bulk is an **input cost on every downstream stage**, and it
dilutes the few lines that decide whether a round succeeds. So:

- **Stage handoff comment: ≤ 2,000 characters.** What changed, where the doc/commit is,
  the delta since the last round, the marker. The doc carries the detail.
- **Evidence-carrying comment** (`product-review`, `arch-review`/`lld-review`,
  `pr-review`, `development`'s handoff): **≤ 6,000 characters.** Findings first, each as
  one heading plus at most three lines (what, why it matters, the fix — described, not
  applied); non-blocking findings one line each; the verdict one line. Command output
  and quotes go in a `<details>` block trimmed to the lines that prove the point (≤ 15
  lines per block). The Scope/Verification section is ≤ 3 lines — say what you read and
  ran, not everything you found sound.
- **Do not restate the document, the diff, or the previous round.** A reviewer who has
  more to say than the cap allows has found a *class*, not a list: state the class
  once, give two exemplars and the sweep that finds the rest ("A completeness claim …
  is a sweep, not a list"), and stop. Cut the "what I checked and found fine"
  inventory first — it is the bulk in every over-length comment measured.
- **Over the cap is a finding on the comment.** The orchestrator asks for a trimmed
  re-post before dispatching the next stage, the same way it asks for a missing
  comment. `wc -c` on the body before posting is the whole check.

Each handoff comment ends with the hidden marker
`<!-- stage-transition: <from-role>-><to-role> @ <ISO8601> -->` — posted by the
script wherever a script command owns the transition (`open-dev-pr`,
`handoff-to-pr-review`, `open-gate`, `pass-gate`, `skip-gate`).

**Issue comments are status updates, not the source of record** — the real detail
lives in committed artifacts (the doc files, commits, the issue body for durable
requirements). Pipeline tooling (this skill, the agent definitions, and any sibling
tooling the repo tracks alongside them) is tracked in the repo: when a stage touches
it, commit that change with the related code.

## Rework and blockers — what it means for you

Full routing, resume-message construction, the context-reset replacement and the
escalation valve moved to `references/rework.md` on 2026-09-13. They are the
**orchestrator's** decisions — which stage owns a defect, whether a bounce trips the
valve, what a replacement is told — and they were 198 lines in the file every stage
agent reads.

What binds you, as a stage agent:

- **Genuine ambiguity → stop and report the specific question in your final message.**
  Never guess, never open an issue, never change project fields. The orchestrator
  resolves it with whichever earlier stage owns the question and resumes you with the
  answer. Stopping this way is a terminal state, not a failure.
- **Nothing spins off a separate ticket.** Every problem found before merge is fixed
  inline by the stage that owns it. You will be resumed with the finding rather than
  replaced, because you still hold the context.
- **When you are resumed with a review finding, fix the class, not the listed
  instance.** A same-class repeat bounce escalates; a patched instance invites the next
  round. If you believe the finding is wrong, say so with evidence rather than
  complying silently — see `superpowers:receiving-code-review`.
- **A rework round runs at full rigour.** It is never the place for a cheaper model,
  a skipped suite, or a shortened document check.

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

## Stage exit actions live in the agent files

Each stage's exit actions moved into that stage's own `agents/sdlc-*.md` on
2026-09-13. They were 459 lines here — more than a third of this file — and every
agent read all of them to use one eighth. A rule is honoured where it is read, and an
agent is guaranteed to read its own definition.

| Stage | Its exit actions |
|---|---|
| `product` | `agents/sdlc-product.md` |
| `product-review` | `agents/sdlc-product-review.md` |
| `architecture` | `agents/sdlc-architecture.md` |
| `arch-review`, `lld-review` | `agents/sdlc-design-review.md` |
| `lld` | `agents/sdlc-lld.md` |
| `development` | `agents/sdlc-development.md` |
| `pr-review` | `agents/sdlc-pr-review.md` |

**What stayed the orchestrator's**, in every case:

- **Opening a human-review gate**, after the agent returns — never the agent's
  (`references/gates.md`).
- **Posting `start-comment <n> --role <role>`** before a review stage, since the
  review agent is dispatched after the marker exists.
- **`merge-lld-doc`** on a clean `lld-review`, which publishes the doc and advances the
  child to `development` without claiming it (see SKILL.md, "After the subagent
  returns").
- Exit actions update the same issue's fields in place — **never a new issue for a
  normal handoff.**

Worktree paths in those files use the config's `pipeline.worktrees` defaults
(`<root>/<epicPrefix><n>` → `/tmp/sdlc-epic-<n>`, `<root>/<devPrefix><n>` →
`/tmp/sdlc-dev-<n>`); see `references/parallelism.md`.
