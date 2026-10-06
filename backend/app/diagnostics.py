from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.stats import chi2

from .analysis import JobManager
from .models import (
    DiagnosticProgress,
    DiagnosticResult,
    DiagnosticSpec,
    EventStudyMethod,
    EventStudyPoint,
    EventStudyResult,
    JobStatus,
    NeverTreatedMode,
    PlaceboDraw,
    PlaceboMethod,
    PlaceboResult,
    StandardErrorType,
    TreatmentInputMode,
)
from .session_store import DatasetSession, SessionStore


class DiagnosticError(RuntimeError):
    pass


@dataclass
class DiagnosticJob:
    id: str
    kind: Literal["event_study", "placebo"]
    spec: DiagnosticSpec
    total: int
    status: JobStatus = JobStatus.queued
    stage: str = ""
    method: str = ""
    completed: int = 0
    successful: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.monotonic)
    message: str = ""
    result: DiagnosticResult | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_accessed: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    estimated_memory_bytes: int = 2048
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def touch(self, now: float | None = None) -> None:
        self.last_accessed = time.monotonic() if now is None else now

    def finish_cache_metadata(self) -> None:
        now = time.monotonic()
        self.finished_at = now
        self.last_accessed = now
        if self.result is not None:
            self.estimated_memory_bytes = len(self.result.model_dump_json().encode("utf-8"))

    def progress(self) -> DiagnosticProgress:
        with self.lock:
            return DiagnosticProgress(
                job_id=self.id,
                kind=self.kind,
                status=self.status,
                stage=self.stage,
                method=self.method,
                total=self.total,
                completed=self.completed,
                successful=self.successful,
                failed=self.failed,
                elapsed_seconds=round(time.monotonic() - self.started_at, 2),
                message=self.message,
            )


