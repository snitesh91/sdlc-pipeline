# Cloud mode — one unit, one side

`/sdlc:run <n>` runs on the operator's laptop or in a Claude Code cloud session
(claude.ai/code). The two never drive the same unit: the placement label decides which side
owns it, and the operator moves it between them. Nothing on the laptop watches a cloud run.

## Placement rules

- **The label:** `pipeline.placement.cloudLabel` (default `sdlc:cloud`).
- **An Epic is cloud-placed** when it or its parent Initiative carries the label; every other
  Epic is local. A Task belongs to its Epic; an Initiative and its Product-Roadmap Task read
  the Initiative's own label; a parentless issue reads its own. A label on a Task under an
  Epic is ignored.
- **The session's placement:** env `SDLC_PLACEMENT` (`cloud` | `local`; any other value is an
  error) wins; else a truthy `CLAUDE_CODE_REMOTE` (`true` / `1`) means cloud; else local.
  `show-config` → `session_placement` reports it and the signal that decided it.
- **Guarded commands:** `next-action`, `start-stage`, `list-parallel-ready`,
  `list-design-ready`, `list-ready-for-review` refuse a unit placed on the other side.
- **An Initiative run on the laptop** skips its cloud-placed Epics (`none` `reason`: "placed
  in the cloud"); a cloud-placed Initiative runs only in the cloud.

## `placement_mismatch`

A guarded command exits 1 with `{"error": "placement_mismatch", "session_placement",
"signal", "unit_placement", "epic", "hint"}`: this session is on the wrong side for `epic`.
Stop driving it, relay `hint` to the operator, and start nothing on it. Never set
`SDLC_PLACEMENT` to get past it; that override exists only for a misdetected session, and only
the operator sets it.

## Moving a unit

The operator moves a unit from their own terminal, in the driven repo:

```bash
sdlc-cloud <n> [--force] [--yes]   # go-cloud: place it in the cloud, start `claude --cloud "/sdlc:run <a>"`
sdlc-local <n> [--force] [--yes]   # go-local: place it local, start `claude --model <m> --remote-control sdlc-<a> "/sdlc:run <a>"`
```

- **The anchor moves:** an Initiative or an Epic is its own anchor; a Task moves its Epic, an
  Initiative's own Task its Initiative. For a child, a terminal asks "move #A?"; without one,
  `--yes` is required. A parentless issue is refused (`/sdlc:run` drives only Epics and
  Initiatives).
- **`sdlc-cloud` refuses** (exit 1, `{error, reason}`): a closed anchor; an Epic placed through
  its Initiative (move the Initiative); a cloud unit whose session is live (`reason` names its
  url — attach with `claude --cloud <id>`; `--force`, for a session that died, records it
  `abandoned` and relaunches); a laptop run driving it — a run-state file of the
  anchor, or of an Initiative's Epic, written within `pipeline.placement.localRunMinutes`
  (default 30; `--force` overrides); a live local worktree holding any of its units (`place`'s
  refusal; push or release them, `--force` only on the operator's word). A cloud unit whose
  session ended is relaunched.
- **`sdlc-local` refuses** an Epic placed through its Initiative and a live cloud session
  (stop it on claude.ai first; `--force` records it `abandoned` and proceeds). Already local is
  fine.
- **On a terminal** it execs the `claude` command; otherwise it prints the result with
  `command` for the operator to run. `place <n> --where cloud|local` alone only moves the label.
- The cloud session runs on the CLI's default cloud environment, which the operator picks
  once with `/remote-env`.

## A cloud run

A cloud session owns its unit until it ends. Its first `next-action` posts the session's
marker on the unit that carries the label (`cloud_session`; a failure is a `warnings` line);
never post it yourself. It drives the unit through the normal Steps 1–4 loop until it closes —
through `close-epic` when `pipeline.epicClose.auto` allows. It never uses continuous mode or
`ScheduleWakeup`: with `pipeline.humanChannel: "session"` it waits on the operator in session
("Human channel"); with `"github"` it ends once `next-action` returns `none`, reporting what is
parked on GitHub. Before it ends it posts its end marker:

```bash
python3 "$SDLC" end-cloud-session <n> --outcome closed|waiting-human|stopped [--note "<one line>"]
```

`closed` once the unit merged; `waiting-human` when a gate or `needs-human` is parked on
GitHub; `stopped` for anything else (`abandoned` is `--force`'s). Then Step 4.

Markers, on the unit that carries the label:

- `<!-- sdlc:cloud-session id=<session_…> url=<url> launched=<iso> -->` — the session's own
  registration.
- `<!-- sdlc:cloud-session-end id=… outcome=<outcome> ended=<iso> -->` — `end-cloud-session`.

A session is live from its marker until an end marker with its id.

## GitHub access in the cloud

- **The proxy:** a cloud session reaches api.github.com only through Anthropic's GitHub proxy,
  whatever token is set. It rejects GraphQL (HTTP 403), so every `gh issue view|comment|edit|close`
  and `gh pr view|list|create|ready|merge|checks|edit|comment` fails; REST via `gh api` passes.
- **REST everywhere:** the control plane's GitHub client is REST on the laptop and in the
  cloud. Only two operations differ by placement, having no public REST endpoint: marking a
  PR ready, and listing/replying to/resolving review threads. A cloud session runs them
  through the proxy's `ccr` routes, a local one through GraphQL.
- **Escape hatch:** env `SDLC_GITHUB_API=graphql` selects the old all-GraphQL client (local
  only; the proxy blocks it). `show-config` → `github_api` reports the client and its signal.
- **Field ids:** REST maps each `projectFields` field id (a GraphQL node id) to its field in
  `GET orgs/<org>/issue-fields` and writes options by name: Stage and Pipeline Status by their
  display names (`PR Review`, `Awaiting Human Review`, ...), Priority and Effort by their
  config keys. A field or option missing there is an error, never a guess.
- **The footer:** the proxy appends `---` + `_Generated by [Claude Code](https://claude.ai/code)_`
  to every comment and PR body it writes. The control plane strips it on read; ignore it.
- **Review threads:** `check-gate` thread ids read `ccr:<pr>:<comment-id>`; pass them to
  `resolve-thread` unchanged. A thread id from a local session does not work here.
- **Branch deletes:** the proxy refuses them. Enable the repo's auto-delete of head branches;
  a branch left over comes back as a `warnings` line (`references/operations.md`, "Worktree
  release").
- **Hand-run `gh`:** read with `gh api` GETs (`gh api repos/<repo>/issues/<n>`,
  `.../issues/<n>/comments --paginate`, `.../pulls/<pr>`), never `gh issue view` / `gh pr view`.
  Write only through `python3 "$SDLC" <command>`; a reply on a PR is
  `comment <pr> --body-file <f>`.
- **Token:** `GH_TOKEN` and `GITHUB_TOKEN` start as a proxy placeholder; the SessionStart hook
  overrides both from `tokenEnv` (e.g. `SDLC_GH_TOKEN`). The proxy decides access either way.

## Human channel

`pipeline.humanChannel` (`show-config` → `human_channel`) decides where the operator answers,
on the laptop and in the cloud alike. `"github"` (default): today's flow — `mark-needs-human`,
gate PRs merged on GitHub. `"session"`: you ask in this session with `AskUserQuestion`, which
reaches the operator's phone from a cloud session. Never `PushNotification`, never
`ScheduleWakeup`.

- **What you ask:** everything that would otherwise park a unit for the operator — a
  `needs-human` handback (scope questions, an escalation, a design ambiguity), the escalation
  valve's sixth bounce (`references/rework.md`), an open gate, and an epic close when
  `pipeline.epicClose.auto` is off.
- **First dispatch every other runnable unit the lane caps allow** (the pool queries,
  `references/parallelism.md`), so background agents keep working while the question waits;
  then ask. Batch up to 4 questions in one call.
- **A question:** the stage agent's exact question with its options, plus one line of context
  (unit, stage, what it blocks). Always add an option "Decide later on GitHub".
- **The answer:** `record-operator-answer <n> --question "<gist>" --answer "<answer>"` on the
  unit it settles (a `needs-human` unit goes back to `todo`), then resume or re-delegate the
  owning stage with the answer verbatim in its prompt. A valve trip answered "keep going" gets
  one more context-reset round.
- **"Decide later on GitHub":** `mark-needs-human <n> --reason "<the question>"` and park, as
  with `"github"`.
- **Gates:** open the gate PR as usual, then ask (`references/gates.md`, "Deciding a gate in
  session").
- **Stage agents never ask the operator.** They return questions in their handback
  (`references/stage-playbooks.md`, "The handback is terse").
