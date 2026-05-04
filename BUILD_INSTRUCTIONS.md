# SpaceDrive GPS - Build Instructions

## Prérequis

- Python 3.9+
- Tesseract OCR installé
- Pip
- PyInstaller

## Installation des Dépendances

```bash
pip install -r requirements.txt
```

## Configuration

1. Copiez `config.ini.example` vers `config.ini`
2. Ajustez les paramètres selon votre configuration

## Build Local

### Méthode 1 : PyInstaller

```bash
pyinstaller spaceDrive.spec
```

### Méthode 2 : GitHub Actions

Les builds sont automatiquement générés via GitHub Actions :
- Sur chaque push vers `main` et `develop`
- Crée un executable pour Windows

## Versioning

Le versioning suit la convention Semantic Versioning (MAJOR.MINOR.PATCH)
- Incrémentation automatique via les commits conventionnels
- Tags GitHub générés automatiquement

## Débogage

Consultez `spacedrive.log` pour les logs détaillés.