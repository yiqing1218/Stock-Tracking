from __future__ import annotations

from astock_monitor.models import CustomIndicator, Security, SecurityType
from astock_monitor.repository import Repository


def test_watchlist_and_formula_persist(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(tmp_path / "test.db")
    security = Security("000002", "万科A", SecurityType.STOCK, "sz")
    repository.add_security(security)
    assert repository.contains_security(security)
    repository.remove_security(security)
    assert not repository.contains_security(security)

    saved = repository.save_custom_indicator(
        CustomIndicator(None, "测试指标", "SMA(close, 20)")
    )
    assert saved.id is not None
    assert any(item.name == "测试指标" for item in repository.list_custom_indicators())
    repository.delete_custom_indicator(saved.id)
    assert all(item.name != "测试指标" for item in repository.list_custom_indicators())


def test_watchlist_groups_move_and_reorder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(tmp_path / "groups.db")
    growth = repository.create_group("成长")
    first = Security("000002", "万科A", SecurityType.STOCK, "sz")
    second = Security("000333", "美的集团", SecurityType.STOCK, "sz")
    repository.add_security(first, growth.id)
    repository.add_security(second, growth.id)
    assert [item.code for item in repository.list_watchlist(growth.id)][-2:] == [
        "000002",
        "000333",
    ]
    repository.move_security(second, -1)
    assert [item.code for item in repository.list_watchlist(growth.id)][-2:] == [
        "000333",
        "000002",
    ]
    default = next(group for group in repository.list_groups() if group.name == "默认分组")
    repository.move_security_to_group(first, default.id)
    assert repository.group_for_security(first) == default
    repository.delete_group(growth.id)
    assert repository.group_for_security(second) == default
