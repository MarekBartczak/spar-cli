# Exec Cleanup Tranche 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the remaining exec backlog: configurable turn timeout, repo restored to the target branch on abort, compile-capable claude implementer, and two prompt polish items.

**Architecture:** Four independent, small changes. (1) `[execution] turn_timeout_sec` (default 900) replaces the hardcoded `_DEFAULT_TIMEOUT_SEC` in the Executor. (2) A best-effort `_restore_target_checkout` runs on every Executor exit path: if the repo is checked out on the integration branch, clean, and not mid-merge, check the target branch back out. (3) The claude adapter's non-readonly tool allowlist grows to `Read,Edit,Write,Bash,Grep,Glob` — parity with codex's `workspace-write` shell; the readonly (reviewer) allowlist stays `Read`. (4) The permanent missing-file rule hedges its foreign-section reference with "(when present)"; the implementer protocol forbids inventing remark ids.

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

## Global Constraints

- Zero breaking change: `turn_timeout_sec` absent = 900 (current behavior); checkout restore is best-effort and silent-safe (never raises over the real exit code).
- Reviewer adapters stay read-only (`Read` only for claude, `--sandbox read-only` for codex) — this tranche widens ONLY the claude implementer.
- Language-agnostic prompt wording.
- TDD: every behavior lands with a test that failed first.
- Commits: conventional style, NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite green after every task: `python3 -m pytest tests/ -q` (325 passed, 2 skipped before this plan).

---

### Task 1: `[execution] turn_timeout_sec` (Sonnet)

