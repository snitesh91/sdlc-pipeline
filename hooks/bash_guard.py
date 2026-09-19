"""PreToolUse(Bash): deny hand-run pipeline mutations the control plane owns, and
limit each `sdlc:<role>` agent to its role's control-plane commands.

Only the leading command words of each shell segment (command substitutions and
`sh -c`/`eval` scripts included) are inspected, so argument text never triggers a deny.

The main thread is guarded only while it drives a run (see `main_thread_guarded`); an
`sdlc:<role>` agent is always guarded.
"""
import os
import re
import shlex

from _common import emit, load_json, read_input, repo_config, run, run_states, sdlc_role

SDLC = 'python3 "$SDLC"'
# Wrapper -> its options that consume the next word; `timeout` also takes a duration.
WRAPPER_OPTS_WITH_VALUE = {
    "sudo": {"-u", "--user", "-g", "--group", "-p", "--prompt", "-a", "--auth-type", "-C",
             "--close-from", "-c", "--login-class", "-D", "--chdir", "-r", "--role", "-t",
             "--type", "-U", "--other-user", "-T", "--command-timeout", "-R", "--chroot"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "xargs": {"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-J", "-L", "-n",
              "--max-args", "-P", "--max-procs", "-R", "-S", "-s", "--max-chars",
              "--process-slot-var"},
    "env": {"-u", "--unset", "-C", "--chdir"},
    "exec": {"-a"},
}
WRAPPERS = {"command", "builtin", "nohup", "time", "rtk", *WRAPPER_OPTS_WITH_VALUE}
RESERVED = {"!", "{", "if", "then", "elif", "else", "do", "while", "until"}
SHELLS = {"sh", "bash", "zsh", "dash"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC_RE = re.compile(r"<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)(?P<delim>[A-Za-z0-9_]+)(?P=q)")
SEPARATOR_CHARS = set("&|;()")
MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
GH_SUB_ALIASES = {"new": "create"}
# Any of these without an explicit method makes `gh api` send a POST.
GH_API_BODY_OPTS = ("-f", "-F", "--field", "--raw-field", "--input")
GH_API_OPTS_WITH_VALUE = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F",
                          "--field", "--input", "-q", "--jq", "-t", "--template",
                          "--hostname", "--cache", "-p", "--preview"}

# Control-plane commands a stage agent may run; everything else is the orchestrator's.
READ_ONLY_COMMANDS = {"show-config", "lld-section", "pairing-counts", "pr-checks", "cite",
                      "verify-citations", "audit-issues", "check-gate",
                      "check-initiative-closeable"}
ROLE_COMMANDS = {
    # `resolve-thread`: a gate-feedback round replies on the stage's own gate PR threads.
    "product": {"post-comment", "resolve-thread"},
    "architecture": {"post-comment", "resolve-thread"},
    "lld": {"post-comment"},
    "product-review": {"record-design-review"},
    "design-review": {"record-design-review"},
    "development": {"open-dev-pr", "record-local-ci", "handoff-to-pr-review"},
    "pr-review": {"record-pr-review", "record-local-ci"},
    "initiative-close": {"record-initiative-verification"},
}
REVIEW_ROLES = {"product-review", "design-review", "pr-review"}
CONTROL_PLANE_REFS = {"$SDLC", "${SDLC}"}

# A run's state file is written by `next-action --run-id` and never deleted, so "live" means
# written within this window (the orchestrator rewrites it as units start and finish).
RUN_LIVE_SECONDS = 8 * 3600

REASONS = {
    "graphql": f"Hand-run GraphQL is blocked: the control plane owns GitHub reads and writes. Use {SDLC} <command> (next-action, check-gate, pr-checks, resolve-thread, audit-issues, ...).",
    "api-mutation": f"Hand-run GitHub REST mutations are blocked. Use the {SDLC} command that owns the change (set-stage, create-issue, mark-blocked, open-gate, ...).",
    "issue create": f"Use {SDLC} create-issue --parent <n> --title ... --body ... --type T [--priority P] [--effort E] (orchestrator only; stage agents report it in their handoff).",
    "issue edit": f"Issue fields are control-plane-owned: use {SDLC} set-stage / add-blocked-by / mark-blocked. Stage agents report the change in their handoff instead.",
    "issue close": f"Use {SDLC} close-issue <n> [--repo-path <p>] (orchestrator only).",
    "issue reopen": "Reopening an issue is the operator's call; report it (stage agents: outcome needs-human).",
    "pr create": f"Use {SDLC} open-dev-pr (development), {SDLC} open-design-pr or {SDLC} open-gate (orchestrator).",
    "pr merge": f"Use {SDLC} merge-pr <pr> --issue <n>; it is the only code merge gate ({SDLC} merge-design-pr for a phase-Task's design PR; {SDLC} merge-gate <pr> --issue <n> --stage <s> --operator-confirmed for a gate PR, only when the operator explicitly said to merge it).",
    "pr ready": f"Use {SDLC} merge-pr <pr> --issue <n>; it marks the PR ready itself.",
    "pr close": "Closing a pipeline PR is the operator's call; report it instead.",
    "worktree add": f"Use {SDLC} start-stage <n> --role <r> (or worktree-add <n>). Only a detached review worktree (git worktree add --detach) may be made by hand.",
    "push force": "Force-push is blocked: stop and report the rejected push; only the operator may overwrite a branch.",
    "rebase": f"Never rebase a pipeline branch: use {SDLC} sync-branch <n> (merges the base).",
    "orchestrator-only": "That control-plane command is the orchestrator's: report it in your "
                         "SDLC-RESULT/handoff; the orchestrator runs it.",
    "post-comment-role": f"post-comment serves only your own stage: pass --role <your role> ({SDLC} post-comment <n> --role <r> --body-file <f>).",
    "review-git-write": "Review roles are read-only on the branch: describe the fix in your "
                        "review; the owning stage applies it.",
}


def flatten(cmd: str) -> str:
    """Turn unquoted newlines into ';', drop comments, continuations and heredoc bodies,
    and split every `$(...)`/backtick substitution (even inside "...") into its own segment."""
    out, pending, quote, i, n = [], [], None, 0, len(cmd)
    parens = []  # per open "(" or "`": (quote to restore, closer, is a substitution)
    while i < n:
        c = cmd[i]
        opens_sub = c == "`" or cmd.startswith("$(", i)
        if quote == "'" or (quote == '"' and not opens_sub):
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if parens and c == parens[-1][1]:
            restore, _, is_sub = parens.pop()
            out.append((';"' if restore else ";") if is_sub else c)
            quote = restore
            i += 1
            continue
        if opens_sub:
            parens.append((quote, "`" if c == "`" else ")", True))
            out.append('";' if quote else ";")
            quote = None
            i += 1 if c == "`" else 2
            continue
        if c == "(":
            parens.append((None, ")", False))
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":
                out.append(c + cmd[i + 1])
            i += 2
            continue
        elif c == "#" and (not out or out[-1][-1:] in " \t\n;&|()"):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        elif c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = HEREDOC_RE.match(cmd, i)
            if m:
                pending.append((m.group("delim"), bool(m.group("dash"))))
                out.append(" ")
                i = m.end()
                continue
        elif c == "\n":
            out.append(";")
            i += 1
            for delim, dash in pending:
                while i < n:
                    j = cmd.find("\n", i)
                    j = n if j == -1 else j
                    line, i = cmd[i:j], j + 1
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out)


