from __future__ import annotations

from dataclasses import dataclass
import re
import warnings

import numpy as np
import pandas as pd
import pandas_ta_classic as pta


EPSILON = 1e-12
INDICATOR_DIMENSIONS = ("趋势", "动量", "波动", "量能", "情绪", "风险")


def resample_ohlcv(source: pd.DataFrame, period: str) -> pd.DataFrame:
    """Aggregate daily OHLCV into live weekly or monthly candles."""

    if period == "daily":
        return source.copy().reset_index(drop=True)
    rules = {"weekly": "W-FRI", "monthly": "ME"}
    if period not in rules:
        raise ValueError("K线周期只支持 daily、weekly、monthly")
    if source is None or source.empty:
        return pd.DataFrame()
    frame = source.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    frame["_actual_date"] = frame["date"]
    frame = frame.set_index("date")
    aggregations: dict[str, str] = {
        "_actual_date": "max",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    for column in ("amount", "turnover"):
        if column in frame:
            aggregations[column] = "sum"
    result = frame.resample(rules[period]).agg(aggregations)
    result = result.dropna(subset=["open", "high", "low", "close"])
    result = result.rename(columns={"_actual_date": "date"}).reset_index(drop=True)
    previous = result["close"].shift(1)
    result["change"] = result["close"] - previous
    result["pct_change"] = safe_div(result["change"], previous) * 100
    result["amplitude"] = safe_div(result["high"] - result["low"], previous) * 100
    for column in ("amount", "turnover"):
        if column not in result:
            result[column] = np.nan
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
    ]


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wma(series: pd.Series, window: int) -> pd.Series:
    weights = np.arange(1, window + 1, dtype=float)
    return series.rolling(window, min_periods=window).apply(
        lambda values: float(np.dot(values, weights) / weights.sum()),
        raw=True,
    )


def wilder(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0, np.nan)


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def rsi(series: pd.Series, window: int) -> pd.Series:
    delta = series.diff()
    average_gain = wilder(delta.clip(lower=0), window)
    average_loss = wilder(-delta.clip(upper=0), window)
    strength = safe_div(average_gain, average_loss)
    result = 100 - 100 / (1 + strength)
    return result.where(average_loss > EPSILON, 100.0)


def rolling_mad(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).apply(
        lambda values: float(np.mean(np.abs(values - np.mean(values)))),
        raw=True,
    )


def parabolic_sar(frame: pd.DataFrame, step: float = 0.02, maximum: float = 0.2) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float, index=frame.index)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    values = np.full(len(frame), np.nan)
    if len(frame) < 2:
        values[0] = low[0]
        return pd.Series(values, index=frame.index)

    uptrend = close[1] >= close[0]
    acceleration = step
    extreme = high[0] if uptrend else low[0]
    values[0] = low[0] if uptrend else high[0]
    for index in range(1, len(frame)):
        candidate = values[index - 1] + acceleration * (extreme - values[index - 1])
        if uptrend:
            candidate = min(candidate, low[index - 1])
            if index > 1:
                candidate = min(candidate, low[index - 2])
            if low[index] < candidate:
                uptrend = False
                candidate = extreme
                extreme = low[index]
                acceleration = step
            elif high[index] > extreme:
                extreme = high[index]
                acceleration = min(maximum, acceleration + step)
        else:
            candidate = max(candidate, high[index - 1])
            if index > 1:
                candidate = max(candidate, high[index - 2])
            if high[index] > candidate:
                uptrend = True
                candidate = extreme
                extreme = high[index]
                acceleration = step
            elif low[index] < extreme:
                extreme = low[index]
                acceleration = min(maximum, acceleration + step)
        values[index] = candidate
    return pd.Series(values, index=frame.index)


def supertrend(frame: pd.DataFrame, window: int = 10, multiplier: float = 3.0) -> pd.Series:
    atr = wilder(true_range(frame), window)
    middle = (frame["high"] + frame["low"]) / 2
    upper = (middle + multiplier * atr).to_numpy(dtype=float).copy()
    lower = (middle - multiplier * atr).to_numpy(dtype=float).copy()
    close = frame["close"].to_numpy(dtype=float)
    result = np.full(len(frame), np.nan)
    direction_up = True
    for index in range(1, len(frame)):
        if np.isnan(upper[index]) or np.isnan(lower[index]):
            continue
        if not np.isnan(upper[index - 1]) and close[index - 1] <= upper[index - 1]:
            upper[index] = min(upper[index], upper[index - 1])
        if not np.isnan(lower[index - 1]) and close[index - 1] >= lower[index - 1]:
            lower[index] = max(lower[index], lower[index - 1])
        if direction_up and close[index] < lower[index]:
            direction_up = False
        elif not direction_up and close[index] > upper[index]:
            direction_up = True
        result[index] = lower[index] if direction_up else upper[index]
    return pd.Series(result, index=frame.index)


_PTA_DIMENSION_BY_CATEGORY = {
    "candles": "情绪",
    "cycles": "动量",
    "momentum": "动量",
    "overlap": "趋势",
    "performance": "风险",
    "statistics": "风险",
    "trend": "趋势",
    "volatility": "波动",
    "volume": "量能",
}

_PTA_OUTPUT_ALIASES = {
    "isa": "趋势",
    "isb": "趋势",
    "its": "趋势",
    "iks": "趋势",
    "ics": "趋势",
    "dmp": "趋势",
    "dmn": "趋势",
    "vtxp": "趋势",
    "vtxm": "趋势",
    "ttmtrnd": "趋势",
    "psarl": "趋势",
    "psars": "趋势",
    "psaraf": "趋势",
    "psarr": "趋势",
    "bbl": "波动",
    "bbm": "波动",
    "bbu": "波动",
    "bbb": "波动",
    "bbp": "波动",
    "dcl": "波动",
    "dcm": "波动",
    "dcu": "波动",
    "kcl": "波动",
    "kcb": "波动",
    "kcu": "波动",
    "hwcl": "波动",
    "hwcm": "波动",
    "hwcu": "波动",
    "sqz": "动量",
    "sqzpro": "动量",
    "cdl": "情绪",
    "ha": "情绪",
}


