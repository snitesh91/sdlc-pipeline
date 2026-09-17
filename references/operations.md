# Operations — repo access, issue taxonomy, merge policy, conventions

Referenced from `SKILL.md`.

## Repo access (read this first, every time)

Your default `gh` auth may have **no access** to the configured repo.
Prefix every `gh` call with the token:

```bash
GITHUB_TOKEN=$(cat <your GitHub token file>) gh <command>
```

The token must be a **classic** PAT (`ghp_`) — fine-grained PATs are blocked by the
org's Free plan on check-runs and cannot write the custom Stage/Pipeline Status issue
fields.

**Known quirk**: `gh issue create` (porcelain) fails against this token
(`repository.defaultBranchRef` — a PAT limitation). The one code path that files new
issues is `sdlc_next.py create-issue` — it goes through the REST wrapper **and**
enforces the every-issue-has-a-parent-epic invariant (sets `Type: Task`, links as a
sub-issue of `--parent` in the same call):

```bash
python3 "$SDLC" create-issue \
  --title "..." --body "..." --parent <epic-issue-number>
```

No stage label needed under a standing epic — a fresh issue simply has no Stage
value, which `default_stage()` reads correctly. Under a non-standing Epic, follow with
`set-stage` (or let `merge-lld-doc` advance it, for a Task `create-lld-tasks` made). `--label` accepts a real label (e.g.
`epic:standing`) if genuinely needed.

Every other `gh issue`/`gh pr` porcelain subcommand works with this token, including
`--draft`, relabeling, and merging.

**Projects (v2) note**: the classic PAT carries full org scopes and Projects v2
boards **are** usable (confirmed via `gh project list`, field-list, and the v2
GraphQL mutations). The pipeline still doesn't *read* board state for decisions —
Stage/Pipeline Status/Priority/Effort are native Issue custom fields, not board
fields; the board's own `Status` field gets best-effort epic writes only (see
`references/epics.md`, "Epic board Status"). One real API limitation: a view's
`groupBy` cannot be set via GraphQL — UI-only.

## Issue taxonomy

