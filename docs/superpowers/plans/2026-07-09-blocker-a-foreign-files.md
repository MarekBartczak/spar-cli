# Blocker A — Foreign Files & Planning Invariants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-Task isolation work for interdependent tasks: the planner learns two planning invariants (A1), and the reviewer learns which absent files are legitimately "foreign" (A2), so scaffold/build-config tasks stop drawing unsatisfiable MUSTs and unpassable per-Task tests.

**Architecture:** Two prompt-level changes, one data-plumbing change. A1 extends the `--tasks` planning contract in `spar/orchestrator.py` (cross-reference rule, per-Task test satisfiability, optional `test=` field surfaced in the grammar). A2 threads TWO lists from `Executor._run_task` through `run_cross_review` into `build_review_prompt`: *foreign files* (file scopes of other, not-yet-merged Tasks — may be legitimately absent) and *merged files* (actual paths already merged into the Integration branch — present on the task branch though not in the diff). The review-protocol rule's sharp edge accounts for both plus pre-existing repository files: a reference is a defect only if it matches none of {diff, foreign list, merged list, a file already present in the repository}. No parser enforcement, no engine semantics changes.

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
    # the escape hatch is closed: omitted test= means the GLOBAL command
    # gates the merge and must itself be satisfiable on the branch
    assert "GLOBAL test command gates the task" in text
    assert "you MUST give a narrower test=" in text
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
        "- test (optional) is a shell command gating THIS task's merge. "
        "OMITTING it means the GLOBAL test command gates the task instead — "
        "omit it ONLY when the global command can pass on the task's own "
        "branch; otherwise you MUST give a narrower test=.",
        "",
        "Isolation invariants (each task is implemented, reviewed and tested "
        "on its own branch containing ONLY its merged deps):",
        "- Cross-reference rule: if a task's file content references files "
        "owned by another task, it MUST list that task in deps. A "
        "build-config/scaffold task that wires the project together "
        "(build files, manifests, top-level config) therefore comes LAST, "
        "depending on every task whose files it references.",
        "- Per-task test satisfiability: whatever command gates a task's "
        "merge (its test=, or the global test command when test= is "
        "omitted) must be runnable on the task's own branch, i.e. judged "
        "against only its deps, not the finished project. Give partial "
        "states a compile/lint-level check; reserve the full build/suite "
        "for the final task and the final Test phase.",
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
- Modify: `spar/exec/gitops.py` (new `present_files` helper — like `changed_files` but excluding deletions)
- Modify: `spar/exec/loop.py` (`Executor._run_task` — compute the lists, pass to `run_cross_review`)
- Test: `tests/test_exec_prompts.py`, `tests/test_exec_review.py`, `tests/test_exec_gitops.py`, `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `TaskState.status` / `Task.files` from `spar/exec/state.py`; `run_cross_review(**kwargs)` from Task-1-independent existing code.
- Produces:
  - `gitops.present_files(repo, base, ref) -> tuple[str, ...]` — like `changed_files` but with `--diff-filter=d`, so paths DELETED between `base` and `ref` are excluded (the merged-files list must contain only files actually present on the branch).
  - `build_review_prompt(task, diff_text, open_remarks, foreign_files=(), merged_files=())` where `foreign_files: tuple[tuple[str, tuple[str, ...]], ...]` — `(task_id, file_globs)` pairs ordered by task id — and `merged_files: tuple[str, ...]` — actual paths already merged into the Integration branch AND still present there (from `gitops.present_files(repo, target_base_oid, integration_branch)`), NOT globs.
  - `run_cross_review(..., foreign_files=(), merged_files=())` — same types, defaulting to empty (existing callers unaffected).

- [ ] **Step 1: Write the failing prompt tests**

Add to `tests/test_exec_prompts.py`:

```python
def test_review_prompt_lists_foreign_and_merged_files():
    p = build_review_prompt(
        T,
        "diff --git a/CMakeLists.txt ...",
        [],
        foreign_files=(("t3", ("src/main.cpp",)), ("t4", ("tests/*.cpp",))),
        merged_files=("src/factorial.hpp", "src/factorial.cpp"),
    )
    # the foreign section names each scope with its owning task
    assert "Files owned by other, not-yet-merged tasks" in p
    assert "t3: src/main.cpp" in p
    assert "t4: tests/*.cpp" in p
    assert "their absence is NOT a defect by itself" in p
    # ...but a hard reference breaking THIS task's isolation is flagged
    assert "HARD-reference" in p
    assert "plan-ordering defect" in p
    # the merged section lists real paths present on the branch
    assert "Files already merged from earlier tasks" in p
    assert "src/factorial.hpp" in p
    # the defect rule accounts for diff, both lists, AND pre-existing files
    assert "already present in the repository" in p


