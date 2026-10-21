"""The audio side of the export filter graph (R9: split out of core/filter_graph.py, which
re-exports every name): fades, volume automation, delays and atempo, placement on the timeline,
speed-ramped audio, crossfades, ducking and each clip's audio chain."""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Tuple

from core.clip import Clip, MediaType
from core.audio_effects import audio_effect_filters
from core.project import Project
from core.filter_graph_base import _clip_has_audio, _piecewise_linear_expr, _Window


def _afade(kind: str, start: float, duration: float, curve: str = "") -> str:
    """One ``afade``; ``curve="qsin"`` is the equal-power curve used for crossfades.

    A linear (default ``tri``) crossfade of two uncorrelated sources dips
    about 3 dB in the middle; quarter-sine in/out keeps the summed power flat.
    """
    out = f"afade=t={kind}:st={start:.3f}:d={duration:.3f}" if kind == "out" else f"afade=t=in:st=0:d={duration:.3f}"
    return out + (f":curve={curve}" if curve else "")


def _volume_filter(clip: Clip) -> str:
    """Comma-prefixed gain: a per-frame envelope for 2+ volume keyframes, else the scalar volume.

    The envelope uses clip-local time: every audio chain re-stamps its
    samples to start at the clip's own start before this step.
    """
    if clip.has_volume_automation:
        return f",volume=eval=frame:volume='{_piecewise_linear_expr(clip.volume_keyframes, 't', clip.easing_for('volume'))}'"
    if abs(clip.volume - 1.0) > 1e-6:
        return f",volume={clip.volume:.3f}"
    return ""


def _adelay_filter(seconds: float) -> str:
    """``adelay`` shifting *every* channel of a stream by ``seconds``.

    ``all=1`` applies the one delay to all channels -- the previous
    ``adelay=<ms>|<ms>`` form only delayed the first two, so a 5.1 (or any
    3+ channel) source had its remaining channels play early, out of sync.
    """
    return f"adelay=delays={int(round(seconds * 1000))}:all=1"


def _atempo_chain(speed: float) -> List[str]:
    """FFmpeg's ``atempo`` only accepts 0.5-2.0; chain filters for wider ranges."""
    if speed <= 0:
        return []
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f"atempo={remaining:.6f}")
    return filters


# Master-bus peak ceiling: alimiter's limit=0.891 is -1 dBFS. amix sums its
# inputs with normalize=0 (so adding a track never quietly lowers the
# others), which let two ordinary tracks reach +3.7 dBFS and clip on decode.
# level=false keeps alimiter from auto-raising the level; latency=true
# compensates its look-ahead delay so audio stays in sync with the picture.
MASTER_LIMITER = "alimiter=limit=0.891:level=false:latency=true"


def _audio_format_filter(sample_rate: int) -> str:
    """Per-clip normalization to the project's sample rate and a stereo layout.

    Every clip reaches ``amix`` in the same format, instead of leaving a mix
    of 44.1 kHz music, 48 kHz camera audio, mono voice-over and 5.1 to
    amix's own format negotiation.
    """
    return f"aresample={sample_rate},aformat=sample_rates={sample_rate}:channel_layouts=stereo"


def _audio_placement(clip_start: float, head: float, window: _Window, seeked: bool) -> str:
    """Audio counterpart of :func:`_video_placement` (``adelay`` instead of ``setpts``)."""
    if head > 0:
        if seeked:
            return "asetpts=PTS-STARTPTS"
        return f"atrim=start={head:.6f},asetpts=PTS-STARTPTS"
    offset = clip_start - window.start
    return _adelay_filter(offset) if offset > 0 else ""