class DiagnosticJobManager:
    def __init__(self, sessions: SessionStore, analysis_jobs: JobManager) -> None:
        self.sessions = sessions
        self.analysis_jobs = analysis_jobs
        self.jobs: dict[str, DiagnosticJob] = {}
        self.lock = threading.RLock()

    def preview(self, spec: DiagnosticSpec) -> dict[str, Any]:
        analysis_job, session, model = self._resolve(spec)
        frame, sample, warnings = prepare_diagnostic_frame(
            session, analysis_job.result.spec, model, spec
        )
        return {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "rows": len(frame),
            "units": int(frame[spec.panel_id].nunique()),
            "periods": int(frame[spec.time_variable].nunique()),
            "clusters": int(frame[analysis_job.result.spec.cluster_variable].nunique()),
            "treated_units": int(frame.loc[frame["_diag_g"] > 0, spec.panel_id].nunique()),
            "never_treated_units": int(frame.loc[frame["_diag_g"] == 0, spec.panel_id].nunique()),
            "event_models": len(spec.event_methods),
            "placebo_regressions": len(spec.placebo_methods) * spec.repetitions,
            "sample": sample,
        }

    def start(self, kind: Literal["event_study", "placebo"], spec: DiagnosticSpec) -> DiagnosticJob:
        self.preview(spec)
        if kind == "event_study" and not spec.event_methods:
            raise ValueError("Select at least one event-study method")
        if kind == "placebo" and not spec.placebo_methods:
            raise ValueError("Select at least one placebo method")
        total = (
            len(spec.event_methods)
            if kind == "event_study"
            else len(spec.placebo_methods) * spec.repetitions
        )
        job = DiagnosticJob(uuid.uuid4().hex, kind, spec, total)
        with self.lock:
            if any(
                item.status in {JobStatus.queued, JobStatus.running}
                for item in self.jobs.values()
            ):
                raise DiagnosticError("Another diagnostic job is already running")
            self.jobs[job.id] = job
        threading.Thread(
            target=self._run, args=(job,), daemon=True, name=f"diagnostic-{job.id[:8]}"
        ).start()
        return job

    def get(self, job_id: str) -> DiagnosticJob:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            job.touch()
            return job

    def peek(self, job_id: str) -> DiagnosticJob:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def items(self) -> list[tuple[str, DiagnosticJob]]:
        with self.lock:
            return list(self.jobs.items())

    def remove(self, job_id: str) -> DiagnosticJob:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs.pop(job_id)

    def cleanup(self) -> None:
        with self.lock:
            for job in self.jobs.values():
                if job.status in {JobStatus.queued, JobStatus.running}:
                    job.cancel_event.set()
            self.jobs.clear()

    def cancel(self, job_id: str) -> None:
        self.get(job_id).cancel_event.set()

    def _resolve(self, spec: DiagnosticSpec):
        try:
            analysis_job = self.analysis_jobs.get(spec.analysis_job_id)
        except KeyError as exc:
            raise DiagnosticError("The source analysis job was not found") from exc
        if analysis_job.status != JobStatus.completed or not analysis_job.result:
            raise DiagnosticError("The source analysis job has not completed")
        result = analysis_job.result
        session = self.sessions.get(result.spec.dataset_id)
        if session.revision != result.dataset_revision:
            raise DiagnosticError("The dataset changed after the source analysis; rerun it first")
        if (
            result.spec.standard_error != StandardErrorType.cluster
            or not result.spec.cluster_variable
        ):
            raise DiagnosticError("Diagnostics require a clustered source model")
        model = next((item for item in result.top_models if item.rank == spec.model_rank), None)
        if model is None:
            raise DiagnosticError("The selected Top model was not found")
        return analysis_job, session, model

    def _run(self, job: DiagnosticJob) -> None:
        with job.lock:
            job.status = JobStatus.running
            job.started_at = time.monotonic()
            job.stage = "prepare"
            job.message = "Validating the panel and treatment timing"
        try:
            analysis_job, session, model = self._resolve(job.spec)
            analysis_spec = analysis_job.result.spec
            frame, sample, warnings = prepare_diagnostic_frame(
                session, analysis_spec, model, job.spec
            )
            controls = list(dict.fromkeys([*analysis_spec.required_controls, *model.controls]))
            fixed_effects = list(
                dict.fromkeys(
                    [job.spec.panel_id, job.spec.time_variable, *job.spec.extra_fixed_effects]
                )
            )
            cluster = analysis_spec.cluster_variable
            event_results: list[EventStudyResult] = []
            placebo_results: list[PlaceboResult] = []
            if job.kind == "event_study":
                for method in job.spec.event_methods:
                    self._check_cancel(job)
                    with job.lock:
                        job.stage = "event_study"
                        job.method = method.value
                        job.message = f"Estimating {method.value} event study"
                    event_results.append(
                        estimate_event_study(
                            frame,
                            analysis_spec.dependent,
                            controls,
                            fixed_effects,
                            cluster,
                            job.spec,
                            method,
                            warnings,
                        )
                    )
                    with job.lock:
                        job.completed += 1
                        job.successful += 1
            else:
                actual_att = estimate_overall_att(
                    frame,
                    analysis_spec.dependent,
                    controls,
                    fixed_effects,
                    cluster,
                    job.spec.placebo_estimator,
                )
                seeds = np.random.SeedSequence(job.spec.random_seed).spawn(3)
                seed_map = {
                    PlaceboMethod.random_group: seeds[0],
                    PlaceboMethod.random_timing: seeds[1],
                    PlaceboMethod.permute_outcome: seeds[2],
                }
                for method in job.spec.placebo_methods:
                    self._check_cancel(job)
                    placebo_results.append(
                        run_placebo(
                            frame,
                            analysis_spec.dependent,
                            controls,
                            fixed_effects,
                            cluster,
                            job.spec,
                            method,
                            actual_att,
                            seed_map[method],
                            job,
                        )
                    )
            result = DiagnosticResult(
                job_id=job.id,
                kind=job.kind,
                spec=job.spec,
                dataset_revision=session.revision,
                model=model,
                sample=sample,
                event_studies=event_results,
                placebos=placebo_results,
                completed_at=datetime.now(UTC).isoformat(),
            )
            with job.lock:
                job.result = result
                job.status = JobStatus.completed
                job.stage = "completed"
                job.message = "Completed"
                job.finish_cache_metadata()
        except CancelledError:
            with job.lock:
                job.status = JobStatus.cancelled
                job.message = "Cancelled; incomplete diagnostic results were discarded"
                job.finish_cache_metadata()
        except Exception as exc:
            with job.lock:
                job.status = JobStatus.failed
                job.message = str(exc)
                job.finish_cache_metadata()

    @staticmethod
    def _check_cancel(job: DiagnosticJob) -> None:
        if job.cancel_event.is_set():
            raise CancelledError


