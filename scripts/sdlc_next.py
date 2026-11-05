#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""sdlc_next.py — deterministic control plane for the sdlc plugin.

Owns every mechanical GitHub/git read, decision and mutation so the orchestrating
agent calls this CLI and acts on its JSON verdict instead of hand-running gh/git.

Exit codes: 0 = a valid result (including "nothing to do"); 1 = an operational
failure (a gh/git call failed, a required marker was missing). Every command
prints one JSON object to stdout regardless of exit code.

The code lives in the `sdlc` package beside this file, one module per concern; this
file is the entry point `$SDLC` names, and the namespace tests and hooks import.
"""
from __future__ import annotations

import sys
import types

import sdlc
from sdlc import MODULES

for _m in MODULES:
    globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})


class _Namespace(types.ModuleType):
    """Rebinding a name here rebinds it in every `sdlc` module that holds it, so
    `monkeypatch.setattr(sdlc_next, name, ...)` reaches the code that reads the name."""

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            for m in MODULES:
                if name in vars(m):
                    setattr(m, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if not name.startswith("__"):
            for m in MODULES:
                if name in vars(m):
                    delattr(m, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Namespace


if __name__ == "__main__":
    sys.exit(main())
