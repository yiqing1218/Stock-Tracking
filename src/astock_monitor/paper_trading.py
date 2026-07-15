from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .factor_models import MODEL_BY_KEY
from .formula_engine import FormulaEngine
from .historical_store import HistoricalStore
from .models import Security, SecurityType
from .time_utils import beijing_now, beijing_today


@dataclass(frozen=True, slots=True)
class PaperAccountSummary:
    account_id: int
    name: str
    initial_cash: float
    cash: float
    market_value: float
    equity: float
    total_pnl: float
    total_return: float
    positions: int


@dataclass(frozen=True, slots=True)
class PaperOrderResult:
    order_id: int
    status: str
    message: str
    executed_price: float = 0.0
    quantity: int = 0


@dataclass(frozen=True, slots=True)
class PaperRule:
    id: int
    account_id: int
    name: str
    rule_kind: str
    model_key: str
    action: str
    scope: str
    conditions: tuple[dict[str, object], ...]
    position_pct: float
    max_positions: int
    enabled: bool
    last_run_at: str


CONDITION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("最新收盘价", "close", "close"),
    ("当日涨跌幅", "pct_change", "pct_change"),
    ("换手率", "turnover", "turnover"),
    ("成交额", "amount", "amount"),
    ("成交量比5日", "volume_ratio5", "volume/SMA(volume,5)"),
    ("20日动量", "mom20", "ROC(close,20)"),
    ("60日动量", "mom60", "ROC(close,60)"),
    ("RSI(14)", "rsi14", "100*SUM(MAX(returns,0),14)/SUM(ABS(returns),14)"),
    ("20日乖离率", "bias20", "(close/SMA(close,20)-1)*100"),
    ("20日波动率", "vol20", "STD(returns,20)*SQRT(252)"),
    ("20日平均换手", "turnover20", "SMA(turnover,20)"),
    ("20日平均成交额", "amount20", "SMA(amount,20)"),
)
CONDITION_FORMULAS = {key: formula for _, key, formula in CONDITION_FIELDS}


