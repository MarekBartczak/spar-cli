"""Tests for ``spar.gui.repo``'s git-repo prerequisite probe/actions.

A debate must not start outside a local git repo, and ``spar exec``
additionally needs at least one commit (``rev-parse HEAD``). ``repo_state``
classifies a directory into one of three states against *real* tmp git
repos (no git mocking) so the probe is exercised against actual git
behavior rather than assumptions about its output.
"""

from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("PySide6")

from spar.gui import repo as repo_mod


def _git(project_dir, *args):
    subprocess.run(
        ["git", "-C", str(project_dir), *args],
        check=True,
        capture_output=True,
    )


class TestRepoState:
    def test_non_git_dir_is_none(self, tmp_path):
        assert repo_mod.repo_state(tmp_path) == "none"

    def test_git_repo_without_commits_is_no_head(self, tmp_path):
        _git(tmp_path, "init", "-b", "master")

        assert repo_mod.repo_state(tmp_path) == "no_head"

    def test_git_repo_with_a_commit_is_ok(self, tmp_path):
        _git(tmp_path, "init", "-b", "master")
        _git(tmp_path, "config", "user.email", "t@t")
        _git(tmp_path, "config", "user.name", "t")
        _git(tmp_path, "commit", "--allow-empty", "-m", "init")

        assert repo_mod.repo_state(tmp_path) == "ok"


class TestCreateRepo:
    def test_create_repo_yields_ok_state_with_one_commit(self, tmp_path):
        repo_mod.create_repo(tmp_path)

        assert repo_mod.repo_state(tmp_path) == "ok"
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1


class TestCreateInitialCommit:
    def test_create_initial_commit_on_headless_repo_yields_ok(self, tmp_path):
        _git(tmp_path, "init", "-b", "master")

        repo_mod.create_initial_commit(tmp_path)

        assert repo_mod.repo_state(tmp_path) == "ok"


def _init_repo_with_commit(tmp_path):
    _git(tmp_path, "init", "-b", "master")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "--allow-empty", "-m", "init")


class TestDirtyPaths:
    def test_clean_tree_is_empty(self, tmp_path):
        _init_repo_with_commit(tmp_path)

        assert repo_mod.dirty_paths(tmp_path) == []

    def test_modified_tracked_file_is_reported(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        tracked = tmp_path / "CONTEXT.md"
        tracked.write_text("v1\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "add CONTEXT.md")
        tracked.write_text("v2\n", encoding="utf-8")

        assert repo_mod.dirty_paths(tmp_path) == ["CONTEXT.md"]

    def test_untracked_file_is_reported(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "adr").mkdir()
        (tmp_path / "docs" / "adr" / "0001-thing.md").write_text("x\n", encoding="utf-8")

        assert repo_mod.dirty_paths(tmp_path) == ["docs/adr/0001-thing.md"]

    def test_multiple_dirty_paths_all_reported(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("b\n", encoding="utf-8")

        paths = repo_mod.dirty_paths(tmp_path)

        assert sorted(paths) == ["a.md", "b.md"]

    def test_non_git_dir_is_empty(self, tmp_path):
        assert repo_mod.dirty_paths(tmp_path) == []


class TestCommitAll:
    def test_commits_dirty_tree_to_clean(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        (tmp_path / "CONTEXT.md").write_text("grill notes\n", encoding="utf-8")

        repo_mod.commit_all(tmp_path, "docs: pre-exec snapshot")

        assert repo_mod.dirty_paths(tmp_path) == []
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = log.stdout.strip().splitlines()
        assert len(lines) == 2
        assert "docs: pre-exec snapshot" in lines[0]
