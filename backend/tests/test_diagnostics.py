from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.diagnostic_exports import figure_bytes
from backend.app.diagnostics import (
    DiagnosticError,
    DiagnosticJob,
    DiagnosticJobManager,
    derive_treatment_time,
    estimate_event_study,
    estimate_overall_att,
    randomize_frame,
    run_placebo,
)
from backend.app.models import (
    DiagnosticResult,
    DiagnosticSpec,
    EventStudyMethod,
    PlaceboDraw,
    PlaceboMethod,
    PlaceboResult,
    TreatmentInputMode,
    TreatmentTimingSpec,
)


def staggered_frame() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    units = 30
    periods = np.arange(2015, 2023)
    frame = pd.DataFrame(
        {
            "firm": np.repeat(np.arange(units), len(periods)),
            "year": np.tile(periods, units),
        }
    )
    frame["g"] = np.select(
        [frame["firm"] < 10, frame["firm"] < 20],
        [2018, 2020],
        default=0,
    )
    frame["x"] = rng.normal(size=len(frame))
    frame["_diag_g"] = frame["g"]
    frame["_diag_event_raw"] = np.where(frame["g"] > 0, frame["year"] - frame["g"], np.nan)
    frame["_diag_event"] = frame["_diag_event_raw"].clip(-3, 3)
    frame["_diag_treated"] = ((frame["g"] > 0) & (frame["year"] >= frame["g"])).astype(int)
    unit_effect = frame["firm"] * 0.03
    time_effect = (frame["year"] - 2015) * 0.04
    frame["y"] = unit_effect + time_effect + frame["_diag_treated"] + 0.1 * frame["x"]
    return frame


def diagnostic_spec() -> DiagnosticSpec:
    return DiagnosticSpec(
        analysis_job_id="analysis",
        model_rank=1,
        panel_id="firm",
        time_variable="year",
        treatment=TreatmentTimingSpec(variable="g"),
        window_start=-3,
        window_end=3,
    )


def test_did2s_and_saturated_event_studies_have_explicit_baseline() -> None:
    frame = staggered_frame()
    spec = diagnostic_spec()
    for method in [EventStudyMethod.did2s, EventStudyMethod.saturated]:
        result = estimate_event_study(
            frame,
            "y",
            ["x"],
            ["firm", "year"],
            "firm",
            spec,
            method,
            [],
        )
        baseline = next(point for point in result.points if point.event_time == -1)
        assert baseline.estimate == 0
        assert baseline.std_error == 0
        post = [point.estimate for point in result.points if point.event_time >= 0]
        assert max(value for value in post if value is not None) > 0.5


def test_placebo_randomization_preserves_required_margins() -> None:
    frame = staggered_frame()
    spec = diagnostic_spec()
    rng = np.random.default_rng(5)
    original = frame.groupby("firm")["_diag_g"].first()
    group = randomize_frame(frame, "y", spec, PlaceboMethod.random_group, rng)
    shuffled = group.groupby("firm")["_diag_g"].first()
    assert sorted(original.tolist()) == sorted(shuffled.tolist())
    timing = randomize_frame(frame, "y", spec, PlaceboMethod.random_timing, rng)
    timing_g = timing.groupby("firm")["_diag_g"].first()
    assert set(timing_g[timing_g > 0].index) == set(original[original > 0].index)
    assert sorted(timing_g.tolist()) == sorted(original.tolist())


def test_overall_att_and_png_export() -> None:
    frame = staggered_frame()
    att = estimate_overall_att(
        frame,
        "y",
        ["x"],
        ["firm", "year"],
        "firm",
        EventStudyMethod.did2s,
    )
    assert att > 0.5
    placebo = PlaceboResult(
        method=PlaceboMethod.random_group,
        estimator=EventStudyMethod.did2s,
        actual_att=att,
        draws=[
            PlaceboDraw(iteration=i + 1, estimate=float(value))
            for i, value in enumerate(np.linspace(-0.2, 0.2, 100))
        ],
        mean=0,
        std_dev=0.1,
        quantile_low=-0.19,
        quantile_high=0.19,
        empirical_p_value=0.01,
        attempted=100,
        failed=0,
    )
    result = DiagnosticResult(
        job_id="diagnostic",
        kind="placebo",
        spec=diagnostic_spec(),
        dataset_revision=0,
        model={
            "rank": 1,
            "controls": ["x"],
            "core_estimate": 1,
            "core_std_error": 0.1,
            "core_statistic": 10,
            "core_p_value": 0,
            "observations": len(frame),
            "coefficients": [],
        },
        sample={"clusters": 30},
        placebos=[placebo],
        completed_at="2026-10-06T00:00:00Z",
    )
    png = figure_bytes(result, "placebo_random_group", "png")
    assert png.startswith(b"\x89PNG")


