"""``StickyBottom``: follow-the-stream scrolling for a rebuilt-HTML transcript.

Both chat transcripts (grill dialog, orchestrator panel) re-render by calling
``setHtml`` on the whole conversation for every streamed chunk, which resets the
scroll position -- so each of them has to decide "was the user at the bottom?"
and restore accordingly.

Doing that from the scrollbar's ``maximum()`` right after ``setHtml`` does not
work: Qt lays the document out lazily, so immediately after ``setHtml`` the
maximum still reflects the OLD (shorter) document. ``setValue(maximum)`` then
lands short of the real end, and the NEXT render reads that position, finds it
far above the by-then-correct maximum, concludes the user scrolled away, and
latches follow OFF for good -- the live symptom being a transcript that stops
following and has to be dragged down by hand.

So this helper does not derive intent from the position after a render:

* ``_following`` is intent, flipped only by a REAL user scroll (programmatic
  moves are bracketed and ignored), and re-armed when they come back to the end;
* following scrolls with ``moveCursor(End) + ensureCursorVisible()``, which
  forces the layout it needs instead of trusting a stale maximum, and repeats
  once through the event loop so a late layout pass cannot leave it short.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor

__all__ = ["FOLLOW_SLACK_PX", "StickyBottom"]

# How far off the bottom still counts as "following". Chat bubbles are taller
# than log lines, so the stream pane's 2px slack is too tight here.
FOLLOW_SLACK_PX = 8


class StickyBottom:
    """Keeps a ``QTextBrowser``-ish view pinned to the end while streaming."""

    def __init__(self, view, slack_px: int = FOLLOW_SLACK_PX) -> None:
        self._view = view
        self._slack = slack_px
        self._following = True
        self._programmatic = False
        view.verticalScrollBar().valueChanged.connect(self._on_value_changed)

    # -- state ---------------------------------------------------------
    @property
    def following(self) -> bool:
        return self._following

    def _on_value_changed(self, value: int) -> None:
        if self._programmatic:
            return
        bar = self._view.verticalScrollBar()
        # A user drag both DROPS follow (scrolled up) and RE-ARMS it (came back
        # to the end), so returning to the bottom resumes streaming without
        # having to touch a button.
        self._following = value >= bar.maximum() - self._slack

    # -- rendering -----------------------------------------------------
    def set_html(self, html: str) -> None:
        """Replace the view's html, keeping the reader where they belong."""
        bar = self._view.verticalScrollBar()
        previous = bar.value()
        self._programmatic = True
        try:
            self._view.setHtml(html)
        finally:
            self._programmatic = False
        if self._following:
            self.scroll_to_bottom()
            # ...and again once Qt has finished laying the new document out.
            QTimer.singleShot(0, self.scroll_to_bottom)
        else:
            self._programmatic = True
            try:
                bar.setValue(min(previous, bar.maximum()))
            finally:
                self._programmatic = False

    def scroll_to_bottom(self) -> None:
        """Pin to the end (and re-arm following)."""
        self._programmatic = True
        try:
            self._view.moveCursor(QTextCursor.MoveOperation.End)
            self._view.ensureCursorVisible()
            bar = self._view.verticalScrollBar()
            bar.setValue(bar.maximum())
        finally:
            self._programmatic = False
        self._following = True
