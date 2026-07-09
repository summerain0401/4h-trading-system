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
    p.add_argument("--mode", choices=["params", "gate", "all"], default="all",
                   help="params=参数校准臂 gate=体制开关臂 all=两者")
    p.add_argument("--gate-lookback", type=int, default=30,
                   help="体制开关的表现回看天数（近N天策略净利<0→下一段停止开仓）")
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


def build_gate_schedule(config, df_4h, df_daily, boundaries, fee_bps, gate_lookback_days):
    """体制开关日程：每个边界只用之前的数据评估策略近 N 天表现

    规则（元动量）：静态策略在近 gate_lookback_days 天的净利 < 0
    → 下一段停止开新仓/加仓（已有持仓正常管理出场）。
    """
    schedule = {}
    for b in boundaries:
        w4 = df_4h[(df_4h.index >= b - pd.Timedelta(days=gate_lookback_days + 75))
                   & (df_4h.index < b)]
        wd = df_daily[(df_daily.index >= b - pd.Timedelta(days=gate_lookback_days + 105))
                      & (df_daily.index < b)]
        if len(w4) < 200:
            schedule[b] = False
            continue
        res = run_backtest(config, w4, wd, fee_bps=fee_bps,
                           start_ts=b - pd.Timedelta(days=gate_lookback_days + 20))
        recent_net = sum(c["net"] for c in res["campaigns"]
                         if c["exit_ts"] >= b - pd.Timedelta(days=gate_lookback_days))
        schedule[b] = recent_net < 0
    return schedule


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

    # ── 边界：每 interval 天一个决策点 ──
    boundaries = []
    b = eval_start
    while b <= df_4h.index[-1]:
        boundaries.append(b)
        b = b + pd.Timedelta(days=interval_days)

    # ── 静态参数对照组（所有模式都跑）──
    static = run_backtest(config, df_4h, df_daily, fee_bps=args.fee_bps,
                          start_ts=eval_start)
    arms = [("静态默认参数（对照）", static)]
    param_schedule, gate_schedule = None, None

    # ── 参数校准臂 ──
    if args.mode in ("params", "all"):
        optimizer = ParamOptimizer(config)
        param_schedule = {}
        t0 = time.time()
        for n, b in enumerate(boundaries, 1):
            params = optimizer.calibrate(df_4h[df_4h.index < b],
                                         df_daily[df_daily.index < b], b)
            param_schedule[b] = params
            logger.info(f"[{n}/{len(boundaries)}] 校准 @ {b:%Y-%m-%d}: {params} "
                        f"(耗时 {time.time()-t0:.0f}s)")
        pending_p = sorted(param_schedule.keys())

        def reconfigure_params(bar_ts):
            new_cfg = None
            while pending_p and bar_ts >= pending_p[0]:
                params = param_schedule[pending_p.pop(0)]
                if params:
                    new_cfg = ParamOptimizer.apply_params(copy.deepcopy(config), params)
            return new_cfg

        wf = run_backtest(config, df_4h, df_daily, fee_bps=args.fee_bps,
                          start_ts=eval_start, reconfigure=reconfigure_params)
        arms.append(("参数校准（滚动样本外）", wf))

    # ── 体制开关臂：近 N 天策略亏损 → 下一段停止开仓 ──
    if args.mode in ("gate", "all"):
        logger.info(f"构建体制开关日程（表现回看 {args.gate_lookback} 天）...")
        gate_schedule = build_gate_schedule(config, df_4h, df_daily, boundaries,
                                            args.fee_bps, args.gate_lookback)
        pending_g = sorted(gate_schedule.keys())

        def reconfigure_gate(bar_ts):
            new_cfg = None
            while pending_g and bar_ts >= pending_g[0]:
                halt = gate_schedule[pending_g.pop(0)]
                new_cfg = copy.deepcopy(config)
                new_cfg["trading_halt"] = halt
            return new_cfg

        gated = run_backtest(config, df_4h, df_daily, fee_bps=args.fee_bps,
                             start_ts=eval_start, reconfigure=reconfigure_gate)
        arms.append((f"体制开关（近{args.gate_lookback}天亏损→停手）", gated))

    # ── 报告 ──
    print_section(f"样本外对比（{args.days} 天 | 本金 ${account:,.0f}）")
    for name, res in arms:
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

    if param_schedule:
        print_section("校准参数轨迹")
        for b in sorted(param_schedule.keys()):
            print(f"    {b:%Y-%m-%d}: {param_schedule[b]}")

    if gate_schedule:
        print_section("体制开关轨迹（HALT=停止开仓）")
        line = []
        for b in sorted(gate_schedule.keys()):
            line.append(f"{b:%m-%d}:{'HALT' if gate_schedule[b] else 'ok'}")
        for i in range(0, len(line), 6):
            print("    " + "  ".join(line[i:i + 6]))

    # 按月净值对比
    print_section("月末累计净值对比")
    def monthly(res):
        m = {}
        for ts_, cum in res["curve"]:
            m[ts_.strftime("%Y-%m")] = cum
        return m
    monthly_arms = [(name, monthly(res)) for name, res in arms]
    months = sorted(set().union(*(m.keys() for _, m in monthly_arms)))
    for month in months:
        cells = []
        for name, m in monthly_arms:
            v = m.get(month)
            cells.append(f"{v:+8,.0f}" if v is not None else "       -")
        print(f"    {month}: " + " | ".join(cells))
    print("    列顺序: " + " | ".join(name for name, _ in monthly_arms))

    print_banner("验证完成")


if __name__ == "__main__":
    main()
