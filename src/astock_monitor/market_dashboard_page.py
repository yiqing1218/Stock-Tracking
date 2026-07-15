from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import (
    MARKET_DASHBOARD_INDICES,
    DataProvider,
    MarketDashboardBundle,
    infer_market,
)
from .historical_store import HistoricalStore
from .market_analysis import MarketAnalysisService
from .models import Security, SecurityType
from .repository import Repository
from .time_utils import beijing_now
from .ui_common import (
    DOWN_COLOR,
    UP_COLOR,
    MetricCard,
    Worker,
    change_color,
    configure_table,
    format_number,
    format_percent,
    section_title,
)
from .watchlist_page import SortableTableWidgetItem


class MarketDashboardPage(QWidget):
    """A compact A-share market pulse designed for the application's home shell."""

    def __init__(
        self,
        provider: DataProvider,
        thread_pool: QThreadPool,
        parent: QWidget | None = None,
        repository: Repository | None = None,
        store: HistoricalStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.provider = provider
        self.thread_pool = thread_pool
        self.repository = repository
        self.store = store
        self.market_analysis = MarketAnalysisService(store) if store is not None else None
        self._running = False
        self._active_workers: set[Worker] = set()
        self._boards = pd.DataFrame()
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self.refresh)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.market_tabs = QTabWidget()
        self.market_tabs.setDocumentMode(True)
        overview_page = QWidget()
        root = QVBoxLayout(overview_page)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("Section")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 11, 14, 11)
        title_group = QVBoxLayout()
        title = QLabel("A股大盘监看")
        title.setObjectName("SecurityName")
        subtitle = QLabel("当前交易日 · 核心指数 · 市场宽度 · 全部板块 · 强弱个股")
        subtitle.setObjectName("Muted")
        title_group.addWidget(title)
        title_group.addWidget(subtitle)
        header_layout.addLayout(title_group)
        header_layout.addStretch(1)
        self.status_label = QLabel("等待刷新")
        self.status_label.setObjectName("Muted")
        header_layout.addWidget(self.status_label)
        self.refresh_button = QPushButton("刷新大盘")
        self.refresh_button.setObjectName("Primary")
        self.refresh_button.clicked.connect(self.refresh)
        header_layout.addWidget(self.refresh_button)
        root.addWidget(header)

        root.addWidget(
            section_title("核心指数", "指数快照独立回退，不因单一数据源失败而整页空白")
        )
        index_grid = QGridLayout()
        index_grid.setHorizontalSpacing(9)
        index_grid.setVerticalSpacing(9)
        self.index_cards: dict[str, MetricCard] = {}
        for index, security in enumerate(MARKET_DASHBOARD_INDICES):
            card = MetricCard(security.name, "—", security.display_code)
            card.setMinimumHeight(82)
            self.index_cards[security.key] = card
            index_grid.addWidget(card, index // 4, index % 4)
        root.addLayout(index_grid)

        breadth_header = QHBoxLayout()
        breadth_header.addWidget(
            section_title("市场宽度", "全A涨跌家数比单一指数更能反映普遍强弱")
        )
        breadth_header.addStretch(1)
        self.source_label = QLabel("数据源：—")
        self.source_label.setObjectName("Tiny")
        breadth_header.addWidget(self.source_label)
        root.addLayout(breadth_header)
        breadth_layout = QHBoxLayout()
        breadth_layout.setSpacing(9)
        self.breadth_cards = {
            "up": MetricCard("上涨家数"),
            "down": MetricCard("下跌家数"),
            "limit_up": MetricCard("涨停家数"),
            "limit_down": MetricCard("跌停家数"),
            "broken_limit": MetricCard("炸板 / 最高连板"),
            "median_change": MetricCard("全A中位涨幅"),
            "market_score": MetricCard("澄鉴市场评分"),
            "amount": MetricCard("全A成交额"),
        }
        for card in self.breadth_cards.values():
            card.setMinimumHeight(76)
            breadth_layout.addWidget(card)
        root.addLayout(breadth_layout)

        lower = QSplitter(Qt.Orientation.Horizontal)
        boards_panel = QFrame()
        boards_panel.setObjectName("Section")
        boards_layout = QVBoxLayout(boards_panel)
        board_header = QHBoxLayout()
        board_header.addWidget(
            section_title("A股板块", "行业与概念板块完整涨跌列表，不展示个股")
        )
        board_header.addStretch(1)
        self.board_filter = QComboBox()
        self.board_filter.addItems(["全部板块", "行业", "概念"])
        self.board_filter.currentTextChanged.connect(self._filter_boards)
        board_header.addWidget(self.board_filter)
        boards_layout.addLayout(board_header)
        self.board_table = QTableWidget(0, 8)
        self.board_table.setHorizontalHeaderLabels(
            [
                "类型",
                "板块",
                "涨跌幅",
                "板块指数",
                "换手率",
                "上涨家数",
                "下跌家数",
                "总市值",
            ]
        )
        configure_table(self.board_table)
        self.board_table.setSortingEnabled(True)
        board_table_header = self.board_table.horizontalHeader()
        board_table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for index in (0, 2, 3, 4, 5, 6, 7):
            board_table_header.setSectionResizeMode(
                index, QHeaderView.ResizeMode.ResizeToContents
            )
        boards_layout.addWidget(self.board_table)
        lower.addWidget(boards_panel)

        movers_panel = QFrame()
        movers_panel.setObjectName("Section")
        movers_layout = QHBoxLayout(movers_panel)
        movers_layout.setContentsMargins(12, 12, 12, 12)
        self.gainer_table = self._mover_table("涨幅前列（25只）")
        self.loser_table = self._mover_table("跌幅前列（25只）")
        for table in (self.gainer_table, self.loser_table):
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(
                lambda position, current=table: self._show_mover_menu(current, position)
            )
        movers_layout.addWidget(self.gainer_table)
        movers_layout.addWidget(self.loser_table)
        lower.addWidget(movers_panel)
        lower.setStretchFactor(0, 6)
        lower.setStretchFactor(1, 4)
        root.addWidget(lower, 1)

        note = QLabel(
            "涨跌停家数按证券名称、代码板块、昨收价和交易所价格取整规则逐只计算；上市初期无涨跌幅限制证券不计入涨跌停。"
        )
        note.setObjectName("Tiny")
        note.setWordWrap(True)
        root.addWidget(note)
        self.market_tabs.addTab(overview_page, "市场总览")
        self.board_explorers: dict[str, tuple[QTableWidget, QTableWidget, QLabel]] = {}
        self.relative_controls: dict[
            str, tuple[QComboBox, QComboBox, QLabel]
        ] = {}
        self.market_tabs.addTab(self._build_board_explorer("行业"), "行业板块")
        self.market_tabs.addTab(self._build_board_explorer("概念"), "概念板块")
        outer.addWidget(self.market_tabs)

    def _build_board_explorer(self, board_type: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(
            section_title(
                f"{board_type}板块",
                "分类来源不混合；历史从本程序首次观测日起保存，不回填虚构历史",
            )
        )
        status = QLabel("等待市场总览刷新后载入本地板块快照。")
        status.setObjectName("Muted")
        layout.addWidget(status)
        relative = QHBoxLayout()
        relative.addWidget(QLabel("相对强弱"))
        security_combo = QComboBox()
        security_combo.setMinimumWidth(180)
        benchmark_combo = QComboBox()
        benchmark_combo.addItem("对沪深300", "hs300")
        benchmark_combo.addItem(f"对当前{board_type}板块", "board")
        calculate_button = QPushButton("计算5/20/60日")
        result_label = QLabel("需先在本地仓库同步证券及基准历史。")
        result_label.setObjectName("Muted")
        calculate_button.clicked.connect(
            lambda checked=False, kind=board_type: self._calculate_relative_strength(
                kind
            )
        )
        relative.addWidget(security_combo)
        relative.addWidget(benchmark_combo)
        relative.addWidget(calculate_button)
        relative.addWidget(result_label, 1)
        layout.addLayout(relative)
        splitter = QSplitter(Qt.Orientation.Vertical)
        table = QTableWidget(0, 9)
        table.setHorizontalHeaderLabels(
            ["板块", "涨跌幅", "澄鉴热度", "上涨", "下跌", "成交额", "资金净流入", "分类来源", "历史起点"]
        )
        configure_table(table)
        table.setSortingEnabled(True)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda position, kind=board_type: self._show_board_menu(kind, position)
        )
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        history = QTableWidget()
        configure_table(history)
        table.cellClicked.connect(
            lambda row, _column, kind=board_type: self._show_board_history(kind, row)
        )
        splitter.addWidget(table)
        splitter.addWidget(history)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        explanation = QLabel(
            "澄鉴热度=当日涨幅排名40%+换手排名20%+涨跌宽度20%+成交额排名10%+资金流排名10%；"
            "只有来源存在相应字段时才参与，缺失项按中性分处理。"
        )
        explanation.setObjectName("Tiny")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.board_explorers[board_type] = (table, history, status)
        self.relative_controls[board_type] = (
            security_combo,
            benchmark_combo,
            result_label,
        )
        return page

    def _refresh_board_explorers(self) -> None:
        if self.market_analysis is None:
            return
        self._reload_relative_targets()
        for board_type, (table, history, status) in self.board_explorers.items():
            rows = self.market_analysis.list_boards(board_type)
            table.setSortingEnabled(False)
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                numeric_values = (
                    None,
                    self._number(row["change_pct"]),
                    self._number(row["chengjian_heat"]),
                    self._number(row["up_count"]),
                    self._number(row["down_count"]),
                    self._number(row["amount"]),
                    self._number(row["fund_flow"]),
                    None,
                    None,
                )
                values = (
                    row["board_name"],
                    format_percent(self._number(row["change_pct"])),
                    format_number(self._number(row["chengjian_heat"]), decimals=1),
                    format_number(self._number(row["up_count"]), decimals=0),
                    format_number(self._number(row["down_count"]), decimals=0),
                    format_number(self._number(row["amount"])),
                    format_number(self._number(row["fund_flow"])),
                    row["classification_source"],
                    row["first_date"] or "—",
                )
                for column, value in enumerate(values):
                    numeric = numeric_values[column]
                    item = (
                        SortableTableWidgetItem(str(value), numeric)
                        if numeric is not None
                        else QTableWidgetItem(str(value))
                    )
                    item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                    table.setItem(row_index, column, item)
            table.setSortingEnabled(True)
            table.sortItems(1, Qt.SortOrder.DescendingOrder)
            history.setRowCount(0)
            status.setText(f"本地已保存 {len(rows)} 个{board_type}板块的最新快照。")

    def _reload_relative_targets(self) -> None:
        if self.repository is None:
            return
        securities = self.repository.list_watchlist()
        for security_combo, _benchmark, _result in self.relative_controls.values():
            current = security_combo.currentData()
            current_key = current.key if isinstance(current, Security) else ""
            security_combo.clear()
            for security in securities:
                security_combo.addItem(
                    f"{security.name} {security.code}", security
                )
            if current_key:
                for index in range(security_combo.count()):
                    value = security_combo.itemData(index)
                    if isinstance(value, Security) and value.key == current_key:
                        security_combo.setCurrentIndex(index)
                        break

    def _calculate_relative_strength(self, board_type: str) -> None:
        if self.market_analysis is None:
            return
        security_combo, benchmark_combo, result_label = self.relative_controls[
            board_type
        ]
        security = security_combo.currentData()
        if not isinstance(security, Security):
            result_label.setText("自选列表中没有可计算证券。")
            return
        if benchmark_combo.currentData() == "board":
            table = self.board_explorers[board_type][0]
            row = table.currentRow()
            item = table.item(row, 0) if row >= 0 else None
            if item is None:
                result_label.setText(f"请先选择一个{board_type}板块。")
                return
            values = self.market_analysis.relative_strength_to_board(
                security, int(item.data(Qt.ItemDataRole.UserRole))
            )
            benchmark_name = item.text()
        else:
            benchmark = Security("000300", "沪深300", SecurityType.INDEX, "csi")
            values = self.market_analysis.relative_strength(security, benchmark)
            benchmark_name = "沪深300"
        parts = [
            f"{period} {value:+.2f}%" if value is not None else f"{period} 数据不足"
            for period, value in values.items()
        ]
        result_label.setText(f"相对{benchmark_name}：" + " · ".join(parts))

    def _show_board_history(self, board_type: str, row: int) -> None:
        if self.market_analysis is None:
            return
        table, history, status = self.board_explorers[board_type]
        item = table.item(row, 0)
        if item is None:
            return
        frame = self.market_analysis.board_history(
            int(item.data(Qt.ItemDataRole.UserRole))
        )
        history.setColumnCount(len(frame.columns))
        history.setHorizontalHeaderLabels([str(column) for column in frame.columns])
        history.setRowCount(len(frame))
        for row_index, (_, values) in enumerate(frame.iterrows()):
            for column, name in enumerate(frame.columns):
                history.setItem(row_index, column, QTableWidgetItem(str(values[name])))
        status.setText(f"{item.text()}：{len(frame)} 个本地交易日观测。")

    def _show_board_menu(self, board_type: str, position) -> None:  # type: ignore[no-untyped-def]
        if self.market_analysis is None:
            return
        table, _history, _status = self.board_explorers[board_type]
        row = table.rowAt(position.y())
        item = table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        menu = QMenu(self)
        sync = menu.addAction("同步并显示当前成分股")
        selected = menu.exec(table.viewport().mapToGlobal(position))
        if selected is not sync:
            return
        board_id = int(item.data(Qt.ItemDataRole.UserRole))
        board_name = item.text()
        worker = Worker(self._sync_board_members, board_id)
        self._active_workers.add(worker)
        worker.signals.result.connect(
            lambda value, kind=board_type, name=board_name: self._show_board_members(
                kind, name, value
            )
        )
        worker.signals.error.connect(
            lambda message, kind=board_type: self.board_explorers[kind][2].setText(
                f"成分同步失败：{message}"
            )
        )
        worker.signals.finished.connect(
            lambda current=worker: self._active_workers.discard(current)
        )
        self.board_explorers[board_type][2].setText(
            f"正在同步 {board_name} 当前成分；不会覆盖历史成员区间…"
        )
        self.thread_pool.start(worker)

    def _sync_board_members(self, board_id: int) -> tuple[int, list[dict]]:
        if self.market_analysis is None:
            return 0, []
        count = self.market_analysis.sync_board_members(board_id)
        return count, self.market_analysis.board_members(board_id)

    def _show_board_members(
        self, board_type: str, board_name: str, value: object
    ) -> None:
        if not isinstance(value, tuple) or len(value) != 2:
            return
        count, rows = value
        _table, detail, status = self.board_explorers[board_type]
        columns = ["代码", "名称", "市场", "权重", "生效起点", "来源"]
        detail.setColumnCount(len(columns))
        detail.setHorizontalHeaderLabels(columns)
        detail.setRowCount(len(rows))
        keys = ("code", "name", "market", "weight", "effective_from", "source")
        for row_index, row in enumerate(rows):
            for column, key in enumerate(keys):
                detail.setItem(row_index, column, QTableWidgetItem(str(row.get(key) or "—")))
        status.setText(f"{board_name}：同步并保存 {count} 只当前成分股。")

    @staticmethod
    def _mover_table(title: str) -> QTableWidget:
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels([title, "最新价", "涨跌幅"])
        configure_table(table)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def start(self) -> None:
        if not self.timer.isActive():
            self.timer.start()
        self.refresh()

    def stop(self) -> None:
        self.timer.stop()

    def refresh(self) -> None:
        if self._running:
            return
        self._running = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("刷新中…")
        self.status_label.setText("正在并行加载指数、当前交易日全A快照和全部板块…")
        worker = Worker(self.provider.get_market_dashboard)
        self._active_workers.add(worker)
        worker.signals.result.connect(self._on_loaded)
        worker.signals.error.connect(self._on_error)
        worker.signals.finished.connect(
            lambda current=worker: self._active_workers.discard(current)
        )
        worker.signals.finished.connect(self._finish)
        self.thread_pool.start(worker)

    def _on_loaded(self, result: object) -> None:
        if not isinstance(result, MarketDashboardBundle):
            return
        for security in MARKET_DASHBOARD_INDICES:
            quote = result.quotes.get(security.key)
            card = self.index_cards[security.key]
            if quote is None or quote.price is None:
                card.set_value("—", "暂未取得行情")
                continue
            color = change_color(quote.change_pct)
            card.set_value(
                format_number(quote.price),
                f"{format_percent(quote.change_pct)}  {quote.extra.get('source', '公开行情')} ",
                color,
            )

        breadth = result.breadth
        self.breadth_cards["up"].set_value(
            str(int(breadth.get("up", 0))), "家", UP_COLOR
        )
        self.breadth_cards["down"].set_value(
            str(int(breadth.get("down", 0))), "家", DOWN_COLOR
        )
        self.breadth_cards["limit_up"].set_value(
            str(int(breadth.get("limit_up", 0))), "逐只按涨停价统计", UP_COLOR
        )
        self.breadth_cards["limit_down"].set_value(
            str(int(breadth.get("limit_down", 0))), "逐只按跌停价统计", DOWN_COLOR
        )
        self.breadth_cards["broken_limit"].set_value(
            f"{int(breadth.get('broken_limit', 0))} / "
            + (
                str(int(breadth["max_limit_streak"]))
                if breadth.get("max_limit_streak") is not None
                else "—"
            ),
            "盘中炸板家数 / 涨停池真实连板字段",
        )
        median = float(breadth.get("median_change", 0.0))
        self.breadth_cards["median_change"].set_value(
            format_percent(median), "中位数", change_color(median)
        )
        market_score = MarketAnalysisService.market_score(breadth)
        self.breadth_cards["market_score"].set_value(
            f"{market_score:.0f}/100", "宽度60% · 中位25% · 涨停生态15%"
        )
        self.breadth_cards["amount"].set_value(
            format_number(float(breadth.get("amount", 0.0))), "沪深京合计"
        )

        self._boards = result.boards.copy()
        self._filter_boards()
        self._fill_movers(self.gainer_table, result.gainers)
        self._fill_movers(self.loser_table, result.losers)
        if self.market_analysis is not None:
            try:
                self.market_analysis.persist_dashboard(result)
                self._refresh_board_explorers()
            except Exception as exc:
                result.warnings.append(f"本地市场快照保存失败：{exc}")
        now = beijing_now()
        warning = f" · {len(result.warnings)} 个数据源回退" if result.warnings else ""
        trade_date = (
            f" · 当前交易日 {result.trade_date:%Y-%m-%d}" if result.trade_date else ""
        )
        self.status_label.setText(
            f"北京时间 {now:%Y-%m-%d %H:%M:%S} 更新{trade_date}{warning}"
        )
        sources = [result.sources.get("breadth"), result.sources.get("boards")]
        self.source_label.setText(
            "数据源：" + "；".join(item for item in sources if item)
        )

    def _filter_boards(self, *_args) -> None:  # type: ignore[no-untyped-def]
        selected = self.board_filter.currentText()
        frame = self._boards
        if selected in {"行业", "概念"} and not frame.empty and "类型" in frame:
            frame = frame[frame["类型"] == selected]
        self._fill_boards(frame)

    def _fill_boards(self, frame: pd.DataFrame) -> None:
        self.board_table.setSortingEnabled(False)
        self.board_table.setRowCount(len(frame))
        for row_index, (_, row) in enumerate(frame.iterrows()):
            change = self._number(row.get("涨跌幅"))
            raw_values = [
                (str(row.get("类型", "—")), None),
                (str(row.get("板块名称", "—")), None),
                (format_percent(change), change),
                (
                    format_number(self._number(row.get("最新价"))),
                    self._number(row.get("最新价")),
                ),
                (
                    format_percent(self._number(row.get("换手率")), signed=False),
                    self._number(row.get("换手率")),
                ),
                (
                    format_number(self._number(row.get("上涨家数")), decimals=0),
                    self._number(row.get("上涨家数")),
                ),
                (
                    format_number(self._number(row.get("下跌家数")), decimals=0),
                    self._number(row.get("下跌家数")),
                ),
                (
                    format_number(self._number(row.get("总市值"))),
                    self._number(row.get("总市值")),
                ),
            ]
            for column, (text, numeric) in enumerate(raw_values):
                item = (
                    SortableTableWidgetItem(text, numeric)
                    if numeric is not None
                    else QTableWidgetItem(text)
                )
                if column == 2:
                    item.setForeground(QColor(change_color(change)))
                if column >= 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.board_table.setItem(row_index, column, item)
        self.board_table.setSortingEnabled(True)
        self.board_table.sortItems(2, Qt.SortOrder.DescendingOrder)

    def _fill_movers(self, table: QTableWidget, frame: pd.DataFrame) -> None:
        table.setRowCount(len(frame))
        for row_index, (_, row) in enumerate(frame.iterrows()):
            change = self._number(row.get("涨跌幅"))
            values = [
                f"{row.get('名称', '—')}  {row.get('代码', '')}",
                format_number(self._number(row.get("最新价"))),
                format_percent(change),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {
                        "code": str(row.get("代码", "")).zfill(6),
                        "name": str(row.get("名称", "")),
                    },
                )
                if column == 2:
                    item.setForeground(QColor(change_color(change)))
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(row_index, column, item)

    def _show_mover_menu(self, table: QTableWidget, position) -> None:  # type: ignore[no-untyped-def]
        if self.repository is None:
            return
        row = table.rowAt(position.y())
        item = table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(value, dict) or not value.get("code"):
            return
        code = str(value["code"])
        security = Security(
            code,
            str(value.get("name") or code),
            SecurityType.STOCK,
            infer_market(code, SecurityType.STOCK),
        )
        menu = QMenu(self)
        group_menu = menu.addMenu("加入自选分组")
        actions = {}
        for group in self.repository.list_groups():
            action = group_menu.addAction(group.name)
            actions[action] = group.id
        selected = menu.exec(table.viewport().mapToGlobal(position))
        if selected in actions:
            self.repository.add_security(security, actions[selected])
            self.status_label.setText(
                f"已将 {security.name} {security.code} 加入自选分组"
            )

    def _on_error(self, message: str) -> None:
        self.status_label.setText(f"大盘数据加载失败：{message}")

    def _finish(self) -> None:
        self._running = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("刷新大盘")

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if pd.notna(result) else None
