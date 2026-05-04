import sys
import os
import logging
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout, 
                             QWidget, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QAction, QColor
import keyboard
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import NavigationEngine

# Configuration et logging
config = configparser.ConfigParser()
config.read('config.ini')

logging.basicConfig(
    level=getattr(logging, config.get('Logging', 'level', fallback='INFO')),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=config.get('Logging', 'file', fallback='spacedrive.log')
)
logger = logging.getLogger(__name__)

class GPSOverlay(QMainWindow):
    # Signal pour gérer les fenêtres de dialogue depuis le thread principal
    save_point_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.capture = ScreenCapture()
        self.ocr = OCRProcessor()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.is_interactive = False
        self.current_data = {"x": None, "y": None, "z": None, "location": "Unknown"}

        # UI
        self.central_widget = QWidget()
        self.layout = QVBoxLayout()
        
        self.status_label = QLabel("MODE: NAVIGATION (Shift+F2 pour interagir)")
        self.status_label.setStyleSheet("color: #ffffff; font-size: 10px; background-color: rgba(50, 50, 50, 150);")
        
        self.pos_label = QLabel("Recherche de coordonnées...")
        self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Consolas'; font-size: 14px; background-color: rgba(0, 0, 0, 100);")
        
        self.dist_label = QLabel("")
        self.dist_label.setStyleSheet("color: #00ffff; font-family: 'Consolas'; font-size: 18px; font-weight: bold;")
        
        self.layout.addWidget(self.status_label)
        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(self.dist_label)
        self.central_widget.setLayout(self.layout)
        self.setCentralWidget(self.central_widget)

        # Configuration de la fenêtre Overlay (doit être fait après la création de central_widget)
        self.init_window_properties()

        # System Tray Icon
        self.setup_tray_icon()

        # Timer pour la mise à jour (toutes les secondes)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_gps)
        self.timer.start(1000)
        
        # Hotkeys globaux
        self.setup_hotkeys()
        
        # Connexion du signal de sauvegarde
        self.save_point_signal.connect(self.prompt_save_point)

    def init_window_properties(self):
        """Initialise les propriétés de la fenêtre selon le mode interactif"""
        flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint
        if not self.is_interactive:
            flags |= Qt.WindowType.WindowTransparentForInput
        
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 350, 250)
        
        if self.is_interactive:
            self.central_widget.setStyleSheet("background-color: rgba(20, 20, 20, 200); border: 1px solid #555;")
            self.status_label.setText("MODE: INTERACTIF (Shift+F2 pour verrouiller)")
        else:
            self.central_widget.setStyleSheet("background-color: transparent; border: none;")
            self.status_label.setText("MODE: NAVIGATION (Shift+F2 pour interagir)")
        
        if self.isVisible():
            self.show() # Forcer la mise à jour des flags

    def setup_tray_icon(self):
        """Configure l'icône dans la system tray"""
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))
        
        tray_menu = QMenu()
        
        # Navigation
        self.toggle_action = QAction("Masquer l'overlay (Shift+F1)", self)
        self.toggle_action.triggered.connect(self.toggle_overlay)
        tray_menu.addAction(self.toggle_action)
        
        self.interact_action = QAction("Activer Interaction (Shift+F2)", self)
        self.interact_action.triggered.connect(self.toggle_interaction)
        tray_menu.addAction(self.interact_action)

        tray_menu.addSeparator()

        # Points d'intérêt
        poi_menu = tray_menu.addMenu("Points d'intérêt")
        
        save_action = QAction("Enregistrer position (Shift+F3)", self)
        save_action.triggered.connect(self.prompt_save_point)
        poi_menu.addAction(save_action)
        
        select_poi_action = QAction("Choisir une destination...", self)
        select_poi_action.triggered.connect(self.show_poi_selector)
        poi_menu.addAction(select_poi_action)

        # Données
        data_menu = tray_menu.addMenu("Données")
        
        export_action = QAction("Exporter mes points (JSON)", self)
        export_action.triggered.connect(self.export_data)
        data_menu.addAction(export_action)
        
        import_action = QAction("Importer des points (JSON)", self)
        import_action.triggered.connect(self.import_data)
        data_menu.addAction(import_action)
        
        tray_menu.addSeparator()
        
        quit_action = QAction("Quitter", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
        # Message de démarrage
        self.tray_icon.showMessage(
            "SpaceDrive GPS",
            "Overlay GPS actif ! Utilisez Shift+F1 pour afficher/masquer.",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )
    
    def setup_hotkeys(self):
        """Configure les hotkeys globaux"""
        try:
            keyboard.add_hotkey('shift+f1', self.toggle_overlay)
            keyboard.add_hotkey('shift+f2', self.toggle_interaction)
            keyboard.add_hotkey('shift+f3', lambda: self.save_point_signal.emit())
        except Exception as e:
            print(f"Erreur configuration hotkeys: {e}")
    
    def toggle_overlay(self):
        """Affiche ou masque l'overlay"""
        if self.is_visible:
            self.hide()
            self.is_visible = False
            self.toggle_action.setText("Afficher l'overlay (Shift+F1)")
        else:
            self.show()
            self.is_visible = True
            self.toggle_action.setText("Masquer l'overlay (Shift+F1)")

    def toggle_interaction(self):
        """Bascule entre le mode transparent et interactif"""
        self.is_interactive = not self.is_interactive
        self.init_window_properties()
        self.interact_action.setText("Désactiver Interaction (Shift+F2)" if self.is_interactive else "Activer Interaction (Shift+F2)")

    def prompt_save_point(self):
        """Ouvre une fenêtre pour nommer et enregistrer le point actuel"""
        if self.current_data["x"] is None:
            self.tray_icon.showMessage("Erreur", "Coordonnées non détectées. Impossible d'enregistrer.", QSystemTrayIcon.MessageIcon.Warning)
            return

        name, ok = QInputDialog.getText(self, "Enregistrer Point", "Nom du point d'intérêt :")
        if ok and name:
            self.nav.add_user_point(
                name, 
                self.current_data["x"], 
                self.current_data["y"], 
                self.current_data["z"], 
                self.current_data["location"]
            )
            self.tray_icon.showMessage("Succès", f"Point '{name}' enregistré !", QSystemTrayIcon.MessageIcon.Information)

    def show_poi_selector(self):
        """Affiche un menu pour choisir une destination"""
        pois = self.nav.get_all_poi_for_location("All")
        if not pois:
            self.tray_icon.showMessage("Info", "Aucun point enregistré.", QSystemTrayIcon.MessageIcon.Information)
            return

        menu = QMenu()
        for poi in pois:
            action = QAction(f"{poi['name']} ({poi.get('location', 'Unknown')})", self)
            action.triggered.connect(lambda checked, p=poi: self.nav.set_target(p['x'], p['y'], p['z'], p['name']))
            menu.addAction(action)
        
        menu.exec(self.tray_icon.geometry().center())

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exporter les points", "", "JSON Files (*.json)")
        if path:
            if self.nav.export_points(path):
                self.tray_icon.showMessage("Succès", "Points exportés avec succès.", QSystemTrayIcon.MessageIcon.Information)

    def import_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importer des points", "", "JSON Files (*.json)")
        if path:
            if self.nav.import_points(path):
                self.tray_icon.showMessage("Succès", "Points importés avec succès.", QSystemTrayIcon.MessageIcon.Information)
    
    def quit_application(self):
        """Quitte proprement l'application"""
        keyboard.unhook_all()
        self.tray_icon.hide()
        QApplication.quit()
    
    def closeEvent(self, event):
        """Empêche la fermeture de la fenêtre, la masque à la place"""
        event.ignore()
        self.toggle_overlay()

    def update_gps(self):
        try:
            # 1. Capture & OCR
            screenshot = self.capture.capture()
            data = self.ocr.extract_data(screenshot)
            self.current_data = data
            
            if data["x"] is not None:
                self.pos_label.setText(f"LOC: {data['location']}\nX: {data['x']:.1f}\nY: {data['y']:.1f}\nZ: {data['z']:.1f}")
                
                # 2. Calcul distance si une cible est définie
                dist = self.nav.calculate_distance(data)
                if dist:
                    self.dist_label.setText(f"CIBLE: {self.nav.target['name']}\nDIST: {dist/1000:.2f} km")
                else:
                    self.dist_label.setText("PAS DE CIBLE")
            else:
                self.pos_label.setText("Scan UI en cours (r_displayinfo 3)...")
                
        except Exception as e:
            self.pos_label.setText(f"Erreur: {str(e)}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = GPSOverlay()
    overlay.show()
    sys.exit(app.exec())
