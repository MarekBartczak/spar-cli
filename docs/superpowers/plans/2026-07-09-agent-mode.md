# Agent Mode (headless gates) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A host agent (Claude Code / Codex) can drive a full spar run non-interactively: every user gate becomes persist-state + exit 10, the decision returns via `--gate` on resume, state is readable via `spar status --json`, and requirements can arrive as a file.

**Architecture (per ADR 0003 + grill session):** Exit-and-resume gates. A new `GatePending` control exception is raised by headless gate implementations (`HeadlessGate` for the debate, `HeadlessExecGate` for execution); the top-level runners catch it, persist a `pending_gate` record into the state file, and return exit code **10**. Resume commands (`spar --continue --headless --gate X`, `spar exec --continue --headless --gate X`) validate the decision against the persisted `pending_gate`, preload it into the headless gate (consumed exactly once), clear `pending_gate`, and re-drive the loop — which deterministically re-reaches the same gate and consumes the decision. Recovery gate in headless defaults to `repeat` (no pending). `spar status --json` derives everything from the state files. Interactive stdin gates are untouched (headless is opt-in via `--headless`).

**Gate → decision matrix (grill session decisions):**

| Gate | Where | Options | `--gate` values |
|---|---|---|---|
| consensus | debate | accept / remarks / abort | `accept`, `remarks:<file>`, `abort` |
| rounds_exhausted | debate | accept / extend / abort | `accept`, `extend:<n>`, `abort` |
| recovery | debate | keep / repeat | none — headless auto-answers `repeat` |
| review_rounds | exec | accept / extend / abort | `accept`, `extend:<n>`, `abort` |
| final_merge | exec | accept / abort | `accept`, `abort` |

**Resume re-reach semantics (the subtle part):**
- *consensus / rounds_exhausted*: the debate loop re-derives the condition from persisted state (`is_consensus`, `round >= budget`) — the gate is re-hit naturally, no special reconcile.
- *final_merge*: on resume all tasks are `merged`, the final Test re-runs (idempotent cost accepted in v1), then `_final_merge` hits the gate.
- *review_rounds*: the task sits in status `review` with a live branch/worktree. `_reconcile` must NOT restart such a task: when `state.pending_gate` names gate `review_rounds` and `task_id == ts.task.id`, the task keeps its branch and status. `_run_task` gains a resume path that SKIPS the initial implementer turn (the branch already has content) and re-enters `run_cross_review`; `extend:<n>` re-enters with a FRESH budget of `n` rounds (same semantics as interactive extend: n more rounds from now), `accept` skips the review entirely and proceeds to the per-task test, `abort` exits 5.

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

## Global Constraints

- Headless is opt-in (`--headless`); without it every gate behaves exactly as today (stdin). Zero breaking change.
- Exit codes: existing 0/2/3/4/5/130 keep their meanings; **10 = gate pending** is new and reserved.
- `pending_gate` state fields are backward-tolerant (`data.get(..., None)`) — pre-upgrade state files must load.
- A `--gate` decision that does not match the persisted `pending_gate` (wrong gate, wrong option, no pending gate at all) is a usage error: message + exit 2, state untouched.
- `--gate`/`--headless` on resume: `--gate` requires `--continue` AND `--headless`.
- TDD; conventional commits; NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite green after every task: `python3 -m pytest tests/ -q` (335 passed, 2 skipped before this plan).

---

### Task 1: `pending_gate` in both state files + `GatePending` exception (Sonnet)

**Files:**
- Modify: `spar/state.py` (`DebateState` field + (de)serialization; `_STATE_KEYS`-equivalent list)
- Modify: `spar/exec/state.py` (`ExecState` field + (de)serialization)
- Create: `spar/gates.py` (shared `GatePending` + `--gate` value parser)
- Test: `tests/test_state.py`, `tests/test_exec_state.py`, `tests/test_gates.py` (new)

