# Plan d'optimisation OCR — SpaceDrive GPS

## Contexte

La fiabilité de la lecture OCR du HUD `r_DisplayInfo 3` est le facteur critique du logiciel. Une lecture imprécise (perte de décimales, valeurs aberrantes) cause des erreurs de navigation jusqu'à 17 m sur un POI sauvegardé.

Ce plan adopte une approche **template matching NCC pure NumPy**, latence ~1 ms, sans dépendance ML lourde.

## État actuel (v0.5.0)

```
Capture (mss, 600×150)
  → grayscale (BGR2GRAY)
  → upscale ×2 (INTER_LINEAR)
  → GaussianBlur 3×3
  → CLAHE clipLimit=3.0
  → 4 passes parallèles (seuil 180, Otsu, adaptatif, masque HSV blanc)
  → Tesseract OEM3 PSM6 sur chaque passe
  → Score → meilleure passe gagne
  → Regex stricte 3-4 décimales sur Pos
```

**Forces :**
- Regex stricte rejette les lectures dégradées (correctif majeur du bug 17 m).
- Couleur progressive (vert→rouge) signale visuellement la fraîcheur.
- 4 passes couvrent espace/cockpit/pièce éclairée.

**Faiblesses identifiées :**
- Conversion grayscale jette l'info couleur — perte de contraste sur HUD blanc.
- 4 passes parallèles, dont la passe HSV produit du bruit dans ~50 % des cas (logs).
- Aucune validation cross-pass : 1 passe aberrante (~14 M km vs ~4 k km) peut gagner sur le score.
- Aucune validation physique (vitesse impossible entre deux scans).
- Aucun système de templates pour les caractères du HUD SC.

## Architecture retenue

| Composant | Approche | Latence |
|---|---|---|
| `preprocess.isolate_channel("auto")` | Choisit le canal R/G/B/max selon stats du fond | ~0.1 ms |
| `preprocess.otsu_threshold` | Otsu pure NumPy sur canal isolé | ~0.3 ms |
| `preprocess.denoise_if_needed` | Open 3×3 **seulement si** `std > 45` | ~0.5 ms (rare) |
| `segment.find_rows` | Projection horizontale → bandes de texte | ~0.2 ms |
| `segment.split_glyphs_in_row` | Composantes connexes + fusion proximité 2 px | ~0.5 ms |
| `classify.classify_batch` | NCC shift-invariant ±2×±1 px sur templates | ~1 ms / 12 glyphes |
| `validate.validate_*` | Plage + récup décimal manquant via confidence | ~0.05 ms |

**Surprise principale** : malgré le branding "CNN-based", c'est **uniquement du NumPy**, pas de deep learning. Le shift-invariant matching absorbe le wiggle subpixel du HUD en une seule frame.

## Plan en 4 phases

### Phase A — Pré-traitement intelligent (1-2 h, ~25 % gain)

Adapter le pré-traitement à la couleur du fond, sans toucher à Tesseract.

**Tâches :**

1. **`src/capture.py`** : remplacer `cv2.cvtColor(BGR2GRAY)` par une fonction `_isolate_channel_auto(bgr)` :
   - Si `lum > 140` → invert grayscale (pièce éclairée)
   - Si `R - G > 15` → canal R (texte rouge)
   - Si `G - R > 15` → canal G (texte vert)
   - Sinon → `max(R, G, B)` (texte blanc — notre cas par défaut)
2. **Réduire à 2 passes seuillage** : Otsu sur canal isolé + Otsu inversé (texte sombre sur fond clair).
3. **Conditionner `GaussianBlur` à `std(channel) > 45`** (fast-path en conditions normales).
4. **Supprimer la passe HSV** (couvert par `isolate_channel` en mode auto).
5. **Garder pass3 (adaptatif)** uniquement comme backup pour fonds uniformes très clairs.

**Impact attendu :** précision sur fonds variés, élimination du bruit pass4.

---

### Phase B — Validation et lissage temporel (1 h, ~10 % gain)

**Tâches :**

1. **Validation par plage géographique** dans `src/ocr.py` après `_RE_POS.search()` :
   - OOC plausible : `|X|, |Y|, |Z| < 30000 km` (taille système Stanton ~60k km).
   - Rejeter sinon.
2. **Validation par vitesse impossible** dans `src/main.py` `_on_worker_result` :
   - Calcul `Δpos / Δt` entre deux scans.
   - Si > 50 km/s (max plausible hors quantum) → rejeter, garder valeur précédente.
