# Review rules — every review role

Read by `design-review`, `product-review` and `pr-review`. Each agent file holds its own altitude, axes and exit actions.

## Every review role

- **No finding quota.** A manufactured finding costs a rework round.
- **A clean review is a real outcome.** When every layer or axis ran and found nothing, say it is clean. A layer that failed to run makes the review incomplete, not clean.
- **Cite `file:line` on every finding**, at a location you opened. No citation, no finding.
- **Read-only.** Describe the problem and its resolution. Never edit the doc or the code to fix it. `pr-review`'s reverted mutation probe is the one exception.
- **Never `git checkout` or `git switch` in the unit's worktree.** Read through `origin/` refs (`git show origin/issue-<n>:<path>`, `git diff`), or use a detached review worktree (`python3 "$SDLC" review-worktree-add <n> --repo-path <p>`, then `release-review-worktree <n>`).
- **Rework round:** review the delta. Prove its shape first with `git diff --stat` against the integration branch. A code delta can invalidate an earlier measurement; a doc-only delta cannot. A delta that is a redesign is a first round.
- **Same-class recurrence:** when this round's blocker is the same defect class as an earlier round's on this unit, state why the earlier round missed it (out of scope, new instance from the fix, needed execution, or a plain miss). Pass `--same-class-recurrence` on your `record-*` command. A sentence alone is not enough.

## When each review stage fans out — cost discipline

Default to a single pass. Fan out only on a first round, and only when the input is too big for one careful pass:

- `arch-review` / `lld-review`: a design doc of roughly 500+ lines.
- `product-review`: a `product.md` of roughly 300+ lines, or one an Initiative splits into several Epics.
- `pr-review`: a diff of roughly 800+ changed lines, or one spanning several modules. Split by layer (edge, intent, verification) or by module slice. The Blind Hunter child runs whenever an `Agent` tool exists, below the bar too (`agents/pr-review.md`, Step 2).

On a rework round, review the delta yourself. Dispatch an axis only where the delta could reopen something an earlier round established. State any departure, with its reason, in the handoff comment.

When you fan out, read `references/review-fanout.md`.
