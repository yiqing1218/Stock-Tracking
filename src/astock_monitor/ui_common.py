from __future__ import annotations

import math
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


APP_STYLESHEET = """
QWidget {
    background: #07111F;
    color: #DFE8F5;
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}
QMainWindow, QStackedWidget { background: #07111F; }
QFrame#TopBar, QFrame#Section, QFrame#Card {
    background: #0B1728;
    border: 1px solid #192A40;
    border-radius: 10px;
}
QFrame#MainNavigation {
    background: #081525;
    border: 0;
    border-bottom: 1px solid #20324A;
}
QFrame#EmptyState {
    background: #0A1627;
    border: 1px dashed #2B4260;
    border-radius: 12px;
}
QFrame#TopBar { border-radius: 0; border-left: 0; border-right: 0; border-top: 0; }
QFrame#ChartControls {
    background: rgba(8, 19, 33, 218);
    border: 1px solid #2B4260;
    border-radius: 8px;
}
QLabel#AppName { font-size: 18px; font-weight: 700; color: #F4F8FD; }
QLabel#NavigationBrand { font-size: 18px; font-weight: 800; color: #F4F8FD; }
QLabel#EmptyStateTitle { font-size: 26px; font-weight: 700; color: #DCEAF7; }
QLabel#PageTitle { font-size: 22px; font-weight: 700; color: #F4F8FD; }
QLabel#SecurityName { font-size: 24px; font-weight: 700; color: #F4F8FD; }
QLabel#Price { font-size: 30px; font-weight: 700; }
QLabel#Muted { color: #8496AF; }
QLabel#Tiny { color: #6F829D; font-size: 11px; }
QLabel#MetricValue { color: #F4F8FD; font-size: 20px; font-weight: 700; }
QLabel#MetricTitle { color: #8496AF; font-size: 12px; }
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #0E1C30;
    border: 1px solid #253852;
    border-radius: 7px;
    padding: 8px 10px;
    selection-background-color: #0EA5E9;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #38BDF8; }
QCheckBox { color: #AFC0D5; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QCheckBox::indicator:unchecked { border: 1px solid #3A587C; background: #0E1C30; border-radius: 3px; }
QCheckBox::indicator:checked { border: 1px solid #38BDF8; background: #0284C7; border-radius: 3px; }
QLineEdit#SearchBox { font-size: 14px; padding: 10px 14px; }
QPushButton {
    background: #12233A;
    color: #DDE7F5;
    border: 1px solid #263B57;
    border-radius: 7px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton:hover { background: #17304E; border-color: #3A587C; }
QPushButton:pressed { background: #0F2035; }
QPushButton:disabled { color: #52657E; background: #0C1828; border-color: #17283D; }
QPushButton#Primary { background: #0284C7; border-color: #0EA5E9; color: white; }
QPushButton#Primary:hover { background: #0397DF; }
QPushButton#Danger { color: #FF8297; border-color: #713044; }
QPushButton#Ghost { background: transparent; border-color: transparent; padding: 6px 9px; }
QPushButton#Ghost:hover { background: #12233A; border-color: #263B57; }
QPushButton#MainNavigationButton {
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: #8194AD;
    padding: 10px 14px 9px 14px;
}
QPushButton#MainNavigationButton:hover { color: #DCEAF7; background: #0D1D31; }
QPushButton#MainNavigationButton:checked {
    color: #F4F8FD;
    background: #0D2035;
    border-bottom-color: #38BDF8;
}
QPushButton#SubNavigation {
    background: #0D1B2E;
    color: #8194AD;
    border-color: #20344E;
    padding: 7px 14px;
}
QPushButton#SubNavigation:hover { color: #DDE7F5; border-color: #3A587C; }
QPushButton#SubNavigation:checked {
    background: #123B58;
    color: #F4F8FD;
    border-color: #38BDF8;
}
QPushButton#ChartControl {
    padding: 0;
    font-size: 18px;
    font-weight: 700;
    background: #10243B;
    border-color: #315071;
}
QPushButton#ChartControl:hover { background: #174064; border-color: #38BDF8; }
QPushButton#FavoriteFilter:checked {
    color: #FBBF24;
    border-color: #D9A91A;
    background: #2A230F;
}
QPushButton#FavoriteStar {
    padding: 2px;
    color: #71849D;
    background: transparent;
    border: 0;
    font-size: 19px;
}
QPushButton#FavoriteStar:checked { color: #FBBF24; }
QTableWidget, QTableView, QListWidget {
    background: #0A1525;
    alternate-background-color: #0C192B;
    border: 1px solid #192A40;
    border-radius: 8px;
    gridline-color: #17263A;
    selection-background-color: #123B58;
    selection-color: #FFFFFF;
    outline: none;
}
QTableWidget::item, QTableView::item { padding: 7px 8px; border-bottom: 1px solid #14243A; }
QHeaderView::section {
    background: #0F1D31;
    color: #8395AE;
    border: 0;
    border-bottom: 1px solid #253852;
    padding: 9px 8px;
    font-weight: 600;
}
QListWidget::item { padding: 10px; border-bottom: 1px solid #17263A; }
QListWidget::item:hover { background: #10243B; }
QListWidget::item:selected { background: #123B58; }
QTabWidget::pane { border: 0; background: #07111F; }
QTabBar::tab {
    background: transparent;
    color: #7F91AA;
    border: 0;
    padding: 12px 18px;
    font-weight: 600;
}
QTabBar::tab:selected { color: #E8F3FC; border-bottom: 2px solid #38BDF8; }
QTabBar::tab:hover { color: #CDE5F8; }
QScrollBar:vertical { background: #091523; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #263B57; min-height: 30px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #091523; height: 10px; }
QScrollBar::handle:horizontal { background: #263B57; min-width: 30px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QToolTip { background: #13233A; color: #F4F8FD; border: 1px solid #315071; padding: 6px; }
QSplitter::handle { background: #122136; }
QStatusBar { background: #091523; color: #7F91AA; border-top: 1px solid #192A40; }
"""


