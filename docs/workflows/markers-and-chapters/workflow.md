# Markers and Chapters Workflow

## Goal

Bookmark points on the shared project timeline (independent of any clip/track), and have them show up as real, player-navigable chapters in the exported video.

## Steps

1. Position the playhead where you want a marker, then click **+ Marker** in the timeline toolbar, press **M**, or use **Timeline > Add Marker at Playhead**. A dialog asks for a name (pre-filled with "Marker N"); confirming adds it.
2. Markers render as a small colored flag in the ruler band, with a thin dashed line running down through the tracks so its exact position is easy to line up against clips.
3. **Left-click** a marker's flag to seek the playhead there. **Right-click** for **Rename...** / **Delete Marker**.
4. Markers participate in snapping (see [timeline-editing/workflow.md](../timeline-editing/workflow.md)) — dragging or trimming a clip snaps to marker positions the same way it snaps to clip edges, the playhead, and `0:00`.

## Chapters on export

Every marker becomes a real chapter in the exported file (for any format except GIF, which has no meaningful concept of chapters) — a chapter spans from its own marker's time to the *next* marker's time, and the last marker's chapter runs to the end of the video. Players that support chapters (VLC, most browsers' native video controls, YouTube once uploaded, etc.) show these as jump points.

Implementation: `Exporter._write_chapters_file()` writes a temporary FFmpeg [FFMETADATA](https://ffmpeg.org/ffmpeg-formats.html#Metadata-1) file (one `[CHAPTER]` block per marker, with `START`/`END` in milliseconds), passed to FFmpeg as an extra metadata-only pseudo-input (`-f ffmetadata -i <file>`) alongside the real media inputs, then applied via `-map_metadata <that input's index>`. This pseudo-input has no audio/video streams of its own, so it doesn't shift any `[N:v]`/`[N:a]` index the filter graph already references — it's purely a container-metadata attachment, safe to add without touching the rest of the export pipeline. The temp file is deleted after the export finishes (success or failure). Marker names are escaped for FFMETADATA's own metacharacters (`=`, `;`, `#`, `\`) so a name containing them doesn't get mis-parsed as a new key or a comment.

## What this surfaced: a real, pre-existing two-pass export bug

While verifying that chapter embedding didn't interact badly with two-pass export, two-pass export itself turned out to be **completely broken already**, for every project, regardless of markers: pass 1 (`-an`, video-only, output discarded) never mapped or otherwise consumed the base filter graph's always-present audio output pad (`[aout]` — even a silent `anullsrc` one when nothing is audible), and FFmpeg refuses to run at all with an unconsumed named filtergraph output ("... has output ... unconnected"). This is the exact same class of bug GIF export hit earlier (see `docs/PROJECT_STATUS.md`) — just never actually run end-to-end for two-pass mode until now. **Fixed** by routing `[aout]` into an `anullsink` in pass 1, exactly like the GIF fix does. See `docs/PROJECT_STATUS.md`'s "Recently Fixed" for the full writeup.

## Validation

`tests/test_project_serialization.py` covers `Marker`/`Project.add_marker`/`sorted_markers` and a full `.vproj` save/load round trip. `tests/test_commands_and_history.py::MarkerCommandTests` covers add/remove/rename undo-redo. `tests/test_exporter.py` covers the chapters-file construction (correct START/END boundaries, special-character escaping, correct `-map_metadata` input index) and — via a mocked `subprocess.Popen` that never actually spawns FFmpeg — the two-pass regression fix specifically (asserting pass 1's filter graph contains `anullsink`). These exporter tests are a deliberate, narrow exception to this codebase's usual "no unit tests for `core/exporter.py`, verify manually against real FFmpeg" rule: they check pure argument/file-construction logic that doesn't require executing FFmpeg to verify, the same category `core/filter_graph.py` tests already cover.

Also verified against real FFmpeg execution: a project with two markers exported real, `ffprobe`-visible chapters with the correct boundaries and titles; a marker name containing `=`, `;`, `#`, and `\` round-tripped correctly (confirmed via `ffprobe -show_chapters`, not just visual inspection of the intermediate file); and both the no-audio and has-audio two-pass cases were re-verified after the fix.
