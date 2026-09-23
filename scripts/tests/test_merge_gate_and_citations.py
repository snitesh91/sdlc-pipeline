"""Regression + positive-control tests: the citation resolver fails closed on malformed
input, and merge-pr pins the head SHA whose checks it read."""
import json

import pytest

import sdlc_next as s
from tests.test_sdlc_next import (ScriptedRunner, _script_terminal_fields, _NO_UNIT_WORKTREE, _clean_pipeline_comments,
                                  _issue, _list_argv, _list_response)


# ---- citations: every malformed input is reported, never raised ----

def test_cite_block_without_path_reports_unresolved(tmp_path):
    result = s.verify_citations_text("```cite lang=py\nfoo\n```\n", repo_path=str(tmp_path))
    assert result["all_resolved"] is False
    [c] = result["citations"]
    assert c["resolved"] is False and "path" in c["cited_vs_found"]


def test_cite_block_without_path_fails_the_document_closed(tmp_path):
    (tmp_path / "doc.md").write_text("```cite lang=py\nfoo\n```\n")
    result = s.cmd_verify_citations(["doc.md"], repo_path=str(tmp_path))
    assert result["ok"] is False


@pytest.mark.parametrize("lines", ["5-", "1-2-3", "-5", "abc-def", "2"])
def test_cite_malformed_lines_range_returns_ok_false(tmp_path, lines):
    (tmp_path / "foo.yml").write_text("one\ntwo\nthree\n")
    result = s.cmd_cite("foo.yml", lines=lines, repo_path=str(tmp_path))
    assert result["ok"] is False and "block" not in result
    assert lines in result["reason"]


def test_cite_well_formed_lines_range_still_emits_a_block(tmp_path):
    # Positive control for the range guard.
    (tmp_path / "foo.yml").write_text("one\ntwo\nthree\n")
    result = s.cmd_cite("foo.yml", lines="2-3", repo_path=str(tmp_path))
    assert result["block"] == "```cite path=foo.yml\ntwo\nthree\n```"


def test_whitespace_only_body_never_resolves(tmp_path):
    (tmp_path / "foo.yml").write_text("a\n\n\nb\n")
    blank = s.resolve_citation("foo.yml", "\n", repo_path=str(tmp_path))
    assert blank["resolved"] is False and "empty" in blank["cited_vs_found"]
    # Positive control: a real fragment in the same file resolves.
    assert s.resolve_citation("foo.yml", "b", repo_path=str(tmp_path))["resolved"] is True


def test_cite_refuses_a_blank_selection(tmp_path):
    (tmp_path / "foo.yml").write_text("a\n   \nb\n")
    result = s.cmd_cite("foo.yml", line=2, repo_path=str(tmp_path))
    assert result["ok"] is False and "block" not in result
    assert "blank" in result["reason"]


def test_verify_citations_missing_document_returns_ok_false(tmp_path):
    result = s.cmd_verify_citations(["missing.md"], repo_path=str(tmp_path))
    assert result["ok"] is False and result["all_resolved"] is False
    assert "missing.md" in result["reason"]


def test_verify_citations_one_missing_document_fails_the_set_but_reports_the_rest(tmp_path):
    (tmp_path / "t.yml").write_text("x\n")
    (tmp_path / "good.md").write_text("```cite path=t.yml\nx\n```\n")
    result = s.cmd_verify_citations(["good.md", "missing.md"], repo_path=str(tmp_path))
    assert result["ok"] is False
    good, missing = result["documents"]
    assert good["all_resolved"] is True
    assert missing["all_resolved"] is False and "missing.md" in missing["reason"]


def test_undecodable_citation_target_reports_unresolved(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00abc")
    result = s.resolve_citation("blob.bin", "abc", repo_path=str(tmp_path))
    assert result["resolved"] is False and "could not read" in result["cited_vs_found"]


def test_cite_on_undecodable_file_returns_ok_false(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00abc")
    result = s.cmd_cite("blob.bin", line=1, repo_path=str(tmp_path))
    assert result["ok"] is False and "could not read" in result["reason"]


# ---- merge-pr: the squash merges exactly the head whose checks were read ----

def _merge_runner():
    runner = ScriptedRunner({
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"):
            json.dumps({"state": "OPEN"}),
        ("gh", "pr", "checks", "42", "--repo", "owner/repo",
         "--json", "name,state,bucket,link,workflow"): json.dumps([{"name": "ci", "bucket": "pass"}]),
        ("gh", "api", "--paginate", "repos/owner/repo/pulls/42/files", "--jq", ".[].filename"):
            "docs/sdlc/issue-9/product.md\n",
        ("gh", "api", "repos/owner/repo/compare/main...issue-9", "--jq", ".behind_by"): "0\n",
        tuple(_list_argv()): _list_response([_issue(9)]),
        ("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "comments,headRefOid"):
            json.dumps({"comments": [], "headRefOid": "feedface"}),
        ("gh", "pr", "ready", "42", "--repo", "owner/repo"): "",
        ("gh", "issue", "view", "9", "--repo", "owner/repo",
         "--json", "number,title,labels,body,state,comments"):
            json.dumps({"state": "CLOSED", "comments": _clean_pipeline_comments()}),
        **_NO_UNIT_WORKTREE,
    })
    runner.prefix_responses = {("gh", "pr", "merge", "42"): "",
                               ("gh", "pr", "comment", "42"): "",
                               ("gh", "issue", "comment", "9"): ""}
    _script_terminal_fields(runner, 9)
    return runner


def test_merge_pr_pins_the_merge_to_the_head_whose_checks_it_read():
    runner = _merge_runner()
    result = s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    assert result["merged"] is True
    [merge] = [c for c in runner.calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge == ["gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash",
                     "--delete-branch", "--match-head-commit", "feedface"]


def test_merge_pr_reads_the_head_before_the_checks():
    # The pinned SHA must predate the checks read, or a push between the two merges unchecked.
    runner = _merge_runner()
    s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
    order = [c[:3] + c[-1:] for c in runner.calls]
    head_read = order.index(["gh", "pr", "view", "comments,headRefOid"])
    checks_read = order.index(["gh", "pr", "checks", "name,state,bucket,link,workflow"])
    assert head_read < checks_read


def test_pr_merge_without_a_head_pin_sends_no_match_flag():
    # Positive control: the flag is opt-in on the primitive.
    runner = ScriptedRunner({("gh", "pr", "merge", "7", "--repo", "owner/repo", "--squash",
                              "--delete-branch"): ""})
    s.GitHub(runner=runner).pr_merge(7)
    assert runner.calls == [["gh", "pr", "merge", "7", "--repo", "owner/repo", "--squash",
                             "--delete-branch"]]


def test_merge_pr_still_raises_when_the_pinned_merge_is_rejected_and_the_pr_stays_open():
    runner = _merge_runner()
    runner.prefix_responses.pop(("gh", "pr", "merge", "42"))
    runner.fail_on = {("gh", "pr", "merge", "42", "--repo", "owner/repo", "--squash",
                       "--delete-branch", "--match-head-commit", "feedface")}
    with pytest.raises(s.GhError):
        s.cmd_merge_pr(s.GitHub(runner=runner), 42, issue=9)