class CancelledError(RuntimeError):
    pass


def prepare_diagnostic_frame(
    session: DatasetSession, analysis_spec: Any, model: Any, spec: DiagnosticSpec
) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
    source = session.filtered_frame
    controls = list(dict.fromkeys([*analysis_spec.required_controls, *model.controls]))
    fixed_effects = list(dict.fromkeys(spec.extra_fixed_effects))
    invalid_fixed = set(fixed_effects) & {
        analysis_spec.dependent,
        analysis_spec.core,
        spec.treatment.variable,
        *controls,
    }
    if invalid_fixed:
        raise DiagnosticError(
            "Additional fixed effects cannot duplicate regression roles: "
            + ", ".join(sorted(invalid_fixed))
        )
    variables = list(
        dict.fromkeys(
            [
                analysis_spec.dependent,
                *controls,
                spec.panel_id,
                spec.time_variable,
                spec.treatment.variable,
                analysis_spec.cluster_variable,
                *fixed_effects,
            ]
        )
    )
    missing = [name for name in variables if name not in source.columns]
    if missing:
        raise DiagnosticError(f"Unknown diagnostic variables: {', '.join(missing)}")
    if source.duplicated([spec.panel_id, spec.time_variable]).any():
        count = int(source.duplicated([spec.panel_id, spec.time_variable], keep=False).sum())
        raise DiagnosticError(f"Panel ID-time pairs must be unique; {count} duplicate rows found")
    time_values = source[spec.time_variable]
    if not pd.api.types.is_numeric_dtype(time_values):
        raise DiagnosticError("The time variable must be numeric")
    finite_time = time_values.dropna().astype(float)
    if finite_time.empty or not np.allclose(finite_time, np.round(finite_time)):
        raise DiagnosticError("The time variable must use integer period codes")
    unique_periods = np.sort(finite_time.unique())
    if len(unique_periods) < 3:
        raise DiagnosticError("At least three time periods are required")
    if len(unique_periods) > 1 and not np.all(np.diff(unique_periods) == 1):
        raise DiagnosticError("Time codes must increase in consecutive integer periods")
    frame = source[variables].copy()
    frame[spec.time_variable] = frame[spec.time_variable].astype("Int64")
    frame["_diag_g"] = derive_treatment_time(frame, spec)
    required_complete = [
        analysis_spec.dependent,
        *controls,
        spec.panel_id,
        spec.time_variable,
        analysis_spec.cluster_variable,
        *fixed_effects,
    ]
    common = frame[required_complete].notna().all(axis=1) & frame["_diag_g"].notna()
    frame = frame.loc[common].copy()
    if frame.empty:
        raise DiagnosticError("No complete observations remain for diagnostics")
    frame[spec.time_variable] = frame[spec.time_variable].astype(int)
    frame["_diag_g"] = frame["_diag_g"].astype(int)
    frame["_diag_event_raw"] = np.where(
        frame["_diag_g"] > 0,
        frame[spec.time_variable] - frame["_diag_g"],
        np.nan,
    )
    frame["_diag_event"] = frame["_diag_event_raw"].clip(spec.window_start, spec.window_end)
    frame["_diag_treated"] = (
        (frame["_diag_g"] > 0) & (frame[spec.time_variable] >= frame["_diag_g"])
    ).astype(int)
    if not (frame["_diag_g"] > 0).any():
        raise DiagnosticError("At least one treated unit is required")
    if not (frame["_diag_treated"] == 0).any():
        raise DiagnosticError("Untreated or not-yet-treated observations are required")
    clusters = int(frame[analysis_spec.cluster_variable].nunique())
    if clusters < 2:
        raise DiagnosticError("At least two clusters are required")
    warnings: list[str] = []
    if clusters < 30:
        warnings.append("The diagnostic sample has fewer than 30 clusters")
    observed_events = set(frame.loc[frame["_diag_g"] > 0, "_diag_event"].dropna().astype(int))
    if not any(value <= -2 for value in observed_events):
        warnings.append("No estimable pre-treatment event period is available")
    sample = {
        "source_rows": len(session.frame),
        "filtered_rows": len(source),
        "diagnostic_rows": len(frame),
        "units": int(frame[spec.panel_id].nunique()),
        "periods": int(frame[spec.time_variable].nunique()),
        "clusters": clusters,
        "dropped_for_diagnostic_sample": int(len(source) - len(frame)),
        "window_start": spec.window_start,
        "window_end": spec.window_end,
        "baseline": -1,
    }
    return frame, sample, warnings


