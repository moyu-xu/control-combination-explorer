import pandas as pd
import pytest

from backend.app.filter_parser import FilterSyntaxError, evaluate_filter


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "year": [2014, 2015, 2020, 2022],
            "region": ["east", "west", "east", None],
            "score": [1.0, None, 3.0, 4.0],
        }
    )


def test_comparison_and_boolean_precedence(frame: pd.DataFrame) -> None:
    mask = evaluate_filter('year >= 2015 & region == "east"', frame)
    assert mask.tolist() == [False, False, True, False]


def test_missing_and_inlist(frame: pd.DataFrame) -> None:
    mask = evaluate_filter("missing(score) | inlist(year, 2014, 2022)", frame)
    assert mask.tolist() == [True, True, False, True]


def test_rejects_unknown_functions(frame: pd.DataFrame) -> None:
    with pytest.raises(FilterSyntaxError) as exc:
        evaluate_filter("system(year)", frame)
    assert exc.value.position == 0


def test_rejects_unknown_variables(frame: pd.DataFrame) -> None:
    with pytest.raises(FilterSyntaxError):
        evaluate_filter("missing(nope)", frame)
