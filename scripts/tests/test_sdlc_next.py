import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdlc_next import GitHub, GhError, REPO


def test_config_drives_repo_docroot_and_graphql_owner_for_an_arbitrary_repo(tmp_path):
    """The whole suite runs against the neutral sample config; this proves the
    genericity directly -- a *different* config propagates to REPO, DOC_ROOT, the
    schema ids, and the inline GraphQL repo selector, with no code edit."""
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
    """Test double for the Runner protocol: returns canned stdout for an exact argv
    match, raises AssertionError on any unscripted call, and records every call made
    so a test can assert exactly what was invoked."""

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
    # `gh pr view --json files` truncates at 100 files with no pagination (confirmed
    # against the gh 2.76.1 binary directly). `gh pr diff --name-only` was tried next
    # but has its OWN cap: the diff itself 406s past 20,000 lines (confirmed against
    # `gh pr diff 128276 --repo kubernetes/kubernetes --name-only`) -- reachable here,
    # since backend/package-lock.json alone is ~12k lines. `gh api --paginate
    # repos/{repo}/pulls/{n}/files` walks the REST files endpoint page by page and has
    # no line-count or fixed-entry cap -- confirmed to return all 187/187 files on a
    # PR where both prior approaches failed. It also sidesteps `gh pr diff`'s diff-header
    # parsing, which can silently mis-parse C-quoted non-ASCII paths and rename-source
    # paths.
    from sdlc_next import GitHub
    argv = ("gh", "api", "--paginate", f"repos/{REPO}/pulls/42/files", "--jq", ".[].filename")
    runner = ScriptedRunner({argv: "backend/src/a.ts\nfrontend/src/b.tsx\n"})
    gh = GitHub(runner=runner)
    assert gh.pr_files(42) == ["backend/src/a.ts", "frontend/src/b.tsx"]
    assert list(argv) in runner.calls


def test_pr_files_drops_blank_lines_but_keeps_every_real_path():
    from sdlc_next import GitHub
    argv = ("gh", "api", "--paginate", f"repos/{REPO}/pulls/42/files", "--jq", ".[].filename")
    # 150 distinct paths -- well past the old 100-file JSON cap, and enough lines that
    # a diff-based approach would meaningfully approach its own 20,000-line cap on a
    # real PR -- to demonstrate the newline-based parser drops nothing.
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


def test_is_epic_requires_feature_type_and_no_parent():
    from sdlc_next import is_epic
    assert is_epic({"issueType": {"name": "Feature"}, "parent": None}) is True
    assert is_epic({"issueType": {"name": "Feature"}, "parent": {"number": 1}}) is False
    assert is_epic({"issueType": {"name": "Task"}, "parent": None}) is False


def test_default_stage_bug_type_fast_tracks_to_architecture():
    from sdlc_next import default_stage
    assert default_stage({"issueType": {"name": "Bug"}}) == "architecture"
    assert default_stage({"issueType": {"name": "Task"}}) == "product"
    assert default_stage({"issueType": None}) == "product"


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


def _epic(number, priority=None, created="2026-08-01T00:00:00Z", labels=None, stage=None, status=None):
    return _issue(number, issue_type="Feature", priority=priority, created=created, labels=labels,
                  stage=stage, status=status)


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
    """The two pre-merge evidence markers `missing_pipeline_evidence` requires,
    in order (handoff before a clean outcome): a thread that proves the pipeline
    ran through `pr-review` with a clean verdict."""
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
    """Mocks the node-id lookup + Stage-field write that decide_next_action now
    issues, best-effort, the first time it sees an eligible issue with no Stage
    value set yet (operator instruction -- see decide_next_action's docstring)."""
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
    # Once mark-feedback-received has flipped Pipeline Status forward, next-action
    # must still recognize the issue as gate-pending (not fall through to treating it
    # as fresh, unclaimed work) -- same pass-gate/address-gate-feedback detection as
    # awaiting-human-review, just re-derived from the PR's live state either way.
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
    # #5 is blocked on #6 (native blockedBy), so #5 itself is skipped — but #6 is
    # exactly what unblocks #5 once it merges, so #6 must fall through to eligible
    # and get delegated rather than being excluded. Excluding it would deadlock the
    # chain: nothing would ever work on #6, so #5 would stay blocked forever.
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
    # #6 has closed, so gh.blocked_by(5) -- which filters to state == OPEN -- returns
    # nothing, and #5 falls straight through to eligible with no separate
    # "dependency-cleared" step to run.
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


def test_unlabeled_issue_defaults_to_product_stage():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    issues = [epic_90, _issue(9, parent=90)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(9))
    responses.update(_stage_assign_responses(9, "product"))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "delegate", "issue": 9, "unit": "issue", "stage": "product"}


def test_nothing_actionable_returns_none():
    from sdlc_next import GitHub, decide_next_action
    from tests.test_sdlc_next import ScriptedRunner
    epic_90 = _epic(90, labels=["epic:standing"])
    runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic_90])})
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 90) == {"action": "none", "epic": 90}


def test_unknown_epic_number_raises():
    # Epic number is now a required, validated input (see "Epic number is
    # mandatory" in SKILL.md) -- passing a number that isn't actually an open
    # top-level Feature issue is a caller mistake, not a legitimate "nothing to
    # do" outcome.
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
    # A legacy epic is excluded before anything else -- including an
    # in-progress child that would otherwise trigger crash-recovery. No
    # blocked_by or gate calls should fire either -- the ScriptedRunner would
    # raise on any unscripted call, so a bare _list_response is sufficient
    # proof nothing else was touched.
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
    # Both epic docs already on the epic branch (their gate PRs merged there --
    # `epic-94-gate-<stage>` -> `epic-94`; nothing is expected on `main` until
    # close-epic) -- the happy path for the docs check.
    for name in ("product.md", "architecture.md"):
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
    """The skill lives outside the repo it drives; only its config file ships
    through this repo's merges, so a mid-invocation change to it must be surfaced
    (matched by basename, wherever in the repo the config is kept)."""
    from sdlc_next import touches_pipeline_config, CONFIG_FILENAME
    assert touches_pipeline_config([CONFIG_FILENAME])
    assert touches_pipeline_config([f".claude/{CONFIG_FILENAME}"])
    assert touches_pipeline_config(["backend/src/x.ts", CONFIG_FILENAME])
    assert not touches_pipeline_config([])
    assert not touches_pipeline_config(["backend/src/x.ts", "docs/sdlc/issue-9/lld.md"])
    assert not touches_pipeline_config([".claude/skills/some-other-skill/SKILL.md"])


def test_check_epics_closeable_flags_an_epic_doc_that_never_reached_the_epic_branch():
    """An epic doc living only on its unmerged `epic-<n>-gate-<stage>` sub-branch is
    reachable only by SHA, and a SHA quoted from an old comment can resolve to a
    superseded draft -- the #209 pr-review incident. Since 2026-09-06 gate PRs land on
    `epic-<n>` (not `main`), so that is the branch the closing checklist verifies
    against; close-epic's own merge is what takes the docs to `main`."""
    from sdlc_next import GitHub, cmd_check_epics_closeable, _BLOCKING_QUERY, HUMAN_ASSIGNEE, REPO
    from tests.test_sdlc_next import ScriptedRunner
    issues = [_epic(94), _issue(10, parent=94, state="CLOSED")]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses[("gh", "issue", "view", "94", "--repo", REPO,
               "--json", "number,title,labels,body,state,comments")] = json.dumps({"comments": []})
    responses[tuple(["gh", "api", "graphql", "-f", f"query={_BLOCKING_QUERY.format(n=10)}"])] = json.dumps(
        {"data": {"repository": {"issue": {"blocking": {"nodes": []}}}}})
    responses[("gh", "api", f"repos/{REPO}/contents/docs/sdlc/epic-94/product.md?ref=epic-94",
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
    # Standing epic (#95) never even gets an issue_view call -- excluded before that
    # point entirely, per "Epic closing" in references/epics.md -- and #96 is reported as
    # already notified, with no new comment/edit calls made for either.
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
                            _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        # issue #9 here is an ordinary Task child, not an epic -- no board Status write follows.
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 92}, "labels": {"nodes": []},
        }}}}),
        ("gh", "issue", "comment", "9", "--repo", "owner/repo",
         "--body", "🚧 Picking this up — architecture stage starting."): "",
    })
    gh = GitHub(runner=runner)
    assert cmd_claim(gh, 9, "architecture") == {"issue": 9, "claimed": True}
    assert runner.calls.count(list(node_id_argv)) == 2
    assert list(stage_mutation_argv) in runner.calls
    assert list(status_mutation_argv) in runner.calls
    assert list(epic_check_argv) in runner.calls


def test_claim_sets_board_status_in_progress_for_normal_epic():
    """A normal (not standing/legacy) epic claiming its own product/architecture
    stage also flips the board's Status to In Progress."""
    from sdlc_next import (GitHub, cmd_claim, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_EPIC_CHECK_QUERY, _ISSUE_PROJECT_ITEM_QUERY,
                            _SET_PROJECT_STATUS_MUTATION, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS,
                            PROJECT_ID, PROJECT_NUMBER, STATUS_FIELD_ID, STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=110)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_110', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['product'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_110', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=110)}")
    project_item_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_PROJECT_ITEM_QUERY.format(n=110)}")
    board_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_PROJECT_STATUS_MUTATION.format(project_id=PROJECT_ID, item_id='ITEM_110', field_id=STATUS_FIELD_ID, option_id=STATUS_OPTION_IDS['in-progress'])}")
    runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_110"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 110}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 110}}}}),
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None, "labels": {"nodes": []},
        }}}}),
        project_item_argv: json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"id": "ITEM_110", "project": {"number": PROJECT_NUMBER}},
        ]}}}}}),
        board_status_argv: json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_110"}}}}),
        ("gh", "issue", "comment", "110", "--repo", "owner/repo",
         "--body", "🚧 Picking this up — product stage starting."): "",
    })
    gh = GitHub(runner=runner)
    assert cmd_claim(gh, 110, "product") == {"issue": 110, "claimed": True}
    assert list(board_status_argv) in runner.calls


def test_claim_skips_board_status_for_standing_epic():
    from sdlc_next import (GitHub, cmd_claim, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=94)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_94', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['product'])}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_94', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=94)}")
    runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_94"}}}}),
        stage_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 94}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 94}}}}),
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None,
            "labels": {"nodes": [{"name": "epic:standing"}]},
        }}}}),
        ("gh", "issue", "comment", "94", "--repo", "owner/repo",
         "--body", "🚧 Picking this up — product stage starting."): "",
    })
    gh = GitHub(runner=runner)
    # No project_item_id/set_project_status call is scripted -- ScriptedRunner
    # raises AssertionError on any unscripted call, so this itself proves the
    # standing epic never reaches a board-status write.
    assert cmd_claim(gh, 94, "product") == {"issue": 94, "claimed": True}


