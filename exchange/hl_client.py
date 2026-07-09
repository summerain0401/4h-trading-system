"""Hyperliquid 客户端 — 数据用原生 REST，交易用官方 SDK"""

import logging, requests
from typing import Optional
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

logger = logging.getLogger(__name__)

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"


class HLClient:
    """Hyperliquid 客户端"""

    def __init__(
        self,
        private_key: Optional[str] = None,       # API 钱包私钥（用于签名）
        account_address: Optional[str] = None,   # 主钱包地址（交易执行账户）
        network: str = "mainnet",
    ):
        self.network = network
        self.base_url = MAINNET_API if network == "mainnet" else TESTNET_API
        self.info_url = f"{self.base_url}/info"

        # 数据查询：原生 REST（已验证可靠快速）
        self.info = Info(base_url=self.base_url, skip_ws=True)

        # 交易执行：官方 SDK（API 钱包签名，主钱包执行）
        self.account_address = account_address
        self.exchange: Optional[Exchange] = None
        if private_key and account_address:
            wallet = Account.from_key(private_key)
            meta = self.info.meta()
            spot_meta = self.info.spot_meta()
            self.exchange = Exchange(
                wallet=wallet,
                base_url=self.base_url,
                account_address=account_address,
                meta=meta,
                spot_meta=spot_meta,
            )
            logger.info(f"交易模块已初始化 (主钱包: {account_address[:10]}...)")
        elif private_key:
            logger.warning("缺少主钱包地址，交易功能不可用")
        else:
            logger.info("只读模式（无交易功能）")

    # ============================================================
    # 市场数据（原生 REST）
    # ============================================================

    def _post_info(self, request: dict) -> Optional[dict]:
        try:
            resp = requests.post(self.info_url, json=request, timeout=60)
            return resp.json() if resp.status_code == 200 else None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None

    def fetch_ohlcv(self, coin="BTC", interval="4h", start_time=None, end_time=None) -> list:
        result = self._post_info({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_time or 0, "endTime": end_time or 9999999999999},
        })
        return result if isinstance(result, list) else []

    def get_all_mids(self) -> dict:
        result = self._post_info({"type": "allMids"})
        return result if isinstance(result, dict) else {}

    def get_user_state(self, address: str) -> Optional[dict]:
        return self._post_info({"type": "clearinghouseState", "user": address})

    def get_open_orders(self, address: str) -> list:
        result = self._post_info({"type": "openOrders", "user": address})
        return result if isinstance(result, list) else []

    def get_meta(self) -> Optional[dict]:
        return self._post_info({"type": "meta"})

    # ============================================================
    # 交易操作（官方 SDK）
    # ============================================================

    def place_order(
        self, coin: str, is_buy: bool, size: float, price: float,
        order_type: dict, reduce_only: bool = False,
    ) -> Optional[dict]:
        if not self.exchange:
            logger.error("交易功能未初始化")
            return None
        try:
            return self.exchange.order(
                name=coin, is_buy=is_buy, sz=size, limit_px=price,
                order_type=order_type, reduce_only=reduce_only,
            )
        except Exception as e:
            logger.error(f"下单失败: {e}")
            return None

    def cancel_order(self, coin: str, order_id: int) -> Optional[dict]:
        if not self.exchange:
            return None
        try:
            return self.exchange.cancel(name=coin, oid=order_id)
        except Exception as e:
            logger.error(f"撤单失败: {e}")
            return None

    def cancel_all_orders(self, coin: Optional[str] = None) -> Optional[dict]:
        """取消所有挂单（可选按币种过滤）

        SDK 的 bulk_cancel 需要显式的 (coin, oid) 列表，
        因此先查询当前挂单再逐一构造撤单请求。
        """
        if not self.exchange or not self.account_address:
            return None
        try:
            open_orders = self.get_open_orders(self.account_address)
            requests_ = [
                {"coin": o["coin"], "oid": o["oid"]}
                for o in open_orders
                if coin is None or o.get("coin") == coin
            ]
            if not requests_:
                return None
            return self.exchange.bulk_cancel(requests_)
        except Exception as e:
            logger.error(f"取消挂单失败: {e}")
            return None

    def update_leverage(self, coin: str, leverage: int) -> Optional[dict]:
        if not self.exchange:
            return None
        try:
            return self.exchange.update_leverage(name=coin, leverage=leverage, is_cross=False)
        except Exception as e:
            logger.error(f"修改杠杆失败: {e}")
            return None

    def get_account_value(self) -> Optional[float]:
        """获取主钱包账户净值"""
        if not self.account_address:
            return None
        try:
            state = self.get_user_state(self.account_address)
            if state:
                return float(state["marginSummary"]["accountValue"])
        except Exception as e:
            logger.error(f"获取账户净值失败: {e}")
        return None

    # ============================================================
    # 便利方法
    # ============================================================

    @staticmethod
    def make_stop_loss_order_type(trigger_price: float) -> dict:
        return {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "sl"}}

    @staticmethod
    def make_take_profit_order_type(trigger_price: float) -> dict:
        return {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "tp"}}

    @staticmethod
    def make_limit_order_type(tif: str = "Gtc") -> dict:
        return {"limit": {"tif": tif}}
