#!/usr/bin/env python3
"""4H 波段半自动交易系统 — CLI 入口

使用方法：
    python main.py              # 交互模式（CLI 菜单）
    python main.py monitor      # 直接启动监控模式
    python main.py status       # 快速查看当前市场状态

依赖：
    pip install -r requirements.txt
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage.db import init_db, insert_signal, get_recent_signals
from exchange.hl_client import HLClient
from core.data_fetcher import DataFetcher
from core.risk_manager import RiskManager
from core.signal_engine import SignalEngine
from core.backtester import swap_engine
from core.param_optimizer import ParamOptimizer
from core.order_executor import OrderExecutor
from notify.terminal import (
    print_banner, print_section, print_signal,
    print_market_status, print_position_summary, print_account_summary,
    Color, format_price,
)
from notify.desktop import notify_signal, notify_stop_loss_triggered, notify_ranging_market

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class TradingSystem:
    """交易系统主类"""

    def __init__(self, config: dict, private_key: str | None = None, account_address: str | None = None):
        self.config = config
        self.symbol = config["symbol"]
        self.leverage = config["leverage"]
        self.client = HLClient(private_key=private_key, account_address=account_address, network=config["network"])
        self.fetcher = DataFetcher(self.client, config)
        self.risk = RiskManager(config)
        self.engine = SignalEngine(config, self.risk)
        self.optimizer = ParamOptimizer(config)
        self.executor = OrderExecutor(self.client, self.risk, config) if private_key else None
        self.df_daily = None
        self.df_4h = None
        self.has_position = False
        self.position_info = None
        self.current_price = None

    async def initialize(self):
        """系统初始化：建表 + 拉数据 + 首轮计算"""
        print_banner("4H 波段半自动交易系统")
        logger.info("初始化数据库...")
        init_db()
        logger.info("数据库就绪")
        logger.info("拉取历史 K 线数据...")
        data = await self.fetcher.fetch_and_store_historical()
        self.df_daily = data["1d"]
        self.df_4h = data["4h"]
        if len(self.df_4h) < 30:
            logger.warning("⚠️  4H K 线数据不足，信号可能不准确")
        logger.info(f"数据就绪: 日线 {len(self.df_daily)} 根, 4H {len(self.df_4h)} 根")
        self.current_price = await self.fetcher.get_current_price()
        if self.current_price:
            logger.info(f"{self.symbol} 当前价格: {format_price(self.current_price)}")
        await self._refresh_position()

    async def _refresh_position(self):
        """从交易所同步持仓状态"""
        if self.executor:
            pos = self.executor.sync_position()
            if pos:
                self.has_position = True
                self.position_info = pos
                logger.info(f"已有持仓: {pos['side'].upper()} {pos['size']}BTC @ {pos['entry_price']:.0f}")
            else:
                self.has_position = False
                self.position_info = None

    async def _refresh_data(self):
        """增量刷新数据"""
        try:
            data = await self.fetcher.refresh_klines()
            self.df_daily = data["1d"]
            self.df_4h = data["4h"]
        except Exception as e:
            logger.warning(f"数据刷新失败: {e}")

    async def run_check(self) -> list:
        """执行一轮信号检查"""
        if self.df_daily is None or self.df_4h is None:
            logger.error("数据未初始化")
            return []
        if len(self.df_daily) < 30 or len(self.df_4h) < 30:
            logger.warning("数据不足，跳过信号检测")
            return []
        price = await self.fetcher.get_current_price()
        if price:
            self.current_price = price
        signals = self.engine.run_all_checks(
            self.df_daily, self.df_4h,
            self.has_position, self.position_info, self.current_price,
        )
        for sig in signals:
            insert_signal(sig)
        return signals

    async def show_status(self):
        """显示当前市场状态"""
        await self._refresh_data()
        if self.df_daily is None or len(self.df_daily) < 30:
            print(f"\n{Color.RED}数据不足，无法显示状态。请先运行初始化。{Color.RESET}")
            return
        state = self.engine.build_market_state(self.df_daily, self.df_4h)
        print_market_status({
            "adx_4h": state.adx_4h, "is_ranging": state.is_ranging,
            "daily_direction": state.daily_direction,
            "current_phase": state.current_phase,
            "ema9_slope_4h": state.ema9_slope_4h,
            "bb_bandwidth": state.bb_bandwidth,
            "bb_expanding": state.bb_expanding,
        })
        recent = get_recent_signals(5)
        if recent:
            print_section("最近信号")
            for s in recent:
                dir_color = Color.GREEN if s.direction == "long" else Color.RED
                ack = "✓" if s.acknowledged else "○"
                ts = s.timestamp.strftime("%m-%d %H:%M") if s.timestamp else "-"
                print(
                    f"  {ack} {ts} | "
                    f"{dir_color}{s.phase} {s.direction} {s.action}{Color.RESET} | "
                    f"{s.reason[:60]}"
                )
            print()
        if self.current_price:
            print(f"  {Color.BOLD}{self.symbol} 当前价: {format_price(self.current_price)} USD{Color.RESET}")
            print()

    async def monitor_loop(self, check_interval: int = 300):
        """监控循环模式"""
        print_banner("监控模式启动")
        logger.info(f"检查间隔: {check_interval}s | 交易对: {self.symbol} | 杠杆: {self.leverage}x")
        logger.info("按 Ctrl+C 停止\n")
        check_count = 0
        last_ranging_notify = False
        try:
            while True:
                check_count += 1
                timestamp = datetime.now().strftime("%H:%M:%S")
                logger.info(f"[{timestamp}] 第 {check_count} 轮检查...")
                await self._refresh_data()
                # 定期校准（interval_days 周期，近期加权评分）
                if self.df_4h is not None and self.df_daily is not None:
                    new_params = self.optimizer.maybe_optimize(self.df_4h, self.df_daily)
                    if new_params:
                        logger.info(f"参数已更新: {new_params}")
                        opt_cfg = self.optimizer.apply_to_config(self.config)
                        # 热切换：保留持仓与冷却状态
                        self.engine = swap_engine(self.engine, opt_cfg)
                price = await self.fetcher.get_current_price()
                if price:
                    self.current_price = price
                signals = await self.run_check()
                if signals:
                    for sig in signals:
                        details = {
                            "entry_price": sig.entry_price,
                            "stop_loss_price": sig.stop_loss_price,
                            "take_profit_price": sig.take_profit_price,
                            "suggested_size": sig.suggested_size,
                            "reason": sig.reason,
                        }
                        print_signal(sig.phase, sig.direction, sig.action, details)

                        # 自动交易执行
                        if self.executor:
                            if sig.action == "exit":
                                result = self.executor.execute_exit(sig)
                                self.has_position = False
                                self.position_info = None
                                notify_stop_loss_triggered(self.symbol, self.current_price or 0)
                            elif sig.action == "entry":
                                result = self.executor.execute_entry(sig)
                                if result:
                                    self.has_position = True
                                    self.position_info = {
                                        "current_phase": "phase_two",
                                        "side": sig.direction,
                                        "stop_loss_price": sig.stop_loss_price,
                                        "entry_price": sig.entry_price,
                                        "entry_adx": 0,
                                    }
                                notify_signal(sig.phase, sig.direction, sig.action, sig.entry_price or 0)
                            elif sig.action == "add":
                                result = self.executor.execute_add(sig)
                                # 阶段三 SHORT 收紧整体止损：撤全部止损单按批次重挂
                                # （其余加仓的批次止损已在 execute_add 内单独挂出）
                                if sig.phase == "phase_three" and sig.direction == "short":
                                    batches = (self.engine._active_position or {}).get("batches") or []
                                    if batches:
                                        self.executor.replace_all_stops(sig.direction, batches)
                                    if sig.stop_loss_price and self.position_info:
                                        self.position_info["stop_loss_price"] = sig.stop_loss_price
                                logger.info(f"加仓: {sig.phase}")
                        else:
                            if sig.action == "exit":
                                notify_stop_loss_triggered(self.symbol, self.current_price or 0)
                            else:
                                trigger_price = sig.entry_price or self.current_price or 0
                                notify_signal(sig.phase, sig.direction, sig.action, trigger_price)
                else:
                    daily_dir = self.engine.check_daily_direction(self.df_daily)
                    if daily_dir["is_ranging"]:
                        if not last_ranging_notify:
                            logger.info("⏸️  震荡市 (ADX < 20)，暂停交易")
                            notify_ranging_market()
                            last_ranging_notify = True
                    else:
                        last_ranging_notify = False
                        logger.debug("无新信号")
                logger.debug(f"等待 {check_interval}s...")
                await asyncio.sleep(check_interval)
        except KeyboardInterrupt:
            print(f"\n{Color.YELLOW}监控已停止，共执行 {check_count} 轮检查{Color.RESET}")

    async def interactive_menu(self):
        """交互式 CLI 菜单"""
        while True:
            print()
            print(f"{Color.BOLD}{Color.CYAN}┌──────────────────────────────────────────┐{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│      4H 波段半自动交易系统                 │{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}├──────────────────────────────────────────┤{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[1]{Color.RESET} 查看市场状态                        {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[2]{Color.RESET} 启动监控模式                        {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[3]{Color.RESET} 查看信号历史                        {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[4]{Color.RESET} 查看持仓（需配置私钥）              {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[5]{Color.RESET} 手动下单（需配置私钥）              {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}│{Color.RESET}  {Color.BOLD}[0]{Color.RESET} 退出                                {Color.BOLD}{Color.CYAN}│{Color.RESET}")
            print(f"{Color.BOLD}{Color.CYAN}└──────────────────────────────────────────┘{Color.RESET}")
            choice = input(f"\n  {Color.BOLD}请选择 [{Color.RESET}0-5{Color.BOLD}]: {Color.RESET}").strip()

            if choice == "1":
                await self.show_status()
            elif choice == "2":
                interval = input(f"  检查间隔（秒，默认 300=5分钟）: ").strip()
                interval = int(interval) if interval.isdigit() else 300
                await self.monitor_loop(interval)
            elif choice == "3":
                signals = get_recent_signals(20)
                if not signals:
                    print(f"\n  {Color.DIM}暂无信号历史{Color.RESET}")
                else:
                    print_section("信号历史")
                    for s in signals:
                        dir_color = Color.GREEN if s.direction == "long" else Color.RED
                        ack = f"{Color.GREEN}✓{Color.RESET}" if s.acknowledged else f"{Color.DIM}○{Color.RESET}"
                        ts = s.timestamp.strftime("%m-%d %H:%M") if s.timestamp else "-"
                        print(f"  {ack} {ts} | {dir_color}{s.phase} {s.direction} {s.action}{Color.RESET}")
                        if s.reason:
                            print(f"      {Color.DIM}{s.reason[:80]}{Color.RESET}")
                    print()
            elif choice == "4":
                if not self.client._sync:
                    print(f"\n  {Color.RED}需要配置私钥才能查看持仓。{Color.RESET}")
                    print(f"  {Color.DIM}请在环境变量中设置 HYPERLIQUID_PRIVATE_KEY{Color.RESET}")
                else:
                    print(f"\n  {Color.YELLOW}持仓查询功能开发中...{Color.RESET}")
            elif choice == "5":
                if not self.client._sync:
                    print(f"\n  {Color.RED}需要配置私钥才能下单。{Color.RESET}")
                else:
                    print(f"\n  {Color.YELLOW}手动下单功能开发中...{Color.RESET}")
            elif choice == "0":
                print(f"\n{Color.DIM}再见{Color.RESET}")
                break
            else:
                print(f"\n  {Color.RED}无效选项{Color.RESET}")


async def main():
    config = load_config()
    private_key = os.environ.get("PRIVATE_KEY") or os.environ.get("HYPERLIQUID_PRIVATE_KEY")
    account_address = os.environ.get("HYPERLIQUID_WALLET_ADDRESS") or os.environ.get("HYPERLIQUID_MAIN_ADDRESS")
    system = TradingSystem(config, private_key, account_address)
    await system.initialize()

    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "monitor":
            await system.monitor_loop()
        elif command == "status":
            await system.show_status()
        else:
            print(f"未知命令: {command}")
            print("用法: python main.py [monitor|status]")
    else:
        await system.interactive_menu()


if __name__ == "__main__":
    asyncio.run(main())
