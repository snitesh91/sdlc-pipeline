"""Review 2026-11-21 (v0.4.6): SubagentStop finds a `post-comment` whose `--role` sits on a
continuation line."""
from test_hooks import _post_lines, sdlc_repo, stop_role  # noqa: F401

WRAPPED = 'python3 "$SDLC" post-comment 5 \\\n  --role {role} \\\n  --body-file /tmp/c.md'


def test_a_handoff_posted_by_a_line_wrapped_command_counts(tmp_path, sdlc_repo):
    """Regression: the stop was blocked although the comment was posted."""
    lines = _post_lines("lld", cmd=WRAPPED.format(role="lld"))
    assert stop_role(tmp_path, sdlc_repo, "lld", lines=lines).returncode == 0


def test_a_line_wrapped_post_for_another_role_still_does_not_count(tmp_path, sdlc_repo):
    """Positive control."""
    lines = _post_lines("lld", cmd=WRAPPED.format(role="product"))
    assert stop_role(tmp_path, sdlc_repo, "lld", lines=lines).returncode == 2
