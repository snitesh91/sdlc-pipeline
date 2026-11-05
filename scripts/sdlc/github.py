"""GitHub work-item providers (GraphQL via gh, REST) and their query templates."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, runtime_checkable

from .config import (
    EFFORT_FIELD_ID, EFFORT_OPTION_IDS, ISSUE_TYPE_IDS, PIPELINE, PIPELINE_STATUS_FIELD_ID,
    PIPELINE_STATUS_FIELD_NAMES, PIPELINE_STATUS_OPTION_IDS, PRIORITY_FIELD_ID,
    PRIORITY_OPTION_IDS, REPO, Runner, STAGE_FIELD_ID, STAGE_FIELD_NAMES, STAGE_OPTION_IDS,
    _NAME, _OWNER, _PF,
)


class GhError(RuntimeError):
    pass


def _utc_now_marker() -> str:
    """UTC timestamp in the format every marker comment uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _may_move_origin(argv: list) -> bool:
    """A push, a refspec fetch, or any GitHub write (a merge moves a branch server-side)."""
    if argv[:1] == ["git"]:
        return "push" in argv or ("fetch" in argv[3:4] and len(argv) > 5)
    if argv[:2] == ["gh", "api"]:
        if "-X" in argv[:-1]:
            return argv[argv.index("-X") + 1].upper() != "GET"
        return any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in argv)
    return argv[:1] == ["gh"] and argv[2:3] not in (["view"], ["list"], ["checks"], ["diff"],
                                                     ["status"])


def _default_runner(argv: list) -> str:
    if _may_move_origin(argv):
        _PUSH_GENERATION[0] += 1
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}")
    return result.stdout


def _is_real_runner(runner) -> bool:
    """`_default_runner`, or a provider's tracking wrapper around it (not a test fake)."""
    return runner is _default_runner or getattr(runner, "real", False)


@runtime_checkable
class WorkItemProvider(Protocol):
    """Work-item-tracker contract every `cmd_*` function is written against;
    `GitHubRest` (the default) and `GitHub` (GraphQL) implement it. Issue management
    only -- PR/branch/CI mechanics are deliberately out of scope."""

    def issue_list(self) -> list: ...
    def issue_view(self, number: int) -> dict: ...
    def issue_edit(self, number: int, add_labels: list = (), remove_labels: list = (),
                    add_assignees: list = (), remove_assignees: list = ()) -> None: ...
    def issue_node_id(self, number: int) -> str: ...
    def issue_fields(self, number: int) -> dict: ...
    def issue_epic_info(self, number: int) -> dict: ...
    def issue_create(self, title: str, body: str, labels: list) -> int: ...
    def issue_comment(self, number: int, body: str) -> None: ...
    def issue_close(self, number: int, reason: str = "completed") -> None: ...
    def issue_set_body(self, number: int, body: str) -> None: ...
    def issue_set_title(self, number: int, title: str) -> None: ...
    def blocked_by(self, number: int) -> list: ...
    def add_blocked_by(self, issue_number: int, blocking_number: int) -> None: ...
    def remove_blocked_by(self, issue_number: int, blocking_number: int) -> None: ...
    def blocking(self, number: int) -> list: ...
    def set_issue_type(self, number: int, type_name: str) -> None: ...
    def set_stage_field(self, number: int, stage: str) -> None: ...
    def set_pipeline_status_field(self, number: int, status: str) -> None: ...
    def set_priority_field(self, number: int, priority: str) -> None: ...
    def set_effort_field(self, number: int, effort: str) -> None: ...
    def clear_stage_and_status_fields(self, number: int) -> None: ...
    def clear_stage_field(self, number: int) -> None: ...
    def add_sub_issue(self, parent_number: int, child_number: int) -> None: ...
    def remove_sub_issue(self, parent_number: int, child_number: int) -> None: ...
    def ensure_label(self, label: str) -> None: ...

    def classify_unit(self, number: int) -> str:
        """"initiative", "epic", "task" or "other" for `number`."""
        ...


