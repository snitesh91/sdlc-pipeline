# sdlc-pipeline

An agent-driven SDLC pipeline over GitHub Issues. One invocation drives actionable
work — an epic through `product` → `architecture`, then each child through
`lld` → `development` → `testing` → `pr-review` → merge — with the design and
implementation stages fanning out into bounded, worktree-isolated parallel pools.
Eligibility is computed mechanically from native `blockedBy` edges and declared
footprints, never hand-tracked.

The skill is **generic**: it carries no repo-, org-, or product-specific values.
Everything project-specific lives in one config file **in the repo you drive**.

## Layout

```
SKILL.md                     the orchestration contract (read first)
references/*.md              on-demand detail (gates, parallelism, stage playbooks, epics, ops, history)
scripts/sdlc_next.py        the deterministic control plane (all gh/GraphQL/git lives here)
scripts/tests/              pytest suite (runs offline against the sample config)
agents/sdlc-*.md            the eight stage-agent definitions — copy into <repo>/.claude/agents/
workflows/gate-auto-advance.yml  real-time gate backstop — copy into <repo>/.github/workflows/
templates/*.template.md     product/architecture doc skeletons — copy into <docRoot>/_templates/
sdlc.config.sample.json     template config — copy into your repo and fill in
```

## Setup

1. **Copy the config into your repo** and fill in every value:

   ```bash
   cp sdlc.config.sample.json <your-repo>/sdlc-pipeline.config.json
   ```

   The control plane finds it by walking up from the working directory (repo root,
   or under `.config/` / `.claude/`), or via `$SDLC_CONFIG`. Nothing is baked into
   the skill, so a missing config is a loud error, never a silent run on placeholders.

2. **Look up your GitHub Projects-v2 schema ids** (the `projectFields` block). These
   are per-repo/-org ids for your provisioned issue types and single-select fields.
   Introspect them once, e.g.:

   ```bash
   gh api graphql -f query='query { repository(owner:"OWNER", name:"REPO") {
     issueTypes(first:20){nodes{id name}} } }'
   # and the org/project field + option ids via the projectV2 / field queries
   ```

3. **Import the skill into your repo.** Recommended layout: a git submodule (so CI
   and every clone get the same pinned version) plus a *repo-relative* symlink for the
   agent harness, so the link also resolves inside the worktrees the pipeline creates:

   ```bash
   cd <your-repo>
   git submodule add https://github.com/<skill-owner>/sdlc-pipeline.git .github/sdlc-pipeline
   ln -s ../../.github/sdlc-pipeline .claude/skills/sdlc-pipeline
   git add .gitmodules .github/sdlc-pipeline .claude/skills/sdlc-pipeline
   ```

   Commit the symlink (do not gitignore it) — an untracked or absolute link breaks
   in `/tmp/sdlc-dev-<n>`-style worktrees. The shipped workflow assumes the
   `.github/sdlc-pipeline` submodule path. Stage agents never hard-code a skill path:
   they read `$SDLC_DIR/...`, and the orchestrator states `$SDLC_DIR` (the absolute
   skill path) in every agent prompt; an agent that does not receive it stops and asks.

4. **Per shell**, before running:

   ```bash
   export SDLC_DIR="<path-to>/sdlc-pipeline"
   export SDLC="$SDLC_DIR/scripts/sdlc_next.py"
   export GITHUB_TOKEN=$(cat <your-token-file>)   # classic PAT (ghp_)
   cd <your-repo>
   ```

## Tunables — the `pipeline` config block

Everything that used to be a constant in the script or a number in the prose is a key
under `pipeline` in the config, each with a default (see `sdlc.config.sample.json`).
`python3 "$SDLC" show-config` prints the effective values.

