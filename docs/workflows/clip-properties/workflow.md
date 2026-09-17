# Clip Properties Workflow

## Goal

Adjust a selected clip's volume, mute state, playback speed, opacity, and fade in/out from the Properties panel.

## Steps

1. Select a clip on the timeline (click it, or double-click).
2. The Properties panel (`ui/properties_panel.py`) shows the relevant controls: audio/video clips get Volume/Mute/Speed/Opacity/Fade In/Fade Out; video/image clips additionally get the Transform, Reframe (Fill Frame + Crop Focus X/Y — see [shorts-export/workflow.md](../shorts-export/workflow.md)), Keyframes (Opacity/Position/Speed Ramp — see [color-and-effects/workflow.md](../color-and-effects/workflow.md) and [speed-ramps/workflow.md](../speed-ramps/workflow.md)), Transition, Color Correction, and Chroma Key groups; text clips get the text-styling controls instead (see [text-overlays/workflow.md](../text-overlays/workflow.md)).
3. Drag a slider (Volume, Opacity, Brightness/Contrast/Saturation/Gamma, Chroma Key Similarity/Softness) — the change previews live on the clip immediately; releasing the slider commits one undo step for the whole drag, not one per intermediate value.
4. Change Speed/Fade In/Fade Out/Mute/Crossfade/Transition Duration/Chroma Key Enable — these commit immediately (spin box `editingFinished`, checkbox `toggled`).

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

`tests/test_clip_and_track.py::ClipDurationTests` covers how speed affects `Clip.duration`. `tests/test_commands_and_history.py::MoveAndPropertyCommandTests::test_update_property_undo` covers the generic property-command undo path. Slider live-preview-vs-commit behavior and the text/clip panel swap require a live Qt event loop to verify — no automated coverage exists for the panel itself.
