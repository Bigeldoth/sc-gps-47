# Changelog - SpaceDrive GPS

## Version 1.2.0 (2026-05-04)

### 🐛 Corrections critiques

#### Support résolution 2560x1440
- **Problème** : La zone de capture était fixe pour 1920x1080, causant une mauvaise qualité OCR en 2560x1440
- **Solution** : Calcul automatique de la zone de capture selon la résolution d'écran
- **Fichiers modifiés** : `src/capture.py`

#### Prétraitement OCR simplifié
- **Problème** : Le filtre de netteté déformait le texte et rendait l'OCR inefficace
- **Solution** : 
  - Retrait du filtre de netteté agressif
  - Seuillage simple et robuste (seuil fixe à 180)
  - Débruitage léger pour préserver la qualité
- **Résultat** : Meilleure reconnaissance du texte
- **Fichiers modifiés** : `src/capture.py`

#### Regex plus tolérant
- **Problème** : Le regex ne matchait pas les erreurs OCR courantes (km → kn, Km, an)
- **Solution** : Regex acceptant toutes les variations de "km" et casse flexible
- **Fichiers modifiés** : `src/ocr.py`

#### Logs Tesseract améliorés
- **Problème** : Détection silencieuse de Tesseract (print au lieu de logger)
- **Solution** : Logs détaillés pour chaque chemin testé et erreurs visibles
- **Fichiers modifiés** : `src/ocr.py`

### ✨ Améliorations

#### Mode debug pour captures
- Ajout d'une option `save_ocr_images` dans config.ini
- Sauvegarde automatique des captures avant/après traitement
- Facilite le diagnostic des problèmes OCR

---

## Version 1.1.0 (2026-05-04)

### 🐛 Corrections de bugs

#### Plantage Shift+F2
- **Problème** : L'application plantait lors de l'appui sur Shift+F2
- **Cause** : Erreur dans `show_interaction_menu()` avec `self.mapToGlobal(self.rect().center())`
- **Solution** : Utilisation de `QCursor.pos()` pour afficher le menu à la position du curseur
- **Fichiers modifiés** : `src/main.py`

#### Reconnaissance du système Stanton
- **Problème** : Le système n'était pas reconnu avec l'ID numérique "SolarSystem_9948564368677"
- **Cause** : Le code attendait un nom textuel comme "SolarSystem_Stanton"
- **Solution** : 
  - Ajout d'un dictionnaire de mapping `SYSTEM_ID_MAP` pour convertir les IDs numériques en noms
  - Mapping de "9948564368677" → "Stanton"
  - Amélioration du regex pour accepter les IDs numériques longs
- **Fichiers modifiés** : `src/ocr.py`

### ✨ Améliorations

#### Amélioration du prétraitement OCR
- **Augmentation du contraste CLAHE** : clipLimit 3.0 → 5.0 pour une meilleure détection
- **Ajout d'un filtre de netteté** : améliore la lisibilité du texte
- **Optimisation du seuillage adaptatif** : blockSize 11 → 15, C 2 → -2
- **Méthode de seuillage combinée** : utilise à la fois le seuillage adaptatif et Otsu
- **Résultat** : Détection plus robuste du texte "SolarSystem" même avec contraste variable
- **Fichiers modifiés** : `src/capture.py`

#### Logs de debug
- Ajout de logs détaillés pour le processus OCR :
  - Log du texte brut extrait
  - Log des lignes Zone détectées
  - Log des lignes Pos détectées
  - Log des coordonnées extraites avec succès
  - Log des erreurs de conversion
- **Fichiers modifiés** : `src/ocr.py`

#### Gestion d'erreurs
- Ajout de try/catch autour de `menu.exec()` pour éviter les plantages
- Messages d'erreur plus informatifs dans la system tray

### 📝 Notes techniques

**Pour ajouter d'autres systèmes** :
Éditez `src/ocr.py` et ajoutez les mappings dans `SYSTEM_ID_MAP` :
```python
SYSTEM_ID_MAP = {
    "9948564368677": "Stanton",
    "AUTRE_ID": "Nom_Systeme",
    # ...
}
```

**Pour consulter les logs** :
Les logs sont enregistrés dans `spacedrive.log` avec le niveau DEBUG activé dans `config.ini`.

### 🔍 Débogage

Si vous rencontrez des problèmes de reconnaissance :
1. Vérifiez le fichier `spacedrive.log`
2. Cherchez les lignes contenant "Texte OCR brut" pour voir ce qui est capturé
3. Vérifiez si les lignes "Zone" et "Pos" sont détectées
4. Si un nouveau système n'est pas reconnu, ajoutez son ID dans `SYSTEM_ID_MAP`

---

## Version 1.0.0 (Date précédente)

Version initiale du projet SpaceDrive GPS.
