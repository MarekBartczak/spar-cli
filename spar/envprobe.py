"""Probe the local machine for developer tooling.

The debate's ``## Tasks`` contract carries shell ``test=`` commands that are
later executed verbatim on this machine. A planner that guesses tool names
(``python`` vs ``python3``) writes tasks that can never pass. This module
builds a short, deterministic plaintext report of which common tools actually
exist, so the debate prompt can pin planners to commands that are real here.
"""

from __future__ import annotations

import re
import shutil
import subprocess

__all__ = ["PROBED_TOOLS", "probe_environment"]

# Fixed, practical probe list. Its order IS the report order (deterministic).
PROBED_TOOLS: tuple[str, ...] = (
    "python3",
    "python",
    "pip3",
    "node",
    "npm",
    "gcc",
    "g++",
    "make",
    "cmake",
    "cargo",
    "rustc",
    "go",
    "java",
    "pytest",
    "git",
    "bash",
)

# Tools whose version is worth the extra subprocess call (interpreter
# major/minor matters for test commands); everything else is name-only.
_VERSIONED: tuple[str, ...] = ("python3", "python")

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _version_of(tool: str) -> str | None:
    """Best-effort ``<tool> --version`` → short version string, or None.

    Any failure (missing binary, timeout, weird output) is swallowed — the
    probe must never break prompt construction.
    """
    try:
        proc = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    match = _VERSION_RE.search(proc.stdout or proc.stderr or "")
    return match.group(0) if match else None


def probe_environment() -> str:
    """Short plaintext report of available/missing tooling on this machine.

    Example::

        Available on this machine: python3 (3.11.4), git, gcc, make, bash
        NOT available: python, node, cargo

    Ordering follows :data:`PROBED_TOOLS`; output stays under ~10 lines.
    """
    available: list[str] = []
    missing: list[str] = []
    for tool in PROBED_TOOLS:
        if shutil.which(tool):
            version = _version_of(tool) if tool in _VERSIONED else None
            available.append(f"{tool} ({version})" if version else tool)
        else:
            missing.append(tool)
    lines = [
        "Available on this machine: "
        + (", ".join(available) if available else "(none of the probed tools)")
    ]
    if missing:
        lines.append("NOT available: " + ", ".join(missing))
    return "\n".join(lines)
