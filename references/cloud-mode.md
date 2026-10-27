# Cloud mode — one Epic, one side

`/sdlc:run <n>` runs on the laptop or in a Claude Code cloud session. The placement label
decides which side owns an Epic; the two never work on the same Epic.

## Placement rules

- **Label:** `pipeline.placement.cloudLabel` (default `sdlc:cloud`). An Epic is cloud-placed
  when it or its Initiative carries it. A Task follows its Epic; the label on a Task is ignored.
  An Initiative and its Product-Roadmap Task read the Initiative's label.
- **Local Task of a cloud Epic:** it carries `pipeline.placement.localLabel` (default
  `sdlc:local`; `place <task> --where local|cloud` adds or removes it) or matches
  `pipeline.placement.localTasks` (`e2e-test`, `integration-test`, or `label:<name>`). Use it for
  laptop-only work: e2e with no cloud e2e profile, Stage checks, a real phone, or hosts the cloud
  denies (Android SDK, `dl.google.com`, Maven). `create-lld-tasks` adds the label for a Task
  `lld` marks `Placement: local`.
- **Session placement:** env `SDLC_PLACEMENT` (`cloud` | `local`) wins; else a truthy
  `CLAUDE_CODE_REMOTE` means cloud; else local. `show-config` → `session_placement` reports it.
- **Guarded commands** (`next-action`, `start-stage`, `list-parallel-ready`, `list-design-ready`,
  `list-ready-for-review`) refuse a unit on the other side. On a cloud Epic, a cloud session
  skips local Tasks (`none` lists `local_tasks`); a laptop session drives only them
  (`placement_scope: "local-tasks"`).
- A laptop Initiative run skips its cloud Epics. A cloud-placed Initiative runs only in the cloud.

## `placement_mismatch`

A guarded command exits 1 with `{"error": "placement_mismatch", "session_placement", "signal", "unit_placement", "epic", "hint"}`. Stop driving that
Epic, relay `hint`, and start nothing on it. Never set `SDLC_PLACEMENT` to pass it; only the
operator sets it, for a misdetected session.

## Handoff with `place`

```bash
python3 "$SDLC" place <n> --where cloud|local [--force] --repo-path <p>
```

- On an Epic or Initiative it adds or removes the cloud label (`already: true` when nothing
  changes). On a Task of a cloud Epic it toggles `localLabel`; it refuses a Task in progress or
  held by a local worktree unless `--force`. `still_local` means a `localTasks` kind still matches.
- It refuses (`refused: true`, `worktrees`) while a live local worktree holds any unit under
  `<n>`. Push or release them, then re-run. Use `--force` only on the operator's word.
- `--where local` on an Epic of a cloud Initiative refuses (`inherited_from`): place the
  Initiative. A local-moved Initiative lists its cloud-labelled Epics in `still_cloud`.
- **Laptop → cloud:** stop the local run, then `launch-cloud-epic <n> --repo-path <p>`. The
  session uses the CLI's default cloud environment (`/remote-env`).
- **Cloud → laptop:** end or stop the cloud run, run `place <n> --where local`, then `/sdlc:run <n>`.

## A cloud run

A cloud session drives its one Epic or Initiative through Steps 1–4 until the Epic closes
(`close-epic` when `pipeline.epicClose.auto` allows). It never uses continuous mode or
`ScheduleWakeup`.

