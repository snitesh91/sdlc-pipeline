# Stage playbooks: rules for every stage

Every stage agent reads this file first; its agent file names the rest. The driven repo's
`CLAUDE.md` and `AGENTS.md` hold its own commands.

- The SubagentStart hook gives you `$SDLC`, `<docRoot>` and `<requirements-dir>`; else run `python3 "$SDLC" doc-root <n>`.
- The control plane owns every GitHub mutation, worktree, force-push and rebase. The Bash guard denies hand-run ones and names the command to use.
- Launch only the subagents your agent file allows: a read-only `Explore`, a review role's fan-out children (`references/review-fanout.md`), or `development`'s `sdlc:dev-worker` workers (`agents/development.md`, "Parallel workers").
- Put scratch files only under `<worktree>/.sdlc-scratch/`. Never use `/tmp`.
- Never edit the plugin. Flag a pipeline problem in your handoff.

## Style

Write in STE-lite: one instruction or fact per sentence, active voice, present tense, one term per concept, no filler, no preamble, no recap.

## Attribution is falsifiable

Run this check before every handoff:

- `grep -F "<quote>" <file>` hits for every quote. Fix or delete a quote that misses.
- Never cite your delegation prompt. Write a constraint from it in your own voice.
- A block shown as command output is captured bytes. Prose about output is fine.
- Generate every "untouched" or "out of scope" claim from `git diff --name-only origin/<base>...HEAD`.

## Subagents finish in one turn

Nothing wakes you after your turn ends. Wait for a background command with Monitor, or use blocking calls. A final message that waits is a stall.

### The handback is terse

Your final message is for routing; evidence lives in the PR, comment or doc. Send only: **HEAD / PR**; **BLOCKER** (one line, `blocked` / `needs-human` / `failed` only); **QUESTIONS** (`needs-human` only, up to 4 lines `Q: <question> — options: <a> | <b>`); **CONCERN** (`done-with-concerns` only, one line); **DETAIL** (the link to the evidence). The last line is `SDLC-RESULT: {"issue": <n>, "stage": "<role>", "outcome": "<outcome>"}`. A standing child may add `"next"` and `"why"` (≤ 120 chars); the orchestrator decides.

| `outcome` | Meaning |
|---|---|
| `done` | An authoring stage finished its exit actions |
| `done-with-concerns` | `development` only: finished, with one doubt in `"concern"` that `pr-review` checks first |
| `clean` / `rework` | A review verdict (CONDITIONAL ACCEPT is `clean`) |
| `blocked` | You need something outside your stage: an ambiguity, a design that does not fit, a dependency, a rejected push |
| `needs-human` | Only the operator can decide |
| `failed` | A command or exit action failed and you could not recover |

The SubagentStop hook refuses a stop without a valid last line.

## Comment size is a contract

Never post the start comment or type a hidden marker; the commands do. Link docs; do not paste them.

- Stage handoff comment: at most 2,000 characters. Say what changed, where, and the delta since the last round.
- Evidence comment (review `post-comment`, `development`'s `handoff-to-pr-review --summary`, `record-*` `--summary`): at most 6,000 characters. Findings first: one heading and at most three lines each. Put output in a `<details>` block of at most 15 lines.
- State many similar findings once as a class, with two examples and the sweep.
- The control plane refuses an over-cap body.

## Secrets and containers

- Never print environment values; names only.
- Label every container you start `sdlc.issue=<n>`. Prefix compose projects, containers and volumes with your worktree basename. Tear your stack down before your handback.
- Stop only containers you started. Check `docker ps` first.
- Never prune the host. A `docker ... prune` needs `--filter label=sdlc.issue=<n>`.

## Rework and blockers

The orchestrator owns routing (`references/rework.md`).

- On a genuine ambiguity, stop and ask in your final message (`blocked`, or `needs-human` when only the operator can answer). Never guess.
- Nothing spins off a separate ticket. The owning stage fixes it.
- On a resumed finding, check it against the real code first. If it holds, fix the whole class. If it is wrong, show the evidence.
- A rework round runs every check it owes on the new head (`verification-rules.md`, "Test scope").

## Stage exit actions live in the agent files

Each `agents/<role>.md` holds its own exit actions. Worktree defaults: `/tmp/sdlc-epic-<n>`, `/tmp/sdlc-dev-<n>` (`references/parallelism.md`).