**Interfaces:**
- Produces:
  - `DebateState.pending_gate: dict | None = None` and `ExecState.pending_gate: dict | None = None`; serialized as-is; loaded via `data.get("pending_gate")` (tolerant).
  - `spar/gates.py`:

    ```python
    """Shared headless-gate plumbing: the control exception and --gate parsing."""

    from __future__ import annotations

    from dataclasses import dataclass, field


    class GateParseError(Exception):
        """Raised for an unparsable or mismatched --gate value."""


    class GatePending(Exception):
        """Control-flow signal: a user gate was reached in headless mode.

        Carries everything the runner must persist so ``spar status --json``
        can describe the gate and a resume can validate the decision.
        """

        def __init__(self, name: str, options: list[str], context: dict | None = None) -> None:
            super().__init__(f"gate pending: {name}")
            self.name = name
            self.options = list(options)
            self.context = dict(context or {})

        def to_state(self) -> dict:
            return {"name": self.name, "options": self.options, "context": self.context}


    @dataclass(frozen=True)
    class GateChoice:
        """A parsed --gate value, not yet validated against a pending gate."""

        action: str  # "accept" | "abort" | "extend" | "remarks"
        extra_rounds: int = 0  # > 0 iff action == "extend"
        remarks: tuple[str, ...] = ()  # non-empty iff action == "remarks"


    def parse_gate_value(value: str) -> GateChoice:
        """Parse ``accept`` / ``abort`` / ``extend:<n>`` / ``remarks:<file>``.

        ``remarks:<file>`` reads the file (UTF-8); each non-empty line is one
        remark. Raises :class:`GateParseError` on bad syntax, n < 1, an
        unreadable/empty remarks file.
        """
        if value == "accept":
            return GateChoice(action="accept")
        if value == "abort":
            return GateChoice(action="abort")
        if value.startswith("extend:"):
            raw = value[len("extend:"):]
            try:
                n = int(raw)
            except ValueError:
                raise GateParseError(f"extend needs an integer, got {raw!r}")
            if n < 1:
                raise GateParseError(f"extend needs a positive integer, got {n}")
            return GateChoice(action="extend", extra_rounds=n)
        if value.startswith("remarks:"):
            path = value[len("remarks:"):]
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as exc:
                raise GateParseError(f"cannot read remarks file {path!r}: {exc}")
            remarks = tuple(ln.strip() for ln in text.splitlines() if ln.strip())
            if not remarks:
                raise GateParseError(f"remarks file {path!r} contains no remarks")
            return GateChoice(action="remarks", remarks=remarks)
        raise GateParseError(
            f"unknown --gate value {value!r} (expected accept, abort, extend:<n> or remarks:<file>)"
        )


    def validate_choice(choice: GateChoice, pending: dict | None) -> None:
        """Check ``choice`` against the persisted pending-gate record.

        Raises GateParseError when there is no pending gate or the action is
        not among the gate's options.
        """
        if pending is None:
            raise GateParseError("no gate is pending; --gate is not applicable")
        if choice.action not in pending.get("options", []):
            raise GateParseError(
                f"gate {pending.get('name')!r} accepts {pending.get('options')}, "
                f"got {choice.action!r}"
            )
    ```

