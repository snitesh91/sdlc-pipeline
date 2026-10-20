# Review rules — every review role, and fan-out discipline

Read by the review roles: `design-review` (both halves), `product-review` and `pr-review`.
Each agent file holds its own altitude, axes and exit actions; it does not repeat these rules.

## Every review role

- **No finding quota** — a manufactured finding costs a rework round. **A clean review is a
  real outcome:** when every layer or axis ran and found nothing, say it is clean (a layer
  that *failed to run* makes the review incomplete, not clean).
- **Cite `file:line` on every finding** (doc, code, or both) at a location you opened — never
  "somewhere in the checkout flow". No citation, no finding.
- **Read-only.** Describe the problem and its resolution; never edit the doc or the code to
  fix it — that bypasses the rework valve (`pr-review`'s reverted mutation probe is the one
  edit a reviewer makes).
- **Never `git checkout` or `git switch` in the unit's worktree** — its author resumes there.
  Read through `origin/` refs (`git show origin/issue-<n>:<path>`, `git diff`), or make a
  detached review worktree (`python3 "$SDLC" review-worktree-add <n> --repo-path <p>`) and
  release it at the end (`release-review-worktree <n>`).
- **Rework round:** work the delta yourself. Prove its shape first (`git diff --stat` against
  the integration branch): a code delta can invalidate an earlier measurement, a doc-only
  delta cannot. A delta that is a redesign rather than a fix is a first round.
- **Same-class recurrence:** when this round's blocker is the same defect class as an earlier
  round's on this unit (check prior handoff comments), state why the earlier round missed it
  — out of scope for that round's check, a new instance the fix introduced, it needed
  execution (or reading the codebase) and the earlier round only reasoned, or a plain miss —
  and pass `--same-class-recurrence` on your `record-*` command. A sentence in the verdict is
  not enough.

## Fan-out — caps and the wait loop

The Agent guard hook denies review fan-out to every other role, caps the children and sets their model
(opus by default) from `${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json` (`fanout.<role>`).

**`maxChildren` caps the children running at once** (`product-review` 2, `arch-review` 2,
`pr-review` 3 — one per layer, `lld-review` 4 — a whole `lld.md` has more independent axes
to cover). The guard seats each launch in a slot and
frees it when that child reports. **Wait-and-dispatch loop:** a launch denied at the cap
carries `retry_after: {waiting_on: [holders], hint}` — it is not final. Wait for any holder
to report, re-launch the denied axis, repeat until every axis ran. Work an axis yourself only
when no holder is left to wait for — the completeness lens is never dropped. **`reviewDepth: light`** on the unit's Initiative profile (`references/operations.md`)
denies all fan-out: work every axis yourself, in one pass. **No `Agent` tool** (seen in cloud sessions; the harness drops it — the agent file and hooks grant it) when the input is over the fan-out bar: hand the fan-out to the orchestrator ("The orchestrator fans out for you"). Never go serial silently, and never report a fan-out that did not run. A parent whose `ROLE:` header the guard cannot read gets the stricter of the
two caps and a warning, never an uncapped run.

## Review fan-out discipline — every review stage that dispatches subagents

These rules apply whatever a stage calls its axes or layers:

- **Subagents propose; you dispose.** A child's candidate becomes a finding only after
  you verify it yourself and can cite a location you opened. Never forward an unverified
  claim: a citation can make a claim look checked when it was not.
- **Only the parent executes anything that mutates or contends:** builds, suite runs,
  and anything that touches a shared Docker stack or port. Children do read-only
  analysis, because two agents running the same command in one worktree collide.
- **Wait for every dispatched child before you form a verdict or post anything.** A
  verdict that leaves out a dispatched axis claims coverage you did not have.
- **Persist each child's reply the moment it lands** — one file per axis
  (`<worktree>/.sdlc-scratch/review-<n>/<axis>.md`), never once at the end. Resuming a pass that died
  mid-way (a rate limit, a crash), read those files first and re-dispatch only the axes
  with none; a persisted candidate is still verified before it becomes a finding.

## The orchestrator fans out for you — when you have no `Agent` tool

A first-round review over the fan-out bar ("When each review stage fans out") with no `Agent`
tool does not review serially and does not post a verdict yet:

1. **You:** pick the axes as usual. Write one brief per axis to
   `<worktree>/.sdlc-scratch/review-<n>/briefs/<axis>.md` (the doc or diff, the worktree, the
   axis brief, the verify and positive-control rules your agent file gives children). Then
   hand back with outcome `blocked`, `"fanout": "unavailable"` and
   `"axes": ["<axis>", ...]` in your `SDLC-RESULT`. Post nothing else.
2. **Orchestrator:** launch one `general-purpose` agent per axis, all in one message, each
   prompt starting `AXIS: <axis>` followed by its brief file's contents, at the role's fan-out
   model (`show-config` → `fanout.<role>.model`), at most `maxChildren` at once. Save each reply
   to `<worktree>/.sdlc-scratch/review-<n>/<axis>.md` as it lands. Then resume the same review
   agent with `SendMessage`: "axis replies are in `.sdlc-scratch/review-<n>/`".
3. **You, resumed:** read the axis files, verify every candidate yourself (the rules above),
   form the verdict, post it and run your exit actions. The handoff comment states the axis
   coverage as `fanout: orchestrator`.

On a rework round, or below the bar, review in one pass as usual: no hand-off is owed.

## A fan-out child's reply is terse too — same contract, one layer down

A child replying to its parent follows the stage handback contract
(`references/stage-playbooks.md`, "The handback is terse"), **capped at 1,200
characters**:

- **One-word verdict**, from the options in the axis brief (found a candidate / found
  nothing / blocked).
- **Findings, one line each:** the claim plus its `file:line`. No quoted blocks,
  reasoning trace, or "what I checked and ruled out" inventory.
- **Nothing else.** Evidence stays in the source file (the citation points to it) or in
  a file under `<worktree>/.sdlc-scratch/` that the parent can read.

**Parent:** run `wc -c` on each reply. If a reply is over the cap, ask for a terse
re-send before acting on it.

## When each review stage fans out — cost discipline

Default to a single pass. Fan out only on a first round, and only when the input is too big
to hold in one careful pass:

- `arch-review` / `lld-review`: a design doc (`architecture.md`, `lld.md`) of roughly 500+
  lines.
- `product-review`: a `product.md` of roughly 300+ lines, or one an Initiative splits into
  several Epics.
- `pr-review`: a diff of roughly 1,000+ changed lines, or one spanning several modules. Split
  by layer (blind, edge, auditor) or by module slice; the parent still owns suites, mutation
  probes and severity.

On a rework round review the delta yourself, dispatching an axis only where the delta could
reopen something an earlier round established. State any departure, with its reason, in the
handoff comment.
