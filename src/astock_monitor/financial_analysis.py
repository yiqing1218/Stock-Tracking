from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class FinancialMetric:
    group: str
    name: str
    value: float | None
    yoy: float | None
    qoq: float | None
    standardized: float | None
    explanation: str
    source_column: str = ""


FINANCIAL_FIELDS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "利润与质量",
        "营业收入",
        ("营业收入", "营业总收入"),
        "企业主营及其他经营活动确认的收入，是规模与成长分析的基础。",
    ),
    (
        "利润与质量",
        "净利润",
        ("归母净利润", "净利润"),
        "扣除各项成本费用和税后的利润；需与现金流、非经常损益共同判断质量。",
    ),
    (
        "利润与质量",
        "扣非净利润",
        ("扣非净利润", "扣除非经常性损益后的净利润"),
        "剔除一次性损益后更接近持续经营成果。",
    ),
    (
        "利润与质量",
        "毛利率",
        ("销售毛利率", "毛利率"),
        "毛利润占营业收入的比例，反映产品定价和直接成本控制。",
    ),
    (
        "利润与质量",
        "净利率",
        ("销售净利率", "净利率"),
        "净利润占收入比例，综合反映经营效率和费用负担。",
    ),
    (
        "利润与质量",
        "ROE",
        ("净资产收益率", "ROE"),
        "归属股东利润相对净资产的回报，应结合杠杆与资产周转进行杜邦拆解。",
    ),
    (
        "现金流",
        "经营现金流",
        ("经营活动产生的现金流量净额", "经营现金流"),
        "主营经营实际带来的净现金，长期明显低于净利润需进一步核对。",
    ),
    (
        "现金流",
        "自由现金流",
        ("自由现金流",),
        "经营现金流扣除维持和扩张所需资本开支后的现金余量。",
    ),
    (
        "现金流",
        "资本开支",
        ("购建固定资产", "资本性支出", "资本开支"),
        "用于长期资产的现金支出，决定自由现金流和未来产能。",
    ),
    (
        "资产质量",
        "应收账款",
        ("应收账款",),
        "尚未收回的赊销款项；增速长期高于收入可能意味着回款压力。",
    ),
    (
        "资产质量",
        "存货",
        ("存货",),
        "尚未销售或投入生产的资产；快速积累可能反映备货、扩产或需求走弱。",
    ),
    ("资产质量", "商誉", ("商誉",), "并购溢价形成的资产，盈利不及预期时可能发生减值。"),
    (
        "负债与偿债",
        "有息负债",
        ("有息负债", "短期借款", "长期借款"),
        "需要支付利息的债务，是衡量财务杠杆和偿债压力的核心。",
    ),
    (
        "负债与偿债",
        "合同负债",
        ("合同负债", "预收款项"),
        "客户已付款但收入尚未确认的义务，在部分行业可反映订单与预收情况。",
    ),
    (
        "费用投入",
        "研发费用",
        ("研发费用",),
        "研发活动当期费用化投入，应结合收入增长、资本化比例和行业特征。",
    ),
    (
        "费用投入",
        "销售费用",
        ("销售费用",),
        "销售渠道、广告和人员等费用；费用率变化体现获客效率。",
    ),
    (
        "费用投入",
        "管理费用",
        ("管理费用",),
        "公司治理和后台运营费用；需关注规模扩张下的费用效率。",
    ),
    (
        "费用投入",
        "财务费用",
        ("财务费用",),
        "利息、汇兑等财务活动成本，与负债结构高度相关。",
    ),
)


def analyze_financial_frame(frame: pd.DataFrame) -> list[FinancialMetric]:
    if frame is None or frame.empty:
        return []
    data = frame.copy()
    date_column = next(
        (
            c
            for c in data.columns
            if any(k in str(c) for k in ("报告期", "日期", "截止"))
        ),
        None,
    )
    if date_column:
        data["_date"] = pd.to_datetime(data[date_column], errors="coerce")
        data = data.sort_values("_date")
    metrics: list[FinancialMetric] = []
    for group, name, aliases, explanation in FINANCIAL_FIELDS:
        column = next(
            (
                str(c)
                for c in data.columns
                if any(alias.lower() in str(c).lower() for alias in aliases)
            ),
            "",
        )
        if not column:
            metrics.append(
                FinancialMetric(group, name, None, None, None, None, explanation)
            )
            continue
        series = pd.to_numeric(
            data[column]
            .astype(str)
            .str.replace("%", "", regex=False)
            .str.replace(",", "", regex=False),
            errors="coerce",
        )
        valid = series.dropna()
        if valid.empty:
            metrics.append(
                FinancialMetric(
                    group, name, None, None, None, None, explanation, column
                )
            )
            continue
        value = float(valid.iloc[-1])
        qoq = (
            float((valid.iloc[-1] / valid.iloc[-2] - 1) * 100)
            if len(valid) >= 2 and valid.iloc[-2]
            else None
        )
        yoy = (
            float((valid.iloc[-1] / valid.iloc[-5] - 1) * 100)
            if len(valid) >= 5 and valid.iloc[-5]
            else None
        )
        std = (
            float((value - valid.mean()) / valid.std(ddof=0))
            if len(valid) >= 3 and valid.std(ddof=0) > 0
            else None
        )
        metrics.append(
            FinancialMetric(group, name, value, yoy, qoq, std, explanation, column)
        )
    return metrics


def financial_quality_flags(metrics: list[FinancialMetric]) -> list[str]:
    values = {item.name: item for item in metrics}
    flags: list[str] = []
    receivable = values.get("应收账款")
    revenue = values.get("营业收入")
    inventory = values.get("存货")
    if (
        receivable
        and revenue
        and receivable.yoy is not None
        and revenue.yoy is not None
        and receivable.yoy > revenue.yoy + 10
    ):
        flags.append("应收账款同比增速明显高于营业收入，建议核对回款和信用政策。")
    if (
        inventory
        and revenue
        and inventory.yoy is not None
        and revenue.yoy is not None
        and inventory.yoy > revenue.yoy + 10
    ):
        flags.append("存货同比增速明显高于营业收入，建议核对库存结构和减值风险。")
    cash = values.get("经营现金流")
    profit = values.get("净利润")
    if (
        cash
        and profit
        and cash.value is not None
        and profit.value
        and cash.value < profit.value * 0.5
    ):
        flags.append("经营现金流低于净利润的一半，盈利现金含量偏弱。")
    goodwill = values.get("商誉")
    if goodwill and goodwill.standardized is not None and goodwill.standardized > 1.5:
        flags.append("商誉处于自身历史较高位置，需关注并购标的业绩和减值测试。")
    return flags
