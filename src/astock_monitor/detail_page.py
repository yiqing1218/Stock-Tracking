from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

import pandas as pd
from PySide6.QtCore import QDate, QThreadPool, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .analytics_widgets import ChipDistributionWidget, FinancialChartWidget
from .alerts import AlertSettingsWidget
from .chart_widget import MarketChart
from .data_provider import DataProvider, DetailBundle
from .formula_engine import FORMULA_HELP, FormulaEngine, FormulaError
from .indicators import (
    IndicatorDefinition,
    IndicatorSnapshot,
    build_indicator_snapshot,
    calculate_indicators,
    candle_pattern_summary,
    dimension_composites,
    detailed_indicator_description,
    market_regime,
    resample_ohlcv,
)
from .models import CustomIndicator, NewsArticle, Security, SecurityType
from .repository import Repository
from .time_utils import beijing_today
from .ui_common import (
    DOWN_COLOR,
    UP_COLOR,
    MetricCard,
    StatusPill,
    Worker,
    change_color,
    configure_table,
    format_number,
    format_percent,
    section_title,
)


class DetailPage(QWidget):
    back_requested = Signal()
    watchlist_changed = Signal()

    def __init__(
        self,
        repository: Repository,
        provider: DataProvider,
        thread_pool: QThreadPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.provider = provider
        self.thread_pool = thread_pool
        self.security: Security | None = None
        self.bundle: DetailBundle | None = None
        self.indicator_frame = pd.DataFrame()
        self.snapshots: list[IndicatorSnapshot] = []
        self.extended_snapshots: list[IndicatorSnapshot] = []
        self.custom_series: pd.Series | None = None
        self.custom_series_name = "自定义指标"
        self.chart_period = "daily"
        self._load_token = 0
        self._intraday_token = 0
        self._news_token = 0
        self._extras_token = 0
        self._extended_token = 0
        self._detail_running = False
        self._intraday_running = False
        self._news_running = False
        self._extras_running = False
        self._extended_running = False
        self._extended_ready = False
        self._adjustment = "qfq"
        self._active_workers: set[Worker] = set()
        self.indicator_favorites: set[str] = set()
        self.news_articles: list[NewsArticle] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("Section")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        self.back_button = QPushButton("← 返回自选")
        self.back_button.setObjectName("Ghost")
        self.back_button.clicked.connect(self.back_requested.emit)
        header_layout.addWidget(self.back_button)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color:#263B57;")
        header_layout.addWidget(divider)
        identity = QVBoxLayout()
        identity.setSpacing(1)
        self.security_name = QLabel("请选择证券")
        self.security_name.setObjectName("SecurityName")
        self.security_code = QLabel("—")
        self.security_code.setObjectName("Muted")
        identity.addWidget(self.security_name)
        identity.addWidget(self.security_code)
        header_layout.addLayout(identity)
        header_layout.addSpacing(14)
        price_group = QVBoxLayout()
        price_group.setSpacing(0)
        self.price_label = QLabel("—")
        self.price_label.setObjectName("Price")
        self.change_label = QLabel("—")
        self.change_label.setObjectName("Muted")
        price_group.addWidget(self.price_label)
        price_group.addWidget(self.change_label)
        header_layout.addLayout(price_group)
        header_layout.addStretch(1)
        self.loading_label = QLabel("等待加载")
        self.loading_label.setObjectName("Muted")
        header_layout.addWidget(self.loading_label)
        self.watchlist_button = QPushButton("加入自选")
        self.watchlist_button.clicked.connect(self._toggle_watchlist)
        header_layout.addWidget(self.watchlist_button)
        self.refresh_button = QPushButton("刷新详情")
        self.refresh_button.clicked.connect(self._refresh)
        header_layout.addWidget(self.refresh_button)
        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.overview_tab = self._build_overview_tab()
        self.indicators_tab = self._build_indicators_tab()
        self.intraday_tab = self._build_intraday_tab()
        self.fundamentals_tab = self._build_fundamentals_tab()
        self.news_tab = self._build_news_tab()
        self.custom_tab = self._build_custom_tab()
        self.alert_settings_tab = AlertSettingsWidget(self.repository)
        self.tabs.addTab(self.overview_tab, "行情走势")
        self.tabs.addTab(self.intraday_tab, "历史分时")
        self.tabs.addTab(self.indicators_tab, "全部指标")
        self.tabs.addTab(self.fundamentals_tab, "资金、筹码与公司")
        self.tabs.addTab(self.news_tab, "实时资讯")
        self.tabs.addTab(self.custom_tab, "自定义指标")
        self.tabs.addTab(self.alert_settings_tab, "行情提醒设置")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("K线周期"))
        self.period_buttons: dict[str, QPushButton] = {}
        for label, period in (("日K", "daily"), ("周K", "weekly"), ("月K", "monthly")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFixedWidth(58)
            button.clicked.connect(
                lambda checked=False, value=period: self._select_chart_period(value)
            )
            self.period_buttons[period] = button
            toolbar.addWidget(button)
        self.period_buttons["daily"].setChecked(True)
        toolbar.addSpacing(10)
        tip = QLabel("滚轮或＋/－缩放；←/→或键盘方向键平移；双击K线进入分时")
        tip.setObjectName("Tiny")
        toolbar.addWidget(tip)
        toolbar.addStretch(1)
        self.latest_ohlc_label = QLabel("开 —  高 —  低 —  收 —")
        self.latest_ohlc_label.setObjectName("Muted")
        toolbar.addWidget(self.latest_ohlc_label)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        chart_container = QWidget()
        chart_layout = QGridLayout(chart_container)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        self.market_chart = MarketChart()
        self.market_chart.date_activated.connect(self._open_intraday_for_date)
        chart_layout.addWidget(self.market_chart, 0, 0)
        chart_controls = QFrame()
        chart_controls.setObjectName("ChartControls")
        chart_controls_layout = QHBoxLayout(chart_controls)
        chart_controls_layout.setContentsMargins(5, 5, 5, 5)
        chart_controls_layout.setSpacing(4)
        for text, tooltip, callback in (
            ("＋", "放大K线", self.market_chart.zoom_in),
            ("－", "缩小K线", self.market_chart.zoom_out),
            ("←", "向左查看更早K线", self.market_chart.pan_left),
            ("→", "向右查看更新K线", self.market_chart.pan_right),
        ):
            control = QPushButton(text)
            control.setObjectName("ChartControl")
            control.setFixedSize(34, 32)
            control.setToolTip(tooltip)
            control.clicked.connect(callback)
            control.clicked.connect(lambda checked=False: self.market_chart.setFocus())
            chart_controls_layout.addWidget(control)
        chart_layout.addWidget(
            chart_controls,
            0,
            0,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        splitter.addWidget(chart_container)
        insights = QFrame()
        insights.setObjectName("Section")
        insights.setMinimumWidth(315)
        insights.setMaximumWidth(390)
        insight_layout = QVBoxLayout(insights)
        insight_layout.setContentsMargins(12, 12, 12, 12)
        insight_layout.setSpacing(9)
        adjustment_row = QHBoxLayout()
        adjustment_row.addWidget(QLabel("复权方式"))
        self.adjustment_combo = QComboBox()
        self.adjustment_combo.addItem("前复权", "qfq")
        self.adjustment_combo.addItem("不复权", "")
        self.adjustment_combo.addItem("后复权", "hfq")
        self.adjustment_combo.currentIndexChanged.connect(self._change_adjustment)
        adjustment_row.addWidget(self.adjustment_combo, 1)
        insight_layout.addLayout(adjustment_row)
        insight_layout.addWidget(section_title("多维状态", "非单指标信号"))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.regime_card = MetricCard("市场状态")
        self.score_card = MetricCard("综合评分")
        self.volatility_card = MetricCard("20日波动率")
        self.drawdown_card = MetricCard("当前回撤")
        grid.addWidget(self.regime_card, 0, 0)
        grid.addWidget(self.score_card, 0, 1)
        grid.addWidget(self.volatility_card, 1, 0)
        grid.addWidget(self.drawdown_card, 1, 1)
        insight_layout.addLayout(grid)
        narrative_title = QLabel("指标解读")
        narrative_title.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.Bold))
        insight_layout.addWidget(narrative_title)
        self.regime_summary = QLabel("加载后将综合趋势、动量、波动和量能给出状态摘要。")
        self.regime_summary.setWordWrap(True)
        self.regime_summary.setObjectName("Muted")
        self.regime_summary.setMinimumHeight(70)
        insight_layout.addWidget(self.regime_summary)
        self.signal_table = QTableWidget(0, 3)
        self.signal_table.setHorizontalHeaderLabels(["维度", "指标", "状态"])
        configure_table(self.signal_table, alternating=False)
        self.signal_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.signal_table.verticalHeader().setDefaultSectionSize(36)
        self.signal_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.signal_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.signal_table.setMinimumHeight(260)
        insight_layout.addWidget(self.signal_table)
        note = QLabel("状态摘要仅用于把多类指标放在同一语境中，不代表确定性预测。")
        note.setObjectName("Tiny")
        note.setWordWrap(True)
        insight_layout.addWidget(note)
        splitter.addWidget(insights)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)
        return tab

    def _build_intraday_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("交易日"))
        today = beijing_today()
        beijing_qdate = QDate(today.year, today.month, today.day)
        self.intraday_date = QDateEdit(beijing_qdate)
        self.intraday_date.setCalendarPopup(True)
        self.intraday_date.setDisplayFormat("yyyy-MM-dd")
        self.intraday_date.setMaximumDate(beijing_qdate)
        self.intraday_date.setMinimumDate(QDate(1990, 1, 1))
        controls.addWidget(self.intraday_date)
        controls.addWidget(QLabel("周期"))
        self.intraday_period = QComboBox()
        for minutes in ("1", "5", "15", "30", "60"):
            self.intraday_period.addItem(f"{minutes} 分钟", minutes)
        self.intraday_period.setCurrentIndex(0)
        controls.addWidget(self.intraday_period)
        self.intraday_button = QPushButton("加载分时")
        self.intraday_button.setObjectName("Primary")
        self.intraday_button.clicked.connect(self._load_intraday)
        controls.addWidget(self.intraday_button)
        controls.addStretch(1)
        self.intraday_status = QLabel("支持历史分时与北京时间当日实时分时；默认1分钟。")
        self.intraday_status.setObjectName("Muted")
        controls.addWidget(self.intraday_status)
        layout.addLayout(controls)
        self.intraday_chart = MarketChart()
        layout.addWidget(self.intraday_chart, 1)
        note = QLabel(
            "北京时间当日数据每次都会实时刷新；历史分时由公开接口提供，超出接口保存范围时会明确提示无数据。"
        )
        note.setObjectName("Tiny")
        note.setWordWrap(True)
        layout.addWidget(note)
        return tab

    def _build_fundamentals_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        notice = QLabel(
            "口径说明：主力资金按超大单/大单统计，不等于机构真实持仓；筹码为成本区间重建；股东和财务信息来自定期披露。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "background:#201C10; color:#F7D774; border:1px solid #5A4A17; border-radius:7px; padding:9px 12px;"
        )
        layout.addWidget(notice)

        cards = QHBoxLayout()
        cards.setSpacing(9)
        self.main_flow_card = MetricCard("最近主力净流入")
        self.profit_ratio_card = MetricCard("筹码获利比例")
        self.average_cost_card = MetricCard("筹码平均成本")
        self.concentration_card = MetricCard("90%筹码集中度")
        for card in (
            self.main_flow_card,
            self.profit_ratio_card,
            self.average_cost_card,
            self.concentration_card,
        ):
            cards.addWidget(card)
        layout.addLayout(cards)

        self.company_status = QLabel(
            "资金、筹码、股东与企业财务数据仅在相应证券具备公开披露时展示。"
        )
        self.company_status.setObjectName("Muted")
        layout.addWidget(self.company_status)

        navigation = QHBoxLayout()
        navigation.setSpacing(6)
        self.fundamental_navigation = QButtonGroup(self)
        self.fundamental_navigation.setExclusive(True)
        self.fundamental_stack = QStackedWidget()
        for index, label in enumerate(
            (
                "主力资金",
                "筹码分布",
                "主要股东",
                "企业概况",
                "主营业务",
                "财务信息",
                "F10深度",
            )
        ):
            button = QPushButton(label)
            button.setObjectName("SubNavigation")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(
                lambda checked=False, page=index: self._select_fundamental_page(page)
            )
            self.fundamental_navigation.addButton(button, index)
            navigation.addWidget(button)
        navigation.addStretch(1)
        layout.addLayout(navigation)

        flow_page = QWidget()
        flow_layout = QVBoxLayout(flow_page)
        flow_layout.setContentsMargins(0, 0, 0, 0)
        flow_layout.addWidget(section_title("主力资金流", "最近交易日 · 大单统计口径"))
        self.flow_table = QTableWidget(0, 7)
        self.flow_table.setHorizontalHeaderLabels(
            ["日期", "主力净额", "主力占比", "超大单", "大单", "中单", "小单"]
        )
        configure_table(self.flow_table)
        self.flow_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        flow_layout.addWidget(self.flow_table)
        self.fundamental_stack.addWidget(flow_page)

        chip_page = QWidget()
        chip_layout = QVBoxLayout(chip_page)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_splitter = QSplitter(Qt.Orientation.Vertical)
        self.chip_detail_chart = ChipDistributionWidget()
        chip_splitter.addWidget(self.chip_detail_chart)
        self.chip_table = QTableWidget(0, 6)
        self.chip_table.setHorizontalHeaderLabels(
            ["日期", "获利比例", "平均成本", "90%成本区间", "70%成本区间", "90%集中度"]
        )
        configure_table(self.chip_table)
        self.chip_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        chip_splitter.addWidget(self.chip_table)
        chip_splitter.setStretchFactor(0, 2)
        chip_splitter.setStretchFactor(1, 3)
        chip_layout.addWidget(chip_splitter)
        self.fundamental_stack.addWidget(chip_page)

        holder_page = QWidget()
        holder_layout = QVBoxLayout(holder_page)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.holder_title = section_title("主要股东披露", "最新可用报告期")
        holder_layout.addWidget(self.holder_title)
        self.holder_table = QTableWidget(0, 6)
        self.holder_table.setHorizontalHeaderLabels(
            ["股东名称", "持股数量", "持股比例", "股本性质", "截至日期", "公告日期"]
        )
        configure_table(self.holder_table)
        self.holder_table.verticalHeader().setDefaultSectionSize(36)
        holder_header = self.holder_table.horizontalHeader()
        holder_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index in range(1, 6):
            holder_header.setSectionResizeMode(
                index, QHeaderView.ResizeMode.ResizeToContents
            )
        holder_layout.addWidget(self.holder_table)
        self.fundamental_stack.addWidget(holder_page)

        company_page = QWidget()
        company_layout = QVBoxLayout(company_page)
        company_layout.setContentsMargins(0, 0, 0, 0)
        company_layout.addWidget(section_title("企业概况", "东方财富 + 巨潮资讯"))
        self.company_table = QTableWidget(0, 2)
        self.company_table.setHorizontalHeaderLabels(["项目", "内容"])
        configure_table(self.company_table)
        self.company_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.company_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        company_layout.addWidget(self.company_table)
        self.fundamental_stack.addWidget(company_page)

        business_page = QWidget()
        business_layout = QVBoxLayout(business_page)
        business_layout.setContentsMargins(0, 0, 0, 0)
        business_layout.addWidget(section_title("主营业务", "同花顺公开资料"))
        self.business_table = QTableWidget()
        configure_table(self.business_table)
        business_layout.addWidget(self.business_table)
        self.fundamental_stack.addWidget(business_page)

        finance_page = QWidget()
        finance_layout = QVBoxLayout(finance_page)
        finance_layout.setContentsMargins(0, 0, 0, 0)
        finance_layout.addWidget(section_title("财务信息", "报告期主要财务指标"))
        finance_splitter = QSplitter(Qt.Orientation.Vertical)
        self.financial_chart = FinancialChartWidget()
        finance_splitter.addWidget(self.financial_chart)
        self.financial_table = QTableWidget()
        configure_table(self.financial_table)
        finance_splitter.addWidget(self.financial_table)
        finance_splitter.setStretchFactor(0, 2)
        finance_splitter.setStretchFactor(1, 3)
        finance_layout.addWidget(finance_splitter)
        self.fundamental_stack.addWidget(finance_page)

        f10_page = QWidget()
        f10_layout = QVBoxLayout(f10_page)
        f10_layout.setContentsMargins(0, 0, 0, 0)
        f10_layout.addWidget(
            section_title(
                "F10深度分析", "盈利、成长、偿债、现金流与风险线索的披露数据归纳"
            )
        )
        self.f10_notes = QTextEdit()
        self.f10_notes.setReadOnly(True)
        self.f10_notes.setMaximumHeight(170)
        self.f10_table = QTableWidget(0, 4)
        self.f10_table.setHorizontalHeaderLabels(["维度", "代表指标", "最新值", "判断"])
        configure_table(self.f10_table)
        self.f10_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        f10_layout.addWidget(self.f10_notes)
        f10_layout.addWidget(self.f10_table, 1)
        self.fundamental_stack.addWidget(f10_page)

        layout.addWidget(self.fundamental_stack, 1)
        return tab

    def _build_news_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        controls = QHBoxLayout()
        self.news_status = QLabel("自动汇总个股新闻与联网新闻搜索结果。")
        self.news_status.setObjectName("Muted")
        controls.addWidget(self.news_status)
        controls.addStretch(1)
        self.news_refresh_button = QPushButton("刷新资讯")
        self.news_refresh_button.clicked.connect(lambda: self._load_news(force=True))
        controls.addWidget(self.news_refresh_button)
        layout.addLayout(controls)
        self.news_table = QTableWidget(0, 4)
        self.news_table.setHorizontalHeaderLabels(["时间", "来源", "标题", "摘要"])
        configure_table(self.news_table)
        self.news_table.setWordWrap(True)
        news_header = self.news_table.horizontalHeader()
        news_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        news_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        news_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        news_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.news_table.cellDoubleClicked.connect(self._open_news)
        layout.addWidget(self.news_table, 1)
        note = QLabel(
            "双击资讯打开原文。条目来自东方财富个股新闻和联网新闻搜索，标题相同的内容会自动去重。"
        )
        note.setObjectName("Tiny")
        layout.addWidget(note)
        return tab

    def _build_indicators_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        controls = QHBoxLayout()
        self.indicator_search = QLineEdit()
        self.indicator_search.setPlaceholderText("筛选指标名称或说明…")
        self.indicator_search.setClearButtonEnabled(True)
        self.indicator_search.textChanged.connect(self._filter_indicators)
        self.indicator_category = QComboBox()
        self.indicator_category.addItems(
            ["全部分类", "趋势", "动量", "波动", "量能", "情绪", "风险"]
        )
        self.indicator_category.currentIndexChanged.connect(self._filter_indicators)
        self.indicator_count = QLabel("0 项")
        self.indicator_count.setObjectName("Muted")
        controls.addWidget(self.indicator_search, 1)
        controls.addWidget(self.indicator_category)
        controls.addWidget(self.indicator_count)
        self.favorite_only_button = QPushButton("☆ 只看关注")
        self.favorite_only_button.setObjectName("FavoriteFilter")
        self.favorite_only_button.setCheckable(True)
        self.favorite_only_button.toggled.connect(self._toggle_favorite_filter)
        controls.addWidget(self.favorite_only_button)
        layout.addLayout(controls)
        self.indicator_table = QTableWidget(0, 7)
        self.indicator_table.setHorizontalHeaderLabels(
            ["分类", "指标", "来源", "最新值", "状态", "逻辑与含义", "关注"]
        )
        configure_table(self.indicator_table)
        header = self.indicator_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(6, 58)
        layout.addWidget(self.indicator_table, 1)
        note = QLabel(
            "指标采用公开 OHLCV/成交额/换手率数据计算；均线和波动指标具有滞后性，应结合市场环境使用。"
        )
        note.setObjectName("Tiny")
        layout.addWidget(note)
        return tab

    def _build_custom_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)
        left = QFrame()
        left.setObjectName("Section")
        left.setMinimumWidth(250)
        left.setMaximumWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(section_title("我的指标", "本地保存"))
        self.formula_list = QListWidget()
        self.formula_list.currentItemChanged.connect(self._load_formula_item)
        left_layout.addWidget(self.formula_list, 1)
        new_button = QPushButton("新建指标")
        new_button.setObjectName("Primary")
        new_button.clicked.connect(self._new_formula)
        left_layout.addWidget(new_button)
        layout.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        editor = QFrame()
        editor.setObjectName("Section")
        editor_layout = QVBoxLayout(editor)
        editor_layout.addWidget(
            section_title("公式编辑器", "基于当前证券的全部基础行情变量")
        )
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("名称"))
        self.formula_name = QLineEdit()
        self.formula_name.setPlaceholderText("例如：20日量价强度")
        name_row.addWidget(self.formula_name, 1)
        editor_layout.addLayout(name_row)
        classification_row = QHBoxLayout()
        classification_row.addWidget(QLabel("归属维度"))
        self.formula_category = QComboBox()
        self.formula_category.addItems(["趋势", "动量", "波动", "量能", "情绪", "风险"])
        classification_row.addWidget(self.formula_category)
        self.formula_in_library = QCheckBox("加入全部指标库")
        self.formula_in_library.setToolTip(
            "加入后会出现在“全部指标”，并参与对应维度的综合状态"
        )
        classification_row.addWidget(self.formula_in_library)
        classification_row.addStretch(1)
        editor_layout.addLayout(classification_row)
        editor_layout.addWidget(QLabel("公式"))
        self.formula_edit = QTextEdit()
        self.formula_edit.setPlaceholderText("例如：(close / SMA(close, 20) - 1) * 100")
        self.formula_edit.setMaximumHeight(92)
        editor_layout.addWidget(self.formula_edit)
        help_text = "\n".join(
            f"{title}：{value}" for title, value in FORMULA_HELP.items()
        )
        self.formula_help = QLabel(help_text)
        self.formula_help.setObjectName("Tiny")
        self.formula_help.setWordWrap(True)
        editor_layout.addWidget(self.formula_help)
        buttons = QHBoxLayout()
        validate_button = QPushButton("校验公式")
        validate_button.clicked.connect(self._validate_formula)
        save_button = QPushButton("保存")
        save_button.setObjectName("Primary")
        save_button.clicked.connect(self._save_formula)
        calculate_button = QPushButton("计算并绘图")
        calculate_button.clicked.connect(self._calculate_custom)
        self.delete_formula_button = QPushButton("删除")
        self.delete_formula_button.setObjectName("Danger")
        self.delete_formula_button.clicked.connect(self._delete_formula)
        buttons.addWidget(validate_button)
        buttons.addWidget(save_button)
        buttons.addWidget(calculate_button)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_formula_button)
        editor_layout.addLayout(buttons)
        self.formula_status = QLabel("公式只支持白名单变量和函数，不执行任意代码。")
        self.formula_status.setObjectName("Muted")
        editor_layout.addWidget(self.formula_status)
        right_layout.addWidget(editor)
        self.custom_chart = MarketChart()
        self.custom_chart.setMinimumHeight(330)
        right_layout.addWidget(self.custom_chart, 1)
        layout.addWidget(right, 1)
        return tab

    def show_empty_state(self) -> None:
        """Keep the full analysis workspace visible without fabricating a security."""

        self.security = None
        self.alert_settings_tab.set_security(None)
        self.bundle = None
        self.indicator_frame = pd.DataFrame()
        self.snapshots = []
        self.extended_snapshots = []
        self.custom_series = None
        self.news_articles = []
        self._load_token += 1
        self._intraday_token += 1
        self._news_token += 1
        self._extras_token += 1
        self._extended_token += 1
        self._detail_running = False
        self._intraday_running = False
        self._news_running = False
        self._extras_running = False
        self._extended_running = False
        self._extended_ready = False
        self.security_name.setText("尚未选择自选股票")
        self.security_code.setText("请从“自选股票”页面双击一只证券进入")
        self.price_label.setText("—")
        self.change_label.setText("—")
        self.latest_ohlc_label.setText("开 —  高 —  低 —  收 —")
        self.loading_label.setText("分析框架已就绪；未载入任何非自选证券的数据。")
        self.refresh_button.setEnabled(False)
        self.watchlist_button.setEnabled(False)
        self.adjustment_combo.setEnabled(False)
        self.market_chart.clear()
        self.intraday_chart.clear()
        self.custom_chart.clear()
        self.chip_detail_chart.clear()
        self.signal_table.setRowCount(0)
        self.indicator_table.setRowCount(0)
        self.indicator_count.setText("0 项")
        self.flow_table.setRowCount(0)
        self.chip_table.setRowCount(0)
        self.holder_table.setRowCount(0)
        self.company_table.setRowCount(0)
        self.business_table.setRowCount(0)
        self.financial_table.setRowCount(0)
        self.financial_chart.set_data(pd.DataFrame())
        self.news_table.setRowCount(0)
        self.intraday_status.setText("请先从自选股票页面进入一只证券。")
        self.company_status.setText("尚未选择证券，不加载企业与财务数据。")
        for card in (
            self.regime_card,
            self.score_card,
            self.volatility_card,
            self.drawdown_card,
            self.main_flow_card,
            self.profit_ratio_card,
            self.average_cost_card,
            self.concentration_card,
        ):
            card.set_value("—", "等待选择自选证券")
        self.regime_summary.setText(
            "从自选股票进入后，这里会显示加权六维评价与行情解读。"
        )
        self.tabs.setCurrentWidget(self.overview_tab)

    def load_security(self, security: Security) -> None:
        self.security = security
        self.alert_settings_tab.set_security(security)
        self.bundle = None
        self.indicator_frame = pd.DataFrame()
        self.snapshots = []
        self.extended_snapshots = []
        self.custom_series = None
        self._load_token += 1
        self._intraday_token += 1
        self._news_token += 1
        self._extras_token += 1
        self._extended_token += 1
        token = self._load_token
        self._detail_running = True
        self._intraday_running = False
        self._news_running = False
        self._extras_running = False
        self._extended_running = False
        self._extended_ready = False
        self.indicator_favorites = self.repository.list_indicator_favorites()
        self.watchlist_button.setEnabled(True)
        self.intraday_button.setEnabled(True)
        self.news_refresh_button.setEnabled(True)
        self.security_name.setText(security.name)
        self.security_code.setText(
            f"{security.display_code} · {security.security_type.label}"
        )
        self.price_label.setText("—")
        self.change_label.setText("—")
        self.loading_label.setText("正在优先加载行情；资金、公司和扩展指标按需加载…")
        self.refresh_button.setEnabled(False)
        self.adjustment_combo.setEnabled(
            security.security_type is not SecurityType.INDEX
        )
        today = beijing_today()
        beijing_qdate = QDate(today.year, today.month, today.day)
        self.intraday_date.setMaximumDate(beijing_qdate)
        self.intraday_date.setDate(beijing_qdate)
        self.intraday_period.setCurrentIndex(0)
        self._update_watchlist_button()
        self.market_chart.clear()
        self.chip_detail_chart.clear()
        self.intraday_chart.clear()
        self.intraday_status.setText("请选择交易日并点击“加载分时”。")
        self.company_table.setRowCount(0)
        self.business_table.setRowCount(0)
        self.financial_table.setRowCount(0)
        self.financial_chart.set_data(pd.DataFrame())
        self.company_status.setText("正在加载企业概况与财务信息…")
        self.news_table.setRowCount(0)
        self.custom_chart.clear()
        worker = Worker(
            self._load_bundle_and_indicators, security, self._adjustment, token
        )
        worker.signals.result.connect(self._on_bundle_loaded)
        worker.signals.error.connect(
            lambda message, current=token: self._on_load_error(current, message)
        )
        self._start_worker(worker, lambda current=token: self._finish_detail(current))
        self._load_news()

    def _load_bundle_and_indicators(
        self, security: Security, adjustment: str, token: int
    ) -> tuple[int, DetailBundle, pd.DataFrame]:
        bundle = self.provider.get_detail_bundle(
            security, adjustment=adjustment, include_extras=False
        )
        calculated = calculate_indicators(bundle.history, include_extended=False)
        return token, bundle, calculated

    def _on_bundle_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 3:
            return
        token, bundle, calculated = result
        if token != self._load_token or not isinstance(bundle, DetailBundle):
            return
        self.bundle = bundle
        self.indicator_frame = calculated
        self._rebuild_indicator_library()
        warnings = "；".join(bundle.warnings)
        history_source = bundle.sources.get("日线", "未知来源")
        self.loading_label.setText(
            f"已加载 {len(calculated)} 个交易日 · {history_source}"
        )
        self.loading_label.setToolTip(warnings)
        self._update_header()
        self._update_overview()
        self._populate_indicators()
        self._reload_formula_list()
        self._load_detail_extras(token)
        if self.tabs.currentWidget() is self.indicators_tab:
            self._ensure_extended_indicators()

    def _load_detail_extras(self, token: int) -> None:
        if self.security is None or self.bundle is None:
            return
        self._extras_running = True
        self._extras_token = token
        security = self.security
        adjustment = self._adjustment
        worker = Worker(
            self.provider.get_detail_bundle,
            security,
            adjustment,
            True,
        )
        worker.signals.result.connect(
            lambda result, current=token: self._on_detail_extras(current, result)
        )
        worker.signals.error.connect(
            lambda message, current=token: self._on_detail_extras_error(
                current, message
            )
        )
        self._start_worker(
            worker, lambda current=token: self._finish_detail_extras(current)
        )

    def _on_detail_extras(self, token: int, result: object) -> None:
        if token != self._load_token or not isinstance(result, DetailBundle):
            return
        self.bundle = result
        self._populate_capital()
        self._populate_company()
        self._update_charts()

    def _on_detail_extras_error(self, token: int, message: str) -> None:
        if token != self._load_token:
            return
        self.company_status.setText(f"资金与公司资料加载失败：{message}")

    def _finish_detail_extras(self, token: int) -> None:
        if token == self._load_token:
            self._extras_running = False

    def _on_tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.indicators_tab:
            self._ensure_extended_indicators()

    def _ensure_extended_indicators(self) -> None:
        if (
            self.security is None
            or self.bundle is None
            or self._extended_ready
            or self._extended_running
        ):
            return
        self._extended_token += 1
        token = self._extended_token
        security = self.security
        history = self.bundle.history.copy()
        adjustment = self._adjustment
        self._extended_running = True
        self.indicator_count.setText("正在载入扩展指标；首次计算后会使用轻量缓存…")
        worker = Worker(
            self._load_extended_indicator_snapshots,
            token,
            security,
            history,
            adjustment,
        )
        worker.signals.result.connect(self._on_extended_indicators_loaded)
        worker.signals.error.connect(
            lambda message, current=token: self._on_extended_indicators_error(
                current, message
            )
        )
        self._start_worker(
            worker, lambda current=token: self._finish_extended_indicators(current)
        )

    def _load_extended_indicator_snapshots(
        self,
        token: int,
        security: Security,
        history: pd.DataFrame,
        adjustment: str,
    ) -> tuple[int, list[IndicatorSnapshot]]:
        last = history.iloc[-1]
        signature = {
            "version": 4,
            "rows": len(history),
            "date": str(pd.Timestamp(last["date"])),
            "close": float(last["close"]),
            "volume": float(last["volume"]),
            "adjustment": adjustment,
        }
        cache_path = self.provider.cache_dir / (
            f"indicator_snapshots_v4_{security.security_type.value}_{security.code}_{adjustment or 'raw'}.json"
        )
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_signature = cached.get("signature", {})
                same_series = all(
                    cached_signature.get(key) == signature.get(key)
                    for key in ("version", "rows", "date", "adjustment")
                )
                exact_tick = cached_signature == signature
                recent_tick = self.provider._cache_is_fresh(
                    cache_path, timedelta(minutes=10)
                )
                if same_series and (exact_tick or recent_tick):
                    snapshots = [
                        IndicatorSnapshot(
                            IndicatorDefinition(**item["definition"]),
                            item.get("value"),
                            str(item.get("status", "—")),
                        )
                        for item in cached.get("snapshots", [])
                    ]
                    if snapshots:
                        return token, snapshots
            except (OSError, TypeError, ValueError, KeyError):
                pass
        calculated = calculate_indicators(history, include_extended=True)
        snapshots = [
            item
            for item in build_indicator_snapshot(calculated)
            if item.definition.column.startswith("PTA_")
        ]
        payload = {
            "signature": signature,
            "snapshots": [
                {
                    "definition": {
                        "category": item.definition.category,
                        "name": item.definition.name,
                        "column": item.definition.column,
                        "description": item.definition.description,
                        "unit": item.definition.unit,
                        "origin": item.definition.origin,
                        "key": item.definition.key,
                    },
                    "value": item.value,
                    "status": item.status,
                }
                for item in snapshots
            ],
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return token, snapshots

    def _on_extended_indicators_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        token, snapshots = result
        if token != self._extended_token or not isinstance(snapshots, list):
            return
        self.extended_snapshots = snapshots
        self._extended_ready = True
        self._rebuild_indicator_library()
        self._populate_indicators()
        self._update_overview()

    def _on_extended_indicators_error(self, token: int, message: str) -> None:
        if token == self._extended_token:
            self.indicator_count.setText(f"扩展指标加载失败：{message}")

    def _finish_extended_indicators(self, token: int) -> None:
        if token == self._extended_token:
            self._extended_running = False

    def _on_load_error(self, token: int, message: str) -> None:
        if token != self._load_token:
            return
        self.loading_label.setText(f"加载失败：{message}")
        self.regime_summary.setText("无法取得行情数据。请检查网络后点击“刷新详情”。")

    def _finish_detail(self, token: int) -> None:
        if token != self._load_token:
            return
        self._detail_running = False
        self.refresh_button.setEnabled(True)

    def _start_worker(self, worker: Worker, finished) -> None:  # type: ignore[no-untyped-def]
        self._active_workers.add(worker)
        worker.signals.finished.connect(finished)
        worker.signals.finished.connect(
            lambda current=worker: self._active_workers.discard(current)
        )
        self.thread_pool.start(worker)

    def _refresh(self) -> None:
        if self.security is not None:
            self.load_security(self.security)

    def _update_header(self) -> None:
        if self.indicator_frame.empty:
            return
        last = self.indicator_frame.iloc[-1]
        previous_close = (
            float(self.indicator_frame.iloc[-2]["close"])
            if len(self.indicator_frame) > 1
            else float("nan")
        )
        close = float(last["close"])
        change = (
            close - previous_close if math.isfinite(previous_close) else float("nan")
        )
        change_pct = change / previous_close * 100 if previous_close else float("nan")
        color = change_color(change_pct)
        self.price_label.setText(f"{close:.2f}")
        self.price_label.setStyleSheet(f"color:{color};")
        self.change_label.setText(f"{change:+.2f}  {change_pct:+.2f}%")
        self.change_label.setStyleSheet(f"color:{color};")
        self.latest_ohlc_label.setText(
            f"开 {last['open']:.2f}  高 {last['high']:.2f}  低 {last['low']:.2f}  收 {last['close']:.2f}  量 {format_number(last['volume'])}"
        )

    def _select_chart_period(self, period: str) -> None:
        if period not in {"daily", "weekly", "monthly"}:
            return
        self.chart_period = period
        for name, button in self.period_buttons.items():
            button.setChecked(name == period)
        self._update_charts()

    def _change_adjustment(self) -> None:
        adjustment = str(self.adjustment_combo.currentData() or "")
        if adjustment == self._adjustment:
            return
        self._adjustment = adjustment
        if (
            self.security is not None
            and self.security.security_type is not SecurityType.INDEX
        ):
            self.load_security(self.security)

    def _open_intraday_for_date(self, trading_day: object) -> None:
        if self.security is None:
            return
        try:
            selected = pd.Timestamp(trading_day).date()
        except (TypeError, ValueError):
            return
        today = beijing_today()
        if selected > today:
            selected = today
        self.intraday_date.setDate(QDate(selected.year, selected.month, selected.day))
        period_index = self.intraday_period.findData("1")
        self.intraday_period.setCurrentIndex(max(0, period_index))
        self.tabs.setCurrentWidget(self.intraday_tab)
        self._load_intraday()

    def _select_fundamental_page(self, index: int) -> None:
        if 0 <= index < self.fundamental_stack.count():
            self.fundamental_stack.setCurrentIndex(index)

    def _visible_frame(self) -> pd.DataFrame:
        return self.indicator_frame

    def _update_charts(self) -> None:
        if self.bundle is not None:
            market_history = resample_ohlcv(self.bundle.history, self.chart_period)
            market_frame = calculate_indicators(market_history, include_extended=False)
        else:
            market_frame = self._visible_frame()
        markers = (
            self.bundle.corporate_actions if self.bundle is not None else pd.DataFrame()
        )
        self.market_chart.set_data(market_frame, event_markers=markers)
        visible = self._visible_frame()
        if self.custom_series is not None:
            series = self.custom_series.reindex(visible.index)
            self.custom_chart.set_data(visible, series, self.custom_series_name)
        else:
            self.custom_chart.set_data(visible)

    def _update_overview(self) -> None:
        if self.indicator_frame.empty:
            return
        self._update_charts()
        regime = market_regime(self.indicator_frame)
        regime_color = (
            UP_COLOR
            if regime["direction"] == "偏多"
            else DOWN_COLOR
            if regime["direction"] == "偏空"
            else None
        )
        adx = self._last_value("ADX_14")
        self.regime_card.set_value(
            str(regime["regime"]),
            f"ADX {adx:.1f}" if adx is not None else "ADX —",
            regime_color,
        )
        self.score_card.set_value(
            f"{float(regime['score']):.0f}/100", str(regime["direction"]), regime_color
        )
        hv = self._last_value("HV_20")
        self.volatility_card.set_value(
            format_percent(hv, signed=False), "日收益年化标准差"
        )
        drawdown = self._last_value("DRAWDOWN")
        self.drawdown_card.set_value(format_percent(drawdown), "相对历史最高收盘")
        self.regime_summary.setText(
            f"{regime['summary']} 当前K线：{candle_pattern_summary(self.indicator_frame)}。"
            "趋势、位置、量能应共同判断，单一超买/超卖不等于立即反转。"
        )
        composites = dimension_composites(self.snapshots)
        dimensions = ("趋势", "动量", "波动", "量能", "情绪", "风险")
        self.signal_table.setRowCount(len(dimensions))
        for row, category in enumerate(dimensions):
            composite = composites[category]
            self.signal_table.setItem(row, 0, QTableWidgetItem(category))
            self.signal_table.setItem(
                row,
                1,
                QTableWidgetItem(
                    f"综合 {int(composite['count'])} 项 · 权重 {float(composite['weight']):.1f}"
                ),
            )
            pill = StatusPill(
                f"{composite['status']} · {float(composite['score']):.0f}"
            )
            self.signal_table.setCellWidget(row, 2, pill)

    def _populate_indicators(self) -> None:
        self.indicator_table.setRowCount(len(self.snapshots))
        for row, snapshot in enumerate(self.snapshots):
            definition = snapshot.definition
            value = (
                "—"
                if snapshot.value is None
                else f"{snapshot.value:.4f}".rstrip("0").rstrip(".")
            )
            if value != "—" and definition.unit:
                value += definition.unit
            self.indicator_table.setItem(row, 0, QTableWidgetItem(definition.category))
            self.indicator_table.setItem(row, 1, QTableWidgetItem(definition.name))
            self.indicator_table.setItem(row, 2, QTableWidgetItem(definition.origin))
            value_item = QTableWidgetItem(value)
            value_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.indicator_table.setItem(row, 3, value_item)
            self.indicator_table.setCellWidget(row, 4, StatusPill(snapshot.status))
            description = QTableWidgetItem(detailed_indicator_description(definition))
            description.setForeground(QColor("#A8B6C9"))
            self.indicator_table.setItem(row, 5, description)
            identifier = definition.identifier
            favorite = identifier in self.indicator_favorites
            button = QPushButton("★" if favorite else "☆")
            button.setObjectName("FavoriteStar")
            button.setCheckable(True)
            button.setChecked(favorite)
            button.setToolTip("取消关注" if favorite else "关注该指标")
            button.toggled.connect(
                lambda checked=False, key=identifier, control=button: (
                    self._toggle_indicator_favorite(key, checked, control)
                )
            )
            self.indicator_table.setCellWidget(row, 6, button)
        self._filter_indicators()

    def _rebuild_indicator_library(self) -> None:
        if self.indicator_frame.empty:
            self.snapshots = []
            return
        custom_columns = [
            column
            for column in self.indicator_frame
            if str(column).startswith("CUSTOM_")
        ]
        if custom_columns:
            self.indicator_frame = self.indicator_frame.drop(columns=custom_columns)
        definitions: list[IndicatorDefinition] = []
        engine = FormulaEngine(self.indicator_frame)
        for indicator in self.repository.list_custom_indicators():
            if not indicator.in_library or indicator.id is None:
                continue
            try:
                series = engine.evaluate(indicator.formula)
            except FormulaError:
                continue
            column = f"CUSTOM_{indicator.id}"
            self.indicator_frame[column] = series
            definitions.append(
                IndicatorDefinition(
                    indicator.category,
                    indicator.name,
                    column,
                    f"自定义公式：{indicator.formula}",
                    origin="自定义",
                    key=f"custom:{indicator.id}",
                )
            )
        self.indicator_frame.attrs["custom_indicator_definitions"] = definitions
        self.snapshots = [
            *build_indicator_snapshot(self.indicator_frame),
            *self.extended_snapshots,
        ]

    def _filter_indicators(self, *_args) -> None:  # type: ignore[no-untyped-def]
        query = self.indicator_search.text().strip().lower()
        category = self.indicator_category.currentText()
        favorites_only = self.favorite_only_button.isChecked()
        shown = 0
        for row, snapshot in enumerate(self.snapshots):
            definition = snapshot.definition
            matches_query = (
                not query
                or query in definition.name.lower()
                or query in definition.description.lower()
                or query in definition.column.lower()
            )
            matches_category = category == "全部分类" or category == definition.category
            matches_favorite = (
                not favorites_only or definition.identifier in self.indicator_favorites
            )
            visible = matches_query and matches_category and matches_favorite
            self.indicator_table.setRowHidden(row, not visible)
            shown += int(visible)
        self.indicator_count.setText(f"{shown} / {len(self.snapshots)} 项")

    def _toggle_favorite_filter(self, checked: bool) -> None:
        self.favorite_only_button.setText("★ 只看关注" if checked else "☆ 只看关注")
        self._filter_indicators()

    def _toggle_indicator_favorite(
        self, identifier: str, checked: bool, button: QPushButton
    ) -> None:
        self.repository.set_indicator_favorite(identifier, checked)
        if checked:
            self.indicator_favorites.add(identifier)
        else:
            self.indicator_favorites.discard(identifier)
        button.setText("★" if checked else "☆")
        button.setToolTip("取消关注" if checked else "关注该指标")
        self._filter_indicators()

    def _populate_capital(self) -> None:
        if self.bundle is None:
            return
        if self.security and self.security.security_type is not SecurityType.STOCK:
            self.main_flow_card.set_value("不适用", "仅个股提供")
            self.profit_ratio_card.set_value("不适用", "仅个股提供")
            self.average_cost_card.set_value("不适用", "仅个股提供")
            self.concentration_card.set_value("不适用", "仅个股提供")
        self._populate_flow(self.bundle.fund_flow)
        self._populate_chips(self.bundle.chips)
        self._populate_holders(self.bundle.holders)
        flow_source = self.bundle.sources.get("fund_flow", "未知资金源")
        chip_source = self.bundle.sources.get("chips", "未知筹码源")
        self.main_flow_card.subtitle_label.setText(
            f"{self.main_flow_card.subtitle_label.text()}\n{flow_source}"
        )
        for card in (
            self.profit_ratio_card,
            self.average_cost_card,
            self.concentration_card,
        ):
            card.subtitle_label.setText(f"{card.subtitle_label.text()}\n{chip_source}")
        latest_price = None
        if not self.indicator_frame.empty:
            latest_price = self._number(self.indicator_frame.iloc[-1].get("close"))
        self.chip_detail_chart.set_data(self.bundle.chips, latest_price)

    def _populate_flow(self, frame: pd.DataFrame) -> None:
        self.flow_table.setRowCount(0)
        if frame is None or frame.empty:
            if self.security and self.security.security_type is SecurityType.STOCK:
                self.main_flow_card.set_value("暂无数据", "接口未返回")
            return
        data = frame.tail(15).iloc[::-1].reset_index(drop=True)
        columns = [
            "日期",
            "主力净流入-净额",
            "主力净流入-净占比",
            "超大单净流入-净额",
            "大单净流入-净额",
            "中单净流入-净额",
            "小单净流入-净额",
        ]
        self.flow_table.setRowCount(len(data))
        for row_index, row in data.iterrows():
            for column_index, column in enumerate(columns):
                raw = row.get(column)
                if column == "日期":
                    text = str(raw)
                elif "占比" in column:
                    text = format_percent(self._number(raw), signed=True)
                else:
                    text = format_number(self._number(raw))
                item = QTableWidgetItem(text)
                if column_index > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setForeground(QColor(change_color(self._number(raw))))
                self.flow_table.setItem(row_index, column_index, item)
        latest = frame.iloc[-1]
        main = self._number(latest.get("主力净流入-净额"))
        ratio = self._number(latest.get("主力净流入-净占比"))
        self.main_flow_card.set_value(
            format_number(main), format_percent(ratio), change_color(main)
        )

    def _populate_chips(self, frame: pd.DataFrame) -> None:
        self.chip_table.setRowCount(0)
        if frame is None or frame.empty:
            if self.security and self.security.security_type is SecurityType.STOCK:
                self.profit_ratio_card.set_value("暂无数据", "接口未返回")
                self.average_cost_card.set_value("暂无数据", "接口未返回")
                self.concentration_card.set_value("暂无数据", "接口未返回")
            return
        data = frame.tail(15).iloc[::-1].reset_index(drop=True)
        self.chip_table.setRowCount(len(data))
        for row_index, row in data.iterrows():
            values = [
                str(row.get("日期", "")),
                format_percent(
                    (self._number(row.get("获利比例")) or 0) * 100, signed=False
                ),
                format_number(self._number(row.get("平均成本"))),
                f"{format_number(self._number(row.get('90成本-低')))} - {format_number(self._number(row.get('90成本-高')))}",
                f"{format_number(self._number(row.get('70成本-低')))} - {format_number(self._number(row.get('70成本-高')))}",
                format_percent(
                    (self._number(row.get("90集中度")) or 0) * 100, signed=False
                ),
            ]
            for column_index, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column_index > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.chip_table.setItem(row_index, column_index, item)
        latest = frame.iloc[-1]
        profit_ratio = (self._number(latest.get("获利比例")) or 0) * 100
        average_cost = self._number(latest.get("平均成本"))
        concentration = (self._number(latest.get("90集中度")) or 0) * 100
        self.profit_ratio_card.set_value(
            format_percent(profit_ratio, signed=False), "估算获利筹码占比"
        )
        self.average_cost_card.set_value(
            format_number(average_cost), "筹码重建平均成本"
        )
        self.concentration_card.set_value(
            format_percent(concentration, signed=False), "越低通常越集中"
        )

    def _populate_holders(self, frame: pd.DataFrame) -> None:
        self.holder_table.setRowCount(0)
        if frame is None or frame.empty or "截至日期" not in frame:
            return
        data = frame.copy()
        data["_date"] = pd.to_datetime(data["截至日期"], errors="coerce")
        data = data.dropna(subset=["_date"])
        if data.empty:
            return
        latest_date = data["_date"].max()
        latest = data[data["_date"] == latest_date].head(10)
        self.holder_table.setRowCount(len(latest))
        for row_index, (_, row) in enumerate(latest.iterrows()):
            values = [
                str(row.get("股东名称", "")),
                format_number(self._number(row.get("持股数量")), decimals=0),
                format_percent(self._number(row.get("持股比例")), signed=False),
                str(row.get("股本性质", "—")) if pd.notna(row.get("股本性质")) else "—",
                str(row.get("截至日期", "")),
                str(row.get("公告日期", "")),
            ]
            for column_index, text in enumerate(values):
                self.holder_table.setItem(
                    row_index, column_index, QTableWidgetItem(text)
                )

    def _populate_company(self) -> None:
        self.company_table.setRowCount(0)
        self.business_table.setRowCount(0)
        self.financial_table.setRowCount(0)
        if self.bundle is None or self.security is None:
            return
        if self.security.security_type is not SecurityType.STOCK:
            self.company_status.setText(
                "ETF和指数没有单一上市公司的股东、筹码、企业概况与财务报表。"
            )
            self.financial_chart.set_data(pd.DataFrame())
            return
        self.company_status.setText(
            "已汇总资金、筹码、主要股东、企业资料和财务披露；"
            f"资金：{self.bundle.sources.get('fund_flow', '未知')}；"
            f"筹码：{self.bundle.sources.get('chips', '未知')}。"
        )
        pairs: list[tuple[str, str]] = []
        frame = self.bundle.company_info
        if frame is not None and not frame.empty:
            for _, row in frame.iterrows():
                if "item" in frame.columns and "value" in frame.columns:
                    pairs.append(
                        (
                            self._display_value(row.get("item")),
                            self._display_value(row.get("value")),
                        )
                    )
                    continue
                if "项目" in frame.columns and "值" in frame.columns:
                    pairs.append(
                        (
                            self._display_value(row.get("项目")),
                            self._display_value(row.get("值")),
                        )
                    )
                    continue
                for column in frame.columns:
                    value = self._display_value(row.get(column))
                    if value not in {"", "—"}:
                        pairs.append((str(column), value))
        unique_pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pair in pairs:
            if pair not in seen:
                seen.add(pair)
                unique_pairs.append(pair)
        if not unique_pairs:
            unique_pairs.append(("状态", "企业概况接口暂时不可用，请稍后刷新详情。"))
        self.company_table.setRowCount(len(unique_pairs))
        for row_index, (name, value) in enumerate(unique_pairs):
            self.company_table.setItem(row_index, 0, QTableWidgetItem(name))
            self.company_table.setItem(row_index, 1, QTableWidgetItem(value))
        self._fill_frame_table(
            self.business_table, self.bundle.business_info, max_rows=80
        )
        self._fill_frame_table(
            self.financial_table, self.bundle.financials, max_rows=80
        )
        self.financial_chart.set_data(self.bundle.financials)
        self._populate_f10(self.bundle.financials)

    def _populate_f10(self, frame: pd.DataFrame) -> None:
        self.f10_table.setRowCount(0)
        if frame is None or frame.empty:
            self.f10_notes.setPlainText("暂无足够财务披露，无法生成F10深度分析。")
            return
        dimensions = {
            "盈利能力": ("净资产收益率", "ROE", "净利率", "毛利率"),
            "成长能力": ("营业收入同比", "净利润同比", "营收增长", "利润增长"),
            "偿债能力": ("资产负债率", "流动比率", "速动比率"),
            "现金流质量": ("经营活动现金流", "每股经营现金流", "现金流量净额"),
        }
        rows: list[tuple[str, str, str, str]] = []
        for dimension, keywords in dimensions.items():
            match = next(
                (
                    str(column)
                    for column in frame.columns
                    if any(key.lower() in str(column).lower() for key in keywords)
                ),
                None,
            )
            if match is None:
                continue
            values = pd.to_numeric(frame[match], errors="coerce").dropna()
            if values.empty:
                continue
            latest = float(values.iloc[-1])
            if len(values) >= 2:
                judgment = (
                    "改善"
                    if latest > float(values.iloc[-2])
                    else "走弱"
                    if latest < float(values.iloc[-2])
                    else "持平"
                )
            else:
                judgment = "仅一期数据"
            rows.append((dimension, match, format_number(latest), judgment))
        self.f10_table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                self.f10_table.setItem(row_index, column, QTableWidgetItem(value))
        warnings: list[str] = []
        for column in frame.columns:
            name = str(column)
            values = pd.to_numeric(frame[column], errors="coerce").dropna()
            if values.empty:
                continue
            latest = float(values.iloc[-1])
            if "资产负债率" in name and latest > 70:
                warnings.append("资产负债率偏高，需结合行业属性和有息负债继续核查。")
            if ("经营" in name and "现金" in name) and latest < 0:
                warnings.append("最近一期经营现金流为负，需核对利润的现金含量。")
        summary = (
            "；".join(warnings)
            if warnings
            else "未从现有披露指标中发现简单阈值型高风险信号。"
        )
        self.f10_notes.setPlainText(
            "本页使用已加载的财务披露做纵向比较，不额外常驻报表缓存。\n"
            + summary
            + "\n提示：F10归纳用于研究，不能替代审计报告原文。"
        )

    @classmethod
    def _fill_frame_table(
        cls, table: QTableWidget, frame: pd.DataFrame, max_rows: int = 100
    ) -> None:
        table.clear()
        if frame is None or frame.empty:
            table.setRowCount(0)
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["暂无数据"])
            return
        data = frame.copy()
        if len(data) > max_rows:
            data = data.tail(max_rows).iloc[::-1]
        columns = [str(column) for column in data.columns[:24]]
        data = data.iloc[:, : len(columns)]
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(data))
        for row_index, (_, row) in enumerate(data.iterrows()):
            for column_index, column in enumerate(data.columns):
                table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(cls._display_value(row.get(column))),
                )
        header = table.horizontalHeader()
        for index in range(len(columns)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        if columns:
            header.setSectionResizeMode(
                len(columns) - 1, QHeaderView.ResizeMode.Stretch
            )

    @staticmethod
    def _display_value(value: object) -> str:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return "—"
        text = str(value).strip()
        return text if text and text.lower() != "nan" else "—"

    def _load_intraday(self) -> None:
        if self.security is None:
            return
        self._intraday_token += 1
        token = self._intraday_token
        self._intraday_running = True
        security = self.security
        trading_day = self.intraday_date.date().toPython()
        period = str(self.intraday_period.currentData())
        self.intraday_button.setEnabled(False)
        self.intraday_status.setText(
            f"正在加载 {trading_day:%Y-%m-%d} 的 {period} 分钟数据…"
        )
        worker = Worker(self._fetch_intraday, security, trading_day, period, token)
        worker.signals.result.connect(self._on_intraday_loaded)
        worker.signals.error.connect(
            lambda message, current=token: self._on_intraday_error(current, message)
        )
        self._start_worker(worker, lambda current=token: self._finish_intraday(current))

    def _fetch_intraday(
        self,
        security: Security,
        trading_day,
        period: str,
        token: int,  # type: ignore[no-untyped-def]
    ) -> tuple[int, pd.DataFrame, str]:
        frame, source = self.provider.get_intraday(security, trading_day, period)
        return token, calculate_indicators(frame), source

    def _on_intraday_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 3:
            return
        token, frame, source = result
        if token != self._intraday_token or not isinstance(frame, pd.DataFrame):
            return
        trading_day = (
            pd.Timestamp(frame.iloc[-1]["date"]).date() if not frame.empty else None
        )
        reference_price = self._intraday_reference_price(trading_day, frame)
        self.intraday_chart.set_data(
            frame,
            reference_price=reference_price,
            percentage_axis=True,
        )
        day = (
            pd.Timestamp(frame.iloc[-1]["date"]).strftime("%Y-%m-%d")
            if not frame.empty
            else ""
        )
        realtime = " · 北京时间当日实时" if day == f"{beijing_today():%Y-%m-%d}" else ""
        base_text = f" · 零线 {reference_price:.2f}" if reference_price else ""
        self.intraday_status.setText(
            f"已加载 {len(frame)} 个分时点 · {source}{realtime}{base_text}"
        )

    def _intraday_reference_price(
        self,
        trading_day,
        frame: pd.DataFrame,  # type: ignore[no-untyped-def]
    ) -> float | None:
        if trading_day is not None and self.bundle is not None:
            history = self.bundle.history.copy()
            dates = pd.to_datetime(history.get("date"), errors="coerce")
            previous = history[dates.dt.date < trading_day]
            if not previous.empty:
                value = self._number(previous.iloc[-1].get("close"))
                if value and value > 0:
                    return value
        if frame is not None and not frame.empty:
            value = self._number(frame.iloc[0].get("open"))
            if value and value > 0:
                return value
        return None

    def _on_intraday_error(self, token: int, message: str) -> None:
        if token != self._intraday_token:
            return
        self.intraday_chart.clear()
        self.intraday_status.setText(message)

    def _finish_intraday(self, token: int) -> None:
        if token != self._intraday_token:
            return
        self._intraday_running = False
        self.intraday_button.setEnabled(True)

    def _load_news(self, force: bool = False) -> None:
        if self.security is None:
            return
        self._news_token += 1
        token = self._news_token
        self._news_running = True
        security = self.security
        self.news_refresh_button.setEnabled(False)
        self.news_status.setText("正在联网汇总并整理资讯…")
        worker = Worker(self._fetch_news, security, force, token)
        worker.signals.result.connect(self._on_news_loaded)
        worker.signals.error.connect(
            lambda message, current=token: self._on_news_error(current, message)
        )
        self._start_worker(worker, lambda current=token: self._finish_news(current))

    def _fetch_news(
        self, security: Security, force: bool, token: int
    ) -> tuple[int, list[NewsArticle]]:
        return token, self.provider.get_news(security, force=force)

    def _on_news_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        token, articles = result
        if token != self._news_token or not isinstance(articles, list):
            return
        self.news_articles = articles
        self.news_table.setRowCount(len(articles))
        for row_index, article in enumerate(articles):
            values = [
                article.published_at,
                article.source,
                article.title,
                article.summary,
            ]
            for column_index, text in enumerate(values):
                item = QTableWidgetItem(text or "—")
                item.setData(Qt.ItemDataRole.UserRole, article.url)
                item.setToolTip(article.url or article.title)
                self.news_table.setItem(row_index, column_index, item)
        self.news_table.resizeRowsToContents()
        self.news_status.setText(f"已整理 {len(articles)} 条资讯 · 10分钟缓存")

    def _on_news_error(self, token: int, message: str) -> None:
        if token != self._news_token:
            return
        self.news_status.setText(f"资讯加载失败：{message}")

    def _finish_news(self, token: int) -> None:
        if token != self._news_token:
            return
        self._news_running = False
        self.news_refresh_button.setEnabled(True)

    def _open_news(self, row: int, _column: int) -> None:
        item = self.news_table.item(row, 0)
        url = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        parsed = QUrl(url)
        if parsed.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(parsed)

    def _reload_formula_list(self, select_id: int | None = None) -> None:
        self.formula_list.blockSignals(True)
        self.formula_list.clear()
        selected_row = 0
        for row, indicator in enumerate(self.repository.list_custom_indicators()):
            item = QListWidgetItem(indicator.name)
            item.setData(Qt.ItemDataRole.UserRole, indicator.id)
            item.setToolTip(indicator.formula)
            self.formula_list.addItem(item)
            if indicator.id == select_id:
                selected_row = row
        self.formula_list.blockSignals(False)
        if self.formula_list.count():
            self.formula_list.setCurrentRow(selected_row)

    def _load_formula_item(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        if current is None:
            return
        indicator_id = current.data(Qt.ItemDataRole.UserRole)
        indicator = next(
            (
                item
                for item in self.repository.list_custom_indicators()
                if item.id == indicator_id
            ),
            None,
        )
        if indicator is None:
            return
        self.formula_name.setText(indicator.name)
        self.formula_edit.setPlainText(indicator.formula)
        category_index = self.formula_category.findText(indicator.category)
        self.formula_category.setCurrentIndex(max(0, category_index))
        self.formula_in_library.setChecked(indicator.in_library)
        self.formula_name.setProperty("indicator_id", indicator.id)
        self.delete_formula_button.setEnabled(True)

    def _new_formula(self) -> None:
        self.formula_list.clearSelection()
        self.formula_name.clear()
        self.formula_edit.clear()
        self.formula_category.setCurrentText("趋势")
        self.formula_in_library.setChecked(False)
        self.formula_name.setProperty("indicator_id", None)
        self.delete_formula_button.setEnabled(False)
        self.formula_status.setText("请输入名称和公式。")
        self.formula_name.setFocus()

    def _formula_engine(self) -> FormulaEngine:
        if self.indicator_frame.empty:
            raise FormulaError("请先打开并成功加载一只证券")
        return FormulaEngine(self.indicator_frame)

    def _validate_formula(self) -> None:
        try:
            validation = self._formula_engine().validate(
                self.formula_edit.toPlainText()
            )
        except FormulaError as exc:
            self.formula_status.setStyleSheet("color:#FF8297;")
            self.formula_status.setText(f"校验失败：{exc}")
            return
        variables = "、".join(validation.dependencies) or "常量"
        self.formula_status.setStyleSheet("color:#6EE7B7;")
        self.formula_status.setText(f"公式有效。使用变量：{variables}")

    def _save_formula(self) -> None:
        try:
            self._formula_engine().validate(self.formula_edit.toPlainText())
            raw_id = self.formula_name.property("indicator_id")
            indicator = CustomIndicator(
                id=int(raw_id) if raw_id not in (None, "") else None,
                name=self.formula_name.text().strip(),
                formula=self.formula_edit.toPlainText().strip(),
                category=self.formula_category.currentText(),
                in_library=self.formula_in_library.isChecked(),
            )
            saved = self.repository.save_custom_indicator(indicator)
        except (FormulaError, ValueError, sqlite3.IntegrityError) as exc:
            self.formula_status.setStyleSheet("color:#FF8297;")
            self.formula_status.setText(f"保存失败：{exc}")
            return
        self.formula_name.setProperty("indicator_id", saved.id)
        self._reload_formula_list(saved.id)
        self._rebuild_indicator_library()
        self._populate_indicators()
        self._update_overview()
        self.formula_status.setStyleSheet("color:#6EE7B7;")
        self.formula_status.setText("已保存到本地数据库。")

    def _calculate_custom(self) -> None:
        name = self.formula_name.text().strip() or "自定义指标"
        try:
            series = self._formula_engine().evaluate(self.formula_edit.toPlainText())
        except FormulaError as exc:
            self.formula_status.setStyleSheet("color:#FF8297;")
            self.formula_status.setText(f"计算失败：{exc}")
            return
        self.custom_series = series
        self.custom_series_name = name
        latest = series.dropna()
        latest_text = (
            "数据不足" if latest.empty else f"最新值 {float(latest.iloc[-1]):.6g}"
        )
        self.formula_status.setStyleSheet("color:#6EE7B7;")
        self.formula_status.setText(
            f"计算完成：{latest_text}；有效点 {series.notna().sum()} / {len(series)}"
        )
        self._update_charts()

    def _delete_formula(self) -> None:
        raw_id = self.formula_name.property("indicator_id")
        if raw_id in (None, ""):
            return
        answer = QMessageBox.question(self, "删除指标", "确定删除这个自定义指标吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.repository.delete_custom_indicator(int(raw_id))
        self._new_formula()
        self._reload_formula_list()
        self.indicator_favorites = self.repository.list_indicator_favorites()
        self._rebuild_indicator_library()
        self._populate_indicators()
        self._update_overview()

    def _toggle_watchlist(self) -> None:
        if self.security is None:
            return
        if self.repository.contains_security(self.security):
            self.repository.remove_security(self.security)
        else:
            self.repository.add_security(self.security)
        self._update_watchlist_button()
        self.watchlist_changed.emit()

    def _update_watchlist_button(self) -> None:
        if self.security is None:
            return
        contains = self.repository.contains_security(self.security)
        self.watchlist_button.setText("已在自选 · 移除" if contains else "加入自选")
        self.watchlist_button.setObjectName("Danger" if contains else "Primary")
        self.watchlist_button.style().unpolish(self.watchlist_button)
        self.watchlist_button.style().polish(self.watchlist_button)

    def _last_value(self, column: str) -> float | None:
        if column not in self.indicator_frame:
            return None
        values = self.indicator_frame[column].dropna()
        if values.empty:
            return None
        value = float(values.iloc[-1])
        return value if math.isfinite(value) else None

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None
