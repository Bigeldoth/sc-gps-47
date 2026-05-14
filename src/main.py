import sys
import os
import math
import time
import logging
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout,
                             QWidget, QFrame, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QAction, QColor, QCursor
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import (
    NavigationEngine,
    format_distance,
    calculate_velocity_bearing,
    calculate_absolute_bearing,
    format_axis_delta,
    ema_angle,
    normalize_angle_signed,
    _zones_match,
)
from config_manager import ConfigManager
from hotkey_listener import HotkeyListener
from velocity_tracker import VelocityTracker
from ui.options import OptionsWindow
from ui.poi_manager import POIManagerWindow

config = configparser.ConfigParser()
config.read('config.ini')

logging.basicConfig(
    level=getattr(logging, config.get('Logging', 'level', fallback='INFO')),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=config.get('Logging', 'file', fallback='spacedrive.log')
)
logger = logging.getLogger(__name__)


class GPSWorker(QObject):
    result_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.capture = ScreenCapture()
        ocr_engine = config.get('OCR', 'engine', fallback='tesseract')
        glyph_engine = config.get('OCR', 'glyph_engine', fallback='ncc')
        self.ocr = OCRProcessor(
            engine=ocr_engine,
            glyph_engine=glyph_engine,
            onnx_model_path=config.get('OCR', 'onnx_model_path',
                                       fallback='models/spacedrive_ocr.onnx'),
            onnx_classes_path=config.get('OCR', 'onnx_classes_path',
                                         fallback='models/spacedrive_ocr.classes.json'),
            onnx_confidence_threshold=float(config.get(
                'OCR', 'onnx_confidence_threshold', fallback='0.85')),
        )
        logger.info(f"OCR engine initialized: text={ocr_engine}  glyphs={glyph_engine}")
        self._running = True

    def process(self):
        if not self._running:
            return
        try:
            images, glyph_data = self.capture.capture()
            data = self.ocr.extract_data(images)
            self.result_ready.emit(data)
        except Exception as e:
            logger.error(f"GPS worker error: {e}")
            self.result_ready.emit({"x": None, "y": None, "z": None, "location": "Unknown", "error": str(e)})

    def stop(self):
        self._running = False
        self.capture.stop()
        self.ocr.shutdown()


def _fmt_ooc(ooc):
    """Transforms 'Stanton_1_Hurston' → 'Stanton 1 Hurston' for display."""
    if not ooc:
        return "?"
    return ooc.replace("_", " ").strip()


_ARROWS_8 = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")


def _world_arrow(abs_bearing):
    """8-direction navigation arrow to target (SC frame corrected).

    SC (OOC) Convention: Y+ = forward (↑), Y- = backward (↓),
    X- = right (→), X+ = left (←). X correction is already
    applied in calculate_absolute_bearing (yaw_deg uses -dx).

    Returns 'horizontal_arrow + vertical_indicator':
      - ▲ if target is notably above (pitch > 25°)
      - ▼ if notably below
    """
    if not abs_bearing:
        return ""
    yaw_norm = (abs_bearing["yaw_deg"] + 360.0) % 360.0
    arrow_h = _ARROWS_8[int((yaw_norm + 22.5) / 45) % 8]
    pitch = abs_bearing["pitch_deg"]
    if pitch > 25.0:
        arrow_v = "▲"
    elif pitch < -25.0:
        arrow_v = "▼"
    else:
        arrow_v = ""
    return arrow_h + arrow_v


# Time→color anchors for OCR data aging.
# Linear RGB interpolation between these points (in seconds).
_AGE_STOPS = (
    (0.0,  (0x40, 0xff, 0x40)),   # bright green — fresh data
    (3.0,  (0xff, 0xee, 0x40)),   # yellow (3s)
    (7.0,  (0xff, 0xaa, 0x00)),   # orange (7s)
    (12.0, (0xff, 0x40, 0x40)),   # red — stale data
)

