# Human-review gates — product and architecture require explicit sign-off (via PR)

Referenced from `SKILL.md`. Two points in the pipeline are, by default, hard stops for
a human, inserted deliberately as a check on the earliest, most consequential
decisions (what to build, how to design it) before implementation effort is spent
against them. This is separate from, and unaffected by, the PR-merge auto-approval in
`references/operations.md` ("PRs merge automatically") — that remains fully automatic
and applies only to the later, code-carrying PR.

**Every gate runs on an issue, never on an Epic or Initiative issue itself.** Gate A
gates an Initiative's Product-Roadmap Task (or a standing child's / parentless issue's
own `product.md`); Gate B gates an Epic's Architecture-phase Task (or Architecture
revision Task), or a standing child's / parentless issue's own `architecture.md`.
Mechanics are identical for all of them except the phase-Task difference called out
below.

**Gate A** — between `product-review` finishing clean and `architecture` starting,
gating `product.md`. (The universal `product-review` stage runs first; Gate A is only
reached on its clean verdict — see `references/stage-playbooks.md`.) **Whether Gate A
needs a human is a profile decision** — `gates.requiresHumanGateA`, default `true`:
- `true` (default profile) → a human hard stop, exactly as before: opens the doc-only
  PR, no confidence skip. Product-stage mistakes are the most expensive to catch late.
- `false` (a standing/RTB profile) → **auto-passed** by `auto-pass-gate-a` on a clean
  `product-review`, advancing straight to `architecture` with no human. See "Gate A
  configurability" below.

A bug fast-tracked straight to `architecture` with no `product.md` has no Gate A to
open (see `references/epics.md`, "Bug fast-track"); if that path later escalates to
product after all, the resulting `product.md` runs `product-review` and Gate A per the
profile.

### Gate A WIP cap

**At most `pipeline.productWip.maxGateAPending` units (default 5) may sit at Stage
`Product` with an open Gate A — Pipeline Status `awaiting-human-review` or
`feedback-received` — across the whole repo at once.** Operator instruction 2026-08-16:
epic #92 produced five parallel Gate A PRs a single human could not keep up with
reviewing; a standing/RTB backlog opens Gate A per child, and every Initiative opens
one. Enforced by the control plane:

- `next-action` skips a *fresh* `product` delegation at the cap
  and walks on to the next actionable unit; a `none` reached this way carries
  `product_cap: {limit, pending, deferred}`.
- `list-design-ready` proposes `product`-stage candidates only up to the remaining
  headroom (`cap − pending`, decremented per candidate selected in that call), so one
  fan-out cannot overshoot; `architecture` candidates are past Gate A and never gated.
  The result carries `product_cap`.
- Never gated: a `resume` (the unit is already counted or about to be), a rework round
  (`product-review` → `product`, in-session), `pass-gate`, `address-gate-feedback`, and
  any unit of a profile with `requiresHumanGateA: false` — it auto-passes Gate A and
  never enters the human's queue, so the cap has nothing to protect there.
- The count is repo-wide by design: the reviewer is one person across every epic, and
  N concurrent invocations on N epics would otherwise each open five. Units in
  `product` that are `in-progress` are not counted (they are not yet in the human's
  queue), so concurrent invocations can overshoot by at most their in-flight product
  units — accepted. `0` disables the cap.

**Gate B** — between `arch-review` finishing clean and the next stage starting, gating
`architecture.md`. **Conditional** — skipped automatically when `arch-review` returns
a clean verdict *and* self-reported confidence above the threshold; see "Gate B
confidence skip" below. **What "the next stage" means depends on the issue**: for a
standing child or a parentless issue, passing/skipping Gate B claims `development`.
For a **phase-Task** (a child of an Initiative or of a non-standing Epic —
`phase_task_parent` in `sdlc_next.py`), it claims no stage at all: an
Architecture-phase Task's `architecture.md` is published to
`epic-<n>/architecture.md` on the epic branch, then the Task is closed
(`phase_task_complete: true`); a Product-Roadmap Task just closes. If the publish is
not verified on origin the Task stays open (`phase_task_complete: false`, with a
`reason`) — fix it, then `publish-doc <n> --doc architecture.md` and `close-issue <n>`.

Each gate is a **small, doc-only PR**, not a checkbox, always `issue-<n>` → `main`.

