"""Pure (Qt-free) tests for spar/gui/files.py — NO importorskip.

These RUN, not skip, under a plain ``python3`` interpreter: the helpers
live above the ``if _HAS_QT:`` guard (mirrors test_gui_orchestrator_pure).
"""
from __future__ import annotations

from spar.gui.files import (
    build_file_index,
    filter_paths,
    fuzzy_score,
    pick_lexer,
)


class TestPickLexer:
    def test_known_extension_picks_specific_lexer(self):
        assert "Python" in pick_lexer("a/b/thing.py").name

    def test_unknown_extension_falls_back_to_text(self):
        assert pick_lexer("mystery.zzz").name.lower() in ("text only", "text")


class TestFuzzyScore:
    def test_non_subsequence_is_none(self):
        assert fuzzy_score("xyz", "spar/gui/app.py") is None

    def test_subsequence_matches(self):
        assert fuzzy_score("app", "spar/gui/app.py") is not None

    def test_empty_query_matches_everything(self):
        assert fuzzy_score("", "any/path.py") == -len("any/path.py")

    def test_basename_start_beats_midword(self):
        # "app" at the start of the basename must outrank "app" buried
        # mid-path.
        direct = fuzzy_score("app", "gui/app.py")
        buried = fuzzy_score("app", "grapple/xxx.py")
        assert direct is not None and buried is not None
        assert direct > buried

    def test_contiguous_beats_scattered(self):
        contig = fuzzy_score("abc", "abc.py")
        scattered = fuzzy_score("abc", "a_b_c.py")
        assert contig > scattered


class TestFilterPaths:
    def test_drops_non_matches_and_ranks(self):
        paths = ["spar/gui/app.py", "spar/gui/files.py", "README.md"]
        out = filter_paths("app", paths)
        assert out == ["spar/gui/app.py"]

    def test_empty_query_returns_all_sorted_by_path(self):
        paths = ["b.py", "a.py"]
        out = filter_paths("", paths)
        assert out == ["a.py", "b.py"]


class TestBuildFileIndex:
    def test_skips_noise_dirs_and_returns_relative_posix(self, tmp_path):
        (tmp_path / "spar" / "gui").mkdir(parents=True)
        (tmp_path / "spar" / "gui" / "app.py").write_text("x")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("y")
        (tmp_path / "README.md").write_text("r")

        out = build_file_index(tmp_path)

        assert "spar/gui/app.py" in out
        assert "README.md" in out
        assert not any(p.startswith(".git/") for p in out)
        assert not any(p.startswith("node_modules/") for p in out)
        assert out == sorted(out)
