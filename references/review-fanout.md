# Review fan-out discipline

Read by `design-review` (both halves), the only role that may fan out. Its agent file lists
the axes; it does not repeat these rules. The Agent guard hook denies fan-out to every other
role, caps the children and sets their model from `${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json` (`fanout.<role>`).

**`maxChildren` caps the children running at once** (`arch-review` 2, `lld-review` 4 — a
whole `lld.md` has more independent axes to cover). The guard seats each launch in a slot and
frees it when that child reports. **Wait-and-dispatch loop:** a launch denied at the cap
carries `retry_after: {waiting_on: [holders], hint}` — it is not final. Wait for any holder
to report, re-launch the denied axis, repeat until every axis ran. Work an axis yourself only
when no holder is left to wait for (or there is no `Agent` tool) — the completeness lens is
never dropped. A parent whose `ROLE:` header the guard cannot read gets the stricter of the
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
  (`<scratchpad>/review-<n>/<axis>.md`), never once at the end. Resuming a pass that died
  mid-way (a rate limit, a crash), read those files first and re-dispatch only the axes
  with none; a persisted candidate is still verified before it becomes a finding.

## A fan-out child's reply is terse too — same contract, one layer down

A child replying to its parent follows the stage handback contract
(`references/stage-playbooks.md`, "The handback is terse"), **capped at 1,200
characters**:

- **One-word verdict**, from the options in the axis brief (found a candidate / found
  nothing / blocked).
- **Findings, one line each:** the claim plus its `file:line`. No quoted blocks,
  reasoning trace, or "what I checked and ruled out" inventory.
- **Nothing else.** Evidence stays in the source file (the citation points to it) or in
  a scratch file under the worktree that the parent can read.

**Parent:** run `wc -c` on each reply. If a reply is over the cap, ask for a terse
re-send before acting on it.

## Defaults for when a review stage fans out — cost discipline

Default to a single pass. Fan out only on a first round over a design doc (`architecture.md`,
`lld.md`) of roughly 500+ lines; on a rework round review the delta yourself, dispatching an
axis only where the delta could reopen something an earlier round established. State any
departure, with its reason, in the handoff comment.