def _maybe_add_speed_ramped_audio(
    project: Project,
    clip: Clip,
    segment_specs: List[Tuple[int, float, float, float]],
    counter: int,
    filter_lines: List[str],
    audio_labels: List[str],
    probe_cache: Dict[str, bool],
    window: _Window = _Window(),
    sample_rate: int = 48000,
) -> None:
    """Speed-ramped-clip counterpart to ``_maybe_add_audio``.

    Builds one ``atempo``-adjusted audio segment per video segment (reusing
    each segment's already-registered ``-i`` input, see
    ``_build_speed_ramp_video_chain``), concatenates them, then applies
    fade/volume/placement once to the whole result — identical
    end-to-end behavior to ``_build_audio_chain``, just fed by N segments
    instead of one continuous stream. Segment inputs aren't seeked to an
    export window, so a clip straddling the window's start is trimmed after
    the fades instead (``_audio_placement(..., seeked=False)``).
    """
    if clip.is_silent:
        return
    if not _clip_has_audio(project, clip, probe_cache):
        return

    seg_labels = []
    ordered_specs = list(reversed(segment_specs)) if clip.reverse else list(segment_specs)
    for i, (idx, seg_in, seg_out, seg_speed) in enumerate(ordered_specs):
        seg_chain = f"[{idx}:a]atrim=duration={(seg_out - seg_in):.3f},asetpts=PTS-STARTPTS"
        if clip.reverse:
            seg_chain += ",areverse"
        if abs(seg_speed - 1.0) > 1e-6:
            for f in _atempo_chain(seg_speed):
                seg_chain += f",{f}"
        label = f"sr{counter}a{i}"
        filter_lines.append(f"{seg_chain}[{label}]")
        seg_labels.append(label)

    if len(seg_labels) > 1:
        joined = "".join(f"[{lbl}]" for lbl in seg_labels)
        base_label = f"sr{counter}a"
        filter_lines.append(f"{joined}concat=n={len(seg_labels)}:v=0:a=1[{base_label}]")
    else:
        base_label = seg_labels[0]

    tail = f",{_audio_format_filter(sample_rate)}"
    if clip.has_denoise:
        tail += f",afftdn=nr={clip.denoise_strength:.3f}"
    tail += audio_effect_filters(clip)
    # The freeze-frame tail is silent (the audio simply ends there), so the
    # fade-out ends where the sound does instead of inside the silence.
    clip_len = clip.playable_duration
    if clip.fade_in > 0:
        tail += "," + _afade("in", 0.0, clip.fade_in)
    if clip.fade_out > 0:
        tail += "," + _afade("out", max(0.0, clip_len - clip.fade_out), clip.fade_out)
    tail += _volume_filter(clip)
    placement = _audio_placement(clip.timeline_start, window.head(clip.timeline_start), window, seeked=False)
    if placement:
        tail += f",{placement}"

    final_label = f"sr{counter}af"
    filter_lines.append(f"[{base_label}]{tail.lstrip(',')}[{final_label}]")
    audio_labels.append(final_label)


