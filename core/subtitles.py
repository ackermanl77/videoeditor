"""Subtitles: parse SRT/WebVTT/ASS files into text clips, and write them back out as SRT.

Imported subtitles become ordinary, editable text clips on a "Subtitles"
video track (see ``ui/main_window.py::import_subtitle_file``), saved in the
project file like any other clip. By default they are burned into the
picture by the same ``drawtext`` path every text overlay uses; the export's
subtitle mode can instead mux them as a soft subtitle stream or write a
sidecar ``.srt`` (:func:`subtitle_cues_for_export`, :func:`format_srt`).

ASS/SSA files can be imported two ways. As plain cues (text and timing),
like SRT. Or styled (:func:`split_ass_script`, :func:`styled_subtitle_tracks`):
the script's header -- info, styles, embedded fonts -- is stored once in
``Project.subtitle_scripts`` and every ``Dialogue`` line becomes a text clip
carrying its original line (:class:`core.clip.AssEvent`); the export writes
those clips back into an ASS script at their current timeline times
(:func:`build_ass_script`) and renders it with libass (FFmpeg's ``ass``
filter), so fonts, colours, outlines, positions and effects come out as
authored. Overlapping lines go on extra "Subtitles" tracks instead of being
merged. Stdlib only, like the rest of ``core``.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
import uuid
from typing import Dict, Iterable, List, Optional, Tuple

from core.clip import AssEvent, Clip, MediaType, TextStyle

#: Subtitle file extensions the importer accepts.
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})

#: Name prefix of the video tracks imported subtitles go on.
SUBTITLE_TRACK_PREFIX = "Subtitles"

#: Export subtitle modes: burn into the picture, mux as a soft subtitle
#: stream, or write a sidecar .srt next to the video.
SUBTITLE_MODES = ("burn", "embed", "sidecar")
SUBTITLE_MODE_LABELS = (
    ("Burn into the picture", "burn"),
    ("Embed as a subtitle track", "embed"),
    ("Save as a subtitle file next to the video (.srt, or .ass when styled)", "sidecar"),
)

#: Soft-subtitle codec per container (``-c:s``); a container missing here
#: (AVI, FLV, GIF) can't carry a text subtitle stream.
SUBTITLE_CODECS = {".mp4": "mov_text", ".mov": "mov_text", ".mkv": "srt", ".webm": "webvtt"}

# HH:MM:SS,mmm (SRT) or [HH:]MM:SS.mmm (WebVTT; hours optional, any number
# of hour digits). A comma or a dot before the milliseconds is accepted in
# both, since hand-made files mix them up.
_TIMECODE_RE = re.compile(
    r"(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2})[,.](?P<ms>\d{1,3})"
)
# WebVTT blocks that are not cues.
_VTT_NON_CUE_BLOCKS = ("NOTE", "STYLE", "REGION")
# Markup a cue's text may carry: HTML-like tags (<i>, <b>, <font ...>, WebVTT
# <c.class>/<v Speaker>/<00:00:01.000>) and ASS override blocks ({\an8}).
_TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")

#: Shortest cue kept (seconds); a clip needs some length to be selectable.
MIN_CUE_SECONDS = 0.05


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


class SubtitleParseError(ValueError):
    """The subtitle file could not be parsed as SRT or WebVTT."""


def _parse_timestamp(raw: str) -> float:
    match = _TIMECODE_RE.fullmatch(raw.strip())
    if not match:
        raise SubtitleParseError(f"Invalid subtitle timestamp: {raw!r}")
    ms = match.group("ms").ljust(3, "0")
    return (
        int(match.group("h") or 0) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(ms) / 1000.0
    )


def _normalize_text(lines: Iterable[str]) -> str:
    text = "\n".join(line.rstrip() for line in lines).strip()
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _ass_text(raw: str) -> str:
    """An ASS Dialogue text field as plain text: override blocks removed,
    ``\\N``/``\\n`` line breaks and ``\\h`` hard spaces converted."""
    text = re.sub(r"\{[^}]*\}", "", raw)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def parse_ass_text(text: str) -> List[SubtitleCue]:
    """Cues of an ASS/SSA script: every ``Dialogue:`` line of ``[Events]``,
    read through that section's ``Format:`` line (Start, End, Text). Styles,
    positions and effects are not kept. Raises :class:`SubtitleParseError`
    when there is no usable ``[Events]`` section."""
    fields: List[str] = []
    in_events = False
    cues: List[SubtitleCue] = []
    for line in text.replace("\ufeff", "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().lower()
        if key == "format":
            fields = [f.strip().lower() for f in value.split(",")]
        elif key == "dialogue" and fields:
            # Text is the last field and may itself contain commas.
            parts = value.split(",", len(fields) - 1)
            if len(parts) < len(fields):
                continue
            row = dict(zip(fields, (p.strip() if f != "text" else p for f, p in zip(fields, parts))))
            try:
                start, end = _parse_timestamp(row["start"]), _parse_timestamp(row["end"])
            except KeyError:
                raise SubtitleParseError("ASS Format line has no Start/End field")
            cue_text = _ass_text(row.get("text", ""))
            if cue_text and end > start:
                cues.append(SubtitleCue(start=start, end=end, text=cue_text))
    if not fields:
        raise SubtitleParseError("No [Events] Format line found in the ASS/SSA file")
    cues.sort(key=lambda cue: cue.start)
    return cues


def parse_subtitle_text(text: str) -> List[SubtitleCue]:
    """Parse ``text`` as SRT, WebVTT or ASS/SSA and return its cues, ordered by start time.

    Tolerates the usual real-world variations: a BOM, CRLF line ends,
    WebVTT headers with a title (``WEBVTT - Episode 1``), NOTE/STYLE/REGION
    blocks, text cue identifiers, cue settings after the end time, missing
    hours (``01:02.500``), and markup tags. Cues with no text or no length
    are skipped, as are stray blocks without a time range (a blank line
    inside a cue's text). Raises :class:`SubtitleParseError` for a bad
    timestamp, or when nothing in the text looks like a cue.
    """
    raw = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    if raw.lstrip().lower().startswith("[script info]") or "\n[events]" in raw.lower():
        return parse_ass_text(raw)
    blocks = [block.split("\n") for block in re.split(r"\n[ \t]*\n", raw) if block.strip()]
    if blocks and blocks[0][0].strip().upper().startswith("WEBVTT"):
        # The header block: "WEBVTT[ title]" plus optional metadata lines
        # ("Kind: captions"). Keep the rest only if a cue follows the header
        # without the blank line the format asks for.
        rest = blocks[0][1:]
        timing = next((i for i, line in enumerate(rest) if "-->" in line), None)
        blocks = ([rest[max(0, timing - 1):]] if timing is not None else []) + blocks[1:]

    cues: List[SubtitleCue] = []
    stray: List[str] = []
    for lines in blocks:
        first = lines[0].strip()
        if first.upper().split(" ")[0] in _VTT_NON_CUE_BLOCKS:
            continue
        timing_index = next((i for i, line in enumerate(lines[:2]) if "-->" in line), None)
        if timing_index is None:
            # E.g. a blank line inside an SRT cue's text: skip the stray
            # block, but a file with no cue at all is not a subtitle file.
            stray.append(first)
            continue
        start_raw, end_raw = (part.strip().split()[0] if part.strip() else "" for part in
                              lines[timing_index].split("-->", 1))
        start = _parse_timestamp(start_raw)
        end = _parse_timestamp(end_raw)
        cue_text = _normalize_text(lines[timing_index + 1:])
        if cue_text and end > start:
            cues.append(SubtitleCue(start=start, end=end, text=cue_text))

    if stray and not cues:
        raise SubtitleParseError(f"Expected subtitle time range, got: {stray[0]!r}")
    cues.sort(key=lambda cue: cue.start)  # stable: cues starting together keep file order
    return cues


def read_subtitle_file(path: str) -> str:
    """The file's text: UTF-8 (with or without BOM), else UTF-16 with BOM, else Windows-1252.

    Many SRT files in the wild are not UTF-8; failing the import on the
    first accented character would be worse than a rare mis-decoded glyph.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_subtitle_file(path: str) -> List[SubtitleCue]:
    return parse_subtitle_text(read_subtitle_file(path))


def _without_overlaps(cues: List[SubtitleCue]) -> List[SubtitleCue]:
    """End each cue no later than the next one starts (clips on one track can't overlap).

    Cues that start together are merged into one two-line cue, so no text
    is lost; a cue cut down to less than :data:`MIN_CUE_SECONDS` is dropped.
    """
    merged: List[SubtitleCue] = []
    for cue in cues:
        if merged and abs(merged[-1].start - cue.start) < 1e-6:
            last = merged[-1]
            merged[-1] = SubtitleCue(last.start, max(last.end, cue.end), f"{last.text}\n{cue.text}")
        else:
            merged.append(cue)
    out: List[SubtitleCue] = []
    for i, cue in enumerate(merged):
        end = cue.end
        if i + 1 < len(merged):
            end = min(end, merged[i + 1].start)
        if end - cue.start >= MIN_CUE_SECONDS:
            out.append(SubtitleCue(cue.start, end, cue.text))
    return out


def cues_to_text_clips(cues: Iterable[SubtitleCue], *, name_prefix: str = "Subtitle") -> List[Clip]:
    """Convert subtitle cues into editable text clips for one video track.

    Overlapping cues are shortened/merged first (see :func:`_without_overlaps`);
    each clip shows its cue as centred, boxed caption text near the bottom.
    """
    clips: List[Clip] = []
    ordered = sorted((c for c in cues if c.text.strip()), key=lambda c: c.start)
    for index, cue in enumerate(_without_overlaps(ordered), start=1):
        clips.append(
            Clip(
                media_type=MediaType.TEXT,
                source_in=0.0,
                source_out=max(MIN_CUE_SECONDS, cue.end - cue.start),
                timeline_start=max(0.0, cue.start),
                name=f"{name_prefix} {index}",
                text_style=TextStyle(
                    content=cue.text.strip(),
                    font_size=44,
                    position_x=0.5,
                    position_y=0.86,
                    alignment="center",
                    box_enabled=True,
                    box_opacity=0.35,
                    box_padding=10,
                ),
            )
        )
    return clips


def is_subtitle_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUBTITLE_EXTENSIONS


def is_subtitle_track(track) -> bool:
    """A video track the subtitle importer made (or one named like it):
    named "Subtitles...", holding text clips only."""
    from core.track import TrackType  # local: core.track imports core.clip like this module

    return (
        track.track_type == TrackType.VIDEO
        and track.name.startswith(SUBTITLE_TRACK_PREFIX)
        and bool(track.clips)
        and all(c.media_type == MediaType.TEXT for c in track.clips)
    )


def subtitle_cues_for_export(project, trim_start: float = 0.0, trim_end: Optional[float] = None) -> List[SubtitleCue]:
    """The visible subtitle tracks' text clips as cues on the output's own
    timeline: clipped to the ``trim_start``..``trim_end`` window and
    re-anchored to it, ordered by start. Hidden tracks are left out."""
    end_limit = float("inf") if trim_end is None else trim_end
    cues: List[SubtitleCue] = []
    for track in project.tracks:
        if track.hidden or not is_subtitle_track(track):
            continue
        for clip in track.sorted_clips():
            text = (clip.text_style.content if clip.text_style else "").strip()
            start = max(clip.timeline_start, trim_start)
            end = min(clip.timeline_end, end_limit)
            if text and end - start >= MIN_CUE_SECONDS:
                cues.append(SubtitleCue(start=start - trim_start, end=end - trim_start, text=text))
    cues.sort(key=lambda cue: cue.start)
    return cues


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_srt(cues: Iterable[SubtitleCue]) -> str:
    """SRT text for ``cues`` (numbered from 1, a blank line between cues)."""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(f"{index}\n{format_srt_timestamp(cue.start)} --> {format_srt_timestamp(cue.end)}\n{cue.text}\n")
    return "\n".join(blocks)


def sidecar_subtitle_path(output_path: str) -> str:
    """Where a sidecar subtitle file for ``output_path`` goes: same name, ``.srt``."""
    return os.path.splitext(output_path)[0] + ".srt"


# ---------------------------------------------------------------------------
# Styled ASS/SSA (rendered with libass)
# ---------------------------------------------------------------------------

#: The Events format every exported script uses (libass reads fields by name,
#: so a v4/SSA header with this v4+ line still works).
ASS_EVENTS_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"

# ASS colour &HAABBGGRR (alpha first, 00 = opaque) or &HBBGGRR.
_ASS_COLOUR_RE = re.compile(r"&H([0-9A-Fa-f]{6,8})&?")
_ALIGNMENT_OVERRIDE_RE = re.compile(r"\\an([1-9])")
# Legacy SSA \a / style alignment values -> numpad (1-3 bottom, 5-7 top, 9-11 middle).
_LEGACY_ALIGNMENT = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}


