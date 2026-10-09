"""Retro 2026-10-09 control-plane fixes: update-pr-body, open-dev-pr recovery/adoption,
record-local-ci on an uncovered PR, verify-exit's pushed-head check."""
import json

import pytest

import sdlc_next as s
from sdlc_next import GhError
from tests.test_sdlc_next import ScriptedRunner
from tests.test_v2_phase_tasks import FakeGh


class _BodyGh(FakeGh):
    def pr_edit_body(self, n, body):
        self.prs[n]["body"] = body


def _file(tmp_path, text, name="b.md"):
    f = tmp_path / name
    f.write_text(text)
    return str(f)


# --- update-pr-body (dev-rework-pr-body-edit-blocked) ---

def test_update_pr_body_replaces_the_description_and_keeps_the_closing_line(tmp_path):
    gh = _BodyGh([], prs={42: {"headRefName": "issue-9", "body": "old\n\nCloses #9"}})
    out = s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New: rework round 2 changed X."))
    assert out["updated"] is True and out["kept_closing_lines"] == ["Closes #9"]
    assert gh.prs[42]["body"] == "New: rework round 2 changed X.\n\nCloses #9"


def test_update_pr_body_does_not_duplicate_a_closing_line_the_new_body_has(tmp_path):
    gh = _BodyGh([], prs={42: {"body": "old\n\nCloses #9"}})
    s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New.\n\nCloses #9"))
    assert gh.prs[42]["body"] == "New.\n\nCloses #9"


def test_update_pr_body_refuses_a_closed_pr_and_an_empty_file(tmp_path):
    gh = _BodyGh([], prs={42: {"state": "MERGED", "body": "old"}})
    with pytest.raises(GhError, match="not open"):
        s.cmd_update_pr_body(gh, 42, _file(tmp_path, "New."))
    with pytest.raises(GhError, match="empty"):
        s.cmd_update_pr_body(gh, 42, _file(tmp_path, "  \n"))
    assert gh.prs[42]["body"] == "old"


def test_pr_edit_body_is_a_rest_patch_cloud_safe():
    runner = ScriptedRunner({("gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42",
                              "-f", "body=x"): "{}"})
    s.GitHubRest(runner=runner).pr_edit_body(42, "x")
    assert runner.calls == [["gh", "api", "-X", "PATCH", "repos/owner/repo/pulls/42",
                             "-f", "body=x"]]


def test_cli_wires_update_pr_body(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_update_pr_body", lambda *a: seen.append(a) or {"updated": True})
    assert s.main(["update-pr-body", "42", "--body-file", "b.md"]) == 0
    assert seen == [("GH", 42, "b.md")]
