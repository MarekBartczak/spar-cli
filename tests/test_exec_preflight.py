"""Preflight validation of per-task test commands (spar/exec/preflight.py).

A FRESH ``spar exec`` must refuse to start when any task's ``test`` command
names a tool that does not exist on this machine (live incident: a plan wrote
``python -m py_compile`` on a python3-only host and the run failed deep into
execution). Resume (``--continue``) is exempt — a mid-run broken command is
already handled by the 126/127 gate + ``fix:``.

Unit tests drive the pure helpers with an injected ``which``; integration
tests drive the Executor over a real tmp git repo (fixtures shared with
tests/test_exec_loop.py).
"""

from spar.exec.preflight import first_command_token, preflight_test_commands
from spar.exec.state import ExecState, TaskState
from spar.orchestrator import GateDecision

from tests.test_exec_loop import (  # noqa: F401  (repo fixture)
    FakeGate,
    Step,
    branch_exists,
    build_executor,
    make_task,
    repo,
    vblock,
)
from spar.config import ExecutionConfig


# ---------------------------------------------------------------------------
# first_command_token
# ---------------------------------------------------------------------------


def test_first_token_plain_command():
    assert first_command_token("pytest -q tests/") == "pytest"


def test_first_token_skips_env_assignments():
    assert first_command_token("PYTHONPATH=x FOO=bar python3 -m pytest") == "python3"


def test_first_token_handles_quotes():
    assert first_command_token("'my tool' --flag") == "my tool"


def test_first_token_empty_and_env_only():
    assert first_command_token("") is None
    assert first_command_token("FOO=bar") is None


def test_first_token_unparsable_returns_none():
    # Unbalanced quote: shlex cannot split — preflight must not crash on it.
    assert first_command_token("echo 'oops") is None


# ---------------------------------------------------------------------------
# preflight_test_commands (pure, injected which)
# ---------------------------------------------------------------------------


def _which_factory(available):
    return lambda tok: f"/usr/bin/{tok}" if tok in available else None


def test_preflight_all_present_no_problems():
    tasks = [make_task("t1", "A", ["a.py"], test="pytest -q")]
    assert preflight_test_commands(tasks, which=_which_factory({"pytest"})) == []


def test_preflight_missing_tool_reported_with_task_and_token():
    tasks = [make_task("t1", "A", ["a.py"], test="frobnicate --check a.py")]
    problems = preflight_test_commands(tasks, which=_which_factory(set()))
    assert len(problems) == 1
    assert "[t1]" in problems[0]
    assert "'frobnicate'" in problems[0]
    assert "frobnicate --check a.py" in problems[0]
    assert "not found" in problems[0]


def test_preflight_python_suggests_python3():
    tasks = [make_task("t1", "A", ["a.py"], test="python -m py_compile a.py")]
    problems = preflight_test_commands(tasks, which=_which_factory({"python3"}))
    assert len(problems) == 1
    assert "'python'" in problems[0]
    assert "python3" in problems[0]


def test_preflight_python_no_suggestion_without_python3():
    tasks = [make_task("t1", "A", ["a.py"], test="python -m py_compile a.py")]
    problems = preflight_test_commands(tasks, which=_which_factory(set()))
    assert len(problems) == 1
    assert "python3" not in problems[0]


def test_preflight_shell_builtins_count_as_available():
    tasks = [
        make_task("t1", "A", ["a"], test="test -f README.md"),
        make_task("t2", "A", ["b"], test="[ -f README.md ]"),
        make_task("t3", "A", ["c"], test="true"),
        make_task("t4", "A", ["d"], test="command -v pytest"),
    ]
    assert preflight_test_commands(tasks, which=_which_factory(set())) == []


def test_preflight_skips_empty_test():
    tasks = [make_task("t1", "A", ["a.py"], test=None)]
    assert preflight_test_commands(tasks, which=_which_factory(set())) == []


def test_preflight_env_assignment_skipped_validates_real_command():
    tasks = [make_task("t1", "A", ["a.py"], test="PYTHONPATH=x python3 -m pytest")]
    assert preflight_test_commands(tasks, which=_which_factory({"python3"})) == []
    problems = preflight_test_commands(tasks, which=_which_factory(set()))
    assert len(problems) == 1 and "'python3'" in problems[0]


def test_preflight_skips_command_substitution():
    # Shell substitution has no plain command word to validate — preflight
    # must never block on a guess (the 126/127 gate still covers it mid-run).
    tasks = [
        make_task("t1", "A", ["a"], test="n=$(($(cat cnt)+1)); [ $n -ge 4 ]"),
        make_task("t2", "A", ["b"], test="`missing-tool` --x"),
    ]
    assert preflight_test_commands(tasks, which=_which_factory(set())) == []


def test_preflight_skips_variable_command_token():
    tasks = [make_task("t1", "A", ["a"], test="$RUNNER -q tests/")]
    assert preflight_test_commands(tasks, which=_which_factory(set())) == []


def test_preflight_reports_every_offending_task():
    tasks = [
        make_task("t1", "A", ["a.py"], test="missing-one a"),
        make_task("t2", "B", ["b.py"], test="pytest -q"),
        make_task("t3", "A", ["c.py"], test="missing-two c"),
    ]
    problems = preflight_test_commands(tasks, which=_which_factory({"pytest"}))
    assert len(problems) == 2
    assert "[t1]" in problems[0] and "[t3]" in problems[1]


# ---------------------------------------------------------------------------
# Executor integration: fresh run refuses BEFORE any git state is touched
# ---------------------------------------------------------------------------


def test_fresh_run_refuses_on_missing_tool_before_any_git_state(repo, tmp_path):
    tasks = [
        make_task(
            "t1", "A", ["a.py"], test="definitely-not-a-real-tool-xyz --check"
        )
    ]
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo,
        tmp_path,
        tasks=tasks,
        steps_by_side={},
        gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 2
    joined = "\n".join(logs)
    assert "preflight" in joined
    assert "[t1]" in joined
    assert "definitely-not-a-real-tool-xyz" in joined
    # No git state was created: no integration branch, no adapter turn ran,
    # no exec state persisted.
    assert not branch_exists(repo, "spar/integration")
    assert not adapters
    assert not (tmp_path / ".spar" / "exec.json").exists()


def test_fresh_run_passes_preflight_with_shell_builtin(repo, tmp_path):
    # ``test -f seed.txt`` is a shell builtin over a file the repo fixture
    # seeds: preflight must let the run through, and the run completes.
    tasks = [make_task("t1", "A", ["work1.py"], test="test -f seed.txt")]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work1.py": "print(1)\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo,
        tmp_path,
        tasks=tasks,
        steps_by_side=steps,
        gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    assert ex.run() == 0


def test_resume_does_not_rerun_preflight(repo, tmp_path):
    # Resume is exempt by design: a broken command discovered mid-run is
    # handled by the 126/127 gate + ``fix:`` (which updates state). A resume
    # must not be blocked even when a task's test names a missing tool.
    tasks = [
        make_task("t1", "A", ["a.py"], test="definitely-not-a-real-tool-xyz")
    ]
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo,
        tmp_path,
        tasks=tasks,
        steps_by_side={},
        gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    state = ExecState(
        phase="done",
        target_branch="master",
        target_base_oid="0" * 40,
        integration_branch="spar/integration",
        tasks={t.id: TaskState(task=t, status="merged") for t in tasks},
    )
    store.save(state)
    rc = ex.run_continue()
    assert rc == 0
    assert "preflight" not in "\n".join(logs)
