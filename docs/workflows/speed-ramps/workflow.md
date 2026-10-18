# Speed Ramps Workflow

## Goal

Vary a video/image clip's playback speed within itself — e.g. play normally, then slow-motion, then normal again — rather than one constant speed for the whole clip.

## Steps

1. Select a video or image clip.
2. Set the **Speed** field (Clip Adjustments group) to the speed you want at a given moment.
3. In the Properties panel's **Keyframes** group's Speed Ramp section, set **At** to that moment's time in seconds *from the clip's source start* (not its timeline position — see "Why source time" below), then click **+ Add**. Repeat for each speed change.
4. With 2 or more speed keyframes, the plain **Speed** field is overridden. **- Remove Selected** deletes the highlighted row; dropping back to 0 or 1 keyframes returns to the plain constant-speed behavior.

## Step segments, not a smooth ramp — and why

A keyframe `(time, speed)` sets the speed from that source time forward, until the next keyframe — a **step change** at each boundary, not a continuously accelerating/decelerating curve. This was a deliberate scope call, not a shortcut taken lightly:

FFmpeg has no filter option that natively accepts "an arbitrary time-varying speed multiplier" the way `overlay`'s `x`/`y` or `rotate`'s angle do (which is what makes the smooth Position/Rotation keyframing elsewhere in this app possible). The community-known technique for a *continuous* ramp accumulates output timestamps frame-by-frame via `setpts`'s `PREV_OUTPTS`/`PREV_INPTS` variables — mathematically an Euler integration of the instantaneous speed curve. It's real, but genuinely easy to get subtly wrong (drifting timing that "runs without error" rather than crashing, which is a much harder class of bug to catch than an outright filtergraph rejection), and this codebase's own recent history includes two cases where a *plausible-looking* filter combination only broke under real FFmpeg execution, not text-based review (`xfade`'s constant-frame-rate requirement, and GIF export's unconnected-audio-pad error — see `docs/PROJECT_STATUS.md`).

Instead, each segment between keyframes gets its own `trim` + `setpts` at a genuinely constant speed, and the segments are joined with FFmpeg's `concat` filter — the same well-understood, easy-to-verify building block already used elsewhere in this codebase (for crossfade's `xfade`, segments compose predictably). The trade-off: speed changes are instant at each keyframe rather than eased in/out. For most "speed ramp" use cases (a deliberate normal→slow-mo→normal beat) this reads as an intentional cut in pacing anyway; a slowly-accelerating ramp is the one look this implementation can't produce.

## Why source time, not timeline time

Speed keyframe times are relative to the clip's **source** start (`source_in`), not its position on the timeline. Position/Opacity keyframes use timeline-local time instead — but for speed specifically, timeline-local time is a moving target: it's *defined by* the very speed curve you're trying to specify (a point 3 seconds into playback could be almost anywhere in the source, depending on how fast earlier segments played). Source time has no such circularity — it's the same anchor `source_in`/`source_out` trimming already uses.

## What still works alongside a speed ramp

| Feature | Works? | Notes |
|---|---|---|
| Chroma key | Yes | Pure per-pixel color op, applied identically to each segment. |
| Color correction | Yes | Same — per-segment, size-neutral. |
| Fade in/out, Opacity (constant or keyframed) | Yes | Applied once, to the whole concatenated result — not per segment. |
| Transform scale/rotation | **No** (documented no-op) | `concat` requires every segment to already be the same fixed canvas size; independently scaling/rotating each segment can't guarantee that stays consistent. Mirrors the same trade-off already made for a crossfading clip. |
| Transform/keyframed position | Yes | Resolved separately, at the compositing (`overlay`) step — unaffected by how the layer's own internal segments were built. |
| Crossfade transition | **No** (documented no-op) | A speed-ramped clip doesn't produce the single continuous chain `xfade` needs on both sides; `_find_crossfade_partner()` refuses to pair a clip with 2+ speed keyframes on either side. |
| A standalone AUDIO-track clip's own speed ramp | **Yes** (2026-10-17) | Rendered like a video clip's own audio: one seeked input and one `atempo` chain per speed segment, concatenated (`_maybe_add_speed_ramped_audio()`). Until then it exported at constant `clip.speed` and overran its ramped length (a 1x->2x ramp of a 4 s clip filled 3.0 s on the timeline but exported 4.0 s). A clip in an audio crossfade is still rendered at constant speed. Covered by `tests/integration/test_p4_real_ffmpeg.py::AudioTrackSpeedRampTests`. |

## How the audio stays in sync

A speed-ramped video clip's own audio (if it has any) is segmented the same way: each segment gets its own `atempo` chain (chained, since FFmpeg's `atempo` only accepts 0.5–2.0 per instance — see `_atempo_chain()`) at that segment's speed, and the segments are concatenated with FFmpeg's audio `concat` — reusing the exact same per-segment `-i` registrations the video side already made (one file open serves both streams per segment, same principle a non-ramped clip already uses).

## Validation

`tests/test_clip_and_track.py::ClipDurationTests` covers `Clip.speed_segments`/`duration`: a single keyframe falls back to constant speed, segments correctly extend to cover the full trimmed region (including before the first / after the last keyframe), and `duration` sums each segment's own contribution. `tests/test_filter_graph.py::SpeedRampTests` covers the export side without invoking FFmpeg: a single keyframe produces no `concat`, 2+ keyframes produce the right number of `-i` inputs and a `concat` filter, a speed-ramped clip's audio is segmented and concatenated too, and a speed-ramped clip is correctly refused as a crossfade partner.

Verified against real FFmpeg execution, which caught one real bug text-based tests couldn't: `_maybe_add_speed_ramped_audio()`'s fade/volume/delay suffix was appended with a leading comma directly after a bracket reference (`"[label],afade=..."`), which FFmpeg parses as an empty filter name and refuses to run at all ("No such filter: ''") — fixed by stripping it, and a regression test now asserts no `"],afade"`/`"],fade"` substring ever appears in the generated graph. Separately verified: a two-color source clip (red then blue) ramped 1x→3x produces the exact predicted total duration and shows red/blue at the exact predicted output times; chroma-keying a speed-ramped clip's blue segment correctly reveals the track beneath it.

## Speed ramps on reversed clips (round 4)

A reversed, speed-ramped clip plays its segments in reverse order, each reversed and at its own speed (video `reverse` and audio `areverse` per segment, then `concat`). Speed keyframes stay attached to the source (source-relative times), so on a reversed clip a keyframe's speed applies to the content *after* it in the source -- which is *before* it on the timeline. Splitting a reversed ramp gives each half the keyframes of the source it holds. Covered by `tests/test_p4_core.py` and `tests/integration/test_p4_real_ffmpeg.py::ReverseExportTests::test_reversed_speed_ramp_renders_with_its_duration`.
