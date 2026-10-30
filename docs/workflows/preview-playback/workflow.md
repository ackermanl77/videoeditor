# Preview Playback Workflow

## Goal

Play back the timeline at the current playhead position to check timing and content — with a clearly bounded, intentional gap versus what actually gets exported.

## Steps

1. Press **Space**, click the play button in the toolbar, or click the preview panel's own play button.
2. Click the ruler or drag the playhead to seek; the preview follows.
3. The time label and slider reflect `PreviewPlayer.current_time` against the project's total duration.

## What it does and doesn't show

`PreviewPlayer._resolve_active_clip()` picks **one** clip to show at the current time: the topmost (highest-index) non-hidden video track's clip if one covers that time (video, image, or text), otherwise the first audio track's clip. This is rendered through a single `QMediaPlayer`/`QVideoWidget` (video/audio) or a placeholder label (image/text).

This means the preview does **not**:
- Composite multiple video tracks together (no real-time alpha blending of overlays).
- Show text overlays drawn on top of video underneath them — a text clip either wins as the topmost clip (shown alone) or is invisible if a video clip is on top of it in track order.
- Show fades, transitions, or opacity.

All of the above **are** rendered correctly in the exported file — export always builds the full multi-track FFmpeg filter graph (see [export/workflow.md](../export/workflow.md)). This is a deliberate trade-off for a simple, fast, maintainable preview implementation rather than a custom frame compositor; the panel shows a permanent on-screen reminder of this ("Playback shows the top active video clip only. Turn on View > Accurate frame to see the exact export composite (transitions, text, effects) while paused."). While paused or scrubbing, **Accurate frame** closes the gap (see below).

## Playback across gaps and speed

- Playing through a gap with no active clip advances by wall-clock time (`_on_poll`, at `POLL_INTERVAL_MS = 40ms`) until a clip starts.
- The timeline<->source mapping goes through the clip's speed segments (`Clip.source_offset_at()`/`timeline_offset_at()`/`speed_at()`), and the playback rate follows the segment under the playhead, so speed-ramped clips no longer drift (it used the constant `speed` only).
- Property edits don't reseek: `PreviewPlayer.refresh_after_edit()` reseeks only when the clip under the playhead changed in a way this preview shows (source, trim, timing, speed); volume/mute are applied in place. Every Properties slider tick used to call `QMediaPlayer.setPosition()`.
- Keyboard transport (**Playback** menu): J/K/L shuttle (repeat J or L for 2x/4x; reverse is stepped by seeking, since `QMediaPlayer` can't play backwards). The time label shows `HH:MM:SS:FF` at the project frame rate.
- Cuts between two different files no longer pause: the next file is preloaded in a second player (see "Accurate frame, preloading and proxies" below).

## Validation

Offscreen Qt tests cover the preview's logic without real playback: preloading, proxies and dropping stale Accurate-frame results (`tests/test_p2_ui.py`), and the level meters and stepped playback of reversed/frozen clips (`tests/test_p4_ui.py`); `tests/integration/` checks rendered preview frames against the export with real FFmpeg. Actual `QMediaPlayer` output still needs a manual check: play across a gap between clips, scrub during playback, and confirm a video clip on a higher track correctly takes over from one below it as the playhead crosses into its span.

## Accurate frame, preloading and proxies (P2)

- **Accurate frame** (checkbox): while paused or scrubbing, `core/frame_render.py` builds the export graph for a one-frame window (`trim_range=(t, t+1/fps)`, `video_only=True`) at preview size and pipes one PNG back; it runs on a worker thread, debounced by 150 ms, and a generation counter drops results superseded by a newer request.
- **Next-clip preloading**: within 1.5 s of a cut to a different file, a second `QMediaPlayer` opens the next file; at the cut the two players (and their video widgets) swap instead of calling `setSource`.
- **Proxies**: when an asset has a proxy (`MediaAsset.proxy_path`, see `core/proxy.py`), the preview plays it; export never does.

## Stabilization in Accurate frame (round 3)

- The Accurate-frame renderer builds the export graph with `stabilize_cached_only=True`: a stabilized clip shows stabilized only after an export has cached its motion analysis (`~/.video_editor/stabilize`), so scrubbing never waits for a full analysis pass.

## Reverse/freeze stepping, level meters, safe areas (round 4)

- **Reversed clips and freeze frames** can't be played by `QMediaPlayer` (forwards only), so `PreviewPlayer._is_stepped()` switches them to the same seek-per-poll-tick stepping the reverse shuttle uses: the player stays paused and `_load_for_time()` seeks to `Clip.source_time_at()` every 40 ms of wall-clock time. They are silent in the preview (the export reverses the sound). A forward clip with a freeze plays normally and then holds its last frame until the clip ends. Accurate frame renders them exactly.
- **Meters.** `core/waveform.py` stores an RMS value next to each peak (100 per second; caches written before keep loading, with RMS = peak). `_update_audio_meters()` runs on every poll tick/seek and reads `window_levels()` -- a slice of ~13 cached values around the source time on screen, no decoding -- scaled by the clip's volume. Zero for: no clip, text/image, muted clip or track, freeze frame, no waveform yet (the cache is keyed by the waveform file, so one that arrives after import is picked up). It meters the active clip, not the mix.
- **Safe areas** checkbox: a mouse-transparent overlay drawing the 90 % action-safe and 80 % title-safe rectangles.
- **Accurate frame** now shows a quick half-size render first, then the full one (tooltip says which).
- **Validation.** `tests/test_p4_ui.py::PreviewMeterTests` (levels at the playhead, reverse/mute/freeze, no audio data, stepping without `play()`).
