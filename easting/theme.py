"""Theme-aware colors for the plugin's widgets.

QGIS ships light and dark themes and users switch freely, so nothing here
hardcodes a single palette. Two rules keep the dock legible either way:

1. Secondary and accent *text* colors are chosen per theme, because a color
   that reads on paper (a dark green) disappears on a dark background.
2. Any custom *background* sets an explicit foreground alongside it. Setting
   only a background inherits the theme's text color, which is exactly how
   light-on-light and dark-on-dark illegibility happens.
"""

from __future__ import annotations

from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication

# Verdict badges keep saturated backgrounds with white text in both themes:
# the badge supplies its own contrast, so it does not need a theme variant.
# REVIEW is a darkened amber: plain #b58900 only reaches 3.2:1 against white
# text, which fails AA at this size.
VERDICT_BADGE_BG = {"PASS": "#0a7d32", "REVIEW": "#8a6500", "FAIL": "#c0392b"}

# Verdict-colored *text* (the reason lines) does need one.
_VERDICT_TEXT_LIGHT = {"PASS": "#0a7d32", "REVIEW": "#8a6a00", "FAIL": "#c0392b"}
_VERDICT_TEXT_DARK = {"PASS": "#5fd48a", "REVIEW": "#e8b93d", "FAIL": "#f08a80"}


def is_dark() -> bool:
    """True when the active palette is a dark theme."""
    return QApplication.palette().color(QPalette.Window).lightness() < 128


def verdict_text_color(status: str) -> str:
    table = _VERDICT_TEXT_DARK if is_dark() else _VERDICT_TEXT_LIGHT
    return table.get(status, secondary_text())


def secondary_text() -> str:
    """Muted text that still clears contrast against the current background."""
    return "#9aa2ad" if is_dark() else "#5c6370"


def notes_colors() -> tuple[str, str, str]:
    """(background, foreground, accent) for the extractor-notes callout."""
    if is_dark():
        return "#1b2536", "#dfe5ee", "#4f8bd6"
    return "#f4f7fb", "#14161a", "#1d5fbf"


def notes_style() -> str:
    """Stylesheet for the extractor-notes callout: background plus foreground."""
    background, foreground, accent = notes_colors()
    return (
        f"background:{background}; color:{foreground}; padding:6px; border-left:3px solid {accent};"
    )


def confidence_colors(confidence: str) -> tuple[QColor, QColor] | None:
    """(background, foreground) for a flagged confidence cell, or None for high."""
    if confidence == "high":
        return None
    if is_dark():
        fills = {"medium": "#4a3d10", "low": "#4d2320"}
        return QColor(fills.get(confidence, "#3a3a3a")), QColor("#f2ede0")
    fills = {"medium": "#fdf6d8", "low": "#fde8e8"}
    return QColor(fills.get(confidence, "#eeeeee")), QColor("#14161a")
