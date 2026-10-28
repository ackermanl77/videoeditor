# Changelog

All notable changes to Open Video Editor. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased] -- 0.2.0

### Added
- Keyframed and tracked effect masks, recording (voiceover, screen, webcam), Keep Subject in
  Frame, color wheels and Auto Color, speech search, a keyframe graph and an Explorer
  "Edit with" verb.
- Subclips, named workspaces, Lift/Extract/Match Frame/Add Edit/Extend Edit, an audio track
  mixer, and word-by-word and translated captions.
- Video scopes, audio effects (gate, EQ, compressor), keyframe easing, Ctrl+K command search,
  automatic captions with faster-whisper, a markers panel, compound clips, motion tracking,
  smooth speed ramps, title styles, OpenTimelineIO export/import, scene detection.
- 10-bit and HDR export, stabilization, a render queue, styled ASS/SSA subtitles, a loudness
  meter, background preview renders and per-source GPU decoding.
- A log file (`~/.video_editor/logs/video_editor.log`, plus `crash.log` for native crashes);
  any uncaught error is logged and shown in a dialog instead of vanishing.
- A once-per-launch update check against the project's GitHub Releases (update.json).
- Dependabot, and a `pip-audit` job in CI.
- `THIRD_PARTY_NOTICES.md` with the FFmpeg (GPLv3) and PySide6 (LGPLv3) terms and the FFmpeg
  source pointer, shipped in the build and the installer; `SECURITY.md`; a privacy note.

### Fixed
- Project recovery and loading keep every autosave generation and bound what they read.

## [0.1.0] - 2026-09-17

- First release: a multi-track timeline, media bin, live preview, undo/redo, transitions,
  text overlays and an FFmpeg export with NVENC support.
