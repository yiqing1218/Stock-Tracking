from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QDate, QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .indicators import INDICATOR_CATALOG, IndicatorDefinition, build_screening_catalog
from .models import Security
from .historical_store import HistoricalStore
from .repository import Repository
from .scanner_service import ScannerRepository
from .screening import ScreeningCondition, screen_all_stocks
from .ui_common import Worker, configure_table, section_title
from .time_utils import beijing_today


class ConditionRow(QFrame):
    def __init__(self, catalog: list[IndicatorDefinition], first: bool = False) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        self.connector = QComboBox()
        self.connector.addItems(["且", "或", "非"])
        if first:
            self.connector.setItemText(0, "首项")
            self.connector.model().item(1).setEnabled(False)
        self.dimension = QComboBox()
        self.dimension.addItems(["趋势", "动量", "波动", "量能", "情绪", "风险"])
        self.indicator = QComboBox()
        self.operator = QComboBox()
        self.operator.addItems([">", ">=", "<", "<=", "=", "!="])
        self.threshold = QLineEdit("0")
        self.threshold.setFixedWidth(100)
        self._catalog = catalog
        self.dimension.currentTextChanged.connect(self._reload_indicators)
        for widget in (self.connector, self.dimension, self.indicator, self.operator):
            layout.addWidget(widget)
        layout.addWidget(self.threshold)
        layout.addStretch(1)
        self._reload_indicators()

    def set_catalog(self, catalog: list[IndicatorDefinition]) -> None:
        current = self.indicator.currentData()
        current_column = (
            current.column if isinstance(current, IndicatorDefinition) else ""
        )
        self._catalog = catalog
        self._reload_indicators(current_column)

    def _reload_indicators(self, preferred: str = "") -> None:
        self.indicator.clear()
        for definition in self._catalog:
            if definition.category == self.dimension.currentText():
                self.indicator.addItem(definition.name, definition)
                if definition.column == preferred:
                    self.indicator.setCurrentIndex(self.indicator.count() - 1)

    def value(self) -> ScreeningCondition:
        definition = self.indicator.currentData()
        if not isinstance(definition, IndicatorDefinition):
            raise ValueError("请选择指标")
        return ScreeningCondition(
            self.connector.currentText(),
            definition,
            self.operator.currentText(),
            float(self.threshold.text().strip()),
        )


