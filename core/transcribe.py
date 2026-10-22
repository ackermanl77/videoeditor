"""Automatic captions (F17): speech to text with the optional faster-whisper package.

Edit > Generate Captions transcribes the selected clip's own trimmed audio. FFmpeg extracts it as
16 kHz mono WAV, and faster-whisper's ``WhisperModel`` (GPU when CUDA is there, otherwise CPU int8)
transcribes it with word timestamps and voice-activity filtering. The words are grouped into caption
cues of at most two lines of ~42 characters and ~6 seconds, mapped through the clip's own speed and
speed ramp onto the timeline, and the editor adds them as ordinary caption text clips on a
"Subtitles" track. Styling, Apply to Track, soft-subtitle export and burn-in then work as for an
imported SRT.

F33: ``style="words"`` makes one cue per word instead (the punchy short-video look), each lasting
until the next word starts (or ``WORD_HOLD_SECONDS`` at most across a pause); ``translate=True``
uses Whisper's own speech translation into English (the same model, no extra download).

faster-whisper isn't a requirement: without it the command explains how to add it. Its models are
downloaded by faster-whisper itself on first use into the Hugging Face cache (tiny ~75 MB,
base ~145 MB, small ~480 MB, medium ~1.5 GB, large-v3 ~3 GB).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from core.clip import Clip, MediaType
from core.ffmpeg_utils import find_binary
from core.subtitles import SubtitleCue

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL = "small"
MODEL_DOWNLOAD = {"tiny": "75 MB", "base": "145 MB", "small": "480 MB", "medium": "1.5 GB", "large-v3": "3 GB"}
LANGUAGES = (("Detect automatically", None), ("English", "en"), ("Spanish", "es"), ("French", "fr"),
             ("German", "de"), ("Italian", "it"), ("Portuguese", "pt"), ("Dutch", "nl"), ("Japanese", "ja"),
             ("Chinese", "zh"), ("Korean", "ko"), ("Russian", "ru"))
MAX_LINE_CHARS = 42
MAX_CUE_CHARS = 2 * MAX_LINE_CHARS
MAX_CUE_SECONDS = 6.0
#: A pause this long between words always starts a new cue.
CUE_GAP_SECONDS = 0.8
CAPTION_STYLES = (("Two-line captions", "lines"), ("One word at a time", "words"))
#: A single-word cue stays this long at most after its word when the speaker pauses.
WORD_HOLD_SECONDS = 0.4
MIN_WORD_SECONDS = 0.15
INSTALL_HINT = ("Automatic captions need the optional faster-whisper package. In the editor's Python "
                "environment run:\n\n    pip install faster-whisper\n\nThe first transcription then "
                "downloads the chosen speech model.")


class TranscriptionCancelled(Exception):
    pass


@dataclass(frozen=True)
class Word:
    start: float  # seconds into the transcribed audio
    end: float
    text: str


def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def transcribable_problem(clip: Optional[Clip]) -> Optional[str]:
    if clip is None or clip.media_type not in (MediaType.VIDEO, MediaType.AUDIO):
        return "Select a video or audio clip with speech first."
    if clip.reverse:
        return "The clip plays backwards: captions need it playing forwards."
    return None


def audio_args(ffmpeg: str, source: str, source_in: float, source_out: float, wav_path: str) -> List[str]:
    return [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, source_in):.3f}",
            "-t", f"{max(0.0, source_out - source_in):.3f}", "-i", source, "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000", "-f", "wav", wav_path]


def _wrap(text: str) -> str:
    """At most two lines, broken at the space nearest the middle when longer than one line."""
    text = " ".join(text.split())
    if len(text) <= MAX_LINE_CHARS:
        return text
    middle = len(text) // 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    cut = min(spaces, key=lambda i: abs(i - middle))
    return text[:cut] + "\n" + text[cut + 1:]


def word_cues(words: Sequence[Word]) -> List[SubtitleCue]:
    """One cue per word (F33): from its start until the next word starts -- held at most
    WORD_HOLD_SECONDS past the word's end across a pause, and never shorter than MIN_WORD_SECONDS."""
    spoken = [w for w in words if w.text.strip()]
    cues = []
    for i, word in enumerate(spoken):
        end = word.end + WORD_HOLD_SECONDS
        if i + 1 < len(spoken):
            end = min(end, spoken[i + 1].start)  # cues never overlap
        end = max(end, word.start + MIN_WORD_SECONDS)
        cues.append(SubtitleCue(word.start, end, word.text.strip()))
    return cues