Native Issue Type, the org-provisioned **Priority**/**Effort** issue fields, and the
native `blockedBy` relationship replace the old label taxonomy. The `stage:*`/
`status:*` labels are **retired** — throughout this skill, `stage:X`/`status:X` is
compact shorthand for "Stage field = X" / "Pipeline Status field = X", never an
actual label. `epic:architected` remains a runtime **state** marker (renameable via
`pipeline.labels.architected`). The old `epic:standing` / `epic:legacy` labels are no
longer read directly — an epic's behaviour is now a **profile** matched by label in
`pipeline.profiles` (see below and `references/epics.md`, "Epic profiles"); a client may
keep those labels or use its own (e.g. `RTB`).

### `pipeline.profiles` — epic behaviour by label

An ordered array; `resolve_profile(epic)` returns the first entry whose `match` (a
`{ "label": "<name>" }` or the string `"*"` catch-all) holds against the epic's labels.
Each entry is a bundle of toggles (`driven`, `epicLevelPhase` — `false` = standing,
`childrenNeedArchitectedEpic`, `closes`, and a `gates` block with
`skipConfidenceThreshold` / `requiresHumanGateA`); omitted toggles inherit shipped
defaults, and `gates` inherit the global `pipeline.gates`. The shipped defaults are
`legacy`, `standing` and the `"*"` catch-all `default`. A profile only ever applies to an
issue `pipeline.classification` already calls an epic — the profile label rides on top
of the epic classification, it never makes an issue an epic. Full field reference and a
worked example live in `sdlc.config.sample.json`. `product-review` is universal (not a
profile toggle); its model is `pipeline.models.product-review` (default opus).

| Category | Mechanism | Meaning |
|---|---|---|
| Stage | Native "Stage" field (`Product` / `Architecture` / `Development` / `Testing` / `PR Review` / `LLD`) | Sole source of truth `current_stage()` reads. Set in place as the unit advances (`claim`, `open-dev-pr`). `arch-review`/`lld-review`/`pr-review` set no value of their own (the `PR Review` option exists but nothing writes it) — they run immediately after the stage before them, signaled by comment content. `next-action` assigns `default_stage()`'s value the first time it sees an eligible issue with none set (`product` for an Initiative's child, a standing child or a parentless issue; `architecture` for a standing child or parentless `Bug`). A non-standing Epic's children get **no** default: phase-Tasks are staged by `set-stage`, functional Tasks by `merge-lld-doc`, and a late Stage-less child is reported as `unstaged`. Cleared entirely on issue close (`mark-issue-closed`), and on the Epic itself when `merge-lld-doc` marks it `epic:architected`. |
| Pipeline Status | Native "Pipeline Status" field (`Todo` / `In Progress` / `Awaiting Human Review` / `Feedback Received` / `Needs Human` / `Done`) | `In Progress` = actually claimed by a live run (crash-recovery marker) — the CI gate-advance path deliberately never sets it. `Awaiting Human Review` / `Feedback Received` = paused at an open gate (the latter is a visibility flip, same gate-pending state — `GATE_PENDING_STATUSES`). `Needs Human` = a resumed agent concluded only the operator can decide. `Todo` = set by the Action's `init-todo-status` job on `issues: opened` (fields have no schema default); the pipeline itself never writes it. `Done` = issue closed — set by the Action's `mark-issue-closed` job on every close, pipeline-driven or manual. Blocked-ness has **no** value here — derived live from `blockedBy`. |
| Type | Native Issue Type: `Task` / `Bug` / `Feature` (plus `Initiative`/`Epic` where provisioned) | Unit kind (Initiative / Epic / Task) is read **only** from `pipeline.classification` — label-based by default (`type:initiative` / `type:epic` / `type:task`); a parentless `Feature` is not an epic by itself. `Bug` fast-tracks to `architecture` **only for a standing-epic child** (or a parentless issue); under a non-standing Epic it gets no default stage and is routed by the orchestrator. |
| Priority | Native "Priority" field (`Urgent`/`High`/`Medium`/`Low`) | Assigned primarily **on epics**. No value = `Medium` for sorting. Readable on children too (`sort_key`) for intra-epic ordering. |
| Effort | Native "Effort" field (`High`/`Medium`/`Low`) | Assigned at the product stage. `High` alone is **not** a reason to split a Task — footprint collision is (see `references/epics.md`). |
| Relationship (blocking) | Native `blockedBy` (`addBlockedBy` mutation) | ≥1 *open* blocker = not eligible — derived live every `next-action` run, auto-clears when the blocker closes. Set via `mark-blocked <issue> --dep <n>`. |
| Assignee | Native assignee | Unassigned **is** the agent-owned state (see below). |

A hand-filed **child** **must** be linked as a sub-issue of an epic before
`next-action` will ever see it (the picker only looks at the named epic's children).
Under a standing epic it needs no Stage value; under a non-standing Epic it needs one
(`set-stage`), or `next-action` reports it as `unstaged`. A brand-new **Epic** runs
nothing by itself — the orchestrator cuts its Architecture-phase and LLD-phase Tasks
(`SKILL.md`, "Cutting an Epic's phase-Tasks"); it must carry the epic classification
label/type to be recognised at all.

## Assignee convention

Only the operator's own login is assignable — there is no agent account. **Unassigned
is the default, agent-owned state**: `claim`, `mark-needs-human`, and the gate
commands never mutate the assignee. `check-epics-closeable` still assigns the
operator when posting an epic's closing checklist — a one-time notification.

## Before opening any PR, check whether one already exists

Two sessions can drive this repo at once, and the second one to arrive has no way to
know the work is already done. On 2026-08-20 the same epic-doc PR was opened twice
(#220 and #226, byte-identical content) because the orchestrator branched without
checking the open-PR list — and the signal was already on screen, a leftover worktree
for the other session's branch in the very first `git worktree list`.

So, before creating a branch for any ad-hoc PR:

```bash
gh pr list --state open --json number,title,headRefName
git worktree list          # a worktree you did not create = another session is on it
```

`open-dev-pr` enforces this mechanically for development PRs — it returns the existing
PR with `created: false` rather than opening a second one on the same branch. Ad-hoc
PRs (doc landings, pipeline fixes) have no such guard, so check by hand.

If a duplicate does get opened, keep the one that is current with `main` and close the
other with a comment saying which superseded it and why — never leave two open PRs for
one change.

## PRs merge automatically — no human review gate

`~/.claude/CLAUDE.md` requires all PRs be drafts, human-promoted. **This pipeline
carries a standing, narrow exception, scoped only to PRs it opens in
the configured repo, per the operator's direct instruction**: development opens a
draft PR; once it passes adversarial `pr-review` and CI is green, the pipeline marks
it ready and merges it. This is separate from the human gates in product/architecture
— those pause deliberately; this step deliberately doesn't.

Loud and traceable, never quietly automatic:

- Every auto-merge leaves an audit-trail comment on the merged PR referencing this
  policy.
- The issue's closing comment states the PR number in words ("Merged via #<n>") —
  never rely solely on the automatic `Closes #n` link.
- The per-issue docs folder stays in the repo post-merge — never delete it.

**`merge-pr` is the only merge gate in this repo.** Branch protection and rulesets
are unavailable (`403 Upgrade to GitHub Pro`); don't go looking. `merge-pr` refuses
on any non-passing check, on a code-touching PR whose required suite has neither a
passing GHA check nor a fresh local-CI attestation (`requiredWorkflows` in
the config — keep in step with the workflows' `name:` fields), **and** — as a
structured exit-0 result — on a branch behind `origin/main` (green CI on a stale base
is meaningless under the parallel lane; see `references/parallelism.md`, "Merge-time
freshness gate").

**Carry-forward on a behind-base merge is a positive docs-only test, not "the base
delta doesn't touch a required suite."** Those read the same in a repo whose config
happens to declare a required suite over every file the base moved — but not in one
with a narrow or empty `requiredWorkflows`, where "doesn't touch a required suite" is
trivially true of *any* delta, code included, and would carry a stale attestation
forward over a base that changed real application code. The actual test is the
inverse and unconditional: the branch's existing attestation is carried forward only
when **every file** the base delta touched is a doc path; a base delta containing even
one non-doc file is re-attested, regardless of whether any `requiredWorkflows` entry's
`prefixes`/`files` happens to cover it. `merge-pr`'s result carries
`carried_attestation_forward: true` when this fires — treat its absence on a
behind-base result as "this needs `sync-branch`, fresh CI, and a re-attest," never as
"the config has no suite configured for this, so it's fine."

**Local-CI attestation (main-only GHA CI, 2026-09-04 operator cost directive).** The
backend (`Backend CI`) and frontend (`Frontend CI`) suites
no longer run on child PRs in GitHub Actions — the backend suite alone is a ~30-minute
Postgres-backed job that reran on every push of every child, and it was the dominant
Actions spend. Both are now **main-only** (`on: push: [main]` + `workflow_dispatch`;
`paths` also skip `**/*.md`). Under this pipeline, push-to-`main` happens when an
epic's integration branch merges at close — so each suite runs roughly once per epic
in CI, not once per child push.

They stay **mandatory for a child PR to merge**. The proof moves off the GHA check
and onto the suite `development` runs locally:

- `development`, once its suites pass, runs
  `record-local-ci --pr <pr> --suite backend|frontend --sha <HEAD> --command "..."
  --output <file>` — once per suite it actually ran — posting
  `<!-- local-ci: <suite>:<pr> @ <sha> -->` on the **PR**, with that run's own captured
  output embedded.
- **The captured output is not decoration.** Since the `testing` stage was merged into
  `development` on 2026-09-12, the implementer both writes and validates its own tests,
  and this attestation is the only mechanical thing between a self-run suite and the
  merge gate. It refuses an empty or missing output file, which is why a summary cannot
  be passed in its place.
- `merge-pr` / `pr-checks` accept that attestation in place of the GHA check, but
  **only while `<sha>` matches the PR's current head**. A commit pushed after the
  attestation (a rework round) makes it stale, and `development` must re-run and
  re-attest — the same freshness rule as the behind-base gate. `merge-pr` still
  independently requires the `development->pr-review` handoff and a clean `pr-review`
  outcome (`missing_pipeline_evidence`); local-CI is only the CI half.
- `infra.yml` is fully disabled (`workflow_dispatch` only) until the infra side is
  built out — it shipped zero roots and every run just no-op'd `discover`.
- `gate-auto-advance.yml` dropped its `issue_comment`/`pull_request_review` triggers
  (they fired a runner on every comment/review repo-wide for a pure-visibility flag
  `next-action` rebuilds anyway); its merge/close/open jobs are unchanged.

**`pr-checks` has a fourth status: `missing-checks`** — a required suite has neither
a passing GHA check nor a fresh local-CI attestation. Two distinct causes now, and
they route differently:

- **The main-only suite (backend/frontend) simply hasn't been attested for the
  current head yet** — the common case, and *not* a defect. It clears when
  `development` runs `record-local-ci` for that head (or re-runs it after a rework push
  staled the old attestation). If development handed off but skipped the attestation,
  that's the fix: re-run the suite and attest it. **Never poll** — no GHA run is coming
  on a child PR.
- **A genuine config defect** — a still-required GHA workflow never reported (renamed
  out of step with the config's `requiredWorkflows`, disabled, `paths:` mismatch). Route per
  `references/stage-playbooks.md` (`pr-review` exit actions); `mark-needs-human`.

Either way `missing-checks` is never a "still running" state — don't poll it.

**Config keys that shape the gate (retro #39, #42, #43).** Each `requiredWorkflows` entry
mirrors ONE GHA workflow's `paths:` filter:

- `prefixes` / `files` are the positive paths; `excludeGlobs` mirrors the workflow's own
  path **negations** (e.g. `!**/*.md` → `"excludeGlobs": ["**/*.md"]`). A changed file
  matching an `excludeGlob` does not count as touching the suite, so a docs-only edit
  inside a required prefix (e.g. `backend/AGENTS.md`) no longer demands a full attestation
  the real workflow would have skipped.