@dataclass(frozen=True)
class AssDialogue:
    start: float
    end: float
    text: str  # the raw Text field, override tags included
    style: str = "Default"
    layer: int = 0
    name: str = ""
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0
    effect: str = ""


@dataclass(frozen=True)
class AssScript:
    header: str  # every section but [Events], ending with a newline
    dialogues: List[AssDialogue]
    styles: Dict[str, Dict[str, str]]  # style name -> its fields (lower-case keys)
    play_res_y: int


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return default


def split_ass_script(text: str) -> AssScript:
    """An ASS/SSA script's header (kept verbatim for libass), its ``Dialogue`` lines with every
    field, and its styles. Raises :class:`SubtitleParseError` without an ``[Events]`` Format line."""
    header_lines: List[str] = []
    dialogues: List[AssDialogue] = []
    styles: Dict[str, Dict[str, str]] = {}
    section = ""
    event_fields: List[str] = []
    style_fields: List[str] = []
    play_res_y = 0
    for line in text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            if section != "[events]":
                header_lines.append(stripped)
            continue
        if section != "[events]":
            header_lines.append(line.rstrip())
            if ":" not in stripped:
                continue
            key, value = (part.strip() for part in stripped.split(":", 1))
            if section == "[script info]" and key.lower() == "playresy":
                play_res_y = _int(value)
            elif section in ("[v4+ styles]", "[v4 styles]"):
                if key.lower() == "format":
                    style_fields = [f.strip().lower() for f in value.split(",")]
                elif key.lower() == "style" and style_fields:
                    row = dict(zip(style_fields, (p.strip() for p in value.split(",", len(style_fields) - 1))))
                    styles[row.get("name", "Default")] = row
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().lower()
        if key == "format":
            event_fields = [f.strip().lower() for f in value.split(",")]
        elif key == "dialogue" and event_fields:
            parts = value.split(",", len(event_fields) - 1)
            if len(parts) < len(event_fields):
                continue
            row = dict(zip(event_fields, (p.strip() if f != "text" else p for f, p in zip(event_fields, parts))))
            try:
                start, end = _parse_timestamp(row["start"]), _parse_timestamp(row["end"])
            except KeyError:
                raise SubtitleParseError("ASS Format line has no Start/End field")
            if end <= start:
                continue
            dialogues.append(AssDialogue(
                start=start, end=end, text=row.get("text", ""), style=row.get("style", "Default") or "Default",
                layer=_int(row.get("layer", "0")), name=row.get("name", ""),
                margin_l=_int(row.get("marginl", "0")), margin_r=_int(row.get("marginr", "0")),
                margin_v=_int(row.get("marginv", "0")), effect=row.get("effect", ""),
            ))
    if not event_fields:
        raise SubtitleParseError("No [Events] Format line found in the ASS/SSA file")
    header = "\n".join(header_lines).strip() + "\n"
    dialogues.sort(key=lambda d: d.start)
    # libass's default when the script doesn't say (the classic SSA canvas).
    return AssScript(header=header, dialogues=dialogues, styles=styles, play_res_y=play_res_y or 288)


