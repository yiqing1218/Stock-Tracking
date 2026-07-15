from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import COMMON_ETFS, COMMON_INDICES, DataProvider
from .models import Quote, Security, SecurityType
from .repository import Repository
from .time_utils import beijing_now
from .ui_common import (
    DOWN_COLOR,
    UP_COLOR,
    Worker,
    change_color,
    configure_table,
    format_number,
    format_percent,
)


def normalize_security_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"[\s·._\-/]", "", value)
    return re.sub(r"^(sh|sz|bj|csi)", "", value)


def rank_security_search(
    universe: list[Security], raw_query: str, limit: int = 80
) -> list[Security]:
    query = normalize_security_query(raw_query)
    if not query:
        return []
    exact: list[Security] = []
    prefix: list[Security] = []
    contains: list[Security] = []
    for security in universe:
        code = normalize_security_query(security.code)
        display_code = normalize_security_query(security.display_code)
        name = normalize_security_query(security.name)
        if query in {code, display_code, name}:
            exact.append(security)
        elif code.startswith(query) or name.startswith(query):
            prefix.append(security)
        elif query in code or query in name:
            contains.append(security)
    type_priority = {
        SecurityType.STOCK: 0,
        SecurityType.ETF: 1,
        SecurityType.INDEX: 2,
    }
    for group in (exact, prefix, contains):
        group.sort(key=lambda item: (type_priority[item.security_type], item.code, item.name))
    return (exact + prefix + contains)[:limit]


class SortableTableWidgetItem(QTableWidgetItem):
    def __init__(self, text: str, sort_value: float | None = None) -> None:
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, SortableTableWidgetItem):
            table = self.tableWidget()
            order = (
                table.horizontalHeader().sortIndicatorOrder()
                if table is not None
                else Qt.SortOrder.AscendingOrder
            )
            missing = (
                float("inf")
                if order == Qt.SortOrder.AscendingOrder
                else float("-inf")
            )
            left = self.sort_value if self.sort_value is not None else missing
            right = other.sort_value if other.sort_value is not None else missing
            return left < right
        return super().__lt__(other)


