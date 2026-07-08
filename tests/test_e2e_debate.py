"""End-to-end tests for spar-cli: Orchestrator -> real subprocess adapters ->
scripted fake CLI binaries -> verdict parsing -> state persistence.

Unlike ``tests/test_orchestrator.py`` (in-Python fake adapters, no
subprocesses) these tests build a *real* :class:`~spar.orchestrator.Orchestrator`
wired to real :class:`~spar.adapters.claude.ClaudeAdapter` /
:class:`~spar.adapters.codex.CodexAdapter` instances, whose ``command`` points
at the scripted fake binaries in ``tests/fakes/``. Every turn is an actual
subprocess invocation; the fakes are driven by a per-side "script directory"
(see the docstrings in ``fake_claude.py`` / ``fake_codex.py``) so each
scripted reply, and any artifact/foreign-file edits it makes, is fully
under test control.

No real ``claude``/``codex`` CLI is ever invoked. Scenario 6 additionally
drives the whole thing through ``python -m spar.cli`` as a subprocess, with
``PATH`` restricted to a directory containing only the fakes, to prove the
CLI wiring itself never has a path to a real AI CLI.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spar.adapters.claude import ClaudeAdapter
from spar.adapters.codex import CodexAdapter
from spar.config import DebateConfig
from spar.guard import Guard
from spar.orchestrator import GateDecision, Orchestrator
from spar.state import StateStore, check_recovery, hash_artifact
from spar.verdict import Severity

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = str(REPO_ROOT / "tests" / "fakes" / "fake_claude.py")
FAKE_CODEX = str(REPO_ROOT / "tests" / "fakes" / "fake_codex.py")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def vblock(status, resolved=(), remarks=()):
    """Build a full reply body: some prose plus a <verdict> block."""
    lines = ["Some prose from the agent.", "", "<verdict>", f"status: {status}"]
    if resolved:
        lines.append("resolved:")
        lines += [f"- {r}" for r in resolved]
    if remarks:
        lines.append("remarks:")
        lines += [f"- {r}" for r in remarks]
    lines.append("</verdict>")
    return "\n".join(lines)


class ScriptedGate:
    """A :class:`~spar.orchestrator.UserGate` driven by canned decisions."""

    def __init__(self, consensus=(), rounds=(), recovery=()):
        self.consensus = list(consensus)
        self.rounds = list(rounds)
        self.recovery = list(recovery)
        self.consensus_calls = []
        self.rounds_calls = []
        self.recovery_calls = []

    def consensus_gate(self, artifact_path, nice_backlog):
        self.consensus_calls.append((artifact_path, list(nice_backlog)))
        return self.consensus.pop(0)

    def rounds_exhausted_gate(self, artifact_path, pending):
        self.rounds_calls.append((artifact_path, list(pending)))
        return self.rounds.pop(0)

    def recovery_gate(self, artifact_path, expected_hash):
        self.recovery_calls.append((artifact_path, expected_hash))
        return self.recovery.pop(0)


def init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)


def build_env(monkeypatch, claude_dir: Path, codex_dir: Path, artifact_path: Path) -> None:
    """Point both fakes at their script directories and the shared artifact."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    codex_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT_DIR", str(claude_dir))
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT_PATH", str(artifact_path))
    monkeypatch.setenv("FAKE_CODEX_SCRIPT_DIR", str(codex_dir))
    monkeypatch.setenv("FAKE_CODEX_ARTIFACT_PATH", str(artifact_path))


def write_claude_reply(script_dir: Path, n: int, session_id: str, result_text: str) -> None:
    (script_dir / f"{n}.json").write_text(
        json.dumps({"session_id": session_id, "result": result_text}), encoding="utf-8"
    )


def write_claude_artifact(script_dir: Path, n: int, content: str) -> None:
    (script_dir / f"{n}.artifact").write_text(content, encoding="utf-8")


def write_claude_foreign(script_dir: Path, n: int, content: str) -> None:
    (script_dir / f"{n}.foreign").write_text(content, encoding="utf-8")


def write_codex_reply(script_dir: Path, n: int, session_id: str, message_text: str) -> None:
    jsonl = (
        "\n".join(
            [
                json.dumps({"type": "session.created", "session_id": session_id}),
                json.dumps({"type": "agent_message", "message": message_text}),
            ]
        )
        + "\n"
    )
    (script_dir / f"{n}.jsonl").write_text(jsonl, encoding="utf-8")
    (script_dir / f"{n}.md").write_text(message_text, encoding="utf-8")


