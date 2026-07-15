from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .historical_store import HistoricalStore
from .formula_engine import FormulaEngine
from .models import Quote, Security
from .models import SecurityType
from .time_utils import beijing_now


@dataclass(slots=True)
class AlertEvent:
    rule_id: int
    security: Security
    title: str
    message: str
    snapshot: dict[str, object]


class AlertEngine:
    """Persistent edge-triggered alert evaluator with cooldown and dedupe."""

    def __init__(self, store: HistoricalStore) -> None:
        self.store = store

    def save_rule(
        self,
        name: str,
        condition_key: str,
        comparison_mode: str,
        threshold: float | None,
        target_type: str,
        target_value: str = "",
        cooldown_seconds: int = 300,
        rule_type: str = "quote",
        formula: str = "",
        bar_mode: str = "completed",
    ) -> int:
        with self.store.connect() as db:
            cursor = db.execute(
                """INSERT INTO alert_rules(name,rule_type,condition_key,comparison_mode,threshold,formula,bar_mode,cooldown_seconds)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    name,
                    rule_type,
                    condition_key,
                    comparison_mode,
                    threshold,
                    formula,
                    bar_mode,
                    cooldown_seconds,
                ),
            )
            rule_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO alert_rule_targets(rule_id,target_type,target_value) VALUES(?,?,?)",
                (rule_id, target_type, target_value),
            )
        return rule_id

    def list_rules(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE r.enabled=1" if enabled_only else ""
        with self.store.connect() as db:
            rows = db.execute(f"""SELECT r.*,t.target_type,t.target_value FROM alert_rules r
            JOIN alert_rule_targets t ON t.rule_id=r.id {where} ORDER BY r.id DESC""").fetchall()
        return [dict(row) for row in rows]

    def evaluate_quote(self, rule: dict, quote: Quote) -> AlertEvent | None:
        value = self._quote_value(str(rule["condition_key"]), quote)
        if value is None:
            return None
        snapshot = {
            "price": quote.price,
            "change_pct": quote.change_pct,
            "amount": quote.amount,
            "turnover": quote.turnover,
            "volume_ratio": quote.volume_ratio,
            "value": value,
            "threshold": rule["threshold"],
            "source": quote.extra.get("source", ""),
        }
        return self.evaluate_value(rule, quote.security, value, snapshot)

    def evaluate_history(self, rule: dict, security: Security) -> AlertEvent | None:
        frame = self.store.get_bars(
            security, adjustment=str(rule.get("adjustment", "qfq") or "qfq"), limit=1200
        )
        if frame.empty:
            return None
        if str(rule.get("bar_mode", "completed")) == "completed" and bool(
            frame.iloc[-1].get("is_temporary", 0)
        ):
            frame = frame.iloc[:-1]
        if frame.empty:
            return None
        formula = str(rule.get("formula", "")).strip()
        if formula:
            series = FormulaEngine(frame).evaluate(formula)
            value = (
                float(series.dropna().iloc[-1]) if not series.dropna().empty else None
            )
        else:
            raw = frame.iloc[-1].get(str(rule["condition_key"]))
            value = float(raw) if raw is not None and not pd.isna(raw) else None
        if value is None:
            return None
        snapshot = {
            "date": str(frame.iloc[-1]["date"]),
            "value": value,
            "threshold": rule["threshold"],
            "source": "本地历史仓库",
            "formula": formula,
        }
        return self.evaluate_value(rule, security, value, snapshot)

    def evaluate_value(
        self, rule: dict, security: Security, value: float, snapshot: dict[str, object]
    ) -> AlertEvent | None:
        security_id = self.store.upsert_security(security, "提醒目标")
        with self.store.connect() as db:
            state = db.execute(
                "SELECT * FROM alert_states WHERE rule_id=? AND security_id=?",
                (rule["id"], security_id),
            ).fetchone()
            previous = (
                float(state["last_value"])
                if state and state["last_value"] is not None
                else None
            )
            previous_truth = bool(state["last_truth"]) if state else False
            truth = self._truth(
                str(rule["comparison_mode"]),
                value,
                previous,
                rule["threshold"],
                rule["secondary_threshold"],
            )
            now = beijing_now()
            last_triggered = None
            if state and state["last_triggered_at"]:
                try:
                    last_triggered = datetime.fromisoformat(
                        str(state["last_triggered_at"])
                    )
                except ValueError:
                    pass
            cooldown_ok = last_triggered is None or now - last_triggered >= timedelta(
                seconds=int(rule["cooldown_seconds"] or 0)
            )
            triggered = truth and not previous_truth and cooldown_ok
            triggered_at = (
                now.isoformat()
                if triggered
                else (state["last_triggered_at"] if state else "")
            )
            db.execute(
                """INSERT INTO alert_states(rule_id,security_id,last_value,last_truth,last_evaluated_at,last_triggered_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(rule_id,security_id) DO UPDATE SET last_value=excluded.last_value,last_truth=excluded.last_truth,last_evaluated_at=excluded.last_evaluated_at,last_triggered_at=excluded.last_triggered_at""",
                (
                    rule["id"],
                    security_id,
                    value,
                    int(truth),
                    now.isoformat(),
                    triggered_at,
                ),
            )
            if not triggered:
                return None
            title = f"{security.name}：{rule['name']}"
            key = hashlib.sha256(
                f"{rule['id']}:{security_id}:{now:%Y%m%d%H%M%S}".encode()
            ).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO alert_events(rule_id,security_id,event_key,title,message,snapshot_json,triggered_at) VALUES(?,?,?,?,?,?,?)",
                (
                    rule["id"],
                    security_id,
                    key,
                    title,
                    f"当前值 {value:g}",
                    json.dumps(snapshot, ensure_ascii=False),
                    now.isoformat(),
                ),
            )
        return AlertEvent(
            int(rule["id"]), security, title, f"当前值 {value:g}", snapshot
        )

    def resolve_targets(self, rule: dict) -> list[Security]:
        kind, value = (
            str(rule.get("target_type", "single")),
            str(rule.get("target_value", "")),
        )
        with self.store.connect() as db:
            if kind == "single":
                security_type, _, code = value.partition(":")
                rows = db.execute(
                    "SELECT code,name,security_type,market FROM securities WHERE code=? AND security_type=? LIMIT 1",
                    (code, security_type),
                ).fetchall()
            elif kind == "group":
                rows = db.execute(
                    "SELECT w.code,w.name,w.security_type,w.market FROM watchlist w WHERE w.group_id=?",
                    (value,),
                ).fetchall()
            elif kind == "dynamic_group":
                rows = db.execute(
                    """SELECT s.code,s.name,s.security_type,s.market FROM dynamic_group_members m JOIN securities s ON s.id=m.security_id WHERE m.group_id=?""",
                    (value,),
                ).fetchall()
            elif kind == "scan_result":
                rows = db.execute(
                    """SELECT s.code,s.name,s.security_type,s.market FROM scan_results r JOIN securities s ON s.id=r.security_id WHERE r.run_id=?""",
                    (value,),
                ).fetchall()
            elif kind == "all_etfs":
                rows = db.execute(
                    "SELECT code,name,security_type,market FROM securities WHERE active=1 AND security_type='etf'"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT code,name,security_type,market FROM securities WHERE active=1 AND security_type='stock'"
                ).fetchall()
        return [
            Security(
                row["code"],
                row["name"],
                SecurityType(row["security_type"]),
                row["market"],
            )
            for row in rows
        ]

    @staticmethod
    def _quote_value(key: str, quote: Quote) -> float | None:
        mapping = {
            "price": quote.price,
            "change_pct": quote.change_pct,
            "amount": quote.amount,
            "turnover": quote.turnover,
            "volume_ratio": quote.volume_ratio,
        }
        value = mapping.get(key)
        return float(value) if value is not None else None

    @staticmethod
    def _truth(
        mode: str, value: float, previous: float | None, first: object, second: object
    ) -> bool:
        threshold = float(first or 0)
        high = float(second or threshold)
        if mode == "above":
            return value >= threshold
        if mode == "below":
            return value <= threshold
        if mode == "cross_up":
            return previous is not None and previous < threshold <= value
        if mode == "cross_down":
            return previous is not None and previous > threshold >= value
        if mode == "enter":
            return threshold <= value <= high
        if mode == "leave":
            return (
                previous is not None
                and threshold <= previous <= high
                and not threshold <= value <= high
            )
        return False

    def list_events(self, unread_only: bool = False, limit: int = 500) -> list[dict]:
        where = "WHERE e.is_read=0" if unread_only else ""
        with self.store.connect() as db:
            rows = db.execute(
                f"""SELECT e.*,s.code,s.name,s.market,s.security_type FROM alert_events e
            LEFT JOIN securities s ON s.id=e.security_id {where} ORDER BY e.triggered_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_read(self, event_id: int | None = None) -> None:
        with self.store.connect() as db:
            if event_id is None:
                db.execute("UPDATE alert_events SET is_read=1")
            else:
                db.execute("UPDATE alert_events SET is_read=1 WHERE id=?", (event_id,))
