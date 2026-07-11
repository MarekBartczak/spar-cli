"""Pure tests for the orchestrator chat persistence (.spar/chat.json)."""
from __future__ import annotations

import json

from spar.gui.chat_store import ChatMeta, load_chat, save_chat


def test_missing_file_returns_none(tmp_path):
    assert load_chat(tmp_path / ".spar" / "chat.json") is None


def test_corrupt_file_returns_none(tmp_path):
    p = tmp_path / "chat.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_chat(p) is None


def test_missing_session_id_returns_none(tmp_path):
    p = tmp_path / "chat.json"
    p.write_text(json.dumps({"model": "opus", "turn_count": 2}), encoding="utf-8")
    assert load_chat(p) is None


def test_round_trip(tmp_path):
    p = tmp_path / ".spar" / "chat.json"
    save_chat(p, ChatMeta("sess-abc", "opus", 5))
    meta = load_chat(p)
    assert meta == ChatMeta("sess-abc", "opus", 5)
