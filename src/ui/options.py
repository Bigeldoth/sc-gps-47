"""Options window for SpaceDrive GPS.

Tabbed dialog that surfaces the contents of config.ini:
  - General: scan interval, overlay opacity, status bar
  - Navigation: arrival radius (in meters)
  - OCR: text engine, glyph engine, ONNX confidence threshold
  - Debug: image dumps, verbose logging, log level
  - Hotkeys: 4 global shortcuts
"""
import logging
import sys
from pathlib import Path

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QSlider, QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QMessageBox, QKeySequenceEdit, QWidget,
                             QTabWidget, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
                             QFormLayout)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QFontDatabase, QFont

logger = logging.getLogger(__name__)


class _GpuProbeThread(QThread):
    """Probes CUDA / paddle-GPU status off the UI thread.

    The probe can spawn nvidia-smi and a sidecar-venv paddle import (~2.7 s
    cold), so it must never run on the UI thread — otherwise the Options dialog
    freezes for seconds on open. Results are memoised in engine_installer, so a
    warm probe returns in ~5 ms.
    """

    done = pyqtSignal(bool, str, str)  # cuda_available, gpu_status, gpu_msg

    def run(self):
        cuda_available, gpu_status, gpu_msg = False, "no_gpu", ""
        try:
            from engine_installer import detect_cuda, detect_gpu_paddle_status
            cuda_available = detect_cuda()
            gpu_status, gpu_msg = detect_gpu_paddle_status()
        except Exception:
            logger.debug("GPU availability probe failed", exc_info=True)
        self.done.emit(cuda_available, gpu_status, gpu_msg)


class _UpdateCheckThread(QThread):
    """Checks for available updates off the UI thread."""

    check_complete = pyqtSignal(str, dict)  # new_version, latest_json (or None on error)

    def __init__(self, current_version: str):
        super().__init__()
        self.current_version = current_version

    def run(self):
        try:
            from update_manager import UpdateManager
            um = UpdateManager(None, self.current_version)
            new_version, latest_json = um.check_for_update()
            if latest_json is None:
                latest_json = {}
            self.check_complete.emit(new_version or "", latest_json)
        except Exception as exc:
            logger.warning(f"Update check failed: {exc}")
            self.check_complete.emit("", {})


class _UpdateApplyThread(QThread):
    """Downloads and applies delta update off the UI thread."""

    progress = pyqtSignal(int, str)  # percentage, message
    complete = pyqtSignal(bool, str)  # success, message

    def __init__(self, config_manager, current_version: str, target_version: str, delta_url: str, delta_checksum: str):
        super().__init__()
        self.config_manager = config_manager
        self.current_version = current_version
        self.target_version = target_version
        self.delta_url = delta_url
        self.delta_checksum = delta_checksum

    def run(self):
        try:
            from update_manager import UpdateManager
            from pathlib import Path

            um = UpdateManager(self.config_manager, self.current_version)

            # Download
            self.progress.emit(10, "Downloading update...")
            def on_progress(pct, written, total):
                self.progress.emit(10 + int(pct * 0.8), f"Downloading... {pct}%")

            delta_zip = um.download_delta(self.delta_url, self.target_version, on_progress)
            if not delta_zip:
                self.complete.emit(False, "Download failed")
                return

            # Verify
            self.progress.emit(95, "Verifying integrity...")
            if not um.verify_delta(delta_zip, self.delta_checksum):
                self.complete.emit(False, "Checksum verification failed")
                return

            # Apply
            self.progress.emit(96, "Applying update...")
            success, msg = um.apply_delta(self.target_version, delta_zip)

            if success:
                self.complete.emit(True, f"Update to {self.target_version} applied successfully")
            else:
                self.complete.emit(False, f"Apply failed: {msg}")

        except Exception as exc:
            logger.error(f"Update apply thread failed: {exc}", exc_info=True)
            self.complete.emit(False, f"Error: {exc}")


