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

## A fan-out child's reply is terse too — same contract, one layer down

`references/stage-playbooks.md`, "The handback is terse" binds a stage agent
reporting to the **orchestrator**. It says nothing about a fan-out child reporting to
its **parent reviewer** — and that gap is exactly where the discipline broke down.
Measured on the 2026-09-16 live v2 integration test: 22 fan-out children returned
**8,320–14,592 characters each**, re-pasting the evidence they'd gathered straight
into the parent's context — the identical failure the terse-handback contract exists
to prevent, recurring one layer down where nothing named it.

So a dispatched child's reply to its parent follows the same shape as a stage
handoff, **capped at 1,200 characters**:

- **One word verdict** — whatever the axis brief asked for (a candidate found / found
  nothing / blocked).
- **A findings list, one line each**: the claim plus its `file:line`. Nothing more per
  finding — no quoted blocks, no reasoning trace, no "here's what I checked and ruled
  out" inventory.
- **Nothing else.** Evidence stays where it was gathered: in the file it came from
  (the parent re-opens it — the citation is the pointer), or in a scratch file under
  the worktree the parent can read on demand. Never inlined into the reply.

This is what makes fan-out cheaper than a serial pass in practice, not just in
theory — the parent verifies each candidate against the real file anyway (see
"Subagents propose; you dispose" above), so a child that pastes the evidence is
paying to re-derive something the parent was always going to re-check independently.

**A parent receiving an over-cap reply asks for a terse re-send before acting on it**
— exactly as the orchestrator does with an over-cap stage handoff
(`references/stage-playbooks.md`, "Comment size is a contract"). `wc -c` on the
child's reply is the whole check.

## Defaults for when a review stage fans out — cost discipline

Reviews are the dominant cost of a run: measured on the 2026-09-16 live v2
integration test, `pr-review` ($65), `lld-review` ($31), and `arch-review` ($25) were
63% of a ~$194 run. The operator accepted a slight review-depth trade-off on
2026-09-16 for roughly half that cost. These are the resulting defaults for every
review stage that dispatches subagents — a reviewer may exceed any of them, stated as
a reason in the handoff comment, but the default is what runs absent that reason:

- **Fan out only when the artifact earns it. Default to a single-pass review.** Fan
  out only above a real size threshold:
  - `pr-review`: a diff over 400 changed lines, or touching more than 10 files.
  - `product-review` / `arch-review` / `lld-review`: a design doc (`product.md`,
    `architecture.md`, `lld.md`) over roughly 500 lines.
  Below the threshold, work the axes/layers yourself in one pass — the fan-out's
  value is independent-context discovery on something large enough that one pass
  would miss a class of defect, not a mechanical default for every review regardless
  of size.
- **Never fan out on a rework round, regardless of size.** A rework round is scoped
  to the findings that bounced it, not a fresh first pass — dispatching axes/layers
  over a bounded delta re-buys coverage you already have. Evidence, same unit,
  consecutive rounds: `lld-review` round 1 fanned out to 5 children across 45 tool
  calls; round 2, scoped and fan-out-free, used 9 calls and still verified the fix by
  execution rather than by re-reading. `product-review` and `design-review` already
  state this in their own files; it binds `pr-review` too — a rework pass reviews the
  delta yourself, dispatching a layer only where the delta plausibly reopens
  something an earlier round established.
- **Cap the breadth when fan-out is warranted: at most 3 children.** More children
  than that is diminishing return on a task this narrow (one axis or one layer each)
  and the coordination/verification cost the parent pays per child rises linearly
  with the count.
- **When dispatching a fan-out child, you MUST pass `model: "sonnet"` explicitly on
  the `Agent` call.** This is a requirement, not a default to lean on: a subagent
  dispatch with no `model` param does not run at some neutral tier — it silently
  inherits the *dispatching* agent's own tier, which for every review stage is opus.
  Measured on the 2026-09-16 live v2 integration test: `arch-review` and `lld-review`
  each passed `model=sonnet` on 4 of 5 children and omitted it on the 5th, which
  silently ran opus; all 12 `pr-review` children were dispatched with no `model` at
  all, so every one of them ran opus. The run's 14 opus children cost $50.0 against
  8 sonnet children at $12.5 — the omission alone is roughly a 4x-per-child cost
  multiplier, and it is exactly the failure this whole section exists to prevent,
  reintroduced by a missing parameter rather than a missing rule. The parent itself
  stays at its own table tier and owns the verdict — only the dispatched children
  take `model: "sonnet"`.
- A child's job is narrow, read-only, proposal-only candidate-finding ("subagents
  propose; you dispose", above): the parent re-verifies every candidate before it
  becomes a finding, so a weaker child tier costs a missed lead, never a false claim
  in the review — sonnet is not a compromise on this axis. The one standing exception
  is a stage's own completeness/generative axis, which stays at the parent's tier
  because it is exploratory rather than confirmatory — already the case in
  `agents/sdlc-design-review.md` and `agents/sdlc-product-review.md`; state any other
  exception the same way, with a reason, on the dispatch that takes it.

