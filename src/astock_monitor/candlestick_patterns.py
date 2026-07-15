from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class CandlestickPattern:
    name: str
    family: str
    direction: str
    explanation: str
    key: str


def _explain(name: str, shape: str, meaning: str) -> str:
    return f"{name}：{shape}。常见解读为{meaning}。形态只描述价格结构，需结合趋势位置、成交量和后续K线确认，不能单独视为买卖结论。"


PATTERNS: tuple[CandlestickPattern, ...] = (
    CandlestickPattern(
        "大阳线",
        "单K",
        "看多",
        _explain("大阳线", "实体明显较长且收盘接近最高价", "当日买方占优"),
        "long_bull",
    ),
    CandlestickPattern(
        "大阴线",
        "单K",
        "看空",
        _explain("大阴线", "实体明显较长且收盘接近最低价", "当日卖方占优"),
        "long_bear",
    ),
    CandlestickPattern(
        "十字星",
        "单K",
        "中性",
        _explain(
            "十字星", "开盘与收盘接近，实体很小", "多空暂时均衡，转折与否取决于所处趋势"
        ),
        "doji",
    ),
    CandlestickPattern(
        "长腿十字",
        "单K",
        "中性",
        _explain("长腿十字", "实体很小且上下影线都长", "盘中分歧显著"),
        "long_leg_doji",
    ),
    CandlestickPattern(
        "蜻蜓十字",
        "单K",
        "看多",
        _explain("蜻蜓十字", "收开接近高位且下影线很长", "低位承接增强"),
        "dragonfly",
    ),
    CandlestickPattern(
        "墓碑十字",
        "单K",
        "看空",
        _explain("墓碑十字", "收开接近低位且上影线很长", "高位抛压增强"),
        "gravestone",
    ),
    CandlestickPattern(
        "锤头线",
        "单K",
        "看多",
        _explain("锤头线", "小实体、长下影、短上影", "下跌后可能出现承接"),
        "hammer",
    ),
    CandlestickPattern(
        "倒锤头",
        "单K",
        "看多",
        _explain("倒锤头", "小实体、长上影、短下影", "下跌后买方曾明显反攻"),
        "inverted_hammer",
    ),
    CandlestickPattern(
        "上吊线",
        "单K",
        "看空",
        _explain("上吊线", "上涨后出现长下影小实体", "高位抛压可能增加"),
        "hanging_man",
    ),
    CandlestickPattern(
        "流星线",
        "单K",
        "看空",
        _explain("流星线", "上涨后出现长上影小实体", "冲高受阻"),
        "shooting_star",
    ),
    CandlestickPattern(
        "光头阳线",
        "单K",
        "看多",
        _explain("光头阳线", "收盘接近最高价", "买盘持续到收盘"),
        "bull_marubozu",
    ),
    CandlestickPattern(
        "光脚阴线",
        "单K",
        "看空",
        _explain("光脚阴线", "收盘接近最低价", "卖压持续到收盘"),
        "bear_marubozu",
    ),
    CandlestickPattern(
        "纺锤线",
        "单K",
        "中性",
        _explain("纺锤线", "小实体且上下影线存在", "多空犹豫"),
        "spinning_top",
    ),
    CandlestickPattern(
        "一字线",
        "单K",
        "中性",
        _explain(
            "一字线", "开高低收几乎相同", "常见于涨跌停或极低流动性，须核对成交状态"
        ),
        "four_price",
    ),
    CandlestickPattern(
        "阳线吞没",
        "双K",
        "看多",
        _explain("阳线吞没", "后阳线实体完全覆盖前阴线实体", "买方反包前一日卖压"),
        "bull_engulf",
    ),
    CandlestickPattern(
        "阴线反包",
        "双K",
        "看空",
        _explain("阴线反包", "后阴线实体完全覆盖前阳线实体", "卖方反包前一日涨幅"),
        "bear_engulf",
    ),
    CandlestickPattern(
        "曙光初现",
        "双K",
        "看多",
        _explain("曙光初现", "长阴后阳线收复前阴实体中部", "下跌动能可能减弱"),
        "piercing",
    ),
    CandlestickPattern(
        "乌云盖顶",
        "双K",
        "看空",
        _explain("乌云盖顶", "长阳后阴线跌入前阳实体中部", "上涨动能可能减弱"),
        "dark_cloud",
    ),
    CandlestickPattern(
        "看涨孕线",
        "双K",
        "看多",
        _explain("看涨孕线", "下跌中小阳实体被前长阴实体包含", "波动收缩并可能止跌"),
        "bull_harami",
    ),
    CandlestickPattern(
        "看跌孕线",
        "双K",
        "看空",
        _explain("看跌孕线", "上涨中小阴实体被前长阳实体包含", "波动收缩并可能转弱"),
        "bear_harami",
    ),
    CandlestickPattern(
        "孕十字",
        "双K",
        "中性",
        _explain("孕十字", "十字星实体位于前一长实体内部", "趋势出现明显犹豫"),
        "harami_cross",
    ),
    CandlestickPattern(
        "平头底部",
        "双K",
        "看多",
        _explain("平头底部", "连续两根K线最低价接近", "相同价位出现支撑"),
        "tweezer_bottom",
    ),
    CandlestickPattern(
        "平头顶部",
        "双K",
        "看空",
        _explain("平头顶部", "连续两根K线最高价接近", "相同价位出现压力"),
        "tweezer_top",
    ),
    CandlestickPattern(
        "向上跳空",
        "双K",
        "看多",
        _explain("向上跳空", "后K最低价高于前K最高价", "价格重心快速上移"),
        "gap_up",
    ),
    CandlestickPattern(
        "向下跳空",
        "双K",
        "看空",
        _explain("向下跳空", "后K最高价低于前K最低价", "价格重心快速下移"),
        "gap_down",
    ),
    CandlestickPattern(
        "红三兵（三白兵/三尖兵）",
        "三K",
        "看多",
        _explain("红三兵", "连续三根阳线且收盘逐步走高", "买方持续推进"),
        "three_soldiers",
    ),
    CandlestickPattern(
        "三只乌鸦",
        "三K",
        "看空",
        _explain("三只乌鸦", "连续三根阴线且收盘逐步走低", "卖方持续推进"),
        "three_crows",
    ),
    CandlestickPattern(
        "早晨之星",
        "三K",
        "看多",
        _explain("早晨之星", "长阴、小实体、长阳三段结构", "下跌后可能反转"),
        "morning_star",
    ),
    CandlestickPattern(
        "黄昏之星",
        "三K",
        "看空",
        _explain("黄昏之星", "长阳、小实体、长阴三段结构", "上涨后可能反转"),
        "evening_star",
    ),
    CandlestickPattern(
        "早晨十字星",
        "三K",
        "看多",
        _explain("早晨十字星", "中间K为十字星的早晨之星", "底部犹豫后买方反攻"),
        "morning_doji",
    ),
    CandlestickPattern(
        "黄昏十字星",
        "三K",
        "看空",
        _explain("黄昏十字星", "中间K为十字星的黄昏之星", "顶部犹豫后卖方反攻"),
        "evening_doji",
    ),
    CandlestickPattern(
        "三内升",
        "三K",
        "看多",
        _explain("三内升", "看涨孕线后继续收高", "孕线反转得到确认"),
        "three_inside_up",
    ),
    CandlestickPattern(
        "三内降",
        "三K",
        "看空",
        _explain("三内降", "看跌孕线后继续收低", "孕线转弱得到确认"),
        "three_inside_down",
    ),
    CandlestickPattern(
        "三外升",
        "三K",
        "看多",
        _explain("三外升", "阳线吞没后继续收高", "反包形态得到确认"),
        "three_outside_up",
    ),
    CandlestickPattern(
        "三外降",
        "三K",
        "看空",
        _explain("三外降", "阴线反包后继续收低", "反包形态得到确认"),
        "three_outside_down",
    ),
    CandlestickPattern(
        "两阳夹一阴",
        "三K",
        "看多",
        _explain("两阳夹一阴", "两根阳线夹住一根较小阴线", "短暂整理后买方恢复"),
        "up_sandwich",
    ),
    CandlestickPattern(
        "两阴夹一阳",
        "三K",
        "看空",
        _explain("两阴夹一阳", "两根阴线夹住一根较小阳线", "短暂反弹后卖方恢复"),
        "down_sandwich",
    ),
    CandlestickPattern(
        "上升三法",
        "多K",
        "看多",
        _explain("上升三法", "长阳后数根小阴整理，再以长阳突破", "趋势中的整理延续"),
        "rising_three",
    ),
    CandlestickPattern(
        "下降三法",
        "多K",
        "看空",
        _explain("下降三法", "长阴后数根小阳整理，再以长阴破位", "下跌趋势延续"),
        "falling_three",
    ),
    CandlestickPattern(
        "多方炮",
        "三K",
        "看多",
        _explain("多方炮", "阳阴阳结构且第三根收复中间阴线", "多方重新控制"),
        "bull_cannon",
    ),
    CandlestickPattern(
        "空方炮",
        "三K",
        "看空",
        _explain("空方炮", "阴阳阴结构且第三根跌破中间阳线", "空方重新控制"),
        "bear_cannon",
    ),
    CandlestickPattern(
        "好友反攻",
        "双K",
        "看多",
        _explain("好友反攻", "长阴后阳线收盘接近前阴收盘", "低位出现反击"),
        "matching_low",
    ),
    CandlestickPattern(
        "淡友反攻",
        "双K",
        "看空",
        _explain("淡友反攻", "长阳后阴线收盘接近前阳收盘", "高位出现反击"),
        "matching_high",
    ),
    CandlestickPattern(
        "尽头线",
        "双K",
        "中性",
        _explain("尽头线", "小实体藏在前一长K影线范围内", "原趋势动能可能耗尽"),
        "meeting_end",
    ),
    CandlestickPattern(
        "穿头破脚",
        "双K",
        "反转",
        _explain(
            "穿头破脚", "后一K线高低范围完全包住前一K线", "波动突然扩张，方向由收盘决定"
        ),
        "outside_bar",
    ),
    CandlestickPattern(
        "身怀六甲",
        "双K",
        "中性",
        _explain("身怀六甲", "后一根小K完全位于前一长K范围内", "波动收缩等待选择方向"),
        "inside_bar",
    ),
    CandlestickPattern(
        "高位揉搓线",
        "多K",
        "中性",
        _explain("高位揉搓线", "上涨后连续出现长上下影小实体", "高位换手与分歧增加"),
        "top_churn",
    ),
    CandlestickPattern(
        "低位揉搓线",
        "多K",
        "中性",
        _explain("低位揉搓线", "下跌后连续出现长上下影小实体", "低位换手与承接增加"),
        "bottom_churn",
    ),
    CandlestickPattern(
        "岛形反转向上",
        "多K",
        "看多",
        _explain(
            "岛形反转向上", "向下跳空形成孤岛后再向上跳空", "价格区间被快速重新定价"
        ),
        "island_up",
    ),
    CandlestickPattern(
        "岛形反转向下",
        "多K",
        "看空",
        _explain(
            "岛形反转向下", "向上跳空形成孤岛后再向下跳空", "价格区间被快速重新定价"
        ),
        "island_down",
    ),
)


