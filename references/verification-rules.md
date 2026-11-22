# Verification rules — prove a claim by running it

`lld` reads it in full. Other roles read a section only where their file cites it. Which suites
run where is in `references/test-scope.md`.

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

## Local stack only

- Every check during Initiative and Epic development runs on the local stack: Task verification,
  the exploratory pass, `## Epic-close checks`, `initiative-close`. Use the repo's local stack,
  integration tests, local e2e and provider test doubles.
- No Task criterion, Epic-close check or close gate names Stage, a store or a real device.
- **A check that needs a Stage deploy, a store release or a real device is a release Task.** File
  it under the product's standing backlog Epic (`create-issue --parent <standing epic> --type
  Task`); with many, one release Epic holds them. It never blocks an Epic's or Initiative's close.

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
