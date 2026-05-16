# Licensing & monetization analysis — SpaceDrive GPS

**Target business model:** Freemium / SaaS (core free, premium features or hosted service).

This document audits every runtime dependency and trained-model asset against a freemium / SaaS distribution model and identifies what changes are required (or recommended) to keep the project legally monetizable.

---

## TL;DR

The **single blocker for closed-source / paid binary distribution is `PyQt6` (GPL-3)**. Every other dependency is permissive (Apache 2.0 / MIT / BSD / LGPL) and is compatible with freemium or SaaS distribution. Two viable paths:

1. **Migrate `PyQt6` → `PySide6` (LGPL-3)** — ~few hours of porting, API near-identical. Recommended.
2. **Keep PyQt6 and buy a Riverbank commercial license** — recurring per-developer fee, simpler legally but ongoing cost.

If the freemium model only distributes the **free tier as open-source under GPL-3** and gates premium features **server-side (SaaS)**, PyQt6 is fine on the client because the GPL obligations are met by the open-source release, and the SaaS server is not "distributed" in the GPL sense. This is the easiest monetization shape given the current stack.

---

## Dependency-by-dependency audit

| Dependency | License | Commercial OK? | Closed-source binary OK? | Notes |
|---|---|---|---|---|
| `mss` | MIT | ✅ | ✅ | No restrictions. |
| `numpy` | BSD-3 | ✅ | ✅ | No restrictions. |
| `opencv-contrib-python` | Apache 2.0 | ✅ | ✅ | Note: the Python wheel bundles `ffmpeg` (LGPL) and a couple of GPL codecs — disabled by default. As long as we don't enable GPL-only features (`x264`), we stay Apache. |
| `Pillow` | HPND (MIT-like) | ✅ | ✅ | |
| `pytesseract` | Apache 2.0 | ✅ | ✅ | Thin wrapper. |
| `tesseract` (system binary) | Apache 2.0 | ✅ | ✅ | Bundled `eng.traineddata` "best" is also Apache 2.0. Redistribution OK with attribution. |
| **`PyQt6`** | **GPL-3 or Commercial** | ⚠ Depends on distribution | **❌ NO without commercial license** | The blocker. See below. |
| `pynput` | LGPL-3 | ✅ | ✅ (dynamic link) | LGPL only requires that users can replace the lib — pip wheels satisfy this trivially. |
| `onnxruntime` | MIT | ✅ | ✅ | |
| `paddleocr` (optional) | Apache 2.0 | ✅ | ✅ | |
| `paddlepaddle` (optional) | Apache 2.0 | ✅ | ✅ | |
| `pyinstaller` (build only) | GPL-2 with bootloader exception | ✅ | ✅ | The exception specifically allows shipping non-GPL apps wrapped by PyInstaller. Confirmed pattern, no issue. |

### Trained-model assets

| Asset | Base license | Fine-tuned redistributable? |
|---|---|---|
| `eng.traineddata` ("best", base for Tesseract fine-tune) | Apache 2.0 | ✅ Yes, with attribution. |
| `PP-OCRv4_rec` (base for Paddle fine-tune) | Apache 2.0 | ✅ Yes, with attribution. |
| Our fine-tuned `spacedrive.traineddata` / `spacedrive_paddle_rec/` | Derived work — same Apache 2.0 | ✅ Ship out-of-the-box, add NOTICE entry. |
| `models/spacedrive_ocr.onnx` (TinyGlyphCNN, trained from scratch on our own data) | Our copyright, any license we choose | ✅ |

---

## The PyQt6 problem in detail

PyQt6 is dual-licensed:
- **GPL-3** — free of charge. Any program that *links* to it (PyQt apps do, at import time) must itself be GPL-3 when distributed.
- **Commercial** — Riverbank Computing, per-developer annual fee. Removes the GPL obligation.

What "distributed" means matters:
- **Desktop binary** sent to users = distribution → GPL kicks in → source must be available under GPL.
- **SaaS server** that users only interact with over HTTP = **not** distribution (the AGPL would catch this; GPL-3 does not). The server code can be closed.

### Implications for a freemium model

