import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdlc_next import GitHub, GhError, REPO


def test_config_drives_repo_docroot_and_graphql_owner_for_an_arbitrary_repo(tmp_path):
    """A different config propagates to REPO, DOC_ROOT, schema ids and the GraphQL repo selector."""
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["repo"] = "acme/rocket"
    cfg["docRoot"] = "design/pipeline"
    cfg["projectFields"]["stageFieldId"] = "STAGE_FIELD_XYZ"
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {**os.environ, "SDLC_CONFIG": str(cfg_path)}
    script_dir = str(Path(__file__).resolve().parents[1])
    out = subprocess.check_output(
        [sys.executable, "-c",
         "import sdlc_next as s; print(s.REPO); print(s.DOC_ROOT); "
         "print(s.STAGE_FIELD_ID); print('__OWNER__' not in s._ISSUE_LIST_QUERY); "
         "print('acme' in s._ISSUE_LIST_QUERY and 'rocket' in s._ISSUE_LIST_QUERY)"],
        env=env, cwd=script_dir, text=True).splitlines()
    assert out == ["acme/rocket", "design/pipeline", "STAGE_FIELD_XYZ", "True", "True"]


class ScriptedRunner:
    """Runner double: canned stdout per exact argv (or prefix), GhError for `fail_on`,
    AssertionError on any unscripted call; records every call."""

    def __init__(self, responses: dict[tuple, str] | None = None):
        self.responses = dict(responses or {})
        self.prefix_responses: dict = {}
        self.calls: list[list[str]] = []
        self.fail_on: set[tuple] = set()

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        key = tuple(argv)
        if key in self.fail_on:
            raise GhError(f"command failed (1): {' '.join(argv)}\nsimulated failure")
        for prefix, response in self.prefix_responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return response
        if key not in self.responses:
            raise AssertionError(f"unexpected gh invocation: {argv}")
        return self.responses[key]


_NO_UNIT_WORKTREE = {("git", "-C", ".", "worktree", "list", "--porcelain"):
                      "worktree /repo\nHEAD x\nbranch refs/heads/main\n"}
_NO_WORKTREE_RESULT = {"released": False, "reason": "no worktree"}


def _live_wt(branch: str, path: str = "/repo", main: str = "/main") -> dict:
    """Scripted `git worktree list` showing main checkout on `main` and a live worktree
    at `path` holding `branch`."""
    return {("git", "-C", path, "worktree", "list", "--porcelain"):
            f"worktree {main}\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {path}\nHEAD bbb\nbranch refs/heads/{branch}\n"}


def _issue_list_page(nodes, has_next=False, end_cursor=None):
    return json.dumps({"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        "nodes": nodes,
    }}}})


def _field_values_nodes(fields: dict) -> list:
    return [{"__typename": "IssueFieldSingleSelectValue", "field": {"name": k}, "name": v}
            for k, v in fields.items() if v]


def _list_node(number, title="x", labels=None, created="2026-08-01T00:00:00Z", body="",
                state="OPEN", issue_type=None, parent=None, priority=None):
    return {
        "number": number, "title": title, "body": body, "createdAt": created, "state": state,
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "issueType": {"name": issue_type} if issue_type else None,
        "parent": {"number": parent} if parent else None,
        "issueFieldValues": {"nodes": _field_values_nodes({"Priority": priority})},
    }


def test_issue_list_fetches_via_graphql_and_flattens_labels_fields_and_type():
    from sdlc_next import _ISSUE_LIST_QUERY
    argv = ["gh", "api", "graphql", "-f", f"query={_ISSUE_LIST_QUERY.format(after='null')}"]
    node = _list_node(9, issue_type="Task", parent=92, priority="High")
    runner = ScriptedRunner({tuple(argv): _issue_list_page([node])})
    gh = GitHub(runner=runner)
    issues = gh.issue_list()
    assert len(issues) == 1
    issue = issues[0]
    assert issue["number"] == 9
    assert issue["labels"] == []
    assert issue["issueType"] == {"name": "Task"}
    assert issue["parent"] == {"number": 92}
    assert issue["fields"] == {"Priority": "High"}
    assert "issueFieldValues" not in issue


def test_issue_list_paginates_until_hasNextPage_false():
    from sdlc_next import _ISSUE_LIST_QUERY
    page1_argv = ["gh", "api", "graphql", "-f", f"query={_ISSUE_LIST_QUERY.format(after='null')}"]
    page2_argv = ["gh", "api", "graphql", "-f", f"query={_ISSUE_LIST_QUERY.format(after=chr(34)+'CURSOR'+chr(34))}"]
    runner = ScriptedRunner({
        tuple(page1_argv): _issue_list_page([_list_node(1)], has_next=True, end_cursor="CURSOR"),
        tuple(page2_argv): _issue_list_page([_list_node(2)], has_next=False),
    })
    gh = GitHub(runner=runner)
    issues = gh.issue_list()
    assert [i["number"] for i in issues] == [1, 2]


def test_issue_edit_adds_and_removes_labels_in_one_call():
    argv = ["gh", "issue", "edit", "42", "--repo", REPO,
            "--add-label", "status:awaiting-human-review", "--remove-label", "status:in-progress"]
    runner = ScriptedRunner({tuple(argv): ""})
    gh = GitHub(runner=runner)
    gh.issue_edit(42, add_labels=["status:awaiting-human-review"], remove_labels=["status:in-progress"])
    assert runner.calls == [argv]


def test_issue_edit_no_op_when_no_labels_given():
    runner = ScriptedRunner({})
    gh = GitHub(runner=runner)
    gh.issue_edit(42)
    assert runner.calls == []


def test_pr_checks_treats_no_checks_reported_error_as_empty_list():
    from sdlc_next import GitHub

    def runner(argv):
        raise GhError("command failed (1): gh pr checks 53 --repo owner/repo "
                       "--json name,state,bucket,link\nno checks reported on the 'issue-18' branch\n")

    gh = GitHub(runner=runner)
    assert gh.pr_checks(53) == []


def test_pr_checks_reraises_other_gh_errors():
    from sdlc_next import GitHub

    def runner(argv):
        raise GhError("command failed (1): some other failure")

    gh = GitHub(runner=runner)
    try:
        gh.pr_checks(53)
        assert False, "expected GhError"
    except GhError as e:
        assert "some other failure" in str(e)


def test_pr_files_uses_gh_api_paginate_pulls_files():
    # The paginated REST files endpoint has no file-count or diff-size cap.
    from sdlc_next import GitHub
    argv = ("gh", "api", "--paginate", f"repos/{REPO}/pulls/42/files", "--jq", ".[].filename")
    runner = ScriptedRunner({argv: "backend/src/a.ts\nfrontend/src/b.tsx\n"})
    gh = GitHub(runner=runner)
    assert gh.pr_files(42) == ["backend/src/a.ts", "frontend/src/b.tsx"]
    assert list(argv) in runner.calls


def test_pr_files_drops_blank_lines_but_keeps_every_real_path():
    from sdlc_next import GitHub
    argv = ("gh", "api", "--paginate", f"repos/{REPO}/pulls/42/files", "--jq", ".[].filename")
    # 150 paths: past the 100-file cap of `gh pr view --json files`.
    paths = [f"backend/src/file{i}.ts" for i in range(150)]
    runner = ScriptedRunner({argv: "\n".join(paths) + "\n"})
    gh = GitHub(runner=runner)
    assert gh.pr_files(42) == paths


def test_pr_files_empty_output_returns_empty_list():
    from sdlc_next import GitHub
    argv = ("gh", "api", "--paginate", f"repos/{REPO}/pulls/42/files", "--jq", ".[].filename")
    runner = ScriptedRunner({argv: ""})
    gh = GitHub(runner=runner)
    assert gh.pr_files(42) == []


def test_pr_create_parses_pr_number_from_returned_url():
    argv = ["gh", "pr", "create", "--repo", REPO, "--base", "main", "--head", "issue-9",
            "--title", "t", "--body", "b"]
    runner = ScriptedRunner({tuple(argv): "https://github.com/owner/repo/pull/57\n"})
    gh = GitHub(runner=runner)
    assert gh.pr_create(base="main", head="issue-9", title="t", body="b") == 57


def test_graphql_returns_data_field_only():
    query = "query { viewer { login } }"
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    runner = ScriptedRunner({tuple(argv): json.dumps({"data": {"viewer": {"login": "x"}}})})
    gh = GitHub(runner=runner)
    assert gh.graphql(query) == {"viewer": {"login": "x"}}


def test_runner_raises_ghe_error_on_nonzero_exit(monkeypatch):
    import subprocess
    from sdlc_next import _default_runner

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    try:
        _default_runner(["gh", "issue", "list"])
        assert False, "expected GhError"
    except GhError as e:
        assert "boom" in str(e)


def test_label_names_and_has_label():
    from sdlc_next import label_names, has_label
    issue = {"labels": [{"name": "stage:architecture"}, {"name": "priority:p1"}]}
    assert label_names(issue) == {"stage:architecture", "priority:p1"}
    assert has_label(issue, "priority:p1")
    assert not has_label(issue, "status:blocked")


def test_current_stage_reads_native_stage_field_or_none():
    from sdlc_next import current_stage
    assert current_stage({"fields": {"Stage": "Testing"}}) == "testing"
    assert current_stage({"fields": {"Priority": "High"}}) is None
    assert current_stage({"fields": {}}) is None
    assert current_stage({}) is None


def test_pipeline_status_reads_native_field_or_none():
    from sdlc_next import pipeline_status
    assert pipeline_status({"fields": {"Pipeline Status": "In Progress"}}) == "in-progress"
    assert pipeline_status({"fields": {"Pipeline Status": "Needs Human"}}) == "needs-human"
    assert pipeline_status({"fields": {}}) is None


def test_priority_rank_reads_native_priority_field_defaults_to_medium_when_missing():
    from sdlc_next import priority_rank
    assert priority_rank({"fields": {"Priority": "Urgent"}}) == 0
    assert priority_rank({"fields": {"Priority": "Low"}}) == 3
    assert priority_rank({"fields": {}}) == 2
    assert priority_rank({}) == 2


def test_sort_key_orders_by_priority_then_oldest_created_at():
    from sdlc_next import sort_key
    a = {"fields": {"Priority": "High"}, "createdAt": "2026-08-02T00:00:00Z"}
    b = {"fields": {"Priority": "High"}, "createdAt": "2026-08-01T00:00:00Z"}
    c = {"fields": {"Priority": "Urgent"}, "createdAt": "2026-08-05T00:00:00Z"}
    ordered = sorted([a, b, c], key=sort_key)
    assert ordered == [c, b, a]


def test_issue_type_reads_native_type_or_none():
    from sdlc_next import issue_type
    assert issue_type({"issueType": {"name": "Bug"}}) == "Bug"
    assert issue_type({"issueType": None}) is None
    assert issue_type({}) is None


def test_is_epic_reads_pipeline_classification_only():
    from sdlc_next import is_epic
    assert is_epic({"issueType": None, "parent": None, "labels": [{"name": "type:epic"}]}) is True
    assert is_epic({"issueType": None, "parent": {"number": 1},
                    "labels": [{"name": "type:epic"}]}) is True
    # A parentless Feature is not an epic by shape alone.
    assert is_epic({"issueType": {"name": "Feature"}, "parent": None, "labels": []}) is False
    assert is_epic({"issueType": None, "parent": None, "labels": [{"name": "type:task"}]}) is False


def test_default_stage_guesses_product_except_for_an_epics_child():
    # No Bug fast-track: a standing child is routed at pickup, whatever its type.
    from sdlc_next import default_stage
    assert default_stage(None) == "product"
    assert default_stage({"labels": [{"name": "type:initiative"}]}) == "product"
    assert default_stage({"labels": [{"name": "type:epic"}, {"name": "epic:standing"}]}) is None
    assert default_stage({"labels": [{"name": "type:epic"}]}) is None


def test_find_gate_pr_reads_marker_from_comments():
    from sdlc_next import find_gate_pr
    comments = [
        {"body": "🚧 Picking this up"},
        {"body": "✅ done\n\n<!-- gate-pr: product:31 -->\n<!-- stage-transition: product->human-review:product @ 2026-08-01T00:00:00Z -->"},
    ]
    assert find_gate_pr(comments) == ("product", 31)


def test_find_gate_pr_none_when_no_marker():
    from sdlc_next import find_gate_pr
    assert find_gate_pr([{"body": "no marker here"}]) is None


def test_find_gate_comments_cutoff_uses_marker_or_pr_created_at():
    from sdlc_next import find_gate_comments_cutoff
    pr_with_marker = {"createdAt": "2026-08-01T00:00:00Z", "comments": [
        {"body": "first", "createdAt": "2026-08-01T01:00:00Z"},
        {"body": "<!-- gate-comments-processed: 2026-08-02T00:00:00Z -->", "createdAt": "2026-08-02T00:00:00Z"},
    ]}
    assert find_gate_comments_cutoff(pr_with_marker) == "2026-08-02T00:00:00Z"

    pr_without_marker = {"createdAt": "2026-08-01T00:00:00Z", "comments": []}
    assert find_gate_comments_cutoff(pr_without_marker) == "2026-08-01T00:00:00Z"


def test_new_plain_comments_filters_by_cutoff():
    from sdlc_next import new_plain_comments
    pr = {"comments": [
        {"body": "old", "createdAt": "2026-08-01T00:00:00Z"},
        {"body": "new feedback", "createdAt": "2026-08-03T00:00:00Z"},
    ]}
    result = new_plain_comments(pr, cutoff="2026-08-02T00:00:00Z")
    assert [c["body"] for c in result] == ["new feedback"]


def test_evaluate_gate_satisfied_when_merged():
    from sdlc_next import GitHub, evaluate_gate
    import json
    from tests.test_sdlc_next import ScriptedRunner
    responses = {
        ("gh", "issue", "view", "10", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-08-05T00:00:00Z"}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = evaluate_gate(gh, 10)
    assert result == {"gate_pr": 40, "stage": "product", "status": "satisfied",
                       "unresolved_threads": [], "new_comments": []}


def test_evaluate_gate_feedback_pending_from_unresolved_thread():
    from sdlc_next import GitHub, evaluate_gate
    import json
    from tests.test_sdlc_next import ScriptedRunner
    query = (
        "query { repository(owner:\"owner\", name:\"repo\") {\n"
        "  pullRequest(number: 40) {\n"
        "    reviewThreads(first: 50) { nodes { id isResolved "
        "comments(first:10){ nodes{ body author{ login } } } } }\n"
        "  }\n"
        "} }"
    )
    responses = {
        ("gh", "issue", "view", "10", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "OPEN", "mergedAt": None}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "comments,createdAt"):
            json.dumps({"createdAt": "2026-08-01T00:00:00Z", "comments": []}),
        ("gh", "api", "graphql", "-f", f"query={query}"):
            json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {"id": "T1", "isResolved": False, "comments": {"nodes": [{"body": "fix this", "author": {"login": "op"}}]}}
            ]}}}}}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = evaluate_gate(gh, 10)
    assert result["status"] == "feedback_pending"
    assert result["unresolved_threads"][0]["id"] == "T1"


def test_evaluate_gate_feedback_pending_from_plain_comment():
    from sdlc_next import GitHub, evaluate_gate
    import json
    from tests.test_sdlc_next import ScriptedRunner
    query = (
        "query { repository(owner:\"owner\", name:\"repo\") {\n"
        "  pullRequest(number: 40) {\n"
        "    reviewThreads(first: 50) { nodes { id isResolved "
        "comments(first:10){ nodes{ body author{ login } } } } }\n"
        "  }\n"
        "} }"
    )
    responses = {
        ("gh", "issue", "view", "10", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "OPEN", "mergedAt": None}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "comments,createdAt"):
            json.dumps({"createdAt": "2026-08-01T00:00:00Z", "comments": [
                {"body": "please reconsider this", "createdAt": "2026-08-03T00:00:00Z"}
            ]}),
        ("gh", "api", "graphql", "-f", f"query={query}"):
            json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = evaluate_gate(gh, 10)
    assert result["status"] == "feedback_pending"
    assert result["unresolved_threads"] == []
    assert len(result["new_comments"]) == 1
    assert result["new_comments"][0]["body"] == "please reconsider this"


def test_evaluate_gate_not_satisfied_when_open_and_nothing_pending():
    from sdlc_next import GitHub, evaluate_gate
    import json
    from tests.test_sdlc_next import ScriptedRunner
    query = (
        "query { repository(owner:\"owner\", name:\"repo\") {\n"
        "  pullRequest(number: 40) {\n"
        "    reviewThreads(first: 50) { nodes { id isResolved "
        "comments(first:10){ nodes{ body author{ login } } } } }\n"
        "  }\n"
        "} }"
    )
    responses = {
        ("gh", "issue", "view", "10", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "OPEN", "mergedAt": None}),
        ("gh", "pr", "view", "40", "--repo", "owner/repo", "--json", "comments,createdAt"):
            json.dumps({"createdAt": "2026-08-01T00:00:00Z", "comments": []}),
        ("gh", "api", "graphql", "-f", f"query={query}"):
            json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert evaluate_gate(gh, 10)["status"] == "not_satisfied"


_STAGE_TITLE_CASE = {v: k for k, v in {
    "Product": "product", "Architecture": "architecture", "Development": "development",
    "Testing": "testing", "PR Review": "pr-review", "LLD": "lld",
}.items()}
_STATUS_TITLE_CASE = {v: k for k, v in {
    "Todo": "todo", "In Progress": "in-progress",
    "Awaiting Human Review": "awaiting-human-review", "Needs Human": "needs-human",
    "Feedback Received": "feedback-received",
    "Done": "done",
}.items()}


def _issue(number, stage=None, status=None, priority=None, created="2026-08-01T00:00:00Z", body="",
           issue_type=None, parent=None, state="OPEN", labels=None):
    fields = {}
    if priority:
        fields["Priority"] = priority
    if stage:
        fields["Stage"] = _STAGE_TITLE_CASE[stage]
    if status:
        fields["Pipeline Status"] = _STATUS_TITLE_CASE[status]
    return {"number": number, "title": f"issue {number}", "labels": labels or [], "createdAt": created,
            "body": body, "state": state, "issueType": {"name": issue_type} if issue_type else None,
            "parent": {"number": parent} if parent else None,
            "fields": fields}


def _epic(number, priority=None, created="2026-08-01T00:00:00Z", labels=None, stage=None, status=None,
          parent=None):
    """An Epic per the sample config's classification: `type:epic` plus any profile labels."""
    return _issue(number, priority=priority, created=created,
                  labels=["type:epic", *(labels or [])], stage=stage, status=status, parent=parent)


def _list_argv(after="null"):
    from sdlc_next import _ISSUE_LIST_QUERY
    return ["gh", "api", "graphql", "-f", f"query={_ISSUE_LIST_QUERY.format(after=after)}"]


def _list_response(issues):
    return json.dumps({"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{**i, "labels": {"nodes": [{"name": n} for n in i["labels"]]},
                   "issueFieldValues": {"nodes": _field_values_nodes(i.get("fields", {}))}}
                  for i in issues],
    }}}})


def _clean_pipeline_comments(pr=42):
    """The handoff + clean pr-review markers `missing_pipeline_evidence` requires, in order."""
    return [
        {"body": f"<!-- stage-transition: testing->pr-review @ 2026-08-19T00:00:00Z -->"},
        {"body": f"<!-- pr-review-outcome: clean:{pr} @ 2026-08-19T01:00:00Z -->"},
    ]


def _no_blockers_responses(*numbers):
    from sdlc_next import _BLOCKED_BY_QUERY
    return {
        tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKED_BY_QUERY.format(n=n)}"]):
            json.dumps({"data": {"repository": {"issue": {"blockedBy": {"nodes": []}}}}})
        for n in numbers
    }


def _stage_assign_responses(number, stage):
    """Node-id lookup + Stage-field write decide_next_action issues for an unstaged eligible issue."""
    from sdlc_next import _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID, STAGE_OPTION_IDS
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=number)}")
    mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id=f'ISSUE_{number}', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS[stage])}")
    return {
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": f"ISSUE_{number}"}}}}),
        mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": number}}}}),
    }


def test_crash_recovery_takes_priority_over_everything():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [epic_90, _issue(1, stage="development", status="in-progress", parent=90),
              _issue(2, stage="product", parent=90)]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "resume", "issue": 1, "unit": "issue", "stage": "development"}


def test_stale_in_progress_alongside_awaiting_review_is_not_crash_recovery():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issue = _issue(3, stage="product", status="awaiting-human-review", parent=90)
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([epic_90, issue]),
        ("gh", "issue", "view", "3", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:31 -->"}]}),
        ("gh", "pr", "view", "31", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-08-05T00:00:00Z"}),
    })
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 90)
    assert result["action"] == "pass-gate"
    assert result["issue"] == 3


def test_feedback_received_status_still_recognized_as_gate_pending():
    # feedback-received is gate-pending exactly like awaiting-human-review.
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issue = _issue(3, stage="product", status="feedback-received", parent=90)
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([epic_90, issue]),
        ("gh", "issue", "view", "3", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:31 -->"}]}),
        ("gh", "pr", "view", "31", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-08-05T00:00:00Z"}),
    })
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 90)
    assert result["action"] == "pass-gate"
    assert result["issue"] == 3


def test_needs_human_is_excluded():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [epic_90, _issue(4, stage="testing", status="needs-human", parent=90)]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "none", "epic": 90}


def test_blocked_issue_excluded_but_its_open_dependency_is_worked():
    # Excluding the dependency too would deadlock the chain.
    from sdlc_next import GitHub, decide_next_action, _BLOCKED_BY_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    blocked = _issue(5, stage="development", parent=90)
    dep = _issue(6, stage="development", parent=90)
    responses = {tuple(_list_argv()): _list_response([epic_90, blocked, dep])}
    responses[tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKED_BY_QUERY.format(n=5)}"])] = json.dumps(
        {"data": {"repository": {"issue": {"blockedBy": {"nodes": [{"number": 6, "state": "OPEN"}]}}}}})
    responses.update(_no_blockers_responses(6))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 6, "unit": "issue", "stage": "development"}


def test_blocked_issue_whose_dependency_closed_is_available():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    blocked = _issue(5, stage="development", parent=90)
    responses = {tuple(_list_argv()): _list_response([epic_90, blocked])}
    # blocked_by filters to OPEN blockers, so a closed dependency reads as none.
    responses.update(_no_blockers_responses(5))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 5, "unit": "issue", "stage": "development"}


def test_picks_highest_priority_then_oldest_among_eligible_within_one_epic():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [
        epic_90,
        _issue(7, stage="product", priority="Medium", created="2026-08-01T00:00:00Z", parent=90),
        _issue(8, stage="product", priority="Urgent", created="2026-08-03T00:00:00Z", parent=90),
    ]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(7, 8))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 8, "unit": "issue", "stage": "product"}


@pytest.mark.parametrize("issue_type", [None, "Bug"])
def test_a_fresh_standing_child_is_handed_out_for_routing_with_no_stage_written(issue_type):
    # Regression: it was stamped `product` (a Bug `architecture`) before anyone read it.
    from sdlc_next import GitHub, decide_next_action
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [epic_90, _issue(9, parent=90, issue_type=issue_type)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    runner = ScriptedRunner(responses)
    assert decide_next_action(GitHub(runner=runner), 90) == {
        "action": "route", "issue": 9, "unit": "issue"}
    assert not any("updateIssueFieldValue" in c[-1] for c in runner.calls)


def test_a_standing_child_with_a_stage_is_still_delegated_at_it():
    # Positive control: an in-flight child (already staged) resumes its flow unchanged.
    from sdlc_next import GitHub, decide_next_action
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [epic_90, _issue(9, parent=90, stage="architecture", issue_type="Bug")]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    assert decide_next_action(GitHub(runner=ScriptedRunner(responses)), 90) == {
        "action": "delegate", "issue": 9, "unit": "issue", "stage": "architecture"}


def test_a_fresh_standing_child_is_not_routed_while_blocked():
    from sdlc_next import GitHub, decide_next_action, _BLOCKED_BY_QUERY
    epic_90 = _epic(90, labels=["epic:standing"])
    responses = {tuple(_list_argv()): _list_response([epic_90, _issue(9, parent=90)]),
                 ("gh", "api", "graphql", "-f", f"query={_BLOCKED_BY_QUERY.format(n=9)}"):
                     json.dumps({"data": {"repository": {"issue": {"blockedBy": {
                         "nodes": [{"number": 7, "state": "OPEN"}]}}}}})}
    assert decide_next_action(GitHub(runner=ScriptedRunner(responses)), 90) == {
        "action": "none", "epic": 90}


def test_nothing_actionable_returns_none():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic_90])})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "none", "epic": 90}


def test_unknown_epic_number_raises():
    # A number that isn't an open epic is a caller error, not "nothing to do".
    from sdlc_next import GitHub, GhError, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([])})
    gh = GitHub(runner=runner)
    try:
        decide_next_action(gh, 999)
        assert False, "expected GhError"
    except GhError as e:
        assert "999" in str(e)


def test_child_issue_number_passed_as_epic_raises():
    from sdlc_next import GitHub, GhError, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    child = _issue(9, parent=90)
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic_90, child])})
    gh = GitHub(runner=runner)
    try:
        decide_next_action(gh, 9)  # 9 is a child issue, not an epic
        assert False, "expected GhError"
    except GhError as e:
        assert "9" in str(e)


def test_epic_legacy_skips_entirely_even_over_crash_recovery():
    # Only the issue list is scripted, so any further call would fail the test.
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:legacy"])
    issues = [epic_90, _issue(1, stage="development", status="in-progress", parent=90),
              _issue(2, stage="product", status="awaiting-human-review", parent=90)]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {
        "action": "skip", "epic": 90,
        "reason": "epic:legacy -- this pipeline no longer drives this epic or any of "
                  "its children; skipping entirely.",
    }


def test_check_epics_closeable_posts_checklist_flags_open_dependents_and_assigns_human():
    from sdlc_next import GitHub, cmd_check_epics_closeable, _BLOCKING_QUERY, HUMAN_ASSIGNEE, REPO
    from tests.test_sdlc_next import ScriptedRunner
    epic = _epic(94)
    child_a = _issue(10, parent=94, state="CLOSED")
    child_b = _issue(11, parent=94, state="CLOSED")
    issues = [epic, child_a, child_b]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses[("gh", "issue", "view", "94", "--repo", REPO,
               "--json", "number,title,labels,body,state,comments")] = json.dumps({"comments": []})
    responses[tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKING_QUERY.format(n=10)}"])] = json.dumps(
        {"data": {"repository": {"issue": {"blocking": {"nodes": [{"number": 21, "state": "OPEN"}]}}}}})
    responses[tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKING_QUERY.format(n=11)}"])] = json.dumps(
        {"data": {"repository": {"issue": {"blocking": {"nodes": []}}}}})
    # Both epic docs already on the epic branch (they reach main only at close-epic).
    for name in ("architecture.md", "lld.md"):
        responses[("gh", "api", f"repos/{REPO}/contents/docs/sdlc/epic-94/{name}?ref=epic-94",
                   "--jq", ".sha")] = "abc123\n"
    runner = ScriptedRunner(responses)
    runner.prefix_responses[("gh", "issue", "comment", "94", "--repo", REPO, "--body")] = ""
    runner.prefix_responses[("gh", "issue", "edit", "94", "--repo", REPO,
                              "--add-assignee", HUMAN_ASSIGNEE)] = ""
    gh = GitHub(runner=runner)
    result = cmd_check_epics_closeable(gh)
    assert result == {"closeable_epics": [
        {"epic": 94, "title": "issue 94", "notified": True, "open_dependents": [21],
         "docs_missing_from_epic_branch": []}
    ]}
    comment_calls = [c for c in runner.calls if c[:3] == ["gh", "issue", "comment"]]
    assert len(comment_calls) == 1
    assert "#21" in comment_calls[0][-1]
    assert "both on `epic-94` (auto-verified)" in comment_calls[0][-1]
    # Never asks GitHub about main -- an open epic's docs are not expected there.
    assert not any("?ref=main" in " ".join(c) for c in runner.calls)


def test_touches_pipeline_config_detects_config_changes_and_ignores_product_code():
    """The config file is matched by basename wherever it lives; nothing else counts."""
    from sdlc_next import touches_pipeline_config, CONFIG_FILENAME
    assert touches_pipeline_config([CONFIG_FILENAME])
    assert touches_pipeline_config([f".claude/{CONFIG_FILENAME}"])
    assert touches_pipeline_config(["backend/src/x.ts", CONFIG_FILENAME])
    assert not touches_pipeline_config([])
    assert not touches_pipeline_config(["backend/src/x.ts", "docs/sdlc/issue-9/lld.md"])
    assert not touches_pipeline_config([".claude/skills/some-other-skill/SKILL.md"])


