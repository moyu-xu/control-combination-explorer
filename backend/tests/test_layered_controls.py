from backend.app.analysis import dimension_conflicts, iter_feasible_combinations
from backend.app.control_classification import classify_variable
from backend.app.models import AnalysisSpec


def classification(variable: str, level: str, dimension: str) -> dict[str, object]:
    return {
        "variable": variable,
        "suggested_level": level,
        "suggested_dimension": dimension,
        "level": level,
        "dimension": dimension,
        "confidence": "high",
        "reason": "test",
    }


def layered_spec(**updates: object) -> AnalysisSpec:
    values: dict[str, object] = {
        "dataset_id": "dataset",
        "dependent": "y",
        "core": "x",
        "required_controls": [],
        "firm_candidate_controls": ["size_a", "size_b", "profit"],
        "regional_candidate_controls": ["gdp", "population"],
        "fixed_other_controls": ["industry_dummy"],
        "firm_control_target": 2,
        "regional_control_target": 1,
        "control_classifications": [
            classification("size_a", "firm", "size"),
            classification("size_b", "firm", "size"),
            classification("profit", "firm", "profitability"),
            classification("gdp", "regional", "economic_development"),
            classification("population", "regional", "population_urbanization"),
            classification("industry_dummy", "other", "unclassified"),
        ],
        "classification_rule_version": "test",
        "classification_confirmed": True,
        "dimension_conflict_overrides": [],
        "expected_sign": "positive",
    }
    values.update(updates)
    return AnalysisSpec(**values)


def test_layered_cartesian_product_filters_duplicate_dimensions() -> None:
    combinations = list(iter_feasible_combinations(layered_spec()))
    assert combinations == [
        ("size_a", "profit", "gdp", "industry_dummy"),
        ("size_a", "profit", "population", "industry_dummy"),
        ("size_b", "profit", "gdp", "industry_dummy"),
        ("size_b", "profit", "population", "industry_dummy"),
    ]


def test_required_controls_offset_targets_and_occupy_dimensions() -> None:
    spec = layered_spec(
        required_controls=["required_size"],
        firm_control_target=2,
        control_classifications=[
            classification("required_size", "firm", "size"),
            *layered_spec().control_classifications,
        ],
    )
    combinations = list(iter_feasible_combinations(spec))
    assert combinations == [
        ("profit", "gdp", "industry_dummy"),
        ("profit", "population", "industry_dummy"),
    ]


def test_required_dimension_conflict_is_reported_by_level() -> None:
    spec = layered_spec(
        required_controls=["size_a", "size_b"],
        firm_candidate_controls=["profit"],
        firm_control_target=2,
    )
    lookup = {item.variable: item for item in spec.control_classifications}
    assert dimension_conflicts(spec.required_controls, lookup) == {
        "firm:size": ["size_a", "size_b"]
    }


def test_transparent_dictionary_handles_chinese_and_unknown_labels() -> None:
    firm = classify_variable("control_a", "企业规模")
    regional = classify_variable("city_gdp", "地区经济发展水平")
    unknown = classify_variable("control_z", "控制变量")
    assert (firm.level.value, firm.dimension, firm.confidence.value) == (
        "firm",
        "size",
        "medium",
    )
    assert (regional.level.value, regional.dimension) == (
        "regional",
        "economic_development",
    )
    assert (unknown.level.value, unknown.dimension, unknown.confidence.value) == (
        "other",
        "unclassified",
        "unrecognized",
    )