def derive_treatment_time(frame: pd.DataFrame, spec: DiagnosticSpec) -> pd.Series:
    id_name = spec.panel_id
    time_name = spec.time_variable
    source_name = spec.treatment.variable
    if spec.treatment.mode == TreatmentInputMode.indicator:
        values = frame[source_name]
        nonmissing = values.dropna()
        if not nonmissing.isin([0, 1, False, True]).all():
            raise DiagnosticError("The treatment indicator must contain only 0 and 1")
        ordered = frame.sort_values([id_name, time_name])
        for _, group in ordered.groupby(id_name, sort=False):
            sequence = group[source_name].fillna(0).astype(int).to_numpy()
            if np.any(np.diff(sequence) < 0):
                raise DiagnosticError("Treatment indicators must remain one after treatment begins")
        first = (
            ordered.loc[ordered[source_name].fillna(0).astype(int) == 1]
            .groupby(id_name)[time_name]
            .min()
        )
        return frame[id_name].map(first).fillna(0)
    raw = frame[source_name].copy()
    if not pd.api.types.is_numeric_dtype(raw):
        raise DiagnosticError("The first-treatment variable must be numeric")
    if spec.treatment.never_treated_mode == NeverTreatedMode.missing:
        normalized = raw.fillna(0)
    elif spec.treatment.never_treated_mode == NeverTreatedMode.custom:
        normalized = raw.mask(raw == spec.treatment.never_treated_value, 0)
    else:
        normalized = raw.fillna(0)
    by_id = frame.assign(_g=normalized).groupby(id_name)["_g"]
    if (by_id.nunique(dropna=False) > 1).any():
        raise DiagnosticError("First-treatment time must be constant within each panel unit")
    g = frame[id_name].map(by_id.first())
    finite = g.dropna().astype(float)
    if not np.allclose(finite, np.round(finite)):
        raise DiagnosticError("First-treatment time must use integer period codes")
    observed_periods = set(frame[time_name].dropna().astype(int))
    invalid = sorted(set(finite.astype(int)) - observed_periods - {0})
    if invalid:
        raise DiagnosticError(f"Treatment times are not observed periods: {invalid[:5]}")
    return g.fillna(0)


def _formula(terms: list[str], fixed_effects: list[str]) -> str:
    rhs = " + ".join(terms) if terms else "0"
    return f"~ {rhs} | {' + '.join(fixed_effects)}"


def _fit_terms(fit: Any) -> tuple[list[str], np.ndarray, np.ndarray, pd.DataFrame]:
    tidy = fit.tidy()
    names = [str(value) for value in tidy.index]
    estimates = tidy["Estimate"].to_numpy(dtype=float)
    covariance = np.asarray(fit._vcov, dtype=float)
    return names, estimates, covariance, tidy


