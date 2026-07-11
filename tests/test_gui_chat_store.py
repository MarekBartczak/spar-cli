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


# -- prompt-hash invalidation (live smoke: OPENING_PROMPT changed but restarted
# GUIs kept resuming sessions opened with the OLD prompt) --------------------

def test_round_trip_with_matching_prompt_hash_resumes(tmp_path):
    p = tmp_path / "chat.json"
    save_chat(p, ChatMeta("sess-abc", "opus", 5, prompt_hash="cafe0123deadbeef"))
    meta = load_chat(p, expected_prompt_hash="cafe0123deadbeef")
    assert meta == ChatMeta("sess-abc", "opus", 5, "cafe0123deadbeef")


def test_mismatched_prompt_hash_returns_none(tmp_path):
    p = tmp_path / "chat.json"
    save_chat(p, ChatMeta("sess-abc", "opus", 5, prompt_hash="oldhash000000000"))
    assert load_chat(p, expected_prompt_hash="newhash111111111") is None


def test_missing_prompt_hash_returns_none_when_expected(tmp_path):
    # Backward compat: a chat.json written BEFORE the field existed must be
    # treated as no-session when the caller expects a hash.
    p = tmp_path / "chat.json"
    p.write_text(json.dumps({"session_id": "sess-abc", "model": "opus",
                             "turn_count": 5}), encoding="utf-8")
    assert load_chat(p, expected_prompt_hash="cafe0123deadbeef") is None


def test_expected_none_skips_hash_check(tmp_path):
    # chat_store stays generic (grill purity): no expected hash -> no check,
    # even against a stored mismatching hash or a legacy file without one.
    p = tmp_path / "chat.json"
    save_chat(p, ChatMeta("sess-abc", "opus", 5, prompt_hash="whatever12345678"))
    meta = load_chat(p)
    assert meta is not None and meta.session_id == "sess-abc"
