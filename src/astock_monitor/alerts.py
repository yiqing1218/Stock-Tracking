from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from datetime import time

import pandas as pd
from PySide6.QtCore import QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .company_events import CompanyEventService
from .alert_engine import AlertEngine
from .historical_store import HistoricalStore
from .models import NewsArticle, Quote, Security, SecurityType
from .repository import Repository
from .time_utils import beijing_now
from .ui_common import Worker, configure_table, section_title


ALERT_RULES = {
    "price_above": "股价突破某一价格",
    "price_below": "股价跌破某一价格",
    "pct_above": "涨幅超过阈值(%)",
    "pct_below": "跌幅超过阈值(%)",
    "amount_above": "成交额超过阈值(元)",
    "turnover_above": "换手率异常(%)",
    "volume_ratio_above": "量比异常",
    "volume_up": "放量上涨",
    "volume_down": "放量下跌",
    "break_high": "突破近期最高价",
    "break_low": "跌破近期最低价",
    "near_limit_up": "接近涨停",
    "near_limit_down": "接近跌停",
    "sealed_limit": "封板",
    "opened_limit": "开板/炸板",
    "rapid_up": "短时间急涨(%)",
    "rapid_down": "短时间急跌(%)",
}

EVENT_KEYWORDS = (
    ("业绩预告", ("业绩预告", "预盈", "预亏")),
    ("财报披露", ("年度报告", "季度报告", "半年度报告", "财报")),
    ("分红", ("分红", "派息", "除权除息")),
    ("限售股解禁", ("解禁", "限售股")),
    ("股东减持", ("减持",)),
    ("股权质押", ("质押",)),
    ("回购", ("回购",)),
    ("停牌或复牌", ("停牌", "复牌")),
    ("龙虎榜", ("龙虎榜",)),
    ("大宗交易", ("大宗交易",)),
    ("融资余额异常", ("融资余额", "融资融券")),
    ("发布公告", ("公告",)),
)


def is_a_share_market_open() -> bool:
    now = beijing_now()
    if now.weekday() >= 5:
        return False
    current = now.time().replace(tzinfo=None)
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(
        15, 0
    )


def classify_event(article: NewsArticle) -> str:
    text = f"{article.title} {article.summary}"
    for event_type, keywords in EVENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return event_type
    return "资讯"


