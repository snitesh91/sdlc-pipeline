# Parallelism, worktrees, and git-conflict handling

Referenced from `SKILL.md` ("Concurrency"). This file owns the full mechanics of every
concurrent path in the pipeline and every git rule that keeps them safe. The one-line
summary: **the epic's own Product/Architecture phase is strictly sequential; everything
at the child level fans out in bounded, worktree-isolated pools with mechanically
computed eligibility.**

Worktree paths in this file are the defaults from the config's `pipeline.worktrees`
block — `root` (default `/tmp`) joined with `devPrefix` (`sdlc-dev-`), `epicPrefix`
(`sdlc-epic-`) or `reviewPrefix` (`sdlc-review-`) and the unit number — so
`/tmp/sdlc-dev-<n>`, `/tmp/sdlc-epic-<n>`, `/tmp/sdlc-review-<n>`. `<repo-root>` is the
driven repo's shared checkout. Substitute your configured values wherever a path below
is quoted.

## Where concurrency exists, and where it never does

| Work | Concurrency | Detail |
|---|---|---|
| `pr-review` (within the one epic being driven) | Up to `PR_REVIEW_PARALLELISM` finished PRs at once, one worktree + one subagent each | "Parallel PR review" below |
| Epic-level `product` / `architecture` (epic-self, default profile) | **Never**, within one invocation — one epic, one architecture pass at a time | `SKILL.md`, "Epic number is mandatory" |
| Standing-epic child `product` / `architecture` | Up to `DESIGN_LANE_PARALLELISM` children of one standing epic at once, each in its own `git worktree` — `list-design-ready` computes eligibility mechanically | "Design lane" below |
| `lld` / `development` / `testing` | Up to `DEV_LANE_PARALLELISM` children of one epic at once, each always in its own `git worktree` — `list-parallel-ready` computes eligibility mechanically | "Parallel implementation lane" below |
| Rework from any review finding | **One development thread per issue, always** — a finding resumes that issue's own tracked `development` agent; several issues' rework threads may be live at once (one each), but a single issue never has two | "Rework routing stays sequential" below |

**Concurrency across two *different* epics is achieved by running two separate
`/sdlc-pipeline <epic>` invocations** (different sessions/conversations), not by anything
inside this script. GitHub-side this is always safe: each invocation's `claim`/field
writes only ever touch its own epic and children. Git-side, every branch this
pipeline touches — child `issue-<n>` *and* epic-self `epic-<n>` — lives in its own
worktree (see "Working on a branch" below), so two invocations never contend for a
checkout. Two invocations targeting the *same* epic would still race on
GitHub state — don't do that.

Note on the caps: `parallelism.devLane` and `parallelism.prReview` (default 3 each)
are each tuned against the machine's real resource cap for running test suites (memory
and CPU available to the containers or processes the suites run in); `parallelism.designLane`
(default **2**) is set below devLane because `product`/`architecture` both run the
`opus` model (`pipeline.models`) and cost more per unit than the dev lane's sonnet
stages — a token/cost cap rather than a suite-resource one. All three are
**independent pools**. `active_count` in `list-parallel-ready` also counts `issue-*`
worktrees from *other* epics' invocations (it reads `git worktree list` on the shared
repo), which makes the dev-lane cap effectively machine-global rather than per-epic —
deliberate, since the constraint it protects (the machine's suite-running resources)
is machine-global too. `list-design-ready` counts only *design-stage* worktrees toward
its own cap, so a sibling already in the dev lane never consumes a design slot (and
vice-versa). If concurrent runs start reporting resource starvation rather than real
defects, lower the caps — don't make reviews shallower.

What has *not* come back is the old label-coordinated free-for-all: context-blind
agents discovering each other through the board. The main agent stays in the loop
across every unit's lifecycle and owns every handoff; a parallel pool is the *same*
orchestrator dispatching N subagents in one parallel `Agent` call and waiting on all
of them. Resuming an earlier stage's subagent is that agent picking back up, never a
second one running alongside it.

## Parallel implementation lane — mechanical eligibility, worktree-always

Applies to children at `lld`/`development`/`testing`: a **normal,
already-`epic:architected` epic**'s children (past the epic-level design phase), and a
standing-epic child once it reaches those stages. A standing-epic child's earlier
per-issue `product`/`architecture` fans out through the **design lane** instead (see
"Design lane" below), not this one.

**Eligibility is computed by the script, never hand-tracked:**

```bash
python3 "$SDLC" list-parallel-ready <epic> \
  --repo-path <repo-root>
```

Returns up to `DEV_LANE_PARALLELISM` new candidates safe to start or resume right now,
each already checked against:

1. **Not already active** — excludes any child whose `issue-<n>` branch has a live
   `git worktree` (`git worktree list --porcelain`, read fresh every call — this is
   what makes `active_count` correct even after a crashed session).
2. **Not `blockedBy`** anything still open (native relationship).
3. **Not needs-human, not gate-pending, and not stuck `in-progress` with no worktree**
   (that last case is a crashed run — route it through `next-action`'s `resume`
   outcome, not this command).
4. **Footprint doesn't overlap** any currently-active child's, or any higher-priority
   candidate already selected earlier in the same call — read off the required
   `## Footprint` bullet list in the child's own committed `lld.md`/`architecture.md`
   on `origin/issue-<n>` (see `references/epics.md`, "How to size the children", for
   the format contract). A child whose branch exists but carries no parseable
   footprint is excluded, not assumed safe.

The command refuses a non-epic argument, returns empty (with a `note`) for an
`epic:legacy` or not-yet-`epic:architected` epic — same guard `next-action`'s children
loop applies — and runs `git fetch origin` first so every origin-ref read (footprints,
branch existence) reflects current remote state.

**Bootstrap rule — a never-started child needs no footprint.** A fresh child (Stage
`lld`, or no Stage value yet, with no `origin/issue-<n>` branch) has no committed
`lld.md` to declare a footprint — the footprint is written *by* the `lld` stage this
command exists to start, so requiring one would deadlock the lane. Starting `lld`
itself is safe: it writes only that issue's own `<docRoot>/issue-<n>/`
folder, which cannot collide with a sibling; that folder stands in as the child's
footprint for the call's collision bookkeeping. From the next call onward (branch
exists), the real committed footprint is required. `lld-review` independently checks
the freshly declared footprint against active siblings before `development` starts —
that's the check that catches a bootstrapped child whose real footprint turns out to
overlap one.

