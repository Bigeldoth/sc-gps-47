"""HTTP client for the PaddleOCR-VL sidecar.

Implements the same `recognize(image) -> str` interface as `PaddleAdapter`
so `OCRProcessor._extract_full_text_paddle` can drive both transparently.
Communicates with the sidecar started by `paddle_vl_service.PaddleVLService`
via the OpenAI-compatible Chat Completions endpoint that the `paddleocr
genai_server` exposes (model='PaddleOCR-VL-1.5-0.9B', backend='transformers').

The image is uploaded as a base64-encoded data URL embedded in a multi-modal
message — the same format used by the OpenAI vision API.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import urllib.error
import urllib.request
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_PROMPT = (
    "Transcribe every line of text in this image, in reading order. "
    "Output one line per detected text region. No commentary."
)


class PaddleVLAdapter:
    """OpenAI-style client for `paddleocr genai_server`.

    The constructor takes the endpoint URL and the model name. It does NOT
    start the sidecar — `PaddleVLService` is responsible for the lifecycle.
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8118",
        model: str = "PaddleOCR-VL-1.5-0.9B",
        timeout: float = _DEFAULT_TIMEOUT,
        prompt: str = _DEFAULT_PROMPT,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.prompt = prompt

    def recognize(self, image: np.ndarray) -> str:
        """Sends `image` to the sidecar and returns the transcribed text.

        Image is encoded as PNG (lossless, small for a 600×45 HUD strip),
        base64-wrapped, and submitted via the OpenAI chat completions schema.
        Failures are logged and an empty string is returned so the caller can
        fall back gracefully without a try/except.
        """
        if image is None or image.size == 0:
            return ""

        # Same upscale trick as PaddleAdapter — VL models are also trained
        # at natural document resolutions, not 45-px tall HUD strips.
        h, w = image.shape[:2]
        if max(h, w) < 800:
            image = cv2.resize(
                image, (w * 3, h * 3), interpolation=cv2.INTER_LINEAR,
            )

        ok, buf = cv2.imencode(".png", image)
        if not ok:
            logger.error("paddle-vl: failed to PNG-encode the input image")
            return ""
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            # Keep tokens tight — HUD strip never produces long output.
            "max_tokens": 256,
            "temperature": 0.0,
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.endpoint}/v1/chat/completions"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            logger.warning("paddle-vl: request failed (%s) — sidecar down?", exc)
            return ""
        except Exception as exc:
            logger.error("paddle-vl: unexpected error: %s", exc)
            return ""

        try:
            data: dict[str, Any] = json.loads(response_text)
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                # Some servers return content as a list of parts.
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict)
                )
            return str(content).strip()
        except (KeyError, IndexError, ValueError) as exc:
            logger.error(
                "paddle-vl: could not parse response (%s): %s",
                exc, response_text[:200],
            )
            return ""
