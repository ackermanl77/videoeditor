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
5. **File > Open Recent** lists up to `MAX_RECENT_PROJECTS` (10) most-recently opened-or-saved `.vproj` paths, most recent first; picking one goes through the same discard-confirmation and missing-media check as **Open Project...**. Re-saving/re-opening a path already in the list moves it to the front instead of duplicating it.

## Recent Projects

Persisted as a plain JSON array of paths at `~/.video_editor/recent_projects.json` (`MainWindow._load_recent_projects()`/`_save_recent_projects()`), updated by both `_save_to()` (Save/Save As) and `_open_project_from_path()` (Open Project.../Open Recent) — so a project shows up as "recent" whether you just opened it or just saved it, matching most editors' convention. The **Open Recent** submenu is rebuilt on every change (`MainWindow._refresh_recent_projects_menu()`); a path that no longer exists on disk is shown grayed out, suffixed "(missing)", rather than silently dropped — it's only actually removed from the list once you try to open it and it's confirmed missing (`_open_recent_project()`), the same "don't erase something the user might restore" caution already applied to the media-pool "Missing Media" warning. **Clear Recent Projects** empties the list entirely. An empty list shows a single disabled "No Recent Projects" entry instead of an empty submenu.

## Autosave and crash recovery

While the project has unsaved changes, `MainWindow` autosaves a full snapshot to a fixed recovery slot every 2 minutes: `~/.video_editor/autosave/autosave.vproj`, with a small sidecar `.meta.json` recording the project's name and its real file path (if any). This is deliberately **not** the project's own file — autosaving never overwrites what the user explicitly saved.

On startup, if a recovery file exists, the user is asked whether to recover it. Declining, or a subsequent successful **Save**, or a confirmed **discard** (closing/opening/starting a new project without saving) all clear the recovery slot — so it never resurfaces work the user has already saved or explicitly thrown away. Recovering restores the project marked dirty (it doesn't exist at its original path yet, since the original file was never overwritten).

## Validation

`tests/test_project_serialization.py` covers the default two-track project, `find_asset_by_path()` path-normalization, a full round-trip through `to_dict()`/`from_dict()`, and markers (round-tripping through save/load, including an auto-generated name, and removal). The autosave/recovery flow (file I/O against `~/.video_editor/autosave/`, the recovery prompt) has no automated test — it was verified manually with an isolated `HOME`/`USERPROFILE` pointing at a scratch directory, confirming a written autosave is correctly offered and restored by a fresh `MainWindow` instance, and that `_dirty` is set to `True` on the recovered project. Recent Projects likewise has no automated test but was verified headlessly (`QT_QPA_PLATFORM=offscreen`, with `QMessageBox.warning`/`.critical`/`.question` stubbed out so a real modal dialog doesn't block the unattended script forever): saving two projects populates the menu most-recent-first, re-saving an already-listed path moves it to the front without duplicating, opening a recent project correctly loads it, opening one whose file has since been deleted shows a warning and removes it from the list rather than crashing or leaving a dead entry behind, and Clear Recent Projects empties it back to the single disabled "No Recent Projects" placeholder.

## Format 2, relinking and backups (P2)

- `file_version` 2 adds `app_version`, clip `asset_id`, `path_relative`/`source_path_relative`, `volume_keyframes`, `effects` and `link_group`. Version 1 files are migrated (clips linked to assets by path); a newer version is refused with a message naming the version that wrote it; unknown keys are ignored and unknown enum values defaulted, all listed once after opening (`Project.load_warnings`) together with `Project.validate()` problems such as overlapping clips.
- Paths stored relative to the `.vproj` are preferred on load when they exist, so a project folder moved with its media keeps working. Otherwise **File > Relink Media** (`core/relink.py`, `ui/relink_dialog.py`) finds files by name, verifies duration (+-0.1 s) and frame size, links the rest of the same folder automatically, and is one undo step. Offline clips are hatched red on the timeline.
- Autosave is per session (`core/autosave.py`): `<pid>-<id>.vproj` + `.lock`, three older generations kept, recovery offered only for sessions whose process is gone. Each Save first copies the old file to `<name>.vproj.bak`. The window's unsaved-changes star follows `History.is_clean`.
