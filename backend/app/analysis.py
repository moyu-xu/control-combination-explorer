from __future__ import annotations

import itertools
import math
import os
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .models import (
    AnalysisResult,
    AnalysisSpec,
    CoefficientResult,
    ControlLevel,
    ExpectedSign,
    JobProgress,
    JobStatus,
    ModelResult,
    ModelType,
    StandardErrorType,
)
from .session_store import DatasetSession, SessionStore


class ModelExcluded(RuntimeError):
    pass


def debug_failure(job_id: str, combo: tuple[str, ...], exc: Exception) -> None:
    path = os.environ.get("CCE_DEBUG_LOG")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(f"job={job_id} combo={combo!r}\n")
        stream.write("".join(traceback.format_exception(exc)))


@dataclass
class AnalysisJob:
    id: str
    spec: AnalysisSpec
    total: int
    status: JobStatus = JobStatus.queued
    completed: int = 0
    successful: int = 0
    excluded: int = 0
    started_at: float = field(default_factory=time.monotonic)
    message: str = ""
    failure_counts: Counter[str] = field(default_factory=Counter)
    result: AnalysisResult | None = None
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

    def progress(self) -> JobProgress:
        with self.lock:
            return JobProgress(
                job_id=self.id,
                status=self.status,
                total=self.total,
                completed=self.completed,
                successful=self.successful,
                excluded=self.excluded,
                elapsed_seconds=round(time.monotonic() - self.started_at, 2),
                message=self.message,
                failure_counts=dict(self.failure_counts),
            )


