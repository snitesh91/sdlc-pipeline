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

No stage label needed — a fresh issue simply has no Stage value, which
`default_stage()` reads correctly. `--label` accepts a real label (e.g.
`epic:standing`) if genuinely needed.

Every other `gh issue`/`gh pr` porcelain subcommand works with this token, including
`--draft`, relabeling, and merging.

**Projects (v2) note**: the classic PAT carries full org scopes and Projects v2
boards **are** usable (confirmed via `gh project list`, field-list, and the v2
GraphQL mutations). The pipeline still doesn't *read* board state for decisions —
Stage/Pipeline Status/Priority/Effort are native Issue custom fields, not board
fields; the board's own `Status` field gets best-effort epic-level writes only (see
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
Each entry is a bundle of toggles (`driven`, `epicLevelPhase`, `childEntryStage`,
`childrenNeedArchitectedEpic`, `closes`, and a `gates` block with
`skipConfidenceThreshold` / `requiresHumanGateA`); omitted toggles inherit shipped
defaults, and `gates` inherit the global `pipeline.gates`. The shipped defaults reproduce
the historical `standing` / `legacy` / `default` behaviour. Full field reference and a
worked example live in `sdlc.config.sample.json`. `product-review` is universal (not a
profile toggle); its model is `pipeline.models.product-review` (default opus).

| Category | Mechanism | Meaning |
|---|---|---|
| Stage | Native "Stage" field (`Product` / `Architecture` / `Development` / `Testing` / `PR Review` / `LLD`) | Sole source of truth `current_stage()` reads. Set in place as the unit advances (`claim`, `open-dev-pr`). `arch-review`/`lld-review`/`pr-review` set no value of their own (the `PR Review` option exists but nothing writes it) — they run immediately after the stage before them, signaled by comment content. `next-action` assigns `default_stage()`'s value the first time it sees an eligible issue with none set (`lld` for a child of a normal architected epic; else `product`, or `architecture` for a `Bug`). Cleared entirely on issue close (`mark-issue-closed`) and on `_complete_epic_architecture`. |
| Pipeline Status | Native "Pipeline Status" field (`Todo` / `In Progress` / `Awaiting Human Review` / `Feedback Received` / `Needs Human` / `Done`) | `In Progress` = actually claimed by a live run (crash-recovery marker) — the CI gate-advance path deliberately never sets it. `Awaiting Human Review` / `Feedback Received` = paused at an open gate (the latter is a visibility flip, same gate-pending state — `GATE_PENDING_STATUSES`). `Needs Human` = a resumed agent concluded only the operator can decide. `Todo` = set by the Action's `init-todo-status` job on `issues: opened` (fields have no schema default); the pipeline itself never writes it. `Done` = issue closed — set by the Action's `mark-issue-closed` job on every close, pipeline-driven or manual. Blocked-ness has **no** value here — derived live from `blockedBy`. |
| Type | Native Issue Type: `Task` / `Bug` / `Feature` | `Feature` **with no parent** = an Epic (there is no separate Epic type). `Bug` fast-tracks to `architecture` **only for a standing-epic child**; against a normal architected epic it starts at `lld` like any child. Everything else is `Task`. |
| Priority | Native "Priority" field (`Urgent`/`High`/`Medium`/`Low`) | Assigned primarily **on epics**. No value = `Medium` for sorting. Readable on children too (`sort_key`) for intra-epic ordering. |
| Effort | Native "Effort" field (`High`/`Medium`/`Low`) | Assigned at the product stage (epic-level pass sets all children at once). `High` alone is **not** a reason to split a normal-epic child — footprint collision is (see `references/epics.md`). |
| Relationship (blocking) | Native `blockedBy` (`addBlockedBy` mutation) | ≥1 *open* blocker = not eligible — derived live every `next-action` run, auto-clears when the blocker closes. Set via `mark-blocked <issue> --dep <n>`. |
| Assignee | Native assignee | Unassigned **is** the agent-owned state (see below). |

A hand-filed **child** needs no Stage value but **must** be linked as a sub-issue of
an epic before `next-action` will ever see it (the picker only looks at the named
epic's children). A brand-new **epic** needs nothing — eligible for its epic-level
phase the moment it exists, unless marked `epic:standing`/`epic:legacy`.

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
