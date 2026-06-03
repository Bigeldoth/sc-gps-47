import sys
import os
import math
import time
import logging
import threading
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout,
                             QWidget, QFrame, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog,
                             QDialog, QHBoxLayout, QLineEdit, QPushButton,
                             QRadioButton, QButtonGroup, QComboBox, QMessageBox)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QAction, QColor, QCursor, QFontDatabase, QFont
from app_paths import user_data_dir, bundle_dir
from ui.widgets import SignalBarsWidget
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import (
    NavigationEngine,
    format_distance,
    calculate_velocity_bearing,
    calculate_absolute_bearing,
    format_axis_delta,
    ema_angle,
    _zones_match,
)
from config_manager import ConfigManager
from hotkey_listener import HotkeyListener
from velocity_tracker import VelocityTracker
from telemetry import create_session_recorder
from ui.options import OptionsWindow
from ui.poi_manager import POIManagerWindow
from poi_categories import POI_CATEGORIES
import poi_io

# Read config.ini from the same paths ConfigManager uses so the logging
# setup honors what the user changed in Options. Order matters: bundle is
# read first as fallback defaults, then user_data_dir overrides — that
# matches ConfigManager's source-of-truth precedence. Both paths resolve
# to the repo root in dev mode and to MEIPASS / %LOCALAPPDATA%\SpaceDrive
# in the installed bundle. Reading via a relative path ('config.ini')
# was unreliable because CWD differs between dev runs and the installed
# .exe launched from the start menu — in the installed case it silently
# fell back to defaults, ignoring the user's Options changes.
_bundle_config_path = os.path.join(str(bundle_dir()), 'config.ini')
_user_config_path = os.path.join(str(user_data_dir()), 'config.ini')
config = configparser.ConfigParser()
config.read([_bundle_config_path, _user_config_path], encoding='utf-8')

# Log file path: respect an absolute path explicitly set in config, otherwise
# write into the user data dir (writable from any user, survives reinstall).
# Writing relative to CWD breaks the installed build because CWD = Program
# Files\SpaceDrive\ which is read-only for non-admin processes.
_log_file_setting = config.get('Logging', 'file', fallback='spacedrive.log')
if os.path.isabs(_log_file_setting):
    _log_file_path = _log_file_setting
else:
    # All runtime logs live under user_data_dir()/logs/, alongside the
    # per-session telemetry-*.jsonl traces, for one coherent and readable
    # location (see CLAUDE.md "Logging & debug").
    _logs_dir = user_data_dir() / "logs"
    _logs_dir.mkdir(parents=True, exist_ok=True)
    _log_file_path = str(_logs_dir / _log_file_setting)

logging.basicConfig(
    level=getattr(logging, config.get('Logging', 'level', fallback='INFO')),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=_log_file_path,
    filemode='w',
)
logger = logging.getLogger(__name__)
logger.info("Log file: %s", _log_file_path)


# ── PADEK design system ──────────────────────────────────────────────────────

def _load_padek_fonts():
    """Load Manrope ExtraBold for PADEK titles and labels. Requires QApplication."""
    font_id = QFontDatabase.addApplicationFont(
        str(bundle_dir() / "assets" / "fonts" / "Manrope-ExtraBold.ttf")
    )
    if font_id >= 0:
        return QFontDatabase.applicationFontFamilies(font_id)[0]
    return "Manrope"


# Module-level fallback — overwritten after QApplication is created (see main()).
PADEK_DISPLAY_FONT = "Manrope"

# Freshness stops (age_s) -> PADEK border colors
_PADEK_BORDER_STOPS = [
    (0.0,  "#19C28A"),   # emerald -- LIVE
    (3.0,  "#D9A368"),   # copper  -- AGING
    (7.0,  "#FF7A45"),   # orange  -- STALE
    (12.0, "#E5484D"),   # danger  -- LOST
]
# Freshness stops (age_s) -> coordinate text colors
_PADEK_COORD_STOPS = [
    (0.0,  "#6FE8FF"),   # neon    -- LIVE
    (3.0,  "#D9A368"),   # copper  -- AGING
    (7.0,  "#FF7A45"),   # orange  -- STALE
    (12.0, "#E5484D"),   # danger  -- LOST
]


def _padek_freshness_level(age_s):
    """Return 0=LIVE / 1=AGING / 2=STALE / 3=LOST."""
    if age_s is None:
        return 3
    if age_s < 3.0:
        return 0
    if age_s < 7.0:
        return 1
    if age_s < 12.0:
        return 2
    return 3


def _padek_interp(stops, age_s):
    """Linear RGB interpolation between freshness stops."""
    if age_s is None or age_s >= stops[-1][0]:
        return stops[-1][1]
    if age_s <= stops[0][0]:
        return stops[0][1]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= age_s <= t1:
            def _hex(c):
                return [int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)]
            r0, g0, b0 = _hex(c0)
            r1, g1, b1 = _hex(c1)
            t = (age_s - t0) / (t1 - t0)
            r = int(r0 + t * (r1 - r0))
            g = int(g0 + t * (g1 - g0))
            b = int(b0 + t * (b1 - b0))
            return f"#{r:02x}{g:02x}{b:02x}"
    return stops[-1][1]


# ── End PADEK ────────────────────────────────────────────────────────────────


def _build_ocr_processor(cfg):
    """Reads the [OCR] section of `cfg` and returns a fresh OCRProcessor."""
    ocr_engine = cfg.get(
        'OCR', 'text_engine',
        fallback=cfg.get('OCR', 'engine', fallback='tesseract'),
    )
    glyph_engine = cfg.get('OCR', 'glyph_engine', fallback='ncc')
    pipeline_mode = cfg.get('OCR', 'pipeline_mode', fallback='hybrid')
    paddle_device = cfg.get('OCR', 'paddle_device', fallback='cpu')
    paddle_model_dir = cfg.get('OCR', 'paddle_model_dir', fallback='')
    paddle_lang = cfg.get('OCR', 'paddle_lang', fallback='en')
    try:
        paddle_min_confidence = float(cfg.get(
            'OCR', 'paddle_min_confidence', fallback='0.30'))
    except ValueError:
        paddle_min_confidence = 0.30
    tesseract_lang = (cfg.get('OCR', 'tesseract_lang', fallback='eng') or 'eng').strip()
    tesseract_tessdata_dir = (cfg.get(
        'OCR', 'tesseract_tessdata_dir', fallback='') or '').strip()
    ocr = OCRProcessor(
        engine=ocr_engine,
        glyph_engine=glyph_engine,
        onnx_model_path=cfg.get('OCR', 'onnx_model_path',
                                fallback='models/spacedrive_ocr.onnx'),
        onnx_classes_path=cfg.get('OCR', 'onnx_classes_path',
                                  fallback='models/spacedrive_ocr.classes.json'),
        onnx_confidence_threshold=float(cfg.get(
            'OCR', 'onnx_confidence_threshold', fallback='0.85')),
        pipeline_mode=pipeline_mode,
        paddle_device=paddle_device,
        paddle_model_dir=paddle_model_dir,
        paddle_lang=paddle_lang,
        paddle_min_confidence=paddle_min_confidence,
        tesseract_lang=tesseract_lang,
        tesseract_tessdata_dir=tesseract_tessdata_dir,
    )
    logger.info(
        "OCR engine initialized: text=%s glyphs=%s mode=%s device=%s",
        ocr_engine, glyph_engine, pipeline_mode, paddle_device,
    )
    return ocr


