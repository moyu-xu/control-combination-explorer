from __future__ import annotations

import io

import numpy as np
import pandas as pd

from backend.app.analysis import AnalysisJob, JobManager
from backend.app.cache_manager import CacheManager
from backend.app.diagnostics import DiagnosticJob, DiagnosticJobManager
from backend.app.models import (
    AnalysisSpec,
    DiagnosticSpec,
    ExpectedSign,
    JobStatus,
    TreatmentTimingSpec,
)
from backend.app.session_store import SessionStore


class Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def dta_bytes() -> bytes:
    frame = pd.DataFrame(
        {
            "firm": np.repeat(np.arange(3), 3),
            "year": np.tile(np.arange(2020, 2023), 3),
            "y": np.arange(9, dtype=float),
            "x": np.linspace(0, 1, 9),
            "g": np.repeat([2021, 2022, 0], 3),
        }
    )
    stream = io.BytesIO()
    frame.to_stata(stream, write_index=False, version=118)
    return stream.getvalue()


def analysis_spec(dataset_id: str) -> AnalysisSpec:
    return AnalysisSpec(
        dataset_id=dataset_id,
        dependent="y",
        core="x",
        expected_sign=ExpectedSign.positive,
        classification_confirmed=True,
        classification_rule_version="test",
    )


def diagnostic_spec(analysis_job_id: str) -> DiagnosticSpec:
    return DiagnosticSpec(
        analysis_job_id=analysis_job_id,
        model_rank=1,
        panel_id="firm",
        time_variable="year",
        treatment=TreatmentTimingSpec(variable="g"),
    )


def cache_fixture(*, budget: int = 512 * 1024 * 1024):
    clock = Clock()
    sessions = SessionStore()
    analyses = JobManager(sessions)
    diagnostics = DiagnosticJobManager(sessions, analyses)
    cache = CacheManager(
        sessions,
        analyses,
        diagnostics,
        clock=clock,
        history_budget_bytes=budget,
    )
    return clock, sessions, analyses, diagnostics, cache


def terminal_chain(sessions, analyses, diagnostics):
    session = sessions.create("fixture.dta", dta_bytes())
    analysis = AnalysisJob("analysis", analysis_spec(session.id), 1)
    analysis.status = JobStatus.completed
    analysis.last_accessed = 1_000
    analysis.estimated_memory_bytes = 40
    analyses.jobs[analysis.id] = analysis
    diagnostic = DiagnosticJob("diagnostic", "event_study", diagnostic_spec(analysis.id), 1)
    diagnostic.status = JobStatus.completed
    diagnostic.last_accessed = 1_000
    diagnostic.estimated_memory_bytes = 60
    diagnostics.jobs[diagnostic.id] = diagnostic
    session.last_accessed = 1_000
    return session, analysis, diagnostic


def test_active_lease_protects_the_full_dependency_chain() -> None:
    clock, sessions, analyses, diagnostics, cache = cache_fixture()
    session, analysis, diagnostic = terminal_chain(sessions, analyses, diagnostics)
    cache.lease(None, [], [diagnostic.id])
    result = cache.cleanup(manual=True)
    assert result["reclaimed_resources"] == 0
    assert sessions.peek(session.id)
    assert analyses.peek(analysis.id)
    assert diagnostics.peek(diagnostic.id)
    clock.advance(91)
    result = cache.cleanup(manual=True)
    assert result["reclaimed_resources"] == 3


def test_idle_resources_expire_only_after_one_hour() -> None:
    clock, sessions, analyses, diagnostics, cache = cache_fixture()
    session, _, _ = terminal_chain(sessions, analyses, diagnostics)
    clock.advance(3599)
    assert cache.cleanup()["reclaimed_resources"] == 0
    clock.advance(2)
    assert cache.cleanup()["reclaimed_resources"] == 3
    assert cache.is_expired("dataset", session.id)


def test_history_budget_evicts_oldest_diagnostic_first() -> None:
    _, sessions, analyses, diagnostics, cache = cache_fixture(budget=100)
    session = sessions.create("fixture.dta", dta_bytes())
    session.estimated_memory_bytes = 0
    analysis = AnalysisJob("analysis", analysis_spec(session.id), 1)
    analysis.status = JobStatus.completed
    analysis.estimated_memory_bytes = 0
    analyses.jobs[analysis.id] = analysis
    old = DiagnosticJob("old", "event_study", diagnostic_spec(analysis.id), 1)
    new = DiagnosticJob("new", "placebo", diagnostic_spec(analysis.id), 1)
    for job, accessed in [(old, 10.0), (new, 20.0)]:
        job.status = JobStatus.completed
        job.last_accessed = accessed
        job.estimated_memory_bytes = 60
        diagnostics.jobs[job.id] = job
    cache.cleanup()
    assert cache.is_expired("diagnostic", old.id)
    assert diagnostics.peek(new.id)


def test_manual_preview_matches_cleanup_without_concurrent_changes() -> None:
    _, sessions, analyses, diagnostics, cache = cache_fixture()
    session, analysis, diagnostic = terminal_chain(sessions, analyses, diagnostics)
    cache.lease(session.id, [analysis.id], [])
    preview = cache.preview_manual_cleanup()
    assert preview["resources"] == 1
    assert preview["estimated_bytes"] == diagnostic.estimated_memory_bytes
    cleaned = cache.cleanup(manual=True)
    assert cleaned["reclaimed_resources"] == preview["resources"]
    assert cleaned["reclaimed_bytes"] == preview["estimated_bytes"]


def test_retired_dataset_cannot_be_reprotected_by_a_late_heartbeat() -> None:
    _, sessions, analyses, diagnostics, cache = cache_fixture()
    session, analysis, diagnostic = terminal_chain(sessions, analyses, diagnostics)
    analysis.status = JobStatus.running

    cache.retire_dataset(session.id)
    lease_status = cache.lease(session.id, [analysis.id], [diagnostic.id])

    assert {item["id"] for item in lease_status["missing_resources"]} == {
        session.id,
        analysis.id,
        diagnostic.id,
    }
    assert lease_status["pending_retired_datasets"] == 1
    assert not any(
        resource_id in {session.id, analysis.id, diagnostic.id}
        for _, resource_id in cache.leases
    )