Merging it *is* the approval signal; any review comment on it is feedback the
pipeline must address before that merge.

## Opening a gate

Done by the orchestrator itself — not a subagent — once the doc is committed and
pushed to the gate's head branch; Gate B specifically once `arch-review` passes clean
on the committed doc. **Where the doc is authored** (the exit-action expectation for
the `product`/`architecture` stages in `references/stage-playbooks.md`):

on `issue-<n>`, pushed to `origin/issue-<n>`, for every unit. A phase-Task's branch
is cut from `origin/main` (`worktree-add <n> --base origin/main`), never from the epic
branch — nothing is ever committed to `epic-<n>` directly; the Epic's docs reach it only
through `publish-doc`.

`open-gate` opens the PR from `issue-<n>` against `main` and returns both as
`head`/`base`:

```bash
python3 "$SDLC" open-gate <n> \
  --title "<issue title, verbatim>" --doc product.md --next-stage architecture \
  --summary "<2-3 sentence plain-language summary of what this stage decided/built>"
  # --repo-path optional: omitted, the branch's live worktree is auto-resolved
```

The command handles the PR title/body template, sets Pipeline Status to
`Awaiting Human Review`, looks up the commit SHA, and posts the marked issue/epic
comment. Your judgment calls: the `--summary` text and picking `--doc`/`--next-stage`
(A: `product.md` → `architecture`; B: `architecture.md` → `development`;
`--next-stage` on a phase-Task's gate is nominal-but-required — no stage is claimed).

The PR **title** must read like a normal PR about the feature — the issue's own
title verbatim, plus which doc it's gating — never the word "Gate" or a bare "A"/"B".
("Gate A"/"Gate B" are internal shorthand for this skill's prose only; they must never
leak into PR titles, issue comments, or commit messages.)

This PR is opened **ready for review, not draft** — its whole purpose is immediate
human review (a narrow, scoped exception to the global draft-PR rule). It carries
**no `Closes #<n>`** — merging it must not close the tracking issue; only the final
development PR closes an issue. How it may be merged depends on the issue:

- A standing child's or parentless issue's gate is **never squash-merged and never
  deletes the branch** — `issue-<n>` keeps living through every later stage, and a
  squash would make the next `sync-branch` merge of `main` a phantom diff.
- A **phase-Task's** gate (a Product-Roadmap, Architecture-phase or Architecture
  revision Task) **may be squash-merged**: `pass-gate` closes the Task and releases its
  worktree, so the branch never takes another stage. Still **do not delete the branch
  before `pass-gate` runs** — an Architecture-phase Task's `architecture.md` is
  published to the epic branch from `origin/issue-<n>`.

> **Never delete a per-issue branch while its issue is open — its unmerged
> design docs die with it.** A gate merges only `product.md`; the later-stage docs
> (`architecture.md`, `arch-review` rework, `lld.md`) live *only* on `issue-<n>` until
> the final development PR carries them to `main`. If that branch is deleted, force-reset,
> or abandoned before the dev PR merges, those docs are lost — recoverable only from the
> git object DB via `git fsck --unreachable`, and gone for good once GC runs. This is not
> hypothetical: the 2026-09-09 retro found `#101` and `#170` with their whole
> `architecture.md` + `arch-review` cycle stranded on dangling commits, and `#179`'s arch
> docs live only on `issue-179` right now. So: the source of record for a stage is durable
> **only after** the dev PR merges to `main`; treat an unmerged issue branch as the sole
> copy and never delete it while the issue is open. When a standing/RTB child is parked at
> a gate for a long time, the branch staying alive is what protects its design record.

Once `open-gate` returns, park this unit and return to Step 1 (see `SKILL.md`,
"Looping within an invocation"). There is no polling for a gate; it sits at
`awaiting-human-review` until a later Step 1 pass finds the PR merged or carrying
feedback worth acting on.

## Checking a gate

This is exactly what `next-action`'s `pass-gate`/`address-gate-feedback` outcomes
already compute. To check one gate in isolation:

```bash
python3 "$SDLC" check-gate <issue-number>
```

## Real-time backstop