class GPSWorker(QObject):
    result_ready = pyqtSignal(dict)
    reload_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.capture = ScreenCapture()
        self.ocr = _build_ocr_processor(config)
        self._running = True
        # Cross-thread reload: the UI emits reload_requested after the user
        # saves Options; the slot runs inside the worker thread (queued
        # connection) so OCRProcessor (re)init does not block the UI.
        self.reload_requested.connect(self._reload_ocr)

    def process(self):
        if not self._running:
            return
        try:
            images, glyph_data, t_capture = self.capture.capture()
            data = self.ocr.extract_data(images)
            # Carry the capture instant alongside the OCR result so the UI
            # thread can derive velocity from measurement time, not handling time.
            data["t_capture"] = t_capture
            self.result_ready.emit(data)
        except Exception as e:
            # 0xC000013A (3221225786 = STATUS_CONTROL_C_EXIT) is the Tesseract
            # subprocess being signal-terminated mid-call. The payload is
            # just Tesseract dumping its ObjectCache leak warnings to stderr
            # — not an OCR failure. Demote to DEBUG to keep the log clean.
            msg = str(e)
            if "3221225786" in msg or "ObjectCache" in msg:
                logger.debug("Tesseract subprocess interrupted (cleanup noise): %s", e)
            else:
                logger.error(f"GPS worker error: {e}")
            self.result_ready.emit({"x": None, "y": None, "z": None, "location": "Unknown", "error": str(e), "t_capture": time.monotonic()})

    def _reload_ocr(self):
        """Rebuilds the OCR processor from the on-disk config.ini.

        Triggered by the `reload_requested` signal so it runs inside the
        worker thread, off the UI thread.
        """
        try:
            # Re-read both paths (bundle defaults + user override), same
            # precedence as the initial boot read above.
            config.read([_bundle_config_path, _user_config_path], encoding='utf-8')
            old = self.ocr
            new_ocr = _build_ocr_processor(config)
            self.ocr = new_ocr
            try:
                old.shutdown()
            except Exception as exc:
                logger.warning("Old OCR shutdown failed: %s", exc)
            logger.info("OCR processor reloaded after Options save")
        except Exception as exc:
            logger.error("OCR reload failed: %s", exc)

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

# A4 graded pitch: below this magnitude the vertical offset is treated as
# aligned and no ▲/▼ indicator is shown. Lower than the old binary 25°
# threshold because the indicator now carries the angle itself.
_PITCH_DEADBAND_DEG = 10.0


def _pitch_indicator(pitch_deg):
    """Graded vertical indicator for the nav arrow (A4).

    ``pitch_deg`` — signed degrees, positive = target/correction is *up*.
    Only meaningful for space POIs; callers pass ``None`` (or a 0 produced by
    ``_effective_dz``) for surface POIs, where altitude is ignored.

    Returns ``""`` within ``_PITCH_DEADBAND_DEG``, else a graded indicator with
    the magnitude rounded to whole degrees: ``▲N°`` (climb) / ``▼N°`` (dive).
    """
    if pitch_deg is None or abs(pitch_deg) < _PITCH_DEADBAND_DEG:
        return ""
    if pitch_deg > 0:
        return f"▲{pitch_deg:.0f}°"
    return f"▼{abs(pitch_deg):.0f}°"


def _world_arrow(abs_bearing):
    """8-direction navigation arrow to target (SC frame corrected).

    SC (OOC) Convention: Y+ = forward (↑), Y- = backward (↓),
    X- = right (→), X+ = left (←). X correction is already
    applied in calculate_absolute_bearing (yaw_deg uses -dx).

    Returns 'horizontal_arrow + graded vertical indicator' (``▲N°`` / ``▼N°``,
    see _pitch_indicator). Surface POIs have pitch 0 (Z ignored) so no vertical
    indicator is appended.

    Shown when the ship is stationary (no velocity vector available).
    """
    if not abs_bearing:
        return ""
    yaw_norm = (abs_bearing["yaw_deg"] + 360.0) % 360.0
    arrow_h = _ARROWS_8[int((yaw_norm + 22.5) / 45) % 8]
    return arrow_h + _pitch_indicator(abs_bearing["pitch_deg"])


_HEADING_FLIP_DEG = 60.0    # tick-to-tick velocity-heading swing above this is
                            # jitter/jockeying, not a smooth turn
_HEADING_RECOVER_S = 1.5    # show the stable world bearing for this long after


def _velocity_arrow(yaw_off, pitch_off=None):
    """Velocity-relative guidance arrow (car-GPS style).

    ``yaw_off``   — signed degrees: positive = target is to the right of the
                    current movement direction; negative = to the left.
    ``pitch_off`` — signed degrees or None; ignored for surface POIs.

    Returns a compact string showing the correction needed:
      - ``"↑"``      already heading toward the target (< 20°)
      - ``"→15°"``   turn right 15°
      - ``"←8°"``    turn left 8°
      - ``"↓"``      target is behind (> 150°) — U-turn needed
    A graded vertical indicator (``▲N°`` / ``▼N°``, see _pitch_indicator) is
    appended for space POIs; surface POIs pass ``pitch_off=None``.
    """
    parts = []
    abs_off = abs(yaw_off)
    if abs_off < 20.0:
        parts.append("↑")
    elif abs_off > 150.0:
        parts.append("↓")
    elif yaw_off > 0:
        parts.append(f"→{yaw_off:.0f}°")
    else:
        parts.append(f"←{abs_off:.0f}°")

    vert = _pitch_indicator(pitch_off)
    if vert:
        parts.append(vert)

    return " ".join(parts)


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


