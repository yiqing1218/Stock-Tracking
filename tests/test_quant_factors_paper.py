from __future__ import annotations

import os

import numpy as np
import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from astock_monitor.data_provider import DataProvider  # noqa: E402
from astock_monitor.factor_models import (  # noqa: E402
    FACTOR_CATALOG,
    FACTOR_MODELS,
    model_backtest_template,
)
from astock_monitor.formula_engine import FormulaEngine  # noqa: E402
from astock_monitor.historical_store import HistoricalStore  # noqa: E402
from astock_monitor.models import Security, SecurityType  # noqa: E402
from astock_monitor.paper_trading import (  # noqa: E402
    PaperTradingService,
    compile_conditions,
)
from astock_monitor.repository import Repository  # noqa: E402
from astock_monitor.strategy_page import StrategyBacktestPage  # noqa: E402


def _history(size: int = 330) -> pd.DataFrame:
    index = np.arange(size, dtype=float)
    close = 10 + index * 0.01 + np.sin(index / 7) * 0.1
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=size),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000 + index * 100,
            "amount": close * (1_000_000 + index * 100),
            "turnover": 1.5 + np.sin(index / 11) * 0.2,
            "pct_change": pd.Series(close).pct_change() * 100,
        }
    )


def test_pdf_factor_library_is_classified_explained_and_executable() -> None:
    assert len(FACTOR_CATALOG) >= 75
    assert len({item.category for item in FACTOR_CATALOG}) >= 10
    assert all(item.formula and item.explanation and item.data_requirement for item in FACTOR_CATALOG)
    assert any(not item.executable for item in FACTOR_CATALOG)

    engine = FormulaEngine(_history())
    for factor in FACTOR_CATALOG:
        if factor.executable:
            engine.evaluate(factor.executable_formula)
    for model in FACTOR_MODELS:
        if model.executable:
            engine.evaluate(model.entry_formula)
            engine.evaluate(model.exit_formula)
            engine.evaluate(model.score_formula)


def test_factor_models_can_populate_backtest_template() -> None:
    name, entry, exit_formula, score = model_backtest_template("price_momentum")
    assert name == "中期动量模型"
    assert entry and exit_formula and score
    try:
        model_backtest_template("a_share_core")
    except ValueError as exc:
        assert "当前本地仓库尚不足" in str(exc)
    else:
        raise AssertionError("完整财务模型不应在缺少时点数据时伪装为可执行")


def test_paper_trading_manual_ledger_and_t_plus_one(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "monitor.db")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    store.upsert_bars(security, _history(), "qfq", "test")
    service = PaperTradingService(store)
    account_id = service.default_account_id()

    buy = service.execute_order(account_id, security, "buy", 1000, 13.0)
    assert buy.status == "filled"
    assert service.summary(account_id).positions == 1
    assert int(service.positions(account_id).iloc[0]["可卖"]) == 0

    sell = service.execute_order(account_id, security, "sell", 100, 13.0)
    assert sell.status == "rejected"
    assert "T+1" in sell.message
    assert len(service.orders(account_id)) == 2
    assert len(service.trades(account_id)) == 1


def test_custom_paper_conditions_compile_and_rules_persist(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "monitor.db")
    service = PaperTradingService(store)
    conditions = [
        {"connector": "且", "field": "mom20", "operator": ">", "threshold": 5},
        {"connector": "非", "field": "vol20", "operator": ">", "threshold": 50},
    ]
    formula = compile_conditions(conditions)
    assert "ROC(close,20)" in formula
    FormulaEngine(_history()).evaluate(formula)
    rule_id = service.save_rule(
        service.default_account_id(), "测试条件", "custom", conditions=conditions
    )
    assert service.rules(service.default_account_id())[0].id == rule_id


def test_factor_model_can_drive_ranked_paper_trade(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "monitor.db")
    security = Security("600000", "浦发银行", SecurityType.STOCK, "sh")
    store.upsert_bars(security, _history(), "qfq", "test")
    service = PaperTradingService(store)
    account_id = service.default_account_id()
    rule_id = service.save_rule(
        account_id,
        "动量自动交易",
        "factor",
        model_key="price_momentum",
        position_pct=10,
        max_positions=3,
    )

    result = service.run_rule(rule_id, [security])

    assert result["scanned"] == 1
    assert result["signals"] == 1
    assert result["filled"] == 1
    assert service.summary(account_id).positions == 1


def test_strategy_page_renames_custom_indicator_and_loads_model(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    repository = Repository(tmp_path / "monitor.db")
    store = HistoricalStore(repository.database_path)
    page = StrategyBacktestPage(
        repository,
        DataProvider(tmp_path / "cache"),
        QThreadPool(),
        historical_store=store,
    )
    assert [page.tabs.tabText(index) for index in range(page.tabs.count())] == [
        "量化",
        "回测",
        "自定义指标",
    ]
    assert page.quant_workspace.tabs.count() == 2
    page._load_factor_model("price_momentum")
    assert page.tabs.currentIndex() == 1
    assert "中期动量模型" in page.strategy_name.text()
    page.deleteLater()
    app.processEvents()