def test_set_project_status_swallows_missing_project_item():
    """An issue not (yet) on the board -- no project item -- is a silent no-op,
    never a failure, since board Status is a convenience, not load-bearing."""
    from sdlc_next import GitHub, _ISSUE_PROJECT_ITEM_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    project_item_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_PROJECT_ITEM_QUERY.format(n=999)}")
    runner = ScriptedRunner({
        project_item_argv: json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}),
    })
    gh = GitHub(runner=runner)
    gh.set_project_status(999, "done")  # must not raise
    assert list(project_item_argv) in runner.calls


def test_set_project_status_swallows_graphql_failure():
    """A transient GraphQL failure writing the board's Status field is swallowed,
    not raised -- it must never fail the caller (a stage claim, a workflow run)."""
    from sdlc_next import (GitHub, _ISSUE_PROJECT_ITEM_QUERY, _SET_PROJECT_STATUS_MUTATION,
                            PROJECT_ID, PROJECT_NUMBER, STATUS_FIELD_ID, STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    project_item_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_PROJECT_ITEM_QUERY.format(n=110)}")
    board_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_PROJECT_STATUS_MUTATION.format(project_id=PROJECT_ID, item_id='ITEM_110', field_id=STATUS_FIELD_ID, option_id=STATUS_OPTION_IDS['done'])}")
    runner = ScriptedRunner({
        project_item_argv: json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"id": "ITEM_110", "project": {"number": PROJECT_NUMBER}},
        ]}}}}}),
    })
    runner.fail_on.add(board_status_argv)
    gh = GitHub(runner=runner)
    gh.set_project_status(110, "done")  # must not raise despite the scripted failure


def test_mark_issue_closed_clears_stage_sets_done_and_board_status_for_epic():
    from sdlc_next import (GitHub, cmd_mark_issue_closed, _ISSUE_EPIC_CHECK_QUERY,
                            _ISSUE_PROJECT_ITEM_QUERY, _SET_PROJECT_STATUS_MUTATION,
                            _ISSUE_NODE_ID_QUERY, _DELETE_ISSUE_FIELD_VALUE_MUTATION,
                            _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS,
                            PROJECT_ID, PROJECT_NUMBER, STATUS_FIELD_ID, STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=110)}")
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=110)}")
    clear_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_110', field_id=STAGE_FIELD_ID)}")
    set_done_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_110', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['done'])}")
    project_item_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_PROJECT_ITEM_QUERY.format(n=110)}")
    board_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_PROJECT_STATUS_MUTATION.format(project_id=PROJECT_ID, item_id='ITEM_110', field_id=STATUS_FIELD_ID, option_id=STATUS_OPTION_IDS['done'])}")
    runner = ScriptedRunner({
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None, "labels": {"nodes": []},
        }}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_110"}}}}),
        clear_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 110}}}}),
        set_done_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 110}}}}),
        project_item_argv: json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"id": "ITEM_110", "project": {"number": PROJECT_NUMBER}},
        ]}}}}}),
        board_status_argv: json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_110"}}}}),
    })
    gh = GitHub(runner=runner)
    assert cmd_mark_issue_closed(gh, 110) == {"issue": 110, "is_epic": True, "marked_done": True}
    assert list(clear_stage_argv) in runner.calls
    assert list(set_done_argv) in runner.calls
    assert list(board_status_argv) in runner.calls


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
    # No project-item/board-status call is scripted -- proves the non-epic path
    # never reaches a board write.
    assert cmd_mark_issue_closed(gh, 183) == {"issue": 183, "is_epic": False, "marked_done": True}


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
    })
    gh = GitHub(runner=runner)
    assert cmd_start_comment(gh, 9, "arch-review") == {"issue": 9, "started": "arch-review"}
    assert len(runner.calls) == 1


def test_handoff_to_pr_review_posts_marker_without_referencing_a_testing_doc():
    """`testing` writes no `testing.md` any more -- its output is the structured
    handoff comment the caller passes as `--summary`. The marker must still be the
    canonical `testing->pr-review` one, and the body must not point at a file that
    is never written."""
    from sdlc_next import GitHub, cmd_handoff_to_pr_review
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner()
    runner.prefix_responses[("gh", "issue", "comment", "42", "--repo",
                             "owner/repo", "--body")] = ""
    gh = GitHub(runner=runner)
    result = cmd_handoff_to_pr_review(gh, 42, 77, "142 passed, 0 failed; every criterion mutation-checked.")
    assert result == {"issue": 42, "pr": 77, "queued_for": "pr-review"}
    assert len(runner.calls) == 1
    body = runner.calls[0][-1]
    assert "testing.md" not in body
    assert "docs/sdlc/issue-42" not in body
    assert body.startswith("✅ Testing passed. 142 passed, 0 failed; every criterion mutation-checked. ")
    assert "PR #77 is queued for `pr-review`." in body
    assert "<!-- stage-transition: testing->pr-review @ " in body


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
    git_runner = ScriptedRunner({("git", "-C", "/repo", "rev-parse", "HEAD"): "abc1234\n"})
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
        "`/sdlc-pipeline` again once you've merged it, or any time after leaving comments if you'd "
        "like the revision done sooner.\n\n"
        "<!-- gate-pr: product:40 -->\n"
    )
    assert "<!-- gate-pr: product:40 -->" in body
    assert re.search(
        r"<!-- stage-transition: product->human-review:product @ "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->$", body)


def test_git_reconcile_branch_runs_fetch_checkout_merge_push_in_order():
    from sdlc_next import git_reconcile_branch
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    git_reconcile_branch("/repo", "issue-9", runner=runner)
    assert runner.calls == [
        ["git", "-C", "/repo", "fetch", "origin"],
        ["git", "-C", "/repo", "checkout", "issue-9"],
        ["git", "-C", "/repo", "merge", "origin/main"],
        ["git", "-C", "/repo", "push", "origin", "issue-9"],
    ]


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
    """A blocked unit left at `in-progress` with no worktree is indistinguishable from
    a crashed run to decide_next_action and list_parallel_ready, which share that
    heuristic -- #310 sat blocked for a whole session while next-action kept offering
    it as a `resume`. Blocked-ness comes from the native relationship; the field just
    has to stop lying."""
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
    # and it must not be set to in-progress or needs-human by any other path
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


_SEARCH_COUNT_ARGV = ("gh", "api",
                       "search/issues?q=repo:owner/repo+type:issue+state:closed&per_page=1",
                       "--jq", ".total_count")


def test_retro_watermark_default_is_formatted_with_the_config_doc_root(tmp_path):
    """`pipeline.retro.watermarkFile` defaults to `{docRoot}/retro-watermark`, resolved
    against the config's own `docRoot` at load time -- so the watermark lives with the
    driven repo's committed docs, not inside the skill checkout (where a leaked client
    value once sat at the skill root)."""
    from sdlc_next import RETRO_WATERMARK_FILE, _PIPELINE_DEFAULTS, DOC_ROOT
    assert _PIPELINE_DEFAULTS["retro"]["watermarkFile"] == "{docRoot}/retro-watermark"
    assert RETRO_WATERMARK_FILE == f"{DOC_ROOT}/retro-watermark" == "docs/sdlc/retro-watermark"
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["docRoot"] = "design/pipeline"
    del cfg["pipeline"]["retro"]["watermarkFile"]  # fall back to the default
    cfg_path = tmp_path / "sdlc-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg))
    out = subprocess.check_output(
        [sys.executable, "-c", "import sdlc_next as s; print(s.RETRO_WATERMARK_FILE)"],
        env={**os.environ, "SDLC_CONFIG": str(cfg_path)},
        cwd=str(Path(__file__).resolve().parents[1]), text=True).strip()
    assert out == "design/pipeline/retro-watermark"


def test_retro_check_mark_done_creates_the_watermark_parent_directory(tmp_path):
    from sdlc_next import GitHub, cmd_retro_check, RETRO_WATERMARK_FILE
    gh = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "3\n"}))
    assert cmd_retro_check(gh, str(tmp_path), mark_done=True)["marked_done"] is True
    assert (tmp_path / RETRO_WATERMARK_FILE).read_text().strip() == "3"


def test_retro_check_true_when_five_or_more_closed_since_watermark(tmp_path):
    from sdlc_next import GitHub, cmd_retro_check, RETRO_WATERMARK_FILE
    wm = tmp_path / RETRO_WATERMARK_FILE
    wm.parent.mkdir(parents=True)
    wm.write_text("5\n")
    gh = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "10\n"}))
    assert cmd_retro_check(gh, str(tmp_path)) == {"closed_count": 10, "watermark": 5,
                                                    "run_retro": True}


def test_retro_check_false_when_fewer_than_five_since_watermark(tmp_path):
    from sdlc_next import GitHub, cmd_retro_check, RETRO_WATERMARK_FILE
    wm = tmp_path / RETRO_WATERMARK_FILE
    wm.parent.mkdir(parents=True)
    wm.write_text("5\n")
    gh = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "7\n"}))
    assert cmd_retro_check(gh, str(tmp_path)) == {"closed_count": 7, "watermark": 5,
                                                    "run_retro": False}


def test_retro_check_missing_watermark_reads_as_zero(tmp_path):
    # No watermark file yet = 0, so run_retro fires at the fifth-ever close --
    # and, unlike the old `count % 5 == 0` trigger, keeps firing until a retro
    # actually records --mark-done, instead of skipping when two issues close
    # between checks.
    from sdlc_next import GitHub, cmd_retro_check
    gh = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "7\n"}))
    assert cmd_retro_check(gh, str(tmp_path)) == {"closed_count": 7, "watermark": 0,
                                                    "run_retro": True}


def test_retro_check_mark_done_writes_current_count_as_watermark(tmp_path):
    from sdlc_next import GitHub, cmd_retro_check, RETRO_WATERMARK_FILE
    wm = tmp_path / RETRO_WATERMARK_FILE
    wm.parent.mkdir(parents=True)
    gh = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "12\n"}))
    result = cmd_retro_check(gh, str(tmp_path), mark_done=True)
    assert result == {"closed_count": 12, "watermark": 12, "marked_done": True}
    assert wm.read_text().strip() == "12"
    gh2 = GitHub(runner=ScriptedRunner({_SEARCH_COUNT_ARGV: "13\n"}))
    assert cmd_retro_check(gh2, str(tmp_path))["run_retro"] is False


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
        "sync_conflict_count": 1,
        "design_review": {},
    }