def _point_rows(
    event_values: list[int], names: list[str], estimates: np.ndarray, covariance: np.ndarray
) -> tuple[list[EventStudyPoint], np.ndarray]:
    index = {name: position for position, name in enumerate(names)}
    points: list[EventStudyPoint] = []
    transform = np.zeros((len(event_values), len(names)))
    for row_index, event_time in enumerate(event_values):
        if event_time == -1:
            points.append(
                EventStudyPoint(
                    event_time=-1, estimate=0, std_error=0, conf_low=0, conf_high=0, weight=1
                )
            )
            continue
        term = event_name(event_time)
        if term not in index:
            points.append(
                EventStudyPoint(
                    event_time=event_time,
                    estimate=None,
                    std_error=None,
                    conf_low=None,
                    conf_high=None,
                    identifiable=False,
                )
            )
            continue
        position = index[term]
        transform[row_index, position] = 1
        estimate = float(estimates[position])
        se = float(math.sqrt(max(covariance[position, position], 0)))
        points.append(
            EventStudyPoint(
                event_time=event_time,
                estimate=estimate,
                std_error=se,
                conf_low=estimate - 1.96 * se,
                conf_high=estimate + 1.96 * se,
                weight=1,
            )
        )
    return points, transform @ covariance @ transform.T


def event_name(event_time: int) -> str:
    return f"_diag_ev_{'m' + str(abs(event_time)) if event_time < 0 else 'p' + str(event_time)}"


def cohort_event_name(cohort: int, event_time: int) -> str:
    event = "m" + str(abs(event_time)) if event_time < 0 else "p" + str(event_time)
    return f"_diag_c{cohort}_{event}"


def estimate_event_study(
    frame: pd.DataFrame,
    outcome: str,
    controls: list[str],
    fixed_effects: list[str],
    cluster: str,
    spec: DiagnosticSpec,
    method: EventStudyMethod,
    shared_warnings: list[str],
) -> EventStudyResult:
    import pyfixest as pf

    work = frame.copy()
    events = list(range(spec.window_start, spec.window_end + 1))
    if method == EventStudyMethod.did2s:
        terms: list[str] = []
        for event_time in events:
            if event_time == -1:
                continue
            name = event_name(event_time)
            work[name] = ((work["_diag_g"] > 0) & (work["_diag_event"] == event_time)).astype(int)
            if work[name].sum() > 0:
                terms.append(name)
        if not terms:
            raise DiagnosticError("No event-time coefficient is identifiable for DID2S")
        fit = pf.did2s(
            data=work,
            yname=outcome,
            first_stage=_formula(controls, fixed_effects),
            second_stage="~ 0 + " + " + ".join(terms),
            treatment="_diag_treated",
            cluster=cluster,
        )
        names, estimates, covariance, _ = _fit_terms(fit)
        points, point_covariance = _point_rows(events, names, estimates, covariance)
    else:
        terms = []
        term_metadata: dict[str, tuple[int, int, int]] = {}
        cohorts = sorted(int(value) for value in work.loc[work["_diag_g"] > 0, "_diag_g"].unique())
        for cohort in cohorts:
            for event_time in events:
                if event_time == -1:
                    continue
                name = cohort_event_name(cohort, event_time)
                mask = (work["_diag_g"] == cohort) & (work["_diag_event"] == event_time)
                count = int(mask.sum())
                if count:
                    work[name] = mask.astype(int)
                    terms.append(name)
                    term_metadata[name] = (cohort, event_time, count)
        if not terms:
            raise DiagnosticError("No cohort-event coefficient is identifiable")
        formula = f"{outcome} " + _formula([*terms, *controls], fixed_effects)
        fit = pf.feols(formula, data=work, vcov={"CRV1": cluster}, fixef_rm="singleton")
        names, estimates, covariance, _ = _fit_terms(fit)
        name_index = {name: position for position, name in enumerate(names)}
        transform = np.zeros((len(events), len(names)))
        points = []
        for event_index, event_time in enumerate(events):
            if event_time == -1:
                points.append(
                    EventStudyPoint(
                        event_time=-1, estimate=0, std_error=0, conf_low=0, conf_high=0, weight=1
                    )
                )
                continue
            available = [
                (name, metadata[2])
                for name, metadata in term_metadata.items()
                if metadata[1] == event_time and name in name_index
            ]
            total_weight = sum(count for _, count in available)
            if not total_weight:
                points.append(
                    EventStudyPoint(
                        event_time=event_time,
                        estimate=None,
                        std_error=None,
                        conf_low=None,
                        conf_high=None,
                        identifiable=False,
                    )
                )
                continue
            for name, count in available:
                transform[event_index, name_index[name]] = count / total_weight
            estimate = float(transform[event_index] @ estimates)
            variance = float(transform[event_index] @ covariance @ transform[event_index])
            se = math.sqrt(max(variance, 0))
            points.append(
                EventStudyPoint(
                    event_time=event_time,
                    estimate=estimate,
                    std_error=se,
                    conf_low=estimate - 1.96 * se,
                    conf_high=estimate + 1.96 * se,
                    weight=float(total_weight),
                )
            )
        point_covariance = transform @ covariance @ transform.T
    statistic, degrees, p_value = (
        pretrend_test(points, point_covariance) if spec.run_pretrend_test else (None, None, None)
    )
    return EventStudyResult(
        method=method,
        points=points,
        observations=len(work),
        clusters=int(work[cluster].nunique()),
        pretrend_statistic=statistic,
        pretrend_df=degrees,
        pretrend_p_value=p_value,
        warnings=list(shared_warnings),
    )


