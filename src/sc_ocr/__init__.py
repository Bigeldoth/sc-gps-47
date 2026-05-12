"""sc_ocr — NCC / ONNX glyph pipeline for Star Citizen HUD OCR.

Phase D: replaces Tesseract for numeric coordinates with a custom
glyph classifier (NCC pure NumPy or ONNX CNN), ~10 ms/frame vs ~100 ms.

Architecture:
  - preprocess : channel isolation, Otsu thresholding, conditional denoising (~0.3 ms)
  - segment    : horizontal projection → glyph bounding boxes (~0.5 ms)
  - classify   : shift-invariant NCC ±2×±1 px on 16×24 templates (~1 ms / 12 glyphs)
                 OR ONNX CNN inference (TinyGlyphCNN, ~25k params)
  - templates  : template library load + cache
"""
from .segment import find_glyph_regions
from .classify import classify_batch
from .templates import TemplateLibrary

__all__ = ["find_glyph_regions", "classify_batch", "TemplateLibrary"]