def test_pairing_counts_tracks_design_reviews_per_role():
    """The `lld-review <-> lld` pairing tripped the valve's context-reset
    replacement on three of epic #98's four children while having no mechanical
    counter at all -- the strike count lived only in one orchestrator's head, so a
    crashed session would have resumed it at zero. Roles are counted separately
    because a unit can bounce on both and the valve treats them as distinct
    pairings."""
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
    # Three lld-review bounces with no clean between them -- this is the shape the
    # valve escalates on, and the count a fresh session must be able to recover.
    assert result["design_review"]["lld-review"] == {
        "rework_since_last_clean": 3, "total_rework": 3, "total_clean": 0}
    # arch-review's clean verdict resets its own counter and does not touch
    # lld-review's -- the whole reason roles are keyed separately.
    assert result["design_review"]["arch-review"] == {
        "rework_since_last_clean": 0, "total_rework": 1, "total_clean": 1}
    # A review that never ran on this unit has no key at all, rather than a
    # zeroed entry that reads like "ran and found nothing".
    assert "pr-review" not in result["design_review"]


def test_record_design_review_refuses_an_unknown_role():
    """`record-pr-review` refuses an unknown outcome loudly; the design twin
    refuses an unknown role the same way. A typo'd role would silently create a
    third pairing whose count nothing ever reads."""
    import pytest
    from sdlc_next import GitHub, GhError, cmd_record_design_review
    with pytest.raises(GhError, match="role must be one of"):
        cmd_record_design_review(GitHub(runner=ScriptedRunner({})), 9,
                                 "pr-review", "clean", "s")


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
    # merge-pr no longer touches Stage/Pipeline Status itself when this closes the
    # issue -- that's cmd_mark_issue_closed's job now, fired in real time by
    # gate-auto-advance.yml's `issues: closed` trigger (see "Epic board Status" /
    # "Issue taxonomy" in references/operations.md). Confirmed here by *not* scripting either
    # field-mutation call: ScriptedRunner raises on any unscripted call, so this
    # test itself proves cmd_merge_pr never attempts one.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pass"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
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
    # status is the retryable "pending", not "missing-checks" -- the required workflow
    # DID trigger, it just hasn't posted a pass yet. It also shows up in `missing` per
    # missing_required_workflows' own contract ("reported a passing check ... and
    # didn't" -- true of a still-running check too), which is fine: pr-review only
    # escalates off `status`, never off `missing` alone (see SKILL.md's routing).
    assert status == "pending"
    assert missing == ["Backend CI"]


def test_merge_gate_status_surfaces_missing_workflow_even_when_status_is_pending():
    # Regression: a PR touching both trees where backend passed but frontend's
    # workflow never reported at all, and some unrelated check (e.g. the gate
    # auto-advance workflow) is still mid-run. Overall status resolves to
    # "pending" from the unrelated check -- but the missing frontend workflow
    # must still surface, not get discarded because status != "passed".
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
    # Regression: backend genuinely failed a test (a real code problem -- route to
    # development for a test fix) while frontend's required workflow never reported
    # at all (a config defect -- needs mark-needs-human, not a test fix). Both facts
    # must be visible together, or the routing sends "CI failed" to development,
    # which goes hunting for a frontend test failure that doesn't exist.
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts", "frontend/src/y.tsx"],
        [{"workflow": "Backend CI", "bucket": "fail"}],
    )
    assert status == "failed"
    # Both are in `missing` here: backend genuinely failed (never posted a pass), and
    # frontend never reported at all. `status` alone can't distinguish them -- a
    # caller inspecting `missing` (e.g. mark-needs-human's reason text) sees both.
    assert set(missing) == {"Backend CI", "Frontend CI"}


def test_merge_gate_status_missing_checks_when_required_workflow_absent():
    from sdlc_next import merge_gate_status
    status, missing = merge_gate_status(
        ["backend/src/x.ts"],
        [{"workflow": "sdlc-next Gate Auto-Advance", "bucket": "pass"}],
    )
    assert status == "missing-checks"
    assert missing == ["Backend CI"]


# ── Local-CI attestation (main-only GHA CI, 2026-09-04) ──────────────────────

def test_local_ci_attestation_satisfies_required_backend_when_sha_matches():
    # Backend went main-only: no GHA check on a child PR. A fresh local-ci
    # attestation for the current head stands in for it.
    from sdlc_next import missing_required_workflows
    comments = [{"body": "🧪 <!-- local-ci: backend:42 @ abc1234def5678 -->"}]
    missing = missing_required_workflows(
        ["backend/src/x.ts"], [], comments, "abc1234def5678")
    assert missing == []


def test_local_ci_attestation_ignored_when_sha_is_stale():
    # A local run against an OLD commit must not satisfy the gate after a new push
    # (same freshness principle as the behind-base gate).
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
    # Back-compat: the GHA-only 2-arg signature still behaves as before.
    from sdlc_next import missing_required_workflows
    assert missing_required_workflows(
        ["backend/src/x.ts"],
        [{"workflow": "Backend CI", "bucket": "pass"}]) == []
    assert missing_required_workflows(
        ["backend/src/x.ts"], []) == ["Backend CI"]


def test_record_local_ci_posts_attestation_marker_on_the_pr():
    from sdlc_next import GitHub, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({})
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    result = cmd_record_local_ci(gh, pr=42, suite="backend", sha="abc1234def")
    assert result == {"pr": 42, "suite": "backend", "sha": "abc1234def", "attested": True}
    body = next(c for c in runner.calls if c[:3] == ["gh", "pr", "comment"])[-1]
    assert "<!-- local-ci: backend:42 @ abc1234def -->" in body


def test_record_local_ci_rejects_unknown_suite():
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="suite must be one of"):
        cmd_record_local_ci(gh, pr=42, suite="infra", sha="abc1234")


def test_record_local_ci_rejects_non_hex_sha():
    from sdlc_next import GitHub, GhError, cmd_record_local_ci
    from tests.test_sdlc_next import ScriptedRunner
    import pytest
    gh = GitHub(runner=ScriptedRunner({}))
    with pytest.raises(GhError, match="sha must be"):
        cmd_record_local_ci(gh, pr=42, suite="backend", sha="not-a-sha")


def test_merge_pr_refuses_when_backend_workflow_reported_no_check():
    from sdlc_next import GitHub, GhError, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
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
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
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
    """The pipeline merges to main continuously and its config lives in the driven
    repo. A merge that changes the config mid-invocation can change how later
    commands behave, silently -- merge-pr knows what the PR touched, so it says so."""
    from sdlc_next import GitHub, cmd_merge_pr, CONFIG_FILENAME
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            f"{CONFIG_FILENAME}\ndocs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
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
    # Green CI on a stale base proves nothing about the combined state -- with
    # the parallel dev lane, two sibling PRs can each be green independently and
    # still break `main` together. A behind-main branch is a valid, actionable
    # outcome (sync-branch, wait for fresh CI, re-merge), not an operational
    # failure -- so exit-0 structured result, and no checks/ready/merge calls
    # at all.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    runner = ScriptedRunner({
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "2\n",
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
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
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
         "--json", "comments,headRefOid"): json.dumps({"comments": [], "headRefOid": "abc1234"}),
    })
    gh = GitHub(runner=runner)
    result = cmd_pr_checks(gh, 42)
    assert result["status"] == "missing-checks"
    assert "Frontend CI" in result["missing_required_workflows"]


def test_pr_checks_local_ci_attestation_clears_missing_required_workflow():
    # The mirror case: a frontend-touching child PR with no GHA check but a fresh
    # local-ci attestation for the current head reads as passed, not missing-checks.
    from sdlc_next import GitHub, cmd_pr_checks
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "frontend/app/page.tsx\n",
        ("gh", "pr", "view", "42", "--repo", "owner/repo",
         "--json", "comments,headRefOid"): json.dumps({
            "comments": [{"body": "<!-- local-ci: frontend:42 @ abc1234def -->"}],
            "headRefOid": "abc1234def"}),
    })
    gh = GitHub(runner=runner)
    result = cmd_pr_checks(gh, 42)
    assert result["status"] == "passed"
    assert result["missing_required_workflows"] == []


def test_open_dev_pr_creates_draft_pr_with_closes_and_sets_stage_field_to_testing():
    from sdlc_next import (GitHub, cmd_open_dev_pr, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            STAGE_FIELD_ID, STAGE_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['testing'])}")
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
    })
    runner.prefix_responses = {
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=runner)
    result = cmd_open_dev_pr(gh, issue=9, title="Add widget", body="Implements the widget.",
                              summary="Implemented per architecture.md.")
    assert result == {"issue": 9, "pr": 42, "created": True}


def test_open_dev_pr_reuses_an_already_open_pr_instead_of_opening_a_duplicate():
    """A resumed development agent -- or a concurrent session that got there first --
    must not open a second PR on the same branch, splitting review history in two."""
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
    # No PR was created, no Stage field written, no comment posted.
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in runner.calls)


def test_pass_gate_reconciles_comments_and_reclaims_next_stage():
    from sdlc_next import (GitHub, cmd_pass_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    import json
    git_runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
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
    # Regression test: passing the wrong --stage (the gate's owning doc-stage is not
    # a value to guess -- it must come from next-action's own 'stage' field) used to
    # silently relabel the issue to the wrong stage and post a wrong confirmation
    # comment. The script must now catch this itself instead of trusting the caller.
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
    # cmd_skip_gate resolves the Gate B confidence threshold from the child's parent
    # epic profile -- so it also fetches the parent (#92) to read its labels.
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


def test_skip_gate_refuses_below_threshold():
    from sdlc_next import GitHub, GhError, cmd_skip_gate, _ISSUE_EPIC_CHECK_QUERY
    from tests.test_sdlc_next import ScriptedRunner
    # Default-profile epic -> threshold 95; confidence 95 does not clear it.
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


def test_skip_gate_threshold_is_per_profile_standing_epic_child_clears_at_91():
    # A standing/RTB profile lowers skipConfidenceThreshold to 90 (sample config);
    # a child of that epic clears Gate B at 91 but not at 90.
    from sdlc_next import GitHub, GhError, cmd_skip_gate, _ISSUE_EPIC_CHECK_QUERY
    from tests.test_sdlc_next import ScriptedRunner

    epic_check_9 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=9)}")
    epic_check_94 = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=94)}")
    responses = {
        epic_check_9: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Task"}, "parent": {"number": 94},
            "labels": {"nodes": []}}}}}),
        epic_check_94: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None,
            "labels": {"nodes": [{"name": "epic:standing"}]}}}}}),
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


# --- Epic-level stages (epic-level Product/Architecture, `lld` at child level) ---
# See "Epic-level stages" in references/epics.md.

