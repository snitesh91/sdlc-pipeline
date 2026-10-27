# Verification rules — prove a claim by running it

Read by `architecture`, `lld`, `development`, `pr-review` and `design-review`, and by the
orchestrator for "Test scope".

## Compile-checking is not verification

- A `--list` or dry-run, a type-check and a lint pass show only that code parses. In
  `--list` output a skipped test looks like a passing one.
- Never report a test as covering a criterion unless you saw it execute. If it could not
  run, say why and name the criterion still unproven.
- **A build's exit 0 is not evidence.** An incremental build with a stale `*.tsbuildinfo`
  can emit nothing and exit 0. When a build is a gate, do one of these and say which:
  - delete the stale cache before the build (`find <package> -name '*.tsbuildinfo' -delete`);
  - assert the artifact is newer than the sources after it
    (`test -f dist/main.js && find dist -newer src -type f | head -1`).
- **Integration tests run against compiled `dist`.** When the diff changes a DTO, a route or
  anything OpenAPI-visible, run `rm -rf dist *.tsbuildinfo` and rebuild first.

## Test scope — which suites run where

The one home of this rule. The orchestrator reads it before a `development` or `pr-review`
brief.

- **The full integration suite runs as CI on the PR that lands work on `main`** (or on
  `initiative-<i>`, then `main`, under an initiative branch), and on any PR into those bases
  that a required workflow covers. The pipeline waits for that check (`merge-pr`,
  `close-epic`'s `awaiting_checks`). No stage runs it locally. Exception: a suite whose
  workflow does not run on that PR is listed in `close-epic`'s `unattested_suites`; run it
  once on the PR head and `record-local-ci` it.
- **The config must say so.** Each integration suite's `requiredWorkflows` entry carries
  `bases: ["main", "initiative-*"]` and `attestable: false`. Without `bases`, each Task PR into
  `epic-<n>` seems to owe the suite (`transition` holds, `suites.needed` asks): fix the config,
  never run the suite. Without `attestable: false`, wait for the PR's check instead.
- **A Task into `epic-<n>` runs no integration suite.** `development` runs unit tests for its
  change and the configured pre-PR command, with no integration option.
- **Two exceptions. Each runs only the specs concerned, never the whole suite (unless the
  repo's `AGENTS.md` requires it, below):**
  1. The brief says `Integration tests: mandatory`. The orchestrator adds this line for a
     migration, a raw query, a cross-module change, or a new or changed external-API
     integration, when no required workflow runs the suite on the PR. Run the specs the change
     impacts. It also adds it when the Task's footprint changes a cross-cutting rule (auth
     guard, token/session lifetime, rate limit, clock, DB layer, shared test setup). The repo's
     `AGENTS.md` may list its own such paths and require the full suite for them; follow it.
  2. The unit adds or changes integration specs (the standing Integration-test Task, a Task
     under `testTasks: false`, a standing child). Run those specs and the specs the change impacts.
- **Standing test Tasks** run only the specs they add or change; the e2e-test Task runs them
  against a stack built from `epic-<n>`. With no spec to add, run the existing specs the section
  names. The pipeline never runs the full e2e suite.
- **Never attest a scoped run under a whole-suite key.** Put it in the handoff's Commands
  table. Exception: `e2e`, whose attestation is the e2e-test Task's own specs.
- **`pr-review`** runs targeted specs only to confirm a finding. It never re-runs a suite a
  required check covers. It never bounces a Task for integration or e2e evidence this section
  does not owe.
- **A reconcile or sync** runs no suite. When the resolution changed code, run the specs that
  code touches.
- **After a failure, re-run only the failing specs.** One clean pass is the evidence. Run
  stability re-runs only when the design names them (the e2e-test Task's section does).
- **Any run:** run a truncating suite only against a database whose name ends in `_test`.
  Rebuild `dist` when the diff changes compiled code.

## Establish a number by running the thing

- **Run the rule; do not model it with a regex.** Apply the change in a scratch copy, run the
  real lint, script, test or build, and report that output.
- **Search the whole tree.** Anchor the pattern to the symbol, not to a path you assume.
- **Prove the detector detects before you report an absence** ("0 cycles", "no diff"). Add
  one deliberate instance, see the count go non-zero, revert, see it return.

Vacuous-pass tells:

- A stale build cache emits part of the tree. Clear it and assert the emit count.
- A pattern too narrow gives zero edges and "0 cycles". Assert the edge and node counts.
- A harness that drops credentials skips tests on every run. Count executed tests, not listed.
- A mutation that changes nothing stays green. Check the mutation was real first.

**Never `git stash`.** All worktrees share one stash stack. Mutate a scratch copy
(`cp f .sdlc-scratch/f.orig`, edit, run, copy back), or commit and `git revert`.

Check a script you wrote for false positives before you use its output. Say so in the handoff.

## A completeness claim over a footprint is a sweep, not a list

This applies to a class-of-surfaces criterion ("every interactive control is ≥44px"), a
criterion → test map, and an inventory you will act on ("every endpoint to migrate").

- **State completeness as a reproducible sweep.** Define the class with a `grep`/`find`
  anchored to the real signal (symbol, attribute, call site, decorator). State what it
  includes and excludes. Paste the command and its output (`comm -23 <find> <inventory>`
  returning empty).
- Build every inventory with a sweep, never by reading code.
- Cover each dimension the criterion names ("44×44px" is height and width), each with its own
  positive control.
- Criteria that share one assertion shape need one positive control per family.
- `lld` states the class rule once. `development` applies it to each swept instance and
  reports per instance.
- The class population is a requirements fact. Pin it at `architecture`/`lld`. Escalate when
  unclear. Never narrow it at `development`.
- After a second consecutive same-class bounce, close the class at its root with a sweep.
  Reviewers record the repeat with `--same-class-recurrence`.
