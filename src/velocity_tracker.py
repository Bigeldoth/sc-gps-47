"""Position/velocity estimator: per-axis constant-velocity Kalman filter.

Why a Kalman filter (A2): the previous finite-difference + EMA estimator
amplified measurement noise and lagged 1-2 ticks on turns. A constant-velocity
(CV) Kalman gives a smoother, lower-lag velocity AND three properties the live
telemetry showed we need:

  * **Innovation gating** — a measurement whose normalised innovation squared
    (NIS) exceeds a threshold is rejected as an OCR misread. The gross misreads
    seen in practice (multi-km digit flips) score NIS ~1e5 and are trivially
    rejected, while clean reads score ~0.
  * **Adaptive gate after a gap** — during a dropout the predict step grows the
    state covariance, so the acceptance window widens automatically. The first
    valid read after a long occlusion is therefore accepted instead of being
    rejected as an "implausible jump" (the cascade the hard velocity gate caused).
  * **Dead-reckoning / coasting** — when no measurement arrives (flare / scenery
    occlusion), ``predict_only`` advances the state on the last known velocity.
    SC flight has constant velocity between thrust inputs, so coasting is nearly
    exact over the ~1-3 s a dropout typically lasts.

Integrity is annunciated as FRESH / COASTING / LOST from the time since the last
accepted measurement (see ``integrity``).

The public surface (``add_sample`` / ``velocity`` / ``speed_km_s`` /
``is_moving`` / ``reset`` / ``last_update_age_s``) is preserved for callers; new
accessors ``position`` / ``integrity`` / ``coast_age_s`` / ``predict_only``
support the dead-reckoning UI.
"""
import math
import logging
import time

logger = logging.getLogger(__name__)


class _KalmanCV1D:
    """1-D constant-velocity Kalman filter. State = [position, velocity]."""

    def __init__(self, sigma_a, sigma_z, v_var0=1e6):
        self._q = sigma_a * sigma_a   # acceleration (process) noise PSD
        self._r = sigma_z * sigma_z   # measurement noise variance
        self._v_var0 = v_var0         # initial velocity variance (bounded)
        self.p = 0.0
        self.v = 0.0
        # 2x2 covariance, row-major [[P00, P01], [P10, P11]].
        self.P = [[1e6, 0.0], [0.0, v_var0]]

    def initialize(self, z):
        """Seed position from the first measurement. Initial velocity variance
        is bounded (``v_var0``) so an early noisy finite difference cannot latch
        the filter onto an impossible speed."""
        self.p = z
        self.v = 0.0
        self.P = [[self._r, 0.0], [0.0, self._v_var0]]

    def predict(self, dt):
        """Advance state + covariance by ``dt`` seconds (no measurement)."""
        self.p += self.v * dt
        P = self.P
        # P' = F P F^T  with F = [[1, dt], [0, 1]]
        p00 = P[0][0] + dt * (P[1][0] + P[0][1]) + dt * dt * P[1][1]
        p01 = P[0][1] + dt * P[1][1]
        p10 = P[1][0] + dt * P[1][1]
        p11 = P[1][1]
        # + Q  (continuous white-noise acceleration model)
        q = self._q
        dt2 = dt * dt
        dt3 = dt2 * dt
        self.P = [
            [p00 + q * dt3 / 3.0, p01 + q * dt2 / 2.0],
            [p10 + q * dt2 / 2.0, p11 + q * dt],
        ]

    def innovation(self, z):
        """Return (y, S): innovation and its variance for measurement ``z``."""
        y = z - self.p
        S = self.P[0][0] + self._r
        return y, S

    def update(self, z):
        """Correct state with measurement ``z`` (call after ``predict``)."""
        y, S = self.innovation(z)
        P = self.P
        k0 = P[0][0] / S
        k1 = P[1][0] / S
        self.p += k0 * y
        self.v += k1 * y
        self.P = [
            [(1.0 - k0) * P[0][0], (1.0 - k0) * P[0][1]],
            [P[1][0] - k1 * P[0][0], P[1][1] - k1 * P[0][1]],
        ]