def test_review_prompt_omits_context_sections_when_empty():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "not-yet-merged tasks" not in p
    assert "already merged from earlier tasks" not in p
    # ...but the missing-file protocol rule is PERMANENT: a standalone first
    # task must not draw MUSTs about files that pre-date the run
    assert "already present in the repository" in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_exec_prompts.py -v -k "foreign or context_sections"`
Expected: first test FAILS with `TypeError: build_review_prompt() got an unexpected keyword argument 'foreign_files'`; second PASSES trivially (guard that the sections are conditional — keep it).

- [ ] **Step 3: Implement the prompt change**

In `spar/exec/prompts.py`, change `build_review_prompt`:

```python
def build_review_prompt(
    task: Task,
    diff_text: str,
    open_remarks: list[StateRemark],
    foreign_files: tuple[tuple[str, tuple[str, ...]], ...] = (),
    merged_files: tuple[str, ...] = (),
) -> str:
```

and before `protocol = _REVIEW_PROTOCOL_BLOCK` add:

```python
    context_section = ""
    context_lines: list[str] = []
    if foreign_files:
        context_lines.append(
            "Files owned by other, not-yet-merged tasks (they arrive with "
            "later merges and may be ABSENT on this branch): do NOT require "
            "this task to create them — their absence is NOT a defect by "
            "itself; check that references to them match these planned "
            "names/paths. HOWEVER, if this task's own files HARD-reference a "
            "foreign file (import/include/link/build-source) so that this "
            "task cannot build or pass its test on THIS branch, raise a "
            "[MUST] naming the missing dependency — that is a plan-ordering "
            "defect (the task should have depended on the owner)."
        )
        for task_id, globs in foreign_files:
            context_lines.append(f"  {task_id}: {', '.join(globs)}")
    if merged_files:
        context_lines.append(
            "Files already merged from earlier tasks (present on this branch "
            "even though they do not appear in the diff above):"
        )
        for path in merged_files:
            context_lines.append(f"  {path}")
    if context_lines:
        context_section = "\n" + "\n".join(context_lines) + "\n"
```

and extend `_REVIEW_PROTOCOL_BLOCK` with a PERMANENT missing-file rule (always
present, independent of the conditional context sections — a standalone first
task must also not draw MUSTs about files that pre-date the run; the
hard-reference plan-ordering defect from the foreign section is explicitly
exempt so the two rules cannot suppress each other):

```python
_REVIEW_PROTOCOL_BLOCK = """\
End your reply with EXACTLY ONE verdict block, using this syntax verbatim:

<verdict>
status: CONTINUE
remarks:
- [MUST] <a blocking concern that must be fixed>
- [NICE] <an optional, non-blocking suggestion>
</verdict>

Protocol for review:
- Do not edit the code — this is read-only. You are reviewing only, not implementing.
- In `remarks:` raise new concerns, tagged `[MUST]` (blocking) or `[NICE]` (optional).
- Treat a reference to a MISSING file as a defect only if it matches none of:
  the diff, the context lists above (if any), or a file already present in the
  repository. This does NOT override the hard-reference rule of the
  foreign-files section (a plan-ordering defect stays a [MUST] even though the
  file matches the foreign list).
- Use `status: DONE` only if you have NO blocking `[MUST]` remarks remaining.
- Use `status: CONTINUE` if you have open `[MUST]`/`[NICE]` remarks to raise.
"""
```

and include `{context_section}` in the returned f-string between the diff and `{remarks_section}`:

```python
    return f"""\
Task ID: {task.id}
Description: {task.description}

Review the following diff (read-only — you must NOT edit code):

{diff_text}
{context_section}{remarks_section}

{protocol}\
"""
```

(Note the newline handling: `context_section` is empty or starts/ends with `\n`, so the empty case renders identically to today's output except for one blank line after the diff — the existing `test_exec_prompts.py` assertions are substring-based and survive this.)

- [ ] **Step 4: Run prompt tests to verify they pass**

Run: `python3 -m pytest tests/test_exec_prompts.py -q`
Expected: PASS (all).

- [ ] **Step 5: Write the failing plumbing test (review loop)**

Add to `tests/test_exec_review.py`:

```python
def test_foreign_and_merged_files_reach_the_review_prompt(env):
    task = _task(files=("work.py",))
    impl_steps = []
    review_steps = [Step(vblock("DONE"))]
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps,
        foreign_files=(("t9", ("src/*.cpp",)),),
        merged_files=("lib/util.py",),
    )
    assert "t9: src/*.cpp" in review.calls[0]["prompt"]
    assert "lib/util.py" in review.calls[0]["prompt"]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python3 -m pytest tests/test_exec_review.py::test_foreign_and_merged_files_reach_the_review_prompt -v`
Expected: FAIL with `TypeError: run_cross_review() got an unexpected keyword argument 'foreign_files'`.

- [ ] **Step 7: Thread the parameters through `run_cross_review`**

In `spar/exec/review.py` add the keyword-only parameters (after `rounds_gate`):

```python
    max_rounds: int = 0,
    rounds_gate=None,
    foreign_files: tuple[tuple[str, tuple[str, ...]], ...] = (),
    merged_files: tuple[str, ...] = (),
