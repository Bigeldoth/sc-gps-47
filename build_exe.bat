@echo off
echo ========================================
echo SpaceDrive GPS - Build Executable
echo ========================================
echo.

echo [1/3] Installation des dependances...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo ERREUR: Installation des dependances echouee
    pause
    exit /b 1
)

echo.
echo [2/3] Build de l'executable avec PyInstaller...
pyinstaller --clean spaceDrive.spec
if %errorlevel% neq 0 (
    echo ERREUR: Build PyInstaller echoue
    pause
    exit /b 1
)

echo.
echo [3/3] Creation du dossier tesseract...
if not exist "dist\tesseract" mkdir "dist\tesseract"

echo.
echo ========================================
echo BUILD TERMINE !
echo ========================================
echo.
echo Votre executable se trouve ici:
echo %cd%\dist\spaceDrive.exe
echo.
echo IMPORTANT: Pour que l'OCR fonctionne, vous devez:
echo 1. Telecharger Tesseract portable depuis:
echo    https://github.com/UB-Mannheim/tesseract/wiki
echo 2. Extraire tesseract.exe dans: dist\tesseract\
echo.
echo Ou installez Tesseract normalement sur votre systeme.
echo.
pause
