"""PaddleOCR adapter — alternative text engine to Tesseract.

Wraps `paddleocr.PaddleOCR` so the rest of the pipeline can use it through
the same `recognize(image) -> str` interface as Tesseract. The adapter is
imported lazily (inside __init__) so that environments without PaddleOCR
installed are not penalised at import time.

Device selection (cpu | gpu) is forwarded to PaddleOCR's `use_gpu` flag.
If `model_dir` points to a directory containing a fine-tuned recognition
model (produced by tools/train_paddle.py), it is loaded via `rec_model_dir`.
"""
import logging
import os

# Cap the OpenMP / oneDNN thread pools BEFORE any paddle import so CPU
# inference does not steal every core from the game and the UI. Paddle
# spawns its OMP pool the moment `paddle` is imported, so setting these
# vars after the import is too late.
#
# 2 threads is empirically the sweet spot on a quad-core: ~30% slower than
# unrestricted (still ~250-400 ms / frame for our 600×45 HUD crop) but the
# rest of the system stays responsive.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# PaddleOCR's PP-OCRv5 detection net was trained on natural scene images and
# struggles with the tiny 45×600 SC HUD strip at native resolution. Upscale
# 3× (linear interp) before recognition. Larger factors hurt latency without
# improving accuracy in practice.
_UPSCALE_FACTOR = 3


