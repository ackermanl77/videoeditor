# Media Import Workflow

## Goal

Bring video, audio, and image files into the project's media bin so they can be dragged onto the timeline.

## Supported formats

`ui/media_bin.py::SUPPORTED_EXTENSIONS` drives the file-picker's filter, but decoding itself is generic — any container/codec the installed FFmpeg build can decode works, whether or not its extension is explicitly listed (the filter can always be switched to "All Files").

| Kind | Extensions |
|---|---|
| Video | `.mp4 .mov .mkv .avi .webm .m4v .flv .wmv .mpg .mpeg .3gp .3g2 .ts .mts .m2ts .ogv .vob` |
| Audio | `.mp3 .wav .aac .flac .ogg .m4a .opus .wma .aiff .aif` |
| Image | `.png .jpg .jpeg .bmp .webp .tiff .tif .gif` |

Still-image extensions are the one place format matters beyond the picker filter: `core/media_probe.py::IMAGE_EXTENSIONS` overrides ffprobe's "1-frame video stream" reading for these specific extensions so they're treated as `MediaType.IMAGE` (looped for a set duration) rather than `MediaType.VIDEO`. An image format not in that set would still import and decode, but would be misclassified as a very-short video clip.

## Steps

1. Click **Import** in the Media Bin panel, use **File > Import Media...** (`Ctrl+I`), or drag files directly onto the Media Bin. An empty bin draws its own placeholder text ("Drop video, audio, or image files here, or click Import above to get started" — `MediaListWidget.paintEvent()`, since `QListWidget` has no built-in placeholder-text equivalent to `QLineEdit`'s) instead of looking like an unlabeled blank rectangle; it disappears as soon as the first asset is added. The "Drag a clip onto the timeline..." hint below the list is likewise only shown once there's actually something to drag (`MediaBinWidget.set_assets()` toggles it), rather than describing an action that's impossible on an empty bin. `MediaBinWidget` overrides `dragMoveEvent` (in addition to `dragEnterEvent`/`dropEvent`) to accept file URLs — without it, `QWidget`'s default handler doesn't accept during the hover phase, so the cursor shows "not allowed" for the whole drag on several platforms even though the drop itself would still work on release; the override makes the cursor honestly reflect that the bin is a valid drop target throughout, matching the placeholder text's own promise.
2. Each new path is probed with `ffprobe` (`core/media_probe.py::probe_file()`) for duration, resolution, frame rate, codec, and whether it has video/audio streams.
3. A thumbnail is generated (`generate_thumbnail()`, a single JPEG frame at 1 second in, or frame 1 for still images) and, for anything with audio, a waveform peak cache (`core/waveform.py::generate_waveform()`).
4. The finished `MediaAsset` appears in the Media Bin with its thumbnail and duration.
5. Drag an asset onto the timeline, or double-click it to append it to the end of the first compatible track.

## Threading

All of the above (probing, thumbnailing, waveform extraction) runs on a background `QThread` via `ui/import_worker.py::ImportWorker`, not on the UI thread — a batch of large files no longer freezes the app. `MainWindow.import_media_paths()` starts the worker and shows progress in the status bar; only the final `Project.add_media_asset()` calls and Media Bin refresh happen back on the main thread, via the `asset_ready`/`failed`/`finished` signals.

Only one import batch can run at a time — starting a second import while one is in progress shows a message instead of queuing or interleaving the work.

## Missing / unreadable files

- A path that no longer exists on disk is reported per-file (`ImportWorker` checks `os.path.isfile()` before probing) and shown in a summary dialog once the batch finishes; it never silently becomes a zero-duration asset.
- Opening a `.vproj` project whose media pool references files that can no longer be found triggers the same warning (`MainWindow._warn_about_missing_media()`), listing up to 10 missing paths. Their clips are hatched red and marked offline; **File > Relink Media...** (`core/relink.py`, `ui/relink_dialog.py`, see [project-files/workflow.md](../project-files/workflow.md)) points them at the files' new location in one undo step.

## Caches

Thumbnails and waveforms are cached under `~/.video_editor/thumbnails/` and `~/.video_editor/waveforms/`, keyed by a hash of the source path + modification time — re-importing the same unmodified file is instant.

## Validation

`tests/test_waveform.py` covers the peak-extraction math (silence, full-scale, negative full-scale, odd trailing byte, bounded amplitude) and the cache load path (missing file, malformed JSON, missing keys) without invoking FFmpeg. `probe_file()`/`generate_thumbnail()` run against real FFmpeg in the integration tests (`tests/integration/test_p3_real_ffmpeg.py`, `test_p4_real_ffmpeg.py`: HDR/10-bit probing, thumbnails and filmstrips with the GPU-decode fallback) on lavfi-generated media; importing a representative real video, audio and image file and checking the Media Bin is still worth a manual pass. The empty-state placeholder/hint toggling was verified with a headless (`QT_QPA_PLATFORM=offscreen`) script: `hint_label.isVisible()` is `False` with zero assets, flips to `True` after `set_assets()` is called with at least one, and back to `False` after clearing to zero again; `MediaListWidget.paintEvent()`'s own placeholder text isn't independently unit-testable (it's drawn directly with `QPainter`, not a separate queryable widget), so it was instead confirmed by grabbing the whole window (`QWidget.grab()`) and visually inspecting the rendered pixmap. `dragMoveEvent`'s acceptance was verified directly by constructing a real `QDragMoveEvent` carrying a file `QUrl` and calling the handler, asserting `event.isAccepted()` — a real OS-level drag can't be driven from an unattended headless script, so this checks the handler's own logic rather than the full platform drag session.

## Parallel import, timeouts and proxies (P2)

`ImportWorker` probes every file in parallel on `min(4, cpu_count / 2)` threads, adds each asset to the bin as soon as it is probed, then generates thumbnails (at `min(1 s, duration / 2)`) and waveforms in a second parallel stage. **Cancel Import** (status bar or Media menu) stops work not yet started. ffprobe (30 s), thumbnails (30 s) and waveforms (60 s + duration / 20) have timeouts, and `core.ffmpeg_utils.run` defaults to 60 s. Probing also records pixel format, bit depth and colour tags; the bin marks HDR / 10-bit / offline / proxy. **Media > Generate Proxy** (or automatically for >1080p, 10-bit or HDR when enabled) encodes a 540p, 15-frame-GOP, no-B-frame H.264 proxy (NVENC when available) under `~/.video_editor/proxies/`.

## Subtitle files, filmstrips, RMS waveforms, GPU decoding (round 4)

- `.srt`/`.vtt` files given to Import Media (dialog or drop) are not probed as media: they go to `MainWindow.import_subtitle_file()` (see text-overlays/workflow.md).
- The import worker also makes a **filmstrip** for video files (`media_probe.generate_filmstrip`: 8 tiles across the whole file, cached next to the thumbnails, `MediaAsset.filmstrip_path`); the timeline draws the tiles for the clip's own source range (fixed here: the tile lookup divided by `source_out` instead of the file length, so a clip trimmed short of the file's end showed the file's last frames).
- Waveform caches hold peaks **and RMS** at 100 values per second (was 25). Older caches still load (RMS = peaks). The RMS sum runs in C (`map(operator.mul)`) so long files don't import noticeably slower.
- With `VIDEO_EDITOR_HWACCEL` set, thumbnails and filmstrips try the GPU decoder first and fall back to software; a failed attempt's image is deleted so it can't be served from the cache later. The audio-only waveform decode never uses it.
