"""技术指标计算模块

所有指标均为纯函数，输入 pandas DataFrame，输出 pandas Series。
不依赖外部状态，方便单元测试。
"""

import pandas as pd
import numpy as np
from typing import Tuple


# ============================================================
# EMA（指数移动平均）
# ============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """计算 EMA

    Args:
        series: 价格序列（通常是 close）
        period: 周期（9, 20, etc.）

    Returns:
        EMA 序列
    """
    return series.ewm(span=period, adjust=False).mean()


# ============================================================
# VWMA（成交量加权移动平均）
# ============================================================

def calc_vwma(df: pd.DataFrame, period: int) -> pd.Series:
    """计算 VWMA（Volume-Weighted Moving Average）

    VWMA = Σ(close × volume) / Σ(volume)

    Args:
        df: 包含 close, volume 列的 DataFrame
        period: 周期

    Returns:
        VWMA 序列
    """
    pv = df["close"] * df["volume"]
    vwma = pv.rolling(window=period).sum() / df["volume"].rolling(window=period).sum()
    return vwma


# ============================================================
# ATR（平均真实波幅）
# ============================================================

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 ATR（Average True Range）

    Args:
        df: 包含 high, low, close 列的 DataFrame
        period: 周期

    Returns:
        ATR 序列
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return true_range.ewm(span=period, adjust=False).mean()


# ============================================================
# ADX（平均趋向指数）
# ============================================================

def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 ADX（Average Directional Index）

    步骤：
    1. 计算 True Range
    2. 计算 +DM 和 -DM
    3. 计算 +DI 和 -DI
    4. 计算 DX = |+DI - -DI| / (+DI + -DI) × 100
    5. ADX = DX 的 EMA(period)

    Args:
        df: 包含 high, low, close 列的 DataFrame
        period: 周期（默认 14）

    Returns:
        ADX 序列
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    # True Range
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(span=period, adjust=False).mean()

    # Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    # Smoothed DM
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

    # DX and ADX (guard against +DI + -DI == 0)
    di_sum = plus_di + minus_di
    dx = pd.Series(0.0, index=df.index)
    mask = di_sum > 0
    dx[mask] = 100 * (plus_di[mask] - minus_di[mask]).abs() / di_sum[mask]
    adx = dx.ewm(span=period, adjust=False).mean()

    return adx


def calc_di(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series]:
    """计算 +DI 和 -DI

    Returns:
        (plus_di, minus_di)
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(span=period, adjust=False).mean()

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

    return plus_di, minus_di


# ============================================================
# 布林带
# ============================================================

def calc_bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """计算布林带

    Args:
        df: 包含 close 列的 DataFrame
        period: 周期（默认 20）
        std_dev: 标准差倍数（默认 2.0）

    Returns:
        (middle, upper, lower, bandwidth)
        bandwidth = (upper - lower) / middle
    """
    middle = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = (upper - lower) / middle

    return middle, upper, lower, bandwidth


def is_bb_squeeze_expanding(
    bandwidth: pd.Series,
    lookback: int = 20,
    expand_threshold: float = 0.05,
) -> bool:
    """判断布林带是否从收窄转为张口

    逻辑：
    1. 找过去 lookback 根 K 线的最低带宽（收窄点）
    2. 当前带宽是否比最低点高出 expand_threshold 比例
    3. 最近 2 根 K 线带宽是否在上升

    Args:
        bandwidth: 带宽序列
        lookback: 回看周期
        expand_threshold: 扩张阈值（5%）

    Returns:
        是否正在扩张
    """
    if len(bandwidth) < lookback + 2:
        return False

    recent = bandwidth.iloc[-lookback:]
    min_bandwidth = recent.min()
    current = bandwidth.iloc[-1]
    prev = bandwidth.iloc[-2]

    # 当前带宽 > 最低点 × (1 + threshold)，且最近 2 根在上升
    expanding = current > min_bandwidth * (1 + expand_threshold)
    rising = current > prev

    return expanding and rising


