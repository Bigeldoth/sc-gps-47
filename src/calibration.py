"""Calibration automatique de la convention yaw monde ↔ caméra.

Star Citizen expose un yaw caméra signé (-180..+180°) mais on ne sait pas
*a priori* :
- quel axe monde correspond à yaw=0° (+X ? +Y ? -Y ?)
- le sens de rotation (horaire vs anti-horaire vu du dessus)

Ce module observe les déplacements du joueur ET son yaw caméra simultanément,
et déduit la transformation ``cam_yaw ≈ sign * world_yaw + offset`` qui
explique le mieux les samples observés. Hypothèse implicite : pendant le
voyage, le vecteur de vélocité monde est aligné avec le cap de la caméra
(le joueur regarde où il va, pas en strafe latéral). Plus le joueur fait
varier son cap, plus la calibration est rapide et fiable.
"""
import math
import logging
from collections import deque

logger = logging.getLogger(__name__)

# Réutilise normalize_angle_signed sans import circulaire
def _norm(deg):
    return ((deg + 180.0) % 360.0) - 180.0


class YawCalibrator:
    """Collecte (Δposition, cam_yaw) et résout (sign, offset)."""

    STATE_UNCALIBRATED = "uncalibrated"
    STATE_CALIBRATING = "calibrating"
    STATE_CALIBRATED = "calibrated"

    MAX_SAMPLES = 20
    MIN_SAMPLES = 5
    MIN_SPEED_KM_S = 0.5  # km/s : seuil pour considérer le joueur "en mouvement"
    MIN_YAW_RANGE_DEG = 90.0  # variabilité minimale du cap pour calibrer
    MAX_DISPERSION_DEG = 15.0  # tolérance sur la dispersion circulaire

    def __init__(self, config_manager=None):
        self._cfg = config_manager
        self._samples = deque(maxlen=self.MAX_SAMPLES)
        self._result = None
        self._state = self.STATE_UNCALIBRATED

        if self._cfg is not None:
            persisted = self._cfg.get_yaw_calibration()
            if persisted is not None:
                self._result = persisted
                self._state = self.STATE_CALIBRATED
                logger.info(f"Calibration yaw chargée : sign={persisted[0]}, offset={persisted[1]:.2f}°")

    @property
    def state(self):
        return self._state

    @property
    def result(self):
        return self._result

    def reset(self):
        """Force une nouvelle calibration."""
        self._samples.clear()
        self._result = None
        self._state = self.STATE_UNCALIBRATED
        if self._cfg is not None:
            self._cfg.clear_yaw_calibration()
        logger.info("Calibration yaw réinitialisée")

    def add_sample(self, prev_pos, curr_pos, dt, cam_yaw):
        """Ajoute un sample si conditions remplies (mouvement, dt valide)."""
        if self._state == self.STATE_CALIBRATED:
            return
        if dt is None or dt <= 0:
            return
        if prev_pos is None or curr_pos is None:
            return
        if cam_yaw is None:
            return
        try:
            dx = curr_pos["x"] - prev_pos["x"]
            dy = curr_pos["y"] - prev_pos["y"]
        except (KeyError, TypeError):
            return

        # km/s — coords sont en km
        speed = math.hypot(dx, dy) / dt
        if speed < self.MIN_SPEED_KM_S:
            return

        world_yaw = math.degrees(math.atan2(dx, dy))
        self._samples.append((world_yaw, cam_yaw))
        if self._state == self.STATE_UNCALIBRATED:
            self._state = self.STATE_CALIBRATING

    def try_solve(self):
        """Tente de calculer (sign, offset). Retourne True si calibré."""
        if self._state == self.STATE_CALIBRATED:
            return True
        if len(self._samples) < self.MIN_SAMPLES:
            return False

        cam_yaws = [s[1] for s in self._samples]
        if (max(cam_yaws) - min(cam_yaws)) < self.MIN_YAW_RANGE_DEG:
            # Pas assez de variation de cap — le joueur va trop droit
            return False

        best = None  # (dispersion, sign, offset)
        for sign in (1, -1):
            sin_sum = 0.0
            cos_sum = 0.0
            n = 0
            for world_yaw, cam_yaw in self._samples:
                offset_i = _norm(cam_yaw - sign * world_yaw)
                sin_sum += math.sin(math.radians(offset_i))
                cos_sum += math.cos(math.radians(offset_i))
                n += 1
            mean_offset = math.degrees(math.atan2(sin_sum / n, cos_sum / n))
            # Dispersion circulaire : 1 - R où R est la longueur du vecteur moyen
            r = math.hypot(sin_sum, cos_sum) / n
            # Convertit en "écart-type circulaire" en degrés (approx)
            dispersion_deg = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(r)))) if r > 0 else 180.0

            if best is None or dispersion_deg < best[0]:
                best = (dispersion_deg, sign, mean_offset)

        dispersion, sign, offset = best
        if dispersion > self.MAX_DISPERSION_DEG:
            logger.debug(f"Calibration en cours : dispersion {dispersion:.1f}° trop large")
            return False

        self._result = (sign, offset)
        self._state = self.STATE_CALIBRATED
        if self._cfg is not None:
            self._cfg.set_yaw_calibration(sign, offset)
        logger.info(
            f"Calibration yaw réussie : sign={sign}, offset={offset:.2f}°, "
            f"dispersion={dispersion:.2f}°, n={len(self._samples)}"
        )
        return True
