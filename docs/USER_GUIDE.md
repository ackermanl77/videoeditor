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
18. [Effects, audio automation and editing tools](#effects-audio-automation-and-editing-tools)
19. [The preview player — what it does and doesn't show](#the-preview-player--what-it-does-and-doesnt-show)
20. [Project files, autosave, and Recent Projects](#project-files-autosave-and-recent-projects)
21. [Exporting your video](#exporting-your-video)
22. [Keyboard shortcuts](#keyboard-shortcuts)
23. [Tips, limits, and things to know](#tips-limits-and-things-to-know)
24. [Troubleshooting](#troubleshooting)

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

**If you just want to use the app** (Windows): download
`VideoEditor-<version>-setup.exe` from the project's
[Releases page](https://github.com/ackermanl77/videoeditor/releases) and run
it. It installs for your user only (no administrator rights needed), adds
Open Video Editor to the Start menu, and makes double-clicking a `.vproj`
project open it in the editor. Uninstall it from Windows Settings > Apps.
No Python installation is needed. If you'd rather not install anything,
download `VideoEditor-<version>-portable.zip` instead, unzip it anywhere and
run `VideoEditor.exe` from that folder (keep the `_internal` folder next to
it). Both include FFmpeg, so nothing else needs installing. (The repository
is private, so the Releases page needs a GitHub account with access to it;
the installer and portable zip come from the current release workflow, while
the older `v0.1.0` release only has a single `VideoEditor.exe`.)

**If you're running it from source:**

1. Install [Python 3.10+](https://www.python.org/) and
   [FFmpeg](https://ffmpeg.org/download.html) (with `ffprobe`), and make
   sure `ffmpeg`/`ffprobe` are on your system `PATH`. On Windows you can
   instead run `python scripts/fetch_ffmpeg.py`, which downloads the exact
   FFmpeg build the app is tested with into the project's `bin\` folder
   (the app looks there first).
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
│  Menu bar (File · Edit · Timeline · Playback · Media · Help) │
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

The **Render Queue** (**File > Render Queue**, **Ctrl+Shift+E**) opens as an
extra panel when you need it.

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
  project, red-hatched and marked "(offline)", until you restore the file or
  point the project at its new location with **File > Relink Media...**
  (see [Offline media and relinking](#effects-audio-automation-and-editing-tools)).
- **Subtitle files** (`.srt`, `.vtt`) picked here or dropped on the window
  become caption clips — see [Subtitles](#subtitles-srt--webvtt).
- Large batches can be stopped with **Media > Cancel Import**.

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
  timeline toolbar (or the **Timeline** menu); click a track's header (or
  select one of its clips) and click **Delete Track** to remove it.
- **Rename / reorder tracks**: double-click a track's header (or right-click
  it > **Rename Track...**) to rename it; right-click > **Move Track Up** /
  **Move Track Down** moves it among tracks of its own kind. Video tracks
  higher in the list are drawn on top, so moving one up brings its picture
  in front of the tracks below. Both are undoable.
- **Track height**: right-click a track's header > **Track Height** >
  **Small** / **Medium** / **Large** to make its row shorter or taller (a
  taller row shows more of a clip's waveform or filmstrip). Saved with the
  project and undoable.
- **Mute / Lock / Hide a track**: click a track header's **M** / **L** /
  **H** buttons. A locked track's clips can't be dragged or trimmed; a
  hidden video track's picture is left out of the preview and the export,
  but its sound is kept (mute it to silence it). The **D** button sets up
  ducking (see [Ducking settings](#effects-audio-automation-and-editing-tools)).
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
**Ctrl+-** (or **Ctrl+scroll**), to zoom the timeline in or out around the
playhead; **Shift+Z** (Zoom to Fit) shows the whole edit.

**Moving around.** Drag on the ruler to scrub. The **Playback** menu lists the
transport keys: **J** / **K** / **L** shuttle backwards / stop / forwards
(press J or L again to go faster), **Left**/**Right** step one frame,
**Shift+Left**/**Shift+Right** one second, **Home**/**End** jump to the start
or end, **Up**/**Down** to the previous/next cut, **Shift+M** /
**Ctrl+Shift+M** to the previous/next marker. **I** and **O** set In and Out
points (**Alt+X** clears them); the Export dialog's Trim Range starts from
them. Times are shown as `HH:MM:SS:FF` timecode.

**Clip look.** Video clips show a filmstrip of frames along the clip and audio
shows its waveform, drawn at the clip's volume: it follows the volume
automation, is flat when the clip is muted, and peaks that the volume pushes past
full scale turn red (they will clip). Lettered badges mark what's applied to a clip (hover one
for its name), and a small glyph at the end marks a transition out. When
that transition can't be applied -- no clip right after it (a gap, or the end of
the track), a next clip of another kind, a speed ramp on either side, clips too
short for it, or a clip that already ends the previous transition -- the glyph
turns into a solid red cross with a "!", and hovering the clip says why.

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
  tracks. **Left-click** to seek there; **right-click** to **Rename**, pick a
  **Colour** (blue, red, orange, yellow, green, purple, pink or gray -- to sort
  chapters, cuts and notes at a glance; undoable and saved with the project) or
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
| Reduce noise (afftdn) | on/off, strength | Removes steady background hiss/hum from the clip's audio (an **N** badge marks the clip). |
| Speed | 0.25x–4.0x | Slow motion to 4x fast-forward (also changes how much of the source maps to the timeline). |
| Opacity | 0–100% | How transparent the clip is (video/image only; not shown in the live preview, only in the exported file). |
| Fade In / Fade Out | 0–30s | A gradual fade at the start/end — applied to video and to the clip's own audio. |
| Reverse playback | on/off | Plays the clip back to front — picture and sound (video and audio clips). |
| Freeze Frame | 0–30s | Makes a video clip longer by holding its last frame for this long, in silence. |

Dragging a slider previews live; releasing it commits one single undo
step for the whole drag. Everything here is undoable with **Ctrl+Z**.

**Reverse and freeze frame in detail**

- A reversed clip starts on the last frame of its trimmed range and ends on
  the first. Its **left** edge is now the *end* of the source: dragging the
  left edge in cuts the end of the footage, dragging the right edge in cuts
  the beginning. Splitting a reversed clip gives two reversed halves that
  together still play the same frames in the same order.
- A freeze frame is shown hatched at the end of the clip and adds an **FF**
  badge (a reversed clip shows **R**). Dragging the clip's right edge in
  shortens the freeze first, then the footage; the freeze ends up on the
  second half when you split the clip. You can't split *inside* a freeze
  (the timeline tells you so) — shorten it with the right edge instead.
  On a reversed clip the held frame is the first frame of the source range.
- A freeze that would run into the next clip is refused, like a speed change
  that would.
- The sound stops where the freeze starts, and a Fade Out on the audio ends
  there too; the picture's Fade Out runs over the held frame.
- To reverse a clip FFmpeg holds all of its frames in memory while exporting,
  so keep reversed clips short (a few seconds of HD is fine; minutes of 4K
  are not).

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

1. Select a clip that's immediately followed (no gap) by another clip on
   the *same* track.
2. In the **Transition** group, pick a **Transition Out** — **Crossfade**,
   **Wipe** (Left/Right/Up/Down), **Slide** (Left/Right/Up/Down), **Circle
   Crop** or **Dissolve** — and set a **Duration** (0.1–10 s).

Notes:
- The sound crossfades over the same moment, with an equal-power curve (no
  dip in the middle); on audio clips, Transition Out gives that audio
  crossfade on its own.
- Only one transition per pair of clips — chaining three or more
  consecutive transitions on one track isn't supported.
- The second clip starts the transition duration early, so the pair ends
  that much sooner than the timeline shows; nothing after it on the track
  shifts left to close the resulting gap — drag the next clip over manually
  if you want it snug.
- The duration is limited to what both clips can provide.

## Color correction

For video/image clips, the **Color Correction** group offers Brightness,
Contrast, Saturation, and Gamma sliders — standard color grading controls,
applied at export time. The sliders edit a **Color Correction** effect in
the clip's effect stack (it appears in the Effects list after your first
change), so it can be reordered with other effects, switched off, and
**animated**: in the Effects list, each of its values has a **◆** button
(see [Effects, audio automation and editing tools](#effects-audio-automation-and-editing-tools)).

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
picture-in-picture or split-screen layouts. Like Color Correction, this group
edits an effect in the clip's stack (**Chroma Key (RGB)**). Projects saved by
earlier versions are converted automatically when opened and look the same;
once saved, they need this version (or newer) to open.

## Text overlays

1. Press **T**, click **Add Text**, or use **Edit > Add Text Clip**. This
   drops a 4-second text clip onto a video track at the playhead and
   selects it.
2. In the Properties panel, edit the **Content**, **Font**, **Font Size**
   (scales with the export resolution), **Color**, **Outline** (width and
   colour), **Shadow**, **Background box**, **Bold**/**Italic**, position,
   and alignment.
3. Trim and move it on the timeline like any other clip to control when it
   appears and for how long.

*Note: during playback the preview shows text as a plain centered label
rather than composited over your video; pause with **Accurate frame** ticked
to see it exactly as exported.*

### Subtitles (SRT / WebVTT / ASS)

**File > Import Subtitles...** (or drop an `.srt`/`.vtt`/`.ass`/`.ssa` file on the window
or pick it in Import Media) turns every cue into a text clip at its own time,
centred near the bottom of the frame on a translucent box, on a video track
named **Subtitles** (a second import that would overlap goes to
**Subtitles 2**, and so on). The whole import is one undo step.

- They are ordinary text clips: restyle, retime, edit or delete them like any
  other, and they are saved in the project file.
- **On export**, the Export dialog's **Subtitles** setting decides what happens
  to every track named "Subtitles...": **Burn into the picture** (the
  default: drawn like any title), **Embed as a subtitle track** (a soft
  subtitle stream viewers switch on and off -- MP4, MOV, MKV and WebM only;
  the captions are left out of the picture), or **Save as .srt next to the
  video** (`clip.mp4` gets `clip.srt`; also left out of the picture). An
  export trimmed to a range shifts the captions to match. Embedded and `.srt`
  captions carry the text and timing only, not the clip's font or box.
- **Styled ASS/SSA subtitles.** Importing an `.ass`/`.ssa` file asks whether
  to **Keep Styling** or import **Plain Text**. With Keep Styling, every line
  becomes a text clip that remembers its original line, and the export draws
  them with the file's own fonts, colours, outlines, positions and effects
  (libass), exactly as the subtitle file was made. Lines that overlap in time
  (a sign at the top while someone speaks at the bottom) go on extra tracks
  (**Subtitles 2**...) instead of being merged. You can still move, trim,
  split and delete them; editing a line's text keeps its style but drops that
  line's inline effects. The editor's own preview shows them as plain
  captions -- tick **Accurate frame** to see the styled result while paused.
  Burned in, embedded in an **MKV** (as a real ASS track) or saved next to the
  video (as `.ass`) they keep their look; MP4, MOV and WebM can only carry
  their text. In an HDR export they'd look much too bright (the export warns).
- Accepted: SRT, WebVTT and ASS/SSA (as plain text: its dialogue lines, without
  styles, positions or effects), UTF-8 or UTF-16 with a byte-order mark, or
  Windows-1252 (common for older SRT files). Formatting tags (`<i>`, `{\an8}`
  and similar) are removed. Cues that overlap are shortened so each ends when
  the next starts (cues starting at the same moment are merged into one
  two-line caption).
- A file that isn't a subtitle file shows a message and changes nothing.

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

## Effects, audio automation and editing tools

**Effects stack** (Properties > **Effects**, video and image clips): pick an
effect, click **+ Add**, then edit its settings below the list. Effects run
top to bottom; **Up**/**Down** reorder them and **Enabled** switches one off
without losing its settings. Available: **LUT (.cube)** for camera-log to
Rec.709 or creative looks, **Chroma Key (YUV) + Despill** (cleaner edges than
the older RGB Chroma Key group), **Sharpen**, **Gaussian Blur**, **Vignette**,
**Video Denoise**, **Color Correction**, **Chroma Key (RGB)** and
**Stabilize**. A clip with effects shows an **FX** badge (colour correction
and the RGB key keep their **C**/**K** badges). A LUT file that has gone
missing stops the export with a clear message.

**Stabilize** smooths out camera shake (vid.stab). The first export of a
stabilized clip first analyses its motion ("Analyzing motion for
stabilization" in the progress line); the result is remembered, so later
exports and changes to **Smoothing**, **Zoom** or **Borders** don't repeat
it (changing **Shakiness** or **Accuracy**, or the clip's in/out points, does).
**Zoom to hide borders** enlarges the picture just enough to hide the edges
the correction exposes; with it off, **Borders** chooses black edges or the
previous frame's. The preview's **Accurate frame** shows the stabilized
picture once an export has analysed the clip.

**Animating effect settings**: settings with a **◆** button next to them
(Color Correction's four values and Vignette's strength) can change over the
clip. Set the value, put the playhead on the clip and click **◆** to add a
keyframe there; move the playhead, change the value and click **◆** again.
With two or more keyframes the value glides between them; the label next to
the button counts them and **Clear** removes them. Other settings (keys,
LUTs, blur, sharpen, denoise, stabilization) can't be animated, because FFmpeg
applies them with one value for the whole clip.

**Keyframes at the playhead**: the **◆** buttons next to Volume, Speed and
Opacity, and **◆ Key Position at Playhead** in Transform, add or update a
keyframe at the playhead from the control's current value. You no longer type
times by hand, and speed keyframes are converted to source time for you.
Keyframes show as small yellow diamonds on the clip. **Shift+K** /
**Ctrl+Shift+K** jump to the previous/next keyframe of the selected clip.

**Volume automation**: two or more volume keyframes draw a volume envelope
that overrides the Volume slider (a **V** badge marks the clip). **Clear**
removes them.

**Audio crossfades**: set **Transition Out** on an audio clip that touches the
next one and the two crossfade with an equal-power curve (no dip in the
middle). As with video transitions, the second clip starts the transition
duration early. Video crossfades now use the same equal-power audio curve.

**Ducking settings**: the track header's **D** button (or right-click > Duck
Audio Under) now also sets threshold, ratio, attack and release.

**Selecting several clips**: **Ctrl+click** adds or removes a clip; drag on
empty timeline space to draw a selection box; **Ctrl+A** selects everything;
**Tab**/**Shift+Tab** select the next/previous clip. Dragging one selected
clip moves the whole selection; **Alt+Left/Right** nudges it by one frame;
**Delete** removes all of it (with Ripple on, gaps close on each track).

**Clipboard**: **Ctrl+C**, **Ctrl+X**, **Ctrl+V** (pastes at the playhead,
on a free track), **Ctrl+D** (duplicate right after the selection),
**Ctrl+Alt+V** (paste attributes: colour, keying, transform, effects and audio
settings from the copied clip). Right-click a clip for all of these plus
Split, Ripple Delete, Detach Audio, Roll, Slip, Slide and Reveal in Media Bin.

**Editing tools**:

- **Insert** (**,**) puts the media selected in the Media Bin at the playhead
  and pushes everything after it on every unlocked track to the right.
  **Overwrite** (**.**) replaces whatever is under it instead.
- **Detach Audio** (**Ctrl+Shift+D**) moves a video clip's sound to an audio
  track as its own clip, linked to the picture: they move together, but you
  can trim them separately (J/L cuts).
- **Roll Edit to Playhead** (**Ctrl+R**) moves the cut between the selected
  clip and the one after it; **Slip** changes which part of the source a clip
  shows without moving it; **Slide** moves a clip between its neighbours,
  trimming them to compensate; **Ripple Move** moves a clip together with
  everything after it on its track.
- The same edits work by **dragging with a modifier key**:

  | Drag | With | Does |
  |---|---|---|
  | A clip's left or right edge | **Ctrl** | **Roll**: the cut between this clip and the one touching that edge moves |
  | Inside a clip | **Alt** | **Slip**: the clip stays put, its content shifts (drag right to show earlier footage) |
  | Inside a clip | **Ctrl+Alt** | **Slide**: the clip moves, its neighbours trim to keep the cut closed |
  | Inside a clip | **Shift** | **Ripple Move**: the clip and every later clip on its track move together (linked audio follows) |

  Each drag is one undo step. If it isn't possible (no touching neighbour,
  not enough footage, it would overlap), nothing changes and the status bar
  says why. **Ctrl+click** inside a clip still adds it to or removes it from
  the selection.
- **Razor Tool** (**C**): click a clip to cut it there. Press **C** again to
  turn it off.

**Offline media and relinking**: clips whose file is missing get red hatching
and "(offline)". **File > Relink Media...** lists missing files: **Locate
File...** for one (other missing files in the same folder are then found
automatically) or **Search Folder...** for all. Matches are checked by
duration and frame size, and the relink is one undo step. Projects also store
media paths relative to the project file, so moving a project folder together
with its media keeps working.

**HDR and colour**: by default HDR (HLG/PQ) footage is tone-mapped to
standard BT.709 on export, SD footage is converted to the HD colour matrix,
and every export is tagged BT.709, so it looks the same in every player. The
Media Bin marks HDR and 10-bit files. To keep HDR footage in HDR, or to get a
10-bit file, use the Export dialog's **Color** setting (see
[Exporting your video](#exporting-your-video)).

**Preview proxies**: **Media > Generate Proxy for Selected Media** (or
right-click a Media Bin item) makes a small, fast-seeking copy that the
preview plays instead of the original; export always uses the original.
**Media > Generate Proxies Automatically** does this for every imported file
larger than 1080p, 10-bit or HDR. Proxies use the NVIDIA encoder when there is
one.

## The preview player — what it does and doesn't show

Press **Space**, or click the play button, to preview your edit. Click the
ruler or drag the playhead to scrub.

**Important**: playback shows a *simplified* version of your project:

- It plays only **one** clip at a time — the topmost non-hidden video
  track's clip if one is playing, otherwise the first audio track's clip.
- It does **not** show multiple video tracks composited together, text
  drawn over video, crossfade transitions, fades, or opacity.

**Accurate frame**: tick **Accurate frame** under the preview to see the exact
exported frame (every track, transitions, text, keyframes, effects) whenever
playback is paused or you scrub. It is rendered by the export engine at
preview size, so it takes a moment to appear; playback itself stays the fast,
simplified view.

When a cut between two different files is coming up, the preview opens the
next file in advance, so cuts no longer pause briefly.

**Reversed clips and freeze frames** play in the preview by stepping through
the frames (the player can only play forwards), so they look a little less
smooth there and are **silent** in the preview; the exported file has the
reversed sound. **Accurate frame** shows them exactly.

**Level meters**: the small bar next to **Safe areas** shows the sound level
of the clip under the playhead — red for the peak, green for the average
(RMS) level. It reads the waveform made at import, so it costs nothing while
playing; it shows the active clip only (not the mix of every track), takes the
clip's volume into account, and drops to zero for silence, a muted clip or
track, a freeze frame, or media without sound (and until the waveform of a
just-imported file is ready).

**Loudness**: after the level meters, `M`, `S` and `I` show the loudness of
the clip under the playhead in LUFS (EBU R128, what streaming services
normalise to): **M** over the last 0.4 s, **S** over the last 3 s, and **I**
for everything that has played since you pressed Play. They include the clip's
volume (and its volume keyframes). Each file is measured once in the
background the first time you play it, so the meter fills in after a moment.
Like the level bars it follows the active clip, not the mix of every track;
**Export > Measure** gives the exact figure for the whole mix.

**Render Preview**: for a stretch that's too heavy to play live (several
tracks, transitions, text, effects), set In/Out points and pick **Playback >
Render Preview (In/Out)** (`Ctrl+Return`; without In/Out it renders the whole
timeline). It renders in the background, small and fast, with the export
engine; when it's done a **green bar** on the ruler marks the range, and the
preview plays the render there -- exactly what the export will look and sound
like. Edit anything in that range and the bar turns **red**: the preview goes
back to the simplified view until you render again (undoing the edit makes the
old render valid again). **Cancel Preview Render** stops one in progress.

**Safe areas** draws the action-safe (outer, 90 %) and title-safe (inner,
80 %) rectangles over the preview, to keep titles and captions away from the
edges.

**Transform** (next to Safe areas) edits the selected video or image clip right
on the preview while the playhead is over it: its box shows where it sits on
the frame; drag inside it to move the clip, drag a corner to scale it. Each drag
is one undo step and updates the Transform sliders in Properties. Rotation is
shown but set in Properties; a clip with keyframed position can only be scaled
here (edit its keyframes for the path), and Fill-frame clips have no box.

**The exported file is always the real, fully composited result** — every
track, transition, text overlay, fade, and opacity setting included.

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
  saved), keeping the last few snapshots. Each open editor window has its
  own snapshots, so two editors never overwrite each other's, and you're
  only offered recovery for an editor that actually closed unexpectedly.
- **Backups**: each Save keeps the previous version of the file as
  `<name>.vproj.bak` next to it.
- **Unsaved-changes star**: the `*` in the title follows Undo, so undoing
  back to what you saved clears it.
- **Newer and older files**: a project made by a newer version of the app is
  refused with a message naming that version, rather than opened with parts
  silently dropped. Older projects are upgraded on open. Anything the app
  can't read (an unknown transition, say) is replaced by a safe default and
  listed once after opening, along with any overlapping clips.
- Note: only the *paths* to your media files are saved, not the media
  itself (both the full path and the path relative to the project file). If
  a file moves, its clips show as offline until you relink it (**File >
  Relink Media...**, see [above](#effects-audio-automation-and-editing-tools)).

## Exporting your video

**File > Export Video...** (**Ctrl+E**) renders your full, composited
timeline to a real video file via FFmpeg.

**File > Export Frame...** (**Ctrl+Alt+E**) saves the single frame under the
playhead as a PNG or JPEG still -- composited exactly as the export renders it
(every track, text, effects, transitions), at the project's full resolution.

1. *(Optional)* Pick a **Preset**: YouTube 1080p, YouTube 4K (HEVC),
   Shorts/Reels/TikTok, Archive (ProRes) or Animated GIF, or one you saved
   with **Save...**. The dialog also remembers your last export's settings,
   and starts at the resolution and frame rate of your main footage.
2. Choose the output location and container format: MP4, MOV, MKV, AVI,
   FLV, WebM, or animated GIF, then the **Video Codec**: H.264, HEVC and AV1
   (smaller files at the same quality), or ProRes 422 HQ / DNxHR HQ (large,
   edit-friendly intermediates, in MOV/MKV).
3. Pick a resolution (a preset, or Custom), frame rate, and quality.
   23.976, 29.97 and 59.94 are the exact rates most cameras record; use them
   for that footage to avoid judder.
4. *(Optional)* Pick the **Color** format. **8-bit SDR (BT.709)** is the
   default and plays everywhere. With HEVC, AV1, ProRes or DNxHR you can also
   choose **10-bit SDR** (smoother gradients, less banding), and when your
   timeline has HDR footage, **10-bit HDR10 (PQ)** or **10-bit HDR HLG** —
   whichever your HDR footage uses — to keep it in HDR. In an HDR export,
   ordinary (SDR) clips and titles are placed at HDR "reference white", so
   they look normal next to the HDR footage rather than glaringly bright.
   Only the choices your codec, your computer and your footage support are
   listed; with an NVIDIA card that can't encode 10-bit, the CPU encoder is
   used. For HDR10 (PQ), the CPU HEVC encoder (GPU encoding off) also writes
   the footage's mastering-display and brightness metadata; GPU and AV1
   exports carry the HDR colour tags only. Check HDR exports on an HDR
   screen or TV.
5. *(Optional)* **Use GPU encoding (NVENC) when available** (on by default
   when an NVIDIA encoder is found) renders H.264/HEVC/AV1 on the graphics
   card; if the GPU encode fails, the export is redone on the CPU
   automatically. Check **Two-pass, target file size** and enter a target
   size in MB instead of a quality preset — useful when you need to hit a
   specific file-size limit (e.g. for an upload cap). With an NVIDIA GPU,
   **Target size on the GPU** does this in one fast pass that lands close to
   the size; leave it off to hit the size exactly.
6. *(Optional)* Audio: pick the **Audio Bitrate**, and a **Loudness** target
   (-14 / -16 / -23 LUFS) to normalise the whole mix to a platform's level
   (this measures the audio first, so the export takes a little longer). A
   limiter always keeps the mix from clipping (peaks stay below -1 dBFS).
   **Measure** next to it reads the mix this export would render (same trim
   range) without rendering the picture: integrated loudness (LUFS), true
   peak (dBTP) and loudness range (LU), and how far it is from the chosen
   target.
7. *(Optional)* Set up vertical/Shorts export settings — see
   [above](#shorts--vertical-video-export).
8. Click **Export**. You'll be asked to confirm if the output file already
   exists. Progress shows the render speed and time left. The editor stays
   usable while it renders (it exports the project as it was when you clicked
   Export). **Cancel**, **Esc** or closing the dialog stops the render.
   Or click **Add to Queue** to render it later (below).

**Render queue**: **Add to Queue** puts the export in the **Render Queue**
panel (**File > Render Queue**, **Ctrl+Shift+E**) instead of rendering it in
the dialog. Each queued export remembers the project exactly as it was when
you queued it, so you can queue several versions (say a 4K master, a 1080p
upload and a vertical cut), keep editing, and let them render one after
another in the background. The panel shows each job's status, progress and
time left. Select a job to **Cancel** it (running or waiting), **Remove** it,
move a waiting job **Up**/**Down**, **Retry** a failed or cancelled one, or
**Show File** when it's done. Quitting while jobs are waiting asks whether to
cancel them.

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
| Show / hide the Render Queue | Ctrl+Shift+E |
| Exit | Ctrl+Q |
| Undo / Redo | Ctrl+Z / Ctrl+Y |
| Play / Pause | Space |
| Shuttle back / stop / forward | J / K / L |
| Previous / next frame (timeline or preview focused) | Left / Right |
| Back / forward 1 second | Shift+Left / Shift+Right |
| Go to start / end | Home / End |
| Previous / next edit point | Up / Down |
| Previous / next marker | Shift+M / Ctrl+Shift+M |
| Set In / Out point, clear both | I / O, Alt+X |
| Render Preview of In/Out | Ctrl+Return |
| Split clip at playhead | S |
| Delete selected clip | Delete |
| Add text clip | T |
| Add marker at playhead | M |
| Zoom timeline in / out | Ctrl+= / Ctrl+- (or Ctrl+scroll) |
| Zoom to fit | Shift+Z |
| Copy / Cut / Paste at playhead | Ctrl+C / Ctrl+X / Ctrl+V |
| Duplicate / Paste attributes | Ctrl+D / Ctrl+Alt+V |
| Select all clips | Ctrl+A |
| Select next / previous clip (timeline) | Tab / Shift+Tab |
| Edit the selected clip's properties (timeline) | Enter |
| Nudge selection one frame (timeline) | Alt+Left / Alt+Right |
| Insert / Overwrite selected media at playhead | , / . |
| Detach audio | Ctrl+Shift+D |
| Roll edit to playhead | Ctrl+R |
| Roll / slip / slide / ripple move by dragging | Ctrl+drag an edge / Alt+drag / Ctrl+Alt+drag / Shift+drag |
| Razor tool on/off | C |
| Previous / next keyframe | Shift+K / Ctrl+Shift+K |
| Keyboard shortcut list | F1 |

**Help > Keyboard Shortcuts** (**F1**) lists every shortcut, generated from
the app itself. **Help > User Guide** opens this guide. (**Open Recent** and
**Clear Recent Projects** are menu-only — no keyboard shortcut.)

## Tips, limits, and things to know

- **Playback is not what you'll get frame-for-frame** — pause with
  **Accurate frame** ticked to check transitions, text-over-video, fades and
  opacity; see [above](#the-preview-player--what-it-does-and-doesnt-show).
- **Hover a clip** for a description (type, name, start and end, track,
  effects, linked/offline); hover a track-header button for what it does and
  whether it's on.
- **Screen readers** (Narrator, NVDA, JAWS) can read the timeline: it is
  presented as a list of tracks ("Video track Video 1, 3 clips, muted"), each
  listing its clips by name, start and end time, with effects and offline
  status as the description. **Tab**/**Shift+Tab** moves to the next/previous
  clip and announces it; **Enter** jumps to its properties.
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
- **GPU decoding is opt-in.** Pick **Playback > GPU Decoding > Auto** (or
  D3D11VA / CUDA for just one method; remembered between sessions) -- or set
  the environment variable `VIDEO_EDITOR_HWACCEL` before starting the app,
  which then takes priority and locks the menu -- to let FFmpeg decode video on the graphics card during
  export, Accurate frame, proxies and thumbnails. If the GPU decoder fails,
  the same job runs again with normal (software) decoding, so it can only cost
  time, never a failed export. It mostly helps with heavy 4K HEVC/AV1 camera
  footage on a slower CPU; on a fast CPU it can even be slower (each frame
  has to be copied back from the GPU), which is why it is off by default.
  `python main.py --self-test` shows the current setting.
  **Per source** decides for each file instead: the first time a video file
  is imported (or a project opens) it is decoded briefly both ways in the
  background, and only files the GPU decodes clearly faster use it. The
  result is remembered per file.

## Troubleshooting

**"FFmpeg was not found" on startup.** Media import, thumbnails, preview,
and export all need FFmpeg. Install it from
[ffmpeg.org](https://ffmpeg.org/download.html) (or `winget install ffmpeg`
on Windows) and make sure it's on your system `PATH`, then restart the
app. If you're running the packaged `.exe`, placing `ffmpeg.exe`/
`ffprobe.exe` next to it also works.

**A clip in my project looks blank or plays silent.** Its source media
file has probably moved or been deleted since it was imported (the clip is
red-hatched and marked "(offline)") — restore the file, or use **File >
Relink Media...** to point the project at its new location.

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
