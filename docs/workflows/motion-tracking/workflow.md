# Motion Tracking Workflow

## Goal

Make a clip (a logo, a sticker, a blur patch, a picture-in-picture) move with an object in the video underneath it, without keyframing it by hand.

## Steps

1. Put the video to track on a video track, and the clip that should follow it (a video or an image) on a track above.
2. Move the playhead to a frame where the object is clearly visible, and select the clip that should follow.
3. Choose **Edit > Track Motion...**. The dialog shows that frame of the video clip under the playhead on the nearest track below the selected clip.
4. Drag a box around the object. Pick something with clear detail, not a flat colour; a little background inside the box is fine.
5. Leave **Keep the clip's current offset from the object** on to keep the clip where you placed it relative to the object (a label beside a face stays beside it), or turn it off to centre the clip on the object.
6. Click **Track**. When it finishes, the selected clip has position keyframes that follow the object. It is one undo step, and the keyframes appear in the clip's Keyframes table, where they can be edited like any others.

## How it works

`core/motion_track.py` uses FFmpeg's own `find_rect` filter, so it needs no new dependency.

- **Template.** The box is cut out of the frame at the playhead as a small grey PGM (`template_args()`), from the same 480 px-wide scaling the tracking pass uses.
- **Tracking.** `find_rect` searches each frame of a 480 px-wide, 15 fps decode of the tracked clip's trimmed source range (`track_args()`). For each frame, `metadata=print` reports the best match's box and score (0 = exact). Frames with a score above 0.3 count as lost and are skipped, so the keyframes interpolate across them. The pass runs on a background thread with a cancellable progress dialog, like Detect Scene Cuts. A 3 s clip takes a fraction of a second.
- **Onto the canvas.** Each match's centre, as a fraction of the tracked frame, goes through the tracked clip's fit (letterbox/pillarbox) and Transform position (static or keyframed) and scale (`frame_to_canvas()`).
- **Onto the follower's time.** Each match is converted to the follower's clip-local time through the tracked clip's speed, speed ramp, reverse and freeze frame (`core.scene_detect.timeline_offset_for_source()`). Only the stretch while the follower is on screen is kept.
- **Thinning.** Keyframes that lie on a straight line between their neighbours (within 0.2 % of the canvas) are dropped (`follow_keyframes()`). A clip moving in a straight line gets just two keyframes.

## Limits

- `find_rect` matches the template as it was drawn. An object that turns, changes size or is covered is lost while it does; those frames are skipped.
- The tracked clip can't be in Fill Frame mode or rotated (the command says so). The follower must be a video or image clip, because text clips have no position keyframes.
- Only the position is tracked, not scale or rotation.
- To blur a moving face, use an effect mask's **Track Mask...** (the mask's centre follows the object) or animate the mask with **Keyframe Here** (see the User Guide's "Tracked masks" and "Animated masks").

## Verification

`tests/integration/test_motion_track.py`:

- The log parsing, template box clamping, fit/Transform mapping and follower timing/offset/thinning.
- `find_rect` with the real FFmpeg on a generated clip of a square moving 150 px/s right and 30 px/s down: every tracked centre lies within 4 px of the true path on the 640×360 frame.
- The whole Edit > Track Motion flow in the main window: the follower's keyframes lie within 5 px of the true path, Undo removes them, and the box-picker dialog works.

## Keep Subject in Frame (2026-10-21, F35)

Edit > Keep Subject in Frame... tracks a subject in a video clip whose shape differs from the project's (e.g. 16:9 footage in a 9:16 project) and, instead of moving another clip, animates the clip's own **Fill Frame** crop focus (`Clip.crop_focus_keyframes`, `motion_track.reframe_keyframes()`): the focus centres the subject, clamped so the crop never leaves the frame. The export's Fill crop becomes a per-frame expression of those keyframes. One undo step; the keyframes are saved with the project and rebased when the clip is trimmed or split.

Verified in `tests/test_competitor_features.py`: `ReframeTests` (focus clamping, a real render whose crop follows the keyframes, saving and splitting) and `WindowTests.test_auto_color_and_reframe_are_single_undo_steps`.