**Ordering rule — worktree before claim.** When starting a child the orchestrator
creates its worktree *first*, then `claim`s it. The collision set and `active_count`
are read off live worktrees; a child claimed before its worktree exists is invisible
to both for that window (and reads as a crashed run to `list-parallel-ready`). **On a
resume, base that worktree on `origin/issue-<n>` when the branch exists, never fresh
off `main`** — see "Resume base" under "Working on a branch" below.

`architecture.md`'s "Execution order" note is optional narrative for a human reader —
`list-parallel-ready` derives the ordering from `blockedBy` plus per-child footprints
every time it's called, rather than trusting a paragraph to stay accurate.

**Mechanics** — every child, solo or concurrent, always gets its own worktree (see
"Working on a branch" below). The orchestrator tracks one stage-agent line per active
child — same bookkeeping as the sequential case, just potentially more than one at a
time. Resume-based rework is unaffected: a review finding on one child resumes only
that child's own tracked agent, never a sibling's. Once a child reaches `testing`, it
enters the same `list-ready-for-review` pool as any other child.

## Design lane — standing-epic children's product/architecture fan out

A **standing** epic (profile `epicLevelPhase == false`) runs no epic-level design
phase; each child runs its own full `product`→`architecture`→`lld`→`development`→`testing`
flow on its own issue number/branch/worktree. Without a pool query for the design
stages, the orchestrator could only run one child's `product`/`architecture` at a time
— serializing all design work on a backlog of 20+ children. The design lane fixes
that:

```bash
python3 "$SDLC" list-design-ready <epic> \
  --repo-path <repo-root>
```

Returns up to `DESIGN_LANE_PARALLELISM` (config `parallelism.designLane`, default
**2**) children currently at `product` or `architecture` that are safe to start or
resume right now, each checked against the **same eligibility gates as the dev lane**:

1. **Not already active** — excludes any child whose `issue-<n>` branch has a live
   `git worktree`.
