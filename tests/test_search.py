from __future__ import annotations

from astock_monitor.models import Security, SecurityType
from astock_monitor.watchlist_page import normalize_security_query, rank_security_search


UNIVERSE = [
    Security("600519", "贵州茅台", SecurityType.STOCK, "sh"),
    Security("000333", "美的集团", SecurityType.STOCK, "sz"),
    Security("000001", "平安银行", SecurityType.STOCK, "sz"),
    Security("000001", "上证指数", SecurityType.INDEX, "sh"),
    Security("510300", "沪深300ETF", SecurityType.ETF, "sh"),
]


def test_search_accepts_market_prefix_and_full_width_code() -> None:
    assert normalize_security_query(" SH-600519 ") == "600519"
    assert rank_security_search(UNIVERSE, "ＳＨ６００５１９")[0].name == "贵州茅台"


def test_search_ranks_exact_code_and_partial_name() -> None:
    assert rank_security_search(UNIVERSE, "000333")[0].name == "美的集团"
    assert rank_security_search(UNIVERSE, "茅台")[0].code == "600519"
    assert rank_security_search(UNIVERSE, "300etf")[0].code == "510300"


def test_duplicate_code_prefers_a_share_stock() -> None:
    results = rank_security_search(UNIVERSE, "000001")
    assert [item.security_type for item in results[:2]] == [
        SecurityType.STOCK,
        SecurityType.INDEX,
    ]
