from __future__ import annotations

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from .data_provider import DataProvider
from .detail_page import DetailPage
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

        self.stack = QStackedWidget()
        self.watchlist_page = WatchlistPage(repository, provider, self.thread_pool)
        self.detail_page = DetailPage(repository, provider, self.thread_pool)
        self.stack.addWidget(self.watchlist_page)
        self.stack.addWidget(self.detail_page)
        self.setCentralWidget(self.stack)

        self.watchlist_page.open_security.connect(self.open_detail)
        self.detail_page.back_requested.connect(self.show_watchlist)
        self.detail_page.watchlist_changed.connect(self.watchlist_page.start)
        self._restore_geometry()

    def start(self) -> None:
        self.watchlist_page.start()

    def open_detail(self, security: Security) -> None:
        self.stack.setCurrentWidget(self.detail_page)
        self.detail_page.load_security(security)

    def show_watchlist(self) -> None:
        self.stack.setCurrentWidget(self.watchlist_page)
        self.watchlist_page.start()

    def _restore_geometry(self) -> None:
        settings = QSettings()
        geometry = settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings().setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)

