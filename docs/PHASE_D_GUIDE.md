# Phase D : Template Matching NCC Custom — Guide Complet

## 🎯 Objectif

Remplacer Tesseract pour les **coordonnées numériques** par un classifieur **NCC pur NumPy** :
- **Latence** : ~10 ms/frame (vs ~100 ms Tesseract)
- **Précision** : 99 %+ sur coordonnées (avec vrais templates)
- **Architecture hybride** : Tesseract reste pour noms (Zone, OOC) **jusqu'à ce
  que les templates alphabétiques soient collectés** (voir Phase E).

## 🔀 Phase E — Segmentation / classification découplées

Depuis Phase E, la segmentation s'exécute sur la passe binaire `otsu` (fiable
pour les bounding boxes même quand le texte est épaissi) et la classification
sur l'image grayscale `enhanced` (sortie CLAHE — gradients préservés).
NCC tourne **une fois par frame** dans `extract_data()` ; ses coords sont
réutilisées par les workers Tesseract parallèles, et Tesseract est
court-circuité dès que NCC reconstruit le HUD complet (coords + zone).

`EXPECTED_CHARS` couvre désormais chiffres + unités + A-Z/a-z + `:`/`_`/`espace`.
Pour activer la reconnaissance complète du HUD par NCC, il faut donc collecter
des templates de **lettres** en plus des chiffres :

- les dossiers Windows-illégaux sont mappés via `_PATH_SAFE_MAP` :
  `:` → `_colon`, ` ` → `_space`, `.` → `_dot`, `-` → `_dash`, etc.
- `tools/dataset_builder.py` génère automatiquement ces dossiers.
- Une fois `data/templates/{_upA..Z, a-z, _colon, _space, ...}/` peuplés, le
  chemin `[ncc-first]` dans `extract_data()` se déclenche et Tesseract est
  complètement skippé.

> **NTFS case-folding** : `A/` et `a/` se résolvent au même dossier physique
> sur Windows. Les majuscules sont donc encodées via le préfixe `_up` :
> `A` → `_upA`, `Z` → `_upZ`, etc. Ce mapping est géré par
> `_PATH_SAFE_MAP` dans `src/sc_ocr/templates.py` et
> `tools/dataset_builder.py`.

### Génération synthétique initiale (alphabet A-Z, a-z)

Pour amorcer le NCC avant toute session de jeu, des templates synthétiques
peuvent être générés depuis une font visuellement proche du HUD SC
(Electrolize est le meilleur candidat — voir `tools/find_sc_font.py`).

```bash
# 1. Télécharger Electrolize (OFL — Google Fonts)
mkdir -p tools/fonts
curl -fsSL -o tools/fonts/Electrolize-Regular.ttf \
  "https://github.com/google/fonts/raw/main/ofl/electrolize/Electrolize-Regular.ttf"

# 2. Générer ~60 variantes par lettre (chiffres préservés s'ils existent déjà)
python tools/dataset_synthetic.py \
  --font tools/fonts/Electrolize-Regular.ttf \
  --samples-per-char 60 \
  --out data/templates \
  --chars "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
```

Les templates synthétiques sont moins précis que des captures réelles
(le moyennage des variantes augmentées floute le contour), donc les scores
NCC sur HUD réel peuvent être proches du seuil `NCC_THRESHOLD = 0.78`.
Une session de collecte via `dataset_builder.py` reste recommandée pour
affiner les lettres les plus utilisées (ABCDE OOC_ Pos: Zone: km).

---

## 📋 Processus en 3 étapes

### Étape 1 : Collecte de Templates (30 min en jeu)

#### 1.1 Activer la collecte

Éditer `config.ini` :
```ini
[Debug]
save_glyph_crops = True
```

Le logiciel sauvegarde chaque glyphe segmenté dans `data/glyphs/{TIMESTAMP}/`.

#### 1.2 Session de jeu

