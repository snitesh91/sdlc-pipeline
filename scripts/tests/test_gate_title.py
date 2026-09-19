"""open-gate composes a phase-Task's PR title from its parent's title."""
import pytest

import sdlc_next as s
from tests.test_composites import Gh


def _tree(*tasks, epic_labels=("type:epic",)):
    return Gh([{"number": 1, "labels": ["type:initiative"], "title": "Object storage"},
               {"number": 9, "labels": list(epic_labels), "parent": 1, "title": "Image uploads"},
               *tasks])


def _title(gh, n, title):
    return s.phase_gate_title(gh, n, title)


def test_a_product_roadmap_task_carries_its_initiative_title():
    gh = _tree({"number": 2, "labels": ["type:task"], "parent": 1, "stage": "product"})
    assert _title(gh, 2, "Product Roadmap") == "Product Roadmap - Object storage"


@pytest.mark.parametrize("stage,title,expected", [
    ("architecture", "Architecture phase", "Architecture - Image uploads"),
    ("lld", "LLD phase", "LLD - Image uploads"),
    ("architecture", "Architecture revision: new port", "Architecture revision: new port - Image uploads"),
])
def test_an_epic_phase_task_carries_its_epic_title(stage, title, expected):
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": stage})
    assert _title(gh, 10, title) == expected


def test_a_title_already_naming_the_parent_is_left_alone():
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"})
    assert _title(gh, 10, "Architecture - Image uploads") == "Architecture - Image uploads"


def test_a_standing_child_gate_keeps_its_own_title():
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "architecture"},
               epic_labels=("type:epic", "epic:standing"))
    assert _title(gh, 10, "Fix the widget") == "Fix the widget"


def test_a_functional_task_and_a_parentless_issue_keep_their_titles():
    gh = _tree({"number": 10, "labels": ["type:task"], "parent": 9, "stage": "development"},
               {"number": 11, "labels": ["type:task"], "stage": "product"})
    assert _title(gh, 10, "Add uploader") == "Add uploader"
    assert _title(gh, 11, "Solo change") == "Solo change"


def test_the_cli_passes_the_composed_title_to_open_gate(monkeypatch, capsys):
    gh = _tree({"number": 2, "labels": ["type:task"], "parent": 1, "stage": "product"})
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(s, "get_work_item_provider", lambda: gh)
    monkeypatch.setattr(s, "cmd_open_gate", lambda *a, **k: seen.setdefault("args", a) and {})
    s.main(["open-gate", "2", "--title", "Product Roadmap", "--doc", "product.md",
            "--next-stage", "architecture", "--summary", "x"])
    assert seen["args"][3] == "Product Roadmap - Object storage"
