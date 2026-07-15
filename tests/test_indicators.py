from __future__ import annotations

import numpy as np
import pandas as pd

from astock_monitor.indicators import (
    INDICATOR_CATALOG,
    build_indicator_snapshot,
    calculate_indicators,
    market_regime,
)


def sample_history(rows: int = 420) -> pd.DataFrame:
    rng = np.random.default_rng(20260714)
    close = 10 + np.linspace(0, 8, rows) + rng.normal(0, 0.18, rows)
    open_price = close + rng.normal(0, 0.12, rows)
    high = np.maximum(open_price, close) + rng.uniform(0.05, 0.28, rows)
    low = np.minimum(open_price, close) - rng.uniform(0.05, 0.28, rows)
    volume = rng.integers(100_000, 2_000_000, rows).astype(float)
    amount = volume * 100 * close
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=rows, freq="B"),
            "open": open_price,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "turnover": rng.uniform(0.2, 5, rows),
            "pct_change": pd.Series(close).pct_change(fill_method=None) * 100,
        }
    )


def test_calculate_indicators_produces_comprehensive_finite_snapshot() -> None:
    result = calculate_indicators(sample_history())
    required = {
        "SMA_250",
        "MACD_HIST",
        "RSI_14",
        "KDJ_J",
        "ADX_14",
        "BB_WIDTH",
        "ATR_14",
        "OBV",
        "MFI_14",
        "CMF_20",
        "PSAR",
        "SUPERTREND",
        "ROLLING_SHARPE_60",
        "DRAWDOWN",
    }
    assert required.issubset(result.columns)
    assert len(INDICATOR_CATALOG) >= 60
    snapshots = build_indicator_snapshot(result)
    assert len(snapshots) == len(INDICATOR_CATALOG)
    assert sum(item.value is not None for item in snapshots) >= 55


def test_market_regime_is_bounded() -> None:
    result = calculate_indicators(sample_history())
    regime = market_regime(result)
    assert 0 <= float(regime["score"]) <= 100
    assert regime["regime"] in {"趋势", "趋势酝酿", "震荡"}


def test_rsi_stays_in_expected_range() -> None:
    result = calculate_indicators(sample_history())
    values = result["RSI_14"].dropna()
    assert values.between(0, 100).all()