- **Lancer SpaceDrive** (`python src/main.py` ou executable)
- **Jouer ~30 min en Star Citizen** avec HUD visible (`r_DisplayInfo 3`)
  - Explore différentes zones : Hurston, Crusader, microTech
  - Varie l'éclairage (pièce sombre, cockpit, extérieur)
  - Mouvement + stationnaire (300+ glyphes par zone)

**Résultat attendu** : `data/glyphs/YYYYMMDD_HHMMSS_mmm/` avec ~500 PNG

#### 1.3 Vérifier la capture

```bash
ls -la data/glyphs/*/
# Exemple : data/glyphs/20260510_143025_456/
#   0000_r0.png  (glyph 0, row 0)
#   0001_r0.png
#   ...
#   0487_r2.png
```

---

### Étape 2 : Organisation Automatique (5-10 min)

Utilise Tesseract haute confiance pour labelliser automatiquement et organiser par caractère.

#### 2.1 Lancer le script

```bash
python tools/template_organizer.py
```

**Options disponibles :**
```bash
# Auto-détecte le dossier le plus récent
python tools/template_organizer.py

# Spécifier manuellement
python tools/template_organizer.py --glyph-dir data/glyphs/20260510_143025_456

# Chemin Tesseract personnalisé
python tools/template_organizer.py --tesseract-path "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

#### 2.2 Résultat

Structure générée dans `data/templates/` :
```
data/templates/
  ├── 0/
  │   ├── 0000_r0.png
  │   ├── 0015_r1.png
  │   └── ... (tous les '0' détectés)
  ├── 1/
  ├── ...
  ├── 9/
  ├── ./
  ├── -/
  ├── k/
  ├── m/
  └── _/
```

Le script affiche un rapport :
```
============================================================
RAPPORT ORGANISATION TEMPLATES
============================================================
Total glyphes traités : 512
Organisés avec succès : 487

Répartition par caractère :
  '0' :  42 templates
  '1' :  38 templates
  '2' :  41 templates
  ...
  '.' :  52 templates
  '-' :  28 templates
  'k' :  12 templates
  'm' :  11 templates
  ' ' :   5 templates

Problèmes rencontrés :
  Non classifiable : 15
  Caractère inattendu : 10
```

---

### Étape 3 : Validation Manuelle (10-20 min)

Parcourir les templates et supprimer les mal segmentés.

#### 3.1 Vérifier la qualité

```bash
# Unix/Mac
ls -la data/templates/0/ | head -5

