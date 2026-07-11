"""Pure (Qt-free) tests for spar/gui/files.py — NO importorskip.

These RUN, not skip, under a plain ``python3`` interpreter: the helpers
live above the ``if _HAS_QT:`` guard (mirrors test_gui_orchestrator_pure).
"""
from __future__ import annotations

import re

import pytest

from spar.gui.files import (
    SearchMatch,
    SearchSpec,
    build_file_index,
    compile_search_pattern,
    filter_paths,
    fuzzy_score,
    passes_search_guards,
    pick_lexer,
    replace_in_text,
    search_file,
    search_paths,
    search_text,
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


class TestSearchEngine:
    def test_literal_case_insensitive_by_default(self):
        pat = compile_search_pattern(SearchSpec("todo"))
        out = search_text("a.py", "# TODO fix\nok\n", pat)
        assert out == [SearchMatch("a.py", 1, "# TODO fix", 2, 6)]

    def test_case_sensitive_excludes_wrong_case(self):
        pat = compile_search_pattern(SearchSpec("TODO", case_sensitive=True))
        assert search_text("a.py", "todo\nTODO\n", pat) == [
            SearchMatch("a.py", 2, "TODO", 0, 4)
        ]

    def test_regex_mode(self):
        pat = compile_search_pattern(SearchSpec(r"f\w+", regex=True))
        out = search_text("a.py", "foo bar fizz\n", pat)
        assert [(m.start, m.end) for m in out] == [(0, 3), (8, 12)]

    def test_whole_word_excludes_substring(self):
        pat = compile_search_pattern(SearchSpec("cat", whole_word=True))
        out = search_text("a.py", "cat scatter cat\n", pat)
        assert [m.start for m in out] == [0, 12]

    def test_literal_dot_is_escaped(self):
        pat = compile_search_pattern(SearchSpec("a.b"))
        assert search_text("a.py", "axb a.b\n", pat) == [
            SearchMatch("a.py", 1, "axb a.b", 4, 7)
        ]

    def test_invalid_regex_raises(self):
        with pytest.raises(re.error):
            compile_search_pattern(SearchSpec("(", regex=True))

    def test_search_file_skips_binary(self, tmp_path):
        (tmp_path / "bin").write_bytes(b"ab\x00cdtodo")
        pat = compile_search_pattern(SearchSpec("todo"))
        assert search_file(tmp_path, "bin", pat) == []

    def test_search_file_skips_oversize(self, tmp_path, monkeypatch):
        f = tmp_path / "big.py"
        f.write_text("todo\n", encoding="utf-8")
        import spar.gui.files as files_mod
        monkeypatch.setattr(files_mod, "_SEARCH_MAX_BYTES", 1)
        pat = compile_search_pattern(SearchSpec("todo"))
        assert search_file(tmp_path, "big.py", pat) == []

    def test_passes_search_guards_predicate(self, tmp_path):
        # review #19: the ONE guard predicate shared by search_file and
        # the rg prefilter.
        (tmp_path / "ok.py").write_text("todo\n", encoding="utf-8")
        (tmp_path / "bin").write_bytes(b"ab\x00cd")
        assert passes_search_guards(tmp_path, "ok.py") is True
        assert passes_search_guards(tmp_path, "bin") is False
        assert passes_search_guards(tmp_path, "missing.py") is False

    def test_search_file_limit_stops_at_cap(self, tmp_path):
        # review #37: search_file must stop scanning at *limit* — it must
        # not materialize every match in the file and slice afterwards
        # (a <=2 MB file can hold millions of matches).
        (tmp_path / "many.txt").write_text(
            "todo " * 500 + "\n", encoding="utf-8"
        )
        pat = compile_search_pattern(SearchSpec("todo"))
        out = search_file(tmp_path, "many.txt", pat, limit=10)
        assert len(out) == 10                      # exactly the cap
        assert out == search_file(tmp_path, "many.txt", pat)[:10]
        # limit=None stays unbounded:
        assert len(search_file(tmp_path, "many.txt", pat)) == 500

    def test_search_text_limit_zero_returns_empty(self):
        # review #39: limit=0 (or negative) means "no results", not
        # "unbounded" — the check-after-append shape must not skip it.
        pat = compile_search_pattern(SearchSpec("todo"))
        assert search_text("a.py", "todo todo\n", pat, limit=0) == []
        assert search_text("a.py", "todo todo\n", pat, limit=-1) == []

    def test_search_file_decodes_replacing_errors(self, tmp_path):
        (tmp_path / "x.txt").write_bytes(b"caf\xe9 todo\n")  # latin-1 é
        pat = compile_search_pattern(SearchSpec("todo"))
        out = search_file(tmp_path, "x.txt", pat)
        assert len(out) == 1 and out[0].line == 1

    def test_search_paths_sorted(self, tmp_path):
        (tmp_path / "b.py").write_text("todo\ntodo\n", encoding="utf-8")
        (tmp_path / "a.py").write_text("todo\n", encoding="utf-8")
        pat = compile_search_pattern(SearchSpec("todo"))
        out = search_paths(tmp_path, ["b.py", "a.py"], pat)
        assert [(m.path, m.line) for m in out] == [
            ("a.py", 1), ("b.py", 1), ("b.py", 2)
        ]


class TestReplaceInText:
    def test_literal_replace_counts(self):
        pat = compile_search_pattern(SearchSpec("cat"))
        assert replace_in_text("cat cat", pat, "dog", regex=False) == ("dog dog", 2)

    def test_literal_replacement_is_verbatim(self):
        # A "\1" replacement must NOT be treated as a backref in literal mode.
        pat = compile_search_pattern(SearchSpec("x"))
        assert replace_in_text("x", pat, r"\1", regex=False) == (r"\1", 1)

    def test_regex_replace_uses_backrefs(self):
        pat = compile_search_pattern(SearchSpec(r"(\w+)=(\d+)", regex=True))
        out, n = replace_in_text("a=1 b=2", pat, r"\2:\1", regex=True)
        assert (out, n) == ("1:a 2:b", 2)

    def test_regex_replace_skips_zero_length_matches(self):
        # review #38: "a*" matches the empty string at every gap; a bare
        # pattern.subn would replace positions search_text never displays.
        # Only the non-empty matches are replaced, and the count agrees
        # with what the search reported.
        pat = compile_search_pattern(SearchSpec("a*", regex=True))
        out, n = replace_in_text("baab", pat, "X", regex=True)
        assert (out, n) == ("bXb", 1)
        assert n == len(search_text("f", "baab", pat))

    def test_regex_replace_never_crosses_newlines(self):
        # review #40: search_text scans per-line, so replace must too — a
        # whole-file finditer would let foo\s+bar match across "foo\nbar"
        # though the search never displayed it.
        pat = compile_search_pattern(SearchSpec(r"foo\s+bar", regex=True))
        assert replace_in_text("foo\nbar", pat, "X", regex=True) == ("foo\nbar", 0)
        assert search_text("f", "foo\nbar", pat) == []
        # Same-line whitespace still matches and is replaced.
        out, n = replace_in_text("foo  bar\nfoo\nbar", pat, "X", regex=True)
        assert (out, n) == ("X\nfoo\nbar", 1)