class VelocityTracker:
    """Tracks position + velocity (km, km/s) from (x, y, z, t) OCR samples."""

    # Below this speed the player is considered stationary (50 m/s).
    MIN_SPEED_KM_S = 0.05
    # Hysteresis: once moving, only drop back to stationary below this (avoids
    # mode flicker around the threshold).
    EXIT_SPEED_KM_S = 0.03
    # Gap beyond which we reset rather than coast (quantum jump / long pause).
    MAX_DT_S = 3.0
    # Kalman tuning, seeded from live telemetry (clean reads ~0.05 m at ~2.5 Hz;
    # SC accelerates briskly). SIGMA_A governs how fast the filter follows a
    # turn; SIGMA_Z is the per-axis measurement noise; GATE_NIS is the 3-DOF
    # normalised-innovation gate (gross km-scale misreads score >1e4).
    SIGMA_A_KM_S2 = 1.0
    SIGMA_Z_KM = 0.02
    GATE_NIS = 30.0
    # Max dead-reckoning window before integrity degrades to LOST.
    COAST_S = 2.0
    # Physical speed ceiling (SC tops out ~1.4 km/s/axis; 2.5 km/s 3D leaves
    # margin). The velocity estimate is clamped to this and the initial velocity
    # covariance is bounded by it, so the filter can never latch onto an
    # impossible speed (e.g. from an early misread or a diverged coast).
    MAX_SPEED_KM_S = 2.5

    def __init__(self, min_speed_km_s=None, smoothing_alpha=None,
                 sigma_a=None, sigma_z=None, gate_nis=None,
                 coast_s=None, max_speed_km_s=None):
        """Construct a velocity tracker.

        The Kalman/OCR tuning knobs (``sigma_a``, ``sigma_z``, ``gate_nis``,
        ``coast_s``, ``max_speed_km_s``) default to the class constants, which
        were seeded from live telemetry. Callers (and the Options dialog via
        config.ini) may override them to tune filter behaviour without editing
        source. Out-of-range values are clamped to sane bounds.
        """
        if min_speed_km_s is not None:
            self.MIN_SPEED_KM_S = min_speed_km_s
            self.EXIT_SPEED_KM_S = 0.6 * min_speed_km_s
        # smoothing_alpha is accepted for backward compatibility (the EMA it
        # tuned is gone); it no longer has any effect.
        sa = sigma_a if sigma_a is not None else self.SIGMA_A_KM_S2
        sz = sigma_z if sigma_z is not None else self.SIGMA_Z_KM
        # Clamp to strictly-positive, finite noise std (a zero/negative std
        # would make the filter ill-conditioned). Bound the other knobs too.
        sa = max(1e-6, float(sa))
        sz = max(1e-6, float(sz))
        if gate_nis is not None:
            self.GATE_NIS = max(1e-3, float(gate_nis))
        if coast_s is not None:
            self.COAST_S = max(0.0, float(coast_s))
        if max_speed_km_s is not None:
            self.MAX_SPEED_KM_S = max(1e-3, float(max_speed_km_s))
        # Remember the noise std so reset() can rebuild identical filters.
        self._sigma_a = sa
        self._sigma_z = sz
        v_var0 = self.MAX_SPEED_KM_S ** 2
        self._fx = _KalmanCV1D(sa, sz, v_var0)
        self._fy = _KalmanCV1D(sa, sz, v_var0)
        self._fz = _KalmanCV1D(sa, sz, v_var0)
        self._init = False          # filter seeded with a first sample
        self._has_velocity = False  # at least one predict+update done
        self._moving = False
        self._last_t = None         # latest time the filter was advanced to
        self._last_accept_t = None  # time of the last accepted measurement

    # ── sample ingestion ────────────────────────────────────────────────

    def add_sample(self, x, y, z, t=None):
        """Ingest a position measurement. Returns ``True`` if it was accepted,
        ``False`` if ignored (missing coords / non-positive dt) or rejected by
        the innovation gate (treated as a misread → the filter coasts)."""
        if x is None or y is None or z is None:
            return False
        if t is None:
            t = time.monotonic()

        if not self._init:
            self._seed(x, y, z, t)
            return True

        dt = t - self._last_t
        if dt <= 0:
            return False
        if dt > self.MAX_DT_S:
            # Too old to coast (quantum jump / pause): restart from this sample.
            logger.debug("VelocityTracker reset (dt=%.1fs > %.1fs)", dt, self.MAX_DT_S)
            self._seed(x, y, z, t)
            return True
        if self._last_accept_t is not None and (t - self._last_accept_t) > self.COAST_S:
            # Coasted past the integrity window (LOST): the velocity estimate is
            # stale/diverged. Re-seed from this fix (v=0) rather than correcting
            # a bad filter — prevents the velocity lock-up seen in the field.
            logger.debug("VelocityTracker re-seed on LOST recovery")
            self._seed(x, y, z, t)
            return True

        self._fx.predict(dt)
        self._fy.predict(dt)
        self._fz.predict(dt)
        self._last_t = t

        # 3-DOF normalised innovation squared. Gross OCR misreads dwarf the gate.
        yx, sx = self._fx.innovation(x)
        yy, sy = self._fy.innovation(y)
        yz, sz = self._fz.innovation(z)
        nis = yx * yx / sx + yy * yy / sy + yz * yz / sz
        if nis > self.GATE_NIS:
            logger.debug("Kalman gate rejected sample (NIS=%.1f)", nis)
            self._update_moving()
            return False

        self._fx.update(x)
        self._fy.update(y)
        self._fz.update(z)
        self._clamp_speed()
        self._has_velocity = True
        self._last_accept_t = t
        self._update_moving()
        return True

    def predict_only(self, t):
        """Advance the estimate to time ``t`` with no measurement (dead-reckon
        through an OCR dropout). No-op before the filter is seeded."""
        if not self._init or self._last_t is None:
            return
        dt = t - self._last_t
        if dt <= 0:
            return
        if dt > self.MAX_DT_S:
            self.reset()
            return
        self._fx.predict(dt)
        self._fy.predict(dt)
        self._fz.predict(dt)
        self._last_t = t
        self._update_moving()

    def _seed(self, x, y, z, t):
        self._fx.initialize(x)
        self._fy.initialize(y)
        self._fz.initialize(z)
        self._init = True
        self._has_velocity = False
        self._moving = False
        self._last_t = t
        self._last_accept_t = t

    def _clamp_speed(self):
        """Bound the 3D velocity magnitude to MAX_SPEED_KM_S (physical ceiling).

        Hard guarantee against impossible speeds regardless of filter dynamics —
        the last line of defence behind the bounded init covariance and the
        LOST-recovery re-seed.
        """
        spd = math.sqrt(self._fx.v ** 2 + self._fy.v ** 2 + self._fz.v ** 2)
        if spd > self.MAX_SPEED_KM_S:
            s = self.MAX_SPEED_KM_S / spd
            self._fx.v *= s
            self._fy.v *= s
            self._fz.v *= s

    def _update_moving(self):
        spd = self.speed_km_s
        if self._moving:
            self._moving = spd >= self.EXIT_SPEED_KM_S
        else:
            self._moving = spd >= self.MIN_SPEED_KM_S

    # ── accessors ───────────────────────────────────────────────────────

    @property
    def velocity(self):
        """(vx, vy, vz) in km/s, or None until a second sample is processed."""
        if not self._has_velocity:
            return None
        return (self._fx.v, self._fy.v, self._fz.v)

    @property
    def position(self):
        """Estimated (x, y, z) in km (includes dead-reckoning), or None."""
        if not self._init:
            return None
        return (self._fx.p, self._fy.p, self._fz.p)

    @property
    def speed_km_s(self):
        """Magnitude of the estimated velocity, in km/s."""
        if not self._has_velocity:
            return 0.0
        return math.sqrt(self._fx.v ** 2 + self._fy.v ** 2 + self._fz.v ** 2)

    @property
    def is_moving(self):
        """True if moving (hysteretic: enters at MIN_SPEED, exits at EXIT_SPEED)."""
        return self._moving

    @property
    def coast_age_s(self):
        """Seconds since the last accepted measurement (capture-time), or None.

        Grows while dead-reckoning through rejected/absent reads; 0 right after
        an accepted sample."""
        if self._last_accept_t is None or self._last_t is None:
            return None
        return max(0.0, self._last_t - self._last_accept_t)

    @property
    def integrity(self):
        """FRESH (just measured) / COASTING (dead-reckoning) / LOST (stale)."""
        age = self.coast_age_s
        if age is None:
            return "LOST"
        if age <= 1e-9:
            return "FRESH"
        if age <= self.COAST_S:
            return "COASTING"
        return "LOST"

    @property
    def last_update_age_s(self):
        """Age in seconds of the last accepted sample (wall/monotonic), or None."""
        if self._last_accept_t is None:
            return None
        return time.monotonic() - self._last_accept_t

    def reset(self):
        """Discard all state and restart estimation."""
        sa = self._sigma_a
        sz = self._sigma_z
        v_var0 = self.MAX_SPEED_KM_S ** 2
        self._fx = _KalmanCV1D(sa, sz, v_var0)
        self._fy = _KalmanCV1D(sa, sz, v_var0)
        self._fz = _KalmanCV1D(sa, sz, v_var0)
        self._init = False
        self._has_velocity = False
        self._moving = False
        self._last_t = None
        self._last_accept_t = None
