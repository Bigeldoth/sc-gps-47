"""PaddleOCR sidecar worker — runs inside .venv-paddle/ (Python 3.12).

The host app (Python 3.10-3.14) spawns this script via `subprocess.Popen`
and communicates over stdin/stdout using newline-delimited JSON (NDJSON):

  Request  host -> worker (one JSON object per line):
    {"op":"init","device":"cpu|gpu","model_dir":"<path>|null","lang":"en","upscale":3}
    {"op":"recognize","img":"<b64_raw_bgr>","shape":[h,w,c],"dtype":"uint8"}
    {"op":"recognize_detailed","img":"<b64_raw_bgr>","shape":[h,w,c],"dtype":"uint8"}
    {"op":"ping"}
    {"op":"shutdown"}

  Response worker -> host (one JSON object per line):
    {"ok":true,"result":<payload>}
    {"ok":false,"error":"...","traceback":"..."}

Stdout is reserved EXCLUSIVELY for JSON responses. Every `print()` and every
log line emitted by paddleocr / paddlepaddle is rerouted to stderr at
bootstrap to prevent stream corruption. The host drains stderr in a
background thread and forwards it to its own log.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import traceback
from typing import Any

# ─── Bootstrap: protect stdout from accidental writes ────────────────────
# Save the real stdout handle (binary mode for exact byte control), then
# redirect sys.stdout to stderr so any third-party print() lands harmlessly
# on stderr instead of corrupting our JSON channel.
_REAL_STDOUT = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
sys.stdout = sys.stderr

# Force UTF-8 on stderr (Windows defaults to cp1252 which mangles non-ASCII).
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# Pin OMP threads BEFORE importing paddle — see PaddleAdapter for rationale.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

# Set up logging to stderr (host will tag and forward these lines).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("paddle_worker")


def _send(payload: dict) -> None:
    """Writes one NDJSON response line to the real stdout."""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    _REAL_STDOUT.write(data)
    _REAL_STDOUT.flush()


def _err(exc: BaseException) -> dict:
    return {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
    }


class Worker:
    """Wraps a single PaddleOCR instance for the lifetime of the subprocess."""

    def __init__(self):
        self._ocr = None
        self._api: str | None = None
        self._upscale: int = 3
        self._device: str = "cpu"

    def init(
        self,
        device: str = "cpu",
        model_dir: str | None = None,
        lang: str = "en",
        upscale: int = 3,
    ) -> dict:
        """Builds the PaddleOCR instance. Idempotent: rebuilds on re-init."""
        import paddle  # type: ignore  # noqa: F401
        from paddleocr import PaddleOCR

        # Belt-and-suspenders: pin paddle's internal thread count.
        try:
            import paddle  # type: ignore
            paddle.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
        except Exception as exc:
            logger.warning("paddle.set_num_threads failed: %s", exc)

        self._device = device.lower()
        self._upscale = int(upscale)

        # PaddleOCR 3.x API kwargs. MKLDNN is force-disabled on CPU because of
        # a ConvertPirAttribute2RuntimeAttribute crash. PP-OCRv4 is pinned to
        # avoid PP-OCRv5+GPU+MKLDNN PIR crashes and to match our fine-tuned
        # en_PP-OCRv4_mobile_rec weights.
        new_api_kwargs: dict = {
            "lang": lang,
            "device": "gpu" if self._device == "gpu" else "cpu",
            "use_textline_orientation": False,
            "enable_mkldnn": False,
            "ocr_version": "PP-OCRv4",
            "text_detection_model_name": "PP-OCRv4_mobile_det",
        }
        old_api_kwargs: dict = {
            "lang": lang,
            "use_gpu": self._device == "gpu",
            "use_angle_cls": False,
            "show_log": False,
            "enable_mkldnn": False,
        }
        if model_dir and os.path.isdir(model_dir):
            new_api_kwargs["text_recognition_model_dir"] = model_dir
            old_api_kwargs["rec_model_dir"] = model_dir
            new_api_kwargs["text_recognition_model_name"] = "en_PP-OCRv4_mobile_rec"
            logger.info("PaddleOCR: using fine-tuned rec model at %s", model_dir)

        try:
            self._ocr = PaddleOCR(**new_api_kwargs)
            self._api = "3.x"
        except TypeError:
            new_api_kwargs.pop("enable_mkldnn", None)
            old_api_kwargs.pop("enable_mkldnn", None)
            try:
                self._ocr = PaddleOCR(**new_api_kwargs)
                self._api = "3.x"
            except TypeError:
                self._ocr = PaddleOCR(**old_api_kwargs)
                self._api = "2.x"
        logger.info(
            "PaddleOCR initialized (api=%s, device=%s, lang=%s, upscale=%s)",
            self._api, self._device, lang, self._upscale,
        )
        return {"api": self._api, "device": self._device, "upscale": self._upscale}

    def _decode_image(self, payload: dict):
        """Reconstructs a BGR ndarray from the IPC payload."""
        import numpy as np

        img_b64 = payload["img"]
        shape = tuple(payload["shape"])
        dtype = payload.get("dtype", "uint8")
        raw = base64.b64decode(img_b64)
        return np.frombuffer(raw, dtype=np.dtype(dtype)).reshape(shape)

    def _maybe_upscale(self, image):
        import cv2

        h, w = image.shape[:2]
        scale = 1
        if max(h, w) < 800:
            scale = self._upscale
            image = cv2.resize(
                image, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR,
            )
        return image, scale

    def recognize(self, payload: dict) -> dict:
        """Returns the joined text — mirrors PaddleAdapter.recognize()."""
        if self._ocr is None:
            raise RuntimeError("Worker not initialized — send {'op':'init'} first")
        image = self._decode_image(payload)
        if image.size == 0:
            return {"text": ""}
        image, _ = self._maybe_upscale(image)

        if self._api == "3.x":
            result = self._ocr.predict(image)
            if not result:
                return {"text": ""}
            lines: list[str] = []
            for page in result:
                texts = None
                if isinstance(page, dict):
                    texts = page.get("rec_texts")
                else:
                    texts = getattr(page, "rec_texts", None)
                if texts:
                    lines.extend(t for t in texts if t)
            return {"text": "\n".join(lines)}

        # Legacy 2.x path.
        result = self._ocr.ocr(image, cls=False)
        if not result:
            return {"text": ""}
        lines = []
        for page in result:
            if not page:
                continue
            for item in page:
                if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1]:
                    text = item[1][0]
                    if text:
                        lines.append(text)
        return {"text": "\n".join(lines)}

    def recognize_detailed(self, payload: dict) -> dict:
        """Returns the structured output — mirrors PaddleAdapter.recognize_detailed()."""
        if self._ocr is None:
            raise RuntimeError("Worker not initialized — send {'op':'init'} first")
        image = self._decode_image(payload)
        if image.size == 0:
            return {"texts": [], "scores": [], "polys": [], "upscale": 1, "shape": [0, 0, 0]}
        image, scale = self._maybe_upscale(image)

        texts: list[str] = []
        scores: list[float] = []
        polys: list[list[list[int]]] = []

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
                            polys.append([[int(x), int(y)] for x, y in poly])
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
                            polys.append([[int(x), int(y)] for x, y in box])
                        except (TypeError, ValueError):
                            polys.append([])

        return {
            "texts": texts,
            "scores": scores,
            "polys": polys,
            "upscale": scale,
            "shape": list(image.shape),
        }


def main() -> int:
    worker = Worker()
    # Read stdin as binary lines so the encoding never bites us.
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    for raw_line in stdin:
        try:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            msg = json.loads(line)
            op = msg.get("op")
            if op == "init":
                result = worker.init(
                    device=msg.get("device", "cpu"),
                    model_dir=msg.get("model_dir"),
                    lang=msg.get("lang", "en"),
                    upscale=msg.get("upscale", 3),
                )
                _send({"ok": True, "result": result})
            elif op == "recognize":
                _send({"ok": True, "result": worker.recognize(msg)})
            elif op == "recognize_detailed":
                _send({"ok": True, "result": worker.recognize_detailed(msg)})
            elif op == "ping":
                _send({"ok": True, "result": {"pong": True}})
            elif op == "shutdown":
                _send({"ok": True, "result": {"bye": True}})
                return 0
            else:
                _send({"ok": False, "error": f"unknown op: {op!r}"})
        except Exception as exc:
            try:
                _send(_err(exc))
            except Exception:
                logger.exception("paddle_worker: fatal — could not send error response")
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
