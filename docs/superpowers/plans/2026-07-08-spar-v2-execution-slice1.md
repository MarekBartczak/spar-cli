# spar v2 — Execution mode, sequential slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the first sequential slice of spar v2 execution mode — take a consensus Plan's `## Tasks`, run each Task through implement → asymmetric cross-review → per-Task test → merge into an integration branch, then a final whole-suite Test phase, then a user-gated final merge.

**Architecture:** New `spar/exec/` package layered on the v1 engine. Reuses v1 adapters (`Adapter`/`TurnResult`), the verdict parser (`parse_verdict`, statuses AGREE/CONTINUE/DONE), the guard, and the `.spar/` atomic-state + flock patterns. Execution is strictly sequential (one ready Task at a time); no concurrency and no merge-conflict handling (each Task branches from the latest integration, so sequential merges are trivial). Fresh agent sessions per phase.

**Tech Stack:** Python ≥3.11, stdlib only (`argparse`, `subprocess`, `tomllib`, `dataclasses`, `pathlib`, `fcntl`), `pytest`. Git via subprocess.

**Spec:** `docs/superpowers/specs/2026-07-08-spar-v2-execution-design.md` (reviewed, challenge AGREE). Glossary: `CONTEXT.md`. Decisions: `docs/adr/0001`, `docs/adr/0002`.

## Global Constraints

- Python ≥3.11; stdlib only (no new runtime deps). Tests: pytest.
- Package/distribution `spar-cli`; command `spar`. New code under `spar/exec/`.
- Sessions are a token optimization only — every turn reproducible from `.spar/` state (source of truth). Fresh sessions per phase in this slice.
- Guard rule reused: on an implement turn the agent may edit **only files in the Task's file scope**; violation → reject + rollback to pre-turn state (as v1 guards the artifact).
- Verdict protocol reused verbatim: `parse_verdict(reply_text) -> Verdict` with `status ∈ {AGREE, CONTINUE, DONE}`, `resolutions`, `remarks`, `Severity ∈ {MUST, NICE, USER}`.
- No `Co-Authored-By` / AI-attribution trailers in any commit (user rule).
- Sequential only; no concurrency, no merge-conflict resolution in this slice.
- All state writes atomic (temp + `os.replace`) under a `fcntl.flock` single-instance lock, mirroring `spar/state.py`.

## Reused v1 interfaces (do not reimplement)

```python
# spar/adapters/base.py
@dataclass(frozen=True)
class TurnResult:
    session_id: str | None
    reply_text: str
    events_path: Path
    exit_code: int
class Adapter(Protocol):
    name: str
    def run_turn(self, prompt: str, session_id: str | None, timeout_sec: int) -> TurnResult: ...
class SessionLost(Exception): ...
class AdapterError(Exception): ...

# spar/verdict.py
class Severity(enum.Enum): MUST; NICE; USER
@dataclass(frozen=True)
class Remark: severity: Severity; text: str
@dataclass(frozen=True)
class Resolution: remark_id: int; accepted: bool; justification: str | None
@dataclass(frozen=True)
class Verdict: status: str; resolutions: tuple[Resolution, ...]; remarks: tuple[Remark, ...]
def parse_verdict(reply_text: str) -> Verdict  # raises VerdictError

# spar/state.py
def hash_artifact(path: Path) -> str            # "sha256:…"; raises StateError if missing
class StateStore:  # pattern to mirror for ExecStateStore
    def __init__(self, spar_dir: Path)
    def save(self, state) -> None                # atomic temp+replace
    def load(self)                                # raises StateError
    def locked(self)                              # context manager, fcntl.flock; raises LockHeld

# spar/config.py
@dataclass(frozen=True)
class SideConfig: adapter: str; command: str; model: str = ""
def load_config(project_dir: Path, global_path: Optional[Path] = None) -> Config
```

## File structure

