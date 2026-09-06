# Continuous mode — unattended looping over one epic

Referenced from `SKILL.md`. Only enter this mode when the operator explicitly asks
(e.g. "keep this running on epic 110", "work until epic 110's backlog is done").
Default behavior already loops across an epic's units within one invocation; this
mode adds surviving *real time passing unattended*: `ScheduleWakeup` pacing, and a
fresh top-level agent per unit so context doesn't grow unbounded.

**Scoped to one epic, like every invocation.** If the operator didn't name the epic,
clarify first — never default or infer. Driving several epics continuously is
several loops, one per epic, each its own `ScheduleWakeup` chain — safe, since every
branch (child and epic-self) is worktree-isolated (`references/parallelism.md`).

**Mechanism**: the `loop` skill in dynamic (self-paced) mode — `ScheduleWakeup`
re-fires the same prompt (including the epic number) until stopped.

**Context discipline**: pipeline state lives in GitHub fields/comments plus the
committed docs, so nothing needs to survive in the orchestrator's conversation
between units. On **every** wakeup:

1. Spawn a fresh agent (`general-purpose` or `claude`, **not** a fork) whose prompt
   is self-contained and names the target epic: it re-derives everything from GitHub
   and the repo, following Steps 1-4 of `SKILL.md` exactly as a cold read would
   (`next-action <epic>`, drive the unit stage by stage — including within-unit
   agent tracking and resume-based rework — report back). Zero memory of prior units
   by design. Because tracked-agent bounce counters don't survive between cycle
   agents, each cycle agent runs `sdlc_next.py pairing-counts <issue>` on the unit
   it picks up and counts the returned marker-derived strikes toward the escalation
   valve's thresholds (3 → context-reset replacement agent, 6 → `needs-human`),
   rather than starting every pairing at zero. A count of 3 or more on a marker-backed
   pairing also means the replacement swap has *already* happened — never re-run it.
2. Wait for that agent's completion notification. Note tersely which unit ran and
   its outcome (merged / `epic:architected` / blocked / needs-human) — no growing
   narrative.
3. Decide whether to continue:
   - **Stop** (`ScheduleWakeup stop:true`) when the epic-scoped survey finds nothing
     actionable — every open child (and the epic's own phase) is closed, blocked,
     needs-human, or gate-pending with nothing to address.
   - **Surface loudly but keep looping** when a cycle ends blocked/needs-human/
     gate-pending — that unit is paused; others may remain.
   - **Otherwise spawn the next cycle agent immediately** once the notification
     arrives and backlog remains, **then** `ScheduleWakeup` with a long fallback
     delay (1200s+) purely as a hang safety net.
4. **Cycle cap — pause every 8 issues fully driven to a merge.** No tool access to
   usage-limit state, so this is the proxy safeguard. After the 8th merge:
   `ScheduleWakeup stop:true`, checkpoint summary, wait for the operator's
   "continue", reset the counter.
5. Each wakeup report stays terse: a couple lines on the unit driven and its
   outcome.

Retrospective checkpoints (`SKILL.md`, Step 5) still apply — a cycle agent that just
merged a feature's PR runs `retro-check` itself before finishing.
