# History — dated decisions, incidents, and why rules exist

Provenance for rules that would otherwise read as arbitrary. Newest first. Keep
entries to a few lines; the rule itself lives in the spine or its reference file —
this file records *why* and *when*.

## 2026-09-06 — SKILL.md slimmed; narrative moved here; generic-skill leaks fixed

`SKILL.md` cut from ~4.4k to ~2.9k words with no rule removed: the concurrency table,
the model-tier rationale and the delegation checklist were compressed, and incident
narrative moved to this file. Fixed on the same pass: `RETRO_WATERMARK_FILE` still
pointed at the skill's old name (`.claude/skills/sdlc-next/`), so `retro-check` under
the symlinked `sdlc-pipeline` name never found the watermark and fired every run;
issue-comment text still told humans to run `/sdlc-next`; and `parallelism.md` /
`operations.md` carried an absolute personal repo path and a personal `gh` login in a
skill that claims to be generic.

Narrative that used to live in `SKILL.md`, kept here for provenance:

- **2026-08-22 — the orchestrator takes implementation calls itself.** Set after four
  mid-epic operator asks, two of which were implementation calls the orchestrator could
  have made. Hence "escalate exactly three things".
- **Re-query the dev lane after every `merge-pr`.** Epic #156 ran the lane at 1–2 of 3
  for most of its length because a freshly unblocked child was never re-queried.
- **`sdlc-design-review` was split across tiers** (`arch-review` on one, `lld-review`
  on another) until 2026-08-28; nothing is split today, but the call-site pinning that
  made it possible stays.

## 2026-09-06 — Epic gate PRs route through the epic branch; retro watermark moves into the driven repo

Resolves the 2026-09-05 tension below, in favour of the operator's stated preference.
An epic-level gate doc (`product.md` / `architecture.md`) is now authored on a
disposable sub-branch `epic-<n>-gate-<stage>` (e.g. `epic-5-gate-product`;
`pipeline.branches.gateSuffix`, default `-gate-`) cut from `origin/epic-<n>`.
`open-gate --unit epic` opens `epic-<n>-gate-<stage>` → `epic-<n>`; the human merges
it — squash is fine, the sub-branch may be deleted — and the doc lands on the epic
branch; `pass-gate --unit epic` reconciles the `epic-<n>` worktree with
`origin/epic-<n>` (a fast-forward) and continues as before (Gate A claims
`architecture`, Gate B completes the epic architecture). `epic-<n>` itself still
merges to `main` only at `close-epic`, unsquashed; `sync-branch --unit epic` still
reconciles it against `origin/main`, its integration base. Standing-epic child gates
(`issue-<n>` → `main`, never squashed, branch never deleted) are unchanged.

The "docs reachable by name mid-epic" guarantee that justified the old
straight-to-`main` exception moved one branch over instead of being dropped:
`check-epics-closeable` now verifies both docs exist on `origin/epic-<n>`
(`docs_missing_from_epic_branch`, was `docs_missing_from_main`), and `close-epic`'s
merge is what carries them to `main`. `_GATE_BRANCH_RE` / `_match_open_gate` (the
real-time backstop) recognise the new head shape, cross-check the base (`main` for
`issue-<n>`, `epic-<n>` for the gate sub-branch) and the head's stage against the
gate marker; a bare `epic-<n>` head — now only ever the close-epic integration PR —
is no longer a gate.

Same pass: `pipeline.retro.watermarkFile` defaults to `{docRoot}/retro-watermark`,
formatted with the config's `docRoot` at load (`docs/sdlc/retro-watermark` under the
sample), so the watermark is committed in the driven repo rather than inside the
skill checkout; the leaked client `retro-watermark` at the skill root was deleted.

## 2026-09-05 — Gate PRs (Product/Architecture) should route through the epic branch too, not straight to main

*Resolved 2026-09-06 — see the entry above: gate docs go on `epic-<n>-gate-<stage>`
→ `epic-<n>`, and `check-epics-closeable` reads the epic branch.*

