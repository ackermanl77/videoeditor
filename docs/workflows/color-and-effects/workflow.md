# Transform, Keyframes, Color Correction, Chroma Key, and Transitions Workflow

## Goal

Position/scale/rotate a video/image clip (optionally animated over time), grade its color, key out a solid-color background, and/or crossfade it into the next clip on its track — all applied at export time via FFmpeg; none of it is visible during preview playback, only in the paused **Accurate frame** view (rendered by the export engine) and the exported file.

## Transform (position, scale, rotation)

1. Select a video or image clip.
2. In the Properties panel's **Transform** group, drag **Position X**/**Position Y** to move the clip, **Scale** to resize it, and **Rotation** to rotate it. **Reset Transform** restores all four to their defaults in one click.
3. The clip is first fit inside the canvas preserving aspect ratio (as always), then this additional scale/rotation is applied, and the result is positioned by its *center* at (Position X, Position Y) via FFmpeg's `overlay=x:y` — replacing the old unconditional `pad`-to-canvas step. At the defaults (X/Y 0.5, Scale 1.0, Rotation 0°) this evaluates to exactly the same centered, full-canvas result as before Transform existed — an unedited clip's exported position is unchanged.

| Control | Range | Default | Notes |
|---|---|---|---|
| Position X / Y | 0.0 .. 1.0 | 0.5, 0.5 | 0 = left/top edge, 1 = right/bottom edge; positions the clip's *center*. |
| Scale | 0.1 .. 3.0 | 1.0 | Multiplies the already-fit-to-canvas size — 0.5 halves it, 2.0 doubles it (can exceed canvas bounds; `overlay` naturally crops anything outside the frame, so zooming in and cropping "off-screen" both work as expected). |
| Rotation | -180 .. 180° | 0.0 | Via FFmpeg's `rotate` filter; the output bounding box (`ow=rotw(...)`, `oh=roth(...)`) expands to fit the rotated frame, filled transparent (`c=none`) rather than cropped. |

This is what makes picture-in-picture, split-screen, and off-center framing possible — put a second video/image clip on a track above another, scale it down, and position it in a corner.

**Known limit: Transform is a no-op on a clip while it's crossfading into the next one.** `xfade` (used for crossfade transitions, below) requires both its inputs to be exactly the same frame size; Transform's overlay-based positioning deliberately allows a clip's frame to be smaller or larger than the canvas, which the two sides of a crossfade pair can't reconcile against each other. A crossfading clip's chain falls back to the old fixed-size, centered `pad` step instead — documented, not silent: `core/filter_graph.py::_build_video_clip_chain`'s `apply_transform` parameter is `False` for both sides of a crossfade pair.

## Keyframing (Opacity and Position)

Both Opacity (Clip Adjustments group) and Transform's Position X/Y can be animated over time instead of held constant:

1. Select a video or image clip and set the property's slider (Opacity, or Transform Position X/Y) to the value you want at a specific moment.
2. In the Properties panel's **Keyframes** group, set **At** to that moment's time in seconds *from the clip's own start*, then click **+ Add** — it captures the slider's current value at that time. Repeat for other moments; the table lists every keyframe, and **- Remove Selected** deletes the highlighted row.
3. With 2 or more keyframes, the plain slider is overridden: the value smoothly (linearly) interpolates between keyframes, holding the first/last value outside the keyframed range.

Implementation differs by property, because FFmpeg's per-frame expression support isn't uniform across filters:
- **Position** reuses the exact same `overlay=x:y` mechanism Transform already uses (see above) — the x/y expressions become piecewise-linear functions of `t` instead of plain numbers. Since `overlay` runs *after* each layer's `tpad` shift to its timeline position, the expression subtracts `timeline_start` to convert back to clip-local time before evaluating.
- **Opacity** can't reuse `colorchannelmixer` (its options are plain floats, not per-frame expressions in this FFmpeg build) — animated opacity instead uses `geq`, re-sampling `lum`/`cb`/`cr` from the source frame unchanged and only recomputing alpha as `alpha(X,Y) * (piecewise-linear expression of T)`, preserving whatever transparency fades/rotate already introduced.

Both were verified against real FFmpeg (a `geq` alpha ramp and an `overlay` position ramp, each sampled at multiple timestamps and confirmed to match the predicted linear-interpolation math almost exactly), not just the text-based unit tests — `geq`'s exact syntax in particular wasn't something this codebase had used before, so it was worth confirming end-to-end before relying on it.

**Known limit**: like Transform's scale/rotation, position keyframes are a no-op on a clip while it's crossfading (same `xfade`-needs-matching-sizes reason).

