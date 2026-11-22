# Test scope — which suites run where

The one home of this rule. `development`, `pr-review` and `design-review` read it before a suite
run or a test-scope judgement; the orchestrator reads it before a `development` or `pr-review` brief.

- **The full integration suite runs as CI on the PR that lands work on `main`** (or on
  `initiative-<i>`, then `main`, under an initiative branch), and on any PR into those bases
  that a required workflow covers. The pipeline waits for that check (`merge-pr`,
  `close-epic`'s `awaiting_checks`). No stage runs it locally. Exception: a suite whose
  workflow does not run on that PR is listed in `close-epic`'s `unattested_suites`; run it
  once on the PR head and `record-local-ci` it. In a cloud session, a PR marked
  `<!-- sdlc:ci-local -->` runs its suites in session instead (`references/cloud-mode.md`,
  "CI in a cloud run").
- **The config must say so.** Each integration suite's `requiredWorkflows` entry carries
  `bases: ["main", "initiative-*"]` and `attestable: false`. Without `bases`, each Task PR into
  `epic-<n>` seems to owe the suite (`transition` holds, `suites.needed` asks): fix the config,
  never run the suite. Without `attestable: false`, wait for the PR's check instead.
- **A Task into `epic-<n>` runs no integration suite.** `development` runs unit tests for its
  changed files only, and the configured pre-PR command once, on the final round, scoped to
  `origin/epic-<n>`, with no integration option. The full suites run once, at epic close.
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
