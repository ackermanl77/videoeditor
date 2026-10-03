# Clip Properties Workflow

## Goal

Adjust a selected clip's volume, mute state, playback speed, opacity, and fade in/out from the Properties panel.

## Steps

1. Select a clip on the timeline (click it, or double-click).
2. The Properties panel (`ui/properties_panel.py`) shows the relevant controls: audio/video clips get Volume/Mute/Speed/Opacity/Fade In/Fade Out; video/image clips additionally get the Transform, Reframe (Fill Frame + Crop Focus X/Y — see [shorts-export/workflow.md](../shorts-export/workflow.md)), Keyframes (Opacity/Position/Speed Ramp — see [color-and-effects/workflow.md](../color-and-effects/workflow.md) and [speed-ramps/workflow.md](../speed-ramps/workflow.md)), Transition, Color Correction, and Chroma Key groups; text clips get the text-styling controls instead (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
3. Every group for a video/image clip is a `CollapsibleGroupBox` (click its title, or the checkbox next to it, to expand/collapse — the "▼"/"▶" prefix tracks state) — Clip Adjustments, Transform, and Color Correction start expanded (the most commonly touched groups); Reframe, Keyframes, Transition, and Chroma Key start collapsed, since all seven fully expanded need ~1,300px of vertical space against a panel that typically has ~500px to work with. Collapsing hides a group's controls entirely (not just disables them, unlike `QGroupBox`'s own native checkable behavior) — a collapsed control still holds and applies its last value, it just isn't shown; toggling Fill Frame or Chroma Key's own internal "Enable" checkbox works identically whether their outer group is expanded or collapsed.
4. Move a slider (Volume, Opacity, Brightness/Contrast/Saturation/Gamma, Chroma Key Similarity/Softness, Transform, Crop Focus, Denoise Strength) by any means -- mouse drag, click on the groove, mouse wheel, arrow/PageUp keys. Every change executes a *mergeable* `UpdateClipPropertyCommand`; `History` merges consecutive edits of the same clip property within 0.7 s (or until the slider is released) into one undo step that restores the value from before the gesture. Previously only a real mouse drag was recorded: keyboard, wheel and groove edits changed the clip with no undo step at all.
5. Change Speed/Fade In/Fade Out/Mute/Crossfade/Transition Duration/Chroma Key Enable — these commit immediately (spin box `editingFinished`, checkbox `toggled`). A Speed change or speed-ramp keyframe that would make the clip run into the next clip on its track is refused with a status-bar message (`core/commands.py::property_change_overlaps()`); it used to silently create overlapping clips.
6. The timeline item updates right away after every committed property (width after a speed change, effect badges) via `PropertiesPanel.clip_changed` -> `TimelineWidget.refresh_clip()`.

## Ranges

| Property | Range | Notes |
|---|---|---|
| Volume | 0-200% | Values above 100% amplify via FFmpeg's `volume` filter at export time. |
| Speed | 0.25x-4.0x | Changes both playback rate and how much of the trimmed source region maps to timeline duration (`Clip.duration = source_length / speed`) — unless 2+ Speed Ramp keyframes are set, in which case this field is overridden; see [speed-ramps/workflow.md](../speed-ramps/workflow.md). |
| Opacity | 0-100% | Applied via `colorchannelmixer=aa=...` at export (or a `geq` alpha ramp with 2+ Opacity keyframes — see [color-and-effects/workflow.md](../color-and-effects/workflow.md)); not reflected in the preview. |
| Fade In/Out | 0-30s | Applied as an alpha fade (`fade=...:alpha=1`) at export, independent of opacity; also applied to the clip's own audio (`afade`) if it has any. |

## Undo/redo

Every committed change goes through `UpdateClipPropertyCommand` (or `UpdateTextClipCommand` for text clips), so each is individually undoable.

## Validation

`tests/test_clip_and_track.py::ClipDurationTests` covers how speed affects `Clip.duration`. `tests/test_commands_and_history.py::MoveAndPropertyCommandTests::test_update_property_undo` covers the generic property-command undo path. Slider live-preview-vs-commit behavior and the text/clip panel swap require a live Qt event loop to verify — no automated coverage exists for the panel itself, but `CollapsibleGroupBox`'s own behavior was verified with a headless (`QT_QPA_PLATFORM=offscreen`) script: selecting a video clip and measuring the scroll content's `sizeHint()` directly (2048x1376 with every group forced expanded, down to 426x717 with the actual defaults — a real, measured ~48% height reduction, not an estimate), toggling every group off/on and confirming its direct children's `isVisible()` actually follows (`QGroupBox.setCheckable()` alone would only disable them, not hide them — the exact bug this class exists to avoid), and confirming Fill Frame/Chroma Key's own internal checkboxes still update `Clip.reframe_mode`/`chroma_key_enabled` correctly while their outer group is collapsed. Caught one real bug during that verification: the initial expand/collapse state was applied in `CollapsibleGroupBox.__init__`, before `PropertiesPanel._build_ui()` had added any of that group's rows yet — hiding "the current children" did nothing, since none existed. Fixed by re-applying each group's state once at the end of `_build_ui()`, after every row across every group has actually been added.

## Reverse and freeze frame (round 4)

- **Model.** `Clip.reverse` (bool) and `Clip.freeze_frame_duration` (seconds, >= 0), both saved in the project (absent = off; no format bump). `Clip.duration` includes the freeze; `Clip.playable_duration` excludes it. Playback order goes through `Clip.playback_source_consumed()`/`playback_timeline_offset()` (speed segments walked back to front when reversed) and `Clip.source_time_at()` (the source frame on screen, reverse/freeze aware, clamped to the trim). `source_offset_at()` keeps its forward meaning and only clamps when a freeze exists.
- **Panel.** "Reverse playback" (video and audio clips) and "Freeze Frame" (video clips) in Clip Adjustments; each change is one undo step. A freeze that would overlap the next clip is refused like a speed change (`freeze_frame_duration` is in `commands.DURATION_ATTRIBUTES`) and the spin box shows the real value again. Key Speed at Playhead uses the frame on screen (`source_time_at`), so it is right on reversed clips.
- **Render.** Reverse = `reverse` after the trim (and `areverse` on the clip's audio), applied per segment with the segments in reverse order on a speed ramp. Freeze = `tpad=stop_mode=clone` placed *before* the fades/opacity so a Fade Out reaches into the held frame; the audio simply ends (silence), and its fade-out ends with the sound (`afade` relative to `playable_duration`). A trimmed export / preview window seeks a reversed clip's mirrored source range; a window starting inside the freeze reads just the last frame and is trimmed afterwards (`filter_graph._clip_source_slice`).
- **Limits.** FFmpeg's `reverse` buffers every frame of the clip in memory: keep reversed clips short. The preview steps through reversed/frozen clips silently (see preview-playback). Splitting inside a freeze is refused.
- **Validation.** `tests/test_p4_core.py` (mapping, edits, graph, files), `tests/test_p4_ui.py` (panel, badges, drags), `tests/integration/test_p4_real_ffmpeg.py` (brightness ramp and sound measured on the exported file: direction, freeze hold and silence, fade over the freeze, trimmed window and preview frame equal to the full export).
