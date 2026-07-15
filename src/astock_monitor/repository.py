from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import CustomIndicator, Security, SecurityType, WatchlistGroup


DEFAULT_WATCHLIST = [
    Security("000001", "上证指数", SecurityType.INDEX, "sh"),
    Security("000300", "沪深300", SecurityType.INDEX, "csi"),
    Security("510300", "沪深300ETF", SecurityType.ETF, "sh"),
    Security("000001", "平安银行", SecurityType.STOCK, "sz"),
    Security("600519", "贵州茅台", SecurityType.STOCK, "sh"),
]

DEFAULT_FORMULAS = [
    CustomIndicator(None, "MACD动量差", "EMA(close, 12) - EMA(close, 26)", "#38BDF8"),
    CustomIndicator(
        None, "20日价格偏离率", "(close / SMA(close, 20) - 1) * 100", "#F59E0B"
    ),
    CustomIndicator(
        None, "量价共振", "ZSCORE(returns, 20) + ZSCORE(volume, 20)", "#A78BFA"
    ),
]


class Repository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    security_type TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (security_type, code)
                );

                CREATE TABLE IF NOT EXISTS custom_indicators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    formula TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#38BDF8',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS indicator_favorites (
                    indicator_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_type TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    external_key TEXT NOT NULL UNIQUE,
                    published_at TEXT NOT NULL DEFAULT '',
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS market_alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_type TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    threshold REAL,
                    secondary_threshold REAL,
                    window_minutes INTEGER NOT NULL DEFAULT 5,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_state TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(security_type, code, rule_type)
                );

                CREATE INDEX IF NOT EXISTS idx_notifications_unread
                ON notifications(is_read, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled
                ON market_alert_rules(enabled, security_type, code);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO watchlist_groups (name, sort_order) VALUES ('默认分组', 0)"
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(watchlist)").fetchall()
            }
            if "group_id" not in columns:
                connection.execute("ALTER TABLE watchlist ADD COLUMN group_id INTEGER")
            custom_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(custom_indicators)"
                ).fetchall()
            }
            if "category" not in custom_columns:
                connection.execute(
                    "ALTER TABLE custom_indicators ADD COLUMN category TEXT NOT NULL DEFAULT '趋势'"
                )
            if "in_library" not in custom_columns:
                connection.execute(
                    "ALTER TABLE custom_indicators ADD COLUMN in_library INTEGER NOT NULL DEFAULT 0"
                )
            default_group_id = self._default_group_id(connection)
            connection.execute(
                "UPDATE watchlist SET group_id = ? WHERE group_id IS NULL",
                (default_group_id,),
            )
            count = connection.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            if count == 0:
                for index, security in enumerate(DEFAULT_WATCHLIST):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO watchlist
                        (security_type, code, name, market, sort_order)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            security.security_type.value,
                            security.code,
                            security.name,
                            security.market,
                            index,
                        ),
                    )
            formula_count = connection.execute(
                "SELECT COUNT(*) FROM custom_indicators"
            ).fetchone()[0]
            if formula_count == 0:
                for item in DEFAULT_FORMULAS:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO custom_indicators
                        (name, formula, color, category, in_library) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            item.name,
                            item.formula,
                            item.color,
                            item.category,
                            int(item.in_library),
                        ),
                    )

    @staticmethod
    def _default_group_id(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT id FROM watchlist_groups WHERE name = '默认分组'"
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO watchlist_groups (name, sort_order) VALUES ('默认分组', 0)"
            )
            return int(cursor.lastrowid)
        return int(row["id"])

    def list_groups(self) -> list[WatchlistGroup]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, sort_order FROM watchlist_groups ORDER BY sort_order, id"
            ).fetchall()
        return [
            WatchlistGroup(int(row["id"]), row["name"], int(row["sort_order"]))
            for row in rows
        ]

    def create_group(self, name: str) -> WatchlistGroup:
        name = name.strip()
        if not name:
            raise ValueError("分组名称不能为空")
        with self._connect() as connection:
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist_groups"
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO watchlist_groups (name, sort_order) VALUES (?, ?)",
                (name, next_order),
            )
        return WatchlistGroup(int(cursor.lastrowid), name, int(next_order))

    def rename_group(self, group_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("分组名称不能为空")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT name FROM watchlist_groups WHERE id = ?", (group_id,)
            ).fetchone()
            if current is None:
                raise ValueError("分组不存在")
            if current["name"] == "默认分组":
                raise ValueError("默认分组不能重命名")
            connection.execute(
                "UPDATE watchlist_groups SET name = ? WHERE id = ?", (name, group_id)
            )

    def delete_group(self, group_id: int) -> None:
        with self._connect() as connection:
            default_id = self._default_group_id(connection)
            if group_id == default_id:
                raise ValueError("默认分组不能删除")
            connection.execute(
                "UPDATE watchlist SET group_id = ? WHERE group_id = ?",
                (default_id, group_id),
            )
            connection.execute("DELETE FROM watchlist_groups WHERE id = ?", (group_id,))

    def list_watchlist(self, group_id: int | None = None) -> list[Security]:
        with self._connect() as connection:
            where = "WHERE w.group_id = ?" if group_id is not None else ""
            parameters: tuple[int, ...] = (group_id,) if group_id is not None else ()
            rows = connection.execute(
                f"""
                SELECT w.code, w.name, w.security_type, w.market
                FROM watchlist AS w
                LEFT JOIN watchlist_groups AS g ON g.id = w.group_id
                {where}
                ORDER BY g.sort_order, g.id, w.sort_order, w.added_at
                """,
                parameters,
            ).fetchall()
        return [
            Security(
                code=row["code"],
                name=row["name"],
                security_type=SecurityType(row["security_type"]),
                market=row["market"],
            )
            for row in rows
        ]

    def group_for_security(self, security: Security) -> WatchlistGroup | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT g.id, g.name, g.sort_order
                FROM watchlist AS w
                JOIN watchlist_groups AS g ON g.id = w.group_id
                WHERE w.security_type = ? AND w.code = ?
                """,
                (security.security_type.value, security.code),
            ).fetchone()
        if row is None:
            return None
        return WatchlistGroup(int(row["id"]), row["name"], int(row["sort_order"]))

    def add_security(self, security: Security, group_id: int | None = None) -> None:
        with self._connect() as connection:
            if group_id is None:
                group_id = self._default_group_id(connection)
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist WHERE group_id = ?",
                (group_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO watchlist (security_type, code, name, market, group_id, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_type, code) DO UPDATE SET
                    name = excluded.name,
                    market = excluded.market,
                    group_id = excluded.group_id
                """,
                (
                    security.security_type.value,
                    security.code,
                    security.name,
                    security.market,
                    group_id,
                    next_order,
                ),
            )

    def move_security_to_group(self, security: Security, group_id: int) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM watchlist_groups WHERE id = ?", (group_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("目标分组不存在")
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist WHERE group_id = ?",
                (group_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE watchlist SET group_id = ?, sort_order = ?
                WHERE security_type = ? AND code = ?
                """,
                (group_id, next_order, security.security_type.value, security.code),
            )

    def move_security(self, security: Security, direction: int) -> None:
        if direction not in {-1, 1}:
            raise ValueError("direction 必须为 -1 或 1")
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT group_id, sort_order FROM watchlist
                WHERE security_type = ? AND code = ?
                """,
                (security.security_type.value, security.code),
            ).fetchone()
            if current is None:
                return
            operator = "<" if direction < 0 else ">"
            order = "DESC" if direction < 0 else "ASC"
            neighbor = connection.execute(
                f"""
                SELECT security_type, code, sort_order FROM watchlist
                WHERE group_id = ? AND sort_order {operator} ?
                ORDER BY sort_order {order}, added_at {order} LIMIT 1
                """,
                (current["group_id"], current["sort_order"]),
            ).fetchone()
            if neighbor is None:
                return
            marker = -2_147_483_648
            connection.execute(
                "UPDATE watchlist SET sort_order = ? WHERE security_type = ? AND code = ?",
                (marker, security.security_type.value, security.code),
            )
            connection.execute(
                "UPDATE watchlist SET sort_order = ? WHERE security_type = ? AND code = ?",
                (current["sort_order"], neighbor["security_type"], neighbor["code"]),
            )
            connection.execute(
                "UPDATE watchlist SET sort_order = ? WHERE security_type = ? AND code = ?",
                (neighbor["sort_order"], security.security_type.value, security.code),
            )

    def remove_security(self, security: Security) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM watchlist WHERE security_type = ? AND code = ?",
                (security.security_type.value, security.code),
            )

    def contains_security(self, security: Security) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM watchlist WHERE security_type = ? AND code = ?",
                (security.security_type.value, security.code),
            ).fetchone()
        return row is not None

    def list_custom_indicators(self) -> list[CustomIndicator]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, formula, color, category, in_library
                FROM custom_indicators ORDER BY id
                """
            ).fetchall()
        return [
            CustomIndicator(
                row["id"],
                row["name"],
                row["formula"],
                row["color"],
                row["category"],
                bool(row["in_library"]),
            )
            for row in rows
        ]

    def save_custom_indicator(self, indicator: CustomIndicator) -> CustomIndicator:
        name = indicator.name.strip()
        formula = indicator.formula.strip()
        if not name or not formula:
            raise ValueError("指标名称和公式不能为空")
        if indicator.category not in {"趋势", "动量", "波动", "量能", "情绪", "风险"}:
            raise ValueError("自定义指标必须归入六个指标维度之一")
        with self._connect() as connection:
            if indicator.id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO custom_indicators
                    (name, formula, color, category, in_library) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        formula,
                        indicator.color,
                        indicator.category,
                        int(indicator.in_library),
                    ),
                )
                indicator.id = int(cursor.lastrowid)
            else:
                connection.execute(
                    """
                    UPDATE custom_indicators
                    SET name = ?, formula = ?, color = ?, category = ?, in_library = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        name,
                        formula,
                        indicator.color,
                        indicator.category,
                        int(indicator.in_library),
                        indicator.id,
                    ),
                )
        return indicator

    def delete_custom_indicator(self, indicator_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM custom_indicators WHERE id = ?", (indicator_id,)
            )
            connection.execute(
                "DELETE FROM indicator_favorites WHERE indicator_key = ?",
                (f"custom:{indicator_id}",),
            )

    def list_indicator_favorites(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT indicator_key FROM indicator_favorites"
            ).fetchall()
        return {str(row["indicator_key"]) for row in rows}

    def set_indicator_favorite(self, indicator_key: str, favorite: bool) -> None:
        key = indicator_key.strip()
        if not key:
            raise ValueError("指标标识不能为空")
        with self._connect() as connection:
            if favorite:
                connection.execute(
                    "INSERT OR IGNORE INTO indicator_favorites (indicator_key) VALUES (?)",
                    (key,),
                )
            else:
                connection.execute(
                    "DELETE FROM indicator_favorites WHERE indicator_key = ?", (key,)
                )

    def add_notification(
        self,
        security: Security,
        event_type: str,
        title: str,
        *,
        content: str = "",
        source_url: str = "",
        external_key: str,
        published_at: str = "",
    ) -> bool:
        """Insert one compact event and return whether it was new."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notifications
                (security_type, code, name, event_type, title, content, source_url,
                 external_key, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    security.security_type.value,
                    security.code,
                    security.name,
                    event_type,
                    title.strip(),
                    content.strip(),
                    source_url.strip(),
                    external_key.strip(),
                    published_at.strip(),
                ),
            )
        return cursor.rowcount > 0

    def list_notifications(
        self, unread_only: bool = False, limit: int = 500
    ) -> list[dict]:
        where = "WHERE is_read = 0" if unread_only else ""
        safe_limit = max(1, min(int(limit), 2000))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notifications {where}
                ORDER BY CASE WHEN published_at = '' THEN created_at ELSE published_at END DESC,
                         id DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unread_notification_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM notifications WHERE is_read = 0"
                ).fetchone()[0]
            )

    def mark_notification_read(self, notification_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,)
            )

    def mark_all_notifications_read(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")

    def list_market_alert_rules(
        self,
        security: Security | None = None,
        enabled_only: bool = False,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if security is not None:
            clauses.extend(["security_type = ?", "code = ?"])
            parameters.extend([security.security_type.value, security.code])
        if enabled_only:
            clauses.append("enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM market_alert_rules {where} ORDER BY name, rule_type",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_market_alert_rule(
        self,
        security: Security,
        rule_type: str,
        threshold: float | None = None,
        secondary_threshold: float | None = None,
        window_minutes: int = 5,
        enabled: bool = True,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_alert_rules
                (security_type, code, name, rule_type, threshold, secondary_threshold,
                 window_minutes, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_type, code, rule_type) DO UPDATE SET
                    name = excluded.name,
                    threshold = excluded.threshold,
                    secondary_threshold = excluded.secondary_threshold,
                    window_minutes = excluded.window_minutes,
                    enabled = excluded.enabled,
                    last_state = '',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    security.security_type.value,
                    security.code,
                    security.name,
                    rule_type,
                    threshold,
                    secondary_threshold,
                    max(1, min(int(window_minutes), 120)),
                    int(enabled),
                ),
            )

    def delete_market_alert_rule(self, rule_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM market_alert_rules WHERE id = ?", (rule_id,)
            )

    def update_market_alert_state(self, rule_id: int, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE market_alert_rules SET last_state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state[:64], rule_id),
            )
