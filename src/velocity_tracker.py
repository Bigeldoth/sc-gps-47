"""Velocity estimator derived from successive OCR positions.

"Car GPS" approach: player position is sampled at regular intervals
(driven by OCR at ~5 Hz) and the velocity vector is derived by finite
differences, smoothed by EMA. The movement direction is used for guidance:
no calibration needed, not dependent on the game's frame-of-reference convention.

Limitations:
- Only works when the player is **moving** (speed above threshold).
- Heading lags 1-2 ticks behind reality; visible when the player
  changes course abruptly.
"""
import math
import logging
import time

logger = logging.getLogger(__name__)


class VelocityTracker:
    """Estimates a smoothed velocity vector (km/s) from (x, y, z, t) samples."""

    # Below this threshold the player is considered stationary (50 m/s).
    MIN_SPEED_KM_S = 0.05
    # Sample older than this: reset (teleportation, pause, etc.).
    MAX_DT_S = 3.0
    # EMA smoothing applied to velocity components.
    SMOOTHING_ALPHA = 0.5

    def __init__(self, min_speed_km_s=None, smoothing_alpha=None):
        if min_speed_km_s is not None:
            self.MIN_SPEED_KM_S = min_speed_km_s
        if smoothing_alpha is not None:
            self.SMOOTHING_ALPHA = smoothing_alpha
        self._last_pos = None  # (x, y, z)
        self._last_t = None
        self._smoothed_vel = None  # (vx, vy, vz) in km/s
        self._last_update_t = None

    def add_sample(self, x, y, z, t=None):
        """Add a sample. ``t`` is a monotonic timestamp in seconds."""
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
            # Too old: reset (quantum jump, pause, etc.)
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
        """Tuple (vx, vy, vz) in km/s, or None if not yet computed."""
        return self._smoothed_vel

    @property
    def speed_km_s(self):
        """Magnitude of the smoothed velocity, in km/s."""
        if self._smoothed_vel is None:
            return 0.0
        vx, vy, vz = self._smoothed_vel
        return math.sqrt(vx * vx + vy * vy + vz * vz)

    @property
    def is_moving(self):
        """True if velocity exceeds MIN_SPEED_KM_S."""
        return self.speed_km_s >= self.MIN_SPEED_KM_S

    @property
    def last_update_age_s(self):
        """Age in seconds of the last sample, or None."""
        if self._last_update_t is None:
            return None
        return time.monotonic() - self._last_update_t

    def reset(self):
        """Discard all state and restart estimation."""
        self._last_pos = None
        self._last_t = None
        self._smoothed_vel = None
        self._last_update_t = None
