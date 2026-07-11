# Files module — tranche B (ADR 0006): find/replace in files + Ctrl+F in editor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each task below is a self-contained unit of work: dispatch it to a subagent, have the subagent follow the TDD loop verbatim (write the failing test, run it, implement, run again, commit), and report the exact pytest tail back. Do NOT batch tasks. Verify the suite is green (`.venv/bin/python -m pytest tests/ -q`, baseline **906 passed, 2 skipped**) at the end of every task before starting the next.

## Goal

Deliver **tranche B** of the Pliki module exactly as scoped by
`docs/adr/0006-files-module-editor-and-search.md` — and nothing beyond it (no git
module). Concretely:

- a **pure content-search engine** above the `_HAS_QT` guard: literal / regex /
  case-sensitive / whole-word modes, per-line matches with spans, binary + size
  guards, and an **optional ripgrep accelerator** that produces the identical
  result shape (pure Python is the reference and the fallback);
- a **find-in-files panel** inside `FilesView`, opened by **Ctrl+Shift+F**, that
  runs the search on a **cancellable QThread worker** (generation-token pattern),
  shows a file→lines results tree, and opens a file at the clicked line;
- **replace-in-files** in the same panel (checkbox-selected files + "Zamień
  zaznaczone"), honouring the read-only matrix and refusing to write files with
  unsaved local edits;
- **Ctrl+F** find/replace bar inside the open editor tab (next/prev, F3/Shift+F3,
  wrap, highlight-all, replace respecting read-only);
- QSS for the new widgets (TOKENS only, colour-purity test green) and docs
  (README shortcuts + replace semantics, HANDOFF entry, ADR 0006 stamp).

## Architecture

Everything lands in the **existing** `spar/gui/files.py` (extending, not
replacing, tranche A) and its wiring in `spar/gui/app.py` (`MainWindow`).

- **Pure helpers (ABOVE the `if _HAS_QT:` guard)** — tested under a plain
  `python3` in `tests/test_gui_files_pure.py` (no `importorskip`):
  - `SearchSpec` (frozen dataclass: `query`, `regex`, `case_sensitive`,
    `whole_word`), `SearchMatch` (frozen dataclass: `path`, `line`, `text`,
    `start`, `end`);
  - `compile_search_pattern(spec) -> re.Pattern` (raises `re.error` on a bad
    regex — the caller validates and disables search);
  - `search_text(rel, text, pattern, limit=None) -> list[SearchMatch]`,
    `passes_search_guards(root, rel) -> bool` (the shared 2 MB size guard +
    NUL-in-first-8KB binary guard — review #19),
    `search_file(root, rel, pattern, limit=None) -> list[SearchMatch]` (applies
    the guards, `errors="replace"`; review #37: `limit` stops the scan at that
    many matches instead of materializing every match in the file),
    `search_paths(root, rel_paths, pattern) -> list[SearchMatch]` (the
    reference python path);
  - `replace_in_text(text, pattern, replacement, *, regex) -> tuple[str, int]`
    (review #38: same zero-length-match skip as `search_text`; review #40:
    processes the text line-by-line with the same `split("\n")` semantics as
    `search_text` — only matches the search results display are ever
    replaced, so a pattern like `foo\s+bar` can never edit across a `\n`);
  - ripgrep accelerator: `ripgrep_available()`, `is_rg_compatible(spec)`
    (review #19: rg only for case-SENSITIVE, non-whole-word, literal specs),
    `build_ripgrep_argv(root, spec, files)`,
    `parse_ripgrep_stream(lines, root) -> Iterator[SearchMatch]` — parses
    `rg --json` and yields the **identical** `SearchMatch` shape (byte offsets
    remapped to character offsets so spans match the python reference).
- **Qt layer (UNDER the guard):**
  - `_SearchWorker(QObject)` on a persistent `QThread` — runs the search
    (ripgrep stream when available, else the per-file python loop), emits
    generation-stamped batches, and early-exits when a newer generation
    supersedes it (cancellation);
  - `SearchPanel(QWidget)` — query line edit + Aa/`.*`/W toggles + replace field
    + results tree (file rows checkable, default checked; line rows display-only)
    + "Zamień zaznaczone" + status label; owns the worker facade;
  - `EditorFindBar(QWidget)` — the Ctrl+F find/replace bar embedded in
    `EditorTab`;
  - extensions to `FileEditor` (merge find-match `ExtraSelections` with the
    existing current-line highlight), `EditorTab` (host the find bar), and
    `FilesView` (bottom search dock, Ctrl+Shift+F / Ctrl+F shortcut+bridge,
    `open_at`, replace-disable on the read-only matrix).

**Search-panel placement (resolved).** `FilesView` today is a `QVBoxLayout`
wrapping one horizontal `QSplitter` (tree | editor-side). Tranche B wraps that
horizontal splitter and a new `SearchPanel` in an **outer vertical `QSplitter`**,
with the `SearchPanel` as the second (bottom) child, **hidden by default** and
shown by Ctrl+Shift+F. A full-width bottom strip gives the file→lines results
tree width without stealing horizontal space from the tree or editor, and
mirrors WebStorm's "Find in Path" tool window. This is the cleanest fit for the
existing layout.

Data flow (search):

```
Ctrl+Shift+F ─▶ FilesView shortcut+bridge ─▶ FilesView.open_search()  (show dock, focus query)
SearchPanel query Enter / toggle ─▶ _run_search(spec) ─▶ facade.search(spec)
  facade bumps generation, _dispatch(gen, spec) ─▶ worker.run_search (QThread)
  worker emits match_batch(gen, [SearchMatch]) / finished(gen, n, m)  (generation-filtered in facade)
SearchPanel results click/Enter ─▶ open_location(rel, line, start, end)
  ─▶ MainWindow: _set_centre_view("files") + FilesView.open_at(rel, line, start, end)
```

Data flow (replace):

```
"Zamień zaznaczone" ─▶ SearchPanel._apply_replace()
  for each CHECKED file row:
    if the file has a dirty open tab → skip (count "pominięto N (niezapisane zmiany)")
    else read disk, replace_in_text(same compiled pattern, replacement), write disk
  (watcher auto-reloads open CLEAN tabs) ─▶ re-run the search to refresh results
Replace disabled (button + checkboxes) while FilesView is read-only (RUNNING/GATE_PENDING/LOCKED); search stays enabled.
```

Data flow (Ctrl+F):

```
Ctrl+F ─▶ FilesView shortcut+bridge ─▶ current EditorTab.open_find(prefill=selection)
EditorFindBar: next/prev/F3/Shift+F3 (wrap) + highlight-all (ExtraSelections)
  + "Zamień"/"Zamień wszystko" (disabled while editor read-only); Esc closes.
```

## Tech Stack

Python 3.12, PySide6 (`gui` extra), pytest + pytest-qt, GUI tests offscreen
(`QT_QPA_PLATFORM=offscreen`, set in `tests/conftest.py`). No new dependency:
`re`, `shutil`, `subprocess` are stdlib; `rg` is used only when
`shutil.which("rg")` finds it (present on the dev box; parity tests `skipif` it
is absent). `pygments` already ships in the `gui` extra from tranche A.

## Global Constraints

- **Suite green after every task:** `.venv/bin/python -m pytest tests/ -q`,
  baseline **906 passed, 2 skipped**. Each task's commit is made only after a
  green run. For every task after Task 1, assert **"baseline + this task's new
  tests, no failures, no regressions"** — do NOT hard-code an intermediate
  total (several tasks add a variable number of tests).
- **GUI tests offscreen** — never call `window.show()`/`view.show()` unless a
  test explicitly needs a real chord routed through a QShortcut; prefer
  `isHidden()` over `isVisible()` for pre-show assertions.
- **Pure helpers stay pure** — every symbol in this plan's "pure" list lives
  ABOVE the `if _HAS_QT:` guard; its tests go in `tests/test_gui_files_pure.py`
  with **no `importorskip`** and run under a plain `python3`.
- **Dirty-flag simulation** follows the tranche-A #9 convention: `setPlainText`
  RESETS `document().isModified()`, so a test that needs a dirty buffer must
  either type real keys (`qtbot.keyClicks`) or call
  `ed.document().setModified(True)` explicitly after `setPlainText`.
- **Shortcut tests encode BOTH halves** (the Ctrl+S lesson): a real-chord
  `qtbot.keyClick` test (with `view.show()` + `setFocus`) to exercise the
  chord path, AND an `activated.emit()` / signal-emit pin so the
  `QShortcut → slot` connection cannot be silently deleted while offscreen CI
  stays green.
- **QSettings** — the new persisted key `files/search_split` (the vertical
  splitter state) must be namespaced under `files/*`; any test asserting
  persisted state uses the `_hermetic_qsettings` fixture convention from
  `tests/test_gui_app.py`.
- **Regex safety** — `compile_search_pattern` raises `re.error` on invalid input;
  the panel catches it, marks the query invalid (red border via a dynamic
  `invalid` property styled from TOKENS) and disables the search action. Replace
  in regex mode MUST reuse the **same compiled pattern** the search used (no
  literal/regex mismatch).
- **Thread cancellation** — a superseded search must never deliver stale
  results: the facade filters every worker→facade signal by generation, and the
  worker early-exits when its generation is no longer live. Test with an
  injectable slow per-file scan fake.
- **Watcher/replace interplay** is tested with **real disk writes** (mirroring
  tranche A's watcher tests): replace on disk → the open clean tab auto-reloads.
- **QSS** — new widgets are themed with `TOKENS` values only; the colour-purity
  test (`tests/test_gui_theme.py`) stays green after each task that touches
  `build_qss`.
- **No AI-attribution trailers** in commits (no `Co-Authored-By`, no
  "Generated with").
- **README + HANDOFF + ADR 0006 stamp** land in this tranche (Task 7), per the
  keep-README-current rule. Screenshot stays a TODO comment.
- **Do not regress tranche A** — the `MainWindow.__init__` init-order constraint
  (single trailing `side_pane.refresh()`), the read-only matrix, the Ctrl+S
  bridge, the double-Shift finder, and the orchestrator chat all keep working.

---

### Task 1: Pure search + replace engine — compile, scan, guards, replace (Sonnet)

Foundational and Qt-free. Adds the search/replace core above the `_HAS_QT` guard.
No ripgrep yet (Task 2).

**Files**
- `spar/gui/files.py` (add pure section: `SearchSpec`, `SearchMatch`,
  `compile_search_pattern`, `search_text`, `passes_search_guards`,
  `search_file`, `search_paths`, `replace_in_text`, constants; extend
  `__all__`)
- `tests/test_gui_files_pure.py` (append `TestSearchEngine`, `TestReplaceInText`)

**Interfaces (exact signatures)**
```python
_SEARCH_MAX_BYTES = 2 * 1024 * 1024   # skip files larger than 2 MB
_SEARCH_BINARY_SNIFF = 8192           # NUL in the first 8 KB ⇒ treat as binary

@dataclass(frozen=True)
class SearchSpec:
    query: str
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False

@dataclass(frozen=True)
class SearchMatch:
    path: str    # project-relative POSIX
    line: int    # 1-based line number
    text: str    # full line text, trailing newline stripped
    start: int   # 0-based CHARACTER offset of the match within the line
    end: int     # 0-based CHARACTER offset one past the match

def compile_search_pattern(spec: SearchSpec) -> "re.Pattern[str]": ...
def search_text(rel: str, text: str, pattern, limit: "int | None" = None) -> "list[SearchMatch]": ...
def passes_search_guards(root, rel: str) -> bool: ...   # size + binary guards (review #19: shared with rg prefilter)
def search_file(root, rel: str, pattern, limit: "int | None" = None) -> "list[SearchMatch]": ...
def search_paths(root, rel_paths, pattern) -> "list[SearchMatch]": ...
def replace_in_text(text: str, pattern, replacement: str, *, regex: bool) -> "tuple[str, int]": ...
```

Semantics:
- `compile_search_pattern`: `flags = 0 if case_sensitive else re.IGNORECASE`;
  `body = spec.query if regex else re.escape(spec.query)`; if `whole_word`,
  `body = rf"\b(?:{body})\b"`; `return re.compile(body, flags)`. Raises
  `re.error` on invalid regex.
- `search_text`: split on `\n`, keep 1-based line numbers, for each line collect
  **non-overlapping** `pattern.finditer` matches as `SearchMatch(rel, lineno,
  line, m.start(), m.end())`. An empty-query pattern (`re.escape("") == ""`)
  matches at every position — guard: if `spec.query == ""` the caller never
  searches (SearchPanel disables search on an empty query), but `search_text`
  must still be well-defined: skip zero-length matches (`m.start() == m.end()`)
  so an accidental empty pattern yields nothing. Review #37: `limit` (default
  `None` = unbounded) stops the scan and returns as soon as
  `len(out) == limit` — a caller with a result cap must never force the full
  match list of a pathological file (a ≤2 MB file can hold millions of
  matches) to materialize; the caller infers truncation via
  `len(result) == limit`. Review #39: `limit <= 0` (a non-None non-positive
  cap) returns `[]` immediately — it must not fall through the
  check-after-append shape and behave as unbounded.
- `passes_search_guards`: the ONE place the size + binary guards live (review
  #19 — the rg prefilter reuses it so the two engines cannot diverge on which
  files are skipped): `p = Path(root) / rel`; `False` if
  `p.stat().st_size > _SEARCH_MAX_BYTES`, if the first `_SEARCH_BINARY_SNIFF`
  bytes contain `b"\x00"`, or on any `OSError`; else `True`.
- `search_file`: if `not passes_search_guards(root, rel)` return `[]`; read
  bytes (`OSError` ⇒ `[]`); `text = data.decode("utf-8", errors="replace")`;
  return `search_text(rel, text, pattern, limit=limit)` (review #37: forwards
  `limit` so the scan stops at the cap instead of building the full list).
- `search_paths`: flat-map `search_file(root, rel, pattern)` over `rel_paths`,
  then `sort(key=lambda m: (m.path, m.line, m.start))`.
- `replace_in_text` (review #38, #40): must use the SAME match semantics as
  `search_text` in BOTH dimensions. Review #40: `search_text` scans per-LINE
  (`text.split("\n")`), so `replace_in_text` must too — a whole-file
  `pattern.finditer(text)` would let a regex like `foo\s+bar` replace across
  `foo\nbar` even though the search never displayed that match. So it splits
  on `"\n"`, runs `pattern.finditer(line)` per line, **skips
  `m.start() == m.end()`** (review #38 — zero-width positions the results
  never display, e.g. `a*`), splices the replacements within each line, and
  rejoins with `"\n"`: regex mode expands user backrefs via
  `m.expand(replacement)` (may raise `re.error` on a bad replacement —
  caller catches); literal mode splices *replacement* verbatim (no backref
  interpretation). Returns `(new_text, count)` where `count` is the total of
  per-line replaced **non-zero** matches — identical to what `search_text`
  reports for the same pattern.

**Steps**

- [ ] Append the failing pure tests to `tests/test_gui_files_pure.py`:
    ```python
    import re

    from spar.gui.files import (
        SearchMatch,
        SearchSpec,
        compile_search_pattern,
        passes_search_guards,
        replace_in_text,
        search_file,
        search_paths,
        search_text,
    )


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
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py -q`
  - Expected: **ImportError** (`SearchSpec` etc. not defined).

- [ ] Implement the pure engine ABOVE the `if _HAS_QT:` guard in
  `spar/gui/files.py`. Add imports at the top (`import re`, `import shutil`,
  `from dataclasses import dataclass`) and extend `__all__` with `"SearchSpec"`,
  `"SearchMatch"`, `"compile_search_pattern"`, `"search_text"`, `"passes_search_guards"`,
  `"search_file"`, `"search_paths"`, `"replace_in_text"`:
    ```python
    _SEARCH_MAX_BYTES = 2 * 1024 * 1024
    _SEARCH_BINARY_SNIFF = 8192


    @dataclass(frozen=True)
    class SearchSpec:
        """One find-in-files query with its mode toggles. Pure/hashable."""
        query: str
        regex: bool = False
        case_sensitive: bool = False
        whole_word: bool = False


    @dataclass(frozen=True)
    class SearchMatch:
        """One match: project-relative POSIX path, 1-based line, full line
        text, and the CHARACTER span [start, end) of the match in that line."""
        path: str
        line: int
        text: str
        start: int
        end: int


    def compile_search_pattern(spec: "SearchSpec") -> "re.Pattern[str]":
        """Compile *spec* into a regex. Literal queries are re.escape'd; a
        whole-word query is wrapped in ``\\b(?:...)\\b``; case-insensitive
        unless spec.case_sensitive. Raises ``re.error`` on an invalid regex —
        the caller validates and disables search."""
        flags = 0 if spec.case_sensitive else re.IGNORECASE
        body = spec.query if spec.regex else re.escape(spec.query)
        if spec.whole_word:
            body = rf"\b(?:{body})\b"
        return re.compile(body, flags)


    def search_text(
        rel: str, text: str, pattern, limit: "int | None" = None
    ) -> "list[SearchMatch]":
        """Scan already-decoded *text* line by line. Zero-length matches are
        skipped so an accidental empty pattern yields nothing. review #37:
        *limit* stops the scan as soon as that many matches are collected —
        never materialize a pathological file's full match list. review #39:
        a non-positive *limit* returns [] immediately (it must not behave as
        unbounded). Pure."""
        if limit is not None and limit <= 0:
            return []
        out: list[SearchMatch] = []
        for lineno, line in enumerate(text.split("\n"), start=1):
            for m in pattern.finditer(line):
                if m.start() == m.end():
                    continue
                out.append(SearchMatch(rel, lineno, line, m.start(), m.end()))
                if limit is not None and len(out) == limit:
                    return out
        return out


    def passes_search_guards(root, rel: str) -> bool:
        """True when ``root/rel`` passes the python engine's file guards:
        size <= 2 MB and no NUL byte in the first 8 KB. Any OSError => False.
        review #19: this is the SINGLE guard predicate — search_file uses it
        AND the ripgrep path prefilters its file list with it, so rg never
        sees a file the python reference would skip (rg's own binary
        detection differs from the NUL-in-first-8KB rule)."""
        p = Path(root) / rel
        try:
            if p.stat().st_size > _SEARCH_MAX_BYTES:
                return False
            with p.open("rb") as fh:
                sniff = fh.read(_SEARCH_BINARY_SNIFF)
        except OSError:
            return False
        return b"\x00" not in sniff


    def search_file(
        root, rel: str, pattern, limit: "int | None" = None
    ) -> "list[SearchMatch]":
        """Read ``root/rel`` and scan it. Applies passes_search_guards (size +
        binary, review #19); decodes utf-8 errors='replace'. Any OSError ⇒ no
        matches. review #37: *limit* is forwarded to search_text so scanning
        stops at the cap (caller infers truncation via ``len == limit``).
        Touches the filesystem, no Qt."""
        if not passes_search_guards(root, rel):
            return []
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError:
            return []
        return search_text(
            rel, data.decode("utf-8", errors="replace"), pattern, limit=limit
        )


    def search_paths(root, rel_paths, pattern) -> "list[SearchMatch]":
        """Reference python content search over *rel_paths*, sorted by
        (path, line, start). This is the shape ripgrep must reproduce."""
        out: list[SearchMatch] = []
        for rel in rel_paths:
            out.extend(search_file(root, rel, pattern))
        out.sort(key=lambda m: (m.path, m.line, m.start))
        return out


    def replace_in_text(text: str, pattern, replacement: str, *, regex: bool):
        """Return ``(new_text, count)``. Mirrors search_text's match semantics
        exactly. review #40: scans per-LINE (``text.split("\\n")``) like
        search_text — a whole-file finditer would let ``foo\\s+bar`` replace
        across ``foo\\nbar`` though the search never displayed it. review #38:
        within each line, replaces ONLY non-zero-length matches — the same
        skip search_text applies — so a pattern like ``a*`` never edits the
        zero-width positions the results tree never displayed. Manual
        per-line finditer splice (no bare ``pattern.subn``): regex mode
        expands backrefs via ``m.expand(replacement)`` (may raise ``re.error``
        on a bad replacement — the caller catches); literal mode splices
        *replacement* verbatim (``\\1`` is not a backref). *count* is the
        total of per-line replaced non-zero matches."""
        new_lines: list[str] = []
        count = 0
        for line in text.split("\n"):
            pieces: list[str] = []
            pos = 0
            for m in pattern.finditer(line):
                if m.start() == m.end():
                    continue
                pieces.append(line[pos:m.start()])
                pieces.append(m.expand(replacement) if regex else replacement)
                pos = m.end()
                count += 1
            pieces.append(line[pos:])
            new_lines.append("".join(pieces))
        return "\n".join(new_lines), count
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py -q`
  - Expected: **all pass** (existing tranche-A pure tests + the new ones).

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline **906 passed, 2 skipped** plus the new pure tests, no
    failures.

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files_pure.py`
  - `git commit -m "feat(gui): pure find/replace engine (spec, match, scan, guards)"`

---

### Task 2: ripgrep accelerator + python-parity (Sonnet)

Adds the opportunistic `rg --json` path. The python engine from Task 1 is the
reference; ripgrep must yield the identical `SearchMatch` list on a small ASCII
fixture.

**Files**
- `spar/gui/files.py` (add `ripgrep_available`, `is_rg_compatible`,
  `build_ripgrep_argv`, `parse_ripgrep_stream`; extend `__all__`)
- `tests/test_gui_files_pure.py` (append `TestRipgrep`)

**Interfaces (exact signatures)**
```python
class _RipgrepParseError(Exception): ...   # anomaly (non-UTF-8 "bytes" member) → python fallback
def ripgrep_available() -> bool: ...                       # shutil.which("rg") is not None
def is_rg_compatible(spec: SearchSpec) -> bool: ...        # the ONE acceleration predicate (review #19)
def build_ripgrep_argv(root, spec: SearchSpec, files) -> "list[str]": ...   # explicit file list (review #13)
def parse_ripgrep_stream(lines, root) -> "Iterator[SearchMatch]": ...
```

**Parity scope (reviews #4, #19).** True `re`/rust-regex parity is impossible:
python `re` accepts constructs rg rejects, unicode case-folding (`-i`) and word
boundaries (`-w`) / symlink semantics differ, and rg reports a `"bytes"`
(base64) member instead of `"text"` for a line/path that is not valid UTF-8.
Therefore the accelerator is used **only for demonstrably-compatible specs —
case-SENSITIVE, non-whole-word, LITERAL searches** (review #19: `-i` diverges
from python's unicode case-fold and `-w` from python's `\b`, so those modes run
python too, not just regex). The predicate lives in ONE function,
`is_rg_compatible(spec)`, which the Task 3 worker consults; everything else
runs the python reference. Additionally the file list handed to rg is
**prefiltered with `passes_search_guards`** (review #19) — the same size +
NUL-in-first-8KB guards `search_file` applies — so rg's own (different) binary
detection never sees a file python would skip. The guard only inspects the
FIRST 8KB, though, so a file whose first NUL sits *after* the window passes it
and python scans it in full — rg without `--text` would stop at that NUL and
silently drop later matches, so the fixed argv carries `--text` (review #41).
The parser additionally
**signals a fallback** by raising `_RipgrepParseError` on any anomaly (a match
event whose `path`/`lines` carries `"bytes"` rather than `"text"`, i.e.
non-UTF-8), so the worker drops the rg result and re-scans in python. The
worker also falls back on spawn failure and on a non-clean rg exit
(returncode ≥ 2, or any stderr).

Semantics:
- `is_rg_compatible`: `return (not spec.regex) and spec.case_sensitive and not
  spec.whole_word` — the single place the acceleration predicate is encoded
  (review #19).
- `build_ripgrep_argv`: only ever called for an `is_rg_compatible` spec, so
  the flags are fixed: base
  `["rg", "--json", "--no-messages", "--max-filesize",
  f"{_SEARCH_MAX_BYTES}", "-F", "--text"]` (always literal, never `-i`/`-w`;
  **review #41:** `--text` because `passes_search_guards` only checks the first
  8KB for NUL — a file whose first NUL falls after that window is scanned in
  FULL by the python engine, while rg without `--text` stops at the NUL and
  loses any later match); then
  `["-e", spec.query, "--"]` followed by the EXPLICIT file paths
  `[str(Path(root) / rel) for rel in files]`. **review #13:** rg is handed the
  exact file LIST produced by `build_file_index` (not the directory root), so
  the two engines share an identical file set and cannot diverge on filesystem
  semantics. This is the robust fix for the symlink-parity gap: `os.walk`
  (default `followlinks=False`) *lists* symlinked files but rg without `--follow`
  *skips* them, while `--follow` would over-collect (descend symlinked dirs the
  walker prunes) — passing the explicit files sidesteps both. Because the files
  are enumerated explicitly, rg never consults `.gitignore`, hidden-file, or
  skip-dir rules (the index already applied them), so `--no-ignore`/`--hidden`/
  `-g` excludes are dropped. **review #19:** the caller prefilters *files* with
  `passes_search_guards(root, rel)` before building batches, so rg never scans
  a file the python engine's size/binary guards would skip (`--max-filesize`
  stays as defence-in-depth). The caller batches the filtered *files* via
  `_rg_batches` (review #34: an estimated argv BYTE budget
  `_RIPGREP_ARGV_BUDGET`, with the `_RIPGREP_BATCH` file count as the
  secondary bound; review #36: the budget is computed on the absolute
  `str(root / rel)` strings the argv actually carries) to respect ARG_MAX; `--no-messages` suppresses per-file
  stat warnings so a spurious stderr line does not trip the clean-exit
  fallback check.
- `parse_ripgrep_stream`: iterate JSON lines; for each `type == "match"` event,
  read `data.path.text` (relative to *root*, POSIX), `data.line_number`,
  `data.lines.text` (strip a single trailing `\n`), and each entry in
  `data.submatches` (`start`/`end` are **byte** offsets into the line's UTF-8).
  **If `data.path` or `data.lines` has no `"text"` member** (rg emitted a
  `"bytes"` base64 member because the content is not valid UTF-8), **raise
  `_RipgrepParseError`** — the caller falls back to the python reference (review
  #4). Convert byte offsets to **character** offsets by decoding the byte prefix
  (`len(line_bytes[:byte_off].decode("utf-8", errors="replace"))`) so spans
  match the python reference. Yield one `SearchMatch` per submatch. Ignore
  non-match event types and unparseable lines (`json.JSONDecodeError`).

**Steps**

- [ ] Append the failing `TestRipgrep` to `tests/test_gui_files_pure.py`:
    ```python
    import json
    import os
    import shutil
    import subprocess

    from spar.gui.files import (
        _RipgrepParseError,
        build_file_index,
        build_ripgrep_argv,
        is_rg_compatible,
        parse_ripgrep_stream,
        passes_search_guards,
        ripgrep_available,
    )

    _HAS_RG = shutil.which("rg") is not None


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
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py::TestRgCompatible tests/test_gui_files_pure.py::TestRipgrepArgv tests/test_gui_files_pure.py::TestRipgrepParity -q`
  - Expected: **ImportError** (`build_ripgrep_argv` not defined).

- [ ] Implement the ripgrep helpers ABOVE the guard (after `replace_in_text`).
  Add `import json` at the top; extend `__all__` with `"_RipgrepParseError"`,
  `"ripgrep_available"`, `"is_rg_compatible"`, `"build_ripgrep_argv"`,
  `"parse_ripgrep_stream"`:
    ```python
    class _RipgrepParseError(Exception):
        """rg reported a non-UTF-8 ("bytes") member — the accelerator cannot
        reproduce the python reference shape, so the worker falls back to the
        python scan (review #4)."""


    def ripgrep_available() -> bool:
        """True when the ``rg`` binary is on PATH (opportunistic accelerator)."""
        return shutil.which("rg") is not None


    def is_rg_compatible(spec: "SearchSpec") -> bool:
        """True only for specs whose rg semantics provably match the python
        reference: LITERAL, case-SENSITIVE, non-whole-word (review #19).
        rg's ``-i`` unicode case-folding and ``-w`` word boundaries diverge
        from python ``re`` (as do the regex dialects), so every other spec
        runs the python path. The ONE place this predicate is encoded."""
        return (not spec.regex) and spec.case_sensitive and not spec.whole_word


    def build_ripgrep_argv(root, spec: "SearchSpec", files) -> "list[str]":
        """Argv for ``rg --json`` over an EXPLICIT list of *files* (project-
        relative paths from build_file_index), resolved against *root*. Only
        ever called for an ``is_rg_compatible`` spec (review #19), so the
        flag set is fixed: always ``-F``, never ``-i``/``-w``.

        review #13: handing rg the exact file set the python reference scans —
        rather than a directory root — guarantees the two engines never diverge
        on filesystem semantics (symlinked files, .gitignore, hidden files,
        skip-dirs). The index already applied every skip rule, so no
        ``--no-ignore``/``--hidden``/``-g`` flags are needed. review #19: the
        caller prefilters *files* with ``passes_search_guards`` (size + binary),
        so rg never sees a file python would skip; ``--max-filesize`` stays as
        defence-in-depth. The caller batches *files* to respect ARG_MAX."""
        root = Path(root)
        argv = ["rg", "--json", "--no-messages",
                "--max-filesize", str(_SEARCH_MAX_BYTES), "-F", "--text"]
        argv += ["-e", spec.query, "--"]
        argv += [str(root / rel) for rel in files]
        return argv


    def parse_ripgrep_stream(lines, root):
        """Parse ``rg --json`` stdout *lines*, yielding SearchMatch with the
        SAME shape as search_paths. rg reports byte offsets into each line;
        remap them to CHARACTER offsets so spans match the python reference."""
        root = Path(root)
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            # Non-UTF-8 content → rg emits a "bytes" member instead of "text".
            # We cannot reproduce the python reference shape; signal fallback.
            if "text" not in data.get("path", {}) or "text" not in data.get("lines", {}):
                raise _RipgrepParseError("non-UTF-8 rg match (bytes member)")
            abs_path = Path(data["path"]["text"])
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                rel = abs_path.as_posix()
            line_no = data["line_number"]
            line_text = data["lines"]["text"]
            if line_text.endswith("\n"):
                line_text = line_text[:-1]
            line_bytes = line_text.encode("utf-8", errors="replace")
            for sm in data.get("submatches", []):
                start = len(line_bytes[: sm["start"]].decode("utf-8", errors="replace"))
                end = len(line_bytes[: sm["end"]].decode("utf-8", errors="replace"))
                yield SearchMatch(rel, line_no, line_text, start, end)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py -q`
  - Expected: argv tests pass; parity tests pass (rg present on the dev box).

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + Task 1 + Task 2 tests, no failures.

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files_pure.py`
  - `git commit -m "feat(gui): ripgrep accelerator with python-parity search shape"`

---

### Task 3: Search worker + SearchPanel — threaded, cancellable, results tree, open-at-line (Opus)

The hard concurrency task: a persistent-QThread worker with generation-token
cancellation, and the `SearchPanel` widget that drives it (query + toggles +
results tree + status). Replace UI comes in Task 4.

**Files**
- `spar/gui/files.py` (add, UNDER the guard: `_SearchWorker`, `SearchSession`
  facade, `SearchPanel`; extend the guard's imports)
- `tests/test_gui_files.py` (append `TestSearchSession`, `TestSearchPanel`)

**Interfaces (exact signatures)**
```python
class _SearchWorker(QObject):
    # review #23: one FILE per batch — (matches, scan-time fingerprint or None)
    match_batch = Signal(int, object)   # generation, (list[SearchMatch], fingerprint)
    # review #33: trailing bool = truncated (cap hit; "wyniki obcięte")
    finished = Signal(int, int, int, bool)  # generation, total_matches, total_files, truncated
    def __init__(self, project_dir, scan_file=None): ...   # scan_file injectable (slow-fake test)
    @Slot(int, object)
    def run_turn(self, generation: int, spec: "SearchSpec") -> None: ...

class SearchSession(QObject):           # GUI-thread facade (mirrors ConversationSession)
    batch = Signal(object)              # (list[SearchMatch], fingerprint), one file (generation-filtered)
    finished = Signal(int, int, bool)   # total_matches, total_files, truncated (review #33)
    def __init__(self, project_dir, parent=None, scan_file=None): ...
    def search(self, spec: "SearchSpec") -> None: ...       # lazy-start thread, bump gen, dispatch
    def cancel(self) -> None: ...                           # bump gen + _live_generation, no dispatch
    def stop(self) -> None: ...                             # idempotent abandon (bump gen, quit thread)

class SearchPanel(QWidget):
    open_location = Signal(str, int, int, int)   # rel, line, start, end
    def __init__(self, project_dir, parent=None, session=None): ...
    def focus_query(self) -> None: ...
    def stop_session(self) -> None: ...                         # idempotent; stops the search thread
    def set_replace_enabled(self, enabled: bool) -> None: ...   # Task 4 uses it; stub here → True/attr
    # internals exercised by tests: query, case_toggle, regex_toggle, word_toggle,
    #   results (QTreeWidget), status (QLabel), _current_spec(), _run_search(),
    #   _on_batch(payload), _on_finished(n, m, truncated), _invalid (bool)
```

Cancellation design (mirrors `ConversationSession`), corrected per review #2 —
**`_live_generation` is MONOTONIC and only the facade ever writes it**: the worker
only READS it for early-exit. `SearchSession.search()` increments
`self._generation`, assigns `self._worker._live_generation = self._generation`
(a plain int write; atomic in CPython) BEFORE emitting a queued
`_dispatch(gen, spec)`. The worker's `run_turn` **must not reset
`_live_generation`** — if it did, an older turn still sitting in the queue would,
when it finally runs, restore its own (lower) generation and full-scan, delaying
the newer search and possibly repainting a cleared panel. `run_turn` early-exits
its loop whenever `generation != self._live_generation`, and every worker→facade
signal is generation-stamped and dropped by the facade when stale.

Empty/invalid queries must also **cancel any in-flight search**: `SearchPanel`
calls `SearchSession.cancel()` (bumps the generation and the worker's
`_live_generation` without dispatching) before clearing the results — otherwise a
still-running older search repopulates a panel the user just cleared or that
holds an invalid query. `scan_file` is injectable so a test can supply a slow
fake and prove a superseded run delivers nothing; the injected callable must
match `search_file`'s signature including `limit` (review #37 — the worker
always passes the remaining result allowance as `limit=`).

**Thread lifetime (review #3): lazy start + explicit stop.** The `QThread` is
created in `__init__` but **started lazily on the first `search()`** (so the many
panel tests that never search never spawn a thread). `SearchSession.stop()` is
**idempotent** and mirrors the chat `stop_session()` teardown: it bumps the
generation, hands a still-running thread to `_SEARCH_ABANDONED_THREADS`, and
`quit()`s. `SearchPanel.stop_session()`/`closeEvent`,
`FilesView.stop_search()`/`closeEvent` (review #21: a STANDALONE FilesView must
tear the thread down itself — nothing routes through `MainWindow.closeEvent`),
and `MainWindow.closeEvent` all call it (idempotent), so no thread leaks.

The worker uses **ripgrep only for `is_rg_compatible` specs — case-SENSITIVE,
non-whole-word, literal — and only on the real scan path** (reviews #4, #19:
`self._scan_file is search_file and is_rg_compatible(spec) and
ripgrep_available()`; rg's `-i` case-folding and `-w` word boundaries diverge
from python `re`, so those modes take the python loop too). rg is handed the
**explicit file list** from `build_file_index` (review #13), **prefiltered with
`passes_search_guards`** (review #19: the same size/binary guards `search_file`
applies, so rg never sees a file python would skip) and batched to respect
ARG_MAX, so it scans exactly the same file set as the python reference (no
symlink/`.gitignore`/hidden-file/binary divergence). Each batch runs under
**`subprocess.Popen` with stdout drained via a reader thread + queue** (reviews
#14/#22/#24): a daemon reader thread iterates rg's stdout and pushes each line
into a `queue.Queue` (a `None` sentinel marks EOF), while a second daemon
side-thread drains stderr. The worker loop pulls lines with
`queue.get(timeout=0.05)` and re-checks `_live_generation` on **every timeout
tick**, not just between lines — review #24: a large NO-MATCH batch emits no
stdout at all, so a worker that blocked in `for line in proc.stdout` awaiting
output could not see a supersede until the batch finished, stalling the worker
and every queued search behind a silent rg. Waiting for rg to EXIT before
reading (a poll loop followed by `communicate`) would **deadlock** once the
JSON output exceeds the OS pipe capacity (~64 KB): rg blocks writing the full
pipe while the worker blocks waiting for exit; the reader thread keeps the
pipe drained regardless. A superseding search `kill()`s rg mid-stream (or
mid-silence) and returns `None`, so a newer queued query is never stalled
behind a blocking `subprocess.run`. The worker falls back to the per-file
python loop on **spawn failure**, a **non-clean exit** (returncode ≥ 2 or any
stderr), or a **parse anomaly** (`_RipgrepParseError` from a non-UTF-8
`"bytes"` member); a mid-stream cancellation also returns `None`, and the
python path's first `_live_generation` check then early-exits at once. Regex
specs, and the injected-scan test path, always take the python loop
(`scan_file` over `build_file_index`), checking `_live_generation` between
files. Batches are grouped per file to keep signal traffic bounded, and the
generation is re-checked before each emit.

**Fingerprint at scan time (reviews #23/#25).** The per-file `(st_mtime_ns,
st_size)` fingerprint that replace later verifies (review #6) is captured
**inside the scan itself**, never in the GUI — and always BEFORE the file's
bytes are read: the python path stats each file immediately BEFORE reading it;
the rg path stats **every file of a batch BEFORE launching that batch's rg
process** (a pre-launch snapshot `dict` rel → fingerprint) and parsed groups
look their fingerprints up in the snapshot. Review #25: the previous shape
(stat when a file's FIRST match row is parsed) ran AFTER rg had already read
the file — a write landing between rg's read and the parse got blessed with
the post-write fingerprint, and replace would clobber it. rg also scans a
whole batch before rows are built, so a
GUI-side stat (`_file_item`) would stamp the NEW fingerprint onto STALE
matches for a file modified in between — making it wrongly eligible for
replacement. The fingerprint therefore **travels with the batch payload**
`(list[SearchMatch], fingerprint)` — one file per batch — and `_file_item`
only stores/displays it; the write-time verification is unchanged.

**Steps**

- [ ] Append the failing tests to `tests/test_gui_files.py`:
    ```python
    class TestSearchSession:
        def test_delivers_matches(self, qtbot, tmp_path):
            from spar.gui.files import SearchSession, SearchSpec

            (tmp_path / "a.py").write_text("todo one\ntodo two\n", encoding="utf-8")
            session = SearchSession(tmp_path)
            got = []
            # review #23: payload is (matches, fingerprint) — one file per batch
            session.batch.connect(lambda payload: got.extend(payload[0]))
            done = []
            session.finished.connect(lambda n, m: done.append((n, m)))
            session.search(SearchSpec("todo"))
            qtbot.waitUntil(lambda: bool(done), timeout=5000)
            assert done[0][0] == 2  # two matches
            assert {m.line for m in got} == {1, 2}
            session.stop()

        def test_superseded_search_delivers_no_stale_results(self, qtbot, tmp_path):
            # Slow fake scan: first query is still "scanning" when the second
            # supersedes it; only the second query's results may arrive.
            import time
            from spar.gui.files import SearchSession, SearchSpec, SearchMatch

            (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

            def slow_scan(root, rel, pattern, limit=None):
                time.sleep(0.2)
                # Tag the match text with the pattern so we can tell runs apart.
                return [SearchMatch(rel, 1, pattern.pattern, 0, 1)]

            session = SearchSession(tmp_path, scan_file=slow_scan)
            got = []
            # review #23: payload is (matches, fingerprint) — one file per batch
            session.batch.connect(lambda payload: got.extend(payload[0]))
            session.search(SearchSpec("first"))
            session.search(SearchSpec("second"))  # supersedes immediately
            qtbot.wait(800)
            texts = {m.text for m in got}
            assert "first" not in texts  # stale run dropped
            session.stop()

        def test_regex_search_uses_python_not_ripgrep(self, qtbot, tmp_path, monkeypatch):
            # review #4: regex specs must never shell out to rg (no parity).
            from spar.gui import files as fmod

            (tmp_path / "a.py").write_text("v1 v2\n", encoding="utf-8")

            def _boom(*_a, **_k):
                raise AssertionError("ripgrep used for a regex search")

            monkeypatch.setattr(fmod, "build_ripgrep_argv", _boom)
            session = fmod.SearchSession(tmp_path)
            done, got = [], []
            session.finished.connect(lambda n, m: done.append((n, m)))
            # review #23: payload is (matches, fingerprint) — one file per batch
            session.batch.connect(lambda payload: got.extend(payload[0]))
            session.search(fmod.SearchSpec(r"v\d", regex=True))
            qtbot.waitUntil(lambda: bool(done), timeout=5000)
            assert done[0][0] == 2  # two matches, via the python path
            session.stop()

        def test_case_insensitive_search_uses_python_not_ripgrep(self, qtbot, tmp_path, monkeypatch):
            # review #19: rg's -i unicode case-folding diverges from python re,
            # so a case-insensitive spec (is_rg_compatible == False) must take
            # the python path, never rg.
            from spar.gui import files as fmod

            (tmp_path / "a.py").write_text("TODO todo\n", encoding="utf-8")

            def _boom(*_a, **_k):
                raise AssertionError("ripgrep used for a case-insensitive search")

            monkeypatch.setattr(fmod, "build_ripgrep_argv", _boom)
            session = fmod.SearchSession(tmp_path)
            done = []
            session.finished.connect(lambda n, m: done.append((n, m)))
            session.search(fmod.SearchSpec("todo"))  # default: case-insensitive
            qtbot.waitUntil(lambda: bool(done), timeout=5000)
            assert done[0][0] == 2  # both cases matched, via the python path
            session.stop()

        def test_cancel_bumps_generation_and_drops_stale(self, qtbot, tmp_path):
            # review #2 (empty/invalid clause): cancel() must supersede an
            # in-flight run so its late batches never reach the facade.
            import time
            from spar.gui.files import SearchSession, SearchSpec, SearchMatch

            (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

            def slow_scan(root, rel, pattern, limit=None):
                time.sleep(0.2)
                return [SearchMatch(rel, 1, "x", 0, 1)]

            session = SearchSession(tmp_path, scan_file=slow_scan)
            got = []
            # review #23: payload is (matches, fingerprint) — one file per batch
            session.batch.connect(lambda payload: got.extend(payload[0]))
            session.search(SearchSpec("first"))
            session.cancel()  # supersede without dispatching a new search
            qtbot.wait(600)
            assert got == []  # cancelled run delivered nothing
            session.stop()

        def test_stop_bumps_worker_live_generation(self, qtbot, tmp_path):
            # review #14: stop() must bump the WORKER's live generation (not just
            # the facade's), so an in-flight python scan sees the supersede and
            # bails promptly instead of finishing the whole walk.
            import time
            from spar.gui.files import SearchSession, SearchSpec, SearchMatch

            (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

            def slow_scan(root, rel, pattern, limit=None):
                time.sleep(0.2)
                return [SearchMatch(rel, 1, "x", 0, 1)]

            session = SearchSession(tmp_path, scan_file=slow_scan)
            session.search(SearchSpec("first"))
            searched_gen = session._generation
            session.stop()
            assert session._worker._live_generation == session._generation
            assert session._worker._live_generation > searched_gen

        def test_ripgrep_batch_killed_when_superseded_with_no_output_yet(self, tmp_path, monkeypatch):
            # reviews #14/#22/#24: a large NO-MATCH rg batch emits no stdout
            # at all. A worker blocking in `for line in proc.stdout` could
            # only notice a supersede on the next line — which never comes —
            # stalling this run AND every queued search. The queue-based
            # drain re-checks _live_generation on every queue.get timeout
            # tick (~50 ms), so rg is killed even while completely silent.
            import io
            import subprocess
            import threading

            from spar.gui import files as fmod

            (tmp_path / "a.py").write_text("todo\n", encoding="utf-8")
            worker = fmod._SearchWorker(tmp_path)
            worker._live_generation = 1
            killed = threading.Event()

            class FakeStdout:
                def __iter__(self):
                    return self

                def __next__(self):
                    # the daemon reader thread blocks HERE: rg has produced
                    # NO output yet. Supersede now — only a queue.get
                    # timeout tick can notice it (there is no line to read).
                    worker._live_generation = 2
                    if not killed.wait(timeout=5):
                        raise AssertionError(
                            "worker never killed the silent rg batch"
                        )
                    raise StopIteration  # kill() → stream ends

            class FakePopen:
                def __init__(self, *a, **k):
                    self.stdout = FakeStdout()
                    self.stderr = io.StringIO("")
                    self.returncode = 0

                def kill(self):
                    killed.set()

                def wait(self, timeout=None):
                    return 0

            monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
            result = worker._ripgrep_grouped(
                1, fmod.SearchSpec("todo", case_sensitive=True)
            )
            assert result is None      # cancelled → caller takes python early-exit
            assert killed.is_set()     # rg killed while it had emitted NOTHING

        def test_ripgrep_output_exceeding_pipe_capacity_completes(self, tmp_path):
            # review #22: stdout must be drained WHILE rg runs. The old shape
            # (wait for exit, then communicate) deadlocked once the --json
            # output exceeded the OS pipe capacity (~64 KB): rg blocked
            # writing the full pipe while the worker blocked waiting for
            # exit. 3000 match events ≈ several hundred KB of JSON.
            import shutil

            if shutil.which("rg") is None:
                pytest.skip("ripgrep not installed")
            from spar.gui import files as fmod

            body = "".join(f"todo padding line {i:05d}\n" for i in range(3000))
            (tmp_path / "big.py").write_text(body, encoding="utf-8")
            worker = fmod._SearchWorker(tmp_path)
            worker._live_generation = 1
            result = worker._ripgrep_grouped(
                1, fmod.SearchSpec("todo", case_sensitive=True)
            )
            assert result is not None           # completed — no deadlock
            grouped, truncated = result         # review #33: (groups, truncated)
            assert truncated is False           # 3000 < _SEARCH_MAX_RESULTS
            (rel, fingerprint, bucket), = grouped
            assert rel == "big.py" and len(bucket) == 3000
            assert fingerprint is not None      # review #25: pre-launch snapshot

        def test_batch_payload_carries_scan_time_fingerprint(self, qtbot, tmp_path):
            # review #23: the (mtime_ns, size) fingerprint is captured by the
            # WORKER (before the file is read) and travels with the payload —
            # the GUI never stats the file itself.
            from spar.gui.files import SearchSession, SearchSpec

            f = tmp_path / "a.py"
            f.write_text("todo\n", encoding="utf-8")
            st = f.stat()
            session = SearchSession(tmp_path)
            payloads = []
            session.batch.connect(payloads.append)
            done = []
            session.finished.connect(lambda n, m: done.append(n))
            session.search(SearchSpec("todo"))
            qtbot.waitUntil(lambda: bool(done), timeout=5000)
            (matches, fingerprint), = payloads
            assert [m.line for m in matches] == [1]
            assert fingerprint == (st.st_mtime_ns, st.st_size)
            session.stop()

        def test_rg_fingerprint_snapshot_taken_before_launch(self, tmp_path, monkeypatch):
            # review #25: the rg path must stat the WHOLE batch BEFORE
            # launching rg. Stat-at-first-parsed-match (the old shape) ran
            # AFTER rg had already read the file: a write landing between
            # rg's read and the parse got blessed with the POST-write
            # fingerprint, and replace would clobber the newer content.
            # Here a fake rg mutates the file mid-stream — the emitted
            # fingerprint must be the PRE-launch one, so the replace-time
            # verification (review #6) refuses with "plik zmienił się".
            import io
            import json
            import subprocess

            from spar.gui import files as fmod

            f = tmp_path / "a.py"
            f.write_text("todo\n", encoding="utf-8")
            st = f.stat()
            pre = (st.st_mtime_ns, st.st_size)

            row = json.dumps({
                "type": "match",
                "data": {
                    "path": {"text": str(f)},
                    "line_number": 1,
                    "lines": {"text": "todo\n"},
                    "submatches": [{"start": 0, "end": 4}],
                },
            }) + "\n"

            class FakeStdout:
                def __init__(self):
                    self._lines = iter([row])

                def __iter__(self):
                    return self

                def __next__(self):
                    # rg is streaming: the file changes AFTER launch (and
                    # after rg's read), BEFORE its match row is parsed.
                    f.write_text("todo but changed on disk\n", encoding="utf-8")
                    return next(self._lines)

            class FakePopen:
                def __init__(self, *a, **k):
                    self.stdout = FakeStdout()
                    self.stderr = io.StringIO("")
                    self.returncode = 0

                def kill(self):
                    pass

                def wait(self, timeout=None):
                    return 0

            monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
            worker = fmod._SearchWorker(tmp_path)
            worker._live_generation = 1
            grouped, _truncated = worker._ripgrep_grouped(
                1, fmod.SearchSpec("todo", case_sensitive=True)
            )  # review #33: returns (groups, truncated)
            (rel, fingerprint, bucket), = grouped
            assert rel == "a.py" and len(bucket) == 1
            assert fingerprint == pre  # snapshot from BEFORE the launch
            st2 = f.stat()
            # the mid-stream write no longer matches the payload fingerprint,
            # so _apply_replace's check (review #6) skips it: replace refuses.
            assert (st2.st_mtime_ns, st2.st_size) != fingerprint

        def test_rg_result_cap_enforced_during_parse_kills_rg(self, tmp_path, monkeypatch):
            # review #33: the old shape accumulated EVERY parsed match in
            # `grouped` and only capped the EMISSION in _emit_grouped — a
            # common literal over a big tree could parse millions of match
            # rows into memory (OOM). The cap must stop the PARSE: a fake rg
            # streaming far more than _SEARCH_MAX_RESULTS rows is killed the
            # moment the cap is reached, the stream is not drained further,
            # and the collected groups come back flagged truncated.
            import io
            import json
            import subprocess
            import threading

            from spar.gui import files as fmod

            f = tmp_path / "a.py"
            f.write_text("todo\n", encoding="utf-8")
            cap = fmod._SEARCH_MAX_RESULTS
            killed = threading.Event()

            def _rows():
                # an "endless" rg: serves comfortably past the cap, then
                # STALLS — only a kill (which in real life closes stdout)
                # lets the stream end. If the worker kept accumulating
                # instead of capping the parse, it would hang here and the
                # wait below would fail the test.
                for i in range(cap + 100):
                    yield json.dumps({
                        "type": "match",
                        "data": {
                            "path": {"text": str(f)},
                            "line_number": i + 1,
                            "lines": {"text": "todo\n"},
                            "submatches": [{"start": 0, "end": 4}],
                        },
                    }) + "\n"
                if not killed.wait(timeout=5):
                    raise AssertionError(
                        "worker never killed rg at the result cap"
                    )

            class FakePopen:
                def __init__(self, *a, **k):
                    self.stdout = _rows()
                    self.stderr = io.StringIO("")
                    self.returncode = 0

                def kill(self):
                    killed.set()

                def wait(self, timeout=None):
                    return 0

            monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
            worker = fmod._SearchWorker(tmp_path)
            worker._live_generation = 1
            grouped, truncated = worker._ripgrep_grouped(
                1, fmod.SearchSpec("todo", case_sensitive=True)
            )
            assert truncated is True
            assert killed.is_set()           # rg killed mid-stream at the cap
            total = sum(len(bucket) for _rel, _fp, bucket in grouped)
            assert total == cap              # collected == cap, nothing beyond

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
            import os as _os
            for b in batches:
                # review #36: measure what rg's argv actually carries.
                size = sum(
                    len(_os.fsencode(str(tmp_path / p))) + 1 for p in b
                )
                assert size <= fmod._RIPGREP_ARGV_BUDGET
                assert len(b) <= fmod._RIPGREP_BATCH     # secondary bound

        def test_python_path_result_cap_slices_single_huge_file(self, qtbot, tmp_path):
            # review #35: the python path emitted each file's COMPLETE matches
            # list BEFORE the cap check — one file with more matches than
            # _SEARCH_MAX_RESULTS blew straight past the cap. The per-file
            # list must be sliced to the remaining allowance before the emit:
            # exactly cap results arrive, flagged truncated.
            from spar.gui import files as fmod

            cap = fmod._SEARCH_MAX_RESULTS
            (tmp_path / "huge.py").write_text("x\n", encoding="utf-8")
            seen_limits = []

            def fat_scan(root, rel, pattern, limit=None):
                seen_limits.append(limit)   # review #37: spy on the allowance
                return [
                    fmod.SearchMatch(rel, i + 1, "x", 0, 1)
                    for i in range(cap + 100)   # over-returns past its limit —
                    # exercises the worker's belt-and-braces slice
                ]

            session = fmod.SearchSession(tmp_path, scan_file=fat_scan)
            got, done = [], []
            # review #23: payload is (matches, fingerprint) — one file per batch
            session.batch.connect(lambda payload: got.extend(payload[0]))
            session.finished.connect(lambda n, m, t: done.append((n, m, t)))
            session.search(fmod.SearchSpec("x"))
            qtbot.waitUntil(lambda: bool(done), timeout=5000)
            assert len(got) == cap              # sliced to EXACTLY the cap
            assert done[0] == (cap, 1, True)    # truncated flagged
            # review #37: the worker passed the REMAINING allowance down as
            # search_file's limit (first file ⇒ the full cap).
            assert seen_limits == [cap]
            session.stop()


    class TestSearchPanel:
        def _panel(self, qtbot, tmp_path):
            from spar.gui.files import SearchPanel

            (tmp_path / "a.py").write_text("todo one\nplain\n", encoding="utf-8")
            (tmp_path / "b.py").write_text("todo two\n", encoding="utf-8")
            panel = SearchPanel(tmp_path)
            # qtbot.addWidget closes the panel at teardown; SearchPanel.closeEvent
            # calls stop_session() so the search thread never leaks (review #3).
            qtbot.addWidget(panel)
            return panel

        def test_search_populates_results_tree(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            panel.query.setText("todo")
            panel._run_search()
            qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)
            files = {
                panel.results.topLevelItem(i).text(0).split(" ")[0]
                for i in range(panel.results.topLevelItemCount())
            }
            assert files == {"a.py", "b.py"}

        def test_status_reports_counts(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            panel.query.setText("todo")
            panel._run_search()
            qtbot.waitUntil(lambda: "wyników" in panel.status.text(), timeout=5000)
            assert "2" in panel.status.text()  # 2 matches
            assert "2 plik" in panel.status.text()  # in 2 files

        def test_invalid_regex_marks_query_and_disables(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            panel.regex_toggle.setChecked(True)
            panel.query.setText("(")
            panel._run_search()
            assert panel._invalid is True
            assert panel.query.property("invalid") is True

        def test_empty_query_cancels_and_clears(self, qtbot, tmp_path):
            # review #2: an empty query must bump the generation (cancel any
            # in-flight run) and clear, not silently leave a search running.
            panel = self._panel(qtbot, tmp_path)
            cancelled = []
            panel._session.cancel = lambda: cancelled.append(True)
            panel.query.setText("")
            panel._run_search()
            assert cancelled == [True]
            assert panel.results.topLevelItemCount() == 0

        def test_invalid_query_cancels_in_flight(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            cancelled = []
            panel._session.cancel = lambda: cancelled.append(True)
            panel.regex_toggle.setChecked(True)
            panel.query.setText("(")
            panel._run_search()
            assert cancelled == [True]

        def test_invalid_query_clears_results_and_resets_specs(self, qtbot, tmp_path):
            # review #28: the invalid-regex branch mirrors the empty-query
            # path — already-delivered (possibly partial) results are cleared
            # and both specs reset, so replace stays disabled and no stale
            # tree sits under the error banner.
            panel = self._panel(qtbot, tmp_path)
            panel.query.setText("todo")
            panel._run_search()
            qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
            assert panel.results.topLevelItemCount() == 2
            panel.regex_toggle.setChecked(True)  # re-runs; "todo" still valid
            panel.query.setText("todo(")         # now an invalid regex
            panel._run_search()
            assert panel._invalid is True
            assert panel.results.topLevelItemCount() == 0  # partials cleared
            assert panel._pending_spec is None
            assert panel._results_spec is None

        def test_clicking_line_emits_open_location(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            panel.query.setText("todo")
            panel._run_search()
            qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)
            emitted = []
            panel.open_location.connect(lambda *a: emitted.append(a))
            # first file's first (and only) line child
            file_item = panel.results.topLevelItem(0)
            line_item = file_item.child(0)
            panel._on_item_activated(line_item, 0)
            assert emitted and emitted[0][0] in ("a.py", "b.py")
            assert emitted[0][1] == 1  # line number
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestSearchSession tests/test_gui_files.py::TestSearchPanel -q`
  - Expected: **ImportError / AttributeError** (`SearchSession` not defined).

- [ ] Extend the guard's imports (in the existing `if _HAS_QT:` import blocks):
  `QThread`, `Slot` from `PySide6.QtCore`; `QTreeWidget`, `QTreeWidgetItem`,
  `QToolButton` from `PySide6.QtWidgets`. Add module-level
  `_SEARCH_ABANDONED_THREADS: "set[QThread]" = set()`,
  `_SEARCH_MAX_RESULTS = 5000`, `_RIPGREP_BATCH = 2000` (files per rg
  invocation, secondary bound), and `_RIPGREP_ARGV_BUDGET = 128 * 1024`
  (review #34: a fixed file COUNT alone doesn't bound argv BYTES — path
  lengths vary, so 2000 long paths could still trip ARG_MAX/E2BIG; batches
  are built by an estimated argv byte budget with the count as the secondary
  bound) near the other guarded constants, plus the batch builder:
    ```python
    def _rg_batches(root, files):
        """Split *files* into rg argv batches (review #34): primary bound is
        an estimated argv byte budget (capped at _RIPGREP_ARGV_BUDGET ≈
        128 KB — far below Linux's ~2 MB ARG_MAX, leaving room for the fixed
        flags and the environment), secondary bound _RIPGREP_BATCH files.
        review #36: the budget counts what rg's argv ACTUALLY carries —
        build_ripgrep_argv passes ``str(root / rel)``, so each entry is
        estimated as ``len(os.fsencode(str(root / rel))) + 1`` (NUL); budgeting
        the bare relatives omitted the absolute prefix (a deep *root* times
        thousands of files could silently violate the bound). Best-effort: a
        SINGLE pathological path over the budget still ships alone, and an
        E2BIG at exec surfaces as OSError from Popen, which _ripgrep_grouped
        already turns into the python-reference fallback (review #4)."""
        root = Path(root)
        batch: list = []
        size = 0
        for rel in files:
            n = len(os.fsencode(str(root / rel))) + 1
            if batch and (
                size + n > _RIPGREP_ARGV_BUDGET or len(batch) >= _RIPGREP_BATCH
            ):
                yield batch
                batch, size = [], 0
            batch.append(rel)
            size += n
        if batch:
            yield batch
    ```

- [ ] Add the pure fingerprint helper ABOVE the guard (near `search_file`) —
  review #23: the fingerprint is captured INSIDE the scan, never in the GUI:
    ```python
    def _stat_fingerprint(path):
        """``(st_mtime_ns, st_size)`` of *path*, or ``None`` when unreadable.
        reviews #23/#25: captured inside the scan itself — the python path
        stats each file BEFORE reading it; the rg path snapshots the WHOLE
        batch BEFORE launching rg (pre-launch dict rel → fingerprint) — so a
        file modified between the scan's read and the GUI row creation keeps
        its SCAN-time fingerprint and replace refuses it ("plik zmienił się")."""
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)
    ```

- [ ] Implement `_SearchWorker`, `SearchSession`, `SearchPanel` UNDER the guard
  (after `FilesView`). Reference the `ConversationSession` pattern verbatim for
  the thread lifecycle:
    ```python
        class _SearchWorker(QObject):
            """Runs the content search on a worker QThread. Every signal is
            generation-stamped; the loop early-exits when a newer generation
            supersedes it (``_live_generation`` is set from the GUI thread —
            a plain int assignment, atomic in CPython)."""

            # review #23: one FILE per batch — payload is
            # (list[SearchMatch], fingerprint-or-None) so the scan-time stat
            # travels with the matches it describes.
            match_batch = Signal(int, object)   # generation, (list[SearchMatch], fingerprint)
            # review #33: trailing bool = truncated (result cap hit mid-scan)
            finished = Signal(int, int, int, bool)  # generation, total_matches, total_files, truncated

            def __init__(self, project_dir, scan_file=None):
                super().__init__()
                self._project_dir = Path(project_dir)
                self._scan_file = scan_file or search_file
                self._live_generation = 0

            @Slot(int, object)
            def run_turn(self, generation: int, spec) -> None:
                # review #2: NEVER write _live_generation here — the facade owns
                # it (monotonic). The worker only READS it for early-exit. An
                # older queued turn writing its own generation back would delay
                # the newer search and repaint a cleared panel.
                try:
                    pattern = compile_search_pattern(spec)
                except re.error:
                    self.finished.emit(generation, 0, 0, False)
                    return
                # reviews #4/#19: ripgrep ONLY for is_rg_compatible specs
                # (case-sensitive, non-whole-word, literal) on the real scan
                # path. rg runs to completion — or to the result cap (review
                # #33) — grouped per file; None means "fall back to the python
                # reference" (spawn failure, non-clean exit, or a non-UTF-8
                # parse anomaly).
                grouped = None
                if (
                    self._scan_file is search_file
                    and is_rg_compatible(spec)
                    and ripgrep_available()
                ):
                    grouped = self._ripgrep_grouped(generation, spec)
                if grouped is not None:
                    groups, truncated = grouped  # review #33
                    self._emit_grouped(generation, groups, truncated)
                    return
                # -- python reference path (regex, injected fake, or fallback) --
                total_matches = 0
                total_files = 0
                truncated = False  # review #33: cap hit → flag, not silent stop
                for rel in build_file_index(self._project_dir):
                    if generation != self._live_generation:
                        return
                    # review #23: fingerprint BEFORE the read — a file
                    # modified after this stat (even mid-scan) must fail the
                    # replace-time verification.
                    fingerprint = _stat_fingerprint(self._project_dir / rel)
                    # review #35: cap BEFORE emitting — the old shape emitted
                    # each file's COMPLETE matches list and only then checked
                    # the cap. review #37: the remaining allowance is passed
                    # DOWN as search_file's limit so the scan stops at the cap
                    # instead of materializing millions of SearchMatch objects
                    # from one pathological file and slicing afterwards.
                    remaining = _SEARCH_MAX_RESULTS - total_matches
                    matches = self._scan_file(
                        self._project_dir, rel, pattern, limit=remaining
                    )
                    if matches:
                        # len == remaining ⇒ allowance exhausted ⇒ truncated.
                        # The slice stays as belt-and-braces for an injected
                        # scan_file that over-returns past its limit.
                        if len(matches) >= remaining:
                            matches = matches[:remaining]
                            truncated = True  # review #33: mirrors the rg path
                        total_files += 1
                        total_matches += len(matches)
                        self.match_batch.emit(generation, (matches, fingerprint))
                    if truncated:
                        break
                if generation == self._live_generation:
                    self.finished.emit(
                        generation, total_matches, total_files, truncated
                    )

            def _ripgrep_grouped(self, generation, spec):
                """Run rg for an is_rg_compatible *spec* (review #19) over the
                EXPLICIT file list from build_file_index (review #13),
                prefiltered by passes_search_guards and batched by _rg_batches
                (review #34: argv BYTE budget first, file count second;
                review #36: the budget counts the ABSOLUTE ``str(root / rel)``
                strings build_ripgrep_argv actually passes — see the
                builder's docstring; an E2BIG that still slips through
                surfaces as OSError from Popen and takes the fallback below);
                return ``(grouped, truncated)`` where *grouped* is a list of
                (rel, fingerprint, [SearchMatch]) grouped per file and
                *truncated* flags a result-cap stop (review #33) — or None to
                signal a python fallback (review #4).

                review #33: the _SEARCH_MAX_RESULTS cap is enforced HERE,
                while parsing — accumulating everything and letting
                _emit_grouped cap the EMISSION would still hold an unbounded
                `grouped` in memory (a common literal over a big tree can
                match millions of lines → OOM). On reaching the cap the
                worker kills rg mid-stream, skips the remaining batches, and
                returns what it has with truncated=True, mirroring the python
                path's cap-break.

                review #13: rg is handed the exact same file set the python
                reference scans (``rg -e pat -- f1 f2 …``) instead of a directory
                root, so the two engines can never diverge on filesystem
                semantics (symlinked files, .gitignore, hidden files, excludes).

                reviews #14/#22/#24: each batch runs under subprocess.Popen.
                A daemon reader thread drains stdout into a queue.Queue
                (None sentinel = EOF) so rg can never block on a full pipe;
                the worker pulls lines with queue.get(timeout=0.05) and
                re-checks self._live_generation on EVERY timeout tick —
                review #24: a large no-match batch emits NO stdout, so a
                worker blocking in `for line in proc.stdout` awaiting output
                could not see a supersede until the batch ended, stalling
                this run and every queued search. Waiting for exit before
                reading (poll loop + communicate) would DEADLOCK once the
                --json output exceeds the OS pipe capacity (~64 KB). A
                second daemon thread drains stderr. A superseding search
                kills rg mid-stream (or mid-silence) and returns None (the
                caller then takes the python path, whose first generation
                check early-exits at once). Never blocks a newer query
                behind a slow rg.

                reviews #23/#25: every file of a batch is statted BEFORE
                that batch's rg process is launched (pre-launch snapshot
                dict rel → fingerprint); parsed groups take their
                fingerprints from the snapshot. Stat-at-first-parsed-match
                ran AFTER rg had already read the file and could bless a
                write landing between rg's read and the parse."""
                import queue
                import subprocess
                import threading

                # review #19: apply the SAME size/binary guards the python
                # reference applies (shared predicate), so rg never scans a
                # file search_file would skip.
                files = [
                    rel for rel in build_file_index(self._project_dir)
                    if passes_search_guards(self._project_dir, rel)
                ]
                if not files:
                    return ([], False)
                # Each file appears in exactly one batch and rg keeps a file's
                # matches contiguous, so per-file grouping is stable across
                # the consecutive batches (state spans the batch loop).
                grouped: list = []
                current_rel = None
                bucket: list = []
                total_parsed = 0  # review #33: cap counted DURING the parse
                fingerprints: dict = {}  # review #25: pre-launch snapshot
                # review #34: byte-budget batches, count as secondary bound;
                # review #36: budgeted on the ABSOLUTE strings rg receives.
                for batch in _rg_batches(self._project_dir, files):
                    if generation != self._live_generation:
                        return None  # superseded → python path early-exits
                    # review #25: stat the WHOLE batch BEFORE launching rg —
                    # rg reads the files only after this snapshot, so any
                    # later write (even mid-stream) fails the replace-time
                    # fingerprint verification.
                    for rel in batch:
                        fingerprints[rel] = _stat_fingerprint(
                            self._project_dir / rel
                        )
                    argv = build_ripgrep_argv(self._project_dir, spec, batch)
                    try:
                        proc = subprocess.Popen(
                            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True,
                        )
                    except OSError:
                        return None  # spawn/exec failure
                    # review #22: drain stderr on a side thread so a warning-
                    # spewing rg can never fill THAT pipe either.
                    stderr_chunks: list = []
                    drain = threading.Thread(
                        target=lambda: stderr_chunks.append(proc.stderr.read()),
                        daemon=True,
                    )
                    drain.start()
                    # review #24: a daemon reader pushes stdout lines into a
                    # queue (None = EOF) — the worker never blocks on the
                    # pipe itself, so a supersede is noticed within ~50 ms
                    # even while rg emits nothing (large no-match batch).
                    lines: "queue.Queue" = queue.Queue()

                    def _pump(out=proc.stdout, q=lines):
                        for line in out:
                            q.put(line)
                        q.put(None)

                    reader = threading.Thread(target=_pump, daemon=True)
                    reader.start()

                    def _stdout_lines():
                        # reviews #22/#24: queue.get with a 50 ms timeout —
                        # the live generation is checked on every tick, not
                        # only between lines.
                        while True:
                            if generation != self._live_generation:
                                return  # loop below sees the stale generation
                            try:
                                line = lines.get(timeout=0.05)
                            except queue.Empty:
                                continue
                            if line is None:
                                return  # EOF — rg closed stdout
                            yield line

                    try:
                        for m in parse_ripgrep_stream(
                            _stdout_lines(), self._project_dir
                        ):
                            if m.path != current_rel:
                                if current_rel is not None:
                                    grouped.append((
                                        current_rel,
                                        # review #25: fingerprint from the
                                        # PRE-launch snapshot, never a stat
                                        # taken after rg read the file.
                                        fingerprints[current_rel],
                                        bucket,
                                    ))
                                    bucket = []
                                current_rel = m.path
                            bucket.append(m)
                            total_parsed += 1
                            if total_parsed >= _SEARCH_MAX_RESULTS:
                                # review #33: cap reached WHILE parsing —
                                # kill rg mid-stream, skip every remaining
                                # batch, flush the open bucket and return the
                                # collected groups flagged as truncated.
                                proc.kill()
                                proc.wait()
                                reader.join()
                                drain.join()
                                grouped.append((
                                    current_rel,
                                    fingerprints[current_rel],
                                    bucket,
                                ))
                                return (grouped, True)
                    except _RipgrepParseError:
                        proc.kill()
                        proc.wait()
                        reader.join()
                        drain.join()
                        return None  # non-UTF-8 "bytes" member → python reference
                    if generation != self._live_generation:
                        # review #14/#24: kill mid-stream OR mid-silence,
                        # never await — the queue drain guarantees this
                        # branch is reached within ~50 ms of the supersede.
                        proc.kill()
                        proc.wait()
                        reader.join()
                        drain.join()
                        return None  # cancelled mid-run → python early-exits
                    proc.wait()      # stdout is exhausted — rg is exiting
                    reader.join()
                    drain.join()
                    err = (stderr_chunks[0] if stderr_chunks else "") or ""
                    # rg exit codes: 0 = matches, 1 = no matches, >=2 = error.
                    if proc.returncode not in (0, 1) or err.strip():
                        return None
                if current_rel is not None:
                    grouped.append(
                        (current_rel, fingerprints[current_rel], bucket)
                    )
                return (grouped, False)

            def _emit_grouped(self, generation, grouped, truncated):
                # review #33: the cap is enforced at PARSE time (see
                # _ripgrep_grouped) — `grouped` is already ≤ the cap; the
                # break below is a defensive belt only.
                total_matches = 0
                total_files = 0
                for _rel, fingerprint, bucket in grouped:
                    if generation != self._live_generation:
                        return
                    if bucket:
                        total_files += 1
                        total_matches += len(bucket)
                        # review #23: the scan-time fingerprint travels with
                        # its file's matches.
                        self.match_batch.emit(generation, (bucket, fingerprint))
                    if total_matches >= _SEARCH_MAX_RESULTS:
                        break
                if generation == self._live_generation:
                    self.finished.emit(
                        generation, total_matches, total_files, truncated
                    )

        class SearchSession(QObject):
            """GUI-thread facade over _SearchWorker (mirrors ConversationSession):
            generation-token cancellation, generation-filtered delivery."""

            batch = Signal(object)        # (list[SearchMatch], fingerprint) — one file
            # review #33: trailing bool = truncated ("wyniki obcięte")
            finished = Signal(int, int, bool)  # total_matches, total_files, truncated
            _dispatch = Signal(int, object)  # generation, spec

            def __init__(self, project_dir, parent=None, scan_file=None):
                super().__init__(parent)
                self._generation = 0
                self._started = False   # review #3: lazy thread start
                self._stopped = False   # review #3: idempotent stop
                self._thread = QThread()
                self._worker = _SearchWorker(project_dir, scan_file=scan_file)
                self._worker.moveToThread(self._thread)
                self._dispatch.connect(
                    self._worker.run_turn, Qt.ConnectionType.QueuedConnection
                )
                self._worker.match_batch.connect(
                    self._on_batch, Qt.ConnectionType.QueuedConnection
                )
                self._worker.finished.connect(
                    self._on_finished, Qt.ConnectionType.QueuedConnection
                )
                self._thread.finished.connect(self._worker.deleteLater)

            def _ensure_started(self) -> None:
                # review #3: only spawn the QThread once a search actually runs,
                # so panels that never search never leak a thread.
                if not self._started and not self._stopped:
                    self._thread.start()
                    self._started = True

            def search(self, spec) -> None:
                self._ensure_started()
                self._generation += 1
                # Set the worker's live generation BEFORE dispatch so an
                # in-flight older run sees the bump and bails (int write is
                # atomic in CPython; the worker only ever reads it — review #2).
                self._worker._live_generation = self._generation
                self._dispatch.emit(self._generation, spec)

            def cancel(self) -> None:
                # review #2: supersede any in-flight run WITHOUT dispatching a
                # new one (empty/invalid query). Late batches are dropped by the
                # generation filter and the worker loop early-exits.
                self._generation += 1
                self._worker._live_generation = self._generation

            def stop(self) -> None:
                if self._stopped:   # review #3: idempotent
                    return
                self._stopped = True
                self._generation += 1
                # review #14: bump the worker's live generation too, so an
                # in-flight python scan (or an rg poll loop) sees the supersede
                # and bails PROMPTLY instead of finishing the whole walk before
                # the thread quits.
                self._worker._live_generation = self._generation
                thread = self._thread
                try:
                    if thread.isRunning():
                        _SEARCH_ABANDONED_THREADS.add(thread)

                        def _release(thread=thread) -> None:
                            thread.deleteLater()
                            _SEARCH_ABANDONED_THREADS.discard(thread)

                        thread.finished.connect(_release)
                    thread.quit()
                except RuntimeError:
                    pass

            def _on_batch(self, generation: int, payload) -> None:
                if generation != self._generation:
                    return
                self.batch.emit(payload)

            def _on_finished(self, generation: int, total_matches: int,
                             total_files: int, truncated: bool) -> None:
                if generation != self._generation:
                    return
                self.finished.emit(total_matches, total_files, truncated)

        class SearchPanel(QWidget):
            """Find/replace-in-files dock (ADR 0006 tranche B). Replace UI is
            added in Task 4; this task builds search + results + status."""

            open_location = Signal(str, int, int, int)  # rel, line, start, end

            def __init__(self, project_dir, parent=None, session=None):
                super().__init__(parent)
                self.setObjectName("searchPanel")
                self.project_dir = Path(project_dir)
                self._invalid = False
                self._replace_enabled = True
                # review #5/#15: the spec whose results FULLY populate the tree.
                # Replace reuses THIS, not the live controls; if the controls
                # drift from it the replace button is disabled. It is promoted
                # from _pending_spec ONLY in _on_finished (never at dispatch), so
                # a still-running search never exposes a partial tree to replace.
                self._pending_spec = None
                self._results_spec = None
                # review #16: sticky replace summary; when set, the refresh
                # search's _on_finished appends its counts to it (not overwrite).
                self._replace_summary = None

                outer = QVBoxLayout(self)
                outer.setContentsMargins(6, 4, 6, 4)
                outer.setSpacing(4)

                # -- query row --
                query_row = QHBoxLayout()
                self.query = QLineEdit(self)
                self.query.setObjectName("searchQuery")
                self.query.setPlaceholderText("Szukaj w plikach… (Enter)")
                self.query.returnPressed.connect(self._run_search)
                # review #5: editing the query without re-running marks the
                # existing results stale (disables replace).
                self.query.textChanged.connect(self._update_replace_state)
                query_row.addWidget(self.query, stretch=1)
                self.case_toggle = QToolButton(self)
                self.case_toggle.setObjectName("searchToggle")
                self.case_toggle.setText("Aa")
                self.case_toggle.setCheckable(True)
                self.case_toggle.setToolTip("Rozróżniaj wielkość liter")
                self.regex_toggle = QToolButton(self)
                self.regex_toggle.setObjectName("searchToggle")
                self.regex_toggle.setText(".*")
                self.regex_toggle.setCheckable(True)
                self.regex_toggle.setToolTip("Wyrażenie regularne")
                self.word_toggle = QToolButton(self)
                self.word_toggle.setObjectName("searchToggle")
                self.word_toggle.setText("W")
                self.word_toggle.setCheckable(True)
                self.word_toggle.setToolTip("Całe słowa")
                for tb in (self.case_toggle, self.regex_toggle, self.word_toggle):
                    tb.clicked.connect(self._run_search)
                    query_row.addWidget(tb)
                outer.addLayout(query_row)

                # -- results tree --
                self.results = QTreeWidget(self)
                self.results.setObjectName("searchResults")
                self.results.setHeaderHidden(True)
                self.results.itemActivated.connect(self._on_item_activated)
                self.results.itemClicked.connect(self._on_item_activated)
                outer.addWidget(self.results, stretch=1)

                # -- status --
                self.status = QLabel("", self)
                self.status.setObjectName("searchStatus")
                outer.addWidget(self.status)

                self._session = session or SearchSession(self.project_dir, self)
                self._session.batch.connect(self._on_batch)
                self._session.finished.connect(self._on_finished)

            # -- spec / validation --
            def _current_spec(self):
                return SearchSpec(
                    self.query.text(),
                    regex=self.regex_toggle.isChecked(),
                    case_sensitive=self.case_toggle.isChecked(),
                    whole_word=self.word_toggle.isChecked(),
                )

            def _mark_invalid(self, invalid: bool) -> None:
                self._invalid = invalid
                self.query.setProperty("invalid", invalid)
                # Repolish so the dynamic-property QSS re-applies.
                self.query.style().unpolish(self.query)
                self.query.style().polish(self.query)

            def focus_query(self) -> None:
                self.query.setFocus()
                self.query.selectAll()

            def set_replace_enabled(self, enabled: bool) -> None:
                self._replace_enabled = enabled  # Task 4 wires the widgets
                self._update_replace_state()

            def stop_session(self) -> None:
                # review #3: idempotent teardown of the search thread. Called by
                # closeEvent, FilesView.stop_search/closeEvent (review #21),
                # and MainWindow.closeEvent.
                self._session.stop()

            def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
                self.stop_session()
                super().closeEvent(event)

            def _update_replace_state(self) -> None:
                # review #5: replace targets the spec whose results are shown.
                # If the live controls drift from it (query edited, toggle
                # changed without re-running), disable replace with a hint.
                # Defensive: no-op until Task 4 adds the replace button.
                button = getattr(self, "replace_button", None)
                if button is None:
                    return
                stale = (
                    self._results_spec is None
                    or self._results_spec != self._current_spec()
                )
                button.setEnabled(self._replace_enabled and not stale)
                button.setToolTip(
                    "wyniki nieaktualne — uruchom szukanie ponownie"
                    if (stale and self._replace_enabled) else ""
                )

            # -- run --
            def _run_search(self, *_args, preserve_summary: bool = False) -> None:
                # *_args absorbs the bool that QToolButton.clicked emits (the
                # toggles connect straight to this slot). review #16: a
                # user-initiated search clears any sticky replace summary; only
                # the post-replace refresh passes preserve_summary=True.
                if not preserve_summary:
                    self._replace_summary = None
                spec = self._current_spec()
                if not spec.query:
                    # review #2: cancel any in-flight run so it can't repopulate
                    # the panel we are about to clear.
                    self._session.cancel()
                    self.results.clear()
                    self.status.setText("")
                    self._mark_invalid(False)
                    self._pending_spec = None
                    self._results_spec = None
                    self._update_replace_state()
                    return
                try:
                    compile_search_pattern(spec)
                except re.error:
                    self._session.cancel()  # review #2: cancel in-flight
                    # review #28: mirror the empty-query path — the cancelled
                    # run may already have delivered partial batches, so clear
                    # the tree and reset BOTH specs; otherwise stale partial
                    # results sit under the error banner and (with specs still
                    # set) could even be replaced.
                    self.results.clear()
                    self._pending_spec = None
                    self._results_spec = None
                    self._mark_invalid(True)
                    self.status.setText("niepoprawne wyrażenie regularne")
                    self._update_replace_state()
                    return
                self._mark_invalid(False)
                self.results.clear()
                if not preserve_summary:
                    self.status.setText("szukam…")
                # review #15: the tree is being (re)built for *spec* but is NOT
                # yet complete. Stash it as _pending_spec ONLY; keep _results_spec
                # None so replace stays DISABLED until _on_finished promotes it —
                # otherwise a partial (mid-scan) tree could be replaced.
                self._pending_spec = spec
                self._results_spec = None
                self._session.search(spec)
                self._update_replace_state()

            def _on_batch(self, payload) -> None:
                # Group into (or reuse) a file row, append line children.
                # review #23: the scan-time fingerprint arrives WITH the
                # matches; the GUI only stores it, never stats the file.
                matches, fingerprint = payload
                for m in matches:
                    file_item = self._file_item(m.path, fingerprint)
                    line_item = QTreeWidgetItem(file_item, [f"{m.line}: {m.text}"])
                    line_item.setData(0, Qt.ItemDataRole.UserRole,
                                      (m.path, m.line, m.start, m.end))
                    count = file_item.childCount()
                    file_item.setText(0, f"{m.path}  ({count})")

            def _file_item(self, rel: str, fingerprint):
                for i in range(self.results.topLevelItemCount()):
                    item = self.results.topLevelItem(i)
                    if item.data(0, Qt.ItemDataRole.UserRole + 1) == rel:
                        return item
                item = QTreeWidgetItem(self.results, [f"{rel}  (0)"])
                item.setData(0, Qt.ItemDataRole.UserRole + 1, rel)
                # review #23: store the SCAN-TIME fingerprint from the payload
                # (rows are built after rg scanned a whole batch — a GUI-side
                # stat here would bless STALE matches with a NEW fingerprint).
                item.setData(0, Qt.ItemDataRole.UserRole + 2, fingerprint)
                item.setExpanded(True)
                return item

            def _on_finished(self, total_matches: int, total_files: int,
                             truncated: bool = False) -> None:
                # review #15: the tree is now COMPLETE for _pending_spec — only
                # here (never at dispatch) is it promoted to the replace target.
                # The facade already generation-filters finished, so this fires
                # only for the current (live) search.
                self._results_spec = self._pending_spec
                counts = f"{total_matches} wyników w {total_files} plikach"
                if truncated:  # review #33: the scan stopped at the cap
                    counts += (
                        f" · wyniki obcięte do {_SEARCH_MAX_RESULTS}"
                    )
                # review #16: if a replace just ran, this is its refresh search —
                # append the counts to the sticky summary instead of clobbering
                # it, then consume the summary (persists until the next search).
                if self._replace_summary is not None:
                    self.status.setText(f"{self._replace_summary} · {counts}")
                    self._replace_summary = None
                else:
                    self.status.setText(counts)
                self._update_replace_state()

            def _on_item_activated(self, item, _col) -> None:
                payload = item.data(0, Qt.ItemDataRole.UserRole)
                if payload is None:  # a file row, not a line
                    return
                rel, line, start, end = payload
                self.open_location.emit(rel, line, start, end)
    ```
  - Note: `re`, `SearchSpec`, `compile_search_pattern`, `passes_search_guards`,
    `search_file`, `build_file_index`, `is_rg_compatible`, `build_ripgrep_argv`,
    `parse_ripgrep_stream`, `ripgrep_available` are the pure symbols from
    Tasks 1–2 (module scope). `Qt`,
    `QThread`, `Slot`, `Signal`, `QVBoxLayout`, `QHBoxLayout`, `QLineEdit`,
    `QLabel`, `QToolButton`, `QTreeWidget`, `QTreeWidgetItem` come from the
    guard's import blocks (add the missing ones).
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestSearchSession tests/test_gui_files.py::TestSearchPanel -q`
  - Expected: all listed tests pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + prior tasks + these, no failures.

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): cancellable search worker + SearchPanel results/status"`

---

### Task 4: Replace-in-files — checkbox rows, safety semantics, read-only gating (Opus)

Adds the replace field, per-file checkboxes, "Zamień zaznaczone", and the write
safety rules: refuse files with unsaved local edits, respect the read-only
matrix, re-run after replace.

**Files**
- `spar/gui/files.py` (extend `SearchPanel`: replace row + checkboxes +
  `_apply_replace`; add a `dirty_paths` provider hook)
- `tests/test_gui_files.py` (append `TestReplaceInFiles`)

**Interfaces (exact signatures)**
```python
class SearchPanel(QWidget):
    # new attributes: replace (QLineEdit), replace_button (QPushButton)
    # new hook (default: no open dirty tabs) — MainWindow injects the real one:
    dirty_open_paths: "Callable[[], set[str]]"   # returns ABS paths with unsaved edits
    def set_replace_enabled(self, enabled: bool) -> None: ...  # real impl now
    def _apply_replace(self) -> None: ...
```

Replace semantics (resolved):
- **Checkboxes are at the FILE level** (each file row is user-checkable, default
  `Qt.CheckState.Checked`; line rows are display-only). Whole-file substitution
  with the same compiled pattern replaces every match in each checked file — a
  magnifier-grade, per-file operation (stated in the ADR-scope resolution).
- For each checked file: if its **resolved** absolute path is in the
  **resolved** `dirty_open_paths()` set (an open tab with unsaved local edits —
  review #29: compared after symlink resolution on BOTH sides, so an alias of a
  dirty tab is caught too), **skip** it and count it; otherwise read
  the file from disk, `replace_in_text(text, pattern, replacement, regex=spec.regex)`
  using the **same** `compile_search_pattern(spec)` the current toggles produce,
  and write it back. The `QFileSystemWatcher` on any open **clean** tab
  auto-reloads it (tranche A behaviour).
- **Symlinks (review #20):** `os.replace` on a symlink path would swap the
  LINK itself for a regular file — destructive and user-reachable (rows are
  default-checked). So replace **resolves the real target first**
  (`Path.resolve()`) and performs the temp + `os.replace` dance on the
  RESOLVED path (mode preserved per #17, so the link stays a link and the
  target's content changes). If the resolved target escapes the (resolved)
  project root, the file is **skipped** and counted as
  `pominięto N (dowiązanie poza projektem)`.
- Status after replace:
  `"zamieniono {n} w {f} plikach"` plus, when any were skipped,
  `" · pominięto {k} (niezapisane zmiany)"` (analogous labels for the other
  skip classes, incl. `(dowiązanie poza projektem)`). Then **re-run the
  search** to refresh the results tree.
- While `FilesView` is read-only (RUNNING/GATE_PENDING/LOCKED),
  `set_replace_enabled(False)` disables the replace field, the button, and the
  row checkboxes (search stays enabled).

**Steps**

- [ ] Append the failing `TestReplaceInFiles` to `tests/test_gui_files.py`:
    ```python
    class TestReplaceInFiles:
        def _panel(self, qtbot, tmp_path):
            from spar.gui.files import SearchPanel

            (tmp_path / "a.py").write_text("cat here\ncat again\n", encoding="utf-8")
            (tmp_path / "b.py").write_text("cat only\n", encoding="utf-8")
            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            return panel

        def _search(self, qtbot, panel, text="cat"):
            panel.query.setText(text)
            panel._run_search()
            qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)

        def test_fresh_panel_starts_with_replace_disabled(self, qtbot, tmp_path):
            # review #42: __init__ must end with _update_replace_state() —
            # a fresh panel has _results_spec None (stale), so the replace
            # button starts DISABLED (Qt buttons default to enabled), and a
            # click is a guarded no-op. Rows created later while the panel is
            # read-only stay uncheckable (covered by
            # test_rows_created_while_read_only_not_checkable).
            panel = self._panel(qtbot, tmp_path)
            assert panel.replace_button.isEnabled() is False
            panel.replace.setText("dog")
            panel._apply_replace()  # no results → must not touch any file
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"

        def test_replace_writes_checked_files(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel._apply_replace()
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dog here\ndog again\n"
            assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"

        def test_unchecked_file_is_left_alone(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            # Uncheck the b.py row.
            for i in range(panel.results.topLevelItemCount()):
                item = panel.results.topLevelItem(i)
                if item.data(0, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemDataRole.UserRole + 1) == "b.py":
                    item.setCheckState(0, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Unchecked)
            panel.replace.setText("dog")
            panel._apply_replace()
            assert (tmp_path / "b.py").read_text(encoding="utf-8") == "cat only\n"

        def test_skips_files_with_unsaved_edits_and_reports(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            # a.py is "open dirty".
            panel.dirty_open_paths = lambda: {str(tmp_path / "a.py")}
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel._apply_replace()
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"
            assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
            assert "pominięto 1" in panel.status.text()

        def test_regex_replace_uses_same_toggle_pattern(self, qtbot, tmp_path):
            (tmp_path / "c.py").write_text("v1 v2\n", encoding="utf-8")
            from spar.gui.files import SearchPanel
            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            panel.regex_toggle.setChecked(True)
            panel.query.setText(r"v(\d)")
            panel._run_search()
            qtbot.waitUntil(lambda: panel.results.topLevelItemCount() >= 1, timeout=5000)
            panel.replace.setText(r"w\1")
            panel._apply_replace()
            assert (tmp_path / "c.py").read_text(encoding="utf-8") == "w1 w2\n"

        def test_read_only_disables_replace_not_search(self, qtbot, tmp_path):
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)  # give it results so the button can enable
            panel.set_replace_enabled(False)
            assert panel.replace.isEnabled() is False
            assert panel.replace_button.isEnabled() is False
            assert panel.query.isEnabled() is True  # search stays live

        def test_replace_disabled_when_query_edited_after_search(self, qtbot, tmp_path):
            # review #5: editing the query without re-running makes the results
            # stale → replace disabled with an explanatory hint.
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            assert panel.replace_button.isEnabled() is True
            panel.query.setText("dog")  # drift from the searched spec
            assert panel.replace_button.isEnabled() is False
            assert "nieaktualne" in panel.replace_button.toolTip()

        def test_replace_disabled_while_search_in_flight(self, qtbot, tmp_path):
            # review #15: replace must stay DISABLED until the search completes —
            # _results_spec is promoted only in _on_finished, never at dispatch,
            # so a partial (mid-scan) tree can never be replaced.
            import time
            from spar.gui.files import SearchMatch, SearchPanel, SearchSession

            (tmp_path / "a.py").write_text("cat\n", encoding="utf-8")

            def slow_scan(root, rel, pattern, limit=None):
                time.sleep(0.2)
                return [SearchMatch(rel, 1, "cat", 0, 3)]

            session = SearchSession(tmp_path, scan_file=slow_scan)
            panel = SearchPanel(tmp_path, session=session)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            # dispatch happened but finished has NOT fired yet.
            assert panel._results_spec is None
            assert panel.replace_button.isEnabled() is False
            qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
            assert panel.replace_button.isEnabled() is True  # enabled on finish

        def test_apply_replace_uses_stored_spec_not_current_controls(self, qtbot, tmp_path):
            # review #5: even if forced, replace must not run against a drifted
            # spec — it targets the stored one and no-ops when they differ.
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel.query.setText("zzz")  # current spec now differs from stored
            panel._apply_replace()
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"

        def test_skips_file_changed_since_search_and_reports(self, qtbot, tmp_path):
            # review #6: a file whose size/mtime changed after the search must
            # be refused (its content may no longer match the results).
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            (tmp_path / "a.py").write_text("cat cat cat\n", encoding="utf-8")  # differs
            panel.replace.setText("dog")
            panel._apply_replace()
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat cat cat\n"
            assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
            assert "plik zmienił się" in panel.status.text()

        def test_file_modified_between_scan_and_row_creation_is_skipped(self, qtbot, tmp_path):
            # review #23: the fingerprint is captured AT SCAN TIME (before the
            # file's bytes are read), not when the GUI row is built — rg scans
            # a whole batch before any row lands, so a GUI-side stat would
            # bless STALE matches with the NEW fingerprint. A file modified
            # after the scan's read but before its row exists must be refused.
            from spar.gui.files import SearchPanel, SearchSession, search_file

            target = tmp_path / "a.py"
            target.write_text("cat here\n", encoding="utf-8")

            def mutating_scan(root, rel, pattern, limit=None):
                matches = search_file(root, rel, pattern, limit=limit)
                # the file changes AFTER the scan read, BEFORE the GUI row.
                (tmp_path / rel).write_text(
                    "cat mutated after scan\n", encoding="utf-8"
                )
                return matches

            session = SearchSession(tmp_path, scan_file=mutating_scan)
            panel = SearchPanel(tmp_path, session=session)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(
                lambda: panel.results.topLevelItemCount() == 1, timeout=5000
            )
            panel.replace.setText("dog")
            panel._apply_replace()
            # untouched: the scan-time fingerprint no longer matches disk.
            assert target.read_text(encoding="utf-8") == "cat mutated after scan\n"
            assert "plik zmienił się" in panel.status.text()

        def test_skips_non_utf8_file_and_reports(self, qtbot, tmp_path):
            # review #6: strict decode — never corrupt non-UTF-8 bytes.
            from spar.gui.files import SearchPanel

            (tmp_path / "x.txt").write_bytes(b"caf\xe9 cat\n")  # invalid UTF-8
            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 1, timeout=5000)
            panel.replace.setText("dog")
            panel._apply_replace()
            assert (tmp_path / "x.txt").read_bytes() == b"caf\xe9 cat\n"  # untouched
            assert "nie-UTF-8" in panel.status.text()

        def test_replace_leaves_no_temp_file(self, qtbot, tmp_path):
            # review #6: the atomic temp file must not linger after a write.
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel._apply_replace()
            assert list(tmp_path.glob("*.spar-tmp")) == []

        def test_replace_never_clobbers_existing_spar_tmp_sibling(self, qtbot, tmp_path):
            # review #32: the old predictable temp name `<file>.spar-tmp`
            # would OVERWRITE a legitimate user file of exactly that name
            # (write_bytes truncates) and then rename it away or unlink it.
            # mkstemp's unique, exclusively-created name can never collide:
            # the pre-existing sibling must survive byte-identical.
            sibling_a = tmp_path / "a.py.spar-tmp"
            sibling_b = tmp_path / "b.py.spar-tmp"
            sibling_a.write_text("legit user file, hands off\n", encoding="utf-8")
            sibling_b.write_text("also legit\n", encoding="utf-8")
            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel._apply_replace()
            # the replace itself worked…
            assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dog here\ndog again\n"
            # …and the pre-existing *.spar-tmp files are untouched, in place.
            assert sibling_a.read_text(encoding="utf-8") == "legit user file, hands off\n"
            assert sibling_b.read_text(encoding="utf-8") == "also legit\n"

        def test_replace_preserves_executable_mode(self, qtbot, tmp_path):
            # review #17: os.replace swaps the inode, so the temp file's perms
            # would clobber the original's — a 0o755 script would lose +x. The
            # atomic writer chmods the temp to the original mode before rename.
            import os
            import stat as _stat

            script = tmp_path / "run.sh"
            script.write_text("cat here\ncat again\n", encoding="utf-8")
            os.chmod(script, 0o755)
            from spar.gui.files import SearchPanel

            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(
                lambda: panel.results.topLevelItemCount() == 1, timeout=5000
            )
            panel.replace.setText("dog")
            panel._apply_replace()
            assert script.read_text(encoding="utf-8") == "dog here\ndog again\n"
            assert _stat.S_IMODE(script.stat().st_mode) == 0o755  # +x survived

        def test_replace_through_symlink_keeps_link_writes_target(self, qtbot, tmp_path):
            # review #20: os.replace on the symlink path would swap the LINK
            # itself for a regular file. The writer resolves first, so the
            # link SURVIVES as a symlink and the TARGET's content changes.
            # The target lives in a skip-dir (node_modules) so only the LINK
            # row appears in the results.
            import os

            (tmp_path / "node_modules").mkdir()
            real = tmp_path / "node_modules" / "real.txt"
            real.write_text("cat here\n", encoding="utf-8")
            link = tmp_path / "link.txt"
            os.symlink(real, link)
            from spar.gui.files import SearchPanel

            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(
                lambda: panel.results.topLevelItemCount() == 1, timeout=5000
            )
            panel.replace.setText("dog")
            panel._apply_replace()
            assert link.is_symlink()  # the link is STILL a symlink
            assert real.read_text(encoding="utf-8") == "dog here\n"  # target rewritten

        def test_replace_skips_symlink_escaping_project(self, qtbot, tmp_path):
            # review #20: a symlink resolving OUTSIDE the project root is never
            # written — skipped and reported.
            import os

            outside = tmp_path / "outside"
            outside.mkdir()
            target = outside / "real.txt"
            target.write_text("cat here\n", encoding="utf-8")
            proj = tmp_path / "proj"
            proj.mkdir()
            link = proj / "link.txt"
            os.symlink(target, link)
            from spar.gui.files import SearchPanel

            panel = SearchPanel(proj)
            qtbot.addWidget(panel)
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(
                lambda: panel.results.topLevelItemCount() == 1, timeout=5000
            )
            panel.replace.setText("dog")
            panel._apply_replace()
            assert target.read_text(encoding="utf-8") == "cat here\n"  # untouched
            assert link.is_symlink()
            assert "pominięto 1 (dowiązanie poza projektem)" in panel.status.text()

        def test_replace_via_symlink_alias_of_dirty_tab_is_skipped(self, qtbot, tmp_path):
            # review #29: the target is open DIRTY under its real path while
            # the results row reaches it through a symlink alias. Comparing
            # unresolved strings would miss it and overwrite the dirty file —
            # resolved-vs-resolved comparison skips it as niezapisane zmiany.
            # The target lives in a skip-dir so only the LINK row appears.
            import os

            (tmp_path / "node_modules").mkdir()
            real = tmp_path / "node_modules" / "real.txt"
            real.write_text("cat here\n", encoding="utf-8")
            link = tmp_path / "link.txt"
            os.symlink(real, link)
            from spar.gui.files import SearchPanel

            panel = SearchPanel(tmp_path)
            qtbot.addWidget(panel)
            panel.dirty_open_paths = lambda: {str(real)}  # dirty under REAL path
            panel.query.setText("cat")
            panel._run_search()
            qtbot.waitUntil(
                lambda: panel.results.topLevelItemCount() == 1, timeout=5000
            )
            panel.replace.setText("dog")
            panel._apply_replace()
            assert real.read_text(encoding="utf-8") == "cat here\n"  # untouched
            assert "pominięto 1 (niezapisane zmiany)" in panel.status.text()

        def test_symlink_loop_row_skipped_without_aborting_batch(self, qtbot, tmp_path):
            # review #30: resolving a symlink loop raises (RuntimeError on
            # older Pythons, OSError/ELOOP via stat elsewhere). It must be
            # caught PER ROW — counted as błąd zapisu — while the rest of the
            # batch is still replaced, never abort _apply_replace wholesale.
            import os

            panel = self._panel(qtbot, tmp_path)
            self._search(qtbot, panel)  # rows for a.py and b.py
            # a.py becomes a self-referential symlink AFTER the scan.
            (tmp_path / "a.py").unlink()
            os.symlink(tmp_path / "a.py", tmp_path / "a.py")
            panel.replace.setText("dog")
            panel._apply_replace()  # must NOT raise
            assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
            assert "błąd zapisu" in panel.status.text()

        def test_skip_warning_survives_refresh(self, qtbot, tmp_path):
            # review #16: the replace summary (with skip warnings) must persist
            # through the async refresh search — its _on_finished appends counts
            # to the summary instead of overwriting it.
            panel = self._panel(qtbot, tmp_path)
            panel.dirty_open_paths = lambda: {str(tmp_path / "a.py")}
            self._search(qtbot, panel)
            panel.replace.setText("dog")
            panel._apply_replace()
            # let the refresh search complete (its finished fires on a later turn)
            qtbot.waitUntil(lambda: "wyników" in panel.status.text(), timeout=5000)
            assert "pominięto 1" in panel.status.text()  # skip warning survived

        def test_rows_created_while_read_only_not_checkable(self, qtbot, tmp_path):
            # review #11: rows built while replace is disabled must not be
            # user-checkable.
            from PySide6.QtCore import Qt

            panel = self._panel(qtbot, tmp_path)
            panel.set_replace_enabled(False)
            self._search(qtbot, panel)
            item = panel.results.topLevelItem(0)
            assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestReplaceInFiles -q`
  - Expected: **AttributeError** (`panel.replace` not defined).

- [ ] Extend `SearchPanel.__init__` with the replace row (add `QPushButton` to
  the guard imports if not already present) and add the replace logic. Also make
  file rows checkable in `_file_item`, and add the `dirty_open_paths` hook:
    ```python
        # in __init__, after the query_row and BEFORE the results tree:
                replace_row = QHBoxLayout()
                self.replace = QLineEdit(self)
                self.replace.setObjectName("replaceField")
                self.replace.setPlaceholderText("Zamień na…")
                replace_row.addWidget(self.replace, stretch=1)
                self.replace_button = QPushButton("Zamień zaznaczone", self)
                self.replace_button.setObjectName("replaceButton")
                self.replace_button.clicked.connect(self._apply_replace)
                replace_row.addWidget(self.replace_button)
                outer.addLayout(replace_row)  # ensure this is inserted before results

        # default hook: no open editor is dirty (MainWindow injects the real one).
        # set as an instance attribute at the END of __init__:
                self.dirty_open_paths = lambda: set()
                # review #42: QPushButton defaults to ENABLED, and nothing else
                # runs before the first search — sync the fresh panel now
                # (_results_spec is None → stale → button disabled). Must be
                # the LAST line of __init__, after all controls exist.
                self._update_replace_state()
    ```
  - Make file rows checkable (checked by default) inside `_file_item`, but
    **only when replace is enabled** (review #11: a row created while the panel
    is read-only must not be user-checkable). The **scan-time fingerprint**
    (mtime_ns + size) is already stored at `UserRole + 2` by Task 3 — review
    #23: it is captured INSIDE the scan and delivered with the batch payload,
    never stat'ed here (rows are built after rg scanned a whole batch, so a
    GUI-side stat would stamp a NEW fingerprint onto STALE matches) — so
    replace can detect an on-disk change before writing (review #6):
    ```python
                item = QTreeWidgetItem(self.results, [f"{rel}  (0)"])
                item.setData(0, Qt.ItemDataRole.UserRole + 1, rel)
                # reviews #6/#23: scan-time fingerprint from the payload.
                item.setData(0, Qt.ItemDataRole.UserRole + 2, fingerprint)
                item.setCheckState(0, Qt.CheckState.Checked)  # default checked
                if self._replace_enabled:  # review #11
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setExpanded(True)
                return item
    ```
  - Add the atomic-write helper ABOVE the guard (near `replace_in_text`, review
    #6) — add `import stat` and `import tempfile` to the top-level imports —
    and extend `__all__` with `"_atomic_write_bytes"`:
    ```python
    def _atomic_write_bytes(path, data: bytes) -> None:
        """Write *data* to *path* atomically: a temp file in the same directory
        then ``os.replace`` (same-filesystem rename). Preserves exact bytes —
        no newline translation, no encoding round-trip (review #6). review #32:
        the temp file comes from ``tempfile.mkstemp`` (unique random name,
        O_EXCL-created) — the old predictable sibling ``<file>.spar-tmp`` would
        have OVERWRITTEN a legitimate user file of that exact name and then
        deleted it (unlink in the error path) or renamed it away; mkstemp can
        never collide with an existing file. Because ``os.replace`` swaps in a
        fresh inode, the temp fd is ``fchmod``'d to the ORIGINAL file's
        permission bits BEFORE the rename so executable bits (e.g. a 0o755
        script) survive the replace (review #17; mkstemp's default is 0o600,
        so without this even plain 0o644 files would come out unreadable to
        the group). review #20: *path* is resolved first — os.replace on a
        symlink path would replace the LINK itself with a regular file, so the
        dance always targets the real file (the caller has already done the
        project-root escape check on the resolved path)."""
        path = Path(path).resolve()  # review #20: write the TARGET, not the link
        # review #32: unique + exclusive; keep the .spar-tmp suffix so humans
        # (and the leftover-cleanup test) can still recognise strays.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".spar-tmp"
        )
        tmp = Path(tmp_name)
        try:
            try:
                orig_mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                orig_mode = None
            with os.fdopen(fd, "wb") as fh:  # review #32: write via the fd
                if orig_mode is not None:
                    # review #17: preserve exec/perm bits (fchmod while the
                    # fd is guaranteed open — no window for an fd leak)
                    os.fchmod(fd, orig_mode)
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    ```
    (`os.fdopen` takes ownership of `fd` — it is closed exactly once, by the
    `with` block, before the rename.)
  - Implement `set_replace_enabled` and `_apply_replace`. Replace targets the
    **stored** search spec (review #5) and is destructively safe (review #6):
    strict-UTF-8 decode with a skip class, a search-time fingerprint check, and
    an atomic byte-preserving write, each file wrapped so one failure never
    aborts the batch:
    ```python
            def set_replace_enabled(self, enabled: bool) -> None:
                self._replace_enabled = enabled
                self.replace.setEnabled(enabled)
                for i in range(self.results.topLevelItemCount()):
                    item = self.results.topLevelItem(i)
                    flags = item.flags()
                    if enabled:
                        flags |= Qt.ItemFlag.ItemIsUserCheckable
                    else:
                        flags &= ~Qt.ItemFlag.ItemIsUserCheckable
                    item.setFlags(flags)
                # _update_replace_state also honours result-staleness (review #5).
                self._update_replace_state()

            def _apply_replace(self) -> None:
                if not self._replace_enabled:
                    return
                # review #5: replace the STORED search spec, never the live
                # controls (which may have drifted). If the results are stale
                # the button is already disabled, but guard defensively.
                spec = self._results_spec
                if spec is None or spec != self._current_spec():
                    return
                try:
                    pattern = compile_search_pattern(spec)
                except re.error:
                    return
                replacement = self.replace.text()
                # review #29: dirty-tab protection compares RESOLVED targets on
                # BOTH sides — a file open dirty under one path must also block
                # a replace reaching the same file via a symlink alias.
                dirty = set()
                for p in self.dirty_open_paths():
                    try:
                        dirty.add(str(Path(p).resolve()))
                    except (OSError, RuntimeError):
                        dirty.add(str(p))  # unresolvable → keep the raw string
                # review #20: escape check is done on RESOLVED paths, both sides.
                root_resolved = Path(self.project_dir).resolve()
                total = files = 0
                skipped_dirty = skipped_changed = skipped_nonutf8 = skipped_err = 0
                skipped_symlink = 0
                for i in range(self.results.topLevelItemCount()):
                    item = self.results.topLevelItem(i)
                    if item.checkState(0) != Qt.CheckState.Checked:
                        continue
                    rel = item.data(0, Qt.ItemDataRole.UserRole + 1)
                    fingerprint = item.data(0, Qt.ItemDataRole.UserRole + 2)
                    abs_path = self.project_dir / rel
                    # review #20: os.replace on a symlink path would replace the
                    # LINK with a regular file. Resolve to the real target and
                    # operate on THAT; a target outside the project is refused.
                    # review #30: resolution runs INSIDE the per-row try — a
                    # symlink loop raises (RuntimeError on older Pythons,
                    # OSError/ELOOP elsewhere) and must skip THIS row only,
                    # never abort the whole batch.
                    try:
                        real_path = abs_path.resolve()
                    except (OSError, RuntimeError):
                        skipped_err += 1
                        continue
                    # review #29: check the RESOLVED target against the
                    # RESOLVED dirty set, so a symlink alias of a dirty tab
                    # is skipped as niezapisane zmiany.
                    if str(real_path) in dirty:
                        skipped_dirty += 1
                        continue
                    try:
                        real_path.relative_to(root_resolved)
                    except ValueError:
                        skipped_symlink += 1
                        continue
                    try:
                        st = real_path.stat()
                    except OSError:
                        skipped_err += 1
                        continue
                    # review #6: refuse to write if the file changed since the
                    # SCAN-time fingerprint (review #23) was captured.
                    if fingerprint is None or (st.st_mtime_ns, st.st_size) != fingerprint:
                        skipped_changed += 1
                        continue
                    try:
                        raw = real_path.read_bytes()
                    except OSError:
                        skipped_err += 1
                        continue
                    try:
                        text = raw.decode("utf-8")  # STRICT — never corrupt bytes
                    except UnicodeDecodeError:
                        skipped_nonutf8 += 1
                        continue
                    try:
                        new_text, n = replace_in_text(
                            text, pattern, replacement, regex=spec.regex
                        )
                    except re.error:
                        skipped_err += 1
                        continue
                    if not n:
                        continue
                    try:
                        # No newline translation: encode and write bytes
                        # atomically — to the RESOLVED target (review #20).
                        _atomic_write_bytes(real_path, new_text.encode("utf-8"))
                    except OSError:
                        skipped_err += 1
                        continue
                    total += n
                    files += 1
                msg = f"zamieniono {total} w {files} plikach"
                skips = []
                if skipped_dirty:
                    skips.append(f"{skipped_dirty} (niezapisane zmiany)")
                if skipped_symlink:
                    skips.append(f"{skipped_symlink} (dowiązanie poza projektem)")
                if skipped_changed:
                    skips.append(f"{skipped_changed} (plik zmienił się)")
                if skipped_nonutf8:
                    skips.append(f"{skipped_nonutf8} (nie-UTF-8)")
                if skipped_err:
                    skips.append(f"{skipped_err} (błąd zapisu)")
                if skips:
                    msg += " · pominięto " + ", ".join(skips)
                # review #16: make the replace summary STICKY. Setting the label
                # synchronously is not enough — the refresh search below finishes
                # on a later event-loop turn and its _on_finished would clobber
                # this text. Stash it so that _on_finished APPENDS its counts to
                # the summary instead of overwriting it, and re-run the search
                # with preserve_summary so the transient "szukam…" never wins.
                self._replace_summary = msg
                self.status.setText(msg)
                self._run_search(preserve_summary=True)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestReplaceInFiles -q`
  - Expected: all pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + prior tasks + these, no failures.

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): replace-in-files with dirty-skip + read-only gating"`

---

### Task 5: Ctrl+F editor find/replace bar (Sonnet)

An in-editor find/replace bar in each `EditorTab`: find + replace fields, case
toggle, next/prev, F3/Shift+F3, wrap, highlight-all (merged with the current-line
highlight), replace respecting read-only, Esc to close.

**Files**
- `spar/gui/files.py` (add `EditorFindBar`; extend `FileEditor` to merge find
  ExtraSelections with the current-line highlight; extend `EditorTab` to host
  the bar + `open_find`)
- `tests/test_gui_files.py` (append `TestEditorFindBar`)

**Interfaces (exact signatures)**
```python
class FileEditor(QPlainTextEdit):
    def set_match_selections(self, spans: "list[tuple[int, int]]") -> None: ...
    # merges find highlights with the current-line highlight in one setExtraSelections

class EditorFindBar(QWidget):
    def __init__(self, editor: "FileEditor", parent=None): ...
    def open(self, prefill: str = "") -> None: ...   # show, focus find, select-all
    def close_bar(self) -> None: ...                 # hide, clear match highlights
    def apply_read_only(self, ro: bool) -> None: ... # (#7) refresh replace controls
    def find_next(self) -> bool: ...
    def find_prev(self) -> bool: ...
    def replace_one(self) -> None: ...
    def replace_all(self) -> int: ...
    # internals for tests: find_field, replace_field, case_toggle,
    #   replace_all_button, _f3 / _shift_f3 (QShortcut wiring pins)

class EditorTab(QWidget):
    def open_find(self, prefill: str = "") -> None: ...   # show its EditorFindBar
    # set_read_only also refreshes the (possibly open) find bar's replace
    # controls via EditorFindBar.apply_read_only (review #7)
```

`FileEditor` refactor: keep the current-line highlight in
`self._current_line_selection` (a one-element list) and the find highlights in
`self._match_selections`; a private `_apply_extra_selections()` calls
`setExtraSelections(self._current_line_selection + self._match_selections)`
— **current-line FIRST, matches AFTER** (review #10) so a match on the active
line is not hidden under the full-width current-line band.
`_highlight_current_line` rebuilds `self._current_line_selection` then calls
`_apply_extra_selections`; `set_match_selections` rebuilds `self._match_selections`
(each span → an `ExtraSelection` with background `TOKENS["warn"]`) then calls it.
This preserves the tranche-A current-line highlight (its test stays green) while
adding match highlights.

**Unicode offsets (review #8).** Python string offsets are code-point indices,
but `QTextCursor.setPosition` counts UTF-16 code units, so a non-BMP char (e.g.
😀) desynchronises them and selects the wrong range. A pure helper
`_utf16_offset(text, char_index) -> int` (`len(text[:char_index].encode("utf-16-le")) // 2`)
maps a code-point offset to the UTF-16 position; `set_match_selections` converts
every span through it before `setPosition`.

Find semantics: case-insensitive unless `case_toggle`, implemented with
`re.finditer(re.escape(needle), text, flags)` — **not** `str.lower()` scanning,
because case folding can change length (e.g. `"İ".lower()` → two code points) and
corrupt offsets (review #8). `find_next`/`find_prev` pick the match after/before
the cursor over the document text, **wrapping** around, then select it via
UTF-16-converted positions and `centerCursor`; F3/Shift+F3 map to next/prev.
`replace_one` replaces the current selection when it matches the pattern;
`replace_all` uses `pattern.subn`. Replace fields/buttons are disabled when
`editor.isReadOnly()` — refreshed on `open` **and** whenever `set_read_only` is
toggled while the bar is open (review #7).

**Steps**

- [ ] Append the failing `TestEditorFindBar` to `tests/test_gui_files.py`:
    ```python
    class TestEditorFindBar:
        def _tab(self, qtbot, tmp_path, text="alpha beta alpha gamma alpha\n"):
            from spar.gui.files import EditorTab

            f = tmp_path / "a.py"
            f.write_text(text, encoding="utf-8")
            tab = EditorTab(f)
            qtbot.addWidget(tab)
            return tab

        def test_open_prefills_and_shows(self, qtbot, tmp_path):
            tab = self._tab(qtbot, tmp_path)
            tab.open_find(prefill="alpha")
            assert tab.find_bar.isHidden() is False
            assert tab.find_bar.find_field.text() == "alpha"

        def test_find_next_wraps(self, qtbot, tmp_path):
            tab = self._tab(qtbot, tmp_path)
            bar = tab.find_bar
            bar.open("alpha")
            assert bar.find_next() is True
            first = tab.editor.textCursor().selectedText()
            assert first == "alpha"
            bar.find_next()
            bar.find_next()
            # a 4th next wraps back to the first occurrence (only 3 exist)
            assert bar.find_next() is True

        def test_highlight_all_marks_every_match(self, qtbot, tmp_path):
            from PySide6.QtGui import QColor, QTextFormat

            from spar.gui.theme import TOKENS

            tab = self._tab(qtbot, tmp_path)
            bar = tab.find_bar
            bar.open("alpha")
            bar.find_next()  # triggers highlight-all
            sels = tab.editor.extraSelections()
            # review #10: current-line FIRST, 3 match selections AFTER.
            assert len(sels) == 4
            warn = QColor(TOKENS["warn"])
            match_bgs = [s.format.background().color() for s in sels[1:]]
            assert all(c == warn for c in match_bgs)  # assert formats, not count
            # the current-line band is full-width; the matches are not
            assert sels[0].format.property(
                QTextFormat.Property.FullWidthSelection
            )

        def test_f3_and_shift_f3_wired(self, qtbot, tmp_path):
            # review #7: F3 / Shift+F3 map to next/prev (emit-pin the shortcuts).
            tab = self._tab(qtbot, tmp_path)
            bar = tab.find_bar
            bar.open("alpha")
            bar._f3.activated.emit()
            assert tab.editor.textCursor().selectedText() == "alpha"
            first = tab.editor.textCursor().selectionStart()
            bar._f3.activated.emit()
            assert tab.editor.textCursor().selectionStart() > first  # moved on
            bar._shift_f3.activated.emit()
            assert tab.editor.textCursor().selectionStart() == first  # back

        def test_real_f3_keypress_navigates_next_and_prev(self, qtbot, tmp_path):
            # review #26 (both-halves rule): the emit-pin above proves the
            # signal→slot wiring; this proves the REAL key half — a physical
            # F3 / Shift+F3 pressed while a QLineEdit child holds focus must
            # reach the bar (WidgetWithChildrenShortcut) instead of being
            # swallowed by the line edit.
            from PySide6.QtCore import Qt

            tab = self._tab(qtbot, tmp_path)
            tab.show()
            qtbot.waitExposed(tab)
            bar = tab.find_bar
            bar.open("alpha")  # focuses find_field (a child QLineEdit)
            assert bar.find_field.hasFocus()
            qtbot.keyClick(bar.find_field, Qt.Key.Key_F3)
            assert tab.editor.textCursor().selectedText() == "alpha"
            first = tab.editor.textCursor().selectionStart()
            qtbot.keyClick(bar.find_field, Qt.Key.Key_F3)
            assert tab.editor.textCursor().selectionStart() > first  # next
            qtbot.keyClick(
                bar.find_field,
                Qt.Key.Key_F3,
                Qt.KeyboardModifier.ShiftModifier,
            )
            assert tab.editor.textCursor().selectionStart() == first  # prev

        def test_case_insensitive_uses_regex_not_lower(self, qtbot, tmp_path):
            # review #8: "İ".lower() is two code points; a lower()-based scan
            # would desync offsets. A regex IGNORECASE search stays aligned.
            tab = self._tab(qtbot, tmp_path, text="x İ y İ\n")
            bar = tab.find_bar
            bar.open("i̇")  # combining form should NOT match; İ literal should
            bar.find_field.setText("İ")
            assert bar.find_next() is True
            assert tab.editor.textCursor().selectedText() == "İ"

        def test_non_bmp_span_selects_correct_range(self, qtbot, tmp_path):
            # review #8: a match after a non-BMP char (😀) must select the right
            # UTF-16 range, not a code-point-shifted one.
            tab = self._tab(qtbot, tmp_path, text="😀 alpha\n")
            bar = tab.find_bar
            bar.open("alpha")
            assert bar.find_next() is True
            assert tab.editor.textCursor().selectedText() == "alpha"

        def test_replace_one(self, qtbot, tmp_path):
            tab = self._tab(qtbot, tmp_path)
            bar = tab.find_bar
            bar.open("alpha")
            bar.replace_field.setText("X")
            bar.find_next()
            bar.replace_one()
            assert tab.editor.toPlainText().startswith("X beta alpha")

        def test_replace_all(self, qtbot, tmp_path):
            tab = self._tab(qtbot, tmp_path)
            bar = tab.find_bar
            bar.open("alpha")
            bar.replace_field.setText("X")
            assert bar.replace_all() == 3
            assert "alpha" not in tab.editor.toPlainText()

        def test_replace_disabled_when_read_only(self, qtbot, tmp_path):
            tab = self._tab(qtbot, tmp_path)
            tab.set_read_only(True)
            tab.open_find("alpha")
            assert tab.find_bar.replace_field.isEnabled() is False
            assert tab.find_bar.replace_all_button.isEnabled() is False

        def test_read_only_toggled_while_bar_open_updates_controls(self, qtbot, tmp_path):
            # review #7: locking the editor while the find bar is already open
            # must disable its replace controls (not just on next open).
            tab = self._tab(qtbot, tmp_path)
            tab.open_find("alpha")
            assert tab.find_bar.replace_field.isEnabled() is True
            tab.set_read_only(True)
            assert tab.find_bar.replace_field.isEnabled() is False
            assert tab.find_bar.replace_button.isEnabled() is False
            tab.set_read_only(False)
            assert tab.find_bar.replace_field.isEnabled() is True

        def test_current_line_highlight_survives(self, qtbot, tmp_path):
            # Tranche-A invariant: opening the find bar must not drop the
            # current-line highlight.
            tab = self._tab(qtbot, tmp_path)
            tab.editor.set_match_selections([])
            assert len(tab.editor.extraSelections()) == 1  # current line only
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestEditorFindBar -q`
  - Expected: **AttributeError** (`tab.find_bar` / `set_match_selections` missing).

- [ ] Add the pure UTF-16 offset helper ABOVE the guard (near `search_text`,
  review #8) and extend `__all__` with `"_utf16_offset"`:
    ```python
    def _utf16_offset(text: str, char_index: int) -> int:
        """Map a Python code-point offset into *text* to a UTF-16 code-unit
        offset (what QTextCursor.setPosition counts). Non-BMP chars occupy two
        UTF-16 units, so the two indices diverge without this (review #8)."""
        return len(text[:char_index].encode("utf-16-le")) // 2
    ```

- [ ] Refactor `FileEditor` for merged ExtraSelections and add
  `set_match_selections`. Replace `_highlight_current_line` body and add the
  helpers (keep the existing signal/gutter code untouched):
    ```python
            # in __init__, before the first _highlight_current_line() call:
                self._current_line_selection = []
                self._match_selections = []

            def _apply_extra_selections(self) -> None:
                # review #10: current-line FIRST, matches AFTER, so a match on
                # the active line paints on top of the full-width current line.
                self.setExtraSelections(
                    self._current_line_selection + self._match_selections
                )

            def _highlight_current_line(self) -> None:
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(QColor(TOKENS["panel-alt"]))
                selection.format.setProperty(
                    QTextFormat.Property.FullWidthSelection, True
                )
                selection.cursor = self.textCursor()
                selection.cursor.clearSelection()
                self._current_line_selection = [selection]
                self._apply_extra_selections()

            def set_match_selections(self, spans) -> None:
                """Highlight every (char_start, char_end) span with the warn
                colour, merged with the current-line highlight. Char offsets are
                mapped to UTF-16 positions so non-BMP text selects correctly
                (review #8)."""
                text = self.toPlainText()
                sels = []
                for start, end in spans:
                    sel = QTextEdit.ExtraSelection()
                    sel.format.setBackground(QColor(TOKENS["warn"]))
                    cur = self.textCursor()
                    cur.setPosition(_utf16_offset(text, start))
                    cur.setPosition(
                        _utf16_offset(text, end), QTextCursor.MoveMode.KeepAnchor
                    )
                    sel.cursor = cur
                    sels.append(sel)
                self._match_selections = sels
                self._apply_extra_selections()
    ```
  - Add `QTextCursor` to the guard's `PySide6.QtGui` imports.

- [ ] Implement `EditorFindBar` and wire it into `EditorTab`. Add `QToolButton`
  / `QPushButton` to the guard imports if missing. In `EditorTab.__init__`, add
  the bar between the disk banner and the editor:
    ```python
        class EditorFindBar(QWidget):
            """In-editor Ctrl+F find/replace bar (ADR 0006 tranche B)."""

            def __init__(self, editor, parent=None):
                super().__init__(parent)
                self.setObjectName("editorFindBar")
                self._editor = editor
                row = QHBoxLayout(self)
                row.setContentsMargins(6, 2, 6, 2)
                self.find_field = QLineEdit(self)
                self.find_field.setObjectName("findField")
                self.find_field.setPlaceholderText("Znajdź")
                self.find_field.textChanged.connect(self._on_find_text_changed)
                self.find_field.returnPressed.connect(self.find_next)
                row.addWidget(self.find_field, stretch=1)
                self.case_toggle = QToolButton(self)
                self.case_toggle.setObjectName("searchToggle")
                self.case_toggle.setText("Aa")
                self.case_toggle.setCheckable(True)
                self.case_toggle.clicked.connect(self._rehighlight)
                row.addWidget(self.case_toggle)
                self.prev_button = QPushButton("◀", self)
                self.prev_button.clicked.connect(self.find_prev)
                self.next_button = QPushButton("▶", self)
                self.next_button.clicked.connect(self.find_next)
                row.addWidget(self.prev_button)
                row.addWidget(self.next_button)
                self.replace_field = QLineEdit(self)
                self.replace_field.setObjectName("findReplaceField")
                self.replace_field.setPlaceholderText("Zamień")
                row.addWidget(self.replace_field, stretch=1)
                self.replace_button = QPushButton("Zamień", self)
                self.replace_button.clicked.connect(self.replace_one)
                self.replace_all_button = QPushButton("Zamień wszystko", self)
                self.replace_all_button.clicked.connect(self.replace_all)
                row.addWidget(self.replace_button)
                row.addWidget(self.replace_all_button)
                # review #7: F3 / Shift+F3 jump next/previous. QShortcuts scoped
                # to the bar+children fire even though focus is in a QLineEdit
                # (which would otherwise swallow the key); the .activated signals
                # are also the emit-pins the wiring tests exercise.
                self._f3 = QShortcut(QKeySequence(Qt.Key.Key_F3), self)
                self._f3.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self._f3.activated.connect(self.find_next)
                self._shift_f3 = QShortcut(QKeySequence("Shift+F3"), self)
                self._shift_f3.setContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut
                )
                self._shift_f3.activated.connect(self.find_prev)
                self.setVisible(False)

            # -- open / close --
            def apply_read_only(self, ro: bool) -> None:
                # review #7: keep the replace controls in sync with the editor's
                # read-only state even while the bar is already open.
                self.replace_field.setEnabled(not ro)
                self.replace_button.setEnabled(not ro)
                self.replace_all_button.setEnabled(not ro)

            def open(self, prefill: str = "") -> None:
                if prefill:
                    self.find_field.setText(prefill)
                self.apply_read_only(self._editor.isReadOnly())
                self.setVisible(True)
                self.find_field.setFocus()
                self.find_field.selectAll()
                self._rehighlight()

            def close_bar(self) -> None:
                self.setVisible(False)
                self._editor.set_match_selections([])
                self._editor.setFocus()

            def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    self.close_bar()
                    return
                if key == Qt.Key.Key_F3:  # review #7 (also handled by QShortcut)
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        self.find_prev()
                    else:
                        self.find_next()
                    return
                super().keyPressEvent(event)

            # -- search (review #8: re-based, never str.lower — case folding can
            #    change length, e.g. "İ".lower() → two code points) --
            def _pattern(self):
                needle = self.find_field.text()
                if not needle:
                    return None
                flags = 0 if self.case_toggle.isChecked() else re.IGNORECASE
                return re.compile(re.escape(needle), flags)

            def _on_find_text_changed(self, _t) -> None:
                self._rehighlight()

            def _rehighlight(self) -> None:
                pat = self._pattern()
                spans = []
                if pat is not None:
                    text = self._editor.toPlainText()
                    spans = [(m.start(), m.end()) for m in pat.finditer(text)]
                self._editor.set_match_selections(spans)

            def find_next(self) -> bool:
                return self._find(forward=True)

            def find_prev(self) -> bool:
                return self._find(forward=False)

            def _find(self, forward: bool) -> bool:
                pat = self._pattern()
                if pat is None:
                    return False
                text = self._editor.toPlainText()
                spans = [(m.start(), m.end()) for m in pat.finditer(text)]
                if not spans:
                    return False
                cursor = self._editor.textCursor()
                # QTextCursor positions are UTF-16 units; python match offsets
                # are code points. Compare in code points by converting the
                # cursor's UTF-16 positions is awkward, so instead work in code
                # points throughout and convert only when setting the cursor.
                sel_start_u16 = cursor.selectionStart()
                sel_end_u16 = cursor.selectionEnd()
                # Convert the cursor's UTF-16 anchor/end to code-point offsets.
                cur_start = len(
                    text.encode("utf-16-le")[: sel_start_u16 * 2].decode(
                        "utf-16-le", errors="ignore"
                    )
                )
                cur_end = len(
                    text.encode("utf-16-le")[: sel_end_u16 * 2].decode(
                        "utf-16-le", errors="ignore"
                    )
                )
                if forward:
                    nxt = next((s for s in spans if s[0] >= cur_end), None)
                    start, end = nxt if nxt is not None else spans[0]  # wrap
                else:
                    prev = [s for s in spans if s[1] <= cur_start]
                    start, end = prev[-1] if prev else spans[-1]  # wrap
                cur = self._editor.textCursor()
                cur.setPosition(_utf16_offset(text, start))
                cur.setPosition(
                    _utf16_offset(text, end), QTextCursor.MoveMode.KeepAnchor
                )
                self._editor.setTextCursor(cur)
                self._editor.centerCursor()
                self._rehighlight()
                return True

            # -- replace --
            def replace_one(self) -> None:
                if self._editor.isReadOnly():
                    return
                pat = self._pattern()
                cursor = self._editor.textCursor()
                selected = cursor.selectedText()
                if pat is not None and selected and pat.fullmatch(selected):
                    cursor.insertText(self.replace_field.text())
                self.find_next()

            def replace_all(self) -> int:
                if self._editor.isReadOnly():
                    return 0
                pat = self._pattern()
                if pat is None:
                    return 0
                original = self._editor.toPlainText()
                new_text, n = pat.subn(
                    lambda _m: self.replace_field.text(), original
                )
                if n:
                    cur = self._editor.textCursor()
                    cur.beginEditBlock()
                    cur.select(QTextCursor.SelectionType.Document)
                    cur.insertText(new_text)
                    cur.endEditBlock()
                self._rehighlight()
                return n
    ```
  - Wire into `EditorTab.__init__` (after the disk banner, before adding the
    editor to the layout) and add `open_find`:
    ```python
                # ... existing disk_banner setup ...
                self.editor = FileEditor(self.path, self)
                self.editor.load_from_disk()
                self.editor.disk_conflict.connect(self._show_disk_banner)
                self.editor.disk_reloaded.connect(self._hide_disk_banner)
                self.find_bar = EditorFindBar(self.editor, self)
                layout.addWidget(self.find_bar)
                layout.addWidget(self.editor, stretch=1)

            def open_find(self, prefill: str = "") -> None:
                self.find_bar.open(prefill)
    ```
  - Extend the existing `EditorTab.set_read_only` so toggling read-only while
    the find bar is open updates its replace controls (review #7):
    ```python
            def set_read_only(self, ro: bool) -> None:
                self.editor.set_read_only(ro)
                self.find_bar.apply_read_only(ro)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestEditorFindBar -q`
  - Expected: all pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + prior tasks + these, no failures. In particular the
    tranche-A `test_read_only_matrix_locks_editor_banner_and_tab` and current-line
    highlight behaviour still pass (the ExtraSelections refactor is invariant).

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): Ctrl+F editor find/replace bar with highlight + wrap"`

---

### Task 6: FilesView + MainWindow wiring — Ctrl+Shift+F, Ctrl+F, open-at-line, read-only push (Sonnet)

Wires the search dock into `FilesView`, the two shortcuts (with the offscreen
eventFilter bridge + emit-based wiring pins), `open_at`, the `dirty_open_paths`
provider, and the read-only replace-disable via `set_state`.

**Files**
- `spar/gui/files.py` (extend `FilesView`: search dock in a vertical splitter,
  Ctrl+Shift+F / Ctrl+F shortcut+bridge, `open_search`, `open_at`,
  `_dirty_open_paths`, replace-disable in `set_state`, split persistence)
- `spar/gui/app.py` (`MainWindow`: connect `FilesView.open_location` → switch +
  `open_at`; ensure `set_state` still pushed)
- `tests/test_gui_files.py` (append `TestFilesViewSearchWiring`)
- `tests/test_gui_app.py` (append one wiring test for the finder-style open path)

**Interfaces (exact signatures)**
```python
class FilesView(QWidget):
    open_location = Signal(str, int, int, int)   # re-emitted from SearchPanel
    def open_search(self) -> None: ...           # show dock, focus query
    def open_at(self, path, line: int, start: int, end: int) -> None: ...
    # closeEvent → stop_search() → super() (review #21): a standalone view
    # tears down the search thread itself
    # set_state(state) additionally toggles SearchPanel.set_replace_enabled
    # new shortcuts: _find_in_files_shortcut, _find_in_editor_shortcut
    # eventFilter also bridges Ctrl+Shift+F (find in files) and Ctrl+F (editor)
```

`open_at`: `self.open_file(path)`; take the current tab's editor; build a
`QTextCursor` at `line-1`, offset `start`, extend to `end`
(`KeepAnchor`), `setTextCursor` + `centerCursor` so the match is selected and
scrolled into view.

Shortcut+bridge (mirror `_save_shortcut`): `_find_in_files_shortcut =
QShortcut(QKeySequence("Ctrl+Shift+F"), self)` and `_find_in_editor_shortcut =
QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)`, both
`WidgetWithChildrenShortcut`, connected to `open_search` and
`_open_find_in_current_tab`. Extend the existing `eventFilter` (already installed
on each editor for Ctrl+S) to also match `Ctrl+Shift+F` (custom chord check) and
`StandardKey.Find`, routing to the same slots — this is the offscreen bridge.

**Steps**

- [ ] Append `TestFilesViewSearchWiring` to `tests/test_gui_files.py`:
    ```python
    class TestFilesViewSearchWiring:
        def _view(self, qtbot, tmp_path):
            from spar.gui.files import FilesView

            (tmp_path / "app.py").write_text("todo one\nplain\n", encoding="utf-8")
            (tmp_path / ".git").mkdir()
            (tmp_path / ".git" / "HEAD").write_text("ref\n")
            view = FilesView(tmp_path)
            qtbot.addWidget(view)
            return view

        def test_open_search_shows_dock_and_focuses(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            assert view.search_panel.isHidden() is True  # hidden by default
            view.open_search()
            assert view.search_panel.isHidden() is False

        def test_open_at_positions_cursor_and_selects_span(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_at(tmp_path / "app.py", 1, 0, 4)  # "todo"
            ed = view.tabs.currentWidget().editor
            assert ed.textCursor().selectedText() == "todo"

        def test_search_open_location_opens_tab_at_line(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_search()
            view.search_panel.query.setText("plain")
            view.search_panel._run_search()
            qtbot.waitUntil(
                lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
            )
            line_item = view.search_panel.results.topLevelItem(0).child(0)
            view.search_panel._on_item_activated(line_item, 0)
            assert view.tabs.currentWidget().path.name == "app.py"
            assert view.tabs.currentWidget().editor.textCursor().selectedText() == "plain"

        def test_read_only_disables_replace_keeps_search(self, qtbot, tmp_path):
            from spar.gui.runner import RunnerState

            view = self._view(qtbot, tmp_path)
            # Give the panel live results so the replace button is not disabled
            # purely on staleness grounds (review #5).
            view.open_search()
            view.search_panel.query.setText("todo")
            view.search_panel._run_search()
            qtbot.waitUntil(
                lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
            )
            view.set_state(RunnerState.RUNNING)
            assert view.search_panel.replace_button.isEnabled() is False
            assert view.search_panel.query.isEnabled() is True
            view.set_state(RunnerState.IDLE)
            assert view.search_panel.replace_button.isEnabled() is True

        def test_dirty_open_paths_reports_unsaved_tabs(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("dirty\n")
            ed.document().setModified(True)  # #9
            assert str(tmp_path / "app.py") in view.search_panel.dirty_open_paths()

        def test_find_in_files_shortcut_wired(self, qtbot, tmp_path):
            # emit-based pin (the Ctrl+S lesson): the QShortcut→open_search
            # connection must be exercised even though offscreen never routes
            # the real chord through the shortcut map.
            view = self._view(qtbot, tmp_path)
            view._find_in_files_shortcut.activated.emit()
            assert view.search_panel.isHidden() is False

        def test_ctrl_shift_f_real_chord_opens_search(self, qtbot, tmp_path):
            # real-chord half: deliver Ctrl+Shift+F to an editor; the
            # eventFilter bridge must open the dock offscreen.
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            view.show()
            ed = view.tabs.currentWidget().editor
            ed.setFocus()
            qtbot.keyClick(
                ed, Qt.Key.Key_F,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
            )
            assert view.search_panel.isHidden() is False

        def test_find_in_editor_shortcut_wired(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            view._find_in_editor_shortcut.activated.emit()
            assert view.tabs.currentWidget().find_bar.isHidden() is False

        def test_ctrl_f_real_chord_opens_editor_find_bar(self, qtbot, tmp_path):
            # review #7: the real-chord Ctrl+F half — the eventFilter bridge must
            # open the editor find bar offscreen (mirrors the Ctrl+S lesson).
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            view.show()
            ed = view.tabs.currentWidget().editor
            ed.setFocus()
            qtbot.keyClick(ed, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
            assert view.tabs.currentWidget().find_bar.isHidden() is False

        def test_replace_reloads_open_clean_tab(self, qtbot, tmp_path):
            # review #11: replace-in-files rewrites disk; the open CLEAN tab must
            # auto-reload via the watcher (real disk write, mirroring tranche A).
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")   # open + clean (not dirty)
            ed = view.tabs.currentWidget().editor
            view.open_search()
            view.search_panel.query.setText("todo")
            view.search_panel._run_search()
            qtbot.waitUntil(
                lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
            )
            view.search_panel.replace.setText("DONE")
            view.search_panel._apply_replace()
            # the watcher reload lands on a later event-loop turn
            qtbot.waitUntil(lambda: "DONE" in ed.toPlainText(), timeout=5000)
            assert "todo" not in ed.toPlainText()

        def test_close_standalone_view_stops_search_thread(self, qtbot, tmp_path):
            # review #21: closing a STANDALONE FilesView (no MainWindow) must
            # stop the SearchPanel's QThread via closeEvent → stop_search();
            # without it the started thread outlives the closed widget.
            view = self._view(qtbot, tmp_path)
            view.open_search()
            view.search_panel.query.setText("todo")
            view.search_panel._run_search()
            qtbot.waitUntil(
                lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
            )
            session = view.search_panel._session
            assert session._started is True   # the thread actually ran
            view.close()
            assert session._stopped is True   # closeEvent tore it down
            qtbot.waitUntil(lambda: not session._thread.isRunning(), timeout=5000)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestFilesViewSearchWiring -q`
  - Expected: **AttributeError** (`view.search_panel` not defined).

- [ ] Extend `FilesView`. Restructure the layout so the horizontal splitter and
  a `SearchPanel` share an outer vertical splitter; add the shortcuts, bridge,
  `open_search`, `open_at`, `_open_find_in_current_tab`, `_dirty_open_paths`, and
  the replace-disable in `set_state`:
    ```python
            # in __init__: after building self.splitter (the tree|editor one)
            # and BEFORE outer.addWidget(...), wrap it in a vertical splitter:
                self.search_panel = SearchPanel(self.project_dir, self)
                self.search_panel.dirty_open_paths = self._dirty_open_paths
                # review #9: FilesView itself performs the open (tab + cursor)
                # AND re-emits open_location so MainWindow can switch the centre
                # view. This makes a standalone FilesView work without MainWindow.
                self.search_panel.open_location.connect(self._open_at_location)
                self.search_panel.setVisible(False)

                self.vsplit = QSplitter(Qt.Orientation.Vertical, self)
                self.vsplit.setObjectName("filesVSplit")
                self.vsplit.addWidget(self.splitter)   # tree | editor
                self.vsplit.addWidget(self.search_panel)
                self.vsplit.setStretchFactor(0, 4)
                self.vsplit.setStretchFactor(1, 1)
                outer.addWidget(self.vsplit)            # replaces outer.addWidget(self.splitter)
                self._restore_search_split_state()
                self.vsplit.splitterMoved.connect(self._save_search_split_state)

            # shortcuts (mirror _save_shortcut), added after it:
                self._find_in_files_shortcut = QShortcut(
                    QKeySequence("Ctrl+Shift+F"), self
                )
                self._find_in_files_shortcut.setContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut
                )
                self._find_in_files_shortcut.activated.connect(self.open_search)
                self._find_in_editor_shortcut = QShortcut(
                    QKeySequence(QKeySequence.StandardKey.Find), self
                )
                self._find_in_editor_shortcut.setContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut
                )
                self._find_in_editor_shortcut.activated.connect(
                    self._open_find_in_current_tab
                )
    ```
  - Add the `open_location` signal to `FilesView` (class attribute):
    ```python
            open_location = Signal(str, int, int, int)
    ```
  - Extend the existing `eventFilter` to also bridge the two new chords (add the
    branches before the Ctrl+S branch's `return`):
    ```python
            def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
                if event.type() == QEvent.Type.KeyPress:
                    if event.matches(QKeySequence.StandardKey.Save):
                        self._save_current()
                        return True
                    if event.matches(QKeySequence.StandardKey.Find):
                        self._open_find_in_current_tab()
                        return True
                    if (
                        event.key() == Qt.Key.Key_F
                        and event.modifiers() == (
                            Qt.KeyboardModifier.ControlModifier
                            | Qt.KeyboardModifier.ShiftModifier
                        )
                    ):
                        self.open_search()
                        return True
                return super().eventFilter(obj, event)
    ```
  - Add the new methods:
    ```python
            def open_search(self) -> None:
                self.search_panel.setVisible(True)
                self.search_panel.focus_query()

            def _open_find_in_current_tab(self) -> None:
                tab = self.tabs.currentWidget()
                if tab is None:
                    return
                cursor = tab.editor.textCursor()
                tab.open_find(cursor.selectedText())

            def _open_at_location(self, rel: str, line: int, start: int, end: int) -> None:
                # review #9: open here, then re-emit so MainWindow switches view.
                self.open_at(self.project_dir / rel, line, start, end)
                self.open_location.emit(rel, line, start, end)

            def open_at(self, path, line: int, start: int, end: int) -> None:
                self.open_file(path)
                tab = self.tabs.currentWidget()
                if tab is None:
                    return
                ed = tab.editor
                block = ed.document().findBlockByNumber(max(0, line - 1))
                base = block.position()
                line_text = block.text()
                # review #8: start/end are code-point offsets within the line;
                # QTextCursor counts UTF-16 units. Convert so non-BMP lines
                # (e.g. an emoji before the match) select the right range.
                cur = QTextCursor(block)
                cur.setPosition(base + _utf16_offset(line_text, start))
                cur.setPosition(
                    base + _utf16_offset(line_text, end),
                    QTextCursor.MoveMode.KeepAnchor,
                )
                ed.setTextCursor(cur)
                ed.centerCursor()

            def stop_search(self) -> None:
                # review #3: idempotent teardown, called from closeEvent (below)
                # and MainWindow.closeEvent.
                self.search_panel.stop_session()

            def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
                # review #21: a STANDALONE FilesView (no MainWindow) must tear
                # down the SearchPanel's QThread when it closes — nothing else
                # routes through MainWindow.closeEvent in that case, so without
                # this the searched panel's thread would outlive the widget.
                self.stop_search()
                super().closeEvent(event)

            def _dirty_open_paths(self) -> set:
                return {
                    str(t.path) for t in self._tabs_by_path.values() if t.is_dirty()
                }

            def _restore_search_split_state(self) -> None:
                state = self._settings.value("files/search_split")
                if state is not None:
                    self.vsplit.restoreState(state)

            def _save_search_split_state(self, *_args) -> None:
                self._settings.setValue("files/search_split", self.vsplit.saveState())
    ```
  - In `set_state`, after the existing read-only push, gate replace:
    ```python
            def set_state(self, state) -> None:
                self._read_only = state in _READ_ONLY_STATES
                self.read_only_banner.setVisible(self._read_only)
                for tab in self._tabs_by_path.values():
                    tab.set_read_only(self._read_only)
                self._refresh_all_labels()
                self.search_panel.set_replace_enabled(not self._read_only)
    ```
  - Add `QTextCursor` import already added in Task 5; `QSplitter` and `QShortcut`
    already imported. Run:
    `.venv/bin/python -m pytest tests/test_gui_files.py::TestFilesViewSearchWiring -q`
  - Expected: all pass.

- [ ] Wire `MainWindow` (`spar/gui/app.py`): connect the FilesView search
  open-at path to the centre switch. After `self.files_view` is constructed and
  the finder wiring block, add:
    ```python
            # ADR 0006 tranche B: a find-in-files result opens the file in the
            # Pliki view at the match line (mirrors the double-Shift finder).
            self.files_view.open_location.connect(self._on_search_open_location)
    ```
  - Add the handler near `_on_finder_chosen`. Review #9: `FilesView` has ALREADY
    opened the tab and positioned the cursor (via `_open_at_location`); the
    handler only switches the centre view so it becomes visible:
    ```python
        def _on_search_open_location(self, rel: str, line: int, start: int, end: int) -> None:
            self._set_centre_view("files")
    ```
  - In `MainWindow.closeEvent`, tear down the search thread too (review #3),
    alongside the existing `self.chat_panel.stop_session()`:
    ```python
            self.chat_panel.stop_session()
            self.files_view.stop_search()   # review #3: idempotent search-thread stop
            super().closeEvent(event)
    ```
  - Append a wiring test to `tests/test_gui_app.py` (uses `_hermetic_qsettings`):
    ```python
        def test_search_open_location_switches_to_files_and_opens(self, qtbot, tmp_path):
            # review #9: drive the REAL wiring — FilesView opens the tab and
            # positions the cursor; MainWindow only switches the centre view.
            (tmp_path / "app.py").write_text("todo here\n", encoding="utf-8")
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.files_view.search_panel.open_location.emit("app.py", 1, 0, 4)
            assert window.centre_stack.currentIndex() == 1  # Pliki
            tab = window.files_view.tabs.currentWidget()
            assert tab.path.name == "app.py"
            assert tab.editor.textCursor().selectedText() == "todo"
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py tests/test_gui_files.py -q`
  - Expected: all pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + all prior tasks + these, no failures. Confirm the
    tranche-A `FilesView` tests (tree, tabs, Ctrl+S, read-only matrix) still pass
    unchanged after the layout wrap.

- [ ] Commit:
  - `git add spar/gui/files.py spar/gui/app.py tests/test_gui_files.py tests/test_gui_app.py`
  - `git commit -m "feat(gui): wire find-in-files + Ctrl+F shortcuts, open-at-line, read-only replace gate"`

---

### Task 7: QSS + docs — theme new widgets, README, HANDOFF, ADR stamp (Haiku)

Themes every new widget from `TOKENS` (colour-purity test green) and lands the
documentation in the same tranche.

**Files**
- `spar/gui/theme.py` (append QSS rules for the new object names)
- `README.md` (shortcuts + replace semantics)
- `docs/HANDOFF.md` (tranche B entry)
- `docs/adr/0006-files-module-editor-and-search.md` (stamp: tranche B implemented)
- `tests/test_gui_app.py` (extend `test_qss_styles_files_widgets` with the new
  object names; the colour-purity guard `test_build_qss_uses_only_token_colors`
  in the same file needs no change)

**Steps**

- [ ] Extend the widget-presence assertion in `tests/test_gui_app.py`
  (`test_qss_styles_files_widgets`) so the new object names must be themed:
    ```python
        assert "#searchPanel" in qss
        assert "#searchToggle" in qss
        assert "#searchResults" in qss
        assert "#editorFindBar" in qss
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py -k qss -q`
  - Expected: **fails** (the new object names are not in the QSS yet).

- [ ] Add QSS. Append to the returned string in `build_qss()` (every value a
  `TOKENS` lookup — the `test_build_qss_uses_only_token_colors` purity guard must
  stay green):
    ```python
        #searchPanel {{
            background-color: {t['panel']};
            border-top: 1px solid {t['line']};
        }}
        #searchQuery, #replaceField, #findField, #findReplaceField {{
            background-color: {t['panel-alt']};
            color: {t['text']};
            border: 1px solid {t['line']};
            border-radius: 4px;
            padding: 2px 6px;
        }}
        #searchQuery[invalid="true"] {{
            border: 1px solid {t['gate']};
        }}
        #searchToggle {{
            color: {t['text']};
            background-color: {t['panel']};
            border: 1px solid {t['line']};
            border-radius: 4px;
            padding: 2px 6px;
        }}
        #searchToggle:checked {{
            background-color: {t['panel-alt']};
            border: 1px solid {t['claude']};
        }}
        #searchResults {{
            background-color: {t['panel']};
            color: {t['text']};
            border: none;
        }}
        #searchStatus {{
            color: {t['muted']};
        }}
        #editorFindBar {{
            background-color: {t['panel-alt']};
            border-bottom: 1px solid {t['line']};
        }}
        #replaceButton {{
            color: {t['text']};
            background-color: {t['panel']};
            border: 1px solid {t['line']};
            border-radius: 4px;
            padding: 2px 8px;
        }}
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py -k "qss or token" -q`
  - Expected: the widget-presence test now passes and
    `test_build_qss_uses_only_token_colors` stays green (all hex values are
    TOKENS entries; the QSS adds only `TOKENS`-derived colours).

- [ ] Update `README.md`. After the "Double Shift" paragraph (around the
  `<!-- TODO: screenshot docs/img/gui-files.png ... -->` line), add:
    ```markdown
    **Find in files (Ctrl+Shift+F).** Opens a search dock at the bottom of the
    Pliki view: type a query, toggle **Aa** (case), **.*** (regex) or **W**
    (whole word); results group as file → matching lines with a per-file count,
    and clicking a line opens the file at that match. Search runs off the UI
    thread and a new query cancels the previous one (ripgrep, when on PATH,
    accelerates only case-sensitive literal non-whole-word searches; every
    other search — case-insensitive, whole-word or regex — uses the built-in
    Python scan). **Replace in files:** fill the *Zamień na…* field, keep the
    files you want checked (all checked by default) and press **Zamień
    zaznaczone**. A checked file is skipped and reported (`pominięto N`) when
    it has unsaved edits in an open tab (niezapisane zmiany), changed on disk
    since the search (plik zmienił się), is not valid UTF-8 (nie-UTF-8), is a
    symlink pointing outside the project (dowiązanie poza projektem), or its
    write fails (błąd zapisu); every other checked file is rewritten on disk
    and any open clean tab auto-reloads. Replace is disabled while a run is
    live (read-only matrix); search stays available.

    **Find in the editor (Ctrl+F).** Opens a find/replace bar in the current
    tab, prefilled with the selection: **F3 / Shift+F3** jump to the next/previous
    match (wrapping around), all matches are highlighted, and **Zamień** /
    **Zamień wszystko** replace (disabled while the editor is read-only). **Esc**
    closes the bar.
    ```
  - No test; verified by the docs review in self-review.

- [ ] Add a `docs/HANDOFF.md` entry at the top (after the tranche-A section),
  summarising tranche B: the pure search/replace engine + ripgrep parity, the
  cancellable `SearchSession`/`_SearchWorker`, the `SearchPanel` dock
  (Ctrl+Shift+F), replace safety (skip dirty tabs, read-only gate), the
  `EditorFindBar` (Ctrl+F, F3/Shift+F3, wrap, highlight-all), the new QSS object
  names, and the new QSettings key `files/search_split`. Note the test baseline
  moved with "baseline + tranche-B tests, no regressions" and that the git module
  remains the last pending left-rail tranche. Reference this plan path.

- [ ] Update the ADR stamp in
  `docs/adr/0006-files-module-editor-and-search.md`: change the tranche line
  (lines 12–13) to record tranche B as implemented, e.g.:
    ```markdown
    Tranche A implemented 2026-07-11 (view switch, Pygments editor,
    save/dirty, read-only matrix + auto-reload, double-Shift finder). Tranche B
    implemented 2026-07-11 (find-in-files Ctrl+Shift+F with ripgrep-accelerated
    cancellable search, replace-in-files honouring the read-only matrix and
    unsaved-tab safety, Ctrl+F in the editor). The git module remains the last
    pending left-rail tranche.
    ```
  - Leave the screenshot TODO comment in README as-is (`docs/img/gui-files.png`).

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: baseline + all tranche-B tests, no failures, colour-purity green.

- [ ] Commit:
  - `git add spar/gui/theme.py tests/test_gui_app.py README.md docs/HANDOFF.md docs/adr/0006-files-module-editor-and-search.md`
  - `git commit -m "docs(gui): theme find/replace widgets + README/HANDOFF/ADR tranche B stamp"`

---

## Self-review

- **Tranche-B coverage vs ADR 0006 item 5 + the brief scope:**
  1. Search engine (pure), literal/regex/case/whole-word, spans, binary
     (NUL-in-8KB) + size (>2 MB) guards (`passes_search_guards`),
     `errors="replace"` — Task 1. Optional ripgrep accelerator with identical
     shape + parity `skipif`, gated by `is_rg_compatible` and prefiltered by
     the shared guards (#19) — Task 2. ✅
  2. Search panel opened by Ctrl+Shift+F, toggles, results tree (file→lines +
     count badges), click/Enter opens at line, QThread worker with
     generation-token cancellation, status "N wyników w M plikach"/"szukam…" —
     Tasks 3 + 6. ✅
  3. Replace-in-files: replace field, per-file checkboxes (default checked),
     "Zamień zaznaczone", skip files with unsaved edits (`pominięto N`), disk
     write + watcher reload (through-symlink writes resolve to the target,
     out-of-project links skipped — #20), replace disabled under the read-only
     matrix, re-run after replace, regex reuses the same compiled pattern —
     Task 4. ✅
  4. Ctrl+F editor bar: find + replace, case toggle, next/prev, F3/Shift+F3,
     wrap, highlight-all (ExtraSelections, TOKENS colour, merged with current
     line), replace respects read-only, Esc closes, prefill from selection —
     Tasks 5 + 6. ✅
  5. QSS via TOKENS only, colour-purity green — Task 7. ✅
  6. README + HANDOFF + ADR stamp + screenshot TODO — Task 7. ✅
- **Placeholder scan:** no `TODO`/`...`/`pass`-stub left in shipped code; the
  only TODO is the pre-existing README screenshot comment (intentional).
- **Name/type consistency with existing files.py APIs:** reuses
  `build_file_index`, `_FINDER_SKIP_DIRS`, `TOKENS`, `RunnerState`,
  `_READ_ONLY_STATES`, the `EditorTab`/`FileEditor`/`FilesView` structure, the
  `_save_shortcut` + `eventFilter` bridge pattern, and the
  `ConversationSession` thread/generation lifecycle (persistent `QThread`,
  `_dispatch` queued signal, `_ABANDONED_THREADS`-style release). New symbols
  (`SearchSpec`, `SearchMatch`, `SearchSession`, `SearchPanel`, `EditorFindBar`)
  do not collide with existing names. The `set_state` extension and the layout
  wrap preserve every tranche-A attribute (`tree`, `tabs`, `splitter`,
  `read_only_banner`, `_save_shortcut`).
- **Ambiguities resolved (see the plan sections):** search-dock placement
  (bottom vertical-splitter strip), file-level replace checkboxes with
  whole-file substitution, ripgrep flag set matching `build_file_index`, and
  byte→char span remapping for rg parity.

## Review history

Round 1 (codex gpt-5.6-sol): verdict CONTINUE; accepted #1–#12.
- #1 rg argv test now asserts `argv[-4:] == ["-e", "todo", "--", "/proj"]`.
- #2 `_live_generation` is facade-owned/monotonic (worker only reads); empty/invalid queries call `SearchSession.cancel()` to supersede in-flight runs.
- #3 lazy thread start + idempotent `stop()`; `SearchPanel.stop_session`/`closeEvent`, `FilesView.stop_search`, and `MainWindow.closeEvent` all tear it down.
- #4 ripgrep only for literal specs; python for all regex; fallback on spawn failure / non-clean exit / `_RipgrepParseError` (non-UTF-8 "bytes" member); parity tests reworked (unicode, invalid-UTF-8, exclusion, regex-always-python).
- #5 completed search's spec stored with results; replace uses the stored spec; drift disables the button with "wyniki nieaktualne — uruchom szukanie ponownie".
- #6 read bytes + strict-UTF-8 decode (skip nie-UTF-8), mtime+size fingerprint check (skip plik zmienił się), atomic byte-preserving `os.replace` write, per-file try/except; tests per skip class.
- #7 F3/Shift+F3 implemented (QShortcut + keyPressEvent), real-chord Ctrl+F test added, `set_read_only` refreshes an open bar's replace controls.
- #8 code-point→UTF-16 offset helper for cursor positions (😀 test); editor-bar search uses `re` IGNORECASE not `.lower()` (İ test).
- #9 `FilesView._open_at_location` opens the tab + cursor and re-emits; `MainWindow` only switches the centre view; test/wiring adjusted.
- #10 ExtraSelections order is current-line first, matches after; test asserts formats.
- #11 rows built while read-only are non-checkable; real watcher reload test added.
- #12 Task 7 commit adds `tests/test_gui_app.py` to the git add list.

Round 2 (codex gpt-5.6-sol): verdict CONTINUE; accepted #13–#17.
- #13 rg gets the EXPLICIT file list from `build_file_index` (`rg -e pat -- f1 f2 …`, batched by `_RIPGREP_BATCH`) so both engines share one file set; symlink parity test added.
- #14 rg runs under `subprocess.Popen` with a poll loop on `_live_generation` (kill on supersede); `SearchSession.stop()` now also bumps `worker._live_generation`; slow-fake + fake-Popen kill tests added.
- #15 replace stays disabled while searching: `_run_search` stashes `_pending_spec` and sets `_results_spec=None` at dispatch; promotion happens ONLY in `_on_finished`; in-flight-disabled test added.
- #16 replace summary is sticky: stashed in `_replace_summary`, and the refresh's `_on_finished` appends counts instead of overwriting; skip-warning-survives-refresh test added.
- #17 `_atomic_write_bytes` chmods the temp file to the original mode before `os.replace`; 0o755-script mode-preserved regression test added.

Round 3 (codex gpt-5.6-sol): verdict CONTINUE; accepted #18–#20.
- #18 invalid-UTF-8 parity test now calls `build_ripgrep_argv(tmp_path, spec, build_file_index(tmp_path))` — the required *files* arg was missing.
- #19 rg gated by `is_rg_compatible(spec)` (case-SENSITIVE, non-whole-word, literal only; `-i`/`-w`/regex → python) with predicate tests, and the file list is prefiltered by the shared `passes_search_guards` (extracted from `search_file`) so rg never sees files python's size/binary guards skip; binary-prefilter + case-insensitive-python-path tests added.
- #20 replace resolves the symlink target first and runs temp+`os.replace` on the RESOLVED path (mode preserved per #17); a target escaping the project root is skipped as "pominięto N (dowiązanie poza projektem)"; through-symlink (link survives, target rewritten) and escape-skip regression tests added.

Round 4 (codex gpt-5.6-sol): verdict CONTINUE; accepted #21–#23.
- #21 `FilesView.closeEvent` now calls `stop_search()` (then super), so a standalone view tears down the SearchPanel's QThread; close-a-searched-standalone-view regression test added.
- #22 `_ripgrep_grouped` drains stdout INCREMENTALLY (line-by-line with generation checks between lines; stderr on a daemon side-thread) instead of waiting for exit before `communicate` — no deadlock when the --json output exceeds the pipe capacity; >64 KB real-rg test + mid-stream-kill test (reworked FakePopen) added.
- #23 the `(mtime_ns, size)` fingerprint is captured INSIDE the scan (`_stat_fingerprint`: python path stats BEFORE reading each file; rg path stats at a file's first parsed match row) and travels with the batch payload `(matches, fingerprint)`; `_file_item` only stores it (grouped result shape now `(rel, fingerprint, bucket)`); payload-fingerprint session test + modified-between-scan-and-row replace-skip test added.

Round 5 (codex gpt-5.6-sol): verdict CONTINUE; accepted #24–#27.
- #24 rg stdout now drained by a daemon reader thread into a `queue.Queue`; the worker pulls with `queue.get(timeout=0.05)` and checks `_live_generation` on every timeout tick, so a silent (no-match) batch is killed on supersede too; mid-stream-kill test reworked to a no-output-yet scenario.
- #25 rg fingerprints come from a pre-launch snapshot (`dict` rel → fingerprint statted for the WHOLE batch BEFORE Popen), never a stat after rg read the file; snapshot-before-launch test added (fake Popen mutates the file mid-stream → payload keeps the pre-launch fingerprint → replace refuses).
- #26 real-key F3/Shift+F3 test added (shown widget, focused `find_field` child, `qtbot.keyClick`) alongside the emit-pin — both halves covered.
- #27 Task 7 README text corrected: rg accelerates only case-sensitive literal non-whole-word searches (python otherwise), and the five replace skip classes are enumerated (niezapisane zmiany, plik zmienił się, nie-UTF-8, dowiązanie poza projektem, błąd zapisu).

Round 6 (codex gpt-5.6-sol): verdict CONTINUE; accepted #28–#31.
- #28 the invalid-regex branch of `_run_search` now mirrors the empty-query path (results tree cleared, `_pending_spec`/`_results_spec` reset to None, replace disabled via `_update_replace_state`); clears-partials-and-resets-specs test added.
- #29 dirty-tab protection compares RESOLVED targets on both sides (dirty set resolved once, row checked as resolved `real_path`), so a symlink alias of a dirty tab is skipped as niezapisane zmiany; alias regression test added.
- #30 `abs_path.resolve()` moved INSIDE the per-row try with `except (OSError, RuntimeError)` → błąd zapisu, so a symlink loop skips one row instead of aborting the whole batch; loop-row regression test added.
- #31 `_stat_fingerprint` docstring corrected to the #25 reality: the rg path snapshots the whole batch BEFORE launching rg (pre-launch snapshot), not at a file's first parsed match.

Round 7 (codex gpt-5.6-sol): verdict CONTINUE; accepted #32–#34.
- #32 `_atomic_write_bytes` uses `tempfile.mkstemp(dir=parent, prefix=name+".", suffix=".spar-tmp")` (unique, O_EXCL) with `os.fchmod` + fd write instead of the predictable `<file>.spar-tmp` sibling that could clobber a legitimate file; pre-existing-`*.spar-tmp`-survives collision regression test added.
- #33 `_SEARCH_MAX_RESULTS` is enforced DURING the rg parse (count as parsed; at the cap kill rg, skip remaining batches, return collected groups) instead of accumulating everything for `_emit_grouped` to cap (unbounded memory); both paths now return/emit a `truncated` flag surfaced as "wyniki obcięte do N"; fake-Popen >cap streaming test added (parse stops at cap, kill called).
- #34 rg batches built by `_rg_batches`: estimated argv BYTE budget (`_RIPGREP_ARGV_BUDGET` = 128 KB of `os.fsencode`d paths) as the primary bound, `_RIPGREP_BATCH` file count secondary — a fixed count alone couldn't guarantee ARG_MAX; residual E2BIG surfaces as `OSError` from Popen → existing python fallback; byte-budget split test added.

Round 8 (codex gpt-5.6-sol): verdict CONTINUE; accepted #35–#36.
- #35 python scan slices each file's matches to the remaining allowance BEFORE emitting (the old shape emitted the complete list, then checked the cap — one huge file blew past `_SEARCH_MAX_RESULTS`); sliced/exhausted sets `truncated` and breaks; single->cap-file regression test added (exactly cap results, truncated True).

Round 9 (codex gpt-5.6-sol): verdict CONTINUE; accepted #37, one line.

- #37 `search_file`/`search_text` gain `limit: int | None = None` — the scan stops and returns as soon as `len(matches) == limit` instead of materializing every match in a file (a ≤2 MB file can hold millions of matches) and slicing after; `run_turn` passes the remaining allowance as `limit=` (the existing slice stays as belt-and-braces for over-returning injected fakes; caller infers truncation via `len == limit`); pure test added (many-match file, limit=10 → exactly 10) and the worker cap test now spies that `scan_file` received the remaining allowance; injected test fakes updated to the `limit`-bearing signature.
- #36 `_rg_batches` now takes *root* and budgets `len(os.fsencode(str(root / rel))) + 1` — the absolute strings `build_ripgrep_argv` actually passes — instead of the bare relatives whose missing prefix could silently violate the 128 KB bound; byte-budget split test measures the absolute forms.

Round 10 (codex gpt-5.6-sol): verdict CONTINUE; accepted #38–#39, one line each.

- #38 `replace_in_text` no longer uses a bare `pattern.subn` (which would also replace at zero-width positions `search_text` skips and never displays, e.g. `a*`): it iterates `pattern.finditer`, skips `m.start() == m.end()`, and splices manually (regex mode via `m.expand(replacement)` for backrefs, literal verbatim), counting only non-zero matches; `a*`-on-`"baab"` regression test added (only the non-empty match replaced, count agrees with search).
- #39 `search_text` returns `[]` immediately for `limit is not None and limit <= 0` — the check-after-append shape treated `limit=0` as unbounded; limit=0/-1 pure test added.

Round 11 (codex gpt-5.6-sol): verdict CONTINUE; accepted #40, one line.

- #40 `replace_in_text` now processes line-by-line with the same `split("\n")` semantics as `search_text` (finditer per line, skip zero-length, splice, rejoin with `"\n"`; count = per-line non-zero replacements) — the previous whole-file `finditer` let a regex like `foo\s+bar` replace across `foo\nbar` though the per-line search never displayed that match; regression test added (`foo\s+bar` on `"foo\nbar"` → text unchanged, count 0, agrees with `search_text`; same-line case still replaced).

Round 12 (codex gpt-5.6-sol): verdict CONTINUE; accepted #41–#42, one line each.

- #41 the fixed rg argv gains `--text` (before `-e`) — `passes_search_guards` only checks the first 8KB for NUL, so a file whose first NUL sits after the window is scanned whole by python but truncated at the NUL by rg without `--text`; argv assertions updated and a parity test added (NUL after 8KB + a match after it → both engines find it).
- #42 `SearchPanel.__init__` (Task 4) now ends with `_update_replace_state()` — QPushButton defaults to enabled, so a fresh panel (`_results_spec is None`) left replace clickable before any search; fresh-panel regression test added (button disabled, `_apply_replace` a no-op that touches no file; read-only rows-uncheckable already covered).
- Round 13 (codex gpt-5.6-sol): verdict CONTINUE; accepted #43 — `--text` added to build_ripgrep_argv's actual argv list (was present only in prose/tests).
