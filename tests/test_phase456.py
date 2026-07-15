from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astock_monitor import alerts as alerts_module  # noqa: E402
from astock_monitor.alerts import MessagePage  # noqa: E402
from astock_monitor.company_events import (
    CompanyEventService,
    NormalizedCompanyEvent,
)
from astock_monitor.data_provider import DataProvider, MarketDashboardBundle
from astock_monitor.detail_page import DetailPage
from astock_monitor.historical_store import HistoricalStore
from astock_monitor.market_analysis import MarketAnalysisService
from astock_monitor.models import Security, SecurityType
from astock_monitor.portfolio_backtest import (
    LocalPortfolioBacktester,
    PortfolioBacktestConfig,
)
from astock_monitor.repository import Repository


class _EventAdapter:
    name = "测试官方源"

    def __init__(self, security: Security) -> None:
        self.security = security
        self.version = 1

    def fetch(self, security: Security, start: date, end: date):  # type: ignore[no-untyped-def]
        values = [
            NormalizedCompanyEvent(
                security,
                "periodic_report",
                "2025年年度报告",
                "2026-03-01",
                self.name,
                source_document_id="notice-1",
                source_url="https://example.com/notice-1",
                official_source=True,
                raw_payload={"version": 1},
            )
        ]
        if self.version > 1:
            values.append(
                NormalizedCompanyEvent(
                    security,
                    "share_repurchase",
                    "首次实施股份回购",
                    "2026-03-02",
                    self.name,
                    event_subtype="first_execution",
                    source_document_id="notice-2",
                    source_url="https://example.com/notice-2",
                    official_source=True,
                    raw_payload={"version": 2},
                )
            )
        return values


def _history(size: int = 120) -> pd.DataFrame:
    close = np.r_[
        np.full(35, 10.0),
        np.linspace(10.0, 16.0, 45),
        np.linspace(16.0, 12.0, size - 80),
    ]
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-02", periods=size),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
            "amount": close * 1_000_000,
            "turnover": 1.0,
            "pct_change": pd.Series(close).pct_change() * 100,
        }
    )


def test_stage_four_first_sync_does_not_notify_and_later_sync_does(tmp_path) -> None:
    repository = Repository(tmp_path / "monitor.db")
    store = HistoricalStore(repository.database_path)
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    adapter = _EventAdapter(security)
    service = CompanyEventService(store, repository, adapters=(adapter,))

    first = service.sync_security(security)
    assert first.inserted == 1
    assert first.notified == 0
    assert repository.unread_notification_count() == 0

    adapter.version = 2
    second = service.sync_security(security)
    assert second.inserted == 1
    assert second.notified == 1
    assert repository.unread_notification_count() == 1
    events = service.list_events(security)
    assert {event["event_subtype"] for event in events} >= {"", "first_execution"}
    assert service.raw_payloads(int(events[0]["id"]))


def test_stage_four_each_source_builds_its_own_notification_baseline(tmp_path) -> None:
    repository = Repository(tmp_path / "monitor.db")
    store = HistoricalStore(repository.database_path)
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    adapter = _EventAdapter(security)
    adapter.version = 2

    class LateSource:
        name = "延迟来源"

        def __init__(self) -> None:
            self.available = False
            self.version = 1

        def fetch(self, security: Security, start: date, end: date):  # type: ignore[no-untyped-def]
            if not self.available:
                raise RuntimeError("暂不可用")
            values = [
                NormalizedCompanyEvent(
                    security,
                    "major_contract",
                    f"重大合同{index}",
                    f"2026-03-0{index}",
                    self.name,
                    source_document_id=f"late-{index}",
                )
                for index in range(1, self.version + 1)
            ]
            return values

    late = LateSource()
    service = CompanyEventService(store, repository, adapters=(adapter, late))
    service.sync_security(security, retry_count=0)
    late.available = True
    second = service.sync_security(security, retry_count=0)
    assert second.notified == 0
    late.version = 2
    third = service.sync_security(security, retry_count=0)
    assert third.notified == 1


def test_stage_five_persists_real_observation_start_and_source(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "warehouse.db")
    service = MarketAnalysisService(store)
    bundle = MarketDashboardBundle(
        breadth={
            "up": 3200,
            "down": 1700,
            "flat": 100,
            "limit_up": 60,
            "limit_down": 8,
            "median_change": 0.8,
            "amount": 1.1e12,
        },
        boards=pd.DataFrame(
            {
                "类型": ["行业", "概念"],
                "板块名称": ["银行", "人工智能"],
                "涨跌幅": [1.2, 2.5],
                "换手率": [0.8, 3.0],
                "上涨家数": [30, 80],
                "下跌家数": [5, 20],
                "成交额": [1e10, 3e10],
            }
        ),
        trade_date=date(2026, 7, 15),
        sources={"breadth": "测试行情", "boards": "东方财富板块"},
    )
    service.persist_dashboard(bundle)
    industries = service.list_boards("行业")
    assert len(industries) == 1
    assert industries[0]["first_date"] == "2026-07-15"
    assert industries[0]["classification_source"] == "东方财富板块"
    assert 0 <= industries[0]["chengjian_heat"] <= 100