| Key | Default | Controls |
|---|---|---|
| `parallelism.devLane` / `.prReview` | 3 / 3 | Dev-lane and review-pool caps (top-level, required) |
| `pipeline.labels.*` | `epic:standing` / `epic:legacy` / `epic:architected` | The three epic labels |
| `pipeline.branches.issuePrefix` / `.epicPrefix` | `issue-` / `epic-` | Branch naming; also how gate PRs are recognised |
| `pipeline.worktrees.*` | `/tmp`, `sdlc-dev-`, `sdlc-epic-`, `sdlc-review-`, `sdlc-tmp-` | Where the orchestrator puts worktrees; `ephemeralPrefix` names the throwaway worktree a branch-writing command creates when nothing holds its branch |
| `pipeline.locks.dir` / `.waitSeconds` | `{worktreesRoot}/.sdlc-locks` / 600 | Per-branch `flock` every branch-writing command takes (`SDLC_LOCK_DIR` env overrides the dir) |
| `pipeline.skill.submodulePath` / `.probeFile` | `.github/sdlc-pipeline` / `SKILL.md` | Where the driven repo vendors this skill; `worktree-add`/`sync-branch` init it per worktree so each unit's agents read their own branch's pinned copy (empty path disables) |
| `pipeline.stack.*` | `enabled: false`, dev-profile ports 3000/3001/5432/9229, stride 20 | Per-epic isolated runtime stack for `provision-epic-stack`/`teardown-epic-stack`: base profile, env/secrets file templates, compose project template, port keys, data-dir key, up/seed/down commands (`references/parallelism.md`, "Per-epic isolated stack") |
| `pipeline.gates.skipConfidenceThreshold` | 95 | `arch-review` confidence needed to skip Gate B |
| `pipeline.escalation.replaceAt` / `.needsHumanAt` | 3 / 6 | Bounce counts for the context-reset replacement and `needs-human` |
| `pipeline.retro.everyClosedIssues` / `.watermarkFile` | 5 / `{docRoot}/retro-watermark` (`{docRoot}` resolves to the config's `docRoot`, e.g. `docs/sdlc/retro-watermark`) | Retro trigger and watermark location (relative to the driven repo) |
| `pipeline.continuous.cycleCap` | 8 | Merges per unattended run before pausing for the operator |
| `pipeline.models.<role>` | opus/sonnet per `SKILL.md` table | Model tier passed to each stage's `Agent` call |
| `pipeline.docTemplates` | `_templates` | Template folder under `docRoot` |

Repo-specific *commands* (lint, test, e2e) are not config — they belong in the
`sdlc-*` agent definitions and the repo's own `CLAUDE.md`, which every stage agent
already reads.

## Installing the shipped pieces

`SKILL.md` assumes these exist in the repo you drive. The first three ship here —
copy them in, then adapt the agents' repo-specific commands (lint, test, e2e) and
the `<placeholder>` values to your repo:

```bash
cp -r agents/* <repo>/.claude/agents/                        # the eight sdlc-* stage agents
cp workflows/gate-auto-advance.yml <repo>/.github/workflows/  # needs secret SDLC_GH_TOKEN (classic PAT)
mkdir -p <repo>/<docRoot>/_templates && cp templates/*.template.md <repo>/<docRoot>/_templates/
```

- **Agent definitions** (`agents/`): `sdlc-product`, `sdlc-architecture`,
  `sdlc-design-review`, `sdlc-lld`, `sdlc-development`, `sdlc-testing`,
  `sdlc-pr-review`, `sdlc-exploratory`. Each carries persona, procedure, refusal
  criteria and `tools:` only; pipeline rules stay in `references/stage-playbooks.md`.
  They reference the skill only as `$SDLC_DIR/...` (see Setup step 3).
- **`workflows/gate-auto-advance.yml`** calling `auto-pass-gate`, `mark-todo`,
  `mark-issue-closed` (real-time gate backstop; `next-action` works without it, just
  later). Assumes the skill is the `.github/sdlc-pipeline` submodule.
- **Doc templates** (`templates/`) → `<docRoot>/_templates/{product,architecture}.template.md`.

Still external — not in this repository:

- **The `superpowers` plugin** (the `development` agent invokes three of its skills).
- **GitHub Projects v2 custom issue fields** `Stage`, `Pipeline Status`, `Priority`,
  `Effort` with the option names the sample config lists, plus issue types
  Task/Bug/Feature.

`references/stage-playbooks.md` and `references/parallelism.md` still quote the
origin repo's own commands and limits (`make lint`, `npm run test:it`, `make e2e`,
Docker memory) as worked examples; substitute your repo's equivalents.

## Running the tests

```bash
python3 -m pytest scripts/tests/ -q
```

The suite points itself at `sdlc.config.sample.json` (via `conftest.py`) and makes no
network calls — every `gh`/`git` call is a scripted test double.

## Harness coupling

The control plane (`scripts/sdlc_next.py`) and config are harness-agnostic. The
orchestration layer in `SKILL.md` assumes an agent runtime that can dispatch
subagents by type and pick a model tier per call (documented against Claude Code:
`.claude/agents/` definitions, `Agent`/`SendMessage` tools). Port that layer to your
runtime; the control plane and config do not change.