def _preview_style(dialogue: AssDialogue, script: AssScript, plain: str) -> TextStyle:
    """An approximation of the line's look as a TextStyle -- what the editor's own preview and the
    timeline show (the export renders the real thing with libass)."""
    style = script.styles.get(dialogue.style) or script.styles.get("Default") or {}
    scale = 1080.0 / script.play_res_y
    alignment = _int(style.get("alignment", "2"), 2)
    if "marginl" in style and style.get("alignment") and "[v4 styles]" in script.header.lower():
        alignment = _LEGACY_ALIGNMENT.get(alignment, 2)
    override = _ALIGNMENT_OVERRIDE_RE.search(dialogue.text)
    if override:
        alignment = int(override.group(1))
    row, column = (alignment - 1) // 3, (alignment - 1) % 3  # row 0 = bottom, 2 = top
    colour = "#FFFFFF"
    match = _ASS_COLOUR_RE.fullmatch(style.get("primarycolour", "").strip())
    if match:
        bgr = match.group(1)[-6:]
        colour = f"#{bgr[4:6]}{bgr[2:4]}{bgr[0:2]}".upper()
    return TextStyle(
        content=plain,
        font_family=style.get("fontname", "Arial") or "Arial",
        font_size=max(8, int(round(_int(style.get("fontsize", "20"), 20) * scale))),
        color=colour,
        outline_width=max(0, int(round(_int(style.get("outline", "2"), 2) * scale))),
        bold=style.get("bold", "0").strip() not in ("0", ""),
        italic=style.get("italic", "0").strip() not in ("0", ""),
        position_x=(0.2, 0.5, 0.8)[column],
        position_y=(0.88, 0.5, 0.12)[row],
        alignment=("left", "center", "right")[column],
    )


