from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6.QtCore import QThreadPool, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astock_monitor.chart_widget import MarketChart  # noqa: E402
from astock_monitor.data_provider import DataProvider, DetailBundle  # noqa: E402
from astock_monitor.detail_page import DetailPage  # noqa: E402
from astock_monitor.indicators import (  # noqa: E402
    IndicatorDefinition,
    IndicatorSnapshot,
    build_indicator_snapshot,
    calculate_indicators,
    detailed_indicator_description,
    dimension_composites,
    indicator_weight,
    resample_ohlcv,
)
from astock_monitor.main_window import MainWindow  # noqa: E402
from astock_monitor.models import (  # noqa: E402
    CustomIndicator,
    Quote,
    Security,
    SecurityType,
)
from astock_monitor.repository import Repository  # noqa: E402
from astock_monitor.watchlist_page import WatchlistPage  # noqa: E402


def sample_history(rows: int = 90) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=rows)
    close = np.linspace(10, 14, rows)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "close": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "volume": np.linspace(1000, 3000, rows),
            "amount": np.linspace(10000, 42000, rows),
            "turnover": np.full(rows, 2.5),
        }
    )


def test_daily_history_resamples_to_week_and_month() -> None:
    history = sample_history()
    weekly = resample_ohlcv(history, "weekly")
    monthly = resample_ohlcv(history, "monthly")

    assert 15 <= len(weekly) <= 20
    assert 3 <= len(monthly) <= 5
    assert weekly.iloc[0]["open"] == history.iloc[0]["open"]
    assert weekly.iloc[-1]["close"] == history.iloc[-1]["close"]
    assert weekly["volume"].sum() == history["volume"].sum()


def test_dimension_composite_uses_every_indicator_in_dimension() -> None:
    definition_a = IndicatorDefinition("趋势", "A", "A", "A")
    definition_b = IndicatorDefinition("趋势", "B", "B", "B")
    snapshots = [
        IndicatorSnapshot(definition_a, 1.0, "偏多"),
        IndicatorSnapshot(definition_b, -1.0, "偏空"),
    ]

    result = dimension_composites(snapshots)

    assert result["趋势"]["count"] == 2
    assert result["趋势"]["score"] == 50.0


