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
    # Hash of the opening prompt the session was STARTED with (empty on
    # legacy files). The caller decides whether to check it — chat_store
    # stays generic.
    prompt_hash: str = ""


def load_chat(
    path: "str | Path", expected_prompt_hash: "str | None" = None
) -> "ChatMeta | None":
    """Load chat metadata; None on missing/unreadable/malformed/no session_id.

    When ``expected_prompt_hash`` is given, a stored hash that is missing or
    differs from it also yields None — the persisted session was opened with
    a DIFFERENT opening prompt, so the caller must start fresh. ``None``
    skips the check entirely (generic callers, legacy behavior).
    """
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
    prompt_hash = obj.get("prompt_hash") if isinstance(obj.get("prompt_hash"), str) else ""
    if expected_prompt_hash is not None and prompt_hash != expected_prompt_hash:
        return None
    return ChatMeta(
        session_id=session_id, model=model, turn_count=turn_count,
        prompt_hash=prompt_hash,
    )


def save_chat(path: "str | Path", meta: ChatMeta) -> None:
    """Best-effort write of chat metadata; swallow filesystem errors."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"session_id": meta.session_id, "model": meta.model,
                 "turn_count": meta.turn_count, "prompt_hash": meta.prompt_hash}
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
