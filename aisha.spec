# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Aisha — one-folder Windows build.

Bundles the src/aisha package, the Live2D web runtime + model, fonts and icon.
Build:  pyinstaller aisha.spec --noconfirm
Output: dist/Aisha/Aisha.exe  (distribute the whole dist/Aisha folder / zip it)
"""

import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Ship the runtime assets end users need (Live2D model, JS libs, fonts, icon).
datas = [
    ("assets/live2d/web", "assets/live2d/web"),
    ("assets/fonts", "assets/fonts"),
    ("assets/icon.ico", "assets"),
]

# Hidden imports PyInstaller can miss (lazy imports, Qt WebEngine, providers).
hiddenimports = (
    collect_submodules("aisha")
    + [
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebChannel",
        "openai", "anthropic", "elevenlabs",
        "sounddevice", "soundfile", "numpy",
        "faster_whisper", "keyring.backends.Windows",
        "pptx", "docx", "openpyxl", "PIL",
    ]
)

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Aisha",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # windowed app (no console)
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Aisha",
)
