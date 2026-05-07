# Star Citizen GPS v4.7 (Open Source)

GPS externe utilisant l'OCR pour la navigation "Off-Grid" dans Star Citizen 4.7.

## 🚀 Fonctionnalités
- **OCR Real-time :** Lit vos coordonnées X, Y, Z directement sur l'écran.
  - Support de **Tesseract** et **PaddleOCR** (plus rapide et précis)
  - Choix du moteur OCR configurable
- **Navigation Vecteur :** Calcule la direction et la distance vers des points communautaires.
- **Overlay Transparent :** S'affiche par dessus le jeu sans injection (Anti-EAC Safe).
- **Gestionnaire de POI :** Interface graphique complète pour gérer vos points d'intérêt.
  - Recherche instantanée dans la base de données
  - Ajout, édition et suppression de POI personnalisés
  - Tri par colonnes (Nom, X, Y, Z, Description)
  - Définition rapide de destination
- **Fenêtre d'Options :** Configuration intuitive avec thème sombre.
  - Réglage de la fréquence de scan OCR (50-2000 ms)
  - Personnalisation des raccourcis clavier
  - Sauvegarde automatique des paramètres
- **Hotkeys Globaux :** Contrôle complet même quand le jeu a le focus.

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

### Raccourcis Clavier (par défaut)
- **Shift+F1** : Afficher/Masquer l'overlay
- **Shift+F2** : Ouvrir la fenêtre d'options
- **Shift+F3** : Enregistrer la position actuelle
- **Ctrl+Shift+P** : Ouvrir le gestionnaire de POI

### Gestion des Points d'Intérêt
1. Appuyez sur **Ctrl+Shift+P** pour ouvrir le gestionnaire de POI
2. Utilisez la barre de recherche pour filtrer les POI
3. Double-cliquez sur un POI pour le définir comme destination
4. Utilisez les boutons pour ajouter, éditer ou supprimer des POI personnalisés
5. Le bouton "Aller" définit la destination et ferme la fenêtre

### Configuration
1. Appuyez sur **Shift+F2** pour ouvrir les options
2. Ajustez la fréquence de scan OCR avec le curseur
3. Modifiez les raccourcis clavier selon vos préférences
4. Cliquez sur "Enregistrer & Fermer" pour appliquer les changements

Le GPS détectera automatiquement vos coordonnées si l'UI de Star Citizen est visible.

### Utilisation de PaddleOCR (Optionnel - Performances améliorées)

PaddleOCR offre de meilleures performances que Tesseract (2x plus rapide, meilleure précision).

**Installation :**
```bash
# Pour CPU uniquement
pip install paddleocr paddlepaddle

# Pour GPU (nécessite CUDA)
pip install paddleocr paddlepaddle-gpu
```

**Activation :**
1. Ouvrez le fichier `config.ini`
2. Dans la section `[OCR]`, changez `engine = tesseract` en `engine = paddle`
3. Pour utiliser le GPU, changez `paddle_use_gpu = False` en `paddle_use_gpu = True`
4. Redémarrez l'application

**Note :** Si PaddleOCR n'est pas installé ou échoue, l'application basculera automatiquement vers Tesseract.

## ⚠️ Sécurité
Ce projet est 100% externe. Il ne lit pas la mémoire du jeu et ne modifie aucun fichier. Il utilise uniquement la capture d'écran, ce qui le rend invisible pour Easy Anti-Cheat.

---
Projet créé pour la communauté Star Citizen. 100% Gratuit et Open Source.
