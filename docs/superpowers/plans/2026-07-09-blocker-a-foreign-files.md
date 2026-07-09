# Blocker A — Foreign Files & Planning Invariants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-Task isolation work for interdependent tasks: the planner learns two planning invariants (A1), and the reviewer learns which absent files are legitimately "foreign" (A2), so scaffold/build-config tasks stop drawing unsatisfiable MUSTs and unpassable per-Task tests.

**Architecture:** Two prompt-level changes, one data-plumbing change. A1 extends the `--tasks` planning contract in `spar/orchestrator.py` (cross-reference rule, per-Task test satisfiability, optional `test=` field surfaced in the grammar). A2 threads a *foreign files* list — file scopes of other, not-yet-merged Tasks — from `Executor._run_task` through `run_cross_review` into `build_review_prompt`, plus a review-protocol rule with a sharp edge (foreign absence ≠ defect; reference outside diff∪foreign = defect). No parser enforcement, no engine semantics changes.

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

**Design decisions (from the grilling session, recorded in CONTEXT.md):**
- Reviewer gets a *file list only* (task id + globs), never other tasks' descriptions.
- List covers only NOT-yet-merged other tasks (merged files are on the branch already).
- A1 is prompt guidance only — statically unenforceable; the existing round/fix caps are the safety net for a bad plan.
- Guidance is language-agnostic (C++ test repo is just an example).
- Validation: regenerate the plan in `/home/marek/P_PROJ/spar_tests` through a real debate, then run exec.

## Global Constraints

- Language-agnostic wording in all prompts — no C++/CMake-specific rules (examples allowed as illustrations).
- `CONTEXT.md` terminology is canonical: "foreign files", "cross-reference rule", "per-Task test satisfiability".
- TDD: every behavior lands with a test that failed first.
- Commits: conventional style, NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite must stay green: `python3 -m pytest tests/ -q` → 0 failures (306 passed, 2 skipped expected before this plan).

---

### Task 1: A1 — planning invariants in the `--tasks` contract (Sonnet)

**Files:**
- Modify: `spar/orchestrator.py` (`_format_tasks_contract`, ~lines 248–286)
- Test: `tests/test_orchestrator.py` (next to `test_build_turn_prompt_require_tasks_injects_tasks_contract`, line ~204)

**Interfaces:**
- Consumes: `_format_tasks_contract(catalogs)` — existing pure function returning the contract text appended to debate turn prompts when `--tasks` is on.
- Produces: same signature; the returned text additionally shows the optional `test=` field in the grammar line and three new planning rules. Task 2 does not depend on this text.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py` (after `test_build_turn_prompt_require_tasks_injects_tasks_contract`):

```python
def test_tasks_contract_includes_planning_invariants():
    # The --tasks contract must teach the planner the two isolation
    # invariants (cross-reference rule, per-task test satisfiability) and
    # surface the optional per-task test= field in the grammar.
    from spar.orchestrator import _format_tasks_contract

    text = _format_tasks_contract({"claude": ("m1",), "codex": ("m2",)})
    # grammar line shows the optional test= field
    assert "[ | test=<cmd>]" in text
    # cross-reference rule: referencing another task's files => deps on it
    assert "references files owned by another task" in text
    # scaffold/build-config guidance: such a task comes last
    assert "comes LAST" in text
    # per-task test satisfiability: runnable on the task's own branch
    assert "only its deps" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_orchestrator.py::test_tasks_contract_includes_planning_invariants -v`
Expected: FAIL with `AssertionError` (the strings are not in the contract yet).

- [ ] **Step 3: Extend `_format_tasks_contract`**

In `spar/orchestrator.py`, change the grammar line and extend the `Rules:` list. The grammar line

```python
        "- [t<n>] <desc> | side=<side> | model=<impl-model> | "
        "review=<review-model> | deps=<id,id|-> | files=<glob,glob>",
```

becomes

```python
        "- [t<n>] <desc> | side=<side> | model=<impl-model> | "
        "review=<review-model> | deps=<id,id|-> | files=<glob,glob>"
        "[ | test=<cmd>]",