| Variant | PyQt6 viable? | Notes |
|---|---|---|
| Free desktop client, open-source under GPL-3, no paid binary | ✅ | Pure FOSS, monetize via donations/Patreon/sponsorships. |
| Free desktop client + paid features unlocked by license key, all client code shipped | ❌ with PyQt6 | The unlock layer is in the GPL'd binary → entire client must be GPL → users can patch out the unlock. **Migrate to PySide6 or buy commercial license.** |
| Free desktop client (GPL-3, with PyQt6) + paid SaaS backend (closed source, separate codebase) | ✅ | The client stays FOSS, the value-add (overlay routing API, multi-user routes, fleet sharing, premium POI databases, etc.) lives server-side. GPL does not reach the server. **Easiest monetization with current stack.** |
| Closed-source desktop binary sold or freemium-gated client | ❌ with PyQt6 | Must migrate to PySide6 (LGPL) or buy Riverbank commercial license. |

### PySide6 migration cost

API parity with PyQt6 is ~95%. The known mechanical changes:
- Imports: `from PyQt6.QtWidgets import …` → `from PySide6.QtWidgets import …`.
- Signal/slot syntax: `pyqtSignal` → `Signal`, `pyqtSlot` → `Slot`.
- Enum access: PyQt6 already uses fully-qualified enums (`Qt.Key.Key_A`), same in PySide6 — no change.
- `QAction` lives in `QtGui` in both — no change.
- `.ui` file loading: `uic.loadUi` → `QUiLoader().load(...)`.

Estimated effort for this codebase (one main window, one options dialog, a few sub-dialogs, no `.ui` files based on the source): **2-4 hours including testing**.

PySide6 is LGPL-3: dynamic linking (pip install, normal import) satisfies the LGPL — closed-source apps allowed.

---

## Recommended path for freemium / SaaS

Given the chosen model is freemium / SaaS, the **lowest-cost legally-clean shape** is:

1. **Keep the desktop client open-source under GPL-3 with PyQt6.** No migration needed today.
2. **Build the premium tier server-side**: shared routes, cloud-synced POIs, route analytics, fleet coordination, premium POI dataset (e.g. mining hotspots, scanner data). Closed-source backend, monetized via subscription.
3. **License auth via a thin client-side API client** — the client makes authenticated HTTPS calls; gated content lives on the server. The GPL-3 client is "just" a thin viewer for the user's own data + free POIs.
4. **If at some point we want to ship a paid native desktop premium (offline) feature**, migrate to PySide6 then. The migration is small enough that it doesn't need to happen preemptively.

### What to NOT do
- Ship a closed-source paid build of the current PyQt6 binary. That is the only outright license violation in the dependency stack.
- Forget to publish the GPL-3 source for the free client if we redistribute binaries (e.g. via a `pyinstaller` release). The source must be either bundled or linked from the download page.

---

## Action items (for monetization-readiness)

| Priority | Action | Effort |
|---|---|---|
| P0 | Add a `LICENSE` file declaring the project's chosen license (GPL-3 if keeping PyQt6, MIT/Apache if migrating to PySide6). | 5 min |
| P0 | Add a `NOTICE` or `THIRD_PARTY_LICENSES.md` listing every dependency + its license + attribution. | 30 min |
| P1 | Add NOTICE entries for the bundled trained models (`eng.traineddata` derivative, `PP-OCRv4_rec` derivative). | 10 min |
| P1 | Decide GPL-3 vs PySide6+permissive. If staying GPL-3, publish the source repo publicly before shipping binaries. | decision |
| P2 | If/when paid offline premium is on the roadmap, plan PySide6 migration. Mechanical, ~half a day. | 2-4 h |
| P3 | If SaaS backend handles user data, plan the standard GDPR / data-retention paperwork — out of scope for this doc. | — |

---

## Conclusion

**Yes, the project remains fully monetizable** under a freemium / SaaS model **without any code change today**. The only thing to clean up immediately is licensing paperwork (LICENSE + NOTICE files). A PyQt6 → PySide6 migration becomes necessary only if/when the business model evolves to selling a closed-source desktop binary — and even then it's a small, well-defined porting task.