`.github/workflows/gate-auto-advance.yml` calls the same gate logic automatically the
instant a human closes a gate PR either way — via `auto-pass-gate --pr <n>`, which
re-derives the issue and owning stage from the `<!-- gate-pr: stage:pr -->` marker,
**and derives the issue number from the head branch** (`issue-<n>` based on `main`;
any other head or base — including a bare `epic-<n>` head, the close-epic integration
PR — is skipped as not-a-gate). A merged phase-Task gate routes through the same
publish-and-close completion as a manual `pass-gate`, never through a stage claim.
On a merge it calls `pass_gate` with `live=False`: the Stage field advances but the
stage is deliberately **not** claimed — Pipeline Status is left cleared and no start
comment posted, because CI firing in real time doesn't mean an agent is about to run
the stage. This keeps `In Progress` reliably meaning "a stage was actually claimed and
may have crashed", so `next-action`'s `resume` never misfires on a CI-advanced gate.
On a close-without-merge it calls `mark_needs_human` directly (see "Edge cases").
`next-action`'s own detection is the backstop for when the Action didn't run (missing
`SDLC_GH_TOKEN`, workflow disabled): it finds the fields already advanced and proceeds
normally.

## Real-time feedback visibility

The same Action's `flag-feedback-received` job reacts to a review/review-comment/plain
comment landing on an *open* gate PR via
`mark-feedback-received --pr <n>`, flipping Pipeline Status from `Awaiting Human
Review` to `Feedback Received` the instant real feedback (non-empty body, not a bare
approval, not a bot) lands. Visibility only — `evaluate_gate`'s live PR-content
detection remains what decides there's something to address. Flips back via
`mark-feedback-addressed <issue>` once the revision is pushed (step 5 below).

## Addressing gate feedback

Spawn a **fresh** agent of the owning stage's `subagent_type` from `SKILL.md`'s
delegation table (`sdlc-product` or `sdlc-architecture`, both on Opus) — not a
resumed one; tracked agent IDs don't survive across invocations, and a gate can sit
open for days. Its prompt carries: the
issue's full body/comments, the doc's current content, every unresolved
thread's text plus its anchored diff hunk, and every plain PR comment after the
cutoff. Instruct it to:

1. Revise `docs/sdlc/issue-<n>/<doc>.md` to address each piece of feedback,
   from either channel — or say explicitly in its reply why something shouldn't be
   applied, never silently ignore it.
2. Commit and push to the gate PR's head branch, `origin/issue-<n>` — this updates
   the open gate PR's diff; no new PR.
