"""Persistence for the orchestrator chat session id + metadata (.spar/chat.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChatMeta:
    session_id: str
    model: str
    turn_count: int


def load_chat(path: "str | Path") -> "ChatMeta | None":
    """Load chat metadata; None on missing/unreadable/malformed/no session_id."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        obj = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    model = obj.get("model") if isinstance(obj.get("model"), str) else ""
    turn_count = obj.get("turn_count") if isinstance(obj.get("turn_count"), int) else 0
    return ChatMeta(session_id=session_id, model=model, turn_count=turn_count)


def save_chat(path: "str | Path", meta: ChatMeta) -> None:
    """Best-effort write of chat metadata; swallow filesystem errors."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"session_id": meta.session_id, "model": meta.model,
                 "turn_count": meta.turn_count}
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def discard_chat(path: "str | Path") -> None:
    """Best-effort deletion of chat metadata; swallow filesystem errors.

    Review #35: called from Qt recovery slots (null-session-id turn,
    session_lost) — a raised OSError would abort the slot mid-recovery,
    leaving input disabled and flags stale.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
