# Review fan-out discipline

Read by `design-review` (both halves), the only role that may fan out. Its agent file lists
the axes; it does not repeat these rules. The Agent guard hook denies fan-out to every other
role, caps the children and sets their model from `${CLAUDE_PLUGIN_ROOT}/hooks/model_policy.json` (`fanout.<role>`).

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
