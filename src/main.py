import sys
import os
import logging
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout,
                             QWidget, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QAction, QColor, QCursor
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import NavigationEngine
from config_manager import ConfigManager
from hotkey_listener import HotkeyListener
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
        self.ocr = OCRProcessor(engine=ocr_engine)
        logger.info(f"Moteur OCR initialisé : {ocr_engine}")
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
        self.capture.stop()
        self.ocr.shutdown()


class GPSOverlay(QMainWindow):
    save_point_signal = pyqtSignal()
    trigger_worker = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.current_data = {"x": None, "y": None, "z": None, "location": "Unknown"}
        self._worker_busy = False
        self.options_window = None
        self.poi_manager_window = None

        # UI
        self.central_widget = QWidget()
        self.layout = QVBoxLayout()

        self.status_label = QLabel("MODE: NAVIGATION")
        self.status_label.setStyleSheet("color: #ffffff; font-size: 10px; background-color: rgba(50, 50, 50, 150);")

        self.location_label = QLabel("SYSTÈME: Recherche...")
        self.location_label.setStyleSheet("color: #ffaa00; font-family: 'Menlo', 'Consolas', monospace; font-size: 12px; font-weight: bold; background-color: rgba(0, 0, 0, 100);")

        self.pos_label = QLabel("Recherche de coordonnées...")
        self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Menlo', 'Consolas', monospace; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

        self.dist_label = QLabel("")
        self.dist_label.setStyleSheet("color: #00ffff; font-family: 'Menlo', 'Consolas', monospace; font-size: 18px; font-weight: bold;")

        self.layout.addWidget(self.status_label)
        self.layout.addWidget(self.location_label)
        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(self.dist_label)
        self.central_widget.setLayout(self.layout)
        self.setCentralWidget(self.central_widget)

        self.init_window_properties()
        self.setup_tray_icon()
        self._setup_worker_thread()

        scan_interval = self.config_manager.get_scan_interval()
        self.timer = QTimer()
        self.timer.timeout.connect(self._request_update)
        self.timer.start(scan_interval)

        self.save_point_signal.connect(self.prompt_save_point)

        self.hotkey_listener = HotkeyListener(self.config_manager)
        # Hotkeys émis depuis le thread pynput → QueuedConnection pour traiter sur le thread Qt
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
        logger.debug("Hotkey 'save_position' détecté")
        self.save_point_signal.emit()

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
            self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Menlo', 'Consolas', monospace; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

            dist = self.nav.calculate_distance(data)
            if dist:
                self.dist_label.setText(f"CIBLE: {self.nav.target['name']}\nDIST: {dist/1000:.2f} km")
            else:
                self.dist_label.setText("PAS DE CIBLE")
        else:
            self.pos_label.setText("X: --- | Y: --- | Z: --- (Scan en cours...)")
            self.pos_label.setStyleSheet("color: #ffaa00; font-family: 'Menlo', 'Consolas', monospace; font-size: 14px; background-color: rgba(0, 0, 0, 100);")

    def init_window_properties(self):
        flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 350, 250)
        self.central_widget.setStyleSheet("background-color: transparent; border: none;")

        if self.isVisible():
            self.show()

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))

        tray_menu = QMenu()

        self.toggle_action = QAction("Masquer l'overlay", self)
        self.toggle_action.triggered.connect(self.toggle_overlay)
        tray_menu.addAction(self.toggle_action)

        options_action = QAction("Options...", self)
        options_action.triggered.connect(self.show_options_window)
        tray_menu.addAction(options_action)

        poi_action = QAction("Gestion des POI...", self)
        poi_action.triggered.connect(self.show_poi_manager_window)
        tray_menu.addAction(poi_action)

        tray_menu.addSeparator()

        poi_menu = tray_menu.addMenu("Points d'intérêt")

        save_action = QAction("Enregistrer position", self)
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
            "Overlay GPS actif !",
            QSystemTrayIcon.MessageIcon.Information,
            2000
        )

    def toggle_overlay(self):
        if self.is_visible:
            self.hide()
            self.is_visible = False
            self.toggle_action.setText("Afficher l'overlay")
        else:
            self.show()
            self.is_visible = True
            self.toggle_action.setText("Masquer l'overlay")

    def _bring_dialog_to_front(self, dialog):
        """Force le dialogue au premier plan malgré l'overlay always-on-top."""
        dialog.show()
        # Activation différée pour que Qt traite l'événement de show() avant
        QTimer.singleShot(0, dialog.raise_)
        QTimer.singleShot(0, dialog.activateWindow)

    def show_options_window(self):
        try:
            if self.options_window is None or not self.options_window.isVisible():
                self.options_window = OptionsWindow(self.config_manager, self.hotkey_listener, self)
                self.options_window.options_saved.connect(self._on_options_saved)
            self._bring_dialog_to_front(self.options_window)
        except Exception:
            logger.exception("Erreur ouverture options")
            self.tray_icon.showMessage(
                "Erreur",
                "Impossible d'ouvrir la fenêtre d'options. Voir spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def show_poi_manager_window(self):
        logger.debug("Ouverture de la fenêtre POI manager demandée")
        try:
            if self.poi_manager_window is None or not self.poi_manager_window.isVisible():
                self.poi_manager_window = POIManagerWindow(self.nav, self)
                self.poi_manager_window.destination_changed.connect(self._on_destination_changed)
                self.poi_manager_window.goto_requested.connect(self._on_goto_requested)
            self._bring_dialog_to_front(self.poi_manager_window)
        except Exception:
            logger.exception("Erreur ouverture POI manager")
            self.tray_icon.showMessage(
                "Erreur",
                "Impossible d'ouvrir la gestion des POI. Voir spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def _on_options_saved(self):
        scan_interval = self.config_manager.get_scan_interval()
        self.timer.setInterval(scan_interval)
        logger.info(f"Intervalle de scan mis à jour : {scan_interval} ms")
        # Rafraîchir les hotkeys au cas où ils ont été modifiés
        self.hotkey_listener.reload_hotkeys()

    def _on_destination_changed(self, poi):
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"])
        logger.info(f"Destination définie : {poi['name']}")

    def _on_goto_requested(self, poi):
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"])
        if not self.is_visible:
            self.toggle_overlay()

    def prompt_save_point(self):
        if self.current_data["x"] is None:
            self.tray_icon.showMessage("Erreur", "Coordonnées non détectées.", QSystemTrayIcon.MessageIcon.Warning)
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
                self.tray_icon.showMessage("Succès", "Points exportés.", QSystemTrayIcon.MessageIcon.Information)

    def import_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importer des points", "", "JSON Files (*.json)")
        if path:
            if self.nav.import_points(path):
                self.tray_icon.showMessage("Succès", "Points importés.", QSystemTrayIcon.MessageIcon.Information)

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
