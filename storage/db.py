"""SQLite 数据库初始化与操作"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

from .models import Kline, Signal, MarketState, BEIJING_TZ

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def get_db_path() -> str:
    """获取数据库文件路径"""
    os.makedirs(DB_DIR, exist_ok=True)
    return os.path.join(DB_DIR, "trading.db")


def get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_conn()
    cursor = conn.cursor()

    # K 线表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time TEXT NOT NULL,
            close_time TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            trades INTEGER DEFAULT 0,
            UNIQUE(symbol, interval, open_time)
        )
    """)

    # K 线索引
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_klines_symbol_interval_time
        ON klines(symbol, interval, open_time)
    """)

    # 信号表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            phase TEXT NOT NULL,
            direction TEXT NOT NULL,
            action TEXT NOT NULL,
            entry_price REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            suggested_size REAL,
            reason TEXT,
            acknowledged INTEGER DEFAULT 0
        )
    """)

    # 市场状态表（保留最近状态）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            daily_direction TEXT,
            adx_4h REAL,
            adx_daily REAL,
            ema9_4h REAL,
            vwma20_4h REAL,
            ema9_daily REAL,
            vwma20_daily REAL,
            ema9_slope_daily TEXT,
            ema9_slope_4h TEXT,
            bb_bandwidth REAL,
            bb_expanding INTEGER DEFAULT 0,
            is_ranging INTEGER DEFAULT 0,
            current_phase TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# K 线操作
# ============================================================

def insert_klines(klines: list[Kline]) -> int:
    """批量插入 K 线（忽略重复）"""
    conn = get_conn()
    cursor = conn.cursor()
    count = 0
    for k in klines:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO klines
                (symbol, interval, open_time, close_time, open, high, low, close, volume, trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (k.symbol, k.interval, k.open_time.isoformat(), k.close_time.isoformat(),
                  k.open, k.high, k.low, k.close, k.volume, k.trades))
            if cursor.rowcount > 0:
                count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return count


def get_klines(symbol: str, interval: str, limit: int = 360) -> list[Kline]:
    """获取最近的 K 线"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM klines
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time ASC
        LIMIT ?
    """, (symbol, interval, limit))
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_kline(r) for r in rows]


def _row_to_kline(row) -> Kline:
    return Kline(
        symbol=row["symbol"],
        interval=row["interval"],
        open_time=datetime.fromisoformat(row["open_time"]),
        close_time=datetime.fromisoformat(row["close_time"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        trades=row["trades"],
    )


# ============================================================
# 信号操作
# ============================================================

def insert_signal(signal: Signal) -> int:
    """插入信号，返回 ID"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO signals
        (timestamp, phase, direction, action, entry_price, stop_loss_price,
         take_profit_price, suggested_size, reason, acknowledged)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal.timestamp.isoformat() if signal.timestamp else datetime.now(tz=BEIJING_TZ).isoformat(),
        signal.phase, signal.direction, signal.action,
        signal.entry_price, signal.stop_loss_price, signal.take_profit_price,
        signal.suggested_size, signal.reason, int(signal.acknowledged)
    ))
    signal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return signal_id


def get_recent_signals(limit: int = 20) -> list[Signal]:
    """获取最近的信号"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_signal(r) for r in rows]


def _row_to_signal(row) -> Signal:
    return Signal(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        phase=row["phase"],
        direction=row["direction"],
        action=row["action"],
        entry_price=row["entry_price"],
        stop_loss_price=row["stop_loss_price"],
        take_profit_price=row["take_profit_price"],
        suggested_size=row["suggested_size"],
        reason=row["reason"],
        acknowledged=bool(row["acknowledged"]),
    )


# ============================================================
# 市场状态操作
# ============================================================

def save_market_state(state: MarketState):
    """保存市场状态快照"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO market_states
        (timestamp, symbol, daily_direction, adx_4h, adx_daily,
         ema9_4h, vwma20_4h, ema9_daily, vwma20_daily,
         ema9_slope_daily, ema9_slope_4h, bb_bandwidth, bb_expanding,
         is_ranging, current_phase)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        state.timestamp.isoformat(),
        state.symbol,
        state.daily_direction,
        state.adx_4h,
        state.adx_daily,
        state.ema9_4h,
        state.vwma20_4h,
        state.ema9_daily,
        state.vwma20_daily,
        state.ema9_slope_daily,
        state.ema9_slope_4h,
        state.bb_bandwidth,
        int(state.bb_expanding),
        int(state.is_ranging),
        state.current_phase,
    ))
    conn.commit()
    conn.close()


def get_latest_market_state(symbol: str) -> Optional[MarketState]:
    """获取最新市场状态"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM market_states
        WHERE symbol = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (symbol,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return MarketState(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        symbol=row["symbol"],
        daily_direction=row["daily_direction"],
        adx_4h=row["adx_4h"],
        adx_daily=row["adx_daily"],
        ema9_4h=row["ema9_4h"],
        vwma20_4h=row["vwma20_4h"],
        ema9_daily=row["ema9_daily"],
        vwma20_daily=row["vwma20_daily"],
        ema9_slope_daily=row["ema9_slope_daily"],
        ema9_slope_4h=row["ema9_slope_4h"],
        bb_bandwidth=row["bb_bandwidth"],
        bb_expanding=bool(row["bb_expanding"]),
        is_ranging=bool(row["is_ranging"]),
        current_phase=row["current_phase"],
    )
