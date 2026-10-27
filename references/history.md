# History — why the rules exist

The why-log: one entry per change, newest first. Instruction files do not depend on it; every
rule lives in its own file. Entries older than ~3 weeks are pruned; the full history is in git
(`git log -p -- references/history.md`).

## Changes

- **2026-10-27 — retro (v0.4.2).** Issue reads are scoped to the unit (open issues plus the closed sub-issues and parents it reaches; the whole-repo closed-issue disk cache is gone); REST paging retries a short or empty page and fails rather than return a cut list; `pass-gate --doc-only`; `post-comment` refuses a repeated body; `create-lld-tasks` applies `Placement: local`. Every agent and the orchestrator docs are rewritten in STE-lite to a word budget; design docs get audience rules (`product.md` non-technical, `architecture.md` plain words first) and length targets as guidance, never a blocking limit; `development` gets RED proof, `done-with-concerns` and a slice hand-back without an `Agent` tool; `pr-review` runs a blind diff-only pass and rates findings by confidence; cross-cutting Tasks make integration tests mandatory; the review edit guard matches its own worktree exactly and keeps fan-out children read-only.
  Why: park issue #3957 (15 findings): truncated listings poisoned the cache, the whole-repo cache did not scale, agent prompts were 5–20× public equivalents, cloud fan-out failed silently. Operator chose STE-lite over caveman, a full rewrite and unit-scoped reads.
- **2026-10-20 — v0.4.1.** One test-scope policy: the full integration suite runs once, as the epic PR's CI into `main`; a Task runs integration specs only when its brief says `Integration tests: mandatory` or it adds specs. `pr-review` gets `Edit` for its mutation probe, fenced to its review worktree.
  Why: ITs ran on every Task and in the standing IT Task's full pass; pr-review re-ran whole suites.
- **2026-10-20 — retro (v0.4.0).** Task-level placement (`sdlc:local`, `localTasks`); cloud sessions keep working through waits, record them (`--waits-on`) and relaunch once they clear; heartbeat stall detection; review fan-out fallback without an `Agent` tool; optional `development` workers; idempotent `mark-blocked`.
  Why: park issue #3004 (18 findings) and a sweep of 29 cloud sessions.
- **2026-10-12 — retro (v0.3.22).** Per-process issue index and one `git fetch` per process (`next-action` 50 s → 9 s); cloud sessions skip local `/tmp` cleanup; a standing child integrates into its Initiative's branch; `close-epic` speed-ups; `reviewDepth: light`.
  Why: park issue #2669 (15 findings plus 11 carried); cut-phase-tasks took minutes.
- **2026-10-09 — retro (v0.3.21).** Review roles post their own comment and review on a detached worktree; `verify-exit` refuses a detached or unpushed HEAD; `record-local-ci` attests suites no workflow covers; `transition` returns the profile skip threshold.
  Why: review agents detached unit worktrees; a 6-round arch-review ratchet never escalated; 4 of 10 pr-review bounces were comment-only.
- **2026-10-07 — v0.3.20.** `launch-cloud-epic` runs `claude --cloud` on a pseudo-terminal.
  Why: the CLI refuses `--cloud` without a terminal, so every laptop launch failed.
- **2026-10-07 — v0.3.19.** `SKILL.md` keeps the core loop; command tables, Initiatives and the retro move to on-demand references.
  Why: the skill loaded 59 KB on every run.
- **2026-10-07 — v0.3.18.** `requiredWorkflows[].bases` take globs; `pipeline.prePr.command`; `next-action` refuses an older plugin than the run started with; `park-finding` parks retro findings.
  Why: development moved to cloud sessions with no CI on Task PRs; findings were lost between retros.
- **2026-10-07 — v0.3.17.** Opt-in initiative branch per Initiative label (`initiativeProfiles[].branch: true`): Epics close into `initiative-<i>`, which reaches `main` once.
  Why: one plugin for every repo; an Initiative must not reach `main` before it is done.

## Incidents behind standing rules

- **Never hand-type fetch/checkout/merge/push.** A silently failed checkout merged and pushed onto the wrong branch.
- **Branch and diff from `origin/main`, never local `main`.** Local `main` goes stale; epic #156 worktrees started 4 commits behind.
- **Classic PAT only.** The org's Free plan blocks fine-grained PATs on check-runs.
- **Parked/closed units release their worktree; stale ones don't count as slots.** #186's leftover worktree ran the lane at one-third capacity with no error.
- **`product.md` follows the repo's IRD form and names no vendor.** Epic #98's product doc was rejected on ten counts, all written-down pipeline rules.
- **Never a two-dot diff range.** It reported 4 files where the agent had touched 1.
- **Resolve docs by branch name, not a SHA from an old comment.** `pr-review` blocked on an unreachable superseded draft (epic #110).
- **Compile-checking is not verification.** #209's E2E locator passed `--list` and was broken.
- **Establish a number by running the tool, not a grep that models it.** Epic #156 shipped 18+ wrong measurements; a stale `tsbuildinfo` made `tsc` emit 92 of 533 files.
- **A finding this unit won't fix goes to the owning sibling or backlog epic, never only a closing PR.** #243's coverage gap survived only by hand.
- **A replacement agent starts from the committed work.** #238/#251 replacements had to be told by hand.
- **Rework rounds are scoped.** A scoped pass took 277s vs 913s and still found a blocker.
- **Name the recurring class, not the bounce.** #234 and #243 settled one round after the class was named.
- **Commit and push after every step.** #274's batching replacement lost three rounds; per-finding commits survived nine interruptions.
- **Force-push only with `--force-with-lease` after proving a content superset.** A resumed agent hit a push rejected by an intervening rebase.
- **The machine's resource cap is the real cap.** The full IT suite was OOM-killed in the Docker VM.
- **Hold long waits in the orchestrator; run long jobs detached, writing under the bind mount.** Poll-and-re-park burned ~190k tokens; `--rm` containers lost reports.
- **Reviews stay empirical, not diff-only.** Re-running caught an IPv6-mapped-address bypass.
- **Never create the epic branch as an orphan.** `epic-110` had no merge base and could never merge.
