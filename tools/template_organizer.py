"""Organisation automatique des glyphes segmentés en templates par caractère.

Après une session de collecte (30 min en jeu avec save_glyph_crops=True),
ce script :
  1. Scanne data/glyphs/{TIMESTAMP}/
  2. Utilise Tesseract haute confiance pour labelliser automatiquement
  3. Organise dans data/templates/{char}/ (0-9, ., -, k, m, etc.)
  4. Génère un rapport

Structure finale :
  data/templates/
    ├── 0/ (chiffre)
    │   ├── template_001.png
    │   ├── template_002.png
    │   ...
    ├── 1/
    ├── ...
    ├── ./
    ├── -/
    ├── k/
    ├── m/
    └── _/ (espace)
"""
import os
import cv2
import sys
import logging
import pytesseract
from pathlib import Path
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Caractères attendus pour les coordonnées numériques
EXPECTED_CHARS = set('0123456789.-km ')

# Config Tesseract pour haute confiance sur caractères
_TESSERACT_CONFIG = (
    r'--oem 3 --psm 10 '
    r'-c classify_bln_numeric_mode=1 '
    r'-c tessedit_char_whitelist=0123456789.-km '
)


def init_tesseract(tesseract_path=None):
    """Initialise pytesseract avec le chemin correct."""
    if not tesseract_path:
        possible_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                logger.info(f"Tesseract trouvé : {path}")
                return True
        logger.error("Tesseract-OCR non trouvé !")
        return False
    else:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        return True


def find_latest_glyph_dir(glyphs_base_dir='data/glyphs'):
    """Trouve le répertoire de glyphes le plus récent.

    Returns:
        chemin du dossier TIMESTAMP ou None
    """
    if not os.path.isdir(glyphs_base_dir):
        logger.error(f"Répertoire {glyphs_base_dir} non trouvé")
        return None

    subdirs = [
        d for d in os.listdir(glyphs_base_dir)
        if os.path.isdir(os.path.join(glyphs_base_dir, d))
    ]

    if not subdirs:
        logger.error(f"Aucun dossier de glyphes dans {glyphs_base_dir}")
        return None

    # Le plus récent (tri lexicographique si noms YYYYMMDD_HHMMSS_mmm)
    latest = sorted(subdirs)[-1]
    path = os.path.join(glyphs_base_dir, latest)
    logger.info(f"Dossier glyphes détecté : {path}")
    return path


def classify_glyph(image_path, config=_TESSERACT_CONFIG):
    """Utilise Tesseract pour labelliser un glyphe.

    Args:
        image_path : chemin vers le PNG
        config : config Tesseract

    Returns:
        (char_recognized, confidence_score) ou (None, 0.0)
    """
    try:
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return None, 0.0

        # Essayer petit upscale pour PSM 10 (single character)
        h, w = img.shape
        if w < 8 or h < 8:
            img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LINEAR)

        # Tesseract data
        text = pytesseract.image_to_string(img, config=config)
        text = text.strip()

        if not text:
            return None, 0.0

        # Prendre le premier caractère
        char = text[0]

        # Confidence très élevée (Tesseract retourne 0-100, normalisé ici)
        # On accepte seulement si confiance > 70
        confidence = 0.85  # Approximation, Tesseract n'expose pas la confiance facilement en PSM 10

        return char, confidence

    except Exception as e:
        logger.warning(f"Erreur classification {image_path} : {e}")
        return None, 0.0