class _SavePOIDialog(QDialog):
    """Modal dialog prompting for a POI name, kind, category, and description.

    Surface kind defaults checked: most save-point hotkey presses happen
    while flying around a planet/moon, and surface POIs need horizontal-only
    distance to avoid altitude-inflated readings.
    """

    def __init__(self, freshness_level: int = 0, parent=None):
        super().__init__(parent)
        self._freshness_level = freshness_level
        self.setWindowTitle("Save Position")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(440)

        border_colors = ["#19C28A", "#D9A368", "#FF7A45", "#E5484D"]
        border = border_colors[min(freshness_level, 3)]
        self.setStyleSheet(f"""
            QDialog {{
                background-color: #161B22;
                border: 1px solid {border};
                border-radius: 16px;
            }}
        """)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("SAVE POSITION")
        title.setFont(QFont(PADEK_DISPLAY_FONT, 11, QFont.Weight.ExtraBold))
        title.setStyleSheet("color: #EEF3F6; background: transparent;")
        layout.addWidget(title)

        layout.addWidget(QLabel("Point of interest name:"))
        self.name_input = QLineEdit()
        layout.addWidget(self.name_input)

        # Toggle Surface / Space (pill buttons)
        type_widget = QWidget()
        type_layout = QHBoxLayout(type_widget)
        type_layout.setSpacing(8)
        type_layout.setContentsMargins(0, 0, 0, 0)

        self.surface_btn = QPushButton("Surface (planet / moon)")
        self.surface_btn.setCheckable(True)
        self.surface_btn.setChecked(True)
        self.space_btn = QPushButton("Space (3D)")
        self.space_btn.setCheckable(True)

        for btn in (self.surface_btn, self.space_btn):
            btn.setFont(QFont(PADEK_DISPLAY_FONT, 8, QFont.Weight.ExtraBold))
            type_layout.addWidget(btn)

        self._type_group = QButtonGroup(self)
        self._type_group.addButton(self.surface_btn)
        self._type_group.addButton(self.space_btn)
        self._type_group.buttonClicked.connect(self._update_type_styles)
        self._update_type_styles(self.surface_btn)
        layout.addWidget(type_widget)

        layout.addWidget(QLabel("Category:"))
        self.category_combo = QComboBox()
        for slug, label in POI_CATEGORIES:
            self.category_combo.addItem(label, slug)
        layout.addWidget(self.category_combo)

        layout.addWidget(QLabel("Description (optional):"))
        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("Short note…")
        layout.addWidget(self.desc_input)

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)
        ok_btn = QPushButton("SAVE")
        ok_btn.setObjectName("btn_primary")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        button_row.addWidget(ok_btn)
        layout.addLayout(button_row)

        self.setLayout(layout)
        self.name_input.setFocus()

    def _update_type_styles(self, checked_btn):
        for btn in (self.surface_btn, self.space_btn):
            active = (btn is checked_btn)
            btn.setStyleSheet("""
                QPushButton {
                    background: rgba(25,194,138,0.12); color: #19C28A;
                    border: 1.5px solid rgba(25,194,138,0.5); border-radius: 999px;
                    padding: 8px 10px;
                    font-family: "Manrope"; font-weight: 800; font-size: 9px;
                }
            """ if active else """
                QPushButton {
                    background: transparent; color: #7E8B97;
                    border: 1px solid rgba(238,243,246,0.12); border-radius: 999px;
                    padding: 8px 10px;
                    font-family: "Manrope"; font-weight: 800; font-size: 9px;
                }
                QPushButton:hover { color: #B6C3CE; border-color: rgba(238,243,246,0.2); }
            """)

    # Compatibility: surface_radio/space_radio properties for existing callers
    @property
    def surface_radio(self):
        return self.surface_btn

    @property
    def space_radio(self):
        return self.space_btn

    def get_name(self):
        return self.name_input.text().strip()

    def get_kind(self):
        return "surface" if self.surface_btn.isChecked() else "space"

    def get_category(self):
        return self.category_combo.currentData() or ""

    def get_description(self):
        return self.desc_input.text().strip()


