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
2. Choose the output path, container format, resolution (a preset or Custom), frame rate, quality, audio bitrate (128/192/256/320 kbps) and **Loudness** (Off, -14 LUFS web, -16 LUFS podcast, -23 LUFS EBU R128). The **Format** combo is authoritative: changing it rewrites the output path's extension, and typing a different known extension into the path selects that format (the exporter picks the codec from the extension, and choosing `.webm`/`.gif` with the default `...mp4` path used to still produce an MP4). In/Out points set on the timeline (`I`/`O`) pre-fill **Trim Range**.
3. Optionally check **Two-pass, target file size** and enter a target size in MB instead of picking a CRF quality preset (not available for GIF — see below).
4. Click **Export**. If the output file already exists, you're asked to confirm overwriting it first.
5. Progress and status are shown live; **Cancel** stops the render (FFmpeg is terminated immediately, not just flagged).

## Animated GIF export

Selecting `.gif` disables the Quality and Two-pass controls (a GIF has no CRF/bitrate concept) and shows a note explaining why. `Exporter._export_gif()` uses FFmpeg's two-stage palette workflow — `palettegen` (one 256-color palette for the whole clip) into `paletteuse` (Bayer-dithered) — which looks substantially better than FFmpeg's default per-frame GIF quantizer. The output has no audio track at all; the base filter graph's `[aout]` pad (always present, even as silent `anullsrc` — see below) is fed into an `anullsink` so FFmpeg doesn't complain about an unmapped output. Loops infinitely (`-loop 0`).

## How the filter graph is built

`core/filter_graph.py::build_filter_graph()` walks every non-hidden video track bottom-to-top and every non-muted audio track, producing:
- One `-i` input per clip (`-ss` seek plus a `-t` bound of the clip's trim + 0.5 s, so a decoder stops shortly after the trim point instead of reading to the end of the source; `-loop 1 -t` for a still image).
- A `scale` + `pad` + `format=yuva420p` chain per video/image clip onto the target resolution, with chroma key, color correction, fade-in/out, and opacity (`colorchannelmixer=aa=...`) applied per clip — see [color-and-effects/workflow.md](../color-and-effects/workflow.md) for the chroma key/color correction/crossfade filters specifically.
- Adjacent clips with a crossfade transition enabled are merged with `xfade` into a single layer instead of two independent ones (see [color-and-effects/workflow.md](../color-and-effects/workflow.md)).
- Each layer placed at its timeline position with `setpts=PTS+<start>/TB` (it used to be `tpad=start_duration=<start>`, which generated `start x fps` transparent full-canvas frames per clip for `overlay` to blend -- export cost grew with the *sum* of all clip start times, quadratically for a sequential edit; the output is frame-identical, see Validation), then composited bottom-to-top onto a black base canvas via `overlay` (`eof_action=pass`).
- **Trimmed exports build only their window** (`trim_range`, used by Trim Range and the Shorts cap): clips entirely outside it aren't in the graph at all, a clip straddling the window's start has its input seeked to the window (its frames re-stamped to clip-local time so fades/opacity keyframes still land correctly), and every layer, text overlay and position-keyframe expression is placed relative to the window's start. Speed-ramped clips and crossfade pairs (which have several inputs) are trimmed after their own chain instead of seeked, so a window starting inside one still decodes that one clip from its start. The last 5 s of an 80 s timeline used to cost as much as the whole export.
- Text clips drawn last, on top of the composited video, via `drawtext` (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
- Every audible clip's audio trimmed, tempo-adjusted (`atempo`, chained for speeds outside FFmpeg's native 0.5-2.0 range), fade-adjusted (`afade`), volume-adjusted, delayed to its timeline position, and mixed via `amix`. Each clip is first resampled to the project sample rate and a stereo layout (`aresample`/`aformat`), so mixed 44.1/48 kHz, mono and 5.1 sources no longer depend on amix's own negotiation, and the master always ends in a peak limiter (`alimiter=limit=0.891:level=false:latency=true`, -1 dBFS): `amix ... normalize=0` sums its inputs, and two ordinary tracks measured +3.7 dBFS before. If nothing is audible, a silent `anullsrc` track is emitted so the output always has an audio stream.

## Loudness normalization

With **Loudness** set, `Exporter` runs an extra audio-only measurement pass (`build_filter_graph(audio_only=True)`, so no video is rendered) through `loudnorm=I=<target>:TP=-1:LRA=11:print_format=json`, then renders with `loudnorm=...:measured_I=...:linear=true,aresample=<rate>` appended to the master (a single gain change, not dynamic compression). A silent mix is left alone. Verified with real FFmpeg: a -16 LUFS export measured -16.0 LUFS integrated.

A clip's audio-mapping decision (`_clip_has_audio()`) prefers the matching media-pool asset's probed `has_audio`; if no pool entry matches the clip's source path, it falls back to probing the file directly rather than assuming audio is present — this avoids FFmpeg failing the whole export by mapping a non-existent audio stream.

## Single-pass vs. two-pass

- **Single-pass** (default): one FFmpeg run at a fixed CRF (`QUALITY_PRESETS`: 18/23/28 for High/Balanced/Smaller File).
- **Two-pass, target size**: `Exporter._export_two_pass()` computes a video bitrate budget from the target size in MB and the project's total duration (reserving 192kbps for audio), runs an analysis-only first pass (video only, output discarded), then encodes the real output at that bitrate in a second pass. Progress is reported across both passes (0-50% / 50-100%). Pass 1 also routes the base graph's `[aout]` pad into `anullsink` (like the GIF export path does) — see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md) for the real, previously-undetected bug this fixed.