def segments(cmd: str) -> list:
    lex = shlex.shlex(flatten(cmd), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    segs, cur = [], []
    for tok in lex:
        if tok and set(tok) <= SEPARATOR_CHARS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _takes_next(opt: str, with_value: set) -> bool:
    """Whether option word `opt` consumes the next word (`-u x`, and `-Eu x` clusters)."""
    if opt.startswith("--"):
        return opt in with_value
    for j in range(1, len(opt)):
        if "-" + opt[j] in with_value:
            return j == len(opt) - 1  # otherwise the value is attached: `-uroot`
    return False


def leading_words(seg: list) -> list:
    i = 0
    while i < len(seg):
        tok = seg[i]
        name = os.path.basename(tok)
        if ASSIGN_RE.match(tok) or tok in RESERVED:
            i += 1
        elif name in WRAPPERS:
            with_value = WRAPPER_OPTS_WITH_VALUE.get(name, set())
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if _takes_next(seg[i], with_value) else 1
            if name == "timeout":
                i += 1  # the duration
        else:
            break
    words = seg[i:]
    return [os.path.basename(words[0])] + words[1:] if words else []


def _positionals(args: list, opts_with_value: set) -> list:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if a in opts_with_value else 1
        else:
            out.append(a)
            i += 1
    return out


def check_gh(args: list):
    pos = _positionals(args, {"-R", "--repo"} | GH_API_OPTS_WITH_VALUE)
    if not pos:
        return None
    group = pos[0]
    if group == "api":
        endpoint = pos[1] if len(pos) > 1 else ""
        if endpoint == "graphql":
            return "graphql"
        method, has_body = None, False
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1]
            elif a.startswith("--method="):
                method = a.split("=", 1)[1]
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:]
            elif a.startswith(GH_API_BODY_OPTS):
                has_body = True
        method = method or ("POST" if has_body else "GET")
        return "api-mutation" if method.upper() in MUTATING_METHODS else None
    sub = GH_SUB_ALIASES.get(pos[1], pos[1]) if len(pos) > 1 else ""
    if group == "issue" and sub in ("create", "edit", "close", "reopen"):
        return f"issue {sub}"
    if group == "pr" and sub in ("create", "merge", "ready", "close"):
        return f"pr {sub}"
    return None


