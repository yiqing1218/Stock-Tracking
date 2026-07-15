from __future__ import annotations

import numpy as np
import pandas as pd

from astock_monitor.backtest import BacktestConfig, run_single_backtest
from astock_monitor.data_provider import DataProvider
from astock_monitor.indicators import INDICATOR_CATALOG, build_screening_catalog
from astock_monitor.models import Security, SecurityType
from astock_monitor.repository import Repository
from astock_monitor.screening import ScreeningCondition, matches_conditions


def test_notifications_and_market_alert_rules_are_compact_and_deduplicated(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "monitor.db")
    security = Security("600519", "贵州茅台", SecurityType.STOCK, "sh")
    assert repository.add_notification(
        security, "发布公告", "测试公告", external_key="same-event"
    )
    assert not repository.add_notification(
        security, "发布公告", "测试公告", external_key="same-event"
    )
    assert repository.unread_notification_count() == 1
    repository.mark_all_notifications_read()
    assert repository.unread_notification_count() == 0

    repository.save_market_alert_rule(security, "price_above", 1800, window_minutes=5)
    rules = repository.list_market_alert_rules(security)
    assert len(rules) == 1
    assert rules[0]["threshold"] == 1800
    repository.update_market_alert_state(rules[0]["id"], "1")
    assert repository.list_market_alert_rules(security)[0]["last_state"] == "1"


def test_screening_catalog_and_three_way_logic() -> None:
    catalog = build_screening_catalog()
    assert len(catalog) >= 480
    definitions = {item.column: item for item in [*INDICATOR_CATALOG, *catalog]}
    conditions = [
        ScreeningCondition("首项", definitions["RSI_14"], ">", 50),
        ScreeningCondition("且", definitions["RETURN_20D"], ">", 0),
        ScreeningCondition("非", definitions["HV_20"], ">", 80),
    ]
    assert matches_conditions({"RSI_14": 60, "RETURN_20D": 2, "HV_20": 20}, conditions)
    assert not matches_conditions(
        {"RSI_14": 60, "RETURN_20D": 2, "HV_20": 90}, conditions
    )


def test_market_breadth_uses_board_specific_price_limits(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    timestamp = pd.Timestamp("2026-07-15 14:00", tz="Asia/Shanghai").timestamp()
    spot = pd.DataFrame(
        {
            "代码": ["600001", "000002", "300001", "830001", "600003", "600004"],
            "名称": ["普通", "ST测试", "创业板", "北交所", "N新股", "下跌股"],
            "最新价": [11.0, 9.5, 12.0, 13.0, 15.0, 8.8],
            "昨收": [10.0] * 6,
            "涨跌幅": [10, -5, 20, 30, 50, -12],
            "成交额": [100.0] * 6,
            "更新时间戳": [timestamp] * 6,
        }
    )
    boards = pd.DataFrame(
        {
            "类型": ["行业"],
            "板块名称": ["测试"],
            "涨跌幅": [1.0],
            "更新时间戳": [timestamp],
        }
    )
    monkeypatch.setattr(provider, "refresh_quotes", lambda _items: {})
    monkeypatch.setattr(
        provider,
        "_load_extra_with_cache",
        lambda name, *_args: spot.copy() if name == "market_breadth" else boards.copy(),
    )
    result = provider.get_market_dashboard()
    assert result.trade_date.isoformat() == "2026-07-15"
    assert result.breadth["limit_up"] == 3
    assert result.breadth["limit_down"] == 2
    assert result.breadth["amount"] == 600
    assert result.losers.iloc[0]["代码"] == "600004"


def test_backtest_executes_signal_next_day_and_respects_t_plus_one() -> None:
    size = 80
    close = np.r_[np.full(25, 10.0), np.linspace(10, 15, 30), np.linspace(15, 11, 25)]
    history = pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-01", periods=size),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
            "amount": close * 1_000_000,
            "turnover": 1.0,
        }
    )
    security = Security("600000", "测试股票", SecurityType.STOCK, "sh")
    result = run_single_backtest(history, security, BacktestConfig())
    assert "累计收益(%)" in result.metrics
    assert {"ENTRY", "EXIT"}.issubset(result.signals.columns)
    if len(result.trades) >= 2:
        buy = result.trades[result.trades["方向"] == "买入"].iloc[0]
        sell = result.trades[result.trades["方向"] == "卖出"].iloc[0]
        assert pd.Timestamp(sell["日期"]) > pd.Timestamp(buy["日期"])


def test_etf_local_dividend_adjustment_produces_qfq_and_hfq(tmp_path) -> None:
    provider = DataProvider(tmp_path / "cache")
    history = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
            "open": [10.0, 9.5, 9.7],
            "high": [10.2, 9.8, 9.9],
            "low": [9.9, 9.4, 9.6],
            "close": [10.0, 9.6, 9.8],
            "volume": [100, 100, 100],
            "amount": [1000, 960, 980],
            "turnover": [1, 1, 1],
        }
    )
    dividends = pd.DataFrame(
        {"日期": pd.to_datetime(["2025-01-03"]), "累计分红": [0.5]}
    )
    qfq = provider._adjust_etf_history_locally(history, dividends, "qfq")
    hfq = provider._adjust_etf_history_locally(history, dividends, "hfq")
    assert qfq.iloc[0]["close"] < history.iloc[0]["close"]
    assert hfq.iloc[-1]["close"] > history.iloc[-1]["close"]
