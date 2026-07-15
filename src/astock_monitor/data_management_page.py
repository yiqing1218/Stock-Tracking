from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDate, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .data_quality import validate_warehouse
from .historical_store import HistoricalStore
from .models import SecurityType
from .repository import Repository
from .sync_service import SyncProgress, SyncService
from .time_utils import beijing_today
from .ui_common import MetricCard, Worker, configure_table, format_number, section_title


class DataManagementPage(QWidget):
    """Phase-one warehouse management and data export without blocking the UI."""

    warehouse_changed = Signal()
    sync_progress = Signal(object)

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
        self.sync_service = SyncService(store, provider)
        self._worker: Worker | None = None
        self._export_worker: Worker | None = None
        self._job_id: int | None = None
        self.sync_progress.connect(self._progress)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.addWidget(
            section_title("数据导出", "本地历史仓库 · 增量同步 · 质量检查 · CSV导出")
        )
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._overview(), "仓库概况")
        self.tabs.addTab(self._sync(), "数据同步")
        self.tabs.addTab(self._export(), "数据导出")
        self.tabs.addTab(self._quality(), "质量与任务")

    def _overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        cards = QGridLayout()
        self.security_card = MetricCard("证券数量")
        self.bar_card = MetricCard("日线记录")
        self.range_card = MetricCard("数据区间")
        self.size_card = MetricCard("数据库大小")
        for index, card in enumerate(
            (self.security_card, self.bar_card, self.range_card, self.size_card)
        ):
            cards.addWidget(card, index // 2, index % 2)
        layout.addLayout(cards)
        note = QLabel(
            "本地仓库与原自选、指标、消息表共用 SQLite 文件，但使用独立表和增量迁移；网络失败不会删除已有数据。"
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新概况")
        refresh.clicked.connect(self.refresh)
        import_button = QPushButton("导入旧版CSV缓存")
        import_button.clicked.connect(self._import_cache)
        actions.addWidget(refresh)
        actions.addWidget(import_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    def _sync(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        panel = QFrame()
        panel.setObjectName("Section")
        form = QHBoxLayout(panel)
        self.scope = QComboBox()
        for label, value in (
            ("全部A股", "stocks"),
            ("全部ETF", "etfs"),
            ("主要指数", "indices"),
            ("全部证券", "all"),
            ("当前自选", "watchlist"),
        ):
            self.scope.addItem(label, value)
        self.adjustment = QComboBox()
        self.adjustment.addItem("前复权", "qfq")
        self.adjustment.addItem("不复权", "")
        self.adjustment.addItem("后复权", "hfq")
        self.mode = QComboBox()
        self.mode.addItem("增量同步", "incremental")
        self.mode.addItem("完整补齐", "full")
        self.mode.addItem("修复重拉", "repair")
        self.sync_button = QPushButton("开始同步")
        self.sync_button.setObjectName("Primary")
        self.sync_button.clicked.connect(self._start_sync)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_sync)
        for label, widget in (
            ("范围", self.scope),
            ("复权", self.adjustment),
            ("模式", self.mode),
        ):
            form.addWidget(QLabel(label))
            form.addWidget(widget)
        form.addStretch(1)
        form.addWidget(self.cancel_button)
        form.addWidget(self.sync_button)
        layout.addWidget(panel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.sync_status = QLabel("建议先同步当前自选验证，再按磁盘空间同步全市场。")
        self.sync_status.setObjectName("Muted")
        layout.addWidget(self.sync_status)
        layout.addStretch(1)
        return page

    def _export(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        scope_panel = QFrame()
        scope_panel.setObjectName("Section")
        scope_layout = QGridLayout(scope_panel)
        scope_title = QLabel("1　选择导出数据")
        scope_title.setObjectName("AppName")
        self.export_scope = QComboBox()
        self.export_scope.addItem("全部本地证券", "all")
        self.export_scope.addItem("全部本地A股", "stocks")
        self.export_scope.addItem("全部本地ETF", "etfs")
        self.export_scope.addItem("当前自选", "watchlist")
        self.export_adjustment = QComboBox()
        self.export_adjustment.addItem("前复权", "qfq")
        self.export_adjustment.addItem("不复权", "")
        self.export_adjustment.addItem("后复权", "hfq")
        self.export_format = QComboBox()
        self.export_format.addItem("CSV 长表（UTF-8 BOM，Excel可直接打开）", "csv")
        scope_layout.addWidget(scope_title, 0, 0, 1, 6)
        scope_layout.addWidget(QLabel("证券范围"), 1, 0)
        scope_layout.addWidget(self.export_scope, 1, 1)
        scope_layout.addWidget(QLabel("价格口径"), 1, 2)
        scope_layout.addWidget(self.export_adjustment, 1, 3)
        scope_layout.addWidget(QLabel("文件格式"), 1, 4)
        scope_layout.addWidget(self.export_format, 1, 5)
        layout.addWidget(scope_panel)

        date_panel = QFrame()
        date_panel.setObjectName("Section")
        date_layout = QGridLayout(date_panel)
        date_title = QLabel("2　设置交易日期")
        date_title.setObjectName("AppName")
        today = beijing_today()
        qtoday = QDate(today.year, today.month, today.day)
        self.export_start = QDateEdit(QDate(1990, 1, 1))
        self.export_start.setCalendarPopup(True)
        self.export_start.setDisplayFormat("yyyy-MM-dd")
        self.export_end = QDateEdit(qtoday)
        self.export_end.setCalendarPopup(True)
        self.export_end.setDisplayFormat("yyyy-MM-dd")
        self.export_end.setMaximumDate(qtoday)
        date_layout.addWidget(date_title, 0, 0, 1, 8)
        date_layout.addWidget(QLabel("开始"), 1, 0)
        date_layout.addWidget(self.export_start, 1, 1)
        date_layout.addWidget(QLabel("结束"), 1, 2)
        date_layout.addWidget(self.export_end, 1, 3)
        for column, (label, years) in enumerate(
            (("近1年", 1), ("近3年", 3), ("近5年", 5), ("全部历史", 0)), start=4
        ):
            preset = QPushButton(label)
            preset.clicked.connect(
                lambda checked=False, value=years: self._set_export_range(value)
            )
            date_layout.addWidget(preset, 1, column)
        layout.addWidget(date_panel)

        output_panel = QFrame()
        output_panel.setObjectName("Section")
        output_layout = QGridLayout(output_panel)
        output_title = QLabel("3　选择文件并导出")
        output_title.setObjectName("AppName")
        self.export_path = QLineEdit(str(Path.home() / "A股历史数据.csv"))
        self.export_path.setReadOnly(True)
        browse = QPushButton("选择保存位置…")
        browse.clicked.connect(self._choose_export_path)
        self.export_button = QPushButton("开始流式导出")
        self.export_button.setObjectName("Primary")
        self.export_button.clicked.connect(self._export_csv)
        output_layout.addWidget(output_title, 0, 0, 1, 4)
        output_layout.addWidget(self.export_path, 1, 0, 1, 2)
        output_layout.addWidget(browse, 1, 2)
        output_layout.addWidget(self.export_button, 1, 3)
        layout.addWidget(output_panel)
        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 0)
        self.export_progress.hide()
        layout.addWidget(self.export_progress)
        self.export_status = QLabel(
            "采用证券逐批读取与写入，不构建全市场内存宽表；输出包含代码、名称、类型、市场、交易日、复权口径和全部日线字段。"
        )
        self.export_status.setObjectName("Muted")
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        tip = QLabel(
            "建议：先在“数据同步”确认所需复权口径和日期已入库，再导出；导出过程只读数据库，不会修改或删除现有数据。"
        )
        tip.setObjectName("Tiny")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch(1)
        return page

    def _set_export_range(self, years: int) -> None:
        end = self.export_end.date()
        self.export_start.setDate(QDate(1990, 1, 1) if years == 0 else end.addYears(-years))

    def _choose_export_path(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出本地历史数据",
            self.export_path.text() or str(Path.home() / "A股历史数据.csv"),
            "CSV 文件 (*.csv)",
        )
        if not path:
            return False
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self.export_path.setText(path)
        return True

    def _quality(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        check = QPushButton("运行质量检查")
        check.clicked.connect(self._validate)
        row.addWidget(check)
        row.addStretch(1)
        layout.addLayout(row)
        self.jobs_table = QTableWidget(0, 8)
        self.jobs_table.setHorizontalHeaderLabels(
            ["任务", "范围", "模式", "状态", "进度", "失败", "开始", "结束"]
        )
        configure_table(self.jobs_table)
        self.jobs_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.jobs_table)
        return page

    def refresh(self) -> None:
        summary = self.store.summary()
        self.security_card.set_value(f"{summary.securities:,}", "已登记")
        self.bar_card.set_value(f"{summary.bars:,}", "本地日K")
        self.range_card.set_value(
            f"{summary.first_date or '—'}\n{summary.last_date or '—'}", "最早 / 最新"
        )
        self.size_card.set_value(
            format_number(summary.database_bytes), f"未解决问题 {summary.issues}"
        )
        jobs = self.store.list_sync_jobs()
        self.jobs_table.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            total = int(job["total_count"] or 0)
            done = int(job["completed_count"] or 0)
            values = [
                job["id"],
                job["scope"],
                job["mode"],
                job["status"],
                f"{done}/{total}",
                job["failed_count"],
                job["started_at"],
                job["finished_at"],
            ]
            for c, value in enumerate(values):
                self.jobs_table.setItem(r, c, QTableWidgetItem(str(value)))

    def _start_sync(self) -> None:
        if self._worker is not None:
            return
        scope = str(self.scope.currentData())
        securities = self.repository.list_watchlist() if scope == "watchlist" else None
        self.sync_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self._worker = Worker(
            self.sync_service.sync,
            scope,
            securities,
            str(self.adjustment.currentData()),
            str(self.mode.currentData()),
            16000,
            self.sync_progress.emit,
        )
        self._worker.signals.result.connect(self._sync_finished)
        self._worker.signals.error.connect(
            lambda message: self.sync_status.setText(f"同步失败：{message}")
        )
        self._worker.signals.finished.connect(self._finish_worker)
        self.thread_pool.start(self._worker)

    def _progress(self, value: SyncProgress) -> None:
        self._job_id = value.job_id
        percent = int(value.completed / max(value.total, 1) * 100)
        self.progress.setValue(percent)
        self.sync_status.setText(
            f"{value.current} · {value.completed}/{value.total} · 失败 {value.failed}"
        )

    def _sync_finished(self, value: object) -> None:
        if isinstance(value, SyncProgress):
            self.sync_status.setText(
                f"同步完成：处理 {value.completed}，失败 {value.failed}。"
            )
        self.refresh()
        self.warehouse_changed.emit()

    def _finish_worker(self) -> None:
        self._worker = None
        self.sync_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _cancel_sync(self) -> None:
        self.sync_service.cancel(self._job_id)
        self.sync_status.setText("已请求取消；当前单只证券完成后停止。")

    def _import_cache(self) -> None:
        files, bars = self.store.import_cache_directory(self.provider.cache_dir)
        self.refresh()
        self.warehouse_changed.emit()
        QMessageBox.information(
            self, "导入完成", f"导入 {files} 个缓存文件、{bars:,} 条日线记录。"
        )

    def _export_csv(self) -> None:
        if self._export_worker is not None:
            return
        path = self.export_path.text().strip()
        if not path:
            if not self._choose_export_path():
                return
            path = self.export_path.text().strip()
        scope = str(self.export_scope.currentData())
        if scope == "watchlist":
            securities = self.repository.list_watchlist()
        elif scope == "stocks":
            securities = [
                item
                for item in self.store.list_securities()
                if item.security_type is SecurityType.STOCK
            ]
        elif scope == "etfs":
            securities = [
                item
                for item in self.store.list_securities()
                if item.security_type is SecurityType.ETF
            ]
        else:
            securities = None
        self.export_button.setEnabled(False)
        self.export_progress.show()
        self.export_status.setText("正在按证券流式导出，请勿关闭程序……")
        self._export_worker = Worker(
            self.store.export_csv,
            Path(path),
            securities,
            str(self.export_adjustment.currentData()),
            self.export_start.date().toString("yyyy-MM-dd"),
            self.export_end.date().toString("yyyy-MM-dd"),
        )
        self._export_worker.signals.result.connect(
            lambda count: self.export_status.setText(f"导出完成：{count:,} 行 · {path}")
        )
        self._export_worker.signals.error.connect(
            lambda message: self.export_status.setText(f"导出失败：{message}")
        )
        self._export_worker.signals.finished.connect(self._finish_export)
        self.thread_pool.start(self._export_worker)

    def _finish_export(self) -> None:
        self._export_worker = None
        self.export_button.setEnabled(True)
        self.export_progress.hide()

    def _validate(self) -> None:
        worker = Worker(validate_warehouse, self.store)
        worker.signals.result.connect(
            lambda counts: (
                self.refresh(),
                QMessageBox.information(
                    self,
                    "质量检查完成",
                    "\n".join(f"{key}: {value}" for key, value in counts.items()),
                ),
            )
        )
        worker.signals.error.connect(
            lambda message: QMessageBox.warning(self, "检查失败", message)
        )
        self.thread_pool.start(worker)
