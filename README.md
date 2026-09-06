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

3. **Import the skill into your agent harness.** For Claude Code, symlink it in:

   ```bash
   ln -s <path-to>/sdlc-pipeline <your-repo>/.claude/skills/sdlc-pipeline
   ```

   (Or vendor a copy into the repo — then the skill files themselves version with the
   repo; see "Config can move under you" in `SKILL.md`.)

4. **Per shell**, before running:

   ```bash
   export SDLC="<path-to>/sdlc-pipeline/scripts/sdlc_next.py"
   export GITHUB_TOKEN=$(cat <your-token-file>)   # classic PAT (ghp_)
   cd <your-repo>
   ```

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