2. **Not `blockedBy`** anything still open.
3. **Not needs-human, not gate-pending, and not stuck `in-progress` with no worktree**
   (that last case is a crashed run — route it through `next-action`'s `resume`).

There is **no footprint check** (the one gate the dev lane adds that this one does
not): a standing child's `product`/`architecture` writes only that child's own
`<docRoot>/issue-<n>/` docs folder, which cannot collide with a sibling's, so there is
no shared-source overlap to verify. `active_count` counts only worktrees whose child
is *itself* in a design stage — a sibling that has moved on to the dev lane holds a
worktree but is the dev lane's slot, on the dev lane's cap, never a design slot.

**Standing-profile only.** For a default-profile epic (`epicLevelPhase == true`),
`list-design-ready` returns empty with a `note`: that epic's `product`/`architecture`
is the epic-level phase — a single epic-self unit run in the `epic-<n>` worktree,
strictly serial, never fanned out. The gate is the resolved profile's `epicLevelPhase`,
not the hardcoded `epic:standing` label, so a client's own label→profile mapping is
honoured. The command also refuses a non-epic argument and returns empty for an
`epic:legacy` epic, and runs `git fetch origin` first so the active-worktree read
reflects current remote state.

Mechanics are identical to the dev lane — worktree-before-claim, one stage-agent line
per active child, resume off `origin/issue-<n>`. `worktree-add <n>` stands up a
standing child's `issue-<n>` worktree the same way regardless of which stage it is in.

## The machine's resource cap is the real cap — serialize suite-heavy stages

`DEV_LANE_PARALLELISM` and `PR_REVIEW_PARALLELISM` bound *agents*, not *suite runs*.
The binding constraint is whatever memory and CPU the machine actually gives the
containers or processes the suites run in (a container runtime's VM allotment, minus
whatever the standing dev stack already holds), and two stages that each run a real
suite will starve each other well before either cap is reached.

This has been observed, not inferred: a full integration suite run single-process has
been OOM-killed mid-`development`, and the stage then reported its blast radius by grep
rather than by execution; two agents independently rediscovered the same workaround
(see `references/history.md`).

The rules, so nobody has to rediscover it a third time:

- **Never run a suite that has exceeded the machine's cap single-process.** Run it in
  memory-scoped batches — the memory flag, the in-band/serial runner flag, and the
  isolated-database setup are whatever the repo's `CLAUDE.md` and the stage's agent
  definition document — and record which batch covered which directories, so the
  batches demonstrably partition the whole tree with no overlap or omission.
- **Do not start a second suite-running stage while one is live.** `testing` and
  `pr-review` both re-run real suites; so does `development` under TDD. Two children
  may sit in the dev lane concurrently, but hold the second one's *suite-heavy* stage
  until the first finishes. Waiting a few minutes beats spending an hour diagnosing a
  resource kill dressed up as a test failure.
- **At most ONE Docker-IT-heavy stage runs concurrently — even when the agent cap is
  2.** A Docker-IT-heavy stage is any `development`/`testing`/`pr-review` running the
  integration suite. A *light* stage (an `lld` or a `design-review` that is not running
  the suite) may run alongside it, so the cap-2 lane is not wasted; a second IT suite is
  not. And **the IT suite is always run backgrounded/chunked**, never as one blocking
  foreground call over the whole suite. Epic #430 lost both agents at once this way: two
  concurrent full IT suites saturated the Docker VM, each blew past the 600s stream
  watchdog, and the watchdog killed both (see `references/history.md`).
- **A resource kill is never a code finding.** If a run dies from exhaustion, say so
  explicitly, name which suites did and did not execute, and never report an unrun
  suite as passing.
- An E2E stage that needs a stack built from its own worktree should stand one up on
  remapped ports rather than reusing the shared dev stack, which serves `main` and not
  the branch under test.

### The e2e suite is not known-broken — the committed config is the working one

Several stages in one epic hit e2e failures, concluded the suite was broken, and
routed around it; one agent was killed outright by a stall watchdog doing so. A later
`lld` root-caused it and reproduced the result in both directions on the same machine:
the committed harness configuration worked, and the obvious "fix" (switching the
container's networking mode to reach the host-side dev server another way) was the
thing that failed — plain HTTP reachability was identical under both, and the real
discriminator was a protocol upgrade the alternative network path could not complete
(see `references/history.md`).

The rules that came out of it:

- **The committed harness config is presumed working.** An e2e failure is a finding to
  investigate, not a known-broken suite to work around.
- **Reproduce in both directions before "fixing" the harness.** A change to how the
  test container reaches the application under test must be shown to pass where the
  committed config passes; an alternative that returns 200 on a plain GET has proved
  nothing about the client-side session the specs actually need.
- If it genuinely cannot run, say which surfaces are therefore unproven — never
  substitute a build or a `--list` (see `references/stage-playbooks.md`,
  "Compile-checking is not verification").

**Long commands need an explicit watchdog.** An agent's default ~120s tool-call timeout
kills a full e2e run long before the harness's own internal timeout applies — that is
what the stall watchdog fired on. Any stage running the e2e suite, a full integration
batch, or a container image build must use `run_in_background` or an explicit outer
timeout of at least 600s. Tell the agent this in the delegation prompt; it cannot
discover it without dying first.

## When an agent dies mid-stage

Agents die mid-stage for reasons unrelated to the work — an API error, the machine
sleeping, a stall watchdog — and in every recorded case the work survived because it
had been committed, and the same recovery worked (see `references/history.md`):

1. **Inspect the worktree first, don't ask the agent.** Commits ahead of `origin`,
   `git status --porcelain`, whether a PR already exists.
2. **Resume the *same* agent** with `SendMessage` — its context survived the tool
   failure — and tell it explicitly: **treat nothing as verified; re-run every check
   from scratch.** A half-finished stage's own claims about what it verified are the
   least trustworthy thing in the worktree.
3. **Never force-push, and never authorise one on the agent's word.** A resumed agent
   that hits a push rejected by an intervening rebase should stop and ask instead of
   forcing; `--force-with-lease` is authorised only after the branch is verified a
   strict content-superset of what it would overwrite. "Push rejected → stop and
   report" is already the standing rule (`SKILL.md`, Step 3) — this is what it looks
   like when it works.

### Commit and push after every step — the unit of loss is the unbanked step

"The work survived because it had been committed" is the whole recovery story above,
and one epic driven on a machine that kept sleeping turned it from an observation into
a rule: an agent that batched — investigate everything, then apply one large edit at
the end — lost everything, repeatedly, while the same agent switched to one finding,
one commit, one push banked every step across many more interruptions and lost
nothing (see `references/history.md`).

So when dispatching any stage into an unstable session, say it explicitly: **commit and
push after each self-contained step; never hold a batch to the end.** Prefer several
small `Edit` calls over one whole-file rewrite — smaller, and it fails loudly rather
than silently. Order the work cheapest-and-most-certain first, so the highest-value
pieces bank earliest.

The orchestrator's half: **inspect the worktree before every resume and tell the agent
what actually survived** — staged-but-uncommitted work, unpushed commits, an untracked
file that has now survived three cut-offs on luck alone. An agent that has to
rediscover its own state spends the turn it was given on that instead of on the work.

### Stopping a fan-out parent does not stop its children

`TaskStop` on an agent that has fanned out kills the parent only. Its axis subagents
keep running and **report to the orchestrator** when they finish, minutes later, under
their own task ids.

This has happened: a design review stopped mid-verification to free the lane had one
axis return afterwards carrying a large body of fully verified work — a byte-identical
re-derivation, a permissions sweep with a positive control, every quote checked against
a grep-anchored locator — none of it in the parent's verdict, because there was no
verdict (see `references/history.md`).

Two consequences, both the orchestrator's job:

- **Read what arrives and salvage it.** Post the verified parts to the issue as
  *evidence, not a verdict*, and say plainly that the review still stands at zero
  rounds. The next round is told not to re-derive it. Discarding it pays for the same
  work twice.
- **Check for artifacts after any kill.** A stopped agent never runs its own cleanup.
  An interrupted review has left stray state objects in live cloud resources from a
  probe that had contaminated its own control. Sweep whatever the stage could have
  touched — cloud resources, worktrees, temporary containers — before declaring the
  lane quiet.

A kill is therefore never instantaneous or free. Prefer letting a review finish when
the difference is minutes.

### Hold the waiting yourself

A subagent cannot wait across turns: parked on a poll, it burns a full turn per wake
and re-park, learning nothing. That pattern has consumed a large share of a stage's
token budget across several cycles while an hour-long e2e run ground on (see
`references/history.md`).

When a stage is genuinely blocked on a long external thing — a container build, a full
suite, a stack coming healthy — **the orchestrator runs the poll** (`run_in_background`
with a generous window) and resumes the agent once, with the result. Better still, have
the agent launch the long job as a **detached, daemon-managed container** so it survives
the agent's own turn deaths, then hand back the outcome — writing its report **under a
bind mount** rather than to a path inside an ephemeral (`--rm`) container, where
earlier runs' output has vanished with the container.

## Git-conflict handling — layered, not just "shouldn't happen"

- **Prevention** is the footprint-overlap check above — non-overlapping footprints
  between siblings is the pipeline's stated design goal (see `references/epics.md`,
  "How to size the children"), not a constraint invented for parallelism.
- **Early detection**: `sync-branch` runs before every stage transition on every
  active worktree (see "Keeping a branch current" below), so a conflict that slips
  through surfaces mid-development — while the responsible agent still has full
  context — not at final merge.
- **Handling**: `sync-branch` returns `{"synced": false, "conflict": true,
  "conflicting_files": [...]}` at exit 0 when reconciling with `origin/main` hits a
  real merge conflict (actual unmerged paths — any other merge failure still raises
  loudly; the worktree is left clean, merge aborted). Treat it exactly like any other
  rework finding (`references/stage-playbooks.md`, "Rework and blockers"): resume that
  child's own `development` agent with the conflicting files, have it resolve in its
  own worktree, re-run `sync-branch`. Track as its own escalation-valve pairing
  (`sync-branch-conflict` <-> `development`, per child) — third conflict on the same
  pairing without resolution means `mark-needs-human`. `sync-branch` itself posts a
  `<!-- sync-conflict: ... -->` marker comment on the issue for every conflict, and
  `sdlc_next.py pairing-counts <issue>` reads the strike count back mechanically —
  the counter survives crashed sessions and fresh continuous-mode agents.
- **Merge-time freshness gate**: `merge-pr` refuses — as a structured exit-0 result
  (`"merged": false, "behind_main": N`), not an error — whenever the branch is behind
  `origin/main`. Green CI on a stale base proves nothing about the combined state:
  with the parallel lane, two sibling PRs can each be green independently yet break
  `main` together (a semantic conflict no textual merge check catches). The response
  is mechanical: `sync-branch` (the push re-triggers CI), wait for green, re-run
  `merge-pr`. A `sync-branch` conflict at this point routes through the conflict
  handling above.
- **Merge-time backstop**: if GitHub itself reports the PR non-mergeable,
  `gh pr merge` exits nonzero and `merge-pr` raises loudly — treat that like a
  `sync-branch` conflict: resume `development` to reconcile, don't blindly retry.

## Parallel PR review — reviews fan out, rework stays sequential

`pr-review` is a decoupled pool: any PR that `testing` has passed sits waiting until a
review agent picks it up, and up to `PR_REVIEW_PARALLELISM` of them can be reviewed at
once, each in its own worktree. Reviews have **zero** dependency on each other; the
cap is tuned purely against the machine's suite-running resource contention (each
review re-runs the real test suite).