## Color correction

1. Select a video or image clip.
2. In the Properties panel's **Color Correction** group, adjust Brightness, Contrast, Saturation, and Gamma.
3. Exported via FFmpeg's `eq` filter. Since project format 3 the sliders edit the clip's first `color_correction` effect (`core/effects.py`; created by the first non-neutral edit, inserted after any RGB key so the old order is kept). The filter is only emitted when at least one value differs from neutral (brightness 0.0, contrast/saturation/gamma 1.0) or is animated, so an unedited clip's generated command is unchanged.

| Control | Range | Neutral |
|---|---|---|
| Brightness | -1.0 .. 1.0 | 0.0 |
| Contrast | 0.0 .. 2.0 | 1.0 |
| Saturation | 0.0 .. 3.0 | 1.0 |
| Gamma | 0.1 .. 3.0 | 1.0 |

## Chroma key (green screen)

1. Select a video or image clip filmed against a solid-color background.
2. In the Properties panel's **Chroma Key** group, check **Enable chroma key**, pick the **Color** to key out (defaults to pure green), and tune **Similarity** (how close a pixel's color must be to count as a match) and **Softness** (edge feathering).
3. Exported via FFmpeg's `colorkey` filter (the stack's first `colorkey` effect since format 3; picking a colour before ticking Enable creates it switched off), applied after `format=yuva420p` and before color correction — keyed-out pixels become transparent through the same alpha channel opacity/fades already use, so the result composites naturally with whatever track is beneath it via the existing `overlay` stack. No structural change to the compositing pipeline was needed for this.

## Crossfade transitions

