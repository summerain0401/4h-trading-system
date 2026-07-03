#!/usr/bin/env python3
"""信号验证脚本 — 支持灵活日期范围

用法:
    python verify_signals.py                        # 默认近 90 天
    python verify_signals.py --days 180             # 近 180 天
    python verify_signals.py --start 2025-10-01 --end 2025-12-31   # 指定日期
"""

import argparse
import asyncio
import logging
import sys
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exchange.hl_client import HLClient
from core.risk_manager import RiskManager
from core.signal_engine import SignalEngine
from core.indicators import (
    calc_ema, calc_vwma, calc_adx, calc_atr, calc_bollinger_bands,
    calc_ema_slope, count_ma_crosses, is_bb_squeeze_expanding, is_volume_surge,
)
from storage.db import init_db, insert_klines
from storage.models import Kline
from notify.terminal import (
    print_banner, print_section, print_market_status,
    Color, format_price,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
logger = logging.getLogger("verify")


def parse_args():
    p = argparse.ArgumentParser(description="4H 波段交易系统回测")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=90, help="回测最近 N 天（默认 90）")
    g.add_argument("--start", type=str, help="起始日期 YYYY-MM-DD（需配合 --end）")
    p.add_argument("--end", type=str, help="结束日期 YYYY-MM-DD（默认今天）")
    return p.parse_args()


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    config["network"] = "mainnet"
    return config


def klines_to_df(klines: list[Kline]) -> pd.DataFrame:
    data = [{"open_time": k.open_time, "open": k.open, "high": k.high,
             "low": k.low, "close": k.close, "volume": k.volume} for k in klines]
    df = pd.DataFrame(data)
    df.set_index("open_time", inplace=True)
    df.sort_index(inplace=True)
    return df


def fetch_batch(client, symbol, interval, start_ms, end_ms):
    """分批拉取，绕过 API 单次 ~500 根上限"""
    batch_ms = 90 * 24 * 3600 * 1000  # 90 天毫秒
    all_data = []
    cursor = end_ms
    while cursor > start_ms:
        batch_start = max(cursor - batch_ms, start_ms)
        data = client.fetch_ohlcv(coin=symbol, interval=interval, start_time=batch_start, end_time=cursor)
        if data:
            all_data.extend(data)
        cursor = batch_start
    # 去重排序
    all_data = sorted(all_data, key=lambda c: c["t"])
    seen = set()
    dedup = []
    for c in all_data:
        if c["t"] not in seen:
            seen.add(c["t"]); dedup.append(c)
    return dedup


