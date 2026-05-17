# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for SpaceDrive GPS.
#
# Output: one-folder bundle in dist/spaceDrive/ — `dist/spaceDrive/spaceDrive.exe`
# plus its runtime files. Inno Setup packs that folder into the user-facing
# installer (see installer/spaceDrive.iss).
#
# The Paddle sidecar (.venv-paddle/) is NOT bundled here — it lives under
# %LOCALAPPDATA%\SpaceDrive\ and is provisioned post-install by the Engine
# Manager via scripts/install_paddle.ps1. Same for paddle-vl.

block_cipher = None

a = Analysis(
    ['src\\main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        # Static game data
        ('data\\poi.json', 'data'),
        ('data\\templates', 'data\\templates'),  # NCC templates
        # Trained glyph classifier
        ('models\\spacedrive_ocr.onnx', 'models'),
        ('models\\spacedrive_ocr.classes.json', 'models'),
        # Assets
        ('assets\\icon.png', 'assets'),
        ('assets\\spacedrive.ico', 'assets'),
        # Default config (user override goes in %LOCALAPPDATA%\SpaceDrive\)
        ('config.ini', '.'),
        # Project license shown in About + by the installer
        ('LICENSE.txt', '.'),
        # Sidecar provisioners + worker (Engine Manager calls these post-install)
        ('scripts\\paddle_worker.py', 'scripts'),
        ('scripts\\install_paddle.ps1', 'scripts'),
        ('scripts\\install_paddle_vl.ps1', 'scripts'),
    ],
    hiddenimports=[
        'mss',
        'numpy',
        'cv2',
        'pytesseract',
        'PIL',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'pynput',
        'pynput.keyboard',
        'onnxruntime',
        'certifi',  # CA bundle for HTTPS downloads from inside the bundle
        # First-party — make sure PyInstaller bundles them even if it
        # can't follow every dynamic import chain.
        'app_paths',
        'ocr',
        'capture',
        'navigation',
        'velocity_tracker',
        'config_manager',
        'hotkey_listener',
        'engine_installer',
        'paddle_adapter',
        'paddle_service',
        'paddle_vl_adapter',
        'paddle_vl_service',
        'sc_ocr',
        'sc_ocr.preprocess',
        'sc_ocr.segment',
        'sc_ocr.classify',
        'sc_ocr.templates',
        'sc_ocr.onnx_classifier',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Big optional deps explicitly NOT bundled — they live in their own
        # sidecar venvs provisioned post-install.
        'paddle',
        'paddleocr',
        'torch',
        'torchvision',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-folder build: the EXE references the bundled libs/data in the
# surrounding folder. Faster startup than one-file (no temp extract).
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='spaceDrive',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\spacedrive.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='spaceDrive',
)
