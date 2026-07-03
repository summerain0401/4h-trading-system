"""K 线形态识别模块

所有函数输入单根或两根 K 线的 OHLC 数据，返回 bool。
不依赖外部状态，方便单元测试。
"""

from typing import Optional


# ============================================================
# 辅助函数
# ============================================================

def body_size(open_price: float, close: float) -> float:
    """计算实体大小"""
    return abs(close - open_price)


def upper_shadow(open_price: float, close: float, high: float) -> float:
    """计算上影线长度"""
    return high - max(open_price, close)


def lower_shadow(open_price: float, close: float, low: float) -> float:
    """计算下影线长度"""
    return min(open_price, close) - low


def total_range(high: float, low: float) -> float:
    """计算总波幅"""
    return high - low


def is_bullish(open_price: float, close: float) -> bool:
    """是否阳线"""
    return close > open_price


# ============================================================
# 锤子线 / 倒锤子线
# ============================================================

def is_hammer(
    open_price: float,
    high: float,
    low: float,
    close: float,
    body_ratio: float = 0.3,
    shadow_ratio: float = 2.0,
) -> bool:
    """锤子线（看涨反转信号）

    条件：
    1. 下影线 ≥ 实体 × shadow_ratio（默认 2 倍）
    2. 上影线 ≤ 实体 × body_ratio（上影很短或无）
    3. 总波幅不能太小（> 0）

    通常在下跌趋势末端出现有效。
    """
    body = body_size(open_price, close)
    lower = lower_shadow(open_price, close, low)
    upper = upper_shadow(open_price, close, high)
    total = total_range(high, low)

    if total == 0 or body == 0:
        return False

    if lower < body * shadow_ratio:
        return False

    if upper > body * body_ratio:
        return False

    return True


def is_inverted_hammer(
    open_price: float,
    high: float,
    low: float,
    close: float,
    body_ratio: float = 0.3,
    shadow_ratio: float = 2.0,
) -> bool:
    """倒锤子线（看涨反转信号）

    与锤子线相反：上影线很长，下影线很短。
    """
    body = body_size(open_price, close)
    lower = lower_shadow(open_price, close, low)
    upper = upper_shadow(open_price, close, high)
    total = total_range(high, low)

    if total == 0 or body == 0:
        return False

    if upper < body * shadow_ratio:
        return False

    if lower > body * body_ratio:
        return False

    return True


# ============================================================
# 十字星
# ============================================================

def is_doji(
    open_price: float,
    high: float,
    low: float,
    close: float,
    body_threshold: float = 0.001,
) -> bool:
    """十字星

    条件：
    1. 实体极小（实体 / 开盘价 < body_threshold）
    2. 有一定的上下影线（不是一字）

    缩量十字星在阶段四中作为止跌确认信号。
    """
    if open_price == 0:
        return False

    body = body_size(open_price, close)
    if body / open_price > body_threshold:
        return False

    total = total_range(high, low)
    return total > 0


# ============================================================
# 长下影线 / 长上影线
# ============================================================

def has_long_lower_shadow(
    open_price: float,
    high: float,
    low: float,
    close: float,
    shadow_ratio: float = 0.6,
) -> bool:
    """长下影线（止跌信号）

    下影线占总波幅的比例超过 shadow_ratio。
    """
    total = total_range(high, low)
    if total == 0:
        return False

    lower = lower_shadow(open_price, close, low)
    return (lower / total) >= shadow_ratio


def has_long_upper_shadow(
    open_price: float,
    high: float,
    low: float,
    close: float,
    shadow_ratio: float = 0.6,
) -> bool:
    """长上影线（滞涨信号）"""
    total = total_range(high, low)
    if total == 0:
        return False

    upper = upper_shadow(open_price, close, high)
    return (upper / total) >= shadow_ratio


# ============================================================
# 吞没形态
# ============================================================

def is_bullish_engulfing(
    prev_open: float, prev_high: float, prev_low: float, prev_close: float,
    curr_open: float, curr_high: float, curr_low: float, curr_close: float,
) -> bool:
    """看涨吞没

    条件：
    1. 前一根是阴线
    2. 当前是阳线
    3. 当前实体完全吞没前一根实体
    """
    if is_bullish(prev_open, prev_close):
        return False
    if not is_bullish(curr_open, curr_close):
        return False

    return curr_open < prev_close and curr_close > prev_open


def is_bearish_engulfing(
    prev_open: float, prev_high: float, prev_low: float, prev_close: float,
    curr_open: float, curr_high: float, curr_low: float, curr_close: float,
) -> bool:
    """看跌吞没"""
    if not is_bullish(prev_open, prev_close):
        return False
    if is_bullish(curr_open, curr_close):
        return False

    return curr_open > prev_close and curr_close < prev_open


# ============================================================
# 综合止跌/滞涨确认（阶段四用）
# ============================================================

def is_pullback_stabilizing(
    prev_candle: dict,
    curr_candle: dict,
    direction: str,
) -> bool:
    """判断回调是否出现止跌/滞涨信号

    做多场景（回调止跌）：
    - 长下影线
    - 锤子线
    - 看涨吞没
    - 缩量十字星

    做空场景（反弹滞涨）：
    - 长上影线
    - 倒锤子线
    - 看跌吞没
    - 缩量十字星

    Args:
        prev_candle: 前一根 K 线 {o, h, l, c}
        curr_candle: 当前 K 线 {o, h, l, c}
        direction: "long" / "short"

    Returns:
        是否出现止跌/滞涨信号
    """
    po, ph, pl, pc = prev_candle["o"], prev_candle["h"], prev_candle["l"], prev_candle["c"]
    co, ch, cl, cc = curr_candle["o"], curr_candle["h"], curr_candle["l"], curr_candle["c"]

    if direction == "long":
        return (
            has_long_lower_shadow(co, ch, cl, cc)
            or is_hammer(co, ch, cl, cc)
            or is_bullish_engulfing(po, ph, pl, pc, co, ch, cl, cc)
            or is_doji(co, ch, cl, cc)
        )
    else:
        return (
            has_long_upper_shadow(co, ch, cl, cc)
            or is_inverted_hammer(co, ch, cl, cc)
            or is_bearish_engulfing(po, ph, pl, pc, co, ch, cl, cc)
            or is_doji(co, ch, cl, cc)
        )
