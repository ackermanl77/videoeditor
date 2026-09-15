# Project Status

## Architecture

```text
main.py (PySide6 QApplication entry point)
        |
        v
ui/  (MainWindow, TimelineWidget, PreviewPlayer, PropertiesPanel, MediaBinWidget, ExportDialog)
        |  reads Project/Track/Clip directly, but every mutation goes
        |  through a Command object executed via core.history.History
        v
core/ (Project, Track, Clip, History/Commands, FilterGraph, Exporter, MediaProbe, Waveform)
        |
        v
FFmpeg / FFprobe (subprocess, via core/ffmpeg_utils.py)
```

`core/` is the entire editing model and the FFmpeg render pipeline, with **zero Qt/GUI dependencies** — every module under `core/` can be imported and unit-tested (see `tests/`) without a display, and the whole test suite runs in well under a second. `ui/` only *reads* from `core` and issues `Command` objects (`core/commands.py`) through `core.history.History`, so every editing operation (add/move/trim/split/delete a clip, add/remove a track, change any clip property) is undoable by construction; the UI never mutates `Project`/`Track`/`Clip` directly outside of a `Command`.

The preview (`ui/preview_player.py`) and the export pipeline (`core/filter_graph.py` + `core/exporter.py`) are two independent renderers of the same `Project`, and they deliberately don't agree on everything: the preview plays only the topmost active video clip through `QMediaPlayer` (no real-time multi-track alpha compositing), while export always builds a full `-filter_complex` graph and renders the true composited timeline — transitions, opacity, fades, and text overlays included. This is a conscious simplicity/fidelity trade-off, not a bug; see [preview-playback/workflow.md](workflows/preview-playback/workflow.md).

`core/ffmpeg_utils.py` centralizes how `ffmpeg`/`ffprobe` are located (bundled next to a PyInstaller build, then system `PATH`, then the `imageio-ffmpeg` package as a last resort) so no other module has to know how the binary was found.

## Primary Entry Points

| Purpose | Entry point |
|---|---|
| Desktop application | `python main.py` |
| Standalone executable build | `python build.py` (wraps PyInstaller with `build.spec`) |
| Unit test suite (no Qt/display required) | `python -m unittest discover -s tests -v` |

There is a single UI entry point — unlike some Qt/Streamlit projects that accumulate several parallel front ends over time, this app has never had more than one `MainWindow`. `main.py` additionally shows a splash screen (if `assets/icons/splash.png` exists), sets the Windows taskbar `AppUserModelID` so the app doesn't get grouped under a generic Python icon, and warns (non-fatally) if FFmpeg can't be found on startup.

## Release

```text
dist/VideoEditor.exe        -- PyInstaller one-file build (Windows)
dist/VideoEditor             -- same, no extension (macOS/Linux)
```

Build with `python build.py` (uses `build.spec`; see [packaging/workflow.md](workflows/packaging/workflow.md)). PyInstaller does not cross-compile — build on the OS you're targeting. Bundling `ffmpeg.exe`/`ffprobe.exe` in a `bin/` folder next to `build.spec` before building is optional but recommended so end users don't need FFmpeg on `PATH`. Generated `build/` and `dist/` folders are build artifacts, not source — safe to delete and regenerate.

## Verification

```powershell
python -m unittest discover -s tests -v
```

36 tests as of this writing, covering `core/` only (`clip`/`track` duration and overlap math, command undo/redo, filter-graph text construction and escaping, project serialization round-trips, and waveform peak extraction). None of them require FFmpeg to be installed or a display to be present — they test the graph-building *text*, not actual FFmpeg execution. There is no automated UI test coverage (no Qt widget tests); UI changes are currently verified manually (headless smoke construction + interaction scripts under `QT_QPA_PLATFORM=offscreen`, not checked into the repo as a suite).

## Known Limitations