class OptionsWindow(QDialog):
    """Tabbed options dialog (dark MFD-style theme)."""

    options_saved = pyqtSignal()

    def __init__(self, config_manager, hotkey_listener, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.hotkey_listener = hotkey_listener

        self.setWindowTitle("SpaceDrive GPS — Settings")
        self.setMinimumWidth(680)
        self.setMinimumHeight(530)

        self._apply_dark_theme()
        self._create_ui()
        self._load_current_values()

    def _apply_dark_theme(self):
        self.setFont(QFont("Roboto", 11))
        # The global QSS (padek-theme.qss) handles all widget styling.
        self.setStyleSheet("""
            OptionsWindow, QDialog {
                background-color: #0E1216;
            }
        """)

    def _create_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_general_tab(), "General")
        self.tabs.addTab(self._create_navigation_tab(), "Navigation")
        self.tabs.addTab(self._create_ocr_tab(), "OCR")
        self.tabs.addTab(self._create_debug_tab(), "Debug")
        self.tabs.addTab(self._create_hotkey_tab(), "Hotkeys")
        self.tabs.addTab(self._create_updates_tab(), "Updates")
        layout.addWidget(self.tabs)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.save_button = QPushButton("SAVE")
        self.save_button.setObjectName("btn_primary")
        self.save_button.clicked.connect(self._save_and_close)
        button_layout.addWidget(self.save_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _section_title(self, text):
        label = QLabel(text.upper())
        label.setFont(QFont("Roboto", 9, QFont.Weight.Bold))
        label.setStyleSheet("color: #19C28A; background: transparent; padding-top: 4px;")
        return label

    # ----- General tab -----
    def _create_general_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("OCR Scan Frequency"))

        slider_row = QHBoxLayout()
        self.ocr_slider = QSlider(Qt.Orientation.Horizontal)
        self.ocr_slider.setMinimum(50)
        self.ocr_slider.setMaximum(2000)
        self.ocr_slider.setSingleStep(10)
        self.ocr_slider.setPageStep(100)
        self.ocr_slider.valueChanged.connect(self._update_ocr_label)
        self.ocr_value_label = QLabel("200 ms")
        self.ocr_value_label.setMinimumWidth(80)
        self.ocr_value_label.setStyleSheet("font-size: 11pt; color: #4682b4;")
        slider_row.addWidget(self.ocr_slider)
        slider_row.addWidget(self.ocr_value_label)
        layout.addLayout(slider_row)
        layout.addWidget(self._hint("Interval between each OCR scan (50–2000 ms)"))

        layout.addSpacing(10)
        layout.addWidget(self._section_title("Overlay"))
        form = QFormLayout()

        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.1, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        form.addRow("Default opacity:", self.opacity_spin)

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(50, 1000)
        self.refresh_spin.setSuffix(" ms")
        form.addRow("Display refresh interval:", self.refresh_spin)

        self.show_status_check = QCheckBox("Show status bar in overlay")
        form.addRow("", self.show_status_check)

        layout.addLayout(form)
        layout.addStretch()
        widget.setLayout(layout)
        return widget

    # ----- Navigation tab -----
    def _create_navigation_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("Arrival Precision"))

        form = QFormLayout()
        self.arrival_radius_spin = QSpinBox()
        self.arrival_radius_spin.setRange(1, 10000)
        self.arrival_radius_spin.setSingleStep(10)
        self.arrival_radius_spin.setSuffix(" m")
        form.addRow("Arrival radius:", self.arrival_radius_spin)
        layout.addLayout(form)

        layout.addWidget(self._hint(
            "Below this distance to the target, the EMA smoothing is bypassed "
            "and the raw OCR distance is shown directly. Lower values give a "
            "more reactive readout near the destination."
        ))

        # ── Kalman filter tuning (advanced) ─────────────────────────────
        layout.addSpacing(12)
        layout.addWidget(self._section_title("Filter Tuning (Advanced)"))

        kalman_form = QFormLayout()

        self.kalman_max_speed_spin = QDoubleSpinBox()
        self.kalman_max_speed_spin.setRange(0.1, 50.0)
        self.kalman_max_speed_spin.setSingleStep(0.1)
        self.kalman_max_speed_spin.setDecimals(2)
        self.kalman_max_speed_spin.setSuffix(" km/s")
        kalman_form.addRow("Max speed:", self.kalman_max_speed_spin)

        self.kalman_sigma_a_spin = QDoubleSpinBox()
        self.kalman_sigma_a_spin.setRange(0.001, 100.0)
        self.kalman_sigma_a_spin.setSingleStep(0.1)
        self.kalman_sigma_a_spin.setDecimals(3)
        self.kalman_sigma_a_spin.setSuffix(" km/s²")
        kalman_form.addRow("Process noise (sigma_a):", self.kalman_sigma_a_spin)

        self.kalman_sigma_z_spin = QDoubleSpinBox()
        self.kalman_sigma_z_spin.setRange(0.0001, 10.0)
        self.kalman_sigma_z_spin.setSingleStep(0.01)
        self.kalman_sigma_z_spin.setDecimals(4)
        self.kalman_sigma_z_spin.setSuffix(" km")
        kalman_form.addRow("Measurement noise (sigma_z):", self.kalman_sigma_z_spin)

        self.kalman_gate_nis_spin = QDoubleSpinBox()
        self.kalman_gate_nis_spin.setRange(0.1, 100000.0)
        self.kalman_gate_nis_spin.setSingleStep(1.0)
        self.kalman_gate_nis_spin.setDecimals(1)
        kalman_form.addRow("Reject gate (NIS):", self.kalman_gate_nis_spin)

        self.kalman_coast_s_spin = QDoubleSpinBox()
        self.kalman_coast_s_spin.setRange(0.0, 30.0)
        self.kalman_coast_s_spin.setSingleStep(0.5)
        self.kalman_coast_s_spin.setDecimals(1)
        self.kalman_coast_s_spin.setSuffix(" s")
        kalman_form.addRow("Coast window:", self.kalman_coast_s_spin)

        layout.addLayout(kalman_form)
        layout.addWidget(self._hint(
            "Advanced Kalman tuning knobs for the velocity/heading filter. "
            "Defaults are seeded from live telemetry — only change these if you "
            "know what you are doing.\n"
            "Max speed = physical velocity clamp.\n"
            "sigma_a = how fast the filter follows turns (higher = snappier, noisier).\n"
            "sigma_z = how much OCR position noise to assume (higher = smoother).\n"
            "Reject gate = innovation threshold above which a read is dropped as a misread.\n"
            "Coast window = how long dead-reckoning lasts through an OCR dropout "
            "before the filter re-seeds."
        ))

        layout.addStretch()
        widget.setLayout(layout)
        return widget

    # ----- OCR tab -----
    def _create_ocr_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("OCR Engines"))

        form = QFormLayout()

        self.text_engine_combo = QComboBox()
        self.text_engine_combo.addItems(["tesseract", "paddle"])
        form.addRow("Text engine:", self.text_engine_combo)

        self.pipeline_mode_combo = QComboBox()
        self.pipeline_mode_combo.addItems(["hybrid", "full_text"])
        form.addRow("Pipeline mode:", self.pipeline_mode_combo)

        self.paddle_device_combo = QComboBox()
        self.paddle_device_combo.addItems(["cpu", "gpu"])
        form.addRow("Paddle device:", self.paddle_device_combo)

        self.glyph_engine_combo = QComboBox()
        self.glyph_engine_combo.addItems(["ncc", "onnx"])
        form.addRow("Glyph engine:", self.glyph_engine_combo)

        self.onnx_threshold_spin = QDoubleSpinBox()
        self.onnx_threshold_spin.setRange(0.0, 1.0)
        self.onnx_threshold_spin.setSingleStep(0.05)
        self.onnx_threshold_spin.setDecimals(2)
        form.addRow("ONNX confidence threshold:", self.onnx_threshold_spin)

        layout.addLayout(form)

        # Engine management (install / detect Tesseract & Paddle).
        button_row = QHBoxLayout()
        self.manage_engines_button = QPushButton("Manage engines…")
        self.manage_engines_button.clicked.connect(self._open_engine_manager)
        button_row.addWidget(self.manage_engines_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        # Disable the GPU option if CUDA cannot be detected on this machine.
        # Probed asynchronously so the dialog opens instantly (see
        # _start_gpu_availability_probe); the GPU entry is greyed when it returns.
        self._start_gpu_availability_probe()

        layout.addWidget(self._hint(
            "Text engine = tesseract → fast, requires Tesseract-OCR installed.\n"
            "Text engine = paddle → PaddleOCR (PP-OCRv4), pip-installable.\n"
            "Pipeline mode = hybrid → NCC/ONNX glyphs first, text engine as "
            "fallback for zone lines.\n"
            "Pipeline mode = full_text → skip glyph stage, run the text engine "
            "alone on the full HUD crop."
        ))
        layout.addStretch()
        widget.setLayout(layout)
        return widget

    def _start_gpu_availability_probe(self):
        """Kicks off the background CUDA/paddle-GPU probe.

        Non-blocking: the GPU combo entry starts enabled (optimistic) and is
        greyed by `_apply_gpu_status` once the probe finishes. Keeps a reference
        to the thread so it isn't garbage-collected mid-run.
        """
        self._gpu_probe = _GpuProbeThread(self)
        self._gpu_probe.done.connect(self._apply_gpu_status)
        self._gpu_probe.start()

    def _apply_gpu_status(self, cuda_available, gpu_status, gpu_msg):
        """Greys out the GPU choice when CUDA is missing or the local GPU is
        too new for the current paddlepaddle build. Tristate-aware:
          - 'ok'                     → GPU enabled
          - 'needs_blackwell_wheel'  → GPU disabled, tooltip points at
                                       Manage engines… → Migrate
          - 'too_new' / 'no_gpu'     → GPU disabled, generic tooltip
        """
        gpu_index = self.paddle_device_combo.findText("gpu")
        if gpu_index < 0:
            return
        model = self.paddle_device_combo.model()
        item = model.item(gpu_index)

        enable = cuda_available and gpu_status == "ok"
        if item is not None:
            item.setEnabled(enable)

        if enable:
            self.paddle_device_combo.setItemData(
                gpu_index, "", Qt.ItemDataRole.ToolTipRole,
            )
            return

        # GPU turned out unavailable — if it is the current selection, fall
        # back to CPU (mirrors the saved-device handling in _load_current_values).
        if self.paddle_device_combo.currentText() == "gpu":
            cpu_idx = self.paddle_device_combo.findText("cpu")
            if cpu_idx >= 0:
                self.paddle_device_combo.setCurrentIndex(cpu_idx)

        if gpu_status == "needs_blackwell_wheel":
            tooltip = (
                gpu_msg + "\n\nClick 'Manage engines…' → 'Migrate to Blackwell "
                "wheel (cu129)' to enable GPU."
            )
        elif gpu_status == "too_new":
            tooltip = gpu_msg
        else:
            tooltip = "CUDA runtime not detected on this machine"
        self.paddle_device_combo.setItemData(
            gpu_index, tooltip, Qt.ItemDataRole.ToolTipRole,
        )

    def _open_engine_manager(self):
        try:
            from ui.engine_manager import EngineManagerDialog
        except ImportError as exc:
            QMessageBox.warning(
                self, "Engine manager",
                f"Engine manager unavailable: {exc}",
            )
            return
        dlg = EngineManagerDialog(self, config_manager=self.config_manager)
        dlg.exec()
        # Re-evaluate availability after the user may have installed/migrated
        # (the manager cleared the detection cache, so this re-probes).
        self._start_gpu_availability_probe()

    # ----- Debug tab -----
    def _create_debug_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("Debug & Logging"))

        self.save_ocr_check = QCheckBox("Save OCR debug images (debug_capture_*.png)")
        self.save_glyph_check = QCheckBox("Save segmented glyph crops (data/glyphs/)")
        self.verbose_check = QCheckBox("Verbose OCR logging")
        self.record_telemetry_check = QCheckBox(
            "Record navigation telemetry (logs/telemetry-*.jsonl)"
        )
        layout.addWidget(self.save_ocr_check)
        layout.addWidget(self.save_glyph_check)
        layout.addWidget(self.verbose_check)
        layout.addWidget(self.record_telemetry_check)

        form = QFormLayout()
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        form.addRow("Log level:", self.log_level_combo)
        layout.addLayout(form)

        layout.addWidget(self._hint(
            "These options are useful when reporting an OCR issue or building "
            "a new template set. They have a noticeable I/O cost — leave off "
            "during normal play."
        ))

        # ── PaddleOCR diagnostic ────────────────────────────────────────
        layout.addSpacing(12)
        layout.addWidget(self._section_title("PaddleOCR diagnostic"))
        layout.addWidget(self._hint(
            "Captures N live HUD strips, runs paddle on each, and saves the "
            "raw image + an annotated visualisation + a JSON / Markdown "
            "summary under diagnostics/paddle/<timestamp>/. The summary is "
            "designed to be shared back to Claude for targeted improvement."
        ))
        diag_row = QHBoxLayout()
        diag_row.addWidget(QLabel("Max samples:"))
        self.paddle_diag_samples_spin = QSpinBox()
        self.paddle_diag_samples_spin.setRange(1, 500)
        self.paddle_diag_samples_spin.setValue(20)
        diag_row.addWidget(self.paddle_diag_samples_spin)
        diag_row.addWidget(QLabel("Interval (ms):"))
        self.paddle_diag_interval_spin = QSpinBox()
        self.paddle_diag_interval_spin.setRange(0, 5000)
        self.paddle_diag_interval_spin.setSingleStep(50)
        self.paddle_diag_interval_spin.setValue(200)
        diag_row.addWidget(self.paddle_diag_interval_spin)
        self.run_paddle_diag_button = QPushButton("Run PaddleOCR diagnostic")
        self.run_paddle_diag_button.clicked.connect(self._on_run_paddle_diagnostic)
        diag_row.addWidget(self.run_paddle_diag_button)
        diag_row.addStretch()
        layout.addLayout(diag_row)

        layout.addStretch()
        widget.setLayout(layout)
        return widget

    def _on_run_paddle_diagnostic(self):
        """Spawns the diagnostic in a QThread with a progress dialog.

        Output paths are predictable (diagnostics/paddle/<timestamp>/) so the
        user can share them back; the result message offers to open the
        folder directly.
        """
        try:
            from paddle_diagnose import run_diagnostic
            from engine_installer import detect_paddleocr
        except Exception as exc:
            QMessageBox.critical(
                self, "Diagnostic", f"Could not import diagnostic: {exc}",
            )
            return

        if not detect_paddleocr().installed:
            QMessageBox.warning(
                self, "Diagnostic",
                "PaddleOCR is not installed in this Python environment.\n"
                "Use Options → OCR → Manage engines… first.",
            )
            return

        max_samples = self.paddle_diag_samples_spin.value()
        interval_ms = self.paddle_diag_interval_spin.value()
        device = self.config_manager.get_paddle_device()

        from PyQt6.QtCore import QThread, pyqtSignal
        from PyQt6.QtWidgets import QProgressDialog

        progress = QProgressDialog(
            "Initialising PaddleOCR…", "Cancel", 0, max_samples, self,
        )
        progress.setWindowTitle("PaddleOCR diagnostic")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()

        class _DiagWorker(QThread):
            tick = pyqtSignal(int, int, str)
            done = pyqtSignal(str, str)  # (out_dir, error)

            def __init__(self, max_samples, interval_ms, device):
                super().__init__()
                self._max = max_samples
                self._int = interval_ms
                self._device = device
                self._cancelled = False

            def cancel(self):
                self._cancelled = True

            def _on_progress(self, i, n, msg):
                self.tick.emit(i, n, msg)
                # Return False to stop the loop when the user clicks Cancel.
                return not self._cancelled

            def run(self):
                try:
                    out_dir = run_diagnostic(
                        max_samples=self._max,
                        interval_ms=self._int,
                        device=self._device,
                        progress=self._on_progress,
                    )
                    self.done.emit(str(out_dir), "")
                except Exception as exc:
                    self.done.emit("", str(exc))

        worker = _DiagWorker(max_samples, interval_ms, device)

        def _on_tick(i, n, msg):
            progress.setValue(i)
            progress.setLabelText(msg)

        def _on_done(out_dir, error):
            progress.close()
            self.run_paddle_diag_button.setEnabled(True)
            if error:
                QMessageBox.critical(
                    self, "Diagnostic failed", error,
                )
                return
            summary_path = Path(out_dir) / "summary.md"
            text = (
                f"Diagnostic complete.\n\nOutput:\n{out_dir}\n\n"
                f"Share `summary.md` + the annotated PNGs with Claude to iterate."
            )
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setWindowTitle("Diagnostic complete")
            msg.setText(text)
            open_btn = msg.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton(QMessageBox.StandardButton.Close)
            msg.exec()
            if msg.clickedButton() is open_btn:
                import os, subprocess, sys as _sys
                if _sys.platform == "win32":
                    os.startfile(out_dir)
                else:
                    subprocess.Popen(["xdg-open", out_dir])

        progress.canceled.connect(worker.cancel)
        worker.tick.connect(_on_tick)
        worker.done.connect(_on_done)
        # Keep a reference so the QThread isn't GC'd mid-run.
        self._diag_worker = worker
        self.run_paddle_diag_button.setEnabled(False)
        worker.start()

    # ----- Hotkeys tab -----
    def _create_hotkey_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("Keyboard Shortcuts"))

        self.hotkey_table = QTableWidget()
        self.hotkey_table.setColumnCount(3)
        self.hotkey_table.setHorizontalHeaderLabels(["Action", "Shortcut", "Modify"])
        header = self.hotkey_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.hotkey_table.verticalHeader().setVisible(False)
        self.hotkey_table.verticalHeader().setDefaultSectionSize(34)
        layout.addWidget(self.hotkey_table)
        layout.addWidget(self._hint("Click 'Modify' to change a shortcut."))

        widget.setLayout(layout)
        return widget

    # ----- Updates tab -----
    def _create_updates_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("Application Updates"))

        # Current version display
        form = QFormLayout()
        self.current_version_label = QLabel("Loading...")
        self.current_version_label.setStyleSheet("color: #6FE8FF;")
        form.addRow("Current version:", self.current_version_label)

        self.latest_version_label = QLabel("—")
        self.latest_version_label.setStyleSheet("color: #6FE8FF;")
        form.addRow("Latest version:", self.latest_version_label)

        self.update_size_label = QLabel("—")
        form.addRow("Update size:", self.update_size_label)

        layout.addLayout(form)

        # Changelog
        layout.addWidget(self._section_title("Release Notes"))
        self.changelog_text = QLabel("")
        self.changelog_text.setStyleSheet("color: #A8B5C1; font-size: 10px; background: transparent;")
        self.changelog_text.setWordWrap(True)
        layout.addWidget(self.changelog_text)

        # Buttons
        button_row = QHBoxLayout()
        self.check_update_button = QPushButton("Check for Updates")
        self.check_update_button.clicked.connect(self._on_check_updates)
        button_row.addWidget(self.check_update_button)

        self.update_now_button = QPushButton("Update Now")
        self.update_now_button.clicked.connect(self._on_update_now)
        self.update_now_button.setEnabled(False)
        button_row.addWidget(self.update_now_button)

        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(self._on_skip_update)
        self.skip_button.setEnabled(False)
        button_row.addWidget(self.skip_button)

        button_row.addStretch()
        layout.addLayout(button_row)

        # Progress bar (initially hidden)
        from PyQt6.QtWidgets import QProgressBar
        self.update_progress_bar = QProgressBar()
        self.update_progress_bar.setVisible(False)
        layout.addWidget(self.update_progress_bar)

        layout.addWidget(self._hint(
            "Delta updates download only the changed files (~15-30 MB) instead of "
            "the full installer (~50 MB). Click 'Check for Updates' to see if a new "
            "version is available."
        ))

        layout.addStretch()
        widget.setLayout(layout)
        return widget

    def _hint(self, text):
        label = QLabel(text)
        label.setStyleSheet("color: #7E8B97; font-size: 11px; background: transparent;")
        label.setWordWrap(True)
        return label

    def _get_current_app_version(self) -> str:
        """Get the current app version from config.ini or installer config."""
        # First, try reading from config.ini (most reliable in both dev and bundled modes)
        try:
            app_version = self.config_manager.get('Updates', 'app_version', fallback=None)
            if app_version and app_version.strip():
                return app_version.strip()
        except Exception:
            pass

        # Fallback: try reading from installer/spaceDrive.iss (only works in dev mode)
        try:
            from pathlib import Path
            from app_paths import bundle_dir
            iss_file = bundle_dir() / "installer" / "spaceDrive.iss"
            if iss_file.exists():
                with open(iss_file, "r", encoding='utf-8') as f:
                    for line in f:
                        if line.strip().startswith("#define MyAppVersion"):
                            # Extract version from: #define MyAppVersion "0.7.4"
                            parts = line.split('"')
                            if len(parts) >= 2:
                                version = parts[1].strip()
                                if version:
                                    return f"v{version}"
        except Exception as exc:
            logger.debug(f"Could not read version from installer config: {exc}")

        logger.warning("App version could not be determined")
        return "unknown"

    def _on_check_updates(self):
        """Check for updates in a background thread."""
        self.check_update_button.setEnabled(False)
        self.check_update_button.setText("Checking...")
        self._update_check_thread = _UpdateCheckThread(self._get_current_app_version())
        self._update_check_thread.check_complete.connect(self._on_update_check_complete)
        self._update_check_thread.start()

    def _on_update_check_complete(self, new_version: str, latest_json: dict):
        """Handle update check result."""
        self.check_update_button.setEnabled(True)
        self.check_update_button.setText("Check for Updates")

        if not new_version:
            self.latest_version_label.setText("Up to date")
            self.update_now_button.setEnabled(False)
            self.skip_button.setEnabled(False)
            self.changelog_text.setText("You are using the latest version.")
            return

        # Update available
        self.latest_version_label.setText(new_version)
        self.update_now_button.setEnabled(True)
        self.skip_button.setEnabled(True)

        # Show update size and store delta info for download
        delta_info = latest_json.get("delta", {})
        if delta_info.get("available"):
            size_mb = delta_info.get("size_bytes", 0) / (1024 * 1024)
            self.update_size_label.setText(f"~{size_mb:.1f} MB (delta)")
            # Store delta info for _on_update_now()
            self._latest_delta_url = delta_info.get("url", "")
            self._latest_delta_checksum = delta_info.get("checksum", "")
        else:
            self.update_size_label.setText("Full installer")
            self._latest_delta_url = ""
            self._latest_delta_checksum = ""

        # Show changelog
        changelog = latest_json.get("changelog", {})
        if changelog:
            summary = changelog.get("summary", "New version available")
            highlights = changelog.get("highlights", [])
            text = f"{summary}\n"
            for highlight in highlights:
                text += f"• {highlight}\n"
            self.changelog_text.setText(text.strip())
        else:
            self.changelog_text.setText(f"A new version ({new_version}) is available.")

    def _on_update_now(self):
        """Initiate delta download and apply."""
        new_version = self.latest_version_label.text()
        if not new_version or new_version in ("—", "Up to date"):
            return

        current_version = self._get_current_app_version()

        # Show progress dialog
        from PyQt6.QtWidgets import QProgressDialog
        progress_dialog = QProgressDialog(
            "Preparing update...", "Cancel", 0, 100, self
        )
        progress_dialog.setWindowTitle("Updating SpaceDrive GPS")
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dialog.setCancelButton(None)  # Disable cancel during download
        progress_dialog.show()

        # Start update thread
        self._update_thread = _UpdateApplyThread(
            self.config_manager,
            current_version,
            new_version,
            self._latest_delta_url,
            self._latest_delta_checksum
        )
        self._update_thread.progress.connect(self._on_update_progress)
        self._update_thread.complete.connect(lambda ok, msg: self._on_update_complete(ok, msg, progress_dialog))
        self._update_thread.start()
        self._update_progress_dialog = progress_dialog

    def _on_update_progress(self, percent: int, message: str):
        """Update progress dialog."""
        if hasattr(self, '_update_progress_dialog'):
            self._update_progress_dialog.setValue(percent)
            self._update_progress_dialog.setLabelText(message)

    def _on_update_complete(self, success: bool, message: str, dialog):
        """Handle update completion and restart."""
        dialog.close()

        if not success:
            QMessageBox.critical(self, "Update Failed", message)
            return

        # Show restart countdown dialog
        self._show_restart_countdown(message)

    def _show_restart_countdown(self, message: str):
        """Show countdown dialog and restart app."""
        from PyQt6.QtCore import QTimer

        countdown_dialog = QMessageBox(self)
        countdown_dialog.setWindowTitle("Update Complete")
        countdown_dialog.setIcon(QMessageBox.Icon.Information)
        countdown_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)

        # Countdown timer
        self._countdown = 5
        timer = QTimer()

        def update_countdown():
            self._countdown -= 1
            countdown_dialog.setText(
                f"{message}\n\n"
                f"Restarting application in {self._countdown} seconds..."
            )
            if self._countdown <= 0:
                timer.stop()
                countdown_dialog.close()
                self._restart_application()

        timer.timeout.connect(update_countdown)
        countdown_dialog.setText(
            f"{message}\n\n"
            f"Restarting application in {self._countdown} seconds..."
        )
        timer.start(1000)  # Update every second
        countdown_dialog.exec()

    def _restart_application(self):
        """Force quit and let parent/system relaunch."""
        logger.info(f"Restarting application after successful update")
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def _on_skip_update(self):
        """Mark this version as skipped."""
        new_version = self.latest_version_label.text()
        if new_version and new_version != "—" and new_version != "Up to date":
            self.config_manager.set_last_skipped_version(new_version)
            self.update_now_button.setEnabled(False)
            self.skip_button.setEnabled(False)
            QMessageBox.information(self, "Update Skipped", f"You won't be asked about {new_version} again.")

    def _update_ocr_label(self, value):
        self.ocr_value_label.setText(f"{value} ms")

    def _load_current_values(self):
        cfg = self.config_manager

        # Updates
        current_version = self._get_current_app_version()
        self.current_version_label.setText(current_version)

        # General
        self.ocr_slider.setValue(cfg.get_scan_interval())
        self.opacity_spin.setValue(float(cfg.get('Overlay', 'default_opacity', fallback='0.7')))
        self.refresh_spin.setValue(int(cfg.get('Settings', 'refresh_interval_ms', fallback='150')))
        self.show_status_check.setChecked(self._cfg_bool('Overlay', 'show_status_bar', True))

        # Navigation
        self.arrival_radius_spin.setValue(int(round(cfg.get_arrival_radius_m())))
        self.kalman_max_speed_spin.setValue(cfg.get_kalman_max_speed_km_s())
        self.kalman_sigma_a_spin.setValue(cfg.get_kalman_sigma_a())
        self.kalman_sigma_z_spin.setValue(cfg.get_kalman_sigma_z())
        self.kalman_gate_nis_spin.setValue(cfg.get_kalman_gate_nis())
        self.kalman_coast_s_spin.setValue(cfg.get_kalman_coast_s())

        # OCR
        engine = cfg.get_ocr_engine()
        idx = self.text_engine_combo.findText(engine)
        if idx >= 0:
            self.text_engine_combo.setCurrentIndex(idx)
        mode = cfg.get_pipeline_mode()
        idx = self.pipeline_mode_combo.findText(mode)
        if idx >= 0:
            self.pipeline_mode_combo.setCurrentIndex(idx)
        device = cfg.get_paddle_device()
        idx = self.paddle_device_combo.findText(device)
        if idx >= 0:
            # If the saved device is GPU but it has been disabled, fall back to CPU.
            model = self.paddle_device_combo.model()
            item = model.item(idx)
            if item is not None and not item.isEnabled():
                cpu_idx = self.paddle_device_combo.findText("cpu")
                self.paddle_device_combo.setCurrentIndex(cpu_idx if cpu_idx >= 0 else 0)
            else:
                self.paddle_device_combo.setCurrentIndex(idx)
        glyph = cfg.get_glyph_engine()
        idx = self.glyph_engine_combo.findText(glyph)
        if idx >= 0:
            self.glyph_engine_combo.setCurrentIndex(idx)
        self.onnx_threshold_spin.setValue(cfg.get_onnx_confidence_threshold())

        # Debug
        self.save_ocr_check.setChecked(self._cfg_bool('Debug', 'save_ocr_images', False))
        self.save_glyph_check.setChecked(self._cfg_bool('Debug', 'save_glyph_crops', False))
        self.verbose_check.setChecked(self._cfg_bool('Debug', 'verbose_mode', False))
        self.record_telemetry_check.setChecked(self._cfg_bool('Debug', 'record_telemetry', False))
        level = cfg.get('Logging', 'level', fallback='INFO').upper()
        idx = self.log_level_combo.findText(level)
        if idx >= 0:
            self.log_level_combo.setCurrentIndex(idx)

        # Hotkeys
        hotkeys = cfg.get_all_hotkeys()
        action_names = {
            'toggle_overlay': 'Show/Hide overlay',
            'open_options': 'Open options',
            'save_position': 'Save position',
            'open_poi_manager': 'Open POI manager',
            'reset_gps_nav': 'Stop navigation',
        }
        self.hotkey_table.setRowCount(len(hotkeys))
        for row, (action, hotkey) in enumerate(hotkeys.items()):
            action_item = QTableWidgetItem(action_names.get(action, action))
            action_item.setFlags(action_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.hotkey_table.setItem(row, 0, action_item)

            hotkey_item = QTableWidgetItem(hotkey)
            hotkey_item.setFlags(hotkey_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            hotkey_item.setData(Qt.ItemDataRole.UserRole, action)
            self.hotkey_table.setItem(row, 1, hotkey_item)

            modify_button = QPushButton("✎")
            modify_button.setFixedSize(28, 28)
            modify_button.setFont(QFont("Segoe UI Symbol", 11))
            modify_button.setToolTip("Modifier ce raccourci")
            modify_button.setStyleSheet("""
                QPushButton {
                    background: rgba(111,232,255,0.10);
                    color: #6FE8FF;
                    border: 1px solid rgba(111,232,255,0.45);
                    border-radius: 6px;
                    padding: 0px;
                    min-height: 0px;
                }
                QPushButton:hover {
                    background: rgba(111,232,255,0.22);
                    border-color: rgba(111,232,255,0.8);
                }
            """)
            modify_button.clicked.connect(lambda _checked, r=row: self._modify_hotkey(r))
            self.hotkey_table.setCellWidget(row, 2, modify_button)

    def _cfg_bool(self, section, option, default):
        raw = self.config_manager.get(section, option, fallback=str(default))
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

    def _set_cfg(self, section, option, value):
        if not self.config_manager.config.has_section(section):
            self.config_manager.config.add_section(section)
        self.config_manager.config.set(section, option, str(value))

    def _modify_hotkey(self, row):
        action_item = self.hotkey_table.item(row, 1)
        action = action_item.data(Qt.ItemDataRole.UserRole)
        current_hotkey = action_item.text()
        dialog = HotkeyEditDialog(current_hotkey, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_hotkey = dialog.get_hotkey()
            if new_hotkey and new_hotkey != current_hotkey:
                action_item.setText(new_hotkey)
                logger.info(f"Hotkey modified: {action} -> {new_hotkey}")

    def _save_and_close(self):
        try:
            cfg = self.config_manager

            # General
            cfg.set_scan_interval(self.ocr_slider.value())
            self._set_cfg('Overlay', 'default_opacity', f"{self.opacity_spin.value():.2f}")
            self._set_cfg('Settings', 'refresh_interval_ms', self.refresh_spin.value())
            self._set_cfg('Overlay', 'show_status_bar', self.show_status_check.isChecked())

            # Navigation
            cfg.set_arrival_radius_m(self.arrival_radius_spin.value())
            cfg.set_kalman_max_speed_km_s(self.kalman_max_speed_spin.value())
            cfg.set_kalman_sigma_a(self.kalman_sigma_a_spin.value())
            cfg.set_kalman_sigma_z(self.kalman_sigma_z_spin.value())
            cfg.set_kalman_gate_nis(self.kalman_gate_nis_spin.value())
            cfg.set_kalman_coast_s(self.kalman_coast_s_spin.value())

            # OCR
            cfg.set_ocr_engine(self.text_engine_combo.currentText())
            cfg.set_pipeline_mode(self.pipeline_mode_combo.currentText())
            cfg.set_paddle_device(self.paddle_device_combo.currentText())
            cfg.set_glyph_engine(self.glyph_engine_combo.currentText())
            self._set_cfg('OCR', 'onnx_confidence_threshold', f"{self.onnx_threshold_spin.value():.2f}")

            # Debug
            self._set_cfg('Debug', 'save_ocr_images', self.save_ocr_check.isChecked())
            self._set_cfg('Debug', 'save_glyph_crops', self.save_glyph_check.isChecked())
            self._set_cfg('Debug', 'verbose_mode', self.verbose_check.isChecked())
            self._set_cfg('Debug', 'record_telemetry', self.record_telemetry_check.isChecked())
            self._set_cfg('Logging', 'level', self.log_level_combo.currentText())

            # Hotkeys
            for row in range(self.hotkey_table.rowCount()):
                action_item = self.hotkey_table.item(row, 1)
                action = action_item.data(Qt.ItemDataRole.UserRole)
                new_hotkey = action_item.text()
                self.hotkey_listener.update_hotkey(action, new_hotkey)

            if cfg.save():
                logger.info("Configuration saved successfully")
                self.options_saved.emit()
                QMessageBox.information(self, "Success",
                                        "Settings have been saved successfully.")
                self.accept()
            else:
                QMessageBox.warning(self, "Error", "Failed to save configuration.")
        except Exception as e:
            logger.error(f"Error saving options: {e}")
            QMessageBox.critical(self, "Error", f"An error occurred: {e}")


class HotkeyEditDialog(QDialog):
    """Simple dialog for editing a hotkey."""

    def __init__(self, current_hotkey, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Modify Shortcut")
        self.setModal(True)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Press the new key combination:"))

        self.key_edit = QKeySequenceEdit(current_hotkey)
        layout.addWidget(self.key_edit)

        button_layout = QHBoxLayout()
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)
        button_layout.addWidget(ok_button)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        self.setLayout(layout)

        if parent is not None:
            self.setPalette(parent.palette())
            self.setStyleSheet(parent.styleSheet())

    def get_hotkey(self):
        """Returns the entered hotkey in pynput format (e.g., 'cmd+shift+p')."""
        sequence = self.key_edit.keySequence()
        if sequence.isEmpty():
            return ""

        key_string = sequence.toString().lower()
        parts = [p.strip() for p in key_string.split('+') if p.strip()]

        converted = []
        for p in parts:
            if sys.platform == 'darwin':
                if p == 'ctrl':
                    converted.append('cmd')
                elif p == 'meta':
                    converted.append('ctrl')
                else:
                    converted.append(p)
            else:
                if p == 'meta':
                    converted.append('cmd')
                else:
                    converted.append(p)
        return '+'.join(converted)