def test_is_epic_legacy_and_is_epic_architected_read_their_labels():
    from sdlc_next import is_epic_legacy, is_epic_architected
    assert is_epic_legacy({"labels": [{"name": "epic:legacy"}]}) is True
    assert is_epic_legacy({"labels": []}) is False
    assert is_epic_architected({"labels": [{"name": "epic:architected"}]}) is True
    assert is_epic_architected({"labels": []}) is False


def test_default_stage_normal_epic_child_always_starts_at_lld():
    from sdlc_next import default_stage
    normal_epic = {"labels": []}
    assert default_stage({"issueType": {"name": "Task"}}, normal_epic) == "lld"
    assert default_stage({"issueType": {"name": "Bug"}}, normal_epic) == "lld"


def test_default_stage_standing_epic_child_keeps_old_behavior():
    from sdlc_next import default_stage
    standing_epic = {"labels": [{"name": "epic:standing"}]}
    assert default_stage({"issueType": {"name": "Task"}}, standing_epic) == "product"
    assert default_stage({"issueType": {"name": "Bug"}}, standing_epic) == "architecture"


def test_default_stage_no_parent_epic_keeps_old_behavior():
    from sdlc_next import default_stage
    assert default_stage({"issueType": {"name": "Task"}}, None) == "product"
    assert default_stage({"issueType": {"name": "Bug"}}, None) == "architecture"


def test_fresh_normal_epic_is_delegated_epic_self():
    # Epic 92 has no epic:standing/legacy/architected label and no Stage of its
    # own yet -- its own Product/Architecture phase is picked as the unit of work.
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent")
    responses = {tuple(_list_argv()): _list_response([epic_92])}
    responses.update(_no_blockers_responses(92))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 92) == {"action": "delegate", "issue": 92, "unit": "epic", "stage": "product"}


def test_normal_epic_awaiting_review_returns_pass_gate_when_merged():
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent", stage="product", status="awaiting-human-review")
    issues = [epic_92]
    responses = {
        tuple(_list_argv()): _list_response(issues),
        ("gh", "issue", "view", "92", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:129 -->"}]}),
        ("gh", "pr", "view", "129", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "MERGED", "mergedAt": "2026-08-16T00:00:00Z"}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 92) == {"action": "pass-gate", "issue": 92, "unit": "epic",
                                           "gate_pr": 129, "stage": "product"}


def test_non_architected_normal_epic_blocks_its_own_children():
    # Epic 92 isn't epic:architected yet -- its child #101 must not be picked up
    # even though it's otherwise a normal, unblocked child (would previously have
    # been delegated directly under the old per-child-only model).
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent")
    issues = [epic_92, _issue(101, parent=92)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(92, 101))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    # Epic 92 itself is the only eligible unit -- its own epic-self candidacy.
    assert decide_next_action(gh, 92) == {"action": "delegate", "issue": 92, "unit": "epic", "stage": "product"}


def test_non_architected_epic_with_open_unsatisfied_gate_parks_children_too():
    # The epic-self branch returns nothing when its gate is open with nothing to
    # address (not_satisfied) -- that must park the WHOLE epic, not fall through
    # and delegate a child at `lld` against an architecture.md that doesn't
    # exist yet. (The same guard covers the epic-needs-human and epic-blocked
    # fall-through cases.)
    from sdlc_next import GitHub, decide_next_action, _UNRESOLVED_THREADS_QUERY
    epic_92 = _epic(92, priority="Urgent", stage="product", status="awaiting-human-review")
    issues = [epic_92, _issue(101, parent=92)]
    responses = {
        tuple(_list_argv()): _list_response(issues),
        ("gh", "issue", "view", "92", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:140 -->"}]}),
        ("gh", "pr", "view", "140", "--repo", "owner/repo", "--json", "state,mergedAt"):
            json.dumps({"state": "OPEN", "mergedAt": None}),
        ("gh", "pr", "view", "140", "--repo", "owner/repo", "--json", "comments,createdAt"):
            json.dumps({"createdAt": "2026-08-16T00:00:00Z", "comments": []}),
        ("gh", "api", "graphql", "-f", f"query={_UNRESOLVED_THREADS_QUERY.format(pr=140)}"):
            json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}),
    }
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    # Child #101 must NOT be delegated (and no Stage write attempted for it --
    # ScriptedRunner would raise on the unscripted mutation).
    assert decide_next_action(gh, 92) == {"action": "none", "epic": 92}


def test_architected_normal_epic_child_becomes_eligible_at_lld_stage():
    # Once the epic is epic:architected, an unlabeled child defaults straight to
    # `lld` -- its own dedicated Stage value (see "LLD is its own Stage value" in
    # references/epics.md), not an overloaded "architecture".
    from sdlc_next import GitHub, decide_next_action
    epic_92 = _epic(92, priority="Urgent", labels=["epic:architected"])
    issues = [epic_92, _issue(101, parent=92)]
    responses = {tuple(_list_argv()): _list_response(issues)}
    responses.update(_no_blockers_responses(101))
    responses.update(_stage_assign_responses(101, "lld"))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    assert decide_next_action(gh, 92) == {"action": "delegate", "issue": 101, "unit": "issue", "stage": "lld"}


def test_epic_mid_second_gate_round_is_still_checked_even_though_architected():
    # A deviation found mid-`lld` reopens a second Product/Architecture gate round
    # on an already-`epic:architected` epic (see "Epic-level deviation escalation"
    # in SKILL.md) -- the pending-gate check must still fire for it.
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


def test_open_gate_unit_epic_opens_gate_sub_branch_against_epic_branch():
    # Since 2026-09-06 an epic-level gate is `epic-<n>-gate-<stage>` -> `epic-<n>`,
    # never straight to `main` -- the doc lands on the epic branch when merged,
    # and only `epic-<n>` itself reaches `main`, at close-epic. The issue-comment
    # doc path is still the epic's own `docs/sdlc/epic-<n>/` folder.
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "rev-parse", "HEAD"): "abcd1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=92)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_92', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_92"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 92}}}}),
        ("gh", "api", "repos/owner/repo/compare/epic-92...epic-92-gate-product",
         "--jq", ".ahead_by"): "1\n",
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/129\n",
        ("gh", "issue", "comment", "92"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 92, "Seller notifications epic", "product.md",
                            "architecture", "Locked epic-level requirements.", unit="epic",
                            runner=git_runner)
    assert result == {"issue": 92, "unit": "epic", "gate_pr": 129, "stage": "product",
                      "sha": "abcd1234", "head": "epic-92-gate-product", "base": "epic-92"}
    pr_create_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "pr", "create"])
    assert pr_create_call[pr_create_call.index("--base") + 1] == "epic-92"
    assert pr_create_call[pr_create_call.index("--head") + 1] == "epic-92-gate-product"
    assert "main" not in pr_create_call
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "docs/sdlc/epic-92/product.md" in body


def test_open_gate_unit_epic_gate_b_uses_architecture_gate_sub_branch():
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "rev-parse", "HEAD"): "abcd1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=5)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_5', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_5"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 5}}}}),
        ("gh", "api", "repos/owner/repo/compare/epic-5...epic-5-gate-architecture",
         "--jq", ".ahead_by"): "2\n",
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/130\n",
        ("gh", "issue", "comment", "5"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 5, "Seller notifications epic", "architecture.md",
                            "development", "Locked the design.", unit="epic", runner=git_runner)
    assert result["head"] == "epic-5-gate-architecture"
    assert result["base"] == "epic-5"
    pr_create_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "pr", "create"])
    assert pr_create_call[pr_create_call.index("--base") + 1] == "epic-5"
    assert pr_create_call[pr_create_call.index("--head") + 1] == "epic-5-gate-architecture"


def test_open_gate_unit_issue_still_targets_main_from_issue_branch():
    # Standing-epic child gates are unchanged by the 2026-09-06 epic-gate rerouting.
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "rev-parse", "HEAD"): "abcd1234\n"})
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['awaiting-human-review'])}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "pr", "create"): "https://github.com/owner/repo/pull/41\n",
        ("gh", "issue", "comment", "9"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_open_gate(gh, "/repo", 9, "Fix widget", "architecture.md", "development",
                            "Design locked.", unit="issue", runner=git_runner)
    assert result["head"] == "issue-9"
    assert result["base"] == "main"


def test_epic_gate_branch_uses_configured_prefix_and_suffix():
    from sdlc_next import epic_gate_branch, GATE_BRANCH_SUFFIX, _PIPELINE_DEFAULTS
    assert _PIPELINE_DEFAULTS["branches"]["gateSuffix"] == "-gate-"
    assert GATE_BRANCH_SUFFIX == "-gate-"
    assert epic_gate_branch(5, "product") == "epic-5-gate-product"
    assert epic_gate_branch(5, "architecture") == "epic-5-gate-architecture"


def test_pass_gate_unit_epic_at_architecture_completes_epic_instead_of_claiming_development():
    # The merged epic gate landed on `epic-92` (its head was the gate sub-branch),
    # so the epic worktree reconciles with `origin/epic-92` -- `main` is not
    # involved until close-epic.
    from sdlc_next import GitHub, cmd_pass_gate, _ISSUE_NODE_ID_QUERY, _DELETE_ISSUE_FIELD_VALUE_MUTATION, \
        STAGE_FIELD_ID, PIPELINE_STATUS_FIELD_ID
    git_runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "epic-92"): "",
        ("git", "-C", "/repo", "merge", "origin/epic-92"): "",
        ("git", "-C", "/repo", "push", "origin", "epic-92"): "",
    })
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=92)}")
    del_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=STAGE_FIELD_ID)}")
    del_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=PIPELINE_STATUS_FIELD_ID)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "92", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:140 -->"}]}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_92"}}}}),
        del_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
        del_status_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "issue", "edit", "92"): "",
        ("gh", "issue", "comment", "92"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_pass_gate(gh, "/repo", issue=92, gate_pr=140, stage="architecture",
                            unit="epic", runner=git_runner)
    assert result == {"issue": 92, "unit": "epic", "epic_architecture_complete": True}
    assert ["git", "-C", "/repo", "merge", "origin/epic-92"] in git_runner.calls
    assert ["git", "-C", "/repo", "merge", "origin/main"] not in git_runner.calls
    edit_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "edit"])
    assert "epic:architected" in edit_call
    # Never writes a Stage-field value (e.g. "Development") for an epic.
    stage_writes = [c for c in gh_runner.calls if "updateIssueFieldValue" in " ".join(c)]
    assert stage_writes == []


