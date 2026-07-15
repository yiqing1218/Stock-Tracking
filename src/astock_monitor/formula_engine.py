from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .indicators import ema, safe_div, sma, wma


class FormulaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FormulaValidation:
    dependencies: tuple[str, ...]
    normalized_formula: str


class FormulaEngine:
    """只执行白名单数学表达式，不使用 Python eval。"""

    MAX_LENGTH = 800
    MAX_NODES = 180
    MAX_WINDOW = 500

    def __init__(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            raise FormulaError("没有可用于计算的行情数据")
        index = frame.index
        close = self._series(frame, "close")
        high = self._series(frame, "high")
        low = self._series(frame, "low")
        self.variables: dict[str, pd.Series] = {
            "open": self._series(frame, "open"),
            "high": high,
            "low": low,
            "close": close,
            "volume": self._series(frame, "volume"),
            "amount": self._series(frame, "amount"),
            "turnover": self._series(frame, "turnover"),
            "pct_change": self._series(frame, "pct_change"),
            "returns": frame["returns"].astype(float) if "returns" in frame else close.pct_change(fill_method=None) * 100,
            "typical": (high + low + close) / 3,
            "hl2": (high + low) / 2,
            "ohlc4": (self._series(frame, "open") + high + low + close) / 4,
        }
        self.index = index
        self.functions: dict[str, Callable[..., object]] = {
            "SMA": self._sma,
            "EMA": self._ema,
            "WMA": self._wma,
            "SUM": self._sum,
            "STD": self._std,
            "HHV": self._hhv,
            "LLV": self._llv,
            "REF": self._ref,
            "DIFF": self._diff,
            "ROC": self._roc,
            "ABS": self._abs,
            "MAX": self._maximum,
            "MIN": self._minimum,
            "IF": self._if,
            "CROSS": self._cross,
            "LOG": self._log,
            "SQRT": self._sqrt,
            "ZSCORE": self._zscore,
            "TS_RANK": self._ts_rank,
            "CORR": self._corr,
            "CLIP": self._clip,
        }

    @staticmethod
    def _series(frame: pd.DataFrame, name: str) -> pd.Series:
        if name in frame:
            return pd.to_numeric(frame[name], errors="coerce").astype(float)
        return pd.Series(np.nan, index=frame.index, dtype=float)

    def validate(self, formula: str) -> FormulaValidation:
        tree = self._parse(formula)
        dependencies = sorted(
            {
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id.lower() in self.variables
            }
        )
        self._evaluate_node(tree.body)
        return FormulaValidation(tuple(dependencies), ast.unparse(tree.body))

    def evaluate(self, formula: str) -> pd.Series:
        tree = self._parse(formula)
        result = self._evaluate_node(tree.body)
        series = self._to_series(result)
        return pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)

    def _parse(self, formula: str) -> ast.Expression:
        formula = formula.strip()
        if not formula:
            raise FormulaError("公式不能为空")
        if len(formula) > self.MAX_LENGTH:
            raise FormulaError(f"公式过长，最多 {self.MAX_LENGTH} 个字符")
        try:
            tree = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise FormulaError(f"公式语法错误：{exc.msg}") from exc
        if sum(1 for _ in ast.walk(tree)) > self.MAX_NODES:
            raise FormulaError("公式过于复杂，请拆分后再计算")
        forbidden = (
            ast.Attribute,
            ast.Subscript,
            ast.Lambda,
            ast.Dict,
            ast.List,
            ast.Tuple,
            ast.Set,
            ast.ListComp,
            ast.DictComp,
            ast.GeneratorExp,
            ast.NamedExpr,
        )
        if any(isinstance(node, forbidden) for node in ast.walk(tree)):
            raise FormulaError("公式包含不允许的访问或对象构造")
        return tree

    def _evaluate_node(self, node: ast.AST) -> object:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool)):
                return node.value
            raise FormulaError("仅允许数字常量")
        if isinstance(node, ast.Name):
            key = node.id.lower()
            if key in self.variables:
                return self.variables[key]
            raise FormulaError(f"未知变量：{node.id}")
        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left)
            right = self._evaluate_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return self._safe_operator_div(left, right)
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if isinstance(right, (int, float)) and abs(float(right)) <= 10:
                    return left**right
                raise FormulaError("幂指数必须是绝对值不超过10的常量")
            if isinstance(node.op, ast.BitAnd):
                return self._to_series(left).astype(bool) & self._to_series(right).astype(bool)
            if isinstance(node.op, ast.BitOr):
                return self._to_series(left).astype(bool) | self._to_series(right).astype(bool)
            raise FormulaError("不支持该运算符")
        if isinstance(node, ast.UnaryOp):
            operand = self._evaluate_node(node.operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, (ast.Not, ast.Invert)):
                return ~self._to_series(operand).astype(bool)
            raise FormulaError("不支持该一元运算")
        if isinstance(node, ast.BoolOp):
            values = [self._to_series(self._evaluate_node(value)).astype(bool) for value in node.values]
            result = values[0]
            for value in values[1:]:
                result = result & value if isinstance(node.op, ast.And) else result | value
            return result
        if isinstance(node, ast.Compare):
            left = self._evaluate_node(node.left)
            result = pd.Series(True, index=self.index)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._evaluate_node(comparator)
                if isinstance(operator, ast.Gt):
                    current = left > right
                elif isinstance(operator, ast.GtE):
                    current = left >= right
                elif isinstance(operator, ast.Lt):
                    current = left < right
                elif isinstance(operator, ast.LtE):
                    current = left <= right
                elif isinstance(operator, ast.Eq):
                    current = left == right
                elif isinstance(operator, ast.NotEq):
                    current = left != right
                else:
                    raise FormulaError("不支持该比较运算")
                result &= self._to_series(current).astype(bool)
                left = right
            return result
        if isinstance(node, ast.IfExp):
            return self._if(
                self._evaluate_node(node.test),
                self._evaluate_node(node.body),
                self._evaluate_node(node.orelse),
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise FormulaError("仅允许调用内置指标函数")
            function_name = node.func.id.upper()
            function = self.functions.get(function_name)
            if function is None:
                raise FormulaError(f"未知函数：{node.func.id}")
            if node.keywords:
                raise FormulaError("指标函数暂不支持命名参数")
            arguments = [self._evaluate_node(argument) for argument in node.args]
            try:
                return function(*arguments)
            except FormulaError:
                raise
            except (TypeError, ValueError) as exc:
                raise FormulaError(f"{function_name} 参数错误：{exc}") from exc
        raise FormulaError(f"不支持的公式结构：{type(node).__name__}")

    def _to_series(self, value: object) -> pd.Series:
        if isinstance(value, pd.Series):
            return value.reindex(self.index)
        if isinstance(value, (int, float, bool, np.number)):
            return pd.Series(value, index=self.index)
        if isinstance(value, np.ndarray) and len(value) == len(self.index):
            return pd.Series(value, index=self.index)
        raise FormulaError("表达式结果必须是数值或时间序列")

    def _window(self, value: object) -> int:
        if isinstance(value, pd.Series):
            raise FormulaError("窗口参数必须是整数常量")
        try:
            window = int(value)
        except (TypeError, ValueError) as exc:
            raise FormulaError("窗口参数必须是整数") from exc
        if window < 1 or window > self.MAX_WINDOW:
            raise FormulaError(f"窗口必须在 1 到 {self.MAX_WINDOW} 之间")
        return window

    def _safe_operator_div(self, left: object, right: object) -> object:
        if isinstance(left, pd.Series) or isinstance(right, pd.Series):
            return safe_div(self._to_series(left), self._to_series(right))
        if float(right) == 0:
            return np.nan
        return left / right

    def _sma(self, value: object, window: object) -> pd.Series:
        return sma(self._to_series(value), self._window(window))

    def _ema(self, value: object, window: object) -> pd.Series:
        return ema(self._to_series(value), self._window(window))

    def _wma(self, value: object, window: object) -> pd.Series:
        return wma(self._to_series(value), self._window(window))

    def _sum(self, value: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(value).rolling(size, min_periods=size).sum()

    def _std(self, value: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(value).rolling(size, min_periods=size).std(ddof=0)

    def _hhv(self, value: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(value).rolling(size, min_periods=size).max()

    def _llv(self, value: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(value).rolling(size, min_periods=size).min()

    def _ref(self, value: object, periods: object) -> pd.Series:
        return self._to_series(value).shift(self._window(periods))

    def _diff(self, value: object, periods: object = 1) -> pd.Series:
        return self._to_series(value).diff(self._window(periods))

    def _roc(self, value: object, periods: object) -> pd.Series:
        return self._to_series(value).pct_change(self._window(periods), fill_method=None) * 100

    def _abs(self, value: object) -> pd.Series:
        return self._to_series(value).abs()

    def _maximum(self, first: object, second: object) -> pd.Series:
        return pd.concat([self._to_series(first), self._to_series(second)], axis=1).max(axis=1)

    def _minimum(self, first: object, second: object) -> pd.Series:
        return pd.concat([self._to_series(first), self._to_series(second)], axis=1).min(axis=1)

    def _if(self, condition: object, when_true: object, when_false: object) -> pd.Series:
        return pd.Series(
            np.where(
                self._to_series(condition).fillna(False).astype(bool),
                self._to_series(when_true),
                self._to_series(when_false),
            ),
            index=self.index,
        )

    def _cross(self, first: object, second: object) -> pd.Series:
        left = self._to_series(first)
        right = self._to_series(second)
        return ((left > right) & (left.shift(1) <= right.shift(1))).astype(float)

    def _log(self, value: object) -> pd.Series:
        series = self._to_series(value)
        return np.log(series.where(series > 0))

    def _sqrt(self, value: object) -> pd.Series:
        series = self._to_series(value)
        return np.sqrt(series.where(series >= 0))

    def _zscore(self, value: object, window: object) -> pd.Series:
        series = self._to_series(value)
        size = self._window(window)
        return safe_div(series - series.rolling(size).mean(), series.rolling(size).std(ddof=0))

    def _ts_rank(self, value: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(value).rolling(size).apply(
            lambda values: float(pd.Series(values).rank(pct=True).iloc[-1] * 100),
            raw=True,
        )

    def _corr(self, first: object, second: object, window: object) -> pd.Series:
        size = self._window(window)
        return self._to_series(first).rolling(size).corr(self._to_series(second))

    def _clip(self, value: object, lower: object, upper: object) -> pd.Series:
        if isinstance(lower, pd.Series) or isinstance(upper, pd.Series):
            raise FormulaError("CLIP 的上下界必须是数字常量")
        return self._to_series(value).clip(lower=float(lower), upper=float(upper))


FORMULA_HELP = {
    "变量": "open, high, low, close, volume, amount, turnover, pct_change, returns, typical, hl2, ohlc4",
    "窗口": "SMA(x,n), EMA(x,n), WMA(x,n), SUM(x,n), STD(x,n), HHV(x,n), LLV(x,n)",
    "序列": "REF(x,n), DIFF(x,n), ROC(x,n), ZSCORE(x,n), TS_RANK(x,n), CORR(x,y,n)",
    "逻辑": "IF(condition,a,b), CROSS(a,b), MAX(a,b), MIN(a,b), ABS(x), CLIP(x,min,max)",
}
