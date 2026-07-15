from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .formula_engine import FormulaEngine
from .models import Security


@dataclass(slots=True)
class BacktestConfig:
    entry_formula: str = "CROSS(SMA(close,5), SMA(close,20))"
    exit_formula: str = "CROSS(SMA(close,20), SMA(close,5))"
    holding_days: int = 0
    take_profit_pct: float = 0.0
    stop_loss_pct: float = 0.0
    position_pct: float = 100.0
    max_positions: int = 10
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_pct: float = 0.02
    initial_cash: float = 1_000_000.0


@dataclass(slots=True)
class BacktestResult:
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    equity: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    annual_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)


def _limit_rate(security: Security) -> float:
    if "ST" in security.name.upper():
        return 0.05
    if security.code.startswith(("300", "301", "688", "689")):
        return 0.20
    if security.code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _trade_blocked(
    row: pd.Series, previous_close: float, buying: bool, security: Security
) -> bool:
    if float(row.get("volume", 0) or 0) <= 0 or previous_close <= 0:
        return True
    open_price = float(row.get("open", 0) or 0)
    limit = _limit_rate(security)
    return (
        open_price >= previous_close * (1 + limit - 0.001)
        if buying
        else open_price <= previous_close * (1 - limit + 0.001)
    )


def run_single_backtest(
    history: pd.DataFrame,
    security: Security,
    config: BacktestConfig,
    benchmark: pd.DataFrame | None = None,
) -> BacktestResult:
    frame = history.copy().reset_index(drop=True)
    if len(frame) < 30:
        raise ValueError("回测至少需要30个交易日")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "close"]).reset_index(drop=True)
    engine = FormulaEngine(frame)
    entry = engine.evaluate(config.entry_formula).fillna(0).astype(bool)
    exit_signal = engine.evaluate(config.exit_formula).fillna(0).astype(bool)
    raw_signals = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    raw_signals["ENTRY"] = entry
    raw_signals["EXIT"] = exit_signal

    cash = float(config.initial_cash)
    shares = 0
    entry_price = 0.0
    entry_index = -1
    pending_buy = False
    pending_sell = False
    pending_sell_reason = ""
    trades: list[dict[str, object]] = []
    equity_rows: list[dict[str, object]] = []
    losses_in_row = maximum_losses = 0
    for index, row in frame.iterrows():
        close = float(row["close"])
        previous_close = float(frame.iloc[index - 1]["close"]) if index else close
        # Signals are observed after close and executed at the next day's open.
        if (
            index >= 5
            and pending_buy
            and shares == 0
            and not _trade_blocked(row, previous_close, True, security)
        ):
            price = float(row["open"]) * (1 + config.slippage_pct / 100)
            budget = cash * min(max(config.position_pct, 0), 100) / 100
            quantity = int(budget / price / 100) * 100
            commission = (
                max(config.min_commission, quantity * price * config.commission_rate)
                if quantity
                else 0
            )
            if quantity and quantity * price + commission <= cash:
                cash -= quantity * price + commission
                shares = quantity
                entry_price = price
                entry_index = index
                trades.append(
                    {
                        "日期": row["date"],
                        "股票": security.name,
                        "代码": security.code,
                        "方向": "买入",
                        "价格": price,
                        "数量": quantity,
                        "费用": commission,
                        "原因": "ENTRY",
                    }
                )
        held_days = index - entry_index if shares else 0
        should_sell = pending_sell
        # T+1: a position cannot be sold on its buy date.
        if (
            should_sell
            and shares
            and index > entry_index
            and not _trade_blocked(row, previous_close, False, security)
        ):
            price = float(row["open"]) * (1 - config.slippage_pct / 100)
            commission = max(
                config.min_commission, shares * price * config.commission_rate
            )
            stamp = shares * price * config.stamp_tax_rate
            proceeds = shares * price - commission - stamp
            pnl = proceeds - shares * entry_price
            cash += proceeds
            trades.append(
                {
                    "日期": row["date"],
                    "股票": security.name,
                    "代码": security.code,
                    "方向": "卖出",
                    "价格": price,
                    "数量": shares,
                    "费用": commission + stamp,
                    "盈亏": pnl,
                    "持有天数": held_days,
                    "原因": pending_sell_reason or "EXIT",
                }
            )
            if pnl < 0:
                losses_in_row += 1
                maximum_losses = max(maximum_losses, losses_in_row)
            else:
                losses_in_row = 0
            shares = 0
            entry_price = 0
            entry_index = -1
        equity_rows.append({"date": row["date"], "equity": cash + shares * close})
        pending_buy = bool(entry.iloc[index]) and shares == 0
        if shares:
            unrealized = (close / entry_price - 1) * 100 if entry_price else 0
            forced_reason = ""
            if config.take_profit_pct > 0 and unrealized >= config.take_profit_pct:
                forced_reason = "止盈"
            elif config.stop_loss_pct > 0 and unrealized <= -config.stop_loss_pct:
                forced_reason = "止损"
            elif config.holding_days > 0 and held_days >= config.holding_days:
                forced_reason = "固定持有期"
            new_exit = bool(exit_signal.iloc[index])
            if not pending_sell:
                pending_sell = bool(forced_reason) or new_exit
                pending_sell_reason = forced_reason or ("EXIT" if new_exit else "")
        else:
            pending_sell = False
            pending_sell_reason = ""

    equity = pd.DataFrame(equity_rows).set_index("date")
    trades_frame = pd.DataFrame(trades)
    metrics, annual, monthly = calculate_performance(
        equity["equity"], config.initial_cash, trades_frame, maximum_losses, benchmark
    )
    return BacktestResult(
        metrics, equity.reset_index(), trades_frame, annual, monthly, raw_signals
    )