def test_check_epics_closeable_flags_an_epic_doc_that_never_reached_the_epic_branch():
    """The closing checklist verifies epic docs on `epic-<n>` and flags any that are missing."""
    from sdlc_next import GitHub, cmd_check_epics_closeable, _BLOCKING_QUERY, HUMAN_ASSIGNEE, REPO
    from tests.test_sdlc_next import ScriptedRunner
    issues = [_epic(94), _issue(10, parent=94, state="CLOSED")]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses[("gh", "issue", "view", "94", "--repo", REPO,
               "--json", "number,title,labels,body,state,comments")] = json.dumps({"comments": []})
    responses[tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKING_QUERY.format(n=10)}"])] = json.dumps(
        {"data": {"repository": {"issue": {"blocking": {"nodes": []}}}}})
    responses[("gh", "api", f"repos/{REPO}/contents/docs/sdlc/epic-94/lld.md?ref=epic-94",
               "--jq", ".sha")] = "abc123\n"
    runner = ScriptedRunner(responses)
    # architecture.md is deliberately unscripted -> path_on_ref sees the failure as "absent".
    runner.fail_on.add(("gh", "api",
                        f"repos/{REPO}/contents/docs/sdlc/epic-94/architecture.md?ref=epic-94",
                        "--jq", ".sha"))
    runner.prefix_responses[("gh", "issue", "comment", "94", "--repo", REPO, "--body")] = ""
    runner.prefix_responses[("gh", "issue", "edit", "94", "--repo", REPO,
                              "--add-assignee", HUMAN_ASSIGNEE)] = ""
    gh = GitHub(runner=runner)
    result = cmd_check_epics_closeable(gh)
    assert result["closeable_epics"][0]["docs_missing_from_epic_branch"] == [
        "docs/sdlc/epic-94/architecture.md"]
    body = [c for c in runner.calls if c[:3] == ["gh", "issue", "comment"]][0][-1]
    assert "1 epic doc(s) never reached `epic-94`" in body
    assert "epic-94/architecture.md" in body
    edit_calls = [c for c in runner.calls if c[:3] == ["gh", "issue", "edit"]]
    assert edit_calls == [["gh", "issue", "edit", "94", "--repo", REPO, "--add-assignee", HUMAN_ASSIGNEE]]


def test_check_epics_closeable_skips_standing_epic_and_already_notified_epic():
    from sdlc_next import GitHub, cmd_check_epics_closeable, REPO
    from tests.test_sdlc_next import ScriptedRunner

    standing_epic = _epic(95, labels=["epic:standing"])
    standing_child = _issue(50, parent=95, state="CLOSED")

    notified_epic = _epic(96)
    notified_child = _issue(60, parent=96, state="CLOSED")

    issues = [standing_epic, standing_child, notified_epic, notified_child]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses[("gh", "issue", "view", "96", "--repo", REPO,
               "--json", "number,title,labels,body,state,comments")] = json.dumps(
        {"comments": [{"body": "some note\n<!-- epic-closeable-checklist-posted -->"}]})
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_check_epics_closeable(gh)
    # The standing epic is excluded before any issue_view; #96 posts nothing new.
    assert result == {"closeable_epics": [
        {"epic": 96, "title": "issue 96", "already_notified": True}
    ]}
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in runner.calls)


def test_check_epics_closeable_skips_epic_with_open_children():
    from sdlc_next import GitHub, cmd_check_epics_closeable
    from tests.test_sdlc_next import ScriptedRunner
    epic = _epic(97)
    open_child = _issue(70, parent=97, state="OPEN")
    issues = [epic, open_child]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    assert cmd_check_epics_closeable(gh) == {"closeable_epics": []}


def test_claim_sets_stage_and_status_fields_and_posts_start_comment():
    from sdlc_next import (GitHub, cmd_claim, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        ("gh", "issue", "comment", "9", "--repo", "owner/repo",
         "--body", "🚧 Picking this up — architecture stage starting."): "",
    })
    gh = GitHub(runner=runner)
    assert cmd_claim(gh, 9, "architecture") == {"issue": 9, "claimed": True}
    assert runner.calls.count(list(node_id_argv)) == 2
    assert list(stage_mutation_argv) in runner.calls
    assert list(status_mutation_argv) in runner.calls


def test_mark_issue_closed_clears_stage_and_sets_done_for_epic():
    from sdlc_next import (GitHub, cmd_mark_issue_closed, _ISSUE_EPIC_CHECK_QUERY,
                            _ISSUE_NODE_ID_QUERY, _DELETE_ISSUE_FIELD_VALUE_MUTATION,
                            _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=110)}")
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=110)}")
    clear_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_110', field_id=STAGE_FIELD_ID)}")
    set_done_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_110', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['done'])}")
    runner = ScriptedRunner({
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": None, "parent": None, "labels": {"nodes": [{"name": "type:epic"}]},
        }}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_110"}}}}),
        clear_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 110}}}}),
        set_done_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 110}}}}),
    })
    gh = GitHub(runner=runner)
    assert cmd_mark_issue_closed(gh, 110) == {"issue": 110, "is_epic": True, "is_initiative": False,
                                              "marked_done": True}
    assert list(clear_stage_argv) in runner.calls
    assert list(set_done_argv) in runner.calls


def test_mark_issue_closed_clears_stage_and_sets_done_for_non_epic():
    from sdlc_next import (GitHub, cmd_mark_issue_closed, _ISSUE_EPIC_CHECK_QUERY,
                            _ISSUE_NODE_ID_QUERY, _DELETE_ISSUE_FIELD_VALUE_MUTATION,
                            _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=183)}")
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=183)}")
    clear_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_183', field_id=STAGE_FIELD_ID)}")
    set_done_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_183', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['done'])}")
    runner = ScriptedRunner({
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 110}, "labels": {"nodes": []},
        }}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_183"}}}}),
        clear_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 183}}}}),
        set_done_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 183}}}}),
    })
    gh = GitHub(runner=runner)
    # No board-status call is scripted, so the non-epic path must not make one.
    assert cmd_mark_issue_closed(gh, 183) == {"issue": 183, "is_epic": False, "is_initiative": False,
                                              "marked_done": True}


def test_cli_mark_issue_closed_dispatches(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    captured = {}

    def fake(gh, issue):
        captured["issue"] = issue
        return {"issue": issue, "is_epic": True, "marked_done": True}

    monkeypatch.setattr(sdlc_next, "cmd_mark_issue_closed", fake)
    exit_code = sdlc_next.main(["mark-issue-closed", "110"])
    assert exit_code == 0
    assert captured == {"issue": 110}


def test_start_comment_posts_marker_with_no_label_mutation():
    from sdlc_next import GitHub, cmd_start_comment
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "issue", "comment", "9", "--repo", "owner/repo",
         "--body", "🚧 Picking this up — arch-review stage starting."): "",
        **dict([_epic_check(9)]),  # arch-review surfaces the profile's skip threshold
    })
    gh = GitHub(runner=runner)
    assert cmd_start_comment(gh, 9, "arch-review") == {
        "issue": 9, "started": "arch-review", "skip_confidence_threshold": 80}
    assert any(c[:3] == ["gh", "issue", "comment"] for c in runner.calls)


def test_handoff_to_pr_review_posts_marker_without_referencing_a_stage_doc():
    """The handoff posts the development->pr-review marker and points at no stage doc."""
    from sdlc_next import GitHub, cmd_handoff_to_pr_review
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner()
    runner.prefix_responses[("gh", "issue", "comment", "42", "--repo",
                             "owner/repo", "--body")] = ""
    gh = GitHub(runner=runner)
    result = cmd_handoff_to_pr_review(gh, 42, 77, "142 passed, 0 failed.")
    assert result == {"issue": 42, "pr": 77, "queued_for": "pr-review"}
    assert len(runner.calls) == 1
    body = runner.calls[0][-1]
    assert "development.md" not in body
    assert "testing.md" not in body
    assert "docs/sdlc/issue-42" not in body
    assert body.startswith("✅ Development complete. 142 passed, 0 failed. ")
    assert "PR #77 is queued for `pr-review`." in body
    assert "<!-- stage-transition: development->pr-review @ " in body


def test_git_rev_parse_head_runs_in_repo_path():
    from sdlc_next import git_rev_parse_head
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "rev-parse", "HEAD"): "abc1234\n",
    })
    assert git_rev_parse_head("/repo", runner=runner) == "abc1234"


def test_open_gate_creates_pr_sets_status_field_and_posts_marked_comment():
    from sdlc_next import (GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    git_runner = ScriptedRunner({("git", "-C", "/repo", "fetch", "origin"): "",
                                 ("git", "-C", "/repo", "rev-parse", "origin/issue-9"): "abc1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "create", "--repo", "owner/repo", "--base", "main", "--head", "issue-9",
         "--title", "Add widget — product.md for review (#9)",
         "--body",
         "Doc-only review gate for #9 — see \"Human-review gates\" in the pipeline "
         "docs. Merging this PR is the approval to proceed to "
         "architecture. Do NOT use Closes/Fixes here — the tracking issue stays open until the "
         "final code PR merges."):
            "https://github.com/owner/repo/pull/40\n",
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh_runner.prefix_responses[("gh", "issue", "comment", "9", "--repo", "owner/repo")] = ""
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", issue=9, title="Add widget", doc="product.md",
                            next_stage="architecture", summary="Adds a widget.", runner=git_runner)
    assert result["gate_pr"] == 40
    assert result["stage"] == "product"
    assert result["sha"] == "abc1234"

    call = gh_runner.calls[-1]
    body = call[call.index("--body") + 1]
    assert body.startswith(
        "✅ Requirements locked — see `docs/sdlc/issue-9/product.md` (`abc1234`). "
        "Adds a widget.\n\n"
        "⏸️ Awaiting human review — see #40. Merge it to approve and continue to `architecture`, "
        "or leave review comments on it for anything that needs to change (leave it unmerged — "
        "the pipeline picks up your comments and revises the doc automatically). Run "
        "`/sdlc:run` again once you've merged it, or any time after leaving comments if you'd "
        "like the revision done sooner.\n\n"
        "<!-- gate-pr: product:40 -->\n"
    )
    assert "<!-- gate-pr: product:40 -->" in body
    assert re.search(
        r"<!-- stage-transition: product->human-review:product @ "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->$", body)


def test_git_reconcile_branch_fast_forwards_to_its_origin_tip_before_merging_base():
    # Regression: a stale local tip merged base onto an old commit and the push was
    # rejected non-fast-forward.
    from sdlc_next import git_reconcile_branch
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    git_reconcile_branch("/repo", "issue-9", runner=runner)
    assert runner.calls == [
        ["git", "-C", "/repo", "fetch", "origin"],
        ["git", "-C", "/repo", "checkout", "issue-9"],
        ["git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"],
        ["git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"],
        ["git", "-C", "/repo", "merge", "origin/main"],
        ["git", "-C", "/repo", "push", "origin", "issue-9"],
    ]


def test_git_reconcile_branch_skips_the_fast_forward_for_a_branch_not_on_origin():
    from sdlc_next import git_reconcile_branch
    show_ref = ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
                "refs/remotes/origin/issue-9")
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    runner.fail_on = {show_ref}
    git_reconcile_branch("/repo", "issue-9", runner=runner)
    assert not any("--ff-only" in c for c in runner.calls)
    assert runner.calls[-1] == ["git", "-C", "/repo", "push", "origin", "issue-9"]


def test_mark_blocked_adds_native_relationship_and_comment():
    from sdlc_next import (GitHub, cmd_mark_blocked, _ISSUE_NODE_ID_QUERY, _ADD_BLOCKED_BY_MUTATION,
                            _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "api", "graphql", "-f",
         f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['todo'])}"):
            json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=6)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_6"}}}}),
        ("gh", "api", "graphql", "-f",
         f"query={_ADD_BLOCKED_BY_MUTATION.format(issue_id='ISSUE_9', blocking_id='ISSUE_6')}"):
            json.dumps({"data": {"addBlockedBy": {"issue": {"number": 9}}}}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=runner)
    assert cmd_mark_blocked(gh, 9, dep=6) == {"issue": 9, "status": "blocked", "on": 6,
                                               "worktree": _NO_WORKTREE_RESULT}


def test_mark_blocked_resets_pipeline_status_so_it_does_not_read_as_a_crashed_run():
    """A blocked unit left `in-progress` with no worktree would read as a crashed run."""
    from sdlc_next import (GitHub, cmd_mark_blocked, _ISSUE_NODE_ID_QUERY, _ADD_BLOCKED_BY_MUTATION,
                            _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    status_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['todo'])}")
    runner = ScriptedRunner({
        status_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=6)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_6"}}}}),
        ("gh", "api", "graphql", "-f",
         f"query={_ADD_BLOCKED_BY_MUTATION.format(issue_id='ISSUE_9', blocking_id='ISSUE_6')}"):
            json.dumps({"data": {"addBlockedBy": {"issue": {"number": 9}}}}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    cmd_mark_blocked(GitHub(runner=runner), 9, dep=6)
    assert list(status_argv) in runner.calls, "mark-blocked must reset Pipeline Status to todo"
    for wrong in ("in-progress", "needs-human"):
        bad = ("gh", "api", "graphql", "-f",
            f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS[wrong])}")
        assert list(bad) not in runner.calls


def test_mark_needs_human_sets_status_field_and_comment():
    from sdlc_next import (GitHub, cmd_mark_needs_human, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['needs-human'])}")
    runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=runner)
    assert cmd_mark_needs_human(gh, 9, reason="three failed bounces") == {
        "issue": 9, "status": "needs-human", "worktree": _NO_WORKTREE_RESULT}


def test_verify_exit_reports_stage_docs_and_commits(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    import json
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (docs_dir / "product.md").write_text("x")
    (docs_dir / "architecture.md").write_text("x")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Testing"},
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"}, "name": "In Progress"},
        ]}}}}}),
    })
    git_runner = ScriptedRunner({
        ("git", "-C", str(tmp_path), "log", "--oneline", "-5"): "abc1234 add development.md\ndef5678 wire endpoint\n",
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="testing", runner=git_runner)
    assert result["expected_stage_present"] is True
    assert result["stage"] == "testing"
    assert result["pipeline_status"] == "in-progress"
    assert result["docs_present"] == ["architecture.md", "product.md"]
    assert result["recent_commits"] == ["abc1234 add development.md", "def5678 wire endpoint"]
    assert "pr" not in result


def test_verify_exit_includes_pr_state_when_pr_given(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    import json
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Testing"},
        ]}}}}}),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "isDraft,headRefName,baseRefName"):
            json.dumps({"isDraft": True, "headRefName": "issue-9", "baseRefName": "main"}),
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="testing", pr=42, runner=git_runner)
    assert result["pr"] == 42
    assert result["pr_is_draft"] is True
    assert result["pr_head"] == "issue-9"
    assert result["pr_base"] == "main"
    assert result["docs_present"] == []
    assert result["recent_commits"] == []


def test_verify_exit_flags_missing_expected_stage(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    import json
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Development"},
        ]}}}}}),
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="testing", runner=git_runner)
    assert result["expected_stage_present"] is False


def test_list_needs_human_extracts_reason_from_mark_needs_human_comment():
    from sdlc_next import GitHub, cmd_list_needs_human
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([_issue(11, stage="testing", status="needs-human", parent=90)]),
        ("gh", "issue", "view", "11", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [
                {"body": "🚧 Picking this up"},
                {"body": "🙋 Needs human input — pr-checks token permission gap, grant "
                          "Checks read access.\n\n<!-- stage-transition: pr-review->needs-human"
                          " @ 2026-08-09T00:00:00Z -->"},
            ]}),
    })
    gh = GitHub(runner=runner)
    result = cmd_list_needs_human(gh)
    assert result == {"needs_human": [
        {"issue": 11, "title": "issue 11",
         "reason": "pr-checks token permission gap, grant Checks read access."},
    ]}


def test_list_needs_human_skips_issues_without_the_label():
    from sdlc_next import GitHub, cmd_list_needs_human
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([_issue(12, stage="development", parent=90)])})
    gh = GitHub(runner=runner)
    assert cmd_list_needs_human(gh) == {"needs_human": []}


def test_list_needs_human_skips_closed_issues():
    from sdlc_next import GitHub, cmd_list_needs_human
    from tests.test_sdlc_next import ScriptedRunner
    closed = _issue(13, status="needs-human", parent=90, state="CLOSED")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([closed])})
    gh = GitHub(runner=runner)
    assert cmd_list_needs_human(gh) == {"needs_human": []}


def test_claim_raises_on_unknown_role():
    from sdlc_next import GitHub, GhError, cmd_claim
    gh = GitHub(runner=ScriptedRunner({}))
    try:
        cmd_claim(gh, 9, "developement")
        assert False, "expected GhError"
    except GhError as e:
        assert "unknown role" in str(e)


def test_pairing_counts_derives_bounces_from_markers():
    from sdlc_next import GitHub, cmd_pairing_counts, REPO
    comments = [
        {"body": "review\n<!-- pr-review-outcome: rework:42 @ 2026-08-19T00:00:00Z -->"},
        {"body": "review\n<!-- pr-review-outcome: clean:42 @ 2026-08-19T01:00:00Z -->"},
        {"body": "conflict\n<!-- sync-conflict: issue-9 @ 2026-08-19T02:00:00Z -->"},
        {"body": "review\n<!-- pr-review-outcome: rework:42 @ 2026-08-19T03:00:00Z -->"},
        {"body": "review\n<!-- pr-review-outcome: rework:42 @ 2026-08-19T04:00:00Z -->"},
    ]
    runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": comments}),
    })
    gh = GitHub(runner=runner)
    assert cmd_pairing_counts(gh, 9) == {
        "issue": 9,
        "thresholds": {"replace_at": 3, "needs_human_at": 6},
        "pr_review_rework_since_last_clean": 2,
        "pr_review_total_rework": 3,
        "pr_review_total_clean": 1,
        "pr_review_same_class_recurrence_count": 0,
        "sync_conflict_count": 1,
        "design_review": {},
    }


def test_pairing_counts_tracks_design_reviews_per_role():
    """Design-review bounces are counted from markers, keyed per review role."""
    from sdlc_next import GitHub, cmd_pairing_counts, REPO
    comments = [
        {"body": "r\n<!-- design-review-outcome: rework:lld-review @ 2026-08-28T00:00:00Z -->"},
        {"body": "r\n<!-- design-review-outcome: rework:lld-review @ 2026-08-28T01:00:00Z -->"},
        {"body": "r\n<!-- design-review-outcome: rework:arch-review @ 2026-08-28T02:00:00Z -->"},
        {"body": "r\n<!-- design-review-outcome: clean:arch-review @ 2026-08-28T03:00:00Z -->"},
        {"body": "r\n<!-- design-review-outcome: rework:lld-review @ 2026-08-28T04:00:00Z -->"},
    ]
    runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": comments}),
    })
    result = cmd_pairing_counts(GitHub(runner=runner), 9)
    assert result["design_review"]["lld-review"] == {
        "rework_since_last_clean": 3, "total_rework": 3, "total_clean": 0,
        "same_class_recurrence_count": 0}
    # arch-review's clean verdict resets only its own counter.
    assert result["design_review"]["arch-review"] == {
        "rework_since_last_clean": 0, "total_rework": 1, "total_clean": 1,
        "same_class_recurrence_count": 0}
    # A review that never ran has no key, not a zeroed entry.
    assert "pr-review" not in result["design_review"]


def test_record_design_review_refuses_an_unknown_role():
    """A typo'd role would create a pairing nothing ever reads, so it is refused."""
    import pytest
    from sdlc_next import GitHub, GhError, cmd_record_design_review
    with pytest.raises(GhError, match="role must be one of"):
        cmd_record_design_review(GitHub(runner=ScriptedRunner({})), 9,
                                 "pr-review", "clean", "s")


def test_record_design_review_refuses_same_class_recurrence_on_a_clean_verdict():
    # same_class_recurrence is only valid with outcome="rework".
    import pytest
    from sdlc_next import GitHub, GhError, cmd_record_design_review
    with pytest.raises(GhError, match="only makes sense on outcome='rework'"):
        cmd_record_design_review(GitHub(runner=ScriptedRunner({})), 9,
                                 "lld-review", "clean", "s", same_class_recurrence=True)


_ISSUE_VIEW_9 = ("gh", "issue", "view", "9", "--repo", REPO,
                 "--json", "number,title,labels,body,state,comments")


def test_record_design_review_embeds_the_same_class_marker_and_pairing_counts_reads_it_back():
    # same_class_recurrence=True must produce a marker cmd_pairing_counts counts.
    from sdlc_next import GitHub, cmd_record_design_review, cmd_pairing_counts, REPO
    posted = {}

    class RecordingGitHub(GitHub):
        def issue_comment(self, issue, body):
            posted["body"] = body

    gh = RecordingGitHub(runner=ScriptedRunner({_ISSUE_VIEW_9: json.dumps({"comments": []})}))
    result = cmd_record_design_review(gh, 9, "lld-review", "rework", "same miss again",
                                      same_class_recurrence=True)
    assert result["same_class_recurrence"] is True
    assert "same-class:true" in posted["body"]
    assert "escalation candidate" in posted["body"]

    runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": posted["body"]}]}),
    })
    counts = cmd_pairing_counts(GitHub(runner=runner), 9)
    assert counts["design_review"]["lld-review"]["same_class_recurrence_count"] == 1


def test_record_design_review_without_the_flag_leaves_same_class_count_at_zero():
    # An ordinary rework is not counted as a same-class recurrence.
    from sdlc_next import GitHub, cmd_record_design_review, cmd_pairing_counts, REPO
    posted = {}

    class RecordingGitHub(GitHub):
        def issue_comment(self, issue, body):
            posted["body"] = body

    gh = RecordingGitHub(runner=ScriptedRunner({_ISSUE_VIEW_9: json.dumps({"comments": []})}))
    cmd_record_design_review(gh, 9, "lld-review", "rework", "a fresh defect")
    assert "same-class" not in posted["body"]

    runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": posted["body"]}]}),
    })
    counts = cmd_pairing_counts(GitHub(runner=runner), 9)
    assert counts["design_review"]["lld-review"]["same_class_recurrence_count"] == 0


def test_record_pr_review_embeds_the_same_class_marker_and_pairing_counts_reads_it_back():
    from sdlc_next import GitHub, cmd_record_pr_review, cmd_pairing_counts, REPO
    posted = {}

    class RecordingGitHub(GitHub):
        def issue_comment(self, issue, body):
            posted["body"] = body

    gh = RecordingGitHub(runner=ScriptedRunner({}))
    result = cmd_record_pr_review(gh, 9, 42, "rework", "same miss again",
                                  same_class_recurrence=True)
    assert result["same_class_recurrence"] is True
    assert "same-class:true" in posted["body"]

    runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": posted["body"]}]}),
    })
    counts = cmd_pairing_counts(GitHub(runner=runner), 9)
    assert counts["pr_review_same_class_recurrence_count"] == 1


def test_checks_status_pending_if_any_check_still_pending():
    from sdlc_next import checks_status
    checks = [{"bucket": "pass"}, {"bucket": "pending"}]
    assert checks_status(checks) == "pending"


def test_checks_status_failed_if_any_check_failed():
    from sdlc_next import checks_status
    checks = [{"bucket": "pass"}, {"bucket": "fail"}]
    assert checks_status(checks) == "failed"


def test_checks_status_passed_if_all_pass():
    from sdlc_next import checks_status
    assert checks_status([{"bucket": "pass"}, {"bucket": "pass"}]) == "passed"


def test_checks_status_passed_when_no_checks_configured():
    from sdlc_next import checks_status
    assert checks_status([]) == "passed"


def test_merge_pr_refuses_when_checks_not_passed():
    from sdlc_next import GitHub, GhError, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pending"}]),
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
    })
    gh = GitHub(runner=runner)
    try:
        cmd_merge_pr(gh, 42, issue=9)
        assert False, "expected GhError"
    except GhError as e:
        assert "not passed" in str(e)


def test_merge_pr_merges_and_confirms_issue_closed_when_checks_pass():
    # No field-mutation call is scripted: merge-pr must not touch Stage/Pipeline Status.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pass"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result == {"pr": 42, "issue": 9, "merged": True, "issue_closed": True, "config_changed": False,
                       "worktree": _NO_WORKTREE_RESULT}


def test_missing_required_workflows_flags_backend_touch_with_no_check():
    from sdlc_next import missing_required_workflows
    missing = missing_required_workflows(
        ["backend/src/x.ts"],
        [{"workflow": "sdlc-next Gate Auto-Advance", "bucket": "pass"}],
    )
    assert missing == ["Backend CI"]


def test_missing_required_workflows_empty_for_docs_only_pr():
    from sdlc_next import missing_required_workflows
    assert missing_required_workflows(["docs/sdlc/issue-9/product.md"], []) == []


def test_missing_required_workflows_satisfied_by_passing_check():
    from sdlc_next import missing_required_workflows
    missing = missing_required_workflows(
        ["backend/src/x.ts"],
        [{"workflow": "Backend CI", "bucket": "pass"}],
    )
    assert missing == []


def test_missing_required_workflows_treats_all_jobs_skipped_as_missing():
    from sdlc_next import missing_required_workflows
    missing = missing_required_workflows(
        ["backend/src/x.ts"],
        [{"workflow": "Backend CI", "bucket": "skipping"}],
    )
    assert missing == ["Backend CI"]


def test_merge_gate_status_returns_pending_verbatim_when_required_workflow_itself_is_pending():
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts"],
        [{"workflow": "Backend CI", "bucket": "pending"}],
    )
    # Retryable "pending", not "missing-checks"; a still-running check also counts as missing.
    assert status == "pending"
    assert missing == ["Backend CI"]


def test_merge_gate_status_surfaces_missing_workflow_even_when_status_is_pending():
    # A never-reported workflow still surfaces while an unrelated check is pending.
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts", "frontend/src/y.tsx"],
        [
            {"workflow": "Backend CI", "bucket": "pass"},
            {"workflow": "sdlc-next Gate Auto-Advance", "bucket": "pending"},
        ],
    )
    assert status == "pending"
    assert missing == ["Frontend CI"]


def test_merge_gate_status_surfaces_missing_workflow_even_when_status_is_failed():
    # A failed suite and a never-reported workflow must surface together.
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts", "frontend/src/y.tsx"],
        [{"workflow": "Backend CI", "bucket": "fail"}],
    )
    assert status == "failed"
    assert set(missing) == {"Backend CI", "Frontend CI"}


def test_merge_gate_status_missing_checks_when_required_workflow_absent():
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts"],
        [{"workflow": "sdlc-next Gate Auto-Advance", "bucket": "pass"}],
    )
    assert status == "missing-checks"
    assert missing == ["Backend CI"]


# ── Local-CI attestation ──────────────────────────────────────────────────────

def test_local_ci_attestation_satisfies_required_backend_when_sha_matches():
    # A local-ci attestation for the current head stands in for a missing GHA check.
    from sdlc_next import missing_required_workflows
    comments = [{"body": "🧪 <!-- local-ci: backend:42 @ abc1234def5678 -->"}]
    missing = missing_required_workflows(
        ["backend/src/x.ts"], [], comments, "abc1234def5678")
    assert missing == []


def test_local_ci_attestation_ignored_when_sha_is_stale():
    # An attestation for an older commit does not satisfy the gate.
    from sdlc_next import missing_required_workflows
    comments = [{"body": "<!-- local-ci: backend:42 @ deadbeef0000 -->"}]
    missing = missing_required_workflows(
        ["backend/src/x.ts"], [], comments, "abc1234def5678")
    assert missing == ["Backend CI"]


def test_local_ci_attestation_short_sha_prefix_matches_full_head():
    from sdlc_next import missing_required_workflows
    comments = [{"body": "<!-- local-ci: frontend:42 @ abc1234 -->"}]
    missing = missing_required_workflows(
        ["frontend/app/page.tsx"], [], comments,
        "abc1234def5678901234567890abcdef12345678")
    assert missing == []


def test_local_ci_attestation_is_per_suite():
    # A PR touching both trees, attested for backend only, still misses frontend.
    from sdlc_next import missing_required_workflows
    comments = [{"body": "<!-- local-ci: backend:42 @ abc1234 -->"}]
    missing = missing_required_workflows(
        ["backend/src/x.ts", "frontend/app/page.tsx"],
        [], comments, "abc1234")
    assert missing == ["Frontend CI"]


def test_local_ci_no_head_sha_trusts_nothing():
    from sdlc_next import local_ci_suites_attested
    assert local_ci_suites_attested(
        [{"body": "<!-- local-ci: backend:42 @ abc1234 -->"}], None) == set()


def test_missing_required_workflows_two_arg_calls_unchanged_by_local_ci():
    # The 2-arg GHA-only form still works.
    from sdlc_next import missing_required_workflows
    assert missing_required_workflows(
        ["backend/src/x.ts"],
        [{"workflow": "Backend CI", "bucket": "pass"}]) == []
    assert missing_required_workflows(
        ["backend/src/x.ts"], []) == ["Backend CI"]


def _ci_output(tmp_path, text="Tests:  142 passed, 142 total\nexit 0\n"):
    out = tmp_path / "backend-it.log"
    out.write_text(text)
    return str(out)


def test_record_local_ci_posts_attestation_marker_on_the_pr(tmp_path):
    from sdlc_next import GitHub, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({})
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    result = cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234def",
                                  command="npm run test:it", output=_ci_output(tmp_path))
    assert result == {"pr": 42, "suite": "backend", "sha": "abc1234def",
                      "command": "npm run test:it", "evidence_lines": 2, "attested": True}
    body = next(c for c in runner.calls if c[:3] == ["gh", "pr", "comment"])[-1]
    assert "<!-- local-ci: backend:42 @ abc1234def -->" in body