def _git_sub(args: list):
    """(subcommand, its args) after git's global options; (None, []) when there is none."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_OPTS_WITH_VALUE else 1
    return (args[i], args[i + 1:]) if i < len(args) else (None, [])


def check_git(args: list):
    sub, rest = _git_sub(args)
    if sub == "rebase":
        return "rebase"
    if sub == "pull" and any(a in ("--rebase", "-r") or a.startswith("--rebase=") for a in rest):
        if not any(a in ("--rebase=false", "--no-rebase") for a in rest):
            return "rebase"
    if sub == "worktree" and rest[:1] == ["add"]:
        if not any(a in ("--detach", "-d") for a in rest):
            return "worktree add"
    if sub == "push":
        for a in rest:
            if a in ("--force", "--force-with-lease", "--force-if-includes") or a.startswith("--force-with-lease="):
                return "push force"
            if a.startswith("-") and not a.startswith("--") and "f" in a[1:]:
                return "push force"
            if a.startswith("+"):
                return "push force"
    return None


def control_plane_command(words: list):
    """The sdlc_next.py subcommand a segment runs ("" for none given), or None."""
    i = 1 if words[0].startswith("python") else 0
    while 0 < i < len(words) and words[i].startswith("-"):
        i += 1
    if i >= len(words) or not (words[i] in CONTROL_PLANE_REFS
                               or words[i].endswith("sdlc_next.py")):
        return None
    return next((w for w in words[i + 1:] if not w.startswith("-")), "")


def _flag_value(words: list, flag: str):
    """The value of `--flag v` / `--flag=v` in `words`, or None."""
    for i, w in enumerate(words):
        if w == flag and i + 1 < len(words):
            return words[i + 1]
        if w.startswith(flag + "="):
            return w.split("=", 1)[1]
    return None


def check_role(words: list, role: str):
    """A stage agent's limits: its role's control-plane commands; reviewers never write git."""
    cmd = control_plane_command(words)
    if cmd and cmd not in READ_ONLY_COMMANDS | ROLE_COMMANDS.get(role, set()):
        return "orchestrator-only"
    if cmd == "post-comment" and _flag_value(words, "--role") != role:
        return "post-comment-role"
    if role in REVIEW_ROLES and words[0] == "git":
        sub, rest = _git_sub(words[1:])
        if (sub in ("commit", "push", "merge") or (sub == "reset" and "--hard" in rest)
                or (sub == "checkout" and "--" in rest)):
            return "review-git-write"
    return None


def verdict(command: str, role: str = ""):
    """The REASONS key for the first denied segment, or None to allow.
    `role` is the calling `sdlc:<role>` agent's role ("" on the main thread)."""
    for seg in segments(command):
        words = leading_words(seg)
        if not words:
            continue
        if words[0] == "gh":
            key = check_gh(words[1:])
        elif words[0] == "git":
            key = check_git(words[1:])
        elif words[0] == "eval":
            key = verdict(" ".join(words[1:]), role)
        elif words[0] in SHELLS:
            script = next((words[j + 1] for j in range(1, len(words) - 1)
                           if re.fullmatch(r"-[a-z]*c[a-z]*", words[j])), None)
            key = verdict(script, role) if script else None
        else:
            key = None
        if not key and role:
            key = check_role(words, role)
        if key:
            return key
    return None


def main_thread_guarded(config: dict, session_id: str) -> bool:
    """Whether the main thread's hand-run mutations are denied: only while this session
    drives a run, i.e. it has a fresh run-state file. `guard.mainThread: "always"` guards
    every session; an unknown session id (no run can be matched) stays guarded."""
    if (config.get("guard") or {}).get("mainThread") == "always" or not session_id:
        return True
    return bool(run_states(config, session_id, max_age=RUN_LIVE_SECONDS))


def main() -> int:
    data = read_input()
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command")
    config_path = repo_config(data.get("cwd") or os.getcwd())
    if not isinstance(command, str) or not config_path:
        return 0
    role = sdlc_role(data.get("agent_type"))
    if not role and not main_thread_guarded(load_json(config_path),
                                            str(data.get("session_id") or "")):
        return 0
    try:
        key = verdict(command, role)
    except ValueError:  # unbalanced quotes etc. -- let the shell report it
        return 0
    if key:
        emit("PreToolUse", permissionDecision="deny",
             permissionDecisionReason="sdlc guard: " + REASONS[key])
    return 0


if __name__ == "__main__":
    run(main)