class GPSOverlay(QMainWindow):
    save_point_signal = pyqtSignal()
    trigger_worker = pyqtSignal()
    poi_data_updated = pyqtSignal(dict)   # emitted every OCR cycle with current coords

    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.current_data = {
            "x": None, "y": None, "z": None,
            "ooc": None, "location": "Unknown",
        }
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
        # We sample position and derive movement direction. The Kalman tuning
        # knobs come from config.ini ([Kalman]) so they can be adjusted from the
        # Options dialog without editing source.
        self._velocity_tracker = self._build_velocity_tracker()
        self._last_known_ooc = None  # to detect frame change
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        # Velocity-heading confidence: the last heading and a cooldown timestamp.
        # While the heading is jumpy (jockeying near the target) the overlay
        # shows the stable world bearing instead of a flipping turn arrow.
        self._last_vel_heading = None
        self._heading_unstable_until = 0.0

        # Optional per-tick navigation telemetry (off by default). Records a
        # JSONL trace of positions, dropouts and rejects for offline replay /
        # filter tuning. See src/telemetry.py and [Debug] record_telemetry.
        self._telemetry = create_session_recorder(
            self.config_manager.get_record_telemetry(),
            user_data_dir() / "logs",
        )

        # Snapshot of coordinates frozen at hotkey save press moment.
        # Preserves value even if OCR fails between press and dialog confirmation.
        # None = no save in progress via hotkey.
        self._save_snapshot = None

        # Rolling buffer of the last accepted positions (after velocity check).
        # The smoothed position used for distance/bearing is the per-axis
        # MEDIAN over this buffer — kills isolated 8↔6 or 0↔6 misreads on a
        # single decimal without affecting legitimate motion (median tracks
        # the cluster, ignores 1-2 outliers). 7 samples ≈ 1.4 s at 200 ms.
        self._pos_buffer = []  # list[tuple[x, y, z]]
        self._POS_BUFFER_SIZE = 7

        # Temporary message displayed in nav_label: (text, expire_monotonic_ts).
        self._overlay_message = None


        # UI — PADEK MFD frame
        self.central_widget = QWidget()
        self.central_widget.setStyleSheet("background-color: transparent;")

        outer = QVBoxLayout(self.central_widget)
        outer.setContentsMargins(0, 0, 0, 0)

        self._mfd = QFrame()
        self._mfd.setObjectName("mfd_frame")

        inner = QVBoxLayout(self._mfd)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(26)
        header.setStyleSheet("background: transparent;")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(10, 5, 10, 5)
        h_layout.setSpacing(6)

        self._title_label = QLabel("SPACEDRIVE GPS")
        self._title_label.setFont(QFont(PADEK_DISPLAY_FONT, 7, QFont.Weight.ExtraBold))
        self._title_label.setStyleSheet("color: #7E8B97; background: transparent;")

        self._signal_bars = SignalBarsWidget()

        self._live_dot = QLabel("●")
        self._live_dot.setFont(QFont("Roboto", 7))
        self._live_dot.setFixedWidth(10)

        self._live_label = QLabel("LIVE")
        self._live_label.setFont(QFont(PADEK_DISPLAY_FONT, 7, QFont.Weight.ExtraBold))

        h_layout.addWidget(self._title_label)
        h_layout.addStretch()
        h_layout.addWidget(self._signal_bars)
        h_layout.addWidget(self._live_dot)
        h_layout.addWidget(self._live_label)

        inner.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(238,243,246,18); border: none; max-height: 1px;")
        inner.addWidget(sep)

        # Coordinates
        self.pos_label = QLabel("Scanning…")
        self.pos_label.setFont(QFont("Consolas", 10))
        self.pos_label.setContentsMargins(10, 7, 10, 5)
        self.pos_label.setStyleSheet("color: #6FE8FF; background: transparent;")
        inner.addWidget(self.pos_label)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background: rgba(238,243,246,12); border: none; max-height: 1px;")
        inner.addWidget(sep2)

        # Navigation
        self.nav_label = QLabel("NO TARGET")
        self.nav_label.setFont(QFont(PADEK_DISPLAY_FONT, 9, QFont.Weight.Bold))
        self.nav_label.setContentsMargins(10, 6, 10, 9)
        self.nav_label.setStyleSheet("color: #4F5965; background: transparent;")
        inner.addWidget(self.nav_label)

        outer.addWidget(self._mfd)
        self.setCentralWidget(self.central_widget)

        # Dot pulse timer
        self._dot_timer = QTimer()
        self._dot_timer.timeout.connect(self._tick_dot)
        self._dot_timer.start(600)
        self._dot_opacity = True

        self.init_window_properties()
        self.setup_tray_icon()
        self._setup_worker_thread()

        # Surface an engine fallback (e.g. Paddle selected but its sidecar isn't
        # installed) so the user isn't silently left on Tesseract. Deferred so
        # the tray icon and overlay are ready.
        QTimer.singleShot(1500, self._check_engine_fallback)

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
        self.hotkey_listener.reset_gps_nav_triggered.connect(
            self.reset_velocity_tracker, Qt.ConnectionType.QueuedConnection
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

    def _check_engine_fallback(self):
        """Warn (tray + overlay) when the configured OCR text engine fell back
        to another — e.g. Paddle selected but its sidecar venv isn't installed,
        so the app is silently running on Tesseract."""
        ocr = getattr(self._worker, "ocr", None)
        reason = getattr(ocr, "fallback_reason", None) if ocr is not None else None
        if not reason:
            return
        logger.warning("OCR engine fallback surfaced to user: %s", reason)
        self._show_overlay_message(reason, duration_s=8.0)
        try:
            self.tray_icon.showMessage(
                "OCR engine fallback", reason,
                QSystemTrayIcon.MessageIcon.Warning, 6000,
            )
        except Exception:
            pass

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

        # Measurement time stamped at capture (see ScreenCapture.capture).
        # Fed to the velocity tracker so dt reflects the frame interval, not the
        # variable OCR-handling latency on this thread.
        t_capture = data.get("t_capture")

        if "error" in data:
            # Don't touch displayed text — it will age via color.
            self._coast_guidance(t_capture)
            self._record_telemetry(data, "no_read", reason="ocr_error")
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
                self._coast_guidance(t_capture)
                self._record_telemetry(data, "rejected", reason="implausible_velocity")
                self._refresh_pos_color()
                self._refresh_nav_label()
                return

            # Raw OCR position (pre-median) — the input an offline filter needs.
            raw_pos = (data["x"], data["y"], data["z"])

            # Apply per-axis median smoothing over the last N accepted reads.
            # Resets on OOC change (legitimate teleport — buffer is stale).
            if (
                self.current_data.get("ooc")
                and data.get("ooc")
                and not _zones_match(data["ooc"], self.current_data["ooc"])
            ):
                self._pos_buffer.clear()
            self._pos_buffer.append((data["x"], data["y"], data["z"]))
            if len(self._pos_buffer) > self._POS_BUFFER_SIZE:
                self._pos_buffer.pop(0)
            sx = sorted(p[0] for p in self._pos_buffer)
            sy = sorted(p[1] for p in self._pos_buffer)
            sz = sorted(p[2] for p in self._pos_buffer)
            mid = len(self._pos_buffer) // 2
            smoothed = dict(data)
            smoothed["x"] = sx[mid]
            smoothed["y"] = sy[mid]
            smoothed["z"] = sz[mid]
            data = smoothed

            self.current_data = data
            self.pos_label.setText(
                f"X: {data['x']:>10.2f}   Y: {data['y']:>10.2f}\n"
                f"Z: {data['z']:>10.2f}"
            )
            self._last_coord_ts = time.monotonic()
            self.poi_data_updated.emit(data)

            # Updates target zone tracking (lock after 1st match,
            # grace period 3 min otherwise — see NavigationEngine).
            self.nav.update_zone_tracking(data)

            self._update_bearing_state(data, t_capture)

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

            self._record_telemetry(data, "ok", raw_pos=raw_pos)
        else:
            # OCR ran but parsed no coordinates (flare / scenery occlusion /
            # partial HUD): a genuine dropout — dead-reckon, then record it.
            self._coast_guidance(t_capture)
            self._record_telemetry(data, "no_read", reason="no_coords")

        # Always refresh color (aging), even without new data.
        self._refresh_pos_color()
        self._refresh_nav_label()

    def _record_telemetry(self, data, status, reason=None, raw_pos=None):
        """Append one navigation telemetry record (no-op when disabled).

        ``status`` is one of ``"ok"`` (accepted read), ``"rejected"`` (misread
        caught by the plausibility gate) or ``"no_read"`` (OCR dropout — flare
        / occlusion / error). ``raw_pos`` is the OCR ``(x, y, z)`` *before*
        median smoothing — the raw input an offline filter must replay.
        """
        rec = self._telemetry
        if rec is None:
            return
        tgt = self.nav.target
        if raw_pos is not None:
            rx, ry, rz = raw_pos
        else:
            rx, ry, rz = data.get("x"), data.get("y"), data.get("z")
        rec.record(
            t_capture=data.get("t_capture"),
            status=status,
            reason=reason,
            x=rx, y=ry, z=rz,
            ooc=data.get("ooc"),
            location=data.get("location"),
            target=(tgt.get("name") if tgt else None),
            # Guidance fields are only meaningful with an active target — log
            # null otherwise so a stale smoothed value never pollutes the trace.
            yaw_off=(self._smoothed_yaw_off if tgt else None),
            pitch_off=(self._smoothed_pitch_off if tgt else None),
            dist_km=(self._smoothed_distance_km if tgt else None),
            speed_km_s=round(self._velocity_tracker.speed_km_s, 6),
            integrity=self._velocity_tracker.integrity,
            coord_age_s=self._coord_age_s(),
        )

    def _apply_telemetry_setting(self):
        """Enable/disable the telemetry recorder to match config, live.

        Lets the Options → Debug toggle take effect without an app restart:
        opens a fresh session file when turned on, closes it when turned off.
        """
        want = self.config_manager.get_record_telemetry()
        have = self._telemetry is not None
        if want and not have:
            self._telemetry = create_session_recorder(True, user_data_dir() / "logs")
        elif not want and have:
            self._telemetry.close()
            self._telemetry = None

    def _coord_age_s(self):
        """Age (s) of last valid OCR, or None if none yet."""
        if self._last_coord_ts is None:
            return None
        return time.monotonic() - self._last_coord_ts

    # Speed caps between two scans (km/s). SC outside-atmosphere top speed
    # is **1.4 km/s per axis**. The cap is intentionally loose (7× the
    # physical max) so we let through the common 8↔6 unit-digit OCR
    # confusion (≈2 km swing → 10 km/s at 200 ms scan) — the rolling-median
    # buffer (F4, last 7 samples) absorbs those isolated outliers without
    # us having to reject the frame. The cap still catches:
    #   • 0↔6 hundreds digit (60 km swing → 300 km/s)
    #   • Catastrophic digit drops / extra digits (≥ 100 km)
    #   • Sign drops not already caught by the sign-flip mirror gate
    # 3D-norm cap stays ~1.5× the per-axis cap for diagonal headroom.
    # Quantum jumps trigger an OOC change which bypasses these gates.
    _MAX_PLAUSIBLE_SPEED_KM_S = 10.0
    _MAX_PLAUSIBLE_3D_SPEED_KM_S = 15.0
    # Absolute per-axis delta cap regardless of scan interval. A jump of
    # ≥ 4 km on a single axis without an OOC change is always an OCR error
    # (e.g. '6'↔'0' confusion in the tens digit: 16.xx → 10.xx = 6 km).
    # Legitimate max-speed flight at 1.4 km/s for 3 s = 4.2 km, so the cap
    # is set at 4 km — just below that and well above the 2 km 8↔6 artifact.
    _MAX_PLAUSIBLE_AXIS_DELTA_KM = 4.0
    # Tightest gap allowed when a sign flips on one axis. A '-748.27' read
    # as '748.27' produces |new + cur| = 0; we treat anything ≤ 5 km of
    # mirror-equality as a near-certain digit-1 sign drop and reject without
    # even needing the velocity gate.
    _SIGN_FLIP_MIRROR_TOL_KM = 5.0

    def _is_velocity_implausible(self, new_data):
        """True if new position implies a physically impossible jump.

        Three independent gates (any one of which triggers rejection):
          1. **Sign-flip mirror** — a single axis crosses zero while the
             reflected magnitude matches the previous reading to within
             ``_SIGN_FLIP_MIRROR_TOL_KM``. This catches the typical
             ``-748.27 → 748.27`` sign-drop without needing dt.
          2. **Per-axis speed cap** — each of dx/dy/dz divided by dt must
             stay under ``_MAX_PLAUSIBLE_SPEED_KM_S``. Rejecting per-axis
             prevents a 60 km jump on Y (the classic ``-103 → -163`` 0↔6
             confusion in the hundreds digit) from being averaged out by
             two quiet axes the way the 3D norm would.
          3. **3D-norm speed cap** — kept as a backstop for legitimate
             diagonal moves that pass per-axis but accumulate fast.

        Tolerates:
          - first scan (no reference) → False;
          - OOC change (legitimate teleport via QT) → False;
          - long time gap (> 5 s, we may have missed a jump) → False.
        """
        if self.current_data.get("x") is None or self._last_coord_ts is None:
            return False
        # OOC change: don't compare, frame changed.
        new_ooc = new_data.get("ooc")
        cur_ooc = self.current_data.get("ooc")
        if new_ooc and cur_ooc and not _zones_match(new_ooc, cur_ooc):
            return False
        dt = time.monotonic() - self._last_coord_ts
        if dt <= 0 or dt > 5.0:
            return False

        cur_x, cur_y, cur_z = (
            self.current_data["x"], self.current_data["y"], self.current_data["z"],
        )
        new_x, new_y, new_z = new_data["x"], new_data["y"], new_data["z"]

        # Gate 1 — sign-flip mirror on a single axis.
        flip_tol = self._SIGN_FLIP_MIRROR_TOL_KM
        for axis, cur, new in (("X", cur_x, new_x), ("Y", cur_y, new_y), ("Z", cur_z, new_z)):
            if (cur * new) < 0 and abs(abs(cur) - abs(new)) < flip_tol:
                logger.warning(
                    f"OCR rejection: sign-flip on {axis} "
                    f"({cur:.4f} → {new:.4f}), mirror gap {abs(abs(cur)-abs(new)):.4f} km"
                )
                return True

        # Gate 2a — absolute per-axis delta cap (dt-independent).
        # Catches the '6'↔'0' tens-digit confusion (16.xx → 10.xx = 6 km)
        # when the scan gap is long enough that the speed gate alone misses it.
        abs_delta_cap = self._MAX_PLAUSIBLE_AXIS_DELTA_KM
        for axis, cur, new in (("X", cur_x, new_x), ("Y", cur_y, new_y), ("Z", cur_z, new_z)):
            delta = abs(new - cur)
            if delta > abs_delta_cap:
                logger.warning(
                    f"OCR rejection: absolute {axis}-axis jump {delta:.2f} km "
                    f"({cur:.4f} → {new:.4f}) exceeds {abs_delta_cap} km cap"
                )
                return True

        # Gate 2b — per-axis speed cap.
        per_axis_cap = self._MAX_PLAUSIBLE_SPEED_KM_S
        for axis, cur, new in (("X", cur_x, new_x), ("Y", cur_y, new_y), ("Z", cur_z, new_z)):
            axis_speed = abs(new - cur) / dt
            if axis_speed > per_axis_cap:
                logger.warning(
                    f"OCR rejection: implausible {axis}-axis speed {axis_speed:.1f} km/s "
                    f"(Δ{axis}={new - cur:+.2f} km in {dt:.2f} s)"
                )
                return True

        # Gate 3 — 3D-norm speed cap (slightly higher than per-axis to
        # accommodate sqrt(3)× the per-axis maximum on diagonal travel).
        dx, dy, dz = new_x - cur_x, new_y - cur_y, new_z - cur_z
        dist_km = (dx * dx + dy * dy + dz * dz) ** 0.5
        speed = dist_km / dt
        if speed > self._MAX_PLAUSIBLE_3D_SPEED_KM_S:
            logger.warning(
                f"OCR rejection: implausible 3D speed {speed:.1f} km/s "
                f"(Δ={dist_km:.2f} km in {dt:.2f} s)"
            )
            return True
        return False

    # Max age (s) of the last real OCR read for which we still extrapolate
    # the displayed position from the smoothed velocity vector. Past this,
    # the velocity estimate becomes stale (turns, decelerations) so we
    # freeze the label and let it age into red.
    _EXTRAPOLATION_MAX_AGE_S = 3.0

    def _tick_visual_refresh(self):
        """Recolors pos_label and nav_label without triggering OCR.

        Decoupled from worker so green→orange→red transition remains
        smooth independent of cadence or OCR failures.

        While OCR is between successful reads (age 0.3-3 s), we
        extrapolate the displayed position from the last known coords +
        the smoothed velocity vector. The user sees a smoothly ticking
        readout even when OCR drops a frame, and the colour still ages
        toward red so the data freshness signal isn't lost.
        ``current_data`` is intentionally NOT updated — only the visible
        label — so the next real read's velocity calculation remains
        anchored to the last true position.
        """
        self._refresh_pos_color()
        self._refresh_nav_label()
        self._extrapolate_position_label()

    def _extrapolate_position_label(self):
        """Fill the gap between successful OCR reads with velocity-based
        extrapolation. See ``_tick_visual_refresh``."""
        if self.current_data.get("x") is None or self._last_coord_ts is None:
            return
        age = time.monotonic() - self._last_coord_ts
        if age < 0.3 or age > self._EXTRAPOLATION_MAX_AGE_S:
            return
        if not self._velocity_tracker.is_moving:
            return
        v = self._velocity_tracker.velocity
        if v is None:
            return
        vx, vy, vz = v
        ext_x = self.current_data["x"] + vx * age
        ext_y = self.current_data["y"] + vy * age
        ext_z = self.current_data["z"] + vz * age
        self.pos_label.setText(
            f"X: {ext_x:>10.2f}   Y: {ext_y:>10.2f}\n"
            f"Z: {ext_z:>10.2f}"
        )

    def _refresh_pos_color(self):
        """Updates MFD border, signal bars, dot, and coordinate color based on OCR age."""
        age = self._coord_age_s()
        level = _padek_freshness_level(age)

        age_val = age if age is not None else 999
        border_color = _padek_interp(_PADEK_BORDER_STOPS, age_val)
        coord_color  = _padek_interp(_PADEK_COORD_STOPS,  age_val)
        label_texts  = ["LIVE", "AGING", "STALE", "LOST"]
        label_text   = label_texts[level]

        if age is None:
            text = self.pos_label.text()
            if not text or text in ("Scanning...", "Scanning…"):
                self.pos_label.setText("Scanning…")

        # MFD border color
        self._mfd.setStyleSheet(f"""
            QFrame#mfd_frame {{
                background-color: rgba(14, 18, 22, 235);
                border: 1px solid {border_color};
                border-radius: 6px;
            }}
        """)

        # Signal bars
        self._signal_bars.set_level(level)

        # LIVE/AGING/STALE/LOST label
        self._live_dot.setStyleSheet(f"color: {border_color}; background: transparent;")
        self._live_label.setStyleSheet(
            f"color: {border_color}; background: transparent; "
            f"font-family: '{PADEK_DISPLAY_FONT}'; font-weight: 800; font-size: 7pt;"
        )
        self._live_label.setText(label_text)

        # Coordinate text color
        self.pos_label.setStyleSheet(
            f"color: {coord_color}; background: transparent; "
            "font-family: 'Consolas'; font-size: 10pt;"
        )

    def _tick_dot(self):
        """Pulses the live dot opacity when signal is LIVE."""
        age = self._coord_age_s()
        if age is not None and age < 3.0:
            col = "#19C28A"
            opacity = "1.0" if self._dot_opacity else "0.4"
            self._live_dot.setStyleSheet(
                f"color: {col}; background: transparent; opacity: {opacity};"
            )
            self._dot_opacity = not self._dot_opacity

    def _update_bearing_state(self, data, t_capture=None):
        """Updates velocity and bearing offsets from OCR coordinates.

        ``t_capture`` is the monotonic timestamp of the frame grab. When
        available it is used as the velocity sample time so dt matches the
        capture interval; otherwise we fall back to the handling time.
        """
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
                data["x"], data["y"], data["z"],
                t_capture if t_capture is not None else time.monotonic(),
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

        # Require meaningful horizontal speed before trusting yaw direction.
        # Total 3D speed can exceed is_moving threshold while flying nearly
        # vertically — horizontal components are then OCR jitter, not heading.
        vx, vy, _ = velocity
        vel_horiz = math.hypot(vx, vy)
        if vel_horiz < self._velocity_tracker.MIN_SPEED_KM_S:
            self._smoothed_yaw_off = None
            self._smoothed_pitch_off = None
            self._last_raw_yaw_off = None
            self._last_raw_pitch_off = None
            return

        bearing = calculate_velocity_bearing(velocity, data, self.nav.target)
        if bearing is None:
            return
        yaw_off, pitch_off = bearing

        # Track velocity-heading stability: a large tick-to-tick swing means the
        # player is jockeying / the heading is jittery, so flag a cooldown during
        # which _refresh_nav_label shows the stable world bearing instead.
        vel_heading = math.degrees(math.atan2(-vx, vy))
        if self._last_vel_heading is not None:
            dh = abs(((vel_heading - self._last_vel_heading + 180.0) % 360.0) - 180.0)
            if dh > _HEADING_FLIP_DEG:
                self._heading_unstable_until = time.monotonic() + _HEADING_RECOVER_S
        self._last_vel_heading = vel_heading

        # VelocityTracker already applies EMA to the velocity vector itself,
        # so yaw_off is already smoothed. A second EMA here adds 1-2 s of lag
        # when changing direction and was causing stale arrows (e.g. ←112° when
        # already flying straight). Assign directly.
        self._smoothed_yaw_off = yaw_off
        self._smoothed_pitch_off = pitch_off
        self._last_raw_yaw_off = yaw_off
        self._last_raw_pitch_off = pitch_off

    def _velocity_heading_confident(self):
        """True when the velocity heading is steady enough to drive the turn
        arrow. After a large heading swing (jockeying / jitter) we stay
        un-confident for HEADING_RECOVER_S and show the stable world bearing."""
        return time.monotonic() >= self._heading_unstable_until

    def _coast_guidance(self, t_capture):
        """Dead-reckon guidance for a tick with no accepted OCR fix.

        Advances the Kalman filter with no measurement (predict-only) and, while
        still within the coast window, refreshes distance + the velocity arrow
        from the predicted position so guidance survives a brief OCR dropout
        (flare / scenery occlusion). Beyond the coast window the arrow is dropped.
        """
        vt = self._velocity_tracker
        if t_capture is not None:
            vt.predict_only(t_capture)
        if not self.nav.target:
            return
        if vt.integrity == "LOST":
            # Too stale to dead-reckon: drop the arrow (colour aging + the LOST
            # tag already signal the loss); leave the last distance frozen.
            self._smoothed_yaw_off = None
            self._smoothed_pitch_off = None
            return
        pos = vt.position
        if pos is None:
            return
        pos_dict = {
            "x": pos[0], "y": pos[1], "z": pos[2],
            "ooc": self.current_data.get("ooc"),
        }
        dist = self.nav.calculate_distance(pos_dict)
        if dist is not None:
            self._smoothed_distance_km = dist
        vel = vt.velocity if vt.is_moving else None
        if vel is not None:
            bearing = calculate_velocity_bearing(vel, pos_dict, self.nav.target)
            if bearing is not None:
                self._smoothed_yaw_off, self._smoothed_pitch_off = bearing

    def _refresh_nav_label(self):
        """Updates nav_label: target, distance and heading merged."""
        _font_css = (
            f"font-family: '{PADEK_DISPLAY_FONT}'; font-weight: 800; "
            "background: transparent;"
        )

        # Ephemeral message takes priority (e.g., "Quick save not possible").
        if self._overlay_message is not None:
            text, expire_ts = self._overlay_message
            if time.monotonic() < expire_ts:
                self.nav_label.setText(text)
                self.nav_label.setStyleSheet(
                    f"color: #E5484D; font-size: 9pt; {_font_css}"
                )
                return
            self._overlay_message = None

        if not self.nav.target:
            self.nav_label.setText("NO TARGET")
            self.nav_label.setStyleSheet(f"color: #4F5965; font-size: 9pt; {_font_css}")
            return

        target_ooc = self.nav.target.get("ooc")
        current_ooc = self.current_data.get("ooc")

        if target_ooc is None:
            self.nav_label.setText(f"▶ {self.nav.target['name']}\n  Legacy POI — recreate")
            self.nav_label.setStyleSheet(f"color: #D9A368; font-size: 9pt; {_font_css}")
            return

        if not self.nav.is_target_in_same_ooc(self.current_data):
            self.nav_label.setText(
                f"▶ {self.nav.target['name']}\n"
                f"  zone: {_fmt_ooc(target_ooc)}\n"
                f"  current: {_fmt_ooc(current_ooc) if current_ooc else '?'}"
            )
            self.nav_label.setStyleSheet(f"color: #D9A368; font-size: 9pt; {_font_css}")
            return

        name = self.nav.target['name']

        if self._smoothed_distance_km is None:
            self.nav_label.setText(f"▶ {name}\n  ---")
            self.nav_label.setStyleSheet(f"color: #4F5965; font-size: 9pt; {_font_css}")
            return

        age = self._coord_age_s()
        age_val = age if age is not None else 999
        nav_color = _padek_interp(_PADEK_BORDER_STOPS, age_val)

        dist_str = format_distance(self._smoothed_distance_km)

        # Terminal logic (A5): within the arrival radius the line-of-sight angle
        # becomes unstable (it swings wildly as range -> 0), so suppress the
        # directional arrow and show a terminal marker. Distance and freshness
        # colour still update; the DR/LOST tag below is kept.
        within_arrival = (self._smoothed_distance_km * 1000.0) < self._arrival_radius_m

        arrow_str = ""
        if within_arrival:
            arrow_str = "  ●"
        elif self._smoothed_yaw_off is not None and self._velocity_heading_confident():
            is_surface = self.nav.target.get("kind") == "surface"
            pitch_arg = None if is_surface else self._smoothed_pitch_off
            arrow = _velocity_arrow(self._smoothed_yaw_off, pitch_arg)
            arrow_str = f"  {arrow}"
            logger.debug(
                "Velocity arrow: yaw_off=%.1f° pitch_off=%s → '%s'",
                self._smoothed_yaw_off,
                f"{self._smoothed_pitch_off:.1f}°" if self._smoothed_pitch_off is not None else "N/A",
                arrow,
            )
        else:
            abs_bearing = calculate_absolute_bearing(self.current_data, self.nav.target)
            if abs_bearing:
                arrow = _world_arrow(abs_bearing)
                if arrow:
                    arrow_str = f"  {arrow}"

        # Dead-reckoning annunciation: tag the readout while coasting through an
        # OCR dropout (FRESH shows nothing; LOST once the coast window expired).
        coast_age = self._velocity_tracker.coast_age_s
        if coast_age is not None and coast_age > 0.05:
            arrow_str += "  DR" if self._velocity_tracker.integrity == "COASTING" else "  LOST"

        self.nav_label.setText(f"▶ {name}\n  {dist_str}{arrow_str}")
        self.nav_label.setStyleSheet(f"color: {nav_color}; font-size: 9pt; {_font_css}")

    def init_window_properties(self):
        flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 340, 155)
        self.central_widget.setStyleSheet("background-color: transparent; border: none;")

        if self.isVisible():
            self.show()

    def _reassert_on_top(self):
        """Re-raise the overlay so it stays above SC's borderless window.

        WindowStaysOnTopHint is honoured by Qt but Star Citizen's borderless
        fullscreen window keeps pushing other top-level widgets behind itself
        on focus. Qt's ``raise_()`` alone does not survive — on Windows we
        must call ``SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)`` directly
        through Win32 to reassert true "always on top" status without
        stealing input focus (the overlay is WindowTransparentForInput).

        Skip while any of our own modal/dialog windows is active: raising the
        overlay would defocus combobox popups and similar transient widgets,
        causing them to close before the user can click.
        """
        if not self.is_visible:
            return
        from PyQt6.QtWidgets import QApplication
        active = QApplication.activeWindow()
        if active is not None and active is not self:
            return
        self.raise_()
        if sys.platform == "win32":
            try:
                import ctypes
                # HWND_TOPMOST = -1
                # flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                ctypes.windll.user32.SetWindowPos(
                    int(self.winId()), -1, 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010,
                )
            except Exception:
                pass  # best-effort; Qt raise_() above already ran

    def _load_app_icon(self):
        """Loads SpaceDrive icon from assets/, or system fallback.

        Looks in order:
          1. assets/spacedrive.ico  (Windows, native multi-size)
          2. assets/icon.png        (cross-platform high resolution fallback)
          3. standard SP_ComputerIcon (ultimate fallback)
        """
        # `bundle_dir()` resolves to sys._MEIPASS in a PyInstaller bundle
        # (where assets/ actually lives — under _internal/ in PyInstaller 6+).
        # Using dirname(sys.executable) instead misses the icon because data
        # files are NOT alongside the exe in modern PyInstaller layouts.
        base_dir = str(bundle_dir())

        for filename in ('spacedrive.ico', 'icon.png'):
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

        reset_gps_action = QAction("Stop navigation", self)
        reset_gps_action.setShortcut("Shift+F5")  # cosmetic — the global
        # hotkey is wired separately via HotkeyListener so it fires even
        # when SpaceDrive is not the focused window.
        reset_gps_action.triggered.connect(self.reset_velocity_tracker)
        tray_menu.addAction(reset_gps_action)

        tray_menu.addSeparator()

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

    @staticmethod
    def _center_on_screen(widget):
        """Center a top-level widget on the primary screen.

        Called before show() so the geometry is applied before the window
        becomes visible. Centering is critical for Star Citizen: mouse
        movement on an off-center dialog causes ship input while hovering
        on the dialog, risking an unintentional maneuver.
        """
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        x = rect.x() + (rect.width()  - widget.width())  // 2
        y = rect.y() + (rect.height() - widget.height()) // 2
        widget.move(x, y)

    def _bring_dialog_to_front(self, dialog):
        """Center, show, and focus a dialog above the overlay."""
        dialog.adjustSize()
        self._center_on_screen(dialog)
        dialog.show()
        # Deferred raise+activate so Qt processes the show() event first.
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
                "Could not open options window. See logs/spacedrive.log.",
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
                "Could not open POI manager. See logs/spacedrive.log.",
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

        # Rebuild the velocity tracker so any [Kalman] tuning change takes
        # effect immediately. Rebuilding (rather than mutating in place) starts
        # from a clean state with the new noise std, which is the safe path —
        # the tracker re-seeds on the next OCR sample anyway.
        self._velocity_tracker = self._build_velocity_tracker()
        logger.info("Velocity tracker rebuilt with updated Kalman settings")

        # Apply the telemetry toggle live (enable/disable without a restart).
        self._apply_telemetry_setting()

        # Refresh hotkeys in case they were modified
        self.hotkey_listener.reload_hotkeys()

        # Hot-reload the OCR processor so an engine/mode/device change in
        # Options takes effect on the next tick — no app restart needed.
        try:
            self._worker.reload_requested.emit()
        except Exception as exc:
            logger.error("Could not request OCR reload: %s", exc)

        # After the (async) reload, surface any engine fallback so changing the
        # text engine in Options to one that can't start is visible, not silent.
        QTimer.singleShot(2500, self._check_engine_fallback)

    def _on_destination_changed(self, poi):
        self.nav.set_target(
            poi["x"], poi["y"], poi["z"], poi["name"],
            ooc=poi.get("ooc"), kind=poi.get("kind", "space"),
        )
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        logger.info(f"Destination set: {poi['name']}")

    def _on_goto_requested(self, poi):
        self.nav.set_target(
            poi["x"], poi["y"], poi["z"], poi["name"],
            ooc=poi.get("ooc"), kind=poi.get("kind", "space"),
        )
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        if not self.is_visible:
            self.toggle_overlay()

    def _build_velocity_tracker(self):
        """Construct a VelocityTracker with the Kalman tuning knobs from config.

        Centralised so both initial construction and a live rebuild (after the
        Options dialog is saved) read the same [Kalman] settings.
        """
        cfg = self.config_manager
        return VelocityTracker(
            sigma_a=cfg.get_kalman_sigma_a(),
            sigma_z=cfg.get_kalman_sigma_z(),
            gate_nis=cfg.get_kalman_gate_nis(),
            coast_s=cfg.get_kalman_coast_s(),
            max_speed_km_s=cfg.get_kalman_max_speed_km_s(),
        )

    def reset_velocity_tracker(self):
        """Resets GPS state AND cancels any active navigation toward a POI.

        Triggered by the tray action and by the `reset_gps_nav` hotkey
        (default Shift+F5). Both reset the velocity tracker (useful after a
        quantum jump that breaks the EMA) and clear the current destination
        so the overlay stops painting bearing/distance toward a stale POI.
        """
        self._velocity_tracker.reset()
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        # Clear the smoothed distance too, else it lingers as a stale readout
        # (and a phantom value in telemetry) after navigation is stopped.
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._last_vel_heading = None
        self._heading_unstable_until = 0.0
        had_target = self.nav.target is not None
        self.nav.clear_target()
        self._refresh_nav_label()
        self.tray_icon.showMessage(
            "GPS reset",
            ("Movement history cleared and navigation stopped."
             if had_target
             else "Movement history cleared. Move to recalculate direction."),
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )
        logger.info(
            "GPS reset (velocity tracker cleared, target cleared=%s)", had_target,
        )

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
            # Custom QDialog to force always-on-top
            # (overlay parent has WindowTransparentForInput, default dialog
            # may appear unfocused behind overlay) and prompt for POI kind.
            age = self._coord_age_s()
            fl = _padek_freshness_level(age)
            dialog = _SavePOIDialog(freshness_level=fl, parent=self)
            dialog.adjustSize()
            self._center_on_screen(dialog)
            QTimer.singleShot(0, dialog.raise_)
            QTimer.singleShot(0, dialog.activateWindow)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            name = dialog.get_name()
            if not name:
                return
            kind = dialog.get_kind()
            category = dialog.get_category()
            description = dialog.get_description()

            ooc = snap.get("ooc")
            self.nav.add_user_point(
                name,
                snap["x"],
                snap["y"],
                snap["z"],
                snap.get("location", "Unknown"),
                ooc=ooc,
                kind=kind,
                category=category,
            )
            # Store description in the last appended entry (add_user_point
            # does not expose this field to keep its signature minimal).
            if description:
                self.nav.user_poi[-1]["description"] = description
                self.nav.save_user_poi()
            ooc_str = f" ({ooc})" if ooc else " (unknown zone)"
            self.tray_icon.showMessage(
                "Success",
                f"Point '{name}' [{kind}] saved{ooc_str}.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        except Exception:
            logger.exception("Error saving position")
            self.tray_icon.showMessage(
                "Error",
                "Could not save position. See logs/spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export points", "", "JSON Files (*.json)")
        if path:
            if self.nav.export_points(path):
                self.tray_icon.showMessage("Success", "Points exported.", QSystemTrayIcon.MessageIcon.Information)

    def import_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import points", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Could not read file:\n{e}")
            return

        try:
            pois = poi_io.parse_poi_list(text)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid file", f"File does not contain valid POI data:\n\n{e}")
            return

        imported = skipped = replaced = renamed = 0
        for poi_data in pois:
            name_lower = poi_data["name"].lower()
            existing_idx = next(
                (i for i, p in enumerate(self.nav.user_poi)
                 if str(p.get("name", "")).lower() == name_lower),
                None,
            )
            if existing_idx is not None:
                reply = QMessageBox.question(
                    self,
                    "Duplicate POI",
                    f"A POI named '{poi_data['name']}' already exists.\n\n"
                    "Yes = Replace existing\n"
                    "No = Keep both (auto-rename imported one)\n"
                    "Cancel = Skip this POI",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    skipped += 1
                    continue
                if reply == QMessageBox.StandardButton.Yes:
                    self.nav.user_poi.pop(existing_idx)
                    replaced += 1
                else:
                    poi_data["name"] = self._next_free_poi_name(poi_data["name"])
                    renamed += 1
            else:
                imported += 1
            self.nav.user_poi.append(poi_data)

        if imported + replaced + renamed == 0:
            self.tray_icon.showMessage(
                "Import", f"Nothing imported ({skipped} skipped).",
                QSystemTrayIcon.MessageIcon.Warning, 3000,
            )
            return

        try:
            self.nav.save_user_poi()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save POIs:\n{e}")
            return

        parts = []
        if imported:
            parts.append(f"{imported} added")
        if replaced:
            parts.append(f"{replaced} replaced")
        if renamed:
            parts.append(f"{renamed} renamed")
        if skipped:
            parts.append(f"{skipped} skipped")
        self.tray_icon.showMessage(
            "Import complete", ", ".join(parts) + ".",
            QSystemTrayIcon.MessageIcon.Information, 3000,
        )

    def _next_free_poi_name(self, base_name: str) -> str:
        """Return a non-colliding name by appending ' (2)', ' (3)', ..."""
        existing = {str(p.get("name", "")).lower() for p in self.nav.user_poi}
        i = 2
        while f"{base_name} ({i})".lower() in existing:
            i += 1
        return f"{base_name} ({i})"

    def quit_application(self):
        self.hotkey_listener.cleanup()
        self._worker.stop()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        if self._telemetry is not None:
            self._telemetry.close()
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.toggle_overlay()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Load PADEK font now that QApplication exists, then update the module global.
    PADEK_DISPLAY_FONT = _load_padek_fonts()  # noqa: F841 (read by sub-modules)
    logger.info("PADEK display font: %s", PADEK_DISPLAY_FONT)

    qss_path = bundle_dir() / "assets" / "padek-theme.qss"
    if qss_path.exists():
        with open(qss_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    overlay = GPSOverlay()
    overlay.show()

    # Warm the engine-detection cache off the UI thread so the first Options
    # dialog open (Shift+F2) does not block on nvidia-smi / sidecar-venv probes.
    # Results are memoised in engine_installer; the Engine Manager invalidates
    # them after an install/uninstall via reset_detection_cache().
    def _warm_engine_detection():
        try:
            from engine_installer import detect_cuda, detect_gpu_paddle_status
            detect_cuda()
            detect_gpu_paddle_status()
        except Exception:
            logger.debug("engine detection warm-up failed", exc_info=True)

    threading.Thread(
        target=_warm_engine_detection, name="engine-detect-warmup", daemon=True
    ).start()

    sys.exit(app.exec())