def styled_subtitle_tracks(script: AssScript, script_id: str) -> List[List[Clip]]:
    """The script's lines as styled text clips, spread over as few tracks as keep them from
    overlapping (a sign at the top while someone speaks at the bottom needs two). One list of
    clips per track, the first being the main "Subtitles" track."""
    tracks: List[List[Clip]] = []
    track_ends: List[float] = []
    for index, dialogue in enumerate(script.dialogues, start=1):
        plain = _ass_text(dialogue.text) or "(styled subtitle)"
        clip = Clip(
            media_type=MediaType.TEXT,
            source_in=0.0,
            source_out=max(MIN_CUE_SECONDS, dialogue.end - dialogue.start),
            timeline_start=max(0.0, dialogue.start),
            name=f"Subtitle {index}",
            text_style=_preview_style(dialogue, script, plain),
            ass_event=AssEvent(
                script_id=script_id, text=dialogue.text, plain=plain, style=dialogue.style, layer=dialogue.layer,
                name=dialogue.name, margin_l=dialogue.margin_l, margin_r=dialogue.margin_r,
                margin_v=dialogue.margin_v, effect=dialogue.effect,
            ),
        )
        slot = next((i for i, end in enumerate(track_ends) if end <= clip.timeline_start + 1e-6), None)
        if slot is None:
            tracks.append([])
            track_ends.append(0.0)
            slot = len(tracks) - 1
        tracks[slot].append(clip)
        track_ends[slot] = clip.timeline_end
    return tracks