class PaddleAdapter:
    def __init__(
        self,
        device: str = "cpu",
        model_dir: str | None = None,
        lang: str = "en",
    ):
        # Snapshot the root logger level BEFORE importing paddleocr. Paddle's
        # model registry calls `logging.getLogger().setLevel(WARNING)` somewhere
        # during `PaddleOCR(...)`, which silently filters out every DEBUG/INFO
        # message we emit afterwards. We restore the original level at the
        # end of __init__ so the app log keeps recording our own diagnostics.
        _root_level_before = logging.getLogger().level

        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR is not installed. Install it from the Options "
                "dialog (Manage engines…) or run "
                "`pip install paddleocr paddlepaddle`."
            ) from exc

        # Belt-and-suspenders: pin Paddle's internal thread count even when
        # the user imported paddle indirectly (e.g. via paddleocr) before the
        # OMP env vars at module-top could take effect.
        try:
            import paddle  # type: ignore
            paddle.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
        except Exception:
            pass

        self.device = device.lower()

        # PaddleOCR 3.x changed its kwargs ("device", "use_textline_orientation"),
        # PaddleOCR 2.x used "use_gpu" / "use_angle_cls" / "show_log". We try the
        # 3.x signature first and fall back to 2.x on TypeError so the adapter
        # works across versions without pinning paddleocr to a specific release.
        #
        # MKLDNN is force-disabled on CPU because PaddlePaddle 3.3 on Windows
        # hits a `ConvertPirAttribute2RuntimeAttribute not support` runtime
        # crash inside the MKLDNN executor on the text detection model. The
        # fallback path (plain CPU executor) is ~20% slower but stable.
        new_api_kwargs: dict = {
            "lang": lang,
            "device": "gpu" if self.device == "gpu" else "cpu",
            "use_textline_orientation": False,
            "enable_mkldnn": False,
        }
        old_api_kwargs: dict = {
            "lang": lang,
            "use_gpu": self.device == "gpu",
            "use_angle_cls": False,
            "show_log": False,
            "enable_mkldnn": False,
        }
        if model_dir and os.path.isdir(model_dir):
            new_api_kwargs["text_recognition_model_dir"] = model_dir
            old_api_kwargs["rec_model_dir"] = model_dir
            logger.info("PaddleOCR: using fine-tuned rec model at %s", model_dir)

        try:
            self._ocr = PaddleOCR(**new_api_kwargs)
            self._api = "3.x"
        except TypeError:
            # Some PaddleOCR builds reject enable_mkldnn — retry without it.
            new_api_kwargs.pop("enable_mkldnn", None)
            old_api_kwargs.pop("enable_mkldnn", None)
            try:
                self._ocr = PaddleOCR(**new_api_kwargs)
                self._api = "3.x"
            except TypeError:
                self._ocr = PaddleOCR(**old_api_kwargs)
                self._api = "2.x"
        # Restore the root logger level — see the snapshot above.
        _root_level_after = logging.getLogger().level
        if _root_level_after != _root_level_before:
            logger.debug(
                "Restoring root logger level %s → %s (paddle silently raised it)",
                _root_level_after, _root_level_before,
            )
            logging.getLogger().setLevel(_root_level_before)
        logger.info(
            "PaddleOCR initialized (api=%s, device=%s, lang=%s)",
            self._api, self.device, lang,
        )

    def recognize_detailed(self, image: np.ndarray) -> dict:
        """Same inference as `recognize()` but returns the structured output.

        Returns a dict with:
          - texts:   list[str] per detected region (reading order)
          - scores:  list[float] per region
          - polys:   list[list[(int,int)]] — 4-point polygon per region in
                     the **upscaled** image coordinate space (so callers can
                     draw the boxes on top of the same scaled image).
          - upscale: int  the integer scale we applied to the input
          - shape:   (h, w, …) of the upscaled image actually fed to paddle

        Used by `tools/paddle_diagnose` to visualise detection quality.
        """
        if image is None or image.size == 0:
            return {"texts": [], "scores": [], "polys": [], "upscale": 1, "shape": (0, 0, 0)}

        h, w = image.shape[:2]
        scale = 1
        if max(h, w) < 800:
            scale = _UPSCALE_FACTOR
            image = cv2.resize(
                image, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR,
            )

        texts: list[str] = []
        scores: list[float] = []
        polys: list[list[tuple[int, int]]] = []

        if self._api == "3.x":
            result = self._ocr.predict(image)
            if result:
                for page in result:
                    if isinstance(page, dict):
                        page_texts = page.get("rec_texts") or []
                        page_scores = page.get("rec_scores") or []
                        page_polys = page.get("rec_polys") or page.get("dt_polys") or []
                    else:
                        page_texts = getattr(page, "rec_texts", []) or []
                        page_scores = getattr(page, "rec_scores", []) or []
                        page_polys = getattr(page, "rec_polys", None)
                        if page_polys is None:
                            page_polys = getattr(page, "dt_polys", []) or []
                    for i, t in enumerate(page_texts):
                        if not t:
                            continue
                        texts.append(t)
                        try:
                            scores.append(float(page_scores[i]))
                        except (IndexError, TypeError, ValueError):
                            scores.append(0.0)
                        try:
                            poly = page_polys[i]
                            polys.append([(int(x), int(y)) for x, y in poly])
                        except (IndexError, TypeError, ValueError):
                            polys.append([])
        else:
            result = self._ocr.ocr(image, cls=False)
            if result:
                for page in result:
                    if not page:
                        continue
                    for item in page:
                        if len(item) < 2:
                            continue
                        box = item[0]
                        text_conf = item[1]
                        if not isinstance(text_conf, (list, tuple)) or not text_conf:
                            continue
                        texts.append(text_conf[0])
                        scores.append(float(text_conf[1]) if len(text_conf) > 1 else 0.0)
                        try:
                            polys.append([(int(x), int(y)) for x, y in box])
                        except (TypeError, ValueError):
                            polys.append([])

        return {
            "texts": texts,
            "scores": scores,
            "polys": polys,
            "upscale": scale,
            "shape": tuple(image.shape),
        }

    def recognize(self, image: np.ndarray) -> str:
        """Runs detection + recognition on the image and returns joined text.

        The output format mirrors Tesseract's `image_to_string`: one detected
        text region per line, in reading order. The caller applies the same
        regex parsing (`_RE_POS`, `_RE_CAMDIR_TAG`, etc.) as for Tesseract.
        """
        if image is None or image.size == 0:
            return ""

        # Upscale before detection — PaddleOCR's detector misses the native
        # 45×600 HUD strip but reliably picks up text once it's ≥135×1800.
        h, w = image.shape[:2]
        if max(h, w) < 800:
            image = cv2.resize(
                image,
                (w * _UPSCALE_FACTOR, h * _UPSCALE_FACTOR),
                interpolation=cv2.INTER_LINEAR,
            )

        # 3.x uses .predict() and returns a list of dict-like results with
        # `rec_texts` (and `rec_scores`, `rec_polys`). 2.x uses .ocr(img, cls=)
        # which returns a nested list [[ [box, (text, conf)], ... ]].
        if self._api == "3.x":
            result = self._ocr.predict(image)
            if not result:
                return ""
            lines: list[str] = []
            for page in result:
                # `page` is typically a dict OR an object with attribute access.
                texts = None
                if isinstance(page, dict):
                    texts = page.get("rec_texts")
                else:
                    texts = getattr(page, "rec_texts", None)
                if texts:
                    lines.extend(t for t in texts if t)
            return "\n".join(lines)

        # Legacy 2.x path.
        result = self._ocr.ocr(image, cls=False)
        if not result:
            return ""
        lines = []
        for page in result:
            if not page:
                continue
            for item in page:
                if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1]:
                    text = item[1][0]
                    if text:
                        lines.append(text)
        return "\n".join(lines)
