# Export Workflow

## Goal

Render the full, composited multi-track timeline to a single video file via FFmpeg.

## Formats

| Format | Video codec | Audio | Notes |
|---|---|---|---|
| `.mp4` | libx264 | aac | Default; `+faststart` for streaming-friendly playback. |
| `.mov` | libx264 | aac | |
| `.mkv` | libx264 | aac | |
| `.avi` | libx264 | aac | |
| `.flv` | libx264 | aac | |
| `.webm` | libvpx-vp9 | libopus | |
| `.gif` | gif (palette-based) | none | See below — a fundamentally different encode. |

## Steps

1. **File > Export Video...** or `Ctrl+E` (disabled with a message if the timeline is empty).
2. Choose the output path, container format, resolution (a preset or Custom), frame rate, and quality.
3. Optionally check **Two-pass, target file size** and enter a target size in MB instead of picking a CRF quality preset (not available for GIF — see below).
4. Click **Export**. If the output file already exists, you're asked to confirm overwriting it first.
5. Progress and status are shown live; **Cancel** stops the render (FFmpeg is terminated immediately, not just flagged).

## Animated GIF export

Selecting `.gif` disables the Quality and Two-pass controls (a GIF has no CRF/bitrate concept) and shows a note explaining why. `Exporter._export_gif()` uses FFmpeg's two-stage palette workflow — `palettegen` (one 256-color palette for the whole clip) into `paletteuse` (Bayer-dithered) — which looks substantially better than FFmpeg's default per-frame GIF quantizer. The output has no audio track at all; the base filter graph's `[aout]` pad (always present, even as silent `anullsrc` — see below) is fed into an `anullsink` so FFmpeg doesn't complain about an unmapped output. Loops infinitely (`-loop 0`).

## How the filter graph is built

`core/filter_graph.py::build_filter_graph()` walks every non-hidden video track bottom-to-top and every non-muted audio track, producing:
- One `-i` input per clip (trimmed with `-ss`/`trim`, or `-loop 1` for a still image).
- A `scale` + `pad` + `format=yuva420p` chain per video/image clip onto the target resolution, with chroma key, color correction, fade-in/out, and opacity (`colorchannelmixer=aa=...`) applied per clip — see [color-and-effects/workflow.md](../color-and-effects/workflow.md) for the chroma key/color correction/crossfade filters specifically.
- Adjacent clips with a crossfade transition enabled are merged with `xfade` into a single layer instead of two independent ones (see [color-and-effects/workflow.md](../color-and-effects/workflow.md)).
- Every video layer composited bottom-to-top onto a black base canvas via `overlay`.
- Text clips drawn last, on top of the composited video, via `drawtext` (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
- Every audible clip's audio trimmed, tempo-adjusted (`atempo`, chained for speeds outside FFmpeg's native 0.5-2.0 range), fade-adjusted (`afade`), volume-adjusted, delayed to its timeline position, and mixed via `amix`. If nothing is audible, a silent `anullsrc` track is emitted so the output always has an audio stream.

A clip's audio-mapping decision (`_clip_has_audio()`) prefers the matching media-pool asset's probed `has_audio`; if no pool entry matches the clip's source path, it falls back to probing the file directly rather than assuming audio is present — this avoids FFmpeg failing the whole export by mapping a non-existent audio stream.

## Single-pass vs. two-pass

- **Single-pass** (default): one FFmpeg run at a fixed CRF (`QUALITY_PRESETS`: 18/23/28 for High/Balanced/Smaller File).
- **Two-pass, target size**: `Exporter._export_two_pass()` computes a video bitrate budget from the target size in MB and the project's total duration (reserving 192kbps for audio), runs an analysis-only first pass (video only, output discarded), then encodes the real output at that bitrate in a second pass. Progress is reported across both passes (0-50% / 50-100%). Pass 1 also routes the base graph's `[aout]` pad into `anullsink` (like the GIF export path does) — see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md) for the real, previously-undetected bug this fixed.

## Chapters

Every project marker becomes a real, player-navigable chapter (for every format except GIF) — see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md) for how markers are created and how the chapter metadata is embedded.

## Cancellation and dialog close

`Exporter.cancel()` terminates the underlying FFmpeg subprocess immediately (not just on the next progress line). Closing the export dialog while a render is in-flight cancels it and waits up to 5 seconds for the thread to actually stop before allowing the dialog to close; if it hasn't stopped in time, the dialog stays open with a message instead of destroying a live thread.

## Validation

`tests/test_filter_graph.py` covers input/label construction, muted-clip audio exclusion, image `-loop` inputs, text `drawtext` generation, multi-clip mixing, and the empty-project fallback — all without invoking FFmpeg (they assert on the generated command/graph text). `tests/test_exporter.py` covers `core/exporter.py`'s own pure argument/file-construction logic the same way (chapters-file content, `-map_metadata` indexing, and — via a mocked `subprocess.Popen` that spawns nothing real — that pass 1's filter graph contains `anullsink`); this is a deliberate, narrow exception to the "no unit tests for `core/exporter.py`" rule below, for logic that doesn't actually require executing FFmpeg to verify. There is no automated test that runs FFmpeg end-to-end otherwise; every format (mp4/mov/mkv/avi/flv/webm/gif) was verified manually with a real FFmpeg install against a real imported media file, including three bugs real execution caught that text-only tests couldn't: `xfade` silently requiring constant-frame-rate inputs (see [color-and-effects/workflow.md](../color-and-effects/workflow.md)), GIF export failing with "output unconnected" until the base graph's always-present `[aout]` pad was routed into `anullsink`, and — found later, the same way — two-pass export's pass 1 having exactly the same unconnected-`[aout]` bug this whole time, for every project, not just ones with markers.
