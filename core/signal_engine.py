"""信号检测引擎 — 完整实现手册的四阶段信号逻辑

这是整个系统的核心。所有逻辑严格按照 4H 波段交易手册。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.indicators import (
    calc_ema,
    calc_vwma,
    calc_adx,
    calc_atr,
    calc_bollinger_bands,
    calc_ema_slope,
    is_bb_squeeze_expanding,
    count_ma_crosses,
    is_volume_surge,
    calc_di,
    calc_choppiness_index,
)
from core.patterns import is_pullback_stabilizing
from core.risk_manager import RiskManager
from storage.models import Signal, MarketState, BEIJING_TZ

logger = logging.getLogger(__name__)


class SignalEngine:
    """4H 波段交易信号引擎"""

    def __init__(self, config: dict, risk_manager: RiskManager):
        self.config = config
        self.risk = risk_manager
        self.symbol = config["symbol"]
        self.leverage = config["leverage"]
        self.adx_ranging = config["adx"]["ranging_threshold"]
        self.adx_weak = config["adx"]["weak_trend_threshold"]
        self.phase_four_cooldown = config["cooldown"]["phase_four_min_bars"]
        self.phase_two_reentry_cooldown = config["cooldown"].get("phase_two_reentry_bars", 5)
        self.dir_config = config.get("direction", {})  # V9: 多空不对称参数
        # 总名义仓位上限（账户 × 杠杆 × max_total_position_pct）
        # 账户基数与仓位计算一致使用 10000（Signal.suggested_size 的口径）
        self.max_total_notional = (
            10000 * config["leverage"]
            * config.get("risk", {}).get("max_total_position_pct", 1.0)
        )
        # 加仓批次风险缩放（手册：底仓为主，加仓不应与底仓同额风险）
        self.add_risk_scale = config.get("risk", {}).get("add_risk_scale", 1.0)
        self._last_phase_four_ts: Optional[pd.Timestamp] = None
        self._last_exit_ts: Optional[pd.Timestamp] = None
        self._current_phase: str = "phase_one"
        self._active_position: Optional[dict] = None
        self._phase_four_count: int = 0
        self._consecutive_big_losses: int = 0  # 连续 >2% 亏损计数（用于熔断冷却）

    def _get_dir_params(self, direction: str) -> dict:
        """获取方向相关的不对称参数"""
        default = {"adx_entry": 25, "stop_loss_atr": 2.0, "approaching_atr": 0.8,
                   "trailing_atr": 0.8, "timeout_bars": 12}
        return self.dir_config.get(direction, default)

    @staticmethod
    def _bars_between(ts_then: pd.Timestamp, ts_now: pd.Timestamp) -> float:
        """两个时间戳之间的 4H K 线数

        冷却/超时必须用时间差计算：实盘 DataFrame 是固定长度的滚动窗口，
        行索引 len(df)-1 不随时间增长，用索引差会导致冷却永不解除。
        """
        return (ts_now - ts_then) / pd.Timedelta(hours=4)

    def _cap_size(self, size: float, price: float) -> float:
        """按总名义仓位上限截断批次大小（risk.max_total_position_pct 的执行点）"""
        if price <= 0:
            return size
        used = 0.0
        if self._active_position:
            used = sum(b["price"] * b["size"]
                       for b in self._active_position.get("batches", []))
        remaining = max(self.max_total_notional - used, 0.0)
        return min(size, remaining / price)

    # ============================================================
    # 大周期方向判断（前置）
    # ============================================================

    def check_daily_direction(self, df_daily: pd.DataFrame) -> dict:
        """大周期方向判断

        Returns:
            {"direction": "long"/"short"/"none", "adx": float,
             "is_ranging": bool, "ema9_slope": str, "reason": str}
        """
        if len(df_daily) < 30:
            return {"direction": "none", "adx": 0, "is_ranging": True,
                    "reason": "日线数据不足（需 ≥ 30 根）"}

        adx = calc_adx(df_daily, 14)
        ema9 = calc_ema(df_daily["close"], 9)
        vwma20 = calc_vwma(df_daily, 20)

        current_adx = adx.iloc[-1]
        current_ema9 = ema9.iloc[-1]
        current_vwma20 = vwma20.iloc[-1]
        current_close = df_daily["close"].iloc[-1]
        ema9_slope = calc_ema_slope(ema9)

        # Step 1: ADX 过滤（日线级别）
        is_ranging = current_adx < self.adx_ranging
        if is_ranging:
            return {"direction": "none", "adx": current_adx, "is_ranging": True,
                    "ema9_slope": ema9_slope,
                    "reason": f"ADX(14)={current_adx:.1f} < {self.adx_ranging}，震荡市不做"}

        # Step 2: 均线缠绕检查
        crosses = count_ma_crosses(ema9, vwma20, lookback=5)
        if crosses > 2 and current_adx < self.adx_weak:
            return {"direction": "none", "adx": current_adx, "is_ranging": True,
                    "ema9_slope": ema9_slope,
                    "reason": f"均线缠绕({crosses}次交叉)+ADX边缘({current_adx:.1f})，防守性退让"}

        # Step 3: 方向判定
        price_above_vwma = current_close > current_vwma20

        if price_above_vwma and ema9_slope == "up":
            direction, reason = "long", "价格在 20VWMA 之上 + 9EMA 斜率向上"
        elif not price_above_vwma and ema9_slope == "down":
            direction, reason = "short", "价格在 20VWMA 之下 + 9EMA 斜率向下"
        else:
            direction, reason = "none", f"方向不明：价格{'上' if price_above_vwma else '下'}穿均线+9EMA斜率{ema9_slope}"

        return {"direction": direction, "adx": current_adx, "is_ranging": False,
                "ema9_slope": ema9_slope, "reason": reason}

    # ============================================================
    # 阶段一：方向确认
    # ============================================================

    def check_phase_one(self, df_daily: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
        """阶段一：确认交易方向"""
        daily_dir = self.check_daily_direction(df_daily)
        if daily_dir["direction"] == "none":
            return {"phase": "phase_one", "direction": "none", "passed": False,
                    "reason": daily_dir["reason"]}

        if len(df_4h) < 10:
            return {"phase": "phase_one", "direction": daily_dir["direction"],
                    "passed": False, "reason": "4H 数据不足"}

        ema9_4h = calc_ema(df_4h["close"], 9)
        slope_4h = calc_ema_slope(ema9_4h)

        slope_dir = {"up": "long", "down": "short"}.get(slope_4h, slope_4h)
        if slope_dir != daily_dir["direction"] and slope_4h != "flat":
            return {"phase": "phase_one", "direction": daily_dir["direction"],
                    "passed": False,
                    "reason": f"4H 9EMA斜率({slope_4h})与日线方向({daily_dir['direction']})不一致，等待"}

        self._current_phase = "phase_one"
        return {"phase": "phase_one", "direction": daily_dir["direction"],
                "passed": True,
                "reason": f"方向确认：{daily_dir['direction'].upper()}，ADX={daily_dir['adx']:.1f}"}

    # ============================================================
    # 阶段二：趋势启动 → 建底仓
    # ============================================================

    def check_phase_two(
        self, df_daily: pd.DataFrame, df_4h: pd.DataFrame, has_position: bool
    ) -> Optional[Signal]:
        """阶段二：金叉 + 站稳均线 → 开底仓"""
        if has_position:
            return None

        # 冷却：最低 3 根 K（12h），连续两笔 >2% 亏损 → 熔断 8 根 K（32h）
        if self._last_exit_ts is not None:
            min_cool = 8 if self._consecutive_big_losses >= 2 else 3
            if self._bars_between(self._last_exit_ts, df_4h.index[-1]) < min_cool:
                return None

        # 方向检查
        daily_dir = self.check_daily_direction(df_daily)
        if daily_dir["direction"] == "none":
            return None
        if len(df_4h) < 25:
            return None
        if daily_dir["is_ranging"]:
            return None
        direction = daily_dir["direction"]
        dp = self._get_dir_params(direction)

        # V9: 日线 ADX 门槛按方向不对称（LONG=25, SHORT=30）
        if daily_dir["adx"] < dp["adx_entry"]:
            return None

        ema9_4h = calc_ema(df_4h["close"], 9)
        vwma20_4h = calc_vwma(df_4h, 20)
        atr = calc_atr(df_4h, 14)

        slope_4h = calc_ema_slope(ema9_4h)
        slope_dir = {"up": "long", "down": "short"}.get(slope_4h, slope_4h)
        if slope_dir != direction and slope_4h != "flat":
            return None

        current_close = df_4h["close"].iloc[-1]
        current_ema9 = ema9_4h.iloc[-1]
        current_vwma20 = vwma20_4h.iloc[-1]

        # V8 即时入场：交叉发生的当前K线或即将交叉（EMA9与VWMA20逼近到0.8 ATR内）
        current_atr_val = atr.iloc[-1]
        ema_vwma_gap = abs(current_ema9 - current_vwma20)
        approaching = ema_vwma_gap < current_atr_val * dp["approaching_atr"]  # V9: 不对称阈值

        golden_cross_bar = None
        for i in range(1, min(3, len(ema9_4h))):
            prev_diff = ema9_4h.iloc[-i - 1] - vwma20_4h.iloc[-i - 1]
            curr_diff = ema9_4h.iloc[-i] - vwma20_4h.iloc[-i]
            if direction == "long":
                if prev_diff < 0 and curr_diff > 0:
                    golden_cross_bar = i; break
            else:
                if prev_diff > 0 and curr_diff < 0:
                    golden_cross_bar = i; break

        # 允许入场：交叉在当前K线(i=1) 或 即将交叉(逼近+价格已越过VWMA)
        cross_ready = (golden_cross_bar == 1) or (approaching and (
            (direction == "long" and current_close > current_vwma20) or
            (direction == "short" and current_close < current_vwma20)
        ))
        if not cross_ready:
            return None

        # 确认价格站稳 VWMA（核心支撑/阻力）
        # 放松条件：只要求价格在 VWMA 的正确一侧（不再额外要求也在 9EMA 同侧）
        # 因为金叉后 EMA9 会自然跟随，价格可能在 EMA9 附近波动但 VWMA 是更可靠的趋势边界
        if direction == "long":
            if current_close <= current_vwma20:
                return None
            if current_ema9 <= current_vwma20:
                return None
        else:
            if current_close >= current_vwma20:
                return None
            if current_ema9 >= current_vwma20:
                return None

        entry_price = current_close
        current_atr = atr.iloc[-1]
        # P2-2: 近 3 根 K 实际最大波幅（动态止损用）
        recent_3_range = max(
            df_4h["high"].iloc[-3:].max() - df_4h["low"].iloc[-3:].min(),
            df_4h["high"].iloc[-2:].max() - df_4h["low"].iloc[-2:].min(),
            df_4h["high"].iloc[-1] - df_4h["low"].iloc[-1],
        )
        sl_price = self.risk.calc_stop_loss_price(
            current_vwma20, current_atr, direction, dp["stop_loss_atr"], recent_3_range,
        )

        # 杠杆安全校验：止损必须在强平价之前
        if not self._validate_leverage_safety(entry_price, sl_price, direction):
            logger.warning("止损价过于接近强平价，跳过阶段二信号")
            return None

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=10000, entry_price=entry_price,
            stop_loss_price=sl_price, leverage=self.config["leverage"], side=direction,
        )

        # P1-2: ADX 强度缩放仓位（趋势越强仓位越大）
        current_adx = daily_dir["adx"]
        if current_adx > 35:
            adx_scale = 1.0
        elif current_adx >= 25:
            adx_scale = 0.7
        else:
            adx_scale = 0.4
        scaled_size = self._cap_size(pos_info["size"] * adx_scale, entry_price)
        if scaled_size <= 0:
            return None

        self._current_phase = "phase_two"
        return Signal(
            timestamp=datetime.now(tz=BEIJING_TZ), phase="phase_two",
            direction=direction, action="entry",
            entry_price=entry_price, stop_loss_price=sl_price,
            suggested_size=scaled_size,
            reason=(f"金叉在第 {golden_cross_bar} 根K线前触发, "
                    f"价格站稳均线(EMA9={current_ema9:.1f}, VWMA20={current_vwma20:.1f}), "
                    f"ADX={current_adx:.1f}[{adx_scale:.0%}仓位]"),
        )

    # ============================================================
    # 阶段三：趋势加速 → 追仓
    # ============================================================

    def check_phase_three(
        self, df_daily: pd.DataFrame, df_4h: pd.DataFrame, has_position: bool
    ) -> Optional[Signal]:
        """阶段三：布林带张口 + 放量 → 追仓"""
        if not has_position:
            return None

        daily_dir = self.check_daily_direction(df_daily)
        if daily_dir["direction"] == "none" or daily_dir["adx"] < self.adx_weak:
            return None

        # 阶段三额外要求：ADX ≥ adx_entry + 5（趋势加速只在强趋势中追仓）
        dp = self._get_dir_params(daily_dir["direction"])
        if daily_dir["adx"] < dp["adx_entry"] + 5:
            return None

        if len(df_4h) < 25:
            return None

        direction = daily_dir["direction"]
        _, bb_upper, bb_lower, bandwidth = calc_bollinger_bands(df_4h, 20, 2.0)

        # 阶段三收紧参数：lookback=12（3天4H），expand_threshold=5%（只抓真正突破）
        if not is_bb_squeeze_expanding(bandwidth, lookback=12, expand_threshold=0.05):
            return None

        # 放量收紧：1.5x 才确认趋势加速有资金跟进
        if not is_volume_surge(df_4h, lookback=5, surge_mult=1.5):
            return None

        current_close = df_4h["close"].iloc[-1]

        # 张口方向确认
        if direction == "long":
            if current_close < bb_upper.iloc[-1] * 0.98:
                return None
        else:
            if current_close > bb_lower.iloc[-1] * 1.02:
                return None

        entry_price = current_close
        prev = df_4h.iloc[-2]
        curr = df_4h.iloc[-1]

        sl_price = self.risk.calc_phase_three_stop_loss(
            curr["low"], curr["high"], prev["low"], prev["high"], direction,
        )

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=10000, entry_price=entry_price,
            stop_loss_price=sl_price, leverage=self.config["leverage"], side=direction,
        )
        capped_size = self._cap_size(pos_info["size"] * self.add_risk_scale, entry_price)
        if capped_size <= 0:
            return None

        self._current_phase = "phase_three"
        return Signal(
            timestamp=datetime.now(tz=BEIJING_TZ), phase="phase_three",
            direction=direction, action="add",
            entry_price=entry_price, stop_loss_price=sl_price,
            suggested_size=capped_size,
            reason=(f"布林带张口+放量, 带宽={bandwidth.iloc[-1]:.4f}, "
                    f"ADX={daily_dir['adx']:.1f}, {direction.upper()}方向加速"),
        )

    # ============================================================
    # 阶段四：趋势回调 → 加主仓
    # ============================================================

    def check_phase_four(
        self, df_daily: pd.DataFrame, df_4h: pd.DataFrame, has_position: bool
    ) -> Optional[Signal]:
        """阶段四：回调到 20VWMA + 止跌信号 → 加主仓

        优化：
        - 冷却期 5 根 K 线（20小时）
        - 价格必须正在接近 VWMA（而非早已在附近横盘）
        - 距离 VWMA 需 > 0.3 ATR（真正回调而非小幅波动）
        """
        if not has_position:
            return None

        daily_dir = self.check_daily_direction(df_daily)
        if daily_dir["direction"] == "none" or daily_dir["is_ranging"]:
            return None

        # V8: ADX < 25 不允许阶段四加仓（趋势弱时回调很可能是反转）
        if daily_dir["adx"] < 25:
            return None

        if len(df_4h) < 25:
            return None

        direction = daily_dir["direction"]
        vwma20 = calc_vwma(df_4h, 20)
        atr = calc_atr(df_4h, 14)

        current_vwma20 = vwma20.iloc[-1]
        current_atr = atr.iloc[-1]
        current_close = df_4h["close"].iloc[-1]

        # 冷却检查
        current_ts = df_4h.index[-1]
        if (self._last_phase_four_ts is not None
                and self._bars_between(self._last_phase_four_ts, current_ts) < self.phase_four_cooldown):
            return None

        # 距离检查：必须在 0.3~1.5 ATR 范围内（太远=没回调到位，太近=早就在均线附近横盘）
        distance = abs(current_close - current_vwma20)
        if distance > current_atr * 1.5:
            return None
        if distance < current_atr * 0.3:
            return None  # 太近，不是真正的回调

        # 方向检查：做多时价格必须从上方回调下来（前一根 K 线收盘 > VWMA）
        prev_close = df_4h["close"].iloc[-2]
        if direction == "long" and prev_close <= current_vwma20:
            return None  # 不是回调（已在均线下方）
        if direction == "short" and prev_close >= current_vwma20:
            return None

        # 止跌/滞涨确认（ADX ≥ 30 强趋势豁免，钝刀下跌无需经典形态）
        if daily_dir["adx"] < 30:
            prev_kline = {"o": df_4h["open"].iloc[-2], "h": df_4h["high"].iloc[-2],
                           "l": df_4h["low"].iloc[-2], "c": df_4h["close"].iloc[-2]}
            curr_kline = {"o": df_4h["open"].iloc[-1], "h": df_4h["high"].iloc[-1],
                           "l": df_4h["low"].iloc[-1], "c": df_4h["close"].iloc[-1]}
            if not is_pullback_stabilizing(prev_kline, curr_kline, direction):
                return None

        entry_price = current_close
        # P2-2: 近 3 根 K 实际最大波幅
        recent_3_range = max(
            df_4h["high"].iloc[-3:].max() - df_4h["low"].iloc[-3:].min(),
            df_4h["high"].iloc[-1] - df_4h["low"].iloc[-1],
        )
        sl_price = self.risk.calc_phase_four_stop_loss(
            current_vwma20, current_atr, direction, recent_3_range,
        )

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=10000, entry_price=entry_price,
            stop_loss_price=sl_price, leverage=self.config["leverage"], side=direction,
        )
        capped_size = self._cap_size(pos_info["size"] * self.add_risk_scale, entry_price)
        if capped_size <= 0:
            return None

        self._last_phase_four_ts = current_ts
        self._current_phase = "phase_four"

        dist_pct = distance / current_close * 100
        stable_note = "" if daily_dir["adx"] >= 30 else "出现止跌/滞涨确认信号, "
        return Signal(
            timestamp=datetime.now(tz=BEIJING_TZ), phase="phase_four",
            direction=direction, action="add",
            entry_price=entry_price, stop_loss_price=sl_price,
            suggested_size=capped_size,
            reason=(f"价格回调靠近20VWMA(距离={dist_pct:.1f}%), "
                    f"{stable_note}20VWMA={current_vwma20:.1f}"),
        )

    # ============================================================
    # 阶段四独立开仓：错过阶段二时的补救入口
    # ============================================================

    def check_phase_four_entry(
        self, df_daily: pd.DataFrame, df_4h: pd.DataFrame,
    ) -> Optional[Signal]:
        """阶段四独立开仓：回调到 20VWMA + 止跌信号 → 开底仓

        与阶段四加仓共用回调检测逻辑，区别：
        - 仓位：标准 × ADX系数 × 50%（半仓，补偿无浮盈保护）
        - 止损：VWMA + max(2.5 ATR, 近3K波幅)（更宽）
        - 计入阶段四 3 次上限（开仓用掉 1 次）
        - 出场冷却与阶段二相同
        """
        # 冷却（同阶段二）
        if self._last_exit_ts is not None:
            min_cool = 8 if self._consecutive_big_losses >= 2 else 3
            if self._bars_between(self._last_exit_ts, df_4h.index[-1]) < min_cool:
                return None

        daily_dir = self.check_daily_direction(df_daily)
        if daily_dir["direction"] == "none" or daily_dir["is_ranging"]:
            return None

        direction = daily_dir["direction"]
        dp = self._get_dir_params(direction)

        # 方向不对称 ADX 门槛
        if daily_dir["adx"] < dp["adx_entry"]:
            return None

        if len(df_4h) < 25:
            return None

        vwma20 = calc_vwma(df_4h, 20)
        atr = calc_atr(df_4h, 14)

        current_vwma20 = vwma20.iloc[-1]
        current_atr = atr.iloc[-1]
        current_close = df_4h["close"].iloc[-1]

        # 冷却检查（使用阶段四冷却计数）
        current_ts = df_4h.index[-1]
        if (self._last_phase_four_ts is not None
                and self._bars_between(self._last_phase_four_ts, current_ts) < self.phase_four_cooldown):
            return None

        # 距离检查：必须在 0.3~1.5 ATR 范围内
        distance = abs(current_close - current_vwma20)
        if distance > current_atr * 1.5:
            return None
        if distance < current_atr * 0.3:
            return None

        # 方向检查：前一根 K 线收盘在 VWMA 正确一侧
        prev_close = df_4h["close"].iloc[-2]
        if direction == "long" and prev_close <= current_vwma20:
            return None
        if direction == "short" and prev_close >= current_vwma20:
            return None

        # 止跌/滞涨确认（ADX ≥ 30 强趋势豁免，钝刀下跌无需经典形态）
        if daily_dir["adx"] < 30:
            prev_kline = {"o": df_4h["open"].iloc[-2], "h": df_4h["high"].iloc[-2],
                           "l": df_4h["low"].iloc[-2], "c": df_4h["close"].iloc[-2]}
            curr_kline = {"o": df_4h["open"].iloc[-1], "h": df_4h["high"].iloc[-1],
                           "l": df_4h["low"].iloc[-1], "c": df_4h["close"].iloc[-1]}
            if not is_pullback_stabilizing(prev_kline, curr_kline, direction):
                return None

        entry_price = current_close
        recent_3_range = max(
            df_4h["high"].iloc[-3:].max() - df_4h["low"].iloc[-3:].min(),
            df_4h["high"].iloc[-1] - df_4h["low"].iloc[-1],
        )
        sl_price = self.risk.calc_stop_loss_price(
            current_vwma20, current_atr, direction, dp["stop_loss_atr"] + 0.5, recent_3_range,
        )

        # 杠杆安全校验
        if not self._validate_leverage_safety(entry_price, sl_price, direction):
            logger.warning("止损价过于接近强平价，跳过阶段四独立开仓")
            return None

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=10000, entry_price=entry_price,
            stop_loss_price=sl_price, leverage=self.config["leverage"], side=direction,
        )

        # ADX 强度缩放 × 半仓
        current_adx = daily_dir["adx"]
        if current_adx > 35:
            adx_scale = 1.0
        elif current_adx >= 25:
            adx_scale = 0.7
        else:
            adx_scale = 0.4
        scaled_size = self._cap_size(pos_info["size"] * adx_scale * 0.5, entry_price)  # 半仓
        if scaled_size <= 0:
            return None

        self._last_phase_four_ts = current_ts
        self._current_phase = "phase_four"
        self._phase_four_count = 1  # 开仓计入 3 次上限

        dist_pct = distance / current_close * 100
        stable_note = "" if current_adx >= 30 else "出现止跌/滞涨确认信号, "
        return Signal(
            timestamp=datetime.now(tz=BEIJING_TZ), phase="phase_four",
            direction=direction, action="entry",
            entry_price=entry_price, stop_loss_price=sl_price,
            suggested_size=scaled_size,
            reason=(f"[独立开仓] 价格回调靠近20VWMA(距离={dist_pct:.1f}%), "
                    f"{stable_note}20VWMA={current_vwma20:.1f}, "
                    f"ADX={current_adx:.1f}[{adx_scale:.0%}仓位×0.5], 止损={dp['stop_loss_atr']+0.5:.1f}ATR"),
        )

    # ============================================================
    # 止损止盈监控
    # ============================================================

    def check_stop_loss_take_profit(
        self, df_4h: pd.DataFrame, position: dict, current_price: float,
    ) -> Optional[Signal]:
        """检查已有仓位是否触及止损或止盈

        止损：无缓冲，触及即走。
        跟踪止盈：0.8 ATR 缓冲 + 最小保护距离（入场后价格须走 ≥1 ATR 才激活）。
        ADX 衰减：ADX 从峰值降 >40% 且 ADX < 25 → 跟踪均线收紧到 9EMA。
        """
        phase = position.get("current_phase", "phase_two")
        direction = position.get("side", "long")
        sl_price = position.get("stop_loss_price")
        entry_price = position.get("entry_price")
        entry_adx = position.get("entry_adx")  # 入场时的 ADX 峰值

        ema9 = calc_ema(df_4h["close"], 9).iloc[-1]
        vwma20 = calc_vwma(df_4h, 20).iloc[-1]
        atr = calc_atr(df_4h, 14).iloc[-1]
        dp = self._get_dir_params(direction)
        atr_buffer = atr * dp["trailing_atr"]  # V9: 不对称缓冲
        current_adx = calc_adx(df_4h, 14).iloc[-1]

        # 止损检查（无缓冲，触及即走）
        if sl_price:
            if direction == "long" and current_price <= sl_price:
                return Signal(
                    timestamp=datetime.now(tz=BEIJING_TZ), phase=phase,
                    direction=direction, action="exit",
                    reason=f"止损触发: 当前价{current_price:.1f} ≤ 止损价{sl_price:.1f}",
                )
            elif direction == "short" and current_price >= sl_price:
                return Signal(
                    timestamp=datetime.now(tz=BEIJING_TZ), phase=phase,
                    direction=direction, action="exit",
                    reason=f"止损触发: 当前价{current_price:.1f} ≥ 止损价{sl_price:.1f}",
                )

        # P1-1: ADX 衰减检测——ADX 从峰值降 >40% 且 ADX < 25（绝对值）才算衰减
        adx_declining = False
        if entry_adx and current_adx > 0:
            adx_declining = current_adx < entry_adx * 0.6 and current_adx < 25

        # P0-2: 最小保护距离——价格须走 ≥1 ATR 才激活跟踪止盈
        min_protection_atr = 1.0
        if entry_price and atr > 0:
            profit_distance = abs(current_price - entry_price)
            protection_active = profit_distance >= atr * min_protection_atr
        else:
            protection_active = True  # 没有入场价时不限制

        # 利润保护（V9）：浮盈越大，跟踪越紧
        profit_pct = 0.0
        if entry_price and entry_price > 0:
            if direction == "long":
                profit_pct = (current_price - entry_price) / entry_price
            else:
                profit_pct = (entry_price - current_price) / entry_price

        profit_tier = ""
        if profit_pct > 0.10:
            track_ma = "9EMA(暴利锁仓)"
            track_price = ema9
            effective_buffer = 0.0
            profit_tier = "[暴利>10%]"
        elif profit_pct > 0.05:
            track_ma = "9EMA(利润保护)"
            track_price = ema9
            effective_buffer = atr * 0.5
            profit_tier = "[利润>5%]"
        else:
            if phase == "phase_three":
                track_ma = "9EMA"
                track_price = ema9
            else:
                track_ma = "20VWMA"
                track_price = vwma20

            protection_buffer = atr * 0.5
            effective_buffer = atr_buffer if protection_active else protection_buffer

        if direction == "long":
            triggered = current_price < track_price - effective_buffer
        else:
            triggered = current_price > track_price + effective_buffer

        if triggered:
            distance = abs(current_price - track_price)
            decay_note = " [ADX衰减]" if adx_declining else ""
            protect_note = " [保护期]" if not protection_active else ""
            return Signal(
                timestamp=datetime.now(tz=BEIJING_TZ), phase=phase,
                direction=direction, action="exit",
                take_profit_price=track_price,
                reason=(f"跟踪止盈触发{profit_tier}: 收盘价突破{track_ma}"
                        f"({track_price:.1f})+{effective_buffer:.1f}缓冲{decay_note}{protect_note}, 距离={distance:.1f}"),
            )

        # V9-超时保护：按方向不对称（LONG=12K, SHORT=8K）
        entry_ts = position.get("entry_ts")
        if entry_ts is not None and current_price and entry_price:
            bars_held = self._bars_between(entry_ts, df_4h.index[-1])
            is_losing = (direction == "long" and current_price < entry_price) or (direction == "short" and current_price > entry_price)
            if bars_held >= dp["timeout_bars"] and is_losing:
                return Signal(
                    timestamp=datetime.now(tz=BEIJING_TZ), phase=phase,
                    direction=direction, action="exit",
                    reason=f"持仓超时({bars_held:.0f}根K线/48h+)仍亏损，主动离场",
                )

        # P2-1: 趋势衰减出场——ADX 降 >40% 且 ADX < 25 + 4 根 K 未创新高/低 → 提前离场
        if adx_declining and protection_active and len(df_4h) >= 5:
            recent_4 = df_4h.iloc[-5:-1]  # 前 4 根（不含当前）
            if direction == "long":
                prev_high = recent_4["high"].max()
                current_stalling = df_4h["high"].iloc[-1] <= prev_high
            else:
                prev_low = recent_4["low"].min()
                current_stalling = df_4h["low"].iloc[-1] >= prev_low

            if current_stalling:
                return Signal(
                    timestamp=datetime.now(tz=BEIJING_TZ), phase=phase,
                    direction=direction, action="exit",
                    reason=(f"趋势衰减出场: ADX从{entry_adx:.1f}降至{current_adx:.1f}"
                            f"({(1-current_adx/entry_adx)*100:.0f}%), 4K未创新{'高' if direction=='long' else '低'}"),
                )

        return None

    # ============================================================
    # 批次止损（回测保真：模拟交易所按批次挂的止损单在 K 线内触发）
    # ============================================================

    def check_batch_stops(self, df_4h: pd.DataFrame) -> tuple[list[dict], bool]:
        """逐批次检查盘中止损：触发的批次单独平仓，其余批次继续持有

        每个批次（底仓/各次加仓）带自己的止损价，对应实盘中
        每次成交后挂出的独立 reduce-only 止损单。加仓批次的紧止损
        触发时只砍掉该批次，底仓继续跟踪趋势——避免"方向对了、
        加仓拖累整笔亏损"的出场语义缺陷。

        成交价按止损价计，跳空时用 K 线开盘价（更差方向）修正。
        全部批次止损 → 执行与正常出场一致的状态簿记（熔断计数、冷却）。

        Returns:
            (events, position_closed)
            events: [{price, size, stop, phase, is_base, fill_px}, ...]
        """
        pos = self._active_position
        if not pos or len(df_4h) == 0:
            return [], False
        batches = pos.get("batches")
        if not batches:
            if not pos.get("stop_loss_price"):
                return [], False
            # 兼容：无批次信息时退化为单一止损
            batches = [{"price": pos.get("entry_price"), "size": 0.0,
                        "stop": pos["stop_loss_price"],
                        "phase": pos.get("current_phase", "phase_two"), "is_base": True}]
            pos["batches"] = batches

        side = pos.get("side", "long")
        bar_high = df_4h["high"].iloc[-1]
        bar_low = df_4h["low"].iloc[-1]
        bar_open = df_4h["open"].iloc[-1]

        events, remaining = [], []
        for b in batches:
            stop = b.get("stop")
            hit = stop is not None and (
                (side == "long" and bar_low <= stop)
                or (side == "short" and bar_high >= stop)
            )
            if hit:
                fill = min(stop, bar_open) if side == "long" else max(stop, bar_open)
                events.append({**b, "fill_px": fill})
            else:
                remaining.append(b)

        if not events:
            return [], False

        pos["batches"] = remaining
        closed = len(remaining) == 0
        if closed:
            # 熔断计数按底仓入场价 vs 底仓成交价（与正常出场路径口径一致）
            entry = pos.get("entry_price")
            base_ev = next((e for e in events if e.get("is_base")), events[-1])
            if entry:
                fill = base_ev["fill_px"]
                loss_pct = (entry - fill) / entry if side == "long" else (fill - entry) / entry
                if loss_pct > 0.02:
                    self._consecutive_big_losses += 1
                else:
                    self._consecutive_big_losses = 0
            self._active_position = None
            self._phase_four_count = 0
            self._last_exit_ts = df_4h.index[-1]
        else:
            # 剩余批次中最早的一批作为整体持仓的硬止损口径
            pos["stop_loss_price"] = remaining[0].get("stop")

        return events, closed

    # ============================================================
    # 4H 级别震荡检测
    # ============================================================

    def _is_4h_ranging(self, df_4h: pd.DataFrame) -> bool:
        """4H 级别震荡检测 — BB+DI收敛 + Choppiness Index

        在 4H 上检测更灵敏，避免日线 CI 过于敏感的问题。
        返回 True 表示当前 4H 市场处于震荡/盘整，应暂停交易。
        """
        if len(df_4h) < 21:
            return False

        # 条件 1：BB 带宽收缩 + DI 双弱（暴跌后横盘、趋势末端收窄）
        _, _, _, bb_bw = calc_bollinger_bands(df_4h, 20, 2.0)
        plus_di, minus_di = calc_di(df_4h, 14)
        current_bb_bw = bb_bw.iloc[-1]
        current_plus_di = plus_di.iloc[-1]
        current_minus_di = minus_di.iloc[-1]
        if current_bb_bw < 0.025 and current_plus_di < 15 and current_minus_di < 15:
            return True

        # 条件 2：Choppiness Index > 65（4H 级别需更高阈值，61.8 为日线标准）
        if len(df_4h) >= 15:
            ci = calc_choppiness_index(df_4h, 14)
            if ci.iloc[-1] > 65:
                return True

        return False

    # ============================================================
    # 综合运行
    # ============================================================

    def run_all_checks(
        self,
        df_daily: pd.DataFrame,
        df_4h: pd.DataFrame,
        has_position: bool,
        position_info: Optional[dict] = None,
        current_price: Optional[float] = None,
    ) -> list[Signal]:
        """运行所有阶段检查，返回触发的信号列表"""
        signals = []

        # 用内部状态或显式传入的 position_info
        pos = position_info or self._active_position
        if not current_price and len(df_4h) > 0:
            current_price = df_4h["close"].iloc[-1]

        daily_dir = self.check_daily_direction(df_daily)

        # 已有持仓 → 先查止损止盈
        if has_position and pos and current_price:
            exit_signal = self.check_stop_loss_take_profit(
                df_4h, pos, current_price,
            )
            if exit_signal:
                signals.append(exit_signal)
                # 熔断冷却：连续两笔 >2% 亏损 → 触发 8 根 K 线冷却
                if pos and pos.get("entry_price") and current_price:
                    if pos["side"] == "long":
                        loss_pct = (pos["entry_price"] - current_price) / pos["entry_price"]
                    else:
                        loss_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
                    if loss_pct > 0.02:
                        self._consecutive_big_losses += 1
                    else:
                        self._consecutive_big_losses = 0  # 盈利或小亏 → 重置
                self._active_position = None
                self._phase_four_count = 0
                self._last_exit_ts = df_4h.index[-1]
                return signals

        # 震荡市 → 不交易
        if daily_dir["is_ranging"]:
            return signals

        # 4H 级别震荡检测（BB+DI收敛 / Choppiness Index）
        if self._is_4h_ranging(df_4h):
            return signals

        # 方向变化 → 重置计数器（持仓字典的方向键是 side）
        if pos and pos.get("side") != daily_dir["direction"]:
            self._phase_four_count = 0

        # 阶段一
        p1 = self.check_phase_one(df_daily, df_4h)
        if not p1["passed"]:
            return signals

        # 阶段二（无仓位，优先级最高：金叉/死叉入场）
        if not has_position:
            p2 = self.check_phase_two(df_daily, df_4h, has_position)
            if p2:
                signals.append(p2)
                entry_adx_4h = calc_adx(df_4h, 14).iloc[-1] if len(df_4h) >= 15 else 0
                self._active_position = {
                    "current_phase": "phase_two",
                    "side": p2.direction,
                    "stop_loss_price": p2.stop_loss_price,
                    "entry_price": p2.entry_price,
                    "entry_adx": entry_adx_4h,
                    "entry_ts": df_4h.index[-1],
                    "batches": [{"price": p2.entry_price, "size": p2.suggested_size,
                                 "stop": p2.stop_loss_price, "phase": "phase_two",
                                 "is_base": True}],
                }
                self._phase_four_count = 0
                return signals

            # 阶段四独立开仓：错过阶段二时的补救入口
            p4_entry = self.check_phase_four_entry(df_daily, df_4h)
            if p4_entry:
                signals.append(p4_entry)
                entry_adx_4h = calc_adx(df_4h, 14).iloc[-1] if len(df_4h) >= 15 else 0
                self._active_position = {
                    "current_phase": "phase_two",  # 后续止损止盈按阶段二处理
                    "side": p4_entry.direction,
                    "stop_loss_price": p4_entry.stop_loss_price,
                    "entry_price": p4_entry.entry_price,
                    "entry_adx": entry_adx_4h,
                    "entry_ts": df_4h.index[-1],
                    "batches": [{"price": p4_entry.entry_price, "size": p4_entry.suggested_size,
                                 "stop": p4_entry.stop_loss_price, "phase": "phase_four",
                                 "is_base": True}],
                }
                return signals

        # 阶段三（仅 SHORT 追仓，LONG 慢涨趋势追仓=追在局部高点）
        dp_guard = self._get_dir_params(daily_dir["direction"])
        if has_position and daily_dir["direction"] == "short" and daily_dir["adx"] >= dp_guard["adx_entry"]:
            p3 = self.check_phase_three(df_daily, df_4h, has_position)
            if p3:
                signals.append(p3)
                target = self._active_position or pos
                if target is not None:
                    target.setdefault("batches", []).append({
                        "price": p3.entry_price, "size": p3.suggested_size,
                        "stop": p3.stop_loss_price, "phase": "phase_three",
                        "is_base": False,
                    })
                # SHORT: 收紧整体止损（下跌反弹暴烈需保护）——全部批次止损收到阶段三止损价
                if p3.stop_loss_price and target is not None and p3.direction == "short":
                    for b in target.get("batches", []):
                        if b.get("stop") is None or p3.stop_loss_price < b["stop"]:
                            b["stop"] = p3.stop_loss_price
                    target["stop_loss_price"] = p3.stop_loss_price

        # 阶段四（有仓位，限制每趋势最多 3 次）
        max_phase_four = 3
        if has_position and self._phase_four_count < max_phase_four:
            p4 = self.check_phase_four(df_daily, df_4h, has_position)
            if p4:
                signals.append(p4)
                target = self._active_position or pos
                if target is not None:
                    target.setdefault("batches", []).append({
                        "price": p4.entry_price, "size": p4.suggested_size,
                        "stop": p4.stop_loss_price, "phase": "phase_four",
                        "is_base": False,
                    })
                self._phase_four_count += 1

        return signals

    def _validate_leverage_safety(
        self, entry_price: float, stop_loss_price: float, direction: str
    ) -> bool:
        """验证止损价在强平价之前（杠杆安全边界）

        估算强平价格：
        - 逐仓模式下: 强平价 ≈ entry * (1 - 1/leverage + maintenance_margin_rate)
        - 此处用保守近似: 强平价 ≈ entry * (1 - 1.1/leverage)

        要求止损价距离强平价至少 5% 的缓冲空间。
        """
        if direction == "long":
            liq_price = entry_price * (1 - 1.1 / self.leverage)
            # 止损必须远在强平价之上
            min_sl = liq_price * 1.05
            safe = stop_loss_price > min_sl
        else:
            liq_price = entry_price * (1 + 1.1 / self.leverage)
            max_sl = liq_price * 0.95
            safe = stop_loss_price < max_sl

        if not safe:
            dist_sl = abs(entry_price - stop_loss_price) / entry_price * 100
            dist_liq = abs(entry_price - liq_price) / entry_price * 100
            logger.warning(
                f"杠杆安全校验失败: 止损距={dist_sl:.1f}%, "
                f"强平距≈{dist_liq:.1f}%({self.leverage}x杠杆), 缓冲不足"
            )
        return safe

    def build_market_state(
        self, df_daily: pd.DataFrame, df_4h: pd.DataFrame
    ) -> MarketState:
        """构建当前市场状态快照"""
        daily_dir = self.check_daily_direction(df_daily)
        adx_4h = calc_adx(df_4h, 14).iloc[-1] if len(df_4h) >= 15 else 0
        ema9_4h = calc_ema(df_4h["close"], 9).iloc[-1] if len(df_4h) >= 10 else 0
        vwma20_4h = calc_vwma(df_4h, 20).iloc[-1] if len(df_4h) >= 21 else 0
        ema9_daily = calc_ema(df_daily["close"], 9).iloc[-1] if len(df_daily) >= 10 else 0
        vwma20_daily = calc_vwma(df_daily, 20).iloc[-1] if len(df_daily) >= 21 else 0
        slope_4h = calc_ema_slope(calc_ema(df_4h["close"], 9))
        _, _, _, bandwidth = calc_bollinger_bands(df_4h, 20, 2.0)
        bb_exp = is_bb_squeeze_expanding(bandwidth) if len(bandwidth) >= 22 else False

        return MarketState(
            timestamp=datetime.now(tz=BEIJING_TZ), symbol=self.symbol,
            daily_direction=daily_dir["direction"],
            adx_4h=adx_4h, adx_daily=daily_dir["adx"],
            ema9_4h=ema9_4h, vwma20_4h=vwma20_4h,
            ema9_daily=ema9_daily, vwma20_daily=vwma20_daily,
            ema9_slope_daily=daily_dir.get("ema9_slope", "flat"),
            ema9_slope_4h=slope_4h,
            bb_bandwidth=bandwidth.iloc[-1] if len(bandwidth) > 0 else 0,
            bb_expanding=bb_exp, is_ranging=daily_dir["is_ranging"],
            current_phase=self._current_phase,
        )