def test_record_local_ci_embeds_the_runs_own_output_not_a_summary(tmp_path):
    """The attestation carries the run's captured output, not a summary."""
    from sdlc_next import GitHub, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({})
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234def",
                        command="npm run test:it",
                        output=_ci_output(tmp_path, "Suites: 12 passed\nTests:  142 passed\n"))
    body = next(c for c in runner.calls if c[:3] == ["gh", "pr", "comment"])[-1]
    assert "Command: `npm run test:it`" in body
    assert "Tests:  142 passed" in body
    assert "<details><summary>captured output (tail)</summary>" in body


def test_record_local_ci_trims_a_long_run_to_its_tail(tmp_path):
    from sdlc_next import GitHub, cmd_record_local_ci, LOCAL_CI_EVIDENCE_LINES
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({})
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    long_log = "\n".join(f"line {i}" for i in range(500)) + "\nTests:  142 passed\n"
    cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234def",
                        command="npm run test:it", output=_ci_output(tmp_path, long_log))
    body = next(c for c in runner.calls if c[:3] == ["gh", "pr", "comment"])[-1]
    assert "line 0\n" not in body
    assert "... (earlier output trimmed) ..." in body
    assert "Tests:  142 passed" in body
    assert body.count("\nline ") <= LOCAL_CI_EVIDENCE_LINES


def test_record_local_ci_refuses_an_empty_output_file(tmp_path):
    """An empty output capture is refused."""
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="must carry the run's own output"):
        cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234",
                            command="npm run test:it", output=_ci_output(tmp_path, "   \n"))


def test_record_local_ci_refuses_a_missing_output_file(tmp_path):
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="readable file"):
        cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234",
                            command="npm run test:it", output=str(tmp_path / "nope.log"))


def test_record_local_ci_rejects_an_empty_command(tmp_path):
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="--command is required"):
        cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234",
                            command="  ", output=_ci_output(tmp_path))


def test_record_local_ci_rejects_unknown_suite(tmp_path):
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="suite must be one of"):
        cmd_record_local_ci(gh, pr=42, suite="infra", sha="abc1234",
                            command="npm run test:it", output=_ci_output(tmp_path))


def test_record_local_ci_rejects_non_hex_sha(tmp_path):
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="sha must be"):
        cmd_record_local_ci(gh, pr=42, suite="backend", sha="not-a-sha",
                            command="npm run test:it", output=_ci_output(tmp_path))


def test_merge_pr_refuses_when_backend_workflow_reported_no_check():
    from sdlc_next import GitHub, GhError, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps(
            [{"name": "gate", "bucket": "pass", "workflow": "sdlc-next Gate Auto-Advance"}]),
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "backend/src/x.ts\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
    })
    gh = GitHub(runner=runner)
    try:
        cmd_merge_pr(gh, 42, issue=9)
        assert False, "expected GhError"
    except GhError as e:
        assert "Backend CI" in str(e)
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_merge_pr_refuses_when_required_workflow_only_skipped():
    from sdlc_next import GitHub, GhError, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps(
            [{"name": "build", "bucket": "skipping", "workflow": "Backend CI"}]),
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "backend/src/x.ts\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
    })
    gh = GitHub(runner=runner)
    try:
        cmd_merge_pr(gh, 42, issue=9)
        assert False, "expected GhError"
    except GhError as e:
        assert "Backend CI" in str(e)
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_merge_pr_allows_docs_only_pr_with_no_checks():
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result == {"pr": 42, "issue": 9, "merged": True, "issue_closed": True, "config_changed": False,
                       "worktree": _NO_WORKTREE_RESULT}


def test_merge_pr_reports_config_changed_when_the_merged_pr_touched_the_pipeline_config():
    """merge-pr flags a merge that changed the pipeline config file."""
    from sdlc_next import GitHub, cmd_merge_pr, CONFIG_FILENAME
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            f"{CONFIG_FILENAME}\ndocs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is True
    assert result["config_changed"] is True
    # pr_files is fetched once and reused for both the merge gate and this flag.
    file_calls = [c for c in runner.calls
                  if c[:2] == ["gh", "api"] and "pulls/42/files" in " ".join(c)]
    assert len(file_calls) == 1


def test_merge_pr_refuses_as_structured_result_when_branch_behind_main():
    # Behind-main is a structured exit-0 refusal with no checks/ready/merge calls.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "2\n",
        # A suite-covered base delta, so the attestation is not carried forward.
        ("gh", "api", "repos/owner/repo/compare/issue-9...main", "--jq", ".files[]?.filename"):
            "backend/src/x.ts\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
    })
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is False
    assert result["behind_base"] == 2
    assert result["base"] == "main"
    assert "sync-branch" in result["reason"]
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in runner.calls)


def test_merge_pr_allows_when_both_required_workflows_pass():
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([
            {"name": "b", "bucket": "pass", "workflow": "Backend CI"},
            {"name": "f", "bucket": "pass", "workflow": "Frontend CI"},
        ]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "backend/src/x.ts\nfrontend/src/y.tsx\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result == {"pr": 42, "issue": 9, "merged": True, "issue_closed": True, "config_changed": False,
                       "worktree": _NO_WORKTREE_RESULT}


def test_pr_checks_reports_missing_required_workflows():
    from sdlc_next import GitHub, cmd_pr_checks
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps(
            [{"name": "gate", "bucket": "pass", "workflow": "sdlc-next Gate Auto-Advance"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "frontend/app/page.tsx\n",
        # No local-ci attestation on the PR -> frontend suite is genuinely missing.
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid,baseRefName"):
            json.dumps({"comments": [], "headRefOid": "abc1234", "baseRefName": "main"}),
    })
    gh = GitHub(runner=runner)
    result = cmd_pr_checks(gh, 42)
    assert result["status"] == "missing-checks"
    assert "Frontend CI" in result["missing_required_workflows"]
    assert "sync-branch" in result["hint"]  # distinguishes missing from "still running"


def test_pr_checks_local_ci_attestation_clears_missing_required_workflow():
    # A fresh local-ci attestation for the head reads as passed, not missing-checks.
    from sdlc_next import GitHub, cmd_pr_checks
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "frontend/app/page.tsx\n",
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid,baseRefName"): json.dumps({
            "comments": [{"body": "<!-- local-ci: frontend:42 @ abc1234def -->"}],
            "headRefOid": "abc1234def", "baseRefName": "main"}),
    })
    gh = GitHub(runner=runner)
    result = cmd_pr_checks(gh, 42)
    assert result["status"] == "passed"
    assert result["missing_required_workflows"] == []
    assert "hint" not in result


def test_open_dev_pr_creates_draft_pr_with_closes_and_sets_stage_field_to_pr_review():
    from sdlc_next import (GitHub, cmd_open_dev_pr, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            STAGE_FIELD_ID, STAGE_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['pr-review'])}")
    runner = ScriptedRunner({
        ("gh", "pr", "create", "--repo", "owner/repo", "--base", "main",
         "--head", "issue-9", "--title", "Add widget", "--body",
         "Implements the widget.\n\nCloses #9", "--draft"):
            "https://github.com/owner/repo/pull/42\n",
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        ("gh", "pr", "list", "--repo", "owner/repo", "--head", "issue-9",
         "--state", "open", "--json", "number,isDraft,headRefName,title,url"): "[]",
        tuple(_list_argv()): _list_response([_issue(9)]),
        # dev-PR scope check reads the branch's changed files vs its base (none here).
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".files[]?.filename"):
            "",
    })
    runner.prefix_responses = {
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_open_dev_pr(gh, issue=9, title="Add widget", body="Implements the widget.",
                              summary="Implemented per architecture.md.")
    assert result == {"issue": 9, "pr": 42, "created": True}
    body = next(c for c in runner.calls if c[:3] == ["gh", "issue", "comment"])[-1]
    # No queue marker: the PR must not be reviewable before its suites are attested.
    assert "development.md" not in body
    assert "stage-transition" not in body
    assert "record-local-ci" in body


def test_open_dev_pr_reuses_an_already_open_pr_instead_of_opening_a_duplicate():
    """A second open-dev-pr on the same branch reuses the open PR."""
    from sdlc_next import GitHub, cmd_open_dev_pr
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "pr", "list", "--repo", "owner/repo", "--head", "issue-9",
         "--state", "open", "--json", "number,isDraft,headRefName,title,url"):
            json.dumps([{"number": 41, "isDraft": True, "headRefName": "issue-9",
                         "title": "Add widget", "url": "https://github.com/x/y/pull/41"}]),
    })
    gh = GitHub(runner=runner)
    result = cmd_open_dev_pr(gh, issue=9, title="Add widget", body="Implements the widget.",
                              summary="Implemented per architecture.md.")
    assert result["pr"] == 41
    assert result["created"] is False
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in runner.calls)


def test_pass_gate_reconciles_comments_and_reclaims_next_stage():
    from sdlc_next import (GitHub, cmd_pass_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    import json
    git_runner = ScriptedRunner({
        **_live_wt("issue-9"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 92}, "labels": {"nodes": []},
        }}}}),
        # The parent is a standing epic, so #9 is not a phase-Task and claims onward.
        **dict([_epic_check(92, labels=("epic:standing",))]),
    })
    gh_runner.prefix_responses = {
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_pass_gate(gh, "/repo", issue=9, gate_pr=40, stage="product", runner=git_runner)
    assert result == {"issue": 9, "unit": "issue", "next_stage": "architecture", "claimed": True}
    assert len([c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"]]) == 2
    assert list(stage_mutation_argv) in gh_runner.calls
    assert list(status_mutation_argv) in gh_runner.calls


def test_pass_gate_refuses_when_stage_arg_does_not_match_gate_pr_marker():
    # A --stage that disagrees with the gate-pr marker is refused, not trusted.
    from sdlc_next import GitHub, GhError, cmd_pass_gate
    from tests.test_sdlc_next import ScriptedRunner
    import json
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:40 -->"}]}),
    })
    gh = GitHub(runner=gh_runner)
    try:
        cmd_pass_gate(gh, "/repo", issue=9, gate_pr=40, stage="product",
                       runner=ScriptedRunner())
        assert False, "expected GhError"
    except GhError as e:
        assert "architecture" in str(e) and "product" in str(e)


def test_pass_gate_refuses_when_gate_pr_arg_does_not_match_marker():
    from sdlc_next import GitHub, GhError, cmd_pass_gate
    from tests.test_sdlc_next import ScriptedRunner
    import json
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
    })
    gh = GitHub(runner=gh_runner)
    try:
        cmd_pass_gate(gh, "/repo", issue=9, gate_pr=99, stage="product",
                       runner=ScriptedRunner())
        assert False, "expected GhError"
    except GhError as e:
        assert "40" in str(e) and "99" in str(e)


def test_skip_gate_sets_fields_comments_and_reclaims_next_stage():
    from sdlc_next import (GitHub, cmd_skip_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['development'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    # The threshold comes from the parent epic's profile, so the parent is fetched too.
    epic_check_argv_92 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=92)}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 92}, "labels": {"nodes": []},
        }}}}),
        epic_check_argv_92: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None, "labels": {"nodes": []},
        }}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "issue", "comment", "9"): "",
        # claim(development) refuses when the branch has an open PR.
        ("gh", "pr", "list", "--repo", "owner/repo", "--head", "issue-9"): "[]",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_skip_gate(gh, issue=9, stage="architecture", confidence=97,
                            summary="No findings.")
    assert result == {"issue": 9, "unit": "issue", "next_stage": "development", "skipped": True, "confidence": 97}
    assert list(stage_mutation_argv) in gh_runner.calls
    assert list(status_mutation_argv) in gh_runner.calls
    comment_calls = [c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"]]
    assert len(comment_calls) == 2
    comment_call = comment_calls[0]
    body = comment_call[comment_call.index("--body") + 1]
    assert "97%" in body
    assert "<!-- arch-review-confidence: 97 -->" in body
    assert re.search(
        r"<!-- stage-transition: arch-review->development @ "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->$", body)


def test_skip_gate_refuses_below_threshold(monkeypatch):
    import sdlc_next
    from sdlc_next import GitHub, GhError, cmd_skip_gate, _ISSUE_EPIC_CHECK_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    # Default-profile epic at threshold 95; confidence 95 does not clear it.
    monkeypatch.setitem(sdlc_next.PIPELINE, "gates",
                        {"skipConfidenceThreshold": 95, "requiresHumanGateA": True})
    epic_check_9 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    epic_check_92 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=92)}")
    gh = GitHub(runner=ScriptedRunner({
        epic_check_9: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 92}, "labels": {"nodes": []}}}}}),
        epic_check_92: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None, "labels": {"nodes": []}}}}}),
    }))
    try:
        cmd_skip_gate(gh, issue=9, stage="architecture", confidence=95, summary="x")
        assert False, "expected GhError"
    except GhError as e:
        assert "threshold" in str(e)


def test_skip_gate_threshold_is_per_profile(monkeypatch):
    # A profile's own threshold (90 here) refuses confidence 90.
    import sdlc_next
    from sdlc_next import GitHub, GhError, cmd_skip_gate, _ISSUE_EPIC_CHECK_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    monkeypatch.setitem(sdlc_next.PIPELINE, "profiles", [
        {"name": "strict", "match": {"label": "epic:strict"},
         "gates": {"skipConfidenceThreshold": 90}}, *sdlc_next.PIPELINE["profiles"]])

    epic_check_9 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    epic_check_94 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=94)}")
    responses = {
        epic_check_9: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 94},
            "labels": {"nodes": []}}}}}),
        epic_check_94: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None,
            "labels": {"nodes": [{"name": "epic:strict"}]}}}}}),
    }
    # At 90 -> refused (90 <= 90); raises before any comment/claim.
    gh = GitHub(runner=ScriptedRunner(dict(responses)))
    try:
        cmd_skip_gate(gh, issue=9, stage="architecture", confidence=90, summary="x")
        assert False, "expected GhError at the profile threshold"
    except GhError as e:
        assert "90 threshold" in str(e)


def test_skip_gate_refuses_for_product_stage():
    from sdlc_next import GitHub, GhError, cmd_skip_gate
    from tests.test_sdlc_next import ScriptedRunner
    gh = GitHub(runner=ScriptedRunner())
    try:
        cmd_skip_gate(gh, issue=9, stage="product", confidence=99, summary="x")
        assert False, "expected GhError"
    except GhError as e:
        assert "architecture" in str(e)


# --- Epic-level stages ---

def test_is_epic_legacy_and_is_epic_architected_read_their_labels():
    from sdlc_next import is_epic_legacy, is_epic_architected
    assert is_epic_legacy({"labels": [{"name": "epic:legacy"}]}) is True
    assert is_epic_legacy({"labels": []}) is False
    assert is_epic_architected({"labels": [{"name": "epic:architected"}]}) is True
    assert is_epic_architected({"labels": []}) is False


def test_non_architected_epic_holds_its_stageless_tasks():
    # Before epic:architected, a Stage-less child waits on merge-lld-doc: not delegated or staged.
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent")
    issues = [epic_92, _issue(101, parent=92)]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 92) == {"action": "none", "epic": 92}


def test_standing_epic_hands_out_a_fresh_child_without_being_architected():
    # A standing epic's Stage-less child is routed at pickup, not held for `epic:architected`.
    from sdlc_next import GitHub, decide_next_action
    epic_94 = _epic(94, labels=["epic:standing"])
    issues = [epic_94, _issue(101, parent=94)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(101))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 94) == {"action": "route", "issue": 101, "unit": "issue"}


def test_architected_epic_reports_a_late_stageless_child_as_unstaged():
    # A late Stage-less child of an architected Epic is reported, never staged.
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent", labels=["epic:architected"])
    issues = [epic_92, _issue(101, parent=92)]
    runner = ScriptedRunner({tuple(_list_argv()): _list_response(issues)})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 92)
    assert result["action"] == "none" and result["unstaged"] == [101]
    assert "set-stage" in result["unstaged_reason"]
    assert all(c[:3] != ["gh", "api", "graphql"] or "updateIssueFieldValue" not in c[-1]
               for c in runner.calls)


def test_epic_mid_second_gate_round_is_still_checked_even_though_architected():
    # An architected epic with a reopened gate round still gets its pending-gate check.
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent", labels=["epic:architected"],
                     stage="architecture", status="awaiting-human-review")
    issues = [epic_92]
    responses = {
        tuple(_list_argv()): _list_response(issues),
        ("gh", "issue", "view", "92", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:140 -->"}]}),
        ("gh", "pr", "view", "140", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "OPEN", "mergedAt": None}),
        ("gh", "pr", "view", "140", "--repo", "owner/repo", "--json", "comments,createdAt"):
            json.dumps({"createdAt": "2026-08-16T00:00:00Z", "comments": []}),
    }
    from sdlc_next import _UNRESOLVED_THREADS_QUERY
    responses[tuple(["gh", "api", "graphql", "-f",
                      f"query={_UNRESOLVED_THREADS_QUERY.format(pr=140)}"])] = json.dumps(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 92) == {"action": "none", "epic": 92}  # not_satisfied, no other work


def test_open_gate_unit_issue_phase_task_opens_its_own_branch_against_main():
    # A phase-Task's gate is a plain `unit="issue"` gate: issue-<n> -> main.
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "fetch", "origin"): "",
                                 ("git", "-C", "/repo", "rev-parse", "origin/issue-40"): "cafe1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=40)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_40', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_40"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 40}}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/500\n",
        ("gh", "issue", "comment", "40"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 40, "Product Roadmap Task", "product.md",
                            "architecture", "Locked Initiative-level requirements.",
                            runner=git_runner)
    assert result == {"issue": 40, "unit": "issue", "gate_pr": 500, "stage": "product",
                      "sha": "cafe1234", "head": "issue-40", "base": "main"}
    pr_create_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "pr", "create"])
    assert pr_create_call[pr_create_call.index("--base") + 1] == "main"
    assert pr_create_call[pr_create_call.index("--head") + 1] == "issue-40"
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "docs/sdlc/issue-40/product.md" in body


def test_open_gate_targets_main_from_issue_branch():
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "fetch", "origin"): "",
                                 ("git", "-C", "/repo", "rev-parse", "origin/issue-9"): "abcd1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        **dict([_epic_check(9)]),  # parentless: not an Epic's phase-Task
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/41\n",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 9, "Fix widget", "architecture.md", "development",
                            "Design locked.", runner=git_runner)
    assert result["head"] == "issue-9"
    assert result["base"] == "main"


def test_pause_for_epic_regate_parks_the_unit_at_todo_without_touching_stage():
    from sdlc_next import GitHub, cmd_pause_for_epic_regate, _ISSUE_NODE_ID_QUERY, \
        _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=101)}")
    set_status_argv = ("gh", "api", "graphql", "-f", "query=" + _SET_ISSUE_FIELD_MUTATION.format(
        issue_id="ISSUE_101", field_id=PIPELINE_STATUS_FIELD_ID,
        option_id=PIPELINE_STATUS_OPTION_IDS["todo"]))
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_101"}}}}),
        set_status_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 101}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "101"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_pause_for_epic_regate(gh, 101, epic=92, gate_pr=141)
    assert result == {"issue": 101, "paused_for_epic_regate": 92, "gate_pr": 141,
                      "found_by": "lld"}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "#92" in body and "#141" in body


def test_pause_for_epic_regate_names_the_finding_stage():
    # Regression: a deviation found at pr-review was reported as "`lld` found".
    from sdlc_next import GitHub, cmd_pause_for_epic_regate, _ISSUE_NODE_ID_QUERY, \
        _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    gh_runner = ScriptedRunner({
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=101)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_101"}}}}),
        ("gh", "api", "graphql", "-f", "query=" + _SET_ISSUE_FIELD_MUTATION.format(
            issue_id="ISSUE_101", field_id=PIPELINE_STATUS_FIELD_ID,
            option_id=PIPELINE_STATUS_OPTION_IDS["todo"])):
            json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 101}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "101"): ""}
    result = cmd_pause_for_epic_regate(GitHub(runner=gh_runner), 101, epic=92, gate_pr=141,
                                       found_by="pr-review")
    assert result["found_by"] == "pr-review"
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "`pr-review` found" in body and "`lld` found" not in body


# --- auto-pass-gate (gate-auto-advance webhook) ---

def test_auto_pass_gate_skips_still_open_pr():
    # A still-open PR is a clean skip.
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "OPEN", "mergedAt": None, "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not closed" in result["skipped"]


def test_auto_pass_gate_skips_non_main_base():
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "develop",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z", "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not main" in result["skipped"]


def test_auto_pass_gate_skips_non_gate_branch():
    # Only `issue-<n>` heads are gates; anything else skips.
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "docs-tidy", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z", "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not an issue-<n> branch" in result["skipped"]


def test_auto_pass_gate_skips_bare_epic_branch_head_which_is_the_integration_pr():
    # An `epic-<n>` head is close-epic's integration merge: skip before any issue lookup.
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "epic-92", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-09-06T10:00:00Z", "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not an issue-<n> branch" in result["skipped"]
    assert not any(c[:3] == ["gh", "issue", "view"] for c in gh_runner.calls)


def test_auto_pass_gate_skips_dev_pr_carrying_closes_hash():
    # A "Closes #<n>" body marks a dev PR, not a gate: clean skip.
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 42, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z",
                        "body": "Implements the widget.\n\nCloses #9"}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 42)
    assert result["ok"] is True
    assert "Closes #" in result["skipped"]


def test_auto_pass_gate_skips_when_issue_not_awaiting_human_review():
    from sdlc_next import GitHub, cmd_auto_pass_gate, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z", "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "In Progress"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not currently" in result["skipped"]


def test_auto_pass_gate_skips_when_no_gate_marker():
    from sdlc_next import GitHub, cmd_auto_pass_gate, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z", "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "no gate-pr marker" in result["skipped"]


def test_auto_pass_gate_skips_when_marker_points_at_a_different_pr():
    # A marker naming a different PR means this gate is superseded: skip, don't mutate.
    from sdlc_next import GitHub, cmd_auto_pass_gate, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z", "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:41 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "#41" in result["skipped"] and "#40" in result["skipped"]


def test_auto_pass_gate_dispatches_to_pass_gate_for_matching_open_gate():
    # CI advances the Stage but does not claim or post a start comment (live=False).
    from sdlc_next import (GitHub, cmd_auto_pass_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_FIELDS_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS, REPO)
    git_runner = ScriptedRunner({
        **_live_wt("issue-9"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_todo_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['todo'])}")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "MERGED", "mergedAt": "2026-08-16T10:00:00Z",
                        "body": "Doc-only review gate for #9 -- see SKILL.md."}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_todo_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        # No parent: not a phase-Task, so it advances to its next stage.
        **dict([_epic_check(9)]),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40, runner=git_runner)
    assert result == {"issue": 9, "unit": "issue", "next_stage": "architecture",
                       "claimed": False, "ok": True}
    assert len([c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"]]) == 1


def test_auto_pass_gate_marks_needs_human_when_matching_gate_pr_closed_without_merging():
    # A gate PR closed without merging parks the issue needs-human with a comment.
    from sdlc_next import (GitHub, cmd_auto_pass_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_FIELDS_QUERY, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS, REPO)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['needs-human'])}")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "CLOSED", "mergedAt": None,
                        "body": "Doc-only review gate for #9 -- see SKILL.md."}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result == {"issue": 9, "status": "needs-human", "ok": True,
                       "worktree": _NO_WORKTREE_RESULT}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "closed without merging" in body and "#40" in body


def test_auto_pass_gate_closed_without_merge_still_skips_cleanly_on_no_match():
    # The closed-without-merge path shares _match_open_gate's skips (marker mismatch shown).
    from sdlc_next import GitHub, cmd_auto_pass_gate, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "CLOSED", "mergedAt": None, "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:41 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "#41" in result["skipped"] and "#40" in result["skipped"]


def test_auto_pass_gate_reports_ok_false_on_genuine_gh_failure():
    from sdlc_next import GitHub, GhError, cmd_auto_pass_gate

    def failing_runner(argv):
        raise GhError("boom")

    gh = GitHub(runner=failing_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is False
    assert "boom" in result["reason"]


def test_main_exits_nonzero_when_auto_pass_gate_reports_ok_false(monkeypatch):
    # main() exits 1 on an explicit "ok": False result, not only on a raised GhError.
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(sdlc_next, "cmd_auto_pass_gate",
                         lambda gh, repo_path, pr: {"ok": False, "reason": "simulated"})
    exit_code = sdlc_next.main(["auto-pass-gate", "--pr", "40"])
    assert exit_code == 1


# --- mark-feedback-received (review/comment webhook) ---

def test_mark_feedback_received_skips_bot_author():
    from sdlc_next import GitHub, cmd_mark_feedback_received
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_mark_feedback_received(gh, 40, author="github-actions[bot]", body="looks good")
    assert result == {"ok": True, "skipped": "comment/review author 'github-actions[bot]' is a bot"}


def test_mark_feedback_received_skips_empty_body():
    # An empty body carries no feedback and must not flip the field.
    from sdlc_next import GitHub, cmd_mark_feedback_received
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="   ")
    assert result["ok"] is True
    assert "no body text" in result["skipped"]


def test_mark_feedback_received_skips_when_not_a_tracked_gate():
    # Shares _match_open_gate's skips (non-issue head shown).
    from sdlc_next import GitHub, cmd_mark_feedback_received, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "docs-tidy", "baseRefName": "main",
                        "state": "OPEN", "mergedAt": None, "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="please fix this")
    assert result["ok"] is True
    assert "not an issue-<n> branch" in result["skipped"]


def test_mark_feedback_received_skips_when_already_feedback_received():
    # Already Feedback Received: clean no-op, no second comment.
    from sdlc_next import GitHub, cmd_mark_feedback_received, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "OPEN", "mergedAt": None, "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Feedback Received"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="one more thing")
    assert result["ok"] is True
    assert "nothing to flip forward" in result["skipped"]
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_mark_feedback_received_skips_when_needs_human():
    # Never clobbers needs-human.
    from sdlc_next import GitHub, cmd_mark_feedback_received, _ISSUE_FIELDS_QUERY, REPO
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "OPEN", "mergedAt": None, "body": ""}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Needs Human"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="one more thing")
    assert result["ok"] is True
    assert "not currently awaiting-human-review or feedback-received" in result["skipped"]


def test_mark_feedback_received_flips_field_and_posts_comment_on_happy_path():
    from sdlc_next import (GitHub, cmd_mark_feedback_received, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, _ISSUE_FIELDS_QUERY,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS, REPO)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['feedback-received'])}")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "issue-9", "baseRefName": "main",
                        "state": "OPEN", "mergedAt": None,
                        "body": "Doc-only review gate for #9 -- see SKILL.md."}),
        ("gh", "issue", "view", "9", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="please tweak the wording")
    assert result == {"ok": True, "issue": 9, "gate_pr": 40, "pipeline_status": "feedback-received"}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "gate PR #40" in body and "Feedback Received" in body


def test_mark_feedback_received_reports_ok_false_on_genuine_gh_failure():
    from sdlc_next import GitHub, GhError, cmd_mark_feedback_received

    def failing_runner(argv):
        raise GhError("boom")

    gh = GitHub(runner=failing_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="feedback")
    assert result["ok"] is False
    assert "boom" in result["reason"]


