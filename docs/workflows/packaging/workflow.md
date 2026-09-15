# Packaging a Standalone Build Workflow

## Goal

Package the app (with the Qt runtime) into a single executable file end users can double-click, with no Python installation required.

## Steps

1. Create and activate a virtual environment, then `pip install -r requirements.txt` (includes `pyinstaller`).
2. *(Optional, recommended)* Download an FFmpeg build for the target OS and copy `ffmpeg`/`ffmpeg.exe` and `ffprobe`/`ffprobe.exe` into a `bin/` folder at the project root, next to `build.spec` — this bundles FFmpeg so end users don't need it installed separately. Without this step, `core/ffmpeg_utils.py::find_binary()` falls back to the system `PATH`, then the `imageio-ffmpeg` package's bundled binary.
3. Run `python build.py` (a thin wrapper around `pyinstaller build.spec`).
4. The output is `dist/VideoEditor.exe` (Windows) or `dist/VideoEditor` (macOS/Linux, no extension) — a one-file build that unpacks itself to a temp folder on each launch (a couple of seconds slower to start than the source version, traded for "just one file to share").

## Cross-compilation

PyInstaller does not cross-compile — build on the same OS you're targeting (Windows for `.exe`, macOS for a macOS app, etc.).

## Where the bundle is found at runtime

`core/ffmpeg_utils.py::_bundle_dir()` uses `sys._MEIPASS` (set by a PyInstaller one-file build when unpacked) to locate a bundled `ffmpeg`/`ffprobe` next to the running executable or in its `bin/` subfolder, before falling back to `PATH`. `main.py` shows a non-fatal warning dialog if FFmpeg can't be found by any of those routes, since media import, thumbnails, preview, and export all depend on it.

## Validation

There is no automated test for the packaged build (PyInstaller output isn't something `unittest` exercises). Verify manually after each `python build.py`: launch the produced executable on a clean machine/VM without the dev virtual environment active, confirm the splash screen and taskbar icon appear, and exercise the FFmpeg-dependent path (import a file, play it, export a short clip) — with and without a bundled `bin/ffmpeg.exe` present, to confirm both the bundled-binary and system-`PATH` lookup paths work.
