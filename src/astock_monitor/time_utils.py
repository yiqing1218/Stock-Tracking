from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return an aware Beijing timestamp, independent of the OS timezone."""

    return datetime.now(timezone.utc).astimezone(BEIJING_TZ)


def beijing_today() -> date:
    return beijing_now().date()


def cache_age_seconds(path: Path) -> float:
    """Measure file age on the UTC timeline so local timezone never leaks in."""

    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - modified).total_seconds())
