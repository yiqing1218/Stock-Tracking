from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SecurityType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"

    @property
    def label(self) -> str:
        return {
            SecurityType.STOCK: "股票",
            SecurityType.ETF: "ETF",
            SecurityType.INDEX: "指数",
        }[self]


@dataclass(frozen=True, slots=True)
class Security:
    code: str
    name: str
    security_type: SecurityType = SecurityType.STOCK
    market: str = ""

    @property
    def key(self) -> str:
        return f"{self.security_type.value}:{self.code}"

    @property
    def display_code(self) -> str:
        market = self.market.upper()
        return f"{market}{self.code}" if market else self.code

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "security_type": self.security_type.value,
            "market": self.market,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Security":
        return cls(
            code=str(value["code"]),
            name=str(value["name"]),
            security_type=SecurityType(value.get("security_type", "stock")),
            market=str(value.get("market", "")),
        )


@dataclass(slots=True)
class Quote:
    security: Security
    price: float | None = None
    change: float | None = None
    change_pct: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    amplitude: float | None = None
    turnover: float | None = None
    volume_ratio: float | None = None
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    float_market_cap: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WatchlistGroup:
    id: int
    name: str
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class NewsArticle:
    title: str
    summary: str = ""
    source: str = ""
    published_at: str = ""
    url: str = ""


@dataclass(slots=True)
class CustomIndicator:
    id: int | None
    name: str
    formula: str
    color: str = "#38BDF8"
    category: str = "趋势"
    in_library: bool = False
