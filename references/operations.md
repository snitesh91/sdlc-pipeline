# Operations — repo access, issue taxonomy, merge policy, conventions

## Repo access

- `GITHUB_TOKEN` must be set for every `gh` and control-plane call; the SessionStart
  hook exports it from the config's `tokenEnv` var, else `tokenPath`, overriding any ambient one.
- The token must be a **classic** PAT (`ghp_`); fine-grained PATs cannot read check-runs
  or write the custom Stage/Pipeline Status fields.
- `gh` reads and comments work; issue creation is `create-issue` only ("Issue taxonomy").
- The pipeline neither reads nor writes Projects v2 board state; Pipeline Status is the
  only status it maintains (`references/epics.md`, "Epic board Status").

## Issue taxonomy

`stage:X` / `status:X` in this skill mean "Stage field = X" / "Pipeline Status field =
X" — never labels. `epic:architected` is a runtime state label (renameable via
`pipeline.labels.architected`). Unit kind (Initiative / Epic / Task) comes **only**
from `pipeline.classification` (label-based by default: `type:initiative` /
`type:epic` / `type:task`); a parentless `Feature` is not an epic.

Every issue the pipeline creates goes through `create-issue --parent <n> --title ..
--body .. --type <T> [--priority <P>] [--effort <E>]` — the orchestrator's command.
Before creating it refuses an unknown or unprovisioned type and an invalid Priority/Effort;
the refusal lists the valid values (Priority: `Urgent`/`High`/`Medium`/`Low`, plus the
aliases `Critical`/`Blocker` → `Urgent`; Effort: `High`/`Medium`/`Low`). It then sets the
Issue Type (mandatory), the parent link, Pipeline Status `Todo`, and Priority and Effort (flag,
else `pipeline.issueDefaults`; skipped when the field isn't configured), and adds the
classification label itself. A post-create failure returns `ok: false` naming the issue:
run the `repair-issue` command its `reason` gives, never re-run. `repair-issue <n>
[--parent <p>] [--type <T>] [--priority <P>] [--effort <E>]` sets only what is missing
(type inferred from the classification label; an Epic/Initiative's Pipeline Status is
left cleared) and never overwrites parent/type/status — but an explicit `--priority`/`--effort`
**is** set even over an existing value, so a half-created issue can be corrected to the
requested Priority/Effort. `file-closing-delta` self-repairs a mid-create failure in place
(passing the requested Priority/Effort to `repair-issue`) rather than duplicating the issue.
Under a non-standing Epic follow with `set-stage`
(Tasks from `create-lld-tasks` are staged by `merge-lld-doc`). `audit-issues [--epic
<n>]` lists open issues missing any of these fields (with `--epic`, only that Epic's
subtree), each with its `repair` command; an
Epic or Initiative is never flagged for lacking a parent.

| Field | Values | Rules |
|---|---|---|
| Stage | `Product` / `Architecture` / `Development` / `Testing` / `PR Review` / `LLD` | Sole source of `current_stage()`. Set by `claim` (inside `start-stage`, `pass-gate`, `skip-gate`, `waive-gate`), `set-stage`, `route`, `merge-lld-doc`; `open-dev-pr` sets `PR Review`. `arch-review`/`lld-review` write no value of their own. `Testing` is retired (only read, for issues stranded there). `next-action` sets `default_stage()` on first sight: `product` for an Initiative's child or parentless issue. A standing child gets none — `next-action` returns `route`. A non-standing Epic's children get **no** default (phase-Tasks via `set-stage`, Tasks via `merge-lld-doc`; a late Stage-less child is reported `unstaged`). Cleared on close, and on the Epic when it becomes `epic:architected`. |
| Pipeline Status | `Todo` / `In Progress` / `Awaiting Human Review` / `Feedback Received` / `Needs Human` / `Done` | `In Progress` = claimed by a live run (crash-recovery marker). `Awaiting Human Review` / `Feedback Received` = paused at an open gate (both gate-pending). `Needs Human` = only the operator can decide. `Todo` is set by `create-issue` (and the workflow on open), and on a unit parked or advanced unclaimed; `Done` on close. Blocked is not a value — it is derived from `blockedBy`. |
| Type | `Task` / `Bug` / `Feature` / `Epic` / `Initiative` | Mandatory; a defect is type `Bug`, never a `Task` with a label. Routing ignores it. |
| Priority | `Urgent` / `High` / `Medium` / `Low` | Set by `create-issue`; empty = `Medium`. `Critical` / `Blocker` are accepted as aliases for `Urgent`. Orders children. |
| Effort | `High` / `Medium` / `Low` | Set by `create-issue`; read by nothing. `High` alone is not a reason to split a Task — footprint collision is (`references/epics.md`). |
| `blockedBy` | native relationship | ≥ 1 open blocker = not eligible; clears when the blocker closes. Set with `mark-blocked <n> --dep <m>`. |
| Assignee | native | Unassigned = agent-owned (below). |

A hand-filed child must be a sub-issue of an epic or `next-action` never sees it; under a
non-standing Epic it also needs `set-stage`. A new Epic runs nothing itself — cut its
phase-Tasks (`SKILL.md`); it must carry the epic classification.

### `pipeline.profiles` — epic behaviour by label

Ordered array; `resolve_profile(epic)` returns the first entry whose `match`
(`{ "label": "<name>" }` or `"*"`) holds. Toggles: `driven`, `epicLevelPhase` (`false` =
standing), `childrenNeedArchitectedEpic`, `closes`, and `gates`
(`skipConfidenceThreshold`, `requiresHumanGateA`, `requiresHumanGateB`). Omitted toggles inherit shipped
defaults (`legacy`, `standing`, catch-all `default`); `gates` inherit `pipeline.gates`. A
profile applies only to an issue `pipeline.classification` already calls an epic.
Field reference: `sdlc.config.sample.json`. `product-review` is not a toggle.

## Assignee convention

Only the operator's login is assignable; there is no agent account. Unassigned is the
agent-owned state: `claim`, `mark-needs-human` and the gate commands never change the
assignee. `check-epics-closeable` assigns the operator once, with an epic's closing
checklist.

## Before opening any PR, check whether one already exists

Before creating a branch for any ad-hoc PR (doc landing, pipeline fix):

```bash
gh pr list --state open --json number,title,headRefName
git worktree list          # a worktree you did not create = another session is on it
```

(`open-dev-pr` already returns an existing PR with `created: false`.) If a duplicate is
opened anyway, keep the one current with `main` and close the other with a comment
naming its replacement. Never leave two open PRs for one change.

## PRs merge automatically — no human review gate

Standing, narrow exception to any draft-PR rule in the operator's global `CLAUDE.md`, scoped
to PRs this pipeline opens in the configured repo: `development` opens a draft; after a clean
adversarial `pr-review` and green CI, `merge-pr` marks it ready and squash-merges it. The
human gates on product/architecture are separate and unaffected.

- **A phase-Task's design PR** (`issue-<n>` → `epic-<n>`, docs only) merges with
  `merge-design-pr` (directly, or inside `skip-gate`/`waive-gate`/`finish-lld`) after
  `lld-review` records clean, or `arch-review` records clean above the skip threshold /
  under a profile that waives Gate B; otherwise the human merges it at Gate B. It runs no
  code checks, keeps the branch, and never closes the Task. `merge-pr` refuses a phase-Task.
  A gate PR the operator told you to merge goes through `merge-gate --operator-confirmed`
  (`references/gates.md`, "Merging a gate PR for the operator").
- Merge only with `merge-pr`, run by the orchestrator after `pr-review` records clean — a
  reviewer never merges the diff it reviewed. It posts the audit-trail comment on the PR
  and "Merged via #<n>" on the issue, and closes a child merged into an epic branch.
- `merge-pr` is idempotent: on a PR already `MERGED` (a retry, or a 5xx after the squash
  landed) it only finishes the bookkeeping and returns `recovered: true`.
- **A development PR authors no design doc, and touches no other Task's footprint.**
  `open-dev-pr` refuses (before opening) a branch diff that edits any Epic design doc under
  `<docRoot>/epic-*/` (e.g. `lld.md`) or a file listed in another open Task's `## Footprint`
  of the Epic's `lld.md`; files in the unit's own footprint are fine. `verify-exit` reports
  the same offenders on the pr-review handoff (`dev_pr_scope`). If the design truly needs to
  change, escalate an Architecture/LLD revision and record the deviation in the PR
  description — do not edit the doc from a development branch.
- Never delete the per-issue docs folder after merge.
- `merge-pr` is the only merge gate (branch protection is unavailable — don't look for
  it). It refuses on a non-passing check, a code-touching PR whose required suite has
  neither a passing GHA check nor a fresh local-CI attestation, missing pipeline
  evidence (`missing_evidence`), or a stale base (`behind_base` —
  `references/parallelism.md`, "Git-conflict handling").

## Local-CI attestation

A required suite (`requiredWorkflows`; keep in step with the workflows' `name:` fields) is
required on a PR only when the PR changes a file its entry covers; an empty or docs-only diff
requires nothing. **One rule wherever the pipeline checks a suite** (`transition` into
`pr-review`, `pr-checks`, `merge-pr`, `close-epic`): the suite is satisfied by **a passing
GHA check for its workflow on the current PR head OR a `record-local-ci` attestation for
that head**. A suite whose workflow runs on every PR it applies to needs no local run at
all — mark it `attestable: false` so no stage is ever asked for one. `record-local-ci` is
for a suite with no PR-level CI (a workflow that runs only on push to `main`).

- Attesting: for each attestable suite it actually ran and passed, `development` runs
  `record-local-ci --pr <pr> --suite <suite> --sha <HEAD> --command "..." --output <file>`,
  posting `<!-- local-ci: <suite>:<pr> @ <sha> -->` on the PR with the captured output. It
  refuses a missing or empty output file (never pass a summary), a `--command` still holding
  an unexpanded `<placeholder>` token (a template copied from the docs; genuine shell syntax
  `< file`, `2>&1`, `<(...)` is fine), and a suite configured `attestable: false`.
- An attestation counts only while `<sha>` is the PR's current head. A later push (rework,
  `sync-branch`) makes it stale unless the push only merged files outside the suite's
  coverage, which carries it forward (`carried_attestation_forward`), never re-run. A passing
  check on the new head satisfies the suite too: re-run and re-attest only when neither holds.
- The rule is applied **post-sync**: `transition --expect-stage pr-review` runs
  `start-comment` (role pr-review) after `sync-branch`. It refuses (`ok: false`, no start
  comment) when an attestable suite the PR touches has neither, returning `held`: one
  `{suite, workflow, attested_sha, check, reason}` per suite, `check` being the workflow's
  state on the head (`pending` / `failing` / `skipped` / `missing`) and `reason` e.g.
  `` `backend`: no local attestation on head 1a2b3c4 and check 'Backend Validate' is pending ``.
  `pending` → wait, re-run `transition`. `failing` → the PR is not reviewable; resume
  `development`. `missing` → the suite has no PR-level CI: run it and `record-local-ci`
  (or see `missing-checks` below). Suites satisfied by their check are listed as
  `satisfied_by_check`. A non-attestable suite is never held here; `merge-pr` gates it.
- `merge-pr` / `pr-checks` apply the same OR rule per suite; `merge-pr` also refuses on any
  failing or pending check on the PR.
- `merge-pr` accepts `--run-id`: the terminal-unit count is booked under that run rather
  than whatever id last wrote the epic's run-state file (which may be a throwaway probe
  run's). Without it, the state file's current id stands.
- Each `requiredWorkflows` entry mirrors one workflow's `paths:` filter: `prefixes` /
  `files`, plus `excludeGlobs` for its negations (`!**/*.md` → `"**/*.md"`); a changed
  file matching one never requires the suite. `suite` keys are `[A-Za-z0-9_-]+` (checked
  at config load). An optional `commandPattern` regex must match the attested `--command`.
  Optional **`bases`** (e.g. `["main"]`) scopes an entry to those PR base branches only
  (omitted = every base) — so an integration suite can be required on PRs into `main` yet
  ignored on child PRs into an epic branch. Optional **`attestable: false`** (default
  `true`): the workflow runs on every PR the entry applies to, so only its passing check
  satisfies the suite — no stage runs it locally, `record-local-ci` refuses it, and
  `close-epic` lists it under `awaiting_checks` instead of `unattested_suites`.

**`pr-checks` status `missing-checks`** is never "still running" — never poll it. When it
is set, `pr-checks` returns a `hint` field naming the causes in likelihood order (and
`merge-pr`'s refusal carries the same hint):

- The workflow does not run on this PR (e.g. main-only) and its suite is not attested for
  the current head (the usual case, not a defect) → `development` re-runs the suite and
  `record-local-ci` for that head.
- The workflow file is not on the PR branch (added on the base after the branch was cut)
  → run `sync-branch` and push so it can run.
- A still-required GHA workflow never reported (renamed out of step with
  `requiredWorkflows`, disabled, `paths:` mismatch) → config defect: `pr-review` stops
  with `needs-human` (`agents/pr-review.md`, exit actions); you run `mark-needs-human`.

## Break-glass: local merge while the PR API is down

Only while `gh pr ready`/`gh pr merge` fail and the git remote still accepts pushes — never
as a shortcut, never skipping a check `merge-pr` enforces:

1. Verify by hand what `merge-pr` would: passing checks or a fresh matching local-CI
   attestation, the `development->pr-review` handoff with a clean `pr-review`, and the
   freshness gate (`sync-branch` first when behind on anything but docs).
2. Read each affected PR's real state (`gh pr view <pr> --json state,mergedAt`); a
   `MERGED` PR needs no second merge.
3. Squash, never a merge commit, in an ephemeral worktree on `origin/<base>` (`epic-<n>` for
   a Task of a non-standing Epic, else `main`): `git merge --squash origin/issue-<n>`,
   `git commit -m "<title> (Closes #<n>)"`, `git push origin <base>`. Never force-push
   `main`; reconcile forward.
4. Once the API is back: close the PR with the audit-trail comment `merge-pr` would post,
   close the issue with "Merged via #<pr>", delete the branch, and set its terminal
   fields (`close-issue`). Record the outage and what was merged by hand.
