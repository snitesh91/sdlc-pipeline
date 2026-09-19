"""Comment size caps: over-cap caller text is refused before any tracker call."""
import json

import pytest

import sdlc_next as s

# command -> argv with "{text}" where the capped flag's value goes
ARGV = {
    "handoff-to-pr-review": ["handoff-to-pr-review", "5", "--pr", "9", "--summary", "{text}"],
    "record-pr-review": ["record-pr-review", "5", "--pr", "9", "--outcome", "clean",
                         "--summary", "{text}"],
    "record-design-review": ["record-design-review", "5", "--role", "arch-review",
                             "--outcome", "clean", "--summary", "{text}"],
    "open-dev-pr": ["open-dev-pr", "5", "--title", "t", "--body", "b", "--summary", "{text}"],
    "open-gate": ["open-gate", "5", "--title", "t", "--doc", "product.md",
                  "--next-stage", "architecture", "--summary", "{text}"],
    "skip-gate": ["skip-gate", "5", "--stage", "architecture", "--confidence", "90",
                  "--summary", "{text}"],
    "waive-gate": ["waive-gate", "5", "--stage", "product", "--summary", "{text}"],
    "record-epic-verification": ["record-epic-verification", "9", "--kind", "e2e",
                                 "--summary", "{text}"],
    "record-initiative-verification": ["record-initiative-verification", "1",
                                       "--summary", "{text}"],
    "mark-needs-human": ["mark-needs-human", "5", "--reason", "{text}"],
    "resolve-thread": ["resolve-thread", "--thread-id", "T", "--reply", "{text}"],
    "route": ["route", "5", "--to", "development", "--reason", "{text}"],
}


def test_every_capped_command_has_a_case():
    assert set(ARGV) == set(s.COMMENT_CAPS)


def _run(monkeypatch, capsys, command, length):
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: calls.append(1) or "GH")
    monkeypatch.setattr(s, "cmd_" + command.replace("-", "_"), lambda *a, **k: {})
    monkeypatch.setattr(s, "phase_gate_title", lambda gh, issue, title: title)
    argv = [a.replace("{text}", "x" * length) for a in ARGV[command]]
    code = s.main(argv)
    return code, json.loads(capsys.readouterr().out), calls


@pytest.mark.parametrize("command", sorted(ARGV))
def test_over_cap_text_is_refused_before_any_call(monkeypatch, capsys, command):
    flag, cap = s.COMMENT_CAPS[command]
    code, out, calls = _run(monkeypatch, capsys, command, cap + 1)
    assert code == 0 and out["refused"] is True and calls == []
    assert f"--{flag}" in out["reason"] and f"{cap:,}" in out["reason"]


@pytest.mark.parametrize("command", sorted(ARGV))
def test_text_at_the_cap_runs(monkeypatch, capsys, command):
    _, cap = s.COMMENT_CAPS[command]
    code, out, calls = _run(monkeypatch, capsys, command, cap)
    assert code == 0 and "refused" not in out and calls == [1]


def test_caps_follow_the_contract():
    assert s.COMMENT_CAPS["record-pr-review"][1] == 6_000
    assert s.COMMENT_CAPS["open-gate"][1] == 2_000


def test_pr_body_is_uncapped(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: "GH")
    monkeypatch.setattr(s, "cmd_open_dev_pr", lambda *a: {"ok": True})
    assert s.main(["open-dev-pr", "5", "--title", "t", "--body", "b" * 50_000,
                   "--summary", "s"]) == 0
    assert "refused" not in json.loads(capsys.readouterr().out)
