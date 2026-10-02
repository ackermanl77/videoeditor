# Timeline Editing Workflow

## Goal

Arrange imported media on a multi-track timeline: move, trim, split, and delete clips; add/remove tracks; mute, lock, or hide a track.

## Empty-state hint

While every track is empty (a brand-new project, or one that's had everything deleted), a centered, non-interactive label overlays the timeline — "Import media (Ctrl+I), then drag a clip from the Media Bin here to begin editing" (`TimelineWidget.empty_hint_label`) — instead of a first-time user seeing nothing but a bare ruler and empty track rows. It's a child of the `QGraphicsView`'s viewport (not a `QGraphicsItem` in the scene), repositioned to fill the viewport on every resize (`TimelineView.resizeEvent`) and re-evaluated at the end of every `rebuild()`; `Qt.WA_TransparentForMouseEvents` keeps it from intercepting clicks meant for the view beneath it, like seeking via the ruler.

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

Adding media or text never overlaps either: a drop onto an occupied spot (or **T** over an existing clip) lands on another track of the same kind where the spot is free, or on a new track added in the same undo step (`core/commands.py::build_add_clip_command()`, `AddTrackCommand` now keeps one track id across redo). The drop row is the row under the mouse (`row_at_scene_y()`, `floor`) -- rounding sent a drop on the lower half of a track to the next one. Click a track header to select the track (accent outline); **Delete Track** acts on it, so empty tracks can be deleted.

## Ripple editing

Toggle **Ripple** in the timeline toolbar. It changes what **Delete** and **Trim** (either edge) do to the clips *after* the affected one on the **same track**:

- **Off** (default): deleting/trimming leaves whatever gap (or overlap risk) results — the "overwrite" editing model described above.
- **On**: every later clip on that track shifts to close (delete, or a shortening trim) or open (a lengthening trim) the resulting gap, so the track stays contiguous. Move (plain dragging) is deliberately unaffected by Ripple — only Delete and Trim ripple, matching how most NLEs scope "ripple delete"/"ripple trim."

Same-track only, by design — rippling other tracks too would risk shifting something intentionally aligned to a specific global time on a track that had nothing to do with the edit (the same trade-off already made for crossfade transitions' own gap, see [color-and-effects/workflow.md](../color-and-effects/workflow.md)).

Implementation: `core/commands.py::build_ripple_delete_command()`/`build_ripple_trim_command()` compute the shift (the deleted clip's duration, or the trimmed clip's net change in `timeline_end`) and bundle the primary Remove/Trim command with a `MoveClipCommand` per affected clip into one `core.history.CompositeCommand` — so `Ctrl+Z` undoes the whole ripple (every shifted clip, plus the delete/trim itself) in a single step, not one step per clip. A ripple trim still can't push the trimmed clip's edge back into whatever's *before* it — clips that would ripple-shift are excluded from that overlap check (they move in lockstep and can never newly collide with it), but stationary earlier clips still block it, same as a non-ripple trim would.

Trim math (the trim drag in `ClipItem` and `build_ripple_trim_command`) uses the real `Clip.duration` of the trimmed clip, so speed-ramped clips ripple by the right amount (it assumed one constant speed and could create an overlap).

## Zoom, scrubbing and keyboard navigation

- Zoom (slider, `Ctrl+=`/`Ctrl+-`, `Ctrl`+wheel) rescales the existing items in place and keeps the anchor still: the point under the mouse for wheel zoom, otherwise the playhead. **Timeline > Zoom to Fit** (`Shift+Z`) fits the whole project.
- Press on the ruler (or the playhead handle in it) and drag to scrub; preview seeks are throttled to ~30 Hz, with a final exact seek on release.
- **Playback** menu: `Left`/`Right` one frame, `Shift+Left`/`Shift+Right` one second, `Home`/`End`, `Up`/`Down` previous/next edit point (these are active while the timeline or preview has focus, so they don't steal arrow keys from the Properties panel), `Shift+M`/`Ctrl+Shift+M` previous/next marker, `I`/`O` In/Out points (shaded on the ruler and offered as the Export dialog's Trim Range; view state, not saved), `Alt+X` clear, `J`/`K`/`L` shuttle.
- The ruler shows `MM:SS:FF` below one-second ticks, and the preview/status bar show `HH:MM:SS:FF` at the project frame rate.

## Undo/redo

Every operation above goes through a `Command` (`core/commands.py`) executed via `core.history.History`, so `Ctrl+Z`/`Ctrl+Y` (or **Edit > Undo/Redo**) reverses/reapplies it. The Edit menu and toolbar show the label of the next undo/redo action. The toolbar reuses the menu's `QAction`s, so each shortcut is registered once (Export's `Ctrl+E` used to be on two actions and fired neither).

## Layout notes

The track-header column (name + M/L/H buttons, `HEADER_WIDTH = 130px`) is pinned to the left edge of the viewport — it stays visible and never overlaps clip content regardless of horizontal scroll position (`TimelineWidget._pin_header_items()`, connected to the view's horizontal scrollbar). Clips, the ruler, the playhead, and the snap guide all live in scene coordinates starting at `x = HEADER_WIDTH`.

The toolbar row (8 buttons plus the Zoom slider) uses a custom `FlowLayout` (`ui/widgets/timeline_widget.py`, adapted from Qt's own "Flow Layout" example) instead of a plain `QHBoxLayout`. At its natural size the row needs ~1,088px — a `QHBoxLayout` would let it run past the edge of any narrower window, with no scrollbar and no wrap, rendering the rightmost buttons genuinely unreachable. `FlowLayout` instead wraps whatever doesn't fit onto additional rows, so every button and the zoom slider stay visible and clickable at any window width — measured directly (not assumed) at three widths: one row at 1,400px, three rows at 600px, five rows at 320px, every button's right edge still within the available width in all three cases. One behavior change from the plain `QHBoxLayout` this replaced: the Zoom label/slider used to be pushed to the row's far right via `addStretch()`, which `FlowLayout` doesn't support (a "push to the far edge" concept doesn't really apply once a row can wrap) — it now just follows immediately after the last button, wrapping to its own row if it doesn't fit.

## Validation

`tests/test_clip_and_track.py` covers clip duration/speed math and `Track.has_overlap()`/`clip_at()`. `tests/test_commands_and_history.py` covers add/remove/move/trim/split undo-redo round trips (including that a split outside the clip's span is a no-op and that redo is cleared after a new command) plus `RippleEditTests`: ripple delete/trim shift only later same-track clips (a clip on a different track is untouched), a lengthening trim shifts clips later while a shortening one shifts them earlier, a trim with no net duration change ripples nothing, and undo restores every shifted clip in one step. There is no automated test for drag/drop mouse interaction or the header-pinning behavior (both require a live Qt event loop) — verify those manually, or headlessly via a `QT_QPA_PLATFORM=offscreen` script driving `TimelineWidget` directly (which is how the Ripple toolbar button itself was verified, how the empty-state hint above was verified: visible for a freshly-constructed project, hidden after adding a clip and calling `rebuild()`, visible again after removing it and rebuilding, and correctly resized to match the viewport after `TimelineWidget.resize()`; and how `FlowLayout`'s wrapping was verified: resizing the widget to 1400/600/320px and reading every toolbar button's actual `geometry()` back — confirming both the row count that emerges at each width and that every button's right edge stays within the resized width, not just that the call didn't crash. Also confirmed visually via `QWidget.grab()`: at 600px the button row and Zoom label share one row with the slider wrapping to a second, no button clipped or cut off at the window edge).

## Multi-clip editing and NLE tools (P2)

All in `core/edit_ops.py` (Qt-free, one undo step each), surfaced by `TimelineWidget`, the Edit menu and the clip context menu:

- **Selection**: Ctrl+click, rubber band, Ctrl+A, Tab/Shift+Tab; dragging one selected (or linked) clip moves the group by the same time on their own tracks, refused if anything would overlap; Alt+Left/Right nudges by a frame; Delete removes the selection (Ripple closes each track's gaps).
- **Clipboard**: copy/cut/paste at playhead (fresh ids, original tracks when free, else another or a new track), duplicate, paste attributes.
- **Insert / Overwrite** (`,` / `.`) the Media Bin selection at the playhead (insert ripples every unlocked track), **Roll** to playhead, **Slip**, **Slide**, **Razor** (C), **Detach Audio** (a linked audio clip; the video clip is muted).
- A left trim now re-bases speed/opacity/position/volume keyframes so they stay on the same content (`Clip.with_source_range`).
- After an edit the scene is updated by clip id (only added/removed clips create or delete items); a change to tracks, headers or markers still rebuilds it.