def validate_analysis(session: DatasetSession, spec: AnalysisSpec) -> dict[str, Any]:
    frame = session.filtered_frame
    errors: list[str] = []
    warnings: list[str] = []
    all_candidates = [*spec.firm_candidate_controls, *spec.regional_candidate_controls]
    variables = [
        spec.dependent,
        spec.core,
        *spec.required_controls,
        *all_candidates,
        *spec.fixed_other_controls,
        *spec.fixed_effects,
    ]
    if spec.cluster_variable:
        variables.append(spec.cluster_variable)
    missing_variables = sorted(set(variables) - set(frame.columns))
    if missing_variables:
        errors.append(f"Unknown variables: {', '.join(missing_variables)}")
    for variable in [
        spec.dependent,
        spec.core,
        *spec.required_controls,
        *all_candidates,
        *spec.fixed_other_controls,
    ]:
        if variable in frame and not pd.api.types.is_numeric_dtype(frame[variable]):
            errors.append(f"Regression variable must be numeric: {variable}")
    if not spec.classification_confirmed:
        errors.append("Control-variable classifications must be confirmed")
    classification_map = {item.variable: item for item in spec.control_classifications}
    classified_controls = [*spec.required_controls, *all_candidates, *spec.fixed_other_controls]
    unclassified = [name for name in classified_controls if name not in classification_map]
    if unclassified:
        errors.append(f"Missing control classifications: {', '.join(unclassified)}")
    for name in spec.firm_candidate_controls:
        item = classification_map.get(name)
        if item and item.level != ControlLevel.firm:
            errors.append(f"Firm candidate has a different confirmed level: {name}")
    for name in spec.regional_candidate_controls:
        item = classification_map.get(name)
        if item and item.level != ControlLevel.regional:
            errors.append(f"Regional candidate has a different confirmed level: {name}")
    for name in spec.fixed_other_controls:
        item = classification_map.get(name)
        if item and item.level != ControlLevel.other:
            errors.append(f"Fixed other control has a different confirmed level: {name}")

    required_firm = controls_at_level(spec.required_controls, classification_map, ControlLevel.firm)
    required_regional = controls_at_level(
        spec.required_controls, classification_map, ControlLevel.regional
    )
    required_other = controls_at_level(
        spec.required_controls, classification_map, ControlLevel.other
    )
    firm_needed = spec.firm_control_target - len(required_firm)
    regional_needed = spec.regional_control_target - len(required_regional)
    if firm_needed < 0:
        errors.append("Firm-control target is below the number of required firm controls")
    elif firm_needed > len(spec.firm_candidate_controls):
        errors.append("Firm-control target exceeds the firm candidate-pool capacity")
    if regional_needed < 0:
        errors.append("Regional-control target is below the number of required regional controls")
    elif regional_needed > len(spec.regional_candidate_controls):
        errors.append("Regional-control target exceeds the regional candidate-pool capacity")

    required_conflicts = dimension_conflicts(spec.required_controls, classification_map)
    unresolved_conflicts = [
        key for key in required_conflicts if key not in spec.dimension_conflict_overrides
    ]
    if unresolved_conflicts:
        errors.append(
            "Required-control dimension conflicts must be resolved or explicitly retained: "
            + ", ".join(unresolved_conflicts)
        )
    for key in required_conflicts:
        if key in spec.dimension_conflict_overrides:
            warnings.append(f"Required-control dimension conflict explicitly retained: {key}")

    raw_total = (
        math.comb(len(spec.firm_candidate_controls), firm_needed)
        * math.comb(len(spec.regional_candidate_controls), regional_needed)
        if firm_needed >= 0
        and regional_needed >= 0
        and firm_needed <= len(spec.firm_candidate_controls)
        and regional_needed <= len(spec.regional_candidate_controls)
        else 0
    )
    feasible = [] if errors else list(iter_feasible_combinations(spec))
    total = len(feasible)
    if not errors and total == 0:
        errors.append("No feasible combination remains after applying the economic-dimension rules")
    combination_examples = [list(combo) for combo in feasible[:10]]
    combination_examples_truncated = total > len(combination_examples)
    combination_example_groups = [combination_group(spec, combo) for combo in feasible[:10]]
    dimension_excluded = max(0, raw_total - total)
    if total > 5_000:
        warnings.append("This exhaustive search may take a long time")
    if total == 1 and not errors:
        warnings.append("The current settings produce one combination")
    if any(
        item.confidence.value in {"low", "unrecognized"} for item in spec.control_classifications
    ):
        warnings.append("Some control classifications have low confidence or were not recognized")
    if errors:
        common_rows = 0
        dropped_rows = len(frame)
    else:
        common = frame[variables].notna().all(axis=1)
        common_rows = int(common.sum())
        dropped_rows = int(len(frame) - common_rows)
        regressors = (
            1
            + len(spec.required_controls)
            + max(firm_needed, 0)
            + max(regional_needed, 0)
            + len(spec.fixed_other_controls)
        )
        if common_rows <= regressors + 1:
            errors.append("Too few common-sample observations for the requested model")
        if spec.standard_error == StandardErrorType.cluster and spec.cluster_variable:
            clusters = frame.loc[common, spec.cluster_variable].nunique(dropna=True)
            if clusters < 2:
                errors.append("Clustered inference requires at least two clusters")
            elif clusters < 30:
                warnings.append("The selected cluster variable has fewer than 30 clusters")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_rows": int(len(session.frame)),
        "filtered_rows": int(len(frame)),
        "common_rows": common_rows,
        "dropped_for_common_sample": dropped_rows,
        "candidate_pool": len(all_candidates),
        "candidate_count": max(firm_needed, 0) + max(regional_needed, 0),
        "firm_candidate_pool": len(spec.firm_candidate_controls),
        "regional_candidate_pool": len(spec.regional_candidate_controls),
        "firm_required_count": len(required_firm),
        "regional_required_count": len(required_regional),
        "other_control_count": len(required_other) + len(spec.fixed_other_controls),
        "firm_candidate_count": max(firm_needed, 0),
        "regional_candidate_count": max(regional_needed, 0),
        "total_control_count": spec.firm_control_target
        + spec.regional_control_target
        + len(required_other)
        + len(spec.fixed_other_controls),
        "raw_combination_count": raw_total,
        "dimension_excluded_count": dimension_excluded,
        "feasible_combination_count": total,
        "total_combinations": total,
        "combination_examples": combination_examples,
        "combination_example_groups": combination_example_groups,
        "combination_examples_truncated": combination_examples_truncated,
        "target_adjustments": [],
        "blocking_errors": errors,
        "resource_level": "high" if total > 5_000 else "medium" if total > 500 else "low",
    }


