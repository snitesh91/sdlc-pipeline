"""Gate feedback evaluation."""
from __future__ import annotations


from .github import GhError, GitHub
from .evidence import _GATE_CUTOFF_MARKER, find_gate_pr


def find_gate_comments_cutoff(pr: dict) -> str:
    cutoff = pr["createdAt"]
    for c in pr.get("comments", []):
        m = _GATE_CUTOFF_MARKER.search(c.get("body", ""))
        if m:
            cutoff = c["createdAt"]
    return cutoff


def new_plain_comments(pr: dict, cutoff: str) -> list:
    return [c for c in pr.get("comments", []) if c["createdAt"] > cutoff]


def unresolved_review_threads(gh: GitHub, pr_number: int) -> list:
    return gh.unresolved_review_threads(pr_number)


def evaluate_gate(gh: GitHub, issue_number: int) -> dict:
    issue = gh.issue_view(issue_number)
    found = find_gate_pr(issue["comments"])
    if not found:
        raise GhError(f"issue #{issue_number} has Pipeline Status = awaiting-human-review but no gate-pr marker")
    stage, pr_number = found
    pr_state = gh.pr_view(pr_number, fields="state,mergedAt")
    if pr_state["state"] == "MERGED":
        return {"gate_pr": pr_number, "stage": stage, "status": "satisfied",
                "unresolved_threads": [], "new_comments": []}
    threads = unresolved_review_threads(gh, pr_number)
    pr = gh.pr_view(pr_number, fields="comments,createdAt")
    cutoff = find_gate_comments_cutoff(pr)
    comments = new_plain_comments(pr, cutoff)
    status = "feedback_pending" if (threads or comments) else "not_satisfied"
    return {"gate_pr": pr_number, "stage": stage, "status": status,
            "unresolved_threads": threads, "new_comments": comments}


def cmd_check_gate(gh: GitHub, args) -> dict:
    return evaluate_gate(gh, args.issue)


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import run_state  # noqa: E402,F401
