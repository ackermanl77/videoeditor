# Shorts / Vertical Video Export Workflow

## Goal

Export a project as a vertical 9:16 video suitable for YouTube Shorts, TikTok, or Instagram Reels: auto-crop to fill a vertical frame (with a smart or manual point of interest, instead of letterboxing), cap the duration, and apply a ready-made platform preset — for the user's own or properly licensed footage. This is not a copyright-evasion feature: it does not, and cannot, make republishing someone else's copyrighted movie/series footage non-infringing (see `docs/PROJECT_STATUS.md`'s Known Limitations for the explicit scope decision behind this).

## Reframe modes (per clip)

Every video/image clip has a `reframe_mode` (`Clip.reframe_mode` in `core/clip.py`, `ReframeMode.FIT` or `ReframeMode.FILL`):

- **Fit** (default, unchanged from before this feature): scale the clip to fit entirely inside the canvas, preserving aspect ratio — may letterbox (black bars) when the clip and canvas aspect ratios differ.
- **Fill**: scale the clip to fully *cover* the canvas instead, then crop the overflow — no letterboxing, at the cost of cropping some of the frame. The crop is centered on `crop_focus_x`/`crop_focus_y` (0.0-1.0, default 0.5/0.5 = center) — this is the "manual point of interest" reframing: drag **Crop Focus X/Y** in the Properties panel's **Reframe (Vertical / Shorts)** group to keep, say, a person on the left side of a 16:9 shot centered in a 9:16 vertical crop instead of cropping them out.

Set per clip via the **Fill Frame** checkbox in that same group; **Crop Focus X/Y** are only enabled (and only matter) in Fill mode.

There is no automatic subject-detection ("smart") reframing — "smart" here means *fast to set up* (one checkbox + two sliders, not building a keyframed Transform), not automatic. Doing real subject tracking would need a frame-content analysis pass this codebase doesn't have; manual focus-point selection was the scope call made instead.

## Export-wide auto-fill override

Turning on **every** clip's Fill Frame individually for a one-off vertical export would be tedious, and would also permanently change the project's own stored per-clip settings. Instead, the Export dialog's **Auto-fill vertical frame** checkbox passes a `reframe_override` to `build_filter_graph()` (`core/filter_graph.py`) that forces every video/image clip's reframe mode for *that render only* — implemented by `_project_with_reframe_override()`, which deep-copies the project and overrides the copy's clips before building the graph, leaving the real project (and every clip's own `reframe_mode`) untouched. A clip whose own Fill Frame is already checked behaves the same whether or not the export-wide override is also on.

## Duration cap / Trim Range

`build_filter_graph()` also accepts a `trim_range: (start, end)` — when given, the graph is built for that window only (clips outside it are left out, straddling clips are seeked to it, everything is placed relative to its start; see [export/workflow.md](../export/workflow.md)), so a 60 s Short cut from a long timeline costs about 60 s of rendering (an `end` of `None` means "to the project's end"). It used to render the whole timeline and trim the result. `ExportSettings.trim_start`/`trim_end` (`core/exporter.py`) carry this through `Exporter.export()`.

In the Export dialog, **Trim Range**'s **Limit duration** checkbox enables Start/End spinboxes (seconds). Selecting a **Shorts Platform** preset with a duration cap (currently YouTube Shorts, 60s) auto-checks it, sets Start to 0, and clamps End's own spinbox *maximum* to the cap — so a manual edit afterward can't exceed it while that platform stays selected, not just a one-time default value.

## Platform presets

The **Shorts Platform** combo (`ui/export_dialog.py`) applies, when set to something other than "None":
- Resolution → 1080 x 1920 (a new entry in `RESOLUTION_PRESETS`)
- Frame rate → 30
- **Auto-fill vertical frame** → checked
- A duration cap, if the platform has one (`PLATFORM_DURATION_CAPS`): YouTube Shorts 60s, Instagram Reels 90s, TikTok none. These are adjustable starting points reflecting each platform's typical short-form vertical slot, not a fetched or contractual limit — the user can always widen/narrow Trim Range afterward (except past a capped platform's own maximum, while that platform stays selected).

Choosing "None (custom settings)" leaves every setting exactly as the user last configured it — the preset only pushes values on selection, it doesn't lock the form.

## Chapters interact correctly with trimming

Project markers still embed as real chapters (see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md)) even when the export is trimmed: `Exporter._write_chapters_file()` re-anchors each chapter to the trimmed output's own 0-based timeline (a chapter starting before the trim window starts at 0 in the output, not at a negative time) and drops any chapter that falls entirely outside the trimmed range. This was verified against real FFmpeg (see Validation) — trimming a 10s, 2-marker project to `[2, 6)` produced exactly 2 correctly-clipped, correctly-re-anchored chapters in the output file's own metadata, not the original 2 at their untrimmed times.

## Validation

`tests/test_filter_graph.py::ReframeTests` covers: Fit is unchanged from before this feature (no `crop=`, `force_original_aspect_ratio=decrease`), Fill mode produces `force_original_aspect_ratio=increase` + a `crop=` expression using the clip's own focus point instead of a `pad`, Transform's scale/rotation are skipped in Fill mode (the cropped frame is already exactly canvas-sized) while Transform's position collapses to dead-center (a documented no-op, since there's nothing left to reposition), and `reframe_override` forces Fill for the graph without mutating the original clip's stored `reframe_mode`. `tests/test_filter_graph.py::TrimRangeTests` covers: no `trim_range` leaves the output labels unwrapped (`None` end defaults to the project's own duration; a set range wraps `video_out_label`/`audio_out_label` in `trim`/`atrim` with the trimmed labels correctly returned as `GraphResult.video_out_label`/`audio_out_label`).

`tests/test_exporter.py::ChaptersFileTests` covers the trim/chapters interaction directly: a chapter overlapping the trim window is clipped and re-anchored to 0-based output time, and a chapter entirely before or after the window is dropped. `tests/test_exporter.py::ShortsExportSettingsTests` covers `ExportSettings.reframe_override`/`trim_start`/`trim_end` actually reaching the real FFmpeg command line (via the same mocked-`subprocess.Popen` pattern as the two-pass regression test — see [export/workflow.md](../export/workflow.md)), not just existing as unused dataclass fields.

Verified against real FFmpeg execution (this codebase's established bar for anything touching `core/exporter.py`/`core/filter_graph.py` — see `docs/PROJECT_STATUS.md`): a synthetic 640x180 half-red/half-blue source, cropped to a 360x640 (and later 1080x1920) vertical canvas, confirmed the crop-to-fill technique keeps the correct half of the frame at `crop_focus_x=0.0` vs `1.0` (sampled the exact output pixel color with `ffprobe`/a raw-pixel dump). A full `Exporter.export()` run — real FFmpeg, no mocking — with a Fill-mode clip, two markers, and `trim_start=2.0`/`trim_end=6.0` produced a 1080x1920, exactly-4.000s output whose embedded chapters (read back with `ffprobe -show_chapters`) were correctly clipped and re-anchored to `[0, 1)` and `[1, 4)`. A second real run using only the export-wide `reframe_override` (no per-clip Fill Frame set at all) produced the same 1080x1920 vertical output from the same 640x180 source, and confirmed the clip's own `reframe_mode` was still `FIT` afterward — the override never touched the real project.
