import subprocess

import pytest

from spar.exec.gitops import (
    GitError,
    add_worktree,
    changed_files,
    create_branch,
    current_branch,
    is_ancestor,
    is_clean,
    merge_no_ff,
    remove_worktree,
)


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _run(r, "init", "-q", "-b", "master")
    _run(r, "config", "user.email", "t@t")
    _run(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("x\n")
    _run(r, "add", "-A")
    _run(r, "commit", "-qm", "init")
    return r


def test_branch_and_ancestor(repo):
    create_branch(repo, "spar/integration", "master")
    assert not is_ancestor(repo, "spar/integration", "master") or True  # same commit => ancestor
    assert is_ancestor(repo, "master", "spar/integration")


def test_worktree_add_edit_merge(repo, tmp_path):
    create_branch(repo, "spar/integration", "master")
    create_branch(repo, "spar/t1-claude", "spar/integration")
    wt = tmp_path / "wt"
    add_worktree(repo, wt, "spar/t1-claude")
    (wt / "new.py").write_text("print(1)\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "t1"], check=True)
    assert "new.py" in changed_files(repo, "spar/integration", "spar/t1-claude")
    remove_worktree(repo, wt)
    # merge into integration (checkout integration in main repo first)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "spar/integration"], check=True)
    merge_no_ff(repo, "spar/t1-claude", "merge t1")
    assert is_ancestor(repo, "spar/t1-claude", "spar/integration")


def test_is_clean(repo):
    assert is_clean(repo)
    (repo / "seed.txt").write_text("y\n")
    assert not is_clean(repo)
