"""Glyph classification via an ONNX-exported CNN model.

Drop-in replacement for `sc_ocr.classify.classify_batch`: identical signature, same
return format, enabling clean toggling via config.

Pipeline:
  1. Normalizes each glyph to 16×24 uint8 via `normalize_glyph()` (reused).
  2. Stacks into batch (N, 1, 24, 16) float32 normalized /255.
  3. `onnxruntime.InferenceSession.run()` → logits → softmax.
  4. If confidence < threshold → `char = None` (allows pipeline to fall back to
     Tesseract fallbacks in `OCRProcessor`).
  5. Decimal point heuristic (reused from `classify._is_likely_dot`) applied
     to unclassified glyphs if no equivalent '.' template exists in the
     model's class list.

Inference is 100% CPU (`CPUExecutionProvider`).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from sc_ocr.classify import _is_likely_dot, normalize_glyph

logger = logging.getLogger(__name__)


class ONNXGlyphClassifier:
    """ONNX classifier with the same interface as `classify.classify_batch`."""

    def __init__(
        self,
        model_path: str | Path,
        classes_path: str | Path,
        confidence_threshold: float = 0.85,
        providers: Optional[list[str]] = None,
    ) -> None:
        import onnxruntime as ort  # lazy import — onnxruntime is optional

        self.model_path = Path(model_path)
        self.classes_path = Path(classes_path)
        self.confidence_threshold = float(confidence_threshold)

        if not self.model_path.is_file():
            raise FileNotFoundError(f"ONNX model not found: {self.model_path}")
        if not self.classes_path.is_file():
            raise FileNotFoundError(f"Class mapping not found: {self.classes_path}")

        with self.classes_path.open(encoding="utf-8") as f:
            payload = json.load(f)
        classes = payload.get("classes")
        if not isinstance(classes, list) or not classes:
            raise ValueError(f"Invalid class mapping in {self.classes_path}")
        self.classes: list[str] = [str(c) for c in classes]

        self._providers = providers or ["CPUExecutionProvider"]
        self._session = ort.InferenceSession(str(self.model_path), providers=self._providers)
        self._input_name = self._session.get_inputs()[0].name
        self._has_dot_class = "." in self.classes

        logger.info(
            "ONNXGlyphClassifier ready: %s  (%d classes, threshold %.2f, provider %s)",
            self.model_path.name, len(self.classes),
            self.confidence_threshold, self._providers[0],
        )

    @staticmethod
    def _softmax(logits: np.ndarray) -> np.ndarray:
        shifted = logits - logits.max(axis=1, keepdims=True)
        exp = np.exp(shifted)
        return exp / exp.sum(axis=1, keepdims=True)

    def _infer(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Returns (argmax indices, confidences) for a batch (N,1,24,16)."""
        logits = self._session.run(None, {self._input_name: batch})[0]
        probs = self._softmax(logits)
        idx = np.argmax(probs, axis=1)
        conf = probs[np.arange(len(idx)), idx]
        return idx, conf

    def classify_batch(
        self,
        glyphs_images: Iterable[tuple[int, np.ndarray]],
        template_library=None,  # noqa: ARG002 — kept for signature compatibility with NCC
        glyphs_meta: Optional[list[dict]] = None,
    ) -> list[dict]:
        """Classifies a batch of glyphs. Same contract as `classify.classify_batch`.

        Args:
            glyphs_images : iterable of (glyph_id, image uint8 H×W).
            template_library : ignored (present for drop-in NCC).
            glyphs_meta : optional, enables decimal point heuristic on rejections.

        Returns:
            list of dicts {'glyph_id', 'char', 'score', 'raw_image'}.
        """
        items = list(glyphs_images)
        if not items:
            return []

        meta_by_id: dict[int, dict] = {}
        if glyphs_meta is not None:
            meta_by_id = {g["id"]: g for g in glyphs_meta}

        normalized_imgs = [normalize_glyph(img) for _, img in items]
        batch = np.stack(normalized_imgs, axis=0).astype(np.float32) / 255.0
        batch = batch[:, np.newaxis, :, :]  # (N, 1, H, W)

        idx, conf = self._infer(batch)

        results: list[dict] = []
        for (glyph_id, raw_img), class_idx, score in zip(items, idx, conf):
            score_f = float(score)
            char: Optional[str]
            if score_f >= self.confidence_threshold:
                char = self.classes[int(class_idx)]
            else:
                char = None

            if char is None and not self._has_dot_class:
                meta = meta_by_id.get(glyph_id)
                if _is_likely_dot(meta):
                    char = "."
                    score_f = 0.99

            results.append({
                "glyph_id": glyph_id,
                "char": char,
                "score": score_f,
                "raw_image": raw_img,
            })

        return results
