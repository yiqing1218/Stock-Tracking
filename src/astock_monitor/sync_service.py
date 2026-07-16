from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

from .data_provider import DataProvider
from .historical_store import HistoricalStore
from .models import Security, SecurityType
from .time_utils import beijing_today


@dataclass(slots=True)
class SyncProgress:
    job_id: int
    completed: int
    failed: int
    total: int
    current: str = ""


class SyncService:
    def __init__(
        self, store: HistoricalStore, provider: DataProvider, workers: int = 4
    ) -> None:
        self.store = store
        self.provider = provider
        self.workers = max(1, min(workers, 6))
        self._cancel = threading.Event()

    def resolve_scope(
        self, scope: str, securities: Iterable[Security] | None = None
    ) -> list[Security]:
        if securities is not None:
            return list(securities)
        universe = list(self.provider.load_universe())
        mapping = {
            "stocks": {SecurityType.STOCK},
            "etfs": {SecurityType.ETF},
            "indices": {SecurityType.INDEX},
            "all": set(SecurityType),
        }
        allowed = mapping.get(scope, set(SecurityType))
        return [item for item in universe if item.security_type in allowed]

    def cancel(self, job_id: int | None = None) -> None:
        self._cancel.set()
        if job_id:
            self.store.request_cancel(job_id)

    def sync(
        self,
        scope: str = "all",
        securities: Iterable[Security] | None = None,
        adjustment: str = "qfq",
        mode: str = "incremental",
        calendar_days: int = 16000,
        progress: Callable[[SyncProgress], None] | None = None,
    ) -> SyncProgress:
        items = self.resolve_scope(scope, securities)
        self.store.upsert_universe(items, "证券目录")
        job_id = self.store.create_sync_job(scope, adjustment, mode, len(items))
        self._cancel.clear()
        completed = failed = 0
        errors: list[str] = []

        def download(security: Security) -> tuple[Security, pd.DataFrame, str]:
            last = self.store.latest_date(security, adjustment)
            days = calendar_days
            if mode == "incremental" and last:
                days = max(30, (beijing_today() - pd.Timestamp(last).date()).days + 15)
            error: Exception | None = None
            for attempt in range(3):
                try:
                    frame = self.provider.get_history(
                        security,
                        calendar_days=days,
                        use_cache=attempt == 0,
                        adjustment=adjustment,
                        include_live=False,
                        persist=True,
                    )
                    if last and mode == "incremental":
                        frame = frame[
                            pd.to_datetime(frame["date"])
                            >= pd.Timestamp(last) - pd.Timedelta(days=3)
                        ]
                    return (
                        security,
                        frame,
                        self.provider.history_sources.get(security.key, "公开行情接口"),
                    )
                except Exception as exc:
                    error = exc
                    if attempt < 2:
                        time.sleep(min(4.0, (2**attempt) + random.random()))
            raise RuntimeError(str(error or "未知同步错误"))

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                future_map = {pool.submit(download, item): item for item in items}
                for future in as_completed(future_map):
                    security = future_map[future]
                    if self._cancel.is_set() or self.store.cancel_requested(job_id):
                        for pending in future_map:
                            pending.cancel()
                        self.store.update_sync_job(
                            job_id,
                            completed=completed,
                            failed=failed,
                            status="cancelled",
                        )
                        return SyncProgress(
                            job_id, completed, failed, len(items), security.name
                        )
                    try:
                        item, frame, source = future.result()
                        self.store.upsert_bars(
                            item,
                            frame,
                            adjustment,
                            source,
                            temporary_last=False,
                        )
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{security.display_code}: {exc}")
                        self.store.add_sync_failure(job_id, security, str(exc))
                    completed += 1
                    self.store.update_sync_job(
                        job_id, completed=completed, failed=failed
                    )
                    if progress:
                        progress(
                            SyncProgress(
                                job_id, completed, failed, len(items), security.name
                            )
                        )
            status = "completed_with_errors" if failed else "completed"
            self.store.update_sync_job(
                job_id,
                completed=completed,
                failed=failed,
                status=status,
                error="\n".join(errors[-20:]),
            )
        except Exception as exc:
            self.store.update_sync_job(
                job_id,
                completed=completed,
                failed=failed,
                status="failed",
                error=str(exc),
            )
            raise
        return SyncProgress(job_id, completed, failed, len(items))
