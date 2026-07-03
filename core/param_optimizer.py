"""参数优化器 — 每 30 天用近 90 天数据回测选最优参数

不预测市场，只适应市场。用近期实际表现说话。
"""

import logging, copy
from typing import Optional

import pandas as pd

from core.signal_engine import SignalEngine
from core.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class ParamOptimizer:
    """网格搜索最优参数组合"""

    OPTIMIZE_BARS = 180       # 每 30 天
    BACKTEST_DAYS = 90        # 回测窗口

    PARAM_GRID = {
        "atr_buffer": [0.5, 0.8, 1.0],
        "phase_two_reentry_bars": [3, 5, 8],
        "phase_four_cooldown": [3, 5, 8],
    }

    def __init__(self, base_config: dict):
        self.base_config = base_config
        self._best_params: Optional[dict] = None
        self._bars_since: int = 999

    def maybe_optimize(self, df_4h: pd.DataFrame, df_daily: pd.DataFrame) -> Optional[dict]:
        """每 N 根 K 线触发优化，返回最优参数或 None"""
        self._bars_since += 1
        if self._bars_since < self.OPTIMIZE_BARS:
            return None
        self._bars_since = 0
        return self._optimize(df_4h, df_daily)

    @property
    def best_params(self) -> Optional[dict]:
        return self._best_params

    def apply_to_config(self, config: dict) -> dict:
        if not self._best_params:
            return config
        c = copy.deepcopy(config)
        if "atr_buffer" in self._best_params:
            c["take_profit"]["atr_buffer"] = self._best_params["atr_buffer"]
        if "phase_two_reentry_bars" in self._best_params:
            c["cooldown"]["phase_two_reentry_bars"] = self._best_params["phase_two_reentry_bars"]
        if "phase_four_cooldown" in self._best_params:
            c["cooldown"]["phase_four_min_bars"] = self._best_params["phase_four_cooldown"]
        return c

    def _optimize(self, df_4h: pd.DataFrame, df_daily: pd.DataFrame) -> dict:
        cutoff = df_4h.index[-1] - pd.Timedelta(days=self.BACKTEST_DAYS)
        w4 = df_4h[df_4h.index >= cutoff]
        wd = df_daily[df_daily.index >= cutoff - pd.Timedelta(days=30)]

        if len(w4) < 60:
            logger.warning("优化数据不足")
            return {}

        best_score = -999
        best_combo = None

        for buf in self.PARAM_GRID["atr_buffer"]:
            for reentry in self.PARAM_GRID["phase_two_reentry_bars"]:
                for p4cd in self.PARAM_GRID["phase_four_cooldown"]:
                    score = self._backtest(w4, wd, buf, reentry, p4cd)
                    if score > best_score:
                        best_score = score
                        best_combo = (buf, reentry, p4cd)

        self._best_params = {
            "atr_buffer": best_combo[0],
            "phase_two_reentry_bars": best_combo[1],
            "phase_four_cooldown": best_combo[2],
        }
        logger.info(f"优化: buf={best_combo[0]} reentry={best_combo[1]} p4cd={best_combo[2]} score={best_score:+.1f}")
        return self._best_params

    def _backtest(self, w4, wd, buf, reentry, p4cd) -> float:
        config = copy.deepcopy(self.base_config)
        config["take_profit"]["atr_buffer"] = buf
        config["cooldown"]["phase_two_reentry_bars"] = reentry
        config["cooldown"]["phase_four_min_bars"] = p4cd

        engine = SignalEngine(config, RiskManager(config))
        has_pos = False; ep = None; ed = None; total = 0.0

        for i in range(60, len(w4)):
            wh = w4.iloc[:i]; wd_ = wd[wd.index <= wh.index[-1]]
            if len(wd_) < 30: continue
            sigs = engine.run_all_checks(wd_, wh, has_position=has_pos)
            for sig in sigs:
                if sig.action == "exit":
                    has_pos = False
                    if ep and ed:
                        px = wh["close"].iloc[-1]
                        total += (px-ep)/ep if ed=="long" else (ep-px)/ep
                        ep = ed = None
                else:
                    has_pos = True; ep = sig.entry_price or wh["close"].iloc[-1]; ed = sig.direction

        if ep and ed:
            lp = w4["close"].iloc[-1]
            total += (lp-ep)/ep if ed=="long" else (ep-lp)/ep
        return total * 100
