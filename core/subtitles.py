"""Subtitles: parse SRT/WebVTT/ASS files into text clips, and write them back out as SRT.

Imported subtitles become ordinary, editable text clips on a "Subtitles"
video track (see ``ui/main_window.py::import_subtitle_file``), saved in the
project file like any other clip. By default they are burned into the
picture by the same ``drawtext`` path every text overlay uses; the export's
subtitle mode can instead mux them as a soft subtitle stream or write a
sidecar ``.srt`` (:func:`subtitle_cues_for_export`, :func:`format_srt`).
ASS/SSA files are read for their text and timing only -- their styles are
not kept (no libass). Stdlib only, like the rest of ``core``.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

from core.clip import Clip, MediaType, TextStyle

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
    ("Save as .srt next to the video", "sidecar"),
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
