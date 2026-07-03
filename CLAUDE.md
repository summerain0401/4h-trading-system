# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BTC 4H swing trading system — a semi-automated trading bot that detects trend-following entry/exit signals on Hyperliquid. Uses a four-phase signal engine: Phase 1 (direction confirmation) → Phase 2 (trend start, golden cross entry) → Phase 3 (trend acceleration, SHORT only) → Phase 4 (pullback to VWMA, dual-mode: add or independent entry). All logic is derived from `4H波段交易手册.md`.

## Commands

```bash
# Setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Interactive CLI menu
python main.py

# Direct monitoring mode (5-min check interval)
python main.py monitor

# Quick market status snapshot
python main.py status

# Backtest (flexible date ranges)
python verify_signals.py                        # default: last 90 days mainnet
python verify_signals.py --days 180             # last 180 days
python verify_signals.py --start 2025-10-01 --end 2025-12-31  # specific dates

# Web dashboard (FastAPI on port 8080)
python dashboard.py

# Run tests
pytest tests/ -v
```

## Timezone

All timestamps use **UTC+8 (Beijing time)**. Defined as `BEIJING_TZ` in `storage/models.py`. API data is converted to Beijing time in `Kline.from_api_candle`.

## Environment

Set via `.env` or shell environment:
- `PRIVATE_KEY` / `HYPERLIQUID_PRIVATE_KEY` — API wallet private key for signing orders
- `HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_MAIN_ADDRESS` — main wallet address for trade execution
- Without these, the system runs in read-only mode (signals only, no order placement)

All other parameters are in `config.yaml`.

## Architecture

```
main.py                         # CLI entry point, TradingSystem class orchestrates everything
dashboard.py                    # FastAPI + server-side HTML dashboard (port 8080)
verify_signals.py               # Flexible backtest script (--start/--end/--days)
config.yaml                     # All trading parameters (including direction.LONG/SHORT)

core/
  signal_engine.py              # THE HEART — four-phase signal detection + stop/take-profit
  indicators.py                 # Pure-function indicators (EMA, VWMA, ADX, ATR, BB, DI, CI)
  patterns.py                   # Candlestick pattern recognition
  risk_manager.py               # Position sizing from max loss, stop-loss price calculation
  order_executor.py             # Translates Signals into exchange orders (entry/add/exit)
  data_fetcher.py               # Pulls OHLCV from Hyperliquid, stores in SQLite
  param_optimizer.py            # Grid-search optimizer for atr_buffer/reentry/cooldown params

exchange/
  hl_client.py                  # Hyperliquid: data via native REST, orders via official Python SDK

storage/
  models.py                     # Dataclasses: Kline, Signal, Position, MarketState; BEIJING_TZ
  db.py                         # SQLite CRUD for klines, signals, market_states

notify/
  terminal.py                   # Colorful terminal output with signal cards
  desktop.py                    # macOS notification center alerts via osascript

templates/
  dashboard.html                # Single-page dashboard UI
```

## Key Design Decisions (V9)

- **Asymmetric LONG/SHORT**: Different stop loss (2.0/2.5 ATR), trailing buffer (0.8/1.0 ATR), timeout (12/8 bars).
- **Phase 3 SHORT-only**: LONG trend grinding makes Phase 3 adds counterproductive. SHORT keeps Phase 3.
- **Profit protection (3-tier)**: <5%=20VWMA normal, 5-10%=9EMA+0.5ATR, >10%=9EMA no buffer (lock profit).
- **Ranging detection**: ADX<20 (daily) + BB+DI convergence (4H) + Choppiness Index>65 (4H). Three independent conditions.
- **Dual cooldown**: Min 3 bars after all exits; meltdown 8 bars when two consecutive >2% losses.
- **Phase 4 dual-mode**: Has position=add; no position=independent entry (half-size, wider stop).
- **Signal engine pure functions**: Receives DataFrames, returns Signal objects. No side effects.
- **Risk-first sizing**: Position = max_loss / |entry - stop_loss|.
- **Entry timing (V9)**: Phase 2 enters on cross bar (i=1) or approaching (<0.8 ATR gap, not 3-5 bars).
- **Optimization sync**: Always update `4H波段交易手册.md` after code changes. See memory rule.
