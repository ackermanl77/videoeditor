# Text Overlays Workflow

## Goal

Add a text clip to the timeline and style its content, font, color, position, and alignment.

## Steps

1. Press **T**, click **Add Text** in the toolbar, or use **Edit > Add Text Clip**. This adds a 4-second `TEXT`-type clip to the last video track (or a new one if none exists) at the current playhead position, selects it, and opens it in the Properties panel.
2. Edit the **Content** text, **Font Size**, **Color** (via the color picker), **Bold**/**Italic**, horizontal/vertical position sliders, and alignment.
3. Trim/move the text clip on the timeline like any other clip to control its timing.

## How it renders

- **Preview**: the topmost active clip at a given time takes priority over text, video, and image clips uniformly (`PreviewPlayer._resolve_active_clip()`) — if a text clip is the topmost clip at the playhead, its content is shown as a centered label in the preview area rather than composited over the video underneath. This is a simplification, not what the final export produces.
- **Export**: text clips are rendered as FFmpeg `drawtext` filters, composited on top of every video layer (`core/filter_graph.py`, the "Text overlays" section, applied after all video-track compositing). Position, color, outline, and bold are all mapped directly to `drawtext` options; the clip only draws during its own timeline span (`enable='between(t,start,end)'`).

## Escaping

Text content is escaped for safe use inside the `drawtext` filter string (`core/filter_graph.py::_escape_text()`): backslashes, colons, single quotes, and `%` (which `drawtext` would otherwise treat as a `strftime`-style expansion token) are all escaped.

## Validation

`tests/test_filter_graph.py::FilterGraphTests::test_text_clip_generates_drawtext` confirms a text clip produces a `drawtext` filter containing its content; `TextEscapingTests::test_escapes_special_characters` confirms the escaping above. Verify visually by exporting a short project with an overlapping text + video clip and confirming the text appears in the rendered file at the right time and position — the preview intentionally won't show this composite (see [preview-playback/workflow.md](../preview-playback/workflow.md)).
