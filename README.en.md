# BTC 4H Swing Trading System

[English](README.md) | [简体中文](README.zh-CN.md)

A Hyperliquid BTC trend-following research and semi-automated trading tool using 4-hour candles with daily context. Includes terminal signals, historical backtesting, and a web dashboard.

**Execution boundary:** without a private key it reads market data and produces signals. With both an API-wallet private key and the main account address configured, the monitoring loop can submit real orders. The network is selected in `config.yaml`; monitoring is not a paper-trading mode.

## Strategy and implementation

Direction confirmation → crossover entry → SHORT trend acceleration → VWMA pullback add / independent entry. Includes asymmetric long/short parameters, per-batch stops, trailing exits, cooldowns, and range filters. The [Chinese strategy manual](4H波段交易手册.md) records rules and historical experiments. Historical returns are neither a current verification nor a forecast.

`core/backtester.py` is the shared accounting engine. Use `verify_signals.py` for interval backtests, `walkforward.py` for rolling out-of-sample comparisons, and `exit_experiment.py` for exit experiments. Adaptive parameter optimization is disabled by default; strategy changes must also update the manual.

## Install and start read-only

The source uses Python 3.10+ syntax. Use an isolated environment on macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py status
```

This fetches network data and initializes local SQLite. First make sure the shell has not inherited `PRIVATE_KEY` or `HYPERLIQUID_PRIVATE_KEY`.

```bash
python main.py             # Interactive menu
python main.py monitor     # Can place orders when keys are configured
python verify_signals.py --days 90
python walkforward.py --days 240 --interval 15 --mode all
```

The CLI reads **process environment variables**; it does not load `.env` automatically. Key names: `PRIVATE_KEY` or `HYPERLIQUID_PRIVATE_KEY`; account names: `HYPERLIQUID_WALLET_ADDRESS` or `HYPERLIQUID_MAIN_ADDRESS`. Inject them securely; never commit real credentials.

## Optional dashboard

```bash
pip install fastapi uvicorn
python dashboard.py
```

It binds to `0.0.0.0:8080`. FastAPI and Uvicorn are not listed in `requirements.txt` yet. The dashboard reads `HYPERLIQUID_PRIVATE_KEY` and `HYPERLIQUID_WALLET_ADDRESS`.

## Structure and limits

- `core/`: signals, indicators, risk, backtesting, and execution.
- `exchange/`: market REST and the Hyperliquid SDK.
- `storage/`: models and SQLite, using UTC+8 timestamps.
- `notify/`, `templates/`: notifications and dashboard views.

`tests/` currently contains no test cases. The interactive positions/manual-order menu still has incomplete logic; it is not a validated feature. No network backtests or live trades were performed for this documentation update.

## Risk and license

Research use only; crypto trading can cause losses. The previous README stated MIT, but the default branch has no standalone LICENSE file. This update does not establish or change the project license.
