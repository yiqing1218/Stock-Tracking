from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from .data_provider import DataProvider
from .main_window import MainWindow
from .repository import Repository
from .ui_common import APP_STYLESHEET


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    QCoreApplication.setOrganizationName("ChengJian")
    QCoreApplication.setOrganizationDomain("local.astockmonitor")
    QCoreApplication.setApplicationName("AStockMonitor")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLESHEET)

    root = project_root()
    data_dir = root / "data"
    repository = Repository(data_dir / "astock_monitor.db")
    provider = DataProvider(data_dir / "cache")

    def handle_exception(exception_type, exception, traceback_object) -> None:  # type: ignore[no-untyped-def]
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, traceback_object)
            return
        sys.__excepthook__(exception_type, exception, traceback_object)
        QMessageBox.critical(
            None,
            "应用发生错误",
            f"{exception_type.__name__}: {exception}\n\n请保留当前界面并重试；不会上传任何本地数据。",
        )

    sys.excepthook = handle_exception
    window = MainWindow(repository, provider)
    window.show()
    window.start()
    return app.exec()
