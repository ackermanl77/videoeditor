# Timeline Editing Workflow

## Goal

Arrange imported media on a multi-track timeline: move, trim, split, and delete clips; add/remove tracks; mute, lock, or hide a track.

## Steps

1. Drag a clip from the Media Bin onto a track, or double-click it to append it to the end of the first compatible track (video clips/images go to a video track, audio-only files go to an audio track).
2. Drag a clip horizontally to move it; drag its left/right edge (the `HANDLE_WIDTH`-wide strip at each end, `ui/widgets/timeline_items.py`) to trim in/out without affecting the rest of the timeline.
3. Position the playhead (click the ruler, or drag it) and press **S** / click **Split** to split the selected clip at the playhead.
4. Press **Delete** / click **Delete** to remove the selected clip.
5. Use **+ Video Track** / **+ Audio Track** to add tracks; select a clip and click **Delete Track** to remove its track.
6. Click a track header's **M** / **L** / **H** buttons to mute, lock, or hide that track. A locked track's clips can't be dragged or trimmed; a hidden video track is skipped entirely by both the preview and the export filter graph.

## Snapping

Toggle **Snap** in the timeline toolbar. When enabled, dragging or trimming a clip snaps its edges to: `0:00`, the playhead, every marker (see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md)), and every other clip's start/end across all tracks (`TimelineWidget._snap_targets()`), within `SNAP_THRESHOLD_PX` (8px) of the pointer. A dashed guide line shows the active snap target while dragging.

## Overlap prevention

Every move/trim commits through `Track.has_overlap()` before being applied (`TimelineWidget._on_clip_moved`/`_on_clip_trimmed`) — a drop or trim that would overlap another clip on the destination track snaps back to the clip's pre-drag position instead of applying. Tracks are strictly non-overlapping by design.

## Ripple editing

Toggle **Ripple** in the timeline toolbar. It changes what **Delete** and **Trim** (either edge) do to the clips *after* the affected one on the **same track**:

- **Off** (default): deleting/trimming leaves whatever gap (or overlap risk) results — the "overwrite" editing model described above.
- **On**: every later clip on that track shifts to close (delete, or a shortening trim) or open (a lengthening trim) the resulting gap, so the track stays contiguous. Move (plain dragging) is deliberately unaffected by Ripple — only Delete and Trim ripple, matching how most NLEs scope "ripple delete"/"ripple trim."

Same-track only, by design — rippling other tracks too would risk shifting something intentionally aligned to a specific global time on a track that had nothing to do with the edit (the same trade-off already made for crossfade transitions' own gap, see [color-and-effects/workflow.md](../color-and-effects/workflow.md)).

Implementation: `core/commands.py::build_ripple_delete_command()`/`build_ripple_trim_command()` compute the shift (the deleted clip's duration, or the trimmed clip's net change in `timeline_end`) and bundle the primary Remove/Trim command with a `MoveClipCommand` per affected clip into one `core.history.CompositeCommand` — so `Ctrl+Z` undoes the whole ripple (every shifted clip, plus the delete/trim itself) in a single step, not one step per clip. A ripple trim still can't push the trimmed clip's edge back into whatever's *before* it — clips that would ripple-shift are excluded from that overlap check (they move in lockstep and can never newly collide with it), but stationary earlier clips still block it, same as a non-ripple trim would.

## Undo/redo

Every operation above goes through a `Command` (`core/commands.py`) executed via `core.history.History`, so `Ctrl+Z`/`Ctrl+Y` (or **Edit > Undo/Redo**) reverses/reapplies it. The Edit menu and toolbar show the label of the next undo/redo action.

## Layout notes

The track-header column (name + M/L/H buttons, `HEADER_WIDTH = 130px`) is pinned to the left edge of the viewport — it stays visible and never overlaps clip content regardless of horizontal scroll position (`TimelineWidget._pin_header_items()`, connected to the view's horizontal scrollbar). Clips, the ruler, the playhead, and the snap guide all live in scene coordinates starting at `x = HEADER_WIDTH`.

## Validation

`tests/test_clip_and_track.py` covers clip duration/speed math and `Track.has_overlap()`/`clip_at()`. `tests/test_commands_and_history.py` covers add/remove/move/trim/split undo-redo round trips (including that a split outside the clip's span is a no-op and that redo is cleared after a new command) plus `RippleEditTests`: ripple delete/trim shift only later same-track clips (a clip on a different track is untouched), a lengthening trim shifts clips later while a shortening one shifts them earlier, a trim with no net duration change ripples nothing, and undo restores every shifted clip in one step. There is no automated test for drag/drop mouse interaction or the header-pinning behavior (both require a live Qt event loop) — verify those manually, or headlessly via a `QT_QPA_PLATFORM=offscreen` script driving `TimelineWidget` directly (which is how the Ripple toolbar button itself was verified).
