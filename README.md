# SpaceDrive GPS

> Overlay de navigation GPS pour Star Citizen, basé sur l'OCR du HUD debug `r_DisplayInfo 3`.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![Anti-cheat](https://img.shields.io/badge/EAC-safe-green.svg)](#sécurité-anti-cheat)

SpaceDrive lit en continu les coordonnées affichées par le HUD debug du jeu (`Zone:OOC_X Pos: X.XXXX km Y.XXXX km Z.XXXX km`) et fournit un overlay always-on-top avec distance, cap et indicateur de fraîcheur des données. Aucune lecture mémoire — 100 % capture d'écran.

---

## Aperçu

```
┌─────────────────────────────────┐  ← bordure ambre MFD style
│ X:   4133.56   Y:  -1964.13    │  ← couleur évolue selon la fraîcheur
│ Z:   -529.89                   │     (vert → jaune → orange → rouge)
├─────────────────────────────────┤
│ ▶ asop hurL2                   │  ← cible courante
│   17 m   ↗▲   →42° ↑12°        │  ← distance + flèche 3D + cap relatif
└─────────────────────────────────┘
```

**Légende des indicateurs :**
- **Couleur des coordonnées** : interpolation temporelle continue. Vert = données fraîches (<3 s), jaune (3-7 s), orange (7-12 s), rouge = à rafraîchir.
- **Flèche 3D `↗▲`** : direction monde combinée. 8 directions cardinales (`↑↗→↘↓↙←↖`) en X/Y, plus `▲`/`▼` si élévation Z significative.
- **Cap relatif `→42° ↑12°`** : offset yaw/pitch par rapport à la direction de déplacement (uniquement quand tu bouges).

---

## Fonctionnalités

### Lecture OCR robuste
- **Tesseract OEM3** + 4 passes de seuillage (fixe / Otsu / adaptatif / masque HSV blanc) pour gérer les fonds variés (espace, cockpit éclairé, surfaces planétaires).
- **Regex stricte 3-4 décimales** : rejette les lectures dégradées de Tesseract qui causaient des erreurs de ~17 m sur les POI sauvegardés.
- **Normalisation post-OCR** : corrige les artefacts courants (`Pos:_`, variantes `lkm/Km/kn`, underscores parasites).
- **Région de capture** : 600×150 px en haut à droite (les 3 premières lignes du HUD suffisent).

### Navigation
- **POI système** chargés depuis `data/poi.json` + **POI utilisateur** dans `data/user_poi.json`.
- **Distance euclidienne 3D** dans le repère **planet-relative (OOC)** — invariant à l'orbite des planètes, contrairement au repère Root/SolarSystem.
- **Refus du calcul cross-OOC** : si la cible et le joueur ne sont pas dans le même ObjectContainer, l'overlay l'indique au lieu d'afficher une distance fausse.
- **Snap-on-large-jump** sur la distance : à l'arrivée brutale, bypass de l'EMA pour éviter qu'elle traîne.

### Snapshot hotkey rapide
- `Shift+F3` fige les coordonnées **à l'instant exact de l'appui** dans un snapshot — la valeur ne dérive pas pendant que le dialog reste ouvert.
- Refus avec message overlay si les données sont périmées (rouge, > 9 s).

### Overlay MFD style Star Citizen
- Frame avec bordure ambre fine, fond noir semi-transparent.
- `WindowTransparentForInput` → ne capture jamais le clic souris du jeu.
- `WindowStaysOnTopHint` → reste visible par-dessus Star Citizen.
- Couleur des coordonnées progresse linéairement avec le temps écoulé depuis le dernier OCR valide (timer dédié 150 ms, indépendant du cycle de capture).

---

## Installation

### Prérequis

