# Export Workflow

## Goal

Render the full, composited multi-track timeline to a single MP4/MOV/MKV/WebM file via FFmpeg.

## Steps

1. **File > Export Video...** or `Ctrl+E` (disabled with a message if the timeline is empty).
2. Choose the output path, container format, resolution (a preset or Custom), frame rate, and quality.
3. Optionally check **Two-pass, target file size** and enter a target size in MB instead of picking a CRF quality preset.
4. Click **Export**. If the output file already exists, you're asked to confirm overwriting it first.
5. Progress and status are shown live; **Cancel** stops the render (FFmpeg is terminated immediately, not just flagged).

## How the filter graph is built

`core/filter_graph.py::build_filter_graph()` walks every non-hidden video track bottom-to-top and every non-muted audio track, producing:
- One `-i` input per clip (trimmed with `-ss`/`trim`, or `-loop 1` for a still image).
- A `scale` + `pad` + `format=yuva420p` chain per video/image clip onto the target resolution, with fade-in/out and opacity (`colorchannelmixer=aa=...`) applied per clip.
- Every video layer composited bottom-to-top onto a black base canvas via `overlay`.
- Text clips drawn last, on top of the composited video, via `drawtext` (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
- Every audible clip's audio trimmed, tempo-adjusted (`atempo`, chained for speeds outside FFmpeg's native 0.5-2.0 range), volume-adjusted, delayed to its timeline position, and mixed via `amix`. If nothing is audible, a silent `anullsrc` track is emitted so the output always has an audio stream.

A clip's audio-mapping decision (`_clip_has_audio()`) prefers the matching media-pool asset's probed `has_audio`; if no pool entry matches the clip's source path, it falls back to probing the file directly rather than assuming audio is present — this avoids FFmpeg failing the whole export by mapping a non-existent audio stream.

## Single-pass vs. two-pass

- **Single-pass** (default): one FFmpeg run at a fixed CRF (`QUALITY_PRESETS`: 18/23/28 for High/Balanced/Smaller File).
- **Two-pass, target size**: `Exporter._export_two_pass()` computes a video bitrate budget from the target size in MB and the project's total duration (reserving 192kbps for audio), runs an analysis-only first pass (video only, output discarded), then encodes the real output at that bitrate in a second pass. Progress is reported across both passes (0-50% / 50-100%).

## Cancellation and dialog close

`Exporter.cancel()` terminates the underlying FFmpeg subprocess immediately (not just on the next progress line). Closing the export dialog while a render is in-flight cancels it and waits up to 5 seconds for the thread to actually stop before allowing the dialog to close; if it hasn't stopped in time, the dialog stays open with a message instead of destroying a live thread.

## Validation

`tests/test_filter_graph.py` covers input/label construction, muted-clip audio exclusion, image `-loop` inputs, text `drawtext` generation, multi-clip mixing, and the empty-project fallback — all without invoking FFmpeg (they assert on the generated command/graph text). There is no automated test that actually runs FFmpeg end-to-end; verify a real export manually with a project containing at least one video clip, one audio clip, one text overlay, and a clip with no matching media-pool asset (to exercise the audio-mapping fallback).
