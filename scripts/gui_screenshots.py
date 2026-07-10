"""Generate the two README screenshots of ``spar gui`` headlessly.

Runs entirely offscreen (``QT_QPA_PLATFORM=offscreen``) against a throwaway
project directory seeded with realistic-looking state -- no live model calls,
no network, fully reproducible. Two shots:

* ``docs/img/gui-exec.png``  -- mid-execution: a live-log transcript and a
  task board with a merged/reviewing/pending task.
* ``docs/img/gui-gate.png``  -- a ``final_merge`` consensus gate pending,
  showing the GatePanel's Accept/Abort buttons and a diffstat summary.

Usage::

    .venv/bin/python scripts/gui_screenshots.py

Writes (and overwrites) the two PNGs under ``docs/img/`` and asserts each is
a non-trivial, correctly-sized image before exiting.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Must be set before PySide6/Qt is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spar.exec.state import ExecState, ExecStateStore, TaskState  # noqa: E402
from spar.exec.tasklist import Task  # noqa: E402
from spar.gui.app import MainWindow  # noqa: E402

IMG_DIR = REPO_ROOT / "docs" / "img"
WINDOW_SIZE = QSize(1400, 860)
MIN_PNG_BYTES = 20_000


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_LIVE_LOG_LINES = [
    "[spar-log] spar exec: sequential FSM executor starting (3 tasks)",
    "[claude r0] proposing initial architecture split across 3 tasks",
    "[codex r0] counter-proposal: shared interface module before task split",
    "[claude r1] agreed -- interface module folded into t1",
    "[spar-log] consensus reached after 2 rounds, plan accepted",
    "[claude t1 impl] exec: implementing shared parser interface (t1)",
    "[claude t1 impl] exec: g++ -std=c++20 -Wall -Wextra -c parser.cpp -o parser.o",
    "[codex t1 review] exec: reviewing t1 diff (parser.cpp, parser.h)",
    "[codex t1 review] remark: missing null-check on malformed header (MUST)",
    "[claude t1 impl] exec: addressed remark, added guard clause",
    "[codex t1 review] exec: re-review clean, no remaining remarks",
    "[spar-log] t1 merged into spar/integration",
    "[codex t2 impl] exec: implementing tokenizer state machine (t2)",
    "[codex t2 impl] exec: g++ -std=c++20 -Wall -Wextra -c tokenizer.cpp -o tokenizer.o",
    "[claude t2 review] exec: reviewing t2 diff (tokenizer.cpp)",
    "[claude t2 review] remark: unhandled UTF-8 continuation byte (USER)",
    "[codex t2 impl] exec: fixed continuation-byte handling",
    "[claude t2 review] exec: second pass in progress",
    "[spar-log] t3 blocked on t2, waiting",
    "[spar-log] spar exec: 1/3 tasks merged, 1 in review, 1 pending",
]

_EXEC_JSON_TASKS = [
    Task(
        id="t1",
        description="Shared parser interface module",
        side="claude",
        model="sonnet",
        review_model="gpt-5.5",
        deps=(),
        files=("parser.h", "parser.cpp"),
        test="ctest -R parser",
    ),
    Task(
        id="t2",
        description="Tokenizer state machine",
        side="codex",
        model="gpt-5.5",
        review_model="sonnet",
        deps=("t1",),
        files=("tokenizer.h", "tokenizer.cpp"),
        test="ctest -R tokenizer",
    ),
    Task(
        id="t3",
        description="AST builder + integration tests",
        side="claude",
        model="sonnet",
        review_model="gpt-5.5",
        deps=("t2",),
        files=("ast.h", "ast.cpp"),
        test="ctest -R ast",
    ),
]

_GATE_SUMMARY = (
    "final Test passed: 214/214 (ctest, 3 suites)\n"
    " diff --stat spar/integration..spar/target\n"
    "  parser.cpp    | 118 ++++++++++++++++++++++++\n"
    "  tokenizer.cpp | 96 +++++++++++++++++++++\n"
    "  ast.cpp       | 142 ++++++++++++++++++++++++++++\n"
    "  3 files changed, 356 insertions(+)"
)


def _build_exec_state(*, with_gate: bool) -> ExecState:
    tasks: dict[str, TaskState] = {}
    for task, status in zip(_EXEC_JSON_TASKS, ["merged", "review", "pending"]):
        tasks[task.id] = TaskState(task=task, status=status)  # type: ignore[arg-type]

    state = ExecState(
        phase="execution",
        target_branch="spar/target",
        target_base_oid="abc1234",
        integration_branch="spar/integration",
        tasks=tasks,
    )
    if with_gate:
        state.pending_gate = {
            "name": "final_merge",
            "options": ["accept", "abort"],
            "context": {"summary": _GATE_SUMMARY},
        }
    return state


def _seed_project(project_dir: Path, *, with_gate: bool) -> None:
    spar_dir = project_dir / ".spar"
    spar_dir.mkdir(parents=True, exist_ok=True)

    live_log = spar_dir / "live.log"
    live_log.write_text("\n".join(_LIVE_LOG_LINES) + "\n", encoding="utf-8")

    ExecStateStore(spar_dir).save(_build_exec_state(with_gate=with_gate))


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _capture(app: QApplication, project_dir: Path, out_path: Path) -> None:
    window = MainWindow(project_dir)
    window.resize(WINDOW_SIZE)

    # Force-feed the live log (bypassing the 250ms poll timer -- same
    # technique tests/test_gui_stream.py uses) and refresh the side pane's
    # task board / gate panel from exec.json.
    window.tailer.poll()
    window.side_pane.refresh()

    for _ in range(5):
        app.processEvents()

    window.show()
    for _ in range(10):
        app.processEvents()

    pixmap = window.grab()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    ok = pixmap.save(str(out_path), "PNG")
    window.close()

    assert ok, f"failed to save {out_path}"
    assert out_path.stat().st_size > MIN_PNG_BYTES, (
        f"{out_path} is only {out_path.stat().st_size} bytes (expected > {MIN_PNG_BYTES})"
    )
    assert pixmap.width() == WINDOW_SIZE.width(), pixmap.width()
    assert pixmap.height() == WINDOW_SIZE.height(), pixmap.height()
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes, {pixmap.width()}x{pixmap.height()})")


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv[:1])

    with tempfile.TemporaryDirectory(prefix="spar-gui-shots-") as tmp:
        tmp_path = Path(tmp)

        exec_dir = tmp_path / "exec-shot"
        exec_dir.mkdir()
        _seed_project(exec_dir, with_gate=False)
        _capture(app, exec_dir, IMG_DIR / "gui-exec.png")

        gate_dir = tmp_path / "gate-shot"
        gate_dir.mkdir()
        _seed_project(gate_dir, with_gate=True)
        _capture(app, gate_dir, IMG_DIR / "gui-gate.png")


if __name__ == "__main__":
    main()