def write_codex_artifact(script_dir: Path, n: int, content: str) -> None:
    (script_dir / f"{n}.artifact").write_text(content, encoding="utf-8")


def write_codex_foreign(script_dir: Path, n: int, content: str) -> None:
    (script_dir / f"{n}.foreign").write_text(content, encoding="utf-8")


def read_prompts_log(script_dir: Path) -> list[list[str]]:
    """Parse a fake's ``prompts.log`` into a list of argv (one per call)."""
    path = script_dir / "prompts.log"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    calls: list[list[str]] = []
    for block in text.split("=== call ")[1:]:
        _header, _, rest = block.partition("\n")
        argv_line = rest.strip().splitlines()[0]
        calls.append(json.loads(argv_line))
    return calls


def build_orch(
    repo_dir: Path,
    gate,
    artifact_path: Path,
    max_rounds: int = 6,
    turn_timeout_sec: int = 10,
    use_guard: bool = True,
    order=("claude", "codex"),
):
    events_dir = repo_dir / ".spar" / "transcript"
    claude = ClaudeAdapter(
        command=FAKE_CLAUDE, cwd=repo_dir, events_dir=events_dir, side_name="claude"
    )
    codex = CodexAdapter(
        command=FAKE_CODEX, cwd=repo_dir, events_dir=events_dir, side_name="codex"
    )
    sides = {"claude": claude, "codex": codex}
    store = StateStore(repo_dir / ".spar")
    debate = DebateConfig(max_rounds=max_rounds, turn_timeout_sec=turn_timeout_sec)
    guard = (
        Guard(repo_dir=repo_dir, artifact_path=artifact_path, spar_dir=repo_dir / ".spar")
        if use_guard
        else None
    )
    logs: list[str] = []
    orch = Orchestrator(
        sides, list(order), store, artifact_path, debate, gate, guard=guard, log=logs.append
    )
    return orch, store, logs


# ---------------------------------------------------------------------------
# Scenario 1: happy debate to consensus
# ---------------------------------------------------------------------------


def test_happy_debate_reaches_consensus_through_real_subprocesses(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)
    artifact_path = repo_dir / "artifact.md"
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    build_env(monkeypatch, claude_dir, codex_dir, artifact_path)

    # Turn 1: claude creates the artifact, CONTINUE with one MUST remark.
    write_claude_artifact(claude_dir, 1, "# Artifact v0\n\nfirst draft\n")
    write_claude_reply(
        claude_dir, 1, "s-1", vblock("CONTINUE", remarks=["[MUST] add error handling"])
    )

    # Turn 2: codex edits the artifact, resolves #1, AGREEs.
    write_codex_artifact(codex_dir, 1, "# Artifact v1\n\nfirst draft, now with error handling\n")
    write_codex_reply(codex_dir, 1, "cx-1", vblock("AGREE", resolved=["#1 accepted"]))

    # Turn 3: claude re-confirms AGREE at the same (codex-edited) hash.
    write_claude_reply(claude_dir, 2, "s-1", vblock("AGREE"))

    gate = ScriptedGate(consensus=[GateDecision("accept")])
    orch, store, logs = build_orch(repo_dir, gate, artifact_path)

    code = orch.run_new("Design something")

    assert code == 0
    assert len(gate.consensus_calls) == 1
    assert artifact_path.read_text() == "# Artifact v1\n\nfirst draft, now with error handling\n"

    state = store.load()
    assert state.sides["claude"].last_verdict_status == "AGREE"
    assert state.sides["codex"].last_verdict_status == "AGREE"
    assert state.pending_remarks == []
    assert len(state.resolved_remarks) == 1
    assert state.resolved_remarks[0].resolution == "accepted"

    # Transcript event files exist for every turn: claude called twice
    # (.json), codex called once (.jsonl + its last-message .md).
    events_dir = repo_dir / ".spar" / "transcript"
    claude_events = list(events_dir.glob("claude-*.json"))
    codex_events = list(events_dir.glob("codex-*.jsonl"))
    codex_last_msgs = list(events_dir.glob("codex-last-*.md"))
    assert len(claude_events) == 2
    assert len(codex_events) == 1
    assert len(codex_last_msgs) == 1

    assert store.exists()