def test_custom_indicator_category_library_and_favorite_persist(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(tmp_path / "monitor.db")
    saved = repository.save_custom_indicator(
        CustomIndicator(
            None,
            "测试量能",
            "ZSCORE(volume, 20)",
            "#38BDF8",
            category="量能",
            in_library=True,
        )
    )
    repository.set_indicator_favorite(f"custom:{saved.id}", True)

    loaded = next(
        item for item in repository.list_custom_indicators() if item.id == saved.id
    )
    assert loaded.category == "量能"
    assert loaded.in_library is True
    assert f"custom:{saved.id}" in repository.list_indicator_favorites()


def test_local_chip_estimate_always_produces_core_metrics(tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    chips = provider._estimate_chips(sample_history())

    assert not chips.empty
    assert {
        "获利比例",
        "平均成本",
        "90成本-低",
        "90成本-高",
        "90集中度",
    }.issubset(chips.columns)
    assert chips.iloc[-1]["平均成本"] > 0


def test_market_chart_can_zoom_and_pan() -> None:
    app = QApplication.instance() or QApplication([])
    chart = MarketChart()
    chart.set_data(sample_history())
    initial = chart._visible_count
    chart.zoom_in()
    assert chart._visible_count < initial
    chart.pan_left()
    assert chart._right_offset > 0
    chart.pan_right()
    assert chart._right_offset == 0
    app.processEvents()


def test_extended_library_contains_hundreds_of_system_outputs() -> None:
    frame = calculate_indicators(sample_history(180), include_extended=True)
    snapshots = build_indicator_snapshot(frame)

    assert len(snapshots) > 300
    assert {item.definition.category for item in snapshots} == {
        "趋势",
        "动量",
        "波动",
        "量能",
        "情绪",
        "风险",
    }
    assert all(item.definition.origin == "系统" for item in snapshots)


def test_overview_has_period_controls_and_six_rows_without_scrollbars(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    page = DetailPage(
        Repository(tmp_path / "monitor.db"),
        DataProvider(tmp_path / "cache"),
        QThreadPool(),
    )
    page.indicator_frame = calculate_indicators(sample_history())
    page.snapshots = build_indicator_snapshot(page.indicator_frame)
    page._update_overview()

    assert set(page.period_buttons) == {"daily", "weekly", "yearly"}
    assert not hasattr(page, "chip_distribution_chart")
    assert page.signal_table.rowCount() == 6
    assert (
        page.signal_table.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    app.processEvents()


def test_fund_flow_can_be_estimated_but_chips_are_never_fabricated(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")

    def unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("offline")

    monkeypatch.setattr(
        "astock_monitor.data_provider.ak.stock_individual_fund_flow", unavailable
    )
    monkeypatch.setattr(
        "astock_monitor.data_provider.ak.stock_individual_fund_flow_rank", unavailable
    )
    monkeypatch.setattr(
        "astock_monitor.data_provider.ak.stock_fund_flow_individual", unavailable
    )
    monkeypatch.setattr("astock_monitor.data_provider.ak.stock_cyq_em", unavailable)

    flow = provider._load_fund_flow(security, sample_history())
    chips = provider._load_chips(security, sample_history(), "qfq")

    assert not flow.empty
    assert flow.attrs["source"] == "本地OHLCV资金流估算（非逐笔主力）"
    assert chips.empty
    assert chips.attrs["source"] == "东方财富筹码接口无可靠返回"


def test_watchlist_numeric_sort_keeps_security_mapping_and_score(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    repository = Repository(tmp_path / "monitor.db")
    for saved in repository.list_watchlist():
        repository.remove_security(saved)
    first = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    second = Security("000001", "平安银行", SecurityType.STOCK, "sz")
    repository.add_security(first)
    repository.add_security(second)
    page = WatchlistPage(repository, DataProvider(tmp_path / "cache"), QThreadPool())
    page.quotes = {
        first.key: Quote(first, price=12.0),
        second.key: Quote(second, price=8.0),
    }
    page.scores = {first.key: 61.0, second.key: 42.0}
    page._render_table()
    page.table.sortItems(4, Qt.SortOrder.AscendingOrder)

    assert page.table.horizontalHeaderItem(13).text() == "评分"
    assert page.table.item(0, 4).text() == "8.00"
    assert page._security_at_row(0).key == second.key
    app.processEvents()


def test_intraday_chart_keeps_zero_line_reference_and_percentage_axis() -> None:
    app = QApplication.instance() or QApplication([])
    frame = sample_history(30)
    frame["date"] = pd.date_range("2026-07-15 09:31", periods=len(frame), freq="min")
    chart = MarketChart()
    chart.set_data(frame, reference_price=10.0, percentage_axis=True)

    assert chart._reference_price == 10.0
    assert chart._percentage_axis is True
    chart.resize(900, 600)
    chart.show()
    app.processEvents()
    chart.close()


def test_classic_indicators_have_more_score_weight_and_detailed_text() -> None:
    classic = IndicatorDefinition("趋势", "MACD 柱", "MACD_HIST", "趋势动量柱")
    extended = IndicatorDefinition("趋势", "扩展输出", "PTA_SAMPLE", "扩展指标")
    snapshots = [
        IndicatorSnapshot(classic, 1.0, "偏多"),
        IndicatorSnapshot(extended, -1.0, "偏空"),
    ]

    result = dimension_composites(snapshots)["趋势"]
    assert indicator_weight(classic) > indicator_weight(extended)
    assert float(result["score"]) > 75
    description = detailed_indicator_description(classic)
    assert "计算与解读" in description
    assert "评分预设权重" in description
    assert len(description) > 80


def test_detail_first_paint_does_not_expand_hundreds_of_indicators(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    repository = Repository(tmp_path / "monitor.db")
    page = DetailPage(repository, provider, QThreadPool())
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    history = sample_history(180)
    monkeypatch.setattr(
        provider,
        "get_detail_bundle",
        lambda *_args, **_kwargs: DetailBundle(security=security, history=history),
    )

    _, _, frame = page._load_bundle_and_indicators(security, "qfq", 1)

    assert not any(str(column).startswith("PTA_") for column in frame.columns)
    assert len(frame.columns) < 150


def test_market_dashboard_calculates_breadth_and_leading_sector(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    spot = pd.DataFrame(
        {
            "代码": ["600000", "000001", "300750"],
            "名称": ["A", "B", "C"],
            "最新价": [10.0, 11.0, 12.0],
            "涨跌幅": [10.0, -10.0, 1.5],
            "成交额": [100.0, 200.0, 300.0],
        }
    )
    sectors = pd.DataFrame(
        {
            "板块名称": ["银行", "电池"],
            "涨跌幅": [1.0, 3.0],
            "领涨股票": ["A", "C"],
            "领涨股票-涨跌幅": [2.0, 6.0],
        }
    )
    monkeypatch.setattr(provider, "refresh_quotes", lambda _items: {})

    def fake_cache(name, _code, _loader, _age):  # type: ignore[no-untyped-def]
        return spot.copy() if name == "market_breadth" else sectors.copy()

    monkeypatch.setattr(provider, "_load_extra_with_cache", fake_cache)
    result = provider.get_market_dashboard()

    assert result.breadth["up"] == 2
    assert result.breadth["down"] == 1
    assert result.breadth["limit_up"] == 1
    assert result.sectors.iloc[0]["行业"] == "电池"


def test_main_navigation_keeps_empty_detail_until_watchlist_selection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        Repository(tmp_path / "monitor.db"), DataProvider(tmp_path / "cache")
    )
    window.show_page("detail")

    assert window.stack.currentWidget() is window.detail_page
    assert window.detail_page.security is None
    assert window.navigation_buttons["detail"].isChecked()
    window.show_page("strategy")
    assert window.stack.currentWidget() is window.strategy_page
    window.close()
    app.processEvents()