def controls_at_level(
    names: list[str], classification_map: dict[str, Any], level: ControlLevel
) -> list[str]:
    return [
        name
        for name in names
        if classification_map.get(name) and classification_map[name].level == level
    ]


def recognized_dimension(value: str) -> bool:
    return bool(value and value not in {"unclassified", "other"})


def dimension_conflicts(
    names: list[str], classification_map: dict[str, Any]
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in names:
        item = classification_map.get(name)
        if not item or not recognized_dimension(item.dimension):
            continue
        key = f"{item.level.value}:{item.dimension}"
        groups.setdefault(key, []).append(name)
    return {key: values for key, values in groups.items() if len(values) > 1}


def combo_respects_dimensions(
    combo: tuple[str, ...], required: list[str], classification_map: dict[str, Any]
) -> bool:
    occupied: set[str] = set()
    for name in required:
        item = classification_map.get(name)
        if item and recognized_dimension(item.dimension):
            occupied.add(item.dimension)
    for name in combo:
        item = classification_map.get(name)
        if not item or not recognized_dimension(item.dimension):
            continue
        if item.dimension in occupied:
            return False
        occupied.add(item.dimension)
    return True


def iter_feasible_combinations(spec: AnalysisSpec):
    classification_map = {item.variable: item for item in spec.control_classifications}
    required_firm = controls_at_level(spec.required_controls, classification_map, ControlLevel.firm)
    required_regional = controls_at_level(
        spec.required_controls, classification_map, ControlLevel.regional
    )
    firm_needed = spec.firm_control_target - len(required_firm)
    regional_needed = spec.regional_control_target - len(required_regional)
    if firm_needed < 0 or regional_needed < 0:
        return
    firm_combos = (
        combo
        for combo in itertools.combinations(spec.firm_candidate_controls, firm_needed)
        if combo_respects_dimensions(combo, required_firm, classification_map)
    )
    valid_firm = list(firm_combos)
    regional_combos = (
        combo
        for combo in itertools.combinations(spec.regional_candidate_controls, regional_needed)
        if combo_respects_dimensions(combo, required_regional, classification_map)
    )
    valid_regional = list(regional_combos)
    for firm_combo, regional_combo in itertools.product(valid_firm, valid_regional):
        yield (*firm_combo, *regional_combo, *spec.fixed_other_controls)


def combination_group(spec: AnalysisSpec, combo: tuple[str, ...]) -> dict[str, list[str]]:
    firm = [name for name in combo if name in spec.firm_candidate_controls]
    regional = [name for name in combo if name in spec.regional_candidate_controls]
    other = [name for name in combo if name in spec.fixed_other_controls]
    return {
        "required": list(spec.required_controls),
        "firm": firm,
        "regional": regional,
        "other": other,
    }


def preview_combinations(
    candidates: list[str], candidate_count: int, limit: int = 10
) -> tuple[list[list[str]], bool]:
    total = math.comb(len(candidates), candidate_count)
    examples = [
        list(combo)
        for combo in itertools.islice(itertools.combinations(candidates, candidate_count), limit)
    ]
    return examples, total > len(examples)


class JobManager:
    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions
        self.jobs: dict[str, AnalysisJob] = {}
        self.lock = threading.RLock()

    def start(self, spec: AnalysisSpec) -> AnalysisJob:
        session = self.sessions.get(spec.dataset_id)
        preview = validate_analysis(session, spec)
        if not preview["valid"]:
            raise ValueError("; ".join(preview["errors"]))
        job = AnalysisJob(uuid.uuid4().hex, spec, preview["total_combinations"])
        with self.lock:
            self.jobs[job.id] = job
        thread = threading.Thread(
            target=self._run, args=(job,), daemon=True, name=f"analysis-{job.id[:8]}"
        )
        thread.start()
        return job

    def get(self, job_id: str) -> AnalysisJob:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            job.touch()
            return job

    def peek(self, job_id: str) -> AnalysisJob:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.jobs[job_id]

    def items(self) -> list[tuple[str, AnalysisJob]]:
        with self.lock:
            return list(self.jobs.items())

    def remove(self, job_id: str) -> AnalysisJob:
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

    def _run(self, job: AnalysisJob) -> None:
        with job.lock:
            job.status = JobStatus.running
            job.started_at = time.monotonic()
            job.message = "Preparing the common sample"
        try:
            session = self.sessions.get(job.spec.dataset_id)
            frame, sample = prepare_common_sample(session, job.spec)
            qualifying: list[ModelResult] = []
            combos = iter_feasible_combinations(job.spec)
            with job.lock:
                job.message = "Estimating all control-variable combinations"
            for combo in combos:
                if job.cancel_event.is_set():
                    with job.lock:
                        job.status = JobStatus.cancelled
                        job.message = "Cancelled; partial rankings were discarded"
                        job.finish_cache_metadata()
                    return
                try:
                    result = estimate_model(frame, job.spec, list(combo))
                    with job.lock:
                        job.successful += 1
                    if qualifies(result, job.spec):
                        qualifying.append(result)
                except ModelExcluded as exc:
                    reason = str(exc) or "excluded"
                    with job.lock:
                        job.excluded += 1
                        job.failure_counts[reason] += 1
                except Exception as exc:
                    debug_failure(job.id, combo, exc)
                    with job.lock:
                        job.excluded += 1
                        job.failure_counts["numerical_failure"] += 1
                finally:
                    with job.lock:
                        job.completed += 1
            qualifying.sort(
                key=lambda item: (
                    item.core_p_value,
                    -abs(item.core_statistic),
                    tuple(item.controls),
                )
            )
            top = qualifying[: job.spec.top_n]
            for rank, item in enumerate(top, start=1):
                item.rank = rank
            best = top[0] if top else None
            result = AnalysisResult(
                job_id=job.id,
                spec=job.spec,
                sample=sample,
                total_combinations=job.total,
                successful_models=job.successful,
                excluded_models=job.excluded,
                failure_counts=dict(job.failure_counts),
                top_models=top,
                best_model=best,
                stata_command=stata_command(job.spec, best.controls) if best else None,
                completed_at=datetime.now(UTC).isoformat(),
                dataset_revision=session.revision,
            )
            with job.lock:
                job.result = result
                job.status = JobStatus.completed
                job.message = "Completed" if best else "No qualifying combination"
                job.finish_cache_metadata()
        except Exception as exc:
            with job.lock:
                job.status = JobStatus.failed
                job.message = str(exc)
                job.finish_cache_metadata()


def prepare_common_sample(
    session: DatasetSession, spec: AnalysisSpec
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = session.filtered_frame
    variables = list(
        dict.fromkeys(
            [
                spec.dependent,
                spec.core,
                *spec.required_controls,
                *spec.firm_candidate_controls,
                *spec.regional_candidate_controls,
                *spec.fixed_other_controls,
                *spec.fixed_effects,
                *([spec.cluster_variable] if spec.cluster_variable else []),
            ]
        )
    )
    common = source[variables].notna().all(axis=1)
    frame = source.loc[common, variables].copy()
    classification_map = {item.variable: item for item in spec.control_classifications}
    required_firm = controls_at_level(spec.required_controls, classification_map, ControlLevel.firm)
    required_regional = controls_at_level(
        spec.required_controls, classification_map, ControlLevel.regional
    )
    raw_total = math.comb(
        len(spec.firm_candidate_controls), spec.firm_control_target - len(required_firm)
    ) * math.comb(
        len(spec.regional_candidate_controls), spec.regional_control_target - len(required_regional)
    )
    feasible_total = sum(1 for _ in iter_feasible_combinations(spec))
    return frame, {
        "source_rows": int(len(session.frame)),
        "filtered_rows": int(len(source)),
        "common_rows": int(len(frame)),
        "dropped_by_filter": int(len(session.frame) - len(source)),
        "dropped_for_common_sample": int(len(source) - len(frame)),
        "filter_expression": session.filter_expression,
        "raw_combination_count": raw_total,
        "dimension_excluded_count": raw_total - feasible_total,
        "feasible_combination_count": feasible_total,
    }


def quote_formula_name(name: str) -> str:
    return name


def estimate_model(frame: pd.DataFrame, spec: AnalysisSpec, controls: list[str]) -> ModelResult:
    import pyfixest as pf

    rhs = [spec.core, *spec.required_controls, *controls]
    fixed = ""
    if spec.model_type == ModelType.fixed_effects:
        fixed = " | " + " + ".join(map(quote_formula_name, spec.fixed_effects))
    formula = (
        f"{quote_formula_name(spec.dependent)} ~ "
        + " + ".join(map(quote_formula_name, rhs))
        + fixed
    )
    if spec.standard_error == StandardErrorType.iid:
        vcov: Any = "iid"
    elif spec.standard_error == StandardErrorType.robust:
        vcov = "HC1"
    else:
        vcov = {"CRV1": spec.cluster_variable}
    ssc = pf.ssc(
        k_fixef="nonnested" if spec.standard_error == StandardErrorType.cluster else "full"
    )
    try:
        fit = pf.feols(
            formula,
            data=frame,
            vcov=vcov,
            ssc=ssc,
            fixef_rm="singleton",
            collin_tol=1e-10,
            store_data=False,
            lean=True,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "collinear" in message or "singular" in message:
            raise ModelExcluded("collinearity") from exc
        if "cluster" in message:
            raise ModelExcluded("invalid_cluster") from exc
        raise
    tidy = fit.tidy()
    if spec.core not in tidy.index:
        raise ModelExcluded("core_absorbed_or_collinear")
    expected_terms = set(rhs)
    present_terms = set(str(value) for value in tidy.index)
    if not expected_terms.issubset(present_terms):
        raise ModelExcluded("collinearity")
    coefficients: list[CoefficientResult] = []
    for variable, row in tidy.iterrows():
        coefficients.append(
            CoefficientResult(
                variable=str(variable),
                estimate=float(row["Estimate"]),
                std_error=float(row["Std. Error"]),
                statistic=float(row["t value"]),
                p_value=float(row["Pr(>|t|)"]),
                conf_low=float(row["2.5%"]),
                conf_high=float(row["97.5%"]),
            )
        )
    core = next(item for item in coefficients if item.variable == spec.core)
    observations = int(getattr(fit, "_N", len(frame)))
    return ModelResult(
        controls=controls,
        core_estimate=core.estimate,
        core_std_error=core.std_error,
        core_statistic=core.statistic,
        core_p_value=core.p_value,
        observations=observations,
        df_residual=safe_float(getattr(fit, "_df_t", None)),
        r_squared=safe_float(getattr(fit, "_r2", None)),
        adjusted_r_squared=safe_float(getattr(fit, "_adj_r2", None)),
        within_r_squared=safe_float(getattr(fit, "_r2_within", None)),
        coefficients=coefficients,
    )


def safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def qualifies(result: ModelResult, spec: AnalysisSpec) -> bool:
    sign_ok = (
        result.core_estimate > 0
        if spec.expected_sign == ExpectedSign.positive
        else result.core_estimate < 0
    )
    return sign_ok and result.core_p_value < spec.significance


def stata_command(spec: AnalysisSpec, controls: list[str]) -> str:
    terms = " ".join([spec.core, *spec.required_controls, *controls])
    if spec.model_type == ModelType.fixed_effects:
        command = f"reghdfe {spec.dependent} {terms}, absorb({' '.join(spec.fixed_effects)})"
        if spec.standard_error == StandardErrorType.robust:
            command += " vce(robust)"
        elif spec.standard_error == StandardErrorType.cluster:
            command += f" vce(cluster {spec.cluster_variable})"
    else:
        command = f"regress {spec.dependent} {terms}"
        if spec.standard_error == StandardErrorType.robust:
            command += ", vce(robust)"
        elif spec.standard_error == StandardErrorType.cluster:
            command += f", vce(cluster {spec.cluster_variable})"
    return command