# ---------------------------------------------------------------------------
# Scenario 2: session resume flow
# ---------------------------------------------------------------------------


def test_second_turn_resumes_the_session_returned_by_the_first(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)
    artifact_path = repo_dir / "artifact.md"
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    build_env(monkeypatch, claude_dir, codex_dir, artifact_path)

    write_claude_artifact(claude_dir, 1, "# v0\n")
    write_claude_reply(claude_dir, 1, "s-1", vblock("CONTINUE"))
    write_codex_artifact(codex_dir, 1, "# v1\n")
    write_codex_reply(codex_dir, 1, "cx-1", vblock("AGREE"))
    write_claude_reply(claude_dir, 2, "s-1", vblock("AGREE"))

    gate = ScriptedGate(consensus=[GateDecision("accept")])
    orch, store, logs = build_orch(repo_dir, gate, artifact_path)

    code = orch.run_new("Design something")
    assert code == 0

    claude_calls = read_prompts_log(claude_dir)
    assert len(claude_calls) == 2
    # First call: fresh session, no --resume.
    assert "--resume" not in claude_calls[0]
    # Second call: resumes the session_id ("s-1") returned by the first.
    assert "--resume" in claude_calls[1]
    idx = claude_calls[1].index("--resume")
    assert claude_calls[1][idx + 1] == "s-1"


# ---------------------------------------------------------------------------
# Scenario 3: verdict garbage -> retry -> abort -> continue resumes
# ---------------------------------------------------------------------------


def test_garbage_verdict_retry_then_abort_then_continue_resumes(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)
    artifact_path = repo_dir / "artifact.md"
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    build_env(monkeypatch, claude_dir, codex_dir, artifact_path)

    # Claude creates the artifact and immediately AGREEs (no remarks pending,
    # so this is a syntactically valid, if unusual, first verdict).
    write_claude_artifact(claude_dir, 1, "# v0\n")
    write_claude_reply(claude_dir, 1, "s-1", vblock("AGREE"))

    # Codex's turn: no scripted reply files at all for calls 1/2, so the
    # fake falls back to its built-in default reply ("fake final reply"),
    # which contains no <verdict> block at all -> garbage both times.

    gate = ScriptedGate()
    orch, store, logs = build_orch(repo_dir, gate, artifact_path)

    code = orch.run_new("Design something")
    assert code == 4
    assert any("still unusable on retry" in m for m in logs)

    # State on disk is loadable and turn_in_progress was cleared.
    assert store.exists()
    state = store.load()
    assert state.turn_in_progress is None

    codex_calls = read_prompts_log(codex_dir)
    assert len(codex_calls) == 2  # initial turn + one verdict retry

    # Fix the script: codex's next (fresh) call gets a valid AGREE, with no
    # further artifact edit, so it lands at the hash claude already AGREEd.
    write_codex_reply(codex_dir, 3, "cx-1", vblock("AGREE"))

    gate.consensus.append(GateDecision("accept"))
    code2 = orch.run_continue()
    assert code2 == 0
    assert len(gate.consensus_calls) == 1

    final_state = store.load()
    assert final_state.sides["claude"].last_verdict_status == "AGREE"
    assert final_state.sides["codex"].last_verdict_status == "AGREE"


# ---------------------------------------------------------------------------
# Scenario 4: guard violation E2E (foreign file rollback + retry)
# ---------------------------------------------------------------------------


def test_guard_rolls_back_foreign_file_and_retry_completes_debate(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)
    artifact_path = repo_dir / "artifact.md"
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    build_env(monkeypatch, claude_dir, codex_dir, artifact_path)

    write_claude_artifact(claude_dir, 1, "# v0\n")
    write_claude_reply(claude_dir, 1, "s-1", vblock("CONTINUE"))

    # Codex's first attempt: legitimately edits the artifact AND writes a
    # foreign file next to it -> guard must flag + roll back the foreign
    # file and force a whole-turn retry.
    write_codex_artifact(codex_dir, 1, "# v1 (attempt 1)\n")
    write_codex_foreign(codex_dir, 1, "sneaky content")
    write_codex_reply(codex_dir, 1, "cx-1", vblock("AGREE"))

    # Codex's retry: clean edit, no foreign file.
    write_codex_artifact(codex_dir, 2, "# v1 (attempt 2, clean)\n")
    write_codex_reply(codex_dir, 2, "cx-1", vblock("AGREE"))

    # Claude re-confirms AGREE at codex's (clean) hash.
    write_claude_reply(claude_dir, 2, "s-1", vblock("AGREE"))

    gate = ScriptedGate(consensus=[GateDecision("accept")])
    orch, store, logs = build_orch(repo_dir, gate, artifact_path)

    code = orch.run_new("Design something")

    assert code == 0
    assert any("guard violation" in m for m in logs)
    assert artifact_path.read_text() == "# v1 (attempt 2, clean)\n"

    foreign_path = repo_dir / "foreign-codex-1.txt"
    assert not foreign_path.exists()

    codex_calls = read_prompts_log(codex_dir)
    assert len(codex_calls) == 2  # attempt 1 (violated) + attempt 2 (retry)


