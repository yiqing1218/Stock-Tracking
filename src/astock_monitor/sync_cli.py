from __future__ import annotations

import argparse
from pathlib import Path

from .data_provider import DataProvider
from .data_quality import validate_warehouse
from .historical_store import HistoricalStore
from .sync_service import SyncService


def main() -> None:
    parser = argparse.ArgumentParser(description="澄鉴 A股本地历史仓库")
    parser.add_argument(
        "command",
        choices=("daily", "full", "repair", "status", "validate", "import-cache"),
    )
    parser.add_argument(
        "--scope", choices=("stocks", "etfs", "indices", "all"), default="all"
    )
    parser.add_argument("--adjustment", choices=("qfq", "hfq", "raw"), default="qfq")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    store = HistoricalStore(args.data_dir / "astock_monitor.db")
    if args.command == "status":
        print(store.database_report())
        return
    if args.command == "validate":
        print(validate_warehouse(store))
        return
    provider = DataProvider(args.data_dir / "cache")
    if args.command == "import-cache":
        print(store.import_cache_directory(provider.cache_dir))
        return
    mode = "incremental" if args.command == "daily" else args.command
    adjustment = "" if args.adjustment == "raw" else args.adjustment
    result = SyncService(store, provider).sync(
        args.scope,
        adjustment=adjustment,
        mode=mode,
        progress=lambda p: print(
            f"\r{p.completed}/{p.total} 失败{p.failed} {p.current}", end="", flush=True
        ),
    )
    print(f"\n完成: {result}")


if __name__ == "__main__":
    main()
