"""共享回测核心 — verify_signals / param_optimizer / walkforward 共用同一记账口径

口径：
- 批次记账：底仓与每次加仓独立批次，分别归因盈亏
- 批次独立止损：按 K 线 high/low 盘中触发，止损价成交（跳空按开盘价）
- 双边 taker 手续费
- 日线仅用已收盘数据（无前视）
- 固定本金（不复利），与 SignalEngine 仓位口径一致

引擎窗口截断：只把最近 WINDOW_4H 根 4H / WINDOW_DAILY 根日线传给引擎。
指标（ewm/rolling ≤20 周期）在 150 根后的预热残差可忽略，
把逐bar回测从 O(n²) 降为 O(n)，供校准器网格搜索使用。
"""

import copy

import pandas as pd

from core.signal_engine import SignalEngine
from core.risk_manager import RiskManager

WINDOW_4H = 150
WINDOW_DAILY = 60


def swap_engine(old: SignalEngine, new_config: dict) -> SignalEngine:
    """按新参数重建引擎，保留持仓与冷却状态（校准热切换用）"""
    new = SignalEngine(new_config, RiskManager(new_config))
    for attr in ("_active_position", "_phase_four_count", "_last_phase_four_ts",
                 "_last_exit_ts", "_consecutive_big_losses", "_current_phase"):
        setattr(new, attr, getattr(old, attr))
    return new


def _mark_batch_exit(camp: dict, ev: dict) -> None:
    """批次止损事件 → 标记交易记录中对应批次的出场价（FIFO 匹配）"""
    for b in camp["batches"]:
        if ("exit_px" not in b and b["phase"] == ev["phase"]
                and b["price"] == ev["price"] and b["size"] == ev["size"]):
            b["exit_px"] = ev["fill_px"]
            b["stopped"] = True
            return


def _settle(camp: dict, exit_px: float, exit_ts, fee_rate: float,
            is_open: bool = False) -> dict:
    """按批次结算：已被批次止损的按各自成交价，其余按最终出场价"""
    sign = 1 if camp["direction"] == "long" else -1
    for b in camp["batches"]:
        b.setdefault("exit_px", exit_px)
    gross = sum(b["size"] * (b["exit_px"] - b["price"]) * sign for b in camp["batches"])
    base = camp["batches"][0]
    base_gross = base["size"] * (base["exit_px"] - base["price"]) * sign
    fees = sum(b["size"] * (b["price"] + b["exit_px"]) for b in camp["batches"]) * fee_rate
    camp.update(
        exit_ts=exit_ts, exit_px=exit_px, gross=gross,
        base_gross=base_gross, adds_gross=gross - base_gross,
        fees=fees, net=gross - fees,
        n_adds=len(camp["batches"]) - 1,
        n_stopped=sum(1 for b in camp["batches"] if b.get("stopped")),
        is_open=is_open,
    )
    return camp


def equity_stats(campaigns: list) -> dict:
    """逐笔累计净值曲线与最大回撤（按出场时间排序）"""
    cum = peak = max_dd = 0.0
    trough_ts = None
    curve = []
    for c in sorted(campaigns, key=lambda x: x["exit_ts"]):
        cum += c["net"]
        curve.append((c["exit_ts"], cum))
        if cum > peak:
            peak = cum
        if peak - cum > max_dd:
            max_dd = peak - cum
            trough_ts = c["exit_ts"]
    return {"curve": curve, "net": cum, "max_dd": max_dd, "trough_ts": trough_ts}


def run_backtest(config: dict, df_4h: pd.DataFrame, df_daily: pd.DataFrame,
                 fee_bps: float = 4.5, start_ts=None,
                 reconfigure=None, collect: list = None) -> dict:
    """完整策略回测

    Args:
        start_ts: 该时刻（含）之后才开始交易（之前的 K 线仅存在于数据中，不产生信号）
        reconfigure: callable(bar_ts) -> dict|None，返回新 config 时热切换参数（保留持仓状态）
        collect: 传入 list 时收集信号明细（展示用）
    Returns:
        {"campaigns": [...], "net": $, "max_dd": $, "curve": [(ts, cum$)], "trough_ts": ts}
    """
    engine = SignalEngine(config, RiskManager(config))
    fee_rate = fee_bps / 10000.0
    campaigns: list = []
    camp = None
    has_pos = False

    for i in range(60, len(df_4h)):
        w4 = df_4h.iloc[max(0, i - WINDOW_4H):i]
        bar_ts = w4.index[-1]
        if start_ts is not None and bar_ts < start_ts:
            continue
        if reconfigure is not None:
            new_cfg = reconfigure(bar_ts)
            if new_cfg:
                engine = swap_engine(engine, new_cfg)

        # 只用已收盘日线：日线 D 收盘于 D+24h，须不晚于当前 4H K 线收盘时刻
        bar_close_time = bar_ts + pd.Timedelta(hours=4)
        wd = df_daily[df_daily.index + pd.Timedelta(days=1) <= bar_close_time]
        if len(wd) > WINDOW_DAILY:
            wd = wd.iloc[-WINDOW_DAILY:]
        if len(wd) < 30:
            continue

        px = w4["close"].iloc[-1]

        # 批次盘中止损优先（模拟交易所批次止损单）
        if has_pos:
            events, closed = engine.check_batch_stops(w4)
            for ev in events:
                if camp:
                    _mark_batch_exit(camp, ev)
                if collect is not None:
                    collect.append({
                        "time": bar_ts, "price": ev["fill_px"], "phase": ev["phase"],
                        "direction": camp["direction"] if camp else "?",
                        "action": "exit" if closed else "reduce",
                        "reason": (f"批次止损: {'底仓' if ev.get('is_base') else '加仓'}"
                                   f"@{ev['price']:.0f} 触及止损 {ev['stop']:.0f}"),
                        "entry_price": None, "stop_loss": ev.get("stop"),
                    })
            if closed:
                has_pos = False
                if camp:
                    campaigns.append(_settle(camp, events[-1]["fill_px"], bar_ts, fee_rate))
                    camp = None
                continue

        signals = engine.run_all_checks(wd, w4, has_position=has_pos)
        for sig in signals:
            if sig.action == "exit":
                has_pos = False
                if camp:
                    campaigns.append(_settle(camp, px, bar_ts, fee_rate))
                    camp = None
            else:
                has_pos = True
                if sig.action == "entry" or camp is None:
                    camp = {"entry_ts": bar_ts, "direction": sig.direction, "batches": []}
                camp["batches"].append({
                    "price": sig.entry_price or px,
                    "size": sig.suggested_size or 0.0,
                    "phase": sig.phase,
                })
            if collect is not None:
                collect.append({
                    "time": bar_ts, "price": px, "phase": sig.phase,
                    "direction": sig.direction, "action": sig.action,
                    "reason": sig.reason, "entry_price": sig.entry_price,
                    "stop_loss": sig.stop_loss_price,
                })

    if camp:
        campaigns.append(_settle(camp, df_4h["close"].iloc[-1], df_4h.index[-1],
                                 fee_rate, is_open=True))

    stats = equity_stats(campaigns)
    return {"campaigns": campaigns, **stats}
