"""Estimateur de vélocité à partir de positions OCR successives.

Approche « GPS de voiture » : on échantillonne la position du joueur à
intervalle régulier (cadencé par l'OCR ~5 Hz) et on dérive le vecteur
vélocité par différence finie, lissé par EMA. La direction de
déplacement remplace l'angle de caméra (CamDir) pour le guidage : pas
besoin de calibration, pas dépendant de la convention de repère du jeu.

Limitations :
- Ne fonctionne que quand le joueur **bouge** (vitesse > seuil).
- L'orientation a 1-2 ticks de retard sur la réalité ; visible quand le
  joueur change de cap brutalement.
"""
import math
import logging
import time

logger = logging.getLogger(__name__)


class VelocityTracker:
    """Estime un vecteur vélocité (km/s) lissé depuis des samples (x, y, z, t)."""

    # Sous ce seuil on considère le joueur stationnaire (50 m/s).
    MIN_SPEED_KM_S = 0.05
    # Sample plus vieux que ça : on reset (téléportation, pause, etc.).
    MAX_DT_S = 3.0
    # Lissage EMA sur les composantes de vélocité.
    SMOOTHING_ALPHA = 0.5

    def __init__(self, min_speed_km_s=None, smoothing_alpha=None):
        if min_speed_km_s is not None:
            self.MIN_SPEED_KM_S = min_speed_km_s
        if smoothing_alpha is not None:
            self.SMOOTHING_ALPHA = smoothing_alpha
        self._last_pos = None  # (x, y, z)
        self._last_t = None
        self._smoothed_vel = None  # (vx, vy, vz) en km/s
        self._last_update_t = None

    def add_sample(self, x, y, z, t=None):
        """Ajoute un sample. ``t`` est un timestamp monotonic en secondes."""
        if x is None or y is None or z is None:
            return
        if t is None:
            t = time.monotonic()

        if self._last_pos is None or self._last_t is None:
            self._last_pos = (x, y, z)
            self._last_t = t
            self._last_update_t = t
            return

        dt = t - self._last_t
        if dt <= 0:
            return
        if dt > self.MAX_DT_S:
            # Trop vieux : reset (saut quantique, pause, etc.)
            logger.debug(f"VelocityTracker reset (dt={dt:.1f}s > {self.MAX_DT_S}s)")
            self._last_pos = (x, y, z)
            self._last_t = t
            self._smoothed_vel = None
            self._last_update_t = t
            return

        vx = (x - self._last_pos[0]) / dt
        vy = (y - self._last_pos[1]) / dt
        vz = (z - self._last_pos[2]) / dt

        if self._smoothed_vel is None:
            self._smoothed_vel = (vx, vy, vz)
        else:
            a = self.SMOOTHING_ALPHA
            sx, sy, sz = self._smoothed_vel
            self._smoothed_vel = (
                a * vx + (1 - a) * sx,
                a * vy + (1 - a) * sy,
                a * vz + (1 - a) * sz,
            )
        self._last_pos = (x, y, z)
        self._last_t = t
        self._last_update_t = t

    @property
    def velocity(self):
        """Tuple (vx, vy, vz) en km/s, ou None si pas encore calculé."""
        return self._smoothed_vel

    @property
    def speed_km_s(self):
        """Magnitude de la vélocité lissée, en km/s."""
        if self._smoothed_vel is None:
            return 0.0
        vx, vy, vz = self._smoothed_vel
        return math.sqrt(vx * vx + vy * vy + vz * vz)

    @property
    def is_moving(self):
        """True si la vélocité dépasse MIN_SPEED_KM_S."""
        return self.speed_km_s >= self.MIN_SPEED_KM_S

    @property
    def last_update_age_s(self):
        """Âge en secondes du dernier sample, ou None."""
        if self._last_update_t is None:
            return None
        return time.monotonic() - self._last_update_t

    def reset(self):
        """Oublie tout et redémarre l'estimation."""
        self._last_pos = None
        self._last_t = None
        self._smoothed_vel = None
        self._last_update_t = None
