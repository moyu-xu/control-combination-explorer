from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .analysis import AnalysisJob, JobManager
from .diagnostics import DiagnosticJob, DiagnosticJobManager
from .models import JobStatus
from .session_store import SessionStore

CACHE_RETENTION_SECONDS = 3600
CACHE_HISTORY_BUDGET_BYTES = 512 * 1024 * 1024
CACHE_SWEEP_SECONDS = 60
CACHE_LEASE_SECONDS = 90
TOMBSTONE_SECONDS = 3600

ResourceKind = Literal["dataset", "analysis", "diagnostic"]
TERMINAL = {JobStatus.completed, JobStatus.cancelled, JobStatus.failed}


class CacheManager:
    def __init__(
        self,
        sessions: SessionStore,
        jobs: JobManager,
        diagnostic_jobs: DiagnosticJobManager,
        *,
        clock: Callable[[], float] = time.monotonic,
        retention_seconds: int = CACHE_RETENTION_SECONDS,
        history_budget_bytes: int = CACHE_HISTORY_BUDGET_BYTES,
        lease_seconds: int = CACHE_LEASE_SECONDS,
    ) -> None:
        self.sessions = sessions
        self.jobs = jobs
        self.diagnostic_jobs = diagnostic_jobs
        self.clock = clock
        self.retention_seconds = retention_seconds
        self.history_budget_bytes = history_budget_bytes
        self.lease_seconds = lease_seconds
        self.lock = threading.RLock()
        self.leases: dict[tuple[ResourceKind, str], float] = {}
        self.tombstones: dict[tuple[ResourceKind, str], float] = {}
        self.retired_datasets: set[str] = set()
        self.last_cleanup_at: str | None = None
        self.last_reclaimed_bytes = 0
        self.last_reclaimed_resources = 0
        self.total_reclaimed_bytes = 0

    def lease(
        self,
        dataset_id: str | None,
        analysis_job_ids: list[str],
        diagnostic_job_ids: list[str],
    ) -> dict[str, Any]:
        now = self.clock()
        expiry = now + self.lease_seconds
        requested: set[tuple[ResourceKind, str]] = set()
        if dataset_id:
            requested.add(("dataset", dataset_id))
        requested.update(("analysis", item) for item in analysis_job_ids)
        requested.update(("diagnostic", item) for item in diagnostic_job_ids)
        missing: list[dict[str, Any]] = []
        with self.lock:
            self._expire_metadata(now)
            for key in requested:
                if self._exists(*key) and not self._belongs_to_retired_dataset(*key):
                    self.leases[key] = expiry
                    self._touch(*key, now=now)
                else:
                    missing.append(
                        {
                            "kind": key[0],
                            "id": key[1],
                            "expired": key in self.tombstones,
                        }
                    )
            protected = self._protected(now)
            for kind, resource_id in protected:
                if (
                    (kind, resource_id) in requested
                    and not self._belongs_to_retired_dataset(kind, resource_id)
                ):
                    self.leases[(kind, resource_id)] = expiry
        status = self.status()
        status["missing_resources"] = missing
        return status

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            self._expire_metadata(now)
            protected = self._protected(now)
            resources = self._resources()
            total_memory = sum(item[3] for item in resources)
            protected_memory = sum(
                item[3] for item in resources if (item[0], item[1]) in protected
            )
            historical_memory = max(0, total_memory - protected_memory)
            disk_bytes = sum(session.size_bytes for _, session in self.sessions.items())
            counts = {
                "datasets": len(self.sessions.items()),
                "analysis_jobs": len(self.jobs.items()),
                "diagnostic_jobs": len(self.diagnostic_jobs.items()),
            }
            reclaimable = self._manual_candidates(protected)
            reclaimable_bytes = sum(item[3] for item in reclaimable)
            if protected_memory > self.history_budget_bytes:
                state = "protected_over_budget"
            elif historical_memory >= int(self.history_budget_bytes * 0.8):
                state = "near_limit"
            else:
                state = "normal"
            return {
                "state": state,
                "estimated_total_bytes": total_memory,
                "protected_bytes": protected_memory,
                "historical_bytes": historical_memory,
                "reclaimable_bytes": reclaimable_bytes,
                "disk_bytes": disk_bytes,
                "history_budget_bytes": self.history_budget_bytes,
                "retention_seconds": self.retention_seconds,
                "sweep_seconds": CACHE_SWEEP_SECONDS,
                "lease_seconds": self.lease_seconds,
                "counts": counts,
                "reclaimable_resources": len(reclaimable),
                "last_cleanup_at": self.last_cleanup_at,
                "last_reclaimed_bytes": self.last_reclaimed_bytes,
                "last_reclaimed_resources": self.last_reclaimed_resources,
                "total_reclaimed_bytes": self.total_reclaimed_bytes,
                "pending_retired_datasets": len(self.retired_datasets),
            }

    def preview_manual_cleanup(self) -> dict[str, Any]:
        status = self.status()
        return {
            "resources": status["reclaimable_resources"],
            "estimated_bytes": status["reclaimable_bytes"],
        }

    def cleanup(self, *, manual: bool = False) -> dict[str, Any]:
        now = self.clock()
        reclaimed_bytes = 0
        reclaimed_resources = 0
        with self.lock:
            self._expire_metadata(now)
            protected = self._protected(now)
            if manual:
                candidates = self._manual_candidates(protected)
            else:
                cutoff = now - self.retention_seconds
                candidates = [
                    item
                    for item in self._manual_candidates(protected)
                    if self._is_expired_candidate(item, cutoff)
                    or (item[0] == "dataset" and item[1] in self.retired_datasets)
                ]
            for kind, resource_id, _, _ in candidates:
                removed_bytes, removed_count = self._remove(kind, resource_id, protected)
                reclaimed_bytes += removed_bytes
                reclaimed_resources += removed_count

            protected = self._protected(now)
            while self._historical_bytes(protected) > self.history_budget_bytes:
                budget_candidates = self._manual_candidates(protected)
                if not budget_candidates:
                    break
                kind, resource_id, _, _ = budget_candidates[0]
                removed_bytes, removed_count = self._remove(kind, resource_id, protected)
                if not removed_count:
                    break
                reclaimed_bytes += removed_bytes
                reclaimed_resources += removed_count

            self.last_cleanup_at = datetime.now(UTC).isoformat()
            self.last_reclaimed_bytes = reclaimed_bytes
            self.last_reclaimed_resources = reclaimed_resources
            self.total_reclaimed_bytes += reclaimed_bytes
            status = self.status()
            return {
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_resources": reclaimed_resources,
                "status": status,
            }

    def retire_dataset(self, dataset_id: str) -> dict[str, Any]:
        with self.lock:
            if not self._exists("dataset", dataset_id):
                return {"reclaimed_bytes": 0, "reclaimed_resources": 0, "status": self.status()}
            self.retired_datasets.add(dataset_id)
            self._clear_chain_leases(dataset_id)
            for _, diagnostic in self.diagnostic_jobs.items():
                analysis = self._analysis_for_diagnostic(diagnostic)
                if analysis and analysis.spec.dataset_id == dataset_id:
                    diagnostic.cancel_event.set()
            for _, job in self.jobs.items():
                if job.spec.dataset_id == dataset_id:
                    job.cancel_event.set()
        return self.cleanup()

    def is_expired(self, kind: ResourceKind, resource_id: str) -> bool:
        now = self.clock()
        with self.lock:
            self._expire_metadata(now)
            return (kind, resource_id) in self.tombstones

    def shutdown(self) -> None:
        with self.lock:
            self.diagnostic_jobs.cleanup()
            self.jobs.cleanup()
            self.sessions.cleanup()
            self.leases.clear()
            self.tombstones.clear()
            self.retired_datasets.clear()
        matplotlib_cache = Path(tempfile.gettempdir()) / "control-combination-matplotlib"
        shutil.rmtree(matplotlib_cache, ignore_errors=True)

    def _resources(self) -> list[tuple[ResourceKind, str, float, int]]:
        resources: list[tuple[ResourceKind, str, float, int]] = []
        resources.extend(
            ("diagnostic", resource_id, job.last_accessed, job.estimated_memory_bytes)
            for resource_id, job in self.diagnostic_jobs.items()
        )
        resources.extend(
            ("analysis", resource_id, job.last_accessed, job.estimated_memory_bytes)
            for resource_id, job in self.jobs.items()
        )
        resources.extend(
            ("dataset", resource_id, session.last_accessed, session.estimated_memory_bytes)
            for resource_id, session in self.sessions.items()
        )
        return resources

    def _manual_candidates(
        self, protected: set[tuple[ResourceKind, str]]
    ) -> list[tuple[ResourceKind, str, float, int]]:
        priority = {"diagnostic": 0, "analysis": 1, "dataset": 2}
        candidates: list[tuple[ResourceKind, str, float, int]] = []
        for item in self._resources():
            kind, resource_id, _, _ = item
            if (kind, resource_id) in protected:
                continue
            if kind == "diagnostic":
                if self.diagnostic_jobs.peek(resource_id).status not in TERMINAL:
                    continue
            elif kind == "analysis":
                if self.jobs.peek(resource_id).status not in TERMINAL:
                    continue
                if self._has_protected_diagnostic(resource_id, protected):
                    continue
            elif self._dataset_has_protected_or_running_child(resource_id, protected):
                continue
            candidates.append(item)
        return sorted(candidates, key=lambda item: (priority[item[0]], item[2]))

    def _protected(self, now: float) -> set[tuple[ResourceKind, str]]:
        protected = {key for key, expires in self.leases.items() if expires > now}
        for resource_id, job in self.jobs.items():
            if job.status in {JobStatus.queued, JobStatus.running}:
                protected.add(("analysis", resource_id))
        for resource_id, job in self.diagnostic_jobs.items():
            if job.status in {JobStatus.queued, JobStatus.running}:
                protected.add(("diagnostic", resource_id))

        changed = True
        while changed:
            changed = False
            for kind, resource_id in list(protected):
                if kind == "diagnostic" and self._exists("diagnostic", resource_id):
                    source_id = self.diagnostic_jobs.peek(resource_id).spec.analysis_job_id
                    key = ("analysis", source_id)
                    if key not in protected and self._exists(*key):
                        protected.add(key)
                        changed = True
                elif kind == "analysis" and self._exists("analysis", resource_id):
                    dataset_id = self.jobs.peek(resource_id).spec.dataset_id
                    key = ("dataset", dataset_id)
                    if key not in protected and self._exists(*key):
                        protected.add(key)
                        changed = True
        return protected

    def _historical_bytes(self, protected: set[tuple[ResourceKind, str]]) -> int:
        return sum(
            item[3] for item in self._resources() if (item[0], item[1]) not in protected
        )

    def _is_expired_candidate(
        self, item: tuple[ResourceKind, str, float, int], cutoff: float
    ) -> bool:
        kind, resource_id, last_accessed, _ = item
        if last_accessed >= cutoff:
            return False
        if kind == "diagnostic":
            return True
        if kind == "analysis":
            return all(
                diagnostic.last_accessed < cutoff
                for _, diagnostic in self.diagnostic_jobs.items()
                if diagnostic.spec.analysis_job_id == resource_id
            )
        analysis_ids = {
            analysis_id
            for analysis_id, analysis in self.jobs.items()
            if analysis.spec.dataset_id == resource_id
        }
        return all(
            analysis.last_accessed < cutoff
            for analysis_id, analysis in self.jobs.items()
            if analysis_id in analysis_ids
        ) and all(
            diagnostic.last_accessed < cutoff
            for _, diagnostic in self.diagnostic_jobs.items()
            if diagnostic.spec.analysis_job_id in analysis_ids
        )

    def _remove(
        self,
        kind: ResourceKind,
        resource_id: str,
        protected: set[tuple[ResourceKind, str]],
    ) -> tuple[int, int]:
        if (kind, resource_id) in protected or not self._exists(kind, resource_id):
            return 0, 0
        if kind == "diagnostic":
            job = self.diagnostic_jobs.peek(resource_id)
            if job.status not in TERMINAL:
                return 0, 0
            self.diagnostic_jobs.remove(resource_id)
            self._tombstone(kind, resource_id)
            return job.estimated_memory_bytes, 1
        if kind == "analysis":
            job = self.jobs.peek(resource_id)
            if job.status not in TERMINAL or self._has_protected_diagnostic(resource_id, protected):
                return 0, 0
            reclaimed = 0
            count = 0
            for diagnostic_id, diagnostic in self.diagnostic_jobs.items():
                if diagnostic.spec.analysis_job_id == resource_id:
                    removed_bytes, removed_count = self._remove(
                        "diagnostic", diagnostic_id, protected
                    )
                    reclaimed += removed_bytes
                    count += removed_count
            self.jobs.remove(resource_id)
            self._tombstone(kind, resource_id)
            return reclaimed + job.estimated_memory_bytes, count + 1

        if self._dataset_has_protected_or_running_child(resource_id, protected):
            return 0, 0
        reclaimed = 0
        count = 0
        for analysis_id, analysis in self.jobs.items():
            if analysis.spec.dataset_id == resource_id:
                removed_bytes, removed_count = self._remove("analysis", analysis_id, protected)
                reclaimed += removed_bytes
                count += removed_count
        session = self.sessions.remove(resource_id)
        self.retired_datasets.discard(resource_id)
        self._tombstone(kind, resource_id)
        return reclaimed + session.estimated_memory_bytes, count + 1

    def _dataset_has_protected_or_running_child(
        self, dataset_id: str, protected: set[tuple[ResourceKind, str]]
    ) -> bool:
        for analysis_id, analysis in self.jobs.items():
            if analysis.spec.dataset_id != dataset_id:
                continue
            if ("analysis", analysis_id) in protected or analysis.status not in TERMINAL:
                return True
            if self._has_protected_diagnostic(analysis_id, protected):
                return True
        return False

    def _has_protected_diagnostic(
        self, analysis_job_id: str, protected: set[tuple[ResourceKind, str]]
    ) -> bool:
        return any(
            diagnostic.spec.analysis_job_id == analysis_job_id
            and (
                ("diagnostic", diagnostic_id) in protected
                or diagnostic.status not in TERMINAL
            )
            for diagnostic_id, diagnostic in self.diagnostic_jobs.items()
        )

    def _analysis_for_diagnostic(self, diagnostic: DiagnosticJob) -> AnalysisJob | None:
        try:
            return self.jobs.peek(diagnostic.spec.analysis_job_id)
        except KeyError:
            return None

    def _clear_chain_leases(self, dataset_id: str) -> None:
        self.leases.pop(("dataset", dataset_id), None)
        analysis_ids = {
            resource_id
            for resource_id, job in self.jobs.items()
            if job.spec.dataset_id == dataset_id
        }
        for resource_id in analysis_ids:
            self.leases.pop(("analysis", resource_id), None)
        for resource_id, diagnostic in self.diagnostic_jobs.items():
            if diagnostic.spec.analysis_job_id in analysis_ids:
                self.leases.pop(("diagnostic", resource_id), None)

    def _touch(self, kind: ResourceKind, resource_id: str, *, now: float) -> None:
        if kind == "dataset":
            self.sessions.peek(resource_id).touch(now)
        elif kind == "analysis":
            self.jobs.peek(resource_id).touch(now)
        else:
            self.diagnostic_jobs.peek(resource_id).touch(now)

    def _belongs_to_retired_dataset(self, kind: ResourceKind, resource_id: str) -> bool:
        if kind == "dataset":
            return resource_id in self.retired_datasets
        if kind == "analysis":
            try:
                return self.jobs.peek(resource_id).spec.dataset_id in self.retired_datasets
            except KeyError:
                return False
        try:
            analysis = self.jobs.peek(
                self.diagnostic_jobs.peek(resource_id).spec.analysis_job_id
            )
            return analysis.spec.dataset_id in self.retired_datasets
        except KeyError:
            return False

    def _exists(self, kind: ResourceKind, resource_id: str) -> bool:
        try:
            if kind == "dataset":
                self.sessions.peek(resource_id)
            elif kind == "analysis":
                self.jobs.peek(resource_id)
            else:
                self.diagnostic_jobs.peek(resource_id)
            return True
        except KeyError:
            return False

    def _tombstone(self, kind: ResourceKind, resource_id: str) -> None:
        self.tombstones[(kind, resource_id)] = self.clock() + TOMBSTONE_SECONDS
        self.leases.pop((kind, resource_id), None)

    def _expire_metadata(self, now: float) -> None:
        self.leases = {key: expires for key, expires in self.leases.items() if expires > now}
        self.tombstones = {
            key: expires for key, expires in self.tombstones.items() if expires > now
        }