def pretrend_test(
    points: list[EventStudyPoint], covariance: np.ndarray
) -> tuple[float | None, int | None, float | None]:
    indices = [
        index
        for index, point in enumerate(points)
        if point.event_time <= -2 and point.identifiable and point.estimate is not None
    ]
    if not indices:
        return None, None, None
    beta = np.array([points[index].estimate for index in indices], dtype=float)
    vcov = covariance[np.ix_(indices, indices)]
    inverse = np.linalg.pinv(vcov)
    statistic = float(beta.T @ inverse @ beta)
    degrees = len(indices)
    return statistic, degrees, float(chi2.sf(statistic, degrees))


def estimate_overall_att(
    frame: pd.DataFrame,
    outcome: str,
    controls: list[str],
    fixed_effects: list[str],
    cluster: str,
    estimator: EventStudyMethod,
) -> float:
    import pyfixest as pf

    work = frame.copy()
    if estimator == EventStudyMethod.did2s:
        fit = pf.did2s(
            data=work,
            yname=outcome,
            first_stage=_formula(controls, fixed_effects),
            second_stage="~ 0 + _diag_treated",
            treatment="_diag_treated",
            cluster=cluster,
        )
        tidy = fit.tidy()
        return float(tidy.loc["_diag_treated", "Estimate"])
    cohorts = sorted(int(value) for value in work.loc[work["_diag_g"] > 0, "_diag_g"].unique())
    terms: list[str] = []
    counts: dict[str, int] = {}
    for cohort in cohorts:
        name = f"_diag_att_c{cohort}"
        mask = (work["_diag_g"] == cohort) & (work["_diag_treated"] == 1)
        count = int(mask.sum())
        if count:
            work[name] = mask.astype(int)
            terms.append(name)
            counts[name] = count
    if not terms:
        raise DiagnosticError("No treated observations are available for the ATT")
    fit = pf.feols(
        f"{outcome} " + _formula([*terms, *controls], fixed_effects),
        data=work,
        vcov={"CRV1": cluster},
        fixef_rm="singleton",
    )
    tidy = fit.tidy()
    available = [name for name in terms if name in tidy.index]
    total = sum(counts[name] for name in available)
    if not total:
        raise DiagnosticError("No cohort ATT is identifiable")
    return float(
        sum(float(tidy.loc[name, "Estimate"]) * counts[name] / total for name in available)
    )


