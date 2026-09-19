# Human-review gates — product and architecture sign-off via PR

Two gates put a human on what to build and how to design it. They are separate from the
automatic code-PR merge (`references/operations.md`).

- **Gates run on issues, never on an Epic or Initiative issue itself.** Gate A gates an
  Initiative's Product-Roadmap Task, or a standing child's / parentless issue's own
  `product.md`. Gate B gates an Epic's Architecture-phase or Architecture revision Task,
  or a standing child's / parentless issue's own `architecture.md`.
- **Each gate is a small doc-only PR, always `issue-<n>` → `main`.** Merging it is the
  approval; any review comment on it is feedback to address before that merge.

Whether a gate needs a human is the profile's call ("Waived gates"). A standing child
routed past a stage (`route`) never reaches that stage's gate.

**Gate A** — after a clean `product-review`, before `architecture`; gates `product.md`.

**Gate B** — after a clean `arch-review`, before the next stage; gates
`architecture.md`. Skipped on a high-confidence clean verdict ("Gate B confidence skip").
What passing/skipping does depends on the issue:

- Standing child or parentless issue → claims `development`.
- **Phase-Task** (child of an Initiative or a non-standing Epic) → claims no stage. An
  Architecture-phase/revision Task has its `architecture.md` published to
  `epic-<n>/architecture.md` and is closed (`phase_task_complete: true`); a
  Product-Roadmap Task just closes. If the publish is not verified on origin the Task
  stays open (`phase_task_complete: false`, with a `reason`) — fix it, then
  `publish-doc <n> --doc architecture.md` and `close-issue <n>`.

### Gate A WIP cap

**At most `pipeline.productWip.maxGateAPending` units (default 5) may sit at Stage
`Product` with an open Gate A (Pipeline Status `awaiting-human-review` or
`feedback-received`), repo-wide.** Enforced by the control plane:

- `next-action` skips a *fresh* `product` delegation at the cap and walks on; a `none`
  reached this way carries `product_cap: {limit, pending, deferred}` — report it.
- `list-design-ready` proposes `product` candidates only up to the remaining headroom;
  `architecture` candidates are never capped. The result carries `product_cap`.
- Never capped: a `resume`, a rework round (`product-review` → `product`), `pass-gate`,
  `address-gate-feedback`, and any unit whose profile sets `requiresHumanGateA: false`.
- The count is repo-wide (one reviewer across every epic); `in-progress` `product` units
  are not counted. `0` disables the cap.

## Opening a gate

The orchestrator opens it itself (not a subagent) once the doc is committed and pushed
to `origin/issue-<n>` — for Gate B, once `arch-review` passed clean on that commit. Docs
are always authored on `issue-<n>`, never committed to `epic-<n>` directly.

```bash
python3 "$SDLC" open-gate <n> \
  --title "<issue title, verbatim>" --doc product.md --next-stage architecture \
  --summary "<2-3 sentence plain-language summary of what this stage decided>"
  # --repo-path optional: the branch's live worktree is auto-resolved
```

- It writes the PR body, sets `Awaiting Human Review`, posts the issue comment. You
  choose `--summary` and `--doc`/`--next-stage`
  (A: `product.md` → `architecture`; B: `architecture.md` → `development`; on a
  phase-Task `--next-stage` is required but nominal).
- **PR title = the issue's title verbatim plus which doc it gates.** `open-gate` appends the
  parent's title to a phase-Task's (`Product Roadmap - <initiative>`, `Architecture - <epic>`,
  `LLD - <epic>`), so gates from different epics read apart. Never write "Gate",
  "Gate A/B" or a bare "A"/"B" in PR titles, issue comments or commit messages — that
  shorthand is internal to this skill.
- The gate PR is **ready for review, not draft** (a scoped exception to the draft-PR
  rule) and carries **no `Closes #<n>`** — it must not close the issue.
- **Merging** — a standing child's or parentless issue's gate PR is **never squashed and
  never deletes the branch** (`issue-<n>` lives on; a squash makes the next
  `sync-branch` a phantom diff). A phase-Task's gate PR **may be squashed**, but do not
  delete its branch before `pass-gate` runs — `architecture.md` is published from
  `origin/issue-<n>`.
- **Never delete, force-reset or abandon a per-issue branch while its issue is open.**
  Until the final development PR merges, the later-stage docs (`architecture.md`,
  review rework, `lld.md`) exist only on `issue-<n>`; losing the branch loses them.

