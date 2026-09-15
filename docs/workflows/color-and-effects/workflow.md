# Color Correction, Chroma Key, and Transitions Workflow

## Goal

Grade a video/image clip's color, key out a solid-color background, and/or crossfade it into the next clip on its track — all applied at export time via FFmpeg, none of it visible in the live preview.

## Color correction

1. Select a video or image clip.
2. In the Properties panel's **Color Correction** group, adjust Brightness, Contrast, Saturation, and Gamma.
3. Exported via FFmpeg's `eq` filter (`core/filter_graph.py`), inserted right after the clip reaches `format=yuva420p` and before chroma key/fades/opacity. The filter is only emitted when at least one value differs from neutral (brightness 0.0, contrast/saturation/gamma 1.0), so an unedited clip's generated command is unchanged.

| Control | Range | Neutral |
|---|---|---|
| Brightness | -1.0 .. 1.0 | 0.0 |
| Contrast | 0.0 .. 2.0 | 1.0 |
| Saturation | 0.0 .. 3.0 | 1.0 |
| Gamma | 0.1 .. 3.0 | 1.0 |

## Chroma key (green screen)

1. Select a video or image clip filmed against a solid-color background.
2. In the Properties panel's **Chroma Key** group, check **Enable chroma key**, pick the **Color** to key out (defaults to pure green), and tune **Similarity** (how close a pixel's color must be to count as a match) and **Softness** (edge feathering).
3. Exported via FFmpeg's `colorkey` filter, applied right after `format=yuva420p` (before color correction) — keyed-out pixels become transparent through the same alpha channel opacity/fades already use, so the result composites naturally with whatever track is beneath it via the existing `overlay` stack. No structural change to the compositing pipeline was needed for this.

## Crossfade transitions

1. Select a clip that has another video/image clip immediately following it on the *same track*, with no gap between them (clips can't overlap — see [timeline-editing/workflow.md](../timeline-editing/workflow.md)).
2. In the Properties panel's **Transition** group, check **Crossfade into next clip** and set a **Duration**.
3. At export, `core/filter_graph.py::_find_crossfade_partner()` detects the pair and merges both clips' chains with FFmpeg's `xfade` filter into a single composited layer, instead of two independent ones.

**Known scope limits** (deliberate, to keep this contained):
- **One pair per boundary.** If clip B also has Crossfade enabled into clip C, that second transition is not applied — chaining three or more consecutive crossfades on one track isn't supported yet.
- **Video only.** Each clip's audio stays completely independent of the video crossfade — no `acrossfade` — so the audio cuts cleanly at the original boundary while the video blends. This avoids a duration-mismatch problem: shortening the *audio* along with the video would require re-deriving every downstream clip's timeline position (see the next point), which is out of scope.
- **The transition duration isn't "refunded" elsewhere.** A crossfade shortens that pair's combined on-screen time by the transition duration, but nothing downstream on that track shifts left to compensate — every other clip's `timeline_start` is untouched (no ripple, on this track or any other, and no silent cross-track desync). The practical effect: there's a small gap (transition duration long) between the crossfaded pair and whatever clip comes right after it on that track, unless you manually drag that next clip left to close it. This trade-off was chosen deliberately over either (a) auto-rippling that track (which would risk desyncing other tracks that assume absolute timeline positions) or (b) requiring genuinely overlapping clip placement (which would mean relaxing the non-overlap invariant `Track.has_overlap()` enforces everywhere else).
- The requested duration is clamped to whatever both clips can actually provide (`min(transition_duration, clip.duration, next.duration)`).

## Validation

`tests/test_filter_graph.py` covers all three features without invoking FFmpeg (`ColorCorrectionTests`, `ChromaKeyTests`, `CrossfadeTransitionTests`, plus `AudioFadeTests` for the related audio-fade fix): the `eq`/`colorkey` filters appear only when non-neutral/enabled, a valid adjacent pair produces one merged `xfade` layer instead of two `overlay` layers, a gap or a text-clip partner correctly disables crossfading, and the duration is clamped to the shorter clip. There is no automated test that renders real pixels through FFmpeg — verify visually by exporting a short project exercising each feature (a graded clip, a green-screen clip over a background track, and two adjacent clips crossfading).