def test_main_dispatches_mark_feedback_received_with_parsed_args(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    captured = {}

    def fake(gh, pr, author, body):
        captured.update(pr=pr, author=author, body=body)
        return {"ok": True, "skipped": "test"}

    monkeypatch.setattr(sdlc_next, "cmd_mark_feedback_received", fake)
    exit_code = sdlc_next.main(["mark-feedback-received", "--pr", "40",
                                 "--author", "maintainer", "--body", "fix the typo"])
    assert exit_code == 0
    assert captured == {"pr": 40, "author": "maintainer", "body": "fix the typo"}


# --- mark-feedback-addressed ---

def test_mark_feedback_addressed_sets_status_back_to_awaiting_human_review():
    from sdlc_next import (GitHub, cmd_mark_feedback_addressed, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_addressed(gh, 9)
    assert result == {"issue": 9, "pipeline_status": "awaiting-human-review"}
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


# --- mark-todo ---

def test_mark_todo_sets_status_when_none_set():
    from sdlc_next import (GitHub, cmd_mark_todo, _ISSUE_FIELDS_QUERY, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS)
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['todo'])}")
    gh_runner = ScriptedRunner({
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": []}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_todo(gh, 9)
    assert result == {"issue": 9, "pipeline_status": "todo"}


def test_mark_todo_skips_cleanly_when_status_already_set():
    from sdlc_next import GitHub, cmd_mark_todo, _ISSUE_FIELDS_QUERY
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"}, "name": "In Progress"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_todo(gh, 9)
    assert result == {"issue": 9, "skipped": "Pipeline Status already 'in-progress'"}


def test_cli_mark_todo_dispatches(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    captured = {}

    def fake(gh, issue):
        captured["issue"] = issue
        return {"issue": issue, "pipeline_status": "todo"}

    monkeypatch.setattr(sdlc_next, "cmd_mark_todo", fake)
    exit_code = sdlc_next.main(["mark-todo", "9"])
    assert exit_code == 0
    assert captured == {"issue": 9}


# --- Parallel implementation lane: footprint parsing, worktree detection, conflict handling ---

def test_parse_footprint_extracts_bulleted_backtick_paths():
    from sdlc_next import parse_footprint
    doc = (
        "# Some doc\n\n"
        "## Footprint\n\n"
        "- `backend/src/notifications/**`\n"
        "- `frontend/src/lib/api/core.ts` — adds `clientFetchBlob`; sole owner\n\n"
        "## Next section\n\n- `should/not/appear`\n"
    )
    assert parse_footprint(doc) == [
        "backend/src/notifications/**",
        "frontend/src/lib/api/core.ts",
    ]


def test_parse_footprint_returns_empty_when_no_heading():
    from sdlc_next import parse_footprint
    assert parse_footprint("# Doc\n\nNo footprint section here.\n") == []


def test_footprint_overlaps_true_for_directory_prefix():
    from sdlc_next import footprint_overlaps
    assert footprint_overlaps(["backend/src/notifications/**"],
                               ["backend/src/notifications/service.ts"])


def test_footprint_overlaps_false_for_disjoint_paths():
    from sdlc_next import footprint_overlaps
    assert not footprint_overlaps(["backend/src/notifications/**"],
                                   ["frontend/src/lib/api/core.ts"])


def test_active_worktree_branches_parses_porcelain_output_and_excludes_detached():
    from sdlc_next import active_worktree_branches
    porcelain = (
        "worktree /repo\nHEAD abc123\nbranch refs/heads/main\n\n"
        "worktree /tmp/sdlc-dev-185\nHEAD def456\nbranch refs/heads/issue-185\n\n"
        "worktree /tmp/sdlc-review-200\nHEAD ghi789\ndetached\n\n"
    )
    runner = ScriptedRunner({("git", "-C", "/repo", "worktree", "list", "--porcelain"): porcelain})
    assert active_worktree_branches("/repo", runner=runner) == {"main", "issue-185"}


def test_read_footprint_falls_back_to_architecture_md_when_no_lld():
    from sdlc_next import read_footprint
    lld_argv = ("git", "-C", "/repo", "show", "origin/issue-9:docs/sdlc/issue-9/lld.md")
    arch_argv = ("git", "-C", "/repo", "show", "origin/issue-9:docs/sdlc/issue-9/architecture.md")
    runner = ScriptedRunner({arch_argv: "## Footprint\n\n- `a/b`\n"})
    runner.fail_on = {lld_argv}
    assert read_footprint("/repo", 9, runner=runner) == ["a/b"]


def test_git_reconcile_branch_raises_merge_conflict_and_aborts_on_real_conflict():
    from sdlc_next import git_reconcile_branch, MergeConflict
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "diff", "--name-only", "--diff-filter=U"): "src/a.ts\nsrc/b.ts\n",
        ("git", "-C", "/repo", "merge", "--abort"): "",
    })
    runner.fail_on = {("git", "-C", "/repo", "merge", "origin/main")}
    try:
        git_reconcile_branch("/repo", "issue-9", runner=runner)
        assert False, "expected MergeConflict"
    except MergeConflict as e:
        assert e.files == ["src/a.ts", "src/b.ts"]
    calls = [tuple(c) for c in runner.calls]
    assert ("git", "-C", "/repo", "merge", "--abort") in calls
    assert ("git", "-C", "/repo", "push", "origin", "issue-9") not in calls


def test_git_reconcile_branch_reraises_plain_ghererror_when_no_unmerged_paths():
    from sdlc_next import git_reconcile_branch, MergeConflict, GhError
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "diff", "--name-only", "--diff-filter=U"): "",
    })
    runner.fail_on = {("git", "-C", "/repo", "merge", "origin/main")}
    try:
        git_reconcile_branch("/repo", "issue-9", runner=runner)
        assert False, "expected GhError"
    except MergeConflict:
        assert False, "not a content conflict -- should not be MergeConflict"
    except GhError:
        pass


def test_sync_branch_returns_conflict_result_without_raising_and_posts_marker():
    # A conflict is an exit-0 result and is persisted as a sync-conflict marker comment.
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        **_live_wt("issue-9"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "diff", "--name-only", "--diff-filter=U"): "src/a.ts\n",
        ("git", "-C", "/repo", "merge", "--abort"): "",
    })
    runner.fail_on = {("git", "-C", "/repo", "merge", "origin/main")}
    gh_runner = ScriptedRunner({tuple(_list_argv()): _list_response([_issue(9)])})
    gh_runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_sync_branch(gh, "/repo", 9, runner=runner)
    assert result == {"issue": 9, "unit": "issue", "branch": "issue-9", "base": "main", "synced": False,
                       "conflict": True, "conflicting_files": ["src/a.ts"]}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "<!-- sync-conflict: issue-9" in body and "src/a.ts" in body


def test_sync_branch_success_posts_no_comment():
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        **_live_wt("issue-9"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    gh_runner = ScriptedRunner({tuple(_list_argv()): _list_response([_issue(9)])})
    gh = GitHub(runner=gh_runner)
    result = cmd_sync_branch(gh, "/repo", 9, runner=runner)
    assert result == {"issue": 9, "unit": "issue", "branch": "issue-9", "base": "main", "synced": True}
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_sync_branch_auto_resolves_worktree_when_repo_path_omitted():
    # Without --repo-path, the branch's own live worktree is used.
    from sdlc_next import GitHub, cmd_sync_branch
    porcelain = (
        "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /tmp/sdlc-dev-9\nHEAD def\nbranch refs/heads/issue-9\n"
    )
    runner = ScriptedRunner({
        ("git", "-C", ".", "worktree", "list", "--porcelain"): porcelain,
        ("git", "-C", "/tmp/sdlc-dev-9", "fetch", "origin"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/main"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "checkout", "issue-9"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-9"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "merge", "--ff-only", "origin/issue-9"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "merge", "origin/main"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "push", "origin", "issue-9"): "",
    })
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([_issue(9)])}))
    result = cmd_sync_branch(gh, None, 9, runner=runner)
    assert result["synced"] is True


def test_resolve_repo_path_explicit_wins_and_falls_back_to_dot():
    from sdlc_next import resolve_repo_path
    porcelain = "worktree /repo\nHEAD abc\nbranch refs/heads/main\n"
    runner = ScriptedRunner({("git", "-C", ".", "worktree", "list", "--porcelain"): porcelain})
    assert resolve_repo_path("/explicit", "issue-9", runner=runner) == "/explicit"
    assert runner.calls == []  # explicit path never even lists worktrees
    assert resolve_repo_path(None, "issue-9", runner=runner) == "."


def _advance_to_development_responses(number):
    """Scripted field writes `merge-lld-doc` makes once the doc is on origin (Stage ->
    Development, Pipeline Status Todo). Returns (responses, stage_argv, status_argv)."""
    from sdlc_next import (_ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID,
                            STAGE_OPTION_IDS, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=number)}")
    stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id=f'ISSUE_{number}', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['development'])}")
    status_argv = ("gh", "api", "graphql", "-f", "query=" + _SET_ISSUE_FIELD_MUTATION.format(
        issue_id=f"ISSUE_{number}", field_id=PIPELINE_STATUS_FIELD_ID,
        option_id=PIPELINE_STATUS_OPTION_IDS["todo"]))
    responses = {
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": f"ISSUE_{number}"}}}}),
        stage_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": number}}}}),
        status_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": number}}}}),
    }
    return responses, stage_argv, status_argv


def test_next_action_picks_a_development_task_by_sort_key():
    # Stage=development with no Pipeline Status is a plain delegate, ranked by sort_key.
    from sdlc_next import GitHub, decide_next_action
    epic = _epic(110, labels=["epic:architected"])
    newer = _issue(185, stage="development", parent=110, created="2026-08-02T00:00:00Z")
    older = _issue(186, stage="development", parent=110, created="2026-08-01T00:00:00Z")
    responses = {tuple(_list_argv()): _list_response([epic, newer, older])}
    responses.update(_no_blockers_responses(185, 186))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 110) == {"action": "delegate", "issue": 186, "unit": "issue",
                                            "stage": "development"}


def test_next_action_resumes_development_after_a_crash_between_the_two_advance_writes():
    # A crash between the Stage write and the status clear resumes development, never a fresh lld.
    from sdlc_next import GitHub, decide_next_action
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="development", status="in-progress", parent=110)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    assert decide_next_action(gh, 110) == {"action": "resume", "issue": 185, "unit": "issue",
                                            "stage": "development"}


# --- Concurrent multi-epic isolation: branch locks and ephemeral workspaces ---

def test_branch_lock_is_exclusive_per_branch_and_released_on_exit():
    from sdlc_next import branch_lock, BranchLocked
    import pytest
    with branch_lock("epic-1") as path:
        assert path.endswith("epic-1.lock")
        with pytest.raises(BranchLocked):
            with branch_lock("epic-1", wait_seconds=0):
                pass
        # A different branch never contends.
        with branch_lock("epic-2", wait_seconds=0):
            pass
    with branch_lock("epic-1", wait_seconds=0):
        pass


def test_branch_workspace_uses_live_non_main_worktree_and_leaves_it():
    from sdlc_next import BranchWorkspace
    runner = ScriptedRunner({**_live_wt("issue-9", path="/tmp/sdlc-dev-9")})
    with BranchWorkspace("issue-9", "/tmp/sdlc-dev-9", runner) as ws:
        assert ws.path == "/tmp/sdlc-dev-9" and ws.ephemeral is False
    assert ws.retained is None
    assert not any(c[3] == "worktree" and c[4] in ("add", "remove") for c in runner.calls)


def test_branch_workspace_creates_ephemeral_worktree_from_origin_and_removes_it():
    from sdlc_next import BranchWorkspace, PIPELINE
    import os
    path = f"/tmp/sdlc-tmp-epic-365-{os.getpid()}"
    runner = ScriptedRunner({
        ("git", "-C", "/main", "worktree", "list", "--porcelain"):
            "worktree /main\nHEAD aaa\nbranch refs/heads/main\n",
        ("git", "-C", "/main", "fetch", "origin"): "",
        ("git", "-C", "/main", "show-ref", "--verify", "--quiet", "refs/remotes/origin/epic-365"): "",
        ("git", "-C", "/main", "worktree", "add", path, "-B", "epic-365", "origin/epic-365"): "",
        ("git", "-C", path, "status", "--porcelain"): "",
        ("git", "-C", path, "log", "--oneline", "origin/epic-365..epic-365"): "",
        ("git", "-C", "/main", "worktree", "remove", path): "",
    })
    runner.fail_on = {("git", "-C", "/main", "rev-parse", "--verify", "--quiet", "refs/heads/epic-365")}
    with BranchWorkspace("epic-365", "/main", runner) as ws:
        assert ws.path == path and ws.ephemeral is True
    assert ["git", "-C", "/main", "worktree", "remove", path] in runner.calls
    assert ws.retained is None
    # Nothing ever ran `git checkout` in the main checkout.
    assert not any(c[:3] == ["git", "-C", "/main"] and c[3] == "checkout" for c in runner.calls)


def test_branch_workspace_retains_ephemeral_worktree_with_unpushed_commits():
    from sdlc_next import BranchWorkspace
    import os
    path = f"/tmp/sdlc-tmp-epic-365-{os.getpid()}"
    runner = ScriptedRunner({
        ("git", "-C", "/main", "worktree", "list", "--porcelain"):
            "worktree /main\nHEAD aaa\nbranch refs/heads/main\n",
        ("git", "-C", "/main", "fetch", "origin"): "",
        ("git", "-C", "/main", "show-ref", "--verify", "--quiet", "refs/remotes/origin/epic-365"): "",
        ("git", "-C", "/main", "worktree", "add", path, "-B", "epic-365", "origin/epic-365"): "",
        ("git", "-C", path, "status", "--porcelain"): "",
        ("git", "-C", path, "log", "--oneline", "origin/epic-365..epic-365"): "abc unpushed\n",
    })
    runner.fail_on = {("git", "-C", "/main", "rev-parse", "--verify", "--quiet", "refs/heads/epic-365")}
    with BranchWorkspace("epic-365", "/main", runner) as ws:
        pass
    assert ws.retained == path
    assert ["git", "-C", "/main", "worktree", "remove", path] not in runner.calls


def test_branch_workspace_refuses_when_local_ref_has_unpushed_commits_and_no_worktree():
    from sdlc_next import BranchWorkspace, GhError
    import pytest
    runner = ScriptedRunner({
        ("git", "-C", "/main", "worktree", "list", "--porcelain"):
            "worktree /main\nHEAD aaa\nbranch refs/heads/main\n",
        ("git", "-C", "/main", "fetch", "origin"): "",
        ("git", "-C", "/main", "show-ref", "--verify", "--quiet", "refs/remotes/origin/epic-365"): "",
        ("git", "-C", "/main", "rev-parse", "--verify", "--quiet", "refs/heads/epic-365"): "abc\n",
        ("git", "-C", "/main", "log", "--oneline", "origin/epic-365..epic-365"): "abc wip\n",
    })
    with pytest.raises(GhError) as exc:
        with BranchWorkspace("epic-365", "/main", runner):
            pass
    assert "not on origin/epic-365" in str(exc.value)
    assert not any(c[3] == "worktree" and c[4] == "add" for c in runner.calls)


def test_sync_branch_with_no_live_worktree_runs_in_an_ephemeral_one():
    # Nothing holds epic-92: reconcile in an ephemeral worktree; the main checkout is never touched.
    from sdlc_next import GitHub, cmd_sync_branch
    import os
    path = f"/tmp/sdlc-tmp-epic-92-{os.getpid()}"
    runner = ScriptedRunner({
        ("git", "-C", "/main", "worktree", "list", "--porcelain"):
            "worktree /main\nHEAD aaa\nbranch refs/heads/main\n",
        ("git", "-C", "/main", "fetch", "origin"): "",
        ("git", "-C", "/main", "show-ref", "--verify", "--quiet", "refs/remotes/origin/epic-92"): "",
        ("git", "-C", "/main", "worktree", "add", path, "-B", "epic-92", "origin/epic-92"): "",
        ("git", "-C", path, "fetch", "origin"): "",
        ("git", "-C", path, "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): "",
        ("git", "-C", path, "checkout", "epic-92"): "",
        ("git", "-C", path, "show-ref", "--verify", "--quiet", "refs/remotes/origin/epic-92"): "",
        ("git", "-C", path, "merge", "--ff-only", "origin/epic-92"): "",
        ("git", "-C", path, "merge", "origin/main"): "",
        ("git", "-C", path, "push", "origin", "epic-92"): "",
        ("git", "-C", path, "status", "--porcelain"): "",
        ("git", "-C", path, "log", "--oneline", "origin/epic-92..epic-92"): "",
        ("git", "-C", "/main", "worktree", "remove", path): "",
    })
    runner.fail_on = {("git", "-C", "/main", "rev-parse", "--verify", "--quiet", "refs/heads/epic-92")}
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_sync_branch(gh, "/main", 92, unit="epic", runner=runner)
    assert result == {"issue": 92, "unit": "epic", "branch": "epic-92", "base": "main",
                      "synced": True}
    assert not any(c[:3] == ["git", "-C", "/main"] and c[3] in ("checkout", "merge", "push")
                   for c in runner.calls)


# --- Concurrent multi-epic isolation: per-epic runtime stack ---

def _enable_stack(monkeypatch, tmp_path):
    from sdlc_next import PIPELINE
    stack = dict(PIPELINE["stack"])
    stack.update({"enabled": True, "workspaceRoot": str(tmp_path), "seedCommand": "make seed PROFILE={profile}"})
    monkeypatch.setitem(PIPELINE, "stack", stack)
    (tmp_path / ".env.dev").write_text(
        "FRONTEND_PORT=3000\nBACKEND_PORT=3001\nexport DB_PORT_EXPOSE=5432\nBACKEND_DEBUG_PORT=9229\n"
        "COMPOSE_PROJECT_NAME=singlasoft\nPOSTGRES_DATA_DIR=.docker/postgres-data\nOTHER=keep\n")
    (tmp_path / ".secrets.dev").write_text("SECRET=xyz\n")
    return stack


def test_provision_epic_stack_generates_isolated_profile_and_runs_up_and_seed(monkeypatch, tmp_path):
    from sdlc_next import cmd_provision_epic_stack
    _enable_stack(monkeypatch, tmp_path)
    ran = []
    result = cmd_provision_epic_stack(159, shell=lambda c, cwd: ran.append((c, cwd)) or "",
                                      probe=lambda p: True)
    assert result["provisioned"] is True and result["created"] is True
    assert result["profile"] == "epic159" and result["project"] == "sdlc-epic159"
    env = (tmp_path / ".env.epic159").read_text()
    ports = result["ports"]
    # Every port is offset from the base profile -- never the shared dev ports.
    assert ports["FRONTEND_PORT"] != 3000 and ports["BACKEND_DEBUG_PORT"] != 9229
    assert ports["BACKEND_PORT"] - ports["FRONTEND_PORT"] == 1
    assert f"BACKEND_DEBUG_PORT={ports['BACKEND_DEBUG_PORT']}" in env
    assert "export DB_PORT_EXPOSE=5432" not in env and f"DB_PORT_EXPOSE={ports['DB_PORT_EXPOSE']}" in env
    assert "COMPOSE_PROJECT_NAME=sdlc-epic159" in env
    assert "POSTGRES_DATA_DIR=.docker/postgres-data-epic159" in env
    assert "OTHER=keep" in env
    assert (tmp_path / ".secrets.epic159").read_text() == "SECRET=xyz\n"
    assert (tmp_path / ".docker/postgres-data-epic159").is_dir()
    assert [c for c, _ in ran] == [
        "COMPOSE_PROJECT_NAME=sdlc-epic159 make fullstack-d PROFILE=epic159",
        "make seed PROFILE=epic159"]
    assert all(cwd == str(tmp_path) for _, cwd in ran)


def test_provision_epic_stack_is_idempotent_and_reads_ports_back(monkeypatch, tmp_path):
    from sdlc_next import cmd_provision_epic_stack
    _enable_stack(monkeypatch, tmp_path)
    first = cmd_provision_epic_stack(159, shell=lambda c, cwd: "", probe=lambda p: True)
    ran = []
    second = cmd_provision_epic_stack(159, shell=lambda c, cwd: ran.append(c) or "",
                                      probe=lambda p: False)  # probe never consulted
    assert second["created"] is False and second["ports"] == first["ports"]
    assert len(ran) == 2  # up + seed re-run; profile untouched


def test_provision_epic_stack_bumps_ports_when_a_slot_is_taken(monkeypatch, tmp_path):
    from sdlc_next import pick_stack_ports, PIPELINE
    _enable_stack(monkeypatch, tmp_path)
    stride = PIPELINE["stack"]["portStride"]
    first = pick_stack_ports(159, probe=lambda p: True)
    bumped = pick_stack_ports(159, probe=lambda p: p != first["BACKEND_PORT"])
    assert bumped["BACKEND_PORT"] == first["BACKEND_PORT"] + stride


def test_provision_and_teardown_are_noops_when_stack_disabled():
    from sdlc_next import cmd_provision_epic_stack, cmd_teardown_epic_stack
    calls = []
    assert cmd_provision_epic_stack(1, shell=lambda c, cwd: calls.append(c))["provisioned"] is False
    assert cmd_teardown_epic_stack(1, shell=lambda c, cwd: calls.append(c))["torn_down"] is False
    assert calls == []


def test_teardown_epic_stack_downs_and_removes_profile_and_data(monkeypatch, tmp_path):
    from sdlc_next import cmd_provision_epic_stack, cmd_teardown_epic_stack
    _enable_stack(monkeypatch, tmp_path)
    cmd_provision_epic_stack(159, shell=lambda c, cwd: "", probe=lambda p: True)
    ran = []
    result = cmd_teardown_epic_stack(159, shell=lambda c, cwd: ran.append(c) or "",
                                     running_probe=lambda proj: [])
    assert result["torn_down"] is True
    assert ran == ["COMPOSE_PROJECT_NAME=sdlc-epic159 docker compose --env-file .env.epic159 "
                   "down -v --remove-orphans"]
    assert not (tmp_path / ".env.epic159").exists()
    assert not (tmp_path / ".secrets.epic159").exists()
    assert not (tmp_path / ".docker/postgres-data-epic159").exists()
    # The base profile is untouched.
    assert (tmp_path / ".env.dev").exists() and (tmp_path / ".secrets.dev").exists()
    # Second teardown: structured no-op.
    assert cmd_teardown_epic_stack(159, shell=lambda c, cwd: "",
                                   running_probe=lambda proj: [])["torn_down"] is False


def test_teardown_epic_stack_keeps_files_when_down_fails(monkeypatch, tmp_path):
    from sdlc_next import cmd_provision_epic_stack, cmd_teardown_epic_stack, GhError
    import pytest
    _enable_stack(monkeypatch, tmp_path)
    cmd_provision_epic_stack(159, shell=lambda c, cwd: "", probe=lambda p: True)

    def failing(c, cwd):
        raise GhError("compose down failed")
    with pytest.raises(GhError):
        cmd_teardown_epic_stack(159, shell=failing, running_probe=lambda proj: [])
    assert (tmp_path / ".env.epic159").exists()


def test_teardown_epic_stack_is_a_no_op_when_containers_survive_the_down(monkeypatch, tmp_path):
    # Regression: a down that exits 0 but stops nothing (unresolved --env-file) was
    # reported torn down and its files removed.
    from sdlc_next import cmd_provision_epic_stack, cmd_teardown_epic_stack
    _enable_stack(monkeypatch, tmp_path)
    cmd_provision_epic_stack(159, shell=lambda c, cwd: "", probe=lambda p: True)
    result = cmd_teardown_epic_stack(159, shell=lambda c, cwd: "",
                                     running_probe=lambda proj: ["abc123", "def456"])
    assert result["torn_down"] is False
    assert result["still_running"] == ["abc123", "def456"]
    assert (tmp_path / ".env.epic159").exists()
    assert (tmp_path / ".secrets.epic159").exists()
    assert (tmp_path / ".docker/postgres-data-epic159").exists()


def test_teardown_named_profile_when_stack_disabled(monkeypatch, tmp_path):
    from sdlc_next import cmd_teardown_epic_stack
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.e2e159").write_text("X=1\n")
    (tmp_path / ".secrets.e2e159").write_text("Y=2\n")
    # Positive control: stack disabled and nothing named is still a no-op.
    assert cmd_teardown_epic_stack(159, running_probe=lambda proj: [])["torn_down"] is False
    ran = []
    result = cmd_teardown_epic_stack(159, profile="e2e159",
                                     shell=lambda c, cwd: ran.append(c) or "",
                                     running_probe=lambda proj: [])
    assert result["torn_down"] is True
    assert result["profile"] == "e2e159"
    assert not (tmp_path / ".env.e2e159").exists()
    assert not (tmp_path / ".secrets.e2e159").exists()


# --- Parallel implementation lane: list-parallel-ready ---

def test_list_parallel_ready_happy_path_selects_disjoint_children(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "DEV_LANE_PARALLELISM", 3)  # a cap above the sequential default
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c185 = _issue(185, stage="development", parent=110)
    c187 = _issue(187, stage="development", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c185, c187]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
        ("git", "-C", "/repo", "show", "origin/issue-185:docs/sdlc/issue-185/architecture.md"):
            "## Footprint\n\n- `frontend/src/lib/api/core.ts`\n",
        ("git", "-C", "/repo", "show", "origin/issue-187:docs/sdlc/issue-187/architecture.md"):
            "## Footprint\n\n- `frontend/src/app/checkout/**`\n",
    }
    responses.update(_no_blockers_responses(185, 187))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["active_count"] == 0
    assert result["count"] == 2
    assert [c["issue"] for c in result["parallel_ready"]] == [185, 187]
    assert result["skipped"] == []


def test_list_parallel_ready_skips_active_blocked_and_footprint_collision():
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c185 = _issue(185, stage="development", parent=110)   # already active in its own worktree
    c186 = _issue(186, stage="development", parent=110)            # blocked
    c187 = _issue(187, stage="development", parent=110)            # footprint collides with active #185
    responses = {
        tuple(_list_argv()): _list_response([epic, c185, c186, c187]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n\n"
            "worktree /tmp/dev-185\nHEAD y\nbranch refs/heads/issue-185\n",
        ("git", "-C", "/repo", "show", "origin/issue-185:docs/sdlc/issue-185/architecture.md"):
            "## Footprint\n\n- `frontend/src/lib/api/core.ts`\n",
        ("git", "-C", "/repo", "show", "origin/issue-187:docs/sdlc/issue-187/architecture.md"):
            "## Footprint\n\n- `frontend/src/lib/api/core.ts`\n",
    }
    responses.update(_no_blockers_responses(185, 187))
    from sdlc_next import _BLOCKED_BY_QUERY
    responses[("gh", "api", "graphql", "-f", f"query={_BLOCKED_BY_QUERY.format(n=186)}")] = json.dumps(
        {"data": {"repository": {"issue": {"blockedBy": {"nodes": [{"number": 99, "state": "OPEN"}]}}}}})
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["active_count"] == 1
    assert result["count"] == 0
    reasons = {s["issue"]: s["reason"] for s in result["skipped"]}
    assert "already active" in reasons[185]
    assert "blocked" in reasons[186]
    assert "overlaps active/eligible #185" in reasons[187]


def test_list_parallel_ready_respects_limit_and_reports_eligible_total():
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    children = [_issue(n, stage="development", parent=110) for n in (201, 202, 203)]
    responses = {
        tuple(_list_argv()): _list_response([epic] + children),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    for n in (201, 202, 203):
        responses[("git", "-C", "/repo", "show", f"origin/issue-{n}:docs/sdlc/issue-{n}/architecture.md")] = \
            f"## Footprint\n\n- `some/dir-{n}/**`\n"
    responses.update(_no_blockers_responses(201, 202, 203))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, limit=2, runner=runner)
    assert result["eligible_total"] == 3
    assert result["count"] == 2
    assert [c["issue"] for c in result["parallel_ready"]] == [201, 202]


def test_list_parallel_ready_skips_a_child_missing_footprint():
    # No parseable ## Footprint means non-overlap cannot be verified, so the child is skipped.
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c201 = _issue(201, stage="development", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c201]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
        ("git", "-C", "/repo", "show", "origin/issue-201:docs/sdlc/issue-201/architecture.md"):
            "# Architecture\n\nNo footprint section here.\n",
        ("git", "-C", "/repo", "show", "origin/epic-110:docs/sdlc/epic-110/lld.md"):
            "## Task #999: someone else\n\n## Footprint\n\n- `x`\n",
    }
    responses.update(_no_blockers_responses(201))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["parallel_ready"] == []
    assert "no ## Footprint" in result["skipped"][0]["reason"]


def test_list_parallel_ready_returns_empty_for_non_architected_epic():
    # A normal epic's children are not lane work until the epic is epic:architected.
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110)
    c201 = _issue(201, stage="lld", parent=110)
    responses = {tuple(_list_argv()): _list_response([epic, c201])}
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["parallel_ready"] == []
    assert "not epic:architected" in result["note"]


def test_cli_list_parallel_ready_dispatches(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    captured = {}

    def fake(gh, repo_path, epic, limit, run_id=None):
        captured.update(repo_path=repo_path, epic=epic, limit=limit, run_id=run_id)
        return {"parallel_ready": [], "count": 0}

    monkeypatch.setattr(sdlc_next, "cmd_list_parallel_ready", fake)
    exit_code = sdlc_next.main(["list-parallel-ready", "110", "--repo-path", "/repo",
                                "--limit", "2", "--run-id", "run-7"])
    assert exit_code == 0
    assert captured == {"repo_path": "/repo", "epic": 110, "limit": 2, "run_id": "run-7"}


# --- Design lane: a standing epic's product/architecture children (cap default 1) ---

def test_list_design_ready_fans_out_standing_children_up_to_the_cap(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "DESIGN_LANE_PARALLELISM", 2)  # a cap above the sequential default
    from sdlc_next import GitHub, cmd_list_design_ready
    epic = _epic(90, labels=["epic:standing"])
    c1 = _issue(1, stage="product", parent=90, created="2026-08-01T00:00:00Z")
    c2 = _issue(2, stage="architecture", parent=90, created="2026-08-02T00:00:00Z")
    c3 = _issue(3, stage="product", parent=90, created="2026-08-03T00:00:00Z")
    responses = {
        tuple(_list_argv()): _list_response([epic, c1, c2, c3]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    responses.update(_no_blockers_responses(1, 2, 3))
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 90, runner=ScriptedRunner(responses))
    assert result["limit"] == 2
    assert result["eligible_total"] == 3
    assert result["count"] == 2
    assert [c["issue"] for c in result["design_ready"]] == [1, 2]
    assert {c["stage"] for c in result["design_ready"]} == {"product", "architecture"}
    assert result["active_count"] == 0
    assert result["slots_available"] == 2
    assert result["skipped"] == []


# --- Product-stage WIP cap: at most `productWip.maxGateAPending` units awaiting Gate A ---

def _gate_a_queue(epic_number, count, start=900):
    """`count` children of `epic_number` parked at Stage=Product, Gate A open."""
    statuses = ["awaiting-human-review", "feedback-received"]
    return [_issue(start + i, stage="product", status=statuses[i % 2], parent=epic_number)
            for i in range(count)]


_HUMAN_GATE_A_STANDING = "standing-human"


def _with_human_gate_a_standing_profile(monkeypatch):
    """Prepend a per-child-product profile whose Gate A is human (the sample's
    `standing` profile auto-passes Gate A, which the cap exempts)."""
    import sdlc_next
    profile = {"name": _HUMAN_GATE_A_STANDING, "match": {"label": _HUMAN_GATE_A_STANDING},
               "epicLevelPhase": False,
               "childrenNeedArchitectedEpic": False, "closes": False,
               "gates": {"requiresHumanGateA": True}}
    monkeypatch.setitem(sdlc_next.PIPELINE, "profiles", [profile, *sdlc_next.PIPELINE["profiles"]])


def test_product_gate_pending_counts_both_gate_pending_statuses_repo_wide():
    from sdlc_next import product_gate_pending, product_wip_headroom
    other_epic = _epic(91, labels=["epic:standing"])
    queue = _gate_a_queue(91, 3)
    closed = _issue(950, stage="product", status="awaiting-human-review", parent=91, state="CLOSED")
    arch_pending = _issue(951, stage="architecture", status="awaiting-human-review", parent=91)
    in_progress = _issue(952, stage="product", status="in-progress", parent=91)
    issues = [other_epic, *queue, closed, arch_pending, in_progress]
    assert product_gate_pending(issues) == [900, 901, 902]
    assert product_wip_headroom(issues) == 2


def test_next_action_defers_a_fresh_product_child_when_the_gate_a_queue_is_full(monkeypatch):
    from sdlc_next import GitHub, decide_next_action
    _with_human_gate_a_standing_profile(monkeypatch)
    epic_90 = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    fresh = _issue(9, stage="product", parent=90)
    issues = [epic_90, epic_91, fresh, *_gate_a_queue(91, 5)]
    # Nothing beyond the issue list is scripted: a capped unit gets no side effects.
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response(issues)}))
    result = decide_next_action(gh, 90)
    assert result["action"] == "none" and result["epic"] == 90
    assert result["product_cap"] == {"limit": 5, "pending": [900, 901, 902, 903, 904], "deferred": [9]}


