# Human-review gates — product and architecture sign-off via PR

Two gates put a human on what to build and how to design it. They are separate from the
automatic code-PR merge (`references/operations.md`).

- **Gates run on issues, never on an Epic or Initiative issue itself.** Gate A gates an
  Initiative's Product-Roadmap Task, or a standing child's / parentless issue's own
  `product.md`. Gate B gates an Epic's Architecture-phase or Architecture revision Task,
  or a standing child's / parentless issue's own `architecture.md`.
- **Each gate is a small doc-only PR.** A standing child's / parentless issue's / Initiative
  Product-Roadmap Task's is `issue-<n>` → `main`; a non-standing Epic's Architecture-phase or
  revision Task gates its **design PR** `issue-<n>` → `epic-<n>` (raised by `transition`,
  `references/epics.md`, "How a non-standing Epic runs") — no second PR. Merging it is the
  approval; any review comment on it is feedback to address before that merge.

Whether a gate needs a human is the profile's call ("Waived gates"). A standing child
routed past a stage (`route`) never reaches that stage's gate.

**Gate A** — after a clean `product-review`, before `architecture`; gates `product.md`.

**Gate B** — after a clean `arch-review`, before the next stage; gates
`architecture.md`. Skipped on a high-confidence clean verdict ("Gate B confidence skip").
What passing/skipping does depends on the issue:

- Standing child or parentless issue → claims `development`.
- **Phase-Task** (child of an Initiative or a non-standing Epic) → claims no stage. An
  Architecture-phase/revision Task's design PR is merged into `epic-<n>` (by the human, or by
  `skip-gate`/`waive-gate` through `merge-design-pr`) and the Task closes
  (`phase_task_complete: true`); a Product-Roadmap Task just closes. If `architecture.md`
  is not on `epic-<n>` (or the design PR would not merge — `design_pr` in the result) the Task
  stays open (`phase_task_complete: false`, with a `reason`): fix it (`behind_base` →
  `sync-branch <n>`; `review_stale` → re-run the review on the new head and record it), then
  re-run the command.

The **LLD-phase Task has no gate**: `lld-review` clean → `finish-lld` merges its design PR
(`references/epics.md`).

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
are always authored on `issue-<n>` (at `epic-<n>/architecture.md` for an Epic's phase-Task),
never committed to `epic-<n>` directly.

```bash
python3 "$SDLC" open-gate <n> \
  --title "<issue title, verbatim>" --doc product.md --next-stage architecture \
  --summary "<2-3 sentence plain-language summary of what this stage decided>"
  # --repo-path optional: the branch's live worktree is auto-resolved
```

- It writes the PR body (or, for an Epic's Architecture-phase/revision Task, reuses its
  design PR and posts a `gate-comments-processed` cutoff on it, so the reviewer's earlier
  comments are not read as human feedback), sets `Awaiting Human Review`, posts the issue
  comment. You
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
  `sync-branch` a phantom diff). A phase-Task's gate PR **may be squashed**; do not delete
  its branch before `pass-gate` runs (`merge-design-pr` never does).
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
human closes a gate PR (`issue-<n>` head; `main` base, or an `epic-<n>` base carrying a
`design-pr` marker — a design PR is a gate only while its issue is awaiting review, so the
PR the pipeline merges itself is skipped):

- **Merged** → Stage advances but is **not claimed** (no `In Progress`, no start
  comment); a phase-Task is closed as `pass-gate` would. `next-action`
  proceeds from the advanced fields.
- **Closed without merge** → `needs-human` (see "Edge cases").
- If the Action didn't run (missing `SDLC_GH_TOKEN`, workflow disabled), `next-action`
  detects the merge itself.

## Feedback visibility

`Feedback Received` is a visibility flip only: live PR content (unresolved threads, plain
comments after the cutoff marker) decides whether there is something to address, and
`next-action` reads that itself. The shipped workflow does not set it — a comment-triggered
job runs a runner on every comment in the repo, so it was dropped; a driven repo that wants
the flip wires its own `issue_comment` / `pull_request_review` trigger to
`mark-feedback-received --pr <n> --author <login> --body <text>`. `mark-feedback-addressed
<issue>` flips it back (a no-op flip when nothing set it); **the orchestrator runs it, never
the agent** ("Addressing gate feedback").

## Addressing gate feedback

Spawn a **fresh** agent of the owning stage (`sdlc:product` or `sdlc:architecture`) —
never a resumed one; a gate can sit open for days. Its prompt carries the issue's full
body/comments, the doc's current content, every unresolved thread's text with its
anchored diff hunk, and every plain PR comment after the cutoff marker. The doc and the PR
depend on the issue: an Epic's Architecture-phase/revision Task's feedback is on its **design
PR** `issue-<n>` → `epic-<e>` and the doc is `docs/sdlc/epic-<e>/architecture.md`; a
Product-Roadmap Task's or standing child's is on its gate PR `issue-<n>` → `main` and the doc
is `docs/sdlc/issue-<n>/<doc>.md`. Either way the fix lands on `origin/issue-<n>`, which
updates the open PR (no new PR). Instruct the agent to:

1. Revise the gated doc for each piece of feedback from either channel, or state in its reply
   why something should not be applied — never ignore it silently.
