#!/usr/bin/env python3
"""Download the FFmpeg build pinned in ffmpeg.lock, verify it, and install it into bin/.

Used by both GitHub workflows (the release build and CI's real-FFmpeg
integration job) so they always bundle/test the same, checksum-verified
FFmpeg -- the release used to download gyan.dev's floating "latest release"
zip with no integrity check, so two tags a week apart could ship different
FFmpeg versions.

    python scripts/fetch_ffmpeg.py [--dest bin] [--zip already-downloaded.zip]

Stdlib only. Exits non-zero (and installs nothing) on any hash mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "ffmpeg.lock")


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path: str = LOCK_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def install_from_zip(zip_path: str, lock: dict, dest: str) -> list:
    """Verify ``zip_path`` against ``lock`` and extract its pinned binaries into ``dest``."""
    actual = sha256_of(zip_path)
    if actual != lock["sha256"]:
        raise ValueError(f"archive sha256 {actual} does not match ffmpeg.lock ({lock['sha256']})")
    os.makedirs(dest, exist_ok=True)
    installed = []
    with zipfile.ZipFile(zip_path) as archive:
        members = {os.path.basename(name): name for name in archive.namelist() if "/bin/" in name.replace("\\", "/")}
        for binary, expected in lock["binaries"].items():
            if binary not in members:
                raise ValueError(f"{binary} not found in the archive's bin/ folder")
            data = archive.read(members[binary])
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"{binary} sha256 does not match ffmpeg.lock")
            target = os.path.join(dest, binary)
            with open(target, "wb") as f:
                f.write(data)
            installed.append(target)
    return installed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", default=os.path.join(ROOT, "bin"))
    parser.add_argument("--zip", help="use an already-downloaded archive instead of downloading")
    args = parser.parse_args(argv)
    lock = load_lock()
    zip_path = args.zip
    tmp = None
    try:
        if zip_path is None:
            fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="ffmpeg-")
            os.close(fd)
            print(f"Downloading FFmpeg {lock['version']} from {lock['url']}")
            urllib.request.urlretrieve(lock["url"], tmp)
            zip_path = tmp
        for path in install_from_zip(zip_path, lock, args.dest):
            print(f"Installed {path} (sha256 verified)")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
