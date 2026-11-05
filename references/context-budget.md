# Orchestrator context budget

One Epic run reached about 461K tokens of orchestrator context (#4848, 2026-11-04). Most of it came from verbose updates, long handbacks, restated briefs, raw tool output and notification overhead. Each rule below targets one of those sources.

## Updates to the operator

- Routine events get one line each: claimed, handed off, transitioned, merged, synced.
- Write a full summary only at a gate, a question, a rework finding and Step 4.

## Briefs point; they do not copy

- Name the issue and, for a functional Task, the `lld-section` command. The agent reads the body, thread and design itself.
- Add only what the agent cannot read: operator answers given in session, prior-round findings, an exported prior-art file.
- Keep a brief under about 1,500 characters. A rework message lists the findings and what earlier rounds settled, nothing more.
- Never restate the code-graph rule: when the repo has a graph, SubagentStart adds it, with the note that the graph shows `main` and branch changes come from `git diff origin/<base>...HEAD`.

## Handbacks are capped

The SubagentStop hook refuses a stage handback over 1,500 characters once; the agent re-sends it terse (`stage-playbooks.md`, "The handback is terse"). `exploratory`, `initiative-close` and a `"fanout"` hand-back are exempt.

## Never read raw output

- A failing check: `python3 "$SDLC" ci-failure-summary <pr>` returns each failing job's failure lines only (at most 25 per job, 4,000 characters in total). Never `gh run view --log` into your context.
- `close-epic --brief` reduces the evidence, file and cleanup lists to counts and names. Drop `--brief` only to brief an exploratory re-pass that needs the file list.
- A reference file: `grep -n` the heading you need, then read that section only.

## Side requests go to a subagent

An operator request outside the pipeline stages (a diagnosis, a tool or mod fix, a lookup over several files) goes to one `general-purpose` subagent. Its brief states:

- the goal, and the files or commands to start from;
- the constraints: no pipeline mutations, no edits in the main checkout;
- the reply shape: cause, change made, verification, open risks, under about 200 words.

Keep only that reply. Do it inline only for one or two tool calls whose target you already know.

## Fewer events

Each background notification costs about 300 tokens of fixed harness text.

- Launch a batch of agents in one message.
- Wait on one `Monitor` until-loop or one background command, never on stacked short sleep timers. Poll external state (CI) at the interval it changes, not every minute.
- A stage agent sends `SDLC-RESULT` once, when its stage ends. An interim "still waiting" result is a defect: tell the agent to wait on one Monitor and finish.

## Stop at a phase boundary

Once this run has driven any unit, stop at the next phase boundary and report (Step 4):

- after a `finish-lld` that cut the Epic's Tasks;
- before the first `close-epic` call on a closeable Epic.

The next `/sdlc:run <epic>` starts from GitHub with a clean context. A cloud run ends with `end-cloud-session <epic> --outcome context-reset`; that session is relaunch-ready at once (`cloud-status`, `next-action <initiative>`'s `cloud.relaunch`). Keep `parallelism.maxTasksPerRun` at about 5 so a long development phase also breaks into batches.

## After a container restart

A restart can drop a background agent's completion notification, and the agent can then not be messaged. Before you brief a replacement, read the agent's final text from its transcript (`<session dir>/subagents/agent-<id>.jsonl`, the last assistant message; `tail` it, never read it whole). Act on its `SDLC-RESULT` if it has one. Otherwise brief a replacement per `rework.md`, "Replacing an agent that cannot be resumed".
