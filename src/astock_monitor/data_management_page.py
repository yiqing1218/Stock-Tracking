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
    """Explain and manage durable data, downloads, storage and exports."""

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
            section_title(
                "数据中心",
                "本地数据库概况 · 历史数据下载 · 安全存储管理 · 从数据库导出",
            )
        )
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._overview(), "本地数据概况")
        self.tabs.addTab(self._sync(), "历史数据下载")
        self.tabs.addTab(self._quality(), "存储管理")
        self.tabs.addTab(self._export(), "数据导出")

    def _overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        cards = QGridLayout()
        self.security_card = MetricCard("证券数量")
        self.bar_card = MetricCard("日线记录")
        self.intraday_card = MetricCard("一分钟分时")
        self.score_card = MetricCard("每日评分")
        self.fund_card = MetricCard("资金记录")
        self.chip_card = MetricCard("筹码记录")
        self.dataset_card = MetricCard("F10/财务数据集")
        self.breadth_card = MetricCard("市场宽度日记录")
        self.backtest_card = MetricCard("回测运行")
        self.backtest_trade_card = MetricCard("回测交易记录")
        self.range_card = MetricCard("日线数据区间")
        self.size_card = MetricCard("数据库大小")
        for index, card in enumerate(
            (
                self.security_card,
                self.bar_card,
                self.intraday_card,
                self.score_card,
                self.fund_card,
                self.chip_card,
                self.dataset_card,
                self.breadth_card,
                self.backtest_card,
                self.backtest_trade_card,
                self.range_card,
                self.size_card,
            )
        ):
            cards.addWidget(card, index // 4, index % 4)
        layout.addLayout(cards)
        note = QLabel(
            "长期保存：完成交易日K线、历史1分钟分时、每日评分、资金、公开筹码、F10/财务及市场宽度。"
            "只临时展示：大盘实时指数、板块榜、涨跌榜、当日未收盘K线、当日分时和涨跌原因。"
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新概况")
        refresh.clicked.connect(self.refresh)
        import_button = QPushButton("迁移旧版日线CSV到数据库")
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
        self.mode.addItem("只下载缺少和新增日期", "incremental")
        self.mode.addItem("上市至今完整下载", "full")
        self.mode.addItem("重新下载异常证券", "repair")
        self.sync_button = QPushButton("开始下载到本地数据库")
        self.sync_button.setObjectName("Primary")
        self.sync_button.clicked.connect(self._start_sync)
        self.full_history_button = QPushButton("准备条件荐股：全部A股上市至今")
        self.full_history_button.clicked.connect(self._start_full_stock_history)
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
        form.addWidget(self.full_history_button)
        form.addWidget(self.cancel_button)
        form.addWidget(self.sync_button)
        layout.addWidget(panel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.sync_status = QLabel(
            "条件荐股只读取本地前复权日线。要保证随时可用，请下载“全部A股 + 前复权”；"
            "再次执行增量下载只补缺失日期，不重复下载已有历史。"
        )
        self.sync_status.setObjectName("Muted")
        self.sync_status.setWordWrap(True)
        layout.addWidget(self.sync_status)
        self.startup_prefetch_status = QLabel("启动近5日分时下载尚未执行。")
        self.startup_prefetch_status.setObjectName("Tiny")
        self.startup_prefetch_status.setWordWrap(True)
        layout.addWidget(self.startup_prefetch_status)
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
        self.export_content = QComboBox()
        for label, value in (
            ("日K线", "daily_bars"),
            ("历史1分钟分时", "intraday_bars"),
            ("每日综合评分", "daily_scores"),
            ("资金信息", "fund_flow"),
            ("公开筹码信息", "chips"),
            ("市场宽度日记录", "market_breadth"),
            ("回测运行摘要", "backtest_runs"),
            ("回测全部交易", "backtest_trades"),
            ("回测净值曲线", "backtest_equity"),
            ("回测持仓快照", "backtest_positions"),
            ("回测评价指标", "backtest_metrics"),
        ):
            self.export_content.addItem(label, value)
        self.export_adjustment = QComboBox()
        self.export_adjustment.addItem("前复权", "qfq")
        self.export_adjustment.addItem("不复权", "")
        self.export_adjustment.addItem("后复权", "hfq")
        self.export_format = QComboBox()
        self.export_format.addItem("CSV 长表（UTF-8 BOM，Excel可直接打开）", "csv")
        scope_layout.addWidget(scope_title, 0, 0, 1, 8)
        scope_layout.addWidget(QLabel("导出内容"), 1, 0)
        scope_layout.addWidget(self.export_content, 1, 1)
        scope_layout.addWidget(QLabel("证券范围"), 1, 2)
        scope_layout.addWidget(self.export_scope, 1, 3)
        scope_layout.addWidget(QLabel("价格口径（日K）"), 1, 4)
        scope_layout.addWidget(self.export_adjustment, 1, 5)
        scope_layout.addWidget(QLabel("文件格式"), 1, 6)
        scope_layout.addWidget(self.export_format, 1, 7)
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
            "导出只读取 SQLite 本地数据库，不读取或打包桌面应用缓存；"
            "采用分批流式写入，不把全市场数据一次性放进内存。"
        )
        self.export_status.setObjectName("Muted")
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        tip = QLabel(
            "建议：先在“历史数据下载”确认所需数据已经入库。导出过程只读，不会修改数据库。"
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
        protected = QLabel(
            "受保护、不可在本页删除：自选股与分组、自定义指标、提醒规则与消息、公司事件、证券目录、"
            "条件荐股定义，以及支撑条件荐股的日K主仓库。下方只能清理可重新下载的数据。"
        )
        protected.setObjectName("Muted")
        protected.setWordWrap(True)
        layout.addWidget(protected)
        cleanup_panel = QFrame()
        cleanup_panel.setObjectName("Section")
        cleanup = QHBoxLayout(cleanup_panel)
        self.cleanup_category = QComboBox()
        for label, value in (
            ("历史1分钟分时", "intraday"),
            ("每日评分", "scores"),
            ("资金记录", "fund_flow"),
            ("筹码记录", "chips"),
            ("市场宽度日记录", "market_breadth"),
        ):
            self.cleanup_category.addItem(label, value)
        today = beijing_today()
        self.cleanup_before = QDateEdit(
            QDate(today.year, today.month, today.day).addYears(-1)
        )
        self.cleanup_before.setCalendarPopup(True)
        self.cleanup_before.setDisplayFormat("yyyy-MM-dd")
        clear_data = QPushButton("删除所选日期之前的数据")
        clear_data.clicked.connect(self._clear_selected_data)
        clear_cache = QPushButton("清理临时文件缓存")
        clear_cache.clicked.connect(self._clear_transient_cache)
        clear_tasks = QPushButton("清理已完成任务记录")
        clear_tasks.clicked.connect(self._clear_task_history)
        optimize = QPushButton("整理并压缩数据库")
        optimize.clicked.connect(self._optimize_database)
        cleanup.addWidget(QLabel("可重建数据"))
        cleanup.addWidget(self.cleanup_category)
        cleanup.addWidget(QLabel("早于"))
        cleanup.addWidget(self.cleanup_before)
        cleanup.addWidget(clear_data)
        cleanup.addWidget(clear_cache)
        cleanup.addWidget(clear_tasks)
        cleanup.addWidget(optimize)
        layout.addWidget(cleanup_panel)
        backtest_panel = QFrame()
        backtest_panel.setObjectName("Section")
        backtest_layout = QHBoxLayout(backtest_panel)
        self.backtest_run_combo = QComboBox()
        self.backtest_run_combo.setMinimumWidth(360)
        delete_backtest = QPushButton("删除所选回测及其明细")
        delete_backtest.setObjectName("Danger")
        delete_backtest.clicked.connect(self._delete_backtest_run)
        backtest_layout.addWidget(QLabel("回测数据"))
        backtest_layout.addWidget(self.backtest_run_combo, 1)
        backtest_layout.addWidget(delete_backtest)
        layout.addWidget(backtest_panel)
        row = QHBoxLayout()
        check = QPushButton("检查本地日线质量")
        check.clicked.connect(self._validate)
        row.addWidget(check)
        row.addStretch(1)
        layout.addLayout(row)
        self.storage_status = QLabel(
            "删除操作不会触及受保护表；临时缓存和本地数据库是两套独立存储。"
        )
        self.storage_status.setObjectName("Tiny")
        self.storage_status.setWordWrap(True)
        layout.addWidget(self.storage_status)
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
        inventory = self.store.inventory()
        self.security_card.set_value(f"{summary.securities:,}", "已登记")
        self.bar_card.set_value(f"{summary.bars:,}", "本地日K")
        self.intraday_card.set_value(
            f"{int(inventory['intraday_bars']):,}",
            f"{int(inventory['intraday_securities']):,} 只证券",
        )
        self.score_card.set_value(f"{int(inventory['daily_scores']):,}", "证券交易日")
        self.fund_card.set_value(f"{int(inventory['fund_flow']):,}", "公开值与标明的估算值")
        self.chip_card.set_value(f"{int(inventory['chips']):,}", "不编造缺失筹码")
        self.dataset_card.set_value(f"{int(inventory['datasets']):,}", "本地数据集")
        self.breadth_card.set_value(
            f"{int(inventory['market_breadth']):,}", "每天覆盖更新一条"
        )
        self.backtest_card.set_value(
            f"{int(inventory['backtest_runs']):,}",
            f"净值点 {int(inventory['backtest_equity']):,}",
        )
        self.backtest_trade_card.set_value(
            f"{int(inventory['backtest_trades']):,}",
            f"持仓快照 {int(inventory['backtest_positions']):,}",
        )
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
        current_run = self.backtest_run_combo.currentData()
        self.backtest_run_combo.clear()
        for run in self.store.list_backtest_runs():
            label = (
                f"#{run['id']} {run['name']} · {run['status']} · "
                f"交易 {run['trade_count']} · {run['started_at']}"
            )
            self.backtest_run_combo.addItem(label, int(run["id"]))
        if current_run is not None:
            index = self.backtest_run_combo.findData(current_run)
            if index >= 0:
                self.backtest_run_combo.setCurrentIndex(index)

    def _start_sync(self) -> None:
        if self._worker is not None:
            return
        scope = str(self.scope.currentData())
        securities = self.repository.list_watchlist() if scope == "watchlist" else None
        self.sync_button.setEnabled(False)
        self.full_history_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self._worker = Worker(
            self.sync_service.sync,
            scope,
            securities,
            str(self.adjustment.currentData()),
            str(self.mode.currentData()),
            20000,
            self.sync_progress.emit,
        )
        self._worker.signals.result.connect(self._sync_finished)
        self._worker.signals.error.connect(
            lambda message: self.sync_status.setText(f"同步失败：{message}")
        )
        self._worker.signals.finished.connect(self._finish_worker)
        self.thread_pool.start(self._worker)

    def _start_full_stock_history(self) -> None:
        self.scope.setCurrentIndex(max(0, self.scope.findData("stocks")))
        self.adjustment.setCurrentIndex(max(0, self.adjustment.findData("qfq")))
        self.mode.setCurrentIndex(max(0, self.mode.findData("full")))
        self._start_sync()

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
        self.full_history_button.setEnabled(True)
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
            self.store.export_dataset_csv,
            str(self.export_content.currentData()),
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

    def set_startup_prefetch_status(self, message: str) -> None:
        self.startup_prefetch_status.setText(message)
        self.refresh()

    def _clear_selected_data(self) -> None:
        category = str(self.cleanup_category.currentData())
        before = self.cleanup_before.date().toString("yyyy-MM-dd")
        answer = QMessageBox.question(
            self,
            "确认删除可重建数据",
            f"确认删除“{self.cleanup_category.currentText()}”中早于 {before} 的记录？\n"
            "这些数据以后需要时必须重新下载。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.store.clear_rebuildable_data(category, before)
            self.storage_status.setText(f"已删除 {count:,} 条可重建记录。")
            self.refresh()
            self.warehouse_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))

    def _clear_transient_cache(self) -> None:
        patterns = (
            "intraday_*.csv",
            "market_*.csv",
            "market_*.source.txt",
            "alert_market_snapshot_*.csv",
            "alert_market_snapshot_*.source.txt",
            "news_v3_*.json",
        )
        removed = 0
        for pattern in patterns:
            for path in self.provider.cache_dir.glob(pattern):
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        self.storage_status.setText(
            f"已清理 {removed} 个临时缓存文件；本地数据库未改变。"
        )

    def _clear_task_history(self) -> None:
        count = self.store.clear_task_history()
        self.storage_status.setText(f"已清理 {count} 条下载任务记录。")
        self.refresh()

    def _optimize_database(self) -> None:
        self.storage_status.setText("正在整理数据库空闲页并更新查询统计…")
        worker = Worker(self.store.optimize)
        worker.signals.result.connect(
            lambda _value: (
                self.storage_status.setText("数据库整理完成。"),
                self.refresh(),
            )
        )
        worker.signals.error.connect(
            lambda message: self.storage_status.setText(f"数据库整理失败：{message}")
        )
        self.thread_pool.start(worker)

    def _delete_backtest_run(self) -> None:
        run_id = self.backtest_run_combo.currentData()
        if run_id is None:
            self.storage_status.setText("当前没有可删除的回测运行。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除回测",
            f"确认删除回测运行 #{int(run_id)} 及其交易、净值、持仓和指标明细？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        count = self.store.delete_backtest_run(int(run_id))
        self.storage_status.setText(
            "已删除所选回测运行及全部关联明细。"
            if count
            else "所选回测运行已不存在。"
        )
        self.refresh()
