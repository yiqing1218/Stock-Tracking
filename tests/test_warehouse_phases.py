from __future__ import annotations

import pandas as pd

from astock_monitor.alert_engine import AlertEngine
from astock_monitor.candlestick_patterns import detect_patterns
from astock_monitor.historical_store import HistoricalStore
from astock_monitor.models import Quote, Security, SecurityType


def history() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=8, freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10, 9.8, 9.6, 9.7, 9.8, 10, 10.3, 10.7],
            "high": [10.2, 10, 9.8, 9.9, 10.1, 10.4, 10.8, 11.2],
            "low": [9.7, 9.5, 9.4, 9.5, 9.6, 9.9, 10.2, 10.6],
            "close": [9.8, 9.6, 9.7, 9.8, 10, 10.3, 10.7, 11.1],
            "volume": [1000, 1100, 1200, 1150, 1300, 1500, 1800, 2100],
            "amount": [9800, 10560, 11640, 11270, 13000, 15450, 19260, 23310],
        }
    )


def test_historical_store_upsert_query_and_export(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = HistoricalStore(tmp_path / "monitor.db")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    assert store.upsert_bars(security, history(), "qfq", "test") == 8
    assert store.upsert_bars(security, history(), "qfq", "test") == 8
    assert len(store.get_bars(security, "qfq")) == 8
    assert store.summary().bars == 8
    output = tmp_path / "bars.csv"
    assert store.export_csv(output, [security]) == 8
    assert "浦发银行" in output.read_text(encoding="utf-8-sig")


def test_pattern_catalog_detects_three_soldiers() -> None:
    frame = history().tail(3).copy()
    detected = detect_patterns(frame)
    assert detected["three_soldiers"] is True


def test_persistent_alert_cross_and_cooldown(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = HistoricalStore(tmp_path / "monitor.db")
    engine = AlertEngine(store)
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    rule_id = engine.save_rule(
        "突破10元", "price", "cross_up", 10, "single", security.key
    )
    rule = next(item for item in engine.list_rules(True) if item["id"] == rule_id)
    assert engine.evaluate_quote(rule, Quote(security, price=9.9)) is None
    assert engine.evaluate_quote(rule, Quote(security, price=10.1)) is not None
    assert engine.evaluate_quote(rule, Quote(security, price=10.2)) is None
    assert len(engine.list_events()) == 1
