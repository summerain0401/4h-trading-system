"""终端输出模块 — 彩色信号展示"""

import os, sys
from datetime import datetime


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def format_price(price: float, decimals: int = 1) -> str:
    return f"{price:,.{decimals}f}"


def print_banner(text: str):
    width = 60
    print()
    print(f"{Color.BOLD}{Color.CYAN}{'=' * width}{Color.RESET}")
    print(f"{Color.BOLD}{Color.CYAN}  {text}{Color.RESET}")
    print(f"{Color.BOLD}{Color.CYAN}{'=' * width}{Color.RESET}")
    print()


def print_section(title: str):
    print(f"\n{Color.BOLD}{Color.YELLOW}── {title} ──{Color.RESET}")


def print_signal(phase: str, direction: str, action: str, details: dict):
    """打印交易信号"""
    phase_names = {
        "phase_one": "阶段一 · 方向确认",
        "phase_two": "阶段二 · 趋势启动",
        "phase_three": "阶段三 · 趋势加速",
        "phase_four": "阶段四 · 趋势回调",
    }
    phase_name = phase_names.get(phase, phase)
    direction_color = Color.GREEN if direction == "long" else Color.RED
    direction_text = "做多 LONG" if direction == "long" else "做空 SHORT"
    action_text = {"entry": "开仓", "add": "加仓", "exit": "平仓", "alert": "提醒"}.get(action, action)

    width = 58
    print()
    print(f"{Color.BOLD}{Color.YELLOW}{'┌' + '─' * width + '┐'}{Color.RESET}")
    print(
        f"{Color.YELLOW}│{Color.RESET} "
        f"{Color.BOLD}{Color.MAGENTA}🚨 交易信号 🚨{Color.RESET}".ljust(width + 16)
        + f"{Color.YELLOW}│{Color.RESET}"
    )
    print(
        f"{Color.YELLOW}│{Color.RESET} "
        f"  {Color.BOLD}{phase_name}{Color.RESET}".ljust(width + 16)
        + f"{Color.YELLOW}│{Color.RESET}"
    )
    print(
        f"{Color.YELLOW}│{Color.RESET} "
        f"  {direction_color}{Color.BOLD}{direction_text} → {action_text}{Color.RESET}".ljust(width + 22)
        + f"{Color.YELLOW}│{Color.RESET}"
    )
    print(
        f"{Color.YELLOW}│{Color.RESET} "
        f"  {Color.DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC+8')}{Color.RESET}".ljust(width + 22)
        + f"{Color.YELLOW}│{Color.RESET}"
    )
    print(f"{Color.YELLOW}│{' ' * (width + 2)}│{Color.RESET}")

    if details.get("entry_price"):
        print(
            f"{Color.YELLOW}│{Color.RESET} "
            f"  入场价: {Color.BOLD}{format_price(details['entry_price'])}{Color.RESET}".ljust(width + 19)
            + f"{Color.YELLOW}│{Color.RESET}"
        )
    if details.get("stop_loss_price"):
        print(
            f"{Color.YELLOW}│{Color.RESET} "
            f"  止损价: {Color.RED}{format_price(details['stop_loss_price'])}{Color.RESET}".ljust(width + 19)
            + f"{Color.YELLOW}│{Color.RESET}"
        )
    if details.get("take_profit_price"):
        print(
            f"{Color.YELLOW}│{Color.RESET} "
            f"  止盈价: {Color.GREEN}{format_price(details['take_profit_price'])}{Color.RESET}".ljust(width + 19)
            + f"{Color.YELLOW}│{Color.RESET}"
        )
    if details.get("suggested_size"):
        print(
            f"{Color.YELLOW}│{Color.RESET} "
            f"  建议仓位: {Color.BOLD}{details['suggested_size']:.4f} 张{Color.RESET}".ljust(width + 19)
            + f"{Color.YELLOW}│{Color.RESET}"
        )
    if details.get("reason"):
        reason = details["reason"]
        print(
            f"{Color.YELLOW}│{Color.RESET} "
            f"  {Color.DIM}原因: {reason[:width-4]}{Color.RESET}".ljust(width + 16)
            + f"{Color.YELLOW}│{Color.RESET}"
        )

    print(f"{Color.YELLOW}{'└' + '─' * width + '┘'}{Color.RESET}")
    print()


def print_market_status(state: dict):
    """打印市场状态摘要"""
    print_section("市场状态")

    adx = state.get("adx_4h", 0)
    adx_color = Color.RED if adx < 20 else (Color.YELLOW if adx < 25 else Color.GREEN)
    ranging_text = "震荡市 ⛔" if state.get("is_ranging") else "趋势市 ✅"

    direction = state.get("daily_direction", "none")
    dir_color = Color.GREEN if direction == "long" else (Color.RED if direction == "short" else Color.YELLOW)

    print(f"  ADX(4H):     {adx_color}{adx:.1f}{Color.RESET}")
    print(f"  市场状态:    {adx_color}{ranging_text}{Color.RESET}")
    print(f"  大周期方向:  {dir_color}{direction.upper()}{Color.RESET}")
    print(f"  当前阶段:    {Color.CYAN}{state.get('current_phase', '-')}{Color.RESET}")
    print(f"  9EMA 斜率:   {state.get('ema9_slope_4h', '-')}")
    bb_exp = "📈扩张" if state.get("bb_expanding") else ""
    print(f"  布林带带宽:  {state.get('bb_bandwidth', 0):.4f} {bb_exp}")
    print()


def print_position_summary(positions: list):
    """打印持仓摘要"""
    print_section("当前持仓")
    if not positions:
        print(f"  {Color.DIM}无持仓{Color.RESET}")
        return

    for p in positions:
        pnl_color = Color.GREEN if p.unrealized_pnl > 0 else Color.RED
        print(
            f"  {p.coin} {p.side.upper()} | "
            f"仓位: {p.size:.4f} | "
            f"入场: {format_price(p.entry_price)} | "
            f"未实现盈亏: {pnl_color}{p.unrealized_pnl:+.2f} USD{Color.RESET}"
        )
        if p.stop_loss_price:
            print(f"    止损: {Color.RED}{format_price(p.stop_loss_price)}{Color.RESET}", end="")
        if p.take_profit_price:
            print(f"  止盈: {Color.GREEN}{format_price(p.take_profit_price)}{Color.RESET}", end="")
        print(f"  强平: {Color.DIM}{format_price(p.liquidation_price)}{Color.RESET}")
    print()


def print_account_summary(account_value: float, margin_used: float, withdrawable: float):
    """打印账户摘要"""
    print_section("账户")
    print(f"  账户净值:   {Color.BOLD}{account_value:,.2f} USD{Color.RESET}")
    print(f"  已用保证金: {margin_used:,.2f} USD")
    print(f"  可用余额:   {withdrawable:,.2f} USD")
    print()