UP_COLOR = "#FF4D6D"
DOWN_COLOR = "#20C997"
NEUTRAL_COLOR = "#A8B6C9"


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function: Callable[..., object], *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            try:
                self.signals.error.emit(message)
            except RuntimeError:
                return
            traceback.print_exc()
        else:
            try:
                self.signals.result.emit(result)
            except RuntimeError:
                return
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setMinimumWidth(145)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(5)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("Tiny")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.subtitle_label)
        layout.addStretch(1)

    def set_value(self, value: str, subtitle: str | None = None, color: str | None = None) -> None:
        self.value_label.setText(value)
        self.value_label.setStyleSheet(f"color: {color};" if color else "")
        if subtitle is not None:
            self.subtitle_label.setText(subtitle)


class StatusPill(QLabel):
    def __init__(self, text: str = "—", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(74)
        self.set_status(text)

    def set_status(self, text: str) -> None:
        self.setText(text)
        if any(word in text for word in ("偏多", "上涨", "强势", "多方", "价在线上", "较优")):
            color, background, border = UP_COLOR, "#2A1723", "#683046"
        elif any(word in text for word in ("偏空", "下跌", "空方", "价在线下", "风险较高", "偏弱")):
            color, background, border = DOWN_COLOR, "#0B2927", "#1E5B53"
        elif any(word in text for word in ("过热", "偏热", "高波动", "上轨外", "明显放量")):
            color, background, border = "#FBBF24", "#2A2410", "#66561C"
        elif any(word in text for word in ("超卖", "偏冷", "低波动", "下轨外", "缩量")):
            color, background, border = "#7DD3FC", "#102839", "#28536E"
        else:
            color, background, border = "#AAB8CA", "#172335", "#2B3D55"
        self.setStyleSheet(
            f"color:{color}; background:{background}; border:1px solid {border}; "
            "border-radius:10px; padding:3px 8px; font-size:11px; font-weight:600;"
        )


def configure_table(table: QTableWidget, alternating: bool = True) -> None:
    table.setAlternatingRowColors(alternating)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(48)
    table.setShowGrid(False)
    table.setSortingEnabled(False)
    table.horizontalHeader().setHighlightSections(False)


def format_number(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"
    value = float(value)
    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if absolute >= 10_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:.{decimals}f}"


def format_percent(value: float | None, signed: bool = True) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{float(value):.2f}%"


def change_color(value: float | None) -> str:
    if value is None or value == 0:
        return NEUTRAL_COLOR
    return UP_COLOR if value > 0 else DOWN_COLOR


def section_title(title: str, subtitle: str = "") -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    label = QLabel(title)
    label.setFont(QFont("Microsoft YaHei UI", 13, QFont.Weight.Bold))
    layout.addWidget(label)
    if subtitle:
        muted = QLabel(subtitle)
        muted.setObjectName("Muted")
        layout.addWidget(muted)
    layout.addStretch(1)
    return widget
