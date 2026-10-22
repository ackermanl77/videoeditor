"""Search media by what is said (F37).

Generate Captions keeps the words it recognised, with their times in the
SOURCE file (seconds from the file's start), on the clip's media asset
(``MediaAsset.transcript``: ``[start, end, word]``). Searching the media bin for
a phrase finds it across every asset -- accents, case and punctuation ignored --
and a hit jumps the playhead to where a timeline clip plays that moment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

CONTEXT_WORDS = 6
MAX_WORDS = 200_000


def normalize(word: str) -> str:
    text = unicodedata.normalize("NFKD", word.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w']+", "", text)


def clean_transcript(value) -> List[list]:
    """A project file's transcript made safe: ``[start, end, word]`` with start <= end, sorted."""
    out = []
    for item in value if isinstance(value, list) else []:
        try:
            start, end, word = float(item[0]), float(item[1]), str(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if word.strip() and start >= 0 and end >= start:
            out.append([round(start, 3), round(end, 3), word.strip()])
    out.sort(key=lambda w: w[0])
    return out[:MAX_WORDS]


def merge_transcript(existing: Sequence[list], words: Sequence[list]) -> List[list]:
    """``existing`` with the span ``words`` covers replaced by ``words`` (re-transcribing part of a file)."""
    if not words:
        return clean_transcript(existing)
    start, end = min(w[0] for w in words), max(w[1] for w in words)
    kept = [w for w in existing if w[1] < start or w[0] > end]
    return clean_transcript(kept + [list(w) for w in words])


@dataclass
class TranscriptHit:
    asset_id: str
    asset_name: str
    start: float  # source seconds
    end: float
    snippet: str


def search(assets: Iterable, query: str, limit: int = 100) -> List[TranscriptHit]:
    """Every place in the assets' transcripts where the words of ``query`` are said in a row."""
    terms = [normalize(t) for t in query.split()]
    terms = [t for t in terms if t]
    if not terms:
        return []
    hits: List[TranscriptHit] = []
    for asset in assets:
        words = getattr(asset, "transcript", None) or []
        normalized = [normalize(w[2]) for w in words]
        for i in range(len(words) - len(terms) + 1):
            if all(normalized[i + k] == term or (k == len(terms) - 1 and normalized[i + k].startswith(term))
                   for k, term in enumerate(terms)):
                first, last = max(0, i - CONTEXT_WORDS), min(len(words), i + len(terms) + CONTEXT_WORDS)
                snippet = " ".join(w[2] for w in words[first:last])
                hits.append(TranscriptHit(asset.id, asset.display_name, words[i][0], words[i + len(terms) - 1][1],
                                          ("… " if first else "") + snippet + (" …" if last < len(words) else "")))
                if len(hits) >= limit:
                    return hits
    return hits


def timeline_time_for(project, asset_id: str, source_time: float) -> Optional[float]:
    """The timeline time where a clip of ``asset_id`` plays ``source_time``, or None if none does."""
    from core.scene_detect import timeline_offset_for_source

    for track in project.tracks:
        for clip in track.clips:
            if getattr(clip, "asset_id", None) != asset_id:
                continue
            low, high = min(clip.source_in, clip.source_out), max(clip.source_in, clip.source_out)
            if low - 1e-6 <= source_time <= high + 1e-6:
                local = timeline_offset_for_source(clip, source_time)
                if local is not None:
                    return clip.timeline_start + local
    return None