2. Commit and push to `origin/issue-<n>`.
3. Reply to and resolve each addressed **review thread**: `python3 "$SDLC" resolve-thread
   --thread-id <id> --reply "<summary of the change>"` (the Bash guard allows it to these
   two roles).
4. Post one reply **on the PR** covering the plain comments (`gh pr comment`), ending with
   `<!-- gate-comments-processed: <ISO8601 of this comment> -->`.
5. Post one short **issue** comment (`post-comment <n> --role <its role> --body-file <f>`)
   naming what was addressed and the new commit SHA, then end with `SDLC-RESULT` outcome
   `done` ("feedback addressed and pushed"). Anything it could not finish is `blocked` /
   `needs-human` / `failed`. It never runs `mark-feedback-addressed`: that is yours.

**Then you:** on `done`, `transition <n> --expect-stage <the gated doc's stage>` (it verifies
the push and that the doc is where the gate expects it, and re-syncs the branch); only on
`ready: true` run `python3 "$SDLC" mark-feedback-addressed <n>`, which returns the issue to
`Awaiting Human Review`. On any other outcome leave the status at `Feedback Received` and
handle the outcome as usual (`SKILL.md`, "After the subagent returns").

**Escalation valve**: if the same thread or the same point survives three revisions
without the human accepting it, dispatch the context-reset replacement for revisions 4–6
(`references/rework.md`, "Context-reset replacement") with the thread's text, the doc
SHA and the class of the objection — not the prior agent's rationale. If the sixth
revision still doesn't land it, `mark-needs-human`.

## Merging a gate PR for the operator

`merge-gate <pr> --issue <n> --stage product|architecture --operator-confirmed` merges an
open gate PR (Gate A's `issue-<n>` → `main`, or a design PR held for the human). **Run it
only when the operator explicitly tells you to merge that gate PR** — never on your own
judgement, never because feedback looks resolved; without `--operator-confirmed` it refuses.
It also refuses (exit 0) a PR that is not the issue's open gate, a branch behind its base
(`behind_base` → `sync-branch <n>`, wait for fresh checks, re-run) and required checks that
are not green. A design PR and a Roadmap Task's gate squash-merge; a standing child's or
parentless issue's merges as a merge commit; the branch is never deleted. It does not pass
the gate: `next-action` (or the Action) then returns `pass-gate` for the merged PR.

## Passing a gate

Once the gate PR is merged (usually the Action already did this; run it when it hasn't):

```bash
python3 "$SDLC" pass-gate <n> \
  --gate-pr <gate-pr-number> --stage product   # --repo-path optional
```

- It merges the gate's base into `issue-<n>` (`origin/main`; `origin/epic-<n>` for an Epic's
  phase-Task), pushes, sets the next Stage and claims it — continue straight into that
  stage's delegation.
- On a **phase-Task** it claims nothing: an Architecture-phase/revision Task closes once
  `epic-<n>/architecture.md` is on `epic-<n>`; a Product-Roadmap Task closes. Continue into
  Step 1's next survey.
- A human may merge an Architecture-phase Task's design PR **before any gate opened**
  (there is no gate marker). `next-action` returns `pass-gate` with the design PR as
  `gate_pr` (and `finish-lld` for an LLD-phase Task's); `open-gate` refuses such a PR and
  names the command. Run what it returns.
- Pass `next-action`'s `issue`/`gate_pr`/`stage` fields verbatim. `--stage` is the
  gate's owning doc-stage (the doc approved), never the stage you are heading to; the
  command cross-checks it against the gate marker and refuses on mismatch.

## Gate B confidence skip

Only where the profile needs a human at Gate B; otherwise waive it ("Waived gates").
A clean `arch-review` reports `confidence` (0–100) in a
`<!-- arch-review-confidence: N -->` marker. The threshold is the governing epic's
profile `gates.skipConfidenceThreshold` (a child resolves via its parent epic), falling
back to the global `pipeline.gates.skipConfidenceThreshold` (default 80). Never hardcode
a number — `skip-gate` reports the threshold it applied, and `start-comment --role arch-review`
surfaces the same effective bar up front as `skip_confidence_threshold`, so the reviewer
knows what its confidence marker must clear.

- **Confidence > threshold** → skip Gate B:
  ```bash
  python3 "$SDLC" skip-gate <n> \
    --stage architecture --confidence <N> --summary "<one sentence: what arch-review checked and confirmed sound>" \
    [--repo-path <p>]   # used only for a phase-Task's design PR merge
  ```
  Standing child / parentless issue: Stage → `Development`, score recorded, re-claimed
  for `development` — continue immediately. Architecture-phase/revision Task: merges its
  design PR into `epic-<n>` (needs the recorded clean `arch-review`) and closes — continue
  into Step 1's next survey. The pipeline is the merge owner here; a confidence at or below
  the bar leaves the PR open for the human's merge (`open-gate`).
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
  phase-Task instead merges its design PR and completes as `skip-gate` does) and leaves a
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
- Never `--delete-branch` when merging a standing child's or parentless issue's gate PR,
  or a design PR. Never delete `epic-<n>` while its Epic is open (`close-epic`'s merge is the
  one deletion).
