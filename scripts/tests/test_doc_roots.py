"""Per-product doc roots (`docRoots`): one repo hosting two products whose sdlc docs live in
different trees. Every unit resolves its root from its own Initiative/Epic ancestry, so two
runs (one per product) can be live at once without flipping the global `docRoot`."""
import pytest

import sdlc_next as s
from tests.test_sdlc_next import ScriptedRunner

BOOKSHAW = "apps/bookshaw/bookshaw-docs/sdlc"
TIJORI = "apps/tijori/tijori-docs/sdlc"
TIJORI_REQ = "apps/tijori/tijori-docs/requirements"

# number -> (parent, labels, issueType)
ISSUES = {
    1994: (None, [], "Initiative"),       # Tijori Initiative (matched by number)
    2000: (1994, [], "Task"),             # its Product-Roadmap Task
    2010: (1994, [], "Epic"),             # an Epic cut from it
    2011: (2010, [], "Task"),             # that Epic's functional Task
    2012: (2010, [], "Task"),             # that Epic's LLD-phase Task
    2100: (None, ["product:tijori"], "Epic"),   # engineering-driven Tijori Epic (by label)
    2101: (2100, [], "Task"),
    1975: (None, [], "Initiative"),       # Bookshaw Initiative
    1980: (1975, [], "Task"),             # its Product-Roadmap Task
    1966: (None, [], "Epic"),             # Bookshaw Epic
    1970: (1966, [], "Task"),
    1500: (None, ["epic:standing"], "Epic"),   # Bookshaw standing epic
    1501: (1500, [], "Task"),
    1600: (None, [], "Task"),             # parentless issue
}


class AncestryGh:
    """issue_epic_info over ISSUES, counting calls."""

    def __init__(self):
        self.calls = []

    def issue_epic_info(self, number):
        self.calls.append(number)
        parent, labels, kind = ISSUES[number]
        return {"issueType": {"name": kind},
                "parent": {"number": parent} if parent else None,
                "labels": [{"name": l} for l in labels]}

    def issue_list(self):
        return [{"number": n, "state": "OPEN", **self.issue_epic_info(n)} for n in ISSUES]


def unreadable(argv):
    raise s.GhError(f"command failed (128): {' '.join(argv)}")


@pytest.fixture
def two_products(monkeypatch):
    """Bookshaw is the top-level docRoot; Tijori is a `docRoots` entry."""
    gh = AncestryGh()
    monkeypatch.setattr(s, "DOC_ROOT", BOOKSHAW)
    monkeypatch.setitem(s.CONFIG, "docRoots", [
        {"name": "tijori", "match": {"issues": [1994]},
         "docRoot": TIJORI, "requirementsDir": TIJORI_REQ},
        {"name": "tijori-label", "match": {"label": "product:tijori"}, "docRoot": TIJORI},
    ])
    monkeypatch.setattr(s, "get_work_item_provider", lambda *a, **k: gh)
    return gh


@pytest.fixture
def single_root(monkeypatch):
    """Only a top-level docRoot, and no GitHub reachable: nothing may look up ancestry."""
    monkeypatch.delitem(s.CONFIG, "docRoots", raising=False)

    def no_provider(*a, **k):
        raise AssertionError("a single-docRoot config must never look up issue ancestry")
    monkeypatch.setattr(s, "get_work_item_provider", no_provider)


LLD = "# lld.md\n\n## Task #2011: parse\nDesign.\n\n## Footprint\n- `a.ts`\n"


# --- (a) an Epic's lld-section reads its own product's tree ---

@pytest.mark.parametrize("epic,root", [(2010, TIJORI), (1966, BOOKSHAW), (2100, TIJORI)])
def test_lld_section_reads_the_epics_own_doc_root(two_products, capsys, epic, root):
    argv = ("git", "-C", "/repo", "show", f"origin/epic-{epic}:{root}/epic-{epic}/lld.md")
    result = s.cmd_lld_section("/repo", epic, 2011, runner=ScriptedRunner({argv: LLD}))
    assert result["ok"] is True
    assert capsys.readouterr().out.startswith("## Task #2011: parse")


# --- (b) a Product-Roadmap Task's product.md dir ---

@pytest.mark.parametrize("issue,expected", [
    (2000, f"{TIJORI}/issue-2000"),       # Tijori Initiative's Product-Roadmap Task
    (1980, f"{BOOKSHAW}/issue-1980"),     # Bookshaw Initiative's Product-Roadmap Task
])
def test_product_roadmap_task_doc_dir_follows_its_initiative(two_products, issue, expected):
    assert s.design_doc_dir(None, issue) == expected


def test_epic_phase_task_doc_dir_follows_its_epic(two_products):
    assert s.design_doc_dir(2010, 2012) == f"{TIJORI}/epic-2010"
    assert s.design_doc_dir(1966, 1970) == f"{BOOKSHAW}/epic-1966"


# --- (c) the footprint design prefix ---