1. Select a clip that has another video/image clip immediately following it on the *same track*, with no gap between them (clips can't overlap — see [timeline-editing/workflow.md](../timeline-editing/workflow.md)).
2. In the Properties panel's **Transition** group, pick a **Transition Out** (Crossfade, Wipe Left/Right/Up/Down, Slide Left/Right/Up/Down, Circle Crop or Dissolve; each maps to an `xfade` transition via `_XFADE_TRANSITION_NAMES`) and set a **Duration** (0.1–10 s).
3. At export, `core/filter_graph.py::_find_crossfade_partner()` detects the pair and merges both clips' chains with FFmpeg's `xfade` filter into a single composited layer, instead of two independent ones.

**Known scope limits** (deliberate, to keep this contained):
- **One pair per boundary.** If clip B also has Crossfade enabled into clip C, that second transition is not applied — chaining three or more consecutive crossfades on one track isn't supported yet.
- **Audio follows the video.** `xfade` starts the second clip's picture `transition_duration` (d) seconds before its own `timeline_start` (at `A.end - d`), so its audio now starts there too, and the two sides' audio crossfade over the same d-second overlap (A's audio fades out, B's fades in, replacing each side's own Fade In/Out there, like the video side). Previously B's audio stayed at `B.timeline_start` and lagged its own picture by d for the whole clip (measured with real FFmpeg: tone onset 4.003 s vs. picture onset 3.500 s for d = 0.5; now 3.503 s vs. 3.500 s). The pair still ends d seconds earlier than the timeline shows (see the next point) -- audio and video now agree on that.
- **The transition duration isn't "refunded" elsewhere.** A crossfade shortens that pair's combined on-screen time by the transition duration, but nothing downstream on that track shifts left to compensate — every other clip's `timeline_start` is untouched (no ripple, on this track or any other, and no silent cross-track desync). The practical effect: there's a small gap (transition duration long) between the crossfaded pair and whatever clip comes right after it on that track, unless you manually drag that next clip left to close it. This trade-off was chosen deliberately over either (a) auto-rippling that track (which would risk desyncing other tracks that assume absolute timeline positions) or (b) requiring genuinely overlapping clip placement (which would mean relaxing the non-overlap invariant `Track.has_overlap()` enforces everywhere else).
- The requested duration is clamped to whatever both clips can actually provide (`min(transition_duration, clip.duration, next.duration)`).

## Validation

`tests/test_filter_graph.py` covers all five features without invoking FFmpeg (`TransformTests`, `KeyframeTests`, `ColorCorrectionTests`, `ChromaKeyTests`, `CrossfadeTransitionTests`, plus `AudioFadeTests` for the related audio-fade fix): the `eq`/`colorkey` filters appear only when non-neutral/enabled, a default-transform clip's overlay expression evaluates to the same 0:0 position as before Transform existed, a single keyframe behaves like no keyframes at all, 2+ keyframes produce the expected piecewise-linear expression text, a valid adjacent crossfade pair produces one merged `xfade` layer instead of two `overlay` layers (and correctly falls back to the old `pad` step, ignoring Transform/position-keyframes, while doing so), a gap or a text-clip partner correctly disables crossfading, and the duration is clamped to the shorter clip.

Unlike the other export-only features, Transform was also verified against **real FFmpeg execution**, not just these text-based tests — a picture-in-picture composite (a small, repositioned clip on top of a full-frame background clip) confirmed the background is visible everywhere the foreground doesn't cover (proving `overlay` positioning, not the old `pad`, is what's compositing it), plus default-transform-reproduces-old-behavior, rotation, and transform+crossfade-together checks. This matters because two earlier features in this same file (crossfade's `fps=` requirement, GIF export's unconnected-audio-pad error) were bugs that only real FFmpeg execution caught — the text-only tests structurally can't catch a rejected filtergraph.

## Effect stack, HDR and audio automation (P2)

- `Clip.effects` is an ordered list of `EffectInstance(kind, params, enabled)` from the registry in `core/effects.py` (LUT `.cube`, YUV chroma key + despill, sharpen, Gaussian blur, vignette, video denoise). Each entry declares its parameter schema; the Properties panel's Effects group builds its form from it, and adding an effect kind is one registry entry. `hqdn3d`/`vignette` have no alpha-capable pixel format, so they run before the clip gains alpha; the rest run after keying. Unknown kinds from newer files are kept, not rendered. Since format 3 the older Color Correction/Chroma Key fields are effects too (see below).
- HDR sources are tone-mapped to SDR BT.709 at export (see export/workflow.md).
- Volume keyframes (`Clip.volume_keyframes`) render as `volume=eval=frame` envelopes; audio clips that touch with a transition crossfade with `qsin` curves (video crossfade audio uses them too). Ducking threshold/ratio/attack/release are set in the track's ducking dialog.
- **Key at playhead** (the diamond buttons) adds keyframes at the playhead; keyframes are drawn on clips and Shift+K / Ctrl+Shift+K navigate them.

## Stabilization, migrated colour fields and animatable parameters (round 3)

- **Colour/key migration (format 3).** `brightness`/`contrast`/`saturation`/`gamma` and `chroma_key_*` are no longer stored on clips. `core.project._migrate_2_to_3` turns them into `colorkey` and `color_correction` effects at the front of the stack (colorkey, then eq, then the rest -- the old fixed order), so the filter text is identical. `Clip(brightness=...)`, `clip.brightness` and `UpdateClipPropertyCommand(..., "brightness", ...)` keep working: they are compatibility attributes over the first effect of that kind (`core/clip.py::_legacy_property`). The C/K badges read those effects; FX counts every other renderable effect.
- **Stabilize (vid.stab).** Stage `"source"`: `vidstabtransform` runs on the decoded frames right after the input's `trim`, before speed changes, colour conversion and scaling, because its motion data (`vidstabdetect`) is per source frame. `build_filter_graph` records a `core.stabilize.StabilizeJob` per stabilized clip (or per speed-ramp segment: each has its own input); `Exporter._run_stabilize_analyses` runs the missing ones first with the same `-ss`/`-t`/`trim` as the export input, into `<hash>.trf.partial`, renamed when complete. The cache key is the file (path, size, mtime), the range and shakiness/accuracy, so smoothing/zoom/border changes reuse it. Stabilized clips are never seeked into (`_needs_full_decode`): a trimmed export decodes them from the clip start and trims after. Frame previews pass `stabilize_cached_only=True`. If an FFmpeg build lacks vid.stab, the self-test lists the missing filters (there is no `deshake` fallback).
- **Animatable parameters.** `ParamSpec(animatable=True)` marks parameters the filter evaluates per frame: the four `color_correction` values (`eq ...:eval=frame`) and the vignette angle (`vignette=angle='PI*(expr)/180':eval=frame`). `EffectInstance.keyframes = {param: [(clip-local s, value), ...]}`; 2+ keys become `_piecewise_linear_expr` over `t`. Every effect stage runs on clip-local time; speed-ramp segments pass `t_offset` (the segment's start in clip-local time). Keys are clamped to the schema, re-based by left trims (`Clip.with_source_range`) and split (`SplitClipCommand`), included in keyframe ticks and Shift+K/Ctrl+Shift+K, and saved as `"keyframes"` in the effect dict. UI: the ◆ / key count / Clear row next to each animatable parameter in the Effects form (`PropertiesPanel.key_effect_param_at_playhead`).
