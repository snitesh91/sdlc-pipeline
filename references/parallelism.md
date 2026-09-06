# Parallelism, worktrees, and git-conflict handling

Referenced from `SKILL.md` ("Concurrency"). This file owns the full mechanics of every
concurrent path in the pipeline and every git rule that keeps them safe. The one-line
summary: **the epic's own Product/Architecture phase is strictly sequential; everything
at the child level fans out in bounded, worktree-isolated pools with mechanically
computed eligibility.**

## Where concurrency exists, and where it never does

| Work | Concurrency | Detail |
|---|---|---|
| `pr-review` (within the one epic being driven) | Up to `PR_REVIEW_PARALLELISM` finished PRs at once, one worktree + one subagent each | "Parallel PR review" below |
| Epic-level `product` / `architecture` (epic-self) | **Never**, within one invocation — one epic, one architecture pass at a time | `SKILL.md`, "Epic number is mandatory" |
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

Note on the two caps: `DEV_LANE_PARALLELISM` and `PR_REVIEW_PARALLELISM` (both 3) are
each tuned against Docker test-suite resource pressure, but they are **independent
pools** — worst case 6 concurrent real-suite runs. `active_count` in
`list-parallel-ready` also counts `issue-*` worktrees from *other* epics' invocations
(it reads `git worktree list` on the shared repo), which makes the dev-lane cap
effectively machine-global rather than per-epic — deliberate, since the constraint it
protects (Docker resources) is machine-global too. If concurrent runs start reporting
resource starvation rather than real defects, lower the caps — don't make reviews
shallower.

What has *not* come back is the old label-coordinated free-for-all: context-blind
agents discovering each other through the board. The main agent stays in the loop
across every unit's lifecycle and owns every handoff; a parallel pool is the *same*
orchestrator dispatching N subagents in one parallel `Agent` call and waiting on all
of them. Resuming an earlier stage's subagent is that agent picking back up, never a
second one running alongside it.

## Parallel implementation lane — mechanical eligibility, worktree-always

