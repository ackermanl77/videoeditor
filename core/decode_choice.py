"""Per-source GPU decoding (PG2): time each video file's decode once with
software and with every GPU decoder, and remember the faster one.

Whether the GPU helps depends on the file, not just its codec: on the RTX
5080 a synthetic 4K HEVC decoded *slower* through D3D11VA/NVDEC than on the
CPU (download to system memory included), so a codec/resolution rule would
guess. The "Per source" GPU Decoding mode measures instead: the file's first
video stream is decoded to ``-f null`` with each decoder, and the time for
``MEASURE_FRAMES`` frames past a short warm-up is compared (set-up costs
cancel out, see measure_decoders); a GPU decoder is chosen only when it is
at least ``GPU_MARGIN`` faster than software. Choices are cached in
``~/.video_editor/decode_choices.json`` keyed by path, size and modification
time (a replaced file is measured again), and
:func:`core.ffmpeg_utils.apply_decode_hwaccel` then puts ``-hwaccel`` only
before the inputs whose measured winner is a GPU decoder.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Dict, Iterable, List, Optional

from core.ffmpeg_utils import find_binary, run

MEASURE_FRAMES = 120
WARMUP_FRAMES = 15
GPU_MARGIN = 0.9  # a GPU decoder must take at most 90 % of software's time
MEASURE_TIMEOUT_S = 60.0
SOFTWARE = "software"

_lock = threading.Lock()
_memory: Optional[Dict[str, dict]] = None


def choices_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".video_editor", "decode_choices.json")


def gpu_candidates() -> List[str]:
    return ["d3d11va", "cuda"] if sys.platform.startswith("win") else ["cuda"]


def source_key(path: str) -> Optional[str]:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|{stat.st_mtime_ns}"


def _load() -> Dict[str, dict]:
    global _memory
    if _memory is None:
        try:
            with open(choices_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _memory = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _memory = {}
    return _memory


def forget_cached_choices() -> None:
    """Drop the in-memory copy (tests, or after the file changed on disk)."""
    global _memory
    with _lock:
        _memory = None


def _save(choices: Dict[str, dict]) -> None:
    path = choices_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(choices, f, indent=1)
    os.replace(tmp, path)


def chosen_decoder(path: str) -> Optional[str]:
    """The measured winner for ``path`` (``"d3d11va"``/``"cuda"``), or None for
    software -- also when the file hasn't been measured (yet)."""
    key = source_key(path)
    if key is None:
        return None
    with _lock:
        entry = _load().get(key)
    winner = entry.get("decoder") if isinstance(entry, dict) else None
    return winner if winner in gpu_candidates() else None


def is_measured(path: str) -> bool:
    key = source_key(path)
    with _lock:
        return key is not None and key in _load()


def _decode_seconds(ffmpeg: str, path: str, mode: str, frames: int) -> Optional[float]:
    args = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin"]
    if mode != SOFTWARE:
        args += ["-hwaccel", mode]
    args += ["-i", path, "-map", "0:v:0", "-frames:v", str(frames), "-an", "-f", "null", "-"]
    start = time.perf_counter()
    try:
        result = run(args, timeout=MEASURE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- timeout or a crash: this decoder doesn't count
        return None
    elapsed = time.perf_counter() - start
    # A GPU decoder that fails to initialise falls back to software and still
    # exits 0; its error output marks it as failed instead of timing software.
    if result.returncode != 0 or (mode != SOFTWARE and (result.stderr or "").strip()):
        return None
    return elapsed


def measure_decoders(path: str, candidates: Optional[Iterable[str]] = None,
                     frames: int = MEASURE_FRAMES) -> Dict[str, Optional[float]]:
    """Seconds each decoder (software and every GPU one) spends on ``frames`` frames
    of ``path``, None where it failed. Each is timed twice -- a short run and one
    ``frames`` longer -- and the difference kept, so process start-up and the GPU
    device's one-time set-up (about 0.2 s, which would decide a short test on its
    own but not a long export) cancel out."""
    ffmpeg = find_binary("ffmpeg")
    timings: Dict[str, Optional[float]] = {}
    for mode in [SOFTWARE] + list(candidates if candidates is not None else gpu_candidates()):
        short = _decode_seconds(ffmpeg, path, mode, WARMUP_FRAMES)
        long = _decode_seconds(ffmpeg, path, mode, WARMUP_FRAMES + frames) if short is not None else None
        timings[mode] = max(long - short, 1e-3) if long is not None else None
    return timings


def choose_decoder(timings: Dict[str, Optional[float]], margin: float = GPU_MARGIN) -> Optional[str]:
    """The GPU decoder to use, or None (software): the fastest GPU time, if it
    is under ``margin`` x software's."""
    software = timings.get(SOFTWARE)
    gpu = [(t, mode) for mode, t in timings.items() if mode != SOFTWARE and t is not None]
    if not gpu:
        return None
    best_time, best_mode = min(gpu)
    if software is None or best_time <= software * margin:
        return best_mode
    return None


def measure_and_remember(path: str, candidates: Optional[Iterable[str]] = None) -> Optional[str]:
    """Measure ``path`` (unless already measured) and cache the winner; returns it."""
    key = source_key(path)
    if key is None:
        return None
    if is_measured(path):
        return chosen_decoder(path)
    timings = measure_decoders(path, candidates)
    if timings.get(SOFTWARE) is None:
        return None  # not decodable as video at all: nothing to remember
    winner = choose_decoder(timings)
    with _lock:
        choices = _load()
        choices[key] = {"decoder": winner or SOFTWARE, "seconds": timings, "frames": MEASURE_FRAMES}
        _save(choices)
    return winner


def measured_summary() -> str:
    """e.g. ``"3 files measured, 1 decodes on the GPU"`` (self-test / status bar)."""
    with _lock:
        entries = list(_load().values())
    on_gpu = sum(1 for e in entries if isinstance(e, dict) and e.get("decoder") not in (None, SOFTWARE))
    return f"{len(entries)} file{'s' if len(entries) != 1 else ''} measured, {on_gpu} decode{'s' if on_gpu == 1 else ''} on the GPU"
