"""风险管理模块 — 仓位计算 + 止损止盈价格计算"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    """仓位计算和风险管理

    核心原则：以单笔最大亏损倒推仓位大小，而不是先定仓位再设止损。
    """

    def __init__(self, config: dict):
        self.max_risk_per_trade = config["risk"]["max_risk_per_trade"]
        self.max_total_position_pct = config["risk"]["max_total_position_pct"]
        self.phase_two_atr_mult = config["stop_loss"]["phase_two_atr_mult"]
        self.phase_three_atr_mult = config["stop_loss"]["phase_three_atr_mult"]
        self.phase_four_atr_mult = config["stop_loss"]["phase_four_atr_mult"]

    def calc_position_size(
        self,
        account_value: float,
        entry_price: float,
        stop_loss_price: float,
        side: str = "long",
    ) -> float:
        """按最大亏损倒推仓位大小

        公式：仓位(张数) = 最大亏损金额 / |入场价 - 止损价|
        """
        max_loss_amount = account_value * self.max_risk_per_trade
        risk_per_unit = abs(entry_price - stop_loss_price)

        if risk_per_unit == 0:
            logger.error("入场价等于止损价，无法计算仓位")
            return 0.0

        position_size = max_loss_amount / risk_per_unit

        logger.info(
            f"仓位计算: 账户={account_value:.2f}, 最大亏损={max_loss_amount:.2f}, "
            f"单位风险={risk_per_unit:.2f}, 建议仓位={position_size:.4f}"
        )

        return position_size

    def calc_position_size_with_leverage(
        self,
        account_value: float,
        entry_price: float,
        stop_loss_price: float,
        leverage: int,
        side: str = "long",
    ) -> dict:
        """计算仓位并返回详细信息"""
        size = self.calc_position_size(account_value, entry_price, stop_loss_price, side)
        notional = size * entry_price
        margin_used = notional / leverage
        max_loss = abs(entry_price - stop_loss_price) * size
        max_loss_pct = max_loss / account_value

        return {
            "size": size,
            "notional": notional,
            "margin_used": margin_used,
            "max_loss": max_loss,
            "max_loss_pct": max_loss_pct,
        }

    def calc_stop_loss_price(
        self,
        vwma_20: float,
        atr: float,
        side: str,
        atr_mult: float,
        recent_range: float = 0.0,
    ) -> float:
        """计算止损价格

        P2-2 动态止损: 仅当近 3K 最大波幅 > 1.5×ATR（真正波动异常）时才放大距离。
        避免常态化拉宽止损。
        """
        base_distance = atr_mult * atr
        if recent_range > atr * 1.5:
            # 波动异常：取 ATR 距离和近 3K 波幅的较大者
            distance = max(base_distance, recent_range)
        else:
            distance = base_distance
        if side == "long":
            return vwma_20 - distance
        else:
            return vwma_20 + distance

    def calc_phase_two_stop_loss(
        self, vwma_20: float, atr: float, side: str, recent_range: float = 0.0
    ) -> float:
        """阶段二止损"""
        return self.calc_stop_loss_price(vwma_20, atr, side, self.phase_two_atr_mult, recent_range)

    def calc_phase_three_stop_loss(
        self,
        entry_kline_low: float,
        entry_kline_high: float,
        prev_kline_low: float,
        prev_kline_high: float,
        side: str,
    ) -> float:
        """阶段三止损：入场 K 线或前一根 K 线的高低点"""
        if side == "long":
            return min(entry_kline_low, prev_kline_low)
        else:
            return max(entry_kline_high, prev_kline_high)

    def calc_phase_four_stop_loss(
        self, vwma_20: float, atr: float, side: str, recent_range: float = 0.0
    ) -> float:
        """阶段四止损"""
        return self.calc_stop_loss_price(vwma_20, atr, side, self.phase_four_atr_mult, recent_range)

    def calc_phase_four_entry_stop_loss(
        self, vwma_20: float, atr: float, side: str, recent_range: float = 0.0
    ) -> float:
        """阶段四独立开仓止损（2.5 ATR，比正常底仓更宽，补偿无浮盈保护）"""
        return self.calc_stop_loss_price(vwma_20, atr, side, 2.5, recent_range)

    @staticmethod
    def calc_take_profit_tracker(
        current_price: float,
        ema_9: float,
        vwma_20: float,
        track_ma: str,
        side: str,
    ) -> dict:
        """计算跟踪止盈触发条件

        Returns:
            {"track_price": float, "triggered": bool, "distance_pct": float}
        """
        track_price = ema_9 if track_ma == "9EMA" else vwma_20
        distance_pct = abs(current_price - track_price) / current_price

        if side == "long":
            triggered = current_price < track_price
        else:
            triggered = current_price > track_price

        return {
            "track_price": track_price,
            "triggered": triggered,
            "distance_pct": distance_pct,
        }

    @staticmethod
    def calc_liquidation_distance(
        entry_price: float, liquidation_price: float, side: str
    ) -> float:
        """计算强平距离百分比"""
        if side == "long":
            return (entry_price - liquidation_price) / entry_price * 100
        else:
            return (liquidation_price - entry_price) / entry_price * 100

    @staticmethod
    def check_stop_loss_before_liquidation(
        stop_loss_price: float,
        liquidation_price: float,
        side: str,
        min_buffer_pct: float = 2.0,
    ) -> bool:
        """确保止损价在强平价之前（留缓冲）"""
        if side == "long":
            return stop_loss_price > liquidation_price * (1 + min_buffer_pct / 100)
        else:
            return stop_loss_price < liquidation_price * (1 - min_buffer_pct / 100)
