"""Render Queue panel: queued exports rendered one after another in the background.

Jobs come from the Export dialog's "Add to Queue" (each is a project
snapshot plus its settings, see core/render_queue.py). One job renders at a
time on a worker thread (the same :class:`ui.export_dialog.ExportWorker` the
dialog uses); the table shows each job's status, progress and time left,
and the buttons cancel, remove, reorder, retry or reveal jobs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable, Dict, Optional

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.exporter import ExportSettings
from core.project import Project
from core.render_queue import JobStatus, RenderJob, RenderQueue
from ui.export_dialog import ExportWorker

COLUMNS = ("Name", "Status", "Progress", "Time Left", "Output")


class RenderQueueWidget(QWidget):
    """The queue table plus its buttons; runs the jobs sequentially."""

    job_finished = Signal(str, str)  # job id, status value
    queue_idle = Signal()

    def __init__(self, parent: Optional[QWidget] = None,
                 worker_factory: Callable[[Project, ExportSettings], ExportWorker] = ExportWorker):
        super().__init__(parent)
        self.queue = RenderQueue()
        # Injected so tests can run the queue without FFmpeg.
        self.worker_factory = worker_factory
        self._thread: Optional[QThread] = None
        self._worker: Optional[ExportWorker] = None
        self._running_job: Optional[RenderJob] = None
        self._cancel_requested = False
        self._bars: Dict[str, QProgressBar] = {}
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.summary_label = QLabel("")
        layout.addWidget(self.summary_label)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAccessibleName("Render queue")
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop the selected job (a waiting job is skipped)")
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Remove the selected job from the list")
        self.up_btn = QPushButton("Up")
        self.up_btn.setToolTip("Render the selected waiting job earlier")
        self.down_btn = QPushButton("Down")
        self.down_btn.setToolTip("Render the selected waiting job later")
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setToolTip("Queue a failed or cancelled job again")
        self.reveal_btn = QPushButton("Show File")
        self.reveal_btn.setToolTip("Open the folder containing the finished file")
        self.clear_btn = QPushButton("Clear Finished")
        for button, slot in (
            (self.cancel_btn, self.cancel_selected), (self.remove_btn, self.remove_selected),
            (self.up_btn, lambda: self.move_selected(-1)), (self.down_btn, lambda: self.move_selected(1)),
            (self.retry_btn, self.retry_selected), (self.reveal_btn, self.reveal_selected),
            (self.clear_btn, self.clear_finished),
        ):
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_job(self, project: Project, settings: ExportSettings) -> RenderJob:
        """Queue a snapshot of ``project``; starts rendering right away when nothing else is running."""
        job = self.queue.add(project, settings)
        self._refresh()
        self._start_next()
        return job

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def selected_job(self) -> Optional[RenderJob]:
        row = self.table.currentRow()
        if 0 <= row < len(self.queue.jobs):
            return self.queue.jobs[row]
        return None

    def select_job(self, job_id: str) -> None:
        for row, job in enumerate(self.queue.jobs):
            if job.id == job_id:
                self.table.selectRow(row)
                return

    def cancel_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        if job.status == JobStatus.RUNNING:
            self.cancel_running()
        elif self.queue.cancel_queued(job.id):
            self._refresh()

    def cancel_running(self) -> None:
        if self._worker is not None and self.is_running():
            self._cancel_requested = True
            self._worker.cancel()
            if self._running_job is not None:
                self._running_job.message = "Cancelling..."
            self._refresh()

    def remove_selected(self) -> None:
        job = self.selected_job()
        if job is not None and self.queue.remove(job.id):
            self._refresh()

    def move_selected(self, step: int) -> None:
        job = self.selected_job()
        if job is not None and self.queue.move(job.id, step):
            self._refresh()
            self.select_job(job.id)

    def retry_selected(self) -> None:
        job = self.selected_job()
        if job is not None and self.queue.requeue(job.id):
            self._refresh()
            self._start_next()

    def reveal_selected(self) -> None:
        job = self.selected_job()
        if job is None or job.status != JobStatus.DONE or not os.path.isfile(job.output_path):
            return
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(job.output_path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(job.output_path)))

    def clear_finished(self) -> None:
        if self.queue.clear_finished():
            self._refresh()

    def cancel_all_and_wait(self, timeout_ms: int = 5000) -> bool:
        """Skip every waiting job and stop the running one; True when nothing is running any more."""
        for job in self.queue.jobs:
            self.queue.cancel_queued(job.id)
        if not self.is_running():
            self._refresh()
            return True
        self.cancel_running()
        finished = self._thread.wait(timeout_ms)
        if finished:
            self._on_thread_done()
        return finished

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def _start_next(self) -> None:
        if self.is_running() or self._worker is not None:
            return
        job = self.queue.next_queued()
        if job is None:
            self._refresh()
            self.queue_idle.emit()
            return
        os.makedirs(os.path.dirname(os.path.abspath(job.output_path)) or ".", exist_ok=True)
        self.queue.mark_started(job)
        self._running_job = job
        self._cancel_requested = False
        self._thread = QThread(self)
        self._worker = self.worker_factory(job.project, job.settings)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        # Direct: quit() is thread-safe, and a queued call would never run
        # while cancel_all_and_wait() blocks the GUI thread in wait().
        self._worker.finished.connect(self._thread.quit, Qt.DirectConnection)
        self._worker.failed.connect(self._thread.quit, Qt.DirectConnection)
        self._thread.finished.connect(self._on_thread_done)
        self._refresh()
        self._thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        if self._running_job is not None and not self._cancel_requested:
            self.queue.mark_progress(self._running_job, fraction, message)
            self._refresh_row(self._running_job)

    def _on_finished(self, _path: str) -> None:
        job = self._running_job
        if job is None:
            return
        warnings = list(getattr(getattr(self._worker, "exporter", None), "warnings", []) or [])
        self.queue.mark_done(job, warnings)
        self.job_finished.emit(job.id, job.status.value)
        self._refresh()

    def _on_failed(self, message: str) -> None:
        job = self._running_job
        if job is None:
            return
        self.queue.mark_failed(job, message.splitlines()[0] if message else "Export failed.",
                               cancelled=self._cancel_requested)
        self.job_finished.emit(job.id, job.status.value)
        self._refresh()

    def _on_thread_done(self) -> None:
        if self._thread is None:
            return
        thread, worker, job = self._thread, self._worker, self._running_job
        if job is not None and job.status == JobStatus.RUNNING:
            # The worker's own finished/failed signal hasn't been delivered
            # (cancel_all_and_wait waited for the thread instead).
            self.queue.mark_failed(job, "Export cancelled." if self._cancel_requested else "Export stopped.",
                                   cancelled=self._cancel_requested)
            self.job_finished.emit(job.id, job.status.value)
        self._thread = self._worker = self._running_job = None
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()
        self._start_next()

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        selected = self.selected_job()
        self.table.setRowCount(len(self.queue.jobs))
        self._bars = {}
        for row, job in enumerate(self.queue.jobs):
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.table.setCellWidget(row, 2, bar)
            self._bars[job.id] = bar
            self._refresh_row(job, row)
        if selected is not None and selected in self.queue.jobs:
            self.select_job(selected.id)
        pending = self.queue.pending_count()
        self.summary_label.setText(
            f"{pending} export{'s' if pending != 1 else ''} waiting or rendering" if pending else "Nothing to render."
        )
        self._update_buttons()

    def _refresh_row(self, job: RenderJob, row: Optional[int] = None) -> None:
        if row is None:
            if job not in self.queue.jobs:
                return
            row = self.queue.jobs.index(job)
        status = job.status.value
        if job.status == JobStatus.RUNNING and job.message:
            status = job.message
        elif job.status in (JobStatus.FAILED, JobStatus.CANCELLED) and job.message:
            status = f"{job.status.value}: {job.message}"
        elif job.status == JobStatus.DONE and job.warnings:
            status = f"Done ({len(job.warnings)} warning{'s' if len(job.warnings) != 1 else ''})"
        cells = (job.name, status, None, job.eta, job.output_path)
        for column, text in enumerate(cells):
            if text is None:
                continue
            item = QTableWidgetItem(text)
            item.setToolTip("\n".join([text] + job.warnings) if column == 1 and job.warnings else text)
            self.table.setItem(row, column, item)
        bar = self._bars.get(job.id)
        if bar is not None:
            bar.setValue(int(round(job.progress * 100)))
            bar.setAccessibleName(f"{job.name} progress")

    def _update_buttons(self) -> None:
        job = self.selected_job()
        status = job.status if job is not None else None
        self.cancel_btn.setEnabled(status in (JobStatus.QUEUED, JobStatus.RUNNING))
        self.remove_btn.setEnabled(job is not None and status != JobStatus.RUNNING)
        self.up_btn.setEnabled(status == JobStatus.QUEUED)
        self.down_btn.setEnabled(status == JobStatus.QUEUED)
        self.retry_btn.setEnabled(status in (JobStatus.FAILED, JobStatus.CANCELLED))
        self.reveal_btn.setEnabled(status == JobStatus.DONE)
        self.clear_btn.setEnabled(any(j.is_finished for j in self.queue.jobs))
