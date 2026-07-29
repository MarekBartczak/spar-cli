# 0007. One window per project: a GUI process per project directory

Date: 2026-07-28

## Status

Accepted, implemented 2026-07-28 (plan
`docs/superpowers/plans/2026-07-28-multi-project-gui.md`, 5 challenge rounds,
9 MUST findings). Extends [0004](0004-gui-dashboard-pilot-with-grill-module.md)
and [0006](0006-files-module-editor-and-search.md): the rails, the
read-only-advisor boundary, and the Pliki module all stay in force — this ADR
settles how the GUI behaves when the user works on SEVERAL projects at once.

## Context

Real use is 2-3 projects in parallel (a run takes tens of minutes, so the user
starts a second one while the first grinds), and nothing should stop them from
running 20. The engine was already per-project: the GUI spawns `spar` as a
subprocess scoped to one `project_dir`, and the engine holds a per-project
`fcntl.flock` on `<project>/.spar/lock`. What was missing was everything
around it — layout state was global, the only way to open another project was
another terminal, and a second launch on the SAME project opened a read-only
`LOCKED` twin.

## Decision

1. **One window = one project = one OS process.** No instance cap, no
   counter, no warning dialog: `spar gui --dir A` and `spar gui --dir B` are
   independent processes with independent engines and locks.
2. **Tabs/single-process rejected.** N engines and N `live.log` tailers on one
   Qt event loop means a hung adapter freezes every project, and every piece
   of window state (`RunnerState`, gate, stream, files tree) is a
   singleton-per-window that would have to be rewritten as a per-project map.
   Note IntelliJ/WebStorm hosts multiple project frames in ONE JVM — we copy
   its UX (separate windows, "open in new window"), not its implementation.
3. **Layout state is scoped per project** under
   `projects/<sha1(realpath)[:12]>/…` in the existing `QSettings("spar",
   "gui")` store: `mainSplitter/state`, `rails/right_split`,
   `rails/centre_view`, `rails/tasks_visible`, `rails/chat_visible`,
   `files/tree_split`, `window/geometry`. Deliberately global (user habits,
   not project layout): `files/mask_history`,
   `files/search_dialog_geometry`, `recent_projects`. Pre-existing global
   layout keys are NOT migrated — the first launch per project starts from
   defaults, because a migration would have to guess which project the old
   global layout belonged to.
4. **A second launch on the same directory raises the running window** and
   exits 0. Each window owns a `QLocalServer` named `spar-gui-<project key>`;
   a newcomer that cannot claim the name asks the owner to raise itself.
   `try_claim()` owns that delivery: `False` means "an owner exists and has
   already been asked", so the caller must not send a second request.
   A raise arriving while `MainWindow` is still being constructed (FilesView
   pumps the event loop) is buffered and replayed by `attach()`.
   Two API traps are load-bearing here, both verified against PySide6:
   `QLocalSocket.waitForBytesWritten()` returns False after a successful
   `flush()` even though the peer got the bytes (so delivery is proven by
   `bytesToWrite() == 0`, never by that call), and
   `QLocalServer.removeServer()` succeeds against a LIVE listener — which is
   why the stale-socket cleanup only runs after a probe went unanswered, and
   why `release()` is a no-op for a guard that never claimed.
5. **`RunnerState.LOCKED` is unchanged.** It answers a foreign *engine*
   holding `.spar/lock` — e.g. a headless CLI run — where there is no window
   to raise. Single-instance and the engine lock are separate mechanisms.
6. **New windows are spawned detached** via `QProcess.startDetached`:
   `python -m spar.cli gui --dir X` normally, and
   `open -n -a Spar.app --args --dir X` for the frozen macOS bundle (without
   `-n`, `open` just activates the running bundle instead of starting a
   second instance).

## Consequences

- No cross-project aggregate view. At 2-3 windows there is nothing to
  aggregate; if it ever matters, a read-only "hub" listing projects with
  their `spar status --json` state is a possible later addition, deliberately
  out of scope here.
- A crashed owner leaves its socket name behind; the next launch probes it,
  gets no answer, removes it and claims. The residual race is a live-but-hung
  owner that cannot answer within the 1 s probe: it loses its name and the
  user gets two windows. The alternative — refusing to launch — is worse.
- Each window keeps its own geometry and splitter layout, and the window
  title carries the parent path so two same-named repos are distinguishable.
- The `Projekt` toolbar menu is the in-app entry point: directory picker plus
  the recent-projects list, both opening NEW windows, never swapping the
  project under an existing one.