- **No automated UI tests.** `tests/` covers `core/` exhaustively but nothing under `ui/` — a regression in `TimelineWidget`, `PreviewPlayer`, or the dialogs would only surface manually or in production.
- **No project-level relink UI for offline media.** Opening a project whose source files have moved warns the user (see [project-files/workflow.md](workflows/project-files/workflow.md)) but there's no in-app way to point a clip at a new path; the file has to be restored at its original location.
- **Preview is intentionally not WYSIWYG** for multi-track composites, transitions, and text — see [preview-playback/workflow.md](workflows/preview-playback/workflow.md). This is a documented design trade-off, not a defect.
- **No ripple/insert editing mode** — moving or deleting a clip never shifts other clips on the same or other tracks; all edits are overwrite-style, matching the "non-overlapping clips per track" model enforced by `Track.has_overlap()`.

## Recently Added

- **Crossfade transitions between adjacent clips.** `Clip.transition_out`/`TransitionType` existed in the data model and round-tripped through `.vproj` files, but nothing ever read them — no UI control, and the export filter graph ignored them entirely. Setting a clip's transition to Crossfade (Properties panel, video/image clips only) now blends its tail into the next clip on the same track over `Clip.transition_duration` seconds, via `core/filter_graph.py::_find_crossfade_partner()` + FFmpeg's `xfade` filter, whenever that next clip immediately follows with no gap (clips can't overlap per `Track.has_overlap()`). Scope, deliberately: only one crossfade pair per boundary (chaining three or more consecutive crossfades isn't supported), video only (each side's audio stays independent, meeting at a clean cut rather than an `acrossfade`), and the pair's combined on-screen footprint is `transition_duration` seconds shorter than the sum of both clips' durations — nothing downstream on that track is auto-shifted to close the resulting gap, so there is a `transition_duration`-second gap on that track after a crossfaded pair unless the next clip is manually nudged left. See [color-and-effects/workflow.md](workflows/color-and-effects/workflow.md).
- **Per-clip color correction.** Video/image clips gained `brightness`/`contrast`/`saturation`/`gamma` fields (neutral defaults matching FFmpeg's `eq` filter, so an unedited clip never emits it) with sliders in the Properties panel's new "Color Correction" group.
- **Chroma key (green screen).** Video/image clips gained `chroma_key_enabled`/`chroma_key_color`/`chroma_key_similarity`/`chroma_key_blend`, applied via FFmpeg's `colorkey` filter right after the clip reaches `format=yuva420p` — the keyed-out pixels become transparent through the exact same alpha channel opacity/fades already use, so it composites with the existing `overlay` stack with no structural changes needed. Exposed in the Properties panel's new "Chroma Key" group.

## Recently Fixed

- **An audio-only clip's Fade In/Out sliders did nothing.** The Properties panel shows Fade In/Out for every non-text clip, including audio, and the values were saved to the project — but `core/filter_graph.py::_build_audio_chain()` never applied them; only a video clip's alpha channel got `fade=...:alpha=1`. **Fixed**: `_build_audio_chain()` now applies the matching `afade=t=in:...`/`afade=t=out:...` filters, so fades work for standalone audio clips and also duck a video clip's own embedded audio during its fade.
- **Track headers visually overlapped the first ~3 seconds of every track's clips and scrolled away with the timeline instead of staying pinned.** `ui/widgets/timeline_items.py`'s `TrackHeaderItem` (the sticky name/mute/lock/hide panel, `HEADER_WIDTH = 130`) lived at the same scene x-origin as `ClipItem`s and the ruler, with a higher z-value — so it sat on top of, rather than beside, the start of the timeline, and scrolling right made it disappear along with the content, losing track identity entirely. **Fixed**: clip items, the ruler, the playhead, and the snap guide now all start at scene x = `HEADER_WIDTH`; the header column and a new `CornerItem` (covering the top-left corner above the ruler) stay pinned to the viewport's left edge via `TimelineWidget._pin_header_items()`, connected to the view's horizontal scrollbar `valueChanged` signal and re-applied after every `rebuild()`. A headless smoke test (`QT_QPA_PLATFORM=offscreen`) exercising zoom + scroll + rebuild caught and fixed a follow-on crash from this change: `scene.setSceneRect()` can synchronously fire a scrollbar `valueChanged` mid-`rebuild()`, momentarily pointing `_pin_header_items()` at an already-deleted `CornerItem` from the just-cleared scene — fixed by creating the corner item before `setSceneRect()` runs, plus a defensive `try/except RuntimeError` around the pin loop.
- **Export could fail outright for a clip whose source file wasn't in the project's media pool.** `core/filter_graph.py` decided whether to map a clip's `[N:a]` audio stream by checking `project.find_asset_by_path()`, defaulting to `has_audio=True` when no matching pool entry existed (e.g. the asset had been removed from the pool, or the project file was hand-edited). If the clip's source genuinely had no audio track, FFmpeg would fail the entire `-filter_complex` graph trying to map a non-existent stream. **Fixed**: `_clip_has_audio()` now falls back to probing the file directly (`core.media_probe.probe_file`) when no pool asset matches, defaulting to `False` (silent, not a crash) if even that fails.
- **The "two-pass" export setting was inert.** `ExportSettings.two_pass` existed as a dataclass field but was never read anywhere in `Exporter.export()`, and the export dialog didn't even expose a control for it. **Fixed**: implemented real two-pass, target-file-size encoding — `Exporter._export_two_pass()` computes a video bitrate budget from a target size in MB and the project duration, then runs an FFmpeg analysis pass (video only, discarded output) followed by the real encode pass at that bitrate. `ui/export_dialog.py` gained a "Two-pass, target file size" checkbox and an MB field.
- **Importing media froze the UI.** `MainWindow.import_media_paths()` ran `probe_file()`/`generate_thumbnail()`/`generate_waveform()` synchronously in a loop on the UI thread, papering over the freeze with `QCoreApplication.processEvents()` — a batch of large files would visibly hang the app. **Fixed**: the work now runs in `ui/import_worker.py::ImportWorker` on its own `QThread` (the same pattern the export dialog already used), with a status-bar progress bar; the main thread only handles the final `Project`/UI updates via signal handlers.
- **No feedback for missing media.** Importing a path that no longer exists, or opening a `.vproj` referencing moved/deleted files, failed silently. **Fixed**: `ImportWorker` now checks `os.path.isfile()` before probing and reports a clear per-file error instead of mis-probing a nonexistent path; `MainWindow.open_project()` now warns (listing up to 10 paths) if any media in the loaded project's pool can't be found on disk.
- **Closing the export dialog while FFmpeg was still running could destroy a live `QThread`.** `ExportDialog.closeEvent()` cancelled and waited only 3 seconds for the render thread before closing regardless of whether FFmpeg had actually exited. **Fixed**: `Exporter.cancel()` now terminates the FFmpeg subprocess immediately (stored as `self._process`) instead of waiting for the run loop to next notice a cancellation flag between progress lines; `closeEvent()` now refuses to close (with a message asking the user to wait) if the thread still hasn't stopped after 5 seconds, rather than destroying it mid-flight.
- **No overwrite confirmation on export.** Picking an existing output filename silently replaced it. **Fixed**: `ExportDialog._on_export()` now asks for confirmation first.
- **No crash recovery.** A crash mid-edit lost all unsaved work, with no autosave of any kind. **Fixed**: `MainWindow` now autosaves the current project to a dedicated recovery slot (`~/.video_editor/autosave/autosave.vproj`, separate from the user's actual project file) every 2 minutes while dirty, and offers to restore it on the next launch if found; the slot is cleared on a successful save or a confirmed discard, so it never resurrects work the user explicitly saved or threw away.

See [workflows/README.md](workflows/README.md) for per-feature workflow guides.
