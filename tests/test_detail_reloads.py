from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd  # noqa: E402
from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astock_monitor.data_provider import DataProvider  # noqa: E402
from astock_monitor.detail_page import DetailPage  # noqa: E402
from astock_monitor.models import Quote, Security, SecurityType  # noqa: E402
from astock_monitor.repository import Repository  # noqa: E402
from astock_monitor.time_utils import BEIJING_TZ, beijing_today  # noqa: E402


def history_frame(day, close: float = 10.0) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(day),
                "open": close,
                "close": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "volume": 1000,
                "amount": 10000,
                "amplitude": 4,
                "pct_change": 0,
                "change": 0,
                "turnover": 1,
            }
        ]
    )


def test_live_quote_is_merged_into_adjusted_daily_bar(tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = DataProvider(tmp_path / "cache")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    today = beijing_today()
    previous_day = today - timedelta(days=1)
    quote = Quote(
        security=security,
        price=11,
        previous_close=10,
        open=10.5,
        high=11.2,
        low=10.4,
        volume=2000,
        amount=22000,
    )
    quote.extra["source"] = "测试实时源"
    quote.extra["trade_datetime"] = datetime(
        today.year, today.month, today.day, 10, 30, tzinfo=BEIJING_TZ
    ).isoformat()
    provider._fetch_direct_quote = lambda _security: quote  # type: ignore[method-assign]

    merged = provider._merge_live_daily_bar(
        security, history_frame(previous_day, close=5), "qfq"
    )

    assert merged.iloc[-1]["date"].date() == today
    assert merged.iloc[-1]["close"] == 5.5
    assert merged.iloc[-1]["volume"] == 2000


def test_intraday_can_be_loaded_repeatedly_without_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    provider = DataProvider(tmp_path / "cache")
    repository = Repository(tmp_path / "watchlist.db")
    pool = QThreadPool()
    page = DetailPage(repository, provider, pool)
    page.security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    calls: list[int] = []
    today = beijing_today()

    def fake_intraday(_security, _day, _period):  # type: ignore[no-untyped-def]
        calls.append(1)
        frame = pd.concat(
            [
                history_frame(today, 10.0),
                history_frame(today, 10.1),
                history_frame(today, 10.2),
            ],
            ignore_index=True,
        )
        frame["date"] = pd.date_range(f"{today:%Y-%m-%d} 09:31", periods=3, freq="min")
        return frame, "测试分时源"

    provider.get_intraday = fake_intraday  # type: ignore[method-assign]
    for _ in range(2):
        page._load_intraday()
        assert pool.waitForDone(3000)
        for _ in range(5):
            app.processEvents()
        assert page.intraday_button.isEnabled()

    assert len(calls) == 2
    assert "已加载" in page.intraday_status.text()