3. **Récupération du `.` manquant** :
   - Si la regex échoue mais qu'une chaîne `\d{6,8}km` existe, tenter d'insérer un `.` à toutes les positions plausibles et accepter celle dans la plage.
4. **Consensus multi-pass** : si ≥ 2 passes convergent à ±0.1 km, moyenner ; sinon prendre la meilleure mais marquer faible confiance.

**Impact attendu :** rejet des hallucinations (lectures à 14 M km), récupération d'OCR partiels.

---

### Phase C — Tesseract finement tuné (30 min, ~5 % gain)

**Tâches :**

1. **Ajouter `classify_bln_numeric_mode=1`** dans `_TESSERACT_CONFIG` pour forcer la normalisation numérique (réduit la confusion 5↔S, 0↔O, 1↔l).
2. **Tester `oem=2`** (legacy rapide) en parallèle : 30-50 % plus rapide, mais 2-4 % moins précis. À A/B tester sur logs réels.
3. **Créer un fichier `data/user_words.txt`** :
   ```
   Stanton
   Pyro
   Hurston
   Crusader
   ArcCorp
   microTech
   OOC_Stanton1_L2
   OOC_Stanton2_L1
   ...
   ```
   Passé via `-c user_words=<path>`.
4. **Créer un fichier `data/user_patterns.txt`** :
   ```
   Pos:\n*.\n\n\n\nkm
   ```
   Passé via `-c user_patterns=<path>`.
5. **Tester `psm=7`** (single text line) sur ROI tight : peut être plus précis que psm=6 si on segmente en amont.

**Référence :** [Tesseract ImproveQuality](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)

---

### Phase D — Template matching NCC custom (semaine, ~30 % gain final)

C'est l'aboutissement : remplacer Tesseract pour les chiffres par un classifieur NCC pur NumPy.

**Tâches :**

1. **Collecte de templates** :
   - Ajouter un mode `[Debug] save_glyph_crops = True` dans `config.ini`.
   - Logiciel sauvegarde chaque glyphe segmenté avec son label probable.
   - Session de 30 min en jeu → ~500 glyphes labellisés (auto via Tesseract sur cas vert haute confidence).
   - Validation manuelle.
2. **Module `src/sc_ocr/`** :
   - `preprocess.py`
   - `segment.py`
   - `classify.py` (NCC shift-invariant pure NumPy)
   - `templates.py` (chargement bibliothèque)
3. **Pipeline hybride** :
   - Tesseract reste pour l'extraction des **noms** (Zone, OOC).
   - NCC custom pour les **coordonnées numériques** (chiffres + `.` + `-` + `k` + `m`).
4. **Latence cible** : < 10 ms par frame (vs ~100 ms Tesseract actuellement).

---

## Anti-patterns à éviter

| Approche | Pourquoi pas |
|---|---|
| Memory reading | EAC actif depuis nov. 2021 → ban garanti |
| Real-ESRGAN super-resolution | 150-300 ms même GPU, incompatible avec scan 50 ms |
| PaddleOCR / EasyOCR CPU | 80-800 ms par image, viole la contrainte temps réel |
| Multi-frame averaging | +300 ms latence, le shift-invariant matching le rend inutile |
| Augmentation excessive du nombre de passes | Diminishing returns, le bruit cross-pass devient le problème |

## Ordre de priorité recommandé

1. **Phase A** (pré-traitement intelligent) — gros ROI, faible risque, base pour la suite.
2. **Phase B** (validation) — élimine les hallucinations résiduelles.
3. **Phase C** (Tesseract tuning) — quick wins triviaux.
4. **Phase D** (NCC custom) — long terme, vise la précision 99 %+.

## Métriques de succès

À mesurer après chaque phase via un harnais de logs analysé :

- **Taux de capture réussie** : % de scans avec `data["x"] is not None`. Cible : 95 %+ en conditions normales.
- **Précision pose-retour** : enregistrer un POI, s'éloigner 100 m, revenir → distance à l'arrivée. Cible : < 2 m.
- **Stabilité au repos** : variance des coordonnées sur 10 s immobile. Cible : < 0.5 m.
- **Latence end-to-end** : capture → résultat. Cible : < 50 ms.

## Liens utiles

- [Tesseract ImproveQuality](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)
- [PSM modes explained](https://pyimagesearch.com/2021/11/15/tesseract-page-segmentation-modes-psms-explained-how-to-improve-your-ocr-accuracy/)
- [Star Citizen EAC notice](https://starcitizen.tools/Easy_Anti-Cheat) — rappel pourquoi memory reading est exclu
