# Open Video Editor

[![CI](https://github.com/ackermanl77/videoeditor/actions/workflows/ci.yml/badge.svg)](https://github.com/ackermanl77/videoeditor/actions/workflows/ci.yml)

A desktop, non-linear video editor built with **Python**, **PySide6** (Qt)
and **FFmpeg**. It has a professional-looking dark UI, a multi-track
timeline, a media bin, a live preview, an undo/redo history, and exports
finished videos through a real FFmpeg render pipeline. The codebase is
deliberately layered and documented so it is easy to read, test and extend.

## Features

- **Import** most common video (MP4, MOV, MKV, AVI, WebM, FLV, WMV, MPG,
  3GP, TS/M2TS, OGV, ...), audio (MP3, WAV, AAC, FLAC, OGG, Opus, WMA,
  AIFF, ...) and image (PNG, JPG, BMP, WebP, TIFF, GIF, ...) formats —
  drag-and-drop or file dialog; anything the installed FFmpeg build can
  decode works even beyond that list
- **Media bin** with automatically generated thumbnails
- **Multi-track timeline**: unlimited video and audio tracks, drag to move
  clips, drag clip edges to trim, split at the playhead, delete, mute/lock/
  hide per track, with an optional **Ripple** mode so Delete/Trim shift
  later same-track clips to close (or open) the gap
- **Markers/chapters**: bookmark points on the timeline, click to seek,
  snap clips to them, and have them embedded as real player-navigable
  chapters on export
- **Preview player** synced to the timeline playhead (play/pause/seek)
- **Clip properties**: volume, mute, playback speed, opacity, fade in/out
  (video and audio both)
- **Speed ramps**: vary a clip's playback speed within itself (e.g.
  normal → slow-motion → normal) via speed keyframes
- **Reverse and freeze frame**: play a clip (picture and sound) backwards,
  and/or hold its last frame for a set time at its end
- **Subtitles**: File > Import Subtitles (or drop a file) turns an SRT,
  WebVTT or ASS/SSA file into editable caption text clips on a "Subtitles"
  track; the export burns them in, embeds them as a soft subtitle track
  (MP4/MOV/MKV/WebM) or saves them as a `.srt` next to the video
- **Audio level meters** (peak and RMS) next to the preview controls, and
  title/action-safe guides over the preview
- **Per-clip Transform**: position, scale, and rotation — picture-in-picture,
  split-screen, off-center framing
- **Shorts / vertical video export**: crop-to-fill reframing (with a manual
  focus point) into a 9:16 canvas instead of letterboxing, a duration cap,
  and ready-made YouTube Shorts / TikTok / Instagram Reels presets
- **Keyframing** for Opacity and Position — animate them smoothly over a
  clip's duration instead of one constant value
- **Crossfade transitions** between adjacent clips on the same track
- **Per-clip color correction**: brightness, contrast, saturation, gamma
  (an effect in the clip's stack, each value keyframable)
- **Chroma key** (green-screen) compositing per clip
- **Text overlays**: content, font size, color, bold/italic, position,
  alignment
- **Undo/redo** for every editing operation (command-pattern history)
- **Project files** (`.vproj`, plain JSON) — save and reopen your edit, with
  autosave/crash recovery, and a **File > Open Recent** list of your last
  10 opened/saved projects
- **Export** to MP4 / MOV / MKV / AVI / FLV / WebM / animated GIF with
  resolution, frame-rate and quality presets (or two-pass
  target-file-size encoding), rendered by FFmpeg with a live progress bar
- **Render queue**: queue several exports (each a snapshot of the project
  and its settings) and let them render one after another in the
  background, with per-job progress, time left, cancel, retry and reorder
- **Keyboard shortcuts** for the operations you use most (Help > Keyboard
  Shortcuts lists them all)
- **Effect stack**: color correction, RGB and YUV chroma keys (with despill),
  LUT (.cube), sharpen, blur, vignette, video denoise and two-pass
  **stabilization** (vid.stab), in any order per clip; parameters marked
  animatable (color correction, vignette) take keyframes
- **Audio**: equal-power audio crossfades, volume keyframes, ducking with
  adjustable threshold/ratio/attack/release, master limiter, loudness targets
- **Editing tools**: multi-select (Ctrl+click, rubber band), copy/cut/paste,
  duplicate, paste attributes, insert/overwrite, roll/slip/slide and ripple
  move (also as drags: Ctrl+edge = roll, Alt = slip, Ctrl+Alt = slide,
  Shift = ripple move), razor, detach/linked audio, keyframe-at-playhead buttons
- **Screen readers**: the timeline exposes its tracks and clips (names,
  times, effects, selection) to assistive technology, with keyboard focus
- **Accurate preview frames**: while paused, show the exact export frame
  rendered by the export engine; next-clip preloading; preview proxies
- **Colour**: 8-bit SDR BT.709 by default (HDR tone-mapped to it), 10-bit
  SDR, or 10-bit **HDR passthrough** (HDR10/PQ or HLG, BT.2020, with the
  source's mastering metadata on CPU HEVC) for HDR footage; only the modes
  the chosen codec and this machine's encoders support are offered
- **Codecs**: H.264, HEVC, AV1 (CPU or NVENC, 10-bit where supported),
  ProRes 422 HQ, DNxHR HQ/HQX; export presets, remembered settings, exact
  23.976/29.97/59.94 rates
- **Robust projects**: versioned file format, relative media paths, Relink
  Media, per-session autosave, `.bak` on save
- **Optional GPU decoding**: **Playback > GPU Decoding** (or
  `VIDEO_EDITOR_HWACCEL=auto` / `d3d11va` / `cuda`) decodes video on the GPU for exports, preview frames, proxies and
  thumbnails; anything the GPU decoder can't handle falls back to normal
  software decoding automatically. Off by default (see the User Guide for when
  it helps)

## Project layout

```
video_editor/
├── main.py                  # Entry point (splash, taskbar icon, a .vproj argument, --self-test)
├── build.spec               # PyInstaller spec (folder build: dist/VideoEditor/)
├── build.py                 # `python build.py [--installer]`
├── version_info.txt         # Template for the exe's version resource (filled from core.__version__)
├── ffmpeg.lock              # Pinned FFmpeg build (version, URL, SHA256s) for CI and releases
├── requirements.txt
├── core/                    # Pure-Python editing engine (no Qt dependency)
│   ├── clip.py, track.py, project.py   # Data models + versioned .vproj save/load
│   ├── media_asset.py, media_probe.py  # Imported-media metadata, ffprobe, thumbnails/filmstrips
│   ├── waveform.py                     # Peak/RMS waveform caches (timeline + level meters)
│   ├── ffmpeg_utils.py                 # Locate/run ffmpeg/ffprobe, NVENC and GPU-decode probes
│   ├── history.py, commands.py         # Undo/redo (Command pattern) and the undoable operations
│   ├── edit_ops.py, timeline_nav.py    # Multi-clip edits (clipboard, roll/slip/slide...), navigation
│   ├── filter_graph.py                 # Builds the FFmpeg filter_complex graph
│   ├── effects.py                      # Effect registry (schema, animatable params) + instances
│   ├── output_color.py                 # 8/10-bit SDR and HDR (PQ/HLG) export targets
│   ├── stabilize.py                    # vid.stab motion-analysis pass + cache
│   ├── exporter.py, export_presets.py  # Runs FFmpeg with progress; export presets
│   ├── render_queue.py                 # Render queue model (project snapshots)
│   ├── frame_render.py, proxy.py       # Accurate preview frames; preview proxies
│   ├── subtitles.py, fonts.py          # SRT/WebVTT/ASS import, SRT export; font files for drawtext
│   ├── autosave.py, relink.py          # Per-session autosave; Relink Media matching
│   └── selftest.py                     # `--self-test`
├── ui/                      # PySide6 UI layer (imports from core, never the reverse)
│   ├── main_window.py                  # Menus, toolbar, panel layout, wiring
│   ├── media_bin.py, import_worker.py  # Media bin; background import
│   ├── preview_player.py               # Preview panel (QMediaPlayer, Accurate frame, meters)
│   ├── properties_panel.py             # Clip inspector panel
│   ├── export_dialog.py, render_queue.py  # Export dialog; Render Queue dock
│   ├── relink_dialog.py, help_dialogs.py  # Relink Media; shortcuts / User Guide / About
│   ├── theme.py                        # Dark Qt stylesheet
│   └── widgets/
│       ├── timeline_widget.py          # The timeline panel (QGraphicsView)
│       ├── timeline_items.py           # Clip / ruler / header graphics items
│       └── timeline_accessibility.py   # Screen-reader interfaces for tracks/clips
├── scripts/                 # fetch_ffmpeg.py (pinned FFmpeg into bin/), version_info.py
├── installer/VideoEditor.iss  # Per-user Inno Setup installer
├── tests/                   # Core unit tests (stdlib only), real-FFmpeg tests (tests/integration/)
│                            # and offscreen Qt tests; each kind skips without its dependency
├── docs/                    # USER_GUIDE.md, PROJECT_STATUS.md, workflows/
└── assets/
    └── icons/               # App icon (PNG + .ico), splash screen,
        └── toolbar/         # and the toolbar/menu icon set (all generated art)
```

**Why this layout?** `core/` contains the entire editing model and the
FFmpeg render pipeline with **zero GUI dependencies** — it can be unit
tested (see `tests/`), reused in a script, or driven by a different UI
entirely. `ui/` only *reads* from `core` and issues `Command` objects
through `core.history.History`, so every edit is undoable by construction
and the two layers can be worked on independently.

## Installing

Requires **Python 3.10+** and **FFmpeg** (with `ffprobe`). The app looks for
it in a `bin/` folder next to `main.py` (or inside the built app), then on your
`PATH`, then (`ffmpeg` only) in the `imageio-ffmpeg` package. On Windows,
`python scripts/fetch_ffmpeg.py` puts the exact, checksum-verified build CI and
releases use (`ffmpeg.lock`, FFmpeg 9.0.1 essentials) into `bin/`. Otherwise
install it on your `PATH`:

- Windows: https://ffmpeg.org/download.html (or `winget install ffmpeg`)
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg` (or your distro's package manager)

Then install the Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running

```bash
python main.py
```

## Documentation

New to the app? [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) walks through the whole interface and every feature in plain language, aimed at people using the app rather than building it.

`docs/PROJECT_STATUS.md` is the authoritative reference for architecture, entry points, release info, and known limitations — this README only covers day-to-day commands. Per-feature guides (media import, timeline editing, export, project files, packaging, ...) live under [docs/workflows/](docs/workflows/README.md) — these are implementation-focused, for contributors.

## Running the tests

The editing engine (`core/`) has no GUI dependency, so its tests run
anywhere Python does, without a display; the real-FFmpeg and offscreen Qt
tests run too when FFmpeg and PySide6 are available, and skip otherwise:

```bash
python -m pytest -q                       # or: python -m unittest discover -s tests -v
```

Current result (2026-10-07, Windows, with FFmpeg in `bin/` and PySide6):
**508 passed plus 89 subtests**, none skipped.

GitHub Actions (`.github/workflows/ci.yml`) runs this same command, plus a `compileall` syntax check, on every push and pull request — with no install step at all, since nothing under `core/` (what `tests/` exercises) depends on a third-party package. A second, Windows job installs the pinned FFmpeg and PySide6 and runs the full suite (real-FFmpeg integration tests and offscreen Qt tests, `QT_QPA_PLATFORM=offscreen`) plus `python main.py --self-test`.

To check an installation or a built exe without opening the GUI:

```bash
python main.py --self-test        # or: VideoEditor.exe --self-test
```

It prints the version, the FFmpeg it found, NVENC availability, the video-decoding setting and the 25 required filters, renders a one-second test export, and exits 0 on success.

## Building a standalone executable

This packages the app (with the Qt runtime) into a **folder**,
`dist\VideoEditor\` — `VideoEditor.exe` plus its `_internal\` folder — that
runs with no Python installation on the user's machine, and (on Windows)
into a per-user **installer** for that folder. The folder starts without
unpacking anything; the earlier single-file exe unpacked ~293 MB into
`%TEMP%` on every launch and left it behind whenever the app was killed.

### Windows, step by step

1. Open **PowerShell** (or Command Prompt) in the project folder.
2. Create and activate a virtual environment with the Python version releases
   use (`.python-version`, currently 3.14), then install the build
   dependencies at their tested versions:
   ```powershell
   py -3.14 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements-build.txt -c requirements-lock.txt
   ```
   (`requirements.txt` alone is the runtime; `scripts/make_lock.py`
   regenerates the lock from a clean venv when a bound is raised.)
3. *(Optional but recommended)* Bundle FFmpeg so your users don't need to
   install it separately: download an FFmpeg **Windows build** (e.g. from
   https://www.gyan.dev/ffmpeg/builds/, the "essentials" zip), and copy
   `ffmpeg.exe` and `ffprobe.exe` from its `bin\` folder into a new `bin\`
   folder at the root of this project (next to `build.spec`) — or just run
   `python scripts/fetch_ffmpeg.py`, which downloads and verifies the pinned
   build from `ffmpeg.lock` into `bin\`.
4. Build:
   ```powershell
   python build.py               # the folder only
   python build.py --installer   # the folder, then the installer (needs Inno Setup 6)
   ```
   `build.py` stops when the interpreter isn't the `.python-version` one
   (`--any-python` overrides). `--sign` Authenticode-signs the exe and, with
   `--installer`, the installer and its uninstaller (through Inno Setup's
   SignTool) with signtool (Windows SDK) and the certificate whose SHA-1
   thumbprint is in `VIDEO_EDITOR_SIGN_THUMBPRINT` (or `CODESIGN_THUMBPRINT`),
   timestamped (DigiCert by default; `VIDEO_EDITOR_TIMESTAMP_URL` or
   `CODESIGN_TIMESTAMP_URL` changes it).
5. Your app is at `dist\VideoEditor\VideoEditor.exe` — share the whole
   `dist\VideoEditor\` folder (zipped), not just the exe. With
   `--installer`, `dist\installer\VideoEditor-<version>-setup.exe` installs
   it per user (no administrator rights) under
   `%LOCALAPPDATA%\Programs\OpenVideoEditor`, adds a Start menu entry and
   opens `.vproj` projects on double-click. (If you skipped step 3, users
   also need FFmpeg installed and on their `PATH`.)

### macOS / Linux

Same idea, with the platform's own shell:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build.py
```

The output is the `dist/VideoEditor/` folder (the executable inside has no
extension); the installer is Windows-only.

Always build on the same OS you're targeting — PyInstaller does not
cross-compile (build on Windows for a `.exe`, on macOS for a macOS app).

### Automated builds

Push a version tag matching `core.__version__` (currently `0.2.0`:
`git tag v0.2.0 && git push origin v0.2.0`) and
`.github/workflows/release.yml` builds the app on GitHub's own Windows
runner (FFmpeg bundled), self-tests it, and publishes the installer
(`VideoEditor-<version>-setup.exe`) and a portable zip of the folder as
[GitHub Release](https://github.com/ackermanl77/videoeditor/releases) assets
— no local build needed. This repo is currently **private**, so downloading
it still needs a GitHub account with access to the repo (that becomes a
plain, no-login download only if the repo's visibility is ever changed to
public — a separate, deliberate decision, since that also exposes the
source/history). Running the workflow manually (Actions tab → Release → Run
workflow) instead of pushing a tag builds from the current branch and
publishes it as a prerelease, for a one-off test build.

## Keyboard shortcuts

| Action              | Shortcut       |
|----------------------|----------------|
| New / Open / Save     | Ctrl+N / Ctrl+O / Ctrl+S |
| Save Project As        | Ctrl+Shift+S |
| Import Media             | Ctrl+I |
| Export Video               | Ctrl+E |
| Show/hide Render Queue       | Ctrl+Shift+E |
| Undo / Redo                  | Ctrl+Z / Ctrl+Y |
| Play / Pause                   | Space |
| Split clip at playhead            | S |
| Delete selected clip                | Delete |
| Add text clip                         | T |
| Add marker at playhead                   | M |
| Zoom timeline in / out                   | Ctrl+= / Ctrl+- (or Ctrl+scroll) |
| Copy / Cut / Paste / Duplicate           | Ctrl+C / Ctrl+X / Ctrl+V / Ctrl+D |
| Insert / Overwrite media at playhead     | , / . |
| Razor tool                               | C |
| Roll / slip / slide / ripple move (drag) | Ctrl+edge / Alt / Ctrl+Alt / Shift |
| Select next/previous clip, edit its properties | Tab / Shift+Tab, Enter |
| Shuttle back / stop / forward            | J / K / L |
| Frame / second steps, start / end        | Left/Right, Shift+Left/Right, Home / End |
| Previous / next edit point, marker       | Up / Down, Shift+M / Ctrl+Shift+M |
| In / Out point, clear                    | I / O, Alt+X |
| Previous / next keyframe                 | Shift+K / Ctrl+Shift+K |
| All shortcuts                            | F1 |

## How editing works

- Every timeline mutation (add/move/trim/split/delete clip, add/remove
  track, change a clip property) is a `Command` object in `core/commands.py`
  executed through `core.history.History`. This is what gives you
  reliable, centralized undo/redo — new editing features should follow
  the same pattern rather than mutating the project directly.
- The **preview** plays the topmost active video clip (falling back to an
  audio-only clip when nothing is visible) via `QMediaPlayer`. It does
  **not** perform full multi-track alpha compositing in real time — that
  would require a custom video-frame compositor. This keeps the preview
  implementation simple and fast; while paused or scrubbing, the optional
  **Accurate frame** view shows the exact frame rendered by the export
  engine (`core/frame_render.py`). The **export**, by contrast, always
  renders the true, fully composited multi-track timeline (transitions,
  text overlays, opacity, fades and all) through FFmpeg — what you get in
  the exported file reflects the whole timeline, not just the preview.
- **Export** works by turning the project timeline into a single FFmpeg
  `-filter_complex` graph (`core/filter_graph.py`): every clip is trimmed,
  scaled, time-shifted and layered onto a base canvas with `overlay`; audio
  clips are trimmed, delayed to their timeline position and mixed with
  `amix`; text clips become `drawtext` filters. See that module's
  docstring for the details.

## Extending the app

- **A new effect**: register it in `core/effects.py` (its parameter schema,
  which parameters are animatable, and the FFmpeg filter text); the
  Properties panel's Effects list builds its controls from that schema, and
  `core/filter_graph.py` places it in the clip's chain.
- **A new editing operation**: add an undoable `Command` (or a composite of
  existing ones) in `core/commands.py` / `core/edit_ops.py` and call it from
  the UI through `History`.
- **Real multi-track WYSIWYG playback**: replace `QMediaPlayer` with a
  custom decoder + compositor (e.g. via PyAV) feeding a `QVideoSink`.
  Today only paused frames are exact (Accurate frame).

Known gaps are listed under "Known Limitations" in
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

## License

MIT — see `LICENSE`. Use, modify and redistribute freely.