def detect_patterns(frame: pd.DataFrame) -> dict[str, bool]:
    result = {item.key: False for item in PATTERNS}
    if frame is None or len(frame) < 1:
        return result
    f = frame.tail(8).copy()
    for col in ("open", "high", "low", "close"):
        f[col] = pd.to_numeric(f[col], errors="coerce")
    f = f.dropna(subset=["open", "high", "low", "close"])
    if f.empty:
        return result
    o, h, low, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    span = np.maximum(h - low, 1e-12)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - low
    bull = c > o
    bear = c < o
    doji = body / span <= 0.1
    long = body / span >= 0.65
    result.update(
        {
            "long_bull": bool(bull[-1] and long[-1]),
            "long_bear": bool(bear[-1] and long[-1]),
            "doji": bool(doji[-1]),
            "long_leg_doji": bool(
                doji[-1] and upper[-1] / span[-1] > 0.3 and lower[-1] / span[-1] > 0.3
            ),
            "dragonfly": bool(doji[-1] and lower[-1] / span[-1] > 0.6),
            "gravestone": bool(doji[-1] and upper[-1] / span[-1] > 0.6),
            "hammer": bool(
                lower[-1] > 2 * max(body[-1], span[-1] * 0.03) and upper[-1] < body[-1]
            ),
            "inverted_hammer": bool(
                upper[-1] > 2 * max(body[-1], span[-1] * 0.03) and lower[-1] < body[-1]
            ),
            "spinning_top": bool(body[-1] / span[-1] < 0.35 and not doji[-1]),
            "four_price": bool(span[-1] <= max(abs(c[-1]) * 0.0002, 1e-6)),
            "bull_marubozu": bool(bull[-1] and upper[-1] / span[-1] < 0.08),
            "bear_marubozu": bool(bear[-1] and lower[-1] / span[-1] < 0.08),
        }
    )
    trend = (c[-1] - c[max(0, len(c) - 6)]) if len(c) > 1 else 0
    result["hanging_man"] = result["hammer"] and trend > 0
    result["shooting_star"] = result["inverted_hammer"] and trend > 0
    if len(f) >= 2:
        result.update(
            {
                "bull_engulf": bool(
                    bear[-2] and bull[-1] and o[-1] <= c[-2] and c[-1] >= o[-2]
                ),
                "bear_engulf": bool(
                    bull[-2] and bear[-1] and o[-1] >= c[-2] and c[-1] <= o[-2]
                ),
                "piercing": bool(bear[-2] and bull[-1] and c[-1] > (o[-2] + c[-2]) / 2),
                "dark_cloud": bool(
                    bull[-2] and bear[-1] and c[-1] < (o[-2] + c[-2]) / 2
                ),
                "bull_harami": bool(
                    bear[-2]
                    and bull[-1]
                    and min(o[-1], c[-1]) >= min(o[-2], c[-2])
                    and max(o[-1], c[-1]) <= max(o[-2], c[-2])
                ),
                "bear_harami": bool(
                    bull[-2]
                    and bear[-1]
                    and min(o[-1], c[-1]) >= min(o[-2], c[-2])
                    and max(o[-1], c[-1]) <= max(o[-2], c[-2])
                ),
                "harami_cross": bool(
                    doji[-1]
                    and min(o[-1], c[-1]) >= min(o[-2], c[-2])
                    and max(o[-1], c[-1]) <= max(o[-2], c[-2])
                ),
                "tweezer_bottom": bool(abs(low[-1] - low[-2]) / max(c[-1], 1) < 0.003),
                "tweezer_top": bool(abs(h[-1] - h[-2]) / max(c[-1], 1) < 0.003),
                "gap_up": bool(low[-1] > h[-2]),
                "gap_down": bool(h[-1] < low[-2]),
                "matching_low": bool(
                    bear[-2] and bull[-1] and abs(c[-1] - c[-2]) / max(c[-1], 1) < 0.004
                ),
                "matching_high": bool(
                    bull[-2] and bear[-1] and abs(c[-1] - c[-2]) / max(c[-1], 1) < 0.004
                ),
                "outside_bar": bool(h[-1] > h[-2] and low[-1] < low[-2]),
                "inside_bar": bool(h[-1] < h[-2] and low[-1] > low[-2]),
                "meeting_end": bool(
                    body[-1] < body[-2] * 0.35 and low[-1] >= low[-2] and h[-1] <= h[-2]
                ),
            }
        )
    if len(f) >= 3:
        result.update(
            {
                "three_soldiers": bool(all(bull[-3:]) and c[-3] < c[-2] < c[-1]),
                "three_crows": bool(all(bear[-3:]) and c[-3] > c[-2] > c[-1]),
                "morning_star": bool(
                    bear[-3]
                    and long[-3]
                    and body[-2] < body[-3] * 0.45
                    and bull[-1]
                    and c[-1] > (o[-3] + c[-3]) / 2
                ),
                "evening_star": bool(
                    bull[-3]
                    and long[-3]
                    and body[-2] < body[-3] * 0.45
                    and bear[-1]
                    and c[-1] < (o[-3] + c[-3]) / 2
                ),
                "up_sandwich": bool(
                    bull[-3] and bear[-2] and bull[-1] and c[-1] > c[-3]
                ),
                "down_sandwich": bool(
                    bear[-3] and bull[-2] and bear[-1] and c[-1] < c[-3]
                ),
                "bull_cannon": bool(
                    bull[-3] and bear[-2] and bull[-1] and c[-1] > o[-2]
                ),
                "bear_cannon": bool(
                    bear[-3] and bull[-2] and bear[-1] and c[-1] < o[-2]
                ),
                "top_churn": bool(
                    trend > 0
                    and upper[-1] / span[-1] > 0.35
                    and lower[-2] / span[-2] > 0.35
                ),
                "bottom_churn": bool(
                    trend < 0
                    and lower[-1] / span[-1] > 0.35
                    and upper[-2] / span[-2] > 0.35
                ),
                "island_up": bool(h[-3] < low[-2] and low[-1] > h[-2]),
                "island_down": bool(low[-3] > h[-2] and h[-1] < low[-2]),
            }
        )
        result["morning_doji"] = result["morning_star"] and bool(doji[-2])
        result["evening_doji"] = result["evening_star"] and bool(doji[-2])
        result["three_inside_up"] = result["bull_harami"] and c[-1] > h[-2]
        result["three_inside_down"] = result["bear_harami"] and c[-1] < low[-2]
        result["three_outside_up"] = bool(result["bull_engulf"] and c[-1] > c[-2])
        result["three_outside_down"] = bool(result["bear_engulf"] and c[-1] < c[-2])
    if len(f) >= 5:
        result["rising_three"] = bool(
            bull[-5] and bull[-1] and c[-1] > h[-5] and all(h[-4:-1] < h[-5])
        )
        result["falling_three"] = bool(
            bear[-5] and bear[-1] and c[-1] < low[-5] and all(low[-4:-1] > low[-5])
        )
    return result
