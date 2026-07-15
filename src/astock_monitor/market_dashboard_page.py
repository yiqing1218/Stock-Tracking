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
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import (
    MARKET_DASHBOARD_INDICES,
    DataProvider,
    MarketDashboardBundle,
)
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
    ) -> None:
        super().__init__(parent)
        self.provider = provider
        self.thread_pool = thread_pool
        self._running = False
        self._active_workers: set[Worker] = set()
        self._boards = pd.DataFrame()
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self.refresh)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
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
            "median_change": MetricCard("全A中位涨幅"),
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
        self.gainer_table = self._mover_table("涨幅前列")
        self.loser_table = self._mover_table("跌幅前列")
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
        median = float(breadth.get("median_change", 0.0))
        self.breadth_cards["median_change"].set_value(
            format_percent(median), "中位数", change_color(median)
        )
        self.breadth_cards["amount"].set_value(
            format_number(float(breadth.get("amount", 0.0))), "沪深京合计"
        )

        self._boards = result.boards.copy()
        self._filter_boards()
        self._fill_movers(self.gainer_table, result.gainers)
        self._fill_movers(self.loser_table, result.losers)
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
                if column == 2:
                    item.setForeground(QColor(change_color(change)))
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(row_index, column, item)

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
