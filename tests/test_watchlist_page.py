from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astock_monitor.data_provider import DataProvider  # noqa: E402
from astock_monitor.models import Security, SecurityType  # noqa: E402
from astock_monitor.repository import Repository  # noqa: E402
from astock_monitor.watchlist_page import WatchlistPage  # noqa: E402


def test_full_universe_survives_reentering_watchlist(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = QApplication.instance() or QApplication([])
    repository = Repository(tmp_path / "watchlist.db")
    provider = DataProvider(tmp_path / "cache")
    page = WatchlistPage(repository, provider, QThreadPool())
    # 隔离网络与行情线程，只验证“载入目录 → 搜索 → 返回页面 → 再搜索”的状态生命周期。
    page.refresh_quotes = lambda: None  # type: ignore[method-assign]
    page._load_universe_async = lambda: None  # type: ignore[method-assign]
    full_universe = [
        Security("601318", "中国平安", SecurityType.STOCK, "sh"),
        Security("300750", "宁德时代", SecurityType.STOCK, "sz"),
    ]

    page._on_universe_loaded(full_universe)
    page.search_edit.setText("601318")
    app.processEvents()
    assert page.search_matches[0].code == "601318"

    page.start()  # 模拟从详情页返回自选页
    page.search_edit.setText("300750")
    app.processEvents()

    assert page.search_matches[0].code == "300750"
    assert {item.code for item in full_universe}.issubset(
        {item.code for item in page.universe}
    )