def calculate_performance(
    equity: pd.Series,
    initial_cash: float,
    trades: pd.DataFrame,
    maximum_losses: int = 0,
    benchmark: pd.DataFrame | None = None,
) -> tuple[dict[str, float | int | str], pd.DataFrame, pd.DataFrame]:
    returns = equity.pct_change().fillna(0)
    cumulative = equity.iloc[-1] / initial_cash - 1
    years = max(len(equity) / 242, 1 / 242)
    annualized = (1 + cumulative) ** (1 / years) - 1 if cumulative > -1 else -1
    volatility = float(returns.std(ddof=0) * np.sqrt(242))
    downside = float(returns[returns < 0].std(ddof=0) * np.sqrt(242))
    sharpe = annualized / volatility if volatility > 0 else 0
    sortino = annualized / downside if downside > 0 else 0
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = annualized / abs(max_drawdown) if max_drawdown < 0 else 0
    sells = (
        trades[trades.get("方向", pd.Series(dtype=str)) == "卖出"]
        if not trades.empty
        else pd.DataFrame()
    )
    pnl = pd.to_numeric(sells.get("盈亏", pd.Series(dtype=float)), errors="coerce")
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = len(wins) / len(pnl) if len(pnl) else 0
    profit_loss = wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else 0
    average_holding = pd.to_numeric(
        sells.get("持有天数", pd.Series(dtype=float)), errors="coerce"
    ).mean()
    dated = equity.copy()
    dated.index = pd.to_datetime(dated.index)
    annual = (
        dated.resample("YE")
        .last()
        .pct_change()
        .fillna(dated.resample("YE").last() / initial_cash - 1)
    )
    monthly = dated.resample("ME").last().pct_change().fillna(0)
    annual_frame = pd.DataFrame(
        {"年度": annual.index.year, "收益率": annual.values * 100}
    )
    monthly_frame = pd.DataFrame(
        {"月份": monthly.index.strftime("%Y-%m"), "收益率": monthly.values * 100}
    )
    excess = 0.0
    if benchmark is not None and not benchmark.empty and "close" in benchmark:
        bench = pd.to_numeric(benchmark["close"], errors="coerce").dropna()
        if len(bench) >= 2:
            excess = cumulative - (bench.iloc[-1] / bench.iloc[0] - 1)
    metrics: dict[str, float | int | str] = {
        "累计收益(%)": cumulative * 100,
        "年化收益(%)": annualized * 100,
        "最大回撤(%)": max_drawdown * 100,
        "夏普比率": sharpe,
        "Sortino比率": sortino,
        "Calmar比率": calmar,
        "胜率(%)": win_rate * 100,
        "盈亏比": float(profit_loss),
        "平均持仓时间(天)": float(average_holding) if pd.notna(average_holding) else 0,
        "最大连续亏损": maximum_losses,
        "对沪深300超额收益(%)": excess * 100,
        "交易次数": len(trades),
    }
    return metrics, annual_frame, monthly_frame
