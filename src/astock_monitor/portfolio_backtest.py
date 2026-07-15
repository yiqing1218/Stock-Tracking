from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from .formula_engine import FormulaEngine
from .historical_store import HistoricalStore
from .models import Security, SecurityType
from .time_utils import beijing_now


@dataclass(slots=True)
class PortfolioBacktestConfig:
    name: str = "自定义公式回测"
    universe: str = "single"
    start_date: str = ""
    end_date: str = ""
    adjustment: str = "qfq"
    entry_formula: str = "CROSS(SMA(close,5), SMA(close,20))"
    exit_formula: str = "CROSS(SMA(close,20), SMA(close,5))"
    score_formula: str = "ROC(close,20)"
    holding_days: int = 0
    take_profit_pct: float = 0.0
    stop_loss_pct: float = 0.0
    initial_cash: float = 1_000_000.0
    position_sizing: str = "equal_weight"
    position_pct: float = 100.0
    max_positions: int = 10
    rebalance_frequency: str = "daily"
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_pct: float = 0.02
    execution_price: str = "next_open"
    benchmark_key: str = "index:000300"
    exclude_st: bool = True
    minimum_listing_days: int = 20
    lot_size: int = 100


@dataclass(slots=True)
class PortfolioBacktestResult:
    run_id: int
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    equity: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    unfilled: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    annual_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    audit: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Position:
    security: Security
    quantity: int
    cost: float
    buy_date: pd.Timestamp
    buy_index: int


