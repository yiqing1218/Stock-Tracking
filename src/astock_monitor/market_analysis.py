from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import akshare as ak

from .data_provider import MarketDashboardBundle
from .data_provider import infer_market
from .historical_store import HistoricalStore
from .models import Security, SecurityType
from .time_utils import beijing_now, beijing_today


@dataclass(frozen=True, slots=True)
class HeatWeights:
    change: float = 0.40
    turnover: float = 0.20
    breadth: float = 0.20
    amount: float = 0.10
    fund_flow: float = 0.10


class MarketAnalysisService:
    """Persist and query stage-five market/board observations.

    The service never backfills board history from invented values. History starts on
    the first observation day and every row keeps its classification source.
    """

    def __init__(
        self, store: HistoricalStore, heat_weights: HeatWeights = HeatWeights()
    ) -> None:
        self.store = store
        self.heat_weights = heat_weights

    @staticmethod
    def _number(row: pd.Series, *names: str) -> float | None:
        for name in names:
            value = pd.to_numeric(row.get(name), errors="coerce")
            if pd.notna(value):
                return float(value)
        return None

    @staticmethod
    def market_score(breadth: dict[str, float | int]) -> float:
        up = float(breadth.get("up", 0))
        down = float(breadth.get("down", 0))
        flat = float(breadth.get("flat", 0))
        total = max(up + down + flat, 1)
        median = float(breadth.get("median_change", 0))
        limit_delta = float(breadth.get("limit_up", 0)) - float(
            breadth.get("limit_down", 0)
        )
        # Transparent 0-100 composite: width 60%, median 25%, limit ecology 15%.
        score = (
            50
            + ((up - down) / total) * 30
            + np.clip(median, -5, 5) / 5 * 12.5
            + np.clip(limit_delta / max(total * 0.02, 1), -1, 1) * 7.5
        )
        return float(np.clip(score, 0, 100))

    def _board_heat(self, frame: pd.DataFrame) -> pd.Series:
        def percentile(*names: str) -> pd.Series:
            name = next((name for name in names if name in frame), "")
            values = (
                pd.to_numeric(frame[name], errors="coerce")
                if name
                else pd.Series(np.nan, index=frame.index)
            )
            return values.rank(pct=True).fillna(0.5) * 100

        up = percentile("上涨家数")
        down = percentile("下跌家数")
        breadth = (50 + (up - down) / 2).clip(0, 100)
        weights = self.heat_weights
        return (
            percentile("涨跌幅") * weights.change
            + percentile("换手率") * weights.turnover
            + breadth * weights.breadth
            + percentile("成交额", "总成交额") * weights.amount
            + percentile("主力净流入", "资金净流入") * weights.fund_flow
        ).clip(0, 100)

    def persist_dashboard(self, bundle: MarketDashboardBundle) -> None:
        trade_date = bundle.trade_date or beijing_today()
        breadth = dict(bundle.breadth)
        breadth["market_score"] = self.market_score(breadth)
        breadth["source"] = bundle.sources.get("breadth", "")
        self.store.save_market_breadth(trade_date, breadth)

    def persist_boards(
        self, frame: pd.DataFrame, trade_date: str, timestamp: str, source: str
    ) -> None:
        if frame is None or frame.empty or "板块名称" not in frame:
            return
        data = frame.copy()
        data["_heat"] = self._board_heat(data)
        data["_change"] = pd.to_numeric(data.get("涨跌幅"), errors="coerce")
        ranks = data["_change"].rank(ascending=False, method="min")
        with self.store.connect() as db:
            for index, row in data.iterrows():
                board_name = str(row.get("板块名称", "")).strip()
                if not board_name:
                    continue
                board_type = str(row.get("类型", "板块")).strip() or "板块"
                board_source = str(row.get("分类来源", source or "公开板块接口"))
                db.execute(
                    """INSERT INTO board_definitions
                    (board_code,board_name,board_type,classification_source,source)
                    VALUES(?,?,?,?,?) ON CONFLICT(board_name,board_type,classification_source)
                    DO UPDATE SET board_code=excluded.board_code,source=excluded.source,
                    updated_at=CURRENT_TIMESTAMP""",
                    (
                        str(row.get("板块代码", "")),
                        board_name,
                        board_type,
                        board_source,
                        source,
                    ),
                )
                board_id = int(
                    db.execute(
                        "SELECT id FROM board_definitions WHERE board_name=? AND board_type=? AND classification_source=?",
                        (board_name, board_type, board_source),
                    ).fetchone()[0]
                )
                change = self._number(row, "涨跌幅")
                amount = self._number(row, "成交额", "总成交额")
                fund_flow = self._number(row, "主力净流入", "资金净流入")
                heat = float(row["_heat"])
                db.execute(
                    """INSERT OR REPLACE INTO board_snapshots
                    (board_id,snapshot_time,trade_date,change_pct,amount,up_count,down_count,
                    limit_up_count,leader_code,laggard_code,fund_flow,pe,pb,dividend_yield,
                    chengjian_heat,strong_streak,source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        board_id,
                        timestamp,
                        trade_date,
                        change,
                        amount,
                        self._number(row, "上涨家数"),
                        self._number(row, "下跌家数"),
                        self._number(row, "涨停家数"),
                        str(row.get("领涨股票代码", "")),
                        str(row.get("领跌股票代码", "")),
                        fund_flow,
                        self._number(row, "市盈率"),
                        self._number(row, "市净率"),
                        self._number(row, "股息率"),
                        heat,
                        0,
                        source,
                    ),
                )
                db.execute(
                    """INSERT INTO board_history
                    (board_id,trade_date,close,change_pct,amount,volume,heat_rank,change_rank,fund_flow,source)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(board_id,trade_date) DO UPDATE SET
                    close=excluded.close,change_pct=excluded.change_pct,amount=excluded.amount,
                    heat_rank=excluded.heat_rank,change_rank=excluded.change_rank,
                    fund_flow=excluded.fund_flow,source=excluded.source""",
                    (
                        board_id,
                        trade_date,
                        self._number(row, "最新价", "板块指数"),
                        change,
                        amount,
                        self._number(row, "成交量"),
                        int(
                            data["_heat"].rank(ascending=False, method="min").loc[index]
                        ),
                        int(ranks.loc[index]) if pd.notna(ranks.loc[index]) else None,
                        fund_flow,
                        source,
                    ),
                )

    def list_boards(self, board_type: str = "") -> list[dict]:
        where = "WHERE d.board_type=?" if board_type else ""
        parameters = (board_type,) if board_type else ()
        with self.store.connect() as db:
            rows = db.execute(
                f"""SELECT d.id,d.board_name,d.board_type,d.classification_source,
                s.trade_date,s.change_pct,s.amount,s.up_count,s.down_count,s.fund_flow,
                s.chengjian_heat,s.strong_streak,s.source,
                (SELECT MIN(trade_date) FROM board_history h WHERE h.board_id=d.id) first_date
                FROM board_definitions d JOIN board_snapshots s ON s.board_id=d.id
                JOIN (SELECT board_id,MAX(snapshot_time) latest FROM board_snapshots GROUP BY board_id) x
                ON x.board_id=s.board_id AND x.latest=s.snapshot_time {where}
                ORDER BY s.change_pct DESC,d.board_name""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def board_history(self, board_id: int, limit: int = 260) -> pd.DataFrame:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT trade_date AS 日期,change_pct AS 涨跌幅,amount AS 成交额,
                heat_rank AS 热度排名,change_rank AS 涨幅排名,fund_flow AS 资金净流入,source AS 来源
                FROM board_history WHERE board_id=? ORDER BY trade_date DESC LIMIT ?""",
                (board_id, max(1, min(limit, 2000))),
            ).fetchall()
        return pd.DataFrame([dict(row) for row in rows])

    def sync_board_members(self, board_id: int) -> int:
        """Refresh one board's current members without rewriting past membership."""

        with self.store.connect() as db:
            board = db.execute(
                "SELECT * FROM board_definitions WHERE id=?", (board_id,)
            ).fetchone()
        if board is None:
            raise ValueError("板块不存在")
        if board["board_type"] == "行业":
            frame = ak.stock_board_industry_cons_em(symbol=board["board_name"])
        elif board["board_type"] == "概念":
            frame = ak.stock_board_concept_cons_em(symbol=board["board_name"])
        else:
            raise ValueError("该分类来源暂不支持成分同步")
        if frame is None or frame.empty:
            return 0
        code_column = next(
            (name for name in ("代码", "股票代码", "证券代码") if name in frame), None
        )
        name_column = next(
            (name for name in ("名称", "股票名称", "证券简称") if name in frame), None
        )
        if code_column is None or name_column is None:
            raise ValueError("板块成分接口缺少代码或名称字段")
        observed = beijing_today().isoformat()
        current_ids: set[int] = set()
        source = str(board["classification_source"])
        with self.store.connect() as db:
            for _, row in frame.iterrows():
                digits = "".join(
                    character
                    for character in str(row[code_column])
                    if character.isdigit()
                )[-6:].zfill(6)
                if not digits:
                    continue
                security = Security(
                    digits,
                    str(row[name_column]).strip(),
                    SecurityType.STOCK,
                    infer_market(digits, SecurityType.STOCK),
                )
                db.execute(
                    """INSERT INTO securities(code,market,security_type,name,full_symbol,source)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(code,market,security_type) DO UPDATE SET
                    name=excluded.name,full_symbol=excluded.full_symbol,active=1,
                    source=excluded.source,updated_at=CURRENT_TIMESTAMP""",
                    (
                        security.code,
                        security.market,
                        security.security_type.value,
                        security.name,
                        security.display_code,
                        source,
                    ),
                )
                security_id = int(
                    db.execute(
                        "SELECT id FROM securities WHERE code=? AND market=? AND security_type=?",
                        (security.code, security.market, security.security_type.value),
                    ).fetchone()[0]
                )
                current_ids.add(security_id)
                existing = db.execute(
                    "SELECT 1 FROM board_members WHERE board_id=? AND security_id=? AND effective_to=''",
                    (board_id, security_id),
                ).fetchone()
                if existing is None:
                    db.execute(
                        "INSERT INTO board_members(board_id,security_id,effective_from,source) VALUES(?,?,?,?)",
                        (board_id, security_id, observed, source),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO security_board_map(security_id,board_id,source,effective_from) VALUES(?,?,?,?)",
                        (security_id, board_id, source, observed),
                    )
            active = db.execute(
                "SELECT security_id FROM board_members WHERE board_id=? AND effective_to=''",
                (board_id,),
            ).fetchall()
            for row in active:
                security_id = int(row["security_id"])
                if security_id not in current_ids:
                    db.execute(
                        "UPDATE board_members SET effective_to=?,updated_at=CURRENT_TIMESTAMP WHERE board_id=? AND security_id=? AND effective_to=''",
                        (observed, board_id, security_id),
                    )
                    db.execute(
                        "UPDATE security_board_map SET effective_to=? WHERE board_id=? AND security_id=? AND effective_to=''",
                        (observed, board_id, security_id),
                    )
        return len(current_ids)

    def board_members(self, board_id: int) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT s.code,s.name,s.market,m.weight,m.effective_from,m.source
                FROM board_members m JOIN securities s ON s.id=m.security_id
                WHERE m.board_id=? AND m.effective_to='' ORDER BY s.code""",
                (board_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def relative_strength(
        self, security: Security, benchmark: Security, adjustment: str = "qfq"
    ) -> dict[str, float | None]:
        left = self.store.get_bars(security, adjustment=adjustment)
        right = self.store.get_bars(benchmark, adjustment=adjustment)
        if left.empty or right.empty:
            return {"5日": None, "20日": None, "60日": None}
        merged = left[["date", "close"]].merge(
            right[["date", "close"]], on="date", suffixes=("_stock", "_benchmark")
        )
        result: dict[str, float | None] = {}
        for window, label in ((5, "5日"), (20, "20日"), (60, "60日")):
            if len(merged) <= window:
                result[label] = None
                continue
            stock_return = (
                merged["close_stock"].iloc[-1] / merged["close_stock"].iloc[-window - 1]
                - 1
            )
            benchmark_return = (
                merged["close_benchmark"].iloc[-1]
                / merged["close_benchmark"].iloc[-window - 1]
                - 1
            )
            result[label] = float((stock_return - benchmark_return) * 100)
        return result

    def relative_strength_to_board(
        self, security: Security, board_id: int, adjustment: str = "qfq"
    ) -> dict[str, float | None]:
        stock = self.store.get_bars(security, adjustment=adjustment)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT trade_date AS date,close FROM board_history WHERE board_id=? AND close IS NOT NULL ORDER BY trade_date",
                (board_id,),
            ).fetchall()
        board = pd.DataFrame([dict(row) for row in rows])
        if stock.empty or board.empty:
            return {"5日": None, "20日": None, "60日": None}
        board["date"] = pd.to_datetime(board["date"])
        merged = stock[["date", "close"]].merge(
            board[["date", "close"]], on="date", suffixes=("_stock", "_board")
        )
        result: dict[str, float | None] = {}
        for window, label in ((5, "5日"), (20, "20日"), (60, "60日")):
            if len(merged) <= window:
                result[label] = None
                continue
            stock_return = (
                merged["close_stock"].iloc[-1] / merged["close_stock"].iloc[-window - 1]
                - 1
            )
            board_return = (
                merged["close_board"].iloc[-1] / merged["close_board"].iloc[-window - 1]
                - 1
            )
            result[label] = float((stock_return - board_return) * 100)
        return result