Applies only to children of a **normal, already-`epic:architected` epic** (a
standing-epic child's per-issue `product`/`architecture` flow is untouched; its
`development`/`testing` join the lane like anyone else's).

**Eligibility is computed by the script, never hand-tracked:**

```bash
python3 "$SDLC" list-parallel-ready <epic> \
  --repo-path /Users/nisingla/Documents/personal/owner/repo
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
itself is safe: it writes only that issue's own `docs/sdlc/issue-<n>/`
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

## Docker is the real cap — serialize suite-heavy stages

`DEV_LANE_PARALLELISM` and `PR_REVIEW_PARALLELISM` bound *agents*, not *containers*.
The binding constraint on this machine is the Docker VM (~7.6 GB, with the dev
frontend container alone holding ~2.6 GB), and two stages that each run a real suite
will starve each other well before either cap is reached.

Observed twice on 2026-08-20, epic #110: the full backend IT suite
(`npm run test:it`, 65 suites) was **OOM-killed** when run single-process — once during
`development` on #186, which then reported its blast radius by grep rather than by
execution. Two agents independently rediscovered the same workaround.

The rules, so nobody has to rediscover it a third time:

- **Never run the full backend suite single-process.** Run it in memory-scoped batches
  — `--memory=3g`, `--runInBand`, against an isolated Postgres on its own Docker
  network — and record which batch covered which directories, so the batches
  demonstrably partition the whole tree with no overlap or omission.
- **Do not start a second suite-running stage while one is live.** `testing` and
  `pr-review` both re-run real suites; so does `development` under TDD. Two children
  may sit in the dev lane concurrently, but hold the second one's *suite-heavy* stage
  until the first finishes. Waiting a few minutes beats spending an hour diagnosing a
  resource kill dressed up as a test failure.
- **A resource kill is never a code finding.** If a run dies from exhaustion, say so
  explicitly, name which suites did and did not execute, and never report an unrun
  suite as passing.
- An E2E stage that needs a stack built from its own worktree should stand one up on
  remapped ports rather than reusing the shared dev stack, which serves `main` and not
  the branch under test.

### `make e2e` works — the committed config is the working one

Three separate stages on epic #156 (#232, #233, #245) hit `make e2e` failures,
concluded the suite was broken, and routed around it; one agent was killed outright by
a stall watchdog doing so. #238's `lld` root-caused it and reproduced the result in
both directions on the same machine:

- **`--network host` + `localhost:3001` — what is committed — works.** Auth setup
  completes, 30+ specs execute.
- **Bridge networking + `host.docker.internal:3001` fails**, and it is the obvious
  "fix" that fails: Docker Desktop's gVisor network proxy cannot complete the Next dev
  server's WebSocket upgrade (`ws://host.docker.internal:3001/_next/webpack-hmr`), so
  the client never hydrates, so the sign-in modal never opens. **Plain HTTP GET returns
  200 under both** — HTTP reachability is not the discriminator, the WebSocket upgrade
  is. Do not "fix" the harness in this direction.

So: a `make e2e` failure is a finding to investigate, not a known-broken suite to work
around. If it genuinely cannot run, say which surfaces are therefore unproven — never
substitute a build or a `--list` (see `references/stage-playbooks.md`,
"Compile-checking is not verification").

**Long commands need an explicit watchdog.** An agent's default ~120s tool-call timeout
kills `make e2e` long before the harness's own `timeout --foreground 480` applies —
that is what the stall watchdog fired on. Any stage running `make e2e`, a full IT
batch, or a Docker image build must use `run_in_background` or an explicit outer
timeout of at least 600s. Tell the agent this in the delegation prompt; it cannot
discover it without dying first.

## When an agent dies mid-stage

Three agents died on epic #156 — an API error, the machine sleeping, and a stall
watchdog at 600s. In all three the work survived because it had been committed, and the
same recovery worked:

1. **Inspect the worktree first, don't ask the agent.** Commits ahead of `origin`,
   `git status --porcelain`, whether a PR already exists.
2. **Resume the *same* agent** with `SendMessage` — its context survived the tool
   failure — and tell it explicitly: **treat nothing as verified; re-run every check
   from scratch.** A half-finished stage's own claims about what it verified are the
   least trustworthy thing in the worktree.
3. **Never force-push, and never authorise one on the agent's word.** One resumed agent
   hit a push rejected by an intervening rebase and correctly stopped and asked instead
   of forcing; `--force-with-lease` was authorised only after the branch was verified a
   strict content-superset of what it would overwrite. "Push rejected → stop and
   report" is already the standing rule (`SKILL.md`, Step 3) — this is what it looks
   like when it works.

### Commit and push after every step — the unit of loss is the unbanked step

"The work survived because it had been committed" is the whole recovery story above,
and epic #98 turned it from an observation into a rule. That session lost roughly
fifteen agent turns to a machine that kept sleeping, and the two working styles
separated cleanly:

- **#274's replacement batched** — investigate everything, then apply one large edit at
  the end. It lost **everything, three times running**: three full rounds of
  verification, with the document never once modified. Only its scratch files survived.
- Switched to **one finding, one commit, one push**, it then landed **seven commits
  across nine more interruptions and lost nothing.** #284 did the same and carried ten.

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

Observed 2026-09-03: a `lld-review` on #313 was stopped mid-verification to free the
lane. One axis returned afterwards carrying ~166k tokens of verified work — a
byte-identical re-derivation of a shell-generated filter, a `setIamPolicy` sweep across
thirteen roles with `roles/owner` as a positive control, and every runbook quote checked
against grep-anchored locators. None of it was in the parent's verdict, because there
was no verdict.

Two consequences, both the orchestrator's job:

- **Read what arrives and salvage it.** Post the verified parts to the issue as
  *evidence, not a verdict*, and say plainly that the review still stands at zero
  rounds. The next round is told not to re-derive it. Discarding it pays for the same
  work twice.
- **Check for artifacts after any kill.** A stopped agent never runs its own cleanup.
  An earlier interrupted review left two empty state objects in live buckets from a
  probe that had contaminated its own control. Sweep whatever the stage could have
  touched — buckets, worktrees, cloud resources — before declaring the lane quiet.

A kill is therefore never instantaneous or free. Prefer letting a review finish when
the difference is minutes.

### Hold the waiting yourself

A subagent cannot wait across turns: parked on a poll, it burns a full turn per wake
and re-park, learning nothing. On #275 that pattern consumed ~190k tokens across
several cycles while a 53-minute e2e run ground on.

When a stage is genuinely blocked on a long external thing — a container build, a full
suite, a stack coming healthy — **the orchestrator runs the poll** (`run_in_background`
with a generous window) and resumes the agent once, with the result. Better still, have
the agent launch the long job as a **detached, daemon-managed container** so it survives
the agent's own turn deaths, then hand back the outcome. #275's suite only produced a
usable artifact once it was run that way, writing its report **under the bind mount**
rather than to a path inside a `--rm` container, where two earlier runs' output vanished
with the container.

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
cap is tuned purely against Docker resource contention (each review re-runs the real
test suite).

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
   git -C /Users/nisingla/Documents/personal/owner/repo fetch origin
   git -C /Users/nisingla/Documents/personal/owner/repo worktree add --detach /tmp/sdlc-review-<n> origin/issue-<n>
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
   handoff comment — that is
   what caught the IPv6-mapped-address bypass and the seller-status authorization gap
   (#111/#114/#130). If the suites starve each other, lower `PR_REVIEW_PARALLELISM` —
   don't make the reviews shallower.
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
git -C /Users/nisingla/Documents/personal/owner/repo worktree add /tmp/sdlc-epic-<n> -b epic-<n> origin/main
# or, resuming a crashed run / later stage on an existing branch:
git -C /Users/nisingla/Documents/personal/owner/repo worktree add /tmp/sdlc-epic-<n> epic-<n>
```

The branch is fully done once the epic reaches `epic:architected` — remove the
worktree then; nothing works on `epic-<n>` afterward, except a second Gate B round
from a deviation escalation, which stands the worktree up again on this same branch.

**Land the epic's docs on `main` at that point, and delete the branch.** An
`architecture.md` that lives only on `epic-<n>` is reachable only by SHA, which is how
#209's `pr-review` came to quote a superseded draft (`references/history.md`,
2026-08-20). `epic-110` was worse: it had been created as an **orphan** with no merge
base, so it could never be merged at all and its whole tree was a stale snapshot of the
repo. Create the epic branch from `origin/main`
(`worktree add <path> -b epic-<n> origin/main`),
never as an orphan, and open a docs PR for `epic-<n>/architecture.md` once the epic is
architected. `check-epics-closeable` reports anything still missing under
`docs_missing_from_main`, but that fires only at the very end — landing it at
`epic:architected` is the intended path.
The shared checkout stays on `main` and is never checked out to a pipeline branch —
which also means nothing this pipeline runs can collide with it.

**Branch from `origin/main`, never local `main`.** Both commands above (and the child
command below) say `origin/main` deliberately. The shared checkout is never checked out
to a pipeline branch and nothing ever pulls it, so **its local `main` is stale by
construction** and drifts further for the whole session. On epic #156 the early
worktrees were created from local `main`, four commits behind — two children started on
a base that did not contain their own epic's merged `architecture.md`. `sync-branch`
reconciled it, but only after the branch existed. Fetch first if in doubt; the cost is
a second.

**Every child issue — normal or standing epic, any stage — gets its own
`git worktree`**, created the first time any stage touches it (and *before* the
`claim`, per the ordering rule above):

```bash
git -C /Users/nisingla/Documents/personal/owner/repo worktree add /tmp/sdlc-dev-<n> -b issue-<n> origin/epic-<parent>
# or, resuming a crashed run / later stage on an existing branch — base it on the
# PUSHED branch, never fresh off main:
git -C /Users/nisingla/Documents/personal/owner/repo worktree add /tmp/sdlc-dev-<n> -B issue-<n> origin/issue-<n>
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
child; `architecture` for a bug fast-tracked there. Branch-touching commands
(`sync-branch`/`verify-exit`/`open-gate`/`pass-gate`) auto-resolve this worktree when
`--repo-path` is omitted — see the note at the end of this file; there is no
shared-checkout path for a child issue either way.

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
one didn't hold: on 2026-08-20 #186 was parked `needs-human` with its worktree left on
disk, the lane read 3/3 full, and the rest of that invocation ran at one-third
capacity with no error anywhere to notice (see `references/history.md`).

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

`--repo-path` is optional on every branch-touching command (`sync-branch`,
`verify-exit`, `open-gate`, `pass-gate`, `merge-lld-doc`): omitted, the command
resolves the branch's own live worktree from `git worktree list` itself —
deterministic, and immune to the old forgot-the-flag footgun where "." silently
targeted the shared checkout. Pass it explicitly only to override (e.g. CI's own
checkout in `gate-auto-advance.yml`). Fallback when no worktree holds the branch is
still "." — stand the worktree up first (see the creation commands above) rather than
relying on that.

## Publishing lld.md to the epic branch — durable design, sibling visibility

A normal-epic child's `lld.md` used to reach the epic branch only when the child's
whole pipeline merged. As soon as `lld-review` comes back CLEAN, publish it early
instead:

```bash
python3 "$SDLC" merge-lld-doc <n>   # auto-resolves the epic branch's worktree
```

Run it right after `record-design-review`, before `claim <n> --role development` (see
`references/stage-playbooks.md`, the `lld-review` exit action). It takes
`docs/sdlc/issue-<n>/lld.md` verbatim from `origin/issue-<n>` and commits
**only that one file** onto `epic-<parent>` as a doc-only commit, then pushes — not a
merge of the whole child branch, so none of the child's in-progress code goes with it.
Two payoffs: the low-level design is durable on the epic branch independent of the
still-open `issue-<n>` branch, and every sibling picks it up in-tree on its next
`sync-branch`, so cross-child overlap checks read the real committed design.

- **Scope: normal-epic children only.** A standing-epic child (integrates into `main`,
  not an epic branch) or a parentless issue is a structured no-op at exit 0, never an
  error.
- **Idempotent.** Identical content already on the epic branch → `up-to-date` no-op;
  safe to re-run (a re-review, a crashed session).
- **A push refused because the epic branch advanced under it** returns a structured
  `conflict` result at exit 0 (same spirit as `sync-branch`'s conflict), not a crash —
  re-run to reconcile and retry. Any genuine operational git failure still raises.
