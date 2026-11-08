"""2026-11-09 fixes: a cloud Task PR into main runs its CI in session; a cloud self-relaunch
that gets no session id leaves the context-reset end marker the latest."""
import sdlc_next as s
from tests.test_cloud_epics import FakeClaude, FakeGit, _initiative
from tests.test_retro_0930_composite import _OpenDevGh, _offenders
from tests.test_retro_1105_cp import _CLOUD_IT


class _MarkGh(_OpenDevGh):
    def __init__(self):
        super().__init__()
        self.bodies, self.labels = [], []

    def pr_create(self, base, head, title, body, draft):
        self.bodies.append(body)
        return 42

    def ensure_label(self, label):
        pass

    def pr_add_label(self, n, label):
        self.labels.append((n, label))


def _open(monkeypatch, attestable):
    _offenders(monkeypatch)
    monkeypatch.setattr(s, "integration_base", lambda gh, n: "main")
    monkeypatch.setattr(s, "required_workflows",
                        lambda base=None: ({**_CLOUD_IT, "attestable": attestable},))
    gh = _MarkGh()
    return gh, s.cmd_open_dev_pr(gh, 10, "t", "Body", "s")


def test_a_cloud_task_pr_into_main_carries_the_ci_local_marker(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    gh, result = _open(monkeypatch, "cloud")
    assert gh.bodies[0].endswith(f"\n\n{s.CI_LOCAL_MARKER}") and result["ci_local"] is True
    assert gh.labels == [(42, "ci:local")]


def test_a_laptop_or_non_cloud_suite_task_pr_has_no_marker(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    gh, result = _open(monkeypatch, "cloud")
    assert s.CI_LOCAL_MARKER not in gh.bodies[0] and gh.labels == [] and "ci_local" not in result
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    gh, _ = _open(monkeypatch, True)
    assert s.CI_LOCAL_MARKER not in gh.bodies[0]


def test_a_cloud_relaunch_with_no_session_id_posts_no_marker(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    gh = _initiative(2)
    result = s.cmd_launch_cloud_epic(gh, 2, runner=FakeGit(), claude=FakeClaude("Login required\n"))
    assert result["ok"] is False and "Login required" in result["output_tail"]
    assert not any("sdlc:cloud-session " in c for c in gh.comments_on(2))


def test_a_reused_cloud_task_pr_into_main_gets_the_marker(monkeypatch):
    """Review: a PR opened before (laptop, another path) returned before the marker check."""
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    _offenders(monkeypatch)
    monkeypatch.setattr(s, "integration_base", lambda gh, n: "main")
    monkeypatch.setattr(s, "required_workflows", lambda base=None: (_CLOUD_IT,))
    monkeypatch.setattr(s, "_dev_pr_comment_posted", lambda gh, n, pr: True)
    gh = _MarkGh()
    gh.pr_list_for_branch = lambda branch, state="open": [{"number": 7, "isDraft": True}]
    edits = []
    gh.pr_view = lambda pr, fields: {"body": "Old"}
    gh.pr_edit_body = lambda pr, body: edits.append((pr, body))
    result = s.cmd_open_dev_pr(gh, 10, "t", "Body", "s")
    assert result["ci_local"] is True and edits == [(7, f"Old\n\n{s.CI_LOCAL_MARKER}")]
    assert gh.labels == [(7, "ci:local")]
