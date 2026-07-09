#!/usr/bin/env python3
"""滚动样本外验证 — 检验"定期校准流程"本身是否赚钱

模拟真实操作流程：每 interval 天，只用该时点之前 lookback 天的数据
网格选参，然后用选出的参数交易接下来的 interval 天（纯样本外）。
持仓与冷却状态跨参数切换保留（热切换）。

与"静态默认参数跑全程"对比——如果校准流程有效，
样本外累计净值应不差于（理想情况优于）静态参数。

用法:
    python walkforward.py --days 240                # 评估最近 240 天（样本外部分）
    python walkforward.py --days 240 --interval 30  # 30 天校准一次
"""

import argparse
import copy
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exchange.hl_client import HLClient
from core.backtester import run_backtest
from core.param_optimizer import ParamOptimizer
from storage.models import Kline
from notify.terminal import print_banner, print_section, Color

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("walkforward")


def parse_args():
    p = argparse.ArgumentParser(description="滚动样本外验证")
    p.add_argument("--days", type=int, default=240, help="样本外评估天数（默认 240）")
    p.add_argument("--interval", type=int, default=None, help="校准周期天数（默认取 config）")
    p.add_argument("--lookback", type=int, default=None, help="校准回看天数（默认取 config）")
    p.add_argument("--fee-bps", type=float, default=4.5)
    p.add_argument("--account", type=float, default=10000.0)
    return p.parse_args()


def load_config():
    with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as f:
        config = yaml.safe_load(f)
    config["network"] = "mainnet"
    return config


def fetch_batch(client, symbol, interval, start_ms, end_ms):
    batch_ms = 90 * 24 * 3600 * 1000
    all_data, cursor = [], end_ms
    while cursor > start_ms:
        batch_start = max(cursor - batch_ms, start_ms)
        data = client.fetch_ohlcv(coin=symbol, interval=interval,
                                  start_time=batch_start, end_time=cursor)
        if data:
            all_data.extend(data)
        cursor = batch_start
    seen, dedup = set(), []
    for c in sorted(all_data, key=lambda c: c["t"]):
        if c["t"] not in seen:
            seen.add(c["t"])
            dedup.append(c)
    return dedup


def klines_to_df(klines):
    df = pd.DataFrame([{"open_time": k.open_time, "open": k.open, "high": k.high,
                        "low": k.low, "close": k.close, "volume": k.volume} for k in klines])
    df.set_index("open_time", inplace=True)
    df.sort_index(inplace=True)
    return df


def main():
    args = parse_args()
    config = load_config()
    symbol = config["symbol"]
    cal = config.get("calibration", {})
    interval_days = args.interval or cal.get("interval_days", 15)
    lookback_days = args.lookback or cal.get("lookback_days", 90)
    account = args.account

    # 数据范围 = 样本外评估期 + 校准回看 + 指标预热
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + lookback_days + 30)
    print_banner(f"滚动样本外验证 — {symbol} | 评估 {args.days} 天 | "
                 f"每 {interval_days} 天校准 | 回看 {lookback_days} 天")

    client = HLClient(network="mainnet")
    raw_4h = fetch_batch(client, symbol, "4h",
                         int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    raw_d = fetch_batch(client, symbol, "1d",
                        int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    df_4h = klines_to_df([Kline.from_api_candle(c, symbol, "4h") for c in raw_4h])
    df_daily = klines_to_df([Kline.from_api_candle(c, symbol, "1d") for c in raw_d])
    logger.info(f"4H: {len(df_4h)} 根 | 日线: {len(df_daily)} 根 "
                f"({df_4h.index[0]:%Y-%m-%d} → {df_4h.index[-1]:%Y-%m-%d})")

    eval_start = df_4h.index[-1] - pd.Timedelta(days=args.days)

    # ── 第一步：构建校准日程（每个边界只用之前的数据选参，无泄漏）──
    boundaries = []
    b = eval_start
    while b <= df_4h.index[-1]:
        boundaries.append(b)
        b = b + pd.Timedelta(days=interval_days)

    optimizer = ParamOptimizer(config)
    schedule = {}
    t0 = time.time()
    for n, b in enumerate(boundaries, 1):
        past_4h = df_4h[df_4h.index < b]
        past_d = df_daily[df_daily.index < b]
        params = optimizer.calibrate(past_4h, past_d, b)
        schedule[b] = params
        logger.info(f"[{n}/{len(boundaries)}] 校准 @ {b:%Y-%m-%d}: {params} "
                    f"(耗时 {time.time()-t0:.0f}s)")

    # ── 第二步：样本外单遍回测（到达边界时热切换参数，保留持仓状态）──
    pending = sorted(schedule.keys())

    def reconfigure(bar_ts):
        new_cfg = None
        while pending and bar_ts >= pending[0]:
            boundary = pending.pop(0)
            params = schedule[boundary]
            if params:
                new_cfg = ParamOptimizer.apply_params(copy.deepcopy(config), params)
        return new_cfg

    wf = run_backtest(config, df_4h, df_daily, fee_bps=args.fee_bps,
                      start_ts=eval_start, reconfigure=reconfigure)

    # ── 第三步：静态参数对照组 ──
    static = run_backtest(config, df_4h, df_daily, fee_bps=args.fee_bps,
                          start_ts=eval_start)

    # ── 报告 ──
    print_section(f"样本外对比（{args.days} 天 | 本金 ${account:,.0f}）")
    for name, res in (("定期校准（滚动样本外）", wf), ("静态默认参数（对照）", static)):
        n = len(res["campaigns"])
        if n == 0:
            print(f"  {name}: 无交易")
            continue
        wins = sum(1 for c in res["campaigns"] if c["net"] > 0)
        nc = Color.GREEN if res["net"] > 0 else Color.RED
        print(f"  {name}:")
        print(f"    交易 {n} 笔 | 胜率 {wins/n*100:.0f}% | "
              f"净盈亏 {nc}{res['net']:+,.0f} ({res['net']/account*100:+.1f}%){Color.RESET} | "
              f"最大回撤 -{res['max_dd']:,.0f} (-{res['max_dd']/account*100:.1f}%)")

    # 校准参数的漂移轨迹
    print_section("校准参数轨迹")
    for b in sorted(schedule.keys()):
        print(f"    {b:%Y-%m-%d}: {schedule[b]}")

    # 按月净值对比
    print_section("月末累计净值对比（校准 vs 静态）")
    def monthly(res):
        m = {}
        for ts_, cum in res["curve"]:
            m[ts_.strftime("%Y-%m")] = cum
        return m
    m_wf, m_st = monthly(wf), monthly(static)
    for month in sorted(set(m_wf) | set(m_st)):
        w = m_wf.get(month)
        s = m_st.get(month)
        w_str = f"{w:+8,.0f}" if w is not None else "       -"
        s_str = f"{s:+8,.0f}" if s is not None else "       -"
        print(f"    {month}: 校准 {w_str} | 静态 {s_str}")

    print_banner("验证完成")


if __name__ == "__main__":
    main()
