"""
Options window for SpaceDrive GPS.
Allows configuring OCR scan frequency and hotkeys.
"""
import logging
import sys
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QSlider, QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QMessageBox, QKeySequenceEdit, QWidget)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor

logger = logging.getLogger(__name__)


class OptionsWindow(QDialog):
    """
    Options window with minimalist dark theme.
    Allows configuring:
    - OCR scan frequency (slider)
    - Hotkeys (editable table)
    """

    # Signal emitted when options are saved
    options_saved = pyqtSignal()

    def __init__(self, config_manager, hotkey_listener, parent=None):
        """
        Initializes the options window.

        Args:
            config_manager: ConfigManager instance
            hotkey_listener: HotkeyListener instance
            parent: Parent widget (optional)
        """
        super().__init__(parent)
        self.config_manager = config_manager
        self.hotkey_listener = hotkey_listener

        self.setWindowTitle("Star Citizen GPS Settings")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)

        # Apply dark theme
        self._apply_dark_theme()

        # Create UI
        self._create_ui()

        # Load current values
        self._load_current_values()

    def _apply_dark_theme(self):
        """Applies minimalist dark theme to the window"""
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

        # CSS styling for widgets
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #dcdcdc;
            }
            QLabel {
                color: #dcdcdc;
                font-size: 11pt;
            }
            QSlider::groove:horizontal {
                border: 1px solid #555;
                height: 8px;
                background: #2a2a2a;
                margin: 2px 0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #4682b4;
                border: 1px solid #5c5c5c;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #5a9fd4;
            }
            QPushButton {
                background-color: #3a3a3a;
                color: #dcdcdc;
                border: 1px solid #555;
                padding: 8px 16px;
                border-radius: 4px;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border: 1px solid #777;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QTableWidget {
                background-color: #282828;
                color: #dcdcdc;
                gridline-color: #3a3a3a;
                border: 1px solid #555;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QTableWidget::item:selected {
                background-color: #4682b4;
            }
            QHeaderView::section {
                background-color: #3a3a3a;
                color: #dcdcdc;
                padding: 5px;
                border: 1px solid #555;
                font-weight: bold;
            }
            QKeySequenceEdit {
                background-color: #282828;
                color: #dcdcdc;
                border: 1px solid #555;
                padding: 5px;
                border-radius: 3px;
            }
        """)
    
    def _create_ui(self):
        """Creates the user interface"""
        layout = QVBoxLayout()

        # Section 1: OCR frequency
        ocr_section = self._create_ocr_section()
        layout.addWidget(ocr_section)

        # Section 2: Hotkeys
        hotkey_section = self._create_hotkey_section()
        layout.addWidget(hotkey_section)

        # Save & Close button
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.save_button = QPushButton("Save & Close")
        self.save_button.clicked.connect(self._save_and_close)
        button_layout.addWidget(self.save_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _create_ocr_section(self):
        """Creates the OCR configuration section"""
        widget = QWidget()
        layout = QVBoxLayout()

        # Title
        title = QLabel("OCR Scan Frequency")
        title.setStyleSheet("font-size: 12pt; font-weight: bold; color: #4682b4;")
        layout.addWidget(title)

        # Slider with value label
        slider_layout = QHBoxLayout()

        self.ocr_slider = QSlider(Qt.Orientation.Horizontal)
        self.ocr_slider.setMinimum(50)
        self.ocr_slider.setMaximum(2000)
        self.ocr_slider.setSingleStep(10)
        self.ocr_slider.setPageStep(100)
        self.ocr_slider.valueChanged.connect(self._update_ocr_label)

        self.ocr_value_label = QLabel("200 ms")
        self.ocr_value_label.setMinimumWidth(80)
        self.ocr_value_label.setStyleSheet("font-size: 11pt; color: #4682b4;")

        slider_layout.addWidget(self.ocr_slider)
        slider_layout.addWidget(self.ocr_value_label)

        layout.addLayout(slider_layout)

        # Description
        desc = QLabel("Interval between each OCR scan (50-2000 ms)")
        desc.setStyleSheet("font-size: 9pt; color: #999;")
        layout.addWidget(desc)

        widget.setLayout(layout)
        return widget
    
    def _create_hotkey_section(self):
        """Creates the hotkey configuration section"""
        widget = QWidget()
        layout = QVBoxLayout()

        # Title
        title = QLabel("Keyboard Shortcuts")
        title.setStyleSheet("font-size: 12pt; font-weight: bold; color: #4682b4;")
        layout.addWidget(title)

        # Hotkey table
        self.hotkey_table = QTableWidget()
        self.hotkey_table.setColumnCount(3)
        self.hotkey_table.setHorizontalHeaderLabels(["Action", "Shortcut", "Modify"])

        # Configure columns
        header = self.hotkey_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.hotkey_table.verticalHeader().setVisible(False)

        layout.addWidget(self.hotkey_table)

        # Description
        desc = QLabel("Click 'Modify' to change a shortcut")
        desc.setStyleSheet("font-size: 9pt; color: #999;")
        layout.addWidget(desc)

        widget.setLayout(layout)
        return widget

    def _update_ocr_label(self, value):
        """Updates the OCR value label"""
        self.ocr_value_label.setText(f"{value} ms")

    def _load_current_values(self):
        """Loads current values from configuration"""
        # Load OCR frequency
        scan_interval = self.config_manager.get_scan_interval()
        self.ocr_slider.setValue(scan_interval)

        # Load hotkeys
        hotkeys = self.config_manager.get_all_hotkeys()

        # Human-readable action names
        action_names = {
            'toggle_overlay': 'Show/Hide overlay',
            'open_options': 'Open options',
            'save_position': 'Save position',
            'open_poi_manager': 'Open POI manager'
        }
        
        self.hotkey_table.setRowCount(len(hotkeys))

        for row, (action, hotkey) in enumerate(hotkeys.items()):
            # Action column
            action_item = QTableWidgetItem(action_names.get(action, action))
            action_item.setFlags(action_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.hotkey_table.setItem(row, 0, action_item)

            # Shortcut column
            hotkey_item = QTableWidgetItem(hotkey)
            hotkey_item.setFlags(hotkey_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            hotkey_item.setData(Qt.ItemDataRole.UserRole, action)  # Store action
            self.hotkey_table.setItem(row, 1, hotkey_item)

            # Modify column (button)
            modify_button = QPushButton("Modify")
            modify_button.clicked.connect(lambda checked, r=row: self._modify_hotkey(r))
            self.hotkey_table.setCellWidget(row, 2, modify_button)

    def _modify_hotkey(self, row):
        """Opens a dialog to modify a hotkey"""
        action_item = self.hotkey_table.item(row, 1)
        action = action_item.data(Qt.ItemDataRole.UserRole)
        current_hotkey = action_item.text()

        # Create simple dialog to capture new hotkey
        dialog = HotkeyEditDialog(current_hotkey, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_hotkey = dialog.get_hotkey()
            if new_hotkey and new_hotkey != current_hotkey:
                # Update table
                action_item.setText(new_hotkey)
                logger.info(f"Hotkey modified: {action} -> {new_hotkey}")

    def _save_and_close(self):
        """Saves settings and closes the window"""
        try:
            # Save OCR frequency
            scan_interval = self.ocr_slider.value()
            self.config_manager.set_scan_interval(scan_interval)

            # Save hotkeys
            for row in range(self.hotkey_table.rowCount()):
                action_item = self.hotkey_table.item(row, 1)
                action = action_item.data(Qt.ItemDataRole.UserRole)
                new_hotkey = action_item.text()

                # Update hotkey in listener
                self.hotkey_listener.update_hotkey(action, new_hotkey)

            # Save configuration
            if self.config_manager.save():
                logger.info("Configuration saved successfully")

                # Emit signal
                self.options_saved.emit()

                # Show confirmation message
                QMessageBox.information(
                    self,
                    "Success",
                    "Settings have been saved successfully."
                )

                # Close window
                self.accept()
            else:
                QMessageBox.warning(
                    self,
                    "Error",
                    "Failed to save configuration."
                )
        except Exception as e:
            logger.error(f"Error saving options: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"An error occurred: {str(e)}"
            )


class HotkeyEditDialog(QDialog):
    """Simple dialog for editing a hotkey"""

    def __init__(self, current_hotkey, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Modify Shortcut")
        self.setModal(True)

        layout = QVBoxLayout()

        # Instruction label
        label = QLabel("Press the new key combination:")
        layout.addWidget(label)

        # Key sequence editor
        self.key_edit = QKeySequenceEdit(current_hotkey)
        layout.addWidget(self.key_edit)

        # Buttons
        button_layout = QHBoxLayout()

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)
        button_layout.addWidget(ok_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)

        # Apply same dark theme
        self.setPalette(parent.palette())
        self.setStyleSheet(parent.styleSheet())

    def get_hotkey(self):
        """Returns the entered hotkey in pynput format (e.g., 'cmd+shift+p')"""
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