- A wait parks only its unit. Dispatch every other runnable unit, then ask in session ("Human
  channel"; a gate: `references/gates.md`, "Deciding a gate in session"). Run
  `cloud-heartbeat <epic> --state asking` before a question.
- End only when nothing else can move (`none`, every question answered or deferred) or the
  operator says stop. With `"github"`, end at the first `none`.

Before the end, post the end marker:

```bash
python3 "$SDLC" end-cloud-session <epic> --outcome <outcome> [--waits-on <n> ...] [--note "<one line>"]
```

| Outcome | When | `--waits-on` |
|---|---|---|
| `closed` | the Epic merged | — |
| `waiting-human` | a gate or `needs-human` is parked | default: those units |
| `waiting-epic` | only another Epic unblocks it | required: that Epic |
| `waiting-local` | `none` says `waiting_local`: only local Tasks remain | default: the open local Tasks |
| `stopped` | stopped by the operator, or anything else | — |
| `abandoned` | laptop only, for a dead session ("Status and stalls") | — |

The recorded wait drives the relaunch ("Initiative → cloud Epics"). Then Step 4.

- **No local cleanup:** the container and its `/tmp` data are discarded at the end. Cleanup
  commands skip worktree and data removal (`skipped: "cloud"`); never remove them by hand. Branch
  and Docker cleanup still run; `close-epic --clean-worktree` still cleans the epic worktree.
- **e2e:** with no cloud e2e profile in the repo's cloud setup, make the e2e-test Task local
  instead of parking `needs-human`.
- **Pushed work survives** a dead session (`agents/development.md`, "Working in the repo").

## GitHub access in the cloud

- **Proxy:** the session reaches api.github.com only through Anthropic's GitHub proxy. It
  rejects GraphQL (403), so `gh issue …` and `gh pr …` fail; `gh api` REST passes.
- **Client:** the control plane uses REST on both sides. Marking a PR ready and review threads
  have no REST endpoint: the cloud uses the proxy's `ccr` routes, the laptop GraphQL. Env
  `SDLC_GITHUB_API=graphql` selects the all-GraphQL client (laptop only); `show-config` →
  `github_api` reports the client.
- **Field ids:** REST maps each `projectFields` id to `GET orgs/<org>/issue-fields` and writes
  options by name. A missing field or option is an error.
- **Footer:** the proxy appends `_Generated by [Claude Code](…)_` to comments and PR bodies. The
  control plane strips it on read.
- **Review threads:** ids read `ccr:<pr>:<comment-id>`; pass them to `resolve-thread` unchanged.
- **Branch deletes:** the proxy refuses them. Enable the repo's auto-delete of head branches;
  otherwise a leftover branch comes back as a `warnings` line.
- **Draft PRs:** `open-dev-pr` recovers a draft create the proxy answers with no body. Relay a
  `draft_warning`: the PR must be a draft before review.
- **Hand-run `gh`:** read with `gh api` GETs, never `gh issue view` / `gh pr view`. Write only
  through `python3 "$SDLC"`. A delegation prompt says exactly "read GitHub with `gh api` GETs;
  write only through `python3 "$SDLC"` commands"; never "GETs only".
- **Token:** the SessionStart hook sets `GH_TOKEN` / `GITHUB_TOKEN` from `tokenEnv`.
- **SSH key:** set it with `core.sshCommand`, never a host alias or `insteadOf` on `origin`.

## Human channel

`pipeline.humanChannel` (`show-config` → `human_channel`) decides where the operator answers.
`"github"` (default): `mark-needs-human` and gate PRs on GitHub. `"session"`: ask in this
session with `AskUserQuestion`. Never use `PushNotification` or `ScheduleWakeup`.

- **Ask about:** a `needs-human` handback, the valve's sixth bounce, an open gate, `escalate:
  true` from `nudge-cloud-epic`, and an epic close when `pipeline.epicClose.auto` is off.
- **First** dispatch every runnable unit the lane caps allow; then ask. Batch up to 4 questions.
- **A question:** the agent's exact question and options, one line of context (unit, stage,
  what it blocks), and the option "Decide later on GitHub".
- **The answer:** `record-operator-answer <n> --question "<gist>" --answer "<answer>"`, then
  resume or re-delegate the owning stage with the answer verbatim. A valve trip answered "keep
  going" gets one more context-reset round.
- **"Decide later on GitHub":** `mark-needs-human <n> --reason "<the question>"` and park.
- **Stage agents never ask the operator**; they return questions in their handback.

## Initiative → cloud Epics

With `pipeline.placement.initiativeEpics: "cloud"` (or the profile's `placement: "cloud"`), a
laptop Initiative run keeps the Initiative's own work and gives each Epic its own cloud session.

- `next-action <initiative>` returns `launch-cloud-epics` (`epics`). For each, run
  `launch-cloud-epic <epic> --repo-path <p>`: it places the Epic, starts `claude --cloud
  "/sdlc:run <epic>"` on a pseudo-terminal, and posts the session marker. Then call
  `next-action <initiative>` again.
- **Launchable:** open, not legacy, not blocked, no live session, footprint disjoint from every
  live cloud Epic's (unknown footprints never count as disjoint;
  `pipeline.placement.softOverlapPaths` never overlap), within `parallelism.cloudSessions`
  (default 3). An ended Epic relaunches once its recorded `waits=` clears: the waited Epic
  closed (`waiting-epic`), or a waited unit closed, changed state, got a comment, or had its
  gate PR merged, reviewed or pushed. An end marker with no waits waits for new GitHub activity.
  `--relaunch` overrides; `survey.relaunch` lists ready ones.
- **`--allow-overlap`** skips the footprint checks (marker `overlap=1`). Operator's word only.
- **A session started by hand:** `record-cloud-session <epic> --session <url|session_…>` (adds the cloud label, posts the launch marker with `manual=1`).
- **Idempotent:** a live session is never relaunched (`already_running`). `ok: false` with
  `output_tail`: no session id printed; relay the tail.
- **`none`** names each Epic's cloud state; report and end. Re-run `/sdlc:run <initiative>` to
  launch newly runnable Epics and, once all close, to close the Initiative
  (`references/initiatives.md`, "Closing an Initiative").
- **Optional watch** (offer once at the end): a `Monitor` on
  `until python3 "$SDLC" cloud-status <initiative> | grep -q '"attention": true'; do sleep 600; done`.
  When it fires, re-run `next-action <initiative>` and act on `cloud-status`.

### Markers

Written on the Epic's issue only by these commands:

- `<!-- sdlc:cloud-session id=<session_…> url=<url> launched=<iso> [overlap=1] [manual=1]
  [heartbeat=<iso> state=working|asking] -->`: `launch-cloud-epic` / `record-cloud-session`;
  `cloud-heartbeat`, `start-stage` and `transition` refresh `heartbeat` / `state` (at most every
  5 minutes, never failing the step).
- `<!-- sdlc:cloud-session-nudge id=… n=<k> at=<iso> -->`: `nudge-cloud-epic`.
- `<!-- sdlc:cloud-session-end id=… outcome=<outcome> ended=<iso> [waits=<n>,…] -->`: `end-cloud-session`.

A session is live from its launch marker until an end marker. Markers never count as activity.

### Status and stalls

```bash
python3 "$SDLC" cloud-status <initiative|epic> [--repo-path <p>]   # read-only, any placement
python3 "$SDLC" nudge-cloud-epic <epic>
```

- `cloud-status` reports per cloud Epic: `session`, children `by_stage` / `by_status`,
  `open_prs`, `gates_pending`, `needs_human`, `last_activity`, `idle_minutes`, `stalled`,
  `end_marker`, `nudges_since_activity`, `escalate`, `waits`, `relaunch_ready` (run
  `launch-cloud-epic <epic>`). An Initiative adds `launchable`, `waiting`, `all_closed`,
  `attention`.
- `stalled`: live session, open Epic, idle ≥ `pipeline.placement.stallMinutes` (default 90), or
  that × `askingStallFactor` (default 6) while `asking`. Read a stalled Epic's run log first;
  end-mark a dead session.
- `nudge-cloud-epic <epic>` sends `continue: /sdlc:run <epic>` to the session. After two
  nudges with no activity it returns `escalate: true`: ask the operator with the session url.
  On "abandon": `end-cloud-session <epic> --outcome abandoned`, then `launch-cloud-epic <epic>
  --relaunch` for a fresh session.
