from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Protocol

import akshare as ak
import pandas as pd

from .historical_store import HistoricalStore
from .models import Security
from .repository import Repository
from .time_utils import beijing_now, beijing_today


EVENT_TYPES: dict[str, str] = {
    "periodic_report": "定期报告",
    "earnings_forecast": "业绩预告",
    "earnings_flash": "业绩快报",
    "dividend": "分红送转",
    "shareholder_meeting": "股东大会",
    "shareholder_change": "股东增减持",
    "executive_change": "高管增减持",
    "share_repurchase": "回购",
    "equity_incentive": "股权激励",
    "employee_stock_plan": "员工持股",
    "share_pledge": "股权质押",
    "restricted_release": "限售股解禁",
    "merger_restructuring": "并购重组",
    "external_investment": "对外投资",
    "major_contract": "重大合同",
    "regulatory_inquiry": "监管问询",
    "administrative_penalty": "行政处罚",
    "litigation": "诉讼仲裁",
    "suspension_resume": "停复牌",
    "abnormal_trading": "异常波动",
    "dragon_tiger": "龙虎榜",
    "block_trade": "大宗交易",
    "margin_trading": "融资融券",
    "institutional_research": "机构调研",
    "disclosure_schedule": "财报预约",
    "shareholder_count": "股东户数",
    "other": "其他公告",
}


CLASSIFICATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("earnings_forecast", ("业绩预告", "预盈", "预亏", "扭亏")),
    ("earnings_flash", ("业绩快报",)),
    (
        "periodic_report",
        ("年度报告", "半年度报告", "季度报告", "年报摘要", "半年报摘要"),
    ),
    ("dividend", ("利润分配", "分红派息", "权益分派", "除权除息", "送转股")),
    ("shareholder_meeting", ("股东大会",)),
    ("share_repurchase", ("回购方案", "回购股份", "股份回购", "回购进展", "回购完成")),
    ("equity_incentive", ("股权激励", "限制性股票激励", "股票期权激励")),
    ("employee_stock_plan", ("员工持股计划",)),
    ("share_pledge", ("股份质押", "股权质押", "解除质押")),
    ("restricted_release", ("解除限售", "限售股解禁", "上市流通")),
    ("merger_restructuring", ("重大资产重组", "吸收合并", "发行股份购买资产")),
    ("external_investment", ("对外投资", "设立子公司")),
    ("major_contract", ("重大合同", "中标通知", "签订合同")),
    ("regulatory_inquiry", ("问询函", "关注函", "监管工作函", "审核问询")),
    ("administrative_penalty", ("行政处罚", "纪律处分", "监管措施", "警示函")),
    ("litigation", ("重大诉讼", "诉讼仲裁", "仲裁事项")),
    ("suspension_resume", ("停牌", "复牌")),
    ("abnormal_trading", ("股票交易异常波动", "严重异常波动")),
    ("dragon_tiger", ("龙虎榜",)),
    ("block_trade", ("大宗交易",)),
    ("margin_trading", ("融资融券", "融资余额")),
    ("institutional_research", ("投资者关系活动", "机构调研", "调研活动")),
    ("disclosure_schedule", ("预约披露", "披露时间变更")),
    ("shareholder_count", ("股东户数", "股东人数")),
    ("executive_change", ("董事减持", "监事减持", "高管减持", "董监高增持")),
    ("shareholder_change", ("股东减持", "股东增持", "权益变动报告书", "持股变动")),
)


@dataclass(slots=True)
class NormalizedCompanyEvent:
    security: Security
    event_type: str
    title: str
    announcement_date: str
    source_name: str
    event_subtype: str = ""
    summary: str = ""
    event_date: str = ""
    effective_date: str = ""
    source_url: str = ""
    source_document_id: str = ""
    official_source: bool = False
    importance: int = 0
    sentiment: str = "unknown"
    amount: float | None = None
    currency: str = ""
    counterparty: str = ""
    status: str = ""
    related_event_key: str = ""
    raw_payload: dict[str, object] = field(default_factory=dict)


