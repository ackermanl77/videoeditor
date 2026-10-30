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

While the project has unsaved changes, `MainWindow` autosaves a full snapshot every 2 minutes into `~/.video_editor/autosave/`, one file per editor session (`<pid>-<id>.vproj` plus a `.lock` and a small `.meta.json` with the project's name and real path; three older generations are kept — see "Format 2" below). This is deliberately **not** the project's own file — autosaving never overwrites what the user explicitly saved. (The first version used a single `autosave.vproj` slot shared by every window; a leftover one is still offered for recovery.)

On startup, if a recovery file exists for a session whose process is gone, the user is asked whether to recover it. Declining, or a subsequent successful **Save**, or a confirmed **discard** (closing/opening/starting a new project without saving) all clear the recovery slot — so it never resurfaces work the user has already saved or explicitly thrown away. Recovering restores the project marked dirty (it doesn't exist at its original path yet, since the original file was never overwritten).

## Validation

`tests/test_project_serialization.py` covers the default two-track project, `find_asset_by_path()` path-normalization, a full round-trip through `to_dict()`/`from_dict()`, markers (round-tripping through save/load, including an auto-generated name, and removal), and bounded project loading (oversized files, malformed JSON, excessive collection sizes, total items, and nesting). `tests/test_p2_ui.py` verifies that the size-limit failure is shown in an error dialog. The per-session autosave (locks, generations, which sessions are offered for recovery) is covered by `tests/test_p2_core.py`; offscreen UI tests in `tests/test_p2_ui.py::MainWindowP2Tests` cover accepting recovery (including fallback from a corrupt newest generation), declining and discarding an orphan, and retaining the source after a failed post-recovery save. The same UI class exercises the Recent Projects menu: opening the most recent project, showing/removing a path that goes missing, and clearing the list back to the single disabled "No Recent Projects" placeholder. The atomic Recent Projects write-failure case is also covered. These Qt tests stub modal message boxes so an unattended run cannot block.

## Format 2, relinking and backups (P2)

- `file_version` 2 adds `app_version`, clip `asset_id`, `path_relative`/`source_path_relative`, `volume_keyframes`, `effects` and `link_group`. Version 1 files are migrated (clips linked to assets by path); a newer version is refused with a message naming the version that wrote it; unknown keys are ignored and unknown enum values defaulted, all listed once after opening (`Project.load_warnings`) together with `Project.validate()` problems such as overlapping clips.
- Paths stored relative to the `.vproj` are preferred on load when they exist, so a project folder moved with its media keeps working. Otherwise **File > Relink Media** (`core/relink.py`, `ui/relink_dialog.py`) finds files by name, verifies duration (+-0.1 s) and frame size, links the rest of the same folder automatically, and is one undo step. Offline clips are hatched red on the timeline.
- Autosave is per session (`core/autosave.py`): `<pid>-<id>.vproj` + `.lock`, three older generations kept, recovery offered only for sessions whose process is gone. Each Save first copies the old file to `<name>.vproj.bak`. The window's unsaved-changes star follows `History.is_clean`.

## Format 3 (round 3)

- `file_version` 3 moves the clip fields `brightness`, `contrast`, `saturation`, `gamma` and `chroma_key_*` into the effect stack as `color_correction` / `colorkey` effects (`core.project._migrate_2_to_3`, using `core.effects.legacy_effect_dicts`): neutral values add nothing, a configured but disabled key is kept disabled, and the render is identical. Effects can now carry `"keyframes"` (`{param: [[t, v], ...]}`) for animatable parameters. Version 1 and 2 files open and are migrated in order; files saved now are refused by older versions with the usual "made by a newer version" message.

## Project-file resource limits

Opening a project reads at most 64 MiB from disk. Its JSON collections are limited to 50,000 entries each and 200,000 entries total, with a maximum nesting depth of 64. These bounds align with the app's existing 50,000-file relink scan and 200,000-word transcript cap while leaving room for large editing sessions. Projects beyond a limit are rejected with an actionable error instead of being partially loaded; the same bounds apply to autosave recovery.

## Reverse, freeze frame and subtitles (round 4)

`Clip.reverse` and `Clip.freeze_frame_duration` are new clip keys with defaults (off / 0), so the file format stays **3**: older projects open unchanged, and a negative or malformed freeze value loads as 0. Imported subtitles are plain text clips. `MediaAsset.filmstrip_path` is a cache path, saved like the thumbnail path. Note that an older build opening a newer project ignores the new keys (a reversed clip would play forwards there).
