"""VBoxFront theme."""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication


def _palette() -> QPalette | None:
    app = QApplication.instance()
    return app.palette() if app is not None else None


def dark_theme() -> bool:
    palette = _palette()
    return palette is not None and palette.color(QPalette.ColorRole.Base).lightness() < 128


def muted_color() -> QColor:
    """Secondary text: subordinate to the body text but still readable.

    Half-way between the window's text and its background, which is the one
    definition of "dimmed" that holds whichever way round those two are.
    """
    palette = _palette()
    if palette is None:
        return QColor(136, 136, 136)
    text = palette.color(QPalette.ColorRole.WindowText)
    back = palette.color(QPalette.ColorRole.Window)
    return QColor(
        (text.red() + back.red()) // 2,
        (text.green() + back.green()) // 2,
        (text.blue() + back.blue()) // 2,
    )


def warning_color() -> QColor:
    """Amber for "this will surprise you", light enough to read on dark."""
    return QColor(255, 176, 72) if dark_theme() else QColor(192, 80, 0)


def error_color() -> QColor:
    """Red for "this is broken"."""
    return QColor(255, 120, 120) if dark_theme() else QColor(200, 40, 40)


def color_css(color: QColor) -> str:
    """A stylesheet fragment for one of the colours above."""
    return f"color:{color.name()};"
