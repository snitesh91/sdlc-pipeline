# Continuous mode — unattended looping over one epic

Enter only when the operator explicitly asks (e.g. "keep this running on epic 110"). It
adds survival across unattended real time: `ScheduleWakeup` pacing and a fresh agent per
unit so context stays bounded.

- **One epic per loop.** If the operator didn't name the epic, ask — never infer.
  Several epics = several loops, one `ScheduleWakeup` chain each.
- **Mechanism:** the `loop` skill in dynamic (self-paced) mode; `ScheduleWakeup`
  re-fires the same prompt, epic number included, until stopped.

On **every** wakeup:

1. Spawn a fresh agent (`general-purpose` or `claude`, **not** a fork) with a
   self-contained prompt naming the epic and telling it to run the `sdlc:run` skill. It
   follows `SKILL.md` Steps 1–4 from a cold read (`next-action <epic>`, drive the unit stage by stage with resume-based rework,
   report back) and remembers nothing of prior units. On the unit it picks up it runs
   `pairing-counts <issue>` and counts those strikes toward the escalation valve's
   `thresholds` instead of starting at zero; a count ≥ 3 on a marker-backed pairing
   means the replacement swap already happened — never repeat it.
2. Wait for its completion notification. Note in one or two lines which unit ran and
   its outcome (merged / `epic:architected` / blocked / needs-human).
3. Decide:
   - **Stop** (`ScheduleWakeup stop:true`) when the epic survey finds nothing actionable
     — every open child closed, blocked, needs-human, or gate-pending with nothing to
     address. A `none` with `unstaged` children is not a stop: route them first
     (`SKILL.md`, "The lifecycle model").
   - **Blocked / needs-human / gate-pending** → surface it loudly, keep looping.
   - **Otherwise** spawn the next cycle agent immediately, **then** `ScheduleWakeup`
     with a long fallback delay (1200s+) as a hang safety net only.
4. **Cycle cap:** after every `pipeline.continuous.cycleCap` (default 8) issues merged,
   `ScheduleWakeup stop:true`, post a checkpoint summary, wait for the operator's
   "continue", reset the counter.
