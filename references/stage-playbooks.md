# Stage playbooks: rules for every stage

Every stage agent reads this file first. Rules for some roles only live in:

| File | Read by |
|---|---|
| `references/design-doc-rules.md` | `product`, `product-review`, `architecture`, `design-review` (`arch-review`) |
| `references/verification-rules.md` | `lld`, `development`, `pr-review`, `design-review`; `architecture` on demand |
| `references/review-fanout.md` | `design-review`, `product-review`, `pr-review` |

The driven repo's `CLAUDE.md` and `AGENTS.md` hold its own commands (lint, build, test, containers, ports).

- The SubagentStart hook gives you `$SDLC`, the plugin root, `<docRoot>` and `<requirements-dir>`. If it cannot resolve your docRoot, run `python3 "$SDLC" doc-root <n>`.
- The control plane owns every GitHub mutation, GraphQL call, worktree, force-push and rebase. The Bash guard denies hand-run ones and names the command to use. It also limits you to your role's commands. Report anything outside your role in your handoff.
- Put scratch files only under `<worktree>/.sdlc-scratch/`. Never use `/tmp`.

## Style

Write docs, comments and handoffs in STE-lite: one instruction or fact per sentence, active voice, present tense, one term per concept, no filler. Keep the handoff and report terse: no preamble, no recap.

## Per-issue docs

A design stage commits its doc on branch `issue-<n>` at one path. An Epic's architecture or lld phase-Task writes `<docRoot>/epic-<e>/<doc>`. Every other issue writes `<docRoot>/issue-<n>/<doc>`. Your prompt names the path.

| File | Written by |
|---|---|
| `product.md` | `product`: an Initiative's Product-Roadmap Task or a standing child. Never an Epic's Task, except an approved amendment (`references/initiatives.md`, "Amending an approved `product.md`") |
| `architecture.md` | `architecture`: an Epic's Architecture-phase or revision Task, or a standing child |
| `lld.md` | `lld`: an Epic's LLD-phase Task. One `## Task` section per Task |

- No other file goes in your doc directory. Propose a fourth doc in your handoff; do not create it.
- `development` and the review roles write no doc. `development`'s record is the PR description. Its evidence is the PR's checks and any `record-local-ci` attestation. Never commit self-reported pass/fail numbers.
- Write each doc so the next stage works from it alone.
- Edit a doc only with `Edit`/`Write`. Never edit a doc through `Bash` (`sed`, `python`, heredoc).
- Each `lld.md` Task section and a standing child's `architecture.md` carry a `## Footprint` in the shape from `references/epics.md`, "How to size the Tasks".

## Citation discipline

- Anchor a citation to a heading, an exact test title or a grep-able quote. A line number is only a hint. In a shipped doc, use the quote alone.
- Cite only files you opened in this session.
- Before you resolve a doc from a branch, run `git fetch origin`. State the ref you used.
- See a branch's changes with `git diff origin/<base>...<branch>` (three dots). `<base>` is `epic-<e>` for a child of a non-standing Epic, else `main`.
- A stale citation in a doc about to merge is a real finding.
- After `sync-branch` brings in a sibling's files that your doc cites, re-check those citations.

### Attribution is falsifiable

Run this check before every handoff:

- `grep -F "<quote>" <file>` hits for every quote. Fix or delete a quote that misses.
- Never cite your delegation prompt. Write a constraint from it in the doc's own voice.
- A block shown as command output is captured bytes. Prose about output is fine.
- Generate every "untouched" or "out of scope" claim from `git diff --name-only origin/<base>...HEAD`.

## Subagents finish in one turn

Nothing wakes you after your turn ends.

- Run a long command with `run_in_background` and wait for it with Monitor. An agent without Monitor uses blocking calls.
- Your final message declares exactly one terminal outcome. A final message that waits ("I'll check when it lands") is a stall.

### The handback is terse

Your final message is for routing. The evidence is already in the PR, comment or doc. Send only:

- **HEAD / PR:** head SHA, PR number if any.
- **BLOCKER:** one line, for `blocked` / `needs-human` / `failed` only.
- **QUESTIONS** (`needs-human` only): up to 4 lines `Q: <question> — options: <a> | <b>`. The orchestrator asks the operator and resumes you.
- **CONCERN** (`done-with-concerns` only): one line.
- **DETAIL:** the link that holds the evidence.
- **Last line:** `SDLC-RESULT: {"issue": <n>, "stage": "<role>", "outcome": "<outcome>"}`.
- **Standing child, optional:** `"next": "<later stage or merge>"`, `"why": "<≤ 120 chars>"`. The orchestrator decides.

| `outcome` | Meaning |
|---|---|
| `done` | An authoring stage finished its exit actions (a gate-feedback round included) |
| `done-with-concerns` | `development` finished, with one stated doubt for `pr-review` to check first |
| `clean` / `rework` | A review verdict (CONDITIONAL ACCEPT is `clean`) |
| `blocked` | You need something outside your stage: an ambiguity, a design that does not fit, a dependency, a rejected push |
| `needs-human` | Only the operator can decide |
| `failed` | A command or exit action failed and you could not recover |

The SubagentStop hook refuses a stop without a valid last line. A fan-out child replies to its parent under the same contract, capped at 1,200 characters (`references/review-fanout.md`).

## Commenting discipline

Comments are the visibility log and resume point. Docs and commits are the record.

- The orchestrator posts the start comment. Never post it yourself.
- Post one comment per milestone. Post at least the handoff.
- Link the doc; do not paste it.

### Posting a handoff comment

`product`, `architecture`, `lld` and the review roles post with `python3 "$SDLC" post-comment <n> --role <role> --body-file <file>`. Write a new body file each round (`.sdlc-scratch/comment-round-<k>.md`). Post every round. A review role then runs its `record-*` command. `development` uses `handoff-to-pr-review`.

### Comment size is a contract

- Stage handoff comment: at most 2,000 characters. Say what changed, where, and the delta since the last round.
- Evidence comment (review `post-comment`, `development` handoff, `record-*` `--summary`): at most 6,000 characters. Findings first: one heading and at most three lines each. Put output in a `<details>` block of at most 15 lines.
- Many similar findings are a class: state it once, give two examples and the sweep.
- The control plane refuses an over-cap body. Check other comments with `wc -c`.
- Commands post every hidden marker. Never type one.

If you change pipeline files the repo tracks (its config, the gate workflow), commit them with the related code. Never edit the plugin; flag a pipeline problem in your handoff.

## Secrets and containers

- Never print the environment. Print variable names only, never values.
- Label every container you start `sdlc.issue=<n>`. Name compose projects, containers and volumes with your worktree basename as prefix. Tear your stack down before your handback.
- Stop only containers you started. Check `docker ps` first.
- Never prune the host. A `docker ... prune` needs `--filter label=sdlc.issue=<n>`.

## Rework and blockers

The orchestrator owns routing (`references/rework.md`). For you:

- On a genuine ambiguity, stop and put the question in your final message (`blocked`, or `needs-human` when only the operator can answer). Never guess.
- Nothing spins off a separate ticket. The owning stage fixes it.
- On a resumed finding, check it against the real code first. If it holds, fix the whole class. If it is wrong, show the evidence.
- A rework round runs every check it owes on the new head (`verification-rules.md`, "Test scope").

## Stage exit actions live in the agent files

Each `agents/<role>.md` holds its own exit actions. Worktree defaults: `/tmp/sdlc-epic-<n>`, `/tmp/sdlc-dev-<n>` (`references/parallelism.md`).
