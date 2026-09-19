"""Subprocess tests for the plugin hooks: each hook gets JSON on stdin, as Claude Code sends it."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_repo(path, config=None):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if config is not None:
        (path / "sdlc-pipeline.config.json").write_text(json.dumps(config))
    return str(path)


@pytest.fixture
def sdlc_repo(tmp_path):
    return _git_repo(tmp_path / "driven", {"repo": "o/r", "docRoot": "docs/sdlc"})


@pytest.fixture
def plain_repo(tmp_path):
    return _git_repo(tmp_path / "plain")


# Hook state (fan-out slots, metrics) never lands in the developer's real plugin data dir.
DATA_DIR = tempfile.mkdtemp(prefix="sdlc-hook-data-")


def run_hook(name, payload, env=None):
    full_env = {k: v for k, v in os.environ.items()
                if k not in ("GITHUB_TOKEN", "CLAUDE_ENV_FILE", "SDLC_CONFIG", "SDLC_RUNS_DIR")}
    full_env["CLAUDE_PLUGIN_DATA"] = DATA_DIR
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload),
                          capture_output=True, text=True, env=full_env, timeout=10)
    return proc


def guard(command, cwd, agent_type=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "t1",
               "cwd": cwd}
    if agent_type:
        payload.update(agent_id="a1", agent_type=agent_type)
    proc = run_hook("bash_guard.py", payload)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


DENIED = [
    "gh api graphql -f query='{ viewer { login } }'",
    "GITHUB_TOKEN=$(cat ~/.t) gh api graphql -f query=@q.graphql",
    "gh issue create --title x --body y",
    "gh -R o/r issue create --title x --body y --label bug",
    "rtk gh issue create --title x",
    "gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:\"T\"}) { thread { id } } }'",
    "gh api graphql -f query='mutation { addPullRequestReviewThreadReply(input:{}) { comment { id } } }'",
    "gh issue edit 5 --add-label foo",
    "gh issue close 5",
    "gh issue reopen 5",
    "gh pr create --draft --title x --body y",
    "gh pr merge 12 --squash",
    "gh pr ready 12",
    "gh pr close 12",
    "gh api -X PATCH repos/o/r/issues/5 -f state=closed",
    "gh api repos/o/r/issues/5 --method=DELETE",
    "gh api -XPOST repos/o/r/issues",
    "git worktree add /tmp/sdlc-dev-5 -b issue-5",
    "git -C /repo worktree add /tmp/x issue-5",
    "git push --force origin issue-5",
    "git push -f",
    "git push --force-with-lease origin issue-5",
    "git push origin +issue-5",
    "git rebase origin/main",
    "git pull --rebase",
    "cd /tmp/sdlc-dev-5 && git rebase -i HEAD~2",
    "git status; gh pr merge 3",
    "echo ok | gh issue close 4",
    "rtk git rebase main",
    "git status\ngh pr merge 3",
    "echo $(gh pr merge 3)",
    # bypasses: implicit POST, gh aliases, quoted/backtick substitution, shell keywords, sh -c
    "gh api repos/o/r/issues -f title=x -f body=y",
    "gh api repos/o/r/issues --input issue.json",
    "gh issue new --title x",
    "gh pr new --title x",
    'URL="$(gh pr create --title x --body y)"',
    "echo `gh pr merge 3`",
    "if true; then gh pr merge 3; fi",
    "{ gh pr merge 3; }",
    "timeout 60 gh pr merge 3",
    "bash -c 'gh pr merge 3'",
    "eval 'git rebase main'",
    # wrapper options that take a value
    "sudo -u deploy gh pr merge 3",
    "sudo -E -u deploy git rebase main",
    "sudo -Eu deploy gh pr merge 3",
    "sudo --user deploy gh issue close 4",
    "echo 3 | xargs gh pr merge",
    "xargs -n 1 -P 4 gh pr merge < prs.txt",
    "xargs -I {} gh pr merge {} < prs.txt",
    "timeout -s KILL 60 gh pr merge 3",
    "timeout --signal=KILL 60 gh pr merge 3",
    "timeout -k 5 --signal KILL 60 git rebase main",
    "env -u GH_HOST gh pr merge 3",
]

ALLOWED = [
    'python3 "$SDLC" merge-pr 12 --issue 5',
    'python3 "$SDLC" merge-gate 40 --issue 5 --stage product --operator-confirmed',
    'python3 "$SDLC" mark-feedback-addressed 5',
    'python3 "$SDLC" open-dev-pr 5 --title t --body "gh pr merge 5 && git rebase main" --summary s',
    'python3 /x/scripts/sdlc_next.py handoff-to-pr-review 5 --pr 9 --summary "use gh api graphql"',
    "gh issue view 5 --comments",
    "gh issue comment 5 --body 'gh issue close 5'",
    "gh pr view 12 --json files",
    "gh pr diff 12",
    "gh api repos/o/r/pulls/12/comments",
    "gh api -X GET repos/o/r/issues",
    "git fetch origin && git diff origin/main...HEAD --stat",
    "git push origin issue-5",
    "git push -u origin issue-5",
    "git worktree add --detach /tmp/sdlc-review-5 origin/issue-5",
    "git worktree remove --force /tmp/sdlc-review-5",
    "git log --grep 'git rebase'",
    "echo 'git rebase main'",
    "# git rebase main\ngit status",
    "cat <<'EOF' > notes.md\ngh pr merge 3\ngit rebase main\nEOF\nwc -c notes.md",
    'git commit -m "never git rebase or gh pr merge by hand"',
    "gh api -X GET search/issues -f q=x",
    "gh run view 7 --log-failed",
    "git commit -m \"$(cat <<'EOF'\nfix: x\n\nnever gh pr merge\nEOF\n)\"",
    "gh issue comment 5 --body 'run `gh pr merge 5` yourself'",
    'gh issue comment 5 --body "ran \\`gh pr view\\`"',
    "N=$((1 + 2)); echo \"$N\"",
    "sudo -u deploy ls /srv",
    "xargs -n 1 echo gh pr merge < prs.txt",
    "timeout -s KILL 60 git status",
]


@pytest.mark.parametrize("command", DENIED)
def test_guard_denies(sdlc_repo, command):
    reason = guard(command, sdlc_repo)
    assert reason and reason.startswith("sdlc guard: ")


@pytest.mark.parametrize("command", ALLOWED)
def test_guard_allows(sdlc_repo, command):
    assert guard(command, sdlc_repo) is None


def test_guard_reason_names_control_plane_command(sdlc_repo):
    assert "merge-pr" in guard("gh pr merge 3", sdlc_repo)
    assert "merge-design-pr" in guard("gh pr merge 3", sdlc_repo)
    reason = guard("gh pr merge 3", sdlc_repo)
    assert "merge-gate" in reason and "--operator-confirmed" in reason
    assert "operator explicitly said" in reason
    assert "open-design-pr" in guard("gh pr create --base x", sdlc_repo)
    assert "create-issue" in guard("gh issue create -t x", sdlc_repo)
    assert "set-stage" in guard("gh issue edit 5 --add-label bug", sdlc_repo)
    assert "resolve-thread" in guard("gh api graphql -f query='mutation { resolveReviewThread }'", sdlc_repo)
    assert "start-stage" in guard("git worktree add /tmp/x", sdlc_repo)
    assert "sync-branch" in guard("git rebase main", sdlc_repo)


def test_guard_noop_outside_sdlc_repo(plain_repo, tmp_path):
    assert guard("gh pr merge 3", plain_repo) is None
    assert guard("git rebase main", str(tmp_path)) is None


def _guard_env(command, cwd, env=None):
    return run_hook("bash_guard.py", {"tool_name": "Bash", "tool_input": {"command": command},
                                      "cwd": cwd}, env=env).stdout


def test_guard_finds_config_like_the_script(tmp_path, plain_repo):
    # Walk-up from a non-git cwd, as sdlc_next.py's _find_config does.
    root = tmp_path / "cfgroot"
    (root / ".config").mkdir(parents=True)
    (root / ".config" / "sdlc-pipeline.config.json").write_text("{}")
    (root / "a" / "b").mkdir(parents=True)
    assert "deny" in _guard_env("gh pr merge 3", str(root / "a" / "b"))
    # $SDLC_CONFIG wins over the cwd; a missing one is a no-op.
    config = root / ".config" / "sdlc-pipeline.config.json"
    assert "deny" in _guard_env("gh pr merge 3", plain_repo, {"SDLC_CONFIG": str(config)})
    assert _guard_env("gh pr merge 3", plain_repo, {"SDLC_CONFIG": str(root / "nope.json")}) == ""


def test_guard_ignores_other_tools(sdlc_repo):
    proc = run_hook("bash_guard.py", {"tool_name": "Read", "tool_input": {"file_path": "x"},
                                      "cwd": sdlc_repo})
    assert proc.returncode == 0 and proc.stdout == ""


def test_guard_fails_open_on_garbage():
    proc = subprocess.run([sys.executable, os.path.join(HOOKS, "bash_guard.py")],
                          input="not json", capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0 and proc.stdout == ""


# --- role allowlist ---------------------------------------------------------------------

CP = 'python3 "$SDLC" '
ROLE_ALLOWED = [
    ("sdlc:development", CP + "open-dev-pr 5 --title t --body b --summary s"),
    ("sdlc:development", CP + "record-local-ci --pr 9 --suite backend --sha a --command c --output o"),
    ("sdlc:development", "python3 /p/scripts/sdlc_next.py handoff-to-pr-review 5 --pr 9 --summary s"),
    ("sdlc:development", 'git commit -m x && git push origin issue-5'),
    ("sdlc:pr-review", CP + "record-pr-review 5 --pr 9 --outcome clean --summary s"),
    ("sdlc:pr-review", "git restore src/a.ts && git status"),
    ("sdlc:design-review", CP + "record-design-review 5 --role lld-review --outcome clean --summary s"),
    ("sdlc:design-review", CP + "record-design-review 5 --role arch-review --outcome rework --summary s --same-class-recurrence"),
    ("sdlc:product-review", CP + "record-design-review 5 --role product-review --outcome rework --summary s"),
    ("sdlc:exploratory", CP + "show-config"),
    ("sdlc:initiative-close", CP + "check-initiative-closeable 1"),
    ("sdlc:initiative-close", CP + "record-initiative-verification 1 --summary s"),
    ("sdlc:lld", CP + "lld-section --epic 9 --task 5 --repo-path /w"),
    ("sdlc:product", CP + "show-config"),
    ("sdlc:architecture", CP + "cite docs/a.md --line 3"),
    ("sdlc:architecture", CP + "--help"),
    ("sdlc:lld", "git commit -m 'lld' && git push origin issue-5"),
    ("sdlc:product", CP + "post-comment 5 --role product --body-file /tmp/c.md"),
    ("sdlc:architecture", CP + "post-comment 5 --body-file /tmp/c.md --role=architecture"),
    ("sdlc:lld", CP + "post-comment 5 --role lld --body-file /tmp/c.md"),
]
ROLE_DENIED = [
    ("sdlc:development", CP + "set-stage 5 --stage pr-review", "orchestrator"),
    ("sdlc:development", CP + "merge-pr 9 --issue 5", "orchestrator"),
    ("sdlc:pr-review", CP + "merge-pr 9 --issue 5", "orchestrator"),
    ("sdlc:pr-review", CP + "sync-branch 5", "orchestrator"),
    ("sdlc:exploratory", CP + "record-epic-verification 9 --kind exploratory --summary s",
     "orchestrator"),
    ("sdlc:product", CP + "create-issue --parent 1 --title t --body b", "orchestrator"),
    ("sdlc:architecture", CP + "mark-needs-human 5 --reason r", "orchestrator"),
    ("sdlc:lld", 'SDLC=x; "$SDLC" finish-lld 5 --epic 9 --repo-path /w', "orchestrator"),
    ("sdlc:lld", "python3 -u /p/scripts/sdlc_next.py close-issue 5", "orchestrator"),
    ("sdlc:design-review", CP + "skip-gate 5 --stage architecture --confidence 90 --summary s", "orchestrator"),
    ("sdlc:design-review", CP + "merge-design-pr 40 --issue 5", "orchestrator"),
    ("sdlc:design-review", CP + "open-design-pr 5", "orchestrator"),
    ("sdlc:architecture", CP + "open-design-pr 5", "orchestrator"),
    ("sdlc:lld", CP + "merge-design-pr 40 --issue 5", "orchestrator"),
    ("sdlc:architecture", CP + "merge-gate 40 --issue 5 --stage architecture --operator-confirmed",
     "orchestrator"),
    ("sdlc:product", CP + "merge-gate 40 --issue 5 --stage product --operator-confirmed",
     "orchestrator"),
    ("sdlc:architecture", CP + "mark-feedback-addressed 5", "orchestrator"),
    ("sdlc:product", CP + "mark-feedback-addressed 5", "orchestrator"),
    ("sdlc:architecture", CP + "cut-phase-tasks 9 --repo-path /w", "orchestrator"),
    ("sdlc:pr-review", CP + "claim 5 --role pr-review", "orchestrator"),
    ("sdlc:exploratory", "echo $(" + CP + "close-epic 9)", "orchestrator"),
    ("sdlc:initiative-close", CP + "close-initiative 1", "orchestrator"),
    ("sdlc:development", CP + "route 5 --to merge --reason r", "orchestrator"),
    ("sdlc:design-review", CP + "waive-gate 5 --stage architecture --summary s", "orchestrator"),
    ("sdlc:product", CP + "post-comment 5 --role architecture --body-file f", "only your own stage"),
    ("sdlc:lld", CP + "post-comment 5 --body-file f", "only your own stage"),
    ("sdlc:design-review", CP + "post-comment 5 --role lld --body-file f", "orchestrator"),
    ("sdlc:development", CP + "post-comment 5 --role development --body-file f", "orchestrator"),
    ("sdlc:pr-review", CP + "post-comment 5 --role pr-review --body-file f", "orchestrator"),
    ("sdlc:pr-review", "git commit -am fix", "read-only on the branch"),
    ("sdlc:pr-review", "git push origin issue-5", "read-only on the branch"),
    ("sdlc:design-review", "git -C /w merge origin/main", "read-only on the branch"),
    ("sdlc:product-review", "git reset --hard HEAD~1", "read-only on the branch"),
    ("sdlc:pr-review", "git checkout -- src/a.ts", "read-only on the branch"),
]


@pytest.mark.parametrize("agent_type,command", ROLE_ALLOWED)
def test_role_allowlist_allows(sdlc_repo, agent_type, command):
    assert guard(command, sdlc_repo, agent_type) is None


@pytest.mark.parametrize("agent_type,command,reason", ROLE_DENIED)
def test_role_allowlist_denies(sdlc_repo, agent_type, command, reason):
    assert reason in guard(command, sdlc_repo, agent_type)


@pytest.mark.parametrize("agent_type", [None, "general-purpose"])
def test_orchestrator_and_other_agents_keep_every_command(sdlc_repo, agent_type):
    assert guard(CP + "set-stage 5 --stage development", sdlc_repo, agent_type) is None
    assert guard(CP + "route 5 --to development --reason r", sdlc_repo, agent_type) is None
    assert guard("git commit -m x && git reset --hard", sdlc_repo, agent_type) is None


# --- SubagentStop -----------------------------------------------------------------------

def _transcript(path, final_text, extra_lines=()):
    lines = [
        {"type": "user", "message": {"role": "user", "content": "do the stage"}},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "text", "text": "SDLC-RESULT: {\"issue\": 1, \"stage\": \"old\", \"outcome\": \"done\"}"},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]}},
        {"type": "attachment", "attachment": {}},
        {"type": "assistant", "message": {"id": "m2", "role": "assistant",
                                          "content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "assistant", "message": {"id": "m2", "role": "assistant",
                                          "content": [{"type": "text", "text": final_text}]}},
        *extra_lines,
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(path)


def stop(tmp_path, cwd, text, agent_type="sdlc:development", active=False):
    tpath = _transcript(tmp_path / "agent.jsonl", text)
    return run_hook("subagent_stop.py", {
        "agent_id": "a1", "agent_type": agent_type, "agent_transcript_path": tpath,
        "stop_hook_active": active, "cwd": cwd, "hook_event_name": "SubagentStop"})


GOOD = 'HEAD: abc123 / PR #9\nDETAIL: https://x\nSDLC-RESULT: {"issue": 5, "stage": "development", "outcome": "done"}'


@pytest.mark.parametrize("text", [
    GOOD,
    '`SDLC-RESULT: {"issue": 7, "stage": "pr-review", "outcome": "needs-human"}`',
    'SDLC-RESULT: {"issue": 5, "stage": "development", "outcome": "done", "next": "merge", '
    '"why": "pr-review not needed -- docs-only diff"}',
])
def test_subagent_stop_passes(tmp_path, sdlc_repo, text):
    proc = stop(tmp_path, sdlc_repo, text)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("text,problem", [
    ("All done, monitoring the suite.", "no SDLC-RESULT line"),
    ('SDLC-RESULT: {"issue": "5", "stage": "development", "outcome": "done"}', '"issue"'),
    ('SDLC-RESULT: {"issue": 5, "stage": "development", "outcome": "finished"}', '"outcome"'),
    ('SDLC-RESULT: {"issue": 5, "outcome": "done"}', '"stage"'),
    ("SDLC-RESULT: {issue: 5}", "not valid JSON"),
    ('SDLC-RESULT: {"issue": 5, "stage": "product", "outcome": "done", "next": "qa", "why": "x"}',
     '"next"'),
    ('SDLC-RESULT: {"issue": 5, "stage": "product", "outcome": "done", "why": "no next"}',
     '"next"'),
    ('SDLC-RESULT: {"issue": 5, "stage": "product", "outcome": "done", "next": "development"}',
     '"why"'),
    ('SDLC-RESULT: {"issue": 5, "stage": "product", "outcome": "done", "next": "development", '
     '"why": "' + "x" * 121 + '"}', '"why"'),
])
def test_subagent_stop_blocks(tmp_path, sdlc_repo, text, problem):
    proc = stop(tmp_path, sdlc_repo, text)
    assert proc.returncode == 2
    assert problem in proc.stderr and "SDLC-RESULT: {" in proc.stderr


POST_CMD = 'python3 "$SDLC" post-comment 5 --role {role} --body-file /tmp/c.md'
DONE = 'SDLC-RESULT: {{"issue": 5, "stage": "{role}", "outcome": "{outcome}"}}'


def _post_lines(role, ok=True, resume_after=False, cmd=None, tid="p1"):
    lines = [
        {"type": "assistant", "message": {"id": "m9", "role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash",
             "input": {"command": cmd or POST_CMD.format(role=role)}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": not ok,
             "content": '{"issue": 5, "role": "%s", "posted": true}' % role if ok
             else '{"error": "not claimed"}'}]}},
    ]
    if resume_after:
        lines.append({"type": "user", "message": {"role": "user", "content": "rework this"}})
    return lines


def stop_role(tmp_path, cwd, role, outcome="done", lines=(), agent_type=None):
    entries = [{"type": "user", "message": {"role": "user", "content": "do the stage"}}, *lines,
               {"type": "assistant", "message": {"id": "mf", "role": "assistant", "content": [
                   {"type": "text", "text": DONE.format(role=role, outcome=outcome)}]}}]
    tpath = tmp_path / "agent.jsonl"
    tpath.write_text("\n".join(json.dumps(x) for x in entries) + "\n")
    tpath = str(tpath)
    return run_hook("subagent_stop.py", {
        "agent_id": "a1", "agent_type": agent_type or f"sdlc:{role}",
        "agent_transcript_path": tpath, "stop_hook_active": False, "cwd": cwd})


@pytest.mark.parametrize("role", ["product", "architecture", "lld"])
def test_authoring_stage_done_without_a_posted_handoff_is_blocked(tmp_path, sdlc_repo, role):
    proc = stop_role(tmp_path, sdlc_repo, role)
    assert proc.returncode == 2
    assert f"post-comment 5 --role {role}" in proc.stderr


@pytest.mark.parametrize("role", ["product", "architecture", "lld"])
def test_authoring_stage_done_with_a_posted_handoff_stops(tmp_path, sdlc_repo, role):
    assert stop_role(tmp_path, sdlc_repo, role, lines=_post_lines(role)).returncode == 0


def test_a_failed_or_wrong_role_post_does_not_count(tmp_path, sdlc_repo):
    assert stop_role(tmp_path, sdlc_repo, "product",
                     lines=_post_lines("product", ok=False)).returncode == 2
    assert stop_role(tmp_path, sdlc_repo, "product",
                     lines=_post_lines("lld")).returncode == 2


def test_a_post_from_before_a_resume_message_does_not_count(tmp_path, sdlc_repo):
    lines = _post_lines("product", resume_after=True)
    assert stop_role(tmp_path, sdlc_repo, "product", lines=lines).returncode == 2
    assert stop_role(tmp_path, sdlc_repo, "product",
                     lines=[*lines, *_post_lines("product", tid="p2")]).returncode == 0


@pytest.mark.parametrize("outcome", ["needs-human", "blocked", "failed", "rework"])
def test_only_a_done_outcome_needs_the_handoff_comment(tmp_path, sdlc_repo, outcome):
    assert stop_role(tmp_path, sdlc_repo, "product", outcome=outcome).returncode == 0


@pytest.mark.parametrize("agent_type", ["sdlc:development", "sdlc:pr-review", "sdlc:design-review"])
def test_other_roles_are_not_held_to_a_handoff_comment(tmp_path, sdlc_repo, agent_type):
    assert stop_role(tmp_path, sdlc_repo, "product", agent_type=agent_type).returncode == 0


def test_the_handoff_check_fails_open_on_an_unreadable_transcript(sdlc_repo):
    proc = run_hook("subagent_stop.py", {
        "agent_type": "sdlc:product", "cwd": sdlc_repo, "stop_hook_active": False,
        "last_assistant_message": DONE.format(role="product", outcome="done"),
        "agent_transcript_path": "/nonexistent/x.jsonl"})
    assert proc.returncode == 0


def test_subagent_stop_reads_only_final_turn(tmp_path, sdlc_repo):
    # The earlier turn's valid line must not satisfy a final turn that lacks one.
    assert stop(tmp_path, sdlc_repo, "waiting for CI").returncode == 2


def test_subagent_stop_hook_active_allows(tmp_path, sdlc_repo):
    assert stop(tmp_path, sdlc_repo, "no result", active=True).returncode == 0


def test_subagent_stop_ignores_other_agents(tmp_path, sdlc_repo):
    assert stop(tmp_path, sdlc_repo, "no result", agent_type="general-purpose").returncode == 0


def test_subagent_stop_noop_outside_sdlc_repo(tmp_path, plain_repo):
    assert stop(tmp_path, plain_repo, "no result").returncode == 0


def test_subagent_stop_fails_open_on_missing_transcript(sdlc_repo):
    proc = run_hook("subagent_stop.py", {"agent_type": "sdlc:lld", "cwd": sdlc_repo,
                                         "agent_transcript_path": "/nonexistent/x.jsonl"})
    assert proc.returncode == 0


# --- SessionStart -----------------------------------------------------------------------

def test_session_start_writes_env_file(tmp_path, sdlc_repo):
    env_file = tmp_path / "env.sh"
    proc = run_hook("session_start.py", {"cwd": sdlc_repo, "source": "startup"},
                    env={"CLAUDE_ENV_FILE": str(env_file), "GITHUB_TOKEN": "ghp_x"})
    assert proc.returncode == 0, proc.stderr
    content = env_file.read_text()
    assert "export SDLC=" in content and content.rstrip().endswith("scripts/sdlc_next.py")
    assert "GITHUB_TOKEN" not in content  # no tokenPath in this config
    assert "GITHUB_TOKEN" not in proc.stdout  # already set: no warning


def test_session_start_wires_token_from_config(tmp_path):
    token = tmp_path / "tok"
    token.write_text("ghp_secret\n")
    repo = _git_repo(tmp_path / "r", {"repo": "o/r", "tokenPath": str(token)})
    env_file = tmp_path / "env.sh"
    run_hook("session_start.py", {"cwd": repo}, env={"CLAUDE_ENV_FILE": str(env_file)})
    content = env_file.read_text()
    assert "export GITHUB_TOKEN=" in content and "ghp_secret" not in content
    # The env file is sourced before each Bash call: the configured file overrides an ambient token.
    probe = f"{content}\nprintf %s \"$GITHUB_TOKEN\""
    clean_env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    assert subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                          env=clean_env).stdout == "ghp_secret"
    assert subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                          env={**clean_env, "GITHUB_TOKEN": "github_pat_stray"}).stdout == "ghp_secret"


def _token_probe(tmp_path, config, env, ambient=None):
    repo = _git_repo(tmp_path / "r", {"repo": "o/r", **config})
    env_file = tmp_path / "env.sh"
    proc = run_hook("session_start.py", {"cwd": repo}, env={"CLAUDE_ENV_FILE": str(env_file), **env})
    clean_env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    clean_env.pop("MY_GH_TOKEN", None)
    clean_env.update(env)
    if ambient:
        clean_env["GITHUB_TOKEN"] = ambient
    out = subprocess.run(["bash", "-c", f'{env_file.read_text()}\nprintf %s "$GITHUB_TOKEN"'],
                         capture_output=True, text=True, env=clean_env).stdout
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout else ""
    return out, ctx


def test_token_env_beats_token_path_and_ambient(tmp_path):
    token = tmp_path / "tok"
    token.write_text("ghp_file\n")
    out, ctx = _token_probe(tmp_path, {"tokenPath": str(token), "tokenEnv": "MY_GH_TOKEN"},
                            {"MY_GH_TOKEN": "ghp_env"}, ambient="github_pat_stray")
    assert out == "ghp_env" and "classic PAT" not in ctx


def test_token_env_falls_back_to_token_path_when_unset(tmp_path):
    token = tmp_path / "tok"
    token.write_text("ghp_file\n")
    out, _ = _token_probe(tmp_path, {"tokenPath": str(token), "tokenEnv": "MY_GH_TOKEN"}, {},
                          ambient="github_pat_stray")
    assert out == "ghp_file"


def test_token_env_alone_works_without_token_file(tmp_path):
    out, _ = _token_probe(tmp_path, {"tokenEnv": "MY_GH_TOKEN"}, {"MY_GH_TOKEN": "ghp_env"})
    assert out == "ghp_env"


def test_session_start_warns_when_configured_token_is_not_classic(tmp_path):
    _, ctx = _token_probe(tmp_path, {"tokenEnv": "MY_GH_TOKEN"}, {"MY_GH_TOKEN": "github_pat_x"})
    assert "not a classic PAT" in ctx


def test_session_start_ignores_an_invalid_token_env_name(tmp_path):
    token = tmp_path / "tok"
    token.write_text("ghp_file\n")
    out, _ = _token_probe(tmp_path, {"tokenPath": str(token), "tokenEnv": "x; rm -rf /"}, {})
    assert out == "ghp_file"


@pytest.mark.parametrize("token_path", ["tok", "~/tok"])
def test_session_start_exports_an_absolute_token_path(tmp_path, token_path):
    home = tmp_path / "home"
    repo = _git_repo(tmp_path / "r", {"repo": "o/r", "tokenPath": token_path})
    tok_dir = home if token_path.startswith("~") else tmp_path / "r"
    tok_dir.mkdir(exist_ok=True)
    (tok_dir / "tok").write_text("ghp_secret\n")
    env_file = tmp_path / "env.sh"
    run_hook("session_start.py", {"cwd": repo},
             env={"CLAUDE_ENV_FILE": str(env_file), "HOME": str(home)})
    content = env_file.read_text()
    assert str(tok_dir / "tok") in content and "ghp_secret" not in content
    elsewhere = tmp_path / "worktree"
    elsewhere.mkdir()
    clean_env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    probe = f"{content}\nprintf %s \"$GITHUB_TOKEN\""
    assert subprocess.run(["bash", "-c", probe], capture_output=True, text=True, env=clean_env,
                          cwd=elsewhere).stdout == "ghp_secret"


def test_session_start_warns_when_token_missing(tmp_path, sdlc_repo):
    env_file = tmp_path / "env.sh"
    proc = run_hook("session_start.py", {"cwd": sdlc_repo}, env={"CLAUDE_ENV_FILE": str(env_file)})
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart" and "GITHUB_TOKEN" in ctx["additionalContext"]


def test_session_start_noop_outside_sdlc_repo(tmp_path, plain_repo):
    env_file = tmp_path / "env.sh"
    proc = run_hook("session_start.py", {"cwd": plain_repo}, env={"CLAUDE_ENV_FILE": str(env_file)})
    assert proc.returncode == 0 and proc.stdout == "" and not env_file.exists()


# --- this repo's own Stop check (.claude/dev_stop_check.py) ------------------------------

def _dev_stop(cwd, stdin):
    return subprocess.run([sys.executable, os.path.join(os.path.dirname(HOOKS), ".claude",
                                                        "dev_stop_check.py")],
                          input=stdin, capture_output=True, text=True, cwd=cwd, timeout=60)


def test_dev_stop_check_fails_open_on_a_non_object_payload(tmp_path):
    repo = _git_repo(tmp_path / "r")
    tests = tmp_path / "r" / "hooks" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_x.py").write_text("def test_x():\n    assert False\n")
    assert _dev_stop(repo, "[]").returncode == 0
    assert _dev_stop(repo, '"x"').returncode == 0
    assert _dev_stop(repo, "{}").returncode == 2  # a real payload still runs the failing suite


# --- agent_guard (PreToolUse Agent|Task) -------------------------------------------------

def _policy_repo(tmp_path, pipeline=None):
    return _git_repo(tmp_path / "pol", {"repo": "o/r", "pipeline": pipeline or {}})


def launch(cwd, tmp_path, subagent_type, model="opus", prompt="do it", parent=None,
           transcript=None):
    tool_input = {"subagent_type": subagent_type, "model": model, "prompt": prompt,
                  "description": "d", "run_in_background": True}
    payload = {"tool_name": "Agent", "tool_input": tool_input, "cwd": cwd,
               "transcript_path": transcript or str(tmp_path / "s.jsonl")}
    if parent:
        payload.update(agent_id=parent[0], agent_type=parent[1])
    proc = run_hook("agent_guard.py", payload, env={"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else None


def forced_model(out):
    assert out["permissionDecision"] == "allow", out
    return out["updatedInput"]["model"]


def test_agent_guard_forces_stage_model_and_keeps_other_fields(tmp_path, sdlc_repo):
    out = launch(sdlc_repo, tmp_path, "sdlc:development", model="opus")
    assert out["updatedInput"] == {"subagent_type": "sdlc:development", "model": "sonnet",
                                   "prompt": "do it", "description": "d",
                                   "run_in_background": True}
    assert forced_model(launch(sdlc_repo, tmp_path, "sdlc:pr-review", model=None)) == "opus"
    assert launch(sdlc_repo, tmp_path, "sdlc:lld", model="sonnet") is None  # already right


def test_agent_guard_config_overrides_the_policy(tmp_path):
    repo = _policy_repo(tmp_path, {"models": {"development": "opus"}})
    assert forced_model(launch(repo, tmp_path, "sdlc:development", model="sonnet")) == "opus"


def test_agent_guard_design_review_role_header(tmp_path, sdlc_repo):
    assert forced_model(launch(sdlc_repo, tmp_path, "sdlc:design-review", model="haiku")) == "opus"
    repo = _policy_repo(tmp_path, {"models": {"lld-review": "sonnet"}})
    out = launch(repo, tmp_path, "sdlc:design-review", prompt="Review the lld")
    assert out["permissionDecision"] == "deny" and "ROLE: arch-review|lld-review" in \
        out["permissionDecisionReason"]
    assert forced_model(launch(repo, tmp_path, "sdlc:design-review",
                               prompt="ROLE: lld-review ISSUE: 5\nReview")) == "sonnet"
    assert launch(repo, tmp_path, "sdlc:design-review", model="opus",
                  prompt="ROLE: arch-review ISSUE: 5\nReview") is None


def _parent_transcript(tmp_path, agent_id, prompt):
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")


def test_agent_guard_forces_fanout_model(tmp_path, sdlc_repo):
    out = launch(sdlc_repo, tmp_path, "general-purpose", model="opus",
                 parent=("p1", "sdlc:design-review"))
    assert forced_model(out) == "sonnet" and out["updatedInput"]["prompt"] == "do it"


def test_agent_guard_axis_override(tmp_path):
    repo = _policy_repo(tmp_path, {"fanout": {"arch-review": {
        "allowed": True, "maxChildren": 3, "model": "sonnet", "axisModels": {"completeness": "opus"}}}})
    _parent_transcript(tmp_path, "p1", "ROLE: arch-review ISSUE: 5\nReview")
    parent = ("p1", "sdlc:design-review")
    assert forced_model(launch(repo, tmp_path, "general-purpose", model="haiku", parent=parent,
                               prompt="AXIS: completeness\nFind gaps")) == "opus"
    assert forced_model(launch(repo, tmp_path, "general-purpose", model="opus", parent=parent,
                               prompt="AXIS: flows\nTrace")) == "sonnet"


def test_agent_guard_reads_design_review_role_from_parent_transcript(tmp_path):
    repo = _policy_repo(tmp_path, {"fanout": {"lld-review": {"allowed": False}}})
    _parent_transcript(tmp_path, "arch", "ROLE: arch-review ISSUE: 5\nReview")
    _parent_transcript(tmp_path, "lld", "ROLE: lld-review ISSUE: 6\nReview")
    assert forced_model(launch(repo, tmp_path, "Explore", parent=("arch", "sdlc:design-review"))) == "sonnet"
    out = launch(repo, tmp_path, "Explore", parent=("lld", "sdlc:design-review"))
    assert out["permissionDecision"] == "deny" and "single pass" in out["permissionDecisionReason"]


def test_agent_guard_denies_nested_stage(tmp_path, sdlc_repo):
    out = launch(sdlc_repo, tmp_path, "sdlc:development", parent=("p1", "sdlc:pr-review"))
    assert out["permissionDecision"] == "deny" and "never launches another stage" in \
        out["permissionDecisionReason"]


@pytest.mark.parametrize("parent", ["sdlc:pr-review", "sdlc:product-review", "sdlc:development"])
def test_agent_guard_denies_fanout_where_not_allowed(tmp_path, sdlc_repo, parent):
    out = launch(sdlc_repo, tmp_path, "general-purpose", parent=("p1", parent))
    assert out["permissionDecision"] == "deny" and "single pass" in out["permissionDecisionReason"]


def test_agent_guard_lets_explore_roles_launch_explore_only(tmp_path, sdlc_repo):
    for role in ("development", "lld"):
        assert launch(sdlc_repo, tmp_path, "Explore", parent=("p1", f"sdlc:{role}")) is None
    out = launch(sdlc_repo, tmp_path, "Explore", parent=("p1", "sdlc:pr-review"))
    assert out["permissionDecision"] == "deny" and "single pass" in out["permissionDecisionReason"]


def test_agent_guard_enforces_max_children(tmp_path, sdlc_repo):
    parent = ("p1", "sdlc:design-review")
    assert forced_model(launch(sdlc_repo, tmp_path, "general-purpose", parent=parent)) == "sonnet"
    assert forced_model(launch(sdlc_repo, tmp_path, "general-purpose", parent=parent)) == "sonnet"
    out = launch(sdlc_repo, tmp_path, "general-purpose", parent=parent)
    assert out["permissionDecision"] == "deny" and "at most 2" in out["permissionDecisionReason"]
    other = launch(sdlc_repo, tmp_path, "general-purpose", parent=("p2", "sdlc:design-review"))
    assert forced_model(other) == "sonnet"


def test_agent_guard_counts_parallel_launches_exactly(tmp_path, sdlc_repo):
    payload = json.dumps({"tool_name": "Agent", "cwd": sdlc_repo, "agent_id": "p9",
                          "agent_type": "sdlc:design-review",
                          "tool_input": {"subagent_type": "general-purpose", "prompt": "x"}})
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    procs = [subprocess.Popen([sys.executable, os.path.join(HOOKS, "agent_guard.py")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
             for _ in range(6)]
    outs = [json.loads(p.communicate(payload)[0])["hookSpecificOutput"] for p in procs]
    assert sorted(o["permissionDecision"] for o in outs) == ["allow"] * 2 + ["deny"] * 4


def test_agent_guard_expires_stale_slots(tmp_path, sdlc_repo):
    slots = tmp_path / "data" / "fanout"
    slots.mkdir(parents=True)
    (slots / "p1").write_text("2")
    os.utime(slots / "p1", (0, 0))
    assert forced_model(launch(sdlc_repo, tmp_path, "general-purpose",
                               parent=("p1", "sdlc:design-review"))) == "sonnet"


def test_agent_guard_leaves_other_agents_alone(tmp_path, sdlc_repo, plain_repo):
    assert launch(sdlc_repo, tmp_path, "general-purpose") is None
    assert launch(sdlc_repo, tmp_path, "Explore", parent=("g1", "general-purpose")) is None
    assert launch(plain_repo, tmp_path, "sdlc:development", model="opus") is None


# --- SubagentStart ----------------------------------------------------------------------

def test_subagent_start_tells_sdlc_agents_where_things_are(sdlc_repo):
    proc = run_hook("subagent_start.py", {"agent_id": "a1", "agent_type": "sdlc:lld",
                                          "cwd": sdlc_repo},
                    env={"CLAUDE_PLUGIN_ROOT": "/plug"})
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    ctx = out["additionalContext"]
    assert out["hookEventName"] == "SubagentStart" and len(ctx.splitlines()) <= 8
    assert "$SDLC=/plug/scripts/sdlc_next.py" in ctx and 'python3 "$SDLC"' in ctx
    assert "docRoot=docs/sdlc" in ctx and "/plug/references" in ctx
    assert 'SDLC-RESULT: {"issue": <n>' in ctx


def test_subagent_start_ignores_other_agents_and_repos(sdlc_repo, plain_repo):
    for payload in ({"agent_type": "general-purpose", "cwd": sdlc_repo},
                    {"agent_type": "sdlc:lld", "cwd": plain_repo}):
        assert run_hook("subagent_start.py", payload).stdout == ""


# --- metrics (SubagentStop / SessionEnd) ------------------------------------------------

def _usage(inp, out, write=0, read=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read}


def _assistant(msg_id, model, usage, text="", ts="2026-09-18T10:00:10Z", sidechain=False):
    return {"type": "assistant", "timestamp": ts, "isSidechain": sidechain,
            "message": {"id": msg_id, "role": "assistant", "model": model, "usage": usage,
                        "content": [{"type": "text", "text": text}]}}


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(path)


RESULT = 'SDLC-RESULT: {"issue": 13, "stage": "development", "outcome": "done"}'


def _agent_transcript(tmp_path, final=RESULT, prompt="ROLE: development ISSUE: 13 EPIC: 9\nGo"):
    path = _write_jsonl(tmp_path / "s" / "subagents" / "agent-a1.jsonl", [
        {"type": "user", "timestamp": "2026-09-18T10:00:00Z",
         "message": {"role": "user", "content": prompt}},
        # One API message over two lines: counted once, at its largest usage.
        _assistant("m1", "claude-opus-5", _usage(1000, 5, 4000, 100_000)),
        _assistant("m1", "claude-opus-5", _usage(1000, 2000, 4000, 100_000)),
        {"type": "user", "timestamp": "2026-09-18T10:01:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]}},
        _assistant("m2", "claude-sonnet-5", _usage(10, 100), final, ts="2026-09-18T10:02:30Z"),
        _assistant("m3", "<synthetic>", _usage(0, 0), ts="2026-09-18T10:02:30Z"),
    ])
    (tmp_path / "s" / "subagents" / "agent-a1.meta.json").write_text(json.dumps(
        {"agentType": "sdlc:development", "toolUseId": "tu1", "spawnDepth": 1}))
    return path


def _records(data_dir, repo="o__r"):
    path = os.path.join(data_dir, "metrics", f"{repo}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]


def _stop_payload(cwd, transcript, agent_type="sdlc:development", **extra):
    return {"agent_id": "a1", "agent_type": agent_type, "agent_transcript_path": transcript,
            "session_id": "sess", "cwd": cwd, "stop_hook_active": False, **extra}


def test_subagent_stop_records_metrics(tmp_path, sdlc_repo):
    data = str(tmp_path / "data")
    proc = run_hook("subagent_stop.py", _stop_payload(sdlc_repo, _agent_transcript(tmp_path)),
                    env={"CLAUDE_PLUGIN_DATA": data})
    assert proc.returncode == 0, proc.stderr
    [rec] = _records(data)
    assert rec["tokens"] == {
        "claude-opus-5": {"input": 1000, "output": 2000, "cache_write": 4000, "cache_read": 100_000},
        "claude-sonnet-5": {"input": 10, "output": 100, "cache_write": 0, "cache_read": 0}}
    # opus: 5*(1000 + 1.25*4000 + 0.1*100000)/1e6 + 25*2000/1e6; sonnet: 3*10/1e6 + 15*100/1e6
    assert rec["est_cost_usd"] == 0.1315
    assert rec["turns"] == 2 and rec["duration_s"] == 150.0
    assert (rec["issue"], rec["stage"], rec["outcome"], rec["epic"]) == (13, "development", "done", 9)
    assert rec["role"] == "development" and rec["session_id"] == "sess" and rec["repo"] == "o/r"
    assert rec["parent"] == {"spawn_depth": 1, "tool_use_id": "tu1"}


def test_metrics_record_counts_tool_calls_peak_context_and_the_run(tmp_path, sdlc_repo):
    data, runs = str(tmp_path / "data"), tmp_path / "runs"
    _run_state(runs, "sess")
    transcript = _agent_transcript(tmp_path)
    with open(transcript, "a") as f:
        for tool_id in ("t1", "t2", "t1"):  # a streamed tool_use can repeat across lines
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-09-18T10:02:00Z",
                                "message": {"id": "m4", "role": "assistant", "model": "claude-opus-5",
                                            "usage": _usage(50, 1),
                                            "content": [{"type": "tool_use", "id": tool_id}]}}) + "\n")
        f.write(json.dumps(_assistant("m5", "claude-sonnet-5", _usage(1, 1), RESULT,
                                      ts="2026-09-18T10:02:30Z")) + "\n")
    run_hook("subagent_stop.py", _stop_payload(sdlc_repo, transcript),
             env={"CLAUDE_PLUGIN_DATA": data, "SDLC_RUNS_DIR": str(runs)})
    [rec] = _records(data)
    assert rec["tool_calls"] == 2
    assert rec["peak_context"] == 1000 + 4000 + 100_000  # m1's input + cache write + cache read
    assert rec["run_id"] == "r-1"


def test_subagent_stop_records_non_sdlc_agents_without_a_result(tmp_path, sdlc_repo):
    data = str(tmp_path / "data")
    transcript = _agent_transcript(tmp_path, final="found nothing", prompt="AXIS: flows\nTrace")
    run_hook("subagent_stop.py", _stop_payload(sdlc_repo, transcript, "general-purpose"),
             env={"CLAUDE_PLUGIN_DATA": data})
    [rec] = _records(data)
    assert rec["role"] == "general-purpose" and "outcome" not in rec and "epic" not in rec


def test_subagent_stop_records_nothing_when_it_blocks_or_outside_sdlc(tmp_path, sdlc_repo, plain_repo):
    data = str(tmp_path / "data")
    blocked = run_hook("subagent_stop.py",
                       _stop_payload(sdlc_repo, _agent_transcript(tmp_path, final="waiting")),
                       env={"CLAUDE_PLUGIN_DATA": data})
    assert blocked.returncode == 2
    run_hook("subagent_stop.py", _stop_payload(plain_repo, _agent_transcript(tmp_path)),
             env={"CLAUDE_PLUGIN_DATA": data})
    assert not os.path.exists(os.path.join(data, "metrics"))


def _run_state(runs_dir, session, epic=9, **extra):
    runs_dir.mkdir(exist_ok=True)
    (runs_dir / f"epic-{epic}.json").write_text(json.dumps(
        {"run_id": "r-1", "terminal": [11], "epic": epic, "session_id": session,
         "in_flight": {"13": "development", "14": "pr-review"}, **extra}))


def test_session_end_records_the_orchestrator_main_thread_only(tmp_path, sdlc_repo):
    data, runs = str(tmp_path / "data"), tmp_path / "runs"
    _run_state(runs, "sess")
    transcript = _write_jsonl(tmp_path / "sess.jsonl", [
        {"type": "user", "timestamp": "2026-09-18T09:00:00Z",
         "message": {"role": "user", "content": "/sdlc:run 9"}},
        _assistant("o1", "claude-sonnet-5", _usage(100, 200), ts="2026-09-18T09:00:05Z"),
        _assistant("o1", "claude-sonnet-5", _usage(100, 200), ts="2026-09-18T09:00:06Z"),
        _assistant("x1", "claude-opus-5", _usage(9999, 9999), sidechain=True),
        _assistant("o2", "claude-opus-5", _usage(1, 1), ts="2026-09-18T09:10:00Z"),
    ])
    proc = run_hook("session_end.py", {"session_id": "sess", "transcript_path": transcript,
                                       "cwd": sdlc_repo, "reason": "exit"},
                    env={"CLAUDE_PLUGIN_DATA": data, "SDLC_RUNS_DIR": str(runs)})
    assert proc.returncode == 0, proc.stderr
    [rec] = _records(data)
    assert rec["agent_type"] == rec["role"] == "orchestrator" and rec["epic"] == 9
    assert rec["models"] == ["claude-opus-5", "claude-sonnet-5"] and rec["turns"] == 2
    assert rec["tokens"]["claude-sonnet-5"]["output"] == 200
    assert rec["tokens"]["claude-opus-5"]["input"] == 1  # the sidechain line is not counted
    assert rec["duration_s"] == 600.0


def test_session_end_noop_outside_sdlc_repo(tmp_path, plain_repo):
    transcript = _write_jsonl(tmp_path / "sess.jsonl", [_assistant("o1", "m", _usage(1, 1))])
    run_hook("session_end.py", {"session_id": "s", "transcript_path": transcript,
                                "cwd": plain_repo}, env={"CLAUDE_PLUGIN_DATA": str(tmp_path / "d")})
    assert not os.path.exists(tmp_path / "d")


# --- SessionStart: compaction recovery and the orchestrator model --------------------------

def _session_start(cwd, tmp_path, **payload):
    proc = run_hook("session_start.py", {"cwd": cwd, "session_id": "sess", **payload},
                    env={"CLAUDE_ENV_FILE": str(tmp_path / "env.sh"), "GITHUB_TOKEN": "ghp_x",
                         "SDLC_RUNS_DIR": str(tmp_path / "runs")})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout else ""


def test_compaction_restates_the_run_being_driven(tmp_path, sdlc_repo):
    _run_state(tmp_path / "runs", "sess")
    _run_state(tmp_path / "runs", "another-session", epic=77)
    ctx = [l for l in _session_start(sdlc_repo, tmp_path, source="compact").splitlines()
           if "rtk" not in l]
    assert len(ctx) <= 5
    text = "\n".join(ctx)
    assert "#9" in text and "r-1" in text and "#13 (development)" in text and "#14 (pr-review)" in text
    assert "#11" in text and "#77" not in text and "next-action" in text


def test_compaction_injects_nothing_without_run_state(tmp_path, sdlc_repo):
    _run_state(tmp_path / "runs", "another-session")
    assert "compacted" not in _session_start(sdlc_repo, tmp_path, source="compact")
    _run_state(tmp_path / "runs", "sess")
    assert "compacted" not in _session_start(sdlc_repo, tmp_path, source="startup")


@pytest.mark.parametrize("model,hint", [("claude-opus-5", True), ("claude-sonnet-5", False),
                                        ({"id": "claude-opus-5[1m]"}, True), (None, False)])
def test_session_start_flags_an_off_policy_orchestrator_model(tmp_path, sdlc_repo, model, hint):
    ctx = _session_start(sdlc_repo, tmp_path, source="startup", model=model)
    assert ("sdlc-run <n>" in ctx) is hint


# --- bin/sdlc-run -----------------------------------------------------------------------

SDLC_RUN = os.path.join(os.path.dirname(HOOKS), "bin", "sdlc-run")


def _sdlc_run(tmp_path, *args):
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "claude").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (fake / "claude").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.path.dirname(sys.executable)}:/usr/bin:/bin"}
    return subprocess.run([SDLC_RUN, *args], capture_output=True, text=True, env=env, timeout=10)


def test_sdlc_run_starts_claude_on_the_policy_orchestrator_model(tmp_path):
    with open(os.path.join(HOOKS, "model_policy.json")) as f:
        model = json.load(f)["orchestrator"]["model"]
    proc = _sdlc_run(tmp_path, "9", "--permission-mode", "auto")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["--model", model, "/sdlc:run 9", "--permission-mode", "auto"]


def test_sdlc_run_needs_a_number(tmp_path):
    proc = _sdlc_run(tmp_path)
    assert proc.returncode == 2 and "usage: sdlc-run" in proc.stderr


def test_skill_frontmatter_model_matches_the_orchestrator_policy():
    with open(os.path.join(HOOKS, "model_policy.json")) as f:
        model = json.load(f)["orchestrator"]["model"]
    with open(os.path.join(os.path.dirname(HOOKS), "skills", "run", "SKILL.md")) as f:
        assert f"\nmodel: {model}\n" in f.read().split("\n---", 1)[0] + "\n"


def test_agent_frontmatter_models_match_the_policy():
    with open(os.path.join(HOOKS, "model_policy.json")) as f:
        models = json.load(f)["models"]
    agents = os.path.join(os.path.dirname(HOOKS), "agents")
    for name in os.listdir(agents):
        role = name[:-len(".md")]
        with open(os.path.join(agents, name)) as f:
            head = f.read().split("\n---", 1)[0]
        want = models["arch-review"] if role == "design-review" else models[role]
        assert f"\nmodel: {want}" in head, role
