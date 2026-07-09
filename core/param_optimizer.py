"""定期校准器 — 系统哲学：不追求万能参数，定期用近期数据校准

每 interval_days（默认 15 天）触发一次：用近 lookback_days（默认 90 天）数据
网格回测候选参数，评分选优后热切换（保留持仓与冷却状态）。

评分 = 近期加权净利 − dd_penalty × 最大回撤
- 近期加权：每笔交易按出场距今天数以 half_life_days 半衰期指数衰减，
  时间越近的表现权重越高
- 回撤惩罚：同等收益下优先选净值曲线更平滑的参数组

回测口径与 verify_signals 完全一致（core/backtester.run_backtest）。
"""

import copy
import itertools
import logging
from typing import Optional

import pandas as pd

from core.backtester import run_backtest

logger = logging.getLogger(__name__)


class ParamOptimizer:
    """网格搜索 + 近期加权评分的定期校准器"""

    PARAM_GRID = {
        "trailing_scale": [0.75, 1.0, 1.25],   # 跟踪止盈缓冲缩放（乘到多空各自 trailing_atr）
        "add_risk_scale": [0.5, 1.0],          # 加仓批次风险系数
        "phase_four_cooldown": [3, 5, 8],      # 阶段四加仓冷却（根 K）
    }

    def __init__(self, base_config: dict):
        self.base_config = base_config
        cal = base_config.get("calibration", {})
        self.interval_days = cal.get("interval_days", 15)
        self.lookback_days = cal.get("lookback_days", 90)
        self.half_life_days = cal.get("half_life_days", 30)
        self.dd_penalty = cal.get("dd_penalty", 1.0)
        self.fee_bps = cal.get("fee_bps", 4.5)
        self._best_params: Optional[dict] = None
        self._last_calib_ts: Optional[pd.Timestamp] = None

    @property
    def best_params(self) -> Optional[dict]:
        return self._best_params

    def maybe_optimize(self, df_4h: pd.DataFrame, df_daily: pd.DataFrame) -> Optional[dict]:
        """到达校准周期时触发，返回最优参数或 None"""
        if len(df_4h) == 0:
            return None
        now = df_4h.index[-1]
        if (self._last_calib_ts is not None
                and (now - self._last_calib_ts) < pd.Timedelta(days=self.interval_days)):
            return None
        self._last_calib_ts = now
        return self.calibrate(df_4h, df_daily, now)

    def calibrate(self, df_4h: pd.DataFrame, df_daily: pd.DataFrame,
                  now_ts: pd.Timestamp) -> dict:
        """用截至 now_ts 的近 lookback_days 数据选参（只用过去数据，无泄漏）"""
        start = now_ts - pd.Timedelta(days=self.lookback_days)
        # 预热余量：指标窗口 + 日线 30 根门槛
        w4 = df_4h[(df_4h.index >= start - pd.Timedelta(days=30)) & (df_4h.index <= now_ts)]
        wd = df_daily[(df_daily.index >= start - pd.Timedelta(days=60)) & (df_daily.index <= now_ts)]
        if len(w4) < 200 or len(wd) < 40:
            logger.warning("校准数据不足，保持当前参数")
            return {}

        keys = list(self.PARAM_GRID.keys())
        best_score, best_params = -1e18, None
        for values in itertools.product(*(self.PARAM_GRID[k] for k in keys)):
            params = dict(zip(keys, values))
            cfg = self.apply_params(copy.deepcopy(self.base_config), params)
            res = run_backtest(cfg, w4, wd, fee_bps=self.fee_bps, start_ts=start)
            score = self.score(res["campaigns"], res["max_dd"], now_ts)
            if score > best_score:
                best_score, best_params = score, params

        self._best_params = best_params
        logger.info(f"校准完成 @ {now_ts:%m-%d}: {best_params} score={best_score:+.0f}")
        return best_params or {}

    def score(self, campaigns: list, max_dd: float, now_ts: pd.Timestamp) -> float:
        """近期加权净利 − 回撤惩罚"""
        weighted_net = 0.0
        for c in campaigns:
            age_days = max((now_ts - c["exit_ts"]).total_seconds() / 86400.0, 0.0)
            weighted_net += c["net"] * 0.5 ** (age_days / self.half_life_days)
        return weighted_net - self.dd_penalty * max_dd

    @staticmethod
    def apply_params(config: dict, params: dict) -> dict:
        """把校准参数写进 config（就地修改并返回）"""
        if not params:
            return config
        if "trailing_scale" in params:
            scale = params["trailing_scale"]
            for side in ("long", "short"):
                base = config["direction"][side]["trailing_atr"]
                config["direction"][side]["trailing_atr"] = round(base * scale, 3)
        if "add_risk_scale" in params:
            config.setdefault("risk", {})["add_risk_scale"] = params["add_risk_scale"]
        if "phase_four_cooldown" in params:
            config["cooldown"]["phase_four_min_bars"] = params["phase_four_cooldown"]
        return config

    def apply_to_config(self, config: dict) -> dict:
        """返回应用了最优参数的新 config（不修改原对象）"""
        return self.apply_params(copy.deepcopy(config), self._best_params or {})
