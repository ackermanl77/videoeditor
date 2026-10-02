"""Render queue model: exports waiting to run one after another.

Each :class:`RenderJob` holds a deep copy of the project taken when it was
queued plus its own :class:`~core.exporter.ExportSettings`, so editing the
timeline afterwards never changes a queued render. The queue only tracks
order and state; ``ui/render_queue.py`` runs the jobs (one at a time, on a
worker thread) and reports progress back here.
"""

from __future__ import annotations

import copy
import os
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from core.exporter import ExportSettings
from core.project import Project


class JobStatus(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Rendering"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


FINISHED = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)

_ETA_RE = re.compile(r"~(\d[\d:]*) left")


@dataclass
class RenderJob:
    project: Project
    settings: ExportSettings
    name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0  # 0..1
    message: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def output_path(self) -> str:
        return self.settings.output_path

    @property
    def eta(self) -> str:
        """Time left as reported by the running export ("01:12"), or ""."""
        if self.status != JobStatus.RUNNING:
            return ""
        match = _ETA_RE.search(self.message)
        return match.group(1) if match else ""

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED


class RenderQueue:
    """Ordered render jobs; at most one is RUNNING at a time (the runner enforces it)."""

    def __init__(self) -> None:
        self.jobs: List[RenderJob] = []

    def add(self, project: Project, settings: ExportSettings, name: Optional[str] = None) -> RenderJob:
        """Queue a render of a snapshot of ``project`` as it is now."""
        job = RenderJob(
            project=copy.deepcopy(project),
            settings=copy.deepcopy(settings),
            name=name or os.path.basename(settings.output_path) or project.name,
        )
        self.jobs.append(job)
        return job

    def find(self, job_id: str) -> Optional[RenderJob]:
        return next((j for j in self.jobs if j.id == job_id), None)

    def running(self) -> Optional[RenderJob]:
        return next((j for j in self.jobs if j.status == JobStatus.RUNNING), None)

    def next_queued(self) -> Optional[RenderJob]:
        return next((j for j in self.jobs if j.status == JobStatus.QUEUED), None)

    def pending_count(self) -> int:
        return sum(1 for j in self.jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING))

    def remove(self, job_id: str) -> bool:
        """Drop a job that isn't running (cancel a running one first)."""
        job = self.find(job_id)
        if job is None or job.status == JobStatus.RUNNING:
            return False
        self.jobs.remove(job)
        return True

    def cancel_queued(self, job_id: str) -> bool:
        """Mark a waiting job cancelled (it stays listed, and won't run)."""
        job = self.find(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return False
        job.status = JobStatus.CANCELLED
        job.message = "Cancelled before it started."
        return True

    def move(self, job_id: str, step: int) -> bool:
        """Move a waiting job up (-1) or down (+1) among the jobs."""
        job = self.find(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return False
        i = self.jobs.index(job)
        j = i + step
        if not (0 <= j < len(self.jobs)):
            return False
        self.jobs[i], self.jobs[j] = self.jobs[j], self.jobs[i]
        return True

    def requeue(self, job_id: str) -> bool:
        """Run a failed or cancelled job again (same snapshot and settings)."""
        job = self.find(job_id)
        if job is None or job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        job.status, job.progress, job.message, job.warnings = JobStatus.QUEUED, 0.0, "", []
        return True

    def clear_finished(self) -> int:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if not j.is_finished]
        return before - len(self.jobs)

    # -- runner callbacks --------------------------------------------------
    def mark_started(self, job: RenderJob) -> None:
        job.status, job.progress, job.message = JobStatus.RUNNING, 0.0, "Starting..."

    def mark_progress(self, job: RenderJob, fraction: float, message: str) -> None:
        job.progress, job.message = max(0.0, min(1.0, fraction)), message

    def mark_done(self, job: RenderJob, warnings: Optional[List[str]] = None) -> None:
        job.status, job.progress, job.message = JobStatus.DONE, 1.0, f"Saved to {job.output_path}"
        job.warnings = list(warnings or [])

    def mark_failed(self, job: RenderJob, message: str, cancelled: bool = False) -> None:
        job.status = JobStatus.CANCELLED if cancelled else JobStatus.FAILED
        job.message = message
