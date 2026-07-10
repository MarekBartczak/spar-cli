"""True end-to-end test for the sequential execution engine.

Unlike ``tests/test_exec_loop.py`` (real ``Executor`` + real git, but an
in-Python ``FakeAdapter`` standing in for the ``Adapter`` protocol) this test
drives the whole stack for real: a real tmp git repo, a real
``spar.config.load_config``-parsed ``.spar/config.toml``, real
``spar.exec.tasklist.parse_task_list`` over a real ``.spar/artifact.md`` Plan,
and — crucially — the real :class:`~spar.adapters.claude.ClaudeAdapter` /
:class:`~spar.adapters.codex.CodexAdapter` subprocess adapters, pointed at the
scripted fake CLI binaries in ``tests/fakes/`` (mirroring how
``tests/test_e2e_debate.py`` drives v1's Orchestrator). Every implement/review
turn is an actual subprocess invocation of a fake binary; no real
``claude``/``codex`` CLI is ever invoked.

The fakes' scripted-file-writing mode (``<n>.files.json``) was extended for
this test (see ``tests/fakes/fake_claude.py`` / ``fake_codex.py``) so a
scripted implementer turn can write its task's file(s) into its ``cwd`` (the
task's git worktree), exactly like a real coding agent would.
"""

import json
import subprocess
from pathlib import Path

from spar.adapters.claude import ClaudeAdapter
from spar.adapters.codex import CodexAdapter
from spar.config import load_config
from spar.exec.loop import Executor
from spar.exec.state import ExecStateStore
from spar.exec.tasklist import parse_task_list
from spar.orchestrator import GateDecision

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = str(REPO_ROOT / "tests" / "fakes" / "fake_claude.py")
FAKE_CODEX = str(REPO_ROOT / "tests" / "fakes" / "fake_codex.py")

_ADAPTER_CLASSES = {"claude": ClaudeAdapter, "codex": CodexAdapter}


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


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def branch_exists(repo, name) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    git(repo_dir, "init", "-q", "-b", "master")
    git(repo_dir, "config", "user.email", "test@example.com")
    git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-qm", "init")


def write_claude_reply(script_dir: Path, n: int, session_id: str, result_text: str) -> None:
    (script_dir / f"{n}.json").write_text(
        json.dumps({"session_id": session_id, "result": result_text}), encoding="utf-8"
    )


def write_claude_files(script_dir: Path, n: int, files: dict[str, str]) -> None:
    (script_dir / f"{n}.files.json").write_text(json.dumps(files), encoding="utf-8")


def write_codex_reply(script_dir: Path, n: int, session_id: str, message_text: str) -> None:
    jsonl = (
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": message_text},
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        + "\n"
    )
    (script_dir / f"{n}.jsonl").write_text(jsonl, encoding="utf-8")
    (script_dir / f"{n}.md").write_text(message_text, encoding="utf-8")


def write_codex_files(script_dir: Path, n: int, files: dict[str, str]) -> None:
    (script_dir / f"{n}.files.json").write_text(json.dumps(files), encoding="utf-8")


def call_count(script_dir: Path) -> int:
    counter = script_dir / ".calls"
    if not counter.exists():
        return 0
    return int(counter.read_text().strip())


class ScriptedFinalGate:
    """A minimal :class:`~spar.exec.loop.ExecGate`; asserts it is never asked
    to decide anything it wasn't scripted for (the happy path here relies on
    ``auto_integration_merge`` instead, so this only guards against a
    surprise gate consultation)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def final_merge_gate(self, summary: str) -> GateDecision:
        self.calls.append(summary)
        return GateDecision(action="accept")


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_sequential_execution_end_to_end_through_real_subprocess_adapters(tmp_path, monkeypatch):
    # -- 1. Real tmp git repo (project root == tmp_path; .spar lives beside
    # the repo, mirroring tests/test_exec_loop.py, so the target repo itself
    # stays clean — is_clean() is checked at the start of a fresh run). ----
    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)

    spar_dir = tmp_path / ".spar"
    spar_dir.mkdir()
    artifact_path = spar_dir / "artifact.md"

    # -- 2. A real .spar/config.toml, loaded through the real config loader,
    # pointing both sides' commands at the scripted fake binaries. ---------
    (spar_dir / "config.toml").write_text(
        f"""
[sides.claude]
adapter = "claude"
command = "{FAKE_CLAUDE}"
models = ["m1"]
default_model = "m1"

[sides.codex]
adapter = "codex"
command = "{FAKE_CODEX}"
models = ["m2"]
default_model = "m2"

