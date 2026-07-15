from __future__ import annotations

import json
import pandas as pd
from PySide6.QtCore import QDate, QThreadPool, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .company_events import EVENT_TYPES, CompanyEventService, EventSyncResult
from .models import Security
from .time_utils import beijing_now, beijing_today
from .ui_common import Worker, configure_table, section_title


class EventTimelineWidget(QWidget):
    """Company-event timeline backed by the local warehouse.

    Local rows are always rendered first. Network adapters run in the shared thread
    pool and failures are reported per source without discarding successful sources.
    """

    events_changed = Signal(object)

    def __init__(
        self,
        service: CompanyEventService,
        thread_pool: QThreadPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.thread_pool = thread_pool
        self.security: Security | None = None
        self._rows: list[dict] = []
        self._worker: Worker | None = None
        self._running = False
        self._auto_synced_key = ""
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        header = QHBoxLayout()
        header.addWidget(
            section_title(
                "公告与上市公司事件时间线",
                "规则分类 · 原始来源可追溯 · 重要度为澄鉴透明规则评分",
            )
        )
        header.addStretch(1)
        self.sync_button = QPushButton("增量同步")
        self.sync_button.setObjectName("Primary")
        self.sync_button.clicked.connect(self.sync)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        header.addWidget(self.cancel_button)
        header.addWidget(self.sync_button)
        layout.addLayout(header)

        filters = QHBoxLayout()
        today = beijing_today()
        self.start_date = QDateEdit(
            QDate(today.year - 2, today.month, min(today.day, 28))
        )
        self.end_date = QDateEdit(QDate(today.year, today.month, today.day))
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setMaximumDate(QDate(today.year, today.month, today.day))
            edit.dateChanged.connect(self.reload)
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部事件", "")
        for key, label in EVENT_TYPES.items():
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self.reload)
        self.official_only = QCheckBox("仅官方来源")
        self.official_only.toggled.connect(self.reload)
        self.importance = QSpinBox()
        self.importance.setRange(0, 100)
        self.importance.setSingleStep(10)
        self.importance.setSuffix(" 分以上")
        self.importance.valueChanged.connect(self.reload)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题或摘要")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)
        filters.addWidget(QLabel("日期"))
        filters.addWidget(self.start_date)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date)
        filters.addWidget(self.type_combo)
        filters.addWidget(self.official_only)
        filters.addWidget(QLabel("重要度"))
        filters.addWidget(self.importance)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)

        self.status = QLabel("请选择证券。")
        self.status.setObjectName("Muted")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["状态", "公告日", "事件/生效日", "类型", "重要度", "标题", "来源", "备注"]
        )
        configure_table(self.table)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.table.cellDoubleClicked.connect(self._open_row)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for index in (0, 1, 2, 3, 4, 6, 7):
            header_view.setSectionResizeMode(
                index, QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(self.table, 1)
        footnote = QLabel(
            "双击打开原始网页；右键可查看原始数据、编辑研究备注或切换已读状态。"
            "情绪字段默认未知，不对标题作确定性利好/利空判断。"
        )
        footnote.setObjectName("Tiny")
        footnote.setWordWrap(True)
        layout.addWidget(footnote)

    def set_security(self, security: Security | None) -> None:
        self.security = security
        self._auto_synced_key = ""
        self.reload()

    def sync_if_needed(self) -> None:
        if self.security is None or self._running:
            return
        if self._auto_synced_key == self.security.key:
            return
        self._auto_synced_key = self.security.key
        scope = f"security:{self.security.key}"
        with self.service.store.connect() as db:
            state = db.execute(
                "SELECT last_success_at FROM event_sync_state WHERE scope_key=?",
                (scope,),
            ).fetchone()
        if state and state["last_success_at"]:
            refreshed = pd.to_datetime(
                state["last_success_at"], errors="coerce", utc=True
            )
            now = pd.Timestamp(beijing_now())
            if pd.notna(refreshed) and now - refreshed < pd.Timedelta(hours=6):
                return
        self.sync()

    def reload(self, *_args: object) -> None:
        if self.security is None:
            self._rows = []
            self.table.setRowCount(0)
            self.status.setText("请先从自选股票进入一只证券。")
            self.events_changed.emit(pd.DataFrame())
            return
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        self._rows = self.service.list_events(
            self.security,
            start=start,
            end=end,
            event_type=str(self.type_combo.currentData() or ""),
            official_only=self.official_only.isChecked(),
            minimum_importance=self.importance.value(),
            query=self.search.text().strip(),
        )
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            values = (
                "未读" if not row["is_read"] else "已读",
                row["announcement_date"] or "—",
                row["effective_date"] or row["event_date"] or "—",
                EVENT_TYPES.get(row["event_type"], row["event_type"]),
                str(row["importance"]),
                row["title"],
                f"{'官方 · ' if row['official_source'] else ''}{row['source_name']}",
                "有" if row.get("note") else "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                if column == 5:
                    item.setToolTip(row["summary"] or row["title"])
                self.table.setItem(row_index, column, item)
        markers = self.service.event_markers(self.security, start=start)
        self.events_changed.emit(markers)
        if not self._running:
            self.status.setText(
                f"本地共 {len(self._rows)} 条；最近一次同步状态可在增量同步后查看。"
            )

    def sync(self) -> None:
        if self.security is None or self._running:
            return
        self._running = True
        self.sync_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("正在按来源逐项增量同步；单一来源失败不会清空其他结果…")
        security = self.security
        start = self.start_date.date().toPython()
        end = self.end_date.date().toPython()
        worker = Worker(self.service.sync_security, security, start, end)
        self._worker = worker
        worker.signals.result.connect(self._sync_finished)
        worker.signals.error.connect(self._sync_error)
        worker.signals.finished.connect(self._sync_cleanup)
        self.thread_pool.start(worker)

    def focus_event(self, event_id: int) -> None:
        self.reload()
        for row_index, row in enumerate(self._rows):
            if int(row["id"]) == int(event_id):
                self.table.selectRow(row_index)
                self.table.scrollToItem(self.table.item(row_index, 5))
                break

    def _cancel(self) -> None:
        self.service.cancel()
        self.status.setText("正在安全停止；已写入的数据会保留。")

    def _sync_finished(self, result: object) -> None:
        if not isinstance(result, EventSyncResult):
            return
        self.reload()
        errors = f"；{len(result.errors)} 个来源失败" if result.errors else ""
        cancelled = "；已取消" if result.cancelled else ""
        self.status.setText(
            f"同步完成：处理 {result.processed}，新增 {result.inserted}，更新 {result.updated}，"
            f"新消息 {result.notified}{errors}{cancelled}"
        )
        if result.errors:
            self.status.setToolTip("\n".join(result.errors))

    def _sync_error(self, message: str) -> None:
        self.status.setText(f"事件同步失败：{message}")

    def _sync_cleanup(self) -> None:
        self._running = False
        self._worker = None
        self.sync_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _row(self, table_row: int) -> dict | None:
        if 0 <= table_row < len(self._rows):
            return self._rows[table_row]
        return None

    def _open_row(self, table_row: int, _column: int) -> None:
        row = self._row(table_row)
        if row is None:
            return
        self.service.mark_read(int(row["id"]))
        url = str(row.get("source_url") or "").strip()
        if url and QUrl(url).scheme().lower() in {"http", "https"}:
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(
                self, "无原始网页", "该来源没有提供可打开的网页地址。"
            )
        self.reload()

    def _show_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        table_row = self.table.rowAt(position.y())
        row = self._row(table_row)
        if row is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction("打开原始网页")
        raw_action = menu.addAction("查看原始数据")
        note_action = menu.addAction("编辑研究备注")
        read_action = menu.addAction("标记未读" if row["is_read"] else "标记已读")
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected is open_action:
            self._open_row(table_row, 0)
        elif selected is raw_action:
            self._show_raw(row)
        elif selected is note_action:
            self._edit_note(row)
        elif selected is read_action:
            self.service.mark_read(int(row["id"]), not bool(row["is_read"]))
            self.reload()

    def _show_raw(self, row: dict) -> None:
        payloads = self.service.raw_payloads(int(row["id"]))
        dialog = QDialog(self)
        dialog.setWindowTitle("事件原始数据与来源")
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(json.dumps(payloads, ensure_ascii=False, indent=2))
        layout.addWidget(editor)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _edit_note(self, row: dict) -> None:
        note, accepted = QInputDialog.getMultiLineText(
            self,
            "研究备注",
            row["title"],
            str(row.get("note") or ""),
        )
        if accepted:
            self.service.save_note(int(row["id"]), note)
            self.reload()
