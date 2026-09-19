# Stage playbooks — rules that bind every stage

Every stage subagent Reads this file first. Rules that bind only some roles live in:

| File | Read by |
|---|---|
| `references/design-doc-rules.md` | `product`, `product-review`, `architecture`, `design-review` (its `arch-review` half) |
| `references/verification-rules.md` | `architecture`, `lld`, `development`, `pr-review`, `design-review` |
| `references/review-fanout.md` | `design-review` |

A role not listed for a file does not read it. Your prompt gives your doc path and worktree.
Repo-specific commands (lint/build/test, host vs container, suite names, memory flags,
ports) live in the driven repo's `CLAUDE.md` and `AGENTS.md` — "the repo's own commands"
means there.

- A SubagentStart hook gives you `$SDLC` (the control plane: `python3 "$SDLC" <cmd>`), the
  plugin root that `references/…` and `templates/…` are under, and `<docRoot>` /
  `<requirements-dir>`.
- **The control plane owns every GitHub mutation, GraphQL call, worktree, force-push and
  rebase.** A Bash guard hook denies hand-run ones and names the command to use instead. It
  also limits you to your role's commands (review roles: no git writes); anything else is
  the orchestrator's — report it in your handoff.

## Per-issue docs (the source of record)

Each issue that runs a design stage gets `<docRoot>/issue-<n>/`, committed on branch
`issue-<n>`. `publish-doc` publishes a phase-Task's doc to `<docRoot>/epic-<n>/` on the
epic branch (`references/epics.md`, "Doc layout at the epic level"); a standing child's
docs reach `main` with its squash-merge.

**Exactly three filenames exist:**

| File | Written by | Required? |
|---|---|---|
| `product.md` | `product` — an Initiative's Product-Roadmap Task, or a standing-epic child | Yes, except a standing child routed past `product`. **Never for an Epic's Task** — its requirements are the Initiative's `product.md` (or the Epic's issue body, engineering-driven). |
| `architecture.md` | `architecture` — an Epic's Architecture-phase (or revision) Task, or a standing-epic child | Yes, except a standing child routed past `architecture` |
| `lld.md` | `lld` — an Epic's LLD-phase Task only | Always; one `## Task` subsection per Task the Epic will run |

- `development`, `arch-review`/`lld-review` and `pr-review` write no doc file. Review
  findings live in the issue comment thread. `development`'s record is the **PR
  description** (what was built and why); its evidence is the `record-local-ci`
  attestations, pinned to a head SHA. Never commit self-reported pass/fail numbers.
- Any other file under `<docRoot>/issue-<n>/` is a defect. If you think a fourth doc is
  needed, say so in your handoff; do not create it.
- Write each doc so the next stage can work from it alone, without the comment history.
  A doc may hold your working checklist; never use alternate filenames.
- Every `## Task` subsection of an Epic's `lld.md`, and a standing child's
  `architecture.md`, carries a `## Footprint` section in the exact parseable shape from
  `references/epics.md`, "How to size the Tasks": backticked paths, one per bullet.

## Citation discipline — every stage, without exception

- **Anchor to something stable:** a section heading (`"### Shared files"`), an exact
  quoted test title, or a grep-anchored quote. A line number is at most a hint next to
  the anchor, never the anchor.
