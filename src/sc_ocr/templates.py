"""Chargement et gestion de la bibliothèque de templates pour NCC.

Templates stockés dans data/templates/ organisés par caractère :
  data/templates/0/, data/templates/1/, ... data/templates/9/
  data/templates/-/, data/templates/k/, data/templates/m/

Chaque template est une image PNG uint8 grayscale.

Au chargement :
  1. Tous les PNG d'un dossier sont redimensionnés à 16×24
  2. Moyennés pour produire un template synthétique stable
  3. Centrés (mean=0) et L2-normalisés pour accélérer le NCC vectorisé
"""
import os
import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

# Caractères attendus pour les coordonnées numériques
# (le '.' est optionnel : si absent, l'heuristique de classify.py le détecte)
EXPECTED_CHARS = set('0123456789.-km')

GLYPH_TARGET_WIDTH = 16
GLYPH_TARGET_HEIGHT = 24


def _is_safe_char_dir(name):
    """Vérifie qu'un nom de dossier est un caractère valide (gère Windows)."""
    return len(name) == 1 and name in EXPECTED_CHARS


class TemplateLibrary:
    def __init__(self, template_dir='data/templates'):
        self.template_dir = template_dir
        self.templates = {}       # {char: image (H, W) uint8}
        self.char_list = []       # liste ordonnée des chars
        self.centered_stack = None  # tenseur (N, H, W) float32 centré + L2-normalisé
        self._load_templates()
        self._build_stack()

    def _load_templates(self):
        if not os.path.exists(self.template_dir):
            logger.warning(f"Répertoire templates non trouvé : {self.template_dir}")
            return

        for char in sorted(EXPECTED_CHARS):
            char_dir = os.path.join(self.template_dir, char)
            if not os.path.isdir(char_dir):
                continue

            images = []
            for filename in os.listdir(char_dir):
                if not filename.lower().endswith(('.png', '.jpg', '.bmp')):
                    continue
                filepath = os.path.join(char_dir, filename)
                try:
                    img = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
                    if img is None or img.size == 0:
                        continue
                    # Redimensionner à la taille standard avant moyenne
                    resized = cv2.resize(
                        img, (GLYPH_TARGET_WIDTH, GLYPH_TARGET_HEIGHT),
                        interpolation=cv2.INTER_LINEAR,
                    )
                    images.append(resized)
                except Exception as e:
                    logger.warning(f"Erreur chargement template {filepath} : {e}")

            if images:
                stacked = np.stack(images, axis=0).astype(np.float32)
                avg = np.mean(stacked, axis=0).astype(np.uint8)
                self.templates[char] = avg
                logger.debug(f"Char '{char}': {len(images)} templates moyennés")
            else:
                logger.warning(f"Aucun template valide pour '{char}'")

        if not self.templates:
            logger.warning(
                "Aucun template chargé. Vérifie que data/templates/{0-9,-,k,m}/ "
                "contiennent des PNG."
            )

    def _build_stack(self):
        """Pré-calcule le tenseur centré+normalisé pour NCC batch."""
        if not self.templates:
            self.char_list = []
            self.centered_stack = None
            return

        self.char_list = sorted(self.templates.keys())
        n = len(self.char_list)
        stack = np.zeros((n, GLYPH_TARGET_HEIGHT, GLYPH_TARGET_WIDTH), dtype=np.float32)

        for i, char in enumerate(self.char_list):
            t = self.templates[char].astype(np.float32) / 255.0
            t_centered = t - t.mean()
            norm = np.sqrt(np.sum(t_centered * t_centered))
            if norm > 0:
                stack[i] = t_centered / norm
            else:
                stack[i] = t_centered

        self.centered_stack = stack
        logger.info(f"Stack NCC pré-calculé : {n} templates {GLYPH_TARGET_HEIGHT}x{GLYPH_TARGET_WIDTH}")

    def get_template(self, char):
        return self.templates.get(char)

    def has_templates(self):
        return len(self.templates) > 0

    def list_chars(self):
        return set(self.templates.keys())

    def stats(self):
        return {
            'total_chars': len(self.templates),
            'chars': sorted(self.templates.keys()),
        }
