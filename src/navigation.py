import math
import json
import os
import re
import sys
import time
import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# Durée pendant laquelle on tolère que l'OCR ne retrouve pas le nom de zone
# avant de considérer que le joueur a quitté la zone de la cible.
_ZONE_GRACE_PERIOD_S = 180.0  # 3 minutes

# Seuil de similarité pour considérer deux noms de zone comme identiques
# (tolérance aux variations OCR : OOC_Stanton1 vs 0OC_Stanton1 vs OOC Stanton1).
_ZONE_MATCH_THRESHOLD = 0.85


def _normalize_zone(name):
    """Normalise un nom de zone pour comparaison robuste aux variations OCR.

    Lowercase + retire tout caractère non-alphanumérique.
    Ex: ``OOC_Stanton1_Hurston`` et ``OOC Stanton1-Hurston`` → ``oocstanton1hurston``.
    """
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]+', '', name.lower())


def _zones_match(a, b, threshold=_ZONE_MATCH_THRESHOLD):
    """True si deux noms de zone sont considérés comme la même zone.

    Tolérance aux confusions OCR (O/0, I/1, espaces/underscores) via
    SequenceMatcher de difflib.
    """
    if not a or not b:
        return False
    a_norm = _normalize_zone(a)
    b_norm = _normalize_zone(b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


def format_distance(distance_km):
    """Formate une distance (en km) pour l'affichage.

    Retourne une chaîne en mètres sous 1 km (sans décimale), en kilomètres
    au-delà (deux décimales). ``None`` devient ``"---"``.
    """
    if distance_km is None:
        return "---"
    if distance_km < 1.0:
        return f"{distance_km * 1000:.0f} m"
    return f"{distance_km:.2f} km"


def normalize_angle_signed(deg):
    """Ramène un angle (degrés) dans l'intervalle ]-180, +180]."""
    if deg is None:
        return None
    return ((deg + 180.0) % 360.0) - 180.0


def ema_angle(prev, new, alpha):
    """EMA sur des angles avec gestion correcte du wrap-around ±180°."""
    if prev is None:
        return new
    diff = normalize_angle_signed(new - prev)
    return normalize_angle_signed(prev + alpha * diff)


def calculate_absolute_bearing(current_pos, target):
    """Cap absolu (repère monde) entre la position courante et la cible.

    À utiliser quand le joueur est stationnaire : on ne connaît pas son
    orientation mais on peut indiquer le vecteur monde à atteindre.

    Args:
        current_pos: dict avec ``x/y/z`` (km).
        target: dict avec ``x/y/z`` (km).

    Returns:
        Dict ``{dx, dy, dz, yaw_deg, pitch_deg, distance_km}`` en repère
        monde, ou ``None`` si impossible.
    """
    if not target or current_pos is None:
        return None
    if current_pos.get("x") is None:
        return None

    dx = target["x"] - current_pos["x"]
    dy = target["y"] - current_pos["y"]
    dz = target["z"] - current_pos["z"]
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)

    horiz = math.hypot(dx, dy)
    if dx == 0 and dy == 0:
        yaw = 0.0
    else:
        yaw = math.degrees(math.atan2(dx, dy))
    if horiz == 0:
        pitch = 90.0 if dz > 0 else (-90.0 if dz < 0 else 0.0)
    else:
        pitch = math.degrees(math.atan2(dz, horiz))

    return {
        "dx": dx, "dy": dy, "dz": dz,
        "yaw_deg": yaw, "pitch_deg": pitch,
        "distance_km": distance,
    }


def format_axis_delta(km):
    """Formate un delta d'axe pour affichage compact.

    < 10 km : 1 décimale ; < 1000 km : entier ; >= 1000 km : 'k' notation.
    """
    if km is None:
        return "?"
    if abs(km) < 10:
        return f"{km:+.1f}"
    if abs(km) < 1000:
        return f"{km:+.0f}"
    return f"{km / 1000:+.1f}k"


def calculate_velocity_bearing(velocity, current_pos, target):
    """Offsets (yaw, pitch) en degrés signés entre la direction de
    déplacement et la cible.

    Args:
        velocity: tuple ``(vx, vy, vz)`` en km/s, ou ``None`` (stationnaire).
        current_pos: dict avec ``x/y/z`` (km) — position du joueur.
        target: dict avec ``x/y/z`` (km) — destination.

    Returns:
        ``(yaw_off, pitch_off)`` dans ]-180, +180], ou ``None`` si la
        vélocité, la position ou la cible ne sont pas définies.

        - ``yaw_off`` positif : la cible est à droite du déplacement.
        - ``pitch_off`` positif : la cible est au-dessus du déplacement.
    """
    if velocity is None or target is None or current_pos is None:
        return None
    if current_pos.get("x") is None:
        return None

    vx, vy, vz = velocity
    if vx == 0 and vy == 0 and vz == 0:
        return None

    dx = target["x"] - current_pos["x"]
    dy = target["y"] - current_pos["y"]
    dz = target["z"] - current_pos["z"]
    if dx == 0 and dy == 0 and dz == 0:
        return 0.0, 0.0

    # Cap horizontal de la vélocité et de la cible
    vel_yaw = math.degrees(math.atan2(vx, vy)) if (vx or vy) else 0.0
    tgt_yaw = math.degrees(math.atan2(dx, dy)) if (dx or dy) else 0.0
    yaw_off = normalize_angle_signed(tgt_yaw - vel_yaw)

    # Pitch (composante verticale)
    vel_horiz = math.hypot(vx, vy)
    tgt_horiz = math.hypot(dx, dy)
    vel_pitch = math.degrees(math.atan2(vz, vel_horiz)) if vel_horiz > 0 else 0.0
    tgt_pitch = math.degrees(math.atan2(dz, tgt_horiz)) if tgt_horiz > 0 else 0.0
    pitch_off = normalize_angle_signed(tgt_pitch - vel_pitch)

    return yaw_off, pitch_off