# ---------------------------------------------------------------------------
# Scenario 5: timeout E2E
# ---------------------------------------------------------------------------


def test_turn_timeout_aborts_with_recoverable_state(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)
    artifact_path = repo_dir / "artifact.md"
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    build_env(monkeypatch, claude_dir, codex_dir, artifact_path)

    write_claude_artifact(claude_dir, 1, "# v0\n")
    write_claude_reply(claude_dir, 1, "s-1", vblock("CONTINUE"))

    # Codex hangs well past the configured turn timeout.
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")

    gate = ScriptedGate()
    orch, store, logs = build_orch(repo_dir, gate, artifact_path, turn_timeout_sec=1)

    code = orch.run_new("Design something")

    assert code == 4
    assert any("adapter failed" in m for m in logs)

    # State is recoverable: loadable, and recovery classification doesn't
    # raise (the interrupted turn's artifact_hash_before still matches disk
    # since codex never got to write anything before timing out).
    assert store.exists()
    state = store.load()
    assert state.turn_in_progress is not None
    assert state.turn_in_progress.side == "codex"
    status = check_recovery(state, artifact_path)
    assert status == "repeat_turn"


# ---------------------------------------------------------------------------
# Scenario 6: CLI wiring smoke test (the only test going through cli.main)
# ---------------------------------------------------------------------------


def test_cli_end_to_end_through_subprocess(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    spar_dir = project_dir / ".spar"
    spar_dir.mkdir()

    (spar_dir / "config.toml").write_text(
        """
[sides.claude]
adapter = "claude"
command = "claude"

[sides.codex]
adapter = "codex"
command = "codex"

[debate]
max_rounds = 4
turn_timeout_sec = 15
""",
        encoding="utf-8",
    )

    # PATH-isolated bin dir: contains ONLY the fakes, named as the real CLIs.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").symlink_to(FAKE_CLAUDE)
    (bin_dir / "codex").symlink_to(FAKE_CODEX)

    artifact_path = spar_dir / "artifact.md"  # CLI default: .spar/artifact.md
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    claude_dir.mkdir()
    codex_dir.mkdir()

    write_claude_artifact(claude_dir, 1, "# CLI smoke artifact\n")
    write_claude_reply(claude_dir, 1, "cli-s1", vblock("CONTINUE"))
    write_codex_artifact(codex_dir, 1, "# CLI smoke artifact v2\n")
    write_codex_reply(codex_dir, 1, "cli-cx1", vblock("AGREE"))
    write_claude_reply(claude_dir, 2, "cli-s1", vblock("AGREE"))

    env = os.environ.copy()
    # bin_dir first (so "claude"/"codex" resolve to the fakes), then the
    # directory holding this interpreter (so the fakes' "#!/usr/bin/env
    # python3" shebang still resolves) -- nothing else, so a real claude/
    # codex on the ambient PATH can never be reached.
    env["PATH"] = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)])
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["FAKE_CLAUDE_SCRIPT_DIR"] = str(claude_dir)
    env["FAKE_CLAUDE_ARTIFACT_PATH"] = str(artifact_path)
    env["FAKE_CODEX_SCRIPT_DIR"] = str(codex_dir)
    env["FAKE_CODEX_ARTIFACT_PATH"] = str(artifact_path)

    result = subprocess.run(
        [sys.executable, "-m", "spar.cli", "Design something via the real CLI"],
        cwd=project_dir,
        env=env,
        input="a\n",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}"
    )
    assert artifact_path.exists()
    assert artifact_path.read_text() == "# CLI smoke artifact v2\n"
