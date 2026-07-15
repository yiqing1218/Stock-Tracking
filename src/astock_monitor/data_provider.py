from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd
import requests

from .models import NewsArticle, Quote, Security, SecurityType
from .time_utils import beijing_today, cache_age_seconds


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
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class DataProvider:
    """AkShare 数据适配层，并为网络波动保留最近一次成功缓存。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_sources: dict[str, str] = {}

    def load_universe(self, force: bool = False) -> list[Security]:
        cache_path = self.cache_dir / "security_universe.json"
        # 启动时始终优先读完整本地目录，避免上游超时导致搜索临时只剩内置的少数证券。
        if not force and cache_path.exists():
            try:
                values = json.loads(cache_path.read_text(encoding="utf-8"))
                cached = {item.key: item for item in (Security.from_dict(value) for value in values)}
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
        for loader in (self._load_stock_universe, self._load_etf_universe, self._load_index_universe):
            try:
                for security in loader():
                    universe[security.key] = security
            except Exception as exc:  # 数据源可能临时限流，保留其余类别
                errors.append(exc)

        values = sorted(
            universe.values(),
            key=lambda item: (item.security_type.value, item.code, item.name),
        )
        if len(values) <= len(COMMON_INDICES) + len(COMMON_ETFS) and cache_path.exists():
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
            result.append(Security(code, name, SecurityType.STOCK, infer_market(code, SecurityType.STOCK)))
        return result

    def _load_etf_universe(self) -> list[Security]:
        frame = ak.fund_etf_spot_em()
        result: list[Security] = []
        for _, row in frame.iterrows():
            code = str(row.get("代码", "")).zfill(6)
            name = str(row.get("名称", ""))
            if re.fullmatch(r"\d{6}", code) and name:
                result.append(Security(code, name, SecurityType.ETF, infer_market(code, SecurityType.ETF)))
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
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(securities)))) as executor:
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
                self.cache_dir / f"history_{security.security_type.value}_{security.code}_qfq.csv",
                self.cache_dir / f"history_{security.security_type.value}_{security.code}.csv",
            )
            path = next((item for item in candidates if item.exists()), candidates[0])
            history = self._read_frame(path) if path.exists() else pd.DataFrame()
            if not history.empty:
                quotes[security.key] = self._quote_from_history(security, history)
            else:
                quotes[security.key] = Quote(security=security)
        return quotes

    def refresh_scores(self, securities: list[Security]) -> dict[str, float]:
        """Calculate the same six-dimension score used by the detail page."""

        from .indicators import calculate_indicators, market_regime

        def calculate(security: Security) -> tuple[str, float]:
            path = self.cache_dir / (
                f"score_v2_{security.security_type.value}_{security.code}.json"
            )
            if self._cache_is_fresh(path, timedelta(minutes=10)):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    return security.key, float(value["score"])
                except (OSError, TypeError, ValueError, KeyError):
                    pass
            history = self.get_history(
                security, use_cache=True, adjustment="qfq", include_live=True
            )
            frame = calculate_indicators(history, include_extended=True)
            score = float(market_regime(frame)["score"])
            path.write_text(
                json.dumps(
                    {
                        "score": score,
                        "beijing_time": beijing_today().isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return security.key, score

        scores: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=min(2, max(1, len(securities)))) as executor:
            futures = {executor.submit(calculate, security): security for security in securities}
            for future in as_completed(futures):
                security = futures[future]
                try:
                    key, score = future.result()
                    scores[key] = score
                except Exception:
                    path = self.cache_dir / (
                        f"score_v2_{security.security_type.value}_{security.code}.json"
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
            ("东方财富", self._fetch_eastmoney_quote),
            ("腾讯行情", self._fetch_tencent_quote),
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
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
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
                quote.extra["trade_datetime"] = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
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
            market_cap=(_as_float(values[45]) or 0) * 100_000_000 if len(values) > 45 else None,
            pb=_as_float(values[46]) if len(values) > 46 else None,
        )
        if len(values) > 30 and re.fullmatch(r"\d{14}", values[30] or ""):
            timestamp = datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
            quote.extra["trade_datetime"] = timestamp.isoformat()
        return quote

    def _fetch_sina_quote(self, security: Security) -> Quote:
        symbol = self._market_symbol(security)
        response = requests.get(
            "https://hq.sinajs.cn/list=" + symbol,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
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
        change = price - previous_close if price is not None and previous_close is not None else None
        change_pct = change / previous_close * 100 if change is not None and previous_close else None
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
        change = close - float(previous) if close is not None and pd.notna(previous) else None
        change_pct = change / float(previous) * 100 if change is not None and previous else None
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
        existing_row = existing.iloc[-1] if not existing.empty else pd.Series(dtype=object)
        live_row = {
            "date": trade_timestamp,
            "open": open_value,
            "close": close,
            "high": high,
            "low": low,
            "volume": quote.volume if quote.volume is not None else existing_row.get("volume", np.nan),
            "amount": quote.amount if quote.amount is not None else existing_row.get("amount", np.nan),
            "amplitude": quote.amplitude if adjustment == "" and quote.amplitude is not None else amplitude,
            "pct_change": quote.change_pct if adjustment == "" and quote.change_pct is not None else pct_change,
            "change": quote.change * scale if quote.change is not None else change,
            "turnover": quote.turnover if quote.turnover is not None else existing_row.get("turnover", np.nan),
        }
        result = result[result["date"] != trade_timestamp]
        result = pd.concat([result, pd.DataFrame([live_row])], ignore_index=True)
        result = self._normalize_history(result)
        source = self.history_sources.get(security.key, "日线")
        self.history_sources[security.key] = f"{source} + {quote.extra.get('source', '实时行情')}"
        return result

    def get_history(
        self,
        security: Security,
        calendar_days: int = 1100,
        use_cache: bool = True,
        adjustment: str = "qfq",
        include_live: bool = True,
    ) -> pd.DataFrame:
        if adjustment not in {"", "qfq", "hfq"}:
            raise ValueError("复权方式只支持不复权、前复权或后复权")
        effective_adjustment = "" if security.security_type is SecurityType.INDEX else adjustment
        cache_suffix = effective_adjustment or "raw"
        path = self.cache_dir / (
            f"history_{security.security_type.value}_{security.code}_{cache_suffix}.csv"
        )
        if use_cache and self._cache_is_fresh(path, timedelta(hours=6)):
            cached = self._read_frame(path)
            if not cached.empty:
                self.history_sources[security.key] = "本地缓存"
                return self._merge_live_daily_bar(
                    security, cached, effective_adjustment
                ) if include_live else cached

        end = beijing_today()
        start = end - timedelta(days=calendar_days)
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
                ("AkShare·新浪ETF日线", lambda: ak.fund_etf_hist_sina(symbol=market_symbol)),
            ]
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
                ("AkShare·新浪指数日线", lambda: ak.stock_zh_index_daily(symbol=market_symbol)),
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

        errors: list[str] = []
        for source, loader in loaders:
            try:
                normalized = self._normalize_history(self._call_with_timeout(loader, 12))
                normalized = normalized[
                    (normalized["date"] >= pd.Timestamp(start))
                    & (normalized["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1))
                ]
                if normalized.empty:
                    raise RuntimeError("返回空数据")
                normalized.to_csv(path, index=False, encoding="utf-8-sig")
                self.history_sources[security.key] = source
                normalized = normalized.reset_index(drop=True)
                return self._merge_live_daily_bar(
                    security, normalized, effective_adjustment
                ) if include_live else normalized
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if path.exists():
            cached = self._read_frame(path)
            if not cached.empty:
                self.history_sources[security.key] = "过期本地缓存"
                return self._merge_live_daily_bar(
                    security, cached, effective_adjustment
                ) if include_live else cached
        raise RuntimeError("所有日线接口均不可用：" + "；".join(errors))

    def get_detail_bundle(self, security: Security, adjustment: str = "qfq") -> DetailBundle:
        history = self.get_history(security, adjustment=adjustment, include_live=True)
        bundle = DetailBundle(security=security, history=history)
        bundle.sources["日线"] = self.history_sources.get(security.key, "未知")
        if security.security_type is not SecurityType.STOCK:
            bundle.warnings.append("ETF/指数不提供个股股东与筹码披露数据。")
            return bundle

        extras: tuple[tuple[str, Callable[[], pd.DataFrame], str, timedelta, str], ...] = (
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
                "东财筹码/本地成本模型",
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
        )
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {
                executor.submit(
                    self._load_extra_with_cache, attribute, security.code, loader, max_age
                ): (attribute, warning, source)
                for attribute, loader, warning, max_age, source in extras
            }
            for future in as_completed(future_map):
                attribute, warning, source = future_map[future]
                try:
                    frame = future.result()
                    setattr(bundle, attribute, frame if frame is not None else pd.DataFrame())
                    bundle.sources[attribute] = str(
                        getattr(frame, "attrs", {}).get("source", source)
                    )
                except Exception:
                    bundle.warnings.append(warning)
        return bundle

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
        matched = frame[
            frame[code_column].map(self._normalized_code) == security.code
        ]
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
                    "超大单净流入-净额": number("今日超大单净流入-净额", "超大单净流入-净额"),
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
                return frame
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        estimated = self._estimate_fund_flow(history)
        estimated.attrs["source"] = "本地OHLCV资金流估算（非逐笔主力）"
        estimated.attrs["fallback_errors"] = errors
        return estimated

    def _estimate_fund_flow(self, history: pd.DataFrame) -> pd.DataFrame:
        if history is None or history.empty:
            return pd.DataFrame()
        frame = history.copy().tail(120)
        span = (frame["high"] - frame["low"]).replace(0, np.nan)
        multiplier = (((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])) / span).fillna(0)
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3
        amount = pd.to_numeric(frame.get("amount"), errors="coerce")
        amount = amount.where(amount > 0, typical * pd.to_numeric(frame["volume"], errors="coerce") * 100)
        main = multiplier * amount
        return pd.DataFrame(
            {
                "日期": pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d"),
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
        try:
            frame = self._call_with_timeout(
                lambda: ak.stock_cyq_em(symbol=security.code, adjust=adjustment),
                6,
            )
            if frame is None or frame.empty:
                raise RuntimeError("筹码接口返回空数据")
            frame = frame.copy()
            frame.attrs["source"] = "AkShare·东方财富筹码分布"
            return frame
        except Exception as exc:
            frame = self._estimate_chips(history)
            frame.attrs["source"] = "本地换手衰减成本模型估算"
            frame.attrs["fallback_error"] = str(exc)
            return frame

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
        path = self.cache_dir / (
            f"intraday_{security.security_type.value}_{security.code}_{trading_day:%Y%m%d}_{period}.csv"
        )
        max_age = (
            timedelta(seconds=20)
            if trading_day == beijing_today()
            else timedelta(days=3650)
        )
        if self._cache_is_fresh(path, max_age):
            cached = self._read_frame(path)
            if not cached.empty:
                return cached, "本地分时缓存"

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
                        period=period,
                        adjust="",
                    ),
                ),
                (
                    "AkShare·新浪股票分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period=period, adjust=""
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
                        period=period,
                        adjust="",
                    ),
                ),
                (
                    "AkShare·新浪ETF分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period=period, adjust=""
                    ),
                ),
            ]
        else:
            loaders = [
                (
                    "AkShare·东方财富指数分时",
                    lambda: ak.index_zh_a_hist_min_em(
                        symbol=security.code,
                        period=period,
                        start_date=start,
                        end_date=end,
                    ),
                ),
                (
                    "AkShare·新浪指数分时",
                    lambda: ak.stock_zh_a_minute(
                        symbol=market_symbol, period=period, adjust=""
                    ),
                ),
            ]

        errors: list[str] = []
        for source, loader in loaders:
            try:
                frame = self._normalize_history(self._call_with_timeout(loader, 10))
                frame = frame[frame["date"].dt.date == trading_day].reset_index(drop=True)
                if frame.empty:
                    raise RuntimeError("该日期无分时数据")
                frame.to_csv(path, index=False, encoding="utf-8-sig")
                return frame, source
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        raise RuntimeError(
            "该日期暂无可用分时数据（1分钟通常仅保留近5个交易日）：" + "；".join(errors)
        )

    def get_news(self, security: Security, force: bool = False) -> list[NewsArticle]:
        path = self.cache_dir / f"news_v3_{security.security_type.value}_{security.code}.json"
        if not force and self._cache_is_fresh(path, timedelta(minutes=10)):
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
                return [NewsArticle(**value) for value in values]
            except (OSError, TypeError, ValueError):
                pass

        articles: list[NewsArticle] = []
        keyword = security.code if security.security_type is SecurityType.STOCK else security.name
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
            source = source_node.text if source_node is not None else urlparse(link).netloc
            result.append(
                NewsArticle(
                    title=self._clean_news_text(item.findtext("title", "")),
                    summary=self._short_news_text(item.findtext("description", "")),
                    source=self._clean_news_text(
                        source or "Bing 联网搜索"
                    ),
                    published_at=self._normalize_news_time(item.findtext("pubDate", "")),
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
                        cached.attrs["source"] = source_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                return cached
        try:
            frame = self._call_with_timeout(loader, 15)
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
                        cached.attrs["source"] = source_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                return cached
            raise

    @staticmethod
    def _call_with_timeout(loader: Callable[[], pd.DataFrame], seconds: float) -> pd.DataFrame:
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
        return result[
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
        ].sort_values("date").drop_duplicates("date").reset_index(drop=True)

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
