"""Unit tests for spar.envprobe — the debate-time tooling probe."""

from __future__ import annotations

import spar.envprobe as envprobe
from spar.envprobe import PROBED_TOOLS, probe_environment


def _no_versions(monkeypatch):
    monkeypatch.setattr(envprobe, "_version_of", lambda tool: None)


def test_all_tools_available(monkeypatch):
    _no_versions(monkeypatch)
    monkeypatch.setattr(envprobe.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    report = probe_environment()
    assert report.startswith("Available on this machine: ")
    assert "NOT available" not in report
    for tool in PROBED_TOOLS:
        assert tool in report


def test_all_tools_missing(monkeypatch):
    _no_versions(monkeypatch)
    monkeypatch.setattr(envprobe.shutil, "which", lambda tool: None)
    report = probe_environment()
    lines = report.splitlines()
    assert lines[0] == "Available on this machine: (none of the probed tools)"
    assert lines[1] == "NOT available: " + ", ".join(PROBED_TOOLS)


def test_mixed_availability_and_format(monkeypatch):
    _no_versions(monkeypatch)
    present = {"python3", "git", "gcc", "make", "bash"}
    monkeypatch.setattr(
        envprobe.shutil,
        "which",
        lambda tool: f"/usr/bin/{tool}" if tool in present else None,
    )
    report = probe_environment()
    lines = report.splitlines()
    assert lines[0] == "Available on this machine: python3, gcc, make, git, bash"
    assert lines[1] == (
        "NOT available: python, pip3, node, npm, g++, cmake, cargo, rustc, "
        "go, java, pytest"
    )
    assert len(lines) <= 10


def test_ordering_is_deterministic_probe_list_order(monkeypatch):
    _no_versions(monkeypatch)
    present = {"bash", "python3", "node"}  # deliberately unsorted set
    monkeypatch.setattr(
        envprobe.shutil,
        "which",
        lambda tool: "/bin/x" if tool in present else None,
    )
    first = probe_environment()
    second = probe_environment()
    assert first == second
    # PROBED_TOOLS order, not alphabetical or set order.
    assert first.splitlines()[0] == "Available on this machine: python3, node, bash"


def test_version_shown_when_fetchable(monkeypatch):
    monkeypatch.setattr(
        envprobe.shutil,
        "which",
        lambda tool: "/usr/bin/python3" if tool == "python3" else None,
    )
    monkeypatch.setattr(
        envprobe, "_version_of", lambda tool: "3.11.4" if tool == "python3" else None
    )
    report = probe_environment()
    assert "python3 (3.11.4)" in report


def test_version_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(
        envprobe.shutil,
        "which",
        lambda tool: "/usr/bin/git" if tool == "git" else None,
    )
    monkeypatch.setattr(envprobe, "_version_of", lambda tool: None)
    report = probe_environment()
    assert "git" in report
    assert "(" not in report.splitlines()[0]


def test_version_of_swallows_subprocess_errors(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no such tool")

    monkeypatch.setattr(envprobe.subprocess, "run", boom)
    assert envprobe._version_of("python3") is None


def test_probed_tools_cover_required_list():
    for tool in (
        "python3", "python", "pip3", "node", "npm", "gcc", "g++", "make",
        "cmake", "cargo", "rustc", "go", "java", "pytest", "git", "bash",
    ):
        assert tool in PROBED_TOOLS
