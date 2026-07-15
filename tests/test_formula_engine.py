from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from astock_monitor.formula_engine import FormulaEngine, FormulaError


@pytest.fixture()
def frame() -> pd.DataFrame:
    rows = 100
    close = pd.Series(np.linspace(10, 20, rows))
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.linspace(1000, 3000, rows),
            "amount": np.linspace(1000, 3000, rows) * close * 100,
            "turnover": 1.2,
            "pct_change": close.pct_change(fill_method=None) * 100,
            "returns": close.pct_change(fill_method=None) * 100,
        }
    )


def test_formula_engine_calculates_series(frame: pd.DataFrame) -> None:
    engine = FormulaEngine(frame)
    result = engine.evaluate("(close / SMA(close, 20) - 1) * 100")
    assert len(result) == len(frame)
    assert result.notna().sum() == 81
    assert result.iloc[-1] > 0


def test_formula_engine_supports_cross_and_condition(frame: pd.DataFrame) -> None:
    engine = FormulaEngine(frame)
    result = engine.evaluate("IF(close > SMA(close, 10), 1, -1)")
    assert set(result.dropna().unique()).issubset({-1, 1})
    assert result.iloc[-1] == 1


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os').system('whoami')",
        "close.__class__",
        "close[0]",
        "open('secret.txt')",
        "[x for x in close]",
    ],
)
def test_formula_engine_rejects_arbitrary_python(frame: pd.DataFrame, formula: str) -> None:
    with pytest.raises(FormulaError):
        FormulaEngine(frame).evaluate(formula)


def test_formula_engine_reports_dependencies(frame: pd.DataFrame) -> None:
    validation = FormulaEngine(frame).validate("ZSCORE(returns, 20) + ZSCORE(volume, 20)")
    assert validation.dependencies == ("returns", "volume")

