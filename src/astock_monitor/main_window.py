from __future__ import annotations

import os

from PySide6.QtCore import QSettings, QThreadPool, Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QApplication,
    QMenu,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QSystemTrayIcon,
)

from .data_provider import DataProvider
from .data_management_page import DataManagementPage
from .detail_page import DetailPage
from .historical_store import HistoricalStore
from .market_dashboard_page import MarketDashboardPage
from .alerts import MessagePage
from .models import Security
from .repository import Repository
from .screening_page import ScreeningPage
from .strategy_page import StrategyBacktestPage
from .time_utils import recent_completed_market_days
from .watchlist_page import WatchlistPage
from .ui_common import Worker


class MainWindow(QMainWindow):
    def __init__(self, repository: Repository, provider: DataProvider) -> None:
        super().__init__()
        self.repository = repository
        self.provider = provider
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(4)
        self.setWindowTitle("澄鉴 A股监看")
        self.setMinimumSize(1180, 720)
        self.resize(1480, 900)

        self._last_watchlist_security: Security | None = None
        self._force_quit = False
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        navigation = QFrame()
        navigation.setObjectName("MainNavigation")
        navigation_layout = QHBoxLayout(navigation)
        navigation_layout.setContentsMargins(18, 8, 18, 8)
        navigation_layout.setSpacing(6)
        brand = QLabel("澄鉴 A股监看")
        brand.setObjectName("NavigationBrand")
        navigation_layout.addWidget(brand)
        navigation_layout.addSpacing(18)

        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        self.navigation_buttons: dict[str, QPushButton] = {}
        for key, label in (
            ("market", "大盘监看"),
            ("screener", "条件荐股"),
            ("watchlist", "自选股票"),
            ("detail", "股票详情"),
            ("strategy", "量化策略/回测/自定义指标"),
            ("alerts", "消息提示"),
            ("data", "数据导出"),
        ):
            button = QPushButton(label)
            button.setObjectName("MainNavigationButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked=False, page_key=key: self.show_page(page_key)
            )
            self.navigation_group.addButton(button)
            self.navigation_buttons[key] = button
            navigation_layout.addWidget(button)
        navigation_layout.addStretch(1)
        environment = QLabel("A股 · ETF · 指数  |  北京时间")
        environment.setObjectName("Tiny")
        navigation_layout.addWidget(environment)
        shell_layout.addWidget(navigation)

        self.stack = QStackedWidget()
        self.historical_store = HistoricalStore(repository.database_path)
        self.provider.attach_store(self.historical_store)
        self._startup_prefetch_worker: Worker | None = None
        self.market_page = MarketDashboardPage(
            provider, self.thread_pool, repository=repository, store=self.historical_store
        )
        self.screening_page = ScreeningPage(
            repository, provider, self.thread_pool, self.historical_store
        )
        self.watchlist_page = WatchlistPage(repository, provider, self.thread_pool)
        self.detail_page = DetailPage(
            repository, provider, self.thread_pool, self.historical_store
        )
        self.strategy_page = StrategyBacktestPage(
            repository,
            provider,
            self.thread_pool,
            self.detail_page.custom_tab,
            self.historical_store,
        )
        self.alerts_page = MessagePage(repository, provider, self.thread_pool)
        self.data_page = DataManagementPage(
            repository, provider, self.historical_store, self.thread_pool
        )
        self.pages = {
            "market": self.market_page,
            "screener": self.screening_page,
            "watchlist": self.watchlist_page,
            "detail": self.detail_page,
            "strategy": self.strategy_page,
            "alerts": self.alerts_page,
            "data": self.data_page,
        }
        self.stack.addWidget(self.market_page)
        self.stack.addWidget(self.screening_page)
        self.stack.addWidget(self.watchlist_page)
        self.stack.addWidget(self.detail_page)
        self.stack.addWidget(self.strategy_page)
        self.stack.addWidget(self.alerts_page)
        self.stack.addWidget(self.data_page)
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(shell)

        self.watchlist_page.open_security.connect(self.open_detail)
        self.detail_page.back_requested.connect(self.show_watchlist)
        self.detail_page.watchlist_changed.connect(self.watchlist_page.start)
        self.alerts_page.unread_count_changed.connect(self._update_unread_count)
        self._setup_tray()
        self._restore_geometry()

    def start(self) -> None:
        self.show_page("market")
        self.alerts_page.start()
        self._start_recent_intraday_prefetch()

    def _start_recent_intraday_prefetch(self) -> None:
        if self._startup_prefetch_worker is not None:
            return
        securities = self.repository.list_watchlist()
        if not securities:
            self.data_page.set_startup_prefetch_status("自选列表为空，未执行近5日分时下载。")
            return
        self.data_page.set_startup_prefetch_status(
            f"后台准备 {len(securities)} 只自选证券最近5个可用交易日的一分钟分时…"
        )
        worker = Worker(self._prefetch_recent_intraday, securities)
        self._startup_prefetch_worker = worker
        worker.signals.result.connect(
            lambda value: self.data_page.set_startup_prefetch_status(str(value))
        )
        worker.signals.error.connect(
            lambda message: self.data_page.set_startup_prefetch_status(
                f"近5日分时后台下载未完成：{message}"
            )
        )
        worker.signals.finished.connect(
            lambda: setattr(self, "_startup_prefetch_worker", None)
        )
        self.thread_pool.start(worker)

    def _prefetch_recent_intraday(self, securities: list[Security]) -> str:
        candidates = recent_completed_market_days(9)
        completed = failed = 0
        for security in securities:
            success_days = 0
            for trading_day in candidates:
                if success_days >= 5:
                    break
                try:
                    frame, _source = self.provider.get_intraday(
                        security, trading_day, "1"
                    )
                    if frame.empty:
                        continue
                    success_days += 1
                    completed += 1
                except Exception:
                    failed += 1
        return (
            f"近5日分时后台下载完成：新增或复用 {completed} 个证券交易日"
            + (f"，不可用日期 {failed} 次。" if failed else "。")
        )

    def open_detail(self, security: Security) -> None:
        if not self.repository.contains_security(security):
            self._last_watchlist_security = None
            self.detail_page.show_empty_state()
            self.show_page("detail")
            return
        self._last_watchlist_security = security
        self.stack.setCurrentWidget(self.detail_page)
        self._set_navigation("detail")
        self.market_page.stop()
        self.watchlist_page.stop()
        self.detail_page.load_security(security)

    def show_watchlist(self) -> None:
        self.show_page("watchlist")

    def show_page(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.market_page.stop()
        self.watchlist_page.stop()
        if key == "detail":
            security = self._last_watchlist_security
            if security is None or not self.repository.contains_security(security):
                self.detail_page.show_empty_state()
            elif self.detail_page.security != security:
                self.detail_page.load_security(security)
        self.stack.setCurrentWidget(page)
        self._set_navigation(key)
        if key == "market":
            self.market_page.start()
        elif key == "screener":
            self.screening_page.start()
        elif key == "watchlist":
            self.watchlist_page.start()
        elif key == "alerts":
            self.alerts_page.start()

    def _update_unread_count(self, count: int) -> None:
        label = "消息提示" if count <= 0 else f"消息提示 ({count})"
        self.navigation_buttons["alerts"].setText(label)

    def _set_navigation(self, key: str) -> None:
        for name, button in self.navigation_buttons.items():
            button.setChecked(name == key)

    @staticmethod
    def _placeholder_page(title: str, description: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        panel = QFrame()
        panel.setObjectName("EmptyState")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(42, 42, 42, 42)
        heading = QLabel(title)
        heading.setObjectName("EmptyStateTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(description)
        body.setObjectName("Muted")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addStretch(1)
        panel_layout.addWidget(heading)
        panel_layout.addWidget(body)
        panel_layout.addStretch(1)
        layout.addWidget(panel, 1)
        return page

    def _restore_geometry(self) -> None:
        settings = QSettings()
        geometry = settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _setup_tray(self) -> None:
        self.tray_icon: QSystemTrayIcon | None = None
        if (
            os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"
            or not QSystemTrayIcon.isSystemTrayAvailable()
        ):
            return
        tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        show_action = QAction("显示澄鉴 A股监看", menu)
        quit_action = QAction("退出", menu)
        show_action.triggered.connect(self._restore_from_tray)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: (
                self._restore_from_tray()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )
        self.alerts_page.desktop_notification.connect(
            lambda title, message: tray.showMessage(
                title, message, QSystemTrayIcon.MessageIcon.Information, 6000
            )
        )
        tray.show()
        self.tray_icon = tray

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        self.alerts_page.stop()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings().setValue("window/geometry", self.saveGeometry())
        if self.tray_icon is not None and not self._force_quit:
            self.hide()
            event.ignore()
            if not getattr(self, "_tray_hint_shown", False):
                self._tray_hint_shown = True
                self.tray_icon.showMessage(
                    "澄鉴 A股监看",
                    "程序已在托盘继续运行提醒。",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            return
        self.alerts_page.stop()
        super().closeEvent(event)
