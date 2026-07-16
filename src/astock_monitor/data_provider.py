from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd
import requests

from .models import NewsArticle, Quote, Security, SecurityType
from .historical_store import HistoricalStore
from .time_utils import (
    beijing_now,
    beijing_today,
    cache_age_seconds,
    latest_completed_market_day,
)


COMMON_INDICES = [
    Security("000001", "上证指数", SecurityType.INDEX, "sh"),
    Security("000010", "上证180", SecurityType.INDEX, "sh"),
    Security("000009", "上证380", SecurityType.INDEX, "sh"),
    Security("000015", "上证红利", SecurityType.INDEX, "sh"),
    Security("000016", "上证50", SecurityType.INDEX, "sh"),
    Security("000300", "沪深300", SecurityType.INDEX, "csi"),
    Security("000905", "中证500", SecurityType.INDEX, "csi"),
    Security("000852", "中证1000", SecurityType.INDEX, "csi"),
    Security("000985", "中证全指", SecurityType.INDEX, "csi"),
    Security("000922", "中证红利", SecurityType.INDEX, "csi"),
    Security("930050", "中证A50", SecurityType.INDEX, "csi"),
    Security("932000", "中证A500", SecurityType.INDEX, "csi"),
    Security("399001", "深证成指", SecurityType.INDEX, "sz"),
    Security("399005", "中小100", SecurityType.INDEX, "sz"),
    Security("399006", "创业板指", SecurityType.INDEX, "sz"),
    Security("399330", "深证100", SecurityType.INDEX, "sz"),
    Security("399673", "创业板50", SecurityType.INDEX, "sz"),
    Security("000688", "科创50", SecurityType.INDEX, "sh"),
    Security("000689", "科创100", SecurityType.INDEX, "sh"),
    Security("899050", "北证50", SecurityType.INDEX, "bj"),
]

COMMON_ETFS = [
    Security("510300", "沪深300ETF", SecurityType.ETF, "sh"),
    Security("510050", "上证50ETF", SecurityType.ETF, "sh"),
    Security("510500", "中证500ETF", SecurityType.ETF, "sh"),
    Security("512100", "中证1000ETF", SecurityType.ETF, "sh"),
    Security("159915", "创业板ETF", SecurityType.ETF, "sz"),
    Security("588000", "科创50ETF", SecurityType.ETF, "sh"),
]

MARKET_DASHBOARD_INDICES = [
    Security("000001", "上证指数", SecurityType.INDEX, "sh"),
    Security("399001", "深证成指", SecurityType.INDEX, "sz"),
    Security("399006", "创业板指", SecurityType.INDEX, "sz"),
    Security("000300", "沪深300", SecurityType.INDEX, "csi"),
    Security("000016", "上证50", SecurityType.INDEX, "sh"),
    Security("000905", "中证500", SecurityType.INDEX, "csi"),
    Security("000688", "科创50", SecurityType.INDEX, "sh"),
    Security("899050", "北证50", SecurityType.INDEX, "bj"),
]

INDEX_GROUPS = ("沪深重要指数", "上证系列指数", "深证系列指数", "中证系列指数")


def infer_market(code: str, security_type: SecurityType) -> str:
    code = str(code).zfill(6)
    if security_type is SecurityType.ETF:
        return "sh" if code.startswith(("5", "51", "56", "58")) else "sz"
    if security_type is SecurityType.INDEX:
        if code.startswith("399"):
            return "sz"
        if code.startswith("899"):
            return "bj"
        if code in {"000300", "000905", "000852", "000985"}:
            return "csi"
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def _as_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _row_float(row: pd.Series, *names: str) -> float | None:
    for name in names:
        if name in row.index:
            value = _as_float(row[name])
            if value is not None:
                return value
    return None


