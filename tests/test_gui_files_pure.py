"""Pure (Qt-free) tests for spar/gui/files.py — NO importorskip.

These RUN, not skip, under a plain ``python3`` interpreter: the helpers
live above the ``if _HAS_QT:`` guard (mirrors test_gui_orchestrator_pure).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from spar.gui.files import (
    SearchMatch,
    SearchSpec,
    _RipgrepParseError,
    build_file_index,
    build_ripgrep_argv,
    compile_search_pattern,
    filter_paths,
    fuzzy_score,
    is_rg_compatible,
    matches_file_mask,
    parse_file_mask,
    parse_ripgrep_stream,
    passes_search_guards,
    pick_lexer,
    replace_in_text,
    ripgrep_available,
    search_file,
    search_paths,
    search_text,
)

_HAS_RG = shutil.which("rg") is not None


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


class TestRgCompatible:
    # review #19: ONE predicate decides acceleration — only case-SENSITIVE,
    # non-whole-word, LITERAL specs may go to rg. -i (unicode case-fold)
    # and -w (word boundaries) diverge from python re, as do regexes.
    def test_case_sensitive_plain_literal_is_compatible(self):
        assert is_rg_compatible(SearchSpec("todo", case_sensitive=True)) is True

    def test_regex_is_incompatible(self):
        assert is_rg_compatible(
            SearchSpec(r"f\w+", regex=True, case_sensitive=True)
        ) is False

    def test_case_insensitive_is_incompatible(self):
        assert is_rg_compatible(SearchSpec("todo")) is False  # default -i

    def test_whole_word_is_incompatible(self):
        assert is_rg_compatible(
            SearchSpec("todo", case_sensitive=True, whole_word=True)
        ) is False

    def test_ripgrep_available_matches_which(self):
        assert ripgrep_available() is _HAS_RG


class TestRipgrepArgv:
    def test_compatible_literal_argv(self):
        # review #13: rg gets the explicit file LIST (resolved against root),
        # not the directory root, so both engines share one file set.
        # review #19: only is_rg_compatible specs reach the builder, so the
        # flags are fixed — always -F, never -i / -w.
        argv = build_ripgrep_argv(
            "/proj", SearchSpec("todo", case_sensitive=True), ["a.py", "pkg/b.py"]
        )
        assert argv[:2] == ["rg", "--json"]
        assert "-F" in argv and "-i" not in argv and "-w" not in argv
        # review #41: --text so rg scans past a NUL that sits after the
        # 8KB guard window, matching the whole-file python reference.
        assert "--text" in argv
        # query, then "--", then the resolved file paths (in order).
        sep = argv.index("--")
        assert argv[sep - 2:sep] == ["-e", "todo"]
        assert argv[sep + 1:] == ["/proj/a.py", "/proj/pkg/b.py"]


class TestRgBatches:
    def test_rg_batches_bounded_by_argv_byte_budget(self, tmp_path):
        # review #34: _RIPGREP_BATCH alone bounds the file COUNT, not the
        # argv BYTES — long paths could trip ARG_MAX. Batches split when
        # the estimated encoded size exceeds _RIPGREP_ARGV_BUDGET, with
        # the count as the secondary bound. review #36: the budget must
        # count the ABSOLUTE strings build_ripgrep_argv passes
        # (str(root / rel)), not the bare relatives — the assertion below
        # measures the absolute forms.
        from spar.gui import files as fmod

        long_rel = "d/" + "x" * 4094  # 4 KB per path
        files = [f"{long_rel}{i:04d}" for i in range(100)]  # ~400 KB total
        batches = list(fmod._rg_batches(tmp_path, files))
        assert len(batches) > 1                      # split by BYTES
        assert [p for b in batches for p in b] == files  # order + coverage
        for b in batches:
            # review #36: measure what rg's argv actually carries.
            size = sum(
                len(os.fsencode(str(tmp_path / p))) + 1 for p in b
            )
            assert size <= fmod._RIPGREP_ARGV_BUDGET
            assert len(b) <= fmod._RIPGREP_BATCH     # secondary bound

    def test_rg_batches_count_bound(self):
        # Short root + short rels keep the byte budget far away, so the
        # _RIPGREP_BATCH file COUNT is the bound that splits here (the
        # builder never touches the filesystem — a fake root is fine).
        from spar.gui import files as fmod

        files = [f"f{i}.py" for i in range(fmod._RIPGREP_BATCH + 5)]
        batches = list(fmod._rg_batches("/r", files))
        assert [p for b in batches for p in b] == files
        assert len(batches) == 2
        assert len(batches[0]) == fmod._RIPGREP_BATCH


class TestRipgrepParseAnomaly:
    # No rg needed: a non-UTF-8 line makes rg emit a "bytes" (base64)
    # member instead of "text"; the parser must raise so the worker
    # falls back to the python reference (review #4).
    def test_non_utf8_bytes_member_raises(self):
        line = json.dumps({
            "type": "match",
            "data": {
                "path": {"text": "a.txt"},
                "line_number": 1,
                "lines": {"bytes": "Y2Fm6SB0b2RvCg=="},  # "caf\xe9 todo\n"
                "submatches": [{"start": 5, "end": 9}],
            },
        })
        with pytest.raises(_RipgrepParseError):
            list(parse_ripgrep_stream([line], "/root"))

    def test_non_utf8_path_member_raises(self):
        line = json.dumps({
            "type": "match",
            "data": {
                "path": {"bytes": "YS50eHQ="},
                "line_number": 1,
                "lines": {"text": "todo\n"},
                "submatches": [{"start": 0, "end": 4}],
            },
        })
        with pytest.raises(_RipgrepParseError):
            list(parse_ripgrep_stream([line], "/root"))


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
class TestRipgrepParity:
    def _fixture(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text(
            "def todo():\n    return TODO  # todo\n", encoding="utf-8"
        )
        (tmp_path / "b.txt").write_text("no match here\ntodo again\n", encoding="utf-8")
        return tmp_path

    def _via_rg(self, root, spec):
        # review #13: pass rg the SAME explicit file list the python
        # reference scans (build_file_index), so file sets are identical.
        # review #19: prefiltered with the shared guard predicate, so rg
        # never sees a file python's size/binary guards would skip.
        files = [
            rel for rel in build_file_index(root)
            if passes_search_guards(root, rel)
        ]
        proc = subprocess.run(
            build_ripgrep_argv(root, spec, files),
            capture_output=True, text=True, check=False,
        )
        return list(parse_ripgrep_stream(proc.stdout.splitlines(), root))

    def test_literal_parity(self, tmp_path):
        # review #19: parity specs are case-SENSITIVE — the only literal
        # mode the accelerator handles (is_rg_compatible).
        root = self._fixture(tmp_path)
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(root, build_file_index(root), pat)
        rg = sorted(self._via_rg(root, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert reference  # non-trivial fixture

    def test_unicode_content_parity(self, tmp_path):
        # A non-BMP char (😀, 4 UTF-8 bytes) and an accented char before
        # the match exercise the byte→character offset remap (review #4).
        (tmp_path / "u.py").write_text(
            "café 😀 todo\ntodo again\n", encoding="utf-8"
        )
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(tmp_path, build_file_index(tmp_path), pat)
        rg = sorted(self._via_rg(tmp_path, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert reference and reference[0].start == 7  # char offset, not byte

    def test_exclusion_parity(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "c.py").write_text("todo\n", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "d.py").write_text("todo\n", encoding="utf-8")
        (tmp_path / "keep.py").write_text("todo\n", encoding="utf-8")
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(tmp_path, build_file_index(tmp_path), pat)
        rg = sorted(self._via_rg(tmp_path, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert {m.path for m in reference} == {"keep.py"}  # excludes honoured

    def test_binary_prefilter_parity(self, tmp_path):
        # review #19: rg's binary detection differs from python's
        # NUL-in-first-8KB rule, so a NUL-bearing file that rg might still
        # report is prefiltered OUT (passes_search_guards) before rg runs —
        # both engines skip exactly the same files.
        (tmp_path / "bin.dat").write_bytes(b"todo\x00todo\n")
        (tmp_path / "ok.py").write_text("todo\n", encoding="utf-8")
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(tmp_path, build_file_index(tmp_path), pat)
        rg = sorted(self._via_rg(tmp_path, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert {m.path for m in reference} == {"ok.py"}  # binary skipped by BOTH

    def test_nul_after_guard_window_parity(self, tmp_path):
        # review #41: passes_search_guards only inspects the FIRST 8KB,
        # so a file whose first NUL sits AFTER the window passes the
        # guard and the python engine scans it whole. rg without --text
        # would stop at the NUL and drop the later match — --text keeps
        # the engines identical.
        (tmp_path / "late_nul.txt").write_bytes(
            b"x" * 9000 + b"\x00\n" + b"todo late\n"
        )
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(tmp_path, build_file_index(tmp_path), pat)
        rg = sorted(self._via_rg(tmp_path, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert {m.path for m in reference} == {"late_nul.txt"}  # match after the NUL, found by BOTH

    def test_symlinked_file_parity(self, tmp_path):
        # review #13: os.walk lists a symlinked FILE, so build_file_index
        # includes it; rg without --follow would skip it if given the dir
        # root. Passing the explicit file list makes rg search exactly the
        # same files, so a match behind a symlink appears in BOTH engines.
        real = tmp_path / "real.py"
        real.write_text("todo real\n", encoding="utf-8")
        link = tmp_path / "link.py"
        os.symlink(real, link)  # symlink to a regular file
        spec = SearchSpec("todo", case_sensitive=True)
        pat = compile_search_pattern(spec)
        reference = search_paths(tmp_path, build_file_index(tmp_path), pat)
        rg = sorted(self._via_rg(tmp_path, spec), key=lambda m: (m.path, m.line, m.start))
        assert rg == reference
        assert "link.py" in {m.path for m in reference}  # symlink searched

    def test_invalid_utf8_file_raises_parse_anomaly(self, tmp_path):
        # rg emits a "bytes" member for the non-UTF-8 line → the parser
        # raises so the worker falls back to python (review #4). Note the
        # file has no NUL, so it PASSES the guards (review #19) and is
        # handed to rg — the anomaly path, not the prefilter, catches it.
        # review #18: build_ripgrep_argv takes the explicit file list.
        (tmp_path / "bad.txt").write_bytes(b"caf\xe9 todo\n")  # latin-1 é
        spec = SearchSpec("todo", case_sensitive=True)
        proc = subprocess.run(
            build_ripgrep_argv(tmp_path, spec, build_file_index(tmp_path)),
            capture_output=True, text=True, check=False,
        )
        with pytest.raises(_RipgrepParseError):
            list(parse_ripgrep_stream(proc.stdout.splitlines(), tmp_path))


class TestFileMask:
    # -- parse_file_mask --
    def test_parse_splits_strips_and_tuples(self):
        assert parse_file_mask("*.ts, *.tsx") == ("*.ts", "*.tsx")

    def test_parse_single_glob(self):
        assert parse_file_mask("*.py") == ("*.py",)

    def test_parse_drops_empty_parts(self):
        assert parse_file_mask(" *.ts ,, ,*.tsx ") == ("*.ts", "*.tsx")

    def test_parse_all_empty_is_none(self):
        assert parse_file_mask("") is None
        assert parse_file_mask(" , ") is None

    # -- matches_file_mask --
    def test_none_mask_passes_everything(self):
        assert matches_file_mask("any/where/at.all", None) is True

    def test_matches_basename_not_full_path(self):
        # The glob applies to the BASENAME (WebStorm semantics): a nested
        # .py matches *.py, and a directory-shaped glob does NOT match.
        assert matches_file_mask("deep/nested/mod.py", ("*.py",)) is True
        assert matches_file_mask("src/mod.py", ("src/*",)) is False

    def test_ts_does_not_match_tsx(self):
        assert matches_file_mask("app/main.tsx", ("*.ts",)) is False
        assert matches_file_mask("app/main.tsx", ("*.ts", "*.tsx")) is True

    def test_case_sensitive_on_posix(self):
        assert matches_file_mask("A.PY", ("*.py",)) is False

    def test_no_glob_matches_is_false(self):
        assert matches_file_mask("readme.md", ("*.ts", "*.tsx")) is False

    # -- SearchSpec integration --
    def test_searchspec_default_mask_is_none_and_drift_detected(self):
        base = SearchSpec("q")
        assert base.file_mask is None
        masked = SearchSpec("q", file_mask=("*.py",))
        assert base != masked            # mask change == spec drift
        assert hash(masked)              # stays hashable