def group_words(words: Sequence[Word], style: str = "lines") -> List[SubtitleCue]:
    """Caption cues (times in seconds of the audio) from timed words: a new cue when the text would
    pass two lines, the cue would pass MAX_CUE_SECONDS, or the speaker pauses. ``style="words"``:
    one cue per word (word_cues)."""
    if style == "words":
        return word_cues(words)
    cues: List[SubtitleCue] = []
    current: List[Word] = []

    def flush():
        if current:
            cues.append(SubtitleCue(current[0].start, current[-1].end,
                                    _wrap(" ".join(w.text.strip() for w in current))))
            current.clear()

    for word in words:
        if not word.text.strip():
            continue
        if current:
            text = " ".join(w.text.strip() for w in current + [word])
            if (len(text) > MAX_CUE_CHARS or word.end - current[0].start > MAX_CUE_SECONDS
                    or word.start - current[-1].end > CUE_GAP_SECONDS):
                flush()
        current.append(word)
    flush()
    return cues


def _whisper_words(wav_path: str, model_size: str, language: Optional[str],
                   should_cancel: Optional[Callable[[], bool]], translate: bool = False) -> Iterable[Word]:
    from faster_whisper import WhisperModel  # optional dependency

    try:
        model = WhisperModel(model_size, device="auto", compute_type="auto")
    except Exception:  # noqa: BLE001 -- a broken CUDA setup: fall back to the CPU
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav_path, language=language, word_timestamps=True, vad_filter=True,
                                       task="translate" if translate else "transcribe")
    for segment in segments:
        if should_cancel is not None and should_cancel():
            raise TranscriptionCancelled()
        words = getattr(segment, "words", None) or []
        if words:
            for w in words:
                yield Word(float(w.start), float(w.end), str(w.word))
        elif segment.text.strip():
            yield Word(float(segment.start), float(segment.end), str(segment.text))


def transcribe_clip(clip: Clip, model_size: str = DEFAULT_MODEL, language: Optional[str] = None,
                    should_cancel: Optional[Callable[[], bool]] = None,
                    words_from: Optional[Callable[[str], Iterable[Word]]] = None,
                    ffmpeg: Optional[str] = None, style: str = "lines", translate: bool = False,
                    on_words: Optional[Callable[[List[list]], None]] = None) -> List[SubtitleCue]:
    """Caption cues for ``clip``'s trimmed audio, in timeline seconds. ``words_from(wav_path)``
    replaces faster-whisper (tests). ``on_words`` receives the words as ``[start, end, word]`` in
    seconds of the clip's source file (F37: the media bin's speech search)."""
    from core.scene_detect import timeline_offset_for_source

    problem = transcribable_problem(clip)
    if problem:
        raise ValueError(problem)
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    handle, wav = tempfile.mkstemp(prefix="ve_captions_", suffix=".wav")
    os.close(handle)
    try:
        made = subprocess.run(audio_args(ffmpeg, clip.source_path, clip.source_in, clip.source_out, wav),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if made.returncode != 0 or os.path.getsize(wav) <= 44:
            raise RuntimeError("The clip's audio couldn't be read (does it have a sound track?). "
                               + made.stderr.strip()[-300:])
        if words_from is None:
            words = list(_whisper_words(wav, model_size, language, should_cancel, translate))
        else:
            words = list(words_from(wav))
        if should_cancel is not None and should_cancel():
            raise TranscriptionCancelled()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    if on_words is not None:
        on_words([[clip.source_in + w.start, clip.source_in + w.end, w.text.strip()] for w in words if w.text.strip()])
    cues = []
    for cue in group_words(words, style):
        start = timeline_offset_for_source(clip, min(clip.source_out, clip.source_in + cue.start))
        end = timeline_offset_for_source(clip, min(clip.source_out, clip.source_in + cue.end))
        if start is None or end is None or end <= start:
            continue
        cues.append(SubtitleCue(clip.timeline_start + start, clip.timeline_start + end, cue.text))
    return cues