def test_next_action_starts_product_while_the_gate_a_queue_has_headroom(monkeypatch):
    from sdlc_next import GitHub, decide_next_action
    _with_human_gate_a_standing_profile(monkeypatch)
    epic_90 = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    fresh = _issue(9, stage="product", parent=90)
    issues = [epic_90, epic_91, fresh, *_gate_a_queue(91, 4)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 9, "unit": "issue", "stage": "product"}


def test_next_action_at_cap_loops_to_a_sibling_past_product(monkeypatch):
    # At the cap the fresh product unit is skipped and an architecture sibling is delegated.
    from sdlc_next import GitHub, decide_next_action
    _with_human_gate_a_standing_profile(monkeypatch)
    epic_90 = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    fresh = _issue(9, stage="product", parent=90, created="2026-08-01T00:00:00Z")
    later = _issue(10, stage="architecture", parent=90, created="2026-08-05T00:00:00Z")
    issues = [epic_90, epic_91, fresh, later, *_gate_a_queue(91, 5)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(10))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 10, "unit": "issue",
                                           "stage": "architecture"}


def test_next_action_cap_never_gates_a_resume_or_a_gate_action(monkeypatch):
    # The cap never blocks a resume or a gate pass (passing is what drains the queue).
    from sdlc_next import GitHub, decide_next_action
    _with_human_gate_a_standing_profile(monkeypatch)
    epic_90 = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    crashed = _issue(9, stage="product", status="in-progress", parent=90)
    issues = [epic_90, epic_91, crashed, *_gate_a_queue(91, 5)]
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response(issues)}))
    assert decide_next_action(gh, 90) == {"action": "resume", "issue": 9, "unit": "issue", "stage": "product"}
    gated = _issue(3, stage="product", status="awaiting-human-review", parent=90)
    issues = [epic_90, epic_91, gated, *_gate_a_queue(91, 5)]
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response(issues),
        ("gh", "issue", "view", "3", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:31 -->"}]}),
        ("gh", "pr", "view", "31", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-08-05T00:00:00Z"}),
    })
    result = decide_next_action(GitHub(runner=runner), 90)
    assert result["action"] == "pass-gate" and result["issue"] == 3


def test_next_action_cap_does_not_gate_a_profile_whose_gate_a_auto_passes():
    # A profile whose Gate A auto-passes never feeds the human queue, so the cap ignores it.
    from sdlc_next import GitHub, decide_next_action
    epic_90 = _epic(90, labels=["epic:standing"])
    epic_91 = _epic(91, labels=["epic:standing"])
    fresh = _issue(9, stage="product", parent=90)
    issues = [epic_90, epic_91, fresh, *_gate_a_queue(91, 5)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 9, "unit": "issue", "stage": "product"}


def test_next_action_cap_disabled_by_zero(monkeypatch):
    import sdlc_next
    from sdlc_next import GitHub, decide_next_action
    _with_human_gate_a_standing_profile(monkeypatch)
    monkeypatch.setattr(sdlc_next, "PRODUCT_WIP_CAP", 0)
    epic_90 = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    fresh = _issue(9, stage="product", parent=90)
    issues = [epic_90, epic_91, fresh, *_gate_a_queue(91, 7)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    gh = GitHub(runner=ScriptedRunner(responses))
    assert decide_next_action(gh, 90)["action"] == "delegate"


def test_list_design_ready_skips_product_candidates_at_the_cap_but_not_architecture(monkeypatch):
    from sdlc_next import GitHub, cmd_list_design_ready
    _with_human_gate_a_standing_profile(monkeypatch)
    epic = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    c1 = _issue(1, stage="product", parent=90, created="2026-08-01T00:00:00Z")
    c2 = _issue(2, stage="architecture", parent=90, created="2026-08-02T00:00:00Z")
    responses = {
        tuple(_list_argv()): _list_response([epic, epic_91, c1, c2, *_gate_a_queue(91, 5)]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    responses.update(_no_blockers_responses(1, 2))
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 90, runner=ScriptedRunner(responses))
    assert [c["issue"] for c in result["design_ready"]] == [2]
    assert result["product_cap"] == {"limit": 5, "pending": [900, 901, 902, 903, 904]}
    assert [s["issue"] for s in result["skipped"]] == [1]
    assert "product WIP cap" in result["skipped"][0]["reason"]


def test_list_design_ready_one_fan_out_cannot_overshoot_the_cap(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "DESIGN_LANE_PARALLELISM", 2)  # a cap above the sequential default
    # WIP headroom 1, two fresh product candidates: only one is proposed despite a free lane slot.
    from sdlc_next import GitHub, cmd_list_design_ready
    _with_human_gate_a_standing_profile(monkeypatch)
    epic = _epic(90, labels=[_HUMAN_GATE_A_STANDING])
    epic_91 = _epic(91, labels=["epic:standing"])
    c1 = _issue(1, stage="product", parent=90, created="2026-08-01T00:00:00Z")
    c3 = _issue(3, stage="product", parent=90, created="2026-08-03T00:00:00Z")
    responses = {
        tuple(_list_argv()): _list_response([epic, epic_91, c1, c3, *_gate_a_queue(91, 4)]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    responses.update(_no_blockers_responses(1, 3))
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 90, runner=ScriptedRunner(responses))
    assert [c["issue"] for c in result["design_ready"]] == [1]
    assert result["slots_available"] == 2
    assert [s["issue"] for s in result["skipped"]] == [3]
    assert "product WIP cap" in result["skipped"][0]["reason"]


def test_list_design_ready_returns_empty_for_a_non_standing_epic():
    # A non-standing Epic's design runs through its phase-Tasks, so nothing fans out.
    from sdlc_next import GitHub, cmd_list_design_ready
    epic = _epic(110)   # no epic:standing label -> default profile
    c1 = _issue(1, stage="product", parent=110)
    c2 = _issue(2, stage="architecture", parent=110)
    responses = {tuple(_list_argv()): _list_response([epic, c1, c2])}
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 110, runner=ScriptedRunner(responses))
    assert result["design_ready"] == []
    assert result["count"] == 0
    assert "not a standing epic" in result["note"]


def test_list_design_ready_skips_parked_blocked_gate_pending_and_active(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "DESIGN_LANE_PARALLELISM", 2)  # a cap above the sequential default
    from sdlc_next import GitHub, cmd_list_design_ready
    epic = _epic(90, labels=["epic:standing"])
    c1 = _issue(1, stage="product", status="needs-human", parent=90)
    c2 = _issue(2, stage="architecture", status="in-progress", parent=90)  # active in worktree
    c3 = _issue(3, stage="product", parent=90)                              # blocked
    c4 = _issue(4, stage="architecture", status="awaiting-human-review", parent=90)  # gate-pending
    c5 = _issue(5, stage="product", parent=90, created="2026-08-09T00:00:00Z")        # clean
    responses = {
        tuple(_list_argv()): _list_response([epic, c1, c2, c3, c4, c5]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n\n"
            "worktree /tmp/dev-2\nHEAD y\nbranch refs/heads/issue-2\n",
    }
    responses.update(_no_blockers_responses(2, 5))
    from sdlc_next import _BLOCKED_BY_QUERY
    responses[("gh", "api", "graphql", "-f", f"query={_BLOCKED_BY_QUERY.format(n=3)}")] = json.dumps(
        {"data": {"repository": {"issue": {"blockedBy": {"nodes": [{"number": 99, "state": "OPEN"}]}}}}})
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 90, runner=ScriptedRunner(responses))
    reasons = {s["issue"]: s["reason"] for s in result["skipped"]}
    assert "needs-human" in reasons[1]
    assert "already active" in reasons[2]
    assert "blocked" in reasons[3]
    assert "awaiting-human-review" in reasons[4]
    assert result["active_count"] == 1                        # issue-2 worktree, design-stage
    assert [c["issue"] for c in result["design_ready"]] == [5]


def test_list_design_ready_dev_lane_worktree_does_not_consume_a_design_slot():
    # A standing child's dev-lane worktree does not count against the design-lane cap.
    from sdlc_next import GitHub, cmd_list_design_ready
    epic = _epic(90, labels=["epic:standing"])
    c1 = _issue(1, stage="development", parent=90)   # dev lane, active in a worktree
    c2 = _issue(2, stage="product", parent=90)
    responses = {
        tuple(_list_argv()): _list_response([epic, c1, c2]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n\n"
            "worktree /tmp/dev-1\nHEAD y\nbranch refs/heads/issue-1\n",
    }
    responses.update(_no_blockers_responses(2))
    gh = GitHub(runner=ScriptedRunner(responses))
    result = cmd_list_design_ready(gh, "/repo", 90, runner=ScriptedRunner(responses))
    assert result["active_count"] == 0                  # dev-lane worktree ignored here
    assert [c["issue"] for c in result["design_ready"]] == [2]


def test_list_design_ready_raises_for_a_non_epic():
    from sdlc_next import GitHub, GhError, cmd_list_design_ready
    child = _issue(5, stage="product", parent=90)
    responses = {tuple(_list_argv()): _list_response([child])}
    gh = GitHub(runner=ScriptedRunner(responses))
    import pytest
    with pytest.raises(GhError):
        cmd_list_design_ready(gh, "/repo", 5, runner=ScriptedRunner(responses))


def test_cli_list_design_ready_dispatches(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    captured = {}

    def fake(gh, repo_path, epic, limit):
        captured.update(repo_path=repo_path, epic=epic, limit=limit)
        return {"design_ready": [], "count": 0}

    monkeypatch.setattr(sdlc_next, "cmd_list_design_ready", fake)
    exit_code = sdlc_next.main(["list-design-ready", "90", "--repo-path", "/repo", "--limit", "2"])
    assert exit_code == 0
    assert captured == {"repo_path": "/repo", "epic": 90, "limit": 2}


def test_lane_caps_follow_the_sample_config():
    from sdlc_next import DESIGN_LANE_PARALLELISM, DEV_LANE_PARALLELISM, PR_REVIEW_PARALLELISM
    assert (DEV_LANE_PARALLELISM, PR_REVIEW_PARALLELISM, DESIGN_LANE_PARALLELISM) == (1, 1, 1)


def test_show_config_lane_caps_default_to_one_when_absent(tmp_path):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    for key in ("devLane", "prReview", "designLane"):
        cfg["parallelism"].pop(key)
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    out = subprocess.check_output(
        [sys.executable, script, "show-config"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path), "GITHUB_TOKEN": "x"}, text=True)
    lanes = json.loads(out)["parallelism"]
    assert (lanes["devLane"], lanes["prReview"], lanes["designLane"]) == (1, 1, 1)


def test_config_is_found_from_cwd_not_from_the_script_location(tmp_path):
    """CI runs the script from a plugin checkout; config comes from the driven repo's cwd."""
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["repo"] = "driven/repo"
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "sdlc-pipeline.config.json").write_text(json.dumps(cfg))
    (tmp_path / "sub").mkdir()
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    env = {k: v for k, v in os.environ.items() if k != "SDLC_CONFIG"}
    env["GITHUB_TOKEN"] = "x"
    out = subprocess.check_output([sys.executable, script, "show-config"],
                                  cwd=tmp_path / "sub", env=env, text=True)
    assert json.loads(out)["repo"] == "driven/repo"


def test_show_config_honours_a_design_lane_override(tmp_path):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["parallelism"]["designLane"] = 5
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    out = subprocess.check_output(
        [sys.executable, script, "show-config"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path), "GITHUB_TOKEN": "x"}, text=True)
    assert json.loads(out)["parallelism"]["designLane"] == 5


def test_show_config_max_tasks_per_run_defaults_to_zero_unlimited(tmp_path):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["parallelism"].pop("maxTasksPerRun", None)  # a config predating the key
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    out = subprocess.check_output(
        [sys.executable, script, "show-config"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path), "GITHUB_TOKEN": "x"}, text=True)
    assert json.loads(out)["parallelism"]["maxTasksPerRun"] == 0


def test_show_config_honours_a_max_tasks_per_run_override(tmp_path):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["parallelism"]["maxTasksPerRun"] = 10
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    out = subprocess.check_output(
        [sys.executable, script, "show-config"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path), "GITHUB_TOKEN": "x"}, text=True)
    assert json.loads(out)["parallelism"]["maxTasksPerRun"] == 10


# --- Worktree release: a parked or merged unit stops holding a dev-lane slot ---

def _worktree_list(*entries):
    """Porcelain `git worktree list` output: (path, branch) pairs, branch None = detached."""
    out = []
    for path, branch in entries:
        out.append(f"worktree {path}\nHEAD abc123")
        if branch:
            out.append(f"branch refs/heads/{branch}")
        out.append("")
    return "\n".join(out)


def test_release_worktree_removes_a_clean_fully_pushed_worktree():
    from sdlc_next import release_worktree
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain"): "",
        ("git", "-C", "/tmp/sdlc-dev-186", "log", "--oneline", "origin/issue-186..issue-186"): "",
        ("git", "-C", "/repo", "worktree", "remove", "--force", "/tmp/sdlc-dev-186"): "",
    })
    assert release_worktree("issue-186", runner=runner, base_repo="/repo") == {
        "released": True, "path": "/tmp/sdlc-dev-186"}
    assert ["git", "-C", "/repo", "worktree", "remove", "--force", "/tmp/sdlc-dev-186"] in runner.calls


def test_release_worktree_refuses_when_the_worktree_is_dirty():
    from sdlc_next import release_worktree
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain"): " M src/app.ts\n",
    })
    result = release_worktree("issue-186", runner=runner, base_repo="/repo")
    assert result == {"released": False, "path": "/tmp/sdlc-dev-186", "reason": "uncommitted changes"}
    assert not any(c[:2] == ["git", "-C"] and "remove" in c for c in runner.calls)


def test_release_worktree_refuses_when_commits_are_not_on_origin():
    from sdlc_next import release_worktree
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain"): "",
        ("git", "-C", "/tmp/sdlc-dev-186", "log", "--oneline", "origin/issue-186..issue-186"):
            "deadbee wip\n",
    })
    result = release_worktree("issue-186", runner=runner, base_repo="/repo")
    assert result["released"] is False
    assert "not pushed" in result["reason"]


def test_release_worktree_is_a_noop_when_no_worktree_holds_the_branch():
    from sdlc_next import release_worktree
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"): _worktree_list(("/repo", "main")),
    })
    assert release_worktree("issue-186", runner=runner, base_repo="/repo") == {
        "released": False, "reason": "no worktree"}


def test_mark_needs_human_releases_the_units_worktree():
    from sdlc_next import (GitHub, cmd_mark_needs_human, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS)
    runner = ScriptedRunner({
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=186)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_186"}}}}),
        ("gh", "api", "graphql", "-f",
         f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_186', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['needs-human'])}"):
            json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 186}}}}),
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "rev-parse", "--path-format=absolute", "--git-common-dir"):
            "/repo/.git\n",
        ("git", "-C", "/tmp/sdlc-dev-186", "status", "--porcelain"): "",
        ("git", "-C", "/tmp/sdlc-dev-186", "log", "--oneline", "origin/issue-186..issue-186"): "",
        ("git", "-C", "/repo", "worktree", "remove", "--force", "/tmp/sdlc-dev-186"): "",
    })
    runner.prefix_responses = {("gh", "issue", "comment", "186"): ""}
    result = cmd_mark_needs_human(GitHub(runner=runner), 186, reason="contract gap", repo_path="/repo")
    assert result["worktree"] == {"released": True, "path": "/tmp/sdlc-dev-186", "branch": "issue-186"}


def test_list_parallel_ready_does_not_count_a_parked_issues_leftover_worktree(monkeypatch):
    """A needs-human unit's leftover worktree consumes no slot; the next child is still proposed."""
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "DEV_LANE_PARALLELISM", 3)  # a cap above the sequential default
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c186 = _issue(186, stage="development", status="needs-human", parent=110)
    c188 = _issue(188, stage="development", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c186, c188]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "show", "origin/issue-186:docs/sdlc/issue-186/architecture.md"):
            "## Footprint\n\n- `frontend/src/app/seller/**`\n",
        ("git", "-C", "/repo", "show", "origin/issue-188:docs/sdlc/issue-188/architecture.md"):
            "## Footprint\n\n- `backend/src/modules/notifications/**`\n",
    }
    responses.update(_no_blockers_responses(188))
    runner = ScriptedRunner(responses)
    result = cmd_list_parallel_ready(GitHub(runner=runner), "/repo", 110, runner=runner)
    assert result["active_count"] == 0
    assert result["slots_available"] == 3
    assert [c["issue"] for c in result["parallel_ready"]] == [188]
    assert result["stale_worktrees"] == [
        {"branch": "issue-186", "reason": "Pipeline Status is 'needs-human'"}]


def test_list_parallel_ready_does_not_count_a_closed_issues_leftover_worktree():
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c150 = _issue(150, stage="testing", parent=110, state="CLOSED")
    c188 = _issue(188, stage="lld", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c150, c188]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/issue-150-final", "issue-150")),
        ("git", "-C", "/repo", "show", "origin/issue-150:docs/sdlc/issue-150/lld.md"):
            "## Footprint\n\n- `some/dir/**`\n",
        ("git", "-C", "/repo", "show", "origin/issue-188:docs/sdlc/issue-188/lld.md"):
            "## Footprint\n\n- `backend/src/modules/notifications/**`\n",
    }
    responses.update(_no_blockers_responses(188))
    runner = ScriptedRunner(responses)
    result = cmd_list_parallel_ready(GitHub(runner=runner), "/repo", 110, runner=runner)
    assert result["active_count"] == 0
    assert result["stale_worktrees"] == [
        {"branch": "issue-150", "reason": "issue is closed or not found"}]


def _wt_list_main(repo="."):
    return {("git", "-C", repo, "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n"}


def test_worktree_add_first_touch_child_branches_off_epic_branch():
    from sdlc_next import GitHub, cmd_worktree_add
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="development", parent=110)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/issue-185"): "",
        ("git", "-C", ".", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/epic-110"): "",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-dev-185", "-b", "issue-185",
         "origin/epic-110"): "",
    })
    result = cmd_worktree_add(gh, 185, runner=runner)
    assert {k: result[k] for k in ("created", "path", "branch", "base", "resumed")} == {
        "created": True, "path": "/tmp/sdlc-dev-185", "branch": "issue-185",
        "base": "origin/epic-110", "resumed": False}


def test_worktree_add_resume_uses_pushed_branch_never_main():
    from sdlc_next import GitHub, cmd_worktree_add
    gh = GitHub(runner=ScriptedRunner({}))
    runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/issue-185"): "  origin/issue-185\n",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-dev-185", "-B", "issue-185",
         "origin/issue-185"): "",
    })
    result = cmd_worktree_add(gh, 185, runner=runner)
    assert result["resumed"] is True and result["base"] == "origin/issue-185"
    assert not any("origin/main" in c for c in runner.calls)


def test_worktree_add_epic_unit_branches_off_main_into_epic_path():
    from sdlc_next import GitHub, cmd_worktree_add
    gh = GitHub(runner=ScriptedRunner({}))
    runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/epic-110"): "",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-epic-110", "-b", "epic-110",
         "origin/main"): "",
        ("git", "-C", ".", "push", "origin", "refs/heads/epic-110:refs/heads/epic-110"): "",
    })
    assert cmd_worktree_add(gh, 110, unit="epic", runner=runner)["path"] == "/tmp/sdlc-epic-110"


def test_worktree_add_explicit_base_override_skips_auto_detection():
    # An explicit base is honoured without ever consulting integration_base.
    from sdlc_next import GitHub, cmd_worktree_add
    gh = GitHub(runner=ScriptedRunner({}))  # would raise on any call -- proves no auto-detection ran
    runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/issue-40"): "",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-dev-40", "-b", "issue-40",
         "origin/main"): "",
    })
    result = cmd_worktree_add(gh, 40, unit="issue", runner=runner, base="origin/main")
    assert result["path"] == "/tmp/sdlc-dev-40"
    assert result["branch"] == "issue-40"
    assert result["base"] == "origin/main"


def test_worktree_add_noop_when_branch_already_checked_out():
    # The live tree is handed back (no `worktree add`); already current, so no fast-forward.
    from sdlc_next import GitHub, cmd_worktree_add
    gh = GitHub(runner=ScriptedRunner({}))
    wt = "/tmp/sdlc-dev-185"
    runner = ScriptedRunner({
        ("git", "-C", ".", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n"
            f"\nworktree {wt}\nHEAD y\nbranch refs/heads/issue-185\n",
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/issue-185"): "",
        ("git", "-C", wt, "rev-list", "--count", "issue-185..origin/issue-185"): "0\n",
        ("git", "-C", wt, "rev-list", "--count", "origin/issue-185..issue-185"): "0\n",
    })
    result = cmd_worktree_add(gh, 185, runner=runner)
    assert result["created"] is False and result["path"] == wt
    assert result["resumed"] is True
    assert result["behind_before"] == 0 and result["synced_to_origin"] is True
    assert not any(c[3:5] == ["worktree", "add"] for c in runner.calls)
    assert not any("merge" in c for c in runner.calls)


def test_merge_pr_closes_child_explicitly_when_merged_into_epic_branch():
    # `Closes #<n>` only fires on the default branch, so merge-pr closes the child itself.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        tuple(_list_argv()): _list_response([
            _issue(9, parent=90), _issue(90, issue_type="Feature")]),
        ("gh", "api", "repos/owner/repo/compare/epic-90...issue-9", "--jq", ".behind_by"): "0\n",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pass"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/lld.md\n",
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "close", "9", "--repo", "owner/repo", "--reason", "completed"): "",
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is True and result["issue_closed"] is True
    assert ["gh", "issue", "close", "9", "--repo", "owner/repo",
            "--reason", "completed"] in runner.calls


def test_merge_pr_does_not_close_the_issue_itself_when_the_base_is_main():
    # Merging to main fires `Closes #<n>` itself; no scripted `gh issue close` proves none is issued.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "OPEN", "comments": _clean_pipeline_comments()}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pass"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/lld.md\n",
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is True and result["issue_closed"] is False
    assert not any(c[:3] == ["gh", "issue", "close"] for c in runner.calls)


def test_open_gate_unit_issue_does_not_consult_the_gate_branch_guard():
    # The gate-branch guard is epic-only; no scripted compare proves it is skipped.
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "fetch", "origin"): "",
                                 ("git", "-C", "/repo", "rev-parse", "origin/issue-9"): "abcd1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/131\n",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 9, "Notification prefs", "product.md",
                            "architecture", "Locked requirements.", runner=git_runner)
    assert result["head"] == "issue-9" and result["base"] == "main"
    assert not any("compare" in " ".join(c) for c in gh_runner.calls)


# --- Epic profiles + product-review stage + Gate A configurability ---

def test_resolve_profile_matches_standing_label():
    from sdlc_next import resolve_profile
    p = resolve_profile({"labels": [{"name": "epic:standing"}]})
    assert p["name"] == "standing"
    assert p["epicLevelPhase"] is False
    assert "childEntryStage" not in p
    assert p["childrenNeedArchitectedEpic"] is False
    assert p["closes"] is False
    # Sample config's standing profile waives both human gates.
    assert p["gates"]["requiresHumanGateA"] is False
    assert p["gates"]["requiresHumanGateB"] is False


def test_resolve_profile_legacy_wins_over_standing_by_order():
    from sdlc_next import resolve_profile
    p = resolve_profile({"labels": [{"name": "epic:standing"}, {"name": "epic:legacy"}]})
    assert p["name"] == "legacy"
    assert p["driven"] is False


def test_resolve_profile_falls_through_to_default_catch_all():
    from sdlc_next import resolve_profile
    p = resolve_profile({"labels": [{"name": "unrelated"}]})
    assert p["name"] == "default"
    assert p["epicLevelPhase"] is True
    assert "childEntryStage" not in p
    assert p["gates"]["skipConfidenceThreshold"] == 80
    assert p["gates"]["requiresHumanGateA"] is True
    assert p["gates"]["requiresHumanGateB"] is True


def test_resolve_profile_none_epic_is_default():
    from sdlc_next import resolve_profile
    assert resolve_profile(None)["name"] == "default"


def test_is_epic_standing_and_legacy_read_from_profile():
    from sdlc_next import is_epic_standing, is_epic_legacy
    assert is_epic_standing({"labels": [{"name": "epic:standing"}]}) is True
    assert is_epic_standing({"labels": []}) is False
    assert is_epic_legacy({"labels": [{"name": "epic:legacy"}]}) is True
    assert is_epic_legacy({"labels": []}) is False


def test_product_review_is_a_design_review_and_review_role():
    from sdlc_next import DESIGN_REVIEW_ROLES, REVIEW_ROLES
    assert "product-review" in DESIGN_REVIEW_ROLES
    assert "product-review" in REVIEW_ROLES


def _epic_check(n, parent=None, labels=()):
    return (("gh", "api", "graphql", "-f",
             f"query={__import__('sdlc_next')._ISSUE_EPIC_CHECK_QUERY.format(n=n)}"),
            json.dumps({"data": {"repository": {"issue": {
                "issueType": None,
                "parent": {"number": parent} if parent else None,
                "labels": {"nodes": [{"name": l} for l in
                                     ([*labels, "type:epic"] if parent is None else labels)]}}}}}))


# --- citations: byte-exact literal fragments; `cite-example` blocks are never resolved ---

def test_parse_cite_blocks_distinguishes_asserted_from_illustrative():
    from sdlc_next import parse_cite_blocks
    text = (
        "intro\n"
        "```cite path=foo.yml\n"
        "  a: 1\n"
        "```\n"
        "middle\n"
        "```cite-example path=bar.yml rev=abc123\n"
        "  b: 2\n"
        "  c: 3\n"
        "```\n"
        "end\n"
    )
    blocks = parse_cite_blocks(text)
    assert len(blocks) == 2
    assert blocks[0] == {"kind": "cite", "path": "foo.yml", "rev": None,
                          "body": "  a: 1", "line_no": 3}
    assert blocks[1] == {"kind": "cite-example", "path": "bar.yml", "rev": "abc123",
                          "body": "  b: 2\n  c: 3", "line_no": 7}