def organize_glyphs(glyph_dir, output_base='data/templates'):
    """Organise les glyphes en dossiers par caractère.

    Args:
        glyph_dir : répertoire source des glyphes (TIMESTAMP)
        output_base : répertoire de sortie (data/templates/)

    Returns:
        dict avec stats
    """
    os.makedirs(output_base, exist_ok=True)

    # Créer les dossiers de sortie pour chaque caractère attendu
    char_dirs = {}
    for char in EXPECTED_CHARS:
        char_dir = os.path.join(output_base, char)
        os.makedirs(char_dir, exist_ok=True)
        char_dirs[char] = char_dir

    stats = defaultdict(int)
    failed = []
    total = 0

    # Scanner tous les PNG
    for filename in sorted(os.listdir(glyph_dir)):
        if not filename.lower().endswith('.png'):
            continue

        filepath = os.path.join(glyph_dir, filename)
        total += 1

        # Classifier
        char, confidence = classify_glyph(filepath)

        if char is None:
            failed.append(filename)
            stats['unclassified'] += 1
            logger.warning(f"[{total}] {filename} : non classifiable")
            continue

        # Vérifier que c'est un caractère attendu
        if char not in EXPECTED_CHARS:
            logger.debug(f"[{total}] {filename} : '{char}' (hors liste, skip)")
            stats['unexpected_char'] += 1
            failed.append(filename)
            continue

        # Copier dans le dossier approprié
        dest_dir = char_dirs[char]
        dest_path = os.path.join(dest_dir, filename)

        try:
            img = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
            cv2.imwrite(dest_path, img)
            stats[char] += 1
            logger.info(f"[{total}] {filename} → '{char}' ({confidence:.2f})")
        except Exception as e:
            logger.error(f"[{total}] Erreur copie {filename} : {e}")
            stats['copy_error'] += 1
            failed.append(filename)

    return dict(stats), failed, total


def print_report(stats, failed, total, output_base='data/templates'):
    """Affiche un rapport des résultats.

    Args:
        stats : dict de stats par caractère
        failed : list de fichiers échoués
        total : nombre total de fichiers
        output_base : répertoire output
    """
    print("\n" + "=" * 60)
    print("RAPPORT ORGANISATION TEMPLATES")
    print("=" * 60)
    print(f"Total glyphes traités : {total}")
    print(f"Organisés avec succès : {sum(v for k, v in stats.items() if k not in ['unclassified', 'unexpected_char', 'copy_error'])}")
    print()

    print("Répartition par caractère :")
    for char in sorted(EXPECTED_CHARS):
        count = stats.get(char, 0)
        if count > 0:
            print(f"  '{char}' : {count:3d} templates")

    unclassified = stats.get('unclassified', 0)
    unexpected = stats.get('unexpected_char', 0)
    errors = stats.get('copy_error', 0)

    if unclassified + unexpected + errors > 0:
        print()
        print("Problèmes rencontrés :")
        if unclassified > 0:
            print(f"  Non classifiable : {unclassified}")
        if unexpected > 0:
            print(f"  Caractère inattendu : {unexpected}")
        if errors > 0:
            print(f"  Erreurs copy : {errors}")

    if failed:
        print()
        print(f"Premiers fichiers échoués ({len(failed)}) :")
        for f in failed[:10]:
            print(f"  - {f}")

    print()
    print(f"Templates organisés dans : {output_base}/")
    print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Organise les glyphes collectés en templates par caractère"
    )
    parser.add_argument(
        "--glyph-dir",
        type=str,
        default=None,
        help="Chemin du dossier source (data/glyphs/{TIMESTAMP}). Auto-détecte si absent."
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/templates",
        help="Répertoire de sortie (défaut: data/templates)"
    )
    parser.add_argument(
        "--tesseract-path",
        type=str,
        default=None,
        help="Chemin vers tesseract.exe si non standard"
    )

    args = parser.parse_args()

    # Init Tesseract
    if not init_tesseract(args.tesseract_path):
        sys.exit(1)

    # Trouver le dossier de glyphes
    glyph_dir = args.glyph_dir or find_latest_glyph_dir()
    if not glyph_dir:
        sys.exit(1)

    logger.info(f"Organisation des glyphes depuis : {glyph_dir}")
    logger.info(f"Destination : {args.output}")

    # Organiser
    stats, failed, total = organize_glyphs(glyph_dir, args.output)

    # Rapport
    print_report(stats, failed, total, args.output)

    # Retour
    success_count = sum(v for k, v in stats.items() if k not in ['unclassified', 'unexpected_char', 'copy_error'])
    if success_count == 0:
        logger.error("Aucun glyph organisé correctement !")
        sys.exit(1)

    logger.info(f"✅ {success_count}/{total} glyphes organisés")


if __name__ == "__main__":
    main()
