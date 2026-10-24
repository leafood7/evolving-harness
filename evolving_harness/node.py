"""Resolve the Pi host runtime without depending on the launching terminal's PATH."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


def resolve_node(config_path: Path | None = None) -> str:
    config_path = config_path or Path(__file__).resolve().parents[1] / "harness.local.toml"
    configured = os.environ.get("EH_NODE")
    origin = "EH_NODE"
    if not configured and config_path.is_file():
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        section = config.get("pi", {})
        if not isinstance(section, dict):
            raise ValueError("harness.local.toml [pi] must be a table")
        configured = section.get("node")
        origin = str(config_path)
        if configured is not None and (not isinstance(configured, str) or not configured.strip()):
            raise ValueError("harness.local.toml pi.node must be a nonempty executable path")
    if configured:
        expanded = Path(os.path.expandvars(configured)).expanduser()
        if not expanded.is_absolute():
            expanded = (config_path.parent / expanded) if origin != "EH_NODE" else expanded
        executable = str(expanded.resolve()) if expanded.is_file() else None
        if not executable:
            raise ValueError(f"Node executable configured by {origin} does not exist: {configured}")
    else:
        executable = shutil.which("node")
        if not executable:
            raise ValueError("Node.js was not found. Set EH_NODE to the full node executable path, "
                             "or set [pi] node in harness.local.toml. Pi needs Node.js >=22.19.")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Cannot run Node.js at {executable}; check EH_NODE or harness.local.toml") from exc
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-[\w.-]+)?", result.stdout.strip())
    if not match or tuple(map(int, match.groups())) < (22, 19, 0):
        raise ValueError(f"Pi requires Node.js >=22.19; {executable} reported {result.stdout.strip()!r}")
    return executable
