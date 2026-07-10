"""``spar status`` — read-only projection of debate/execution state to a dict.

Pure function, no I/O side effects beyond reading the state files under
``spar_dir``. The CLI layer (``spar/cli.py``) is a thin wrapper that calls
this and prints the result as JSON.
"""

from __future__ import annotations

from pathlib import Path

from spar.exec.state import ExecStateStore
from spar.state import StateStore

__all__ = ["build_status"]


def build_status(spar_dir: Path) -> dict:
    """Return the current debate/execution status as a plain dict.

    Exec state (``exec.json``) takes precedence over debate state
    (``session.json``) when both exist — once ``spar exec`` has started,
    it is the authoritative source of truth. With neither present (a fresh
    repo, or one that has not yet run a debate), every field is ``None``/
    empty and this is not an error.
    """
    spar_dir = Path(spar_dir)
    artifact_path = spar_dir / "artifact.md"
    artifact = str(artifact_path) if artifact_path.exists() else None

    exec_store = ExecStateStore(spar_dir)
    if exec_store.exists():
        state = exec_store.load()
        tasks = {
            task_id: {
                "status": task_state.status,
                "side": task_state.task.side,
                "model": task_state.task.model,
                "review_model": task_state.task.review_model,
            }
            for task_id, task_state in state.tasks.items()
        }
        return {
            "phase": state.phase,
            "pending_gate": state.pending_gate,
            "tasks": tasks,
            "artifact": artifact,
            "branches": {
                "target": state.target_branch,
                "integration": state.integration_branch,
            },
        }

    debate_store = StateStore(spar_dir)
    if debate_store.exists():
        state = debate_store.load()
        return {
            "phase": "debate",
            "pending_gate": state.pending_gate,
            "tasks": {},
            "artifact": artifact,
            "branches": None,
        }

    return {
        "phase": None,
        "pending_gate": None,
        "tasks": {},
        "artifact": None,
        "branches": None,
    }
