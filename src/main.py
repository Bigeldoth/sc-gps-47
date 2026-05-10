import sys
import os
import time
import logging
import configparser
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout,
                             QWidget, QFrame, QSystemTrayIcon, QMenu, QInputDialog, QFileDialog)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QAction, QColor, QCursor
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import (
    NavigationEngine,
    format_distance,
    calculate_velocity_bearing,
    calculate_absolute_bearing,
    format_axis_delta,
    ema_angle,
    normalize_angle_signed,
)
from config_manager import ConfigManager
from hotkey_listener import HotkeyListener
from velocity_tracker import VelocityTracker
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


def _fmt_ooc(ooc):
    """Transforme 'Stanton_1_Hurston' → 'Stanton 1 Hurston' pour l'affichage."""
    if not ooc:
        return "?"
    return ooc.replace("_", " ").strip()


_ARROWS_8 = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")


def _world_arrow(abs_bearing):
    """Combine les 3 axes du bearing absolu en une flèche compacte.

    Renvoie 'arrow_xy + arrow_z' où :
      - arrow_xy : 8 directions cardinales dans le plan monde XY (Y+ = ↑, X+ = →)
      - arrow_z  : ▲ si la cible est nettement au-dessus, ▼ en-dessous, sinon vide

    Le pilote connaît le repère monde fixe du jeu (X+, Y+, Z+ visibles dans la
    HUD via les coordonnées), il peut donc s'orienter directement.
    """
    if not abs_bearing:
        return ""
    yaw_norm = (abs_bearing["yaw_deg"] + 360.0) % 360.0
    arrow_h = _ARROWS_8[int((yaw_norm + 22.5) / 45) % 8]
    pitch = abs_bearing["pitch_deg"]
    if pitch > 25.0:
        arrow_v = "▲"
    elif pitch < -25.0:
        arrow_v = "▼"
    else:
        arrow_v = ""
    return arrow_h + arrow_v


# Anchors temps→couleur pour le vieillissement des données OCR.
# Interpolation linéaire RGB entre ces points (en secondes).
_AGE_STOPS = (
    (0.0,  (0x40, 0xff, 0x40)),   # vert vif — donnée fraîche
    (3.0,  (0xff, 0xee, 0x40)),   # jaune (3s)
    (7.0,  (0xff, 0xaa, 0x00)),   # orange (7s)
    (12.0, (0xff, 0x40, 0x40)),   # rouge — donnée périmée
)

# Au-delà de cet âge, l'enregistrement rapide via hotkey est refusé.
# Cale juste avant l'entrée franche dans le rouge → on accepte vert+jaune+orange.
_QUICK_SAVE_MAX_AGE_S = 9.0


