# Human-review gates — product and architecture sign-off via PR

- **Gate A** gates `product.md` after a clean `product-review`: a Product-Roadmap Task's, a
  standing child's or a parentless issue's. **Gate B** gates `architecture.md` after a clean
  `arch-review`: an Epic's Architecture-phase or revision Task's, a standing child's or a
  parentless issue's. Never gate an Epic or Initiative issue itself.
- Each gate is one doc-only PR: `issue-<n>` → `main`, or for an Epic's phase-Task its design PR
  `issue-<n>` → `epic-<n>` (raised by `transition`). Merging it is the approval; a review
  comment is feedback to address first.
- The profile decides whether a gate needs a human ("Waived gates"). A standing child routed
  past a stage never reaches its gate.

Passing or skipping a gate:

- Standing child or parentless issue: claims the next stage (`architecture` after Gate A,
  `development` after Gate B).
- Phase-Task: claims no stage. An Architecture-phase or revision Task's design PR merges into
  `epic-<n>` (by the human, or by `skip-gate` / `waive-gate` through `merge-design-pr`), and the
  Task closes (`phase_task_complete: true`). A Product-Roadmap Task (Gate A) closes.
- On `phase_task_complete: false`, read `reason` and `design_pr`. Fix the cause
  (`behind_base` → `sync-branch <n>`; `review_stale` → re-run the review on the new head and
  record it). Then re-run the command.

The LLD-phase Task has no gate: a clean `lld-review` → `finish-lld` (`references/epics.md`).

### Gate A WIP cap

At most `pipeline.productWip.maxGateAPending` units (default 5) sit at Stage `Product` with an
open Gate A (`awaiting-human-review` or `feedback-received`), counted repo-wide. `0` disables the
cap. `in-progress` `product` units do not count.

- `next-action` skips fresh `product` work at the cap; its `none` then carries `product_cap`
  (`limit`, `pending`, `deferred`): report it. `list-design-ready` caps `product` candidates the
  same way, never `architecture` ones.
- Never capped: `resume`, a rework round (`product-review` → `product`), `pass-gate`,
  `address-gate-feedback`, and a unit whose profile sets `requiresHumanGateA: false`.

## Opening a gate