def _apply_audio_ducking(
    project: Project,
    filter_lines: List[str],
    audio_labels: List[str],
    audio_labels_by_track: Dict[str, List[str]],
) -> None:
    """Route each ducked track's audio through ``sidechaincompress`` against its trigger.

    A track whose ``duck_trigger_track_id`` names another track (e.g. a
    background-music track ducking under a narration/dialogue track) has
    its own mixed audio compressed using the trigger track's mixed audio as
    the sidechain "detector" input -- so it automatically gets quieter
    whenever the trigger track is audible. Implemented via FFmpeg's
    ``sidechaincompress=threshold:ratio:attack:release`` filter, which takes
    exactly two inputs (``[main][sidechain]``) and emits one compressed
    output for ``main`` only -- the trigger's own audio is *not* consumed or
    silenced by this: its labels are left untouched in ``audio_labels``, so
    it continues to play normally, independently of being used as a
    detector here.

    Mutates ``audio_labels`` in place: the ducked track's individual clip
    labels are removed and replaced with the single compressed output
    label, so the final ``amix`` (in ``build_filter_graph``) picks up the
    ducked result instead of double-counting the pre-compression labels.
    Silently does nothing for a track with no ``duck_trigger_track_id``, an
    unresolvable/self-referencing trigger, or either side with no audible
    audio at all (nothing to duck, or nothing to duck against).
    """
    counter = 0
    for track in project.tracks:
        trigger_id = track.duck_trigger_track_id
        if not trigger_id or trigger_id == track.id:
            continue
        main_labels = audio_labels_by_track.get(track.id) or []
        trigger_labels = audio_labels_by_track.get(trigger_id) or []
        if not main_labels or not trigger_labels:
            continue

        if len(main_labels) > 1:
            main_label = f"duckmain{counter}"
            joined = "".join(f"[{lbl}]" for lbl in main_labels)
            filter_lines.append(
                f"{joined}amix=inputs={len(main_labels)}:duration=longest:dropout_transition=0:normalize=0[{main_label}]"
            )
        else:
            main_label = main_labels[0]

        if len(trigger_labels) > 1:
            trigger_label = f"ducktrig{counter}"
            joined = "".join(f"[{lbl}]" for lbl in trigger_labels)
            filter_lines.append(
                f"{joined}amix=inputs={len(trigger_labels)}:duration=longest:dropout_transition=0:normalize=0[{trigger_label}]"
            )
        else:
            trigger_label = trigger_labels[0]

        out_label = f"duckout{counter}"
        filter_lines.append(
            f"[{main_label}][{trigger_label}]sidechaincompress=threshold={track.duck_threshold:.4f}"
            f":ratio={track.duck_ratio:.2f}:attack={track.duck_attack:.2f}:release={track.duck_release:.2f}[{out_label}]"
        )

        for lbl in main_labels:
            if lbl in audio_labels:
                audio_labels.remove(lbl)
        audio_labels.append(out_label)
        counter += 1


def _maybe_add_audio(
    project: Project,
    clip: Clip,
    input_index: int,
    filter_lines: List[str],
    audio_labels: List[str],
    counter: int,
    probe_cache: Dict[str, bool],
    window: "_Window" = _Window(),
    head: float = 0.0,
    source_length: Optional[float] = None,
    seeked: bool = True,
    sample_rate: int = 48000,
    curve_in: str = "",
    curve_out: str = "",
    input_head: Optional[float] = None,
) -> None:
    """Append clip's audio chain to ``audio_labels`` if it's actually audible.

    Shared by every place a video/image clip's own audio needs handling
    (standalone clips and both sides of a crossfade pair) so the has-audio /
    muted / volume checks can't drift out of sync between them. The window
    arguments are passed through to :func:`_build_audio_chain`.
    """
    if not _clip_is_audible(project, clip, probe_cache):
        return
    audio_labels.append(
        _build_audio_chain(
            input_index, clip, filter_lines, counter,
            window=window, head=head, source_length=source_length, seeked=seeked,
            sample_rate=sample_rate, curve_in=curve_in, curve_out=curve_out, input_head=input_head,
        )
    )


def _clip_is_audible(project: Project, clip: Clip, probe_cache: Dict[str, bool]) -> bool:
    """Whether a video/image clip's own embedded audio would be mixed at all.

    The clip-level half of the check (a track's own ``muted`` flag is the
    caller's concern). Shared by ``_maybe_add_audio`` and the hidden-track
    path in ``build_filter_graph``, which registers a clip's ``-i`` input
    purely for its audio and so needs to know up front whether to bother.
    """
    if clip.media_type == MediaType.IMAGE:
        return False
    if clip.is_silent:
        return False
    return _clip_has_audio(project, clip, probe_cache)