def test_cite_generates_block_from_real_file_read(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("one\ntwo\nthree\n")
    result = cmd_cite("foo.yml", line=2, repo_path=str(tmp_path))
    assert result.get("ok", True) is True
    assert "```cite path=foo.yml\ntwo\n```" == result["block"]
    assert result["line_range"] == [2, 2]


def test_cite_generates_multiline_block_with_lines_range(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("one\ntwo\nthree\nfour\n")
    result = cmd_cite("foo.yml", lines="2-3", repo_path=str(tmp_path))
    assert result["block"] == "```cite path=foo.yml\ntwo\nthree\n```"
    assert result["line_range"] == [2, 3]


def test_cite_selects_by_first_line_literally_containing_match(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("one\ncancel-in-progress: true\nthree\n")
    result = cmd_cite("foo.yml", match="cancel-in-progress", repo_path=str(tmp_path))
    assert result["block"] == "```cite path=foo.yml\ncancel-in-progress: true\n```"


def test_cite_errors_and_emits_nothing_when_match_absent(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("one\ntwo\n")
    result = cmd_cite("foo.yml", match="nonexistent-fragment", repo_path=str(tmp_path))
    assert result["ok"] is False
    assert "block" not in result


def test_cite_errors_and_emits_nothing_when_match_is_ambiguous(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("dup: 1\ndup: 2\n")
    result = cmd_cite("foo.yml", match="dup", repo_path=str(tmp_path))
    assert result["ok"] is False
    assert "block" not in result
    assert "ambiguous" in result["reason"]


def test_cite_errors_and_emits_nothing_when_line_out_of_range(tmp_path):
    from sdlc_next import cmd_cite
    (tmp_path / "foo.yml").write_text("one\ntwo\n")
    result = cmd_cite("foo.yml", line=9, repo_path=str(tmp_path))
    assert result["ok"] is False
    assert "block" not in result


def test_cite_chooses_fence_longer_than_any_backtick_run_in_body(tmp_path):
    from sdlc_next import cmd_cite, parse_cite_blocks
    (tmp_path / "foo.md").write_text("before\n```nested```\nafter\n")
    result = cmd_cite("foo.md", line=2, repo_path=str(tmp_path))
    assert result["block"].startswith("````cite ")
    # round-trips: parsing the emitted block recovers the exact body.
    blocks = parse_cite_blocks(result["block"])
    assert blocks == [{"kind": "cite", "path": "foo.md", "rev": None,
                        "body": "```nested```", "line_no": 2}]


def test_cite_reads_pinned_revision_via_git_show():
    from sdlc_next import cmd_cite
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "show", "170df64:foo.yml"): "old: 1\ncancel-in-progress: true\n",
    })
    result = cmd_cite("foo.yml", match="cancel-in-progress", rev="170df64",
                       repo_path="/repo", runner=runner)
    assert result["block"] == "```cite path=foo.yml rev=170df64\ncancel-in-progress: true\n```"


def test_verify_citations_reports_resolved_and_unresolved_with_positive_control(tmp_path):
    from sdlc_next import cmd_verify_citations
    (tmp_path / "target.yml").write_text("hello world\n")
    good_doc = tmp_path / "good.md"
    good_doc.write_text("```cite path=target.yml\nhello world\n```\n")
    result = cmd_verify_citations(["good.md"], repo_path=str(tmp_path))
    assert result["all_resolved"] is True
    assert result["ok"] is True
    assert result["citations"] == [{"path": "target.yml", "rev": None, "resolved": True,
                                     "line_hint": 1}]

    # Positive control: break the citation, watch it go unresolved, then revert.
    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text("```cite path=target.yml\ngoodbye world\n```\n")
    broken = cmd_verify_citations(["bad.md"], repo_path=str(tmp_path))
    assert broken["all_resolved"] is False
    assert broken["ok"] is False
    assert broken["citations"][0]["resolved"] is False
    assert "not found" in broken["citations"][0]["cited_vs_found"]

    reverted = cmd_verify_citations(["good.md"], repo_path=str(tmp_path))
    assert reverted["all_resolved"] is True


def test_verify_citations_matching_is_literal_with_no_escaping(tmp_path):
    from sdlc_next import cmd_verify_citations
    line = "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}"
    (tmp_path / "backend-ci.yml").write_text(f"jobs:\n{line}\nmore: [a, b]*\n")
    doc = tmp_path / "doc.md"
    doc.write_text(f"```cite path=backend-ci.yml\n{line}\n```\n")
    result = cmd_verify_citations(["doc.md"], repo_path=str(tmp_path))
    assert result["all_resolved"] is True


def test_verify_citations_rev_pinned_resolves_against_historical_content_only():
    from sdlc_next import cmd_verify_citations
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "show", "170df64:ci.yml"):
            "cancel-in-progress: true\n",
    })
    doc = "```cite path=ci.yml rev=170df64\ncancel-in-progress: true\n```\n"
    result = cmd_verify_citations_text_helper(doc, repo_path="/repo", runner=runner)
    assert result["all_resolved"] is True
    assert result["citations"][0]["rev"] == "170df64"


def cmd_verify_citations_text_helper(text, repo_path, runner):
    from sdlc_next import verify_citations_text
    return verify_citations_text(text, repo_path=repo_path, runner=runner)


def test_verify_citations_unpinned_citation_does_not_resolve_against_superseded_value(tmp_path):
    from sdlc_next import cmd_verify_citations
    # The working tree no longer holds the cited value.
    (tmp_path / "ci.yml").write_text("cancel-in-progress: ${{ true }}\n")
    doc = tmp_path / "doc.md"
    doc.write_text("```cite path=ci.yml\ncancel-in-progress: true\n```\n")
    result = cmd_verify_citations(["doc.md"], repo_path=str(tmp_path))
    assert result["all_resolved"] is False


def test_verify_citations_multiline_block_is_byte_exact_paraphrase_fails(tmp_path):
    from sdlc_next import cmd_verify_citations
    (tmp_path / "wf.yml").write_text("on:\n  push:\n    branches: [main]\n")
    good_doc = tmp_path / "good.md"
    good_doc.write_text("```cite path=wf.yml\non:\n  push:\n    branches: [main]\n```\n")
    good = cmd_verify_citations(["good.md"], repo_path=str(tmp_path))
    assert good["all_resolved"] is True

    paraphrased_doc = tmp_path / "bad.md"
    paraphrased_doc.write_text("```cite path=wf.yml\non:\n  push:\n    branches: [ main ]\n```\n")
    bad = cmd_verify_citations(["bad.md"], repo_path=str(tmp_path))
    assert bad["all_resolved"] is False


def test_verify_citations_skips_cite_example_blocks_entirely(tmp_path):
    from sdlc_next import cmd_verify_citations
    # The fragment exists nowhere; as `cite-example` it must not be resolved.
    doc = tmp_path / "spec.md"
    doc.write_text("```cite-example path=nonexistent.yml\nthis is illustrative only\n```\n")
    result = cmd_verify_citations(["spec.md"], repo_path=str(tmp_path))
    assert result["all_resolved"] is True
    assert result["ok"] is True
    assert result["citations"] == []
    assert result["examples_skipped"] == 1


def test_verify_citations_multiple_docs_wraps_and_aggregates(tmp_path):
    from sdlc_next import cmd_verify_citations
    (tmp_path / "a.yml").write_text("alpha\n")
    (tmp_path / "b.yml").write_text("beta\n")
    (tmp_path / "doc1.md").write_text("```cite path=a.yml\nalpha\n```\n")
    (tmp_path / "doc2.md").write_text("```cite path=b.yml\nMISSING\n```\n")
    result = cmd_verify_citations(["doc1.md", "doc2.md"], repo_path=str(tmp_path))
    assert result["all_resolved"] is False
    assert result["ok"] is False
    assert [d["document"] for d in result["documents"]] == ["doc1.md", "doc2.md"]
    assert result["documents"][0]["all_resolved"] is True
    assert result["documents"][1]["all_resolved"] is False


def test_verify_exit_citations_ok_true_when_stage_record_all_resolve(tmp_path):
    """A stage record whose citations all resolve yields citations_ok True."""
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    (docs_dir / "lld.md").write_text("```cite path=target.yml\nhello world\n```\n")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "LLD"},
        ]}}}}}),
        **dict([_epic_check(9)]),  # parentless: docs stay under issue-<n>/
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="lld", runner=git_runner)
    assert result["citations_ok"] is True
    assert result.get("ok", True) is True


def test_verify_exit_citations_ok_false_fails_overall_result_positive_control(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    record = docs_dir / "lld.md"
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "LLD"},
        ]}}}}}),
        **dict([_epic_check(9)]),  # parentless: docs stay under issue-<n>/
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)

    # Break: a citation that does not resolve.
    record.write_text("```cite path=target.yml\ngoodbye world\n```\n")
    broken = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="lld", runner=git_runner)
    assert broken["citations_ok"] is False
    assert broken["ok"] is False
    assert "reason" in broken

    # Revert: fix the citation, confirm it goes back to passing.
    record.write_text("```cite path=target.yml\nhello world\n```\n")
    fixed = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="lld", runner=git_runner)
    assert fixed["citations_ok"] is True
    assert fixed.get("ok", True) is True


def test_verify_exit_citations_scoped_to_own_record_ignores_other_docs(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    # A rotted citation in an earlier stage's doc must not affect this stage's exit check.
    (docs_dir / "product.md").write_text("```cite path=target.yml\nlong gone content\n```\n")
    (docs_dir / "lld.md").write_text("```cite path=target.yml\nhello world\n```\n")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "LLD"},
        ]}}}}}),
        **dict([_epic_check(9)]),  # parentless: docs stay under issue-<n>/
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="lld", runner=git_runner)
    assert result["citations_ok"] is True
    assert result.get("ok", True) is True


def test_verify_exit_omits_citations_ok_when_stage_has_no_canonical_record(tmp_path):
    """`development` has no doc record, so the citation gate neither fires nor crashes."""
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Development"},
        ]}}}}}),
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="development", runner=git_runner)
    assert "citations_ok" not in result
    assert result.get("ok", True) is True


def test_resolve_citation_reports_every_matching_line_as_a_hint_on_ambiguity(tmp_path):
    """An ambiguous fragment still resolves, reporting every matching line as a hint."""
    from sdlc_next import resolve_citation
    (tmp_path / "dup.yml").write_text("dup: 1\nother: x\ndup: 1\n")
    unique = resolve_citation("dup.yml", "other: x", repo_path=str(tmp_path))
    assert unique == {"path": "dup.yml", "rev": None, "resolved": True, "line_hint": 2}

    ambiguous = resolve_citation("dup.yml", "dup: 1", repo_path=str(tmp_path))
    assert ambiguous["resolved"] is True
    assert ambiguous["match_count"] == 2
    assert ambiguous["line_hint"] == 1
    assert ambiguous["line_hints"] == [1, 3]


def test_verify_exit_omits_citations_ok_when_stage_record_file_is_absent(tmp_path):
    """A missing stage record is `docs_present`'s concern, not a citation failure."""
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-92"
    docs_dir.mkdir(parents=True)
    (docs_dir / "product.md").write_text("x")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=92)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "92", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Architecture"},
        ]}}}}}),
        **dict([_epic_check(92)]),  # parentless: docs stay under issue-<n>/
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 92, expect_stage="architecture", runner=git_runner)
    assert "citations_ok" not in result


def test_list_parallel_ready_ignores_a_parked_worktrees_footprint_for_collisions():
    """A parked worktree's footprint never collides, just as it never holds a slot."""
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    parked = _issue(323, stage="development", status="needs-human", parent=110)
    candidate = _issue(494, stage="development", parent=110)
    shared = "## Footprint\n\n- `backend/src/modules/search/**`\n"
    responses = {
        tuple(_list_argv()): _list_response([epic, parked, candidate]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-323", "issue-323")),
        ("git", "-C", "/repo", "show", "origin/issue-323:docs/sdlc/issue-323/architecture.md"): shared,
        ("git", "-C", "/repo", "show", "origin/issue-494:docs/sdlc/issue-494/architecture.md"): shared,
    }
    responses.update(_no_blockers_responses(494))
    runner = ScriptedRunner(responses)
    result = cmd_list_parallel_ready(GitHub(runner=runner), "/repo", 110, runner=runner)
    assert [c["issue"] for c in result["parallel_ready"]] == [494]
    assert result["skipped"] == [
        {"issue": 323, "reason": "already active in its own worktree"}]
    assert result["active_count"] == 0
    assert result["stale_worktrees"] == [
        {"branch": "issue-323", "reason": "Pipeline Status is 'needs-human'"}]


def test_list_parallel_ready_still_collides_with_a_live_worktrees_footprint():
    """Positive control: the same overlap against a live sibling is still refused."""
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    live = _issue(323, stage="development", parent=110)
    candidate = _issue(494, stage="development", parent=110)
    shared = "## Footprint\n\n- `backend/src/modules/search/**`\n"
    responses = {
        tuple(_list_argv()): _list_response([epic, live, candidate]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-323", "issue-323")),
        ("git", "-C", "/repo", "show", "origin/issue-323:docs/sdlc/issue-323/architecture.md"): shared,
        ("git", "-C", "/repo", "show", "origin/issue-494:docs/sdlc/issue-494/architecture.md"): shared,
    }
    responses.update(_no_blockers_responses(323, 494))
    runner = ScriptedRunner(responses)
    result = cmd_list_parallel_ready(GitHub(runner=runner), "/repo", 110, runner=runner)
    assert result["parallel_ready"] == []
    assert result["skipped"] == [
        {"issue": 323, "reason": "already active in its own worktree"},
        {"issue": 494, "reason": "footprint overlaps active/eligible #323"}]
    assert result["active_count"] == 1


def _verify_exit_runners(tmp_path, stage_field_value):
    from sdlc_next import _ISSUE_FIELDS_QUERY
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"): json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"},
             "name": stage_field_value},
        ]}}}}}),
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    return gh_runner, git_runner


def test_verify_exit_accepts_pr_review_because_it_is_a_real_stage_value(tmp_path):
    """`pr-review` is a real Stage value (set by open-dev-pr), so it is not misuse."""
    from sdlc_next import GitHub, cmd_verify_exit
    gh_runner, git_runner = _verify_exit_runners(tmp_path, "PR Review")
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="pr-review", runner=git_runner)
    assert result["stage"] == "pr-review"
    assert result["expected_stage_present"] is True
    assert "misuse" not in result
    assert result.get("ok") is not False


def test_verify_exit_still_refuses_review_roles_with_no_stage_value(tmp_path):
    """Review roles with no Stage value of their own are still reported as misuse."""
    from sdlc_next import GitHub, cmd_verify_exit
    for role in ("product-review", "arch-review", "lld-review"):
        gh_runner, git_runner = _verify_exit_runners(tmp_path, "LLD")
        result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                                 expect_stage=role, runner=git_runner)
        assert result["ok"] is False, role
        assert "misuse" in result, role


def test_get_work_item_provider_defaults_to_github():
    from sdlc_next import GitHub, get_work_item_provider
    provider = get_work_item_provider(runner=ScriptedRunner({}))
    assert isinstance(provider, GitHub)


def test_get_work_item_provider_honours_a_configured_github_type():
    import sdlc_next
    from sdlc_next import GitHub, get_work_item_provider
    original = dict(sdlc_next.PIPELINE.get("workItemProvider", {}))
    sdlc_next.PIPELINE["workItemProvider"] = {"type": "github"}
    try:
        provider = get_work_item_provider(runner=ScriptedRunner({}))
        assert isinstance(provider, GitHub)
    finally:
        sdlc_next.PIPELINE["workItemProvider"] = original


def test_get_work_item_provider_refuses_an_unimplemented_type():
    # An unbuilt provider must refuse, never silently fall back to GitHub.
    import sdlc_next
    from sdlc_next import GhError, get_work_item_provider
    import pytest
    original = dict(sdlc_next.PIPELINE.get("workItemProvider", {}))
    sdlc_next.PIPELINE["workItemProvider"] = {"type": "jira"}
    try:
        with pytest.raises(GhError, match="not implemented"):
            get_work_item_provider(runner=ScriptedRunner({}))
    finally:
        sdlc_next.PIPELINE["workItemProvider"] = original


def test_github_satisfies_the_work_item_provider_protocol():
    # GitHub structurally satisfies the runtime_checkable WorkItemProvider protocol.
    from sdlc_next import GitHub, WorkItemProvider
    assert isinstance(GitHub(runner=ScriptedRunner({})), WorkItemProvider)


def test_classify_unit_returns_other_when_no_rules_configured():
    import sdlc_next
    from sdlc_next import GitHub, REPO
    original = dict(sdlc_next.PIPELINE.get("classification", {}))
    sdlc_next.PIPELINE["classification"] = {}
    try:
        runner = ScriptedRunner({})
        runner.prefix_responses[("gh", "api", "graphql")] = json.dumps(
            {"data": {"repository": {"issue": {
                "issueType": {"name": "Feature"}, "parent": None,
                "labels": {"nodes": []}}}}})
        gh = GitHub(runner=runner)
        assert gh.classify_unit(9) == "other"
    finally:
        sdlc_next.PIPELINE["classification"] = original


def test_classify_unit_matches_a_configured_issue_type_rule():
    # Positive control: a configured issueType rule actually classifies.
    import sdlc_next
    from sdlc_next import GitHub, REPO
    original = dict(sdlc_next.PIPELINE.get("classification", {}))
    sdlc_next.PIPELINE["classification"] = {
        "initiative": {"field": "issueType", "value": "Initiative"},
        "epic": {"field": "issueType", "value": "Epic"},
    }
    try:
        runner = ScriptedRunner({})
        runner.prefix_responses[("gh", "api", "graphql")] = json.dumps(
            {"data": {"repository": {"issue": {
                "issueType": {"name": "Initiative"}, "parent": None,
                "labels": {"nodes": []}}}}})
        gh = GitHub(runner=runner)
        assert gh.classify_unit(9) == "initiative"
    finally:
        sdlc_next.PIPELINE["classification"] = original


def test_classify_unit_matches_a_configured_label_rule():
    # A client without native Issue Types provisioned classifies by label instead.
    import sdlc_next
    from sdlc_next import GitHub, REPO
    original = dict(sdlc_next.PIPELINE.get("classification", {}))
    sdlc_next.PIPELINE["classification"] = {
        "task": {"field": "label", "value": "type:task"},
    }
    try:
        runner = ScriptedRunner({})
        runner.prefix_responses[("gh", "api", "graphql")] = json.dumps(
            {"data": {"repository": {"issue": {
                "issueType": None, "parent": {"number": 5},
                "labels": {"nodes": [{"name": "type:task"}]}}}}})
        gh = GitHub(runner=runner)
        assert gh.classify_unit(9) == "task"
    finally:
        sdlc_next.PIPELINE["classification"] = original


def test_classify_unit_returns_other_when_rules_exist_but_none_match():
    # Configured rules that don't match must not produce a false positive.
    import sdlc_next
    from sdlc_next import GitHub, REPO
    original = dict(sdlc_next.PIPELINE.get("classification", {}))
    sdlc_next.PIPELINE["classification"] = {
        "initiative": {"field": "issueType", "value": "Initiative"},
    }
    try:
        runner = ScriptedRunner({})
        runner.prefix_responses[("gh", "api", "graphql")] = json.dumps(
            {"data": {"repository": {"issue": {
                "issueType": {"name": "Bug"}, "parent": {"number": 5},
                "labels": {"nodes": []}}}}})
        gh = GitHub(runner=runner)
        assert gh.classify_unit(9) == "other"
    finally:
        sdlc_next.PIPELINE["classification"] = original


def test_set_issue_type_refuses_an_unconfigured_type_with_a_clear_error():
    # An unprovisioned type raises a clear GhError, not a bare KeyError.
    from sdlc_next import GitHub, GhError
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="not in projectFields.issueTypeIds"):
        gh.set_issue_type(9, "Story")


def test_set_issue_type_still_works_for_a_configured_type():
    # A provisioned type (Task) passes the guard.
    from sdlc_next import GitHub
    runner = ScriptedRunner({})
    runner.prefix_responses[("gh", "api", "graphql")] = json.dumps(
        {"data": {"repository": {"issue": {"id": "ISSUE_ID_1"}}}})
    gh = GitHub(runner=runner)
    gh.set_issue_type(9, "Task")  # must not raise


def test_merge_epic_lld_doc_not_merged_when_doc_not_yet_on_origin():
    # No lld.md on origin/epic-<n> means nothing to merge and no fields touched.
    from sdlc_next import GitHub, cmd_merge_lld_doc
    path = "/epic-110"
    doc = "docs/sdlc/epic-110/lld.md"
    runner = ScriptedRunner({
        **_live_wt("epic-110", path=path),
        ("git", "-C", path, "fetch", "origin"): "",
        ("git", "-C", path, "rev-parse", "--verify", "--quiet", f"origin/epic-110:{doc}"): "",
    })
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_merge_lld_doc(gh, path, 110, runner=runner)
    assert result["merged"] is False
    assert "not have pushed" not in result["reason"]  # sanity: real message, not a stub
    assert "epic" in result and result["epic"] == 110


def test_merge_epic_lld_doc_advances_every_freshly_created_task_and_skips_already_advanced():
    # Fresh Tasks advance; one already at development is left untouched (idempotent).
    from sdlc_next import GitHub, cmd_merge_lld_doc
    path = "/epic-110"
    doc = "docs/sdlc/epic-110/lld.md"
    epic = _epic(110, labels=["epic:architected"])
    fresh_task_1 = _issue(501, parent=110, issue_type="Task", labels=["type:task"])
    fresh_task_2 = _issue(502, parent=110, issue_type="Task", labels=["type:task"])
    already_advanced = _issue(503, parent=110, issue_type="Task", labels=["type:task"], stage="development")

    advance_501, _, _ = _advance_to_development_responses(501)
    advance_502, _, _ = _advance_to_development_responses(502)

    gh_runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([epic, fresh_task_1, fresh_task_2, already_advanced]),
        ("gh", "issue", "view", "110", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": [{"name": "epic:architected"}]}),
        **advance_501, **advance_502,
    })
    gh_runner.prefix_responses[("gh", "issue", "comment")] = ""
    gh = GitHub(runner=gh_runner)

    git_runner = ScriptedRunner({
        **_live_wt("epic-110", path=path),
        ("git", "-C", path, "fetch", "origin"): "",
        ("git", "-C", path, "rev-parse", "--verify", "--quiet", f"origin/epic-110:{doc}"): "blobXYZ\n",
        ("git", "-C", path, "rev-parse", "origin/epic-110"): "tip110\n",
    })

    result = cmd_merge_lld_doc(gh, path, 110, runner=git_runner)
    assert result["merged"] is True
    assert sorted(result["advanced_tasks"]) == [501, 502]
    # Already epic:architected: no re-clearing fields or re-adding the label.
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in gh_runner.calls)

    graphql_calls = [c for c in gh_runner.calls if c[:3] == ["gh", "api", "graphql"]]
    for c in graphql_calls:
        assert "ISSUE_503" not in c[-1] or "updateIssueFieldValue" not in c[-1]
    comment_calls = [c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"]]
    assert not any(c[3] == "503" for c in comment_calls)


def test_merge_epic_lld_doc_completes_the_epics_own_design_phase():
    # merge-lld-doc labels the epic epic:architected and posts the lld.md link on its thread.
    from sdlc_next import (GitHub, cmd_merge_lld_doc, _ISSUE_NODE_ID_QUERY,
                            _DELETE_ISSUE_FIELD_VALUE_MUTATION, STAGE_FIELD_ID,
                            PIPELINE_STATUS_FIELD_ID)
    path = "/epic-120"
    doc = "docs/sdlc/epic-120/lld.md"
    epic = _epic(120)  # not yet epic:architected
    fresh_task = _issue(701, parent=120, issue_type="Task", labels=["type:task"])
    advance_701, _, _ = _advance_to_development_responses(701)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=120)}")
    del_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_120', field_id=STAGE_FIELD_ID)}")
    del_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_120', field_id=PIPELINE_STATUS_FIELD_ID)}")
    gh_runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([epic, fresh_task]),
        ("gh", "issue", "view", "120", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_120"}}}}),
        del_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 120}}}}),
        del_status_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 120}}}}),
        **advance_701,
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment"): "",
                                   ("gh", "issue", "edit", "120"): ""}
    gh = GitHub(runner=gh_runner)
    git_runner = ScriptedRunner({
        **_live_wt("epic-120", path=path),
        ("git", "-C", path, "fetch", "origin"): "",
        ("git", "-C", path, "rev-parse", "--verify", "--quiet", f"origin/epic-120:{doc}"): "blobXYZ\n",
        ("git", "-C", path, "rev-parse", "origin/epic-120"): "tip120\n",
    })
    result = cmd_merge_lld_doc(gh, path, 120, runner=git_runner)
    assert result["merged"] is True
    edit_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "edit"])
    assert "epic:architected" in edit_call
    epic_comment = next(c for c in gh_runner.calls
                         if c[:3] == ["gh", "issue", "comment"] and c[3] == "120")
    body = epic_comment[epic_comment.index("--body") + 1]
    # Cites the epic branch commit, not the doc's blob.
    assert doc in body and "tip120" in body and "blobXYZ" not in body and "#701" in body


def test_v2_full_lifecycle_cutting_an_epic_and_its_phase_tasks():
    """Wiring smoke test: cut an Epic and its phase-Tasks, then merge-lld-doc against one mocked GitHub."""
    from sdlc_next import (GitHub, cmd_worktree_add, cmd_create_issue, cmd_set_stage,
                            cmd_add_blocked_by, cmd_merge_lld_doc,
                            ISSUE_TYPE_IDS, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _SET_ISSUE_TYPE_MUTATION, _ADD_SUB_ISSUE_MUTATION,
                            _ADD_BLOCKED_BY_MUTATION, _DELETE_ISSUE_FIELD_VALUE_MUTATION,
                            STAGE_FIELD_ID, STAGE_OPTION_IDS, PIPELINE_STATUS_FIELD_ID,
                            PIPELINE_STATUS_OPTION_IDS, PRIORITY_FIELD_ID, PRIORITY_OPTION_IDS,
                            EFFORT_FIELD_ID, EFFORT_OPTION_IDS)

    def node_id(n):
        return (("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=n)}"),
                json.dumps({"data": {"repository": {"issue": {"id": f"ISSUE_{n}"}}}}))

    gh_responses = {}
    # create-issue's Pipeline Status / Priority / Effort writes for every issue it cuts.
    for n in (41, 43, 44, 45):
        for field_id, option_id in ((PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS["todo"]),
                                    (PRIORITY_FIELD_ID, PRIORITY_OPTION_IDS["Medium"]),
                                    (EFFORT_FIELD_ID, EFFORT_OPTION_IDS["Medium"])):
            gh_responses[("gh", "api", "graphql", "-f", "query=" + _SET_ISSUE_FIELD_MUTATION.format(
                issue_id=f"ISSUE_{n}", field_id=field_id, option_id=option_id))] = "{\"data\": {}}"
    gh_prefixes = {("gh", "issue", "comment"): "", ("gh", "issue", "edit"): ""}

    # 1. Cut Epic #41 under Initiative #40.
    gh_responses[("gh", "api", "repos/owner/repo/issues", "-f", "title=Epic: WhatsApp booklist lookup",
                  "-f", "body=Slice of Initiative #40.", "-f", "labels[]=type:epic")] = \
        json.dumps({"number": 41})
    a40, r40 = node_id(40)
    a41, r41 = node_id(41)
    gh_responses[a40] = r40
    gh_responses[a41] = r41
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_TYPE_MUTATION.format(issue_id='ISSUE_41', type_id=ISSUE_TYPE_IDS['Epic'])}")] = \
        json.dumps({"data": {"updateIssueIssueType": {"issue": {"number": 41}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_ADD_SUB_ISSUE_MUTATION.format(parent_id='ISSUE_40', child_id='ISSUE_41')}")] = \
        json.dumps({"data": {"addSubIssue": {"subIssue": {"number": 41}}}})
    a41c, r41c = _epic_check(41, parent=40, labels=["type:epic"])
    gh_responses[a41c] = r41c

    # 2. Cut the Architecture-phase Task #43.
    gh_responses[("gh", "api", "repos/owner/repo/issues", "-f", "title=Architecture phase",
                  "-f", "body=Epic #41's architecture.md.", "-f", "labels[]=type:task")] = \
        json.dumps({"number": 43})
    a43, r43 = node_id(43)
    gh_responses[a43] = r43
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_TYPE_MUTATION.format(issue_id='ISSUE_43', type_id=ISSUE_TYPE_IDS['Task'])}")] = \
        json.dumps({"data": {"updateIssueIssueType": {"issue": {"number": 43}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_ADD_SUB_ISSUE_MUTATION.format(parent_id='ISSUE_41', child_id='ISSUE_43')}")] = \
        json.dumps({"data": {"addSubIssue": {"subIssue": {"number": 43}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_43', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")] = \
        json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 43}}}})

    # 3. Cut the LLD-phase Task #44, block it on #43.
    gh_responses[("gh", "api", "repos/owner/repo/issues", "-f", "title=LLD phase",
                  "-f", "body=Epic #41's lld.md.", "-f", "labels[]=type:task")] = \
        json.dumps({"number": 44})
    a44, r44 = node_id(44)
    gh_responses[a44] = r44
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_TYPE_MUTATION.format(issue_id='ISSUE_44', type_id=ISSUE_TYPE_IDS['Task'])}")] = \
        json.dumps({"data": {"updateIssueIssueType": {"issue": {"number": 44}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_ADD_SUB_ISSUE_MUTATION.format(parent_id='ISSUE_41', child_id='ISSUE_44')}")] = \
        json.dumps({"data": {"addSubIssue": {"subIssue": {"number": 44}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_ADD_BLOCKED_BY_MUTATION.format(issue_id='ISSUE_44', blocking_id='ISSUE_43')}")] = \
        json.dumps({"data": {"addSubIssue": None}})

    # 5. `lld` carves the real functional Task #45 as a sibling.
    gh_responses[("gh", "api", "repos/owner/repo/issues", "-f", "title=Task: parse WhatsApp payload",
                  "-f", "body=Carved by epic-41's lld.", "-f", "labels[]=type:task")] = \
        json.dumps({"number": 45})
    a45, r45 = node_id(45)
    gh_responses[a45] = r45
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_TYPE_MUTATION.format(issue_id='ISSUE_45', type_id=ISSUE_TYPE_IDS['Task'])}")] = \
        json.dumps({"data": {"updateIssueIssueType": {"issue": {"number": 45}}}})
    gh_responses[("gh", "api", "graphql", "-f",
        f"query={_ADD_SUB_ISSUE_MUTATION.format(parent_id='ISSUE_41', child_id='ISSUE_45')}")] = \
        json.dumps({"data": {"addSubIssue": {"subIssue": {"number": 45}}}})
    a45c, r45c = _epic_check(45, parent=41, labels=["type:task"])
    gh_responses[a45c] = r45c

    gh_responses[tuple(_list_argv())] = _list_response([
        _epic(41, labels=["type:epic"]),
        _issue(43, parent=41, issue_type="Task", labels=["type:task"], stage="architecture"),
        _issue(45, parent=41, issue_type="Task", labels=["type:task"])])
    gh_responses[("gh", "issue", "view", "41", "--repo", "owner/repo",
                  "--json", "number,title,labels,body,state,comments")] = \
        json.dumps({"labels": []})
    del_stage_41 = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_41', field_id=STAGE_FIELD_ID)}")
    del_status_41 = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_41', field_id=PIPELINE_STATUS_FIELD_ID)}")
    gh_responses[del_stage_41] = json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 41}}}})
    gh_responses[del_status_41] = json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 41}}}})
    advance_45, _, _ = _advance_to_development_responses(45)
    gh_responses.update(advance_45)

    gh_runner = ScriptedRunner(gh_responses)
    gh_runner.prefix_responses = gh_prefixes
    gh = GitHub(runner=gh_runner)

    epic_result = cmd_create_issue(gh, "Epic: WhatsApp booklist lookup", "Slice of Initiative #40.",
                                    parent=40, labels=["type:epic"], type_name="Epic")
    assert epic_result == {"issue": 41, "parent": 40, "type": "Epic", **_CREATED_FIELDS}
    assert gh.classify_unit(41) == "epic"

    arch_task_result = cmd_create_issue(gh, "Architecture phase", "Epic #41's architecture.md.",
                                        parent=41, labels=["type:task"], type_name="Task")
    assert arch_task_result == {"issue": 43, "parent": 41, "type": "Task", **_CREATED_FIELDS}
    assert cmd_set_stage(gh, 43, "architecture") == {"issue": 43, "stage": "architecture"}

    arch_wt_runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/issue-43"): "",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-dev-43", "-b", "issue-43",
         "origin/epic-41"): "",
    })
    arch_wt_result = cmd_worktree_add(gh, 43, unit="issue", runner=arch_wt_runner,
                                      base="origin/epic-41")
    assert arch_wt_result["path"] == "/tmp/sdlc-dev-43"
    assert arch_wt_result["base"] == "origin/epic-41"

    lld_task_result = cmd_create_issue(gh, "LLD phase", "Epic #41's lld.md.",
                                       parent=41, labels=["type:task"], type_name="Task")
    assert lld_task_result == {"issue": 44, "parent": 41, "type": "Task", **_CREATED_FIELDS}
    assert cmd_add_blocked_by(gh, 44, 43) == {"issue": 44, "blocked_on": 43, "added": True}

    task_result = cmd_create_issue(gh, "Task: parse WhatsApp payload", "Carved by epic-41's lld.",
                                    parent=41, labels=["type:task"], type_name="Task")
    assert task_result == {"issue": 45, "parent": 41, "type": "Task", **_CREATED_FIELDS}
    assert gh.classify_unit(45) == "task"

    doc_path = "docs/sdlc/epic-41/lld.md"
    merge_git_runner = ScriptedRunner({
        **_live_wt("epic-41", path="/epic-41"),
        ("git", "-C", "/epic-41", "fetch", "origin"): "",
        ("git", "-C", "/epic-41", "rev-parse", "--verify", "--quiet",
         f"origin/epic-41:{doc_path}"): "blobABC\n",
        ("git", "-C", "/epic-41", "rev-parse", "origin/epic-41"): "tip41\n",
    })
    merge_result = cmd_merge_lld_doc(gh, "/epic-41", 41, runner=merge_git_runner)
    assert merge_result["merged"] is True
    assert merge_result["advanced_tasks"] == [45]