Open the gate yourself, never through a subagent. Open it once the doc is pushed to
`origin/issue-<n>`; for Gate B, once `arch-review` passed clean on that commit. Agents author
docs on `issue-<n>` (at `epic-<n>/architecture.md` for an Epic's phase-Task), never on `epic-<n>`.

```bash
python3 "$SDLC" open-gate <n> --doc product.md --next-stage architecture \
  --summary "<2-3 sentence plain-language summary of what this stage decided>"
  # --repo-path optional: the branch's live worktree is auto-resolved
```

- `open-gate` owns the PR title, body, status and issue comment. You choose only `--summary`,
  `--doc` and `--next-stage` (A: `product.md` → `architecture`; B: `architecture.md` →
  `development`; a phase-Task needs `--next-stage` but ignores it). Pass no `--title`.
- Never write "Gate", "Gate A/B" or a bare "A"/"B" in PR titles, issue comments or commit messages.
- **Merging:** never squash a standing child's or parentless issue's gate PR (the next
  `sync-branch` would show a phantom diff). A phase-Task's gate PR may be squashed.
- Never delete, force-reset or abandon `issue-<n>` while its issue is open: later docs live
  only there.

After `open-gate`, park the unit and return to Step 1. Never poll a gate. With
`pipeline.humanChannel: "session"`, ask instead ("Deciding a gate in session").

## Deciding a gate in session

With `pipeline.humanChannel: "session"`, dispatch every other runnable unit first. Then ask the
operator with `AskUserQuestion`: the gate PR url, the doc, your `--summary`, and three options:

- **Approve** → `approve-gate <n> --by-operator-session [--note "<their words>"]`: `merge-gate`,
  then `pass-gate`, plus an audit comment (it drops the `sdlc:gate` label so the Action does not
  pass the gate twice). Continue into the claimed stage. On `behind_base`: `prepare-rework <n>`,
  then re-run.
- **Request changes** (free text) → `request-gate-changes <n> --feedback "<text>"`; then
  "Addressing gate feedback", and ask again.
- **Decide later on GitHub** → park the unit as above.

Never pick an option for the operator.

A cloud session decides every design gate below the skip bar this way. Run `cloud-heartbeat
<epic> --state asking` before the question. Park an unanswered gate only at the session's end,
in a `waiting-human` end marker.

## Checking a gate

`next-action` checks gates. To check one: `check-gate <issue-number>`.

## Real-time backstop

`.github/workflows/gate-auto-advance.yml` runs `auto-pass-gate --pr <n>` when a human closes a
PR labelled `sdlc:gate` (`open-gate` adds it). Merged: the Stage advances unclaimed, and a
phase-Task closes. Closed unmerged: `needs-human`. If the Action did not run, `next-action`
detects the merge itself.

## Feedback visibility

`Feedback Received` is a display flag only; `next-action` reads the live PR to find open
feedback. A repo may wire its own comment trigger to `mark-feedback-received --pr <n> --author
<login> --body <text>`. Only `finish-gate-feedback` clears it, never the agent.

## Addressing gate feedback

Spawn a fresh agent of the owning stage (`sdlc:product` or `sdlc:architecture`), never a resumed
one. Give it the issue thread, the doc (`<docRoot>/epic-<e>/architecture.md` for an Epic's
phase-Task, else `<docRoot>/issue-<n>/<doc>.md`), each unresolved thread with its diff hunk, and
each plain PR comment after the cutoff marker. The fix pushes to `origin/issue-<n>`, which
updates the open PR. Tell the agent to:

1. Revise the doc for each piece of feedback, or reply why it does not apply. Never ignore one.
2. Commit and push to `origin/issue-<n>`.
3. Reply to and resolve each addressed thread: `resolve-thread --thread-id <id> --reply "<change>"`.
4. Post one PR reply covering the plain comments (`comment <pr> --body-file <f>`), ending with
   `<!-- gate-comments-processed: <ISO8601 of this comment> -->`.
5. Post one short issue comment (`post-comment <n> --role <role> --body-file <f>`) naming what
   changed and the new SHA. End with outcome `done`, or `blocked` / `needs-human` / `failed`.

**Then you:** on `done`, run `finish-gate-feedback <n> --expect-stage <the doc's stage>
--repo-path <the unit's worktree>`. It returns the unit to `Awaiting Human Review` only on
`ready`; otherwise act on `failed_step` / `reason` as for `transition`. On any other outcome,
keep `Feedback Received` and handle the outcome as usual.

**Escalation valve:** when one thread or point survives three revisions, dispatch the
context-reset replacement for revisions 4–6 (`references/rework.md`, "Context-reset replacement")
with the thread text, the doc SHA and the objection's class, not the prior agent's rationale.
After a sixth failed revision, `mark-needs-human`.

## Merging a gate PR for the operator

`merge-gate <pr> --issue <n> --stage product|architecture --operator-confirmed` merges an open
gate PR. Run it only when the operator tells you to merge that PR. On `behind_base`:
`prepare-rework <n>`, wait for its `suites.needed`, re-run. It does not pass the gate:
`next-action` (or the Action) then returns `pass-gate`.

## Passing a gate

Once the gate PR is merged, run this (usually the Action already has):

```bash
python3 "$SDLC" pass-gate <n> \
  --gate-pr <gate-pr-number> --stage product   # --repo-path optional
```

- It reconciles `issue-<n>` with the gate's base and claims the next stage: continue into that
  stage. On a phase-Task it closes the Task: return to Step 1.
- A design PR a human merged before any gate opened: `next-action` returns `pass-gate` with it
  as `gate_pr` (`finish-lld` for an LLD-phase Task).
- Pass `next-action`'s `issue`, `gate_pr` and `stage` verbatim. `--stage` is the approved doc's
  stage, never the next stage.
- A standing child whose only deliverable is the gate doc: `pass-gate <n> --doc-only`
  (`references/control-plane.md`).

## Gate B confidence skip

Use this only where the profile needs a human at Gate B. A clean `arch-review` posts
`<!-- arch-review-confidence: N -->` (0–100). The threshold is the Epic profile's
`gates.skipConfidenceThreshold`, else `pipeline.gates.skipConfidenceThreshold` (default 80).
Never hardcode it: use `transition`'s `skip_confidence_threshold`.

- **Confidence > threshold** → skip:
  ```bash
  python3 "$SDLC" skip-gate <n> \
    --stage architecture --confidence <N> --summary "<one sentence: what arch-review confirmed>" \
    [--repo-path <p>]   # used only for a phase-Task's design PR merge
  ```
  It completes as "Passing a gate" does.
- **Confidence ≤ threshold, marker missing, or findings present** → open Gate B.

## Waived gates

The profile's `gates.requiresHumanGateA` / `requiresHumanGateB` (default `true`; the shipped
`standing` profile sets both `false`) decide. On a clean `product-review` (A) or `arch-review` (B):

- `true` → `open-gate` (Gate B: try the confidence skip first).
- `false` → no gate PR:
  ```bash
  python3 "$SDLC" waive-gate <n> --stage product|architecture --summary "<one sentence>"
  ```
  It claims the next stage; a phase-Task completes as with `skip-gate`.

## Edge cases

- A merged gate PR wins, even over an unresolved thread.
- A gate PR closed without merging → `needs-human`.
- Rework on `product` / `architecture` after its gate passed does not reopen the gate. A
  deviation from an Epic's design runs a fresh Gate B on a revision Task (`references/epics.md`,
  "Architecture deviation escalation").
- Branch deletion: "Opening a gate", **Merging**; `epic-<n>`: `references/epics.md`, "The epic
  integration branch".