def run_placebo(
    frame: pd.DataFrame,
    outcome: str,
    controls: list[str],
    fixed_effects: list[str],
    cluster: str,
    spec: DiagnosticSpec,
    method: PlaceboMethod,
    actual_att: float,
    seed: np.random.SeedSequence,
    job: DiagnosticJob,
) -> PlaceboResult:
    rng = np.random.default_rng(seed)
    draws: list[PlaceboDraw] = []
    attempted = 0
    failed = 0
    maximum = spec.repetitions * 5
    with job.lock:
        job.stage = "placebo"
        job.method = method.value
        job.message = f"Running {method.value} placebo repetitions"
    while len(draws) < spec.repetitions and attempted < maximum:
        if job.cancel_event.is_set():
            raise CancelledError
        attempted += 1
        placebo = randomize_frame(frame, outcome, spec, method, rng)
        try:
            estimate = estimate_overall_att(
                placebo,
                outcome,
                controls,
                fixed_effects,
                cluster,
                spec.placebo_estimator,
            )
            if math.isfinite(estimate):
                draws.append(PlaceboDraw(iteration=len(draws) + 1, estimate=estimate))
                with job.lock:
                    job.completed += 1
                    job.successful += 1
            else:
                failed += 1
                with job.lock:
                    job.completed += 1
                    job.failed += 1
        except Exception:
            failed += 1
            with job.lock:
                job.completed += 1
                job.failed += 1
    if len(draws) < spec.repetitions:
        raise DiagnosticError(
            f"{method.value} produced only {len(draws)} valid estimates after {attempted} attempts"
        )
    values = np.array([draw.estimate for draw in draws], dtype=float)
    empirical = float((1 + np.sum(np.abs(values) >= abs(actual_att))) / (len(values) + 1))
    return PlaceboResult(
        method=method,
        estimator=spec.placebo_estimator,
        actual_att=actual_att,
        draws=draws,
        mean=float(values.mean()),
        std_dev=float(values.std(ddof=1)),
        quantile_low=float(np.quantile(values, 0.025)),
        quantile_high=float(np.quantile(values, 0.975)),
        empirical_p_value=empirical,
        attempted=attempted,
        failed=failed,
    )


def randomize_frame(
    frame: pd.DataFrame,
    outcome: str,
    spec: DiagnosticSpec,
    method: PlaceboMethod,
    rng: np.random.Generator,
) -> pd.DataFrame:
    work = frame.copy()
    id_name = spec.panel_id
    time_name = spec.time_variable
    unit_g = work.groupby(id_name)["_diag_g"].first()
    if method == PlaceboMethod.random_group:
        shuffled = rng.permutation(unit_g.to_numpy())
        mapping = dict(zip(unit_g.index, shuffled, strict=True))
        work["_diag_g"] = work[id_name].map(mapping).astype(int)
    elif method == PlaceboMethod.random_timing:
        treated = unit_g[unit_g > 0]
        shuffled = rng.permutation(treated.to_numpy())
        mapping = unit_g.to_dict()
        mapping.update(dict(zip(treated.index, shuffled, strict=True)))
        work["_diag_g"] = work[id_name].map(mapping).astype(int)
    else:
        groups: dict[tuple[int, ...], list[Any]] = {}
        for unit, values in work.groupby(id_name)[time_name]:
            signature = tuple(sorted(int(value) for value in values))
            groups.setdefault(signature, []).append(unit)
        eligible = False
        original = work[[id_name, time_name, outcome]].copy()
        for units in groups.values():
            if len(units) < 2:
                continue
            eligible = True
            donors = rng.permutation(units)
            for target, donor in zip(units, donors, strict=True):
                donor_values = original.loc[original[id_name] == donor, [time_name, outcome]]
                value_map = donor_values.set_index(time_name)[outcome]
                mask = work[id_name] == target
                work.loc[mask, outcome] = work.loc[mask, time_name].map(value_map).to_numpy()
        if not eligible:
            raise DiagnosticError(
                "No units share the same time-support pattern for trajectory permutation"
            )
    if method != PlaceboMethod.permute_outcome:
        work["_diag_event_raw"] = np.where(
            work["_diag_g"] > 0, work[time_name] - work["_diag_g"], np.nan
        )
        work["_diag_event"] = work["_diag_event_raw"].clip(spec.window_start, spec.window_end)
        work["_diag_treated"] = (
            (work["_diag_g"] > 0) & (work[time_name] >= work["_diag_g"])
        ).astype(int)
    return work
