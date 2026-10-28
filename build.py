#!/usr/bin/env python3
"""Convenience wrapper around PyInstaller for building the standalone app.

Usage:
    python build.py [--keep-build] [--installer] [--sign] [--any-python]

This simply invokes ``pyinstaller build.spec`` after a couple of sanity
checks, so contributors don't need to remember PyInstaller's CLI flags.
The result is a folder, ``dist/VideoEditor/`` (``VideoEditor.exe`` plus its
``_internal/`` folder on Windows). ``--installer`` then compiles
``installer/VideoEditor.iss`` with Inno Setup 6 (Windows only) into
``dist/installer/VideoEditor-<version>-setup.exe``.

Builds use the Python version in ``.python-version`` (the one CI tests and
the release workflow builds with); ``--any-python`` skips that check.
``--sign`` Authenticode-signs ``VideoEditor.exe``, the installer and its uninstaller with
signtool and the certificate whose SHA-1 thumbprint is in
``VIDEO_EDITOR_SIGN_THUMBPRINT`` or ``CODESIGN_THUMBPRINT`` (Windows certificate store),
timestamped by ``VIDEO_EDITOR_TIMESTAMP_URL`` (default DigiCert's).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"


def expected_python() -> tuple[int, int] | None:
    """The (major, minor) in .python-version, or None when it's missing."""
    try:
        with open(os.path.join(ROOT, ".python-version"), encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    major, minor = text.split(".")[:2]
    return int(major), int(minor)


def find_signtool() -> str | None:
    """signtool.exe: on PATH, or the newest one in the Windows 10/11 SDK."""
    found = shutil.which("signtool")
    if found:
        return found
    kits = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Windows Kits", "10", "bin")
    if not os.path.isdir(kits):
        return None
    for version in sorted(os.listdir(kits), reverse=True):
        candidate = os.path.join(kits, version, "x64", "signtool.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def sign_command(signtool: str, thumbprint: str, path: str, timestamp_url: str = DEFAULT_TIMESTAMP_URL) -> list[str]:
    return [signtool, "sign", "/sha1", thumbprint, "/fd", "SHA256", "/tr", timestamp_url, "/td", "SHA256", path]


def signing_settings() -> tuple[str, str, str] | None:
    """(signtool, thumbprint, timestamp URL) for --sign, or None (with the reason printed).
    The thumbprint comes from VIDEO_EDITOR_SIGN_THUMBPRINT, else CODESIGN_THUMBPRINT -- the
    variable all of this machine's projects read, so one setting signs every build."""
    thumbprint = (os.environ.get("VIDEO_EDITOR_SIGN_THUMBPRINT") or os.environ.get("CODESIGN_THUMBPRINT") or "").replace(" ", "")
    if not thumbprint:
        print("--sign needs VIDEO_EDITOR_SIGN_THUMBPRINT (or CODESIGN_THUMBPRINT): the SHA-1 thumbprint of a code-signing certificate")
        return None
    signtool = find_signtool()
    if signtool is None:
        print("signtool.exe was not found (install the Windows SDK's signing tools)")
        return None
    timestamp = os.environ.get("VIDEO_EDITOR_TIMESTAMP_URL") or os.environ.get("CODESIGN_TIMESTAMP_URL") or DEFAULT_TIMESTAMP_URL
    return signtool, thumbprint, timestamp


def inno_sign_args(signtool: str, thumbprint: str, timestamp_url: str) -> list[str]:
    """ISCC arguments that make Inno Setup sign the installer and its uninstaller
    (installer/VideoEditor.iss: SignTool=codesign under #ifdef Sign; $q = quote, $f = file)."""
    return ["/DSign", f"/Scodesign=$q{signtool}$q sign /sha1 {thumbprint} /fd SHA256 /tr {timestamp_url} /td SHA256 $f"]


def sign(path: str) -> int:
    """Sign `path` (see the module docstring); non-zero on any failure."""
    settings = signing_settings()
    if settings is None:
        return 1
    signtool, thumbprint, timestamp = settings
    print("Signing:", path)
    return subprocess.run(sign_command(signtool, thumbprint, path, timestamp), cwd=ROOT).returncode


def find_iscc() -> str | None:
    """Inno Setup's compiler: on PATH, or in its default install folders."""
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for folder in ("Inno Setup 6", os.path.join("Programs", "Inno Setup 6")):
            candidate = os.path.join(base, folder, "ISCC.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def build_installer() -> int:
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup 6 (ISCC.exe) was not found. Install it from https://jrsoftware.org/isinfo.php")
        return 1
    sys.path.insert(0, ROOT)
    from core import __version__

    cmd = [iscc, f"/DAppVersion={__version__}"]
    if "--sign" in sys.argv[1:]:
        settings = signing_settings()
        if settings is None:
            return 1
        cmd += inno_sign_args(*settings)
    cmd.append(os.path.join("installer", "VideoEditor.iss"))
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode
    print(f"\nInstaller: dist/installer/VideoEditor-{__version__}-setup.exe")
    return 0


def main() -> int:
    wanted = expected_python()
    if wanted and sys.version_info[:2] != wanted and "--any-python" not in sys.argv[1:]:
        print(
            f"This build would use Python {sys.version_info[0]}.{sys.version_info[1]}, but releases are built with "
            f"{wanted[0]}.{wanted[1]} (.python-version). Use that interpreter, or pass --any-python."
        )
        return 1
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed. Install it with:")
            print("  pip install pyinstaller")
            return 1

    bin_dir = os.path.join(ROOT, "bin")
    if not os.path.isdir(bin_dir) or not os.listdir(bin_dir):
        print(
            "Note: no bundled FFmpeg binaries found in ./bin/. The built app "
            "will rely on FFmpeg being installed on the end user's PATH.\n"
            "To bundle FFmpeg instead, copy ffmpeg/ffprobe (or ffmpeg.exe/"
            "ffprobe.exe on Windows) into ./bin/ before building.\n"
        )

    # THIRD_PARTY_NOTICES.md lists the packages actually being bundled; build.spec ships it.
    notices = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "third_party_notices.py")], cwd=ROOT)
    if notices.returncode != 0:
        return notices.returncode
    cmd = [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode
    # build/ is PyInstaller's intermediate work folder (~160 MB), not part of the app. Remove it
    # after a successful build -- a failed one keeps it for debugging -- unless --keep-build is
    # passed, which trades the disk space for a faster next rebuild.
    if "--keep-build" not in sys.argv[1:]:
        shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    exe_name = "VideoEditor.exe" if sys.platform.startswith("win") else "VideoEditor"
    if "--sign" in sys.argv[1:] and sign(os.path.join(ROOT, "dist", "VideoEditor", exe_name)) != 0:
        return 1
    print(f"\nBuild finished. Your app is at: dist/VideoEditor/{exe_name} (ship the whole folder)")
    if "--installer" in sys.argv[1:]:
        return build_installer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