## Chapters

Every project marker becomes a real, player-navigable chapter (for every format except GIF) — see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md) for how markers are created and how the chapter metadata is embedded.

## Cancellation and dialog close

`Exporter.cancel()` terminates the underlying FFmpeg subprocess immediately (not just on the next progress line). Closing the export dialog while a render is in-flight cancels it and waits up to 5 seconds for the thread to actually stop before allowing the dialog to close; if it hasn't stopped in time, the dialog stays open with a message instead of destroying a live thread.

## Validation

`tests/integration/test_real_ffmpeg.py` runs real FFmpeg on lavfi-generated fixtures (skipped without FFmpeg): framemd5 identity of the setpts graph against the same graph with the old `tpad` shift, a trimmed export matching the same frames of the full export, a clip-count scaling guard (t16/t4 < 6), the limiter's -1 dBFS ceiling, loudness hitting its target, and trimmed-export duration. Measured on a 1080p30 source (`-f null`, this machine): 16 sequential 5 s clips 14.7 s -> 4.7 s, 32 clips 49.7 s -> 9.5 s, the last 5 s of an 80 s timeline 15.1 s -> 1.1 s; old and new graphs gave identical video `framemd5` for full exports (and bit-identical decoded audio before the limiter/format changes).

`tests/test_filter_graph.py` covers input/label construction, muted-clip audio exclusion, image `-loop` inputs, text `drawtext` generation, multi-clip mixing, and the empty-project fallback — all without invoking FFmpeg (they assert on the generated command/graph text). `tests/test_exporter.py` covers `core/exporter.py`'s own pure argument/file-construction logic the same way (chapters-file content, `-map_metadata` indexing, and — via a mocked `subprocess.Popen` that spawns nothing real — that pass 1's filter graph contains `anullsink`); this is a deliberate, narrow exception to the "no unit tests for `core/exporter.py`" rule below, for logic that doesn't actually require executing FFmpeg to verify. There is no automated test that runs FFmpeg end-to-end otherwise; every format (mp4/mov/mkv/avi/flv/webm/gif) was verified manually with a real FFmpeg install against a real imported media file, including three bugs real execution caught that text-only tests couldn't: `xfade` silently requiring constant-frame-rate inputs (see [color-and-effects/workflow.md](../color-and-effects/workflow.md)), GIF export failing with "output unconnected" until the base graph's always-present `[aout]` pad was routed into `anullsink`, and — found later, the same way — two-pass export's pass 1 having exactly the same unconnected-`[aout]` bug this whole time, for every project, not just ones with markers.

## Codecs, presets and colour (P2)

- **Video Codec** (`ExportSettings.video_codec`, `core.exporter.resolve_codecs`): container default, H.264, HEVC (`libx265` / `hevc_nvenc`), AV1 (`libaom-av1` / `av1_nvenc`), ProRes 422 HQ (`prores_ks`, MOV/MKV, PCM audio) and DNxHR HQ (`dnxhd`, MOV/MKV, PCM audio). Invalid container/codec pairs are refused. Each NVENC encoder is probed separately (`nvenc_encoder_available`).
- **NVENC quality flags**: p4 + `-tune hq -multipass qres -spatial-aq 1 -temporal-aq 1 -rc-lookahead 20 -b_ref_mode middle`; measured 1.03-1.24x the time of plain p4 on an RTX 5080 (p5 with full-resolution multipass was 1.12-1.36x and was not adopted).
- **Target size**: CPU two-pass needs H.264, VP9 or AV1. With **Target size on the GPU**, one NVENC pass with `-b:v` / `-maxrate 1.5x` / `-bufsize 2x` lands close to the size; on a GPU-specific failure it falls back to CPU two-pass.
- **Colour**: HDR (PQ/HLG) sources are tone-mapped to SDR BT.709 (`zscale` with explicit input transfer/matrix/primaries, `tonemap=hable`), BT.601 sources get `in_color_matrix=bt601:out_color_matrix=bt709`, and the final frames are tagged with `setparams=...bt709...:range=tv` (the `-colorspace`/`-color_trc` output options, also passed, were not enough on their own in FFmpeg 9).
- **Dialog**: presets (built-in + `~/.video_editor/export_presets.json`), last settings remembered in QSettings, resolution/fps defaulted from the dominant source, exact NTSC rates (`fps_expr` writes `30000/1001`), speed/time-left in the progress text, non-modal, renders a deep-copied project snapshot, deleted on close; Esc/Cancel/close cancel a running render. Probe failures during planning are listed as warnings after the export.
