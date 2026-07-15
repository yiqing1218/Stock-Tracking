from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


BACKGROUND = QColor("#0B1728")
PANEL = QColor("#081321")
GRID = QColor("#1A2A40")
TEXT = QColor("#DDE7F5")
MUTED = QColor("#7F91AA")
BLUE = QColor("#38BDF8")
PINK = QColor("#F472B6")
UP = QColor("#FF4D6D")
DOWN = QColor("#20C997")


def number_from_text(value: object) -> float | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, (int, float, np.number)):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "—", "nan", "None"}:
        return None
    multiplier = 1.0
    if "万亿" in text:
        multiplier = 1_000_000_000_000
    elif "亿" in text:
        multiplier = 100_000_000
    elif "万" in text:
        multiplier = 10_000
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    result = float(match.group()) * multiplier
    return result if math.isfinite(result) else None


def _row_number(row: pd.Series, *names: str) -> float | None:
    for name in names:
        if name in row.index:
            value = number_from_text(row.get(name))
            if value is not None:
                return value
    return None


class ChipDistributionWidget(QWidget):
    """Paint a compact cost-profile estimate from AkShare CYQ intervals."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._chips = pd.DataFrame()
        self._latest_price: float | None = None

    def set_data(self, chips: pd.DataFrame, latest_price: float | None = None) -> None:
        self._chips = chips.copy() if chips is not None else pd.DataFrame()
        self._latest_price = latest_price
        self.update()

    def clear(self) -> None:
        self._chips = pd.DataFrame()
        self._latest_price = None
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        bounds = QRectF(self.rect()).adjusted(10, 8, -10, -10)
        painter.fillRect(bounds, PANEL)
        painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        painter.setPen(TEXT)
        painter.drawText(QPointF(bounds.left() + 8, bounds.top() + 19), "筹码分布图")
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(MUTED)
        painter.drawText(QPointF(bounds.left() + 8, bounds.top() + 36), "基于最新成本区间的分布估算")
        if self._chips.empty:
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "暂无筹码分布数据")
            return

        row = self._chips.iloc[-1]
        average = _row_number(row, "平均成本")
        low90 = _row_number(row, "90成本-低", "90%成本-低")
        high90 = _row_number(row, "90成本-高", "90%成本-高")
        low70 = _row_number(row, "70成本-低", "70%成本-低")
        high70 = _row_number(row, "70成本-高", "70%成本-高")
        values = [value for value in (average, low90, high90, low70, high70, self._latest_price) if value and value > 0]
        if len(values) < 2:
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "成本区间字段不足")
            return
        low_value, high_value = min(values), max(values)
        span = max(high_value - low_value, high_value * 0.03)
        low_value -= span * 0.12
        high_value += span * 0.12
        plot = QRectF(bounds.left() + 50, bounds.top() + 48, bounds.width() - 60, bounds.height() - 58)
        painter.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
        for step in range(5):
            y = plot.top() + plot.height() * step / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            value = high_value - (high_value - low_value) * step / 4
            painter.setPen(MUTED)
            painter.drawText(QPointF(bounds.left() + 4, y + 4), f"{value:.2f}")
            painter.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))

        center = average or (low_value + high_value) / 2
        sigma70 = max(((high70 or center) - (low70 or center)) / 2.1, span * 0.08)
        sigma90 = max(((high90 or center) - (low90 or center)) / 3.3, span * 0.14)
        bins = np.linspace(low_value, high_value, 50)
        weights = np.exp(-0.5 * ((bins - center) / sigma70) ** 2)
        weights += 0.42 * np.exp(-0.5 * ((bins - center) / sigma90) ** 2)
        weights /= max(float(weights.max()), 1e-12)
        bar_height = plot.height() / len(bins)
        for price, weight in zip(bins, weights, strict=True):
            y = plot.bottom() - (price - low_value) / (high_value - low_value) * plot.height()
            width = plot.width() * float(weight) * 0.92
            color = QColor(UP if self._latest_price is not None and price <= self._latest_price else DOWN)
            color.setAlpha(185)
            painter.fillRect(QRectF(plot.right() - width, y - bar_height * 0.45, width, max(1.2, bar_height * 0.9)), color)

        def draw_marker(value: float | None, color: QColor, label: str) -> None:
            if value is None or value <= 0:
                return
            y = plot.bottom() - (value - low_value) / (high_value - low_value) * plot.height()
            painter.setPen(QPen(color, 1.2, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(QPointF(plot.left() + 4, y - 3), f"{label} {value:.2f}")

        draw_marker(average, BLUE, "均价")
        draw_marker(self._latest_price, QColor("#FBBF24"), "现价")


class FinancialChartWidget(QWidget):
    """Show revenue/profit and their year-on-year trends without QtCharts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._frame = pd.DataFrame()

    def set_data(self, frame: pd.DataFrame) -> None:
        self._frame = frame.copy() if frame is not None else pd.DataFrame()
        self.update()

    @staticmethod
    def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            for column in columns:
                if candidate == column or candidate in column:
                    return column
        return None

    def _series(self) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        if self._frame.empty:
            return None
        frame = self._frame.copy()
        columns = [str(column) for column in frame.columns]
        date_column = self._find_column(columns, ("报告期", "报告日期", "日期", "date"))
        revenue_column = self._find_column(columns, ("营业总收入", "营业收入"))
        profit_column = self._find_column(columns, ("归属净利润", "净利润"))
        revenue_yoy_column = self._find_column(columns, ("营业总收入同比增长率", "营业收入同比增长率", "营业收入同比"))
        profit_yoy_column = self._find_column(columns, ("净利润同比增长率", "净利润同比"))
        if date_column is None or (revenue_column is None and profit_column is None):
            return None
        frame["_date"] = pd.to_datetime(frame[date_column], errors="coerce")
        frame = frame.dropna(subset=["_date"]).sort_values("_date").tail(8)
        if frame.empty:
            return None

        def values(column: str | None) -> np.ndarray:
            if column is None:
                return np.full(len(frame), np.nan)
            return np.array([number_from_text(value) for value in frame[column]], dtype=float)

        labels = [value.strftime("%y-%m") for value in frame["_date"]]
        return labels, values(revenue_column), values(profit_column), values(revenue_yoy_column), values(profit_yoy_column)

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        bounds = QRectF(self.rect()).adjusted(10, 8, -10, -10)
        painter.fillRect(bounds, PANEL)
        painter.setPen(TEXT)
        painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        painter.drawText(QPointF(bounds.left() + 10, bounds.top() + 20), "财务趋势")
        data = self._series()
        if data is None:
            painter.setPen(MUTED)
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "暂无可绘制的财务趋势数据")
            return
        labels, revenue, profit, revenue_yoy, profit_yoy = data
        plot = QRectF(bounds.left() + 12, bounds.top() + 42, bounds.width() - 24, bounds.height() - 65)
        upper = QRectF(plot.left(), plot.top(), plot.width(), plot.height() * 0.58)
        lower = QRectF(plot.left(), upper.bottom() + 14, plot.width(), plot.height() * 0.32)
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(BLUE)
        painter.drawText(QPointF(upper.left(), upper.top() - 8), "营业收入")
        painter.setPen(PINK)
        painter.drawText(QPointF(upper.left() + 68, upper.top() - 8), "净利润（独立刻度）")
        self._draw_bars(painter, upper, revenue, BLUE)
        self._draw_line(painter, upper, profit, PINK, independent=True)
        self._draw_growth(painter, lower, revenue_yoy, profit_yoy)
        painter.setPen(MUTED)
        spacing = plot.width() / max(len(labels), 1)
        for index, label in enumerate(labels):
            x = plot.left() + (index + 0.5) * spacing
            width = painter.fontMetrics().horizontalAdvance(label)
            painter.drawText(QPointF(x - width / 2, bounds.bottom() - 5), label)

    @staticmethod
    def _finite_max(values: np.ndarray) -> float:
        finite = values[np.isfinite(values)]
        return max(float(np.max(np.abs(finite))) if finite.size else 0.0, 1e-12)

    def _draw_bars(self, painter: QPainter, rect: QRectF, values: np.ndarray, color: QColor) -> None:
        maximum = self._finite_max(values)
        spacing = rect.width() / max(len(values), 1)
        width = max(3.0, spacing * 0.48)
        painter.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.right(), rect.bottom()))
        fill = QColor(color)
        fill.setAlpha(150)
        for index, value in enumerate(values):
            if not np.isfinite(value):
                continue
            height = abs(float(value)) / maximum * rect.height() * 0.88
            x = rect.left() + (index + 0.5) * spacing
            painter.fillRect(QRectF(x - width / 2, rect.bottom() - height, width, height), fill)

    def _draw_line(self, painter: QPainter, rect: QRectF, values: np.ndarray, color: QColor, independent: bool = False) -> None:
        del independent
        finite = values[np.isfinite(values)]
        if not finite.size:
            return
        low, high = float(finite.min()), float(finite.max())
        if math.isclose(low, high):
            low, high = low - 1, high + 1
        spacing = rect.width() / max(len(values), 1)
        path = QPainterPath()
        started = False
        for index, value in enumerate(values):
            if not np.isfinite(value):
                started = False
                continue
            x = rect.left() + (index + 0.5) * spacing
            y = rect.bottom() - (float(value) - low) / (high - low) * rect.height() * 0.78 - rect.height() * 0.08
            point = QPointF(x, y)
            path.lineTo(point) if started else path.moveTo(point)
            started = True
        painter.setPen(QPen(color, 2))
        painter.drawPath(path)

    def _draw_growth(self, painter: QPainter, rect: QRectF, revenue: np.ndarray, profit: np.ndarray) -> None:
        finite = np.concatenate([revenue[np.isfinite(revenue)], profit[np.isfinite(profit)]])
        if not finite.size:
            painter.setPen(MUTED)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "同比增长率暂无")
            return
        low, high = min(float(finite.min()), 0.0), max(float(finite.max()), 0.0)
        padding = max((high - low) * 0.08, 1.0)
        low, high = low - padding, high + padding
        zero_y = rect.bottom() - (0 - low) / (high - low) * rect.height()
        painter.setPen(QPen(GRID, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(rect.left(), zero_y), QPointF(rect.right(), zero_y))
        painter.setPen(BLUE)
        painter.drawText(QPointF(rect.left(), rect.top() + 10), "收入同比")
        painter.setPen(PINK)
        painter.drawText(QPointF(rect.left() + 58, rect.top() + 10), "利润同比")

        def paint(values: np.ndarray, color: QColor) -> None:
            spacing = rect.width() / max(len(values), 1)
            path = QPainterPath()
            started = False
            for index, value in enumerate(values):
                if not np.isfinite(value):
                    started = False
                    continue
                point = QPointF(
                    rect.left() + (index + 0.5) * spacing,
                    rect.bottom() - (float(value) - low) / (high - low) * rect.height(),
                )
                path.lineTo(point) if started else path.moveTo(point)
                started = True
            painter.setPen(QPen(color, 1.6))
            painter.drawPath(path)

        paint(revenue, BLUE)
        paint(profit, PINK)
