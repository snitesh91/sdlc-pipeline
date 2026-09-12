# Human-review gates — product and architecture require explicit sign-off (via PR)

Referenced from `SKILL.md`. Two points in the pipeline are, by default, hard stops for
a human, inserted deliberately as a check on the earliest, most consequential
decisions (what to build, how to design it) before implementation effort is spent
against them. This is separate from, and unaffected by, the PR-merge auto-approval in
`references/operations.md` ("PRs merge automatically") — that remains fully automatic
and applies only to the later, code-carrying PR.

**For a normal epic, both gates run once at the epic level**, gating
`docs/sdlc/epic-<n>/{product,architecture}.md` — pass `--unit epic` to
`open-gate`/`pass-gate`/`skip-gate`. For a standing-epic child, both gates run per
issue (`--unit issue`, the default). Mechanics are identical at both levels except the
one Gate B difference called out below.

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
reviewing, and the epic-level product phase fixed that only for default-profile epics —
a standing/RTB backlog still opens Gate A per child. Enforced by the control plane:

- `next-action` skips a *fresh* `product` delegation (epic-self or child) at the cap
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
confidence skip" below. **What "the next stage" means depends on `--unit`**: for
`--unit issue`, passing/skipping Gate B claims `development` for that issue. For
`--unit epic`, it does **not** claim any stage — an epic never develops — it marks the
epic `epic:architected` and clears its Stage/Pipeline Status entirely, handing off to
its children's `lld` (see `_complete_epic_architecture` in `sdlc_next.py`).

Each gate is a **small, doc-only PR**, not a checkbox. Its shape depends on the unit:

- `--unit issue` (standing-epic child): `issue-<n>` → `main`.
- `--unit epic`: `epic-<n>-gate-<stage>` → `epic-<n>` — a disposable sub-branch of
  the epic branch (`epic-5-gate-product`, `epic-5-gate-architecture`; the `-gate-`
  joiner is `pipeline.branches.gateSuffix`). The epic's gate never targets `main`
  directly; `epic-<n>` itself reaches `main` once, unsquashed, at `close-epic`.
  Decided 2026-09-06 — see `references/history.md`.

Merging it *is* the approval signal; any review comment on it is feedback the
pipeline must address before that merge.

## Opening a gate

Done by the orchestrator itself — not a subagent — once the doc is committed and
pushed to the gate's head branch; Gate B specifically once `arch-review` passes clean
on the committed doc. **Where the doc is authored** (the exit-action expectation for
the `product`/`architecture` stages in `references/stage-playbooks.md`):

