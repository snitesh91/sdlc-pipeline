# sdlc-pipeline

A Claude Code plugin (`sdlc`) that runs an agent-driven SDLC over GitHub Issues.
`/sdlc:run <n>` drives an Initiative or Epic stage by stage — `product`, `architecture`,
`lld` as their own phase-Tasks, then each Task through `development` → `pr-review` → merge —
with human gates on the product and architecture docs. A deterministic control plane
(`scripts/sdlc_next.py`) owns every GitHub and branch mutation; hooks enforce that.

The plugin is generic. Everything project-specific lives in one config file in the repo you drive.

## Layout

```
.claude-plugin/                  plugin.json, marketplace.json (this repo is its own marketplace)
skills/run/SKILL.md              the orchestrator contract, invoked as /sdlc:run <n>
agents/*.md                      stage agents, addressed as sdlc:<role>
references/*.md                  on-demand rules shared by the skill and the agents
templates/*.template.md          product.md / architecture.md skeletons
hooks/                           hooks.json, the hook scripts, model_policy.json, hooks/tests
bin/sdlc-run                     starts a run on the policy's orchestrator model (on PATH)
scripts/sdlc_next.py             the control plane; scripts/tests is its suite
scripts/sdlc_metrics.py          cost/outcome report over the metrics store
workflows/gate-auto-advance.yml  real-time gate backstop, copied into the driven repo
sdlc.config.sample.json          config template
```

## Install in a driven repo

1. **Enable the plugin** in the repo's `.claude/settings.json`, pinned to a tag:

   ```json
   {
     "extraKnownMarketplaces": {
       "sdlc-pipeline": { "source": { "source": "github", "repo": "snitesh91/sdlc-pipeline", "ref": "<tag>" } }
     },
     "enabledPlugins": { "sdlc@sdlc-pipeline": true }
   }
   ```

2. **Add the config.** Copy `sdlc.config.sample.json` to `<repo>/sdlc-pipeline.config.json`
   (repo root, `.config/` or `.claude/`), fill in every value and commit it. A missing
   config is a loud error, never a silent run on placeholders.
3. **Provision GitHub's org-level Issue Types** `Task`, `Bug`, `Feature`, `Epic`,
   `Initiative` (mandatory: every issue the pipeline files gets one) **and custom Issue
   Fields** `Stage`, `Pipeline Status` (plus optional `Priority`, `Effort`) via
   Settings → Issue types / Issue fields. No Projects-v2 board is involved. Look up the ids
   for `projectFields` once, from your own terminal:

   ```bash
   gh api graphql -f query='query { repository(owner:"OWNER", name:"REPO") {
     issueTypes(first:20){nodes{id name}} } }'
   gh api graphql -f query='query { organization(login:"ORG") {
     issueFields(first:20){nodes{ ... on IssueFieldSingleSelect { id name options { id name } } }} } }'
   ```

4. **Copy the workflow**: `workflows/gate-auto-advance.yml` → `<repo>/.github/workflows/`.
   Set the Actions secret `SDLC_GH_TOKEN` (classic PAT) and the Actions variable
   `SDLC_PIPELINE_REF` to the same tag `.claude/settings.json` pins (defaults to `main`).
5. **Token**: point the config's `tokenPath` at a file holding a classic PAT (`ghp_`);
   fine-grained PATs cannot read check-runs or write the custom fields. The SessionStart
   hook exports it as `GITHUB_TOKEN` unless you already set one.
6. Optional: a repo-specific `<docRoot>/_templates/{product,architecture}.template.md`
   overrides the plugin's templates.
7. Start a run with `sdlc-run <initiative-or-epic-number> [claude args]`: it launches `claude`
   on the policy's orchestrator model with `/sdlc:run <n>` (a skill's `model:` does not
   outlast its turn).

**Upgrading**: bump the `ref` in `.claude/settings.json` and `SDLC_PIPELINE_REF` together,
run `claude plugin marketplace update sdlc-pipeline`, and start a new session. Bump only
when no run is live. There is one plugin version per driven repo.

Repo-specific commands (lint, build, test, suite names, ports) belong in the driven repo's
`CLAUDE.md` / `AGENTS.md`, which every stage agent reads.

## Hooks

Every hook is a no-op unless the session's git toplevel holds `sdlc-pipeline.config.json`.
They are stdlib Python, and they fail open on internal errors.

| Hook | Enforces |
|---|---|
| `SessionStart` | Exports `$SDLC` and `GITHUB_TOKEN` (from `tokenPath`) for every Bash call; warns when no token is available; suggests `rtk init` if `rtk` is absent; flags a session model off the policy's orchestrator model; after a compaction, restates this session's run (epic, run-id, units in flight) |
| `PreToolUse` (Bash) | Denies hand-run `gh api graphql`, `gh issue create/edit/close/reopen`, `gh pr create/merge/ready/close`, mutating `gh api`, `git worktree add` (except `--detach`), force-push and rebase, naming the `python3 "$SDLC"` command to use instead; limits each `sdlc:<role>` agent to its role's control-plane commands and review roles to no git writes. Only each segment's leading words count |
| `PreToolUse` (Agent) | Sets every `sdlc:*` agent's `model` from `hooks/model_policy.json` (overridable per role by `pipeline.models` / `pipeline.fanout`); denies nested stages and fan-out a role may not do or has exhausted |
| `SubagentStart` | Gives each `sdlc:*` agent `$SDLC`, `docRoot`, `requirementsDir`, the references path and the `SDLC-RESULT` format |
| `SubagentStop` | An `sdlc:*` agent cannot stop until its final message ends with `SDLC-RESULT: {"issue": <n>, "stage": "<stage>", "outcome": "done\|clean\|rework\|blocked\|needs-human\|failed"}`; records every finished agent's metrics |
| `SessionEnd` | Records the orchestrator's (main thread's) metrics |

