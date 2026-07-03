#!/usr/bin/env python3
"""监控仪表盘 — FastAPI 后端 + HTML 前端"""

import os, sys, yaml, logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from exchange.hl_client import HLClient
from storage.db import init_db, get_recent_signals, get_latest_market_state
from core.indicators import calc_adx, calc_ema, calc_vwma, calc_bollinger_bands, calc_ema_slope, count_ma_crosses
from storage.models import Kline, BEIJING_TZ
import pandas as pd

logging.basicConfig(level=logging.WARNING)

app = FastAPI(title="4H Trading Monitor")

config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(config_path) as f:
    config = yaml.safe_load(f)

init_db()
network = "testnet"
pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
addr = os.environ.get("HYPERLIQUID_WALLET_ADDRESS")
client = HLClient(private_key=pk, account_address=addr, network=network)

TEMPLATE = (Path(__file__).parent / "templates" / "dashboard.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index():
    return TEMPLATE


@app.get("/api/status")
def api_status():
    symbol = config["symbol"]

    # Account
    account = {"balance": 0, "positions": [], "pnl": 0}
    if client.account_address:
        state = client.get_user_state(client.account_address)
        if state:
            account["balance"] = round(float(state.get("marginSummary", {}).get("accountValue", 0)), 2)
            for p in state.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == symbol:
                    szi = float(pos.get("szi", 0))
                    account["positions"].append({
                        "coin": pos["coin"], "side": "LONG" if szi > 0 else "SHORT",
                        "size": abs(szi), "entry": round(float(pos.get("entryPx", 0)), 1),
                        "pnl": round(float(pos.get("unrealizedPnl", 0)), 2),
                        "liq": round(float(pos.get("liquidationPx", 0)), 1),
                        "leverage": int(pos.get("leverage", {}).get("value", 0)),
                    })
                    account["pnl"] += float(pos.get("unrealizedPnl", 0))
        account["pnl"] = round(account["pnl"], 2)

    # Market — 优先 DB，否则实时计算
    mids = client.get_all_mids()
    btc_price = float(mids.get(symbol, 0))
    market = {"price": btc_price, "adx_4h": 0, "direction": "—", "phase": "—",
              "ranging": False, "ema9": 0, "vwma20": 0, "bb": 0, "bb_expand": False}

    db_state = get_latest_market_state(symbol)
    if db_state:
        market.update({
            "adx_4h": round(db_state.adx_4h, 1),
            "direction": db_state.daily_direction.upper(),
            "phase": db_state.current_phase,
            "ranging": db_state.is_ranging,
            "ema9": round(db_state.ema9_4h, 1),
            "vwma20": round(db_state.vwma20_4h, 1),
            "bb": round(db_state.bb_bandwidth, 4),
            "bb_expand": db_state.bb_expanding,
        })
    else:
        # 实时计算指标
        try:
            raw_4h = client.fetch_ohlcv(coin=symbol, interval="4h", start_time=0)
            raw_1d = client.fetch_ohlcv(coin=symbol, interval="1d", start_time=0)
            if raw_4h and len(raw_4h) >= 30 and raw_1d and len(raw_1d) >= 20:
                k4 = [Kline.from_api_candle(c, symbol, "4h") for c in raw_4h[-200:]]
                kd = [Kline.from_api_candle(c, symbol, "1d") for c in raw_1d[-100:]]
                def to_df(kl):
                    d=[{"open_time":k.open_time,"open":k.open,"high":k.high,"low":k.low,"close":k.close,"volume":k.volume} for k in kl]
                    df=pd.DataFrame(d);df.set_index("open_time",inplace=True);df.sort_index(inplace=True);return df
                df4=to_df(k4);dfd=to_df(kd)
                adx4=calc_adx(df4,14).iloc[-1]
                ema9=calc_ema(df4["close"],9).iloc[-1]
                vwma20=calc_vwma(df4,20).iloc[-1]
                _,_,_,bb=calc_bollinger_bands(df4,20)
                ema9d=calc_ema(dfd["close"],9)
                vwma20d=calc_vwma(dfd,20)
                slope_d=calc_ema_slope(ema9d)
                price_vs=dfd["close"].iloc[-1]>vwma20d.iloc[-1]
                is_ranging=float(adx4)<20
                if not is_ranging:
                    dir_="long" if (price_vs and slope_d=="up") else ("short" if (not price_vs and slope_d=="down") else "none")
                else:
                    dir_="none"
                market.update({
                    "adx_4h":round(float(adx4),1),
                    "direction":dir_.upper(),
                    "ranging":is_ranging,
                    "ema9":round(float(ema9),1),
                    "vwma20":round(float(vwma20),1),
                    "bb":round(float(bb.iloc[-1]),4),
                    "phase":"实时计算",
                })
        except Exception as e:
            logging.warning(f"实时指标计算失败: {e}")

    # Signals
    signals = []
    for s in get_recent_signals(20):
        signals.append({
            "time": s.timestamp.strftime("%m-%d %H:%M") if s.timestamp else "-",
            "phase": s.phase, "direction": s.direction.upper(), "action": s.action,
            "entry": round(s.entry_price, 1) if s.entry_price else None,
            "stop": round(s.stop_loss_price, 1) if s.stop_loss_price else None,
            "reason": (s.reason or "")[:80],
        })

    # Config
    sys_cfg = {
        "symbol": symbol, "network": network, "leverage": config["leverage"],
        "atr_buffer": config["take_profit"]["atr_buffer"],
        "reentry": config["cooldown"]["phase_two_reentry_bars"],
        "phase4_cd": config["cooldown"]["phase_four_min_bars"],
        "risk": config["risk"]["max_risk_per_trade"],
    }

    return {
        "account": account, "market": market, "signals": signals,
        "config": sys_cfg, "ts": datetime.now(tz=BEIJING_TZ).strftime("%H:%M:%S"),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
