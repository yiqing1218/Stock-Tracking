from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from .models import Security, SecurityType
from .time_utils import beijing_now


ADJUSTMENTS = {"", "qfq", "hfq"}


@dataclass(slots=True)
class WarehouseSummary:
    securities: int
    bars: int
    first_date: str
    last_date: str
    database_bytes: int
    issues: int


class HistoricalStore:
    """SQLite-backed historical warehouse shared by sync, scans and alerts.

    Connections are deliberately short-lived. WAL plus a busy timeout lets UI readers
    continue while one background writer commits batches.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS securities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    security_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    full_symbol TEXT NOT NULL DEFAULT '',
                    list_date TEXT NOT NULL DEFAULT '',
                    delist_date TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    industry TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, market, security_type)
                );
                CREATE TABLE IF NOT EXISTS daily_bars (
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL,
                    adjustment TEXT NOT NULL DEFAULT '',
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, amount REAL, turnover REAL,
                    pct_change REAL, change REAL, amplitude REAL,
                    source TEXT NOT NULL DEFAULT '',
                    source_timestamp TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    quality_flags TEXT NOT NULL DEFAULT '',
                    is_temporary INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(security_id, trade_date, adjustment)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_bars_date
                    ON daily_bars(trade_date, adjustment, security_id);
                CREATE INDEX IF NOT EXISTS idx_daily_bars_security
                    ON daily_bars(security_id, adjustment, trade_date DESC);

                CREATE TABLE IF NOT EXISTS sync_state (
                    scope_key TEXT PRIMARY KEY,
                    last_trade_date TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    cursor TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    adjustment TEXT NOT NULL DEFAULT 'qfq',
                    mode TEXT NOT NULL DEFAULT 'incremental',
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sync_job_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
                    security_key TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS data_quality_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id INTEGER REFERENCES securities(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL DEFAULT '',
                    adjustment TEXT NOT NULL DEFAULT '',
                    issue_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    details TEXT NOT NULL DEFAULT '',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(security_id, trade_date, adjustment, issue_type)
                );

                CREATE TABLE IF NOT EXISTS scan_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    security_types TEXT NOT NULL DEFAULT '["stock"]',
                    universe TEXT NOT NULL DEFAULT 'all',
                    group_id INTEGER,
                    formula_json TEXT NOT NULL,
                    date_mode TEXT NOT NULL DEFAULT 'latest_completed_day',
                    fixed_date TEXT NOT NULL DEFAULT '',
                    lookback INTEGER NOT NULL DEFAULT 620,
                    min_history INTEGER NOT NULL DEFAULT 30,
                    adjustment TEXT NOT NULL DEFAULT 'qfq',
                    prefilters_json TEXT NOT NULL DEFAULT '{}',
                    sort_json TEXT NOT NULL DEFAULT '[]',
                    result_limit INTEGER NOT NULL DEFAULT 500,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id INTEGER REFERENCES scan_definitions(id) ON DELETE SET NULL,
                    name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    target_date TEXT NOT NULL DEFAULT '',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS scan_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL,
                    trigger_price REAL,
                    score REAL,
                    values_json TEXT NOT NULL DEFAULT '{}',
                    forward_returns_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'local_warehouse',
                    completeness REAL NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id, security_id)
                );
                CREATE INDEX IF NOT EXISTS idx_scan_results_run ON scan_results(run_id, score DESC);
                CREATE TABLE IF NOT EXISTS dynamic_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    definition_id INTEGER REFERENCES scan_definitions(id) ON DELETE CASCADE,
                    refresh_mode TEXT NOT NULL DEFAULT 'manual',
                    last_run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS dynamic_group_members (
                    group_id INTEGER NOT NULL REFERENCES dynamic_groups(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    score REAL,
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(group_id, security_id)
                );

                CREATE TABLE IF NOT EXISTS alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    rule_type TEXT NOT NULL DEFAULT 'quote',
                    condition_key TEXT NOT NULL,
                    comparison_mode TEXT NOT NULL DEFAULT 'cross_up',
                    threshold REAL,
                    secondary_threshold REAL,
                    formula TEXT NOT NULL DEFAULT '',
                    bar_mode TEXT NOT NULL DEFAULT 'completed',
                    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS alert_rule_targets (
                    rule_id INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL,
                    target_value TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(rule_id, target_type, target_value)
                );
                CREATE TABLE IF NOT EXISTS alert_states (
                    rule_id INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    last_value REAL,
                    last_truth INTEGER NOT NULL DEFAULT 0,
                    last_evaluated_at TEXT NOT NULL DEFAULT '',
                    last_triggered_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(rule_id, security_id)
                );
                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL,
                    security_id INTEGER REFERENCES securities(id) ON DELETE SET NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    triggered_at TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_unread ON alert_events(is_read, triggered_at DESC);
                CREATE TABLE IF NOT EXISTS notification_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    desktop_enabled INTEGER NOT NULL DEFAULT 1,
                    sound_enabled INTEGER NOT NULL DEFAULT 0,
                    max_per_minute INTEGER NOT NULL DEFAULT 4,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO notification_settings(id) VALUES(1);

                CREATE TABLE IF NOT EXISTS event_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    source_document_id TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    official_source INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_name, source_document_id, source_url)
                );
                CREATE TABLE IF NOT EXISTS company_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_subtype TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    announcement_date TEXT NOT NULL DEFAULT '',
                    event_date TEXT NOT NULL DEFAULT '',
                    effective_date TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    source_document_id TEXT NOT NULL DEFAULT '',
                    official_source INTEGER NOT NULL DEFAULT 0,
                    importance INTEGER NOT NULL DEFAULT 0,
                    sentiment TEXT NOT NULL DEFAULT 'unknown',
                    amount REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    counterparty TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    dedup_key TEXT NOT NULL UNIQUE,
                    related_event_key TEXT NOT NULL DEFAULT '',
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_company_events_security_date
                    ON company_events(security_id, announcement_date DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_company_events_type_date
                    ON company_events(event_type, announcement_date DESC);
                CREATE TABLE IF NOT EXISTS event_raw_payloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER REFERENCES company_events(id) ON DELETE CASCADE,
                    source_id INTEGER REFERENCES event_sources(id) ON DELETE SET NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS event_sync_state (
                    scope_key TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL DEFAULT '',
                    first_sync_completed INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS event_research_notes (
                    event_id INTEGER PRIMARY KEY REFERENCES company_events(id) ON DELETE CASCADE,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    up_count INTEGER, down_count INTEGER, flat_count INTEGER,
                    limit_up_count INTEGER, limit_down_count INTEGER, broken_limit_count INTEGER,
                    max_limit_streak INTEGER, amount REAL, amount_change REAL,
                    median_return REAL, equal_weight_return REAL,
                    market_volatility REAL, chengjian_market_score REAL,
                    source TEXT NOT NULL DEFAULT '',
                    UNIQUE(trade_date, snapshot_time)
                );
                CREATE INDEX IF NOT EXISTS idx_market_snapshots_date
                    ON market_snapshots(trade_date, snapshot_time DESC);
                CREATE TABLE IF NOT EXISTS board_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_code TEXT NOT NULL DEFAULT '',
                    board_name TEXT NOT NULL,
                    board_type TEXT NOT NULL,
                    classification_source TEXT NOT NULL,
                    level INTEGER NOT NULL DEFAULT 1,
                    parent_id INTEGER REFERENCES board_definitions(id) ON DELETE SET NULL,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(board_name, board_type, classification_source)
                );
                CREATE TABLE IF NOT EXISTS board_members (
                    board_id INTEGER NOT NULL REFERENCES board_definitions(id) ON DELETE CASCADE,
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    weight REAL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(board_id, security_id, effective_from)
                );
                CREATE INDEX IF NOT EXISTS idx_board_members_security
                    ON board_members(security_id, board_id, effective_to);
                CREATE TABLE IF NOT EXISTS board_snapshots (
                    board_id INTEGER NOT NULL REFERENCES board_definitions(id) ON DELETE CASCADE,
                    snapshot_time TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    change_pct REAL, amount REAL, amount_change REAL,
                    up_count INTEGER, down_count INTEGER, limit_up_count INTEGER,
                    leader_code TEXT NOT NULL DEFAULT '', laggard_code TEXT NOT NULL DEFAULT '',
                    fund_flow REAL, pe REAL, pb REAL, dividend_yield REAL,
                    return_5d REAL, return_20d REAL, volatility_20d REAL,
                    chengjian_heat REAL, strong_streak INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(board_id, snapshot_time)
                );
                CREATE INDEX IF NOT EXISTS idx_board_snapshots_date
                    ON board_snapshots(trade_date, board_id);
                CREATE TABLE IF NOT EXISTS board_history (
                    board_id INTEGER NOT NULL REFERENCES board_definitions(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL,
                    close REAL, change_pct REAL, amount REAL, volume REAL,
                    heat_rank INTEGER, change_rank INTEGER, fund_flow REAL,
                    source TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(board_id, trade_date)
                );
                CREATE TABLE IF NOT EXISTS security_board_map (
                    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    board_id INTEGER NOT NULL REFERENCES board_definitions(id) ON DELETE CASCADE,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    effective_from TEXT NOT NULL,
                    effective_to TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(security_id, board_id, effective_from)
                );

                CREATE TABLE IF NOT EXISTS backtest_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    universe TEXT NOT NULL,
                    security_types TEXT NOT NULL DEFAULT '["stock"]',
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    adjustment TEXT NOT NULL DEFAULT 'qfq',
                    entry_formula TEXT NOT NULL,
                    exit_formula TEXT NOT NULL,
                    score_formula TEXT NOT NULL DEFAULT '',
                    holding_period INTEGER NOT NULL DEFAULT 0,
                    take_profit REAL NOT NULL DEFAULT 0,
                    stop_loss REAL NOT NULL DEFAULT 0,
                    initial_cash REAL NOT NULL DEFAULT 1000000,
                    position_sizing TEXT NOT NULL DEFAULT 'equal_weight',
                    max_positions INTEGER NOT NULL DEFAULT 10,
                    rebalance_frequency TEXT NOT NULL DEFAULT 'daily',
                    commission_rate REAL NOT NULL DEFAULT 0.0003,
                    minimum_commission REAL NOT NULL DEFAULT 5,
                    stamp_tax_rate REAL NOT NULL DEFAULT 0.0005,
                    slippage_model TEXT NOT NULL DEFAULT 'fixed_pct',
                    slippage_value REAL NOT NULL DEFAULT 0.02,
                    benchmark TEXT NOT NULL DEFAULT 'index:000300',
                    execution_price TEXT NOT NULL DEFAULT 'next_open',
                    exclude_st INTEGER NOT NULL DEFAULT 1,
                    minimum_listing_days INTEGER NOT NULL DEFAULT 20,
                    config_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id INTEGER REFERENCES backtest_definitions(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    data_version TEXT NOT NULL DEFAULT '',
                    code_version TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    current_date TEXT NOT NULL DEFAULT '',
                    current_security TEXT NOT NULL DEFAULT '',
                    error_count INTEGER NOT NULL DEFAULT 0,
                    bias_notes TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
                    security_id INTEGER REFERENCES securities(id) ON DELETE SET NULL,
                    signal_date TEXT NOT NULL DEFAULT '', order_date TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '', side TEXT NOT NULL,
                    requested_price REAL, executed_price REAL, quantity INTEGER,
                    gross_amount REAL, commission REAL, stamp_tax REAL, slippage REAL,
                    reason TEXT NOT NULL DEFAULT '', position_after INTEGER, cash_after REAL,
                    filled INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_trades_run
                    ON backtest_trades(run_id, trade_date, id);
                CREATE TABLE IF NOT EXISTS backtest_equity (
                    run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
                    benchmark_equity REAL, drawdown REAL, exposure REAL,
                    PRIMARY KEY(run_id, trade_date)
                );
                CREATE TABLE IF NOT EXISTS backtest_positions (
                    run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
                    trade_date TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL, market_value REAL NOT NULL, cost REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    PRIMARY KEY(run_id, trade_date, security_id)
                );
                CREATE TABLE IF NOT EXISTS backtest_metrics (
                    run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
                    metric_key TEXT NOT NULL, metric_value REAL, metric_text TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(run_id, metric_key)
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return beijing_now().strftime("%Y-%m-%d %H:%M:%S%z")

    def upsert_security(self, security: Security, source: str = "") -> int:
        with self.connect() as db:
            db.execute(
                """INSERT INTO securities(code,market,security_type,name,full_symbol,source)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(code,market,security_type) DO UPDATE SET
                   name=excluded.name, full_symbol=excluded.full_symbol,
                   source=CASE WHEN excluded.source='' THEN securities.source ELSE excluded.source END,
                   active=1, updated_at=CURRENT_TIMESTAMP""",
                (
                    security.code,
                    security.market,
                    security.security_type.value,
                    security.name,
                    security.display_code,
                    source,
                ),
            )
            row = db.execute(
                "SELECT id FROM securities WHERE code=? AND market=? AND security_type=?",
                (security.code, security.market, security.security_type.value),
            ).fetchone()
        return int(row[0])

    def upsert_universe(
        self, securities: Iterable[Security], source: str = "universe"
    ) -> int:
        count = 0
        with self.connect() as db:
            for security in securities:
                db.execute(
                    """INSERT INTO securities(code,market,security_type,name,full_symbol,source)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(code,market,security_type) DO UPDATE SET
                       name=excluded.name, full_symbol=excluded.full_symbol, active=1,
                       source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
                    (
                        security.code,
                        security.market,
                        security.security_type.value,
                        security.name,
                        security.display_code,
                        source,
                    ),
                )
                count += 1
        return count

    def security_id(self, security: Security) -> int | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM securities WHERE code=? AND market=? AND security_type=?",
                (security.code, security.market, security.security_type.value),
            ).fetchone()
        return int(row[0]) if row else None

    def list_securities(
        self, types: Iterable[SecurityType] | None = None
    ) -> list[Security]:
        values = [item.value for item in types or []]
        sql = "SELECT code,name,security_type,market FROM securities WHERE active=1"
        params: list[str] = []
        if values:
            sql += f" AND security_type IN ({','.join('?' for _ in values)})"
            params.extend(values)
        sql += " ORDER BY security_type, market, code"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [
            Security(
                r["code"], r["name"], SecurityType(r["security_type"]), r["market"]
            )
            for r in rows
        ]

    def upsert_bars(
        self,
        security: Security,
        frame: pd.DataFrame,
        adjustment: str = "qfq",
        source: str = "",
        temporary_last: bool = False,
    ) -> int:
        if adjustment not in ADJUSTMENTS:
            raise ValueError("复权方式必须是不复权、qfq 或 hfq")
        if frame is None or frame.empty:
            return 0
        security_id = self.upsert_security(security, source)
        normalized = frame.copy()
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
        now = self._now()
        rows = []
        for index, row in normalized.iterrows():
            flags: list[str] = []
            o, h, low, c = (
                self._number(row.get(k)) for k in ("open", "high", "low", "close")
            )
            if any(value is None for value in (o, h, low, c)):
                continue
            if low > min(o, c) or h < max(o, c) or low > h:
                flags.append("invalid_ohlc")
            is_last = index == normalized.index[-1]
            rows.append(
                (
                    security_id,
                    row["date"].strftime("%Y-%m-%d"),
                    adjustment,
                    o,
                    h,
                    low,
                    c,
                    self._number(row.get("volume")),
                    self._number(row.get("amount")),
                    self._number(row.get("turnover")),
                    self._number(row.get("pct_change")),
                    self._number(row.get("change")),
                    self._number(row.get("amplitude")),
                    source,
                    "",
                    now,
                    ",".join(flags),
                    int(temporary_last and is_last),
                )
            )
        with self.connect() as db:
            db.executemany(
                """INSERT INTO daily_bars
                (security_id,trade_date,adjustment,open,high,low,close,volume,amount,turnover,
                 pct_change,change,amplitude,source,source_timestamp,fetched_at,quality_flags,is_temporary)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(security_id,trade_date,adjustment) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                 volume=excluded.volume,amount=excluded.amount,turnover=excluded.turnover,
                 pct_change=excluded.pct_change,change=excluded.change,amplitude=excluded.amplitude,
                 source=excluded.source,fetched_at=excluded.fetched_at,
                 quality_flags=excluded.quality_flags,is_temporary=excluded.is_temporary""",
                rows,
            )
        return len(rows)

    @staticmethod
    def _number(value: object) -> float | None:
        parsed = pd.to_numeric(value, errors="coerce")
        return None if pd.isna(parsed) else float(parsed)

    def get_bars(
        self,
        security: Security,
        adjustment: str = "qfq",
        start: date | str | None = None,
        end: date | str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        security_id = self.security_id(security)
        if security_id is None:
            return pd.DataFrame()
        where = ["security_id=?", "adjustment=?"]
        params: list[object] = [security_id, adjustment]
        if start:
            where.append("trade_date>=?")
            params.append(str(start))
        if end:
            where.append("trade_date<=?")
            params.append(str(end))
        sql = (
            "SELECT trade_date AS date,open,close,high,low,volume,amount,amplitude,pct_change,change,turnover,source,is_temporary FROM daily_bars WHERE "
            + " AND ".join(where)
        )
        if limit:
            sql += " ORDER BY trade_date DESC LIMIT ?"
            params.append(int(limit))
        else:
            sql += " ORDER BY trade_date"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows])
        if limit and not frame.empty:
            frame = frame.iloc[::-1]
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame.reset_index(drop=True)

    def latest_date(
        self, security: Security, adjustment: str = "qfq", completed_only: bool = False
    ) -> str:
        security_id = self.security_id(security)
        if security_id is None:
            return ""
        extra = " AND is_temporary=0" if completed_only else ""
        with self.connect() as db:
            row = db.execute(
                f"SELECT MAX(trade_date) FROM daily_bars WHERE security_id=? AND adjustment=?{extra}",
                (security_id, adjustment),
            ).fetchone()
        return str(row[0] or "")

    def summary(self) -> WarehouseSummary:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*),MIN(trade_date),MAX(trade_date) FROM daily_bars"
            ).fetchone()
            securities = db.execute(
                "SELECT COUNT(*) FROM securities WHERE active=1"
            ).fetchone()[0]
            issues = db.execute(
                "SELECT COUNT(*) FROM data_quality_issues WHERE resolved=0"
            ).fetchone()[0]
        return WarehouseSummary(
            int(securities),
            int(row[0]),
            str(row[1] or ""),
            str(row[2] or ""),
            self.database_path.stat().st_size if self.database_path.exists() else 0,
            int(issues),
        )

    def create_sync_job(
        self, scope: str, adjustment: str, mode: str, total: int
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO sync_jobs(scope,adjustment,mode,status,total_count,started_at) VALUES(?,?,?,'running',?,?)",
                (scope, adjustment, mode, total, self._now()),
            )
            return int(cursor.lastrowid)

    def update_sync_job(
        self,
        job_id: int,
        *,
        completed: int | None = None,
        failed: int | None = None,
        status: str | None = None,
        error: str = "",
    ) -> None:
        fields, params = [], []
        for name, value in (
            ("completed_count", completed),
            ("failed_count", failed),
            ("status", status),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                params.append(value)
        if error:
            fields.append("error_summary=?")
            params.append(error[:4000])
        if status in {"completed", "completed_with_errors", "cancelled", "failed"}:
            fields.append("finished_at=?")
            params.append(self._now())
        if not fields:
            return
        params.append(job_id)
        with self.connect() as db:
            db.execute(f"UPDATE sync_jobs SET {','.join(fields)} WHERE id=?", params)

    def request_cancel(self, job_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE sync_jobs SET cancel_requested=1 WHERE id=?", (job_id,))

    def cancel_requested(self, job_id: int) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM sync_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def add_sync_failure(self, job_id: int, security: Security, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO sync_job_failures(job_id,security_key,error) VALUES(?,?,?)",
                (job_id, security.key, error[:2000]),
            )

    def list_sync_jobs(self, limit: int = 30) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_local_histories(
        self,
        types: Iterable[SecurityType] = (SecurityType.STOCK,),
        adjustment: str = "qfq",
        end: str = "",
        min_rows: int = 1,
    ) -> Iterator[tuple[Security, pd.DataFrame]]:
        for security in self.list_securities(types):
            frame = self.get_bars(security, adjustment, end=end or None)
            if len(frame) >= min_rows:
                yield security, frame

    def export_csv(
        self,
        output: Path,
        securities: Iterable[Security] | None = None,
        adjustment: str = "qfq",
        start: str = "",
        end: str = "",
    ) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        items = list(securities) if securities is not None else self.list_securities()
        count = 0
        with output.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "代码",
                    "名称",
                    "市场",
                    "类型",
                    "日期",
                    "复权",
                    "开",
                    "高",
                    "低",
                    "收",
                    "成交量",
                    "成交额",
                    "换手率",
                    "涨跌幅",
                    "来源",
                    "临时K线",
                ]
            )
            for security in items:
                frame = self.get_bars(security, adjustment, start or None, end or None)
                for _, row in frame.iterrows():
                    writer.writerow(
                        [
                            security.code,
                            security.name,
                            security.market,
                            security.security_type.value,
                            pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                            adjustment,
                            row.get("open"),
                            row.get("high"),
                            row.get("low"),
                            row.get("close"),
                            row.get("volume"),
                            row.get("amount"),
                            row.get("turnover"),
                            row.get("pct_change"),
                            row.get("source"),
                            row.get("is_temporary", 0),
                        ]
                    )
                    count += 1
        return count

    def import_cache_directory(self, cache_dir: Path) -> tuple[int, int]:
        files = 0
        bars = 0
        for path in Path(cache_dir).glob("history_*_*.csv"):
            parts = path.stem.split("_")
            if len(parts) < 4:
                continue
            try:
                security_type = SecurityType(parts[1])
            except ValueError:
                continue
            code, adjustment = parts[2], parts[3]
            if adjustment == "raw":
                adjustment = ""
            if adjustment not in ADJUSTMENTS:
                continue
            with self.connect() as db:
                row = db.execute(
                    "SELECT name,market FROM securities WHERE code=? AND security_type=? ORDER BY active DESC LIMIT 1",
                    (code, security_type.value),
                ).fetchone()
            security = Security(
                code,
                row["name"] if row else code,
                security_type,
                row["market"] if row else "",
            )
            try:
                frame = pd.read_csv(path, encoding="utf-8-sig")
                bars += self.upsert_bars(security, frame, adjustment, "旧版CSV缓存导入")
                files += 1
            except Exception:
                continue
        return files, bars

    def database_report(self) -> dict:
        return asdict(self.summary())