- [ ] **Step 1: Write failing tests** — `tests/test_gates.py` (new file): parse each of the four forms (remarks via a tmp file with two lines + a blank), each error case (`extend:x`, `extend:0`, `remarks:/nonexistent`, empty remarks file, junk value), `validate_choice` happy + no-pending + wrong-option. `tests/test_state.py` / `tests/test_exec_state.py`: `pending_gate` round-trips through save/load; a state JSON WITHOUT the key loads with `pending_gate is None` (mirror `test_fix_tasks_opened_missing_key_defaults_to_zero`).
- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: spar.gates`, `TypeError` on the state fields).
- [ ] **Step 3: Implement** — `spar/gates.py` as above; add `pending_gate: dict | None = None` to both dataclasses; serialize (`"pending_gate": state.pending_gate`) and load tolerantly (`data.get("pending_gate")`), NOT in the required-keys lists.
- [ ] **Step 4: Run suite — PASS.**
- [ ] **Step 5: Commit** — `feat(gates): GatePending, --gate parsing, pending_gate state fields`

---

### Task 2: headless execution gates (Opus)

**Files:**
- Create: `spar/exec/headless.py` (`HeadlessExecGate`)
- Modify: `spar/exec/loop.py` (`Executor._guarded` catches `GatePending`; `_reconcile` pending-gate awareness; `_run_task` review-resume path; `run_continue` decision preload)
- Test: `tests/test_exec_loop.py`

**Interfaces:**
- Consumes: `GatePending`, `GateChoice` from `spar/gates.py`; `GateDecision` from `spar/orchestrator.py`.
- Produces:
  - `HeadlessExecGate(preloaded: tuple[str, GateDecision] | None = None)` implementing `ExecGate`. Each gate method: if `preloaded` is set AND `preloaded[0]` == this gate's name → consume it (set to None) and return the `GateDecision`; else raise `GatePending(name, options, context)`:
    - `final_merge_gate(summary)` → name `"final_merge"`, options `["accept", "abort"]`, context `{"summary": summary}`.
    - `review_rounds_exhausted_gate(task_id, rounds, pending)` → name `"review_rounds"`, options `["accept", "extend", "abort"]`, context `{"task_id": task_id, "rounds": rounds, "open_remarks": [{"id": r.remark_id, "severity": r.severity.name, "author": r.author, "text": r.text} for r in pending]}`.
  - `Executor.run_continue(gate_choice: GateChoice | None = None)` — new optional parameter (default None keeps today's signature working). With a choice: load state, `validate_choice(choice, state.pending_gate)` (GateParseError → log + return 2), convert to `GateDecision` (`accept`→accept, `abort`→abort, `extend`→`GateDecision("extend", extra_rounds=n)`), remember `(state.pending_gate["name"], decision)` as the preload for the gate object, clear `state.pending_gate`, save, then proceed as today.
  - `Executor._guarded` gains: `except GatePending as exc: <persist exc.to_state() into the CURRENT state's pending_gate; save; log; return 10>`. The current state object must be reachable — store it on `self._state` at the top of `_run_fresh`/`_run_continue` (mirroring how `_restore_target_checkout` reloads; here use the live object).
  - `_reconcile`: a task in status `review` is NOT restarted when `state.pending_gate` (as loaded, BEFORE clearing) names `review_rounds` with `context.task_id == ts.task.id` — its branch/worktree are kept and it is left in status `review`. All other cases unchanged. To make this work, `run_continue` must reconcile BEFORE clearing `pending_gate` (order: load → reconcile(state) → validate/preload/clear → drive).
  - `_drive`/`_run_task` review-resume: `next_task()` only returns `ready` tasks — add to `_drive`, before `next_task()`: if any task has status `review` (only possible via the pending-gate path), run `self._resume_review_task(state, ts)` for it. `_resume_review_task` mirrors `_run_task` WITHOUT the initial implementer turn and WITHOUT branch/worktree creation (both exist), driving `run_cross_review` → per-task test → merge exactly like `_run_task`'s tail. The preloaded gate decision plays out inside `run_cross_review`'s rounds gate on re-entry:
    - `accept` — `run_cross_review` is SKIPPED entirely (jump to the testing step); the preload is consumed by `_resume_review_task` itself (it checks the preload's gate name/decision before deciding whether to re-enter review).
    - `extend:<n>` — re-enter `run_cross_review` with `max_rounds=n` (fresh budget of n rounds from now) and the normal `rounds_gate` wiring (a headless gate that pends again if n more rounds still don't converge).
    - `abort` — `run_continue` translates to exit 5 directly (log like the interactive abort), no re-entry.
  - Worktree existence on resume: the review-pending task's worktree survived the exit; `_resume_review_task` asserts it exists and re-creates it from the surviving branch when missing (crash between exit and resume): `gitops.add_worktree(...)` guarded by `worktree.exists()`.

- [ ] **Step 1: Failing test — final-merge pends.** Happy 1-task run, `HeadlessExecGate()` as the gate → `ex.run()` returns 10; `store.load().pending_gate["name"] == "final_merge"`; `"accept" in options`; summary in context; repo restored to master (existing helper — integration is clean).
- [ ] **Step 2: Failing test — final-merge resume accept.** After the pend: build a second executor over the same store with `HeadlessExecGate()` and call `ex2.run_continue(gate_choice=GateChoice(action="accept"))` → rc 0, phase done, merged into master, `pending_gate` cleared.
- [ ] **Step 3: Failing test — review-rounds pends and resumes.** `max_review_rounds=1`, reviewer scripted to never DONE → rc 10 with `pending_gate.name == "review_rounds"`, task still in status `review`, its branch alive. Resume with `extend:1` and a reviewer that now DONEs → rc continues to final-merge pend (10) — assert task merged and new pending gate is `final_merge`. Also resume-with-`accept` variant: review skipped, task tested + merged.
- [ ] **Step 4: Failing test — mismatched decision.** Pending `final_merge`, resume with `extend:2` → rc 2, `pending_gate` still set.
- [ ] **Step 5: Implement** per the interfaces above. Order inside `run_continue`: load → `self._state = state` → `_reconcile(state)` (pending-gate aware) → if `gate_choice`: validate → preload → clear → save → `_drive`.
- [ ] **Step 6: Full suite — PASS.**
- [ ] **Step 7: Commit** — `feat(exec): headless gates — exit 10 + --gate resume (final merge, review rounds)`

---

### Task 3: headless debate gates (Sonnet)

**Files:**
- Create: `spar/headless.py` (`HeadlessGate` for the debate `UserGate`)
- Modify: `spar/orchestrator.py` (catch `GatePending` in the same place `_Abort` is caught for `run_new`/`run_continue`; preload on `run_continue`; recovery gate headless default)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `GatePending`, `GateChoice`, `validate_choice`; `DebateState.pending_gate` from Task 1.
- Produces:
  - `HeadlessGate(preloaded: tuple[str, GateDecision] | None = None)` implementing `UserGate`:
    - `consensus_gate(artifact_path, nice_backlog)` → name `"consensus"`, options `["accept", "remarks", "abort"]`, context `{"artifact": str(artifact_path), "nice_backlog": [...remarks dicts...]}`.
    - `rounds_exhausted_gate(artifact_path, pending)` → name `"rounds_exhausted"`, options `["accept", "extend", "abort"]`, context `{"artifact": str(artifact_path), "open_remarks": [...]}`.
    - `recovery_gate(...)` → returns `"repeat"` unconditionally (grill decision: safe default, never pends).
  - `Orchestrator.run_new`/`run_continue` catch `GatePending` exactly like `_Abort`: persist `exc.to_state()` into `state.pending_gate`, `store.save(state)`, log, return 10. (The state object is in scope in `_run_new`/`_run_continue`; catch there, not in the public wrappers.)
  - `Orchestrator.run_continue(gate_choice: GateChoice | None = None)`: load → validate against `state.pending_gate` (GateParseError → log + 2) → convert (`remarks` → `GateDecision(action="remarks", remarks=choice.remarks)`) → preload into the gate object (the orchestrator owns `self.gate`; when it is a `HeadlessGate`, set its preload) → clear + save → resume the loop. Consensus/rounds conditions re-derive from state, so the gate is re-hit and consumes the preload.

- [ ] **Step 1: Failing test — consensus pends.** Drive a scripted debate to consensus with `HeadlessGate()` → rc 10, `pending_gate.name == "consensus"`, artifact path in context.
- [ ] **Step 2: Failing test — resume with remarks.** `run_continue(gate_choice=GateChoice(action="remarks", remarks=("tighten the API",)))` → remark injected as USER severity (assert in state), debate resumes (scripted sides re-AGREE), next consensus pends again (rc 10).
- [ ] **Step 3: Failing test — rounds-exhausted pends + extend resume.** Budget-exhausted scripted debate → rc 10 `rounds_exhausted`; resume `extend:1` → one more round runs.
- [ ] **Step 4: Failing test — recovery never pends.** Interrupted-turn state + `HeadlessGate()` on `run_continue` → the turn is repeated (assert adapter called again), no rc 10 from recovery.
- [ ] **Step 5: Implement.**
- [ ] **Step 6: Full suite — PASS.**
- [ ] **Step 7: Commit** — `feat(orchestrator): headless debate gates — consensus/rounds pend, recovery auto-repeats`

---

### Task 4: CLI — `--headless`, `--gate`, `--task-file`, `spar status --json` (Sonnet)

**Files:**
- Modify: `spar/cli.py` (both parsers, gate wiring, new `status` subcommand routing)
- Create: `spar/status.py` (state → JSON dict)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `HeadlessGate`, `HeadlessExecGate`, `parse_gate_value`, `GateParseError`; the two state stores.
- Produces:
  - Main parser: `--headless` (store_true), `--gate VALUE` (str), `--task-file PATH`. Validation: `--gate` requires `--continue` and `--headless` (else `parser.error`); `--task-file` mutually exclusive with the positional prompt (and satisfies the "either prompt or --continue" rule; its content becomes the task prompt).
  - Exec parser: `--headless`, `--gate VALUE` (requires `--continue` + `--headless`).
  - Wiring: `--headless` swaps `ConsoleGate` → `HeadlessGate()` / `ConsoleExecGate` → `HeadlessExecGate()`; a `--gate` value is parsed with `parse_gate_value` (GateParseError → stderr + exit 2) and handed to `run_continue(gate_choice=...)`.
  - `spar status` subcommand (leading token, like `exec`): flag `--json` (required in v1 — plain output can come later). Reads `.spar/exec.json` if present, else `.spar/session.json`; prints the schema agreed in the grill session:

    ```json
    {
      "phase": "debate | execution | test | done",
      "pending_gate": {"name": "...", "options": ["..."], "context": {}},
      "tasks": {"t1": {"status": "merged", "side": "claude", "model": "sonnet"}},
      "artifact": ".spar/artifact.md"
    }
    ```

    Debate-only state: `"phase": "debate"`, `"tasks": {}`. No state at all: `{"phase": null, "pending_gate": null, "tasks": {}, "artifact": null}` with exit 0 (an agent probing a fresh repo is not an error).
  - Exit code 10 propagates from the runners through `main`.

- [ ] **Step 1: Failing CLI tests** — argument validation (`--gate` without `--continue` errors; `--task-file` + prompt errors; task-file content reaches the orchestrator (monkeypatch `_build_orchestrator` like existing cli tests do)), `status --json` on: no state / debate state / exec state with a pending gate (craft state files via the stores).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** (`spar/status.py` pure function `build_status(spar_dir: Path) -> dict` + thin CLI printing `json.dumps(..., ensure_ascii=False, indent=2)`).
- [ ] **Step 4: Full suite — PASS.**
- [ ] **Step 5: Commit** — `feat(cli): --headless/--gate/--task-file and spar status --json`

---

### Task 5: agent protocol doc + host skill (Sonnet)

**Files:**
- Create: `docs/AGENT.md` — the contract: commands, exit codes (incl. 10), the gate matrix table (from this plan's header), `status --json` schema, the canonical driving loop:
  1. grill requirements with the human → write `requirements.md`
  2. `spar --task-file requirements.md --sides claude,codex --first claude --tasks --headless` → rc 10 → `spar status --json` → decide/relay (`--gate accept` | `remarks:file` | `abort`)
  3. `spar exec --headless` → on each rc 10: `status --json` → decide/relay → `spar exec --continue --headless --gate ...`
  4. rc 0 → report the merge summary to the human; rc 4/5/2 → surface verbatim.
- Create: `skills/spar/SKILL.md` — a Claude Code skill (frontmatter `name: spar`, `description: Drive a two-vendor spar run (plan debate + execution + tests) headlessly for a grilled task; use when the user wants spar to implement something`) whose body instructs the agent: prerequisites check (`spar` on PATH or venv path, `.spar/config.toml` exists), the loop above, gate-relay etiquette (ALWAYS show the human the final-merge summary and ask before `--gate accept` on final_merge; consensus/review gates may be auto-decided only if the human pre-authorized), failure surfacing (never bury a non-zero exit).
- Test: none (docs) — but verify every command in AGENT.md against `spar --help` / `spar exec --help` output by running them.

- [ ] **Step 1: Write both docs.**
- [ ] **Step 2: Verify commands** — run `spar --help`, `spar exec --help`, `spar status --json` in a throwaway tmp dir; every flag named in the docs must exist.
- [ ] **Step 3: Commit** — `docs: agent protocol (AGENT.md) + Claude Code skill for driving spar`

---

### Task 6: live headless smoke test (manual, user-driven — no model)

- [ ] **Step 1:** in `/home/marek/P_PROJ/spar_tests` (clean lab): run the AGENT.md loop by hand or let the host agent do it: debate with `--headless --task-file`, `status --json` at the consensus pend, `--gate accept`, `spar exec --headless`, gates via `--gate`, verify rc 10/0 transitions and that interactive mode still works without `--headless`.
- [ ] **Step 2:** record the outcome in `docs/HANDOFF.md` + the auto-memory.

---

## Self-Review Notes

- Grill decisions all encoded: exit-and-resume (Q1), `--headless` + all gates + recovery auto-repeat (Q2), `--gate` grammar + exit 10 (Q3), status schema derived from state (Q4), `--task-file` + docs/skill wrapper (Q5).
- The one genuinely tricky seam — review-rounds pend/resume vs `_reconcile`'s restart policy — is isolated in Task 2 with explicit ordering (reconcile before clearing `pending_gate`) and a dedicated resume path; assigned to Opus.
- Backward compatibility: new state fields tolerant; `run_continue` signatures gain optional params only; no interactive behavior changes without `--headless`.
- Final-merge resume re-runs the final test (documented v1 trade-off; cheap and keeps the gate re-reach trivial).
