"""
Gestionnaire de Points d'Intérêt pour SpaceDrive GPS.
Permet de rechercher, ajouter, éditer, supprimer et sélectionner des POI.
"""
import logging
import json
import os
import sys
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QMessageBox, QLineEdit, QWidget,
                             QAbstractItemView, QInputDialog)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor

logger = logging.getLogger(__name__)


class POIManagerWindow(QDialog):
    """
    Fenêtre de gestion des Points d'Intérêt avec style sombre minimaliste.
    Permet de :
    - Rechercher des POI
    - Ajouter/Éditer/Supprimer des POI
    - Définir une destination
    - Naviguer vers un POI
    """
    
    # Signaux émis lors des actions
    destination_changed = pyqtSignal(dict)  # Émet le POI sélectionné
    goto_requested = pyqtSignal(dict)  # Émet le POI vers lequel naviguer
    
    def __init__(self, navigation_engine, parent=None):
        """
        Initialise la fenêtre de gestion des POI.
        
        Args:
            navigation_engine: Instance de NavigationEngine
            parent: Widget parent (optionnel)
        """
        super().__init__(parent)
        self.nav = navigation_engine
        self.all_pois = []  # Liste complète des POI
        self.filtered_pois = []  # Liste filtrée par la recherche
        
        self.setWindowTitle("Gestion des Points d'Intérêt")
        self.setMinimumWidth(800)
        self.setMinimumHeight(600)
        
        # Appliquer le thème sombre
        self._apply_dark_theme()
        
        # Créer l'interface
        self._create_ui()
        
        # Charger les POI
        self._load_pois()
    
    def _apply_dark_theme(self):
        """Applique un thème sombre minimaliste à la fenêtre"""
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
        
        # Style CSS pour les widgets
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #dcdcdc;
            }
            QLabel {
                color: #dcdcdc;
                font-size: 11pt;
            }
            QLineEdit {
                background-color: #282828;
                color: #dcdcdc;
                border: 1px solid #555;
                padding: 8px;
                border-radius: 4px;
                font-size: 10pt;
            }
            QLineEdit:focus {
                border: 1px solid #4682b4;
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
            QPushButton#goto_button {
                background-color: #2d5a2d;
                border: 1px solid #3a7a3a;
            }
            QPushButton#goto_button:hover {
                background-color: #3a7a3a;
            }
            QPushButton#delete_button {
                background-color: #5a2d2d;
                border: 1px solid #7a3a3a;
            }
            QPushButton#delete_button:hover {
                background-color: #7a3a3a;
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
        """)
    
    def _create_ui(self):
        """Crée l'interface utilisateur"""
        layout = QVBoxLayout()
        
        # Barre de recherche
        search_layout = QHBoxLayout()
        search_label = QLabel("Rechercher :")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Nom, coordonnées ou description...")
        self.search_input.textChanged.connect(self._filter_pois)
        
        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_input)
        layout.addLayout(search_layout)
        
        # Tableau des POI
        self.poi_table = QTableWidget()
        self.poi_table.setColumnCount(6)
        self.poi_table.setHorizontalHeaderLabels(["Nom", "Zone (OOC)", "X", "Y", "Z", "Description"])

        # Configurer les colonnes
        header = self.poi_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        
        # Activer le tri
        self.poi_table.setSortingEnabled(True)
        
        # Sélection d'une seule ligne à la fois
        self.poi_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.poi_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        
        # Double-clic pour définir comme destination
        self.poi_table.doubleClicked.connect(self._set_as_destination)
        
        self.poi_table.verticalHeader().setVisible(False)
        
        layout.addWidget(self.poi_table)
        
        # Boutons d'action
        button_layout = QHBoxLayout()
        
        self.add_button = QPushButton("Ajouter")
        self.add_button.clicked.connect(self._add_poi)
        button_layout.addWidget(self.add_button)
        
        self.edit_button = QPushButton("Éditer")
        self.edit_button.clicked.connect(self._edit_poi)
        button_layout.addWidget(self.edit_button)
        
        self.delete_button = QPushButton("Supprimer")
        self.delete_button.setObjectName("delete_button")
        self.delete_button.clicked.connect(self._delete_poi)
        button_layout.addWidget(self.delete_button)
        
        button_layout.addStretch()
        
        self.destination_button = QPushButton("Définir comme destination")
        self.destination_button.clicked.connect(self._set_as_destination)
        button_layout.addWidget(self.destination_button)
        
        self.goto_button = QPushButton("Aller")
        self.goto_button.setObjectName("goto_button")
        self.goto_button.clicked.connect(self._goto_poi)
        button_layout.addWidget(self.goto_button)
        
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def _load_pois(self):
        """Charge tous les POI depuis le NavigationEngine"""
        try:
            self.all_pois = []
            
            # Charger les POI du système (depuis poi.json)
            if isinstance(self.nav.poi_data, list):
                for system in self.nav.poi_data:
                    if not isinstance(system, dict):
                        continue
                    for body in system.get("bodies", []):
                        if not isinstance(body, dict):
                            continue
                        for poi in body.get("poi", []):
                            if not isinstance(poi, dict):
                                continue
                            poi_entry = {
                                "name": poi.get("name", "Unknown"),
                                "x": poi.get("x", 0.0),
                                "y": poi.get("y", 0.0),
                                "z": poi.get("z", 0.0),
                                "description": poi.get("description", ""),
                                "location": body.get("name", "Unknown System"),
                                "ooc": poi.get("ooc"),
                                "source": "system",
                            }
                            self.all_pois.append(poi_entry)
            else:
                logger.warning("Structure de poi_data invalide (attendu: liste)")
            
            # Charger les POI utilisateur
            for poi in self.nav.user_poi or []:
                if not isinstance(poi, dict):
                    continue
                poi_entry = {
                    "name": poi.get("name", "Unknown"),
                    "x": poi.get("x", 0.0),
                    "y": poi.get("y", 0.0),
                    "z": poi.get("z", 0.0),
                    "description": poi.get("description", ""),
                    "location": poi.get("location", "Unknown"),
                    "ooc": poi.get("ooc"),
                    "source": "user",
                }
                self.all_pois.append(poi_entry)
            
            # Afficher tous les POI
            self.filtered_pois = self.all_pois.copy()
            self._update_table()
            
            logger.info(f"{len(self.all_pois)} POI chargés")
        except Exception as e:
            logger.exception(f"Erreur lors du chargement des POI : {e}")
            QMessageBox.critical(self, "Erreur", f"Impossible de charger les POI : {str(e)}")
    
    def _update_table(self):
        """Met à jour le tableau avec les POI filtrés"""
        self.poi_table.setSortingEnabled(False)  # Désactiver le tri pendant la mise à jour
        self.poi_table.setRowCount(len(self.filtered_pois))
        
        def fmt(value):
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return "?"

        for row, poi in enumerate(self.filtered_pois):
            # Nom
            name_item = QTableWidgetItem(str(poi.get("name", "")))
            name_item.setData(Qt.ItemDataRole.UserRole, poi)  # Stocker le POI complet
            self.poi_table.setItem(row, 0, name_item)

            # Zone OOC ('legacy' si absent — POI sauvegardé avant le refactor)
            ooc_str = poi.get("ooc") or "(legacy)"
            ooc_item = QTableWidgetItem(ooc_str)
            self.poi_table.setItem(row, 1, ooc_item)

            # Coordonnées
            x_item = QTableWidgetItem(fmt(poi.get("x")))
            x_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 2, x_item)

            y_item = QTableWidgetItem(fmt(poi.get("y")))
            y_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 3, y_item)

            z_item = QTableWidgetItem(fmt(poi.get("z")))
            z_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.poi_table.setItem(row, 4, z_item)

            # Description
            desc_item = QTableWidgetItem(str(poi.get("description", "")))
            self.poi_table.setItem(row, 5, desc_item)

        self.poi_table.setSortingEnabled(True)  # Réactiver le tri
    
    def _filter_pois(self, search_text):
        """Filtre les POI selon le texte de recherche"""
        search_text = search_text.lower()
        
        if not search_text:
            self.filtered_pois = self.all_pois.copy()
        else:
            self.filtered_pois = [
                poi for poi in self.all_pois
                if search_text in poi["name"].lower()
                or search_text in poi["description"].lower()
                or search_text in str(poi["x"])
                or search_text in str(poi["y"])
                or search_text in str(poi["z"])
            ]
        
        self._update_table()
    
    def _get_selected_poi(self):
        """Retourne le POI sélectionné ou None"""
        selected_rows = self.poi_table.selectedIndexes()
        if not selected_rows:
            return None
        
        row = selected_rows[0].row()
        name_item = self.poi_table.item(row, 0)
        return name_item.data(Qt.ItemDataRole.UserRole)
    
    def _add_poi(self):
        """Ouvre un dialogue pour ajouter un nouveau POI"""
        dialog = POIEditDialog(None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            poi_data = dialog.get_poi_data()
            
            # Ajouter au NavigationEngine
            self.nav.add_user_point(
                poi_data["name"],
                poi_data["x"],
                poi_data["y"],
                poi_data["z"],
                poi_data.get("location", "Unknown")
            )
            
            # Recharger les POI
            self._load_pois()
            
            logger.info(f"POI ajouté : {poi_data['name']}")
            QMessageBox.information(self, "Succès", f"POI '{poi_data['name']}' ajouté avec succès.")
    
    def _edit_poi(self):
        """Ouvre un dialogue pour éditer le POI sélectionné"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Attention", "Veuillez sélectionner un POI à éditer.")
            return
        
        if poi["source"] == "system":
            QMessageBox.warning(self, "Attention", "Les POI système ne peuvent pas être édités.")
            return
        
        dialog = POIEditDialog(poi, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_data = dialog.get_poi_data()
            
            # Trouver et mettre à jour le POI dans user_poi
            for i, user_poi in enumerate(self.nav.user_poi):
                if (user_poi["name"] == poi["name"] and
                    user_poi["x"] == poi["x"] and
                    user_poi["y"] == poi["y"] and
                    user_poi["z"] == poi["z"]):
                    self.nav.user_poi[i] = {
                        "name": new_data["name"],
                        "x": new_data["x"],
                        "y": new_data["y"],
                        "z": new_data["z"],
                        "description": new_data.get("description", ""),
                        "location": new_data.get("location", "Unknown")
                    }
                    break
            
            # Sauvegarder
            self.nav.save_user_poi()
            
            # Recharger les POI
            self._load_pois()
            
            logger.info(f"POI édité : {new_data['name']}")
            QMessageBox.information(self, "Succès", f"POI '{new_data['name']}' modifié avec succès.")
    
    def _delete_poi(self):
        """Supprime le POI sélectionné après confirmation"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Attention", "Veuillez sélectionner un POI à supprimer.")
            return
        
        if poi["source"] == "system":
            QMessageBox.warning(self, "Attention", "Les POI système ne peuvent pas être supprimés.")
            return
        
        # Dialogue de confirmation
        reply = QMessageBox.question(
            self,
            "Confirmation",
            f"Êtes-vous sûr de vouloir supprimer le POI '{poi['name']}' ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # Supprimer du NavigationEngine
            self.nav.user_poi = [
                p for p in self.nav.user_poi
                if not (p["name"] == poi["name"] and
                       p["x"] == poi["x"] and
                       p["y"] == poi["y"] and
                       p["z"] == poi["z"])
            ]
            
            # Sauvegarder
            self.nav.save_user_poi()
            
            # Recharger les POI
            self._load_pois()
            
            logger.info(f"POI supprimé : {poi['name']}")
            QMessageBox.information(self, "Succès", f"POI '{poi['name']}' supprimé avec succès.")
    
    def _set_as_destination(self):
        """Définit le POI sélectionné comme destination"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Attention", "Veuillez sélectionner un POI.")
            return
        
        # Émettre le signal
        self.destination_changed.emit(poi)
        
        logger.info(f"Destination définie : {poi['name']}")
        QMessageBox.information(self, "Succès", f"Destination définie : {poi['name']}")
    
    def _goto_poi(self):
        """Navigue immédiatement vers le POI sélectionné"""
        poi = self._get_selected_poi()
        if not poi:
            QMessageBox.warning(self, "Attention", "Veuillez sélectionner un POI.")
            return
        
        # Émettre les deux signaux
        self.destination_changed.emit(poi)
        self.goto_requested.emit(poi)
        
        logger.info(f"Navigation vers : {poi['name']}")
        QMessageBox.information(self, "Succès", f"Navigation vers {poi['name']} activée.")
        
        # Fermer la fenêtre
        self.accept()


class POIEditDialog(QDialog):
    """Dialogue pour ajouter ou éditer un POI"""
    
    def __init__(self, poi_data=None, parent=None):
        super().__init__(parent)
        self.poi_data = poi_data
        self.is_edit = poi_data is not None
        
        self.setWindowTitle("Éditer le POI" if self.is_edit else "Ajouter un POI")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        # Appliquer le même thème
        self.setPalette(parent.palette())
        self.setStyleSheet(parent.styleSheet())
        
        self._create_ui()
        
        if self.is_edit:
            self._load_poi_data()
    
    def _create_ui(self):
        """Crée l'interface du dialogue"""
        layout = QVBoxLayout()
        
        # Nom
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Nom :"))
        self.name_input = QLineEdit()
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)
        
        # Coordonnées X
        x_layout = QHBoxLayout()
        x_layout.addWidget(QLabel("X :"))
        self.x_input = QLineEdit()
        x_layout.addWidget(self.x_input)
        layout.addLayout(x_layout)
        
        # Coordonnées Y
        y_layout = QHBoxLayout()
        y_layout.addWidget(QLabel("Y :"))
        self.y_input = QLineEdit()
        y_layout.addWidget(self.y_input)
        layout.addLayout(y_layout)
        
        # Coordonnées Z
        z_layout = QHBoxLayout()
        z_layout.addWidget(QLabel("Z :"))
        self.z_input = QLineEdit()
        z_layout.addWidget(self.z_input)
        layout.addLayout(z_layout)
        
        # Location
        location_layout = QHBoxLayout()
        location_layout.addWidget(QLabel("Localisation :"))
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("Ex: MicroTech, Crusader...")
        location_layout.addWidget(self.location_input)
        layout.addLayout(location_layout)
        
        # Description
        desc_layout = QHBoxLayout()
        desc_layout.addWidget(QLabel("Description :"))
        self.desc_input = QLineEdit()
        desc_layout.addWidget(self.desc_input)
        layout.addLayout(desc_layout)
        
        # Boutons
        button_layout = QHBoxLayout()
        
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self._validate_and_accept)
        button_layout.addWidget(ok_button)
        
        cancel_button = QPushButton("Annuler")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def _load_poi_data(self):
        """Charge les données du POI dans les champs"""
        self.name_input.setText(self.poi_data["name"])
        self.x_input.setText(str(self.poi_data["x"]))
        self.y_input.setText(str(self.poi_data["y"]))
        self.z_input.setText(str(self.poi_data["z"]))
        self.location_input.setText(self.poi_data.get("location", ""))
        self.desc_input.setText(self.poi_data.get("description", ""))
    
    def _validate_and_accept(self):
        """Valide les données et accepte le dialogue"""
        # Vérifier que tous les champs requis sont remplis
        if not self.name_input.text():
            QMessageBox.warning(self, "Erreur", "Le nom est requis.")
            return
        
        try:
            float(self.x_input.text())
            float(self.y_input.text())
            float(self.z_input.text())
        except ValueError:
            QMessageBox.warning(self, "Erreur", "Les coordonnées doivent être des nombres valides.")
            return
        
        self.accept()
    
    def get_poi_data(self):
        """Retourne les données du POI saisies"""
        return {
            "name": self.name_input.text(),
            "x": float(self.x_input.text()),
            "y": float(self.y_input.text()),
            "z": float(self.z_input.text()),
            "location": self.location_input.text() or "Unknown",
            "description": self.desc_input.text()
        }
