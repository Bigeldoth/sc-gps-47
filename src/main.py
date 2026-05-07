import sys
import os
import logging
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout,
                             QWidget, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QAction, QColor, QCursor
import keyboard
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import NavigationEngine

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
        self.ocr = OCRProcessor()
        self._running = True

    def process(self):
        if not self._running:
            return
        try:
            images = self.capture.capture()
            data = self.ocr.extract_data(images)
            self.result_ready.emit(data)
        except Exception as e:
            logger.error(f"Erreur worker GPS : {e}")
            self.result_ready.emit({"x": None, "y": None, "z": None, "location": "Unknown", "error": str(e)})

    def stop(self):
        self._running = False
        self.ocr.shutdown()


class GPSOverlay(QMainWindow):
    save_point_signal = pyqtSignal()
    trigger_worker = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.is_interactive = False
        self.current_data = {"x": None, "y": None, "z": None, "location": "Unknown"}
        self._worker_busy = False

        # UI
        self.central_widget = QWidget()
        self.layout = QVBoxLayout()

        self.status_label = QLabel("MODE: NAVIGATION (Shift+F2 pour interagir)")
        self.status_label.setStyleSheet("color: #ffffff; font-size: 10px; background-color: rgba(50, 50, 50, 150);")

        self.location_label = QLabel("SYSTÈME: Recherche...")
        self.location_label.setStyleSheet("color: #ffaa00; font-family: 'Consolas'; font-size: 12px; font-weight: bold; background-color: rgba(0, 0, 0, 100);")

        self.pos_label = QLabel("Recherche de coordonnées...")
        self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Consolas'; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

        self.dist_label = QLabel("")
        self.dist_label.setStyleSheet("color: #00ffff; font-family: 'Consolas'; font-size: 18px; font-weight: bold;")

        self.layout.addWidget(self.status_label)
        self.layout.addWidget(self.location_label)
        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(self.dist_label)
        self.central_widget.setLayout(self.layout)
        self.setCentralWidget(self.central_widget)

        self.init_window_properties()

        self.setup_tray_icon()

        self._setup_worker_thread()

        try:
            interval = config.getint('Settings', 'refresh_interval_ms', fallback=2000)
        except Exception:
            interval = 2000
        self.timer = QTimer()
        self.timer.timeout.connect(self._request_update)
        self.timer.start(interval)

        self.setup_hotkeys()

        self.save_point_signal.connect(self.prompt_save_point)

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
            self.pos_label.setText(f"Erreur: {data['error']}")
            return

        self.current_data = data

        if data["location"] != "Unknown":
            self.location_label.setText(f"SYSTÈME: {data['location']}")
        else:
            self.location_label.setText("SYSTÈME: Recherche...")

        if data["x"] is not None:
            self.pos_label.setText(f"X: {data['x']:.3f} km | Y: {data['y']:.3f} km | Z: {data['z']:.3f} km")
            self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Consolas'; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

            dist = self.nav.calculate_distance(data)
            if dist:
                self.dist_label.setText(f"CIBLE: {self.nav.target['name']}\nDIST: {dist/1000:.2f} km")
            else:
                self.dist_label.setText("PAS DE CIBLE")
        else:
            self.pos_label.setText("X: --- | Y: --- | Z: --- (Scan en cours...)")
            self.pos_label.setStyleSheet("color: #ffaa00; font-family: 'Consolas'; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

    def init_window_properties(self):
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
            self.show()

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))

        tray_menu = QMenu()

        self.toggle_action = QAction("Masquer l'overlay (Shift+F1)", self)
        self.toggle_action.triggered.connect(self.toggle_overlay)
        tray_menu.addAction(self.toggle_action)

        self.interact_action = QAction("Activer Interaction (Shift+F2)", self)
        self.interact_action.triggered.connect(self.toggle_interaction)
        tray_menu.addAction(self.interact_action)

        tray_menu.addSeparator()

        poi_menu = tray_menu.addMenu("Points d'intérêt")

        save_action = QAction("Enregistrer position (Shift+F3)", self)
        save_action.triggered.connect(self.prompt_save_point)
        poi_menu.addAction(save_action)

        select_poi_action = QAction("Choisir une destination...", self)
        select_poi_action.triggered.connect(self.show_poi_selector)
        poi_menu.addAction(select_poi_action)

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

        self.tray_icon.showMessage(
            "SpaceDrive GPS",
            "Overlay GPS actif ! Utilisez Shift+F1 pour afficher/masquer.",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def setup_hotkeys(self):
        try:
            keyboard.add_hotkey('shift+f1', self.toggle_overlay)
            keyboard.add_hotkey('shift+f2', self.toggle_interaction)
            keyboard.add_hotkey('shift+f3', lambda: self.save_point_signal.emit())
        except Exception as e:
            print(f"Erreur configuration hotkeys: {e}")

    def toggle_overlay(self):
        if self.is_visible:
            self.hide()
            self.is_visible = False
            self.toggle_action.setText("Afficher l'overlay (Shift+F1)")
        else:
            self.show()
            self.is_visible = True
            self.toggle_action.setText("Masquer l'overlay (Shift+F1)")

    def toggle_interaction(self):
        try:
            logger.debug("Opening Options window via Shift+F2")
            self.show_options_window()
        except Exception as e:
            logger.error(f"Erreur lors de l'ouverture des options : {e}")
            self.tray_icon.showMessage("Erreur", f"Impossible d'ouvrir les options : {e}", QSystemTrayIcon.MessageIcon.Warning)

    def show_interaction_menu(self):
        menu = QMenu()

        save_action = QAction("Enregistrer Position (Shift+F3)", self)
        save_action.triggered.connect(self.prompt_save_point)
        menu.addAction(save_action)

        select_poi_action = QAction("Choisir Destination", self)
        select_poi_action.triggered.connect(self.show_poi_selector)
        menu.addAction(select_poi_action)

        data_menu = menu.addMenu("Gestion des Données")

        export_action = QAction("Exporter Points (JSON)", self)
        export_action.triggered.connect(self.export_data)
        data_menu.addAction(export_action)

        import_action = QAction("Importer Points (JSON)", self)
        import_action.triggered.connect(self.import_data)
        data_menu.addAction(import_action)

        try:
            menu.exec(QCursor.pos())
        except Exception as e:
            logger.error(f"Erreur lors de l'affichage du menu : {e}")
            self.tray_icon.showMessage("Erreur", f"Impossible d'afficher le menu : {e}", QSystemTrayIcon.MessageIcon.Warning)

    def prompt_save_point(self):
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
        keyboard.unhook_all()
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
