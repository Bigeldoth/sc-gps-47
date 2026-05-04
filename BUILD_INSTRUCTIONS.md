# 🚀 Instructions de Build - SpaceDrive GPS

## Prérequis

1. **Python 3.8+** installé (vous l'avez déjà ✓)
2. **Tesseract OCR** installé sur votre système

### Installation de Tesseract OCR

Téléchargez et installez Tesseract depuis :
https://github.com/UB-Mannheim/tesseract/wiki

**Important** : Notez le chemin d'installation (par défaut : `C:\Program Files\Tesseract-OCR\`)

---

## 📦 Étapes de Build

### 1. Installer les dépendances Python

Ouvrez PowerShell ou CMD dans le dossier du projet et exécutez :

```powershell
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

### 2. Builder l'exécutable

```powershell
python3 -m PyInstaller --clean spaceDrive.spec
```

### 3. Localiser votre exécutable

Votre exécutable se trouve ici :
```
c:\Users\patri\.cursor\projects\spaceDrive\dist\spaceDrive.exe
```

---

## 🎮 Utilisation

### Lancer l'application

Double-cliquez sur `spaceDrive.exe` ou exécutez :
```powershell
.\dist\spaceDrive.exe
```

### Fonctionnalités

- **Overlay GPS** : Affiche automatiquement vos coordonnées en jeu
- **Hotkey Shift+F1** : Affiche/masque l'overlay
- **System Tray** : Icône dans la barre des tâches
  - Clic droit → Menu avec options
  - Afficher/Masquer l'overlay
  - Quitter l'application

### Configuration Tesseract

Si l'OCR ne fonctionne pas, vérifiez que Tesseract est installé :
- Chemin par défaut : `C:\Program Files\Tesseract-OCR\tesseract.exe`
- Le code détecte automatiquement ce chemin

---

## 🐛 Dépannage

### Erreur "pip not found"
```powershell
python3 -m ensurepip --upgrade
```

### Erreur PyInstaller
```powershell
python3 -m pip install --upgrade pyinstaller
```

### L'overlay ne s'affiche pas
- Vérifiez que Star Citizen est lancé
- Appuyez sur Shift+F1 pour afficher l'overlay
- Vérifiez l'icône dans la system tray

### OCR ne fonctionne pas
- Installez Tesseract OCR
- Vérifiez le chemin dans `src/ocr.py` ligne 18

---

## 📁 Structure du projet

```
spaceDrive/
├── dist/
│   └── spaceDrive.exe          ← VOTRE EXÉCUTABLE
├── src/
│   ├── main.py                 ← Application principale
│   ├── capture.py              ← Capture d'écran
│   ├── ocr.py                  ← Reconnaissance de texte
│   └── navigation.py           ← Calculs de navigation
├── data/
│   └── poi.json                ← Points d'intérêt
├── spaceDrive.spec             ← Configuration PyInstaller
└── requirements.txt            ← Dépendances Python
```

---

## ✨ Prochaines étapes

Une fois l'exécutable créé, vous pouvez :
1. Le copier n'importe où sur votre PC
2. Créer un raccourci sur le bureau
3. Le lancer avant de jouer à Star Citizen
4. Utiliser Shift+F1 pour afficher/masquer l'overlay en jeu

**Bon vol, Citizen ! o7**
