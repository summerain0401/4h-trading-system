#!/usr/bin/env python3
"""LONG 出场放宽实验 — 两段式验证（选择窗口 + 验证窗口）

背景：多头趋势段系统只吃到趋势的约 2/3，每次跟踪止盈都在回调底部交仓。
假设：LONG 的跟踪缓冲（trailing_atr）太紧 / 超时太短。

方法（防止在同一段数据上挑参数）：
- 选择窗口：480→240 天前（旧数据）——在此挑最优变体
- 验证窗口：最近 240 天——检验选出的变体是否站得住
- 变体只改 direction.long.*，SHORT 完全不动
- 评分 = 净利 − 最大回撤（与校准器同口径，不加近期加权）

用法:
    python exit_experiment.py
    python exit_experiment.py --window-days 240 --fee-bps 4.5
"""

import argparse
import copy
import itertools
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exchange.hl_client import HLClient
from core.backtester import run_backtest
from storage.models import Kline
from walkforward import fetch_batch, klines_to_df, load_config
from notify.terminal import print_banner, print_section, Color

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("exit_experiment")

VARIANTS = {
    "long_trailing_atr": [0.8, 1.0, 1.2, 1.5],   # 现状 0.8
    "long_timeout_bars": [12, 16],                # 现状 12
}


def parse_args():
    p = argparse.ArgumentParser(description="LONG 出场放宽实验")
    p.add_argument("--window-days", type=int, default=240, help="每个窗口的天数")
    p.add_argument("--fee-bps", type=float, default=4.5)
    p.add_argument("--account", type=float, default=10000.0)
    return p.parse_args()


def apply_variant(config: dict, trailing: float, timeout: int) -> dict:
    cfg = copy.deepcopy(config)
    cfg["direction"]["long"]["trailing_atr"] = trailing
    cfg["direction"]["long"]["timeout_bars"] = timeout
    return cfg


def evaluate(config, df_4h, df_daily, start_ts, end_ts, fee_bps):
    """在 [start_ts, end_ts] 窗口上回测（预热数据由切片保留）"""
    w4 = df_4h[df_4h.index <= end_ts]
    wd = df_daily[df_daily.index <= end_ts]
    res = run_backtest(config, w4, wd, fee_bps=fee_bps, start_ts=start_ts)
    longs = [c for c in res["campaigns"] if c["direction"] == "long"]
    shorts = [c for c in res["campaigns"] if c["direction"] == "short"]
    return {
        "net": res["net"], "max_dd": res["max_dd"],
        "score": res["net"] - res["max_dd"],
        "n": len(res["campaigns"]),
        "long_net": sum(c["net"] for c in longs), "long_n": len(longs),
        "short_net": sum(c["net"] for c in shorts),
    }


def main():
    args = parse_args()
    config = load_config()
    symbol = config["symbol"]
    account = args.account

    total_days = args.window_days * 2 + 40  # 两个窗口 + 预热
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=total_days)

    print_banner(f"LONG 出场放宽实验 — {symbol} | 选择/验证各 {args.window_days} 天")

    client = HLClient(network="mainnet")
    raw_4h = fetch_batch(client, symbol, "4h",
                         int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    raw_d = fetch_batch(client, symbol, "1d",
                        int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    df_4h = klines_to_df([Kline.from_api_candle(c, symbol, "4h") for c in raw_4h])
    df_daily = klines_to_df([Kline.from_api_candle(c, symbol, "1d") for c in raw_d])

    now = df_4h.index[-1]
    val_start = now - pd.Timedelta(days=args.window_days)          # 验证窗口：最近 240 天
    sel_start = val_start - pd.Timedelta(days=args.window_days)    # 选择窗口：更早 240 天
    logger.info(f"数据: {df_4h.index[0]:%Y-%m-%d} → {now:%Y-%m-%d} ({len(df_4h)} 根 4H)")
    logger.info(f"选择窗口: {sel_start:%Y-%m-%d} → {val_start:%Y-%m-%d}")
    logger.info(f"验证窗口: {val_start:%Y-%m-%d} → {now:%Y-%m-%d}")

    rows = []
    for trailing, timeout in itertools.product(VARIANTS["long_trailing_atr"],
                                               VARIANTS["long_timeout_bars"]):
        cfg = apply_variant(config, trailing, timeout)
        sel = evaluate(cfg, df_4h, df_daily, sel_start, val_start, args.fee_bps)
        val = evaluate(cfg, df_4h, df_daily, val_start, now, args.fee_bps)
        rows.append({"trailing": trailing, "timeout": timeout, "sel": sel, "val": val})
        logger.info(f"trail={trailing} timeout={timeout}: "
                    f"选择窗 net={sel['net']:+,.0f} dd={sel['max_dd']:,.0f} | "
                    f"验证窗 net={val['net']:+,.0f} dd={val['max_dd']:,.0f}")

    # 报告
    print_section(f"变体矩阵（本金 ${account:,.0f}，评分 = 净利 − 最大回撤）")
    base_mark = lambda t, o: " ←现状" if (t == 0.8 and o == 12) else ""
    print(f"    {'trail':>5} {'timeout':>7} | {'选择窗net':>9} {'DD':>7} {'LONG净':>8} {'评分':>8} |"
          f" {'验证窗net':>9} {'DD':>7} {'LONG净':>8} {'评分':>8}")
    for r in rows:
        s, v = r["sel"], r["val"]
        print(f"    {r['trailing']:>5} {r['timeout']:>7} |"
              f" {s['net']:>+9,.0f} {s['max_dd']:>7,.0f} {s['long_net']:>+8,.0f} {s['score']:>+8,.0f} |"
              f" {v['net']:>+9,.0f} {v['max_dd']:>7,.0f} {v['long_net']:>+8,.0f} {v['score']:>+8,.0f}"
              f"{base_mark(r['trailing'], r['timeout'])}")

    # 预先承诺的选择规则：只看选择窗口评分
    best = max(rows, key=lambda r: r["sel"]["score"])
    base = next(r for r in rows if r["trailing"] == 0.8 and r["timeout"] == 12)
    print_section("结论（选择规则：选择窗口评分最高）")
    print(f"    选择窗口最优: trail={best['trailing']} timeout={best['timeout']}"
          f" (评分 {best['sel']['score']:+,.0f} vs 现状 {base['sel']['score']:+,.0f})")
    print(f"    该变体在验证窗口: net={best['val']['net']:+,.0f} dd={best['val']['max_dd']:,.0f}"
          f" 评分={best['val']['score']:+,.0f}")
    print(f"    现状在验证窗口:   net={base['val']['net']:+,.0f} dd={base['val']['max_dd']:,.0f}"
          f" 评分={base['val']['score']:+,.0f}")
    verdict = ("✅ 两窗一致，建议采纳" if best["val"]["score"] >= base["val"]["score"]
               and (best["trailing"], best["timeout"]) != (0.8, 12)
               else "❌ 验证窗口不支持（或现状已最优），维持现状")
    print(f"    {verdict}")

    print_banner("实验完成")


if __name__ == "__main__":
    main()