class GitHub:
    """The only place gh/GraphQL subprocess calls happen. Inject a fake `runner` in tests."""

    def __init__(self, repo: str = REPO, runner: Runner = _default_runner):
        self.repo = repo
        self._run = runner

    def issue_list(self) -> list:
        """Every issue, open and closed, with labels, fields, `issueType` and `parent`
        flattened. GraphQL because `gh issue list --json` exposes neither of the last two."""
        issues = []
        after = "null"
        while True:
            data = self.graphql(_ISSUE_LIST_QUERY.format(after=after))
            conn = data["repository"]["issues"]
            for node in conn["nodes"]:
                node["labels"] = node["labels"]["nodes"]
                node["milestone"] = (node.get("milestone") or {}).get("title")
                node["fields"] = {
                    v["field"]["name"]: v["name"]
                    for v in node.pop("issueFieldValues")["nodes"]
                    if v["__typename"] == "IssueFieldSingleSelectValue"
                }
                issues.append(node)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = f'"{conn["pageInfo"]["endCursor"]}"'
        return without_retro_issues(issues)

    def issue_view(self, number: int) -> dict:
        out = self._run(["gh", "issue", "view", str(number), "--repo", self.repo,
                          "--json", "number,title,labels,body,state,comments"])
        return json.loads(out)

    def issue_edit(self, number: int, add_labels: list = (), remove_labels: list = (),
                    add_assignees: list = (), remove_assignees: list = ()):
        if not add_labels and not remove_labels and not add_assignees and not remove_assignees:
            return
        argv = ["gh", "issue", "edit", str(number), "--repo", self.repo]
        for l in add_labels:
            argv += ["--add-label", l]
        for l in remove_labels:
            argv += ["--remove-label", l]
        for a in add_assignees:
            argv += ["--add-assignee", a]
        for a in remove_assignees:
            argv += ["--remove-assignee", a]
        self._run(argv)

    def issue_node_id(self, number: int) -> str:
        data = self.graphql(_ISSUE_NODE_ID_QUERY.format(n=number))
        return data["repository"]["issue"]["id"]

    def issue_fields(self, number: int) -> dict:
        """One issue's single-select field values, name -> value (e.g. {"Stage": "LLD"})."""
        data = self.graphql(_ISSUE_FIELDS_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["issueFieldValues"]["nodes"]
        return {n["field"]["name"]: n["name"] for n in nodes if n["__typename"] == "IssueFieldSingleSelectValue"}

    def issue_epic_info(self, number: int) -> dict:
        """One issue's issueType/parent/labels (labels flattened), for classification."""
        data = self.graphql(_ISSUE_EPIC_CHECK_QUERY.format(n=number))
        node = data["repository"]["issue"]
        node["labels"] = node["labels"]["nodes"]
        return node

    def classify_unit(self, number: int) -> str:
        """Classify one issue by number. A caller already holding `issue_list()` dicts
        uses `classify_unit_from_issue` directly to avoid a fetch per issue."""
        return classify_unit_from_issue(self.issue_epic_info(number))

    def blocked_by(self, number: int) -> list:
        """Open issue numbers blocking `number` (native blockedBy); closed ones are omitted."""
        data = self.graphql(_BLOCKED_BY_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blockedBy"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def add_blocked_by(self, issue_number: int, blocking_number: int):
        issue_id = self.issue_node_id(issue_number)
        blocking_id = self.issue_node_id(blocking_number)
        self.graphql(_ADD_BLOCKED_BY_MUTATION.format(issue_id=issue_id, blocking_id=blocking_id))

    def remove_blocked_by(self, issue_number: int, blocking_number: int):
        issue_id = self.issue_node_id(issue_number)
        blocking_id = self.issue_node_id(blocking_number)
        self.graphql(_REMOVE_BLOCKED_BY_MUTATION.format(issue_id=issue_id, blocking_id=blocking_id))

    def blocking(self, number: int) -> list:
        """Open issue numbers that `number` blocks (the reverse of `blocked_by`)."""
        data = self.graphql(_BLOCKING_QUERY.format(n=number))
        nodes = data["repository"]["issue"]["blocking"]["nodes"]
        return [n["number"] for n in nodes if n["state"] == "OPEN"]

    def set_issue_type(self, number: int, type_name: str):
        if type_name not in ISSUE_TYPE_IDS:
            raise GhError(
                f"type_name={type_name!r} is not in projectFields.issueTypeIds "
                f"(configured: {sorted(ISSUE_TYPE_IDS)}) -- Epic/Initiative "
                f"types need provisioning as real GitHub Issue Types (org-level) and "
                f"their GraphQL ids added to config before "
                f"this call can set them; a bare KeyError here would hide that.")
        issue_id = self.issue_node_id(number)
        type_id = ISSUE_TYPE_IDS[type_name]
        self.graphql(_SET_ISSUE_TYPE_MUTATION.format(issue_id=issue_id, type_id=type_id))

    def _set_single_select(self, number: int, field_id: str, option_id: str):
        issue_id = self.issue_node_id(number)
        self.graphql(_SET_ISSUE_FIELD_MUTATION.format(
            issue_id=issue_id, field_id=field_id, option_id=option_id))

    def set_stage_field(self, number: int, stage: str):
        """Write the Stage field. Never swallows GhError: an unset Stage reads as a fresh issue."""
        self._set_single_select(number, STAGE_FIELD_ID, STAGE_OPTION_IDS[stage])

    def set_pipeline_status_field(self, number: int, status: str):
        """Write the Pipeline Status field (`status` like "in-progress"); never swallows GhError."""
        self._set_single_select(number, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS[status])

    def set_priority_field(self, number: int, priority: str):
        self._set_single_select(number, PRIORITY_FIELD_ID, PRIORITY_OPTION_IDS[priority])

    def set_effort_field(self, number: int, effort: str):
        self._set_single_select(number, EFFORT_FIELD_ID, EFFORT_OPTION_IDS[effort])

    def clear_stage_and_status_fields(self, number: int):
        """Delete both Stage and Pipeline Status: an Epic handing off to its Tasks has no
        stage of its own but is not `Done` either."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=PIPELINE_STATUS_FIELD_ID))

    def clear_stage_field(self, number: int):
        """Delete only the Stage value (Pipeline Status untouched)."""
        issue_id = self.issue_node_id(number)
        self.graphql(_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id=issue_id, field_id=STAGE_FIELD_ID))

    def add_sub_issue(self, parent_number: int, child_number: int):
        parent_id = self.issue_node_id(parent_number)
        child_id = self.issue_node_id(child_number)
        self.graphql(_ADD_SUB_ISSUE_MUTATION.format(parent_id=parent_id, child_id=child_id))

    def remove_sub_issue(self, parent_number: int, child_number: int):
        parent_id = self.issue_node_id(parent_number)
        child_id = self.issue_node_id(child_number)
        self.graphql(_REMOVE_SUB_ISSUE_MUTATION.format(parent_id=parent_id, child_id=child_id))

    def issue_comment(self, number: int, body: str):
        self._run(["gh", "issue", "comment", str(number), "--repo", self.repo, "--body", body])

    def issue_close(self, number: int, reason: str = "completed"):
        self._run(["gh", "issue", "close", str(number), "--repo", self.repo,
                   "--reason", reason])

    def issue_set_body(self, number: int, body: str):
        self._run(["gh", "issue", "edit", str(number), "--repo", self.repo, "--body", body])

    def issue_set_title(self, number: int, title: str):
        self._run(["gh", "issue", "edit", str(number), "--repo", self.repo, "--title", title])

    def auto_deletes_head_branches(self) -> bool:
        """The repo's `delete_branch_on_merge` (False when unreadable); cached per process."""
        if not hasattr(self, "_auto_delete_heads"):
            try:
                out = self._run(["gh", "api", f"repos/{self.repo}", "--jq",
                                 ".delete_branch_on_merge"]).strip()
                self._auto_delete_heads = out == "true"
            except GhError:
                self._auto_delete_heads = False
        return self._auto_delete_heads

    def delete_branch(self, branch: str):
        """Delete `origin/<branch>` GitHub-side (REST); a missing ref raises GhError
        (`cleanup_unit` re-checks origin, so an already-gone branch counts as deleted)."""
        self._run(["gh", "api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{branch}"])

    def pr_comment(self, number: int, body: str):
        self._run(["gh", "pr", "comment", str(number), "--repo", self.repo, "--body", body])

    def pr_edit_body(self, number: int, body: str):
        """Replace a PR's description: REST `PATCH pulls/<n>`, so it works through the cloud
        proxy too (`gh pr edit` is GraphQL)."""
        self._run(["gh", "api", "-X", "PATCH", f"repos/{self.repo}/pulls/{number}",
                   "-f", f"body={body}"])

    def issue_create(self, title: str, body: str, labels: list) -> int:
        argv = ["gh", "api", f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        for l in labels:
            argv += ["-f", f"labels[]={l}"]
        out = self._run(argv)
        return json.loads(out)["number"]

    def pr_view(self, number: int, fields: str = "state,mergedAt") -> dict:
        out = self._run(["gh", "pr", "view", str(number), "--repo", self.repo, "--json", fields])
        return json.loads(out)

    def path_on_ref(self, path: str, ref: str = "main") -> bool:
        """Whether `path` exists on `ref`, checked GitHub-side (independent of any local checkout)."""
        try:
            self._run(["gh", "api", f"repos/{self.repo}/contents/{path}?ref={ref}",
                       "--jq", ".sha"])
            return True
        except GhError:
            return False

    def pr_list_for_branch(self, branch: str, state: str = "open") -> list:
        """PRs (default open) whose head branch is exactly `branch`."""
        out = self._run(["gh", "pr", "list", "--repo", self.repo, "--head", branch,
                          "--state", state, "--json", "number,isDraft,headRefName,title,url"])
        return json.loads(out)

    def pr_create(self, base: str, head: str, title: str, body: str, draft: bool = False) -> int:
        argv = ["gh", "pr", "create", "--repo", self.repo, "--base", base, "--head", head,
                "--title", title, "--body", body]
        if draft:
            argv.append("--draft")
        out = self._run(argv)
        url = out.strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def pr_checks(self, number: int) -> list:
        try:
            out = self._run(["gh", "pr", "checks", str(number), "--repo", self.repo,
                              "--json", "name,state,bucket,link,workflow"])
        except GhError as e:
            # gh errors instead of returning [] when no check ever reported; a required
            # workflow that never ran is caught later as missing-checks.
            if "no checks reported" in str(e):
                return []
            raise
        return json.loads(out)

    def job_failed_log(self, job_id: int) -> str:
        """The failed steps' log of one Actions job (`gh run view --job --log-failed`)."""
        return self._run(["gh", "run", "view", "--job", str(job_id), "--log-failed",
                          "--repo", self.repo])

    def run_rerun_failed(self, run_id: int) -> None:
        """Re-run only the failed (and cancelled) jobs of one Actions run."""
        self._run(["gh", "run", "rerun", str(run_id), "--failed", "--repo", self.repo])

    def pr_files(self, number: int) -> list:
        """Every changed path of a PR, via paginated REST `pulls/{n}/files`. `gh pr view
        --json files` caps at 100 and `gh pr diff` 406s past 20k lines; this feeds the
        merge gate, so a truncated list could let code merge without CI."""
        out = self._run(["gh", "api", "--paginate", f"repos/{self.repo}/pulls/{number}/files",
                          "--jq", ".[].filename"])
        return [line for line in out.splitlines() if line.strip()]

    def branch_behind_by(self, head: str, base: str = "main") -> int:
        """Commits `base` has that `head` lacks (REST compare, GitHub-side)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                          "--jq", ".behind_by"])
        return int(out.strip())

    def branch_head_sha(self, branch: str) -> str:
        """Head commit of `branch`, GitHub-side."""
        return self._run(["gh", "api", f"repos/{self.repo}/branches/{branch}",
                          "--jq", ".commit.sha"]).strip()

    def files_since(self, sha: str, branch: str) -> list:
        """Files changed on `branch` since `sha` (REST compare; capped at 300 files)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{sha}...{branch}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def base_delta_files(self, head: str, base: str = "main") -> list:
        """Files `base` changed since its merge-base with `head` (REST compare). GitHub
        returns at most 300 files; callers treat a delta at that cap as truncated."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{head}...{base}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def branch_commits(self, base: str, head: str) -> list:
        """Shas of the commits on `head` that `base` lacks (REST compare, capped at 250)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/compare/{base}...{head}",
                          "--jq", ".commits[]?.sha"])
        return [line for line in out.splitlines() if line.strip()]

    def commit_parents(self, sha: str) -> list:
        """Parent shas of one commit, first parent first."""
        out = self._run(["gh", "api", f"repos/{self.repo}/commits/{sha}",
                          "--jq", ".parents[].sha"])
        return [line for line in out.splitlines() if line.strip()]

    def commit_files(self, sha: str) -> list:
        """Files one commit changed against its first parent (capped at 300)."""
        out = self._run(["gh", "api", f"repos/{self.repo}/commits/{sha}",
                          "--jq", ".files[]?.filename"])
        return [line for line in out.splitlines() if line.strip()]

    def pr_ready(self, number: int):
        self._run(["gh", "pr", "ready", str(number), "--repo", self.repo])

    def pr_convert_to_draft(self, number: int):
        self._run(["gh", "pr", "ready", str(number), "--repo", self.repo, "--undo"])

    def ensure_label(self, label: str):
        """Create `label` in the repo when missing (idempotent)."""
        self._run(["gh", "label", "create", label, "--repo", self.repo, "--force"])

    def pr_add_label(self, number: int, label: str):
        """Add `label` to a PR, creating it in the repo first (idempotent)."""
        self.ensure_label(label)
        self._run(["gh", "pr", "edit", str(number), "--repo", self.repo, "--add-label", label])

    def pr_remove_label(self, number: int, label: str):
        self._run(["gh", "pr", "edit", str(number), "--repo", self.repo, "--remove-label", label])

    def pr_heads_for_branch(self, branch: str) -> list:
        """Every PR (any state) whose head is `branch`: `number`, `state`, `headRefOid`."""
        out = self._run(["gh", "pr", "list", "--repo", self.repo, "--head", branch,
                          "--state", "all", "--json", "number,state,headRefOid"])
        return json.loads(out)

    def open_prs(self) -> list:
        """Every open PR: `number`, `headRefName`, `title`, `isDraft`, `updatedAt`."""
        out = self._run(["gh", "pr", "list", "--repo", self.repo, "--state", "open",
                          "--limit", "500", "--json", "number,headRefName,title,isDraft,updatedAt"])
        return json.loads(out)

    def branch_last_commit_at(self, branch: str) -> Optional[str]:
        """Committer date of `branch`'s head on GitHub (REST); None when the branch is absent."""
        try:
            out = self._run(["gh", "api", f"repos/{self.repo}/branches/{branch}",
                             "--jq", ".commit.commit.committer.date"])
        except GhError:
            return None
        return out.strip() or None

    def pr_merge(self, number: int, delete_branch: bool = False, method: str = "squash",
                 match_head: Optional[str] = None):
        """`match_head` makes GitHub refuse the merge unless the head is still that SHA.
        Branch deletion is `cleanup_unit`'s: gh's `--delete-branch` fails while a local
        worktree holds the branch, leaving it on origin."""
        argv = ["gh", "pr", "merge", str(number), "--repo", self.repo, f"--{method}"]
        if delete_branch:
            argv.append("--delete-branch")
        if match_head:
            argv += ["--match-head-commit", match_head]
        self._run(argv)

    def unresolved_review_threads(self, pr_number: int) -> list:
        """The PR's unresolved review threads: `id`, `isResolved`, `comments.nodes[]`
        (`body`, `author.login`)."""
        data = self.graphql(_UNRESOLVED_THREADS_QUERY.format(pr=pr_number))
        nodes = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        return [n for n in nodes if not n["isResolved"]]

    def reply_review_thread(self, thread_id: str, body: str):
        self.graphql(_REPLY_REVIEW_THREAD_MUTATION, threadId=thread_id, body=body)

    def resolve_review_thread(self, thread_id: str):
        self.graphql(_RESOLVE_REVIEW_THREAD_MUTATION, threadId=thread_id)

    def graphql(self, query: str, **variables: str) -> dict:
        """Run `query`; `variables` pass as GraphQL string variables (no escaping needed)."""
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            argv += ["-f", f"{name}={value}"]
        return json.loads(self._run(argv))["data"]

    def issue_comment_edit(self, comment: dict, body: str):
        """Replace the body of `comment` (an `issue_view` comment)."""
        self.graphql("mutation($id: ID!, $body: String!) { updateIssueComment(input: "
                     "{id: $id, body: $body}) { issueComment { id } } }",
                     id=comment["id"], body=body)


# The Claude Code cloud proxy appends this to every text it writes to GitHub.
_PROXY_FOOTER_RE = re.compile(
    r"\s*\r?\n---\r?\n_Generated by \[Claude Code\]\(https://claude\.ai/code\)_\s*\Z")


# gh's `pr checks` bucket per check state (conclusion when completed, else status).
_CHECK_BUCKETS = {"SUCCESS": "pass", "SKIPPED": "skipping", "NEUTRAL": "skipping",
                  "FAILURE": "fail", "ERROR": "fail", "TIMED_OUT": "fail",
                  "ACTION_REQUIRED": "fail", "STARTUP_FAILURE": "fail", "CANCELLED": "cancel"}


_STAGE_OPTION_NAMES = {slug: display for display, slug in STAGE_FIELD_NAMES.items()}


_STATUS_OPTION_NAMES = {slug: display for display, slug in PIPELINE_STATUS_FIELD_NAMES.items()}


# `check-gate` thread ids in REST mode: the PR and the thread's first comment.
_REST_THREAD_ID_RE = re.compile(r"^ccr:(\d+):([^:]+)$")


def strip_proxy_footer(text: Optional[str]) -> Optional[str]:
    """`text` without the cloud proxy's trailing "Generated by Claude Code" footer."""
    return _PROXY_FOOTER_RE.sub("", text) if text else text


def _number_from_url(url: Optional[str]) -> Optional[int]:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


# Upper bound on pages `GitHubRest._api_items` walks (100 items each).
_REST_MAX_PAGES = 200


# Refetches of a short page whose next page still has items (a transient GitHub glitch).
_REST_SHORT_PAGE_RETRIES = 3


# Concurrent GitHub reads per command: enough to hide latency, well under REST's secondary
# rate limit (100 concurrent requests).
_READ_WORKERS = 6


def _parallel(fn: Callable, items) -> list:
    """`[fn(x) for x in items]`, run on up to `_READ_WORKERS` threads; order kept and the
    first exception re-raised, as the serial loop would."""
    items = list(items)
    if len(items) < 2:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


_ISSUE_KEYS = ("id", "node_id", "number", "title", "body", "created_at", "updated_at", "state", "labels",
               "type", "parent", "parent_issue_url", "milestone", "issue_field_values",
               "sub_issues_summary")


def _slim_issue(raw: dict) -> dict:
    return {k: raw[k] for k in _ISSUE_KEYS if k in raw}


def _raw_parent(raw: dict) -> Optional[int]:
    return (raw.get("parent") or {}).get("number") or _number_from_url(raw.get("parent_issue_url"))


# Issue numbers the running command names (`main` sets it): `GitHubRest.issue_list` reads
# them, and their sub-issue trees, even when closed.
_FOCUS: set = set()


# Which open parents' closed sub-issues `GitHubRest.issue_list` reads (`main` sets it per
# command): "all" (with `_FOCUS`, only the trees holding those numbers), "childless" (only
# parents with no open child: every child closed), or "none".
_CLOSED_WALK = ["all"]


def focus_issues(gh, numbers) -> list:
    """`gh.issue_list()` that also holds `numbers` (and their parent chains): a unit-scoped
    index reloads once when one of them is missing (a closed Epic, a closed `Realises:` issue,
    a finished unit's leftover branch). A whole-repo provider lists them anyway."""
    numbers = {n for n in numbers if n}
    new = numbers - _FOCUS
    _FOCUS.update(numbers)
    issues = gh.issue_list()
    if new and new - {i["number"] for i in issues} and hasattr(gh, "_stale"):
        gh._stale = True
        issues = gh.issue_list()
    return issues


class GitHubRest(GitHub):
    """The GitHub client: REST for everything public REST covers, and `_no_rest_path` for the
    rest. Reads strip the cloud proxy's footer; config field ids map to REST ids and option
    names through the org's fields. Inherited methods are REST already."""

    def __init__(self, repo: str = REPO, runner: Runner = _default_runner):
        super().__init__(repo, runner)
        self._owner = repo.split("/", 1)[0]
        self._org_fields: Optional[list] = None
        # issue_list() index: raw REST issues by number, refreshed per issue this process
        # mutates (`_dirty`), or wholesale (`_stale`) after a mutation it cannot attribute.
        self._index: Optional[dict] = None
        self._dirty: set = set()
        self._stale = False
        self._sub_issue_child = False  # add/remove_sub_issue mark the child themselves
        self._issues: dict = {}  # `_issue` reads, dropped on any write
        self._prs: dict = {}  # `_prs_for_branch` reads, dropped on any write
        self._run = self._tracked(runner)

    _MUTATING_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")
    _READ_VERBS = ("view", "list", "diff", "checks", "status")

    def _tracked(self, runner: Runner) -> Runner:
        def run(argv: list) -> str:
            if _may_move_origin(argv):
                self._issues.clear()
                self._prs.clear()
            created = self._note_mutation(argv)
            out = runner(argv)
            if created and self._index is not None:
                try:
                    self._dirty.add(int(json.loads(out)["number"]))
                except (ValueError, KeyError, TypeError):
                    self._stale = True
            return out
        run.real = _is_real_runner(runner)
        return run

    def _note_mutation(self, argv: list) -> bool:
        """Mark what a write invalidates in the issue index: the issue it names, else all.
        Returns True for an issue create, whose number only its output carries."""
        if self._index is None or len(argv) < 3 or argv[0] != "gh":
            return False
        kind, verb = argv[1], argv[2]
        if kind == "issue":
            if verb in self._READ_VERBS or verb == "comment":
                return False
            if len(argv) > 3 and argv[3].isdigit():
                self._dirty.add(int(argv[3]))
            else:
                self._stale = True
            return False
        if kind == "pr":
            # Only a merge can change an issue (a closing keyword closes it).
            self._stale = self._stale or verb == "merge"
            return False
        if kind != "api":
            return False
        args, method, path, i = argv[2:], None, "", 0
        while i < len(args):
            if args[i] == "-X" and i + 1 < len(args):
                method, i = args[i + 1].upper(), i + 2
                continue
            if not path and not args[i].startswith("-"):
                path = args[i]
            i += 2 if args[i] in self._MUTATING_FLAGS + ("--jq", "-q", "-H") else 1
        method = method or ("POST" if any(a in self._MUTATING_FLAGS for a in args) else "GET")
        repo = re.escape(self.repo)
        if path == "graphql":
            self._stale = self._stale or any(
                a.startswith("query=") and a[6:].lstrip().startswith("mutation") for a in args)
            return False
        if method == "GET":
            return False
        if re.fullmatch(rf"repos/{repo}/issues", path):
            return True
        m = re.match(rf"repos/{repo}/issues/(\d+)(/[^?]*)?$", path)
        if m:
            tail = m.group(2) or ""
            if tail.startswith("/sub_issue"):
                self._stale = self._stale or not self._sub_issue_child
            elif not tail.startswith(("/comments", "/dependencies")):
                self._dirty.add(int(m.group(1)))
        elif re.fullmatch(rf"repos/{repo}/pulls/\d+/merge", path):
            self._stale = True
        return False

    def graphql(self, query: str, **variables: str) -> dict:
        if session_placement()["placement"] == "cloud":
            raise GhError("GraphQL is not available through the Claude Code cloud GitHub proxy")
        return super().graphql(query, **variables)

    @staticmethod
    def _no_rest_path(proxy: Callable[[], object], graphql: Callable[[], object]):
        """The PR operations public REST lacks (ready-for-review, review threads): the cloud
        proxy's `ccr` routes in a cloud session, which blocks GraphQL; GraphQL locally."""
        return proxy() if session_placement()["placement"] == "cloud" else graphql()

    # --- REST plumbing ---------------------------------------------------------------

    def _api(self, path: str, *args: str, method: Optional[str] = None) -> str:
        argv = ["gh", "api"] + (["-X", method] if method else []) + [path, *args]
        return self._run(argv)

    def _api_json(self, path: str, *args: str, method: Optional[str] = None):
        out = self._api(path, *args, method=method)
        return json.loads(out) if out.strip() else None

    def _api_items(self, path: str, jq: str = ".[]", confirm_end: bool = False,
                   expected: Optional[int] = None) -> list:
        """Every item across all pages. `jq` is `.[]` (the page is a list) or `.<key>[]` (the
        list under `<key>`). Pages by `page=N` on this `repos/{owner}/{repo}` path rather than
        `gh api --paginate`: GitHub's `Link: next` URLs use `repositories/{id}/...`, which the
        cloud's GitHub proxy rejects (HTTP 403). `expected` (a count the caller already holds,
        e.g. a parent's `sub_issues_summary.total`) ends the list without an end-of-list peek."""
        key = None if jq == ".[]" else jq[1:-2]
        if "per_page=" not in path:
            path += ("&" if "?" in path else "?") + "per_page=100"
        per_page = int(re.search(r"per_page=(\d+)", path).group(1))
        def fetch(page: int) -> list:
            data = self._api_json(f"{path}&page={page}")
            return (data or []) if key is None else ((data or {}).get(key) or [])

        def is_end(page: int, batch: list, held: int) -> bool:
            # GitHub sometimes serves a short or empty page mid-list. A short page is the end
            # when the caller's count is reached, or when the next page is empty: one peek, or
            # two for an empty page of a `confirm_end` list (the glitch seen most). Page 1 is
            # confirmed only for `confirm_end` lists, to keep small reads at one request.
            if expected is not None and held + len(batch) >= expected:
                return True
            if page == 1 and not confirm_end and expected is None:
                return True
            if fetch(page + 1):
                return False
            return not (confirm_end and not batch and fetch(page + 1))

        items: list = []
        for page in range(1, _REST_MAX_PAGES + 1):
            batch = fetch(page)
            for _ in range(_REST_SHORT_PAGE_RETRIES):
                if len(batch) >= per_page or is_end(page, batch, len(items)):
                    break
                batch = fetch(page)
            else:
                if len(batch) < per_page:
                    raise GhError(f"{path}: page {page} came back short ({len(batch)} of "
                                  f"{per_page}) while page {page + 1} has items -- GitHub served "
                                  f"a truncated listing; re-run the command")
            items.extend(batch)
            if len(batch) < per_page or (expected is not None and len(items) >= expected):
                return items
        raise GhError(f"{path}: more than {_REST_MAX_PAGES} pages -- refusing to list further")

    def pr_files(self, number: int) -> list:
        """Every changed path of a PR (`pulls/{n}/files`, paged like `_api_items`)."""
        return [f["filename"] for f in self._api_items(f"repos/{self.repo}/pulls/{number}/files")]

    def _issue(self, number: int) -> dict:
        """One issue, read once per process until a write may have changed it."""
        cached = self._issues.get(number)
        if cached is None:
            cached = self._issues[number] = self._api_json(f"repos/{self.repo}/issues/{number}")
        return cached

    def _held_issue(self, number: int) -> Optional[dict]:
        """The issue if this process already read it (index or `_issue`), else None."""
        return self._issues.get(number) or (self._index or {}).get(number)

    def _issue_db_id(self, number: int) -> int:
        """The issue's numeric REST id (sub-issue and dependency endpoints take it); ids never
        change, so one already read is reused."""
        held = self._held_issue(number)
        if held and held.get("id"):
            return int(held["id"])
        return int(self._api(f"repos/{self.repo}/issues/{number}", "--jq", ".id").strip())

    # --- org issue fields: config GraphQL ids -> REST ids / option names ---------------

    def _fields(self) -> list:
        """The org's issue fields (`id`, `node_id`, `name`, `options[{id, name}]`):
        `projectFields.issueFieldsRest` when the config carries that snapshot, else
        `GET orgs/{owner}/issue-fields`. The cloud's GitHub proxy allows only repo-scoped
        paths, so a repo driven from the cloud must carry the snapshot (`show-issue-fields`
        prints it)."""
        if self._org_fields is None:
            self._org_fields = (_PF.get("issueFieldsRest")
                                or self._api_items(f"orgs/{self._owner}/issue-fields"))
        return self._org_fields

    def _field(self, node_id: Optional[str], key: str) -> dict:
        """The org field whose GraphQL `node_id` is `projectFields.<key>`; raises on a miss."""
        if not node_id:
            raise GhError(f"projectFields.{key} is not configured")
        for field in self._fields():
            if field.get("node_id") == node_id:
                return field
        found = ", ".join(f"{f.get('name')} ({f.get('node_id')})" for f in self._fields())
        source = ("projectFields.issueFieldsRest (stale? re-run show-issue-fields)"
                  if _PF.get("issueFieldsRest") else f"GET orgs/{self._owner}/issue-fields")
        raise GhError(f"projectFields.{key}={node_id!r} matches no field in {source} "
                      f"(found: {found or 'none'})")

    @staticmethod
    def _option_name(field: dict, wanted: str) -> str:
        """The REST spelling of option `wanted` (matched by name, case-insensitively)."""
        names = [o.get("name") for o in field.get("options") or []]
        for name in names:
            if (name or "").lower() == wanted.lower():
                return name
        raise GhError(f"field {field.get('name')!r} has no option named {wanted!r} "
                      f"(options: {', '.join(map(str, names)) or 'none'})")

    def _set_field_option(self, number: int, field: dict, option: str):
        name = self._option_name(field, option)
        self._api(f"repos/{self.repo}/issues/{number}/issue-field-values",
                  "-F", f"issue_field_values[][field_id]={field['id']}",
                  "-f", f"issue_field_values[][value]={name}", method="POST")

    def _clear_field(self, number: int, field: dict):
        try:
            self._api(f"repos/{self.repo}/issues/{number}/issue-field-values/{field['id']}",
                      method="DELETE")
        except GhError as e:
            if "HTTP 404" not in str(e):  # no value set: already clear
                raise

    def _field_values(self, raw: dict) -> dict:
        """Single-select values of a REST issue, field name -> option name."""
        out = {}
        for v in raw.get("issue_field_values") or []:
            option = v.get("single_select_option")
            if v.get("data_type") != "single_select" or not option:
                continue
            name = v.get("issue_field_name") or next(
                (f.get("name") for f in self._fields() if f.get("id") == v.get("issue_field_id")),
                None)
            if name:
                out[name] = option.get("name")
        return out

    def _node(self, raw: dict) -> dict:
        """A REST issue in `issue_list()`'s GraphQL-era shape."""
        parent = _raw_parent(raw)
        return {"number": raw["number"], "title": raw.get("title"),
                "body": strip_proxy_footer(raw.get("body")) or "",
                "createdAt": raw.get("created_at"), "updatedAt": raw.get("updated_at"),
                "state": (raw.get("state") or "").upper(),
                "labels": [{"name": l["name"]} for l in raw.get("labels") or []],
                "issueType": {"name": raw["type"]["name"]} if raw.get("type") else None,
                "parent": {"number": parent} if parent else None,
                "milestone": (raw.get("milestone") or {}).get("title"),
                "fields": self._field_values(raw)}

    # --- issues ----------------------------------------------------------------------

    def issue_list(self) -> list:
        """Every open issue, plus the closed issues the running command can reach: the
        sub-issue trees of open parents and of the issues it names (`_FOCUS`), and every
        listed issue's parent chain. Cost scales with open work, not the repo's history.
        Served from a per-process index that refetches only what this process changed."""
        if self._index is None or self._stale:
            self._index, self._stale = self._load_index(), False
            self._dirty.clear()
        for number in sorted(self._dirty):
            raw = self._issue_or_none(number)
            if raw:
                self._index[number] = _slim_issue(raw)
            else:
                self._index.pop(number, None)
        self._dirty.clear()
        return without_retro_issues(
            [self._node(self._index[n]) for n in sorted(
                self._index, key=lambda n: (self._index[n].get("created_at") or "", n))])

    def _list_issues(self, state: str, extra: str = "&sort=created&direction=asc") -> list:
        return [i for i in self._api_items(f"repos/{self.repo}/issues?state={state}"
                                           f"&per_page=100{extra}", confirm_end=True)
                if not i.get("pull_request")]

    def _issue_or_none(self, number: int) -> Optional[dict]:
        """The issue by number; None when it is gone or is a PR."""
        try:
            raw = self._issue(number)
        except GhError as e:
            if "HTTP 404" not in str(e) and "HTTP 410" not in str(e):
                raise
            return None
        return raw if raw and not raw.get("pull_request") else None

    def _load_index(self) -> dict:
        """Open issues listed live; closed ones only where a unit reaches them (sub-issues,
        parents, the command's own numbers), each fetched by number or per parent. With
        `_FOCUS` set, only the trees holding those numbers walk their closed sub-issues; other
        open parents keep just their open children (their `sub_issues_summary` still counts
        all). Independent reads run in parallel."""
        index = {i["number"]: _slim_issue(i) for i in self._list_issues("open")}
        for raw in _parallel(self._issue_or_none, sorted(_FOCUS - index.keys())):
            if raw:
                index[raw["number"]] = _slim_issue(raw)
        walked: set = set()
        fetched: set = set()

        def _walks(n: int, in_focus_tree: bool, open_children: int) -> bool:
            if n in _FOCUS or in_focus_tree and _FOCUS:
                return True
            mode = _CLOSED_WALK[0]
            return (mode == "all" and not _FOCUS) or (mode == "childless" and not open_children)

        def top(n: int) -> int:
            seen = {n}
            while (p := _raw_parent(index[n])) in index and p not in seen:
                n = p
                seen.add(n)
            return n

        while True:
            # A parent with more sub-issues than are indexed has closed ones still to read.
            known: dict = {}
            for i in index.values():
                parent = _raw_parent(i)
                if parent:
                    known[parent] = known.get(parent, 0) + 1
            roots = {top(n) for n in _FOCUS if n in index}
            todo = [n for n, i in index.items() if n not in walked
                    and (i.get("sub_issues_summary") or {}).get("total", 0) > known.get(n, 0)
                    and _walks(n, top(n) in roots, known.get(n, 0))]
            missing = sorted(p for p in known if p not in index and p not in fetched)
            if not todo and not missing:
                return index
            walked.update(todo)
            fetched.update(missing)
            subs = _parallel(lambda n: self._api_items(
                f"repos/{self.repo}/issues/{n}/sub_issues",
                expected=index[n]["sub_issues_summary"]["total"]), sorted(todo))
            for n, children in zip(sorted(todo), subs):
                for child in children:
                    if not child.get("pull_request"):
                        index.setdefault(child["number"],
                                         _slim_issue({**child, "parent": {"number": n}}))
            for raw in _parallel(self._issue_or_none, missing):
                if raw:
                    index[raw["number"]] = _slim_issue(raw)

    def _comments(self, number: int) -> list:
        return [{"id": c.get("node_id"), "databaseId": c.get("id"),
                 "author": {"login": (c.get("user") or {}).get("login")},
                 "body": strip_proxy_footer(c.get("body")) or "",
                 "createdAt": c.get("created_at"), "url": c.get("html_url")}
                for c in self._api_items(f"repos/{self.repo}/issues/{number}/comments"
                                         f"?per_page=100")]

    def issue_view(self, number: int) -> dict:
        raw = self._issue(number)
        node = self._node(raw)
        count = raw.get("comments")
        return {**{k: node[k] for k in ("number", "title", "labels", "body", "state")},
                "comments": [] if count == 0 else self._comments(number)}

    def issue_edit(self, number: int, add_labels: list = (), remove_labels: list = (),
                   add_assignees: list = (), remove_assignees: list = ()):
        base = f"repos/{self.repo}/issues/{number}"
        if add_labels:
            self._api(f"{base}/labels", *[a for l in add_labels for a in ("-f", f"labels[]={l}")],
                      method="POST")
        for label in remove_labels:
            try:
                self._api(f"{base}/labels/{urllib.parse.quote(label, safe='')}", method="DELETE")
            except GhError as e:
                if "HTTP 404" not in str(e):  # not on the issue: already removed
                    raise
        for assignees, method in ((add_assignees, "POST"), (remove_assignees, "DELETE")):
            if assignees:
                self._api(f"{base}/assignees",
                          *[a for x in assignees for a in ("-f", f"assignees[]={x}")],
                          method=method)

    def issue_node_id(self, number: int) -> str:
        held = self._held_issue(number)
        if held and held.get("node_id"):
            return held["node_id"]
        return self._api(f"repos/{self.repo}/issues/{number}", "--jq", ".node_id").strip()

    def issue_fields(self, number: int) -> dict:
        return self._field_values(self._issue(number))

    def issue_epic_info(self, number: int) -> dict:
        node = self._node(self._issue(number))
        return {k: node[k] for k in ("issueType", "parent", "labels")}

    def _dependencies(self, number: int, kind: str) -> list:
        items = self._api_items(f"repos/{self.repo}/issues/{number}/dependencies/{kind}"
                                f"?per_page=100")
        return [i["number"] for i in items if i.get("state") == "open"]

    def blocked_by(self, number: int) -> list:
        return self._dependencies(number, "blocked_by")

    def blocking(self, number: int) -> list:
        return self._dependencies(number, "blocking")

    def add_blocked_by(self, issue_number: int, blocking_number: int):
        self._api(f"repos/{self.repo}/issues/{issue_number}/dependencies/blocked_by",
                  "-F", f"issue_id={self._issue_db_id(blocking_number)}", method="POST")

    def remove_blocked_by(self, issue_number: int, blocking_number: int):
        self._api(f"repos/{self.repo}/issues/{issue_number}/dependencies/blocked_by/"
                  f"{self._issue_db_id(blocking_number)}", method="DELETE")

    def set_issue_type(self, number: int, type_name: str):
        """Sets the type by name; still refuses a type missing from `issueTypeIds`."""
        if type_name not in ISSUE_TYPE_IDS:
            raise GhError(f"type_name={type_name!r} is not in projectFields.issueTypeIds "
                          f"(configured: {sorted(ISSUE_TYPE_IDS)})")
        self._api(f"repos/{self.repo}/issues/{number}", "-f", f"type={type_name}",
                  method="PATCH")

    def set_stage_field(self, number: int, stage: str):
        if stage not in STAGE_OPTION_IDS or stage not in _STAGE_OPTION_NAMES:
            raise GhError(f"stage {stage!r} is not in projectFields.stageOptionIds")
        self._set_field_option(number, self._field(STAGE_FIELD_ID, "stageFieldId"),
                               _STAGE_OPTION_NAMES[stage])

    def set_pipeline_status_field(self, number: int, status: str):
        if status not in PIPELINE_STATUS_OPTION_IDS or status not in _STATUS_OPTION_NAMES:
            raise GhError(f"status {status!r} is not in projectFields.pipelineStatusOptionIds")
        self._set_field_option(number, self._field(PIPELINE_STATUS_FIELD_ID,
                                                   "pipelineStatusFieldId"),
                               _STATUS_OPTION_NAMES[status])

    def set_priority_field(self, number: int, priority: str):
        if priority not in PRIORITY_OPTION_IDS:
            raise GhError(f"priority {priority!r} is not in projectFields.priorityOptionIds")
        self._set_field_option(number, self._field(PRIORITY_FIELD_ID, "priorityFieldId"),
                               priority)

    def set_effort_field(self, number: int, effort: str):
        if effort not in EFFORT_OPTION_IDS:
            raise GhError(f"effort {effort!r} is not in projectFields.effortOptionIds")
        self._set_field_option(number, self._field(EFFORT_FIELD_ID, "effortFieldId"), effort)

    def clear_stage_and_status_fields(self, number: int):
        self._clear_field(number, self._field(STAGE_FIELD_ID, "stageFieldId"))
        self._clear_field(number, self._field(PIPELINE_STATUS_FIELD_ID, "pipelineStatusFieldId"))

    def clear_stage_field(self, number: int):
        self._clear_field(number, self._field(STAGE_FIELD_ID, "stageFieldId"))

    def add_sub_issue(self, parent_number: int, child_number: int):
        self._sub_issue_write(f"repos/{self.repo}/issues/{parent_number}/sub_issues",
                              child_number, "POST")

    def remove_sub_issue(self, parent_number: int, child_number: int):
        self._sub_issue_write(f"repos/{self.repo}/issues/{parent_number}/sub_issue",
                              child_number, "DELETE")

    def _sub_issue_write(self, path: str, child_number: int, method: str):
        db_id = self._issue_db_id(child_number)
        self._sub_issue_child = True
        try:
            self._api(path, "-F", f"sub_issue_id={db_id}", method=method)
        finally:
            self._sub_issue_child = False
        if self._index is not None:
            self._dirty.add(child_number)

    def issue_comment(self, number: int, body: str):
        self._api(f"repos/{self.repo}/issues/{number}/comments", "-f", f"body={body}",
                  method="POST")

    def issue_comment_edit(self, comment: dict, body: str):
        if not comment.get("databaseId"):
            raise GhError("the comment has no REST id to edit")
        self._api(f"repos/{self.repo}/issues/comments/{comment['databaseId']}",
                  "-f", f"body={body}", method="PATCH")

    def issue_close(self, number: int, reason: str = "completed"):
        self._api(f"repos/{self.repo}/issues/{number}", "-f", "state=closed",
                  "-f", f"state_reason={reason.replace(' ', '_')}", method="PATCH")

    def issue_set_body(self, number: int, body: str):
        self._api(f"repos/{self.repo}/issues/{number}", "-f", f"body={body}", method="PATCH")

    def issue_set_title(self, number: int, title: str):
        self._api(f"repos/{self.repo}/issues/{number}", "-f", f"title={title}", method="PATCH")

    def ensure_label(self, label: str):
        """No-op: adding a missing label to an issue or PR creates it."""

    def delete_branch(self, branch: str):
        try:
            super().delete_branch(branch)
        except GhError as e:
            raise GhError(f"branch deletion failed (the cloud GitHub proxy refuses ref "
                          f"deletes): {e}")

    # --- pull requests ---------------------------------------------------------------

    @staticmethod
    def _pr_state(raw: dict) -> str:
        if raw.get("merged_at") or raw.get("merged"):
            return "MERGED"
        return (raw.get("state") or "").upper()

    def pr_comment(self, number: int, body: str):
        self.issue_comment(number, body)

    def pr_view(self, number: int, fields: str = "state,mergedAt") -> dict:
        wanted = [f for f in fields.split(",") if f]
        raw = self._api_json(f"repos/{self.repo}/pulls/{number}")
        merged = self._pr_state(raw) == "MERGED"
        view = {"number": raw["number"], "title": raw.get("title"), "state": self._pr_state(raw),
                "mergedAt": raw.get("merged_at"), "createdAt": raw.get("created_at"),
                "updatedAt": raw.get("updated_at"),
                "body": strip_proxy_footer(raw.get("body")) or "",
                "isDraft": bool(raw.get("draft")), "url": raw.get("html_url"),
                "headRefName": raw["head"]["ref"], "headRefOid": raw["head"]["sha"],
                "baseRefName": raw["base"]["ref"],
                "mergeCommit": ({"oid": raw.get("merge_commit_sha")}
                                if merged and raw.get("merge_commit_sha") else None)}
        if "comments" in wanted:
            view["comments"] = self._comments(number)
        return {k: view.get(k) for k in wanted}

    def _prs_for_branch(self, branch: str, state: str) -> list:
        """Read once per process per (branch, state) until a write may change it."""
        if (branch, state) not in self._prs:
            self._prs[(branch, state)] = self._read_prs_for_branch(branch, state)
        return self._prs[(branch, state)]

    def _read_prs_for_branch(self, branch: str, state: str) -> list:
        rest_state = "closed" if state == "merged" else state
        items = self._api_items(f"repos/{self.repo}/pulls?head={self._owner}:{branch}"
                                f"&state={rest_state}&per_page=100")
        items = [p for p in items if p["head"]["ref"] == branch]
        return [p for p in items if p.get("merged_at")] if state == "merged" else items

    def pr_list_for_branch(self, branch: str, state: str = "open") -> list:
        return [{"number": p["number"], "isDraft": bool(p.get("draft")),
                 "headRefName": p["head"]["ref"], "title": p.get("title"),
                 "url": p.get("html_url")}
                for p in self._prs_for_branch(branch, state)]

    def pr_heads_for_branch(self, branch: str) -> list:
        return [{"number": p["number"], "state": self._pr_state(p),
                 "headRefOid": p["head"]["sha"]}
                for p in self._prs_for_branch(branch, "all")]

    def open_prs(self) -> list:
        return [{"number": p["number"], "headRefName": p["head"]["ref"], "title": p.get("title"),
                 "isDraft": bool(p.get("draft")), "updatedAt": p.get("updated_at")}
                for p in self._api_items(f"repos/{self.repo}/pulls?state=open&per_page=100")]

    def pr_create(self, base: str, head: str, title: str, body: str, draft: bool = False) -> int:
        args = ["-f", f"title={title}", "-f", f"body={body}", "-f", f"head={head}",
                "-f", f"base={base}"] + (["-F", "draft=true"] if draft else [])
        try:
            raw = self._api_json(f"repos/{self.repo}/pulls", *args, method="POST")
        except ValueError:
            raw = None
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            # The cloud proxy has answered a draft create with an empty body although the PR
            # was made; `open-dev-pr` then looks it up by its head branch.
            raise GhError(f"{PR_CREATE_NO_NUMBER}: POST repos/{self.repo}/pulls "
                          f"(head {head}) returned {str(raw)[:200]!r}")
        return raw["number"]

    def pr_ready(self, number: int):
        self._no_rest_path(
            lambda: self._api(f"repos/{self.repo}/pulls/{number}/ccr/ready_for_review",
                              method="POST"),
            lambda: super(GitHubRest, self).pr_ready(number))

    def pr_convert_to_draft(self, number: int):
        """Public REST cannot draft an open PR: the proxy's `ccr` route in a cloud session
        (the counterpart of `ccr/ready_for_review`), `gh pr ready --undo` locally."""
        self._no_rest_path(
            lambda: self._api(f"repos/{self.repo}/pulls/{number}/ccr/convert_to_draft",
                              method="POST"),
            lambda: super(GitHubRest, self).pr_convert_to_draft(number))

    def pr_add_label(self, number: int, label: str):
        self.issue_edit(number, add_labels=[label])

    def pr_remove_label(self, number: int, label: str):
        self.issue_edit(number, remove_labels=[label])

    def pr_merge(self, number: int, delete_branch: bool = False, method: str = "squash",
                 match_head: Optional[str] = None):
        """`delete_branch` is ignored: the proxy refuses ref deletes (`cleanup_unit` reports)."""
        args = ["-f", f"merge_method={method}"]
        if match_head:
            args += ["-f", f"sha={match_head}"]
        self._api(f"repos/{self.repo}/pulls/{number}/merge", *args, method="PUT")

    def pr_checks(self, number: int) -> list:
        """`gh pr checks --json name,state,bucket,link,workflow` from the head's check runs and
        commit statuses; `workflow` is the Actions run's workflow name, else ""."""
        return self.commit_checks(self._api_json(f"repos/{self.repo}/pulls/{number}")["head"]["sha"])

    def commit_checks(self, sha: str) -> list:
        """`pr_checks`' shape for any commit."""
        runs = self._api_items(f"repos/{self.repo}/commits/{sha}/check-runs?per_page=100",
                               ".check_runs[]")
        workflows = {}
        if any((r.get("app") or {}).get("slug") == "github-actions" for r in runs):
            workflows = {w.get("check_suite_id"): w.get("name") for w in self._api_items(
                f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100",
                ".workflow_runs[]")}
        checks = []
        for r in runs:
            state = ((r.get("conclusion") if r.get("status") == "completed" else r.get("status"))
                     or "pending").upper()
            checks.append({"name": r.get("name"), "state": state,
                           "bucket": _CHECK_BUCKETS.get(state, "pending"),
                           "link": r.get("details_url") or r.get("html_url") or "",
                           "workflow": workflows.get((r.get("check_suite") or {}).get("id")) or ""})
        status = self._api_json(f"repos/{self.repo}/commits/{sha}/status") or {}
        for st in status.get("statuses") or []:
            state = (st.get("state") or "pending").upper()
            checks.append({"name": st.get("context"), "state": state,
                           "bucket": {"SUCCESS": "pass", "FAILURE": "fail",
                                      "ERROR": "fail"}.get(state, "pending"),
                           "link": st.get("target_url") or "", "workflow": ""})
        return checks

    def run_rerun_failed(self, run_id: int) -> None:
        self._api(f"repos/{self.repo}/actions/runs/{run_id}/rerun-failed-jobs", method="POST")

    # --- review threads (`_no_rest_path`) ----------------------------------------------

    def unresolved_review_threads(self, pr_number: int) -> list:
        graphql = super().unresolved_review_threads
        return self._no_rest_path(lambda: self._ccr_unresolved_threads(pr_number),
                                  lambda: graphql(pr_number))

    def reply_review_thread(self, thread_id: str, body: str):
        self._no_rest_path(lambda: self._ccr_reply(thread_id, body),
                           lambda: super(GitHubRest, self).reply_review_thread(thread_id, body))

    def resolve_review_thread(self, thread_id: str):
        self._no_rest_path(lambda: self._ccr_resolve(thread_id),
                           lambda: super(GitHubRest, self).resolve_review_thread(thread_id))

    def _ccr_unresolved_threads(self, pr_number: int) -> list:
        """GraphQL-shaped unresolved threads; each `id` is `ccr:<pr>:<first comment id>`,
        which `resolve-thread` takes back."""
        data = self._api_json(f"repos/{self.repo}/pulls/{pr_number}/ccr/review_threads")
        if isinstance(data, dict):
            data = next((data[k] for k in ("review_threads", "threads", "nodes") if k in data), [])
        out = []
        for t in data or []:
            resolved = t.get("is_resolved", t.get("isResolved", t.get("resolved", False)))
            if resolved:
                continue
            comments = t.get("comments") or []
            if isinstance(comments, dict):
                comments = comments.get("nodes") or []
            first = comments[0] if comments else {}
            cid = (t.get("root_comment_id") or t.get("comment_id") or first.get("database_id")
                   or first.get("databaseId") or first.get("id"))
            if cid is None:
                raise GhError(f"PR #{pr_number}: a ccr review thread has no comment id: {t}")
            out.append({"id": f"ccr:{pr_number}:{cid}", "isResolved": False,
                        "comments": {"nodes": [
                            {"body": strip_proxy_footer(c.get("body")) or "",
                             "author": {"login": (c.get("user") or c.get("author") or {})
                                        .get("login")}} for c in comments]}})
        return out

    @staticmethod
    def _thread(thread_id: str) -> tuple:
        m = _REST_THREAD_ID_RE.match(thread_id or "")
        if not m:
            raise GhError(f"thread id {thread_id!r} is not a cloud-session id "
                          f"(ccr:<pr>:<comment>) -- take it from this session's check-gate output")
        return int(m.group(1)), m.group(2)

    def _ccr_reply(self, thread_id: str, body: str):
        pr, comment = self._thread(thread_id)
        self._api(f"repos/{self.repo}/pulls/{pr}/comments/{comment}/replies", "-f",
                  f"body={body}", method="POST")

    def _ccr_resolve(self, thread_id: str):
        pr, comment = self._thread(thread_id)
        self._api(f"repos/{self.repo}/pulls/{pr}/ccr/comments/{comment}/resolve", method="POST")


GITHUB_APIS = ("graphql", "rest")


def github_api() -> dict:
    """Which GitHub client this session uses and the `signal` that decided it:
    `SDLC_GITHUB_API` (graphql|rest), else REST (`GitHubRest`) in every placement."""
    explicit = os.environ.get("SDLC_GITHUB_API", "").strip().lower()
    if explicit:
        if explicit not in GITHUB_APIS:
            raise GhError(f"SDLC_GITHUB_API={explicit!r} is neither 'graphql' nor 'rest' -- "
                          f"fix or unset it")
        return {"api": explicit, "signal": "SDLC_GITHUB_API"}
    return {"api": "rest", "signal": "default"}


def get_work_item_provider(runner: Runner = _default_runner) -> WorkItemProvider:
    """The configured `pipeline.workItemProvider`; only "github" exists, anything else raises.
    GitHub is `GitHubRest`, or the GraphQL `GitHub` when `SDLC_GITHUB_API=graphql`."""
    provider = PIPELINE.get("workItemProvider", {}).get("type", "github")
    if provider == "github":
        return (GitHubRest if github_api()["api"] == "rest" else GitHub)(runner=runner)
    raise GhError(f"pipeline.workItemProvider.type={provider!r} is not implemented -- "
                  f"only 'github' exists today (see README.md, 'Initiative-driven lifecycle')")


_UNRESOLVED_THREADS_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  pullRequest(number: {pr}) {{
    reviewThreads(first: 50) {{ nodes {{ id isResolved comments(first:10){{ nodes{{ body author{{ login }} }} }} }} }}
  }}
}} }}"""


_ISSUE_LIST_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issues(first: 100, after: {after}, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      number title body createdAt updatedAt state
      labels(first: 20) {{ nodes {{ name }} }}
      issueType {{ name }}
      parent {{ number }}
      milestone {{ title }}
      issueFieldValues(first: 10) {{
        nodes {{
          __typename
          ... on IssueFieldSingleSelectValue {{ field {{ ... on IssueFieldSingleSelect {{ name }} }} name }}
        }}
      }}
    }}
  }}
}} }}"""