```

and after the existing rule `"- files is a comma list of globs naming the task's file scope."` insert:

```python
        "- test (optional) is a shell command gating THIS task's merge; "
        "without it the global test command runs instead.",
        "",
        "Isolation invariants (each task is implemented, reviewed and tested "
        "on its own branch containing ONLY its merged deps):",
        "- Cross-reference rule: if a task's file content references files "
        "owned by another task, it MUST list that task in deps. A "
        "build-config/scaffold task that wires the project together "
        "(build files, manifests, top-level config) therefore comes LAST, "
        "depending on every task whose files it references.",
        "- Per-task test satisfiability: each task's test= must be runnable "
        "on the task's own branch, i.e. judged against only its deps, not "
        "the finished project. Give partial states a compile/lint-level "
        "check; reserve the full build/suite for the final task and the "
        "final Test phase.",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_orchestrator.py -q`
Expected: PASS (all, including the two existing contract tests — they assert other substrings and must not break).

- [ ] **Step 5: Commit**

```bash
git add spar/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): teach --tasks contract the isolation invariants (blocker A1)"
```

---

### Task 2: A2 — foreign files list in the review prompt (Sonnet)

**Files:**
- Modify: `spar/exec/prompts.py` (`build_review_prompt`, `_REVIEW_PROTOCOL_BLOCK`)
- Modify: `spar/exec/review.py` (`run_cross_review` — new parameter, pass-through at line ~218)
- Modify: `spar/exec/loop.py` (`Executor._run_task` — compute the list, pass to `run_cross_review`)
- Test: `tests/test_exec_prompts.py`, `tests/test_exec_review.py`, `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `TaskState.status` / `Task.files` from `spar/exec/state.py`; `run_cross_review(**kwargs)` from Task-1-independent existing code.
- Produces:
  - `build_review_prompt(task, diff_text, open_remarks, foreign_files=())` where `foreign_files: tuple[tuple[str, tuple[str, ...]], ...]` — `(task_id, file_globs)` pairs, ordered by task id.
  - `run_cross_review(..., foreign_files=())` — same type, defaulting to empty (existing callers unaffected).

- [ ] **Step 1: Write the failing prompt tests**

Add to `tests/test_exec_prompts.py`:

```python
def test_review_prompt_lists_foreign_files():
    p = build_review_prompt(
        T,
        "diff --git a/CMakeLists.txt ...",
        [],
        foreign_files=(("t3", ("src/main.cpp",)), ("t4", ("tests/*.cpp",))),
    )
    # the list section names each foreign scope with its owning task
    assert "Files owned by other, not-yet-merged tasks" in p
    assert "t3: src/main.cpp" in p
    assert "t4: tests/*.cpp" in p
    # the judging rule has a sharp edge in both directions
    assert "their absence is NOT a defect" in p
    assert "neither in the diff nor in that list IS a defect" in p


def test_review_prompt_omits_foreign_section_when_empty():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "not-yet-merged tasks" not in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_exec_prompts.py -v -k foreign`
Expected: first test FAILS with `TypeError: build_review_prompt() got an unexpected keyword argument 'foreign_files'`; second PASSES trivially (guard that the section is conditional — keep it).

- [ ] **Step 3: Implement the prompt change**

In `spar/exec/prompts.py`, change `build_review_prompt`:

```python
def build_review_prompt(
    task: Task,
    diff_text: str,
    open_remarks: list[StateRemark],
    foreign_files: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> str:
```

and before `protocol = _REVIEW_PROTOCOL_BLOCK` add:

```python
    foreign_section = ""
    if foreign_files:
        lines = [
            "Files owned by other, not-yet-merged tasks (they will be created "
            "by later merges and may be ABSENT on this branch — their absence "
            "is NOT a defect; check only that this task's references to them "
            "match these planned names/paths):"
        ]
        for task_id, globs in foreign_files:
            lines.append(f"  {task_id}: {', '.join(globs)}")
        lines.append(
            "A reference to a file that is neither in the diff nor in that "
            "list IS a defect worth a [MUST] remark."
        )
        foreign_section = "\n" + "\n".join(lines) + "\n"
```

and include `{foreign_section}` in the returned f-string between the diff and `{remarks_section}`:

```python
    return f"""\
Task ID: {task.id}
Description: {task.description}

Review the following diff (read-only — you must NOT edit code):

{diff_text}
{foreign_section}{remarks_section}

{protocol}\
"""
```

(Note the newline handling: `foreign_section` is empty or starts/ends with `\n`, so the empty case renders identically to today's output except for one blank line after the diff — the existing `test_exec_prompts.py` assertions are substring-based and survive this.)

- [ ] **Step 4: Run prompt tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_prompts.py -q`
Expected: PASS (all).

- [ ] **Step 5: Write the failing plumbing test (review loop)**

Add to `tests/test_exec_review.py`:

```python
def test_foreign_files_reach_the_review_prompt(env):
    task = _task(files=("work.py",))
    impl_steps = []
    review_steps = [Step(vblock("DONE"))]
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps,
        foreign_files=(("t9", ("src/*.cpp",)),),
    )
    assert "t9: src/*.cpp" in review.calls[0]["prompt"]
    # the implementer never sees the reviewer-facing list (no impl turn here,
    # but the signature must not leak it into build_impl_prompt)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python3 -m pytest tests/test_exec_review.py::test_foreign_files_reach_the_review_prompt -v`
Expected: FAIL with `TypeError: run_cross_review() got an unexpected keyword argument 'foreign_files'`.

- [ ] **Step 7: Thread the parameter through `run_cross_review`**

In `spar/exec/review.py` add the keyword-only parameter (after `rounds_gate`):

```python
    max_rounds: int = 0,
    rounds_gate=None,
    foreign_files: tuple[tuple[str, tuple[str, ...]], ...] = (),
```

and pass it at the reviewer-turn prompt build (line ~218):

```python
        prompt = build_review_prompt(
            task, diff_text, list(task_state.pending_remarks),
            foreign_files=foreign_files,
        )
```

- [ ] **Step 8: Run it to verify it passes**