class WatchlistPage(QWidget):
    open_security = Signal(object)

    COLUMNS = [
        "名称",
        "代码",
        "分组",
        "类型",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交额",
        "换手率",
        "量比",
        "市盈率",
        "市净率",
        "总市值",
        "评分",
        "数据源",
    ]

    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        thread_pool: QThreadPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.provider = provider
        self.thread_pool = thread_pool
        self.universe: list[Security] = []
        self.search_matches: list[Security] = []
        self.selected_search: Security | None = None
        self.search_index: dict[str, list[Security]] = {}
        self.visible_securities: list[Security] = []
        self.quotes: dict[str, Quote] = {}
        self.scores: dict[str, float] = {}
        self._quote_running = False
        self._score_running = False
        self._universe_loaded = False
        self._universe_loading = False
        self._active_workers: set[Worker] = set()
        self._build_ui()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(60_000)
        self.refresh_timer.timeout.connect(self.refresh_quotes)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        brand_mark = QLabel("澄")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setFixedSize(38, 38)
        brand_mark.setStyleSheet(
            "background:#0EA5E9; color:white; border-radius:9px; font-size:18px; font-weight:800;"
        )
        brand_group = QVBoxLayout()
        brand_group.setSpacing(0)
        name = QLabel("澄鉴 A股监看")
        name.setObjectName("AppName")
        subtitle = QLabel("A股 · ETF · 指数｜技术指标与资金结构")
        subtitle.setObjectName("Tiny")
        brand_group.addWidget(name)
        brand_group.addWidget(subtitle)
        header.addWidget(brand_mark)
        header.addLayout(brand_group)
        header.addStretch(1)
        self.market_label = QLabel(self._market_status_text())
        self.market_label.setObjectName("Muted")
        header.addWidget(self.market_label)
        self.refresh_button = QPushButton("刷新行情")
        self.refresh_button.clicked.connect(self.refresh_quotes)
        header.addWidget(self.refresh_button)
        root.addLayout(header)

        search_frame = QFrame()
        search_frame.setObjectName("Section")
        search_layout = QVBoxLayout(search_frame)
        search_layout.setContentsMargins(16, 14, 16, 14)
        search_layout.setSpacing(8)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SearchBox")
        self.search_edit.setPlaceholderText("搜索代码或名称，例如：600519、沪深300、创业板ETF")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._update_search_results)
        self.search_edit.returnPressed.connect(self._add_best_match)
        self.type_filter = QComboBox()
        self.type_filter.addItems(["全部品种", "股票", "ETF", "指数"])
        self.type_filter.currentIndexChanged.connect(self._update_search_results)
        self.type_filter.setMinimumWidth(110)
        self.add_group_combo = QComboBox()
        self.add_group_combo.setMinimumWidth(110)
        self.add_group_combo.setToolTip("新证券加入到这个分组")
        self.add_button = QPushButton("加入自选")
        self.add_button.setObjectName("Primary")
        self.add_button.clicked.connect(self._add_best_match)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.type_filter)
        row.addWidget(self.add_group_combo)
        row.addWidget(self.add_button)
        search_layout.addLayout(row)
        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(210)
        self.search_results.setVisible(False)
        self.search_results.itemDoubleClicked.connect(self._add_search_item)
        self.search_results.itemClicked.connect(self._select_search_item)
        search_layout.addWidget(self.search_results)
        self.search_hint = QLabel("证券列表正在后台更新；即使网络暂时不可用，也可使用已有自选和缓存行情。")
        self.search_hint.setObjectName("Tiny")
        search_layout.addWidget(self.search_hint)
        root.addWidget(search_frame)

        summary = QHBoxLayout()
        summary.setSpacing(10)
        self.count_label = self._summary_box("自选数量", "0")
        self.up_label = self._summary_box("上涨", "0", UP_COLOR)
        self.down_label = self._summary_box("下跌", "0", DOWN_COLOR)
        self.flat_label = self._summary_box("平盘/待更新", "0")
        summary.addWidget(self.count_label)
        summary.addWidget(self.up_label)
        summary.addWidget(self.down_label)
        summary.addWidget(self.flat_label)
        summary.addStretch(1)
        summary.addWidget(QLabel("显示分组"))
        self.group_filter = QComboBox()
        self.group_filter.setMinimumWidth(120)
        self.group_filter.currentIndexChanged.connect(self._render_table)
        summary.addWidget(self.group_filter)
        self.group_button = QPushButton("分组管理")
        self.group_button.clicked.connect(self._show_group_menu)
        summary.addWidget(self.group_button)
        self.update_label = QLabel("尚未刷新")
        self.update_label.setObjectName("Muted")
        summary.addWidget(self.update_label)
        root.addLayout(summary)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        configure_table(self.table)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setMinimumSectionSize(72)
        self.table.setSortingEnabled(True)
        header_view.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        root.addWidget(self.table, 1)

        disclaimer = QLabel(
            "数据来自公开行情接口，可能延迟或临时中断；指标用于研究与监看，不构成任何投资建议。"
        )
        disclaimer.setObjectName("Tiny")
        root.addWidget(disclaimer)

    def _summary_box(self, title: str, value: str, color: str = "#DDE7F5") -> QFrame:
        frame = QFrame()
        frame.setObjectName("Card")
        frame.setMinimumWidth(128)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        title_label = QLabel(title)
        title_label.setObjectName("Muted")
        value_label = QLabel(value)
        value_label.setStyleSheet(f"color:{color}; font-size:18px; font-weight:700;")
        value_label.setProperty("role", "value")
        layout.addWidget(title_label)
        layout.addStretch(1)
        layout.addWidget(value_label)
        return frame

    @staticmethod
    def _set_summary_value(frame: QFrame, value: str) -> None:
        for label in frame.findChildren(QLabel):
            if label.property("role") == "value":
                label.setText(value)
                return

    def start(self) -> None:
        self._reload_group_controls()
        watchlist = self.repository.list_watchlist()
        # 返回自选页时必须保留已经载入的完整证券目录；此前这里会把 7,000+
        # 条目录覆盖为“内置证券 + 自选”，导致只能搜索一次，重启后才恢复。
        existing = self.universe if self._universe_loaded else []
        base = {
            item.key: item
            for item in [*existing, *COMMON_INDICES, *COMMON_ETFS, *watchlist]
        }
        self.universe = list(base.values())
        self._build_search_index()
        self._render_table()
        self.refresh_quotes()
        if not self._universe_loaded:
            self._load_universe_async()
        self.refresh_timer.start()

    def _load_universe_async(self) -> None:
        if self._universe_loading:
            return
        self._universe_loading = True
        self.search_hint.setText("正在更新全部A股、ETF和指数列表…")
        worker = Worker(self.provider.load_universe)
        worker.signals.result.connect(self._on_universe_loaded)
        worker.signals.error.connect(
            lambda message: self.search_hint.setText(f"证券列表更新失败，继续使用缓存：{message}")
        )
        worker.signals.finished.connect(self._on_universe_finished)
        self._start_worker(worker)

    def _on_universe_finished(self) -> None:
        self._universe_loading = False

    def _start_worker(self, worker: Worker) -> None:
        self._active_workers.add(worker)
        worker.signals.finished.connect(
            lambda current=worker: self._active_workers.discard(current)
        )
        self.thread_pool.start(worker)

    def _on_universe_loaded(self, universe: object) -> None:
        if not isinstance(universe, list):
            return
        self.universe = universe
        self._build_search_index()
        self._universe_loaded = True
        self.search_hint.setText(f"已载入 {len(universe):,} 个A股、ETF和指数；双击搜索结果可直接加入。")
        self._update_search_results()

    def _filtered_universe(self) -> list[Security]:
        selected = self.type_filter.currentText()
        if selected == "股票":
            return [item for item in self.universe if item.security_type is SecurityType.STOCK]
        if selected == "ETF":
            return [item for item in self.universe if item.security_type is SecurityType.ETF]
        if selected == "指数":
            return [item for item in self.universe if item.security_type is SecurityType.INDEX]
        return self.universe

    @staticmethod
    def _normalize_query(value: str) -> str:
        return normalize_security_query(value)

    def _build_search_index(self) -> None:
        index: dict[str, list[Security]] = {}
        for security in self.universe:
            aliases = {
                self._normalize_query(security.code),
                self._normalize_query(security.display_code),
                self._normalize_query(security.name),
            }
            for alias in aliases:
                if alias:
                    index.setdefault(alias, []).append(security)
        self.search_index = index

    def _rank_search(self, raw_query: str) -> list[Security]:
        return rank_security_search(self._filtered_universe(), raw_query)

    def _update_search_results(self, *_args) -> None:  # type: ignore[no-untyped-def]
        query = self.search_edit.text()
        self.search_results.clear()
        self.search_matches = []
        self.selected_search = None
        if not query:
            self.search_results.setVisible(False)
            return
        self.search_matches = self._rank_search(query)
        for security in self.search_matches:
            item = QListWidgetItem(
                f"{security.name}    {security.display_code}    · {security.security_type.label}"
            )
            item.setData(Qt.ItemDataRole.UserRole, security.to_dict())
            self.search_results.addItem(item)
        if not self.search_matches:
            item = QListWidgetItem("未找到匹配证券；可输入纯代码、带市场代码或完整/部分名称")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.search_results.addItem(item)
        self.search_results.setVisible(True)
        self.search_results.setCurrentRow(0 if self.search_matches else -1)
        self.selected_search = self.search_matches[0] if self.search_matches else None
        if self.search_matches:
            self.search_hint.setText(
                f"找到 {len(self.search_matches)} 个结果；当前首选 {self.search_matches[0].name}（{self.search_matches[0].code}）"
            )

    def _select_search_item(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self.selected_search = Security.from_dict(value)

    def _add_best_match(self) -> None:
        item = self.search_results.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        security = Security.from_dict(value) if value else self.selected_search
        if security is None:
            query = self._normalize_query(self.search_edit.text())
            exact = self.search_index.get(query, [])
            filtered_keys = {item.key for item in self._filtered_universe()}
            security = next((item for item in exact if item.key in filtered_keys), None)
        if security is None:
            self.search_hint.setText("请先输入证券代码或名称，并从匹配结果中选择。")
            return
        self._add_security(security)

    def _add_search_item(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self._add_security(Security.from_dict(value))

    def _add_security(self, security: Security) -> None:
        group_id = self.add_group_combo.currentData()
        self.repository.add_security(security, int(group_id) if group_id is not None else None)
        self.search_edit.clear()
        self.search_results.setVisible(False)
        self.search_hint.setText(f"已加入 {security.name}（{security.code}）")
        self._reload_group_controls()
        self._render_table()
        self.refresh_quotes()

    def refresh_quotes(self) -> None:
        if self._quote_running:
            return
        securities = self.repository.list_watchlist()
        if not securities:
            self._render_table()
            return
        self._quote_running = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("更新中…")
        self.update_label.setText("正在连接公开行情源…")
        worker = Worker(self.provider.refresh_quotes, securities)
        worker.signals.result.connect(self._on_quotes_loaded)
        worker.signals.error.connect(self._on_quote_error)
        worker.signals.finished.connect(self._quote_finished)
        self._start_worker(worker)

    def _on_quotes_loaded(self, result: object) -> None:
        if isinstance(result, dict):
            self.quotes = result
        self._render_table()
        now = beijing_now()
        self.update_label.setText(f"北京时间 {now:%H:%M:%S} 更新")
        self.market_label.setText(self._market_status_text())
        self._refresh_scores()

    def _on_quote_error(self, message: str) -> None:
        self.update_label.setText(f"行情更新失败：{message}")

    def _quote_finished(self) -> None:
        self._quote_running = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("刷新行情")

    def _refresh_scores(self) -> None:
        if self._score_running:
            return
        securities = self.repository.list_watchlist()
        if not securities:
            return
        self._score_running = True
        worker = Worker(self.provider.refresh_scores, securities)
        worker.signals.result.connect(self._on_scores_loaded)
        worker.signals.finished.connect(self._score_finished)
        self._start_worker(worker)

    def _on_scores_loaded(self, result: object) -> None:
        if isinstance(result, dict):
            self.scores.update(
                {str(key): float(value) for key, value in result.items()}
            )
        self._render_table()

    def _score_finished(self) -> None:
        self._score_running = False

    def _render_table(self, *_args) -> None:  # type: ignore[no-untyped-def]
        sort_section = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        group_id = self.group_filter.currentData() if hasattr(self, "group_filter") else None
        securities = self.repository.list_watchlist(int(group_id) if group_id is not None else None)
        self.visible_securities = securities
        self.table.setRowCount(len(securities))
        up = down = flat = 0
        for row_index, security in enumerate(securities):
            quote = self.quotes.get(security.key, Quote(security))
            change_pct = quote.change_pct
            if change_pct is not None and change_pct > 0:
                up += 1
            elif change_pct is not None and change_pct < 0:
                down += 1
            else:
                flat += 1

            name_item = QTableWidgetItem(security.name)
            name_item.setData(Qt.ItemDataRole.UserRole, security.to_dict())
            name_item.setFont(self.table.font())
            self.table.setItem(row_index, 0, name_item)
            group = self.repository.group_for_security(security)
            values = [
                security.display_code,
                group.name if group else "默认分组",
                security.security_type.label,
                format_number(quote.price),
                format_percent(quote.change_pct),
                format_number(quote.change),
                format_number(quote.amount),
                format_percent(quote.turnover, signed=False),
                format_number(quote.volume_ratio),
                format_number(quote.pe),
                format_number(quote.pb),
                format_number(quote.market_cap),
                f"{self.scores[security.key]:.0f}" if security.key in self.scores else "—",
                str(quote.extra.get("source", "—")),
            ]
            for column_index, text in enumerate(values, start=1):
                numeric_values = {
                    4: quote.price,
                    5: quote.change_pct,
                    6: quote.change,
                    7: quote.amount,
                    8: quote.turnover,
                    9: quote.volume_ratio,
                    10: quote.pe,
                    11: quote.pb,
                    12: quote.market_cap,
                    13: self.scores.get(security.key),
                }
                item = (
                    SortableTableWidgetItem(text, numeric_values[column_index])
                    if column_index in numeric_values
                    else QTableWidgetItem(text)
                )
                item.setData(Qt.ItemDataRole.UserRole, security.to_dict())
                if column_index in (4, 5, 6):
                    item.setForeground(QColor(change_color(change_pct)))
                if 4 <= column_index <= 13:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column_index == 13 and numeric_values[column_index] is not None:
                    score = float(numeric_values[column_index])
                    item.setForeground(
                        QColor(UP_COLOR if score >= 60 else DOWN_COLOR if score <= 40 else "#A8B6C9")
                    )
                self.table.setItem(row_index, column_index, item)

        self._set_summary_value(self.count_label, str(len(self.repository.list_watchlist())))
        self._set_summary_value(self.up_label, str(up))
        self._set_summary_value(self.down_label, str(down))
        self._set_summary_value(self.flat_label, str(flat))
        self.table.setSortingEnabled(True)
        if sort_section >= 0:
            self.table.sortItems(sort_section, sort_order)

    def _open_row(self, row: int, _column: int) -> None:
        security = self._security_at_row(row)
        if security is not None:
            self.open_security.emit(security)

    def _security_at_row(self, row: int) -> Security | None:
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        return Security.from_dict(value) if isinstance(value, dict) else None

    def _show_context_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        row = self.table.rowAt(position.y())
        security = self._security_at_row(row)
        if security is None:
            return
        menu = QMenu(self)
        open_action = QAction("打开详情", menu)
        up_action = QAction("上移", menu)
        down_action = QAction("下移", menu)
        remove_action = QAction("从自选移除", menu)
        open_action.triggered.connect(lambda: self.open_security.emit(security))
        up_action.triggered.connect(lambda: self._move_security(security, -1))
        down_action.triggered.connect(lambda: self._move_security(security, 1))
        remove_action.triggered.connect(lambda: self._remove_security(security))
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(up_action)
        menu.addAction(down_action)
        move_menu = menu.addMenu("移到分组")
        current = self.repository.group_for_security(security)
        for group in self.repository.list_groups():
            action = QAction(group.name, move_menu)
            action.setEnabled(current is None or group.id != current.id)
            action.triggered.connect(
                lambda checked=False, group_id=group.id: self._move_to_group(security, group_id)
            )
            move_menu.addAction(action)
        menu.addSeparator()
        menu.addAction(remove_action)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _remove_security(self, security: Security) -> None:
        self.repository.remove_security(security)
        self.quotes.pop(security.key, None)
        self._render_table()
        self.search_hint.setText(f"已从自选移除 {security.name}")

    def _move_security(self, security: Security, direction: int) -> None:
        self.repository.move_security(security, direction)
        self._render_table()

    def _move_to_group(self, security: Security, group_id: int) -> None:
        self.repository.move_security_to_group(security, group_id)
        self._render_table()
        group = next((item for item in self.repository.list_groups() if item.id == group_id), None)
        self.search_hint.setText(f"已将 {security.name} 移到 {group.name if group else '目标分组'}")

    def _reload_group_controls(self) -> None:
        groups = self.repository.list_groups()
        current_filter = self.group_filter.currentData() if self.group_filter.count() else None
        current_target = self.add_group_combo.currentData() if self.add_group_combo.count() else None
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("全部分组", None)
        self.add_group_combo.clear()
        for group in groups:
            self.group_filter.addItem(group.name, group.id)
            self.add_group_combo.addItem(group.name, group.id)
        filter_index = self.group_filter.findData(current_filter)
        self.group_filter.setCurrentIndex(max(0, filter_index))
        target_index = self.add_group_combo.findData(current_target)
        self.add_group_combo.setCurrentIndex(max(0, target_index))
        self.group_filter.blockSignals(False)

    def _show_group_menu(self) -> None:
        menu = QMenu(self)
        create_action = menu.addAction("新建分组")
        rename_menu = menu.addMenu("重命名分组")
        delete_menu = menu.addMenu("删除分组")
        for group in self.repository.list_groups():
            rename_action = rename_menu.addAction(group.name)
            delete_action = delete_menu.addAction(group.name)
            is_default = group.name == "默认分组"
            rename_action.setEnabled(not is_default)
            delete_action.setEnabled(not is_default)
            rename_action.triggered.connect(
                lambda checked=False, group_id=group.id, name=group.name: self._rename_group(group_id, name)
            )
            delete_action.triggered.connect(
                lambda checked=False, group_id=group.id, name=group.name: self._delete_group(group_id, name)
            )
        selected = menu.exec(self.group_button.mapToGlobal(self.group_button.rect().bottomLeft()))
        if selected == create_action:
            self._create_group()

    def _create_group(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建分组", "分组名称")
        if not accepted:
            return
        try:
            group = self.repository.create_group(name)
        except Exception as exc:
            QMessageBox.warning(self, "无法新建分组", str(exc))
            return
        self._reload_group_controls()
        index = self.add_group_combo.findData(group.id)
        self.add_group_combo.setCurrentIndex(index)

    def _rename_group(self, group_id: int, old_name: str) -> None:
        name, accepted = QInputDialog.getText(self, "重命名分组", "新名称", text=old_name)
        if not accepted:
            return
        try:
            self.repository.rename_group(group_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "无法重命名", str(exc))
            return
        self._reload_group_controls()
        self._render_table()

    def _delete_group(self, group_id: int, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "删除分组",
            f"删除“{name}”？其中证券会移到默认分组。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.repository.delete_group(group_id)
        self._reload_group_controls()
        self._render_table()

    @staticmethod
    def _market_status_text() -> str:
        now = beijing_now()
        weekday_open = now.weekday() < 5
        morning = now.replace(hour=9, minute=30, second=0) <= now <= now.replace(hour=11, minute=30, second=0)
        afternoon = now.replace(hour=13, minute=0, second=0) <= now <= now.replace(hour=15, minute=0, second=0)
        status = "交易中" if weekday_open and (morning or afternoon) else "已休市"
        return f"● {status}  北京时间 {now:%m-%d %H:%M}"
