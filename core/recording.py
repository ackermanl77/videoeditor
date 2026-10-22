"""Recording (F34): a voiceover from a microphone, the screen, or a webcam, straight into the
media bin -- with the bundled FFmpeg, so it needs nothing else installed.

* Microphone and webcam: DirectShow (``-f dshow``), the devices listed by
  :func:`list_devices` (FFmpeg's ``-list_devices``).
* Screen: ``gdigrab`` of the whole desktop (works on every Windows version and
  GPU; ``ddagrab`` needs a filter graph and a D3D11 device), H.264 at 30 fps,
  optionally with a microphone.

A :class:`Recorder` runs FFmpeg until :meth:`Recorder.stop`, which sends "q" on
stdin so FFmpeg finishes the file properly (a killed MP4 has no index).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

KINDS = ("voiceover", "screen", "webcam")
_DEVICE_RE = re.compile(r'"(?P<name>[^"]+)"\s+\((?P<kind>audio|video)\)')


@dataclass(frozen=True)
class Device:
    name: str
    kind: str  # "audio" or "video"
    #: DirectShow's unique ASCII moniker ("@device_cm_{...}"), used to open it: two identical
    #: microphones share a name, and a name with accents depends on the console's code page.
    alternative: str = ""

    @property
    def spec(self) -> str:
        return self.alternative or self.name


_ALTERNATIVE_RE = re.compile(r'Alternative name\s+"(?P<alt>[^"]+)"')


def parse_device_list(text: str) -> List[Device]:
    """DirectShow devices from ``ffmpeg -list_devices true -f dshow -i dummy`` output (stderr)."""
    devices: List[Device] = []
    for line in text.splitlines():
        alternative = _ALTERNATIVE_RE.search(line)
        if alternative and devices:
            last = devices[-1]
            devices[-1] = Device(last.name, last.kind, alternative.group("alt"))
            continue
        match = _DEVICE_RE.search(line)
        if match:
            devices.append(Device(match.group("name"), match.group("kind")))
    return devices


def list_devices(ffmpeg: str, runner=subprocess.run) -> List[Device]:
    run = runner([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return parse_device_list((run.stderr or "") + (run.stdout or ""))


def _dshow(spec: str) -> List[str]:
    return ["-f", "dshow", "-i", spec]


def recording_args(ffmpeg: str, kind: str, output: str, microphone: str = "", camera: str = "",
                   framerate: int = 30) -> List[str]:
    """FFmpeg arguments that record ``kind`` into ``output`` until "q" arrives on stdin."""
    if kind not in KINDS:
        raise ValueError(f"unknown recording kind {kind!r}")
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if kind == "voiceover":
        if not microphone:
            raise ValueError("Choose a microphone.")
        return args + _dshow(f"audio={microphone}") + ["-c:a", "pcm_s16le", "-ar", "48000", output]
    if kind == "screen":
        args += ["-f", "gdigrab", "-framerate", str(framerate), "-draw_mouse", "1", "-i", "desktop"]
        if microphone:
            args += _dshow(f"audio={microphone}")
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        if microphone:
            args += ["-c:a", "aac", "-b:a", "192k"]
        return args + [output]
    if not camera:
        raise ValueError("Choose a camera.")
    spec = f"video={camera}" + (f":audio={microphone}" if microphone else "")
    args += ["-rtbufsize", "256M"] + _dshow(spec) + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                                     "-pix_fmt", "yuv420p"]
    if microphone:
        args += ["-c:a", "aac", "-b:a", "192k"]
    return args + [output]


def output_path(folder: str, kind: str, now: Optional[float] = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(now))
    extension = ".wav" if kind == "voiceover" else ".mp4"
    label = {"voiceover": "Voiceover", "screen": "Screen", "webcam": "Webcam"}[kind]
    return os.path.join(folder, f"{label} {stamp}{extension}")


class Recorder:
    """One running recording."""

    def __init__(self, args: List[str], output: str, popen=subprocess.Popen):
        self.output = output
        self.started = time.monotonic()
        self.process = popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def stop(self, timeout: float = 15.0) -> Optional[str]:
        """Ends the recording; the file's path, or None when FFmpeg wrote nothing usable."""
        if self.running:
            try:
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        ok = os.path.isfile(self.output) and os.path.getsize(self.output) > 1024
        return self.output if ok else None

    def error_text(self) -> str:
        try:
            return (self.process.stderr.read() or b"").decode("utf-8", "replace").strip()[-500:]
        except (OSError, ValueError, AttributeError):
            return ""