async def main():
    args = parse_args()
    config = load_config()
    symbol = config["symbol"]

    # 确定时间范围
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(args.end or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=args.days)

    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)
    total_days = (end_dt - start_dt).days

    date_label = f"{start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}（{total_days} 天）"
    print_banner(f"信号验证 — {symbol} 主网 {date_label}")

    logger.info("连接 Hyperliquid 主网...")
    client = HLClient(network="mainnet")
    risk = RiskManager(config)
    engine = SignalEngine(config, risk)
    init_db()

    logger.info(f"拉取数据: {date_label}")
    raw_4h = fetch_batch(client, symbol, "4h", start_ms, end_ms)
    raw_daily = fetch_batch(client, symbol, "1d", start_ms, end_ms)

    if not raw_4h or not raw_daily:
        logger.error("数据拉取失败")
        return

    klines_4h = [Kline.from_api_candle(c, symbol, "4h") for c in raw_4h]
    klines_daily = [Kline.from_api_candle(c, symbol, "1d") for c in raw_daily]
    insert_klines(klines_4h)
    insert_klines(klines_daily)

    df_4h = klines_to_df(klines_4h)
    df_daily = klines_to_df(klines_daily)

    logger.info(f"4H: {len(df_4h)} 根 ({df_4h.index[0].strftime('%m-%d')} → {df_4h.index[-1].strftime('%m-%d')})")
    logger.info(f"日线: {len(df_daily)} 根 ({df_daily.index[0].strftime('%m-%d')} → {df_daily.index[-1].strftime('%m-%d')})")

    # ====== 当前市场状态 ======
    if len(df_daily) >= 30:
        print_banner("📍 市场状态（数据末尾）")
        state = engine.build_market_state(df_daily, df_4h)
        print_market_status({
            "adx_4h": state.adx_4h,
            "is_ranging": state.is_ranging,
            "daily_direction": state.daily_direction,
            "current_phase": state.current_phase,
            "ema9_slope_4h": state.ema9_slope_4h,
            "bb_bandwidth": state.bb_bandwidth,
            "bb_expanding": state.bb_expanding,
        })

    # ====== 历史信号回溯 ======
    print_banner(f"🔍 历史信号回溯 ({total_days} 天)")

    detected = []
    has_pos = False

    engine._active_position = None
    engine._phase_four_count = 0
    engine._last_phase_four_bar = None
    engine._last_exit_bar = None
    engine._consecutive_big_losses = 0

    for i in range(60, len(df_4h)):
        w4 = df_4h.iloc[:i]
        wd = df_daily[df_daily.index <= w4.index[-1]]
        if len(wd) < 30:
            continue

        signals = engine.run_all_checks(wd, w4, has_position=has_pos)
        for sig in signals:
            if sig.action == "exit":
                has_pos = False
            else:
                has_pos = True

            detected.append({
                "time": w4.index[-1],
                "price": w4["close"].iloc[-1],
                "phase": sig.phase,
                "direction": sig.direction,
                "action": sig.action,
                "reason": sig.reason,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss_price,
            })

    if detected:
        print(f"\n  共 {len(detected)} 个信号:\n")
        pmap = {"phase_two": "阶段二·启动", "phase_three": "阶段三·加速",
                "phase_four": "阶段四·回调"}
        amap = {"entry": "开仓", "add": "加仓", "exit": "平仓"}

        for s in detected:
            dc = Color.GREEN if s["direction"] == "long" else Color.RED
            ts = s["time"].strftime("%m-%d %H:%M")
            print(
                f"  {ts} | {Color.BOLD}{pmap.get(s['phase'], s['phase'])}{Color.RESET} | "
                f"{dc}{s['direction'].upper()} {amap.get(s['action'], s['action'])}{Color.RESET} | "
                f"@{format_price(s['price'])}"
            )
            if s.get("entry_price"):
                print(f"    入场: {format_price(s['entry_price'])} | 止损: {format_price(s['stop_loss'])}")
            if s.get("reason"):
                print(f"    {Color.DIM}{s['reason'][:110]}{Color.RESET}")

        # 统计
        entries = [s for s in detected if s["action"] == "entry"]
        adds = [s for s in detected if s["action"] == "add"]
        exits = [s for s in detected if s["action"] == "exit"]
        longs = [s for s in detected if s["direction"] == "long"]
        shorts = [s for s in detected if s["direction"] == "short"]
        p2 = [s for s in detected if s["phase"] == "phase_two"]
        p3 = [s for s in detected if s["phase"] == "phase_three"]
        p4 = [s for s in detected if s["phase"] == "phase_four"]

        print_section("信号统计")
        print(f"  阶段二（启动建仓）: {len(p2)} 次")
        print(f"  阶段三（加速追仓）: {len(p3)} 次")
        print(f"  阶段四（回调加仓）: {len(p4)} 次")
        print(f"  做多: {len(longs)} | 做空: {len(shorts)}")
        print(f"  开仓: {len(entries)} | 加仓: {len(adds)} | 平仓: {len(exits)}")

        # 交易配对
        if entries:
            print_section("交易盈亏估算（入场→出场配对）")
            leverage = config["leverage"]
            trades = []
            ep = ed = et = None
            for s in detected:
                if s["action"] == "entry":
                    ep = s["price"]; et = s["time"]; ed = s["direction"]
                elif s["action"] == "exit" and ep:
                    spot_pnl = (s["price"] - ep) / ep * 100 if ed == "long" else (ep - s["price"]) / ep * 100
                    trades.append({"entry_ts": et, "direction": ed, "entry_px": ep,
                                   "exit_px": s["price"], "spot_pnl": spot_pnl,
                                   "pnl_pct": spot_pnl * leverage})
                    ep = None

            if trades:
                wins = [t for t in trades if t["pnl_pct"] > 0]
                total = sum(t["pnl_pct"] for t in trades)
                total_spot = sum(t["spot_pnl"] for t in trades)
                win_rate = len(wins) / len(trades) * 100 if trades else 0
                print(f"  配对交易: {len(trades)} 笔 | 杠杆: {leverage}x")
                print(f"  胜率: {win_rate:.0f}% ({len(wins)}W/{len(trades)-len(wins)}L)")
                tc = Color.GREEN if total > 0 else Color.RED
                print(f"  累计现货盈亏: {tc}{total_spot:+.1f}%{Color.RESET}")
                print(f"  累计杠杆盈亏: {tc}{total:+.1f}%{Color.RESET}")
                for t in trades:
                    c = Color.GREEN if t["pnl_pct"] > 0 else Color.RED
                    ts = t["entry_ts"].strftime("%m-%d")
                    print(f"    {ts} {t['direction']} {t['entry_px']:.0f}→{t['exit_px']:.0f}: "
                          f"现货 {c}{t['spot_pnl']:+.1f}%{Color.RESET} | "
                          f"{leverage}x杠杆 {c}{t['pnl_pct']:+.1f}%{Color.RESET}")
    else:
        print(f"\n  {Color.DIM}此时间段内未检测到信号{Color.RESET}")

    print()
    print_banner("验证完成")


if __name__ == "__main__":
    asyncio.run(main())