_ISSUE_NODE_ID_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ id }}
}} }}"""


_ISSUE_FIELDS_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{
    issueFieldValues(first: 10) {{
      nodes {{
        __typename
        ... on IssueFieldSingleSelectValue {{ field {{ ... on IssueFieldSingleSelect {{ name }} }} name }}
      }}
    }}
  }}
}} }}"""


_ISSUE_EPIC_CHECK_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{
    issueType {{ name }}
    parent {{ number }}
    labels(first: 20) {{ nodes {{ name }} }}
  }}
}} }}"""


_BLOCKED_BY_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ blockedBy(first: 20) {{ nodes {{ number state }} }} }}
}} }}"""


_BLOCKING_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issue(number: {n}) {{ blocking(first: 20) {{ nodes {{ number state }} }} }}
}} }}"""


_ADD_BLOCKED_BY_MUTATION = """mutation {{
  addBlockedBy(input: {{issueId: "{issue_id}", blockingIssueId: "{blocking_id}"}}) {{
    issue {{ number }}
  }}
}}"""


_SET_ISSUE_TYPE_MUTATION = """mutation {{
  updateIssueIssueType(input: {{issueId: "{issue_id}", issueTypeId: "{type_id}"}}) {{
    issue {{ number }}
  }}
}}"""


_SET_ISSUE_FIELD_MUTATION = """mutation {{
  updateIssueFieldValue(input: {{
    issueId: "{issue_id}"
    issueField: {{ fieldId: "{field_id}" singleSelectOptionId: "{option_id}" }}
  }}) {{
    issue {{ number }}
  }}
}}"""


_DELETE_ISSUE_FIELD_VALUE_MUTATION = """mutation {{
  deleteIssueFieldValue(input: {{ issueId: "{issue_id}" fieldId: "{field_id}" }}) {{
    issue {{ number }}
  }}
}}"""


_ADD_SUB_ISSUE_MUTATION = """mutation {{
  addSubIssue(input: {{issueId: "{parent_id}", subIssueId: "{child_id}"}}) {{
    subIssue {{ number }}
  }}
}}"""


_REMOVE_SUB_ISSUE_MUTATION = """mutation {{
  removeSubIssue(input: {{issueId: "{parent_id}", subIssueId: "{child_id}"}}) {{
    subIssue {{ number }}
  }}
}}"""


_REMOVE_BLOCKED_BY_MUTATION = """mutation {{
  removeBlockedBy(input: {{issueId: "{issue_id}", blockingIssueId: "{blocking_id}"}}) {{
    issue {{ number }}
  }}
}}"""


_REPLY_REVIEW_THREAD_MUTATION = """mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}"""


_RESOLVE_REVIEW_THREAD_MUTATION = """mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}"""


# Resolve the repo sentinels once; `.format(...)` placeholders are left for call time.
for _qname, _qval in list(globals().items()):
    if isinstance(_qval, str) and "__OWNER__" in _qval:
        globals()[_qname] = _qval.replace("__OWNER__", _OWNER).replace("__NAME__", _NAME)


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import units  # noqa: E402,F401
from .units import classify_unit_from_issue, session_placement, without_retro_issues  # noqa: E402
from .worktrees import _PUSH_GENERATION  # noqa: E402
from .dev_pr import PR_CREATE_NO_NUMBER  # noqa: E402
