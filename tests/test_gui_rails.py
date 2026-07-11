from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from spar.gui.rails import IconRail, RailButtonSpec


class TestIconRail:
    def test_builds_named_buttons_with_state(self, qtbot):
        rail = IconRail([
            RailButtonSpec("tasks", "Taski", "tip", icon="☰"),
            RailButtonSpec("files", "Pliki", "tip", icon="🗀", enabled=False),
        ])
        qtbot.addWidget(rail)
        assert rail.buttons["tasks"].isEnabled() is True
        assert rail.buttons["files"].isEnabled() is False
        assert rail.buttons["tasks"].isCheckable() is True

    def test_button_face_is_the_glyph_not_the_label(self, qtbot):
        # Review #20: it must be an ICON rail — the face shows the glyph, the
        # human name lives only in the tooltip; the face is a fixed square.
        rail = IconRail([RailButtonSpec("tasks", "Taski", "Panel zadań", icon="☰")])
        qtbot.addWidget(rail)
        btn = rail.buttons["tasks"]
        assert btn.text() == "☰"
        assert "Taski" not in btn.text()
        assert "Taski" in btn.toolTip()
        assert btn.width() == btn.height()  # fixed square face

    def test_toggle_emits_key_and_state(self, qtbot):
        rail = IconRail([RailButtonSpec("chat", "Czat", "tip")])
        qtbot.addWidget(rail)
        seen = []
        rail.toggled.connect(lambda k, s: seen.append((k, s)))
        rail.buttons["chat"].setChecked(True)
        assert seen == [("chat", True)]

    def test_non_checkable_button_emits_clicked(self, qtbot):
        rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", checkable=False)])
        qtbot.addWidget(rail)
        seen = []
        rail.clicked.connect(seen.append)
        rail.buttons["gate"].click()
        assert seen == ["gate"]

    def test_attention_draws_dot_overlay(self, qtbot):
        # Review #21: the attention flag must actually PAINT a yellow dot (an
        # overlay drawn in paintEvent), not merely recolor the border via QSS.
        # Verify the property AND that the paintEvent path is exercised: the
        # rendered pixels change and a yellow dot appears top-right.
        rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", icon="⚠",
                                        checkable=False)])
        qtbot.addWidget(rail)
        btn = rail.buttons["gate"]
        before = btn.grab().toImage()
        rail.set_attention("gate", True)
        assert btn.property("attention") is True
        after = btn.grab().toImage()
        assert after != before  # paintEvent overlay actually changed the pixels
        # Yellow dot in the top-right corner region (see paintEvent geometry).
        dot = after.pixelColor(btn.width() - 3 - 4, 3 + 4)
        assert dot.red() > 150 and dot.green() > 120 and dot.blue() < 90
        # Clearing the flag repaints without the dot.
        rail.set_attention("gate", False)
        assert btn.property("attention") is False
        assert btn.grab().toImage() != after

    def test_set_button_visible(self, qtbot):
        rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", checkable=False)])
        qtbot.addWidget(rail)
        rail.show()  # unshown widgets report isVisible() False regardless
        assert rail.buttons["gate"].isHidden() is False
        rail.set_button_visible("gate", False)
        assert rail.buttons["gate"].isHidden() is True
        rail.set_button_visible("gate", True)
        assert rail.buttons["gate"].isHidden() is False