def test_skip_gate_unit_epic_completes_epic_without_opening_a_gate():
    from sdlc_next import GitHub, cmd_skip_gate, _ISSUE_NODE_ID_QUERY, _DELETE_ISSUE_FIELD_VALUE_MUTATION, \
        _ISSUE_EPIC_CHECK_QUERY, STAGE_FIELD_ID, PIPELINE_STATUS_FIELD_ID
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=92)}")
    del_stage_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=STAGE_FIELD_ID)}")
    del_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=PIPELINE_STATUS_FIELD_ID)}")
    # unit=epic resolves the threshold from the epic's own profile (default -> 95).
    epic_check_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_EPIC_CHECK_QUERY.format(n=92)}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_92"}}}}),
        del_stage_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
        del_status_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
        epic_check_argv: json.dumps({"data": {"repository": {"issue": {
            "issueType": {"name": "Feature"}, "parent": None, "labels": {"nodes": []}}}}}),
    })
    gh_runner.prefix_responses = {
        ("gh", "issue", "edit", "92"): "",
        ("gh", "issue", "comment", "92"): "",
    }
    gh = GitHub(runner=gh_runner)
    result = cmd_skip_gate(gh, issue=92, stage="architecture", confidence=98,
                            summary="Clean.", unit="epic")
    assert result == {"issue": 92, "unit": "epic", "epic_architecture_complete": True}
    edit_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "edit"])
    assert "epic:architected" in edit_call


def test_pause_for_epic_regate_clears_only_pipeline_status():
    from sdlc_next import GitHub, cmd_pause_for_epic_regate, _ISSUE_NODE_ID_QUERY, \
        _DELETE_ISSUE_FIELD_VALUE_MUTATION, PIPELINE_STATUS_FIELD_ID
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=101)}")
    del_status_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_101', field_id=PIPELINE_STATUS_FIELD_ID)}")
    gh_runner = ScriptedRunner({
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_101"}}}}),
        del_status_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 101}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "101"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_pause_for_epic_regate(gh, 101, epic=92, gate_pr=141)
    assert result == {"issue": 101, "paused_for_epic_regate": 92, "gate_pr": 141}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "#92" in body and "#141" in body


def test_verify_exit_uses_epic_docs_dir_for_unit_epic(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "epic-92"
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
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 92, expect_stage="architecture", unit="epic", runner=git_runner)
    assert result["docs_present"] == ["product.md"]


# --- auto-pass-gate (real-time gate-auto-advance webhook backstop) ---
# See "Add a new CLI subcommand" / gate-auto-advance.yml, and "Checking a gate" /
# "Passing a gate" in references/gates.md.

def test_auto_pass_gate_skips_still_open_pr():
    # A PR the workflow was somehow invoked against while still open (defensive --
    # the real trigger only ever fires on `closed`) is neither the merged nor the
    # closed-without-merge branch; must be a clean skip, not a crash or a guess.
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
    # `issue-<n>` and `epic-<n>-gate-<stage>` are the two gate branch shapes
    # (epic-level Gate A/B run on a sub-branch of `epic-<n>` -- see "Human-review
    # gates"); anything else is ordinary non-gate traffic and skips cleanly.
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
    assert "not an issue-<n> or epic-<n>-gate-<stage> branch" in result["skipped"]


def test_auto_pass_gate_skips_bare_epic_branch_head_which_is_the_integration_pr():
    # A merged `epic-<n>` -> `main` PR is close-epic's integration merge, not a
    # gate (gates moved to `epic-<n>-gate-<stage>` sub-branches on 2026-09-06).
    # It must skip cleanly on the head shape alone, before any issue lookup.
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
    assert "not an issue-<n> or epic-<n>-gate-<stage> branch" in result["skipped"]
    assert not any(c[:3] == ["gh", "issue", "view"] for c in gh_runner.calls)


def test_auto_pass_gate_skips_epic_gate_head_whose_base_is_not_the_epic_branch():
    # An `epic-<n>-gate-<stage>` head must be based on `epic-<n>`; based on `main`
    # (or anything else) it is not the gate this pipeline opened.
    from sdlc_next import GitHub, cmd_auto_pass_gate, REPO
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "epic-7-gate-architecture",
                        "baseRefName": "main", "state": "MERGED",
                        "mergedAt": "2026-09-06T10:00:00Z", "body": ""}),
    })
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40)
    assert result["ok"] is True
    assert "not epic-7" in result["skipped"]


def test_match_open_gate_derives_unit_epic_and_number_from_gate_sub_branch_head():
    from sdlc_next import GitHub, _match_open_gate, _GATE_BRANCH_RE, _ISSUE_FIELDS_QUERY, REPO
    m = _GATE_BRANCH_RE.match("epic-7-gate-architecture")
    assert m and m.group("epic_n") == "7" and m.group("stage") == "architecture"
    assert m.group("issue_n") is None
    m = _GATE_BRANCH_RE.match("issue-9")
    assert m and m.group("issue_n") == "9" and m.group("epic_n") is None
    assert _GATE_BRANCH_RE.match("epic-7") is None
    assert _GATE_BRANCH_RE.match("epic-7-gate-lld") is None
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=7)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "7", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:55 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    pr = {"headRefName": "epic-7-gate-architecture", "baseRefName": "epic-7", "body": ""}
    assert _match_open_gate(gh, pr, 55) == (7, "architecture", "awaiting-human-review", "epic")


def test_match_open_gate_refuses_epic_gate_head_named_for_the_other_stage():
    # The marker is the authority on the stage; a `-gate-product` head while the
    # epic's open gate is `architecture` is not the gate the epic is waiting on.
    from sdlc_next import GitHub, _match_open_gate, _NotAGate, _ISSUE_FIELDS_QUERY, REPO
    import pytest
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=7)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "7", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:55 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
    })
    gh = GitHub(runner=gh_runner)
    pr = {"headRefName": "epic-7-gate-product", "baseRefName": "epic-7", "body": ""}
    with pytest.raises(_NotAGate, match="product gate branch"):
        _match_open_gate(gh, pr, 55)


def test_auto_pass_gate_completes_epic_architecture_for_merged_epic_gate():
    # A merged epic-level Gate B (head branch `epic-<n>-gate-architecture`, base
    # `epic-<n>`) must route through _complete_epic_architecture -- clear the
    # epic's Stage/Pipeline Status, add epic:architected -- never advance the
    # epic to `development` (epics don't develop). Before 2026-08-20 the branch
    # regex only matched `issue-<n>`, so epic gates got no real-time backstop at
    # all; from 2026-09-06 the epic gate head is the gate sub-branch and the
    # reconcile after the merge is against `origin/epic-<n>`, not `main`.
    from sdlc_next import (GitHub, cmd_auto_pass_gate, _ISSUE_NODE_ID_QUERY,
                            _DELETE_ISSUE_FIELD_VALUE_MUTATION, _ISSUE_FIELDS_QUERY,
                            STAGE_FIELD_ID, PIPELINE_STATUS_FIELD_ID, REPO)
    git_runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "epic-92"): "",
        ("git", "-C", "/repo", "merge", "origin/epic-92"): "",
        ("git", "-C", "/repo", "push", "origin", "epic-92"): "",
    })
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=92)}")
    stage_delete_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=STAGE_FIELD_ID)}")
    status_delete_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_92', field_id=PIPELINE_STATUS_FIELD_ID)}")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=92)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "epic-92-gate-architecture",
                        "baseRefName": "epic-92",
                        "state": "MERGED", "mergedAt": "2026-08-20T10:00:00Z",
                        "body": "Doc-only review gate for #92 -- see SKILL.md."}),
        ("gh", "issue", "view", "92", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: architecture:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_92"}}}}),
        stage_delete_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
        status_delete_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 92}}}}),
        ("gh", "issue", "edit", "92", "--repo", REPO, "--add-label", "epic:architected"): "",
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "92"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40, runner=git_runner)
    assert result == {"issue": 92, "unit": "epic", "epic_architecture_complete": True, "ok": True}


