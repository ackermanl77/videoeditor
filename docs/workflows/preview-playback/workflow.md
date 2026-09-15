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
- A clip's `speed` multiplier drives both `QMediaPlayer.setPlaybackRate()` and the timeline-time-to-source-time mapping used while polling, so scrubbing and playback stay in sync with a sped-up/slowed-down clip.

## Validation

There is no automated test for `PreviewPlayer` (it requires `QMediaPlayer`/a live event loop and, for real playback, actual media files with a working backend). Verify manually: play across a gap between clips, scrub during playback, and confirm a video clip on a higher track correctly takes over from one below it as the playhead crosses into its span.
