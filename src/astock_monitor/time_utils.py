from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return an aware Beijing timestamp, independent of the OS timezone."""

    return datetime.now(timezone.utc).astimezone(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()


def latest_completed_market_day() -> date:
    """Return the latest weekday whose A-share close should already be complete."""

    now = beijing_now()
    candidate = now.date()
    if now.time() < time(15, 15):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def recent_completed_market_days(count: int = 5) -> list[date]:
    """Return recent completed weekday candidates; exchange holidays are skipped later."""

    candidate = latest_completed_market_day()
    values: list[date] = []
    while len(values) < max(0, count):
        if candidate.weekday() < 5:
            values.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(values))


def cache_age_seconds(path: Path) -> float:
    """Measure file age on the UTC timeline so local timezone never leaks in."""

    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - modified).total_seconds())