```

and pass them at the reviewer-turn prompt build (line ~218):

```python
        prompt = build_review_prompt(
            task, diff_text, list(task_state.pending_remarks),
            foreign_files=foreign_files,
            merged_files=merged_files,
        )
```

- [ ] **Step 8: Run it to verify it passes**

Run: `python3 -m pytest tests/test_exec_review.py -q`
Expected: PASS (all).

- [ ] **Step 8a: Write the failing gitops test (deletion regression)**

`tests/test_exec_gitops.py` imports functions DIRECTLY from `spar.exec.gitops` (no module binding) and has a module-level `_run(repo, *args)` git helper — extend the import list with `present_files` and `rev_parse`, then add:

```python
def test_present_files_excludes_deletions(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "master")
    _run(repo, "config", "user.email", "t@t")
    _run(repo, "config", "user.name", "t")
    (repo / "kept.txt").write_text("x\n", encoding="utf-8")
    (repo / "doomed.txt").write_text("y\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "base")
    base = rev_parse(repo, "HEAD")

    (repo / "new.txt").write_text("z\n", encoding="utf-8")
    (repo / "doomed.txt").unlink()
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "change")

    # changed_files reports the deletion; present_files must not
    assert "doomed.txt" in changed_files(repo, base, "HEAD")
    assert present_files(repo, base, "HEAD") == ("new.txt",)
```

- [ ] **Step 8b: Run it, then implement `present_files`**

Run: `python3 -m pytest tests/test_exec_gitops.py::test_present_files_excludes_deletions -v`
Expected: collection ERROR — `ImportError: cannot import name 'present_files'` (the test file imports it directly at module level).

Add to `spar/exec/gitops.py` (below `changed_files`):

```python
def present_files(repo: Path, base: str, ref: str) -> tuple[str, ...]:
    """Files changed between ``base`` and ``ref`` that still EXIST at ``ref``.

    Like :func:`changed_files` but with ``--diff-filter=d``: a path deleted
    between the two refs is excluded, so callers can treat the result as
    "files present on ``ref``" (the review-context merged-files list must
    never vouch for a file a prior task deleted).
    """
    result = _run_ok(
        repo, "-c", "core.quotePath=false", "diff", "--name-only",
        "--diff-filter=d", f"{base}..{ref}",
    )
    lines = result.stdout.strip("\n").split("\n")
    return tuple(line for line in lines if line)
```

Run: `python3 -m pytest tests/test_exec_gitops.py -q`
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
    # B's first call reviewed t1: t2 was pending -> listed as foreign;
    # nothing merged yet -> no merged-files section
    assert "t2: work2.py" in adapters["B"].calls[0]["prompt"]
    assert "already merged from earlier tasks" not in adapters["B"].calls[0]["prompt"]
    # A's second call reviewed t2: t1 already merged -> NOT foreign, but its
    # actual file appears in the merged-files section
    assert "t1: work1.py" not in adapters["A"].calls[1]["prompt"]
    assert "work1.py" in adapters["A"].calls[1]["prompt"]
```

- [ ] **Step 10: Run it to verify it fails**

Run: `python3 -m pytest tests/test_exec_loop.py::test_reviewer_prompt_lists_unmerged_tasks_files_only -v`
Expected: FAIL on the first prompt assertion (no foreign section yet).

- [ ] **Step 11: Compute both lists in `Executor._run_task`**

In `spar/exec/loop.py`, inside `_run_task` (before the `while True:` loop, after `reviewer = ...`), add:

```python
        # Review context (A2). Foreign files: file scopes (globs) of other,
        # not-yet-merged tasks — legitimately absent on the task branch.
        # Merged files: ACTUAL paths already merged into integration — present
        # on the branch though invisible in the reviewer's diff. Together they
        # stop the reviewer from mistaking either for a missing-file defect.
        # Sequential execution: statuses cannot change while this task runs,
        # so compute once.
        foreign_files = tuple(
            (other.task.id, other.task.files)
            for other in sorted(state.tasks.values(), key=lambda t: t.task.id)
            if other.task.id != task.id and other.status != "merged"
        )
        merged_files = gitops.present_files(
            self.repo, state.target_base_oid, state.integration_branch
        )
```

