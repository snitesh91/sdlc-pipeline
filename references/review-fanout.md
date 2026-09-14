# Review fan-out discipline

Read by `product-review`, `design-review` (both halves), and `pr-review` — the only
roles that dispatch subagents for first-round analysis. Split out of
`references/stage-playbooks.md` on 2026-09-14 so `product`, `architecture`, `lld`,
`development`, and `exploratory` (which do not fan out review subagents) are not told
to read it.

## Review fan-out discipline — every review stage that dispatches subagents

`product-review`, `arch-review`, `lld-review`, and `pr-review` each fan out their
first-round analysis to multiple subagents (axes or layers). Three rules govern all of
them, regardless of what the fan-out is called in a given stage's own file:

- **Subagents propose; you dispose.** A subagent's candidate is not a finding until
  you have personally verified it and can cite a location you opened yourself. Never
  forward an unverified claim into your report. This repo has shipped a fabricated
  quote and a citation to a `return {...}` block that was not in the file it named,
  each unverified precisely because the citation made it look checked — a fan-out that
  launders unverified claims is strictly worse than a slow serial pass.
- **Only the parent — never a dispatched subagent — executes anything that mutates or
  contends:** builds, suite runs, anything touching a shared Docker stack or port.
  Read-only analysis parallelizes across subagents; execution stays serialized with
  the parent, because two agents running the same command in one worktree collide.
- **Wait for every dispatched subagent before forming your verdict, and before posting
  anything.** A verdict posted while an axis is still running is a race you will lose:
  on #260 the parent posted CLEAN, the verification axis returned afterwards, and two
  of its candidates survived re-check — one a real defect in text marked for verbatim
  transcription into a doc that merges to `main`. The parent had to post a public
  correction and revise its own confidence marker down. Dispatching an axis and then
  concluding without it is worse than never dispatching it, because the report claims
  coverage the parent did not have.

Each review stage's own file states only its role-specific axis/layer list and any
axis-specific model tier — not these three rules again.

