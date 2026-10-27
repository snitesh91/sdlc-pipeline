# Operations — repo access, issue taxonomy, merge policy, conventions

## Repo access

- Every `gh` and control-plane call needs a **classic** PAT (`ghp_`). Fine-grained PATs cannot read check-runs or write the Stage/Pipeline Status fields.
- Locally, an ambient `GH_TOKEN` / `GITHUB_TOKEN` is used as is. Otherwise, and always in a cloud container (`CLAUDE_CODE_REMOTE`), the SessionStart hook exports both from the config's `tokenEnv` var, else `tokenPath`.
- Create issues only with `create-issue` ("Issue taxonomy").
- In a cloud session, read with `gh api` GETs and write only through `python3 "$SDLC"` (`references/cloud-mode.md`, "GitHub access in the cloud").
- The pipeline never reads or writes Projects v2 board state. Pipeline Status is the only status it keeps (`references/epics.md`, "Epic board Status").

## Plugin pin

- A session runs the marketplace `ref` pinned in `.claude/settings.json` of the checkout it starts in (the driven repo's main checkout). It reads the pin once, at session start, never from a unit's worktree. CI uses the `SDLC_PIPELINE_REF` Actions variable.
- A bump merged to `main` reaches every unit, in-flight Epics included, from the next session that installs it. Bump only while no run is live: agents read the playbook mid-run.
- Install a new tag: `claude plugin marketplace add "<owner>/sdlc-pipeline#<tag>" --scope project`, revert the reformat it makes to `.claude/settings.json`, then `claude plugin update sdlc@sdlc-pipeline --scope project`. `claude plugin marketplace update` never re-reads the pinned ref.
- A session keeps the plugin it loaded. `next-action` stamps the run's root issue with its version (`sdlc:plugin-version` marker). It refuses, naming both versions, when a newer plugin last moved the run or its Initiative: continue from a session on that version or newer.
- A new run on a plugin older than the repo pin gets `plugin_warning`. Relay it.

## Auto-mode classifier denies a composite

- The classifier can deny a heavy composite (e.g. `finish-lld`) even under a `Bash(python3 "$SDLC" *)` allow rule. You cannot override it yourself. Ask the operator to switch the session out of auto mode (or restart with `sdlc-run <n> --permission-mode default`), then re-run the command. Composites are idempotent.
- A denied `git push` or `open-dev-pr` never parks a finished unit. Run the denied step from the main thread (or ask the operator to approve it), then resume the agent for its remaining exit actions. The operator can add the allow rules below.
- When the main thread is denied too: `mark-needs-human <n> --reason "..." --classifier-denied <step> --run-id "$RUN_ID"`. The worktree is kept. The next run's `next-action` returns it as `resume` (`reason_code: classifier_denied`, `denied_step`): retry `denied_step` first. The run that parked it never resumes it.
- A classifier **outage** (every state-changing call errors while reads work) is not a denial and not a stage failure. Resume the agent to retry its exit action once the classifier answers. Never re-run the stage. You may record a review's reported verdict yourself (SKILL.md, "After the subagent returns").

## Auto-mode allow rules for `development`'s push and PR

An explicit allow rule resolves before the classifier. Add to the driven repo's `.claude/settings.json` (the Bash guard still denies force-push):

```json
{ "permissions": { "allow": ["Bash(git push *)", "Bash(git -C * push *)", "Bash(python3 \"$SDLC\" open-dev-pr *)"] } }
```

## Auto-mode allow rule for the `pr-review` mutation probe

The probe edits a file in the review worktree (default `/tmp/sdlc-review-<n>/`), outside the project. Add to the driven repo's `.claude/settings.json` (`//` is an absolute path):

```json
{ "permissions": { "allow": ["Edit(//tmp/sdlc-review-*/**)"] } }
```

Revert the probe with `git restore <file>`. The Bash guard denies a review role `git checkout -- <file>`, commit, push, merge and reset.

## Issue taxonomy

- `stage:X` / `status:X` mean "Stage field = X" / "Pipeline Status field = X", never labels. `epic:architected` is a runtime label (`pipeline.labels.architected`).
- Unit kind (Initiative / Epic / Task) comes **only** from `pipeline.classification` (default labels `type:initiative` / `type:epic` / `type:task`). A parentless `Feature` is not an Epic.
- Create every issue with `create-issue --parent <n> --title .. --body .. --type <T> [--priority <P>] [--effort <E>]`. It sets every field below and the classification label (Priority/Effort from the flag, else `pipeline.issueDefaults`). A refusal lists the valid values.
- On a post-create failure (`ok: false`), run the `repair-issue` command its `reason` gives. Never re-run `create-issue`.
- Under a non-standing Epic, follow `create-issue` with `set-stage` (`finish-lld` stages the Tasks it creates; a closing-run `Bug` goes through `file-closing-delta`).
- `audit-issues` lists open issues missing a field with each `repair` command, children first. Never let the container's repair hold up the children.

| Field | Values | Rules |
|---|---|---|
| Stage | `Product` / `Architecture` / `Development` / `Testing` / `PR Review` / `LLD` | Sole source of `current_stage()`. Set by `claim`, `set-stage`, `route`, `merge-lld-doc`; `open-dev-pr` sets `PR Review`. Reviews set none. `Testing` is retired. `next-action` sets `product` on an Initiative's child or a parentless issue; a standing child gets `route`; a non-standing Epic's Stage-less child is `unstaged`. Cleared on close. |
| Pipeline Status | `Todo` / `In Progress` / `Awaiting Human Review` / `Feedback Received` / `Needs Human` / `Done` | `In Progress` = claimed by a live run. `Awaiting Human Review` / `Feedback Received` = gate-pending. `Needs Human` = operator decides. Blocked derives from `blockedBy`. |
| Type | `Task` / `Bug` / `Feature` / `Epic` / `Initiative` | Mandatory. A defect is `Bug`, never a labelled `Task`. Routing ignores it. |
| Priority | `Urgent` / `High` / `Medium` / `Low` | Empty = `Medium`. `Critical` / `Blocker` alias `Urgent`. Orders children. |
| Effort | `High` / `Medium` / `Low` | Read by nothing. `High` alone never splits a Task; a footprint collision does (`references/epics.md`). |
| `blockedBy` | native relationship | ≥ 1 open blocker = not eligible. Set with `mark-blocked <n> --dep <m>`. |
| Assignee | native | Unassigned = agent-owned ("Assignee convention"). |

A hand-filed child must be a sub-issue of its Epic, or `next-action` never sees it. Under a non-standing Epic it also needs `set-stage`. A new Epic runs nothing itself: cut its phase-Tasks (SKILL.md).

### `pipeline.profiles` — epic behaviour by label

Ordered array. `resolve_profile(epic)` returns the first entry whose `match` (`{ "label": "<name>" }` or `"*"`) holds. Toggles: `driven`, `epicLevelPhase` (`false` = standing), `childrenNeedArchitectedEpic`, `closes`, `gates` (`skipConfidenceThreshold`, `requiresHumanGateA`, `requiresHumanGateB`). Omitted toggles inherit the shipped defaults (`legacy`, `standing`, catch-all `default`); `gates` inherit `pipeline.gates`. A profile applies only to an issue `pipeline.classification` calls an Epic. Field reference: `sdlc.config.sample.json`.

### `pipeline.initiativeProfiles` — Initiative behaviour by label (opt-in)

Ordered array, default `[]`; the first entry whose `match` is on the Initiative wins. With no match, the plain flow holds. A failed read while resolving it stops the step; it never falls back to a base. `initiative-<n>` is a pipeline branch only while `initiativeProfiles` is set. Toggles (`sdlc.config.sample.json`):

- `branch` (`true` = `initiative-<i>`, `references/epics.md`, "Initiative branch").
- `testTasks` (`false` = no standing test Tasks).
- `deferSuites`: suites `close-epic` does not demand into the initiative branch. `closeSuites`: suites `merge-initiative-pr` needs attested at its head.
- `placement` (`cloud` | `local`): overrides `placement.initiativeEpics`.
- `reviewDepth: light`: single-pass reviews, no fan-out, no mutation probe.

`initiative-profile <n>` prints the profile for any unit.

## Dropping a unit

Never close a dropped unit as completed. State why on the thread:

- **An Epic under an Initiative:** run `detach-epic <epic> --reason "<why>"` first. It removes the sub-issue link and every `blockedBy` edge to sibling Epics. Then `close-issue <epic> --not-planned --reason "<why>"`, and each open child the same way.
- **Any other unit:** `close-issue <n> --not-planned --reason "<why>"`. A dirty or unpushed worktree is refused and reported, never destroyed.
- Leave any other audit note with `comment <n> --body "..."` (orchestrator only; stage agents use `post-comment`).

## Worktree release

- Every release path (`close-issue`, `merge-pr`, `mark-blocked`, `mark-needs-human`, `close-epic`) first runs `pipeline.worktrees.releaseCommand` inside the tree, when set (e.g. `make it-down`). It runs only on a tree about to be removed. A failure is reported (`release_command.ok: false`), never fatal. A tree released without it leaves that env for you to tear down.
- On a terminal unit, `merge-pr` and `close-issue` run `cleanup`: worktrees, then branches whose work landed (unmerged work is kept, `retained_local_branch`). `close-epic` does the same for the Epic and its children; `prune-stale` sweeps every closed unit.
- A failed deletion (not a keep) comes back as a top-level `warnings` list on `merge-pr`, `close-issue`, `close-epic`, `prune-stale` and their composites. Relay every line to the operator at once and again in Step 4; the operator removes the branch.

## Assignee convention

Only the operator's login is assignable. Unassigned is agent-owned. Only `check-epics-closeable` assigns (the operator, once, with an Epic's closing checklist).