# Windows PowerShell
Get-ChildItem data/templates/0 | Select-Object Name, Length | head -5
```

Pour chaque caractère, vérifier quelques images pour détecter :
- ✅ Glyphes nets et bien centrés
- ❌ Glyphes flous, partiels, mal segmentés

#### 3.2 Nettoyer manuellement (optionnel)

```bash
# Supprimer les mauvais glyphes
rm data/templates/0/0000_r0.png  # Exemple : glyph mal segmenté
rm data/templates/./0042_r1.png
```

---

## 🚀 Utilisation après Templates

### Vérifier le chargement

Au démarrage du logiciel :
```
2026-05-10 14:30:25 - ocr - INFO - Templates NCC chargés : {'total_chars': 12, 'chars': ['.', '-', '0', '1', ..., '9']}
```

Si absent :
```
2026-05-10 14:30:25 - ocr - INFO - Aucun template NCC trouvé, fallback vers Tesseract pour coordonnées
```

### Pipeline Hybride

1. **Tesseract** extrait les **noms** (Zone, OOC) → pas d'impact
2. **NCC custom** reconnaît les **coordonnées numériques** (chiffres + . - k m)
3. **Fallback Tesseract** si NCC échoue

### Logs

Rechercher "NCC" dans `spacedrive.log` :
```
[otsu] NCC reconstructed: 4133.5653km 2841.2147km 1023.4891km
[otsu] Position NCC récupérée : X=4133.5653 Y=2841.2147 Z=1023.4891
```

---

## 📊 Métriques de Succès

Après 1-2 sessions de jeu, vérifier :

| Métrique | Cible | Mesure |
|----------|-------|---------|
| **Taux de capture** | 95 %+ | % de scans avec `data["x"] != None` |
| **Latence end-to-end** | < 50 ms | capture → résultat |
| **Précision POI** | < 2 m | enregistrer POI, s'éloigner 100 m, revenir |
| **Stabilité au repos** | < 0.5 m | variance coords sur 10 s immobile |

### Outil de benchmarking

À implémenter : script pour mesurer latence + précision automatiquement.

---

## 🔧 Troubleshooting

### Aucun template chargé

1. Vérifier `data/templates/` existe
2. Vérifier au moins un sous-dossier (ex: `data/templates/0/`) existe
3. Vérifier `.png` fichiers dans les sous-dossiers

```bash
ls -R data/templates/
```

### Glyphes mal segmentés

- Réduire `min_glyph_width` dans `segment.py` si glyphes trop petits
- Augmenter `proximity_threshold` si glyphes fusionnés à tort
- Vérifier l'éclairage en jeu (`r_DisplayInfo 3` doit être bien lisible)

### NCC score bas (< 0.85)

- Vérifier que les templates sont bien centrés (pas d'énorme padding)
- Ajouter plus d'exemplaires (3-5 par caractère minimum)
- Vérifier que le HUD n'est pas trop pixelisé ou flou

---

## 📁 Structure du Projet

```
spaceDrive/
├── src/
│   ├── sc_ocr/
│   │   ├── __init__.py
│   │   ├── preprocess.py      # Isolation canal + seuillage
│   │   ├── segment.py         # Projection → glyphes
│   │   ├── classify.py        # NCC matching
│   │   └── templates.py       # Gestion templates
│   ├── capture.py             # Capture + segmentation optionnelle
│   ├── ocr.py                 # Pipeline hybride
│   └── main.py                # App principale
├── tools/
│   ├── template_organizer.py  # Organisation auto des glyphes
│   ├── rename_glyphs.py       # Renommage uniques des glyphes
│   └── generate_icon.py       # Génération de l'icône systray
├── assets/
│   ├── icon.png               # Icône haute résolution
│   └── icon.ico               # Icône Windows multi-tailles
├── data/
│   ├── glyphs/
│   │   └── YYYYMMDD_HHMMSS_mmm/  # Glyphes bruts collectés
│   └── templates/
│       ├── 0/
│       ├── 1/
│       ├── ...
│       ├── 9/
│       ├── -/
│       ├── k/
│       └── m/
└── docs/
    └── PHASE_D_GUIDE.md        # Ce fichier
```

---

## 🔬 Détails Techniques

### NCC Shift-Invariant

Chaque glyphe est testé aux positions ±2 px (horizontal), ±1 px (vertical) pour absorber le wiggle subpixel du HUD SC.

Latence par glyph :
- Normalisation : 0.05 ms
- NCC 25 shifts × templates : 0.9 ms
- **Total : ~1 ms par glyph**

Pour 12 glyphes (ex: `4133.5653 km`) : **~12 ms** (vs ~100 ms Tesseract)

### Seuil d'acceptation

Score NCC > 0.85 → accepter
Score NCC < 0.85 → fallback Tesseract

Ajustable dans `classify.py` ligne ~120.

---

## ⏭️ Prochaines Améliorations

1. **Augmentation de templates** : générer synthétiquement via rotation/zoom/bruit
2. **Fine-tuning Tesseract** : améliorer fallback avec user_words/patterns
3. **Mesure de latence** : ajouter profiling dans `ocr.py`
4. **Benchmarking automatique** : tester précision sur logs
5. **Support multi-font** : reconnaître variantes de taille/style

---

## 📝 Notes

- Phase D est **optionnelle** : sans templates, le système fallback 100 % Tesseract
- Templates collectés sont **spécifiques** à la résolution/HUD (600×150 px avec upscale ×2)
- Compatibilité **100 %** avec les règles de validation Phase B (plage géo, vitesse max)

---

**Bon test ! 🚀**