def calculate_relative_bearing(current_pos, cam_dir, target, yaw_calib):
    """Offsets (yaw, pitch) en degrés signés vers la cible, dans le repère caméra.

    Args:
        current_pos: dict avec 'x', 'y', 'z' en km (sortie OCR).
        cam_dir: dict avec 'pitch', 'yaw' en degrés (sortie OCR CamDir).
        target: dict avec 'x', 'y', 'z' en km.
        yaw_calib: tuple ``(sign, offset)`` issu de la calibration. Convertit
            le yaw monde en yaw caméra : ``cam_yaw = sign * world_yaw + offset``.

    Returns:
        ``(yaw_off, pitch_off)`` dans ]-180, +180], ou ``None`` si une donnée
        manque. ``0,0`` signifie « cible droit devant ». Le yaw_off positif =
        cible à droite du cap, négatif = cible à gauche.
    """
    if not target or not cam_dir or not yaw_calib:
        return None
    if current_pos is None or current_pos.get("x") is None:
        return None
    if cam_dir.get("yaw") is None or cam_dir.get("pitch") is None:
        return None

    dx = target["x"] - current_pos["x"]
    dy = target["y"] - current_pos["y"]
    dz = target["z"] - current_pos["z"]

    horiz = math.hypot(dx, dy)
    if horiz == 0 and dz == 0:
        return 0.0, 0.0

    target_world_yaw = math.degrees(math.atan2(dx, dy)) if horiz > 0 else 0.0
    target_world_pitch = math.degrees(math.atan2(dz, horiz))

    sign, offset = yaw_calib
    target_cam_yaw = sign * target_world_yaw + offset

    yaw_off = normalize_angle_signed(target_cam_yaw - cam_dir["yaw"])
    pitch_off = normalize_angle_signed(target_world_pitch - cam_dir["pitch"])
    return yaw_off, pitch_off


