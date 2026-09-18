"""Stop hook for developing this repo: run the suite whose sources have uncommitted changes."""
import json
import subprocess
import sys

# prefix -> (cwd relative to the repo root, command)
SUITES = {"scripts/": ("scripts", ["python3", "-m", "pytest", "-q", "-x"]),
          "hooks/": (".", ["python3", "-m", "pytest", "-q", "-x", "hooks/tests"])}


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return 0  # not a Stop payload, or already blocked once this turn; don't loop
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True).stdout.strip()
    if not root:
        return 0
    status = subprocess.run(["git", "status", "--porcelain", "--", *SUITES],
                            capture_output=True, text=True, cwd=root).stdout
    for prefix, (cwd, cmd) in SUITES.items():
        if prefix not in status:
            continue
        run = subprocess.run(cmd, capture_output=True, text=True, cwd=f"{root}/{cwd}")
        if run.returncode != 0:
            tail = "\n".join((run.stdout + run.stderr).splitlines()[-30:])
            print(f"{prefix} has uncommitted changes and its tests fail:\n{tail}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0  # fail open: a broken check must never block a Stop
    sys.exit(code)