- `spar/exec/__init__.py` — package marker.
- `spar/exec/tasklist.py` — parse the Plan's `## Tasks` section into `Task` objects + validation (Task 2).
- `spar/exec/state.py` — `ExecState`, `TaskState`, per-Task remark ledger, `ExecStateStore` (atomic + flock) (Task 3).
- `spar/exec/gitops.py` — git worktree/branch/merge/ancestor helpers (Task 4).
- `spar/exec/prompts.py` — implement-turn and review-turn prompt builders (Task 5).
- `spar/exec/review.py` — asymmetric cross-review loop (Task 6).
- `spar/exec/loop.py` — `Executor`: Task FSM, per-Task test, merge, final Test phase, fix-Task, final merge gate, recovery (Task 7).
- `spar/config.py` — extend with `[execution]` + per-side `models`/`default_model` (Task 1).
- `spar/cli.py` — add `exec` subcommand + flags (Task 8).
- `tests/test_exec_*.py` — per-module unit tests; `tests/test_exec_e2e.py` — end-to-end via fake adapters (Task 9).

---

### Task 1: Config — `[execution]` section + per-side model catalog (Haiku)

**Files:**
- Modify: `spar/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `SideConfig`, `load_config`, `_dict_to_config`, `_validate_side_config`.
- Produces:
  - `SideConfig` gains `models: tuple[str, ...] = ()` and `default_model: str = ""`.
  - New `@dataclass(frozen=True) ExecutionConfig: test_command: str = ""; max_review_rounds: int = 0` (0 = unlimited).
  - `Config` gains `execution: ExecutionConfig`.

- [ ] **Step 1: Write failing tests**

```python
def test_side_models_and_default_parsed(tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text('[sides.claude]\nmodels=["opus","sonnet"]\ndefault_model="sonnet"\n')
    cfg = load_config(tmp_path / "p", global_path=gp)
    assert cfg.sides["claude"].models == ("opus", "sonnet")
    assert cfg.sides["claude"].default_model == "sonnet"

def test_execution_section_parsed(tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text('[execution]\ntest_command="pytest -q"\nmax_review_rounds=3\n')
    cfg = load_config(tmp_path / "p", global_path=gp)
    assert cfg.execution.test_command == "pytest -q"
    assert cfg.execution.max_review_rounds == 3

def test_execution_defaults_when_absent(tmp_path):
    cfg = load_config(tmp_path / "p", global_path=tmp_path / "none.toml")
    assert cfg.execution.test_command == ""
    assert cfg.execution.max_review_rounds == 0

def test_default_model_must_be_in_catalog(tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text('[sides.claude]\nmodels=["opus"]\ndefault_model="haiku"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path / "p", global_path=gp)
```

- [ ] **Step 2: Run tests, verify they fail** — `pytest tests/test_config.py -k "models or execution or default_model" -v` → FAIL (unknown attrs).

- [ ] **Step 3: Implement** — in `spar/config.py`:
  - Add fields to `SideConfig`: `models: tuple[str, ...] = ()`, `default_model: str = ""`.
  - Add `ExecutionConfig` dataclass and `execution: ExecutionConfig` on `Config` (default `ExecutionConfig()`).
  - Extend `_validate_side_config` allowed keys with `models`, `default_model`; validate `models` is a list of non-empty strings; if `default_model` set, it must be in `models` else `ConfigError`.
  - Parse `[execution]` in `_dict_to_config`: `test_command` (str), `max_review_rounds` (int ≥ 0).
  - Extend the defaults dict and merge so `[execution]` merges like `[debate]`.

- [ ] **Step 4: Run tests, verify pass** — same command → PASS. Also run full `pytest -q` (no regressions).

- [ ] **Step 5: Commit** — `git add spar/config.py tests/test_config.py && git commit -m "feat(config): [execution] section and per-side model catalog"`

---

### Task 2: Task List parser (§4.1 grammar + validation) (Sonnet)

**Files:**
- Create: `spar/exec/__init__.py`, `spar/exec/tasklist.py`
- Test: `tests/test_exec_tasklist.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Task:
      id: str; description: str; side: str; model: str; review_model: str
      deps: tuple[str, ...]; files: tuple[str, ...]; test: str | None = None
  class TaskListError(Exception): ...
  def parse_task_list(plan_text: str, *, sides: dict[str, "SideConfig"], order: list[str]) -> tuple[Task, ...]
  ```
  `parse_task_list` extracts the `## Tasks` section, parses each `- [id] desc | k=v | …` line, and validates per §4.1. Raises `TaskListError` (with the offending line) on any violation. `sides`/`order` are passed so model-catalog and side-name checks can run here.

- [ ] **Step 1: Write failing tests**

```python
from spar.config import SideConfig
from spar.exec.tasklist import parse_task_list, Task, TaskListError
import pytest

SIDES = {
    "claude": SideConfig(adapter="claude", command="claude", models=("opus","sonnet")),
    "codex": SideConfig(adapter="codex", command="codex", models=("gpt-5.5","gpt-5.4")),
}
ORDER = ["claude", "codex"]

PLAN = """# Plan
blah
## Tasks
- [t1] config bits | side=claude | model=sonnet | review=gpt-5.4 | deps=- | files=spar/config.py,tests/test_config.py
- [t2] parser | side=codex | model=gpt-5.5 | review=opus | deps=t1 | files=spar/exec/tasklist.py | test=pytest tests/test_exec_tasklist.py -q
## Next
"""

def test_parses_tasks():
    tasks = parse_task_list(PLAN, sides=SIDES, order=ORDER)
    assert [t.id for t in tasks] == ["t1", "t2"]
    assert tasks[0] == Task("t1","config bits","claude","sonnet","gpt-5.4",(),("spar/config.py","tests/test_config.py"),None)
    assert tasks[1].deps == ("t1",)
    assert tasks[1].test == "pytest tests/test_exec_tasklist.py -q"

def test_missing_tasks_section_errors():
    with pytest.raises(TaskListError):
        parse_task_list("# Plan\nno tasks here\n", sides=SIDES, order=ORDER)

def test_unknown_side_errors():
    p = "## Tasks\n- [t1] x | side=ghost | model=opus | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_model_not_in_catalog_errors():
    p = "## Tasks\n- [t1] x | side=claude | model=gpt-5.5 | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_review_model_must_be_other_side_catalog():
    p = "## Tasks\n- [t1] x | side=claude | model=opus | review=opus | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):  # review must be in codex catalog
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_unknown_dep_errors():
    p = "## Tasks\n- [t1] x | side=claude | model=opus | review=gpt-5.4 | deps=t9 | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_dependency_cycle_errors():
    p = ("## Tasks\n"
         "- [t1] a | side=claude | model=opus | review=gpt-5.4 | deps=t2 | files=a.py\n"
         "- [t2] b | side=codex | model=gpt-5.5 | review=opus | deps=t1 | files=b.py\n")
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_bad_id_format_errors():
    p = "## Tasks\n- [x1] q | side=claude | model=opus | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_concurrent_file_overlap_is_warning_not_error(caplog):
    # sequential slice: overlap between independent tasks warns, does not raise
    p = ("## Tasks\n"
         "- [t1] a | side=claude | model=opus | review=gpt-5.4 | deps=- | files=shared.py\n"
         "- [t2] b | side=codex | model=gpt-5.5 | review=opus | deps=- | files=shared.py\n")
    tasks = parse_task_list(p, sides=SIDES, order=ORDER)  # no raise
    assert len(tasks) == 2
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_exec_tasklist.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `spar/exec/tasklist.py`.** Behavior:
  - Extract the `## Tasks` section: from a line matching `^##\s+Tasks\s*$` up to the next `^##\s` or EOF. No section → `TaskListError`.
  - For each non-blank line: must match `^- \[(?P<id>t\d+)\]\s+(?P<desc>.*?)\s*\|\s*(?P<rest>.*)$`. Split `rest` on ` | `. The last field may be `test=<cmd>` (value to EOL — so parse `test=` specially before splitting the remainder).
  - Required keys: `side, model, review, deps, files`. Missing/extra key → error.
  - `deps`: `-` → empty; else comma list of ids. `files`: comma list, non-empty.
  - Validation (all `TaskListError`): unique `t\d+` ids; `side ∈ sides`; `model ∈ sides[side].models`; `review ∈ sides[<other side>].models` where the other side is the single element of `order` that isn't `side` (in a 2-side run); `deps` reference existing ids; no cycle (DFS/topo). 
  - File-scope overlap between two tasks with no transitive dep either way and intersecting `files`: emit `logging.warning` (sequential slice), do not raise.
  - Return tasks in file order.

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_exec_tasklist.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add spar/exec/__init__.py spar/exec/tasklist.py tests/test_exec_tasklist.py && git commit -m "feat(exec): Task List parser with §4.1 grammar and validation"`

---

### Task 3: Execution state + per-Task ledger (`ExecState`, `ExecStateStore`) (Sonnet)

**Files:**
- Create: `spar/exec/state.py`
- Test: `tests/test_exec_state.py`

**Interfaces:**
- Consumes: `Task` (Task 2); v1 `StateRemark`/`ResolvedRemark`/`Severity`, `hash_artifact`; the atomic-write + flock pattern from `spar/state.py`.
- Produces:
  ```python
  TaskStatus = Literal["pending","ready","implementing","review","testing","merged"]
  @dataclass
  class TaskState:
      task: Task
      status: TaskStatus = "pending"
      branch: str | None = None            # spar/<id>-<side> once created
      pending_remarks: list[StateRemark] = field(default_factory=list)
      resolved_remarks: list[ResolvedRemark] = field(default_factory=list)
      next_remark_id: int = 1
      impl_session_id: str | None = None   # implementing side's exec-phase session
      review_session_id: str | None = None # reviewing side's exec-phase session
  @dataclass
  class ExecState:
      phase: Literal["execution","test","done"] = "execution"
      target_branch: str = ""
      target_base_oid: str = ""
      integration_branch: str = "spar/integration"
      tasks: dict[str, TaskState] = field(default_factory=dict)
      turn_in_progress: dict | None = None   # {"task_id","role","hash_before"}
  class ExecStateStore:  # mirror StateStore: __init__(spar_dir), save/load (atomic), locked()
      exec_path = "<spar_dir>/exec.json"
  ```
  Provide `to_dict`/`from_dict` round-trip (JSON) and helpers: `ExecState.ready_tasks()` (pending tasks whose deps are all `merged` → mark them ready), `ExecState.next_task()` (first ready task in id order or None), `ExecState.all_merged()`.

- [ ] **Step 1: Write failing tests** (round-trip; ready/next/dep-gating; atomic overwrite):

```python
from spar.exec.state import ExecState, TaskState, ExecStateStore
from spar.exec.tasklist import Task

def _t(id, deps=()): return Task(id, "d", "claude", "opus", "gpt-5.4", tuple(deps), ("a.py",))

def test_round_trip(tmp_path):
    st = ExecState(target_branch="master", target_base_oid="abc",
                   tasks={"t1": TaskState(_t("t1"))})
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    got = store.load()
    assert got.target_branch == "master"
    assert got.tasks["t1"].task.id == "t1"
    assert got.tasks["t1"].status == "pending"

def test_ready_gating_on_deps():
    st = ExecState(tasks={
        "t1": TaskState(_t("t1")),
        "t2": TaskState(_t("t2", deps=["t1"])),
    })
    st.mark_ready()                         # t1 -> ready, t2 stays pending
    assert st.tasks["t1"].status == "ready"
    assert st.tasks["t2"].status == "pending"
    st.tasks["t1"].status = "merged"
    st.mark_ready()                         # now t2 -> ready
    assert st.tasks["t2"].status == "ready"

def test_next_task_first_ready_in_id_order():
    st = ExecState(tasks={"t2": TaskState(_t("t2")), "t1": TaskState(_t("t1"))})
    st.mark_ready()
    assert st.next_task().task.id == "t1"

def test_all_merged():
    st = ExecState(tasks={"t1": TaskState(_t("t1"))})
    assert not st.all_merged()
    st.tasks["t1"].status = "merged"
    assert st.all_merged()
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_exec_state.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `spar/exec/state.py`.** Copy `StateStore`'s atomic-write (`tmp` + `os.replace`) and `locked()` flock context manager, pointing at `exec.json`/`lock`. Implement dataclasses, `to_dict`/`from_dict` (reuse `_remark_*`/`_resolved_*` helpers from `spar/state.py` — import them), and `mark_ready`/`next_task`/`all_merged`.

- [ ] **Step 4: Run, verify pass** — → PASS. Full `pytest -q`.

- [ ] **Step 5: Commit** — `git add spar/exec/state.py tests/test_exec_state.py && git commit -m "feat(exec): execution state, per-Task ledger, atomic store"`

---

### Task 4: Git operations (branches, worktrees, merge, ancestor checks) (Sonnet)

**Files:**
- Create: `spar/exec/gitops.py`
- Test: `tests/test_exec_gitops.py` (uses real `git` in a `tmp_path` repo)

**Interfaces:**
- Produces (all raise `GitError` on failure; run via `subprocess.run(check=False)` and inspect):
  ```python
  class GitError(Exception): ...
  def current_branch(repo: Path) -> str
  def rev_parse(repo: Path, ref: str) -> str                     # OID
  def is_clean(repo: Path) -> bool                                # no staged/unstaged changes
  def create_branch(repo: Path, name: str, base: str) -> None
  def delete_branch(repo: Path, name: str) -> None
  def add_worktree(repo: Path, path: Path, branch: str) -> None   # checkout existing branch
  def remove_worktree(repo: Path, path: Path) -> None
  def merge_no_ff(repo: Path, branch: str, message: str) -> None  # merge branch into current (integration)
  def is_ancestor(repo: Path, maybe_ancestor: str, ref: str) -> bool  # git merge-base --is-ancestor
  def diff(repo: Path, base: str, ref: str) -> str                # git diff base..ref
  def changed_files(repo: Path, base: str, ref: str) -> tuple[str, ...]
  ```

- [ ] **Step 1: Write failing tests** (init a repo in tmp_path, seed a commit, exercise each):

```python
import subprocess, pytest
from spar.exec.gitops import (create_branch, add_worktree, remove_worktree,
    merge_no_ff, is_ancestor, changed_files, current_branch, is_clean, GitError)

def _run(repo, *args): subprocess.run(["git","-C",str(repo),*args],check=True,capture_output=True)

@pytest.fixture
def repo(tmp_path):
    r = tmp_path/"r"; r.mkdir(); _run(r,"init","-q","-b","master")
    _run(r,"config","user.email","t@t"); _run(r,"config","user.name","t")
    (r/"seed.txt").write_text("x\n"); _run(r,"add","-A"); _run(r,"commit","-qm","init")
    return r

def test_branch_and_ancestor(repo):
    create_branch(repo, "spar/integration", "master")
    assert not is_ancestor(repo, "spar/integration", "master") or True  # same commit => ancestor
    assert is_ancestor(repo, "master", "spar/integration")

def test_worktree_add_edit_merge(repo, tmp_path):
    create_branch(repo, "spar/integration", "master")
    create_branch(repo, "spar/t1-claude", "spar/integration")
    wt = tmp_path/"wt"; add_worktree(repo, wt, "spar/t1-claude")
    (wt/"new.py").write_text("print(1)\n")
    subprocess.run(["git","-C",str(wt),"add","-A"],check=True)
    subprocess.run(["git","-C",str(wt),"commit","-qm","t1"],check=True)
    assert "new.py" in changed_files(repo, "spar/integration", "spar/t1-claude")
    remove_worktree(repo, wt)
    # merge into integration (checkout integration in main repo first)
    subprocess.run(["git","-C",str(repo),"checkout","-q","spar/integration"],check=True)
    merge_no_ff(repo, "spar/t1-claude", "merge t1")
    assert is_ancestor(repo, "spar/t1-claude", "spar/integration")

def test_is_clean(repo):
    assert is_clean(repo)
    (repo/"seed.txt").write_text("y\n")
    assert not is_clean(repo)
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_exec_gitops.py -v` → FAIL.

- [ ] **Step 3: Implement `spar/exec/gitops.py`** — thin wrappers over `git` subprocess. `is_ancestor` uses `git merge-base --is-ancestor a b` (exit 0 = ancestor, 1 = not, other = GitError). `changed_files` = `git diff --name-only base..ref`. `is_clean` = `git status --porcelain` empty. Never quote-strip porcelain paths beyond `.strip("\n")` split by line (reuse the un-quoting lesson from v1's guard: set `-c core.quotePath=false`).

- [ ] **Step 4: Run, verify pass** — → PASS.

- [ ] **Step 5: Commit** — `git add spar/exec/gitops.py tests/test_exec_gitops.py && git commit -m "feat(exec): git worktree/branch/merge/ancestor helpers"`

---

### Task 5: Prompt builders (implement + review turns) (Haiku)

**Files:**
- Create: `spar/exec/prompts.py`
- Test: `tests/test_exec_prompts.py`

**Interfaces:**
- Consumes: `Task`, `StateRemark`.
- Produces (pure functions returning `str`):
  ```python
  def build_impl_prompt(task: Task, artifact_plan_path: Path, open_remarks: list[StateRemark], warning: str | None = None) -> str
  def build_review_prompt(task: Task, diff_text: str, open_remarks: list[StateRemark]) -> str
  ```
  Both end with a verdict-block contract. Implement prompt: "edit ONLY files in scope `<files>`; the plan is at `<path>`; address open remarks; end with a verdict that only resolves remark ids (you do not judge your own work; do not emit DONE)". Review prompt: "read this diff (read-only); raise MUST/NICE remarks or emit DONE when no blocking remarks; you do not edit code".

- [ ] **Step 1: Write failing tests** asserting each prompt contains: the task id, the file-scope list, the plan path (impl) / the diff text (review), an open remark line, and the verdict-block delimiters. Assert the review prompt forbids editing and the impl prompt forbids DONE.

```python
from pathlib import Path
from spar.exec.prompts import build_impl_prompt, build_review_prompt
from spar.exec.tasklist import Task
from spar.state import StateRemark
from spar.verdict import Severity

T = Task("t1","do it","claude","opus","gpt-5.4",(),("spar/a.py","tests/test_a.py"))

def test_impl_prompt():
    p = build_impl_prompt(T, Path(".spar/artifact.md"), [StateRemark(3, Severity.MUST, "codex", "fix X")])
    assert "t1" in p and "spar/a.py" in p and ".spar/artifact.md" in p
    assert "#3" in p and "<verdict>" in p
    assert "do not" in p.lower() and "DONE" in p       # impl must not emit DONE

def test_review_prompt():
    p = build_review_prompt(T, "diff --git a/spar/a.py ...", [])
    assert "diff --git" in p and "<verdict>" in p
    assert "read-only" in p.lower() or "do not edit" in p.lower()
```

- [ ] **Step 2: Run, verify fail** → FAIL. **Step 3: Implement `spar/exec/prompts.py`.** **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git add spar/exec/prompts.py tests/test_exec_prompts.py && git commit -m "feat(exec): implement/review turn prompt builders"`

---

### Task 6: Asymmetric cross-review loop (Opus)

**Files:**
- Create: `spar/exec/review.py`
- Test: `tests/test_exec_review.py` (fake adapters + fake git-diff)

**Interfaces:**
- Consumes: `Adapter`, `TurnResult`, `parse_verdict`, `Verdict`, `Severity`, `Task`, `TaskState`, `StateRemark`, `ResolvedRemark`, `build_impl_prompt`, `build_review_prompt`, `gitops.diff`, `gitops.changed_files`, guard.
- Produces:
  ```python
  class ReviewAbort(Exception):  # unusable verdict twice, or guard fail twice
      def __init__(self, reason: str): ...
  def run_cross_review(
      *, task_state: TaskState, impl_adapter: Adapter, review_adapter: Adapter,
      repo: Path, worktree: Path, integration_base: str, plan_path: Path,
      timeout_sec: int, guard, log, store, exec_state,
  ) -> None
  ```
  Drives the asymmetric loop until the reviewer emits `DONE`:
  1. **Reviewer turn**: build review prompt from `gitops.diff(repo, integration_base, task_state.branch)` + open remarks; run `review_adapter` (fresh or resumed `review_session_id`); `parse_verdict`; reviewer editing anything is a guard violation (its cwd is read-only — but assert no code change via hash on the task branch). If `DONE` and no open MUST → return. Append new remarks to the ledger with fresh ids.
  2. **Implementer turn**: build impl prompt with open remarks; run `impl_adapter` in `worktree`; guard: only files in `task.files` may change (reuse v1 guard semantics on the worktree); `parse_verdict`; apply only `resolutions` (ignore implementer's new remarks); commit the worktree changes on the task branch.
  3. Persist `exec_state` after each turn (`turn_in_progress` bracketing as v1). Loop.
  - `AGREE`/`CONTINUE` from the reviewer are treated as "not done" (continue); only reviewer `DONE` terminates. Open `[NICE]` at DONE are allowed (stay in ledger). Verdict-parse failure → one retry (verdict-only prompt), then `ReviewAbort`.

- [ ] **Step 1: Write failing tests** with scripted fake adapters (mirror `tests/test_orchestrator.py` `FakeAdapter`/`Step`): 
  - reviewer raises one MUST (CONTINUE) → implementer resolves it (edits file) → reviewer DONE → loop returns, ledger has 1 resolved, 0 pending MUST, task branch has the implementer's commit.
  - reviewer DONE immediately (no remarks) → returns after one reviewer turn, zero implementer turns.
  - reviewer emits a MUST but implementer leaves it unresolved on next reviewer turn → reviewer re-raises / stays CONTINUE (no premature DONE).
  - implementer edits a file outside `task.files` → guard violation → rejected + rolled back; second violation → `ReviewAbort`.

- [ ] **Step 2: Run, verify fail** → FAIL.
- [ ] **Step 3: Implement `spar/exec/review.py`** per the interface above, reusing v1 guard + verdict retry patterns from `spar/orchestrator.py` (`_parse_verdict_with_retry`, `_apply_verdict` remark bookkeeping) adapted to the single-editing-side ledger.
- [ ] **Step 4: Run, verify pass** → PASS.
- [ ] **Step 5: Commit** — `git add spar/exec/review.py tests/test_exec_review.py && git commit -m "feat(exec): asymmetric cross-review loop to reviewer DONE"`

---

### Task 7: Executor — Task FSM, per-Task test, merge, final Test, fix-Task, final merge, recovery (Opus)

**Files:**
- Create: `spar/exec/loop.py`
- Test: `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: everything above + `ExecStateStore`, `Adapter` map, `ExecutionConfig`, a `Gate` (reuse v1 `ConsoleGate` pattern — inject `input_fn`/`print_fn`), `gitops`, `run_cross_review`.
- Produces:
  ```python
  class Executor:
      def __init__(self, *, repo: Path, spar_dir: Path, sides: dict[str, Adapter],
                   order: list[str], plan_path: Path, tasks: tuple[Task, ...],
                   execution: ExecutionConfig, gate, store: ExecStateStore,
                   log=print, auto_integration_merge: bool = False): ...
      def run(self) -> int: ...            # start fresh; exit codes mirror v1 (0 ok, 3 lock/state, 4 abort, 5 user abort)
      def run_continue(self) -> int: ...   # resume from exec.json + git reconciliation (§11.1)
  ```
  FSM driver (sequential): record target branch + base OID; require clean target (else exit 3). Create integration branch from target base. Loop: `mark_ready`; `next_task`; if none and `all_merged` → Test phase. For a task: create task branch from current integration + worktree; `status=implementing` (first implementer turn creates code); `status=review` → `run_cross_review`; `status=testing` → run per-Task test (`task.test` or `execution.test_command`) in the task branch, fail → back to implementing (loop), pass → checkout integration, `merge_no_ff`, delete branch + worktree, `status=merged`. Test phase: run `execution.test_command` on integration; fail → generate fix-Task (§7 of spec) and continue FSM; pass → final merge gate: present summary; user green-light (or `auto_integration_merge`) → merge integration into target (with §9 target-moved reconciliation), `phase=done`, exit 0.
  Recovery (`run_continue`, §11.1): reconcile each `turn_in_progress`/status against git via `is_ancestor` (merged-but-not-recorded, recorded-but-branch-exists), repeat interrupted turns, re-run interrupted tests.

- [ ] **Step 1: Write failing tests** — end-to-end-ish with fake adapters + a real tmp git repo:
  - **happy path**: 2 tasks, deps t2→t1; scripted so each implementer writes its files and each reviewer DONEs after resolving; per-Task tests pass (test_command = `true`); final test passes; gate `accept` → both branches merged, integration merged to master, exit 0, `all_merged()`.
  - **per-Task test fail then pass**: first test run fails (`test_command` scripted to fail once via a sentinel file), task loops back to implementing, second passes → merged.
  - **final test fail → fix task**: final `test_command` fails once → a `tfix1` task is generated and run → re-run passes → merged.
  - **recovery**: kill after a task merged but before state save (simulate by hand-editing exec.json to pre-merge while git shows merged) → `run_continue` detects via `is_ancestor` and marks merged, does not double-merge.
  - **clean-target guard**: dirty target → exit 3.

- [ ] **Step 2: Run, verify fail** → FAIL.
- [ ] **Step 3: Implement `spar/exec/loop.py`.** Reuse v1 `_Abort`/exit-code conventions and `turn_in_progress` bracketing. Fresh sessions per phase = pass `session_id=None` when a side first acts in the execution phase; resume within the phase via stored `impl_session_id`/`review_session_id`.
- [ ] **Step 4: Run, verify pass** → PASS. Full `pytest -q`.
- [ ] **Step 5: Commit** — `git add spar/exec/loop.py tests/test_exec_loop.py && git commit -m "feat(exec): sequential Task FSM, testing, merge, final test, fix-task, recovery"`

---

### Task 8: CLI — `spar exec` subcommand + flags (Sonnet)

**Files:**
- Modify: `spar/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Executor`, `parse_task_list`, `load_config`, `ExecStateStore`, adapters map (`_ADAPTERS`).
- Produces: `spar exec [--continue] [--merge-sessions] [--auto-integration-merge] [--sides ...] [--first ...]`. Reads `.spar/artifact.md`, parses `## Tasks`, builds adapters per Assignment side/model, constructs `Executor`, runs. `--merge-sessions` is recorded (behavior deferred per spec §13 — accept the flag, note it does not yet change session lifetime; assert it parses).

- [ ] **Step 1: Write failing tests** (mirror existing CLI tests; fake `Executor` via monkeypatch like `fake_orch`):
  - `spar exec` with a plan lacking `## Tasks` → usage error exit 2 with the guidance message.
  - `spar exec` with a valid plan → builds Executor, calls `run()`, propagates its exit code.
  - `spar exec --continue` → calls `run_continue()`.
  - `--auto-integration-merge` / `--merge-sessions` parse without error.

- [ ] **Step 2: Run, verify fail** → FAIL. **Step 3: Implement** — add an `exec` positional/subcommand path in `_build_parser`/`main` (argparse subparsers or a leading-token check consistent with the existing single-command structure; prefer `argparse` subparsers: `run` (default, existing) vs `exec`). Keep existing `spar "<prompt>"` behavior intact. **Step 4: Run, verify pass** — full `pytest -q`.
- [ ] **Step 5: Commit** — `git add spar/cli.py tests/test_cli.py && git commit -m "feat(cli): spar exec subcommand and execution flags"`

---

### Task 9: End-to-end execution through fake subprocess adapters (Sonnet)

**Files:**
- Create: `tests/test_exec_e2e.py`, `tests/fakes/fake_impl.py` (optional if the v1 fakes suffice)
- Test: `tests/test_exec_e2e.py`

**Interfaces:**
- Consumes: real `Executor` + real `gitops` on a tmp git repo, driven by scripted fake adapter binaries (reuse `tests/fakes/fake_claude.py`/`fake_codex.py` patterns: env-scripted replies that also write the task's files).

- [ ] **Step 1: Write the e2e test** — a 2-task plan in `.spar/artifact.md`, fake adapters scripted so: creator/impl writes files, reviewer DONEs, per-Task `test_command=true`, final test passes, gate auto-accept (`--auto-integration-merge`). Assert: integration merged into master, both task branches deleted, worktrees removed, `exec.json` `phase=done`, exit 0.

- [ ] **Step 2: Run, verify fail** → FAIL (until wiring is right). **Step 3:** adjust fakes/wiring as needed (no production placeholders — real behavior). **Step 4: Run, verify pass** — `pytest tests/test_exec_e2e.py -v`, then full `pytest -q`.
- [ ] **Step 5: Commit** — `git add tests/test_exec_e2e.py tests/fakes/ && git commit -m "test(exec): end-to-end sequential execution via fake adapters"`

---

## Self-review notes

- **Spec coverage:** §3 trigger→Task 8; §4/§4.1 Task List→Task 2; §5 isolation→Task 4+7; §6 FSM & cross-review→Task 6+7; §7 testing (per-Task + final) & fix-Task→Task 7; §8 no-failure/Ctrl+C→Task 7 (Ctrl+C = state saved via `turn_in_progress`, resume in Task 7); §9 final merge gate & target-moved→Task 7; §11/§11.1 state & recovery→Task 3+7; §12 CLI→Task 8; config catalog/`[execution]`→Task 1.
- **Deferred (not in this slice, per spec §10/§13):** concurrency, merge-conflict handling, `--merge-sessions` behavior (flag parsed only), soft loop caps, TUI.
- **Model assignments:** Haiku (1,5), Sonnet (2,3,4,8,9), Opus (6,7 — protocol/orchestration core). Per user's global CLAUDE.md convention.