- Standing-epic child: on `issue-<n>`, pushed to `origin/issue-<n>` — as always.
- Epic level: on the gate sub-branch `epic-<n>-gate-<stage>`, cut from
  `origin/epic-<n>` (in the epic's worktree: `git fetch origin && git checkout -b
  epic-<n>-gate-product origin/epic-<n>`), committed there, pushed to
  `origin/epic-<n>-gate-<stage>`. Never commit the doc to `epic-<n>` directly — the
  epic branch only ever receives merges. A second Gate B round (see
  `references/epics.md`, "Epic-level deviation escalation") reuses the same
  `epic-<n>-gate-architecture` name, re-cut from the current `origin/epic-<n>`.

`open-gate --unit epic` enforces this: it refuses, before any write, when the gate
sub-branch is missing from origin or carries no commits over `epic-<n>` — the two
shapes "the doc was committed onto the epic branch instead" takes. Recovery is branch
surgery on a shared branch (move the commits to the sub-branch, force-rewind
`epic-<n>`), so it needs operator approval. Added 2026-09-06 — see
`references/history.md`.

`open-gate` opens the PR from that head against the matching base (`main` for an
issue, `epic-<n>` for an epic) and returns both as `head`/`base`:

```bash
python3 "$SDLC" open-gate <n> \
  --title "<issue/epic title, verbatim>" --doc product.md --next-stage architecture \
  --summary "<2-3 sentence plain-language summary of what this stage decided/built>" \
  [--unit epic]   # --repo-path optional: omitted, the branch's live worktree is auto-resolved
```

The command handles the PR title/body template, sets Pipeline Status to
`Awaiting Human Review`, looks up the commit SHA, and posts the marked issue/epic
comment. Your judgment calls: the `--summary` text and picking `--doc`/`--next-stage`
(A: `product.md` → `architecture`; B: `architecture.md` → `development`;
`--next-stage` on an epic Gate B is nominal-but-required — no stage is claimed).

The PR **title** must read like a normal PR about the feature — the issue's/epic's own
title verbatim, plus which doc it's gating — never the word "Gate" or a bare "A"/"B".
("Gate A"/"Gate B" are internal shorthand for this skill's prose only; they must never
leak into PR titles, issue comments, or commit messages.)

This PR is opened **ready for review, not draft** — its whole purpose is immediate
human review (a narrow, scoped exception to the global draft-PR rule). It carries
**no `Closes #<n>`** — merging it must not close the tracking issue; only the final
development PR closes an issue. How it may be merged depends on the unit:

- A per-issue gate (`issue-<n>` → `main`) is **never squash-merged and never deletes
  the branch** — `issue-<n>` keeps living through every later stage, and a squash
  would make the next `sync-branch` merge of `main` a phantom diff.
- An epic gate (`epic-<n>-gate-<stage>` → `epic-<n>`) **may be squash-merged and the
  sub-branch may be deleted after the merge** — it is disposable; the doc now lives
  on `epic-<n>`. What must never be squashed or deleted is `epic-<n>` itself, which
  merges to `main` only at `close-epic`.

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
**and derives the unit and number from the head branch** (`issue-<n>` based on
`main`, or `epic-<n>-gate-<stage>` based on `epic-<n>` — a wrong base, a bare
`epic-<n>` head (the close-epic integration PR), or a head named for the other stage
than the marker is skipped as not-a-gate), so it covers per-issue gates and epic-level
gates alike: a merged epic Gate B routes through `_complete_epic_architecture` (marks
`epic:architected`), never through a stage claim.
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
comment landing on an *open* gate PR — per-issue or epic-level — via
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
issue's/epic's full body/comments, the doc's current content, every unresolved
thread's text plus its anchored diff hunk, and every plain PR comment after the
cutoff. Instruct it to:

1. Revise `docs/sdlc/<unit>-<n>/<doc>.md` to address each piece of feedback,
   from either channel — or say explicitly in its reply why something shouldn't be
   applied, never silently ignore it.
2. Commit and push to the gate PR's head branch — `origin/issue-<n>` for an issue,
   `origin/epic-<n>-gate-<stage>` for an epic — this updates the open gate PR's diff;
   no new PR.
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
  --gate-pr <gate-pr-number> --stage product [--unit epic]   # --repo-path optional: worktree auto-resolved
```

This reconciles the unit's branch with wherever the gate just landed (merge + push
back to origin — the branch handed to the next stage is already in sync): `issue-<n>`
with `origin/main`, or `epic-<n>` with `origin/epic-<n>` (a fast-forward picking up
the merged gate sub-branch; `main` is not involved — `sync-branch --unit epic` is what
still reconciles the epic branch with `origin/main`, its integration base). It then
sets the Stage field to the next stage and claims it (Pipeline Status `In Progress`,
start comment) — this covers
a per-issue gate at either stage *and* an epic's Gate A (which claims `architecture`
on the epic itself); continue straight into that stage's delegation. The one special
case: **`--unit epic` at `--stage architecture`** (an epic's Gate B) claims nothing —
it completes the epic's architecture phase instead (same effect as `skip-gate --unit
epic`); continue into Step 1's next survey — the epic's children are now what's
eligible.

`--stage` is **cross-checked**, not trusted: the command re-derives the gate's owning
stage from the `<!-- gate-pr: stage:pr -->` marker and refuses (no mutation) on any
mismatch with `--gate-pr`/`--stage`. Always pass `next-action`'s own
`stage`/`gate_pr`/`unit` fields verbatim — `--stage` is the gate's owning doc-stage
(which doc was approved), never the stage you're heading toward.

## Gate B confidence skip

When `arch-review` returns a clean verdict it also reports numeric `confidence`
(0-100) in a `<!-- arch-review-confidence: N -->` marker. The cutoff is
**per-profile**: `skip-gate` reads the governing epic's profile and uses that profile's
`gates.skipConfidenceThreshold` (a child resolves via its parent epic), falling back to
the global `pipeline.gates.skipConfidenceThreshold` (default 95). A standing/RTB profile
may lower it (e.g. 90). Never hardcode a different number — `skip-gate` reports the
threshold it applied in its refusal.

- **Confidence > threshold** → skip Gate B entirely:
  ```bash
  python3 "$SDLC" skip-gate <n> \
    --stage architecture --confidence <N> --summary "<one sentence: what arch-review checked and confirmed sound>" [--unit epic]
  ```
  For `--unit issue`: Stage straight to `Development`, comment recording the score
  (with the confidence marker), re-claim for `development` — continue immediately,
  don't park. For `--unit epic`: marks `epic:architected` and clears the epic's
  fields — continue into Step 1's next survey.
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
  `open-gate ... --doc product.md --next-stage architecture [--unit epic]`.
- `requiresHumanGateA: false` (a standing/RTB profile) → auto-pass:
  ```bash
  python3 "$SDLC" auto-pass-gate-a <n> [--unit epic] --summary "<one sentence>"
  ```
  It refuses (raises) if the resolved profile still requires a human — so a mis-set flag
  fails loud rather than silently skipping a human review. It advances `product ->
  architecture` and claims `architecture` (both units), leaving a
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
  reopen the merged gate PR or require re-approval — intentionally out of scope. (An
  epic-level deviation found by `lld` is the one exception; it deliberately opens a
  *second* Gate B round — see `references/epics.md`, "Epic-level deviation
  escalation".)
- Never `--delete-branch` when merging a **per-issue** gate PR — `issue-<n>` keeps
  being used by every later stage. Deleting the **epic gate sub-branch**
  (`epic-<n>-gate-<stage>`) after its merge is fine; never delete `epic-<n>`.

**Development-section linking, for completeness**: `open-dev-pr` appends `Closes #<n>`
(auto-links, auto-closes); `open-gate`'s body mentions `#<n>` in plain text (links
into the issue's Development section without a closing keyword — deliberate, since a
gate PR must not auto-close the issue). Nothing further to do.