# --- classify_unit routing and Task footprints from an Epic's lld.md ---

def _labeled(issue: dict) -> dict:
    """Wrap `_issue()`'s raw label strings as `{"name": n}` for direct classifier calls."""
    return {**issue, "labels": [{"name": n} for n in issue.get("labels", [])]}


def test_is_epic_and_is_initiative_follow_classification():
    from sdlc_next import is_epic, is_initiative
    epic = _labeled(_issue(41, parent=40, issue_type="Task", labels=["type:epic"]))
    task = _labeled(_issue(42, parent=41, issue_type="Task", labels=["type:task"]))
    initiative = _labeled(_issue(40, labels=["type:initiative"]))
    feature = _labeled(_issue(90, issue_type="Feature"))  # parentless Feature, unclassified
    assert is_epic(epic) is True
    assert is_epic(task) is False
    assert is_epic(feature) is False
    assert is_initiative(initiative) is True
    assert is_epic(initiative) is False


def test_decide_next_action_accepts_an_epic_with_an_initiative_parent():
    # A fresh Epic (no phase-Tasks cut yet) has nothing to delegate.
    from sdlc_next import GitHub, decide_next_action
    v2_epic = _epic(41, parent=40)
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([v2_epic])})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 41)
    assert result == {"action": "none", "epic": 41}


def test_decide_next_action_v2_epic_delegates_its_architecture_phase_task():
    # A staged Architecture-phase Task is delegated as an ordinary child.
    from sdlc_next import GitHub, decide_next_action
    v2_epic = _epic(41, parent=40)
    arch_task = _issue(43, parent=41, stage="architecture")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([v2_epic, arch_task]),
                              **_no_blockers_responses(43)})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 41)
    assert result == {"action": "delegate", "issue": 43, "unit": "issue", "stage": "architecture"}


def test_decide_next_action_rejects_a_plain_child_issue_number():
    # A Task number (not an epic or initiative) raises.
    from sdlc_next import GitHub, GhError, decide_next_action
    task = _issue(42, parent=41, labels=["type:task"])
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([task])})
    gh = GitHub(runner=runner)
    try:
        decide_next_action(gh, 42)
        assert False, "expected GhError"
    except GhError as e:
        assert "not an epic" in str(e)


def test_parse_task_footprint_slices_correct_task_subsection():
    from sdlc_next import parse_task_footprint
    doc = (
        "# lld.md\n\n"
        "## Task #501: parse payload\n"
        "Some prose.\n\n"
        "## Footprint\n"
        "- `backend/src/whatsapp/parse.ts`\n\n"
        "## Task #502: persist result\n"
        "Some other prose.\n\n"
        "## Footprint\n"
        "- `backend/src/whatsapp/store.ts`\n"
    )
    assert parse_task_footprint(doc, 501) == ["backend/src/whatsapp/parse.ts"]
    assert parse_task_footprint(doc, 502) == ["backend/src/whatsapp/store.ts"]
    assert parse_task_footprint(doc, 999) == []


def test_list_parallel_ready_reads_v2_task_footprint_from_epic_level_lld_doc():
    # A Task's footprint is read from the Epic's own `epic-<n>/lld.md`.
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["type:epic", "epic:architected"])
    epic["parent"] = {"number": 40}
    task_501 = _issue(501, stage="development", parent=110, labels=["type:task"])
    epic_doc = (
        "## Task #501: parse payload\n"
        "## Footprint\n"
        "- `backend/src/whatsapp/parse.ts`\n"
    )
    responses = {
        tuple(_list_argv()): _list_response([epic, task_501]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
        ("git", "-C", "/repo", "show", "origin/issue-501:docs/sdlc/issue-501/lld.md"): "",
        ("git", "-C", "/repo", "show", "origin/issue-501:docs/sdlc/issue-501/architecture.md"): "",
        ("git", "-C", "/repo", "show", "origin/epic-110:docs/sdlc/epic-110/lld.md"): epic_doc,
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/issue-501"): "",
    }
    responses.update(_no_blockers_responses(501))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["skipped"] == []
    assert [c["issue"] for c in result["parallel_ready"]] == [501]


# --- decide_next_action's Initiative branch (_decide_initiative_next_action) ---

def test_decide_next_action_initiative_with_no_children_is_none():
    # A fresh Initiative waits for the orchestrator to create its Product-Roadmap Task.
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative])})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 40)
    assert result["action"] == "none" and result["epic"] == 40
    assert "Product-Roadmap Task" in result["reason"]


def test_decide_next_action_initiative_delegates_its_roadmap_task():
    # An Initiative's unstaged Product-Roadmap Task defaults to `product`.
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    roadmap_task = _issue(41, parent=40)
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([initiative, roadmap_task]),
        **_no_blockers_responses(41),
        **_stage_assign_responses(41, "product"),
    })
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 40)
    assert result == {"action": "delegate", "issue": 41, "unit": "issue", "stage": "product"}


def test_decide_next_action_initiative_roadmap_task_gate_pending_dispatches_pass_gate():
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    roadmap_task = _issue(41, parent=40, stage="product", status="awaiting-human-review")
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([initiative, roadmap_task]),
        ("gh", "issue", "view", "41", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:500 -->"}]}),
        ("gh", "pr", "view", "500", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-09-14T00:00:00Z"}),
    })
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 40)
    assert result["action"] == "pass-gate"
    assert result["issue"] == 41
    assert result["unit"] == "issue"


def test_decide_next_action_initiative_roadmap_task_closed_ready_to_cut_epics():
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    roadmap_task = _issue(41, parent=40, state="CLOSED")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, roadmap_task])})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 40)
    assert result["action"] == "none"
    assert "cut Epics" in result["reason"]


def test_decide_next_action_initiative_gate_a_passed_epics_still_open():
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    roadmap_task = _issue(41, parent=40, state="CLOSED")
    cut_epic = _issue(42, parent=40, labels=["type:epic"], state="OPEN")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, roadmap_task, cut_epic])})
    gh = GitHub(runner=runner)
    runner.responses.update(_no_blockers_responses(42))
    result = decide_next_action(gh, 40)
    assert (result["action"], result["epic"]) == ("cut-phase-tasks", 42)


def test_decide_next_action_initiative_gate_a_passed_all_epics_closed():
    from sdlc_next import GitHub, decide_next_action
    initiative = _issue(40, labels=["type:initiative"])
    roadmap_task = _issue(41, parent=40, state="CLOSED")
    cut_epic = _issue(42, parent=40, labels=["type:epic"], state="CLOSED")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, roadmap_task, cut_epic])})
    gh = GitHub(runner=runner)
    result = decide_next_action(gh, 40)
    assert result["action"] == "none"
    assert "initiative-close" in result["reason"]


# --- closing an Initiative ---

def test_check_initiative_closeable_true_when_all_cut_epics_closed():
    from sdlc_next import GitHub, cmd_check_initiative_closeable
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="CLOSED")
    e2 = _issue(42, parent=40, labels=["type:epic"], state="CLOSED")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, e1, e2])})
    gh = GitHub(runner=runner)
    result = cmd_check_initiative_closeable(gh, 40)
    assert result == {"initiative": 40, "closeable": True, "epics": [41, 42]}


def test_check_initiative_closeable_false_with_open_epics_listed():
    from sdlc_next import GitHub, cmd_check_initiative_closeable
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="CLOSED")
    e2 = _issue(42, parent=40, labels=["type:epic"], state="OPEN")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, e1, e2])})
    gh = GitHub(runner=runner)
    result = cmd_check_initiative_closeable(gh, 40)
    assert result["closeable"] is False
    assert result["open_epics"] == [42]


def test_check_initiative_closeable_false_when_no_epics_cut_yet():
    from sdlc_next import GitHub, cmd_check_initiative_closeable
    initiative = _issue(40, labels=["type:initiative"])
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative])})
    gh = GitHub(runner=runner)
    result = cmd_check_initiative_closeable(gh, 40)
    assert result == {"initiative": 40, "closeable": False,
                       "reason": "no Epics have been cut from this Initiative yet"}


def test_check_initiative_closeable_rejects_non_initiative():
    from sdlc_next import GitHub, GhError, cmd_check_initiative_closeable
    epic = _epic(90)
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic])})
    gh = GitHub(runner=runner)
    try:
        cmd_check_initiative_closeable(gh, 90)
        assert False, "expected GhError"
    except GhError as e:
        assert "is not an Initiative" in str(e)


def test_missing_initiative_verification_detects_absence_and_presence():
    from sdlc_next import missing_initiative_verification
    assert missing_initiative_verification([]) != []
    # A marker from before outcomes were recorded reads as met.
    assert missing_initiative_verification(
        [{"body": "<!-- initiative-verification: requirements:40 @ 2026-09-14T00:00:00Z -->"}]) == []
    assert missing_initiative_verification(
        [{"body": "<!-- initiative-verification: requirements:40 outcome:met @ 2026-09-14T00:00:00Z -->"}]) == []


def test_an_unmet_initiative_verification_is_not_closing_evidence():
    """Regression: a recorded `unmet` outcome must block the close; a later `met` clears it."""
    from sdlc_next import missing_initiative_verification
    unmet = {"body": "<!-- initiative-verification: requirements:40 outcome:unmet @ 2026-09-14T00:00:00Z -->"}
    met = {"body": "<!-- initiative-verification: requirements:40 outcome:met @ 2026-09-15T00:00:00Z -->"}
    problems = missing_initiative_verification([unmet])
    assert problems and "unmet" in problems[0]
    assert missing_initiative_verification([met, unmet]) != []   # the latest record wins
    assert missing_initiative_verification([unmet, met]) == []


@pytest.mark.parametrize("outcome", ["met", "unmet"])
def test_record_initiative_verification_posts_marked_comment(outcome):
    from sdlc_next import GitHub, cmd_record_initiative_verification
    runner = ScriptedRunner()
    runner.prefix_responses = {("gh", "issue", "comment", "40"): ""}
    gh = GitHub(runner=runner)
    result = cmd_record_initiative_verification(
        gh, 40, "Every requirement in product.md checked live.", outcome)
    assert result == {"initiative": 40, "kind": "requirements", "outcome": outcome,
                      "recorded": True}
    comment_call = next(c for c in runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert f"<!-- initiative-verification: requirements:40 outcome:{outcome} @" in body


def test_record_initiative_verification_refuses_an_unknown_outcome():
    from sdlc_next import GitHub, GhError, cmd_record_initiative_verification
    runner = ScriptedRunner()
    gh = GitHub(runner=runner)
    with pytest.raises(GhError, match="outcome must be one of"):
        cmd_record_initiative_verification(gh, 40, "s", "partial")
    assert runner.calls == []


def test_close_initiative_refuses_on_an_unmet_verification():
    from sdlc_next import GitHub, cmd_close_initiative
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="CLOSED")
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([initiative, e1]),
        ("gh", "issue", "view", "40", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- initiative-verification: requirements:40 "
                                              "outcome:unmet @ 2026-09-14T00:00:00Z -->"}]}),
    })
    gh = GitHub(runner=runner)
    result = cmd_close_initiative(gh, 40)
    assert result["closed"] is False and "unmet" in result["reason"]
    assert not any(c[:3] == ["gh", "issue", "close"] for c in runner.calls)


def test_close_initiative_refuses_when_not_closeable():
    from sdlc_next import GitHub, cmd_close_initiative
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="OPEN")
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([initiative, e1])})
    gh = GitHub(runner=runner)
    result = cmd_close_initiative(gh, 40)
    assert result["closed"] is False
    assert result["open_epics"] == [41]
    assert not any(c[:3] == ["gh", "issue", "close"] for c in runner.calls)


def test_close_initiative_refuses_when_verification_missing():
    from sdlc_next import GitHub, cmd_close_initiative
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="CLOSED")
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([initiative, e1]),
        ("gh", "issue", "view", "40", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": []}),
    })
    gh = GitHub(runner=runner)
    result = cmd_close_initiative(gh, 40)
    assert result["closed"] is False
    assert "missing_verification" in result
    assert not any(c[:3] == ["gh", "issue", "close"] for c in runner.calls)


def test_close_initiative_closes_when_clean():
    from sdlc_next import (GitHub, cmd_close_initiative, _ISSUE_NODE_ID_QUERY,
                            _DELETE_ISSUE_FIELD_VALUE_MUTATION, _SET_ISSUE_FIELD_MUTATION,
                            STAGE_FIELD_ID, PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    initiative = _issue(40, labels=["type:initiative"])
    e1 = _issue(41, parent=40, labels=["type:epic"], state="CLOSED")
    runner = ScriptedRunner({
        **dict([_epic_check(40, labels=["type:initiative"])]),
        ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=40)}"):
            json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_40"}}}}),
        ("gh", "api", "graphql", "-f",
         f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_40', field_id=STAGE_FIELD_ID)}"):
            json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 40}}}}),
        ("gh", "api", "graphql", "-f",
         f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_40', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['done'])}"):
            json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 40}}}}),
        tuple(_list_argv()): _list_response([initiative, e1]),
        ("gh", "issue", "view", "40", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [
                {"body": "<!-- initiative-verification: requirements:40 @ 2026-09-14T00:00:00Z -->"},
            ]}),
    })
    runner.prefix_responses = {("gh", "issue", "close", "40"): ""}
    gh = GitHub(runner=runner)
    result = cmd_close_initiative(gh, 40)
    assert result == {"initiative": 40, "closed": True, "epics": [41]}
    close_call = next(c for c in runner.calls if c[:3] == ["gh", "issue", "close"])
    assert close_call[:6] == ["gh", "issue", "close", "40", "--repo", "owner/repo"]


# --- dev/pr-review read only their own Task subsection of the Epic lld ---

def test_slice_task_subsection_returns_only_that_task():
    from sdlc_next import slice_task_subsection
    doc = (
        "# lld.md\n\n"
        "## Task #501: parse payload\n"
        "Design for 501.\n\n"
        "## Footprint\n- `a.ts`\n\n"
        "## Task #502: persist\n"
        "Design for 502.\n\n"
        "## Footprint\n- `b.ts`\n"
    )
    s501 = slice_task_subsection(doc, 501)
    assert s501.startswith("## Task #501: parse payload")
    assert "Design for 501." in s501
    assert "Task #502" not in s501
    assert "Design for 502." not in s501
    assert slice_task_subsection(doc, 999) is None


def test_cmd_lld_section_prints_only_the_requested_task_subsection(capsys):
    from sdlc_next import cmd_lld_section
    argv = ("git", "-C", "/repo", "show", "origin/epic-430:docs/sdlc/epic-430/lld.md")
    doc = (
        "# lld.md\n\n"
        "## Task #501: parse\nDesign 501.\n\n## Footprint\n- `a.ts`\n\n"
        "## Task #502: persist\nDesign 502.\n\n## Footprint\n- `b.ts`\n"
    )
    runner = ScriptedRunner({argv: doc})
    result = cmd_lld_section("/repo", 430, 501, runner=runner)
    out = capsys.readouterr().out
    assert out.startswith("## Task #501: parse")
    assert "Design 501." in out
    assert "Task #502" not in out
    assert result == {"ok": True, "epic": 430, "task": 501, "chars": len(out)}


def test_cmd_lld_section_raises_when_task_subsection_absent():
    import pytest
    from sdlc_next import cmd_lld_section, GhError
    argv = ("git", "-C", "/repo", "show", "origin/epic-430:docs/sdlc/epic-430/lld.md")
    runner = ScriptedRunner({argv: "# lld.md\n\n## Task #501: only\nx\n"})
    with pytest.raises(GhError, match="Task #999"):
        cmd_lld_section("/repo", 430, 999, runner=runner)


def test_cmd_lld_section_raises_when_epic_lld_missing():
    import pytest
    from sdlc_next import cmd_lld_section, GhError
    argv = ("git", "-C", "/repo", "show", "origin/epic-430:docs/sdlc/epic-430/lld.md")
    runner = ScriptedRunner({})
    runner.fail_on = {argv}
    with pytest.raises(GhError, match="is the epic's lld merged"):
        cmd_lld_section("/repo", 430, 501, runner=runner)


# --- a docs-only base delta carries the attestation forward ---

def test_base_delta_needs_reattest_classifies_deltas():
    from sdlc_next import base_delta_needs_reattest, CONFIG_FILENAME
    assert base_delta_needs_reattest([]) is False                       # base moved, no net files
    assert base_delta_needs_reattest(["docs/sdlc/epic-1/lld.md"]) is False
    assert base_delta_needs_reattest(["backend/src/x.ts"]) is True      # a required suite's tree
    assert base_delta_needs_reattest([".github/workflows/backend-ci.yml"]) is True
    assert base_delta_needs_reattest([CONFIG_FILENAME]) is True         # pipeline config
    assert base_delta_needs_reattest([f"docs/x{i}.md" for i in range(300)]) is True  # cap: possibly truncated


def test_merge_pr_refuses_behind_base_when_delta_touches_suite_covered_files():
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "2\n",
        ("gh", "api", "repos/owner/repo/compare/issue-9...main", "--jq", ".files[]?.filename"):
            "backend/src/service.ts\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
    })
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is False
    assert result["behind_base"] == 2
    assert "suite-covered" in result["reason"]
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_merge_pr_carries_attestation_forward_when_behind_base_is_docs_only():
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"): json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "2\n",
        ("gh", "api", "repos/owner/repo/compare/issue-9...main", "--jq", ".files[]?.filename"):
            "docs/sdlc/epic-1/lld.md\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch", "--match-head-commit", "abc"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {
        ("gh", "pr", "comment", "42"): "",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is True
    assert result["carried_attestation_forward"] is True
    assert result["behind_base"] == 2
    assert result["base_delta_files"] == 1
    assert any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)  # merged, no re-sync demanded


# --- sync-branch --base, and a base that is not on origin ---

def test_sync_branch_base_override_never_consults_integration_base():
    # With --base, integration_base is never consulted (this `gh` raises on any call).
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        **_live_wt("issue-43"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/main"): "",
        ("git", "-C", "/repo", "checkout", "issue-43"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-43"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-43"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-43"): "",
    })
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_sync_branch(gh, "/repo", 43, runner=runner, base="origin/main")
    assert result["synced"] is True
    assert result["base"] == "main"  # `origin/` accepted and normalised


def test_sync_branch_reports_a_missing_base_as_a_structured_result_not_a_crash():
    # A base missing on origin is a structured `base_missing` result, not a crash.
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        **_live_wt("issue-43"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
    })
    runner.fail_on = {("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
                        "refs/remotes/origin/epic-53")}
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_sync_branch(gh, "/repo", 43, runner=runner, base="epic-53")
    assert result["synced"] is False
    assert result["base_missing"] is True
    assert "epic-53" in result["reason"]
    assert not any(c[3] == "merge" for c in runner.calls if len(c) > 3)


def test_sync_branch_without_base_still_auto_detects_the_integration_base():
    """Without --base, the base is resolved from the parent epic and the branch merged and pushed."""
    from sdlc_next import GitHub, cmd_sync_branch
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="development", parent=110)
    runner = ScriptedRunner({
        **_live_wt("issue-185"),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/epic-110"): "",
        ("git", "-C", "/repo", "checkout", "issue-185"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet", "refs/remotes/origin/issue-185"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", "origin/issue-185"): "",
        ("git", "-C", "/repo", "merge", "origin/epic-110"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-185"): "",
    })
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    result = cmd_sync_branch(gh, "/repo", 185, runner=runner)
    assert result["base"] == "epic-110" and result["synced"] is True


# --- configured branch prefixes ---

@pytest.fixture
def custom_prefixes(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "ISSUE_BRANCH_PREFIX", "feat/")
    monkeypatch.setattr(sdlc_next, "EPIC_BRANCH_PREFIX", "integ/")


@pytest.mark.parametrize("unit,branch", [("issue", "feat/43"), ("epic", "integ/43")])
def test_sync_branch_uses_the_configured_branch_prefix(custom_prefixes, unit, branch):
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        **_live_wt(branch),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/main"): "",
        ("git", "-C", "/repo", "checkout", branch): "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         f"refs/remotes/origin/{branch}"): "",
        ("git", "-C", "/repo", "merge", "--ff-only", f"origin/{branch}"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", branch): "",
    })
    result = cmd_sync_branch(GitHub(runner=ScriptedRunner({})), "/repo", 43, unit=unit,
                             runner=runner, base="main")
    assert result["branch"] == branch and result["synced"] is True


def test_open_gate_uses_the_configured_branch_prefix_but_keeps_the_doc_dir(custom_prefixes):
    from sdlc_next import GitHub, cmd_open_gate
    git_runner = ScriptedRunner({("git", "-C", "/repo", "fetch", "origin"): "",
                                 ("git", "-C", "/repo", "rev-parse", "origin/feat/9"): "abc1234\n"})

    class FakeGitHub(GitHub):
        def pr_create(self, base, head, title, body, draft):
            self.head = head
            return 40

        def set_pipeline_status_field(self, issue, status):
            pass

        def issue_comment(self, issue, body):
            self.comment = body

    gh = FakeGitHub(runner=ScriptedRunner({}))
    result = cmd_open_gate(gh, "/repo", issue=9, title="Add widget", doc="product.md",
                           next_stage="architecture", summary="", runner=git_runner)
    assert result["head"] == gh.head == "feat/9"
    assert "`docs/sdlc/issue-9/product.md`" in gh.comment


def test_claim_maps_the_retired_testing_role_to_development():
    import sdlc_next
    gh = _RecordingGh()
    gh.set_stage_field = lambda n, stage: gh.calls.append(("set_stage", n, stage))
    gh.issue_comment = lambda n, body: None
    gh.pr_list_for_branch = lambda branch: []
    assert sdlc_next.cmd_claim(gh, 9, "testing") == {"issue": 9, "claimed": True}
    assert gh.calls == [("set_stage", 9, "development"), ("set_pipeline_status", 9, "in-progress")]


def test_start_comment_rejects_the_retired_testing_role(monkeypatch):
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    with pytest.raises(SystemExit):
        sdlc_next.main(["start-comment", "9", "--role", "testing"])


# --- create-issue: native type mandatory, every field set, atomic ---

class _RecordingGh:
    """create-issue's write surface, recording calls; raises GhError on the step named `fail`."""

    def __init__(self, number=42, fail=None):
        self.number, self.fail, self.calls = number, fail, []

    def _rec(self, step, *args):
        if step == self.fail:
            raise GhError(f"{step}: simulated failure")
        self.calls.append((step, *args))

    def issue_create(self, title, body, labels):
        self._rec("issue_create", title, list(labels))
        return self.number

    def set_issue_type(self, n, type_name):
        self._rec("set_issue_type", n, type_name)

    def add_sub_issue(self, parent, child):
        self._rec("add_sub_issue", parent, child)

    def set_pipeline_status_field(self, n, status):
        self._rec("set_pipeline_status", n, status)

    def set_priority_field(self, n, value):
        self._rec("set_priority", n, value)

    def set_effort_field(self, n, value):
        self._rec("set_effort", n, value)


_CREATED_FIELDS = {"pipeline_status": "todo", "priority": "Medium", "effort": "Medium"}


def test_create_issue_types_links_and_sets_every_field_in_order():
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh()
    result = cmd_create_issue(gh, "t", "b", 9, [])
    assert result == {"issue": 42, "parent": 9, "type": "Task", **_CREATED_FIELDS}
    assert gh.calls == [("issue_create", "t", ["type:task"]), ("add_sub_issue", 9, 42),
                        ("set_issue_type", 42, "Task"), ("set_pipeline_status", 42, "todo"),
                        ("set_priority", 42, "Medium"), ("set_effort", 42, "Medium")]


def test_create_issue_label_classified_epic_still_gets_its_native_type():
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh(number=43)
    result = cmd_create_issue(gh, "t", "b", 40, [], type_name="Epic")
    assert result["type"] == "Epic" and "ok" not in result
    assert ("issue_create", "t", ["type:epic"]) in gh.calls
    assert ("set_issue_type", 43, "Epic") in gh.calls


def test_create_issue_bug_gets_the_bug_type_not_task():
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh()
    cmd_create_issue(gh, "t", "b", 9, [], type_name="Bug")
    assert ("set_issue_type", 42, "Bug") in gh.calls
    assert ("issue_create", "t", []) in gh.calls


def test_create_issue_refuses_an_unprovisioned_type_before_creating(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "ISSUE_TYPE_IDS", {"Task": "IT_TASK_ID"})
    gh = _RecordingGh()
    with pytest.raises(GhError, match="'Epic' is not provisioned"):
        sdlc_next.cmd_create_issue(gh, "t", "b", 40, ["type:epic"], type_name="Epic")
    assert gh.calls == []


def test_create_issue_refuses_an_unknown_type_before_creating():
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh()
    with pytest.raises(GhError, match="not an issue type"):
        cmd_create_issue(gh, "t", "b", 9, [], type_name="Bogus")
    assert gh.calls == []


def test_create_issue_priority_and_effort_flags_win_over_defaults():
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh()
    result = cmd_create_issue(gh, "t", "b", 9, [], priority="high", effort="Low")
    assert (result["priority"], result["effort"]) == ("High", "Low")
    assert ("set_priority", 42, "High") in gh.calls and ("set_effort", 42, "Low") in gh.calls


def test_create_issue_defaults_come_from_pipeline_issue_defaults(monkeypatch):
    import sdlc_next
    monkeypatch.setitem(sdlc_next.PIPELINE, "issueDefaults", {"priority": "Low", "effort": "High"})
    result = sdlc_next.cmd_create_issue(_RecordingGh(), "t", "b", 9, [])
    assert (result["priority"], result["effort"]) == ("Low", "High")


@pytest.mark.parametrize("kwargs", [{"priority": "Nonsense"}, {"effort": "Huge"}])
def test_create_issue_refuses_an_invalid_priority_or_effort_before_creating(kwargs):
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh()
    with pytest.raises(GhError, match="not a configured option"):
        cmd_create_issue(gh, "t", "b", 9, [], **kwargs)
    assert gh.calls == []


def test_create_issue_skips_priority_and_effort_when_unconfigured(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "PRIORITY_FIELD_ID", None)
    monkeypatch.setattr(sdlc_next, "EFFORT_FIELD_ID", None)
    gh = _RecordingGh()
    result = sdlc_next.cmd_create_issue(gh, "t", "b", 9, [], priority="Critical")
    assert "priority" not in result and "effort" not in result
    assert [c[0] for c in gh.calls][-1] == "set_pipeline_status"