(`present_files`, not `changed_files`: a file DELETED by an earlier task must
not be vouched for as present — deletion regression, review round 2.)

and pass them to `run_cross_review`:

```python
                run_cross_review(
                    ...,
                    max_rounds=self.execution.max_review_rounds,
                    rounds_gate=self._review_rounds_gate(state),
                    foreign_files=foreign_files,
                    merged_files=merged_files,
                )
```

- [ ] **Step 12: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (0 failures; ~310 passed, 2 skipped).

- [ ] **Step 13: Commit**

```bash
git add spar/exec/prompts.py spar/exec/review.py spar/exec/gitops.py spar/exec/loop.py \
        tests/test_exec_prompts.py tests/test_exec_review.py tests/test_exec_gitops.py \
        tests/test_exec_loop.py
git commit -m "feat(exec): review context — foreign + merged file lists (blocker A2)"
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

- Spec coverage: A1 → Task 1; A2 (foreign + merged lists, sharp-edged rule incl. pre-existing files) → Task 2; validation-by-regeneration → Task 3. Grill decisions all encoded.
- Existing callers of `build_review_prompt` / `run_cross_review` keep working (new params default to empty).
- Type consistency: `foreign_files: tuple[tuple[str, tuple[str, ...]], ...]` and `merged_files: tuple[str, ...]` used identically in prompts.py, review.py, loop.py and all tests.

## Review history

- **Round 1** (codex gpt-5.5): Verdict CONTINUE. #1 [MUST] **accepted** — the
  "reference outside diff∪foreign = defect" rule was unsound: files from
  already-merged dependency tasks (and files pre-existing in the repo) are
  neither in the diff nor foreign, so a valid last scaffold task would still
  draw false MUSTs. Fix applied to the plan body: review prompt now carries a
  second list (*merged files*, actual paths from
  `gitops.changed_files(repo, target_base_oid, integration_branch)`) and the
  defect rule requires a reference to match none of {diff, foreign list,
  merged list, file already present in the repository}. CONTEXT.md glossary
  updated accordingly.
- **Round 2** (codex gpt-5.5): Verdict CONTINUE. Confirmed #1 addressed.
  #2 [MUST] **accepted** — `changed_files` (`git diff --name-only`) also
  reports DELETED paths, so a file removed by an earlier task would be
  vouched for as "present". Fix applied: new `gitops.present_files`
  (`--diff-filter=d`) used for the merged-files list, with a deletion
  regression test (Task 2 Steps 8a/8b).
- **Round 3** (codex gpt-5.5): Verdict CONTINUE. #3 [MUST] **accepted** —
  the foreign-files rule as written could mask a plan-ordering violation
  (a hard import/include/link of a not-yet-merged file, which A1 forbids).
  Fix applied: foreign-section wording narrowed — absence alone is not a
  defect and the task is never required to create foreign files, but a
  HARD-reference that breaks this task's own build/test on its branch draws
  a [MUST] naming the missing dependency (plan-ordering defect). Prompt
  tests extended; CONTEXT.md glossary updated. #4 [NICE] **accepted** —
  gitops test snippet adapted to the file's direct-import + `_run` helper
  conventions.
- **Round 4** (codex gpt-5.5): Verdict CONTINUE. Confirmed #1–#4 addressed.
  #5 [MUST] **accepted** — omitting `test=` silently falls back to the
  GLOBAL test command (`Executor._task_test_cmd`), an escape hatch the
  contract left open. Fix applied: contract wording now states the omission
  semantics explicitly and requires a narrower `test=` whenever the global
  command cannot pass on the task's branch; the satisfiability rule now
  covers "whatever command gates the merge". Contract test extended.
- **Round 5** (codex gpt-5.5, verification round): Verdict CONTINUE.
  Confirmed #5 addressed. #6 [MUST] **accepted** — the generic missing-file
  rule could suppress the #3 hard-reference rule (the file matches the
  foreign list); the rule now explicitly exempts the plan-ordering defect.
  #7 [MUST] **accepted** — the missing-file rule lived inside
  `if context_lines`, vanishing for a standalone/first task; moved into
  `_REVIEW_PROTOCOL_BLOCK` (permanent), context sections stay conditional;
  empty-case prompt test extended. #8 [NICE] **accepted** — red-step
  expectation corrected to a collection-time ImportError (direct imports).
