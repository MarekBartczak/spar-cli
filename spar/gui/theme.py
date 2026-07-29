"""Design tokens and QSS builder for the spar gui dark theme.

Every color used anywhere in the gui package comes from :data:`TOKENS`.
Nothing else in ``spar/gui`` should hardcode a hex color -- if a new color
is needed, add a token here first.
"""

from __future__ import annotations

from pathlib import Path

# QSS ``image: url(...)`` needs a resolvable path, and the gui may run from any
# cwd, so asset urls are built from this package's location.
_ASSETS = Path(__file__).resolve().parent / "assets"

TOKENS: dict[str, str] = {
    "ground": "#14171c",
    "panel": "#1b1f26",
    "panel-alt": "#20252e",
    "line": "#2c323c",
    "text": "#d5dae2",
    "muted": "#7d8590",
    "claude": "#e8a854",
    "codex": "#56c3d6",
    "spar-log": "#9d8cff",
    "ok": "#6fbf73",
    "warn": "#e0b154",
    "gate": "#e0679a",
    # Loud red for a mode that acts on the user's behalf (auto mode): it must
    # be impossible to miss that gates are being answered without them.
    "armed": "#e5484d",
}

__all__ = ["TOKENS", "build_qss"]


def build_qss() -> str:
    """Build the application-wide QSS stylesheet from :data:`TOKENS`.

    Kept as plain string templating (no external QSS files) so the token
    dict stays the single source of truth: every color that appears in the
    returned string is a ``TOKENS`` value.
    """
    t = TOKENS
    return f"""
    QMainWindow, QWidget {{
        background-color: {t['ground']};
        color: {t['text']};
    }}

    QToolBar {{
        background-color: {t['panel']};
        border: none;
        border-bottom: 1px solid {t['line']};
        spacing: 6px;
    }}

    QToolBar QToolButton {{
        color: {t['text']};
        background-color: {t['panel']};
        border: 1px solid {t['line']};
        padding: 4px 10px;
        border-radius: 4px;
    }}

    QToolBar QToolButton:disabled {{
        color: {t['muted']};
        background-color: {t['panel']};
    }}

    QStatusBar {{
        background-color: {t['panel']};
        color: {t['muted']};
        border-top: 1px solid {t['line']};
    }}

    /* Auto mode answers gates without asking, so an ARMED toggle shouts.
       The indicator is a real tick (an SVG in armed red): filling the
       indicator's background instead just paints over Qt's checkmark and
       leaves a bare colored square. */
    #autoModeCheckbox {{
        color: {t['muted']};
        padding: 1px 6px;
        border: 1px solid transparent;
        border-radius: 3px;
        spacing: 5px;
    }}
    #autoModeCheckbox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {t['line']};
        border-radius: 3px;
        background-color: {t['panel-alt']};
    }}
    #autoModeCheckbox:checked {{
        color: {t['armed']};
        font-weight: bold;
        border: 1px solid {t['armed']};
    }}
    #autoModeCheckbox::indicator:checked {{
        border: 1px solid {t['armed']};
        background-color: transparent;
        image: url({_ASSETS.joinpath('check-armed.svg').as_posix()});
    }}

    QSplitter::handle {{
        background-color: {t['line']};
    }}

    #rightSplit::handle {{
        background-color: {t['line']};
    }}
    #rightSplit::handle:hover {{
        background-color: {t['muted']};
    }}

    #streamPane {{
        background-color: {t['panel']};
    }}

    #sidePane {{
        background-color: {t['panel-alt']};
    }}

    #iconRail {{
        background-color: {t['panel']};
        border: none;
    }}
    #iconRail QToolButton {{
        color: {t['text']};
        background-color: {t['panel']};
        border: 1px solid {t['line']};
        border-radius: 4px;
        font-size: 18px;   /* large glyph face -> reads as an icon, not a text label */
    }}
    #iconRail QToolButton:checked {{
        background-color: {t['panel-alt']};
        border: 1px solid {t['claude']};
    }}
    #iconRail QToolButton:disabled {{
        color: {t['muted']};
    }}
    #iconRail QToolButton[attention="true"] {{
        border: 1px solid {t['warn']};
        color: {t['warn']};
    }}

    #filesTree {{
        background-color: {t['panel']};
        border: none;
    }}
    #filesTabs::pane {{
        border: 1px solid {t['line']};
    }}
    #filesReadOnlyBanner {{
        color: {t['warn']};
        background-color: {t['panel']};
        border: 1px solid {t['warn']};
        padding: 2px 6px;
    }}
    #diskBanner {{
        background-color: {t['panel-alt']};
        border: 1px solid {t['gate']};
    }}
    #fileFinder {{
        background-color: {t['panel']};
        border: 1px solid {t['line']};
    }}
    #finderList {{
        background-color: {t['panel']};
        color: {t['text']};
    }}

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
    #searchMaskCheck {{
        color: {t['text']};
    }}
    #searchMaskCombo {{
        background-color: {t['panel-alt']};
        color: {t['text']};
        border: 1px solid {t['line']};
        border-radius: 4px;
        padding: 2px 6px;
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
    """