def _normalized_indicator_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _extended_dimension(output_name: str) -> str:
    normalized = _normalized_indicator_name(output_name)
    for prefix, dimension in sorted(
        _PTA_OUTPUT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if normalized.startswith(prefix):
            return dimension
    candidates: list[tuple[int, str]] = []
    for category, names in pta.Category.items():
        dimension = _PTA_DIMENSION_BY_CATEGORY.get(category, "风险")
        for name in names:
            normalized_name = _normalized_indicator_name(name)
            if normalized.startswith(normalized_name):
                candidates.append((len(normalized_name), dimension))
    return max(candidates, default=(0, "风险"))[1]


def _calculate_extended_indicators(
    source: pd.DataFrame,
) -> tuple[pd.DataFrame, list["IndicatorDefinition"]]:
    if source.empty:
        return pd.DataFrame(index=source.index), []
    base_columns = [column for column in ("open", "high", "low", "close", "volume") if column in source]
    pta_frame = source.copy()
    if "date" in pta_frame:
        pta_frame.index = pd.to_datetime(pta_frame["date"], errors="coerce")
    pta_frame = pta_frame[base_columns]
    # Pandas TA Classic's accessor is not cached; configure and reuse the same
    # instance or a second ``df.ta`` access silently restores cpu_count().
    # A single-process run is essential inside a Qt worker on Windows: spawning
    # one process per CPU causes very high memory use and can recursively import
    # the desktop entry point.
    accessor = pta_frame.ta
    accessor.cores = 0
    original_count = len(pta_frame.columns)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        accessor.strategy()
    generated = pta_frame.iloc[:, original_count:]
    series_map: dict[str, pd.Series] = {}
    definitions: list[IndicatorDefinition] = []
    name_counts: dict[str, int] = {}
    for position, raw_column in enumerate(generated.columns):
        raw_name = str(raw_column)
        numeric = pd.to_numeric(generated.iloc[:, position], errors="coerce").reset_index(drop=True)
        if numeric.notna().sum() == 0:
            continue
        name_counts[raw_name] = name_counts.get(raw_name, 0) + 1
        suffix = f"_{name_counts[raw_name]}" if name_counts[raw_name] > 1 else ""
        column = f"PTA_{raw_name}{suffix}"
        series_map[column] = numeric
        definitions.append(
            IndicatorDefinition(
                _extended_dimension(raw_name),
                raw_name + suffix,
                column,
                f"Pandas TA Classic 公开指标输出：{raw_name}",
            )
        )
    return pd.DataFrame(series_map, index=source.index), definitions


def calculate_indicators(
    source: pd.DataFrame, include_extended: bool = False
) -> pd.DataFrame:
    required = {"open", "close", "high", "low", "volume"}
    missing = required.difference(source.columns)
    if missing:
        raise ValueError(f"行情数据缺少字段：{', '.join(sorted(missing))}")
    frame = source.copy()
    for column in ["open", "close", "high", "low", "volume", "amount", "turnover"]:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    open_price = frame["open"]
    volume = frame["volume"].fillna(0)
    amount = frame["amount"]
    returns = close.pct_change(fill_method=None)
    log_returns = np.log(close / close.shift(1))
    frame["returns"] = returns * 100

    # 趋势与均线
    for window in (5, 10, 20, 60, 120, 250):
        frame[f"SMA_{window}"] = sma(close, window)
    for span in (5, 12, 20, 26, 60):
        frame[f"EMA_{span}"] = ema(close, span)
    frame["WMA_20"] = wma(close, 20)
    frame["BBI"] = (sma(close, 3) + sma(close, 6) + sma(close, 12) + sma(close, 24)) / 4
    frame["DMA"] = sma(close, 10) - sma(close, 50)
    frame["DMA_SIGNAL"] = sma(frame["DMA"], 10)
    frame["MACD_DIF"] = frame["EMA_12"] - frame["EMA_26"]
    frame["MACD_DEA"] = ema(frame["MACD_DIF"], 9)
    frame["MACD_HIST"] = 2 * (frame["MACD_DIF"] - frame["MACD_DEA"])
    for window in (6, 12, 24):
        frame[f"BIAS_{window}"] = (safe_div(close, sma(close, window)) - 1) * 100

    tr = true_range(frame)
    frame["TR"] = tr
    frame["ATR_14"] = wilder(tr, 14)
    frame["NATR_14"] = safe_div(frame["ATR_14"], close) * 100

    upward = high.diff()
    downward = -low.diff()
    plus_dm = pd.Series(np.where((upward > downward) & (upward > 0), upward, 0.0), index=frame.index)
    minus_dm = pd.Series(np.where((downward > upward) & (downward > 0), downward, 0.0), index=frame.index)
    frame["PLUS_DI_14"] = safe_div(wilder(plus_dm, 14), frame["ATR_14"]) * 100
    frame["MINUS_DI_14"] = safe_div(wilder(minus_dm, 14), frame["ATR_14"]) * 100
    dx = safe_div(
        (frame["PLUS_DI_14"] - frame["MINUS_DI_14"]).abs(),
        frame["PLUS_DI_14"] + frame["MINUS_DI_14"],
    ) * 100
    frame["ADX_14"] = wilder(dx, 14)

    aroon_window = 25
    frame["AROON_UP_25"] = high.rolling(aroon_window).apply(
        lambda values: (np.argmax(values) + 1) / aroon_window * 100,
        raw=True,
    )
    frame["AROON_DOWN_25"] = low.rolling(aroon_window).apply(
        lambda values: (np.argmin(values) + 1) / aroon_window * 100,
        raw=True,
    )
    frame["AROON_OSC_25"] = frame["AROON_UP_25"] - frame["AROON_DOWN_25"]
    frame["PSAR"] = parabolic_sar(frame)
    frame["SUPERTREND"] = supertrend(frame)

    frame["ICHIMOKU_TENKAN"] = (high.rolling(9).max() + low.rolling(9).min()) / 2
    frame["ICHIMOKU_KIJUN"] = (high.rolling(26).max() + low.rolling(26).min()) / 2
    frame["ICHIMOKU_SENKOU_A"] = (
        (frame["ICHIMOKU_TENKAN"] + frame["ICHIMOKU_KIJUN"]) / 2
    ).shift(26)
    frame["ICHIMOKU_SENKOU_B"] = (
        (high.rolling(52).max() + low.rolling(52).min()) / 2
    ).shift(26)
    frame["ICHIMOKU_CHIKOU"] = close.shift(-26)
    frame["LINREG_SLOPE_20"] = close.rolling(20).apply(
        lambda values: float(np.polyfit(np.arange(len(values)), values, 1)[0]),
        raw=True,
    )

    # 动量、摆动与情绪
    for window in (6, 12, 14, 24):
        frame[f"RSI_{window}"] = rsi(close, window)
    lowest_9 = low.rolling(9).min()
    highest_9 = high.rolling(9).max()
    rsv = safe_div(close - lowest_9, highest_9 - lowest_9) * 100
    frame["KDJ_K"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    frame["KDJ_D"] = frame["KDJ_K"].ewm(alpha=1 / 3, adjust=False).mean()
    frame["KDJ_J"] = 3 * frame["KDJ_K"] - 2 * frame["KDJ_D"]
    frame["STOCH_K_14"] = safe_div(close - low.rolling(14).min(), high.rolling(14).max() - low.rolling(14).min()) * 100
    frame["STOCH_D_3"] = sma(frame["STOCH_K_14"], 3)
    frame["WILLR_14"] = -100 * safe_div(high.rolling(14).max() - close, high.rolling(14).max() - low.rolling(14).min())
    frame["ROC_12"] = close.pct_change(12, fill_method=None) * 100
    frame["MOM_10"] = close - close.shift(10)
    typical = (high + low + close) / 3
    frame["CCI_20"] = safe_div(typical - sma(typical, 20), 0.015 * rolling_mad(typical, 20))
    gains = close.diff().clip(lower=0).rolling(14).sum()
    losses = (-close.diff().clip(upper=0)).rolling(14).sum()
    frame["CMO_14"] = safe_div(gains - losses, gains + losses) * 100
    triple_ema = ema(ema(ema(close, 12), 12), 12)
    frame["TRIX_12"] = triple_ema.pct_change(fill_method=None) * 100
    frame["TRIX_SIGNAL_9"] = sma(frame["TRIX_12"], 9)
    frame["PPO"] = safe_div(frame["EMA_12"] - frame["EMA_26"], frame["EMA_26"]) * 100
    frame["PPO_SIGNAL"] = ema(frame["PPO"], 9)
    buying_pressure = close - pd.concat([low, close.shift(1)], axis=1).min(axis=1)
    true_range_uo = pd.concat([high, close.shift(1)], axis=1).max(axis=1) - pd.concat([low, close.shift(1)], axis=1).min(axis=1)
    average_7 = safe_div(buying_pressure.rolling(7).sum(), true_range_uo.rolling(7).sum())
    average_14 = safe_div(buying_pressure.rolling(14).sum(), true_range_uo.rolling(14).sum())
    average_28 = safe_div(buying_pressure.rolling(28).sum(), true_range_uo.rolling(28).sum())
    frame["ULTOSC"] = 100 * (4 * average_7 + 2 * average_14 + average_28) / 7
    frame["PSY_12"] = (close.diff() > 0).rolling(12).mean() * 100
    frame["DPO_20"] = close.shift(11) - sma(close, 20)

    # 波动率与通道
    frame["BB_MID"] = sma(close, 20)
    bb_std = close.rolling(20).std(ddof=0)
    frame["BB_UPPER"] = frame["BB_MID"] + 2 * bb_std
    frame["BB_LOWER"] = frame["BB_MID"] - 2 * bb_std
    frame["BB_WIDTH"] = safe_div(frame["BB_UPPER"] - frame["BB_LOWER"], frame["BB_MID"]) * 100
    frame["BB_PERCENT_B"] = safe_div(close - frame["BB_LOWER"], frame["BB_UPPER"] - frame["BB_LOWER"]) * 100
    frame["HV_20"] = log_returns.rolling(20).std(ddof=0) * np.sqrt(252) * 100
    frame["KELTNER_MID"] = ema(close, 20)
    frame["KELTNER_UPPER"] = frame["KELTNER_MID"] + 2 * frame["ATR_14"]
    frame["KELTNER_LOWER"] = frame["KELTNER_MID"] - 2 * frame["ATR_14"]
    frame["DONCHIAN_UPPER_20"] = high.rolling(20).max()
    frame["DONCHIAN_LOWER_20"] = low.rolling(20).min()
    frame["CHAIKIN_VOL_10"] = ema(high - low, 10).pct_change(10, fill_method=None) * 100
    rolling_peak = close.rolling(20).max()
    drawdown_20 = (safe_div(close, rolling_peak) - 1) * 100
    frame["ULCER_20"] = np.sqrt(drawdown_20.pow(2).rolling(20).mean())

    # 成交量与资金行为
    direction = np.sign(close.diff()).fillna(0)
    frame["OBV"] = (direction * volume).cumsum()
    money_flow_multiplier = safe_div((close - low) - (high - close), high - low).fillna(0)
    frame["ADL"] = (money_flow_multiplier * volume).cumsum()
    frame["CMF_20"] = safe_div((money_flow_multiplier * volume).rolling(20).sum(), volume.rolling(20).sum())
    frame["CHAIKIN_OSC"] = ema(frame["ADL"], 3) - ema(frame["ADL"], 10)
    raw_money_flow = typical * volume
    positive_flow = raw_money_flow.where(typical.diff() > 0, 0.0)
    negative_flow = raw_money_flow.where(typical.diff() < 0, 0.0)
    money_ratio = safe_div(positive_flow.rolling(14).sum(), negative_flow.rolling(14).sum())
    frame["MFI_14"] = 100 - 100 / (1 + money_ratio)
    frame["PVT"] = (returns.fillna(0) * volume).cumsum()
    frame["FORCE_13"] = ema(close.diff() * volume, 13)
    midpoint_move = ((high + low) / 2).diff()
    box_ratio = safe_div((volume / 100_000_000), high - low)
    frame["EMV_14"] = sma(safe_div(midpoint_move, box_ratio), 14)
    frame["VOLUME_MA_5"] = sma(volume, 5)
    frame["VOLUME_MA_20"] = sma(volume, 20)
    frame["VOLUME_RATIO_5"] = safe_div(volume, sma(volume.shift(1), 5))
    estimated_vwap = safe_div(amount, volume * 100)
    fallback_vwap = safe_div((typical * volume).cumsum(), volume.cumsum())
    frame["VWAP"] = estimated_vwap.where(estimated_vwap.notna() & (estimated_vwap > 0), fallback_vwap)

    # 中国市场常见人气/买卖意愿指标
    previous_close = close.shift(1)
    frame["AR_26"] = safe_div(
        (high - open_price).rolling(26).sum(),
        (open_price - low).rolling(26).sum(),
    ) * 100
    frame["BR_26"] = safe_div(
        (high - previous_close).clip(lower=0).rolling(26).sum(),
        (previous_close - low).clip(lower=0).rolling(26).sum(),
    ) * 100
    frame["CR_26"] = safe_div(
        (high - typical.shift(1)).clip(lower=0).rolling(26).sum(),
        (typical.shift(1) - low).clip(lower=0).rolling(26).sum(),
    ) * 100

    # 收益与风险统计。复制一次可消除大量逐列计算造成的 DataFrame 碎片。
    frame = frame.copy()
    for window in (1, 5, 20, 60, 120, 250):
        frame[f"RETURN_{window}D"] = close.pct_change(window, fill_method=None) * 100
    frame["ROLLING_SHARPE_60"] = safe_div(returns.rolling(60).mean(), returns.rolling(60).std(ddof=0)) * np.sqrt(252)
    frame["VAR_95_60"] = returns.rolling(60).quantile(0.05) * 100
    frame["SKEW_60"] = returns.rolling(60).skew()
    frame["KURT_60"] = returns.rolling(60).kurt()
    cumulative_peak = close.cummax()
    frame["DRAWDOWN"] = (safe_div(close, cumulative_peak) - 1) * 100

    # K线形态（1 表示看多，-1 表示看空）
    body = (close - open_price).abs()
    candle_range = (high - low).replace(0, np.nan)
    upper_shadow = high - pd.concat([close, open_price], axis=1).max(axis=1)
    lower_shadow = pd.concat([close, open_price], axis=1).min(axis=1) - low
    frame["PATTERN_DOJI"] = (body <= candle_range * 0.1).astype(float)
    frame["PATTERN_HAMMER"] = ((lower_shadow >= body * 2) & (upper_shadow <= body)).astype(float)
    frame["PATTERN_SHOOTING_STAR"] = -((upper_shadow >= body * 2) & (lower_shadow <= body)).astype(float)
    bullish_engulf = (close > open_price) & (close.shift(1) < open_price.shift(1)) & (close >= open_price.shift(1)) & (open_price <= close.shift(1))
    bearish_engulf = (close < open_price) & (close.shift(1) > open_price.shift(1)) & (open_price >= close.shift(1)) & (close <= open_price.shift(1))
    frame["PATTERN_ENGULFING"] = bullish_engulf.astype(float) - bearish_engulf.astype(float)

    frame = frame.replace([np.inf, -np.inf], np.nan)
    extended_definitions: list[IndicatorDefinition] = []
    if include_extended:
        extended, extended_definitions = _calculate_extended_indicators(source.reset_index(drop=True))
        duplicate_columns = [column for column in extended if column in frame]
        if duplicate_columns:
            extended = extended.drop(columns=duplicate_columns)
        frame = pd.concat([frame.reset_index(drop=True), extended.reset_index(drop=True)], axis=1)
    frame.attrs["extended_indicator_definitions"] = extended_definitions
    return frame


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    category: str
    name: str
    column: str
    description: str
    unit: str = ""
    origin: str = "系统"
    key: str = ""

    @property
    def identifier(self) -> str:
        return self.key or f"system:{self.column}"


_CLASSIC_WEIGHT_PREFIXES = (
    "SMA_20",
    "SMA_60",
    "MACD_",
    "ADX_",
    "PLUS_DI_",
    "MINUS_DI_",
    "RSI_14",
    "KDJ_",
    "BB_",
    "ATR_",
    "OBV",
    "MFI_",
    "CMF_",
    "VWAP",
    "DRAWDOWN",
)


def indicator_weight(definition: IndicatorDefinition) -> float:
    """Preset scoring weight; classic, interpretable indicators lead the score."""

    if definition.origin == "自定义":
        return 1.0
    if definition.column.startswith("PTA_"):
        return 0.35
    if definition.column.startswith(_CLASSIC_WEIGHT_PREFIXES):
        return 2.5
    if definition.column.startswith(("RETURN_", "HV_", "NATR_", "PSY_", "BIAS_")):
        return 1.8
    return 1.25


_DIMENSION_EXPLANATIONS = {
    "趋势": "用于判断方向、排列和趋势持续性；连续信号比单日信号更可靠，震荡市中容易反复失真。",
    "动量": "衡量价格推进速度与超买超卖位置；极值表示强弱程度，不等同于立即反转。",
    "波动": "描述价格振幅、通道宽度和风险扩张；高低本身不代表涨跌方向，应结合趋势使用。",
    "量能": "把成交量、成交额或资金压力与价格结合；放量要同时观察价格方向和所处位置。",
    "情绪": "刻画短期买卖意愿、K线行为和市场温度；对消息与跳空较敏感，宜用多日确认。",
    "风险": "衡量收益分布、回撤和尾部风险；统计结果依赖样本窗口，不代表未来损失上限。",
}

_INDICATOR_EXPLANATIONS = {
    "MACD": "由快慢指数均线及其信号线构成，重点看零轴位置、交叉方向和柱体扩张或收缩。",
    "RSI": "比较一定窗口内平均上涨幅度与平均下跌幅度，常用 30/70 或 20/80 观察强弱区间。",
    "ADX": "由方向运动系统推导趋势强度，数值升高代表趋势增强，但不直接说明上涨或下跌。",
    "KDJ": "以近期高低区间定位收盘价，并对随机值平滑；J 值更敏感，也更容易出现极端值。",
    "STOCH": "比较收盘价在近期最高价与最低价区间中的位置，适合观察短周期动量转折。",
    "WILLR": "以负值表示收盘价距近期高点的位置，接近 0 偏强，接近 -100 偏弱。",
    "BB_": "以移动均线为中轨、标准差为带宽构造通道，可同时观察趋势位置与波动收缩扩张。",
    "BOLL": "以移动均线为中轨、标准差为带宽构造通道，可同时观察趋势位置与波动收缩扩张。",
    "ATR": "综合当日振幅和跳空缺口估计真实波幅，常用于仓位、止损距离和波动比较。",
    "OBV": "按涨跌方向累计成交量，观察量能趋势是否与价格趋势确认或背离。",
    "MFI": "把典型价格与成交量合成资金流强弱，逻辑类似带成交量权重的 RSI。",
    "CMF": "根据收盘价在日内高低区间的位置对成交量加权，正值偏向净流入压力。",
    "VWAP": "以成交额除以成交量得到成交均价参考，可用于比较现价与市场平均成交成本。",
    "PSAR": "用随趋势加速的抛物线点位跟踪方向，反转时点位会从价格一侧切换到另一侧。",
    "SUPERTREND": "以 ATR 波动带跟踪趋势，价格穿越跟踪线时切换方向，参数越小越灵敏。",
    "ICHIMOKU": "用多组周期中点构成转换线、基准线和云层，综合判断趋势、支撑阻力与位置。",
    "AROON": "比较近期最高点和最低点距当前的时间，判断新高或新低出现的活跃程度。",
    "CCI": "衡量典型价格偏离其移动平均的程度，绝对值扩大表示价格偏离增强。",
    "ROC": "计算当前价格相对若干周期前的百分比变化，零轴上方代表正向动量。",
    "DRAWDOWN": "计算当前价格相对历史高点的跌幅，用于识别尚未修复的资本回撤。",
    "VAR_": "用历史收益分位数估计给定置信水平下的单期损失阈值，不覆盖所有极端风险。",
}


def detailed_indicator_description(definition: IndicatorDefinition) -> str:
    """Return a calculation, interpretation and limitation oriented description."""

    name = f"{definition.name} {definition.column}".upper()
    specific = next(
        (text for key, text in _INDICATOR_EXPLANATIONS.items() if key in name),
        "该条目基于当前证券的公开 OHLCV、成交额或其滚动统计结果计算，最新值使用最近一个有效交易点。",
    )
    dimension = _DIMENSION_EXPLANATIONS.get(definition.category, "应与价格、成交量和市场环境交叉验证。")
    source = (
        "这是用户公式生成的本地自定义指标。"
        if definition.origin == "自定义"
        else "这是系统指标，计算不依赖交易账户或私有持仓数据。"
    )
    base = definition.description.rstrip("。； ")
    return (
        f"{base}。计算与解读：{specific}{dimension}"
        f"{source} 六维评分预设权重 {indicator_weight(definition):.2f}。"
    )


INDICATOR_CATALOG = [
    IndicatorDefinition("趋势", "MA5", "SMA_5", "5日平均价格，反映超短线成本", "元"),
    IndicatorDefinition("趋势", "MA10", "SMA_10", "10日平均价格，反映短线趋势", "元"),
    IndicatorDefinition("趋势", "MA20", "SMA_20", "20日平均价格，中短期趋势基准", "元"),
    IndicatorDefinition("趋势", "MA60", "SMA_60", "60日平均价格，中期趋势基准", "元"),
    IndicatorDefinition("趋势", "MA120", "SMA_120", "120日平均价格，长期趋势参考", "元"),
    IndicatorDefinition("趋势", "MA250", "SMA_250", "250日年线，长期多空分界参考", "元"),
    IndicatorDefinition("趋势", "EMA12", "EMA_12", "近期权重更高的12日指数均线", "元"),
    IndicatorDefinition("趋势", "EMA26", "EMA_26", "MACD的长期指数均线", "元"),
    IndicatorDefinition("趋势", "MACD DIF", "MACD_DIF", "EMA12与EMA26之差"),
    IndicatorDefinition("趋势", "MACD DEA", "MACD_DEA", "DIF的9日指数平滑线"),
    IndicatorDefinition("趋势", "MACD柱", "MACD_HIST", "趋势动量加速度，DIF与DEA差的两倍"),
    IndicatorDefinition("趋势", "ADX14", "ADX_14", "趋势强度，不判断方向", "%"),
    IndicatorDefinition("趋势", "+DI14", "PLUS_DI_14", "上行方向运动强度", "%"),
    IndicatorDefinition("趋势", "-DI14", "MINUS_DI_14", "下行方向运动强度", "%"),
    IndicatorDefinition("趋势", "Aroon振荡", "AROON_OSC_25", "近期高低点出现时间差，衡量趋势方向", "%"),
    IndicatorDefinition("趋势", "抛物线SAR", "PSAR", "跟踪趋势并提供移动止损参考", "元"),
    IndicatorDefinition("趋势", "Supertrend", "SUPERTREND", "ATR通道型趋势跟踪线", "元"),
    IndicatorDefinition("趋势", "一目均衡转换线", "ICHIMOKU_TENKAN", "9周期高低点中值", "元"),
    IndicatorDefinition("趋势", "一目均衡基准线", "ICHIMOKU_KIJUN", "26周期高低点中值", "元"),
    IndicatorDefinition("趋势", "20日线性斜率", "LINREG_SLOPE_20", "20日收盘价回归线每日斜率"),
    IndicatorDefinition("趋势", "BBI多空线", "BBI", "3/6/12/24日均线的综合均值", "元"),
    IndicatorDefinition("趋势", "DMA", "DMA", "10日均线与50日均线之差"),
    IndicatorDefinition("动量", "RSI6", "RSI_6", "短周期相对强弱", "%"),
    IndicatorDefinition("动量", "RSI14", "RSI_14", "14日平均涨跌强度", "%"),
    IndicatorDefinition("动量", "RSI24", "RSI_24", "较慢的相对强弱", "%"),
    IndicatorDefinition("动量", "KDJ-K", "KDJ_K", "当前收盘在9日区间的位置", "%"),
    IndicatorDefinition("动量", "KDJ-D", "KDJ_D", "K值平滑线", "%"),
    IndicatorDefinition("动量", "KDJ-J", "KDJ_J", "放大的极端情绪线", "%"),
    IndicatorDefinition("动量", "随机指标%K", "STOCH_K_14", "收盘价在14日高低区间的位置", "%"),
    IndicatorDefinition("动量", "威廉%R", "WILLR_14", "接近0偏强，接近-100偏弱", "%"),
    IndicatorDefinition("动量", "ROC12", "ROC_12", "12日价格变化率", "%"),
    IndicatorDefinition("动量", "MOM10", "MOM_10", "当前价与10日前价格差", "元"),
    IndicatorDefinition("动量", "CCI20", "CCI_20", "价格偏离典型价格均值的程度"),
    IndicatorDefinition("动量", "CMO14", "CMO_14", "净上涨动量占总动量比例", "%"),
    IndicatorDefinition("动量", "TRIX12", "TRIX_12", "三重EMA变化率，过滤短期噪声", "%"),
    IndicatorDefinition("动量", "PPO", "PPO", "百分比价格振荡器，可跨价格比较", "%"),
    IndicatorDefinition("动量", "终极振荡器", "ULTOSC", "融合7/14/28周期的多尺度动量", "%"),
    IndicatorDefinition("情绪", "PSY12", "PSY_12", "12日上涨天数占比", "%"),
    IndicatorDefinition("情绪", "BIAS6", "BIAS_6", "价格相对6日均线的乖离", "%"),
    IndicatorDefinition("情绪", "BIAS12", "BIAS_12", "价格相对12日均线的乖离", "%"),
    IndicatorDefinition("情绪", "AR26", "AR_26", "开盘后买卖意愿强弱", "%"),
    IndicatorDefinition("情绪", "BR26", "BR_26", "相对昨收的买卖意愿强弱", "%"),
    IndicatorDefinition("情绪", "CR26", "CR_26", "相对昨日中价的多空力量", "%"),
    IndicatorDefinition("波动", "布林上轨", "BB_UPPER", "20日均线加2倍标准差", "元"),
    IndicatorDefinition("波动", "布林中轨", "BB_MID", "20日均线", "元"),
    IndicatorDefinition("波动", "布林下轨", "BB_LOWER", "20日均线减2倍标准差", "元"),
    IndicatorDefinition("波动", "布林带宽", "BB_WIDTH", "布林带相对宽度，收口表示波动压缩", "%"),
    IndicatorDefinition("波动", "布林%B", "BB_PERCENT_B", "价格在布林上下轨中的相对位置", "%"),
    IndicatorDefinition("波动", "ATR14", "ATR_14", "包含跳空的14日真实波幅", "元"),
    IndicatorDefinition("波动", "NATR14", "NATR_14", "ATR占价格比例，便于跨标的比较", "%"),
    IndicatorDefinition("波动", "20日历史波动率", "HV_20", "日对数收益率年化标准差", "%"),
    IndicatorDefinition("波动", "肯特纳上轨", "KELTNER_UPPER", "EMA20加2倍ATR", "元"),
    IndicatorDefinition("波动", "唐奇安上轨", "DONCHIAN_UPPER_20", "20日最高价，突破系统常用", "元"),
    IndicatorDefinition("波动", "唐奇安下轨", "DONCHIAN_LOWER_20", "20日最低价，突破系统常用", "元"),
    IndicatorDefinition("波动", "Chaikin波动率", "CHAIKIN_VOL_10", "高低价差EMA的变化率", "%"),
    IndicatorDefinition("波动", "Ulcer指数", "ULCER_20", "只惩罚向下回撤的波动指标", "%"),
    IndicatorDefinition("量能", "OBV", "OBV", "上涨日加量、下跌日减量的累计能量潮"),
    IndicatorDefinition("量能", "MFI14", "MFI_14", "融合典型价格与成交量的资金流量指标", "%"),
    IndicatorDefinition("量能", "CMF20", "CMF_20", "20日收盘位置加权的量能流入强度"),
    IndicatorDefinition("量能", "A/D累计线", "ADL", "收盘在日内区间位置加权的累计量"),
    IndicatorDefinition("量能", "Chaikin振荡", "CHAIKIN_OSC", "A/D线的短长EMA差"),
    IndicatorDefinition("量能", "PVT", "PVT", "按价格涨跌幅加权的累计成交量"),
    IndicatorDefinition("量能", "Force Index", "FORCE_13", "价格变化乘成交量后平滑"),
    IndicatorDefinition("量能", "EMV14", "EMV_14", "价格位移相对成交量和振幅的效率"),
    IndicatorDefinition("量能", "量比5", "VOLUME_RATIO_5", "当日量相对前5日均量"),
    IndicatorDefinition("量能", "VWAP", "VWAP", "成交额除以成交量得到的成交均价估算", "元"),
    IndicatorDefinition("风险", "1日收益", "RETURN_1D", "最近1个交易日收益", "%"),
    IndicatorDefinition("风险", "5日收益", "RETURN_5D", "最近5个交易日收益", "%"),
    IndicatorDefinition("风险", "20日收益", "RETURN_20D", "最近20个交易日收益", "%"),
    IndicatorDefinition("风险", "60日收益", "RETURN_60D", "最近60个交易日收益", "%"),
    IndicatorDefinition("风险", "60日夏普", "ROLLING_SHARPE_60", "无风险利率按0处理的年化收益波动比"),
    IndicatorDefinition("风险", "60日VaR95", "VAR_95_60", "历史法估计的单日5%分位收益", "%"),
    IndicatorDefinition("风险", "当前回撤", "DRAWDOWN", "相对历史最高收盘价的回撤", "%"),
    IndicatorDefinition("风险", "60日偏度", "SKEW_60", "收益分布左右不对称程度"),
    IndicatorDefinition("风险", "60日峰度", "KURT_60", "收益分布尾部厚度"),
]


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    definition: IndicatorDefinition
    value: float | None
    status: str


def _latest_number(frame: pd.DataFrame, column: str) -> float | None:
    if column not in frame or frame.empty:
        return None
    values = frame[column].dropna()
    if values.empty:
        return None
    result = float(values.iloc[-1])
    return result if np.isfinite(result) else None


def indicator_status(column: str, value: float | None, frame: pd.DataFrame) -> str:
    if value is None:
        return "数据不足"
    close = _latest_number(frame, "close")
    if column.startswith("RSI_") or column in {"MFI_14", "STOCH_K_14", "KDJ_K"}:
        if value >= 80:
            return "极强/过热"
        if value >= 70:
            return "偏热"
        if value <= 20:
            return "极弱/超卖"
        if value <= 30:
            return "偏冷"
        return "中性"
    if column == "WILLR_14":
        return "偏热" if value > -20 else "偏冷" if value < -80 else "中性"
    if column == "ADX_14":
        return "强趋势" if value >= 25 else "趋势形成" if value >= 20 else "震荡"
    if column in {"PLUS_DI_14", "AROON_OSC_25", "MACD_HIST", "MACD_DIF", "PPO", "DMA", "LINREG_SLOPE_20", "CMF_20", "CHAIKIN_OSC", "FORCE_13"}:
        return "偏多" if value > 0 else "偏空" if value < 0 else "中性"
    if column == "MINUS_DI_14":
        plus = _latest_number(frame, "PLUS_DI_14")
        return "空方占优" if plus is not None and value > plus else "多方占优"
    if column.startswith("BIAS_"):
        return "正乖离" if value > 0 else "负乖离" if value < 0 else "贴近均线"
    if column in {"BB_WIDTH", "HV_20", "NATR_14", "CHAIKIN_VOL_10", "ULCER_20"}:
        series = frame[column].dropna()
        if len(series) >= 60:
            rank = float(series.rank(pct=True).iloc[-1])
            return "高波动" if rank >= 0.8 else "低波动" if rank <= 0.2 else "常态"
        return "波动指标"
    if column == "BB_PERCENT_B":
        return "上轨外" if value > 100 else "下轨外" if value < 0 else "带内"
    if column in {"RETURN_1D", "RETURN_5D", "RETURN_20D", "RETURN_60D"}:
        return "上涨" if value > 0 else "下跌" if value < 0 else "持平"
    if column in {"SMA_5", "SMA_10", "SMA_20", "SMA_60", "SMA_120", "SMA_250", "EMA_12", "EMA_26", "BBI", "PSAR", "SUPERTREND", "ICHIMOKU_TENKAN", "ICHIMOKU_KIJUN", "VWAP"} and close is not None:
        return "价在线上" if close >= value else "价在线下"
    if column == "VOLUME_RATIO_5":
        return "明显放量" if value >= 1.5 else "缩量" if value < 0.7 else "常态"
    if column == "ROLLING_SHARPE_60":
        return "风险收益较优" if value >= 1 else "风险收益偏弱" if value < 0 else "一般"
    if column in {"VAR_95_60", "DRAWDOWN"}:
        return "风险较高" if value <= -10 else "可控"
    return "—"


def build_indicator_snapshot(frame: pd.DataFrame) -> list[IndicatorSnapshot]:
    definitions = [
        *INDICATOR_CATALOG,
        *frame.attrs.get("extended_indicator_definitions", []),
        *frame.attrs.get("custom_indicator_definitions", []),
    ]
    return [indicator_snapshot(frame, item) for item in definitions]


def indicator_snapshot(
    frame: pd.DataFrame, definition: IndicatorDefinition
) -> IndicatorSnapshot:
    value = _latest_number(frame, definition.column)
    return IndicatorSnapshot(
        definition=definition,
        value=value,
        status=_definition_status(definition, value, frame),
    )


def _definition_status(
    definition: IndicatorDefinition, value: float | None, frame: pd.DataFrame
) -> str:
    status = indicator_status(definition.column, value, frame)
    if status != "—" or value is None:
        return status
    if definition.category == "趋势":
        close = _latest_number(frame, "close")
        if close is not None and 0.08 * close <= abs(value) <= 12 * close:
            return "价在线上" if close >= value else "价在线下"
        return "偏多" if value > 0 else "偏空" if value < 0 else "中性"
    if definition.category in {"动量", "量能", "情绪"}:
        return "偏多" if value > 0 else "偏空" if value < 0 else "中性"
    if definition.category == "波动":
        values = frame.get(definition.column, pd.Series(dtype=float)).dropna()
        if len(values) >= 20:
            rank = float(values.rank(pct=True).iloc[-1])
            return "高波动" if rank >= 0.75 else "低波动" if rank <= 0.25 else "常态"
        return "波动指标"
    return "风险改善" if value > 0 else "风险偏高" if value < 0 else "中性"


_POSITIVE_STATUS_WORDS = (
    "偏多",
    "多方",
    "上涨",
    "在线上",
    "风险收益较优",
    "风险改善",
    "低波动",
    "可控",
    "明显放量",
    "极强",
    "偏热",
)
_NEGATIVE_STATUS_WORDS = (
    "偏空",
    "空方",
    "下跌",
    "在线下",
    "风险收益偏弱",
    "风险较高",
    "风险偏高",
    "高波动",
    "缩量",
    "极弱",
    "偏冷",
)


def _snapshot_effect(snapshot: IndicatorSnapshot) -> float:
    status = snapshot.status
    if any(word in status for word in _POSITIVE_STATUS_WORDS):
        return 1.0
    if any(word in status for word in _NEGATIVE_STATUS_WORDS):
        return -1.0
    return 0.0


def dimension_composites(
    snapshots: list[IndicatorSnapshot],
) -> dict[str, dict[str, float | int | str]]:
    """Average every available indicator effect within each of six dimensions."""

    result: dict[str, dict[str, float | int | str]] = {}
    for dimension in INDICATOR_DIMENSIONS:
        members = [
            snapshot
            for snapshot in snapshots
            if snapshot.definition.category == dimension and snapshot.value is not None
        ]
        effects = np.asarray([_snapshot_effect(snapshot) for snapshot in members], dtype=float)
        weights = np.asarray(
            [indicator_weight(snapshot.definition) for snapshot in members], dtype=float
        )
        weighted_effect = (
            float(np.average(effects, weights=weights))
            if effects.size and float(weights.sum()) > 0
            else 0.0
        )
        score = float(np.clip((weighted_effect + 1) * 50, 0, 100)) if effects.size else 50.0
        if score >= 67:
            status = "综合偏多"
        elif score >= 56:
            status = "综合略偏多"
        elif score <= 33:
            status = "综合偏空"
        elif score <= 44:
            status = "综合略偏空"
        else:
            status = "综合中性"
        result[dimension] = {
            "score": round(score, 2),
            "status": status,
            "count": len(members),
            "weight": round(float(weights.sum()), 2),
        }
    return result


def market_regime(frame: pd.DataFrame) -> dict[str, str | float]:
    if frame.empty:
        return {"regime": "数据不足", "direction": "未知", "score": 0.0, "summary": "暂无行情数据"}
    adx = _latest_number(frame, "ADX_14") or 0.0
    system_snapshots = [
        snapshot
        for snapshot in build_indicator_snapshot(frame)
        if snapshot.definition.origin == "系统"
    ]
    composites = dimension_composites(system_snapshots)
    scores = [float(item["score"]) for item in composites.values()]
    score = float(np.mean(scores)) if scores else 50.0
    direction = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    regime = "趋势" if adx >= 25 else "趋势酝酿" if adx >= 20 else "震荡"
    summary = f"{regime}环境，综合状态{direction}。ADX {adx:.1f}，多维评分 {score:.0f}/100。"
    return {"regime": regime, "direction": direction, "score": score, "summary": summary}


def candle_pattern_summary(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无"
    row = frame.iloc[-1]
    signals: list[str] = []
    if row.get("PATTERN_DOJI", 0) > 0:
        signals.append("十字星")
    if row.get("PATTERN_HAMMER", 0) > 0:
        signals.append("锤头线")
    if row.get("PATTERN_SHOOTING_STAR", 0) < 0:
        signals.append("射击之星")
    engulfing = row.get("PATTERN_ENGULFING", 0)
    if engulfing > 0:
        signals.append("看涨吞没")
    elif engulfing < 0:
        signals.append("看跌吞没")
    return "、".join(signals) if signals else "未识别到典型单/双K形态"
