# Clip Properties Workflow

## Goal

Adjust a selected clip's volume, mute state, playback speed, opacity, and fade in/out from the Properties panel.

## Steps

1. Select a clip on the timeline (click it, or double-click).
2. The Properties panel (`ui/properties_panel.py`) shows the relevant controls: audio/video clips get Volume/Mute/Speed/Opacity/Fade In/Fade Out; video/image clips additionally get the Transform, Reframe (Fill Frame + Crop Focus X/Y — see [shorts-export/workflow.md](../shorts-export/workflow.md)), Keyframes (Opacity/Position/Speed Ramp — see [color-and-effects/workflow.md](../color-and-effects/workflow.md) and [speed-ramps/workflow.md](../speed-ramps/workflow.md)), Transition, Color Correction, and Chroma Key groups; text clips get the text-styling controls instead (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
3. Every group for a video/image clip is a `CollapsibleGroupBox` (click its title, or the checkbox next to it, to expand/collapse — the "▼"/"▶" prefix tracks state) — Clip Adjustments, Transform, and Color Correction start expanded (the most commonly touched groups); Reframe, Keyframes, Transition, and Chroma Key start collapsed, since all seven fully expanded need ~1,300px of vertical space against a panel that typically has ~500px to work with. Collapsing hides a group's controls entirely (not just disables them, unlike `QGroupBox`'s own native checkable behavior) — a collapsed control still holds and applies its last value, it just isn't shown; toggling Fill Frame or Chroma Key's own internal "Enable" checkbox works identically whether their outer group is expanded or collapsed.
4. Drag a slider (Volume, Opacity, Brightness/Contrast/Saturation/Gamma, Chroma Key Similarity/Softness) — the change previews live on the clip immediately; releasing the slider commits one undo step for the whole drag, not one per intermediate value.
5. Change Speed/Fade In/Fade Out/Mute/Crossfade/Transition Duration/Chroma Key Enable — these commit immediately (spin box `editingFinished`, checkbox `toggled`).

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
