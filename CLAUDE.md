# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BTC 4H swing trading system — a semi-automated trading bot that detects trend-following entry/exit signals on Hyperliquid. Uses a four-phase signal engine: Phase 1 (direction confirmation) → Phase 2 (trend start, golden cross entry) → Phase 3 (trend acceleration, SHORT only) → Phase 4 (pullback to VWMA, dual-mode: add or independent entry). All logic is derived from `4H波段交易手册.md` — the manual is the strategy's source of truth and must be kept in sync with code changes.

## Commands

```bash
# Setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Interactive CLI menu / monitoring / status
python main.py
python main.py monitor          # 5-min check interval, auto-trades if keys configured
python main.py status

# Backtest (shared accounting core, see Backtest Fidelity below)
python verify_signals.py                        # last 90 days mainnet
python verify_signals.py --days 180             # last 180 days
python verify_signals.py --start 2025-10-01 --end 2025-12-31
python verify_signals.py --fee-bps 4.5 --account 10000   # fee/capital overrides

# Rolling out-of-sample validation (mandatory gate for any new adaptive idea)
python walkforward.py --days 240 --interval 15 --mode all    # params + regime-gate arms
python walkforward.py --days 240 --mode gate                 # cheap arm only (~8 min)
# NOTE: --mode params runs a grid per boundary (~30 min for 240 days) — run in background

# Two-window exit-parameter experiment (selection window + validation window)
python exit_experiment.py

# Web dashboard (FastAPI on port 8080)
python dashboard.py

# Run tests (tests/ is currently empty)
pytest tests/ -v
```

## Timezone

All timestamps use **UTC+8 (Beijing time)**. Defined as `BEIJING_TZ` in `storage/models.py`. API data is converted in `Kline.from_api_candle`.

## Environment

Set via `.env` or shell environment:
- `PRIVATE_KEY` / `HYPERLIQUID_PRIVATE_KEY` — API wallet private key for signing orders
- `HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_MAIN_ADDRESS` — main wallet address for trade execution
- Without these, the system runs in read-only mode (signals only, no order placement)

All other parameters are in `config.yaml`.

## Architecture

```
main.py                         # CLI entry, TradingSystem orchestrates; monitor loop auto-executes signals
dashboard.py                    # FastAPI + server-side HTML dashboard (port 8080)
verify_signals.py               # Backtest CLI (thin wrapper around core/backtester)
walkforward.py                  # Rolling out-of-sample validation (params arm + regime-gate arm)
exit_experiment.py              # Two-window (selection/validation) exit-parameter experiments
config.yaml                     # All trading parameters incl. direction.LONG/SHORT, calibration

core/
  signal_engine.py              # THE HEART — four-phase detection, batch tracking, stop/take-profit
  backtester.py                 # Shared backtest core: batch accounting, intrabar batch stops, fees,
                                #   equity/drawdown stats, engine hot-swap (swap_engine)
  indicators.py                 # Pure-function indicators (EMA, VWMA, ADX, ATR, BB, DI, CI)
  patterns.py                   # Candlestick pattern recognition
  risk_manager.py               # Position sizing from max loss, stop-loss price calculation
  order_executor.py             # Signals → exchange orders; per-batch reduce-only stop orders
  data_fetcher.py               # Pulls OHLCV from Hyperliquid, stores in SQLite
  param_optimizer.py            # Periodic calibrator (recency-weighted score − DD penalty).
                                #   DISABLED by default — see Validated Findings

exchange/hl_client.py           # Data via native REST; orders via official Python SDK
storage/                        # models.py (Kline/Signal/Position, BEIJING_TZ), db.py (SQLite)
notify/                         # terminal.py (colored output), desktop.py (macOS notifications)
```

### Position/batch model (core invariant)

A position ("campaign") = base batch + add batches. Each batch carries its own entry price, size, and stop. The engine tracks batches in `_active_position["batches"]`; live execution mirrors this with one reduce-only stop order per fill. **An add's tight stop closes only that batch; the base keeps riding.** Trailing take-profit / timeout / ADX-decay exits close the whole position. Phase-3 SHORT tightens all batch stops to the P3 stop (manual rule) — live side re-hangs all stop orders via `OrderExecutor.replace_all_stops`.

### Engine state rules

- Cooldowns/timeouts are computed from **timestamps** (`_bars_between`), never DataFrame row indices — live uses a fixed-length rolling window where `len(df)-1` does not grow.
- `SignalEngine` is stateful (position, cooldowns, circuit breaker). When rebuilding with new config, use `core.backtester.swap_engine` to preserve state.
- Signal sizes are computed against a fixed $10,000 account; total notional is capped at account × leverage × `risk.max_total_position_pct` (enforced in `_cap_size`).
- `trading_halt: true` in config blocks all entries/adds but leaves exits untouched.

## Backtest Fidelity (do not regress these)

`core/backtester.run_backtest` is the single accounting truth shared by verify/calibrator/walkforward:
1. **No daily look-ahead**: only daily bars whose close time ≤ current 4H bar close are visible.
2. **Batch accounting**: adds' PnL attributed separately from base (base-only PnL was how the old +215% illusion happened).
3. **Fees**: taker bps on every fill, both sides.
4. **Intrabar batch stops**: stops fill at stop price on bar high/low touch (gap → open price); trailing exits stay close-based (matches manual).
5. Engine windows are capped (150×4H / 60×daily) for O(n) speed; warmup residue is negligible.

## Validated Findings (out-of-sample, 2026-07; details in 手册 回测验证 section)

- **Static params, always-on, wins.** 240d OOS: static +45.2% vs 15-day param-chasing +25.8% vs performance-based halt −2.1%. Both adaptive layers are rejected; `calibration.enabled: false` by default.
- **Payoff structure is bleed-then-burst**: small losses are the premium for crash-trades (one trade can be ~40%+ of period PnL). Performance gating exits right before payoff.
- **LONG exit widening rejected**: trail 0.8 ATR / timeout 12 bars is optimal in both experiment windows.
- **Regime dependence**: 2024-12→2025-11 window lost ~−33% under all variants. Sizing must assume a losing year is possible.
- **Any new adaptive/parameter idea must pass walkforward.py (no-leakage, pre-committed selection rule) before going live.**

## Key Design Decisions (V9)

- **Asymmetric LONG/SHORT**: stop 2.0/2.5 ATR, trailing buffer 0.8/1.0 ATR, timeout 12/8 bars.
- **Phase 3 SHORT-only**: LONG trend grinding makes Phase 3 adds counterproductive.
- **Profit protection (3-tier)**: <5% = 20VWMA normal; 5-10% = 9EMA+0.5ATR; >10% = 9EMA no buffer.
- **Ranging detection**: daily ADX<20 + 4H BB+DI convergence + 4H Choppiness>65 (independent conditions).
- **Dual cooldown**: min 3 bars after exits; 8-bar meltdown after two consecutive >2% losses.
- **Phase 4 dual-mode**: has position = add; no position = independent entry (half-size, wider stop).
- **Risk-first sizing**: size = max_loss / |entry − stop|; adds scaled by `risk.add_risk_scale`.
- **Entry timing**: Phase 2 enters on cross bar (i=1) or approaching (<0.8 ATR gap).
- **Manual sync rule**: after any strategy/parameter/experiment change, update `4H波段交易手册.md` (rules, params, backtest numbers). ADX uses EMA smoothing (not Wilder) — values differ from TradingView; thresholds are calibrated to this in-house flavor.