```bash
python3 "$SDLC" list-ready-for-review <epic>            # up to PR_REVIEW_PARALLELISM PRs
python3 "$SDLC" list-ready-for-review <epic> --limit 1  # one-off override
```

`<epic>` is required — the pool is scoped to the epic being driven. Returns
`ready_for_review` (issue, PR, branch, title per entry) plus `skipped` naming every
Stage=`Testing` child excluded and why. Read-only: never claims, never comments.

An issue is listed when **all** hold: open child of `<epic>`, Stage = `Testing`, not
needs-human/gate-pending, an **open draft PR** on `issue-<n>`, `testing`'s handoff
marker posted, and no `pr-review` outcome recorded since that marker.

### The two queue markers

Neither Stage nor Pipeline Status can distinguish "awaiting review" from "already
reviewed, dev is fixing it" — two comment markers do:

```
<!-- stage-transition: testing->pr-review @ <ISO8601> -->     # queued for review        (handoff-to-pr-review)
<!-- pr-review-outcome: clean|rework:<pr> @ <ISO8601> -->     # a review already ran     (record-pr-review)
```

**Both are posted by the script, never hand-typed** — the marker *is* the queue, and
hand-written forms historically came out mangled (see `references/history.md`).

- **`testing` passes** → `sdlc_next.py handoff-to-pr-review <issue> --pr <pr> --summary "..."`
  — **every** time testing passes, including after a rework round; a fresh handoff
  marker is what makes an issue reviewable again after a recorded `rework` outcome.
  Changes no fields — queued-for-review is not a new state.
- **`pr-review` finishes** → `sdlc_next.py record-pr-review <issue> --pr <pr>
  --outcome clean|rework --summary "..."` — its last action, before `merge-pr` on a
  clean verdict or before resuming `development` on findings. Record the clean path
  too: the merge can be delayed by CI, and until the issue closes it would otherwise
  still look unreviewed.

### How the orchestrator runs a review pool

