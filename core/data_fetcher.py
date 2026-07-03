"""数据获取层 — 拉取历史 K 线数据"""

import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from exchange.hl_client import HLClient
from storage.db import insert_klines, get_klines
from storage.models import Kline

logger = logging.getLogger(__name__)


class DataFetcher:
    """负责 BTC 的历史和实时 K 线数据获取"""

    def __init__(self, client: HLClient, config: dict):
        self.client = client
        self.symbol = config["symbol"]
        self.kline_interval = config["data"]["kline_interval"]
        self.daily_interval = config["data"]["daily_interval"]
        self.historical_days = config["data"]["historical_days"]

    async def fetch_and_store_historical(self) -> dict[str, pd.DataFrame]:
        """拉取历史数据并存入 SQLite

        Returns:
            {"4h": df_4h, "1d": df_daily}
        """
        end_time = int(time.time() * 1000)
        start_time = int(
            (datetime.now(timezone.utc) - timedelta(days=self.historical_days)).timestamp() * 1000
        )

        logger.info(f"拉取 {self.symbol} {self.historical_days} 天历史数据...")

        raw_4h = self.client.fetch_ohlcv(
            coin=self.symbol,
            interval=self.kline_interval,
            start_time=start_time,
            end_time=end_time,
        )
        raw_daily = self.client.fetch_ohlcv(
            coin=self.symbol,
            interval=self.daily_interval,
            start_time=start_time,
            end_time=end_time,
        )

        klines_4h = [Kline.from_api_candle(c, self.symbol, self.kline_interval) for c in raw_4h]
        klines_daily = [Kline.from_api_candle(c, self.symbol, self.daily_interval) for c in raw_daily]

        n_4h = insert_klines(klines_4h)
        n_daily = insert_klines(klines_daily)

        logger.info(f"4H: {len(raw_4h)} 拉取, {n_4h} 新增 | 日线: {len(raw_daily)} 拉取, {n_daily} 新增")

        df_4h = await self.load_klines_df(self.kline_interval)
        df_daily = await self.load_klines_df(self.daily_interval)

        return {"4h": df_4h, "1d": df_daily}

    async def load_klines_df(self, interval: str) -> pd.DataFrame:
        """从数据库加载 K 线并转为 DataFrame"""
        klines = get_klines(self.symbol, interval, limit=500)
        if not klines:
            return pd.DataFrame(
                columns=["open_time", "open", "high", "low", "close", "volume"]
            )
        data = []
        for k in klines:
            data.append({
                "open_time": k.open_time,
                "open": k.open, "high": k.high, "low": k.low,
                "close": k.close, "volume": k.volume,
            })
        df = pd.DataFrame(data)
        df.set_index("open_time", inplace=True)
        df.sort_index(inplace=True)
        return df

    async def get_latest_kline(self, interval: str) -> Optional[dict]:
        """获取最新一根 K 线"""
        klines = get_klines(self.symbol, interval, limit=1)
        if not klines:
            return None
        k = klines[-1]
        return {"o": k.open, "h": k.high, "l": k.low, "c": k.close, "v": k.volume}

    async def get_current_price(self) -> Optional[float]:
        """获取当前中间价"""
        try:
            mids = self.client.get_all_mids()
            if mids and self.symbol in mids:
                return float(mids[self.symbol])
        except Exception as e:
            logger.error(f"获取当前价格失败: {e}")
        return None

    async def refresh_klines(self) -> dict[str, pd.DataFrame]:
        """增量刷新"""
        end_time = int(time.time() * 1000)
        start_time = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
        for interval in [self.kline_interval, self.daily_interval]:
            raw = self.client.fetch_ohlcv(
                coin=self.symbol, interval=interval,
                start_time=start_time, end_time=end_time,
            )
            klines = [Kline.from_api_candle(c, self.symbol, interval) for c in raw]
            n = insert_klines(klines)
            if n > 0:
                logger.debug(f"{interval} 增量: {n} 条新 K 线")
        return {
            "4h": await self.load_klines_df(self.kline_interval),
            "1d": await self.load_klines_df(self.daily_interval),
        }