@dataclass(slots=True)
class DetailBundle:
    security: Security
    history: pd.DataFrame
    fund_flow: pd.DataFrame = field(default_factory=pd.DataFrame)
    chips: pd.DataFrame = field(default_factory=pd.DataFrame)
    holders: pd.DataFrame = field(default_factory=pd.DataFrame)
    company_info: pd.DataFrame = field(default_factory=pd.DataFrame)
    business_info: pd.DataFrame = field(default_factory=pd.DataFrame)
    financials: pd.DataFrame = field(default_factory=pd.DataFrame)
    balance_sheet: pd.DataFrame = field(default_factory=pd.DataFrame)
    profit_sheet: pd.DataFrame = field(default_factory=pd.DataFrame)
    cash_flow_sheet: pd.DataFrame = field(default_factory=pd.DataFrame)
    corporate_actions: pd.DataFrame = field(default_factory=pd.DataFrame)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MarketDashboardBundle:
    quotes: dict[str, Quote] = field(default_factory=dict)
    breadth: dict[str, float | int] = field(default_factory=dict)
    gainers: pd.DataFrame = field(default_factory=pd.DataFrame)
    losers: pd.DataFrame = field(default_factory=pd.DataFrame)
    boards: pd.DataFrame = field(default_factory=pd.DataFrame)
    sectors: pd.DataFrame = field(default_factory=pd.DataFrame)  # compatibility alias
    trade_date: date | None = None
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class DataProvider:
    """AkShare 数据适配层，并为网络波动保留最近一次成功缓存。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_sources: dict[str, str] = {}
        self.historical_store: HistoricalStore | None = None

    def attach_store(self, store: HistoricalStore) -> None:
        """Use SQLite as the durable source of truth; files remain short-lived fallback."""

        self.historical_store = store

    def load_universe(self, force: bool = False) -> list[Security]:
        cache_path = self.cache_dir / "security_universe.json"
        # 启动时始终优先读完整本地目录，避免上游超时导致搜索临时只剩内置的少数证券。
        if not force and cache_path.exists():
            try:
                values = json.loads(cache_path.read_text(encoding="utf-8"))
                cached = {
                    item.key: item
                    for item in (Security.from_dict(value) for value in values)
                }
                for security in [*COMMON_INDICES, *COMMON_ETFS]:
                    cached[security.key] = security
                return sorted(
                    cached.values(),
                    key=lambda item: (item.security_type.value, item.code, item.name),
                )
            except (OSError, ValueError, KeyError):
                pass

        universe: dict[str, Security] = {}
        for security in [*COMMON_INDICES, *COMMON_ETFS]:
            universe[security.key] = security

        errors: list[Exception] = []
        for loader in (
            self._load_stock_universe,
            self._load_etf_universe,
            self._load_index_universe,
        ):
            try:
                for security in loader():
                    universe[security.key] = security
            except Exception as exc:  # 数据源可能临时限流，保留其余类别
                errors.append(exc)

        values = sorted(
            universe.values(),
            key=lambda item: (item.security_type.value, item.code, item.name),
        )
        if (
            len(values) <= len(COMMON_INDICES) + len(COMMON_ETFS)
            and cache_path.exists()
        ):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return [Security.from_dict(value) for value in cached]
            except (OSError, ValueError, KeyError):
                pass
        cache_path.write_text(
            json.dumps([item.to_dict() for item in values], ensure_ascii=False),
            encoding="utf-8",
        )
        return values

    def _load_stock_universe(self) -> list[Security]:
        frame = ak.stock_info_a_code_name()
        result: list[Security] = []
        for row in frame.itertuples(index=False):
            code = str(getattr(row, "code")).zfill(6)
            name = str(getattr(row, "name"))
            result.append(
                Security(
                    code,
                    name,
                    SecurityType.STOCK,
                    infer_market(code, SecurityType.STOCK),
                )
            )
        return result

    def _load_etf_universe(self) -> list[Security]:
        frame = ak.fund_etf_spot_em()
        result: list[Security] = []
        for _, row in frame.iterrows():
            code = str(row.get("代码", "")).zfill(6)
            name = str(row.get("名称", ""))
            if re.fullmatch(r"\d{6}", code) and name:
                result.append(
                    Security(
                        code,
                        name,
                        SecurityType.ETF,
                        infer_market(code, SecurityType.ETF),
                    )
                )
        return result

    def _load_index_universe(self) -> list[Security]:
        result: dict[str, Security] = {item.key: item for item in COMMON_INDICES}
        for group in INDEX_GROUPS:
            frame = ak.stock_zh_index_spot_em(symbol=group)
            for _, row in frame.iterrows():
                code = str(row.get("代码", "")).zfill(6)
                name = str(row.get("名称", ""))
                if re.fullmatch(r"\d{6}", code) and name:
                    security = Security(
                        code,
                        name,
                        SecurityType.INDEX,
                        infer_market(code, SecurityType.INDEX),
                    )
                    result[security.key] = security
        return list(result.values())

    def refresh_quotes(self, securities: list[Security]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        # 单证券并发请求比下载全市场分页快得多；每个请求有硬超时，避免监看页长时间空白。
        with ThreadPoolExecutor(
            max_workers=min(8, max(1, len(securities)))
        ) as executor:
            future_map = {
                executor.submit(self._fetch_direct_quote, security): security
                for security in securities
            }
            for future in as_completed(future_map):
                security = future_map[future]
                try:
                    quotes[security.key] = future.result()
                except Exception:
                    continue

        for security in securities:
            if security.key in quotes:
                continue
            candidates = (
                self.cache_dir
                / f"history_{security.security_type.value}_{security.code}_qfq.csv",
                self.cache_dir
                / f"history_{security.security_type.value}_{security.code}.csv",
            )
            path = next((item for item in candidates if item.exists()), candidates[0])
            history = self._read_frame(path) if path.exists() else pd.DataFrame()
            if not history.empty:
                quotes[security.key] = self._quote_from_history(security, history)
            else:
                quotes[security.key] = Quote(security=security)
        return quotes

    def refresh_quotes_efficient(self, securities: list[Security]) -> dict[str, Quote]:
        """Use one market snapshot for large alert targets, direct quotes for small sets."""
        if len(securities) <= 80:
            return self.refresh_quotes(securities)
        by_code = {item.code: item for item in securities}
        types = {item.security_type for item in securities}
        try:
            if types == {SecurityType.ETF}:
                frame = ak.fund_etf_spot_em()
                source = "AkShare·东方财富ETF实时快照"
            else:
                frame = self._load_extra_with_cache(
                    "alert_market_snapshot",
                    beijing_today().strftime("%Y%m%d"),
                    self._load_market_spot,
                    timedelta(seconds=20),
                )
                source = str(frame.attrs.get("source", "全A实时快照"))
            code_column = next(
                (
                    column
                    for column in ("代码", "基金代码", "证券代码")
                    if column in frame
                ),
                None,
            )
            if code_column is None:
                raise RuntimeError("快照缺少证券代码")
            result: dict[str, Quote] = {}
            for _, row in frame.iterrows():
                code = self._normalized_code(row.get(code_column))
                security = by_code.get(code)
                if security is None:
                    continue
                quote = self._quote_from_spot_row(security, row)
                quote.extra["source"] = source
                result[security.key] = quote
            return result
        except Exception:
            # A failed market-wide request must not fan out into thousands of HTTP calls.
            return {item.key: Quote(security=item) for item in securities}

    def get_market_dashboard(self) -> MarketDashboardBundle:
        """Load index snapshots, A-share breadth and leading industries."""

        bundle = MarketDashboardBundle()
        bundle.quotes = self.refresh_quotes(MARKET_DASHBOARD_INDICES)
        bundle.sources["indices"] = "东方财富/腾讯/新浪实时行情自动回退"

        jobs = {
            "breadth": (
                "market_breadth",
                self._load_market_spot,
                timedelta(seconds=45),
            ),
            "boards": (
                "market_boards",
                self._load_all_boards,
                timedelta(minutes=3),
            ),
        }
        frames: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    self._load_extra_with_cache,
                    cache_name,
                    "all",
                    loader,
                    max_age,
                ): name
                for name, (cache_name, loader, max_age) in jobs.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    frames[name] = future.result()
                except Exception as exc:
                    bundle.warnings.append(f"{name}: {exc}")

        spot = frames.get("breadth", pd.DataFrame())
        if not spot.empty:
            change_column = next(
                (column for column in ("涨跌幅", "涨跌幅%") if column in spot),
                None,
            )
            amount_column = next(
                (column for column in ("成交额", "成交金额") if column in spot),
                None,
            )
            code_column = next(
                (column for column in ("代码", "股票代码") if column in spot),
                None,
            )
            name_column = next(
                (column for column in ("名称", "股票简称") if column in spot),
                None,
            )
            price_column = next(
                (column for column in ("最新价", "最新") if column in spot),
                None,
            )
            previous_column = next(
                (column for column in ("昨收", "前收盘") if column in spot),
                None,
            )
            high_column = next(
                (column for column in ("最高", "最高价") if column in spot),
                None,
            )
            timestamp_column = next(
                (column for column in ("更新时间戳", "时间戳") if column in spot),
                None,
            )
            if timestamp_column:
                timestamps = pd.to_datetime(
                    pd.to_numeric(spot[timestamp_column], errors="coerce"),
                    unit="s",
                    utc=True,
                    errors="coerce",
                ).dt.tz_convert("Asia/Shanghai")
                valid_dates = timestamps.dropna()
                if not valid_dates.empty:
                    bundle.trade_date = valid_dates.dt.date.max()
                    spot = spot[timestamps.dt.date == bundle.trade_date].copy()
            if change_column and price_column:
                prices = pd.to_numeric(spot[price_column], errors="coerce")
                previous = (
                    pd.to_numeric(spot[previous_column], errors="coerce")
                    if previous_column
                    else pd.Series(np.nan, index=spot.index)
                )
                reported_changes = pd.to_numeric(spot[change_column], errors="coerce")
                if not previous_column:
                    previous = prices / (1 + reported_changes / 100)
                calculated_changes = (prices / previous - 1) * 100
                changes = calculated_changes.where(previous > 0, reported_changes)
                active = prices.gt(0) & changes.notna()
                spot = spot[active].copy()
                prices = prices[active]
                previous = previous[active]
                changes = changes[active]
                valid = changes.dropna()
                amounts = (
                    pd.to_numeric(spot[amount_column], errors="coerce").fillna(0)
                    if amount_column
                    else pd.Series(0.0, index=spot.index)
                )
                codes = (
                    spot[code_column]
                    .astype(str)
                    .str.extract(r"(\d+)", expand=False)
                    .str.zfill(6)
                    if code_column
                    else pd.Series("", index=spot.index)
                )
                names = (
                    spot[name_column].astype(str)
                    if name_column
                    else pd.Series("", index=spot.index)
                )
                limit_up = 0
                limit_down = 0
                broken_limit = 0
                highs = (
                    pd.to_numeric(spot[high_column], errors="coerce")
                    if high_column
                    else pd.Series(np.nan, index=spot.index)
                )
                for row_index in spot.index:
                    prev = _as_float(previous.get(row_index))
                    price = _as_float(prices.get(row_index))
                    if not prev or not price:
                        continue
                    name = str(names.get(row_index, ""))
                    code = str(codes.get(row_index, ""))
                    if self._is_unlimited_new_listing(name):
                        continue
                    rate = self._a_share_limit_rate(code, name)
                    upper = self._rounded_limit_price(prev, 1 + rate)
                    lower = self._rounded_limit_price(prev, 1 - rate)
                    limit_up += int(price >= upper - 0.005)
                    limit_down += int(price <= lower + 0.005)
                    high = _as_float(highs.get(row_index))
                    broken_limit += int(
                        high is not None
                        and high >= upper - 0.005
                        and price < upper - 0.005
                    )
                bundle.breadth = {
                    "up": int((valid > 0).sum()),
                    "down": int((valid < 0).sum()),
                    "flat": int((valid == 0).sum()),
                    "limit_up": limit_up,
                    "limit_down": limit_down,
                    "broken_limit": broken_limit,
                    "median_change": float(valid.median()) if not valid.empty else 0.0,
                    "equal_weight_return": float(valid.mean()) if not valid.empty else 0.0,
                    "market_volatility": float(valid.std(ddof=0)) if not valid.empty else 0.0,
                    "amount": float(amounts.sum()) if not amounts.empty else 0.0,
                }
                if code_column and name_column and price_column:
                    movers = pd.DataFrame(
                        {
                            "代码": codes,
                            "名称": names,
                            "最新价": prices,
                            "涨跌幅": changes,
                            "成交额": amounts,
                        }
                    ).dropna(subset=["涨跌幅", "最新价"])
                    movers = movers[(movers["成交额"] > 0) & (movers["最新价"] > 0)]
                    bundle.gainers = movers.nlargest(25, "涨跌幅").reset_index(drop=True)
                    bundle.losers = movers.nsmallest(25, "涨跌幅").reset_index(drop=True)
            bundle.sources["breadth"] = str(
                spot.attrs.get("source", "AkShare·东方财富A股快照/本地缓存")
            )

        if bundle.trade_date is not None and bundle.breadth:
            trade_date_text = bundle.trade_date.strftime("%Y%m%d")
            try:
                limit_pool = self._load_extra_with_cache(
                    "market_limit_pool",
                    trade_date_text,
                    lambda: ak.stock_zt_pool_em(date=trade_date_text),
                    timedelta(minutes=3),
                )
                streak_column = next(
                    (
                        column
                        for column in ("连板数", "连板统计", "涨停统计")
                        if column in limit_pool
                    ),
                    None,
                )
                if streak_column and not limit_pool.empty:
                    streaks = (
                        limit_pool[streak_column]
                        .astype(str)
                        .str.extract(r"(\d+)", expand=False)
                    )
                    values = pd.to_numeric(streaks, errors="coerce").dropna()
                    if not values.empty:
                        bundle.breadth["max_limit_streak"] = int(values.max())
                bundle.sources["limit_pool"] = "AkShare·东方财富涨停池"
            except Exception as exc:
                bundle.warnings.append(f"涨停生态: {exc}")

        boards = frames.get("boards", pd.DataFrame())
        if not boards.empty:
            boards = boards.copy()
            board_timestamp = next(
                (name for name in ("更新时间戳", "时间戳") if name in boards), None
            )
            if board_timestamp:
                timestamps = pd.to_datetime(
                    pd.to_numeric(boards[board_timestamp], errors="coerce"),
                    unit="s",
                    utc=True,
                    errors="coerce",
                ).dt.tz_convert("Asia/Shanghai")
                board_date = bundle.trade_date or (
                    timestamps.dropna().dt.date.max()
                    if not timestamps.dropna().empty
                    else None
                )
                if board_date:
                    boards = boards[timestamps.dt.date == board_date].copy()
            boards["涨跌幅"] = pd.to_numeric(boards.get("涨跌幅"), errors="coerce")
            boards = boards.dropna(subset=["涨跌幅"]).sort_values(
                "涨跌幅", ascending=False
            )
            bundle.boards = boards.reset_index(drop=True)
            bundle.sectors = bundle.boards.copy()
            if "行业" not in bundle.sectors and "板块名称" in bundle.sectors:
                bundle.sectors["行业"] = bundle.sectors["板块名称"]
            bundle.sources["boards"] = str(
                boards.attrs.get("source", "东方财富行业/概念板块/本地缓存")
            )
        return bundle

    @staticmethod
    def _a_share_limit_rate(code: str, name: str) -> float:
        if "ST" in name.upper():
            return 0.05
        if code.startswith(("300", "301", "688", "689")):
            return 0.20
        if code.startswith(("4", "8", "92")):
            return 0.30
        return 0.10

    @staticmethod
    def _is_unlimited_new_listing(name: str) -> bool:
        normalized = name.upper().strip()
        return normalized.startswith(("N", "C"))

    @staticmethod
    def _rounded_limit_price(previous_close: float, multiplier: float) -> float:
        value = Decimal(str(previous_close)) * Decimal(str(multiplier))
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _eastmoney_clist(
        url: str, *, fs: str, fields: str, page_size: int
    ) -> pd.DataFrame:
        response = requests.get(
            url,
            params={
                "pn": "1",
                "pz": str(page_size),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fs": fs,
                "fields": fields,
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
            timeout=8,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        values = data.get("diff")
        if not isinstance(values, list) or not values:
            raise RuntimeError("东方财富列表接口未返回数据")
        return pd.DataFrame(values)

    def _load_market_spot(self) -> pd.DataFrame:
        errors: list[str] = []
        try:
            frame = self._load_market_spot_tencent()
            frame.attrs["source"] = "腾讯全A分批实时快照"
            return frame
        except Exception as exc:
            errors.append(f"腾讯分批行情: {exc}")
        try:
            raw = self._eastmoney_clist(
                "https://82.push2.eastmoney.com/api/qt/clist/get",
                fs="m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                fields="f12,f14,f2,f3,f6,f13,f15,f16,f17,f18,f124",
                page_size=6000,
            )
            frame = raw.rename(
                columns={
                    "f12": "代码",
                    "f14": "名称",
                    "f2": "最新价",
                    "f3": "涨跌幅",
                    "f6": "成交额",
                    "f13": "市场编号",
                    "f15": "最高",
                    "f16": "最低",
                    "f17": "今开",
                    "f18": "昨收",
                    "f124": "更新时间戳",
                }
            )
            frame.attrs["source"] = "东方财富全A快照直连"
            return frame
        except Exception as exc:
            errors.append(f"东方财富直连: {exc}")
        try:
            frame = ak.stock_zh_a_spot_em()
            frame = frame.copy()
            frame.attrs["source"] = "AkShare·东方财富A股实时快照"
            return frame
        except Exception as exc:
            errors.append(f"AkShare: {exc}")
        raise RuntimeError("；".join(errors))

    def _load_market_spot_tencent(self) -> pd.DataFrame:
        universe = [
            item
            for item in self.load_universe()
            if item.security_type is SecurityType.STOCK
        ]
        if len(universe) < 1000:
            raise RuntimeError("本地A股证券目录不完整")
        batches = [
            universe[index : index + 120] for index in range(0, len(universe), 120)
        ]

        def load_batch(batch: list[Security]) -> list[dict[str, object]]:
            symbols = ",".join(self._market_symbol(item) for item in batch)
            response = requests.get(
                "https://qt.gtimg.cn/q=" + symbols,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=8,
            )
            response.raise_for_status()
            response.encoding = "gbk"
            rows: list[dict[str, object]] = []
            for match in re.finditer(r'v_([^=]+)="([^"]*)"', response.text):
                values = match.group(2).split("~")
                if len(values) < 38 or not re.fullmatch(r"\d{6}", values[2] or ""):
                    continue
                timestamp = values[30] if len(values) > 30 else ""
                unix_time: float | None = None
                if re.fullmatch(r"\d{14}", timestamp or ""):
                    unix_time = (
                        datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                        .timestamp()
                    )
                rows.append(
                    {
                        "代码": values[2],
                        "名称": values[1],
                        "最新价": _as_float(values[3]),
                        "昨收": _as_float(values[4]),
                        "今开": _as_float(values[5]),
                        "涨跌幅": _as_float(values[32]),
                        "最高": _as_float(values[33]),
                        "最低": _as_float(values[34]),
                        "成交额": (_as_float(values[37]) or 0) * 10_000,
                        "更新时间戳": unix_time,
                    }
                )
            return rows

        rows: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(load_batch, batch) for batch in batches]
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception:
                    continue
        if len(rows) < max(1000, int(len(universe) * 0.7)):
            raise RuntimeError(f"仅取得 {len(rows)}/{len(universe)} 只股票")
        return pd.DataFrame(rows)

    def _load_all_boards(self) -> pd.DataFrame:
        errors: list[str] = []
        frames: list[pd.DataFrame] = []
        for board_type, host, filter_text in (
            (
                "行业",
                "https://17.push2.eastmoney.com/api/qt/clist/get",
                "m:90 t:2 f:!50",
            ),
            (
                "概念",
                "https://79.push2.eastmoney.com/api/qt/clist/get",
                "m:90 t:3 f:!50",
            ),
        ):
            try:
                raw = self._eastmoney_clist(
                    host,
                    fs=filter_text,
                    fields="f12,f14,f2,f3,f8,f20,f104,f105,f124",
                    page_size=1000,
                )
                frame = raw.rename(
                    columns={
                        "f12": "板块代码",
                        "f14": "板块名称",
                        "f2": "最新价",
                        "f3": "涨跌幅",
                        "f8": "换手率",
                        "f20": "总市值",
                        "f104": "上涨家数",
                        "f105": "下跌家数",
                        "f124": "更新时间戳",
                    }
                )
                frame.insert(0, "类型", board_type)
                frames.append(frame)
            except Exception as exc:
                errors.append(f"东方财富{board_type}板块直连: {exc}")
        if frames:
            result = pd.concat(frames, ignore_index=True)
            result.attrs["source"] = "东方财富行业+概念板块直连"
            return result
        try:
            industry = ak.stock_board_industry_summary_ths().copy()
            industry = industry.rename(
                columns={"板块": "板块名称", "均价": "最新价", "总成交额": "成交额"}
            )
            industry.insert(0, "类型", "行业")
            industry["更新时间戳"] = beijing_now().timestamp()
            industry.attrs["source"] = "AkShare·同花顺行业板块（概念源不可用时回退）"
            return industry
        except Exception as exc:
            errors.append(f"同花顺行业: {exc}")
        try:
            industry = ak.stock_board_industry_name_em().copy()
            concept = ak.stock_board_concept_name_em().copy()
            industry.insert(0, "类型", "行业")
            concept.insert(0, "类型", "概念")
            industry["更新时间戳"] = beijing_now().timestamp()
            concept["更新时间戳"] = beijing_now().timestamp()
            frame = pd.concat([industry, concept], ignore_index=True)
            frame.attrs["source"] = "AkShare·东方财富行业+概念板块"
            return frame
        except Exception as exc:
            errors.append(f"AkShare: {exc}")
        raise RuntimeError("；".join(errors))

    def refresh_scores(self, securities: list[Security]) -> dict[str, float]:
        """Calculate the same six-dimension score used by the detail page."""

        from .indicators import (
            build_indicator_snapshot,
            calculate_indicators,
            dimension_composites,
            market_regime,
        )

        def calculate(security: Security) -> tuple[str, float]:
            target_date = latest_completed_market_day()
            store = self.historical_store
            if store is not None:
                saved = store.daily_score(security, target_date)
                if saved is not None:
                    return security.key, float(saved["score"])
            path = self.cache_dir / (
                f"score_v3_{security.security_type.value}_{security.code}.json"
            )
            if store is None and self._cache_is_fresh(path, timedelta(minutes=10)):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    return security.key, float(value["score"])
                except (OSError, TypeError, ValueError, KeyError):
                    pass
            history = self.get_history(
                security, use_cache=True, adjustment="qfq", include_live=False
            )
            # 首页评分与详情页首屏采用同一组经典系统指标；扩展指标库
            # 在用户打开“全部指标”时按需计算，避免监看页为每只股票
            # 同时展开数百列而占用大量内存。
            frame = calculate_indicators(history, include_extended=False)
            regime = market_regime(frame)
            score = float(regime["score"])
            score_date = pd.Timestamp(frame.iloc[-1]["date"]).date()
            if store is not None:
                composites = dimension_composites(build_indicator_snapshot(frame))
                dimensions = {
                    key: {
                        "score": float(value["score"]),
                        "status": str(value["status"]),
                        "count": int(value["count"]),
                        "weight": float(value["weight"]),
                    }
                    for key, value in composites.items()
                }
                store.save_daily_score(
                    security,
                    score_date,
                    score,
                    direction=str(regime["direction"]),
                    regime=str(regime["regime"]),
                    dimensions=dimensions,
                )
            else:
                path.write_text(
                    json.dumps(
                        {
                            "score": score,
                            "beijing_time": score_date.isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            return security.key, score

        scores: dict[str, float] = {}
        with ThreadPoolExecutor(
            max_workers=min(2, max(1, len(securities)))
        ) as executor:
            futures = {
                executor.submit(calculate, security): security
                for security in securities
            }
            for future in as_completed(futures):
                security = futures[future]
                try:
                    key, score = future.result()
                    scores[key] = score
                except Exception:
                    if self.historical_store is not None:
                        saved = self.historical_store.daily_score(security)
                        if saved is not None:
                            scores[security.key] = float(saved["score"])
                            continue
                    path = self.cache_dir / (
                        f"score_v3_{security.security_type.value}_{security.code}.json"
                    )
                    if path.exists():
                        try:
                            scores[security.key] = float(
                                json.loads(path.read_text(encoding="utf-8"))["score"]
                            )
                        except (OSError, TypeError, ValueError, KeyError):
                            pass
        return scores

    def _fetch_direct_quote(self, security: Security) -> Quote:
        errors: list[str] = []
        for source, loader in (
            ("腾讯行情", self._fetch_tencent_quote),
            ("东方财富", self._fetch_eastmoney_quote),
            ("新浪行情", self._fetch_sina_quote),
        ):
            try:
                quote = loader(security)
                if quote.price is None:
                    raise RuntimeError("未返回最新价")
                quote.extra["source"] = source
                return quote
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        raise RuntimeError("；".join(errors))

    def _fetch_eastmoney_quote(self, security: Security) -> Quote:
        market_id = 1 if security.market in {"sh", "csi"} else 0
        response = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f116,f117,f162,f167,f168,f169,f170,f171",
                "secid": f"{market_id}.{security.code}",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
            timeout=5,
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, dict) or _as_float(data.get("f43")) is None:
            raise RuntimeError("实时行情未返回有效数据")
        quote = Quote(
            security=security,
            price=_as_float(data.get("f43")),
            change=_as_float(data.get("f169")),
            change_pct=_as_float(data.get("f170")),
            open=_as_float(data.get("f46")),
            high=_as_float(data.get("f44")),
            low=_as_float(data.get("f45")),
            previous_close=_as_float(data.get("f60")),
            volume=_as_float(data.get("f47")),
            amount=_as_float(data.get("f48")),
            amplitude=_as_float(data.get("f171")),
            turnover=_as_float(data.get("f168")),
            volume_ratio=_as_float(data.get("f50")),
            pe=_as_float(data.get("f162")),
            pb=_as_float(data.get("f167")),
            market_cap=_as_float(data.get("f116")),
            float_market_cap=_as_float(data.get("f117")),
        )
        timestamp = _as_float(data.get("f86"))
        if timestamp is not None and timestamp > 0:
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                quote.extra["trade_datetime"] = (
                    datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .isoformat()
                )
            except (OSError, OverflowError, ValueError):
                pass
        return quote

    @staticmethod
    def _market_symbol(security: Security) -> str:
        market = "sh" if security.market == "csi" else security.market
        return f"{market}{security.code}"

    def _fetch_tencent_quote(self, security: Security) -> Quote:
        symbol = self._market_symbol(security)
        response = requests.get(
            "https://qt.gtimg.cn/q=" + symbol,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=5,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r'="(.*)"', response.text)
        values = match.group(1).split("~") if match else []
        if len(values) < 35:
            raise RuntimeError("返回字段不完整")
        price = _as_float(values[3])
        previous_close = _as_float(values[4])
        change = _as_float(values[31]) if len(values) > 31 else None
        change_pct = _as_float(values[32]) if len(values) > 32 else None
        if change is None and price is not None and previous_close is not None:
            change = price - previous_close
        if change_pct is None and change is not None and previous_close:
            change_pct = change / previous_close * 100
        quote = Quote(
            security=security,
            price=price,
            previous_close=previous_close,
            open=_as_float(values[5]),
            high=_as_float(values[33]),
            low=_as_float(values[34]),
            change=change,
            change_pct=change_pct,
            volume=_as_float(values[6]),
            amount=(_as_float(values[37]) or 0) * 10_000 if len(values) > 37 else None,
            turnover=_as_float(values[38]) if len(values) > 38 else None,
            pe=_as_float(values[39]) if len(values) > 39 else None,
            market_cap=(_as_float(values[45]) or 0) * 100_000_000
            if len(values) > 45
            else None,
            pb=_as_float(values[46]) if len(values) > 46 else None,
        )
        if len(values) > 30 and re.fullmatch(r"\d{14}", values[30] or ""):
            timestamp = datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
            quote.extra["trade_datetime"] = timestamp.isoformat()
        if len(values) >= 29:
            quote.extra["order_book"] = {
                "bids": [
                    {
                        "level": level,
                        "price": _as_float(values[9 + (level - 1) * 2]),
                        "volume": _as_float(values[10 + (level - 1) * 2]),
                    }
                    for level in range(1, 6)
                ],
                "asks": [
                    {
                        "level": level,
                        "price": _as_float(values[19 + (level - 1) * 2]),
                        "volume": _as_float(values[20 + (level - 1) * 2]),
                    }
                    for level in range(1, 6)
                ],
                "source": "腾讯公开五档行情（非Level-2）",
            }
        quote.extra["source"] = "腾讯行情"
        return quote

    def get_order_book(self, security: Security) -> dict[str, object]:
        """Return public five-level quotes. Never synthesizes missing depth."""
        quote = self._fetch_tencent_quote(security)
        order_book = quote.extra.get("order_book")
        if not isinstance(order_book, dict):
            raise RuntimeError("公开行情接口未返回五档盘口")
        return {"quote": quote, **order_book}

    def _fetch_sina_quote(self, security: Security) -> Quote:
        symbol = self._market_symbol(security)
        response = requests.get(
            "https://hq.sinajs.cn/list=" + symbol,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=5,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r'="(.*)"', response.text)
        values = match.group(1).split(",") if match else []
        if len(values) < 10:
            raise RuntimeError("返回字段不完整")
        price = _as_float(values[3])
        previous_close = _as_float(values[2])
        change = (
            price - previous_close
            if price is not None and previous_close is not None
            else None
        )
        change_pct = (
            change / previous_close * 100
            if change is not None and previous_close
            else None
        )
        quote = Quote(
            security=security,
            price=price,
            previous_close=previous_close,
            open=_as_float(values[1]),
            high=_as_float(values[4]),
            low=_as_float(values[5]),
            change=change,
            change_pct=change_pct,
            volume=_as_float(values[8]),
            amount=_as_float(values[9]),
        )
        if len(values) > 31:
            try:
                timestamp = datetime.strptime(
                    f"{values[30]} {values[31]}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                quote.extra["trade_datetime"] = timestamp.isoformat()
            except ValueError:
                pass
        return quote

    def _quote_from_spot_row(self, security: Security, row: pd.Series) -> Quote:
        return Quote(
            security=security,
            price=_row_float(row, "最新价", "收盘"),
            change=_row_float(row, "涨跌额"),
            change_pct=_row_float(row, "涨跌幅"),
            open=_row_float(row, "今开", "开盘"),
            high=_row_float(row, "最高"),
            low=_row_float(row, "最低"),
            previous_close=_row_float(row, "昨收"),
            volume=_row_float(row, "成交量"),
            amount=_row_float(row, "成交额"),
            amplitude=_row_float(row, "振幅"),
            turnover=_row_float(row, "换手率"),
            volume_ratio=_row_float(row, "量比"),
            pe=_row_float(row, "市盈率-动态", "市盈率"),
            pb=_row_float(row, "市净率"),
            market_cap=_row_float(row, "总市值"),
            float_market_cap=_row_float(row, "流通市值"),
        )

    def _quote_from_history(self, security: Security, frame: pd.DataFrame) -> Quote:
        row = frame.iloc[-1]
        previous = frame.iloc[-2]["close"] if len(frame) > 1 else np.nan
        close = _as_float(row.get("close"))
        change = (
            close - float(previous)
            if close is not None and pd.notna(previous)
            else None
        )
        change_pct = (
            change / float(previous) * 100 if change is not None and previous else None
        )
        quote = Quote(
            security=security,
            price=close,
            change=change,
            change_pct=_as_float(row.get("pct_change")) or change_pct,
            open=_as_float(row.get("open")),
            high=_as_float(row.get("high")),
            low=_as_float(row.get("low")),
            previous_close=_as_float(previous),
            volume=_as_float(row.get("volume")),
            amount=_as_float(row.get("amount")),
            amplitude=_as_float(row.get("amplitude")),
            turnover=_as_float(row.get("turnover")),
        )
        quote.extra["source"] = "本地历史缓存"
        return quote

    @staticmethod
    def _quote_trade_day(quote: Quote, fallback: date) -> date:
        raw = str(quote.extra.get("trade_datetime", "")).strip()
        if raw:
            try:
                timestamp = datetime.fromisoformat(raw)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                return timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date()
            except ValueError:
                pass
        today = beijing_today()
        return today if today.weekday() < 5 else fallback

    def _merge_live_daily_bar(
        self,
        security: Security,
        frame: pd.DataFrame,
        adjustment: str,
    ) -> pd.DataFrame:
        """Overlay the latest direct quote on the daily series.

        AkShare daily endpoints and the six-hour cache may not contain today's
        unfinished candle.  The direct quote supplies the live OHLCV fields;
        adjusted series are scaled to the previous adjusted close.
        """

        if frame is None or frame.empty:
            return frame
        try:
            quote = self._fetch_direct_quote(security)
        except Exception:
            return frame
        if quote.price is None or quote.price <= 0:
            return frame

        result = frame.copy()
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        result = result.dropna(subset=["date"]).sort_values("date")
        fallback_day = result.iloc[-1]["date"].date()
        trade_day = self._quote_trade_day(quote, fallback_day)
        if trade_day > beijing_today():
            trade_day = beijing_today()
        trade_timestamp = pd.Timestamp(trade_day)
        previous_rows = result[result["date"] < trade_timestamp]
        previous_adjusted = (
            _as_float(previous_rows.iloc[-1].get("close"))
            if not previous_rows.empty
            else quote.previous_close
        )

        scale = 1.0
        if adjustment and quote.previous_close and previous_adjusted:
            candidate = previous_adjusted / quote.previous_close
            if math.isfinite(candidate) and candidate > 0:
                scale = candidate

        close = quote.price * scale
        open_value = (quote.open or quote.price) * scale
        high = (quote.high or max(quote.price, quote.open or quote.price)) * scale
        low = (quote.low or min(quote.price, quote.open or quote.price)) * scale
        if open_value <= 0:
            open_value = close
        if high <= 0:
            high = max(open_value, close)
        if low <= 0:
            low = min(open_value, close)
        high = max(high, open_value, close)
        low = min(low, open_value, close)

        previous_close = previous_adjusted or (quote.previous_close or close) * scale
        change = close - previous_close
        pct_change = change / previous_close * 100 if previous_close else np.nan
        amplitude = (high - low) / previous_close * 100 if previous_close else np.nan
        existing = result[result["date"] == trade_timestamp]
        existing_row = (
            existing.iloc[-1] if not existing.empty else pd.Series(dtype=object)
        )
        live_row = {
            "date": trade_timestamp,
            "open": open_value,
            "close": close,
            "high": high,
            "low": low,
            "volume": quote.volume
            if quote.volume is not None
            else existing_row.get("volume", np.nan),
            "amount": quote.amount
            if quote.amount is not None
            else existing_row.get("amount", np.nan),
            "amplitude": quote.amplitude
            if adjustment == "" and quote.amplitude is not None
            else amplitude,
            "pct_change": quote.change_pct
            if adjustment == "" and quote.change_pct is not None
            else pct_change,
            "change": quote.change * scale if quote.change is not None else change,
            "turnover": quote.turnover
            if quote.turnover is not None
            else existing_row.get("turnover", np.nan),
        }
        result = result[result["date"] != trade_timestamp]
        result = pd.concat([result, pd.DataFrame([live_row])], ignore_index=True)
        result = self._normalize_history(result)
        source = self.history_sources.get(security.key, "日线")
        self.history_sources[security.key] = (
            f"{source} + {quote.extra.get('source', '实时行情')}"
        )
        return result

    def get_history(
        self,
        security: Security,
        calendar_days: int = 1100,
        use_cache: bool = True,
        adjustment: str = "qfq",
        include_live: bool = True,
        persist: bool = True,
    ) -> pd.DataFrame:
        if adjustment not in {"", "qfq", "hfq"}:
            raise ValueError("复权方式只支持不复权、前复权或后复权")
        effective_adjustment = (
            "" if security.security_type is SecurityType.INDEX else adjustment
        )
        cache_suffix = effective_adjustment or "raw"
        path = self.cache_dir / (
            f"history_{security.security_type.value}_{security.code}_{cache_suffix}.csv"
        )
        end = beijing_today()
        start = end - timedelta(days=calendar_days)
        completed_end = latest_completed_market_day()
        store = self.historical_store
        stored = (
            store.get_bars(
                security,
                effective_adjustment,
                start=start,
                end=completed_end,
            )
            if store is not None
            else pd.DataFrame()
        )
        state_key = effective_adjustment or "raw"
        state = store.fetch_state(security, "daily_bars", state_key) if store else {}
        last_attempt = str(state.get("last_attempt_at", ""))[:10]
        latest_stored = (
            pd.Timestamp(stored.iloc[-1]["date"]).date()
            if not stored.empty
            else None
        )
        if use_cache and not stored.empty and (
            latest_stored is not None
            and latest_stored >= completed_end
            or last_attempt == end.isoformat()
        ):
            self.history_sources[security.key] = "本地数据库"
            return (
                self._merge_live_daily_bar(
                    security, stored, effective_adjustment
                )
                if include_live
                else stored
            )
        if use_cache and self._cache_is_fresh(path, timedelta(hours=6)):
            cached = self._read_frame(path)
            if not cached.empty:
                if store is not None:
                    completed = cached[
                        pd.to_datetime(cached["date"], errors="coerce").dt.date
                        <= completed_end
                    ]
                    store.upsert_bars(
                        security,
                        completed,
                        effective_adjustment,
                        "旧版CSV缓存迁移",
                    )
                self.history_sources[security.key] = "本地缓存"
                return (
                    self._merge_live_daily_bar(security, cached, effective_adjustment)
                    if include_live
                    else cached
                )

        start_text = start.strftime("%Y%m%d")
        end_text = end.strftime("%Y%m%d")
        market_symbol = self._market_symbol(security)
        loaders: list[tuple[str, Callable[[], pd.DataFrame]]]
        if security.security_type is SecurityType.STOCK:
            loaders = [
                (
                    "AkShare·东方财富日线",
                    lambda: ak.stock_zh_a_hist(
                        symbol=security.code,
                        period="daily",
                        start_date=start_text,
                        end_date=end_text,
                        adjust=effective_adjustment,
                        timeout=10,
                    ),
                ),
                (
                    "AkShare·腾讯日线",
                    lambda: ak.stock_zh_a_hist_tx(
                        symbol=market_symbol,
                        start_date=start_text,
                        end_date=end_text,
                        adjust=effective_adjustment,
                        timeout=10,
                    ),
                ),
                (
                    "AkShare·新浪日线",
                    lambda: ak.stock_zh_a_daily(
                        symbol=market_symbol,
                        start_date=start_text,
                        end_date=end_text,
                        adjust=effective_adjustment,
                    ),
                ),
            ]
        elif security.security_type is SecurityType.ETF:
            loaders = [
                (
                    "AkShare·东方财富ETF日线",
                    lambda: ak.fund_etf_hist_em(
                        symbol=security.code,
                        period="daily",
                        start_date=start_text,
                        end_date=end_text,
                        adjust=effective_adjustment,
                    ),
                ),
                (
                    "AkShare·腾讯ETF日线",
                    lambda: ak.stock_zh_a_hist_tx(
                        symbol=market_symbol,
                        start_date=start_text,
                        end_date=end_text,
                        adjust=effective_adjustment,
                        timeout=10,
                    ),
                ),
            ]
            if not effective_adjustment:
                loaders.append(
                    (
                        "AkShare·新浪ETF日线",
                        lambda: ak.fund_etf_hist_sina(symbol=market_symbol),
                    )
                )
        else:
            loaders = [
                (
                    "AkShare·东方财富指数日线",
                    lambda: ak.stock_zh_index_daily_em(
                        symbol=("sh" if security.market == "csi" else security.market)
                        + security.code,
                        start_date=start_text,
                        end_date=end_text,
                    ),
                ),
                (
                    "AkShare·腾讯指数日线",
                    lambda: ak.stock_zh_index_daily_tx(
                        symbol=market_symbol, start_date=start_text, end_date=end_text
                    ),
                ),
                (
                    "AkShare·新浪指数日线",
                    lambda: ak.stock_zh_index_daily(symbol=market_symbol),
                ),
                (
                    "AkShare·东方财富指数历史",
                    lambda: ak.index_zh_a_hist(
                        symbol=security.code,
                        period="daily",
                        start_date=start_text,
                        end_date=end_text,
                    ),
                ),
            ]

        def persist_completed_history(frame: pd.DataFrame, source: str) -> None:
            if not persist:
                return
            completed = frame[
                pd.to_datetime(frame["date"], errors="coerce").dt.date
                <= completed_end
            ].copy()
            if store is not None:
                store.upsert_bars(
                    security,
                    completed,
                    effective_adjustment,
                    source,
                    temporary_last=False,
                )
                if not completed.empty:
                    store.update_fetch_state(
                        security,
                        "daily_bars",
                        state_key,
                        success=True,
                        coverage_start=pd.Timestamp(
                            completed.iloc[0]["date"]
                        ).strftime("%Y-%m-%d"),
                        coverage_end=pd.Timestamp(
                            completed.iloc[-1]["date"]
                        ).strftime("%Y-%m-%d"),
                        record_count=len(completed),
                        source=source,
                    )
            else:
                frame.to_csv(path, index=False, encoding="utf-8-sig")

        errors: list[str] = []
        for source, loader in loaders:
            try:
                normalized = self._normalize_history(
                    self._call_with_timeout(loader, 12)
                )
                normalized = normalized[
                    (normalized["date"] >= pd.Timestamp(start))
                    & (normalized["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1))
                ]
                if normalized.empty:
                    raise RuntimeError("返回空数据")
                persist_completed_history(normalized, source)
                self.history_sources[security.key] = source
                if store is not None:
                    database_frame = store.get_bars(
                        security,
                        effective_adjustment,
                        start=start,
                        end=completed_end,
                    )
                    if not database_frame.empty:
                        normalized = database_frame
                normalized = normalized.reset_index(drop=True)
                return (
                    self._merge_live_daily_bar(
                        security, normalized, effective_adjustment
                    )
                    if include_live
                    else normalized
                )
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if security.security_type is SecurityType.ETF and effective_adjustment:
            try:
                raw = self._normalize_history(
                    self._call_with_timeout(
                        lambda: ak.fund_etf_hist_sina(symbol=market_symbol), 12
                    )
                )
                raw = raw[
                    (raw["date"] >= pd.Timestamp(start))
                    & (raw["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1))
                ]
                dividends = self._call_with_timeout(
                    lambda: ak.fund_etf_dividend_sina(symbol=market_symbol), 10
                )
                normalized = self._adjust_etf_history_locally(
                    raw, dividends, effective_adjustment
                )
                if normalized.empty:
                    raise RuntimeError("本地复权结果为空")
                source = "新浪ETF日线 + 分红序列本地复权"
                persist_completed_history(normalized, source)
                self.history_sources[security.key] = source
                if store is not None:
                    database_frame = store.get_bars(
                        security,
                        effective_adjustment,
                        start=start,
                        end=completed_end,
                    )
                    if not database_frame.empty:
                        normalized = database_frame
                return (
                    self._merge_live_daily_bar(
                        security, normalized, effective_adjustment
                    )
                    if include_live
                    else normalized
                )
            except Exception as exc:
                errors.append(f"ETF分红序列本地复权: {exc}")
        if store is not None:
            retry = beijing_now() + timedelta(minutes=15 if stored.empty else 120)
            store.update_fetch_state(
                security,
                "daily_bars",
                state_key,
                success=False,
                record_count=len(stored),
                source=self.history_sources.get(security.key, ""),
                error="；".join(errors),
                retry_after=retry.strftime("%Y-%m-%d %H:%M:%S%z"),
            )
            if not stored.empty:
                self.history_sources[security.key] = "本地数据库（网络更新失败）"
                return (
                    self._merge_live_daily_bar(
                        security, stored, effective_adjustment
                    )
                    if include_live
                    else stored
                )
        if path.exists():
            cached = self._read_frame(path)
            if not cached.empty:
                self.history_sources[security.key] = "过期本地缓存"
                return (
                    self._merge_live_daily_bar(security, cached, effective_adjustment)
                    if include_live
                    else cached
                )
        raise RuntimeError("所有日线接口均不可用：" + "；".join(errors))

    @staticmethod
    def _adjust_etf_history_locally(
        history: pd.DataFrame, dividends: pd.DataFrame, adjustment: str
    ) -> pd.DataFrame:
        if history is None or history.empty or dividends is None or dividends.empty:
            return pd.DataFrame()
        date_column = next(
            (name for name in ("日期", "date") if name in dividends), None
        )
        cumulative_column = next(
            (name for name in ("累计分红", "累计派息", "分红") if name in dividends),
            None,
        )
        if date_column is None or cumulative_column is None:
            return pd.DataFrame()
        result = history.copy().sort_values("date").reset_index(drop=True)
        events = dividends[[date_column, cumulative_column]].copy()
        events[date_column] = pd.to_datetime(events[date_column], errors="coerce")
        events[cumulative_column] = pd.to_numeric(
            events[cumulative_column], errors="coerce"
        )
        events = events.dropna().sort_values(date_column)
        events["cash"] = (
            events[cumulative_column].diff().fillna(events[cumulative_column])
        )
        factor = pd.Series(1.0, index=result.index)
        for _, event in events.iterrows():
            ex_date = pd.Timestamp(event[date_column]).normalize()
            previous = result[result["date"] < ex_date]
            if previous.empty:
                continue
            previous_close = _as_float(previous.iloc[-1].get("close"))
            cash = _as_float(event.get("cash"))
            if (
                not previous_close
                or cash is None
                or cash <= 0
                or cash >= previous_close
            ):
                continue
            ratio = (previous_close - cash) / previous_close
            if adjustment == "qfq":
                factor.loc[result["date"] < ex_date] *= ratio
            else:
                factor.loc[result["date"] >= ex_date] /= ratio
        for column in ("open", "close", "high", "low"):
            result[column] = pd.to_numeric(result[column], errors="coerce") * factor
        previous = result["close"].shift(1)
        result["change"] = result["close"] - previous
        result["pct_change"] = (result["close"] / previous - 1) * 100
        result["amplitude"] = (result["high"] - result["low"]) / previous * 100
        return result

    def get_detail_bundle(
        self,
        security: Security,
        adjustment: str = "qfq",
        include_extras: bool = True,
    ) -> DetailBundle:
        history = self.get_history(security, adjustment=adjustment, include_live=True)
        bundle = DetailBundle(security=security, history=history)
        bundle.sources["日线"] = self.history_sources.get(security.key, "未知")
        if not include_extras:
            return bundle
        if security.security_type is SecurityType.ETF:
            try:
                bundle.corporate_actions = self._load_persistent_extra(
                    "corporate_actions",
                    security,
                    lambda: self._load_corporate_actions(security),
                    timedelta(hours=24),
                    "AkShare·ETF分红",
                )
                bundle.sources["corporate_actions"] = str(
                    bundle.corporate_actions.attrs.get("source", "AkShare·ETF分红")
                )
            except Exception:
                bundle.warnings.append("ETF分红标注接口暂时不可用")
            bundle.warnings.append("ETF不提供个股股东与企业财务披露数据。")
            return bundle
        if security.security_type is not SecurityType.STOCK:
            bundle.warnings.append("ETF/指数不提供个股股东与筹码披露数据。")
            return bundle

        extras: tuple[
            tuple[str, Callable[[], pd.DataFrame], str, timedelta, str], ...
        ] = (
            (
                "fund_flow",
                lambda: self._load_fund_flow(security, history),
                "主力资金流接口暂时不可用",
                timedelta(hours=1),
                "多源资金流",
            ),
            (
                "chips",
                lambda: self._load_chips(security, history, adjustment),
                "筹码分布接口暂时不可用",
                timedelta(hours=6),
                "AkShare·东方财富公开筹码分布",
            ),
            (
                "holders",
                lambda: ak.stock_main_stock_holder(stock=security.code),
                "主要股东披露接口暂时不可用",
                timedelta(hours=24),
                "AkShare·主要股东披露",
            ),
            (
                "company_info",
                lambda: self._load_company_info(security),
                "企业概况接口暂时不可用",
                timedelta(hours=24),
                "AkShare·发行资料/东财/巨潮企业概况",
            ),
            (
                "business_info",
                lambda: ak.stock_zyjs_ths(symbol=security.code),
                "主营业务接口暂时不可用",
                timedelta(hours=24),
                "AkShare·同花顺主营",
            ),
            (
                "financials",
                lambda: self._load_financials(security.code),
                "财务指标接口暂时不可用",
                timedelta(hours=12),
                "AkShare·同花顺/新浪财务",
            ),
            (
                "balance_sheet",
                lambda: self._load_statement(security, "balance"),
                "资产负债表接口暂时不可用",
                timedelta(hours=12),
                "AkShare·东方财富/新浪资产负债表",
            ),
            (
                "profit_sheet",
                lambda: self._load_statement(security, "profit"),
                "利润表接口暂时不可用",
                timedelta(hours=12),
                "AkShare·东方财富/新浪利润表",
            ),
            (
                "cash_flow_sheet",
                lambda: self._load_statement(security, "cash_flow"),
                "现金流量表接口暂时不可用",
                timedelta(hours=12),
                "AkShare·东方财富/新浪现金流量表",
            ),
            (
                "corporate_actions",
                lambda: self._load_corporate_actions(security),
                "除权除息与分红标注接口暂时不可用",
                timedelta(hours=24),
                "AkShare·分红送配",
            ),
        )
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {}
            for attribute, loader, warning, max_age, source in extras:
                future = (
                    executor.submit(loader)
                    if attribute in {"fund_flow", "chips"}
                    else executor.submit(
                        self._load_persistent_extra,
                        attribute,
                        security,
                        loader,
                        max_age,
                        source,
                    )
                )
                future_map[future] = (attribute, warning, source)
            for future in as_completed(future_map):
                attribute, warning, source = future_map[future]
                try:
                    frame = future.result()
                    setattr(
                        bundle,
                        attribute,
                        frame if frame is not None else pd.DataFrame(),
                    )
                    bundle.sources[attribute] = str(
                        getattr(frame, "attrs", {}).get("source", source)
                    )
                except Exception:
                    bundle.warnings.append(warning)
        return bundle

    def _load_corporate_actions(self, security: Security) -> pd.DataFrame:
        loaders: list[tuple[str, Callable[[], pd.DataFrame]]]
        if security.security_type is SecurityType.ETF:
            loaders = [
                (
                    "AkShare·新浪ETF分红",
                    lambda: ak.fund_etf_dividend_sina(
                        symbol=self._market_symbol(security)
                    ),
                )
            ]
        else:
            loaders = [
                (
                    "AkShare·东方财富分红送配",
                    lambda: ak.stock_fhps_detail_em(symbol=security.code),
                ),
                (
                    "AkShare·巨潮分红",
                    lambda: ak.stock_dividend_cninfo(symbol=security.code),
                ),
            ]
        for source, loader in loaders:
            try:
                frame = self._call_with_timeout(loader, 10)
                if frame is None or frame.empty:
                    continue
                date_column = next(
                    (
                        name
                        for name in (
                            "除权除息日",
                            "除权日",
                            "实施方案公告日期",
                            "公告日期",
                            "日期",
                        )
                        if name in frame
                    ),
                    None,
                )
                if date_column is None:
                    continue
                result = pd.DataFrame(
                    {"date": pd.to_datetime(frame[date_column], errors="coerce")}
                )
                label_columns = [
                    name
                    for name in ("分红方案", "送转股份-送转总比例", "派息比例", "红利")
                    if name in frame
                ]
                result["label"] = (
                    frame[label_columns].astype(str).agg(" ".join, axis=1)
                    if label_columns
                    else "除权除息/分红"
                )
                result = (
                    result.dropna(subset=["date"])
                    .drop_duplicates("date")
                    .sort_values("date")
                )
                result.attrs["source"] = source
                return result.reset_index(drop=True)
            except Exception:
                continue
        return pd.DataFrame(columns=["date", "label"])

    @staticmethod
    def _normalized_code(value: object) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        return digits[-6:].zfill(6) if digits else ""

    @staticmethod
    def _chinese_number(value: object) -> float | None:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return None
        if isinstance(value, (int, float, np.number)):
            return _as_float(value)
        text = str(value).strip().replace(",", "").replace("%", "")
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if not match:
            return None
        multiplier = 100_000_000 if "亿" in text else 10_000 if "万" in text else 1
        return float(match.group()) * multiplier

    def _snapshot_fund_flow(
        self, frame: pd.DataFrame, security: Security, source: str
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        code_column = next(
            (column for column in ("代码", "股票代码", "证券代码") if column in frame),
            None,
        )
        if code_column is None:
            return pd.DataFrame()
        matched = frame[frame[code_column].map(self._normalized_code) == security.code]
        if matched.empty:
            return pd.DataFrame()
        row = matched.iloc[0]

        def number(*columns: str) -> float | None:
            for column in columns:
                if column in row.index:
                    parsed = self._chinese_number(row.get(column))
                    if parsed is not None:
                        return parsed
            return None

        main = number("今日主力净流入-净额", "主力净流入-净额", "净额")
        amount = number("成交额")
        ratio = number("今日主力净流入-净占比", "主力净流入-净占比")
        if ratio is None and main is not None and amount:
            ratio = main / amount * 100
        result = pd.DataFrame(
            [
                {
                    "日期": f"{beijing_today():%Y-%m-%d}",
                    "主力净流入-净额": main,
                    "主力净流入-净占比": ratio,
                    "超大单净流入-净额": number(
                        "今日超大单净流入-净额", "超大单净流入-净额"
                    ),
                    "大单净流入-净额": number("今日大单净流入-净额", "大单净流入-净额"),
                    "中单净流入-净额": number("今日中单净流入-净额", "中单净流入-净额"),
                    "小单净流入-净额": number("今日小单净流入-净额", "小单净流入-净额"),
                }
            ]
        )
        if result["主力净流入-净额"].isna().all():
            return pd.DataFrame()
        result.attrs["source"] = source
        return result

    def _load_fund_flow(
        self, security: Security, history: pd.DataFrame
    ) -> pd.DataFrame:
        store = self.historical_store
        completed_end = latest_completed_market_day()
        stored = store.get_fund_flow(security) if store is not None else pd.DataFrame()
        state = store.fetch_state(security, "fund_flow") if store is not None else {}
        latest_stored = (
            pd.to_datetime(stored["日期"], errors="coerce").dt.date.max()
            if not stored.empty and "日期" in stored
            else None
        )
        if (
            not stored.empty
            and latest_stored is not None
            and latest_stored >= completed_end
            and str(state.get("last_success_at", ""))[:10]
            == beijing_today().isoformat()
        ):
            stored.attrs["source"] = "本地资金流仓库（已覆盖最近完成交易日）"
            return stored

        errors: list[str] = []
        loaders: tuple[tuple[str, Callable[[], pd.DataFrame]], ...] = (
            (
                "AkShare·东方财富个股近100日资金流",
                lambda: ak.stock_individual_fund_flow(
                    stock=security.code, market=security.market
                ),
            ),
            (
                "AkShare·东方财富全市场当日资金流",
                lambda: self._snapshot_fund_flow(
                    ak.stock_individual_fund_flow_rank(indicator="今日"),
                    security,
                    "AkShare·东方财富全市场当日资金流",
                ),
            ),
            (
                "AkShare·同花顺个股资金流",
                lambda: self._snapshot_fund_flow(
                    ak.stock_fund_flow_individual(symbol="即时"),
                    security,
                    "AkShare·同花顺个股资金流",
                ),
            ),
        )
        for source, loader in loaders:
            try:
                # 单个公开源卡住时必须尽快切换，不能让整个详情页等到外层超时。
                frame = self._call_with_timeout(loader, 4)
                if frame is None or frame.empty:
                    raise RuntimeError("未找到该股票")
                frame = frame.copy()
                frame.attrs["source"] = frame.attrs.get("source", source)
                if store is not None and "日期" in frame:
                    completed = frame[
                        pd.to_datetime(frame["日期"], errors="coerce").dt.date
                        <= completed_end
                    ].copy()
                    stored_count = store.upsert_fund_flow(
                        security,
                        completed,
                        source=source,
                        is_estimated=False,
                    )
                    store.update_fetch_state(
                        security,
                        "fund_flow",
                        success=True,
                        coverage_start=(
                            str(completed["日期"].iloc[0]) if not completed.empty else ""
                        ),
                        coverage_end=(
                            str(completed["日期"].iloc[-1]) if not completed.empty else ""
                        ),
                        record_count=stored_count,
                        source=source,
                    )
                return frame
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if not stored.empty:
            stored.attrs["source"] = "本地资金流仓库（联网更新失败）"
            stored.attrs["fallback_errors"] = errors
            if store is not None:
                store.update_fetch_state(
                    security,
                    "fund_flow",
                    success=False,
                    record_count=len(stored),
                    error="；".join(errors),
                )
            return stored
        estimated = self._estimate_fund_flow(history)
        estimated.attrs["source"] = "本地OHLCV资金流估算（非逐笔主力）"
        estimated.attrs["fallback_errors"] = errors
        if store is not None and not estimated.empty:
            completed = estimated[
                pd.to_datetime(estimated["日期"], errors="coerce").dt.date
                <= completed_end
            ].copy()
            count = store.upsert_fund_flow(
                security,
                completed,
                source="本地OHLCV资金流估算（非逐笔主力）",
                is_estimated=True,
            )
            store.update_fetch_state(
                security,
                "fund_flow",
                success=bool(count),
                coverage_start=(
                    str(completed["日期"].iloc[0]) if not completed.empty else ""
                ),
                coverage_end=(
                    str(completed["日期"].iloc[-1]) if not completed.empty else ""
                ),
                record_count=count,
                source="本地OHLCV资金流估算（非逐笔主力）",
                error="" if count else "；".join(errors),
            )
        return estimated

    def _estimate_fund_flow(self, history: pd.DataFrame) -> pd.DataFrame:
        if history is None or history.empty:
            return pd.DataFrame()
        frame = history.copy().tail(120)
        span = (frame["high"] - frame["low"]).replace(0, np.nan)
        multiplier = (
            ((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])) / span
        ).fillna(0)
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3
        amount = pd.to_numeric(frame.get("amount"), errors="coerce")
        amount = amount.where(
            amount > 0, typical * pd.to_numeric(frame["volume"], errors="coerce") * 100
        )
        main = multiplier * amount
        return pd.DataFrame(
            {
                "日期": pd.to_datetime(frame["date"], errors="coerce").dt.strftime(
                    "%Y-%m-%d"
                ),
                "主力净流入-净额": main,
                "主力净流入-净占比": multiplier * 100,
                "超大单净流入-净额": main * 0.35,
                "大单净流入-净额": main * 0.65,
                "中单净流入-净额": -main * 0.35,
                "小单净流入-净额": -main * 0.65,
            }
        ).reset_index(drop=True)

    def _load_chips(
        self, security: Security, history: pd.DataFrame, adjustment: str
    ) -> pd.DataFrame:
        store = self.historical_store
        completed_end = latest_completed_market_day()
        stored = store.get_chips(security) if store is not None else pd.DataFrame()
        state = (
            store.fetch_state(security, "chips", adjustment or "raw")
            if store is not None
            else {}
        )
        latest_stored = (
            pd.to_datetime(stored["日期"], errors="coerce").dt.date.max()
            if not stored.empty and "日期" in stored
            else None
        )
        if (
            not stored.empty
            and latest_stored is not None
            and latest_stored >= completed_end
            and str(state.get("last_success_at", ""))[:10]
            == beijing_today().isoformat()
        ):
            stored.attrs["source"] = "本地筹码仓库（公开接口原始结果）"
            return stored
        try:
            frame = self._call_with_timeout(
                lambda: ak.stock_cyq_em(symbol=security.code, adjust=adjustment),
                6,
            )
            if frame is None or frame.empty:
                raise RuntimeError("筹码接口返回空数据")
            frame = frame.copy()
            frame.attrs["source"] = "AkShare·东方财富筹码分布"
            if store is not None and "日期" in frame:
                completed = frame[
                    pd.to_datetime(frame["日期"], errors="coerce").dt.date
                    <= completed_end
                ].copy()
                count = store.upsert_chips(
                    security,
                    completed,
                    source="AkShare·东方财富筹码分布",
                    is_estimated=False,
                )
                store.update_fetch_state(
                    security,
                    "chips",
                    adjustment or "raw",
                    success=True,
                    coverage_start=(
                        str(completed["日期"].iloc[0]) if not completed.empty else ""
                    ),
                    coverage_end=(
                        str(completed["日期"].iloc[-1]) if not completed.empty else ""
                    ),
                    record_count=count,
                    source="AkShare·东方财富筹码分布",
                )
            return frame
        except Exception as exc:
            if not stored.empty:
                stored.attrs["source"] = "本地筹码仓库（联网更新失败）"
                stored.attrs["error"] = str(exc)
                if store is not None:
                    store.update_fetch_state(
                        security,
                        "chips",
                        adjustment or "raw",
                        success=False,
                        record_count=len(stored),
                        error=str(exc),
                    )
                return stored
            # Cost-distribution reconstruction is a model, not disclosed exchange data.
            # Returning an explicit empty result prevents estimated values being shown as
            # real chips, profit chips, average cost or concentration.
            frame = pd.DataFrame()
            frame.attrs["source"] = "东方财富筹码接口无可靠返回"
            frame.attrs["error"] = str(exc)
            return frame

    def get_price_reasons(self, security: Security) -> dict[str, object]:
        """Return public intraday anomaly evidence without inventing a causal story."""

        quote = self._fetch_direct_quote(security)
        if security.security_type is not SecurityType.STOCK:
            return {
                "quote": quote,
                "events": pd.DataFrame(
                    columns=["时间", "类型", "说明", "来源"]
                ),
                "summary": "ETF和指数暂无可靠的个股涨跌原因接口。",
                "source": "公开实时行情",
            }

        positive = (quote.change_pct or 0) >= 0
        categories = (
            ("火箭发射", "快速反弹", "大笔买入", "封涨停板", "打开跌停板")
            if positive
            else ("高台跳水", "加速下跌", "大笔卖出", "封跌停板", "打开涨停板")
        )
        records: list[dict[str, object]] = []

        def load_change(category: str) -> pd.DataFrame:
            return self._call_with_timeout(
                lambda: ak.stock_changes_em(symbol=category), 4
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(load_change, category): category
                for category in categories
            }
            for future in as_completed(futures):
                category = futures[future]
                try:
                    frame = future.result()
                except Exception:
                    continue
                if frame is None or frame.empty:
                    continue
                code_column = next(
                    (
                        name
                        for name in ("代码", "股票代码", "证券代码")
                        if name in frame
                    ),
                    None,
                )
                if code_column is None:
                    continue
                matched = frame[
                    frame[code_column].map(self._normalized_code) == security.code
                ]
                for _, row in matched.iterrows():
                    records.append(
                        {
                            "时间": str(row.get("时间", "")),
                            "类型": str(row.get("板块", row.get("异动类型", category))),
                            "说明": str(row.get("相关信息", row.get("异动信息", ""))),
                            "来源": "AkShare·东方财富盘口异动",
                        }
                    )

        day_text = beijing_today().strftime("%Y%m%d")
        try:
            pool = self._call_with_timeout(
                lambda: ak.stock_zt_pool_em(date=day_text), 5
            )
            if pool is not None and not pool.empty:
                code_column = next(
                    (name for name in ("代码", "股票代码") if name in pool), None
                )
                matched = (
                    pool[pool[code_column].map(self._normalized_code) == security.code]
                    if code_column
                    else pd.DataFrame()
                )
                if not matched.empty:
                    row = matched.iloc[0]
                    details = [
                        f"{name}：{row.get(name)}"
                        for name in ("所属行业", "首次封板时间", "最后封板时间", "炸板次数", "连板数")
                        if name in row.index and pd.notna(row.get(name))
                    ]
                    records.append(
                        {
                            "时间": str(row.get("最后封板时间", "")),
                            "类型": "涨停池",
                            "说明": "；".join(details) or "进入当日公开涨停池",
                            "来源": "AkShare·东方财富涨停池",
                        }
                    )
        except Exception:
            pass

        events = pd.DataFrame(records, columns=["时间", "类型", "说明", "来源"])
        if not events.empty:
            events = events.drop_duplicates().sort_values(
                "时间", ascending=False
            ).reset_index(drop=True)
            summary = f"检索到 {len(events)} 条公开盘口异动或涨停池证据。"
        else:
            summary = "当前公开源未检索到可核验的涨跌原因；程序不根据涨跌幅自行编造原因。"
        return {
            "quote": quote,
            "events": events,
            "summary": summary,
            "source": "东方财富盘口异动/涨停池 + 实时行情",
        }

    @staticmethod
    def _weighted_quantile(
        prices: np.ndarray, weights: np.ndarray, quantile: float
    ) -> float:
        cumulative = np.cumsum(weights)
        if cumulative.size == 0 or cumulative[-1] <= 0:
            return float("nan")
        return float(np.interp(quantile * cumulative[-1], cumulative, prices))

    def _estimate_chips(self, history: pd.DataFrame) -> pd.DataFrame:
        """Estimate a cost distribution from OHLC and turnover when CYQ is unavailable."""

        if history is None or history.empty:
            return pd.DataFrame()
        frame = history.copy().dropna(subset=["date", "high", "low", "close"])
        if frame.empty:
            return pd.DataFrame()
        price_low = max(float(frame["low"].min()) * 0.96, 1e-6)
        price_high = float(frame["high"].max()) * 1.04
        prices = np.linspace(price_low, max(price_high, price_low * 1.02), 180)
        weights = np.zeros_like(prices)
        records: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            close = float(row["close"])
            typical = float((row["high"] + row["low"] + row["close"]) / 3)
            turnover = _as_float(row.get("turnover"))
            turnover_fraction = float(np.clip((turnover or 2.5) / 100, 0.001, 1.0))
            sigma = max(float(row["high"] - row["low"]) / 2.8, close * 0.004)
            new_distribution = np.exp(-0.5 * ((prices - typical) / sigma) ** 2)
            total = float(new_distribution.sum())
            if total > 0:
                new_distribution /= total
            weights *= 1 - turnover_fraction
            weights += new_distribution * turnover_fraction
            weight_total = float(weights.sum())
            if weight_total <= 0:
                continue
            normalized = weights / weight_total
            average = float(np.sum(prices * normalized))
            low90 = self._weighted_quantile(prices, normalized, 0.05)
            high90 = self._weighted_quantile(prices, normalized, 0.95)
            low70 = self._weighted_quantile(prices, normalized, 0.15)
            high70 = self._weighted_quantile(prices, normalized, 0.85)
            records.append(
                {
                    "日期": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                    "获利比例": float(normalized[prices <= close].sum()),
                    "平均成本": average,
                    "90成本-低": low90,
                    "90成本-高": high90,
                    "90集中度": (high90 - low90) / max(high90 + low90, 1e-12),
                    "70成本-低": low70,
                    "70成本-高": high70,
                    "70集中度": (high70 - low70) / max(high70 + low70, 1e-12),
                }
            )
        return pd.DataFrame(records).tail(120).reset_index(drop=True)

    def get_intraday(
        self,
        security: Security,
        trading_day: date,
        period: str = "5",
    ) -> tuple[pd.DataFrame, str]:
        if period not in {"1", "5", "15", "30", "60"}:
            raise ValueError("分时周期只支持 1/5/15/30/60 分钟")
        store = self.historical_store
        completed = trading_day <= latest_completed_market_day()
        stored = (
            store.get_intraday_bars(security, trading_day)
            if store is not None
            else pd.DataFrame()
        )
        if completed and not stored.empty:
            return self._resample_intraday(stored, int(period)), "本地一分钟分时数据库"

        path = self.cache_dir / (
            f"intraday_{security.security_type.value}_{security.code}_{trading_day:%Y%m%d}_1.csv"
        )
        max_age = (
            timedelta(seconds=20)
            if not completed
            else timedelta(days=3650)
        )
        if self._cache_is_fresh(path, max_age):
            cached = self._read_frame(path)
            if not cached.empty:
                if completed and store is not None:
                    store.upsert_intraday_bars(
                        security, cached, period_minutes=1, source="旧版分时缓存迁移"
                    )
                    store.update_fetch_state(
                        security,
                        "intraday",
                        trading_day.isoformat(),
                        success=True,
                        coverage_start=trading_day.isoformat(),
                        coverage_end=trading_day.isoformat(),
                        record_count=len(cached),
                        source="旧版分时缓存迁移",
                    )
                return self._resample_intraday(cached, int(period)), "本地分时缓存"

        start = f"{trading_day:%Y-%m-%d} 09:15:00"
        end = f"{trading_day:%Y-%m-%d} 15:15:00"
        market_symbol = self._market_symbol(security)
        loaders: list[tuple[str, Callable[[], pd.DataFrame]]]
        if security.security_type is SecurityType.STOCK:
            loaders = [
                (
                    "AkShare·东方财富股票分时",
                    lambda: ak.stock_zh_a_hist_min_em(
                        symbol=security.code,
                        start_date=start,
                        end_date=end,
                        period="1",
                        adjust="",
                    ),
                ),
                (
                    "AkShare·新浪股票分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period="1", adjust=""
                    ),
                ),
            ]
        elif security.security_type is SecurityType.ETF:
            loaders = [
                (
                    "AkShare·东方财富ETF分时",
                    lambda: ak.fund_etf_hist_min_em(
                        symbol=security.code,
                        start_date=start,
                        end_date=end,
                        period="1",
                        adjust="",
                    ),
                ),
                (
                    "AkShare·新浪ETF分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period="1", adjust=""
                    ),
                ),
            ]
        else:
            loaders = [
                (
                    "AkShare·东方财富指数分时",
                    lambda: ak.index_zh_a_hist_min_em(
                        symbol=security.code,
                        period="1",
                        start_date=start,
                        end_date=end,
                    ),
                ),
                (
                    "AkShare·新浪指数分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period="1", adjust=""
                    ),
                ),
            ]

        errors: list[str] = []
        for source, loader in loaders:
            try:
                frame = self._normalize_history(self._call_with_timeout(loader, 10))
                frame = frame[frame["date"].dt.date == trading_day].reset_index(
                    drop=True
                )
                if frame.empty:
                    raise RuntimeError("该日期无分时数据")
                if completed and store is not None:
                    store.upsert_intraday_bars(
                        security, frame, period_minutes=1, source=source
                    )
                    store.update_fetch_state(
                        security,
                        "intraday",
                        trading_day.isoformat(),
                        success=True,
                        coverage_start=trading_day.isoformat(),
                        coverage_end=trading_day.isoformat(),
                        record_count=len(frame),
                        source=source,
                    )
                elif not completed:
                    # 当日实时数据只放短时文件缓存，不进入长期数据库。
                    frame.to_csv(path, index=False, encoding="utf-8-sig")
                return self._resample_intraday(frame, int(period)), source
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if store is not None:
            retry = beijing_now() + timedelta(minutes=10 if not completed else 120)
            store.update_fetch_state(
                security,
                "intraday",
                trading_day.isoformat(),
                success=False,
                record_count=len(stored),
                error="；".join(errors),
                retry_after=retry.strftime("%Y-%m-%d %H:%M:%S%z"),
            )
            if not stored.empty:
                return (
                    self._resample_intraday(stored, int(period)),
                    "本地一分钟分时数据库（网络更新失败）",
                )
        raise RuntimeError(
            "该日期暂无可用分时数据（1分钟通常仅保留近5个交易日）：" + "；".join(errors)
        )

    @staticmethod
    def _resample_intraday(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if frame is None or frame.empty or minutes == 1:
            return frame.reset_index(drop=True) if frame is not None else pd.DataFrame()
        source = frame.copy()
        source["date"] = pd.to_datetime(source["date"], errors="coerce")
        source = source.dropna(subset=["date"]).set_index("date")
        aggregation: dict[str, str] = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
        }
        for column in ("volume", "amount", "turnover"):
            if column in source:
                aggregation[column] = "sum"
        result = (
            source.resample(f"{minutes}min", origin="start_day")
            .agg(aggregation)
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index()
        )
        return result

    def get_news(self, security: Security, force: bool = False) -> list[NewsArticle]:
        path = (
            self.cache_dir
            / f"news_v3_{security.security_type.value}_{security.code}.json"
        )
        if not force and self._cache_is_fresh(path, timedelta(minutes=10)):
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
                return [NewsArticle(**value) for value in values]
            except (OSError, TypeError, ValueError):
                pass

        articles: list[NewsArticle] = []
        keyword = (
            security.code
            if security.security_type is SecurityType.STOCK
            else security.name
        )
        try:
            frame = self._call_with_timeout(
                lambda: ak.stock_news_em(symbol=keyword), 10
            )
            articles.extend(self._news_from_frame(frame))
        except Exception:
            pass
        try:
            bing_articles = self._news_from_bing(security)
            articles.extend(
                article
                for article in bing_articles
                if security.name.lower() in f"{article.title} {article.summary}".lower()
                or security.code in f"{article.title} {article.summary}"
            )
        except Exception:
            pass

        deduplicated: list[NewsArticle] = []
        seen: set[str] = set()
        for article in articles:
            key = re.sub(r"\W+", "", article.title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduplicated.append(article)
        deduplicated.sort(key=lambda item: item.published_at, reverse=True)
        deduplicated = deduplicated[:100]
        if not deduplicated and path.exists():
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
                return [NewsArticle(**value) for value in values]
            except (OSError, TypeError, ValueError):
                pass
        path.write_text(
            json.dumps(
                [
                    {
                        "title": item.title,
                        "summary": item.summary,
                        "source": item.source,
                        "published_at": item.published_at,
                        "url": item.url,
                    }
                    for item in deduplicated
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return deduplicated

    def get_watchlist_notices(
        self, securities: list[Security]
    ) -> list[tuple[Security, NewsArticle]]:
        """Load the current Beijing-date announcement feed once, then filter locally."""
        stocks = {
            item.code: item
            for item in securities
            if item.security_type is SecurityType.STOCK
        }
        if not stocks:
            return []
        day = beijing_today().strftime("%Y%m%d")
        frame = self._load_extra_with_cache(
            "market_notices",
            day,
            lambda: ak.stock_notice_report(symbol="全部", date=day),
            timedelta(minutes=10),
        )
        code_column = next(
            (name for name in ("代码", "股票代码", "证券代码") if name in frame), None
        )
        title_column = next(
            (name for name in ("公告标题", "标题", "公告名称") if name in frame), None
        )
        if code_column is None or title_column is None:
            return []
        type_column = next(
            (name for name in ("公告类型", "类型") if name in frame), None
        )
        date_column = next(
            (name for name in ("公告日期", "公告时间", "日期") if name in frame), None
        )
        url_column = next(
            (name for name in ("网址", "公告链接", "链接", "URL") if name in frame),
            None,
        )
        result: list[tuple[Security, NewsArticle]] = []
        for _, row in frame.iterrows():
            code = self._normalized_code(row.get(code_column))
            security = stocks.get(code)
            if security is None:
                continue
            result.append(
                (
                    security,
                    NewsArticle(
                        title=self._clean_news_text(row.get(title_column)),
                        summary=self._clean_news_text(row.get(type_column))
                        if type_column
                        else "",
                        source="交易所公告",
                        published_at=self._normalize_news_time(row.get(date_column))
                        if date_column
                        else "",
                        url=self._clean_news_text(row.get(url_column))
                        if url_column
                        else "",
                    ),
                )
            )
        return result

    @staticmethod
    def _clean_news_text(value: object) -> str:
        text = re.sub(r"<[^>]+>", " ", str(value or ""))
        return re.sub(r"\s+", " ", text).strip()

    def _news_from_frame(self, frame: pd.DataFrame) -> list[NewsArticle]:
        if frame is None or frame.empty:
            return []
        result: list[NewsArticle] = []
        for _, row in frame.iterrows():
            title = self._clean_news_text(row.get("新闻标题", row.get("标题", "")))
            if not title:
                continue
            result.append(
                NewsArticle(
                    title=title,
                    summary=self._short_news_text(
                        row.get("新闻内容", row.get("内容", row.get("摘要", "")))
                    ),
                    source=self._clean_news_text(
                        row.get("文章来源", row.get("来源", "东方财富"))
                    ),
                    published_at=self._normalize_news_time(
                        row.get("发布时间", row.get("时间", ""))
                    ),
                    url=str(row.get("新闻链接", row.get("链接", "")) or ""),
                )
            )
        return result

    def _news_from_bing(self, security: Security) -> list[NewsArticle]:
        query = quote_plus(f"{security.name} {security.code} A股 最新资讯")
        response = requests.get(
            f"https://cn.bing.com/search?q={query}&format=rss&setlang=zh-cn",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        result: list[NewsArticle] = []
        for item in root.findall(".//item"):
            source_node = item.find("source")
            link = item.findtext("link", "") or ""
            source = (
                source_node.text if source_node is not None else urlparse(link).netloc
            )
            result.append(
                NewsArticle(
                    title=self._clean_news_text(item.findtext("title", "")),
                    summary=self._short_news_text(item.findtext("description", "")),
                    source=self._clean_news_text(source or "Bing 联网搜索"),
                    published_at=self._normalize_news_time(
                        item.findtext("pubDate", "")
                    ),
                    url=link,
                )
            )
        return result

    @classmethod
    def _short_news_text(cls, value: object, limit: int = 280) -> str:
        text = cls._clean_news_text(value)
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    @staticmethod
    def _normalize_news_time(value: object) -> str:
        text = DataProvider._clean_news_text(value)
        if not text:
            return ""
        localized = re.search(
            r"(\d{1,2})\s+(\d{1,2})月\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s+GMT",
            text,
        )
        if localized:
            day, month, year, hour, minute, second = map(int, localized.groups())
            timestamp = datetime(
                year, month, day, hour, minute, second, tzinfo=timezone.utc
            ).astimezone(ZoneInfo("Asia/Shanghai"))
            return timestamp.strftime("%Y-%m-%d %H:%M")
        try:
            timestamp = pd.to_datetime(text)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("Asia/Shanghai")
            return timestamp.strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _load_company_info(security: Security) -> pd.DataFrame:
        errors: list[Exception] = []
        for loader in (
            lambda: ak.stock_ipo_info(stock=security.code),
            lambda: ak.stock_individual_info_em(symbol=security.code, timeout=8),
            lambda: ak.stock_profile_cninfo(symbol=security.code),
            lambda: ak.stock_individual_basic_info_xq(
                symbol=f"{security.market.upper()}{security.code}", timeout=8
            ),
        ):
            try:
                frame = loader()
                if frame is not None and not frame.empty:
                    frame = frame.copy()
                    frame.columns = [str(column) for column in frame.columns]
                    return frame
            except Exception as exc:
                errors.append(exc)
        raise RuntimeError(str(errors[-1]) if errors else "企业概况为空")

    @staticmethod
    def _load_financials(code: str) -> pd.DataFrame:
        errors: list[Exception] = []
        for loader in (
            lambda: ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期"),
            lambda: ak.stock_financial_analysis_indicator(
                symbol=code, start_year=str(max(1990, beijing_today().year - 8))
            ),
        ):
            try:
                frame = loader()
                if frame is not None and not frame.empty:
                    return frame
            except Exception as exc:
                errors.append(exc)
        raise RuntimeError(str(errors[-1]) if errors else "财务指标为空")

    @staticmethod
    def _load_statement(security: Security, statement: str) -> pd.DataFrame:
        symbol = f"{security.market.upper()}{security.code}"
        names = {"balance": "资产负债表", "profit": "利润表", "cash_flow": "现金流量表"}
        primary = {
            "balance": ak.stock_balance_sheet_by_report_em,
            "profit": ak.stock_profit_sheet_by_report_em,
            "cash_flow": ak.stock_cash_flow_sheet_by_report_em,
        }[statement]
        errors: list[Exception] = []
        for source, loader in (
            ("AkShare·东方财富", lambda: primary(symbol=symbol)),
            (
                "AkShare·新浪",
                lambda: ak.stock_financial_report_sina(
                    stock=symbol.lower(), symbol=names[statement]
                ),
            ),
        ):
            try:
                frame = loader()
                if frame is not None and not frame.empty:
                    frame = frame.copy()
                    frame.attrs["source"] = f"{source}{names[statement]}"
                    return frame
            except Exception as exc:
                errors.append(exc)
        raise RuntimeError(str(errors[-1]) if errors else f"{names[statement]}为空")

    def _load_extra_with_cache(
        self,
        name: str,
        code: str,
        loader: Callable[[], pd.DataFrame],
        max_age: timedelta,
    ) -> pd.DataFrame:
        path = self.cache_dir / f"{name}_{code}.csv"
        source_path = self.cache_dir / f"{name}_{code}.source.txt"
        if self._cache_is_fresh(path, max_age):
            cached = self._read_raw_frame(path)
            if not cached.empty:
                if source_path.exists():
                    try:
                        cached.attrs["source"] = source_path.read_text(
                            encoding="utf-8"
                        ).strip()
                    except OSError:
                        pass
                return cached
        try:
            timeout = 45 if name in {"market_breadth", "market_boards"} else 15
            frame = self._call_with_timeout(loader, timeout)
            if frame is None or frame.empty:
                raise RuntimeError("数据源返回空数据")
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            source = str(frame.attrs.get("source", "")).strip()
            if source:
                source_path.write_text(source, encoding="utf-8")
            return frame
        except Exception:
            cached = self._read_raw_frame(path)
            if not cached.empty:
                if source_path.exists():
                    try:
                        cached.attrs["source"] = source_path.read_text(
                            encoding="utf-8"
                        ).strip()
                    except OSError:
                        pass
                return cached
            raise

    def _load_persistent_extra(
        self,
        name: str,
        security: Security,
        loader: Callable[[], pd.DataFrame],
        max_age: timedelta,
        default_source: str = "",
    ) -> pd.DataFrame:
        """Keep reusable company datasets in SQLite and serve stale data on outages."""

        store = self.historical_store
        if store is None:
            return self._load_extra_with_cache(
                name, security.code, loader, max_age
            )
        cached, metadata = store.load_dataset_snapshot(security, name)
        fetched_at = str(metadata.get("fetched_at", ""))
        if not cached.empty and fetched_at:
            try:
                fetched = pd.Timestamp(fetched_at)
                now = pd.Timestamp(beijing_now())
                if fetched.tzinfo is None:
                    fetched = fetched.tz_localize("Asia/Shanghai")
                if now - fetched <= max_age:
                    cached.attrs["source"] = str(
                        metadata.get("source") or default_source or "本地数据库"
                    )
                    return cached
            except (TypeError, ValueError):
                pass
        try:
            frame = self._call_with_timeout(loader, 15)
            if frame is None or frame.empty:
                raise RuntimeError("数据源返回空数据")
            source = str(frame.attrs.get("source") or default_source)
            store.save_dataset_snapshot(
                security,
                name,
                frame,
                source=source,
                as_of_date=latest_completed_market_day(),
            )
            store.update_fetch_state(
                security,
                "dataset",
                name,
                success=True,
                record_count=len(frame),
                source=source,
            )
            frame.attrs["source"] = source
            return frame
        except Exception as exc:
            store.update_fetch_state(
                security,
                "dataset",
                name,
                success=False,
                record_count=len(cached),
                source=str(metadata.get("source", "")),
                error=str(exc),
            )
            if not cached.empty:
                cached.attrs["source"] = (
                    str(metadata.get("source") or default_source or "本地数据库")
                    + "（联网更新失败，使用上次成功数据）"
                )
                return cached
            raise

    @staticmethod
    def _call_with_timeout(
        loader: Callable[[], pd.DataFrame], seconds: float
    ) -> pd.DataFrame:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="data-source")
        future = executor.submit(loader)
        try:
            return future.result(timeout=seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"数据源超过 {seconds:g} 秒未响应") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _normalize_history(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        aliases = {
            "日期": "date",
            "时间": "date",
            "day": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "vol": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "涨跌幅": "pct_change",
            "涨跌额": "change",
            "换手率": "turnover",
        }
        result = frame.rename(columns=aliases).copy()
        required = ("date", "open", "close", "high", "low", "volume")
        missing = [column for column in required if column not in result.columns]
        if missing:
            raise ValueError(f"历史行情缺少字段：{', '.join(missing)}")
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        numeric = [
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
            "amplitude",
            "pct_change",
            "change",
            "turnover",
        ]
        for column in numeric:
            if column not in result:
                result[column] = np.nan
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result.dropna(subset=["date", "open", "close", "high", "low"])
        return (
            result[
                [
                    "date",
                    "open",
                    "close",
                    "high",
                    "low",
                    "volume",
                    "amount",
                    "amplitude",
                    "pct_change",
                    "change",
                    "turnover",
                ]
            ]
            .sort_values("date")
            .drop_duplicates("date")
            .reset_index(drop=True)
        )

    def _read_frame(self, path: Path) -> pd.DataFrame:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            return frame.dropna(subset=["date"]).reset_index(drop=True)
        except (OSError, ValueError, KeyError, pd.errors.ParserError):
            return pd.DataFrame()

    @staticmethod
    def _read_raw_frame(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except (OSError, ValueError, pd.errors.ParserError):
            return pd.DataFrame()

    @staticmethod
    def _cache_is_fresh(path: Path, max_age: timedelta) -> bool:
        if not path.exists():
            return False
        return cache_age_seconds(path) <= max_age.total_seconds()
