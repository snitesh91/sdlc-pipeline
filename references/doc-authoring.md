# Doc authoring: rules for `product`, `architecture` and `lld`

Read this file after `references/stage-playbooks.md`. It binds the three stages that commit a design doc.

## Style

Write every doc and handoff in STE-lite (`references/stage-playbooks.md`, "Style"). Write for the reader the doc's rules name (`references/design-doc-rules.md`).

## Per-issue docs

A design stage commits its doc on branch `issue-<n>` at one path. An Epic's architecture or lld phase-Task writes `<docRoot>/epic-<e>/<doc>`. Every other issue writes `<docRoot>/issue-<n>/<doc>`. Your prompt names the path.

| File | Written by |
|---|---|
| `product.md` | `product`: an Initiative's Product-Roadmap Task or a standing child. Never an Epic's Task, except an approved amendment (`references/initiatives.md`, "Amending an approved `product.md`") |
| `architecture.md` | `architecture`: an Epic's Architecture-phase or revision Task, or a standing child |
| `lld.md` | `lld`: an Epic's LLD-phase Task. One `## Task` section per Task |

- No other file goes in your doc directory. Propose a fourth doc in your handoff; do not create it.
- Write each doc so the next stage works from it alone.
- Edit a doc only with `Edit`/`Write`. Never edit a doc through `Bash` (`sed`, `python`, heredoc).
- Each `lld.md` Task section and a standing child's `architecture.md` carry a `## Footprint` in the shape from `references/epics.md`, "How to size the Tasks".
- If you change pipeline files the repo tracks (its config, the gate workflow), commit them with the related doc.

## Citation discipline

- Anchor a citation to a heading, an exact test title or a grep-able quote. A line number is only a hint. In a shipped doc, use the quote alone.
- Cite only files you opened in this session.
- Before you resolve a doc from a branch, run `git fetch origin`. State the ref you used.
- See a branch's changes with `git diff origin/<base>...<branch>` (three dots). `<base>` is `epic-<e>` for a child of a non-standing Epic, else `main`.
- A stale citation in a doc about to merge is a real finding.
- After `sync-branch` brings in a sibling's files that your doc cites, re-check those citations.
- Run "Attribution is falsifiable" (`references/stage-playbooks.md`) before every handoff.

## Commenting discipline

Comments are the visibility log and resume point. Docs and commits are the record. Post one comment per milestone, at least the handoff.

### Posting a handoff comment

Post with `python3 "$SDLC" post-comment <n> --role <role> --body-file <file>`. Write a new body file each round (`.sdlc-scratch/comment-round-<k>.md`). Post every round; the SubagentStop hook requires it on `done`. Keep it within the stage handoff cap (`references/stage-playbooks.md`, "Comment size is a contract").