3. Reply to each addressed **review thread** summarizing the change, then resolve it
   (`resolveReviewThread` GraphQL mutation on the thread's `id`).
4. Post one reply **on the PR** addressing the plain comments, ending with a fresh
   cutoff marker: `<!-- gate-comments-processed: <ISO8601 of this comment> -->` — the
   next check reads this back so addressed comments aren't reprocessed.
5. Post one short comment on the **issue** noting what was addressed and the new
   commit SHA. Then, as the last action, run `sdlc_next.py mark-feedback-addressed
   <issue>` — unconditional field write back to `Awaiting Human Review`, safe whether
   or not the forward flip ever fired.

Escalation valve, shared with "Rework and blockers" — same two-stage shape, same
ceiling of six. If the **same thread, or the same point raised repeatedly,** survives
three revisions without the human accepting it, dispatch the **context-reset
replacement** agent for revisions 4–6 (`references/stage-playbooks.md`, "Context-reset
replacement"): it gets the thread's text, the doc SHA and the class of the objection,
*not* the prior agent's rationale for why its answers were right. If the sixth revision
still doesn't land it, mark `needs-human` — six rounds of "still not right" on one
point, across two independent agents, is a genuine disagreement, not context rot and
not a typo.

## Passing a gate

Once the gate PR is merged (in practice usually already handled by the Action above;
run this manually when it hasn't):

```bash
python3 "$SDLC" pass-gate <n> \
  --gate-pr <gate-pr-number> --stage product   # --repo-path optional: worktree auto-resolved
```

This reconciles `issue-<n>` with `origin/main`, where the gate just landed (merge +
push back to origin — the branch handed to the next stage is already in sync). It then
sets the Stage field to the next stage and claims it (Pipeline Status `In Progress`,
start comment); continue straight into that stage's delegation. The special case is a
**phase-Task**: it claims nothing — an Architecture-phase Task publishes
`epic-<n>/architecture.md` and closes, a Product-Roadmap Task closes (same effect as
`skip-gate` on one); continue into Step 1's next survey.

`--stage` is **cross-checked**, not trusted: the command re-derives the gate's owning
stage from the `<!-- gate-pr: stage:pr -->` marker and refuses (no mutation) on any
mismatch with `--gate-pr`/`--stage`. Always pass `next-action`'s own
`stage`/`gate_pr` fields verbatim — `--stage` is the gate's owning doc-stage
(which doc was approved), never the stage you're heading toward.

## Gate B confidence skip

When `arch-review` returns a clean verdict it also reports numeric `confidence`
(0-100) in a `<!-- arch-review-confidence: N -->` marker. The cutoff is
**per-profile**: `skip-gate` reads the governing epic's profile and uses that profile's
`gates.skipConfidenceThreshold` (a child resolves via its parent epic), falling back to
the global `pipeline.gates.skipConfidenceThreshold` (default 80, operator instruction
2026-09-16 — was 95). A standing/RTB profile may lower it further (e.g. 70). Never
hardcode a different number — `skip-gate` reports the
threshold it applied in its refusal.

- **Confidence > threshold** → skip Gate B entirely:
  ```bash
  python3 "$SDLC" skip-gate <n> \
    --stage architecture --confidence <N> --summary "<one sentence: what arch-review checked and confirmed sound>" \
    [--repo-path <p>]   # used only for a phase-Task's publish
  ```
  For a standing child or parentless issue: Stage straight to `Development`, comment
  recording the score (with the confidence marker), re-claim for `development` —
  continue immediately, don't park. For an Architecture-phase Task: publishes
  `epic-<n>/architecture.md` and closes the Task — continue into Step 1's next survey.
- **Confidence <= threshold, missing, or verdict has findings** → open Gate B as
  above. The default is always the human gate; skipping must be earned by a high,
  explicit score.
- A handoff comment omitting the marker entirely = below threshold — never 0, never a
  guess.
- The skip path only ever applies to Gate B. `skip-gate` refuses (raises) on
  `--stage product`.

## Gate A configurability

Gate A has **no confidence skip** — but whether it requires a human at all is a profile
decision, `gates.requiresHumanGateA` (default `true`). The orchestrator applies it only
on a **clean `product-review`**:

- `requiresHumanGateA: true` (the default profile) → open the human Gate A as always:
  `open-gate ... --doc product.md --next-stage architecture`.
- `requiresHumanGateA: false` (a standing/RTB profile) → auto-pass:
  ```bash
  python3 "$SDLC" auto-pass-gate-a <n> --summary "<one sentence>"
  ```
  It refuses (raises) if the resolved profile still requires a human — so a mis-set flag
  fails loud rather than silently skipping a human review. It advances `product ->
  architecture` and claims `architecture`, leaving a
  `<!-- gate-a-auto-passed: <profile> -->` marker as the audit trail. An auto-passed
  Gate A is a **profile decision, not a skipped step** — the marker records that the
  profile, not a human, signed off.

This is the counterpart to Gate B's confidence skip: Gate B earns its skip per-review
(confidence), Gate A earns its skip per-profile (policy).

## Edge cases

- A merged gate PR always wins, even over an unresolved thread from an earlier
  revision round — don't re-litigate old feedback once a human has merged.
- Human **closes the gate PR without merging** → same as `needs-human`: a bare close
  carries nothing actionable to revise against; ask the operator rather than guess.
  The Action already does this automatically the instant the close happens.
- Later rework resuming `product`/`architecture` after their gate passed does **not**
  reopen the merged gate PR or require re-approval — intentionally out of scope. (A
  deviation from an Epic's design is the one exception; it runs a fresh Gate B on an
  Architecture revision Task — see `references/epics.md`, "Architecture deviation
  escalation".)
- Never `--delete-branch` when merging a standing child's or parentless issue's gate
  PR — `issue-<n>` keeps being used by every later stage. Never delete `epic-<n>`.

**Development-section linking, for completeness**: `open-dev-pr` appends `Closes #<n>`
(auto-links, auto-closes); `open-gate`'s body mentions `#<n>` in plain text (links
into the issue's Development section without a closing keyword — deliberate, since a
gate PR must not auto-close the issue). Nothing further to do.
