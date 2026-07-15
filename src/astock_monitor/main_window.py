from __future__ import annotations

from PySide6.QtCore import QSettings, QThreadPool, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .data_provider import DataProvider
from .detail_page import DetailPage
from .market_dashboard_page import MarketDashboardPage
from .models import Security
from .repository import Repository
from .watchlist_page import WatchlistPage


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
            ("watchlist", "自选股票"),
            ("detail", "股票详情"),
            ("strategy", "量化策略"),
            ("alerts", "消息提示"),
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
        self.market_page = MarketDashboardPage(provider, self.thread_pool)
        self.watchlist_page = WatchlistPage(repository, provider, self.thread_pool)
        self.detail_page = DetailPage(repository, provider, self.thread_pool)
        self.strategy_page = self._placeholder_page(
            "量化策略",
            "策略编排、回测与信号管理将在这里提供；当前版本先保留完整页面入口。",
        )
        self.alerts_page = self._placeholder_page(
            "消息提示",
            "价格、指标、资金与资讯提醒将在这里统一管理；当前版本先保留完整页面入口。",
        )
        self.pages = {
            "market": self.market_page,
            "watchlist": self.watchlist_page,
            "detail": self.detail_page,
            "strategy": self.strategy_page,
            "alerts": self.alerts_page,
        }
        self.stack.addWidget(self.market_page)
        self.stack.addWidget(self.watchlist_page)
        self.stack.addWidget(self.detail_page)
        self.stack.addWidget(self.strategy_page)
        self.stack.addWidget(self.alerts_page)
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(shell)

        self.watchlist_page.open_security.connect(self.open_detail)
        self.detail_page.back_requested.connect(self.show_watchlist)
        self.detail_page.watchlist_changed.connect(self.watchlist_page.start)
        self._restore_geometry()

    def start(self) -> None:
        self.show_page("market")

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
        elif key == "watchlist":
            self.watchlist_page.start()

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

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings().setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)
