"""Capture d'écran + pré-traitement OCR.

Pipeline (inspiré de SC-Toolbox-Beta-V2/sc_ocr/preprocess.py) :

  1. Capture mss BGR
  2. isolate_channel(auto) — choisit le canal qui maximise la séparation
     texte/fond selon les statistiques R/G/B :
       - lum > 140         → invert grayscale (pièce éclairée)
       - R - G > 15        → canal R (texte rouge/orange)
       - G - R > 15        → canal G (texte vert/cyan)
       - sinon             → max(R, G, B) (texte blanc — défaut HUD SC)
  3. Upscale ×2
  4. CLAHE clipLimit=3.0
  5. GaussianBlur conditionnel : seulement si std(canal) > 45 (fast-path
     en conditions normales — économie CPU + meilleure netteté)
  6. 3 passes seuillage :
       - pass_otsu         : Otsu sur canal isolé (texte = blanc, défaut)
       - pass_otsu_inv     : Otsu inversé (cas dark-on-bright résiduel)
       - pass_adaptive     : seuillage adaptatif local (backup fond uniforme)

Par rapport à l'ancienne version (4 passes), suppression de la passe HSV
qui produisait du bruit dans ~50 % des cas — l'isolation par canal couleur
auto la rend redondante.
"""
import mss
import numpy as np
import cv2
import configparser
import logging

logger = logging.getLogger(__name__)

CAPTURE_WIDTH = 600
CAPTURE_HEIGHT = 150

# Seuil au-delà duquel on applique un GaussianBlur (sinon fast-path).
# Mesure : écart-type du canal isolé. Au-dessus de ~45, le fond est
# vraiment bruité (texture asteroid, particules), un blur léger aide.
_NOISE_STD_THRESHOLD = 45.0


def _isolate_channel_auto(bgr):
    """Réduit une image BGR (H, W, 3) à un canal uint8 où le texte est clair.

    Choix automatique du canal selon les statistiques :
      - Luminance moyenne > 140 → fond clair (pièce éclairée). On inverse
        la grayscale pour que le texte sombre devienne clair.
      - R - G > 15 → texte rouge/orange dominant : canal R.
      - G - R > 15 → texte vert/cyan dominant : canal G.
      - Sinon → max(R, G, B), idéal pour texte blanc sur fond varié.

    OpenCV utilise BGR (b=0, g=1, r=2). Le HUD SC est blanc → le défaut
    est ``max(R, G, B)`` qui maximise le contraste pour le texte clair.
    """
    if bgr.ndim == 2:
        return bgr  # déjà single channel
    b = bgr[..., 0]
    g = bgr[..., 1]
    r = bgr[..., 2]
    lum = bgr.mean()
    r_mean = r.mean()
    g_mean = g.mean()

    if lum > 140:
        # Fond globalement clair → texte sombre relatif. Invert grayscale.
        gray = bgr.mean(axis=2)
        return (255 - gray).astype(np.uint8)
    if r_mean - g_mean > 15:
        return r.astype(np.uint8)
    if g_mean - r_mean > 15:
        return g.astype(np.uint8)
    # Texte blanc/mixte sur fond sombre — cas par défaut Star Citizen.
    return bgr.max(axis=2).astype(np.uint8)


class ScreenCapture:
    def __init__(self):
        self._sct = mss.mss()
        self.monitor_index = 1

        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        config = configparser.ConfigParser()
        config.read('config.ini')
        self._save_debug = config.getboolean('Debug', 'save_ocr_images', fallback=False)
        test_path = config.get('Debug', 'test_screenshot', fallback='').strip()
        self._test_screenshot = test_path if test_path else None

        self._region = None
        if self._test_screenshot:
            logger.info(f"Mode test : lecture depuis {self._test_screenshot}")
            self._test_img = cv2.imread(self._test_screenshot)
            if self._test_img is None:
                raise FileNotFoundError(f"Screenshot introuvable : {self._test_screenshot}")
        else:
            monitor = self._sct.monitors[self.monitor_index]
            # Le debug overlay SC (r_DisplayInfo 3) commence au pixel 0 ;
            # on capture depuis le tout haut sinon la ligne CamDir est coupée.
            self._region = {
                "top": monitor["top"],
                "left": monitor["left"] + monitor["width"] - CAPTURE_WIDTH,
                "width": CAPTURE_WIDTH,
                "height": CAPTURE_HEIGHT,
            }
            logger.info(f"Capture {CAPTURE_WIDTH}x{CAPTURE_HEIGHT} top-right, region={self._region}")

    def capture(self):
        if self._test_screenshot:
            img = self._test_img
        else:
            raw = self._sct.grab(self._region)
            img = np.asarray(raw, dtype=np.uint8)[:, :, :3]

        # Phase A : isolation par canal couleur intelligent.
        channel = _isolate_channel_auto(img)
        channel = cv2.resize(channel, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)

        # GaussianBlur conditionnel : seulement si fond bruité.
        # Préserve la netteté du texte fin en conditions normales.
        if float(channel.std()) > _NOISE_STD_THRESHOLD:
            channel = cv2.GaussianBlur(channel, (3, 3), 0)

        enhanced = self._clahe.apply(channel)

        images = {}

        # Pass1 : Otsu sur canal isolé. Cas par défaut HUD blanc.
        _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        images['otsu'] = otsu

        # Pass2 : Otsu inversé pour les cas résiduels où le canal isolé
        # ne suffit pas (ex : transition de scène, mid-luminance).
        _, otsu_inv = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        images['otsu_inv'] = otsu_inv

        # Pass3 : adaptatif local, garde-fou pour fond uniformément clair.
        adaptive = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, -8
        )
        images['adaptive'] = adaptive

        if self._save_debug:
            cv2.imwrite("debug_capture_original.png", img)
            cv2.imwrite("debug_capture_channel.png", channel)
            cv2.imwrite("debug_capture_otsu.png", otsu)
            cv2.imwrite("debug_capture_adaptive.png", adaptive)

        return images

    def stop(self):
        pass


if __name__ == "__main__":
    cap = ScreenCapture()
    images = cap.capture()
    if images:
        cv2.imwrite("test_capture.png", images.get('otsu'))
        print("Capture de test effectuée : test_capture.png")
    else:
        print("Pas de frame capturée.")
