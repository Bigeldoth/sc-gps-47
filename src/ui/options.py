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
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QSlider, QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QMessageBox, QKeySequenceEdit, QWidget,
                             QTabWidget, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
                             QFormLayout)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QFontDatabase, QFont

logger = logging.getLogger(__name__)


class OptionsWindow(QDialog):
    """Tabbed options dialog (dark MFD-style theme)."""

    options_saved = pyqtSignal()

    def __init__(self, config_manager, hotkey_listener, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.hotkey_listener = hotkey_listener

        self.setWindowTitle("Star Citizen GPS Settings")
        self.setMinimumWidth(640)
        self.setMinimumHeight(480)

        self._apply_dark_theme()
        self._create_ui()
        self._load_current_values()

    def _apply_dark_theme(self):
        font_id = QFontDatabase.addApplicationFont("tools/fonts/Electrolize-Regular.ttf")
        if font_id >= 0:
            font_family = QFontDatabase.applicationFontFamilies(font_id)[0]
            self.setFont(QFont(font_family, 10))

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
        palette.setColor(QPalette.ColorRole.Base, QColor(40, 40, 40))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(50, 50, 50))
        palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
        palette.setColor(QPalette.ColorRole.Button, QColor(50, 50, 50))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(70, 130, 180))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        self.setPalette(palette)

        self.setStyleSheet("""
            QDialog { background-color: #1e1e1e; color: #dcdcdc; }
            QLabel { color: #dcdcdc; font-size: 11pt; }
            QTabWidget::pane {
                border: 1px solid #555;
                background: #1e1e1e;
                top: -1px;
            }
            QTabBar::tab {
                background: #2a2a2a;
                color: #dcdcdc;
                border: 1px solid #555;
                padding: 6px 14px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #3a3a3a;
                border-bottom: 1px solid #1e1e1e;
                color: #4682b4;
                font-weight: bold;
            }
            QSlider::groove:horizontal {
                border: 1px solid #555; height: 8px;
                background: #2a2a2a; margin: 2px 0; border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #4682b4; border: 1px solid #5c5c5c;
                width: 18px; margin: -5px 0; border-radius: 9px;
            }
            QSlider::handle:horizontal:hover { background: #5a9fd4; }
            QPushButton {
                background-color: #3a3a3a; color: #dcdcdc;
                border: 1px solid #555; padding: 8px 16px;
                border-radius: 4px; font-size: 10pt;
            }
            QPushButton:hover { background-color: #4a4a4a; border: 1px solid #777; }
            QPushButton:pressed { background-color: #2a2a2a; }
            QTableWidget {
                background-color: #282828; color: #dcdcdc;
                gridline-color: #3a3a3a; border: 1px solid #555;
            }
            QTableWidget::item { padding: 5px; }
            QTableWidget::item:selected { background-color: #4682b4; }
            QHeaderView::section {
                background-color: #3a3a3a; color: #dcdcdc;
                padding: 5px; border: 1px solid #555; font-weight: bold;
            }
            QKeySequenceEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #282828; color: #dcdcdc;
                border: 1px solid #555; padding: 4px;
                border-radius: 3px;
                min-height: 22px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 18px;
                border-left: 1px solid #555;
                background: #3a3a3a;
            }
            QComboBox::down-arrow {
                width: 0; height: 0;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #dcdcdc;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #282828;
                color: #dcdcdc;
                border: 1px solid #555;
                selection-background-color: #4682b4;
                selection-color: #ffffff;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 2px 6px;
            }
            QCheckBox { color: #dcdcdc; spacing: 6px; }
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
        self.save_button = QPushButton("Save & Close")
        self.save_button.clicked.connect(self._save_and_close)
        button_layout.addWidget(self.save_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _section_title(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #4682b4;")
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
        self.text_engine_combo.addItems(["tesseract"])
        form.addRow("Text engine:", self.text_engine_combo)

        self.glyph_engine_combo = QComboBox()
        self.glyph_engine_combo.addItems(["ncc", "onnx"])
        form.addRow("Glyph engine:", self.glyph_engine_combo)

        self.onnx_threshold_spin = QDoubleSpinBox()
        self.onnx_threshold_spin.setRange(0.0, 1.0)
        self.onnx_threshold_spin.setSingleStep(0.05)
        self.onnx_threshold_spin.setDecimals(2)
        form.addRow("ONNX confidence threshold:", self.onnx_threshold_spin)

        layout.addLayout(form)
        layout.addWidget(self._hint(
            "Glyph engine = ncc → template matching (NumPy, ~3 ms).\n"
            "Glyph engine = onnx → TinyGlyphCNN model in models/spacedrive_ocr.onnx."
        ))
        layout.addStretch()
        widget.setLayout(layout)
        return widget

    # ----- Debug tab -----
    def _create_debug_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._section_title("Debug & Logging"))

        self.save_ocr_check = QCheckBox("Save OCR debug images (debug_capture_*.png)")
        self.save_glyph_check = QCheckBox("Save segmented glyph crops (data/glyphs/)")
        self.verbose_check = QCheckBox("Verbose OCR logging")
        layout.addWidget(self.save_ocr_check)
        layout.addWidget(self.save_glyph_check)
        layout.addWidget(self.verbose_check)

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
        layout.addStretch()
        widget.setLayout(layout)
        return widget

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
        layout.addWidget(self.hotkey_table)
        layout.addWidget(self._hint("Click 'Modify' to change a shortcut."))

        widget.setLayout(layout)
        return widget

    def _hint(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-size: 9pt; color: #999;")
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
        glyph = cfg.get_glyph_engine()
        idx = self.glyph_engine_combo.findText(glyph)
        if idx >= 0:
            self.glyph_engine_combo.setCurrentIndex(idx)
        self.onnx_threshold_spin.setValue(cfg.get_onnx_confidence_threshold())

        # Debug
        self.save_ocr_check.setChecked(self._cfg_bool('Debug', 'save_ocr_images', False))
        self.save_glyph_check.setChecked(self._cfg_bool('Debug', 'save_glyph_crops', False))
        self.verbose_check.setChecked(self._cfg_bool('Debug', 'verbose_mode', False))
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

            modify_button = QPushButton("Modify")
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
            cfg.set_glyph_engine(self.glyph_engine_combo.currentText())
            self._set_cfg('OCR', 'onnx_confidence_threshold', f"{self.onnx_threshold_spin.value():.2f}")

            # Debug
            self._set_cfg('Debug', 'save_ocr_images', self.save_ocr_check.isChecked())
            self._set_cfg('Debug', 'save_glyph_crops', self.save_glyph_check.isChecked())
            self._set_cfg('Debug', 'verbose_mode', self.verbose_check.isChecked())
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
