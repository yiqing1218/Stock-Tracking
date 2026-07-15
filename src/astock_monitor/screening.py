from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from .data_provider import DataProvider
from .indicators import IndicatorDefinition, calculate_selected_indicators
from .historical_store import HistoricalStore
from .models import Security, SecurityType


@dataclass(frozen=True, slots=True)
class ScreeningCondition:
    connector: str
    definition: IndicatorDefinition
    operator: str
    threshold: float


def compare_value(value: float | None, operator: str, threshold: float) -> bool:
    if value is None or pd.isna(value):
        return False
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "=": value == threshold,
        "!=": value != threshold,
    }.get(operator, False)


def matches_conditions(
    values: dict[str, float | None], conditions: list[ScreeningCondition]
) -> bool:
    if not conditions or len(conditions) > 5:
        return False
    result = compare_value(
        values.get(conditions[0].definition.column),
        conditions[0].operator,
        conditions[0].threshold,
    )
    if conditions[0].connector == "非":
        result = not result
    for condition in conditions[1:]:
        current = compare_value(
            values.get(condition.definition.column),
            condition.operator,
            condition.threshold,
        )
        if condition.connector == "或":
            result = result or current
        elif condition.connector == "非":
            result = result and not current
        else:
            result = result and current
    return result


def screen_all_stocks(
    provider: DataProvider,
    conditions: list[ScreeningCondition],
    progress: Callable[[int, int], None] | None = None,
    store: HistoricalStore | None = None,
    target_date: str = "",
    adjustment: str = "qfq",
) -> pd.DataFrame:
    if not 1 <= len(conditions) <= 5:
        raise ValueError("筛选条件必须为 1 至 5 个")
    if store is None:
        raise ValueError(
            "条件荐股只读取本地历史仓库；请先在“数据导出→数据同步”同步A股日线"
        )
    universe = store.list_securities((SecurityType.STOCK,))
    if not universe:
        raise ValueError("本地历史仓库尚无股票目录，请先同步或导入旧版CSV缓存")
    definitions = list(
        {item.definition.column: item.definition for item in conditions}.values()
    )

    def evaluate(security: Security) -> dict[str, object] | None:
        try:
            history = store.get_bars(
                security, adjustment=adjustment, end=target_date or None, limit=620
            )
            if len(history) < 30:
                return None
            values = calculate_selected_indicators(history, definitions)
            if not matches_conditions(values, conditions):
                return None
            row: dict[str, object] = {
                "代码": security.code,
                "名称": security.name,
                "市场": security.market,
                "日期": pd.Timestamp(history.iloc[-1]["date"]).strftime("%Y-%m-%d"),
                "收盘价": float(history.iloc[-1]["close"]),
                "数据来源": "本地历史仓库",
                "security": security,
            }
            row.update({item.name: values.get(item.column) for item in definitions})
            row["数据完整度"] = sum(
                pd.notna(values.get(item.column)) for item in definitions
            ) / max(len(definitions), 1)
            if target_date:
                future = store.get_bars(
                    security, adjustment=adjustment, start=target_date
                )
                future = future[
                    pd.to_datetime(future["date"])
                    > pd.Timestamp(history.iloc[-1]["date"])
                ]
                base = float(history.iloc[-1]["close"])
                for days in (1, 3, 5, 10, 20):
                    row[f"{days}日后收益%"] = (
                        float((future.iloc[days - 1]["close"] / base - 1) * 100)
                        if len(future) >= days and base
                        else None
                    )
            return row
        except Exception:
            return None

    rows: list[dict[str, object]] = []
    completed = 0
    # SQLite readers are cheap, but indicator calculation is CPU bound. A small pool
    # keeps memory bounded and never performs a per-stock network request.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for start in range(0, len(universe), 128):
            batch = universe[start : start + 128]
            futures = [executor.submit(evaluate, security) for security in batch]
            for future in as_completed(futures):
                completed += 1
                row = future.result()
                if row is not None:
                    rows.append(row)
                if progress and (completed % 25 == 0 or completed == len(universe)):
                    progress(completed, len(universe))
    return pd.DataFrame(rows)
