from __future__ import annotations

from collections.abc import Iterable
from html import escape

import pandas as pd
from PySide6.QtCore import QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .factor_models import (
    FACTOR_CATALOG,
    FACTOR_CATEGORIES,
    FACTOR_MODELS,
    FactorDefinition,
    FactorModelDefinition,
)
from .historical_store import HistoricalStore
from .models import Quote, Security, SecurityType
from .paper_trading import CONDITION_FIELDS, PaperRule, PaperTradingService
from .repository import Repository
from .time_utils import beijing_now
from .ui_common import MetricCard, Worker, configure_table, section_title


class FactorLibraryPage(QWidget):
    model_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._visible_factors: list[FactorDefinition] = []
        self._visible_models: list[FactorModelDefinition] = list(FACTOR_MODELS)
        root = QVBoxLayout(self)
        root.addWidget(
            section_title(
                "量化因子模型库",
                "依据《A股量化因子公式与平台实现指南》整理 · 公式、解释、方向、数据要求与回测状态完整保留",
            )
        )
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索因子名称、公式或解释")
        self.search.textChanged.connect(self._reload_factors)
        self.category = QComboBox()
        self.category.addItem("全部分类", "")
        for category in FACTOR_CATEGORIES:
            self.category.addItem(category, category)
        self.category.currentIndexChanged.connect(self._reload_factors)
        self.count_label = QLabel()
        self.count_label.setObjectName("Muted")
        controls.addWidget(self.search, 1)
        controls.addWidget(self.category)
        controls.addWidget(self.count_label)
        root.addLayout(controls)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.factor_table = QTableWidget(0, 5)
        self.factor_table.setHorizontalHeaderLabels(
            ["分类", "因子", "方向", "回测", "数据要求"]
        )
        configure_table(self.factor_table)
        self.factor_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.factor_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.factor_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.factor_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.factor_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.factor_table.itemSelectionChanged.connect(self._show_factor)
        self.factor_table.setMinimumWidth(620)
        self.factor_detail = QTextBrowser()
        self.factor_detail.setOpenExternalLinks(False)
        split.addWidget(self.factor_table)
        split.addWidget(self.factor_detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 3)

        model_header = QHBoxLayout()
        model_header.addWidget(QLabel("因子模型与回测模板"))
        model_header.addStretch(1)
        self.apply_button = QPushButton("将选中模型载入回测")
        self.apply_button.setObjectName("Primary")
        self.apply_button.clicked.connect(self._request_model)
        model_header.addWidget(self.apply_button)
        root.addLayout(model_header)
        self.model_table = QTableWidget(0, 5)
        self.model_table.setHorizontalHeaderLabels(
            ["分类", "模型", "组合公式", "数据要求", "状态"]
        )
        configure_table(self.model_table)
        self.model_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.model_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.model_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.model_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.model_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self.model_table.itemDoubleClicked.connect(lambda *_: self._request_model())
        self.model_table.setMinimumHeight(205)
        root.addWidget(self.model_table, 2)
        note = QLabel(
            "回测状态“可执行”表示现有本地日线字段足以计算；财务、资金、筹码和分析师模型只有在历史时点快照齐全后才开放，避免未来函数与编造数据。"
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)
        self._reload_factors()
        self._reload_models()

    def _reload_factors(self) -> None:
        query = self.search.text().strip().lower()
        category = str(self.category.currentData() or "")
        self._visible_factors = [
            item
            for item in FACTOR_CATALOG
            if (not category or item.category == category)
            and (
                not query
                or query
                in f"{item.name} {item.formula} {item.explanation} {item.data_requirement}".lower()
            )
        ]
        self.factor_table.setRowCount(len(self._visible_factors))
        for row, item in enumerate(self._visible_factors):
            values = (
                item.category,
                item.name,
                item.direction,
                "本地可计算" if item.executable else "需扩展数据",
                item.data_requirement,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(item.explanation)
                self.factor_table.setItem(row, column, cell)
        self.count_label.setText(
            f"显示 {len(self._visible_factors)} / {len(FACTOR_CATALOG)} 个"
        )
        if self._visible_factors:
            self.factor_table.selectRow(0)
        else:
            self.factor_detail.clear()

    def _show_factor(self) -> None:
        row = self.factor_table.currentRow()
        if row < 0 or row >= len(self._visible_factors):
            return
        item = self._visible_factors[row]
        executable = (
            f"<p><b>本地执行公式</b><br><code>{escape(item.executable_formula)}</code></p>"
            if item.executable
            else "<p><b>本地执行状态</b><br>当前仓库字段不足，暂不允许直接回测。</p>"
        )
        caveat = (
            f"<p><b>口径与风险提示</b><br>{escape(item.caveat)}</p>"
            if item.caveat
            else ""
        )
        self.factor_detail.setHtml(
            f"<h2>{escape(item.name)}</h2>"
            f"<p><b>分类</b>　{escape(item.category)}　　<b>方向</b>　{escape(item.direction)}</p>"
            f"<p><b>研究公式</b><br><code>{escape(item.formula)}</code></p>"
            f"<p><b>详细解释</b><br>{escape(item.explanation)}</p>"
            f"<p><b>所需数据</b><br>{escape(item.data_requirement)}</p>"
            f"{executable}{caveat}"
        )

    def _reload_models(self) -> None:
        self.model_table.setRowCount(len(self._visible_models))
        for row, model in enumerate(self._visible_models):
            values = (
                model.category,
                model.name,
                model.formula,
                model.data_requirement,
                "可载入回测" if model.executable else "等待真实数据",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(model.explanation)
                self.model_table.setItem(row, column, cell)
        if self._visible_models:
            self.model_table.selectRow(0)

    def _request_model(self) -> None:
        row = self.model_table.currentRow()
        if row < 0 or row >= len(self._visible_models):
            return
        model = self._visible_models[row]
        if not model.executable:
            QMessageBox.information(
                self,
                "暂不可回测",
                f"“{model.name}”需要：{model.data_requirement}\n\n"
                "平台不会用当前值回填历史，也不会用估算数据冒充真实数据。",
            )
            return
        self.model_requested.emit(model.key)


class PaperConditionRow(QFrame):
    def __init__(self, first: bool = False) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        self.connector = QComboBox()
        self.connector.addItems(["首项" if first else "且", "或", "非"])
        self.field = QComboBox()
        for label, key, _ in CONDITION_FIELDS:
            self.field.addItem(label, key)
        self.operator = QComboBox()
        for label, value in ((">", ">"), (">=", ">="), ("<", "<"), ("<=", "<="), ("=", "=="), ("!=", "!=")):
            self.operator.addItem(label, value)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-1e15, 1e15)
        self.threshold.setDecimals(4)
        for widget in (self.connector, self.field, self.operator, self.threshold):
            layout.addWidget(widget)
        layout.addStretch(1)

    def value(self) -> dict[str, object]:
        connector = "且" if self.connector.currentText() == "首项" else self.connector.currentText()
        return {
            "connector": connector,
            "field": str(self.field.currentData()),
            "operator": str(self.operator.currentData()),
            "threshold": self.threshold.value(),
        }


class PaperTradingPage(QWidget):
    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        store: HistoricalStore,
        thread_pool: QThreadPool,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.provider = provider
        self.store = store
        self.thread_pool = thread_pool
        self.service = PaperTradingService(store)
        self.account_id = self.service.default_account_id()
        self._workers: set[Worker] = set()
        self._automation_running = False
        self.condition_rows: list[PaperConditionRow] = []
        self._universe: list[Security] = []
        self.timer = QTimer(self)
        self.timer.setInterval(5 * 60 * 1000)
        self.timer.timeout.connect(self._automatic_tick)
        self._build_ui()
        self._load_universe()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(
            section_title(
                "模拟交易",
                "仅本地虚拟资金 · 手动委托 · 因子模型自动交易 · 自定义条件自动交易；不会连接券商或发送真实订单",
            )
        )
        cards = QHBoxLayout()
        self.equity_card = MetricCard("账户权益")
        self.cash_card = MetricCard("可用资金")
        self.market_card = MetricCard("持仓市值")
        self.pnl_card = MetricCard("累计盈亏")
        for card in (self.equity_card, self.cash_card, self.market_card, self.pnl_card):
            cards.addWidget(card)
        root.addLayout(cards)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._manual_page(), "账户与手动交易")
        self.tabs.addTab(self._automation_page(), "自动化交易规则")
        self.tabs.addTab(self._records_page(), "委托与成交记录")
        root.addWidget(self.tabs, 1)

    def _manual_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        order = QFrame()
        order.setObjectName("Section")
        grid = QGridLayout(order)
        self.security_combo = QComboBox()
        self.security_combo.setEditable(True)
        self.security_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = self.security_combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.side = QComboBox()
        self.side.addItem("买入", "buy")
        self.side.addItem("卖出", "sell")
        self.order_type = QComboBox()
        self.order_type.addItem("市价模拟成交", "market")
        self.order_type.addItem("限价触及成交", "limit")
        self.quantity = QSpinBox()
        self.quantity.setRange(100, 100_000_000)
        self.quantity.setSingleStep(100)
        self.quantity.setValue(1000)
        self.limit_price = QDoubleSpinBox()
        self.limit_price.setRange(0, 1_000_000)
        self.limit_price.setDecimals(3)
        self.limit_price.setEnabled(False)
        self.order_type.currentIndexChanged.connect(
            lambda: self.limit_price.setEnabled(self.order_type.currentData() == "limit")
        )
        submit = QPushButton("提交模拟委托")
        submit.setObjectName("Primary")
        submit.clicked.connect(self._submit_order)
        universe_button = QPushButton("加载/更新完整证券目录")
        universe_button.clicked.connect(self._load_full_universe)
        reset = QPushButton("重置模拟账户")
        reset.clicked.connect(self._reset_account)
        grid.addWidget(QLabel("证券"), 0, 0)
        grid.addWidget(self.security_combo, 0, 1, 1, 3)
        grid.addWidget(QLabel("方向"), 1, 0)
        grid.addWidget(self.side, 1, 1)
        grid.addWidget(QLabel("委托方式"), 1, 2)
        grid.addWidget(self.order_type, 1, 3)
        grid.addWidget(QLabel("数量"), 2, 0)
        grid.addWidget(self.quantity, 2, 1)
        grid.addWidget(QLabel("限价"), 2, 2)
        grid.addWidget(self.limit_price, 2, 3)
        grid.addWidget(universe_button, 3, 1)
        grid.addWidget(reset, 3, 2)
        grid.addWidget(submit, 3, 3)
        layout.addWidget(order)
        self.manual_status = QLabel("手动市价委托会先读取实时行情；读取失败时不会成交。A股按100股、T+1和交易成本模拟。")
        self.manual_status.setObjectName("Muted")
        self.manual_status.setWordWrap(True)
        layout.addWidget(self.manual_status)
        self.position_table = QTableWidget()
        configure_table(self.position_table)
        layout.addWidget(self.position_table, 1)
        return page

    def _automation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        editor = QFrame()
        editor.setObjectName("Section")
        grid = QGridLayout(editor)
        self.rule_name = QLineEdit("自动模拟规则")
        self.rule_kind = QComboBox()
        self.rule_kind.addItem("量化因子模型", "factor")
        self.rule_kind.addItem("自定义条件", "custom")
        self.rule_kind.currentIndexChanged.connect(self._rule_kind_changed)
        self.rule_model = QComboBox()
        for model in FACTOR_MODELS:
            if model.executable:
                self.rule_model.addItem(model.name, model.key)
        self.rule_action = QComboBox()
        self.rule_action.addItem("满足条件买入", "buy")
        self.rule_action.addItem("满足条件卖出", "sell")
        self.rule_scope = QComboBox()
        self.rule_scope.addItem("当前自选股票", "watchlist")
        self.rule_scope.addItem("全部本地A股", "local_stocks")
        self.position_pct = QDoubleSpinBox()
        self.position_pct.setRange(1, 100)
        self.position_pct.setValue(10)
        self.position_pct.setSuffix(" %可用资金")
        self.max_positions = QSpinBox()
        self.max_positions.setRange(1, 100)
        self.max_positions.setValue(10)
        grid.addWidget(QLabel("规则名称"), 0, 0)
        grid.addWidget(self.rule_name, 0, 1)
        grid.addWidget(QLabel("类型"), 0, 2)
        grid.addWidget(self.rule_kind, 0, 3)
        grid.addWidget(QLabel("因子模型"), 1, 0)
        grid.addWidget(self.rule_model, 1, 1)
        grid.addWidget(QLabel("动作"), 1, 2)
        grid.addWidget(self.rule_action, 1, 3)
        grid.addWidget(QLabel("扫描范围"), 2, 0)
        grid.addWidget(self.rule_scope, 2, 1)
        grid.addWidget(QLabel("单笔仓位"), 2, 2)
        grid.addWidget(self.position_pct, 2, 3)
        grid.addWidget(QLabel("最大持仓"), 3, 0)
        grid.addWidget(self.max_positions, 3, 1)
        layout.addWidget(editor)
        condition_tools = QHBoxLayout()
        self.condition_label = QLabel("自定义条件（最多5项，且/或/非）")
        add = QPushButton("＋ 条件")
        add.clicked.connect(self._add_condition)
        remove = QPushButton("－ 条件")
        remove.clicked.connect(self._remove_condition)
        condition_tools.addWidget(self.condition_label)
        condition_tools.addWidget(add)
        condition_tools.addWidget(remove)
        condition_tools.addStretch(1)
        layout.addLayout(condition_tools)
        self.conditions_layout = QVBoxLayout()
        layout.addLayout(self.conditions_layout)
        self._add_condition()
        actions = QHBoxLayout()
        save = QPushButton("保存自动规则")
        save.clicked.connect(self._save_rule)
        run = QPushButton("运行选中规则一次")
        run.setObjectName("Primary")
        run.clicked.connect(self._run_selected_rule)
        toggle = QPushButton("启用/停用选中规则")
        toggle.clicked.connect(self._toggle_rule)
        self.auto_scan = QCheckBox("交易时段每5分钟自动扫描已启用规则")
        self.auto_scan.toggled.connect(self._toggle_timer)
        actions.addWidget(save)
        actions.addWidget(run)
        actions.addWidget(toggle)
        actions.addWidget(self.auto_scan)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.rule_status = QLabel(
            "自动交易只扫描本地历史仓库最新完整日线并写入虚拟账本；同一规则、交易日、证券和方向只执行一次。"
        )
        self.rule_status.setWordWrap(True)
        self.rule_status.setObjectName("Muted")
        layout.addWidget(self.rule_status)
        self.rule_table = QTableWidget(0, 8)
        self.rule_table.setHorizontalHeaderLabels(
            ["规则", "类型", "模型/条件", "动作", "范围", "仓位", "状态", "上次运行"]
        )
        configure_table(self.rule_table)
        self.rule_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.rule_table, 1)
        self._rule_kind_changed()
        return page

    def _records_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        refresh = QPushButton("刷新记录")
        refresh.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(refresh)
        row.addStretch(1)
        layout.addLayout(row)
        tabs = QTabWidget()
        self.order_table = QTableWidget()
        self.trade_table = QTableWidget()
        configure_table(self.order_table)
        configure_table(self.trade_table)
        tabs.addTab(self.order_table, "委托记录")
        tabs.addTab(self.trade_table, "成交记录")
        layout.addWidget(tabs, 1)
        return page

    def _load_universe(self) -> None:
        values = {item.key: item for item in self.store.list_securities()}
        for item in self.repository.list_watchlist():
            values[item.key] = item
        # 已存在的证券目录缓存只做本地读取，不在应用启动时触发全市场联网。
        if (self.provider.cache_dir / "security_universe.json").exists():
            try:
                for item in self.provider.load_universe():
                    values[item.key] = item
            except Exception:
                pass
        self._set_universe(values.values())

    def _set_universe(self, securities: Iterable[Security]) -> None:
        self._universe = sorted(securities, key=lambda item: (item.name, item.code))
        self.security_combo.clear()
        for security in self._universe:
            if security.security_type is SecurityType.INDEX:
                continue
            self.security_combo.addItem(
                f"{security.name}  {security.display_code}  {security.security_type.label}",
                security,
            )

    def _load_full_universe(self) -> None:
        self.manual_status.setText("正在后台加载完整A股、ETF证券目录……")
        worker = Worker(self.provider.load_universe, True)
        self._workers.add(worker)
        worker.signals.result.connect(self._full_universe_ready)
        worker.signals.error.connect(
            lambda message: self.manual_status.setText(f"证券目录更新失败：{message}")
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        self.thread_pool.start(worker)

    def _full_universe_ready(self, value: object) -> None:
        if not isinstance(value, list):
            return
        merged = {item.key: item for item in self._universe}
        for item in value:
            if isinstance(item, Security):
                merged[item.key] = item
        self._set_universe(list(merged.values()))
        self.manual_status.setText(
            f"已载入 {len(self._universe):,} 个股票、ETF和指数；可按名称或代码搜索。"
        )

    def refresh(self) -> None:
        summary = self.service.summary(self.account_id)
        color = "#FF4D6D" if summary.total_pnl >= 0 else "#20C997"
        self.equity_card.set_value(f"{summary.equity:,.2f}", summary.name)
        self.cash_card.set_value(f"{summary.cash:,.2f}", "虚拟可用资金")
        self.market_card.set_value(f"{summary.market_value:,.2f}", f"{summary.positions} 只持仓")
        self.pnl_card.set_value(
            f"{summary.total_pnl:+,.2f}", f"{summary.total_return:+.2%}", color
        )
        self._fill(self.position_table, self.service.positions(self.account_id))
        self._fill(self.order_table, self.service.orders(self.account_id))
        self._fill(self.trade_table, self.service.trades(self.account_id))
        self._reload_rules()

    @staticmethod
    def _fill(table: QTableWidget, frame: pd.DataFrame) -> None:
        table.setColumnCount(len(frame.columns))
        table.setHorizontalHeaderLabels([str(column) for column in frame.columns])
        table.setRowCount(len(frame))
        for row_index, (_, row) in enumerate(frame.iterrows()):
            for column_index, name in enumerate(frame.columns):
                value = row[name]
                text = "—" if pd.isna(value) else (
                    f"{value:,.3f}" if isinstance(value, float) else str(value)
                )
                table.setItem(row_index, column_index, QTableWidgetItem(text))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _selected_security(self) -> Security | None:
        value = self.security_combo.currentData()
        return value if isinstance(value, Security) else None

    def _submit_order(self) -> None:
        security = self._selected_security()
        if security is None:
            QMessageBox.warning(self, "请选择证券", "请从证券下拉搜索结果中选择股票或ETF。")
            return
        self.manual_status.setText(f"正在读取 {security.name} 的实时行情并模拟撮合……")
        worker = Worker(self._fetch_and_execute, security)
        self._workers.add(worker)
        worker.signals.result.connect(self._order_finished)
        worker.signals.error.connect(
            lambda message: self.manual_status.setText(f"模拟委托失败：{message}")
        )
        worker.signals.finished.connect(lambda current=worker: self._workers.discard(current))
        self.thread_pool.start(worker)

    def _fetch_and_execute(self, security: Security) -> object:
        quote: Quote | None = self.provider.refresh_quotes([security]).get(security.key)
        if quote is None or quote.price is None or quote.price <= 0:
            raise ValueError("实时行情不可用，本次模拟委托未成交")
        return self.service.execute_order(
            self.account_id,
            security,
            str(self.side.currentData()),
            self.quantity.value(),
            float(quote.price),
            order_type=str(self.order_type.currentData()),
            limit_price=(
                self.limit_price.value()
                if self.order_type.currentData() == "limit"
                else None
            ),
        )

    def _order_finished(self, result: object) -> None:
        status = getattr(result, "status", "")
        message = getattr(result, "message", str(result))
        price = float(getattr(result, "executed_price", 0) or 0)
        self.manual_status.setText(
            f"{message}" + (f" · 模拟成交价 {price:.3f}" if status == "filled" else "")
        )
        self.refresh()

    def _reset_account(self) -> None:
        answer = QMessageBox.question(
            self,
            "重置模拟账户",
            "将清空虚拟持仓、委托和成交记录，并恢复100万元虚拟资金。已保存自动规则保留。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.service.reset_account(self.account_id)
        self.refresh()

    def _rule_kind_changed(self) -> None:
        factor = self.rule_kind.currentData() == "factor"
        self.rule_model.setEnabled(factor)
        self.rule_action.setEnabled(not factor)
        self.condition_label.setEnabled(not factor)
        for row in self.condition_rows:
            row.setEnabled(not factor)

    def _add_condition(self) -> None:
        if len(self.condition_rows) >= 5:
            QMessageBox.information(self, "条件上限", "自定义自动交易最多设置5个条件。")
            return
        row = PaperConditionRow(first=not self.condition_rows)
        self.condition_rows.append(row)
        self.conditions_layout.addWidget(row)
        self._rule_kind_changed()

    def _remove_condition(self) -> None:
        if len(self.condition_rows) <= 1:
            return
        self.condition_rows.pop().deleteLater()

    def _save_rule(self) -> None:
        try:
            rule_id = self.service.save_rule(
                self.account_id,
                self.rule_name.text(),
                str(self.rule_kind.currentData()),
                model_key=str(self.rule_model.currentData() or ""),
                action=str(self.rule_action.currentData()),
                scope=str(self.rule_scope.currentData()),
                conditions=[row.value() for row in self.condition_rows],
                position_pct=self.position_pct.value(),
                max_positions=self.max_positions.value(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "规则错误", str(exc))
            return
        self.rule_status.setText(f"已保存自动模拟规则 #{rule_id}。")
        self._reload_rules()

    def _reload_rules(self) -> None:
        rules = self.service.rules(self.account_id)
        self.rule_table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            model_or_conditions = (
                next((model.name for model in FACTOR_MODELS if model.key == rule.model_key), rule.model_key)
                if rule.rule_kind == "factor"
                else f"{len(rule.conditions)}个条件"
            )
            values = (
                rule.name,
                "因子模型" if rule.rule_kind == "factor" else "自定义条件",
                model_or_conditions,
                "模型自动买卖" if rule.rule_kind == "factor" else ("买入" if rule.action == "buy" else "卖出"),
                "当前自选" if rule.scope == "watchlist" else "全部本地A股",
                f"{rule.position_pct:g}% / {rule.max_positions}只",
                "启用" if rule.enabled else "停用",
                rule.last_run_at or "未运行",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, rule.id)
                self.rule_table.setItem(row, column, cell)
        if rules and self.rule_table.currentRow() < 0:
            self.rule_table.selectRow(0)

    def _selected_rule(self) -> PaperRule | None:
        row = self.rule_table.currentRow()
        cell = self.rule_table.item(row, 0) if row >= 0 else None
        rule_id = int(cell.data(Qt.ItemDataRole.UserRole)) if cell else -1
        return next((rule for rule in self.service.rules(self.account_id) if rule.id == rule_id), None)

    def _toggle_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        self.service.set_rule_enabled(rule.id, not rule.enabled)
        self._reload_rules()

    def _securities_for_rule(self, rule: PaperRule) -> list[Security]:
        if rule.scope == "watchlist":
            return [item for item in self.repository.list_watchlist() if item.security_type is SecurityType.STOCK]
        return [item for item in self.store.list_securities() if item.security_type is SecurityType.STOCK]

    def _run_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            QMessageBox.information(self, "请选择规则", "请先选择一条自动模拟规则。")
            return
        self._run_rule_ids([rule.id])

    def _run_rule_ids(self, rule_ids: list[int]) -> None:
        if self._automation_running or not rule_ids:
            return
        self._automation_running = True
        self.rule_status.setText("正在逐只读取本地日线并执行虚拟撮合，不会逐股下载网络数据……")
        worker = Worker(self._run_rules_impl, rule_ids)
        self._workers.add(worker)
        worker.signals.result.connect(self._rules_finished)
        worker.signals.error.connect(lambda message: self.rule_status.setText(f"自动规则失败：{message}"))
        worker.signals.finished.connect(lambda current=worker: self._workers.discard(current))
        worker.signals.finished.connect(lambda: setattr(self, "_automation_running", False))
        self.thread_pool.start(worker)

    def _run_rules_impl(self, rule_ids: list[int]) -> dict[str, int]:
        totals = {"rules": 0, "scanned": 0, "signals": 0, "filled": 0, "skipped": 0}
        rules = {rule.id: rule for rule in self.service.rules(self.account_id)}
        for rule_id in rule_ids:
            rule = rules.get(rule_id)
            if rule is None or not rule.enabled:
                continue
            result = self.service.run_rule(rule.id, self._securities_for_rule(rule))
            totals["rules"] += 1
            for key in ("scanned", "signals", "filled", "skipped"):
                totals[key] += result[key]
        return totals

    def _rules_finished(self, result: object) -> None:
        if isinstance(result, dict):
            self.rule_status.setText(
                f"自动扫描完成：{result['rules']} 条规则，扫描 {result['scanned']} 只，"
                f"信号 {result['signals']} 个，模拟成交 {result['filled']} 笔，跳过 {result['skipped']} 个。"
            )
        self.refresh()

    def _toggle_timer(self, enabled: bool) -> None:
        if enabled:
            self.timer.start()
            self.rule_status.setText("已开启北京时间交易时段每5分钟自动扫描；关闭程序后停止。")
            self._automatic_tick()
        else:
            self.timer.stop()
            self.rule_status.setText("自动定时扫描已关闭；仍可手动运行规则。")

    def _automatic_tick(self) -> None:
        now = beijing_now()
        clock = now.hour * 60 + now.minute
        in_session = now.weekday() < 5 and (
            9 * 60 + 25 <= clock <= 11 * 60 + 35 or 12 * 60 + 55 <= clock <= 15 * 60
        )
        if not in_session:
            self.rule_status.setText("当前不在北京时间交易时段，自动规则等待下一次扫描。")
            return
        enabled = [rule.id for rule in self.service.rules(self.account_id) if rule.enabled]
        self._run_rule_ids(enabled)


class QuantWorkspacePage(QWidget):
    model_requested = Signal(str)

    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        store: HistoricalStore,
        thread_pool: QThreadPool,
    ) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.factor_library = FactorLibraryPage()
        self.paper_trading = PaperTradingPage(repository, provider, store, thread_pool)
        self.factor_library.model_requested.connect(self.model_requested)
        self.tabs.addTab(self.factor_library, "量化因子模型库")
        self.tabs.addTab(self.paper_trading, "模拟交易")
        layout.addWidget(self.tabs)