class CompanyEventAdapter(Protocol):
    name: str

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]: ...


def clean_text(value: object) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value))).strip()


def normalize_date(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y-%m-%d")


def normalized_code(value: object) -> str:
    digits = re.sub(r"\D", "", clean_text(value))
    return digits[-6:].zfill(6) if digits else ""


def classify_announcement(title: str, category: str = "") -> str:
    text = f"{category} {title}"
    for event_type, keywords in CLASSIFICATION_RULES:
        if any(keyword in text for keyword in keywords):
            return event_type
    return "other"


def repurchase_stage(title: str) -> str:
    for stage, words in (
        ("plan", ("方案", "预案")),
        ("first_execution", ("首次实施", "首次回购")),
        ("progress", ("进展", "比例达到")),
        ("completed", ("完成", "实施结果")),
        ("cancelled", ("终止", "取消")),
    ):
        if any(word in title for word in words):
            return stage
    return "announcement"


def importance_score(event_type: str, title: str, amount: float | None = None) -> int:
    score = {
        "periodic_report": 55,
        "earnings_forecast": 65,
        "earnings_flash": 60,
        "merger_restructuring": 85,
        "administrative_penalty": 80,
        "litigation": 70,
        "share_repurchase": 60,
        "restricted_release": 55,
        "regulatory_inquiry": 70,
    }.get(event_type, 35)
    if any(word in title for word in ("控制权", "重大", "大幅", "终止上市")):
        score += 15
    if amount is not None and amount >= 100_000_000:
        score += 10
    return min(100, score)


class AnnouncementsAdapter:
    name = "巨潮资讯公告"

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]:
        loader = getattr(ak, "stock_zh_a_disclosure_report_cninfo", None)
        if loader is None:
            raise RuntimeError("AkShare未提供巨潮公告适配器")
        frame = loader(
            symbol=security.code,
            market="沪深京",
            keyword="",
            category="",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        return self._normalize(frame, security)

    def _normalize(
        self, frame: pd.DataFrame, security: Security
    ) -> list[NormalizedCompanyEvent]:
        if frame is None or frame.empty:
            return []
        result: list[NormalizedCompanyEvent] = []
        for _, row in frame.iterrows():
            title = clean_text(
                row.get("公告标题", row.get("标题", row.get("公告名称", "")))
            )
            if not title:
                continue
            category = clean_text(row.get("公告类别", row.get("公告类型", "")))
            event_type = classify_announcement(title, category)
            document_id = clean_text(
                row.get("公告ID", row.get("公告编号", row.get("announcementId", "")))
            )
            url = clean_text(row.get("公告链接", row.get("网址", row.get("URL", ""))))
            announcement_date = normalize_date(
                row.get("公告时间", row.get("公告日期", row.get("发布时间", "")))
            )
            subtype = (
                repurchase_stage(title)
                if event_type == "share_repurchase"
                else category
            )
            result.append(
                NormalizedCompanyEvent(
                    security=security,
                    event_type=event_type,
                    event_subtype=subtype,
                    title=title,
                    summary=title,
                    announcement_date=announcement_date,
                    source_name=self.name,
                    source_url=url,
                    source_document_id=document_id,
                    official_source=True,
                    importance=importance_score(event_type, title),
                    raw_payload={
                        str(key): clean_text(value) for key, value in row.items()
                    },
                )
            )
        return result


class DividendAdapter:
    name = "东方财富分红送转"

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]:
        loader = getattr(ak, "stock_fhps_detail_em", None)
        if loader is None:
            raise RuntimeError("AkShare未提供分红适配器")
        frame = loader(symbol=security.code)
        result: list[NormalizedCompanyEvent] = []
        for _, row in (frame if frame is not None else pd.DataFrame()).iterrows():
            announcement = normalize_date(
                row.get("公告日期", row.get("预案公告日", row.get("实施公告日", "")))
            )
            effective = normalize_date(row.get("除权除息日", row.get("股权登记日", "")))
            check_date = effective or announcement
            if check_date and not start.isoformat() <= check_date <= end.isoformat():
                continue
            plan = clean_text(row.get("分红方案说明", row.get("分红方案", "")))
            title = f"分红送转 {plan}".strip()
            result.append(
                NormalizedCompanyEvent(
                    security,
                    "dividend",
                    title,
                    announcement,
                    self.name,
                    summary=title,
                    effective_date=effective,
                    official_source=False,
                    importance=50,
                    raw_payload={
                        str(key): clean_text(value) for key, value in row.items()
                    },
                )
            )
        return result


