"""Per-tick navigation telemetry recorder (JSONL).

Records one JSON object per OCR worker result so a full session — including
OCR dropouts (flares / scenery occlusion) and rejected misreads — can be
replayed and analysed offline. This is the enabling infrastructure for tuning
the velocity filter and the dead-reckoning coast window: without a raw trace
of ``(t_capture, position, status)`` there is no way to measure the real
dropout rate or validate guidance changes.

Format: JSON Lines (one self-describing object per line). Heterogeneous
records (a successful read vs. a no-read) coexist without a fixed schema, and
the file stays appendable/streamable.

Enabled via ``[Debug] record_telemetry`` in config.ini. Disabled by default:
when off, ``create_session_recorder`` returns ``None`` and there is zero I/O.
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class TelemetryRecorder:
    """Appends navigation records to a JSONL file, one per ``record`` call.

    Failures are swallowed (logged once) so telemetry never disrupts the
    overlay: a recorder that fails to open behaves like a disabled one.
    """

    def __init__(self, path):
        self._path = str(path)
        self._fh = None
        self._count = 0
        try:
            self._fh = open(self._path, "a", encoding="utf-8")
            logger.info("Telemetry recording to %s", self._path)
        except OSError as e:
            logger.error("Could not open telemetry file %s: %s", self._path, e)
            self._fh = None

    @property
    def enabled(self):
        """True when the file is open and records will be written."""
        return self._fh is not None

    @property
    def path(self):
        return self._path

    def record(self, **fields):
        """Write one record. ``t_wall`` (epoch seconds) is added if absent.

        Unknown / None fields are kept as-is so the offline reader can rely on
        a stable set of keys per record type.
        """
        if self._fh is None:
            return
        fields.setdefault("t_wall", time.time())
        try:
            self._fh.write(json.dumps(fields, separators=(",", ":")) + "\n")
            self._fh.flush()
            self._count += 1
        except (OSError, TypeError, ValueError) as e:
            logger.warning("Telemetry write failed: %s", e)

    def close(self):
        """Close the file. Idempotent."""
        if self._fh is not None:
            try:
                self._fh.close()
                logger.info(
                    "Telemetry closed (%d records): %s", self._count, self._path
                )
            except OSError:
                pass
            self._fh = None


def create_session_recorder(enabled, logs_dir):
    """Return a recorder writing to a session-stamped file, or ``None``.

    Args:
        enabled: when False, returns ``None`` (no file created, no I/O).
        logs_dir: directory for the JSONL file; created if missing.

    The filename embeds a launch timestamp (``telemetry-YYYYmmdd-HHMMSS.jsonl``)
    so consecutive sessions never clobber one another.
    """
    if not enabled:
        return None
    try:
        os.makedirs(str(logs_dir), exist_ok=True)
    except OSError as e:
        logger.error("Could not create telemetry dir %s: %s", logs_dir, e)
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(logs_dir) / f"telemetry-{stamp}.jsonl"
    return TelemetryRecorder(path)
