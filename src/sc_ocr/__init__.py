"""Module sc_ocr — NCC template matching pour OCR HUD Star Citizen.

Phase D : remplacer Tesseract pour les coordonnées numériques par un classifieur
NCC pur NumPy, latence ~10 ms/frame vs ~100 ms Tesseract.

Architecture :
  - preprocess : canal isolé, seuillage Otsu, débruitage conditionnel (~0.3 ms)
  - segment : projection horizontale → glyphes, composantes connexes (~0.5 ms)
  - classify : NCC shift-invariant ±2×±1 px sur templates (~1 ms / 12 glyphes)
  - templates : chargement/cache de la bibliothèque de templates
"""
from .preprocess import preprocess_image
from .segment import find_glyph_regions
from .classify import classify_batch
from .templates import TemplateLibrary

__all__ = ["preprocess_image", "find_glyph_regions", "classify_batch", "TemplateLibrary"]
