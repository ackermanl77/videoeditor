"""Background worker that probes/thumbnails/waveforms imported media files.

Runs on a QThread so a batch import never blocks the UI thread, and works
in two stages on a small thread pool (``min(4, cpu_count / 2)`` workers):

1. every file is probed in parallel and appears in the media bin as soon as
   its probe finishes (``asset_ready``);
2. thumbnails and waveforms are then generated in parallel and delivered
   with ``asset_updated``.

Each FFmpeg/FFprobe call has a timeout (see core/media_probe.py and
core/waveform.py), and :meth:`cancel` stops everything not yet started.
The previous worker handled one file at a time, probe -> thumbnail ->
waveform, with no timeout and no way to stop it.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

from core.media_asset import MediaAsset
from core.media_probe import generate_filmstrip, generate_thumbnail, probe_file, safe_thumbnail_time
from core.waveform import generate_waveform


def default_worker_count() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


class ImportWorker(QObject):
    asset_ready = Signal(object)  # MediaAsset, probed (no thumbnail/waveform yet)
    asset_updated = Signal(str, object, object, object)  # asset id, thumbnail path, filmstrip path, waveform path
    failed = Signal(str, str)  # path, error message
    finished = Signal()

    def __init__(self, paths: List[str], thumbnail_cache_dir: str, waveform_cache_dir: str,
                 max_workers: Optional[int] = None):
        super().__init__()
        self._paths = list(paths)
        self._thumbnail_cache_dir = thumbnail_cache_dir
        self._waveform_cache_dir = waveform_cache_dir
        self._max_workers = max_workers or default_worker_count()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Stop after the calls already running; safe to call from any thread."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _probe(self, path: str) -> MediaAsset:
        if not os.path.isfile(path):
            raise FileNotFoundError("File not found.")
        return probe_file(path)

    def _extras(self, asset: MediaAsset):
        thumb = None
        filmstrip = None
        if asset.has_video or asset.is_image:
            thumb = generate_thumbnail(asset.path, self._thumbnail_cache_dir, safe_thumbnail_time(asset.duration))
        if asset.has_video:
            filmstrip = generate_filmstrip(asset.path, self._thumbnail_cache_dir, asset.duration)
        waveform = generate_waveform(asset.path, self._waveform_cache_dir, asset.duration) if asset.has_audio else None
        return asset.id, thumb, filmstrip, waveform

    def run(self) -> None:
        probed: List[MediaAsset] = []
        pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="import")
        try:
            futures = {pool.submit(self._probe, path): path for path in self._paths}
            for future in as_completed(futures):
                if self.cancelled:
                    break
                path = futures[future]
                try:
                    asset = future.result()
                except Exception as exc:  # noqa: BLE001 - report per file, never abort the batch
                    self.failed.emit(path, str(exc) or exc.__class__.__name__)
                    continue
                probed.append(asset)
                self.asset_ready.emit(asset)
            if not self.cancelled:
                extras = [pool.submit(self._extras, asset) for asset in probed]
                for future in as_completed(extras):
                    if self.cancelled:
                        break
                    try:
                        asset_id, thumb, filmstrip, waveform = future.result()
                    except Exception:  # noqa: BLE001 - a missing thumbnail/waveform isn't fatal
                        continue
                    self.asset_updated.emit(asset_id, thumb, filmstrip, waveform)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            self.finished.emit()