class RestrictedReleaseAdapter:
    name = "东方财富限售解禁"

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]:
        loader = getattr(ak, "stock_restricted_release_queue_em", None)
        if loader is None:
            raise RuntimeError("AkShare未提供解禁适配器")
        frame = loader(symbol=security.code)
        result: list[NormalizedCompanyEvent] = []
        for _, row in (frame if frame is not None else pd.DataFrame()).iterrows():
            effective = normalize_date(
                row.get("解禁时间", row.get("上市流通日期", row.get("解禁日期", "")))
            )
            if effective and not start.isoformat() <= effective <= end.isoformat():
                continue
            amount = pd.to_numeric(
                row.get("可流通数量", row.get("解禁数量", None)), errors="coerce"
            )
            amount_value = None if pd.isna(amount) else float(amount)
            title = f"限售股解禁 {effective}".strip()
            result.append(
                NormalizedCompanyEvent(
                    security,
                    "restricted_release",
                    title,
                    "",
                    self.name,
                    summary=title,
                    effective_date=effective,
                    official_source=False,
                    importance=importance_score("restricted_release", title),
                    amount=amount_value,
                    currency="shares",
                    raw_payload={
                        str(key): clean_text(value) for key, value in row.items()
                    },
                )
            )
        return result


class PledgeAdapter:
    name = "东方财富股权质押"

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]:
        loader = getattr(ak, "stock_gpzy_individual_pledge_ratio_detail_em", None)
        if loader is None:
            raise RuntimeError("AkShare未提供质押适配器")
        frame = loader(symbol=security.code)
        result: list[NormalizedCompanyEvent] = []
        for _, row in (frame if frame is not None else pd.DataFrame()).iterrows():
            event_date = normalize_date(row.get("交易日期", row.get("公告日期", "")))
            if event_date and not start.isoformat() <= event_date <= end.isoformat():
                continue
            title = "股权质押情况更新"
            result.append(
                NormalizedCompanyEvent(
                    security,
                    "share_pledge",
                    title,
                    event_date,
                    self.name,
                    summary=title,
                    event_date=event_date,
                    official_source=False,
                    importance=45,
                    raw_payload={
                        str(key): clean_text(value) for key, value in row.items()
                    },
                )
            )
        return result


class ShareholderCountAdapter:
    name = "东方财富股东户数"

    def fetch(
        self, security: Security, start: date, end: date
    ) -> list[NormalizedCompanyEvent]:
        loader = getattr(ak, "stock_zh_a_gdhs_detail_em", None)
        if loader is None:
            raise RuntimeError("AkShare未提供股东户数适配器")
        frame = loader(symbol=security.code)
        result: list[NormalizedCompanyEvent] = []
        for _, row in (frame if frame is not None else pd.DataFrame()).iterrows():
            announcement = normalize_date(
                row.get("公告日期", row.get("股东户数统计截止日", ""))
            )
            if (
                announcement
                and not start.isoformat() <= announcement <= end.isoformat()
            ):
                continue
            count = pd.to_numeric(row.get("股东户数", None), errors="coerce")
            summary = (
                f"股东户数为 {int(count):,} 户。"
                if pd.notna(count)
                else "股东户数发生更新，请查看来源。"
            )
            result.append(
                NormalizedCompanyEvent(
                    security,
                    "shareholder_count",
                    "股东户数变化",
                    announcement,
                    self.name,
                    summary=summary,
                    official_source=False,
                    importance=30,
                    amount=None if pd.isna(count) else float(count),
                    currency="households",
                    raw_payload={
                        str(key): clean_text(value) for key, value in row.items()
                    },
                )
            )
        return result


