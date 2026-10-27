# Review rules — every review role, and fan-out discipline

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
- `pr-review`: a diff of roughly 1,000+ changed lines, or one spanning several modules. Split by layer (edge, intent, verification) or by module slice. The Blind Hunter child runs whenever an `Agent` tool exists, below the bar too (`agents/pr-review.md`, Step 2).

On a rework round, review the delta yourself. Dispatch an axis only where the delta could reopen something an earlier round established. State any departure, with its reason, in the handoff comment.

Read the sections below only when you fan out.

## Fan-out — caps and the wait loop

- The Agent guard hook caps the children and sets their model from `${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json` (`fanout.<role>`).
- The policy's `maxChildren` (`show-config` → `fanout.<role>.maxChildren`) caps children running at once.
- **Wait-and-dispatch loop:** a launch denied at the cap carries `retry_after: {waiting_on: [holders], hint}`. Wait for a holder to report, re-launch the denied axis, and repeat until every axis ran. Work an axis yourself only when no holder is left. Never drop the completeness lens.
- **`reviewDepth: light`** on the Initiative profile denies all fan-out. Work every axis yourself in one pass.
- **No `Agent` tool** (the SubagentStart context says so) and the input is over the fan-out bar: hand the fan-out to the orchestrator ("The orchestrator fans out for you"). Never go serial silently. Never report a fan-out that did not run.

## Review fan-out discipline

- **Children propose; you dispose.** A child's candidate becomes a finding only after you verify it and cite a location you opened.
- **Only the parent runs anything that mutates or contends:** builds, suite runs, Docker stacks, ports. Children do read-only analysis.
- **Wait for every dispatched child** before you form a verdict or post anything.
- **Persist each child's reply when it lands**, one file per axis: `<worktree>/.sdlc-scratch/review-<n>/<axis>.md`. On a resumed pass, read those files first and re-dispatch only the axes with none.

## The orchestrator fans out for you — when you have no `Agent` tool

On a first round over the fan-out bar with no `Agent` tool, do not review serially and do not post a verdict yet:

1. **You:** pick the axes. Write one brief per axis to `<worktree>/.sdlc-scratch/review-<n>/briefs/<axis>.md`: the doc or diff, the worktree, the axis brief, and the verify and positive-control rules. Hand back with outcome `blocked`, `"fanout": "unavailable"` and `"axes": ["<axis>", ...]` in your `SDLC-RESULT`. Post nothing else. `pr-review`'s `blind` brief holds the diff only.
2. **Orchestrator:** launch one `general-purpose` agent per axis in one message, each prompt's first line `PARENT: <agent role> ISSUE: <n> AXIS: <axis>`, where `<agent role>` is `product-review`, `design-review` or `pr-review` (the guards then hold the agent read-only) followed by its brief, at the role's fan-out model (`show-config` → `fanout.<role>.model`), at most `maxChildren` at once. Save each reply to `<worktree>/.sdlc-scratch/review-<n>/<axis>.md`. Resume the review agent with `SendMessage`: "axis replies are in `.sdlc-scratch/review-<n>/`".
3. **You, resumed:** read the axis files, verify every candidate, form the verdict, post it and run your exit actions. State axis coverage as `fanout: orchestrator`.

## A fan-out child's reply is terse too

A child's reply follows "The handback is terse" (`references/stage-playbooks.md`), capped at **1,200 characters**:

- A one-word verdict from the brief's options (found a candidate / found nothing / blocked).
- Findings, one line each: the claim plus its `file:line`.
- Nothing else. Evidence stays in the source file or under `<worktree>/.sdlc-scratch/`.

**Parent:** run `wc -c` on each reply. Ask for a terse re-send of any reply over the cap before you act on it.