class LocalPortfolioBacktester:
    """Auditable, local-only portfolio backtester with A-share constraints."""

    def __init__(self, store: HistoricalStore) -> None:
        self.store = store
        self._cancel = threading.Event()
        self._current_run_id = 0

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _limit_rate(security: Security) -> float:
        if "ST" in security.name.upper():
            return 0.05
        if security.code.startswith(("300", "301", "688", "689")):
            return 0.20
        if security.code.startswith(("4", "8", "92")):
            return 0.30
        return 0.10

    @staticmethod
    def _price(row: pd.Series, mode: str) -> float:
        if mode == "next_close":
            return float(row["close"])
        if mode == "vwap_approx":
            return float(np.mean([row["open"], row["high"], row["low"], row["close"]]))
        return float(row["open"])

    def _blocked_reason(
        self, row: pd.Series, previous_close: float, buying: bool, security: Security
    ) -> str:
        required = ("open", "high", "low", "close")
        if any(
            pd.isna(row.get(name)) or float(row.get(name, 0)) <= 0 for name in required
        ):
            return "价格无效"
        if float(row.get("volume", 0) or 0) <= 0:
            return "停牌或无成交"
        if previous_close <= 0:
            return "缺少昨收"
        rate = self._limit_rate(security)
        upper = previous_close * (1 + rate)
        lower = previous_close * (1 - rate)
        one_price = abs(float(row["high"]) - float(row["low"])) < 0.005
        if buying and one_price and float(row["open"]) >= upper - 0.01:
            return "一字涨停无法买入"
        if not buying and one_price and float(row["open"]) <= lower + 0.01:
            return "一字跌停无法卖出"
        return ""

    def _load_signal_frame(
        self, security: Security, config: PortfolioBacktestConfig
    ) -> pd.DataFrame:
        frame = self.store.get_bars(
            security,
            adjustment=config.adjustment,
            start=config.start_date or None,
            end=config.end_date or None,
        )
        if len(frame) < max(30, config.minimum_listing_days + 2):
            return pd.DataFrame()
        frame = frame.dropna(subset=["date", "open", "close"]).copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame = (
            frame.drop_duplicates("date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        engine = FormulaEngine(frame)
        frame["ENTRY"] = engine.evaluate(config.entry_formula).fillna(0).astype(bool)
        frame["EXIT"] = engine.evaluate(config.exit_formula).fillna(0).astype(bool)
        frame["SCORE"] = pd.to_numeric(
            engine.evaluate(config.score_formula or "ROC(close,20)"), errors="coerce"
        )
        frame["_index"] = np.arange(len(frame))
        columns = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "ENTRY",
            "EXIT",
            "SCORE",
            "_index",
        ]
        compact = frame[columns].copy()
        for column in ("open", "high", "low", "close", "volume", "SCORE"):
            compact[column] = pd.to_numeric(compact[column], errors="coerce").astype(
                "float32"
            )
        return compact

    @staticmethod
    def _config_hash(
        config: PortfolioBacktestConfig, securities: list[Security]
    ) -> str:
        payload = {
            "config": asdict(config),
            "securities": [security.to_dict() for security in securities],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _create_run(
        self, config: PortfolioBacktestConfig, securities: list[Security]
    ) -> tuple[int, str]:
        config_hash = self._config_hash(config, securities)
        data_version = "|".join(
            f"{security.key}:{self.store.latest_date(security, config.adjustment)}"
            for security in securities
        )
        notes = "全市场范围使用当前本地证券清单，可能存在幸存者偏差；板块成分也以本地可得历史为准。"
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO backtest_definitions
                (name,universe,security_types,start_date,end_date,adjustment,entry_formula,exit_formula,
                score_formula,holding_period,take_profit,stop_loss,initial_cash,
                position_sizing,max_positions,rebalance_frequency,commission_rate,
                minimum_commission,stamp_tax_rate,slippage_model,slippage_value,
                benchmark,execution_price,exclude_st,minimum_listing_days,config_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET universe=excluded.universe,
                security_types=excluded.security_types,
                start_date=excluded.start_date,end_date=excluded.end_date,
                adjustment=excluded.adjustment,entry_formula=excluded.entry_formula,
                exit_formula=excluded.exit_formula,score_formula=excluded.score_formula,
                holding_period=excluded.holding_period,take_profit=excluded.take_profit,
                stop_loss=excluded.stop_loss,initial_cash=excluded.initial_cash,
                position_sizing=excluded.position_sizing,max_positions=excluded.max_positions,
                rebalance_frequency=excluded.rebalance_frequency,
                commission_rate=excluded.commission_rate,
                minimum_commission=excluded.minimum_commission,
                stamp_tax_rate=excluded.stamp_tax_rate,
                slippage_value=excluded.slippage_value,benchmark=excluded.benchmark,
                execution_price=excluded.execution_price,exclude_st=excluded.exclude_st,
                minimum_listing_days=excluded.minimum_listing_days,
                config_hash=excluded.config_hash,updated_at=CURRENT_TIMESTAMP""",
                (
                    config.name,
                    config.universe,
                    json.dumps(
                        sorted(
                            {security.security_type.value for security in securities}
                        )
                    ),
                    config.start_date,
                    config.end_date,
                    config.adjustment,
                    config.entry_formula,
                    config.exit_formula,
                    config.score_formula,
                    config.holding_days,
                    config.take_profit_pct,
                    config.stop_loss_pct,
                    config.initial_cash,
                    config.position_sizing,
                    config.max_positions,
                    config.rebalance_frequency,
                    config.commission_rate,
                    config.min_commission,
                    config.stamp_tax_rate,
                    "fixed_pct",
                    config.slippage_pct,
                    config.benchmark_key,
                    config.execution_price,
                    int(config.exclude_st),
                    config.minimum_listing_days,
                    config_hash,
                ),
            )
            definition_id = int(
                db.execute(
                    "SELECT id FROM backtest_definitions WHERE name=?", (config.name,)
                ).fetchone()[0]
            )
            cursor = db.execute(
                """INSERT INTO backtest_runs
                (definition_id,name,status,config_json,config_hash,data_version,code_version,bias_notes)
                VALUES(?,?,'running',?,?,?,?,?)""",
                (
                    definition_id,
                    config.name,
                    json.dumps(asdict(config), ensure_ascii=False),
                    config_hash,
                    hashlib.sha256(data_version.encode()).hexdigest(),
                    "local-backtester-v1",
                    notes,
                ),
            )
        return int(cursor.lastrowid), config_hash

    def run(
        self,
        securities: Iterable[Security],
        config: PortfolioBacktestConfig,
        benchmark: Security | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> PortfolioBacktestResult:
        try:
            return self._run_impl(securities, config, benchmark, progress)
        except Exception as exc:
            if self._current_run_id:
                self._finish_run(self._current_run_id, "failed", 0, str(exc))
            raise
        finally:
            self._current_run_id = 0

    def _run_impl(
        self,
        securities: Iterable[Security],
        config: PortfolioBacktestConfig,
        benchmark: Security | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> PortfolioBacktestResult:
        self._cancel.clear()
        universe = list(dict.fromkeys(securities))
        if not universe:
            raise ValueError("回测范围为空")
        run_id, config_hash = self._create_run(config, universe)
        self._current_run_id = run_id
        audit = [
            f"配置哈希：{config_hash}",
            "数据来源：仅本地历史仓库，不在回测过程中联网下载。",
            "信号时点：T日收盘后确认；默认T+1开盘执行。",
            "无未来数据：公式只允许向过去REF，撮合不读取信号日之后的价格。",
            "成交限制：停牌、无量、一字涨跌停、A股T+1、整手、佣金、印花税和滑点。",
        ]
        if config.execution_price == "next_close":
            audit.append(
                "执行价选择下一日收盘价，仅用于敏感性分析，不代表盘中可提前知道该价格。"
            )
        elif config.execution_price == "vwap_approx":
            audit.append(
                "VWAP为OHLC4近似值；本地日线没有逐笔成交，结果按近似撮合标记。"
            )
        if config.universe in {"all", "all_etf", "dynamic"}:
            audit.append("警告：当前本地证券清单可能存在幸存者偏差。")

        frames: dict[str, pd.DataFrame] = {}
        lookup: dict[str, pd.DataFrame] = {}
        skipped: list[str] = []
        for index, security in enumerate(universe, 1):
            if self._cancel.is_set():
                self._finish_run(run_id, "cancelled", 0, "")
                return self._cancelled_result(run_id, audit)
            if config.exclude_st and "ST" in security.name.upper():
                skipped.append(f"{security.code} {security.name}：排除ST")
                continue
            try:
                frame = self._load_signal_frame(security, config)
            except Exception as exc:
                skipped.append(f"{security.code} {security.name}：公式/数据错误 {exc}")
                continue
            if frame.empty:
                skipped.append(f"{security.code} {security.name}：本地历史不足")
                continue
            frames[security.key] = frame
            lookup[security.key] = frame.set_index("date", drop=False)
            if progress:
                progress(index, len(universe), f"载入 {security.name}")
        if not frames:
            self._finish_run(run_id, "failed", 0, "没有满足条件的本地历史数据")
            raise ValueError("本地仓库没有足够的回测数据；请先在数据导出页同步历史行情")
        audit.extend(skipped[:100])
        security_map = {
            security.key: security for security in universe if security.key in frames
        }
        dates = sorted(
            {
                pd.Timestamp(value)
                for frame in frames.values()
                for value in frame["date"]
            }
        )
        positions: dict[str, _Position] = {}
        pending: list[dict] = []
        cash = float(config.initial_cash)
        trades: list[dict] = []
        unfilled: list[dict] = []
        position_rows: list[dict] = []
        equity_rows: list[dict] = []
        signal_rows: list[dict] = []
        total_cost = total_slippage = turnover_amount = 0.0

        def next_date(key: str, current: pd.Timestamp) -> pd.Timestamp | None:
            values = frames[key]["date"]
            future = values[values > current]
            return pd.Timestamp(future.iloc[0]) if not future.empty else None

        def row_for(key: str, current: pd.Timestamp) -> pd.Series | None:
            indexed = lookup[key]
            if current not in indexed.index:
                return None
            value = indexed.loc[current]
            return value.iloc[-1] if isinstance(value, pd.DataFrame) else value

        for day_number, current_date in enumerate(dates):
            if self._cancel.is_set():
                self._finish_run(
                    run_id, "cancelled", day_number / max(len(dates), 1), ""
                )
                return self._cancelled_result(run_id, audit)
            todays_orders = [
                order for order in pending if order["order_date"] == current_date
            ]
            pending = [
                order for order in pending if order["order_date"] != current_date
            ]
            todays_orders.sort(key=lambda item: 0 if item["side"] == "sell" else 1)
            for order in todays_orders:
                key = order["key"]
                row = row_for(key, current_date)
                security = security_map[key]
                if row is None:
                    unfilled.append(
                        {**order, "trade_date": current_date, "reason": "目标日无行情"}
                    )
                    continue
                frame = frames[key]
                row_index = int(row["_index"])
                previous_close = (
                    float(frame.iloc[row_index - 1]["close"])
                    if row_index > 0
                    else float(row["close"])
                )
                reason = self._blocked_reason(
                    row, previous_close, order["side"] == "buy", security
                )
                if reason:
                    unfilled.append(
                        {**order, "trade_date": current_date, "reason": reason}
                    )
                    continue
                base_price = self._price(row, config.execution_price)
                price = base_price * (
                    1 + config.slippage_pct / 100
                    if order["side"] == "buy"
                    else 1 - config.slippage_pct / 100
                )
                if order["side"] == "sell":
                    position = positions.get(key)
                    if position is None:
                        continue
                    if current_date <= position.buy_date:
                        unfilled.append(
                            {
                                **order,
                                "trade_date": current_date,
                                "reason": "A股T+1限制",
                            }
                        )
                        continue
                    gross = position.quantity * price
                    commission = max(
                        config.min_commission, gross * config.commission_rate
                    )
                    stamp = (
                        gross * config.stamp_tax_rate
                        if security.security_type is SecurityType.STOCK
                        else 0.0
                    )
                    cash += gross - commission - stamp
                    total_cost += commission + stamp
                    total_slippage += position.quantity * abs(price - base_price)
                    turnover_amount += gross
                    pnl = gross - commission - stamp - position.quantity * position.cost
                    trades.append(
                        {
                            "signal_date": order["signal_date"],
                            "order_date": order["order_date"],
                            "trade_date": current_date,
                            "股票": security.name,
                            "代码": security.code,
                            "方向": "卖出",
                            "价格": price,
                            "数量": position.quantity,
                            "成交额": gross,
                            "佣金": commission,
                            "印花税": stamp,
                            "滑点成本": position.quantity * abs(price - base_price),
                            "盈亏": pnl,
                            "持有天数": row_index - position.buy_index,
                            "原因": order["reason"],
                            "filled": 1,
                        }
                    )
                    del positions[key]
                else:
                    if key in positions or len(positions) >= config.max_positions:
                        unfilled.append(
                            {
                                **order,
                                "trade_date": current_date,
                                "reason": "已持仓或达到最大持仓数",
                            }
                        )
                        continue
                    slots = max(config.max_positions - len(positions), 1)
                    target_budget = (
                        float(
                            order.get(
                                "budget", cash * config.position_pct / 100 / slots
                            )
                        )
                        if config.position_sizing == "score_weight"
                        else cash * config.position_pct / 100 / slots
                        if config.position_sizing == "equal_weight"
                        else cash * config.position_pct / 100
                    )
                    quantity = (
                        int(target_budget / price / config.lot_size) * config.lot_size
                    )
                    if quantity <= 0:
                        unfilled.append(
                            {
                                **order,
                                "trade_date": current_date,
                                "reason": "资金不足一手",
                            }
                        )
                        continue
                    gross = quantity * price
                    commission = max(
                        config.min_commission, gross * config.commission_rate
                    )
                    while quantity > 0 and gross + commission > cash:
                        quantity -= config.lot_size
                        gross = quantity * price
                        commission = (
                            max(config.min_commission, gross * config.commission_rate)
                            if quantity
                            else 0
                        )
                    if quantity <= 0:
                        unfilled.append(
                            {**order, "trade_date": current_date, "reason": "现金不足"}
                        )
                        continue
                    cash -= gross + commission
                    total_cost += commission
                    total_slippage += quantity * abs(price - base_price)
                    turnover_amount += gross
                    positions[key] = _Position(
                        security,
                        quantity,
                        (gross + commission) / quantity,
                        current_date,
                        row_index,
                    )
                    trades.append(
                        {
                            "signal_date": order["signal_date"],
                            "order_date": order["order_date"],
                            "trade_date": current_date,
                            "股票": security.name,
                            "代码": security.code,
                            "方向": "买入",
                            "价格": price,
                            "数量": quantity,
                            "成交额": gross,
                            "佣金": commission,
                            "印花税": 0.0,
                            "滑点成本": quantity * abs(price - base_price),
                            "盈亏": np.nan,
                            "持有天数": 0,
                            "原因": order["reason"],
                            "filled": 1,
                        }
                    )

            market_value = 0.0
            for key, position in list(positions.items()):
                row = row_for(key, current_date)
                if row is None:
                    historical = frames[key][frames[key]["date"] <= current_date]
                    close = float(historical.iloc[-1]["close"])
                else:
                    close = float(row["close"])
                value = position.quantity * close
                market_value += value
                position_rows.append(
                    {
                        "date": current_date,
                        "股票": position.security.name,
                        "代码": position.security.code,
                        "数量": position.quantity,
                        "市值": value,
                        "成本": position.cost,
                        "浮动盈亏": value - position.quantity * position.cost,
                    }
                )
            equity = cash + market_value
            equity_rows.append(
                {
                    "date": current_date,
                    "equity": equity,
                    "cash": cash,
                    "exposure": market_value / equity if equity else 0,
                }
            )

            queued_keys = {(order["key"], order["side"]) for order in pending}
            candidates: list[dict] = []
            for key, frame in frames.items():
                row = row_for(key, current_date)
                if row is None:
                    continue
                security = security_map[key]
                row_index = int(row["_index"])
                if key in positions:
                    position = positions[key]
                    return_pct = (float(row["close"]) / position.cost - 1) * 100
                    reason = ""
                    if bool(row["EXIT"]):
                        reason = "EXIT"
                    elif (
                        config.take_profit_pct and return_pct >= config.take_profit_pct
                    ):
                        reason = "止盈"
                    elif config.stop_loss_pct and return_pct <= -config.stop_loss_pct:
                        reason = "止损"
                    elif (
                        config.holding_days
                        and row_index - position.buy_index >= config.holding_days
                    ):
                        reason = "固定持有期"
                    if reason and (key, "sell") not in queued_keys:
                        target = next_date(key, current_date)
                        if target is not None:
                            pending.append(
                                {
                                    "key": key,
                                    "side": "sell",
                                    "signal_date": current_date,
                                    "order_date": target,
                                    "reason": reason,
                                    "score": float(row["SCORE"] or 0),
                                }
                            )
                    if bool(row["EXIT"]):
                        signal_rows.append(
                            {
                                "日期": current_date,
                                "股票": security.name,
                                "代码": security.code,
                                "信号": "EXIT",
                                "原始收盘": row["close"],
                                "SCORE": row["SCORE"],
                            }
                        )
                elif bool(row["ENTRY"]) and row_index >= config.minimum_listing_days:
                    signal_rows.append(
                        {
                            "日期": current_date,
                            "股票": security.name,
                            "代码": security.code,
                            "信号": "ENTRY",
                            "原始开盘": row["open"],
                            "原始最高": row["high"],
                            "原始最低": row["low"],
                            "原始收盘": row["close"],
                            "原始成交量": row["volume"],
                            "SCORE": row["SCORE"],
                        }
                    )
                    if (key, "buy") not in queued_keys:
                        target = next_date(key, current_date)
                        rebalance_allowed = (
                            config.rebalance_frequency == "daily"
                            or (
                                config.rebalance_frequency == "weekly"
                                and target is not None
                                and target.isocalendar().week
                                != current_date.isocalendar().week
                            )
                            or (
                                config.rebalance_frequency == "monthly"
                                and target is not None
                                and target.month != current_date.month
                            )
                        )
                        if target is not None and rebalance_allowed:
                            candidates.append(
                                {
                                    "key": key,
                                    "side": "buy",
                                    "signal_date": current_date,
                                    "order_date": target,
                                    "reason": "ENTRY",
                                    "score": float(row["SCORE"])
                                    if pd.notna(row["SCORE"])
                                    else -math.inf,
                                }
                            )
            capacity = max(config.max_positions - len(positions), 0)
            selected = sorted(candidates, key=lambda item: item["score"], reverse=True)[
                :capacity
            ]
            if config.position_sizing == "score_weight" and selected:
                scores = np.array(
                    [
                        item["score"] if math.isfinite(item["score"]) else 0.0
                        for item in selected
                    ],
                    dtype=float,
                )
                positive = scores - scores.min() + 1e-6
                weights = positive / positive.sum()
                for item, weight in zip(selected, weights, strict=True):
                    item["budget"] = equity * config.position_pct / 100 * float(weight)
            pending.extend(selected)
            if progress and day_number % max(1, len(dates) // 100) == 0:
                progress(day_number + 1, len(dates), current_date.strftime("%Y-%m-%d"))

        equity_frame = pd.DataFrame(equity_rows)
        trades_frame = pd.DataFrame(trades)
        unfilled_frame = pd.DataFrame(unfilled)
        position_frame = pd.DataFrame(position_rows)
        benchmark_frame = (
            self.store.get_bars(
                benchmark,
                config.adjustment,
                config.start_date or None,
                config.end_date or None,
            )
            if benchmark is not None
            else pd.DataFrame()
        )
        metrics, annual, monthly = self._performance(
            equity_frame,
            trades_frame,
            benchmark_frame,
            total_cost,
            total_slippage,
            turnover_amount,
        )
        self._persist_result(
            run_id,
            metrics,
            equity_frame,
            trades_frame,
            unfilled_frame,
            position_frame,
            security_map,
            status="completed",
        )
        audit.append(
            f"跳过证券 {len(skipped)} 只；未成交订单 {len(unfilled_frame)} 笔。"
        )
        return PortfolioBacktestResult(
            run_id,
            metrics,
            equity_frame,
            trades_frame,
            unfilled_frame,
            position_frame,
            annual,
            monthly,
            pd.DataFrame(signal_rows),
            audit,
        )

    def _performance(
        self,
        equity: pd.DataFrame,
        trades: pd.DataFrame,
        benchmark: pd.DataFrame,
        total_cost: float,
        slippage: float,
        turnover_amount: float,
    ) -> tuple[dict[str, float | int | str], pd.DataFrame, pd.DataFrame]:
        series = equity.set_index("date")["equity"].astype(float)
        returns = series.pct_change().fillna(0)
        cumulative = series.iloc[-1] / series.iloc[0] - 1 if len(series) > 1 else 0
        years = max(len(series) / 242, 1 / 242)
        annualized = (1 + cumulative) ** (1 / years) - 1 if cumulative > -1 else -1
        volatility = float(returns.std(ddof=0) * np.sqrt(242))
        downside = float(returns[returns < 0].std(ddof=0) * np.sqrt(242))
        drawdown = series / series.cummax() - 1
        maximum_drawdown = float(drawdown.min())
        in_drawdown = drawdown < 0
        groups = (~in_drawdown).cumsum()
        drawdown_duration = (
            int(in_drawdown.groupby(groups).sum().max()) if in_drawdown.any() else 0
        )
        benchmark_return = excess = beta = alpha = information = 0.0
        if not benchmark.empty and "close" in benchmark:
            bench = benchmark.copy()
            bench["date"] = pd.to_datetime(bench["date"])
            bench_series = bench.set_index("date")["close"].astype(float)
            joined = pd.concat(
                [
                    returns.rename("strategy"),
                    bench_series.pct_change().rename("benchmark"),
                ],
                axis=1,
            ).dropna()
            if len(joined) > 2 and joined["benchmark"].var() > 0:
                beta = float(
                    joined.cov().loc["strategy", "benchmark"]
                    / joined["benchmark"].var()
                )
                alpha = float(
                    (joined["strategy"].mean() - beta * joined["benchmark"].mean())
                    * 242
                )
                active = joined["strategy"] - joined["benchmark"]
                information = (
                    float(active.mean() / active.std(ddof=0) * np.sqrt(242))
                    if active.std(ddof=0) > 0
                    else 0
                )
            if len(bench_series) > 1:
                benchmark_return = float(
                    bench_series.iloc[-1] / bench_series.iloc[0] - 1
                )
                excess = cumulative - benchmark_return
        sells = (
            trades[trades.get("方向", pd.Series(dtype=str)) == "卖出"]
            if not trades.empty
            else pd.DataFrame()
        )
        pnl = pd.to_numeric(
            sells.get("盈亏", pd.Series(dtype=float)), errors="coerce"
        ).dropna()
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        holding = pd.to_numeric(
            sells.get("持有天数", pd.Series(dtype=float)), errors="coerce"
        )
        consecutive = maximum_losses = 0
        for value in pnl:
            consecutive = consecutive + 1 if value < 0 else 0
            maximum_losses = max(maximum_losses, consecutive)
        annual_series = (
            series.resample("YE")
            .last()
            .pct_change()
            .fillna(series.resample("YE").last() / series.iloc[0] - 1)
        )
        monthly_series = series.resample("ME").last().pct_change().fillna(0)
        annual = pd.DataFrame(
            {"年度": annual_series.index.year, "收益率(%)": annual_series.values * 100}
        )
        monthly = pd.DataFrame(
            {
                "月份": monthly_series.index.strftime("%Y-%m"),
                "收益率(%)": monthly_series.values * 100,
            }
        )
        metrics: dict[str, float | int | str] = {
            "累计收益(%)": cumulative * 100,
            "年化收益(%)": annualized * 100,
            "基准收益(%)": benchmark_return * 100,
            "超额收益(%)": excess * 100,
            "年化波动率(%)": volatility * 100,
            "最大回撤(%)": maximum_drawdown * 100,
            "最大回撤持续(交易日)": drawdown_duration,
            "夏普比率": annualized / volatility if volatility else 0,
            "Sortino比率": annualized / downside if downside else 0,
            "Calmar比率": annualized / abs(maximum_drawdown)
            if maximum_drawdown < 0
            else 0,
            "Beta": beta,
            "Alpha(年化)": alpha,
            "信息比率": information,
            "胜率(%)": len(wins) / len(pnl) * 100 if len(pnl) else 0,
            "盈亏比": float(wins.mean() / abs(losses.mean()))
            if len(wins) and len(losses)
            else 0,
            "平均盈利": float(wins.mean()) if len(wins) else 0,
            "平均亏损": float(losses.mean()) if len(losses) else 0,
            "平均持仓时间(天)": float(holding.mean()) if not holding.empty else 0,
            "最大连续亏损": maximum_losses,
            "交易次数": len(trades),
            "换手金额": turnover_amount,
            "总交易成本": total_cost,
            "滑点成本": slippage,
            "平均仓位(%)": float(equity["exposure"].mean() * 100),
        }
        return metrics, annual, monthly

    def _persist_result(
        self,
        run_id: int,
        metrics: dict,
        equity: pd.DataFrame,
        trades: pd.DataFrame,
        unfilled: pd.DataFrame,
        positions: pd.DataFrame,
        security_map: dict[str, Security],
        status: str,
    ) -> None:
        by_code = {security.code: security for security in security_map.values()}
        with self.store.connect() as db:
            for frame, filled in ((trades, 1), (unfilled, 0)):
                for _, row in frame.iterrows():
                    security = by_code.get(
                        str(row.get("代码", ""))
                    ) or security_map.get(str(row.get("key", "")))
                    security_id = self.store.security_id(security) if security else None
                    db.execute(
                        """INSERT INTO backtest_trades
                        (run_id,security_id,signal_date,order_date,trade_date,side,executed_price,
                        quantity,gross_amount,commission,stamp_tax,slippage,reason,filled)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            security_id,
                            str(row.get("signal_date", "")),
                            str(row.get("order_date", "")),
                            str(row.get("trade_date", "")),
                            str(row.get("方向", row.get("side", ""))),
                            row.get("价格"),
                            row.get("数量"),
                            row.get("成交额"),
                            row.get("佣金"),
                            row.get("印花税"),
                            row.get("滑点成本"),
                            str(row.get("原因", row.get("reason", ""))),
                            filled,
                        ),
                    )
            maximum = equity["equity"].cummax()
            drawdown = equity["equity"] / maximum - 1
            for index, row in equity.iterrows():
                db.execute(
                    "INSERT INTO backtest_equity(run_id,trade_date,equity,cash,drawdown,exposure) VALUES(?,?,?,?,?,?)",
                    (
                        run_id,
                        str(row["date"]),
                        float(row["equity"]),
                        float(row["cash"]),
                        float(drawdown.iloc[index]),
                        float(row["exposure"]),
                    ),
                )
            for _, row in positions.iterrows():
                security = by_code.get(str(row["代码"]))
                security_id = self.store.security_id(security) if security else None
                if security_id is not None:
                    db.execute(
                        "INSERT INTO backtest_positions(run_id,trade_date,security_id,quantity,market_value,cost,unrealized_pnl) VALUES(?,?,?,?,?,?,?)",
                        (
                            run_id,
                            str(row["date"]),
                            security_id,
                            int(row["数量"]),
                            float(row["市值"]),
                            float(row["成本"]),
                            float(row["浮动盈亏"]),
                        ),
                    )
            for key, value in metrics.items():
                numeric = (
                    float(value) if isinstance(value, (int, float, np.number)) else None
                )
                db.execute(
                    "INSERT INTO backtest_metrics(run_id,metric_key,metric_value,metric_text) VALUES(?,?,?,?)",
                    (run_id, key, numeric, "" if numeric is not None else str(value)),
                )
            db.execute(
                "UPDATE backtest_runs SET status=?,progress=1,finished_at=? WHERE id=?",
                (status, beijing_now().isoformat(), run_id),
            )

    def _finish_run(
        self, run_id: int, status: str, progress: float, error: str
    ) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE backtest_runs SET status=?,progress=?,error_count=?,finished_at=? WHERE id=?",
                (status, progress, int(bool(error)), beijing_now().isoformat(), run_id),
            )

    @staticmethod
    def _cancelled_result(run_id: int, audit: list[str]) -> PortfolioBacktestResult:
        return PortfolioBacktestResult(
            run_id=run_id, metrics={"状态": "已取消"}, audit=[*audit, "用户取消运行。"]
        )
