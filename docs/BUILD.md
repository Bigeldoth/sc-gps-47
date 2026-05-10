# Build et déploiement

## Build local (PyInstaller)

```powershell
# Depuis la racine du projet
python -m PyInstaller --clean spaceDrive.spec
```

**Sortie** : `dist/spaceDrive.exe` (~80 Mo).

L'exécutable est portable : copie-le où tu veux, double-clic pour lancer. Tesseract OCR doit être installé séparément sur la machine cible (ou bundlé dans le dossier `tesseract/` à côté de l'exe — voir la spec).

## Build via GitHub Actions

Le workflow `.github/workflows/build.yml` génère automatiquement un exécutable Windows :
- À chaque push sur `main` ou `develop`.
- Disponible dans les artefacts du run GitHub Actions.

## Versioning

[Semantic Versioning](https://semver.org/) : `MAJOR.MINOR.PATCH`.

- Tag git pour chaque release : `git tag -a v0.6.0 -m "MFD redesign + précision OCR"`
- `setuptools_scm` lit le tag et expose la version au runtime.

## Configuration locale

```powershell
# 1. Copie config.ini.example vers config.ini si absent
# (config.ini est versionné par défaut avec des valeurs raisonnables)

# 2. Vérifie que Tesseract est installé
tesseract --version
```

## Tests

```powershell
python -m pytest tests/ -v
```

Couvre : navigation, calcul de bearing, parsing CamDir, calibration, vélocité.

## Débogage

- Logs : `spacedrive.log` (niveau via `[Logging] level` dans `config.ini`).
- Captures de debug : activer `[Debug] save_ocr_images = True` → écrit `debug_capture_original.png` et `debug_capture_processed.png` à chaque scan.
- Pour analyser une capture statique sans le jeu : pointer `[Debug] test_screenshot = chemin/vers/screenshot.png` dans `config.ini`.

## Structure du projet

```
spaceDrive/
├── src/
│   ├── main.py              # GPSOverlay + worker OCR
│   ├── capture.py           # Capture écran + pré-traitement
│   ├── ocr.py               # OCRProcessor (Tesseract/Paddle)
│   ├── navigation.py        # NavigationEngine + POI
│   ├── velocity_tracker.py  # Estimation vélocité
│   ├── calibration.py       # Calibration yaw cam ↔ monde
│   ├── config_manager.py    # Wrapper config.ini
│   ├── hotkey_listener.py   # pynput → signaux Qt
│   └── ui/
│       ├── options.py       # Fenêtre options
│       └── poi_manager.py   # Fenêtre POI
├── data/
│   ├── poi.json             # POI système (versionnés)
│   └── user_poi.json        # POI utilisateur (locaux)
├── tests/                   # pytest
├── docs/                    # Documentation
├── config.ini               # Configuration runtime
├── requirements.txt
├── setup.py
└── spaceDrive.spec          # Spec PyInstaller
```