def test_market_dashboard_returns_25_gainers_and_losers(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    timestamp = pd.Timestamp("2026-07-15 14:00", tz="Asia/Shanghai").timestamp()
    changes = np.linspace(-9.5, 9.5, 60)
    spot = pd.DataFrame(
        {
            "代码": [f"60{index:04d}" for index in range(60)],
            "名称": [f"测试{index}" for index in range(60)],
            "最新价": 10 * (1 + changes / 100),
            "昨收": 10.0,
            "最高": 10 * (1 + changes / 100),
            "涨跌幅": changes,
            "成交额": 1_000_000,
            "更新时间戳": timestamp,
        }
    )
    monkeypatch.setattr(provider, "refresh_quotes", lambda _items: {})
    monkeypatch.setattr(
        provider,
        "_load_extra_with_cache",
        lambda name, *_args: (
            spot.copy() if name == "market_breadth" else pd.DataFrame()
        ),
    )
    result = provider.get_market_dashboard()
    assert len(result.gainers) == 25
    assert len(result.losers) == 25
    assert result.gainers["涨跌幅"].min() > result.losers["涨跌幅"].max()


def test_message_double_click_marks_read_and_opens_source(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    repository = Repository(tmp_path / "monitor.db")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    repository.add_notification(
        security,
        "公告",
        "测试公告",
        source_url="https://example.com/notice",
        external_key="open-source",
    )
    opened: list[str] = []
    monkeypatch.setattr(
        alerts_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    page = MessagePage(
        repository, DataProvider(tmp_path / "cache"), QThreadPool.globalInstance()
    )
    page.reload()
    page._read_row(0, 0)
    app.processEvents()
    assert repository.unread_notification_count() == 0
    assert opened == ["https://example.com/notice"]


def test_intraday_manual_button_is_hidden_and_auto_loader_is_debounced(
    tmp_path,
) -> None:
    QApplication.instance() or QApplication([])
    repository = Repository(tmp_path / "monitor.db")
    page = DetailPage(
        repository,
        DataProvider(tmp_path / "cache"),
        QThreadPool.globalInstance(),
    )
    assert page.intraday_button.isHidden()
    assert page._intraday_debounce.isSingleShot()
    assert page._intraday_debounce.interval() == 180


def test_stage_six_uses_local_bars_and_persists_auditable_run(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "warehouse.db")
    security = Security("600000", "测试股票", SecurityType.STOCK, "sh")
    benchmark = Security("000300", "沪深300", SecurityType.INDEX, "csi")
    store.upsert_bars(security, _history(), adjustment="qfq", source="unit-test")
    store.upsert_bars(benchmark, _history(), adjustment="qfq", source="unit-test")
    engine = LocalPortfolioBacktester(store)
    result = engine.run(
        [security],
        PortfolioBacktestConfig(
            start_date="2025-01-02",
            end_date="2025-12-31",
            minimum_listing_days=20,
            max_positions=1,
        ),
        benchmark=benchmark,
    )
    assert result.run_id > 0
    assert "累计收益(%)" in result.metrics
    assert any("仅本地历史仓库" in line for line in result.audit)
    buys = result.trades[result.trades.get("方向") == "买入"]
    if not buys.empty:
        assert (
            pd.to_datetime(buys["trade_date"]) > pd.to_datetime(buys["signal_date"])
        ).all()
    with store.connect() as db:
        run = db.execute(
            "SELECT status,config_hash,data_version FROM backtest_runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert run["status"] == "completed"
        assert run["config_hash"] and run["data_version"]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM backtest_equity WHERE run_id=?", (result.run_id,)
            ).fetchone()[0]
            > 0
        )


def test_stage_six_etf_sales_do_not_charge_stamp_tax(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "warehouse.db")
    etf = Security("510300", "沪深300ETF", SecurityType.ETF, "sh")
    store.upsert_bars(etf, _history(), adjustment="qfq", source="unit-test")
    result = LocalPortfolioBacktester(store).run(
        [etf],
        PortfolioBacktestConfig(
            universe="all_etf",
            start_date="2025-01-02",
            end_date="2025-12-31",
            minimum_listing_days=20,
            max_positions=1,
        ),
    )
    sells = result.trades[result.trades.get("方向") == "卖出"]
    if not sells.empty:
        assert (pd.to_numeric(sells["印花税"], errors="coerce") == 0).all()