Run: `python3 -m pytest tests/test_exec_review.py -q`
Expected: PASS (all).

- [ ] **Step 9: Write the failing Executor test**

Add to `tests/test_exec_loop.py`:

```python
def test_reviewer_prompt_lists_unmerged_tasks_files_only(repo, tmp_path):
    # While t1 is under review, t2 (pending) is foreign; after t1 merges,
    # t2's review must NOT list t1 (already merged).
    tasks = [
        make_task("t1", "A", ["work1.py"]),
        make_task("t2", "B", ["work2.py"], deps=["t1"], model="mb", review="ma"),
    ]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work1.py": "print(1)\n"}),  # impl t1
            Step(vblock("DONE")),  # review t2
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("CONTINUE"), edits={"work2.py": "print(2)\n"}),  # impl t2
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    # B's first call reviewed t1: t2 was pending -> listed as foreign
    assert "t2: work2.py" in adapters["B"].calls[0]["prompt"]
    # A's second call reviewed t2: t1 already merged -> NOT listed
    assert "t1: work1.py" not in adapters["A"].calls[1]["prompt"]
```

- [ ] **Step 10: Run it to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_reviewer_prompt_lists_unmerged_tasks_files_only -v`
Expected: FAIL on the first prompt assertion (no foreign section yet).

- [ ] **Step 11: Compute the list in `Executor._run_task`**

In `spar/exec/loop.py`, inside `_run_task` (before the `while True:` loop, after `reviewer = ...`), add:

```python
        # Foreign files (A2): file scopes of other, not-yet-merged tasks.
        # From the reviewer's seat these may be legitimately absent on the
        # task branch; the review prompt lists them so their absence is not
        # mistaken for a defect. Merged tasks' files are on the branch
        # already, so they are not foreign. Sequential execution: statuses
        # cannot change while this task runs, so compute once.
        foreign_files = tuple(
            (other.task.id, other.task.files)
            for other in sorted(state.tasks.values(), key=lambda t: t.task.id)
            if other.task.id != task.id and other.status != "merged"
        )
```

and pass it to `run_cross_review`:

```python
                run_cross_review(
                    ...,
                    max_rounds=self.execution.max_review_rounds,
                    rounds_gate=self._review_rounds_gate(state),
                    foreign_files=foreign_files,
                )
```

- [ ] **Step 12: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (0 failures; ~310 passed, 2 skipped).

- [ ] **Step 13: Commit**

```bash
git add spar/exec/prompts.py spar/exec/review.py spar/exec/loop.py \
        tests/test_exec_prompts.py tests/test_exec_review.py tests/test_exec_loop.py
git commit -m "feat(exec): reviewer learns foreign files of unmerged tasks (blocker A2)"
```

---

### Task 3: Live validation in spar_tests (manual, user-driven — no model)

Interactive gates; run by the human with the agent watching. Not a subagent task.

- [ ] **Step 1: Reset the exec state (keep `.spar/config.toml`)**

```bash
cd /home/marek/P_PROJ/spar_tests && \
git checkout master 2>/dev/null; git worktree prune; \
for b in $(git branch --format='%(refname:short)' | grep '^spar/'); do git branch -D "$b"; done; \
rm -f CMakeLists.txt *.cpp; rm -rf src tests build .spar/exec.json .spar/worktrees .spar/lock
```

- [ ] **Step 2: Regenerate the plan through a real debate (validates A1)**

```bash
cd /home/marek/P_PROJ/spar_tests && /home/marek/P_PROJ/ai_fight/.venv/bin/spar \
  "Napisz mały program C++: CLI liczący silnię liczby z argv, z walidacją i obsługą błędu. Zakończ plan sekcją ## Tasks dzielącą pracę między strony." \
  --sides claude,codex --first claude --tasks
```

Acceptance: the agreed `## Tasks` section puts the build-config task LAST with `deps=` on the source tasks, and every task carries a `test=` runnable on its own branch. If not → A1 guidance too weak; iterate on the contract text before touching the engine.

- [ ] **Step 3: Run execution (validates A2 + the whole engine)**

```bash
cd /home/marek/P_PROJ/spar_tests && /home/marek/P_PROJ/ai_fight/.venv/bin/spar exec
```

Acceptance: all tasks reach merged without an unsatisfiable-MUST stall; the review-rounds gate does NOT fire on the scaffold task; final Test passes; final merge lands on master. Watch transcripts in `.spar/transcript/` for the foreign-files section in review prompts.

- [ ] **Step 4: Record the outcome**

Update `docs/HANDOFF.md` (blocker A status) and the auto-memory `exec-review-open-design-gaps` with the result.

---

## Self-Review Notes

- Spec coverage: A1 → Task 1; A2 (list content, unmerged-only, sharp-edged rule) → Task 2; validation-by-regeneration → Task 3. Grill decisions all encoded.
- Existing callers of `build_review_prompt` / `run_cross_review` keep working (new params default to empty).
- Type consistency: `foreign_files: tuple[tuple[str, tuple[str, ...]], ...]` used identically in prompts.py, review.py, loop.py and all tests.
