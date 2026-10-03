"""Subtitle import: parse SRT/WebVTT files and turn their cues into text clips.

Imported subtitles become ordinary, editable text clips on a "Subtitles"
video track (see ``ui/main_window.py::import_subtitle_file``), so they are
burned into the picture by the same ``drawtext`` path every text overlay
uses -- no libass dependency, no sidecar file, and they are saved in the
project file like any other clip. Stdlib only, like the rest of ``core``.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from typing import Iterable, List

from core.clip import Clip, MediaType, TextStyle

#: Subtitle file extensions the importer accepts.
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})

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


def parse_subtitle_text(text: str) -> List[SubtitleCue]:
    """Parse ``text`` as SRT or WebVTT and return its cues, ordered by start time.

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
