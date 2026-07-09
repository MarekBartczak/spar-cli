# Exec Cleanup Tranche 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four cleanliness gaps found in the live C++ test: weak-model implementation assignments, NICE remarks lost at merge, ugly Ctrl+C, and two prompt nits.

**Architecture:** Four independent, small changes. (1) An opt-in per-side `impl_models` floor enforced where task lists are validated (`parse_task_list`) and taught to the planner in the `--tasks` contract. (2) `Executor._merge_summary` additionally lists open NICE remarks. (3) `Executor.run`/`run_continue` catch `KeyboardInterrupt` → friendly message + exit 130 (state is already saved by the existing `BaseException` handler in `_run_task`; the lock is released by the `locked()` context manager). (4) Review-protocol prompt tells the reviewer not to emit a "no concerns" remark; the foreign-files list sorts task ids numerically.

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

## Global Constraints

- `impl_models` is opt-in: absent/empty = no restriction (zero breaking change for existing configs).
- Language-agnostic prompt wording.
- TDD: each behavior lands with a test that failed first.
- Commits: conventional style, NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite green after every task: `python3 -m pytest tests/ -q` (311 passed, 2 skipped before this plan; grows with each task).

---

### Task 1: `impl_models` floor per side (Sonnet)

**Files:**
- Modify: `spar/config.py` (`SideConfig`, `_validate_side_config`, side-merge block in `_dict_to_config`, `defaults_dict` in `load_config`)
- Modify: `spar/exec/tasklist.py` (`_validate_task`)
- Modify: `spar/orchestrator.py` (`_format_tasks_contract`, `build_turn_prompt`, `Orchestrator._catalogs` + `_compose_prompt`)
- Test: `tests/test_config.py`, `tests/test_exec_tasklist.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `SideConfig` (frozen dataclass: `adapter, command, model, models, default_model`), `parse_task_list(plan_text, *, sides, order)`, `_format_tasks_contract(catalogs)` where `catalogs: dict[str, tuple[str, ...]]`.
- Produces:
  - `SideConfig.impl_models: tuple[str, ...] = ()` — models allowed for `model=` (implementation) assignments; `()` = all of `models` allowed.
  - `_format_tasks_contract(catalogs, impl_catalogs=None)` — `impl_catalogs: dict[str, tuple[str, ...]] | None`, same keys as `catalogs`; only sides with a non-empty entry get the restriction shown.
  - `build_turn_prompt(..., impl_catalogs=None)` — passed through to the contract.
  - `TaskListError` raised by `parse_task_list` when a task's `model=` is outside its side's non-empty `impl_models`.

- [ ] **Step 1: Write the failing config tests**

Add to `tests/test_config.py` inside `class TestSideConfig` (or as module-level functions next to the other side-config tests if there is no such class — follow the file's convention):

```python
def test_impl_models_parsed(self, tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text(
        '[sides.claude]\nmodels=["opus","sonnet","haiku"]\n'
        'default_model="sonnet"\nimpl_models=["opus","sonnet"]\n'
    )
    cfg = load_config(tmp_path / "p", global_path=gp)
    assert cfg.sides["claude"].impl_models == ("opus", "sonnet")

def test_impl_models_default_empty(self, tmp_path):
    cfg = load_config(tmp_path / "p", global_path=tmp_path / "none.toml")
    assert cfg.sides["claude"].impl_models == ()

def test_impl_models_must_be_subset_of_models(self, tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text(
        '[sides.claude]\nmodels=["sonnet"]\nimpl_models=["opus"]\n'
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path / "p", global_path=gp)

def test_impl_models_must_be_string_list(self, tmp_path):
    gp = tmp_path / "c.toml"
    gp.write_text('[sides.claude]\nimpl_models="opus"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path / "p", global_path=gp)
```

(If the file uses `self`-less module functions, drop `self`. Import `ConfigError`/`pytest` are already present.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_config.py -q -k impl_models`
Expected: FAIL — `TypeError`/`AssertionError` (no `impl_models` field) and the two error cases fail with `ConfigError` not raised... except `test_impl_models_must_be_string_list`, which fails with "Unknown key in side 'claude': impl_models" being raised as ConfigError — that one may PASS trivially; keep it anyway as a regression net.

- [ ] **Step 3: Implement in `spar/config.py`**

Field:

```python
@dataclass(frozen=True)
class SideConfig:
    """Configuration for a debate side (adapter)."""

    adapter: str
    command: str
    model: str = ""
    models: tuple[str, ...] = ()
    default_model: str = ""
    # Models allowed for IMPLEMENTATION (`model=`) assignments in a task list.
    # Empty = no restriction (any of `models`). `review=` is never restricted.
    impl_models: tuple[str, ...] = ()
```

In `_validate_side_config`, extend `allowed_keys` with `"impl_models"` and add:

```python
    if "impl_models" in config:
        impl_models = config["impl_models"]
        if not isinstance(impl_models, list):
            raise ConfigError(
                f"Side '{side_name}': impl_models must be a list, got {type(impl_models).__name__}"
            )
        for m in impl_models:
            if not isinstance(m, str) or not m.strip():
                raise ConfigError(
                    f"Side '{side_name}': impl_models must be a list of non-empty strings"
                )
```

In the side-merge block of `_dict_to_config` (both branches, mirroring `models`):

```python
            impl_models = tuple(side_config.get("impl_models", base_side.impl_models)) if "impl_models" in side_config else base_side.impl_models
```

(existing-side branch) and

```python
            impl_models = tuple(side_config.get("impl_models", ())) if "impl_models" in side_config else ()
```

(new-side branch); pass `impl_models=impl_models` into the `SideConfig(...)` construction, and after the existing `default_model` final check add the authoritative subset check on the FINAL MERGED config:

```python
        if merged_side.impl_models:
            missing = [m for m in merged_side.impl_models if m not in merged_side.models]
            if missing:
                raise ConfigError(
                    f"Side '{side_name}': impl_models {missing} must be members of models"
                )
```

In `load_config`'s `defaults_dict`, extend each side entry with `"impl_models": list(side.impl_models)`.

- [ ] **Step 4: Run config tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: PASS (all).

- [ ] **Step 5: Write the failing tasklist test**

Add to `tests/test_exec_tasklist.py` (follow its existing helper for building `sides` — it constructs `SideConfig` objects; extend or build inline):

```python
def test_model_outside_impl_models_rejected():
    from spar.config import SideConfig

    sides = {
        "claude": SideConfig(
            adapter="claude", command="claude",
            models=("opus", "sonnet", "haiku"), default_model="sonnet",
            impl_models=("opus", "sonnet"),
        ),
        "codex": SideConfig(
            adapter="codex", command="codex",
            models=("gpt-5.5",), default_model="gpt-5.5",
        ),
    }
    plan = """## Tasks
- [t1] do it | side=claude | model=haiku | review=gpt-5.5 | deps=- | files=a.py
"""
    with pytest.raises(TaskListError) as excinfo:
        parse_task_list(plan, sides=sides, order=["claude", "codex"])
    assert "impl_models" in str(excinfo.value)


def test_empty_impl_models_allows_any_catalog_model():
    from spar.config import SideConfig

    sides = {
        "claude": SideConfig(
            adapter="claude", command="claude",
            models=("haiku",), default_model="haiku",
        ),
        "codex": SideConfig(
            adapter="codex", command="codex",
            models=("gpt-5.5",), default_model="gpt-5.5",
        ),
    }
    plan = """## Tasks
- [t1] do it | side=claude | model=haiku | review=gpt-5.5 | deps=- | files=a.py
"""
    tasks = parse_task_list(plan, sides=sides, order=["claude", "codex"])
    assert tasks[0].model == "haiku"
```

- [ ] **Step 6: Run to verify the first fails**

Run: `python3 -m pytest tests/test_exec_tasklist.py -q -k impl_models`
Expected: `test_model_outside_impl_models_rejected` FAILS (no error raised); the second passes (guard).

- [ ] **Step 7: Implement in `spar/exec/tasklist.py`**

In `_validate_task`, directly after the existing `rt["model"] not in sides[side].models` check:

```python
    impl_allowed = getattr(sides[side], "impl_models", ()) or ()
    if impl_allowed and rt["model"] not in impl_allowed:
        raise TaskListError(
            f"model {rt['model']!r} not allowed for implementation on side {side!r} "
            f"(impl_models={list(impl_allowed)}), line: {line!r}"
        )
```

(`getattr` keeps the function tolerant of test doubles that predate the field.)

- [ ] **Step 8: Run tasklist tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_tasklist.py -q`
Expected: PASS (all).

- [ ] **Step 9: Write the failing contract test**

Add to `tests/test_orchestrator.py` next to `test_tasks_contract_includes_planning_invariants`:

```python
def test_tasks_contract_shows_impl_model_restriction():
    from spar.orchestrator import _format_tasks_contract

    text = _format_tasks_contract(
        {"claude": ("opus", "sonnet", "haiku"), "codex": ("gpt-5.5",)},
        impl_catalogs={"claude": ("opus", "sonnet"), "codex": ()},
    )
    # restricted side: implementation subset called out
    assert "claude: opus, sonnet, haiku (implementation: ONLY opus, sonnet)" in text
    # unrestricted side: plain catalog line, no restriction note
    assert "- codex: gpt-5.5" in text
    assert "codex: gpt-5.5 (implementation" not in text
    # the rule is stated
    assert "review= may use any model of the reviewing side" in text
```

- [ ] **Step 10: Run to verify it fails**

Run: `python3 -m pytest tests/test_orchestrator.py::test_tasks_contract_shows_impl_model_restriction -v`
Expected: FAIL with `TypeError: _format_tasks_contract() got an unexpected keyword argument 'impl_catalogs'`.

- [ ] **Step 11: Implement in `spar/orchestrator.py`**

`_format_tasks_contract(catalogs, impl_catalogs=None)`: the per-side catalog loop

```python
    for side, models in catalogs.items():
        lines.append(f"- {side}: {', '.join(models)}")
```

becomes

```python
    impl_catalogs = impl_catalogs or {}
    for side, models in catalogs.items():
        impl = impl_catalogs.get(side, ())
        if impl:
            lines.append(
                f"- {side}: {', '.join(models)} "
                f"(implementation: ONLY {', '.join(impl)})"
            )
        else:
            lines.append(f"- {side}: {', '.join(models)}")
```

and after the existing "- model is one of THAT side's models" rule line, append:

```python
        "- where a side's catalog notes an implementation restriction, model= "
        "MUST be one of those implementation models; review= may use any model "
        "of the reviewing side.",
```

`build_turn_prompt` gains `impl_catalogs: dict[str, tuple[str, ...]] | None = None` and passes it to `_format_tasks_contract` wherever `catalogs` is passed today. `Orchestrator._compose_prompt` supplies it via a new helper next to `_catalogs`:

```python
    def _impl_catalogs(self) -> dict[str, tuple[str, ...]]:
        """Per-side implementation-model floors (empty tuple = unrestricted)."""
        cfgs = self.side_configs or {}
        return {
            name: getattr(cfgs[name], "impl_models", ()) or ()
            for name in self.order
            if name in cfgs
        }
```

called as `impl_catalogs=self._impl_catalogs() if self.require_tasks else None` in both `build_turn_prompt` call sites of `_compose_prompt`.

- [ ] **Step 12: Full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS, 0 failures.

- [ ] **Step 13: Commit**

```bash
git add spar/config.py spar/exec/tasklist.py spar/orchestrator.py \
        tests/test_config.py tests/test_exec_tasklist.py tests/test_orchestrator.py
git commit -m "feat(config): per-side impl_models floor for task assignments"
```

---

### Task 2: open NICE remarks in the final-merge summary (Sonnet)

**Files:**
- Modify: `spar/exec/loop.py` (`_merge_summary`, imports)
- Test: `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `TaskState.pending_remarks` (list of `StateRemark` with `.severity`, `.remark_id`, `.author`, `.text`), `Severity` from `spar.verdict`.
- Produces: `_merge_summary` output gains an `open NICE remarks:` block when any merged task carries pending NICE remarks; unchanged otherwise.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_exec_loop.py`:

```python
def test_merge_summary_lists_open_nice_remarks(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        # DONE with a NICE remark: non-blocking, stays pending through merge
        "B": [Step(vblock("DONE", remarks=["[NICE] consider a docstring"]))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    summary = gate.calls[0]
    assert "open NICE remarks" in summary
    assert "consider a docstring" in summary
    assert "[t1]" in summary


def test_merge_summary_omits_nice_block_when_none(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    assert "open NICE remarks" not in gate.calls[0]
```

- [ ] **Step 2: Run to verify the first fails**

Run: `python3 -m pytest tests/test_exec_loop.py -q -k merge_summary`
Expected: first FAILS (`"open NICE remarks" not in summary`); second passes (guard).

- [ ] **Step 3: Implement in `spar/exec/loop.py`**

Add `from spar.verdict import Severity` to the imports. Replace `_merge_summary`:

```python
    def _merge_summary(self, state: ExecState) -> str:
        merged = [tid for tid, ts in state.tasks.items() if ts.status == "merged"]
        diffstat = self._diffstat(state.target_base_oid, state.integration_branch)
        summary = (
            "spar exec: final Test passed. Ready to merge integration into "
            f"'{state.target_branch}'.\n"
            f"  tasks merged: {', '.join(sorted(merged))}\n"
            f"  diff --stat {state.target_branch}..integration:\n{diffstat}"
        )
        # Open NICE remarks are non-blocking by design, but they should not
        # silently die with the run — surface them once, at the final gate.
        nice_lines = [
            f"    [{tid}] #{r.remark_id} ({r.author}): {r.text}"
            for tid, ts in sorted(state.tasks.items())
            for r in ts.pending_remarks
            if r.severity == Severity.NICE
        ]
        if nice_lines:
            summary += "\n  open NICE remarks (non-blocking):\n" + "\n".join(nice_lines)
        return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_loop.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add spar/exec/loop.py tests/test_exec_loop.py
git commit -m "feat(exec): surface open NICE remarks at the final-merge gate"
```

---

### Task 3: graceful SIGINT in exec (Sonnet)

**Files:**
- Modify: `spar/exec/loop.py` (`Executor.run`, `Executor.run_continue`)
- Test: `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: existing `Step(..., raises=...)` support does NOT exist in `tests/test_exec_loop.py`'s `Step` (only in `test_exec_review.py`'s) — add a `raises` parameter mirroring it.
- Produces: `run()`/`run_continue()` return `130` on `KeyboardInterrupt` after logging a resume hint. State file exists (saved by the pre-existing `except BaseException` in `_run_task`); the lock is released (context manager).

- [ ] **Step 1: Write the failing test**

In `tests/test_exec_loop.py`, extend `Step.__init__` with `raises=None` and honor it first thing in `__call__`:

```python
class Step:
    def __init__(self, reply, sid="sess", edits=None, raises=None):
        self.reply = reply
        self.sid = sid
        self.edits = edits or {}
        self.raises = raises

    def __call__(self, root):
        if self.raises is not None:
            raise self.raises
        ...
```

(keep the rest of `__call__` as-is). Then add:

```python
def test_keyboard_interrupt_exits_130_with_resume_hint(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step("", raises=KeyboardInterrupt())],
        "B": [],
    }
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 130
    assert any("--continue" in ln for ln in logs)
    # state survived and the lock is free: a resume can load it
    assert store.exists()
    with store.locked():
        pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_keyboard_interrupt_exits_130_with_resume_hint -v`
Expected: FAIL — `KeyboardInterrupt` propagates out of `ex.run()` (pytest reports it as an error/failure), instead of returning 130.

- [ ] **Step 3: Implement in `spar/exec/loop.py`**

Wrap both entry points (`run` and `run_continue`) — the pattern for `run` (mirror it in `run_continue`):

```python
    def run(self) -> int:
        """Start a fresh Execution. Holds the single-instance lock throughout."""
        try:
            with self.store.locked():
                return self._guarded(self._run_fresh)
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3
        except KeyboardInterrupt:
            # State was persisted by the in-flight save points (every turn is
            # bracketed by store.save); the lock was released by locked().
            self.log(
                "spar exec: interrupted — state saved; resume with "
                "'spar exec --continue'."
            )
            return 130
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_loop.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add spar/exec/loop.py tests/test_exec_loop.py
git commit -m "feat(exec): graceful Ctrl+C — exit 130 with a --continue hint"
```

---

### Task 4: prompt nits — no "no concerns" remark + numeric foreign sort (Sonnet)

**Files:**
- Modify: `spar/exec/prompts.py` (`_REVIEW_PROTOCOL_BLOCK`)
- Modify: `spar/exec/loop.py` (`_run_task` foreign-files sort)
- Test: `tests/test_exec_prompts.py`, `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `_REVIEW_PROTOCOL_BLOCK` (module string), the `foreign_files` computation added by the blocker-A work in `Executor._run_task`.
- Produces: protocol text gains the omit-empty-remarks rule; foreign list ordered by numeric task id (`t2` before `t10`), non-`t<n>` ids after numeric ones.

- [ ] **Step 1: Write the failing prompt test**

Add to `tests/test_exec_prompts.py`:

```python
def test_review_protocol_forbids_no_concerns_remark():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "OMIT the `remarks:` section entirely" in p
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_exec_prompts.py::test_review_protocol_forbids_no_concerns_remark -v`
Expected: FAIL (`AssertionError`).

- [ ] **Step 3: Implement the protocol line**

In `spar/exec/prompts.py`, inside `_REVIEW_PROTOCOL_BLOCK`, after the line
`- In \`remarks:\` raise new concerns, tagged \`[MUST]\` (blocking) or \`[NICE]\` (optional).` add:

```text
- If you have NO concerns to raise, OMIT the `remarks:` section entirely — do
  NOT add a remark whose text merely says you have no concerns.
```

Run: `python3 -m pytest tests/test_exec_prompts.py -q` → PASS.

- [ ] **Step 4: Write the failing sort test**

Add to `tests/test_exec_loop.py`:

```python
def test_foreign_files_sorted_numerically_by_task_id(repo, tmp_path):
    # Reviewing t1 while t2 and t10 are pending: the foreign list must order
    # t2 before t10 (numeric), not lexicographically (t10 < t2).
    tasks = [
        make_task("t1", "A", ["w1.py"]),
        make_task("t2", "B", ["w2.py"], deps=["t1"], model="mb", review="ma"),
        make_task("t10", "B", ["w10.py"], deps=["t1", "t2"], model="mb", review="ma"),
    ]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"w1.py": "x\n"}),  # impl t1
            Step(vblock("DONE")),  # review t2
            Step(vblock("DONE")),  # review t10
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("CONTINUE"), edits={"w2.py": "x\n"}),  # impl t2
            Step(vblock("CONTINUE"), edits={"w10.py": "x\n"}),  # impl t10
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    prompt = adapters["B"].calls[0]["prompt"]  # review of t1
    assert prompt.index("t2: w2.py") < prompt.index("t10: w10.py")
```

- [ ] **Step 5: Run to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_foreign_files_sorted_numerically_by_task_id -v`
Expected: FAIL on the index assertion (lexicographic order puts `t10` first).

- [ ] **Step 6: Implement the numeric sort**

In `spar/exec/loop.py`, in `_run_task`, replace the foreign-files sort key
(`key=lambda t: t.task.id`) with a numeric-aware one:

```python
        def _task_order(ts_: TaskState):
            m = re.match(r"t(\d+)$", ts_.task.id)
            return (0, int(m.group(1)), "") if m else (1, 0, ts_.task.id)

        foreign_files = tuple(
            (other.task.id, other.task.files)
            for other in sorted(state.tasks.values(), key=_task_order)
            if other.task.id != task.id and other.status != "merged"
        )
```

(`re` is already imported in loop.py.)

- [ ] **Step 7: Full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add spar/exec/prompts.py spar/exec/loop.py \
        tests/test_exec_prompts.py tests/test_exec_loop.py
git commit -m "fix(exec): review-protocol omit-empty-remarks rule + numeric foreign-list order"
```

---

## Self-Review Notes

- Spec coverage: floor → Task 1; NICE at gate → Task 2; SIGINT → Task 3; both prompt nits → Task 4. All four tranche-1 items covered; nothing extra.
- `impl_models` enforcement path: config parse → `parse_task_list` (used by BOTH the debate's consensus gate `_tasks_section_valid` and `spar exec`'s startup parse), so a hand-edited artifact is caught at exec time too.
- Type consistency: `impl_models: tuple[str, ...]` everywhere; `impl_catalogs: dict[str, tuple[str, ...]] | None` in both prompt-layer functions.
