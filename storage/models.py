"""数据模型定义 — SQLite 表结构"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

BEIJING_TZ = timezone(timedelta(hours=8))


# ============================================================
# K 线数据结构
# ============================================================

@dataclass
class Kline:
    """单根 K 线"""
    symbol: str
    interval: str        # "4h" / "1d"
    open_time: datetime   # K 线开盘时间 (UTC)
    close_time: datetime  # K 线收盘时间 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int           # 成交笔数

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "trades": self.trades,
        }

    @classmethod
    def from_api_candle(cls, candle: dict, symbol: str, interval: str) -> "Kline":
        """从 Hyperliquid API 返回的 candle 数据构造"""
        return cls(
            symbol=symbol,
            interval=interval,
            open_time=datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).astimezone(BEIJING_TZ).replace(tzinfo=None),
            close_time=datetime.fromtimestamp(candle["T"] / 1000, tz=timezone.utc).astimezone(BEIJING_TZ).replace(tzinfo=None),
            open=float(candle["o"]),
            high=float(candle["h"]),
            low=float(candle["l"]),
            close=float(candle["c"]),
            volume=float(candle["v"]),
            trades=int(candle["n"]),
        )


# ============================================================
# 信号数据结构
# ============================================================

@dataclass
class Signal:
    """交易信号"""
    id: Optional[int] = None
    timestamp: Optional[datetime] = None
    phase: str = ""                          # "phase_one" / "phase_two" / "phase_three" / "phase_four"
    direction: str = ""                      # "long" / "short"
    action: str = ""                         # "entry" / "add" / "exit" / "alert"
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    suggested_size: Optional[float] = None   # 建议仓位
    reason: str = ""                         # 触发原因（人类可读）
    acknowledged: bool = False               # 用户是否已确认

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "phase": self.phase,
            "direction": self.direction,
            "action": self.action,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "suggested_size": self.suggested_size,
            "reason": self.reason,
            "acknowledged": self.acknowledged,
        }


# ============================================================
# 持仓数据结构
# ============================================================

@dataclass
class Position:
    """持仓信息"""
    coin: str
    side: str                    # "long" / "short"
    size: float                  # 持仓数量
    entry_price: float           # 入场均价
    leverage: int
    liquidation_price: float     # 强平价
    unrealized_pnl: float        # 未实现盈亏
    margin_used: float           # 已用保证金
    current_phase: str           # 当前所属阶段 "phase_two" / "phase_three" / "phase_four"
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None


# ============================================================
# 账户摘要
# ============================================================

@dataclass
class AccountSummary:
    """账户摘要"""
    account_value: float         # 账户总净值
    total_margin_used: float     # 已用保证金
    withdrawable: float          # 可提现余额
    open_positions: list[Position]


# ============================================================
# 市场状态（系统内部状态机）
# ============================================================

@dataclass
class MarketState:
    """市场状态快照"""
    timestamp: datetime
    symbol: str
    daily_direction: str         # "long" / "short" / "none"
    adx_4h: float               # 4H ADX(14) 当前值
    adx_daily: float            # 日线 ADX(14) 当前值
    ema9_4h: float              # 4H 9EMA
    vwma20_4h: float            # 4H 20VWMA
    ema9_daily: float           # 日线 9EMA
    vwma20_daily: float         # 日线 20VWMA
    ema9_slope_daily: str       # 日线 9EMA 斜率方向 "up"/"down"/"flat"
    ema9_slope_4h: str          # 4H 9EMA 斜率方向
    bb_bandwidth: float         # 布林带带宽
    bb_expanding: bool          # 布林带是否在扩张
    is_ranging: bool            # 是否震荡市
    current_phase: str          # 当前所处阶段
    last_signal: Optional[Signal] = None
