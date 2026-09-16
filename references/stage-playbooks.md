# Stage playbooks — docs, comments, rework, and per-stage exit actions

Referenced from `SKILL.md` (Step 3). **Every stage subagent is told to Read this file
before doing anything else** — it holds the rules that bind every role: the per-issue
doc set, citation discipline, the one-turn-finish contract, commenting discipline, the
rework model, and a pointer to every stage's own exit actions. Rules that bind only
some roles moved out on 2026-09-14 (see below), each to its own file, so a role that
does not need them is not told to read them:

- `references/design-doc-rules.md` — the content rules for `product.md`/`architecture.md`
  and the pre-`product` scope-alignment step: read by `product`, `product-review`,
  `architecture`, and `design-review` (its `arch-review` half).
- `references/verification-rules.md` — proving a claim by running it rather than
  asserting it, for `architecture`, `lld`, `development`, `pr-review`, and `design-review`.
- `references/review-fanout.md` — the subagent-dispatch discipline, for `product-review`,
  `design-review`, and `pr-review`.

Each of those three files states, at its own top, exactly which roles read it — a role
not named there does not need to Read it. (A stage agent's prompt also spells out,
verbatim, its own doc path and worktree — those are issue-specific and not in any of
these files.)

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

### The handback is terse — the detail already shipped

Your final message to the orchestrator is read for routing, not for record. Every
piece of evidence you gathered — command tables, mutation logs, suite output, file
paths, the criterion→test map — already lives in the artifact this stage owns: the PR
description, the issue comment, or the committed doc. Re-pasting it into the handback
pays for it a second time, in the orchestrator's context — the one context in the run
that every later stage inherits. So the handback carries only what the orchestrator
routes on, in this fixed shape and nothing else:

- **VERDICT:** one word. `finished` / `blocked` / `stopped` for an implementing stage;
  `clean` / `rework` / `blocked` for a review; `fits` / `deviates` for `lld` and the
  design stages. It restates the terminal state above, not a summary of the work.
- **HEAD / PR:** the head SHA, and the PR number if one exists.
- **BLOCKER:** one line, only when VERDICT is `blocked` or `stopped` — the specific
  thing named, nothing more.
- **DETAIL:** a link to the PR description or issue comment where the full evidence
  already lives — the link, never the evidence itself.

Nothing else: no command tables, no re-pasted logs, no restated diff, no "what I
checked and found fine" inventory. If the orchestrator needs the detail to route, it
opens the DETAIL link and reads GitHub — it does not need it inlined to do so. A
handback that inlines evidence the record already holds is a finding on the handback,
the same way an over-cap comment is: the orchestrator asks for a terse re-send before
acting, then routes on the fields.

**This contract binds a stage agent reporting to the orchestrator. A fan-out child
reporting to its parent reviewer is the same discipline one layer down, capped
tighter (1,200 characters) — `references/review-fanout.md`, "A fan-out child's reply
is terse too".**


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
  round. Verify the finding against the real code before you act on it: a review
  finding is a claim to check, not an order to obey. If it is right, fix the whole
  class; if it is wrong, say so with the evidence that shows it, rather than agreeing
  performatively or complying silently.
- **A rework round runs at full rigour.** It is never the place for a cheaper model,
  a skipped suite, or a shortened document check.


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
