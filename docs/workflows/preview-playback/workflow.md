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

All of the above **are** rendered correctly in the exported file — export always builds the full multi-track FFmpeg filter graph (see [export/workflow.md](../export/workflow.md)). This is a deliberate trade-off for a simple, fast, maintainable preview implementation rather than a custom frame compositor; the panel shows a permanent on-screen reminder of this ("Preview shows the top active video clip only...").

## Playback across gaps and speed

- Playing through a gap with no active clip advances by wall-clock time (`_on_poll`, at `POLL_INTERVAL_MS = 40ms`) until a clip starts.
- The timeline<->source mapping goes through the clip's speed segments (`Clip.source_offset_at()`/`timeline_offset_at()`/`speed_at()`), and the playback rate follows the segment under the playhead, so speed-ramped clips no longer drift (it used the constant `speed` only).
- Property edits don't reseek: `PreviewPlayer.refresh_after_edit()` reseeks only when the clip under the playhead changed in a way this preview shows (source, trim, timing, speed); volume/mute are applied in place. Every Properties slider tick used to call `QMediaPlayer.setPosition()`.
- Keyboard transport (**Playback** menu): J/K/L shuttle (repeat J or L for 2x/4x; reverse is stepped by seeking, since `QMediaPlayer` can't play backwards). The time label shows `HH:MM:SS:FF` at the project frame rate.
- Not done yet: preloading the next clip's file in a second player, so a cut between two different files still pauses briefly while `QMediaPlayer` opens the next one.

## Validation

There is no automated test for `PreviewPlayer` (it requires `QMediaPlayer`/a live event loop and, for real playback, actual media files with a working backend). Verify manually: play across a gap between clips, scrub during playback, and confirm a video clip on a higher track correctly takes over from one below it as the playhead crosses into its span.

## Accurate frame, preloading and proxies (P2)

- **Accurate frame** (checkbox): while paused or scrubbing, `core/frame_render.py` builds the export graph for a one-frame window (`trim_range=(t, t+1/fps)`, `video_only=True`) at preview size and pipes one PNG back; it runs on a worker thread, debounced by 150 ms, and a generation counter drops results superseded by a newer request.
- **Next-clip preloading**: within 1.5 s of a cut to a different file, a second `QMediaPlayer` opens the next file; at the cut the two players (and their video widgets) swap instead of calling `setSource`.
- **Proxies**: when an asset has a proxy (`MediaAsset.proxy_path`, see `core/proxy.py`), the preview plays it; export never does.

## Stabilization in Accurate frame (round 3)

- The Accurate-frame renderer builds the export graph with `stabilize_cached_only=True`: a stabilized clip shows stabilized only after an export has cached its motion analysis (`~/.video_editor/stabilize`), so scrubbing never waits for a full analysis pass.