After `open-gate` returns, park the unit and go back to Step 1 (`SKILL.md`, "Looping
within an invocation"). Never poll a gate.

## Checking a gate

`next-action`'s `pass-gate` / `address-gate-feedback` outcomes already compute this. To
check one gate alone: `python3 "$SDLC" check-gate <issue-number>`.

## Real-time backstop

`.github/workflows/gate-auto-advance.yml` runs `auto-pass-gate --pr <n>` the instant a
human closes a gate PR (`issue-<n>` head, `main` base only):

- **Merged** → Stage advances but is **not claimed** (no `In Progress`, no start
  comment); a phase-Task is published and closed as `pass-gate` would. `next-action`
  proceeds from the advanced fields.
- **Closed without merge** → `needs-human` (see "Edge cases").
- If the Action didn't run (missing `SDLC_GH_TOKEN`, workflow disabled), `next-action`
  detects the merge itself.

## Real-time feedback visibility

The same workflow runs `mark-feedback-received --pr <n>` when real feedback lands on an
open gate PR, flipping Pipeline Status to `Feedback Received`. Visibility only — live PR
content decides whether there is something to address. `mark-feedback-addressed
<issue>` flips it back (step 5 below).

## Addressing gate feedback

Spawn a **fresh** agent of the owning stage (`sdlc:product` or `sdlc:architecture`, Opus)
— never a resumed one; a gate can sit open for days. Its prompt carries the issue's full
body/comments, the doc's current content, every unresolved thread's text with its
anchored diff hunk, and every plain PR comment after the cutoff marker. Instruct it to:

1. Revise `docs/sdlc/issue-<n>/<doc>.md` for each piece of feedback from either channel,
   or state in its reply why something should not be applied — never ignore it silently.
2. Commit and push to `origin/issue-<n>` (updates the open gate PR; no new PR).
3. Reply to and resolve each addressed **review thread**: `python3 "$SDLC" resolve-thread
   --thread-id <id> --reply "<summary of the change>"`.
4. Post one reply **on the PR** covering the plain comments, ending with
   `<!-- gate-comments-processed: <ISO8601 of this comment> -->`.
5. Post one short **issue** comment (`post-comment <n> --role <its role> --body-file <f>`) naming what was addressed and the new commit SHA,
   then, as the last action, run `python3 "$SDLC" mark-feedback-addressed <issue>`.

**Escalation valve**: if the same thread or the same point survives three revisions
without the human accepting it, dispatch the context-reset replacement for revisions 4–6
(`references/rework.md`, "Context-reset replacement") with the thread's text, the doc
SHA and the class of the objection — not the prior agent's rationale. If the sixth
revision still doesn't land it, `mark-needs-human`.

## Passing a gate

Once the gate PR is merged (usually the Action already did this; run it when it hasn't):

```bash
python3 "$SDLC" pass-gate <n> \
  --gate-pr <gate-pr-number> --stage product   # --repo-path optional
```

- It merges `origin/main` into `issue-<n>`, pushes, sets the next Stage and claims it —
  continue straight into that stage's delegation.
- On a **phase-Task** it claims nothing: an Architecture-phase/revision Task publishes
  `epic-<n>/architecture.md` and closes; a Product-Roadmap Task closes. Continue into
  Step 1's next survey.
- Pass `next-action`'s `issue`/`gate_pr`/`stage` fields verbatim. `--stage` is the
  gate's owning doc-stage (the doc approved), never the stage you are heading to; the
  command cross-checks it against the gate marker and refuses on mismatch.

## Gate B confidence skip

Only where the profile needs a human at Gate B; otherwise waive it ("Waived gates").
A clean `arch-review` reports `confidence` (0–100) in a
`<!-- arch-review-confidence: N -->` marker. The threshold is the governing epic's
profile `gates.skipConfidenceThreshold` (a child resolves via its parent epic), falling
back to the global `pipeline.gates.skipConfidenceThreshold` (default 80). Never hardcode
a number — `skip-gate` reports the threshold it applied.

- **Confidence > threshold** → skip Gate B:
  ```bash
  python3 "$SDLC" skip-gate <n> \
    --stage architecture --confidence <N> --summary "<one sentence: what arch-review checked and confirmed sound>" \
    [--repo-path <p>]   # used only for a phase-Task's publish
  ```
  Standing child / parentless issue: Stage → `Development`, score recorded, re-claimed
  for `development` — continue immediately. Architecture-phase/revision Task: publishes
  `epic-<n>/architecture.md` and closes — continue into Step 1's next survey.
- **Confidence ≤ threshold, marker missing, or findings present** → open Gate B. A
  missing marker counts as below threshold — never 0, never a guess.
- Only Gate B can be skipped; `skip-gate --stage product` refuses.

## Waived gates

The profile's `gates.requiresHumanGateA` / `requiresHumanGateB` (default `true`; the
shipped `standing` profile sets both `false`) decide whether a human reviews the gate.
On a **clean `product-review`** (Gate A) or **clean `arch-review`** (Gate B):

- `true` → `open-gate` (Gate B: first try the confidence skip above).
- `false` → no gate PR; waive it:
  ```bash
  python3 "$SDLC" waive-gate <n> --stage product|architecture --summary "<one sentence>"
  ```
  It refuses when the profile still requires a human. It claims the next stage (a
  phase-Task instead completes as `skip-gate` does) and leaves a
  `<!-- gate-waived: <stage>:<profile> -->` marker as the audit trail.

## Edge cases

- A merged gate PR always wins, even over an unresolved thread from an earlier round —
  don't re-litigate feedback once a human has merged.
- **Gate PR closed without merging** → `needs-human`: a bare close carries nothing to
  revise against; ask the operator. The Action does this automatically.
- Later rework on `product`/`architecture` after its gate passed does **not** reopen the
  gate PR or require re-approval. Exception: a deviation from an Epic's design runs a
  fresh Gate B on an Architecture revision Task (`references/epics.md`, "Architecture
  deviation escalation").
- Never `--delete-branch` when merging a standing child's or parentless issue's gate PR.
  Never delete `epic-<n>`.
