"""PaddleOCR adapter — thin wrapper over the sidecar service.

PaddleOCR ships wheels only for CPython 3.8-3.12 on Windows, which would
otherwise pin the entire app to Python 3.12. To free the host process to
run on 3.13/3.14 (and still benefit from PaddleOCR), we run PaddleOCR
inside a dedicated `.venv-paddle/` (Python 3.12) and talk to it via a
JSON IPC sidecar — see `src/paddle_service.py` and
`scripts/paddle_worker.py`.

This module preserves the historical `PaddleAdapter` API
(`recognize(image) -> str`, `recognize_detailed(image) -> dict`) so the rest
of the pipeline (`ocr.py`, `paddle_diagnose.py`, `prepare_paddle_dataset.py`)
keeps working without changes.
"""
from __future__ import annotations

import logging

import numpy as np

from capture import UPSCALE_FACTOR
from paddle_service import PaddleServiceError, get_service

logger = logging.getLogger(__name__)


class PaddleAdapter:
    """Drop-in adapter that delegates inference to the sidecar process.

    `device`, `model_dir`, `lang` are forwarded to the worker on init. The
    shared `PaddleService` singleton (see `paddle_service.get_service`) is
    rebuilt if any of those parameters change.
    """

    def __init__(
        self,
        device: str = "cpu",
        model_dir: str | None = None,
        lang: str = "en",
    ):
        self.device = device.lower()
        self.model_dir = model_dir
        self.lang = lang
        try:
            self._service = get_service(
                device=self.device,
                model_dir=self.model_dir,
                lang=self.lang,
                upscale=UPSCALE_FACTOR,
            )
            self._service.start(blocking=True, timeout=60.0)
        except PaddleServiceError as exc:
            raise RuntimeError(str(exc)) from exc
        logger.info(
            "PaddleAdapter ready via sidecar (device=%s, lang=%s, model_dir=%s)",
            self.device, lang, model_dir,
        )

    def recognize(self, image: np.ndarray) -> str:
        """Runs detection + recognition and returns the joined text."""
        try:
            return self._service.recognize(image)
        except PaddleServiceError as exc:
            logger.error("paddle: recognize failed: %s", exc)
            return ""

    def recognize_detailed(self, image: np.ndarray) -> dict:
        """Returns structured output: texts, scores, polys, upscale, shape."""
        try:
            return self._service.recognize_detailed(image)
        except PaddleServiceError as exc:
            logger.error("paddle: recognize_detailed failed: %s", exc)
            return {"texts": [], "scores": [], "polys": [], "upscale": 1, "shape": (0, 0, 0)}
