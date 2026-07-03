"""自动交易执行引擎"""

import logging
from typing import Optional

from exchange.hl_client import HLClient
from core.risk_manager import RiskManager
from storage.models import Signal

logger = logging.getLogger(__name__)


class OrderExecutor:
    """信号→下单→止损止盈→持仓管理"""

    def __init__(self, client: HLClient, risk: RiskManager, config: dict):
        self.client = client
        self.risk = risk
        self.symbol = config["symbol"]
        self.leverage = config["leverage"]

    # ============================================================
    # 入场
    # ============================================================

    def execute_entry(self, signal: Signal) -> Optional[dict]:
        """市价入场 + 设置止损"""
        if not self.client.exchange:
            return None

        account_value = self.client.get_account_value()
        if not account_value:
            return None

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=account_value,
            entry_price=signal.entry_price or 0,
            stop_loss_price=signal.stop_loss_price or 0,
            leverage=self.leverage,
            side=signal.direction,
        )
        size = round(pos_info["size"], 5)
        if size <= 0:
            return None

        self.client.update_leverage(self.symbol, self.leverage)

        logger.info(f"入场 {signal.direction.upper()} {size}BTC 保证金{pos_info['margin_used']:.1f} 风控{pos_info['max_loss']:.1f}")

        result = self.client.exchange.market_open(
            self.symbol, signal.direction == "long", size,
        )
        if result.get("status") != "ok":
            logger.error(f"入场失败: {result}")
            return None

        fill = self._parse(result)
        if fill:
            logger.info(f"成交 {fill['size']}BTC @ {fill['price']}")
            if signal.stop_loss_price:
                self.place_stop_loss(signal.direction, signal.stop_loss_price)
        return fill

    def execute_add(self, signal: Signal) -> Optional[dict]:
        """市价加仓 + 更新止损"""
        if not self.client.exchange:
            return None

        account_value = self.client.get_account_value()
        if not account_value:
            return None

        pos_info = self.risk.calc_position_size_with_leverage(
            account_value=account_value,
            entry_price=signal.entry_price or 0,
            stop_loss_price=signal.stop_loss_price or 0,
            leverage=self.leverage,
            side=signal.direction,
        )
        size = round(pos_info["size"], 5)
        if size <= 0:
            return None

        logger.info(f"加仓 {signal.direction.upper()} {size}BTC")
        result = self.client.exchange.market_open(
            self.symbol, signal.direction == "long", size,
        )
        if result.get("status") != "ok":
            logger.error(f"加仓失败: {result}")
            return None

        fill = self._parse(result)
        if fill:
            logger.info(f"成交 {fill['size']}BTC @ {fill['price']}")
            # 市价单成交后旧止损单自动取消，下新止损
            if signal.stop_loss_price:
                self.place_stop_loss(signal.direction, signal.stop_loss_price)
        return fill

    # ============================================================
    # 出场
    # ============================================================

    def execute_exit(self, signal: Signal) -> Optional[dict]:
        """市价全部平仓"""
        if not self.client.exchange:
            return None

        logger.info(f"出场 {signal.direction.upper()} | {signal.reason[:60]}")
        try:
            result = self.client.exchange.market_close(self.symbol)
            if result.get("status") == "ok":
                fill = self._parse(result)
                if fill:
                    logger.info(f"平仓 {fill['size']}BTC @ {fill['price']}")
                return fill
        except Exception as e:
            logger.error(f"平仓失败: {e}")
        return None

    # ============================================================
    # 止损止盈
    # ============================================================

    def place_stop_loss(self, direction: str, price: float) -> Optional[dict]:
        close_side = direction == "short"
        return self.client.place_order(
            coin=self.symbol, is_buy=close_side, size=0,
            price=price, order_type=self.client.make_stop_loss_order_type(price),
            reduce_only=True,
        )

    def place_take_profit(self, direction: str, price: float) -> Optional[dict]:
        close_side = direction == "short"
        return self.client.place_order(
            coin=self.symbol, is_buy=close_side, size=0,
            price=price, order_type=self.client.make_take_profit_order_type(price),
            reduce_only=True,
        )

    def cancel_all_orders(self):
        self.client.cancel_all_orders()

    # ============================================================
    # 辅助
    # ============================================================

    def _parse(self, result: dict) -> Optional[dict]:
        try:
            for s in result["response"]["data"]["statuses"]:
                if "filled" in s:
                    return {"size": s["filled"]["totalSz"], "price": s["filled"]["avgPx"]}
        except Exception:
            pass
        return None

    def sync_position(self) -> Optional[dict]:
        """从交易所同步持仓"""
        if not self.client.account_address:
            return None
        try:
            state = self.client.get_user_state(self.client.account_address)
            if not state:
                return None
            for p in state.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == self.symbol:
                    szi = float(pos.get("szi", 0))
                    if szi != 0:
                        return {
                            "side": "long" if szi > 0 else "short",
                            "size": abs(szi),
                            "entry_price": float(pos.get("entryPx", 0)),
                            "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                            "leverage": int(pos.get("leverage", {}).get("value", 0)),
                        }
        except Exception as e:
            logger.error(f"持仓同步失败: {e}")
        return None