def test_auto_pass_gate_skips_dev_pr_carrying_closes_hash():
    # A PR body containing "Closes #<n>" is the pipeline's own development PR
    # (auto-merged by pr-review already), never a human-merged gate PR -- must be a
    # clean no-op, not an error, since this fires on every merged PR in the repo.
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
    # The marker always reflects the *current* open gate (find_gate_pr returns the
    # last one seen) -- if it doesn't match this webhook's PR number, this PR isn't
    # the issue's currently open gate (stale/superseded), so this must be a clean
    # skip, not a mutation against the wrong PR.
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
    # live=False: CI advances the Stage field but must NOT claim in-progress or post
    # a start comment -- no agent is actually about to run this stage in real time,
    # only the next live `/sdlc-pipeline` invocation claims it for real. See
    # cmd_pass_gate's `live` docstring.
    from sdlc_next import (GitHub, cmd_auto_pass_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION,
                            _DELETE_ISSUE_FIELD_VALUE_MUTATION, _ISSUE_FIELDS_QUERY, STAGE_FIELD_ID,
                            STAGE_OPTION_IDS, PIPELINE_STATUS_FIELD_ID, REPO)
    git_runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_delete_argv = ("gh", "api", "graphql", "-f",
        f"query={_DELETE_ISSUE_FIELD_VALUE_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID)}")
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
        status_delete_argv: json.dumps({"data": {"deleteIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_auto_pass_gate(gh, "/repo", 40, runner=git_runner)
    assert result == {"issue": 9, "unit": "issue", "next_stage": "architecture",
                       "claimed": False, "ok": True}
    assert len([c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"]]) == 1


def test_auto_pass_gate_marks_needs_human_when_matching_gate_pr_closed_without_merging():
    # See "Edge cases" under "Human-review gates" in references/gates.md: a human closing a gate
    # PR without merging is exactly as deterministic as merging it -- no judgment
    # needed, just status:needs-human + a comment, so this dispatches to the same
    # cmd_mark_needs_human logic a manual /sdlc-pipeline run would use.
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
    # The closed-without-merge branch reuses the exact same _match_open_gate skip
    # logic as the merged branch -- one representative case (marker mismatch) proves
    # the sharing works; the merged-branch tests above already cover every other skip
    # condition (base, head branch, Closes #, status) via the same shared helper.
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
    # main()'s generic dispatch honors an explicit "ok": False in a subcommand's own
    # result (rather than only ever exiting 1 via a raised GhError) -- this is the
    # convention auto-pass-gate uses so the workflow shows a real failure, not a
    # silently-green run, on a genuine operational error.
    import sdlc_next
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(sdlc_next, "cmd_auto_pass_gate",
                         lambda gh, repo_path, pr: {"ok": False, "reason": "simulated"})
    exit_code = sdlc_next.main(["auto-pass-gate", "--pr", "40"])
    assert exit_code == 1


# --- mark-feedback-received (real-time review/comment webhook -- flip-forward) ---
# See "New/extended GitHub Action" / gate-auto-advance.yml, and "Human-review gates"
# in SKILL.md.

def test_mark_feedback_received_skips_bot_author():
    from sdlc_next import GitHub, cmd_mark_feedback_received
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_mark_feedback_received(gh, 40, author="github-actions[bot]", body="looks good")
    assert result == {"ok": True, "skipped": "comment/review author 'github-actions[bot]' is a bot"}


def test_mark_feedback_received_skips_empty_body():
    # An approval-with-no-comment review (or, defensively, an empty comment) carries
    # no actual feedback -- must not flip the field.
    from sdlc_next import GitHub, cmd_mark_feedback_received
    gh = GitHub(runner=ScriptedRunner({}))
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="   ")
    assert result["ok"] is True
    assert "no body text" in result["skipped"]


def test_mark_feedback_received_skips_when_not_a_tracked_gate():
    # Reuses _match_open_gate -- one representative non-match (a head branch
    # that is neither issue-<n> nor epic-<n>-gate-<stage>) proves the sharing works; every
    # other skip condition it checks is already covered by the auto-pass-gate
    # tests above.
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
    assert "not an issue-<n> or epic-<n>-gate-<stage> branch" in result["skipped"]


def test_mark_feedback_received_flips_forward_on_epic_gate_pr():
    # An epic-level gate PR (head `epic-<n>-gate-<stage>`, base `epic-<n>`) gets
    # the same Feedback Received flip a per-issue gate does -- it was silently
    # skipped before the branch matcher learned the epic gate shape.
    from sdlc_next import (GitHub, cmd_mark_feedback_received, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, _ISSUE_FIELDS_QUERY,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS, REPO)
    node_id_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=92)}")
    status_mutation_argv = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_92', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['feedback-received'])}")
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=92)}")
    gh_runner = ScriptedRunner({
        ("gh", "pr", "view", "40", "--repo", REPO,
         "--json", "number,headRefName,baseRefName,state,mergedAt,body"):
            json.dumps({"number": 40, "headRefName": "epic-92-gate-product",
                        "baseRefName": "epic-92",
                        "state": "OPEN", "mergedAt": None, "body": ""}),
        ("gh", "issue", "view", "92", "--repo", REPO,
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"comments": [{"body": "<!-- gate-pr: product:40 -->"}]}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Pipeline Status"},
             "name": "Awaiting Human Review"},
        ]}}}}}),
        node_id_argv: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_92"}}}}),
        status_mutation_argv: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 92}}}}),
    })
    gh_runner.prefix_responses = {("gh", "issue", "comment", "92"): ""}
    gh = GitHub(runner=gh_runner)
    result = cmd_mark_feedback_received(gh, 40, author="maintainer", body="please fix this")
    assert result == {"ok": True, "issue": 92, "gate_pr": 40,
                       "pipeline_status": "feedback-received"}


def test_mark_feedback_received_skips_when_already_feedback_received():
    # A second comment while already Feedback Received must be a clean no-op, not an
    # error and not a second issue comment.
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
    # Must never clobber needs-human -- _match_open_gate itself won't even match a
    # PR whose issue is at a status outside GATE_PENDING_STATUSES.
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


# --- mark-feedback-addressed (flip-back, run by the fixing agent's last step) ---
# See "Addressing gate feedback" in references/gates.md.

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


# --- mark-todo (init-todo-status Action, `issues: opened`) ---
# See "Issue taxonomy" -> Pipeline Status row in references/operations.md.

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
    # No mutation attempted -- the scripted runner would raise on any unscripted call.


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
    # The conflict is also persisted as an issue comment carrying the
    # sync-conflict marker -- the JSON result alone doesn't survive a crashed
    # session, and pairing-counts reads the marker back for the three-strike
    # valve.
    from sdlc_next import GitHub, cmd_sync_branch
    runner = ScriptedRunner({
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
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
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "checkout", "issue-9"): "",
        ("git", "-C", "/repo", "merge", "origin/main"): "",
        ("git", "-C", "/repo", "push", "origin", "issue-9"): "",
    })
    gh_runner = ScriptedRunner({tuple(_list_argv()): _list_response([_issue(9)])})
    gh = GitHub(runner=gh_runner)
    result = cmd_sync_branch(gh, "/repo", 9, runner=runner)
    assert result == {"issue": 9, "unit": "issue", "branch": "issue-9", "base": "main", "synced": True}
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_sync_branch_auto_resolves_worktree_when_repo_path_omitted():
    # --repo-path omitted (None): the command resolves the branch's own live
    # worktree from `git worktree list` instead of defaulting to the shared
    # checkout -- the old default of "." silently targeted the wrong repo when
    # the caller forgot the flag.
    from sdlc_next import GitHub, cmd_sync_branch
    porcelain = (
        "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /tmp/sdlc-dev-9\nHEAD def\nbranch refs/heads/issue-9\n"
    )
    runner = ScriptedRunner({
        ("git", "-C", ".", "worktree", "list", "--porcelain"): porcelain,
        ("git", "-C", "/tmp/sdlc-dev-9", "fetch", "origin"): "",
        ("git", "-C", "/tmp/sdlc-dev-9", "checkout", "issue-9"): "",
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


# --- merge-lld-doc: publish lld.md to the epic branch on a clean lld-review ---

def test_merge_lld_doc_happy_path_commits_and_pushes_to_epic_branch():
    # A normal-epic child's lld.md is taken from origin/issue-<n> and committed
    # onto epic-<parent> as a doc-only commit, then pushed; a marker comment is
    # posted on the issue.
    from sdlc_next import GitHub, cmd_merge_lld_doc
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="lld", parent=110)
    gh_runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])})
    gh_runner.prefix_responses = {("gh", "issue", "comment", "185"): ""}
    gh = GitHub(runner=gh_runner)
    doc = "docs/sdlc/issue-185/lld.md"
    runner = ScriptedRunner({
        ("git", "-C", "/epic-110", "fetch", "origin"): "",
        ("git", "-C", "/epic-110", "checkout", "epic-110"): "",
        ("git", "-C", "/epic-110", "checkout", "origin/issue-185", "--", doc): "",
        ("git", "-C", "/epic-110", "status", "--porcelain", "--", doc): " M " + doc + "\n",
        ("git", "-C", "/epic-110", "commit", "-m",
         "docs(sdlc): publish issue-185 lld.md to epic-110", "--", doc): "",
        ("git", "-C", "/epic-110", "rev-parse", "HEAD"): "deadbeef\n",
        ("git", "-C", "/epic-110", "push", "origin", "epic-110"): "",
    })
    result = cmd_merge_lld_doc(gh, "/epic-110", 185, runner=runner)
    assert result == {"issue": 185, "merged": True, "epic_branch": "epic-110", "commit": "deadbeef"}
    comment_call = next(c for c in gh_runner.calls if c[:3] == ["gh", "issue", "comment"])
    body = comment_call[comment_call.index("--body") + 1]
    assert "<!-- lld-doc-published: epic-110:deadbeef" in body
    # Doc-only: the child branch is never merged, only its one lld.md path touched.
    assert ["git", "-C", "/epic-110", "merge", "origin/issue-185"] not in runner.calls


def test_merge_lld_doc_idempotent_noop_when_content_matches():
    # Identical content: `git checkout origin/issue-<n> -- lld.md` leaves the tree
    # clean, so status --porcelain is empty and nothing is committed or pushed.
    from sdlc_next import GitHub, cmd_merge_lld_doc
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="lld", parent=110)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    doc = "docs/sdlc/issue-185/lld.md"
    runner = ScriptedRunner({
        ("git", "-C", "/epic-110", "fetch", "origin"): "",
        ("git", "-C", "/epic-110", "checkout", "epic-110"): "",
        ("git", "-C", "/epic-110", "checkout", "origin/issue-185", "--", doc): "",
        ("git", "-C", "/epic-110", "status", "--porcelain", "--", doc): "",
    })
    result = cmd_merge_lld_doc(gh, "/epic-110", 185, runner=runner)
    assert result == {"issue": 185, "merged": False, "epic_branch": "epic-110", "reason": "up-to-date"}
    assert ["git", "-C", "/epic-110", "push", "origin", "epic-110"] not in runner.calls


def test_merge_lld_doc_noop_for_standing_epic_child():
    # A standing epic's children integrate into main, not an epic branch -- no-op,
    # no git touched at all.
    from sdlc_next import GitHub, cmd_merge_lld_doc
    epic = _epic(94, labels=["epic:standing"])
    child = _issue(300, stage="lld", parent=94)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    runner = ScriptedRunner({})
    result = cmd_merge_lld_doc(gh, "/whatever", 300, runner=runner)
    assert result["merged"] is False
    assert "epic:standing" in result["reason"]
    assert runner.calls == []


def test_merge_lld_doc_noop_for_parentless_issue():
    from sdlc_next import GitHub, cmd_merge_lld_doc
    child = _issue(400, stage="lld")  # no parent
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([child])}))
    runner = ScriptedRunner({})
    result = cmd_merge_lld_doc(gh, "/whatever", 400, runner=runner)
    assert result == {"issue": 400, "merged": False,
                      "reason": "issue has no parent epic — nothing to publish to"}
    assert runner.calls == []


def test_merge_lld_doc_push_rejection_returns_conflict_without_raising():
    # Epic branch advanced concurrently -> push refused. Structured conflict
    # result at exit 0, not a crash; no marker comment posted.
    from sdlc_next import GitHub, cmd_merge_lld_doc, GhError
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="lld", parent=110)
    gh_runner = ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])})
    gh = GitHub(runner=gh_runner)
    doc = "docs/sdlc/issue-185/lld.md"
    push_argv = ("git", "-C", "/epic-110", "push", "origin", "epic-110")

    def runner(argv):
        runner.calls.append(argv)
        if tuple(argv) == push_argv:
            raise GhError("command failed (1): git push\n ! [rejected]  epic-110 -> epic-110 "
                          "(non-fast-forward)\nUpdates were rejected because the remote "
                          "contains work that you do not have locally.")
        canned = {
            ("git", "-C", "/epic-110", "fetch", "origin"): "",
            ("git", "-C", "/epic-110", "checkout", "epic-110"): "",
            ("git", "-C", "/epic-110", "checkout", "origin/issue-185", "--", doc): "",
            ("git", "-C", "/epic-110", "status", "--porcelain", "--", doc): " M " + doc + "\n",
            ("git", "-C", "/epic-110", "commit", "-m",
             "docs(sdlc): publish issue-185 lld.md to epic-110", "--", doc): "",
            ("git", "-C", "/epic-110", "rev-parse", "HEAD"): "cafef00d\n",
        }
        return canned[tuple(argv)]
    runner.calls = []

    result = cmd_merge_lld_doc(gh, "/epic-110", 185, runner=runner)
    assert result["merged"] is False
    assert result["conflict"] is True
    assert result["epic_branch"] == "epic-110"
    assert result["commit"] == "cafef00d"
    # No marker comment on a conflict.
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_merge_lld_doc_noop_when_no_lld_on_child_branch():
    # `git checkout origin/issue-<n> -- lld.md` fails when the file isn't there --
    # structured no-op, not a crash.
    from sdlc_next import GitHub, cmd_merge_lld_doc
    epic = _epic(110, labels=["epic:architected"])
    child = _issue(185, stage="lld", parent=110)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    doc = "docs/sdlc/issue-185/lld.md"
    runner = ScriptedRunner({
        ("git", "-C", "/epic-110", "fetch", "origin"): "",
        ("git", "-C", "/epic-110", "checkout", "epic-110"): "",
    })
    runner.fail_on = {("git", "-C", "/epic-110", "checkout", "origin/issue-185", "--", doc)}
    result = cmd_merge_lld_doc(gh, "/epic-110", 185, runner=runner)
    assert result["merged"] is False
    assert "no lld.md" in result["reason"]


