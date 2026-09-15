# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the video editor.

Build with:
    pyinstaller build.spec

To bundle FFmpeg with the app (recommended so end users don't need to
install it separately), copy your platform's ``ffmpeg``/``ffprobe``
binaries (``ffmpeg.exe``/``ffprobe.exe`` on Windows) into a ``bin/``
folder next to this spec file before building. They will be detected
automatically and included in the app bundle.
"""

import os

block_cipher = None

root_dir = os.path.abspath(SPECPATH)
bin_dir = os.path.join(root_dir, "bin")

ffmpeg_binaries = []
if os.path.isdir(bin_dir):
    for name in os.listdir(bin_dir):
        lower = name.lower()
        if lower.startswith("ffmpeg") or lower.startswith("ffprobe"):
            ffmpeg_binaries.append((os.path.join(bin_dir, name), "."))

a = Analysis(
    ["main.py"],
    pathex=[root_dir],
    binaries=ffmpeg_binaries,
    datas=[("assets", "assets")],
    hiddenimports=[
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = os.path.join(root_dir, "assets", "icons", "app_icon.ico")
version_path = os.path.join(root_dir, "version_info.txt")

# Single-file build: a.binaries/a.zipfiles/a.datas are passed straight into
# EXE (rather than via COLLECT), which is what makes PyInstaller produce one
# self-contained VideoEditor.exe under dist/ instead of a dist/VideoEditor/
# folder. This trades a couple of seconds of startup time (the exe unpacks
# itself to a temp dir on each launch) for the simplicity of "one file to
# share" that most end users expect from "an executable".
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="VideoEditor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
    version=version_path if os.path.exists(version_path) else None,
)