1. `list-ready-for-review` → up to N PRs. Zero is the common, unremarkable case.
2. **One worktree per PR, checked out detached at `origin/issue-<n>`** — deliberately
   **not** a local `issue-<n>` branch:
   ```bash
   git -C <repo-root> fetch origin
   git -C <repo-root> worktree add --detach /tmp/sdlc-review-<n> origin/issue-<n>
   ```
   Detached matters: `issue-<n>` must stay free for `development`'s own worktree to
   hold that branch if this review lands findings and rework resumes. A review
   worktree holding that branch is precisely the collision `git worktree add`/
   `sync-branch` abort on. (Detached worktrees carry no branch line in
   `git worktree list --porcelain`, so they correctly don't consume dev-lane slots.)
3. `start-comment <issue> --role pr-review` for each, then dispatch one review
   subagent per PR in a **single parallel `Agent` call**, each with its own worktree
   path. Same `sdlc-pr-review` agent type, prompt, process, and model as the
   sequential case.
4. **Reviews stay empirical, not diff-only.** Each agent re-runs the real test suite
   and independently re-verifies the claims in `development.md` and in `testing`'s
   handoff comment — that is what has caught real authorization bypasses that a
   diff-only read passed (see `references/history.md`). If the suites starve each
   other, lower `PR_REVIEW_PARALLELISM` — don't make the reviews shallower.
5. Each agent finishes with `record-pr-review`, then the normal outcome handling
   (`references/stage-playbooks.md`, "pr-review").
6. **Remove each worktree when its review ends**, crash included
   (`git worktree remove <path>` / `git worktree prune`). A review that installed
   dependencies to run the suite leaves untracked files behind, so this one needs
   `--force` — unlike a dev worktree, nothing of value can live in a detached review
   checkout. (Detached worktrees consume no dev-lane slot either way; this is disk
   hygiene.)

### Rework routing stays sequential — this is not negotiable

A parallel review finding defects does **not** spawn a second concurrent development
thread *for that same issue*. The fix resumes that issue's own tracked `development`
agent, in that issue's own worktree — one active development thread per issue, always.
If several reviews land findings on the same issue at once, those rework requests
queue behind whatever that issue's development thread is already doing. Two
*different* issues can each have their own development thread concurrently; a single
issue never has two. The escalation valve (3 bounces with the incumbent, a context-reset
replacement for 4–6, `needs-human` at 6) and the test-only-findings exception all apply
unchanged, per pairing. A context-reset replacement is still one thread: `TaskStop` the
incumbent before dispatching, and the replacement takes over the same worktree.

## Working on a branch — worktree-always, for every branch this pipeline touches

**Epic-self work (`product`/`architecture` on `epic-<n>`) gets its own worktree too**
— one uniform rule instead of two. (It used to use the shared checkout; that was the
last second code path, and the only remaining way two invocations could collide.)
Created by the epic's first stage:

```bash
git -C <repo-root> worktree add /tmp/sdlc-epic-<n> -b epic-<n> origin/main
# or, resuming a crashed run / later stage on an existing branch:
git -C <repo-root> worktree add /tmp/sdlc-epic-<n> epic-<n>
```

The branch is fully done once the epic reaches `epic:architected` — remove the
worktree then; nothing works on `epic-<n>` afterward, except a second Gate B round
from a deviation escalation, which stands the worktree up again on this same branch.

**Land the epic's docs on `main` at that point, and delete the branch.** An
`architecture.md` that lives only on `epic-<n>` is reachable only by SHA, which is how
a `pr-review` has come to quote a superseded draft; and an epic branch created as an
**orphan** with no merge base can never be merged at all — its whole tree is a stale
snapshot of the repo (see `references/history.md`). Create the epic branch from
`origin/main` (`worktree add <path> -b epic-<n> origin/main`), never as an orphan, and
open a docs PR for `epic-<n>/architecture.md` once the epic is architected.
`check-epics-closeable` reports anything still missing under `docs_missing_from_main`,
but that fires only at the very end — landing it at `epic:architected` is the intended
path. The shared checkout stays on `main` and is never checked out to a pipeline
branch — which also means nothing this pipeline runs can collide with it.

**Branch from `origin/main`, never local `main`.** Both commands above (and the child
command below) say `origin/main` deliberately. The shared checkout is never checked out
to a pipeline branch and nothing ever pulls it, so **its local `main` is stale by
construction** and drifts further for the whole session. Worktrees created from local
`main` have started children on a base several commits behind — one that did not
contain their own epic's merged `architecture.md`; `sync-branch` reconciled it, but
only after the branch existed (see `references/history.md`). Fetch first if in doubt;
the cost is a second.

**Every child issue — normal or standing epic, any stage — gets its own
`git worktree`**, created the first time any stage touches it (and *before* the
`claim`, per the ordering rule above):

```bash
git -C <repo-root> worktree add /tmp/sdlc-dev-<n> -b issue-<n> origin/epic-<parent>
# or, resuming a crashed run / later stage on an existing branch — base it on the
# PUSHED branch, never fresh off main:
git -C <repo-root> worktree add /tmp/sdlc-dev-<n> -B issue-<n> origin/issue-<n>
```

**Resume base — `origin/issue-<n>` when it exists, never `main`.** A resumed child's
first stage already pushed its work to `origin/issue-<n>` — its `lld.md` (and, later
stages, its code). A worktree recreated fresh off `origin/epic-<parent>`/`main`
(e.g. the *create* form above, or a bare `worktree add <path> -b issue-<n> origin/main`)
silently starts from a tree that has none of it, and the resumed agent rewrites work
that already existed. This — not a crash losing the file — was the real re-run bug
(`references/history.md`, 2026-09-04): `lld.md` survives a crash because it is on
`origin/issue-<n>`; what dropped it was branching the resume off the wrong base. So on
any resume, check `origin/issue-<n>` first and base the worktree on it (`-B issue-<n>
origin/issue-<n>` recreates the local branch at the pushed tip); use the epic-branch
create form only for a genuinely first-touch child with no `origin/issue-<n>` yet.

Which stage creates it: `lld` for a normal-epic child; `product` for a standing-epic
child; `architecture` for a bug fast-tracked there. Branch-writing commands
(`sync-branch`/`pass-gate`/`merge-lld-doc`/`close-epic`) resolve this worktree
themselves, or create an ephemeral one — see "`--repo-path` means any path inside the
repository" under "Keeping a branch current"; there is no main-checkout path for any
branch.

**Releasing the worktree at a stopping point is the script's job, not the
orchestrator's memory.** `mark-needs-human`, `mark-blocked` and `merge-pr` each call
`release_worktree` themselves and report the outcome under a `worktree` key in their
JSON. It **never destroys work**: a worktree with uncommitted changes, with commits
not yet on `origin/<branch>`, or one that is the repository's main checkout, is left
alone with a stated reason. Pass `--repo-path` when the shared checkout isn't the
current directory. The only removal still done by hand is the *review* pool's
detached worktrees (step 6 above) and tidy-up when an invocation simply ends.

**Slot accounting self-heals independently of that.** `list-parallel-ready` does not
count an `issue-<n>` worktree whose issue is closed, `needs-human`, gate-pending, or
blocked — it reports those under `stale_worktrees` instead. Both layers exist because
one didn't hold: a child parked `needs-human` with its worktree left on disk made the
lane read full, and the rest of that invocation ran at a fraction of capacity with no
error anywhere to notice (see `references/history.md`).

A crashed session still leaves a worktree behind for the next
`next-action`/`list-parallel-ready` call to find and reuse. When reusing a leftover
worktree, `git fetch origin` and compare its HEAD against `origin/issue-<n>` first —
a stale worktree can otherwise push from an outdated base.

Every stage from creation onward works in that worktree, on that branch, and pushes
its commits to `origin/<branch>` as it goes — don't leave commits unpushed between
stages; origin is the recovery point if a session ends mid-unit.

**After `pass-gate`, the branch is already synced** — `pass-gate` (via
`git_reconcile_branch`) fetches, merges `origin/main` in, and pushes back before it
returns. No `git checkout main && git pull` detour is ever needed; `git fetch origin`
updates the `origin/main` ref regardless of what's checked out.

**A child branches from its epic's integration branch, not from `main`.** Use
`origin/epic-<parent>` — the epic's own branch, which already carries every sibling
merged so far. `origin/main` is correct only for a standing epic's child or a top-level
issue with no parent; `integration_base()` in the control script is the authority, and
`sync-branch` reconciles against whatever it returns rather than always `origin/main`.

## Keeping a branch current — sync before every stage, not just gates

`main` can move at any moment a sibling's PR merges. **Immediately before delegating
to *any* new stage's subagent — every stage transition, not just a gate pass — run:**

```bash
python3 "$SDLC" sync-branch <n> [--unit epic]   # auto-resolves the branch's worktree
```

The one skip: when the step just run was itself `pass-gate`/`skip-gate` — both
reconcile internally, so an immediate second call is a redundant (harmless) no-op.

**Never hand-type the fetch/checkout/merge/push sequence.** Typing it directly caused
a real incident: a `git checkout` failed silently in a shell sequence that didn't stop
on error (branch checked out in another worktree), and the merge/push ran against
whatever was still checked out — corrupting that branch. `sync-branch` aborts loudly
on that same collision.

**A real merge conflict is a valid, non-crashing result** — see "Git-conflict
handling" above for the full routing. Any other git failure still raises loudly.

**Always a merge, never a rebase — because of squash-merging.** `merge-pr`
squash-merges every development PR; a squashed commit shares no ancestry with the
history it replaced. Rebasing another still-open branch onto a `main` containing that
squash would replay commits `main` already has under different SHAs — spurious
conflicts or duplicated diffs. A merge only compares tree content, never ancestry.
This is also why gate PRs are never squash-merged: so a later `sync-branch` merge of
`main` back into that same branch is a clean no-op, not a phantom diff.

`--repo-path` means **"any path inside the repository"** on every branch-writing
command (`sync-branch`, `pass-gate`, `merge-lld-doc`, `close-epic`) — it is where the
worktree map is read from, not the checkout to write in. The command then operates in
the branch's own live worktree, or in an ephemeral one it creates and removes when
nothing holds the branch; it never writes in the main checkout (see "Concurrent
multi-epic isolation" below). `verify-exit` is read-only and still resolves the
branch's live worktree for its doc listing; `open-gate` reads `origin/<head>` after a
fetch and needs no working tree at all.

**The main checkout must be on `main`, and since 2026-09-12 the control plane refuses
to make it otherwise.** Until then a `sync-branch`/`merge-lld-doc`/`pass-gate` run
from an orchestrator shell whose cwd was the main checkout could leave that checkout
*on* a pipeline branch (`epic-<n>`, `issue-<n>`), which forces the branch's real
`/tmp/sdlc-*` worktree into **detached HEAD** — twice on epic #430 (once under a live
`development` agent, so its commits and uncommitted files sat on a detached HEAD), four
more times on epics #159/#365, and once onto a *peer session's* branch. Every one of
those commands now takes the branch's lock and works in its own worktree; if it finds
the branch already checked out in the main checkout it **refuses** with the recovery
recipe rather than operating there. Recovery when the state is already present (no
work lost): commit WIP on the detached HEAD → `git checkout main` in the main checkout
to free the branch → `git checkout -B issue-<n>` in the `/tmp` worktree (carries the
WIP) → `git push origin issue-<n>`; verify with `git merge-base --is-ancestor` that
the detached HEAD descends from the branch tip before trusting it. Incidents: memory
`ops_main_checkout_steals_branch`, `sdlc_merge_lld_doc_branch_steal_bug`.

## Publishing lld.md to the epic branch — durable design, sibling visibility

A normal-epic child's `lld.md` used to reach the epic branch only when the child's
whole pipeline merged. As soon as `lld-review` comes back CLEAN, publish it early
instead:

```bash
python3 "$SDLC" merge-lld-doc <n>   # auto-resolves the epic branch's worktree
```

Run it right after `record-design-review` (see `references/stage-playbooks.md`, the
`lld-review` exit action). It takes `<docRoot>/issue-<n>/lld.md` verbatim from
`origin/issue-<n>` and commits **only that one file** onto `epic-<parent>` as a
doc-only commit, then pushes — not a merge of the whole child branch, so none of the
child's in-progress code goes with it. Two payoffs: the low-level design is durable on
the epic branch independent of the still-open `issue-<n>` branch, and every sibling
picks it up in-tree on its next `sync-branch`, so cross-child overlap checks read the
real committed design.

**It also advances the child — advance-not-claim (2026-09-12).** Once the doc is
verified on origin, the command sets Stage to `Development` and clears Pipeline
Status, exactly as `pass-gate --unit epic` hands off to children: no `in-progress`, no
start comment, no `claim`. `development` is then a *fresh* `next-action` /
`list-parallel-ready` unit — so a single orchestrator pass can return, say, one
`development` plus the two sibling `lld`s that were `blockedBy` it, scheduled by lane
and priority instead of chained opportunistically onto whichever lld finished first.
Mechanism only: an independent child still goes `lld` → `development` on the very next
pick; this is **not** "all llds merge before any development starts".

- **Every persisted state maps to one next step** (the old chain left `Stage=LLD,
  in-progress` with a clean review marker after a crash — an ambiguous resume). Stage
  is written *before* the status clear, so the only crash window is `Stage=Development,
  in-progress`, which `next-action` reads as `resume` at `development` — the same work,
  from `origin/issue-<n>` + the published doc. A crash *before* the field write leaves
  `Stage=LLD` with the doc already on origin; the re-run lands on `up-to-date`
  (verified on origin) and still advances. A child already at `Development` is left
  untouched (`advanced: false`).
- **`sync-branch` still runs first, every transition.** Decoupling widens the gap
  between the lld merge and the development pick, so the worktree freshness rule
  ("Keeping a branch current") matters more here, not less.
- **Not advanced on a conflict or refusal** — the doc is not on origin, so the child
  stays at `LLD`; re-run once the branch is quiet.

- **Scope: normal-epic children only.** A standing-epic child (integrates into `main`,
  not an epic branch) or a parentless issue is a structured no-op at exit 0, never an
  error.
- **Idempotent, judged on origin.** The doc's blob on `origin/issue-<n>` is compared
  with its blob on `origin/epic-<parent>` — identical → `up-to-date` with
  `verified_on_origin: true`; safe to re-run. The local tree is never the reference:
  the 2026-09-12 defect was exactly a re-run comparing a working tree that already
  held the doc (committed on a stale base, push rejected) to itself and reporting
  `up-to-date` while `origin/epic-<n>` never received the file (memory
  `sdlc_merge_lld_doc_branch_steal_bug`).
- **`merged: true` is only reported after a post-push fetch shows the blob on
  `origin/epic-<parent>`.** The commit is replayed from origin's current tip
  (`checkout -B epic-<n> origin/epic-<n>`, then the doc from `origin/issue-<n>`), so a
  stale doc-only commit from an earlier rejected attempt is discarded and redone;
  unpushed local commits touching anything *else* on the epic branch make it refuse.
- **A push refused because the epic branch advanced under it** is retried once from
  the re-fetched tip; still refused → a structured `conflict` result at exit 0 whose
  reason says the doc is **not** on origin (same spirit as `sync-branch`'s conflict),
  never a crash and never a false success. Any genuine operational git failure still
  raises.

## Concurrent multi-epic isolation — N invocations on N epics, zero shared mutable state

Operator goal (2026-09-12): the skill must run N instances on N *different* epics at
once. Two shared singletons made that impossible and had to go together: the one main
git checkout, and the one shared dev Docker stack/DB/ports. A third, quieter one —
every instance reading the *same* `.github/sdlc-pipeline` copy in the main checkout —
went with them. What follows is what the control plane now guarantees, and what it
still does not.

### Git — the main checkout is never a write target

- **Every branch-writing command works in a worktree that holds its branch, never the
  main checkout.** `sync-branch`, `merge-lld-doc`, `pass-gate` and `close-epic` resolve
  the branch's live worktree from `git worktree list`; when nothing holds it (the
  normal state of an epic branch at gate-pass, doc-publish and close time) they create
  an **ephemeral** worktree at `<worktrees.root>/<ephemeralPrefix><branch>-<pid>` from
  `origin/<branch>`, operate, and remove it if it is clean and fully pushed (a retained
  tree is reported as `retained_worktree` in the result). A branch found checked out
  in the main checkout — the stolen state — is **refused** with the recovery recipe.
  A local ref carrying unpushed commits with no worktree is refused too, never reset.
- **Per-branch lock.** Each of those commands holds an exclusive `flock` on
  `<pipeline.locks.dir>/<branch>.lock` for its whole run (`waitSeconds`, default 600,
  then a `BranchLocked` error naming the file). Keyed by branch, so operations on
  different branches never contend — different epics, different children, no
  serialisation. Two sessions on the same branch queue instead of racing.
- **`open-gate` cites `origin/<head>`** after a fetch — the PR is opened against
  origin, so a local HEAD could only ever cite a SHA the PR does not contain, and
  reading origin needs no working tree.
- **What is still shared, deliberately:** the repository's object store and worktree
  map (one clone, many worktrees — that is the point), and the dev-lane / review-pool
  caps, which `list-parallel-ready` counts off the *machine's* live worktrees. Across
  N instances on one clone that makes the caps machine-wide, which is the right
  reading: the binding constraint is the machine ("The machine's resource cap is the
  real cap" above), not the invocation.

### Each unit reads its own skill copy

- **Two paths.** `$SDLC` (the control-plane script) is a **stable bootstrap path** —
  the main checkout's `.github/sdlc-pipeline/scripts/sdlc_next.py` or any fixed clone.
  It has to exist before the unit's worktree does, because it is what creates that
  worktree, so it cannot live only inside a per-epic tree; it is read-only tooling.
  `$SDLC_DIR` (the agent-facing `references/` and `agents/`) is **per unit**:
  `<worktree>/<pipeline.skill.submodulePath>`. Every stage agent reads its playbook
  and persona at the skill commit *its own branch pins*.
- **`worktree-add` initialises the submodule.** A linked worktree's submodule
  directory is empty after `git worktree add`; the command runs `git submodule update
  --init -- <submodulePath>` in the new tree and returns `skill_dir` + `skill_commit`.
  Set `$SDLC_DIR` from that before delegating.
- **`sync-branch` re-syncs it after every merge.** Merging the integration base can
  move the submodule gitlink; a merge alone leaves the working files at the *old* pin
  (`modified (new commit)` in `git status`). `sync-branch` runs the same `submodule
  update --init` after a successful reconcile — and only in a live worktree, never in
  an ephemeral one. Timing is what makes this safe: `sync-branch` fires between stage
  agents (SKILL.md, "After the subagent returns"), which is the one naturally quiet
  point per worktree. Never run it under a live agent of the same unit; nothing else
  in the pipeline needs to.
- **Consequence:** a submodule bump merged for one epic reaches another epic's agents
  only when *that* epic's branch takes the bump through its own `sync-branch`, at its
  own transition. The retro rule "merge a retrospective only when the lane is quiet"
  (SKILL.md Step 5) now scopes to the epic being bumped, not the whole machine. The
  config file (`sdlc-pipeline.config.json`) is committed on the branch, so it was
  already per-branch.
- **Git requirement — verified, and checked at runtime.** Submodules inside linked
  worktrees only isolate if git gives each worktree its own submodule gitdir under
  `$GIT_COMMON_DIR/worktrees/<id>/modules/<name>` (older gits shared one
  `.git/modules/<name>` across worktrees, and git's own `worktree` docs still call
  multiple checkouts of a superproject with submodules incomplete). Verified
  empirically on 2026-09-12 with git 2.50.1 (Apple Git-155): a linked worktree's init
  cloned into `.git/worktrees/<wt>/modules/.github/sdlc-pipeline`, checked out the
  branch's pin while the main checkout stayed on its own, and after a merge moved the
  gitlink `submodule update --init` brought the files to the new pin with a clean
  status. Rather than trust a version floor nobody could source, `init_skill_submodule`
  **asserts the per-worktree gitdir after every init** (`rev-parse --absolute-git-dir`
  must contain `/worktrees/`) and that `probeFile` is present, and refuses with an
  upgrade message otherwise. Each init is a clone of the skill repo from the URL in
  `.gitmodules` — small, and paid once per worktree.

### Per-epic isolated stack — the runtime half

`make e2e` against the shared dev stack (`localhost:3001`, the shared `bookshaw` DB)
made every e2e run depend on shared reference data and exposed it to a stray
`cleanTables()` from a concurrent backend-IT run — which blocked #509 on epic #159.
The isolation is **config-only** on the origin repo: compose is parametrised by
`COMPOSE_PROJECT_NAME`, `FRONTEND_PORT`, `BACKEND_PORT`, `DB_PORT_EXPOSE`,
`BACKEND_DEBUG_PORT` (this one *must* move too or `9229` collides), `POSTGRES_DATA_DIR`,
`ENV_FILE`, `SECRETS_FILE`. The "secret" is a plain file copy of `.secrets.dev` — the
sandbox blocks reading secret values, not copying the file — so no human step.

```bash
python3 "$SDLC" provision-epic-stack <n>     # at the epic's first touch (or before its first e2e child)
#   -> .env.epic<n> = .env.dev with distinct ports (+stride×slot, bumped while any port is bound),
#      COMPOSE_PROJECT_NAME=sdlc-epic<n>, POSTGRES_DATA_DIR=.docker/postgres-data-epic<n>;
#      cp .secrets.dev .secrets.epic<n>; runs upCommand then seedCommand; returns ports + a `use` line
python3 "$SDLC" teardown-epic-stack <n>      # after close-epic's merge
#   -> downCommand (compose down -v), rm the two profile files and the data dir
```

Both are structured no-ops while `pipeline.stack.enabled` is false, and provisioning
is idempotent (an existing profile is reused, ports read back). Tell every stage that
runs e2e for the epic to use the returned `use` line
(`COMPOSE_PROJECT_NAME=<project> make <target> PROFILE=<profile>`) and the returned
ports, never the dev ones. **Build from the code under test:** a child validating its
own e2e fixes runs `make fullstack-d`/`make e2e` *from its own worktree* with the epic
profile, since its changes are not on the epic branch until it merges; the epic-close
full run builds from the epic integration branch.

**DESIGN — deferred, not built (state it, don't fake it):**

- **One stack per epic, not per child.** Two children of one epic that both need a
  stack at the same time still share it (and the IT-heavy-stage cap keeps that to one
  at a time anyway). A per-child profile is the same recipe with `profileTemplate`
  keyed by child number; it is not wired because nothing has needed it yet.
- **Not auto-invoked.** `next-action` does not call provision/teardown; the
  orchestrator does, at the epic's first touch and after the closing merge (SKILL.md
  Steps 1–2). Wiring it into `worktree-add --unit epic` / `close-epic` was left out so
  a repo without a parametrised compose never has an epic start fail on Docker.
- **Seeding is a config string** (`seedCommand`); the origin repo's seed for e2e
  users/profiles is still the open gap recorded in `references/epics.md` ("a
  verification could not be obtained") — the stack helper runs whatever the repo
  provides and does not solve that.
- **Same-epic double invocation still races.** The lock is per branch; two
  invocations driving the same epic still collide on stage claims and the review
  pool exactly as "Epic number is mandatory" warns. A per-epic invocation lock is the
  obvious next step and is deliberately not in this change.
- **The Docker resource cap stays machine-wide.** N isolated stacks are N sets of
  containers on one VM; the "at most one Docker-IT-heavy stage" rule above is now a
  per-machine rule the N instances cannot see each other enforce.