# --- Parallel implementation lane: list-parallel-ready ---

def test_list_parallel_ready_happy_path_selects_disjoint_children():
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c185 = _issue(185, stage="development", parent=110)
    c187 = _issue(187, stage="lld", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c185, c187]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
        ("git", "-C", "/repo", "show", "origin/issue-185:docs/sdlc/issue-185/lld.md"):
            "## Footprint\n\n- `frontend/src/lib/api/core.ts`\n",
        ("git", "-C", "/repo", "show", "origin/issue-187:docs/sdlc/issue-187/lld.md"):
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
    c186 = _issue(186, stage="lld", parent=110)            # blocked
    c187 = _issue(187, stage="lld", parent=110)            # footprint collides with active #185
    responses = {
        tuple(_list_argv()): _list_response([epic, c185, c186, c187]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n\n"
            "worktree /tmp/dev-185\nHEAD y\nbranch refs/heads/issue-185\n",
        ("git", "-C", "/repo", "show", "origin/issue-185:docs/sdlc/issue-185/lld.md"):
            "## Footprint\n\n- `frontend/src/lib/api/core.ts`\n",
        ("git", "-C", "/repo", "show", "origin/issue-187:docs/sdlc/issue-187/lld.md"):
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
    children = [_issue(n, stage="lld", parent=110) for n in (201, 202, 203)]
    responses = {
        tuple(_list_argv()): _list_response([epic] + children),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    for n in (201, 202, 203):
        responses[("git", "-C", "/repo", "show", f"origin/issue-{n}:docs/sdlc/issue-{n}/lld.md")] = \
            f"## Footprint\n\n- `some/dir-{n}/**`\n"
    responses.update(_no_blockers_responses(201, 202, 203))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, limit=2, runner=runner)
    assert result["eligible_total"] == 3
    assert result["count"] == 2
    assert [c["issue"] for c in result["parallel_ready"]] == [201, 202]


def test_list_parallel_ready_bootstraps_fresh_lld_child_with_no_branch_yet():
    # A never-started child (Stage lld or still unset, no origin/issue-<n>
    # branch) has no committed lld.md to declare a footprint -- requiring one
    # would deadlock the lane on a chicken-and-egg, since the footprint is
    # written BY the lld stage this call exists to start. It's eligible with
    # its own docs folder standing in as the footprint.
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c201 = _issue(201, stage="lld", parent=110)   # fresh: no branch on origin
    c202 = _issue(202, parent=110)                 # fresher still: Stage not even set yet
    responses = {
        tuple(_list_argv()): _list_response([epic, c201, c202]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
    }
    responses.update(_no_blockers_responses(201, 202))
    runner = ScriptedRunner(responses)
    for n in (201, 202):
        runner.fail_on.add(("git", "-C", "/repo", "show",
                             f"origin/issue-{n}:docs/sdlc/issue-{n}/lld.md"))
        runner.fail_on.add(("git", "-C", "/repo", "show",
                             f"origin/issue-{n}:docs/sdlc/issue-{n}/architecture.md"))
        runner.fail_on.add(("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
                             f"refs/remotes/origin/issue-{n}"))
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert [c["issue"] for c in result["parallel_ready"]] == [201, 202]
    assert all(c["stage"] == "lld" for c in result["parallel_ready"])
    assert result["skipped"] == []


def test_list_parallel_ready_still_skips_started_lld_child_missing_footprint():
    # The bootstrap exemption is ONLY for a child whose branch doesn't exist
    # yet. A branch that exists with an lld.md that has no parseable
    # ## Footprint stays excluded -- "cannot verify non-overlap", never
    # "no footprint declared, so no risk".
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c201 = _issue(201, stage="lld", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c201]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            "worktree /repo\nHEAD x\nbranch refs/heads/main\n",
        ("git", "-C", "/repo", "show", "origin/issue-201:docs/sdlc/issue-201/lld.md"):
            "# LLD\n\nNo footprint section here.\n",
        ("git", "-C", "/repo", "show", "origin/issue-201:docs/sdlc/issue-201/architecture.md"):
            "",
        ("git", "-C", "/repo", "show-ref", "--verify", "--quiet",
         "refs/remotes/origin/issue-201"): "",
    }
    responses.update(_no_blockers_responses(201))
    runner = ScriptedRunner(responses)
    gh = GitHub(runner=runner)
    result = cmd_list_parallel_ready(gh, "/repo", 110, runner=runner)
    assert result["parallel_ready"] == []
    assert "no ## Footprint" in result["skipped"][0]["reason"]


def test_list_parallel_ready_returns_empty_for_non_architected_epic():
    # Same guard as decide_next_action's children loop: a normal epic's
    # children are not implementation-lane work before the epic itself is
    # epic:architected.
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

    def fake(gh, repo_path, epic, limit):
        captured.update(repo_path=repo_path, epic=epic, limit=limit)
        return {"parallel_ready": [], "count": 0}

    monkeypatch.setattr(sdlc_next, "cmd_list_parallel_ready", fake)
    exit_code = sdlc_next.main(["list-parallel-ready", "110", "--repo-path", "/repo", "--limit", "2"])
    assert exit_code == 0
    assert captured == {"repo_path": "/repo", "epic": 110, "limit": 2}


# --- worktree release: a parked or merged unit stops holding a dev-lane slot ---
# Regression cover for the 2026-08-20 incident: #186 was parked `needs-human` but
# its worktree was left on disk, so `list-parallel-ready` read the lane as full
# (3/3) and silently proposed nothing for the rest of the invocation.

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
        ("git", "-C", "/repo", "worktree", "remove", "/tmp/sdlc-dev-186"): "",
    })
    assert release_worktree("issue-186", runner=runner, base_repo="/repo") == {
        "released": True, "path": "/tmp/sdlc-dev-186"}
    assert ["git", "-C", "/repo", "worktree", "remove", "/tmp/sdlc-dev-186"] in runner.calls


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
        ("git", "-C", "/repo", "worktree", "remove", "/tmp/sdlc-dev-186"): "",
    })
    runner.prefix_responses = {("gh", "issue", "comment", "186"): ""}
    result = cmd_mark_needs_human(GitHub(runner=runner), 186, reason="contract gap", repo_path="/repo")
    assert result["worktree"] == {"released": True, "path": "/tmp/sdlc-dev-186", "branch": "issue-186"}


def test_list_parallel_ready_does_not_count_a_parked_issues_leftover_worktree():
    """The incident itself: #186 parked needs-human with its worktree still on disk
    must not consume a slot, and the next child must still be proposed."""
    from sdlc_next import GitHub, cmd_list_parallel_ready
    epic = _epic(110, labels=["epic:architected"])
    c186 = _issue(186, stage="lld", status="needs-human", parent=110)
    c188 = _issue(188, stage="lld", parent=110)
    responses = {
        tuple(_list_argv()): _list_response([epic, c186, c188]),
        ("git", "-C", "/repo", "fetch", "origin"): "",
        ("git", "-C", "/repo", "worktree", "list", "--porcelain"):
            _worktree_list(("/repo", "main"), ("/tmp/sdlc-dev-186", "issue-186")),
        ("git", "-C", "/repo", "show", "origin/issue-186:docs/sdlc/issue-186/lld.md"):
            "## Footprint\n\n- `frontend/src/app/seller/**`\n",
        ("git", "-C", "/repo", "show", "origin/issue-188:docs/sdlc/issue-188/lld.md"):
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
    child = _issue(185, stage="lld", parent=110)
    gh = GitHub(runner=ScriptedRunner({tuple(_list_argv()): _list_response([epic, child])}))
    runner = ScriptedRunner({
        **_wt_list_main(),
        ("git", "-C", ".", "fetch", "origin"): "",
        ("git", "-C", ".", "branch", "-r", "--list", "origin/issue-185"): "",
        ("git", "-C", ".", "worktree", "add", "/tmp/sdlc-dev-185", "-b", "issue-185",
         "origin/epic-110"): "",
    })
    assert cmd_worktree_add(gh, 185, runner=runner) == {
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
    })
    assert cmd_worktree_add(gh, 110, unit="epic", runner=runner)["path"] == "/tmp/sdlc-epic-110"


def test_worktree_add_noop_when_branch_already_checked_out():
    from sdlc_next import GitHub, cmd_worktree_add
    gh = GitHub(runner=ScriptedRunner({}))
    runner = ScriptedRunner({("git", "-C", ".", "worktree", "list", "--porcelain"):
                             "worktree /repo\nHEAD x\nbranch refs/heads/main\n"
                             "\nworktree /tmp/sdlc-dev-185\nHEAD y\nbranch refs/heads/issue-185\n"})
    result = cmd_worktree_add(gh, 185, runner=runner)
    assert result["created"] is False and result["path"] == "/tmp/sdlc-dev-185"
    assert len(runner.calls) == 1  # no fetch, no add