## Tunables

Every tunable is a config key with a code default; `sdlc.config.sample.json` shows most of
them, and `python3 "$SDLC" show-config` prints the effective values.

| Key | Default | Controls |
|---|---|---|
| `parallelism.devLane` / `.prReview` / `.designLane` | 1 / 1 / 1 | Lane caps; set a lane above 1 to fan it out |
| `parallelism.maxTasksPerRun` | 0 (unlimited) | Units driven to a terminal state per run |
| `docRoot` / `requirementsDir` / `tokenPath` | — | Doc tree, requirements docs (IRDs), PAT file |
| `pipeline.classification.*` | `{}` (sample: `type:initiative` / `type:epic` / `type:task` labels) | The only way an issue is an Initiative, Epic or Task |
| `pipeline.profiles` | `legacy`, `standing`, `"*"` | Epic behaviour by label: `driven`, `epicLevelPhase`, `childrenNeedArchitectedEpic`, `closes`, `gates.*` |
| `pipeline.labels.*` | `epic:standing` / `epic:legacy` / `epic:architected` | Epic labels |
| `pipeline.branches.*` / `pipeline.worktrees.*` | `issue-` / `epic-`; `/tmp/sdlc-dev-<n>` etc. | Branch and worktree naming |
| `pipeline.locks.*` | `{worktreesRoot}/.sdlc-locks`, 600 s | Per-branch `flock` |
| `pipeline.stack.*` | `enabled: false` | Per-epic isolated runtime stack |
| `pipeline.gates.skipConfidenceThreshold` / `.requiresHumanGateA` | 80 / `true` | Gate B skip bar; whether Gate A needs a human |
| `pipeline.productWip.maxGateAPending` | 5 | Open Gate A PRs allowed repo-wide |
| `pipeline.escalation.replaceAt` / `.needsHumanAt` | 3 / 6 | Bounces before a context-reset replacement / `needs-human` |
| `pipeline.retro.everyClosedIssues` / `.watermarkFile` | 5 / `{docRoot}/retro-watermark` | Retro trigger and watermark (driven repo) |
| `pipeline.continuous.cycleCap` | 8 | Merges per unattended run before pausing |
| `pipeline.models.<role>` / `pipeline.fanout.<role>` | `hooks/model_policy.json` | Model per stage; which reviews fan out, how wide, at which model |
| `pipeline.epicClose.auto` | `false` | Whether the orchestrator closes a verified epic itself |
| `pipeline.issueDefaults.priority` / `.effort` | `Medium` / `Medium` | Priority / Effort `create-issue` sets when no flag or lld line names one |
| `projectFields.issueTypeIds` | — | Native Issue Type ids; `create-issue` refuses a type missing here |
| `projectFields.priorityFieldId` / `.priorityOptionIds` / `.effortFieldId` / `.effortOptionIds` | unset | Optional Priority / Effort fields; when set, `create-issue` writes them |

## Metrics

The `SubagentStop` and `SessionEnd` hooks append one JSON line per finished agent to
`$CLAUDE_PLUGIN_DATA/metrics/<owner>__<repo>.jsonl` (never the driven repo's tree): per-model
tokens, estimated cost (prices in `hooks/_metrics.py`), duration, turns, and the
`SDLC-RESULT` issue/stage/outcome.

```bash
python3 scripts/sdlc_metrics.py report [--repo o/r] [--epic N] [--since YYYY-MM-DD] [--by role|model|issue|epic]
python3 scripts/sdlc_metrics.py backfill --projects-dir ~/.claude/projects/<project>   # history, idempotent
```

## How it runs

- **Initiative-driven**: `product` writes one IRD for the Initiative; after Gate A the
  orchestrator cuts independently shippable Epics from it.
- **Engineering-driven**: a bare Epic with its scope in the body starts at architecture.
- Each Epic's `architecture` and `lld` run as their own phase-Tasks, each gated
  `issue-<n>` → `main`. After a clean `lld-review`, `finish-lld` publishes `lld.md` and
  creates the Tasks. Every Epic also gets Integration-test and e2e-test standing Tasks.
- Tasks run `development` → `pr-review` → auto-merge into the epic branch. The epic closes
  after a full e2e and an exploratory pass, and an Initiative closes after a PM-style
  validation of its `product.md`.
- Issue tracking sits behind a `WorkItemProvider` interface, and GitHub is the only
  implementation. Code hosting is GitHub.

## Running the tests

```bash
(cd scripts && python3 -m pytest -q)   # control plane: offline, against the sample config
python3 -m pytest -q hooks/tests       # hooks: each runs as a subprocess with JSON on stdin
```

Contributors: see `CLAUDE.md`. This repo's `.claude/settings.json` runs the relevant suite on
`Stop` whenever `scripts/` or `hooks/` has uncommitted changes.