- **Python 3.10+** ([python.org](https://www.python.org/downloads/) — cocher "Add to PATH" à l'installation)
- **Tesseract OCR** ([UB-Mannheim build pour Windows](https://github.com/UB-Mannheim/tesseract/wiki))
  - Installation par défaut dans `C:\Program Files\Tesseract-OCR\` (auto-détecté).
- **Windows 10/11** (chemins Tesseract Windows ; Linux/macOS non testé).

### Procédure

```powershell
# 1. Cloner le repo
git clone https://github.com/Bigeldoth/sc-gps-47.git
cd sc-gps-47

# 2. Installer les dépendances Python
python -m pip install -r requirements.txt

# 3. Lancer
python src/main.py
```

### Build d'un exécutable standalone (PyInstaller)

```powershell
python -m PyInstaller --clean spaceDrive.spec
# → dist/spaceDrive.exe
```

Voir [`docs/BUILD.md`](docs/BUILD.md) pour les détails du build.

---

## Configuration en jeu

L'overlay nécessite l'affichage du **debug HUD** de Star Citizen :

1. Ouvrir la console du jeu : touche **`** (à gauche du `1` clavier US, sous `Échap` clavier FR).
2. Taper `r_DisplayInfo 3` puis Entrée.
3. Le HUD debug apparaît en haut à droite avec `CamDir`, `Zone`, `Pos`, `FPS`, etc.

L'overlay capture cette zone automatiquement.

---

## Raccourcis clavier (par défaut)

| Raccourci | Action |
|---|---|
| `Shift+F1` | Afficher/masquer l'overlay |
| `Shift+F2` | Ouvrir la fenêtre d'options |
| `Shift+F3` | **Snapshot rapide** des coordonnées courantes (POI sauvegardé à l'instant T) |
| `Shift+F4` | Ouvrir le gestionnaire de POI |

Les raccourcis sont reconfigurables via `Shift+F2`.

---

## Configuration (`config.ini`)

| Section | Clé | Valeur par défaut | Description |
|---|---|---|---|
| `[OCR]` | `engine` | `tesseract` | `tesseract` ou `paddle` (paddle est plus précis mais beaucoup plus lent en CPU — non recommandé pour le scan temps réel) |
| `[OCR]` | `scan_interval_ms` | `50` | Intervalle entre deux captures (ms) |
| `[Logging]` | `level` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `[Debug]` | `save_ocr_images` | `False` | Sauvegarde les images pré-traitées (`debug_capture_*.png`) à chaque scan |

---

## Architecture

```
┌──────────────────┐
│ ScreenCapture    │  mss → BGR → grayscale → upscale ×2 → CLAHE
│ (capture.py)     │  → 4 passes seuillage (fixe / Otsu / adaptatif / HSV)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ OCRProcessor     │  ThreadPoolExecutor → Tesseract sur chaque passe
│ (ocr.py)         │  → meilleure passe par score → regex Pos stricte
└────────┬─────────┘  → normalisation + extraction (x, y, z, ooc)
         ▼
┌──────────────────┐
│ NavigationEngine │  Charge POI système + user → set_target → distance
│ (navigation.py)  │  euclidienne planet-relative
└────────┬─────────┘
         ▼
┌──────────────────┐
│ VelocityTracker  │  Échantillonne pos sur 50 ms → vélocité EMA →
│ (velocity_tracker.py) │ direction de déplacement
└────────┬─────────┘
         ▼
┌──────────────────┐
│ GPSOverlay       │  PyQt6 transparent always-on-top → frame MFD
│ (main.py)        │  → couleur basée sur l'âge OCR
└──────────────────┘
```

**Threading :**
- Worker OCR sur `QThread` séparé (capture + OCR ne bloquent pas l'UI).
- `HotkeyListener` (pynput) sur son propre thread → signaux Qt avec `QueuedConnection` obligatoire.

**Repère de coordonnées :**
- Le HUD SC affiche **deux types de Pos** : `Root/SolarSystem` (relatif au système, mais les planètes orbitent → instable pour POI fixés) et `OOC_X` (relatif à l'ObjectContainer planet-bound, **stable**).
- SpaceDrive utilise **uniquement les coordonnées OOC** pour les POI et la navigation.

---

## Sécurité anti-cheat

SpaceDrive est **100 % externe** :
- Capture d'écran via `mss` (équivalent à un screenshot Windows).
- Aucune lecture/écriture dans le process Star Citizen.
- Aucune injection DLL, hook ou modification de fichier jeu.
- L'overlay PyQt6 est une fenêtre Windows standard, transparente au clic.

Star Citizen utilise **Easy Anti-Cheat (EAC)** depuis novembre 2021. Toute solution basée sur la lecture mémoire (type Sanderling pour EVE) entraînerait un ban. Notre approche OCR est la seule voie compatible.

---

## Roadmap

Plan d'optimisation OCR détaillé : [`docs/OCR_OPTIMIZATION_PLAN.md`](docs/OCR_OPTIMIZATION_PLAN.md)

**À venir :**
- Phase A — Pré-traitement par canal couleur (inspiré SC_OCR)
- Phase B — Validation par plage géographique + récupération du `.` manquant
- Phase C — Tesseract tuning (`classify_bln_numeric_mode`, user_words/patterns)
- Phase D — Template matching NCC custom pour les chiffres (~10 ms/frame)

---

## Contribuer

1. Fork → branche `feat/...` ou `fix/...`
2. Tests : `python -m pytest tests/`
3. Commits en français, format conventionnel (`feat:`, `fix:`, `refactor:`)
4. PR vers `main`

Le code utilisateur (messages affichés, logs) est en français. Les commentaires de code sont en français également.

---

## Licence

MIT — voir [LICENSE](LICENSE).

---

## Crédits

- **Tesseract OCR** — moteur OCR principal
- **mss** — capture d'écran rapide
- **PyQt6** — overlay
- **pynput** — hotkeys globaux
- **[SC-Toolbox-Beta-V2/SC_OCR](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2)** — inspiration architecture pré-traitement et template matching

Communauté Star Citizen — `o7`