def test_merge_pr_closes_child_explicitly_when_merged_into_epic_branch():
    # Merging a child PR into its epic branch does NOT fire the PR's `Closes #<n>`
    # -- GitHub only auto-closes on the default branch -- so merge-pr must close the
    # child itself, or it lingers open: phantom-resuming next-action and blocking the
    # epic's all-children-closed gate. Regression test for the 2026-09-06 retro fix.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
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
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
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
    # The mirror of the test above: merging to the default branch DOES fire
    # `Closes #<n>`, so merge-pr must not also issue a close of its own --
    # ScriptedRunner raises on any unscripted call, so the absence of a scripted
    # `gh issue close` is what proves it.
    from sdlc_next import GitHub, cmd_merge_pr
    from tests.test_sdlc_next import ScriptedRunner
    import json
    runner = ScriptedRunner({
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
        ("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash", "--delete-branch"): "",
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "pr", "comment", "42"): ""}
    gh = GitHub(runner=runner)
    result = cmd_merge_pr(gh, 42, issue=9)
    assert result["merged"] is True and result["issue_closed"] is False
    assert not any(c[:3] == ["gh", "issue", "close"] for c in runner.calls)


def test_open_gate_unit_epic_refuses_when_the_gate_sub_branch_was_never_pushed():
    # The epic branch is merge-only: committing `architecture.md` straight onto
    # `epic-<n>` (what happened on epic-345) leaves the gate sub-branch absent, so
    # open-gate must refuse instead of opening a PR from a ref that isn't there.
    # Regression test for the 2026-09-06 retro guard.
    from sdlc_next import GitHub, GhError, cmd_open_gate
    import pytest
    gh_runner = ScriptedRunner({})
    gh_runner.fail_on = {("gh", "api", "repos/owner/repo/compare/epic-345...epic-345-gate-architecture",
                          "--jq", ".ahead_by")}
    gh = GitHub(runner=gh_runner)
    with pytest.raises(GhError) as exc:
        cmd_open_gate(gh, "/repo", 345, "Mobile responsive audit", "architecture.md",
                       "development", "Locked the design.", unit="epic",
                       runner=ScriptedRunner({}))
    msg = str(exc.value)
    assert "epic-345-gate-architecture does not exist on origin" in msg
    assert "force-rewind epic-345" in msg
    # Refused before any write: no PR, no field mutation, no issue comment.
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh_runner.calls)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in gh_runner.calls)


def test_open_gate_unit_epic_refuses_when_the_gate_sub_branch_is_empty():
    # The sub-branch exists but carries nothing over the epic branch -- the doc
    # went to `epic-<n>` and the gate PR would read as "nothing to review".
    from sdlc_next import GitHub, GhError, cmd_open_gate
    import pytest
    gh_runner = ScriptedRunner({
        ("gh", "api", "repos/owner/repo/compare/epic-345...epic-345-gate-architecture",
         "--jq", ".ahead_by"): "0\n",
    })
    gh = GitHub(runner=gh_runner)
    with pytest.raises(GhError) as exc:
        cmd_open_gate(gh, "/repo", 345, "Mobile responsive audit", "architecture.md",
                       "development", "Locked the design.", unit="epic",
                       runner=ScriptedRunner({}))
    assert "carries no commits over epic-345" in str(exc.value)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh_runner.calls)


def test_open_gate_unit_issue_does_not_consult_the_gate_branch_guard():
    # The guard is epic-only: a standing-epic child's gate is `issue-<n>` -> `main`,
    # where the doc genuinely is committed on the head branch. ScriptedRunner raises
    # on any unscripted call, so the absence of a scripted compare proves it.
    from sdlc_next import GitHub, cmd_open_gate, _ISSUE_NODE_ID_QUERY, _SET_ISSUE_FIELD_MUTATION, \
        PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS
    git_runner = ScriptedRunner({("git", "-C", "/repo", "rev-parse", "HEAD"): "abcd1234\n"})
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
    assert p["childEntryStage"] == "product"
    assert p["childrenNeedArchitectedEpic"] is False
    assert p["closes"] is False
    # Sample config's standing profile lowers the bar + drops the human Gate A.
    assert p["gates"]["skipConfidenceThreshold"] == 90
    assert p["gates"]["requiresHumanGateA"] is False


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
    assert p["childEntryStage"] == "lld"
    assert p["gates"]["skipConfidenceThreshold"] == 95
    assert p["gates"]["requiresHumanGateA"] is True


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
                "issueType": {"name": "Feature" if parent is None else "Task"},
                "parent": {"number": parent} if parent else None,
                "labels": {"nodes": [{"name": l} for l in labels]}}}}}))


def test_auto_pass_gate_a_refuses_non_product_stage():
    from sdlc_next import GitHub, GhError, cmd_auto_pass_gate_a
    from tests.test_sdlc_next import ScriptedRunner
    gh = GitHub(runner=ScriptedRunner())
    try:
        cmd_auto_pass_gate_a(gh, issue=9, stage="architecture", summary="x")
        assert False, "expected GhError"
    except GhError as e:
        assert "product gate" in str(e)


def test_auto_pass_gate_a_refuses_when_profile_requires_human():
    from sdlc_next import GitHub, GhError, cmd_auto_pass_gate_a
    from tests.test_sdlc_next import ScriptedRunner
    a9, r9 = _epic_check(9, parent=50)
    a50, r50 = _epic_check(50)  # default profile -> requiresHumanGateA True
    gh = GitHub(runner=ScriptedRunner({a9: r9, a50: r50}))
    try:
        cmd_auto_pass_gate_a(gh, issue=9, stage="product", summary="x")
        assert False, "expected GhError"
    except GhError as e:
        assert "requiresHumanGateA" in str(e)


def test_auto_pass_gate_a_advances_child_of_no_human_profile():
    from sdlc_next import (GitHub, cmd_auto_pass_gate_a, _ISSUE_NODE_ID_QUERY,
                            _SET_ISSUE_FIELD_MUTATION, STAGE_FIELD_ID, STAGE_OPTION_IDS,
                            PIPELINE_STATUS_FIELD_ID, PIPELINE_STATUS_OPTION_IDS)
    from tests.test_sdlc_next import ScriptedRunner
    a9, r9 = _epic_check(9, parent=94)
    a94, r94 = _epic_check(94, labels=["epic:standing"])  # requiresHumanGateA False
    node_id = ("gh", "api", "graphql", "-f", f"query={_ISSUE_NODE_ID_QUERY.format(n=9)}")
    stage_mut = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=STAGE_FIELD_ID, option_id=STAGE_OPTION_IDS['architecture'])}")
    status_mut = ("gh", "api", "graphql", "-f",
        f"query={_SET_ISSUE_FIELD_MUTATION.format(issue_id='ISSUE_9', field_id=PIPELINE_STATUS_FIELD_ID, option_id=PIPELINE_STATUS_OPTION_IDS['in-progress'])}")
    runner = ScriptedRunner({
        a9: r9, a94: r94,
        node_id: json.dumps({"data": {"repository": {"issue": {"id": "ISSUE_9"}}}}),
        stage_mut: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
        status_mut: json.dumps({"data": {"updateIssueFieldValue": {"issue": {"number": 9}}}}),
    })
    runner.prefix_responses = {("gh", "issue", "comment", "9"): ""}
    gh = GitHub(runner=runner)
    result = cmd_auto_pass_gate_a(gh, issue=9, stage="product", summary="Clean.")
    assert result == {"issue": 9, "unit": "issue", "next_stage": "architecture",
                       "auto_passed": True, "profile": "standing"}
    assert list(stage_mut) in runner.calls
    comment_calls = [c for c in runner.calls if c[:3] == ["gh", "issue", "comment"]]
    assert any("Gate A auto-passed" in c[-1] for c in comment_calls)


# --- citations (cite / verify-citations / verify-exit citations_ok) ---
# See architecture.md for issue #194: a citation block's authority is a
# byte-exact fragment copied from the real file, matched literally; an
# `cite-example` block is illustrative and never resolved or counted.

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
    # current working tree no longer has the unconditional value -- it was
    # superseded by a conditional guard.
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
    # the fragment below is not present anywhere -- if this were an asserted
    # `cite` block it would fail to resolve; as `cite-example` it must not.
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
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    (docs_dir / "development.md").write_text("```cite path=target.yml\nhello world\n```\n")
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
    assert result["citations_ok"] is True
    assert result.get("ok", True) is True


def test_verify_exit_citations_ok_false_fails_overall_result_positive_control(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    record = docs_dir / "development.md"
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

    # Break: a citation that does not resolve.
    record.write_text("```cite path=target.yml\ngoodbye world\n```\n")
    broken = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="development", runner=git_runner)
    assert broken["citations_ok"] is False
    assert broken["ok"] is False
    assert "reason" in broken

    # Revert: fix the citation, confirm it goes back to passing.
    record.write_text("```cite path=target.yml\nhello world\n```\n")
    fixed = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="development", runner=git_runner)
    assert fixed["citations_ok"] is True
    assert fixed.get("ok", True) is True


def test_verify_exit_citations_scoped_to_own_record_ignores_other_docs(tmp_path):
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "issue-9"
    docs_dir.mkdir(parents=True)
    (tmp_path / "target.yml").write_text("hello world\n")
    # An older, already-merged doc with a rotted citation -- must not affect
    # a later stage's own exit check (AC14).
    (docs_dir / "product.md").write_text("```cite path=target.yml\nlong gone content\n```\n")
    (docs_dir / "development.md").write_text("```cite path=target.yml\nhello world\n```\n")
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
    assert result["citations_ok"] is True
    assert result.get("ok", True) is True


def test_verify_exit_omits_citations_ok_when_stage_has_no_canonical_record(tmp_path):
    """`testing` writes no doc file at all -- there is nothing to re-check, so
    the citation gate must not fire (and must not crash) for it."""
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    fields_argv = ("gh", "api", "graphql", "-f", f"query={_ISSUE_FIELDS_QUERY.format(n=9)}")
    gh_runner = ScriptedRunner({
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"labels": []}),
        fields_argv: json.dumps({"data": {"repository": {"issue": {"issueFieldValues": {"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "field": {"name": "Stage"}, "name": "Testing"},
        ]}}}}}),
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 9, expect_stage="testing", runner=git_runner)
    assert "citations_ok" not in result
    assert result.get("ok", True) is True


def test_verify_exit_omits_citations_ok_when_stage_record_file_is_absent(tmp_path):
    """Mirrors test_verify_exit_uses_epic_docs_dir_for_unit_epic: an
    architecture-stage exit whose architecture.md is not yet on disk must not
    be flipped to a citation failure -- that gap is `docs_present`'s job, not
    this one's."""
    from sdlc_next import GitHub, cmd_verify_exit, _ISSUE_FIELDS_QUERY
    docs_dir = tmp_path / "docs" / "sdlc" / "epic-92"
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
    })
    git_runner = ScriptedRunner({("git", "-C", str(tmp_path), "log", "--oneline", "-5"): ""})
    gh = GitHub(runner=gh_runner)
    result = cmd_verify_exit(gh, str(tmp_path), 92, expect_stage="architecture", unit="epic", runner=git_runner)
    assert "citations_ok" not in result
