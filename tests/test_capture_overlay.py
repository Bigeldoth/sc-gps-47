"""Keep debug graphics outside the physical pixels sent to OCR."""

import os
from pathlib import Path
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from ui.capture_overlay import CaptureRegionOverlay, _paint_outline


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("ratio", [1.0, 1.25, 1.5, 1.75, 2.0])
@pytest.mark.parametrize("desktop_origin", [(1959, 0), (-2560, -1440), (0, 1440)])
def test_capture_pixels_stay_transparent_at_fractional_dpi(ratio, desktop_origin):
    region = QRect(*desktop_origin, 600, 60)
    origin = region.topLeft() - QPoint(3, 3)
    image = QImage(606, 66, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    _paint_outline(painter, region, origin)
    painter.end()

    # Every captured pixel, plus the one-pixel safety gap, must remain untouched.
    for y in range(2, 64):
        for x in range(2, 604):
            assert image.pixelColor(x, y).alpha() == 0
    # The two physical pixels of the border must remain visible at every scale.
    for x, y in [(0, 30), (1, 30), (604, 30), (605, 30),
                 (300, 0), (300, 1), (300, 64), (300, 65)]:
        assert image.pixelColor(x, y).name() == "#6fe8ff"
        assert image.pixelColor(x, y).alpha() == 255


def test_repositioned_window_cannot_draw_inside_capture():
    # Simulate a window manager shifting the native window at a screen edge.
    # Drawing must use the real client origin, not the requested window origin.
    region = QRect(-600, 0, 600, 60)
    actual_origin = QPoint(-604, -1)
    image = QImage(606, 66, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    _paint_outline(painter, region, actual_origin)
    painter.end()
    for y in range(1, 61):
        for x in range(4, 604):
            assert image.pixelColor(x, y).alpha() == 0
    assert image.pixelColor(1, 30).alpha() == 255
    assert image.pixelColor(300, 62).alpha() == 255


def test_overlay_requires_explicit_show_and_never_accepts_input(app):
    parent = QWidget()
    overlay = CaptureRegionOverlay(parent)
    overlay.set_capture_region({"left": -600, "top": -100, "width": 600, "height": 60})
    assert overlay.isWindow()
    assert overlay.parent() is parent
    assert not overlay.isVisible()
    assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus

    overlay.show()
    app.processEvents()
    assert overlay.isVisible()
    assert overlay.geometry() == QRect(-607, -107, 614, 74)
    overlay.set_capture_region(None)
    assert not overlay.isVisible()
    overlay.show()
    app.processEvents()
    assert not overlay.isVisible()
    parent.close()


def test_empty_capture_region_hides_existing_outline(app):
    overlay = CaptureRegionOverlay()
    overlay.set_capture_region({"left": 10, "top": 10, "width": 600, "height": 60})
    overlay.show()
    assert overlay.isVisible()
    overlay.set_capture_region({"left": 10, "top": 10, "width": 0, "height": 60})
    assert not overlay.isVisible()
    overlay.close()
