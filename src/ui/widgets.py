"""PADEK utility widgets for SpaceDrive GPS."""
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QBrush, QColor, QPen
from PyQt6.QtCore import Qt, QRect


class SignalBarsWidget(QWidget):
    """4 vertical bars indicating OCR signal freshness.

    level=0 LIVE   -> 4 emerald bars
    level=1 AGING  -> 3 copper bars
    level=2 STALE  -> 2 orange bars
    level=3 LOST   -> 1 danger bar
    """

    COLORS = {
        0: QColor(0x19, 0xC2, 0x8A),   # emerald
        1: QColor(0xD9, 0xA3, 0x68),   # copper
        2: QColor(0xFF, 0x7A, 0x45),   # orange
        3: QColor(0xE5, 0x48, 0x4D),   # danger
    }
    INACTIVE = QColor(238, 243, 246, 30)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 14)
        self._level = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_level(self, level: int) -> None:
        if self._level != level:
            self._level = max(0, min(3, level))
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        active_count = 4 - self._level
        color = self.COLORS.get(self._level, self.COLORS[3])
        heights = [5, 8, 11, 14]
        for i, h in enumerate(heights):
            x = i * 5
            y = 14 - h
            p.setBrush(QBrush(color if i < active_count else self.INACTIVE))
            p.drawRoundedRect(QRect(x, y, 3, h), 1, 1)
        p.end()