def _age_to_color(age_s):
    """Interpole linéairement une couleur HEX depuis l'âge (s) du dernier OCR valide.

    Vert (0s) → jaune (1.5s) → orange (3s) → rouge (5s+). La progression
    dépend uniquement du temps écoulé, pas du nombre de scans, pour rester
    stable même quand la fréquence d'OCR varie.
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


class GPSOverlay(QMainWindow):
    save_point_signal = pyqtSignal()
    trigger_worker = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.nav = NavigationEngine()
        self.is_visible = True
        self.current_data = {"x": None, "y": None, "z": None, "ooc": None, "location": "Unknown"}
        self._worker_busy = False
        self.options_window = None
        self.poi_manager_window = None

        # Lissage EMA + détection d'OCR périmé pour la distance vers la cible
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._last_coord_ts = None
        self._ema_alpha = 0.4
        self._stale_threshold_s = 2.0

        # Guidage directionnel basé sur la vélocité (style GPS voiture).
        # On échantillonne la position et on dérive la direction de
        # déplacement, plutôt que de lire l'orientation caméra (CamDir).
        self._velocity_tracker = VelocityTracker()
        self._last_known_ooc = None  # pour détecter un changement de repère
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None

        # Snapshot des coordonnées figé au moment de l'appui hotkey save.
        # Préserve la valeur même si l'OCR rate entre l'appui et la confirmation
        # du dialog. None = pas d'enregistrement en cours via hotkey.
        self._save_snapshot = None

        # Message temporaire affiché dans nav_label : (texte, expire_monotonic_ts).
        self._overlay_message = None

        # UI — frame MFD style Star Citizen
        self.central_widget = QWidget()
        self.central_widget.setStyleSheet("background-color: transparent;")

        outer = QVBoxLayout(self.central_widget)
        outer.setContentsMargins(0, 0, 0, 0)

        self._mfd = QFrame()
        self._mfd.setStyleSheet("""
            QFrame {
                background-color: rgba(0, 0, 0, 175);
                border: 1px solid rgba(255, 170, 0, 210);
                border-radius: 4px;
            }
        """)
        self.layout = QVBoxLayout(self._mfd)
        self.layout.setContentsMargins(10, 7, 10, 7)
        self.layout.setSpacing(4)

        _lbl_css = "border: none; font-family: 'Consolas', 'Menlo', monospace;"

        self.pos_label = QLabel("Scan en cours...")
        self.pos_label.setStyleSheet(f"color: #c8c8c8; font-size: 13px; {_lbl_css}")

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setStyleSheet("background-color: rgba(255,170,0,160); border: none; max-height: 1px;")

        self.nav_label = QLabel("PAS DE CIBLE")
        self.nav_label.setStyleSheet(f"color: #00ffff; font-size: 14px; font-weight: bold; {_lbl_css}")

        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(_sep)
        self.layout.addWidget(self.nav_label)

        outer.addWidget(self._mfd)
        self.setCentralWidget(self.central_widget)

        self.init_window_properties()
        self.setup_tray_icon()
        self._setup_worker_thread()

        scan_interval = self.config_manager.get_scan_interval()
        self.timer = QTimer()
        self.timer.timeout.connect(self._request_update)
        self.timer.start(scan_interval)

        # Timer dédié au vieillissement visuel des couleurs : indépendant du
        # rythme de l'OCR, il garantit une transition fluide (vert→orange→rouge)
        # même si le worker ralentit ou rate plusieurs scans d'affilée.
        self._color_refresh_timer = QTimer()
        self._color_refresh_timer.timeout.connect(self._tick_visual_refresh)
        self._color_refresh_timer.start(150)

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
        """Capture les coords courantes au moment exact de l'appui.

        Si la lecture OCR est trop ancienne (rouge), refuse l'enregistrement et
        affiche un message dans l'overlay. Sinon, fige un snapshot pour que la
        valeur ne change pas pendant que le dialog reste ouvert.
        """
        logger.debug("Hotkey 'save_position' détecté")
        age = self._coord_age_s()
        if self.current_data.get("x") is None or age is None:
            self._show_overlay_message("Aucune position OCR")
            return
        if age > _QUICK_SAVE_MAX_AGE_S:
            self._show_overlay_message("Enregistrement rapide impossible")
            return
        self._save_snapshot = dict(self.current_data)
        self.save_point_signal.emit()

    def _show_overlay_message(self, text, duration_s=3.0):
        """Affiche un message éphémère dans nav_label, en rouge."""
        self._overlay_message = (text, time.monotonic() + duration_s)
        self._refresh_nav_label()

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
            # On ne touche pas au texte affiché — il vieillira via la couleur.
            self._refresh_pos_color()
            self._refresh_nav_label()
            return

        # Un scan qui retourne None ne doit PAS effacer la dernière valeur connue.
        # On ne met à jour current_data + texte que si l'OCR a vraiment lu une position.
        if data["x"] is not None:
            # Rejet par vitesse impossible (Phase B) : si la nouvelle position
            # impliquerait une vitesse > 100 km/s par rapport au dernier scan,
            # c'est probablement une hallucination OCR. On garde la valeur
            # précédente. Hors quantum drive, les vaisseaux SC plafonnent
            # autour de 1-2 km/s — 100 km/s laisse une marge confortable
            # pour les sauts post-quantum.
            if self._is_velocity_implausible(data):
                self._refresh_pos_color()
                self._refresh_nav_label()
                return

            self.current_data = data
            self.pos_label.setText(
                f"X: {data['x']:>10.2f}   Y: {data['y']:>10.2f}\n"
                f"Z: {data['z']:>10.2f}"
            )
            self._last_coord_ts = time.monotonic()

            self._update_bearing_state(data)

            dist_km = self.nav.calculate_distance(data)
            if dist_km is None:
                # Distance temporairement indisponible (ex: OOC mal lu à ce
                # tick) — on garde la dernière valeur connue plutôt que
                # d'effacer l'affichage. Le coloring temporel signale déjà
                # la fraîcheur des données.
                pass
            elif (
                not self._velocity_tracker.is_moving
                or self._smoothed_distance_km is None
            ):
                self._smoothed_distance_km = dist_km
            else:
                # Snap-on-large-jump : à l'arrivée sur une cible, le tracker met
                # 1-2s à passer is_moving=False, et l'EMA traîne (ex: cible
                # affichée à 4.68km alors qu'on est à 18m). Si l'écart relatif
                # est important, on bypass l'EMA.
                prev = self._smoothed_distance_km
                relative_jump = abs(dist_km - prev) / max(prev, 0.001)
                if relative_jump > 0.3:
                    self._smoothed_distance_km = dist_km
                else:
                    a = self._ema_alpha
                    self._smoothed_distance_km = a * dist_km + (1 - a) * prev
            self._last_raw_distance_km = dist_km

        # Toujours rafraîchir la couleur (vieillissement), même sans nouvelles données.
        self._refresh_pos_color()
        self._refresh_nav_label()

    def _coord_age_s(self):
        """Âge (s) du dernier OCR valide, ou None si aucun encore."""
        if self._last_coord_ts is None:
            return None
        return time.monotonic() - self._last_coord_ts

    # Vitesse maximale plausible entre deux scans (km/s).
    # Hors quantum drive, les vaisseaux SC font ~1-2 km/s. 100 km/s laisse
    # de la marge pour la sortie de quantum, sans accepter les sauts d'OCR.
    _MAX_PLAUSIBLE_SPEED_KM_S = 100.0

    def _is_velocity_implausible(self, new_data):
        """Vrai si la nouvelle position implique un saut physiquement impossible.

        Compare la position courante (`current_data`) à la nouvelle (`new_data`)
        en utilisant le delta temps depuis `_last_coord_ts`. Rejette si la
        vitesse implicite dépasse `_MAX_PLAUSIBLE_SPEED_KM_S`.

        Tolère :
          - le premier scan (pas de référence) → False ;
          - un changement d'OOC (téléportation légitime via QT) → False ;
          - un long écart de temps (> 5 s, on a peut-être manqué le saut) → False.
        """
        if self.current_data.get("x") is None or self._last_coord_ts is None:
            return False
        # Changement d'OOC : on ne compare pas, le repère a changé.
        if new_data.get("ooc") != self.current_data.get("ooc"):
            return False
        dt = time.monotonic() - self._last_coord_ts
        if dt <= 0 or dt > 5.0:
            return False
        dx = new_data["x"] - self.current_data["x"]
        dy = new_data["y"] - self.current_data["y"]
        dz = new_data["z"] - self.current_data["z"]
        dist_km = (dx * dx + dy * dy + dz * dz) ** 0.5
        speed = dist_km / dt
        if speed > self._MAX_PLAUSIBLE_SPEED_KM_S:
            logger.warning(
                f"Rejet OCR : vitesse implausible {speed:.1f} km/s "
                f"(Δ={dist_km:.2f} km en {dt:.2f} s)"
            )
            return True
        return False

    def _tick_visual_refresh(self):
        """Recolorise pos_label et nav_label sans déclencher d'OCR.

        Découplé du worker pour que la transition vert→orange→rouge reste
        fluide indépendamment de la cadence ou des échecs OCR.
        """
        self._refresh_pos_color()
        self._refresh_nav_label()

    def _refresh_pos_color(self):
        """Met à jour uniquement la couleur du pos_label selon l'âge des coords."""
        age = self._coord_age_s()
        if age is None:
            color = "#666666"
            text = self.pos_label.text()
            if not text or text == "Scan en cours...":
                self.pos_label.setText("Scan en cours...")
        else:
            color = _age_to_color(age)
        self.pos_label.setStyleSheet(
            f"color: {color}; font-size: 13px; border: none; "
            "font-family: 'Consolas', 'Menlo', monospace;"
        )

    def _update_bearing_state(self, data):
        """Met à jour la vélocité et les offsets bearing depuis les coords OCR."""
        # Si l'OOC a changé, les coords sont dans un repère différent → reset
        # du tracker pour éviter une vélocité aberrante.
        current_ooc = data.get("ooc")
        if current_ooc is not None and current_ooc != self._last_known_ooc:
            if self._last_known_ooc is not None:
                logger.info(
                    f"Changement de zone OOC : {self._last_known_ooc} → {current_ooc}, "
                    f"reset du VelocityTracker"
                )
                self._velocity_tracker.reset()
            self._last_known_ooc = current_ooc

        if data.get("x") is not None:
            self._velocity_tracker.add_sample(
                data["x"], data["y"], data["z"], time.monotonic()
            )

        velocity = self._velocity_tracker.velocity if self._velocity_tracker.is_moving else None

        if velocity is None or self.nav.target is None:
            self._smoothed_yaw_off = None
            self._smoothed_pitch_off = None
            self._last_raw_yaw_off = None
            self._last_raw_pitch_off = None
            return

        bearing = calculate_velocity_bearing(velocity, data, self.nav.target)
        if bearing is None:
            return
        yaw_off, pitch_off = bearing

        # Skip-on-stable + EMA avec wrap-around
        if (
            self._last_raw_yaw_off == yaw_off
            and self._last_raw_pitch_off == pitch_off
        ):
            self._smoothed_yaw_off = yaw_off
            self._smoothed_pitch_off = pitch_off
        elif self._smoothed_yaw_off is None:
            self._smoothed_yaw_off = yaw_off
            self._smoothed_pitch_off = pitch_off
        else:
            a = self._ema_alpha
            self._smoothed_yaw_off = ema_angle(self._smoothed_yaw_off, yaw_off, a)
            self._smoothed_pitch_off = ema_angle(self._smoothed_pitch_off, pitch_off, a)
        self._last_raw_yaw_off = yaw_off
        self._last_raw_pitch_off = pitch_off

    def _refresh_nav_label(self):
        """Met à jour nav_label : cible, distance et cap fusionnés."""
        _css = "border: none; font-family: 'Consolas', 'Menlo', monospace;"

        # Message éphémère en priorité (ex: "Enregistrement rapide impossible").
        if self._overlay_message is not None:
            text, expire_ts = self._overlay_message
            if time.monotonic() < expire_ts:
                self.nav_label.setText(text)
                self.nav_label.setStyleSheet(
                    f"color: #ff4040; font-size: 14px; font-weight: bold; {_css}"
                )
                return
            self._overlay_message = None

        if not self.nav.target:
            self.nav_label.setText("PAS DE CIBLE")
            self.nav_label.setStyleSheet(f"color: #555555; font-size: 13px; {_css}")
            return

        target_ooc = self.nav.target.get("ooc")
        current_ooc = self.current_data.get("ooc")

        if target_ooc and current_ooc and target_ooc != current_ooc:
            self.nav_label.setText(
                f"▶ {self.nav.target['name']}\n"
                f"  zone: {_fmt_ooc(target_ooc)}\n"
                f"  actuel: {_fmt_ooc(current_ooc)}"
            )
            self.nav_label.setStyleSheet(f"color: #ffaa00; font-size: 12px; {_css}")
            return

        if target_ooc is None:
            self.nav_label.setText(f"▶ {self.nav.target['name']}\n  POI legacy — recréer")
            self.nav_label.setStyleSheet(f"color: #ffaa00; font-size: 13px; {_css}")
            return

        name = self.nav.target['name']

        if self._smoothed_distance_km is None:
            self.nav_label.setText(f"▶ {name}\n  ---")
            self.nav_label.setStyleSheet(f"color: #555555; font-size: 14px; {_css}")
            return

        # Couleur de fiabilité interpolée linéairement sur l'âge du dernier OCR.
        age_color = _age_to_color(self._coord_age_s())

        dist_str = format_distance(self._smoothed_distance_km)

        # Flèche compacte combinant les 3 axes monde (XY direction + Z élévation).
        abs_bearing = calculate_absolute_bearing(self.current_data, self.nav.target)
        arrow_str = ""
        if abs_bearing:
            arrow = _world_arrow(abs_bearing)
            if arrow:
                arrow_str = f"  {arrow}"

        # Cap yaw/pitch relatif au déplacement — uniquement disponible en mouvement.
        # La distance s'affiche TOUJOURS, indépendamment du cap.
        cap_str = ""
        if self._velocity_tracker.is_moving and self._smoothed_yaw_off is not None:
            yaw = self._smoothed_yaw_off
            pitch = self._smoothed_pitch_off
            max_off = max(abs(yaw), abs(pitch))
            if max_off < 5.0:
                cap_str = "   ✓ aligné"
            else:
                yaw_arrow = "→" if yaw > 0 else "←"
                pitch_arrow = "↑" if pitch > 0 else "↓"
                cap_str = f"   {yaw_arrow}{abs(yaw):.0f}°  {pitch_arrow}{abs(pitch):.0f}°"

        self.nav_label.setText(f"▶ {name}\n  {dist_str}{arrow_str}{cap_str}")
        self.nav_label.setStyleSheet(f"color: {age_color}; font-size: 14px; font-weight: bold; {_css}")

    def init_window_properties(self):
        flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 320, 150)
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

        reset_gps_action = QAction("Réinitialiser le GPS", self)
        reset_gps_action.triggered.connect(self.reset_velocity_tracker)
        tray_menu.addAction(reset_gps_action)

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
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"], ooc=poi.get("ooc"))
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        logger.info(f"Destination définie : {poi['name']}")

    def _on_goto_requested(self, poi):
        self.nav.set_target(poi["x"], poi["y"], poi["z"], poi["name"], ooc=poi.get("ooc"))
        self._smoothed_distance_km = None
        self._last_raw_distance_km = None
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        if not self.is_visible:
            self.toggle_overlay()

    def reset_velocity_tracker(self):
        """Oublie l'historique de positions (utile après un saut quantique)."""
        self._velocity_tracker.reset()
        self._smoothed_yaw_off = None
        self._smoothed_pitch_off = None
        self._last_raw_yaw_off = None
        self._last_raw_pitch_off = None
        self._refresh_nav_label()
        self.tray_icon.showMessage(
            "GPS réinitialisé",
            "Historique de mouvement effacé. Bouge pour recalculer la direction.",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )
        logger.info("VelocityTracker réinitialisé")

    def prompt_save_point(self):
        logger.debug("prompt_save_point déclenché")
        # Si appel via hotkey, snapshot figé au moment de l'appui.
        # Si appel via menu tray, on prend l'état courant.
        snap = self._save_snapshot if self._save_snapshot is not None else self.current_data
        self._save_snapshot = None  # consommé

        if snap.get("x") is None:
            self.tray_icon.showMessage(
                "Aucune position",
                "Les coordonnées ne sont pas encore détectées par l'OCR.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        try:
            # QInputDialog en instance pour pouvoir forcer l'always-on-top
            # (l'overlay parent a WindowTransparentForInput, le dialogue par
            # défaut peut apparaître sans focus derrière l'overlay)
            dialog = QInputDialog(self)
            dialog.setWindowTitle("Enregistrer Point")
            dialog.setLabelText("Nom du point d'intérêt :")
            dialog.setInputMode(QInputDialog.InputMode.TextInput)
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            dialog.setModal(True)
            QTimer.singleShot(0, dialog.raise_)
            QTimer.singleShot(0, dialog.activateWindow)
            if dialog.exec() != QInputDialog.DialogCode.Accepted:
                return
            name = dialog.textValue().strip()
            if not name:
                return

            ooc = snap.get("ooc")
            self.nav.add_user_point(
                name,
                snap["x"],
                snap["y"],
                snap["z"],
                snap.get("location", "Unknown"),
                ooc=ooc,
            )
            ooc_str = f" ({ooc})" if ooc else " (zone inconnue)"
            self.tray_icon.showMessage(
                "Succès",
                f"Point '{name}' enregistré{ooc_str}.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        except Exception:
            logger.exception("Erreur enregistrement position")
            self.tray_icon.showMessage(
                "Erreur",
                "Impossible d'enregistrer la position. Voir spacedrive.log.",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )

    def show_poi_selector(self):
        pois = self.nav.get_all_poi_for_location("All")
        if not pois:
            self.tray_icon.showMessage("Info", "Aucun point enregistré.", QSystemTrayIcon.MessageIcon.Information)
            return

        menu = QMenu()
        for poi in pois:
            action = QAction(f"{poi['name']} ({poi.get('location', 'Unknown')})", self)
            action.triggered.connect(lambda checked, p=poi: self._on_destination_changed(p))
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
