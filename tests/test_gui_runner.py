"""Tests for ``spar.gui.runner`` (SparRunner) + the toolbar state machine.

Skipped entirely on interpreters without the optional ``gui`` extra.

Two layers:

* ``derive_state`` — a *pure* function, exercised as an exhaustive truth
  table (no Qt, no I/O);
* ``SparRunner`` — driven against a *fake* spar script (a tmp Python file
  that records its ``argv``/``cwd``, optionally traps SIGINT, and exits with a
  scripted code) so spawn/stop/resume/remarks/archival/lock behavior is
  observed end-to-end without touching real AI CLIs.

Integration tests deliberately run with ``cwd != project_dir`` to prove the
runner never relies on the process working directory (review #1/#2).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from spar.gui import toolbar as tb
from spar.gui.runner import RunnerState, SparRunner, derive_state


# ----------------------------------------------------------------------
# derive_state — pure truth table
# ----------------------------------------------------------------------
_PENDING = {"pending_gate": {"kind": "consensus"}}


@pytest.mark.parametrize(
    "alive, exit_code, status, expected",
    [
        # alive
        (True, None, {}, RunnerState.RUNNING),
        (True, None, _PENDING, RunnerState.GATE_PENDING),
        # exit-code driven (not alive)
        (False, 10, {}, RunnerState.GATE_PENDING),
        (False, None, _PENDING, RunnerState.GATE_PENDING),
        (False, 130, {}, RunnerState.RESUMABLE),
        (False, 4, {}, RunnerState.RESUMABLE),
        (False, -1, {}, RunnerState.RESUMABLE),  # crash sentinel
        (False, 5, {}, RunnerState.ABORTED),
        (False, 0, {"phase": "done"}, RunnerState.DONE),
        (False, 0, {"phase": "debate"}, RunnerState.DONE),
        (False, 2, {}, RunnerState.ERROR),
        (False, 3, {}, RunnerState.LOCKED),
        # no exit this session — derive from persisted status
        (False, None, {"phase": None}, RunnerState.IDLE),
        (False, None, {"phase": "done"}, RunnerState.DONE),
        (False, None, {"phase": "debate"}, RunnerState.RESUMABLE),
    ],
)
def test_derive_state_truth_table(alive, exit_code, status, expected):
    assert derive_state(alive, exit_code, status) is expected


# ----------------------------------------------------------------------
# toolbar enablement state machine
# ----------------------------------------------------------------------
def test_enablement_idle_only_new_debate():
    e = tb.enablement_for(RunnerState.IDLE)
    assert e[tb.NEW_DEBATE] is True
    assert e[tb.START_EXEC] is False
    assert e[tb.RESUME] is False
    assert e[tb.STOP] is False


def test_enablement_running_only_stop():
    e = tb.enablement_for(RunnerState.RUNNING)
    assert e[tb.STOP] is True
    assert e[tb.NEW_DEBATE] is False
    assert e[tb.RESUME] is False


def test_enablement_gate_pending_resume_and_new():
    e = tb.enablement_for(RunnerState.GATE_PENDING)
    assert e[tb.RESUME] is True and e[tb.NEW_DEBATE] is True
    assert e[tb.STOP] is False


def test_enablement_aborted_resume_and_new():
    e = tb.enablement_for(RunnerState.ABORTED)
    assert e[tb.RESUME] is True and e[tb.NEW_DEBATE] is True


def test_enablement_done_debate_phase_offers_start_exec():
    e = tb.enablement_for(RunnerState.DONE, {"phase": "debate"})
    assert e[tb.START_EXEC] is True and e[tb.NEW_DEBATE] is True


def test_enablement_done_exec_phase_no_start_exec():
    e = tb.enablement_for(RunnerState.DONE, {"phase": "done"})
    assert e[tb.START_EXEC] is False and e[tb.NEW_DEBATE] is True


def test_enablement_locked_all_off():
    e = tb.enablement_for(RunnerState.LOCKED)
    assert not any(e.values())


# ----------------------------------------------------------------------
# Fakes / fixtures for integration
# ----------------------------------------------------------------------
_FAKE_TEMPLATE = textwrap.dedent(
    '''\
    import json, os, sys, signal, time

    RECORD = {record!r}
    EXIT_CODE = {exit_code}
    SLEEP = {sleep}
    SIGINT_MARKER = {sigint_marker!r}


    def _record():
        argv = sys.argv[1:]
        entry = {{"argv": argv, "cwd": os.getcwd()}}
        if "--task-file" in argv:
            p = argv[argv.index("--task-file") + 1]
            try:
                entry["task_content"] = open(p).read()
            except OSError:
                entry["task_content"] = None
        if "--gate" in argv:
            val = argv[argv.index("--gate") + 1]
            if val.startswith("remarks:"):
                rp = val.split("remarks:", 1)[1]
                entry["remarks_path"] = rp
                try:
                    entry["remarks_lines"] = open(rp).read().splitlines()
                except OSError:
                    entry["remarks_lines"] = None
        with open(RECORD, "a") as f:
            f.write(json.dumps(entry) + "\\n")


    if SLEEP:
        def _handler(signum, frame):
            if SIGINT_MARKER:
                with open(SIGINT_MARKER, "w") as f:
                    f.write("sigint")
            os._exit(130)

        signal.signal(signal.SIGINT, _handler)
        _record()
        for _ in range(600):
            time.sleep(0.05)
        sys.exit(0)
    else:
        _record()
        sys.exit(EXIT_CODE)
    '''
)


def _make_fake(tmp_path, record, exit_code=0, sleep=False, sigint_marker=None):
    script = tmp_path / f"fake_spar_{int(time.time() * 1e6)}.py"
    script.write_text(
        _FAKE_TEMPLATE.format(
            record=str(record),
            exit_code=exit_code,
            sleep=bool(sleep),
            sigint_marker=str(sigint_marker) if sigint_marker else None,
        ),
        encoding="utf-8",
    )
    return script


def _read_records(record: Path):
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


_EXEC_JSON = {
    "phase": "execution",
    "target_branch": None,
    "target_base_oid": None,
    "integration_branch": None,
    "tasks": {},
    "turn_in_progress": None,
    "fix_tasks_opened": 0,
    "pending_gate": None,
}


def _write_exec_json(spar_dir: Path, phase: str):
    spar_dir.mkdir(parents=True, exist_ok=True)
    data = dict(_EXEC_JSON, phase=phase)
    (spar_dir / "exec.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def project_dir(tmp_path, monkeypatch):
    """A project dir with cwd deliberately pointed *elsewhere*."""
    proj = tmp_path / "proj"
    (proj / ".spar").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return proj


@pytest.fixture()
def runner(project_dir, qtbot, tmp_path):
    r = SparRunner(project_dir)
    return r


def _use_fake(runner, fake):
    runner._base_cmd = [sys.executable, str(fake)]


# ----------------------------------------------------------------------
# SparRunner integration
# ----------------------------------------------------------------------
def test_start_debate_spawns_with_correct_argv_and_cwd(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))

    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.start_debate("do the thing", "claude,codex", "claude", True)

    entries = _read_records(record)
    assert len(entries) == 1
    argv = entries[0]["argv"]
    assert "--task-file" in argv
    assert argv[argv.index("--sides") + 1] == "claude,codex"
    assert argv[argv.index("--first") + 1] == "claude"
    assert "--headless" in argv and "--quiet" in argv
    assert "--tasks" in argv
    # cwd was project_dir, not the test's cwd (which is 'elsewhere')
    assert os.path.samefile(entries[0]["cwd"], project_dir)
    assert entries[0]["task_content"] == "do the thing"
    # task temp file is reclaimed after finish
    assert runner._task_file_path is None


def test_start_debate_no_tasks_flag_omitted(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.start_debate("x", "claude,codex", "claude", False)
    assert "--tasks" not in _read_records(record)[0]["argv"]


def test_started_signal_carries_command(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.started, timeout=10000) as blocker:
        runner.start_debate("x", "claude,codex", "claude", True)
    assert "--headless" in blocker.args[0]
    qtbot.waitSignal(runner.finished, timeout=10000).wait()


def test_stop_delivers_sigint(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    marker = tmp_path / "sigint.marker"
    _use_fake(runner, _make_fake(tmp_path, record, sleep=True, sigint_marker=marker))

    runner.start_debate("x", "claude,codex", "claude", True)
    # The fake writes its record only after installing the SIGINT handler.
    qtbot.waitUntil(lambda: record.exists(), timeout=10000)

    runner.stop()

    assert marker.exists()
    assert marker.read_text() == "sigint"
    assert runner._last_exit == 130


def test_resume_debate_argv(runner, project_dir, tmp_path, qtbot):
    # No state files -> phase None -> `spar --continue`.
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.resume(None)
    argv = _read_records(record)[0]["argv"]
    assert argv[:1] == ["--continue"]
    assert "--gate" not in argv
    assert "--headless" in argv and "--quiet" in argv


def test_resume_exec_argv_with_gate(runner, project_dir, tmp_path, qtbot):
    _write_exec_json(project_dir / ".spar", "execution")
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.resume("accept")
    argv = _read_records(record)[0]["argv"]
    assert argv[:2] == ["exec", "--continue"]
    assert argv[argv.index("--gate") + 1] == "accept"


def test_resume_with_remarks_writes_two_line_file_and_unlinks(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.resume_with_remarks("line1\nline2")

    entry = _read_records(record)[0]
    # child saw --gate remarks:<path> and the file had exactly two lines
    assert entry["remarks_lines"] == ["line1", "line2"]
    remarks_path = Path(entry["remarks_path"])
    # file existed while child ran, gone after finished
    assert not remarks_path.exists()
    assert runner._remarks_path is None


def test_start_debate_archives_stale_done_exec_json(runner, project_dir, tmp_path, qtbot):
    spar_dir = project_dir / ".spar"
    _write_exec_json(spar_dir, "done")
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))

    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.start_debate("fresh start", "claude,codex", "claude", True)

    assert not (spar_dir / "exec.json").exists()
    archived = list(spar_dir.glob("exec.json.prev-*"))
    assert len(archived) == 1


def test_auto_exec_chains_on_exit_0(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))

    finishes = []
    runner.finished.connect(lambda code: finishes.append(code))
    runner.resume("accept", auto_exec=True)
    qtbot.waitUntil(lambda: len(finishes) >= 2, timeout=15000)

    entries = _read_records(record)
    assert len(entries) == 2
    # second spawn is the chained exec run
    assert entries[1]["argv"][:1] == ["exec"]
    assert "--headless" in entries[1]["argv"] and "--quiet" in entries[1]["argv"]


def test_no_auto_exec_does_not_chain(runner, project_dir, tmp_path, qtbot):
    record = tmp_path / "rec.jsonl"
    _use_fake(runner, _make_fake(tmp_path, record, exit_code=0))
    with qtbot.waitSignal(runner.finished, timeout=10000):
        runner.resume("accept", auto_exec=False)
    # give any (erroneous) chained spawn a chance to appear
    qtbot.wait(300)
    assert len(_read_records(record)) == 1


# ----------------------------------------------------------------------
# Lock probe (review #2)
# ----------------------------------------------------------------------
_LOCK_HOLDER = textwrap.dedent(
    """\
    import fcntl, os, sys, time
    lock_path = sys.argv[1]
    ready = sys.argv[2]
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(ready, "w") as f:
        f.write("held")
    time.sleep(30)
    """
)


def test_probe_lock_detects_held_lock_on_project_dir(runner, project_dir, tmp_path):
    spar_dir = project_dir / ".spar"
    lock_path = spar_dir / "lock"
    lock_path.touch()

    # A decoy, *unheld* lock in the current working dir: if the runner probed
    # cwd-relative it would report "free" — it must probe project_dir instead.
    cwd_spar = Path.cwd() / ".spar"
    cwd_spar.mkdir(parents=True, exist_ok=True)
    (cwd_spar / "lock").touch()

    helper = tmp_path / "holder.py"
    helper.write_text(_LOCK_HOLDER, encoding="utf-8")
    ready = tmp_path / "held.marker"

    proc = subprocess.Popen([sys.executable, str(helper), str(lock_path), str(ready)])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.02)
        assert ready.exists(), "lock holder never acquired"
        assert runner.probe_lock() is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # Once the holder is gone, the lock is free again.
    assert runner.probe_lock() is False


def test_probe_lock_false_when_no_lock_file(runner, project_dir):
    # Fresh project: lock file absent -> not held.
    (project_dir / ".spar" / "lock").unlink(missing_ok=True)
    assert runner.probe_lock() is False


def test_current_state_locked_when_lock_held(runner, project_dir, tmp_path):
    lock_path = project_dir / ".spar" / "lock"
    lock_path.touch()
    helper = tmp_path / "holder.py"
    helper.write_text(_LOCK_HOLDER, encoding="utf-8")
    ready = tmp_path / "held.marker"
    proc = subprocess.Popen([sys.executable, str(helper), str(lock_path), str(ready)])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.02)
        assert runner.current_state() is RunnerState.LOCKED
    finally:
        proc.terminate()
        proc.wait(timeout=5)
