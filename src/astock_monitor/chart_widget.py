from __future__ import annotations

import math

import numpy as np
import pandas as pd
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


COLORS = {
    "background": QColor("#081321"),
    "panel": QColor("#0B1728"),
    "grid": QColor("#1A2A40"),
    "text": QColor("#DDE7F5"),
    "muted": QColor("#7F91AA"),
    "up": QColor("#FF4D6D"),
    "down": QColor("#20C997"),
    "ma5": QColor("#FBBF24"),
    "ma20": QColor("#38BDF8"),
    "bb": QColor("#A78BFA"),
    "custom": QColor("#F472B6"),
}


class MarketChart(QWidget):
    date_activated = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(430)
        self._frame = pd.DataFrame()
        self._custom: pd.Series | None = None
        self._custom_name = "MACD"
        self._reference_price: float | None = None
        self._percentage_axis = False
        self._visible_count = 120
        self._right_offset = 0
        self._hover_position: QPointF | None = None
        self._last_plot_rect = QRectF()
        self._visible_frame = pd.DataFrame()
        self._event_markers = pd.DataFrame()
        self._overlay_columns = ["SMA_5", "SMA_20", "BB_UPPER", "BB_LOWER"]

    def sizeHint(self) -> QSize:
        return QSize(900, 600)

    def set_data(
        self,
        frame: pd.DataFrame,
        custom: pd.Series | None = None,
        custom_name: str = "MACD",
        reference_price: float | None = None,
        percentage_axis: bool = False,
        event_markers: pd.DataFrame | None = None,
    ) -> None:
        self._frame = frame.copy()
        self._custom = custom.reindex(frame.index) if custom is not None else None
        self._custom_name = custom_name
        self._reference_price = (
            float(reference_price)
            if reference_price is not None
            and np.isfinite(float(reference_price))
            and float(reference_price) > 0
            else None
        )
        self._percentage_axis = bool(percentage_axis and self._reference_price)
        self._event_markers = (
            event_markers.copy() if event_markers is not None else pd.DataFrame()
        )
        self._visible_count = min(max(40, self._visible_count), max(40, len(frame)))
        self._right_offset = 0
        self.update()

    def clear(self) -> None:
        self._frame = pd.DataFrame()
        self._custom = None
        self._reference_price = None
        self._percentage_axis = False
        self._visible_frame = pd.DataFrame()
        self._right_offset = 0
        self.update()

    def set_overlays(self, columns: list[str]) -> None:
        self._overlay_columns = list(dict.fromkeys(columns))
        self.update()

    def zoom_in(self) -> None:
        if self._frame.empty:
            return
        self._visible_count = int(
            np.clip(self._visible_count - 10, 20, max(20, len(self._frame)))
        )
        self._right_offset = min(self._right_offset, self._maximum_offset())
        self.update()

    def zoom_out(self) -> None:
        if self._frame.empty:
            return
        self._visible_count = int(
            np.clip(self._visible_count + 10, 20, max(20, len(self._frame)))
        )
        self._right_offset = min(self._right_offset, self._maximum_offset())
        self.update()

    def _maximum_offset(self) -> int:
        return max(0, len(self._frame) - min(self._visible_count, len(self._frame)))

    def pan_left(self) -> None:
        if self._frame.empty:
            return
        step = max(1, self._visible_count // 5)
        self._right_offset = min(self._maximum_offset(), self._right_offset + step)
        self.update()

    def pan_right(self) -> None:
        if self._frame.empty:
            return
        step = max(1, self._visible_count // 5)
        self._right_offset = max(0, self._right_offset - step)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._frame.empty:
            return
        self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
        event.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() == Qt.Key.Key_Left:
            self.pan_left()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.pan_right()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Plus, Qt.Key.Key_Equal}:
            self.zoom_in()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Minus:
            self.zoom_out()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._hover_position = event.position()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._visible_frame.empty
            and self._last_plot_rect.contains(event.position())
        ):
            spacing = self._last_plot_rect.width() / max(len(self._visible_frame), 1)
            index = int(
                np.clip(
                    (event.position().x() - self._last_plot_rect.left()) / spacing,
                    0,
                    len(self._visible_frame) - 1,
                )
            )
            timestamp = pd.to_datetime(
                self._visible_frame.iloc[index].get("date"), errors="coerce"
            )
            if pd.notna(timestamp):
                self.date_activated.emit(pd.Timestamp(timestamp).date())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._hover_position = None
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), COLORS["background"])
        if self._frame.empty:
            painter.setPen(COLORS["muted"])
            painter.setFont(QFont("Microsoft YaHei UI", 11))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "正在加载行情图表…"
            )
            return

        end = len(self._frame) - self._right_offset
        start = max(0, end - self._visible_count)
        visible = self._frame.iloc[start:end].reset_index(drop=True)
        self._visible_frame = visible
        custom = None
        if self._custom is not None:
            custom = self._custom.iloc[start:end].reset_index(drop=True)

        bounds = QRectF(self.rect()).adjusted(14, 12, -12, -12)
        painter.fillRect(bounds, COLORS["panel"])
        header_height = 34.0
        content_top = bounds.top() + header_height
        content_height = bounds.height() - header_height - 24
        right_axis = 118.0 if self._percentage_axis else 62.0
        price_rect = QRectF(
            bounds.left() + 8,
            content_top,
            bounds.width() - right_axis - 12,
            content_height * 0.62,
        )
        volume_rect = QRectF(
            price_rect.left(),
            price_rect.bottom() + 8,
            price_rect.width(),
            content_height * 0.16,
        )
        indicator_rect = QRectF(
            price_rect.left(),
            volume_rect.bottom() + 8,
            price_rect.width(),
            content_height * 0.20 - 12,
        )
        self._last_plot_rect = price_rect

        self._draw_header(painter, bounds, visible)
        self._draw_grid(painter, price_rect, 5, 4)
        self._draw_grid(painter, volume_rect, 2, 4)
        self._draw_grid(painter, indicator_rect, 2, 4)
        self._draw_price_panel(painter, price_rect, visible)
        self._draw_event_markers(painter, price_rect, visible)
        self._draw_volume_panel(painter, volume_rect, visible)
        self._draw_indicator_panel(painter, indicator_rect, visible, custom)
        self._draw_dates(painter, bounds, indicator_rect, visible)
        self._draw_crosshair(painter, bounds, price_rect, visible)

    def _draw_event_markers(
        self, painter: QPainter, rect: QRectF, frame: pd.DataFrame
    ) -> None:
        if (
            self._event_markers.empty
            or "date" not in self._event_markers
            or "date" not in frame
        ):
            return
        dates = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        spacing = rect.width() / max(len(frame), 1)
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        for _, marker in self._event_markers.iterrows():
            marker_date = pd.to_datetime(marker.get("date"), errors="coerce")
            if pd.isna(marker_date):
                continue
            matches = np.flatnonzero(dates.eq(marker_date.normalize()).to_numpy())
            if not len(matches):
                continue
            x = rect.left() + (int(matches[0]) + 0.5) * spacing
            painter.setPen(QPen(QColor("#F59E0B"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(QColor("#FBBF24"))
            painter.drawText(QPointF(x + 3, rect.top() + 13), "除权/分红")
        painter.restore()

    def _draw_header(
        self, painter: QPainter, bounds: QRectF, frame: pd.DataFrame
    ) -> None:
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        items = [("K线", COLORS["text"])]
        for index, name in enumerate(self._overlay_columns[:6]):
            items.append((name.replace("_", ""), self._overlay_color(index)))
        items.append((self._custom_name, COLORS["custom"]))
        x = bounds.left() + 12
        for text, color in items:
            painter.setPen(color)
            painter.drawText(QPointF(x, bounds.top() + 22), text)
            x += painter.fontMetrics().horizontalAdvance(text) + 20
        last = frame.iloc[-1]
        summary = f"开 {last['open']:.2f}  高 {last['high']:.2f}  低 {last['low']:.2f}  收 {last['close']:.2f}"
        if self._percentage_axis and self._reference_price:
            change_pct = (float(last["close"]) / self._reference_price - 1) * 100
            summary += f"  涨跌 {change_pct:+.2f}%"
        painter.setPen(COLORS["muted"])
        width = painter.fontMetrics().horizontalAdvance(summary)
        painter.drawText(
            QPointF(bounds.right() - width - 12, bounds.top() + 22), summary
        )

    def _draw_grid(
        self, painter: QPainter, rect: QRectF, rows: int, columns: int
    ) -> None:
        painter.save()
        painter.setClipRect(rect)
        painter.setPen(QPen(COLORS["grid"], 1, Qt.PenStyle.DotLine))
        for row in range(rows + 1):
            y = rect.top() + rect.height() * row / rows
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for column in range(columns + 1):
            x = rect.left() + rect.width() * column / columns
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.restore()

    def _draw_price_panel(
        self, painter: QPainter, rect: QRectF, frame: pd.DataFrame
    ) -> None:
        overlay_columns = [
            column for column in self._overlay_columns if column in frame
        ]
        values = [frame["low"].min(), frame["high"].max()]
        for column in overlay_columns:
            values.extend(
                [frame[column].min(skipna=True), frame[column].max(skipna=True)]
            )
        finite = [
            float(value)
            for value in values
            if pd.notna(value) and math.isfinite(float(value))
        ]
        reference = self._reference_price if self._percentage_axis else None
        if reference is not None:
            maximum_deviation = max(
                [abs(value - reference) for value in finite] + [reference * 0.001]
            )
            maximum_deviation *= 1.08
            low_value, high_value = (
                reference - maximum_deviation,
                reference + maximum_deviation,
            )
        else:
            low_value, high_value = min(finite), max(finite)
            padding = max((high_value - low_value) * 0.06, high_value * 0.002)
            low_value -= padding
            high_value += padding

        def map_y(value: float) -> float:
            return (
                rect.bottom()
                - (value - low_value) / max(high_value - low_value, EPS) * rect.height()
            )

        count = len(frame)
        spacing = rect.width() / max(count, 1)
        body_width = max(1.5, min(10.0, spacing * 0.62))
        painter.save()
        painter.setClipRect(rect)
        if reference is not None:
            zero_y = map_y(reference)
            painter.setPen(QPen(QColor("#6B7F99"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(rect.left(), zero_y), QPointF(rect.right(), zero_y)
            )
        for index, row in frame.iterrows():
            x = rect.left() + (index + 0.5) * spacing
            up = row["close"] >= row["open"]
            color = COLORS["up"] if up else COLORS["down"]
            painter.setPen(QPen(color, 1))
            painter.drawLine(
                QPointF(x, map_y(row["high"])), QPointF(x, map_y(row["low"]))
            )
            top = map_y(max(row["open"], row["close"]))
            bottom = map_y(min(row["open"], row["close"]))
            height = max(1.2, bottom - top)
            body = QRectF(x - body_width / 2, top, body_width, height)
            if up:
                painter.fillRect(body, color)
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(body)
        for index, column in enumerate(overlay_columns):
            self._draw_series(
                painter, rect, frame[column], map_y, self._overlay_color(index), 1.2
            )
        painter.restore()
        self._draw_axis_labels(
            painter,
            rect,
            low_value,
            high_value,
            reference_price=reference,
        )

    @staticmethod
    def _overlay_color(index: int) -> QColor:
        palette = (
            "#FBBF24",
            "#38BDF8",
            "#A78BFA",
            "#F472B6",
            "#22D3EE",
            "#FB7185",
            "#84CC16",
            "#F97316",
            "#C084FC",
            "#2DD4BF",
        )
        return QColor(palette[index % len(palette)])

    def _draw_volume_panel(
        self, painter: QPainter, rect: QRectF, frame: pd.DataFrame
    ) -> None:
        maximum = float(frame["volume"].max()) if frame["volume"].notna().any() else 1.0
        maximum = max(maximum, 1.0)
        spacing = rect.width() / max(len(frame), 1)
        width = max(1.0, spacing * 0.58)
        painter.save()
        painter.setClipRect(rect)
        for index, row in frame.iterrows():
            height = float(row["volume"]) / maximum * rect.height()
            x = rect.left() + (index + 0.5) * spacing
            color = QColor(
                COLORS["up"] if row["close"] >= row["open"] else COLORS["down"]
            )
            color.setAlpha(150)
            painter.fillRect(
                QRectF(x - width / 2, rect.bottom() - height, width, height), color
            )
        painter.restore()
        painter.setPen(COLORS["muted"])
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(QPointF(rect.left() + 4, rect.top() + 14), "VOL")
        painter.drawText(
            QPointF(rect.right() + 6, rect.top() + 12), self._compact_number(maximum)
        )

    def _draw_indicator_panel(
        self,
        painter: QPainter,
        rect: QRectF,
        frame: pd.DataFrame,
        custom: pd.Series | None,
    ) -> None:
        if custom is not None:
            series = custom
            name = self._custom_name
            color = COLORS["custom"]
            second = None
        else:
            series = frame.get("MACD_DIF", pd.Series(np.nan, index=frame.index))
            second = frame.get("MACD_DEA")
            name = "MACD"
            color = COLORS["ma20"]
        finite = (
            pd.concat([series, second] if second is not None else [series])
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        if finite.empty:
            painter.setPen(COLORS["muted"])
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "指标数据不足")
            return
        low_value, high_value = float(finite.min()), float(finite.max())
        if low_value > 0:
            low_value = 0
        if high_value < 0:
            high_value = 0
        padding = max((high_value - low_value) * 0.08, 1e-6)
        low_value -= padding
        high_value += padding

        def map_y(value: float) -> float:
            return (
                rect.bottom()
                - (value - low_value) / max(high_value - low_value, EPS) * rect.height()
            )

        painter.save()
        painter.setClipRect(rect)
        zero_y = map_y(0)
        painter.setPen(QPen(COLORS["grid"], 1))
        painter.drawLine(QPointF(rect.left(), zero_y), QPointF(rect.right(), zero_y))
        if custom is None and "MACD_HIST" in frame:
            spacing = rect.width() / max(len(frame), 1)
            width = max(1.0, spacing * 0.55)
            for index, value in enumerate(frame["MACD_HIST"]):
                if pd.isna(value):
                    continue
                x = rect.left() + (index + 0.5) * spacing
                y = map_y(float(value))
                color_bar = QColor(COLORS["up"] if value >= 0 else COLORS["down"])
                color_bar.setAlpha(150)
                painter.fillRect(
                    QRectF(x - width / 2, min(y, zero_y), width, abs(y - zero_y)),
                    color_bar,
                )
        self._draw_series(painter, rect, series, map_y, color, 1.4)
        if second is not None:
            self._draw_series(painter, rect, second, map_y, COLORS["ma5"], 1.2)
        painter.restore()
        painter.setPen(COLORS["muted"])
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(QPointF(rect.left() + 4, rect.top() + 14), name)
        self._draw_axis_labels(painter, rect, low_value, high_value, precision=3)

    def _draw_series(
        self,
        painter: QPainter,
        rect: QRectF,
        series: pd.Series,
        map_y,
        color: QColor,
        width: float,
    ) -> None:
        path = QPainterPath()
        started = False
        spacing = rect.width() / max(len(series), 1)
        for index, value in enumerate(series):
            if pd.isna(value) or not np.isfinite(float(value)):
                started = False
                continue
            point = QPointF(rect.left() + (index + 0.5) * spacing, map_y(float(value)))
            if started:
                path.lineTo(point)
            else:
                path.moveTo(point)
                started = True
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _draw_axis_labels(
        self,
        painter: QPainter,
        rect: QRectF,
        low_value: float,
        high_value: float,
        precision: int = 2,
        reference_price: float | None = None,
    ) -> None:
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(COLORS["muted"])
        for step in range(5):
            ratio = step / 4
            value = high_value - (high_value - low_value) * ratio
            y = rect.top() + rect.height() * ratio + 4
            label = f"{value:.{precision}f}"
            if reference_price is not None and reference_price > 0:
                percent = (value / reference_price - 1) * 100
                label += f"  {percent:+.2f}%"
            painter.drawText(QPointF(rect.right() + 6, y), label)

    def _draw_dates(
        self, painter: QPainter, bounds: QRectF, rect: QRectF, frame: pd.DataFrame
    ) -> None:
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(COLORS["muted"])
        count = len(frame)
        dates = pd.to_datetime(frame.get("date"), errors="coerce")
        intraday = bool(
            not dates.empty
            and dates.dt.date.nunique() == 1
            and dates.dt.time.nunique() > 1
        )
        for step in range(5):
            index = min(count - 1, round((count - 1) * step / 4))
            raw = frame.iloc[index].get("date")
            date_text = (
                pd.Timestamp(raw).strftime("%H:%M" if intraday else "%Y-%m-%d")
                if pd.notna(raw)
                else ""
            )
            x = rect.left() + rect.width() * step / 4
            width = painter.fontMetrics().horizontalAdvance(date_text)
            x = float(
                np.clip(x - width / 2, bounds.left() + 4, bounds.right() - width - 4)
            )
            painter.drawText(QPointF(x, bounds.bottom() - 4), date_text)

    def _draw_crosshair(
        self, painter: QPainter, bounds: QRectF, rect: QRectF, frame: pd.DataFrame
    ) -> None:
        position = self._hover_position
        if position is None or not rect.contains(position):
            return
        spacing = rect.width() / max(len(frame), 1)
        index = int(np.clip((position.x() - rect.left()) / spacing, 0, len(frame) - 1))
        x = rect.left() + (index + 0.5) * spacing
        painter.setPen(QPen(QColor("#5B708D"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, bounds.bottom() - 20))
        painter.drawLine(
            QPointF(rect.left(), position.y()), QPointF(rect.right(), position.y())
        )
        row = frame.iloc[index]
        timestamp = pd.Timestamp(row["date"]) if pd.notna(row.get("date")) else None
        dates = pd.to_datetime(frame.get("date"), errors="coerce")
        intraday = bool(
            not dates.empty
            and dates.dt.date.nunique() == 1
            and dates.dt.time.nunique() > 1
        )
        date_text = (
            timestamp.strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d")
            if timestamp
            else ""
        )
        text = (
            f"{date_text}   开 {row['open']:.2f}  高 {row['high']:.2f}  "
            f"低 {row['low']:.2f}  收 {row['close']:.2f}  量 {self._compact_number(row['volume'])}"
        )
        if self._percentage_axis and self._reference_price:
            percent = (float(row["close"]) / self._reference_price - 1) * 100
            text += f"  涨跌 {percent:+.2f}%"
        metrics = painter.fontMetrics()
        tooltip = QRectF(
            rect.left() + 8, rect.top() + 8, metrics.horizontalAdvance(text) + 20, 28
        )
        painter.fillRect(tooltip, QColor("#13233A"))
        painter.setPen(COLORS["text"])
        painter.drawText(tooltip, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _compact_number(value: float) -> str:
        value = float(value)
        if abs(value) >= 100_000_000:
            return f"{value / 100_000_000:.2f}亿"
        if abs(value) >= 10_000:
            return f"{value / 10_000:.1f}万"
        return f"{value:.0f}"


EPS = 1e-12
