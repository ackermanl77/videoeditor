# Open Video Editor — User Guide

A complete, plain-language guide to the app and everything it can do. If you
just want architecture/contributor-facing detail instead, see
[PROJECT_STATUS.md](PROJECT_STATUS.md) and the per-feature docs under
[workflows/](workflows/README.md) — this guide is for *using* the app, not
building it.

## Table of contents

1. [What is Open Video Editor?](#what-is-open-video-editor)
2. [Installing and running](#installing-and-running)
3. [The interface at a glance](#the-interface-at-a-glance)
4. [Quick start](#quick-start)
5. [Importing media](#importing-media)
6. [The Media Bin](#the-media-bin)
7. [Timeline editing](#timeline-editing)
8. [Markers and chapters](#markers-and-chapters)
9. [Clip properties (volume, speed, opacity, fades)](#clip-properties-volume-speed-opacity-fades)
10. [Transform: position, scale, rotation](#transform-position-scale-rotation)
11. [Keyframing (animating opacity and position)](#keyframing-animating-opacity-and-position)
12. [Speed ramps](#speed-ramps)
13. [Crossfade transitions](#crossfade-transitions)
14. [Color correction](#color-correction)
15. [Chroma key (green screen)](#chroma-key-green-screen)
16. [Text overlays](#text-overlays)
17. [Shorts / vertical video export](#shorts--vertical-video-export)
18. [The preview player — what it does and doesn't show](#the-preview-player--what-it-does-and-doesnt-show)
19. [Project files, autosave, and Recent Projects](#project-files-autosave-and-recent-projects)
20. [Exporting your video](#exporting-your-video)
21. [Keyboard shortcuts](#keyboard-shortcuts)
22. [Tips, limits, and things to know](#tips-limits-and-things-to-know)
23. [Troubleshooting](#troubleshooting)

---

## What is Open Video Editor?

Open Video Editor is a free, desktop, non-linear video editor. You import
video/audio/image files, arrange them on a multi-track timeline, adjust and
animate clips, add text and transitions, and export a finished video —
rendered by [FFmpeg](https://ffmpeg.org/), the same engine behind most
professional and open-source video tools.

It's built to feel familiar if you've used any other timeline-based editor
(Premiere, DaVinci Resolve, Kdenlive, Shotcut, CapCut): a media bin on the
left, a preview in the middle, a properties/inspector panel on the right,
and a timeline across the bottom.

## Installing and running

**If you just want to use the app** (Windows): download `VideoEditor.exe`
from the project's [Releases page](https://github.com/ackermanl77/videoeditor/releases)
and double-click it. No Python installation needed — it's a single
self-contained file. It takes a couple of seconds to start (a one-file
build unpacks itself to a temp folder each launch) — that's normal.

**If you're running it from source:**

1. Install [Python 3.10+](https://www.python.org/) and
   [FFmpeg](https://ffmpeg.org/download.html) (with `ffprobe`), and make
   sure `ffmpeg`/`ffprobe` are on your system `PATH`.
2. From the project folder:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python main.py
   ```

If FFmpeg can't be found on startup, the app still opens but warns you —
importing, previewing, and exporting all depend on it, so install it and
restart.

## The interface at a glance

```
┌─────────────────────────────────────────────────────────────┐
│  Menu bar (File · Edit · Timeline · Help)                    │
├─────────────────────────────────────────────────────────────┤
│  Toolbar (Import · Save · Undo/Redo · Split · Delete · ...)  │
├───────────────┬───────────────────────────┬─────────────────┤
│  MEDIA BIN     │        PREVIEW            │   PROPERTIES    │
│  (your         │  (plays the topmost       │  (controls for  │
│  imported      │   clip at the             │   whichever     │
│  files)        │   playhead)               │   clip is       │
│                │                           │   selected)     │
├───────────────┴───────────────────────────┴─────────────────┤
│  TIMELINE toolbar (+Track · Split · Delete · Snap · Ripple…) │
│  ─────────────────────────────────────────────────────────  │
│  Video 1  [clip][clip]      [clip]                           │
│  Audio 1        [clip]                                       │
└─────────────────────────────────────────────────────────────┘
```

Every panel is resizable — drag the thin divider between any two of them.
They can be made very narrow, but never dragged away completely, so you
never accidentally lose a panel. Your window size and panel proportions are
remembered the next time you open the app.

## Quick start

1. **Import** your video/audio/image files (drag them onto the Media Bin,
   or click **Import**).
2. **Drag** a clip from the Media Bin onto a timeline track (or
   double-click it to append it to the end).
3. **Arrange**: drag clips to move them, drag their edges to trim, select
   one and press **S** to split it at the playhead.
4. **Select a clip** and use the **Properties** panel on the right to
   adjust volume, speed, position, color, and more.
5. **Preview** with **Space** to check timing (remember: the preview is a
   simplified view — see [below](#the-preview-player--what-it-does-and-doesnt-show)).
6. **Export** with **Ctrl+E** when you're happy with the result.
7. **Save your project** (**Ctrl+S**) so you can come back and keep
   editing later — this saves your edit, not a rendered video.

## Importing media

- **File > Import Media...** (`Ctrl+I`), the **Import** button in the
  Media Bin, or just **drag files onto the Media Bin** from your file
  explorer.
- Supported: most common video (MP4, MOV, MKV, AVI, WebM, FLV, WMV, MPG,
  3GP, TS/M2TS, OGV, ...), audio (MP3, WAV, AAC, FLAC, OGG, Opus, WMA,
  AIFF, ...), and image (PNG, JPG, BMP, WebP, TIFF, GIF, ...) formats.
  Anything your installed FFmpeg can decode will generally work even if
  it's not in this list.
- Each file is analyzed (duration, resolution, frame rate) and a thumbnail
  is generated automatically. Large batches import in the background, with
  a progress bar in the status bar — the app stays responsive while it
  works.
- If a file can't be imported (unsupported/corrupt), you'll see a clear
  error listing exactly which file(s) failed and why.
- If a project references a file that's since been moved or deleted,
  opening it warns you which files are missing; affected clips stay in the
  project but appear blank/silent until the file is restored at its
  original location.

## The Media Bin

The left-hand panel lists everything you've imported, with thumbnails and
durations. From here you can:

- **Drag** an item onto any timeline track.
- **Double-click** an item to append it to the end of the first compatible
  track (video/image files go to a video track, audio-only files go to an
  audio track).

An empty bin shows its own instructions ("Drop video, audio, or image
files here...") instead of looking like a blank, confusing rectangle.

## Timeline editing

The timeline is where you arrange your edit.

- **Add a clip**: drag it from the Media Bin, or double-click it there.
- **Move a clip**: drag it horizontally (or between tracks).
- **Trim a clip**: drag its left or right edge.
- **Split a clip**: position the playhead where you want the cut, select
  the clip, then press **S** or click **Split**.
- **Delete a clip**: select it and press **Delete**, or click **Delete**.
- **Add/remove tracks**: **+ Video Track** / **+ Audio Track** in the
  timeline toolbar; select a clip and click **Delete Track** to remove its
  track.
- **Mute / Lock / Hide a track**: click a track header's **M** / **L** /
  **H** buttons. A locked track's clips can't be dragged or trimmed; a
  hidden video track is skipped by both the preview and the final export.
- Tracks are strictly non-overlapping — a move or trim that would overlap
  another clip on the same track snaps back instead of applying.

**Snapping.** Toggle **Snap** in the timeline toolbar. While on, dragging
or trimming a clip snaps to `0:00`, the playhead, every marker, and every
other clip's start/end — a dashed guide line shows the active snap target.

**Ripple editing.** Toggle **Ripple** in the timeline toolbar. With it on,
**Delete** and **Trim** (either edge) also shift every *later clip on that
same track* to close (or open) the resulting gap, so the track stays
contiguous — a single **Ctrl+Z** undoes the whole ripple at once, not one
step per shifted clip. Plain dragging (Move) is never rippled — only
Delete and Trim. Ripple only affects the track you're editing; other
tracks are never shifted.

**Zoom.** Use the Zoom slider in the timeline toolbar, or **Ctrl+=**/
**Ctrl+-** (or **Ctrl+scroll**), to zoom the timeline in or out.

**Narrow windows.** If the window gets too narrow for every timeline
toolbar button to fit on one row, the extra buttons wrap onto additional
rows automatically — nothing is ever cut off or unreachable.

## Markers and chapters

Markers are named bookmarks on the timeline, independent of any clip or
track — handy for noting "intro ends here" or "add B-roll here."

- **Add one**: position the playhead, then click **+ Marker**, press
  **M**, or use **Timeline > Add Marker at Playhead**. You'll be asked to
  name it.
- Markers show as a small colored flag with a dashed line through the
  tracks. **Left-click** to seek there; **right-click** to **Rename** or
  **Delete**.
- Markers participate in snapping, just like clip edges and the playhead.
- **On export, every marker becomes a real, player-navigable chapter** —
  VLC, most browsers, and YouTube (once uploaded) will show these as jump
  points. A chapter runs from its own marker to the next one, and the last
  marker's chapter runs to the end.

## Clip properties (volume, speed, opacity, fades)

Select any clip to see its controls in the **Properties** panel on the
right. Every group is collapsible — click its title (or the checkbox next
to it) to expand or collapse it; a collapsed group's settings still apply,
they're just tucked out of the way.

For video/audio clips, the **Clip Adjustments** group offers:

| Property | Range | What it does |
|---|---|---|
| Volume | 0–200% | Above 100% amplifies the audio. |
| Mute | on/off | Silences the clip's own audio. |
| Speed | 0.25x–4.0x | Slow motion to 4x fast-forward (also changes how much of the source maps to the timeline). |
| Opacity | 0–100% | How transparent the clip is (video/image only; not shown in the live preview, only in the exported file). |
| Fade In / Fade Out | 0–30s | A gradual fade at the start/end — applied to video and to the clip's own audio. |

Dragging a slider previews live; releasing it commits one single undo
step for the whole drag. Everything here is undoable with **Ctrl+Z**.

## Transform: position, scale, rotation

For video/image clips, the **Transform** group lets you reposition, resize,
and rotate a clip on the canvas — this is what makes **picture-in-picture**,
**split-screen**, and **off-center framing** possible. Put a clip on a
track above another, scale it down, and move it into a corner.

| Control | Range | Default |
|---|---|---|
| Position X / Y | 0–1 | 0.5, 0.5 (dead center) |
| Scale | 0.1x–3.0x | 1.0x |
| Rotation | -180°–180° | 0° |

**Reset Transform** puts all four back to default in one click. At the
defaults, a clip looks exactly as it would without ever touching
Transform.

*Note: Transform has no effect on a clip while it's crossfading into the
next one — see [Crossfade transitions](#crossfade-transitions).*

## Keyframing (animating opacity and position)

Opacity and Transform's Position X/Y can be **animated over time** instead
of held at one constant value:

1. Select a video/image clip and set the slider (Opacity, or Position
   X/Y) to the value you want at a specific moment.
2. In the **Keyframes** group, set **At** to that moment's time (in
   seconds, from the clip's own start), then click **+ Add**.
3. Repeat for other moments. With 2 or more keyframes, the value smoothly
   animates between them instead of staying constant. **- Remove
   Selected** deletes a keyframe from the table.

This is a simple, table-based tool (not an on-timeline curve editor), but
it's enough for common moves like a fade, a slide-in, or a pan.

## Speed ramps

Beyond one constant Speed, a clip can **change speed partway through** —
e.g. normal speed, then slow-motion, then back to normal:

1. Select a video/image clip and set **Speed** to the value you want at a
   given moment.
2. In the Keyframes group's **Speed Ramp** section, set **At** to that
   moment (in seconds from the *source* file's start, not the timeline),
   and click **+ Add**.
3. Repeat for each speed change.

Speed changes happen as a **step** at each keyframe (an instant change,
not a gradually accelerating ramp) — for a normal→slow-mo→normal beat,
this reads as a deliberate cut in pace rather than a smooth blend.

Chroma key, color correction, and fades/opacity all still work normally
on a speed-ramped clip. Transform's scale/rotation and crossfade
transitions do **not** currently combine with a speed ramp on the same
clip.

## Crossfade transitions

To blend one clip into the next instead of a hard cut:

1. Select a clip that's immediately followed (no gap) by another
   video/image clip on the *same* track.
2. In the **Transition** group, check **Crossfade into next clip** and
   set a **Duration**.

Notes:
- Only one crossfade per pair of clips — chaining three or more
  consecutive crossfades on one track isn't supported.
- The audio doesn't crossfade with it — it cuts cleanly at the original
  boundary while the video blends.
- The transition shortens that pair's combined on-screen time; nothing
  after it on the track automatically shifts left to close the resulting
  gap — drag the next clip over manually if you want it snug.

## Color correction

For video/image clips, the **Color Correction** group offers Brightness,
Contrast, Saturation, and Gamma sliders — standard color grading controls,
applied at export time.

| Control | Range | Neutral |
|---|---|---|
| Brightness | -1.0 – 1.0 | 0.0 |
| Contrast | 0.0 – 2.0 | 1.0 |
| Saturation | 0.0 – 3.0 | 1.0 |
| Gamma | 0.1 – 3.0 | 1.0 |

## Chroma key (green screen)

For footage shot against a solid-color background:

1. Select the clip.
2. In the **Chroma Key** group, check **Enable chroma key**, pick the
   **Color** to remove (defaults to green), and adjust **Similarity**
   (how close a pixel needs to be to count as a match) and **Softness**
   (edge feathering).

The keyed-out background becomes transparent, letting whatever's on the
track beneath show through — the same way Transform lets you build
picture-in-picture or split-screen layouts.

## Text overlays

1. Press **T**, click **Add Text**, or use **Edit > Add Text Clip**. This
   drops a 4-second text clip onto a video track at the playhead and
   selects it.
2. In the Properties panel, edit the **Content**, **Font Size**, **Color**,
   **Bold**/**Italic**, position, and alignment.
3. Trim and move it on the timeline like any other clip to control when it
   appears and for how long.

*Note: the live preview shows text as a plain centered label rather than
composited over your video — the exported file always shows it correctly
overlaid at the position/style you set.*

## Shorts / vertical video export

Built for exporting a 9:16 vertical video for YouTube Shorts, TikTok, or
Instagram Reels, without the ugly black bars ("letterboxing") a plain
resize would produce.

- **Fill Frame** (Properties panel, **Reframe** group): instead of fitting
  the whole frame inside the vertical canvas with black bars, this scales
  the clip to *fill* the canvas and crops the overflow — cropped centered
  by default, or on whatever point you drag **Crop Focus X/Y** to (so a
  person standing to one side of a wide shot doesn't get cropped out of a
  narrow one).
- **Auto-fill vertical frame** (Export dialog): applies Fill Frame to
  *every* clip for this export only, without changing each clip's own
  saved setting — handy for a one-off vertical export of a project you
  otherwise want to keep in its original aspect ratio.
- **Shorts Platform preset** (Export dialog): pick YouTube Shorts, TikTok,
  or Instagram Reels to instantly set 1080x1920 resolution, 30fps,
  auto-fill, and that platform's typical duration cap in one click.
  Picking "None" leaves your settings untouched.
- **Trim Range / duration cap**: check **Limit duration** and set a Start/
  End (in seconds) to export only part of your timeline — useful on its
  own or alongside a Shorts preset's automatic cap.

Markers/chapters still work correctly even on a trimmed, reframed export.

> This feature is meant for your own footage, or footage you're properly
> licensed to use. It does not, and will not, include anything aimed at
> republishing or monetizing someone else's copyrighted material, or at
> evading a platform's copyright-detection systems — no export setting
> makes that non-infringing.

## The preview player — what it does and doesn't show

Press **Space**, or click the play button, to preview your edit. Click the
ruler or drag the playhead to scrub.

**Important**: the preview intentionally shows a *simplified* version of
your project, not a frame-accurate render:

- It shows only **one** clip at a time — the topmost non-hidden video
  track's clip if one is playing, otherwise the first audio track's clip.
- It does **not** show multiple video tracks composited together, text
  drawn over video, crossfade transitions, fades, or opacity.

**The exported file is always the real, fully composited result** — every
track, transition, text overlay, fade, and opacity setting included. The
preview trades some accuracy for a simple, fast, reliable playback
experience; a small note in the preview panel reminds you of this.

## Project files, autosave, and Recent Projects

Your edit — every track, clip, marker, and setting — is saved as a
`.vproj` file (plain, human-readable JSON). This is your *editable
project*, not a rendered video; export separately when you want an actual
video file.

- **Save** (**Ctrl+S**) / **Save As...** (**Ctrl+Shift+S**).
- **Open Project...** (**Ctrl+O**) — you're warned first if you have
  unsaved changes, and afterward if any of the project's media can't be
  found.
- **New Project** (**Ctrl+N**) resets to an empty two-track project.
- **Open Recent** (**File** menu) lists your last 10 opened/saved
  projects, most recent first — picking one behaves just like **Open
  Project...**. A project whose file has since been deleted shows grayed
  out as "(missing)" rather than just disappearing from the list.
- **Autosave**: while you have unsaved changes, the app automatically
  saves a recovery snapshot every 2 minutes (separately from your actual
  project file — autosaving never overwrites what you deliberately
  saved). If the app closes unexpectedly, you'll be offered to recover
  that snapshot the next time you open it.
- Note: only the *paths* to your media files are saved, not the media
  itself — if you move or delete a source file, clips referencing it will
  appear blank/silent until it's restored at its original location.

## Exporting your video

**File > Export Video...** (**Ctrl+E**) renders your full, composited
timeline to a real video file via FFmpeg.

1. Choose the output location and container format: MP4, MOV, MKV, AVI,
   FLV, WebM, or animated GIF.
2. Pick a resolution (a preset, or Custom), frame rate, and quality.
3. *(Optional)* Check **Two-pass, target file size** and enter a target
   size in MB instead of a quality preset — useful when you need to hit a
   specific file-size limit (e.g. for an upload cap).
4. *(Optional)* Set up vertical/Shorts export settings — see
   [above](#shorts--vertical-video-export).
5. Click **Export**. You'll be asked to confirm if the output file already
   exists. Progress is shown live, and **Cancel** stops the render
   immediately if you change your mind.

Animated GIF export uses a dedicated high-quality palette technique (not
FFmpeg's default, which looks visibly banded) and has no audio track,
since GIFs can't hold sound — the Quality and Two-pass controls are
disabled and explained when GIF is selected.

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New Project | Ctrl+N |
| Open Project | Ctrl+O |
| Save Project | Ctrl+S |
| Save Project As | Ctrl+Shift+S |
| Import Media | Ctrl+I |
| Export Video | Ctrl+E |
| Undo / Redo | Ctrl+Z / Ctrl+Y |
| Play / Pause | Space |
| Split clip at playhead | S |
| Delete selected clip | Delete |
| Add text clip | T |
| Add marker at playhead | M |
| Zoom timeline in / out | Ctrl+= / Ctrl+- (or Ctrl+scroll) |

(**Open Recent** and **Clear Recent Projects** are menu-only — no
keyboard shortcut.)

## Tips, limits, and things to know

- **The preview is not what you'll get frame-for-frame** — always trust
  the exported file for transitions, text-over-video, fades, and opacity;
  see [above](#the-preview-player--what-it-does-and-doesnt-show).
- **Ripple editing only affects the track you're editing** — other tracks
  never shift, so a ripple delete/trim can't accidentally desync audio and
  video that live on different tracks.
- **Speed ramps change speed in steps, not a smooth curve** — plan for a
  deliberate pacing cut at each speed-keyframe, not a gradual
  acceleration.
- **There's no automatic subject tracking** for vertical/Shorts reframing
  — Crop Focus X/Y is a manual point you set once per clip, not something
  that follows a moving subject.
- **Undo/redo covers every edit** (add/move/trim/split/delete a clip, add/
  remove a track, any property change) — don't be afraid to experiment.
- **Every panel can be resized but never fully collapsed** — if a panel
  looks squeezed to almost nothing, drag its divider back out; it's still
  there.
- **Your window size and panel layout are remembered** between sessions,
  so you don't have to re-arrange things every time you open the app.

## Troubleshooting

**"FFmpeg was not found" on startup.** Media import, thumbnails, preview,
and export all need FFmpeg. Install it from
[ffmpeg.org](https://ffmpeg.org/download.html) (or `winget install ffmpeg`
on Windows) and make sure it's on your system `PATH`, then restart the
app. If you're running the packaged `.exe`, placing `ffmpeg.exe`/
`ffprobe.exe` next to it also works.

**A clip in my project looks blank or plays silent.** Its source media
file has probably moved or been deleted since it was imported — restore
the file to its original path, or re-import it and rebuild that part of
the timeline.

**I can't fit a clip where I want it to go.** Tracks don't allow
overlapping clips by design — trim, split, or move the clip in the way
first, or turn on **Ripple** so a delete/trim automatically shifts later
clips instead of leaving a gap.

**Export failed or the app closed unexpectedly.** Check for an autosave
recovery prompt the next time you open the app — your edit up to the last
autosave (every 2 minutes while you have unsaved changes) should still be
there.

**I accidentally dragged a panel divider and now a panel looks tiny.**
Drag the same divider back — panels can be made very small, but the app
won't let one disappear completely.
