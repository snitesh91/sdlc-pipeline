"""Per-epic local stacks: ports, env profiles, provision and teardown."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from typing import Callable, Optional

from .config import PIPELINE
from .github import GhError
from .units import cloud_skips_local_cleanup


# --- Per-epic isolated runtime stack -----------------------------------------
# Each epic gets its own compose project, ports, env/secrets profile and data dir.
# Isolation is config-only: the driven repo's compose must read the keys in `pipeline.stack`.

Shell = Callable[[str, str], str]  # (command, cwd) -> stdout; raises GhError on nonzero


def _default_shell(command: str, cwd: str) -> str:
    result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed ({result.returncode}) in {cwd}: {command}\n"
                      f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result.stdout


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def stack_profile(epic: int) -> str:
    return PIPELINE["stack"]["profileTemplate"].format(n=epic)


def _stack_layout(epic: int, profile: Optional[str] = None) -> dict:
    """Every path/name the two stack commands share, derived from config once;
    `profile` overrides the epic's own (a hand-made stack)."""
    s = PIPELINE["stack"]
    profile = profile or stack_profile(epic)
    root = os.path.abspath(s["workspaceRoot"])
    fmt = {"profile": profile, "n": epic, "workspaceRoot": root}
    env_file = s["envFile"].format(**fmt)
    secrets_file = s["secretsFile"].format(**fmt)
    return {
        "profile": profile,
        "project": s["composeProjectTemplate"].format(**fmt),
        "workspace_root": root,
        "env_file": os.path.join(root, env_file),
        "secrets_file": os.path.join(root, secrets_file),
        "base_env_file": os.path.join(root, s["envFile"].format(profile=s["baseProfile"], n=epic,
                                                                 workspaceRoot=root)),
        "base_secrets_file": os.path.join(root, s["secretsFile"].format(
            profile=s["baseProfile"], n=epic, workspaceRoot=root)),
        "data_dir": s["dataDirTemplate"].format(**fmt),
        "env_file_rel": env_file,
    }


def pick_stack_ports(epic: int, probe: Callable[[int], bool] = _port_free) -> dict:
    """Host ports for this epic's stack: base ports plus a non-zero stride offset
    derived from the epic number, bumped while any port is bound. Returns {key: port}."""
    s = PIPELINE["stack"]
    stride = int(s["portStride"])
    base_ports = {k: int(v) for k, v in s["ports"].items()}
    slots = 45
    start = (epic % slots) + 1
    for i in range(slots):
        offset = stride * (((start + i - 1) % slots) + 1)
        candidate = {k: v + offset for k, v in base_ports.items()}
        if all(probe(p) for p in candidate.values()):
            return candidate
    raise GhError(f"no free port set found for epic #{epic} across {slots} stride slots "
                  f"(stride {stride}); tear down stale epic stacks first")


_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def render_env_profile(base_text: str, overrides: dict) -> str:
    """The base env text with each `overrides` key replaced in place; missing keys
    are appended under a generated-section marker."""
    seen = set()
    out = []
    for line in base_text.splitlines():
        m = _ENV_LINE_RE.match(line)
        key = m.group(1) if m else None
        if key in overrides:
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in overrides if k not in seen]
    if missing:
        out.append("")
        out.append("# --- sdlc-pipeline per-epic isolation (generated) ---")
        out.extend(f"{k}={overrides[k]}" for k in missing)
    return "\n".join(out) + "\n"


def _read_env_ports(path: str) -> dict:
    ports = {}
    keys = set(PIPELINE["stack"]["ports"])
    with open(path) as f:
        for line in f:
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1) in keys:
                try:
                    ports[m.group(1)] = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return ports


