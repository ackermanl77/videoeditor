# Packaging a Standalone Build Workflow

## Goal

Package the app (with the Qt runtime) into a single executable file end users can double-click, with no Python installation required.

## Steps

1. Create and activate a virtual environment, then `pip install -r requirements.txt` (includes `pyinstaller`).
2. *(Optional, recommended)* Download an FFmpeg build for the target OS and copy `ffmpeg`/`ffmpeg.exe` and `ffprobe`/`ffprobe.exe` into a `bin/` folder at the project root, next to `build.spec` — this bundles FFmpeg so end users don't need it installed separately. `build.spec` auto-detects any file in `bin/` whose name starts with `ffmpeg`/`ffprobe` and bundles it, no spec changes needed. Without this step, `core/ffmpeg_utils.py::find_binary()` falls back to the system `PATH`, then the `imageio-ffmpeg` package's bundled binary.

   **Use the "essentials" build, not "full."** On Windows, gyan.dev's own **full** build's `ffmpeg.exe`/`ffprobe.exe` are ~222MB *each* (statically links codecs/SDKs this app never uses — Decklink, fdk-aac, etc.), which would bloat `VideoEditor.exe` from ~60MB to 500MB+. The **essentials** build's binaries are ~98MB each — still statically linked with everything `CONTAINER_CODECS` actually needs (`libx264`, `libx265`, `libvpx`, `libopus`, `libmp3lame`) — for a ~135MB final exe. Confirmed both ways while bundling FFmpeg into a real release build: `curl -sIL https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip` resolves to the correctly-sized essentials package; the plain, un-suffixed `ffmpeg-release-full.zip` on the same page is the one to avoid here.
   To get exactly the build the release uses, run `python scripts/fetch_ffmpeg.py`: it downloads the FFmpeg version pinned in `ffmpeg.lock` (URL, archive SHA256 and each binary's SHA256) and installs `ffmpeg.exe`/`ffprobe.exe` into `bin/`, failing on any hash mismatch. `release.yml` and CI's integration job use the same script.

   `build.spec` excludes `imageio_ffmpeg` whenever `bin/` holds FFmpeg (its own 87.6 MB `ffmpeg` was never used in the packaged app) and drops the Qt Quick/Qml/Pdf modules and DLLs (plus the `qpdf` image plugin): walking the PE import tables of every Qt module/plugin the app uses reaches none of them. `opengl32sw.dll` is still bundled until a built exe has been playback-tested without it.
3. Run `python build.py` (a thin wrapper around `pyinstaller build.spec`).
4. The output is `dist/VideoEditor.exe` (Windows) or `dist/VideoEditor` (macOS/Linux, no extension) — a one-file build that unpacks itself to a temp folder on each launch (a couple of seconds slower to start than the source version, traded for "just one file to share").

## Cross-compilation

PyInstaller does not cross-compile — build on the same OS you're targeting (Windows for `.exe`, macOS for a macOS app, etc.).

## Where the bundle is found at runtime

`core/ffmpeg_utils.py::_bundle_dir()` uses `sys._MEIPASS` (set by a PyInstaller one-file build when unpacked) to locate a bundled `ffmpeg`/`ffprobe` next to the running executable or in its `bin/` subfolder, before falling back to `PATH`. `main.py` shows a non-fatal warning dialog if FFmpeg can't be found by any of those routes, since media import, thumbnails, preview, and export all depend on it.

## Validation

There is no automated test for the packaged build (PyInstaller output isn't something `unittest` exercises). Verify manually after each `python build.py`: launch the produced executable on a clean machine/VM without the dev virtual environment active, confirm the splash screen and taskbar icon appear, and exercise the FFmpeg-dependent path (import a file, play it, export a short clip) — with and without a bundled `bin/ffmpeg.exe` present, to confirm both the bundled-binary and system-`PATH` lookup paths work.

Confirming *what actually got bundled* doesn't need a second machine, and catches a stale/wrong build before ever launching the exe: PyInstaller's own `PyInstaller.archive.readers.CArchiveReader`/`ZlibArchiveReader` can inspect a built one-file exe directly —

```python
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
car = CArchiveReader("dist/VideoEditor.exe")
print([n for n in car.toc.keys() if "ffmpeg" in n.lower() or "ffprobe" in n.lower()])  # confirms the binaries are embedded, not just present in bin/ at build time
```

and, for confirming a specific *source-code* change actually made it into the build (not just an old cached copy under `build/`), extracting a module's compiled bytecode out of the `PYZ.pyz` entry and checking its `co_consts` for a string that only exists in the new code — this is how the Shorts-export UI strings were confirmed present after a rebuild, and how a stale pre-Shorts `dist/VideoEditor.exe` (whose timestamp predated every commit in the repo) was caught in the first place. Also always sanity-check the bundled `ffmpeg.exe`/`ffprobe.exe` themselves still run standalone (`bin/ffmpeg.exe -version`) before packaging — a build that embeds a corrupted or partially-downloaded binary would otherwise only surface as an export failure deep inside the packaged app.

## Version, self-test and UPX (P2)

- `core/__init__.py: __version__` is the single version source: `build.spec` renders the exe's Windows version resource from it (`scripts/version_info.py`, `version_info.txt` is the template), the About box and `.vproj` files show it, and `release.yml` fails when the pushed tag isn't `v<__version__>`.
- `VideoEditor.exe --self-test` (or `python main.py --self-test`) runs before any `QApplication`: version, FFmpeg/FFprobe paths and versions, NVENC, 18 required filters, and a real one-second export checked with ffprobe; exit code 0/1, report also in `%TEMP%\VideoEditor-selftest.txt` (the exe has no console). `release.yml` runs it on the built exe before publishing.
- `upx=False` is explicit. `docs/USER_GUIDE.md` is bundled for **Help > User Guide**.
- Still one-file, with static FFmpeg binaries: switching to onedir + an installer, or to a shared FFmpeg build, needs a build and installer verified on a real machine.