## Before opening any PR, check whether one already exists

Before any ad-hoc PR, list open PRs (`gh pr list --state open`; cloud: `gh api 'repos/<repo>/pulls?state=open'`) and `git worktree list` (a worktree you did not create = another session). If a duplicate exists, keep the one current with `main` and close the other naming its replacement.

## PRs merge automatically — no human review gate

This is a standing exception to any draft-PR rule in the operator's global `CLAUDE.md`, for PRs this pipeline opens. `development` opens a draft. After a clean `pr-review` and green CI, `merge-pr` marks it ready and squash-merges it. Human gates on product and architecture are separate.

- **A phase-Task's design PR** merges inside `skip-gate`/`waive-gate`/`finish-lld`, or by the human at Gate B. `merge-pr` refuses a phase-Task. A gate PR the operator told you to merge: `merge-gate --operator-confirmed`.
- Only the orchestrator merges, with `merge-pr`, after `pr-review` records clean. `merge-pr` does every post-merge write.
- **A development PR authors no open Epic's design doc and touches no other Task's footprint.** `open-dev-pr` and `verify-exit` (`dev_pr_scope`) refuse it. A closed Epic's doc correction passes. An open Epic's design-doc path never passes as a deviation. A foreign-footprint path listed under `## Footprint deviations` passes as `acknowledged_deviations` for `pr-review` to judge. A design that must change needs an Architecture revision.
- Never delete the per-issue docs folder after merge.
- `merge-pr` is the only merge gate (no branch protection). It refuses on a non-passing check, a required suite with neither a passing check nor a fresh attestation, missing pipeline evidence (`missing_evidence`), or a stale base (`behind_base`: `references/parallelism.md`, "Git-conflict handling"). It pins the squash to the head SHA it read (`--match-head-commit`).
- Pass `--run-id` to `merge-pr` so the terminal count books under this run.