@pytest.mark.parametrize("issue,doc", [
    (2011, f"{TIJORI}/epic-2010/lld.md"),
    (1970, f"{BOOKSHAW}/epic-1966/lld.md"),
])
def test_dev_scope_refuses_the_units_own_product_epic_doc(two_products, issue, doc):
    offenders = s.dev_pr_scope_offenders(two_products, issue, [doc], repo_path="/repo",
                                         runner=unreadable)
    assert offenders["design_docs"] == [doc]


# --- positive control: a config with only top-level docRoot is unchanged ---

def test_single_doc_root_config_is_unchanged(single_root, capsys):
    root = s.DOC_ROOT
    argv = ("git", "-C", "/repo", "show", f"origin/epic-430:{root}/epic-430/lld.md")
    assert s.cmd_lld_section("/repo", 430, 2011, runner=ScriptedRunner({argv: LLD}))["ok"]
    capsys.readouterr()
    assert s.design_doc_dir(None, 5) == f"{root}/issue-5"
    assert s.design_doc_dir(7, 8) == f"{root}/epic-7"
    doc = f"{root}/epic-7/lld.md"
    offenders = s.dev_pr_scope_offenders(object(), 8, [doc], repo_path="/repo",
                                         runner=ScriptedRunner({}))
    assert offenders["design_docs"] == [doc]


# --- every unit kind resolves, and the hint file the SubagentStart hook reads ---

@pytest.mark.parametrize("issue,root,req", [
    (1994, TIJORI, TIJORI_REQ),          # the Initiative itself
    (2011, TIJORI, TIJORI_REQ),          # functional Task -> Epic -> Initiative
    (2101, TIJORI, None),                # Task of a label-matched Epic (requirementsDir default)
    (1501, BOOKSHAW, None),              # standing-epic child
    (1600, BOOKSHAW, None),              # parentless issue
])
def test_doc_root_cmd_resolves_every_unit_kind(two_products, issue, root, req):
    out = s.cmd_doc_root(None, issue)
    assert out["docRoot"] == root and out["per_product"] is True
    assert out["requirementsDir"] == (req or s.CONFIG.get("requirementsDir"))
    # No product tree has its own templates dir here, so each falls back to the top-level one.
    assert out["docTemplates"] == f"{BOOKSHAW}/{s.PIPELINE['docTemplates']}"


def test_resolution_is_cached_and_hinted_for_every_ancestor(two_products, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("SDLC_RUNS_DIR", str(tmp_path))
    assert s.doc_root_for(2011) == TIJORI
    assert two_products.calls == [2011, 2010, 1994]
    assert s.doc_root_for(2010) == TIJORI and s.doc_root_for(1994) == TIJORI
    assert two_products.calls == [2011, 2010, 1994]      # served from the cache
    hints = json.loads((tmp_path / s.DOC_ROOT_HINTS_FILE).read_text())
    assert {k: v["docRoot"] for k, v in hints.items()} == {
        "2011": TIJORI, "2010": TIJORI, "1994": TIJORI}


def test_doc_paths_and_dev_scope_cover_every_product(two_products):
    assert s.all_doc_roots() == [BOOKSHAW, TIJORI]
    assert s._is_doc_path(f"{TIJORI}/issue-2000/notes.txt")
    assert s._is_doc_path(f"{BOOKSHAW}/issue-1980/notes.txt")
    # A Bookshaw development diff may not edit a Tijori Epic doc either.
    doc = f"{TIJORI}/epic-2010/lld.md"
    assert s.dev_pr_scope_offenders(two_products, 1970, [doc], repo_path="/repo",
                                    runner=unreadable)["design_docs"] == [doc]


def test_show_config_and_evidence_globs_name_every_root(tmp_path):
    import json, os, subprocess, sys
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parents[2] / "sdlc.config.sample.json").read_text())
    cfg["docRoot"] = BOOKSHAW
    cfg["docRoots"] = [{"name": "tijori", "match": {"issues": [1994]}, "docRoot": TIJORI}]
    path = tmp_path / "sdlc-pipeline.config.json"
    path.write_text(json.dumps(cfg))
    env = {**os.environ, "SDLC_CONFIG": str(path), "GITHUB_TOKEN": "x"}
    script = str(Path(__file__).resolve().parents[1] / "sdlc_next.py")
    shown = json.loads(subprocess.check_output([sys.executable, script, "show-config"],
                                               env=env, text=True))
    assert shown["docRoot"] == BOOKSHAW and shown["docRoots"] == cfg["docRoots"]
    globs = subprocess.check_output(
        [sys.executable, "-c", "import sdlc_next as s; print('\\n'.join(s.EVIDENCE_CARRY_FORWARD_GLOBS))"],
        env=env, text=True, cwd=str(Path(script).parent)).split()
    assert f"{BOOKSHAW}/**" in globs and f"{TIJORI}/**" in globs and globs.count("**/*.md") == 1