# Beyond this age, quick save via hotkey is rejected.
# Calibrated just before entering red → accepts green+yellow+orange.
_QUICK_SAVE_MAX_AGE_S = 9.0


def _age_to_color(age_s):
    """Linearly interpolates a HEX color from age (s) of last valid OCR.

    Green (0s) → yellow (1.5s) → orange (3s) → red (5s+). Progression
    depends only on elapsed time, not number of scans, to remain
    stable even when OCR frequency varies.
    """
    if age_s is None or age_s <= _AGE_STOPS[0][0]:
        r, g, b = _AGE_STOPS[0][1]
    elif age_s >= _AGE_STOPS[-1][0]:
        r, g, b = _AGE_STOPS[-1][1]
    else:
        for i in range(len(_AGE_STOPS) - 1):
            t0, c0 = _AGE_STOPS[i]
            t1, c1 = _AGE_STOPS[i + 1]
            if t0 <= age_s <= t1:
                t = (age_s - t0) / (t1 - t0)
                r = int(c0[0] + t * (c1[0] - c0[0]))
                g = int(c0[1] + t * (c1[1] - c0[1]))
                b = int(c0[2] + t * (c1[2] - c0[2]))
                break
    return f"#{r:02x}{g:02x}{b:02x}"


class GPSOverlay(QMainWindow):
    save_point_signal = pyqtSignal()
    trigger_worker = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.current_data = {"x": None, "y": None, "z": None, "ooc": None, "location": "Unknown"}
        self._worker_busy = False
        self.options_window = None
        self.poi_manager_window = None

        # EMA smoothing + stale OCR detection for target distance
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._last_coord_ts = None
        self._ema_alpha = 0.4
        self._stale_threshold_s = 2.0

        # Distance below which we consider the target reached: the EMA is
        # bypassed and the raw OCR distance is shown directly. Configurable
        # via Navigation.arrival_radius_m in config.ini.
        self._arrival_radius_m = self.config_manager.get_arrival_radius_m()

        # Directional guidance based on velocity (car GPS style).
        # We sample position and derive movement direction,
        # rather than reading camera orientation (CamDir).
        self._velocity_tracker = VelocityTracker()
        self._last_known_ooc = None  # to detect frame change
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None

        # Snapshot of coordinates frozen at hotkey save press moment.
        # Preserves value even if OCR fails between press and dialog confirmation.
        # None = no save in progress via hotkey.
        self._save_snapshot = None

        # Temporary message displayed in nav_label: (text, expire_monotonic_ts).
        self._overlay_message = None

        # UI — MFD frame Star Citizen style
        self.central_widget = QWidget()
        self.central_widget.setStyleSheet("background-color: transparent;")

        outer = QVBoxLayout(self.central_widget)
        outer.setContentsMargins(0, 0, 0, 0)

        self._mfd = QFrame()
        self._mfd.setStyleSheet("""
            QFrame {
                background-color: rgba(0, 0, 0, 175);
                border: 1px solid rgba(255, 170, 0, 210);
                border-radius: 4px;
            }
        """)
        self.layout = QVBoxLayout(self._mfd)
        self.layout.setContentsMargins(10, 7, 10, 7)
        self.layout.setSpacing(4)

        _lbl_css = "border: none; font-family: 'Consolas', 'Menlo', monospace;"

        self.pos_label = QLabel("Scanning...")
        self.pos_label.setStyleSheet(f"color: #c8c8c8; font-size: 13px; {_lbl_css}")

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setStyleSheet("background-color: rgba(255,170,0,160); border: none; max-height: 1px;")

        self.nav_label = QLabel("NO TARGET")
        self.nav_label.setStyleSheet(f"color: #00ffff; font-size: 14px; font-weight: bold; {_lbl_css}")

        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(_sep)
        self.layout.addWidget(self.nav_label)

        outer.addWidget(self._mfd)
        self.setCentralWidget(self.central_widget)

        self.init_window_properties()
        self.setup_tray_icon()
        self._setup_worker_thread()

        scan_interval = self.config_manager.get_scan_interval()
        self.timer = QTimer()
        self.timer.timeout.connect(self._request_update)
        self.timer.start(scan_interval)

        # Dedicated timer for visual color aging: independent of OCR rhythm,
        # guarantees smooth transition (green→orange→red) even if worker slows
        # or misses several scans in a row.
        self._color_refresh_timer = QTimer()
        self._color_refresh_timer.timeout.connect(self._tick_visual_refresh)
        self._color_refresh_timer.start(150)

        # Always-on-top safety net: SC borderless fullscreen and some games
        # steal Z-order despite WindowStaysOnTopHint. Re-raise periodically so
        # the overlay reappears within at most ~1 second of being hidden.
        self._stay_on_top_timer = QTimer()
        self._stay_on_top_timer.timeout.connect(self._reassert_on_top)
        self._stay_on_top_timer.start(1000)

        self.save_point_signal.connect(self.prompt_save_point)

        self.hotkey_listener = HotkeyListener(self.config_manager)
        # Hotkeys emitted from pynput thread → QueuedConnection to process on Qt thread
        self.hotkey_listener.toggle_overlay_triggered.connect(
            self.toggle_overlay, Qt.ConnectionType.QueuedConnection
        )
        self.hotkey_listener.open_options_triggered.connect(
            self.show_options_window, Qt.ConnectionType.QueuedConnection
        )
        self.hotkey_listener.save_position_triggered.connect(
            self._on_hotkey_save_position, Qt.ConnectionType.QueuedConnection
        )
        self.hotkey_listener.open_poi_manager_triggered.connect(
            self.show_poi_manager_window, Qt.ConnectionType.QueuedConnection
        )

    def _on_hotkey_save_position(self):
        """Captures current coordinates at exact press moment.

        If OCR reading is too old (red), rejects save and displays
        message in overlay. Otherwise, freezes snapshot so value doesn't
        change while dialog remains open.
        """
        logger.debug("Hotkey 'save_position' detected")
        age = self._coord_age_s()
        if self.current_data.get("x") is None or age is None:
            self._show_overlay_message("No OCR position")
            return
        if age > _QUICK_SAVE_MAX_AGE_S:
            self._show_overlay_message("Quick save not possible")
            return
        self._save_snapshot = dict(self.current_data)
        self.save_point_signal.emit()

    def _show_overlay_message(self, text, duration_s=3.0):
        """Displays ephemeral message in nav_label in red."""
        self._overlay_message = (text, time.monotonic() + duration_s)
        self._refresh_nav_label()

    def _setup_worker_thread(self):
        self._worker_thread = QThread()
        self._worker = GPSWorker()
        self._worker.moveToThread(self._worker_thread)
        self.trigger_worker.connect(self._worker.process)
        self._worker.result_ready.connect(self._on_worker_result)
        self._worker_thread.start()

    def _request_update(self):
        if self._worker_busy:
            return
        self._worker_busy = True
        self.trigger_worker.emit()

    def _on_worker_result(self, data):
        self._worker_busy = False

        if "error" in data:
            # Don't touch displayed text — it will age via color.
            self._refresh_pos_color()
            self._refresh_nav_label()
            return

        # A scan returning None must NOT erase last known value.
        # We only update current_data + text if OCR actually read a position.
        if data["x"] is not None:
            # Reject by implausible velocity (Phase B): if new position
            # implies speed > 100 km/s vs last scan, probably OCR hallucination.
            # Keep previous value. Outside quantum drive, SC vessels cap
            # around 1-2 km/s — 100 km/s gives comfortable margin
            # for post-quantum jumps.
            if self._is_velocity_implausible(data):
                self._refresh_pos_color()
                self._refresh_nav_label()
                return

            self.current_data = data
            self.pos_label.setText(
                f"X: {data['x']:>10.2f}   Y: {data['y']:>10.2f}\n"
                f"Z: {data['z']:>10.2f}"
            )
            self._last_coord_ts = time.monotonic()

            # Updates target zone tracking (lock after 1st match,
            # grace period 3 min otherwise — see NavigationEngine).
            self.nav.update_zone_tracking(data)

            self._update_bearing_state(data)

            dist_km = self.nav.calculate_distance(data)
            if dist_km is None:
                # Distance temporarily unavailable (e.g., OOC misread this tick)
                # — keep last known value rather than clearing display.
                # Temporal coloring already signals data freshness.
                pass
            elif (
                not self._velocity_tracker.is_moving
                or self._smoothed_distance_km is None
            ):
                self._smoothed_distance_km = dist_km
            elif dist_km * 1000.0 < self._arrival_radius_m:
                # Within arrival radius: bypass EMA so the readout matches the
                # raw OCR distance exactly. Configurable via Navigation.arrival_radius_m.
                self._smoothed_distance_km = dist_km
            else:
                # Snap-on-large-jump: when arriving at target, tracker takes
                # 1-2s to set is_moving=False, and EMA lags (e.g., target
                # shows 4.68km when at 18m). If relative gap is large, bypass EMA.
                prev = self._smoothed_distance_km
                relative_jump = abs(dist_km - prev) / max(prev, 0.001)
                if relative_jump > 0.3:
                    self._smoothed_distance_km = dist_km
                else:
                    a = self._ema_alpha
                    self._smoothed_distance_km = a * dist_km + (1 - a) * prev
            self._last_raw_distance_km = dist_km

        # Always refresh color (aging), even without new data.
        self._refresh_pos_color()
        self._refresh_nav_label()

    def _coord_age_s(self):
        """Age (s) of last valid OCR, or None if none yet."""
        if self._last_coord_ts is None:
            return None
        return time.monotonic() - self._last_coord_ts

    # Maximum plausible speed between two scans (km/s).
    # Outside quantum drive, SC vessels do ~1-2 km/s. 100 km/s gives
    # margin for quantum exit without accepting OCR jumps.
    _MAX_PLAUSIBLE_SPEED_KM_S = 100.0

    def _is_velocity_implausible(self, new_data):
        """True if new position implies physically impossible jump.

        Compares current position (`current_data`) to new (`new_data`)
        using time delta from `_last_coord_ts`. Rejects if
        implied speed exceeds `_MAX_PLAUSIBLE_SPEED_KM_S`.

        Tolerates:
          - first scan (no reference) → False;
          - OOC change (legitimate teleport via QT) → False;
          - long time gap (> 5 s, we may have missed jump) → False.
        """
        if self.current_data.get("x") is None or self._last_coord_ts is None:
            return False
        # OOC change: don't compare, frame changed.
        # Fuzzy comparison to tolerate OCR variations in zone name.
        new_ooc = new_data.get("ooc")
        cur_ooc = self.current_data.get("ooc")
        if new_ooc and cur_ooc and not _zones_match(new_ooc, cur_ooc):
            return False
        dt = time.monotonic() - self._last_coord_ts
        if dt <= 0 or dt > 5.0:
            return False
        dx = new_data["x"] - self.current_data["x"]
        dy = new_data["y"] - self.current_data["y"]
        dz = new_data["z"] - self.current_data["z"]
        dist_km = (dx * dx + dy * dy + dz * dz) ** 0.5
        speed = dist_km / dt
        if speed > self._MAX_PLAUSIBLE_SPEED_KM_S:
            logger.warning(
                f"OCR rejection: implausible speed {speed:.1f} km/s "
                f"(Δ={dist_km:.2f} km in {dt:.2f} s)"
            )
            return True
        return False

    def _tick_visual_refresh(self):
        """Recolors pos_label and nav_label without triggering OCR.

        Decoupled from worker so green→orange→red transition remains
        smooth independent of cadence or OCR failures.
        """
        self._refresh_pos_color()
        self._refresh_nav_label()

    def _refresh_pos_color(self):
        """Updates only pos_label color based on coordinate age."""
        age = self._coord_age_s()
        if age is None:
            color = "#666666"
            text = self.pos_label.text()
            if not text or text == "Scanning...":
                self.pos_label.setText("Scanning...")
        else:
            color = _age_to_color(age)
        self.pos_label.setStyleSheet(
            f"color: {color}; font-size: 13px; border: none; "
            "font-family: 'Consolas', 'Menlo', monospace;"
        )

    def _update_bearing_state(self, data):
        """Updates velocity and bearing offsets from OCR coordinates."""
        # If OOC changed, coordinates are in different frame → reset
        # tracker to avoid spurious velocity. Uses fuzzy comparison
        # to ignore OCR variations in zone name.
        current_ooc = data.get("ooc")
        if current_ooc is not None and not _zones_match(current_ooc, self._last_known_ooc or ""):
            if self._last_known_ooc is not None:
                logger.info(
                    f"OOC zone change: {self._last_known_ooc} → {current_ooc}, "
                    f"VelocityTracker reset"
                )
                self._velocity_tracker.reset()
            self._last_known_ooc = current_ooc

        if data.get("x") is not None:
            self._velocity_tracker.add_sample(
                data["x"], data["y"], data["z"], time.monotonic()
            )
            if self.nav.target:
                logger.debug(
                    "OCR Position: X=%.2f Y=%.2f Z=%.2f | "
                    "Target '%s': X=%.2f Y=%.2f Z=%.2f",
                    data["x"], data["y"], data["z"],
                    self.nav.target.get("name", "?"),
                    self.nav.target["x"], self.nav.target["y"], self.nav.target["z"],
                )

        velocity = self._velocity_tracker.velocity if self._velocity_tracker.is_moving else None

        if velocity is None or self.nav.target is None:
            self._smoothed_yaw_off = None
            self._smoothed_pitch_off = None
            self._last_raw_yaw_off = None
            self._last_raw_pitch_off = None
            return

        bearing = calculate_velocity_bearing(velocity, data, self.nav.target)
        if bearing is None:
            return
        yaw_off, pitch_off = bearing
        logger.debug(
            "Velocity bearing: vel=(%.3f,%.3f,%.3f) km/s "
            "→ yaw_off=%.1f° pitch_off=%.1f°",
            velocity[0], velocity[1], velocity[2], yaw_off, pitch_off,
        )

        # Skip-on-stable + EMA with wrap-around
        if (
            self._last_raw_yaw_off == yaw_off
            and self._last_raw_pitch_off == pitch_off
        ):
            self._smoothed_yaw_off = yaw_off
            self._smoothed_pitch_off = pitch_off
        elif self._smoothed_yaw_off is None:
            self._smoothed_yaw_off = yaw_off
            self._smoothed_pitch_off = pitch_off
        else:
            a = self._ema_alpha
            self._smoothed_yaw_off = ema_angle(self._smoothed_yaw_off, yaw_off, a)
            self._smoothed_pitch_off = ema_angle(self._smoothed_pitch_off, pitch_off, a)
        self._last_raw_yaw_off = yaw_off
        self._last_raw_pitch_off = pitch_off

    def _refresh_nav_label(self):
        """Updates nav_label: target, distance and heading merged."""
        _css = "border: none; font-family: 'Consolas', 'Menlo', monospace;"

        # Ephemeral message takes priority (e.g., "Quick save not possible").
        if self._overlay_message is not None:
            text, expire_ts = self._overlay_message
            if time.monotonic() < expire_ts:
                self.nav_label.setText(text)
                self.nav_label.setStyleSheet(
                    f"color: #ff4040; font-size: 14px; font-weight: bold; {_css}"
                )
                return
            self._overlay_message = None

        if not self.nav.target:
            self.nav_label.setText("NO TARGET")
            self.nav_label.setStyleSheet(f"color: #555555; font-size: 13px; {_css}")
            return

        target_ooc = self.nav.target.get("ooc")
        current_ooc = self.current_data.get("ooc")

        if target_ooc is None:
            self.nav_label.setText(f"▶ {self.nav.target['name']}\n  Legacy POI — recreate")
            self.nav_label.setStyleSheet(f"color: #ffaa00; font-size: 13px; {_css}")
            return

        # Zone tracking logic: assume in-zone while grace period active.
        # Once locked (1st OCR match), permanent. Otherwise after 3 min without
        # match, display out-of-zone warning.
        if not self.nav.is_target_in_same_ooc(self.current_data):
            self.nav_label.setText(
                f"▶ {self.nav.target['name']}\n"
                f"  zone: {_fmt_ooc(target_ooc)}\n"
                f"  current: {_fmt_ooc(current_ooc) if current_ooc else '?'}"
            )
            self.nav_label.setStyleSheet(f"color: #ffaa00; font-size: 12px; {_css}")
            return

        name = self.nav.target['name']

        if self._smoothed_distance_km is None:
            self.nav_label.setText(f"▶ {name}\n  ---")
            self.nav_label.setStyleSheet(f"color: #555555; font-size: 14px; {_css}")
            return

        # Reliability color linearly interpolated on age of last OCR.
        age_color = _age_to_color(self._coord_age_s())

        dist_str = format_distance(self._smoothed_distance_km)

        # Navigation arrow to target (SC frame: X inverted corrected).
        abs_bearing = calculate_absolute_bearing(self.current_data, self.nav.target)
        arrow_str = ""
        if abs_bearing:
            logger.debug(
                "Absolute bearing to '%s': dx=%.2f dy=%.2f dz=%.2f "
                "yaw=%.1f° pitch=%.1f° dist=%.2f km",
                self.nav.target.get("name", "?"),
                abs_bearing["dx"], abs_bearing["dy"], abs_bearing["dz"],
                abs_bearing["yaw_deg"], abs_bearing["pitch_deg"],
                abs_bearing["distance_km"],
            )
            arrow = _world_arrow(abs_bearing)
            if arrow:
                arrow_str = f"  {arrow}"

        # Yaw/pitch heading relative to movement — only available while moving.
        # Distance ALWAYS displays, independent of heading.
        cap_str = ""
        if self._velocity_tracker.is_moving and self._smoothed_yaw_off is not None:
            yaw = self._smoothed_yaw_off
            pitch = self._smoothed_pitch_off
            max_off = max(abs(yaw), abs(pitch))
            if max_off < 5.0:
                cap_str = "   ✓ aligned"
            else:
                yaw_arrow = "→" if yaw > 0 else "←"
                pitch_arrow = "↑" if pitch > 0 else "↓"
                cap_str = f"   {yaw_arrow}{abs(yaw):.0f}°  {pitch_arrow}{abs(pitch):.0f}°"

        self.nav_label.setText(f"▶ {name}\n  {dist_str}{arrow_str}{cap_str}")
        self.nav_label.setStyleSheet(f"color: {age_color}; font-size: 14px; font-weight: bold; {_css}")

    def init_window_properties(self):
        flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 320, 150)
        self.central_widget.setStyleSheet("background-color: transparent; border: none;")

        if self.isVisible():
            self.show()

    def _reassert_on_top(self):
        """Re-raise the overlay so it stays above SC's borderless window.

        WindowStaysOnTopHint is honoured by Qt but some fullscreen game windows
        push other top-level widgets behind themselves when focused. Calling
        raise_() periodically is enough to restore the Z-order without
        stealing input focus (the overlay is WindowTransparentForInput).
        """
        if not self.is_visible:
            return
        self.raise_()

    def _load_app_icon(self):
        """Loads SpaceDrive icon from assets/, or system fallback.

        Looks in order:
          1. assets/icon.ico  (Windows, native multi-size)
          2. assets/icon.png  (cross-platform high resolution)
          3. standard SP_ComputerIcon (ultimate fallback)
        """
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        for filename in ('icon.ico', 'icon.png'):
            path = os.path.join(base_dir, 'assets', filename)
            if os.path.exists(path):
                icon = QIcon(path)
                if not icon.isNull():
                    logger.debug(f"Icône application : {path}")
                    return icon

        logger.warning("No icon found in assets/, fallback SP_ComputerIcon")
        return self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self._load_app_icon())
        # Also set as window icon (taskbar, alt-tab, etc.)
        self.setWindowIcon(self.tray_icon.icon())

        tray_menu = QMenu()

        self.toggle_action = QAction("Hide overlay", self)
        self.toggle_action.triggered.connect(self.toggle_overlay)
        tray_menu.addAction(self.toggle_action)

        options_action = QAction("Options...", self)
        options_action.triggered.connect(self.show_options_window)
        tray_menu.addAction(options_action)

        poi_action = QAction("POI Manager...", self)
        poi_action.triggered.connect(self.show_poi_manager_window)
        tray_menu.addAction(poi_action)

        reset_gps_action = QAction("Reset GPS", self)
        reset_gps_action.triggered.connect(self.reset_velocity_tracker)
        tray_menu.addAction(reset_gps_action)

        tray_menu.addSeparator()

        poi_menu = tray_menu.addMenu("Points of Interest")

        save_action = QAction("Save position", self)
        save_action.triggered.connect(self.prompt_save_point)
        poi_menu.addAction(save_action)

        select_poi_action = QAction("Choose destination...", self)
        select_poi_action.triggered.connect(self.show_poi_selector)
        poi_menu.addAction(select_poi_action)

        data_menu = tray_menu.addMenu("Data")

        export_action = QAction("Export points (JSON)", self)
        export_action.triggered.connect(self.export_data)
        data_menu.addAction(export_action)

        import_action = QAction("Import points (JSON)", self)
        import_action.triggered.connect(self.import_data)
        data_menu.addAction(import_action)

        tray_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        self.tray_icon.showMessage(
            "SpaceDrive GPS",
            "GPS overlay active!",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def toggle_overlay(self):
        if self.is_visible:
            self.hide()
            self.is_visible = False
            self.toggle_action.setText("Show overlay")
        else:
            self.show()
            self.is_visible = True
            self.toggle_action.setText("Hide overlay")

    def _bring_dialog_to_front(self, dialog):
        """Forces dialog to foreground despite overlay always-on-top."""
        dialog.show()
        # Deferred activation so Qt processes show() event first
        QTimer.singleShot(0, dialog.raise_)
        QTimer.singleShot(0, dialog.activateWindow)

    def show_options_window(self):
        try:
            if self.options_window is None or not self.options_window.isVisible():
                self.options_window = OptionsWindow(self.config_manager, self.hotkey_listener, self)
                self.options_window.options_saved.connect(self._on_options_saved)
            self._bring_dialog_to_front(self.options_window)
        except Exception:
            logger.exception("Error opening options")
            self.tray_icon.showMessage(
                "Error",
                "Could not open options window. See spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def show_poi_manager_window(self):
        logger.debug("POI manager window opening requested")
        try:
            if self.poi_manager_window is None or not self.poi_manager_window.isVisible():
                self.poi_manager_window = POIManagerWindow(self.nav, self)
                self.poi_manager_window.destination_changed.connect(self._on_destination_changed)
                self.poi_manager_window.goto_requested.connect(self._on_goto_requested)
            self._bring_dialog_to_front(self.poi_manager_window)
        except Exception:
            logger.exception("Error opening POI manager")
            self.tray_icon.showMessage(
                "Error",
                "Could not open POI manager. See spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def _on_options_saved(self):
        scan_interval = self.config_manager.get_scan_interval()
        self.timer.setInterval(scan_interval)
        logger.info(f"Scan interval updated: {scan_interval} ms")

        # Pick up the new arrival radius for the next OCR tick.
        self._arrival_radius_m = self.config_manager.get_arrival_radius_m()
        logger.info(f"Arrival radius updated: {self._arrival_radius_m:.0f} m")

        # Refresh hotkeys in case they were modified
        self.hotkey_listener.reload_hotkeys()

    def _on_destination_changed(self, poi):
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"], ooc=poi.get("ooc"))
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        logger.info(f"Destination set: {poi['name']}")

    def _on_goto_requested(self, poi):
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"], ooc=poi.get("ooc"))
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        if not self.is_visible:
            self.toggle_overlay()

    def reset_velocity_tracker(self):
        """Forgets position history (useful after quantum jump)."""
        self._velocity_tracker.reset()
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        self.tray_icon.showMessage(
            "GPS reset",
            "Movement history cleared. Move to recalculate direction.",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )
        logger.info("VelocityTracker reset")

    def prompt_save_point(self):
        logger.debug("prompt_save_point triggered")
        # If called via hotkey, snapshot frozen at press moment.
        # If called via tray menu, take current state.
        snap = self._save_snapshot if self._save_snapshot is not None else self.current_data
        self._save_snapshot = None  # consumed

        if snap.get("x") is None:
            self.tray_icon.showMessage(
                "No position",
                "Coordinates not yet detected by OCR.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        try:
            # QInputDialog instance to force always-on-top
            # (overlay parent has WindowTransparentForInput, default dialog
            # may appear unfocused behind overlay)
            dialog = QInputDialog(self)
            dialog.setWindowTitle("Save Point")
            dialog.setLabelText("Point of interest name:")
            dialog.setInputMode(QInputDialog.InputMode.TextInput)
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            dialog.setModal(True)
            QTimer.singleShot(0, dialog.raise_)
            QTimer.singleShot(0, dialog.activateWindow)
            if dialog.exec() != QInputDialog.DialogCode.Accepted:
                return
            name = dialog.textValue().strip()
            if not name:
                return

            ooc = snap.get("ooc")
            self.nav.add_user_point(
                name,
                snap["x"],
                snap["y"],
                snap["z"],
                snap.get("location", "Unknown"),
                ooc=ooc,
            )
            ooc_str = f" ({ooc})" if ooc else " (unknown zone)"
            self.tray_icon.showMessage(
                "Success",
                f"Point '{name}' saved{ooc_str}.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        except Exception:
            logger.exception("Error saving position")
            self.tray_icon.showMessage(
                "Error",
                "Could not save position. See spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def show_poi_selector(self):
        pois = self.nav.get_all_poi_for_location("All")
        if not pois:
            self.tray_icon.showMessage("Info", "No points registered.", QSystemTrayIcon.MessageIcon.Information)
            return

        menu = QMenu()
        for poi in pois:
            action = QAction(f"{poi['name']} ({poi.get('location', 'Unknown')})", self)
            action.triggered.connect(lambda checked, p=poi: self._on_destination_changed(p))
            menu.addAction(action)

        menu.exec(self.tray_icon.geometry().center())

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export points", "", "JSON Files (*.json)")
        if path:
            if self.nav.export_points(path):
                self.tray_icon.showMessage("Success", "Points exported.", QSystemTrayIcon.MessageIcon.Information)

    def import_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import points", "", "JSON Files (*.json)")
        if path:
            if self.nav.import_points(path):
                self.tray_icon.showMessage("Success", "Points imported.", QSystemTrayIcon.MessageIcon.Information)

    def quit_application(self):
        self.hotkey_listener.cleanup()
        self._worker.stop()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.toggle_overlay()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = GPSOverlay()
    overlay.show()
    sys.exit(app.exec())