- `suite` keys may contain hyphens (`e2e-smoke`); a bad key is rejected at config load.
- `commandPattern` (optional) is a regex the `record-local-ci --command` must contain, so
  a suite can require its real invocation (e.g. `test:coverage`) instead of any non-empty
  command. Absent → any non-empty command is accepted.

## Per-run analytics (retro #59)

The control plane cannot see Claude's own token/tool usage — it lives in the harness,
surfaced per subagent in each task-notification's `<usage>` block (`subagent_tokens`,
`tool_uses`, `duration_ms`). So the orchestrator **feeds** those numbers in after each
stage/agent finishes:

```bash
python3 "$SDLC" record-run-metric <epic> --stage <s> --agent <a> \
  --tokens-out N --cache-read N --cache-write N --tool-calls N --peak-context N --duration-ms N
python3 "$SDLC" run-report <epic>      # totals + per-stage + per-agent, at close
```

Metrics live in the same per-epic run-state file as the run cap (`{worktrees.root}/.sdlc-runs/
epic-<n>.json`), so `next-action --run-id` resetting that file per run also scopes the
metrics to one run. `run-report` at close turns cost review into a repeatable, data-driven
step instead of after-the-fact transcript mining. (Open question for a later pass: whether
the harness can hand these numbers to the control plane directly.)

