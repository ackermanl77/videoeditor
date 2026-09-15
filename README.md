# Open Video Editor

A desktop, non-linear video editor built with **Python**, **PySide6** (Qt)
and **FFmpeg**. It has a professional-looking dark UI, a multi-track
timeline, a media bin, a live preview, an undo/redo history, and exports
finished videos through a real FFmpeg render pipeline. The codebase is
deliberately layered and documented so it is easy to read, test and extend.

## Features

- **Import** video, audio and image files (drag-and-drop or file dialog)
- **Media bin** with automatically generated thumbnails
- **Multi-track timeline**: unlimited video and audio tracks, drag to move
  clips, drag clip edges to trim, split at the playhead, delete, mute/lock/
  hide per track
- **Preview player** synced to the timeline playhead (play/pause/seek)
- **Clip properties**: volume, mute, playback speed, opacity, fade in/out
- **Text overlays**: content, font size, color, bold/italic, position,
  alignment
- **Undo/redo** for every editing operation (command-pattern history)
- **Project files** (`.vproj`, plain JSON) — save and reopen your edit
- **Export** to MP4 / MOV / MKV / WebM with resolution, frame-rate and
  quality presets, rendered by FFmpeg with a live progress bar
- **Keyboard shortcuts** for the operations you use most

## Project layout

```
video_editor/
├── main.py                  # Application entry point (splash screen, taskbar icon, etc.)
├── build.spec                # PyInstaller build spec (produces the standalone .exe / app)
├── build.py                  # Convenience wrapper: `python build.py`
├── version_info.txt          # Windows .exe version resource (Company/Product/File version)
├── requirements.txt
├── core/                      # Pure-Python editing engine (no Qt dependency)
│   ├── clip.py                 # Clip / TextStyle data models
│   ├── track.py                # Track data model
│   ├── project.py               # Project model + JSON save/load
│   ├── media_asset.py            # Imported-media metadata
│   ├── media_probe.py             # ffprobe wrapper + thumbnail generation
│   ├── ffmpeg_utils.py             # Locate/run the ffmpeg & ffprobe binaries
│   ├── history.py                   # Undo/redo command stack (Command pattern)
│   ├── commands.py                   # Concrete undoable editing operations
│   ├── filter_graph.py                # Builds the FFmpeg filter_complex graph
│   └── exporter.py                     # Runs FFmpeg, reports progress
├── ui/                         # PySide6 UI layer (imports from core, never the reverse)
│   ├── theme.py                  # Dark Qt stylesheet
│   ├── main_window.py              # Menus, toolbar, panel layout, wiring
│   ├── media_bin.py                  # Media bin panel
│   ├── preview_player.py               # Preview panel (QMediaPlayer-based)
│   ├── properties_panel.py               # Clip inspector panel
│   ├── export_dialog.py                   # Export settings + progress dialog
│   └── widgets/
│       ├── timeline_widget.py                # The timeline panel (QGraphicsView)
│       └── timeline_items.py                   # Clip / ruler / header graphics items
├── tests/                     # Unit tests for the core engine (no Qt required)
└── assets/
    └── icons/                  # App icon (PNG + Windows .ico), splash screen,
        └── toolbar/              # and the toolbar/menu icon set (all generated art)
```

**Why this layout?** `core/` contains the entire editing model and the
FFmpeg render pipeline with **zero GUI dependencies** — it can be unit
tested (see `tests/`), reused in a script, or driven by a different UI
entirely. `ui/` only *reads* from `core` and issues `Command` objects
through `core.history.History`, so every edit is undoable by construction
and the two layers can be worked on independently.

## Installing

Requires **Python 3.10+** and **FFmpeg** (with `ffprobe`) installed and
available on your `PATH`.

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

`docs/PROJECT_STATUS.md` is the authoritative reference for architecture, entry points, release info, and known limitations — this README only covers day-to-day commands. Per-feature guides (media import, timeline editing, export, project files, packaging, ...) live under [docs/workflows/](docs/workflows/README.md).

## Running the tests

The editing engine (`core/`) has no GUI dependency, so its test suite runs
anywhere Python does, without a display:

```bash
python -m unittest discover -s tests -v
```

## Building a standalone executable

This packages the app (with the Qt runtime) into a **single executable
file** — `VideoEditor.exe` on Windows — that your users can double-click
to run, no Python installation required on their machine. (It takes a
couple of seconds longer to start than the Python version, since a
one-file build unpacks itself to a temp folder on each launch — a
worthwhile trade for "just one file to share".)

### Windows, step by step

1. Open **PowerShell** (or Command Prompt) in the project folder.
2. Create and activate a virtual environment, then install dependencies:
   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. *(Optional but recommended)* Bundle FFmpeg so your users don't need to
   install it separately: download an FFmpeg **Windows build** (e.g. from
   https://www.gyan.dev/ffmpeg/builds/, the "essentials" zip), and copy
   `ffmpeg.exe` and `ffprobe.exe` from its `bin\` folder into a new `bin\`
   folder at the root of this project (next to `build.spec`).
4. Build:
   ```powershell
   python build.py
   ```
5. Your app is at `dist\VideoEditor.exe`. Double-click it to run, or send
   that one file to anyone (if you skipped step 3, they'll also need
   FFmpeg installed and on their `PATH`).

### macOS / Linux

Same idea, with the platform's own shell:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build.py
```

The output is `dist/VideoEditor` (no extension).

Always build on the same OS you're targeting — PyInstaller does not
cross-compile (build on Windows for a `.exe`, on macOS for a macOS app).

## Keyboard shortcuts

| Action              | Shortcut       |
|----------------------|----------------|
| New / Open / Save     | Ctrl+N / Ctrl+O / Ctrl+S |
| Save Project As        | Ctrl+Shift+S |
| Import Media             | Ctrl+I |
| Export Video               | Ctrl+E |
| Undo / Redo                  | Ctrl+Z / Ctrl+Y |
| Play / Pause                   | Space |
| Split clip at playhead            | S |
| Delete selected clip                | Delete |
| Add text clip                         | T |
| Zoom timeline in / out                   | Ctrl+= / Ctrl+- (or Ctrl+scroll) |

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
  implementation simple and fast. The **export**, by contrast, always
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

A few natural next steps, each isolated to one or two files:

- **More effects** (color grading, blur, chroma key): add filter fragments
  in `core/filter_graph.py`'s per-clip chain, and matching controls in
  `ui/properties_panel.py`.
- **Keyframed properties** (e.g. animating opacity over time): extend
  `Clip` with keyframe data and generate the corresponding FFmpeg
  expression (e.g. `enable`/`if()` expressions) in `filter_graph.py`.
- **Proxy/low-res preview**: generate downscaled proxies in
  `core/media_probe.py` and have `PreviewPlayer` prefer them.
- **Real multi-track WYSIWYG preview**: replace `QMediaPlayer` with a
  custom decoder + compositor (e.g. via PyAV) feeding a `QVideoSink`.

## License

MIT — see `LICENSE`. Use, modify and redistribute freely.
