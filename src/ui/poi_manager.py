"""
POI (Points of Interest) Manager for SpaceDrive GPS.
Allows searching, adding, editing, deleting, and selecting POIs.
"""
import logging
import json
import os
import sys
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QMessageBox, QLineEdit, QWidget,
                             QAbstractItemView, QInputDialog,
                             QRadioButton, QButtonGroup, QComboBox, QMenu,
                             QFileDialog, QApplication)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QFontDatabase, QFont, QAction

import poi_io
from poi_categories import POI_CATEGORIES, label_for, color_for

logger = logging.getLogger(__name__)


class POIManagerWindow(QDialog):
    """
    POI Manager window with minimalist dark theme.
    Allows:
    - Searching POIs
    - Adding/Editing/Deleting POIs
    - Setting a destination
    - Navigating to a POI
    """

    # Signals emitted on actions
    destination_changed = pyqtSignal(dict)  # Emits selected POI
    goto_requested = pyqtSignal(dict)  # Emits POI to navigate to

    def __init__(self, navigation_engine, parent=None):
        """
        Initializes the POI manager window.

        Args:
            navigation_engine: NavigationEngine instance
            parent: Parent widget (optional)
        """
        super().__init__(parent)
        self.nav = navigation_engine
        self.all_pois = []  # Complete list of POIs
        self.filtered_pois = []  # List filtered by search

        self.setWindowTitle("POI Manager — SpaceDrive GPS")  # English-only app
        self.setMinimumWidth(900)
        self.setMinimumHeight(640)

        self._active_cat = None
        self._cat_buttons = {}

        # Apply dark theme
        self._apply_dark_theme()

        # Create UI
        self._create_ui()

        # Load POIs
        self._load_pois()

    def _apply_dark_theme(self):
        """Apply PADEK theme — global QSS handles all styling."""
        self.setFont(QFont("Roboto", 11))
        # Global padek-theme.qss applied automatically
    
    def _create_ui(self):
        """Creates the user interface."""
        layout = QVBoxLayout()
        layout.setSpacing(8)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search — name, coordinates, description…")
        self.search_input.textChanged.connect(self._filter_pois)
        layout.addWidget(self.search_input)

        # Category chips
        layout.addWidget(self._create_category_chips())

        # POI table — 7 columns: Name, Zone, Category, X, Y, Z, Description
        self.poi_table = QTableWidget()
        self.poi_table.setColumnCount(7)
        self.poi_table.setHorizontalHeaderLabels(
            ["NAME", "ZONE (OOC)", "CATEGORY", "X", "Y", "Z", "DESCRIPTION"]
        )

        # Roboto Bold for header (crisp at small sizes)
        h_font = QFont("Roboto", 8, QFont.Weight.Bold)
        self.poi_table.horizontalHeader().setFont(h_font)

        header = self.poi_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        self.poi_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.poi_table.customContextMenuRequested.connect(self._show_context_menu)
        self.poi_table.setSortingEnabled(True)
        self.poi_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.poi_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.poi_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.poi_table.doubleClicked.connect(self._goto_poi)
        self.poi_table.verticalHeader().setVisible(False)

        layout.addWidget(self.poi_table)

        # Action buttons
        button_layout = QHBoxLayout()

        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self._add_poi)
        button_layout.addWidget(self.add_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit_poi)
        button_layout.addWidget(self.edit_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("btn_danger")
        self.delete_button.clicked.connect(self._delete_poi)
        button_layout.addWidget(self.delete_button)

        self.import_button = QPushButton("Import from clipboard")
        self.import_button.clicked.connect(self._import_poi_from_clipboard)
        button_layout.addWidget(self.import_button)

        button_layout.addStretch()

        self.goto_button = QPushButton("GO")
        self.goto_button.setObjectName("btn_primary")
        self.goto_button.clicked.connect(self._goto_poi)
        button_layout.addWidget(self.goto_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def _create_category_chips(self):
        """Category chip bar for filtering by category."""
        from poi_categories import color_for
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # "TOUS" chip — emerald
        all_btn = QPushButton("TOUS")
        all_btn.setCheckable(True)
        all_btn.setChecked(True)
        all_btn.setObjectName("chip_all")
        all_btn.setFont(QFont("Roboto", 8, QFont.Weight.Bold))
        all_btn.clicked.connect(lambda: self._set_category_filter(None))
        all_btn.setStyleSheet(self._chip_style(active=True, color="#19C28A"))
        layout.addWidget(all_btn)
        self._cat_buttons[None] = all_btn

        for slug, label in POI_CATEGORIES:
            if not slug:          # skip empty slug (Uncategorized) in chip bar
                continue
            color = color_for(slug)
            btn = QPushButton(label.upper())
            btn.setCheckable(True)
            btn.setObjectName(f"chip_{slug}")
            btn.setFont(QFont("Roboto", 8, QFont.Weight.Bold))
            btn.clicked.connect(lambda _=False, s=slug: self._set_category_filter(s))
            btn.setStyleSheet(self._chip_style(active=False, color=color))
            layout.addWidget(btn)
            self._cat_buttons[slug] = btn

        layout.addStretch()
        return widget

    @staticmethod
    def _chip_style(active: bool, color: str) -> str:
        """Return QSS for a chip button using its category color (no px font-size)."""
        if active:
            return (
                f"QPushButton {{"
                f"  background: {color}26;"
                f"  border: 1.5px solid {color}80;"
                f"  color: {color};"
                f"  border-radius: 999px;"
                f"  padding: 4px 12px;"
                f"}}"
            )
        return (
            f"QPushButton {{"
            f"  background: rgba(238,243,246,0.04);"
            f"  border: 1px solid rgba(238,243,246,0.1);"
            f"  color: #7E8B97;"
            f"  border-radius: 999px;"
            f"  padding: 4px 12px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {color}18;"
            f"  border-color: {color}55;"
            f"  color: {color};"
            f"}}"
        )

    def _set_category_filter(self, slug):
        from poi_categories import color_for
        self._active_cat = slug
        for key, btn in self._cat_buttons.items():
            color = "#19C28A" if key is None else color_for(key)
            btn.setStyleSheet(self._chip_style(active=(key == slug), color=color))
        self._filter_pois(self.search_input.text())
    
    def _load_pois(self):
        """Loads user POIs from the NavigationEngine."""
        try:
            self.all_pois = []
            for poi in self.nav.user_poi or []:
                if not isinstance(poi, dict):
                    continue
                self.all_pois.append({
                    "name": poi.get("name", "Unknown"),
                    "x": poi.get("x", 0.0),
                    "y": poi.get("y", 0.0),
                    "z": poi.get("z", 0.0),
                    "description": poi.get("description", ""),
                    "location": poi.get("location", "Unknown"),
                    "ooc": poi.get("ooc"),
                    "kind": poi.get("kind", "space"),
                    "category": poi.get("category", ""),
                })

            self.filtered_pois = self.all_pois.copy()
            self._update_table()
            logger.info(f"{len(self.all_pois)} POIs loaded")
        except Exception as e:
            logger.exception(f"Error loading POIs: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load POIs: {str(e)}")
    
    def _update_table(self):
        """Updates the table with filtered POIs."""
        from poi_categories import color_for
        self.poi_table.setSortingEnabled(False)
        self.poi_table.setRowCount(len(self.filtered_pois))

        def fmt(value):
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return "?"

        for row, poi in enumerate(self.filtered_pois):
            slug = poi.get("category") or ""
            cat_color = color_for(slug)

            # Name
            name_item = QTableWidgetItem(str(poi.get("name", "")))
            name_item.setData(Qt.ItemDataRole.UserRole, poi)
            self.poi_table.setItem(row, 0, name_item)

            # OOC Zone
            ooc_str = poi.get("ooc") or "(legacy)"
            self.poi_table.setItem(row, 1, QTableWidgetItem(ooc_str))

            # Category — colored with Community Hub color
            cat_item = QTableWidgetItem(label_for(slug).upper())
            cat_item.setForeground(QColor(cat_color))
            cat_item.setFont(QFont("Roboto", 8, QFont.Weight.Bold))
            self.poi_table.setItem(row, 2, cat_item)

            # Coordinates
            x_item = QTableWidgetItem(fmt(poi.get("x")))
            x_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 3, x_item)

            y_item = QTableWidgetItem(fmt(poi.get("y")))
            y_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 4, y_item)

            z_item = QTableWidgetItem(fmt(poi.get("z")))
            z_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 5, z_item)

            # Description
            self.poi_table.setItem(row, 6, QTableWidgetItem(str(poi.get("description", ""))))

        self.poi_table.setSortingEnabled(True)
    
    def _filter_pois(self, search_text: str):
        """Filters POIs by category chip and search text."""
        search_text = search_text.lower()
        filtered = self.all_pois

        if self._active_cat is not None:
            filtered = [p for p in filtered if p.get("category") == self._active_cat]

        if search_text:
            filtered = [
                p for p in filtered
                if search_text in p["name"].lower()
                or search_text in p.get("description", "").lower()
                or search_text in str(p.get("x", ""))
                or search_text in str(p.get("y", ""))
                or search_text in str(p.get("z", ""))
                or search_text in label_for(p.get("category")).lower()
            ]

        self.filtered_pois = filtered
        self._update_table()


    def _get_selected_poi(self):
        """Returns the selected POI or None"""
        selected_rows = self.poi_table.selectedIndexes()
        if not selected_rows:
            return None

        row = selected_rows[0].row()
        name_item = self.poi_table.item(row, 0)
        return name_item.data(Qt.ItemDataRole.UserRole)

    def _add_poi(self):
        """Opens a dialog to add a new POI"""
        dialog = POIEditDialog(None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            poi_data = dialog.get_poi_data()

            # Add to NavigationEngine
            self.nav.add_user_point(
                poi_data["name"],
                poi_data["x"],
                poi_data["y"],
                poi_data["z"],
                poi_data.get("location", "Unknown"),
                kind=poi_data.get("kind", "space"),
                category=poi_data.get("category", ""),
            )

            # Reload POIs
            self._load_pois()

            logger.info(f"POI added: {poi_data['name']}")
            QMessageBox.information(self, "Success", f"POI '{poi_data['name']}' added successfully.")
    
    def _edit_poi(self):
        """Opens a dialog to edit the selected POI"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Warning", "Please select a POI to edit.")
            return

        dialog = POIEditDialog(poi, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_data = dialog.get_poi_data()

            # Find and update the POI in user_poi. Merge new_data into the
            # original dict so fields the dialog does not expose (notably
            # ``ooc``, set at save time from the player's zone) are preserved.
            for i, user_poi in enumerate(self.nav.user_poi):
                if (user_poi["name"] == poi["name"] and
                    user_poi["x"] == poi["x"] and
                    user_poi["y"] == poi["y"] and
                    user_poi["z"] == poi["z"]):
                    self.nav.user_poi[i].update(new_data)
                    break

            # Save
            self.nav.save_user_poi()

            # Reload POIs
            self._load_pois()

            logger.info(f"POI edited: {new_data['name']}")
            QMessageBox.information(self, "Success", f"POI '{new_data['name']}' modified successfully.")
    
    def _delete_poi(self):
        """Deletes the selected POI after confirmation"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Warning", "Please select a POI to delete.")
            return

        # Confirmation dialog
        reply = QMessageBox.question(
            self,
            "Confirmation",
            f"Are you sure you want to delete POI '{poi['name']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # Delete from NavigationEngine
            self.nav.user_poi = [
                p for p in self.nav.user_poi
                if not (p["name"] == poi["name"] and
                       p["x"] == poi["x"] and
                       p["y"] == poi["y"] and
                       p["z"] == poi["z"])
            ]

            # Save
            self.nav.save_user_poi()

            # Reload POIs
            self._load_pois()

            logger.info(f"POI deleted: {poi['name']}")
            QMessageBox.information(self, "Success", f"POI '{poi['name']}' deleted successfully.")

    def _goto_poi(self):
        """Immediately navigates to the selected POI"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Warning", "Please select a POI.")
            return

        # Emit both signals
        self.destination_changed.emit(poi)
        self.goto_requested.emit(poi)

        logger.info(f"Navigating to: {poi['name']}")
        QMessageBox.information(self, "Success", f"Navigation to {poi['name']} activated.")

        # Close window
        self.accept()

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos):
        """Right-click menu on the table — export actions."""
        index = self.poi_table.indexAt(pos)
        if not index.isValid():
            return

        # Select the row under the cursor so _get_selected_poi() picks it up
        self.poi_table.selectRow(index.row())
        poi = self._get_selected_poi()
        if not poi:
            return

        menu = QMenu(self)
        copy_action = QAction("Copy to clipboard", self)
        copy_action.triggered.connect(lambda: self._copy_poi_to_clipboard(poi))
        menu.addAction(copy_action)

        export_action = QAction("Export to JSON file...", self)
        export_action.triggered.connect(lambda: self._export_poi_to_file(poi))
        menu.addAction(export_action)

        menu.exec(self.poi_table.viewport().mapToGlobal(pos))

    def _copy_poi_to_clipboard(self, poi):
        try:
            payload = poi_io.to_json(poi)
            QApplication.clipboard().setText(payload)
            logger.info(f"POI copied to clipboard: {poi['name']}")
            QMessageBox.information(self, "Success", f"POI '{poi['name']}' copied to clipboard.")
        except Exception as e:
            logger.exception("Failed to copy POI to clipboard")
            QMessageBox.critical(self, "Error", f"Failed to copy POI: {e}")

    def _export_poi_to_file(self, poi):
        default_name = f"{poi.get('name', 'poi')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export POI", default_name, "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(poi_io.to_json(poi))
            logger.info(f"POI exported to {path}")
            QMessageBox.information(self, "Success", f"POI exported to {path}")
        except OSError as e:
            logger.exception("Failed to write POI file")
            QMessageBox.critical(self, "Error", f"Failed to write file: {e}")

    def _import_poi_from_clipboard(self):
        text = QApplication.clipboard().text()
        if not text or not text.strip():
            QMessageBox.warning(self, "Clipboard empty", "The clipboard is empty.")
            return

        try:
            poi_data = poi_io.parse_poi(text)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid POI", f"Clipboard does not contain a valid POI:\n\n{e}")
            return

        # Duplicate detection (case-insensitive name match)
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
                "Cancel = Abort",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Yes:
                self.nav.user_poi.pop(existing_idx)
            elif reply == QMessageBox.StandardButton.No:
                poi_data["name"] = self._next_free_name(poi_data["name"])

        self.nav.user_poi.append(poi_data)
        try:
            self.nav.save_user_poi()
        except Exception as e:
            logger.exception("Failed to save user POIs after import")
            QMessageBox.critical(self, "Error", f"Failed to save POI: {e}")
            # Roll back the in-memory append
            self.nav.user_poi.pop()
            return

        self._load_pois()
        logger.info(f"POI imported from clipboard: {poi_data['name']}")
        QMessageBox.information(self, "Success", f"POI '{poi_data['name']}' imported.")

    def _next_free_name(self, base_name):
        """Find a non-colliding name by appending ' (2)', ' (3)', ..."""
        existing = {str(p.get("name", "")).lower() for p in self.nav.user_poi}
        i = 2
        while f"{base_name} ({i})".lower() in existing:
            i += 1
        return f"{base_name} ({i})"


class POIEditDialog(QDialog):
    """Dialog for adding or editing a POI"""

    def __init__(self, poi_data=None, parent=None):
        super().__init__(parent)
        self.poi_data = poi_data
        self.is_edit = poi_data is not None

        self.setWindowTitle("Edit POI" if self.is_edit else "Add POI")
        self.setModal(True)
        self.setMinimumWidth(400)

        # Global padek-theme.qss handles all styling automatically

        self._create_ui()

        if self.is_edit:
            self._load_poi_data()

    def _create_ui(self):
        """Creates the dialog interface"""
        layout = QVBoxLayout()

        # Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Name:"))
        self.name_input = QLineEdit()
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)

        # Coordinates X
        x_layout = QHBoxLayout()
        x_layout.addWidget(QLabel("X:"))
        self.x_input = QLineEdit()
        x_layout.addWidget(self.x_input)
        layout.addLayout(x_layout)

        # Coordinates Y
        y_layout = QHBoxLayout()
        y_layout.addWidget(QLabel("Y:"))
        self.y_input = QLineEdit()
        y_layout.addWidget(self.y_input)
        layout.addLayout(y_layout)

        # Coordinates Z
        z_layout = QHBoxLayout()
        z_layout.addWidget(QLabel("Z:"))
        self.z_input = QLineEdit()
        z_layout.addWidget(self.z_input)
        layout.addLayout(z_layout)

        # Location
        location_layout = QHBoxLayout()
        location_layout.addWidget(QLabel("Location:"))
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("E.g.: MicroTech, Crusader...")
        location_layout.addWidget(self.location_input)
        layout.addLayout(location_layout)

        # Kind (surface = planet/moon, ignore Z for distance ; space = full 3D)
        kind_layout = QHBoxLayout()
        kind_layout.addWidget(QLabel("Type:"))
        self.surface_radio = QRadioButton("Surface (planet/moon)")
        self.space_radio = QRadioButton("Space")
        self.space_radio.setChecked(True)
        self._kind_group = QButtonGroup(self)
        self._kind_group.addButton(self.surface_radio)
        self._kind_group.addButton(self.space_radio)
        kind_layout.addWidget(self.surface_radio)
        kind_layout.addWidget(self.space_radio)
        layout.addLayout(kind_layout)

        # Category (SpaceDrive Community taxonomy)
        cat_layout = QHBoxLayout()
        cat_layout.addWidget(QLabel("Category:"))
        self.category_combo = QComboBox()
        for slug, label in POI_CATEGORIES:
            self.category_combo.addItem(label, slug)
        cat_layout.addWidget(self.category_combo)
        layout.addLayout(cat_layout)

        # Description
        desc_layout = QHBoxLayout()
        desc_layout.addWidget(QLabel("Description:"))
        self.desc_input = QLineEdit()
        desc_layout.addWidget(self.desc_input)
        layout.addLayout(desc_layout)

        # Buttons
        button_layout = QHBoxLayout()

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self._validate_and_accept)
        button_layout.addWidget(ok_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _load_poi_data(self):
        """Loads POI data into the fields"""
        self.name_input.setText(self.poi_data["name"])
        self.x_input.setText(str(self.poi_data["x"]))
        self.y_input.setText(str(self.poi_data["y"]))
        self.z_input.setText(str(self.poi_data["z"]))
        # "Unknown" is a sentinel set by the save-point hotkey when no
        # location is known. Don't surface it in the edit dialog — show the
        # field as empty so the placeholder hint is visible.
        location = self.poi_data.get("location", "")
        self.location_input.setText("" if location == "Unknown" else location)
        self.desc_input.setText(self.poi_data.get("description", ""))
        if self.poi_data.get("kind") == "surface":
            self.surface_radio.setChecked(True)
        else:
            self.space_radio.setChecked(True)
        slug = self.poi_data.get("category", "") or ""
        idx = self.category_combo.findData(slug)
        self.category_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _validate_and_accept(self):
        """Validates data and accepts the dialog"""
        # Check that all required fields are filled
        if not self.name_input.text():
            QMessageBox.warning(self, "Error", "Name is required.")
            return

        try:
            float(self.x_input.text())
            float(self.y_input.text())
            float(self.z_input.text())
        except ValueError:
            QMessageBox.warning(self, "Error", "Coordinates must be valid numbers.")
            return

        self.accept()

    def get_poi_data(self):
        """Returns the entered POI data"""
        return {
            "name": self.name_input.text(),
            "x": float(self.x_input.text()),
            "y": float(self.y_input.text()),
            "z": float(self.z_input.text()),
            "location": self.location_input.text() or "Unknown",
            "description": self.desc_input.text(),
            "kind": "surface" if self.surface_radio.isChecked() else "space",
            "category": self.category_combo.currentData() or "",
        }
