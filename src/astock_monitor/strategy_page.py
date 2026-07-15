from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QDate, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .historical_store import HistoricalStore
from .models import Security, SecurityType
from .portfolio_backtest import (
    LocalPortfolioBacktester,
    PortfolioBacktestConfig,
    PortfolioBacktestResult,
)
from .repository import Repository
from .time_utils import beijing_today
from .ui_common import Worker, configure_table, section_title


class StrategyBacktestPage(QWidget):
    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        thread_pool: QThreadPool,
        custom_indicator_page: QWidget | None = None,
        historical_store: HistoricalStore | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.provider = provider
        self.thread_pool = thread_pool
        self.historical_store = historical_store or HistoricalStore(
            repository.database_path
        )
        self.backtester = LocalPortfolioBacktester(self.historical_store)
        self._workers: set[Worker] = set()
        self._running = False
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._quant_placeholder(), "量化")
        self.tabs.addTab(self._backtest_page(), "回测")
        self.tabs.addTab(custom_indicator_page or self._indicator_placeholder(), "指标")
        root.addWidget(self.tabs)

    @staticmethod
    def _quant_placeholder() -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(section_title("量化", "策略编排入口已预留"))
        label = QLabel("后续可在这里管理定时策略、实盘信号和策略版本。")
        label.setObjectName("Muted")
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    @staticmethod
    def _indicator_placeholder() -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            section_title("自定义指标", "请从股票详情载入证券后编辑和绘图")
        )
        text = QLabel("自定义指标编辑器会在股票详情组件初始化后挂载到这里。")
        text.setObjectName("Muted")
        layout.addWidget(text)
        layout.addStretch(1)
        return page

    def _backtest_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            section_title(
                "自定义公式回测",
                "只读本地历史仓库；信号收盘确认、下一交易日撮合；结果和审计记录持久化",
            )
        )
        form = QFormLayout()
        template_row = QHBoxLayout()
        self.strategy_name = QLineEdit("自定义公式回测")
        self.template = QComboBox()
        self.template.addItem("均线金叉/死叉", "ma")
        self.template.addItem("20日突破", "breakout")
        self.template.addItem("RSI均值回归", "mean_reversion")
        self.template.currentIndexChanged.connect(self._load_template)
        template_row.addWidget(self.strategy_name, 1)
        template_row.addWidget(self.template)
        form.addRow("策略名称/教学模板", template_row)
        universe_row = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("单股回测", "single")
        self.scope.addItem("自选组回测", "group")
        self.scope.addItem("动态选股组回测", "dynamic")
        self.scope.addItem("全部A股横截面回测", "all")
        self.scope.addItem("全部ETF回测", "all_etf")
        self.target = QComboBox()
        self._reload_targets()
        self.scope.currentIndexChanged.connect(self._reload_targets)
        universe_row.addWidget(self.scope)
        universe_row.addWidget(self.target, 1)
        form.addRow("回测范围", universe_row)
        date_row = QHBoxLayout()
        current = beijing_today()
        today = QDate(current.year, current.month, current.day)
        self.start_date = QDateEdit(today.addYears(-3))
        self.end_date = QDateEdit(today)
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        date_row.addWidget(self.start_date)
        date_row.addWidget(QLabel("至"))
        date_row.addWidget(self.end_date)
        date_row.addStretch(1)
        form.addRow("回测日期", date_row)
        self.entry = QLineEdit("CROSS(SMA(close,5), SMA(close,20))")
        self.exit = QLineEdit("CROSS(SMA(close,20), SMA(close,5))")
        self.score = QLineEdit("ROC(close,20)")
        form.addRow("ENTRY 买入条件", self.entry)
        form.addRow("EXIT 卖出条件", self.exit)
        form.addRow("SCORE 横截面排序", self.score)
        parameters = QHBoxLayout()
        self.holding = QSpinBox()
        self.holding.setRange(0, 1000)
        self.holding.setSuffix(" 天")
        self.take_profit = QDoubleSpinBox()
        self.take_profit.setRange(0, 1000)
        self.take_profit.setSuffix(" %")
        self.stop_loss = QDoubleSpinBox()
        self.stop_loss.setRange(0, 100)
        self.stop_loss.setSuffix(" %")
        self.position = QDoubleSpinBox()
        self.position.setRange(1, 100)
        self.position.setValue(100)
        self.position.setSuffix(" %")
        self.max_positions = QSpinBox()
        self.max_positions.setRange(1, 100)
        self.max_positions.setValue(10)
        self.initial_cash = QDoubleSpinBox()
        self.initial_cash.setRange(10_000, 1_000_000_000)
        self.initial_cash.setDecimals(0)
        self.initial_cash.setValue(1_000_000)
        self.initial_cash.setSuffix(" 元")
        for label, widget in (
            ("固定持有", self.holding),
            ("止盈", self.take_profit),
            ("止损", self.stop_loss),
            ("仓位", self.position),
            ("最大持仓", self.max_positions),
            ("初始资金", self.initial_cash),
        ):
            parameters.addWidget(QLabel(label))
            parameters.addWidget(widget)
        form.addRow("持仓参数", parameters)
        costs = QHBoxLayout()
        self.commission = QDoubleSpinBox()
        self.commission.setDecimals(4)
        self.commission.setValue(0.03)
        self.commission.setSuffix(" %")
        self.stamp = QDoubleSpinBox()
        self.stamp.setDecimals(4)
        self.stamp.setValue(0.05)
        self.stamp.setSuffix(" %")
        self.slippage = QDoubleSpinBox()
        self.slippage.setDecimals(4)
        self.slippage.setValue(0.02)
        self.slippage.setSuffix(" %")
        self.adjustment = QComboBox()
        self.adjustment.addItem("前复权", "qfq")
        self.adjustment.addItem("后复权", "hfq")
        self.adjustment.addItem("不复权", "")
        self.execution = QComboBox()
        self.execution.addItem("T+1开盘", "next_open")
        self.execution.addItem("T+1收盘（敏感性）", "next_close")
        self.execution.addItem("T+1 VWAP近似", "vwap_approx")
        for label, widget in (
            ("手续费", self.commission),
            ("印花税", self.stamp),
            ("滑点", self.slippage),
            ("复权", self.adjustment),
            ("执行价", self.execution),
        ):
            costs.addWidget(QLabel(label))
            costs.addWidget(widget)
        form.addRow("交易成本", costs)
        constraints = QHBoxLayout()
        self.position_sizing = QComboBox()
        self.position_sizing.addItem("等权组合", "equal_weight")
        self.position_sizing.addItem("固定仓位", "fixed")
        self.position_sizing.addItem("评分权重", "score_weight")
        self.rebalance = QComboBox()
        self.rebalance.addItem("每日", "daily")
        self.rebalance.addItem("每周", "weekly")
        self.rebalance.addItem("每月", "monthly")
        self.exclude_st = QCheckBox("排除ST")
        self.exclude_st.setChecked(True)
        self.minimum_listing = QSpinBox()
        self.minimum_listing.setRange(0, 1000)
        self.minimum_listing.setValue(20)
        self.minimum_listing.setSuffix(" 日")
        constraints.addWidget(QLabel("仓位方法"))
        constraints.addWidget(self.position_sizing)
        constraints.addWidget(QLabel("调仓"))
        constraints.addWidget(self.rebalance)
        constraints.addWidget(self.exclude_st)
        constraints.addWidget(QLabel("最少上市"))
        constraints.addWidget(self.minimum_listing)
        constraints.addStretch(1)
        form.addRow("组合与范围约束", constraints)
        layout.addLayout(form)
        action = QHBoxLayout()
        self.status = QLabel(
            "支持停牌、涨跌停、ST、新股前5日、T+1、整手和卖出印花税约束。"
        )
        self.status.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.run_button = QPushButton("开始回测")
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(self._run)
        action.addWidget(self.status, 1)
        action.addWidget(self.progress)
        action.addWidget(self.cancel_button)
        action.addWidget(self.run_button)
        layout.addLayout(action)
        self.result_tabs = QTabWidget()
        self.metric_table = QTableWidget(0, 2)
        configure_table(self.metric_table)
        self.metric_table.setHorizontalHeaderLabels(["指标", "结果"])
        self.metric_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.trade_table = QTableWidget()
        configure_table(self.trade_table)
        self.annual_table = QTableWidget()
        configure_table(self.annual_table)
        self.monthly_table = QTableWidget()
        configure_table(self.monthly_table)
        self.signal_table = QTableWidget()
        configure_table(self.signal_table)
        self.unfilled_table = QTableWidget()
        configure_table(self.unfilled_table)
        self.position_table = QTableWidget()
        configure_table(self.position_table)
        self.equity_table = QTableWidget()
        configure_table(self.equity_table)
        self.audit_text = QPlainTextEdit()
        self.audit_text.setReadOnly(True)
        for widget, label in (
            (self.metric_table, "绩效指标"),
            (self.trade_table, "所有交易记录"),
            (self.annual_table, "年度收益"),
            (self.monthly_table, "月度收益"),
            (self.signal_table, "信号原始数据"),
            (self.unfilled_table, "未成交订单"),
            (self.position_table, "每日持仓"),
            (self.equity_table, "资金曲线"),
            (self.audit_text, "可复核报告"),
        ):
            self.result_tabs.addTab(widget, label)
        layout.addWidget(self.result_tabs, 1)
        return page

    def _reload_targets(self) -> None:
        self.target.clear()
        mode = str(self.scope.currentData())
        if mode == "group":
            for group in self.repository.list_groups():
                self.target.addItem(group.name, ("group", group.id))
        elif mode == "dynamic":
            with self.historical_store.connect() as db:
                rows = db.execute(
                    "SELECT id,name FROM dynamic_groups ORDER BY name"
                ).fetchall()
            for row in rows:
                self.target.addItem(row["name"], ("dynamic", int(row["id"])))
            if not rows:
                self.target.addItem("暂无动态选股组", None)
        elif mode == "all":
            self.target.addItem("全部A股（仅本地仓库）", "all")
        elif mode == "all_etf":
            self.target.addItem("全部ETF（仅本地仓库）", "all_etf")
        else:
            for security in self.repository.list_watchlist():
                self.target.addItem(f"{security.name} {security.code}", security)

    def _load_template(self) -> None:
        template = str(self.template.currentData())
        formulas = {
            "ma": (
                "均线金叉/死叉",
                "CROSS(SMA(close,5), SMA(close,20))",
                "CROSS(SMA(close,20), SMA(close,5))",
                "ROC(close,20)",
            ),
            "breakout": (
                "20日突破",
                "close > REF(HHV(close,20),1)",
                "close < SMA(close,10)",
                "ROC(close,20)",
            ),
            "mean_reversion": (
                "RSI均值回归",
                "close < SMA(close,20) - 2*STD(close,20)",
                "close >= SMA(close,20)",
                "-ZSCORE(close,20)",
            ),
        }
        name, entry, exit_formula, score = formulas[template]
        self.strategy_name.setText(name)
        self.entry.setText(entry)
        self.exit.setText(exit_formula)
        self.score.setText(score)

    def _config(self) -> PortfolioBacktestConfig:
        return PortfolioBacktestConfig(
            name=self.strategy_name.text().strip() or "自定义公式回测",
            universe=str(self.scope.currentData()),
            start_date=self.start_date.date().toString("yyyy-MM-dd"),
            end_date=self.end_date.date().toString("yyyy-MM-dd"),
            adjustment=str(self.adjustment.currentData()),
            entry_formula=self.entry.text(),
            exit_formula=self.exit.text(),
            score_formula=self.score.text(),
            holding_days=self.holding.value(),
            take_profit_pct=self.take_profit.value(),
            stop_loss_pct=self.stop_loss.value(),
            initial_cash=self.initial_cash.value(),
            position_sizing=str(self.position_sizing.currentData()),
            position_pct=self.position.value(),
            max_positions=self.max_positions.value(),
            rebalance_frequency=str(self.rebalance.currentData()),
            commission_rate=self.commission.value() / 100,
            min_commission=5,
            stamp_tax_rate=self.stamp.value() / 100,
            slippage_pct=self.slippage.value(),
            execution_price=str(self.execution.currentData()),
            exclude_st=self.exclude_st.isChecked(),
            minimum_listing_days=self.minimum_listing.value(),
        )

    def _run(self) -> None:
        if self._running:
            return
        self._running = True
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.show()
        self.status.setText("正在只读本地历史仓库并回测……")
        worker = Worker(
            self._run_impl,
            self._config(),
            self.target.currentData(),
        )
        self._workers.add(worker)
        worker.signals.result.connect(self._show_result)
        worker.signals.error.connect(
            lambda message: self.status.setText(f"回测失败：{message}")
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        worker.signals.finished.connect(self._finish)
        self.thread_pool.start(worker)

    def _run_impl(
        self, config: PortfolioBacktestConfig, value: object
    ) -> PortfolioBacktestResult:
        if isinstance(value, Security):
            securities = [value]
        elif isinstance(value, tuple) and value[0] == "group":
            securities = self.repository.list_watchlist(int(value[1]))
        elif isinstance(value, tuple) and value[0] == "dynamic":
            with self.historical_store.connect() as db:
                rows = db.execute(
                    """SELECT s.code,s.name,s.security_type,s.market
                    FROM dynamic_group_members m JOIN securities s ON s.id=m.security_id
                    WHERE m.group_id=? ORDER BY m.score DESC,s.code""",
                    (int(value[1]),),
                ).fetchall()
            securities = [
                Security(
                    row["code"],
                    row["name"],
                    SecurityType(row["security_type"]),
                    row["market"],
                )
                for row in rows
            ]
        elif value == "all":
            securities = [
                item
                for item in self.historical_store.list_securities()
                if item.security_type is SecurityType.STOCK
            ]
        elif value == "all_etf":
            securities = [
                item
                for item in self.historical_store.list_securities()
                if item.security_type is SecurityType.ETF
            ]
        else:
            raise ValueError("请选择回测对象")
        benchmark_security = Security("000300", "沪深300", SecurityType.INDEX, "csi")
        return self.backtester.run(
            securities,
            config,
            benchmark=benchmark_security,
        )

    def _cancel(self) -> None:
        if self._running:
            self.backtester.cancel()
            self.status.setText("正在安全取消；已经生成的运行记录会保留。")

    def _finish(self) -> None:
        self._running = False
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.hide()

    def _show_result(self, value: object) -> None:
        if not isinstance(value, PortfolioBacktestResult):
            return
        self.metric_table.setRowCount(len(value.metrics))
        for row, (name, metric) in enumerate(value.metrics.items()):
            self.metric_table.setItem(row, 0, QTableWidgetItem(name))
            self.metric_table.setItem(
                row,
                1,
                QTableWidgetItem(
                    f"{metric:.4f}" if isinstance(metric, float) else str(metric)
                ),
            )
        self._fill(self.trade_table, value.trades)
        self._fill(self.annual_table, value.annual_returns)
        self._fill(self.monthly_table, value.monthly_returns)
        self._fill(self.signal_table, value.signals)
        self._fill(self.unfilled_table, value.unfilled)
        self._fill(self.position_table, value.positions)
        self._fill(self.equity_table, value.equity)
        self.audit_text.setPlainText("\n".join(value.audit))
        self.status.setText(
            f"回测完成（运行 #{value.run_id}）：{len(value.trades)} 条成交，"
            f"{len(value.unfilled)} 条未成交。"
        )

    @staticmethod
    def _fill(table: QTableWidget, frame: pd.DataFrame) -> None:
        table.setColumnCount(len(frame.columns))
        table.setHorizontalHeaderLabels([str(c) for c in frame.columns])
        table.setRowCount(len(frame))
        for r, (_, row) in enumerate(frame.iterrows()):
            for c, name in enumerate(frame.columns):
                table.setItem(r, c, QTableWidgetItem(str(row[name])))
