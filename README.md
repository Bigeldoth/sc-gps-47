# Star Citizen GPS v4.7 (Open Source)

GPS externe utilisant l'OCR pour la navigation "Off-Grid" dans Star Citizen 4.7.

## 🚀 Fonctionnalités
- **OCR Real-time :** Lit vos coordonnées X, Y, Z directement sur l'écran.
- **Navigation Vecteur :** Calcule la direction et la distance vers des points communautaires.
- **Overlay Transparent :** S'affiche par dessus le jeu sans injection (Anti-EAC Safe).
- **Base de Données POI :** Système simple en JSON pour ajouter vos propres points.

## 🛠️ Installation

1. **Prérequis :**
   - Python 3.10+
   - [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) installé sur votre PC.

2. **Installation des dépendances :**
   ```bash
   pip install -r requirements.txt
   ```

3. **Lancement :**
   ```bash
   python src/main.py
   ```

## 🛰️ Utilisation
Le GPS détectera automatiquement vos coordonnées si l'UI de Star Citizen est visible.
Pour ajouter des points d'intérêt, modifiez le fichier `data/poi.json`.

## ⚠️ Sécurité
Ce projet est 100% externe. Il ne lit pas la mémoire du jeu et ne modifie aucun fichier. Il utilise uniquement la capture d'écran, ce qui le rend invisible pour Easy Anti-Cheat.

---
Projet créé pour la communauté Star Citizen. 100% Gratuit et Open Source.
