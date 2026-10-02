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
import re
import sys

block_cipher = None

root_dir = os.path.abspath(SPECPATH)
bin_dir = os.path.join(root_dir, "bin")

ffmpeg_binaries = []
if os.path.isdir(bin_dir):
    for name in os.listdir(bin_dir):
        lower = name.lower()
        if lower.startswith("ffmpeg") or lower.startswith("ffprobe"):
            ffmpeg_binaries.append((os.path.join(bin_dir, name), "."))

# Python modules never imported by the app. imageio_ffmpeg is only the
# last-resort FFmpeg locator in core/ffmpeg_utils.py (it handles its absence);
# when bin/ holds FFmpeg, the bundled binary is always found first, so the
# package's own 87.6 MB ffmpeg-win-x86_64 exe was dead weight in the bundle.
excluded_modules = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
]
if ffmpeg_binaries:
    excluded_modules.append("imageio_ffmpeg")

a = Analysis(
    ["main.py"],
    pathex=[root_dir],
    binaries=ffmpeg_binaries,
    # The Help > User Guide dialog reads docs/USER_GUIDE.md.
    datas=[("assets", "assets"), ("docs/USER_GUIDE.md", "docs")],
    hiddenimports=[
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    cipher=block_cipher,
)

# Qt DLLs the app never loads. Verified by walking the PE import tables of
# everything it does use (QtCore/Gui/Widgets/Multimedia/MultimediaWidgets/
# Network, the multimedia/platform/style/imageformat plugins): none of them
# reach Qt6Quick*, Qt6Qml* or Qt6Pdf*. The qpdf image-format plugin is the
# only thing that imports Qt6Pdf, so it goes too. opengl32sw.dll (Qt's
# software-OpenGL fallback, loaded dynamically) is deliberately kept until a
# built exe has been playback-tested without it.
_UNUSED_QT_BINARY = re.compile(r"(^|[\\/])(Qt6(Quick|Qml|Pdf)\w*\.dll|qpdf\.dll)$", re.IGNORECASE)
a.binaries = [entry for entry in a.binaries if not _UNUSED_QT_BINARY.search(entry[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = os.path.join(root_dir, "assets", "icons", "app_icon.ico")
# The Windows version resource is rendered from core.__version__ (the single
# version source) instead of a hard-coded 1.0.0.0; version_info.txt is the template.
sys.path.insert(0, os.path.join(root_dir, "scripts"))
from version_info import write_version_file  # noqa: E402

version_path = write_version_file(workpath)

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
    # Never UPX: compressing the Qt DLLs, python3xx.dll and the FFmpeg exes
    # triggers antivirus heuristics and can break CFG-protected DLLs. Explicit,
    # so a machine that happens to have upx on PATH builds the same exe.
    upx=False,
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