class NavigationEngine:
    def __init__(self, poi_file=None):
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            
        if poi_file is None:
            poi_file = os.path.join(self.base_dir, "data", "poi.json")
            
        self.poi_file = poi_file
        self.user_poi_file = os.path.join(self.base_dir, "data", "user_poi.json")
        
        # Créer le dossier data s'il n'existe pas
        os.makedirs(os.path.dirname(self.user_poi_file), exist_ok=True)
        
        self.poi_data = self.load_poi(self.poi_file)
        self.user_poi = self.load_user_poi()
        self.target = None

        # Zone tracking (cf. set_target/update_zone_tracking/is_target_in_same_ooc).
        # On assume au lancement de la navigation que le joueur est dans la zone
        # de la cible. Si l'OCR confirme au moins une fois la zone, on lock.
        # Sinon, après _ZONE_GRACE_PERIOD_S sans match, on considère hors zone.
        self._zone_match_locked = False
        self._zone_last_match_ts = None

    def load_user_poi(self):
        """Charge les points enregistrés par l'utilisateur"""
        if os.path.exists(self.user_poi_file):
            try:
                with open(self.user_poi_file, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []

    def save_user_poi(self):
        """Sauvegarde les points utilisateur"""
        try:
            with open(self.user_poi_file, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def add_user_point(self, name, x, y, z, location="Unknown", ooc=None):
        """Ajoute un nouveau point personnalisé.

        ``ooc`` (ObjectContainer name, ex: ``Stanton_1_Hurston``) identifie
        le repère planet-relative. Indispensable pour la navigation : la
        distance et le bearing ne sont calculables qu'au sein du même OOC.
        """
        point = {
            "name": name,
            "x": x,
            "y": y,
            "z": z,
            "location": location,
            "ooc": ooc,
        }
        self.user_poi.append(point)
        self.save_user_poi()
        return point

    def export_points(self, export_path):
        """Exporte les points utilisateur vers un fichier JSON"""
        try:
            with open(export_path, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def import_points(self, import_path):
        """Importe des points depuis un fichier JSON"""
        try:
            with open(import_path, 'r') as f:
                new_points = json.load(f)
                if isinstance(new_points, list):
                    self.user_poi.extend(new_points)
                    self.save_user_poi()
                    return True
            return False
        except:
            return False

    def load_poi(self, file_path):
        try:
            if not os.path.exists(file_path):
                print(f"Fichier POI non trouvé: {file_path}")
                return []
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Erreur chargement POI : {e}")
            return []

    def set_target(self, x, y, z, name="Destination", ooc=None):
        """Définit la destination et démarre le tracking de zone.

        ``ooc`` est le nom de zone attendu. On part du principe que l'utilisateur
        est déjà dans cette zone — la navigation démarre immédiatement.

        Le tracking de zone (``update_zone_tracking`` + ``is_target_in_same_ooc``)
        confirme cette hypothèse au premier match OCR. Si aucun match dans les
        ``_ZONE_GRACE_PERIOD_S`` secondes, on considère que le joueur n'est plus
        dans la zone.
        """
        self.target = {"x": x, "y": y, "z": z, "name": name, "ooc": ooc}
        self._zone_match_locked = False
        self._zone_last_match_ts = time.monotonic()
        if ooc:
            logger.info(f"Navigation vers '{name}' (zone attendue: {ooc!r}) — assume in-zone")

    def clear_target(self):
        """Annule la navigation et reset le zone tracking."""
        self.target = None
        self._zone_match_locked = False
        self._zone_last_match_ts = None

    def update_zone_tracking(self, current_pos):
        """Met à jour le tracking de zone selon le scan OCR courant.

        À appeler à chaque résultat OCR. Si le nom de zone courant correspond
        (au sens de ``_zones_match``) à celui de la cible, on lock définitif.
        Sinon, on laisse courir le grace period.
        """
        if not self.target or current_pos is None:
            return
        if self._zone_match_locked:
            return  # déjà verrouillé, plus besoin de comparer
        cur_ooc = current_pos.get("ooc")
        tgt_ooc = self.target.get("ooc")
        if not cur_ooc or not tgt_ooc:
            return
        if _zones_match(cur_ooc, tgt_ooc):
            self._zone_match_locked = True
            self._zone_last_match_ts = time.monotonic()
            logger.info(
                f"Zone confirmée par OCR : '{cur_ooc}' ≈ '{tgt_ooc}' — lock définitif"
            )

    def is_target_in_same_ooc(self, current_pos):
        """True si on considère que le joueur est dans la zone de la cible.

        Logique :
          1. Lock définitif après le 1er match OCR confirmé via ``update_zone_tracking``.
          2. Sinon : tolérance pendant ``_ZONE_GRACE_PERIOD_S`` (3 min) à partir
             de ``set_target`` — on assume in-zone.
          3. Au-delà sans aucun match : on considère hors zone.
        """
        if not self.target:
            return False
        if self._zone_match_locked:
            return True
        if self._zone_last_match_ts is None:
            return False
        return (time.monotonic() - self._zone_last_match_ts) <= _ZONE_GRACE_PERIOD_S

    def time_until_zone_expired(self):
        """Secondes restantes avant que la zone expire.

        Retourne ``None`` si lock définitif ou si pas de cible.
        """
        if self._zone_match_locked or self.target is None or self._zone_last_match_ts is None:
            return None
        elapsed = time.monotonic() - self._zone_last_match_ts
        return max(0.0, _ZONE_GRACE_PERIOD_S - elapsed)

    def calculate_distance(self, current_pos):
        """Distance euclidienne entre la position courante et la cible.

        Coordonnées en km dans le repère **planet-relative** (OOC). Renvoie
        ``None`` si la cible n'est pas définie, si la position courante est
        manquante, OU si le joueur et la cible sont dans des OOC différents
        (la distance n'a alors pas de sens sans transformation cross-zone).
        """
        if not self.target or current_pos.get("x") is None:
            return None
        if not self.is_target_in_same_ooc(current_pos):
            return None

        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = self.target["z"] - current_pos["z"]

        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def calculate_bearing(self, current_pos):
        """Calcul simple du vecteur de direction (Pitch/Yaw)"""
        if not self.target or current_pos["x"] is None:
            return None
            
        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = self.target["z"] - current_pos["z"]
        
        # En navigation spatiale SC, on utilise souvent l'alignement visuel
        # Ce moteur retournera les deltas pour aider l'overlay à placer un curseur
        return {"dx": dx, "dy": dy, "dz": dz}

    def get_all_poi_for_location(self, location_name):
        """Retourne la liste des POI (système + utilisateur) pour une planète donnée"""
        pois = []
        # Points du système
        for system in self.poi_data:
            for body in system["bodies"]:
                if body["name"].lower() in location_name.lower():
                    pois.extend(body["poi"])
        
        # Points utilisateur
        for upoi in self.user_poi:
            if upoi.get("location", "").lower() in location_name.lower() or location_name == "All":
                pois.append(upoi)
                
        return pois
