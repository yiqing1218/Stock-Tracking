from __future__ import annotations



from .historical_store import HistoricalStore


def validate_warehouse(store: HistoricalStore) -> dict[str, int]:
    """Run conservative checks. Issues are facts about stored rows, not guesses."""
    counts = {"invalid_ohlc": 0, "negative_value": 0, "duplicate": 0, "large_gap": 0}
    with store.connect() as db:
        db.execute("DELETE FROM data_quality_issues WHERE resolved=0")
        rows = db.execute(
            """SELECT security_id,trade_date,adjustment,open,high,low,close,volume,amount
               FROM daily_bars ORDER BY security_id,adjustment,trade_date"""
        ).fetchall()
        previous: dict[tuple[int, str], float] = {}
        for row in rows:
            issues: list[tuple[str, str, str]] = []
            values = [row[k] for k in ("open", "high", "low", "close")]
            if (
                any(v is None for v in values)
                or row["low"] > min(row["open"], row["close"])
                or row["high"] < max(row["open"], row["close"])
                or row["low"] > row["high"]
            ):
                issues.append(("invalid_ohlc", "error", "OHLC关系不成立"))
            if any((row[k] or 0) < 0 for k in ("volume", "amount")):
                issues.append(("negative_value", "error", "成交量或成交额为负数"))
            key = (int(row["security_id"]), str(row["adjustment"]))
            prior = previous.get(key)
            close = row["close"]
            if prior and close and abs(close / prior - 1) > 0.35:
                issues.append(
                    (
                        "large_gap",
                        "warning",
                        f"相邻收盘价变化超过35%: {prior} -> {close}",
                    )
                )
            if close:
                previous[key] = float(close)
            for issue_type, severity, details in issues:
                counts[issue_type] += 1
                db.execute(
                    """INSERT OR IGNORE INTO data_quality_issues
                    (security_id,trade_date,adjustment,issue_type,severity,details)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        row["security_id"],
                        row["trade_date"],
                        row["adjustment"],
                        issue_type,
                        severity,
                        details,
                    ),
                )
    return counts
