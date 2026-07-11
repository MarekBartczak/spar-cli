# 0006. Files module: left-rail view switching, Pygments editor, WebStorm-style search

Date: 2026-07-11

## Status

Accepted. Extends [0005](0005-conversation-modules-and-tool-window-rails.md)
(rails and the read-only-advisor boundary stay in force; this ADR settles the
left rail's first real module and the centre-area model).

Tranche A implemented 2026-07-11 (view switch, Pygments editor,
save/dirty, read-only matrix + auto-reload, double-Shift finder). Tranche B
(find/replace in files, Ctrl+F) and the git module remain pending.

## Context

ADR 0005 reserved the left rail for a future "Pliki" module: a way for the
developer to inspect and hand-fix code without leaving spar. Live use of the
orchestrator chat confirmed the need ("zerknąć w kod i coś ręcznie poprawić").
The user refined the interaction against the earlier mockups: no pinned
Strumień tab — the LEFT RAIL switches the whole centre view, and search must
work like WebStorm's (double Shift for files, Ctrl+Shift+F in files).

## Decision

1. **Centre switching.** The left rail carries two exclusive (radio) toggles:
   **Strumień** and **Pliki**. Starting or resuming a run auto-switches the
   centre to Strumień; a pending gate does NOT switch the centre (the right
   rail's attention dot + GatePanel carry that signal). Active view persists
   in QSettings.
2. **Pliki view** = project tree (left) + tabbed editor (right). Tree shows
   the project root, hides `.git/`, shows `.spar/` collapsed. Multiple files
   open as tabs with a dirty marker; Ctrl+S saves; switching views or closing
   with unsaved changes prompts save/discard. Uncommitted edits are caught by
   the existing dirty-tree pre-exec preflight.
3. **Editor stack: QPlainTextEdit + Pygments** (new dependency, BSD, pure
   Python). Pygments picks the lexer from the filename — arbitrary extensions
   highlight correctly, unknown formats degrade to plain text. Line numbers
   and current-line highlight included. **Hard boundary: no LSP, no
   completion, no refactoring, no autoformat** — the editor is a magnifier
   and a screwdriver, not an IDE. QScintilla rejected (GPL/PyQt-only);
   embedded web editors rejected (QtWebEngine dependency).
4. **Edit/view matrix** (same RunnerState signal as the toolbar):
   RUNNING / GATE_PENDING / LOCKED → read-only (banner + lock on the tab);
   IDLE / DONE / RESUMABLE / ERROR → editable. While read-only, an open file
   changed on disk by the engine auto-reloads if locally unmodified;
   locally-modified files show a "changed on disk" banner instead.
5. **Search, WebStorm-convention shortcuts:**
   - **double Shift** → fuzzy file finder (overlay, in-memory name index);
   - **Ctrl+Shift+F** → find in files (QThread scan; ripgrep used
     opportunistically when on PATH), results tree file→lines, click opens
     at line; case/regex/whole-word toggles;
   - **replace in files** in the same panel: checkbox-selected results +
     "Zamień zaznaczone"; replace is disabled while the matrix is read-only
     (search still works);
   - **Ctrl+F** → find/replace within the open editor.
6. **Delivery in two tranches:** A — tree, editor, save/dirty, matrix,
   auto-reload, double-Shift finder; B — find/replace in files + Ctrl+F.
   The git module (commit/push) remains a later left-rail tranche.

## Consequences

- GUI-only feature; no engine/protocol changes. `pygments` joins the GUI
  dependency set.
- The centre becomes a switched stack (Strumień | Pliki); the stream pane
  itself is unchanged.
- Search/replace honours the read-only matrix, so the engine's worktrees and
  merges cannot race a hand edit mid-run.