@pytest.mark.parametrize("step", ["set_issue_type", "add_sub_issue", "set_pipeline_status",
                                  "set_priority", "set_effort"])
def test_create_issue_reports_a_post_create_failure_and_never_recreates(step):
    from sdlc_next import cmd_create_issue
    gh = _RecordingGh(number=52, fail=step)
    result = cmd_create_issue(gh, "t", "b", 9, [])
    assert (result["ok"], result["issue"], result["failed_step"]) == (False, 52, step)
    assert "do not re-run" in result["reason"].lower() and "#52" in result["reason"]
    assert [c[0] for c in gh.calls].count("issue_create") == 1
    assert step in result["reason"]
    assert ("repair-issue 52 --parent 9 --type Task --priority Medium --effort Medium"
            in result["reason"])


# --- verify-exit checks the development->pr-review handoff marker ---

def _verify_exit_pr_runners(tmp_path, comments, stage_field_value="PR Review", pr=42):
    from sdlc_next import _ISSUE_FIELDS_QUERY
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": [], "comments": comments}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"},
             "name": stage_field_value},
        ]}}}}}),
        ("gh", "pr", "view", str(pr), "--repo", "owner/repo",
         "--json", "isDraft,headRefName,baseRefName"):
            json.dumps({"isDraft": True, "headRefName": "issue-9", "baseRefName": "main"}),
        # dev-PR scope check reads the PR's changed files (none here).
        ("gh", "api", "--paginate", f"repos/owner/repo/pulls/{pr}/files",
         "--jq", ".[].filename"): "",
        **dict([_epic_check(9)]),  # parentless: not an Epic's phase-Task
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    return gh_runner, git_runner


def test_verify_exit_flags_a_missing_development_to_pr_review_handoff_marker(tmp_path):
    # A pr-review exit without the handoff marker fails verification (reported, not repaired).
    from sdlc_next import GitHub, cmd_verify_exit
    gh_runner, git_runner = _verify_exit_pr_runners(tmp_path, comments=[])
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="pr-review", pr=42, runner=git_runner)
    assert result["handoff_marker_present"] is False
    assert result["ok"] is False
    assert any("handoff-to-pr-review" in p for p in result["problems"])
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_verify_exit_passes_when_the_handoff_marker_is_present(tmp_path):
    """A pr-review exit with the handoff marker verifies clean."""
    from sdlc_next import GitHub, cmd_verify_exit
    comments = [{"body": "<!-- stage-transition: development->pr-review "
                          "@ 2026-09-16T00:00:00Z -->"}]
    gh_runner, git_runner = _verify_exit_pr_runners(tmp_path, comments=comments)
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="pr-review", pr=42, runner=git_runner)
    assert result.get("ok", True) is True
    assert result["expected_stage_present"] is True


def test_verify_exit_does_not_require_a_handoff_marker_for_other_stages(tmp_path):
    """The handoff-marker check applies only to pr-review; an `lld` exit passes without it."""
    from sdlc_next import GitHub, cmd_verify_exit
    gh_runner, git_runner = _verify_exit_pr_runners(tmp_path, comments=[],
                                                     stage_field_value="LLD")
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="lld", pr=42, runner=git_runner)
    assert result.get("ok", True) is True
    assert "handoff_marker_present" not in result


# --- carry-forward is a positive docs-only test ---

def test_base_delta_reattests_code_when_no_required_workflows_are_configured(monkeypatch):
    # With no required workflows configured, a non-doc delta still re-attests.
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "REQUIRED_WORKFLOWS", ())
    assert sdlc_next.base_delta_needs_reattest(
        ["pom.xml", "src/test/java/AppTest.java"]) is True
    assert sdlc_next.base_delta_needs_reattest(["src/main/java/App.java"]) is True
    assert sdlc_next.base_delta_needs_reattest(["package-lock.json"]) is True


def test_base_delta_docs_only_still_carries_forward_without_workflows(monkeypatch):
    """A docs-only (or empty) delta still carries forward with no workflows configured."""
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "REQUIRED_WORKFLOWS", ())
    assert sdlc_next.base_delta_needs_reattest(["docs/sdlc/epic-1/lld.md"]) is False
    assert sdlc_next.base_delta_needs_reattest(["README.md", "docs/guide/setup.md"]) is False
    assert sdlc_next.base_delta_needs_reattest([]) is False


def test_base_delta_suite_tree_forces_reattest_even_when_every_file_is_markdown():
    """A `.md` inside a configured suite's tree still forces re-attest."""
    from sdlc_next import base_delta_needs_reattest
    assert base_delta_needs_reattest(["backend/README.md"]) is True


# --- both Task heading forms (numbered and keyed) ---

def test_parse_task_headings_reads_numbered_and_keyed_forms_and_ignores_prose():
    from sdlc_next import parse_task_headings
    doc = ("# lld\n\n"
           "## Task #51: parse payload <!-- task-key: parse-payload -->\n"
           "design\n\n"
           "## Task skeleton-health: Add /health\n"
           "Depends on: parse-payload\n\n"
           "## Task Breakdown\n"
           "prose, not a Task\n")
    entries = parse_task_headings(doc)
    assert [(e["number"], e["key"], e["task_key"]) for e in entries] == [
        (51, None, "parse-payload"), (None, "skeleton-health", None)]
    assert entries[0]["title"] == "parse payload"
    assert entries[1]["title"] == "Add /health"


def test_slice_task_subsection_stops_at_a_still_keyed_neighbour():
    # A keyed heading bounds a numbered Task's slice (mid-rewrite docs mix both forms).
    from sdlc_next import slice_task_subsection
    doc = ("## Task #51: parse\nDesign 51.\n\n"
           "## Task skeleton-health: health\nDesign health.\n")
    section = slice_task_subsection(doc, 51)
    assert "Design 51." in section and "Design health." not in section


def test_slice_task_subsection_resolves_a_section_by_its_task_key():
    from sdlc_next import slice_task_subsection
    doc = ("## Task #51: parse <!-- task-key: parse-payload -->\nDesign 51.\n\n"
           "## Task skeleton-health: health\nDesign health.\n")
    assert "Design 51." in slice_task_subsection(doc, "parse-payload")
    assert "Design health." in slice_task_subsection(doc, "skeleton-health")


def test_parse_task_depends_on_reads_declared_keys():
    from sdlc_next import parse_task_depends_on
    assert parse_task_depends_on("Depends on: `skeleton-health`, config-loader\n") == \
        ["skeleton-health", "config-loader"]
    assert parse_task_depends_on("- **Depends on**: skeleton-health.\n") == ["skeleton-health"]
    assert parse_task_depends_on("no dependencies declared here\n") == []


def test_parse_task_field_reads_priority_and_effort_lines():
    from sdlc_next import _EFFORT_LINE, _PRIORITY_LINE, parse_task_field
    section = "Design.\n- **Priority:** High\nEffort: `low`.\n"
    assert parse_task_field(section, _PRIORITY_LINE) == "High"
    assert parse_task_field(section, _EFFORT_LINE) == "low"
    assert parse_task_field("no fields here\n", _PRIORITY_LINE) is None


def test_lld_section_still_resolves_a_numbered_heading(capsys):
    """An all-numbered lld.md still resolves a Task section."""
    from sdlc_next import cmd_lld_section
    argv = ("git", "-C", "/repo", "show", "origin/epic-430:docs/sdlc/epic-430/lld.md")
    doc = ("# lld.md\n\n## Task #501: parse\nDesign 501.\n\n## Footprint\n- `a.ts`\n\n"
           "## Task #502: persist\nDesign 502.\n\n## Footprint\n- `b.ts`\n")
    result = cmd_lld_section("/repo", 430, 501, runner=ScriptedRunner({argv: doc}))
    out = capsys.readouterr().out
    assert out.startswith("## Task #501: parse")
    assert "Task #502" not in out
    assert result["ok"] is True


# --- the default Gate B confidence bar is 80 ---

def _show_config(tmp_path, mutate):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    mutate(cfg)
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    return json.loads(subprocess.check_output(
        [sys.executable, script, "show-config"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path), "GITHUB_TOKEN": "x"}, text=True))


def test_default_skip_confidence_threshold_is_80(tmp_path):
    # Read through a config that doesn't pin the bar, to observe the shipped default.
    effective = _show_config(tmp_path, lambda c: c["pipeline"].pop("gates", None))
    assert effective["gates"]["skipConfidenceThreshold"] == 80


def test_config_still_overrides_the_skip_confidence_threshold(tmp_path):
    """Config and profile overrides of the confidence bar still win."""
    effective = _show_config(tmp_path, lambda c: c["pipeline"].__setitem__(
        "gates", {"skipConfidenceThreshold": 95, "requiresHumanGateA": True}))
    assert effective["gates"]["skipConfidenceThreshold"] == 95


# --- audit-issues ---

def _full(number, **kw):
    """An issue carrying every create-issue field (Priority, Effort, Pipeline Status todo)."""
    issue = _issue(number, priority="Medium", status=kw.pop("status", "todo"), **kw)
    issue["fields"]["Effort"] = "Medium"
    return issue


class _ListGh:
    def __init__(self, issues):
        self.issues = [{**i, "labels": [{"name": l} for l in i["labels"]]} for i in issues]

    def issue_list(self):
        return self.issues


def _audit_tree():
    return _ListGh([
        _full(1, issue_type="Initiative", labels=["type:initiative"], status=None),
        _full(2, issue_type="Epic", labels=["type:epic"], parent=1, status=None),
        _full(3, issue_type="Task", labels=["type:task"], parent=2),
        _issue(4, labels=["type:task"], parent=2),
        _full(5, issue_type="Task"),
        _issue(6, state="CLOSED"),
        _full(7, issue_type="Epic", labels=["type:epic"], status=None),
        _full(8, labels=["type:task"], parent=7),
        _full(10, issue_type="Feature"),
        _issue(11, parent=10),
    ])


def test_audit_issues_lists_open_pipeline_issues_missing_fields():
    from sdlc_next import cmd_audit_issues
    result = cmd_audit_issues(_audit_tree())
    assert result["issues"][0] == {"issue": 4, "title": "issue 4", "missing": [
        "issueType", "Priority", "Effort", "Pipeline Status"], "repair": "repair-issue 4"}
    # 6 is closed; 11 hangs under a non-pipeline parent; 1/2/7 need no parent or status.
    flagged = {i["issue"]: i["missing"] for i in result["issues"]}
    assert flagged == {4: ["issueType", "Priority", "Effort", "Pipeline Status"],
                       5: ["parent"], 8: ["issueType"], 10: ["parent"]}
    assert result["count"] == 4


def test_audit_issues_never_flags_a_parentless_epic_or_initiative_but_flags_a_task():
    from sdlc_next import cmd_audit_issues
    tree = _ListGh([_full(1, issue_type="Initiative", labels=["type:initiative"], status=None),
                    _full(7, issue_type="Epic", labels=["type:epic"], status=None),
                    _full(12, issue_type="Task", labels=["type:task"]),
                    _full(13, issue_type="Bug")])
    flagged = {i["issue"]: i["missing"] for i in cmd_audit_issues(tree)["issues"]}
    assert flagged == {7: ["phase-Tasks"], 12: ["parent"], 13: ["parent"]}


def test_audit_issues_repair_hint_asks_only_for_what_cannot_be_inferred():
    from sdlc_next import cmd_audit_issues
    tree = _ListGh([_issue(20, priority="Medium", status="todo"),
                    _issue(21, labels=["type:task"], priority="Medium", status="todo")])
    hints = {i["issue"]: i["repair"] for i in cmd_audit_issues(tree)["issues"]}
    assert hints == {20: "repair-issue 20 --parent <P> --type <T>",
                     21: "repair-issue 21 --parent <P>"}


def test_audit_issues_epic_scope_is_only_that_subtree():
    # Regression: parentless issues elsewhere in the repo were flagged under --epic too.
    from sdlc_next import cmd_audit_issues
    flagged = {i["issue"] for i in cmd_audit_issues(_audit_tree(), epic=2)["issues"]}
    assert flagged == {4}


def test_audit_issues_without_epic_still_flags_parentless_issues():
    from sdlc_next import cmd_audit_issues
    flagged = {i["issue"] for i in cmd_audit_issues(_audit_tree())["issues"]}
    assert {5, 10} <= flagged


def test_audit_issues_skips_unconfigured_priority_and_effort(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next, "PRIORITY_FIELD_ID", None)
    monkeypatch.setattr(sdlc_next, "EFFORT_FIELD_ID", None)
    flagged = {i["issue"]: i["missing"] for i in sdlc_next.cmd_audit_issues(_audit_tree())["issues"]}
    assert flagged[4] == ["issueType", "Pipeline Status"]


# --- resolve-thread ---

def _thread_argv(query, **variables):
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        argv += ["-f", f"{name}={value}"]
    return tuple(argv)


def test_resolve_thread_replies_then_resolves():
    from sdlc_next import (_REPLY_REVIEW_THREAD_MUTATION, _RESOLVE_REVIEW_THREAD_MUTATION,
                           cmd_resolve_thread)
    reply = _thread_argv(_REPLY_REVIEW_THREAD_MUTATION, threadId="PRRT_1", body='Fixed "x".')
    resolve = _thread_argv(_RESOLVE_REVIEW_THREAD_MUTATION, threadId="PRRT_1")
    runner = ScriptedRunner({reply: '{"data": {}}', resolve: '{"data": {}}'})
    result = cmd_resolve_thread(GitHub(runner=runner), "PRRT_1", 'Fixed "x".')
    assert result == {"thread": "PRRT_1", "replied": True, "resolved": True}
    assert [tuple(c) for c in runner.calls] == [reply, resolve]


def test_resolve_thread_without_reply_only_resolves():
    from sdlc_next import _RESOLVE_REVIEW_THREAD_MUTATION, cmd_resolve_thread
    resolve = _thread_argv(_RESOLVE_REVIEW_THREAD_MUTATION, threadId="PRRT_1")
    runner = ScriptedRunner({resolve: '{"data": {}}'})
    assert cmd_resolve_thread(GitHub(runner=runner), "PRRT_1")["replied"] is False
    assert len(runner.calls) == 1


def test_resolve_thread_reports_a_failed_resolve_after_replying():
    from sdlc_next import (_REPLY_REVIEW_THREAD_MUTATION, _RESOLVE_REVIEW_THREAD_MUTATION,
                           cmd_resolve_thread)
    reply = _thread_argv(_REPLY_REVIEW_THREAD_MUTATION, threadId="PRRT_1", body="done")
    resolve = _thread_argv(_RESOLVE_REVIEW_THREAD_MUTATION, threadId="PRRT_1")
    runner = ScriptedRunner({reply: '{"data": {}}', resolve: '{"data": {}}'})
    runner.fail_on.add(resolve)
    result = cmd_resolve_thread(GitHub(runner=runner), "PRRT_1", "done")
    assert (result["ok"], result["replied"], result["resolved"]) == (False, True, False)
    assert "without --reply" in result["reason"]


# --- CLI wiring for the create-issue flags and the new commands ---

@pytest.mark.parametrize("argv, fn, expected", [
    (["create-issue", "--title", "t", "--body", "b", "--parent", "9", "--type", "Bug",
      "--priority", "High", "--effort", "Low"], "cmd_create_issue",
     ("t", "b", 9, [], "Bug", "High", "Low", [])),
    (["audit-issues", "--epic", "7"], "cmd_audit_issues", (7,)),
    (["resolve-thread", "--thread-id", "PRRT_1", "--reply", "ok"], "cmd_resolve_thread",
     ("PRRT_1", "ok")),
])
def test_cli_passes_new_flags_through(monkeypatch, capsys, argv, fn, expected):
    import sdlc_next
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(sdlc_next, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(sdlc_next, fn, lambda gh, *args: seen.append((gh, *args)) or {})
    assert sdlc_next.main(argv) == 0
    assert seen == [("GH", *expected)]


def test_missing_token_error_names_the_session_start_hook(monkeypatch, capsys):
    import sdlc_next
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert sdlc_next.main(["list-needs-human"]) == 1
    assert "SessionStart hook" in json.loads(capsys.readouterr().out)["error"]


# --- footprint parsing: exact heading, sub-label stop ---

def test_parse_footprint_skips_a_decoy_heading_and_stops_at_a_sublabel():
    # Regression: `## Footprint overlap ...` was taken for the section (footprint `[]`
    # or wrong), and `**Verify-only:**` paths were collected as owned.
    from sdlc_next import parse_footprint
    doc = ("## Footprint overlap outside this task\n- `src/other.ts`\n\n"
           "## Footprint\n- `src/real.ts`\n- `src/also.ts`\n"
           "**Verify-only:**\n- `src/readonly.ts`\n\n## Next\n")
    assert parse_footprint(doc) == ["src/real.ts", "src/also.ts"]


def test_parse_footprint_still_reads_numbered_and_colon_headings():
    from sdlc_next import parse_footprint
    assert parse_footprint("## 17. Footprint\n- `a/b.ts`\n") == ["a/b.ts"]
    assert parse_footprint("### Footprint:\n- `c/d.ts`\n") == ["c/d.ts"]


# --- required workflows: excludeGlobs, hyphenated suites, commandPattern ---

def test_missing_required_workflows_ignores_a_docs_only_touch_matching_exclude_globs():
    # Regression: `backend/AGENTS.md` demanded a suite the workflow's `!**/*.md` skips.
    from sdlc_next import missing_required_workflows
    assert missing_required_workflows(["backend/AGENTS.md"], []) == []


def test_missing_required_workflows_still_requires_the_suite_for_code_under_the_prefix():
    from sdlc_next import missing_required_workflows
    assert missing_required_workflows(["backend/AGENTS.md", "backend/src/x.ts"], []) == [
        "Backend CI"]


def test_workflow_covers_applies_a_leading_double_star_glob_at_the_root():
    from sdlc_next import _workflow_covers
    spec = {"prefixes": ("",), "files": (), "excludeGlobs": ("**/*.md",)}
    assert _workflow_covers(spec, "README.md") is False
    assert _workflow_covers(spec, "src/app.ts") is True


def test_local_ci_marker_parses_a_hyphenated_suite():
    # Regression: `\w+` stopped at the hyphen, so an `e2e-smoke` attestation never matched.
    from sdlc_next import local_ci_suites_attested
    comments = [{"body": "<!-- local-ci: e2e-smoke:42 @ abc1234 -->"},
                {"body": "<!-- local-ci: backend:42 @ abc1234 -->"}]
    assert local_ci_suites_attested(comments, "abc1234") == {"e2e-smoke", "backend"}


def _config_with_workflow(tmp_path, **workflow):
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["requiredWorkflows"][0].update(workflow)
    path = tmp_path / "sdlc-pipeline.config.json"
    path.write_text(json.dumps(cfg))
    return path


def test_config_load_rejects_a_suite_key_the_marker_cannot_match(tmp_path):
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    env = {**os.environ, "GITHUB_TOKEN": "x"}
    bad = subprocess.run([sys.executable, script, "show-config"], capture_output=True, text=True,
                         env={**env, "SDLC_CONFIG": str(_config_with_workflow(tmp_path,
                                                                              suite="e2e smoke"))})
    assert bad.returncode != 0 and "must match" in bad.stderr
    good = subprocess.run([sys.executable, script, "show-config"], capture_output=True, text=True,
                          env={**env, "SDLC_CONFIG": str(_config_with_workflow(tmp_path,
                                                                               suite="e2e-smoke"))})
    assert "e2e-smoke" in json.loads(good.stdout)["localCiSuites"]


def test_record_local_ci_refuses_a_command_not_matching_the_suite_pattern(monkeypatch, tmp_path):
    import sdlc_next
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    monkeypatch.setitem(sdlc_next.LOCAL_CI_COMMAND_PATTERNS, "backend", r"test:coverage")
    runner = ScriptedRunner({})
    with pytest.raises(GhError, match="commandPattern"):
        cmd_record_local_ci(GitHub(runner=runner), 42, "backend", "abc1234",
                            "npm run test:it", _ci_output(tmp_path))
    assert runner.calls == []


def test_record_local_ci_attests_a_command_matching_the_suite_pattern(monkeypatch, tmp_path):
    import sdlc_next
    from sdlc_next import GitHub, cmd_record_local_ci
    monkeypatch.setitem(sdlc_next.LOCAL_CI_COMMAND_PATTERNS, "backend", r"test:coverage")
    runner = ScriptedRunner({})
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    result = cmd_record_local_ci(GitHub(runner=runner), 42, "backend", "abc1234",
                                 "npm run test:coverage", _ci_output(tmp_path))
    assert result["attested"] is True


# --- verify-exit: stage spellings ---

def test_normalize_stage_accepts_field_value_slug_and_case():
    from sdlc_next import normalize_stage
    assert {normalize_stage(x) for x in ("PR Review", "pr-review", "pr review")} == {"pr-review"}
    assert normalize_stage("Development") == "development"
    assert normalize_stage("nonsense") is None


def test_verify_exit_accepts_the_field_display_spelling_of_the_stage(tmp_path):
    # Regression: `--expect-stage "PR Review"` failed against the stored slug `pr-review`.
    from sdlc_next import GitHub, cmd_verify_exit
    gh_runner, git_runner = _verify_exit_runners(tmp_path, "Development")
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="Development", runner=git_runner)
    assert result["expected_stage_present"] is True and result.get("ok") is not False


def test_verify_exit_names_accepted_spellings_for_an_unknown_stage(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit
    gh_runner, git_runner = _verify_exit_runners(tmp_path, "Development")
    result = cmd_verify_exit(GitHub(runner=gh_runner), str(tmp_path), 9,
                             expect_stage="devlopment", runner=git_runner)
    assert result["ok"] is False and "unrecognised --expect-stage" in result["reason"]


# --- claim refuses development with an open PR ---

_PR_LIST_ISSUE_9 = ("gh", "pr", "list", "--repo", "owner/repo", "--head", "issue-9",
                    "--state", "open", "--json", "number,isDraft,headRefName,title,url")


def test_claim_refuses_development_when_the_unit_has_an_open_pr():
    # Regression: re-claiming moved Stage off PR Review and dropped the unit from review.
    from sdlc_next import GitHub, GhError, cmd_claim
    runner = ScriptedRunner({_PR_LIST_ISSUE_9: json.dumps([{"number": 77}])})
    with pytest.raises(GhError, match="already has open PR #77"):
        cmd_claim(GitHub(runner=runner), 9, "development")
    assert runner.calls == [list(_PR_LIST_ISSUE_9)]


def test_claim_development_proceeds_with_no_open_pr():
    import sdlc_next
    gh = _RecordingGh()
    gh.set_stage_field = lambda n, stage: gh.calls.append(("set_stage", n, stage))
    gh.issue_comment = lambda n, body: None
    gh.pr_list_for_branch = lambda branch: []
    assert sdlc_next.cmd_claim(gh, 9, "development") == {"issue": 9, "claimed": True}
    assert ("set_stage", 9, "development") in gh.calls


# --- worktree-add / reconcile: stale local branch, transient network retry ---

def _stale_branch_runner(unique_commits: str):
    add = ("git", "-C", "/r", "worktree", "add", "/wt", "-b", "issue-5", "origin/main")
    runner = ScriptedRunner({
        ("git", "-C", "/r", "branch", "--list", "issue-5"): "  issue-5\n",
        ("git", "-C", "/r", "rev-list", "--count", "origin/main..issue-5"): unique_commits,
        ("git", "-C", "/r", "branch", "-D", "issue-5"): "",
    })
    runner.fail_on = {add}

    def delete_unblocks_add(argv, _call=runner.__call__):
        if argv[3:5] == ["branch", "-D"]:
            runner.fail_on = set()
            runner.responses[add] = ""
        return _call(argv)
    return runner, delete_unblocks_add


def test_add_fresh_worktree_recreates_a_stale_branch_with_no_unique_commits():
    # Regression: a crashed run's leftover local branch made `worktree add -b` fail forever.
    from sdlc_next import _add_fresh_worktree
    runner, call = _stale_branch_runner("0\n")
    _add_fresh_worktree("/r", "/wt", "issue-5", "origin/main", call)
    assert runner.calls[-2:] == [["git", "-C", "/r", "branch", "-D", "issue-5"],
                                 ["git", "-C", "/r", "worktree", "add", "/wt", "-b", "issue-5",
                                  "origin/main"]]


def test_add_fresh_worktree_refuses_to_discard_a_branch_with_unique_commits():
    from sdlc_next import _add_fresh_worktree, GhError
    runner, call = _stale_branch_runner("2\n")
    with pytest.raises(GhError, match="2 commit"):
        _add_fresh_worktree("/r", "/wt", "issue-5", "origin/main", call)
    assert ["git", "-C", "/r", "branch", "-D", "issue-5"] not in runner.calls


def test_run_retry_transient_retries_a_network_error_once(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next.time, "sleep", lambda s: None)
    attempts = []

    def runner(argv):
        attempts.append(argv)
        if len(attempts) == 1:
            raise sdlc_next.GhError("ssh: connect to host github.com port 22: Connection timed out")
        return "ok"
    assert sdlc_next._run_retry_transient(["git", "fetch"], runner) == "ok"
    assert len(attempts) == 2


def test_run_retry_transient_does_not_retry_a_non_network_error(monkeypatch):
    import sdlc_next
    monkeypatch.setattr(sdlc_next.time, "sleep", lambda s: None)
    attempts = []

    def runner(argv):
        attempts.append(argv)
        raise sdlc_next.GhError("! [rejected] issue-9 -> issue-9 (non-fast-forward)")
    with pytest.raises(sdlc_next.GhError):
        sdlc_next._run_retry_transient(["git", "push"], runner)
    assert len(attempts) == 1


# --- merge-pr: idempotent on an already-merged PR, 5xx after the squash landed ---

def _merged_pr_bookkeeping(state_responses):
    """Runner for merge-pr on PR 42 / issue 9 whose `pr view --json state` answers in turn."""
    state_argv = ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state")
    runner = ScriptedRunner({
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/lld.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "pr", "comment", "42"): "",
                               ("gh", "issue", "comment", "9"): ""}
    states = iter(state_responses)

    def call(argv):
        if tuple(argv) == state_argv:
            runner.calls.append(argv)
            return json.dumps({"state": next(states)})
        return runner(argv)
    return runner, call


def test_merge_pr_on_an_already_merged_pr_only_finishes_the_bookkeeping():
    # Regression: a retry after a merge that landed refused as behind-base.
    from sdlc_next import GitHub, cmd_merge_pr
    runner, call = _merged_pr_bookkeeping(["MERGED"])
    result = cmd_merge_pr(GitHub(runner=call), 42, issue=9)
    assert result["merged"] is True and result["recovered"] is True
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls)


def test_merge_pr_recovers_when_the_merge_call_fails_after_the_squash_landed():
    from sdlc_next import GitHub, cmd_merge_pr
    runner, call = _merged_pr_bookkeeping(["OPEN", "MERGED"])
    runner.fail_on = {("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash",
                       "--delete-branch", "--match-head-commit", "abc")}
    result = cmd_merge_pr(GitHub(runner=call), 42, issue=9)
    assert result["merged"] is True and "recovered" not in result


def test_merge_pr_still_raises_when_the_failed_merge_left_the_pr_open():
    from sdlc_next import GitHub, GhError, cmd_merge_pr
    runner, call = _merged_pr_bookkeeping(["OPEN", "OPEN"])
    runner.fail_on = {("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash",
                       "--delete-branch", "--match-head-commit", "abc")}
    with pytest.raises(GhError):
        cmd_merge_pr(GitHub(runner=call), 42, issue=9)


# --- show-config: running plugin version ---

def test_plugin_version_info_reports_manifest_version_and_own_checkout_sha():
    import sdlc_next
    manifest = json.loads((Path(sdlc_next.PLUGIN_ROOT) / ".claude-plugin" / "plugin.json").read_text())
    info = sdlc_next.plugin_version_info(
        runner=lambda argv: f"{sdlc_next.PLUGIN_ROOT}\ncafe123\n")
    assert (info["version"], info["sha"]) == (manifest["version"], "cafe123")


def test_plugin_version_info_ignores_the_head_of_an_enclosing_repo():
    import sdlc_next
    info = sdlc_next.plugin_version_info(runner=lambda argv: "/some/other/repo\ncafe123\n")
    assert info["sha"] is None and info["version"]


def test_plugin_version_info_without_git_reports_no_sha():
    import sdlc_next

    def no_git(argv):
        raise sdlc_next.GhError("fatal: not a git repository")
    assert sdlc_next.plugin_version_info(runner=no_git)["sha"] is None


def test_show_config_cli_reports_the_plugin_version(tmp_path):
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    cfg = Path(__file__).resolve().parents[2] / "sdlc.config.sample.json"
    out = subprocess.check_output([sys.executable, script, "show-config"], text=True,
                                  env={**os.environ, "SDLC_CONFIG": str(cfg), "GITHUB_TOKEN": "x"})
    assert json.loads(out)["plugin"]["version"]


def test_missing_token_error_names_the_configured_token_env(monkeypatch, capsys):
    import sdlc_next
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sdlc_next, "TOKEN_ENV", "MY_GH_TOKEN")
    assert sdlc_next.main(["list-needs-human"]) == 1
    assert "tokenEnv ($MY_GH_TOKEN)" in json.loads(capsys.readouterr().out)["error"]
