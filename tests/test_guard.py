"""Tests for spar/guard.py: snapshot, contract checks, and rollback."""

import subprocess
from pathlib import Path

import pytest

from spar.guard import Guard, GuardContext, GuardViolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(repo_dir, *args):
    subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True)


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(repo_dir, "init", "-q")
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    _run_git(repo_dir, "config", "user.name", "Test")


def _commit_all(repo_dir: Path, message="commit") -> None:
    _run_git(repo_dir, "add", "-A")
    _run_git(repo_dir, "commit", "-q", "-m", message)


def _ctx(artifact_path, hash_before="h0", hash_after="h1", reply_text="ok"):
    return GuardContext(
        artifact_path=artifact_path,
        hash_before=hash_before,
        hash_after=hash_after,
        reply_text=reply_text,
    )


def _make_guard(tmp_path, shrink_threshold=0.6, git=True):
    repo_dir = tmp_path
    if git:
        _init_git_repo(repo_dir)
    spar_dir = repo_dir / ".spar"
    spar_dir.mkdir(parents=True, exist_ok=True)
    artifact = spar_dir / "artifact.md"
    guard = Guard(
        repo_dir=repo_dir,
        artifact_path=artifact,
        spar_dir=spar_dir,
        shrink_threshold=shrink_threshold,
    )
    return guard, repo_dir, artifact


# ---------------------------------------------------------------------------
# Clean turn
# ---------------------------------------------------------------------------


def test_clean_turn_only_artifact_modified_no_exception(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("hello world, this is the artifact content", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("hello world, this is the UPDATED artifact content", encoding="utf-8")

    guard(_ctx(artifact))  # should not raise


def test_clean_turn_non_git_repo_no_exception(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path, git=False)
    artifact.write_text("hello world, this is the artifact content", encoding="utf-8")

    guard.pre_turn()
    artifact.write_text("hello world, this is the UPDATED artifact content", encoding="utf-8")

    guard(_ctx(artifact))  # should not raise


# ---------------------------------------------------------------------------
# Artifact sanity checks
# ---------------------------------------------------------------------------


def test_artifact_deleted_raises_violation(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content here", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.unlink()

    with pytest.raises(GuardViolation, match="missing or not a file"):
        guard(_ctx(artifact))


def test_artifact_empty_raises_violation(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content here", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("", encoding="utf-8")

    with pytest.raises(GuardViolation, match="empty"):
        guard(_ctx(artifact))


def test_artifact_binary_garbage_raises_violation(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content here", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_bytes(b"\xff\xfe\x00\x01garbage\x80\x81")

    with pytest.raises(GuardViolation, match="UTF-8"):
        guard(_ctx(artifact))


# ---------------------------------------------------------------------------
# Shrink ("gutted") check
# ---------------------------------------------------------------------------


def test_shrink_70_percent_raises_violation(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    content = "x" * 100
    artifact.write_text(content, encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("x" * 30, encoding="utf-8")  # 70% smaller

    with pytest.raises(GuardViolation, match="shrank"):
        guard(_ctx(artifact))


def test_shrink_30_percent_is_ok(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    content = "x" * 100
    artifact.write_text(content, encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("x" * 70, encoding="utf-8")  # 30% smaller

    guard(_ctx(artifact))  # should not raise


def test_fresh_artifact_never_trips_shrink(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    # no artifact yet at pre_turn time
    guard.pre_turn()
    artifact.write_text("x", encoding="utf-8")  # tiny, but pre-size was 0

    guard(_ctx(artifact))  # should not raise


# ---------------------------------------------------------------------------
# Foreign new file
# ---------------------------------------------------------------------------


def test_foreign_new_file_raises_and_is_deleted(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    foreign = repo_dir / "sneaky.py"
    # Inert fixture text only (never imported/executed) -- just needs to look
    # like something a rogue turn might drop into the repo.
    foreign.write_text("import os; os.system('rm -rf /')", encoding="utf-8")

    with pytest.raises(GuardViolation, match="foreign changes"):
        guard(_ctx(artifact))

    assert not foreign.exists()  # rolled back


def test_foreign_new_file_in_new_subdir_is_deleted_with_empty_parent(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    subdir = repo_dir / "newdir"
    subdir.mkdir()
    foreign = subdir / "sneaky.txt"
    foreign.write_text("evil", encoding="utf-8")

    with pytest.raises(GuardViolation):
        guard(_ctx(artifact))

    assert not foreign.exists()
    assert not subdir.exists()  # now-empty parent dir also removed


# ---------------------------------------------------------------------------
# Foreign modified file, clean at pre-turn -> git checkout rollback
# ---------------------------------------------------------------------------


def test_foreign_modified_clean_file_git_rolls_back_content(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content", encoding="utf-8")
    other = repo_dir / "other.py"
    other.write_text("original content", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    other.write_text("MALICIOUSLY MODIFIED", encoding="utf-8")

    with pytest.raises(GuardViolation, match="foreign changes"):
        guard(_ctx(artifact))

    assert other.read_text(encoding="utf-8") == "original content"


# ---------------------------------------------------------------------------
# Foreign modified file, dirty before turn -> manual cleanup, not touched
# ---------------------------------------------------------------------------


def test_foreign_modified_dirty_before_turn_not_touched(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content", encoding="utf-8")
    other = repo_dir / "other.py"
    other.write_text("original content", encoding="utf-8")
    _commit_all(repo_dir)

    # dirty it BEFORE the turn starts
    other.write_text("already dirty before the turn", encoding="utf-8")

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    other.write_text("further modified during the turn", encoding="utf-8")

    with pytest.raises(GuardViolation, match="manual cleanup required") as exc_info:
        guard(_ctx(artifact))

    assert "other.py" in str(exc_info.value)
    # not touched: still has the mid-turn content, not rolled back to anything
    assert other.read_text(encoding="utf-8") == "further modified during the turn"


# ---------------------------------------------------------------------------
# Non-git repo
# ---------------------------------------------------------------------------


def test_non_git_repo_new_file_rolled_back_modified_manual_cleanup(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path, git=False)
    artifact.write_text("content", encoding="utf-8")
    other = repo_dir / "other.py"
    other.write_text("original content", encoding="utf-8")

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    other.write_text("modified without git", encoding="utf-8")
    foreign = repo_dir / "new.py"
    foreign.write_text("new file", encoding="utf-8")

    with pytest.raises(GuardViolation, match="manual cleanup required") as exc_info:
        guard(_ctx(artifact))

    assert not foreign.exists()  # new file still rolled back
    assert other.read_text(encoding="utf-8") == "modified without git"  # untouched
    assert "other.py" in str(exc_info.value)


# ---------------------------------------------------------------------------
# .spar/ and .git/ changes ignored
# ---------------------------------------------------------------------------


def test_spar_and_git_dir_changes_ignored(tmp_path):
    guard, repo_dir, artifact = _make_guard(tmp_path)
    artifact.write_text("content", encoding="utf-8")
    _commit_all(repo_dir)

    guard.pre_turn()
    artifact.write_text("content updated", encoding="utf-8")
    # write into .spar/ (transcript-like file) and mutate something under .git/
    (repo_dir / ".spar" / "session.json").write_text("{}", encoding="utf-8")
    (repo_dir / ".git" / "some_marker").write_text("noise", encoding="utf-8")

    guard(_ctx(artifact))  # should not raise