Reviewing epic-181 (#380) and epic-348 (#382), the assistant recommended
squash-merging both Gate PRs — wrong on two counts against this file's own
documented rule: Gate PRs go `epic-<n>` → `main` and are **merged, never
squashed** (see "The epic integration branch" in `epics.md`), and
`cmd_pass_gate` expects a human to have performed that merge directly on
GitHub; the script never squashes a gate PR itself (only `GitHub.pr_merge()`,
used by `merge-pr` for the dev lane, hardcodes `--squash`).

Operator feedback, unresolved: Gate PRs (`product.md`/`architecture.md` for
review) shouldn't special-case straight-to-`main` at all — they should follow
the same shape children already do: branch from `epic-<n>` (or a further
sub-branch of it), squash-merge into `epic-<n>`, and only `epic-<n>` itself
ever merges — unsquashed — into `main`, at epic close.

This is a real design tension, not a typo fix. The current straight-to-main
exception exists specifically so `product.md`/`architecture.md` are
"reachable by name rather than by a SHA quoted from an old comment" and so
`check-epics-closeable`'s check #4 ("epic's own product.md and architecture.md
are both on main") can verify it mechanically mid-epic, before any child
exists to force the epic branch to main. Routing Gate PRs through the epic
branch instead means that guarantee has to move — either `check-epics-closeable`
starts reading `epic-<n>` instead of (or in addition to) `main` for a still-open
epic, or the epic's docs are only ever authoritative-and-reachable once the
epic itself closes. Not resolved here; needs a decision on which before
`SKILL.md`/`epics.md`/`gates.md`/`sdlc_next.py`'s `open-gate`/`cmd_pass_gate`
change to match.

## 2026-09-04 — publish lld.md to the epic branch on a clean lld-review; resume-base bug

A normal-epic child's `lld.md` reached the epic branch only when the child's whole
pipeline merged. New `merge-lld-doc <n>` publishes it the instant `lld-review` is CLEAN
— a doc-only commit of that one file, taken verbatim from `origin/issue-<n>`, onto
`epic-<parent>` (not a merge of the child branch). Two payoffs: the design is durable
on the epic branch independent of the still-open child branch, and siblings pick it up
in-tree on their next `sync-branch`, so cross-child overlap checks see the real
committed design. Scoped no-op for a standing-epic child / parentless issue; a refused
push (epic branch advanced) is a structured `conflict` result at exit 0, not a crash.
Wired into the `lld-review` clean-exit action, after `record-design-review` and before
claiming `development`.

The investigation that prompted it also corrected a misdiagnosis. `lld.md` was thought
to be lost on a crash; it is not — the `lld` stage pushes it to `origin/issue-<n>`, so
it survives. The real re-run bug was a **resume that recreated the worktree fresh off
`main`** instead of `origin/issue-<n>`, silently starting from a tree with none of the
pushed work and letting the resumed agent rewrite it. Hardened as a docs rule (base a
resume worktree on `origin/issue-<n>` when it exists) in `references/parallelism.md`
("Resume base") with a pointer from SKILL.md's Step 2 and the Ordering rule.

## 2026-08-28 — epic-level design pairing reverted to Opus, on cost

`architecture` and `arch-review` were pinned to Fable on 2026-08-24 (both levels, plus
the standing-child `architecture`). Reverted to Opus four days later, operator decision,
on cost rather than on a quality failure.

Fable is $10/$50 per MTok against Opus's $5/$25 — 2x in both directions — and its
thinking cannot be disabled, so the premium lands on a stage that emits long documents
*and* long reasoning. That is the worst possible shape for that rate.

The quality case never materialised in the output. Epic #95's architecture ran three
`arch-review` rounds on Fable, and every finding that mattered came from the reviewer
reading the codebase and falsifying a doc claim that read correctly: `PermissionGuard`
failing open on a missing `@RequirePermission` while CI's own script safe-lists the
decorator as "advisory metadata, not a guard"; `getAllAndOverride([handler, class])`
replacing rather than intersecting, so a decorator added to tighten actually widens;
React Query's `staleTime: 30_000` making an AC unmeetable. That is verification against
reality, and Opus had been doing it for eight epics before the pin.

The structural argument also points at Opus for the author half specifically: an author
error is caught by the reviewer standing in front of it, while a reviewer miss is caught
by nothing — `arch-review`'s confidence marker can skip Gate B outright. That asymmetry
is already how the tier map works one altitude down, where `lld-review` and `pr-review`
are the expensive halves. The epic level was the outlier.

Side effect worth knowing: after this change no agent definition runs at two tiers, so
the call-site pinning rule is currently paying for optionality nobody is using. Keep it
anyway — it is what made this revert a three-word edit.

## 2026-08-28 — retrospective on epic #98 (issues #272, #273, #274, #275, #284)

Five children shipped, nothing broken merged, and the reviews earned their keep: a
runbook command that set prod retention to stage's 14 days and upserted
`.secrets.stage` into prod's `APP_SECRETS`; `severity >= "ERROR"` as a lexicographic
string comparison, so `INFO` matched and `CRITICAL` did not; a `DRY_RUN` guard that
failed open on `true`/`yes`/`on`; a fatal-path scrub bypass that shipped a Postgres
bind parameter into the log store. Every one was caught before merge.

The friction was almost entirely in *what a stage is allowed to assert without having
run it*, and in what the pipeline can still see after a crash. Marker-backed counts
were nearly silent — `pr-review` rework was 1, 1, 0, 0, 0 and sync conflicts were zero
across all five — which is itself the first finding.

**The valve could not see the pairing that fires it most.** `pairing-counts` covered
`pr-review` and sync conflicts only, while `lld-review` <-> `lld` ran roughly nine
tenths of the epic's review rounds and tripped the context-reset replacement on three
of four children. That strike count lived in one orchestrator's head; a crashed session
or any continuous-mode agent would have resumed it at zero. Fixed by
`record-design-review` plus a per-role `design_review` block in `pairing-counts`. The
`arch-review-confidence` marker could not stand in: it is meaningless on a rework
verdict, which is exactly the verdict a valve counts.

**A stage reported a build complete that had never been wired.** #284's `development`
landed ten commits with the design's validators written, unit-tested, and called from
**none** of the four entrypoints — `grep -ln "gcp_require_valid_dry_run" scripts/*.sh`
returned nothing, and no entrypoint file was in the diff at all. Caught only because
the next agent checked ACs against the diff rather than the commit messages. Now
`development`'s fourth completion gate. It is the same class `lld-review` had spent a
round closing one level down: *a unit test of a function in isolation cannot prove the
call site exists.*

**Citations can be correct when written and wrong when merged.** #274 shipped six stale
line numbers, four broken by its own merge commit pulling #284's script rewrites onto
the branch after the docs were authored; one was operator-facing and reached `main`.
Nothing re-checks citations after a merge. Shipped documents now prefer the
grep-anchored quote alone — a quote survives a sibling's refactor, a number does not,
and correcting numbers only resets a clock the next `scripts/**` edit restarts.

**A reviewer claimed coverage it did not have.** On #274 a review posted CLEAN with
**zero of three axes returned**; the two that landed afterwards carried the worst defect
of that round. It disclosed this itself, which is the only reason it was caught. Design
reviews now state how many axes they launched and how many returned.

**Batching loses everything in a hostile session; committing per step loses nothing.**
~15 turn deaths. #274's replacement batched and lost three full rounds of work with the
document never once modified; switched to one-finding-one-commit it landed seven commits
across nine more interruptions intact. #284 carried ten the same way. Also recorded: the
orchestrator holds long waits itself rather than letting a subagent burn a turn per
wake, and long runs go in detached containers writing under the bind mount — two earlier
#275 runs produced no artifact because their reports died inside `--rm` containers.

Two items were filed rather than written into the skill, being repo config: CI trigger
filters not covering epic branches (hit twice — `backend-ci.yml` on #272, `frontend-ci.yml`
on #275, each found by a PR that structurally could not merge), and `scripts/tests/**`
running in no CI job at all. The pipeline's own pytest suite also has 12 pre-existing
failures on `main`, tracked in #182.

## 2026-08-28 — `lld` moves to opus

Operator decision, taken mid-epic-#98 on the evidence of its own children. `lld` had
been sonnet on the theory that it is high-frequency work with a mandatory review behind
it. The review did hold — but the bill came in rounds: **#272 six `lld-review` rounds
plus a context-reset replacement, #284 five rounds plus a reset, #274 three rounds plus
a reset**, against #273's three. Every round found something real (a runbook command
that set prod retention to stage's 14 days and upserted `.secrets.stage` into prod's
`APP_SECRETS`; `severity >= "ERROR"` as a lexicographic string comparison, so `INFO`
matched and `CRITICAL` did not; a `DRY_RUN` guard that failed open on `true`/`yes`/`on`),
so nothing shipped broken.

Two things made the tier the problem rather than the reviewer. First, **several rounds'
fixes introduced the next round's findings** — two of #274 round 3's five, and #284's
round-4 defect was a test that could not go *green*, written by the round meant to close
tests that could not go *red*. Second, the recurring class across all three children was
the same shape: *a specified guard restated until it can no longer discriminate*, which
is a design-judgement failure, not a throughput one. The valve's context-reset
replacement fired three times in one epic — the valve doing its job, and also a signal
that its input was wrong.

`development` stays on sonnet: its work is bounded by an approved design and backstopped
by both `testing` and `pr-review`.

## 2026-08-22 — retrospective on epic #156 (issues #231-#238, #243, #245)

Ten children through the full pipeline in ~24 hours, dev lane and review pool both
concurrent. Nothing shipped broken; the friction was almost entirely in *how a claim
gets established*. Full findings, evidence and rejected candidates:
`docs/sdlc/retro-2026-08-22.md`.

- **The dominant defect class: a number produced by a grep that models a rule, rather
  than by running the rule.** At least eighteen confidently-stated, wrong measurements
  across six children and the epic's own architecture. #236's `lld` counted violations
  with a `*.module` exemption the shipped eslint config did not have — its design would
  have banned 8 legitimate Nest DI imports and shipped a CI-red PR, and the obvious late
  fix would have invalidated the barrel cycle analysis the same doc rested on. #245
  shipped a rule whose meaning lived only in the design doc; applied as written it
  wrongly exempted three real files. #243 existed to fix a source comment that described
  behaviour the code did not have — and its *replacement* comment was wrong twice more
  across three rounds. Every blocking finding in the epic was found by building the
  change and running the real tool; none by reading. Fix: "Establish a number by running
  the thing, not by modelling it" in `stage-playbooks.md` — run the rule, search the
  whole tree, prove the detector detects.
- **`src/database/database.module.ts` was missed twice**, by #235 and #236, for the
  identical reason: a grep scoped to `src/modules/` in a repo whose composition file
  lives in `src/database/`. Same section, sub-rule 2 — anchor to the symbol, not to a
  path prefix you assume.
- **Vacuous passes.** Four mechanisms, all hit live: a stale `tsbuildinfo` made `tsc`
  emit 92 files instead of 533 and exit 0 (two different agents); an SCC script with too
  narrow an edge pattern gave the barrels zero outgoing edges and a vacuous "0 cycles";
  `frontend/Makefile` never forwarded `E2E_SELLER_*`, so ~35 seller/data-entry
  tests skipped on every run this repo has ever done and both sides of a comparison
  "matched" over them; and a mutation landed on a permission both roles already held.
  The counter-practice that worked, repeatedly and deliberately, is the **positive
  control** — four separate agents proved the cycle detector detects before trusting its
  absence result. Encoded as tells plus the positive-control rule, deliberately *not* as
  a catalogue of environment traps.
- **`make e2e` was never broken.** Three stages (#232, #233, #245) hit failures,
  concluded the suite was broken, and routed around it; one agent was killed outright.
  #238's `lld` root-caused it: the committed `--network host` + `localhost` config works;
  the obvious "fix" (`host.docker.internal` + bridge) is what fails, because Docker
  Desktop's gVisor proxy cannot complete the Next dev server's `/_next/webpack-hmr`
  WebSocket upgrade, so the client never hydrates and the sign-in modal never opens.
  Plain HTTP GET returns 200 under both, so reachability is not the discriminator. Fix:
  `parallelism.md`, "The e2e suite is not known-broken" — plus the watchdog rule, since an agent's own
  ~120s tool-call timeout kills the run long before the harness's own 480s cap applies.
  That was the stall-watchdog trip that killed the agent.
- **Residue placement — reinvented independently at least three times.** Findings belong
  on the issue that will act on them. The `check-controller-protection.mjs` coverage gap
  lived only inside a review comment on an **already-closed** issue, where #236-#238
  would never have seen it; it survived because one reviewer noticed and carried it to
  all three by hand. #235's eslint-exemption finding posted onto #236 became half of
  #236's design. Cheapest high-value edit in the retro.
- **The escalation valve counts bounces; what repeats is a class.** #234 bounced 3× at
  `pr-review` and #243 and #245 3× each at `lld-review`, every time on a *different
  instance of the same class* — and every unit settled within one round of somebody
  saying "fix the class, not the case". #243's round-2 reviewer wrote the missing rule
  itself ("that is an escalation candidate on the pattern … rather than a routine
  bounce"). The counter is right; the rework instruction was not. Fix: reviewers name
  the class in the verdict, the resume message asks for the class, and a same-class third
  bounce escalates on the pattern.
- **A stage produced a FAIL verdict, live probe evidence, and no comment.** On #232 a
  29-minute `testing` round sits between two identical start comments with nothing
  between them; `pr-review` later blocked because the evidence existed nowhere. The
  playbook's "one comment per stage" floor already existed — the cause was structural:
  `testing`'s PASS path posts a script-owned comment, its FAIL path posts nothing. Fixed
  the asymmetry rather than restating the floor.
- **Worktrees were created from local `main`.** `parallelism.md` and `SKILL.md` printed
  `worktree add … -b issue-<n> main` literally; local `main` was four commits behind, and
  two children started on a base without their own epic's merged `architecture.md`.
  `sync-branch` reconciled it, but only after the fact. The shared checkout is never
  checked out to a pipeline branch and nothing pulls it, so its local `main` is stale by
  construction. One-word fix in four places, plus the reason stated.
- **Confirming rounds are scoped.** One measured confirming pass took 277s against the
  original's 913s with no loss of rigour — every scoped round on this epic still found a
  blocking issue, and each proved the delta could not disturb what it accepted
  (`git diff --stat origin/main` → doc-only). Written down so it is the default, with
  the counter-rule: a code delta re-derives.
- **Two verification mechanics that bit the orchestrator.** A two-dot `git diff` across
  an `origin/main` merge attributed 4 files to an agent that changed 1, and a wrong
  rework bounce was nearly sent on it — use `git show <sha> --stat` or three-dot. And a
  large delegation prompt **truncated mid-prompt** on the very first dispatch; handing
  the agent a `gh` fetch command instead of pasted content fixed it permanently.
- **Three agents died to infrastructure** (API error, machine sleep, stall watchdog) and
  the same recovery worked each time: inspect the worktree, resume the *same* agent with
  "treat nothing as verified", never force-push. One agent hit a rebase-rejected push and
  correctly stopped and asked. Recorded in `parallelism.md`.
- **Three operator policies set live**, now written down: scoped e2e per child with the
  full suite once at epic close and Blocker/Critical → epic, Normal/Low → the standing backlog epic
  (`epics.md`); parallelism is the orchestrator's call and a `blockedBy` edge usually
  constrains `development` onward, not `lld` (`SKILL.md`); and the escalation line — take
  the recommended fix yourself during `development`/`testing`/`pr-review`, escalate only
  product/scope calls, gate-approved-doc amendments, and valve trips (`SKILL.md`).
- **Hygiene.** `start-comment --role` rejected `lld-review`, which is mandatory on every
  normal-epic child and ran ~15 times this epic without a single start comment; the
  playbook's workaround (reuse `--role arch-review`) would have posted a comment naming
  the wrong stage. Role added. And `EFFORT_FIELD_ID`/`EFFORT_OPTION_IDS` are defined in
  `sdlc_next.py` and referenced nowhere — there is no `set_effort`, an issue filed by a
  review stage carries no Effort, and nothing in the lane reads it. Stated in `epics.md`
  rather than adding a command for a field nothing reads.
- **Not fixed, deliberately:** no catalogue of environment traps (that is how the skill
  gets long enough that nobody reads it — see the port incident below); no `set_effort`;
  no tightening of the test-only merge-and-file exception, which #234 used on a CI-guard
  script and which produced #243 and a good outcome; no cap change, since the lane was
  under-used rather than over-subscribed.

## 2026-08-21 — retrospective on epic #110 (issues #186, #189, #209)

Epic #110 closed out its last two children in one invocation. Six findings, all from
things that actually went wrong, plus one operator decision:

- **The skill moved mid-invocation and nobody noticed.** The run loaded `SKILL.md` at
  start; `f32c905` — which replaced the whole BMAD persona layer with dedicated
  `sdlc-*` subagent types — merged into `main` partway through. Every remaining stage
  was dispatched the superseded way (`general-purpose` + a `bmad-*` skill). Nothing
  failed loudly. Fix: `merge-pr` now returns `config_changed: true` when the merged PR
  touched `the skill directory `, and `SKILL.md` gained "The skill can move under
  you". Mechanical, because remembering is what failed.
- **Wrong citations came from every stage, not just `testing`.** `lld` cited epic-doc
  lines an addendum had shifted; `development` cited `:438` for `:448`; `pr-review`
  raised a blocking finding against an **unreachable draft SHA**, which `development`
  disproved twice before it was withdrawn. Open issue #194 covered only `testing` and
  is now parked under a legacy epic. Fix: a citation-discipline section in
  `stage-playbooks.md` binding all stages — anchor to headings and quoted titles, never
  bare line numbers; verify a branch ref is reachable before quoting it.
- **`--list` is not a test run.** #209 shipped two E2E tests whose locator matched
  `OrderStatusTracker`'s step labels and yielded `NaN`. `playwright --list` was clean,
  and the tests *skip* under `make e2e`, so they would have merged broken and stayed
  broken. Only live execution caught it. Fix: "Compile-checking is not verification" —
  no stage may claim a test covers an AC unless it watched it execute.
- **Docker, not the parallelism caps, is the real limit.** The 65-suite backend run
  OOM-killed single-process; two agents independently rediscovered the same
  memory-scoped batching. Fix: `parallelism.md` now states the batching pattern, says
  not to start a second suite-heavy stage while one is live, and forbids reporting a
  resource kill as a code finding.
- **Epic docs must reach `main`.** `epic-110/architecture.md` lived only on its branch
  — and that branch was an **orphan** with no merge base, so it could never be merged
  and its tree was a stale full-repo snapshot. That is the direct cause of the
  unreachable-SHA finding above. Fix: land the doc at `epic:architected`, never create
  the epic branch as an orphan, and `check-epics-closeable` now reports
  `docs_missing_from_main`.
- **Duplicate PR opened.** #226 duplicated #220 (byte-identical) because the open-PR
  list was never checked — despite the other session's worktree being visible in the
  first `git worktree list`. Fix: `open-dev-pr` returns the existing PR with
  `created: false`; `operations.md` carries the manual check for ad-hoc PRs.
- **`testing` pinned to Sonnet permanently** (operator decision). Haiku assumed the
  stage only runs commands. On #209 it had to stand up an isolated stack, seed a promo
  order directly into Postgres around a credential gap, and reproduce a locator
  over-match live to tell a broken test from a passing one.

Worth recording as things that worked: `pr-review` on #186 used **mutation testing** to
prove the new tests had teeth (flipping the sweep gate failed exactly one test;
dropping `sellerId` from the predicate failed exactly one), and on #209 it simulated a
revert through the real formatters and the real backend allocator to prove the tests
were genuine regression guards. Both went beyond reading the diff, which is the bar.

## 2026-08-20 — consistency pass: determinism fixes + doc split

Per operator instruction ("check thoroughly for consistency, especially parallelism
and git conflicts; make things more deterministic"):

- **Children gated on `epic:architected` mechanically** — `decide_next_action` and
  `list-parallel-ready` both refuse a normal epic's children until the epic is
  architected. Previously, an epic stuck at an open-but-unsatisfied gate,
  needs-human, or blocked fell through to the children loop and could delegate a
  child at `lld` against a nonexistent `architecture.md`.
- **Epic gates joined the real-time backstop** — the gate-PR matcher now recognizes
  `epic-<n>` head branches and derives the unit from the branch, so a merged epic
  Gate A/B auto-advances (Gate B via `_complete_epic_architecture`) and epic gate
  feedback flips `Feedback Received`. Previously only `issue-<n>` matched, and the
  unit was hardcoded `"issue"`.
- **`merge-pr` refuses a branch behind `origin/main`** (structured exit-0 result) —
  green CI on a stale base proves nothing about combined state once siblings merge
  in parallel; sync + fresh CI + re-merge is the mechanical response.
- **Parallel-lane bootstrap** — a never-started child (no `origin/issue-<n>` branch)
  is eligible without a footprint, its own docs folder standing in; requiring a
  committed footprint deadlocked the lane (the footprint is written *by* `lld`).
  `list-parallel-ready` also fetches origin first and accepts Stage-unset children
  via `default_stage()`.
- **SKILL.md split** into a spine plus `references/` files; stale prose contradicting
  the parallel lane ("one issue at a time", "single ongoing development lane")
  removed; footprint format contract documented explicitly.

Follow-up pass, same day (the "smaller list" from the same review):

- **`--repo-path` auto-resolve** — `sync-branch`/`verify-exit`/`open-gate`/
  `pass-gate` resolve the branch's live worktree from `git worktree list` when the
  flag is omitted; the old default of "." silently targeted the shared checkout
  whenever the caller forgot the flag (a footgun the pipeline had already hit).
- **`claim` validates `--role`** — a typo'd role used to silently skip the Stage
  write while still claiming In Progress; now refused loudly.
- **Sync conflicts persist as markers; `pairing-counts` reads strikes back** —
  `sync-branch` posts a `<!-- sync-conflict: ... -->` comment on every real
  conflict, and the new `pairing-counts <issue>` command derives the three-strike
  counts for the two marker-backed pairings (pr-review rework since last clean,
  sync-conflict total) so the valve survives crashed sessions and fresh
  continuous-mode cycle agents.
- **Epic-self went worktree-always** — `epic-<n>` gets `/tmp/sdlc-epic-<n>` like
  every child branch; the shared checkout stays on `main` and the last
  cross-invocation collision path is gone.
- **Retro trigger became a committed watermark** — `retro-check` compares the exact
  closed-issue count (search API `total_count`, replacing a `--limit 200` list whose
  cap made the old `% 5` trigger fire forever) against
  `retro-watermark`; `--mark-done` records the new
  watermark, committed with the retro's own edits. The modulo trigger also skipped
  whenever two issues closed between checks.

## 2026-08-20 — epic number mandatory; LLD stage value; parallel dev lane; board writes

- **Epic number mandatory**: `next-action <epic>` requires the epic; the cross-epic
  ranking picker, milestone scoping, and `next-epics-batch` were all removed —
  naming the epic made all three moot. Cross-epic concurrency = separate
  invocations.
- **`LLD` became its own Stage option** (previously overloaded `Architecture`,
  disambiguated only by prose).
- **Parallel implementation lane**: `lld`/`development`/`testing` fan out to
  `DEV_LANE_PARALLELISM` worktree-isolated children, eligibility computed by
  `list-parallel-ready` (blockedBy + footprint overlap + live worktrees). First
  version (same day) was architect-declared and hand-tracked, with the shared
  checkout as an uncounted extra slot; revised same day per operator instruction
  ("think it through, including git conflicts") to mechanical eligibility and
  worktree-always for every child. Trigger: epic #110's `architecture.md` already
  stated an explicit execution order — "one issue at a time" was a blanket default,
  not a safety claim. `DEV_LANE_PARALLELISM` raised 2→3 after #185/#187 ran clean.
- **`sync-branch` conflict result**: a real merge conflict returns
  `{"conflict": true}` at exit 0 instead of crashing — routed as rework to that
  child's development agent, three-strike valve per pairing.
- **Pipeline Status `Done` + `mark-issue-closed`**: field cleanup on close moved out
  of `cmd_merge_pr` into the Action's `issues: closed` job so manual closes get the
  same treatment ("our GitHub Actions workflow should handle it, not the skill").
- **Epic board Status writes** (best-effort, epic-level only) once Projects v2
  access was confirmed — correcting a stale "Projects unusable" note that had
  outlived the fine-grained PAT it described.

## 2026-08-18 — e2e child mandatory; manual-testing bugs filed as children

Every normal epic gets a dedicated e2e-coverage child task. Epic-closing checklist
gained the manual end-to-end item; anything it finds is filed as a `Bug` child, never
folded into the closing comment.

## 2026-08-17 — parallel PR review; size-by-component

- `pr-review` decoupled into a bounded pool (`PR_REVIEW_PARALLELISM`, detached
  worktrees, queue markers). The queue markers are script-posted because hand-written
  ones did not hold up: #115 wrote `development->pr-review`, #111 a mangled
  `pr-review->development->pr-review`, #130 none at all.
- Child sizing flipped from "split anything big" to "one component each,
  non-overlapping footprints" — `Effort: High` alone stopped being a reason to
  split. This is what later made the parallel dev lane safe.

## 2026-08-16 — epic-level stages; native fields; sync rule; review model

- **Epic-level Product/Architecture**: epic #92 produced 5 concurrent child Gate A
  PRs, two of which (#99/#107) independently scoped the same backend change.
  Product/Architecture moved to once-per-epic with full child visibility.
- **`stage:*`/`status:*` labels retired** for native Stage/Pipeline Status issue
  fields (operator instruction).
- **Sync before every stage transition**: issue #116 confidence-skipped Gate B and
  entered development without ever merging `main` back in — nothing in the
  non-gate handoffs reconciled the branch. Hence `sync-branch` at every transition.
- **Test-only merge-and-file exception**: #115/PR #149 took three review rounds for
  what ended as test-coverage gaps; the narrow exception (file follow-up, merge)
  exists so "tests aren't good enough yet" doesn't block correct code — never for
  correctness/security findings.
- **Codex review removed**: review stages became first-class subagents (Fable, then
  Opus once Fable's per-call token cost became binding across every stage/review).
  Sonnet for `lld`/`development` (backstopped by Opus reviews), Haiku for `testing`.
- **Empirical reviews justified**: re-running the real suite caught the
  IPv6-mapped-address bypass and the seller-status authorization gap
  (#111/#114/#130). Docker suite OOM/flake (#109/#111) is what the parallelism caps
  are tuned against.

## Standing incidents the rules encode

- **Never hand-type fetch/checkout/merge/push**: a hand-typed sequence once kept
  going past a silently failed `git checkout` (branch held by another worktree) and
  merged/pushed onto whatever was checked out, corrupting it. `sync-branch` aborts
  loudly on that collision.
- **Always diff reviews against `origin/main`, never local `main`**: local `main`
  goes stale (no stage ever checks it out), and a stale diff pulls already-merged
  content into the review. Happened once; now a standing prompt instruction.
- **Fine-grained PATs don't work here**: org Free plan blocks them on check-runs;
  the token must be a classic PAT.
- **A parked unit's worktree silently starved the dev lane** (2026-08-20): #186 was
  marked `needs-human` but `/tmp/sdlc-dev-186` was left on disk. `list-parallel-ready`
  counts slots off live worktrees, so it read the lane as full at 3/3 and proposed
  nothing for the rest of the invocation — no error, no warning, just two-thirds of
  the capacity quietly gone. Fixed in two independent layers rather than one:
  `mark-needs-human`/`mark-blocked`/`merge-pr` now release the worktree themselves
  (refusing on dirty or unpushed state), and `list-parallel-ready` no longer counts a
  worktree whose issue is parked or closed, reporting it under `stale_worktrees`. The
  general lesson, already encoded elsewhere in this skill: anything the orchestrator
  must *remember* to do between steps eventually doesn't happen, and the failures that
  matter most are the ones that degrade throughput without raising an error.
- **`product.md` became an IRD, not a pipeline artefact** (2026-08-22): the epic #98
  product document was rejected by the operator on ten counts, and every one of them
  was a rule this skill had written down. It opened with a TL;DR box and a
  `**Stage**: product (John / PM)` line; it explained that the epic had no children yet
  and whose job creating them was; it carried a "what this is a hypothesis about"
  preamble, an "architecture-depth assessment" section, a "what changed versus the
  superseded document" section, and references to the review gate by name. Worse, three
  of its top-line *requirements* were technology choices — the log platform, the metrics
  endpoint's fate, the alert channel's transport — which is the architecture stage's
  entire job, handed to it pre-decided in the document it is required to build from.
  None of that was the agent improvising: the template asked for the TL;DR and the
  decisions table, and the altitude rules asked for the diagram and the collapsed
  detail. The fix was to stop treating `product.md` as a gate deliverable shaped by this
  pipeline and start treating it as the repo's own existing artefact — an IRD in the
  house style of your repo's requirements docs, with IRD-002 as the worked
  example. Template, agent file, and the "Document altitude" section were rewritten
  together: requirements state observable behaviour and never name a vendor, nothing
  about the pipeline appears in the prose, no hedging layer, detail stays inline, and
  system flow diagrams move to `architecture.md` (mock-ups take their place where a
  screen layout matters). `architecture.md`'s rules are unchanged — it is a design
  document and its TL;DR, options tables and collapsed detail all still earn their
  place. The general lesson: when a document has an established form in the repo,
  inventing a second form for the pipeline's copy of it produces something that reads
  like neither.


## 2026-09-01 — `lld` back to Sonnet, and `opus` now means Opus 4.8

Two operator decisions taken mid-epic-#95, both on cost.

**`lld` returns to Sonnet.** It had been Opus since 2026-08-28, pinned on the evidence
of epic #98's children (#272 six `lld-review` rounds, #284 five, #274 three, each with
a context-reset replacement). Epic #95 ran three children's `lld` on Opus — #119, #120
and #121 — and all three came back rework on the first review, on defects of the same
shape the Opus pin was meant to remove: an acceptance criterion labelled `Full` whose
proof is not in the child's own footprint (all three), a premise read off a migration's
transient widening step instead of its final `CHECK` (#119), a design ruling justified
by an epic acceptance that does not exist (#121), and a rounding rationale that its own
worked example refutes (#121). The expensive tier did not change the failure class, so
the class is not a throughput problem — it is what `lld-review` exists to catch, and
`lld-review` caught all of it. `lld-review` stays Opus for that reason.

**The `opus` alias is pinned to Opus 4.8** via `ANTHROPIC_DEFAULT_OPUS_MODEL` in
`.claude/settings.json`. Recorded here because the mechanism is not obvious: the
`Agent` tool's `model` parameter takes only tier aliases, so there is no way to select
a specific model version at the call site, and agent-file frontmatter — which does
accept a full model ID — is overridden by the call-site parameter this skill relies on.
Redirecting what the alias resolves to is the only lever that leaves the call-site
design intact. It applies at session start, so a running session is unaffected.

## 2026-09-03 — retrospective, epic #229 (watermark 92 -> 101)

Run mid-epic, lane deliberately quiet. Five findings, all from a single `/sdlc-pipeline 229`
invocation that spent ~2.75M subagent tokens across twelve stage agents.

- **The `SKILL.md` handed to an invocation can already be stale.** `merge-pr`'s
  `config_changed` covers the mid-run case; nothing covered the load case. A run was
  handed a copy pinning `lld` to opus; `main` had said sonnet since `aab66f8`
  (2026-09-01). Six dispatches went out a tier too high, ~1.55M tokens — over half the
  invocation's spend — before the operator asked why usage was high. Fix: check the
  model table against `origin/main` before the first dispatch.
- **The opus-alias pin was asserted as fact and was never in place.** `SKILL.md` claimed
  `.claude/settings.json` set `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8`. Neither
  settings file contains it, or any `env` block. The paragraph now says so.
- **`mark-blocked` left `Pipeline Status` at `in-progress`**, unlike `mark-needs-human`.
  A blocked unit with no worktree is indistinguishable from a crashed run to the
  heuristic `decide_next_action` and `list_parallel_ready` share, so #310 sat blocked all
  session while `next-action` kept offering it as a `resume`. Now resets to `todo`, with
  a regression test asserting the write and asserting the two wrong values are absent.
- **The design-review fan-out never inherited the scoped-rework rule.** Its own section
  is titled "in one round" and argues first-pass discovery, but nothing stopped it
  re-running on rework rounds; every #312 rework paid a full five-axis sweep to re-check
  a doc-only delta. The axes were also dispatched with no `model`, silently inheriting
  the parent's opus. Both fixed: sonnet axes except the completeness lens, and no
  fan-out on rework rounds. Same class as the older split-rule bugs — a rule living in
  one home and not the other.
- **`TaskStop` on a fan-out parent does not stop its children.** An axis reported ~166k
  tokens of verified work after its parent was killed. Recorded in
  `references/parallelism.md` with the salvage rule and the after-kill artifact sweep,
  since an earlier interrupted review left probe objects in live state buckets.

Also filed rather than fixed: 12 tests red on `main` — production moved to
`integration_base()` (a child merges `origin/epic-<parent>`, not `origin/main`) and the
`merge_pr`/`sync_branch`/`open_dev_pr` tests still encode the old behaviour. That is the
merge gate's own coverage, so it went to #182 with the diagnosis rather than into a
retro commit.

Not changed, deliberately: the model table's tiers beyond the `lld` correction, and the
subagents-propose-parent-disposes guarantee in `sdlc-design-review` — only the cost of
generating leads moved, never the verification.

## 2026-09-06 — origin-repo incidents moved out of `stage-playbooks.md` and `parallelism.md`

The two reference files were made generic on this date: every rule stayed, but the
incident narrative that backed it — issue numbers, the origin repo's commands, files,
symbols and machine limits — was moved here. Each heading below names the rule (and the
file/section that states it) the incident backs. Issue numbers refer to the origin repo.

### `stage-playbooks.md`, "Document altitude" — `Footprint`/`Implementation notes` are omitted from an epic-level doc

Putting per-child implementation depth in the epic doc duplicates it at the wrong
altitude, which is how epic #98's `architecture.md` reached 20k words.

### `stage-playbooks.md`, "Citation discipline" — every stage has shipped a wrong citation

In one cycle (epic #110, 2026-08-20): `lld` cited two epic-doc line numbers that an
addendum had shifted 19 lines; `development` cited `account.spec.ts:438` for a test at
`:448`; and `pr-review` raised a blocking finding against
`origin/epic-110:architecture.md:87-99` after resolving the branch from a SHA in an old
comment link — an **unreachable draft commit**, superseded an hour after it was written.
`development` had to disprove that one twice before it was withdrawn.

### `stage-playbooks.md`, "Citation discipline" — never a two-dot diff range

`git diff 3fee947 b3a518e --stat` reported 4 files where #245's agent reported 1, and a
"you touched files you shouldn't have" correction was nearly sent on that basis. The
agent was right; the check was wrong.

### `stage-playbooks.md`, "Citation discipline" — a stale citation in a doc about to merge is a real finding

That was the blocking finding on #209's `development.md`, which still documented a
locator the `testing` stage had empirically disproved.

### `stage-playbooks.md`, "Citation discipline" — a citation can be correct when written and wrong when merged

#274 shipped six stale line numbers, and **four of them were broken by that PR's own
merge commit**, which pulled sibling #284's script rewrites onto the branch after the
docs were authored. One was operator-facing and reached `main`. Every stage had verified
them honestly; every one drifted afterwards. The "prefer the grep-anchored quote alone
in a shipping document" rule was written with the origin repo's `scripts/**` tree in
mind: correcting numbers there just reset a clock that the next `scripts/**` edit
restarted.

### `stage-playbooks.md`, "Compile-checking is not verification"

#209's two new E2E tests shipped with a locator (`li p.font-semibold`) that also matched
`OrderStatusTracker`'s always-rendered step labels and yielded `NaN`. `development`'s
`playwright test --list` check was clean. The tests skip under `make e2e` (a
pre-existing credential gap), so they would have merged broken and silently stayed
broken. `testing` caught it only by executing them for real.

### `stage-playbooks.md`, "Establish a number by running the thing, not by modelling it"

The dominant defect class of epic #156 — at least eighteen confidently-stated, wrong
measurements across six children and the epic's own architecture — had one shape every
time: a number produced by a grep that models a rule, rather than by running the rule.
Every blocking finding in that epic was found by building the change and running the
real tool; none was found by reading.

- **Run the rule, don't regex-model it.** #236's `lld` counted violations with a
  counting grep carrying a `*.module` exemption that the shipped `eslint.config.mjs` did
  not have — the exemption existed only in the grep and in the AC prose. Applied as
  designed it would have banned **8 legitimate Nest DI imports** and shipped a CI-red
  PR, and the obvious late fix would have invalidated the barrel cycle analysis the same
  doc rested on. #245's `lld` shipped a criterion whose meaning lived only in the design
  doc, which — applied as written — wrongly exempted three real files.
- **Scope the search to the whole tree.** `src/database/database.module.ts` was missed
  **twice**, by #235 and #236, for the identical reason: a grep scoped to `src/modules/`
  in a repo whose composition file lives in `src/database/`. #236's version would have
  broken `npx eslint` on `main`'s own composition file. Anchoring to the symbol
  (`from '[^']*sellers/`) found what the relative-path pattern (`from '(\.\./)+sellers/`)
  could not.
- **Prove the detector detects.** The positive control used repeatedly and well on
  #236: flip `ProductVariant` to route through the barrel, watch a new 16-file SCC
  appear, revert, watch it go. Four separate agents ran that control on the same claim,
  which is why it is trustworthy.

Vacuous-pass tells, all four hit live on #156:

- A stale `tsbuildinfo` made `tsc` emit 92 files instead of 533 and exit 0. Two
  different agents hit it.
- A graph built with too narrow an edge pattern gave the barrels zero outgoing edges and
  a vacuous "0 cycles".
- `frontend/Makefile` never forwarded `E2E_SELLER_*`, so ~35 seller/data-entry tests
  skipped on every run the repo had ever done, and a before/after comparison "matched"
  over them.
- #236's `testing` mutated a permission both roles already held, stayed green, and
  correctly called that a bad discriminator, not a finding about the test.

Two agents on #236 caught false positives in scripts they had just written, before
treating the output as evidence, and said so in the handoff.

### `stage-playbooks.md`, "Commenting discipline" — pipeline tooling is tracked in the repo

In the origin repo the tracked tooling was `.claude/skills/` and `_bmad/`.

### `stage-playbooks.md`, "Where a finding this unit will not fix goes"

1. Sibling that owns the surface: #235's `pr-review` found that `offer.entity.ts`'s
   eslint override silently voids any pattern added to the base block; posting it on
   **#236** made it half of #236's design instead of a note in a merged PR.
2. Standing backlog epic: #250 came out of #238's `lld` correctly refusing to file three
   backend IT/unit findings into an e2e issue.
3. Never only in a closing PR's thread: the `scripts/check-controller-protection.mjs`
   coverage gap lived only inside #243's review comment on an already-closed issue,
   where #236–#238 would never have seen it, and survived only because one reviewer
   noticed and carried it to all three by hand.

### `stage-playbooks.md`, "When the original agent cannot be resumed, the replacement starts from the existing work"

On #238 and #251 the replacement agents had to be told by hand what was already
committed; nothing in the process required it.

### `stage-playbooks.md`, "Rework rounds are scoped, not repeated from zero"

One measured scoped pass took 277s against the original's 913s, and still found a
blocking issue. Every good scoped round on #156 opened with
`git diff --stat origin/main` showing the change was doc-only.

### `stage-playbooks.md`, "The counter counts bounces; the thing that repeats is a class"

#234's round 2 said so in the verdict (*"REWORK for one new blocking finding **of the
same silent-skip class**"*); #243's round 2 went further and named the escalation shape
in advance (*"If a third round produces another, that is an escalation candidate on the
pattern … rather than a routine bounce"*). Both units settled within one round of the
orchestrator asking for the class: #234's `development` restructured so the route set is
derived live and an unknown decorator fails instead of passing (*"fixes the bounced class
at the root, not at the symptom"*); #243's `lld` deleted a citation that was correct
*today* because the shape rots, and swept a fourth instance before it could exist.

### `stage-playbooks.md`, `arch-review` exit action — overlapping child subsection scope is a structural finding

The exact pattern was #99/#107 on the origin repo.

### `stage-playbooks.md`, `lld-review` exit action — `record-design-review` exists because the valve could not see its busiest pairing

On epic #98 the `lld-review` <-> `lld` pairing ran roughly nine tenths of the epic's
review rounds and tripped the context-reset replacement on three of four children, while
`pairing-counts` tracked only `pr-review` and sync conflicts — so the strike count lived
in one orchestrator's head and any crashed session, or any continuous-mode agent, would
have resumed it at zero.

### `stage-playbooks.md`, `lld-review` exit action — state your axis coverage in the handoff

On #274 a review posted CLEAN with **zero of three axes returned**; the two that landed
afterwards carried the worst defect of that round, and the issue came within one
orchestrator decision of moving to `development` on it. The reviewer disclosed it itself,
which is the only reason it was caught.

### `stage-playbooks.md`, `development` completion gate 4 — check every AC against the real diff

Added 2026-08-28. On #284 this stage reported the build complete across ten commits, and
it was not: the design's validators had been written and unit-tested, and **wired into
none of the four entrypoints**. `grep -ln "gcp_require_valid_dry_run" scripts/*.sh`
returned nothing, and no entrypoint file appeared in the diff at all. The commit messages
read as a finished build; the diff did not. It was caught only because the next agent
checked the ACs against the diff instead of the summary — one stage later, and after
`testing` would already have been dispatched at a design that could not have passed it.

### `stage-playbooks.md`, `testing` exit action — the origin repo's verification commands

The independent-verification bullet used to spell out the origin repo's commands, which
are now expected to live in the driven repo's `CLAUDE.md` and the `sdlc-testing` agent
definition: backend commands ran in Docker only — `make lint`, `make build`,
`npm run test:it` inside the container; never `npm` or `nest` on the host. Frontend:
`make lint`, `make typecheck`, `make build`; `make e2e` from the workspace root when the
change touched a user-facing flow. The handoff-comment template's "Where" column read
"backend container / frontend / root". The `record-local-ci --suite` keys were `backend`
and `frontend`, and both suites had gone main-only for cost.

### `stage-playbooks.md`, `testing` exit action — the FAIL path must post the structured handoff comment

On #232 a 29-minute `testing` round produced a FAIL verdict and live probe evidence
between two identical start comments and left **no trace on the issue**; `pr-review`
then blocked because the evidence existed nowhere.

### `parallelism.md`, "The machine's resource cap is the real cap"

On the origin machine the binding constraint was the Docker VM (~7.6 GB, with the dev
frontend container alone holding ~2.6 GB). Observed twice on 2026-08-20, epic #110: the
full backend IT suite (`npm run test:it`, 65 suites) was **OOM-killed** when run
single-process — once during `development` on #186, which then reported its blast radius
by grep rather than by execution. Two agents independently rediscovered the same
workaround: memory-scoped batches with `--memory=3g`, `--runInBand`, against an isolated
Postgres on its own Docker network.

### `parallelism.md`, "The e2e suite is not known-broken"

Three separate stages on epic #156 (#232, #233, #245) hit `make e2e` failures, concluded
the suite was broken, and routed around it; one agent was killed outright by a stall
watchdog doing so. #238's `lld` root-caused it and reproduced the result in both
directions on the same machine:

- **`--network host` + `localhost:3001` — what is committed — works.** Auth setup
  completes, 30+ specs execute.
- **Bridge networking + `host.docker.internal:3001` fails**, and it is the obvious "fix"
  that fails: Docker Desktop's gVisor network proxy cannot complete the Next dev
  server's WebSocket upgrade (`ws://host.docker.internal:3001/_next/webpack-hmr`), so the
  client never hydrates, so the sign-in modal never opens. Plain HTTP GET returns 200
  under both — HTTP reachability is not the discriminator, the WebSocket upgrade is.

The watchdog rule: an agent's default ~120s tool-call timeout killed `make e2e` long
before the harness's own `timeout --foreground 480` applied.

### `parallelism.md`, "When an agent dies mid-stage"

Three agents died on epic #156 — an API error, the machine sleeping, and a stall
watchdog at 600s. In all three the work survived because it had been committed. One
resumed agent hit a push rejected by an intervening rebase and correctly stopped and
asked instead of forcing; `--force-with-lease` was authorised only after the branch was
verified a strict content-superset of what it would overwrite.

### `parallelism.md`, "Commit and push after every step"

Epic #98's session lost roughly fifteen agent turns to a machine that kept sleeping, and
the two working styles separated cleanly: #274's replacement batched — investigate
everything, then apply one large edit at the end — and lost **everything, three times
running**: three full rounds of verification, with the document never once modified.
Only its scratch files survived. Switched to one finding, one commit, one push, it then
landed **seven commits across nine more interruptions and lost nothing.** #284 did the
same and carried ten.

### `parallelism.md`, "Stopping a fan-out parent does not stop its children"

Observed 2026-09-03: a `lld-review` on #313 was stopped mid-verification to free the
lane. One axis returned afterwards carrying ~166k tokens of verified work — a
byte-identical re-derivation of a shell-generated filter, a `setIamPolicy` sweep across
thirteen roles with `roles/owner` as a positive control, and every runbook quote checked
against grep-anchored locators. None of it was in the parent's verdict, because there
was no verdict. An earlier interrupted review had left two empty state objects in live
GCS buckets from a probe that had contaminated its own control.

### `parallelism.md`, "Hold the waiting yourself"

On #275 the poll-and-re-park pattern consumed ~190k tokens across several cycles while a
53-minute e2e run ground on. #275's suite only produced a usable artifact once it was run
as a detached, daemon-managed container writing its report under the bind mount rather
than to a path inside a `--rm` container, where two earlier runs' output vanished with
the container.

### `parallelism.md`, "Reviews stay empirical, not diff-only"

Re-running the suite and re-verifying `development.md`'s claims is what caught the
IPv6-mapped-address bypass and the seller-status authorization gap (#111/#114/#130).

### `parallelism.md`, "Land the epic's docs on `main`; never create the epic branch as an orphan"

An `architecture.md` that lived only on `epic-<n>` is how #209's `pr-review` came to
quote a superseded draft (2026-08-20). `epic-110` was worse: it had been created as an
orphan with no merge base, so it could never be merged at all and its whole tree was a
stale snapshot of the repo.

### `parallelism.md`, "Branch from `origin/main`, never local `main`"

On epic #156 the early worktrees were created from local `main`, four commits behind —
two children started on a base that did not contain their own epic's merged
`architecture.md`. `sync-branch` reconciled it, but only after the branch existed.

### `parallelism.md`, "Slot accounting self-heals"

On 2026-08-20 #186 was parked `needs-human` with its worktree left on disk, the lane read
3/3 full, and the rest of that invocation ran at one-third capacity with no error
anywhere to notice.
