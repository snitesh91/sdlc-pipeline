# Verification rules — prove a claim by running it, not by asserting it

Read by `architecture`, `lld`, `development`, `pr-review`, and `design-review` (both
halves — the completeness-sweep rule below governs both `architecture.md` and
`lld.md`). Split out of `references/stage-playbooks.md` on 2026-09-14 so `product`
and `product-review` (which do not verify code-level claims) are not told to read it.

## Compile-checking is not verification

A test runner's `--list`/dry-run mode, a type-check, and a lint pass tell you the code
*parses*. They cannot tell you a test **ran**, and a skipped test is indistinguishable
from a passing one in a `--list` output.

This is not hypothetical: new end-to-end tests have shipped with a locator that matched
the wrong element and yielded `NaN`, past a clean `--list` check, in a suite where they
skipped under the repo's default e2e invocation — they would have merged broken and
silently stayed broken; it was caught only by executing them for real (see
`references/history.md`).

So: **no stage may report a test as covering an acceptance criterion unless it observed
that test execute.** If it could not run, say so plainly, name the reason, and say
which criterion is therefore unproven — `pr-review` can then weigh a known gap instead
of trusting a coverage claim that was never true.

**A green build is not a build either — clear stale incremental state first.** An
incremental TypeScript build (`nest build`, `tsc -b`) with a stale, gitignored
`*.tsbuildinfo` on disk decides nothing changed, **emits nothing, and exits 0** — a
vacuous green. It happened twice in one epic (#323, in `development` and again in the
since-retired `testing` stage, both reporting a passing build that produced no
`dist/main.js`; see `references/history.md`, 2026-09-12). Standing step for
`development` and `pr-review`, whenever a
build is used as a verification gate: **before** the build, remove the stale cache
(`find <package> -name '*.tsbuildinfo' -delete`, or the repo's clean target) — **or**,
after it, assert the expected artifact exists and is newer than the sources
(`test -f dist/main.js && find dist -newer src -type f | head -1`). Exit 0 alone is
never evidence; state which of the two you did in your handoff.


## Establish a number by running the thing, not by modelling it

The dominant defect class of one whole epic — over a dozen confidently-stated, wrong
measurements across its children and its own architecture — had one shape every time:
**a number produced by a grep that models a rule, rather than by running the rule.**
Every blocking finding in that epic was found by building the change and running the
real tool; none was found by reading (see `references/history.md`). Three rules, each
from a real incident:

- **Run the rule, don't regex-model it.** A counting grep that carries an exemption the
  shipped lint config does not have produces a number that is true of the grep and
  false of the codebase; applied as designed it bans legitimate imports and ships a
  CI-red PR, and the obvious late fix invalidates whatever analysis rested on the
  count. The same applies to prose rules: a criterion whose meaning lives only in the
  design doc will, applied as written, exempt files it should not. **If the artifact is
  executable (a lint config, a script, a test, a build), apply the change in a scratch
  copy and run it. Report that output.**
- **Scope the search to the whole tree, not to the module you are thinking about.**
  The same composition file has been missed twice, by two siblings, for the identical
  reason: a grep scoped to the directory the agent was thinking about, in a repo whose
  composition file lives one directory over. Anchor the pattern to the *symbol*, not to
  a path prefix you assume — a pattern that matches the imported module name wherever
  it appears finds what a relative-path pattern cannot.
- **Prove the detector detects before reporting an absence.** A "0 cycles", "0
  violations", "no diff" result is worth exactly as much as the demonstration that the
  check *can* go non-zero. The move that works is the **positive control**: introduce
  one deliberate instance of the thing you are checking for, watch the count go
  non-zero, revert, watch it go back. A claim that several separate agents have each
  run that control on is trustworthy; one that none has is not.

**Vacuous-pass tells** — a check that passed because it examined nothing. All four have
been hit live:

- A **stale incremental-build cache** made the compiler emit a fraction of the tree and
  **exit 0**. Two different agents hit it. Clear build state and assert the emit count
  before using it.
- A graph built with **too narrow an edge pattern** gave the nodes under test zero
  outgoing edges and a vacuous "0 cycles" — indistinguishable from a real clean result.
  Assert non-vacuity (edge count, node count) before reading a graph result.
- **Structurally skipped tests.** A harness that never forwards the credentials a
  subset of tests needs skips that subset on *every run the repo has ever done*, and a
  before/after comparison "matches" over them. **A skip is not a cover** — count
  executed tests, not listed ones.
- **A mutation that discriminates nothing.** A mutation of a permission both roles
  already hold stays green, and the correct reading is *a bad discriminator, not a
  finding about the test*. If a mutation stays green, first ask whether it was a real
  mutation.

**Your own tooling gets the same scrutiny as any other claim.** Agents have caught
false positives in scripts they had just written, before treating the output as
evidence, and said so in the handoff. That is the bar.


## A completeness claim over a footprint is a sweep, not a list

This is the same rule as "establish a number by running the thing", applied to the
one place it bounces hardest: a child whose acceptance is a **class of surfaces** —
"every interactive control is ≥44px", "no fixed bar overlaps the nav", "every
on-screen file is audited for readability". One whole epic's audit/hardening children
each cost two predictable `lld-review` ↔ `lld` or `pr-review` ↔ `development` rounds
to the same failure, every time (see `references/history.md`): the doc asserted
completeness in prose — "§3 lists every file", "these two bars are all of them" — the
reviewer's completeness lens found one more instance, the author patched that named
instance, and the next round found the next one. A prose enumeration is a claim about
the author's attention, and the reviewer can only falsify it one instance at a time.

For any such child, at **every** stage that touches the class:

- **State completeness as a reproducible sweep, not an instance list.** The `lld`
  defines the class by a mechanical rule — a `grep`/`find` pattern anchored to the
  symbol or attribute, plus the stated partition of what the sweep covers and what is
  excluded and why. Paste the command and its output. "I looked at every file" is not
  a sweep; `comm -23 <sorted-find> <sorted-inventory>` returning empty is.
- **Cover every dimension the acceptance names.** If the criterion is "44×44px", a
  detector that models height only will pass a 44×10px control — a real bounce. Model
  each dimension the AC states as a separate term, and run a **positive control per
  dimension** (introduce one deliberate violation on that axis, watch the count go
  non-zero, revert). A guard that has only ever gone non-zero on one axis does not
  cover the other.
- **`development` applies the class rule per instance; it does not re-judge the
  class.** The `lld` states the rule once ("every `fixed bottom-0` bar this child adds
  or finds takes `bottom-14 md:bottom-0`"); `development`'s handoff reports that the
  rule was applied to each swept instance, not merely that the AC "passes". A per-file checklist
  that `development` works item by item is exactly where a missed-because-unlisted file
  ships looking identical to an audited-clean one — so the inventory the checklist is
  built from must be the sweep's output, not a hand-typed list.

**The narrow scoping of this section is itself a trap — the failure is not confined to
audit/hardening children.** Any criterion → test map is a completeness claim in prose,
and the positive control is what falsifies it; a child with an ordinary feature shape
fails the same way when several criteria share one assertion shape. On #494 — a feature
child, no class-of-surfaces acceptance anywhere in it — four criteria mapped to tests
whose only assertion was the reply's *type*, which every reply in that family shares.
Green suite, complete-looking map, no coverage; the `pr-review` positive control that
exposed it took one run. So read the rule above as scoped to *any* stage claiming a set
of criteria is covered, and see the family positive control in `sdlc-development.md`
("How to write the tests") for the cheap form: one control per family of sibling
expected values, not one per criterion.

**A third shape, one level earlier than a criterion or a test: an inventory of items
you are about to act on.** "Every priority query", "every endpoint that needs
migrating", "every file this touches" is the same completeness claim as the two
above, before any criterion or test exists to check it against — and it fails the
same way when the inventory is assembled by *reading the code* instead of by a
mechanical sweep. On #157's #504 (2026-09-14), a priority-query inventory built by
reading the codebase missed a real query in each of four straight `lld-review`
rounds — a different missing query each time, never the one already found. The
reviewer wrote "escalate on the pattern" at rounds 2 *and* 3; nothing escalated,
because that sentence was prose in a verdict, not a marker anything reads (see
`references/rework.md`, "Same-class recurrence must be a marker, not a sentence").
Build the inventory the same way as the class-population sweep above — a
`grep`/`find` command anchored to the real signal (a call site, a decorator, a query
builder pattern), pasted with its output — before treating it as the scope of what
you are about to fix.

The population of the class is a **requirements** fact, not a `development` call. If
which controls or which dimensions count is ambiguous ("interactive control" —
icon-only, or text buttons and pagination too?), that is pinned at `architecture`/`lld`
and escalated when unclear — never narrowed silently at `development`, which
`pr-review` will (correctly) bounce as an unauthorised scope reduction. A reviewer's
"same-class, second consecutive bounce" note is the signal to stop patching the next
named instance and close the class at its root with a sweep.

