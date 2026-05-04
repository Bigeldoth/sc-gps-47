# ⚡ Guide de Démarrage Rapide - SpaceDrive GPS

## ⚠️ IMPORTANT : Installation de Python

Votre Python actuel (Microsoft Store) n'est pas correctement configuré. Suivez ces étapes :

### Option 1 : Installer Python depuis python.org (RECOMMANDÉ)

1. Téléchargez Python depuis : https://www.python.org/downloads/
2. **IMPORTANT** : Cochez "Add Python to PATH" pendant l'installation
3. Redémarrez votre terminal/PowerShell

### Option 2 : Utiliser le Python du Microsoft Store

1. Ouvrez PowerShell en tant qu'administrateur
2. Exécutez : `python3.exe -m pip install --upgrade pip`

---

## 🚀 Build de l'Exécutable (Une fois Python installé)

### Étape 1 : Ouvrir PowerShell dans le dossier du projet

```powershell
cd c:\Users\patri\.cursor\projects\spaceDrive
```

### Étape 2 : Installer les dépendances

```powershell
python3.exe -m pip install -r requirements.txt
```

OU si vous avez installé Python depuis python.org :

```powershell
python -m pip install -r requirements.txt
```

### Étape 3 : Builder l'exécutable

```powershell
python3.exe -m PyInstaller --clean spaceDrive.spec
```

OU :

```powershell
python -m PyInstaller --clean spaceDrive.spec
```

---

## 📍 EMPLACEMENT DE VOTRE EXÉCUTABLE

Une fois le build terminé, votre exécutable sera ici :

```
📁 c:\Users\patri\.cursor\projects\spaceDrive\dist\spaceDrive.exe
```

**Chemin complet** : `c:\Users\patri\.cursor\projects\spaceDrive\dist\spaceDrive.exe`

---

## 🎮 Lancer l'Application

### Méthode 1 : Double-clic
Naviguez vers `dist\` et double-cliquez sur `spaceDrive.exe`

### Méthode 2 : PowerShell
```powershell
.\dist\spaceDrive.exe
```

### Méthode 3 : Explorateur Windows
1. Appuyez sur `Windows + E`
2. Collez dans la barre d'adresse : `c:\Users\patri\.cursor\projects\spaceDrive\dist`
3. Double-cliquez sur `spaceDrive.exe`

---

## 🎯 Fonctionnalités

### Hotkey Global : Shift+F1
- Affiche/masque l'overlay GPS en jeu
- Fonctionne même quand Star Citizen est au premier plan

### System Tray (Barre des tâches)
- Icône d'ordinateur dans la zone de notification
- **Clic droit** sur l'icône pour :
  - Afficher/Masquer l'overlay
  - Quitter l'application

### Overlay GPS
- Affiche vos coordonnées X, Y, Z en temps réel
- Affiche votre localisation actuelle
- Calcule la distance vers les points d'intérêt
- Mise à jour automatique toutes les secondes

---

## 📋 Prérequis pour l'Exécutable

### Tesseract OCR (OBLIGATOIRE)

L'application a besoin de Tesseract pour lire les coordonnées à l'écran.

**Installation** :
1. Téléchargez : https://github.com/UB-Mannheim/tesseract/wiki
2. Installez dans le chemin par défaut : `C:\Program Files\Tesseract-OCR\`
3. L'application détectera automatiquement Tesseract

---

## 🐛 Résolution de Problèmes

### "Python was not found"
→ Installez Python depuis python.org et cochez "Add to PATH"

### "pip is not recognized"
→ Utilisez `python -m pip` au lieu de `pip`

### L'overlay ne s'affiche pas
→ Appuyez sur Shift+F1 ou clic droit sur l'icône system tray

### OCR ne fonctionne pas
→ Installez Tesseract OCR (voir ci-dessus)

### L'exécutable ne se lance pas
→ Vérifiez que Tesseract est installé
→ Lancez depuis PowerShell pour voir les erreurs

---

## 📦 Fichiers Créés

Après le build, vous aurez :

```
spaceDrive/
├── build/              ← Fichiers temporaires (peut être supprimé)
├── dist/
│   └── spaceDrive.exe  ← 🎯 VOTRE EXÉCUTABLE ICI
└── spaceDrive.spec     ← Configuration du build
```

**Vous pouvez copier `spaceDrive.exe` n'importe où sur votre PC !**

---

## ✅ Checklist de Démarrage

- [ ] Python installé avec "Add to PATH"
- [ ] Tesseract OCR installé
- [ ] Dépendances installées (`pip install -r requirements.txt`)
- [ ] Build exécuté (`PyInstaller --clean spaceDrive.spec`)
- [ ] Exécutable trouvé dans `dist\spaceDrive.exe`
- [ ] Application lancée et icône visible dans system tray
- [ ] Hotkey Shift+F1 testé

---

## 🚀 Commandes Rapides (Copier-Coller)

```powershell
# Tout en une fois (après installation de Python)
cd c:\Users\patri\.cursor\projects\spaceDrive
python -m pip install -r requirements.txt
python -m PyInstaller --clean spaceDrive.spec
.\dist\spaceDrive.exe
```

---

**Bon vol dans le 'verse, Citizen ! o7 🚀**