## Skill version and pin drift (retro #58, #48)

`show-config` reports `skillVersion` — the SHA of the skill code actually running
(`running_sha`), the SHA this repo pins for the submodule (`pinned_sha`), and `drift`.
When they differ it also returns `skill_drift_warning`: the control plane is running
unpinned code (the 2026-09-11 incident where the bootstrap checkout had drifted off the
pin). Read `show-config` once per invocation and re-sync the submodule if it warns.

## Break-glass: degraded local merge when the PR API is down

`merge-pr` completes a merge through the GitHub PR API (`gh pr ready`, then
`gh pr merge`). During a transient PR-API outage — those calls erroring while the git
remote itself still accepts pushes — the gate cannot finish and the merge has to be done
by hand. This is **break-glass only**: use it while the API is genuinely down, never as
a routine shortcut, and never to skip the checks `merge-pr` enforces. First verify every
one of them by hand — a passing GHA check or a fresh matching local-CI attestation, the
`development->pr-review` handoff with a clean `pr-review` outcome, and the behind-base
freshness gate (`sync-branch` first if the branch is behind `origin/main` on anything but
doc paths; see `references/parallelism.md`, "Merge-time freshness gate").

**Squash locally without a merge commit.** Merge the way `merge-pr` would — squash, never
a merge commit. A plain `git merge` leaves a merge commit that then has to be
force-fixed into a squash, which is exactly the mess this avoids:

```bash
git -C <ephemeral-worktree-on-origin/main> fetch origin
git -C <...> merge --squash origin/issue-<n>
git -C <...> commit -m "<message>  (Closes #<issue>)"
git -C <...> push origin main
```

Never force-push `main` to turn an already-pushed merge commit into a squash — reconcile
forward (below) instead.

**Detect a GitHub auto-merge before re-merging.** The transient commits can trip GitHub
into marking a PR merged on its own. Before merging by hand — and again once the API
recovers — read each affected PR's real state rather than trusting the last `merge-pr`
error:

```bash
GITHUB_TOKEN=$(cat <your GitHub token file>) gh pr view <pr> --json state,mergedAt,mergeCommit
```

A PR already `MERGED` needs no second merge; re-running the squash would double-apply it.

**Reconcile once the API is back.**

- For any PR GitHub already marked `MERGED`, confirm its content is on `main` and only
  close out issue state — do not re-merge.
- For a PR you squashed locally, close it with the audit-trail comment `merge-pr` would
  have left (this policy, "Merged via #<n>"), close its issue with the "Merged via #<n>"
  wording, and delete the merged branch.
- Reconcile the ledger the pipeline reads: the issue's closing comment and the
  Stage/Pipeline Status fields (`Done` on close), since the API path that normally sets
  them didn't run.

Record the outage and what was merged by hand, so the next survey doesn't read a
hand-closed PR as an anomaly.
