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
.github/workflows/tests.yml      this repo's CI: both pytest suites on every PR and on main
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
5. **Token**: point the config's `tokenPath` at a file holding a classic PAT (`ghp_`), or name an
   env var in `tokenEnv`; fine-grained PATs cannot read check-runs or write the custom fields.
   The SessionStart hook exports the `tokenEnv` var, else the `tokenPath` file, as
   `GITHUB_TOKEN`, overriding any ambient one, and warns when it is not a `ghp_` token.
6. Optional: a repo-specific `<docRoot>/<pipeline.docTemplates>/{product,architecture}.template.md`
   (default `_templates`) overrides the plugin's templates.
7. Start a run with `sdlc-run <initiative-or-epic-number> [claude args]`: it launches `claude`
   on the policy's orchestrator model with `/sdlc:run <n>` (a skill's `model:` does not
   outlast its turn).

**Upgrading**: bump the `ref` in `.claude/settings.json` and `SDLC_PIPELINE_REF` together,
run `claude plugin marketplace update sdlc-pipeline`, and start a new session. Bump only
when no run is live. There is one plugin version per driven repo; `show-config` → `plugin`
reports the running one (manifest version, plus the HEAD sha of a git checkout).

Repo-specific commands (lint, build, test, suite names, ports) belong in the driven repo's
`CLAUDE.md` / `AGENTS.md`, which every stage agent reads.

## Hooks

Every hook is a no-op unless the session's git toplevel holds `sdlc-pipeline.config.json`.
They are stdlib Python, and they fail open on internal errors.

| Hook | Enforces |
|---|---|
| `SessionStart` | Exports `$SDLC` and `GITHUB_TOKEN` (from `tokenEnv` / `tokenPath`) for every Bash call; warns when no token is available; suggests `rtk init` if `rtk` is absent; flags a session model off the policy's orchestrator model; after a compaction, restates this session's run (epic, run-id, units in flight) |
| `PreToolUse` (Bash) | Denies hand-run `gh api graphql`, `gh issue create/edit/close/reopen`, `gh pr create/merge/ready/close`, mutating `gh api`, `git worktree add` (except `--detach`), force-push and rebase, naming the `python3 "$SDLC"` command to use instead; limits each `sdlc:<role>` agent to its role's control-plane commands (`post-comment` only with its own `--role`) and review roles to no git writes. Only each segment's leading words count. Stage agents are always guarded; the main thread only while its session drives a run (a run-state file written in the last 8 h, stamped by `next-action --run-id`), so hand-run backlog or issue cleanup outside a run is allowed. `guard.mainThread: "always"` guards every main-thread session |
| `PreToolUse` (Agent) | Sets every `sdlc:*` agent's `model` from `hooks/model_policy.json` (overridable per role by `pipeline.models` / `pipeline.fanout`), whether the main thread or a continuous-mode cycle agent launches it; denies nested stages and fan-out a role may not do or has exhausted; lets the policy's `explore` roles (`development`, `lld`) launch a read-only `Explore` search |
| `SubagentStart` | Gives each `sdlc:*` agent `$SDLC`, `docRoot`, `requirementsDir`, `docTemplates`, the references path and the `SDLC-RESULT` format |
| `SubagentStop` | An `sdlc:*` agent cannot stop until its final message ends with `SDLC-RESULT: {"issue": <n>, "stage": "<stage>", "outcome": "done\|clean\|rework\|blocked\|needs-human\|failed"}` (optional `"next"`/`"why"`: a standing child's recommended next stage); and, for `product`/`architecture`/`lld` finishing `done`, until its handoff went out via `post-comment`; records every finished agent's metrics |
| `SessionEnd` | Records the orchestrator's (main thread's) metrics |

## Tunables

Every tunable is a config key with a code default; `sdlc.config.sample.json` shows most of
them, and `python3 "$SDLC" show-config` prints the effective values.

