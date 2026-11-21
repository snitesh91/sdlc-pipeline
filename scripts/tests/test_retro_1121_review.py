"""Retro 2026-11-21, review fixes: an Initiative never merges or closes over an open direct
child, and a `met` verification covers only the PR head it ran on."""
import sdlc_next as s
from tests.test_initiative_branch import _PASS, _all_closed, _initiative_pr, opted_in  # noqa: F401


def _ready(gh):
    _initiative_pr(gh, checks=[_PASS], comments=["<!-- local-ci: e2e:300 @ abcd0000 -->"])


def _merge(gh):
    return s.cmd_merge_initiative_pr(gh, 6, runner=lambda argv: "")


def test_an_open_direct_child_blocks_the_initiative_merge(opted_in):
    gh = _all_closed()
    _ready(gh)
    s.cmd_record_initiative_verification(gh, 6, "all met")
    gh.issues[12] = {**gh.issues[9], "number": 12, "labels": ["type:bug"], "state": "OPEN"}
    held = _merge(gh)
    assert held.get("merged") is not True and held["open_children"] == [12]
    # Positive control: once it closes, the merge goes through.
    gh.issues[12]["state"] = "CLOSED"
    assert _merge(gh)["merged"] is True


def test_a_met_verification_of_another_head_is_stale(opted_in):
    gh = _all_closed()
    _ready(gh)
    s.cmd_record_initiative_verification(gh, 6, "all met")
    assert "sha:abcd0000" in gh.issues[6]["comments"][-1]
    gh.prs[300]["headRefOid"] = "ffff1111"
    gh.prs[300]["comments"] = ["<!-- local-ci: e2e:300 @ ffff1111 -->"]
    gh.branch_head_sha = lambda branch: "ffff1111"
    held = _merge(gh)
    assert held["merged"] is False and "re-run initiative-close" in held["reason"]
    # Positive control: verified again on the new head, it merges.
    s.cmd_record_initiative_verification(gh, 6, "all met")
    assert _merge(gh)["merged"] is True
