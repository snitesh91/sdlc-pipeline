"""The sdlc control plane, split by concern; `scripts/sdlc_next.py` is its CLI entry point.

Modules call each other in both directions. Each one imports names from the modules
before it at its top and from the modules after it at its bottom, so they load in the
order below.
"""
from . import (
    config, github, units, evidence, epic_close, gate_eval, run_state, next_action, footprint,
    plugin_version, worktrees, lanes, handoff, design_gates, lld_tasks, gates, dev_pr, checks,
    issue_ops, park, human_channel, cloud, issue_admin, stack, composites, cli,
)

MODULES = (
    config, github, units, evidence, epic_close, gate_eval, run_state, next_action, footprint,
    plugin_version, worktrees, lanes, handoff, design_gates, lld_tasks, gates, dev_pr, checks,
    issue_ops, park, human_channel, cloud, issue_admin, stack, composites, cli,
)
