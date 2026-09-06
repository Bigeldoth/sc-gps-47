"""Non-interactive debug outline around an MSS capture region."""

import ctypes
from contextlib import contextmanager
import logging
import math
import sys
from typing import Mapping

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QWidget


logger = logging.getLogger(__name__)

_BORDER_PX = 2
_GAP_PX = 1
_MARGIN_PX = _BORDER_PX + _GAP_PX
# Qt rounds top-level edges at fractional DPI. Keep transparent room beyond the
# outline so rounding cannot clip its outermost physical pixel.
_WINDOW_MARGIN_PX = _MARGIN_PX + 4
_COLOR = QColor("#6FE8FF")


def _outline_rects(region: QRect, origin: QPoint) -> tuple[QRect, ...]:
    """Return four physical-pixel strips strictly outside the captured pixels."""
    x = region.x() - origin.x()
    y = region.y() - origin.y()
    width, height = region.width(), region.height()
    outer_x, outer_y = x - _MARGIN_PX, y - _MARGIN_PX
    outer_width = width + 2 * _MARGIN_PX
    return (
        QRect(outer_x, outer_y, outer_width, _BORDER_PX),
        QRect(outer_x, y + height + _GAP_PX, outer_width, _BORDER_PX),
        QRect(outer_x, y - _GAP_PX, _BORDER_PX, height + 2 * _GAP_PX),
        QRect(x + width + _GAP_PX, y - _GAP_PX,
              _BORDER_PX, height + 2 * _GAP_PX),
    )


def _paint_outline(painter: QPainter, region: QRect, origin: QPoint) -> None:
    """Paint in device pixels, independent of the window's fractional DPI scale."""
    painter.save()
    ratio = painter.device().devicePixelRatioF()
    painter.scale(1.0 / ratio, 1.0 / ratio)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    for rect in _outline_rects(region, origin):
        painter.fillRect(rect, _COLOR)
    painter.restore()


class _WindowsGeometry:
    """Keep native window placement and client origins in the MSS pixel frame."""

    def __init__(self):
        from ctypes import wintypes

        self._point_type = wintypes.POINT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._set_position = self._user32.SetWindowPos
        self._set_position.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        )
        self._set_position.restype = wintypes.BOOL
        self._client_to_screen = self._user32.ClientToScreen
        self._client_to_screen.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.POINT),
        )
        self._client_to_screen.restype = wintypes.BOOL
        self._set_dpi_context = getattr(
            self._user32, "SetThreadDpiAwarenessContext", None,
        )
        if self._set_dpi_context is not None:
            self._set_dpi_context.argtypes = (ctypes.c_void_p,)
            self._set_dpi_context.restype = ctypes.c_void_p

    @contextmanager
    def _physical_pixels(self):
        # Qt normally uses per-monitor awareness already. Explicitly preserve it
        # here so native coordinates cannot be virtualized into logical pixels.
        previous = None
        if self._set_dpi_context is not None:
            previous = self._set_dpi_context(ctypes.c_void_p(-4))
        try:
            yield
        finally:
            if previous:
                self._set_dpi_context(previous)

    def place(self, hwnd: int, bounds: QRect) -> None:
        with self._physical_pixels():
            # HWND_TOPMOST, SWP_NOACTIVATE | SWP_NOOWNERZORDER.
            if not self._set_position(
                hwnd, ctypes.c_void_p(-1), bounds.x(), bounds.y(),
                bounds.width(), bounds.height(), 0x0010 | 0x0200,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

    def client_origin(self, hwnd: int) -> QPoint:
        point = self._point_type(0, 0)
        with self._physical_pixels():
            if not self._client_to_screen(hwnd, ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
        return QPoint(point.x, point.y)


class CaptureRegionOverlay(QWidget):
    """Outline an MSS rectangle without receiving input or painting inside it.

    ``set_capture_region`` receives physical desktop pixels, including negative
    origins. The owner controls visibility with ``show``/``hide``; a ``None``
    region always hides the outline. A QWidget parent owns this top-level tool
    window for cleanup without making its coordinates relative to that parent.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        ))
        self.setWindowTitle("Capture region")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._region: QRect | None = None
        self._placement_valid = False
        self._native = (
            _WindowsGeometry()
            if sys.platform == "win32" and QApplication.platformName() == "windows"
            else None
        )

    def set_capture_region(self, region: Mapping[str, int] | None) -> None:
        """Update the rectangle without enabling visibility; None clears it."""
        if region is None:
            self._region = None
            self._placement_valid = False
            self.hide()
            return
        bounds = QRect(*(int(region[key]) for key in (
            "left", "top", "width", "height",
        )))
        if bounds.width() <= 0 or bounds.height() <= 0:
            self.set_capture_region(None)
            return
        self._region = bounds
        self._place()
        self.update()

    def _place(self) -> None:
        self._placement_valid = False
        if self._region is None:
            return
        bounds = self._region.adjusted(
            -_WINDOW_MARGIN_PX, -_WINDOW_MARGIN_PX,
            _WINDOW_MARGIN_PX, _WINDOW_MARGIN_PX,
        )
        try:
            if self._native is not None:
                self._native.place(int(self.winId()), bounds)
                # SetWindowPos updates the native screen/DPI, but QWidget also
                # needs its logical size or its backing store keeps the old one.
                ratio = self.devicePixelRatioF()
                self.resize(math.ceil(bounds.width() / ratio),
                            math.ceil(bounds.height() / ratio))
                self._native.place(int(self.winId()), bounds)
            else:
                # The headless Qt backend uses a one-to-one coordinate frame.
                self.setGeometry(bounds)
            self._placement_valid = True
        except OSError:
            logger.warning("Cannot position the capture region outline", exc_info=True)
            self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        # Qt can constrain a newly shown window to a screen or round its size.
        # Reapply the physical placement before the first visible paint.
        self._place()
        if not self._placement_valid:
            self.hide()

    def paintEvent(self, event):
        if self._region is None or not self._placement_valid:
            return
        try:
            origin = (
                self._native.client_origin(int(self.winId()))
                if self._native is not None else self.pos()
            )
        except OSError:
            # A misplaced border is worse than no border: never guess the origin.
            logger.warning("Cannot locate the capture region outline", exc_info=True)
            self.hide()
            return
        painter = QPainter(self)
        _paint_outline(painter, self._region, origin)
        painter.end()