def cmd_provision_epic_stack(epic: int, up: bool = True, shell: Shell = _default_shell,
                             probe: Callable[[int], bool] = _port_free) -> dict:
    """Generate the epic's env profile (distinct ports, compose project, data dir),
    copy the base secrets file, then run `upCommand`/`seedCommand`. Idempotent: an
    existing profile is reused; `stack.enabled: false` is a structured no-op."""
    s = PIPELINE["stack"]
    if not s["enabled"]:
        return {"epic": epic, "provisioned": False,
                "reason": "pipeline.stack.enabled is false — shared stack in use"}
    lay = _stack_layout(epic)
    created = False
    if os.path.exists(lay["env_file"]):
        ports = _read_env_ports(lay["env_file"])
    else:
        if not os.path.isfile(lay["base_env_file"]):
            raise GhError(f"base profile {lay['base_env_file']} not found -- set "
                          f"pipeline.stack.baseProfile/envFile to the repo's dev profile")
        if not os.path.isfile(lay["base_secrets_file"]):
            raise GhError(f"base secrets file {lay['base_secrets_file']} not found -- the epic "
                          f"profile is a copy of it (pipeline.stack.secretsFile)")
        ports = pick_stack_ports(epic, probe=probe)
        overrides = {"COMPOSE_PROJECT_NAME": lay["project"], **{k: str(v) for k, v in ports.items()},
                     s["dataDirKey"]: lay["data_dir"]}
        with open(lay["base_env_file"]) as f:
            base_text = f.read()
        with open(lay["env_file"], "w") as f:
            f.write(render_env_profile(base_text, overrides))
        shutil.copyfile(lay["base_secrets_file"], lay["secrets_file"])
        os.makedirs(os.path.join(lay["workspace_root"], lay["data_dir"]), exist_ok=True)
        created = True
    fmt = {"profile": lay["profile"], "project": lay["project"], "envFile": lay["env_file_rel"],
           "secretsFile": os.path.relpath(lay["secrets_file"], lay["workspace_root"]),
           "dataDir": lay["data_dir"], "workspaceRoot": lay["workspace_root"], "n": epic}
    ran = []
    if up:
        for key in ("upCommand", "seedCommand"):
            cmd = (s.get(key) or "").format(**fmt)
            if cmd.strip():
                shell(cmd, lay["workspace_root"])
                ran.append(cmd)
    return {"epic": epic, "provisioned": True, "created": created, "profile": lay["profile"],
            "project": lay["project"], "env_file": lay["env_file"],
            "secrets_file": lay["secrets_file"], "data_dir": lay["data_dir"], "ports": ports,
            "commands_run": ran,
            "use": f"COMPOSE_PROJECT_NAME={lay['project']} make <target> PROFILE={lay['profile']}"}


def _docker_compose_running(project: str) -> list:
    """Ids of running containers carrying compose's project label (none without docker)."""
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter",
                              f"label=com.docker.compose.project={project}"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        return []
    return out.stdout.split()


def cmd_teardown_epic_stack(epic: int, keep_data: bool = False,
                            shell: Shell = _default_shell,
                            project: Optional[str] = None, profile: Optional[str] = None,
                            running_probe: Callable[[str], list] = _docker_compose_running) -> dict:
    """Run `downCommand`, confirm no container of the project still runs, then remove
    the generated env/secrets files and data dir. A failing or no-op down removes
    nothing. `project`/`profile` name a hand-made stack, even with the stack disabled."""
    s = PIPELINE["stack"]
    named = bool(project or profile)
    if not s["enabled"] and not named:
        return {"epic": epic, "torn_down": False,
                "reason": "pipeline.stack.enabled is false — pass --project/--profile "
                          "to tear down a hand-made stack"}
    lay = _stack_layout(epic, profile)
    if project:
        lay["project"] = project
    if not named and not os.path.exists(lay["env_file"]):
        return {"epic": epic, "torn_down": False, "profile": lay["profile"],
                "reason": f"no stack provisioned ({lay['env_file']} absent)"}
    fmt = {"profile": lay["profile"], "project": lay["project"], "envFile": lay["env_file_rel"],
           "secretsFile": os.path.relpath(lay["secrets_file"], lay["workspace_root"]),
           "dataDir": lay["data_dir"], "workspaceRoot": lay["workspace_root"], "n": epic}
    cmd = (s.get("downCommand") or "").format(**fmt)
    if cmd.strip():
        shell(cmd, lay["workspace_root"])
    still_running = running_probe(lay["project"])
    if still_running:
        return {"epic": epic, "torn_down": False, "profile": lay["profile"],
                "project": lay["project"], "down_command": cmd, "still_running": still_running,
                "reason": f"{len(still_running)} container(s) of {lay['project']} still run after "
                          f"the down command; nothing removed (does --env-file resolve from "
                          f"{lay['workspace_root']}?)"}
    removed = []
    for p in (lay["env_file"], lay["secrets_file"]):
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    data_abs = os.path.join(lay["workspace_root"], lay["data_dir"])
    data_skipped = cloud_skips_local_cleanup()
    if not keep_data and not data_skipped and os.path.isdir(data_abs):
        shutil.rmtree(data_abs, ignore_errors=True)
        removed.append(data_abs)
    return {"epic": epic, "torn_down": True, "profile": lay["profile"], "project": lay["project"],
            "down_command": cmd, "removed": removed,
            **({"data_kept": "cloud"} if data_skipped and not keep_data else {})}


# Load the later modules in order, then bind the names this one uses from them (see sdlc/__init__.py).
from . import composites  # noqa: E402,F401