- **In a document that ships** (runbook, HLD, anything under the repo's doc/ops trees),
  use the grep-anchored quote alone, with no line number. Line numbers are fine as
  hints in working evidence (an evidence log, a review comment).
- **Never cite a file you did not open in this session.** Not from the issue body, a
  prior stage's doc, or memory. Issue-body line numbers are usually stale.
- **Resolving a doc from a branch:** `git fetch origin` first, then confirm the ref is
  reachable with `git merge-base --is-ancestor <sha> origin/<branch>`. An unreachable
  SHA means a superseded draft. State which ref you resolved and how.
- **To see what a commit changed**, use `git show <sha> --stat` or three-dot
  `git diff origin/main...<branch>`. Never use a two-dot range: it attributes merged
  `origin/main` commits to the branch.
- **A stale citation in a doc about to merge is a real finding**, not a nit.
- **When `sync-branch` pulls a sibling's work onto a branch whose docs cite that
  sibling's files, re-check those citations before the PR merges.** Nothing re-checks
  them after a merge.

### Attribution is falsifiable — run the check, do not recall it

Run this as a mechanical pass before every handoff:

- **Every quoted string must be reproducible**: `grep -F "<the quoted words>" <cited file>`
  must hit. If it doesn't, fix or delete the quote. Keep quotes short enough to survive
  reflowing.
- **The delegation prompt is not a citable source.** A constraint that reached you only
  through your prompt goes in the document's own voice, unquoted and unattributed.
- **A fenced block presented as command output must be bytes you captured.** Redirect
  to a file and paste from it, or cite the `record-local-ci` attestation. Prose saying
  what a command showed is fine.
- **Generate every "untouched" / "unchanged" / "out of scope" claim from the diff**
  (`git diff --name-only origin/<base>...HEAD`) when you write the sentence, and
  re-check it before handoff.

## Subagents finish in one turn — never park awaiting a wake

Nothing re-invokes a subagent after its turn ends. Finish everything in-turn.

- Run long commands (integration suite, container/image build, e2e suite) with
  `run_in_background` and wait on them in-turn with the Monitor tool. Foreground
  `sleep` is blocked.
- **Your final message declares exactly one terminal state** — the `SDLC-RESULT`
  outcome below. A final message whose last act
  is waiting ("I'll pick this up when the run lands", "monitoring for completion") is a
  stall, however much work came before it. If you cannot wait in-turn, say so and name
  what you need; that is a terminal state.
- **Orchestrator:** check every returning agent's final message for this shape before
  acting on it. An agent that declared waiting has not finished, and its issue is not at
  the stage its handoff implies. Re-message it with the result it was waiting for.

### The handback is terse — the detail already shipped

Your final message to the orchestrator is for routing. The evidence already lives in
the PR description, issue comment, or committed doc. Send these fields and nothing else:

- **HEAD / PR:** the head SHA, and the PR number if one exists.
- **BLOCKER:** one line, only for `blocked` / `needs-human` / `failed` — the named
  blocker, the exact question, or the failed command.
- **DETAIL:** a link to the PR description or issue comment that holds the evidence.
  Send the link, not the evidence.
- **Last line, always:** `SDLC-RESULT: {"issue": <n>, "stage": "<stage>", "outcome": "<outcome>"}`
  — `<n>` your unit's number, `<stage>` your role (`product`, `arch-review`, …).
- **Standing-epic child only, optional:** when you judge the default next stage
  unnecessary, add `"next": "<stage>"` (a later stage, or `merge` to skip `pr-review`) and
  `"why": "<≤ 120 chars>"` — e.g. `"next": "development", "why": "no new interface"`. A
  recommendation; the orchestrator decides.

| `outcome` | Meaning |
|---|---|
| `done` | An authoring stage finished and ran its exit actions |
| `clean` / `rework` | A review's verdict (CONDITIONAL ACCEPT → `clean`) |
| `blocked` | Cannot finish without something named outside your stage: a genuine ambiguity an earlier stage owns, a design that does not fit, a dependency, a rejected push |
| `needs-human` | Stopped for a decision only the operator can make (an escalation, a scope question) |
| `failed` | A command or exit action failed and you could not recover |

A SubagentStop hook refuses your stop until the last message carries a valid line.

No command tables, logs, restated diff, or "what I checked and found fine" inventory.
**Orchestrator:** a handback that inlines evidence is a finding. Ask for a terse re-send
before acting, then route on the fields.

A fan-out child reporting to its parent reviewer follows the same contract, capped at
1,200 characters (`references/review-fanout.md`, "A fan-out child's reply is terse too").

## Commenting discipline

Comments are the human's visibility record and the crash-resume point. They are not the
source of record: that is the committed docs, the commits, and the issue body for durable
requirements. Keep comments to a pointer plus a summary.

- **The start comment is mandatory:** `🚧 Picking this up — <role> stage starting.`,
  posted before delegating, every time, by a script and never hand-typed. `claim` /
  `start-stage` / `pass-gate` post it for the roles they claim. For a review role, the
  orchestrator posts it with `transition <n> --expect-stage <role>` or
  `start-comment <n> --role <role>`. Never post it twice.
- Comment per milestone, not per step. Post at least one comment per stage (the
  handoff) and at most one per genuine milestone.
- Link to the doc; don't paste it. Give a few sentences plus the doc path and commit SHA.
- The stage's last comment carries real information, not just a marker.
- A fresh session must be able to tell from comments plus docs where a crashed run
  stopped.

### Posting a handoff comment

`product`, `architecture` and `lld` post theirs with
`python3 "$SDLC" post-comment <n> --role <your role> --body-file <file>` — write the text to a
file first. It tags the comment for you, refuses an over-cap body or an issue you do not hold,
and is the only way these roles comment. The stop hook refuses a `done` finish until it has
succeeded this round, so a rework or gate-feedback round posts again. Review roles and
`development` use their `record-*` / `handoff-to-pr-review` commands instead.

### Comment size is a contract

- **Stage handoff comment: ≤ 2,000 characters.** Say what changed, where the doc/commit
  is, the delta since the last round, and the marker.
- **Evidence-carrying comment** (`product-review`, `arch-review`/`lld-review`,
  `pr-review`, `development`'s handoff): **≤ 6,000 characters.** Findings first, each as
  one heading plus at most three lines (what, why it matters, the fix — described, not
  applied). Non-blocking findings get one line each; the verdict gets one line. Put
  command output and quotes in a `<details>` block trimmed to the lines that prove the
  point (≤ 15 lines per block). Keep the Scope/Verification section to ≤ 3 lines: what
  you read and ran.
- Don't restate the document, the diff, or the previous round. If you have more to say
  than the cap allows, you have found a class: state it once, give two exemplars and the
  sweep that finds the rest. Cut the "what I checked and found fine" inventory first.
- The control plane refuses an over-cap `--summary` / `--reason` / `--reply` / `post-comment`
  body; check any other comment you post yourself with `wc -c`.

Each handoff comment ends with the hidden marker
`<!-- stage-transition: <from-role>-><to-role> @ <ISO8601> -->`. The script posts it
wherever a script command owns the transition (`open-dev-pr`, `handoff-to-pr-review`,
`open-gate`, `pass-gate`, `skip-gate`).

If a stage changes pipeline files the driven repo tracks (its `sdlc-pipeline.config.json`,
the gate workflow), commit that with the related code. Never edit the plugin itself; flag
a pipeline problem in your handoff.

## Secrets and containers

- **Never print the environment** (`env`, `printenv`, `set`, `process.env`, a debugger's
  env dump): transcripts persist. Print variable **names only**, never values.
- To see which credentials a profile configures, list names with a pattern that allows
  digits (`E2E_*` keys): `sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' .env.<profile>
  .secrets.<profile>`. Never read or echo a value to prove it is set.
- **Stop only containers you started.** Label every container you start
  `sdlc.issue=<n>`; check `docker ps` before any `docker stop`/`kill`, and never touch
  an unlabelled container or another issue's — it is a concurrent Task's evidence.

## Rework and blockers — what it means for you

Routing, resume messages, replacements and the escalation valve belong to the
orchestrator (`references/rework.md`). What applies to you:

- **Genuine ambiguity:** stop and put the specific question in your final message
  (outcome `blocked`; `needs-human` when only the operator can answer). Never guess.
  Stopping this way is a terminal state, not a failure.
- **Nothing spins off a separate ticket.** The owning stage fixes every pre-merge
  problem inline. You will be resumed with the finding, not replaced.
- **When you are resumed with a review finding, verify it against the real code first.**
  If it is right, fix the whole class, not just the listed instance: a same-class repeat
  bounce escalates. If it is wrong, say so and show the evidence. Never agree
  performatively or comply silently.
- **A rework round runs at full rigour:** no skipped suite, no shortened document check.

## Stage exit actions live in the agent files

| Stage | Its exit actions |
|---|---|
| `product` | `agents/product.md` |
| `product-review` | `agents/product-review.md` |
| `architecture` | `agents/architecture.md` |
| `arch-review`, `lld-review` | `agents/design-review.md` |
| `lld` | `agents/lld.md` |
| `development` | `agents/development.md` |
| `pr-review` | `agents/pr-review.md` |

Worktree paths use the `pipeline.worktrees` defaults: `<root>/<epicPrefix><n>` →
`/tmp/sdlc-epic-<n>`, `<root>/<devPrefix><n>` → `/tmp/sdlc-dev-<n>`
(`references/parallelism.md`).