**Files:**
- Modify: `spar/config.py` (`ExecutionConfig`, `_validate_execution_config`, execution parse block in `_dict_to_config`, `defaults_dict` in `load_config`)
- Modify: `spar/exec/loop.py` (drop `_DEFAULT_TIMEOUT_SEC` uses in `_run_task`; keep the constant as the dataclass default's single source is config — delete the module constant)
- Test: `tests/test_config.py`, `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `ExecutionConfig` (frozen dataclass: `test_command, max_review_rounds, max_fix_tasks`), `Executor.execution`.
- Produces: `ExecutionConfig.turn_timeout_sec: int = 900`; the Executor passes `self.execution.turn_timeout_sec` everywhere it passed `_DEFAULT_TIMEOUT_SEC` (both `_implementer_turn` calls and `run_cross_review`).

- [ ] **Step 1: Write the failing config tests**

Add to `tests/test_config.py` in `class TestExecutionConfig`:

```python
    def test_turn_timeout_sec_parsed(self, tmp_path):
        gp = tmp_path / "c.toml"
        gp.write_text('[execution]\nturn_timeout_sec=120\n')
        cfg = load_config(tmp_path / "p", global_path=gp)
        assert cfg.execution.turn_timeout_sec == 120

    def test_turn_timeout_sec_defaults_to_900(self, tmp_path):
        cfg = load_config(tmp_path / "p", global_path=tmp_path / "none.toml")
        assert cfg.execution.turn_timeout_sec == 900

    def test_turn_timeout_sec_must_be_positive_int(self, tmp_path):
        gp = tmp_path / "c.toml"
        gp.write_text('[execution]\nturn_timeout_sec=0\n')
        with pytest.raises(ConfigError):
            load_config(tmp_path / "p", global_path=gp)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_config.py -q -k turn_timeout`
Expected: first two FAIL (`TypeError`/unknown key → ConfigError raised as "Unknown key in execution config" makes the first fail; the default test fails with `AttributeError`); the third may pass trivially via the unknown-key path — keep it as a regression net.

- [ ] **Step 3: Implement in `spar/config.py`**

`ExecutionConfig` gains `turn_timeout_sec: int = 900`. `_validate_execution_config`: add `"turn_timeout_sec"` to `allowed_keys` and validate exactly like `[debate] turn_timeout_sec` (int, not bool, `>= 1`). Execution parse block in `_dict_to_config`: read `turn_timeout_sec` like the other keys, pass into `ExecutionConfig(...)`. `defaults_dict` in `load_config`: add `"turn_timeout_sec": defaults.execution.turn_timeout_sec` to the `"execution"` entry.

Run: `python3 -m pytest tests/test_config.py -q` → PASS.

- [ ] **Step 4: Write the failing Executor test**

Add to `tests/test_exec_loop.py`:

```python
def test_turn_timeout_from_execution_config(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true", turn_timeout_sec=123),
    )
    rc = ex.run()
    assert rc == 0
    # every adapter turn ran with the configured timeout
    assert all(c["timeout"] == 123 for a in adapters.values() for c in a.calls)
```

`FakeAdapter.run_turn` in this file records only `prompt`/`session_id` — extend the recorded dict with `"timeout": timeout_sec` (existing assertions index by key, so adding one is safe).

- [ ] **Step 5: Run to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_turn_timeout_from_execution_config -v`
Expected: FAIL — timeouts recorded as 900.

- [ ] **Step 6: Implement in `spar/exec/loop.py`**

Delete the `_DEFAULT_TIMEOUT_SEC = 900` module constant; in `_run_task`, replace all three `timeout_sec=_DEFAULT_TIMEOUT_SEC` arguments with `timeout_sec=self.execution.turn_timeout_sec`.

Run: `python3 -m pytest tests/test_exec_loop.py -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add spar/config.py spar/exec/loop.py tests/test_config.py tests/test_exec_loop.py
git commit -m "feat(config): [execution] turn_timeout_sec replaces hardcoded 900s"
```

---

### Task 2: restore target checkout on Executor exit (Sonnet)

**Files:**
- Modify: `spar/exec/loop.py` (`Executor.run`, `Executor.run_continue`, new `_restore_target_checkout`)
- Test: `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `gitops.current_branch`, `gitops.is_clean`, `gitops.merge_in_progress`, `gitops.checkout`, `ExecStateStore.load`.
- Produces: `Executor._restore_target_checkout() -> None` — best-effort; called after the guarded body in BOTH `run` and `run_continue`, before returning the exit code (including the KeyboardInterrupt path). Never raises.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_exec_loop.py`:

```python
def test_gate_abort_restores_target_checkout(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    # user aborts at the final-merge gate
    gate = FakeGate([GateDecision("abort")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 5
    # the repo must NOT be left on spar/integration
    assert gitops.current_branch(repo) == "master"
    # integration branch still exists (nothing was merged or deleted)
    assert branch_exists(repo, "spar/integration")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_gate_abort_restores_target_checkout -v`
Expected: FAIL — `current_branch` is `spar/integration` (the final-Test phase checked it out; the abort path never restores).

- [ ] **Step 3: Implement in `spar/exec/loop.py`**

New private helper on `Executor`:

```python
    def _restore_target_checkout(self) -> None:
        """Best-effort: leave the user's repo on the target branch.

        The final Test phase (and each task merge) checks out the integration
        branch in the user's repo; an abort or error can otherwise strand the
        checkout there. Only acts when it is unambiguously safe: state loads,
        the current branch IS the integration branch, no merge is in progress,
        and the tree is clean. Never raises — the real exit code always wins.
        """
        try:
            state = self.store.load()
            if (
                gitops.current_branch(self.repo) == state.integration_branch
                and not gitops.merge_in_progress(self.repo)
                and gitops.is_clean(self.repo)
                and self._branch_exists(state.target_branch)
            ):
                gitops.checkout(self.repo, state.target_branch)
                self.log(
                    f"spar exec: restored checkout to '{state.target_branch}'."
                )
        except Exception:
            pass
```

Call it in `run` AND `run_continue`, wrapping the guarded body so every path (normal return, LockHeld excluded — no state touched, KI included) restores. Pattern for `run` (mirror in `run_continue`):

```python
    def run(self) -> int:
        """Start a fresh Execution. Holds the single-instance lock throughout."""
        try:
            with self.store.locked():
                try:
                    return self._guarded(self._run_fresh)
                finally:
                    self._restore_target_checkout()
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3
        except KeyboardInterrupt:
            self.log(
                "spar exec: interrupted — state saved; resume with "
                "'spar exec --continue'."
            )
            return 130
```

(The `finally` runs inside the lock, before release — no racing a concurrent exec. On the happy path `_final_merge` already checked out the target; the helper's branch check makes it a no-op. On the conflict-surface path `merge_in_progress`/dirty tree make it a no-op, preserving the user's manual-resolution instructions.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_loop.py -q`
Expected: PASS (all, including existing abort/conflict tests — the helper is a no-op wherever restoring would be unsafe).

- [ ] **Step 5: Commit**

```bash
git add spar/exec/loop.py tests/test_exec_loop.py
git commit -m "fix(exec): restore target checkout on abort/error exits"
```

---

### Task 3: claude implementer gets Bash/Grep/Glob (Sonnet)

**Files:**
- Modify: `spar/adapters/claude.py` (`_build_argv` non-readonly allowlist)
- Test: `tests/test_adapter_claude.py`

**Interfaces:**
- Consumes: existing `perm_flags` branch in `_build_argv`.
- Produces: non-readonly allowlist `Read,Edit,Write,Bash,Grep,Glob` (readonly branch unchanged: `Read`). Rationale: parity with the codex side, whose `--sandbox workspace-write` already grants a full shell; the scope guard + self-commit handling in the review loop already police the results, and the per-task test needs the implementer to be able to compile-check its own work.

- [ ] **Step 1: Update the argv contract tests (RED)**

In `tests/test_adapter_claude.py`, the three existing argv tests assert `"Read,Edit,Write"` — update all three expected argv lists to `"Read,Edit,Write,Bash,Grep,Glob"`, and add no new test (the readonly test already pins the reviewer allowlist to `"Read"`).

Run: `python3 -m pytest tests/test_adapter_claude.py -q`
Expected: the three updated tests FAIL (code still emits the short list); the readonly test passes.

- [ ] **Step 2: Implement**

In `spar/adapters/claude.py`, the non-readonly branch:

```python
            perm_flags = [
                "--allowedTools",
                "Read,Edit,Write,Bash,Grep,Glob",
                "--permission-mode",
                "acceptEdits",
            ]
```

Update the surrounding comment: the implementer needs a shell to compile/lint its own work before the per-task test; the reviewer branch stays read-only.

Run: `python3 -m pytest tests/test_adapter_claude.py -q` → PASS.

- [ ] **Step 3: Full suite + commit**

Run: `python3 -m pytest tests/ -q` → PASS.

```bash
git add spar/adapters/claude.py tests/test_adapter_claude.py
git commit -m "feat(claude): implementer gets Bash/Grep/Glob (parity with codex shell)"
```

---

### Task 4: prompt polish — conditional foreign reference + no invented remark ids (Sonnet)

**Files:**
- Modify: `spar/exec/prompts.py` (`_REVIEW_PROTOCOL_BLOCK`, `_IMPL_PROTOCOL_BLOCK`)
- Test: `tests/test_exec_prompts.py`

**Interfaces:**
- Consumes: the two module-level protocol strings.
- Produces: review protocol's permanent missing-file rule says "the hard-reference rule of the foreign-files section (when present)"; impl protocol gains a rule forbidding resolutions for ids not listed in the prompt.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_exec_prompts.py`:

```python
def test_review_protocol_hedges_foreign_section_reference():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "foreign-files section (when present)" in p


def test_impl_protocol_forbids_invented_remark_ids():
    p = build_impl_prompt(T, Path("/abs/plan.md"), [])
    assert "ONLY ids listed" in p
```

(`build_impl_prompt` and `Path` are already imported in the file; verify and add imports if not.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_exec_prompts.py -q -k "hedges or invented"`
Expected: both FAIL (`AssertionError`).

- [ ] **Step 3: Implement in `spar/exec/prompts.py`**

In `_REVIEW_PROTOCOL_BLOCK`, change `...the hard-reference rule of the\n  foreign-files section (a plan-ordering defect stays a [MUST]...` to reference `foreign-files section (when present; a plan-ordering defect stays a [MUST]...` — i.e. fold "(when present" into the existing parenthetical so the sentence reads naturally when no foreign section exists. Exact replacement: the fragment

```text
This does NOT override the hard-reference rule of the
  foreign-files section (a plan-ordering defect stays a [MUST] even though the
  file matches the foreign list).
```

becomes

```text
This does NOT override the hard-reference rule of the
  foreign-files section (when present) — a plan-ordering defect stays a [MUST]
  even though the file matches the foreign list.
```

In `_IMPL_PROTOCOL_BLOCK`, after the rule line about including a `resolved:` section, add:

```text
- Resolve ONLY ids listed in the open remarks above — never invent or guess a
  remark id; a resolution for an unlisted id is ignored.
```

- [ ] **Step 4: Full suite + commit**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (existing prompt tests assert other substrings; the reworded sentence keeps "already present in the repository" intact).

```bash
git add spar/exec/prompts.py tests/test_exec_prompts.py
git commit -m "fix(exec): prompt polish — hedged foreign reference, no invented remark ids"
```

---

## Self-Review Notes

- Backlog coverage: timeout → Task 1; stranded checkout → Task 2; claude toolset → Task 3; both prompt nits (foreign hedge, phantom ids) → Task 4. Nothing else pending from HANDOFF's "Still open" list.
- Task 2's helper is deliberately conservative (four guards + swallow-all) so the conflict-surface path and dirty trees are untouched; the existing conflict tests double as its no-op regression net.
- Task 1 removes `_DEFAULT_TIMEOUT_SEC` entirely — grep for stragglers before committing (`grep -rn _DEFAULT_TIMEOUT_SEC spar/ tests/`).
