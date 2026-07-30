"""Tests for spar/gui/sticky.py: follow-the-stream scrolling."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QTextBrowser

from spar.gui.sticky import FOLLOW_SLACK_PX, StickyBottom


def _long_html(n: int, tag: str = "linia") -> str:
    return "".join(f"<p>{tag} {i}</p>" for i in range(n))


def _view(qtbot):
    view = QTextBrowser()
    qtbot.addWidget(view)
    view.resize(300, 120)
    view.show()
    return view


class TestFollowing:
    def test_follows_by_default(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)

        sticky.set_html(_long_html(300))
        qtbot.wait(20)  # let the queued post-layout pin run

        bar = view.verticalScrollBar()
        assert sticky.following is True
        assert bar.value() == bar.maximum()

    def test_keeps_following_across_many_renders(self, qtbot):
        # The regression: each render restored a position derived from a STALE
        # (pre-layout) maximum, drifted off the end, and latched follow OFF --
        # the transcript then had to be dragged down by hand.
        view = _view(qtbot)
        sticky = StickyBottom(view)

        for n in range(20, 400, 40):
            sticky.set_html(_long_html(n))
            qtbot.wait(10)

        bar = view.verticalScrollBar()
        assert sticky.following is True
        assert bar.value() == bar.maximum()

    def test_growing_content_stays_pinned_to_the_new_end(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(200))
        qtbot.wait(10)
        first_max = view.verticalScrollBar().maximum()

        sticky.set_html(_long_html(400))
        qtbot.wait(10)

        bar = view.verticalScrollBar()
        assert bar.maximum() > first_max  # document really did grow
        assert bar.value() == bar.maximum()


class TestUserScroll:
    def test_scrolling_up_stops_following_and_keeps_the_position(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(400))
        qtbot.wait(10)

        bar = view.verticalScrollBar()
        parked = bar.maximum() // 3
        bar.setValue(parked)  # a user drag
        assert sticky.following is False

        sticky.set_html(_long_html(400, "linia"))
        qtbot.wait(10)

        assert sticky.following is False
        assert bar.value() == parked

    def test_scrolling_back_to_the_end_re_arms_following(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(400))
        qtbot.wait(10)
        bar = view.verticalScrollBar()

        bar.setValue(bar.maximum() // 3)
        assert sticky.following is False

        bar.setValue(bar.maximum())  # dragged back down
        assert sticky.following is True

    def test_slack_tolerates_being_a_few_pixels_off_the_end(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(400))
        qtbot.wait(10)
        bar = view.verticalScrollBar()

        bar.setValue(bar.maximum() - FOLLOW_SLACK_PX)

        assert sticky.following is True

    def test_programmatic_scrolling_does_not_count_as_a_user_scroll(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(400))
        qtbot.wait(10)
        bar = view.verticalScrollBar()
        bar.setValue(0)
        assert sticky.following is False

        # A render while parked moves the bar internally; that must not be
        # mistaken for the user coming back to the bottom.
        sticky.set_html(_long_html(500))
        qtbot.wait(10)

        assert sticky.following is False


class TestScrollToBottom:
    def test_explicit_jump_pins_and_re_arms(self, qtbot):
        view = _view(qtbot)
        sticky = StickyBottom(view)
        sticky.set_html(_long_html(400))
        qtbot.wait(10)
        bar = view.verticalScrollBar()
        bar.setValue(0)
        assert sticky.following is False

        sticky.scroll_to_bottom()

        assert sticky.following is True
        assert bar.value() == bar.maximum()