def new_script_id() -> str:
    return uuid.uuid4().hex[:12]


def format_ass_timestamp(seconds: float) -> str:
    """H:MM:SS.cc, ASS's own time format (centiseconds)."""
    total_cs = max(0, int(round(seconds * 100)))
    hours, rest = divmod(total_cs, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_line_text(clip: Clip) -> str:
    """The Text field to write for a styled clip: its original raw text while the clip text is
    unedited, else the edited text (line breaks as \\N; braces, which would start override
    blocks, as parentheses)."""
    event = clip.ass_event
    content = clip.text_style.content if clip.text_style else ""
    if event is not None and content == event.plain:
        return event.text
    return content.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass_script(header: str, clips: Iterable[Clip], offset: float = 0.0, end: Optional[float] = None) -> str:
    """An ASS script: ``header`` plus one Dialogue line per styled clip at its current timeline
    time, clipped to ``offset``..``end`` and shifted so ``offset`` is 0 (a trimmed export)."""
    end_limit = float("inf") if end is None else end
    lines = [header.rstrip("\n"), "", "[Events]", ASS_EVENTS_FORMAT]
    rows: List[Tuple[float, str]] = []
    for clip in clips:
        event = clip.ass_event
        if event is None:
            continue
        start = max(clip.timeline_start, offset)
        stop = min(clip.timeline_end, end_limit)
        if stop - start < 0.01:
            continue
        fields = [
            str(event.layer), format_ass_timestamp(start - offset), format_ass_timestamp(stop - offset), event.style,
            event.name, str(event.margin_l), str(event.margin_r), str(event.margin_v), event.effect,
        ]
        rows.append((start, "Dialogue: " + ",".join(f.replace(",", " ") for f in fields) + "," + _ass_line_text(clip)))
    lines.extend(row for _start, row in sorted(rows, key=lambda r: r[0]))
    return "\n".join(lines) + "\n"


def styled_clips_by_script(project, clips: Iterable[Clip]) -> Dict[str, List[Clip]]:
    """``clips``' styled ones whose script the project still has, grouped by script id (in first-seen order)."""
    grouped: Dict[str, List[Clip]] = {}
    for clip in clips:
        event = clip.ass_event
        if event is not None and event.script_id in project.subtitle_scripts:
            grouped.setdefault(event.script_id, []).append(clip)
    return grouped


def styled_script_for_export(project, trim_start: float = 0.0, trim_end: Optional[float] = None) -> Optional[str]:
    """The visible subtitle tracks as one styled ASS script, for an embedded ASS stream or a
    sidecar ``.ass``: only when every subtitle clip is a styled line of the same script (a mix
    can't share one header -- those exports fall back to plain SRT)."""
    clips = [c for t in project.tracks if not t.hidden and is_subtitle_track(t) for c in t.clips]
    grouped = styled_clips_by_script(project, clips)
    if len(grouped) != 1 or sum(len(v) for v in grouped.values()) != len(clips):
        return None
    script_id, styled = next(iter(grouped.items()))
    return build_ass_script(project.subtitle_scripts[script_id], styled, trim_start, trim_end)
