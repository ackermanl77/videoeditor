# Project Files Workflow

## Goal

Save and reopen a complete editing session — media pool, tracks, clips, and settings — as one `.vproj` file, with automatic crash recovery.

## What it contains

`.vproj` is plain, human-readable JSON (`core/project.py::Project.to_dict()`/`from_dict()`): a file version number, project settings (resolution/fps/sample rate), every track with its clips (including trim points, timeline position, volume/speed/opacity/fades, Transform/reframe/keyframe/speed-ramp fields, and text style where applicable), every marker (see [markers-and-chapters/workflow.md](../markers-and-chapters/workflow.md)), and the media pool (each imported asset's path and probed metadata). Source media itself is **not** embedded — only paths — so moving or deleting a source file breaks the clips that reference it (see the "Missing / unreadable files" section in [media-import/workflow.md](../media-import/workflow.md)).

## Steps

1. **File > Save Project** (`Ctrl+S`) — saves to the current path, or prompts for one the first time.
2. **File > Save Project As...** (`Ctrl+Shift+S`) — always prompts for a new path.
3. **File > Open Project...** (`Ctrl+O`) — prompts to discard unsaved changes first if the current project is dirty; warns afterward if any referenced media can't be found.
4. **File > New Project** (`Ctrl+N`) — same discard-confirmation, then resets to an empty two-track (one video, one audio) project.

## Autosave and crash recovery

While the project has unsaved changes, `MainWindow` autosaves a full snapshot to a fixed recovery slot every 2 minutes: `~/.video_editor/autosave/autosave.vproj`, with a small sidecar `.meta.json` recording the project's name and its real file path (if any). This is deliberately **not** the project's own file — autosaving never overwrites what the user explicitly saved.

On startup, if a recovery file exists, the user is asked whether to recover it. Declining, or a subsequent successful **Save**, or a confirmed **discard** (closing/opening/starting a new project without saving) all clear the recovery slot — so it never resurfaces work the user has already saved or explicitly thrown away. Recovering restores the project marked dirty (it doesn't exist at its original path yet, since the original file was never overwritten).

## Validation

`tests/test_project_serialization.py` covers the default two-track project, `find_asset_by_path()` path-normalization, a full round-trip through `to_dict()`/`from_dict()`, and markers (round-tripping through save/load, including an auto-generated name, and removal). The autosave/recovery flow (file I/O against `~/.video_editor/autosave/`, the recovery prompt) has no automated test — it was verified manually with an isolated `HOME`/`USERPROFILE` pointing at a scratch directory, confirming a written autosave is correctly offered and restored by a fresh `MainWindow` instance, and that `_dirty` is set to `True` on the recovered project.
