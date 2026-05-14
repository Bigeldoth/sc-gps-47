# OCR Optimization Plan — SpaceDrive GPS

## Context

The reliability of OCR reading from the HUD `r_DisplayInfo 3` is the critical factor for the software. Imprecise reading (loss of decimals, aberrant values) causes navigation errors of up to 17 m on a saved POI.

This plan adopts a **pure NumPy NCC template matching** approach, ~1 ms latency, without heavy ML dependencies.

## Current state (v0.5.0)

```
Capture (mss, 600×150)
  → grayscale (BGR2GRAY)
  → upscale ×2 (INTER_LINEAR)
  → GaussianBlur 3×3
  → CLAHE clipLimit=3.0
  → 4 parallel passes (threshold 180, Otsu, adaptive, white HSV mask)
  → Tesseract OEM3 PSM6 on each pass
  → Score → best pass wins
  → Strict 3-4 decimal regex on Pos
```

**Strengths:**
- Strict regex rejects degraded readings (major fix for 17 m bug).
- Progressive color (green→red) visually signals freshness.
- 4 passes cover space/cockpit/lit room.

**Identified weaknesses:**
- Grayscale conversion discards color info — loss of contrast on white HUD.
- 4 parallel passes, HSV pass produces noise in ~50% of cases (logs).
- No cross-pass validation: 1 aberrant pass (~14 M km vs ~4 k km) can win on score.
- No physical validation (impossible velocity between scans).
- No template system for SC HUD characters.

## Chosen architecture

| Component | Approach | Latency |
|---|---|---|
| `preprocess.isolate_channel("auto")` | Choose R/G/B/max channel based on background stats | ~0.1 ms |
| `preprocess.otsu_threshold` | Pure NumPy Otsu on isolated channel | ~0.3 ms |
| `preprocess.denoise_if_needed` | Open 3×3 **only if** `std > 45` | ~0.5 ms (rare) |
| `segment.find_rows` | Horizontal projection → text bands | ~0.2 ms |
| `segment.split_glyphs_in_row` | Connected components + 2 px proximity merge | ~0.5 ms |
| `classify.classify_batch` | Shift-invariant NCC ±2×±1 px on templates | ~1 ms / 12 glyphs |
| `validate.validate_*` | Range + decimal recovery via confidence | ~0.05 ms |

**Main surprise**: despite "CNN-based" branding, it's **pure NumPy only**, no deep learning. Shift-invariant matching absorbs HUD subpixel wiggle in a single frame.

## 4-phase plan

### Phase A — Smart preprocessing (1-2 h, ~25% gain)

Adapt preprocessing to background color, without touching Tesseract.

**Tasks:**

1. **`src/capture.py`**: replace `cv2.cvtColor(BGR2GRAY)` with `_isolate_channel_auto(bgr)` function:
   - If `lum > 140` → invert grayscale (lit room)
   - If `R - G > 15` → R channel (red text)
   - If `G - R > 15` → G channel (green text)
   - Otherwise → `max(R, G, B)` (white text — our default case)
2. **Reduce to 2 thresholding passes**: Otsu on isolated channel + inverted Otsu (dark text on light background).
3. **Condition `GaussianBlur` on `std(channel) > 45`** (fast-path in normal conditions).
4. **Remove HSV pass** (covered by `isolate_channel` in auto mode).
5. **Keep pass3 (adaptive)** only as backup for very light uniform backgrounds.

**Expected impact:** accuracy on varied backgrounds, elimination of pass4 noise.

---

### Phase B — Validation and temporal smoothing (1 h, ~10% gain)

**Tasks:**

1. **Geographic range validation** in `src/ocr.py` after `_RE_POS.search()`:
   - Plausible OOC: `|X|, |Y|, |Z| < 30000 km` (Stanton system size ~60k km).
   - Reject otherwise.
2. **Impossible velocity validation** in `src/main.py` `_on_worker_result`:
   - Calculate `Δpos / Δt` between two scans.
   - If > 50 km/s (max plausible outside quantum) → reject, keep previous value.
3. **Missing decimal recovery**:
   - If regex fails but `\d{6,8}km` string exists, try inserting `.` at all plausible positions and accept the one in range.
4. **Multi-pass consensus**: if ≥ 2 passes converge within ±0.1 km, average; otherwise take best but mark low confidence.

**Expected impact:** rejection of hallucinations (14 M km readings), recovery of partial OCR.

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
5. **Phase E** (NCC-first + enhanced classification) — voir section dédiée ci-dessous.

## Phase E — NCC-first with enhanced classification

**Idée centrale** : découpler segmentation et classification.

- **Segmentation** : sur la passe binaire `otsu`. Otsu épaissit les caractères
  mais les composantes connexes restent séparées : les bounding boxes sont
  fiables même quand le texte est dégradé.
- **Classification** : sur l'image grayscale `enhanced` (sortie CLAHE).
  Préserve les gradients fins que le seuillage binaire détruit ; améliore
  significativement la corrélation NCC et les confidences ONNX.

**Pipeline** :
1. `capture()` retourne `{otsu, adaptive, enhanced}` (passe `otsu_inv`
   supprimée : elle détruisait les caractères dans tous les cas observés).
2. `ocr.extract_data()` exécute NCC **une seule fois** par frame :
   - segmente sur `otsu` via `find_glyph_regions`
   - classifie chaque crop découpé dans `enhanced`
   - reconstruit chaque ligne et applique la regex `Pos:`
3. Le résultat est mis en cache sur `self._frame_ncc_coords` et réutilisé
   par les workers Tesseract parallèles (plus de NCC dupliqué par passe).
4. Si NCC reconstruit l'intégralité du HUD (coords + zone), Tesseract est
   complètement court-circuité (chemin `[ncc-first]`).
5. Sinon, Tesseract tourne sur `{otsu, adaptive}` en parallèle pour
   récupérer `Zone:` / `CamDir:` / `OOC_*`.

**Charset NCC étendu** (`templates.EXPECTED_CHARS`) :
chiffres + unités + A-Z/a-z + `:` / `_` / espace. Le court-circuit total
Tesseract ne s'active qu'une fois les templates alphabétiques collectés via
`tools/dataset_builder.py`.

**Latences attendues** :
- NCC sur enhanced : ~10 ms (8 lignes max, ~30 glyphes, einsum vectorisé).
- Tesseract parallel (2 passes) : ~150 ms.
- Frame-dedup hash : ~0.1 ms si HUD inchangé.

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