def compile_conditions(conditions: Iterable[dict[str, object]]) -> str:
    parts: list[str] = []
    for index, condition in enumerate(conditions):
        field = str(condition.get("field", ""))
        operator = str(condition.get("operator", ">"))
        connector = str(condition.get("connector", "且"))
        if field not in CONDITION_FORMULAS:
            raise ValueError(f"未知模拟交易条件：{field}")
        if operator not in {">", ">=", "<", "<=", "==", "!="}:
            raise ValueError(f"不支持的比较符：{operator}")
        try:
            threshold = float(condition.get("threshold", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("条件阈值必须是数字") from exc
        expression = f"({CONDITION_FORMULAS[field]} {operator} {threshold:g})"
        if connector == "非":
            expression = f"~{expression}"
        if index:
            joiner = " | " if connector == "或" else " & "
            parts.append(joiner)
        parts.append(expression)
    if not parts:
        raise ValueError("自定义自动交易至少需要一个条件")
    return "".join(parts)


class PaperTradingService:
    """Local-only paper ledger. It never connects to a broker or submits live orders."""

    COMMISSION_RATE = 0.0003
    MIN_COMMISSION = 5.0
    STAMP_TAX_RATE = 0.0005
    SLIPPAGE_PCT = 0.02

    def __init__(self, store: HistoricalStore) -> None:
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_roll_date TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    account_id INTEGER NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    available_quantity INTEGER NOT NULL DEFAULT 0,
                    average_cost REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, security_id)
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    requested_price REAL,
                    quote_price REAL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    rule_id INTEGER,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    filled_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_paper_orders_account
                    ON paper_orders(account_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES paper_orders(id) ON DELETE CASCADE,
                    account_id INTEGER NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    gross_amount REAL NOT NULL,
                    commission REAL NOT NULL,
                    stamp_tax REAL NOT NULL,
                    source TEXT NOT NULL,
                    traded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_account
                    ON paper_trades(account_id, traded_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS paper_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    rule_kind TEXT NOT NULL,
                    model_key TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT 'buy',
                    scope TEXT NOT NULL DEFAULT 'watchlist',
                    conditions_json TEXT NOT NULL DEFAULT '[]',
                    position_pct REAL NOT NULL DEFAULT 10,
                    max_positions INTEGER NOT NULL DEFAULT 10,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_rule_executions (
                    rule_id INTEGER NOT NULL REFERENCES paper_rules(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    side TEXT NOT NULL,
                    order_id INTEGER REFERENCES paper_orders(id) ON DELETE SET NULL,
                    signal_formula TEXT NOT NULL DEFAULT '',
                    signal_value REAL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(rule_id, trade_date, security_id, side)
                );
                """
            )
            now = self._now()
            db.execute(
                """INSERT OR IGNORE INTO paper_accounts
                (name,initial_cash,cash,last_roll_date,created_at,updated_at)
                VALUES('默认模拟账户',1000000,1000000,?,?,?)""",
                (self._today(), now, now),
            )

    @staticmethod
    def _now() -> str:
        return beijing_now().strftime("%Y-%m-%d %H:%M:%S%z")

    @staticmethod
    def _today() -> str:
        return beijing_today().isoformat()

    def default_account_id(self) -> int:
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM paper_accounts ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("模拟账户初始化失败")
        return int(row[0])

    def rollover(self, account_id: int) -> None:
        today = self._today()
        with self.store.connect() as db:
            row = db.execute(
                "SELECT last_roll_date FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                raise ValueError("模拟账户不存在")
            if str(row[0]) == today:
                return
            db.execute(
                "UPDATE paper_positions SET available_quantity=quantity,updated_at=? WHERE account_id=?",
                (self._now(), account_id),
            )
            db.execute(
                "UPDATE paper_accounts SET last_roll_date=?,updated_at=? WHERE id=?",
                (today, self._now(), account_id),
            )

    def reset_account(self, account_id: int, initial_cash: float = 1_000_000) -> None:
        if initial_cash <= 0:
            raise ValueError("初始资金必须大于0")
        now = self._now()
        with self.store.connect() as db:
            db.execute("DELETE FROM paper_rule_executions WHERE rule_id IN (SELECT id FROM paper_rules WHERE account_id=?)", (account_id,))
            db.execute("DELETE FROM paper_trades WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM paper_orders WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM paper_positions WHERE account_id=?", (account_id,))
            db.execute(
                "UPDATE paper_accounts SET initial_cash=?,cash=?,last_roll_date=?,updated_at=? WHERE id=?",
                (initial_cash, initial_cash, self._today(), now, account_id),
            )

    def execute_order(
        self,
        account_id: int,
        security: Security,
        side: str,
        quantity: int,
        quote_price: float,
        *,
        order_type: str = "market",
        limit_price: float | None = None,
        source: str = "manual",
        rule_id: int | None = None,
    ) -> PaperOrderResult:
        self.rollover(account_id)
        side = side.lower()
        quantity = int(quantity)
        now = self._now()
        security_id = self.store.upsert_security(security, "paper_trading")
        requested = float(limit_price) if limit_price is not None else None
        with self.store.connect() as db:
            cursor = db.execute(
                """INSERT INTO paper_orders
                (account_id,security_id,side,order_type,requested_quantity,requested_price,
                 quote_price,status,source,rule_id,created_at)
                VALUES(?,?,?,?,?,?,?,'checking',?,?,?)""",
                (
                    account_id,
                    security_id,
                    side,
                    order_type,
                    quantity,
                    requested,
                    quote_price,
                    source,
                    rule_id,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid)

            def reject(message: str, status: str = "rejected") -> PaperOrderResult:
                db.execute(
                    "UPDATE paper_orders SET status=?,message=? WHERE id=?",
                    (status, message, order_id),
                )
                return PaperOrderResult(order_id, status, message)

            if security.security_type is SecurityType.INDEX:
                return reject("指数不可直接交易，请选择股票或ETF")
            if side not in {"buy", "sell"}:
                return reject("买卖方向无效")
            if quantity <= 0 or quantity % 100:
                return reject("A股模拟委托数量必须为100股的整数倍")
            if not math.isfinite(quote_price) or quote_price <= 0:
                return reject("没有可用成交价格")
            if order_type == "limit":
                if requested is None or requested <= 0:
                    return reject("限价委托必须填写有效价格")
                crosses = (side == "buy" and requested >= quote_price) or (
                    side == "sell" and requested <= quote_price
                )
                if not crosses:
                    return reject("限价尚未触及，当前不成交", "unfilled")
            slippage = self.SLIPPAGE_PCT / 100
            executed_price = quote_price * (1 + slippage if side == "buy" else 1 - slippage)
            gross = executed_price * quantity
            commission = max(self.MIN_COMMISSION, gross * self.COMMISSION_RATE)
            stamp_tax = gross * self.STAMP_TAX_RATE if side == "sell" else 0.0
            account = db.execute(
                "SELECT cash FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if account is None:
                return reject("模拟账户不存在")
            cash = float(account[0])
            position = db.execute(
                "SELECT quantity,available_quantity,average_cost FROM paper_positions WHERE account_id=? AND security_id=?",
                (account_id, security_id),
            ).fetchone()
            old_qty = int(position[0]) if position else 0
            available = int(position[1]) if position else 0
            old_cost = float(position[2]) if position else 0.0
            if side == "buy":
                required = gross + commission
                if required > cash + 1e-8:
                    return reject(f"模拟资金不足，需要 {required:,.2f} 元")
                new_qty = old_qty + quantity
                new_cost = (old_cost * old_qty + gross + commission) / new_qty
                new_cash = cash - required
                new_available = available  # T+1: 当日买入不可卖
            else:
                if quantity > available:
                    return reject(f"T+1 可卖数量不足，当前可卖 {available} 股")
                new_qty = old_qty - quantity
                new_cost = old_cost if new_qty else 0.0
                new_cash = cash + gross - commission - stamp_tax
                new_available = available - quantity
            db.execute(
                "UPDATE paper_accounts SET cash=?,updated_at=? WHERE id=?",
                (new_cash, now, account_id),
            )
            if new_qty:
                db.execute(
                    """INSERT INTO paper_positions
                    (account_id,security_id,quantity,available_quantity,average_cost,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,security_id) DO UPDATE SET
                    quantity=excluded.quantity,available_quantity=excluded.available_quantity,
                    average_cost=excluded.average_cost,updated_at=excluded.updated_at""",
                    (account_id, security_id, new_qty, new_available, new_cost, now),
                )
            else:
                db.execute(
                    "DELETE FROM paper_positions WHERE account_id=? AND security_id=?",
                    (account_id, security_id),
                )
            db.execute(
                """INSERT INTO paper_trades
                (order_id,account_id,security_id,side,quantity,price,gross_amount,
                 commission,stamp_tax,source,traded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id,
                    account_id,
                    security_id,
                    side,
                    quantity,
                    executed_price,
                    gross,
                    commission,
                    stamp_tax,
                    source,
                    now,
                ),
            )
            db.execute(
                "UPDATE paper_orders SET status='filled',message='模拟成交',filled_at=? WHERE id=?",
                (now, order_id),
            )
        return PaperOrderResult(order_id, "filled", "模拟成交", executed_price, quantity)

    def summary(self, account_id: int) -> PaperAccountSummary:
        self.rollover(account_id)
        with self.store.connect() as db:
            account = db.execute(
                "SELECT name,initial_cash,cash FROM paper_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise ValueError("模拟账户不存在")
            rows = db.execute(
                """SELECT p.quantity,p.average_cost,
                COALESCE((SELECT close FROM daily_bars d WHERE d.security_id=p.security_id
                    ORDER BY trade_date DESC,CASE adjustment WHEN 'qfq' THEN 0 ELSE 1 END LIMIT 1),p.average_cost) AS price
                FROM paper_positions p WHERE p.account_id=?""",
                (account_id,),
            ).fetchall()
        initial = float(account[1])
        cash = float(account[2])
        market_value = sum(int(row[0]) * float(row[2] or row[1]) for row in rows)
        equity = cash + market_value
        pnl = equity - initial
        return PaperAccountSummary(
            account_id,
            str(account[0]),
            initial,
            cash,
            market_value,
            equity,
            pnl,
            pnl / initial if initial else 0.0,
            len(rows),
        )

    def positions(self, account_id: int) -> pd.DataFrame:
        self.rollover(account_id)
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT s.name,s.full_symbol AS code,p.quantity,p.available_quantity,
                p.average_cost,COALESCE((SELECT close FROM daily_bars d
                    WHERE d.security_id=p.security_id ORDER BY trade_date DESC,
                    CASE adjustment WHEN 'qfq' THEN 0 ELSE 1 END LIMIT 1),p.average_cost) AS latest_price
                FROM paper_positions p JOIN securities s ON s.id=p.security_id
                WHERE p.account_id=? ORDER BY s.code""",
                (account_id,),
            ).fetchall()
        result = []
        for row in rows:
            latest = float(row[5] or row[4])
            cost = float(row[4])
            quantity = int(row[2])
            result.append(
                {
                    "名称": row[0],
                    "代码": row[1],
                    "持仓": quantity,
                    "可卖": int(row[3]),
                    "成本": cost,
                    "最新价": latest,
                    "市值": latest * quantity,
                    "浮动盈亏": (latest - cost) * quantity,
                }
            )
        return pd.DataFrame(result)

    def orders(self, account_id: int, limit: int = 200) -> pd.DataFrame:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT o.id,s.name,s.full_symbol,o.side,o.order_type,o.requested_quantity,
                o.requested_price,o.quote_price,o.status,o.source,o.message,o.created_at
                FROM paper_orders o JOIN securities s ON s.id=o.security_id
                WHERE o.account_id=? ORDER BY o.id DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        return pd.DataFrame(
            [dict(row) for row in rows],
            columns=[
                "id", "name", "code", "side", "order_type", "quantity",
                "requested_price", "quote_price", "status", "source", "message", "created_at",
            ],
        )

    def trades(self, account_id: int, limit: int = 200) -> pd.DataFrame:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT t.id,s.name,s.full_symbol,t.side,t.quantity,t.price,
                t.gross_amount,t.commission,t.stamp_tax,t.source,t.traded_at
                FROM paper_trades t JOIN securities s ON s.id=t.security_id
                WHERE t.account_id=? ORDER BY t.id DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        return pd.DataFrame([dict(row) for row in rows])

    def save_rule(
        self,
        account_id: int,
        name: str,
        rule_kind: str,
        *,
        model_key: str = "",
        action: str = "buy",
        scope: str = "watchlist",
        conditions: Iterable[dict[str, object]] = (),
        position_pct: float = 10,
        max_positions: int = 10,
    ) -> int:
        if rule_kind not in {"factor", "custom"}:
            raise ValueError("自动规则类型无效")
        if rule_kind == "factor":
            model = MODEL_BY_KEY.get(model_key)
            if model is None:
                raise ValueError("请选择量化因子模型")
            if not model.executable:
                raise ValueError(f"该模型需要{model.data_requirement}，当前不能无编造执行")
        else:
            compile_conditions(conditions)
        now = self._now()
        payload = json.dumps(list(conditions), ensure_ascii=False)
        with self.store.connect() as db:
            cursor = db.execute(
                """INSERT INTO paper_rules
                (account_id,name,rule_kind,model_key,action,scope,conditions_json,
                 position_pct,max_positions,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    account_id,
                    name.strip() or "自动模拟规则",
                    rule_kind,
                    model_key,
                    action,
                    scope,
                    payload,
                    max(1.0, min(float(position_pct), 100.0)),
                    max(1, int(max_positions)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def rules(self, account_id: int) -> list[PaperRule]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM paper_rules WHERE account_id=? ORDER BY id DESC",
                (account_id,),
            ).fetchall()
        result: list[PaperRule] = []
        for row in rows:
            try:
                conditions = tuple(json.loads(row["conditions_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                conditions = ()
            result.append(
                PaperRule(
                    int(row["id"]), int(row["account_id"]), str(row["name"]),
                    str(row["rule_kind"]), str(row["model_key"]), str(row["action"]),
                    str(row["scope"]), conditions, float(row["position_pct"]),
                    int(row["max_positions"]), bool(row["enabled"]), str(row["last_run_at"]),
                )
            )
        return result

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE paper_rules SET enabled=?,updated_at=? WHERE id=?",
                (int(enabled), self._now(), rule_id),
            )

    def run_rule(self, rule_id: int, securities: Iterable[Security]) -> dict[str, int]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM paper_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise ValueError("自动模拟规则不存在")
        if not bool(row["enabled"]):
            raise ValueError("该自动模拟规则已停用")
        kind = str(row["rule_kind"])
        action = str(row["action"])
        if kind == "factor":
            model = MODEL_BY_KEY.get(str(row["model_key"]))
            if model is None or not model.executable:
                raise ValueError("该因子模型当前不可执行")
            entry_formula = model.entry_formula
            exit_formula = model.exit_formula
            score_formula = model.score_formula
        else:
            entry_formula = compile_conditions(json.loads(row["conditions_json"] or "[]"))
            exit_formula = ""
            score_formula = ""
        account_id = int(row["account_id"])
        today = self._today()
        scanned = signals = filled = skipped = 0
        candidates: list[
            tuple[Security, str, str, float, float, int]
        ] = []
        for security in securities:
            if security.security_type is not SecurityType.STOCK:
                continue
            frame = self.store.get_bars(security, "qfq", limit=320)
            if len(frame) < 25:
                continue
            scanned += 1
            engine = FormulaEngine(frame)
            with self.store.connect() as db:
                sid = self.store.security_id(security)
                position = db.execute(
                    "SELECT quantity,available_quantity FROM paper_positions WHERE account_id=? AND security_id=?",
                    (account_id, sid),
                ).fetchone() if sid is not None else None
            has_position = bool(position and int(position[0]) > 0)
            signal_side = action
            formula = entry_formula
            if kind == "factor" and has_position and exit_formula:
                signal_side = "sell"
                formula = exit_formula
            elif kind == "factor" and has_position:
                continue
            try:
                value = engine.evaluate(formula).iloc[-1]
            except Exception:
                skipped += 1
                continue
            if pd.isna(value) or not bool(value):
                continue
            price = float(frame.iloc[-1]["close"])
            signal_value = float(value)
            if kind == "factor" and signal_side == "buy":
                try:
                    score = engine.evaluate(score_formula).iloc[-1]
                except Exception:
                    skipped += 1
                    continue
                if pd.isna(score) or not math.isfinite(float(score)):
                    skipped += 1
                    continue
                signal_value = float(score)
                formula = f"ENTRY: {entry_formula}; SCORE: {score_formula}"
            available = int(position[1]) if position else 0
            candidates.append(
                (security, signal_side, formula, signal_value, price, available)
            )

        # 先处理模型退出，再按模型评分从高到低买入；自定义条件保持证券顺序。
        if kind == "factor":
            candidates.sort(
                key=lambda item: (0 if item[1] == "sell" else 1, -item[3])
            )
        signals = len(candidates)
        for security, signal_side, formula, signal_value, price, available in candidates:
            sid = self.store.security_id(security)
            if sid is None:
                skipped += 1
                continue
            with self.store.connect() as db:
                duplicate = db.execute(
                    "SELECT 1 FROM paper_rule_executions WHERE rule_id=? AND trade_date=? AND security_id=? AND side=?",
                    (rule_id, today, sid, signal_side),
                ).fetchone()
            if duplicate:
                skipped += 1
                continue
            if signal_side == "sell":
                quantity = available
            else:
                summary = self.summary(account_id)
                if summary.positions >= int(row["max_positions"]):
                    skipped += 1
                    continue
                budget = summary.cash * float(row["position_pct"]) / 100
                quantity = int(budget // (price * 100)) * 100
            if quantity <= 0:
                skipped += 1
                continue
            result = self.execute_order(
                account_id,
                security,
                signal_side,
                quantity,
                price,
                source=f"rule:{rule_id}",
                rule_id=rule_id,
            )
            with self.store.connect() as db:
                db.execute(
                    """INSERT OR IGNORE INTO paper_rule_executions
                    (rule_id,trade_date,security_id,side,order_id,signal_formula,signal_value,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        rule_id,
                        today,
                        sid,
                        signal_side,
                        result.order_id,
                        formula,
                        signal_value,
                        self._now(),
                    ),
                )
            if result.status == "filled":
                filled += 1
            else:
                skipped += 1
        with self.store.connect() as db:
            db.execute(
                "UPDATE paper_rules SET last_run_at=?,updated_at=? WHERE id=?",
                (self._now(), self._now(), rule_id),
            )
        return {"scanned": scanned, "signals": signals, "filled": filled, "skipped": skipped}
