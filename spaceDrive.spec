# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['src\\main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('data\\poi.json', 'data'),
        ('data\\templates', 'data\\templates'),  # Templates NCC (Phase D)
        ('assets\\icon.png', 'assets'),
        ('assets\\icon.ico', 'assets'),
        ('config.ini', '.'),
    ],
    hiddenimports=[
        'keyboard',
        'mss',
        'numpy',
        'cv2',
        'pytesseract',
        'PIL',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'ocr',
        'capture',
        'navigation',
        'sc_ocr',
        'sc_ocr.preprocess',
        'sc_ocr.segment',
        'sc_ocr.classify',
        'sc_ocr.templates',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='spaceDrive',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Pas de console pour une application GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\icon.ico',
)
