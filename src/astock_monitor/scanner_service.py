from __future__ import annotations

import json

import pandas as pd

from .historical_store import HistoricalStore
from .models import Security
from .screening import ScreeningCondition


BUILTIN_SCANS: tuple[dict[str, object], ...] = (
    {
        "name": "20日放量突破",
        "description": "收盘强度与20日成交量扩张的本地历史筛选模板",
        "template": "volume_breakout",
    },
    {
        "name": "均线多头排列",
        "description": "短中期均线按多头顺序排列的趋势模板",
        "template": "ma_bull",
    },
    {
        "name": "超卖后回升",
        "description": "动量指标从超卖区回升的观察模板",
        "template": "oversold_rebound",
    },
)


class ScannerRepository:
    def __init__(self, store: HistoricalStore) -> None:
        self.store = store

    def save_definition(
        self,
        name: str,
        conditions: list[ScreeningCondition],
        description: str = "",
        date_mode: str = "latest_completed_day",
        fixed_date: str = "",
        adjustment: str = "qfq",
        result_limit: int = 500,
    ) -> int:
        formula = [
            {
                "connector": item.connector,
                "column": item.definition.column,
                "name": item.definition.name,
                "category": item.definition.category,
                "operator": item.operator,
                "threshold": item.threshold,
            }
            for item in conditions
        ]
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO scan_definitions(name,description,formula_json,date_mode,fixed_date,adjustment,result_limit)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                formula_json=excluded.formula_json,date_mode=excluded.date_mode,fixed_date=excluded.fixed_date,
                adjustment=excluded.adjustment,result_limit=excluded.result_limit,updated_at=CURRENT_TIMESTAMP""",
                (
                    name.strip(),
                    description.strip(),
                    json.dumps(formula, ensure_ascii=False),
                    date_mode,
                    fixed_date,
                    adjustment,
                    result_limit,
                ),
            )
            row = db.execute(
                "SELECT id FROM scan_definitions WHERE name=?", (name.strip(),)
            ).fetchone()
        return int(row[0])

    def list_definitions(self) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM scan_definitions ORDER BY updated_at DESC,id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_run(
        self, name: str, definition_id: int | None, target_date: str, total: int
    ) -> int:
        with self.store.connect() as db:
            cursor = db.execute(
                "INSERT INTO scan_runs(definition_id,name,target_date,total_count) VALUES(?,?,?,?)",
                (definition_id, name, target_date, total),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, frame: pd.DataFrame, scanned: int, errors: int = 0
    ) -> None:
        with self.store.connect() as db:
            for _, row in frame.iterrows():
                security = row.get("security")
                if not isinstance(security, Security):
                    continue
                security_id = self.store.security_id(security)
                if security_id is None:
                    continue
                values = {
                    str(k): self._json_value(v)
                    for k, v in row.items()
                    if k not in {"security"}
                }
                forward = {
                    key: value
                    for key, value in values.items()
                    if key.endswith("日后收益%")
                }
                db.execute(
                    """INSERT OR REPLACE INTO scan_results(run_id,security_id,trade_date,trigger_price,score,values_json)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        run_id,
                        security_id,
                        values.get("日期", ""),
                        values.get("收盘价"),
                        values.get("评分"),
                        json.dumps(values, ensure_ascii=False),
                    ),
                )
                db.execute(
                    "UPDATE scan_results SET forward_returns_json=?,completeness=? WHERE run_id=? AND security_id=?",
                    (
                        json.dumps(forward, ensure_ascii=False),
                        float(values.get("数据完整度") or 0),
                        run_id,
                        security_id,
                    ),
                )
            db.execute(
                "UPDATE scan_runs SET status='completed',scanned_count=?,matched_count=?,error_count=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (scanned, len(frame), errors, run_id),
            )

    def save_dynamic_group(self, name: str, definition_id: int, run_id: int) -> int:
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO dynamic_groups(name,definition_id,last_run_id) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET definition_id=excluded.definition_id,last_run_id=excluded.last_run_id",
                (name, definition_id, run_id),
            )
            group_id = int(
                db.execute(
                    "SELECT id FROM dynamic_groups WHERE name=?", (name,)
                ).fetchone()[0]
            )
            db.execute(
                "DELETE FROM dynamic_group_members WHERE group_id=?", (group_id,)
            )
            db.execute(
                "INSERT INTO dynamic_group_members(group_id,security_id,score) SELECT ?,security_id,score FROM scan_results WHERE run_id=?",
                (group_id, run_id),
            )
        return group_id

    @staticmethod
    def _json_value(value: object) -> object:
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value
