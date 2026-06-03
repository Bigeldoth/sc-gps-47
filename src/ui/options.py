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

    def _hint(self, text):
        label = QLabel(text)
        label.setStyleSheet("color: #7E8B97; font-size: 11px; background: transparent;")
        label.setWordWrap(True)
        return label

    def _update_ocr_label(self, value):
        self.ocr_value_label.setText(f"{value} ms")

    def _load_current_values(self):
        cfg = self.config_manager

        # General
        self.ocr_slider.setValue(cfg.get_scan_interval())
        self.opacity_spin.setValue(float(cfg.get('Overlay', 'default_opacity', fallback='0.7')))
        self.refresh_spin.setValue(int(cfg.get('Settings', 'refresh_interval_ms', fallback='150')))
        self.show_status_check.setChecked(self._cfg_bool('Overlay', 'show_status_bar', True))

        # Navigation
        self.arrival_radius_spin.setValue(int(round(cfg.get_arrival_radius_m())))

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