def _crossfade_audio_clips(clip: Clip, partner: Clip, d: float, offset: float) -> Tuple[Clip, Clip]:
    """Audio-timing stand-ins for a crossfade pair's two sides.

    ``xfade`` starts the partner's (B's) first frame ``offset`` seconds into
    the merged layer -- i.e. at ``A.timeline_start + A.duration - d``, which
    is ``d`` seconds *before* ``B.timeline_start``. B's audio used to stay at
    ``B.timeline_start`` and so lagged its own picture by ``d`` for the whole
    of clip B. The returned copy of B starts where its video actually does,
    and the two sides' audio crossfade over the same ``d``-second overlap
    (A fades out, B fades in), replacing each side's own fade there exactly
    like ``skip_fade_out``/``skip_fade_in`` do on the video side.

    Shallow copies: only scalar timing/fade fields are changed, and the
    originals (the real model objects) are never mutated.
    """
    audio_a = copy.copy(clip)
    audio_a.fade_out = d
    audio_b = copy.copy(partner)
    audio_b.timeline_start = clip.timeline_start + offset
    audio_b.fade_in = d
    return audio_a, audio_b


def _build_audio_chain(
    input_index: int,
    clip: Clip,
    filter_lines: List[str],
    counter: int,
    window: _Window = _Window(),
    head: float = 0.0,
    source_length: Optional[float] = None,
    seeked: bool = True,
    sample_rate: int = 48000,
    curve_in: str = "",
    curve_out: str = "",
    input_head: Optional[float] = None,
) -> str:
    """A clip's audio chain, placed on the output timeline, ending in ``[a<counter>]``.

    ``curve_in``/``curve_out`` set the fade curve of a crossfade side's
    fade (``qsin``, equal power); ordinary fades stay linear.

    ``seeked=True`` means the input was already seeked ``head`` clip-local
    seconds past the clip's start (and holds ``source_length`` source
    seconds); its samples are re-stamped to clip-local time so the fades
    below land where they would for the whole clip. ``seeked=False`` inputs
    start at the clip's own start and are trimmed by ``head`` afterwards.

    ``input_head`` (clip-local time of the input's first sample) is given
    for a seeked slice that does *not* start at the window (one starting in
    the freeze-frame tail, see ``_clip_source_slice``): its samples are
    stamped at ``input_head`` and the unseeked trim to ``head`` follows.
    """
    label = f"a{counter}"
    length = clip.source_length if source_length is None else source_length
    stamp = head if seeked else (input_head or 0.0)
    chain = f"[{input_index}:a]atrim=duration={length:.3f},asetpts=PTS-STARTPTS,{_audio_format_filter(sample_rate)}"
    if clip.reverse:
        chain += ",areverse"

    speed = clip.speed if clip.speed > 0 else 1.0
    if abs(speed - 1.0) > 1e-6:
        for f in _atempo_chain(speed):
            chain += f",{f}"

    # Noise reduction, applied before fade/volume so it works on the clip's
    # unmodified audio level rather than an already-faded/attenuated signal.
    if clip.has_denoise:
        chain += f",afftdn=nr={clip.denoise_strength:.3f}"
    # Gate, EQ and compressor (F26), on the same unfaded level.
    chain += audio_effect_filters(clip)

    if stamp > 0:
        chain += f",asetpts=PTS+{stamp:.6f}/TB"

    # Fade in/out apply to a clip's audio too, not just its video alpha —
    # otherwise the Properties panel's Fade sliders silently do nothing for
    # an audio-only clip (and don't duck a video clip's own audio either).
    # The freeze-frame tail is silent (the audio just ends), so the fade-out
    # ends where the sound does rather than inside that silence.
    clip_len = clip.playable_duration
    if clip.fade_in > 0:
        chain += "," + _afade("in", 0.0, clip.fade_in, curve_in)
    if clip.fade_out > 0:
        chain += "," + _afade("out", max(0.0, clip_len - clip.fade_out), clip.fade_out, curve_out)

    chain += _volume_filter(clip)

    placement = _audio_placement(clip.timeline_start, head, window, seeked)
    if placement:
        chain += f",{placement}"

    filter_lines.append(f"{chain}[{label}]")
    return label
