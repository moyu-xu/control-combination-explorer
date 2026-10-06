from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.analysis import estimate_model
from backend.app.exports import build_excel, build_word
from backend.app.models import (
    AnalysisResult,
    AnalysisSpec,
    ExpectedSign,
    Language,
    ModelType,
    StandardErrorType,
)


def sample_frame() -> pd.DataFrame:
    rng = np.random.default_rng(20261005)
    rows = 120
    frame = pd.DataFrame(
        {
            "x": rng.normal(size=rows),
            "c1": rng.normal(size=rows),
            "c2": rng.normal(size=rows),
            "firm": np.repeat(np.arange(12), 10),
        }
    )
    frame["y"] = 0.9 * frame["x"] + 0.25 * frame["c1"] + rng.normal(scale=0.8, size=rows)
    return frame


def make_spec(**updates: object) -> AnalysisSpec:
    values: dict[str, object] = {
        "dataset_id": "fixture",
        "language": Language.zh,
        "dependent": "y",
        "core": "x",
        "required_controls": [],
        "candidate_controls": ["c1", "c2"],
        "candidate_count": 1,
        "model_type": ModelType.ols,
        "standard_error": StandardErrorType.robust,
        "expected_sign": ExpectedSign.positive,
    }
    values.update(updates)
    return AnalysisSpec(**values)


def test_real_ols_and_fixed_effect_estimation() -> None:
    frame = sample_frame()
    ols = estimate_model(frame, make_spec(), ["c1"])
    assert ols.core_estimate > 0
    assert ols.core_p_value < 0.05
    assert ols.observations == len(frame)

    fixed_spec = make_spec(
        model_type=ModelType.fixed_effects,
        fixed_effects=["firm"],
        standard_error=StandardErrorType.cluster,
        cluster_variable="firm",
    )
    fixed = estimate_model(frame, fixed_spec, ["c1"])
    assert fixed.core_estimate > 0
    assert fixed.core_p_value < 0.05


def test_excel_and_word_exports_are_valid_packages() -> None:
    frame = sample_frame()
    spec = make_spec()
    model = estimate_model(frame, spec, ["c1"])
    model.rank = 1
    result = AnalysisResult(
        job_id="job",
        spec=spec,
        sample={"common_rows": len(frame)},
        total_combinations=2,
        successful_models=2,
        excluded_models=0,
        failure_counts={},
        top_models=[model],
        best_model=model,
        stata_command="regress y x c1, vce(robust)",
        completed_at="2026-10-05T00:00:00Z",
    )
    assert build_excel(result).startswith(b"PK")
    assert build_word(result).startswith(b"PK")
