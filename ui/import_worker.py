"""Background worker that probes/thumbnails/waveforms imported media files.

Runs on a QThread so a batch import of large or numerous files never blocks
the UI thread — the previous approach ran this work inline in the main
window and papered over the freeze with ``QCoreApplication.processEvents()``.
"""

from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import QObject, Signal

from core.media_probe import generate_thumbnail, probe_file
from core.waveform import generate_waveform


class ImportWorker(QObject):
    """Probes each path in turn and emits a ready :class:`MediaAsset` for it."""

    asset_ready = Signal(object)  # MediaAsset
    failed = Signal(str, str)  # path, error message
    finished = Signal()

    def __init__(self, paths: List[str], thumbnail_cache_dir: str, waveform_cache_dir: str):
        super().__init__()
        self._paths = paths
        self._thumbnail_cache_dir = thumbnail_cache_dir
        self._waveform_cache_dir = waveform_cache_dir

    def run(self) -> None:
        for path in self._paths:
            if not os.path.isfile(path):
                self.failed.emit(path, "File not found.")
                continue
            try:
                asset = probe_file(path)
                asset.thumbnail_path = generate_thumbnail(path, self._thumbnail_cache_dir)
                if asset.has_audio:
                    asset.waveform_path = generate_waveform(path, self._waveform_cache_dir)
                self.asset_ready.emit(asset)
            except Exception as exc:  # noqa: BLE001 - surface any failure, never abort the batch
                self.failed.emit(path, str(exc))
        self.finished.emit()