DEFAULT_ADAPTERS: tuple[CompanyEventAdapter, ...] = (
    AnnouncementsAdapter(),
    DividendAdapter(),
    RestrictedReleaseAdapter(),
    PledgeAdapter(),
    ShareholderCountAdapter(),
)


@dataclass(slots=True)
class EventSyncResult:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


class CompanyEventService:
    def __init__(
        self,
        store: HistoricalStore,
        repository: Repository | None = None,
        adapters: Iterable[CompanyEventAdapter] = DEFAULT_ADAPTERS,
    ) -> None:
        self.store = store
        self.repository = repository
        self.adapters = tuple(adapters)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _normalized_title(title: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", title.lower())[:180]

    def dedup_key(self, event: NormalizedCompanyEvent) -> str:
        stage = event.event_subtype if event.event_type == "share_repurchase" else ""
        identity = event.source_document_id or self._normalized_title(event.title)
        raw = "|".join(
            (
                event.security.key,
                event.event_type,
                event.announcement_date or event.effective_date or event.event_date,
                identity,
                stage,
                str(event.amount or ""),
                event.counterparty,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def sync_security(
        self,
        security: Security,
        start: date | None = None,
        end: date | None = None,
        retry_count: int = 2,
        _keep_cancel_state: bool = False,
    ) -> EventSyncResult:
        if not _keep_cancel_state:
            self._cancel.clear()
        end = end or beijing_today()
        scope_key = f"security:{security.key}"
        with self.store.connect() as db:
            state = db.execute(
                "SELECT * FROM event_sync_state WHERE scope_key=?", (scope_key,)
            ).fetchone()
        if start is None:
            cursor = normalize_date(state["cursor"]) if state is not None else ""
            start = (
                pd.Timestamp(cursor).date() - timedelta(days=7)
                if cursor
                else end - timedelta(days=365)
            )
        result = EventSyncResult()
        successful_sources = 0
        for adapter in self.adapters:
            if self._cancel.is_set():
                result.cancelled = True
                break
            events: list[NormalizedCompanyEvent] | None = None
            fetched_successfully = False
            source_scope = f"{scope_key}:source:{adapter.name}"
            with self.store.connect() as db:
                source_state = db.execute(
                    "SELECT first_sync_completed FROM event_sync_state WHERE scope_key=?",
                    (source_scope,),
                ).fetchone()
            source_first_sync = source_state is None or not bool(
                source_state["first_sync_completed"]
            )
            for attempt in range(retry_count + 1):
                try:
                    events = adapter.fetch(security, start, end)
                    fetched_successfully = True
                    successful_sources += 1
                    break
                except Exception as exc:
                    if attempt >= retry_count:
                        result.errors.append(f"{adapter.name}: {exc}")
                    else:
                        time.sleep(0.4 * (attempt + 1))
            for event in events or []:
                result.processed += 1
                inserted, event_id = self._upsert_event(event)
                if inserted:
                    result.inserted += 1
                    if not source_first_sync and self._notify_event(event, event_id):
                        result.notified += 1
                else:
                    result.updated += 1
            if fetched_successfully:
                with self.store.connect() as db:
                    db.execute(
                        """INSERT INTO event_sync_state
                        (scope_key,cursor,first_sync_completed,last_success_at,last_error)
                        VALUES(?,?,1,?,'') ON CONFLICT(scope_key) DO UPDATE SET
                        cursor=excluded.cursor,first_sync_completed=1,
                        last_success_at=excluded.last_success_at,last_error='',
                        updated_at=CURRENT_TIMESTAMP""",
                        (source_scope, end.isoformat(), beijing_now().isoformat()),
                    )
        now = beijing_now().isoformat()
        prior_completed = bool(state["first_sync_completed"]) if state else False
        completed = int(prior_completed or successful_sources > 0)
        cursor = (
            end.isoformat()
            if successful_sources
            else str(state["cursor"] if state else "")
        )
        last_success = (
            now
            if successful_sources
            else str(state["last_success_at"] if state else "")
        )
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO event_sync_state(scope_key,cursor,first_sync_completed,last_success_at,last_error)
                VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET cursor=excluded.cursor,
                first_sync_completed=excluded.first_sync_completed,last_success_at=excluded.last_success_at,
                last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP""",
                (
                    scope_key,
                    cursor,
                    completed,
                    last_success,
                    "\n".join(result.errors[-10:]),
                ),
            )
        return result

    def sync_many(
        self,
        securities: Iterable[Security],
        start: date | None = None,
        end: date | None = None,
        progress: Callable[[int, int, Security], None] | None = None,
    ) -> EventSyncResult:
        self._cancel.clear()
        items = list(securities)
        total = EventSyncResult()
        for index, security in enumerate(items, 1):
            if self._cancel.is_set():
                total.cancelled = True
                break
            current = self.sync_security(security, start, end, _keep_cancel_state=True)
            total.processed += current.processed
            total.inserted += current.inserted
            total.updated += current.updated
            total.notified += current.notified
            total.errors.extend(current.errors)
            if progress:
                progress(index, len(items), security)
        return total

    def _upsert_event(self, event: NormalizedCompanyEvent) -> tuple[bool, int]:
        security_id = self.store.upsert_security(event.security, event.source_name)
        dedup_key = self.dedup_key(event)
        payload = json.dumps(event.raw_payload, ensure_ascii=False, default=str)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = beijing_now().isoformat()
        with self.store.connect() as db:
            existing = db.execute(
                "SELECT id,official_source FROM company_events WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
            if existing is None:
                cursor = db.execute(
                    """INSERT INTO company_events
                    (security_id,event_type,event_subtype,title,summary,announcement_date,event_date,
                    effective_date,source_name,source_url,source_document_id,official_source,importance,
                    sentiment,amount,currency,counterparty,status,dedup_key,related_event_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        security_id,
                        event.event_type,
                        event.event_subtype,
                        event.title,
                        event.summary or event.title,
                        event.announcement_date,
                        event.event_date,
                        event.effective_date,
                        event.source_name,
                        event.source_url,
                        event.source_document_id,
                        int(event.official_source),
                        event.importance
                        or importance_score(
                            event.event_type, event.title, event.amount
                        ),
                        "unknown",
                        event.amount,
                        event.currency,
                        event.counterparty,
                        event.status,
                        dedup_key,
                        event.related_event_key,
                    ),
                )
                event_id = int(cursor.lastrowid)
                inserted = True
            else:
                event_id = int(existing["id"])
                inserted = False
                if event.official_source and not bool(existing["official_source"]):
                    db.execute(
                        """UPDATE company_events SET title=?,summary=?,source_name=?,source_url=?,
                        source_document_id=?,official_source=1,importance=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                        (
                            event.title,
                            event.summary or event.title,
                            event.source_name,
                            event.source_url,
                            event.source_document_id,
                            event.importance,
                            event_id,
                        ),
                    )
            db.execute(
                """INSERT INTO event_sources
                (source_name,source_document_id,source_url,official_source,fetched_at,payload_hash)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_name,source_document_id,source_url)
                DO UPDATE SET fetched_at=excluded.fetched_at,payload_hash=excluded.payload_hash""",
                (
                    event.source_name,
                    event.source_document_id,
                    event.source_url,
                    int(event.official_source),
                    now,
                    payload_hash,
                ),
            )
            source_id = int(
                db.execute(
                    """SELECT id FROM event_sources WHERE source_name=? AND
                    source_document_id=? AND source_url=?""",
                    (event.source_name, event.source_document_id, event.source_url),
                ).fetchone()[0]
            )
            duplicate_payload = db.execute(
                """SELECT 1 FROM event_raw_payloads p JOIN event_sources s ON s.id=p.source_id
                WHERE p.event_id=? AND s.payload_hash=? LIMIT 1""",
                (event_id, payload_hash),
            ).fetchone()
            if duplicate_payload is None:
                db.execute(
                    "INSERT INTO event_raw_payloads(event_id,source_id,payload_json) VALUES(?,?,?)",
                    (event_id, source_id, payload),
                )
        return inserted, event_id

    def _notify_event(self, event: NormalizedCompanyEvent, event_id: int) -> bool:
        if self.repository is None:
            return False
        external_key = f"company-event:{event_id}"
        return self.repository.add_notification(
            event.security,
            EVENT_TYPES.get(event.event_type, "公司事件"),
            event.title,
            content=event.summary or event.title,
            source_url=event.source_url,
            external_key=external_key,
            published_at=event.announcement_date,
        )

    def list_events(
        self,
        security: Security,
        start: str = "",
        end: str = "",
        event_type: str = "",
        official_only: bool = False,
        minimum_importance: int = 0,
        query: str = "",
        limit: int = 1000,
    ) -> list[dict]:
        security_id = self.store.security_id(security)
        if security_id is None:
            return []
        where = ["e.security_id=?"]
        parameters: list[object] = [security_id]
        if start:
            where.append("COALESCE(NULLIF(e.announcement_date,''),e.effective_date)>=?")
            parameters.append(start)
        if end:
            where.append("COALESCE(NULLIF(e.announcement_date,''),e.effective_date)<=?")
            parameters.append(end)
        if event_type:
            where.append("e.event_type=?")
            parameters.append(event_type)
        if official_only:
            where.append("e.official_source=1")
        if minimum_importance:
            where.append("e.importance>=?")
            parameters.append(minimum_importance)
        if query:
            where.append("(e.title LIKE ? OR e.summary LIKE ?)")
            parameters.extend((f"%{query}%", f"%{query}%"))
        parameters.append(limit)
        with self.store.connect() as db:
            rows = db.execute(
                f"""SELECT e.*,n.note FROM company_events e
                LEFT JOIN event_research_notes n ON n.event_id=e.id
                WHERE {" AND ".join(where)}
                ORDER BY COALESCE(NULLIF(e.announcement_date,''),e.effective_date) DESC,e.id DESC
                LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def event_markers(self, security: Security, start: str = "") -> pd.DataFrame:
        rows = self.list_events(security, start=start, limit=5000)
        marker_types = {
            "periodic_report",
            "earnings_forecast",
            "dividend",
            "restricted_release",
            "shareholder_change",
            "executive_change",
            "dragon_tiger",
            "block_trade",
            "merger_restructuring",
            "regulatory_inquiry",
        }
        values = []
        for row in rows:
            if row["event_type"] not in marker_types:
                continue
            marker_date = (
                row["effective_date"] or row["event_date"] or row["announcement_date"]
            )
            if not marker_date:
                continue
            values.append(
                {
                    "date": marker_date,
                    "label": EVENT_TYPES.get(row["event_type"], "事件"),
                    "title": row["title"],
                    "event_type": row["event_type"],
                    "event_id": row["id"],
                    "source_url": row["source_url"],
                }
            )
        return pd.DataFrame(values)

    def mark_read(self, event_id: int, read: bool = True) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE company_events SET is_read=? WHERE id=?", (int(read), event_id)
            )

    def save_note(self, event_id: int, note: str) -> None:
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO event_research_notes(event_id,note) VALUES(?,?)
                ON CONFLICT(event_id) DO UPDATE SET note=excluded.note,updated_at=CURRENT_TIMESTAMP""",
                (event_id, note.strip()),
            )

    def raw_payloads(self, event_id: int) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT s.source_name,s.source_url,s.official_source,s.fetched_at,p.payload_json
                FROM event_raw_payloads p LEFT JOIN event_sources s ON s.id=p.source_id
                WHERE p.event_id=? ORDER BY p.id""",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]
