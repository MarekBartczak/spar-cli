"""Design tokens and QSS builder for the spar gui dark theme.

Every color used anywhere in the gui package comes from :data:`TOKENS`.
Nothing else in ``spar/gui`` should hardcode a hex color -- if a new color
is needed, add a token here first.
"""

from __future__ import annotations

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
    """
