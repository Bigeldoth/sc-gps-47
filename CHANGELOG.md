# Changelog — SpaceDrive GPS

Tous les changements notables de ce projet. Format inspiré de [Keep a Changelog](https://keepachangelog.com/), versionnage [SemVer](https://semver.org/).

---

## [Unreleased]

### Plan
- Phase A du plan d'optimisation OCR : pré-traitement par canal couleur (inspiré SC_OCR).
- Voir [`docs/OCR_OPTIMIZATION_PLAN.md`](docs/OCR_OPTIMIZATION_PLAN.md).

---

## [0.6.0] — 2026-05-10

### Ajouté
- **Frame MFD style Star Citizen** : bordure ambre fine + séparateur, fond noir semi-transparent.
- **Couleur progressive temporelle** sur les coordonnées : interpolation RGB linéaire vert→jaune→orange→rouge sur 12 s, indépendante du nombre de scans.
- **Timer dédié 150 ms** pour le rafraîchissement visuel — la transition reste fluide même si l'OCR ralentit ou rate des scans.
- **Snapshot hotkey** : `Shift+F3` fige les coordonnées à l'instant T pour la sauvegarde POI rapide. Refus + message overlay si données rouges (> 9 s).
- **Flèche bearing 3D** `_world_arrow` : combine yaw monde et pitch en une flèche compacte (`↑↗→↘↓↙←↖` + `▲`/`▼`).
- **4 passes seuillage** : seuil fixe + Otsu + adaptatif + masque HSV blanc — couvre les fonds variés (espace, cockpit éclairé, surfaces planétaires).

### Modifié
- **Regex Pos exige 3-4 décimales** (`\d{3,4}`) pour rejeter les lectures dégradées de Tesseract qui causaient ~17 m d'erreur sur les POI sauvegardés.
- **Snap-on-large-jump** sur la distance : si l'écart relatif > 30 %, bypass de l'EMA (évite que la distance traîne après une arrivée brutale).
- **Hauteur de capture** réduite à 150 px (3 premières lignes du HUD suffisent).
- **Format OOC humanisé** : `Stanton_1_Hurston` → `Stanton 1 Hurston` à l'affichage.
- **ID système numériques** non reconnus → `Unknown` (au lieu d'afficher `9948564368677`).

### Supprimé
- Ligne `MODE: NAVIGATION` et ligne `SYSTÈME` orange de l'overlay.
- Fallback split-on-km : produisait trop d'extractions partielles fausses (ex: `X=2` capturé depuis `L2`).

### Documentation
- Refonte complète du `README.md`.
- Nouveau `docs/OCR_OPTIMIZATION_PLAN.md` (plan en 4 phases inspiré SC_OCR).
- `requirements.txt` enrichi avec versions minimales et commentaires.
- `setup.py` corrigé (dépendances synchronisées avec requirements).

---

## [0.5.0] — 2026-04 (estimation)

### Ajouté
- Module `velocity_tracker.py` : estimateur de vélocité par différence finie sur positions OCR successives.
- Module `calibration.py` : calibration yaw caméra ↔ monde.
- Affichage `Δ X / Y / Z` par axe pour guidage à l'arrêt.
- Bascule du repère Root au repère **OOC (planet-relative)** pour les POI — invariant à l'orbite des planètes.

### Modifié
- Capture depuis y=0 pour englober la ligne CamDir du debug overlay.
- Parser CamDir tolérant aux valeurs collées (`25-5177` → `[25, -5177]`).

### Tests
- `test_bearing.py`, `test_calibration.py`, `test_navigation_format.py`, `test_ocr_camdir.py`, `test_velocity_tracker.py`.

---

## [1.4.0] — 2026-05-05

### Corrigé
- Regex Pos ultra-tolérant : underscore, `kn`, `Km`, `k` seul, pas d'espace après `Pos:`.
- Corrections OCR étendues : `Zore:` → `Zone:`, variantes `SovarSysten/SolarSysten` → `SolarSystem`.

---

## [1.3.0] — 2026-05-04

### Corrigé
- Regex Zone : capture proprement l'ID système, ne mange plus `Pos` à la fin.
- Matching intelligent des systèmes : recherche partielle dans `SYSTEM_ID_MAP`.
- Affichage permanent des coordonnées avec placeholders `---` quand non détecté.

---

## [1.2.0] — 2026-05-04

### Corrigé
- Support résolution 2560×1440 : calcul automatique de la zone de capture.
- Pré-traitement OCR simplifié : seuillage fixe à 180, retrait du filtre de netteté agressif.
- Regex `km` plus tolérant aux variations.
- Logs Tesseract via `logger` (et non `print`).

### Ajouté
- Mode debug `[Debug] save_ocr_images` dans `config.ini`.

---

## [1.1.0] — 2026-05-04

### Corrigé
- Plantage `Shift+F2` (utilisation de `QCursor.pos()` au lieu de `mapToGlobal`).
- Reconnaissance Stanton via ID numérique : ajout de `SYSTEM_ID_MAP`.

### Ajouté
- CLAHE clipLimit 3.0 → 5.0.
- Logs détaillés du processus OCR (texte brut, zones, coordonnées).

---

## [1.0.0]

Version initiale.