## Local-CI attestation

- A required suite (`requiredWorkflows`, in step with the workflows' `name:`) applies to a PR only when the PR changes a file its entry covers.
- **One rule** wherever a suite is checked (`transition` into `pr-review`, `pr-checks`, `merge-pr`, `close-epic`): a passing GHA check for its workflow on the PR head **or** a `record-local-ci` attestation for that head. Which suites a stage runs at all: `references/verification-rules.md`, "Test scope".
- Attest with `record-local-ci --pr <pr> --suite <suite> --sha <HEAD> --command "..." --output <file>`, only for a suite actually run and passed. Its refusals name the fix.
- An attestation counts only while `<sha>` is the PR head. A push makes it stale, unless the push only merged files outside the suite's coverage (`carried_attestation_forward`). Re-run and re-attest only when neither an attestation nor a passing check holds.
- `transition --expect-stage pr-review` applies the rule after sync. It returns `held` with a per-suite `check` (`pending` / `failing` / `skipped` / `missing`) and `reason`:
  - `pending`: wait, re-run `transition`.
  - `failing`: resume `development`.
  - `missing`: no PR-level CI; run the suite and `record-local-ci` (or see `missing-checks` below).
  - Suites passed by their check are listed in `satisfied_by_check`. A non-attestable suite is never held here; `merge-pr` gates it.
- **A check carries across a sync merge** (`pipeline.ci.carryChecksAcrossSync`, default `true`): a pending, cancelled or absent run on a sync-merge head takes its pre-merge pass when the merge touched none of its paths or the config (`<workflow> (carried)`). A failing run is never overridden. Cancel the redundant run.
- `requiredWorkflows` entries (`sdlc.config.sample.json`): `prefixes` / `files` / `excludeGlobs` mirror the workflow's `paths:`; optional `commandPattern`, `bases` (fnmatch globs) and `attestable: false` (only the check counts; `record-local-ci` refuses it where the workflow covers the PR; `close-epic` lists it under `awaiting_checks`). A PR into a non-`main` base also owes that base's committed entries.

**`pr-checks` status `missing-checks`** never means "still running". Never poll it. Act on its `hint` (also on `merge-pr`'s refusal), causes in likelihood order:

- The workflow does not run on this PR and the suite is not attested: `development` runs it and `record-local-ci`, only where "Test scope" owes that run. On a Task PR into `epic-<n>`, an integration suite here is a config defect (`bases` missing).
- The workflow file is not on the PR branch: `sync-branch` and push.
- A required workflow never reported (renamed, disabled, `paths:` mismatch): config defect. `pr-review` stops with `needs-human`; you run `mark-needs-human`.

## A check the runner failed

`pr-checks` gives each failed check a `failure_excerpt` and `infra_suspect`, plus a top-level `infra_suspect` and `hint`. `merge-pr`'s refusal and `close-epic`'s `failing_checks` carry the same. On `infra_suspect`, get the runner fixed (operator or infra), then `rerun-checks <pr>`. Never send it back as code rework.

## Break-glass: local merge while the PR API is down

Use only while `gh pr ready`/`gh pr merge` fail and the remote still accepts pushes. Never skip a check `merge-pr` enforces.

1. Verify by hand what `merge-pr` would: checks or attestation, a clean `pr-review`, a current base. Confirm the PR is not already merged.
2. In an ephemeral worktree on `origin/<base>`: `git merge --squash origin/issue-<n>`, `git commit -m "<title> (Closes #<n>)"`, `git push origin <base>`. Never force-push `main`.
3. Once the API is back: close the PR with `merge-pr`'s audit comment, `close-issue` with "Merged via #<pr>", delete the branch, and record what you merged by hand.
