"""The colour format an export is rendered in: 8/10-bit SDR BT.709, or 10-bit HDR (PQ / HLG).

The default stays 8-bit SDR BT.709 -- HDR sources are tone-mapped to it
(core/filter_graph.py). A 10-bit SDR export keeps the same colours with a
10-bit working format end to end. An HDR export ("passthrough") keeps HDR
sources in their own transfer, BT.2020, 10-bit; SDR sources (and text) are
placed into it at the BT.2408 reference white of 203 cd/m2 (75 % HLG, about
58 % PQ), so they look like SDR material inside an HDR programme.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

#: HDR choice -> ffprobe/FFmpeg ``color_trc`` name.
HDR_TRANSFER_NAMES = {"pq": "smpte2084", "hlg": "arib-std-b67"}
#: ffprobe transfer name -> HDR choice.
HDR_KIND_BY_TRANSFER = {v: k for k, v in HDR_TRANSFER_NAMES.items()}
#: Nominal peak luminance zscale uses for linear light: SDR white = 203 cd/m2 (ITU-R BT.2408).
REFERENCE_WHITE_NITS = 203

COLOR_MODE_LABELS = {
    "sdr8": "8-bit SDR (BT.709)",
    "sdr10": "10-bit SDR (BT.709)",
    "pq": "10-bit HDR10 (PQ, BT.2020)",
    "hlg": "10-bit HDR HLG (BT.2020)",
}


@dataclass(frozen=True)
class OutputColor:
    bit_depth: int = 8
    hdr: str = ""  # "" (SDR), "pq" or "hlg"

    @classmethod
    def from_mode(cls, mode: str) -> "OutputColor":
        if mode in HDR_TRANSFER_NAMES:
            return cls(10, mode)
        return cls(10 if mode == "sdr10" else 8, "")

    @property
    def mode(self) -> str:
        return self.hdr or ("sdr10" if self.bit_depth == 10 else "sdr8")

    @property
    def is_hdr(self) -> bool:
        return bool(self.hdr)

    @property
    def is_10bit(self) -> bool:
        return self.bit_depth >= 10 or self.is_hdr

    @property
    def transfer(self) -> str:
        return HDR_TRANSFER_NAMES.get(self.hdr, "bt709")

    @property
    def primaries(self) -> str:
        return "bt2020" if self.is_hdr else "bt709"

    @property
    def matrix(self) -> str:
        return "bt2020nc" if self.is_hdr else "bt709"

    def setparams(self) -> str:
        """``setparams`` tagging frames with this target (encoders take their colour tags from frames)."""
        return (
            f"setparams=color_primaries={self.primaries}:color_trc={self.transfer}"
            f":colorspace={self.matrix}:range=tv"
        )

    def output_tag_args(self) -> List[str]:
        return ["-colorspace", self.matrix, "-color_primaries", self.primaries,
                "-color_trc", self.transfer, "-color_range", "tv"]

    def text_color(self, color: str) -> str:
        """An SDR ``#RRGGBB`` as the code values that show it at reference white in this target."""
        return color if not self.is_hdr else hdr_code_color(color, self.hdr)


SDR_8BIT = OutputColor()

# BT.709 -> BT.2020 primaries, linear light (ITU-R BT.2087).
_709_TO_2020 = (
    (0.6274, 0.3293, 0.0433),
    (0.0691, 0.9195, 0.0114),
    (0.0164, 0.0880, 0.8956),
)


def _pq_oetf(nits: float) -> float:
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    y = max(0.0, nits) / 10000.0
    return ((c1 + c2 * y ** m1) / (1 + c3 * y ** m1)) ** m2


def _hlg_oetf(e: float) -> float:
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = max(0.0, e)
    return math.sqrt(3 * e) if e <= 1 / 12 else a * math.log(12 * e - b) + c


def hdr_code_color(color: str, hdr: str) -> str:
    """Map an SDR (BT.709, gamma 2.4) ``#RRGGBB`` into PQ or HLG BT.2020 code values.

    White becomes 203 cd/m2 (PQ) / 75 % (HLG) instead of the format's peak,
    so a white title isn't drawn at 10,000 cd/m2.
    """
    value = (color or "").lstrip("#")
    if len(value) != 6:
        return color
    try:
        rgb = [int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except ValueError:
        return color
    linear = [c ** 2.4 for c in rgb]
    wide = [sum(m * c for m, c in zip(row, linear)) for row in _709_TO_2020]
    if hdr == "pq":
        coded = [_pq_oetf(c * REFERENCE_WHITE_NITS) for c in wide]
    else:
        coded = [_hlg_oetf(c * 0.2647) for c in wide]  # 0.2647 scene light = 75 % HLG (BT.2408)
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in coded)


def hdr_transfers(transfers: Iterable[str]) -> List[str]:
    """The HDR kinds ("pq"/"hlg") among ffprobe transfer names, sorted and unique."""
    return sorted({HDR_KIND_BY_TRANSFER[t] for t in transfers if t in HDR_KIND_BY_TRANSFER})


def passthrough_choice(transfers: Iterable[str]) -> Optional[str]:
    """The HDR passthrough an export can offer: the one HDR transfer every HDR source shares, or None.

    Mixed PQ and HLG sources would need a PQ<->HLG conversion with no
    reference display to check it against, so no passthrough is offered then.
    """
    kinds = hdr_transfers(transfers)
    return kinds[0] if len(kinds) == 1 else None


def mastering_from_side_data(side_data: Iterable[dict]) -> Tuple[str, str]:
    """``(x265 master-display string, "maxcll,maxfall")`` from ffprobe frame side data ("" when absent).

    x265 wants chromaticities in 0.00002 units (G, B, R, white point) and
    luminance in 0.0001 cd/m2 units -- the units ffprobe reports as
    ``n/50000`` and ``n/10000`` fractions.
    """
    master, cll = "", ""

    def units(value, scale: int) -> int:
        num, _, den = str(value).partition("/")
        return int(round(float(num) / float(den or 1) * scale))

    for entry in side_data:
        kind = entry.get("side_data_type", "")
        try:
            if kind == "Mastering display metadata" and "red_x" in entry:
                xy = {k: units(entry[k], 50000) for k in (
                    "green_x", "green_y", "blue_x", "blue_y", "red_x", "red_y", "white_point_x", "white_point_y")}
                master = (
                    f"G({xy['green_x']},{xy['green_y']})B({xy['blue_x']},{xy['blue_y']})"
                    f"R({xy['red_x']},{xy['red_y']})WP({xy['white_point_x']},{xy['white_point_y']})"
                    f"L({units(entry['max_luminance'], 10000)},{units(entry['min_luminance'], 10000)})"
                )
            elif kind == "Content light level metadata":
                cll = f"{int(entry.get('max_content', 0))},{int(entry.get('max_average', 0))}"
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return master, cll