class ScreeningPage(QWidget):
    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        thread_pool: QThreadPool,
        store: HistoricalStore | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.provider = provider
        self.thread_pool = thread_pool
        self.store = store or HistoricalStore(repository.database_path)
        self.scanner_repository = ScannerRepository(self.store)
        self.catalog = list(INDICATOR_CATALOG)
        self.rows: list[ConditionRow] = []
        self.results = pd.DataFrame()
        self._catalog_loading = False
        self._running = False
        self._workers: set[Worker] = set()
        self._last_conditions: list[ScreeningCondition] = []
        self._current_run_id: int | None = None
        self._build_ui()
        self._add_condition()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(section_title("条件荐股", "全部A股 · 最多5项 · 且/或/非组合"))
        toolbar = QHBoxLayout()
        self.date_mode = QComboBox()
        self.date_mode.addItem("最新完整交易日", "latest_completed_day")
        self.date_mode.addItem("历史指定日期", "fixed_date")
        today = beijing_today()
        self.scan_date = QDateEdit(QDate(today.year, today.month, today.day))
        self.scan_date.setCalendarPopup(True)
        self.scan_date.setDisplayFormat("yyyy-MM-dd")
        self.scan_date.setMaximumDate(QDate(today.year, today.month, today.day))
        self.scan_date.setEnabled(False)
        self.date_mode.currentIndexChanged.connect(
            lambda: self.scan_date.setEnabled(
                self.date_mode.currentData() == "fixed_date"
            )
        )
        self.scan_adjustment = QComboBox()
        self.scan_adjustment.addItem("前复权", "qfq")
        self.scan_adjustment.addItem("不复权", "")
        self.scan_adjustment.addItem("后复权", "hfq")
        self.add_button = QPushButton("＋ 增加条件")
        self.add_button.clicked.connect(self._add_condition)
        self.remove_button = QPushButton("－ 删除末项")
        self.remove_button.clicked.connect(self._remove_condition)
        self.run_button = QPushButton("检索全部股票")
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(self._run)
        self.group_button = QPushButton("结果保存为自选分组")
        self.group_button.clicked.connect(self._save_group)
        self.save_definition_button = QPushButton("保存筛选模板")
        self.save_definition_button.clicked.connect(self._save_definition)
        self.export_button = QPushButton("导出结果")
        self.export_button.clicked.connect(self._export_results)
        self.dynamic_button = QPushButton("保存为动态分组")
        self.dynamic_button.clicked.connect(self._save_dynamic_group)
        toolbar.addWidget(QLabel("扫描日"))
        toolbar.addWidget(self.date_mode)
        toolbar.addWidget(self.scan_date)
        toolbar.addWidget(QLabel("复权"))
        toolbar.addWidget(self.scan_adjustment)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.remove_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.save_definition_button)
        toolbar.addWidget(self.export_button)
        toolbar.addWidget(self.dynamic_button)
        toolbar.addWidget(self.group_button)
        toolbar.addWidget(self.run_button)
        layout.addLayout(toolbar)
        self.condition_box = QVBoxLayout()
        layout.addLayout(self.condition_box)
        self.status = QLabel("指标库准备中；扫描只读取本地历史仓库，不逐股联网。")
        self.status.setObjectName("Muted")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 2)
        configure_table(self.table)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

    def start(self) -> None:
        if self._catalog_loading or len(self.catalog) >= 480:
            return
        self._catalog_loading = True
        worker = Worker(build_screening_catalog)
        self._workers.add(worker)
        worker.signals.result.connect(self._catalog_ready)
        worker.signals.error.connect(
            lambda message: self.status.setText(f"扩展指标库加载失败：{message}")
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        worker.signals.finished.connect(
            lambda: setattr(self, "_catalog_loading", False)
        )
        self.thread_pool.start(worker)

    def _catalog_ready(self, value: object) -> None:
        if isinstance(value, list):
            self.catalog = value
            for row in self.rows:
                row.set_catalog(self.catalog)
            self.status.setText(
                f"已载入 {len(self.catalog)} 个指标；使用维度→指标二级列表选择。"
            )

    def _add_condition(self) -> None:
        if len(self.rows) >= 5:
            QMessageBox.information(self, "条件上限", "最多设置5个条件。")
            return
        row = ConditionRow(self.catalog, first=not self.rows)
        self.rows.append(row)
        self.condition_box.addWidget(row)

    def _remove_condition(self) -> None:
        if len(self.rows) <= 1:
            return
        self.rows.pop().deleteLater()

    def _run(self) -> None:
        if self._running:
            return
        try:
            conditions = [row.value() for row in self.rows]
        except ValueError as exc:
            QMessageBox.warning(self, "条件错误", str(exc))
            return
        self._running = True
        self._last_conditions = conditions
        self.run_button.setEnabled(False)
        summary = self.store.summary()
        if summary.bars <= 0:
            QMessageBox.information(
                self,
                "本地数据为空",
                "请先在“数据导出→数据同步”同步A股历史日线，或导入旧版CSV缓存。",
            )
            self._running = False
            self.run_button.setEnabled(True)
            return
        self.status.setText(
            f"正在扫描本地仓库 {summary.securities:,} 只证券；按股读取、用完即释放……"
        )
        target_date = (
            self.scan_date.date().toString("yyyy-MM-dd")
            if self.date_mode.currentData() == "fixed_date"
            else ""
        )
        self._current_run_id = self.scanner_repository.create_run(
            "临时条件扫描", None, target_date, summary.securities
        )
        worker = Worker(
            screen_all_stocks,
            self.provider,
            conditions,
            None,
            self.store,
            target_date,
            str(self.scan_adjustment.currentData()),
        )
        self._workers.add(worker)
        worker.signals.result.connect(self._show_results)
        worker.signals.error.connect(
            lambda message: self.status.setText(f"检索失败：{message}")
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        worker.signals.finished.connect(self._finish)
        self.thread_pool.start(worker)

    def _finish(self) -> None:
        self._running = False
        self.run_button.setEnabled(True)

    def _show_results(self, value: object) -> None:
        if not isinstance(value, pd.DataFrame):
            return
        self.results = value
        if self._current_run_id is not None:
            self.scanner_repository.finish_run(
                self._current_run_id, value, self.store.summary().securities
            )
        columns = [name for name in value.columns if name not in {"security", "市场"}]
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(value))
        for row_index, (_, row) in enumerate(value.iterrows()):
            for column_index, name in enumerate(columns):
                cell = row.get(name, "")
                self.table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem("—" if pd.isna(cell) else str(cell)),
                )
        self.status.setText(f"检索完成：全部A股中命中 {len(value)} 只。")

    def _save_dynamic_group(self) -> None:
        if self.results.empty or not self._last_conditions:
            QMessageBox.information(self, "没有结果", "请先完成检索。")
            return
        name, accepted = QInputDialog.getText(self, "保存动态分组", "动态分组名称")
        if not accepted or not name.strip():
            return
        try:
            definition_id = self.scanner_repository.save_definition(
                f"动态:{name.strip()}", self._last_conditions, "由条件荐股结果创建"
            )
            run_id = self.scanner_repository.create_run(
                name.strip(), definition_id, "", self.store.summary().securities
            )
            self.scanner_repository.finish_run(
                run_id, self.results, self.store.summary().securities
            )
            group_id = self.scanner_repository.save_dynamic_group(
                name.strip(), definition_id, run_id
            )
            QMessageBox.information(
                self,
                "已保存",
                f"动态分组“{name.strip()}”已保存（编号 {group_id}），可按同一筛选定义刷新。",
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _save_definition(self) -> None:
        try:
            conditions = [row.value() for row in self.rows]
        except ValueError as exc:
            QMessageBox.warning(self, "条件错误", str(exc))
            return
        name, accepted = QInputDialog.getText(self, "保存筛选模板", "模板名称")
        if not accepted or not name.strip():
            return
        try:
            fixed_date = (
                self.scan_date.date().toString("yyyy-MM-dd")
                if self.date_mode.currentData() == "fixed_date"
                else ""
            )
            definition_id = self.scanner_repository.save_definition(
                name,
                conditions,
                date_mode=str(self.date_mode.currentData()),
                fixed_date=fixed_date,
                adjustment=str(self.scan_adjustment.currentData()),
            )
            QMessageBox.information(
                self, "已保存", f"模板“{name.strip()}”已保存，编号 {definition_id}。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _export_results(self) -> None:
        if self.results.empty:
            QMessageBox.information(self, "没有结果", "请先完成检索。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出筛选结果", "条件荐股结果.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        frame = self.results.drop(columns=["security"], errors="ignore")
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        self.status.setText(f"已导出 {len(frame)} 行：{path}")

    def _security_at(self, row: int) -> Security | None:
        if row < 0 or row >= len(self.results):
            return None
        value = self.results.iloc[row].get("security")
        return value if isinstance(value, Security) else None

    def _context_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        security = self._security_at(self.table.rowAt(position.y()))
        if security is None:
            return
        menu = QMenu(self)
        for group in self.repository.list_groups():
            action = menu.addAction(f"添加至 {group.name}")
            action.triggered.connect(
                lambda checked=False, item=security, group_id=group.id: (
                    self.repository.add_security(item, group_id)
                )
            )
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _save_group(self) -> None:
        if self.results.empty:
            QMessageBox.information(self, "没有结果", "请先完成检索。")
            return
        name, accepted = QInputDialog.getText(self, "保存自选分组", "分组名称")
        if not accepted or not name.strip():
            return
        try:
            group = self.repository.create_group(name)
            for security in self.results.get("security", []):
                if isinstance(security, Security):
                    self.repository.add_security(security, group.id)
            QMessageBox.information(
                self, "已保存", f"已将 {len(self.results)} 只股票加入“{group.name}”。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