# ============================================================
# EMA 斜率
# ============================================================

def calc_ema_slope(
    ema: pd.Series,
    lookback: int = 3,
    flat_threshold: float = 0.001,  # 0.1%
) -> str:
    """判断 EMA 斜率方向

    取最近 lookback 根 K 线的 EMA 值，线性回归判断方向。
    如果变化率绝对值 < flat_threshold，判定为走平。

    Args:
        ema: EMA 序列
        lookback: 回看 K 线数
        flat_threshold: 走平阈值

    Returns:
        "up" / "down" / "flat"
    """
    if len(ema) < lookback + 1:
        return "flat"

    recent = ema.iloc[-lookback:].dropna()
    if len(recent) < lookback:
        return "flat"

    # 线性回归斜率
    x = np.arange(len(recent))
    y = recent.values
    slope = np.polyfit(x, y, 1)[0]

    # 相对变化率
    change_rate = slope / recent.iloc[-1]

    if abs(change_rate) < flat_threshold:
        return "flat"
    return "up" if slope > 0 else "down"


# ============================================================
# 均线缠绕识别
# ============================================================

def count_ma_crosses(
    ema: pd.Series,
    vwma: pd.Series,
    lookback: int = 5,
) -> int:
    """计算最近 lookback 根 K 线内，EMA 和 VWMA 交叉的次数

    Args:
        ema: 9EMA 序列
        vwma: 20VWMA 序列
        lookback: 回看 K 线数

    Returns:
        交叉次数
    """
    if len(ema) < lookback + 1 or len(vwma) < lookback + 1:
        return 0

    ema_recent = ema.iloc[-(lookback + 1):]
    vwma_recent = vwma.iloc[-(lookback + 1):]

    crosses = 0
    for i in range(1, len(ema_recent)):
        prev_diff = ema_recent.iloc[i - 1] - vwma_recent.iloc[i - 1]
        curr_diff = ema_recent.iloc[i] - vwma_recent.iloc[i]
        if prev_diff * curr_diff < 0:  # 符号变化 = 交叉
            crosses += 1

    return crosses


# ============================================================
# 成交量判断
# ============================================================

def is_volume_surge(
    df: pd.DataFrame,
    lookback: int = 5,
    surge_mult: float = 1.5,
) -> bool:
    """判断当前 K 线是否放量

    Args:
        df: 包含 volume 列的 DataFrame
        lookback: 回看 K 线数
        surge_mult: 放量倍数

    Returns:
        是否放量（当前成交量 > 前 N 根均值的 surge_mult 倍）
    """
    if len(df) < lookback + 1:
        return False

    current_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(lookback + 1):-1].mean()

    return current_vol > avg_vol * surge_mult


def is_volume_shrinking(
    df: pd.DataFrame,
    lookback: int = 5,
) -> bool:
    """判断当前 K 线是否缩量

    Returns:
        是否缩量（当前成交量 < 前 N 根均值）
    """
    if len(df) < lookback + 1:
        return False

    current_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(lookback + 1):-1].mean()

    return current_vol < avg_vol


# ============================================================
# Choppiness Index（盘整指数）
# ============================================================

def calc_choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 Choppiness Index (CI)

    CI = 100 × log10(ΣTR / (highest_high - lowest_low)) / log10(period)

    CI > 61.8 -> 市场极度震荡
    CI < 38.2 -> 市场极度单边趋势
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    sum_tr = true_range.rolling(window=period).sum()
    highest_high = high.rolling(window=period).max()
    lowest_low = low.rolling(window=period).min()
    total_range = highest_high - lowest_low

    log_n = np.log10(period)
    ci = pd.Series(0.0, index=df.index)
    mask = total_range > 0
    ci[mask] = 100 * np.log10(sum_tr[mask] / total_range[mask]) / log_n
    ci = ci.clip(0, 100)

    return ci