| Key | Default | Controls |
|---|---|---|
| `parallelism.devLane` / `.prReview` / `.designLane` | 1 / 1 / 1 | Lane caps; set a lane above 1 to fan it out |
| `parallelism.maxTasksPerRun` | 0 (unlimited) | Units driven to a terminal state per run |
| `repo` / `docRoot` / `requirementsDir` / `tokenPath` / `humanAssignee` | — (required) | `owner/name`, doc tree, requirements docs (IRDs), PAT file, the operator login `check-epics-closeable` assigns |
| `pipeline.docTemplates` | `_templates` | Directory under `docRoot` whose `product.template.md` / `architecture.template.md` override the plugin's |
| `guard.mainThread` | `run-live` | `run-live`: the Bash guard denies the main thread's hand-run mutations only while its session drives a run; `always`: in every session |
| `tokenEnv` | unset | Env var holding the PAT; wins over `tokenPath` |
| `pipeline.classification.*` | `{}` (sample: `type:initiative` / `type:epic` / `type:task` labels) | The only way an issue is an Initiative, Epic or Task |
| `pipeline.profiles` | `legacy`, `standing`, `"*"` | Epic behaviour by label: `driven`, `epicLevelPhase`, `childrenNeedArchitectedEpic`, `closes`, `gates.*` |
| `pipeline.labels.*` | `epic:standing` / `epic:legacy` / `epic:architected` | Epic labels |
| `pipeline.branches.*` / `pipeline.worktrees.*` | `issue-` / `epic-`; `/tmp/sdlc-dev-<n>` etc. | Branch and worktree naming |
| `pipeline.locks.*` | `{worktreesRoot}/.sdlc-locks`, 600 s | Per-branch `flock` |
| `pipeline.stack.*` | `enabled: false` | Per-epic isolated runtime stack |
| `pipeline.gates.skipConfidenceThreshold` / `.requiresHumanGateA` / `.requiresHumanGateB` | 80 / `true` / `true` (`standing` profile: both `false`) | Gate B skip bar; whether Gate A / Gate B needs a human (else `waive-gate`) |
| `pipeline.productWip.maxGateAPending` | 5 | Open Gate A PRs allowed repo-wide |
| `pipeline.escalation.replaceAt` / `.needsHumanAt` | 3 / 6 | Bounces before a context-reset replacement / `needs-human` |
| `pipeline.continuous.cycleCap` | 8 | Merges per unattended run before pausing |
| `pipeline.resume.liveWindowMinutes` | 30 | A `next-action` resume claimed sooner than this is flagged `likely_live` (another session may be driving it) |
| `pipeline.models.<role>` / `pipeline.fanout.<role>` | `hooks/model_policy.json` | Model per stage; which reviews fan out, how wide, at which model |
| `pipeline.epicClose.auto` | `false` | Whether the orchestrator closes a verified epic itself |
| `pipeline.issueDefaults.priority` / `.effort` | `Medium` / `Medium` | Priority / Effort `create-issue` sets when no flag or lld line names one |
| `projectFields.issueTypeIds` | — | Native Issue Type ids; `create-issue` refuses a type missing here |
| `projectFields.priorityFieldId` / `.priorityOptionIds` / `.effortFieldId` / `.effortOptionIds` | unset | Optional Priority / Effort fields; when set, `create-issue` writes them |

## Metrics

The `SubagentStop` and `SessionEnd` hooks append one JSON line per finished agent to
`$CLAUDE_PLUGIN_DATA/metrics/<owner>__<repo>.jsonl` (never the driven repo's tree; `sdlc_metrics.py`,
run from Bash where that variable is unset, derives the same `<plugin>-<marketplace>` directory): per-model
tokens, estimated cost (prices in `hooks/_metrics.py`), duration, turns, tool calls, peak
context (the largest single request), the `SDLC-RESULT` issue/stage/outcome, and the
control-plane run id (`next-action --run-id`) when the session drives one. `--by route`
groups each issue under the stages its agents ran (e.g. `architecture>development>pr-review`),
so `report --epic <standing epic> --by route` compares routed flows' rework rate with full ones.

```bash
python3 scripts/sdlc_metrics.py report [--repo o/r] [--epic N] [--run ID] [--since YYYY-MM-DD] [--by role|model|issue|epic|route]
python3 scripts/sdlc_metrics.py backfill --projects-dir ~/.claude/projects/<project>   # history, idempotent
```

## How it runs

- **Initiative-driven**: `product` writes one IRD for the Initiative; after Gate A the
  orchestrator cuts independently shippable Epics from it, ordered by native `blockedBy` edges
  (`create-issue --blocked-by`). `/sdlc:run <initiative>` then loops: `next-action` returns
  `cut-phase-tasks` for the next runnable Epic that has none and `run-epic` to drive it, one Epic
  at a time under one run id and one `maxTasksPerRun` cap (`--skip-epic` parks a stalled one).
  `merge-gate --operator-confirmed` merges a gate PR, only when the operator says to.
- **Engineering-driven**: a bare Epic with its scope in the body starts at architecture.
- Each non-standing Epic owns a branch `epic-<n>`, created eagerly, and **every child branches
  from it**. Its `architecture` and `lld` run as their own phase-Tasks that author
  `docs/sdlc/epic-<n>/architecture.md` / `lld.md` directly; a design PR `issue-<n>` →
  `epic-<n>` carries each through review. The pipeline merges it on a clean review
  (`lld-review` always; `arch-review` above the skip threshold, else the human at Gate B);
  `finish-lld` then creates the Tasks. Every Epic also gets Integration-test and e2e-test
  standing Tasks. Product-Roadmap Tasks and standing children still gate into `main`.
- Tasks run `development` → `pr-review` → auto-merge into the epic branch. The epic closes
  after an exploratory pass (the full e2e is owned by the epic's e2e-test Task), and an Initiative closes after a PM-style
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