def test_placebo_draws_are_reproducible(monkeypatch) -> None:
    frame = staggered_frame()
    spec = diagnostic_spec().model_copy(
        update={"repetitions": 100, "placebo_estimator": EventStudyMethod.did2s}
    )

    def quick_att(data, *_args, **_kwargs):
        return float((data["y"] * data["_diag_treated"]).mean())

    monkeypatch.setattr("backend.app.diagnostics.estimate_overall_att", quick_att)
    first_job = DiagnosticJob("first", "placebo", spec, 100)
    second_job = DiagnosticJob("second", "placebo", spec, 100)
    first = run_placebo(
        frame,
        "y",
        ["x"],
        ["firm", "year"],
        "firm",
        spec,
        PlaceboMethod.random_group,
        1.0,
        np.random.SeedSequence(123),
        first_job,
    )
    second = run_placebo(
        frame,
        "y",
        ["x"],
        ["firm", "year"],
        "firm",
        spec,
        PlaceboMethod.random_group,
        1.0,
        np.random.SeedSequence(123),
        second_job,
    )
    assert [draw.estimate for draw in first.draws] == [draw.estimate for draw in second.draws]
    assert len(first.draws) == 100
    assert 0 < first.empirical_p_value <= 1


def test_only_one_diagnostic_job_can_run(monkeypatch) -> None:
    manager = DiagnosticJobManager(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "preview", lambda _spec: {"valid": True})
    active = DiagnosticJob("active", "event_study", diagnostic_spec(), 1)
    manager.jobs[active.id] = active
    with pytest.raises(DiagnosticError, match="already running"):
        manager.start("placebo", diagnostic_spec())


def test_indicator_timing_is_derived_and_must_be_monotone() -> None:
    frame = pd.DataFrame(
        {
            "firm": [1, 1, 1, 2, 2, 2],
            "year": [1, 2, 3, 1, 2, 3],
            "treated": [0, 1, 1, 0, 0, 0],
        }
    )
    spec = diagnostic_spec().model_copy(
        update={
            "panel_id": "firm",
            "time_variable": "year",
            "treatment": TreatmentTimingSpec(
                mode=TreatmentInputMode.indicator, variable="treated"
            ),
        }
    )
    assert derive_treatment_time(frame, spec).tolist() == [2, 2, 2, 0, 0, 0]
    frame.loc[2, "treated"] = 0
    with pytest.raises(DiagnosticError, match="remain one"):
        derive_treatment_time(frame, spec)


def test_pretrend_test_can_be_disabled() -> None:
    frame = staggered_frame()
    spec = diagnostic_spec().model_copy(update={"run_pretrend_test": False})
    result = estimate_event_study(
        frame,
        "y",
        ["x"],
        ["firm", "year"],
        "firm",
        spec,
        EventStudyMethod.did2s,
        [],
    )
    assert result.pretrend_statistic is None
    assert result.pretrend_p_value is None


def test_trajectory_placebo_preserves_policy_and_time_support() -> None:
    frame = staggered_frame().drop(index=[0, 1, 80]).reset_index(drop=True)
    spec = diagnostic_spec()
    original_policy = frame[["firm", "year", "_diag_g", "_diag_treated"]].copy()
    randomized = randomize_frame(
        frame, "y", spec, PlaceboMethod.permute_outcome, np.random.default_rng(19)
    )
    pd.testing.assert_frame_equal(
        randomized[["firm", "year", "_diag_g", "_diag_treated"]].reset_index(drop=True),
        original_policy.reset_index(drop=True),
    )
    original_support = {
        unit: tuple(sorted(group["year"].tolist())) for unit, group in frame.groupby("firm")
    }
    randomized_support = {
        unit: tuple(sorted(group["year"].tolist()))
        for unit, group in randomized.groupby("firm")
    }
    assert randomized_support == original_support