[execution]
test_command = "true"
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path, global_path=tmp_path / "no-such-global-config.toml")

    # -- 3. A real Plan with a two-task ## Tasks section: t1 (claude), t2
    # (codex, depends on t1). Both carry a per-task test=true. -------------
    plan_text = """# Plan

Design prose goes here.

## Tasks
- [t1] Write file a.txt | side=claude | model=m1 | review=m2 | deps=- | files=a.txt | test=true
- [t2] Write file b.txt | side=codex | model=m2 | review=m1 | deps=t1 | files=b.txt | test=true
"""
    artifact_path.write_text(plan_text, encoding="utf-8")
    order = ["claude", "codex"]
    tasks = parse_task_list(plan_text, sides=config.sides, order=order)
    assert [t.id for t in tasks] == ["t1", "t2"]

    # -- 4. Script the fakes. -----------------------------------------------
    claude_dir = tmp_path / "claude_script"
    codex_dir = tmp_path / "codex_script"
    claude_dir.mkdir()
    codex_dir.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT_DIR", str(claude_dir))
    monkeypatch.setenv("FAKE_CODEX_SCRIPT_DIR", str(codex_dir))

    # t1 (side=claude, reviewer=codex):
    #   claude call 1: implementer turn -> writes a.txt, CONTINUE (no open
    #   remarks to resolve on a task's very first turn).
    write_claude_files(claude_dir, 1, {"a.txt": "hello from task 1\n"})
    write_claude_reply(claude_dir, 1, "claude-s1", vblock("CONTINUE"))
    #   codex call 1: reviewer turn -> DONE, no blocking remarks -> the
    #   cross-review loop for t1 ends after this single reviewer turn.
    write_codex_reply(codex_dir, 1, "codex-r1", vblock("DONE"))

    # t2 (side=codex, reviewer=claude):
    #   codex call 2: implementer turn -> writes b.txt, CONTINUE.
    write_codex_files(codex_dir, 2, {"b.txt": "hello from task 2\n"})
    write_codex_reply(codex_dir, 2, "codex-s2", vblock("CONTINUE"))
    #   claude call 2: reviewer turn -> DONE, no blocking remarks.
    write_claude_reply(claude_dir, 2, "claude-r2", vblock("DONE"))

    # -- 5. Build the real Executor, wiring make_adapter to real adapters
    # pointed at the fakes (mirrors cli._build_executor). -------------------
    def make_adapter(side: str, worktree: Path, model: str, readonly: bool = False):
        side_cfg = config.sides[side]
        adapter_cls = _ADAPTER_CLASSES[side_cfg.adapter]
        return adapter_cls(
            command=side_cfg.command,
            model=model,
            cwd=worktree,
            events_dir=spar_dir / "transcript",
            side_name=side,
        )

    store = ExecStateStore(spar_dir)
    logs: list[str] = []
    gate = ScriptedFinalGate()
    executor = Executor(
        repo=repo_dir,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=config.sides,
        order=order,
        plan_path=artifact_path,
        tasks=tasks,
        execution=config.execution,
        gate=gate,
        store=store,
        log=logs.append,
        auto_integration_merge=True,
    )

    # -- 6. Run the real engine end to end. ---------------------------------
    code = executor.run()

    assert code == 0, f"executor.run() failed (exit {code}); log:\n" + "\n".join(logs)

    # auto_integration_merge bypassed the gate entirely.
    assert gate.calls == []

    # Both fakes were invoked exactly as scripted (no retries/extra turns).
    assert call_count(claude_dir) == 2
    assert call_count(codex_dir) == 2

    # -- exec.json: phase == done, both tasks merged. -----------------------
    state = store.load()
    assert state.phase == "done"
    assert state.all_merged()
    assert state.tasks["t1"].status == "merged"
    assert state.tasks["t2"].status == "merged"

    # -- both task branches deleted. -----------------------------------------
    assert not branch_exists(repo_dir, "spar/t1-claude")
    assert not branch_exists(repo_dir, "spar/t2-codex")

    # -- per-side worktrees removed. ------------------------------------------
    assert not (spar_dir / "worktrees" / "claude").exists()
    assert not (spar_dir / "worktrees" / "codex").exists()

    # -- spar/integration merged into master: master now contains both
    # tasks' files with the real content the fakes wrote. --------------------
    assert git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"
    assert (repo_dir / "a.txt").read_text(encoding="utf-8") == "hello from task 1\n"
    assert (repo_dir / "b.txt").read_text(encoding="utf-8") == "hello from task 2\n"
    master_files = git(repo_dir, "ls-tree", "-r", "--name-only", "master").stdout.split()
    assert "a.txt" in master_files
    assert "b.txt" in master_files

    # integration was merged into master, then swept: master's merge commit
    # carries it as second parent, and the branch itself is gone so a fresh
    # exec never trips over it as a leftover.
    assert (
        subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet",
             "refs/heads/spar/integration"],
            capture_output=True,
        ).returncode
        == 1
    )
    assert (
        subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "master^2"],
            capture_output=True,
        ).returncode
        == 0
    )
