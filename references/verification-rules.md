# Verification rules — prove a claim by running it, not by asserting it

Read by `architecture`, `lld`, `development`, `pr-review`, and `design-review` (both
halves; the completeness-sweep rule governs both `architecture.md` and `lld.md`).

## Compile-checking is not verification

- A test runner's `--list`/dry-run, a type-check, and a lint pass show the code parses.
  They do not show a test ran, and in `--list` output a skipped test looks the same as a
  passing one.
- **Never report a test as covering an acceptance criterion unless you saw it execute.**
  If it could not run, say so, give the reason, and name the criterion that is still
  unproven.
- **A build's exit 0 is never evidence on its own.** An incremental build (`nest build`,
  `tsc -b`) with a stale, gitignored `*.tsbuildinfo` can emit nothing and still exit 0.
  Whenever a build is a verification gate (`development`, `pr-review`), do one of these
  and say which in your handoff:
  - before the build, delete the stale cache (`find <package> -name '*.tsbuildinfo' -delete`,
    or the repo's clean target); or
  - after the build, assert the expected artifact exists and is newer than the sources
    (`test -f dist/main.js && find dist -newer src -type f | head -1`).

## Establish a number by running the thing, not by modelling it

- **Run the rule; don't regex-model it.** A grep that imitates a rule measures the grep,
  not the codebase. If the artifact is executable (a lint config, a script, a test, a
  build), apply the change in a scratch copy, run it, and report that output. The same
  applies to a prose criterion defined only in a design doc: apply it to the real tree.
- **Search the whole tree, not the module you have in mind.** Anchor the pattern to the
  symbol (e.g. the imported module name wherever it appears), not to a path prefix you
  assume. Composition files often live one directory over.
- **Prove the detector detects before you report an absence** ("0 cycles",
  "0 violations", "no diff"). Use a **positive control**: add one deliberate instance of
  what you are checking for, watch the count go non-zero, revert it, and watch it go
  back.

**Vacuous-pass tells** (the check passed because it examined nothing):

- **Stale incremental-build cache:** the compiler emits part of the tree and exits 0.
  Clear build state and assert the emit count.
- **Edge pattern too narrow:** the nodes under test get zero edges and a vacuous
  "0 cycles". Assert non-vacuity (edge count, node count) before reading a graph result.
- **Structurally skipped tests:** a harness that never forwards needed credentials
  skips that subset on every run, so a before/after comparison "matches". A skip is not
  coverage: count executed tests, not listed ones.
- **A mutation that discriminates nothing** (e.g. mutating a permission both roles
  already hold) stays green. That means a bad discriminator, not a finding about the
  test. When a mutation stays green, first check it was a real mutation.

Give your own tooling the same scrutiny. Check a script you just wrote for false
positives before using its output as evidence, and say you did so in the handoff.

## A completeness claim over a footprint is a sweep, not a list

This applies whenever a stage claims a set is complete:

- a class-of-surfaces acceptance ("every interactive control is ≥44px", "no fixed bar
  overlaps the nav");
- a criterion → test map;
- an inventory of items you are about to act on ("every endpoint that needs migrating",
  "every file this touches").

A prose list can only be disproved one missing instance per review round, so every stage
that touches the class does the following:

- **State completeness as a reproducible sweep, not an instance list.** Define the class
  with a mechanical `grep`/`find` pattern anchored to the real signal (a symbol,
  attribute, call site, decorator, or query-builder pattern). State which parts the sweep
  covers, which it excludes, and why. Paste the command and its output. "I looked at
  every file" is not a sweep; `comm -23 <sorted-find> <sorted-inventory>` returning empty
  is.
- **Build every inventory with such a sweep** before treating it as the scope of your
  work. Never build it by reading the code.
- **Cover every dimension the acceptance names.** "44×44px" means height and width as
  separate terms, each with its own positive control. A guard that has only gone non-zero
  on one axis does not cover the other.
- **Criterion → test maps:** criteria that share one assertion shape (e.g. only the
  reply's type) are not covered by that assertion. Run one positive control per family of
  sibling expected values (`agents/development.md`, "How to write the tests").
- **`development` applies the class rule to each instance; it does not re-judge the
  class.** `lld` states the rule once (e.g. "every `fixed bottom-0` bar this child adds or
  finds takes `bottom-14 md:bottom-0`"). `development` reports that it applied the rule to
  each swept instance, not just that the AC "passes". Any per-file checklist is built from
  the sweep's output, never typed by hand.
- **The class population is a requirements fact.** Pin it at `architecture`/`lld`, and
  escalate when it is unclear (e.g. does "interactive control" mean icon-only, or text
  buttons and pagination too?). Never narrow it silently at `development`: `pr-review`
  bounces that as an unauthorised scope reduction.
- **After a second consecutive same-class bounce, stop patching instances** and close
  the class at its root with a sweep. Reviewers record the recurrence with
  `--same-class-recurrence` on `record-design-review` / `record-pr-review`, not in
  verdict prose.