def notification_key(security: Security, article: NewsArticle) -> str:
    raw = f"{security.key}|{article.title}|{article.published_at}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class AlertSettingsWidget(QWidget):
    def __init__(self, repository: Repository) -> None:
        super().__init__()
        self.repository = repository
        self.historical_store = HistoricalStore(repository.database_path)
        self.alert_engine = AlertEngine(self.historical_store)
        self.company_event_service = CompanyEventService(
            self.historical_store, repository
        )
        self.security: Security | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(
            section_title("行情提醒设置", "仅在A股开盘时检测；只保存规则和最近状态")
        )
        form = QHBoxLayout()
        self.rule_combo = QComboBox()
        for key, label in ALERT_RULES.items():
            self.rule_combo.addItem(label, key)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-1e12, 1e12)
        self.threshold.setDecimals(4)
        self.threshold.setValue(5)
        self.window = QSpinBox()
        self.window.setRange(1, 120)
        self.window.setValue(5)
        self.enabled = QCheckBox("启用")
        self.enabled.setChecked(True)
        save = QPushButton("保存/更新规则")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        form.addWidget(QLabel("提醒类型"))
        form.addWidget(self.rule_combo, 2)
        form.addWidget(QLabel("阈值"))
        form.addWidget(self.threshold)
        form.addWidget(QLabel("窗口(分钟)"))
        form.addWidget(self.window)
        form.addWidget(self.enabled)
        form.addWidget(save)
        layout.addLayout(form)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["提醒类型", "阈值", "窗口", "状态", "操作"]
        )
        configure_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

    def set_security(self, security: Security | None) -> None:
        self.security = security
        self.reload()

    def reload(self) -> None:
        rules = (
            self.repository.list_market_alert_rules(self.security)
            if self.security
            else []
        )
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            values = [
                ALERT_RULES.get(rule["rule_type"], rule["rule_type"]),
                "—" if rule["threshold"] is None else str(rule["threshold"]),
                f"{rule['window_minutes']} 分钟",
                "启用" if rule["enabled"] else "停用",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            button = QPushButton("删除")
            button.clicked.connect(
                lambda checked=False, rule_id=rule["id"]: self._delete(rule_id)
            )
            self.table.setCellWidget(row, 4, button)

    def _save(self) -> None:
        if self.security is None:
            QMessageBox.information(self, "未选择股票", "请先从自选股进入股票详情。")
            return
        self.repository.save_market_alert_rule(
            self.security,
            str(self.rule_combo.currentData()),
            self.threshold.value(),
            window_minutes=self.window.value(),
            enabled=self.enabled.isChecked(),
        )
        condition_map = {
            "price_above": ("price", "cross_up"),
            "price_below": ("price", "cross_down"),
            "pct_above": ("change_pct", "above"),
            "pct_below": ("change_pct", "below"),
            "amount_above": ("amount", "above"),
            "turnover_above": ("turnover", "above"),
            "volume_ratio_above": ("volume_ratio", "above"),
            "volume_up": ("volume_ratio", "above"),
            "volume_down": ("volume_ratio", "above"),
        }
        rule_key = str(self.rule_combo.currentData())
        if rule_key in condition_map:
            condition, mode = condition_map[rule_key]
            self.alert_engine.save_rule(
                ALERT_RULES[rule_key],
                condition,
                mode,
                self.threshold.value(),
                "single",
                self.security.key,
                cooldown_seconds=max(60, self.window.value() * 60),
            )
        self.reload()

    def _delete(self, rule_id: int) -> None:
        self.repository.delete_market_alert_rule(rule_id)
        self.reload()


class MessagePage(QWidget):
    unread_count_changed = Signal(int)
    desktop_notification = Signal(str, str)

    def __init__(
        self, repository: Repository, provider: DataProvider, thread_pool: QThreadPool
    ) -> None:
        super().__init__()
        self.repository = repository
        self.provider = provider
        self.thread_pool = thread_pool
        self.alert_engine = AlertEngine(HistoricalStore(repository.database_path))
        self._running = False
        self._news_started = False
        self._workers: set[Worker] = set()
        self._recent: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=20)
        )
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self._market_tick)
        self.news_timer = QTimer(self)
        self.news_timer.setInterval(10 * 60_000)
        self.news_timer.timeout.connect(self.scan_news)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = QHBoxLayout()
        header.addWidget(
            section_title("消息提示", "自选股资讯、公司事件与开盘行情提醒")
        )
        header.addStretch(1)
        self.unread_only = QCheckBox("只看未读")
        self.unread_only.toggled.connect(self.reload)
        scan = QPushButton("刷新资讯与事件")
        scan.clicked.connect(self.scan_news)
        read_all = QPushButton("全部已读")
        read_all.clicked.connect(self._read_all)
        header.addWidget(self.unread_only)
        header.addWidget(read_all)
        header.addWidget(scan)
        layout.addLayout(header)
        self.status = QLabel("行情规则只在北京时间交易时段运行。")
        self.status.setObjectName("Muted")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["状态", "时间", "股票", "类型", "标题", "来源"]
        )
        configure_table(self.table)
        self.table.cellDoubleClicked.connect(self._read_row)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

    def start(self) -> None:
        self.reload()
        if not self.timer.isActive():
            self.timer.start()
        if not self.news_timer.isActive():
            self.news_timer.start()
        if not self._news_started:
            self._news_started = True
            QTimer.singleShot(2_000, self.scan_news)
        self._market_tick()

    def stop(self) -> None:
        self.timer.stop()
        self.news_timer.stop()

    def reload(self) -> None:
        rows = self.repository.list_notifications(self.unread_only.isChecked())
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                "未读" if not row["is_read"] else "已读",
                row["published_at"] or row["created_at"],
                f"{row['name']} {row['code']}",
                row["event_type"],
                row["title"],
                row["source_url"] or "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {"id": row["id"], "url": row["source_url"] or ""},
                )
                self.table.setItem(row_index, column, item)
        count = self.repository.unread_notification_count()
        self.unread_count_changed.emit(count)
        self.status.setText(
            f"未读 {count} 条；行情监控：{'运行中' if is_a_share_market_open() else '非交易时段暂停'}。"
        )

    def scan_news(self) -> None:
        if self._running:
            return
        self._running = True
        worker = Worker(self._scan_news_impl)
        self._workers.add(worker)
        worker.signals.result.connect(lambda _value: self.reload())
        worker.signals.error.connect(
            lambda message: self.status.setText(f"资讯刷新失败：{message}")
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        worker.signals.finished.connect(lambda: setattr(self, "_running", False))
        self.thread_pool.start(worker)

    def _scan_news_impl(self) -> int:
        inserted = 0
        watchlist = self.repository.list_watchlist()
        structured = self.company_event_service.sync_many(watchlist)
        inserted += structured.notified
        for security in watchlist:
            for index, article in enumerate(
                self.provider.get_news(security, force=True)
            ):
                published = pd.to_datetime(article.published_at, errors="coerce")
                if pd.notna(published):
                    if getattr(published, "tzinfo", None) is not None:
                        published = published.tz_localize(None)
                    if published < pd.Timestamp(
                        beijing_now().replace(tzinfo=None)
                    ) - pd.Timedelta(days=3):
                        continue
                elif index >= 10:
                    continue
                inserted += int(
                    self.repository.add_notification(
                        security,
                        classify_event(article),
                        article.title,
                        content=article.summary,
                        source_url=article.url,
                        external_key=notification_key(security, article),
                        published_at=article.published_at,
                    )
                )
        try:
            notices = self.provider.get_watchlist_notices(watchlist)
        except Exception:
            notices = []
        for security, article in notices:
            inserted += int(
                self.repository.add_notification(
                    security,
                    classify_event(article),
                    article.title,
                    content=article.summary,
                    source_url=article.url,
                    external_key=notification_key(security, article),
                    published_at=article.published_at,
                )
            )
        return inserted

    def _market_tick(self) -> None:
        if not is_a_share_market_open() or self._running:
            return
        rules = self.repository.list_market_alert_rules(enabled_only=True)
        unified_rules = self.alert_engine.list_rules(enabled_only=True)
        if not rules and not unified_rules:
            return
        securities = {
            f"{row['security_type']}:{row['code']}": Security(
                row["code"], row["name"], SecurityType(row["security_type"])
            )
            for row in rules
        }
        for rule in unified_rules:
            for security in self.alert_engine.resolve_targets(rule):
                securities[security.key] = security
        self._running = True
        worker = Worker(
            self.provider.refresh_quotes_efficient, list(securities.values())
        )
        self._workers.add(worker)
        worker.signals.result.connect(
            lambda quotes: self._evaluate_all_rules(rules, unified_rules, quotes)
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        worker.signals.finished.connect(lambda: setattr(self, "_running", False))
        self.thread_pool.start(worker)

    def _evaluate_all_rules(
        self, legacy: list[dict], unified: list[dict], quotes: object
    ) -> None:
        self._evaluate_rules(legacy, quotes)
        if not isinstance(quotes, dict):
            return
        for rule in unified:
            for security in self.alert_engine.resolve_targets(rule):
                quote = quotes.get(security.key)
                event = (
                    self.alert_engine.evaluate_quote(rule, quote)
                    if isinstance(quote, Quote) and quote.price is not None
                    else (
                        self.alert_engine.evaluate_history(rule, security)
                        if rule.get("rule_type") != "quote"
                        else None
                    )
                )
                if event is None:
                    continue
                external = hashlib.sha256(
                    f"unified:{event.rule_id}:{event.security.key}:{beijing_now():%Y%m%d%H%M%S}".encode()
                ).hexdigest()
                self.repository.add_notification(
                    event.security,
                    "统一提醒",
                    event.title,
                    content=event.message,
                    external_key=external,
                    published_at=f"{beijing_now():%Y-%m-%d %H:%M:%S}",
                )
                self.desktop_notification.emit(event.title, event.message)
        self.reload()

    def _evaluate_rules(self, rules: list[dict], value: object) -> None:
        if not isinstance(value, dict):
            return
        for rule in rules:
            key = f"{rule['security_type']}:{rule['code']}"
            quote = value.get(key)
            if not isinstance(quote, Quote) or quote.price is None:
                continue
            self._recent[key].append((beijing_now().timestamp(), quote.price))
            triggered = self._rule_triggered(rule, quote, self._recent[key])
            state = "1" if triggered else "0"
            if triggered and rule.get("last_state") != "1":
                security = quote.security
                title = f"{security.name}：{ALERT_RULES.get(rule['rule_type'], rule['rule_type'])}"
                external = hashlib.sha256(
                    f"rule:{rule['id']}:{beijing_now():%Y%m%d%H%M}".encode()
                ).hexdigest()
                self.repository.add_notification(
                    security,
                    "行情提醒",
                    title,
                    external_key=external,
                    published_at=f"{beijing_now():%Y-%m-%d %H:%M:%S}",
                )
                self.desktop_notification.emit(
                    title, "行情提醒已触发，双击消息可查看记录。"
                )
            self.repository.update_market_alert_state(rule["id"], state)
        self.reload()

    @staticmethod
    def _rule_triggered(
        rule: dict, quote: Quote, recent: deque[tuple[float, float]]
    ) -> bool:
        kind = rule["rule_type"]
        threshold = float(rule["threshold"] or 0)
        price = float(quote.price or 0)
        pct = float(quote.change_pct or 0)
        if kind == "price_above":
            return price >= threshold
        if kind == "price_below":
            return price <= threshold
        if kind == "pct_above":
            return pct >= threshold
        if kind == "pct_below":
            return pct <= -abs(threshold)
        if kind == "amount_above":
            return float(quote.amount or 0) >= threshold
        if kind == "turnover_above":
            return float(quote.turnover or 0) >= threshold
        if kind == "volume_ratio_above":
            return float(quote.volume_ratio or 0) >= threshold
        if kind == "volume_up":
            return pct > 0 and float(quote.volume_ratio or 0) >= threshold
        if kind == "volume_down":
            return pct < 0 and float(quote.volume_ratio or 0) >= threshold
        limit_rate = (
            5
            if "ST" in quote.security.name.upper()
            else 20
            if quote.security.code.startswith(("300", "301", "688", "689"))
            else 30
            if quote.security.market == "bj"
            else 10
        )
        if kind in {"near_limit_up", "sealed_limit"}:
            return pct >= limit_rate - max(threshold, 0.2)
        if kind == "near_limit_down":
            return pct <= -(limit_rate - max(threshold, 0.2))
        if kind == "opened_limit":
            return float(quote.high or 0) > price and pct >= limit_rate - 1
        if kind == "break_high":
            return quote.high is not None and price >= float(quote.high)
        if kind == "break_low":
            return quote.low is not None and price <= float(quote.low)
        if kind in {"rapid_up", "rapid_down"} and len(recent) >= 2:
            base = recent[0][1]
            move = (price / base - 1) * 100 if base else 0
            return move >= threshold if kind == "rapid_up" else move <= -abs(threshold)
        return False

    def _read_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item:
            value = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(value, dict):
                notification_id = int(value["id"])
                url = str(value.get("url") or "").strip()
            else:
                notification_id = int(value)
                url = ""
            self.repository.mark_notification_read(notification_id)
            if url and QUrl(url).scheme().lower() in {"http", "https"}:
                QDesktopServices.openUrl(QUrl(url))
            elif url:
                self.status.setText("该消息的来源地址不是可打开的 http/https 网页。")
            self.reload()

    def _read_all(self) -> None:
        self.repository.mark_all_notifications_read()
        self.reload()
