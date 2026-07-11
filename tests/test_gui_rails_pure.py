from __future__ import annotations

from spar.gui.rails import right_column_visibility


class TestRightColumnVisibility:
    def test_hidden_only_when_both_collapsed(self):
        assert right_column_visibility(False, False) is False
        assert right_column_visibility(True, False) is True
        assert right_column_visibility(False, True) is True
        assert right_column_visibility(True, True) is True
