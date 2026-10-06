from backend.app.analysis import preview_combinations, qualifies, stata_command
from backend.app.models import (
    AnalysisSpec,
    CoefficientResult,
    ExpectedSign,
    ModelResult,
    ModelType,
    StandardErrorType,
)


def make_spec(**updates):
    values = {
        "dataset_id": "dataset",
        "dependent": "y",
        "core": "x",
        "required_controls": ["required"],
        "candidate_controls": ["c1", "c2"],
        "candidate_count": 1,
        "model_type": ModelType.ols,
        "standard_error": StandardErrorType.robust,
        "expected_sign": ExpectedSign.positive,
        "top_n": 20,
    }
    values.update(updates)
    return AnalysisSpec(**values)


def model(estimate: float, p_value: float) -> ModelResult:
    coefficient = CoefficientResult(
        variable="x",
        estimate=estimate,
        std_error=0.1,
        statistic=estimate / 0.1,
        p_value=p_value,
        conf_low=estimate - 0.2,
        conf_high=estimate + 0.2,
    )
    return ModelResult(
        controls=["c1"],
        core_estimate=estimate,
        core_std_error=0.1,
        core_statistic=estimate / 0.1,
        core_p_value=p_value,
        observations=100,
        coefficients=[coefficient],
    )


def test_qualifying_rule_requires_sign_and_threshold() -> None:
    spec = make_spec()
    assert qualifies(model(0.5, 0.01), spec)
    assert not qualifies(model(-0.5, 0.01), spec)
    assert not qualifies(model(0.5, 0.05), spec)


def test_stata_command_for_fixed_effect_cluster() -> None:
    spec = make_spec(
        model_type=ModelType.fixed_effects,
        fixed_effects=["firm", "year"],
        standard_error=StandardErrorType.cluster,
        cluster_variable="firm",
    )
    assert stata_command(spec, ["c2"]) == (
        "reghdfe y x required c2, absorb(firm year) vce(cluster firm)"
    )


def test_combination_preview_uses_exact_k_and_candidate_order() -> None:
    examples, truncated = preview_combinations(["a", "b", "c", "d"], 2)
    assert examples == [
        ["a", "b"],
        ["a", "c"],
        ["a", "d"],
        ["b", "c"],
        ["b", "d"],
        ["c", "d"],
    ]
    assert not truncated


def test_combination_preview_is_limited_without_changing_total_search() -> None:
    candidates = [f"c{index}" for index in range(9)]
    examples, truncated = preview_combinations(candidates, 3)
    assert len(examples) == 10
    assert truncated


def test_combination_preview_allows_zero_and_full_pool_counts() -> None:
    assert preview_combinations(["a", "b"], 0) == ([[]], False)
    assert preview_combinations(["a", "b"], 2) == ([["a", "b"]], False)
